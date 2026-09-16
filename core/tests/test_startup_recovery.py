import importlib.util
from pathlib import Path
import unittest
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _load_recovery_module():
    path = REPOSITORY_ROOT / "docker/scripts/startup_recovery.py"
    spec = importlib.util.spec_from_file_location("startup_recovery", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


startup_recovery = _load_recovery_module()
RecoveryPolicy = startup_recovery.RecoveryPolicy


class RecoveryPolicyTests(unittest.TestCase):
    def test_automatic_retry_waits_and_shares_single_use_manual_budget(self):
        now = [0.0]
        policy = RecoveryPolicy(clock=lambda: now[0])
        for attempt in range(3):
            policy.enter_failure("gunicorn_start_failed", automatic=True)
            state = policy.snapshot(True)
            self.assertEqual(state["automatic_retry_after_seconds"], 30)
            now[0] += 29.9
            self.assertFalse(policy.accept_automatic(True))
            now[0] += 0.1
            self.assertFalse(policy.accept_automatic(False))
            self.assertEqual(policy.accepted_count, attempt)
            self.assertTrue(policy.accept_automatic(True))
            self.assertFalse(policy.accept_automatic(True))
            self.assertNotEqual(policy.accept(state["token"], state["generation"], True)[0], 202)
        policy.enter_failure("gunicorn_start_failed", automatic=True)
        now[0] += 300
        self.assertFalse(policy.accept_automatic(True))
        self.assertEqual(policy.snapshot(True)["reason"], "exhausted")
        self.assertNotIn("automatic_retry_after_seconds", policy.snapshot(True))

    def test_manual_acceptance_cancels_scheduled_automatic_retry(self):
        now = [0.0]
        policy = RecoveryPolicy(clock=lambda: now[0])
        policy.enter_failure("gunicorn_start_failed", automatic=True)
        state = policy.snapshot(True)
        self.assertEqual(policy.accept(state["token"], state["generation"], True)[0], 202)
        now[0] = 60
        self.assertFalse(policy.accept_automatic(True))
        self.assertEqual(policy.accepted_count, 1)

    def test_automatic_retry_is_not_enabled_for_unclassified_failure(self):
        now = [0.0]
        policy = RecoveryPolicy(clock=lambda: now[0])
        policy.enter_failure("gunicorn_start_failed")
        now[0] = 300
        self.assertFalse(policy.accept_automatic(True))
        self.assertTrue(policy.snapshot(True)["available"])

    def test_snapshot_exposes_only_allowlisted_blocking_reason(self):
        policy, _ = self.make_policy()
        policy.enter_failure("gunicorn_start_failed")
        snapshot = policy.snapshot("children_pending")
        self.assertFalse(snapshot["available"])
        self.assertEqual(snapshot["blocked_reason"], "children_pending")
        self.assertIsNone(snapshot["token"])
        self.assertNotIn("blocked_reason", policy.snapshot("private exception text"))

    def make_policy(self, now=None, tokens=None):
        if now is None:
            now = [100.0]
        if tokens is None:
            tokens = iter(
                (
                    "generation-1",
                    "token-1",
                    "generation-2",
                    "token-2",
                    "generation-3",
                    "token-3",
                )
            )
        policy = RecoveryPolicy(
            clock=lambda: now[0],
            token_factory=lambda size: next(tokens),
        )
        return policy, now

    def test_accept_is_single_use_and_budget_survives_failure(self):
        now = [100.0]
        policy = RecoveryPolicy(clock=lambda: now[0])
        policy.enter_failure("worker_exit_timeout")
        state = policy.snapshot(True)
        status, _ = policy.accept(state["token"], state["generation"], True)
        self.assertEqual(status, 202)
        status, _ = policy.accept(state["token"], state["generation"], True)
        self.assertNotEqual(status, 202)
        policy.enter_failure("gunicorn_start_failed")
        self.assertEqual(policy.snapshot(True)["remaining_attempts"], 2)
        self.assertFalse(policy.snapshot(True)["available"])
        now[0] += 30
        self.assertTrue(policy.snapshot(True)["available"])

    def test_snapshot_has_exact_fields_and_does_not_mutate_state(self):
        policy, _ = self.make_policy()
        policy.enter_failure("worker_exit_timeout")

        first = policy.snapshot(True)
        second = policy.snapshot(True)

        self.assertEqual(
            set(first),
            {
                "schema_version",
                "available",
                "reason",
                "remaining_attempts",
                "retry_after_seconds",
                "generation",
                "token",
            },
        )
        self.assertEqual(first, second)
        self.assertEqual(policy.accepted_count, 0)

    def test_snapshot_token_is_stable_while_available(self):
        policy, _ = self.make_policy()
        policy.enter_failure("worker_exit_timeout")

        self.assertEqual(policy.snapshot(True)["token"], "token-1")
        self.assertEqual(policy.snapshot(True)["token"], "token-1")

    def test_snapshot_only_discloses_token_for_a_possible_failure_generation(self):
        policy, now = self.make_policy()
        self.assertIsNone(policy.snapshot(True)["token"])

        policy.enter_failure("not_recoverable")
        self.assertIsNone(policy.snapshot(True)["token"])
        policy.enter_failure("worker_exit_timeout")
        self.assertIsNone(policy.snapshot(False)["token"])

        state = policy.snapshot(True)
        policy.accept(state["token"], state["generation"], True)
        policy.enter_failure("gunicorn_start_failed")
        self.assertEqual(policy.snapshot(True)["token"], "token-3")
        now[0] += 30
        self.assertEqual(policy.snapshot(True)["token"], "token-3")

    def test_unrecoverable_error_is_unavailable(self):
        policy, _ = self.make_policy()
        policy.enter_failure("worker_protocol_invalid")

        state = policy.snapshot(True)
        status, payload = policy.accept("token-1", "generation-1", True)

        self.assertFalse(state["available"])
        self.assertEqual(state["reason"], "unavailable")
        self.assertEqual(status, 409)
        self.assertEqual(payload["reason"], "unavailable")
        self.assertEqual(policy.accepted_count, 0)

    def test_leave_failure_invalidates_generation_and_token(self):
        policy, _ = self.make_policy()
        policy.enter_failure("worker_exit_timeout")
        state = policy.snapshot(True)

        policy.leave_failure()
        status, payload = policy.accept(state["token"], state["generation"], True)

        self.assertEqual(status, 409)
        self.assertEqual(payload["reason"], "unavailable")
        self.assertIsNone(policy.snapshot(True)["generation"])
        self.assertIsNone(policy.snapshot(True)["token"])

    def test_eligible_requires_the_exact_true_boolean(self):
        for eligible in (False, 1, "true", None):
            with self.subTest(eligible=eligible):
                policy, _ = self.make_policy()
                policy.enter_failure("worker_exit_timeout")
                state = policy.snapshot(True)

                snapshot = policy.snapshot(eligible)
                status, payload = policy.accept(
                    state["token"], state["generation"], eligible
                )

                self.assertFalse(snapshot["available"])
                self.assertEqual(snapshot["reason"], "unsafe_state")
                self.assertIsNone(snapshot["token"])
                self.assertEqual(status, 409)
                self.assertEqual(payload["reason"], "unsafe_state")
                self.assertEqual(policy.accepted_count, 0)

    def test_invalid_credentials_are_forbidden_without_consuming_budget(self):
        cases = (
            ("wrong-token", "generation-1"),
            ("token-1", "wrong-generation"),
            (None, "generation-1"),
            ("token-1", None),
            (1, "generation-1"),
            ("token-1", 1),
        )
        for token, generation in cases:
            with self.subTest(token=token, generation=generation):
                policy, _ = self.make_policy()
                policy.enter_failure("worker_exit_timeout")

                status, payload = policy.accept(token, generation, True)

                self.assertEqual(status, 403)
                self.assertEqual(payload["reason"], "invalid_token")
                self.assertIsNone(payload["token"])
                self.assertEqual(policy.accepted_count, 0)
                self.assertEqual(policy.snapshot(True)["remaining_attempts"], 3)

    def test_invalid_credentials_remain_forbidden_during_cooldown(self):
        policy, _ = self.make_policy()
        policy.enter_failure("worker_exit_timeout")
        state = policy.snapshot(True)
        policy.accept(state["token"], state["generation"], True)
        policy.enter_failure("gunicorn_start_failed")

        status, payload = policy.accept("wrong-token", "generation-2", True)

        self.assertEqual(status, 403)
        self.assertEqual(payload["reason"], "invalid_token")
        self.assertEqual(policy.accepted_count, 1)

    def test_first_accept_is_immediately_available(self):
        policy, _ = self.make_policy()
        policy.enter_failure("worker_exit_timeout")
        state = policy.snapshot(True)

        status, payload = policy.accept(state["token"], state["generation"], True)

        self.assertEqual(status, 202)
        self.assertEqual(payload["reason"], "accepted")
        self.assertEqual(payload["remaining_attempts"], 2)
        self.assertEqual(payload["retry_after_seconds"], 30)
        self.assertIsNone(payload["token"])
        self.assertEqual(policy.accepted_count, 1)

    def test_cooldown_rounds_up_and_opens_at_exact_boundary(self):
        policy, now = self.make_policy()
        policy.enter_failure("worker_exit_timeout")
        state = policy.snapshot(True)
        policy.accept(state["token"], state["generation"], True)
        policy.enter_failure("gunicorn_start_failed")

        now[0] = 129.001
        state = policy.snapshot(True)
        self.assertEqual(state["reason"], "cooldown")
        self.assertEqual(state["retry_after_seconds"], 1)
        status, payload = policy.accept(
            state["token"], state["generation"], True
        )
        self.assertEqual(status, 429)
        self.assertEqual(payload["reason"], "cooldown")
        now[0] = 130.0
        state = policy.snapshot(True)
        self.assertTrue(state["available"])
        self.assertEqual(state["reason"], "available")
        self.assertEqual(state["retry_after_seconds"], 0)

    def test_three_accepted_attempts_exhaust_budget(self):
        tokens = iter(
            (
                "generation-1",
                "token-1",
                "generation-2",
                "token-2",
                "generation-3",
                "token-3",
                "generation-4",
                "token-4",
            )
        )
        policy, now = self.make_policy(tokens=tokens)

        for attempt in range(3):
            policy.enter_failure("worker_exit_timeout")
            state = policy.snapshot(True)
            status, _ = policy.accept(state["token"], state["generation"], True)
            self.assertEqual(status, 202)
            now[0] += 30

        policy.enter_failure("gunicorn_start_failed")
        state = policy.snapshot(True)
        status, payload = policy.accept(state["token"], state["generation"], True)

        self.assertEqual(policy.accepted_count, 3)
        self.assertEqual(state["remaining_attempts"], 0)
        self.assertFalse(state["available"])
        self.assertEqual(state["reason"], "exhausted")
        self.assertIsNone(state["token"])
        self.assertEqual(status, 409)
        self.assertEqual(payload["reason"], "exhausted")
        for token, generation in (
            ("token-4", "generation-4"),
            ("token-3", "generation-3"),
            (None, None),
        ):
            with self.subTest(token=token, generation=generation):
                status, payload = policy.accept(token, generation, True)
                self.assertEqual(status, 409)
                self.assertEqual(payload["reason"], "exhausted")
                self.assertEqual(policy.accepted_count, 3)

    def test_wall_clock_changes_cannot_change_monotonic_budget(self):
        monotonic = [100.0]
        wall_clock = [1_000_000.0]
        with mock.patch.object(
            startup_recovery.time,
            "time",
            side_effect=lambda: wall_clock[0],
        ):
            policy, _ = self.make_policy(now=monotonic)
            policy.enter_failure("worker_exit_timeout")
            state = policy.snapshot(True)
            policy.accept(state["token"], state["generation"], True)
            policy.enter_failure("gunicorn_start_failed")

            wall_clock[0] -= 500_000.0
            self.assertEqual(policy.snapshot(True)["retry_after_seconds"], 30)
            wall_clock[0] += 2_000_000.0
            self.assertEqual(policy.snapshot(True)["retry_after_seconds"], 30)

    def test_accepted_count_is_read_only(self):
        policy, _ = self.make_policy()

        with self.assertRaises(AttributeError):
            policy.accepted_count = 2


if __name__ == "__main__":
    unittest.main()
