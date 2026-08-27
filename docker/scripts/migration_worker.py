#!/usr/bin/env python
import json
import os
import pwd
import subprocess
import sys


PROJECT_ROOT = os.path.realpath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
)
if PROJECT_ROOT in sys.path:
    sys.path.remove(PROJECT_ROOT)
sys.path.insert(0, PROJECT_ROOT)

from docker.scripts.migration_status import ERROR_CODE_CLASSES  # noqa: E402


BOOTSTRAP_SCRIPT = os.path.join(
    PROJECT_ROOT,
    "docker",
    "scripts",
    "bootstrap.sh",
)
_MIGRATION_FLAG = "--migrate-legacy"
_DEFAULT_DATA_ROOT = "/data"
_DEFAULT_SERVICE_UID = 33
_DEFAULT_SERVICE_GID = 33
_MAX_FRAME_BYTES = 4096
_BOOTSTRAP_ERROR_CODES = frozenset((
    "bootstrap_environment_invalid",
    "bootstrap_persistent_settings_invalid",
    "bootstrap_project_settings_invalid",
))
_PHASE_LABELS = {
    "preparing": "실행 환경 확인",
    "recovery": "완료 작업 확인",
    "upgrade_v2": "기존 이전 상태 승격",
    "snapshot": "레거시 상태 고정",
    "planning": "이전 계획 생성",
    "copying": "이미지 파일 이전",
    "database": "데이터베이스 경로 반영",
    "backfill_planning": "미디어 자산 등록 계획",
    "backfill_registering": "미디어 자산 등록",
    "archive": "레거시 백업 확정",
    "finalizing": "최종 검증",
    "complete": "이전 완료",
    "error": "오류",
}


class WorkerProtocolError(Exception):
    def __init__(self, code):
        super(WorkerProtocolError, self).__init__(code)
        self.code = code


