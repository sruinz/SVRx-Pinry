import json
import os
import signal
import socket
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from docker.scripts import migration_status, supervisor
from django_images.services import startup_lock


class RecoveryIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = os.path.realpath(temporary.name)
        self.owner = mock.patch.object(
            migration_status, "_runtime_owner_ids",
            return_value=(os.geteuid(), os.getegid()),
        )
        self.owner.start()
        self.addCleanup(self.owner.stop)
        self.store = migration_status.MigrationStatusStore(
            os.path.join(self.root, "runtime"),
            lambda: datetime.now(timezone.utc), os.geteuid(), os.getegid(),
        )
        self.runtime = supervisor.RuntimeSupervisor(
            [], self.root, os.geteuid(), os.getegid(), self.store,
        )

    def prepare_failure(self, code="gunicorn_start_failed"):
        self.store.prepare_runtime_gate()
        self.store.initialize()
        self.store.failed(code)
        self.runtime.startup_lock = mock.Mock()
        self.runtime._failed_code = code
        self.runtime._failure_published = True
        self.runtime._worker_terminal = "complete"
        self.runtime._worker_eof = True
        self.runtime._worker_succeeded = True
        self.runtime._worker_exit_timed_out = True
        self.runtime.worker = supervisor._ChildRecord(
            "migration", mock.Mock(pid=1234), {
                "pid": 1234, "pgid": 1234, "session": 1234,
                "starttime": "1", "state": "S",
            },
        )
        self.runtime.worker.master_reaped = True
        process = mock.Mock(pid=2345)
        process.poll.return_value = None
        self.runtime.nginx = supervisor._ChildRecord("nginx", process, None)

    def test_eligibility_requires_attempt_evidence_and_safe_durable_state(self):
        self.prepare_failure()
        self.assertTrue(self.runtime._recovery_eligible("gunicorn_start_failed"))
        for name, value in (("_worker_terminal", None), ("_worker_eof", False),
                            ("_worker_succeeded", False),
                            ("_shutdown_signal", signal.SIGTERM),
                            ("_failure_published", False)):
            with self.subTest(name=name):
                old = getattr(self.runtime, name)
                setattr(self.runtime, name, value)
                self.assertFalse(self.runtime._recovery_eligible("gunicorn_start_failed"))
                setattr(self.runtime, name, old)
        self.runtime.children["migration"] = object()
        self.assertFalse(self.runtime._recovery_eligible("gunicorn_start_failed"))
        self.runtime.children.clear()
        os.unlink(self.store.marker_path)
        self.assertFalse(self.runtime._recovery_eligible("gunicorn_start_failed"))

    def test_timeout_string_without_timeout_evidence_is_rejected(self):
        self.prepare_failure("worker_exit_timeout")
        self.assertTrue(self.runtime._recovery_eligible("worker_exit_timeout"))
        self.runtime._worker_exit_timed_out = False
        self.assertFalse(self.runtime._recovery_eligible("worker_exit_timeout"))

    def test_unverified_or_unreaped_child_identity_is_rejected(self):
        self.prepare_failure()
        self.runtime.worker.starttime = None
        self.assertFalse(self.runtime._recovery_eligible("gunicorn_start_failed"))
        self.runtime.worker.starttime = "1"
        self.runtime.worker.master_reaped = False
        self.assertFalse(self.runtime._recovery_eligible("gunicorn_start_failed"))

    def test_nginx_exit_blocks_acceptance_and_new_worker(self):
        self.prepare_failure()
        self.runtime.nginx.process.poll.return_value = 1
        self.assertFalse(self.runtime._recovery_eligible("gunicorn_start_failed"))
        spawned = []
        self.runtime._spawn_worker = lambda descriptor: spawned.append(descriptor)
        self.assertEqual(self.runtime._run_startup_attempt(), 1)
        self.assertEqual(spawned, [])

    def test_failed_status_write_stays_ineligible_after_heartbeat_recovers(self):
        self.prepare_failure()
        self.store.clock = lambda: datetime.now(timezone.utc) + timedelta(seconds=5)
        with mock.patch.object(self.store, "_atomic_write", side_effect=OSError):
            self.assertFalse(self.runtime._project_status(self.store.heartbeat))
        self.store.publish_pending()
        self.assertFalse(self.runtime._recovery_eligible("gunicorn_start_failed"))

    def test_gunicorn_identity_error_is_not_reclassified_as_recoverable(self):
        self.prepare_failure()
        observed = []
        self.runtime._hold_failed = lambda code: observed.append(code) or 1
        with mock.patch.object(self.runtime, "_spawn_gunicorn", side_effect=
                               supervisor.SupervisorError("runtime_supervisor_failed")):
            self.assertEqual(self.runtime._start_application_and_serve(), 1)
        self.assertEqual(observed, ["runtime_supervisor_failed"])

    def test_unobserved_export_identity_blocks_later_recovery(self):
        self.prepare_failure()
        process = mock.Mock(pid=5678)
        process.poll.return_value = 1
        process.wait.return_value = 1
        self.runtime.process_factory = lambda *args, **kwargs: process
        self.runtime.identity_reader = mock.Mock(side_effect=OSError)
        record = self.runtime._spawn("export", ["synthetic"])
        self.runtime._reap_record(record, timeout=0)
        self.assertNotIn("export", self.runtime.children)
        self.assertFalse(self.runtime._recovery_eligible("gunicorn_start_failed"))

    def test_acceptance_then_status_failure_or_signal_never_spawns_next_worker(self):
        for interruption in ("status", "signal", "lock"):
            with self.subTest(interruption=interruption):
                runtime = supervisor.RuntimeSupervisor(
                    [], self.root, os.geteuid(), os.getegid(), self.store,
                )
                process = mock.Mock(pid=4444)
                process.poll.return_value = None
                record = supervisor._ChildRecord("nginx", process, None)
                runtime._spawn_nginx = lambda: record
                runtime._child_survived_start = lambda child: True
                runtime._cleanup = lambda: None
                lock = mock.Mock()
                runtime._acquire_lock = lambda: lock
                attempts = []

                def attempt():
                    attempts.append(1)
                    self.store.failed("gunicorn_start_failed")
                    if interruption == "signal":
                        runtime.handle_signal(signal.SIGTERM, None)
                    elif interruption == "status":
                        self.store._atomic_write = mock.Mock(side_effect=OSError)
                    else:
                        lock.verify_held.side_effect = startup_lock.StartupLockError("startup_lock_failed")
                    return supervisor._RETRY_STARTUP

                runtime._run_startup_attempt = attempt
                original_write = self.store._atomic_write
                try:
                    result = runtime.run()
                finally:
                    self.store._atomic_write = original_write
                self.assertEqual(attempts, [1])
                self.assertEqual(result, 0 if interruption == "signal" else 1)
                self.assertTrue(os.path.exists(self.store.marker_path))

    def test_failed_loop_consumes_budget_and_closes_socket_before_retry(self):
        self.prepare_failure()
        runtime = self.runtime
        process = mock.Mock(pid=4444)
        process.poll.return_value = None
        runtime.nginx = supervisor._ChildRecord("nginx", process, None)
        events = []
        now = [0.0]
        runtime.clock = lambda: now[0]
        runtime.recovery_policy = supervisor.RecoveryPolicy(clock=runtime.clock)
        runtime.sleeper = lambda seconds: runtime.handle_signal(signal.SIGTERM, None)

        class AcceptServer:
            def __init__(self, directory, uid, gid, policy, eligibility_check, clock):
                self.policy = policy
                self.eligible = eligibility_check

            def open(self):
                events.append("open")

            def poll_once(self):
                state = self.policy.snapshot(self.eligible())
                status, _ = self.policy.accept(state["token"], state["generation"], self.eligible())
                events.append(status)
                return status == 202

            def close(self):
                events.append("close")

        runtime.recovery_server_factory = AcceptServer
        for accepted in range(3):
            result = runtime._hold_failed("gunicorn_start_failed")
            self.assertIs(result, supervisor._RETRY_STARTUP)
            self.assertEqual(runtime.recovery_policy.accepted_count, accepted + 1)
            self.assertEqual(events[-3:], ["open", 202, "close"])
            now[0] += 30
        self.assertEqual(runtime._hold_failed("gunicorn_start_failed"), 0)
        self.assertEqual(runtime.recovery_policy.accepted_count, 3)
        self.assertEqual(events[-3:], ["open", 409, "close"])

    def test_failed_loop_polls_active_control_before_nginx_response_deadline(self):
        for opened in (True, False):
            with self.subTest(socket_opened=opened):
                self.prepare_failure()
                self.runtime._shutdown_signal = None
                server = mock.Mock()
                server.poll_once.return_value = False
                if not opened:
                    server.open.side_effect = OSError("시험 소켓 생성 거부")
                self.runtime.recovery_server_factory = mock.Mock(return_value=server)
                sleeps = []

                def sleep(seconds):
                    sleeps.append(seconds)
                    self.runtime.handle_signal(signal.SIGTERM, None)

                self.runtime.sleeper = sleep
                self.assertEqual(self.runtime._hold_failed("gunicorn_start_failed"), 0)
                self.assertEqual(sleeps, [0.1 if opened else 1.0])
                self.assertEqual(server.poll_once.call_count, 1 if opened else 0)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux 잠금·프로세스군 시험")
    def test_real_worker_timeout_http_accept_then_success_reuses_lock_and_gate(self):
        runtime = self.runtime
        worker_fds = []
        processes = []
        original_spawn = runtime._spawn

        def spawn(role, command, pass_fds=(), cwd=None):
            del command, cwd
            if role == "migration":
                worker_fds.append(pass_fds)
                self.assertTrue(os.path.exists(self.store.marker_path))
                runtime.startup_lock.verify_held()
                delay = 2 if len(worker_fds) == 1 else 0
                code = (
                    "import os,time; os.write(PROGRESS,"
                    "b'{\"phase\":\"complete\"}\\n');"
                    "os.close(PROGRESS);time.sleep(DELAY)"
                ).replace("PROGRESS", str(pass_fds[0])).replace("DELAY", str(delay))
            else:
                code = "import time; time.sleep(30)"
            record = original_spawn(role, [sys.executable, "-c", code], pass_fds)
            processes.append(record.process)
            return record

        runtime._spawn = spawn
        runtime.readiness_probe = lambda: True
        runtime.quiescence_probe = lambda record, deadline: True

        def served():
            self.assertFalse(os.path.exists(self.store.marker_path))
            return 0

        runtime._serve_application = served
        runtime._maintain_export_worker = lambda: None
        results = []
        errors = []

        def run():
            try:
                results.append(runtime.run())
            except BaseException as error:
                errors.append(error)

        def request(method, token=None, generation=None):
            path = "/migration/recovery" if method == "GET" else "/migration/restart"
            headers = "{} {} HTTP/1.1\r\nHost: localhost\r\n".format(method, path)
            if token is not None:
                headers += "X-SVRX-Recovery-Token: {}\r\nX-SVRX-Recovery-Generation: {}\r\n".format(token, generation)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(4)
                client.connect(os.path.join(self.store.status_directory, "startup-recovery.sock"))
                client.sendall((headers + "\r\n").encode("ascii"))
                payload = b""
                while True:
                    chunk = client.recv(4096)
                    if not chunk:
                        break
                    payload += chunk
                head, body = payload.split(b"\r\n\r\n", 1)
                return int(head.split()[1]), json.loads(body)

        with mock.patch.object(supervisor, "_WORKER_EXIT_SECONDS", 0.1), mock.patch.object(
            supervisor, "_POLL_SECONDS", 0.02
        ):
            thread = threading.Thread(target=run)
            thread.start()
            try:
                deadline = time.monotonic() + 8
                path = os.path.join(self.store.status_directory, "startup-recovery.sock")
                while not os.path.exists(path) and thread.is_alive() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(os.path.exists(path), errors)
                status, state = request("GET")
                self.assertEqual(status, 200)
                self.assertTrue(state["available"])
                status, _ = request("POST", state["token"], state["generation"])
                self.assertEqual(status, 202)
                thread.join(8)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(results, [0])
                self.assertEqual(len(worker_fds), 2)
                self.assertEqual(worker_fds[0][1], worker_fds[1][1])
                self.assertEqual(runtime.recovery_policy.accepted_count, 1)
                self.assertEqual(runtime.gunicorn.returncode, -signal.SIGTERM)
                self.assertFalse(os.path.exists(path))
            finally:
                runtime.handle_signal(signal.SIGTERM, None)
                thread.join(5)
                for process in processes:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=5)
