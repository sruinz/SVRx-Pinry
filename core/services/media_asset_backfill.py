from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
import re
import stat
import uuid
import warnings

from django.conf import settings
from django.core.management.base import CommandError
from django.db import (
    IntegrityError,
    OperationalError,
    connection,
    transaction,
)
from PIL import Image as PILImage

from core.models import MediaAsset, Pin
from core.services.media_storage import MediaStorage, MediaStorageError
from core.services.safe_url_fetch import FetchedImage
from django_images.file_ops import (
    MediaPathError,
    media_global_writer_gate,
    open_verified_media_file,
    open_verified_media_root,
    sha256_file_descriptor,
)
from django_images.models import Image, Thumbnail
from django_images.paths import (
    FORMAT_EXTENSIONS,
    canonical_derivative_path,
    canonical_original_path,
    sanitize_original_filename,
)


TARGET_SIGNATURE = "media-asset-backfill-v2"
MANIFEST_FILENAME = "media-asset-backfill.jsonl"
_KINDS = ("original", "thumbnail", "standard", "square")
_DERIVATIVE_KINDS = _KINDS[1:]
_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_PRELIMINARY_REASONS = {
    "extra_derivative",
    "file_identity_mismatch",
    "invalid_dimensions",
    "invalid_media_file",
    "invalid_named_leaf",
    "missing_derivative",
    "multi_owner",
    "orphan",
    "pipeline_closure_mismatch",
    "processing_pixel_limit_exceeded",
    "unsafe_media_file",
}
SAFE_BACKFILL_REASON_CODES = frozenset(
    _PRELIMINARY_REASONS
    | {
        "existing_registry_collision",
        "duplicate_registry_collision",
    }
)
_RESULT_EVENTS = {
    "registered",
    "skipped",
    "already_registered",
    "recovered_registered",
}
_SQLITE_BUSY_WORD = re.compile(r"\b(?:busy|locked)\b")
_POSTGRESQL_BUSY_SQLSTATES = frozenset(("40001", "40P01", "55P03"))


def _command_error(code, cause=None, retryable=None):
    del cause
    error = CommandError(code)
    if retryable is not None:
        error.code = code
        error.retryable = retryable
    error.__suppress_context__ = True
    return error


def _identity(file_stat):
    return file_stat.st_dev, file_stat.st_ino


def _processing_pixel_limit():
    pillow_limit = PILImage.MAX_IMAGE_PIXELS
    if pillow_limit is None:
        return settings.PINRY_FETCH_MAX_PIXELS
    return min(settings.PINRY_FETCH_MAX_PIXELS, pillow_limit)


def _valid_digest(value):
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _tupleize(value):
    if isinstance(value, list):
        return tuple(_tupleize(entry) for entry in value)
    return value


def _database_error_is_busy(error, vendor):
    if (
        vendor == "sqlite"
        and isinstance(error, OperationalError)
        and _SQLITE_BUSY_WORD.search(str(error).lower())
    ):
        return True
    if vendor != "postgresql":
        return False
    candidates = (
        error,
        getattr(error, "__cause__", None),
        getattr(error, "__context__", None),
    )
    for candidate in candidates:
        sqlstate = (
            getattr(candidate, "pgcode", None)
            or getattr(candidate, "sqlstate", None)
        )
        if sqlstate in _POSTGRESQL_BUSY_SQLSTATES:
            return True
    return False


def _restore_sqlite_busy_timeout(database_connection, busy_timeout):
    with database_connection.cursor() as cursor:
        cursor.execute(
            "PRAGMA busy_timeout = {}".format(busy_timeout)
        )


def _close_database_connection_safely(database_connection):
    try:
        database_connection.close()
    except BaseException:
        pass


@contextmanager
def _database_write_fence_window(database_connection):
    if database_connection.vendor != "sqlite":
        try:
            yield
        except CommandError:
            raise
        except Exception as error:
            if _database_error_is_busy(
                error,
                database_connection.vendor,
            ):
                raise _command_error(
                    "database_busy",
                    error,
                    retryable=True,
                )
            raise
        return

    try:
        with database_connection.cursor() as cursor:
            cursor.execute("PRAGMA busy_timeout")
            row = cursor.fetchone()
        if (
            not isinstance(row, (tuple, list))
            or len(row) != 1
            or type(row[0]) is not int
            or row[0] < 0
        ):
            raise ValueError("invalid_sqlite_busy_timeout")
        busy_timeout = row[0]
        with database_connection.cursor() as cursor:
            cursor.execute("PRAGMA busy_timeout = 0")
    except BaseException as error:
        _close_database_connection_safely(database_connection)
        if isinstance(error, Exception):
            raise _command_error(
                "registry_plan_identity_changed",
                error,
            )
        raise

    try:
        yield
    except BaseException as error:
        try:
            _restore_sqlite_busy_timeout(
                database_connection,
                busy_timeout,
            )
        except BaseException:
            _close_database_connection_safely(database_connection)
        if (
            not isinstance(error, CommandError)
            and isinstance(error, Exception)
            and _database_error_is_busy(error, "sqlite")
        ):
            raise _command_error(
                "database_busy",
                error,
                retryable=True,
            )
        raise
    else:
        try:
            _restore_sqlite_busy_timeout(
                database_connection,
                busy_timeout,
            )
        except BaseException as error:
            _close_database_connection_safely(database_connection)
            if isinstance(error, Exception):
                raise _command_error(
                    "registry_plan_identity_changed",
                    error,
                )
            raise


def _acquire_database_write_fence(database_connection):
    table_models = (MediaAsset, Pin, Image, Thumbnail)
    try:
        with database_connection.cursor() as cursor:
            if database_connection.vendor == "sqlite":
                table_name = database_connection.ops.quote_name(
                    MediaAsset._meta.db_table
                )
                primary_key = database_connection.ops.quote_name(
                    MediaAsset._meta.pk.column
                )
                cursor.execute(
                    "UPDATE {table} SET {pk} = {pk} WHERE 0 = 1".format(
                        table=table_name,
                        pk=primary_key,
                    )
                )
                return
            if database_connection.vendor == "postgresql":
                for model in table_models:
                    table_name = database_connection.ops.quote_name(
                        model._meta.db_table
                    )
                    cursor.execute(
                        "LOCK TABLE {} IN EXCLUSIVE MODE NOWAIT".format(
                            table_name
                        )
                    )
                return
    except CommandError:
        raise
    except Exception as error:
        if _database_error_is_busy(error, database_connection.vendor):
            raise _command_error(
                "database_busy",
                error,
                retryable=True,
            )
        raise _command_error(
            "registry_plan_identity_changed",
            error,
        )
    raise _command_error("unsupported_media_asset_backfill_database")


@dataclass(frozen=True)
class VerifiedClosure(object):
    image_id: int
    submitter_id: int
    content_sha256: str
    database_signature: tuple
    file_identities: tuple


@dataclass(frozen=True)
class BackfillSummary(object):
    run_id: str
    plan_sha256: str
    manifest_sha256: str
    scanned: int
    eligible: int
    registered: int
    already_registered: int
    skipped: int
    reason_counts: dict


