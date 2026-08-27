import importlib.util
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _timeout_values(arguments):
    values = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument in ("--timeout", "-t"):
            if index + 1 >= len(arguments):
                raise AssertionError("timeout option is missing its value")
            values.append(arguments[index + 1])
            index += 2
            continue
        if argument.startswith("--timeout="):
            values.append(argument.split("=", 1)[1])
        elif argument.startswith("-t"):
            value = argument[2:]
            if value.startswith("="):
                value = value[1:]
            if not value:
                raise AssertionError("timeout option is missing its value")
            values.append(value)
        index += 1
    return values


def _write_argv_recorder(path):
    path.write_text(
        "#!/bin/sh\n"
        ": > \"$PINRY_ARGV_CAPTURE\"\n"
        "for argument in \"$@\"; do\n"
        "    printf '%s\\0' \"$argument\" >> \"$PINRY_ARGV_CAPTURE\"\n"
        "done\n"
    )
    path.chmod(0o700)


def _read_recorded_argv(path):
    return [
        value.decode("utf-8")
        for value in path.read_bytes().split(b"\0")
        if value
    ]


def _write_startup_recorder(path, command_name):
    path.write_text(
        "#!/bin/sh\n"
        "{\n"
        "    printf '%s' '" + command_name + "'\n"
        "    for argument in \"$@\"; do\n"
        "        printf '\\0%s' \"$argument\"\n"
        "    done\n"
        "    printf '\\n'\n"
        "} >> \"$PINRY_STARTUP_CAPTURE\"\n"
        "if [ '" + command_name + "' = python ] "
        "&& [ -n \"${PINRY_ENV_CAPTURE:-}\" ]; then\n"
        "    printf '%s' \"${PYTHONDONTWRITEBYTECODE:-}\" "
        "> \"$PINRY_ENV_CAPTURE\"\n"
        "fi\n"
        "if [ '" + command_name + "' = bash ] "
        "&& [ \"${PINRY_FAIL_BOOTSTRAP:-0}\" = 1 ]; then\n"
        "    exit 43\n"
        "fi\n"
        "if [ '" + command_name + "' = python ] "
        "&& [ \"${PINRY_FAIL_MIGRATE:-0}\" = 1 ] "
        "&& [ \"${2:-}\" = migrate ]; then\n"
        "    exit 41\n"
        "fi\n"
    )
    path.chmod(0o700)


def _read_startup_events(path):
    return [
        [value.decode("utf-8") for value in line.split(b"\0")]
        for line in path.read_bytes().splitlines()
        if line
    ]


def _line_indent(line):
    if "\t" in line[: len(line) - len(line.lstrip())]:
        raise AssertionError("tabs are not accepted in Compose indentation")
    return len(line) - len(line.lstrip(" "))


def _unique_mapping_line(lines, name, indent, start, end):
    expected = "{}{}:".format(" " * indent, name)
    matches = [
        index
        for index in range(start, end)
        if lines[index].rstrip() == expected
    ]
    if len(matches) != 1:
        raise AssertionError("expected one {} mapping".format(name))
    return matches[0]


def _mapping_end(lines, start, indent, end=None):
    limit = len(lines) if end is None else end
    for index in range(start + 1, limit):
        if lines[index].strip() and _line_indent(lines[index]) <= indent:
            return index
    return limit


def _compose_web_command(source):
    lines = source.splitlines()
    services = _unique_mapping_line(lines, "services", 0, 0, len(lines))
    services_end = _mapping_end(lines, services, 0)
    web = _unique_mapping_line(lines, "web", 2, services + 1, services_end)
    web_end = _mapping_end(lines, web, 2, services_end)
    command_lines = [
        index
        for index in range(web + 1, web_end)
        if _line_indent(lines[index]) == 4
        and lines[index].lstrip().startswith("command:")
    ]
    if len(command_lines) != 1:
        raise AssertionError("expected one direct web command")
    command = command_lines[0]
    if lines[command].strip() != "command: >":
        raise AssertionError("web command must be a folded block scalar")
    body = []
    for index in range(command + 1, web_end):
        line = lines[index]
        if line.strip() and _line_indent(line) <= 4:
            break
        if line.strip():
            body.append(line.strip())
    if not body:
        raise AssertionError("web command folded block is empty")
    return " ".join(body)


def _nginx_tokens(source):
    without_comments = "\n".join(
        line.split("#", 1)[0] for line in source.splitlines()
    )
    return re.findall(r"[{};]|[^\s{};]+", without_comments)


def _parse_nginx_block(tokens, index=0, closing=False):
    directives = []
    while index < len(tokens):
        if tokens[index] == "}":
            if not closing:
                raise AssertionError("unexpected closing brace")
            return directives, index + 1
        words = []
        while index < len(tokens) and tokens[index] not in ("{", "}", ";"):
            words.append(tokens[index])
            index += 1
        if not words or index >= len(tokens) or tokens[index] == "}":
            raise AssertionError("malformed nginx directive")
        terminator = tokens[index]
        index += 1
        if terminator == ";":
            directives.append((words[0], words[1:], None))
            continue
        children, index = _parse_nginx_block(tokens, index, closing=True)
        directives.append((words[0], words[1:], children))
    if closing:
        raise AssertionError("missing closing brace")
    return directives, index


def _direct_values(block, name):
    return [
        arguments
        for directive, arguments, children in block
        if directive == name and children is None
    ]


def _walk_nginx_directives(block):
    for directive in block:
        yield directive
        if directive[2] is not None:
            for child in _walk_nginx_directives(directive[2]):
                yield child


def _assert_nginx_contract(source):
    parsed, final_index = _parse_nginx_block(_nginx_tokens(source))
    if final_index != len(_nginx_tokens(source)):
        raise AssertionError("nginx source was not fully parsed")
    servers = [
        children
        for name, _arguments, children in parsed
        if name == "server" and children is not None
    ]
    if len(servers) != 1:
        raise AssertionError("expected one server block")
    server = servers[0]
    if _direct_values(server, "client_max_body_size") != [["50M"]]:
        raise AssertionError("server upload limit must remain 50M")
    locations = [
        (arguments, children)
        for name, arguments, children in server
        if name == "location" and children is not None
    ]
    exact = [children for arguments, children in locations if arguments == [
        "=", "/api/v2/pins/batch/"
    ]]
    prefix = [
        children for arguments, children in locations if arguments == ["/api"]
    ]
    if len(exact) != 1 or len(prefix) != 1:
        raise AssertionError("expected one exact batch and one /api location")
    batch = exact[0]
    if _direct_values(batch, "client_max_body_size") != [["1m"]]:
        raise AssertionError("batch body limit must be exactly 1m")
    if _direct_values(batch, "proxy_read_timeout") != [["65s"]]:
        raise AssertionError("batch read timeout must be exactly 65s")
    if _direct_values(batch, "proxy_send_timeout") != [["65s"]]:
        raise AssertionError("batch send timeout must be exactly 65s")
    if _direct_values(batch, "proxy_pass") != [["http://localhost:8000"]]:
        raise AssertionError("batch proxy_pass must be direct and unique")
    if _direct_values(batch, "break") != [[]]:
        raise AssertionError("batch break must be direct and unique")
    expected_headers = {
        ("Host", "$host"),
        ("X-Real-IP", "$remote_addr"),
        ("X-Forwarded-For", "$remote_addr"),
    }
    headers = _direct_values(batch, "proxy_set_header")
    if len(headers) != 3 or {tuple(value) for value in headers} != (
        expected_headers
    ):
        raise AssertionError("batch proxy headers are incomplete or duplicated")
    if _direct_values(prefix[0], "client_max_body_size"):
        raise AssertionError("the /api prefix must inherit the 50M limit")
    one_megabyte_limits = [
        arguments
        for name, arguments, children in _walk_nginx_directives(parsed)
        if name == "client_max_body_size"
        and children is None
        and len(arguments) == 1
        and arguments[0].lower() == "1m"
    ]
    if len(one_megabyte_limits) != 1:
        raise AssertionError("the exact batch location must own the only 1m limit")


