from dataclasses import dataclass
from contextlib import contextmanager
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import select
import sqlite3
import stat
from urllib.parse import quote

from django.conf import settings

from django_images.services.migration_state import (
    MigrationStateError,
    _DIRECTORY_FLAGS,
    _NOFOLLOW,
    _is_identity_number,
    _is_sha256,
    _load_run,
    _open_absolute_directory,
    _open_configured_backup_root,
    _read_descriptor,
    _write_all,
)


SNAPSHOT_FILENAME = "production.db.before-migration"
SNAPSHOT_TEMP_FILENAME = ".production.db.before-migration.tmp"
SNAPSHOT_RECEIPT_FILENAME = ".production.db.before-migration.receipt"
CLEANUP_SNAPSHOT_FILENAME = "production.db.before-orphan-cleanup"
CLEANUP_TEMP_FILENAME = ".production.db.before-orphan-cleanup.tmp"
CLEANUP_RECEIPT_FILENAME = ".production.db.before-orphan-cleanup.receipt"
CLEANUP_RECEIPT_KIND = "missing_unreferenced_image_cleanup"
_SNAPSHOT_RECEIPT_KEYS = frozenset((
    "size",
    "sha256",
    "source_device",
    "source_inode",
))
_CLEANUP_RECEIPT_KEYS = _SNAPSHOT_RECEIPT_KEYS | frozenset((
    "format_version",
    "kind",
    "run_id",
))


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


@dataclass(frozen=True)
class _SnapshotSourceStat(object):
    st_dev: int
    st_ino: int


class _SQLiteSource(object):
    def __init__(
        self,
        path,
        parent_descriptor,
        descriptor,
        file_stat,
        connection,
        mutation_guard,
    ):
        self.path = path
        self.parent_descriptor = parent_descriptor
        self.descriptor = descriptor
        self.file_stat = file_stat
        self.connection = connection
        self.mutation_guard = mutation_guard

    def verify_current(self):
        self.mutation_guard.verify_unchanged()
        try:
            descriptor_stat = os.fstat(self.descriptor)
            named_stat = os.stat(
                os.path.basename(self.path),
                dir_fd=self.parent_descriptor,
                follow_symlinks=False,
            )
        except OSError:
            raise SQLiteSnapshotError("sqlite_source_identity_changed") from None
        for current in (descriptor_stat, named_stat):
            if (
                not stat.S_ISREG(current.st_mode)
                or current.st_nlink != 1
                or current.st_dev != self.file_stat.st_dev
                or current.st_ino != self.file_stat.st_ino
                or current.st_size != self.file_stat.st_size
            ):
                raise SQLiteSnapshotError("sqlite_source_identity_changed")

    def close(self):
        if self.connection is not None:
            connection = self.connection
            self.connection = None
            connection.close()
        if self.mutation_guard is not None:
            mutation_guard = self.mutation_guard
            self.mutation_guard = None
            mutation_guard.close()
        if self.descriptor is not None:
            descriptor = self.descriptor
            self.descriptor = None
            os.close(descriptor)
        if self.parent_descriptor is not None:
            parent_descriptor = self.parent_descriptor
            self.parent_descriptor = None
            os.close(parent_descriptor)


class _SourceMutationGuard(object):
    def __init__(self, descriptor):
        self._kqueue = None
        self._inotify_descriptor = None
        if hasattr(select, "kqueue"):
            self._open_kqueue(descriptor)
        elif os.path.isdir("/proc/self/fd"):
            self._open_inotify(descriptor)
        else:
            raise SQLiteSnapshotError("sqlite_source_identity_changed")

    def _open_kqueue(self, descriptor):
        queue = select.kqueue()
        flags = select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME
        flags |= getattr(select, "KQ_NOTE_REVOKE", 0)
        event = select.kevent(
            descriptor,
            filter=select.KQ_FILTER_VNODE,
            flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
            fflags=flags,
        )
        try:
            queue.control([event], 0, 0)
        except OSError:
            queue.close()
            raise SQLiteSnapshotError(
                "sqlite_source_identity_changed"
            ) from None
        self._kqueue = queue

    def _open_inotify(self, descriptor):
        libc = ctypes.CDLL(None, use_errno=True)
        try:
            initialize = libc.inotify_init1
            add_watch = libc.inotify_add_watch
        except AttributeError:
            raise SQLiteSnapshotError(
                "sqlite_source_identity_changed"
            ) from None
        initialize.argtypes = [ctypes.c_int]
        initialize.restype = ctypes.c_int
        add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        add_watch.restype = ctypes.c_int
        watch_descriptor = initialize(os.O_NONBLOCK | os.O_CLOEXEC)
        if watch_descriptor < 0:
            raise SQLiteSnapshotError("sqlite_source_identity_changed")
        source_path = "/proc/self/fd/{}".format(descriptor).encode("ascii")
        watch_mask = 0x00000400 | 0x00000800
        if add_watch(watch_descriptor, source_path, watch_mask) < 0:
            os.close(watch_descriptor)
            raise SQLiteSnapshotError("sqlite_source_identity_changed")
        self._inotify_descriptor = watch_descriptor

    def verify_unchanged(self):
        if self._kqueue is not None:
            try:
                events = self._kqueue.control([], 1, 0)
            except OSError:
                raise SQLiteSnapshotError(
                    "sqlite_source_identity_changed"
                ) from None
            if events:
                raise SQLiteSnapshotError("sqlite_source_identity_changed")
            return
        try:
            event = os.read(self._inotify_descriptor, 4096)
        except OSError as error:
            if error.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return
            raise SQLiteSnapshotError(
                "sqlite_source_identity_changed"
            ) from None
        if event:
            raise SQLiteSnapshotError("sqlite_source_identity_changed")

    def close(self):
        if self._kqueue is not None:
            queue = self._kqueue
            self._kqueue = None
            queue.close()
        if self._inotify_descriptor is not None:
            descriptor = self._inotify_descriptor
            self._inotify_descriptor = None
            os.close(descriptor)


