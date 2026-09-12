"""운영 설정의 인코딩 복구가 설정값을 바꾸지 않는지 검증한다."""
import importlib.util
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[2] / 'docker/scripts/normalize_persistent_file.py'
spec = importlib.util.spec_from_file_location('settings_normalizer', SCRIPT)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class SettingsEncodingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / 'local_settings.py'

    def run_normalizer(self):
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(self.root), str(self.path), 'settings'],
            capture_output=True,
        )

    def test_undeclared_korean_comments_are_backed_up_and_repaired_once(self):
        text = "import os\r\nSECRET_KEY='sentinel-secret'\r\n# 설정 주석\r\nPUBLIC=True\r\n"
        original = text.encode('cp949')
        self.path.write_bytes(original)
        result = self.run_normalizer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.path.read_bytes(), text.encode('utf-8'))
        compile(self.path.read_bytes(), '<settings>', 'exec')
        backups = list(self.root.glob('local_settings.py.encoding-*.bak'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)
        self.assertNotIn(b'sentinel-secret', result.stdout + result.stderr)
        self.assertEqual(self.run_normalizer().returncode, 0)
        self.assertEqual(list(self.root.glob('local_settings.py.encoding-*.bak')), backups)

    def test_valid_utf8_and_declared_cp949_are_preserved_without_backup(self):
        for original in (
            '# 정상 설정\nPUBLIC=True\n'.encode('utf-8'),
            '# coding: cp949\n# 기존 설정\nPUBLIC=True\n'.encode('cp949'),
        ):
            with self.subTest(original=original):
                self.path.write_bytes(original)
                self.assertEqual(self.run_normalizer().returncode, 0)
                self.assertEqual(self.path.read_bytes(), original)
                self.assertEqual(list(self.root.glob('*.bak')), [])

    def test_uncertain_encoding_or_non_ascii_values_are_not_changed(self):
        for original in (
            "NAME='한글 설정값'\n".encode('cp949'),
            "NAME='''\n# 문자열이지 주석이 아님\n'''\n".encode('cp949'),
            b'# unknown \xff\nPUBLIC=True\n',
            b'# coding: unknown-encoding\nPUBLIC=True\n',
        ):
            with self.subTest(original=original):
                self.path.write_bytes(original)
                result = self.run_normalizer()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stderr.strip(), b'local_settings_encoding_invalid')
                self.assertEqual(self.path.read_bytes(), original)
                self.assertEqual(list(self.root.glob('*.bak')), [])

    def test_syntax_error_is_safe_and_source_is_not_executed(self):
        self.path.write_bytes(b"SECRET_KEY='do-not-output'\nif:\n")
        result = self.run_normalizer()
        self.assertEqual(result.stderr.strip(), b'local_settings_syntax_invalid')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(b'do-not-output', result.stdout + result.stderr)
        self.path.write_bytes(b"raise RuntimeError('must-not-execute')\n")
        self.assertEqual(self.run_normalizer().returncode, 0)

    def test_backup_failure_leaves_original_unchanged(self):
        original = '# 한글 주석\nPUBLIC=True\n'.encode('cp949')
        self.path.write_bytes(original)
        with mock.patch.object(helper.os, 'fsync', side_effect=OSError):
            with self.assertRaises(OSError):
                helper.normalize(str(self.root), str(self.path), 'settings')
        self.assertEqual(self.path.read_bytes(), original)

    def test_expanded_utf8_over_limit_preserves_original(self):
        original = ('# ' + '가' * 400000 + '\nPUBLIC=True\n').encode('cp949')
        self.path.write_bytes(original)
        result = self.run_normalizer()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.root.glob('*.bak')), [])
