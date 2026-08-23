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
TASK_PRODUCTION_PATHS = (
    ".gitignore",
    ".dockerignore",
    "Dockerfile.autobuild",
    "LICENSE.md",
    "NOTICE.md",
    "UPSTREAM.md",
    "docker/scripts/start.sh",
    "scripts/create_synology_output.sh",
    "deploy/synology/build-image.sh",
    "deploy/synology/README_KO.md",
)
PACKAGE_CONTROL_PATHS = (
    "scripts/create_synology_output.sh",
    "deploy/synology/build-image.sh",
    "deploy/synology/docker-compose.synology.yml",
    "deploy/synology/.env.example",
)


def _git_output(repository, *arguments):
    return subprocess.check_output(
        ["git"] + list(arguments), cwd=str(repository)
    ).decode("utf-8").strip()


def _write_fake_docker(path):
    path.write_text(
        "#!/bin/sh\n"
        "printf '1\\n' >> \"${PINRY_DOCKER_CAPTURE}.calls\"\n"
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
        "create=0\n"
        "no_xattrs=0\n"
        "package_root=\n"
        "package_name=\n"
        "previous=\n"
        "for argument in \"$@\"; do\n"
        "    if [ \"$previous\" = \"-czf\" ]; then\n"
        "        previous=\n"
        "        continue\n"
        "    fi\n"
        "    if [ \"$previous\" = \"-C\" ]; then\n"
        "        package_root=$argument\n"
        "        previous=\n"
        "        continue\n"
        "    fi\n"
        "    if [ \"$previous\" = \"--exclude\" ]; then\n"
        "        previous=\n"
        "        continue\n"
        "    fi\n"
        "    case \"$argument\" in\n"
        "        -czf)\n"
        "            create=1\n"
        "            previous=$argument\n"
        "            ;;\n"
        "        -C)\n"
        "            previous=$argument\n"
        "            ;;\n"
        "        --exclude)\n"
        "            previous=$argument\n"
        "            ;;\n"
        "        --exclude=*)\n"
        "            ;;\n"
        "        --no-xattrs)\n"
        "            no_xattrs=1\n"
        "            ;;\n"
        "        *)\n"
        "            package_name=$argument\n"
        "            ;;\n"
        "    esac\n"
        "done\n"
        "if [ \"$create\" = 1 ]; then\n"
        "    test \"${COPYFILE_DISABLE:-}\" = 1\n"
        "    test \"$no_xattrs\" = 1\n"
        "    package_root=$package_root/$package_name\n"
        "    test -d \"$package_root/context\"\n"
        "    mkdir -p \"$package_root/context/pinry-spa/src/race\"\n"
        "    : > \"$package_root/.DS_Store\"\n"
        "    : > \"$package_root/.DS_Store.backup\"\n"
        "    : > \"$package_root/._BUILD_INFO\"\n"
        "    : > \"$package_root/BUILD_INFO._backup\"\n"
        "    : > \"$package_root/context/pinry-spa/src/race/.DS_Store\"\n"
        "    : > \"$package_root/context/pinry-spa/src/race/.DS_Store.backup\"\n"
        "    : > \"$package_root/context/pinry-spa/src/race/._asset.js\"\n"
        "    : > \"$package_root/context/pinry-spa/src/race/asset._preview.js\"\n"
        "fi\n"
        "exec \"$PINRY_REAL_TAR\" \"$@\"\n"
    )
    path.chmod(0o700)


def _write_final_tar_failure_wrapper(path):
    path.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "for argument in \"$@\"; do\n"
        "    if [ \"$argument\" = \"-czf\" ]; then\n"
        "        exit 42\n"
        "    fi\n"
        "done\n"
        "exec \"$PINRY_REAL_TAR\" \"$@\"\n"
    )
    path.chmod(0o700)


def _write_archive_publish_failure_wrapper(path):
    path.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "for argument in \"$@\"; do\n"
        "    case \"$argument\" in\n"
        "        */.pinry-custom.archive.*)\n"
        "            exit 43\n"
        "            ;;\n"
        "    esac\n"
        "done\n"
        "exec \"$PINRY_REAL_MV\" \"$@\"\n"
    )
    path.chmod(0o700)