class RuntimeConfigTests(unittest.TestCase):
    def test_startup_uses_shared_status_authority_and_supervisor(self):
        startup_path = REPOSITORY_ROOT / "docker/scripts/startup.py"
        spec = importlib.util.spec_from_file_location(
            "test_startup_supervisor_entry",
            str(startup_path),
        )
        startup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(startup)
        migration_status = importlib.import_module(
            "docker.scripts.migration_status"
        )

        self.assertIs(
            startup.ERROR_CODE_CLASSES,
            migration_status.ERROR_CODE_CLASSES,
        )
        source = startup_path.read_text("utf-8")
        self.assertNotIn("_SAFE_ERROR_CODES", source)
        self.assertNotIn("_SAFE_BOOTSTRAP_ERROR_CODES", source)

    def test_startup_installs_signal_handlers_before_supervisor_run(self):
        startup_path = REPOSITORY_ROOT / "docker/scripts/startup.py"
        spec = importlib.util.spec_from_file_location(
            "test_startup_signal_order",
            str(startup_path),
        )
        startup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(startup)
        events = []
        runtime = mock.Mock()
        runtime.run.side_effect = lambda: events.append("run") or 7

        def install(signum, handler):
            del handler
            events.append("signal:{}".format(signum))
            return signal.SIG_DFL

        with mock.patch.object(
            startup, "_service_identity", return_value=(33, 33)
        ), mock.patch.object(
            startup, "MigrationStatusStore", return_value=object()
        ), mock.patch.object(
            startup, "RuntimeSupervisor", return_value=runtime
        ), mock.patch.object(startup.signal, "signal", side_effect=install):
            result = startup._run(["--migrate-legacy"])

        self.assertEqual(result, 7)
        self.assertLess(events.index("signal:{}".format(signal.SIGTERM)),
                        events.index("run"))
        self.assertLess(events.index("signal:{}".format(signal.SIGINT)),
                        events.index("run"))

    def _import_docker_settings(self, local_secret, environment_secret=None):
        script = (
            "import sys, types\n"
            "local_settings = types.ModuleType("
            "'pinry.settings.local_settings')\n"
            "local_settings.SECRET_KEY = {!r}\n"
            "sys.modules['pinry.settings.local_settings'] = local_settings\n"
            "from pinry.settings import docker\n"
            "print(docker.SECRET_KEY)\n"
        ).format(local_secret)
        environment = os.environ.copy()
        if environment_secret is None:
            environment.pop("SECRET_KEY", None)
        else:
            environment["SECRET_KEY"] = environment_secret
        return subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(REPOSITORY_ROOT),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_docker_settings_do_not_warn_for_effective_local_secret(self):
        completed = self._import_docker_settings("effective-local-secret")

        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        self.assertEqual(completed.stdout.decode().strip(), "effective-local-secret")
        self.assertEqual(completed.stderr, b"")

    def test_docker_settings_warn_for_effective_placeholder_secret(self):
        completed = self._import_docker_settings(
            "PLEASE_REPLACE_ME",
            environment_secret="overridden-environment-secret",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        self.assertEqual(completed.stdout.decode().strip(), "PLEASE_REPLACE_ME")
        self.assertIn(b"SECRET_KEY", completed.stderr)

    def _capture_environment(self, binary_name):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        binary_directory = Path(temporary.name) / "bin"
        binary_directory.mkdir()
        capture = Path(temporary.name) / "argv.bin"
        _write_argv_recorder(binary_directory / binary_name)
        environment = os.environ.copy()
        environment["PATH"] = "{}{}{}".format(
            binary_directory,
            os.pathsep,
            environment.get("PATH", ""),
        )
        environment["PINRY_ARGV_CAPTURE"] = str(capture)
        return environment, capture

    def _startup_environment(self, database_exists):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        temporary_root = Path(temporary.name)
        binary_directory = temporary_root / "bin"
        binary_directory.mkdir()
        capture = temporary_root / "startup-events.bin"
        for command_name in (
            "bash",
            "python",
            "chown",
            "nginx",
            "gunicorn",
        ):
            _write_startup_recorder(
                binary_directory / command_name, command_name
            )
        data_directory = temporary_root / "data"
        data_directory.mkdir()
        database_path = data_directory / "production.db"
        if database_exists:
            database_path.write_bytes(b"")
        startup_source = (
            REPOSITORY_ROOT / "docker/scripts/start.sh"
        ).read_text()
        self.assertEqual(startup_source.count('PROJECT_ROOT="/pinry"'), 1)
        startup_directory = temporary_root / "docker/scripts"
        startup_directory.mkdir(parents=True)
        startup_script = startup_directory / "start.sh"
        startup_script.write_text(
            startup_source.replace(
                'PROJECT_ROOT="/pinry"',
                'PROJECT_ROOT="{}"'.format(temporary_root),
                1,
            )
            .replace("/data/production.db", str(database_path))
        )
        shutil.copy2(
            REPOSITORY_ROOT / "docker/scripts/_start_gunicorn.sh",
            startup_directory / "_start_gunicorn.sh",
        )
        environment = os.environ.copy()
        environment["PATH"] = "{}{}{}".format(
            binary_directory,
            os.pathsep,
            environment.get("PATH", ""),
        )
        environment["PINRY_STARTUP_CAPTURE"] = str(capture)
        return environment, capture, startup_script

    def _python_runner_environment(self, real_bootstrap=False):
        temporary = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        project_root = Path(temporary.name, "project")
        scripts = project_root / "docker/scripts"
        scripts.mkdir(parents=True)
        runner_source = (
            REPOSITORY_ROOT / "docker/scripts/startup.py"
        ).read_text("utf-8")
        nginx = Path(temporary.name, "nginx")
        nginx.write_text(
            "#!/bin/sh\n"
            "printf 'nginx\\n' >> \"$PINRY_STARTUP_CAPTURE\"\n"
            "if [ \"${PINRY_FAIL_POINT:-}\" = nginx ]; then exit 42; fi\n"
        )
        nginx.chmod(0o700)
        runner = scripts / "startup.py"
        runner.write_text(runner_source.replace(
            'NGINX_BINARY = "/usr/sbin/nginx"',
            'NGINX_BINARY = "{}"'.format(nginx),
            1,
        ))
        bootstrap = scripts / "bootstrap.sh"
        if real_bootstrap:
            for name in (
                "bootstrap.sh",
                "gen_key.sh",
                "normalize_persistent_file.py",
            ):
                shutil.copy2(
                    REPOSITORY_ROOT / "docker/scripts" / name,
                    scripts / name,
                )
        else:
            bootstrap.write_text(
                "#!/bin/sh\n"
                "printf 'bootstrap\\n' >> \"$PINRY_STARTUP_CAPTURE\"\n"
                "if [ \"${PINRY_FAIL_POINT:-}\" = bootstrap ]; then\n"
                "    printf '%s\\n' "
                "\"${PINRY_BOOTSTRAP_REASON:-sentinel-private-bootstrap-secret}\" "
                ">&2\n"
                "    exit 42\n"
                "fi\n"
                "while [ -n \"${PINRY_BOOTSTRAP_GATE:-}\" ] "
                "&& [ -e \"$PINRY_BOOTSTRAP_GATE\" ]; do\n"
                "    sleep 0.05\n"
                "done\n"
            )
            bootstrap.chmod(0o700)
        gunicorn = scripts / "_start_gunicorn.sh"
        gunicorn.write_text(
            "#!/bin/sh\n"
            "printf 'gunicorn\\n' >> \"$PINRY_STARTUP_CAPTURE\"\n"
            "if [ \"${PINRY_ASSERT_WSGI_IMPORT:-0}\" = 1 ]; then\n"
            "    \"$PINRY_REAL_PYTHON\" -c 'import pinry.wsgi'\n"
            "fi\n"
            "while [ -n \"${PINRY_LIFETIME_GATE:-}\" ] "
            "&& [ -e \"$PINRY_LIFETIME_GATE\" ]; do\n"
            "    sleep 0.05\n"
            "done\n"
        )
        gunicorn.chmod(0o700)

        def write_module(relative, source):
            path = project_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source)

        event_source = (
            "import os\n"
            "def event(value):\n"
            "    with open(os.environ['PINRY_STARTUP_CAPTURE'], 'a') as out:\n"
            "        out.write(value + '\\n')\n"
        )
        write_module("runner_events.py", event_source)
        write_module(
            "django/__init__.py",
            "import os\n"
            "from runner_events import event\n"
            "def setup():\n"
            "    event('setup:' + os.environ.get('DJANGO_SETTINGS_MODULE', ''))\n"
            "    if os.environ.get('PINRY_FAIL_POINT') == 'setup':\n"
            "        raise RuntimeError('sentinel-secret-db-credential')\n",
        )
        write_module("django/core/__init__.py", "")
        write_module(
            "django/core/management.py",
            "import os\n"
            "from runner_events import event\n"
            "def call_command(name, *args, **kwargs):\n"
            "    del args, kwargs\n"
            "    event(name)\n"
            "    if os.environ.get('PINRY_FAIL_POINT') == name:\n"
            "        raise RuntimeError('sentinel-private-command-value')\n",
        )
        write_module(
            "django/conf/__init__.py",
            "import os\n"
            "class Settings(object):\n"
            "    PINRY_DATA_ROOT = os.environ['PINRY_DATA_ROOT']\n"
            "settings = Settings()\n",
        )
        write_module("django_images/__init__.py", "")
        write_module("django_images/services/__init__.py", "")
        write_module(
            "django_images/services/startup_lock.py",
            "import errno\n"
            "import fcntl\n"
            "import os\n"
            "from runner_events import event\n"
            "class StartupLockError(Exception):\n"
            "    def __init__(self, code):\n"
            "        super(StartupLockError, self).__init__(code)\n"
            "        self.code = code\n"
            "class Held(object):\n"
            "    def __init__(self, descriptor): self.descriptor = descriptor\n"
            "    def fileno(self): return self.descriptor\n"
            "    def set_inheritable(self, value):\n"
            "        event('lock_inheritable')\n"
            "        os.set_inheritable(self.descriptor, value)\n"
            "def acquire_startup_lock(root, service_uid, service_gid):\n"
            "    if service_uid < 0 or service_gid < 0: raise AssertionError('invalid service identity')\n"
            "    descriptor = os.open(os.path.join(root, '.svrx-pinry-startup.lock'), os.O_RDWR | os.O_CREAT, 0o600)\n"
            "    os.set_inheritable(descriptor, False)\n"
            "    try:\n"
            "        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "    except OSError as error:\n"
            "        os.close(descriptor)\n"
            "        if error.errno in (errno.EACCES, errno.EAGAIN):\n"
            "            raise StartupLockError('startup_lock_busy')\n"
            "        raise StartupLockError('startup_lock_failed')\n"
            "    event('lock')\n"
            "    return Held(descriptor)\n",
        )
        write_module(
            "django_images/services/migration_state.py",
            "import os\n"
            "from runner_events import event\n"
            "class Inventory(object):\n"
            "    requires_migration_flag = os.environ.get('PINRY_INVENTORY_BLOCK') == '1'\n"
            "def inspect_run_inventory_read_only(path):\n"
            "    del path\n"
            "    event('inventory')\n"
            "    return Inventory()\n",
        )
        write_module(
            "django_images/services/legacy_startup.py",
            "import os\n"
            "from runner_events import event\n"
            "class Failure(Exception):\n"
            "    def __init__(self, code):\n"
            "        super(Failure, self).__init__('sentinel-private-error')\n"
            "        self.code = code\n"
            "def fail(point, code):\n"
            "    if os.environ.get('PINRY_FAIL_POINT') == point: raise Failure(code)\n"
            "class LegacyStartupCoordinator(object):\n"
            "    def __init__(self, uid, gid, progress_reporter=None): "
            "del uid, gid, progress_reporter; event('coordinator')\n"
            "    def prepare_no_flag_before_schema(self):\n"
            "        event('prepare_no_flag'); fail('prepare_no_flag', 'legacy_migration_flag_required')\n"
            "    def prepare_before_schema(self):\n"
            "        event('prepare'); fail('prepare', 'sqlite_snapshot_failed'); return object()\n"
            "    def prepare_migration_locks(self, run):\n"
            "        del run; event('prepare_migration_locks'); "
            "fail('prepare_migration_locks', 'media_lifecycle_lock_failed')\n"
            "    def schema_required(self, run):\n"
            "        del run; return os.environ.get('PINRY_SCHEMA_REQUIRED', '1') == '1'\n"
            "    def converge_after_schema(self, run):\n"
            "        event('converge:none' if run is None else 'converge:run')\n"
            "        fail('converge', 'migration_state_plan_mismatch')\n"
            "    def adjust_ownership(self, descriptor):\n"
            "        del descriptor; event('ownership'); fail('ownership', 'unsafe_storage_ownership')\n"
            "    def runtime_check(self, uid, gid):\n"
            "        del uid, gid; event('runtime'); fail('runtime', 'media_root_not_writable')\n",
        )
        write_module("pinry/__init__.py", "")
        write_module(
            "pinry/wsgi.py",
            "import os\n"
            "from runner_events import event\n"
            "event('wsgi_import:' + os.getcwd())\n",
        )
        write_module("pinry/settings/__init__.py", "")
        write_module("pinry/settings/docker.py", "")
        if real_bootstrap:
            shutil.copy2(
                REPOSITORY_ROOT
                / "pinry/settings/local_settings.example.py",
                project_root / "pinry/settings/local_settings.example.py",
            )

        data_root = Path(temporary.name, "data")
        data_root.mkdir(mode=0o700)
        capture = Path(temporary.name, "runner-events.txt")
        environment = os.environ.copy()
        environment.update({
            "PINRY_DATA_ROOT": str(data_root),
            "PINRY_PROJECT_ROOT": str(project_root),
            "PINRY_STARTUP_CAPTURE": str(capture),
            "PYTHONPATH": "",
            "PINRY_REAL_PYTHON": str(REPOSITORY_ROOT / ".venv/bin/python"),
        })
        if real_bootstrap:
            binary_directory = Path(temporary.name, "bootstrap-bin")
            binary_directory.mkdir()
            identity = binary_directory / "id"
            identity.write_text(
                "#!/bin/sh\n"
                "if [ \"${1:-}\" = -u ] && [ \"${2:-}\" = www-data ]; then\n"
                "    printf '%s\\n' '" + str(os.getuid()) + "'\n"
                "    exit 0\n"
                "fi\n"
                "if [ \"${1:-}\" = -g ] && [ \"${2:-}\" = www-data ]; then\n"
                "    printf '%s\\n' '"
                + str(project_root.stat().st_gid)
                + "'\n"
                "    exit 0\n"
                "fi\n"
                "exec \"" + str(shutil.which("id")) + "\" \"$@\"\n"
            )
            identity.chmod(0o700)
            chown = binary_directory / "chown"
            chown.write_text("#!/bin/sh\nexit 0\n")
            chown.chmod(0o700)
            pwgen = binary_directory / "pwgen"
            pwgen.write_text("#!/bin/sh\nprintf '%065d\\n' 0\n")
            pwgen.chmod(0o700)
            environment["PATH"] = "{}{}{}".format(
                binary_directory,
                os.pathsep,
                environment.get("PATH", ""),
            )
        environment.pop("DJANGO_SETTINGS_MODULE", None)
        return environment, capture, runner, data_root

    @staticmethod
    def _runner_events(capture):
        if not capture.exists():
            return []
        return capture.read_text("utf-8").splitlines()

    def test_start_shell_delegates_directly_to_python_runner(self):
        for database_exists in (False, True):
            with self.subTest(database_exists=database_exists):
                environment, capture, startup_script = (
                    self._startup_environment(database_exists)
                )

                completed = subprocess.run(
                    ["/bin/bash", str(startup_script)],
                    cwd=str(REPOSITORY_ROOT),
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )

                self.assertEqual(
                    completed.returncode,
                    0,
                    completed.stderr.decode("utf-8"),
                )
                events = _read_startup_events(capture)
                self.assertEqual(events, [[
                    "python",
                    str(startup_script.parent / "startup.py"),
                ]])

    def test_start_shell_never_runs_bootstrap_outside_lifetime_lock(self):
        environment, capture, startup_script = self._startup_environment(
            False
        )
        environment["PINRY_FAIL_BOOTSTRAP"] = "1"

        completed = subprocess.run(
            ["/bin/bash", str(startup_script)],
            cwd=str(REPOSITORY_ROOT),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(_read_startup_events(capture), [[
            "python",
            str(startup_script.parent / "startup.py"),
        ]])

    def test_start_rejects_every_non_exact_argument_before_bootstrap(self):
        cases = (
            ("--migrate-legacy", "--migrate-legacy"),
            ("--unknown",),
            ("--migrate-legacy=value",),
            ("--migrate-legacy", "value"),
            ("--",),
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                environment, capture, startup_script = (
                    self._startup_environment(False)
                )

                completed = subprocess.run(
                    ["/bin/bash", str(startup_script)] + list(arguments),
                    cwd=str(REPOSITORY_ROOT),
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )

                self.assertEqual(completed.returncode, 2)
                self.assertFalse(capture.exists())

    def test_start_passes_the_single_migration_flag_to_runner(self):
        environment, capture, startup_script = self._startup_environment(False)

        completed = subprocess.run(
            ["/bin/bash", str(startup_script), "--migrate-legacy"],
            cwd=str(REPOSITORY_ROOT),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(_read_startup_events(capture), [
            [
                "python",
                str(startup_script.parent / "startup.py"),
                "--migrate-legacy",
            ],
        ])

    def test_start_exports_bytecode_suppression_to_python_runner(self):
        environment, _capture, startup_script = self._startup_environment(
            False
        )
        environment_capture = startup_script.parent / "environment.txt"
        environment["PINRY_ENV_CAPTURE"] = str(environment_capture)
        environment.pop("PYTHONDONTWRITEBYTECODE", None)

        completed = subprocess.run(
            ["/bin/bash", str(startup_script)],
            cwd=str(REPOSITORY_ROOT),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        self.assertEqual(environment_capture.read_text("utf-8"), "1")

    def test_python_runner_rejects_invalid_arguments_before_supervisor(self):
        startup_path = REPOSITORY_ROOT / "docker/scripts/startup.py"
        spec = importlib.util.spec_from_file_location(
            "test_startup_arguments",
            str(startup_path),
        )
        startup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(startup)

        with mock.patch.object(startup, "RuntimeSupervisor") as runtime:
            result = startup._run(["--unknown"])

        self.assertEqual(result, 2)
        runtime.assert_not_called()

    def test_runtime_sources_encode_nginx_lock_worker_gunicorn_order(self):
        supervisor_source = (
            REPOSITORY_ROOT / "docker/scripts/supervisor.py"
        ).read_text("utf-8")
        run_start = supervisor_source.index("    def run(self):")
        run_source = supervisor_source[run_start:]

        self.assertLess(
            run_source.index("self._spawn_nginx()"),
            run_source.index("self._acquire_lock()"),
        )
        self.assertLess(
            run_source.index("self._acquire_lock()"),
            run_source.index("self._spawn_worker("),
        )
        self.assertLess(
            supervisor_source.index("def _spawn_worker"),
            supervisor_source.index("def _spawn_gunicorn"),
        )
        self.assertIn("start_new_session=True", supervisor_source)
        self.assertIn("pass_fds=(writer, lock_fd)", supervisor_source)

    def test_worker_owns_bootstrap_django_schema_and_coordinator(self):
        startup_source = (
            REPOSITORY_ROOT / "docker/scripts/startup.py"
        ).read_text("utf-8")
        worker_source = (
            REPOSITORY_ROOT / "docker/scripts/migration_worker.py"
        ).read_text("utf-8")

        for name in (
            "_run_bootstrap",
            "_setup_django",
            "_run_schema_commands",
            "_run_coordinator",
        ):
            self.assertNotIn("def {}".format(name), startup_source)
            self.assertIn("def {}".format(name), worker_source)
        self.assertNotIn("os.execv", startup_source)
        self.assertNotIn("migration-status.json", worker_source)

    def test_runtime_supervisor_keeps_lifetime_lock_until_cleanup(self):
        source = (
            REPOSITORY_ROOT / "docker/scripts/supervisor.py"
        ).read_text("utf-8")
        finally_source = source[source.rindex("        finally:"):]

        self.assertLess(
            finally_source.index("self._cleanup()"),
            finally_source.index("self.startup_lock.close()"),
        )

    def test_start_script_passes_one_effective_60_second_timeout(self):
        environment, capture = self._capture_environment("gunicorn")
        completed = subprocess.run(
            ["bash", "docker/scripts/_start_gunicorn.sh"],
            cwd=str(REPOSITORY_ROOT),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        arguments = _read_recorded_argv(capture)
        self.assertEqual(_timeout_values(arguments), ["60"])

    def test_gunicorn_start_script_replaces_shell_process(self):
        source = (
            REPOSITORY_ROOT / "docker/scripts/_start_gunicorn.sh"
        ).read_text("utf-8")
        commands = [
            line.strip()
            for line in source.splitlines()
            if line.strip() and not line.startswith("#!")
        ]

        self.assertTrue(commands[0].startswith("exec gunicorn "))
        self.assertIn("-b 127.0.0.1:8000", source)
        self.assertNotIn("-b 0.0.0.0:8000", source)

    def _bootstrap_fixture(self, existing=None):
        temporary = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name, "project")
        scripts = root / "docker/scripts"
        settings_directory = root / "pinry/settings"
        data = Path(temporary.name, "data")
        scripts.mkdir(parents=True)
        settings_directory.mkdir(parents=True)
        data.mkdir()
        secret = "A1" * 32 + "B"
        (scripts / "gen_key.sh").write_text(
            "#!/bin/sh\n"
            "if [ \"${{PINRY_PWGEN_FAIL:-0}}\" = 1 ]; then exit 47; fi\n"
            "printf '%s\\n' '{}' > \"$PINRY_DATA_ROOT/production_secret_key.txt\"\n"
            "chmod 600 \"$PINRY_DATA_ROOT/production_secret_key.txt\"\n"
            "printf '%s\\n' '{}'\n".format(secret, secret)
        )
        (scripts / "gen_key.sh").chmod(0o700)
        (scripts / "normalize_persistent_file.py").write_text(
            (
                REPOSITORY_ROOT
                / "docker/scripts/normalize_persistent_file.py"
            ).read_text("utf-8")
        )
        (settings_directory / "local_settings.example.py").write_text(
            "SECRET_KEY = 'secret_key_place_holder'\n"
        )
        if existing is not None:
            (data / "local_settings.py").write_bytes(existing)
            (data / "local_settings.py").chmod(0o600)
        source = (
            REPOSITORY_ROOT / "docker/scripts/bootstrap.sh"
        ).read_text("utf-8")
        script = scripts / "bootstrap.sh"
        script.write_text(
            source.replace(
                "/pinry/docker/scripts/gen_key.sh",
                str(scripts / "gen_key.sh"),
            ).replace(
                "/pinry/pinry/settings/local_settings.example.py",
                str(settings_directory / "local_settings.example.py"),
            ).replace(
                "/pinry/pinry/settings/local_settings.py",
                str(settings_directory / "local_settings.py"),
            ).replace(
                "/data/local_settings.py",
                str(data / "local_settings.py"),
            )
        )
        script.chmod(0o700)
        binary_directory = Path(temporary.name, "bin")
        binary_directory.mkdir()
        sed = binary_directory / "sed"
        sed.write_text(
            "#!/usr/bin/env python3\n"
            "import pathlib, sys\n"
            "in_place = len(sys.argv) == 4 and sys.argv[1] == '-i'\n"
            "expression = sys.argv[2 if in_place else 1].replace('\\\\_', '_')\n"
            "prefix = 's/secret_key_place_holder/'\n"
            "if not expression.startswith(prefix) or not expression.endswith('/'):\n"
            "    raise SystemExit(2)\n"
            "replacement = expression[len(prefix):-1]\n"
            "target = pathlib.Path(sys.argv[3 if in_place else 2])\n"
            "content = target.read_text().replace('secret_key_place_holder', replacement)\n"
            "if in_place:\n"
            "    target.write_text(content)\n"
            "else:\n"
            "    sys.stdout.write(content)\n"
        )
        sed.chmod(0o700)
        service_uid = os.getuid()
        service_gid = os.getgid()
        real_id = shutil.which("id")
        real_chown = shutil.which("chown")
        real_move = shutil.which("mv")
        real_stat = shutil.which("stat")
        self.assertIsNotNone(real_id)
        self.assertIsNotNone(real_chown)
        self.assertIsNotNone(real_move)
        self.assertIsNotNone(real_stat)
        identity = binary_directory / "id"
        identity.write_text(
            "#!/bin/sh\n"
            "if [ \"$#\" -eq 2 ] && [ \"$2\" = www-data ]; then\n"
            "    if [ \"$1\" = -u ]; then\n"
            "        if [ \"${PINRY_TEST_ID_UID_FAIL:-0}\" = 1 ]; then\n"
            "            exit 44\n"
            "        fi\n"
            "        printf '%s\\n' \"${PINRY_TEST_ID_UID:-"
            + str(service_uid)
            + "}\"\n"
            "        exit 0\n"
            "    fi\n"
            "    if [ \"$1\" = -g ]; then\n"
            "        if [ \"${PINRY_TEST_ID_GID_FAIL:-0}\" = 1 ]; then\n"
            "            exit 45\n"
            "        fi\n"
            "        printf '%s\\n' \"${PINRY_TEST_ID_GID:-"
            + str(service_gid)
            + "}\"\n"
            "        exit 0\n"
            "    fi\n"
            "fi\n"
            "exec \"" + str(real_id) + "\" \"$@\"\n"
        )
        identity.chmod(0o700)
        chown = binary_directory / "chown"
        chown.write_text(
            "#!/bin/sh\n"
            "for argument in \"$@\"; do\n"
            "    printf '%s\\0' \"$argument\" >> \"$PINRY_CHOWN_CAPTURE\"\n"
            "done\n"
            "if [ \"${PINRY_TEST_CHOWN_FAIL:-0}\" = 1 ]; then exit 46; fi\n"
            "[ \"$#\" -eq 2 ] && [ -e \"$2\" ]\n"
            "exec \"" + str(real_chown) + "\" \"$@\"\n"
        )
        chown.chmod(0o700)
        move = binary_directory / "mv"
        move.write_text(
            "#!/bin/sh\n"
            "for argument in \"$@\"; do\n"
            "    printf '%s\\0' \"$argument\" >> \"$PINRY_MV_CAPTURE\"\n"
            "done\n"
            "exec \"" + str(real_move) + "\" \"$@\"\n"
        )
        move.chmod(0o700)
        stat_command = binary_directory / "stat"
        stat_command.write_text(
            "#!/bin/sh\n"
            "if [ \"${PINRY_TEST_STAT_UID_PATH:-}\" = \"${3:-}\" ] "
            "&& [ \"${2:-}\" = %u ]; then\n"
            "    if [ \"${PINRY_TEST_STAT_UID_UNTIL_MODE_600:-0}\" = 1 ]; then\n"
            "        mode=\"$(\"" + str(real_stat) + "\" -c %a \"${3:-}\" "
            "2>/dev/null || \"" + str(real_stat) + "\" -f %Lp \"${3:-}\")\"\n"
            "        if [ \"$mode\" = 600 ]; then\n"
            "            exec \"" + str(real_stat) + "\" \"$@\"\n"
            "        fi\n"
            "    fi\n"
            "    printf '%s\\n' \"$PINRY_TEST_STAT_UID\"\n"
            "    exit 0\n"
            "fi\n"
            "if [ \"${PINRY_TEST_STAT_UID_PATH_2:-}\" = \"${3:-}\" ] "
            "&& [ \"${2:-}\" = %u ]; then\n"
            "    printf '%s\\n' \"$PINRY_TEST_STAT_UID_2\"\n"
            "    exit 0\n"
            "fi\n"
            "exec \"" + str(real_stat) + "\" \"$@\"\n"
        )
        stat_command.chmod(0o700)
        environment = os.environ.copy()
        environment["PATH"] = "{}{}{}".format(
            binary_directory,
            os.pathsep,
            environment.get("PATH", ""),
        )
        environment["PINRY_DATA_ROOT"] = str(data)
        environment["PINRY_PROJECT_ROOT"] = str(root)
        environment["PINRY_CHOWN_CAPTURE"] = str(
            Path(temporary.name, "chown.bin")
        )
        environment["PINRY_MV_CAPTURE"] = str(
            Path(temporary.name, "mv.bin")
        )
        return script, root, data, secret, environment

    def test_bootstrap_accepts_safe_settings_copied_by_synology_user(self):
        existing = b"SECRET_KEY='sentinel-existing-secret'\n"
        script, root, data, _secret, environment = (
            self._bootstrap_fixture(existing)
        )
        data_settings = data / "local_settings.py"
        data_settings.chmod(0o666)
        synology_uid = os.getuid() + 10000
        environment["PINRY_TEST_STAT_UID_PATH"] = str(data_settings)
        environment["PINRY_TEST_STAT_UID"] = str(synology_uid)
        environment["PINRY_TEST_STAT_UID_UNTIL_MODE_600"] = "1"

        completed = subprocess.run(
            ["/bin/bash", str(script)],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        self.assertEqual(data_settings.read_bytes(), existing)
        self.assertEqual(stat.S_IMODE(data_settings.stat().st_mode), 0o600)
        self.assertEqual(data_settings.stat().st_uid, os.getuid())
        self.assertEqual(
            (root / "pinry/settings/local_settings.py").read_bytes(),
            existing,
        )

    def test_bootstrap_normalizes_existing_settings_and_key_together(self):
        existing = b"SECRET_KEY='sentinel-existing-secret'\n"
        script, root, data, secret, environment = (
            self._bootstrap_fixture(existing)
        )
        data_settings = data / "local_settings.py"
        key_path = data / "production_secret_key.txt"
        key_path.write_bytes((secret + "\n").encode("ascii"))
        for path in (data_settings, key_path):
            path.chmod(0o666)

        completed = subprocess.run(
            ["/bin/bash", str(script)],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        self.assertEqual(data_settings.read_bytes(), existing)
        self.assertEqual(
            key_path.read_bytes(),
            (secret + "\n").encode("ascii"),
        )
        for path in (data_settings, key_path):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(path.stat().st_uid, os.getuid())
        self.assertEqual(
            (root / "pinry/settings/local_settings.py").read_bytes(),
            existing,
        )

    def test_bootstrap_reports_invalid_environment_before_file_work(self):
        for invalid_path in ("data_root", "settings_directory"):
            with self.subTest(invalid_path=invalid_path):
                script, root, data, _secret, environment = (
                    self._bootstrap_fixture()
                )
                if invalid_path == "data_root":
                    data.rmdir()
                    data.write_bytes(b"not-a-directory")
                else:
                    shutil.rmtree(root / "pinry/settings")

                completed = subprocess.run(
                    ["/bin/bash", str(script)],
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )

                self.assertNotEqual(completed.returncode, 0)
                self.assertEqual(
                    completed.stderr.decode().strip(),
                    "bootstrap_environment_invalid",
                )

    def test_bootstrap_atomically_installs_service_owned_project_copy_only(self):
        existing = b"SECRET_KEY='sentinel-existing-secret'\n"
        script, root, data, secret, environment = self._bootstrap_fixture(
            existing
        )
        key_path = data / "production_secret_key.txt"
        key_path.write_bytes((secret + "\n").encode("ascii"))
        key_path.chmod(0o600)
        data_settings = data / "local_settings.py"
        persistent_before = {
            path: (
                path.read_bytes(),
                stat.S_IMODE(path.stat().st_mode),
                path.stat().st_uid,
                path.stat().st_gid,
            )
            for path in (data_settings, key_path)
        }

        completed = subprocess.run(
            ["/bin/bash", str(script)],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        for path, expected in persistent_before.items():
            self.assertEqual(
                (
                    path.read_bytes(),
                    stat.S_IMODE(path.stat().st_mode),
                    path.stat().st_uid,
                    path.stat().st_gid,
                ),
                expected,
            )
        project_settings = root / "pinry/settings/local_settings.py"
        self.assertEqual(project_settings.read_bytes(), existing)
        self.assertEqual(stat.S_IMODE(project_settings.stat().st_mode), 0o600)
        self.assertEqual(project_settings.stat().st_uid, os.getuid())
        self.assertEqual(project_settings.stat().st_gid, os.getgid())
        chown_arguments = _read_recorded_argv(Path(
            environment["PINRY_CHOWN_CAPTURE"]
        ))
        self.assertEqual(chown_arguments[0], "{}:{}".format(
            os.getuid(), os.getgid()
        ))
        self.assertEqual(len(chown_arguments), 2)
        project_temp = Path(chown_arguments[1])
        self.assertEqual(project_temp.parent, project_settings.parent)
        self.assertTrue(project_temp.name.startswith(
            ".local_settings.py.tmp-"
        ))
        move_arguments = _read_recorded_argv(Path(
            environment["PINRY_MV_CAPTURE"]
        ))
        self.assertEqual(
            move_arguments[-3:],
            ["-f", str(project_temp), str(project_settings)],
        )
        self.assertEqual(
            list(project_settings.parent.glob(".local_settings.py.tmp-*")),
            [],
        )

    def test_bootstrap_fails_closed_without_service_identity_or_chown(self):
        cases = (
            ("PINRY_TEST_ID_UID_FAIL", "1"),
            ("PINRY_TEST_ID_GID_FAIL", "1"),
            ("PINRY_TEST_ID_UID", "not-a-uid"),
            ("PINRY_TEST_ID_GID", "not-a-gid"),
            ("PINRY_TEST_CHOWN_FAIL", "1"),
        )
        for variable, value in cases:
            with self.subTest(variable=variable):
                existing = b"SECRET_KEY='sentinel-existing-secret'\n"
                script, root, data, _secret, environment = (
                    self._bootstrap_fixture(existing)
                )
                environment[variable] = value
                data_settings = data / "local_settings.py"
                before = (
                    data_settings.read_bytes(),
                    stat.S_IMODE(data_settings.stat().st_mode),
                )

                completed = subprocess.run(
                    ["/bin/bash", str(script)],
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )

                self.assertNotEqual(completed.returncode, 0)
                self.assertEqual(
                    (
                        data_settings.read_bytes(),
                        stat.S_IMODE(data_settings.stat().st_mode),
                    ),
                    before,
                )
                settings_directory = root / "pinry/settings"
                self.assertFalse(
                    (settings_directory / "local_settings.py").exists()
                )
                self.assertEqual(
                    list(settings_directory.glob(
                        ".local_settings.py.tmp-*"
                    )),
                    [],
                )

    def test_bootstrap_stores_generated_secret_without_logging_value(self):
        script, root, data, secret, environment = self._bootstrap_fixture()

        completed = subprocess.run(
            ["/bin/bash", str(script)],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        rendered = (completed.stdout + completed.stderr).decode("utf-8")
        self.assertEqual(completed.returncode, 0, rendered)
        self.assertNotIn(secret, rendered)
        self.assertIn(
            secret,
            (data / "local_settings.py").read_text("utf-8"),
        )
        self.assertEqual(
            (root / "pinry/settings/local_settings.py").read_bytes(),
            (data / "local_settings.py").read_bytes(),
        )
        self.assertEqual(
            (data / "production_secret_key.txt").read_bytes(),
            (secret + "\n").encode("ascii"),
        )
        self.assertNotIn(
            b"secret_key_place_holder",
            (data / "local_settings.py").read_bytes(),
        )
        for path in (
            data / "production_secret_key.txt",
            data / "local_settings.py",
            root / "pinry/settings/local_settings.py",
        ):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_bootstrap_preserves_existing_malformed_local_settings_bytes(self):
        existing = (
            b"SECRET_KEY='sentinel-existing-secret'\n"
            b"raise RuntimeError('sentinel-db-credential')\n"
        )
        script, root, data, _secret, environment = (
            self._bootstrap_fixture(existing)
        )

        completed = subprocess.run(
            ["/bin/bash", str(script)],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        rendered = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 0, rendered)
        self.assertEqual((data / "local_settings.py").read_bytes(), existing)
        self.assertEqual(
            (root / "pinry/settings/local_settings.py").read_bytes(),
            existing,
        )
        self.assertNotIn(b"sentinel-existing-secret", rendered)
        self.assertNotIn(b"sentinel-db-credential", rendered)

    def test_bootstrap_key_generation_failure_is_not_overwritten_by_success(self):
        script, root, data, secret, environment = self._bootstrap_fixture()
        environment["PINRY_PWGEN_FAIL"] = "1"

        completed = subprocess.run(
            ["/bin/bash", str(script)],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        rendered = completed.stdout + completed.stderr
        self.assertNotEqual(completed.returncode, 0)
        self.assertNotIn(secret.encode("ascii"), rendered)
        self.assertFalse((data / "local_settings.py").exists())
        self.assertFalse((root / "pinry/settings/local_settings.py").exists())

    def test_bootstrap_rejects_template_without_exact_placeholder(self):
        script, root, data, secret, environment = self._bootstrap_fixture()
        template = root / "pinry/settings/local_settings.example.py"
        template.write_text("SECRET_KEY = 'missing-placeholder'\n")

        completed = subprocess.run(
            ["/bin/bash", str(script)],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertNotIn(
            secret.encode("ascii"),
            completed.stdout + completed.stderr,
        )
        self.assertFalse((data / "local_settings.py").exists())
        self.assertFalse((root / "pinry/settings/local_settings.py").exists())

    def test_bootstrap_rejects_hardlinked_existing_local_settings(self):
        existing = b"SECRET_KEY='existing-compatible-value'\n"
        script, root, data, _secret, environment = self._bootstrap_fixture(
            existing
        )
        os.link(
            str(data / "local_settings.py"),
            str(data / "local_settings-linked.py"),
        )

        completed = subprocess.run(
            ["/bin/bash", str(script)],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stderr.decode().strip(),
            "bootstrap_persistent_settings_invalid",
        )
        self.assertEqual((data / "local_settings.py").read_bytes(), existing)
        self.assertFalse((root / "pinry/settings/local_settings.py").exists())

    def test_bootstrap_rejects_symlinked_settings_without_touching_target(self):
        script, root, data, _secret, environment = self._bootstrap_fixture()
        target = data.parent / "outside-local-settings.py"
        existing = b"SECRET_KEY='outside-secret'\n"
        target.write_bytes(existing)
        target.chmod(0o644)
        (data / "local_settings.py").symlink_to(target)

        completed = subprocess.run(
            ["/bin/bash", str(script)],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stderr.decode().strip(),
            "bootstrap_persistent_settings_invalid",
        )
        self.assertEqual(target.read_bytes(), existing)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)
        self.assertTrue((data / "local_settings.py").is_symlink())
        self.assertFalse((root / "pinry/settings/local_settings.py").exists())

    def test_bootstrap_rejects_placeholder_before_replacing_source(self):
        existing = b"SECRET_KEY='secret_key_place_holder'\n"
        script, root, data, _secret, environment = (
            self._bootstrap_fixture(existing)
        )
        data_settings = data / "local_settings.py"
        data_settings.chmod(0o666)

        completed = subprocess.run(
            ["/bin/bash", str(script)],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(data_settings.read_bytes(), existing)
        self.assertEqual(stat.S_IMODE(data_settings.stat().st_mode), 0o666)
        self.assertEqual(
            list(data.glob(".local_settings.py.tmp-*")),
            [],
        )
        self.assertFalse((root / "pinry/settings/local_settings.py").exists())

    def _gen_key_fixture(self):
        temporary = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        data = root / "data"
        binary = root / "bin"
        data.mkdir()
        binary.mkdir()
        secret = "C3" * 32 + "D"
        pwgen = binary / "pwgen"
        pwgen.write_text(
            "#!/bin/sh\n"
            "if [ \"${PINRY_PWGEN_FAIL:-0}\" = 1 ]; then exit 48; fi\n"
            "printf '%s\\n' \"$PINRY_TEST_SECRET\"\n"
        )
        pwgen.chmod(0o700)
        real_stat = shutil.which("stat")
        self.assertIsNotNone(real_stat)
        stat_command = binary / "stat"
        stat_command.write_text(
            "#!/bin/sh\n"
            "if [ \"${PINRY_TEST_STAT_UID_PATH:-}\" = \"${3:-}\" ] "
            "&& [ \"${2:-}\" = %u ]; then\n"
            "    if [ \"${PINRY_TEST_STAT_UID_UNTIL_MODE_600:-0}\" = 1 ]; then\n"
            "        mode=\"$(\"" + str(real_stat) + "\" -c %a \"${3:-}\" "
            "2>/dev/null || \"" + str(real_stat) + "\" -f %Lp \"${3:-}\")\"\n"
            "        if [ \"$mode\" = 600 ]; then\n"
            "            exec \"" + str(real_stat) + "\" \"$@\"\n"
            "        fi\n"
            "    fi\n"
            "    printf '%s\\n' \"$PINRY_TEST_STAT_UID\"\n"
            "    exit 0\n"
            "fi\n"
            "if [ \"${PINRY_TEST_STAT_UID_PATH_2:-}\" = \"${3:-}\" ] "
            "&& [ \"${2:-}\" = %u ]; then\n"
            "    printf '%s\\n' \"$PINRY_TEST_STAT_UID_2\"\n"
            "    exit 0\n"
            "fi\n"
            "exec \"" + str(real_stat) + "\" \"$@\"\n"
        )
        stat_command.chmod(0o700)
        source = (
            REPOSITORY_ROOT / "docker/scripts/gen_key.sh"
        ).read_text("utf-8")
        script = root / "gen_key.sh"
        script.write_text(source.replace("/data", str(data)))
        script.chmod(0o700)
        environment = os.environ.copy()
        environment["PATH"] = "{}{}{}".format(
            binary,
            os.pathsep,
            environment.get("PATH", ""),
        )
        environment["PINRY_DATA_ROOT"] = str(data)
        environment["PINRY_NORMALIZE_SCRIPT"] = str(
            REPOSITORY_ROOT / "docker/scripts/normalize_persistent_file.py"
        )
        environment["PINRY_TEST_SECRET"] = secret
        return script, data, secret, environment

    def test_gen_key_atomically_creates_exact_private_key_without_logging(self):
        script, data, secret, environment = self._gen_key_fixture()

        completed = subprocess.run(
            ["/bin/bash", str(script)],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        rendered = completed.stdout + completed.stderr
        key_path = data / "production_secret_key.txt"
        self.assertEqual(completed.returncode, 0, rendered)
        self.assertNotIn(secret.encode("ascii"), rendered)
        self.assertEqual(key_path.read_bytes(), (secret + "\n").encode())
        self.assertEqual(stat.S_IMODE(key_path.stat().st_mode), 0o600)
        self.assertEqual(
            list(data.glob(".production_secret_key.txt.tmp-*")),
            [],
        )

    def test_gen_key_rejects_invalid_generator_output_without_target(self):
        script, data, _secret, environment = self._gen_key_fixture()
        environment["PINRY_TEST_SECRET"] = "too-short"

        completed = subprocess.run(
            ["/bin/bash", str(script)],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertFalse((data / "production_secret_key.txt").exists())

    def test_gen_key_preserves_compatible_existing_key_without_generator(self):
        script, data, secret, environment = self._gen_key_fixture()
        key_path = data / "production_secret_key.txt"
        key_path.write_bytes((secret + "\n").encode("ascii"))
        key_path.chmod(0o600)
        before = key_path.read_bytes()
        environment["PINRY_PWGEN_FAIL"] = "1"

        completed = subprocess.run(
            ["/bin/bash", str(script)],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(key_path.read_bytes(), before)
        self.assertNotIn(secret.encode("ascii"), completed.stdout)

    def test_gen_key_accepts_safe_key_copied_by_synology_user(self):
        script, data, secret, environment = self._gen_key_fixture()
        key_path = data / "production_secret_key.txt"
        key_path.write_bytes((secret + "\n").encode("ascii"))
        key_path.chmod(0o666)
        synology_uid = os.getuid() + 10000
        environment["PINRY_TEST_STAT_UID_PATH"] = str(key_path)
        environment["PINRY_TEST_STAT_UID"] = str(synology_uid)
        environment["PINRY_TEST_STAT_UID_UNTIL_MODE_600"] = "1"
        environment["PINRY_PWGEN_FAIL"] = "1"

        completed = subprocess.run(
            ["/bin/bash", str(script)],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        self.assertEqual(
            key_path.read_bytes(),
            (secret + "\n").encode("ascii"),
        )
        self.assertEqual(stat.S_IMODE(key_path.stat().st_mode), 0o600)
        self.assertEqual(key_path.stat().st_uid, os.getuid())

    def test_normalizer_accepts_foreign_source_uid(self):
        temporary = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        data = Path(temporary.name, "data")
        data.mkdir()
        settings = data / "local_settings.py"
        existing = b"SECRET_KEY='sentinel-existing-secret'\n"
        settings.write_bytes(existing)
        settings.chmod(0o666)
        helper_path = (
            REPOSITORY_ROOT / "docker/scripts/normalize_persistent_file.py"
        )
        spec = importlib.util.spec_from_file_location(
            "test_normalize_persistent_file",
            str(helper_path),
        )
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        real_fstat = helper.os.fstat
        real_lstat = helper.os.lstat
        foreign_uid = os.getuid() + 10000

        def with_foreign_uid(result):
            values = {
                name: getattr(result, name)
                for name in dir(result)
                if name.startswith("st_")
            }
            values["st_uid"] = foreign_uid
            return SimpleNamespace(**values)

        def fake_fstat(descriptor):
            result = real_fstat(descriptor)
            if stat.S_IMODE(result.st_mode) == 0o666:
                return with_foreign_uid(result)
            return result

        def fake_lstat(path):
            result = real_lstat(path)
            if (
                os.path.abspath(os.fspath(path)) == os.path.abspath(settings)
                and stat.S_IMODE(result.st_mode) == 0o666
            ):
                return with_foreign_uid(result)
            return result

        with mock.patch.object(helper.os, "fstat", side_effect=fake_fstat):
            with mock.patch.object(
                helper.os,
                "lstat",
                side_effect=fake_lstat,
            ):
                helper.normalize(str(data), str(settings), "settings")

        self.assertEqual(settings.read_bytes(), existing)
        self.assertEqual(stat.S_IMODE(settings.stat().st_mode), 0o600)
        self.assertEqual(settings.stat().st_uid, os.getuid())

    def test_gen_key_rejects_hardlinked_existing_key(self):
        script, data, secret, environment = self._gen_key_fixture()
        key_path = data / "production_secret_key.txt"
        existing = (secret + "\n").encode("ascii")
        key_path.write_bytes(existing)
        key_path.chmod(0o666)
        os.link(str(key_path), str(data / "production_secret_key-linked.txt"))
        environment["PINRY_PWGEN_FAIL"] = "1"

        completed = subprocess.run(
            ["/bin/bash", str(script)],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(key_path.read_bytes(), existing)
        self.assertEqual(stat.S_IMODE(key_path.stat().st_mode), 0o666)
        self.assertEqual(
            list(data.glob(".production_secret_key.txt.tmp-*")),
            [],
        )

    def test_timeout_counter_counts_long_and_short_forms(self):
        self.assertEqual(
            _timeout_values([
                "--timeout",
                "60",
                "--timeout=60",
                "-t",
                "30",
                "-t20",
            ]),
            ["60", "60", "30", "20"],
        )

    def test_make_target_passes_one_effective_60_second_timeout(self):
        environment, capture = self._capture_environment("poetry")
        completed = subprocess.run(
            ["make", "--no-print-directory", "-s", "serve-gunicorn"],
            cwd=str(REPOSITORY_ROOT),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        arguments = _read_recorded_argv(capture)
        self.assertEqual(arguments[:2], ["run", "gunicorn"])
        self.assertEqual(_timeout_values(arguments[2:]), ["60"])

    def test_compose_web_command_has_one_effective_60_second_timeout(self):
        source = (REPOSITORY_ROOT / "docker-compose.example.yml").read_text()
        command = _compose_web_command(source)
        outer_arguments = shlex.split(command)

        self.assertEqual(outer_arguments[:2], ["bash", "-c"])
        self.assertEqual(len(outer_arguments), 3)
        inner_arguments = shlex.split(outer_arguments[2])
        self.assertEqual(inner_arguments.count("gunicorn"), 1)
        gunicorn = inner_arguments.index("gunicorn")
        self.assertEqual(_timeout_values(inner_arguments[gunicorn + 1:]), ["60"])

    def test_compose_parser_rejects_anchor_alias_and_other_scalars(self):
        valid = (
            "version: '3'\n\n"
            "services:\n"
            "  web:\n"
            "    command: >\n"
            "      bash -c \"gunicorn pinry.wsgi --timeout 60\"\n"
        )
        self.assertIn("--timeout 60", _compose_web_command(valid))
        for replacement in (
            "command: &shared >",
            "command: *shared",
            "command: 'gunicorn pinry.wsgi --timeout 60'",
            "command: |",
        ):
            with self.subTest(replacement=replacement):
                with self.assertRaises(AssertionError):
                    _compose_web_command(
                        valid.replace("command: >", replacement)
                    )

    def test_nginx_exact_batch_location_has_isolated_runtime_limits(self):
        source = (
            REPOSITORY_ROOT / "docker/nginx/sites-enabled/default"
        ).read_text()

        _assert_nginx_contract(source)

    def test_nginx_validator_rejects_duplicate_direct_batch_break(self):
        source = (
            REPOSITORY_ROOT / "docker/nginx/sites-enabled/default"
        ).read_text()
        mutant = source.replace(
            "        break;\n    }\n\n    location /api {",
            "        break;\n        break;\n    }\n\n    location /api {",
            1,
        )
        self.assertNotEqual(mutant, source)

        with self.assertRaises(AssertionError):
            _assert_nginx_contract(mutant)

    def test_nginx_validator_rejects_one_megabyte_in_third_location(self):
        source = (
            REPOSITORY_ROOT / "docker/nginx/sites-enabled/default"
        ).read_text()
        mutant = source.replace(
            "    location /admin {",
            "    location /other {\n"
            "        client_max_body_size 1m;\n"
            "    }\n\n"
            "    location /admin {",
            1,
        )
        self.assertNotEqual(mutant, source)

        with self.assertRaises(AssertionError):
            _assert_nginx_contract(mutant)
