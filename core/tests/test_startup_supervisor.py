import errno
import fcntl
import http.client
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

    def test_cli_sanitizes_huge_decimal_descriptor(self):
        with self.assertRaisesRegex(
            self.worker.WorkerProtocolError,
            "startup_argument_invalid",
        ):
            self.worker._parse_cli([
                "--progress-fd", "9" * 1000,
                "--lock-fd", str(self.lock_file.fileno()),
            ])


class WorkerCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.worker = _load_script("migration_worker")

    def test_schema_commands_keep_static_assets_but_skip_current_migrate(self):
        call_command = mock.Mock()

        self.worker._run_schema_commands(
            call_command,
            migrate_required=False,
        )

        call_command.assert_called_once_with(
            "collectstatic", interactive=False
        )

    def test_worker_invalidates_stale_marker_without_schema_migration(self):
        from django.conf import settings

        coordinator = mock.Mock()
        coordinator.prepare_before_schema.return_value = object()
        coordinator.schema_required.return_value = False
        reporter = mock.Mock()
        data_root = os.path.abspath(os.fspath(settings.PINRY_DATA_ROOT))

        with mock.patch(
            "django_images.services.legacy_startup."
            "LegacyStartupCoordinator",
            return_value=coordinator,
        ), mock.patch.object(
            self.worker,
            "_service_identity",
            return_value=(33, 44),
        ), mock.patch.object(
            self.worker,
            "_run_schema_commands",
        ) as schema_commands, mock.patch.object(
            self.worker,
            "_finalize_coordinator",
        ), mock.patch.dict(
            self.worker.os.environ,
            {"PINRY_DATA_ROOT": data_root},
        ):
            self.worker._run_coordinator(
                [self.worker._MIGRATION_FLAG],
                lock_fd=7,
                reporter=reporter,
            )

        coordinator.invalidate_startup_validation.assert_called_once_with()
        schema_commands.assert_called_once_with(
            mock.ANY,
            migrate_required=False,
        )

    def test_success_is_recorded_only_after_runtime_check(self):
        events = []
        coordinator = mock.Mock()
        coordinator.adjust_ownership.side_effect = (
            lambda descriptor: events.append(("ownership", descriptor))
        )
        coordinator.runtime_check.side_effect = (
            lambda uid, gid: events.append(("runtime", uid, gid))
        )
        coordinator.record_successful_startup.side_effect = (
            lambda: events.append(("record",))
        )

        self.worker._finalize_coordinator(
            coordinator,
            lock_fd=7,
            service_uid=33,
            service_gid=44,
        )

        self.assertEqual(events, [
            ("ownership", 7),
            ("runtime", 33, 44),
            ("record",),
        ])

    def test_runtime_failure_never_records_success(self):
        coordinator = mock.Mock()
        coordinator.runtime_check.side_effect = RuntimeError("probe failed")

        with self.assertRaisesRegex(RuntimeError, "probe failed"):
            self.worker._finalize_coordinator(
                coordinator,
                lock_fd=7,
                service_uid=33,
                service_gid=44,
            )

        coordinator.record_successful_startup.assert_not_called()


