from contextlib import contextmanager
import json
import os
import stat
import time
import uuid

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connections, transaction
from django.db.models import Q
from django.utils import timezone
from taggit.models import Tag, TaggedItem

from core.models import MediaAsset, Pin
from core.services.database_fence import (
    DatabaseFenceBusy,
    DatabaseFenceDeadline,
    _database_write_fence_window,
    database_write_fence,
)
from django_images.file_ops import (
    MediaLifecycleLockError,
    MediaPathError,
    open_verified_media_file,
    open_verified_media_root,
)
from django_images.models import Image
from exports.contracts import (
    ExportError,
    LeaseLost,
    StopRequested,
    lock_current_lease,
)
from exports.models import (
    ExportBlob,
    ExportItem,
    ExportJob,
    ExportTarget,
    ExportWorkerLease,
)
from exports.services.file_ops import (
    ClosedFileReceipt,
    DirectoryReceipt,
    ExportStorageError,
    OpenFileReceipt,
    SourceReceipt,
    SpaceBudget,
    clone_or_copy,
    create_private_directory,
    create_private_file_fs,
    media_global_writer_gate,
    open_export_root,
    open_receipted_directory,
    remove_if_receipt_matches,
    rename_noreplace,
    verify_space,
)
from exports.services.metadata import PortableNameAllocator, redact_url


User = get_user_model()
QUERY_CHUNK_SIZE = 400
CLEANUP_CAS_RETRY_SECONDS = 5.0
_METADATA_PER_PIN_BYTES = 32 * 1024
_MANIFEST_OVERHEAD_BYTES = 64 * 1024
_METADATA_ESCAPE_AND_DOCUMENT_FACTOR = 16


def _encoded_size(value):
    if value is None:
        return 0
    return len(str(value).encode("utf-8", errors="replace"))