@dataclass(frozen=True)
class _CandidatePlan(object):
    image_id: int
    submitter_id: object
    content_sha256: object
    database_signature: tuple
    file_identities: tuple
    owner_ids: tuple
    registry_signature: object
    key_registry_signature: object
    preliminary_reason: object

    def as_dict(self):
        return {
            "image_id": self.image_id,
            "submitter_id": self.submitter_id,
            "content_sha256": self.content_sha256,
            "database_signature": self.database_signature,
            "file_identities": self.file_identities,
            "owner_ids": self.owner_ids,
            "registry_signature": self.registry_signature,
            "key_registry_signature": self.key_registry_signature,
            "preliminary_reason": self.preliminary_reason,
        }

    @classmethod
    def from_dict(cls, value):
        keys = {
            "image_id",
            "submitter_id",
            "content_sha256",
            "database_signature",
            "file_identities",
            "owner_ids",
            "registry_signature",
            "key_registry_signature",
            "preliminary_reason",
        }
        if not isinstance(value, dict) or set(value) != keys:
            raise _command_error("invalid_media_asset_manifest")
        plan = cls(
            image_id=value["image_id"],
            submitter_id=value["submitter_id"],
            content_sha256=value["content_sha256"],
            database_signature=_tupleize(value["database_signature"]),
            file_identities=_tupleize(value["file_identities"]),
            owner_ids=_tupleize(value["owner_ids"]),
            registry_signature=_tupleize(value["registry_signature"]),
            key_registry_signature=_tupleize(
                value["key_registry_signature"]
            ),
            preliminary_reason=value["preliminary_reason"],
        )
        plan._validate()
        return plan

    def _validate(self):
        if type(self.image_id) is not int or self.image_id <= 0:
            raise _command_error("invalid_media_asset_manifest")
        if (
            not isinstance(self.database_signature, tuple)
            or len(self.database_signature) != 7
            or self.database_signature[0] != self.image_id
        ):
            raise _command_error("invalid_media_asset_manifest")
        if (
            not isinstance(self.owner_ids, tuple)
            or len(self.owner_ids) > 2
            or any(type(owner_id) is not int for owner_id in self.owner_ids)
        ):
            raise _command_error("invalid_media_asset_manifest")
        for signature in (
            self.registry_signature,
            self.key_registry_signature,
        ):
            if signature is not None and (
                not isinstance(signature, tuple)
                or len(signature) != 4
                or any(type(entry) is not int for entry in signature[:3])
                or not _valid_digest(signature[3])
            ):
                raise _command_error("invalid_media_asset_manifest")
        if self.preliminary_reason is None:
            if (
                type(self.submitter_id) is not int
                or self.submitter_id <= 0
                or self.owner_ids != (self.submitter_id,)
                or not _valid_digest(self.content_sha256)
                or not isinstance(self.file_identities, tuple)
                or len(self.file_identities) != 4
            ):
                raise _command_error("invalid_media_asset_manifest")
        elif (
            self.preliminary_reason not in _PRELIMINARY_REASONS
            or self.submitter_id is not None
            or self.content_sha256 is not None
        ):
            raise _command_error("invalid_media_asset_manifest")


class _CandidateResources(object):
    def __init__(
        self,
        storage,
        image,
        thumbnails,
        root_directory,
        receipts,
        prepared,
        reusable,
        closure,
    ):
        self.storage = storage
        self.image = image
        self.thumbnails = tuple(thumbnails)
        self.root_directory = root_directory
        self.receipts = list(receipts)
        self.prepared = prepared
        self.reusable = reusable
        self.closure = closure
        self._closed = False

    def verify_current(self, refresh_prepared=False):
        if self._closed:
            raise _command_error("registry_plan_identity_changed")
        try:
            self.root_directory.verify_current()
            for receipt in self.receipts:
                receipt.verify_current()
            if refresh_prepared:
                replacement = self.storage.verify_reusable(
                    self.prepared,
                    self.image,
                    self.thumbnails,
                )
                previous = self.reusable
                self.reusable = replacement
                previous.release()
            self.reusable.verify_current()
            for receipt in self.receipts:
                receipt.verify_current()
            self.root_directory.verify_current()
        except CommandError:
            raise
        except (MediaPathError, MediaStorageError, OSError) as error:
            raise _command_error(
                "registry_plan_identity_changed",
                error,
            )
        return True

    def close(self):
        if self._closed:
            return
        first_error = None
        try:
            self.reusable.release()
        except BaseException as error:
            first_error = error
        try:
            self.prepared.cleanup()
        except BaseException as error:
            if first_error is None:
                first_error = error
        for receipt in reversed(self.receipts):
            try:
                receipt.close()
            except BaseException as error:
                if first_error is None:
                    first_error = error
        self._closed = (
            self.reusable._released
            and not self.prepared.is_open
            and all(receipt._closed for receipt in self.receipts)
        )
        if first_error is not None:
            raise first_error


class _ManifestState(object):
    def __init__(self):
        self.events = []
        self.plans = []
        self.plan_by_image = {}
        self.latest_by_image = {}
        self.plan_complete = False
        self.plan_end_offset = None
        self.torn_tail = None
        self.torn_offset = None
        self.raw_bytes = b""


def _verify_contained_run_directory(data_directory, run_directory):
    try:
        data_directory.verify_current()
        run_directory.verify_current()
        data_names = tuple(data_directory.names)
        run_names = tuple(run_directory.names)
        if (
            len(run_names) <= len(data_names)
            or run_names[:len(data_names)] != data_names
        ):
            raise _command_error("unsafe_media_asset_manifest")
        data_stat = os.fstat(data_directory.descriptor)
        contained_stat = os.fstat(
            run_directory.descriptors[len(data_names)]
        )
    except (IndexError, OSError, MediaPathError) as error:
        raise _command_error("unsafe_media_asset_manifest", error)
    if _identity(data_stat) != _identity(contained_stat):
        raise _command_error("unsafe_media_asset_manifest")
    return True


def _json_line(event):
    return (
        json.dumps(
            event,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ) + "\n"
    ).encode("utf-8")


