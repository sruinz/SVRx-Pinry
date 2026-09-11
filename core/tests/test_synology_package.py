import json
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
REQUIRED_STARTUP_PATHS = (
    "docker/scripts/startup.py",
    "docker/scripts/bootstrap.sh",
    "docker/scripts/gen_key.sh",
    "docker/scripts/normalize_persistent_file.py",
    "docker/scripts/_start_gunicorn.sh",
    "docker/scripts/export_storage_bootstrap.py",
    "docker/scripts/export_worker.py",
)
REQUIRED_SYNOLOGY_CONTEXT_PATHS = (
    "exports/apps.py",
    "exports/models.py",
    "docker/scripts/migration_worker.py",
    "docker/scripts/migration_status.py",
    "docker/scripts/supervisor.py",
    "docker/scripts/export_storage_bootstrap.py",
    "docker/scripts/export_worker.py",
    "docker/migration/index.html",
    "docker/migration/migration.css",
    "docker/migration/migration.js",
    "docker/migration/svrx-pinry-dark-ui.png",
    "docker/migration/svrx-pinry-light-ui.png",
)
SERVICE_WORKER_PATH = "pinry-spa/src/service-worker.js"
TRANSITION_COMPOSE_SOURCE_PATH = (
    "deploy/synology/docker-compose.sw-transition.yml"
)
ACCEPTANCE_RUNNER_PATH = "docker/tests/nas_legacy_clone_acceptance.sh"
ACCEPTANCE_FIXTURE_PATH = (
    "docker/tests/fixtures/create_legacy_fixture.py"
)
EXPORT_RUNTIME_SMOKE_PATH = "docker/tests/export_runtime_smoke.sh"
EXPORT_POSTGRES_SMOKE_PATH = (
    "docker/tests/export_postgres_concurrency_smoke.sh"
)
EXPORT_RUNTIME_FIXTURE_PATH = (
    "docker/tests/fixtures/create_export_fixture.py"
)
EXPORT_POSTGRES_DEPENDENCY_PATHS = (
    "exports/tests/__init__.py",
    "exports/tests/helpers.py",
    "exports/tests/test_api.py",
    "exports/tests/test_archive_finalization.py",
    "exports/tests/test_concurrency.py",
    "exports/tests/test_download.py",
    "exports/tests/test_snapshot.py",
    "exports/tests/test_worker.py",
    "exports/tests/test_worker_recovery.py",
    "pinry/settings/test_postgres.py",
)
PACKAGED_SOURCE_SENTINELS = (
    "Dockerfile.autobuild",
    "core/models.py",
    "exports/apps.py",
    "exports/models.py",
    "docker/nginx/sites-enabled/default",
    "docker/scripts/startup.py",
    "docker/scripts/export_storage_bootstrap.py",
    "docker/scripts/export_worker.py",
    SERVICE_WORKER_PATH,
)
SW_TRANSITION_PATHS = (
    "Dockerfile.sw-transition",
    "docker/sw-transition/index.html",
    "docker/sw-transition/nginx.conf",
    "LICENSE.md",
    "NOTICE.md",
    "UPSTREAM.md",
    SERVICE_WORKER_PATH,
)
TASK_PRODUCTION_PATHS = (
    ".github/workflows/node.js.yml",
    ".gitignore",
    ".dockerignore",
    "Dockerfile.autobuild",
    "LICENSE.md",
    "NOTICE.md",
    "UPSTREAM.md",
    "docker/scripts/start.sh",
    *REQUIRED_STARTUP_PATHS,
    *REQUIRED_SYNOLOGY_CONTEXT_PATHS,
    *SW_TRANSITION_PATHS,
    "pinry-spa/package.json",
    "pinry-spa/pnpm-lock.yaml",
    "scripts/create_synology_output.sh",
    "deploy/synology/build-image.sh",
    "deploy/synology/docker-compose.synology.yml",
    TRANSITION_COMPOSE_SOURCE_PATH,
    "deploy/synology/README_KO.md",
    ACCEPTANCE_RUNNER_PATH,
    ACCEPTANCE_FIXTURE_PATH,
    EXPORT_RUNTIME_SMOKE_PATH,
    EXPORT_POSTGRES_SMOKE_PATH,
    EXPORT_RUNTIME_FIXTURE_PATH,
    "docker/nginx/sites-enabled/default",
)
PACKAGE_CONTROL_PATHS = (
    "scripts/create_synology_output.sh",
    "deploy/synology/build-image.sh",
    "deploy/synology/docker-compose.synology.yml",
    TRANSITION_COMPOSE_SOURCE_PATH,
    "deploy/synology/README_KO.md",
    ACCEPTANCE_RUNNER_PATH,
    ACCEPTANCE_FIXTURE_PATH,
    EXPORT_RUNTIME_SMOKE_PATH,
    EXPORT_POSTGRES_SMOKE_PATH,
    EXPORT_RUNTIME_FIXTURE_PATH,
    "LICENSE.md",
    "NOTICE.md",
    "UPSTREAM.md",
)

PACKAGE_TOP_LEVEL_ENTRIES = {
    "BUILD_INFO",
    "LICENSE.md",
    "NOTICE.md",
    "README_KO.md",
    "UPSTREAM.md",
    "build-image.sh",
    "context",
    "docker-compose.yml",
}


def _git_output(repository, *arguments):
    return subprocess.check_output(
        ["git"] + list(arguments), cwd=str(repository)
    ).decode("utf-8").strip()


def _unlink_if_present(path):
    if path.exists() or path.is_symlink():
        path.unlink()


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
        "    if [ \"$previous\" = \"-cf\" ]; then\n"
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
        "        -cf)\n"
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
        "            if [ -z \"$package_name\" ]; then\n"
        "                package_name=${argument%%/*}\n"
        "            fi\n"
        "            ;;\n"
        "    esac\n"
        "done\n"
        "if [ \"$create\" = 1 ]; then\n"
        "    test \"${COPYFILE_DISABLE:-}\" = 1\n"
        "    test \"$no_xattrs\" = 1\n"
        "    package_root=$package_root/$package_name\n"
        "    test -d \"$package_root/context\"\n"
        "    mkdir -p \"$package_root/context/pinry-spa/src/race\"\n"
        "    if [ \"${PINRY_INJECT_ROOT_ENTRY:-0}\" = 1 ]; then\n"
        "        : > \"$package_root/BUILD_INFO._backup\"\n"
        "    fi\n"
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
        "    if [ \"$argument\" = \"-cf\" ]; then\n"
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
        "        *.tar.gz)\n"
        "            exit 43\n"
        "            ;;\n"
        "    esac\n"
        "done\n"
        "exec \"$PINRY_REAL_PYTHON3\" \"$@\"\n"
    )
    path.chmod(0o700)


def _write_default_bundle_hard_exit_python_wrapper(path):
    path.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "destination=\n"
        "for argument in \"$@\"; do\n"
        "    destination=$argument\n"
        "done\n"
        "if [ \"$destination\" = \"$PINRY_DEFAULT_FINAL_ROOT\" ] "
        "&& [ ! -e \"$PINRY_HARD_EXIT_MARKER\" ]; then\n"
        "    : > \"$PINRY_HARD_EXIT_MARKER\"\n"
        "    kill -KILL \"$PPID\"\n"
        "    exit 99\n"
        "fi\n"
        "exec \"$PINRY_REAL_PYTHON3\" \"$@\"\n"
    )
    path.chmod(0o700)


def _write_sync_tree_failure_python_wrapper(path):
    path.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "if [ \"${3:-}\" = tree ]; then\n"
        "    exit 74\n"
        "fi\n"
        "exec \"$PINRY_REAL_PYTHON3\" \"$@\"\n"
    )
    path.chmod(0o700)


def _write_cleanup_child_injection_python_wrapper(path):
    path.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "cleanup_path=${3:-}\n"
        "cleanup_kind=${5:-}\n"
        "if [ \"$cleanup_kind\" = directory ] "
        "&& [ ! -e \"$PINRY_CLEANUP_INJECTION_MARKER\" ]; then\n"
        "    case \"$cleanup_path\" in\n"
        "        */.svrx-pinry*.tmp.*)\n"
        "            : > \"$PINRY_CLEANUP_INJECTION_MARKER\"\n"
        "            printf '%s' \"$cleanup_path\" > "
        "\"$PINRY_CLEANUP_PATH_CAPTURE\"\n"
        "            mv \"$PINRY_CLEANUP_VICTIM\" "
        "\"$cleanup_path/injected-victim\"\n"
        "            ;;\n"
        "    esac\n"
        "fi\n"
        "exec \"$PINRY_REAL_PYTHON3\" \"$@\"\n"
    )
    path.chmod(0o700)