class WorkerSubprocessIntegrationTests(unittest.TestCase):
    def test_real_worker_uses_passed_fds_project_cwd_and_terminal_eof(self):
        temporary = Path(tempfile.mkdtemp(dir="/private/tmp"))
        wrapper = temporary / "worker_wrapper.py"
        capture = temporary / "capture.jsonl"
        lock_path = temporary / "startup.lock"
        wrapper.write_text(
            "import json\n"
            "import os\n"
            "import sys\n"
            "root = sys.argv[1]\n"
            "capture = sys.argv[2]\n"
            "arguments = sys.argv[3:]\n"
            "sys.path.insert(0, root)\n"
            "from docker.scripts import migration_worker as worker\n"
            "def record(name, **values):\n"
            "    values.update({'name': name, 'cwd': os.getcwd()})\n"
            "    with open(capture, 'a', encoding='utf-8') as target:\n"
            "        target.write(json.dumps(values, sort_keys=True) + '\\n')\n"
            "record('wrapper')\n"
            "def bootstrap():\n"
            "    record('bootstrap')\n"
            "def setup_django():\n"
            "    record('django')\n"
            "def coordinator(args, lock_fd, reporter):\n"
            "    os.fstat(lock_fd)\n"
            "    record('coordinator', args=args, "
            "lock_inheritable=os.get_inheritable(lock_fd), "
            "progress_inheritable=os.get_inheritable(reporter.progress_fd))\n"
            "    reporter.send({'phase': 'preparing'})\n"
            "worker._run_bootstrap = bootstrap\n"
            "worker._setup_django = setup_django\n"
            "worker._run_coordinator = coordinator\n"
            "raise SystemExit(worker._entrypoint(arguments))\n",
            encoding="utf-8",
        )
        reader, writer = os.pipe()
        lock_file = open(str(lock_path), "a+b")
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(lock_file.close)
        self.addCleanup(os.close, reader)
        process = subprocess.Popen(
            [
                sys.executable,
                str(wrapper),
                str(REPOSITORY_ROOT),
                str(capture),
                "--migrate-legacy",
                "--progress-fd",
                str(writer),
                "--lock-fd",
                str(lock_file.fileno()),
            ],
            cwd="/private/tmp",
            close_fds=True,
            pass_fds=(writer, lock_file.fileno()),
        )
        os.close(writer)

        chunks = []
        while True:
            payload = os.read(reader, 4096)
            if not payload:
                break
            chunks.append(payload)

        self.assertEqual(process.wait(timeout=5), 0)
        frames = [
            json.loads(line.decode("utf-8"))
            for line in b"".join(chunks).splitlines()
        ]
        self.assertEqual(frames, [
            {"phase": "preparing"},
            {"phase": "complete"},
        ])
        records = [
            json.loads(line)
            for line in capture.read_text("utf-8").splitlines()
        ]
        self.assertEqual(
            [record["name"] for record in records],
            ["wrapper", "bootstrap", "django", "coordinator"],
        )
        self.assertEqual(records[0]["cwd"], "/private/tmp")
        self.assertTrue(all(
            record["cwd"] == str(REPOSITORY_ROOT)
            for record in records[1:]
        ))
        self.assertEqual(records[-1]["args"], ["--migrate-legacy"])
        self.assertFalse(records[-1]["lock_inheritable"])
        self.assertFalse(records[-1]["progress_inheritable"])

        contender = open(str(lock_path), "a+b")
        try:
            with self.assertRaises((IOError, OSError)):
                fcntl.flock(
                    contender.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
        finally:
            contender.close()


class StartupRunnerSubprocessIntegrationTests(unittest.TestCase):
    def test_real_runner_imports_project_modules_from_private_tmp(self):
        temporary = Path(tempfile.mkdtemp(dir="/private/tmp"))
        project_root = temporary / "project"
        scripts = project_root / "docker/scripts"
        scripts.mkdir(parents=True)
        for package in (project_root / "docker", scripts):
            (package / "__init__.py").write_text("", encoding="utf-8")
        (scripts / "startup.py").write_text(
            (REPOSITORY_ROOT / "docker/scripts/startup.py").read_text(
                encoding="utf-8"
            ),
            encoding="utf-8",
        )
        (scripts / "migration_status.py").write_text(
            "ERROR_CODE_CLASSES = {'runtime_supervisor_failed': 'runtime'}\n"
            "STATUS_DIRECTORY = '/private/tmp/status'\n"
            "class MigrationStatusStore(object):\n"
            "    def __init__(self, *arguments): self.arguments = arguments\n",
            encoding="utf-8",
        )
        capture = temporary / "runner.jsonl"
        (scripts / "supervisor.py").write_text(
            "import json, os\n"
            "class RuntimeSupervisor(object):\n"
            "    def __init__(self, arguments, data_root, uid, gid, status):\n"
            "        self.values = {'arguments': arguments, 'data_root': data_root, "
            "'uid': uid, 'gid': gid, 'cwd': os.getcwd()}\n"
            "    def handle_signal(self, signum, frame): pass\n"
            "    def run(self):\n"
            "        with open(os.environ['PINRY_STARTUP_CAPTURE'], 'w') as target:\n"
            "            json.dump(self.values, target)\n"
            "        return 0\n",
            encoding="utf-8",
        )
        environment = os.environ.copy()
        environment["PINRY_STARTUP_CAPTURE"] = str(capture)
        environment["PINRY_DATA_ROOT"] = str(temporary / "data")
        environment["PYTHONPATH"] = ""

        completed = subprocess.run(
            [sys.executable, str(scripts / "startup.py"), "--migrate-legacy"],
            cwd="/private/tmp",
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        values = json.loads(capture.read_text(encoding="utf-8"))
        self.assertEqual(values["arguments"], ["--migrate-legacy"])
        self.assertEqual(values["data_root"], str(temporary / "data"))
        self.assertEqual(values["cwd"], "/private/tmp")


class GunicornSubprocessIntegrationTests(unittest.TestCase):
    def test_real_gunicorn_process_is_started_from_project_root(self):
        supervisor_module = _load_script("supervisor")
        temporary = Path(tempfile.mkdtemp(dir="/private/tmp"))
        capture = temporary / "cwd.txt"
        command = temporary / "capture_cwd.py"
        command.write_text(
            "#!{}\n"
            "import os\n"
            "with open({!r}, 'w') as target:\n"
            "    target.write(os.getcwd())\n".format(
                sys.executable,
                str(capture),
            ),
            encoding="utf-8",
        )
        command.chmod(0o700)

        def identity_reader(pid):
            return {
                "pid": pid,
                "pgid": pid,
                "session": pid,
                "starttime": "100",
                "state": "S",
            }

        status = _FakeStatusStore([], supervisor_module.StatusError)
        runtime = supervisor_module.RuntimeSupervisor(
            [],
            "/data",
            33,
            33,
            status,
            identity_reader=identity_reader,
            group_reader=lambda pgid: {},
        )
        previous_cwd = os.getcwd()
        try:
            os.chdir("/private/tmp")
            with mock.patch.object(
                supervisor_module, "GUNICORN_PATH", str(command)
            ):
                record = runtime._spawn_gunicorn()
            self.assertEqual(record.process.wait(timeout=5), 0)
            runtime._reap_record(record, timeout=0)
        finally:
            os.chdir(previous_cwd)
            if "gunicorn" in runtime.children:
                runtime._terminate_record(runtime.children["gunicorn"])

        self.assertEqual(capture.read_text(encoding="utf-8"), str(
            REPOSITORY_ROOT
        ))
        self.assertNotIn("gunicorn", runtime.children)


@unittest.skipUnless(hasattr(os, "fork"), "POSIX fork is required")
class ProcessGroupIntegrationTests(unittest.TestCase):
    def test_exited_master_record_survives_until_real_descendant_cleanup(self):
        supervisor_module = _load_script("supervisor")
        temporary = Path(tempfile.mkdtemp(dir="/private/tmp"))
        info_path = temporary / "group.json"
        script = (
            "import json, os, signal, sys, time\n"
            "child = os.fork()\n"
            "if child == 0:\n"
            "    signal.signal(signal.SIGTERM, "
            "lambda signum, frame: sys.exit(0))\n"
            "    while True:\n"
            "        time.sleep(0.05)\n"
            "temporary = sys.argv[1] + '.tmp'\n"
            "with open(temporary, 'w') as target:\n"
            "    json.dump({'master': os.getpid(), 'child': child, "
            "'pgid': os.getpgrp(), 'session': os.getsid(0)}, target)\n"
            "os.replace(temporary, sys.argv[1])\n"
            "time.sleep(0.2)\n"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(info_path)],
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 3
            while not info_path.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(info_path.exists())
            info = json.loads(info_path.read_text("utf-8"))
        except BaseException:
            if process.pid != os.getpgrp():
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    pass
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
            raise
        child_pid = info["child"]

        def child_alive():
            try:
                os.kill(child_pid, 0)
                return True
            except OSError as error:
                if error.errno == errno.ESRCH:
                    return False
                raise

        def group_reader(pgid):
            self.assertEqual(pgid, info["pgid"])
            if not child_alive():
                return {}
            return {child_pid: {
                "pid": child_pid,
                "pgid": info["pgid"],
                "session": info["session"],
                "starttime": "101",
                "state": "S",
            }}

        identity = {
            "pid": info["master"],
            "pgid": info["pgid"],
            "session": info["session"],
            "starttime": "100",
            "state": "S",
        }
        status = _FakeStatusStore([], supervisor_module.StatusError)
        runtime = supervisor_module.RuntimeSupervisor(
            [],
            "/data",
            33,
            33,
            status,
            group_reader=group_reader,
            group_signaler=os.killpg,
            sleeper=lambda seconds: time.sleep(min(seconds, 0.05)),
        )
        record = supervisor_module._ChildRecord(
            "migration", process, identity
        )
        runtime.children["migration"] = record
        try:
            self.assertEqual(process.wait(timeout=3), 0)
            self.assertEqual(runtime._reap_record(record, timeout=0), 0)
            self.assertIn("migration", runtime.children)

            runtime._terminate_record(record)

            deadline = time.monotonic() + 3
            while child_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(child_alive())
            self.assertNotIn("migration", runtime.children)
        finally:
            if child_alive():
                try:
                    os.killpg(info["pgid"], signal.SIGKILL)
                except OSError:
                    pass

    @unittest.skipUnless(
        sys.platform.startswith("linux") and os.path.isdir("/proc"),
        "Linux /proc is required",
    )
    def test_complete_waits_for_real_process_group_to_exit_without_signal(self):
        supervisor_module = _load_script("supervisor")
        progress_reader, progress_writer = os.pipe()
        gate_reader, gate_writer = os.pipe()
        script = (
            "import os, sys, time\n"
            "progress = int(sys.argv[1])\n"
            "gate = int(sys.argv[2])\n"
            "child = os.fork()\n"
            "if child == 0:\n"
            "    os.close(progress)\n"
            "    os.read(gate, 1)\n"
            "    os.close(gate)\n"
            "    time.sleep(1.5)\n"
            "    os._exit(0)\n"
            "os.read(gate, 1)\n"
            "os.close(gate)\n"
            "os.write(progress, b'{\"phase\":\"complete\"}\\n')\n"
            "os.close(progress)\n"
        )
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(progress_writer),
                str(gate_reader),
            ],
            close_fds=True,
            pass_fds=(progress_writer, gate_reader),
            start_new_session=True,
        )
        runtime = None
        worker = None
        signals = []
        try:
            os.close(progress_writer)
            progress_writer = -1
            os.close(gate_reader)
            gate_reader = -1
            identity = supervisor_module._read_proc_identity(process.pid)

            def signal_group(pgid, signum):
                signals.append((pgid, signum))
                os.killpg(pgid, signum)

            status = _FakeStatusStore([], supervisor_module.StatusError)
            runtime = supervisor_module.RuntimeSupervisor(
                [],
                "/data",
                33,
                33,
                status,
                group_signaler=signal_group,
            )
            worker = supervisor_module._ChildRecord(
                "migration", process, identity
            )
            nginx = supervisor_module._ChildRecord(
                "nginx", _FakeProcess("nginx", 4990), None
            )
            runtime.worker = worker
            runtime.nginx = nginx
            runtime.children["migration"] = worker
            runtime.progress_reader = progress_reader
            os.close(gate_writer)
            gate_writer = -1
            started_at = time.monotonic()

            self.assertEqual(runtime._drive_worker_and_heartbeat(), 0)
            self.assertEqual(process.poll(), 0)
            self.assertGreaterEqual(time.monotonic() - started_at, 1.3)
            self.assertNotIn("migration", runtime.children)
            self.assertEqual(signals, [])
        finally:
            for descriptor in (progress_writer, gate_reader, gate_writer):
                if descriptor >= 0:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
            if (
                runtime is None
                or runtime.progress_reader is not None
            ):
                try:
                    os.close(progress_reader)
                except OSError:
                    pass
            record_remains = (
                runtime is not None
                and worker is not None
                and runtime._record_is_registered(worker)
            )
            if process.poll() is None or record_remains:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass


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


