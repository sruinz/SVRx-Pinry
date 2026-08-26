import ctypes
from dataclasses import dataclass
from datetime import datetime
import errno
import hashlib
import json
import logging
import os
from pathlib import PurePosixPath
import stat
import uuid


FORMAT_VERSION = 1
JOURNAL_FILENAME = "linear-migration-v1.jsonl"
CHECKPOINT_FILENAME = "linear-migration-checkpoint-v1.json"

_FSYNC_REASONS = frozenset((
    "journal_intent",
    "journal_commit",
    "journal_repair",
    "journal_tail_repair",
    "journal_header",
    "plan_manifest",
    "work_totals",
    "attempt",
    "phase_complete",
    "phase_checkpoint",
    "publication_directory",
))

LOGGER = logging.getLogger(__name__)


class MigrationBatchLogError(Exception):
    def __init__(self, code):
        super(MigrationBatchLogError, self).__init__(code)
        self.code = code


def _utc_now():
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical_json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _frame_for_event(event):
    payload = _canonical_json(event)
    return _canonical_json({
        "checksum": hashlib.sha256(payload).hexdigest(),
        "payload": event,
    }) + b"\n"


def _durable_fsync(descriptor, reason):
    if reason not in _FSYNC_REASONS:
        raise ValueError("unknown durability reason")
    os.fsync(descriptor)


def _load_libc():
    return ctypes.CDLL(None, use_errno=True)


def _durable_syncfs(descriptor, reason="batch_file_data"):
    if reason != "batch_file_data":
        raise ValueError("unknown durability reason")
    error_number = 0
    try:
        libc = _load_libc()
        syncfs = libc.syncfs
        ctypes.set_errno(0)
        result = syncfs(descriptor)
        error_number = ctypes.get_errno()
    except (AttributeError, OSError) as error:
        error_number = getattr(error, "errno", None) or errno.ENOSYS
        result = -1
    if result != 0:
        if not error_number:
            error_number = errno.EIO
        error = MigrationBatchLogError("linear_batch_file_sync_failed")
        error.errno = error_number
        raise error from OSError(error_number, os.strerror(error_number))


def _write_all(descriptor, value):
    position = 0
    while position < len(value):
        try:
            written = os.write(descriptor, value[position:])
        except OSError as error:
            if error.errno == errno.EINTR:
                continue
            raise
        if written <= 0:
            raise MigrationBatchLogError("linear_journal_write_failed")
        position += written


def _read_once(descriptor):
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks = []
    while True:
        try:
            chunk = os.read(descriptor, 1024 * 1024)
        except OSError as error:
            if error.errno == errno.EINTR:
                continue
            raise
        if not chunk:
            break
        chunks.append(chunk)
    os.lseek(descriptor, 0, os.SEEK_END)
    return b"".join(chunks)


@dataclass(frozen=True)
class BatchLimits(object):
    max_images: int = 50
    max_bytes: int = 256 * 1024 * 1024
    max_pixels: int = 500000000

    def __post_init__(self):
        values = (self.max_images, self.max_bytes, self.max_pixels)
        if any(type(value) is not int or value <= 0 for value in values):
            raise MigrationBatchLogError("linear_journal_limits_invalid")


def _is_sha256(value):
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_relative_posix_path(value):
    if type(value) is not str or not value or "\\" in value:
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and ".." not in path.parts
        and str(path) == value
        and value != "."
    )


@dataclass(frozen=True)
class FileReceipt(object):
    file_key: str
    relative_path: str
    operation: str
    size: int
    image_format: str
    width: int
    height: int
    source_device: int
    source_inode: int
    destination_device: int
    destination_inode: int
    sha256: str
    database_signature: str

    def __post_init__(self):
        integer_values = (
            self.size,
            self.width,
            self.height,
            self.source_device,
            self.source_inode,
            self.destination_device,
            self.destination_inode,
        )
        invalid_integer = any(type(value) is not int for value in integer_values)
        invalid_identity = (
            self.source_device < 0
            or self.destination_device < 0
            or self.source_inode <= 0
            or self.destination_inode <= 0
        ) if not invalid_integer else True
        if (
            type(self.file_key) is not str
            or not self.file_key
            or not _is_relative_posix_path(self.relative_path)
            or self.operation not in ("copy", "verify")
            or invalid_integer
            or self.size < 0
            or self.width <= 0
            or self.height <= 0
            or invalid_identity
            or type(self.image_format) is not str
            or not self.image_format
            or not _is_sha256(self.sha256)
            or not _is_sha256(self.database_signature)
        ):
            raise MigrationBatchLogError("linear_journal_receipt_invalid")

    @classmethod
    def for_values(
        cls,
        file_key,
        relative_path,
        operation,
        size,
        image_format,
        width,
        height,
        source_device,
        source_inode,
        destination_device,
        destination_inode,
        sha256,
        database_signature,
    ):
        return cls(
            file_key,
            relative_path,
            operation,
            size,
            image_format,
            width,
            height,
            source_device,
            source_inode,
            destination_device,
            destination_inode,
            sha256,
            database_signature,
        )

    def as_dict(self):
        return {
            "file_key": self.file_key,
            "relative_path": self.relative_path,
            "operation": self.operation,
            "size": self.size,
            "image_format": self.image_format,
            "width": self.width,
            "height": self.height,
            "source_device": self.source_device,
            "source_inode": self.source_inode,
            "destination_device": self.destination_device,
            "destination_inode": self.destination_inode,
            "sha256": self.sha256,
            "database_signature": self.database_signature,
        }

    @classmethod
    def from_dict(cls, value):
        keys = {
            "file_key",
            "relative_path",
            "operation",
            "size",
            "image_format",
            "width",
            "height",
            "source_device",
            "source_inode",
            "destination_device",
            "destination_inode",
            "sha256",
            "database_signature",
        }
        if type(value) is not dict or set(value) != keys:
            raise MigrationBatchLogError("linear_journal_receipt_invalid")
        try:
            return cls.for_values(**value)
        except (TypeError, ValueError):
            raise MigrationBatchLogError("linear_journal_receipt_invalid")