class _BackfillManifestLog(object):
    def __init__(
        self,
        data_directory,
        run_directory,
        filename,
        run_id,
        service_uid,
        service_gid,
        descriptor,
        file_stat,
    ):
        self.data_directory = data_directory
        self.run_directory = run_directory
        self.filename = filename
        self.run_id = run_id
        self.service_uid = service_uid
        self.service_gid = service_gid
        self.descriptor = descriptor
        self.file_stat = file_stat
        self._closed = False
        self.state = self._load_state()

    @classmethod
    def open(  # noqa: C901
        cls,
        run_directory,
        filename,
        run_id,
        service_uid,
        service_gid,
        create=True,
    ):
        if (
            type(run_id) is not str
            or _RUN_ID_PATTERN.match(run_id) is None
        ):
            raise _command_error("invalid_media_asset_run_id")
        if filename != MANIFEST_FILENAME:
            raise _command_error("unsafe_media_asset_manifest")
        if (
            type(service_uid) is not int
            or service_uid < 0
            or type(service_gid) is not int
            or service_gid < 0
            or type(create) is not bool
        ):
            raise _command_error("unsafe_media_asset_manifest")
        if os.path.basename(run_directory) != run_id:
            raise _command_error("manifest_run_id_mismatch")
        data_handle = None
        run_handle = None
        descriptor = None
        try:
            data_handle = open_verified_media_root(
                settings.PINRY_DATA_ROOT
            )
            run_handle = open_verified_media_root(run_directory)
            _verify_contained_run_directory(data_handle, run_handle)
            run_stat = os.fstat(run_handle.descriptor)
            if (
                run_stat.st_uid != service_uid
                or run_stat.st_gid != service_gid
                or stat.S_IMODE(run_stat.st_mode) != 0o700
            ):
                raise _command_error("unsafe_media_asset_manifest")
            flags = os.O_RDWR | os.O_NOFOLLOW
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            try:
                descriptor = os.open(
                    filename,
                    flags,
                    dir_fd=run_handle.descriptor,
                )
            except FileNotFoundError:
                if not create:
                    raise
                descriptor = os.open(
                    filename,
                    flags | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=run_handle.descriptor,
                )
                os.fchmod(descriptor, 0o600)
                os.fchown(descriptor, service_uid, service_gid)
                os.fsync(descriptor)
                os.fsync(run_handle.descriptor)
            file_stat = os.fstat(descriptor)
            named_stat = os.stat(
                filename,
                dir_fd=run_handle.descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(file_stat.st_mode)
                or not stat.S_ISREG(named_stat.st_mode)
                or file_stat.st_nlink != 1
                or named_stat.st_nlink != 1
                or file_stat.st_uid != service_uid
                or file_stat.st_gid != service_gid
                or stat.S_IMODE(file_stat.st_mode) != 0o600
                or _identity(file_stat) != _identity(named_stat)
            ):
                raise _command_error("unsafe_media_asset_manifest")
            opened = cls(
                data_handle,
                run_handle,
                filename,
                run_id,
                service_uid,
                service_gid,
                descriptor,
                file_stat,
            )
            data_handle = None
            run_handle = None
            descriptor = None
            return opened
        except BaseException as error:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except BaseException:
                    pass
            if run_handle is not None:
                try:
                    run_handle.close()
                except BaseException:
                    pass
            if data_handle is not None:
                try:
                    data_handle.close()
                except BaseException:
                    pass
            if isinstance(error, CommandError):
                raise
            if not isinstance(error, Exception):
                raise
            raise _command_error("unsafe_media_asset_manifest", error)

    def __enter__(self):
        return self

    def __exit__(self, error_type, error, traceback):
        del error, traceback
        try:
            self.close()
        except BaseException:
            if error_type is None:
                raise
        return False

    def close(self):
        if self._closed:
            return
        self._closed = True
        first_error = None
        descriptor = self.descriptor
        self.descriptor = None
        try:
            os.close(descriptor)
        except BaseException as error:
            first_error = error
        try:
            self.run_directory.close()
        except BaseException as error:
            if first_error is None:
                first_error = error
        try:
            self.data_directory.close()
        except BaseException as error:
            if first_error is None:
                first_error = error
        if first_error is not None:
            raise first_error

    def _verify_current(self):
        try:
            _verify_contained_run_directory(
                self.data_directory,
                self.run_directory,
            )
            run_stat = os.fstat(self.run_directory.descriptor)
            descriptor_stat = os.fstat(self.descriptor)
            named_stat = os.stat(
                self.filename,
                dir_fd=self.run_directory.descriptor,
                follow_symlinks=False,
            )
        except (OSError, MediaPathError) as error:
            raise _command_error("unsafe_media_asset_manifest", error)
        if (
            not stat.S_ISDIR(run_stat.st_mode)
            or run_stat.st_uid != self.service_uid
            or run_stat.st_gid != self.service_gid
            or stat.S_IMODE(run_stat.st_mode) != 0o700
            or not stat.S_ISREG(descriptor_stat.st_mode)
            or not stat.S_ISREG(named_stat.st_mode)
            or descriptor_stat.st_nlink != 1
            or named_stat.st_nlink != 1
            or descriptor_stat.st_uid != self.service_uid
            or descriptor_stat.st_gid != self.service_gid
            or stat.S_IMODE(descriptor_stat.st_mode) != 0o600
            or _identity(descriptor_stat) != _identity(self.file_stat)
            or _identity(named_stat) != _identity(self.file_stat)
        ):
            raise _command_error("unsafe_media_asset_manifest")
        return True

    def _read_all(self):
        self._verify_current()
        chunks = []
        position = 0
        while True:
            chunk = os.pread(self.descriptor, 1024 * 1024, position)
            if not chunk:
                break
            chunks.append(chunk)
            position += len(chunk)
        self._verify_current()
        return b"".join(chunks)

    def _load_state(self):
        raw = self._read_all()
        state = _ManifestState()
        state.raw_bytes = raw
        offset = 0
        lines = raw.splitlines(True)
        for line_number, line in enumerate(lines, 1):
            if not line.endswith(b"\n"):
                if line_number != len(lines):
                    raise _command_error("invalid_media_asset_manifest")
                state.torn_tail = line
                state.torn_offset = offset
                break
            try:
                event = json.loads(line.decode("utf-8"))
            except (TypeError, ValueError, UnicodeDecodeError) as error:
                raise _command_error("invalid_media_asset_manifest", error)
            self._validate_event(event)
            event_name = event["event"]
            if event_name == "planned":
                if state.plan_complete:
                    raise _command_error("invalid_media_asset_manifest")
                plan = _CandidatePlan.from_dict(event.get("plan"))
                if plan.image_id in state.plan_by_image:
                    raise _command_error("manifest_plan_mismatch")
                state.plans.append(plan)
                state.plan_by_image[plan.image_id] = plan
            elif event_name == "plan_complete":
                if state.plan_complete:
                    raise _command_error("invalid_media_asset_manifest")
                if event.get("scanned") != len(state.plans):
                    raise _command_error("manifest_plan_mismatch")
                state.plan_complete = True
                state.plan_end_offset = offset + len(line)
            else:
                if not state.plan_complete:
                    raise _command_error("invalid_media_asset_manifest")
                image_id = event.get("image_id")
                if image_id not in state.plan_by_image:
                    raise _command_error("manifest_plan_mismatch")
                expected_plan_sha = hashlib.sha256(
                    raw[:state.plan_end_offset]
                ).hexdigest()
                if event.get("plan_sha256") != expected_plan_sha:
                    raise _command_error("manifest_plan_mismatch")
                if image_id in state.latest_by_image:
                    raise _command_error("invalid_media_asset_manifest")
                if event_name == "skipped" and (
                    type(event.get("reason_code")) is not str
                    or not event["reason_code"]
                ):
                    raise _command_error("invalid_media_asset_manifest")
                state.latest_by_image[image_id] = event
            state.events.append(event)
            offset += len(line)
        return state

    def _validate_event(self, event):
        if not isinstance(event, dict) or event.get("format_version") != 2:
            raise _command_error("invalid_media_asset_manifest")
        if event.get("target_signature") != TARGET_SIGNATURE:
            raise _command_error("manifest_target_mismatch")
        if event.get("run_id") != self.run_id:
            raise _command_error("manifest_run_id_mismatch")
        if event.get("event") not in (
            "planned",
            "plan_complete",
            "registered",
            "skipped",
            "already_registered",
            "recovered_registered",
        ):
            raise _command_error("invalid_media_asset_manifest")

    def append(self, event):
        if self.state.torn_tail is not None:
            raise _command_error(
                "media_asset_manifest_torn_tail_requires_execute"
            )
        self._ensure_content_current()
        event = dict(event)
        event.update({
            "format_version": 2,
            "target_signature": TARGET_SIGNATURE,
            "run_id": self.run_id,
        })
        line = _json_line(event)
        self._verify_current()
        os.lseek(self.descriptor, 0, os.SEEK_END)
        view = memoryview(line)
        while view:
            written = os.write(self.descriptor, view)
            if written <= 0:
                raise _command_error("unsafe_media_asset_manifest")
            view = view[written:]
        os.fsync(self.descriptor)
        self._verify_current()
        self.state = self._load_state()

    def record_plan(self, plan):
        self.append({"event": "planned", "plan": plan.as_dict()})

    def record_plan_complete(self, plans):
        self.append({"event": "plan_complete", "scanned": len(plans)})

    def record_result(
        self,
        event_name,
        image_id,
        reason_code=None,
        before_append=None,
    ):
        if event_name not in _RESULT_EVENTS:
            raise _command_error("invalid_media_asset_manifest")
        event = {
            "event": event_name,
            "image_id": image_id,
            "plan_sha256": self.plan_sha256(),
        }
        if event_name == "skipped":
            if type(reason_code) is not str or not reason_code:
                raise _command_error("invalid_media_asset_manifest")
            event["reason_code"] = reason_code
        elif reason_code is not None:
            raise _command_error("invalid_media_asset_manifest")
        if before_append is not None:
            before_append()
        self.append(event)

    def repair_torn_tail(self):
        if self.state.torn_tail is None:
            return None
        self._ensure_content_current()
        quarantine_name = "{}.torn-{}".format(
            self.filename,
            uuid.uuid4(),
        )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        descriptor = None
        try:
            descriptor = os.open(
                quarantine_name,
                flags,
                0o600,
                dir_fd=self.run_directory.descriptor,
            )
            os.fchmod(descriptor, 0o600)
            os.fchown(descriptor, self.service_uid, self.service_gid)
            view = memoryview(self.state.torn_tail)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise _command_error("unsafe_media_asset_manifest")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            if descriptor is not None:
                os.close(descriptor)
        self._ensure_content_current()
        os.ftruncate(self.descriptor, self.state.torn_offset)
        os.fsync(self.descriptor)
        os.fsync(self.run_directory.descriptor)
        self.state = self._load_state()
        return quarantine_name

    def _reset_incomplete_plan(self):
        if self.state.plan_complete:
            raise _command_error("media_asset_plan_reset_forbidden")
        changed = bool(self.state.events or self.state.torn_tail is not None)
        if not changed:
            return False
        if self.state.torn_tail is not None:
            self.repair_torn_tail()
        self._ensure_content_current()
        if self.state.plan_complete or any(
            event["event"] != "planned" for event in self.state.events
        ):
            raise _command_error("media_asset_plan_reset_forbidden")
        self._verify_current()
        os.ftruncate(self.descriptor, 0)
        os.fsync(self.descriptor)
        os.fsync(self.run_directory.descriptor)
        self.state = self._load_state()
        return True

    def plan_sha256(self):
        if not self.state.plan_complete:
            raise _command_error("media_asset_plan_incomplete")
        if self.state.torn_tail is not None:
            raise _command_error(
                "media_asset_manifest_torn_tail_requires_execute"
            )
        return hashlib.sha256(
            self.state.raw_bytes[:self.state.plan_end_offset]
        ).hexdigest()

    def manifest_sha256(self):
        if self.state.torn_tail is not None:
            raise _command_error(
                "media_asset_manifest_torn_tail_requires_execute"
            )
        self._ensure_content_current()
        return hashlib.sha256(self.state.raw_bytes).hexdigest()

    def _ensure_content_current(self):
        if self._read_all() != self.state.raw_bytes:
            raise _command_error("unsafe_media_asset_manifest")
        return True