class _TimedProcess(object):
    def __init__(
        self,
        role,
        pid,
        now,
        exit_at,
        returncode=0,
        wait_observer=None,
        terminate_works=True,
        kill_works=True,
    ):
        self.role = role
        self.pid = pid
        self.now = now
        self.exit_at = exit_at
        self.returncode = returncode
        self.wait_observer = wait_observer
        self.terminate_works = terminate_works
        self.kill_works = kill_works
        self.running = True
        self.wait_timeouts = []
        self.terminate_called = False
        self.kill_called = False

    def poll(self):
        if self.running and self.now[0] >= self.exit_at:
            self.running = False
        if self.running:
            return None
        return self.returncode

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
        result = self.poll()
        if result is not None:
            return result
        if timeout is None:
            self.now[0] = self.exit_at
        else:
            self.now[0] += min(timeout, self.exit_at - self.now[0])
        if self.wait_observer is not None:
            self.wait_observer()
        result = self.poll()
        if result is None:
            raise subprocess.TimeoutExpired(self.role, timeout)
        return result

    def terminate(self):
        self.terminate_called = True
        if self.terminate_works:
            self.running = False
            self.returncode = -signal.SIGTERM

    def kill(self):
        self.kill_called = True
        if self.kill_works:
            self.running = False
            self.returncode = -signal.SIGKILL


class _RecordingProcess(object):
    def __init__(self, process):
        self.process = process
        self.pid = process.pid
        self.wait_timeouts = []

    def poll(self):
        return self.process.poll()

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
        return self.process.wait(timeout=timeout)

    def terminate(self):
        return self.process.terminate()

    def kill(self):
        return self.process.kill()


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
        export_behaviors=None,
        clock=time.monotonic,
    ):
        self.events = events
        self.worker_payload = worker_payload
        self.worker_returncode = worker_returncode
        self.worker_running = worker_running
        self.hold_worker_pipe = hold_worker_pipe
        self.delayed_worker_payload = delayed_worker_payload
        self.block_worker_wait = block_worker_wait
        self.export_behaviors = list(export_behaviors or ())
        self.clock = clock
        self.processes = {}
        self.export_processes = []
        self.export_attempt_times = []
        self.spawn_kwargs = {}
        self.spawn_commands = {}
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
        elif "export_worker.py" in rendered:
            role = "export"
            self.export_attempt_times.append(self.clock())
            behavior = (
                self.export_behaviors.pop(0)
                if self.export_behaviors else (True, 0)
            )
            if behavior == "spawn_error":
                self.events.append("export_spawn_failed")
                raise OSError("export spawn failed")
            running, returncode = behavior
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
        if role == "export":
            self.export_processes.append(process)
        self.spawn_kwargs[role] = kwargs
        self.spawn_commands[role] = list(command)
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
        self.events.append("{}_signal:{}".format(process.role, signum))
        if signum in (signal.SIGTERM, signal.SIGKILL, signal.SIGINT):
            process.running = False
            process.returncode = -signum
            if (
                process.role == "migration"
                and self.held_progress_descriptor is not None
            ):
                os.close(self.held_progress_descriptor)
                self.held_progress_descriptor = None


