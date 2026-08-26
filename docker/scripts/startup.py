#!/usr/bin/env python
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

from django_images.services import startup_lock  # noqa: E402


NGINX_BINARY = "/usr/sbin/nginx"
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
_SAFE_BOOTSTRAP_ERROR_CODES = frozenset((
    "bootstrap_environment_invalid",
    "bootstrap_persistent_settings_invalid",
    "bootstrap_project_settings_invalid",
))
_SAFE_ERROR_CODES = frozenset((
    "archive_failed",
    "archive_manifest_mismatch",
    "archive_state_conflict",
    "atomic_archive_unsupported",
    "bootstrap_failed",
    "corrupt_sqlite_snapshot",
    "legacy_database_invalid",
    "legacy_database_schema_invalid",
    "legacy_evidence_invalid",
    "legacy_media_files_invalid",
    "legacy_media_root_invalid",
    "legacy_media_rows_invalid",
    "legacy_media_still_referenced",
    "legacy_migration_graph_invalid",
    "legacy_migration_flag_required",
    "legacy_migration_space_insufficient",
    "legacy_startup_failed",
    "media_lock_not_usable",
    "media_root_not_writable",
    "media_storage_configuration_invalid",
    "migration_state_conflict",
    "migration_state_create_failed",
    "migration_state_identity_changed",
    "migration_state_invalid",
    "migration_state_invalid_transition",
    "migration_state_missing_or_invalid",
    "migration_state_phase_mismatch",
    "migration_state_plan_mismatch",
    "migration_state_root_invalid",
    "migration_state_write_failed",
    "nginx_start_failed",
    "sqlite_snapshot_conflict",
    "sqlite_snapshot_failed",
    "sqlite_snapshot_phase_invalid",
    "sqlite_snapshot_state_invalid",
    "sqlite_source_identity_changed",
    "sqlite_source_not_configured_database",
    "sqlite_source_outside_data_root",
    "startup_argument_invalid",
    "startup_lock_busy",
    "startup_lock_failed",
    "startup_lock_unsupported",
    "unsafe_auto_v2_manifest",
    "unsafe_media_lock_state",
    "unsafe_migration_summary",
    "unsafe_sqlite_snapshot",
    "unsafe_sqlite_source",
    "unsafe_sqlite_source_hardlink",
    "unsafe_sqlite_source_non_regular",
    "unsafe_sqlite_source_symlink",
    "unsafe_storage_ownership",
    "unsupported_legacy_database_backend",
))


def _write_error(code):
    sys.stderr.write("{}\n".format(code))
    sys.stderr.flush()


def _write_progress(event):
    if not isinstance(event, dict):
        return False
    phase = event.get("phase")
    message = None
    if phase == "planning" and set(event) == {
        "phase",
        "images_total",
        "files_total",
    }:
        images_total = event["images_total"]
        files_total = event["files_total"]
        if _valid_total(images_total) and _valid_total(files_total):
            message = (
                "SVRx Pinry 데이터 이전: 계획 완료 - "
                "{}개 이미지, {}개 파일"
            ).format(images_total, files_total)
    elif phase == "copying" and set(event) == {
        "phase",
        "images_done",
        "images_total",
        "files_done",
        "files_total",
    }:
        values = (
            event["images_done"],
            event["images_total"],
            event["files_done"],
            event["files_total"],
        )
        if _valid_progress_pair(values[0], values[1]) and (
            _valid_progress_pair(values[2], values[3])
        ):
            message = (
                "SVRx Pinry 데이터 이전: 파일 처리 "
                "{}/{} 이미지, {}/{} 파일 ({:.1f}%)"
            ).format(
                values[0],
                values[1],
                values[2],
                values[3],
                _progress_percent(values[2], values[3]),
            )
    elif phase == "database" and set(event) == {
        "phase",
        "images_done",
        "images_total",
    }:
        images_done = event["images_done"]
        images_total = event["images_total"]
        if _valid_progress_pair(images_done, images_total):
            message = (
                "SVRx Pinry 데이터 이전: 데이터베이스 처리 "
                "{}/{} 이미지 ({:.1f}%)"
            ).format(
                images_done,
                images_total,
                _progress_percent(images_done, images_total),
            )
    elif set(event) == {"phase"}:
        message = {
            "preparing": "SVRx Pinry 데이터 이전을 준비하고 있습니다.",
            "snapshot": "데이터베이스 백업을 완료했습니다.",
            "backfill": "이미지 정보 등록을 완료했습니다.",
            "archive": "기존 이미지 폴더를 백업 위치로 옮기고 있습니다.",
            "complete": "SVRx Pinry 데이터 이전을 완료했습니다.",
        }.get(phase)
    if message is None:
        return False
    sys.stdout.write("{}\n".format(message))
    sys.stdout.flush()
    return True