def recover_incomplete_media_asset_plan(
    run_directory,
    filename,
    run_id,
    service_uid,
    service_gid,
):
    """완료 marker 이전의 계획 prefix만 같은 run에서 재계획하게 한다."""
    with _BackfillManifestLog.open(
        run_directory,
        filename,
        run_id,
        service_uid,
        service_gid,
        create=False,
    ) as manifest:
        return manifest._reset_incomplete_plan()


def load_media_asset_plan(
    run_directory,
    filename,
    run_id,
    service_uid,
    service_gid,
):
    """완성되고 fsync된 backfill 계획을 typed 요약으로 읽는다."""
    with _BackfillManifestLog.open(
        run_directory,
        filename,
        run_id,
        service_uid,
        service_gid,
        create=False,
    ) as manifest:
        plans = list(manifest.state.plans)
        manifest.plan_sha256()
        decisions = MediaAssetBackfiller._decisions(plans)
        MediaAssetBackfiller._validate_terminal_events(
            manifest,
            plans,
            decisions,
        )
        return _build_backfill_summary(
            manifest,
            plans,
            decisions,
            run_id,
        )


def load_completed_media_asset_backfill_summary(
    run_directory,
    filename,
    run_id,
    service_uid,
    service_gid,
):
    """execute terminal이 전체 완결된 backfill typed 요약만 읽는다."""
    with _BackfillManifestLog.open(
        run_directory,
        filename,
        run_id,
        service_uid,
        service_gid,
        create=False,
    ) as manifest:
        plans = list(manifest.state.plans)
        manifest.plan_sha256()
        decisions = MediaAssetBackfiller._decisions(plans)
        MediaAssetBackfiller._validate_terminal_events(
            manifest,
            plans,
            decisions,
            require_complete=True,
        )
        return _build_backfill_summary(
            manifest,
            plans,
            decisions,
            run_id,
        )


def _build_backfill_summary(manifest, plans, decisions, run_id):
    reason_counts = {}
    eligible = 0
    already_registered = 0
    skipped = 0
    for plan in plans:
        decision = decisions[plan.image_id]
        if decision == "register":
            eligible += 1
        elif decision == "already_registered":
            already_registered += 1
        else:
            skipped += 1
            reason_counts[decision] = reason_counts.get(decision, 0) + 1
    registered = sum(
        1
        for event in manifest.state.latest_by_image.values()
        if event["event"] in (
            "registered",
            "recovered_registered",
        )
    )
    return BackfillSummary(
        run_id=run_id,
        plan_sha256=manifest.plan_sha256(),
        manifest_sha256=manifest.manifest_sha256(),
        scanned=len(plans),
        eligible=eligible,
        registered=registered,
        already_registered=already_registered,
        skipped=skipped,
        reason_counts=dict(sorted(reason_counts.items())),
    )


