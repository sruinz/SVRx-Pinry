import os
from pathlib import Path
import re
import shlex
import shutil
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


def _write_tar_race_wrapper(path, real_tar):
    path.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "for argument in \"$@\"; do\n"
        "    if [ \"$argument\" = \"-czf\" ]; then\n"
        "        test -d \"$PINRY_PACKAGE_ROOT/context\"\n"
        "        mkdir -p \"$PINRY_PACKAGE_ROOT/context/pinry-spa/src/race\"\n"
        "        : > \"$PINRY_PACKAGE_ROOT/.DS_Store\"\n"
        "        : > \"$PINRY_PACKAGE_ROOT/.DS_Store.backup\"\n"
        "        : > \"$PINRY_PACKAGE_ROOT/context/pinry-spa/src/race/.DS_Store\"\n"
        "        : > \"$PINRY_PACKAGE_ROOT/context/pinry-spa/src/race/.DS_Store.backup\"\n"
        "        break\n"
        "    fi\n"
        "done\n"
        "exec \"$PINRY_REAL_TAR\" \"$@\"\n"
    )
    path.chmod(0o700)


def _read_argv(path):
    return [
        value.decode("utf-8")
        for value in path.read_bytes().split(b"\0")
        if value
    ]


def _final_stage_copy_sources(dockerfile):
    final_stage = dockerfile.split("# Final image", 1)[1]
    sources = []
    for line in final_stage.splitlines():
        if not line.strip().startswith("COPY "):
            continue
        arguments = shlex.split(line.strip())
        if (
            arguments[1].startswith("--from=")
        ):
            continue
        sources.extend(arguments[1:-1])
    return sources


def _environment_values(source):
    values = {}
    for line in source.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            raise AssertionError("invalid environment assignment")
        name, value = stripped.split("=", 1)
        if not name or name in values:
            raise AssertionError("invalid environment variable name")
        values[name] = value
    return values


class SynologyPackageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.temporary_root = Path(temporary.name)
        self.output_root = self.temporary_root / "output"
        self.full_sha = _git_output("rev-parse", "HEAD")
        self.short_sha = _git_output("rev-parse", "--short=12", "HEAD")
        self.package_name = "pinry-custom"
        self.package_directory = self.output_root / self.package_name
        self.context_directory = self.package_directory / "context"
        self.archive_path = self.output_root / "{}-{}.tar.gz".format(
            self.package_name, self.short_sha
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
        self.assertTrue(
            self.package_directory.is_dir(),
            "canonical Synology upload folder was not created",
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

    def _clone_with_tracked_finder_metadata(self):
        repository = self.temporary_root / "fixture-repository"
        completed = subprocess.run(
            ["git", "clone", "--quiet", "--no-local", str(REPOSITORY_ROOT),
             str(repository)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(
            completed.returncode, 0, completed.stderr.decode("utf-8")
        )
        root_finder_file = repository / ".DS_Store"
        root_finder_file.write_text("tracked root Finder metadata")
        finder_file = repository / "pinry-spa/src/.DS_Store"
        finder_file.write_text("tracked Finder metadata")
        backup_file = repository / "pinry-spa/src/.DS_Store.backup"
        backup_file.write_text("keep this backup")
        sentinel = repository / "pinry-spa/src/package-sentinel.txt"
        sentinel.write_text("keep this sentinel")
        script = repository / "scripts/create_synology_output.sh"
        shutil.copy2(PACKAGE_SCRIPT, script)
        completed = subprocess.run(
            [
                "git",
                "-c",
                "user.name=Package Test",
                "-c",
                "user.email=package-test@example.invalid",
                "add",
                "--force",
                str(root_finder_file.relative_to(repository)),
                str(finder_file.relative_to(repository)),
                str(backup_file.relative_to(repository)),
                str(sentinel.relative_to(repository)),
                str(script.relative_to(repository)),
            ],
            cwd=str(repository),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(
            completed.returncode, 0, completed.stderr.decode("utf-8")
        )
        completed = subprocess.run(
            [
                "git",
                "-c",
                "user.name=Package Test",
                "-c",
                "user.email=package-test@example.invalid",
                "commit",
                "--quiet",
                "-m",
                "test fixture",
            ],
            cwd=str(repository),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(
            completed.returncode, 0, completed.stderr.decode("utf-8")
        )
        return repository

    def _run_packager_in(self, repository, output_root, environment):
        return subprocess.run(
            [
                "bash",
                str(repository / "scripts/create_synology_output.sh"),
                str(output_root),
            ],
            cwd=str(repository),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_packager_creates_minimal_build_context_from_head(self):
        self._create_package()

        self.assertEqual(
            {path.name for path in self.package_directory.iterdir()},
            {
                ".env.example",
                "BUILD_INFO",
                "build-image.sh",
                "context",
                "docker-compose.yml",
            },
        )
        required_context = (
            "Dockerfile.autobuild",
            ".dockerignore",
            "requirements.txt",
            "manage.py",
            "core/models.py",
            "django_images/models.py",
            "pinry/settings/docker.py",
            "pinry/settings/local_settings.example.py",
            "pinry_plugins/apps.py",
            "users/models.py",
            "pinry-spa/package.json",
            "pinry-spa/pnpm-lock.yaml",
            "pinry-spa/src/main.js",
            "docker/nginx/nginx.conf",
            "docker/scripts/start.sh",
        )
        for relative_path in required_context:
            with self.subTest(relative_path=relative_path):
                self.assertTrue(
                    (self.context_directory / relative_path).exists()
                )
        self.assertTrue(
            (self.package_directory / "build-image.sh").stat().st_mode
            & 0o111
        )
        self.assertEqual(
            (self.package_directory / "BUILD_INFO").read_text(),
            "source_commit={}\n"
            "default_image=pinry-custom:latest\n".format(self.full_sha),
        )
        self.assertFalse((self.package_directory / ".env").exists())
        self.assertEqual(
            _environment_values(
                (self.package_directory / ".env.example").read_text()
            ),
            {
                "PINRY_IMAGE": "pinry-custom:latest",
                "PINRY_CONTAINER_NAME": "pinry-custom",
                "PINRY_HTTP_PORT": "2048",
                "PINRY_DATA_PATH": "/volume1/docker/pinry-custom/data",
            },
        )
        compose = (
            self.package_directory / "docker-compose.yml"
        ).read_text()
        self.assertEqual(
            [line.strip() for line in compose.splitlines() if line.strip()],
            [
                'version: "3.8"',
                "services:",
                "pinry:",
                "image: ${PINRY_IMAGE}",
                "container_name: ${PINRY_CONTAINER_NAME}",
                "ports:",
                '- "${PINRY_HTTP_PORT}:80"',
                "volumes:",
                '- "${PINRY_DATA_PATH}:/data"',
                "restart: unless-stopped",
            ],
        )
        for unsafe_compose_value in (
            "build:",
            "privileged:",
            "network_mode:",
            ".:/pinry",
            "development",
            "poetry",
        ):
            with self.subTest(unsafe_compose_value=unsafe_compose_value):
                self.assertNotIn(unsafe_compose_value, compose)
        self.assertEqual(
            {path.name for path in self.context_directory.iterdir()},
            {
                ".dockerignore",
                "Dockerfile.autobuild",
                "requirements.txt",
                "manage.py",
                "core",
                "django_images",
                "pinry",
                "pinry_plugins",
                "users",
                "pinry-spa",
                "docker",
            },
        )
        self.assertEqual(
            {
                path.name
                for path in (self.context_directory / "docker").iterdir()
            },
            {"nginx", "scripts"},
        )
        for path in self.context_directory.rglob("*"):
            relative = path.relative_to(self.context_directory)
            with self.subTest(forbidden=str(relative)):
                self.assertNotIn("tests", relative.parts)
                self.assertFalse(path.name == "tests.py")
                self.assertFalse(path.name.startswith("test_"))
                self.assertNotIn(path.suffix.lower(), (".md", ".rst"))
                self.assertNotEqual(path.name, ".DS_Store")
        self.assertEqual(
            (self.context_directory / ".dockerignore").read_text(),
            "Dockerfile.autobuild\n"
            ".dockerignore\n"
            ".DS_Store\n"
            "**/.DS_Store\n",
        )
        for relative_path in (
            "pinry-spa/.editorconfig",
            "pinry-spa/.gitignore",
            "pinry/settings/development.py",
        ):
            with self.subTest(build_only_metadata=relative_path):
                self.assertFalse(
                    (self.context_directory / relative_path).exists()
                )

        self.assertTrue(self.archive_path.is_file())
        with tarfile.open(str(self.archive_path), "r:gz") as archive:
            names = archive.getnames()
        self.assertIn(
            "{}/build-image.sh".format(self.package_name), names
        )
        self.assertIn(
            "{}/.env.example".format(self.package_name), names
        )
        self.assertIn(
            "{}/docker-compose.yml".format(self.package_name), names
        )
        self.assertNotIn("{}/.env".format(self.package_name), names)
        self.assertIn(
            "{}/context/core/models.py".format(self.package_name), names
        )
        self.assertTrue(
            all(
                ".." not in Path(name).parts
                and ".git" not in Path(name).parts
                and ".DS_Store" not in Path(name).parts
                and Path(name).suffix.lower() not in (".md", ".rst")
                for name in names
            )
        )

    def test_packager_excludes_finder_metadata_from_context_and_archive(self):
        repository = self._clone_with_tracked_finder_metadata()
        output_root = self.temporary_root / "fixture-output"
        package_directory = output_root / self.package_name
        binary_directory = self.temporary_root / "tar-wrapper"
        binary_directory.mkdir()
        real_tar = shutil.which("tar")
        self.assertIsNotNone(real_tar)
        _write_tar_race_wrapper(binary_directory / "tar", real_tar)
        environment = os.environ.copy()
        environment["PATH"] = "{}{}{}".format(
            binary_directory,
            os.pathsep,
            environment.get("PATH", ""),
        )
        environment["PINRY_PACKAGE_ROOT"] = str(package_directory)
        environment["PINRY_REAL_TAR"] = real_tar

        completed = self._run_packager_in(
            repository, output_root, environment
        )

        self.assertEqual(
            completed.returncode, 0, completed.stderr.decode("utf-8")
        )
        context = package_directory / "context"
        self.assertFalse((context / "pinry-spa/src/.DS_Store").exists())
        self.assertTrue((context / "pinry-spa/src/.DS_Store.backup").is_file())
        self.assertTrue((context / "pinry-spa/src/package-sentinel.txt").is_file())
        archive_path = output_root / "pinry-custom-{}.tar.gz".format(
            subprocess.check_output(
                ["git", "rev-parse", "--short=12", "HEAD"],
                cwd=str(repository),
            ).decode("utf-8").strip()
        )
        with tarfile.open(str(archive_path), "r:gz") as archive:
            names = archive.getnames()
        self.assertFalse(
            any(Path(name).name == ".DS_Store" for name in names)
        )
        self.assertIn("pinry-custom/.DS_Store.backup", names)
        self.assertIn(
            "pinry-custom/context/pinry-spa/src/.DS_Store.backup", names
        )
        self.assertIn(
            "pinry-custom/context/pinry-spa/src/race/.DS_Store.backup",
            names,
        )
        self.assertIn(
            "pinry-custom/context/pinry-spa/src/package-sentinel.txt", names
        )

    def test_final_image_copies_only_runtime_application_paths(self):
        sources = _final_stage_copy_sources(
            (REPOSITORY_ROOT / "Dockerfile.autobuild").read_text()
        )

        self.assertEqual(
            sources,
            [
                "requirements.txt",
                "manage.py",
                "core",
                "django_images",
                "pinry",
                "pinry_plugins",
                "users",
                "docker/scripts",
            ],
        )

    def test_synology_dockerfile_uses_supported_bookworm_inputs(self):
        source = (REPOSITORY_ROOT / "Dockerfile.autobuild").read_text()

        self.assertEqual(
            re.findall(r"^FROM python:([^\s]+)", source, re.MULTILINE),
            ["3.9-slim-bookworm", "3.9-slim-bookworm"],
        )
        self.assertNotIn("buster", source)
        self.assertIn("libtiff-dev", source)
        self.assertNotIn("libtiff5-dev", source)
        self.assertNotIn("--install-option", source)
        self.assertNotIn("rcssmin==1.0.6", source)

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
                "pinry-custom:latest",
                ".",
            ],
        )
        self.assertEqual(
            Path(working_directory.read_text()).resolve(),
            self.context_directory.resolve(),
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
        (self.context_directory / "Dockerfile.autobuild").unlink()
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