class _HeldPrivateEntry(object):
    def __init__(
        self,
        run_descriptor,
        filename,
        descriptor,
        file_stat,
        mutation_guard,
        service_uid,
        service_gid,
        identity_error,
    ):
        self.run_descriptor = run_descriptor
        self.filename = filename
        self.descriptor = descriptor
        self.file_stat = file_stat
        self.mutation_guard = mutation_guard
        self.service_uid = service_uid
        self.service_gid = service_gid
        self.identity_error = identity_error

    @classmethod
    def open(
        cls,
        run_descriptor,
        filename,
        service_uid,
        service_gid,
        unsafe_error,
        identity_error="sqlite_snapshot_conflict",
    ):
        descriptor = _open_private_entry(
            run_descriptor,
            filename,
            service_uid,
            service_gid,
            unsafe_error,
        )
        mutation_guard = None
        try:
            file_stat = os.fstat(descriptor)
            mutation_guard = _SourceMutationGuard(descriptor)
            entry = cls(
                run_descriptor=run_descriptor,
                filename=filename,
                descriptor=descriptor,
                file_stat=file_stat,
                mutation_guard=mutation_guard,
                service_uid=service_uid,
                service_gid=service_gid,
                identity_error=identity_error,
            )
            entry.verify_current()
            descriptor = None
            mutation_guard = None
            return entry
        except SQLiteSnapshotError as error:
            if error.code == "sqlite_source_identity_changed":
                raise SQLiteSnapshotError(identity_error) from None
            raise
        except OSError:
            raise SQLiteSnapshotError(identity_error) from None
        finally:
            if mutation_guard is not None:
                mutation_guard.close()
            if descriptor is not None:
                os.close(descriptor)

    def verify_current(self):
        if self.descriptor is None or self.mutation_guard is None:
            raise SQLiteSnapshotError(self.identity_error)
        try:
            self.mutation_guard.verify_unchanged()
            descriptor_stat = os.fstat(self.descriptor)
            named_stat = os.stat(
                self.filename,
                dir_fd=self.run_descriptor,
                follow_symlinks=False,
            )
        except (OSError, SQLiteSnapshotError):
            raise SQLiteSnapshotError(self.identity_error) from None
        for current in (descriptor_stat, named_stat):
            if (
                not _is_private_regular_file(
                    current,
                    self.service_uid,
                    self.service_gid,
                )
                or current.st_dev != self.file_stat.st_dev
                or current.st_ino != self.file_stat.st_ino
                or current.st_size != self.file_stat.st_size
            ):
                raise SQLiteSnapshotError(self.identity_error)

    def rebind_after_rename(self, filename):
        _require_private_leaf_name(filename)
        if self.descriptor is None or self.mutation_guard is None:
            raise SQLiteSnapshotError(self.identity_error)
        replacement_guard = None
        try:
            replacement_guard = _SourceMutationGuard(self.descriptor)
            descriptor_stat = os.fstat(self.descriptor)
            named_stat = os.stat(
                filename,
                dir_fd=self.run_descriptor,
                follow_symlinks=False,
            )
            for current in (descriptor_stat, named_stat):
                if (
                    not _is_private_regular_file(
                        current,
                        self.service_uid,
                        self.service_gid,
                    )
                    or current.st_dev != self.file_stat.st_dev
                    or current.st_ino != self.file_stat.st_ino
                    or current.st_size != self.file_stat.st_size
                ):
                    raise SQLiteSnapshotError(self.identity_error)
            replacement_guard.verify_unchanged()
        except (OSError, SQLiteSnapshotError):
            if replacement_guard is not None:
                replacement_guard.close()
            raise SQLiteSnapshotError(self.identity_error) from None
        previous_guard = self.mutation_guard
        self.mutation_guard = replacement_guard
        self.filename = filename
        previous_guard.close()
        self.verify_current()

    def close(self):
        if self.mutation_guard is not None:
            mutation_guard = self.mutation_guard
            self.mutation_guard = None
            mutation_guard.close()
        if self.descriptor is not None:
            descriptor = self.descriptor
            self.descriptor = None
            os.close(descriptor)