@dataclass(frozen=True)
class BatchIntent(object):
    batch_id: str
    batch_number: int
    phase: str
    first_pk: int
    last_pk: int
    receipts: tuple
    pre_signature: str
    post_signature: str
    images: int
    files: int
    total_bytes: int
    total_pixels: int

    def __post_init__(self):
        counters = (
            self.batch_number,
            self.first_pk,
            self.last_pk,
            self.images,
            self.files,
            self.total_bytes,
            self.total_pixels,
        )
        valid_counters = all(type(value) is int for value in counters)
        valid_receipts = (
            type(self.receipts) is tuple
            and bool(self.receipts)
            and all(isinstance(value, FileReceipt) for value in self.receipts)
            and len({value.file_key for value in self.receipts})
            == len(self.receipts)
        )
        if (
            type(self.batch_id) is not str
            or not self.batch_id
            or self.phase not in ("paths", "backfill")
            or not valid_counters
            or self.batch_number < 1
            or self.first_pk < 1
            or self.last_pk < self.first_pk
            or self.images < 1
            or self.files < 1
            or self.total_bytes < 0
            or self.total_pixels < 0
            or not valid_receipts
            or self.files != len(self.receipts)
            or self.total_bytes != sum(value.size for value in self.receipts)
            or self.total_pixels
            != sum(value.width * value.height for value in self.receipts)
            or not _is_sha256(self.pre_signature)
            or not _is_sha256(self.post_signature)
        ):
            raise MigrationBatchLogError("linear_journal_batch_invalid")

    @classmethod
    def for_values(
        cls,
        batch_id,
        batch_number,
        phase,
        first_pk,
        last_pk,
        receipts,
        pre_signature,
        post_signature,
        images,
        files,
        total_bytes,
        total_pixels,
    ):
        return cls(
            batch_id,
            batch_number,
            phase,
            first_pk,
            last_pk,
            receipts,
            pre_signature,
            post_signature,
            images,
            files,
            total_bytes,
            total_pixels,
        )

    def as_dict(self):
        return {
            "batch_id": self.batch_id,
            "batch_number": self.batch_number,
            "phase": self.phase,
            "first_pk": self.first_pk,
            "last_pk": self.last_pk,
            "receipts": [receipt.as_dict() for receipt in self.receipts],
            "pre_signature": self.pre_signature,
            "post_signature": self.post_signature,
            "images": self.images,
            "files": self.files,
            "total_bytes": self.total_bytes,
            "total_pixels": self.total_pixels,
        }

    @classmethod
    def from_dict(cls, value):
        keys = {
            "batch_id",
            "batch_number",
            "phase",
            "first_pk",
            "last_pk",
            "receipts",
            "pre_signature",
            "post_signature",
            "images",
            "files",
            "total_bytes",
            "total_pixels",
        }
        if type(value) is not dict or set(value) != keys:
            raise MigrationBatchLogError("linear_journal_batch_invalid")
        try:
            converted = dict(value)
            if type(converted["receipts"]) is not list:
                raise MigrationBatchLogError("linear_journal_batch_invalid")
            converted["receipts"] = tuple(
                FileReceipt.from_dict(receipt)
                for receipt in converted["receipts"]
            )
            return cls.for_values(**converted)
        except MigrationBatchLogError as error:
            if error.code == "linear_journal_batch_invalid":
                raise
            raise MigrationBatchLogError("linear_journal_batch_invalid")
        except (KeyError, TypeError, ValueError):
            raise MigrationBatchLogError("linear_journal_batch_invalid")


