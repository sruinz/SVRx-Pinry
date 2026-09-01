import re
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
EXPORT_FIXTURE = (
    REPOSITORY_ROOT / "docker/tests/fixtures/create_export_fixture.py"
)
POSTGRES_SMOKE = (
    REPOSITORY_ROOT / "docker/tests/export_postgres_concurrency_smoke.sh"
)
RUNTIME_SMOKE = REPOSITORY_ROOT / "docker/tests/export_runtime_smoke.sh"


class ExportSmokeContractTests(unittest.TestCase):
    @staticmethod
    def _shell_function(source, name):
        match = re.search(
            r"^{}\(\) \{{\n.*?^\}}\n".format(re.escape(name)),
            source,
            re.MULTILINE | re.DOTALL,
        )
        if match is None:
            raise AssertionError("missing shell function: {}".format(name))
        return match.group(0)

    def _run_wait_ready_harness(
        self,
        payload,
        ready_code="503",
        migration_code="200",
    ):
        source = RUNTIME_SMOKE.read_text("utf-8")
        functions = "\n".join((
            self._shell_function(source, "read_startup_error_code"),
            self._shell_function(source, "wait_ready"),
        ))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload_path = root / "payload.json"
            harness_path = root / "harness.sh"
            payload_path.write_text(payload, encoding="utf-8")
            harness_path.write_text(
                "#!/bin/bash\n"
                "set -euo pipefail\n"
                "base_url=http://runtime-smoke.invalid\n"
                "migration_status_json=\"$1/status.json\"\n"
                "payload_source=\"$2\"\n"
                "ready_code=\"$3\"\n"
                "migration_code=\"$4\"\n"
                "clock_file=\"$1/clock\"\n"
                "calls_file=\"$1/calls\"\n"
                "sleeps_file=\"$1/sleeps\"\n"
                "startup_error_code=\n"
                "printf '0\\n' > \"${clock_file}\"\n"
                ": > \"${calls_file}\"\n"
                ": > \"${sleeps_file}\"\n"
                "date() {\n"
                "    local value\n"
                "    value=\"$(cat \"${clock_file}\")\"\n"
                "    value=$((value + 1))\n"
                "    printf '%s\\n' \"${value}\" > \"${clock_file}\"\n"
                "    printf '%s\\n' \"${value}\"\n"
                "}\n"
                "sleep() { printf 'sleep\\n' >> \"${sleeps_file}\"; }\n"
                "curl() {\n"
                "    local argument\n"
                "    local capture_output=0\n"
                "    local output=\n"
                "    local url=\n"
                "    for argument in \"$@\"; do\n"
                "        if [ \"${capture_output}\" -eq 1 ]; then\n"
                "            output=\"${argument}\"\n"
                "            capture_output=0\n"
                "        elif [ \"${argument}\" = --output ]; then\n"
                "            capture_output=1\n"
                "        fi\n"
                "        url=\"${argument}\"\n"
                "    done\n"
                "    printf '%s\\n' \"${url}\" >> \"${calls_file}\"\n"
                "    case \"${url}\" in\n"
                "        */readyz) printf '%s' \"${ready_code}\" ;;\n"
                "        */migration-status.json)\n"
                "            cp \"${payload_source}\" \"${output}\"\n"
                "            printf '%s' \"${migration_code}\"\n"
                "            ;;\n"
                "        *) return 90 ;;\n"
                "    esac\n"
                "}\n"
                + functions
                + "\nif wait_ready 2; then\n"
                "    wait_status=0\n"
                "else\n"
                "    wait_status=$?\n"
                "fi\n"
                "printf 'status=%s\\n' \"${wait_status}\"\n"
                "printf 'error=%s\\n' \"${startup_error_code}\"\n"
                "printf 'calls=%s\\n' \"$(wc -l < \"${calls_file}\" | tr -d ' ')\"\n"
                "printf 'sleeps=%s\\n' \"$(wc -l < \"${sleeps_file}\" | tr -d ' ')\"\n",
                encoding="utf-8",
            )
            completed = subprocess.run(
                [
                    "/bin/bash",
                    str(harness_path),
                    str(root),
                    str(payload_path),
                    ready_code,
                    migration_code,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=2,
            )

        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8", "replace"),
        )
        return dict(
            line.split("=", 1)
            for line in completed.stdout.decode("utf-8").splitlines()
        )

    def test_fixture_bootstraps_project_imports_outside_cwd(self):
        source = "\n".join((
            "import importlib.util",
            "import sys",
            "sys.path.append({!r})".format(str(REPOSITORY_ROOT)),
            "spec = importlib.util.spec_from_file_location(",
            "    'isolated_export_fixture', {!r})".format(
                str(EXPORT_FIXTURE)
            ),
            "module = importlib.util.module_from_spec(spec)",
            "spec.loader.exec_module(module)",
            "assert sys.path[0] == {!r}, sys.path".format(
                str(REPOSITORY_ROOT)
            ),
            "import pinry",
            "print('EXPORT_FIXTURE_PROJECT_IMPORT_OK')",
        ))
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            completed = subprocess.run(
                [sys.executable, "-I", "-c", source],
                cwd=temporary,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8", "replace"),
        )
        self.assertEqual(
            completed.stdout,
            b"EXPORT_FIXTURE_PROJECT_IMPORT_OK\n",
        )

    def test_postgres_smoke_reports_progress_and_bounds_test_suite(self):
        source = POSTGRES_SMOKE.read_text("utf-8")

        self.assertNotIn("docker start --attach", source)
        self.assertIn('docker start "${test_container_id}"', source)
        self.assertIn("test_timeout_seconds=1800", source)
        self.assertIn(
            "EXPORT_POSTGRES_PROGRESS phase=test_suite",
            source,
        )
        self.assertIn("export_postgres_test_timeout", source)

    def test_postgres_smoke_runs_only_focused_concurrency_regressions(self):
        source = POSTGRES_SMOKE.read_text("utf-8")
        match = re.search(
            r"&& python manage\.py test \\\n"
            r"(?P<targets>.*?)\n"
            r"\s*--settings=pinry\.settings\.test_postgres",
            source,
            re.DOTALL,
        )

        self.assertIsNotNone(match)
        targets = tuple(
            line.strip().rstrip("\\").strip()
            for line in match.group("targets").splitlines()
            if line.strip()
        )
        self.assertEqual(
            targets,
            (
                "exports.tests.test_concurrency."
                "ExportCreateConcurrencyTests."
                "test_two_file_sqlite_connections_commit_one_job_and_one_conflict",
                "exports.tests.test_snapshot.SnapshotServiceTests."
                "test_open_closed_and_progress_busy_cleanup_then_use_new_generation",
                "exports.tests.test_snapshot.SnapshotServiceTests."
                "test_short_lease_cas_uses_nowait_worker_and_job_row_locks",
                "exports.tests.test_snapshot.SnapshotServiceTests."
                "test_postgresql_nowait_lock_error_is_normalized_as_busy",
                "exports.tests.test_worker.WorkerLeaseTests."
                "test_claim_transition_blocks_tick_until_new_token_is_published",
                "exports.tests.test_worker.WorkerLeaseTests."
                "test_replace_and_clear_transitions_hide_old_token_from_tick",
                "exports.tests.test_worker.WorkerLeaseTests."
                "test_old_generation_heartbeat_fails_after_takeover",
                "exports.tests.test_worker.WorkerLeaseTests."
                "test_claim_deadline_rolls_back_then_retries_outside_the_guards",
                "exports.tests.test_archive_finalization.ArchiveServiceTests."
                "test_complete_rollback_keeps_previous_ready_job_valid",
                "exports.tests.test_archive_finalization.ArchiveServiceTests."
                "test_middle_revocations_release_guard_between_real_batches",
                "exports.tests.test_archive_finalization.ArchiveServiceTests."
                "test_final_revocation_deadline_rolls_back_db_and_keeps_old_tokens",
                "exports.tests.test_download.ExportDownloadAPITests."
                "test_mismatch_cas_preserves_concurrent_expired_pending",
            ),
        )
        self.assertIn(
            "EXPORT_POSTGRES_CONCURRENCY_SMOKE_OK backend=postgresql "
            "version=14 tests=12 fence_contract=1 actual_lock_retry=1",
            source,
        )
        self.assertNotIn("modules=6", source)

    def test_runtime_smoke_uses_public_profile_auth_routes(self):
        source = RUNTIME_SMOKE.read_text("utf-8")

        self.assertIn(
            "write_auth_body register 'export-smoke-zero-user' "
            '"${auth_body}"\n'
            "    auth_code=\"$(http_request POST "
            "'/api/v2/profile/users/'",
            source,
        )
        self.assertIn(
            'write_auth_body login "${username}" "${auth_body}"\n'
            "auth_code=\"$(http_request POST "
            "'/api/v2/profile/login/'",
            source,
        )
        self.assertEqual(
            source.count("http_request POST '/api/v2/profile/login/'"),
            1,
        )
        self.assertEqual(
            source.count("http_request POST '/api/v2/profile/users/'"),
            1,
        )
        self.assertNotIn(
            "http_request POST '/api/v2/users/login/'",
            source,
        )
        self.assertNotIn("http_request POST '/api/v2/users/'", source)

    def test_runtime_smoke_uses_shell_builtin_for_worker_signals(self):
        source = RUNTIME_SMOKE.read_text("utf-8")

        self.assertNotIn(
            'docker exec "${active_container_id}" kill -',
            source,
        )
        for signal in ("STOP", "CONT"):
            expected = (
                'docker exec "${active_container_id}" sh -c \'\n'
                + 'kill -{} "$1"\n'.format(signal)
                + '\' sh "${worker_pid}"'
            )
            self.assertIn(
                expected,
                source,
            )

    def test_runtime_smoke_fails_fast_on_terminal_startup_status(self):
        source = RUNTIME_SMOKE.read_text("utf-8")

        self.assertIn(
            '"${base_url}/migration-status.json"',
            source,
        )
        self.assertIn(
            'if [ "${startup_error_code}" != "" ]; then',
            source,
        )
        self.assertIn(
            "export_smoke_startup_failed_code=%s",
            source,
        )
        self.assertIn(
            "fail 'export_smoke_startup_failed'",
            source,
        )

        cases = (
            (
                "ready",
                '{"state":"migrating","error_code":null}',
                "200",
                "200",
                {"status": "0", "error": "", "calls": "1", "sleeps": "0"},
            ),
            (
                "failed",
                '{"state":"failed","error_code":'
                '"bootstrap_persistent_settings_invalid"}',
                "503",
                "200",
                {
                    "status": "2",
                    "error": "bootstrap_persistent_settings_invalid",
                    "calls": "2",
                    "sleeps": "0",
                },
            ),
            (
                "unsafe-error-code",
                '{"state":"failed","error_code":"unsafe-code"}',
                "503",
                "200",
                {
                    "status": "2",
                    "error": "migration_status_invalid",
                    "calls": "2",
                    "sleeps": "0",
                },
            ),
            (
                "migrating",
                '{"state":"migrating","error_code":null}',
                "503",
                "200",
                {"status": "1", "error": "", "calls": "2", "sleeps": "1"},
            ),
            (
                "status-unavailable",
                "{}",
                "503",
                "503",
                {"status": "1", "error": "", "calls": "2", "sleeps": "1"},
            ),
        )
        for name, payload, ready_code, migration_code, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(
                    self._run_wait_ready_harness(
                        payload,
                        ready_code=ready_code,
                        migration_code=migration_code,
                    ),
                    expected,
                )


if __name__ == "__main__":
    unittest.main()