class VerifiedCompletedSQLiteSnapshot(object):
    """cleanup commit 직전까지 snapshot 근거를 잠그는 가드."""

    def __init__(
        self,
        run,
        root_descriptor,
        run_descriptor,
        snapshot_entry,
        receipt_entry,
        service_uid,
        service_gid,
        source_stat,
        temp_filename,
        receipt_kind,
        allowed_phases,
    ):
        self.run = run
        self.root_descriptor = root_descriptor
        self.run_descriptor = run_descriptor
        self.snapshot_entry = snapshot_entry
        self.receipt_entry = receipt_entry
        self.service_uid = service_uid
        self.service_gid = service_gid
        self.source_stat = source_stat
        self.temp_filename = temp_filename
        self.receipt_kind = receipt_kind
        self.allowed_phases = allowed_phases
        self.info = None

    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception, traceback):
        self.close()

    def verify_current(self):
        if self.run_descriptor is None or self.root_descriptor is None:
            raise SQLiteSnapshotError("sqlite_snapshot_conflict")
        _verify_completed_snapshot_state(
            self.run,
            self.root_descriptor,
            self.service_uid,
            self.service_gid,
            self.source_stat,
            self.allowed_phases,
        )
        if self.temp_filename is not None and _entry_exists(
            self.run_descriptor,
            self.temp_filename,
        ):
            raise SQLiteSnapshotError("sqlite_snapshot_conflict")
        receipt = _read_held_snapshot_receipt(
            self.receipt_entry,
            receipt_kind=self.receipt_kind,
            run_id=self.run.run_id,
        )
        info = _verified_held_snapshot(
            self.snapshot_entry,
            self.source_stat,
        )
        _verify_snapshot_receipt(
            receipt,
            info,
            receipt_kind=self.receipt_kind,
            run_id=self.run.run_id,
        )
        self.receipt_entry.verify_current()
        self.snapshot_entry.verify_current()
        _verify_named_run_identity(self.run, self.run_descriptor)
        self.info = info
        return info

    def close(self):
        if self.receipt_entry is not None:
            receipt_entry = self.receipt_entry
            self.receipt_entry = None
            receipt_entry.close()
        if self.snapshot_entry is not None:
            snapshot_entry = self.snapshot_entry
            self.snapshot_entry = None
            snapshot_entry.close()
        if self.run_descriptor is not None:
            run_descriptor = self.run_descriptor
            self.run_descriptor = None
            os.close(run_descriptor)
        if self.root_descriptor is not None:
            root_descriptor = self.root_descriptor
            self.root_descriptor = None
            os.close(root_descriptor)


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
    source = _open_sqlite_source(source_absolute)
    if source is None:
        return None

    root_descriptor = None
    run_descriptor = None
    try:
        root_descriptor, run_descriptor = _open_verified_run(run)
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
        _verify_snapshot_intent_source(current.state, source.file_stat)
        final_exists = _entry_exists(run_descriptor, SNAPSHOT_FILENAME)
        temp_exists = _entry_exists(run_descriptor, SNAPSHOT_TEMP_FILENAME)
        receipt_exists = _entry_exists(
            run_descriptor,
            SNAPSHOT_RECEIPT_FILENAME,
        )
        if final_exists and temp_exists:
            raise SQLiteSnapshotError("sqlite_snapshot_conflict")
        if receipt_exists:
            if not final_exists and not temp_exists:
                raise SQLiteSnapshotError("sqlite_snapshot_conflict")
            snapshot_name = (
                SNAPSHOT_FILENAME if final_exists else SNAPSHOT_TEMP_FILENAME
            )
            return _finish_receipted_snapshot(
                run,
                run_descriptor,
                source,
                snapshot_name,
                service_uid,
                service_gid,
            )
        if final_exists:
            info = _verified_snapshot(
                run_descriptor,
                SNAPSHOT_FILENAME,
                service_uid,
                service_gid,
                source.file_stat,
            )
            _verify_named_run_identity(run, run_descriptor)
            source.verify_current()
            return info
        if temp_exists:
            _remove_private_entry(
                run_descriptor,
                SNAPSHOT_TEMP_FILENAME,
                service_uid,
                service_gid,
                "unsafe_sqlite_snapshot",
            )
            _verify_named_run_identity(run, run_descriptor)
            source.verify_current()

        _create_snapshot_temp(
            run_descriptor,
            source,
            service_uid,
            service_gid,
        )
        _verify_named_run_identity(run, run_descriptor)
        info = _verified_snapshot(
            run_descriptor,
            SNAPSHOT_TEMP_FILENAME,
            service_uid,
            service_gid,
            source.file_stat,
        )
        _verify_named_run_identity(run, run_descriptor)
        source.verify_current()
        _create_snapshot_receipt(
            run_descriptor,
            info,
            service_uid,
            service_gid,
        )
        return _finish_receipted_snapshot(
            run,
            run_descriptor,
            source,
            SNAPSHOT_TEMP_FILENAME,
            service_uid,
            service_gid,
        )
    except SQLiteSnapshotError:
        raise
    except MigrationStateError:
        raise SQLiteSnapshotError("sqlite_snapshot_state_invalid") from None
    except (OSError, sqlite3.Error, TypeError, ValueError):
        raise SQLiteSnapshotError("sqlite_snapshot_failed") from None
    finally:
        if run_descriptor is not None:
            os.close(run_descriptor)
        if root_descriptor is not None:
            os.close(root_descriptor)
        source.close()


def verify_completed_sqlite_snapshot(run, service_uid, service_gid):
    """cleanup 직전의 완료 snapshot과 영수증을 재검증한다."""
    with open_verified_completed_sqlite_snapshot(
        run,
        service_uid,
        service_gid,
    ) as guard:
        return guard.info


