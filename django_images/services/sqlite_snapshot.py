from dataclasses import dataclass
import errno
import fcntl
import hashlib
import os
import sqlite3
import stat
from urllib.parse import quote

from django.conf import settings

from django_images.services.migration_state import (
    MigrationStateError,
    _DIRECTORY_FLAGS,
    _NOFOLLOW,
    _load_run,
    _open_absolute_directory,
)


SNAPSHOT_FILENAME = "production.db.before-migration"
SNAPSHOT_TEMP_FILENAME = ".production.db.before-migration.tmp"


class SQLiteSnapshotError(Exception):
    def __init__(self, code):
        super(SQLiteSnapshotError, self).__init__(code)
        self.code = code


@dataclass(frozen=True)
class SnapshotInfo(object):
    size: int
    sha256: str
    source_device: int
    source_inode: int


def snapshot_sqlite(source_path, run, service_uid, service_gid):
    """mode=ro source를 online backup하고 quick_check·fsync한다."""
    configured = settings.DATABASES["default"]
    engine = configured.get("ENGINE")
    if engine != "django.db.backends.sqlite3":
        raise SQLiteSnapshotError(
            "unsupported_legacy_database_backend"
        )
    source_absolute = _configured_source_path(source_path, configured)
    _require_contained_source(source_absolute, settings.PINRY_DATA_ROOT)
    source_stat = _source_identity(source_absolute)
    if source_stat is None:
        return None

    root_descriptor, run_descriptor = _open_verified_run(run)
    try:
        current = _load_run(
            run.backup_root,
            root_descriptor,
            run.run_id,
            run.run_id,
        )
        if (
            current.service_uid != service_uid
            or current.service_gid != service_gid
        ):
            raise SQLiteSnapshotError("sqlite_snapshot_state_invalid")
        if current.state["phase"] != "snapshot_intent":
            raise SQLiteSnapshotError("sqlite_snapshot_phase_invalid")
        _verify_snapshot_intent_source(current.state, source_stat)
        final_exists = _entry_exists(run_descriptor, SNAPSHOT_FILENAME)
        temp_exists = _entry_exists(run_descriptor, SNAPSHOT_TEMP_FILENAME)
        if final_exists and temp_exists:
            _verified_snapshot(
                run_descriptor,
                SNAPSHOT_FILENAME,
                service_uid,
                service_gid,
                source_stat,
            )
            _verified_snapshot(
                run_descriptor,
                SNAPSHOT_TEMP_FILENAME,
                service_uid,
                service_gid,
                source_stat,
            )
            raise SQLiteSnapshotError("sqlite_snapshot_conflict")
        if final_exists:
            info = _verified_snapshot(
                run_descriptor,
                SNAPSHOT_FILENAME,
                service_uid,
                service_gid,
                source_stat,
            )
            _verify_named_run_identity(run, run_descriptor)
            return info
        if temp_exists:
            info = _verified_snapshot(
                run_descriptor,
                SNAPSHOT_TEMP_FILENAME,
                service_uid,
                service_gid,
                source_stat,
            )
            _verify_named_run_identity(run, run_descriptor)
            _promote_snapshot(run_descriptor)
            return info

        _create_snapshot_temp(
            run_descriptor,
            source_absolute,
            source_stat,
            service_uid,
            service_gid,
        )
        _verify_named_run_identity(run, run_descriptor)
        info = _verified_snapshot(
            run_descriptor,
            SNAPSHOT_TEMP_FILENAME,
            service_uid,
            service_gid,
            source_stat,
        )
        _verify_named_run_identity(run, run_descriptor)
        _promote_snapshot(run_descriptor)
        return info
    except SQLiteSnapshotError:
        raise
    except MigrationStateError:
        raise SQLiteSnapshotError("sqlite_snapshot_state_invalid") from None
    except (OSError, sqlite3.Error, TypeError, ValueError):
        raise SQLiteSnapshotError("sqlite_snapshot_failed") from None
    finally:
        os.close(run_descriptor)
        os.close(root_descriptor)


def _configured_source_path(source_path, configured):
    try:
        source_absolute = os.path.abspath(os.fspath(source_path))
        configured_absolute = os.path.abspath(os.fspath(configured.get("NAME")))
    except (TypeError, ValueError):
        raise SQLiteSnapshotError("sqlite_source_not_configured_database") from None
    if source_absolute != configured_absolute:
        raise SQLiteSnapshotError("sqlite_source_not_configured_database")
    return source_absolute


def _require_contained_source(source_path, data_root):
    try:
        root_absolute = os.path.abspath(os.fspath(data_root))
        common = os.path.commonpath((root_absolute, source_path))
    except (TypeError, ValueError):
        raise SQLiteSnapshotError("sqlite_source_outside_data_root") from None
    if common != root_absolute or source_path == root_absolute:
        raise SQLiteSnapshotError("sqlite_source_outside_data_root")
    try:
        descriptor = _open_absolute_directory(
            root_absolute,
            "sqlite_source_outside_data_root",
        )
    except MigrationStateError:
        raise SQLiteSnapshotError("sqlite_source_outside_data_root") from None
    else:
        os.close(descriptor)