class ProgressPipeReporter(object):
    def __init__(self, progress_fd):
        self.progress_fd = progress_fd

    @staticmethod
    def _frame(event):
        if not isinstance(event, dict):
            raise WorkerProtocolError("worker_protocol_invalid")
        try:
            payload = json.dumps(
                event,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8") + b"\n"
        except (TypeError, ValueError, UnicodeError):
            raise WorkerProtocolError("worker_protocol_invalid")
        if len(payload) > _MAX_FRAME_BYTES:
            raise WorkerProtocolError("worker_protocol_invalid")
        return payload

    def send(self, event):
        payload = self._frame(event)
        offset = 0
        while offset < len(payload):
            try:
                written = os.write(self.progress_fd, payload[offset:])
            except OSError:
                raise WorkerProtocolError("worker_protocol_invalid")
            if written <= 0:
                raise WorkerProtocolError("worker_protocol_invalid")
            offset += written
        self._write_log(event)

    def send_error(self, code):
        if code not in ERROR_CODE_CLASSES:
            raise WorkerProtocolError("worker_protocol_invalid")
        self.send({"phase": "error", "error_code": code})

    @staticmethod
    def _write_log(event):
        phase = event.get("phase")
        label = _PHASE_LABELS.get(phase)
        if label is None:
            return
        parts = ["이전 진행: 단계={}".format(label)]
        if "images_done" in event and "images_total" in event:
            parts.append("이미지={}/{}".format(
                event["images_done"], event["images_total"]
            ))
        if "last_committed_batch" in event:
            parts.append("배치={}".format(event["last_committed_batch"]))
        try:
            print(" ".join(parts), flush=True)
        except (IOError, OSError):
            pass


def _safe_error_code(error, fallback="legacy_startup_failed"):
    code = getattr(error, "code", None)
    if code in ERROR_CODE_CLASSES:
        return code
    return fallback


def _safe_bootstrap_error_code(error):
    payload = getattr(error, "stderr", None)
    if not isinstance(payload, bytes) or len(payload) > _MAX_FRAME_BYTES:
        return "bootstrap_failed"
    try:
        lines = payload.decode("ascii").splitlines()
    except UnicodeDecodeError:
        return "bootstrap_failed"
    for line in reversed(lines):
        if line in _BOOTSTRAP_ERROR_CODES:
            return line
    return "bootstrap_failed"


def _service_identity():
    try:
        account = pwd.getpwnam("www-data")
    except KeyError:
        return _DEFAULT_SERVICE_UID, _DEFAULT_SERVICE_GID
    return account.pw_uid, account.pw_gid


def _run_bootstrap():
    try:
        subprocess.run(
            ["/bin/bash", BOOTSTRAP_SCRIPT],
            check=True,
            close_fds=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except OSError as error:
        raise WorkerProtocolError("bootstrap_failed") from error
    except subprocess.CalledProcessError as error:
        raise WorkerProtocolError(_safe_bootstrap_error_code(error))


def _setup_django():
    os.environ["DJANGO_SETTINGS_MODULE"] = "pinry.settings.docker"
    try:
        import django

        django.setup()
    except BaseException as error:
        if not isinstance(error, Exception):
            raise
        raise WorkerProtocolError(
            "media_storage_configuration_invalid"
        ) from error


def _run_schema_commands(call_command):
    call_command("collectstatic", interactive=False)
    call_command("migrate", interactive=False)


def _run_coordinator(arguments, lock_fd, reporter):
    from django.conf import settings
    from django.core.management import call_command
    from django_images.services import migration_state
    from django_images.services.legacy_startup import (
        LegacyStartupCoordinator,
    )

    data_root = os.path.abspath(
        os.fspath(os.environ.get("PINRY_DATA_ROOT", _DEFAULT_DATA_ROOT))
    )
    configured_root = os.path.abspath(os.fspath(settings.PINRY_DATA_ROOT))
    if configured_root != data_root:
        raise WorkerProtocolError("media_storage_configuration_invalid")

    migration_requested = arguments == [_MIGRATION_FLAG]
    run = None
    if not migration_requested:
        inventory = migration_state.inspect_run_inventory_read_only(
            os.path.join(configured_root, "legacy-backup")
        )
        if inventory.requires_migration_flag:
            raise WorkerProtocolError("legacy_migration_flag_required")

    service_uid, service_gid = _service_identity()
    coordinator = LegacyStartupCoordinator(
        service_uid,
        service_gid,
        progress_reporter=reporter.send,
    )
    if migration_requested:
        run = coordinator.prepare_before_schema()
        coordinator.prepare_migration_locks(run)
        if coordinator.schema_required(run):
            _run_schema_commands(call_command)
    else:
        coordinator.prepare_no_flag_before_schema()
        _run_schema_commands(call_command)
    coordinator.converge_after_schema(run)
    coordinator.adjust_ownership(lock_fd)
    coordinator.runtime_check(service_uid, service_gid)


def main(arguments, progress_fd, lock_fd):
    reporter = ProgressPipeReporter(progress_fd)
    try:
        os.chdir(PROJECT_ROOT)
        if not os.path.samefile(os.getcwd(), PROJECT_ROOT):
            raise WorkerProtocolError("media_storage_configuration_invalid")
        _run_bootstrap()
        _setup_django()
        _run_coordinator(arguments, lock_fd, reporter)
        reporter.send({"phase": "complete"})
        return 0
    except BaseException as error:
        if not isinstance(error, Exception):
            raise
        code = _safe_error_code(error)
        try:
            reporter.send_error(code)
        except WorkerProtocolError:
            pass
        return 1
    finally:
        for descriptor in (progress_fd, lock_fd):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _parse_cli(arguments):
    migration_arguments = []
    if arguments[:1] == [_MIGRATION_FLAG]:
        migration_arguments = [_MIGRATION_FLAG]
        arguments = arguments[1:]
    if len(arguments) != 4:
        raise WorkerProtocolError("startup_argument_invalid")
    if arguments[0] != "--progress-fd" or arguments[2] != "--lock-fd":
        raise WorkerProtocolError("startup_argument_invalid")
    descriptors = []
    for raw in (arguments[1], arguments[3]):
        if not raw or any(character not in "0123456789" for character in raw):
            raise WorkerProtocolError("startup_argument_invalid")
        descriptor = int(raw, 10)
        try:
            os.fstat(descriptor)
        except (OSError, OverflowError):
            raise WorkerProtocolError("startup_argument_invalid")
        descriptors.append(descriptor)
    if descriptors[0] == descriptors[1]:
        raise WorkerProtocolError("startup_argument_invalid")
    try:
        for descriptor in descriptors:
            os.set_inheritable(descriptor, False)
    except (OSError, OverflowError):
        raise WorkerProtocolError("startup_argument_invalid")
    return migration_arguments, descriptors[0], descriptors[1]


def _entrypoint(arguments):
    try:
        parsed = _parse_cli(list(arguments))
    except WorkerProtocolError:
        sys.stderr.write("startup_argument_invalid\n")
        sys.stderr.flush()
        return 2
    return main(*parsed)


if __name__ == "__main__":
    sys.exit(_entrypoint(sys.argv[1:]))