def _write_publish_race_python_wrapper(path):
    path.write_text(
        "#!/bin/sh\n"
        "set -u\n"
        "destination=\n"
        "source=\n"
        "injected=0\n"
        "for argument in \"$@\"; do\n"
        "    source=$destination\n"
        "    destination=$argument\n"
        "done\n"
        "if [ \"$destination\" = \"${PINRY_PUBLISH_RACE_DESTINATION}\" ] "
        "&& [ ! -e \"$PINRY_PUBLISH_RACE_MARKER\" ]; then\n"
        "    injected=1\n"
        "    : > \"$PINRY_PUBLISH_RACE_MARKER\"\n"
        "    case \"$PINRY_PUBLISH_RACE_KIND\" in\n"
        "        directory)\n"
        "            mkdir \"$PINRY_PUBLISH_RACE_DESTINATION\"\n"
        "            printf '%s' preserve > "
        "\"$PINRY_PUBLISH_RACE_DESTINATION/sentinel\"\n"
        "            ;;\n"
        "        symlink)\n"
        "            ln -s \"$PINRY_PUBLISH_RACE_TARGET\" "
        "\"$PINRY_PUBLISH_RACE_DESTINATION\"\n"
        "            ;;\n"
        "        dangling-symlink)\n"
        "            ln -s \"$PINRY_PUBLISH_RACE_TARGET\" "
        "\"$PINRY_PUBLISH_RACE_DESTINATION\"\n"
        "            ;;\n"
        "    esac\n"
        "fi\n"
        "\"$PINRY_REAL_PYTHON3\" \"$@\"\n"
        "publish_status=$?\n"
        "if [ \"$injected\" = 1 ] "
        "&& [ \"${PINRY_REPLACE_PUBLISH_SOURCE:-0}\" = 1 ] "
        "&& [ \"$publish_status\" != 0 ]; then\n"
        "    mv \"$source\" \"$PINRY_CLEANUP_REPLACEMENT_TARGET\"\n"
        "    mkdir \"$source\"\n"
        "    printf '%s' preserve > \"$source/sentinel\"\n"
        "    printf '%s' \"$source\" > \"$PINRY_CLEANUP_REPLACED_PATH\"\n"
        "fi\n"
        "if [ \"$injected\" = 1 ]; then\n"
        "case \"$PINRY_PUBLISH_RACE_KIND\" in\n"
        "    directory|symlink)\n"
        "        entry_count=$(find \"$PINRY_PUBLISH_RACE_WATCH_PATH\" "
        "-mindepth 1 -maxdepth 1 | wc -l | tr -d ' ')\n"
        "        if [ ! -f \"$PINRY_PUBLISH_RACE_WATCH_PATH/sentinel\" ] "
        "|| [ \"$entry_count\" != 1 ]; then\n"
        "            printf 'status=%s sentinel=%s entries=%s' "
        "\"$publish_status\" "
        "\"$(test -f \"$PINRY_PUBLISH_RACE_WATCH_PATH/sentinel\" && echo yes || echo no)\" "
        "\"$entry_count\" > \"$PINRY_PUBLISH_RACE_MUTATION\"\n"
        "        fi\n"
        "        ;;\n"
        "    dangling-symlink)\n"
        "        if [ -e \"$PINRY_PUBLISH_RACE_WATCH_PATH\" ] "
        "|| [ -L \"$PINRY_PUBLISH_RACE_WATCH_PATH\" ]; then\n"
        "            : > \"$PINRY_PUBLISH_RACE_MUTATION\"\n"
        "        fi\n"
        "        ;;\n"
        "esac\n"
        "fi\n"
        "exit \"$publish_status\"\n"
    )
    path.chmod(0o700)


def _write_publish_source_swap_python_wrapper(path):
    path.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "previous=\n"
        "source=\n"
        "for argument in \"$@\"; do\n"
        "    if [ \"$argument\" = \"$PINRY_SOURCE_SWAP_DESTINATION\" ]; "
        "then\n"
        "        source=$previous\n"
        "    fi\n"
        "    previous=$argument\n"
        "done\n"
        "if [ -n \"$source\" ] "
        "&& [ ! -e \"$PINRY_SOURCE_SWAP_MARKER\" ]; then\n"
        "    : > \"$PINRY_SOURCE_SWAP_MARKER\"\n"
        "    mv \"$source\" \"$PINRY_SOURCE_SWAP_QUARANTINE\"\n"
        "    ln -s \"$PINRY_SOURCE_SWAP_TARGET\" \"$source\"\n"
        "fi\n"
        "exec \"$PINRY_REAL_PYTHON3\" \"$@\"\n"
    )
    path.chmod(0o700)


def _write_recording_mktemp_wrapper(path):
    path.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "created=$(\"$PINRY_REAL_MKTEMP\" \"$@\")\n"
        "case \"$created\" in\n"
        "    */.svrx-pinry.archive.*)\n"
        "        printf '%s' \"$created\" > \"$PINRY_ARCHIVE_PATH_CAPTURE\"\n"
        "        ;;\n"
        "esac\n"
        "printf '%s\\n' \"$created\"\n"
    )
    path.chmod(0o700)


def _write_archive_file_swap_python_wrapper(path):
    path.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "if [ -f \"$PINRY_ARCHIVE_PATH_CAPTURE\" ] "
        "&& [ ! -e \"$PINRY_ARCHIVE_SWAP_MARKER\" ] "
        "&& [ \"$#\" -ge 8 ]; then\n"
        "    archive=$(cat \"$PINRY_ARCHIVE_PATH_CAPTURE\")\n"
        "    swap=0\n"
        "    for argument in \"$@\"; do\n"
        "        if [ \"$argument\" = \"$archive\" ]; then\n"
        "            swap=1\n"
        "        fi\n"
        "    done\n"
        "    if [ \"$swap\" = 1 ]; then\n"
        "        : > \"$PINRY_ARCHIVE_SWAP_MARKER\"\n"
        "        mv \"$archive\" \"$PINRY_ARCHIVE_SWAP_QUARANTINE\"\n"
        "        case \"$PINRY_ARCHIVE_SWAP_KIND\" in\n"
        "            regular)\n"
        "                cp \"$PINRY_ARCHIVE_SWAP_TARGET\" \"$archive\"\n"
        "                ;;\n"
        "            hardlink)\n"
        "                ln \"$PINRY_ARCHIVE_SWAP_TARGET\" \"$archive\"\n"
        "                ;;\n"
        "            *)\n"
        "                exit 97\n"
        "                ;;\n"
        "        esac\n"
        "    fi\n"
        "fi\n"
        "exec \"$PINRY_REAL_PYTHON3\" \"$@\"\n"
    )
    path.chmod(0o700)


def _write_archive_source_swap_tar_wrapper(path):
    path.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "create=0\n"
        "for argument in \"$@\"; do\n"
        "    if [ \"$argument\" = -cf ]; then\n"
        "        create=1\n"
        "    fi\n"
        "done\n"
        "if [ \"$create\" = 1 ] "
        "&& [ ! -e \"$PINRY_ARCHIVE_SWAP_MARKER\" ]; then\n"
        "    archive=$(cat \"$PINRY_ARCHIVE_PATH_CAPTURE\")\n"
        "    : > \"$PINRY_ARCHIVE_SWAP_MARKER\"\n"
        "    mv \"$archive\" \"$PINRY_ARCHIVE_SWAP_QUARANTINE\"\n"
        "    case \"${PINRY_ARCHIVE_SWAP_KIND:-symlink}\" in\n"
        "        symlink)\n"
        "            ln -s \"$PINRY_ARCHIVE_SWAP_TARGET\" \"$archive\"\n"
        "            ;;\n"
        "        regular)\n"
        "            cp \"$PINRY_ARCHIVE_SWAP_TARGET\" \"$archive\"\n"
        "            ;;\n"
        "        hardlink)\n"
        "            ln \"$PINRY_ARCHIVE_SWAP_TARGET\" \"$archive\"\n"
        "            ;;\n"
        "        *)\n"
        "            exit 97\n"
        "            ;;\n"
        "    esac\n"
        "fi\n"
        "exec \"$PINRY_REAL_TAR\" \"$@\"\n"
    )
    path.chmod(0o700)


