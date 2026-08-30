import errno
import hashlib
import json
import os
from pathlib import PurePosixPath
import stat
import time
import zipfile
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from core.models import Pin
from core.services.database_fence import (
    DatabaseFenceBusy,
    DatabaseFenceDeadline,
    database_write_fence,
)
from exports.contracts import (
    ExportError,
    LeaseLost,
    LeaseToken,
    StopRequested,
    WorkerLeaseToken,
    lock_current_lease,
    lock_current_worker_lease,
)
from exports.models import (
    ExportAttempt,
    ExportAttemptFile,
    ExportBlob,
    ExportItem,
    ExportJob,
    ExportSlot,
    ExportWorkerLease,
)
from exports.services.attempts import (
    ARCHIVE_PART_NAME,
    AttemptService,
    attempt_file_cleanup_receipt,
    closed_file_receipt,
)
from exports.services.file_ops import (
    ClosedFileReceipt,
    DirectoryReceipt,
    ExportStorageError,
    OpenFileReceipt,
    create_private_directory,
    open_export_root,
    open_receipted_directory,
    remove_if_receipt_matches,
    rename_noreplace,
)
from exports.services.metadata import (
    archive_display_name,
    format_utc,
    render_xmp,
    zip_datetime,
)
from exports.services.permissions import revoked_item_ids
from exports.services.snapshot import (
    open_staging_directory,
    snapshot_blob_name,
    snapshot_directory_name,
    snapshot_directory_receipt,
)


STREAM_CHUNK_SIZE = 1024 * 1024
QUERY_CHUNK_SIZE = 400
CLEANUP_HEARTBEAT_INTERVAL = 4.0
OWNER_DELETED = object()
User = get_user_model()


class ArchiveValidationStopped(Exception):
    pass


def _manifest_scope(job):
    board = None
    if job.scope == "board":
        board = {
            "id": job.board_id_snapshot,
            "name": job.board_name_snapshot,
            "is_public": not job.board_private_snapshot,
            "owner_username": job.board_owner_username_snapshot,
        }
    return {"type": job.scope, "board": board}


def _manifest_item(item, blob_hashes):
    digest = blob_hashes.get(item.blob_id)
    if digest is None:
        digest = blob_hashes.get(str(item.blob_id))
    if not isinstance(digest, str) or len(digest) != 64:
        raise ValueError("missing_blob_hash")
    return {
        "pin_id": item.pin_id,
        "owner_username": item.owner_username,
        "is_public": item.is_public,
        "published_at": format_utc(item.published_at),
        "description": item.description,
        "tags": json.loads(item.tags_json),
        "source_url": item.source_url,
        "source_url_redacted": item.source_url_redacted,
        "referer_url": item.referer_url,
        "referer_url_redacted": item.referer_url_redacted,
        "original_filename": item.original_filename,
        "archive_image_path": item.archive_image_path,
        "archive_xmp_path": item.archive_xmp_path,
        "mime_type": item.blob.mime_type,
        "size": item.blob.size,
        "sha256": digest,
    }


def build_manifest(job, items, blob_hashes, exported_at):
    document = {
        "schema_version": 1,
        "export_id": str(job.pk),
        "exported_at": format_utc(exported_at),
        "snapshot_at": format_utc(job.snapshot_at),
        "scope": _manifest_scope(job),
        "counts": {
            "requested_total": job.requested_total,
            "target_total": job.target_total,
            "included_total": job.included_total,
            "excluded_total": job.excluded_total,
        },
        "exclusion_reasons": {
            "not_visible_at_request": job.excluded_not_visible_total,
            "permission_revoked": job.excluded_permission_revoked_total,
        },
        "items": [
            _manifest_item(item, blob_hashes)
            for item in items
        ],
    }
    return json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _pulse(heartbeat, stop_requested):
    if stop_requested is not None and stop_requested():
        raise ArchiveValidationStopped()
    if heartbeat is not None:
        heartbeat()


def _stat_identity(current):
    return (
        current.st_dev,
        current.st_ino,
        current.st_size,
        getattr(current, "st_mtime_ns", int(current.st_mtime * 1000000000)),
        getattr(current, "st_ctime_ns", int(current.st_ctime * 1000000000)),
    )


def _safe_archive_name(name):
    if not isinstance(name, str) or not name or "\\" in name:
        return False
    path = PurePosixPath(name)
    return (
        not path.is_absolute()
        and all(part not in ("", ".", "..") for part in path.parts)
    )


def _expected_compression(name):
    if name == "manifest.json" or name.endswith(".xmp"):
        return zipfile.ZIP_DEFLATED
    return zipfile.ZIP_STORED


def validate_archive(fd, expected_names, heartbeat, stop_requested):
    try:
        before = os.fstat(fd)
        names_expected = list(expected_names)
        digest = hashlib.sha256()
        offset = 0
        while offset < before.st_size:
            chunk = os.pread(
                fd,
                min(STREAM_CHUNK_SIZE, before.st_size - offset),
                offset,
            )
            if not chunk:
                raise ExportStorageError("archive_failed")
            digest.update(chunk)
            offset += len(chunk)
            _pulse(heartbeat, stop_requested)

        duplicate = os.dup(fd)
        try:
            with os.fdopen(duplicate, "rb") as archive_file:
                duplicate = None
                with zipfile.ZipFile(archive_file, "r") as archive:
                    names = archive.namelist()
                    if (
                        names != names_expected
                        or len(names) != len(set(names))
                        or any(not _safe_archive_name(name) for name in names)
                    ):
                        raise ExportStorageError("archive_failed")
                    for info in archive.infolist():
                        if info.is_dir() or info.compress_type != (
                            _expected_compression(info.filename)
                        ):
                            raise ExportStorageError("archive_failed")
                        with archive.open(info, "r") as entry:
                            while entry.read(STREAM_CHUNK_SIZE):
                                _pulse(heartbeat, stop_requested)
        finally:
            if duplicate is not None:
                os.close(duplicate)
        after = os.fstat(fd)
        if _stat_identity(before) != _stat_identity(after):
            raise ExportStorageError("archive_failed")
        _pulse(heartbeat, stop_requested)
        return before.st_size, digest.hexdigest()
    except ArchiveValidationStopped:
        raise
    except ExportStorageError:
        raise
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError):
        raise ExportStorageError("archive_failed") from None


@dataclass(frozen=True)
class RecoveryOutcome(object):
    job: object
    lease: object