def _valid_total(value):
    return type(value) is int and value >= 0


def _valid_progress_pair(done, total):
    return _valid_total(done) and _valid_total(total) and done <= total


def _progress_percent(done, total):
    if total == 0:
        return 100.0
    return (float(done) * 100.0) / float(total)


def _safe_error_code(error, fallback="legacy_startup_failed"):
    code = getattr(error, "code", None)
    if code in _SAFE_ERROR_CODES:
        return code
    return fallback


def _safe_bootstrap_error_code(error):
    payload = getattr(error, "stderr", None)
    if not isinstance(payload, bytes) or len(payload) > 4096:
        return "bootstrap_failed"
    try:
        lines = payload.decode("ascii").splitlines()
    except UnicodeDecodeError:
        return "bootstrap_failed"
    for line in reversed(lines):
        if line in _SAFE_BOOTSTRAP_ERROR_CODES:
            return line
    return "bootstrap_failed"


def _service_identity():
    try:
        account = pwd.getpwnam("www-data")
    except KeyError:
        return _DEFAULT_SERVICE_UID, _DEFAULT_SERVICE_GID
    return account.pw_uid, account.pw_gid


def _run_schema_commands(call_command):
    call_command("collectstatic", interactive=False)
    call_command("migrate", interactive=False)


def _run(arguments):  # noqa: C901
    if arguments not in ([], [_MIGRATION_FLAG]):
        _write_error("startup_argument_invalid")
        return 2

    data_root = os.path.abspath(
        os.fspath(os.environ.get("PINRY_DATA_ROOT", _DEFAULT_DATA_ROOT))
    )
    try:
        service_uid, service_gid = _service_identity()
        held_lock = startup_lock.acquire_startup_lock(
            data_root,
            service_uid,
            service_gid,
        )
    except BaseException as error:
        _write_error(_safe_error_code(error, "startup_lock_failed"))
        return 1

    try:
        os.chdir(PROJECT_ROOT)
        if not os.path.samefile(os.getcwd(), PROJECT_ROOT):
            raise OSError("project root identity changed")
    except BaseException:
        _write_error("media_storage_configuration_invalid")
        return 1

    try:
        subprocess.run(
            ["/bin/bash", BOOTSTRAP_SCRIPT],
            check=True,
            close_fds=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except OSError:
        _write_error("bootstrap_failed")
        return 1
    except subprocess.CalledProcessError as error:
        _write_error(_safe_bootstrap_error_code(error))
        return 1

    os.environ["DJANGO_SETTINGS_MODULE"] = "pinry.settings.docker"
    try:
        import django

        django.setup()
    except BaseException:
        _write_error("media_storage_configuration_invalid")
        return 1

    try:
        from django.conf import settings
        from django.core.management import call_command
        from django_images.services import migration_state
        from django_images.services.legacy_startup import (
            LegacyStartupCoordinator,
        )

        configured_root = os.path.abspath(
            os.fspath(settings.PINRY_DATA_ROOT)
        )
        if configured_root != data_root:
            raise ValueError("configured data root mismatch")

        migration_requested = arguments == [_MIGRATION_FLAG]
        run = None
        if not migration_requested:
            inventory = migration_state.inspect_run_inventory_read_only(
                os.path.join(configured_root, "legacy-backup")
            )
            if inventory.requires_migration_flag:
                _write_error("legacy_migration_flag_required")
                return 1

        coordinator = LegacyStartupCoordinator(
            service_uid,
            service_gid,
            progress_reporter=_write_progress,
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
        coordinator.adjust_ownership(held_lock.fileno())
        coordinator.runtime_check(service_uid, service_gid)
        try:
            subprocess.run([NGINX_BINARY], check=True, close_fds=True)
        except (OSError, subprocess.CalledProcessError):
            _write_error("nginx_start_failed")
            return 1
        held_lock.set_inheritable(True)
        gunicorn_script = os.path.join(
            PROJECT_ROOT,
            "docker",
            "scripts",
            "_start_gunicorn.sh",
        )
        os.execv(gunicorn_script, [gunicorn_script])
    except BaseException as error:
        _write_error(_safe_error_code(error))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_run(sys.argv[1:]))
