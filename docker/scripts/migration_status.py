import json
import os
import re
import stat
import tempfile
import uuid
from datetime import datetime, timezone


STATUS_DIRECTORY = "/run/svrx-pinry"
STATUS_PATH = "/run/svrx-pinry/migration-status.json"
MAINTENANCE_MARKER_PATH = "/run/svrx-pinry/maintenance"

_STATUS_FILENAME = "migration-status.json"
_MARKER_FILENAME = "maintenance"
_MARKER_BYTES = b"maintenance\n"
_DIRECTORY_MODE = 0o755
_FILE_MODE = 0o644

PUBLIC_STATUS_FIELDS = frozenset((
    "schema_version", "state", "phase", "phase_label", "run_id",
    "attempt", "resume_count", "started_at", "phase_started_at",
    "heartbeat_at", "progress_at", "last_committed_batch",
    "images_done", "images_total", "files_done", "files_total",
    "backfill_done", "backfill_total", "phase_percent",
    "overall_percent", "error_class", "error_code",
))

PHASE_LABELS = {
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
}

ERROR_CLASSES = frozenset((
    "retryable", "operator_action_required", "fatal",
))

ERROR_CODE_CLASSES = {
    "archive_failed": "fatal",
    "archive_manifest_mismatch": "fatal",
    "archive_state_conflict": "fatal",
    "atomic_archive_unsupported": "operator_action_required",
    "bootstrap_environment_invalid": "operator_action_required",
    "bootstrap_failed": "fatal",
    "bootstrap_persistent_settings_invalid": "operator_action_required",
    "bootstrap_project_settings_invalid": "operator_action_required",
    "corrupt_sqlite_snapshot": "fatal",
    "gunicorn_start_failed": "retryable",
    "legacy_database_invalid": "operator_action_required",
    "legacy_database_schema_invalid": "operator_action_required",
    "legacy_evidence_invalid": "operator_action_required",
    "legacy_media_files_invalid": "operator_action_required",
    "legacy_media_root_invalid": "operator_action_required",
    "legacy_media_rows_invalid": "operator_action_required",
    "legacy_media_still_referenced": "fatal",
    "legacy_migration_graph_invalid": "operator_action_required",
    "legacy_migration_flag_required": "operator_action_required",
    "legacy_migration_space_insufficient": "operator_action_required",
    "legacy_progress_event_invalid": "fatal",
    "legacy_startup_failed": "fatal",
    "linear_batch_file_sync_failed": "operator_action_required",
    "linear_checkpoint_ahead": "fatal",
    "linear_committed_repair_failed": "operator_action_required",
    "linear_committed_source_changed": "fatal",
    "linear_journal_database_conflict": "fatal",
    "linear_journal_invalid": "fatal",
    "linear_journal_source_changed": "fatal",
    "linear_journal_tail_repair_failed": "operator_action_required",
    "linear_work_totals_changed": "fatal",
    "media_lock_not_usable": "operator_action_required",
    "media_path_conflict": "operator_action_required",
    "media_root_not_writable": "operator_action_required",
    "media_storage_configuration_invalid": "operator_action_required",
    "migration_state_conflict": "fatal",
    "migration_state_create_failed": "operator_action_required",
    "migration_state_identity_changed": "fatal",
    "migration_state_invalid": "fatal",
    "migration_state_invalid_transition": "fatal",
    "migration_state_missing_or_invalid": "fatal",
    "migration_state_phase_mismatch": "fatal",
    "migration_state_plan_mismatch": "fatal",
    "migration_state_root_invalid": "operator_action_required",
    "migration_state_write_failed": "operator_action_required",
    "migration_status_write_failed": "retryable",
    "nginx_start_failed": "retryable",
    "paths_not_complete": "fatal",
    "runtime_gate_fail_closed_failed": "fatal",
    "runtime_gate_open_failed": "operator_action_required",
    "runtime_gate_quiesce_failed": "retryable",
    "runtime_supervisor_failed": "fatal",
    "sqlite_snapshot_conflict": "fatal",
    "sqlite_snapshot_failed": "operator_action_required",
    "sqlite_snapshot_phase_invalid": "fatal",
    "sqlite_snapshot_state_invalid": "fatal",
    "sqlite_source_identity_changed": "fatal",
    "sqlite_source_not_configured_database": "operator_action_required",
    "sqlite_source_outside_data_root": "operator_action_required",
    "startup_argument_invalid": "operator_action_required",
    "startup_lock_busy": "operator_action_required",
    "startup_lock_failed": "operator_action_required",
    "startup_lock_unsupported": "operator_action_required",
    "unsafe_auto_v2_manifest": "fatal",
    "unsafe_media_lock_state": "fatal",
    "unsafe_migration_summary": "fatal",
    "unsafe_sqlite_snapshot": "fatal",
    "unsafe_sqlite_source": "fatal",
    "unsafe_sqlite_source_hardlink": "fatal",
    "unsafe_sqlite_source_non_regular": "fatal",
    "unsafe_sqlite_source_symlink": "fatal",
    "unsafe_storage_ownership": "operator_action_required",
    "unsupported_legacy_database_backend": "operator_action_required",
    "worker_exit_timeout": "retryable",
    "worker_protocol_invalid": "fatal",
}