def _source_identity(source_path):
    try:
        source_stat = os.lstat(source_path)
    except OSError as error:
        if error.errno == errno.ENOENT:
            return None
        raise SQLiteSnapshotError("unsafe_sqlite_source") from None
    if stat.S_ISLNK(source_stat.st_mode):
        raise SQLiteSnapshotError("unsafe_sqlite_source_symlink")
    if not stat.S_ISREG(source_stat.st_mode):
        raise SQLiteSnapshotError("unsafe_sqlite_source_non_regular")
    if source_stat.st_nlink != 1:
        raise SQLiteSnapshotError("unsafe_sqlite_source_hardlink")

    parent_path, leaf_name = os.path.split(source_path)
    try:
        parent_descriptor = _open_absolute_directory(
            parent_path,
            "unsafe_sqlite_source",
        )
    except MigrationStateError:
        raise SQLiteSnapshotError("unsafe_sqlite_source") from None
    descriptor = None
    try:
        descriptor = os.open(
            leaf_name,
            os.O_RDONLY | _NOFOLLOW,
            dir_fd=parent_descriptor,
        )
        descriptor_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(descriptor_stat.st_mode)
            or descriptor_stat.st_nlink != 1
            or descriptor_stat.st_dev != source_stat.st_dev
            or descriptor_stat.st_ino != source_stat.st_ino
        ):
            raise SQLiteSnapshotError("unsafe_sqlite_source")
        return descriptor_stat
    except SQLiteSnapshotError:
        raise
    except OSError:
        raise SQLiteSnapshotError("unsafe_sqlite_source") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_descriptor)


def _open_verified_run(run):
    try:
        root_descriptor = _open_absolute_directory(
            run.backup_root,
            "sqlite_snapshot_state_invalid",
        )
        run_descriptor = os.open(
            run.run_id,
            _DIRECTORY_FLAGS | _NOFOLLOW,
            dir_fd=root_descriptor,
        )
        run_stat = os.fstat(run_descriptor)
        if (
            run_stat.st_dev != run.directory_device
            or run_stat.st_ino != run.directory_inode
            or stat.S_IMODE(run_stat.st_mode) != 0o700
        ):
            raise SQLiteSnapshotError("sqlite_snapshot_state_invalid")
        return root_descriptor, run_descriptor
    except SQLiteSnapshotError:
        if "run_descriptor" in locals():
            os.close(run_descriptor)
        if "root_descriptor" in locals():
            os.close(root_descriptor)
        raise
    except (MigrationStateError, OSError):
        if "run_descriptor" in locals():
            os.close(run_descriptor)
        if "root_descriptor" in locals():
            os.close(root_descriptor)
        raise SQLiteSnapshotError("sqlite_snapshot_state_invalid") from None


def _create_snapshot_temp(
    run_descriptor,
    source_path,
    source_stat,
    service_uid,
    service_gid,
):
    descriptor = None
    ownership_set = False
    try:
        descriptor = os.open(
            SNAPSHOT_TEMP_FILENAME,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
            0o600,
            dir_fd=run_descriptor,
        )
        os.fchmod(descriptor, 0o600)
        os.fchown(descriptor, service_uid, service_gid)
        ownership_set = True
        _copy_database(
            source_path,
            run_descriptor,
            SNAPSHOT_TEMP_FILENAME,
        )
        os.fsync(descriptor)
        _verify_source_unchanged(source_path, source_stat)
    except SQLiteSnapshotError as error:
        if error.code == "sqlite_source_identity_changed":
            if descriptor is not None:
                os.close(descriptor)
                descriptor = None
            try:
                os.unlink(SNAPSHOT_TEMP_FILENAME, dir_fd=run_descriptor)
                os.fsync(run_descriptor)
            except OSError:
                raise SQLiteSnapshotError("sqlite_snapshot_failed") from None
        raise
    except (OSError, sqlite3.Error):
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
        if not ownership_set:
            try:
                os.unlink(SNAPSHOT_TEMP_FILENAME, dir_fd=run_descriptor)
            except OSError:
                pass
        raise SQLiteSnapshotError("sqlite_snapshot_failed") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _copy_database(source_path, destination_descriptor, destination_name):
    source = None
    destination = None
    try:
        destination_path = os.path.join(
            _directory_descriptor_path(destination_descriptor),
            destination_name,
        )
        source = sqlite3.connect(
            "file:{}?mode=ro".format(quote(source_path, safe="/")),
            uri=True,
        )
        destination = sqlite3.connect(destination_path)
        source.backup(destination)
        destination.commit()
    finally:
        if destination is not None:
            destination.close()
        if source is not None:
            source.close()