def _write_mktemp_failure_wrapper(path):
    path.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "if [ -f \"$PINRY_MKTEMP_CAPTURE\" ]; then\n"
        "    count=$(cat \"$PINRY_MKTEMP_CAPTURE\")\n"
        "else\n"
        "    count=0\n"
        "fi\n"
        "count=$((count + 1))\n"
        "printf '%s' \"$count\" > \"$PINRY_MKTEMP_CAPTURE\"\n"
        "if [ \"$count\" = \"$PINRY_MKTEMP_FAIL_ON\" ]; then\n"
        "    exit 73\n"
        "fi\n"
        "exec \"$PINRY_REAL_MKTEMP\" \"$@\"\n"
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
        export_test_module = cls.fixture_repository / "exports/tests.py"
        export_test_module.write_text("raise AssertionError('not runtime')\n")
        completed = subprocess.run(
            ["git", "add"] + list(TASK_PRODUCTION_PATHS) + [
                "exports/tests.py"
            ],
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
        self.package_name = "svrx-pinry"
        self.package_directory = self.output_root / self.package_name
        self.context_directory = self.package_directory / "context"
        self.transition_directory = self.output_root / "sw-transition"
        self.accept_tools_directory = self.output_root / "accept-tools"
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

    def _python_environment(self, wrapper):
        environment = os.environ.copy()
        real_python = shutil.which("python3")
        self.assertIsNotNone(real_python)
        environment["PATH"] = "{}{}{}".format(
            wrapper.parent,
            os.pathsep,
            environment.get("PATH", ""),
        )
        environment["PINRY_REAL_PYTHON3"] = real_python
        return environment

    def _mktemp_environment(self, wrapper, fail_on):
        environment = os.environ.copy()
        real_mktemp = shutil.which("mktemp")
        self.assertIsNotNone(real_mktemp)
        environment["PATH"] = "{}{}{}".format(
            wrapper.parent,
            os.pathsep,
            environment.get("PATH", ""),
        )
        environment["PINRY_MKTEMP_CAPTURE"] = str(
            self.temporary_root / "mktemp-count"
        )
        environment["PINRY_MKTEMP_FAIL_ON"] = str(fail_on)
        environment["PINRY_REAL_MKTEMP"] = real_mktemp
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

    def _commit_all(self, repository, message):
        completed = subprocess.run(
            ["git", "add", "-A"],
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
                message,
            ],
            cwd=str(repository),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(
            completed.returncode, 0, completed.stderr.decode("utf-8")
        )

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
        self.assertEqual(
            canonical.stdout.decode("utf-8").splitlines(),
            [
                "upload_directory={}".format(expected_package.resolve()),
                "upload_archive={}".format(expected_archive.resolve()),
            ],
        )
        self.assertTrue(expected_package.is_dir())
        self.assertTrue(expected_archive.is_file())
        self.assertTrue((default_output / "sw-transition").is_dir())
        self.assertTrue((default_output / "accept-tools").is_dir())
        self.assertEqual(
            (expected_package / "BUILD_INFO").read_text().splitlines()[0],
            "source_commit={}".format(self.full_sha),
        )
        self.assertEqual(
            expected_archive.name,
            "svrx-pinry-{}.tar.gz".format(self.full_sha[:12]),
        )

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

    def test_default_output_rejects_symlinked_workspace_output_parent(self):
        output_parent = self.repository_root.parent / "output"
        external_output = self.temporary_root / "external-output"
        external_output.mkdir()
        if output_parent.exists():
            self.assertEqual(tuple(output_parent.iterdir()), ())
            output_parent.rmdir()
        output_parent.symlink_to(external_output, target_is_directory=True)
        self.addCleanup(_unlink_if_present, output_parent)

        completed = self._run_packager_in(self.repository_root)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(b"output_parent_invalid", completed.stderr)
        self.assertEqual(tuple(external_output.iterdir()), ())

    def _assert_default_output_recovers_from_mktemp_failure(self, fail_on):
        default_output = (
            self.repository_root.parent
            / "output"
            / "svrx-pinry-server-{}".format(self.short_sha)
        )
        self.addCleanup(shutil.rmtree, default_output, ignore_errors=True)
        binary_directory = self.temporary_root / "failing-mktemp"
        binary_directory.mkdir()
        wrapper = binary_directory / "mktemp"
        _write_mktemp_failure_wrapper(wrapper)

        failed = self._run_packager_in(
            self.repository_root, environment=self._mktemp_environment(
                wrapper, fail_on
            )
        )
        leaked_output = default_output.exists()
        leaked_temporary_paths = tuple(default_output.glob(".svrx-pinry.*"))
        retry = self._run_packager_in(self.repository_root)

        self.assertEqual(failed.returncode, 73)
        self.assertFalse(
            leaked_output,
            "기본 목적지 누수로 재시도가 막혔습니다: {}".format(
                retry.stderr.decode("utf-8")
            ),
        )
        self.assertEqual(leaked_temporary_paths, ())
        self.assertEqual(retry.returncode, 0, retry.stderr.decode("utf-8"))
        self.assertTrue((default_output / self.package_name).is_dir())

    def test_default_output_recovers_when_first_mktemp_fails(self):
        self._assert_default_output_recovers_from_mktemp_failure(1)

    def test_default_output_recovers_when_second_mktemp_fails(self):
        self._assert_default_output_recovers_from_mktemp_failure(2)

    def test_default_output_is_not_partially_published_on_hard_exit(self):
        output_parent = self.repository_root.parent / "output"
        default_output = output_parent / "svrx-pinry-server-{}".format(
            self.short_sha
        )
        self.addCleanup(shutil.rmtree, default_output, ignore_errors=True)
        staging_pattern = ".svrx-pinry-server-{}.tmp.*".format(
            self.short_sha
        )

        def remove_abandoned_staging():
            for staging_path in output_parent.glob(staging_pattern):
                shutil.rmtree(staging_path, ignore_errors=True)

        self.addCleanup(remove_abandoned_staging)
        binary_directory = self.temporary_root / "hard-exit-python"
        binary_directory.mkdir()
        wrapper = binary_directory / "python3"
        marker = self.temporary_root / "hard-exit-marker"
        _write_default_bundle_hard_exit_python_wrapper(wrapper)
        environment = self._python_environment(wrapper)
        environment.update(
            {
                "PINRY_DEFAULT_FINAL_ROOT": str(default_output.resolve()),
                "PINRY_HARD_EXIT_MARKER": str(marker),
            }
        )

        failed = self._run_packager_in(
            self.repository_root, environment=environment
        )

        self.assertNotEqual(failed.returncode, 0)
        self.assertTrue(marker.is_file())
        self.assertFalse(default_output.exists())
        self.assertFalse(default_output.is_symlink())

        retried = self._run_packager_in(self.repository_root)

        self.assertEqual(
            retried.returncode, 0, retried.stderr.decode("utf-8")
        )
        self.assertEqual(
            {path.name for path in default_output.iterdir()},
            {
                "accept-tools",
                "svrx-pinry",
                "sw-transition",
                "svrx-pinry-{}.tar.gz".format(self.short_sha),
            },
        )

    def test_default_output_sync_failure_leaves_no_partial_destination(self):
        default_output = (
            self.repository_root.parent
            / "output"
            / "svrx-pinry-server-{}".format(self.short_sha)
        )
        self.addCleanup(shutil.rmtree, default_output, ignore_errors=True)
        binary_directory = self.temporary_root / "sync-failure-python"
        binary_directory.mkdir()
        wrapper = binary_directory / "python3"
        _write_sync_tree_failure_python_wrapper(wrapper)

        failed = self._run_packager_in(
            self.repository_root,
            environment=self._python_environment(wrapper),
        )

        self.assertEqual(failed.returncode, 74)
        self.assertFalse(default_output.exists())
        self.assertFalse(default_output.is_symlink())

        retried = self._run_packager_in(self.repository_root)

        self.assertEqual(
            retried.returncode, 0, retried.stderr.decode("utf-8")
        )
        self.assertEqual(
            {path.name for path in default_output.iterdir()},
            {
                "accept-tools",
                "svrx-pinry",
                "sw-transition",
                "svrx-pinry-{}.tar.gz".format(self.short_sha),
            },
        )

    def test_explicit_output_directory_overrides_shared_default(self):
        explicit_output = self.temporary_root / "explicit-output"

        completed = self._run_packager_in(
            self.repository_root, explicit_output
        )

        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8"))
        self.assertTrue((explicit_output / self.package_name).is_dir())
        self.assertTrue((explicit_output / "accept-tools").is_dir())
        self.assertTrue(
            (
                explicit_output
                / "{}-{}.tar.gz".format(self.package_name, self.short_sha)
            ).is_file()
        )
        self.assertEqual(
            {path.name for path in explicit_output.iterdir()},
            {
                "accept-tools",
                "svrx-pinry",
                "sw-transition",
                "svrx-pinry-{}.tar.gz".format(self.short_sha),
            },
        )

    def test_explicit_output_is_not_partially_published_on_hard_exit(self):
        explicit_output = self.temporary_root / "explicit-hard-exit"
        staging_pattern = ".svrx-pinry-server-{}.tmp.*".format(
            self.short_sha
        )

        def remove_abandoned_staging():
            for staging_path in self.temporary_root.glob(staging_pattern):
                shutil.rmtree(staging_path, ignore_errors=True)

        self.addCleanup(remove_abandoned_staging)
        binary_directory = self.temporary_root / "explicit-hard-exit-bin"
        binary_directory.mkdir()
        wrapper = binary_directory / "python3"
        marker = self.temporary_root / "explicit-hard-exit-marker"
        _write_default_bundle_hard_exit_python_wrapper(wrapper)
        environment = self._python_environment(wrapper)
        environment.update(
            {
                "PINRY_DEFAULT_FINAL_ROOT": str(explicit_output.resolve()),
                "PINRY_HARD_EXIT_MARKER": str(marker),
            }
        )

        failed = self._run_packager_in(
            self.repository_root, explicit_output, environment
        )

        self.assertNotEqual(failed.returncode, 0)
        self.assertTrue(marker.is_file())
        self.assertFalse(explicit_output.exists())
        self.assertFalse(explicit_output.is_symlink())

        retried = self._run_packager_in(
            self.repository_root, explicit_output
        )

        self.assertEqual(
            retried.returncode, 0, retried.stderr.decode("utf-8")
        )
        self.assertEqual(
            {path.name for path in explicit_output.iterdir()},
            {
                "accept-tools",
                "svrx-pinry",
                "sw-transition",
                "svrx-pinry-{}.tar.gz".format(self.short_sha),
            },
        )

    def test_explicit_output_sync_failure_leaves_no_partial_destination(self):
        explicit_output = self.temporary_root / "explicit-sync-failure"
        binary_directory = self.temporary_root / "explicit-sync-failure-bin"
        binary_directory.mkdir()
        wrapper = binary_directory / "python3"
        _write_sync_tree_failure_python_wrapper(wrapper)

        failed = self._run_packager_in(
            self.repository_root,
            explicit_output,
            self._python_environment(wrapper),
        )

        self.assertEqual(failed.returncode, 74)
        self.assertFalse(explicit_output.exists())
        self.assertFalse(explicit_output.is_symlink())

        retried = self._run_packager_in(
            self.repository_root, explicit_output
        )

        self.assertEqual(
            retried.returncode, 0, retried.stderr.decode("utf-8")
        )
        self.assertEqual(
            {path.name for path in explicit_output.iterdir()},
            {
                "accept-tools",
                "svrx-pinry",
                "sw-transition",
                "svrx-pinry-{}.tar.gz".format(self.short_sha),
            },
        )

    def test_explicit_output_rejects_existing_targets_without_mutation(self):
        empty_output = self.temporary_root / "existing-empty"
        empty_output.mkdir()

        empty_result = self._run_packager_in(
            self.repository_root, empty_output
        )

        self.assertNotEqual(empty_result.returncode, 0)
        self.assertIn(b"output_already_exists", empty_result.stderr)
        self.assertEqual(tuple(empty_output.iterdir()), ())

        nonempty_output = self.temporary_root / "existing-nonempty"
        nonempty_output.mkdir()
        nonempty_sentinel = nonempty_output / "sentinel"
        nonempty_sentinel.write_text("preserve")

        nonempty_result = self._run_packager_in(
            self.repository_root, nonempty_output
        )

        self.assertNotEqual(nonempty_result.returncode, 0)
        self.assertIn(b"output_already_exists", nonempty_result.stderr)
        self.assertEqual(
            {path.name for path in nonempty_output.iterdir()}, {"sentinel"}
        )
        self.assertEqual(nonempty_sentinel.read_text(), "preserve")

        symlink_target = self.temporary_root / "symlink-target"
        symlink_target.mkdir()
        symlink_sentinel = symlink_target / "sentinel"
        symlink_sentinel.write_text("preserve")
        symlink_output = self.temporary_root / "existing-symlink"
        symlink_output.symlink_to(symlink_target, target_is_directory=True)

        symlink_result = self._run_packager_in(
            self.repository_root, symlink_output
        )

        self.assertNotEqual(symlink_result.returncode, 0)
        self.assertIn(b"output_already_exists", symlink_result.stderr)
        self.assertTrue(symlink_output.is_symlink())
        self.assertEqual(symlink_output.resolve(), symlink_target.resolve())
        self.assertEqual(
            {path.name for path in symlink_target.iterdir()}, {"sentinel"}
        )
        self.assertEqual(symlink_sentinel.read_text(), "preserve")

    def test_cleanup_preserves_renamed_in_child_and_retry_succeeds(self):
        explicit_output = self.temporary_root / "cleanup-injection-output"
        binary_directory = self.temporary_root / "cleanup-injection-bin"
        binary_directory.mkdir()
        python_wrapper = binary_directory / "python3"
        tar_wrapper = binary_directory / "tar"
        _write_cleanup_child_injection_python_wrapper(python_wrapper)
        _write_final_tar_failure_wrapper(tar_wrapper)
        real_tar = shutil.which("tar")
        self.assertIsNotNone(real_tar)
        marker = self.temporary_root / "cleanup-injection-marker"
        capture = self.temporary_root / "cleanup-path"
        victim = self.temporary_root / "cleanup-victim"
        victim.mkdir()
        sentinel = victim / "sentinel"
        sentinel.write_text("preserve")
        environment = self._python_environment(python_wrapper)
        environment.update(
            {
                "PINRY_REAL_TAR": real_tar,
                "PINRY_CLEANUP_INJECTION_MARKER": str(marker),
                "PINRY_CLEANUP_PATH_CAPTURE": str(capture),
                "PINRY_CLEANUP_VICTIM": str(victim),
            }
        )

        failed = self._run_packager_in(
            self.repository_root, explicit_output, environment
        )

        self.assertEqual(failed.returncode, 42)
        self.assertTrue(marker.is_file(), failed.stderr.decode("utf-8"))
        cleanup_path = Path(capture.read_text())
        self.addCleanup(shutil.rmtree, cleanup_path, ignore_errors=True)
        injected_sentinel = cleanup_path / "injected-victim/sentinel"
        self.assertTrue(injected_sentinel.is_file())
        self.assertEqual(injected_sentinel.read_text(), "preserve")
        self.assertIn(
            "cleanup_deferred={}".format(cleanup_path),
            failed.stderr.decode("utf-8"),
        )
        self.assertFalse(explicit_output.exists())

        retried = self._run_packager_in(
            self.repository_root, explicit_output
        )

        self.assertEqual(
            retried.returncode, 0, retried.stderr.decode("utf-8")
        )
        self.assertEqual(
            {path.name for path in explicit_output.iterdir()},
            {
                "accept-tools",
                "svrx-pinry",
                "sw-transition",
                "svrx-pinry-{}.tar.gz".format(self.short_sha),
            },
        )

    def test_packager_writes_gzip_archive_without_trailing_data(self):
        self._create_package()
        gzip = shutil.which("gzip")
        self.assertIsNotNone(gzip)

        completed = subprocess.run(
            [gzip, "-t", str(self.archive_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual(
            completed.returncode, 0, completed.stderr.decode("utf-8")
        )

    def test_packager_creates_minimal_build_context_from_head(self):
        self._create_package()

        self.assertEqual(
            {path.name for path in self.package_directory.iterdir()},
            PACKAGE_TOP_LEVEL_ENTRIES,
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
            *REQUIRED_STARTUP_PATHS,
            *REQUIRED_SYNOLOGY_CONTEXT_PATHS,
            SERVICE_WORKER_PATH,
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
            "default_image=svrx-pinry:latest\n".format(self.full_sha),
        )
        self.assertFalse((self.package_directory / ".env").exists())
        self.assertFalse((self.package_directory / ".env.example").exists())
        compose = (
            self.package_directory / "docker-compose.yml"
        ).read_text()
        self.assertEqual(
            [line.strip() for line in compose.splitlines() if line.strip()],
            [
                'version: "3.8"',
                "services:",
                "svrx-pinry:",
                "image: svrx-pinry:latest",
                'command: ["/pinry/docker/scripts/start.sh", '
                '"--migrate-legacy"]',
                "container_name: svrx-pinry",
                "ports:",
                '- "2048:80"',
                "volumes:",
                '- "/volume1/docker/svrx-pinry/data:/data"',
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
                "exports",
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
            {"migration", "nginx", "scripts"},
        )
        for excluded_path in (
            "exports/tests",
            "exports/tests.py",
            "pinry/settings/test_postgres.py",
            "pinry/settings/test_sqlite_file.py",
        ):
            with self.subTest(excluded_path=excluded_path):
                self.assertFalse(
                    (self.context_directory / excluded_path).exists()
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
        archive_parts = [Path(name).parts for name in names]
        self.assertTrue(
            all(
                parts and parts[0] == self.package_name
                for parts in archive_parts
            )
        )
        archive_top_level = {
            parts[1]
            for parts in archive_parts
            if len(parts) >= 2
        }
        self.assertEqual(archive_top_level, PACKAGE_TOP_LEVEL_ENTRIES)
        self.assertIn(
            "{}/build-image.sh".format(self.package_name), names
        )
        self.assertNotIn(
            "{}/.env.example".format(self.package_name), names
        )
        self.assertIn(
            "{}/docker-compose.yml".format(self.package_name), names
        )
        self.assertNotIn("{}/.env".format(self.package_name), names)
        self.assertFalse(any("accept-tools" in name for name in names))
        self.assertIn(
            "{}/context/core/models.py".format(self.package_name), names
        )
        for relative_path in REQUIRED_SYNOLOGY_CONTEXT_PATHS + (
            SERVICE_WORKER_PATH,
        ):
            self.assertIn(
                "{}/context/{}".format(self.package_name, relative_path),
                names,
            )
        self.assertTrue(
            all(
                ".." not in Path(name).parts
                and ".git" not in Path(name).parts
                and ".DS_Store" not in Path(name).parts
                and (
                    Path(name).name
                    in {
                        "LICENSE.md",
                        "NOTICE.md",
                        "README_KO.md",
                        "UPSTREAM.md",
                    }
                    or Path(name).suffix.lower() not in (".md", ".rst")
                )
                for name in names
            )
        )

    def test_acceptance_tools_are_separate_exact_head_artifacts(self):
        self._create_package()

        packaged_sources = {
            "nas_legacy_clone_acceptance.sh": ACCEPTANCE_RUNNER_PATH,
            "fixtures/create_legacy_fixture.py": ACCEPTANCE_FIXTURE_PATH,
            EXPORT_RUNTIME_SMOKE_PATH: EXPORT_RUNTIME_SMOKE_PATH,
            EXPORT_POSTGRES_SMOKE_PATH: EXPORT_POSTGRES_SMOKE_PATH,
            EXPORT_RUNTIME_FIXTURE_PATH: EXPORT_RUNTIME_FIXTURE_PATH,
        }
        packaged_sources.update(
            {
                relative_path: relative_path
                for relative_path in EXPORT_POSTGRES_DEPENDENCY_PATHS
            }
        )
        expected_entries = {"BUILD_INFO"}
        for packaged_path in packaged_sources:
            path = Path(packaged_path)
            expected_entries.add(str(path))
            expected_entries.update(
                str(parent)
                for parent in path.parents
                if str(parent) != "."
            )
        self.assertEqual(
            {
                str(path.relative_to(self.accept_tools_directory))
                for path in self.accept_tools_directory.rglob("*")
            },
            expected_entries,
        )
        expected_build_info = (
            "source_commit={}\n"
            "default_image=svrx-pinry:latest\n".format(self.full_sha)
        ).encode("utf-8")
        self.assertEqual(
            (self.accept_tools_directory / "BUILD_INFO").read_bytes(),
            expected_build_info,
        )
        executable_paths = {
            "nas_legacy_clone_acceptance.sh",
            EXPORT_RUNTIME_SMOKE_PATH,
            EXPORT_POSTGRES_SMOKE_PATH,
            EXPORT_RUNTIME_FIXTURE_PATH,
        }
        for packaged_path, source_path in packaged_sources.items():
            artifact_path = self.accept_tools_directory / packaged_path
            with self.subTest(packaged_path=packaged_path):
                self.assertEqual(
                    artifact_path.read_bytes(),
                    _git_blob(
                        self.repository_root,
                        self.full_sha,
                        source_path,
                    ),
                )
                self.assertEqual(
                    artifact_path.stat().st_mode & 0o777,
                    0o755 if packaged_path in executable_paths else 0o644,
                )
        self.assertEqual(
            (self.accept_tools_directory / "BUILD_INFO").stat().st_mode
            & 0o777,
            0o644,
        )

    def test_context_contains_maintenance_assets_and_supervisor(self):
        self._create_package()

        required = REQUIRED_SYNOLOGY_CONTEXT_PATHS + (
            SERVICE_WORKER_PATH,
        )
        for relative_path in required:
            with self.subTest(relative_path=relative_path):
                self.assertTrue(
                    (self.context_directory / relative_path).is_file(),
                    relative_path,
                )
        self.assertEqual(
            len(list(self.package_directory.iterdir())),
            8,
        )

    def test_sw_transition_context_is_separate_and_uses_same_worker(self):
        self._create_package()

        self.assertTrue(self.transition_directory.is_dir())
        self.assertEqual(
            {path.name for path in self.transition_directory.iterdir()},
            {
                "Dockerfile.sw-transition",
                "LICENSE.md",
                "NOTICE.md",
                "UPSTREAM.md",
                "docker-compose.yml",
                "docker",
                "pinry-spa",
            },
        )
        for relative_path in SW_TRANSITION_PATHS:
            with self.subTest(relative_path=relative_path):
                packaged = self.transition_directory / relative_path
                self.assertTrue(packaged.is_file(), relative_path)
                self.assertEqual(
                    packaged.read_bytes(),
                    _git_blob(
                        self.repository_root,
                        self.full_sha,
                        relative_path,
                    ),
                )
        transition_compose = self.transition_directory / "docker-compose.yml"
        self.assertEqual(
            transition_compose.read_bytes(),
            _git_blob(
                self.repository_root,
                self.full_sha,
                TRANSITION_COMPOSE_SOURCE_PATH,
            ),
        )
        compose_text = transition_compose.read_text()
        self.assertIn("build:", compose_text)
        self.assertIn("context: .", compose_text)
        self.assertIn("dockerfile: Dockerfile.sw-transition", compose_text)
        self.assertIn('"2048:80"', compose_text)
        self.assertIn("restart: unless-stopped", compose_text)
        self.assertNotIn("volumes:", compose_text)
        self.assertEqual(
            (self.transition_directory / SERVICE_WORKER_PATH).read_bytes(),
            (self.context_directory / SERVICE_WORKER_PATH).read_bytes(),
        )
        dockerfile_lines = set(
            (
                self.transition_directory / "Dockerfile.sw-transition"
            ).read_text().splitlines()
        )
        self.assertTrue(
            {
                "COPY LICENSE.md /licenses/LICENSE.md",
                "COPY NOTICE.md /licenses/NOTICE.md",
                "COPY UPSTREAM.md /licenses/UPSTREAM.md",
            }.issubset(dockerfile_lines)
        )

    def test_transition_guide_requires_the_existing_service_origin(self):
        self._create_package()

        guide = (self.package_directory / "README_KO.md").read_text()

        self.assertNotIn("`http://NAS주소:2048`을 한 번 열어", guide)
        self.assertIn(
            "scheme(HTTP/HTTPS), host, port가 모두 정확히 같은 origin",
            guide,
        )
        self.assertIn("리버스 프록시를 임시로 전환 project에 연결", guide)
        self.assertIn(
            "평문 HTTP만 사용했고 서비스 워커가 등록된 적이 없다면",
            guide,
        )
        self.assertIn("전환 단계는 필요하지 않다", guide)
        self.assertIn("loopback 자동화 테스트", guide)
        self.assertIn("운영 HTTPS 설정을 검증하지 않는다", guide)

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
        archive_path = output_root / "svrx-pinry-{}.tar.gz".format(
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
        self.assertNotIn("svrx-pinry/.DS_Store.backup", names)
        self.assertIn(
            "svrx-pinry/context/pinry-spa/src/.DS_Store.backup", names
        )
        self.assertIn(
            "svrx-pinry/context/pinry-spa/src/race/.DS_Store.backup",
            names,
        )
        self.assertNotIn("svrx-pinry/BUILD_INFO._backup", names)
        self.assertIn(
            "svrx-pinry/context/pinry-spa/src/race/asset._preview.js",
            names,
        )
        self.assertIn(
            "svrx-pinry/context/pinry-spa/src/package._sentinel.txt",
            names,
        )
        self.assertIn(
            "svrx-pinry/context/pinry-spa/src/package-sentinel.txt", names
        )

    def test_packager_cleans_failed_publish_and_allows_immediate_retry(self):
        sentinel = self.temporary_root / "unrelated-sentinel"
        sentinel.write_text("preserve")
        binary_directory = self.temporary_root / "failing-tar"
        binary_directory.mkdir()
        wrapper = binary_directory / "tar"
        _write_final_tar_failure_wrapper(wrapper)

        failed = self._run_packager(self._tar_environment(wrapper))

        self.assertEqual(failed.returncode, 42)
        self.assertFalse(self.package_directory.exists())
        self.assertFalse(self.accept_tools_directory.exists())
        self.assertFalse(self.archive_path.exists())
        self.assertEqual(sentinel.read_text(), "preserve")
        abandoned_staging = tuple(
            self.temporary_root.glob(
                ".svrx-pinry-server-{}.tmp.*".format(self.short_sha)
            )
        )
        self.assertEqual(len(abandoned_staging), 1)
        self.addCleanup(
            shutil.rmtree, abandoned_staging[0], ignore_errors=True
        )
        self.assertIn(
            "cleanup_deferred={}".format(abandoned_staging[0].resolve()),
            failed.stderr.decode("utf-8"),
        )

        self._create_package()

        self.assertEqual(
            (self.package_directory / "BUILD_INFO").read_text(),
            "source_commit={}\n"
            "default_image=svrx-pinry:latest\n".format(self.full_sha),
        )
        self.assertTrue((self.context_directory / "core/models.py").is_file())
        self.assertTrue(self.archive_path.is_file())
        self.assertEqual(sentinel.read_text(), "preserve")

    def test_packager_rejects_top_level_change_during_archive_creation(self):
        binary_directory = self.temporary_root / "top-level-race-tar"
        binary_directory.mkdir()
        wrapper = binary_directory / "tar"
        real_tar = shutil.which("tar")
        self.assertIsNotNone(real_tar)
        _write_tar_race_wrapper(wrapper, real_tar)
        environment = self._tar_environment(wrapper)
        environment["PINRY_INJECT_ROOT_ENTRY"] = "1"

        completed = self._run_packager(environment)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(b"package_layout_changed", completed.stderr)
        self.assertFalse(self.package_directory.exists())
        self.assertFalse(self.archive_path.exists())
        for staging_path in self.temporary_root.glob(
            ".svrx-pinry-server-{}.tmp.*".format(self.short_sha)
        ):
            self.addCleanup(shutil.rmtree, staging_path, ignore_errors=True)

    def test_packager_leaves_no_output_when_archive_publish_fails(self):
        binary_directory = self.temporary_root / "failing-python"
        binary_directory.mkdir()
        wrapper = binary_directory / "python3"
        _write_archive_publish_failure_wrapper(wrapper)

        failed = self._run_packager(self._python_environment(wrapper))

        self.assertNotEqual(failed.returncode, 0)
        self.assertIn(b"publish_destination_raced", failed.stderr)
        self.assertFalse(self.output_root.exists())
        for staging_path in self.temporary_root.glob(
            ".svrx-pinry-server-{}.tmp.*".format(self.short_sha)
        ):
            self.addCleanup(shutil.rmtree, staging_path, ignore_errors=True)

        self._create_package()

        self.assertTrue(self.package_directory.is_dir())
        self.assertTrue(self.archive_path.is_file())

    def test_packager_rejects_replaced_private_publish_sources(self):
        output_root = self.temporary_root / "source-swap-output"
        binary_directory = self.temporary_root / "source-swap-bin"
        binary_directory.mkdir()
        wrapper = binary_directory / "python3"
        _write_publish_source_swap_python_wrapper(wrapper)
        marker = self.temporary_root / "source-swap-marker"
        target = self.temporary_root / "external-target"
        target.mkdir()
        (target / "sentinel").write_text("preserve")
        quarantine = self.temporary_root / "source-quarantine"
        self.addCleanup(shutil.rmtree, quarantine, ignore_errors=True)
        environment = self._python_environment(wrapper)
        environment.update(
            {
                "PINRY_SOURCE_SWAP_DESTINATION": str(output_root.resolve()),
                "PINRY_SOURCE_SWAP_MARKER": str(marker),
                "PINRY_SOURCE_SWAP_TARGET": str(target),
                "PINRY_SOURCE_SWAP_QUARANTINE": str(quarantine),
            }
        )

        completed = self._run_packager_in(
            self.repository_root, output_root, environment
        )

        self.assertTrue(marker.is_file(), completed.stderr.decode("utf-8"))
        self.assertNotEqual(
            completed.returncode, 0, completed.stderr.decode("utf-8")
        )
        self.assertIn(b"publish_destination_raced", completed.stderr)
        self.assertEqual({path.name for path in target.iterdir()}, {"sentinel"})
        self.assertEqual((target / "sentinel").read_text(), "preserve")
        self.assertFalse(output_root.exists())
        staging_symlinks = tuple(
            path
            for path in self.temporary_root.glob(
                ".svrx-pinry-server-{}.tmp.*".format(self.short_sha)
            )
            if path.is_symlink()
        )
        self.assertEqual(len(staging_symlinks), 1)
        self.assertEqual(staging_symlinks[0].resolve(), target.resolve())
        self.addCleanup(staging_symlinks[0].unlink, missing_ok=True)

    def test_packager_does_not_follow_replaced_temporary_archive(self):
        binary_directory = self.temporary_root / "archive-swap-bin"
        binary_directory.mkdir()
        _write_recording_mktemp_wrapper(binary_directory / "mktemp")
        _write_archive_source_swap_tar_wrapper(binary_directory / "tar")
        real_mktemp = shutil.which("mktemp")
        real_tar = shutil.which("tar")
        self.assertIsNotNone(real_mktemp)
        self.assertIsNotNone(real_tar)
        archive_capture = self.temporary_root / "archive-path"
        marker = self.temporary_root / "archive-swap-marker"
        target = self.temporary_root / "external-archive"
        target.write_bytes(b"preserve")
        quarantine = self.temporary_root / "archive-quarantine"
        self.addCleanup(_unlink_if_present, quarantine)
        environment = os.environ.copy()
        environment.update(
            {
                "PATH": "{}{}{}".format(
                    binary_directory,
                    os.pathsep,
                    environment.get("PATH", ""),
                ),
                "PINRY_REAL_MKTEMP": real_mktemp,
                "PINRY_REAL_TAR": real_tar,
                "PINRY_ARCHIVE_PATH_CAPTURE": str(archive_capture),
                "PINRY_ARCHIVE_SWAP_MARKER": str(marker),
                "PINRY_ARCHIVE_SWAP_TARGET": str(target),
                "PINRY_ARCHIVE_SWAP_QUARANTINE": str(quarantine),
            }
        )

        completed = self._run_packager(environment)

        self.assertTrue(marker.is_file(), completed.stderr.decode("utf-8"))
        self.assertNotEqual(
            completed.returncode, 0, completed.stderr.decode("utf-8")
        )
        self.assertEqual(target.read_bytes(), b"preserve")
        self.assertFalse(self.output_root.exists())
        deferred_roots = tuple(
            self.temporary_root.glob(
                ".svrx-pinry-server-{}.tmp.*".format(self.short_sha)
            )
        )
        self.assertEqual(len(deferred_roots), 1)
        self.assertIn(
            "cleanup_deferred={}".format(deferred_roots[0].resolve()).encode(
                "utf-8"
            ),
            completed.stderr,
        )
        self.addCleanup(shutil.rmtree, deferred_roots[0], ignore_errors=True)

    def test_packager_does_not_truncate_replaced_temporary_archive_files(self):
        for index, replacement_kind in enumerate(("regular", "hardlink")):
            with self.subTest(replacement_kind=replacement_kind):
                output_root = self.temporary_root / "archive-file-swap-{}".format(
                    index
                )
                binary_directory = self.temporary_root / "archive-file-bin-{}".format(
                    index
                )
                binary_directory.mkdir()
                _write_recording_mktemp_wrapper(binary_directory / "mktemp")
                python_wrapper = binary_directory / "python3"
                _write_archive_file_swap_python_wrapper(python_wrapper)
                real_mktemp = shutil.which("mktemp")
                self.assertIsNotNone(real_mktemp)
                archive_capture = self.temporary_root / "archive-path-{}".format(
                    index
                )
                marker = self.temporary_root / "archive-swap-marker-{}".format(
                    index
                )
                target = self.temporary_root / "external-archive-{}".format(
                    index
                )
                target.write_bytes(b"preserve")
                quarantine = self.temporary_root / "archive-quarantine-{}".format(
                    index
                )
                self.addCleanup(_unlink_if_present, quarantine)
                environment = self._python_environment(python_wrapper)
                environment.update(
                    {
                        "PINRY_REAL_MKTEMP": real_mktemp,
                        "PINRY_ARCHIVE_PATH_CAPTURE": str(archive_capture),
                        "PINRY_ARCHIVE_SWAP_MARKER": str(marker),
                        "PINRY_ARCHIVE_SWAP_TARGET": str(target),
                        "PINRY_ARCHIVE_SWAP_QUARANTINE": str(quarantine),
                        "PINRY_ARCHIVE_SWAP_KIND": replacement_kind,
                    }
                )

                completed = self._run_packager_in(
                    self.repository_root, output_root, environment
                )

                self.assertTrue(
                    marker.is_file(), completed.stderr.decode("utf-8")
                )
                self.assertNotEqual(
                    completed.returncode,
                    0,
                    completed.stderr.decode("utf-8"),
                )
                replaced_archive = Path(archive_capture.read_text())
                self.assertEqual(replaced_archive.read_bytes(), b"preserve")
                self.assertEqual(target.read_bytes(), b"preserve")
                self.assertFalse(output_root.exists())
                deferred_roots = tuple(
                    self.temporary_root.glob(
                        ".svrx-pinry-server-{}.tmp.*".format(self.short_sha)
                    )
                )
                self.assertEqual(len(deferred_roots), 1)
                self.assertIn(
                    "cleanup_deferred={}".format(
                        deferred_roots[0].resolve()
                    ).encode("utf-8"),
                    completed.stderr,
                )
                self.addCleanup(
                    shutil.rmtree, deferred_roots[0], ignore_errors=True
                )
                shutil.rmtree(deferred_roots[0])

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
                "exports",
                "django_images",
                "pinry",
                "pinry_plugins",
                "users",
                "docker/scripts",
                "docker/migration",
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
        synology_source = (
            self.repository_root / "Dockerfile.autobuild"
        ).read_text()
        root_source = (REPOSITORY_ROOT / "Dockerfile").read_text()
        workflow_source = (
            REPOSITORY_ROOT / ".github/workflows/pythonpackage.yml"
        ).read_text()

        self.assertEqual(
            re.findall(
                r"^FROM python:([^\s]+)", synology_source, re.MULTILINE
            ),
            ["3.14-slim-bookworm", "3.14-slim-bookworm"],
        )
        self.assertEqual(
            re.findall(r"^FROM python:([^\s]+)", root_source, re.MULTILINE),
            ["3.14-bookworm"],
        )
        self.assertEqual(
            re.findall(
                r"python-version:\s*\['([^']+)'\]", workflow_source
            ),
            ["3.14"],
        )
        self.assertNotIn("buster", synology_source)
        self.assertIn("libtiff-dev", synology_source)
        self.assertNotIn("libtiff5-dev", synology_source)
        self.assertNotIn("--install-option", synology_source)
        self.assertNotIn("rcssmin==1.0.6", synology_source)

    def test_synology_database_drivers_are_available_to_runtime_user(self):
        source = (
            self.repository_root / "Dockerfile.autobuild"
        ).read_text()
        stages = re.split(r"(?=^FROM\s+)", source, flags=re.MULTILINE)
        stages = [stage for stage in stages if stage.startswith("FROM ")]
        self.assertEqual(len(stages), 3)

        database_builder = stages[1]
        final_stage = stages[2]
        normalized_builder = re.sub(
            r"[ \t]*\\\n[ \t]*", " ", database_builder
        )
        normalized_final = re.sub(
            r"[ \t]*\\\n[ \t]*", " ", final_stage
        )
        install_match = re.search(
            r"RUN python -m pip install\s+([^\n]+)", normalized_builder
        )
        self.assertIsNotNone(install_match)
        install_arguments = shlex.split(install_match.group(1))
        self.assertIn("--prefix=/install", install_arguments)
        self.assertEqual(
            [
                argument
                for argument in install_arguments
                if argument.startswith(("mysqlclient", "oracledb"))
            ],
            ["mysqlclient==2.2.8", "oracledb==4.0.2"],
        )
        runtime_apt_packages = {
            package
            for arguments in re.findall(
                r"apt-get -y\s+install\s+([^;&\n]+)", normalized_final
            )
            for package in shlex.split(arguments)
        }
        self.assertIn("libmariadb3", runtime_apt_packages)

        runtime_copy = "COPY --from=base /install /usr/local"
        runtime_probe_steps = (
            "account = pwd.getpwnam('www-data')",
            "os.setgroups([])",
            "os.setgid(account.pw_gid)",
            "os.setuid(account.pw_uid)",
            "assert os.geteuid() == account.pw_uid != 0",
            "assert os.getegid() == account.pw_gid != 0",
            "import MySQLdb, oracledb",
        )
        self.assertNotIn("/root/.local", source)
        self.assertEqual(normalized_final.count(runtime_copy), 1)
        for step in runtime_probe_steps:
            self.assertEqual(normalized_final.count(step), 1)
        probe_positions = [
            normalized_final.index(step) for step in runtime_probe_steps
        ]
        self.assertEqual(probe_positions, sorted(probe_positions))
        self.assertLess(
            normalized_final.index(runtime_copy), probe_positions[0]
        )

    def test_frontend_build_declares_async_runtime_and_pins_pnpm(self):
        package = json.loads(
            (self.repository_root / "pinry-spa/package.json").read_text()
        )
        dockerfile = (
            self.repository_root / "Dockerfile.autobuild"
        ).read_text()
        workflow = (
            self.repository_root / ".github/workflows/node.js.yml"
        ).read_text()

        self.assertEqual(
            package["dependencies"]["regenerator-runtime"], "^0.13.9"
        )
        self.assertEqual(package["packageManager"], "pnpm@9.15.9")
        self.assertIn("RUN npm install -g pnpm@9.15.9", dockerfile)
        self.assertIn("RUN pnpm install --frozen-lockfile", dockerfile)
        self.assertIn("run: npm install -g pnpm@9.15.9", workflow)
        self.assertIn("run: pnpm install --frozen-lockfile", workflow)

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

    def test_packager_rejects_publish_destination_swaps_without_touching_targets(self):
        """최종 mv 직전 목적지가 바뀌어도 외부 대상에는 쓰지 않아야 한다."""
        race_kinds = ("directory", "symlink", "dangling-symlink")
        for index, race_kind in enumerate(race_kinds):
            with self.subTest(race_kind=race_kind):
                output_root = self.temporary_root / "publish-race-{}".format(
                    race_kind
                )
                binary_directory = self.temporary_root / "publish-bin-{}".format(
                    race_kind
                )
                binary_directory.mkdir()
                wrapper = binary_directory / "python3"
                _write_publish_race_python_wrapper(wrapper)
                destination = output_root.resolve()
                marker = self.temporary_root / "race-marker-{}".format(index)
                target = self.temporary_root / "external-target-{}".format(
                    index
                )
                mutation = self.temporary_root / "external-mutation-{}".format(
                    index
                )
                if race_kind == "symlink":
                    target.mkdir()
                    (target / "sentinel").write_text("preserve")

                environment = os.environ.copy()
                environment.update(
                    {
                        "PATH": "{}:{}".format(
                            binary_directory, environment["PATH"]
                        ),
                        "PINRY_REAL_PYTHON3": shutil.which("python3"),
                        "PINRY_PUBLISH_RACE_DESTINATION": str(destination),
                        "PINRY_PUBLISH_RACE_KIND": race_kind,
                        "PINRY_PUBLISH_RACE_MARKER": str(marker),
                        "PINRY_PUBLISH_RACE_TARGET": str(target),
                        "PINRY_PUBLISH_RACE_WATCH_PATH": str(
                            destination
                            if race_kind == "directory"
                            else target
                        ),
                        "PINRY_PUBLISH_RACE_MUTATION": str(mutation),
                    }
                )

                completed = self._run_packager_in(
                    self.repository_root, output_root, environment
                )

                self.assertTrue(
                    marker.is_file(), completed.stderr.decode("utf-8")
                )
                self.assertNotEqual(
                    completed.returncode,
                    0,
                    completed.stderr.decode("utf-8"),
                )
                self.assertIn(b"publish_destination_raced", completed.stderr)
                self.assertFalse(mutation.exists())
                if race_kind == "directory":
                    self.assertTrue(destination.is_dir())
                    self.assertEqual(
                        {path.name for path in destination.iterdir()},
                        {"sentinel"},
                    )
                    self.assertEqual(
                        (destination / "sentinel").read_text(), "preserve"
                    )
                elif race_kind == "symlink":
                    self.assertTrue(destination.is_symlink())
                    self.assertEqual(destination.resolve(), target.resolve())
                    self.assertEqual(
                        {path.name for path in target.iterdir()}, {"sentinel"}
                    )
                    self.assertEqual(
                        (target / "sentinel").read_text(), "preserve"
                    )
                else:
                    self.assertTrue(destination.is_symlink())
                    self.assertFalse(target.exists())

                deferred_roots = tuple(
                    path
                    for path in self.temporary_root.glob(
                        ".svrx-pinry-server-{}.tmp.*".format(self.short_sha)
                    )
                    if path.is_dir() and not path.is_symlink()
                )
                self.assertEqual(len(deferred_roots), 1)
                self.assertIn(
                    "cleanup_deferred={}".format(
                        deferred_roots[0].resolve()
                    ).encode("utf-8"),
                    completed.stderr,
                )

                if race_kind == "directory":
                    shutil.rmtree(destination)
                else:
                    destination.unlink()
                retried = self._run_packager_in(
                    self.repository_root, output_root, os.environ.copy()
                )
                self.assertEqual(
                    retried.returncode, 0, retried.stderr.decode("utf-8")
                )
                self.assertEqual(
                    {path.name for path in output_root.iterdir()},
                    {
                        self.package_name,
                        "{}-{}.tar.gz".format(
                            self.package_name, self.short_sha
                        ),
                        "sw-transition",
                        "accept-tools",
                    },
                )
                shutil.rmtree(deferred_roots[0])

    def test_packager_root_controls_are_exact_blobs_from_source_commit(self):
        self._create_package()
        packaged_controls = {
            "build-image.sh": "deploy/synology/build-image.sh",
            "docker-compose.yml": (
                "deploy/synology/docker-compose.synology.yml"
            ),
            "README_KO.md": "deploy/synology/README_KO.md",
            "LICENSE.md": "LICENSE.md",
            "NOTICE.md": "NOTICE.md",
            "UPSTREAM.md": "UPSTREAM.md",
        }
        expected_build_info = (
            "source_commit={}\n"
            "default_image=svrx-pinry:latest\n".format(self.full_sha)
        ).encode("utf-8")
        expected_modes = {
            "BUILD_INFO": 0o644,
            "LICENSE.md": 0o644,
            "NOTICE.md": 0o644,
            "README_KO.md": 0o644,
            "UPSTREAM.md": 0o644,
            "build-image.sh": 0o755,
            "docker-compose.yml": 0o644,
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
        self.assertEqual(
            (self.package_directory / "BUILD_INFO").read_bytes(),
            expected_build_info,
        )
        for packaged_name, expected_mode in expected_modes.items():
            with self.subTest(
                artifact="upload-directory-mode",
                packaged_name=packaged_name,
            ):
                self.assertEqual(
                    (self.package_directory / packaged_name).stat().st_mode
                    & 0o777,
                    expected_mode,
                )

        with tarfile.open(str(self.archive_path), "r:gz") as archive:
            for packaged_name, tracked_path in packaged_controls.items():
                with self.subTest(
                    artifact="archive-bytes",
                    packaged_name=packaged_name,
                ):
                    member = archive.getmember(
                        "{}/{}".format(self.package_name, packaged_name)
                    )
                    extracted = archive.extractfile(member)
                    self.assertIsNotNone(extracted)
                    self.assertEqual(
                        extracted.read(),
                        _git_blob(
                            self.repository_root,
                            self.full_sha,
                            tracked_path,
                        ),
                    )
            build_info_member = archive.getmember(
                "{}/BUILD_INFO".format(self.package_name)
            )
            extracted_build_info = archive.extractfile(build_info_member)
            self.assertIsNotNone(extracted_build_info)
            self.assertEqual(
                extracted_build_info.read(), expected_build_info
            )
            for packaged_name, expected_mode in expected_modes.items():
                with self.subTest(
                    artifact="archive-mode",
                    packaged_name=packaged_name,
                ):
                    member = archive.getmember(
                        "{}/{}".format(self.package_name, packaged_name)
                    )
                    self.assertEqual(member.mode & 0o777, expected_mode)

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

    def test_packager_rejects_dirty_packaged_source_before_output(self):
        for index, relative_path in enumerate(PACKAGED_SOURCE_SENTINELS):
            with self.subTest(relative_path=relative_path):
                repository = self._clone_repository(
                    "dirty-packaged-source-{}".format(index)
                )
                dirty_path = repository / relative_path
                dirty_path.write_bytes(
                    dirty_path.read_bytes() + b"\n# dirty source\n"
                )
                output_root = self.temporary_root / (
                    "dirty-source-output-{}".format(index)
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

    def test_packager_rejects_source_missing_required_context(self):
        required_paths = (
            "docker/scripts/startup.py",
        ) + REQUIRED_SYNOLOGY_CONTEXT_PATHS
        for index, relative_path in enumerate(required_paths):
            with self.subTest(relative_path=relative_path):
                repository = self._clone_repository(
                    "missing-required-context-{}".format(index)
                )
                (repository / relative_path).unlink()
                completed = subprocess.run(
                    [
                        "git",
                        "-c",
                        "user.name=Package Test",
                        "-c",
                        "user.email=package-test@example.invalid",
                        "commit",
                        "--quiet",
                        "-am",
                        "remove required package context",
                    ],
                    cwd=str(repository),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                self.assertEqual(
                    completed.returncode,
                    0,
                    completed.stderr.decode("utf-8"),
                )
                output_root = (
                    self.temporary_root
                    / "missing-required-output-{}".format(index)
                )

                completed = self._run_packager_in(
                    repository,
                    output_root,
                    os.environ.copy(),
                )

                self.assertNotEqual(completed.returncode, 0)
                self.assertIn(
                    b"package_layout_changed",
                    completed.stderr,
                )
                self.assertFalse(
                    (output_root / self.package_name).exists()
                )
                self.assertFalse(
                    (output_root / "sw-transition").exists()
                )

    def test_packager_rejects_required_file_symlink_from_source(self):
        repository = self._clone_repository("symlinked-required-file")
        required = repository / "docker/scripts/supervisor.py"
        required.unlink()
        required.symlink_to("migration_status.py")
        self._commit_all(repository, "replace required file with symlink")
        output_root = self.temporary_root / "symlinked-file-output"

        completed = self._run_packager_in(
            repository,
            output_root,
            os.environ.copy(),
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(b"package_layout_changed", completed.stderr)
        self.assertTrue(required.is_symlink())
        self.assertFalse((output_root / self.package_name).exists())
        self.assertFalse((output_root / "sw-transition").exists())

    def test_packager_rejects_required_parent_symlink_from_source(self):
        repository = self._clone_repository("symlinked-required-parent")
        migration = repository / "docker/migration"
        replacement = repository / "docker/scripts/migration-source"
        shutil.copytree(migration, replacement)
        shutil.rmtree(migration)
        migration.symlink_to("scripts/migration-source", target_is_directory=True)
        self._commit_all(repository, "replace required parent with symlink")
        output_root = self.temporary_root / "symlinked-parent-output"

        completed = self._run_packager_in(
            repository,
            output_root,
            os.environ.copy(),
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(b"package_layout_changed", completed.stderr)
        self.assertTrue(migration.is_symlink())
        self.assertFalse((output_root / self.package_name).exists())
        self.assertFalse((output_root / "sw-transition").exists())

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
            "deploy/synology/README_KO.md",
            "LICENSE.md",
            "NOTICE.md",
            "UPSTREAM.md",
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
            "default_image=svrx-pinry:latest\n".format(source_commit),
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
            "README_KO.md": "deploy/synology/README_KO.md",
            "LICENSE.md": "LICENSE.md",
            "NOTICE.md": "NOTICE.md",
            "UPSTREAM.md": "UPSTREAM.md",
        }
        for packaged_name, relative_path in packaged_controls.items():
            self.assertEqual(
                (package_directory / packaged_name).read_bytes(),
                _git_blob(repository, source_commit, relative_path),
            )
        self.assertTrue(
            (
                output_root
                / "svrx-pinry-{}.tar.gz".format(source_commit[:12])
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
                "svrx-pinry:latest",
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
                "registry.local/svrx-pinry:nas",
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
            "registry.local/svrx-pinry:nas",
        )

    def test_build_rejects_every_malformed_build_info_without_docker(self):
        self._create_package()
        environment, capture, _working_directory = (
            self._docker_environment()
        )
        build_info = self.package_directory / "BUILD_INFO"
        sentinel = self.temporary_root / "shell-payload-ran"
        valid_default = "default_image=svrx-pinry:latest\n"
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
                "default_image=svrx-pinry:latest\n"
                "default_image=svrx-pinry:other\n".format(self.full_sha)
            ),
            "duplicate-empty-default-image": (
                "source_commit={}\n"
                "default_image=svrx-pinry:latest\n"
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
            "default_image=svrx-pinry:latest\n".format("\u00e9" * 40)
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

    def test_build_rejects_missing_exports_directory_before_docker(self):
        self._create_package()
        exports_directory = self.context_directory / "exports"
        hidden = self.context_directory / "exports.missing"
        exports_directory.rename(hidden)
        self.addCleanup(hidden.rename, exports_directory)
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
        self.assertIn(b"missing_build_input=exports", completed.stderr)
        self.assertFalse(capture.exists())
        self.assertEqual(_docker_call_count(capture), 0)

    def test_build_rejects_each_missing_startup_runtime_before_docker(self):
        self._create_package()
        environment, capture, _working_directory = (
            self._docker_environment()
        )

        for relative_path in REQUIRED_STARTUP_PATHS:
            with self.subTest(relative_path=relative_path):
                required = self.context_directory / relative_path
                hidden = required.with_name(required.name + ".missing")
                required.rename(hidden)
                try:
                    completed = subprocess.run(
                        [
                            "sh",
                            str(self.package_directory / "build-image.sh"),
                        ],
                        env=environment,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                finally:
                    hidden.rename(required)

                self.assertNotEqual(completed.returncode, 0)
                self.assertIn(
                    "missing_build_input={}".format(relative_path).encode(
                        "ascii"
                    ),
                    completed.stderr,
                )
                self.assertFalse(capture.exists())
                self.assertEqual(_docker_call_count(capture), 0)

    def test_build_rejects_incomplete_synology_context_before_docker(self):
        self._create_package()
        for relative_path in REQUIRED_SYNOLOGY_CONTEXT_PATHS:
            required = self.context_directory / relative_path
            required.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.repository_root / relative_path, required)
        environment, capture, _working_directory = (
            self._docker_environment()
        )

        for relative_path in REQUIRED_SYNOLOGY_CONTEXT_PATHS:
            with self.subTest(relative_path=relative_path):
                for artifact in (
                    capture,
                    Path(str(capture) + ".calls"),
                ):
                    if artifact.exists():
                        artifact.unlink()
                required = self.context_directory / relative_path
                hidden = required.with_name(required.name + ".missing")
                required.rename(hidden)
                try:
                    completed = subprocess.run(
                        [
                            "sh",
                            str(self.package_directory / "build-image.sh"),
                        ],
                        env=environment,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                finally:
                    hidden.rename(required)

                self.assertNotEqual(completed.returncode, 0)
                if relative_path in REQUIRED_STARTUP_PATHS:
                    expected_error = "missing_build_input={}".format(
                        relative_path
                    ).encode("ascii")
                else:
                    expected_error = b"synology_context_incomplete"
                self.assertIn(expected_error, completed.stderr)
                self.assertFalse(capture.exists())
                self.assertEqual(_docker_call_count(capture), 0)

    def test_build_rejects_non_file_synology_context_before_docker(self):
        self._create_package()
        relative_path = "docker/migration/migration.js"
        required = self.context_directory / relative_path
        required.unlink()
        required.mkdir()
        self.addCleanup(
            shutil.copy2,
            self.repository_root / relative_path,
            required,
        )
        self.addCleanup(required.rmdir)
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
        self.assertIn(b"synology_context_incomplete", completed.stderr)
        self.assertFalse(capture.exists())
        self.assertEqual(_docker_call_count(capture), 0)

    def test_build_rejects_symlinked_required_inputs_before_docker(self):
        self._create_package()
        environment, capture, _working_directory = (
            self._docker_environment()
        )
        fixtures = (
            (
                self.package_directory / "BUILD_INFO",
                b"missing_build_input=BUILD_INFO",
            ),
            (
                self.context_directory / "docker/scripts/startup.py",
                b"missing_build_input=docker/scripts/startup.py",
            ),
            (
                self.context_directory / "exports",
                b"missing_build_input=exports",
            ),
            (
                self.context_directory
                / "docker/scripts/export_storage_bootstrap.py",
                b"missing_build_input=docker/scripts/"
                b"export_storage_bootstrap.py",
            ),
            (
                self.context_directory / "docker/scripts/export_worker.py",
                b"missing_build_input=docker/scripts/export_worker.py",
            ),
            (
                self.context_directory / "docker/migration/migration.js",
                b"synology_context_incomplete",
            ),
        )

        for index, (required, expected_error) in enumerate(fixtures):
            with self.subTest(required=str(required)):
                for artifact in (
                    capture,
                    Path(str(capture) + ".calls"),
                ):
                    if artifact.exists():
                        artifact.unlink()
                hidden = required.with_name(
                    "{}.symlink-target-{}".format(required.name, index)
                )
                required.rename(hidden)
                required.symlink_to(hidden.name)
                try:
                    completed = subprocess.run(
                        [
                            "sh",
                            str(self.package_directory / "build-image.sh"),
                        ],
                        env=environment,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                finally:
                    required.unlink()
                    hidden.rename(required)

                self.assertNotEqual(completed.returncode, 0)
                self.assertIn(expected_error, completed.stderr)
                self.assertFalse(capture.exists())
                self.assertEqual(_docker_call_count(capture), 0)

    def test_build_rejects_symlinked_context_parent_before_docker(self):
        self._create_package()
        migration = self.context_directory / "docker/migration"
        replacement = self.context_directory / "docker/migration.real"
        migration.rename(replacement)
        migration.symlink_to(replacement.name, target_is_directory=True)
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
        self.assertIn(b"synology_context_incomplete", completed.stderr)
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