_WORKER_KEYS = {
    "preparing": frozenset(("phase",)),
    "snapshot": frozenset(("phase",)),
    "planning": frozenset((
        "phase", "run_id", "attempt", "resume_count", "started_at",
        "images_total", "files_total", "backfill_total",
    )),
    "recovery": frozenset((
        "phase", "run_id", "attempt", "resume_count", "started_at",
        "images_done", "images_total", "files_done", "files_total",
        "backfill_done", "backfill_total", "last_committed_batch",
    )),
    "upgrade_v2": frozenset((
        "phase", "images_done", "images_total", "last_committed_batch",
    )),
    "copying": frozenset((
        "phase", "images_done", "images_total", "files_done",
        "files_total",
    )),
    "database": frozenset((
        "phase", "images_done", "images_total", "last_committed_batch",
    )),
    "backfill_planning": frozenset(("phase", "backfill_total")),
    "backfill_registering": frozenset((
        "phase", "backfill_done", "backfill_total",
        "last_committed_batch",
    )),
    "archive": frozenset(("phase",)),
    "finalizing": frozenset(("phase",)),
    "complete": frozenset(("phase",)),
    "error": frozenset(("phase", "error_code")),
}

_INTEGER_KEYS = frozenset((
    "attempt", "resume_count", "images_done", "images_total",
    "files_done", "files_total", "backfill_done", "backfill_total",
    "last_committed_batch",
))
_COMMIT_PHASES = frozenset((
    "upgrade_v2", "database", "backfill_registering",
))
_RUN_ID_RE = re.compile(
    r"^(\d{8}T\d{6}Z)-"
    r"([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12})$"
)
_UTC_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class StatusError(Exception):
    pass


def _runtime_owner_ids():
    return 0, 0


def _canonical_timestamp(value):
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise StatusError("worker_protocol_invalid")
    value = value.astimezone(timezone.utc).replace(microsecond=0)
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_timestamp(value):
    if not isinstance(value, str) or len(value) > 128:
        raise StatusError("worker_protocol_invalid")
    if not _UTC_TIMESTAMP_RE.match(value):
        raise StatusError("worker_protocol_invalid")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise StatusError("worker_protocol_invalid")


def _validate_run_id(value):
    if not isinstance(value, str) or len(value) > 128:
        raise StatusError("worker_protocol_invalid")
    match = _RUN_ID_RE.match(value)
    if match is None:
        raise StatusError("worker_protocol_invalid")
    try:
        datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ")
        parsed = uuid.UUID(match.group(2))
    except (ValueError, AttributeError):
        raise StatusError("worker_protocol_invalid")
    if parsed.version != 4 or str(parsed) != match.group(2):
        raise StatusError("worker_protocol_invalid")


