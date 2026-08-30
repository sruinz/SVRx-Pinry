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
    transaction,
)
from django.db.models import Q
from PIL import Image as PILImage

from core.models import MediaAsset, Pin
from core.services.database_fence import (
    DatabaseFenceBusy,
    DatabaseFenceError,
    database_write_fence,
)
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
from django_images.services.migration_batch_log import (
    BatchIntent,
    BatchLimits,
    MigrationBatchJournal,
    MigrationBatchLogError,
    _durable_fsync,
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


def _command_error(code, cause=None, retryable=None):
    del cause
    error = CommandError(code)
    if retryable is not None:
        error.code = code
        error.retryable = retryable
    error.__suppress_context__ = True
    return error


@contextmanager
def _backfill_database_write_fence():
    try:
        with database_write_fence(
            using="default",
            models=(MediaAsset, Pin, Image, Thumbnail),
        ):
            yield
    except DatabaseFenceBusy:
        raise _command_error("database_busy", retryable=True) from None
    except DatabaseFenceError:
        raise _command_error("registry_plan_identity_changed") from None


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


class _BulkPlanContext(object):
    def __init__(
        self,
        images,
        thumbnails_by_image,
        owner_ids_by_image,
        registries_by_image,
        registries_by_key,
        database_signatures,
    ):
        self.images = {image.pk: image for image in images}
        self.thumbnails_by_image = thumbnails_by_image
        self.owner_ids_by_image = owner_ids_by_image
        self.registries_by_image = registries_by_image
        self.registries_by_key = registries_by_key
        self.database_signatures = database_signatures


class _ReceiptCandidateResources(object):
    def __init__(self, resource, receipts, closure):
        self.resource = resource
        self.receipts = tuple(receipts)
        self.closure = closure
        self._closed = False

    def verify_current(self, refresh_prepared=False):
        del refresh_prepared
        if self._closed:
            raise _command_error("registry_plan_identity_changed")
        try:
            return self.resource.verify_current()
        except MediaStorageError as error:
            raise _command_error("registry_plan_identity_changed", error)

    def close(self):
        if self._closed:
            return
        self.resource.close()
        self._closed = getattr(self.resource, "_closed", True)


class BatchCandidate(object):
    def __init__(self, plan, resources, target_values):
        self.plan = plan
        self.resources = resources
        self.target_values = target_values


class _ReceiptBatchMeasure(object):
    def __init__(self, receipts):
        self.receipts = tuple(receipts)


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
        self.plan_sha256_value = None
        self.manifest_sha256_value = hashlib.sha256(b"").hexdigest()


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
        state.manifest_sha256_value = hashlib.sha256(raw).hexdigest()
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
                state.plan_sha256_value = hashlib.sha256(
                    raw[:state.plan_end_offset]
                ).hexdigest()
            else:
                if not state.plan_complete:
                    raise _command_error("invalid_media_asset_manifest")
                image_id = event.get("image_id")
                if image_id not in state.plan_by_image:
                    raise _command_error("manifest_plan_mismatch")
                if event.get("plan_sha256") != state.plan_sha256_value:
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

    def write_frozen_plan(self, plans):
        if self.state.raw_bytes or self.state.events:
            raise _command_error("media_asset_plan_reset_forbidden")
        common = {
            "format_version": 2,
            "target_signature": TARGET_SIGNATURE,
            "run_id": self.run_id,
        }
        digest = hashlib.sha256()
        state = _ManifestState()
        written_bytes = 0
        self._verify_current()
        os.lseek(self.descriptor, 0, os.SEEK_END)

        def write_event(event):
            nonlocal written_bytes
            line = _json_line(event)
            digest.update(line)
            self._write_frozen_line(line)
            written_bytes += len(line)

        scanned = 0
        for plan in plans:
            plan._validate()
            if plan.image_id in state.plan_by_image:
                raise _command_error("manifest_plan_mismatch")
            write_event(dict(
                common,
                event="planned",
                plan=plan.as_dict(),
            ))
            state.plans.append(plan)
            state.plan_by_image[plan.image_id] = plan
            scanned += 1
        write_event(dict(
            common,
            event="plan_complete",
            scanned=scanned,
        ))
        _durable_fsync(self.descriptor, "plan_manifest")
        self._verify_current()
        digest_value = digest.hexdigest()
        state.events.append({"event": "plan_complete"})
        state.plan_complete = True
        state.plan_end_offset = written_bytes
        state.raw_bytes = None
        state.plan_sha256_value = digest_value
        state.manifest_sha256_value = digest_value
        self.state = state
        return digest_value

    def _write_frozen_line(self, line):
        view = memoryview(line)
        while view:
            written = os.write(self.descriptor, view)
            if written <= 0:
                raise _command_error("unsafe_media_asset_manifest")
            view = view[written:]

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
        return self.state.plan_sha256_value

    def manifest_sha256(self):
        if self.state.torn_tail is not None:
            raise _command_error(
                "media_asset_manifest_torn_tail_requires_execute"
            )
        self._ensure_content_current()
        return self.state.manifest_sha256_value

    def _ensure_content_current(self):
        self._verify_current()
        digest = hashlib.sha256()
        position = 0
        while True:
            chunk = os.pread(self.descriptor, 1024 * 1024, position)
            if not chunk:
                break
            digest.update(chunk)
            position += len(chunk)
        self._verify_current()
        if digest.hexdigest() != self.state.manifest_sha256_value:
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
    batch_journal=None,
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
        if batch_journal is not None:
            if not isinstance(batch_journal, MigrationBatchJournal):
                raise _command_error("linear_journal_invalid")
            try:
                batch_journal.state.require_source(
                    "backfill",
                    manifest.plan_sha256(),
                    manifest.manifest_sha256(),
                )
            except MigrationBatchLogError as error:
                raise _command_error(error.code, error)
            phase = batch_journal.state.phase_summaries.get("backfill")
            if phase is None:
                raise _command_error("media_asset_plan_incomplete")
            expected = MediaAssetBackfiller._phase_summary_for(
                plans, decisions
            )
            if phase != expected:
                raise _command_error("manifest_plan_mismatch")
            return MediaAssetBackfiller._summary_from_phase(
                manifest, phase, run_id
            )
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
        batch_size=50,
        media_storage=None,
        fault_injector=None,
        progress_reporter=None,
        batch_journal=None,
        batch_limits=None,
    ):
        if type(batch_size) is not int or batch_size <= 0:
            raise _command_error("batch_size_must_be_positive")
        if progress_reporter is not None and not callable(progress_reporter):
            raise _command_error("invalid_progress_reporter")
        if batch_journal is not None and not isinstance(
            batch_journal, MigrationBatchJournal
        ):
            raise _command_error("linear_journal_invalid")
        if batch_limits is not None and not isinstance(
            batch_limits, BatchLimits
        ):
            raise _command_error("linear_journal_limits_invalid")
        self.run_directory = run_directory
        self.filename = filename
        self.run_id = run_id
        self.service_uid = service_uid
        self.service_gid = service_gid
        self.batch_size = batch_size
        self.media_storage = media_storage or MediaStorage()
        self.fault_injector = fault_injector
        self.progress_reporter = progress_reporter
        self.batch_journal = batch_journal
        self.batch_limits = batch_limits or BatchLimits(
            max_images=batch_size
        )
        self._planning_reported = False

    def run(self, execute=False):
        if type(execute) is not bool:
            raise _command_error("invalid_execute_flag")
        if (
            self.batch_journal is not None
            and not self.batch_journal.is_phase_complete("paths")
        ):
            raise _command_error("paths_not_complete")
        if (
            self.batch_journal is not None
            and not self.batch_journal.state.attempts
        ):
            raise _command_error("linear_journal_invalid")
        expected_receipts = None
        if self.batch_journal is not None:
            expected_receipts = self._path_receipts()
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
                plans = self._freeze_all_plans(
                    "plan",
                    expected_receipts_by_image=expected_receipts,
                )
                manifest.write_frozen_plan(plans)
                self._fault("after_plan_complete")
            elif not manifest.state.plan_complete:
                raise _command_error("media_asset_plan_incomplete")
            plans = manifest.state.plans
            decisions = self._decisions(plans)
            self._validate_terminal_events(manifest, plans, decisions)
            self._report_planning(plans)
            if self.batch_journal is not None:
                try:
                    self.batch_journal.bind_source(
                        "backfill",
                        manifest.plan_sha256(),
                        manifest.manifest_sha256(),
                    )
                except MigrationBatchLogError as error:
                    raise _command_error(error.code, error)
                totals = self.batch_journal.state.work_totals
                if (
                    totals is None
                    or totals.get("backfill_total") != len(plans)
                ):
                    raise _command_error("linear_work_totals_changed")
            if execute:
                if self.batch_journal is None:
                    self._execute(manifest, plans, decisions)
                else:
                    return self._execute_linear(
                        manifest,
                        plans,
                        decisions,
                        expected_receipts,
                    )
            return self._summary(manifest, plans, decisions)

    def count_planned_images(self):
        max_pk = Image.objects.order_by("-pk").values_list(
            "pk", flat=True
        ).first() or 0
        if not max_pk:
            return 0
        return Image.objects.filter(pk__lte=max_pk).count()

    def _freeze_all_plans(
        self,
        phase,
        expected_receipts_by_image=None,
    ):
        root_directory = None
        remaining_receipt_ids = (
            None
            if expected_receipts_by_image is None
            else set(expected_receipts_by_image)
        )
        try:
            root_directory = open_verified_media_root(settings.MEDIA_ROOT)
            for context in self._plan_batches():
                batch_plans = []
                for image_id in sorted(context.images):
                    if (
                        remaining_receipt_ids is not None
                        and image_id not in remaining_receipt_ids
                    ):
                        raise _command_error(
                            "linear_journal_batch_conflict"
                        )
                    expected_receipts = (
                        None
                        if remaining_receipt_ids is None
                        else expected_receipts_by_image[image_id]
                    )
                    batch_plans.append(self._freeze_plan(
                        context.images[image_id],
                        root_directory,
                        phase,
                        context=context,
                        expected_receipts=expected_receipts,
                    ))
                    if remaining_receipt_ids is not None:
                        remaining_receipt_ids.remove(image_id)
                    root_directory.verify_current()
                for plan in self._attach_key_registry_signatures(
                    batch_plans
                ):
                    yield plan
            if remaining_receipt_ids:
                raise _command_error("linear_journal_batch_conflict")
            root_directory.verify_current()
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

    def _plan_batches(self):
        last_pk = 0
        max_pk = Image.objects.order_by("-pk").values_list(
            "pk", flat=True
        ).first() or 0
        while last_pk < max_pk:
            image_rows = list(Image.objects.filter(
                pk__gt=last_pk,
                pk__lte=max_pk,
            ).order_by("pk").values(
                "pk",
                "asset_uuid",
                "original_filename",
                "image",
                "width",
                "height",
            )[:self.batch_size])
            if not image_rows:
                return
            images = [Image(**row) for row in image_rows]
            ids = [row["pk"] for row in image_rows]
            yield self._bulk_plan_context(
                images, ids, image_rows=image_rows
            )
            last_pk = ids[-1]

    def _bulk_plan_context(self, images, image_ids, image_rows=None):
        if image_rows is None:
            image_rows = list(Image.objects.filter(
                pk__in=image_ids
            ).order_by("pk").values(
                "pk",
                "asset_uuid",
                "original_filename",
                "image",
                "width",
                "height",
            ))
        thumbnails_by_image = {image_id: [] for image_id in image_ids}
        thumbnail_rows = list(Thumbnail.objects.filter(
            original_id__in=image_ids
        ).order_by("original_id", "size", "pk").values(
            "pk", "original_id", "size", "image", "width", "height"
        ))
        for row in thumbnail_rows:
            thumbnail = Thumbnail(**row)
            thumbnails_by_image[thumbnail.original_id].append(thumbnail)

        raw_thumbnails = {image_id: [] for image_id in image_ids}
        for row in thumbnail_rows:
            raw_thumbnails[row["original_id"]].append(row)
        database_signatures = {
            row["pk"]: (
                row["pk"],
                str(row["asset_uuid"]),
                row["original_filename"],
                row["image"],
                row["width"],
                row["height"],
                tuple(
                    (
                        thumbnail["pk"],
                        thumbnail["size"],
                        thumbnail["image"],
                        thumbnail["width"],
                        thumbnail["height"],
                    )
                    for thumbnail in raw_thumbnails[row["pk"]]
                ),
            )
            for row in image_rows
        }

        owner_ids_by_image = {image_id: [] for image_id in image_ids}
        for image_id, submitter_id in Pin.objects.filter(
            image_id__in=image_ids
        ).order_by("image_id", "submitter_id").values_list(
            "image_id", "submitter_id"
        ):
            owners = owner_ids_by_image[image_id]
            if submitter_id not in owners and len(owners) < 2:
                owners.append(submitter_id)
        owner_ids_by_image = {
            image_id: tuple(owner_ids)
            for image_id, owner_ids in owner_ids_by_image.items()
        }
        registries = list(MediaAsset.objects.filter(
            image_id__in=image_ids
        ).values("pk", "image_id", "submitter_id", "content_sha256"))
        registries_by_image = {
            registry["image_id"]: registry for registry in registries
        }
        registries_by_key = {
            (registry["submitter_id"], registry["content_sha256"]): registry
            for registry in registries
        }
        return _BulkPlanContext(
            images,
            thumbnails_by_image,
            owner_ids_by_image,
            registries_by_image,
            registries_by_key,
            database_signatures,
        )

    def _attach_key_registry_signatures(self, plans):
        pairs = {
            (plan.submitter_id, plan.content_sha256)
            for plan in plans
            if plan.preliminary_reason is None
            and plan.registry_signature is None
        }
        registries_by_key = {}
        if pairs:
            registry_filter = Q()
            for submitter_id, content_sha256 in sorted(pairs):
                registry_filter |= Q(
                    submitter_id=submitter_id,
                    content_sha256=content_sha256,
                )
            registries_by_key = {
                (registry["submitter_id"], registry["content_sha256"]): (
                    registry
                )
                for registry in MediaAsset.objects.filter(
                    registry_filter
                ).values(
                    "pk", "image_id", "submitter_id", "content_sha256"
                )
            }
        indexed = []
        for plan in plans:
            if plan.preliminary_reason is not None:
                indexed.append(plan)
                continue
            signature = plan.key_registry_signature
            if plan.registry_signature is None:
                signature = self._registry_signature(
                    registries_by_key.get((
                        plan.submitter_id, plan.content_sha256
                    ))
                )
            indexed.append(_CandidatePlan(
                image_id=plan.image_id,
                submitter_id=plan.submitter_id,
                content_sha256=plan.content_sha256,
                database_signature=plan.database_signature,
                file_identities=plan.file_identities,
                owner_ids=plan.owner_ids,
                registry_signature=plan.registry_signature,
                key_registry_signature=signature,
                preliminary_reason=plan.preliminary_reason,
            ))
        return indexed

    def _freeze_plan(
        self,
        image,
        root_directory,
        phase,
        context=None,
        expected_receipts=None,
    ):
        if context is None:
            context = self._bulk_plan_context([image], [image.pk])
        thumbnails = context.thumbnails_by_image[image.pk]
        database_signature = context.database_signatures[image.pk]
        owner_ids = context.owner_ids_by_image[image.pk]
        registry_signature = self._registry_signature(
            context.registries_by_image.get(image.pk)
        )
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
                expected_receipts=expected_receipts,
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

        key_registry_signature = self._registry_signature(
            context.registries_by_key.get((
                submitter_id, closure.content_sha256
            ))
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
    def _database_signature(image, thumbnails):
        try:
            image_width = image.width
            image_height = image.height
            image_path = image.image.name
            return (
                image.pk,
                str(image.asset_uuid),
                image.original_filename,
                image_path,
                image_width,
                image_height,
                tuple(
                    (
                        thumbnail.pk,
                        thumbnail.size,
                        thumbnail.image.name,
                        thumbnail.width,
                        thumbnail.height,
                    )
                    for thumbnail in thumbnails
                ),
            )
        except (AttributeError, TypeError, ValueError):
            raise _command_error("registry_plan_identity_changed")

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
        expected_receipts=None,
    ):
        if expected_receipts is not None:
            return self._open_receipt_candidate_resources(
                image,
                thumbnails,
                submitter_id,
                expected_receipts,
                database_signature,
            )
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

    def _open_receipt_candidate_resources(
        self,
        image,
        thumbnails,
        submitter_id,
        expected_receipts,
        database_signature,
    ):
        receipts = tuple(expected_receipts)
        self._require_receipts_match_database_signature(
            receipts, database_signature
        )
        try:
            resource = self.media_storage.prepare_from_receipts(
                image,
                thumbnails,
                receipts,
                database_signature,
            )
        except MediaStorageError as error:
            if error.code == "media_configuration_error":
                raise _command_error(error.code, error)
            raise _command_error(
                "registry_plan_identity_changed", error
            )
        by_key = {receipt.file_key: receipt for receipt in receipts}
        original = by_key.get("original:{}".format(image.pk))
        if original is None:
            resource.close()
            raise _command_error("registry_plan_identity_changed")
        ordered = [original]
        thumbnail_by_size = {
            thumbnail.size: thumbnail for thumbnail in thumbnails
        }
        for kind in _DERIVATIVE_KINDS:
            thumbnail = thumbnail_by_size.get(kind)
            if thumbnail is None:
                resource.close()
                raise _command_error("registry_plan_identity_changed")
            receipt = by_key.get("thumbnail:{}:{}".format(
                image.pk, thumbnail.pk
            ))
            if receipt is None:
                resource.close()
                raise _command_error("registry_plan_identity_changed")
            ordered.append(receipt)
        file_identities = tuple(
            (
                kind,
                receipt.relative_path,
                receipt.destination_device,
                receipt.destination_inode,
                receipt.size,
                1,
                receipt.sha256,
                receipt.image_format,
                receipt.width,
                receipt.height,
            )
            for kind, receipt in zip(_KINDS, ordered)
        )
        closure = VerifiedClosure(
            image_id=image.pk,
            submitter_id=submitter_id,
            content_sha256=original.sha256,
            database_signature=database_signature,
            file_identities=file_identities,
        )
        return _ReceiptCandidateResources(resource, ordered, closure)

    @staticmethod
    def _require_receipts_match_database_signature(
        receipts, database_signature
    ):
        image_id = database_signature[0]
        expected_by_key = {
            "original:{}".format(image_id): database_signature[3]
        }
        expected_by_key.update({
            "thumbnail:{}:{}".format(image_id, thumbnail[0]): (
                thumbnail[2]
            )
            for thumbnail in database_signature[6]
        })
        receipt_by_key = {
            receipt.file_key: receipt for receipt in receipts
        }
        if (
            len(receipts) != len(expected_by_key)
            or len(receipt_by_key) != len(receipts)
            or set(receipt_by_key) != set(expected_by_key)
            or any(
                receipt_by_key[file_key].relative_path != relative_path
                for file_key, relative_path in expected_by_key.items()
            )
        ):
            raise _command_error("registry_plan_identity_changed")

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

    def _path_receipts(self):
        journal = self.batch_journal
        if not journal.is_phase_complete("paths"):
            raise _command_error("paths_not_complete")
        try:
            for batch_id in journal.committed_ids("paths"):
                intent = journal.intent_for(batch_id)
                if any(
                    receipt.database_signature != intent.post_signature
                    for receipt in journal.effective_receipts(batch_id)
                ):
                    raise _command_error(
                        "linear_journal_batch_conflict"
                    )
            return journal.receipts_by_image("paths")
        except MigrationBatchLogError as error:
            raise _command_error(error.code, error)

    def _verify_receipt_plan_closure(
        self, plans, receipts_by_image
    ):
        remaining_image_ids = set(receipts_by_image)
        for plan in plans:
            if plan.image_id not in remaining_image_ids:
                raise _command_error("linear_journal_batch_conflict")
            receipts = receipts_by_image[plan.image_id]
            self._require_receipts_match_database_signature(
                receipts, plan.database_signature
            )
            try:
                self.media_storage.verify_receipts_current(receipts)
            except MediaStorageError as error:
                if error.code == "media_configuration_error":
                    raise _command_error(error.code, error)
                raise _command_error(
                    "registry_plan_identity_changed", error
                )
            remaining_image_ids.remove(plan.image_id)
        if remaining_image_ids:
            raise _command_error("linear_journal_batch_conflict")
        return True

    def _execute_linear(
        self,
        manifest,
        plans,
        decisions,
        receipts_by_image,
    ):
        journal = self.batch_journal
        try:
            old_manifest_complete = all(
                plan.image_id in manifest.state.latest_by_image
                for plan in plans
            )
            if journal.is_phase_complete("backfill"):
                self._verify_database_plan_closure(
                    plans,
                    require_registered=old_manifest_complete,
                )
                if not old_manifest_complete:
                    self._verify_database_plan_closure(
                        plans, require_registered=True
                    )
                phase = journal.state.phase_summaries["backfill"]
                expected = self._phase_summary_for(plans, decisions)
                if phase != expected:
                    raise _command_error("manifest_plan_mismatch")
                return self._summary_from_phase(
                    manifest, phase, self.run_id
                )
            if old_manifest_complete:
                self._verify_database_plan_closure(
                    plans, require_registered=True
                )
                self._verify_receipt_plan_closure(
                    plans, receipts_by_image
                )
                phase = self._phase_summary_for(plans, decisions)
                existing_phase = journal.state.phase_summaries.get(
                    "backfill"
                )
                if existing_phase is not None and existing_phase != phase:
                    raise _command_error("manifest_plan_mismatch")
                journal.append_phase_complete("backfill", phase)
                journal.write_checkpoint()
                self._report_progress({"phase": "finalizing"})
                return self._summary_from_phase(
                    manifest, phase, self.run_id
                )
            self._verify_database_plan_closure(plans)
            imported_last_pk = self._upgrade_v2_terminal_prefix(
                manifest,
                journal,
                plans,
                decisions,
                receipts_by_image,
            )
            pending = [
                BatchCandidate(
                    plan,
                    _ReceiptBatchMeasure(receipts_by_image.get(
                        plan.image_id, ()
                    )),
                    None,
                )
                for plan in plans
                if plan.image_id > imported_last_pk
            ]
            for batch in self._build_backfill_batches(pending):
                self._execute_linear_batch(
                    journal,
                    tuple(batch),
                    decisions,
                    allow_v2_post_state=bool(
                        manifest.state.latest_by_image
                    ),
                )
            self._verify_database_plan_closure(
                plans, require_registered=True
            )
            self._verify_receipt_plan_closure(
                plans, receipts_by_image
            )
            phase = self._phase_summary_for(plans, decisions)
            journal.append_phase_complete("backfill", phase)
            journal.write_checkpoint()
            self._report_progress({"phase": "finalizing"})
            return self._summary_from_phase(
                manifest, phase, self.run_id
            )
        except MigrationBatchLogError as error:
            raise _command_error(error.code, error)

    def _upgrade_v2_terminal_prefix(
        self,
        manifest,
        journal,
        plans,
        decisions,
        receipts_by_image,
    ):
        completed = []
        seen_pending = False
        for plan in plans:
            terminal = manifest.state.latest_by_image.get(plan.image_id)
            if terminal is None:
                seen_pending = True
                continue
            if seen_pending:
                raise _command_error("manifest_plan_mismatch")
            completed.append(BatchCandidate(
                plan,
                _ReceiptBatchMeasure(receipts_by_image.get(
                    plan.image_id, ()
                )),
                None,
            ))
        if not completed:
            return 0
        for batch_number, batch in enumerate(
            self._build_backfill_batches(completed),
            1,
        ):
            plans_in_batch = tuple(item.plan for item in batch)
            batch_id = "upgrade-backfill:{}-{}".format(
                plans_in_batch[0].image_id,
                plans_in_batch[-1].image_id,
            )
            pre_signature = self._expected_registry_signature(
                plans_in_batch, decisions, after=False
            )
            post_signature = self._expected_registry_signature(
                plans_in_batch, decisions, after=True
            )
            if (
                self._current_registry_signature(plans_in_batch)
                != post_signature
            ):
                raise _command_error("linear_journal_database_conflict")
            intent = self._backfill_intent(
                batch_id,
                batch_number,
                tuple(batch),
                pre_signature,
                post_signature,
            )
            existing = journal.intent_for(batch_id)
            if existing is not None:
                if existing != intent:
                    raise _command_error(
                        "linear_journal_batch_conflict"
                    )
                recovery = journal.recover_batch(
                    batch_id, post_signature
                )
                if recovery == "append_commit":
                    journal.append_commit(batch_id, post_signature)
                    self._report_registering(journal)
                elif recovery != "committed":
                    raise _command_error(
                        "linear_journal_database_conflict"
                    )
                continue
            journal.import_v2_batch(intent, committed=True)
            self._report_registering(journal)
        return completed[-1].plan.image_id

    def _execute_linear_batch(
        self,
        journal,
        batch,
        decisions,
        allow_v2_post_state=False,
    ):
        plans = tuple(candidate.plan for candidate in batch)
        batch_id = "backfill:{}-{}".format(
            plans[0].image_id, plans[-1].image_id
        )
        pre_signature = self._expected_registry_signature(
            plans, decisions, after=False
        )
        post_signature = self._expected_registry_signature(
            plans, decisions, after=True
        )
        existing = journal.intent_for(batch_id)
        recovery = None
        adopt_v2_post_state = False
        if existing is not None:
            if (
                existing.first_pk != plans[0].image_id
                or existing.last_pk != plans[-1].image_id
                or existing.pre_signature != pre_signature
                or existing.post_signature != post_signature
                or existing.receipts != self._batch_receipts(batch)
            ):
                raise _command_error("linear_journal_batch_conflict")
            recovery = journal.recover_batch(
                batch_id, self._current_registry_signature(plans)
            )
        else:
            current = self._current_registry_signature(plans)
            adopt_v2_post_state = (
                allow_v2_post_state and current == post_signature
            )
            if current != pre_signature and not adopt_v2_post_state:
                raise _command_error("linear_journal_database_conflict")

        prepared = []
        try:
            context = self._bulk_plan_context(
                list(Image.objects.filter(
                    pk__in=[plan.image_id for plan in plans]
                ).order_by("pk")),
                [plan.image_id for plan in plans],
            )
            for candidate in batch:
                if decisions[candidate.plan.image_id] in (
                    "register",
                    "already_registered",
                ):
                    prepared.append(self._prepare_candidate(
                        candidate.plan,
                        candidate.resources.receipts,
                        context,
                    ))
                else:
                    prepared.append(candidate)
            if recovery == "committed":
                return
            if recovery == "append_commit":
                journal.append_commit(batch_id, post_signature)
                self._fault("after_backfill_commit")
                self._report_registering(journal)
                return
            if existing is None:
                batch_number = (
                    journal.last_committed_batch_for_phase("backfill") + 1
                )
                journal.append_intent(self._backfill_intent(
                    batch_id,
                    batch_number,
                    batch,
                    pre_signature,
                    post_signature,
                ))
                self._fault("after_backfill_intent")
            if not adopt_v2_post_state:
                self._apply_database_batch(
                    prepared, decisions, pre_signature, post_signature
                )
            self._fault("after_backfill_database_commit")
            journal.append_commit(batch_id, post_signature)
            self._fault("after_backfill_commit")
            self._report_registering(journal)
        finally:
            for candidate in reversed(prepared):
                resources = candidate.resources
                if isinstance(resources, _ReceiptCandidateResources):
                    self._close_resources(resources)

    def _prepare_candidate(self, plan, receipts, context):
        image = context.images.get(plan.image_id)
        if (
            image is None
            or context.database_signatures.get(plan.image_id)
            != plan.database_signature
            or context.owner_ids_by_image.get(plan.image_id)
            != plan.owner_ids
        ):
            raise _command_error("registry_plan_identity_changed")
        thumbnails = context.thumbnails_by_image[plan.image_id]
        try:
            resources = self._open_candidate_resources(
                image,
                thumbnails,
                plan.submitter_id,
                None,
                plan.database_signature,
                "execute",
                expected_receipts=receipts,
            )
        except _CandidateSkip as error:
            raise _command_error(
                "registry_plan_identity_changed", error
            )
        try:
            if (
                resources.closure.content_sha256
                != plan.content_sha256
                or resources.closure.file_identities
                != plan.file_identities
            ):
                raise _command_error("registry_plan_identity_changed")
            resources.verify_current()
            return BatchCandidate(
                plan,
                resources,
                {
                    "image_id": plan.image_id,
                    "submitter_id": plan.submitter_id,
                    "content_sha256": plan.content_sha256,
                },
            )
        except BaseException:
            self._close_resources(resources)
            raise

    def _build_backfill_batches(self, candidates):
        batch = []
        total_bytes = 0
        total_pixels = 0
        for candidate in candidates:
            receipts = tuple(candidate.resources.receipts)
            candidate_bytes = sum(receipt.size for receipt in receipts)
            candidate_pixels = sum(
                receipt.width * receipt.height for receipt in receipts
            )
            exceeds = batch and (
                len(batch) + 1 > self.batch_limits.max_images
                or total_bytes + candidate_bytes
                > self.batch_limits.max_bytes
                or total_pixels + candidate_pixels
                > self.batch_limits.max_pixels
            )
            if exceeds:
                yield tuple(batch)
                batch = []
                total_bytes = 0
                total_pixels = 0
            batch.append(candidate)
            total_bytes += candidate_bytes
            total_pixels += candidate_pixels
        if batch:
            yield tuple(batch)

    @staticmethod
    def _batch_receipts(batch):
        return tuple(
            receipt
            for candidate in batch
            for receipt in candidate.resources.receipts
        )

    def _backfill_intent(
        self,
        batch_id,
        batch_number,
        batch,
        pre_signature,
        post_signature,
    ):
        receipts = self._batch_receipts(batch)
        return BatchIntent.for_values(
            batch_id=batch_id,
            batch_number=batch_number,
            phase="backfill",
            first_pk=batch[0].plan.image_id,
            last_pk=batch[-1].plan.image_id,
            receipts=receipts,
            pre_signature=pre_signature,
            post_signature=post_signature,
            images=len(batch),
            files=len(receipts),
            total_bytes=sum(receipt.size for receipt in receipts),
            total_pixels=sum(
                receipt.width * receipt.height for receipt in receipts
            ),
        )

    @staticmethod
    def _hash_registry_rows(rows):
        return hashlib.sha256(json.dumps(
            sorted(rows),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()

    def _expected_registry_signature(self, plans, decisions, after):
        rows = []
        for plan in plans:
            if plan.registry_signature is not None:
                rows.append((
                    plan.image_id,
                    plan.registry_signature[2],
                    plan.registry_signature[3],
                ))
            elif after and decisions[plan.image_id] == "register":
                rows.append((
                    plan.image_id,
                    plan.submitter_id,
                    plan.content_sha256,
                ))
        return self._hash_registry_rows(rows)

    def _current_registry_signature(self, plans):
        image_ids = [plan.image_id for plan in plans]
        rows = MediaAsset.objects.filter(
            image_id__in=image_ids
        ).order_by("image_id").values_list(
            "image_id", "submitter_id", "content_sha256"
        )
        return self._hash_registry_rows(list(rows))

    def _apply_database_batch(
        self,
        candidates,
        decisions,
        pre_signature,
        post_signature,
    ):
        plans = tuple(candidate.plan for candidate in candidates)
        with transaction.atomic():
            if self._current_registry_signature(plans) != pre_signature:
                raise _command_error("linear_journal_database_conflict")
            self._verify_local_batch_context(plans)
            for candidate in candidates:
                resources = candidate.resources
                if isinstance(resources, _ReceiptCandidateResources):
                    resources.verify_current()
            values = [
                MediaAsset(
                    image_id=candidate.target_values["image_id"],
                    submitter_id=candidate.target_values["submitter_id"],
                    content_sha256=candidate.target_values[
                        "content_sha256"
                    ],
                )
                for candidate in candidates
                if decisions[candidate.plan.image_id] == "register"
            ]
            try:
                if values:
                    MediaAsset.objects.bulk_create(values)
            except IntegrityError as error:
                raise _command_error(
                    "linear_journal_database_conflict", error
                )
            if self._current_registry_signature(plans) != post_signature:
                raise _command_error("linear_journal_database_conflict")
            self._verify_local_batch_context(plans)
            for candidate in candidates:
                resources = candidate.resources
                if isinstance(resources, _ReceiptCandidateResources):
                    resources.verify_current()

    def _verify_local_batch_context(self, plans):
        image_ids = [plan.image_id for plan in plans]
        images = list(Image.objects.filter(
            pk__in=image_ids
        ).order_by("pk"))
        if [image.pk for image in images] != image_ids:
            raise _command_error("registry_plan_identity_changed")
        context = self._bulk_plan_context(images, image_ids)
        for plan in plans:
            if (
                context.database_signatures.get(plan.image_id)
                != plan.database_signature
                or context.owner_ids_by_image.get(plan.image_id)
                != plan.owner_ids
            ):
                raise _command_error("registry_plan_identity_changed")
        return True

    @staticmethod
    def _phase_summary_for(plans, decisions):
        reason_counts = {}
        registered = 0
        already_registered = 0
        skipped = 0
        for plan in plans:
            decision = decisions[plan.image_id]
            if decision == "register":
                registered += 1
            elif decision == "already_registered":
                already_registered += 1
            else:
                skipped += 1
                reason_counts[decision] = reason_counts.get(decision, 0) + 1
        return {
            "scanned": len(plans),
            "registered": registered,
            "already_registered": already_registered,
            "skipped": skipped,
            "reason_counts": dict(sorted(reason_counts.items())),
        }

    @staticmethod
    def _summary_from_phase(manifest, phase, run_id):
        return BackfillSummary(
            run_id=run_id,
            plan_sha256=manifest.plan_sha256(),
            manifest_sha256=manifest.manifest_sha256(),
            scanned=phase["scanned"],
            eligible=phase["registered"],
            registered=phase["registered"],
            already_registered=phase["already_registered"],
            skipped=phase["skipped"],
            reason_counts=dict(phase["reason_counts"]),
        )

    def _report_planning(self, plans):
        if self._planning_reported:
            return
        self._planning_reported = True
        self._report_progress({
            "phase": "backfill_planning",
            "backfill_total": len(plans),
        })

    def _report_registering(self, journal):
        snapshot = journal.recovery_snapshot()
        self._report_progress({
            "phase": "backfill_registering",
            "backfill_done": snapshot["backfill_done"],
            "backfill_total": snapshot["backfill_total"],
            "last_committed_batch": journal.last_committed_batch(),
        })

    def _report_progress(self, event):
        if self.progress_reporter is None:
            return False
        try:
            self.progress_reporter(dict(event))
        except Exception:
            return False
        return True

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
                current = self._attach_key_registry_signatures(
                    [current]
                )[0]
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
                with _backfill_database_write_fence():
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

                with _backfill_database_write_fence():
                    self._verify_current_plans(
                        manifest,
                        plans,
                        decisions,
                        root_directory,
                    )
                    for plan in plans:
                        if plan.image_id in manifest.state.latest_by_image:
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
            current = self._attach_key_registry_signatures(
                [current]
            )[0]
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

    def _verify_database_plan_closure(
        self, plans, require_registered=False
    ):
        image_rows = list(Image.objects.order_by("pk").values(
            "pk",
            "asset_uuid",
            "original_filename",
            "image",
            "width",
            "height",
        ))
        image_ids = tuple(row["pk"] for row in image_rows)
        planned_image_ids = tuple(plan.image_id for plan in plans)
        if image_ids != planned_image_ids:
            raise _command_error("registry_plan_identity_changed")
        thumbnail_rows = list(Thumbnail.objects.order_by(
            "original_id", "size", "pk"
        ).values(
            "pk", "original_id", "size", "image", "width", "height"
        ))
        thumbnails_by_image = {image_id: [] for image_id in image_ids}
        for row in thumbnail_rows:
            if row["original_id"] not in thumbnails_by_image:
                raise _command_error("registry_plan_identity_changed")
            thumbnails_by_image[row["original_id"]].append(row)
        owners_by_image = {image_id: [] for image_id in image_ids}
        for image_id, owner_id in Pin.objects.order_by(
            "image_id", "submitter_id"
        ).values_list("image_id", "submitter_id"):
            owners = owners_by_image.get(image_id)
            if owners is None:
                raise _command_error("registry_plan_identity_changed")
            if owner_id not in owners and len(owners) < 2:
                owners.append(owner_id)
        registry_by_image = {
            row["image_id"]: row
            for row in MediaAsset.objects.order_by("image_id").values(
                "pk", "image_id", "submitter_id", "content_sha256"
            )
        }
        decisions = self._decisions(plans)
        plan_by_image = {plan.image_id: plan for plan in plans}
        for row in image_rows:
            plan = plan_by_image[row["pk"]]
            signature = (
                row["pk"],
                str(row["asset_uuid"]),
                row["original_filename"],
                row["image"],
                row["width"],
                row["height"],
                tuple(
                    (
                        thumbnail["pk"],
                        thumbnail["size"],
                        thumbnail["image"],
                        thumbnail["width"],
                        thumbnail["height"],
                    )
                    for thumbnail in thumbnails_by_image[row["pk"]]
                ),
            )
            if (
                signature != plan.database_signature
                or tuple(owners_by_image[row["pk"]]) != plan.owner_ids
            ):
                raise _command_error("registry_plan_identity_changed")
            registry = self._registry_signature(
                registry_by_image.get(row["pk"])
            )
            if plan.registry_signature is not None:
                if registry != plan.registry_signature:
                    raise _command_error(
                        "linear_journal_database_conflict"
                    )
                continue
            if decisions[plan.image_id] == "register":
                expected = (
                    plan.image_id,
                    plan.submitter_id,
                    plan.content_sha256,
                )
                if registry is None and not require_registered:
                    continue
                if registry is None or registry[1:] != expected:
                    raise _command_error(
                        "linear_journal_database_conflict"
                    )
                continue
            if registry is not None:
                raise _command_error("linear_journal_database_conflict")
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