@contextmanager
def protect_orphan_cleanup(
    source_path,
    run,
    service_uid,
    service_gid,
    before_fallback_create=None,
):
    """orphan cleanup이 commit할 때까지 복구 가능한 snapshot을 유지한다."""
    allowed_phases = (
        "snapshot_intent",
        "snapshot_complete",
        "schema_complete",
    )
    final_exists, temp_exists, receipt_exists = _snapshot_inventory(
        run,
        service_uid,
        service_gid,
        allowed_phases,
        SNAPSHOT_FILENAME,
        SNAPSHOT_TEMP_FILENAME,
        SNAPSHOT_RECEIPT_FILENAME,
    )
    if final_exists and not temp_exists and receipt_exists:
        guard = open_verified_completed_sqlite_snapshot(
            run,
            service_uid,
            service_gid,
            allowed_phases=allowed_phases,
        )
    elif final_exists and not temp_exists and not receipt_exists:
        _ensure_cleanup_snapshot(
            source_path,
            run,
            service_uid,
            service_gid,
            allowed_phases,
            before_fallback_create,
        )
        guard = open_verified_completed_sqlite_snapshot(
            run,
            service_uid,
            service_gid,
            snapshot_filename=CLEANUP_SNAPSHOT_FILENAME,
            receipt_filename=CLEANUP_RECEIPT_FILENAME,
            temp_filename=CLEANUP_TEMP_FILENAME,
            receipt_kind=CLEANUP_RECEIPT_KIND,
            allowed_phases=allowed_phases,
        )
    else:
        raise SQLiteSnapshotError("sqlite_snapshot_conflict")
    try:
        with guard:
            yield guard
    finally:
        guard.close()


def _snapshot_inventory(
    run,
    service_uid,
    service_gid,
    allowed_phases,
    snapshot_filename,
    temp_filename,
    receipt_filename,
):
    root_descriptor = None
    run_descriptor = None
    try:
        root_descriptor, run_descriptor = _open_verified_run(run)
        _completed_snapshot_source_stat(
            run,
            root_descriptor,
            service_uid,
            service_gid,
            allowed_phases,
        )
        inventory = tuple(
            _entry_exists(run_descriptor, filename)
            for filename in (
                snapshot_filename,
                temp_filename,
                receipt_filename,
            )
        )
        _verify_named_run_identity(run, run_descriptor)
        return inventory
    except SQLiteSnapshotError:
        raise
    except (MigrationStateError, OSError, TypeError, ValueError):
        raise SQLiteSnapshotError("sqlite_snapshot_conflict") from None
    finally:
        if run_descriptor is not None:
            os.close(run_descriptor)
        if root_descriptor is not None:
            os.close(root_descriptor)


def _ensure_cleanup_snapshot(
    source_path,
    run,
    service_uid,
    service_gid,
    allowed_phases,
    before_fallback_create,
):
    configured = settings.DATABASES["default"]
    if configured.get("ENGINE") != "django.db.backends.sqlite3":
        raise SQLiteSnapshotError("unsupported_legacy_database_backend")
    source_absolute = _configured_source_path(source_path, configured)
    _require_contained_source(source_absolute, settings.PINRY_DATA_ROOT)
    source = _open_sqlite_source(source_absolute)
    if source is None:
        raise SQLiteSnapshotError("sqlite_snapshot_conflict")
    root_descriptor = None
    run_descriptor = None
    try:
        root_descriptor, run_descriptor = _open_verified_run(run)
        expected_source = _completed_snapshot_source_stat(
            run,
            root_descriptor,
            service_uid,
            service_gid,
            allowed_phases,
        )
        if (
            source.file_stat.st_dev != expected_source.st_dev
            or source.file_stat.st_ino != expected_source.st_ino
        ):
            raise SQLiteSnapshotError("sqlite_source_identity_changed")
        original_inventory = tuple(
            _entry_exists(run_descriptor, filename)
            for filename in (
                SNAPSHOT_FILENAME,
                SNAPSHOT_TEMP_FILENAME,
                SNAPSHOT_RECEIPT_FILENAME,
            )
        )
        if original_inventory != (True, False, False):
            raise SQLiteSnapshotError("sqlite_snapshot_conflict")
        _verified_snapshot(
            run_descriptor,
            SNAPSHOT_FILENAME,
            service_uid,
            service_gid,
            source.file_stat,
        )
        source.verify_current()
        inventory = tuple(
            _entry_exists(run_descriptor, filename)
            for filename in (
                CLEANUP_SNAPSHOT_FILENAME,
                CLEANUP_TEMP_FILENAME,
                CLEANUP_RECEIPT_FILENAME,
            )
        )
        if inventory == (True, False, True):
            _verify_cleanup_snapshot_pair(
                run_descriptor,
                CLEANUP_SNAPSHOT_FILENAME,
                source,
                run.run_id,
                service_uid,
                service_gid,
            )
            return
        if inventory == (False, True, True):
            _verify_cleanup_snapshot_pair(
                run_descriptor,
                CLEANUP_TEMP_FILENAME,
                source,
                run.run_id,
                service_uid,
                service_gid,
            )
            source.verify_current()
            _promote_snapshot(
                run_descriptor,
                CLEANUP_TEMP_FILENAME,
                CLEANUP_SNAPSHOT_FILENAME,
            )
            _verify_cleanup_snapshot_pair(
                run_descriptor,
                CLEANUP_SNAPSHOT_FILENAME,
                source,
                run.run_id,
                service_uid,
                service_gid,
            )
            return
        if inventory not in ((False, False, False), (False, True, False)):
            raise SQLiteSnapshotError("sqlite_snapshot_conflict")
        if before_fallback_create is not None:
            if not callable(before_fallback_create):
                raise SQLiteSnapshotError("sqlite_snapshot_state_invalid")
            before_fallback_create(source.file_stat.st_size)
        source.verify_current()
        if inventory == (False, True, False):
            _remove_private_entry(
                run_descriptor,
                CLEANUP_TEMP_FILENAME,
                service_uid,
                service_gid,
                "unsafe_sqlite_snapshot",
            )
            source.verify_current()
        _create_snapshot_temp(
            run_descriptor,
            source,
            service_uid,
            service_gid,
            temp_filename=CLEANUP_TEMP_FILENAME,
        )
        info = _verified_snapshot(
            run_descriptor,
            CLEANUP_TEMP_FILENAME,
            service_uid,
            service_gid,
            source.file_stat,
        )
        receipt = _receipt_for_snapshot(info)
        receipt.update({
            "format_version": 1,
            "kind": CLEANUP_RECEIPT_KIND,
            "run_id": run.run_id,
        })
        _create_private_receipt(
            run_descriptor,
            CLEANUP_RECEIPT_FILENAME,
            receipt,
            service_uid,
            service_gid,
        )
        source.verify_current()
        _promote_snapshot(
            run_descriptor,
            CLEANUP_TEMP_FILENAME,
            CLEANUP_SNAPSHOT_FILENAME,
        )
        _verify_cleanup_snapshot_pair(
            run_descriptor,
            CLEANUP_SNAPSHOT_FILENAME,
            source,
            run.run_id,
            service_uid,
            service_gid,
        )
    except SQLiteSnapshotError:
        raise
    except (MigrationStateError, OSError, sqlite3.Error, TypeError, ValueError):
        raise SQLiteSnapshotError("sqlite_snapshot_conflict") from None
    finally:
        if run_descriptor is not None:
            os.close(run_descriptor)
        if root_descriptor is not None:
            os.close(root_descriptor)
        source.close()