class JournalState(object):
    def __init__(self):
        self.replay_count = 0
        self.run_id = None
        self.started_at = None
        self.source_bindings = {}
        self.intents = {}
        self.commits = set()
        self.commit_order = []
        self.receipt_overlays = {}
        self.attempts = []
        self.work_totals = None
        self.phase_summaries = {}
        self.checkpoint_projection = None
        self.closed = False

    def apply(self, event):
        if type(event) is not dict or type(event.get("event")) is not str:
            raise MigrationBatchLogError("linear_journal_invalid")
        handlers = {
            "header": self._apply_header,
            "source_binding": self._apply_source_binding,
            "batch_intent": self._apply_batch_intent,
            "batch_commit": self._apply_batch_commit,
            "batch_repair": self._apply_batch_repair,
            "attempt_started": self._apply_attempt_started,
            "work_totals": self._apply_work_totals,
            "phase_complete": self._apply_phase_complete,
        }
        handler = handlers.get(event["event"])
        if handler is None:
            raise MigrationBatchLogError("linear_journal_invalid")
        handler(event)
        self.replay_count += 1

    def _apply_header(self, event):
        keys = {
            "event",
            "format_version",
            "run_id",
            "started_at",
            "source_manifest_sha256",
            "source_plan_sha256",
        }
        if (
            self.replay_count != 0
            or set(event) != keys
            or event["format_version"] != FORMAT_VERSION
            or type(event["run_id"]) is not str
            or not event["run_id"]
            or type(event["started_at"]) is not str
            or not event["started_at"]
            or not _is_sha256(event["source_plan_sha256"])
            or not _is_sha256(event["source_manifest_sha256"])
        ):
            raise MigrationBatchLogError("linear_journal_invalid")
        self.run_id = event["run_id"]
        self.started_at = event["started_at"]
        self.source_bindings["paths"] = {
            "plan_sha256": event["source_plan_sha256"],
            "manifest_sha256": event["source_manifest_sha256"],
        }

    def _apply_source_binding(self, event):
        keys = {
            "event",
            "phase",
            "plan_sha256",
            "manifest_sha256",
        }
        phase = event.get("phase")
        if (
            self.replay_count == 0
            or set(event) != keys
            or phase not in ("paths", "backfill")
            or phase in self.source_bindings
            or not _is_sha256(event.get("plan_sha256"))
            or not _is_sha256(event.get("manifest_sha256"))
        ):
            raise MigrationBatchLogError("linear_journal_invalid")
        self.source_bindings[phase] = {
            "plan_sha256": event["plan_sha256"],
            "manifest_sha256": event["manifest_sha256"],
        }

    def _apply_batch_intent(self, event):
        if set(event) != {"event", "intent"}:
            raise MigrationBatchLogError("linear_journal_invalid")
        try:
            intent = BatchIntent.from_dict(event["intent"])
        except MigrationBatchLogError:
            raise MigrationBatchLogError("linear_journal_invalid")
        if not _intent_follows_state(self, intent):
            raise MigrationBatchLogError("linear_journal_invalid")
        self.intents[intent.batch_id] = intent
        self.receipt_overlays[intent.batch_id] = intent.receipts

    def _apply_batch_commit(self, event):
        keys = {"event", "batch_id", "phase", "post_signature"}
        batch_id = event.get("batch_id")
        intent = self.intents.get(batch_id)
        if (
            set(event) != keys
            or intent is None
            or batch_id in self.commits
            or event.get("phase") != intent.phase
            or event.get("post_signature") != intent.post_signature
        ):
            raise MigrationBatchLogError("linear_journal_invalid")
        self.commits.add(batch_id)
        self.commit_order.append(batch_id)

    def _apply_batch_repair(self, event):
        keys = {"event", "batch_id", "phase", "receipts"}
        batch_id = event.get("batch_id")
        intent = self.intents.get(batch_id)
        if (
            set(event) != keys
            or intent is None
            or event.get("phase") != intent.phase
            or type(event.get("receipts")) is not list
        ):
            raise MigrationBatchLogError("linear_journal_invalid")
        try:
            receipts = tuple(
                FileReceipt.from_dict(value)
                for value in event["receipts"]
            )
            repaired = _validate_repair_overlay(intent, receipts)
        except MigrationBatchLogError:
            raise MigrationBatchLogError("linear_journal_invalid")
        if repaired == self.receipt_overlays[batch_id]:
            raise MigrationBatchLogError("linear_journal_invalid")
        self.receipt_overlays[batch_id] = repaired
        if intent.phase in self.phase_summaries:
            self.checkpoint_projection = _checkpoint_projection_for_state(
                self, self.replay_count + 1
            )

    def _apply_attempt_started(self, event):
        keys = {"event", "attempt", "resume_count", "started_at"}
        expected_attempt = len(self.attempts) + 1
        if (
            set(event) != keys
            or type(event.get("attempt")) is not int
            or event["attempt"] != expected_attempt
            or type(event.get("resume_count")) is not int
            or event["resume_count"] != expected_attempt - 1
            or type(event.get("started_at")) is not str
            or not event["started_at"]
        ):
            raise MigrationBatchLogError("linear_journal_invalid")
        self.attempts.append(dict(event))

    def _apply_work_totals(self, event):
        keys = {
            "event",
            "images_total",
            "files_total",
            "backfill_total",
        }
        values = (
            event.get("images_total"),
            event.get("files_total"),
            event.get("backfill_total"),
        )
        if (
            set(event) != keys
            or self.work_totals is not None
            or self.intents
            or any(type(value) is not int or value < 0 for value in values)
        ):
            raise MigrationBatchLogError("linear_journal_invalid")
        self.work_totals = {
            "images_total": values[0],
            "files_total": values[1],
            "backfill_total": values[2],
        }

    def _apply_phase_complete(self, event):
        keys = {"event", "phase", "summary"}
        phase = event.get("phase")
        try:
            summary = _validated_phase_summary(phase, event.get("summary"))
        except MigrationBatchLogError:
            raise MigrationBatchLogError("linear_journal_invalid")
        uncommitted = any(
            intent.phase == phase and batch_id not in self.commits
            for batch_id, intent in self.intents.items()
        )
        if (
            set(event) != keys
            or phase not in self.source_bindings
            or phase in self.phase_summaries
            or uncommitted
            or (phase == "backfill" and "paths" not in self.phase_summaries)
        ):
            raise MigrationBatchLogError("linear_journal_invalid")
        self.phase_summaries[phase] = summary
        self.checkpoint_projection = _checkpoint_projection_for_state(
            self, self.replay_count + 1
        )

    def require_source(self, phase, plan_sha256, manifest_sha256):
        expected = self.source_bindings.get(phase)
        if expected != {
            "plan_sha256": plan_sha256,
            "manifest_sha256": manifest_sha256,
        }:
            raise MigrationBatchLogError("linear_journal_source_changed")