def detect_image_mime(source_fd):
    try:
        header = os.pread(source_fd, 16, 0)
    except OSError as error:
        raise ExportStorageError("source_unsafe") from error
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    if header.startswith(b"BM"):
        return "image/bmp"
    if header.startswith((b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")):
        return "image/tiff"
    raise ExportStorageError("source_unsafe")


def _directory_receipt_from_job(job):
    values = (
        job.candidate_snapshot_dir_dev,
        job.candidate_snapshot_dir_ino,
        job.candidate_snapshot_dir_uid,
        job.candidate_snapshot_dir_gid,
        job.candidate_snapshot_dir_mode,
    )
    if any(value is None for value in values):
        return None
    return DirectoryReceipt(*values)


def _candidate_name(job_id, generation):
    return "snapshot-{}-{}".format(job_id, generation)


def _blob_part_name(blob_id):
    return "{}.part".format(blob_id)


def _blob_final_name(blob_id):
    return str(blob_id)


def _open_staging_directory(export_root):
    try:
        named = os.stat(
            ".staging",
            dir_fd=export_root.descriptor,
            follow_symlinks=False,
        )
    except OSError as error:
        raise ExportStorageError("export_storage_unsafe") from error
    if (
        not stat.S_ISDIR(named.st_mode)
        or named.st_uid != export_root.uid
        or named.st_gid != export_root.gid
        or stat.S_IMODE(named.st_mode) != 0o700
    ):
        raise ExportStorageError("export_storage_unsafe")
    receipt = DirectoryReceipt(
        named.st_dev,
        named.st_ino,
        named.st_uid,
        named.st_gid,
        stat.S_IMODE(named.st_mode),
    )
    return open_receipted_directory(export_root, ".staging", receipt)


class _MetadataFenceBusy(Exception):
    pass


class _CleanupFenceBusy(Exception):
    pass


class SnapshotService(object):
    FENCE_MODELS = (
        User,
        Pin,
        Image,
        MediaAsset,
        Tag,
        TaggedItem,
        ExportWorkerLease,
        ExportJob,
        ExportTarget,
        ExportItem,
        ExportBlob,
    )

    def __init__(
        self,
        using="default",
        fault_injector=None,
        generation_factory=None,
        monotonic=None,
        sleeper=None,
    ):
        self.using = using
        self.fault_injector = fault_injector or (lambda point, context: None)
        self.generation_factory = generation_factory or uuid.uuid4
        self.monotonic = monotonic or time.monotonic
        self.sleeper = sleeper or time.sleep

    def _fault(self, point, **context):
        self.fault_injector(point, context)

    @contextmanager
    def _metadata_fence(self, heartbeat, lease):
        with heartbeat.foreground_write_guard():
            with database_write_fence(
                using=self.using,
                models=self.FENCE_MODELS,
            ):
                yield lock_current_lease(lease, using=self.using)

    @contextmanager
    def _lease_cas(self, heartbeat, lease):
        database_connection = connections[self.using]
        with heartbeat.foreground_write_guard():
            with _database_write_fence_window(database_connection):
                with transaction.atomic(using=self.using):
                    yield self._lock_current_lease_nowait(lease)

    def _lock_current_lease_nowait(self, lease):
        try:
            worker = ExportWorkerLease.objects.using(
                self.using,
            ).select_for_update(nowait=True).get(pk=1)
        except ExportWorkerLease.DoesNotExist:
            raise LeaseLost()
        if (
            worker.generation != lease.worker_generation
            or worker.lease_uuid != lease.worker_lease_uuid
        ):
            raise LeaseLost()
        try:
            return ExportJob.objects.using(
                self.using,
            ).select_for_update(nowait=True).get(**lease.job_fence())
        except ExportJob.DoesNotExist:
            raise LeaseLost()

    def _raise_if_stopped(self, stop_requested, lease):
        if stop_requested():
            raise StopRequested(lease)

    def _wait(self, heartbeat, stop_requested, lease):
        self._raise_if_stopped(stop_requested, lease)
        heartbeat()
        self.sleeper(0.01)
        self._raise_if_stopped(stop_requested, lease)

    def _require_candidate(
        self,
        current,
        generation,
        lease,
        directory=None,
    ):
        if current.candidate_snapshot_generation != generation:
            raise LeaseLost()
        if current.candidate_snapshot_relative_path != _candidate_name(
            lease.job_id,
            generation,
        ):
            raise ExportError("export_storage_unsafe", lease)
        if (
            directory is not None
            and _directory_receipt_from_job(current) != directory.receipt
        ):
            raise ExportError("export_storage_unsafe", lease)

    def _plan_candidate(self, lease, heartbeat):
        generation = self.generation_factory()
        relative_path = _candidate_name(lease.job_id, generation)
        with self._lease_cas(heartbeat, lease) as current:
            if current.snapshot_generation is not None:
                return current.snapshot_generation, None
            if current.owner_id is None:
                raise ExportError("permission_changed", lease)
            if current.targets.filter(
                Q(pin_owner_id_snapshot__isnull=True)
                | Q(pin_published_at_snapshot__isnull=True)
            ).exists():
                raise ExportError("permission_changed", lease)
            if current.candidate_snapshot_generation is not None:
                return None, current.candidate_snapshot_generation
            current.candidate_snapshot_generation = generation
            current.candidate_snapshot_relative_path = relative_path
            current.staging_cleanup_state = "pending"
            current.save(update_fields=(
                "candidate_snapshot_generation",
                "candidate_snapshot_relative_path",
                "staging_cleanup_state",
            ))
        self._fault("after_candidate_planned", generation=generation)
        return generation, generation

    def _prepare_directory(self, staging, generation, job_id):
        name = _candidate_name(job_id, generation)
        directory = create_private_directory(
            staging,
            name,
            staging.uid,
            staging.gid,
        )
        try:
            self._fault(
                "before_directory_receipt",
                generation=generation,
                directory_fd=directory.descriptor,
            )
            return directory
        except BaseException:
            directory.close()
            raise

    def _record_directory(self, lease, generation, directory, heartbeat):
        receipt = directory.receipt
        with self._lease_cas(heartbeat, lease) as current:
            self._require_candidate(current, generation, lease)
            recorded_receipt = _directory_receipt_from_job(current)
            if recorded_receipt not in (None, receipt):
                raise ExportError("export_storage_unsafe", lease)
            current.candidate_snapshot_dir_dev = receipt.dev
            current.candidate_snapshot_dir_ino = receipt.ino
            current.candidate_snapshot_dir_uid = receipt.uid
            current.candidate_snapshot_dir_gid = receipt.gid
            current.candidate_snapshot_dir_mode = receipt.mode
            current.save(update_fields=(
                "candidate_snapshot_dir_dev",
                "candidate_snapshot_dir_ino",
                "candidate_snapshot_dir_uid",
                "candidate_snapshot_dir_gid",
                "candidate_snapshot_dir_mode",
            ))
        self._fault("after_directory_receipt", generation=generation)

    @staticmethod
    def _chunks(values):
        for start in range(0, len(values), QUERY_CHUNK_SIZE):
            yield values[start:start + QUERY_CHUNK_SIZE]

    def _space_budget(self, current, media_root, lease):
        targets = list(current.targets.order_by("position"))
        if current.owner_id is None or any(
            target.pin_owner_id_snapshot is None
            or target.pin_published_at_snapshot is None
            for target in targets
        ):
            raise ExportError("permission_changed", lease)
        pins = {}
        target_ids = [target.pin_id for target in targets]
        for target_chunk in self._chunks(target_ids):
            rows = Pin.objects.using(self.using).filter(
                pk__in=target_chunk,
            ).select_related("submitter", "image", "image__media_asset")
            pins.update((pin.pk, pin) for pin in rows)
        selected = []
        sources = {}
        for target in targets:
            pin = pins.get(target.pin_id)
            if pin is None or pin.published != target.pin_published_at_snapshot:
                continue
            if target.pin_owner_id_snapshot == current.owner_id:
                if pin.submitter_id != current.owner_id:
                    continue
            elif (
                pin.submitter_id != target.pin_owner_id_snapshot
                or pin.private
            ):
                continue
            try:
                asset = pin.image.media_asset
            except MediaAsset.DoesNotExist:
                continue
            selected.append(pin)
            sources[asset.pk] = pin.image.image.name
        tag_names = self._tag_names(
            [pin.pk for pin in selected],
            lambda: None,
        )
        payload_bytes = 0
        source_counts = {}
        for pin in selected:
            payload_bytes += sum(_encoded_size(value) for value in (
                pin.submitter.username,
                pin.description,
                pin.url,
                pin.referer,
                pin.image.original_filename,
                pin.image.image.name,
            ))
            payload_bytes += sum(
                _encoded_size(name) for name in tag_names.get(pin.pk, ())
            )
            source_counts[pin.image.media_asset.pk] = (
                source_counts.get(pin.image.media_asset.pk, 0) + 1
            )
        sizes = {}
        for asset_id, source_path in sources.items():
            source = None
            try:
                source = open_verified_media_file(
                    media_root,
                    source_path,
                    missing_ok=True,
                )
                sizes[asset_id] = (
                    SourceReceipt.from_fd(source.descriptor).size
                    if source is not None
                    else settings.PINRY_FETCH_MAX_BYTES
                )
            except (ExportStorageError, MediaPathError):
                sizes[asset_id] = settings.PINRY_FETCH_MAX_BYTES
            finally:
                if source is not None:
                    source.close()
        distinct_original_bytes = sum(sizes.values())
        archive_pin_bytes = sum(
            sizes[asset_id] * source_counts[asset_id]
            for asset_id in sizes
        )
        metadata_overhead = (
            _MANIFEST_OVERHEAD_BYTES
            + len(selected) * _METADATA_PER_PIN_BYTES
            + payload_bytes * _METADATA_ESCAPE_AND_DOCUMENT_FACTOR
        )
        return (
            SpaceBudget.for_export(
                distinct_original_bytes,
                archive_pin_bytes,
                metadata_overhead,
            ),
            sizes,
        )

    def _candidate_space_budget(self, lease, generation, media_root):
        items = list(ExportItem.objects.using(self.using).filter(
            job_id=lease.job_id,
            snapshot_generation=generation,
        ).order_by("target_position"))
        blobs = list(ExportBlob.objects.using(self.using).filter(
            job_id=lease.job_id,
            snapshot_generation=generation,
        ).order_by("source_media_asset_id"))
        reference_counts = {}
        payload_bytes = 0
        for item in items:
            reference_counts[item.blob_id] = (
                reference_counts.get(item.blob_id, 0) + 1
            )
            payload_bytes += sum(_encoded_size(value) for value in (
                item.owner_username,
                item.description,
                item.tags_json,
                item.source_url,
                item.referer_url,
                item.original_filename,
            ))
        sizes = {}
        for blob in blobs:
            source = None
            try:
                source = open_verified_media_file(
                    media_root,
                    blob.source_relative_path,
                    missing_ok=True,
                )
                sizes[blob.source_media_asset_id] = (
                    SourceReceipt.from_fd(source.descriptor).size
                    if source is not None
                    else settings.PINRY_FETCH_MAX_BYTES
                )
            except (ExportStorageError, MediaPathError):
                sizes[blob.source_media_asset_id] = (
                    settings.PINRY_FETCH_MAX_BYTES
                )
            finally:
                if source is not None:
                    source.close()
            payload_bytes += _encoded_size(blob.source_relative_path)
        distinct_original_bytes = sum(sizes.values())
        archive_pin_bytes = sum(
            sizes[blob.source_media_asset_id]
            * reference_counts.get(blob.pk, 0)
            for blob in blobs
        )
        metadata_overhead = (
            _MANIFEST_OVERHEAD_BYTES
            + len(items) * _METADATA_PER_PIN_BYTES
            + payload_bytes * _METADATA_ESCAPE_AND_DOCUMENT_FACTOR
        )
        return (
            SpaceBudget.for_export(
                distinct_original_bytes,
                archive_pin_bytes,
                metadata_overhead,
            ),
            sizes,
            reference_counts,
        )

    def _tag_names(self, pin_ids, checkpoint):
        names = {pin_id: [] for pin_id in pin_ids}
        for pin_chunk in self._chunks(pin_ids):
            rows = TaggedItem.objects.using(self.using).filter(
                content_type__app_label="core",
                content_type__model="pin",
                object_id__in=pin_chunk,
            ).values_list("object_id", "tag__name")
            for pin_id, name in rows:
                names.setdefault(pin_id, []).append(name)
            checkpoint()
        return names

    def _build_metadata(self, current, generation, lease, checkpoint):
        targets = list(current.targets.order_by("position"))
        if any(
            target.pin_owner_id_snapshot is None
            or target.pin_published_at_snapshot is None
            for target in targets
        ):
            raise ExportError("permission_changed", lease)
        target_ids = [target.pin_id for target in targets]
        pins = {}
        for target_chunk in self._chunks(target_ids):
            rows = Pin.objects.using(self.using).filter(
                pk__in=target_chunk,
            ).select_related("submitter", "image", "image__media_asset")
            pins.update((pin.pk, pin) for pin in rows)
            checkpoint()
        tags = self._tag_names(target_ids, checkpoint)
        blobs = {}
        items = []
        excluded = 0
        for target_index, target in enumerate(targets, 1):
            if target_index % QUERY_CHUNK_SIZE == 0:
                checkpoint()
            pin = pins.get(target.pin_id)
            owned = target.pin_owner_id_snapshot == current.owner_id
            same_identity = (
                pin is not None
                and pin.published == target.pin_published_at_snapshot
            )
            if owned:
                if pin is None or not same_identity:
                    raise ExportError("source_missing", lease)
                if pin.submitter_id != current.owner_id:
                    excluded += 1
                    continue
            elif (
                not same_identity
                or pin.submitter_id != target.pin_owner_id_snapshot
                or pin.private
            ):
                excluded += 1
                continue
            try:
                asset = pin.image.media_asset
            except MediaAsset.DoesNotExist:
                if owned:
                    raise ExportError("source_missing", lease)
                excluded += 1
                continue
            blob = blobs.get(asset.pk)
            if blob is None:
                blob = ExportBlob(
                    job=current,
                    snapshot_generation=generation,
                    source_media_asset_id=asset.pk,
                    source_image_id=pin.image_id,
                    source_relative_path=pin.image.image.name,
                    expected_sha256=asset.content_sha256,
                )
                blobs[asset.pk] = blob
            source_url, source_redacted = redact_url(pin.url)
            referer_url, referer_redacted = redact_url(pin.referer)
            items.append(ExportItem(
                job=current,
                target_position=target.position,
                snapshot_generation=generation,
                blob=blob,
                pin_id=pin.pk,
                pin_owner_id=pin.submitter_id,
                owner_username=pin.submitter.username,
                is_public=not pin.private,
                published_at=pin.published,
                description=pin.description,
                tags_json=json.dumps(
                    sorted(set(tags.get(pin.pk, ()))),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                source_url=source_url,
                source_url_redacted=source_redacted,
                referer_url=referer_url,
                referer_url_redacted=referer_redacted,
                original_filename=pin.image.original_filename,
            ))
        checkpoint()
        return items, list(blobs.values()), excluded

    def _record_metadata(self, lease, generation, directory, heartbeat):
        deadline = DatabaseFenceDeadline(self.monotonic)
        with self._metadata_fence(heartbeat, lease) as current:
            self._require_candidate(
                current,
                generation,
                lease,
                directory=directory,
            )
            if current.owner_id is None:
                raise ExportError("permission_changed", lease)
            if _directory_receipt_from_job(current) is None:
                raise ExportError("export_storage_unsafe", lease)
            snapshot_at = timezone.now()
            items, blobs, excluded = self._build_metadata(
                current,
                generation,
                lease,
                deadline.checkpoint,
            )
            deadline.checkpoint()
            ExportBlob.objects.using(self.using).bulk_create(blobs)
            ExportItem.objects.using(self.using).bulk_create(items)
            deadline.checkpoint()
        return snapshot_at, len(items), excluded

    @staticmethod
    def _open_receipt_values(receipt, size, file_stat):
        return {
            "receipt_dev": receipt.dev,
            "receipt_ino": receipt.ino,
            "receipt_uid": receipt.uid,
            "receipt_gid": receipt.gid,
            "receipt_mode": receipt.mode,
            "receipt_nlink": receipt.nlink,
            "size": size,
            "receipt_mtime_ns": file_stat.st_mtime_ns,
            "receipt_ctime_ns": file_stat.st_ctime_ns,
        }

    def _record_open_blob(
        self,
        lease,
        generation,
        blob,
        source_receipt,
        mime_type,
        open_receipt,
        destination_fd,
        directory,
        heartbeat,
    ):
        current_stat = os.fstat(destination_fd)
        values = self._open_receipt_values(open_receipt, 0, current_stat)
        values.update({
            "source_dev": source_receipt.dev,
            "source_ino": source_receipt.ino,
            "source_size": source_receipt.size,
            "source_mtime_ns": source_receipt.mtime_ns,
            "source_ctime_ns": source_receipt.ctime_ns,
            "mime_type": mime_type,
            "part_relative_path": "{}/{}".format(
                _candidate_name(lease.job_id, generation),
                _blob_part_name(blob.pk),
            ),
            "snapshot_relative_path": "{}/{}".format(
                _candidate_name(lease.job_id, generation),
                _blob_final_name(blob.pk),
            ),
            "file_state": "writing",
        })
        with self._lease_cas(heartbeat, lease) as current:
            self._require_candidate(
                current,
                generation,
                lease,
                directory=directory,
            )
            updated = ExportBlob.objects.using(self.using).filter(
                pk=blob.pk,
                job=current,
                snapshot_generation=generation,
                source_dev__isnull=True,
            ).update(**values)
            if updated != 1:
                raise LeaseLost()
        for name, value in values.items():
            setattr(blob, name, value)

    def _record_closed_blob(
        self,
        lease,
        generation,
        blob,
        method,
        closed_receipt,
        directory,
        heartbeat,
    ):
        values = {
            "file_state": "closed",
            "capture_method": method,
            "size": closed_receipt.size,
            "receipt_dev": closed_receipt.dev,
            "receipt_ino": closed_receipt.ino,
            "receipt_uid": closed_receipt.uid,
            "receipt_gid": closed_receipt.gid,
            "receipt_mode": closed_receipt.mode,
            "receipt_nlink": closed_receipt.nlink,
            "receipt_mtime_ns": closed_receipt.mtime_ns,
            "receipt_ctime_ns": closed_receipt.ctime_ns,
            "receipt_sha256": closed_receipt.sha256,
        }
        with self._lease_cas(heartbeat, lease) as current:
            self._require_candidate(
                current,
                generation,
                lease,
                directory=directory,
            )
            updated = ExportBlob.objects.using(self.using).filter(
                pk=blob.pk,
                job=current,
                snapshot_generation=generation,
                file_state="writing",
            ).update(**values)
            if updated != 1:
                raise LeaseLost()
        for name, value in values.items():
            setattr(blob, name, value)

    def _record_progress(self, lease, generation, directory, heartbeat):
        with self._lease_cas(heartbeat, lease) as current:
            self._require_candidate(
                current,
                generation,
                lease,
                directory=directory,
            )
            current.progress_at = timezone.now()
            current.save(update_fields=("progress_at",))

    def _capture_blobs(
        self,
        lease,
        generation,
        media_root,
        directory,
        space_budget,
        observed_sizes,
        reference_counts,
        heartbeat,
        stop_requested,
    ):
        blobs = list(ExportBlob.objects.using(self.using).filter(
            job_id=lease.job_id,
            snapshot_generation=generation,
        ).order_by("source_media_asset_id"))
        remaining_snapshot_bytes = [sum(observed_sizes.values())]
        current_distinct_bytes = [remaining_snapshot_bytes[0]]
        current_archive_bytes = [space_budget.archive_pin_bytes]
        for blob in blobs:
            self._raise_if_stopped(stop_requested, lease)
            self._fault(
                "before_blob_start_lease_check",
                blob=blob,
                generation=generation,
            )
            with self._lease_cas(heartbeat, lease):
                pass
            self._fault(
                "after_blob_start_lease_check",
                blob=blob,
                generation=generation,
            )
            try:
                source = open_verified_media_file(
                    media_root,
                    blob.source_relative_path,
                    missing_ok=True,
                )
            except MediaPathError:
                raise ExportError("source_unsafe", lease) from None
            if source is None:
                raise ExportError("source_missing", lease)
            destination_fd = None
            try:
                source_receipt = SourceReceipt.from_fd(source.descriptor)
                observed_size = observed_sizes.get(
                    blob.source_media_asset_id,
                    0,
                )
                remaining_snapshot_bytes[0] += (
                    source_receipt.size - observed_size
                )
                current_distinct_bytes[0] += (
                    source_receipt.size - observed_size
                )
                current_archive_bytes[0] += (
                    source_receipt.size - observed_size
                ) * reference_counts.get(blob.pk, 0)
                mime_type = detect_image_mime(source.descriptor)
                part_name = _blob_part_name(blob.pk)
                final_name = _blob_final_name(blob.pk)
                destination_fd, open_receipt = create_private_file_fs(
                    directory,
                    part_name,
                )
                context = {
                    "blob": blob,
                    "source_fd": source.descriptor,
                    "destination_fd": destination_fd,
                    "generation": generation,
                }
                self._fault("before_blob_open_receipt", **context)
                self._record_open_blob(
                    lease,
                    generation,
                    blob,
                    source_receipt,
                    mime_type,
                    open_receipt,
                    destination_fd,
                    directory,
                    heartbeat,
                )
                self._fault("after_blob_open_receipt", **context)
                copy_checkpoint = [False]

                def checked_stop():
                    self._raise_if_stopped(stop_requested, lease)
                    return False

                def copying_heartbeat():
                    heartbeat()
                    if not copy_checkpoint[0]:
                        self._fault("copy_midpoint", **context)
                        copy_checkpoint[0] = True

                method, copied_hash = clone_or_copy(
                    source.descriptor,
                    destination_fd,
                    source_receipt,
                    copying_heartbeat,
                    checked_stop,
                    lambda required: verify_space(
                        directory.parent.parent.descriptor,
                        max(
                            required,
                            remaining_snapshot_bytes[0]
                            + current_archive_bytes[0]
                            + space_budget.metadata_overhead
                            + max(
                                settings.PINRY_EXPORT_SPACE_MIN_MARGIN_BYTES,
                                (current_distinct_bytes[0] + 9) // 10,
                            ),
                        ),
                    ),
                )
                remaining_snapshot_bytes[0] -= source_receipt.size
                if not copy_checkpoint[0]:
                    self._fault("copy_midpoint", **context)
                    copy_checkpoint[0] = True
                source.verify_current()
                source_receipt.verify_identity(source.descriptor)
                if (
                    method == "copy"
                    and blob.expected_sha256 is not None
                    and copied_hash != blob.expected_sha256
                ):
                    raise ExportError("source_changed", lease)
                self._fault("before_blob_rename_lease_check", **context)
                with self._lease_cas(heartbeat, lease):
                    pass
                self._fault("after_blob_rename_lease_check", **context)
                self._fault("before_blob_rename", **context)
                rename_noreplace(
                    directory,
                    part_name,
                    directory,
                    final_name,
                )
                self._fault("after_blob_rename_before_closed", **context)
                closed_receipt = ClosedFileReceipt.from_open_fd(
                    destination_fd,
                    open_receipt,
                    copied_hash if method == "copy" else None,
                )
                self._fault("before_blob_closed_receipt", **context)
                self._record_closed_blob(
                    lease,
                    generation,
                    blob,
                    method,
                    closed_receipt,
                    directory,
                    heartbeat,
                )
                self._fault("after_blob_closed", **context)
                self._fault("before_progress_cas", **context)
                self._record_progress(
                    lease,
                    generation,
                    directory,
                    heartbeat,
                )
            except ExportStorageError as error:
                raise ExportError(error.code, lease) from None
            finally:
                if destination_fd is not None:
                    os.close(destination_fd)
                source.close()

    def _finalize(
        self,
        lease,
        generation,
        directory,
        snapshot_at,
        included,
        excluded,
        heartbeat,
        stop_requested,
    ):
        items = list(ExportItem.objects.using(self.using).filter(
            job_id=lease.job_id,
            snapshot_generation=generation,
        ).select_related("blob").order_by("target_position"))
        self._allocate_archive_names(
            items,
            heartbeat,
            stop_requested,
            lease,
        )
        directory.verify_identity()
        self._fault("before_final_cas", generation=generation)
        deadline = DatabaseFenceDeadline(self.monotonic)
        with self._lease_cas(heartbeat, lease) as current:
            self._require_candidate(
                current,
                generation,
                lease,
                directory=directory,
            )
            if current.blobs.filter(
                snapshot_generation=generation,
            ).exclude(file_state="closed").exists():
                raise ExportError("snapshot_failed", lease)
            if current.items.filter(
                snapshot_generation=generation,
            ).count() != len(items):
                raise LeaseLost()
            for item_chunk in self._chunks(items):
                ExportItem.objects.using(self.using).bulk_update(
                    item_chunk,
                    ("archive_image_path", "archive_xmp_path"),
                )
                deadline.checkpoint()
            self._confirm_candidate_blobs(
                current,
                generation,
                deadline,
            )
            deadline.checkpoint()
            receipt = directory.receipt
            current.snapshot_generation = generation
            current.snapshot_relative_path = current.candidate_snapshot_relative_path
            current.snapshot_dir_dev = receipt.dev
            current.snapshot_dir_ino = receipt.ino
            current.snapshot_dir_uid = receipt.uid
            current.snapshot_dir_gid = receipt.gid
            current.snapshot_dir_mode = receipt.mode
            current.candidate_snapshot_generation = None
            current.candidate_snapshot_relative_path = None
            current.candidate_snapshot_dir_dev = None
            current.candidate_snapshot_dir_ino = None
            current.candidate_snapshot_dir_uid = None
            current.candidate_snapshot_dir_gid = None
            current.candidate_snapshot_dir_mode = None
            current.snapshot_at = snapshot_at
            current.snapshot_done = current.target_total
            current.archive_total = included
            current.bytes_total = sum(item.blob.size for item in items)
            current.included_total = included
            current.excluded_total = current.requested_total - included
            current.excluded_permission_revoked_total += excluded
            current.state = "archiving"
            current.save()
            deadline.checkpoint()

    def _confirm_candidate_blobs(self, current, generation, deadline):
        while True:
            blob_ids = list(current.blobs.filter(
                snapshot_generation=generation,
                confirmed=False,
            ).order_by("source_media_asset_id").values_list(
                "pk",
                flat=True,
            )[:QUERY_CHUNK_SIZE])
            if not blob_ids:
                return
            current.blobs.filter(pk__in=blob_ids).update(confirmed=True)
            deadline.checkpoint()

    def _allocate_archive_names(
        self,
        items,
        heartbeat,
        stop_requested,
        lease,
    ):
        allocator = PortableNameAllocator()
        for item_index, item in enumerate(items, 1):
            image_path, xmp_path = allocator.reserve(
                item.pk,
                item.pin_id,
                item.original_filename,
                item.blob.mime_type,
            )
            item.archive_image_path = image_path
            item.archive_xmp_path = xmp_path
            if item_index % QUERY_CHUNK_SIZE == 0:
                self._raise_if_stopped(stop_requested, lease)
                with self._lease_cas(heartbeat, lease):
                    pass
                heartbeat()
        self._raise_if_stopped(stop_requested, lease)

    @staticmethod
    def _open_receipt_from_blob(blob):
        return OpenFileReceipt(
            blob.receipt_dev,
            blob.receipt_ino,
            blob.receipt_uid,
            blob.receipt_gid,
            blob.receipt_mode,
            blob.receipt_nlink,
        )

    @staticmethod
    def _closed_receipt_from_blob(blob):
        return ClosedFileReceipt(
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

    def _remove_unreceipted(self, directory, name, source_size):
        try:
            current = os.stat(
                name,
                dir_fd=directory.descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_uid != directory.uid
            or current.st_gid != directory.gid
            or stat.S_IMODE(current.st_mode) != 0o600
            or current.st_nlink != 1
            or current.st_size not in (0, source_size)
        ):
            raise ExportStorageError("export_storage_unsafe")
        receipt = OpenFileReceipt(
            current.st_dev,
            current.st_ino,
            current.st_uid,
            current.st_gid,
            stat.S_IMODE(current.st_mode),
            current.st_nlink,
        )
        remove_if_receipt_matches(directory, name, receipt)

    def _cleanup_checkpoint(
        self,
        heartbeat,
        stop_requested,
        lease,
    ):
        def checkpoint():
            self._raise_if_stopped(stop_requested, lease)
            self._fault(
                "before_candidate_cleanup_checkpoint_cas",
                lease=lease,
            )
            with self._lease_cas(heartbeat, lease):
                pass
            heartbeat()
            self._raise_if_stopped(stop_requested, lease)

        self._retry_cleanup_cas(
            checkpoint,
            heartbeat,
            stop_requested,
            lease,
        )

    def _retry_cleanup_cas(
        self,
        operation,
        heartbeat,
        stop_requested,
        lease,
    ):
        deadline = self.monotonic() + CLEANUP_CAS_RETRY_SECONDS
        while True:
            try:
                return operation()
            except DatabaseFenceBusy:
                if self.monotonic() >= deadline:
                    raise _CleanupFenceBusy()
                self._raise_if_stopped(stop_requested, lease)
                try:
                    self._wait(heartbeat, stop_requested, lease)
                except DatabaseFenceBusy:
                    self.sleeper(0.01)

    def _wait_for_cleanup_retry(
        self,
        heartbeat,
        stop_requested,
        lease,
    ):
        self._raise_if_stopped(stop_requested, lease)
        try:
            self._wait(heartbeat, stop_requested, lease)
        except DatabaseFenceBusy:
            self.sleeper(0.01)
            self._raise_if_stopped(stop_requested, lease)

    def _cleanup_candidate_fs(  # noqa: C901
        self,
        staging,
        job,
        blob_chunks,
        has_blobs,
        heartbeat,
        stop_requested,
        lease,
    ):
        if job.candidate_snapshot_relative_path != _candidate_name(
            job.pk,
            job.candidate_snapshot_generation,
        ):
            raise ExportStorageError("export_storage_unsafe")
        receipt = _directory_receipt_from_job(job)
        if receipt is None:
            if has_blobs:
                raise ExportStorageError("export_storage_unsafe")
            try:
                named = os.stat(
                    job.candidate_snapshot_relative_path,
                    dir_fd=staging.descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return
            if (
                not stat.S_ISDIR(named.st_mode)
                or named.st_uid != staging.uid
                or named.st_gid != staging.gid
                or stat.S_IMODE(named.st_mode) != 0o700
            ):
                raise ExportStorageError("export_storage_unsafe")
            receipt = DirectoryReceipt(
                named.st_dev,
                named.st_ino,
                named.st_uid,
                named.st_gid,
                stat.S_IMODE(named.st_mode),
            )
        else:
            try:
                named = os.stat(
                    job.candidate_snapshot_relative_path,
                    dir_fd=staging.descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return
            if not receipt.matches_stat(named):
                raise ExportStorageError("export_storage_unsafe")
        directory = open_receipted_directory(
            staging,
            job.candidate_snapshot_relative_path,
            receipt,
        )
        try:
            for blob_chunk in blob_chunks:
                self._cleanup_checkpoint(
                    heartbeat,
                    stop_requested,
                    lease,
                )
                for blob in blob_chunk:
                    part_name = _blob_part_name(blob.pk)
                    final_name = _blob_final_name(blob.pk)
                    existing = []
                    for name in (part_name, final_name):
                        try:
                            os.stat(
                                name,
                                dir_fd=directory.descriptor,
                                follow_symlinks=False,
                            )
                        except FileNotFoundError:
                            continue
                        existing.append(name)
                    if len(existing) > 1:
                        raise ExportStorageError("export_storage_unsafe")
                    if not existing:
                        continue
                    name = existing[0]
                    if blob.receipt_dev is None:
                        if name != part_name:
                            raise ExportStorageError("export_storage_unsafe")
                        self._remove_unreceipted(
                            directory,
                            name,
                            0,
                        )
                    elif blob.file_state == "closed":
                        if name != final_name:
                            raise ExportStorageError("export_storage_unsafe")
                        remove_if_receipt_matches(
                            directory,
                            name,
                            self._closed_receipt_from_blob(blob),
                        )
                    else:
                        current = os.stat(
                            name,
                            dir_fd=directory.descriptor,
                            follow_symlinks=False,
                        )
                        if (
                            blob.source_size is None
                            or current.st_size < 0
                            or current.st_size > blob.source_size
                        ):
                            raise ExportStorageError("export_storage_unsafe")
                        remove_if_receipt_matches(
                            directory,
                            name,
                            self._open_receipt_from_blob(blob),
                        )
            if os.listdir(directory.descriptor):
                raise ExportStorageError("export_storage_unsafe")
            self._fault(
                "before_candidate_rmdir",
                directory=directory,
                staging=staging,
                generation=job.candidate_snapshot_generation,
            )
            self._cleanup_checkpoint(
                heartbeat,
                stop_requested,
                lease,
            )
            if os.listdir(directory.descriptor):
                raise ExportStorageError("export_storage_unsafe")
            directory.verify_identity()
            os.rmdir(directory.name, dir_fd=staging.descriptor)
            os.fsync(staging.descriptor)
        finally:
            directory.close()

    def _cleanup_candidate(
        self,
        staging,
        lease,
        heartbeat,
        stop_requested,
    ):
        def record_intent():
            self._raise_if_stopped(stop_requested, lease)
            self._fault(
                "before_candidate_cleanup_intent_cas",
                lease=lease,
            )
            with self._lease_cas(heartbeat, lease) as current:
                if current.candidate_snapshot_generation is None:
                    return None
                current.staging_cleanup_state = "pending"
                current.save(update_fields=("staging_cleanup_state",))
                job = current
            return job

        cleanup = self._retry_cleanup_cas(
            record_intent,
            heartbeat,
            stop_requested,
            lease,
        )
        if cleanup is None:
            return
        job = cleanup
        blobs = ExportBlob.objects.using(self.using).filter(
            job_id=job.pk,
            snapshot_generation=job.candidate_snapshot_generation,
        )
        has_blobs = blobs.exists()
        self._fault(
            "after_candidate_cleanup_intent",
            generation=job.candidate_snapshot_generation,
        )
        try:
            self._cleanup_candidate_fs(
                staging,
                job,
                self._candidate_blob_chunks(
                    job.pk,
                    job.candidate_snapshot_generation,
                ),
                has_blobs,
                heartbeat,
                stop_requested,
                lease,
            )
        except ExportStorageError as error:
            raise ExportError(error.code, lease) from None
        except OSError:
            raise ExportError("export_storage_unsafe", lease) from None
        self._fault(
            "after_candidate_cleanup_fs",
            generation=job.candidate_snapshot_generation,
        )

        def record_cleaned():
            self._raise_if_stopped(stop_requested, lease)
            self._fault(
                "before_candidate_cleaned_cas",
                generation=job.candidate_snapshot_generation,
            )
            with self._lease_cas(heartbeat, lease) as current:
                if (
                    current.candidate_snapshot_generation
                    != job.candidate_snapshot_generation
                ):
                    raise LeaseLost()
                deadline = DatabaseFenceDeadline(self.monotonic)
                self._delete_candidate_rows(
                    current,
                    job.candidate_snapshot_generation,
                    deadline,
                )
                current.candidate_snapshot_generation = None
                current.candidate_snapshot_relative_path = None
                current.candidate_snapshot_dir_dev = None
                current.candidate_snapshot_dir_ino = None
                current.candidate_snapshot_dir_uid = None
                current.candidate_snapshot_dir_gid = None
                current.candidate_snapshot_dir_mode = None
                current.staging_cleanup_state = "cleaned"
                current.save(update_fields=(
                    "candidate_snapshot_generation",
                    "candidate_snapshot_relative_path",
                    "candidate_snapshot_dir_dev",
                    "candidate_snapshot_dir_ino",
                    "candidate_snapshot_dir_uid",
                    "candidate_snapshot_dir_gid",
                    "candidate_snapshot_dir_mode",
                    "staging_cleanup_state",
                ))
                deadline.checkpoint()

        self._retry_cleanup_cas(
            record_cleaned,
            heartbeat,
            stop_requested,
            lease,
        )

    def _candidate_blob_chunks(self, job_id, generation):
        last_source_id = None
        while True:
            blobs = ExportBlob.objects.using(self.using).filter(
                job_id=job_id,
                snapshot_generation=generation,
            )
            if last_source_id is not None:
                blobs = blobs.filter(
                    source_media_asset_id__gt=last_source_id,
                )
            blob_chunk = list(blobs.order_by(
                "source_media_asset_id",
            )[:QUERY_CHUNK_SIZE])
            if not blob_chunk:
                return
            yield blob_chunk
            last_source_id = blob_chunk[-1].source_media_asset_id

    def _delete_candidate_rows(self, current, generation, deadline):
        for related_name in ("items", "blobs"):
            related = getattr(current, related_name)
            while True:
                row_ids = list(related.filter(
                    snapshot_generation=generation,
                ).order_by("pk").values_list(
                    "pk",
                    flat=True,
                )[:QUERY_CHUNK_SIZE])
                if not row_ids:
                    break
                related.filter(pk__in=row_ids).delete()
                deadline.checkpoint()

    def _capture_once(
        self,
        job,
        lease,
        heartbeat,
        stop_requested,
        media_root,
        staging,
    ):
        generation, pending = self._plan_candidate(lease, heartbeat)
        if pending is None:
            return generation
        new_candidate = generation is not None
        if generation is None:
            generation = pending
            current = ExportJob.objects.using(self.using).get(pk=lease.job_id)
            self._require_candidate(current, generation, lease)
            receipt = _directory_receipt_from_job(current)
            if (
                receipt is None
                or current.items.filter(snapshot_generation=generation).exists()
                or current.blobs.filter(snapshot_generation=generation).exists()
            ):
                return None
            directory = open_receipted_directory(
                staging,
                current.candidate_snapshot_relative_path,
                receipt,
            )
        else:
            directory = self._prepare_directory(staging, generation, job.pk)
        try:
            if new_candidate:
                self._record_directory(
                    lease,
                    generation,
                    directory,
                    heartbeat,
                )
            try:
                snapshot_at, included, excluded = self._record_metadata(
                    lease,
                    generation,
                    directory,
                    heartbeat,
                )
            except DatabaseFenceBusy:
                raise _MetadataFenceBusy()
            space_budget, observed_sizes, reference_counts = (
                self._candidate_space_budget(
                    lease,
                    generation,
                    media_root,
                )
            )
            verify_space(
                directory.parent.parent.descriptor,
                space_budget.required_bytes,
            )
            self._fault("before_first_child", generation=generation)
            heartbeat.renew_now(lease)
            self._capture_blobs(
                lease,
                generation,
                media_root,
                directory,
                space_budget,
                observed_sizes,
                reference_counts,
                heartbeat,
                stop_requested,
            )
            self._finalize(
                lease,
                generation,
                directory,
                snapshot_at,
                included,
                excluded,
                heartbeat,
                stop_requested,
            )
            return generation
        finally:
            directory.close()

    def capture(self, job, lease, heartbeat, stop_requested):
        confirmed_generation = ExportJob.objects.using(self.using).filter(
            pk=lease.job_id,
        ).values_list("snapshot_generation", flat=True).get()
        if confirmed_generation is not None:
            return confirmed_generation
        export_root = open_export_root(
            settings.PINRY_EXPORT_ROOT,
            os.getuid(),
            os.getgid(),
        )
        staging = None
        media_root = None
        try:
            media_root = open_verified_media_root(settings.MEDIA_ROOT)
            staging = _open_staging_directory(export_root)
            current_for_space = ExportJob.objects.using(self.using).get(
                pk=lease.job_id,
            )
            budget, observed_sizes = self._space_budget(
                current_for_space,
                media_root,
                lease,
            )
            verify_space(export_root.descriptor, budget.required_bytes)
            reuse_prepared = False
            while True:
                self._raise_if_stopped(stop_requested, lease)
                current = ExportJob.objects.using(self.using).get(pk=job.pk)
                if current.snapshot_generation is not None:
                    return current.snapshot_generation
                if (
                    current.candidate_snapshot_generation is not None
                    and not reuse_prepared
                ):
                    try:
                        self._cleanup_candidate(
                            staging,
                            lease,
                            heartbeat,
                            stop_requested,
                        )
                    except _CleanupFenceBusy:
                        self._wait_for_cleanup_retry(
                            heartbeat,
                            stop_requested,
                            lease,
                        )
                        continue
                    reuse_prepared = False
                restart = False
                try:
                    with media_global_writer_gate(media_root):
                        result = self._capture_once(
                            current,
                            lease,
                            heartbeat,
                            stop_requested,
                            media_root,
                            staging,
                        )
                        if result is not None:
                            return result
                        restart = True
                except MediaLifecycleLockError as error:
                    if error.code != "media_lifecycle_busy":
                        raise
                    self._wait(heartbeat, stop_requested, lease)
                    continue
                except DatabaseFenceBusy:
                    restart = True
                except _MetadataFenceBusy:
                    reuse_prepared = True
                    self._wait(heartbeat, stop_requested, lease)
                    continue
                if restart:
                    try:
                        self._cleanup_candidate(
                            staging,
                            lease,
                            heartbeat,
                            stop_requested,
                        )
                    except _CleanupFenceBusy:
                        self._wait_for_cleanup_retry(
                            heartbeat,
                            stop_requested,
                            lease,
                        )
                        continue
                    reuse_prepared = False
                    self._wait_for_cleanup_retry(
                        heartbeat,
                        stop_requested,
                        lease,
                    )
        except ExportStorageError as error:
            raise ExportError(error.code, lease) from None
        finally:
            if media_root is not None:
                media_root.close()
            if staging is not None:
                staging.close()
            export_root.close()


__all__ = ("SnapshotService", "detect_image_mime")