def _write_head_move_git_wrapper(path):
    path.write_text(
        "#!/bin/sh\n"
        "set -u\n"
        "if [ \"${1:-}\" = archive ] "
        "&& [ ! -e \"$PINRY_HEAD_MOVE_MARKER\" ]; then\n"
        "    : > \"$PINRY_HEAD_MOVE_MARKER\"\n"
        "    \"$PINRY_REAL_GIT\" checkout --quiet "
        "\"$PINRY_HEAD_MOVE_TARGET\" || exit $?\n"
        "fi\n"
        "exec \"$PINRY_REAL_GIT\" \"$@\"\n"
    )
    path.chmod(0o700)


def _read_argv(path):
    return [
        value.decode("utf-8")
        for value in path.read_bytes().split(b"\0")
        if value
    ]


def _docker_call_count(capture):
    calls = Path(str(capture) + ".calls")
    if not calls.exists():
        return 0
    return len(calls.read_text().splitlines())


def _find_range_vulnerable_utf8_locale():
    completed = subprocess.run(
        ["locale", "-a"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        return None
    for locale_name in completed.stdout.decode("utf-8").splitlines():
        normalized = locale_name.lower().replace("-", "")
        if "utf8" not in normalized:
            continue
        environment = os.environ.copy()
        environment["LC_ALL"] = locale_name
        environment["PINRY_NONHEX_VALUE"] = "\u00e9" * 40
        probe = subprocess.run(
            [
                "/bin/sh",
                "-c",
                "value=$PINRY_NONHEX_VALUE; "
                "case \"$value\" in "
                "*[!0-9a-f]*) exit 1;; *) exit 0;; esac",
            ],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if probe.returncode == 0:
            return locale_name
    return None


def _write_invalid_commit_git_wrapper(path):
    path.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "if [ \"${1:-}\" = rev-parse ] "
        "&& [ \"${2:-}\" = --verify ] "
        "&& [ \"${3:-}\" = 'HEAD^{commit}' ]; then\n"
        "    printf '%s\\n' \"$PINRY_INVALID_COMMIT\"\n"
        "    exit 0\n"
        "fi\n"
        "exec \"$PINRY_REAL_GIT\" \"$@\"\n"
    )
    path.chmod(0o700)


def _git_blob(repository, commit, relative_path):
    return subprocess.check_output(
        ["git", "show", "{}:{}".format(commit, relative_path)],
        cwd=str(repository),
    )


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
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fixture_temporary = tempfile.TemporaryDirectory()
        cls.fixture_repository = (
            Path(cls.fixture_temporary.name) / "fixture-repository"
        )
        completed = subprocess.run(
            [
                "git",
                "clone",
                "--quiet",
                "--no-local",
                str(REPOSITORY_ROOT),
                str(cls.fixture_repository),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if completed.returncode != 0:
            raise AssertionError(completed.stderr.decode("utf-8"))
        for relative_path in TASK_PRODUCTION_PATHS:
            destination = cls.fixture_repository / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPOSITORY_ROOT / relative_path, destination)
        completed = subprocess.run(
            ["git", "add"] + list(TASK_PRODUCTION_PATHS),
            cwd=str(cls.fixture_repository),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if completed.returncode != 0:
            raise AssertionError(completed.stderr.decode("utf-8"))
        if subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=str(cls.fixture_repository),
        ).returncode != 0:
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
                    "task production fixture",
                ],
                cwd=str(cls.fixture_repository),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if completed.returncode != 0:
                raise AssertionError(completed.stderr.decode("utf-8"))

    @classmethod
    def tearDownClass(cls):
        cls.fixture_temporary.cleanup()
        super().tearDownClass()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.temporary_root = Path(temporary.name)
        self.repository_root = self.fixture_repository
        self.package_script = (
            self.repository_root / "scripts/create_synology_output.sh"
        )
        self.output_root = self.temporary_root / "output"
        self.full_sha = _git_output(self.repository_root, "rev-parse", "HEAD")
        self.short_sha = _git_output(
            self.repository_root, "rev-parse", "--short=12", "HEAD"
        )
        self.package_name = "pinry-custom"
        self.package_directory = self.output_root / self.package_name
        self.context_directory = self.package_directory / "context"
        self.archive_path = self.output_root / "{}-{}.tar.gz".format(
            self.package_name, self.short_sha
        )

    def _run_packager(self, environment=None):
        return subprocess.run(
            ["bash", str(self.package_script), str(self.output_root)],
            cwd=str(self.repository_root),
            env=environment,
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

    def _tar_environment(self, wrapper):
        environment = os.environ.copy()
        real_tar = shutil.which("tar")
        self.assertIsNotNone(real_tar)
        environment["PATH"] = "{}{}{}".format(
            wrapper.parent,
            os.pathsep,
            environment.get("PATH", ""),
        )
        environment["PINRY_REAL_TAR"] = real_tar
        return environment

    def _move_environment(self, wrapper):
        environment = os.environ.copy()
        real_mv = shutil.which("mv")
        self.assertIsNotNone(real_mv)
        environment["PATH"] = "{}{}{}".format(
            wrapper.parent,
            os.pathsep,
            environment.get("PATH", ""),
        )
        environment["PINRY_REAL_MV"] = real_mv
        return environment

    def _clone_repository(self, name):
        repository = self.temporary_root / name
        completed = subprocess.run(
            [
                "git",
                "clone",
                "--quiet",
                "--no-local",
                str(self.repository_root),
                str(repository),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(
            completed.returncode, 0, completed.stderr.decode("utf-8")
        )
        return repository

    def _clone_with_tracked_finder_metadata(self):
        repository = self._clone_repository("finder-fixture-repository")
        root_finder_file = repository / ".DS_Store"
        root_finder_file.write_text("tracked root Finder metadata")
        finder_file = repository / "pinry-spa/src/.DS_Store"
        finder_file.write_text("tracked Finder metadata")
        backup_file = repository / "pinry-spa/src/.DS_Store.backup"
        backup_file.write_text("keep this backup")
        appledouble_file = repository / "pinry-spa/src/._package-sentinel.txt"
        appledouble_file.write_text("tracked AppleDouble metadata")
        appledouble_like_file = (
            repository / "pinry-spa/src/package._sentinel.txt"
        )
        appledouble_like_file.write_text("keep this normal file")
        sentinel = repository / "pinry-spa/src/package-sentinel.txt"
        sentinel.write_text("keep this sentinel")
        script = repository / "scripts/create_synology_output.sh"
        shutil.copy2(self.package_script, script)
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
                str(appledouble_file.relative_to(repository)),
                str(appledouble_like_file.relative_to(repository)),
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

    def _run_packager_in(self, repository, output_root=None, environment=None):
        command = [
            "bash",
            str(repository / "scripts/create_synology_output.sh"),
        ]
        if output_root is not None:
            command.append(str(output_root))
        return subprocess.run(
            command,
            cwd=str(repository),
            env=environment or os.environ.copy(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_default_output_is_shared_by_canonical_and_linked_worktree(self):
        """기본 산출물은 같은 커밋의 모든 checkout에서 하나여야 한다."""
        default_output = (
            self.repository_root.parent
            / "output"
            / "svrx-pinry-server-{}".format(self.short_sha)
        )
        self.addCleanup(shutil.rmtree, default_output, ignore_errors=True)
        expected_package = default_output / self.package_name
        expected_archive = default_output / "{}-{}.tar.gz".format(
            self.package_name, self.short_sha
        )

        canonical = self._run_packager_in(self.repository_root)

        self.assertEqual(canonical.returncode, 0, canonical.stderr.decode("utf-8"))
        self.assertTrue(expected_package.is_dir())
        self.assertTrue(expected_archive.is_file())

        linked_worktree = self.temporary_root / "linked-worktree"
        completed = subprocess.run(
            [
                "git",
                "worktree",
                "add",
                "--quiet",
                "--detach",
                str(linked_worktree),
                self.full_sha,
            ],
            cwd=str(self.repository_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8"))
        self.addCleanup(
            subprocess.run,
            ["git", "worktree", "remove", "--force", str(linked_worktree)],
            cwd=str(self.repository_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        collision = self._run_packager_in(linked_worktree)

        self.assertNotEqual(collision.returncode, 0)
        self.assertIn(b"output_already_exists=", collision.stderr)
        self.assertTrue(expected_package.is_dir())
        self.assertTrue(expected_archive.is_file())

    def test_default_output_refuses_existing_commit_destination(self):
        default_output = (
            self.repository_root.parent
            / "output"
            / "svrx-pinry-server-{}".format(self.short_sha)
        )
        default_output.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, default_output, ignore_errors=True)
        sentinel = default_output / "keep-user-file"
        sentinel.write_text("preserve")

        completed = self._run_packager_in(self.repository_root)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(b"output_already_exists=", completed.stderr)
        self.assertEqual(sentinel.read_text(), "preserve")
        self.assertFalse((default_output / self.package_name).exists())

    def test_explicit_output_directory_overrides_shared_default(self):
        explicit_output = self.temporary_root / "explicit-output"

        completed = self._run_packager_in(
            self.repository_root, explicit_output
        )

        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8"))
        self.assertTrue((explicit_output / self.package_name).is_dir())
        self.assertTrue(
            (
                explicit_output
                / "{}-{}.tar.gz".format(self.package_name, self.short_sha)
            ).is_file()
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
            "LICENSE.md",
            "NOTICE.md",
            "UPSTREAM.md",
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
                "LICENSE.md",
                "NOTICE.md",
                "UPSTREAM.md",
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
                if path.name not in {
                    "LICENSE.md",
                    "NOTICE.md",
                    "UPSTREAM.md",
                }:
                    self.assertNotIn(path.suffix.lower(), (".md", ".rst"))
                self.assertNotEqual(path.name, ".DS_Store")
        self.assertEqual(
            (self.context_directory / ".dockerignore").read_text(),
            "Dockerfile.autobuild\n"
            ".dockerignore\n"
            ".DS_Store\n"
            "**/.DS_Store\n"
            "._*\n"
            "**/._*\n",
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
                and (
                    Path(name).name
                    in {"LICENSE.md", "NOTICE.md", "UPSTREAM.md"}
                    or Path(name).suffix.lower() not in (".md", ".rst")
                )
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
        environment.pop("COPYFILE_DISABLE", None)
        environment["PATH"] = "{}{}{}".format(
            binary_directory,
            os.pathsep,
            environment.get("PATH", ""),
        )
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
        self.assertFalse(
            (context / "pinry-spa/src/._package-sentinel.txt").exists()
        )
        self.assertTrue(
            (context / "pinry-spa/src/package._sentinel.txt").is_file()
        )
        self.assertTrue((context / "pinry-spa/src/package-sentinel.txt").is_file())
        archive_path = output_root / "pinry-custom-{}.tar.gz".format(
            subprocess.check_output(
                ["git", "rev-parse", "--short=12", "HEAD"],
                cwd=str(repository),
            ).decode("utf-8").strip()
        )
        with tarfile.open(str(archive_path), "r:gz") as archive:
            members = archive.getmembers()
        names = [member.name for member in members]
        self.assertFalse(
            any(Path(name).name == ".DS_Store" for name in names)
        )
        self.assertFalse(
            any(Path(name).name.startswith("._") for name in names)
        )
        self.assertFalse(
            any(
                ".xattr." in key.lower()
                for member in members
                for key in member.pax_headers
            )
        )
        self.assertIn("pinry-custom/.DS_Store.backup", names)
        self.assertIn(
            "pinry-custom/context/pinry-spa/src/.DS_Store.backup", names
        )
        self.assertIn(
            "pinry-custom/context/pinry-spa/src/race/.DS_Store.backup",
            names,
        )
        self.assertIn("pinry-custom/BUILD_INFO._backup", names)
        self.assertIn(
            "pinry-custom/context/pinry-spa/src/race/asset._preview.js",
            names,
        )
        self.assertIn(
            "pinry-custom/context/pinry-spa/src/package._sentinel.txt",
            names,
        )
        self.assertIn(
            "pinry-custom/context/pinry-spa/src/package-sentinel.txt", names
        )

    def test_packager_cleans_failed_publish_and_allows_immediate_retry(self):
        self.output_root.mkdir()
        sentinel = self.output_root / "unrelated-sentinel"
        sentinel.write_text("preserve")
        binary_directory = self.temporary_root / "failing-tar"
        binary_directory.mkdir()
        wrapper = binary_directory / "tar"
        _write_final_tar_failure_wrapper(wrapper)

        failed = self._run_packager(self._tar_environment(wrapper))

        self.assertEqual(failed.returncode, 42)
        self.assertFalse(self.package_directory.exists())
        self.assertFalse(self.archive_path.exists())
        self.assertEqual(sentinel.read_text(), "preserve")
        self.assertEqual(
            {
                path.name
                for path in self.output_root.iterdir()
                if path.name.startswith(".pinry-custom.tmp.")
                or path.name.startswith(".pinry-custom.archive.")
            },
            set(),
        )

        self._create_package()

        self.assertEqual(
            (self.package_directory / "BUILD_INFO").read_text(),
            "source_commit={}\n"
            "default_image=pinry-custom:latest\n".format(self.full_sha),
        )
        self.assertTrue((self.context_directory / "core/models.py").is_file())
        self.assertTrue(self.archive_path.is_file())
        self.assertEqual(sentinel.read_text(), "preserve")
        self.assertEqual(
            {
                path.name
                for path in self.output_root.iterdir()
                if path.name.startswith(".pinry-custom.tmp.")
                or path.name.startswith(".pinry-custom.archive.")
            },
            set(),
        )

    def test_packager_rolls_back_directory_when_archive_publish_fails(self):
        self.output_root.mkdir()
        sentinel = self.output_root / "unrelated-sentinel"
        sentinel.write_text("preserve")
        binary_directory = self.temporary_root / "failing-mv"
        binary_directory.mkdir()
        wrapper = binary_directory / "mv"
        _write_archive_publish_failure_wrapper(wrapper)

        failed = self._run_packager(self._move_environment(wrapper))

        self.assertEqual(failed.returncode, 43)
        self.assertFalse(self.package_directory.exists())
        self.assertFalse(self.archive_path.exists())
        self.assertEqual(sentinel.read_text(), "preserve")
        self.assertEqual(
            {
                path.name
                for path in self.output_root.iterdir()
                if path.name.startswith(".pinry-custom.tmp.")
                or path.name.startswith(".pinry-custom.archive.")
            },
            set(),
        )

        self._create_package()

        self.assertTrue(self.package_directory.is_dir())
        self.assertTrue(self.archive_path.is_file())
        self.assertEqual(sentinel.read_text(), "preserve")

    def test_final_image_copies_only_runtime_application_paths(self):
        sources = _final_stage_copy_sources(
            (self.repository_root / "Dockerfile.autobuild").read_text()
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
                "LICENSE.md",
                "NOTICE.md",
                "UPSTREAM.md",
            ],
        )

    def test_root_docker_context_keeps_required_license_files(self):
        ignored = {
            line.strip()
            for line in (REPOSITORY_ROOT / ".dockerignore")
            .read_text()
            .splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }

        self.assertTrue(
            {"LICENSE.md", "NOTICE.md", "UPSTREAM.md"}.isdisjoint(ignored)
        )

    def test_synology_dockerfile_uses_supported_bookworm_inputs(self):
        source = (
            self.repository_root / "Dockerfile.autobuild"
        ).read_text()

        self.assertEqual(
            re.findall(r"^FROM python:([^\s]+)", source, re.MULTILINE),
            ["3.9-slim-bookworm", "3.9-slim-bookworm"],
        )
        self.assertNotIn("buster", source)
        self.assertIn("libtiff-dev", source)
        self.assertNotIn("libtiff5-dev", source)
        self.assertNotIn("--install-option", source)
        self.assertNotIn("rcssmin==1.0.6", source)

    def test_final_image_exposes_exact_source_commit_build_contract(self):
        source = (
            self.repository_root / "Dockerfile.autobuild"
        ).read_text()
        stage_starts = list(re.finditer(r"^FROM\s+", source, re.MULTILINE))
        self.assertTrue(stage_starts)
        final_stage = source[stage_starts[-1].start():]
        source_commit_directives = [
            line.strip()
            for line in final_stage.splitlines()
            if re.match(r"^(ARG|ENV)\s+", line)
            and "PINRY_SOURCE_COMMIT" in line
        ]

        self.assertEqual(
            source_commit_directives,
            [
                "ARG PINRY_SOURCE_COMMIT=development",
                "ENV PINRY_SOURCE_COMMIT=${PINRY_SOURCE_COMMIT}",
            ],
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

    def test_packager_root_controls_are_exact_blobs_from_source_commit(self):
        self._create_package()
        packaged_controls = {
            "build-image.sh": "deploy/synology/build-image.sh",
            "docker-compose.yml": (
                "deploy/synology/docker-compose.synology.yml"
            ),
            ".env.example": "deploy/synology/.env.example",
        }

        for packaged_name, tracked_path in packaged_controls.items():
            with self.subTest(packaged_name=packaged_name):
                self.assertEqual(
                    (self.package_directory / packaged_name).read_bytes(),
                    _git_blob(
                        self.repository_root,
                        self.full_sha,
                        tracked_path,
                    ),
                )

    def test_packager_rejects_dirty_tracked_control_before_output(self):
        for index, relative_path in enumerate(PACKAGE_CONTROL_PATHS):
            with self.subTest(relative_path=relative_path):
                repository = self._clone_repository(
                    "dirty-control-{}".format(index)
                )
                dirty_path = repository / relative_path
                dirty_path.write_bytes(
                    dirty_path.read_bytes() + b"\n# dirty control\n"
                )
                output_root = self.temporary_root / "dirty-output-{}".format(
                    index
                )

                completed = self._run_packager_in(
                    repository, output_root, os.environ.copy()
                )

                self.assertNotEqual(completed.returncode, 0)
                self.assertIn(
                    b"tracked_package_control_mismatch",
                    completed.stderr,
                )
                self.assertFalse(output_root.exists())
                self.assertFalse((output_root / self.package_name).exists())

    def test_packager_rejects_staged_only_control_before_output(self):
        repository = self._clone_repository("staged-control")
        relative_path = "deploy/synology/build-image.sh"
        dirty_path = repository / relative_path
        dirty_path.write_bytes(
            dirty_path.read_bytes() + b"\n# staged control\n"
        )
        completed = subprocess.run(
            ["git", "add", relative_path],
            cwd=str(repository),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(
            completed.returncode, 0, completed.stderr.decode("utf-8")
        )
        dirty_path.write_bytes(
            _git_blob(repository, self.full_sha, relative_path)
        )
        self.assertEqual(
            subprocess.run(
                [
                    "git",
                    "diff",
                    "--quiet",
                    self.full_sha,
                    "--",
                    relative_path,
                ],
                cwd=str(repository),
            ).returncode,
            0,
        )
        self.assertNotEqual(
            subprocess.run(
                ["git", "diff", "--cached", "--quiet", "--", relative_path],
                cwd=str(repository),
            ).returncode,
            0,
        )
        output_root = self.temporary_root / "staged-output"

        completed = self._run_packager_in(
            repository, output_root, os.environ.copy()
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(
            b"tracked_package_control_mismatch", completed.stderr
        )
        self.assertFalse(output_root.exists())

    def test_packager_ignores_untracked_user_metadata_and_output(self):
        repository = self._clone_repository("untracked-user-files")
        finder_metadata = repository / ".DS_Store"
        finder_metadata.write_text("user metadata")
        user_output = repository / "output/synology/user-sentinel"
        user_output.parent.mkdir(parents=True)
        user_output.write_text("preserve")
        output_root = self.temporary_root / "untracked-package-output"

        completed = self._run_packager_in(
            repository, output_root, os.environ.copy()
        )

        self.assertEqual(
            completed.returncode, 0, completed.stderr.decode("utf-8")
        )
        self.assertTrue((output_root / self.package_name).is_dir())
        self.assertEqual(finder_metadata.read_text(), "user metadata")
        self.assertEqual(user_output.read_text(), "preserve")

    def test_packager_keeps_one_frozen_commit_when_head_moves(self):
        repository = self._clone_repository("moving-head-repository")
        source_commit = _git_output(repository, "rev-parse", "HEAD")
        changed_context = repository / "core/models.py"
        changed_context.write_bytes(
            changed_context.read_bytes() + b"\n# later context commit\n"
        )
        changed_controls = (
            "deploy/synology/build-image.sh",
            "deploy/synology/docker-compose.synology.yml",
            "deploy/synology/.env.example",
        )
        for relative_path in changed_controls:
            changed_control = repository / relative_path
            changed_control.write_bytes(
                changed_control.read_bytes() + b"\n# later control commit\n"
            )
        completed = subprocess.run(
            [
                "git",
                "-c",
                "user.name=Package Test",
                "-c",
                "user.email=package-test@example.invalid",
                "add",
                "core/models.py",
            ] + list(changed_controls),
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
                "later fixture commit",
            ],
            cwd=str(repository),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(
            completed.returncode, 0, completed.stderr.decode("utf-8")
        )
        later_commit = _git_output(repository, "rev-parse", "HEAD")
        completed = subprocess.run(
            ["git", "checkout", "--quiet", source_commit],
            cwd=str(repository),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(
            completed.returncode, 0, completed.stderr.decode("utf-8")
        )
        binary_directory = self.temporary_root / "moving-head-bin"
        binary_directory.mkdir()
        wrapper = binary_directory / "git"
        _write_head_move_git_wrapper(wrapper)
        marker = self.temporary_root / "head-moved"
        real_git = shutil.which("git")
        self.assertIsNotNone(real_git)
        environment = os.environ.copy()
        environment["PATH"] = "{}{}{}".format(
            binary_directory,
            os.pathsep,
            environment.get("PATH", ""),
        )
        environment["PINRY_REAL_GIT"] = real_git
        environment["PINRY_HEAD_MOVE_MARKER"] = str(marker)
        environment["PINRY_HEAD_MOVE_TARGET"] = later_commit
        output_root = self.temporary_root / "moving-head-output"

        completed = self._run_packager_in(
            repository, output_root, environment
        )

        self.assertEqual(
            completed.returncode, 0, completed.stderr.decode("utf-8")
        )
        self.assertTrue(marker.is_file())
        self.assertEqual(
            _git_output(repository, "rev-parse", "HEAD"), later_commit
        )
        package_directory = output_root / self.package_name
        self.assertEqual(
            (package_directory / "BUILD_INFO").read_text(),
            "source_commit={}\n"
            "default_image=pinry-custom:latest\n".format(source_commit),
        )
        self.assertEqual(
            (package_directory / "context/core/models.py").read_bytes(),
            _git_blob(repository, source_commit, "core/models.py"),
        )
        packaged_controls = {
            "build-image.sh": "deploy/synology/build-image.sh",
            "docker-compose.yml": (
                "deploy/synology/docker-compose.synology.yml"
            ),
            ".env.example": "deploy/synology/.env.example",
        }
        for packaged_name, relative_path in packaged_controls.items():
            self.assertEqual(
                (package_directory / packaged_name).read_bytes(),
                _git_blob(repository, source_commit, relative_path),
            )
        self.assertTrue(
            (
                output_root
                / "pinry-custom-{}.tar.gz".format(source_commit[:12])
            ).is_file()
        )

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
                "--build-arg",
                "PINRY_SOURCE_COMMIT={}".format(self.full_sha),
                "--label",
                "org.opencontainers.image.revision={}".format(
                    self.full_sha
                ),
                "--tag",
                "pinry-custom:latest",
                ".",
            ],
        )
        self.assertEqual(_docker_call_count(capture), 1)
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
        self.assertEqual(_docker_call_count(capture), 1)
        self.assertEqual(
            arguments[arguments.index("--tag") + 1],
            "registry.local/pinry-custom:nas",
        )

    def test_build_rejects_every_malformed_build_info_without_docker(self):
        self._create_package()
        environment, capture, _working_directory = (
            self._docker_environment()
        )
        build_info = self.package_directory / "BUILD_INFO"
        sentinel = self.temporary_root / "shell-payload-ran"
        valid_default = "default_image=pinry-custom:latest\n"
        invalid_sources = {
            "missing": valid_default,
            "duplicate-valid": (
                "source_commit={0}\nsource_commit={0}\n{1}".format(
                    self.full_sha, valid_default
                )
            ),
            "duplicate-mixed": (
                "source_commit={0}\nsource_commit={1}\n{2}".format(
                    self.full_sha, "g" * 40, valid_default
                )
            ),
            "duplicate-empty-after-valid": (
                "source_commit={}\nsource_commit=\n{}".format(
                    self.full_sha, valid_default
                )
            ),
            "empty-before-valid": (
                "source_commit=\nsource_commit={}\n{}".format(
                    self.full_sha, valid_default
                )
            ),
            "uppercase": "source_commit={}\n{}".format(
                "A" * 40, valid_default
            ),
            "nonhex": "source_commit={}\n{}".format(
                "g" * 40, valid_default
            ),
            "short": "source_commit={}\n{}".format(
                "a" * 39, valid_default
            ),
            "long": "source_commit={}\n{}".format(
                "a" * 41, valid_default
            ),
            "leading-space": "source_commit= {}\n{}".format(
                "a" * 40, valid_default
            ),
            "trailing-space": "source_commit={} \n{}".format(
                "a" * 40, valid_default
            ),
            "carriage-return": "source_commit={}\r\n{}".format(
                "a" * 40, valid_default
            ),
            "shell-payload": "source_commit=$(touch {})\n{}".format(
                sentinel, valid_default
            ),
            "missing-default-image": "source_commit={}\n".format(
                self.full_sha
            ),
            "duplicate-default-image": (
                "source_commit={}\n"
                "default_image=pinry-custom:latest\n"
                "default_image=pinry-custom:other\n".format(self.full_sha)
            ),
            "duplicate-empty-default-image": (
                "source_commit={}\n"
                "default_image=pinry-custom:latest\n"
                "default_image=\n".format(self.full_sha)
            ),
        }

        for name, source in invalid_sources.items():
            with self.subTest(name=name):
                for artifact in (
                    capture,
                    Path(str(capture) + ".calls"),
                ):
                    if artifact.exists():
                        artifact.unlink()
                build_info.write_text(source)

                completed = subprocess.run(
                    ["sh", str(self.package_directory / "build-image.sh")],
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )

                self.assertNotEqual(completed.returncode, 0)
                self.assertIn(b"invalid_build_info", completed.stderr)
                self.assertEqual(_docker_call_count(capture), 0)
                self.assertFalse(capture.exists())
                self.assertFalse(sentinel.exists())

    def test_build_rejects_unicode_nonhex_in_collating_utf8_locale(self):
        locale_name = _find_range_vulnerable_utf8_locale()
        if locale_name is None:
            self.skipTest("collating UTF-8 locale is unavailable")
        self._create_package()
        environment, capture, _working_directory = (
            self._docker_environment()
        )
        environment["LC_ALL"] = locale_name
        build_info = self.package_directory / "BUILD_INFO"
        build_info.write_text(
            "source_commit={}\n"
            "default_image=pinry-custom:latest\n".format("\u00e9" * 40)
        )

        completed = subprocess.run(
            ["sh", str(self.package_directory / "build-image.sh")],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(b"invalid_build_info", completed.stderr)
        self.assertEqual(_docker_call_count(capture), 0)
        self.assertFalse(capture.exists())

    def test_packager_rejects_unicode_nonhex_commit_before_output(self):
        locale_name = _find_range_vulnerable_utf8_locale()
        if locale_name is None:
            self.skipTest("collating UTF-8 locale is unavailable")
        repository = self._clone_repository("unicode-commit-repository")
        binary_directory = self.temporary_root / "unicode-commit-bin"
        binary_directory.mkdir()
        _write_invalid_commit_git_wrapper(binary_directory / "git")
        real_git = shutil.which("git")
        self.assertIsNotNone(real_git)
        environment = os.environ.copy()
        environment["LC_ALL"] = locale_name
        environment["PATH"] = "{}{}{}".format(
            binary_directory,
            os.pathsep,
            environment.get("PATH", ""),
        )
        environment["PINRY_INVALID_COMMIT"] = "\u00e9" * 40
        environment["PINRY_REAL_GIT"] = real_git
        output_root = self.temporary_root / "unicode-commit-output"

        completed = self._run_packager_in(
            repository, output_root, environment
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(b"invalid_source_commit", completed.stderr)
        self.assertFalse(output_root.exists())

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
        self.assertEqual(_docker_call_count(capture), 0)

    def test_generated_synology_output_is_ignored_by_git(self):
        completed = subprocess.run(
            [
                "git",
                "check-ignore",
                "--quiet",
                "output/probe",
            ],
            cwd=str(self.repository_root),
        )

        self.assertEqual(completed.returncode, 0)
