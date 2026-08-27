#!/usr/bin/env python
import os
import pwd
import signal
import sys
from datetime import datetime, timezone


PROJECT_ROOT = os.path.realpath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
)
if PROJECT_ROOT in sys.path:
    sys.path.remove(PROJECT_ROOT)
sys.path.insert(0, PROJECT_ROOT)

from docker.scripts.migration_status import (  # noqa: E402
    ERROR_CODE_CLASSES,
    STATUS_DIRECTORY,
    MigrationStatusStore,
)
from docker.scripts.supervisor import RuntimeSupervisor  # noqa: E402


_MIGRATION_FLAG = "--migrate-legacy"
_DEFAULT_DATA_ROOT = "/data"
_DEFAULT_SERVICE_UID = 33
_DEFAULT_SERVICE_GID = 33


def _write_error(code):
    if code not in ERROR_CODE_CLASSES:
        code = "runtime_supervisor_failed"
    try:
        sys.stderr.write("{}\n".format(code))
        sys.stderr.flush()
    except (IOError, OSError):
        pass


def _service_identity():
    try:
        account = pwd.getpwnam("www-data")
    except KeyError:
        return _DEFAULT_SERVICE_UID, _DEFAULT_SERVICE_GID
    return account.pw_uid, account.pw_gid


def _utc_now():
    return datetime.now(timezone.utc)


def _run(arguments):
    if arguments not in ([], [_MIGRATION_FLAG]):
        _write_error("startup_argument_invalid")
        return 2

    data_root = os.path.abspath(
        os.fspath(os.environ.get("PINRY_DATA_ROOT", _DEFAULT_DATA_ROOT))
    )
    service_uid, service_gid = _service_identity()
    status_store = MigrationStatusStore(
        STATUS_DIRECTORY,
        _utc_now,
        service_uid,
        service_gid,
    )
    runtime = RuntimeSupervisor(
        arguments,
        data_root,
        service_uid,
        service_gid,
        status_store,
    )

    previous = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        previous[signum] = signal.signal(signum, runtime.handle_signal)
    try:
        return runtime.run()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    sys.exit(_run(sys.argv[1:]))