def _verify_cleanup_snapshot_pair(
    run_descriptor,
    snapshot_filename,
    source,
    run_id,
    service_uid,
    service_gid,
):
    receipt = _read_snapshot_receipt(
        run_descriptor,
        service_uid,
        service_gid,
        filename=CLEANUP_RECEIPT_FILENAME,
        receipt_kind=CLEANUP_RECEIPT_KIND,
        run_id=run_id,
    )
    info = _verified_snapshot(
        run_descriptor,
        snapshot_filename,
        service_uid,
        service_gid,
        source.file_stat,
    )
    _verify_snapshot_receipt(
        receipt,
        info,
        receipt_kind=CLEANUP_RECEIPT_KIND,
        run_id=run_id,
    )
    source.verify_current()


def open_verified_completed_sqlite_snapshot(
    run,
    service_uid,
    service_gid,
    snapshot_filename=SNAPSHOT_FILENAME,
    receipt_filename=SNAPSHOT_RECEIPT_FILENAME,
    temp_filename=SNAPSHOT_TEMP_FILENAME,
    receipt_kind=None,
    allowed_phases=("snapshot_complete", "schema_complete"),
):
    """snapshot과 영수증을 열어둔 채 commit 직전 재검증할 가드를 반환한다."""
    _require_private_leaf_name(snapshot_filename)
    _require_private_leaf_name(receipt_filename)
    if temp_filename is not None:
        _require_private_leaf_name(temp_filename)
    if (
        not isinstance(allowed_phases, tuple)
        or not allowed_phases
        or any(not isinstance(phase, str) for phase in allowed_phases)
    ):
        raise SQLiteSnapshotError("sqlite_snapshot_state_invalid")
    root_descriptor = None
    run_descriptor = None
    snapshot_entry = None
    receipt_entry = None
    try:
        root_descriptor, run_descriptor = _open_verified_run(run)
        source_stat = _completed_snapshot_source_stat(
            run,
            root_descriptor,
            service_uid,
            service_gid,
            allowed_phases,
        )
        if (
            not _entry_exists(run_descriptor, snapshot_filename)
            or not _entry_exists(run_descriptor, receipt_filename)
            or (
                temp_filename is not None
                and _entry_exists(run_descriptor, temp_filename)
            )
        ):
            raise SQLiteSnapshotError("sqlite_snapshot_conflict")
        snapshot_entry = _HeldPrivateEntry.open(
            run_descriptor,
            snapshot_filename,
            service_uid,
            service_gid,
            "unsafe_sqlite_snapshot",
        )
        receipt_entry = _HeldPrivateEntry.open(
            run_descriptor,
            receipt_filename,
            service_uid,
            service_gid,
            "sqlite_snapshot_conflict",
        )
        guard = VerifiedCompletedSQLiteSnapshot(
            run=run,
            root_descriptor=root_descriptor,
            run_descriptor=run_descriptor,
            snapshot_entry=snapshot_entry,
            receipt_entry=receipt_entry,
            service_uid=service_uid,
            service_gid=service_gid,
            source_stat=source_stat,
            temp_filename=temp_filename,
            receipt_kind=receipt_kind,
            allowed_phases=allowed_phases,
        )
        root_descriptor = None
        run_descriptor = None
        snapshot_entry = None
        receipt_entry = None
        guard.verify_current()
        return guard
    except SQLiteSnapshotError:
        raise
    except MigrationStateError:
        raise SQLiteSnapshotError("sqlite_snapshot_state_invalid") from None
    except (OSError, sqlite3.Error, TypeError, ValueError):
        raise SQLiteSnapshotError("sqlite_snapshot_conflict") from None
    finally:
        if receipt_entry is not None:
            receipt_entry.close()
        if snapshot_entry is not None:
            snapshot_entry.close()
        if run_descriptor is not None:
            os.close(run_descriptor)
        if root_descriptor is not None:
            os.close(root_descriptor)