def _validate_repair_overlay(intent, receipts):
    if type(receipts) not in (tuple, list):
        raise MigrationBatchLogError("linear_journal_batch_conflict")
    values = tuple(receipts)
    if not all(isinstance(value, FileReceipt) for value in values):
        raise MigrationBatchLogError("linear_journal_batch_conflict")
    by_key = {receipt.file_key: receipt for receipt in values}
    if len(by_key) != len(values):
        raise MigrationBatchLogError("linear_journal_batch_conflict")
    original_by_key = {
        receipt.file_key: receipt for receipt in intent.receipts
    }
    if set(by_key) != set(original_by_key):
        raise MigrationBatchLogError("linear_journal_batch_conflict")
    immutable_keys = set(FileReceipt.__dataclass_fields__) - {
        "destination_device",
        "destination_inode",
    }
    ordered = []
    for original in intent.receipts:
        repaired = by_key[original.file_key]
        if any(
            getattr(original, key) != getattr(repaired, key)
            for key in immutable_keys
        ):
            raise MigrationBatchLogError("linear_journal_batch_conflict")
        ordered.append(repaired)
    return tuple(ordered)


def _intent_follows_state(state, intent):
    previous = [
        value
        for value in state.intents.values()
        if value.phase == intent.phase
    ]
    return (
        state.work_totals is not None
        and intent.batch_id not in state.intents
        and intent.phase in state.source_bindings
        and intent.phase not in state.phase_summaries
        and not any(
            batch_id not in state.commits for batch_id in state.intents
        )
        and (
            intent.phase != "backfill"
            or "paths" in state.phase_summaries
        )
        and (
            not previous
            or intent.batch_number
            > max(value.batch_number for value in previous)
        )
        and (
            not previous
            or intent.first_pk > max(value.last_pk for value in previous)
        )
    )


def _checkpoint_basis_for_state(state):
    return {
        "source_bindings": state.source_bindings,
        "work_totals": state.work_totals,
        "attempts": state.attempts,
        "intents": {
            batch_id: intent.as_dict()
            for batch_id, intent in sorted(state.intents.items())
        },
        "commit_order": list(state.commit_order),
        "receipt_overlays": {
            batch_id: [
                receipt.as_dict()
                for receipt in state.receipt_overlays[batch_id]
            ]
            for batch_id in sorted(state.receipt_overlays)
        },
        "phase_summaries": state.phase_summaries,
    }


def _checkpoint_projection_for_state(state, generation):
    return {
        "format_version": FORMAT_VERSION,
        "run_id": state.run_id,
        "journal_generation": generation,
        "last_committed_batch": len(state.commit_order),
        "state_sha256": hashlib.sha256(
            _canonical_json(_checkpoint_basis_for_state(state))
        ).hexdigest(),
    }