def _verified_snapshot(
    run_descriptor,
    filename,
    service_uid,
    service_gid,
    source_stat,
):
    descriptor = None
    connection = None
    try:
        descriptor = os.open(
            filename,
            os.O_RDONLY | _NOFOLLOW,
            dir_fd=run_descriptor,
        )
        snapshot_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(snapshot_stat.st_mode)
            or snapshot_stat.st_nlink != 1
            or stat.S_IMODE(snapshot_stat.st_mode) != 0o600
            or snapshot_stat.st_uid != service_uid
            or snapshot_stat.st_gid != service_gid
        ):
            raise SQLiteSnapshotError("unsafe_sqlite_snapshot")
        connection = sqlite3.connect(
            "file:{}?mode=ro".format(quote(
                os.path.join(
                    _directory_descriptor_path(run_descriptor),
                    filename,
                ),
                safe="/",
            )),
            uri=True,
        )
        result = connection.execute("PRAGMA quick_check").fetchone()
        if result is None or result[0] != "ok":
            raise SQLiteSnapshotError("corrupt_sqlite_snapshot")
        connection.close()
        connection = None
        os.fsync(descriptor)
        digest = _hash_descriptor(descriptor)
        return SnapshotInfo(
            size=snapshot_stat.st_size,
            sha256=digest,
            source_device=source_stat.st_dev,
            source_inode=source_stat.st_ino,
        )
    except SQLiteSnapshotError:
        raise
    except sqlite3.Error:
        raise SQLiteSnapshotError("corrupt_sqlite_snapshot") from None
    except OSError:
        raise SQLiteSnapshotError("unsafe_sqlite_snapshot") from None
    finally:
        if connection is not None:
            connection.close()
        if descriptor is not None:
            os.close(descriptor)


def _promote_snapshot(run_descriptor):
    try:
        if _entry_exists(run_descriptor, SNAPSHOT_FILENAME):
            raise SQLiteSnapshotError("sqlite_snapshot_conflict")
        os.rename(
            SNAPSHOT_TEMP_FILENAME,
            SNAPSHOT_FILENAME,
            src_dir_fd=run_descriptor,
            dst_dir_fd=run_descriptor,
        )
        os.fsync(run_descriptor)
    except SQLiteSnapshotError:
        raise
    except OSError:
        raise SQLiteSnapshotError("sqlite_snapshot_failed") from None


def _verify_source_unchanged(source_path, expected_stat):
    current = _source_identity(source_path)
    if current is None or (
        current.st_dev != expected_stat.st_dev
        or current.st_ino != expected_stat.st_ino
        or current.st_size != expected_stat.st_size
    ):
        raise SQLiteSnapshotError("sqlite_source_identity_changed")


def _verify_snapshot_intent_source(state, source_stat):
    intent = state.get("intent")
    if intent is None:
        return
    if not isinstance(intent, dict):
        raise SQLiteSnapshotError("sqlite_snapshot_state_invalid")
    has_device = "source_device" in intent
    has_inode = "source_inode" in intent
    if has_device != has_inode:
        raise SQLiteSnapshotError("sqlite_snapshot_state_invalid")
    if has_device and (
        intent["source_device"] != source_stat.st_dev
        or intent["source_inode"] != source_stat.st_ino
    ):
        raise SQLiteSnapshotError("sqlite_source_identity_changed")


def _verify_named_run_identity(run, run_descriptor):
    named_descriptor = None
    try:
        named_descriptor = _open_absolute_directory(
            run.path,
            "sqlite_snapshot_state_invalid",
        )
        named_stat = os.fstat(named_descriptor)
        held_stat = os.fstat(run_descriptor)
        if (
            named_stat.st_dev != held_stat.st_dev
            or named_stat.st_ino != held_stat.st_ino
        ):
            raise SQLiteSnapshotError("sqlite_snapshot_state_invalid")
    except SQLiteSnapshotError:
        raise
    except (MigrationStateError, OSError):
        raise SQLiteSnapshotError("sqlite_snapshot_state_invalid") from None
    finally:
        if named_descriptor is not None:
            os.close(named_descriptor)


def _directory_descriptor_path(descriptor):
    proc_path = "/proc/self/fd/{}".format(descriptor)
    if os.path.exists(proc_path):
        return proc_path
    try:
        raw_path = fcntl.fcntl(descriptor, 50, b"\0" * 1024)
        path = raw_path.split(b"\0", 1)[0].decode("utf-8")
    except (OSError, UnicodeDecodeError):
        raise SQLiteSnapshotError("sqlite_snapshot_state_invalid") from None
    if not path:
        raise SQLiteSnapshotError("sqlite_snapshot_state_invalid")
    return path


def _hash_descriptor(descriptor):
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)


def _entry_exists(directory_descriptor, filename):
    try:
        os.stat(
            filename,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        return True
    except OSError as error:
        if error.errno == errno.ENOENT:
            return False
        raise