def _completed_snapshot_source_stat(
    run,
    root_descriptor,
    service_uid,
    service_gid,
    allowed_phases,
):
    current = _load_run(
        run.backup_root,
        root_descriptor,
        run.run_id,
        run.run_id,
    )
    if (
        current.service_uid != service_uid
        or current.service_gid != service_gid
        or current.state["phase"]
        not in allowed_phases
    ):
        raise SQLiteSnapshotError("sqlite_snapshot_state_invalid")
    source_identity = current.state["database_identity"]
    if (
        not isinstance(source_identity, dict)
        or set(source_identity) != {"device", "inode"}
        or not _is_identity_number(source_identity["device"])
        or not _is_identity_number(source_identity["inode"])
    ):
        raise SQLiteSnapshotError("sqlite_snapshot_state_invalid")
    return _SnapshotSourceStat(
        st_dev=source_identity["device"],
        st_ino=source_identity["inode"],
    )


def _verify_completed_snapshot_state(
    run,
    root_descriptor,
    service_uid,
    service_gid,
    expected_source_stat,
    allowed_phases,
):
    current_source_stat = _completed_snapshot_source_stat(
        run,
        root_descriptor,
        service_uid,
        service_gid,
        allowed_phases,
    )
    if current_source_stat != expected_source_stat:
        raise SQLiteSnapshotError("sqlite_snapshot_conflict")


def _require_private_leaf_name(filename):
    if (
        not isinstance(filename, str)
        or not filename
        or filename in (".", "..")
        or os.path.basename(filename) != filename
    ):
        raise SQLiteSnapshotError("sqlite_snapshot_state_invalid")


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


def _open_sqlite_source(source_path):
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
    connection = None
    mutation_guard = None
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
        connection_path = os.path.join(
            _directory_descriptor_path(parent_descriptor),
            leaf_name,
        )
        mutation_guard = _SourceMutationGuard(descriptor)
        connection = sqlite3.connect(
            "file:{}?mode=ro".format(quote(connection_path, safe="/")),
            uri=True,
        )
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        connection.execute(
            "SELECT rootpage FROM sqlite_master LIMIT 1"
        ).fetchone()
        source = _SQLiteSource(
            path=source_path,
            parent_descriptor=parent_descriptor,
            descriptor=descriptor,
            file_stat=descriptor_stat,
            connection=connection,
            mutation_guard=mutation_guard,
        )
        source.verify_current()
        parent_descriptor = None
        descriptor = None
        connection = None
        mutation_guard = None
        return source
    except SQLiteSnapshotError:
        raise
    except (OSError, sqlite3.Error):
        raise SQLiteSnapshotError("unsafe_sqlite_source") from None
    finally:
        if connection is not None:
            connection.close()
        if mutation_guard is not None:
            mutation_guard.close()
        if descriptor is not None:
            os.close(descriptor)
        if parent_descriptor is not None:
            os.close(parent_descriptor)


def _open_verified_run(run):
    try:
        root_descriptor = _open_configured_backup_root(
            run.backup_root,
        )
        root_stat = os.fstat(root_descriptor)
        if (
            root_stat.st_dev != run.backup_root_device
            or root_stat.st_ino != run.backup_root_inode
        ):
            raise SQLiteSnapshotError("sqlite_snapshot_state_invalid")
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
    source,
    service_uid,
    service_gid,
    temp_filename=SNAPSHOT_TEMP_FILENAME,
):
    descriptor = None
    ownership_set = False
    try:
        descriptor = os.open(
            temp_filename,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
            0o600,
            dir_fd=run_descriptor,
        )
        os.fchmod(descriptor, 0o600)
        os.fchown(descriptor, service_uid, service_gid)
        ownership_set = True
        _copy_database(
            source,
            run_descriptor,
            temp_filename,
        )
        os.fsync(descriptor)
        source.verify_current()
    except SQLiteSnapshotError as error:
        if error.code == "sqlite_source_identity_changed":
            if descriptor is not None:
                os.close(descriptor)
                descriptor = None
            try:
                os.unlink(temp_filename, dir_fd=run_descriptor)
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
                os.unlink(temp_filename, dir_fd=run_descriptor)
            except OSError:
                pass
        raise SQLiteSnapshotError("sqlite_snapshot_failed") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _copy_database(source, destination_descriptor, destination_name):
    destination = None
    try:
        destination_path = os.path.join(
            _directory_descriptor_path(destination_descriptor),
            destination_name,
        )
        destination = sqlite3.connect(destination_path)
        source.connection.backup(destination)
        destination.commit()
    finally:
        if destination is not None:
            destination.close()


def _verified_snapshot(
    run_descriptor,
    filename,
    service_uid,
    service_gid,
    source_stat,
):
    entry = None
    try:
        entry = _HeldPrivateEntry.open(
            run_descriptor,
            filename,
            service_uid,
            service_gid,
            "unsafe_sqlite_snapshot",
        )
        return _verified_held_snapshot(entry, source_stat)
    except SQLiteSnapshotError:
        raise
    except OSError:
        raise SQLiteSnapshotError("unsafe_sqlite_snapshot") from None
    finally:
        if entry is not None:
            entry.close()


def _verified_held_snapshot(entry, source_stat):
    connection = None
    try:
        entry.verify_current()
        os.fsync(entry.descriptor)
        entry.verify_current()
        connection_path = _descriptor_file_path(entry.descriptor)
        connection = sqlite3.connect(
            "file:{}?mode=ro&immutable=1".format(quote(
                connection_path,
                safe="/",
            )),
            uri=True,
        )
        result = connection.execute("PRAGMA quick_check").fetchone()
        if result is None or result[0] != "ok":
            raise SQLiteSnapshotError("corrupt_sqlite_snapshot")
        connection.close()
        connection = None
        entry.verify_current()
        digest = _hash_descriptor(entry.descriptor)
        entry.verify_current()
        return SnapshotInfo(
            size=entry.file_stat.st_size,
            sha256=digest,
            source_device=source_stat.st_dev,
            source_inode=source_stat.st_ino,
        )
    except SQLiteSnapshotError:
        raise
    except sqlite3.Error:
        raise SQLiteSnapshotError("corrupt_sqlite_snapshot") from None
    except OSError:
        raise SQLiteSnapshotError(entry.identity_error) from None
    finally:
        if connection is not None:
            connection.close()