def _validated_phase_summary(phase, summary):
    if type(summary) is not dict:
        raise MigrationBatchLogError("linear_journal_invalid")
    if phase == "paths":
        keys = {
            "image_count",
            "md5_legacy",
            "fixed_slot",
            "named_canonical",
            "copy_required_bytes",
        }
        if set(summary) != keys:
            raise MigrationBatchLogError("linear_journal_invalid")
        values = tuple(summary[key] for key in keys)
        if any(type(value) is not int or value < 0 for value in values):
            raise MigrationBatchLogError("linear_journal_invalid")
        if (
            summary["md5_legacy"]
            + summary["fixed_slot"]
            + summary["named_canonical"]
            != summary["image_count"]
        ):
            raise MigrationBatchLogError("linear_journal_invalid")
        return dict(summary)
    if phase == "backfill":
        keys = {
            "scanned",
            "registered",
            "already_registered",
            "skipped",
            "reason_counts",
        }
        if set(summary) != keys:
            raise MigrationBatchLogError("linear_journal_invalid")
        count_keys = keys - {"reason_counts"}
        if any(
            type(summary[key]) is not int or summary[key] < 0
            for key in count_keys
        ):
            raise MigrationBatchLogError("linear_journal_invalid")
        reason_counts = summary["reason_counts"]
        if (
            type(reason_counts) is not dict
            or any(
                type(key) is not str
                or not key
                or type(value) is not int
                or value <= 0
                for key, value in reason_counts.items()
            )
            or sum(reason_counts.values()) != summary["skipped"]
            or summary["registered"]
            + summary["already_registered"]
            + summary["skipped"]
            != summary["scanned"]
        ):
            raise MigrationBatchLogError("linear_journal_invalid")
        value = dict(summary)
        value["reason_counts"] = dict(sorted(reason_counts.items()))
        return value
    raise MigrationBatchLogError("linear_journal_invalid")


def _image_id_from_file_key(file_key):
    parts = file_key.split(":")
    if len(parts) < 2 or parts[0] not in (
        "original",
        "thumbnail",
        "derivative",
    ):
        raise MigrationBatchLogError("linear_journal_invalid")
    try:
        image_id = int(parts[1])
    except ValueError:
        raise MigrationBatchLogError("linear_journal_invalid")
    if image_id <= 0 or str(image_id) != parts[1]:
        raise MigrationBatchLogError("linear_journal_invalid")
    return image_id


def _directory_descriptor(run_directory):
    descriptor = getattr(run_directory, "descriptor", None)
    if type(descriptor) is not int:
        raise MigrationBatchLogError("linear_journal_directory_invalid")
    try:
        directory_stat = os.fstat(descriptor)
    except OSError:
        raise MigrationBatchLogError("linear_journal_directory_invalid")
    if not stat.S_ISDIR(directory_stat.st_mode):
        raise MigrationBatchLogError("linear_journal_directory_invalid")
    return descriptor


def _validate_open_values(filename, run_id, service_uid, service_gid, hashes):
    if (
        type(filename) is not str
        or not filename
        or PurePosixPath(filename).name != filename
        or type(run_id) is not str
        or not run_id
        or type(service_uid) is not int
        or service_uid < 0
        or type(service_gid) is not int
        or service_gid < 0
        or any(not _is_sha256(value) for value in hashes)
    ):
        raise MigrationBatchLogError("linear_journal_invalid")


def _verify_journal_descriptor(descriptor, service_uid, service_gid):
    file_stat = os.fstat(descriptor)
    if not stat.S_ISREG(file_stat.st_mode):
        raise MigrationBatchLogError("linear_journal_invalid")
    os.fchown(descriptor, service_uid, service_gid)
    os.fchmod(descriptor, 0o600)


def _open_existing_journal(
    directory_descriptor,
    filename,
    service_uid,
    service_gid,
):
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        descriptor = os.open(
            filename, flags, dir_fd=directory_descriptor
        )
    except FileNotFoundError:
        raise
    except OSError as error:
        failure = MigrationBatchLogError("linear_journal_invalid")
        raise failure from error
    try:
        _verify_journal_descriptor(descriptor, service_uid, service_gid)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _create_journal(
    directory_descriptor,
    filename,
    service_uid,
    service_gid,
    header,
):
    temporary_name = ".{}.{}.tmp".format(filename, uuid.uuid4().hex)
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | os.O_NOFOLLOW
        | os.O_CLOEXEC
    )
    descriptor = os.open(
        temporary_name,
        flags,
        0o600,
        dir_fd=directory_descriptor,
    )
    published = False
    try:
        _verify_journal_descriptor(descriptor, service_uid, service_gid)
        _write_all(descriptor, _frame_for_event(header))
        _durable_fsync(descriptor, "journal_header")
        os.rename(
            temporary_name,
            filename,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )
        published = True
        _durable_fsync(directory_descriptor, "journal_header")
        return descriptor
    except BaseException:
        os.close(descriptor)
        if not published:
            try:
                os.unlink(temporary_name, dir_fd=directory_descriptor)
            except OSError:
                pass
        raise


