import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_SCRIPT = (
    REPOSITORY_ROOT / "docker/tests/fixtures/create_legacy_fixture.py"
)


class LegacyFixtureContractTests(unittest.TestCase):
    def test_legacy_md5_fixture_covers_every_root_and_orphan_sentinel(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            data_root = Path(temporary, "data")
            receipt_path = data_root / "fixture-receipt.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(FIXTURE_SCRIPT),
                    "create",
                    "--kind",
                    "legacy-md5",
                    "--data-root",
                    str(data_root),
                    "--count",
                    "3",
                    "--receipt",
                    str(receipt_path),
                ],
                cwd=str(REPOSITORY_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            self.assertEqual(
                completed.returncode, 0, completed.stderr.decode("utf-8")
            )
            receipt = json.loads(receipt_path.read_text("ascii"))
            expected_roots = list("0123456789abcdef")
            self.assertEqual(receipt["expected_direct_roots"], expected_roots)
            self.assertEqual(
                sorted(receipt["archive_root_identity"]), expected_roots
            )
            referenced_roots = {
                item["legacy_original_path"].split("/", 1)[0]
                for item in receipt["items"]
            }
            referenced_roots.update(
                derivative["legacy_path"].split("/", 1)[0]
                for item in receipt["items"]
                for derivative in item["derivatives"]
            )
            orphan = receipt["orphan_sentinel"]
            orphan_root = orphan["path"].split("/", 1)[0]
            self.assertNotIn(orphan_root, referenced_roots)
            sentinel_path = data_root / "static/media" / orphan["path"]
            self.assertTrue(sentinel_path.is_file())
            self.assertEqual(
                hashlib.sha256(sentinel_path.read_bytes()).hexdigest(),
                orphan["sha256"],
            )
            sentinel_stat = sentinel_path.stat()
            self.assertEqual(
                (sentinel_stat.st_dev, sentinel_stat.st_ino),
                (
                    orphan["source_identity"]["device"],
                    orphan["source_identity"]["inode"],
                ),
            )


if __name__ == "__main__":
    unittest.main()