def _canonical_json(payload):
    if set(payload) != PUBLIC_STATUS_FIELDS:
        raise StatusError("migration_status_write_failed")
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def _is_nonnegative_integer(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _ratio(done, total):
    if total == 0:
        return 100.0
    return round(max(0.0, min(100.0, 100.0 * done / total)), 1)


class MigrationStatusStore(object):
    def __init__(self, status_directory, clock, service_uid, service_gid):
        if not _is_nonnegative_integer(service_uid):
            raise StatusError("runtime_identity_invalid")
        if not _is_nonnegative_integer(service_gid):
            raise StatusError("runtime_identity_invalid")
        if not isinstance(status_directory, str) or not status_directory:
            raise StatusError("runtime_gate_fail_closed_failed")
        if not callable(clock):
            raise StatusError("runtime_identity_invalid")

        self.status_directory = status_directory
        self.status_path = os.path.join(status_directory, _STATUS_FILENAME)
        self.marker_path = os.path.join(status_directory, _MARKER_FILENAME)
        self.clock = clock
        self.service_uid = service_uid
        self.service_gid = service_gid

        self._prepared = False
        self._initialized = False
        self._status = None
        self._pending_bytes = None
        self._durable_bytes = None
        self._dirty = False
        self._last_worker_event = None
        self._worker_terminal = False
        self._ordinal_events = {}
        self._readiness_latch = False
        self._gate_removed = False

    def _now(self):
        return _canonical_timestamp(self.clock())

    def _owner(self):
        return _runtime_owner_ids()

    def _validate_directory(self):
        try:
            info = os.lstat(self.status_directory)
        except OSError:
            raise OSError("runtime directory is unavailable")
        uid, gid = self._owner()
        if not stat.S_ISDIR(info.st_mode):
            raise OSError("runtime directory type is invalid")
        if stat.S_IMODE(info.st_mode) != _DIRECTORY_MODE:
            raise OSError("runtime directory mode is invalid")
        if info.st_uid != uid or info.st_gid != gid:
            raise OSError("runtime directory owner is invalid")

    def _validate_regular(self, path, expected_bytes=None):
        try:
            info = os.lstat(path)
        except OSError:
            raise OSError("runtime file is unavailable")
        uid, gid = self._owner()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError("runtime file type is invalid")
        if stat.S_IMODE(info.st_mode) != _FILE_MODE:
            raise OSError("runtime file mode is invalid")
        if info.st_uid != uid or info.st_gid != gid:
            raise OSError("runtime file owner is invalid")
        if expected_bytes is not None:
            with open(path, "rb") as stream:
                if stream.read() != expected_bytes:
                    raise OSError("runtime file payload is invalid")

    def _fsync_runtime_directory(self):
        self._validate_directory()
        flags = os.O_RDONLY
        flags |= getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.status_directory, flags)
        try:
            info = os.fstat(descriptor)
            uid, gid = self._owner()
            if not stat.S_ISDIR(info.st_mode):
                raise OSError("runtime directory type is invalid")
            if stat.S_IMODE(info.st_mode) != _DIRECTORY_MODE:
                raise OSError("runtime directory mode is invalid")
            if info.st_uid != uid or info.st_gid != gid:
                raise OSError("runtime directory owner is invalid")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _atomic_write(self, path, payload):
        if not isinstance(payload, bytes):
            raise OSError("runtime payload is invalid")
        self._validate_directory()
        if os.path.dirname(path) != self.status_directory:
            raise OSError("runtime path is invalid")
        if os.path.lexists(path):
            self._validate_regular(path)

        descriptor = None
        temporary_path = None
        try:
            descriptor, temporary_path = tempfile.mkstemp(
                prefix=".migration-status-",
                dir=self.status_directory,
            )
            info = os.fstat(descriptor)
            uid, gid = self._owner()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise OSError("runtime temporary type is invalid")
            if info.st_uid != uid or info.st_gid != gid:
                raise OSError("runtime temporary owner is invalid")
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written <= 0:
                    raise OSError("runtime write did not progress")
                offset += written
            os.fchmod(descriptor, _FILE_MODE)
            info = os.fstat(descriptor)
            if stat.S_IMODE(info.st_mode) != _FILE_MODE:
                raise OSError("runtime temporary mode is invalid")
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            self._validate_regular(temporary_path, payload)
            if os.path.lexists(path):
                self._validate_regular(path)
            os.replace(temporary_path, path)
            temporary_path = None
            self._fsync_runtime_directory()
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary_path is not None:
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass

    def _publish(self):
        payload = self._pending_bytes
        try:
            self._atomic_write(self.status_path, payload)
        except Exception:
            self._dirty = True
            raise StatusError("migration_status_write_failed")
        self._durable_bytes = payload
        self._dirty = False

    def _project(self, status):
        payload = _canonical_json(status)
        self._status = status
        self._pending_bytes = payload
        self._dirty = True
        self._publish()

    def _initial_status(self, now):
        return {
            "schema_version": 1,
            "state": "starting",
            "phase": "preparing",
            "phase_label": PHASE_LABELS["preparing"],
            "run_id": None,
            "attempt": 0,
            "resume_count": 0,
            "started_at": None,
            "phase_started_at": now,
            "heartbeat_at": now,
            "progress_at": None,
            "last_committed_batch": 0,
            "images_done": 0,
            "images_total": None,
            "files_done": 0,
            "files_total": None,
            "backfill_done": 0,
            "backfill_total": None,
            "phase_percent": None,
            "overall_percent": None,
            "error_class": None,
            "error_code": None,
        }

    def prepare_runtime_gate(self):
        try:
            if not os.path.lexists(self.status_directory):
                os.mkdir(self.status_directory, _DIRECTORY_MODE)
                os.chmod(self.status_directory, _DIRECTORY_MODE)
            self._validate_directory()
            self._prepared = True
            self.create_gate()
        except Exception:
            self._prepared = False
            raise StatusError("runtime_gate_fail_closed_failed")

    def initialize(self):
        if not self._prepared:
            raise StatusError("runtime_gate_not_prepared")
        try:
            self._validate_directory()
            self._validate_regular(self.marker_path, _MARKER_BYTES)
        except OSError:
            raise StatusError("runtime_gate_not_prepared")
        if self._initialized:
            return self.publish_pending()
        now = self._now()
        self._initialized = True
        self._project(self._initial_status(now))

    def create_gate(self):
        if not self._prepared:
            raise StatusError("runtime_gate_not_prepared")
        try:
            self._validate_directory()
            if os.path.lexists(self.marker_path):
                self._validate_regular(self.marker_path, _MARKER_BYTES)
                self._fsync_runtime_directory()
                return
            self._atomic_write(self.marker_path, _MARKER_BYTES)
        except Exception:
            raise StatusError("runtime_gate_fail_closed_failed")

    def verify_failed_gate(self):
        if (
            not self._prepared or not self._initialized
            or self._status["state"] != "failed"
        ):
            raise StatusError("runtime_gate_not_prepared")
        if self._dirty or self._durable_bytes != self._pending_bytes:
            raise StatusError("migration_status_write_failed")
        try:
            self._validate_directory()
            self._validate_regular(self.marker_path, _MARKER_BYTES)
            self._validate_regular(self.status_path, self._durable_bytes)
        except OSError:
            raise StatusError("runtime_gate_not_prepared")

    def restart_startup(self):
        self.verify_failed_gate()
        self._last_worker_event = None
        self._worker_terminal = False
        self._ordinal_events = {}
        self._readiness_latch = False
        self._gate_removed = False
        self._project(self._initial_status(self._now()))

    def publish_pending(self):
        if not self._dirty:
            return
        self._publish()

    def _validate_event(self, event):
        if not isinstance(event, dict):
            raise StatusError("worker_protocol_invalid")
        phase = event.get("phase")
        if not isinstance(phase, str) or len(phase) > 128:
            raise StatusError("worker_protocol_invalid")
        expected = _WORKER_KEYS.get(phase)
        if expected is None or set(event) != expected:
            raise StatusError("worker_protocol_invalid")
        for key in set(event) & _INTEGER_KEYS:
            if not _is_nonnegative_integer(event[key]):
                raise StatusError("worker_protocol_invalid")
        if phase in ("planning", "recovery"):
            self._validate_run_fields(event)
        self._validate_event_ranges(event)

    def _validate_run_fields(self, event):
        _validate_run_id(event["run_id"])
        _validate_timestamp(event["started_at"])
        if event["attempt"] < 1:
            raise StatusError("worker_protocol_invalid")
        if event["resume_count"] != event["attempt"] - 1:
            raise StatusError("worker_protocol_invalid")

    def _validate_event_ranges(self, event):
        phase = event["phase"]
        pairs = {
            "recovery": (
                ("images_done", "images_total"),
                ("files_done", "files_total"),
                ("backfill_done", "backfill_total"),
            ),
            "upgrade_v2": (("images_done", "images_total"),),
            "copying": (
                ("images_done", "images_total"),
                ("files_done", "files_total"),
            ),
            "database": (("images_done", "images_total"),),
            "backfill_registering": (
                ("backfill_done", "backfill_total"),
            ),
        }.get(phase, ())
        self._validate_done_totals(event, pairs)
        if phase == "error":
            code = event["error_code"]
            if not isinstance(code, str) or len(code) > 128:
                raise StatusError("worker_protocol_invalid")
            if code not in ERROR_CODE_CLASSES:
                raise StatusError("worker_protocol_invalid")

    def _validate_done_totals(self, event, pairs):
        for done_key, total_key in pairs:
            if event[done_key] > event[total_key]:
                raise StatusError("worker_protocol_invalid")

    def _validate_frozen_totals(self, event):
        frozen = self._status["images_total"] is not None
        if not frozen:
            raise StatusError("worker_protocol_invalid")
        phase = event["phase"]
        if phase == "upgrade_v2":
            if event["images_total"] > self._status["images_total"]:
                raise StatusError("worker_protocol_invalid")
            return
        for name in ("images_total", "files_total", "backfill_total"):
            if name in event and event[name] != self._status[name]:
                raise StatusError("worker_protocol_invalid")

    def _overall_percent(self, status, complete=False, new_run=False):
        if complete:
            return 100.0
        totals = (
            status["images_total"],
            status["files_total"],
            status["backfill_total"],
        )
        if any(value is None for value in totals):
            return None
        denominator = sum(totals)
        if denominator == 0:
            calculated = 0.0
        else:
            calculated = round(
                100.0 * (
                    status["images_done"]
                    + status["files_done"]
                    + status["backfill_done"]
                ) / denominator,
                1,
            )
        calculated = max(0.0, min(99.0, calculated))
        previous = None if new_run else self._status["overall_percent"]
        if previous is not None:
            calculated = max(previous, calculated)
        return calculated

    def _phase_percent(self, event):
        phase = event["phase"]
        if phase == "recovery":
            done = (
                event["images_done"] + event["files_done"]
                + event["backfill_done"]
            )
            total = (
                event["images_total"] + event["files_total"]
                + event["backfill_total"]
            )
            return _ratio(done, total)
        if phase == "upgrade_v2":
            return _ratio(event["images_done"], event["images_total"])
        if phase == "copying":
            return _ratio(event["files_done"], event["files_total"])
        if phase == "database":
            return _ratio(event["images_done"], event["images_total"])
        if phase == "backfill_registering":
            return _ratio(event["backfill_done"], event["backfill_total"])
        if phase == "complete":
            return 100.0
        return None

    def _event_progressed(self, before, after):
        names = (
            "images_done", "files_done", "backfill_done",
            "last_committed_batch",
        )
        return any(after[name] > before[name] for name in names)

    def _validate_non_decreasing(self, before, after):
        names = (
            "images_done", "files_done", "backfill_done",
            "last_committed_batch",
        )
        if any(after[name] < before[name] for name in names):
            raise StatusError("worker_protocol_invalid")

    def _set_run_event(self, status, event):
        current_run = self._status["run_id"]
        new_run = current_run is not None and event["run_id"] != current_run
        if current_run is None or new_run:
            self._start_run(status, event, require_first_attempt=new_run)
            return new_run or current_run is None

        self._validate_same_run_identity(event)
        if event["phase"] == "planning":
            self._validate_same_attempt(event)
            return False
        self._apply_recovery(status, event)
        return False

    def _start_run(self, status, event, require_first_attempt):
        if event["phase"] == "planning" or require_first_attempt:
            if event["attempt"] != 1 or event["resume_count"] != 0:
                raise StatusError("worker_protocol_invalid")
        self._ordinal_events = {}
        status.update({
            "run_id": event["run_id"],
            "attempt": event["attempt"],
            "resume_count": event["resume_count"],
            "started_at": event["started_at"],
            "progress_at": None,
            "last_committed_batch": 0,
            "images_done": 0,
            "files_done": 0,
            "backfill_done": 0,
        })
        for name in ("images_total", "files_total", "backfill_total"):
            status[name] = event[name]
        if event["phase"] == "recovery":
            for name in (
                "images_done", "files_done", "backfill_done",
                "last_committed_batch",
            ):
                status[name] = event[name]

    def _validate_same_run_identity(self, event):
        if event["started_at"] != self._status["started_at"]:
            raise StatusError("worker_protocol_invalid")
        for name in ("images_total", "files_total", "backfill_total"):
            if event[name] != self._status[name]:
                raise StatusError("worker_protocol_invalid")

    def _validate_same_attempt(self, event):
        if event["attempt"] != self._status["attempt"]:
            raise StatusError("worker_protocol_invalid")
        if event["resume_count"] != self._status["resume_count"]:
            raise StatusError("worker_protocol_invalid")

    def _apply_recovery(self, status, event):
        current_attempt = self._status["attempt"]
        if event["attempt"] == current_attempt:
            if event["resume_count"] != self._status["resume_count"]:
                raise StatusError("worker_protocol_invalid")
        elif event["attempt"] == current_attempt + 1:
            if event["resume_count"] != event["attempt"] - 1:
                raise StatusError("worker_protocol_invalid")
        else:
            raise StatusError("worker_protocol_invalid")
        status["attempt"] = event["attempt"]
        status["resume_count"] = event["resume_count"]
        for name in (
            "images_done", "files_done", "backfill_done",
            "last_committed_batch",
        ):
            status[name] = event[name]
        self._validate_non_decreasing(self._status, status)

    def _classify_commit_ordinal(self, event):
        ordinal = event["last_committed_batch"]
        known = self._ordinal_events.get(ordinal)
        if known is not None:
            if known != event:
                raise StatusError("worker_protocol_invalid")
            return "known_replay"
        current = self._status["last_committed_batch"]
        if ordinal == current and current > 0:
            phase = event["phase"]
            counter = {
                "upgrade_v2": "images_done",
                "database": "images_done",
                "backfill_registering": "backfill_done",
            }[phase]
            if event[counter] != self._status[counter]:
                raise StatusError("worker_protocol_invalid")
            self._ordinal_events[ordinal] = dict(event)
            return "restored_replay"
        if ordinal != current + 1:
            raise StatusError("worker_protocol_invalid")
        return "new"

    def _reduce_event(self, event):
        phase = event["phase"]
        status = dict(self._status)
        before = self._status
        new_run = False

        if phase in ("planning", "recovery"):
            new_run = self._set_run_event(status, event)
        elif phase not in ("preparing", "snapshot", "archive",
                           "finalizing", "complete", "error"):
            self._validate_frozen_totals(event)

        commit_kind = None
        if phase in _COMMIT_PHASES:
            commit_kind = self._classify_commit_ordinal(event)
            if commit_kind == "known_replay":
                return None

        if phase == "upgrade_v2":
            status["images_done"] = event["images_done"]
            status["last_committed_batch"] = event["last_committed_batch"]
        elif phase == "copying":
            status["images_done"] = event["images_done"]
            status["files_done"] = event["files_done"]
        elif phase == "database":
            status["images_done"] = event["images_done"]
            status["last_committed_batch"] = event["last_committed_batch"]
        elif phase == "backfill_registering":
            status["backfill_done"] = event["backfill_done"]
            status["last_committed_batch"] = event["last_committed_batch"]

        if phase not in ("planning", "recovery"):
            self._validate_non_decreasing(before, status)
        if commit_kind == "new":
            self._ordinal_events[event["last_committed_batch"]] = dict(event)

        if phase == "error":
            status["state"] = "failed"
            status["error_class"] = ERROR_CODE_CLASSES[event["error_code"]]
            status["error_code"] = event["error_code"]
            return status

        status["state"] = {
            "preparing": "starting",
            "recovery": "recovering",
        }.get(phase, "migrating")
        status["phase"] = phase
        status["phase_label"] = PHASE_LABELS[phase]
        status["phase_percent"] = self._phase_percent(event)
        status["overall_percent"] = self._overall_percent(
            status,
            complete=phase == "complete",
            new_run=new_run,
        )
        status["error_class"] = None
        status["error_code"] = None

        phase_changed = status["phase"] != before["phase"]
        if new_run:
            progressed = any(
                status[name] > 0 for name in (
                    "images_done", "files_done", "backfill_done",
                    "last_committed_batch",
                )
            )
        else:
            progressed = self._event_progressed(before, status)
        if phase_changed or progressed:
            now = self._now()
            if phase_changed:
                status["phase_started_at"] = now
            if progressed:
                status["progress_at"] = now
        return status

    def apply_worker_event(self, event):
        if not self._initialized:
            raise StatusError("runtime_gate_not_prepared")
        self._validate_event(event)
        normalized = dict(event)
        if self._last_worker_event == normalized:
            return self.publish_pending()
        if self._worker_terminal:
            raise StatusError("worker_protocol_invalid")
        if event["phase"] == "complete":
            if self._status["phase"] not in ("preparing", "finalizing"):
                raise StatusError("worker_protocol_invalid")
        reduced = self._reduce_event(event)
        self._last_worker_event = normalized
        if event["phase"] in ("complete", "error"):
            self._worker_terminal = True
        if reduced is None:
            return self.publish_pending()
        payload = _canonical_json(reduced)
        if payload == self._pending_bytes:
            return self.publish_pending()
        self._status = reduced
        self._pending_bytes = payload
        self._dirty = True
        self._publish()

    def heartbeat(self):
        if not self._initialized:
            raise StatusError("runtime_gate_not_prepared")
        now = self._now()
        if now == self._status["heartbeat_at"]:
            return self.publish_pending()
        status = dict(self._status)
        status["heartbeat_at"] = now
        self._project(status)

    def starting_service(self):
        if not self._initialized or self._status["phase"] != "complete":
            raise StatusError("worker_protocol_invalid")
        if self._status["state"] == "starting_service":
            return self.publish_pending()
        if self._status["state"] == "ready":
            return
        if self._status["state"] != "migrating":
            raise StatusError("worker_protocol_invalid")
        status = dict(self._status)
        status["state"] = "starting_service"
        self._project(status)

    def failed(self, error_code):
        if error_code not in ERROR_CODE_CLASSES:
            raise StatusError("worker_protocol_invalid")
        status = dict(self._status)
        status["state"] = "failed"
        status["error_class"] = ERROR_CODE_CLASSES[error_code]
        status["error_code"] = error_code
        payload = _canonical_json(status)
        if payload == self._pending_bytes:
            return self.publish_pending()
        self._status = status
        self._pending_bytes = payload
        self._dirty = True
        self._publish()

    def open_service(self, readiness_confirmed):
        if readiness_confirmed is not True:
            raise StatusError("readiness_not_confirmed")
        if not self._initialized or self._status["phase"] != "complete":
            raise StatusError("readiness_not_confirmed")
        if self._status["state"] not in (
            "migrating", "starting_service", "ready",
        ):
            raise StatusError("readiness_not_confirmed")

        if self._status["state"] != "ready":
            status = dict(self._status)
            status["state"] = "ready"
            self._project(status)
        else:
            self.publish_pending()
        if self._dirty or self._durable_bytes != self._pending_bytes:
            raise StatusError("migration_status_write_failed")
        self._readiness_latch = True

        try:
            self.remove_gate()
        except Exception:
            self._readiness_latch = False
            try:
                self._atomic_write(self.marker_path, _MARKER_BYTES)
            except Exception:
                raise StatusError("runtime_gate_fail_closed_failed")
            try:
                self.failed("runtime_gate_open_failed")
            except StatusError:
                pass
            raise StatusError("runtime_gate_open_failed")

    def remove_gate(self):
        if (
            not self._readiness_latch
            or self._status is None
            or self._status["state"] != "ready"
            or self._dirty
            or self._durable_bytes != _canonical_json(self._status)
        ):
            raise StatusError("readiness_not_confirmed")
        if self._gate_removed:
            if os.path.lexists(self.marker_path):
                raise OSError("maintenance marker returned")
            return
        self._validate_directory()
        self._validate_regular(self.marker_path, _MARKER_BYTES)
        os.unlink(self.marker_path)
        self._fsync_runtime_directory()
        self._gate_removed = True


if set(ERROR_CODE_CLASSES.values()) != ERROR_CLASSES:
    raise RuntimeError("invalid error code class map")
