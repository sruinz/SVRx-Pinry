import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest


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
        self.assertEqual(startup_source.count("/usr/sbin/nginx"), 1)
        self.assertEqual(startup_source.count('PROJECT_ROOT="/pinry"'), 1)
        startup_directory = temporary_root / "docker/scripts"
        startup_directory.mkdir(parents=True)
        startup_script = startup_directory / "start.sh"
        startup_script.write_text(
            startup_source.replace("/usr/sbin/nginx", "nginx", 1)
            .replace(
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

    def test_start_runs_one_migration_before_services_for_every_database(self):
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
                migration = ["python", "manage.py", "migrate", "--noinput"]
                self.assertEqual(events.count(migration), 1)
                migration_index = events.index(migration)
                self.assertIn(["nginx"], events)
                self.assertTrue(
                    any(event[0] == "gunicorn" for event in events)
                )
                nginx_index = events.index(["nginx"])
                gunicorn_index = next(
                    index
                    for index, event in enumerate(events)
                    if event[0] == "gunicorn"
                )
                self.assertLess(migration_index, nginx_index)
                self.assertLess(migration_index, gunicorn_index)

    def test_start_stops_before_services_when_migration_fails(self):
        environment, capture, startup_script = self._startup_environment(
            False
        )
        environment["PINRY_FAIL_MIGRATE"] = "1"

        completed = subprocess.run(
            ["/bin/bash", str(startup_script)],
            cwd=str(REPOSITORY_ROOT),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual(completed.returncode, 41)
        events = _read_startup_events(capture)
        self.assertEqual(
            events.count(["python", "manage.py", "migrate", "--noinput"]),
            1,
        )
        self.assertFalse(any(event[0] == "nginx" for event in events))
        self.assertFalse(any(event[0] == "gunicorn" for event in events))

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
