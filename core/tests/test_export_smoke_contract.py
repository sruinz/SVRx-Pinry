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


if __name__ == "__main__":
    unittest.main()
