import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile

from django.test import SimpleTestCase
import mock

from django_images.services import startup_lock


class StartupLockTests(SimpleTestCase):
    def setUp(self):
        self.temporary_root = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_root.cleanup)
        self.data_root = Path(os.path.realpath(self.temporary_root.name))
        self.lock_path = self.data_root / ".svrx-pinry-startup.lock"

    def _assert_lock_error(self, code, callable_):
        with self.assertRaises(startup_lock.StartupLockError) as caught:
            callable_()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def _remove_lock_entry(self):
        if self.lock_path.is_symlink() or self.lock_path.is_file():
            self.lock_path.unlink()
        elif self.lock_path.is_dir():
            self.lock_path.rmdir()

    def test_new_lock_is_direct_child_with_exact_security_contract(self):
        acquired = startup_lock.acquire_startup_lock(str(self.data_root))
        self.addCleanup(acquired.close)

        file_stat = os.stat(str(self.lock_path), follow_symlinks=False)
        self.assertTrue(stat.S_ISREG(file_stat.st_mode))
        self.assertEqual(stat.S_IMODE(file_stat.st_mode), 0o600)
        self.assertEqual(file_stat.st_nlink, 1)
        self.assertEqual(file_stat.st_uid, os.geteuid())
        self.assertEqual(file_stat.st_gid, os.getegid())
        self.assertFalse(os.get_inheritable(acquired.fileno()))
        self.assertEqual(
            (os.fstat(acquired.fileno()).st_dev,
             os.fstat(acquired.fileno()).st_ino),
            (file_stat.st_dev, file_stat.st_ino),
        )

    def test_existing_valid_lock_is_reused_without_truncating(self):
        self.lock_path.write_bytes(b"persistent-lock-content")
        os.chmod(str(self.lock_path), 0o600)
        before = self.lock_path.stat()

        with startup_lock.acquire_startup_lock(str(self.data_root)):
            during = self.lock_path.stat()
            self.assertEqual(during.st_ino, before.st_ino)
            self.assertEqual(
                self.lock_path.read_bytes(), b"persistent-lock-content"
            )

    def test_second_process_is_rejected_without_blocking(self):
        project_root = str(Path(__file__).resolve().parent.parent)
        script = "\n".join((
            "import sys",
            "from django_images.services.startup_lock import (",
            "    StartupLockError, acquire_startup_lock,",
            ")",
            "try:",
            "    acquire_startup_lock(sys.argv[1])",
            "except StartupLockError as error:",
            "    print(error.code)",
            "else:",
            "    print('unexpected-success')",
        ))

        with startup_lock.acquire_startup_lock(str(self.data_root)):
            completed = subprocess.run(
                [sys.executable, "-c", script, str(self.data_root)],
                cwd=project_root,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                timeout=5,
            )

        self.assertEqual(completed.stdout.strip(), "startup_lock_busy")
        self.assertEqual(completed.stderr, "")

    def test_symlink_hardlink_directory_and_wrong_mode_are_rejected(self):
        outside = self.data_root.with_name(self.data_root.name + "-outside")
        outside.write_bytes(b"outside")
        self.addCleanup(lambda: outside.exists() and outside.unlink())

        for case in ("symlink", "hardlink", "directory", "mode"):
            with self.subTest(case=case):
                self._remove_lock_entry()
                second_link = self.data_root / "second-link"
                if second_link.exists():
                    second_link.unlink()
                if case == "symlink":
                    self.lock_path.symlink_to(outside)
                elif case == "directory":
                    self.lock_path.mkdir(mode=0o700)
                else:
                    self.lock_path.write_bytes(b"lock")
                    os.chmod(str(self.lock_path), 0o600)
                    if case == "hardlink":
                        os.link(str(self.lock_path), str(second_link))
                    else:
                        os.chmod(str(self.lock_path), 0o640)

                self._assert_lock_error(
                    "startup_lock_failed",
                    lambda: startup_lock.acquire_startup_lock(
                        str(self.data_root)
                    ),
                )
                self.assertEqual(outside.read_bytes(), b"outside")

    def test_lock_owner_must_match_acquiring_effective_identity(self):
        self.lock_path.write_bytes(b"lock")
        os.chmod(str(self.lock_path), 0o600)

        with mock.patch(
            "django_images.services.startup_lock.os.geteuid",
            return_value=os.geteuid() + 1,
        ):
            self._assert_lock_error(
                "startup_lock_failed",
                lambda: startup_lock.acquire_startup_lock(
                    str(self.data_root)
                ),
            )

        with mock.patch(
            "django_images.services.startup_lock.os.getegid",
            return_value=os.getegid() + 1,
        ):
            self._assert_lock_error(
                "startup_lock_failed",
                lambda: startup_lock.acquire_startup_lock(
                    str(self.data_root)
                ),
            )

    def test_replacing_named_lock_blocks_explicit_handoff(self):
        acquired = startup_lock.acquire_startup_lock(str(self.data_root))
        self.addCleanup(acquired.close)
        old_path = self.data_root / ".svrx-pinry-startup.lock-old"
        self.lock_path.rename(old_path)
        self.lock_path.write_bytes(b"replacement")
        os.chmod(str(self.lock_path), 0o600)

        self._assert_lock_error(
            "startup_lock_failed",
            lambda: acquired.set_inheritable(True),
        )
        self.assertFalse(os.get_inheritable(acquired.fileno()))

    def test_handoff_uses_owner_identity_captured_at_acquisition(self):
        acquired = startup_lock.acquire_startup_lock(str(self.data_root))
        self.addCleanup(acquired.close)

        with mock.patch(
            "django_images.services.startup_lock.os.geteuid",
            return_value=os.geteuid() + 1,
        ), mock.patch(
            "django_images.services.startup_lock.os.getegid",
            return_value=os.getegid() + 1,
        ):
            acquired.set_inheritable(True)

        self.assertTrue(os.get_inheritable(acquired.fileno()))
        acquired.set_inheritable(False)

    def test_exec_handoff_keeps_lock_until_child_exits(self):
        project_root = str(Path(__file__).resolve().parent.parent)
        child_script = "\n".join((
            "import os, sys",
            "descriptor = int(sys.argv[1])",
            "os.fstat(descriptor)",
            "print('held {}'.format(os.get_inheritable(descriptor)), flush=True)",
            "sys.stdin.readline()",
        ))
        holder_script = "\n".join((
            "import os, sys",
            "from django_images.services.startup_lock import acquire_startup_lock",
            "held = acquire_startup_lock(sys.argv[1])",
            "held.set_inheritable(True)",
            "os.execv(sys.executable, [sys.executable, '-c', sys.argv[2], str(held.fileno())])",
        ))
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                holder_script,
                str(self.data_root),
                child_script,
            ],
            cwd=project_root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        self.addCleanup(
            lambda: process.poll() is None and process.kill()
        )
        self.assertEqual(process.stdout.readline().strip(), "held True")

        self._assert_lock_error(
            "startup_lock_busy",
            lambda: startup_lock.acquire_startup_lock(str(self.data_root)),
        )

        process.stdin.write("release\n")
        process.stdin.flush()
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "")
        with startup_lock.acquire_startup_lock(str(self.data_root)):
            pass

    def test_close_releases_lock_and_descriptors(self):
        acquired = startup_lock.acquire_startup_lock(str(self.data_root))
        descriptor = acquired.fileno()
        acquired.close()

        with self.assertRaises(OSError):
            os.fstat(descriptor)
        with startup_lock.acquire_startup_lock(str(self.data_root)):
            pass