def _open_or_create_verified_journal(
    run_directory,
    filename,
    service_uid,
    service_gid,
    create,
    header,
):
    directory_descriptor = _directory_descriptor(run_directory)
    try:
        return _open_existing_journal(
            directory_descriptor,
            filename,
            service_uid,
            service_gid,
        )
    except FileNotFoundError:
        if not create:
            raise MigrationBatchLogError("linear_journal_missing")
    return _create_journal(
        directory_descriptor,
        filename,
        service_uid,
        service_gid,
        header,
    )


class MigrationBatchJournal(object):
    def __init__(
        self,
        run_directory,
        filename,
        descriptor,
        state,
        service_uid,
        service_gid,
    ):
        self.run_directory = run_directory
        self.filename = filename
        self.descriptor = descriptor
        self.state = state
        self.service_uid = service_uid
        self.service_gid = service_gid

    @classmethod
    def open(
        cls,
        run_directory,
        filename,
        run_id,
        service_uid,
        service_gid,
        source_plan_sha256,
        source_manifest_sha256,
        create=True,
    ):
        _validate_open_values(
            filename,
            run_id,
            service_uid,
            service_gid,
            (source_plan_sha256, source_manifest_sha256),
        )
        header = {
            "event": "header",
            "format_version": FORMAT_VERSION,
            "run_id": run_id,
            "started_at": _utc_now(),
            "source_manifest_sha256": source_manifest_sha256,
            "source_plan_sha256": source_plan_sha256,
        }
        descriptor = _open_or_create_verified_journal(
            run_directory,
            filename,
            service_uid,
            service_gid,
            create,
            header,
        )
        try:
            raw = _read_once(descriptor)
            state = cls._load_state_with_tail_repair(
                raw, run_id, descriptor
            )
            state.require_source(
                "paths", source_plan_sha256, source_manifest_sha256
            )
            return cls(
                run_directory,
                filename,
                descriptor,
                state,
                service_uid,
                service_gid,
            )
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def _load_state(raw, run_id):
        if not raw or not raw.endswith(b"\n"):
            raise MigrationBatchLogError("linear_journal_invalid")
        state = JournalState()
        for raw_line in raw.splitlines():
            try:
                frame = json.loads(raw_line.decode("utf-8"))
                if type(frame) is not dict or set(frame) != {
                    "checksum",
                    "payload",
                }:
                    raise ValueError
                payload = frame["payload"]
                if not _is_sha256(frame["checksum"]):
                    raise ValueError
                if hashlib.sha256(_canonical_json(payload)).hexdigest() != frame[
                    "checksum"
                ]:
                    raise ValueError
            except (UnicodeDecodeError, ValueError, TypeError, KeyError):
                raise MigrationBatchLogError("linear_journal_invalid")
            try:
                state.apply(payload)
            except MigrationBatchLogError:
                raise MigrationBatchLogError("linear_journal_invalid")
        if state.run_id != run_id:
            raise MigrationBatchLogError("linear_journal_invalid")
        return state

    @classmethod
    def _load_state_with_tail_repair(cls, raw, run_id, descriptor):
        if not raw or raw.endswith(b"\n"):
            return cls._load_state(raw, run_id)
        complete_offset = raw.rfind(b"\n") + 1
        if complete_offset == 0:
            raise MigrationBatchLogError("linear_journal_invalid")
        prefix = raw[:complete_offset]
        suffix = raw[complete_offset:]
        state = cls._load_state(prefix, run_id)
        try:
            os.ftruncate(descriptor, complete_offset)
            _durable_fsync(descriptor, "journal_tail_repair")
        except BaseException as error:
            failure = MigrationBatchLogError(
                "linear_journal_tail_repair_failed"
            )
            raise failure from error
        os.lseek(descriptor, 0, os.SEEK_END)
        LOGGER.warning(
            "Discarded unterminated journal suffix bytes=%d sha256=%s",
            len(suffix),
            hashlib.sha256(suffix).hexdigest(),
        )
        return state

    def _append_event(self, event, durable, reason):
        if self.descriptor is None:
            raise MigrationBatchLogError("linear_journal_closed")
        _write_all(self.descriptor, _frame_for_event(event))
        if durable:
            _durable_fsync(self.descriptor, reason)
        self.state.apply(event)

    def bind_source(self, phase, plan_sha256, manifest_sha256):
        if (
            phase not in ("paths", "backfill")
            or not _is_sha256(plan_sha256)
            or not _is_sha256(manifest_sha256)
        ):
            raise MigrationBatchLogError("linear_journal_source_changed")
        existing = self.state.source_bindings.get(phase)
        expected = {
            "plan_sha256": plan_sha256,
            "manifest_sha256": manifest_sha256,
        }
        if existing is not None:
            if existing != expected:
                raise MigrationBatchLogError("linear_journal_source_changed")
            return
        self._append_event(
            {
                "event": "source_binding",
                "phase": phase,
                "plan_sha256": plan_sha256,
                "manifest_sha256": manifest_sha256,
            },
            durable=True,
            reason="plan_manifest",
        )

    def record_attempt(self, started_at):
        if type(started_at) is not str or not started_at:
            raise MigrationBatchLogError("linear_journal_invalid")
        attempt = len(self.state.attempts) + 1
        self._append_event(
            {
                "event": "attempt_started",
                "attempt": attempt,
                "resume_count": attempt - 1,
                "started_at": started_at,
            },
            durable=True,
            reason="attempt",
        )
        return {
            "attempt": attempt,
            "resume_count": attempt - 1,
            "started_at": self.state.started_at,
        }

    def freeze_work_totals(
        self, images_total, files_total, backfill_total
    ):
        values = (images_total, files_total, backfill_total)
        expected = {
            "images_total": images_total,
            "files_total": files_total,
            "backfill_total": backfill_total,
        }
        if any(type(value) is not int or value < 0 for value in values):
            raise MigrationBatchLogError("linear_journal_batch_conflict")
        if self.state.work_totals is not None:
            if self.state.work_totals != expected:
                raise MigrationBatchLogError("linear_journal_batch_conflict")
            return
        if self.state.intents:
            raise MigrationBatchLogError("linear_journal_batch_conflict")
        self._append_event(
            {
                "event": "work_totals",
                "images_total": images_total,
                "files_total": files_total,
                "backfill_total": backfill_total,
            },
            durable=True,
            reason="work_totals",
        )

    def append_intent(self, intent):
        if (
            not isinstance(intent, BatchIntent)
            or not _intent_follows_state(self.state, intent)
        ):
            raise MigrationBatchLogError("linear_journal_batch_conflict")
        self._append_event(
            {"event": "batch_intent", "intent": intent.as_dict()},
            durable=True,
            reason="journal_intent",
        )

    def append_commit(self, batch_id, post_signature):
        intent = self.state.intents.get(batch_id)
        if intent is None or batch_id in self.state.commits:
            raise MigrationBatchLogError("linear_journal_batch_conflict")
        if post_signature != intent.post_signature:
            raise MigrationBatchLogError(
                "linear_journal_post_signature_changed"
            )
        self._append_event(
            {
                "event": "batch_commit",
                "batch_id": batch_id,
                "phase": intent.phase,
                "post_signature": post_signature,
            },
            durable=True,
            reason="journal_commit",
        )

    def append_repair(self, batch_id, receipts):
        intent = self.state.intents.get(batch_id)
        if intent is None:
            raise MigrationBatchLogError("linear_journal_batch_conflict")
        repaired = _validate_repair_overlay(intent, receipts)
        if repaired == self.effective_receipts(batch_id):
            return
        self._append_event(
            {
                "event": "batch_repair",
                "batch_id": batch_id,
                "phase": intent.phase,
                "receipts": [receipt.as_dict() for receipt in repaired],
            },
            durable=True,
            reason="journal_repair",
        )

    def effective_receipts(self, batch_id):
        receipts = self.state.receipt_overlays.get(batch_id)
        if receipts is None:
            raise MigrationBatchLogError("linear_journal_batch_conflict")
        return receipts

    def is_committed(self, batch_id):
        return batch_id in self.state.commits

    def recover_batch(self, batch_id, current_signature):
        intent = self.state.intents.get(batch_id)
        if intent is None:
            raise MigrationBatchLogError("linear_journal_batch_conflict")
        if batch_id in self.state.commits:
            return "committed"
        if current_signature == intent.post_signature:
            return "append_commit"
        if current_signature == intent.pre_signature:
            return "reexecute"
        raise MigrationBatchLogError("linear_journal_database_conflict")

    def append_phase_complete(self, phase, summary):
        validated = _validated_phase_summary(phase, summary)
        existing = self.state.phase_summaries.get(phase)
        if existing is not None:
            if existing != validated:
                raise MigrationBatchLogError("linear_journal_invalid")
            return
        if phase not in self.state.source_bindings or any(
            intent.phase == phase and batch_id not in self.state.commits
            for batch_id, intent in self.state.intents.items()
        ):
            raise MigrationBatchLogError("linear_journal_invalid")
        self._append_event(
            {
                "event": "phase_complete",
                "phase": phase,
                "summary": validated,
            },
            durable=True,
            reason="phase_complete",
        )

    def import_v2_batch(self, intent, committed=True):
        if type(committed) is not bool:
            raise MigrationBatchLogError("linear_journal_batch_conflict")
        self.append_intent(intent)
        if committed:
            self.append_commit(intent.batch_id, intent.post_signature)

    def intent_for(self, batch_id):
        return self.state.intents.get(batch_id)

    def committed_ids(self, phase):
        if phase not in ("paths", "backfill"):
            raise MigrationBatchLogError("linear_journal_invalid")
        return frozenset(
            batch_id
            for batch_id in self.state.commits
            if self.state.intents[batch_id].phase == phase
        )

    def receipts_by_image(self, phase):
        grouped = {}
        for batch_id in self.state.commit_order:
            intent = self.state.intents[batch_id]
            if intent.phase != phase:
                continue
            for receipt in self.effective_receipts(batch_id):
                image_id = _image_id_from_file_key(receipt.file_key)
                grouped.setdefault(image_id, []).append(receipt)
        return {
            image_id: tuple(receipts)
            for image_id, receipts in sorted(grouped.items())
        }

    def last_committed_batch_for_phase(self, phase):
        numbers = [
            self.state.intents[batch_id].batch_number
            for batch_id in self.state.commits
            if self.state.intents[batch_id].phase == phase
        ]
        return max(numbers) if numbers else 0

    def is_phase_complete(self, phase):
        return phase in self.state.phase_summaries

    def recovery_snapshot(self):
        totals = self.state.work_totals or {
            "images_total": 0,
            "files_total": 0,
            "backfill_total": 0,
        }
        committed = [
            self.state.intents[batch_id]
            for batch_id in self.state.commit_order
        ]
        attempt = len(self.state.attempts)
        return {
            "run_id": self.state.run_id,
            "attempt": attempt,
            "resume_count": max(0, attempt - 1),
            "started_at": self.state.started_at,
            "images_done": sum(
                intent.images for intent in committed if intent.phase == "paths"
            ),
            "images_total": totals["images_total"],
            "files_done": sum(
                intent.files for intent in committed if intent.phase == "paths"
            ),
            "files_total": totals["files_total"],
            "backfill_done": sum(
                intent.images
                for intent in committed
                if intent.phase == "backfill"
            ),
            "backfill_total": totals["backfill_total"],
            "last_committed_batch": self.last_committed_batch(),
        }

    def _checkpoint_value(self):
        if self.state.checkpoint_projection is None:
            return None
        return dict(self.state.checkpoint_projection)

    def _read_checkpoint(self):
        directory_descriptor = _directory_descriptor(self.run_directory)
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            descriptor = os.open(
                CHECKPOINT_FILENAME,
                flags,
                dir_fd=directory_descriptor,
            )
        except FileNotFoundError:
            return None
        except OSError as error:
            failure = MigrationBatchLogError("linear_journal_invalid")
            raise failure from error
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise MigrationBatchLogError("linear_journal_invalid")
            raw = _read_once(descriptor)
        finally:
            os.close(descriptor)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
        keys = {
            "format_version",
            "run_id",
            "journal_generation",
            "last_committed_batch",
            "state_sha256",
        }
        if type(value) is not dict or set(value) != keys:
            return None
        return value

    def _checkpoint_is_ahead(self, value):
        if type(value) is not dict:
            return False
        generation = value.get("journal_generation")
        committed = value.get("last_committed_batch")
        return (
            type(generation) is int
            and generation > self.state.replay_count
        ) or (
            type(committed) is int
            and committed > self.last_committed_batch()
        )

    def _write_checkpoint_value(self, value):
        directory_descriptor = _directory_descriptor(self.run_directory)
        temporary_name = ".{}.{}.tmp".format(
            CHECKPOINT_FILENAME, uuid.uuid4().hex
        )
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | os.O_CLOEXEC
        )
        descriptor = os.open(
            temporary_name,
            flags,
            0o600,
            dir_fd=directory_descriptor,
        )
        published = False
        try:
            _verify_journal_descriptor(
                descriptor, self.service_uid, self.service_gid
            )
            _write_all(descriptor, _canonical_json(value) + b"\n")
            _durable_fsync(descriptor, "phase_checkpoint")
            os.rename(
                temporary_name,
                CHECKPOINT_FILENAME,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
            )
            published = True
            _durable_fsync(directory_descriptor, "phase_checkpoint")
        finally:
            os.close(descriptor)
            if not published:
                try:
                    os.unlink(temporary_name, dir_fd=directory_descriptor)
                except OSError:
                    pass

    def write_checkpoint(self):
        desired = self._checkpoint_value()
        if desired is None:
            return
        current = self._read_checkpoint()
        if self._checkpoint_is_ahead(current):
            raise MigrationBatchLogError("linear_checkpoint_ahead")
        if current == desired:
            return
        self._write_checkpoint_value(desired)

    def validate_or_rebuild_checkpoint(self):
        desired = self._checkpoint_value()
        if desired is None:
            return
        current = self._read_checkpoint()
        if self._checkpoint_is_ahead(current):
            raise MigrationBatchLogError("linear_checkpoint_ahead")
        if current != desired:
            self._write_checkpoint_value(desired)

    def last_committed_batch(self):
        return len(self.state.commit_order)

    def _read_complete_file(self):
        return _read_once(self.descriptor)

    def close(self):
        if self.descriptor is None:
            return
        descriptor = self.descriptor
        self.descriptor = None
        self.state.closed = True
        os.close(descriptor)

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
