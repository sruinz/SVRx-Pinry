import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_SCRIPT = REPOSITORY_ROOT / "scripts/create_synology_output.sh"


def _git_output(*arguments):
    return subprocess.check_output(
        ["git"] + list(arguments), cwd=str(REPOSITORY_ROOT)
    ).decode("utf-8").strip()


def _write_fake_docker(path):
    path.write_text(
        "#!/bin/sh\n"
        ": > \"$PINRY_DOCKER_CAPTURE\"\n"
        "for argument in \"$@\"; do\n"
        "    printf '%s\\0' \"$argument\" >> \"$PINRY_DOCKER_CAPTURE\"\n"
        "done\n"
        "printf '%s' \"$PWD\" > \"$PINRY_DOCKER_CWD\"\n"
    )
    path.chmod(0o700)


def _read_argv(path):
    return [
        value.decode("utf-8")
        for value in path.read_bytes().split(b"\0")
        if value
    ]


class SynologyPackageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.temporary_root = Path(temporary.name)
        self.output_root = self.temporary_root / "output"
        self.full_sha = _git_output("rev-parse", "HEAD")
        self.short_sha = _git_output("rev-parse", "--short=12", "HEAD")
        self.package_name = "pinry-custom-{}".format(self.short_sha)
        self.package_directory = self.output_root / self.package_name
        self.archive_path = self.output_root / "{}.tar.gz".format(
            self.package_name
        )

    def _run_packager(self):
        return subprocess.run(
            ["bash", str(PACKAGE_SCRIPT), str(self.output_root)],
            cwd=str(REPOSITORY_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def _create_package(self):
        completed = self._run_packager()
        self.assertEqual(
            completed.returncode, 0, completed.stderr.decode("utf-8")
        )

    def _docker_environment(self):
        binary_directory = self.temporary_root / "bin"
        binary_directory.mkdir(exist_ok=True)
        capture = self.temporary_root / "docker-argv.bin"
        working_directory = self.temporary_root / "docker-cwd.txt"
        _write_fake_docker(binary_directory / "docker")
        environment = os.environ.copy()
        environment["PATH"] = "{}{}{}".format(
            binary_directory,
            os.pathsep,
            environment.get("PATH", ""),
        )
        environment["PINRY_DOCKER_CAPTURE"] = str(capture)
        environment["PINRY_DOCKER_CWD"] = str(working_directory)
        return environment, capture, working_directory

    def test_packager_creates_upload_folder_and_archive_from_head(self):
        self._create_package()

        required = (
            "Dockerfile.autobuild",
            "requirements.txt",
            "manage.py",
            "pinry",
            "docker/scripts/start.sh",
            "build-image.sh",
            "README_KO.md",
            "BUILD_INFO",
        )
        for relative_path in required:
            with self.subTest(relative_path=relative_path):
                self.assertTrue(
                    (self.package_directory / relative_path).exists()
                )
        self.assertTrue(
            (self.package_directory / "build-image.sh").stat().st_mode
            & 0o111
        )
        self.assertEqual(
            (self.package_directory / "BUILD_INFO").read_text(),
            "source_commit={}\n"
            "default_image=pinry-custom:{}\n".format(
                self.full_sha, self.short_sha
            ),
        )
        for relative_path in (
            ".git",
            ".DS_Store",
            ".venv",
            "media",
            "db.sqlite3",
        ):
            with self.subTest(excluded=relative_path):
                self.assertFalse(
                    (self.package_directory / relative_path).exists()
                )

        self.assertTrue(self.archive_path.is_file())
        with tarfile.open(str(self.archive_path), "r:gz") as archive:
            names = archive.getnames()
        self.assertIn(
            "{}/build-image.sh".format(self.package_name), names
        )
        self.assertTrue(
            all(
                ".." not in Path(name).parts
                and ".git" not in Path(name).parts
                and ".DS_Store" not in Path(name).parts
                for name in names
            )
        )

    def test_packager_refuses_to_replace_existing_output(self):
        self._create_package()
        sentinel = self.package_directory / "keep-user-file"
        sentinel.write_text("preserve")
        archive_before = self.archive_path.read_bytes()

        completed = self._run_packager()

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(b"output_already_exists", completed.stderr)
        self.assertEqual(sentinel.read_text(), "preserve")
        self.assertEqual(self.archive_path.read_bytes(), archive_before)

    def test_default_build_uses_native_docker_and_packaged_context(self):
        self._create_package()
        environment, capture, working_directory = (
            self._docker_environment()
        )

        completed = subprocess.run(
            ["sh", str(self.package_directory / "build-image.sh")],
            cwd=str(self.temporary_root),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual(
            completed.returncode, 0, completed.stderr.decode("utf-8")
        )
        self.assertEqual(
            _read_argv(capture),
            [
                "build",
                "--pull",
                "--file",
                "Dockerfile.autobuild",
                "--tag",
                "pinry-custom:{}".format(self.short_sha),
                ".",
            ],
        )
        self.assertEqual(
            Path(working_directory.read_text()).resolve(),
            self.package_directory.resolve(),
        )

    def test_build_accepts_one_custom_image_tag(self):
        self._create_package()
        environment, capture, _working_directory = (
            self._docker_environment()
        )

        completed = subprocess.run(
            [
                "sh",
                str(self.package_directory / "build-image.sh"),
                "registry.local/pinry-custom:nas",
            ],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual(
            completed.returncode, 0, completed.stderr.decode("utf-8")
        )
        arguments = _read_argv(capture)
        self.assertEqual(
            arguments[arguments.index("--tag") + 1],
            "registry.local/pinry-custom:nas",
        )

    def test_build_rejects_missing_input_before_invoking_docker(self):
        self._create_package()
        (self.package_directory / "Dockerfile.autobuild").unlink()
        environment, capture, _working_directory = (
            self._docker_environment()
        )

        completed = subprocess.run(
            ["sh", str(self.package_directory / "build-image.sh")],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(b"missing_build_input=Dockerfile.autobuild", completed.stderr)
        self.assertFalse(capture.exists())

    def test_generated_synology_output_is_ignored_by_git(self):
        completed = subprocess.run(
            [
                "git",
                "check-ignore",
                "--quiet",
                "output/synology/probe",
            ],
            cwd=str(REPOSITORY_ROOT),
        )

        self.assertEqual(completed.returncode, 0)
