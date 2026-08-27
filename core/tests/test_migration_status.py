import json
import os
import stat
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from docker.scripts import migration_status


RUN_ID = (
    "20260825T120000Z-"
    "12345678-1234-4678-9234-567812345678"
)
RUN_ID_2 = (
    "20260825T130000Z-"
    "87654321-4321-4876-a234-567812345678"
)


class FakeClock(object):
    def __init__(self):
        self.value = datetime(2026, 8, 27, 1, 2, 3, tzinfo=timezone.utc)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.value

    def advance(self, seconds=1):
        self.value += timedelta(seconds=seconds)


class MigrationStatusStoreTests(SimpleTestCase):
    maxDiff = None

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.status_directory = Path(self.temporary.name) / "runtime"
        self.clock = FakeClock()
        self.owner_patch = mock.patch.object(
            migration_status,
            "_runtime_owner_ids",
            return_value=(os.getuid(), os.getgid()),
        )
        self.owner_patch.start()
        self.addCleanup(self.owner_patch.stop)
        self.addCleanup(self.temporary.cleanup)

    @property
    def marker_path(self):
        return self.status_directory / "maintenance"

    @property
    def status_path(self):
        return self.status_directory / "migration-status.json"

    def store(self):
        return migration_status.MigrationStatusStore(
            str(self.status_directory),
            self.clock,
            33,
            33,
        )

    def prepared_store(self):
        store = self.store()
        store.prepare_runtime_gate()
        return store

    def initialized_store(self):
        store = self.prepared_store()
        store.initialize()
        return store

    def payload(self):
        return json.loads(self.status_path.read_text(encoding="utf-8"))

    def planning(self, **updates):
        event = {
            "phase": "planning",
            "run_id": RUN_ID,
            "attempt": 1,
            "resume_count": 0,
            "started_at": "2026-08-25T12:00:00Z",
            "images_total": 100,
            "files_total": 200,
            "backfill_total": 100,
        }
        event.update(updates)
        return event

    def recovery(self, **updates):
        event = {
            "phase": "recovery",
            "run_id": RUN_ID,
            "attempt": 1,
            "resume_count": 0,
            "started_at": "2026-08-25T12:00:00Z",
            "images_done": 0,
            "images_total": 100,
            "files_done": 0,
            "files_total": 200,
            "backfill_done": 0,
            "backfill_total": 100,
            "last_committed_batch": 0,
        }
        event.update(updates)
        return event

    def start_run(self, store=None, **updates):
        store = store or self.initialized_store()
        store.apply_worker_event(self.planning(**updates))
        return store

    def test_initial_status_has_exact_schema_and_canonical_bytes(self):
        store = self.initialized_store()
        expected = {
            "schema_version": 1,
            "state": "starting",
            "phase": "preparing",
            "phase_label": "실행 환경 확인",
            "run_id": None,
            "attempt": 0,
            "resume_count": 0,
            "started_at": None,
            "phase_started_at": "2026-08-27T01:02:03Z",
            "heartbeat_at": "2026-08-27T01:02:03Z",
            "progress_at": None,
            "last_committed_batch": 0,
            "images_done": 0,
            "images_total": None,
            "files_done": 0,
            "files_total": None,
            "backfill_done": 0,
            "backfill_total": None,
            "phase_percent": None,
            "overall_percent": None,
            "error_class": None,
            "error_code": None,
        }
        self.assertEqual(set(expected), migration_status.PUBLIC_STATUS_FIELDS)
        self.assertEqual(self.payload(), expected)
        expected_bytes = (
            json.dumps(
                expected,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        self.assertEqual(self.status_path.read_bytes(), expected_bytes)
        self.assertEqual(self.marker_path.read_bytes(), b"maintenance\n")
        self.assertEqual(self.clock.calls, 1)
        for forbidden in (b"/data/", b"token", b"password", b"sha256", b".png"):
            self.assertNotIn(forbidden, expected_bytes)
        self.assertFalse(store._dirty)

    def test_initialize_requires_a_prepared_and_still_valid_gate(self):
        store = self.store()
        with self.assertRaisesMessage(
            migration_status.StatusError,
            "runtime_gate_not_prepared",
        ):
            store.initialize()
        self.assertFalse(self.status_path.exists())

        store.prepare_runtime_gate()
        self.marker_path.unlink()
        with self.assertRaisesMessage(
            migration_status.StatusError,
            "runtime_gate_not_prepared",
        ):
            store.initialize()
        self.assertFalse(self.status_path.exists())

    def test_initial_status_failure_preserves_gate_and_dirty_snapshot(self):
        store = self.prepared_store()
        with mock.patch.object(
            store,
            "_atomic_write",
            side_effect=OSError("disk full"),
        ):
            with self.assertRaisesMessage(
                migration_status.StatusError,
                "migration_status_write_failed",
            ):
                store.initialize()
        self.assertTrue(self.marker_path.exists())
        self.assertFalse(self.status_path.exists())
        self.assertTrue(store._dirty)

        store.publish_pending()
        self.assertEqual(self.payload()["state"], "starting")

    def test_constructor_rejects_invalid_service_identity(self):
        for uid, gid in ((True, 1), (1, False), (-1, 1), (1, -1)):
            with self.subTest(uid=uid, gid=gid):
                with self.assertRaisesMessage(
                    migration_status.StatusError,
                    "runtime_identity_invalid",
                ):
                    migration_status.MigrationStatusStore(
                        str(self.status_directory), self.clock, uid, gid
                    )

    def test_every_worker_phase_requires_its_exact_key_set(self):
        valid = (
            {"phase": "preparing"},
            {"phase": "snapshot"},
            self.planning(),
            self.recovery(),
            {
                "phase": "upgrade_v2",
                "images_done": 1,
                "images_total": 2,
                "last_committed_batch": 1,
            },
            {
                "phase": "copying",
                "images_done": 1,
                "images_total": 100,
                "files_done": 1,
                "files_total": 200,
            },
            {
                "phase": "database",
                "images_done": 1,
                "images_total": 100,
                "last_committed_batch": 1,
            },
            {"phase": "backfill_planning", "backfill_total": 100},
            {
                "phase": "backfill_registering",
                "backfill_done": 1,
                "backfill_total": 100,
                "last_committed_batch": 1,
            },
            {"phase": "archive"},
            {"phase": "finalizing"},
            {"phase": "complete"},
            {"phase": "error", "error_code": "legacy_startup_failed"},
        )
        for event in valid:
            with self.subTest(phase=event["phase"]):
                extra = dict(event, unexpected="secret")
                missing = dict(event)
                missing.pop(next(iter(sorted(set(event) - {"phase"})), "phase"))
                for invalid in (extra, missing):
                    store = self.initialized_store()
                    with self.assertRaisesMessage(
                        migration_status.StatusError,
                        "worker_protocol_invalid",
                    ):
                        store.apply_worker_event(invalid)

    def test_worker_event_rejects_invalid_numbers_and_run_identity(self):
        invalid_events = (
            self.planning(images_total=True),
            self.planning(files_total=-1),
            self.recovery(images_done=101),
            self.recovery(files_done=201),
            self.recovery(backfill_done=101),
            self.planning(run_id="run-1"),
            self.planning(run_id=(
                "20260825T120000Z-"
                "abcdefab-cdef-4abc-8abc-abcdefabcdef"
            ).upper()),
            self.planning(run_id=(
                "20260825T120000Z-"
                "12345678-1234-4678-7234-567812345678"
            )),
            self.planning(started_at="2026-08-25T12:00:00+09:00"),
        )
        for event in invalid_events:
            with self.subTest(event=event):
                with self.assertRaisesMessage(
                    migration_status.StatusError,
                    "worker_protocol_invalid",
                ):
                    self.initialized_store().apply_worker_event(event)

    def test_planning_and_recovery_freeze_attempt_and_totals(self):
        store = self.start_run()
        store.apply_worker_event(self.recovery())
        self.assertEqual(self.payload()["attempt"], 1)
        for event in (
            self.recovery(files_total=201),
            self.planning(attempt=2, resume_count=1),
        ):
            with self.subTest(event=event):
                with self.assertRaisesMessage(
                    migration_status.StatusError,
                    "worker_protocol_invalid",
                ):
                    store.apply_worker_event(event)

    def test_recovery_restores_fresh_store_and_resumes_exactly_once(self):
        store = self.initialized_store()
        recovered = self.recovery(
            attempt=3,
            resume_count=2,
            images_done=20,
            files_done=40,
            backfill_done=10,
            last_committed_batch=7,
        )
        store.apply_worker_event(recovered)
        payload = self.payload()
        self.assertEqual(payload["attempt"], 3)
        self.assertEqual(payload["resume_count"], 2)
        self.assertEqual(payload["last_committed_batch"], 7)
        self.assertEqual(payload["started_at"], "2026-08-25T12:00:00Z")

        store.apply_worker_event(self.recovery(
            attempt=4,
            resume_count=3,
            images_done=20,
            files_done=40,
            backfill_done=10,
            last_committed_batch=7,
        ))
        self.assertEqual(self.payload()["attempt"], 4)
        for attempt in (3, 6):
            with self.assertRaisesMessage(
                migration_status.StatusError,
                "worker_protocol_invalid",
            ):
                store.apply_worker_event(self.recovery(
                    attempt=attempt,
                    resume_count=attempt - 1,
                    images_done=20,
                    files_done=40,
                    backfill_done=10,
                    last_committed_batch=7,
                ))

    def test_new_run_resets_progress_and_requires_first_attempt(self):
        store = self.start_run()
        store.apply_worker_event({
            "phase": "copying",
            "images_done": 50,
            "images_total": 100,
            "files_done": 100,
            "files_total": 200,
        })
        store.apply_worker_event(self.recovery(
            run_id=RUN_ID_2,
            attempt=1,
            resume_count=0,
            started_at="2026-08-25T13:00:00Z",
        ))
        payload = self.payload()
        self.assertEqual(payload["run_id"], RUN_ID_2)
        self.assertEqual(payload["overall_percent"], 0.0)
        self.assertEqual(payload["images_done"], 0)
        self.assertEqual(payload["files_done"], 0)

        another = self.start_run()
        with self.assertRaisesMessage(
            migration_status.StatusError,
            "worker_protocol_invalid",
        ):
            another.apply_worker_event(self.recovery(
                run_id=RUN_ID_2,
                attempt=2,
                resume_count=1,
                started_at="2026-08-25T13:00:00Z",
            ))

    def test_phase_and_overall_percent_use_frozen_totals(self):
        store = self.start_run()
        self.assertEqual(self.payload()["overall_percent"], 0.0)
        store.apply_worker_event({
            "phase": "copying",
            "images_done": 20,
            "images_total": 100,
            "files_done": 100,
            "files_total": 200,
        })
        payload = self.payload()
        self.assertEqual(payload["phase_percent"], 50.0)
        self.assertEqual(payload["overall_percent"], 30.0)
        store.apply_worker_event({
            "phase": "copying",
            "images_done": 100,
            "images_total": 100,
            "files_done": 200,
            "files_total": 200,
        })
        store.apply_worker_event({
            "phase": "database",
            "images_done": 100,
            "images_total": 100,
            "last_committed_batch": 1,
        })
        self.assertEqual(self.payload()["phase_percent"], 100.0)
        self.assertEqual(self.payload()["overall_percent"], 75.0)
        store.apply_worker_event({
            "phase": "backfill_registering",
            "backfill_done": 100,
            "backfill_total": 100,
            "last_committed_batch": 2,
        })
        self.assertEqual(self.payload()["overall_percent"], 99.0)
        store.apply_worker_event({"phase": "finalizing"})
        store.apply_worker_event({"phase": "complete"})
        self.assertEqual(self.payload()["phase_percent"], 100.0)
        self.assertEqual(self.payload()["overall_percent"], 100.0)

    def test_zero_denominator_and_phase_percent_contract(self):
        store = self.start_run(
            images_total=0,
            files_total=0,
            backfill_total=0,
        )
        self.assertEqual(self.payload()["overall_percent"], 0.0)
        store.apply_worker_event({
            "phase": "copying",
            "images_done": 0,
            "images_total": 0,
            "files_done": 0,
            "files_total": 0,
        })
        self.assertEqual(self.payload()["phase_percent"], 100.0)
        store.apply_worker_event({"phase": "finalizing"})
        store.apply_worker_event({"phase": "complete"})
        self.assertEqual(self.payload()["overall_percent"], 100.0)

    def test_partial_v2_total_is_phase_local_only(self):
        store = self.start_run()
        store.apply_worker_event({
            "phase": "upgrade_v2",
            "images_done": 10,
            "images_total": 20,
            "last_committed_batch": 1,
        })
        payload = self.payload()
        self.assertEqual(payload["images_total"], 100)
        self.assertEqual(payload["images_done"], 10)
        self.assertEqual(payload["phase_percent"], 50.0)
        with self.assertRaisesMessage(
            migration_status.StatusError,
            "worker_protocol_invalid",
        ):
            store.apply_worker_event({
                "phase": "upgrade_v2",
                "images_done": 21,
                "images_total": 101,
                "last_committed_batch": 2,
            })

    def test_global_commit_ordinal_crosses_all_commit_phases(self):
        store = self.start_run()
        upgrade = {
            "phase": "upgrade_v2",
            "images_done": 10,
            "images_total": 10,
            "last_committed_batch": 1,
        }
        store.apply_worker_event(upgrade)
        store.apply_worker_event({
            "phase": "copying",
            "images_done": 20,
            "images_total": 100,
            "files_done": 50,
            "files_total": 200,
        })
        before_replay = self.status_path.read_bytes()
        store.apply_worker_event(upgrade)
        self.assertEqual(self.status_path.read_bytes(), before_replay)
        store.apply_worker_event({
            "phase": "database",
            "images_done": 20,
            "images_total": 100,
            "last_committed_batch": 2,
        })
        store.apply_worker_event({
            "phase": "backfill_registering",
            "backfill_done": 10,
            "backfill_total": 100,
            "last_committed_batch": 3,
        })
        self.assertEqual(self.payload()["last_committed_batch"], 3)

    def test_commit_ordinal_rejects_jump_decrease_and_payload_conflict(self):
        store = self.start_run()
        first = {
            "phase": "database",
            "images_done": 10,
            "images_total": 100,
            "last_committed_batch": 1,
        }
        store.apply_worker_event(first)
        invalid = (
            dict(first, images_done=11),
            {
                "phase": "database",
                "images_done": 20,
                "images_total": 100,
                "last_committed_batch": 3,
            },
            {
                "phase": "backfill_registering",
                "backfill_done": 1,
                "backfill_total": 100,
                "last_committed_batch": 0,
            },
        )
        for event in invalid:
            with self.subTest(event=event):
                with self.assertRaisesMessage(
                    migration_status.StatusError,
                    "worker_protocol_invalid",
                ):
                    store.apply_worker_event(event)

    def test_recovery_ordinal_restores_then_requires_exact_next_commit(self):
        store = self.initialized_store()
        store.apply_worker_event(self.recovery(last_committed_batch=7))
        recovery_payload = self.payload()
        self.clock.advance(seconds=5)
        replay = {
            "phase": "database",
            "images_done": 0,
            "images_total": 100,
            "last_committed_batch": 7,
        }
        store.apply_worker_event(replay)
        payload = self.payload()
        self.assertEqual(payload["last_committed_batch"], 7)
        self.assertEqual(payload["phase"], "database")
        self.assertEqual(payload["state"], "migrating")
        self.assertEqual(payload["phase_percent"], 0.0)
        self.assertEqual(payload["phase_started_at"], "2026-08-27T01:02:08Z")
        self.assertEqual(
            payload["progress_at"], recovery_payload["progress_at"]
        )
        store.apply_worker_event(dict(replay, images_done=1, last_committed_batch=8))
        self.assertEqual(self.payload()["last_committed_batch"], 8)

    def test_copying_rejects_commit_ordinal(self):
        store = self.start_run()
        with self.assertRaisesMessage(
            migration_status.StatusError,
            "worker_protocol_invalid",
        ):
            store.apply_worker_event({
                "phase": "copying",
                "images_done": 1,
                "images_total": 100,
                "files_done": 1,
                "files_total": 200,
                "last_committed_batch": 1,
            })

    def test_duplicate_event_is_byte_and_syscall_noop(self):
        store = self.start_run()
        event = {
            "phase": "copying",
            "images_done": 1,
            "images_total": 100,
            "files_done": 2,
            "files_total": 200,
        }
        store.apply_worker_event(event)
        before = self.status_path.read_bytes()
        before_calls = self.clock.calls
        with mock.patch.object(store, "_atomic_write", wraps=store._atomic_write) as writer:
            store.apply_worker_event(dict(event))
        self.assertEqual(writer.call_count, 0)
        self.assertEqual(self.status_path.read_bytes(), before)
        self.assertEqual(self.clock.calls, before_calls)

    def test_phase_progress_and_heartbeat_timestamps_are_independent(self):
        store = self.start_run()
        planned = self.payload()
        self.clock.advance(5)
        store.apply_worker_event({
            "phase": "copying",
            "images_done": 0,
            "images_total": 100,
            "files_done": 0,
            "files_total": 200,
        })
        entered = self.payload()
        self.assertNotEqual(entered["phase_started_at"], planned["phase_started_at"])
        self.assertIsNone(entered["progress_at"])

        self.clock.advance(5)
        store.apply_worker_event({
            "phase": "copying",
            "images_done": 1,
            "images_total": 100,
            "files_done": 1,
            "files_total": 200,
        })
        progressed = self.payload()
        self.assertEqual(progressed["phase_started_at"], entered["phase_started_at"])
        self.assertEqual(progressed["progress_at"], "2026-08-27T01:02:13Z")
        heartbeat_before = progressed["heartbeat_at"]

        self.clock.advance(5)
        store.heartbeat()
        heartbeat = self.payload()
        self.assertEqual(heartbeat["progress_at"], progressed["progress_at"])
        self.assertEqual(heartbeat["phase_started_at"], entered["phase_started_at"])
        self.assertNotEqual(heartbeat["heartbeat_at"], heartbeat_before)

    def test_same_clock_heartbeat_and_clean_publish_are_syscall_noops(self):
        store = self.initialized_store()
        before = self.status_path.read_bytes()
        with mock.patch.object(store, "_atomic_write", wraps=store._atomic_write) as writer:
            store.heartbeat()
            store.publish_pending()
        self.assertEqual(writer.call_count, 0)
        self.assertEqual(self.status_path.read_bytes(), before)

    def test_dirty_snapshots_coalesce_and_publish_without_new_timestamp(self):
        store = self.start_run()
        original_write = store._atomic_write
        event1 = {
            "phase": "copying",
            "images_done": 1,
            "images_total": 100,
            "files_done": 1,
            "files_total": 200,
        }
        event2 = dict(event1, images_done=2, files_done=2)
        with mock.patch.object(store, "_atomic_write", side_effect=OSError("full")):
            with self.assertRaisesMessage(
                migration_status.StatusError,
                "migration_status_write_failed",
            ):
                store.apply_worker_event(event1)
            self.clock.advance(5)
            with self.assertRaisesMessage(
                migration_status.StatusError,
                "migration_status_write_failed",
            ):
                store.apply_worker_event(event2)
        pending_time = store._status["progress_at"]
        self.assertTrue(store._dirty)
        with mock.patch.object(store, "_atomic_write", wraps=original_write) as writer:
            store.publish_pending()
        self.assertEqual(writer.call_count, 1)
        self.assertFalse(store._dirty)
        self.assertEqual(self.payload()["images_done"], 2)
        self.assertEqual(self.payload()["progress_at"], pending_time)

    def test_dirty_duplicate_only_retries_latest_pending_bytes(self):
        store = self.start_run()
        event = {
            "phase": "copying",
            "images_done": 1,
            "images_total": 100,
            "files_done": 1,
            "files_total": 200,
        }
        original_write = store._atomic_write
        with mock.patch.object(store, "_atomic_write", side_effect=OSError("full")):
            with self.assertRaises(migration_status.StatusError):
                store.apply_worker_event(event)
        before_calls = self.clock.calls
        with mock.patch.object(store, "_atomic_write", wraps=original_write) as writer:
            store.apply_worker_event(dict(event))
        self.assertEqual(writer.call_count, 1)
        self.assertEqual(self.clock.calls, before_calls)
        self.assertEqual(self.payload()["images_done"], 1)

    def test_atomic_publication_orders_sync_replace_and_directory_sync(self):
        store = self.prepared_store()
        calls = []
        original_write = os.write
        original_fchmod = os.fchmod
        original_fsync = os.fsync
        original_replace = os.replace

        def write(fd, payload):
            calls.append("write")
            return original_write(fd, payload)

        def fchmod(fd, mode):
            calls.append("fchmod")
            return original_fchmod(fd, mode)

        def fsync(fd):
            calls.append("fsync")
            return original_fsync(fd)

        def replace(source, target):
            calls.append("replace")
            return original_replace(source, target)

        with mock.patch.object(migration_status.os, "write", side_effect=write):
            with mock.patch.object(
                migration_status.os, "fchmod", side_effect=fchmod
            ):
                with mock.patch.object(
                    migration_status.os, "fsync", side_effect=fsync
                ):
                    with mock.patch.object(
                        migration_status.os,
                        "replace",
                        side_effect=replace,
                    ):
                        store.initialize()
        self.assertEqual(
            calls,
            ["write", "fchmod", "fsync", "replace", "fsync"],
        )
        self.assertEqual(stat.S_IMODE(self.status_path.stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(self.status_directory.stat().st_mode), 0o755)

    def test_runtime_gate_rejects_symlink_non_directory_mode_and_owner(self):
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        self.status_directory.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesMessage(
            migration_status.StatusError,
            "runtime_gate_fail_closed_failed",
        ):
            self.store().prepare_runtime_gate()
        self.status_directory.unlink()

        self.status_directory.mkdir(mode=0o700)
        with self.assertRaisesMessage(
            migration_status.StatusError,
            "runtime_gate_fail_closed_failed",
        ):
            self.store().prepare_runtime_gate()

        self.status_directory.chmod(0o755)
        with mock.patch.object(
            migration_status,
            "_runtime_owner_ids",
            return_value=(os.getuid() + 1, os.getgid()),
        ):
            with self.assertRaisesMessage(
                migration_status.StatusError,
                "runtime_gate_fail_closed_failed",
            ):
                self.store().prepare_runtime_gate()

    def test_existing_marker_and_status_objects_fail_closed(self):
        store = self.prepared_store()
        self.marker_path.write_bytes(b"wrong\n")
        with self.assertRaisesMessage(
            migration_status.StatusError,
            "runtime_gate_fail_closed_failed",
        ):
            store.create_gate()

        self.marker_path.write_bytes(b"maintenance\n")
        self.marker_path.chmod(0o644)
        target = Path(self.temporary.name) / "target"
        target.write_text("safe", encoding="utf-8")
        self.status_path.symlink_to(target)
        with self.assertRaisesMessage(
            migration_status.StatusError,
            "migration_status_write_failed",
        ):
            store.initialize()
        self.assertEqual(target.read_text(encoding="utf-8"), "safe")

    def test_create_gate_is_idempotent_but_requires_preparation(self):
        store = self.store()
        with self.assertRaisesMessage(
            migration_status.StatusError,
            "runtime_gate_not_prepared",
        ):
            store.create_gate()
        store.prepare_runtime_gate()
        before = self.marker_path.read_bytes()
        store.create_gate()
        self.assertEqual(self.marker_path.read_bytes(), before)

    def test_complete_starting_service_and_ready_preserve_progress(self):
        store = self.initialized_store()
        store.apply_worker_event({"phase": "complete"})
        complete = self.payload()
        self.assertEqual(complete["state"], "migrating")
        self.assertEqual(complete["phase"], "complete")
        self.assertEqual(complete["phase_percent"], 100.0)
        self.assertEqual(complete["overall_percent"], 100.0)

        store.starting_service()
        starting = self.payload()
        self.assertEqual(starting["state"], "starting_service")
        self.assertEqual(starting["phase"], "complete")
        self.assertEqual(starting["overall_percent"], 100.0)

        store.open_service(True)
        ready = self.payload()
        self.assertEqual(ready["state"], "ready")
        self.assertEqual(ready["phase"], "complete")
        self.assertEqual(ready["overall_percent"], 100.0)
        self.assertFalse(self.marker_path.exists())
        store.open_service(True)

    def test_ready_reentry_fails_closed_if_marker_returns(self):
        store = self.initialized_store()
        store.apply_worker_event({"phase": "complete"})
        store.open_service(True)
        self.marker_path.write_bytes(b"maintenance\n")
        self.marker_path.chmod(0o644)

        with self.assertRaisesMessage(
            migration_status.StatusError,
            "runtime_gate_open_failed",
        ):
            store.open_service(True)

        self.assertEqual(self.marker_path.read_bytes(), b"maintenance\n")
        payload = self.payload()
        self.assertEqual(payload["state"], "failed")
        self.assertEqual(payload["error_code"], "runtime_gate_open_failed")

    def test_open_service_requires_readiness_and_durable_ready_status(self):
        store = self.initialized_store()
        store.apply_worker_event({"phase": "complete"})
        with self.assertRaisesMessage(
            migration_status.StatusError,
            "readiness_not_confirmed",
        ):
            store.open_service(False)
        self.assertTrue(self.marker_path.exists())

        original_write = store._atomic_write
        with mock.patch.object(store, "_atomic_write", side_effect=OSError("full")):
            with self.assertRaisesMessage(
                migration_status.StatusError,
                "migration_status_write_failed",
            ):
                store.open_service(True)
        self.assertTrue(self.marker_path.exists())
        self.assertTrue(store._dirty)

        with mock.patch.object(store, "_atomic_write", wraps=original_write):
            store.open_service(True)
        self.assertFalse(self.marker_path.exists())

    def test_open_service_rejects_truthy_non_boolean_readiness(self):
        store = self.initialized_store()
        store.apply_worker_event({"phase": "complete"})
        for readiness in (1, "ready", object()):
            with self.subTest(readiness=readiness):
                with self.assertRaisesMessage(
                    migration_status.StatusError,
                    "readiness_not_confirmed",
                ):
                    store.open_service(readiness)
                self.assertTrue(self.marker_path.exists())

    def test_marker_unlink_failure_restores_gate_and_publishes_failed(self):
        store = self.initialized_store()
        store.apply_worker_event({"phase": "complete"})
        with mock.patch.object(
            migration_status.os,
            "unlink",
            side_effect=OSError("denied"),
        ):
            with self.assertRaisesMessage(
                migration_status.StatusError,
                "runtime_gate_open_failed",
            ):
                store.open_service(True)
        self.assertEqual(self.marker_path.read_bytes(), b"maintenance\n")
        payload = self.payload()
        self.assertEqual(payload["state"], "failed")
        self.assertEqual(payload["error_code"], "runtime_gate_open_failed")

    def test_directory_sync_failure_restores_gate_before_failed_status(self):
        store = self.initialized_store()
        store.apply_worker_event({"phase": "complete"})
        original_sync = store._fsync_runtime_directory
        count = [0]

        def fail_removal_sync():
            count[0] += 1
            if count[0] == 2:
                raise OSError("io")
            return original_sync()

        with mock.patch.object(
            store,
            "_fsync_runtime_directory",
            side_effect=fail_removal_sync,
        ):
            with self.assertRaisesMessage(
                migration_status.StatusError,
                "runtime_gate_open_failed",
            ):
                store.open_service(True)
        self.assertTrue(self.marker_path.exists())
        self.assertEqual(self.payload()["state"], "failed")

    def test_marker_restore_failure_is_hard_fail_closed(self):
        store = self.initialized_store()
        store.apply_worker_event({"phase": "complete"})
        original_write = store._atomic_write
        original_sync = store._fsync_runtime_directory
        sync_count = [0]

        def fail_removal_sync():
            sync_count[0] += 1
            if sync_count[0] == 2:
                raise OSError("io")
            return original_sync()

        def fail_marker_restore(path, payload):
            if Path(path) == self.marker_path:
                raise OSError("readonly")
            return original_write(path, payload)

        with mock.patch.object(
            store,
            "_fsync_runtime_directory",
            side_effect=fail_removal_sync,
        ):
            with mock.patch.object(
                store,
                "_atomic_write",
                side_effect=fail_marker_restore,
            ):
                with self.assertRaisesMessage(
                    migration_status.StatusError,
                    "runtime_gate_fail_closed_failed",
                ):
                    store.open_service(True)
        self.assertFalse(self.marker_path.exists())

    def test_remove_gate_cannot_be_called_without_ready_latch(self):
        store = self.initialized_store()
        with self.assertRaisesMessage(
            migration_status.StatusError,
            "readiness_not_confirmed",
        ):
            store.remove_gate()
        self.assertTrue(self.marker_path.exists())

    def test_worker_terminal_frames_are_strict_and_final(self):
        store = self.initialized_store()
        store.apply_worker_event({
            "phase": "error",
            "error_code": "legacy_startup_failed",
        })
        payload = self.payload()
        self.assertEqual(payload["state"], "failed")
        self.assertEqual(payload["error_class"], "fatal")
        before = self.status_path.read_bytes()
        store.apply_worker_event({
            "phase": "error",
            "error_code": "legacy_startup_failed",
        })
        self.assertEqual(self.status_path.read_bytes(), before)
        with self.assertRaisesMessage(
            migration_status.StatusError,
            "worker_protocol_invalid",
        ):
            store.apply_worker_event({"phase": "snapshot"})

        complete_store = self.initialized_store()
        complete_store.apply_worker_event({"phase": "complete"})
        with self.assertRaisesMessage(
            migration_status.StatusError,
            "worker_protocol_invalid",
        ):
            complete_store.apply_worker_event({"phase": "snapshot"})

    def test_complete_requires_finalizing_or_preparing_shortcut(self):
        store = self.start_run()
        with self.assertRaisesMessage(
            migration_status.StatusError,
            "worker_protocol_invalid",
        ):
            store.apply_worker_event({"phase": "complete"})
        store.apply_worker_event({"phase": "finalizing"})
        store.apply_worker_event({"phase": "complete"})

    def test_failed_uses_literal_error_class_and_preserves_progress(self):
        store = self.start_run()
        store.apply_worker_event({
            "phase": "copying",
            "images_done": 1,
            "images_total": 100,
            "files_done": 2,
            "files_total": 200,
        })
        before = self.payload()
        store.failed("migration_status_write_failed")
        after = self.payload()
        self.assertEqual(after["state"], "failed")
        self.assertEqual(after["error_class"], "retryable")
        self.assertEqual(after["error_code"], "migration_status_write_failed")
        for key in (
            "phase",
            "phase_label",
            "images_done",
            "files_done",
            "phase_percent",
            "overall_percent",
        ):
            self.assertEqual(after[key], before[key])
        with self.assertRaisesMessage(
            migration_status.StatusError,
            "worker_protocol_invalid",
        ):
            store.failed("unknown-secret-error")

    def test_error_code_map_is_exact_and_literal(self):
        expected = {
            "archive_failed": "fatal",
            "archive_manifest_mismatch": "fatal",
            "archive_state_conflict": "fatal",
            "atomic_archive_unsupported": "operator_action_required",
            "bootstrap_environment_invalid": "operator_action_required",
            "bootstrap_failed": "fatal",
            "bootstrap_persistent_settings_invalid": "operator_action_required",
            "bootstrap_project_settings_invalid": "operator_action_required",
            "corrupt_sqlite_snapshot": "fatal",
            "gunicorn_start_failed": "retryable",
            "legacy_database_invalid": "operator_action_required",
            "legacy_database_schema_invalid": "operator_action_required",
            "legacy_evidence_invalid": "operator_action_required",
            "legacy_media_files_invalid": "operator_action_required",
            "legacy_media_root_invalid": "operator_action_required",
            "legacy_media_rows_invalid": "operator_action_required",
            "legacy_media_still_referenced": "fatal",
            "legacy_migration_graph_invalid": "operator_action_required",
            "legacy_migration_flag_required": "operator_action_required",
            "legacy_migration_space_insufficient": "operator_action_required",
            "legacy_progress_event_invalid": "fatal",
            "legacy_startup_failed": "fatal",
            "linear_batch_file_sync_failed": "operator_action_required",
            "linear_checkpoint_ahead": "fatal",
            "linear_committed_repair_failed": "operator_action_required",
            "linear_committed_source_changed": "fatal",
            "linear_journal_database_conflict": "fatal",
            "linear_journal_invalid": "fatal",
            "linear_journal_source_changed": "fatal",
            "linear_journal_tail_repair_failed": "operator_action_required",
            "linear_work_totals_changed": "fatal",
            "media_lock_not_usable": "operator_action_required",
            "media_path_conflict": "operator_action_required",
            "media_root_not_writable": "operator_action_required",
            "media_storage_configuration_invalid": "operator_action_required",
            "migration_state_conflict": "fatal",
            "migration_state_create_failed": "operator_action_required",
            "migration_state_identity_changed": "fatal",
            "migration_state_invalid": "fatal",
            "migration_state_invalid_transition": "fatal",
            "migration_state_missing_or_invalid": "fatal",
            "migration_state_phase_mismatch": "fatal",
            "migration_state_plan_mismatch": "fatal",
            "migration_state_root_invalid": "operator_action_required",
            "migration_state_write_failed": "operator_action_required",
            "migration_status_write_failed": "retryable",
            "nginx_start_failed": "retryable",
            "paths_not_complete": "fatal",
            "runtime_gate_fail_closed_failed": "fatal",
            "runtime_gate_open_failed": "operator_action_required",
            "runtime_gate_quiesce_failed": "retryable",
            "runtime_supervisor_failed": "fatal",
            "sqlite_snapshot_conflict": "fatal",
            "sqlite_snapshot_failed": "operator_action_required",
            "sqlite_snapshot_phase_invalid": "fatal",
            "sqlite_snapshot_state_invalid": "fatal",
            "sqlite_source_identity_changed": "fatal",
            "sqlite_source_not_configured_database": "operator_action_required",
            "sqlite_source_outside_data_root": "operator_action_required",
            "startup_argument_invalid": "operator_action_required",
            "startup_lock_busy": "operator_action_required",
            "startup_lock_failed": "operator_action_required",
            "startup_lock_unsupported": "operator_action_required",
            "unsafe_auto_v2_manifest": "fatal",
            "unsafe_media_lock_state": "fatal",
            "unsafe_migration_summary": "fatal",
            "unsafe_sqlite_snapshot": "fatal",
            "unsafe_sqlite_source": "fatal",
            "unsafe_sqlite_source_hardlink": "fatal",
            "unsafe_sqlite_source_non_regular": "fatal",
            "unsafe_sqlite_source_symlink": "fatal",
            "unsafe_storage_ownership": "operator_action_required",
            "unsupported_legacy_database_backend": "operator_action_required",
            "worker_protocol_invalid": "fatal",
        }
        self.assertEqual(migration_status.ERROR_CODE_CLASSES, expected)
        self.assertEqual(
            migration_status.ERROR_CLASSES,
            frozenset(("retryable", "operator_action_required", "fatal")),
        )
        self.assertTrue(all(
            error_class in migration_status.ERROR_CLASSES
            for error_class in migration_status.ERROR_CODE_CLASSES.values()
        ))