class _FakeCommandRunner(object):
    def __init__(self, events, results=None, clock=time.monotonic):
        self.events = events
        self.results = list(results or ())
        self.clock = clock
        self.commands = []
        self.call_times = []
        self.kwargs = []

    def __call__(self, command, **kwargs):
        self.events.append("export_bootstrap")
        self.call_times.append(self.clock())
        self.commands.append(list(command))
        self.kwargs.append(dict(kwargs))
        result = self.results.pop(0) if self.results else 0
        if isinstance(result, Exception):
            raise result
        return subprocess.CompletedProcess(
            list(command), result, stderr=(
                b"" if result == 0 else b"export_storage_unsafe\n"
            )
        )


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
        export_behaviors=None,
        bootstrap_results=None,
        clock=time.monotonic,
        sleeper=None,
    ):
        processes = _FakeProcesses(
            self.events,
            worker_payload,
            worker_returncode,
            worker_running=worker_running,
            hold_worker_pipe=hold_worker_pipe,
            delayed_worker_payload=delayed_worker_payload,
            block_worker_wait=block_worker_wait,
            export_behaviors=export_behaviors,
            clock=clock,
        )
        bootstrap_runner = _FakeCommandRunner(
            self.events, results=bootstrap_results, clock=clock
        )
        processes.bootstrap_runner = bootstrap_runner
        supervisor = self.supervisor_module.RuntimeSupervisor(
            ["--migrate-legacy"],
            "/data",
            33,
            33,
            status or self.status,
            clock=clock,
            process_factory=processes,
            lock_acquirer=lambda *args: self._acquire_lock(*args),
            identity_reader=processes.identity,
            group_reader=processes.group,
            group_signaler=processes.signal,
            readiness_probe=readiness_probe,
            quiescence_probe=quiescence_probe,
            sleeper=(
                sleeper
                if sleeper is not None
                else lambda seconds: time.sleep(min(seconds, 0.01))
            ),
        )
        supervisor.command_runner = bootstrap_runner
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

    def _drive_after_eof(
        self,
        process,
        payload=b'{"phase":"complete"}\n',
        clock=time.monotonic,
        sleeper=time.sleep,
        identity=None,
        group_reader=lambda pgid: {},
        group_signaler=lambda pgid, signum: None,
    ):
        reader, writer = os.pipe()
        os.write(writer, payload)
        os.close(writer)
        supervisor = self.supervisor_module.RuntimeSupervisor(
            [],
            "/data",
            33,
            33,
            self.status,
            clock=clock,
            group_reader=group_reader,
            group_signaler=group_signaler,
            sleeper=sleeper,
        )
        worker = self.supervisor_module._ChildRecord(
            "migration", process, identity
        )
        nginx = self.supervisor_module._ChildRecord(
            "nginx", _FakeProcess("nginx", 4900), None
        )
        supervisor.worker = worker
        supervisor.nginx = nginx
        supervisor.children["migration"] = worker
        supervisor.children["nginx"] = nginx
        supervisor.progress_reader = reader
        return supervisor

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

    def test_ready_shutdown_signals_export_gunicorn_then_nginx(self):
        supervisor, processes = self._supervisor(
            b'{"phase":"preparing"}\n{"phase":"complete"}\n',
            0,
        )
        result = []
        thread = threading.Thread(target=lambda: result.append(supervisor.run()))
        thread.start()
        self._wait_until(lambda: bool(processes.export_processes))

        supervisor.handle_signal(signal.SIGTERM, None)
        thread.join(2)

        self.assertEqual(
            [role for role, signum in processes.signal_order
             if signum == signal.SIGTERM],
            ["export", "gunicorn", "nginx"],
        )
        self.assertTrue(processes.processes["migration"].wait_called)
        self.assertEqual(result, [0])

    def test_export_bootstrap_and_worker_start_after_public_gate_opens(self):
        supervisor, processes = self._supervisor(
            b'{"phase":"preparing"}\n{"phase":"complete"}\n',
            0,
        )
        result = []
        thread = threading.Thread(target=lambda: result.append(supervisor.run()))
        thread.start()
        try:
            self._wait_until(lambda: supervisor.export_worker is not None)

            ordered_events = (
                "migration_started",
                "gunicorn_started",
                "service_opened",
                "nginx_signal:{}".format(signal.SIGCONT),
                "export_bootstrap",
                "export_started",
            )
            positions = [self.events.index(event) for event in ordered_events]
            self.assertEqual(positions, sorted(positions))
            self.assertTrue(self.status.ready)
            self.assertNotIn("gate_created", self.events)
            self.assertIsNot(supervisor.worker, supervisor.export_worker)
            self.assertEqual(supervisor.worker.role, "migration")
            self.assertEqual(supervisor.export_worker.role, "export")
            self.assertNotIn("migration", supervisor.children)
            self.assertIs(
                supervisor.children["export"], supervisor.export_worker
            )
            self.assertEqual(
                processes.bootstrap_runner.commands[0],
                [
                    sys.executable,
                    str(REPOSITORY_ROOT / "docker/scripts/"
                        "export_storage_bootstrap.py"),
                    "--data-root", "/data",
                    "--uid", "33",
                    "--gid", "33",
                ],
            )
            self.assertEqual(
                processes.spawn_commands["export"],
                [
                    sys.executable,
                    str(REPOSITORY_ROOT / "docker/scripts/export_worker.py"),
                    "--uid", "33",
                    "--gid", "33",
                ],
            )
            self.assertEqual(
                processes.spawn_kwargs["export"].get("cwd"),
                str(REPOSITORY_ROOT),
            )
        finally:
            supervisor.handle_signal(signal.SIGTERM, None)
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [0])

    def test_export_bootstrap_failure_is_nonfatal_after_gate_open(self):
        supervisor, processes = self._supervisor(
            b'{"phase":"complete"}\n',
            0,
            bootstrap_results=[1],
        )
        result = []
        thread = threading.Thread(target=lambda: result.append(supervisor.run()))
        thread.start()
        try:
            self._wait_until(lambda: bool(processes.export_processes))

            self.assertTrue(self.status.ready)
            self.assertTrue(processes.processes["nginx"].running)
            self.assertTrue(processes.processes["gunicorn"].running)
            self.assertTrue(processes.processes["export"].running)
            self.assertNotIn("gate_created", self.events)
            self.assertEqual(self.status.failed_codes, [])
        finally:
            supervisor.handle_signal(signal.SIGTERM, None)
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [0])

    def test_export_restart_uses_exact_capped_backoff_and_bootstrap_each_time(self):
        now = [0.0]
        supervisor, processes = self._supervisor(
            b'{"phase":"complete"}\n',
            0,
            export_behaviors=["spawn_error"] * 7,
            clock=lambda: now[0],
            sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
        )
        self.status.ready = True
        expected_attempts = [0.0, 1.0, 3.0, 7.0, 15.0, 31.0, 61.0]

        for index, attempt_at in enumerate(expected_attempts):
            if index:
                now[0] = attempt_at - 0.01
                supervisor._maintain_export_worker()
                self.assertEqual(
                    len(processes.export_attempt_times), index
                )
            now[0] = attempt_at
            supervisor._maintain_export_worker()
            self.assertEqual(
                len(processes.export_attempt_times), index + 1
            )
            self.assertIsNone(supervisor.export_worker)
            self.assertNotIn("export", supervisor.children)

        self.assertEqual(
            processes.export_attempt_times, expected_attempts
        )
        self.assertEqual(
            processes.bootstrap_runner.call_times, expected_attempts
        )
        self.assertEqual(supervisor.export_restart_delay, 30.0)
        self.assertEqual(supervisor.export_restart_at, 91.0)
        self.assertTrue(self.status.ready)
        self.assertNotIn("gate_created", self.events)

    def test_export_master_and_descendant_are_reaped_before_respawn(self):
        now = [0.0]
        supervisor, processes = self._supervisor(
            b'{"phase":"complete"}\n',
            0,
            export_behaviors=[(True, 1), (True, 0)],
            clock=lambda: now[0],
            sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
        )
        self.status.ready = True

        supervisor._maintain_export_worker()
        first = processes.export_processes[0]
        first.running = False
        first.returncode = 1
        descendant_alive = [True]
        descendant_signals = []
        original_group = processes.group
        original_signal = processes.signal

        def read_group(pgid):
            if pgid == first.pid and descendant_alive[0]:
                return {first.pid + 100: {
                    "pid": first.pid + 100,
                    "pgid": first.pid,
                    "session": first.pid,
                    "starttime": "{}1".format(first.pid),
                    "state": "S",
                }}
            return original_group(pgid)

        def signal_group(pgid, signum):
            if pgid == first.pid:
                descendant_signals.append(signum)
                descendant_alive[0] = False
                return
            return original_signal(pgid, signum)

        supervisor.group_reader = read_group
        supervisor.group_signaler = signal_group
        supervisor._maintain_export_worker()

        self.assertTrue(first.wait_called)
        self.assertFalse(descendant_alive[0])
        self.assertIn(signal.SIGTERM, descendant_signals)
        self.assertEqual(len(processes.export_processes), 1)
        self.assertNotIn("export", supervisor.children)
        self.assertEqual(supervisor.export_restart_at, 1.0)

        now[0] = 0.99
        supervisor._maintain_export_worker()
        self.assertEqual(len(processes.export_processes), 1)
        now[0] = 1.0
        supervisor._maintain_export_worker()
        self.assertEqual(len(processes.export_processes), 2)
        self.assertIs(
            supervisor.children["export"].process,
            processes.export_processes[1],
        )

    def test_export_restart_delay_resets_after_sixty_seconds_alive(self):
        now = [0.0]
        supervisor, processes = self._supervisor(
            b'{"phase":"complete"}\n',
            0,
            export_behaviors=[(True, 0)],
            clock=lambda: now[0],
            sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
        )
        supervisor.export_restart_delay = 30.0
        supervisor._maintain_export_worker()
        worker = processes.export_processes[0]

        now[0] = 59.99
        supervisor._maintain_export_worker()
        self.assertEqual(supervisor.export_restart_delay, 30.0)
        now[0] = 60.0
        supervisor._maintain_export_worker()
        self.assertEqual(supervisor.export_restart_delay, 1.0)

        worker.running = False
        worker.returncode = 1
        supervisor._maintain_export_worker()
        self.assertEqual(supervisor.export_restart_at, 61.0)
        self.assertEqual(supervisor.export_restart_delay, 2.0)

    def test_export_dead_at_sixty_without_alive_observation_keeps_backoff(self):
        now = [0.0]
        supervisor, processes = self._supervisor(
            b'{"phase":"complete"}\n',
            0,
            export_behaviors=[(True, 0)],
            clock=lambda: now[0],
            sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
        )
        supervisor.export_restart_delay = 30.0
        supervisor._maintain_export_worker()
        worker = processes.export_processes[0]
        now[0] = 59.99
        worker.running = False
        worker.returncode = 1

        now[0] = 60.0
        supervisor._maintain_export_worker()

        self.assertEqual(supervisor.export_restart_at, 90.0)
        self.assertEqual(supervisor.export_restart_delay, 30.0)

    def test_export_backoff_keeps_public_children_and_status_ticks_alive(self):
        now = [0.0]
        holder = {}
        observation = {}

        def sleep(seconds):
            now[0] += seconds
            processes = holder.get("processes")
            supervisor = holder.get("supervisor")
            if (
                processes is not None
                and len(processes.export_attempt_times) >= 6
                and supervisor._shutdown_signal is None
            ):
                observation.update({
                    "nginx_pid": supervisor.nginx.pid,
                    "gunicorn_pid": supervisor.gunicorn.pid,
                    "nginx_running": supervisor.nginx.process.running,
                    "gunicorn_running": supervisor.gunicorn.process.running,
                    "signals": list(processes.signal_order),
                    "gate_created": "gate_created" in self.events,
                    "publish_count": self.status.publish_count,
                    "heartbeat_count": self.status.heartbeat_count,
                })
                supervisor.handle_signal(signal.SIGTERM, None)
            elif (
                supervisor is not None
                and now[0] >= 100.0
                and supervisor._shutdown_signal is None
            ):
                supervisor.handle_signal(signal.SIGTERM, None)

        supervisor, processes = self._supervisor(
            b'{"phase":"complete"}\n',
            0,
            export_behaviors=["spawn_error"] * 5 + [(True, 0)],
            clock=lambda: now[0],
            sleeper=sleep,
        )
        holder.update({"supervisor": supervisor, "processes": processes})

        result = supervisor.run()

        self.assertEqual(result, 0)
        self.assertTrue(self.status.ready)
        self.assertEqual(len(processes.export_attempt_times), 6)
        self.assertEqual(
            processes.bootstrap_runner.call_times,
            processes.export_attempt_times,
        )
        self.assertTrue(observation["nginx_running"])
        self.assertTrue(observation["gunicorn_running"])
        self.assertEqual(
            observation["nginx_pid"], processes.processes["nginx"].pid
        )
        self.assertEqual(
            observation["gunicorn_pid"],
            processes.processes["gunicorn"].pid,
        )
        self.assertEqual(observation["signals"][-2:], [
            ("nginx", signal.SIGSTOP),
            ("nginx", signal.SIGCONT),
        ])
        self.assertFalse(observation["gate_created"])
        self.assertGreater(observation["publish_count"], 5)
        self.assertGreater(observation["heartbeat_count"], 2)

    def test_export_immediate_exit_reaps_then_retries_after_one_second(self):
        now = [0.0]
        supervisor, processes = self._supervisor(
            b'{"phase":"complete"}\n',
            0,
            export_behaviors=[(False, 1), (True, 0)],
            clock=lambda: now[0],
            sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
        )
        self.status.ready = True
        supervisor.nginx = supervisor._spawn_nginx()
        supervisor.gunicorn = supervisor._spawn_gunicorn()
        public_children = (supervisor.nginx, supervisor.gunicorn)

        supervisor._maintain_export_worker()

        self.assertTrue(processes.export_processes[0].wait_called)
        self.assertNotIn("export", supervisor.children)
        self.assertEqual(supervisor.export_restart_at, 1.0)
        self.assertEqual(supervisor.export_restart_delay, 2.0)
        self.assertEqual((supervisor.nginx, supervisor.gunicorn), public_children)
        self.assertTrue(supervisor.nginx.process.running)
        self.assertTrue(supervisor.gunicorn.process.running)
        self.assertTrue(self.status.ready)
        self.assertNotIn("gate_created", self.events)

        now[0] = 0.99
        supervisor._maintain_export_worker()
        self.assertEqual(len(processes.export_attempt_times), 1)
        now[0] = 1.0
        supervisor._maintain_export_worker()
        self.assertEqual(len(processes.export_attempt_times), 2)
        self.assertEqual(processes.bootstrap_runner.call_times, [0.0, 1.0])

    def test_sigterm_during_export_spawn_defers_application_signal_order(self):
        supervisor, processes = self._supervisor(
            b'{"phase":"complete"}\n',
            0,
        )
        process_factory = supervisor.process_factory

        def spawn_then_signal(command, **kwargs):
            process = process_factory(command, **kwargs)
            if "export_worker.py" in " ".join(command):
                supervisor.handle_signal(signal.SIGTERM, None)
            return process

        supervisor.process_factory = spawn_then_signal

        self.assertEqual(supervisor.run(), 0)
        self.assertEqual(
            [
                role for role, signum in processes.signal_order
                if signum == signal.SIGTERM
            ],
            ["export", "gunicorn", "nginx"],
        )
        self.assertTrue(processes.processes["export"].wait_called)
        self.assertEqual(supervisor.children, {})
        self.assertTrue(self.status.ready)
        self.assertNotIn("gate_created", self.events)

    def test_export_bootstrap_oserror_still_starts_worker(self):
        supervisor, processes = self._supervisor(
            b'{"phase":"complete"}\n',
            0,
            bootstrap_results=[OSError("bootstrap unavailable")],
        )
        self.status.ready = True

        supervisor._maintain_export_worker()

        self.assertEqual(len(processes.export_processes), 1)
        self.assertTrue(processes.export_processes[0].running)
        self.assertNotIn("gate_created", self.events)
        self.assertEqual(self.status.failed_codes, [])

    def test_export_bootstrap_timeout_is_bounded_and_nonfatal(self):
        supervisor, processes = self._supervisor(
            b'{"phase":"complete"}\n',
            0,
            bootstrap_results=[
                subprocess.TimeoutExpired(["bootstrap"], 15.0)
            ],
        )
        self.status.ready = True
        supervisor.nginx = supervisor._spawn_nginx()
        supervisor.gunicorn = supervisor._spawn_gunicorn()

        supervisor._maintain_export_worker()

        self.assertEqual(
            processes.bootstrap_runner.kwargs[0]["timeout"], 15.0
        )
        self.assertEqual(len(processes.export_processes), 1)
        self.assertTrue(processes.export_processes[0].running)
        self.assertTrue(supervisor.nginx.process.running)
        self.assertTrue(supervisor.gunicorn.process.running)
        self.assertTrue(self.status.ready)
        self.assertNotIn("gate_created", self.events)
        self.assertEqual(self.status.failed_codes, [])

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

    def test_complete_eof_waits_for_real_worker_exit_before_success(self):
        reader, writer = os.pipe()
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import os, sys, time\n"
                    "descriptor = int(sys.argv[1])\n"
                    "os.write(descriptor, "
                    "b'{\"phase\":\"complete\"}\\n')\n"
                    "os.close(descriptor)\n"
                    "time.sleep(1.5)\n"
                ),
                str(writer),
            ],
            close_fds=True,
            pass_fds=(writer,),
            start_new_session=True,
        )
        process = _RecordingProcess(child)
        os.close(writer)
        supervisor, processes = self._supervisor(b"", 0)
        worker = self.supervisor_module._ChildRecord(
            "migration", process, None
        )

        def spawn_real_worker(lock_fd):
            del lock_fd
            supervisor.children["migration"] = worker
            return worker, reader

        supervisor._spawn_worker = spawn_real_worker
        result = []
        thread = threading.Thread(target=lambda: result.append(supervisor.run()))
        thread.start()
        try:
            self._wait_until(
                lambda: "worker_event:complete" in self.events
            )
            self.assertIsNone(process.poll())
            self.assertNotIn("gunicorn", processes.processes)

            self._wait_until(lambda: "gunicorn_started" in self.events)
            self.assertEqual(process.poll(), 0)
            self.assertLess(
                self.events.index("worker_event:complete"),
                self.events.index("gunicorn_started"),
            )
            self.assertTrue(process.wait_timeouts)
            self.assertTrue(all(
                timeout is not None and 0 <= timeout <= 1.0
                for timeout in process.wait_timeouts
            ))
        finally:
            supervisor.handle_signal(signal.SIGTERM, None)
            thread.join(2)
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [0])

    def test_worker_exit_at_exact_deadline_is_success(self):
        now = [0.0]
        sleeps = []
        process = _TimedProcess("migration", 4910, now, 15.0)
        supervisor = self._drive_after_eof(
            process,
            clock=lambda: now[0],
            sleeper=lambda seconds: (
                sleeps.append(seconds),
                now.__setitem__(0, now[0] + seconds),
            ),
        )

        self.assertEqual(supervisor._drive_worker_and_heartbeat(), 0)

        self.assertEqual(now[0], 15.0)
        self.assertEqual(process.wait_timeouts, [1.0] * 15)
        self.assertEqual(sleeps, [])
        self.assertNotIn("migration", supervisor.children)

    def test_complete_worker_exit_after_deadline_is_retryable_timeout(self):
        now = [0.0]
        process = _TimedProcess("migration", 4920, now, 15.1)
        supervisor = self._drive_after_eof(
            process,
            clock=lambda: now[0],
            sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
        )

        self.assertEqual(supervisor._drive_worker_and_heartbeat(), 1)

        self.assertEqual(supervisor.last_worker_error, "worker_exit_timeout")
        self.assertTrue(supervisor._worker_eof)
        self.assertTrue(supervisor._worker_exit_timed_out)
        self.assertFalse(supervisor._worker_succeeded)
        self.assertTrue(process.terminate_called)
        self.assertNotIn("migration", supervisor.children)
        self.assertEqual(process.wait_timeouts[:15], [1.0] * 15)
        self.assertIn(0, process.wait_timeouts[15:])
        self.assertGreaterEqual(self.status.publish_count, 15)
        self.assertGreaterEqual(self.status.heartbeat_count, 3)

    def test_error_worker_exit_timeout_preserves_terminal_error(self):
        now = [0.0]
        process = _TimedProcess(
            "migration", 4930, now, 15.1, returncode=1
        )
        supervisor = self._drive_after_eof(
            process,
            payload=(
                b'{"error_code":"bootstrap_failed","phase":"error"}\n'
            ),
            clock=lambda: now[0],
            sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
        )

        self.assertEqual(supervisor._drive_worker_and_heartbeat(), 1)

        self.assertEqual(supervisor.last_worker_error, "bootstrap_failed")
        self.assertFalse(supervisor._worker_exit_timed_out)
        self.assertTrue(process.terminate_called)
        self.assertNotIn("migration", supervisor.children)

    def test_worker_exit_timeout_cleanup_failure_is_fail_closed(self):
        now = [0.0]
        process = _TimedProcess(
            "migration",
            4940,
            now,
            60.0,
            terminate_works=False,
            kill_works=False,
        )
        supervisor = self._drive_after_eof(
            process,
            clock=lambda: now[0],
            sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
        )
        try:
            self.assertEqual(supervisor._drive_worker_and_heartbeat(), 1)

            self.assertEqual(
                supervisor.last_worker_error, "runtime_supervisor_failed"
            )
            self.assertTrue(process.terminate_called)
            self.assertTrue(process.kill_called)
            self.assertIn("migration", supervisor.children)
            self.assertEqual(now[0], 30.0)
        finally:
            process.kill_works = True
            process.kill()
            supervisor._reap_record(supervisor.worker, timeout=0)

    def test_master_exit_waits_in_bounded_slices_for_group_removal(self):
        now = [0.0]
        sleeps = []
        signals = []
        process = _TimedProcess("migration", 4950, now, 0.0)
        identity = {
            "pid": 4950,
            "pgid": 4950,
            "session": 4950,
            "starttime": "100",
            "state": "S",
        }

        def read_group(pgid):
            self.assertEqual(pgid, 4950)
            if now[0] >= 2.0:
                return {}
            return {4951: {
                "pid": 4951,
                "pgid": 4950,
                "session": 4950,
                "starttime": "101",
                "state": "S",
            }}

        def sleep(seconds):
            sleeps.append(seconds)
            now[0] += seconds

        supervisor = self._drive_after_eof(
            process,
            clock=lambda: now[0],
            sleeper=sleep,
            identity=identity,
            group_reader=read_group,
            group_signaler=lambda pgid, signum: signals.append(
                (pgid, signum)
            ),
        )

        self.assertEqual(supervisor._drive_worker_and_heartbeat(), 0)

        self.assertEqual(sleeps, [1.0, 1.0])
        self.assertEqual(signals, [])
        self.assertNotIn("migration", supervisor.children)

    def test_shutdown_is_observed_between_worker_exit_wait_slices(self):
        now = [0.0]
        holder = {}
        process = _TimedProcess(
            "migration",
            4960,
            now,
            30.0,
            wait_observer=lambda: holder["supervisor"].handle_signal(
                signal.SIGTERM, None
            ),
        )
        supervisor = self._drive_after_eof(
            process,
            clock=lambda: now[0],
            sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
        )
        holder["supervisor"] = supervisor
        try:
            self.assertEqual(
                supervisor._drive_worker_and_heartbeat(),
                self.supervisor_module._WORKER_SHUTDOWN,
            )
            self.assertEqual(process.wait_timeouts, [1.0])
        finally:
            process.terminate()
            supervisor._reap_record(supervisor.worker, timeout=0)

    def test_nginx_exit_is_observed_between_worker_exit_wait_slices(self):
        now = [0.0]
        holder = {}

        def stop_nginx():
            nginx = holder["supervisor"].nginx.process
            nginx.running = False
            nginx.returncode = 1

        process = _TimedProcess(
            "migration", 4970, now, 30.0, wait_observer=stop_nginx
        )
        supervisor = self._drive_after_eof(
            process,
            clock=lambda: now[0],
            sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
        )
        holder["supervisor"] = supervisor

        self.assertEqual(
            supervisor._drive_worker_and_heartbeat(),
            self.supervisor_module._NGINX_EXITED,
        )

        self.assertEqual(process.wait_timeouts[0], 1.0)
        self.assertTrue(all(
            timeout is not None and 0 <= timeout <= 1.0
            for timeout in process.wait_timeouts
        ))
        self.assertTrue(process.terminate_called)
        self.assertNotIn("migration", supervisor.children)

    def test_worker_eof_never_blocks_on_a_still_live_process(self):
        now = [0.0]

        def clock():
            value = now[0]
            now[0] += 1.0
            return value

        process = _FakeProcess(
            "migration",
            4980,
            running=True,
            block_wait_while_running=True,
        )
        supervisor = self._drive_after_eof(
            process,
            clock=clock,
            sleeper=lambda seconds: now.__setitem__(
                0, now[0] + seconds
            ),
        )

        self.assertEqual(supervisor._drive_worker_and_heartbeat(), 1)

        self.assertEqual(supervisor.last_worker_error, "worker_exit_timeout")
        self.assertTrue(process.wait_called)
        self.assertTrue(process.kill_called)
        self.assertNotIn("migration", supervisor.children)

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

    def test_gunicorn_exit_recreates_gate_before_stopping_export(self):
        now = [0.0]
        holder = {}

        def sleep(seconds):
            now[0] += seconds
            if "failed:gunicorn_start_failed" in self.events:
                nginx = holder["processes"].processes["nginx"]
                nginx.running = False
                nginx.returncode = 1

        supervisor, processes = self._supervisor(
            b'{"phase":"complete"}\n',
            0,
            clock=lambda: now[0],
            sleeper=sleep,
        )
        holder["processes"] = processes
        self.status.ready = True
        supervisor.nginx = supervisor._spawn_nginx()
        supervisor.gunicorn = supervisor._spawn_gunicorn()
        supervisor._maintain_export_worker()
        export = processes.export_processes[0]
        gunicorn = processes.processes["gunicorn"]
        gunicorn.running = False
        gunicorn.returncode = 1
        original_signal = processes.signal

        def ignore_export_term(pgid, signum):
            if pgid == export.pid and signum == signal.SIGTERM:
                processes.signal_order.append(("export", signum))
                self.events.append("export_signal:{}".format(signum))
                return
            original_signal(pgid, signum)

        supervisor.group_signaler = ignore_export_term

        self.assertEqual(supervisor._serve_application(), 1)
        self.assertLess(
            self.events.index("gate_created"),
            self.events.index("export_signal:{}".format(signal.SIGTERM)),
        )
        self.assertTrue(export.wait_called)
        self.assertFalse(export.running)

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

    def test_direct_master_reap_keeps_record_until_descendant_exits(self):
        process = _FakeProcess(
            "migration", 5300, running=False, returncode=0
        )
        identity = {
            "pid": 5300,
            "pgid": 5300,
            "session": 5300,
            "starttime": "300",
            "state": "S",
        }
        descendant_alive = [True]
        signals = []

        def read_group(pgid):
            self.assertEqual(pgid, 5300)
            if not descendant_alive[0]:
                return {}
            return {5301: {
                "pid": 5301,
                "pgid": 5300,
                "session": 5300,
                "starttime": "301",
                "state": "S",
            }}

        def signal_group(pgid, signum):
            self.assertEqual(pgid, 5300)
            signals.append(signum)
            if signum == signal.SIGTERM:
                descendant_alive[0] = False

        runtime = self.supervisor_module.RuntimeSupervisor(
            ["--migrate-legacy"],
            "/data",
            33,
            33,
            self.status,
            group_reader=read_group,
            group_signaler=signal_group,
            sleeper=lambda seconds: None,
        )
        record = self.supervisor_module._ChildRecord(
            "migration", process, identity
        )
        runtime.children["migration"] = record

        self.assertEqual(runtime._reap_record(record, timeout=0), 0)
        self.assertIn("migration", runtime.children)

        runtime._terminate_record(record)

        self.assertEqual(signals, [signal.SIGTERM])
        self.assertFalse(descendant_alive[0])
        self.assertNotIn("migration", runtime.children)

    def test_shutdown_children_share_one_fifteen_second_deadline(self):
        now = [0.0]
        records = {}
        alive = {}
        signals = []

        def clock():
            return now[0]

        def sleep(seconds):
            now[0] += seconds

        def identity_for(pid):
            if not alive.get(pid):
                raise OSError(errno.ESRCH, "gone")
            return {
                "pid": pid,
                "pgid": pid,
                "session": pid,
                "starttime": str(pid),
                "state": "S",
            }

        def read_group(pgid):
            if not alive.get(pgid):
                return {}
            return {pgid: identity_for(pgid)}

        def signal_group(pgid, signum):
            signals.append((pgid, signum, now[0]))
            if signum == signal.SIGKILL:
                alive[pgid] = False
                records[pgid].process.running = False
                records[pgid].process.returncode = -signal.SIGKILL

        runtime = self.supervisor_module.RuntimeSupervisor(
            [],
            "/data",
            33,
            33,
            self.status,
            clock=clock,
            identity_reader=identity_for,
            group_reader=read_group,
            group_signaler=signal_group,
            sleeper=sleep,
        )
        for role, pid in (
            ("export", 5300),
            ("gunicorn", 5400),
            ("nginx", 5500),
        ):
            process = _FakeProcess(
                role,
                pid,
                running=True,
                block_wait_while_running=True,
            )
            identity = identity_for(pid) if alive.setdefault(pid, True) else None
            record = self.supervisor_module._ChildRecord(
                role, process, identity
            )
            records[pid] = record
            runtime.children[role] = record

        runtime.handle_signal(signal.SIGTERM, None)
        runtime._cleanup()

        self.assertLessEqual(now[0], 15.0)
        term_pids = [item[0] for item in signals if item[1] == signal.SIGTERM]
        term_times = [item[2] for item in signals if item[1] == signal.SIGTERM]
        kill_times = [item[2] for item in signals if item[1] == signal.SIGKILL]
        self.assertEqual(term_pids, [5300, 5400, 5500])
        self.assertEqual(term_times, [0.0, 0.0, 0.0])
        self.assertEqual(len(kill_times), 3)
        self.assertTrue(all(value <= 15.0 for value in kill_times))
        self.assertEqual(runtime.children, {})

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

    def test_shutdown_before_run_starts_no_child(self):
        supervisor, processes = self._supervisor(
            b'{"phase":"complete"}\n',
            0,
        )

        supervisor.handle_signal(signal.SIGTERM, None)

        self.assertEqual(supervisor.run(), 0)
        self.assertEqual(processes.processes, {})

    def test_shutdown_during_lock_acquisition_starts_no_worker(self):
        supervisor, processes = self._supervisor(
            b'{"phase":"complete"}\n',
            0,
        )

        def acquire_then_shutdown(*arguments):
            lock = self._acquire_lock(*arguments)
            supervisor.handle_signal(signal.SIGTERM, None)
            return lock

        supervisor.lock_acquirer = acquire_then_shutdown

        self.assertEqual(supervisor.run(), 0)
        self.assertNotIn("migration", processes.processes)
        self.assertNotIn("gunicorn", processes.processes)

    def test_shutdown_after_worker_complete_starts_no_gunicorn(self):
        class ShutdownOnCompleteStatus(_FakeStatusStore):
            runtime = None

            def apply_worker_event(self, event):
                super(ShutdownOnCompleteStatus, self).apply_worker_event(event)
                if event["phase"] == "complete":
                    self.runtime.handle_signal(signal.SIGTERM, None)

        status = ShutdownOnCompleteStatus(
            self.events,
            self.supervisor_module.StatusError,
        )
        supervisor, processes = self._supervisor(
            b'{"phase":"complete"}\n',
            0,
            status=status,
        )
        status.runtime = supervisor

        self.assertEqual(supervisor.run(), 0)
        self.assertNotIn("gunicorn", processes.processes)

    def test_gunicorn_is_spawned_from_project_root(self):
        supervisor, processes = self._supervisor(
            b'{"phase":"complete"}\n',
            0,
        )

        record = supervisor._spawn_gunicorn()
        self.addCleanup(supervisor._terminate_record, record)

        self.assertEqual(
            processes.spawn_kwargs["gunicorn"].get("cwd"),
            str(REPOSITORY_ROOT),
        )

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
            (200, "application/json", {
                "source_commit": "a" * 40 + "\n",
                "display_version": "a" * 12,
            }, False),
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

    def test_default_readiness_ignores_environment_proxy(self):
        payload = json.dumps({
            "source_commit": "a" * 40,
            "display_version": "a" * 12,
        }).encode("utf-8")

        class Response(object):
            headers = {"Content-Type": "application/json"}

            def read(self, size):
                self.size = size
                return payload

            @staticmethod
            def getcode():
                return 200

            @staticmethod
            def close():
                pass

        class Opener(object):
            @staticmethod
            def open(request, timeout):
                del request, timeout
                return Response()

        handlers = []

        def build_opener(*items):
            handlers.extend(items)
            return Opener()

        status = _FakeStatusStore([], self.supervisor.StatusError)
        runtime = self.supervisor.RuntimeSupervisor(
            [], "/data", 33, 33, status
        )

        with mock.patch.dict(
            os.environ,
            {"HTTP_PROXY": "http://proxy.invalid:8080"},
        ), mock.patch.object(
            self.supervisor.urllib.request,
            "build_opener",
            side_effect=build_opener,
        ):
            self.assertTrue(runtime._default_readiness_probe())
        empty_proxy_handlers = [
            handler for handler in handlers
            if isinstance(handler, self.supervisor.urllib.request.ProxyHandler)
            and handler.proxies == {}
        ]
        self.assertEqual(len(empty_proxy_handlers), 1)

    def test_default_readiness_normalizes_truncated_body(self):
        class TruncatedResponse(object):
            headers = {"Content-Type": "application/json"}

            @staticmethod
            def read(size):
                del size
                raise http.client.IncompleteRead(b"partial", 1000)

            @staticmethod
            def close():
                pass

        opener = mock.Mock()
        opener.open.return_value = TruncatedResponse()
        status = _FakeStatusStore([], self.supervisor.StatusError)
        runtime = self.supervisor.RuntimeSupervisor(
            [], "/data", 33, 33, status
        )

        with mock.patch.object(
            self.supervisor.urllib.request,
            "build_opener",
            return_value=opener,
        ):
            self.assertFalse(runtime._default_readiness_probe())

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
