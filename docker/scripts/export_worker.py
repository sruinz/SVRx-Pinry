#!/usr/bin/env python
import argparse
import os
import pwd
import sys


class ExportWorkerLauncherError(Exception):
    def __init__(self, code):
        super(ExportWorkerLauncherError, self).__init__(code)
        self.code = code


def _positive_decimal(value):
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdecimal()
    ):
        raise argparse.ArgumentTypeError("positive decimal required")
    parsed = int(value, 10)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("positive decimal required")
    return parsed


def _parse_arguments(arguments):
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--uid", required=True, type=_positive_decimal)
    parser.add_argument("--gid", required=True, type=_positive_decimal)
    return parser.parse_args(arguments)


def _validate_service_identity(uid, gid):
    try:
        account = pwd.getpwnam("www-data")
    except KeyError:
        raise ExportWorkerLauncherError("export_worker_identity_invalid")
    if account.pw_uid != uid or account.pw_gid != gid:
        raise ExportWorkerLauncherError("export_worker_identity_invalid")


def drop_privileges(uid, gid):
    try:
        os.umask(0o077)
        os.setgroups([])
        os.setgid(gid)
        os.setuid(uid)
    except (IOError, OSError):
        raise ExportWorkerLauncherError(
            "export_worker_privilege_drop_failed"
        )
    if (
        os.geteuid() != uid
        or os.getegid() != gid
        or os.getgroups()
    ):
        raise ExportWorkerLauncherError(
            "export_worker_privilege_drop_failed"
        )


def _run_worker_command():
    project_root = os.path.realpath(
        os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
    )
    os.chdir(project_root)
    if not os.path.samefile(os.getcwd(), project_root):
        raise ExportWorkerLauncherError("export_worker_start_failed")
    while project_root in sys.path:
        sys.path.remove(project_root)
    sys.path.insert(0, project_root)
    os.environ["DJANGO_SETTINGS_MODULE"] = "pinry.settings.docker"

    import django
    from django.conf import settings
    from django.core.management import call_command
    from django.db import connections

    # 기본값을 채우기 전에 명시된 연결 수명과 다른 DB 설정은 보존합니다.
    database = settings.DATABASES.get("default", {})
    if database.get("ENGINE") == "django.db.backends.sqlite3":
        database.setdefault("CONN_MAX_AGE", 60)
    django.setup()
    try:
        call_command("run_export_worker")
    finally:
        connections.close_all()


def _write_error(code):
    try:
        sys.stderr.write("{}\n".format(code))
        sys.stderr.flush()
    except (IOError, OSError):
        pass


def main(arguments):
    parsed = _parse_arguments(arguments)
    try:
        _validate_service_identity(parsed.uid, parsed.gid)
        drop_privileges(parsed.uid, parsed.gid)
        _run_worker_command()
        return 0
    except BaseException as error:
        if not isinstance(error, Exception):
            raise
        code = getattr(error, "code", "export_worker_start_failed")
        _write_error(code)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
