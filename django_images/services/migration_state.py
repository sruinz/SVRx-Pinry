import copy
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import json
import os
import re
import stat
import uuid

from django.conf import settings


PHASES = (
    "initialized",
    "snapshot_intent",
    "snapshot_complete",
    "schema_complete",
    "copying",
    "paths_complete",
    "registry_complete",
    "archive_intent",
    "archive_complete",
    "complete",
)

PHASE_TRANSITIONS = {
    "initialized": {"snapshot_intent", "schema_complete"},
    "snapshot_intent": {"snapshot_complete"},
    "snapshot_complete": {"schema_complete"},
    "schema_complete": {"copying", "paths_complete"},
    "copying": {"copying", "paths_complete"},
    "paths_complete": {"registry_complete"},
    "registry_complete": {"archive_intent", "complete"},
    "archive_intent": {"archive_intent", "archive_complete"},
    "archive_complete": {"complete"},
    "complete": set(),
}

STATE_FILENAME = "migration-state.json"
FORMAT_VERSION = 2
_RUN_ID_PATTERN = re.compile(
    r"^[0-9]{8}T[0-9]{6}Z-"
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class MigrationStateError(Exception):
    def __init__(self, code):
        super(MigrationStateError, self).__init__(code)
        self.code = code


@dataclass
class MigrationRun(object):
    backup_root: str
    run_id: str
    path: str
    state_path: str
    state: dict
    backup_root_device: int
    backup_root_inode: int
    directory_device: int
    directory_inode: int
    service_uid: int
    service_gid: int


@dataclass(frozen=True)
class RunInventory(object):
    backup_root: str
    root_device: int
    root_inode: int
    completed: tuple
    incomplete: tuple
    invalid_count: int


def scan_run_inventory(backup_root):
    """검증된 완료·미완료·creating run만 분류한다."""
    root_descriptor = _open_configured_backup_root(
        backup_root,
    )
    try:
        root_stat = os.fstat(root_descriptor)
        names = sorted(os.listdir(root_descriptor))
        creating_names = [
            name for name in names if name.startswith(".creating-")
        ]
        if creating_names:
            if len(creating_names) != 1:
                raise MigrationStateError("migration_state_conflict")
            creating_name = creating_names[0]
            run_id = creating_name[len(".creating-"):]
            if not _is_run_id(run_id):
                raise MigrationStateError("migration_state_conflict")
            try:
                _load_run(
                    backup_root,
                    root_descriptor,
                    creating_name,
                    run_id,
                )
                if _entry_exists(root_descriptor, run_id):
                    raise MigrationStateError("migration_state_conflict")
                os.rename(
                    creating_name,
                    run_id,
                    src_dir_fd=root_descriptor,
                    dst_dir_fd=root_descriptor,
                )
                os.fsync(root_descriptor)
            except MigrationStateError:
                raise MigrationStateError("migration_state_conflict") from None
            except (OSError, ValueError):
                raise MigrationStateError("migration_state_conflict") from None
            names = sorted(os.listdir(root_descriptor))

        completed = []
        incomplete = []
        invalid_count = 0
        for name in names:
            if not _is_run_id(name):
                continue
            try:
                run = _load_run(
                    backup_root,
                    root_descriptor,
                    name,
                    name,
                )
            except MigrationStateError:
                invalid_count += 1
                continue
            if run.state["phase"] == "complete":
                completed.append(run)
            else:
                incomplete.append(run)
        return RunInventory(
            backup_root=os.path.abspath(os.fspath(backup_root)),
            root_device=root_stat.st_dev,
            root_inode=root_stat.st_ino,
            completed=tuple(completed),
            incomplete=tuple(incomplete),
            invalid_count=invalid_count,
        )
    finally:
        os.close(root_descriptor)


def resolve_or_create_run(
    inventory,
    evidence,
    pending_schema,
    source_commit,
    service_uid,
    service_gid,
):
    """새 run을 즉시 fchown하거나 같은 미완료 run을 재사용한다."""
    if len(inventory.incomplete) > 1:
        raise MigrationStateError("migration_state_conflict")
    if inventory.incomplete:
        resumed = inventory.incomplete[0]
        if (
            resumed.service_uid != service_uid
            or resumed.service_gid != service_gid
        ):
            raise MigrationStateError("migration_state_missing_or_invalid")
        for identity_name in (
            "database_identity",
            "media_root_identity",
        ):
            current_identity = _json_value(
                _evidence_identity(evidence, identity_name)
            )
            if resumed.state[identity_name] != current_identity:
                raise MigrationStateError("migration_state_identity_changed")
        return resumed

    evidence_present = _evidence_present(evidence)
    if evidence_present and inventory.invalid_count:
        raise MigrationStateError("migration_state_missing_or_invalid")
    if not evidence_present and not pending_schema:
        return None
    if not isinstance(source_commit, str) or not source_commit:
        raise MigrationStateError("migration_state_invalid")

    root_descriptor = _open_configured_backup_root(
        inventory.backup_root,
    )
    try:
        root_stat = os.fstat(root_descriptor)
        if (
            root_stat.st_dev != inventory.root_device
            or root_stat.st_ino != inventory.root_inode
        ):
            raise MigrationStateError("migration_state_conflict")
        run_id = _new_run_id(root_descriptor)
        creating_name = ".creating-{}".format(run_id)
        directory_owned = False
        try:
            os.mkdir(creating_name, mode=0o700, dir_fd=root_descriptor)
            run_descriptor = os.open(
                creating_name,
                _DIRECTORY_FLAGS | _NOFOLLOW,
                dir_fd=root_descriptor,
            )
            try:
                os.fchmod(run_descriptor, 0o700)
                os.fchown(run_descriptor, service_uid, service_gid)
                directory_owned = True
                state = _initial_state(
                    run_id,
                    source_commit,
                    _evidence_identity(evidence, "database_identity"),
                    _evidence_identity(evidence, "media_root_identity"),
                )
                _validate_state(state, run_id)
                _write_new_state(
                    run_descriptor,
                    state,
                    service_uid,
                    service_gid,
                )
                os.fsync(run_descriptor)
            finally:
                os.close(run_descriptor)
            os.rename(
                creating_name,
                run_id,
                src_dir_fd=root_descriptor,
                dst_dir_fd=root_descriptor,
            )
            os.fsync(root_descriptor)
            return _load_run(
                inventory.backup_root,
                root_descriptor,
                run_id,
                run_id,
            )
        except MigrationStateError:
            raise
        except (OSError, TypeError, ValueError):
            if not directory_owned:
                try:
                    os.rmdir(creating_name, dir_fd=root_descriptor)
                except OSError:
                    pass
            raise MigrationStateError("migration_state_create_failed") from None
    finally:
        os.close(root_descriptor)


def transition_state(
    run,
    expected_phase,
    next_phase,
    intent=None,
    plan_sha256=None,
    manifest_sha256=None,
    progress=None,
):
    """승인된 단일 phase 전이만 fsync해 기록한다."""
    root_descriptor = _open_configured_backup_root(
        run.backup_root,
    )
    try:
        root_stat = os.fstat(root_descriptor)
        if (
            root_stat.st_dev != run.backup_root_device
            or root_stat.st_ino != run.backup_root_inode
        ):
            raise MigrationStateError("migration_state_conflict")
        current = _load_run(
            run.backup_root,
            root_descriptor,
            run.run_id,
            run.run_id,
        )
        if (
            current.directory_device != run.directory_device
            or current.directory_inode != run.directory_inode
        ):
            raise MigrationStateError("migration_state_conflict")
        if current.state["phase"] != expected_phase:
            raise MigrationStateError("migration_state_phase_mismatch")
        if expected_phase not in PHASES or next_phase not in PHASES:
            raise MigrationStateError("migration_state_invalid_transition")
        if next_phase not in PHASE_TRANSITIONS[expected_phase]:
            raise MigrationStateError("migration_state_invalid_transition")
        state = copy.deepcopy(current.state)
        state["phase"] = next_phase
        if intent is not None:
            state["intent"] = _json_value(intent)
        if progress is not None:
            state["progress"] = _json_value(progress)
        _update_manifest_hashes(
            state,
            next_phase,
            intent,
            plan_sha256,
            manifest_sha256,
        )
        run_descriptor = os.open(
            run.run_id,
            _DIRECTORY_FLAGS | _NOFOLLOW,
            dir_fd=root_descriptor,
        )
        try:
            run_stat = os.fstat(run_descriptor)
            if (
                run_stat.st_dev != current.directory_device
                or run_stat.st_ino != current.directory_inode
            ):
                raise MigrationStateError("migration_state_conflict")
            _atomic_replace_state(
                run_descriptor,
                state,
                current.service_uid,
                current.service_gid,
            )
        finally:
            os.close(run_descriptor)
        run.state = state
        run.service_uid = current.service_uid
        run.service_gid = current.service_gid
        return run
    finally:
        os.close(root_descriptor)


def _open_absolute_directory(path, error_code):
    try:
        absolute_path = os.path.abspath(os.fspath(path))
    except (TypeError, ValueError):
        raise MigrationStateError(error_code) from None
    if not os.path.isabs(absolute_path):
        raise MigrationStateError(error_code)
    descriptor = None
    try:
        descriptor = os.open("/", _DIRECTORY_FLAGS)
        for component in [item for item in absolute_path.split(os.sep) if item]:
            next_descriptor = os.open(
                component,
                _DIRECTORY_FLAGS | _NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except OSError:
        if descriptor is not None:
            os.close(descriptor)
        raise MigrationStateError(error_code) from None


def _open_configured_backup_root(backup_root):
    try:
        data_root = os.path.abspath(os.fspath(settings.PINRY_DATA_ROOT))
        expected = os.path.join(data_root, "legacy-backup")
        supplied = os.path.abspath(os.fspath(backup_root))
    except (AttributeError, TypeError, ValueError):
        raise MigrationStateError("migration_state_root_invalid") from None
    if supplied != expected:
        raise MigrationStateError("migration_state_root_invalid")
    data_descriptor = _open_absolute_directory(
        data_root,
        "migration_state_root_invalid",
    )
    try:
        descriptor = os.open(
            "legacy-backup",
            _DIRECTORY_FLAGS | _NOFOLLOW,
            dir_fd=data_descriptor,
        )
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise MigrationStateError("migration_state_root_invalid")
        return descriptor
    except MigrationStateError:
        raise
    except OSError:
        raise MigrationStateError("migration_state_root_invalid") from None
    finally:
        os.close(data_descriptor)


def _load_run(backup_root, root_descriptor, entry_name, run_id):
    run_descriptor = None
    state_descriptor = None
    try:
        run_descriptor = os.open(
            entry_name,
            _DIRECTORY_FLAGS | _NOFOLLOW,
            dir_fd=root_descriptor,
        )
        run_stat = os.fstat(run_descriptor)
        if not stat.S_ISDIR(run_stat.st_mode):
            raise MigrationStateError("migration_state_missing_or_invalid")
        if stat.S_IMODE(run_stat.st_mode) != 0o700:
            raise MigrationStateError("migration_state_missing_or_invalid")
        state_descriptor = os.open(
            STATE_FILENAME,
            os.O_RDONLY | _NOFOLLOW,
            dir_fd=run_descriptor,
        )
        state_stat = os.fstat(state_descriptor)
        if (
            not stat.S_ISREG(state_stat.st_mode)
            or state_stat.st_nlink != 1
            or stat.S_IMODE(state_stat.st_mode) != 0o600
            or state_stat.st_uid != run_stat.st_uid
            or state_stat.st_gid != run_stat.st_gid
        ):
            raise MigrationStateError("migration_state_missing_or_invalid")
        raw_state = _read_descriptor(state_descriptor)
        try:
            state = json.loads(raw_state.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise MigrationStateError("migration_state_missing_or_invalid") from None
        _validate_state(state, run_id)
        path = os.path.join(os.path.abspath(os.fspath(backup_root)), entry_name)
        root_stat = os.fstat(root_descriptor)
        return MigrationRun(
            backup_root=os.path.abspath(os.fspath(backup_root)),
            run_id=run_id,
            path=path,
            state_path=os.path.join(path, STATE_FILENAME),
            state=state,
            backup_root_device=root_stat.st_dev,
            backup_root_inode=root_stat.st_ino,
            directory_device=run_stat.st_dev,
            directory_inode=run_stat.st_ino,
            service_uid=run_stat.st_uid,
            service_gid=run_stat.st_gid,
        )
    except MigrationStateError:
        raise
    except (OSError, TypeError, ValueError):
        raise MigrationStateError("migration_state_missing_or_invalid") from None
    finally:
        if state_descriptor is not None:
            os.close(state_descriptor)
        if run_descriptor is not None:
            os.close(run_descriptor)


def _read_descriptor(descriptor):
    chunks = []
    total = 0
    while True:
        chunk = os.read(descriptor, 65536)
        if not chunk:
            break
        total += len(chunk)
        if total > 1024 * 1024:
            raise MigrationStateError("migration_state_missing_or_invalid")
        chunks.append(chunk)
    return b"".join(chunks)


def _validate_state(state, run_id):
    if not isinstance(state, dict):
        raise MigrationStateError("migration_state_missing_or_invalid")
    if (
        state.get("format_version") != FORMAT_VERSION
        or state.get("run_id") != run_id
        or state.get("phase") not in PHASES
        or not isinstance(state.get("source_commit"), str)
        or not state.get("source_commit")
    ):
        raise MigrationStateError("migration_state_missing_or_invalid")
    for identity_name in ("database_identity", "media_root_identity"):
        if identity_name not in state:
            raise MigrationStateError("migration_state_missing_or_invalid")
        _validate_identity(state[identity_name])
    if "intent" not in state or (
        state["intent"] is not None
        and not isinstance(state["intent"], dict)
    ):
        raise MigrationStateError("migration_state_missing_or_invalid")
    if "progress" not in state or (
        state["progress"] is not None
        and not isinstance(state["progress"], dict)
    ):
        raise MigrationStateError("migration_state_missing_or_invalid")
    manifests = state.get("manifests")
    if not isinstance(manifests, dict):
        raise MigrationStateError("migration_state_missing_or_invalid")
    for kind in ("media", "backfill"):
        values = manifests.get(kind)
        if not isinstance(values, dict):
            raise MigrationStateError("migration_state_missing_or_invalid")
        for name in ("plan_sha256", "manifest_sha256"):
            value = values.get(name)
            if value is not None and not _is_sha256(value):
                raise MigrationStateError("migration_state_missing_or_invalid")


def _validate_identity(identity):
    if identity is None:
        return
    if not isinstance(identity, dict):
        raise MigrationStateError("migration_state_missing_or_invalid")
    for field_name in ("device", "inode"):
        value = identity.get(field_name)
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
        ):
            raise MigrationStateError("migration_state_missing_or_invalid")


def _initial_state(run_id, source_commit, database_identity, media_identity):
    return {
        "format_version": FORMAT_VERSION,
        "run_id": run_id,
        "phase": "initialized",
        "source_commit": source_commit,
        "database_identity": _json_value(database_identity),
        "media_root_identity": _json_value(media_identity),
        "manifests": {
            "media": {"plan_sha256": None, "manifest_sha256": None},
            "backfill": {"plan_sha256": None, "manifest_sha256": None},
        },
        "intent": None,
        "progress": None,
    }


def _write_new_state(run_descriptor, state, service_uid, service_gid):
    descriptor = None
    ownership_set = False
    try:
        descriptor = os.open(
            STATE_FILENAME,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
            0o600,
            dir_fd=run_descriptor,
        )
        os.fchmod(descriptor, 0o600)
        os.fchown(descriptor, service_uid, service_gid)
        ownership_set = True
        _write_all(descriptor, _state_bytes(state))
        os.fsync(descriptor)
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
        if not ownership_set:
            try:
                os.unlink(STATE_FILENAME, dir_fd=run_descriptor)
            except OSError:
                pass
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _atomic_replace_state(run_descriptor, state, service_uid, service_gid):
    temp_name = ".migration-state.json.tmp-{}".format(uuid.uuid4())
    descriptor = None
    ownership_set = False
    try:
        descriptor = os.open(
            temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
            0o600,
            dir_fd=run_descriptor,
        )
        os.fchmod(descriptor, 0o600)
        os.fchown(descriptor, service_uid, service_gid)
        ownership_set = True
        _write_all(descriptor, _state_bytes(state))
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(
            temp_name,
            STATE_FILENAME,
            src_dir_fd=run_descriptor,
            dst_dir_fd=run_descriptor,
        )
        os.fsync(run_descriptor)
    except MigrationStateError:
        raise
    except (OSError, TypeError, ValueError):
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
        if not ownership_set:
            try:
                os.unlink(temp_name, dir_fd=run_descriptor)
            except OSError:
                pass
        raise MigrationStateError("migration_state_write_failed") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _write_all(descriptor, content):
    offset = 0
    while offset < len(content):
        written = os.write(descriptor, content[offset:])
        if written <= 0:
            raise MigrationStateError("migration_state_write_failed")
        offset += written


def _state_bytes(state):
    try:
        return (
            json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise MigrationStateError("migration_state_invalid") from None


def _update_manifest_hashes(
    state,
    next_phase,
    intent,
    plan_sha256,
    manifest_sha256,
):
    for field_name, supplied in (
        ("plan_sha256", plan_sha256),
        ("manifest_sha256", manifest_sha256),
    ):
        if supplied is None:
            continue
        if isinstance(supplied, dict):
            items = supplied.items()
        else:
            kind = _manifest_kind(next_phase, intent)
            items = ((kind, supplied),)
        for kind, value in items:
            if kind not in ("media", "backfill") or not _is_sha256(value):
                raise MigrationStateError("migration_state_invalid")
            existing = state["manifests"][kind][field_name]
            if (
                field_name == "plan_sha256"
                and existing is not None
                and existing != value
            ):
                raise MigrationStateError("migration_state_plan_mismatch")
            state["manifests"][kind][field_name] = value


def _manifest_kind(next_phase, intent):
    if isinstance(intent, dict) and intent.get("manifest_kind") in (
        "media",
        "backfill",
    ):
        return intent["manifest_kind"]
    if next_phase in ("copying", "paths_complete"):
        return "media"
    if next_phase == "registry_complete":
        return "backfill"
    raise MigrationStateError("migration_state_invalid")


def _evidence_present(evidence):
    if isinstance(evidence, dict):
        if "present" in evidence:
            return bool(evidence["present"])
        return any(bool(value) for value in evidence.values())
    for name in ("present", "has_legacy", "has_evidence"):
        if hasattr(evidence, name):
            return bool(getattr(evidence, name))
    return bool(evidence)


def _evidence_identity(evidence, name):
    if isinstance(evidence, dict):
        return evidence.get(name)
    return getattr(evidence, name, None)


def _json_value(value):
    try:
        return json.loads(json.dumps(value, sort_keys=True))
    except (TypeError, ValueError):
        raise MigrationStateError("migration_state_invalid") from None


def _new_run_id(root_descriptor):
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for unused_attempt in range(100):
        del unused_attempt
        run_id = "{}-{}".format(timestamp, uuid.uuid4())
        if not _entry_exists(root_descriptor, run_id) and not _entry_exists(
            root_descriptor,
            ".creating-{}".format(run_id),
        ):
            return run_id
    raise MigrationStateError("migration_state_create_failed")


def _entry_exists(directory_descriptor, name):
    try:
        os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        return True
    except OSError as error:
        if error.errno == errno.ENOENT:
            return False
        raise


def _is_run_id(value):
    if not isinstance(value, str) or not _RUN_ID_PATTERN.match(value):
        return False
    try:
        parsed = uuid.UUID(value.split("-", 1)[1])
    except (ValueError, AttributeError):
        return False
    return str(parsed) == value.split("-", 1)[1] and parsed.version == 4


def _is_sha256(value):
    return isinstance(value, str) and _SHA256_PATTERN.match(value) is not None