def _finish_receipted_snapshot(
    run,
    run_descriptor,
    source,
    snapshot_name,
    service_uid,
    service_gid,
):
    snapshot_entry = None
    receipt_entry = None
    try:
        snapshot_entry = _HeldPrivateEntry.open(
            run_descriptor,
            snapshot_name,
            service_uid,
            service_gid,
            "unsafe_sqlite_snapshot",
        )
        receipt_entry = _HeldPrivateEntry.open(
            run_descriptor,
            SNAPSHOT_RECEIPT_FILENAME,
            service_uid,
            service_gid,
            "sqlite_snapshot_conflict",
        )
        _verify_held_snapshot_pair(
            snapshot_entry,
            receipt_entry,
            source.file_stat,
        )
        source.verify_current()
        if snapshot_name == SNAPSHOT_TEMP_FILENAME:
            _promote_snapshot(run_descriptor)
            snapshot_entry.rebind_after_rename(SNAPSHOT_FILENAME)
        _verify_held_snapshot_pair(
            snapshot_entry,
            receipt_entry,
            source.file_stat,
        )
        _verify_named_run_identity(run, run_descriptor)
        source.verify_current()
        return _verify_held_snapshot_pair(
            snapshot_entry,
            receipt_entry,
            source.file_stat,
        )
    finally:
        if receipt_entry is not None:
            receipt_entry.close()
        if snapshot_entry is not None:
            snapshot_entry.close()


def _verify_held_snapshot_pair(snapshot_entry, receipt_entry, source_stat):
    receipt = _read_held_snapshot_receipt(receipt_entry)
    info = _verified_held_snapshot(snapshot_entry, source_stat)
    _verify_snapshot_receipt(receipt, info)
    receipt_entry.verify_current()
    snapshot_entry.verify_current()
    return info


def _create_snapshot_receipt(
    run_descriptor,
    info,
    service_uid,
    service_gid,
):
    receipt = _receipt_for_snapshot(info)
    return _create_private_receipt(
        run_descriptor,
        SNAPSHOT_RECEIPT_FILENAME,
        receipt,
        service_uid,
        service_gid,
    )


