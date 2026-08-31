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


if __name__ == "__main__":
    unittest.main()