class MediaAssetBackfiller(object):
    def __init__(
        self,
        run_directory,
        filename,
        run_id,
        service_uid,
        service_gid,
        batch_size=100,
        media_storage=None,
        fault_injector=None,
    ):
        if type(batch_size) is not int or batch_size <= 0:
            raise _command_error("batch_size_must_be_positive")
        self.run_directory = run_directory
        self.filename = filename
        self.run_id = run_id
        self.service_uid = service_uid
        self.service_gid = service_gid
        self.batch_size = batch_size
        self.media_storage = media_storage or MediaStorage()
        self.fault_injector = fault_injector

    def run(self, execute=False):
        if type(execute) is not bool:
            raise _command_error("invalid_execute_flag")
        with _BackfillManifestLog.open(
            self.run_directory,
            self.filename,
            self.run_id,
            self.service_uid,
            self.service_gid,
        ) as manifest:
            if manifest.state.torn_tail is not None:
                if not execute:
                    raise _command_error(
                        "media_asset_manifest_torn_tail_requires_execute"
                    )
                manifest.repair_torn_tail()
            if not manifest.state.events:
                plans = self._freeze_all_plans("plan")
                for plan in plans:
                    manifest.record_plan(plan)
                manifest.record_plan_complete(plans)
                self._fault("after_plan_complete")
            elif not manifest.state.plan_complete:
                raise _command_error("media_asset_plan_incomplete")
            plans = list(manifest.state.plans)
            decisions = self._decisions(plans)
            self._validate_terminal_events(manifest, plans, decisions)
            if execute:
                self._execute(manifest, plans, decisions)
            return self._summary(manifest, plans, decisions)

    def _freeze_all_plans(self, phase):
        root_directory = None
        try:
            root_directory = open_verified_media_root(settings.MEDIA_ROOT)
            plans = []
            queryset = Image.objects.order_by("pk").iterator(
                chunk_size=self.batch_size
            )
            for image in queryset:
                plans.append(self._freeze_plan(image, root_directory, phase))
                root_directory.verify_current()
            root_directory.verify_current()
            return plans
        except CommandError:
            raise
        except (MediaPathError, OSError) as error:
            raise _command_error(
                "registry_plan_identity_changed",
                error,
            )
        finally:
            if root_directory is not None:
                root_directory.close()

    def _freeze_plan(self, image, root_directory, phase):
        thumbnails = list(
            Thumbnail.objects.filter(original_id=image.pk).order_by(
                "size", "pk"
            )
        )
        database_signature = self._database_signature_for_id(image.pk)
        owner_ids = tuple(
            Pin.objects.filter(image_id=image.pk)
            .order_by("submitter_id")
            .values_list("submitter_id", flat=True)
            .distinct()[:2]
        )
        registry_signature = self._registry_signature_for_image(image.pk)
        structural_reason = self._structural_reason(
            database_signature,
            owner_ids,
        )
        if structural_reason is not None:
            if registry_signature is not None:
                raise _command_error("invalid_existing_registry")
            return _CandidatePlan(
                image_id=image.pk,
                submitter_id=None,
                content_sha256=None,
                database_signature=database_signature,
                file_identities=(),
                owner_ids=owner_ids,
                registry_signature=None,
                key_registry_signature=None,
                preliminary_reason=structural_reason,
            )

        submitter_id = owner_ids[0]
        resources = None
        try:
            resources = self._open_candidate_resources(
                image,
                thumbnails,
                submitter_id,
                root_directory,
                database_signature,
                phase,
            )
            closure = resources.closure
        except _CandidateSkip as skip:
            if registry_signature is not None:
                raise _command_error("invalid_existing_registry")
            return _CandidatePlan(
                image_id=image.pk,
                submitter_id=None,
                content_sha256=None,
                database_signature=database_signature,
                file_identities=skip.file_identities,
                owner_ids=owner_ids,
                registry_signature=None,
                key_registry_signature=None,
                preliminary_reason=skip.reason_code,
            )
        finally:
            if resources is not None:
                self._close_resources(resources)

        key_registry_signature = self._registry_signature_for_key(
            submitter_id,
            closure.content_sha256,
        )
        if registry_signature is not None:
            if (
                registry_signature[1] != image.pk
                or registry_signature[2] != submitter_id
                or registry_signature[3] != closure.content_sha256
                or key_registry_signature != registry_signature
            ):
                raise _command_error("invalid_existing_registry")
        return _CandidatePlan(
            image_id=image.pk,
            submitter_id=submitter_id,
            content_sha256=closure.content_sha256,
            database_signature=closure.database_signature,
            file_identities=closure.file_identities,
            owner_ids=owner_ids,
            registry_signature=registry_signature,
            key_registry_signature=key_registry_signature,
            preliminary_reason=None,
        )

    @staticmethod
    def _database_signature_for_id(image_id):
        try:
            image = Image.objects.filter(pk=image_id).values(
                "pk",
                "asset_uuid",
                "original_filename",
                "image",
                "width",
                "height",
            ).get()
            thumbnails = list(
                Thumbnail.objects.filter(original_id=image_id)
                .order_by("size", "pk")
                .values("pk", "size", "image", "width", "height")
            )
            return (
                image["pk"],
                str(image["asset_uuid"]),
                image["original_filename"],
                image["image"],
                image["width"],
                image["height"],
                tuple(
                    (
                        thumbnail["pk"],
                        thumbnail["size"],
                        thumbnail["image"],
                        thumbnail["width"],
                        thumbnail["height"],
                    )
                    for thumbnail in thumbnails
                ),
            )
        except (AttributeError, Image.DoesNotExist, TypeError, ValueError):
            raise _command_error("registry_plan_identity_changed")

    @staticmethod
    def _structural_reason(database_signature, owner_ids):
        if not owner_ids:
            return "orphan"
        if len(owner_ids) != 1:
            return "multi_owner"
        thumbnail_signatures = database_signature[6]
        sizes = tuple(
            thumbnail_signature[1]
            for thumbnail_signature in thumbnail_signatures
        )
        missing = set(_DERIVATIVE_KINDS) - set(sizes)
        if missing:
            return "missing_derivative"
        if (
            len(thumbnail_signatures) != 3
            or set(sizes) != set(_DERIVATIVE_KINDS)
        ):
            return "extra_derivative"
        dimensions = (
            (database_signature[4], database_signature[5]),
        ) + tuple(
            (thumbnail_signature[3], thumbnail_signature[4])
            for thumbnail_signature in thumbnail_signatures
        )
        if any(
            type(width) is not int
            or type(height) is not int
            or width <= 0
            or height <= 0
            for width, height in dimensions
        ):
            return "invalid_dimensions"
        try:
            asset_uuid = database_signature[1]
            original_filename = database_signature[2]
            original_path = database_signature[3]
            extension = os.path.splitext(original_path)[1]
            if (
                original_filename
                != sanitize_original_filename(original_filename)
                or extension not in FORMAT_EXTENSIONS.values()
                or original_path
                != canonical_original_path(
                    asset_uuid,
                    original_filename,
                    extension,
                )
            ):
                return "invalid_named_leaf"
            for thumbnail_signature in thumbnail_signatures:
                derivative_path = thumbnail_signature[2]
                derivative_extension = os.path.splitext(
                    derivative_path
                )[1]
                if (
                    derivative_extension not in FORMAT_EXTENSIONS.values()
                    or derivative_path
                    != canonical_derivative_path(
                        asset_uuid,
                        thumbnail_signature[1],
                        derivative_extension,
                    )
                ):
                    return "invalid_named_leaf"
        except (AttributeError, TypeError, ValueError):
            return "invalid_named_leaf"
        return None

    def _open_candidate_resources(  # noqa: C901
        self,
        image,
        thumbnails,
        submitter_id,
        root_directory,
        database_signature,
        phase,
    ):
        receipts = []
        prepared = None
        reusable = None
        file_identities = []
        try:
            thumbnail_signature_by_size = {
                entry[1]: entry for entry in database_signature[6]
            }
            rows = [(
                "original",
                database_signature[3],
                database_signature[4],
                database_signature[5],
            )]
            rows.extend(
                (
                    kind,
                    thumbnail_signature_by_size[kind][2],
                    thumbnail_signature_by_size[kind][3],
                    thumbnail_signature_by_size[kind][4],
                )
                for kind in _DERIVATIVE_KINDS
            )
            original_content = None
            original_format = None
            original_dimensions = None
            for kind, relative_path, db_width, db_height in rows:
                try:
                    receipt = open_verified_media_file(
                        root_directory,
                        relative_path,
                    )
                except (MediaPathError, OSError) as error:
                    self._raise_skip_or_identity(
                        root_directory,
                        "unsafe_media_file",
                        file_identities,
                        error,
                    )
                receipts.append(receipt)
                try:
                    image_format, width, height = self._inspect(
                        receipt.descriptor
                    )
                    digest = sha256_file_descriptor(receipt.descriptor)
                    receipt.verify_current()
                    current_stat = os.fstat(receipt.descriptor)
                except (MediaPathError, OSError) as error:
                    self._raise_skip_or_identity(
                        root_directory,
                        "unsafe_media_file",
                        file_identities,
                        error,
                    )
                except Exception as error:
                    raise _CandidateSkip(
                        "invalid_media_file",
                        tuple(file_identities),
                    ) from error
                if (
                    (width, height) != (db_width, db_height)
                    or _identity(current_stat) != _identity(receipt.file_stat)
                    or current_stat.st_size != receipt.file_stat.st_size
                    or current_stat.st_nlink != 1
                ):
                    raise _CandidateSkip(
                        "file_identity_mismatch",
                        tuple(file_identities),
                    )
                extension = FORMAT_EXTENSIONS.get(image_format)
                if extension is None:
                    raise _CandidateSkip(
                        "invalid_media_file",
                        tuple(file_identities),
                    )
                try:
                    expected_path = (
                        canonical_original_path(
                            database_signature[1],
                            database_signature[2],
                            extension,
                        )
                        if kind == "original"
                        else canonical_derivative_path(
                            image.asset_uuid,
                            kind,
                            extension,
                        )
                    )
                except ValueError as error:
                    raise _CandidateSkip(
                        "invalid_named_leaf",
                        tuple(file_identities),
                    ) from error
                if relative_path != expected_path:
                    raise _CandidateSkip(
                        "invalid_named_leaf",
                        tuple(file_identities),
                    )
                file_identities.append((
                    kind,
                    relative_path,
                    current_stat.st_dev,
                    current_stat.st_ino,
                    current_stat.st_size,
                    current_stat.st_nlink,
                    digest,
                    image_format,
                    width,
                    height,
                ))
                if (
                    kind == "original"
                    and width * height > _processing_pixel_limit()
                ):
                    raise _CandidateSkip(
                        "processing_pixel_limit_exceeded",
                        tuple(file_identities),
                    )
                if kind == "original":
                    original_content = self._read_descriptor(
                        receipt.descriptor
                    )
                    receipt.verify_current()
                    original_format = image_format
                    original_dimensions = (width, height)

            self._fault("after_{}_receipts".format(phase))
            for receipt in receipts:
                receipt.verify_current()
            root_directory.verify_current()
            fetched = FetchedImage(
                content=original_content,
                image_format=original_format,
                width=original_dimensions[0],
                height=original_dimensions[1],
                final_url="https://backfill.invalid/verified",
            )
            self._fault("before_{}_prepare".format(phase))
            try:
                prepared = self.media_storage.prepare_from_root(
                    root_directory,
                    fetched,
                    image.asset_uuid,
                    database_signature[2],
                )
            except MediaStorageError as error:
                self._raise_skip_or_identity(
                    root_directory,
                    "pipeline_closure_mismatch",
                    file_identities,
                    error,
                )
            self._fault("after_{}_prepare".format(phase))
            try:
                reusable = self.media_storage.verify_reusable(
                    prepared,
                    image,
                    thumbnails,
                )
                reusable.verify_current()
                for receipt in receipts:
                    receipt.verify_current()
                root_directory.verify_current()
            except MediaStorageError as error:
                self._raise_skip_or_identity(
                    root_directory,
                    "pipeline_closure_mismatch",
                    file_identities,
                    error,
                )
            closure = VerifiedClosure(
                image_id=image.pk,
                submitter_id=submitter_id,
                content_sha256=file_identities[0][6],
                database_signature=database_signature,
                file_identities=tuple(file_identities),
            )
            result = _CandidateResources(
                self.media_storage,
                image,
                thumbnails,
                root_directory,
                receipts,
                prepared,
                reusable,
                closure,
            )
            receipts = []
            prepared = None
            reusable = None
            return result
        except BaseException:
            if reusable is not None:
                try:
                    reusable.release()
                except BaseException:
                    pass
            if prepared is not None:
                try:
                    prepared.cleanup()
                except BaseException:
                    pass
            for receipt in reversed(receipts):
                try:
                    receipt.close()
                except BaseException:
                    pass
            raise

    @staticmethod
    def _raise_skip_or_identity(
        root_directory,
        reason_code,
        file_identities,
        error,
    ):
        try:
            root_directory.verify_current()
        except (MediaPathError, OSError) as identity_error:
            raise _command_error(
                "registry_plan_identity_changed",
                identity_error,
            )
        raise _CandidateSkip(
            reason_code,
            tuple(file_identities),
        ) from error

    @staticmethod
    def _inspect(descriptor):
        with warnings.catch_warnings():
            # 이관된 파일은 헤더만 검증하고 재처리 대상은 별도로 결정한다.
            warnings.simplefilter("ignore", PILImage.DecompressionBombWarning)
            with os.fdopen(os.dup(descriptor), "rb") as file_obj:
                with PILImage.open(file_obj) as image:
                    image_format = image.format
                    width, height = image.size
                    image.verify()
                    return image_format, width, height

    @staticmethod
    def _read_descriptor(descriptor):
        chunks = []
        offset = 0
        while True:
            chunk = os.pread(descriptor, 1024 * 1024, offset)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            offset += len(chunk)

    @staticmethod
    def _registry_signature(registry):
        if registry is None:
            return None
        return (
            registry["pk"],
            registry["image_id"],
            registry["submitter_id"],
            registry["content_sha256"],
        )

    def _registry_signature_for_image(self, image_id):
        registry = (
            MediaAsset.objects.filter(image_id=image_id)
            .values("pk", "image_id", "submitter_id", "content_sha256")
            .first()
        )
        return self._registry_signature(registry)

    def _registry_signature_for_key(self, submitter_id, content_sha256):
        registry = (
            MediaAsset.objects.filter(
                submitter_id=submitter_id,
                content_sha256=content_sha256,
            )
            .values("pk", "image_id", "submitter_id", "content_sha256")
            .first()
        )
        return self._registry_signature(registry)

    @staticmethod
    def _decisions(plans):
        decisions = {}
        groups = {}
        for plan in plans:
            if plan.preliminary_reason is not None:
                decisions[plan.image_id] = plan.preliminary_reason
                continue
            if plan.registry_signature is not None:
                decisions[plan.image_id] = "already_registered"
                continue
            if plan.key_registry_signature is not None:
                decisions[plan.image_id] = "existing_registry_collision"
                continue
            key = plan.submitter_id, plan.content_sha256
            groups.setdefault(key, []).append(plan)
        for group in groups.values():
            decision = (
                "register"
                if len(group) == 1
                else "duplicate_registry_collision"
            )
            for plan in group:
                decisions[plan.image_id] = decision
        return decisions

    @staticmethod
    def _validate_terminal_events(
        manifest,
        plans,
        decisions,
        require_complete=False,
    ):
        for plan in plans:
            terminal = manifest.state.latest_by_image.get(plan.image_id)
            if terminal is None:
                if require_complete:
                    raise _command_error("media_asset_plan_incomplete")
                continue
            decision = decisions[plan.image_id]
            event_name = terminal["event"]
            if decision == "register":
                valid = event_name in (
                    "registered",
                    "recovered_registered",
                )
            elif decision == "already_registered":
                valid = event_name == "already_registered"
            else:
                valid = (
                    event_name == "skipped"
                    and terminal.get("reason_code") == decision
                )
            if not valid:
                raise _command_error("manifest_plan_mismatch")

    def _execute(self, manifest, plans, decisions):
        plan_sha256 = manifest.plan_sha256()
        root_directory = None
        try:
            root_directory = open_verified_media_root(settings.MEDIA_ROOT)
            self._verify_database_plan_closure(plans)
            for plan in plans:
                image = Image.objects.filter(pk=plan.image_id).first()
                if image is None:
                    raise _command_error("registry_plan_identity_changed")
                current = self._freeze_plan(
                    image,
                    root_directory,
                    "execute_verify",
                )
                self._verify_plan_equivalence(
                    plan,
                    current,
                    decisions[plan.image_id],
                    manifest.state.latest_by_image.get(plan.image_id),
                )
                root_directory.verify_current()
            if manifest.plan_sha256() != plan_sha256:
                raise _command_error("manifest_plan_mismatch")

            with media_global_writer_gate(root_directory):
                registry_events = {}
                with _database_write_fence_window(connection):
                    with transaction.atomic():
                        _acquire_database_write_fence(connection)
                        self._verify_current_plans(
                            manifest,
                            plans,
                            decisions,
                            root_directory,
                        )
                        for plan in plans:
                            if decisions[plan.image_id] != "register":
                                continue
                            registry_events[plan.image_id] = (
                                "recovered_registered"
                                if self._execute_candidate(
                                    plan,
                                    root_directory,
                                )
                                else "registered"
                            )
                            root_directory.verify_current()
                        self._verify_current_plans(
                            manifest,
                            plans,
                            decisions,
                            root_directory,
                        )

                self._fault("after_registry_commit")

                with _database_write_fence_window(connection):
                    with transaction.atomic():
                        _acquire_database_write_fence(connection)
                        self._verify_current_plans(
                            manifest,
                            plans,
                            decisions,
                            root_directory,
                        )
                        for plan in plans:
                            if (
                                plan.image_id
                                in manifest.state.latest_by_image
                            ):
                                continue
                            decision = decisions[plan.image_id]
                            if decision == "already_registered":
                                manifest.record_result(
                                    "already_registered",
                                    plan.image_id,
                                )
                            elif decision != "register":
                                manifest.record_result(
                                    "skipped",
                                    plan.image_id,
                                    reason_code=decision,
                                )
                            else:
                                self._record_registry_event(
                                    manifest,
                                    plan,
                                    plans,
                                    root_directory,
                                    registry_events[plan.image_id],
                                )
                            root_directory.verify_current()
                        self._verify_current_plans(
                            manifest,
                            plans,
                            decisions,
                            root_directory,
                        )
        except CommandError:
            raise
        except (MediaPathError, OSError) as error:
            raise _command_error(
                "registry_plan_identity_changed",
                error,
            )
        finally:
            if root_directory is not None:
                root_directory.close()

    def _verify_current_plans(
        self,
        manifest,
        plans,
        decisions,
        root_directory,
    ):
        self._verify_database_plan_closure(plans)
        for plan in plans:
            image = Image.objects.filter(pk=plan.image_id).first()
            if image is None:
                raise _command_error("registry_plan_identity_changed")
            current = self._freeze_plan(
                image,
                root_directory,
                "execute_verify",
            )
            self._verify_plan_equivalence(
                plan,
                current,
                decisions[plan.image_id],
                manifest.state.latest_by_image.get(plan.image_id),
            )
            root_directory.verify_current()
        return True

    @staticmethod
    def _verify_plan_equivalence(
        expected,
        current,
        decision,
        terminal,
    ):
        expected_values = (
            expected.image_id,
            expected.submitter_id,
            expected.content_sha256,
            expected.database_signature,
            expected.file_identities,
            expected.owner_ids,
            expected.preliminary_reason,
        )
        current_values = (
            current.image_id,
            current.submitter_id,
            current.content_sha256,
            current.database_signature,
            current.file_identities,
            current.owner_ids,
            current.preliminary_reason,
        )
        if expected_values != current_values:
            raise _command_error("registry_plan_identity_changed")
        if expected.registry_signature is not None:
            if (
                current.registry_signature != expected.registry_signature
                or current.key_registry_signature
                != expected.key_registry_signature
            ):
                raise _command_error("registry_plan_identity_changed")
            return
        if decision == "register" and current.registry_signature is not None:
            if (
                current.registry_signature[1:] != (
                    expected.image_id,
                    expected.submitter_id,
                    expected.content_sha256,
                )
                or current.key_registry_signature
                != current.registry_signature
            ):
                raise _command_error("registry_plan_identity_changed")
            return
        if (
            decision == "register"
            and terminal is not None
            and terminal["event"] in (
                "registered",
                "recovered_registered",
            )
        ):
            raise _command_error("registry_plan_identity_changed")
        if (
            current.registry_signature is not None
            or current.key_registry_signature
            != expected.key_registry_signature
        ):
            raise _command_error("registry_plan_identity_changed")

    def _execute_candidate(
        self,
        plan,
        root_directory,
    ):
        image = Image.objects.filter(pk=plan.image_id).first()
        if image is None:
            raise _command_error("registry_plan_identity_changed")
        thumbnails = list(
            Thumbnail.objects.filter(original_id=image.pk).order_by(
                "size", "pk"
            )
        )
        resources = None
        try:
            resources = self._open_candidate_resources(
                image,
                thumbnails,
                plan.submitter_id,
                root_directory,
                plan.database_signature,
                "execute",
            )
            if resources.closure.file_identities != plan.file_identities:
                raise _command_error("registry_plan_identity_changed")
            self._verify_owned_root_identity(root_directory, resources)
            current_registry = (
                MediaAsset.objects.filter(image_id=plan.image_id)
                .values(
                    "pk",
                    "image_id",
                    "submitter_id",
                    "content_sha256",
                )
                .first()
            )
            if current_registry is not None:
                signature = self._registry_signature(current_registry)
                if signature[1:] != (
                    plan.image_id,
                    plan.submitter_id,
                    plan.content_sha256,
                ):
                    raise _command_error("invalid_existing_registry")
                resources.verify_current(refresh_prepared=True)
                self._verify_owned_root_identity(
                    root_directory,
                    resources,
                )
                return True

            resources.verify_current(refresh_prepared=True)
            self._verify_owned_root_identity(root_directory, resources)
            self._fault("before_registry_insert")
            if (
                self._database_signature_for_id(plan.image_id)
                != plan.database_signature
                or tuple(
                    Pin.objects.filter(image_id=plan.image_id)
                    .order_by("submitter_id")
                    .values_list("submitter_id", flat=True)
                    .distinct()[:2]
                ) != plan.owner_ids
            ):
                raise _command_error("registry_plan_identity_changed")
            resources.verify_current()
            self._verify_owned_root_identity(root_directory, resources)
            try:
                MediaAsset.objects.create(
                    submitter_id=plan.submitter_id,
                    image_id=plan.image_id,
                    content_sha256=plan.content_sha256,
                )
            except IntegrityError as error:
                raise _command_error(
                    "registry_plan_identity_changed",
                    error,
                )
            resources.verify_current()
            root_directory.verify_current()
            return False
        except _CandidateSkip as error:
            raise _command_error(
                "registry_plan_identity_changed",
                error,
            )
        finally:
            if resources is not None:
                self._close_resources(resources)

    def _record_registry_event(
        self,
        manifest,
        plan,
        all_plans,
        root_directory,
        event_name,
    ):
        image = Image.objects.filter(pk=plan.image_id).first()
        if image is None:
            raise _command_error("registry_plan_identity_changed")
        thumbnails = list(
            Thumbnail.objects.filter(original_id=image.pk).order_by(
                "size", "pk"
            )
        )
        resources = None
        try:
            resources = self._open_candidate_resources(
                image,
                thumbnails,
                plan.submitter_id,
                root_directory,
                plan.database_signature,
                "execute",
            )
            if resources.closure.file_identities != plan.file_identities:
                raise _command_error("registry_plan_identity_changed")
            self._verify_owned_root_identity(root_directory, resources)
            return self._verify_registry_event(
                plan,
                all_plans,
                root_directory,
                resources,
                manifest,
                event_name,
            )
        except _CandidateSkip as error:
            raise _command_error(
                "registry_plan_identity_changed",
                error,
            )
        finally:
            if resources is not None:
                self._close_resources(resources)

    def _verify_registry_event(
        self,
        plan,
        all_plans,
        root_directory,
        resources,
        manifest,
        event_name,
    ):
        def verify_before_append():
            self._verify_database_plan_closure(all_plans)
            key_registries = list(
                MediaAsset.objects.filter(
                    submitter_id=plan.submitter_id,
                    content_sha256=plan.content_sha256,
                )
                .values(
                    "pk",
                    "image_id",
                    "submitter_id",
                    "content_sha256",
                )
            )
            image_registry = (
                MediaAsset.objects.filter(image_id=plan.image_id)
                .values(
                    "pk",
                    "image_id",
                    "submitter_id",
                    "content_sha256",
                )
                .first()
            )
            image_signature = self._registry_signature(image_registry)
            key_signature = (
                self._registry_signature(key_registries[0])
                if len(key_registries) == 1
                else None
            )
            if (
                image_signature is None
                or image_signature != key_signature
                or image_signature[1:] != (
                    plan.image_id,
                    plan.submitter_id,
                    plan.content_sha256,
                )
            ):
                raise _command_error("registry_plan_identity_changed")
            resources.verify_current(refresh_prepared=True)
            self._verify_owned_root_identity(root_directory, resources)
            return True

        manifest.record_result(
            event_name,
            plan.image_id,
            before_append=verify_before_append,
        )
        return True

    def _verify_database_plan_closure(self, plans):
        image_ids = tuple(
            Image.objects.order_by("pk").values_list("pk", flat=True)
        )
        planned_image_ids = tuple(plan.image_id for plan in plans)
        if image_ids != planned_image_ids:
            raise _command_error("registry_plan_identity_changed")
        for plan in plans:
            owner_ids = tuple(
                Pin.objects.filter(image_id=plan.image_id)
                .order_by("submitter_id")
                .values_list("submitter_id", flat=True)
                .distinct()[:2]
            )
            if (
                self._database_signature_for_id(plan.image_id)
                != plan.database_signature
                or owner_ids != plan.owner_ids
            ):
                raise _command_error("registry_plan_identity_changed")
        return True

    @staticmethod
    def _verify_owned_root_identity(root_directory, resources):
        try:
            root_directory.verify_current()
            resources.prepared.root_directory.verify_current()
            shared_stat = os.fstat(root_directory.descriptor)
            owned_stat = os.fstat(
                resources.prepared.root_directory.descriptor
            )
        except (MediaPathError, OSError) as error:
            raise _command_error(
                "registry_plan_identity_changed",
                error,
            )
        if _identity(shared_stat) != _identity(owned_stat):
            raise _command_error("registry_plan_identity_changed")
        return True

    @staticmethod
    def _close_resources(resources):
        for _attempt in range(2):
            try:
                resources.close()
            except BaseException:
                pass
            if resources._closed:
                return

    def _summary(self, manifest, plans, decisions):
        return _build_backfill_summary(
            manifest,
            plans,
            decisions,
            self.run_id,
        )

    def _fault(self, event):
        if self.fault_injector is not None:
            self.fault_injector(event)


class _CandidateSkip(Exception):
    def __init__(self, reason_code, file_identities=()):
        super(_CandidateSkip, self).__init__(reason_code)
        self.reason_code = reason_code
        self.file_identities = tuple(file_identities)
