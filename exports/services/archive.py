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
    lock_current_lease,
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
    snapshot_directory_receipt,
)


STREAM_CHUNK_SIZE = 1024 * 1024
QUERY_CHUNK_SIZE = 400
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

    @staticmethod
    def _chunks(values):
        values = list(values)
        for start in range(0, len(values), QUERY_CHUNK_SIZE):
            yield values[start:start + QUERY_CHUNK_SIZE]

    def _apply_revocations_locked(self, current, revoked):
        changed = 0
        for item_ids in self._chunks(revoked):
            changed += ExportItem.objects.using(self.using).filter(
                job=current, pk__in=item_ids, inclusion_state="included",
            ).update(
                inclusion_state="excluded",
                exclusion_reason="permission_revoked",
            )
        included = current.items.filter(inclusion_state="included")
        current.included_total = included.count()
        current.excluded_total = current.requested_total - current.included_total
        current.excluded_permission_revoked_total += changed
        current.archive_total = current.included_total
        current.bytes_total = sum(included.values_list("blob__size", flat=True))
        current.archive_done = 0
        current.bytes_done = 0
        return changed

    def _initial_permission_check(self, job, lease, heartbeat, stop_requested):
        revoked = revoked_item_ids(
            job, inclusion_state="included", using=self.using,
            heartbeat=heartbeat, stop_requested=stop_requested, lease=lease,
        )
        if revoked:
            with heartbeat.foreground_write_guard():
                with transaction.atomic(using=self.using):
                    current = lock_current_lease(lease, using=self.using)
                    if current.state != "archiving":
                        raise LeaseLost()
                    self._apply_revocations_locked(current, revoked)
                    current.save(update_fields=(
                        "included_total", "excluded_total",
                        "excluded_permission_revoked_total", "archive_total",
                        "bytes_total", "archive_done", "bytes_done",
                    ))
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
            attempt = self.attempt_service.record_directory_receipt_db_only(
                job, lease, attempt_directory, heartbeat,
            )
            descriptor, open_receipt = self.attempt_service.create_file_fs(
                attempt_directory,
            )
            attempt_file = self.attempt_service.record_open_file_receipt_db_only(
                attempt, lease, open_receipt, heartbeat,
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
            closed = self.attempt_service.close_file_fs(
                descriptor, open_receipt,
            )
            descriptor = None
            attempt_file = self.attempt_service.record_closed_file_receipt_db_only(
                attempt, attempt_file, closed, lease, heartbeat,
            )
            return attempt, attempt_file, tuple(expected_names), exported_at
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
            lease, heartbeat,
        )
        attempt.refresh_from_db()
        attempt_file.refresh_from_db()
        return attempt, attempt_file, exported_at

    def _rotate_locked(self, current, attempt, attempt_file, revoked, lease):
        if self._apply_revocations_locked(current, revoked) == 0:
            return None
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
        with heartbeat.job_token_transition(lease) as rotation:
            with heartbeat.foreground_write_guard():
                with transaction.atomic(using=self.using):
                    current = lock_current_lease(lease, using=self.using)
                    locked_attempt = ExportAttempt.objects.using(
                        self.using,
                    ).select_for_update().get(pk=attempt.pk)
                    locked_file = ExportAttemptFile.objects.using(
                        self.using,
                    ).select_for_update().get(pk=attempt_file.pk)
                    rotated = self._rotate_locked(
                        current, locked_attempt, locked_file, revoked, lease,
                    )
            if rotated is not None:
                rotation.replace(rotated)
        return rotated

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
        root = open_export_root(
            settings.PINRY_EXPORT_ROOT, os.getuid(), os.getgid(),
        )
        staging = source = ready = None
        descriptor = None
        try:
            staging = open_staging_directory(root)
            source = open_receipted_directory(
                staging, attempt.relative_path,
                self._attempt_directory_receipt(attempt),
            )
            ready = self._open_ready_directory(root)
            rename_noreplace(
                source, ARCHIVE_PART_NAME, ready, "{}.zip".format(job.pk),
            )
            flags = os.O_RDONLY | os.O_NOFOLLOW
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            descriptor = os.open(
                "{}.zip".format(job.pk), flags, dir_fd=ready.descriptor,
            )
            opened = OpenFileReceipt.from_fd(
                descriptor, ready.uid, ready.gid,
            )
            moved = ClosedFileReceipt.from_open_fd(
                descriptor, opened, attempt_file.receipt_sha256,
            )
            os.fsync(descriptor)
        except FileExistsError:
            raise ExportError("export_storage_unsafe", lease) from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
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
                return current_file

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
        candidate.state = "published"
        candidate.save(update_fields=("state",))
        attempt.state = "published"
        attempt.save(update_fields=("state",))
        ExportSlot.objects.using(self.using).filter(
            current_job=current,
        ).update(current_job=None)

    def _final_fence(self, attempt, candidate, exported_at, lease,
                     heartbeat, stop_requested):
        while True:
            rotated = None
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
                            raise ExportError("permission_changed", lease)
                        revoked = revoked_item_ids(
                            current, inclusion_state="included",
                            using=self.using, heartbeat=None,
                            stop_requested=None,
                            checkpoint=deadline.checkpoint, lease=lease,
                        )
                        if revoked:
                            rotated = self._rotate_locked(
                                current, locked_attempt, locked_file,
                                revoked, lease,
                            )
                        else:
                            self._complete_locked(
                                current, locked_attempt, locked_file,
                                exported_at,
                            )
                        deadline.checkpoint()
                    if rotated is None:
                        rotation.clear()
                    else:
                        rotation.replace(rotated)
                return rotated
            except DatabaseFenceBusy:
                self._stop(stop_requested, lease)
                heartbeat()
                self.sleeper(0.01)

    def _cleanup_retiring(self, attempt, attempt_file, lease, heartbeat):
        root = open_export_root(
            settings.PINRY_EXPORT_ROOT, os.getuid(), os.getgid(),
        )
        staging = directory = ready = None
        try:
            receipt = closed_file_receipt(attempt_file)
            if attempt_file.relative_path.startswith("ready/"):
                ready = self._open_ready_directory(root)
                remove_if_receipt_matches(
                    ready, os.path.basename(attempt_file.relative_path), receipt,
                )
            else:
                staging = open_staging_directory(root)
                directory = open_receipted_directory(
                    staging, attempt.relative_path,
                    self._attempt_directory_receipt(attempt),
                )
                remove_if_receipt_matches(
                    directory, ARCHIVE_PART_NAME, receipt,
                )
        finally:
            for current in (ready, directory, staging, root):
                if current is not None:
                    current.close()
        with heartbeat.foreground_write_guard():
            with transaction.atomic(using=self.using):
                lock_current_lease(lease, using=self.using)
                current_file = ExportAttemptFile.objects.using(
                    self.using,
                ).select_for_update().get(pk=attempt_file.pk)
                current_attempt = ExportAttempt.objects.using(
                    self.using,
                ).select_for_update().get(pk=attempt.pk)
                if current_file.state != "retiring":
                    raise LeaseLost()
                current_file.state = "cleaned"
                current_file.intent_relative_path = None
                current_file.save(update_fields=(
                    "state", "intent_relative_path",
                ))
                current_attempt.state = "cleaned"
                current_attempt.save(update_fields=("state",))

    def _handoff(self, attempt, candidate, heartbeat):
        with heartbeat.foreground_write_guard():
            with transaction.atomic(using=self.using):
                current_attempt = ExportAttempt.objects.using(
                    self.using,
                ).select_for_update().get(pk=attempt.pk)
                current_file = ExportAttemptFile.objects.using(
                    self.using,
                ).select_for_update().get(pk=candidate.pk)
                job = ExportJob.objects.using(self.using).select_for_update().get(
                    pk=current_attempt.job_id,
                )
                if (
                    job.state != "complete"
                    or job.ready_cleanup_state != "retained"
                    or current_file.state != "published"
                    or current_file.relative_path != job.ready_relative_path
                    or current_file.receipt_dev != job.ready_dev
                    or current_file.receipt_ino != job.ready_ino
                ):
                    return
                current_file.state = "cleaned"
                current_file.intent_relative_path = None
                current_file.save(update_fields=(
                    "state", "intent_relative_path",
                ))
                current_attempt.state = "cleaned"
                current_attempt.save(update_fields=("state",))

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
                if rotated is not None:
                    self._cleanup_retiring(
                        attempt, candidate, rotated, heartbeat,
                    )
                    current_lease = rotated
                    continue
                self._handoff(attempt, candidate, heartbeat)
                return ExportJob.objects.using(self.using).get(pk=job.pk)
        except ExportStorageError as error:
            raise ExportError(error.code, current_lease) from None

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
        rotated = self._final_fence(
            attempt, candidate, attempt.exported_at, lease,
            heartbeat, stop_requested,
        )
        return RecoveryOutcome(
            ExportJob.objects.using(self.using).get(pk=lease.job_id),
            rotated or lease,
        )

    def recover_retiring(self, lease, heartbeat, stop_requested):
        self._stop(stop_requested, lease)
        attempt = ExportAttempt.objects.using(self.using).filter(
            job_id=lease.job_id, state="retiring",
        ).order_by("attempt_generation").first()
        if attempt is not None:
            self._cleanup_retiring(
                attempt, attempt.files.get(kind="archive"), lease, heartbeat,
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