class ArchiveService(object):
    FENCE_MODELS = (
        User, Pin, ExportWorkerLease, ExportJob, ExportItem, ExportBlob,
        ExportSlot, ExportAttempt, ExportAttemptFile,
    )

    def __init__(self, using="default", attempt_service=None,
                 fault_injector=None, clock=None, monotonic=None,
                 sleeper=None):
        self.using = using
        self.attempt_service = attempt_service or AttemptService(using=using)
        self.fault_injector = fault_injector or (lambda point, context: None)
        self.clock = clock or timezone.now
        self.monotonic = monotonic or time.monotonic
        self.sleeper = sleeper or time.sleep

    def _fault(self, point, **context):
        self.fault_injector(point, context)

    @staticmethod
    def _stop(stop_requested, lease):
        if stop_requested is not None and stop_requested():
            raise StopRequested(lease)

    def _retry_recovery_db(
        self, operation, lease, heartbeat, stop_requested,
    ):
        deadline = DatabaseFenceDeadline(self.monotonic)
        while True:
            try:
                return operation()
            except DatabaseFenceBusy:
                deadline.checkpoint()
                self._stop(stop_requested, lease)
                heartbeat()
                self.sleeper(0.01)
                self._stop(stop_requested, lease)

    @staticmethod
    def _chunks(values):
        values = list(values)
        for start in range(0, len(values), QUERY_CHUNK_SIZE):
            yield values[start:start + QUERY_CHUNK_SIZE]

    def _apply_revocations_locked(
        self, current, revoked, checkpoint, lease=None,
    ):
        changed = 0
        removed_bytes = 0
        for item_ids in self._chunks(revoked):
            included = list(ExportItem.objects.using(self.using).filter(
                job=current, pk__in=item_ids, inclusion_state="included",
            ).values_list("pk", "blob__size"))
            if not included:
                continue
            included_ids = [item_id for item_id, size in included]
            sizes = [size for item_id, size in included]
            if any(size is None or size < 0 for size in sizes):
                raise ExportError("export_storage_unsafe", lease)
            updated = ExportItem.objects.using(self.using).filter(
                job=current, pk__in=included_ids,
                inclusion_state="included",
            ).update(
                inclusion_state="excluded",
                exclusion_reason="permission_revoked",
            )
            if updated != len(included_ids):
                raise ExportError("export_storage_unsafe", lease)
            changed += updated
            removed_bytes += sum(sizes)
            checkpoint()
        if (
            current.included_total + current.excluded_total
            != current.requested_total
            or current.archive_total != current.included_total
            or current.excluded_permission_revoked_total
            > current.excluded_total
            or changed > current.included_total
            or removed_bytes > current.bytes_total
        ):
            raise ExportError("export_storage_unsafe", lease)
        current.included_total -= changed
        current.excluded_total += changed
        current.excluded_permission_revoked_total += changed
        if (
            current.excluded_permission_revoked_total
            > current.excluded_total
        ):
            raise ExportError("export_storage_unsafe", lease)
        current.archive_total -= changed
        current.bytes_total -= removed_bytes
        current.archive_done = 0
        current.bytes_done = 0
        return changed

    def _apply_revocation_batch(
        self, item_ids, lease, heartbeat, expected_state,
    ):
        with heartbeat.foreground_write_guard():
            with database_write_fence(
                using=self.using, models=self.FENCE_MODELS,
            ):
                current = lock_current_lease(lease, using=self.using)
                if current.state != expected_state:
                    raise LeaseLost()
                deadline = DatabaseFenceDeadline(self.monotonic)
                self._apply_revocations_locked(
                    current, item_ids, deadline.checkpoint, lease,
                )
                current.save(update_fields=(
                    "included_total", "excluded_total",
                    "excluded_permission_revoked_total", "archive_total",
                    "bytes_total", "archive_done", "bytes_done",
                ))
                deadline.checkpoint()
                return current

    def _apply_revocation_batches(
        self, revoked, lease, heartbeat, stop_requested, expected_state,
    ):
        current = None
        for item_ids in self._chunks(revoked):
            current = self._retry_recovery_db(
                lambda item_ids=item_ids: self._apply_revocation_batch(
                    item_ids, lease, heartbeat, expected_state,
                ),
                lease,
                heartbeat,
                stop_requested,
            )
            self._stop(stop_requested, lease)
            heartbeat()
        return current

    def _initial_permission_check(self, job, lease, heartbeat, stop_requested):
        revoked = revoked_item_ids(
            job, inclusion_state="included", using=self.using,
            heartbeat=heartbeat, stop_requested=stop_requested, lease=lease,
        )
        if revoked:
            self._apply_revocation_batches(
                revoked, lease, heartbeat, stop_requested, "archiving",
            )
        current = ExportJob.objects.using(self.using).get(pk=lease.job_id)
        if current.included_total == 0:
            raise ExportError("all_items_revoked", lease)
        return current

    def _archive_inputs(self, job, lease):
        receipt = snapshot_directory_receipt(job)
        candidate = (
            job.candidate_snapshot_generation,
            job.candidate_snapshot_relative_path,
            job.candidate_snapshot_dir_dev,
            job.candidate_snapshot_dir_ino,
            job.candidate_snapshot_dir_uid,
            job.candidate_snapshot_dir_gid,
            job.candidate_snapshot_dir_mode,
        )
        items = list(job.items.filter(
            inclusion_state="included",
        ).select_related("blob").order_by("target_position", "pk"))
        paths = [path for item in items for path in (
            item.archive_image_path, item.archive_xmp_path,
        )]
        invalid_blob = any(
            item.snapshot_generation != job.snapshot_generation
            or item.blob.snapshot_generation != job.snapshot_generation
            or not item.blob.confirmed
            or item.blob.file_state != "closed"
            or item.blob.cleanup_state != "pending"
            for item in items
        )
        if (
            job.state != "archiving" or job.pk != lease.job_id
            or receipt is None or not job.snapshot_relative_path
            or any(value is not None for value in candidate)
            or not items or any(not path for path in paths)
            or len(paths) != len(set(paths)) or invalid_blob
            or job.included_total != len(items)
            or job.archive_total != len(items)
            or job.bytes_total != sum(item.blob.size for item in items)
        ):
            raise ExportError("export_storage_unsafe", lease)
        return items, receipt

    @staticmethod
    def _blob_receipt(blob):
        return ClosedFileReceipt(
            blob.receipt_dev, blob.receipt_ino, blob.receipt_uid,
            blob.receipt_gid, blob.receipt_mode, blob.receipt_nlink,
            blob.size, blob.receipt_mtime_ns, blob.receipt_ctime_ns,
            blob.receipt_sha256,
        )

    def _record_blob_hash(self, blob, digest, lease, heartbeat):
        with heartbeat.foreground_write_guard():
            with transaction.atomic(using=self.using):
                lock_current_lease(lease, using=self.using)
                current = ExportBlob.objects.using(
                    self.using,
                ).select_for_update().get(pk=blob.pk)
                if current.receipt_sha256 not in (None, digest):
                    raise ExportError("source_changed", lease)
                if current.receipt_sha256 is None:
                    current.receipt_sha256 = digest
                    current.save(update_fields=("receipt_sha256",))
        return digest

    def _stream_blob(self, directory, blob, destination, lease,
                     heartbeat, stop_requested):
        receipt = self._blob_receipt(blob)
        flags = os.O_RDONLY | os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        descriptor = None
        digest = hashlib.sha256()
        bytes_written = 0
        try:
            descriptor = os.open(
                snapshot_blob_name(blob.pk), flags,
                dir_fd=directory.descriptor,
            )
            receipt.verify_identity(descriptor)
            while True:
                chunk = os.read(descriptor, STREAM_CHUNK_SIZE)
                if not chunk:
                    break
                destination.write(chunk)
                digest.update(chunk)
                bytes_written += len(chunk)
                self._fault(
                    "after_archive_chunk",
                    blob=blob,
                    lease=lease,
                    chunk_size=len(chunk),
                    bytes_written=bytes_written,
                )
                self._stop(stop_requested, lease)
                heartbeat()
            receipt.verify_identity(descriptor)
        finally:
            if descriptor is not None:
                os.close(descriptor)
        value = digest.hexdigest()
        if blob.expected_sha256 not in (None, value):
            raise ExportError("source_changed", lease)
        return self._record_blob_hash(blob, value, lease, heartbeat)

    def _progress(self, lease, done, byte_count, heartbeat):
        with heartbeat.foreground_write_guard():
            with transaction.atomic(using=self.using):
                current = lock_current_lease(lease, using=self.using)
                if current.state != "archiving":
                    raise LeaseLost()
                current.archive_done = done
                current.bytes_done = byte_count
                current.progress_at = self.clock()
                current.save(update_fields=(
                    "archive_done", "bytes_done", "progress_at",
                ))

    def _write_zip(self, job, lease, heartbeat, stop_requested):
        items, snapshot_receipt = self._archive_inputs(job, lease)
        root = open_export_root(
            settings.PINRY_EXPORT_ROOT, os.getuid(), os.getgid(),
        )
        staging = snapshot_directory = attempt_directory = None
        descriptor = None
        try:
            staging = open_staging_directory(root)
            snapshot_directory = open_receipted_directory(
                staging, job.snapshot_relative_path, snapshot_receipt,
            )
            attempt_directory = self.attempt_service.create_directory_fs(
                staging, job, lease,
            )
            self._fault(
                "after_attempt_directory_create",
                directory=attempt_directory,
                job=job,
                lease=lease,
            )
            attempt = self.attempt_service.record_directory_receipt_db_only(
                job, lease, attempt_directory, heartbeat, stop_requested,
            )
            descriptor, open_receipt = self.attempt_service.create_file_fs(
                attempt_directory,
            )
            attempt_file = self.attempt_service.record_open_file_receipt_db_only(
                attempt, lease, open_receipt, heartbeat, stop_requested,
            )
            expected_names = []
            blob_hashes = {}
            with os.fdopen(descriptor, "w+b", closefd=False) as archive_file:
                with zipfile.ZipFile(
                    archive_file, "w", compression=zipfile.ZIP_DEFLATED,
                    allowZip64=True,
                ) as output:
                    byte_count = 0
                    for index, item in enumerate(items, 1):
                        info = zipfile.ZipInfo(
                            item.archive_image_path,
                            zip_datetime(item.published_at),
                        )
                        info.compress_type = zipfile.ZIP_STORED
                        with output.open(info, "w", force_zip64=True) as target:
                            digest = self._stream_blob(
                                snapshot_directory, item.blob, target, lease,
                                heartbeat, stop_requested,
                            )
                        blob_hashes[item.blob_id] = digest
                        expected_names.append(item.archive_image_path)
                        xmp = zipfile.ZipInfo(
                            item.archive_xmp_path,
                            zip_datetime(item.published_at),
                        )
                        xmp.compress_type = zipfile.ZIP_DEFLATED
                        output.writestr(xmp, render_xmp(
                            item.published_at, item.description,
                            json.loads(item.tags_json),
                        ))
                        expected_names.append(item.archive_xmp_path)
                        byte_count += item.blob.size
                        self._progress(lease, index, byte_count, heartbeat)
                    exported_at = self.attempt_service.ensure_exported_at(
                        attempt, lease, self.clock(), heartbeat,
                        stop_requested,
                    )
                    manifest = zipfile.ZipInfo(
                        "manifest.json", zip_datetime(exported_at),
                    )
                    manifest.compress_type = zipfile.ZIP_DEFLATED
                    output.writestr(
                        manifest,
                        build_manifest(job, items, blob_hashes, exported_at),
                    )
                    expected_names.append("manifest.json")
            closing_descriptor = descriptor
            descriptor = None
            closed = self.attempt_service.close_file_fs(
                closing_descriptor, open_receipt,
            )
            attempt_file = self.attempt_service.record_closed_file_receipt_db_only(
                attempt, attempt_file, closed, lease, heartbeat,
                stop_requested,
            )
            self._fault(
                "after_archive_closed_receipt",
                attempt=attempt,
                attempt_file=attempt_file,
                lease=lease,
            )
            return attempt, attempt_file, tuple(expected_names), exported_at
        except ExportStorageError:
            raise
        except OSError as error:
            code = (
                "insufficient_space"
                if error.errno == errno.ENOSPC
                else "archive_failed"
            )
            raise ExportStorageError(code) from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
            for directory in (
                attempt_directory, snapshot_directory, staging, root,
            ):
                if directory is not None:
                    directory.close()

    def _build_and_verify_attempt(self, job, lease, heartbeat, stop_requested):
        attempt, attempt_file, names, exported_at = self._write_zip(
            job, lease, heartbeat, stop_requested,
        )
        descriptor = self.attempt_service.open_closed_file_for_validation(
            attempt_file, lease,
        )
        try:
            try:
                size, digest = validate_archive(
                    descriptor, names, heartbeat, stop_requested,
                )
            except ArchiveValidationStopped:
                raise StopRequested(lease) from None
        finally:
            os.close(descriptor)
        self.attempt_service.record_verifying(
            attempt, attempt_file, size, digest, exported_at,
            lease, heartbeat, stop_requested,
        )
        attempt.refresh_from_db()
        attempt_file.refresh_from_db()
        return attempt, attempt_file, exported_at

    def _rotate_locked(
        self, current, attempt, attempt_file, revoked, lease, checkpoint,
    ):
        if self._apply_revocations_locked(
            current, revoked, checkpoint, lease,
        ) == 0:
            return None
        return self._rotate_applied_locked(
            current, attempt, attempt_file, lease,
        )

    @staticmethod
    def _rotate_applied_locked(current, attempt, attempt_file, lease):
        attempt.state = "retiring"
        attempt.save(update_fields=("state",))
        attempt_file.state = "retiring"
        attempt_file.save(update_fields=("state",))
        current.state = "archiving"
        current.attempt_generation += 1
        current.verifying_attempt_generation = None
        current.verifying_size = None
        current.verifying_sha256 = None
        current.verifying_completed_at = None
        current.save(update_fields=(
            "state", "attempt_generation", "included_total",
            "excluded_total", "excluded_permission_revoked_total",
            "archive_total", "bytes_total", "archive_done", "bytes_done",
            "verifying_attempt_generation", "verifying_size",
            "verifying_sha256", "verifying_completed_at",
        ))
        return LeaseToken(
            lease.worker_generation, lease.worker_lease_uuid,
            lease.job_id, lease.job_lease_uuid,
            current.attempt_generation,
        )

    def _post_build_revocations(self, job, attempt, attempt_file, lease,
                                heartbeat, stop_requested):
        self._fault(
            "before_final_permission_check", job=job, attempt=attempt,
            attempt_file=attempt_file,
        )
        current = ExportJob.objects.using(self.using).get(pk=lease.job_id)
        revoked = revoked_item_ids(
            current, inclusion_state="included", using=self.using,
            heartbeat=heartbeat, stop_requested=stop_requested, lease=lease,
        )
        if not revoked:
            return None
        self._apply_revocation_batches(
            revoked, lease, heartbeat, stop_requested, "verifying",
        )

        def rotate():
            with heartbeat.job_token_transition(lease) as rotation:
                with heartbeat.foreground_write_guard():
                    with database_write_fence(
                        using=self.using, models=self.FENCE_MODELS,
                    ):
                        current = lock_current_lease(
                            lease, using=self.using,
                        )
                        if current.state != "verifying":
                            raise LeaseLost()
                        locked_attempt = ExportAttempt.objects.using(
                            self.using,
                        ).select_for_update().get(pk=attempt.pk)
                        locked_file = ExportAttemptFile.objects.using(
                            self.using,
                        ).select_for_update().get(pk=attempt_file.pk)
                        rotated = self._rotate_applied_locked(
                            current, locked_attempt, locked_file, lease,
                        )
                rotation.replace(rotated)
            return rotated

        return self._retry_recovery_db(
            rotate, lease, heartbeat, stop_requested,
        )

    @staticmethod
    def _attempt_directory_receipt(attempt):
        return DirectoryReceipt(
            attempt.dir_dev, attempt.dir_ino, attempt.dir_uid,
            attempt.dir_gid, attempt.dir_mode,
        )

    def _open_ready_directory(self, root):
        try:
            current = os.stat(
                "ready", dir_fd=root.descriptor, follow_symlinks=False,
            )
        except FileNotFoundError:
            return create_private_directory(
                root, "ready", root.uid, root.gid,
            )
        receipt = DirectoryReceipt(
            current.st_dev, current.st_ino, current.st_uid, current.st_gid,
            stat.S_IMODE(current.st_mode),
        )
        return open_receipted_directory(root, "ready", receipt)

    @staticmethod
    def _set_receipt(model, receipt):
        model.receipt_dev = receipt.dev
        model.receipt_ino = receipt.ino
        model.receipt_uid = receipt.uid
        model.receipt_gid = receipt.gid
        model.receipt_mode = receipt.mode
        model.receipt_nlink = receipt.nlink
        model.receipt_size = receipt.size
        model.receipt_mtime_ns = receipt.mtime_ns
        model.receipt_ctime_ns = receipt.ctime_ns
        model.receipt_sha256 = receipt.sha256

    @staticmethod
    def _hash_descriptor(descriptor):
        digest = hashlib.sha256()
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            while True:
                chunk = os.read(descriptor, STREAM_CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
            os.lseek(descriptor, 0, os.SEEK_SET)
        except OSError:
            raise ExportStorageError("export_storage_unsafe") from None
        return digest.hexdigest()

    def _verify_source_candidate(self, directory, candidate, lease):
        expected = closed_file_receipt(candidate)
        if expected is None or expected.sha256 is None:
            raise ExportError("export_storage_unsafe", lease)
        flags = os.O_RDONLY | os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        descriptor = None
        try:
            descriptor = os.open(
                ARCHIVE_PART_NAME, flags, dir_fd=directory.descriptor,
            )
            expected.verify_identity(descriptor)
            digest = self._hash_descriptor(descriptor)
            expected.verify_identity(descriptor)
            named = os.stat(
                ARCHIVE_PART_NAME,
                dir_fd=directory.descriptor,
                follow_symlinks=False,
            )
            if not expected.matches_stat(named) or digest != expected.sha256:
                raise ExportError("export_storage_unsafe", lease)
        except ExportStorageError as error:
            raise ExportError(error.code, lease) from None
        except OSError:
            raise ExportError("export_storage_unsafe", lease) from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
        return expected

    def _capture_ready_candidate(self, directory, name, candidate, lease):
        expected = closed_file_receipt(candidate)
        if expected is None or expected.sha256 is None:
            raise ExportError("export_storage_unsafe", lease)
        flags = os.O_RDONLY | os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        descriptor = None
        try:
            descriptor = os.open(name, flags, dir_fd=directory.descriptor)
            opened = OpenFileReceipt.from_fd(
                descriptor, directory.uid, directory.gid,
            )
            before = ClosedFileReceipt.from_open_fd(descriptor, opened)
            if (
                before.dev != expected.dev
                or before.ino != expected.ino
                or before.size != expected.size
                or before.mtime_ns != expected.mtime_ns
            ):
                raise ExportError("export_storage_unsafe", lease)
            digest = self._hash_descriptor(descriptor)
            moved = ClosedFileReceipt.from_open_fd(
                descriptor, opened, digest,
            )
            named = os.stat(
                name, dir_fd=directory.descriptor, follow_symlinks=False,
            )
            if (
                before != ClosedFileReceipt(
                    moved.dev, moved.ino, moved.uid, moved.gid,
                    moved.mode, moved.nlink, moved.size,
                    moved.mtime_ns, moved.ctime_ns,
                )
                or not moved.matches_stat(named)
                or digest != expected.sha256
            ):
                raise ExportError("export_storage_unsafe", lease)
            os.fsync(descriptor)
            return moved
        except ExportStorageError as error:
            raise ExportError(error.code, lease) from None
        except OSError:
            raise ExportError("export_storage_unsafe", lease) from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _prepare_ready_candidate(self, job, attempt, attempt_file, lease,
                                 heartbeat, stop_requested):
        self._stop(stop_requested, lease)
        relative_path = "ready/{}.zip".format(job.pk)
        with heartbeat.foreground_write_guard():
            with transaction.atomic(using=self.using):
                current = lock_current_lease(lease, using=self.using)
                current_file = ExportAttemptFile.objects.using(
                    self.using,
                ).select_for_update().get(pk=attempt_file.pk)
                if current.state != "verifying" or current_file.state != "verifying":
                    raise LeaseLost()
                current_file.state = "publishing"
                current_file.intent_relative_path = relative_path
                current_file.save(update_fields=(
                    "state", "intent_relative_path",
                ))
        self._fault("after_publishing_intent", attempt_file=attempt_file)
        self._resolve_quarantine(job, attempt, lease, heartbeat)
        root = open_export_root(
            settings.PINRY_EXPORT_ROOT, os.getuid(), os.getgid(),
        )
        staging = source = ready = None
        try:
            staging = open_staging_directory(root)
            source = open_receipted_directory(
                staging, attempt.relative_path,
                self._attempt_directory_receipt(attempt),
            )
            ready = self._open_ready_directory(root)
            self._verify_source_candidate(source, attempt_file, lease)
            rename_noreplace(
                source, ARCHIVE_PART_NAME, ready, "{}.zip".format(job.pk),
            )
            moved = self._capture_ready_candidate(
                ready, "{}.zip".format(job.pk), attempt_file, lease,
            )
        except FileExistsError:
            raise ExportError("export_storage_unsafe", lease) from None
        finally:
            for directory in (ready, source, staging, root):
                if directory is not None:
                    directory.close()
        self._fault("after_candidate_rename", attempt_file=attempt_file)
        with heartbeat.foreground_write_guard():
            with transaction.atomic(using=self.using):
                lock_current_lease(lease, using=self.using)
                current_file = ExportAttemptFile.objects.using(
                    self.using,
                ).select_for_update().get(pk=attempt_file.pk)
                if (
                    current_file.state != "publishing"
                    or current_file.intent_relative_path != relative_path
                ):
                    raise LeaseLost()
                current_file.state = "ready_candidate"
                current_file.relative_path = relative_path
                current_file.intent_relative_path = None
                self._set_receipt(current_file, moved)
                current_file.save()
        self._fault("after_ready_candidate", attempt_file=current_file)
        return current_file

    def _resolve_quarantine(self, job, attempt, lease, heartbeat):
        quarantine = attempt.files.filter(kind="quarantine").first()
        if quarantine is None:
            root = open_export_root(
                settings.PINRY_EXPORT_ROOT, os.getuid(), os.getgid(),
            )
            ready = None
            descriptor = None
            try:
                ready = self._open_ready_directory(root)
                try:
                    flags = os.O_RDONLY | os.O_NOFOLLOW
                    descriptor = os.open(
                        "{}.zip".format(job.pk), flags,
                        dir_fd=ready.descriptor,
                    )
                except FileNotFoundError:
                    return
                receipt = OpenFileReceipt.from_fd(
                    descriptor, ready.uid, ready.gid,
                )
                archive = attempt.files.get(kind="archive")
                if (
                    receipt.dev == archive.receipt_dev
                    and receipt.ino == archive.receipt_ino
                ):
                    return
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                if ready is not None:
                    ready.close()
                root.close()
            relative = "ready/{}.zip".format(job.pk)
            intent = "{}/quarantine-ready-{}.zip".format(
                attempt.relative_path, attempt.attempt_generation,
            )
            with heartbeat.foreground_write_guard():
                with transaction.atomic(using=self.using):
                    lock_current_lease(lease, using=self.using)
                    quarantine = ExportAttemptFile.objects.using(
                        self.using,
                    ).create(
                        attempt_id=attempt.pk, kind="quarantine",
                        state="writing", receipt_level="open",
                        relative_path=relative,
                        intent_relative_path=intent,
                        receipt_dev=receipt.dev, receipt_ino=receipt.ino,
                        receipt_uid=receipt.uid, receipt_gid=receipt.gid,
                        receipt_mode=receipt.mode,
                        receipt_nlink=receipt.nlink,
                    )
        if quarantine.state == "closed":
            self._fault("after_quarantine_closed", quarantine=quarantine)
            return
        root = open_export_root(
            settings.PINRY_EXPORT_ROOT, os.getuid(), os.getgid(),
        )
        staging = directory = ready = None
        descriptor = None
        try:
            staging = open_staging_directory(root)
            directory = open_receipted_directory(
                staging, attempt.relative_path,
                self._attempt_directory_receipt(attempt),
            )
            ready = self._open_ready_directory(root)
            leaf = os.path.basename(quarantine.intent_relative_path)
            source_receipt = OpenFileReceipt(
                quarantine.receipt_dev, quarantine.receipt_ino,
                quarantine.receipt_uid, quarantine.receipt_gid,
                quarantine.receipt_mode, quarantine.receipt_nlink,
            )

            def matches(parent, name):
                try:
                    current = os.stat(
                        name, dir_fd=parent.descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    return False
                return source_receipt.matches_stat(current)
            at_source = matches(ready, "{}.zip".format(job.pk))
            at_intent = matches(directory, leaf)
            if at_source == at_intent:
                raise ExportError("export_storage_unsafe", lease)
            if at_source:
                rename_noreplace(
                    ready, "{}.zip".format(job.pk), directory, leaf,
                )
            flags = os.O_RDONLY | os.O_NOFOLLOW
            descriptor = os.open(leaf, flags, dir_fd=directory.descriptor)
            opened = OpenFileReceipt.from_fd(
                descriptor, directory.uid, directory.gid,
            )
            closed = ClosedFileReceipt.from_open_fd(descriptor, opened)
            self._fault("after_quarantine_move", quarantine=quarantine)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            for current in (ready, directory, staging, root):
                if current is not None:
                    current.close()
        with heartbeat.foreground_write_guard():
            with transaction.atomic(using=self.using):
                lock_current_lease(lease, using=self.using)
                current = ExportAttemptFile.objects.using(
                    self.using,
                ).select_for_update().get(pk=quarantine.pk)
                if current.state != "writing":
                    raise LeaseLost()
                current.state = "closed"
                current.receipt_level = "full"
                current.relative_path = current.intent_relative_path
                current.intent_relative_path = None
                self._set_receipt(current, closed)
                current.save()
        self._fault("after_quarantine_closed", quarantine=current)

    def _complete_locked(self, current, attempt, candidate, exported_at):
        if (
            current.state != "verifying"
            or attempt.state != "verifying"
            or candidate.state != "ready_candidate"
            or candidate.receipt_size != current.verifying_size
            or candidate.receipt_sha256 != current.verifying_sha256
            or attempt.exported_at != exported_at
        ):
            raise LeaseLost()
        current.state = "complete"
        current.completed_at = exported_at
        current.expires_at = exported_at + timedelta(hours=24)
        current.ready_cleanup_state = "retained"
        current.ready_relative_path = candidate.relative_path
        current.ready_display_name = archive_display_name(
            current.scope, current.board_name_snapshot, exported_at,
        )
        current.ready_size = candidate.receipt_size
        current.ready_sha256 = candidate.receipt_sha256
        current.ready_dev = candidate.receipt_dev
        current.ready_ino = candidate.receipt_ino
        current.ready_uid = candidate.receipt_uid
        current.ready_gid = candidate.receipt_gid
        current.ready_mode = candidate.receipt_mode
        current.ready_nlink = candidate.receipt_nlink
        current.ready_mtime_ns = candidate.receipt_mtime_ns
        current.ready_ctime_ns = candidate.receipt_ctime_ns
        current.archive_done = current.archive_total
        current.bytes_done = current.bytes_total
        current.save()
        self._expire_previous_complete_locked(current)
        candidate.state = "published"
        candidate.save(update_fields=("state",))
        attempt.state = "published"
        attempt.save(update_fields=("state",))
        ExportSlot.objects.using(self.using).filter(
            current_job=current,
        ).update(current_job=None)

    def _expire_previous_complete_locked(self, current):
        previous_jobs = ExportJob.objects.using(self.using).filter(
            owner_id=current.owner_id, state="complete",
        ).exclude(pk=current.pk).select_for_update()
        for previous in previous_jobs:
            previous.state = "expired"
            previous.ready_cleanup_state = "pending"
            previous.save(update_fields=(
                "state", "ready_cleanup_state",
            ))

    def _record_owner_deleted_locked(self, current, attempt, candidate, lease):
        if (
            current.state != "verifying"
            or attempt.state != "verifying"
            or candidate.state != "ready_candidate"
        ):
            raise LeaseLost()
        failure = ExportError("permission_changed", lease)
        current.state = "failed"
        current.error_code = failure.code
        current.error_class = failure.error_class
        current.error_retryable = failure.retryable
        current.staging_cleanup_state = "pending"
        current.lease_uuid = None
        current.lease_expires_at = None
        current.save(update_fields=(
            "state", "error_code", "error_class", "error_retryable",
            "staging_cleanup_state", "lease_uuid", "lease_expires_at",
        ))
        candidate.state = "retiring"
        candidate.save(update_fields=("state",))
        attempt.state = "retiring"
        attempt.save(update_fields=("state",))
        ExportSlot.objects.using(self.using).filter(
            current_job=current,
        ).update(current_job=None)

    def _final_fence(self, attempt, candidate, exported_at, lease,
                     heartbeat, stop_requested):
        while True:
            rotated = None
            owner_deleted = False
            try:
                with heartbeat.job_token_transition(lease) as rotation:
                    with database_write_fence(
                        using=self.using, models=self.FENCE_MODELS,
                    ):
                        current = lock_current_lease(lease, using=self.using)
                        deadline = DatabaseFenceDeadline(self.monotonic)
                        locked_attempt = ExportAttempt.objects.using(
                            self.using,
                        ).select_for_update().get(pk=attempt.pk)
                        locked_file = ExportAttemptFile.objects.using(
                            self.using,
                        ).select_for_update().get(pk=candidate.pk)
                        if (
                            current.owner_id is None
                            or not User.objects.using(self.using).filter(
                                pk=current.owner_id,
                            ).exists()
                        ):
                            self._record_owner_deleted_locked(
                                current, locked_attempt, locked_file, lease,
                            )
                            owner_deleted = True
                        else:
                            revoked = revoked_item_ids(
                                current, inclusion_state="included",
                                using=self.using, heartbeat=None,
                                stop_requested=None,
                                checkpoint=deadline.checkpoint, lease=lease,
                            )
                            if revoked:
                                rotated = self._rotate_locked(
                                    current, locked_attempt, locked_file,
                                    revoked, lease, deadline.checkpoint,
                                )
                            else:
                                self._complete_locked(
                                    current, locked_attempt, locked_file,
                                    exported_at,
                                )
                        deadline.checkpoint()
                    if owner_deleted or rotated is None:
                        rotation.clear()
                    else:
                        rotation.replace(rotated)
                if owner_deleted:
                    return OWNER_DELETED
                if rotated is None:
                    self._fault(
                        "after_complete_commit",
                        lease=lease,
                        attempt=attempt,
                        attempt_file=candidate,
                    )
                return rotated
            except DatabaseFenceBusy:
                self._stop(stop_requested, lease)
                heartbeat()
                self.sleeper(0.01)

    def _cleanup_checkpointer(self, lease, heartbeat, stop_requested):
        renewed_at = [self.monotonic()]

        def checkpoint():
            self._stop(stop_requested, lease)
            now = self.monotonic()
            if now - renewed_at[0] >= CLEANUP_HEARTBEAT_INTERVAL:
                heartbeat.renew_now(lease)
                renewed_at[0] = self.monotonic()

        return checkpoint

    def _cleanup_fault(self, point, checkpoint, **context):
        checkpoint()
        context["checkpoint"] = checkpoint
        self._fault(point, **context)
        checkpoint()

    @staticmethod
    def _attempt_file_cleanup_snapshot(attempt_file):
        return (
            attempt_file.kind,
            attempt_file.state,
            attempt_file.receipt_level,
            attempt_file.relative_path,
            attempt_file.intent_relative_path,
            attempt_file.receipt_dev,
            attempt_file.receipt_ino,
            attempt_file.receipt_uid,
            attempt_file.receipt_gid,
            attempt_file.receipt_mode,
            attempt_file.receipt_nlink,
            attempt_file.receipt_size,
            attempt_file.receipt_mtime_ns,
            attempt_file.receipt_ctime_ns,
            attempt_file.receipt_sha256,
        )

    @staticmethod
    def _blob_cleanup_snapshot(blob):
        return (
            blob.cleanup_state,
            blob.file_state,
            blob.snapshot_generation,
            blob.snapshot_relative_path,
            blob.confirmed,
            blob.receipt_dev,
            blob.receipt_ino,
            blob.receipt_uid,
            blob.receipt_gid,
            blob.receipt_mode,
            blob.receipt_nlink,
            blob.size,
            blob.receipt_mtime_ns,
            blob.receipt_ctime_ns,
            blob.receipt_sha256,
        )

    @staticmethod
    def _snapshot_cleanup_snapshot(job):
        return (
            job.snapshot_generation,
            job.snapshot_relative_path,
            job.snapshot_dir_dev,
            job.snapshot_dir_ino,
            job.snapshot_dir_uid,
            job.snapshot_dir_gid,
            job.snapshot_dir_mode,
        )

    @staticmethod
    def _attempt_cleanup_snapshot(attempt):
        return (
            attempt.job_id,
            attempt.attempt_generation,
            attempt.lease_uuid,
            attempt.state,
            attempt.relative_path,
            attempt.dir_dev,
            attempt.dir_ino,
            attempt.dir_uid,
            attempt.dir_gid,
            attempt.dir_mode,
            attempt.exported_at,
        )

    def _retiring_cleanup_state(self, attempt, lease, heartbeat):
        with heartbeat.foreground_write_guard():
            with transaction.atomic(using=self.using):
                lock_current_lease(lease, using=self.using)
                current_attempt = ExportAttempt.objects.using(
                    self.using,
                ).select_for_update().get(pk=attempt.pk)
                if current_attempt.state not in ("retiring", "cleaned"):
                    raise LeaseLost()
                current_file = current_attempt.files.using(
                    self.using,
                ).select_for_update().exclude(
                    state="cleaned",
                ).order_by("kind", "pk").first()
                if current_file is not None and not (
                    (
                        current_file.kind == "archive"
                        and current_file.state == "retiring"
                    )
                    or (
                        current_file.kind == "quarantine"
                        and current_file.state == "closed"
                    )
                ):
                    raise ExportStorageError("export_storage_unsafe")
                return current_attempt, current_file

    def _remove_retiring_file_fs(
        self, attempt, attempt_file, checkpoint,
    ):
        root = None
        staging = directory = ready = None
        try:
            checkpoint()
            root = open_export_root(
                settings.PINRY_EXPORT_ROOT, os.getuid(), os.getgid(),
            )
            checkpoint()
            receipt = attempt_file_cleanup_receipt(attempt_file)
            ready_path = "ready/{}.zip".format(attempt.job_id)
            staging_path = "{}/{}".format(
                attempt.relative_path, ARCHIVE_PART_NAME,
            )
            quarantine_path = "{}/quarantine-ready-{}.zip".format(
                attempt.relative_path, attempt.attempt_generation,
            )
            if (
                attempt_file.kind == "archive"
                and attempt_file.relative_path == ready_path
            ):
                ready = self._open_ready_directory(root)
                self._cleanup_fault(
                    "before_cleanup_unlink", checkpoint,
                    attempt=attempt, attempt_file=attempt_file,
                )
                remove_if_receipt_matches(
                    ready, os.path.basename(attempt_file.relative_path), receipt,
                )
                self._cleanup_fault(
                    "after_cleanup_parent_fsync", checkpoint,
                    attempt=attempt, attempt_file=attempt_file,
                )
            elif (
                (
                    attempt_file.kind == "archive"
                    and attempt_file.relative_path == staging_path
                )
                or (
                    attempt_file.kind == "quarantine"
                    and attempt_file.relative_path == quarantine_path
                )
            ):
                staging = open_staging_directory(root)
                directory = open_receipted_directory(
                    staging, attempt.relative_path,
                    self._attempt_directory_receipt(attempt),
                )
                leaf = os.path.basename(attempt_file.relative_path)
                self._cleanup_fault(
                    "before_cleanup_unlink", checkpoint,
                    attempt=attempt, attempt_file=attempt_file,
                )
                remove_if_receipt_matches(
                    directory, leaf, receipt,
                )
                self._cleanup_fault(
                    "after_cleanup_parent_fsync", checkpoint,
                    attempt=attempt, attempt_file=attempt_file,
                )
            else:
                raise ExportStorageError("export_storage_unsafe")
        finally:
            for current in (ready, directory, staging, root):
                if current is not None:
                    current.close()

    def _remove_retiring_directory_fs(
        self, attempt, checkpoint, completion_fault=False,
    ):
        expected_name = "attempt-{}-{}".format(
            attempt.job_id, attempt.attempt_generation,
        )
        if attempt.relative_path != expected_name:
            raise ExportStorageError("export_storage_unsafe")
        root = None
        staging = directory = None
        try:
            checkpoint()
            root = open_export_root(
                settings.PINRY_EXPORT_ROOT, os.getuid(), os.getgid(),
            )
            checkpoint()
            staging = open_staging_directory(root)
            checkpoint()
            try:
                named = os.stat(
                    expected_name,
                    dir_fd=staging.descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                staging.verify_identity()
                checkpoint()
                return False
            receipt = self._attempt_directory_receipt(attempt)
            if not receipt.matches_stat(named):
                raise ExportStorageError("export_storage_unsafe")
            directory = open_receipted_directory(
                staging, expected_name, receipt,
            )
            if os.listdir(directory.descriptor):
                raise ExportStorageError("export_storage_unsafe")
            directory.verify_identity()
            if os.listdir(directory.descriptor):
                raise ExportStorageError("export_storage_unsafe")
            self._cleanup_fault(
                "before_cleanup_unlink", checkpoint,
                attempt=attempt, attempt_file=None,
            )
            os.rmdir(expected_name, dir_fd=staging.descriptor)
            self._cleanup_fault(
                "after_cleanup_unlink", checkpoint,
                attempt=attempt, attempt_file=None,
            )
            self._cleanup_fault(
                "before_cleanup_parent_fsync", checkpoint,
                attempt=attempt, attempt_file=None,
            )
            os.fsync(staging.descriptor)
            self._cleanup_fault(
                "after_cleanup_parent_fsync", checkpoint,
                attempt=attempt, attempt_file=None,
            )
            if completion_fault:
                self._cleanup_fault(
                    "after_complete_attempt_rmdir", checkpoint,
                    attempt=attempt, attempt_file=None,
                )
            return True
        except ExportStorageError:
            raise
        except OSError:
            raise ExportStorageError("export_storage_unsafe") from None
        finally:
            for current in (directory, staging, root):
                if current is not None:
                    current.close()

    def _mark_attempt_file_cleaned(
        self, attempt, attempt_file, lease, heartbeat,
        attempt_state="retiring",
    ):
        expected = self._attempt_file_cleanup_snapshot(attempt_file)
        with heartbeat.foreground_write_guard():
            with transaction.atomic(using=self.using):
                lock_current_lease(lease, using=self.using)
                current_attempt = ExportAttempt.objects.using(
                    self.using,
                ).select_for_update().get(pk=attempt.pk)
                current_file = ExportAttemptFile.objects.using(
                    self.using,
                ).select_for_update().get(pk=attempt_file.pk)
                if (
                    current_attempt.state != attempt_state
                    or current_file.attempt_id != current_attempt.pk
                    or self._attempt_file_cleanup_snapshot(current_file)
                    != expected
                ):
                    raise LeaseLost()
                current_file.state = "cleaned"
                current_file.intent_relative_path = None
                current_file.save(update_fields=(
                    "state", "intent_relative_path",
                ))

    def _cleanup_retiring(
        self, attempt, attempt_file, lease, heartbeat,
        stop_requested=None,
    ):
        del attempt_file
        checkpoint = self._cleanup_checkpointer(
            lease, heartbeat, stop_requested,
        )
        while True:
            checkpoint()
            current_attempt, current_file = self._retiring_cleanup_state(
                attempt, lease, heartbeat,
            )
            checkpoint()
            if current_attempt.state == "cleaned":
                return
            if current_file is None:
                break
            self._remove_retiring_file_fs(
                current_attempt, current_file, checkpoint,
            )
            checkpoint()
            self._mark_attempt_file_cleaned(
                current_attempt, current_file, lease, heartbeat,
            )
            checkpoint()
        current_attempt, current_file = self._retiring_cleanup_state(
            attempt, lease, heartbeat,
        )
        if current_attempt.state == "cleaned":
            return
        if current_file is not None:
            raise LeaseLost()
        self._remove_retiring_directory_fs(current_attempt, checkpoint)
        checkpoint()
        with heartbeat.foreground_write_guard():
            with transaction.atomic(using=self.using):
                lock_current_lease(lease, using=self.using)
                current_attempt = ExportAttempt.objects.using(
                    self.using,
                ).select_for_update().get(pk=attempt.pk)
                if (
                    current_attempt.state != "retiring"
                    or current_attempt.files.exclude(state="cleaned").exists()
                ):
                    raise LeaseLost()
                current_attempt.state = "cleaned"
                current_attempt.save(update_fields=("state",))

    def _terminal_retiring_state(
        self, attempt, worker_token, heartbeat,
    ):
        with heartbeat.foreground_write_guard():
            with transaction.atomic(using=self.using):
                lock_current_worker_lease(
                    worker_token, using=self.using,
                )
                current_job = ExportJob.objects.using(
                    self.using,
                ).select_for_update().get(pk=attempt.job_id)
                current_attempt = ExportAttempt.objects.using(
                    self.using,
                ).select_for_update().get(pk=attempt.pk)
                if (
                    current_job.state != "failed"
                    or current_job.error_code != "permission_changed"
                    or current_job.lease_uuid is not None
                    or current_attempt.state not in ("retiring", "cleaned")
                ):
                    raise LeaseLost()
                current_file = current_attempt.files.using(
                    self.using,
                ).select_for_update().exclude(
                    state="cleaned",
                ).order_by("kind", "pk").first()
                if current_file is not None and not (
                    (
                        current_file.kind == "archive"
                        and current_file.state == "retiring"
                    )
                    or (
                        current_file.kind == "quarantine"
                        and current_file.state == "closed"
                    )
                ):
                    raise ExportStorageError("export_storage_unsafe")
                return current_attempt, current_file

    def _mark_terminal_file_cleaned(
        self, attempt, attempt_file, worker_token, heartbeat,
    ):
        expected = self._attempt_file_cleanup_snapshot(attempt_file)
        with heartbeat.foreground_write_guard():
            with transaction.atomic(using=self.using):
                lock_current_worker_lease(
                    worker_token, using=self.using,
                )
                current_job = ExportJob.objects.using(
                    self.using,
                ).select_for_update().get(pk=attempt.job_id)
                current_attempt = ExportAttempt.objects.using(
                    self.using,
                ).select_for_update().get(pk=attempt.pk)
                current_file = ExportAttemptFile.objects.using(
                    self.using,
                ).select_for_update().get(pk=attempt_file.pk)
                if (
                    current_job.state != "failed"
                    or current_job.error_code != "permission_changed"
                    or current_job.lease_uuid is not None
                    or current_attempt.state != "retiring"
                    or self._attempt_file_cleanup_snapshot(current_file)
                    != expected
                ):
                    raise LeaseLost()
                current_file.state = "cleaned"
                current_file.intent_relative_path = None
                current_file.save(update_fields=(
                    "state", "intent_relative_path",
                ))

    def _cleanup_terminal_retiring(
        self, attempt, lease, heartbeat, stop_requested,
    ):
        worker_token = WorkerLeaseToken(
            lease.worker_generation, lease.worker_lease_uuid,
        )

        def checkpoint():
            self._stop(stop_requested, lease)

        while True:
            checkpoint()
            current_attempt, current_file = self._terminal_retiring_state(
                attempt, worker_token, heartbeat,
            )
            if current_attempt.state == "cleaned":
                return
            if current_file is None:
                break
            self._remove_retiring_file_fs(
                current_attempt, current_file, checkpoint,
            )
            self._mark_terminal_file_cleaned(
                current_attempt, current_file, worker_token, heartbeat,
            )
        self._remove_retiring_directory_fs(
            current_attempt, checkpoint,
        )
        with heartbeat.foreground_write_guard():
            with transaction.atomic(using=self.using):
                lock_current_worker_lease(
                    worker_token, using=self.using,
                )
                current_job = ExportJob.objects.using(
                    self.using,
                ).select_for_update().get(pk=attempt.job_id)
                current_attempt = ExportAttempt.objects.using(
                    self.using,
                ).select_for_update().get(pk=attempt.pk)
                if (
                    current_job.state != "failed"
                    or current_job.error_code != "permission_changed"
                    or current_job.lease_uuid is not None
                    or current_attempt.state != "retiring"
                    or current_attempt.files.exclude(state="cleaned").exists()
                ):
                    raise LeaseLost()
                current_attempt.state = "cleaned"
                current_attempt.save(update_fields=("state",))

    def _excluded_blob_cleanup_batch(
        self, lease, heartbeat, last_pk,
    ):
        with heartbeat.foreground_write_guard():
            with transaction.atomic(using=self.using):
                current = lock_current_lease(lease, using=self.using)
                blobs = current.blobs.using(self.using).select_for_update().filter(
                    cleanup_state="pending",
                ).exclude(
                    items__inclusion_state="included",
                ).order_by("pk")
                if last_pk is not None:
                    blobs = blobs.filter(pk__gt=last_pk)
                return list(blobs[:QUERY_CHUNK_SIZE])

    def _mark_blob_cleanup_batch(
        self, lease, heartbeat, blobs, excluded_only=True,
    ):
        blob_ids = [blob.pk for blob in blobs]
        expected = {
            blob.pk: self._blob_cleanup_snapshot(blob)
            for blob in blobs
        }
        with heartbeat.foreground_write_guard():
            with transaction.atomic(using=self.using):
                current = lock_current_lease(lease, using=self.using)
                locked_query = ExportBlob.objects.using(
                    self.using,
                ).select_for_update().filter(
                    job=current,
                    pk__in=blob_ids,
                    cleanup_state="pending",
                )
                if excluded_only:
                    locked_query = locked_query.exclude(
                        items__inclusion_state="included",
                    )
                locked = list(locked_query.order_by("pk"))
                if (
                    len(locked) != len(blobs)
                    or any(
                        self._blob_cleanup_snapshot(blob)
                        != expected.get(blob.pk)
                        for blob in locked
                    )
                ):
                    raise ExportStorageError("export_storage_unsafe")
                update_query = ExportBlob.objects.using(self.using).filter(
                    job=current,
                    pk__in=blob_ids,
                    cleanup_state="pending",
                )
                if excluded_only:
                    update_query = update_query.exclude(
                        items__inclusion_state="included",
                    )
                updated = update_query.update(cleanup_state="cleaned")
                if updated != len(blob_ids):
                    raise ExportStorageError("export_storage_unsafe")

    def _cleanup_excluded_blobs(
        self, lease, heartbeat, stop_requested=None,
    ):
        job = ExportJob.objects.using(self.using).get(pk=lease.job_id)
        checkpoint = self._cleanup_checkpointer(
            lease, heartbeat, stop_requested,
        )
        last_pk = None
        root = None
        staging = snapshot = None
        try:
            while True:
                checkpoint()
                blobs = self._excluded_blob_cleanup_batch(
                    lease, heartbeat, last_pk,
                )
                checkpoint()
                if not blobs:
                    return
                if root is None:
                    root = open_export_root(
                        settings.PINRY_EXPORT_ROOT,
                        os.getuid(),
                        os.getgid(),
                    )
                    staging = open_staging_directory(root)
                    snapshot = open_receipted_directory(
                        staging, job.snapshot_relative_path,
                        snapshot_directory_receipt(job),
                    )
                    checkpoint()
                for blob in blobs:
                    if (
                        blob.cleanup_state != "pending"
                        or blob.file_state != "closed"
                        or not blob.confirmed
                        or blob.snapshot_relative_path is None
                    ):
                        raise ExportStorageError("export_storage_unsafe")
                    self._cleanup_fault(
                        "before_cleanup_unlink", checkpoint,
                        blob=blob, lease=lease,
                    )
                    remove_if_receipt_matches(
                        snapshot, snapshot_blob_name(blob.pk),
                        self._blob_receipt(blob),
                    )
                    self._cleanup_fault(
                        "after_cleanup_parent_fsync", checkpoint,
                        blob=blob, lease=lease,
                    )
                checkpoint()
                self._mark_blob_cleanup_batch(
                    lease, heartbeat, blobs,
                )
                checkpoint()
                last_pk = blobs[-1].pk
        finally:
            for directory in (snapshot, staging, root):
                if directory is not None:
                    directory.close()

    def _complete_cleanup_context(self, lease, heartbeat):
        with heartbeat.foreground_write_guard():
            with transaction.atomic(using=self.using):
                current = lock_current_lease(lease, using=self.using)
                if (
                    current.state != "complete"
                    or current.ready_cleanup_state != "retained"
                    or current.staging_cleanup_state
                    not in ("pending", "blocked", "cleaned")
                ):
                    raise LeaseLost()
                if current.staging_cleanup_state == "cleaned":
                    return current, None, None, None
                attempts = list(ExportAttempt.objects.using(
                    self.using,
                ).select_for_update().filter(
                    job=current,
                    attempt_generation=lease.attempt_generation,
                )[:2])
                if len(attempts) != 1:
                    raise ExportStorageError("export_storage_unsafe")
                attempt = attempts[0]
                files = list(ExportAttemptFile.objects.using(
                    self.using,
                ).select_for_update().filter(
                    attempt=attempt,
                ).order_by("kind", "pk")[:3])
                archives = [item for item in files if item.kind == "archive"]
                quarantines = [
                    item for item in files if item.kind == "quarantine"
                ]
                if (
                    len(files) != len(archives) + len(quarantines)
                    or len(archives) != 1
                    or len(quarantines) > 1
                    or attempt.state not in ("published", "cleaned")
                ):
                    raise ExportStorageError("export_storage_unsafe")
                archive = archives[0]
                quarantine = quarantines[0] if quarantines else None
                if attempt.state == "cleaned" and any(
                    item.state != "cleaned" for item in files
                ):
                    raise ExportStorageError("export_storage_unsafe")
                return current, attempt, archive, quarantine

    def _complete_quarantine_state(self, attempt, lease, heartbeat):
        with heartbeat.foreground_write_guard():
            with transaction.atomic(using=self.using):
                current = lock_current_lease(lease, using=self.using)
                current_attempt = ExportAttempt.objects.using(
                    self.using,
                ).select_for_update().get(pk=attempt.pk)
                if (
                    current.state != "complete"
                    or current.ready_cleanup_state != "retained"
                    or current.staging_cleanup_state == "cleaned"
                    or current_attempt.job_id != current.pk
                    or current_attempt.state != "published"
                ):
                    raise ExportStorageError("export_storage_unsafe")
                archives = list(current_attempt.files.using(
                    self.using,
                ).select_for_update().filter(kind="archive")[:2])
                quarantines = list(current_attempt.files.using(
                    self.using,
                ).select_for_update().filter(kind="quarantine")[:2])
                if (
                    len(archives) != 1
                    or archives[0].state != "cleaned"
                    or len(quarantines) > 1
                ):
                    raise ExportStorageError("export_storage_unsafe")
                if not quarantines or quarantines[0].state == "cleaned":
                    return current_attempt, None
                if quarantines[0].state != "closed":
                    raise ExportStorageError("export_storage_unsafe")
                return current_attempt, quarantines[0]

    def _cleanup_complete_quarantine(
        self, attempt, lease, heartbeat, checkpoint,
    ):
        checkpoint()
        current_attempt, quarantine = self._complete_quarantine_state(
            attempt, lease, heartbeat,
        )
        checkpoint()
        if quarantine is None:
            return
        self._remove_retiring_file_fs(
            current_attempt, quarantine, checkpoint,
        )
        self._cleanup_fault(
            "after_complete_quarantine_unlink", checkpoint,
            attempt=current_attempt, attempt_file=quarantine,
        )
        self._mark_attempt_file_cleaned(
            current_attempt, quarantine, lease, heartbeat,
            attempt_state="published",
        )
        self._cleanup_fault(
            "after_complete_quarantine_cas", checkpoint,
            attempt=current_attempt, attempt_file=quarantine,
        )

    def _complete_blob_cleanup_batch(
        self, lease, heartbeat, last_pk,
    ):
        with heartbeat.foreground_write_guard():
            with transaction.atomic(using=self.using):
                current = lock_current_lease(lease, using=self.using)
                if (
                    current.state != "complete"
                    or current.ready_cleanup_state != "retained"
                    or current.staging_cleanup_state == "cleaned"
                ):
                    raise ExportStorageError("export_storage_unsafe")
                blobs = current.blobs.using(
                    self.using,
                ).select_for_update().filter(
                    cleanup_state="pending",
                ).order_by("pk")
                if last_pk is not None:
                    blobs = blobs.filter(pk__gt=last_pk)
                return current, list(blobs[:QUERY_CHUNK_SIZE])

    def _cleanup_complete_blobs(
        self, lease, heartbeat, checkpoint,
    ):
        last_pk = None
        root = None
        staging = snapshot = None
        try:
            while True:
                checkpoint()
                current, blobs = self._complete_blob_cleanup_batch(
                    lease, heartbeat, last_pk,
                )
                checkpoint()
                if not blobs:
                    return
                if (
                    current.snapshot_relative_path is None
                    or snapshot_directory_receipt(current) is None
                ):
                    raise ExportStorageError("export_storage_unsafe")
                if root is None:
                    root = open_export_root(
                        settings.PINRY_EXPORT_ROOT,
                        os.getuid(),
                        os.getgid(),
                    )
                    staging = open_staging_directory(root)
                    snapshot = open_receipted_directory(
                        staging, current.snapshot_relative_path,
                        snapshot_directory_receipt(current),
                    )
                    checkpoint()
                for blob in blobs:
                    expected_path = "{}/{}".format(
                        current.snapshot_relative_path,
                        snapshot_blob_name(blob.pk),
                    )
                    if (
                        blob.cleanup_state != "pending"
                        or blob.file_state != "closed"
                        or not blob.confirmed
                        or blob.snapshot_generation
                        != current.snapshot_generation
                        or blob.snapshot_relative_path != expected_path
                    ):
                        raise ExportStorageError("export_storage_unsafe")
                    remove_if_receipt_matches(
                        snapshot, snapshot_blob_name(blob.pk),
                        self._blob_receipt(blob),
                    )
                    self._cleanup_fault(
                        "after_complete_blob_unlink", checkpoint,
                        blob=blob, lease=lease,
                    )
                self._mark_blob_cleanup_batch(
                    lease, heartbeat, blobs, excluded_only=False,
                )
                self._cleanup_fault(
                    "after_complete_blob_cas", checkpoint,
                    blobs=blobs, lease=lease,
                )
                last_pk = blobs[-1].pk
        finally:
            for directory in (snapshot, staging, root):
                if directory is not None:
                    directory.close()

    def _complete_snapshot_state(self, lease, heartbeat):
        with heartbeat.foreground_write_guard():
            with transaction.atomic(using=self.using):
                current = lock_current_lease(lease, using=self.using)
                if (
                    current.state != "complete"
                    or current.ready_cleanup_state != "retained"
                    or current.staging_cleanup_state == "cleaned"
                    or current.blobs.exclude(
                        cleanup_state="cleaned",
                    ).exists()
                ):
                    raise ExportStorageError("export_storage_unsafe")
                snapshot = self._snapshot_cleanup_snapshot(current)
                if all(value is None for value in snapshot):
                    return current, False
                if any(value is None for value in snapshot):
                    raise ExportStorageError("export_storage_unsafe")
                if current.snapshot_relative_path != snapshot_directory_name(
                    current.pk, current.snapshot_generation,
                ):
                    raise ExportStorageError("export_storage_unsafe")
                return current, True

    def _remove_complete_snapshot_directory_fs(
        self, job, checkpoint,
    ):
        expected_name = snapshot_directory_name(
            job.pk, job.snapshot_generation,
        )
        if job.snapshot_relative_path != expected_name:
            raise ExportStorageError("export_storage_unsafe")
        receipt = snapshot_directory_receipt(job)
        if receipt is None:
            raise ExportStorageError("export_storage_unsafe")
        root = None
        staging = snapshot = None
        try:
            checkpoint()
            root = open_export_root(
                settings.PINRY_EXPORT_ROOT, os.getuid(), os.getgid(),
            )
            checkpoint()
            staging = open_staging_directory(root)
            checkpoint()
            try:
                named = os.stat(
                    expected_name,
                    dir_fd=staging.descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                staging.verify_identity()
                checkpoint()
                return False
            if not receipt.matches_stat(named):
                raise ExportStorageError("export_storage_unsafe")
            snapshot = open_receipted_directory(
                staging, expected_name, receipt,
            )
            if os.listdir(snapshot.descriptor):
                raise ExportStorageError("export_storage_unsafe")
            snapshot.verify_identity()
            if os.listdir(snapshot.descriptor):
                raise ExportStorageError("export_storage_unsafe")
            os.rmdir(expected_name, dir_fd=staging.descriptor)
            checkpoint()
            os.fsync(staging.descriptor)
            self._cleanup_fault(
                "after_complete_snapshot_rmdir", checkpoint,
                job=job,
            )
            return True
        except ExportStorageError:
            raise
        except OSError:
            raise ExportStorageError("export_storage_unsafe") from None
        finally:
            for directory in (snapshot, staging, root):
                if directory is not None:
                    directory.close()

    def _mark_complete_snapshot_cleaned(
        self, job, lease, heartbeat,
    ):
        expected = self._snapshot_cleanup_snapshot(job)
        with heartbeat.foreground_write_guard():
            with transaction.atomic(using=self.using):
                current = lock_current_lease(lease, using=self.using)
                if (
                    current.state != "complete"
                    or current.ready_cleanup_state != "retained"
                    or current.staging_cleanup_state == "cleaned"
                    or self._snapshot_cleanup_snapshot(current) != expected
                    or current.blobs.exclude(
                        cleanup_state="cleaned",
                    ).exists()
                ):
                    raise ExportStorageError("export_storage_unsafe")
                current.snapshot_generation = None
                current.snapshot_relative_path = None
                current.snapshot_dir_dev = None
                current.snapshot_dir_ino = None
                current.snapshot_dir_uid = None
                current.snapshot_dir_gid = None
                current.snapshot_dir_mode = None
                current.save(update_fields=(
                    "snapshot_generation", "snapshot_relative_path",
                    "snapshot_dir_dev", "snapshot_dir_ino",
                    "snapshot_dir_uid", "snapshot_dir_gid",
                    "snapshot_dir_mode",
                ))

    def _cleanup_complete_snapshot(
        self, lease, heartbeat, checkpoint,
    ):
        checkpoint()
        current, needs_cleanup = self._complete_snapshot_state(
            lease, heartbeat,
        )
        checkpoint()
        if not needs_cleanup:
            return
        self._remove_complete_snapshot_directory_fs(current, checkpoint)
        self._mark_complete_snapshot_cleaned(
            current, lease, heartbeat,
        )
        self._cleanup_fault(
            "after_complete_snapshot_cas", checkpoint,
            job=current,
        )

    def _complete_attempt_state(self, attempt, lease, heartbeat):
        with heartbeat.foreground_write_guard():
            with transaction.atomic(using=self.using):
                current = lock_current_lease(lease, using=self.using)
                current_attempt = ExportAttempt.objects.using(
                    self.using,
                ).select_for_update().get(pk=attempt.pk)
                if (
                    current.state != "complete"
                    or current.ready_cleanup_state != "retained"
                    or current.staging_cleanup_state == "cleaned"
                    or current_attempt.job_id != current.pk
                    or current_attempt.state not in ("published", "cleaned")
                    or current.blobs.exclude(
                        cleanup_state="cleaned",
                    ).exists()
                    or any(
                        value is not None
                        for value in self._snapshot_cleanup_snapshot(current)
                    )
                    or current_attempt.files.exclude(
                        state="cleaned",
                    ).exists()
                ):
                    raise ExportStorageError("export_storage_unsafe")
                return current_attempt

    def _mark_complete_attempt_cleaned(
        self, attempt, lease, heartbeat,
    ):
        expected = self._attempt_cleanup_snapshot(attempt)
        with heartbeat.foreground_write_guard():
            with transaction.atomic(using=self.using):
                current = lock_current_lease(lease, using=self.using)
                current_attempt = ExportAttempt.objects.using(
                    self.using,
                ).select_for_update().get(pk=attempt.pk)
                if (
                    current.state != "complete"
                    or current.ready_cleanup_state != "retained"
                    or current.staging_cleanup_state == "cleaned"
                    or current_attempt.job_id != current.pk
                    or current_attempt.state != "published"
                    or self._attempt_cleanup_snapshot(current_attempt)
                    != expected
                    or current_attempt.files.exclude(
                        state="cleaned",
                    ).exists()
                    or current.blobs.exclude(
                        cleanup_state="cleaned",
                    ).exists()
                    or any(
                        value is not None
                        for value in self._snapshot_cleanup_snapshot(current)
                    )
                ):
                    raise ExportStorageError("export_storage_unsafe")
                current_attempt.state = "cleaned"
                current_attempt.save(update_fields=("state",))

    def _cleanup_complete_attempt(
        self, attempt, lease, heartbeat, checkpoint,
    ):
        checkpoint()
        current_attempt = self._complete_attempt_state(
            attempt, lease, heartbeat,
        )
        checkpoint()
        if current_attempt.state == "cleaned":
            return
        self._remove_retiring_directory_fs(
            current_attempt, checkpoint, completion_fault=True,
        )
        self._mark_complete_attempt_cleaned(
            current_attempt, lease, heartbeat,
        )
        self._cleanup_fault(
            "after_complete_attempt_cas", checkpoint,
            attempt=current_attempt,
        )

    def _mark_complete_staging_cleaned(
        self, attempt, lease, heartbeat,
    ):
        with heartbeat.foreground_write_guard():
            with transaction.atomic(using=self.using):
                current = lock_current_lease(lease, using=self.using)
                current_attempt = ExportAttempt.objects.using(
                    self.using,
                ).select_for_update().get(pk=attempt.pk)
                if (
                    current.state != "complete"
                    or current.ready_cleanup_state != "retained"
                    or current_attempt.job_id != current.pk
                    or current_attempt.state != "cleaned"
                    or current_attempt.files.exclude(
                        state="cleaned",
                    ).exists()
                    or current.blobs.exclude(
                        cleanup_state="cleaned",
                    ).exists()
                    or any(
                        value is not None
                        for value in self._snapshot_cleanup_snapshot(current)
                    )
                ):
                    raise ExportStorageError("export_storage_unsafe")
                if current.staging_cleanup_state != "cleaned":
                    current.staging_cleanup_state = "cleaned"
                    current.save(update_fields=("staging_cleanup_state",))
                return current

    def _mark_complete_cleanup_blocked(self, lease, heartbeat):
        with heartbeat.foreground_write_guard():
            with transaction.atomic(using=self.using):
                current = lock_current_lease(lease, using=self.using)
                if (
                    current.state != "complete"
                    or current.ready_cleanup_state != "retained"
                ):
                    raise LeaseLost()
                if current.staging_cleanup_state != "cleaned":
                    current.staging_cleanup_state = "blocked"
                    current.save(update_fields=("staging_cleanup_state",))

    @staticmethod
    def _ready_handoff_matches(job, attempt_file):
        return (
            job.ready_relative_path == "ready/{}.zip".format(job.pk)
            and attempt_file.kind == "archive"
            and attempt_file.receipt_level == "full"
            and attempt_file.relative_path == job.ready_relative_path
            and (
                attempt_file.receipt_dev,
                attempt_file.receipt_ino,
                attempt_file.receipt_uid,
                attempt_file.receipt_gid,
                attempt_file.receipt_mode,
                attempt_file.receipt_nlink,
                attempt_file.receipt_size,
                attempt_file.receipt_mtime_ns,
                attempt_file.receipt_ctime_ns,
                attempt_file.receipt_sha256,
            ) == (
                job.ready_dev,
                job.ready_ino,
                job.ready_uid,
                job.ready_gid,
                job.ready_mode,
                job.ready_nlink,
                job.ready_size,
                job.ready_mtime_ns,
                job.ready_ctime_ns,
                job.ready_sha256,
            )
        )

    def _handoff(self, attempt, candidate, lease, heartbeat, checkpoint):
        changed = False
        checkpoint()
        with heartbeat.foreground_write_guard():
            with transaction.atomic(using=self.using):
                job = lock_current_lease(lease, using=self.using)
                current_attempt = ExportAttempt.objects.using(
                    self.using,
                ).select_for_update().get(pk=attempt.pk)
                current_file = ExportAttemptFile.objects.using(
                    self.using,
                ).select_for_update().get(pk=candidate.pk)
                if (
                    job.state != "complete"
                    or job.ready_cleanup_state != "retained"
                    or job.staging_cleanup_state == "cleaned"
                    or current_attempt.job_id != job.pk
                    or current_attempt.state != "published"
                    or current_file.attempt_id != current_attempt.pk
                    or current_file.state not in ("published", "cleaned")
                    or not self._ready_handoff_matches(job, current_file)
                ):
                    raise ExportStorageError("export_storage_unsafe")
                if current_file.state == "published":
                    current_file.state = "cleaned"
                    current_file.intent_relative_path = None
                    current_file.save(update_fields=(
                        "state", "intent_relative_path",
                    ))
                    changed = True
        checkpoint()
        if changed:
            self._cleanup_fault(
                "after_complete_handoff", checkpoint,
                attempt=attempt, attempt_file=candidate,
            )

    def _mark_current_attempt_retiring(self, lease, heartbeat):
        with heartbeat.foreground_write_guard():
            with transaction.atomic(using=self.using):
                lock_current_lease(lease, using=self.using)
                attempt = ExportAttempt.objects.using(
                    self.using,
                ).select_for_update().filter(
                    job_id=lease.job_id,
                    attempt_generation=lease.attempt_generation,
                ).first()
                if attempt is None:
                    return
                archive = attempt.files.select_for_update().filter(
                    kind="archive",
                ).first()
                if archive is not None and archive.state in (
                    "writing", "closed", "verifying", "publishing",
                    "ready_candidate",
                ):
                    archive.state = "retiring"
                    archive.save(update_fields=("state",))
                if attempt.state in ("writing", "closed", "verifying"):
                    attempt.state = "retiring"
                    attempt.save(update_fields=("state",))

    def _archiving_recovery_state(self, lease, heartbeat):
        with heartbeat.foreground_write_guard():
            with transaction.atomic(using=self.using):
                current = lock_current_lease(lease, using=self.using)
                if current.state != "archiving":
                    return current, None, None
                attempt = ExportAttempt.objects.using(
                    self.using,
                ).select_for_update().filter(
                    job=current,
                    attempt_generation=lease.attempt_generation,
                ).first()
                if attempt is None:
                    return current, None, None
                expected_path = "attempt-{}-{}".format(
                    current.pk, lease.attempt_generation,
                )
                if attempt.relative_path != expected_path:
                    raise ExportError("export_storage_unsafe", lease)
                files = list(ExportAttemptFile.objects.using(
                    self.using,
                ).select_for_update().filter(
                    attempt=attempt,
                    kind="archive",
                )[:2])
                if len(files) > 1:
                    raise ExportError("export_storage_unsafe", lease)
                return current, attempt, files[0] if files else None

    def _rotate_recovered_attempt(
        self, attempt, lease, heartbeat,
    ):
        with heartbeat.job_token_transition(lease) as rotation:
            with heartbeat.foreground_write_guard():
                with transaction.atomic(using=self.using):
                    current = lock_current_lease(lease, using=self.using)
                    locked_attempt = ExportAttempt.objects.using(
                        self.using,
                    ).select_for_update().get(pk=attempt.pk)
                    if (
                        current.state != "archiving"
                        or current.attempt_generation
                        != locked_attempt.attempt_generation
                        or locked_attempt.state != "cleaned"
                    ):
                        raise LeaseLost()
                    current.attempt_generation += 1
                    current.archive_done = 0
                    current.bytes_done = 0
                    current.save(update_fields=(
                        "attempt_generation", "archive_done", "bytes_done",
                    ))
                    rotated = LeaseToken(
                        lease.worker_generation,
                        lease.worker_lease_uuid,
                        lease.job_id,
                        lease.job_lease_uuid,
                        current.attempt_generation,
                    )
            rotation.replace(rotated)
        return rotated

    def build_and_publish(self, job, lease, heartbeat, stop_requested):
        current_lease = lease
        try:
            while True:
                self._stop(stop_requested, current_lease)
                current = ExportJob.objects.using(self.using).get(
                    pk=current_lease.job_id,
                )
                current = self._initial_permission_check(
                    current, current_lease, heartbeat, stop_requested,
                )
                attempt, attempt_file, exported_at = self._build_and_verify_attempt(
                    current, current_lease, heartbeat, stop_requested,
                )
                rotated = self._post_build_revocations(
                    current, attempt, attempt_file, current_lease,
                    heartbeat, stop_requested,
                )
                if rotated is not None:
                    self._cleanup_retiring(
                        attempt, attempt_file, rotated, heartbeat,
                        stop_requested,
                    )
                    self._cleanup_excluded_blobs(
                        rotated, heartbeat, stop_requested,
                    )
                    current_lease = rotated
                    continue
                candidate = self._prepare_ready_candidate(
                    current, attempt, attempt_file, current_lease,
                    heartbeat, stop_requested,
                )
                rotated = self._final_fence(
                    attempt, candidate, exported_at, current_lease,
                    heartbeat, stop_requested,
                )
                if rotated is OWNER_DELETED:
                    self._cleanup_terminal_retiring(
                        attempt, current_lease, heartbeat, stop_requested,
                    )
                    return ExportJob.objects.using(self.using).get(
                        pk=current_lease.job_id,
                    )
                if rotated is not None:
                    self._cleanup_retiring(
                        attempt, candidate, rotated, heartbeat,
                        stop_requested,
                    )
                    self._cleanup_excluded_blobs(
                        rotated, heartbeat, stop_requested,
                    )
                    current_lease = rotated
                    continue
                return self.recover_complete(
                    current_lease, heartbeat, stop_requested,
                ).job
        except ExportStorageError as error:
            normalized = ExportError(error.code, current_lease)
            self._mark_current_attempt_retiring(
                current_lease, heartbeat,
            )
            raise normalized from None
        except ExportError:
            self._mark_current_attempt_retiring(current_lease, heartbeat)
            raise

    def recover_complete(self, lease, heartbeat, stop_requested):
        self._stop(stop_requested, lease)
        checkpoint = self._cleanup_checkpointer(
            lease, heartbeat, stop_requested,
        )
        try:
            checkpoint()
            current, attempt, archive, quarantine = (
                self._complete_cleanup_context(lease, heartbeat)
            )
            del quarantine
            checkpoint()
            if current.staging_cleanup_state == "cleaned":
                return RecoveryOutcome(current, lease)
            if attempt.state == "published":
                self._handoff(
                    attempt, archive, lease, heartbeat, checkpoint,
                )
                self._cleanup_complete_quarantine(
                    attempt, lease, heartbeat, checkpoint,
                )
            self._cleanup_complete_blobs(
                lease, heartbeat, checkpoint,
            )
            self._cleanup_complete_snapshot(
                lease, heartbeat, checkpoint,
            )
            self._cleanup_complete_attempt(
                attempt, lease, heartbeat, checkpoint,
            )
            completed = self._mark_complete_staging_cleaned(
                attempt, lease, heartbeat,
            )
            return RecoveryOutcome(completed, lease)
        except ExportStorageError as error:
            self._mark_complete_cleanup_blocked(lease, heartbeat)
            raise ExportError(error.code, lease) from None

    def recover_archiving(self, lease, heartbeat, stop_requested):
        self._stop(stop_requested, lease)
        try:
            current, attempt, attempt_file = self._retry_recovery_db(
                lambda: self._archiving_recovery_state(lease, heartbeat),
                lease,
                heartbeat,
                stop_requested,
            )
            if current.state != "archiving":
                return RecoveryOutcome(current, lease)
            if attempt is None:
                root = open_export_root(
                    settings.PINRY_EXPORT_ROOT,
                    os.getuid(),
                    os.getgid(),
                )
                staging = None
                try:
                    staging = open_staging_directory(root)
                    self.attempt_service.remove_unreceipted_directory_fs(
                        staging, current, lease,
                    )
                finally:
                    if staging is not None:
                        staging.close()
                    root.close()
                completed = self.build_and_publish(
                    current, lease, heartbeat, stop_requested,
                )
                return RecoveryOutcome(completed, lease)
            if attempt.state in ("writing", "closed"):
                self._retry_recovery_db(
                    lambda: self._mark_current_attempt_retiring(
                        lease, heartbeat,
                    ),
                    lease,
                    heartbeat,
                    stop_requested,
                )
                attempt.refresh_from_db()
                if attempt_file is not None:
                    attempt_file.refresh_from_db()
            elif attempt.state not in ("retiring", "cleaned"):
                raise ExportError("export_storage_unsafe", lease)
            if attempt.state == "retiring":
                self._retry_recovery_db(
                    lambda: self._cleanup_retiring(
                        attempt, attempt_file, lease, heartbeat,
                        stop_requested,
                    ),
                    lease,
                    heartbeat,
                    stop_requested,
                )
                attempt.refresh_from_db()
            if attempt.state != "cleaned":
                raise ExportError("export_storage_unsafe", lease)
            rotated = self._retry_recovery_db(
                lambda: self._rotate_recovered_attempt(
                    attempt, lease, heartbeat,
                ),
                lease,
                heartbeat,
                stop_requested,
            )
            current = ExportJob.objects.using(self.using).get(pk=lease.job_id)
            completed = self.build_and_publish(
                current, rotated, heartbeat, stop_requested,
            )
            return RecoveryOutcome(completed, rotated)
        except ExportStorageError as error:
            raise ExportError(error.code, lease) from None

    def recover_verifying(self, lease, heartbeat, stop_requested):
        current = ExportJob.objects.using(self.using).get(pk=lease.job_id)
        if current.state != "verifying":
            return RecoveryOutcome(current, lease)
        attempt = current.attempts.get(
            attempt_generation=lease.attempt_generation,
        )
        candidate = attempt.files.get(kind="archive")
        if candidate.state == "verifying":
            candidate = self._prepare_ready_candidate(
                current, attempt, candidate, lease,
                heartbeat, stop_requested,
            )
        elif candidate.state == "publishing":
            self._resolve_quarantine(
                current, attempt, lease, heartbeat,
            )
            candidate = self._recover_publishing(
                current, attempt, candidate, lease, heartbeat,
            )
        rotated = self._final_fence(
            attempt, candidate, attempt.exported_at, lease,
            heartbeat, stop_requested,
        )
        if rotated is OWNER_DELETED:
            self._cleanup_terminal_retiring(
                attempt, lease, heartbeat, stop_requested,
            )
            return RecoveryOutcome(
                ExportJob.objects.using(self.using).get(pk=lease.job_id),
                lease,
            )
        if rotated is None:
            return self.recover_complete(
                lease, heartbeat, stop_requested,
            )
        self._cleanup_retiring(
            attempt, candidate, rotated, heartbeat, stop_requested,
        )
        self._cleanup_excluded_blobs(
            rotated, heartbeat, stop_requested,
        )
        current = ExportJob.objects.using(self.using).get(pk=lease.job_id)
        completed = self.build_and_publish(
            current, rotated, heartbeat, stop_requested,
        )
        current_lease = LeaseToken(
            rotated.worker_generation,
            rotated.worker_lease_uuid,
            rotated.job_id,
            rotated.job_lease_uuid,
            completed.attempt_generation,
        )
        return RecoveryOutcome(completed, current_lease)

    def _recover_publishing(self, job, attempt, candidate, lease, heartbeat):
        expected = closed_file_receipt(candidate)
        if expected is None or expected.sha256 is None:
            raise ExportError("export_storage_unsafe", lease)
        source_receipt = OpenFileReceipt(
            expected.dev, expected.ino, expected.uid, expected.gid,
            expected.mode, expected.nlink,
        )
        root = open_export_root(
            settings.PINRY_EXPORT_ROOT, os.getuid(), os.getgid(),
        )
        staging = source = ready = None
        try:
            staging = open_staging_directory(root)
            source = open_receipted_directory(
                staging, attempt.relative_path,
                self._attempt_directory_receipt(attempt),
            )
            ready = self._open_ready_directory(root)
            source_stat = ready_stat = None
            try:
                source_stat = os.stat(
                    ARCHIVE_PART_NAME, dir_fd=source.descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            try:
                ready_stat = os.stat(
                    "{}.zip".format(job.pk), dir_fd=ready.descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            source_matches = (
                source_stat is not None
                and source_receipt.matches_stat(source_stat)
            )
            ready_matches = (
                ready_stat is not None
                and source_receipt.matches_stat(ready_stat)
            )
            if source_matches == ready_matches:
                raise ExportError("export_storage_unsafe", lease)
            if source_matches:
                self._verify_source_candidate(source, candidate, lease)
                rename_noreplace(
                    source, ARCHIVE_PART_NAME, ready,
                    "{}.zip".format(job.pk),
                )
            moved = self._capture_ready_candidate(
                ready, "{}.zip".format(job.pk), candidate, lease,
            )
        finally:
            for directory in (ready, source, staging, root):
                if directory is not None:
                    directory.close()
        with heartbeat.foreground_write_guard():
            with transaction.atomic(using=self.using):
                lock_current_lease(lease, using=self.using)
                current = ExportAttemptFile.objects.using(
                    self.using,
                ).select_for_update().get(pk=candidate.pk)
                if current.state != "publishing":
                    raise LeaseLost()
                current.state = "ready_candidate"
                current.relative_path = current.intent_relative_path
                current.intent_relative_path = None
                self._set_receipt(current, moved)
                current.save()
                return current

    def recover_retiring(self, lease, heartbeat, stop_requested):
        self._stop(stop_requested, lease)
        attempt = ExportAttempt.objects.using(self.using).filter(
            job_id=lease.job_id, state="retiring",
        ).order_by("attempt_generation").first()
        if attempt is not None:
            self._cleanup_retiring(
                attempt, attempt.files.get(kind="archive"), lease, heartbeat,
                stop_requested,
            )
        return RecoveryOutcome(
            ExportJob.objects.using(self.using).get(pk=lease.job_id), lease,
        )


__all__ = (
    "ArchiveService",
    "ArchiveValidationStopped",
    "RecoveryOutcome",
    "build_manifest",
    "validate_archive",
)
