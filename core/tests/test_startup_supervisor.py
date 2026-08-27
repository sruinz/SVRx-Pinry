import errno
import importlib.util
import importlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))


def _load_script(name):
    path = REPOSITORY_ROOT / "docker/scripts" / (name + ".py")
    spec = importlib.util.spec_from_file_location(
        "test_{}".format(name), str(path)
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ProgressPipeReporterTests(unittest.TestCase):
    def setUp(self):
        self.worker = _load_script("migration_worker")
        self.reader, self.writer = os.pipe()
        self.addCleanup(self._close_descriptors)

    def _close_descriptors(self):
        for descriptor in (self.reader, self.writer):
            try:
                os.close(descriptor)
            except OSError:
                pass

    def test_send_writes_one_canonical_utf8_json_line(self):
        reporter = self.worker.ProgressPipeReporter(self.writer)

        reporter.send({"phase": "preparing"})
        os.close(self.writer)
        self.writer = -1

        self.assertEqual(
            os.read(self.reader, 4096),
            b'{"phase":"preparing"}\n',
        )

    def test_send_rejects_frame_larger_than_4096_bytes(self):
        reporter = self.worker.ProgressPipeReporter(self.writer)

        with self.assertRaisesRegex(
            self.worker.WorkerProtocolError,
            "worker_protocol_invalid",
        ):
            reporter.send({"phase": "preparing", "padding": "x" * 4096})

    def test_send_error_emits_only_allowlisted_error_frame(self):
        reporter = self.worker.ProgressPipeReporter(self.writer)

        reporter.send_error("legacy_startup_failed")
        os.close(self.writer)
        self.writer = -1

        self.assertEqual(
            json.loads(os.read(self.reader, 4096).decode("utf-8")),
            {"phase": "error", "error_code": "legacy_startup_failed"},
        )
        with self.assertRaisesRegex(
            self.worker.WorkerProtocolError,
            "worker_protocol_invalid",
        ):
            reporter.send_error("sentinel-private-error")

    def test_send_rejects_non_finite_json_numbers(self):
        reporter = self.worker.ProgressPipeReporter(self.writer)

        with self.assertRaisesRegex(
            self.worker.WorkerProtocolError,
            "worker_protocol_invalid",
        ):
            reporter.send({"phase": "preparing", "value": float("nan")})

    def test_stdout_log_failure_does_not_change_pipe_protocol(self):
        reporter = self.worker.ProgressPipeReporter(self.writer)

        with mock.patch("builtins.print", side_effect=BrokenPipeError):
            reporter.send({"phase": "complete"})
        os.close(self.writer)
        self.writer = -1

        self.assertEqual(os.read(self.reader, 4096), b'{"phase":"complete"}\n')


class ErrorCodeAuthorityTests(unittest.TestCase):
    def test_worker_and_supervisor_share_status_error_map_object(self):
        migration_status = importlib.import_module(
            "docker.scripts.migration_status"
        )
        worker = importlib.import_module("docker.scripts.migration_worker")
        supervisor = importlib.import_module("docker.scripts.supervisor")

        self.assertIs(worker.ERROR_CODE_CLASSES,
                      migration_status.ERROR_CODE_CLASSES)
        self.assertIs(supervisor.ERROR_CODE_CLASSES,
                      migration_status.ERROR_CODE_CLASSES)

    def test_worker_never_names_status_or_marker_paths(self):
        source = (
            REPOSITORY_ROOT / "docker/scripts/migration_worker.py"
        ).read_text("utf-8")

        self.assertNotIn("migration-status.json", source)
        self.assertNotIn("MAINTENANCE_MARKER_PATH", source)


class ProgressFrameDecoderTests(unittest.TestCase):
    def setUp(self):
        self.supervisor = _load_script("supervisor")
        self.decoder = self.supervisor.ProgressFrameDecoder()

    def test_split_frame_is_returned_only_after_newline(self):
        self.assertEqual(self.decoder.feed(b'{"phase":'), [])

        self.assertEqual(
            self.decoder.feed(b'"preparing"}\n'),
            [{"phase": "preparing"}],
        )
        self.assertEqual(
            self.decoder.feed(b'{"phase":"complete"}\n'),
            [{"phase": "complete"}],
        )
        self.decoder.finish()

    def test_one_read_can_contain_two_frames(self):
        self.assertEqual(
            self.decoder.feed(
                b'{"phase":"preparing"}\n'
                b'{"phase":"complete"}\n'
            ),
            [{"phase": "preparing"}, {"phase": "complete"}],
        )
        self.decoder.finish()

    def test_split_utf8_character_is_buffered(self):
        decoder = self.supervisor.ProgressFrameDecoder(
            event_validator=lambda event: None,
        )
        payload = '{"phase":"준비"}\n'.encode("utf-8")
        split_at = payload.index("준".encode("utf-8")) + 1

        self.assertEqual(decoder.feed(payload[:split_at]), [])
        self.assertEqual(
            decoder.feed(payload[split_at:]),
            [{"phase": "준비"}],
        )
        with self.assertRaisesRegex(
            self.supervisor.SupervisorError,
            "worker_protocol_invalid",
        ):
            decoder.finish()

    def test_noncanonical_json_is_rejected(self):
        with self.assertRaisesRegex(
            self.supervisor.SupervisorError,
            "worker_protocol_invalid",
        ):
            self.decoder.feed(b'{"phase": "preparing"}\n')

    def test_non_finite_json_number_is_rejected(self):
        decoder = self.supervisor.ProgressFrameDecoder(
            event_validator=lambda event: None,
        )
        with self.assertRaisesRegex(
            self.supervisor.SupervisorError,
            "worker_protocol_invalid",
        ):
            decoder.feed(b'{"phase":NaN}\n')

    def test_ambiguous_or_non_object_frames_are_rejected(self):
        cases = (
            b"\n",
            b"\xef\xbb\xbf{\"phase\":\"preparing\"}\n",
            b'{"phase":"preparing"}\r\n',
            b'["preparing"]\n',
            b'{"phase":"preparing","phase":"preparing"}\n',
            b'{"phase":"\\ud800"}\n',
        )
        for payload in cases:
            with self.subTest(payload=payload):
                decoder = self.supervisor.ProgressFrameDecoder()
                with self.assertRaisesRegex(
                    self.supervisor.SupervisorError,
                    "worker_protocol_invalid",
                ):
                    decoder.feed(payload)

    def test_phase_schema_keys_types_and_ranges_are_exact(self):
        cases = (
            b'{"extra":1,"phase":"preparing"}\n',
            b'{"phase":1}\n',
            b'{"files_done":2,"files_total":1,"images_done":0,'
            b'"images_total":0,"phase":"copying"}\n',
            b'{"error_code":"not-allowlisted","phase":"error"}\n',
        )
        for payload in cases:
            with self.subTest(payload=payload):
                decoder = self.supervisor.ProgressFrameDecoder()
                with self.assertRaisesRegex(
                    self.supervisor.SupervisorError,
                    "worker_protocol_invalid",
                ):
                    decoder.feed(payload)

    def test_4097_byte_line_is_rejected_before_newline(self):
        with self.assertRaisesRegex(
            self.supervisor.SupervisorError,
            "worker_protocol_invalid",
        ):
            self.decoder.feed(b"x" * 4097)

    def test_eof_with_partial_frame_is_rejected(self):
        self.decoder.feed(b'{"phase":"preparing"}')

        with self.assertRaisesRegex(
            self.supervisor.SupervisorError,
            "worker_protocol_invalid",
        ):
            self.decoder.finish()

    def test_eof_without_terminal_frame_is_rejected(self):
        self.assertEqual(
            self.decoder.feed(b'{"phase":"preparing"}\n'),
            [{"phase": "preparing"}],
        )

        with self.assertRaisesRegex(
            self.supervisor.SupervisorError,
            "worker_protocol_invalid",
        ):
            self.decoder.finish()

    def test_error_frame_must_be_last(self):
        self.assertEqual(
            self.decoder.feed(
                b'{"error_code":"legacy_startup_failed","phase":"error"}\n'
            ),
            [{"error_code": "legacy_startup_failed", "phase": "error"}],
        )

        with self.assertRaisesRegex(
            self.supervisor.SupervisorError,
            "worker_protocol_invalid",
        ):
            self.decoder.feed(b'{"phase":"complete"}\n')

    def test_complete_frame_must_be_last(self):
        self.assertEqual(
            self.decoder.feed(b'{"phase":"complete"}\n'),
            [{"phase": "complete"}],
        )

        with self.assertRaisesRegex(
            self.supervisor.SupervisorError,
            "worker_protocol_invalid",
        ):
            self.decoder.feed(b'{"phase":"preparing"}\n')


class WorkerCliTests(unittest.TestCase):
    def setUp(self):
        self.worker = _load_script("migration_worker")
        self.progress_reader, self.progress_writer = os.pipe()
        self.lock_file = tempfile.TemporaryFile()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for descriptor in (self.progress_reader, self.progress_writer):
            try:
                os.close(descriptor)
            except OSError:
                pass
        self.lock_file.close()

    def test_cli_accepts_only_open_decimal_descriptors(self):
        parsed = self.worker._parse_cli([
            "--migrate-legacy",
            "--progress-fd", str(self.progress_writer),
            "--lock-fd", str(self.lock_file.fileno()),
        ])

        self.assertEqual(parsed, (
            ["--migrate-legacy"],
            self.progress_writer,
            self.lock_file.fileno(),
        ))
        self.assertFalse(os.get_inheritable(self.progress_writer))
        self.assertFalse(os.get_inheritable(self.lock_file.fileno()))

    def test_cli_rejects_closed_or_non_decimal_descriptors(self):
        cases = (
            ["--progress-fd", "3x", "--lock-fd", "4"],
            ["--progress-fd", "999999", "--lock-fd", "4"],
            ["--lock-fd", "4", "--progress-fd", "3"],
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                with self.assertRaisesRegex(
                    self.worker.WorkerProtocolError,
                    "startup_argument_invalid",
                ):
                    self.worker._parse_cli(arguments)

    def test_worker_protocol_error_preserves_allowlisted_code(self):
        error = self.worker.WorkerProtocolError(
            "bootstrap_persistent_settings_invalid"
        )

        self.assertEqual(
            self.worker._safe_error_code(error),
            "bootstrap_persistent_settings_invalid",
        )

    def test_cli_sanitizes_descriptor_inheritance_failure(self):
        with mock.patch.object(
            self.worker.os,
            "set_inheritable",
            side_effect=OSError("denied"),
        ):
            with self.assertRaisesRegex(
                self.worker.WorkerProtocolError,
                "startup_argument_invalid",
            ):
                self.worker._parse_cli([
                    "--progress-fd", str(self.progress_writer),
                    "--lock-fd", str(self.lock_file.fileno()),
                ])


class _FakeStatusStore(object):
    def __init__(self, events, status_error, initialize_failures=0):
        self.events = events
        self.status_error = status_error
        self.initialize_failures = initialize_failures
        self.worker_events = []
        self.failed_codes = []
        self.heartbeat_count = 0
        self.publish_count = 0
        self.ready = False

    def prepare_runtime_gate(self):
        self.events.append("gate_prepared")

    def initialize(self):
        self.events.append("status_initialized")
        if self.initialize_failures:
            self.initialize_failures -= 1
            raise self.status_error("migration_status_write_failed")

    def publish_pending(self):
        self.publish_count += 1

    def apply_worker_event(self, event):
        self.worker_events.append(dict(event))
        self.events.append("worker_event:{}".format(event["phase"]))

    def heartbeat(self):
        self.heartbeat_count += 1

    def starting_service(self):
        self.events.append("starting_service")

    def failed(self, code):
        self.failed_codes.append(code)
        self.events.append("failed:{}".format(code))

    def open_service(self, readiness_confirmed):
        if readiness_confirmed is not True:
            raise AssertionError("readiness missing")
        self.ready = True
        self.events.append("service_opened")

    def create_gate(self):
        self.events.append("gate_created")


class _FakeLock(object):
    def __init__(self, events):
        self.events = events
        self.file = tempfile.TemporaryFile()
        self.closed = False

    def fileno(self):
        return self.file.fileno()

    def close(self):
        if not self.closed:
            self.events.append("lock_closed")
            self.closed = True
            self.file.close()


class _FakeProcess(object):
    def __init__(
        self,
        role,
        pid,
        running=True,
        returncode=0,
        block_wait_while_running=False,
    ):
        self.role = role
        self.pid = pid
        self.running = running
        self.returncode = returncode
        self.wait_called = False
        self.kill_called = False
        self.block_wait_while_running = block_wait_while_running

    def poll(self):
        if self.running:
            return None
        return self.returncode

    def wait(self, timeout=None):
        self.wait_called = True
        if self.running and self.block_wait_while_running:
            raise subprocess.TimeoutExpired(self.role, timeout)
        self.running = False
        return self.returncode

    def kill(self):
        self.kill_called = True
        self.running = False
        self.returncode = -signal.SIGKILL


class _FakeProcesses(object):
    def __init__(
        self,
        events,
        worker_payload,
        worker_returncode,
        worker_running=False,
        hold_worker_pipe=False,
        delayed_worker_payload=None,
        block_worker_wait=False,
    ):
        self.events = events
        self.worker_payload = worker_payload
        self.worker_returncode = worker_returncode
        self.worker_running = worker_running
        self.hold_worker_pipe = hold_worker_pipe
        self.delayed_worker_payload = delayed_worker_payload
        self.block_worker_wait = block_worker_wait
        self.processes = {}
        self.spawn_kwargs = {}
        self.signal_order = []
        self.progress_descriptor = None
        self.next_pid = 4100
        self.held_progress_descriptor = None
        self.delayed_thread = None

    def __call__(self, command, **kwargs):
        rendered = " ".join(command)
        if "migration_worker.py" in rendered:
            role = "migration"
            self.progress_descriptor = kwargs["pass_fds"][0]
            if self.worker_payload:
                os.write(self.progress_descriptor, self.worker_payload)
            if self.hold_worker_pipe:
                self.held_progress_descriptor = os.dup(
                    self.progress_descriptor
                )
            if self.delayed_worker_payload is not None:
                delayed = os.dup(self.progress_descriptor)

                def write_delayed():
                    time.sleep(0.05)
                    os.write(delayed, self.delayed_worker_payload)
                    os.close(delayed)

                self.delayed_thread = threading.Thread(target=write_delayed)
                self.delayed_thread.start()
            running = self.worker_running
            returncode = self.worker_returncode
        elif "_start_gunicorn.sh" in rendered:
            role = "gunicorn"
            running = True
            returncode = 0
        else:
            role = "nginx"
            running = True
            returncode = 0
        process = _FakeProcess(
            role,
            self.next_pid,
            running=running,
            returncode=returncode,
            block_wait_while_running=(
                role == "migration" and self.block_worker_wait
            ),
        )
        self.next_pid += 1
        self.processes[role] = process
        self.spawn_kwargs[role] = kwargs
        self.events.append("{}_started".format(role))
        return process

    def identity(self, pid):
        for process in self.processes.values():
            if process.pid == pid and process.running:
                return {
                    "pid": pid,
                    "pgid": pid,
                    "session": pid,
                    "starttime": "{}0".format(pid),
                    "state": "S",
                }
        raise OSError("process is gone")

    def group(self, pgid):
        try:
            process = next(
                item for item in self.processes.values()
                if item.pid == pgid and item.running
            )
        except StopIteration:
            return {}
        return {process.pid: self.identity(process.pid)}

    def signal(self, pgid, signum):
        process = next(
            item for item in self.processes.values() if item.pid == pgid
        )
        self.signal_order.append((process.role, signum))
        if signum in (signal.SIGTERM, signal.SIGKILL, signal.SIGINT):
            process.running = False
            process.returncode = -signum
            if (
                process.role == "migration"
                and self.held_progress_descriptor is not None
            ):
                os.close(self.held_progress_descriptor)
                self.held_progress_descriptor = None


class RuntimeSupervisorLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.supervisor_module = _load_script("supervisor")
        self.events = []
        self.status = _FakeStatusStore(
            self.events,
            self.supervisor_module.StatusError,
        )
        self.lock = _FakeLock(self.events)
        self.addCleanup(self.lock.close)

    def _supervisor(
        self,
        worker_payload,
        worker_returncode,
        status=None,
        readiness_probe=lambda: True,
        quiescence_probe=lambda record, deadline: True,
        worker_running=False,
        hold_worker_pipe=False,
        delayed_worker_payload=None,
        block_worker_wait=False,
    ):
        processes = _FakeProcesses(
            self.events,
            worker_payload,
            worker_returncode,
            worker_running=worker_running,
            hold_worker_pipe=hold_worker_pipe,
            delayed_worker_payload=delayed_worker_payload,
            block_worker_wait=block_worker_wait,
        )
        supervisor = self.supervisor_module.RuntimeSupervisor(
            ["--migrate-legacy"],
            "/data",
            33,
            33,
            status or self.status,
            process_factory=processes,
            lock_acquirer=lambda *args: self._acquire_lock(*args),
            identity_reader=processes.identity,
            group_reader=processes.group,
            group_signaler=processes.signal,
            readiness_probe=readiness_probe,
            quiescence_probe=quiescence_probe,
            sleeper=lambda seconds: time.sleep(min(seconds, 0.01)),
        )
        return supervisor, processes

    def _acquire_lock(self, *arguments):
        del arguments
        self.events.append("startup_lock_acquired")
        return self.lock

    @staticmethod
    def _wait_until(predicate):
        deadline = time.monotonic() + 2
        while not predicate() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not predicate():
            raise AssertionError("condition did not become true")

    def test_gate_nginx_status_lock_and_worker_start_in_exact_order(self):
        supervisor, processes = self._supervisor(
            b'{"error_code":"legacy_startup_failed","phase":"error"}\n',
            1,
        )
        result = []
        thread = threading.Thread(target=lambda: result.append(supervisor.run()))
        thread.start()
        self._wait_until(lambda: bool(self.status.failed_codes))

        self.assertEqual(self.events[:5], [
            "gate_prepared",
            "nginx_started",
            "status_initialized",
            "startup_lock_acquired",
            "migration_started",
        ])
        self.assertTrue(processes.spawn_kwargs["nginx"]["start_new_session"])
        self.assertTrue(
            processes.spawn_kwargs["migration"]["start_new_session"]
        )
        self.assertEqual(
            len(processes.spawn_kwargs["migration"]["pass_fds"]),
            2,
        )
        with self.assertRaises(OSError):
            os.fstat(processes.progress_descriptor)

        supervisor.handle_signal(signal.SIGTERM, None)
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [0])

    def test_worker_exit_is_drained_to_eof_before_error_is_chosen(self):
        supervisor, _processes = self._supervisor(
            b'{"error_code":"bootstrap_failed","phase":"error"}\n',
            1,
        )
        result = []
        thread = threading.Thread(target=lambda: result.append(supervisor.run()))
        thread.start()
        self._wait_until(lambda: bool(self.status.failed_codes))

        self.assertEqual(supervisor.last_worker_error, "bootstrap_failed")
        self.assertEqual(self.status.failed_codes[-1], "bootstrap_failed")
        supervisor.handle_signal(signal.SIGTERM, None)
        thread.join(2)
        self.assertEqual(result, [0])

    def test_status_write_failure_is_retried_without_stopping_worker(self):
        status = _FakeStatusStore(
            self.events,
            self.supervisor_module.StatusError,
            initialize_failures=1,
        )
        supervisor, _processes = self._supervisor(
            b'{"error_code":"legacy_startup_failed","phase":"error"}\n',
            1,
            status=status,
        )
        result = []
        thread = threading.Thread(target=lambda: result.append(supervisor.run()))
        thread.start()
        self._wait_until(lambda: bool(status.failed_codes))

        self.assertIn("migration_started", self.events)
        self.assertGreater(status.publish_count, 0)
        supervisor.handle_signal(signal.SIGTERM, None)
        thread.join(2)
        self.assertEqual(result, [0])

    def test_migration_failure_holds_nginx_until_sigterm(self):
        supervisor, processes = self._supervisor(
            b'{"error_code":"legacy_startup_failed","phase":"error"}\n',
            1,
        )
        result = []
        thread = threading.Thread(target=lambda: result.append(supervisor.run()))
        thread.start()
        self._wait_until(lambda: bool(self.status.failed_codes))

        self.assertTrue(thread.is_alive())
        self.assertTrue(processes.processes["nginx"].running)
        self.assertNotIn("gunicorn", processes.processes)
        supervisor.handle_signal(signal.SIGTERM, None)
        thread.join(2)
        self.assertEqual(
            [role for role, signum in processes.signal_order
             if signum == signal.SIGTERM],
            ["nginx"],
        )
        self.assertEqual(result, [0])

    def test_ready_shutdown_signals_gunicorn_before_nginx(self):
        supervisor, processes = self._supervisor(
            b'{"phase":"preparing"}\n{"phase":"complete"}\n',
            0,
        )
        result = []
        thread = threading.Thread(target=lambda: result.append(supervisor.run()))
        thread.start()
        self._wait_until(lambda: self.status.ready)

        supervisor.handle_signal(signal.SIGTERM, None)
        thread.join(2)

        self.assertEqual(
            [role for role, signum in processes.signal_order
             if signum == signal.SIGTERM],
            ["gunicorn", "nginx"],
        )
        self.assertTrue(processes.processes["migration"].wait_called)
        self.assertEqual(result, [0])

    def test_worker_exit_waits_for_delayed_pipe_eof(self):
        supervisor, processes = self._supervisor(
            b"",
            1,
            delayed_worker_payload=(
                b'{"error_code":"bootstrap_failed","phase":"error"}\n'
            ),
        )
        result = []
        thread = threading.Thread(target=lambda: result.append(supervisor.run()))
        thread.start()
        self._wait_until(lambda: bool(self.status.failed_codes))

        self.assertEqual(supervisor.last_worker_error, "bootstrap_failed")
        self.assertFalse(processes.delayed_thread.is_alive())
        supervisor.handle_signal(signal.SIGTERM, None)
        thread.join(2)
        self.assertEqual(result, [0])

    def test_worker_eof_never_blocks_on_a_still_live_process(self):
        supervisor, processes = self._supervisor(
            b'{"phase":"complete"}\n',
            0,
            worker_running=True,
            block_worker_wait=True,
        )
        result = []
        thread = threading.Thread(target=lambda: result.append(supervisor.run()))
        thread.start()
        self._wait_until(lambda: bool(self.status.failed_codes))

        self.assertEqual(self.status.failed_codes[-1], "worker_protocol_invalid")
        self.assertIn(("migration", signal.SIGTERM), processes.signal_order)
        self.assertTrue(processes.processes["migration"].wait_called)
        supervisor.handle_signal(signal.SIGTERM, None)
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [0])

    def test_protocol_error_terminates_and_reaps_live_worker_group(self):
        supervisor, processes = self._supervisor(
            b'{"unknown":true}\n',
            1,
            worker_running=True,
        )
        result = []
        thread = threading.Thread(target=lambda: result.append(supervisor.run()))
        thread.start()
        self._wait_until(lambda: bool(self.status.failed_codes))

        self.assertIn(("migration", signal.SIGTERM), processes.signal_order)
        self.assertTrue(processes.processes["migration"].wait_called)
        self.assertEqual(self.status.failed_codes[-1], "worker_protocol_invalid")
        supervisor.handle_signal(signal.SIGTERM, None)
        thread.join(2)
        self.assertEqual(result, [0])

    def test_sigterm_while_migrating_signals_worker_then_nginx(self):
        supervisor, processes = self._supervisor(
            b"",
            1,
            worker_running=True,
            hold_worker_pipe=True,
        )
        result = []
        thread = threading.Thread(target=lambda: result.append(supervisor.run()))
        thread.start()
        self._wait_until(lambda: "migration" in processes.processes)

        supervisor.handle_signal(signal.SIGTERM, None)
        thread.join(2)

        self.assertEqual(
            processes.signal_order[:2],
            [("migration", signal.SIGTERM), ("nginx", signal.SIGTERM)],
        )
        self.assertTrue(processes.processes["migration"].wait_called)
        self.assertEqual(result, [0])

    def test_failed_hold_exits_nonzero_when_nginx_dies(self):
        supervisor, processes = self._supervisor(
            b'{"error_code":"legacy_startup_failed","phase":"error"}\n',
            1,
        )
        result = []
        thread = threading.Thread(target=lambda: result.append(supervisor.run()))
        thread.start()
        self._wait_until(lambda: bool(self.status.failed_codes))

        processes.processes["nginx"].running = False
        processes.processes["nginx"].returncode = 1
        thread.join(2)

        self.assertEqual(result, [1])
        self.assertGreater(self.status.publish_count, 0)
        self.assertGreater(self.status.heartbeat_count, 0)

    def test_gunicorn_exit_recreates_gate_then_holds_failed(self):
        supervisor, processes = self._supervisor(
            b'{"phase":"preparing"}\n{"phase":"complete"}\n',
            0,
        )
        result = []
        thread = threading.Thread(target=lambda: result.append(supervisor.run()))
        thread.start()
        self._wait_until(lambda: self.status.ready)

        processes.processes["gunicorn"].running = False
        processes.processes["gunicorn"].returncode = 1
        self._wait_until(
            lambda: "failed:gunicorn_start_failed" in self.events
        )

        self.assertLess(
            self.events.index("gate_created"),
            self.events.index("failed:gunicorn_start_failed"),
        )
        self.assertTrue(processes.processes["nginx"].running)
        supervisor.handle_signal(signal.SIGTERM, None)
        thread.join(2)
        self.assertEqual(result, [0])

    def test_gunicorn_exit_gate_recreation_failure_stops_nginx(self):
        class GateCreateFailure(_FakeStatusStore):
            def create_gate(self):
                raise self.status_error("runtime_gate_fail_closed_failed")

        status = GateCreateFailure(
            self.events,
            self.supervisor_module.StatusError,
        )
        supervisor, processes = self._supervisor(
            b'{"phase":"preparing"}\n{"phase":"complete"}\n',
            0,
            status=status,
        )
        result = []
        thread = threading.Thread(target=lambda: result.append(supervisor.run()))
        thread.start()
        self._wait_until(lambda: status.ready)

        processes.processes["gunicorn"].running = False
        processes.processes["gunicorn"].returncode = 1
        thread.join(2)

        self.assertFalse(thread.is_alive())
        self.assertTrue(processes.processes["nginx"].wait_called)
        self.assertEqual(result, [1])

    def test_quiescence_timeout_keeps_gate_and_resumes_static_nginx(self):
        supervisor, processes = self._supervisor(
            b'{"phase":"preparing"}\n{"phase":"complete"}\n',
            0,
            quiescence_probe=lambda record, deadline: False,
        )
        result = []
        thread = threading.Thread(target=lambda: result.append(supervisor.run()))
        thread.start()
        self._wait_until(
            lambda: "failed:runtime_gate_quiesce_failed" in self.events
        )

        nginx_signals = [
            signum for role, signum in processes.signal_order
            if role == "nginx"
        ]
        self.assertEqual(nginx_signals[:2], [signal.SIGSTOP, signal.SIGCONT])
        self.assertFalse(self.status.ready)
        supervisor.handle_signal(signal.SIGTERM, None)
        thread.join(2)
        self.assertEqual(result, [0])

    def test_quiescence_master_failure_never_resumes_nginx(self):
        def hard_failure(record, deadline):
            del record, deadline
            raise self.supervisor_module.SupervisorError(
                "runtime_supervisor_failed"
            )

        supervisor, processes = self._supervisor(
            b'{"phase":"preparing"}\n{"phase":"complete"}\n',
            0,
            quiescence_probe=hard_failure,
        )

        self.assertEqual(supervisor.run(), 1)
        nginx_signals = [
            signum for role, signum in processes.signal_order
            if role == "nginx"
        ]
        self.assertEqual(nginx_signals[0], signal.SIGSTOP)
        self.assertNotIn(signal.SIGCONT, nginx_signals)
        self.assertTrue(processes.processes["nginx"].wait_called)
        self.assertTrue(processes.processes["gunicorn"].wait_called)
        self.assertIn("failed:runtime_supervisor_failed", self.events)

    def test_shutdown_during_quiescence_never_opens_service(self):
        holder = {}

        def request_shutdown(record, deadline):
            del record, deadline
            holder["supervisor"].handle_signal(signal.SIGTERM, None)
            return True

        supervisor, _processes = self._supervisor(
            b'{"phase":"preparing"}\n{"phase":"complete"}\n',
            0,
            quiescence_probe=request_shutdown,
        )
        holder["supervisor"] = supervisor

        self.assertEqual(supervisor.run(), 0)
        self.assertNotIn("service_opened", self.events)
        self.assertFalse(self.status.ready)

    def test_gunicorn_death_during_quiescence_keeps_static_gate(self):
        holder = {}

        def stop_gunicorn(record, deadline):
            del record, deadline
            process = holder["processes"].processes["gunicorn"]
            process.running = False
            process.returncode = 1
            return True

        supervisor, processes = self._supervisor(
            b'{"phase":"preparing"}\n{"phase":"complete"}\n',
            0,
            quiescence_probe=stop_gunicorn,
        )
        holder["processes"] = processes
        result = []
        thread = threading.Thread(target=lambda: result.append(supervisor.run()))
        thread.start()
        self._wait_until(
            lambda: "failed:gunicorn_start_failed" in self.events
        )
        try:
            self.assertNotIn("service_opened", self.events)
            self.assertIn("gate_created", self.events)
        finally:
            supervisor.handle_signal(signal.SIGTERM, None)
            thread.join(2)
        self.assertEqual(result, [0])

    def test_termination_kills_verified_group_after_master_exits(self):
        process = _FakeProcess("gunicorn", 5100, running=True)
        identity = {
            "pid": 5100,
            "pgid": 5100,
            "session": 5100,
            "starttime": "100",
            "state": "S",
        }
        child_alive = [True]
        signals = []
        ticks = iter((0.0, 0.0, 10.0, 20.0, 30.0, 31.0))

        def read_identity(pid):
            if pid == process.pid and process.running:
                return dict(identity)
            raise OSError(errno.ESRCH, "gone")

        def read_group(pgid):
            self.assertEqual(pgid, 5100)
            if not child_alive[0]:
                return {}
            return {5101: {
                "pid": 5101,
                "pgid": 5100,
                "session": 5100,
                "starttime": "101",
                "state": "S",
            }}

        def signal_group(pgid, signum):
            self.assertEqual(pgid, 5100)
            signals.append(signum)
            if signum == signal.SIGTERM:
                process.running = False
                process.returncode = -signal.SIGTERM
            elif signum == signal.SIGKILL:
                child_alive[0] = False

        runtime = self.supervisor_module.RuntimeSupervisor(
            ["--migrate-legacy"],
            "/data",
            33,
            33,
            self.status,
            clock=lambda: next(ticks),
            identity_reader=read_identity,
            group_reader=read_group,
            group_signaler=signal_group,
            sleeper=lambda seconds: None,
        )
        record = self.supervisor_module._ChildRecord(
            "gunicorn", process, identity
        )
        runtime.children["gunicorn"] = record

        runtime._terminate_record(record)

        self.assertEqual(signals, [signal.SIGTERM, signal.SIGKILL])
        self.assertFalse(child_alive[0])
        self.assertTrue(process.wait_called)
        self.assertNotIn("gunicorn", runtime.children)

    def test_termination_kills_direct_child_when_group_is_unknown(self):
        process = _FakeProcess("gunicorn", 5200, running=True)
        identity = {
            "pid": 5200,
            "pgid": 5200,
            "session": 5200,
            "starttime": "200",
            "state": "S",
        }
        ticks = iter((0.0, 0.0, 10.0, 20.0))
        runtime = self.supervisor_module.RuntimeSupervisor(
            ["--migrate-legacy"],
            "/data",
            33,
            33,
            self.status,
            clock=lambda: next(ticks),
            identity_reader=lambda pid: (_ for _ in ()).throw(
                OSError(errno.EIO, "unknown")
            ),
            group_reader=lambda pgid: (_ for _ in ()).throw(
                OSError(errno.EIO, "unknown")
            ),
            group_signaler=lambda pgid, signum: self.fail(
                "검증하지 않은 프로세스 그룹을 시그널하면 안 됩니다."
            ),
            sleeper=lambda seconds: None,
        )
        record = self.supervisor_module._ChildRecord(
            "gunicorn", process, identity
        )
        runtime.children["gunicorn"] = record

        runtime._terminate_record(record)

        self.assertTrue(process.kill_called)
        self.assertTrue(process.wait_called)

    def test_nginx_stop_signal_failure_kills_both_services(self):
        supervisor, processes = self._supervisor(
            b'{"phase":"complete"}\n',
            0,
        )
        supervisor.nginx = supervisor._spawn_nginx()
        supervisor.gunicorn = supervisor._spawn_gunicorn()
        original_signal = processes.signal

        def fail_stop(pgid, signum):
            if signum == signal.SIGSTOP:
                raise OSError("cannot stop nginx")
            return original_signal(pgid, signum)

        supervisor.group_signaler = fail_stop

        self.assertEqual(supervisor._transition_gate(), 1)
        self.assertTrue(processes.processes["nginx"].wait_called)
        self.assertTrue(processes.processes["gunicorn"].wait_called)
        self.assertIn("failed:runtime_supervisor_failed", self.events)

    def test_ready_projection_failure_is_retried_before_gate_open(self):
        class RetryOpenStatus(_FakeStatusStore):
            def __init__(self, *args, **kwargs):
                super(RetryOpenStatus, self).__init__(*args, **kwargs)
                self.open_attempts = 0

            def open_service(self, readiness_confirmed):
                self.open_attempts += 1
                if self.open_attempts == 1:
                    raise self.status_error("migration_status_write_failed")
                return super(RetryOpenStatus, self).open_service(
                    readiness_confirmed
                )

        status = RetryOpenStatus(
            self.events,
            self.supervisor_module.StatusError,
        )
        supervisor, processes = self._supervisor(
            b'{"phase":"preparing"}\n{"phase":"complete"}\n',
            0,
            status=status,
        )
        result = []
        thread = threading.Thread(target=lambda: result.append(supervisor.run()))
        thread.start()
        self._wait_until(lambda: status.ready)

        self.assertEqual(status.open_attempts, 2)
        self.assertEqual(
            [
                signum for role, signum in processes.signal_order
                if role == "nginx"
            ][:2],
            [signal.SIGSTOP, signal.SIGCONT],
        )
        supervisor.handle_signal(signal.SIGTERM, None)
        thread.join(2)
        self.assertEqual(result, [0])

    def test_gate_preparation_failure_starts_no_child(self):
        class GatePreparationFailure(_FakeStatusStore):
            def prepare_runtime_gate(self):
                raise self.status_error("runtime_gate_fail_closed_failed")

        status = GatePreparationFailure(
            self.events,
            self.supervisor_module.StatusError,
        )
        supervisor, processes = self._supervisor(
            b'{"phase":"complete"}\n',
            0,
            status=status,
        )

        self.assertEqual(supervisor.run(), 1)
        self.assertEqual(processes.processes, {})

    def test_nginx_spawn_failure_is_a_nonzero_supervisor_result(self):
        supervisor, _processes = self._supervisor(
            b'{"phase":"complete"}\n',
            0,
        )

        def fail_to_spawn(command, **kwargs):
            del command, kwargs
            raise OSError("missing nginx")

        supervisor.process_factory = fail_to_spawn

        self.assertEqual(supervisor.run(), 1)

    def test_gate_open_hard_failure_kills_without_sigcont(self):
        class HardGateStatus(_FakeStatusStore):
            marker_path = "/path/that/does/not/exist"

            def open_service(self, readiness_confirmed):
                del readiness_confirmed
                raise self.status_error("runtime_gate_fail_closed_failed")

        status = HardGateStatus(
            self.events,
            self.supervisor_module.StatusError,
        )
        supervisor, processes = self._supervisor(
            b'{"phase":"preparing"}\n{"phase":"complete"}\n',
            0,
            status=status,
        )

        self.assertEqual(supervisor.run(), 1)
        nginx_signals = [
            signum for role, signum in processes.signal_order
            if role == "nginx"
        ]
        self.assertEqual(nginx_signals[0], signal.SIGSTOP)
        self.assertNotIn(signal.SIGCONT, nginx_signals)
        self.assertTrue(processes.processes["nginx"].wait_called)
        self.assertTrue(processes.processes["gunicorn"].wait_called)

    def test_recovered_gate_open_failure_resumes_static_nginx(self):
        marker = tempfile.NamedTemporaryFile()
        self.addCleanup(marker.close)

        class RecoveredGateStatus(_FakeStatusStore):
            marker_path = marker.name

            def open_service(self, readiness_confirmed):
                del readiness_confirmed
                raise self.status_error("runtime_gate_open_failed")

        status = RecoveredGateStatus(
            self.events,
            self.supervisor_module.StatusError,
        )
        supervisor, processes = self._supervisor(
            b'{"phase":"preparing"}\n{"phase":"complete"}\n',
            0,
            status=status,
        )
        result = []
        thread = threading.Thread(target=lambda: result.append(supervisor.run()))
        thread.start()
        self._wait_until(
            lambda: "failed:runtime_gate_open_failed" in self.events
        )

        nginx_signals = [
            signum for role, signum in processes.signal_order
            if role == "nginx"
        ]
        self.assertEqual(nginx_signals[:2], [signal.SIGSTOP, signal.SIGCONT])
        self.assertTrue(processes.processes["nginx"].running)
        supervisor.handle_signal(signal.SIGTERM, None)
        thread.join(2)
        self.assertEqual(result, [0])


class RuntimeSupervisorBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.supervisor = _load_script("supervisor")

    def test_status_error_preserves_only_allowlisted_exact_argument(self):
        allowed = self.supervisor.StatusError("runtime_gate_open_failed")
        private = RuntimeError("runtime_gate_open_failed")

        self.assertEqual(
            self.supervisor._safe_error_code(allowed),
            "runtime_gate_open_failed",
        )
        self.assertEqual(
            self.supervisor._safe_error_code(private),
            "runtime_supervisor_failed",
        )

    def test_readiness_requires_exact_version_relationship(self):
        valid = {
            "source_commit": "a" * 40,
            "display_version": "a" * 12,
        }
        cases = (
            (200, "application/json", valid, True),
            (302, "application/json", valid, False),
            (200, "text/html", valid, False),
            (200, "application/json", {"source_commit": ""}, False),
            (200, "application/json", {
                "source_commit": "a" * 40,
                "display_version": "b" * 12,
            }, False),
            (200, "application/json", {
                "source_commit": "development",
                "display_version": "development",
            }, True),
        )
        for status, content_type, payload, expected in cases:
            with self.subTest(payload=payload):
                self.assertEqual(
                    self.supervisor._valid_readiness_payload(
                        status,
                        content_type,
                        payload,
                    ),
                    expected,
                )

    def test_proc_stat_uses_last_closing_parenthesis(self):
        stat_line = (
            "123 (worker ) name) T 1 123 123 0 0 0 0 0 0 0 0 "
            "0 0 0 0 0 0 0 98765 0 0"
        )

        identity = self.supervisor._parse_proc_stat(stat_line)

        self.assertEqual(identity["state"], "T")
        self.assertEqual(identity["pgid"], 123)
        self.assertEqual(identity["session"], 123)
        self.assertEqual(identity["starttime"], "98765")

    def test_heartbeat_uses_five_second_monotonic_interval(self):
        events = []
        status = _FakeStatusStore(events, self.supervisor.StatusError)
        values = iter((0.0, 4.9, 5.0, 9.9, 10.0))
        runtime = self.supervisor.RuntimeSupervisor(
            [], "/data", 33, 33, status, clock=lambda: next(values)
        )

        for _ in range(5):
            runtime._status_tick()

        self.assertEqual(status.heartbeat_count, 3)
        self.assertEqual(status.publish_count, 5)

    @staticmethod
    def _stopped_wait_status():
        return (signal.SIGSTOP << 8) | 0x7f

    def test_quiescence_requires_master_latch_and_stable_stopped_group(self):
        status = _FakeStatusStore([], self.supervisor.StatusError)
        now = [0.0]

        def clock():
            now[0] += 0.1
            return now[0]

        identity = {
            "pid": 4200,
            "pgid": 4200,
            "session": 4200,
            "starttime": "42",
            "state": "T",
        }
        wait_results = iter((
            (4200, self._stopped_wait_status()),
            (0, 0),
        ))
        runtime = self.supervisor.RuntimeSupervisor(
            [],
            "/data",
            33,
            33,
            status,
            clock=clock,
            identity_reader=lambda pid: dict(identity),
            group_reader=lambda pgid: {4200: dict(identity)},
            waitpid=lambda pid, flags: next(wait_results),
            sleeper=lambda seconds: None,
        )
        record = self.supervisor._ChildRecord(
            "nginx",
            _FakeProcess("nginx", 4200),
            identity,
        )

        self.assertTrue(runtime._default_quiescence_probe(record, 2.0))

    def test_quiescence_timeout_rejects_running_group(self):
        status = _FakeStatusStore([], self.supervisor.StatusError)
        now = [0.0]

        def clock():
            now[0] += 0.4
            return now[0]

        identity = {
            "pid": 4200,
            "pgid": 4200,
            "session": 4200,
            "starttime": "42",
            "state": "S",
        }
        runtime = self.supervisor.RuntimeSupervisor(
            [],
            "/data",
            33,
            33,
            status,
            clock=clock,
            identity_reader=lambda pid: dict(identity),
            group_reader=lambda pgid: {4200: dict(identity)},
            waitpid=lambda pid, flags: (0, 0),
            sleeper=lambda seconds: None,
        )
        record = self.supervisor._ChildRecord(
            "nginx",
            _FakeProcess("nginx", 4200),
            identity,
        )

        self.assertFalse(runtime._default_quiescence_probe(record, 1.0))

    def test_quiescence_echild_is_hard_failure(self):
        status = _FakeStatusStore([], self.supervisor.StatusError)
        identity = {
            "pid": 4200,
            "pgid": 4200,
            "session": 4200,
            "starttime": "42",
            "state": "T",
        }

        def missing_child(pid, flags):
            del pid, flags
            raise OSError(errno.ECHILD, "gone")

        runtime = self.supervisor.RuntimeSupervisor(
            [],
            "/data",
            33,
            33,
            status,
            waitpid=missing_child,
        )
        record = self.supervisor._ChildRecord(
            "nginx",
            _FakeProcess("nginx", 4200),
            identity,
        )

        with self.assertRaisesRegex(
            self.supervisor.SupervisorError,
            "runtime_supervisor_failed",
        ):
            runtime._default_quiescence_probe(
                record,
                time.monotonic() + 1,
            )


if __name__ == "__main__":
    unittest.main()