def _create_private_receipt(
    run_descriptor,
    receipt_filename,
    receipt,
    service_uid,
    service_gid,
):
    content = (
        json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    descriptor = None
    try:
        descriptor = os.open(
            receipt_filename,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
            0o600,
            dir_fd=run_descriptor,
        )
        os.fchmod(descriptor, 0o600)
        os.fchown(descriptor, service_uid, service_gid)
        _write_all(descriptor, content)
        os.fsync(descriptor)
        os.fsync(run_descriptor)
        return receipt
    except (MigrationStateError, OSError) as error:
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
            try:
                os.unlink(
                    receipt_filename,
                    dir_fd=run_descriptor,
                )
                os.fsync(run_descriptor)
            except OSError:
                pass
        if isinstance(error, OSError) and error.errno == errno.EEXIST:
            raise SQLiteSnapshotError("sqlite_snapshot_conflict") from None
        raise SQLiteSnapshotError("sqlite_snapshot_failed") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_snapshot_receipt(
    run_descriptor,
    service_uid,
    service_gid,
    filename=SNAPSHOT_RECEIPT_FILENAME,
    receipt_kind=None,
    run_id=None,
):
    entry = None
    try:
        entry = _HeldPrivateEntry.open(
            run_descriptor,
            filename,
            service_uid,
            service_gid,
            "sqlite_snapshot_conflict",
        )
        return _read_held_snapshot_receipt(
            entry,
            receipt_kind=receipt_kind,
            run_id=run_id,
        )
    except SQLiteSnapshotError:
        raise
    finally:
        if entry is not None:
            entry.close()


def _read_held_snapshot_receipt(
    entry,
    receipt_kind=None,
    run_id=None,
):
    try:
        entry.verify_current()
        os.lseek(entry.descriptor, 0, os.SEEK_SET)
        raw_receipt = _read_descriptor(entry.descriptor)
        entry.verify_current()
        receipt = json.loads(raw_receipt.decode("utf-8"))
        entry.verify_current()
    except SQLiteSnapshotError:
        raise
    except (
        MigrationStateError,
        OSError,
        TypeError,
        UnicodeDecodeError,
        ValueError,
    ):
        raise SQLiteSnapshotError("sqlite_snapshot_conflict") from None
    expected_keys = (
        _CLEANUP_RECEIPT_KEYS
        if receipt_kind is not None
        else _SNAPSHOT_RECEIPT_KEYS
    )
    if (
        not isinstance(receipt, dict)
        or set(receipt) != expected_keys
        or not _is_identity_number(receipt["size"])
        or not _is_sha256(receipt["sha256"])
        or not _is_identity_number(receipt["source_device"])
        or not _is_identity_number(receipt["source_inode"])
    ):
        raise SQLiteSnapshotError("sqlite_snapshot_conflict")
    if receipt_kind is not None and (
        receipt["format_version"] != 1
        or receipt["kind"] != receipt_kind
        or receipt["run_id"] != run_id
    ):
        raise SQLiteSnapshotError("sqlite_snapshot_conflict")
    return receipt


def _verify_snapshot_receipt(
    receipt,
    info,
    receipt_kind=None,
    run_id=None,
):
    expected = _receipt_for_snapshot(info)
    if receipt_kind is not None:
        expected.update({
            "format_version": 1,
            "kind": receipt_kind,
            "run_id": run_id,
        })
    if receipt != expected:
        raise SQLiteSnapshotError("sqlite_snapshot_conflict")


def _receipt_for_snapshot(info):
    return {
        "size": info.size,
        "sha256": info.sha256,
        "source_device": info.source_device,
        "source_inode": info.source_inode,
    }


def _open_private_entry(
    run_descriptor,
    filename,
    service_uid,
    service_gid,
    invalid_code,
):
    descriptor = None
    try:
        descriptor = os.open(
            filename,
            os.O_RDONLY | _NOFOLLOW,
            dir_fd=run_descriptor,
        )
        if not _is_private_regular_file(
            os.fstat(descriptor),
            service_uid,
            service_gid,
        ):
            raise OSError(errno.EPERM, "invalid private snapshot entry")
        return descriptor
    except OSError:
        if descriptor is not None:
            os.close(descriptor)
        raise SQLiteSnapshotError(invalid_code) from None


def _remove_private_entry(
    run_descriptor,
    filename,
    service_uid,
    service_gid,
    invalid_code,
):
    descriptor = _open_private_entry(
        run_descriptor,
        filename,
        service_uid,
        service_gid,
        invalid_code,
    )
    try:
        entry_stat = os.fstat(descriptor)
        named_stat = os.stat(
            filename,
            dir_fd=run_descriptor,
            follow_symlinks=False,
        )
        if (
            named_stat.st_dev != entry_stat.st_dev
            or named_stat.st_ino != entry_stat.st_ino
        ):
            raise SQLiteSnapshotError("sqlite_snapshot_conflict")
        os.unlink(filename, dir_fd=run_descriptor)
        os.fsync(run_descriptor)
    except SQLiteSnapshotError:
        raise
    except OSError:
        raise SQLiteSnapshotError("sqlite_snapshot_failed") from None
    finally:
        os.close(descriptor)


def _is_private_regular_file(file_stat, service_uid, service_gid):
    return (
        stat.S_ISREG(file_stat.st_mode)
        and file_stat.st_nlink == 1
        and stat.S_IMODE(file_stat.st_mode) == 0o600
        and file_stat.st_uid == service_uid
        and file_stat.st_gid == service_gid
    )


def _promote_snapshot(
    run_descriptor,
    temp_filename=SNAPSHOT_TEMP_FILENAME,
    snapshot_filename=SNAPSHOT_FILENAME,
):
    try:
        if _entry_exists(run_descriptor, snapshot_filename):
            raise SQLiteSnapshotError("sqlite_snapshot_conflict")
        os.rename(
            temp_filename,
            snapshot_filename,
            src_dir_fd=run_descriptor,
            dst_dir_fd=run_descriptor,
        )
        os.fsync(run_descriptor)
    except SQLiteSnapshotError:
        raise
    except OSError:
        raise SQLiteSnapshotError("sqlite_snapshot_failed") from None


def _verify_snapshot_intent_source(state, source_stat):
    intent = state.get("intent")
    if not isinstance(intent, dict):
        raise SQLiteSnapshotError("sqlite_snapshot_state_invalid")
    if intent.get("kind") != "sqlite_snapshot":
        raise SQLiteSnapshotError("sqlite_snapshot_state_invalid")
    has_device = "source_device" in intent
    has_inode = "source_inode" in intent
    if not has_device or not has_inode:
        raise SQLiteSnapshotError("sqlite_snapshot_state_invalid")
    if has_device and (
        intent["source_device"] != source_stat.st_dev
        or intent["source_inode"] != source_stat.st_ino
    ):
        raise SQLiteSnapshotError("sqlite_source_identity_changed")


def _verify_named_run_identity(run, run_descriptor):
    root_descriptor = None
    named_descriptor = None
    try:
        root_descriptor = _open_configured_backup_root(
            run.backup_root,
        )
        root_stat = os.fstat(root_descriptor)
        if (
            root_stat.st_dev != run.backup_root_device
            or root_stat.st_ino != run.backup_root_inode
        ):
            raise SQLiteSnapshotError("sqlite_snapshot_state_invalid")
        named_descriptor = os.open(
            run.run_id,
            _DIRECTORY_FLAGS | _NOFOLLOW,
            dir_fd=root_descriptor,
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
        if root_descriptor is not None:
            os.close(root_descriptor)


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


def _descriptor_file_path(descriptor):
    try:
        descriptor_stat = os.fstat(descriptor)
    except OSError:
        raise SQLiteSnapshotError("sqlite_snapshot_conflict") from None
    for directory in ("/proc/self/fd", "/dev/fd"):
        candidate = os.path.join(directory, str(descriptor))
        candidate_descriptor = None
        try:
            candidate_descriptor = os.open(
                candidate,
                os.O_RDONLY,
            )
            candidate_stat = os.fstat(candidate_descriptor)
        except OSError:
            continue
        finally:
            if candidate_descriptor is not None:
                os.close(candidate_descriptor)
        if (
            candidate_stat.st_dev == descriptor_stat.st_dev
            and candidate_stat.st_ino == descriptor_stat.st_ino
        ):
            return candidate
    raise SQLiteSnapshotError("sqlite_snapshot_state_invalid")


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
