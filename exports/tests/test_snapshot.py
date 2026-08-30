from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import time
from types import SimpleNamespace
import uuid

from django.db import (
    OperationalError,
    close_old_connections,
    connection,
    transaction,
)
from django.db.models.query import QuerySet
from django.test import SimpleTestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
import mock

from core.services.database_fence import DatabaseFenceBusy
from django_images.file_ops import MediaLifecycleLockError
from exports.contracts import (
    ExportError,
    LeaseLost,
    LeaseToken,
    StopRequested,
)
from exports.models import (
    ExportBlob,
    ExportItem,
    ExportJob,
    ExportTarget,
    ExportWorkerLease,
)
from exports.services.file_ops import (
    DirectoryReceipt,
    ExportStorageError,
    SpaceBudget,
)
from exports.services.snapshot import SnapshotService, detect_image_mime

from .helpers import ExportStorageMixin, create_export_pin, create_export_user


class ImageMimeTests(SimpleTestCase):
    HEADERS = (
        (b"\xff\xd8\xff\xe0payload", "image/jpeg"),
        (b"\x89PNG\r\n\x1a\npayload", "image/png"),
        (b"GIF87apayload", "image/gif"),
        (b"GIF89apayload", "image/gif"),
        (b"RIFF\x10\x00\x00\x00WEBPpayload", "image/webp"),
        (b"BMpayload", "image/bmp"),
        (b"II*\x00payload", "image/tiff"),
        (b"MM\x00*payload", "image/tiff"),
        (b"II+\x00payload", "image/tiff"),
        (b"MM\x00+payload", "image/tiff"),
    )

    def test_supported_mime_is_detected_from_header_without_changing_offset(self):
        for content, expected in self.HEADERS:
            with self.subTest(expected=expected):
                with tempfile.TemporaryFile() as file_obj:
                    file_obj.write(content)
                    file_obj.flush()
                    file_obj.seek(3)

                    actual = detect_image_mime(file_obj.fileno())

                    self.assertEqual(actual, expected)
                    self.assertEqual(file_obj.tell(), 3)

    def test_unknown_or_truncated_header_fails_closed(self):
        for content in (b"", b"GIF", b"RIFF1234NOPE", b"not-an-image"):
            with self.subTest(content=content):
                with tempfile.TemporaryFile() as file_obj:
                    file_obj.write(content)
                    file_obj.flush()
                    with self.assertRaises(ExportStorageError) as raised:
                        detect_image_mime(file_obj.fileno())
                self.assertEqual(raised.exception.code, "source_unsafe")


class FakeHeartbeat(object):
    def __init__(self):
        self.guard_depth = 0
        self.renewals = []
        self.pulses = 0
        self._lock = threading.RLock()

    @contextmanager
    def foreground_write_guard(self):
        with self._lock:
            self.guard_depth += 1
            try:
                yield
            finally:
                self.guard_depth -= 1

    def renew_now(self, lease):
        self.renewals.append(lease)

    def __call__(self):
        with self._lock:
            self.pulses += 1


class SnapshotServiceTests(ExportStorageMixin, TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        super(SnapshotServiceTests, self).setUp()
        Path(self._export_directory.name, ".staging").mkdir(mode=0o700)
        os.chmod(str(Path(self._export_directory.name, ".staging")), 0o700)
        self.owner = create_export_user("snapshot-owner")
        self.other = create_export_user("snapshot-other")
        self.worker_lease_uuid = uuid.uuid4()
        self.job_lease_uuid = uuid.uuid4()
        ExportWorkerLease.objects.create(
            pk=1,
            generation=7,
            lease_uuid=self.worker_lease_uuid,
            health_state="ready",
            heartbeat_at=timezone.now(),
        )

    def _job(self, pins, owner=None, identity_overrides=None):
        owner = self.owner if owner is None else owner
        identity_overrides = identity_overrides or {}
        sizes = []
        for pin in pins:
            try:
                sizes.append(os.path.getsize(pin.image.image.path))
            except OSError:
                sizes.append(0)
        job = ExportJob.objects.create(
            owner=owner,
            scope="pins",
            state="snapshotting",
            requested_total=len(pins),
            target_total=len(pins),
            included_total=len(pins),
            bytes_total=sum(sizes),
            worker_generation=7,
            lease_uuid=self.job_lease_uuid,
            attempt_generation=0,
        )
        targets = []
        for position, pin in enumerate(pins):
            owner_id, published = identity_overrides.get(
                pin.pk,
                (pin.submitter_id, pin.published),
            )
            targets.append(ExportTarget(
                job=job,
                position=position,
                pin_id=pin.pk,
                pin_owner_id_snapshot=owner_id,
                pin_published_at_snapshot=published,
            ))
        ExportTarget.objects.bulk_create(targets)
        return job, LeaseToken(
            7,
            self.worker_lease_uuid,
            job.pk,
            self.job_lease_uuid,
            0,
        )

    @staticmethod
    def _delete_pin_row(pin_id):
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM core_pin WHERE id = %s", [pin_id])

    def _capture(self, job, lease, **kwargs):
        heartbeat = kwargs.pop("heartbeat", FakeHeartbeat())
        service = SnapshotService(**kwargs)
        with mock.patch("exports.services.file_ops._normalize_metadata"):
            generation = service.capture(job, lease, heartbeat, lambda: False)
        return generation, heartbeat

    def _candidate_path(self, relative_path):
        return Path(self._export_directory.name, ".staging", relative_path)

    @staticmethod
    def _copy_as(method, digest):
        def copy(source_fd, destination_fd, source_receipt, heartbeat,
                 stop_requested, before_copy):
            before_copy(source_receipt.size)
            data = os.pread(source_fd, source_receipt.size, 0)
            os.ftruncate(destination_fd, 0)
            os.pwrite(destination_fd, data, 0)
            os.ftruncate(destination_fd, len(data))
            heartbeat()
            if stop_requested():
                raise AssertionError("unexpected stop")
            os.fsync(destination_fd)
            return method, digest(data) if callable(digest) else digest
        return copy

    def test_capture_confirms_deduplicated_blob_and_frozen_sanitized_metadata(self):
        first = create_export_pin(
            self.owner,
            filename="shared-original.PNG",
        )
        second = first.__class__.objects.create(
            submitter=self.owner,
            image=first.image,
            description="second description",
            url="https://example.test/photo?token=secret&keep=yes#fragment",
            referer="https://example.test/source?api_key=secret",
        )
        first.tags.add("zeta", "alpha")
        second.tags.add("beta")
        job, lease = self._job((second, first))
        source_stat = os.stat(first.image.image.path)

        generation, heartbeat = self._capture(job, lease)

        job.refresh_from_db()
        items = list(job.items.order_by("target_position"))
        blob = job.blobs.get()
        snapshot_file = self._candidate_path(blob.snapshot_relative_path)
        self.assertEqual(job.snapshot_generation, generation)
        self.assertEqual(job.state, "archiving")
        self.assertEqual(job.snapshot_done, job.target_total)
        self.assertEqual(job.archive_total, 2)
        self.assertIsNone(job.candidate_snapshot_generation)
        self.assertEqual(job.blobs.count(), 1)
        self.assertTrue(blob.confirmed)
        self.assertEqual([item.pin_id for item in items], [second.pk, first.pk])
        self.assertEqual(json.loads(items[1].tags_json), ["alpha", "zeta"])
        self.assertEqual(items[0].source_url, "https://example.test/photo?keep=yes")
        self.assertTrue(items[0].source_url_redacted)
        self.assertEqual(items[0].referer_url, "https://example.test/source")
        self.assertTrue(items[0].referer_url_redacted)
        self.assertTrue(items[0].archive_image_path.startswith("originals/"))
        self.assertEqual(items[0].archive_xmp_path, items[0].archive_image_path + ".xmp")
        self.assertEqual(blob.mime_type, "image/png")
        self.assertEqual(blob.receipt_nlink, 1)
        self.assertEqual(blob.receipt_mode, 0o600)
        self.assertTrue(snapshot_file.is_file())
        self.assertEqual(snapshot_file.read_bytes(), Path(first.image.image.path).read_bytes())
        self.assertEqual(os.stat(first.image.image.path).st_nlink, 1)
        self.assertEqual(source_stat.st_ino, os.stat(first.image.image.path).st_ino)
        self.assertTrue(heartbeat.renewals)

    def test_confirmed_snapshot_recall_is_read_only(self):
        pin = create_export_pin(self.owner, filename="confirmed-recall.png")
        job, lease = self._job((pin,))
        generation, _heartbeat = self._capture(job, lease)

        with mock.patch(
            "exports.services.snapshot.open_export_root",
            side_effect=AssertionError("confirmed snapshot touched storage"),
        ):
            recalled = SnapshotService().capture(
                job,
                lease,
                FakeHeartbeat(),
                lambda: False,
            )

        self.assertEqual(recalled, generation)

    def test_foreign_deleted_and_private_targets_are_excluded_without_metadata(self):
        owned = create_export_pin(self.owner, filename="owned-safe.png")
        deleted = create_export_pin(
            self.other,
            filename="foreign-deleted-secret.png",
        )
        hidden = create_export_pin(
            self.other,
            filename="foreign-hidden-secret.png",
        )
        deleted.description = "deleted secret description"
        deleted.url = "https://secret.example/deleted?token=secret"
        deleted.save(update_fields=("description", "url"))
        hidden.description = "hidden secret description"
        hidden.save(update_fields=("description",))
        job, lease = self._job((deleted, owned, hidden))
        self._delete_pin_row(deleted.pk)
        hidden.private = True
        hidden.save(update_fields=("private",))

        self._capture(job, lease)

        job.refresh_from_db()
        serialized = "\n".join(
            str(value)
            for item in job.items.all()
            for value in (
                item.owner_username,
                item.description,
                item.source_url,
                item.original_filename,
            )
        )
        self.assertEqual(list(job.items.values_list("pin_id", flat=True)), [owned.pk])
        self.assertEqual(job.snapshot_done, 3)
        self.assertEqual(job.included_total, 1)
        self.assertEqual(job.excluded_total, 2)
        self.assertEqual(job.excluded_permission_revoked_total, 2)
        self.assertNotIn("secret", serialized)
        self.assertFalse(job.blobs.filter(
            source_image_id__in=(deleted.image_id, hidden.image_id)
        ).exists())

    def test_snapshot_revocations_preserve_initial_exclusion_counters(self):
        safe = create_export_pin(self.owner, filename="initial-safe.png")
        revoked = create_export_pin(
            self.other,
            filename="initial-revoked.png",
        )
        job, lease = self._job((safe, revoked))
        ExportJob.objects.filter(pk=job.pk).update(
            requested_total=3,
            included_total=2,
            excluded_total=1,
            excluded_not_visible_total=1,
        )
        revoked.private = True
        revoked.save(update_fields=("private",))

        self._capture(job, lease)

        job.refresh_from_db()
        self.assertEqual(job.included_total, 1)
        self.assertEqual(job.excluded_total, 2)
        self.assertEqual(job.excluded_not_visible_total, 1)
        self.assertEqual(job.excluded_permission_revoked_total, 1)
        self.assertEqual(job.bytes_total, os.path.getsize(safe.image.image.path))

    def test_owned_deleted_target_is_source_missing_and_preserves_prepared_state(self):
        pin = create_export_pin(self.owner, filename="owned-missing.png")
        job, lease = self._job((pin,))
        self._delete_pin_row(pin.pk)

        with self.assertRaises(ExportError) as raised:
            self._capture(job, lease)

        job.refresh_from_db()
        self.assertEqual(raised.exception.code, "source_missing")
        self.assertIsNone(job.snapshot_generation)
        self.assertIsNotNone(job.candidate_snapshot_generation)
        self.assertTrue(self._candidate_path(job.candidate_snapshot_relative_path).is_dir())
        self.assertEqual(job.blobs.count(), 0)

    def test_missing_target_identity_fails_closed_before_candidate_or_file(self):
        pin = create_export_pin(self.owner, filename="legacy-target.png")
        job, lease = self._job((pin,))
        job.targets.update(
            pin_owner_id_snapshot=None,
            pin_published_at_snapshot=None,
        )

        with self.assertRaises(ExportError) as raised:
            self._capture(job, lease)

        job.refresh_from_db()
        self.assertEqual(raised.exception.code, "permission_changed")
        self.assertIsNone(job.candidate_snapshot_generation)
        self.assertEqual(list(Path(
            self._export_directory.name, ".staging"
        ).iterdir()), [])

    def test_candidate_path_change_before_receipt_is_not_adopted(self):
        pin = create_export_pin(self.owner, filename="path-change.png")
        job, lease = self._job((pin,))

        def change_path(point, context):
            del context
            if point == "after_candidate_planned":
                ExportJob.objects.filter(pk=job.pk).update(
                    candidate_snapshot_relative_path="unexpected-candidate",
                )

        with self.assertRaises(ExportError) as raised:
            self._capture(job, lease, fault_injector=change_path)

        self.assertEqual(raised.exception.code, "export_storage_unsafe")
        self.assertIsNone(ExportJob.objects.get(pk=job.pk).snapshot_generation)

    def test_candidate_directory_receipt_change_before_metadata_is_not_adopted(self):
        pin = create_export_pin(self.owner, filename="receipt-change.png")
        job, lease = self._job((pin,))

        def change_receipt(point, context):
            del context
            if point == "after_directory_receipt":
                current = ExportJob.objects.get(pk=job.pk)
                ExportJob.objects.filter(pk=job.pk).update(
                    candidate_snapshot_dir_ino=(
                        current.candidate_snapshot_dir_ino + 1
                    ),
                )

        with self.assertRaises(ExportError) as raised:
            self._capture(job, lease, fault_injector=change_receipt)

        self.assertEqual(raised.exception.code, "export_storage_unsafe")
        self.assertEqual(ExportJob.objects.get(pk=job.pk).blobs.count(), 0)

    def test_deleted_job_owner_fails_before_candidate_or_file(self):
        pin = create_export_pin(self.owner, filename="deleted-owner.png")
        job, lease = self._job((pin,))
        self.owner.delete()

        with self.assertRaises(ExportError) as raised:
            self._capture(job, lease)

        job.refresh_from_db()
        self.assertEqual(raised.exception.code, "permission_changed")
        self.assertIsNone(job.owner_id)
        self.assertIsNone(job.candidate_snapshot_generation)

    def test_same_published_ownership_transfer_is_excluded(self):
        transferred = create_export_pin(
            self.owner,
            filename="transferred.png",
        )
        safe = create_export_pin(self.owner, filename="transfer-safe.png")
        job, lease = self._job((transferred, safe))
        transferred.submitter = self.other
        transferred.private = True
        transferred.save(update_fields=("submitter", "private"))

        self._capture(job, lease)

        job.refresh_from_db()
        self.assertEqual(list(job.items.values_list("pin_id", flat=True)), [safe.pk])
        self.assertEqual(job.excluded_permission_revoked_total, 1)

    def test_byte_copy_expected_hash_mismatch_is_source_changed(self):
        pin = create_export_pin(self.owner, filename="hash-mismatch.png")
        job, lease = self._job((pin,))
        clone = self._copy_as("copy", "0" * 64)

        with mock.patch("exports.services.snapshot.clone_or_copy", clone):
            with self.assertRaises(ExportError) as raised:
                self._capture(job, lease)

        job.refresh_from_db()
        blob = job.blobs.get()
        self.assertEqual(raised.exception.code, "source_changed")
        self.assertEqual(blob.file_state, "writing")
        self.assertIsNotNone(blob.part_relative_path)
        self.assertTrue(self._candidate_path(blob.part_relative_path).is_file())
        self.assertIsNone(job.snapshot_generation)

    def test_reflink_closed_receipt_keeps_sha_null(self):
        pin = create_export_pin(self.owner, filename="reflink-null.png")
        job, lease = self._job((pin,))
        clone = self._copy_as("reflink", None)

        with mock.patch("exports.services.snapshot.clone_or_copy", clone):
            self._capture(job, lease)

        blob = job.blobs.get()
        self.assertEqual(blob.capture_method, "reflink")
        self.assertIsNone(blob.receipt_sha256)

    def test_source_identity_change_after_open_receipt_is_detected(self):
        pin = create_export_pin(self.owner, filename="identity-change.png")
        job, lease = self._job((pin,))

        def mutate(point, context):
            if point == "after_blob_open_receipt":
                descriptor = os.open(pin.image.image.path, os.O_WRONLY)
                try:
                    os.pwrite(descriptor, b"X", 0)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)

        with self.assertRaises(ExportError) as raised:
            self._capture(job, lease, fault_injector=mutate)

        self.assertEqual(raised.exception.code, "source_changed")
        self.assertEqual(os.stat(pin.image.image.path).st_nlink, 1)
        self.assertIsNone(ExportJob.objects.get(pk=job.pk).snapshot_generation)

    def test_lease_loss_after_open_receipt_preserves_candidate_for_new_worker(self):
        pin = create_export_pin(self.owner, filename="lease-lost.png")
        job, lease = self._job((pin,))

        def lose_lease(point, context):
            del context
            if point == "after_blob_open_receipt":
                ExportJob.objects.filter(pk=job.pk).update(
                    lease_uuid=uuid.uuid4(),
                )

        with self.assertRaises(LeaseLost):
            self._capture(job, lease, fault_injector=lose_lease)

        job.refresh_from_db()
        blob = job.blobs.get()
        self.assertIsNone(job.snapshot_generation)
        self.assertIsNotNone(job.candidate_snapshot_generation)
        self.assertTrue(self._candidate_path(blob.part_relative_path).exists())

    def test_required_fault_points_never_adopt_partial_candidate(self):
        points = (
            "after_candidate_planned",
            "before_directory_receipt",
            "after_directory_receipt",
            "before_first_child",
            "before_blob_open_receipt",
            "after_blob_open_receipt",
            "copy_midpoint",
            "before_blob_rename",
            "after_blob_rename_before_closed",
            "before_blob_closed_receipt",
            "after_blob_closed",
            "before_progress_cas",
            "before_final_cas",
        )
        for index, expected_point in enumerate(points):
            with self.subTest(point=expected_point):
                pin = create_export_pin(
                    self.owner,
                    filename="fault-{}.png".format(index),
                )
                job, lease = self._job((pin,))

                def fail(point, context):
                    del context
                    if point == expected_point:
                        raise RuntimeError(expected_point)

                with self.assertRaisesRegex(RuntimeError, expected_point):
                    self._capture(job, lease, fault_injector=fail)
                job.refresh_from_db()
                self.assertIsNone(job.snapshot_generation)
                self.assertIsNotNone(job.candidate_snapshot_generation)

    def test_planned_candidate_without_directory_is_replaced_by_new_generation(self):
        pin = create_export_pin(self.owner, filename="planned-recovery.png")
        job, lease = self._job((pin,))
        generations = [uuid.uuid4(), uuid.uuid4()]

        def interrupt_planned(point, context):
            del context
            if point == "after_candidate_planned":
                raise RuntimeError("planned interruption")

        with self.assertRaisesRegex(RuntimeError, "planned interruption"):
            self._capture(
                job,
                lease,
                fault_injector=interrupt_planned,
                generation_factory=lambda: generations[0],
            )

        generation, _heartbeat = self._capture(
            job,
            lease,
            generation_factory=lambda: generations[1],
        )

        job.refresh_from_db()
        self.assertEqual(generation, generations[1])
        self.assertEqual(job.snapshot_generation, generations[1])

    def test_fatal_stop_and_lease_lost_do_not_cleanup_candidate(self):
        for index, kind in enumerate(("fatal", "stop", "lease")):
            with self.subTest(kind=kind):
                pin = create_export_pin(
                    self.owner,
                    filename="actor-{}-{}.png".format(kind, index),
                )
                job, lease = self._job((pin,))
                errors = {
                    "fatal": ExportError("source_unsafe", lease),
                    "stop": StopRequested(lease),
                    "lease": LeaseLost(),
                }

                def fail(point, context):
                    del context
                    if point == "after_directory_receipt":
                        raise errors[kind]

                with self.assertRaises(type(errors[kind])):
                    self._capture(job, lease, fault_injector=fail)
                job.refresh_from_db()
                self.assertIsNone(job.snapshot_generation)
                self.assertIsNotNone(job.candidate_snapshot_generation)
                self.assertTrue(self._candidate_path(
                    job.candidate_snapshot_relative_path
                ).is_dir())

    def test_open_closed_and_progress_busy_cleanup_then_use_new_generation(self):
        busy_points = (
            "before_blob_open_receipt",
            "before_blob_closed_receipt",
            "before_progress_cas",
        )
        for index, busy_point in enumerate(busy_points):
            with self.subTest(point=busy_point):
                pin = create_export_pin(
                    self.owner,
                    filename="busy-{}.png".format(index),
                )
                job, lease = self._job((pin,))
                generations = [uuid.uuid4(), uuid.uuid4()]
                planned = []
                armed = [False]
                contended = [False]
                locker_errors = []
                from exports.services import snapshot as snapshot_module
                original_window = (
                    snapshot_module._database_write_fence_window
                )

                def inject(point, context):
                    if point == "after_candidate_planned":
                        planned.append(context["generation"])
                    if point == busy_point and not contended[0]:
                        armed[0] = True

                @contextmanager
                def contend(database_connection):
                    if not armed[0] or contended[0]:
                        with original_window(database_connection) as result:
                            yield result
                        return
                    contended[0] = True
                    acquired = threading.Event()
                    release = threading.Event()

                    def hold_database_write():
                        close_old_connections()
                        try:
                            with transaction.atomic():
                                ExportWorkerLease.objects.filter(pk=1).update(
                                    heartbeat_at=timezone.now(),
                                )
                                acquired.set()
                                if not release.wait(5):
                                    raise AssertionError("DB lock release timeout")
                        except BaseException as error:
                            locker_errors.append(error)
                            acquired.set()
                        finally:
                            close_old_connections()

                    locker = threading.Thread(target=hold_database_write)
                    locker.start()
                    self.assertTrue(acquired.wait(5))
                    try:
                        with original_window(database_connection) as result:
                            yield result
                    finally:
                        release.set()
                        locker.join(5)
                        self.assertFalse(locker.is_alive())

                with mock.patch(
                    "exports.services.snapshot._database_write_fence_window",
                    contend,
                ):
                    generation, _heartbeat = self._capture(
                        job,
                        lease,
                        fault_injector=inject,
                        generation_factory=lambda: generations.pop(0),
                        sleeper=lambda seconds: None,
                    )

                job.refresh_from_db()
                self.assertEqual(locker_errors, [])
                self.assertTrue(contended[0])
                self.assertEqual(len(planned), 2)
                self.assertNotEqual(planned[0], planned[1])
                self.assertEqual(generation, planned[1])
                old_name = "snapshot-{}-{}".format(job.pk, planned[0])
                self.assertFalse(self._candidate_path(old_name).exists())
                self.assertEqual(job.snapshot_generation, planned[1])

    def test_legacy_db_to_gate_contention_does_not_escape_cleanup(self):
        pin = create_export_pin(self.owner, filename="legacy-db-gate.png")
        job, lease = self._job((pin,))
        generations = [uuid.uuid4(), uuid.uuid4()]
        started = threading.Event()
        gate_acquired = threading.Event()
        legacy_errors = []
        legacy_thread = [None]
        from exports.services import snapshot as snapshot_module
        legacy_root = snapshot_module.open_verified_media_root(
            os.path.realpath(self._media_directory.name),
        )

        def hold_db_then_gate():
            close_old_connections()
            try:
                with transaction.atomic():
                    ExportWorkerLease.objects.filter(pk=1).update(
                        heartbeat_at=timezone.now(),
                    )
                    started.set()
                    while True:
                        try:
                            with snapshot_module.media_global_writer_gate(
                                legacy_root,
                            ):
                                gate_acquired.set()
                                time.sleep(0.08)
                                break
                        except MediaLifecycleLockError as error:
                            if error.code != "media_lifecycle_busy":
                                raise
                            time.sleep(0.005)
            except BaseException as error:
                legacy_errors.append(error)
                started.set()
            finally:
                close_old_connections()

        def start_legacy(point, context):
            del context
            if point != "before_blob_open_receipt" or legacy_thread[0] is not None:
                return
            legacy_thread[0] = threading.Thread(target=hold_db_then_gate)
            legacy_thread[0].start()
            if not started.wait(5):
                raise AssertionError("legacy DB writer start timeout")

        try:
            generation, _heartbeat = self._capture(
                job,
                lease,
                fault_injector=start_legacy,
                generation_factory=lambda: generations.pop(0),
            )
            legacy_thread[0].join(5)
        finally:
            legacy_root.close()

        self.assertFalse(legacy_thread[0].is_alive())
        self.assertEqual(legacy_errors, [])
        self.assertTrue(gate_acquired.is_set())
        self.assertIsNotNone(generation)
        self.assertEqual(ExportJob.objects.get(pk=job.pk).snapshot_generation,
                         generation)

    def test_blob_start_and_rename_lease_checks_use_short_cas(self):
        pin = create_export_pin(self.owner, filename="lease-call-sites.png")
        job, lease = self._job((pin,))
        waiting = [None]
        observed = []

        def track_call_site(point, context):
            del context
            before = {
                "before_blob_start_lease_check": "start",
                "before_blob_rename_lease_check": "rename",
            }
            after = {
                "after_blob_start_lease_check": "start",
                "after_blob_rename_lease_check": "rename",
            }
            if point in before:
                waiting[0] = before[point]
            elif point in after:
                self.assertIsNone(
                    waiting[0],
                    "{} lease check skipped short CAS".format(after[point]),
                )

        service = SnapshotService(fault_injector=track_call_site)
        original_cas = service._lease_cas

        @contextmanager
        def spy_cas(heartbeat, current_lease):
            if waiting[0] is not None:
                observed.append(waiting[0])
                waiting[0] = None
            with original_cas(heartbeat, current_lease) as current:
                yield current

        service._lease_cas = spy_cas
        with mock.patch("exports.services.file_ops._normalize_metadata"):
            service.capture(
                job,
                lease,
                FakeHeartbeat(),
                lambda: False,
            )

        self.assertEqual(observed, ["start", "rename"])

    def test_blob_lease_check_busy_restarts_candidate_outside_gate(self):
        points = (
            "before_blob_start_lease_check",
            "before_blob_rename_lease_check",
        )
        for index, busy_point in enumerate(points):
            with self.subTest(point=busy_point):
                pin = create_export_pin(
                    self.owner,
                    filename="lease-busy-{}.png".format(index),
                )
                job, lease = self._job((pin,))
                generations = [uuid.uuid4(), uuid.uuid4()]
                planned = []
                armed = [False]
                raised = [False]

                def inject(point, context):
                    if point == "after_candidate_planned":
                        planned.append(context["generation"])
                    if point == busy_point and not raised[0]:
                        armed[0] = True

                service = SnapshotService(
                    fault_injector=inject,
                    generation_factory=lambda: generations.pop(0),
                    sleeper=lambda seconds: None,
                )
                original_cas = service._lease_cas

                @contextmanager
                def busy_cas(heartbeat, current_lease):
                    if armed[0] and not raised[0]:
                        raised[0] = True
                        armed[0] = False
                        raise DatabaseFenceBusy()
                    with original_cas(heartbeat, current_lease) as current:
                        yield current

                service._lease_cas = busy_cas
                with mock.patch("exports.services.file_ops._normalize_metadata"):
                    generation = service.capture(
                        job,
                        lease,
                        FakeHeartbeat(),
                        lambda: False,
                    )

                self.assertTrue(raised[0])
                self.assertEqual(len(planned), 2)
                self.assertEqual(generation, planned[1])

    def test_metadata_deadline_restarts_outside_gate_with_new_generation(self):
        pin = create_export_pin(self.owner, filename="metadata-deadline.png")
        job, lease = self._job((pin,))
        generation_value = uuid.uuid4()
        planned = []
        readings = iter((0.0, 6.0, 10.0))

        def clock():
            return next(readings, 10.0)

        def inject(point, context):
            if point == "after_candidate_planned":
                planned.append(context["generation"])

        generation, _heartbeat = self._capture(
            job,
            lease,
            fault_injector=inject,
            generation_factory=lambda: generation_value,
            monotonic=clock,
            sleeper=lambda seconds: None,
        )

        self.assertEqual(planned, [generation_value])
        self.assertEqual(generation, generation_value)

    def test_metadata_busy_then_gate_busy_reuses_same_prepared_generation(self):
        pin = create_export_pin(self.owner, filename="prepared-reuse.png")
        job, lease = self._job((pin,))
        generation_value = uuid.uuid4()
        planned = []
        fence_busy = [False]
        gate_calls = [0]
        from exports.services import snapshot as snapshot_module
        original_fence = snapshot_module.database_write_fence
        original_gate = snapshot_module.media_global_writer_gate

        def inject(point, context):
            if point == "after_candidate_planned":
                planned.append(context["generation"])

        @contextmanager
        def fence(using, models):
            if not fence_busy[0]:
                fence_busy[0] = True
                raise DatabaseFenceBusy()
            with original_fence(using=using, models=models) as result:
                yield result

        @contextmanager
        def gate(root):
            gate_calls[0] += 1
            if gate_calls[0] == 2:
                raise MediaLifecycleLockError(
                    "media_lifecycle_busy",
                    retryable=True,
                )
            with original_gate(root) as result:
                yield result

        with mock.patch(
            "exports.services.snapshot.database_write_fence",
            fence,
        ), mock.patch(
            "exports.services.snapshot.media_global_writer_gate",
            gate,
        ):
            generation, _heartbeat = self._capture(
                job,
                lease,
                fault_injector=inject,
                generation_factory=lambda: generation_value,
                sleeper=lambda seconds: None,
            )

        self.assertTrue(fence_busy[0])
        self.assertEqual(gate_calls[0], 3)
        self.assertEqual(planned, [generation_value])
        self.assertEqual(generation, generation_value)

    def test_final_deadline_cleans_candidate_and_uses_new_generation(self):
        pin = create_export_pin(self.owner, filename="final-deadline.png")
        job, lease = self._job((pin,))
        generations = [uuid.uuid4(), uuid.uuid4()]
        planned = []

        class Deadline(object):
            created = 0
            raised = False

            def __init__(self, monotonic):
                del monotonic
                type(self).created += 1
                self.index = type(self).created

            def checkpoint(self):
                if self.index == 2 and not type(self).raised:
                    type(self).raised = True
                    raise DatabaseFenceBusy()

        def inject(point, context):
            if point == "after_candidate_planned":
                planned.append(context["generation"])

        with mock.patch(
            "exports.services.snapshot.DatabaseFenceDeadline",
            Deadline,
        ):
            generation, _heartbeat = self._capture(
                job,
                lease,
                fault_injector=inject,
                generation_factory=lambda: generations.pop(0),
                sleeper=lambda seconds: None,
            )

        self.assertTrue(Deadline.raised)
        self.assertEqual(len(planned), 2)
        self.assertEqual(generation, planned[1])

    def test_final_deadline_checks_after_job_save_before_commit(self):
        pin = create_export_pin(self.owner, filename="final-save-deadline.png")
        job, lease = self._job((pin,))
        generations = [uuid.uuid4(), uuid.uuid4()]
        expected_generations = tuple(generations)
        planned = []

        class Deadline(object):
            created = 0
            raised = False

            def __init__(self, monotonic):
                del monotonic
                type(self).created += 1
                self.index = type(self).created
                self.calls = 0

            def checkpoint(self):
                self.calls += 1
                if (
                    self.index == 2
                    and self.calls == 4
                    and not type(self).raised
                ):
                    type(self).raised = True
                    raise DatabaseFenceBusy()

        with mock.patch(
            "exports.services.snapshot.DatabaseFenceDeadline",
            Deadline,
        ):
            generation, _heartbeat = self._capture(
                job,
                lease,
                fault_injector=lambda point, context: (
                    planned.append(context["generation"])
                    if point == "after_candidate_planned"
                    else None
                ),
                generation_factory=lambda: generations.pop(0),
                sleeper=lambda seconds: None,
            )

        self.assertTrue(Deadline.raised)
        self.assertEqual(planned, list(expected_generations))
        self.assertEqual(generation, expected_generations[1])
        self.assertEqual(job.__class__.objects.get(
            pk=job.pk
        ).snapshot_generation, expected_generations[1])

    def test_partial_unfinished_copy_is_cleaned_before_restart(self):
        pin = create_export_pin(self.owner, filename="partial-copy.png")
        job, lease = self._job((pin,))
        raised = [False]

        def write_partial_then_busy(point, context):
            if point == "after_blob_open_receipt" and not raised[0]:
                raised[0] = True
                os.pwrite(context["destination_fd"], b"partial", 0)
                os.fsync(context["destination_fd"])
                raise DatabaseFenceBusy()

        generation, _heartbeat = self._capture(
            job,
            lease,
            fault_injector=write_partial_then_busy,
            sleeper=lambda seconds: None,
        )

        self.assertIsNotNone(generation)
        self.assertTrue(raised[0])

    def test_open_receipted_truncated_final_is_preserved_and_fails_closed(self):
        pin = create_export_pin(self.owner, filename="truncated-final.png")
        job, lease = self._job((pin,))
        generations = [uuid.uuid4(), uuid.uuid4()]
        planned = []
        truncated = [None]

        def truncate_final_then_busy(point, context):
            if point == "after_candidate_planned":
                planned.append(context["generation"])
                return
            if point != "before_blob_closed_receipt" or truncated[0]:
                return
            blob = context["blob"]
            self.assertGreater(blob.source_size, 0)
            os.ftruncate(context["destination_fd"], blob.source_size - 1)
            os.fsync(context["destination_fd"])
            current = ExportJob.objects.get(pk=job.pk)
            truncated[0] = self._candidate_path(
                current.candidate_snapshot_relative_path,
            ) / str(blob.pk)
            raise DatabaseFenceBusy()

        with self.assertRaises(ExportError) as raised:
            self._capture(
                job,
                lease,
                fault_injector=truncate_final_then_busy,
                generation_factory=lambda: generations.pop(0),
                sleeper=lambda seconds: None,
            )

        job.refresh_from_db()
        self.assertEqual(raised.exception.code, "export_storage_unsafe")
        self.assertEqual(len(planned), 1)
        self.assertEqual(job.candidate_snapshot_generation, planned[0])
        self.assertIsNone(job.snapshot_generation)
        self.assertTrue(truncated[0].is_file())
        self.assertEqual(
            truncated[0].stat().st_size,
            os.path.getsize(pin.image.image.path) - 1,
        )

    def test_cleanup_intent_then_lease_loss_preserves_candidate(self):
        pin = create_export_pin(self.owner, filename="cleanup-lease.png")
        job, lease = self._job((pin,))
        states = {"busy": False, "lost": False}

        def lose_after_intent(point, context):
            del context
            if point == "before_blob_open_receipt" and not states["busy"]:
                states["busy"] = True
                raise DatabaseFenceBusy()
            if (
                point == "after_candidate_cleanup_intent"
                and not states["lost"]
            ):
                states["lost"] = True
                ExportJob.objects.filter(pk=job.pk).update(
                    lease_uuid=uuid.uuid4(),
                )

        with self.assertRaises(LeaseLost):
            self._capture(
                job,
                lease,
                fault_injector=lose_after_intent,
                sleeper=lambda seconds: None,
            )

        job.refresh_from_db()
        candidate = self._candidate_path(job.candidate_snapshot_relative_path)
        self.assertTrue(states["lost"])
        self.assertTrue(candidate.is_dir())
        self.assertEqual(len(list(candidate.iterdir())), 1)

    def test_cleanup_fs_crash_is_resumed_from_missing_receipted_directory(self):
        pin = create_export_pin(self.owner, filename="cleanup-crash.png")
        job, lease = self._job((pin,))
        generations = [uuid.uuid4(), uuid.uuid4()]
        states = {"busy": False, "crashed": False}

        def crash_after_fs(point, context):
            del context
            if point == "before_blob_open_receipt" and not states["busy"]:
                states["busy"] = True
                raise DatabaseFenceBusy()
            if point == "after_candidate_cleanup_fs" and not states["crashed"]:
                states["crashed"] = True
                raise RuntimeError("cleanup fs crash")

        with self.assertRaisesRegex(RuntimeError, "cleanup fs crash"):
            self._capture(
                job,
                lease,
                fault_injector=crash_after_fs,
                generation_factory=lambda: generations.pop(0),
                sleeper=lambda seconds: None,
            )
        job.refresh_from_db()
        old_path = self._candidate_path(job.candidate_snapshot_relative_path)
        self.assertFalse(old_path.exists())
        self.assertIsNotNone(job.candidate_snapshot_generation)

        generation, _heartbeat = self._capture(
            job,
            lease,
            fault_injector=crash_after_fs,
            generation_factory=lambda: generations.pop(0),
            sleeper=lambda seconds: None,
        )

        job.refresh_from_db()
        self.assertEqual(job.snapshot_generation, generation)
        self.assertTrue(states["crashed"])

    def test_cleanup_cas_busy_retries_outside_media_gate(self):
        pin = create_export_pin(self.owner, filename="cleanup-cas-busy.png")
        job, lease = self._job((pin,))
        generations = [uuid.uuid4(), uuid.uuid4()]
        states = {
            "blob": 0,
            "intent": 0,
            "checkpoint": 0,
            "cleaned": 0,
        }

        def busy_cleanup_cas(point, context):
            del context
            if point == "before_blob_open_receipt" and states["blob"] == 0:
                states["blob"] += 1
                raise DatabaseFenceBusy()
            if (
                point == "before_candidate_cleanup_intent_cas"
                and states["intent"] < 4
            ):
                states["intent"] += 1
                raise DatabaseFenceBusy()
            if (
                point == "before_candidate_cleaned_cas"
                and states["cleaned"] < 4
            ):
                states["cleaned"] += 1
                raise DatabaseFenceBusy()
            if (
                point == "before_candidate_cleanup_checkpoint_cas"
                and states["checkpoint"] < 4
            ):
                states["checkpoint"] += 1
                raise DatabaseFenceBusy()

        generation, heartbeat = self._capture(
            job,
            lease,
            fault_injector=busy_cleanup_cas,
            generation_factory=lambda: generations.pop(0),
            sleeper=lambda seconds: None,
        )

        self.assertIsNotNone(generation)
        self.assertEqual(states, {
            "blob": 1,
            "intent": 4,
            "checkpoint": 4,
            "cleaned": 4,
        })
        self.assertGreaterEqual(heartbeat.pulses, 12)

    def test_cleanup_rmdir_name_swap_is_not_deleted(self):
        pin = create_export_pin(self.owner, filename="cleanup-swap.png")
        job, lease = self._job((pin,))
        states = {"busy": False, "swapped": False}
        replacement = [None]

        def swap_directory(point, context):
            if point == "before_blob_open_receipt" and not states["busy"]:
                states["busy"] = True
                raise DatabaseFenceBusy()
            if point == "before_candidate_rmdir" and not states["swapped"]:
                states["swapped"] = True
                directory = context["directory"]
                staging = context["staging"]
                saved_name = directory.name + ".saved"
                os.rename(
                    directory.name,
                    saved_name,
                    src_dir_fd=staging.descriptor,
                    dst_dir_fd=staging.descriptor,
                )
                os.mkdir(directory.name, mode=0o700, dir_fd=staging.descriptor)
                replacement[0] = self._candidate_path(directory.name)

        with self.assertRaises(ExportError) as raised:
            self._capture(
                job,
                lease,
                fault_injector=swap_directory,
                sleeper=lambda seconds: None,
            )

        self.assertEqual(raised.exception.code, "export_storage_unsafe")
        self.assertTrue(states["swapped"])
        self.assertTrue(replacement[0].is_dir())

    def test_cleanup_parent_fsync_error_is_normalized_and_resumable(self):
        pin = create_export_pin(self.owner, filename="cleanup-fsync.png")
        job, lease = self._job((pin,))
        states = {"busy": False, "armed": False, "failed": False}
        original_fsync = os.fsync

        def arm_fsync(point, context):
            del context
            if point == "before_blob_open_receipt" and not states["busy"]:
                states["busy"] = True
                raise DatabaseFenceBusy()
            if point == "before_candidate_rmdir":
                states["armed"] = True

        def fail_fsync(descriptor):
            if states["armed"] and not states["failed"]:
                states["failed"] = True
                raise OSError("simulated parent fsync failure")
            return original_fsync(descriptor)

        with mock.patch("exports.services.snapshot.os.fsync", fail_fsync):
            with self.assertRaises(ExportError) as raised:
                self._capture(
                    job,
                    lease,
                    fault_injector=arm_fsync,
                    sleeper=lambda seconds: None,
                )

        job.refresh_from_db()
        self.assertEqual(raised.exception.code, "export_storage_unsafe")
        self.assertTrue(states["failed"])
        self.assertIsNotNone(job.candidate_snapshot_generation)
        self.assertFalse(self._candidate_path(
            job.candidate_snapshot_relative_path
        ).exists())

        generation, _heartbeat = self._capture(
            job,
            lease,
            fault_injector=arm_fsync,
            sleeper=lambda seconds: None,
        )
        self.assertIsNotNone(generation)

    def test_stop_after_cleanup_intent_preserves_candidate(self):
        pin = create_export_pin(self.owner, filename="cleanup-stop.png")
        job, lease = self._job((pin,))
        states = {"busy": False, "stop": False}

        def request_stop(point, context):
            del context
            if point == "before_blob_open_receipt" and not states["busy"]:
                states["busy"] = True
                raise DatabaseFenceBusy()
            if point == "after_candidate_cleanup_intent":
                states["stop"] = True

        with mock.patch("exports.services.file_ops._normalize_metadata"):
            with self.assertRaises(StopRequested) as raised:
                SnapshotService(
                    fault_injector=request_stop,
                    sleeper=lambda seconds: None,
                ).capture(
                    job,
                    lease,
                    FakeHeartbeat(),
                    lambda: states["stop"],
                )

        job.refresh_from_db()
        self.assertIs(raised.exception.lease, lease)
        self.assertTrue(self._candidate_path(
            job.candidate_snapshot_relative_path
        ).is_dir())

    def test_renew_busy_cleans_candidate_and_uses_new_generation(self):
        pin = create_export_pin(self.owner, filename="renew-busy.png")
        job, lease = self._job((pin,))
        generations = [uuid.uuid4(), uuid.uuid4()]

        class BusyHeartbeat(FakeHeartbeat):
            def __init__(self):
                super(BusyHeartbeat, self).__init__()
                self.renew_calls = 0

            def renew_now(self, current_lease):
                self.renew_calls += 1
                if self.renew_calls == 1:
                    raise DatabaseFenceBusy()
                super(BusyHeartbeat, self).renew_now(current_lease)

        heartbeat = BusyHeartbeat()
        generation, _heartbeat = self._capture(
            job,
            lease,
            heartbeat=heartbeat,
            generation_factory=lambda: generations.pop(0),
            sleeper=lambda seconds: None,
        )

        self.assertIsNotNone(generation)
        self.assertEqual(heartbeat.renew_calls, 2)

    def test_copy_fallback_rechecks_space_and_preflight_includes_metadata(self):
        pin = create_export_pin(self.owner, filename="space-recheck.png")
        job, lease = self._job((pin,))
        observed = []
        source_size = os.path.getsize(pin.image.image.path)

        def record_space(descriptor, required):
            del descriptor
            observed.append(required)

        with mock.patch(
            "exports.services.snapshot.verify_space",
            record_space,
        ), mock.patch(
            "exports.services.snapshot.clone_or_copy",
            self._copy_as("copy", lambda data: hashlib.sha256(data).hexdigest()),
        ):
            SnapshotService().capture(
                job,
                lease,
                FakeHeartbeat(),
                lambda: False,
            )

        job.refresh_from_db()
        item = job.items.get()
        blob = job.blobs.get()
        payload_bytes = sum(len(str(value).encode("utf-8")) for value in (
            item.owner_username,
            item.description,
            item.tags_json,
            item.source_url,
            item.referer_url,
            item.original_filename,
            blob.source_relative_path,
        ) if value is not None)
        metadata_overhead = (
            64 * 1024
            + 32 * 1024
            + payload_bytes * 16
        )
        expected = SpaceBudget.for_export(
            source_size,
            source_size,
            metadata_overhead,
        ).required_bytes
        self.assertGreater(observed[0], source_size * 2)
        self.assertEqual(observed[1:], [expected, expected])

    def test_metadata_and_asset_change_after_preflight_recalculates_budget(self):
        pin = create_export_pin(self.owner, filename="budget-before.png")
        replacement = create_export_pin(
            self.owner,
            filename="budget-after.png",
        )
        job, lease = self._job((pin,))
        replacement_bytes = b"\x89PNG\r\n\x1a\n" + b"x" * 8192
        Path(replacement.image.image.path).write_bytes(replacement_bytes)
        observed = []
        changed = [False]
        from exports.services import snapshot as snapshot_module
        original_gate = snapshot_module.media_global_writer_gate

        def record_space(descriptor, required):
            del descriptor
            observed.append(required)

        @contextmanager
        def change_before_gate(root):
            if not changed[0]:
                changed[0] = True
                pin.__class__.objects.filter(pk=pin.pk).update(
                    image_id=replacement.image_id,
                    description="한" * 4096,
                )
            with original_gate(root) as result:
                yield result

        with mock.patch(
            "exports.services.snapshot.verify_space",
            record_space,
        ), mock.patch(
            "exports.services.snapshot.media_global_writer_gate",
            change_before_gate,
        ), mock.patch(
            "exports.services.snapshot.clone_or_copy",
            self._copy_as("reflink", None),
        ):
            SnapshotService().capture(
                job,
                lease,
                FakeHeartbeat(),
                lambda: False,
            )

        job.refresh_from_db()
        item = job.items.get()
        blob = job.blobs.get()
        payload_bytes = sum(len(str(value).encode("utf-8")) for value in (
            item.owner_username,
            item.description,
            item.tags_json,
            item.source_url,
            item.referer_url,
            item.original_filename,
            blob.source_relative_path,
        ) if value is not None)
        expected = SpaceBudget.for_export(
            len(replacement_bytes),
            len(replacement_bytes),
            64 * 1024 + 32 * 1024 + payload_bytes * 16,
        ).required_bytes
        self.assertTrue(changed[0])
        self.assertGreater(observed[1], observed[0])
        self.assertEqual(observed[1:], [expected, expected])

    def test_fifty_thousand_name_allocation_checkpoints_lease(self):
        job, lease = self._job(())
        heartbeat = FakeHeartbeat()
        blob = SimpleNamespace(mime_type="image/png")
        items = [
            SimpleNamespace(
                pk=uuid.UUID(int=0),
                pin_id=index,
                original_filename="{}.png".format(index),
                blob=blob,
            )
            for index in range(50000)
        ]

        SnapshotService()._allocate_archive_names(
            items,
            heartbeat,
            lambda: False,
            lease,
        )

        self.assertEqual(heartbeat.pulses, 125)
        self.assertEqual(items[0].archive_image_path, "originals/0.png")
        self.assertEqual(
            items[-1].archive_xmp_path,
            "originals/49999.png.xmp",
        )

    def _candidate_metadata_rows(self, job, generation, count):
        published_at = timezone.now()
        blobs = [
            ExportBlob(
                job=job,
                snapshot_generation=generation,
                source_media_asset_id=index + 1,
                source_image_id=index + 1,
                source_relative_path="bulk/{}.png".format(index),
            )
            for index in range(count)
        ]
        items = [
            ExportItem(
                job=job,
                target_position=index,
                snapshot_generation=generation,
                blob=blob,
                pin_id=index + 1,
                pin_owner_id=self.owner.pk,
                owner_username=self.owner.username,
                is_public=True,
                published_at=published_at,
                original_filename="{}.png".format(index),
            )
            for index, blob in enumerate(blobs)
        ]
        return items, blobs

    def _record_candidate_metadata(self, service, job, lease, generation,
                                   items, blobs):
        receipt = DirectoryReceipt(1, 2, os.getuid(), os.getgid(), 0o700)
        job.candidate_snapshot_generation = generation
        job.candidate_snapshot_relative_path = "snapshot-{}-{}".format(
            job.pk,
            generation,
        )
        job.candidate_snapshot_dir_dev = receipt.dev
        job.candidate_snapshot_dir_ino = receipt.ino
        job.candidate_snapshot_dir_uid = receipt.uid
        job.candidate_snapshot_dir_gid = receipt.gid
        job.candidate_snapshot_dir_mode = receipt.mode

        @contextmanager
        def metadata_fence(heartbeat, current_lease):
            del heartbeat, current_lease
            yield job

        service._metadata_fence = metadata_fence
        service._build_metadata = mock.Mock(
            return_value=(items, blobs, 0),
        )
        return service._record_metadata(
            lease,
            generation,
            SimpleNamespace(receipt=receipt),
            FakeHeartbeat(),
        )

    def test_candidate_insert_uses_real_bounded_batches(self):
        job, lease = self._job(())
        generation = uuid.uuid4()
        items, blobs = self._candidate_metadata_rows(job, generation, 801)
        service = SnapshotService()
        calls = []
        events = []
        original_bulk_create = QuerySet.bulk_create

        class Deadline(object):
            def __init__(self, monotonic):
                del monotonic

            def checkpoint(self):
                events.append(("checkpoint",))

        def record_bulk_create(queryset, objects, batch_size=None,
                               ignore_conflicts=False):
            objects = tuple(objects)
            if queryset.model in (ExportBlob, ExportItem):
                calls.append((queryset.model, len(objects), batch_size))
                events.append(("bulk", queryset.model, len(objects)))
            return original_bulk_create(
                queryset,
                objects,
                batch_size=batch_size,
                ignore_conflicts=ignore_conflicts,
            )

        with mock.patch(
            "exports.services.snapshot.DatabaseFenceDeadline",
            Deadline,
        ), mock.patch.object(
            QuerySet,
            "bulk_create",
            autospec=True,
            side_effect=record_bulk_create,
        ):
            self._record_candidate_metadata(
                service,
                job,
                lease,
                generation,
                items,
                blobs,
            )

        expected = [
            (ExportBlob, 400, 400),
            (ExportBlob, 400, 400),
            (ExportBlob, 1, 400),
            (ExportItem, 400, 400),
            (ExportItem, 400, 400),
            (ExportItem, 1, 400),
        ]
        self.assertEqual(calls, expected)
        self.assertEqual(job.blobs.count(), 801)
        self.assertEqual(job.items.count(), 801)
        for index, event in enumerate(events):
            if event[0] == "bulk":
                self.assertEqual(events[index + 1], ("checkpoint",))

    def test_fifty_thousand_candidate_batches_and_checkpoints(self):
        job, lease = self._job(())
        generation = uuid.uuid4()
        items, blobs = self._candidate_metadata_rows(
            job,
            generation,
            50000,
        )
        service = SnapshotService()
        events = []

        class Deadline(object):
            def __init__(self, monotonic):
                del monotonic

            def checkpoint(self):
                events.append(("checkpoint",))

        def record_bulk_create(queryset, objects, batch_size=None,
                               ignore_conflicts=False):
            del ignore_conflicts
            objects = tuple(objects)
            self.assertIn(queryset.model, (ExportBlob, ExportItem))
            events.append((
                "bulk",
                queryset.model,
                len(objects),
                batch_size,
            ))
            return list(objects)

        with mock.patch(
            "exports.services.snapshot.DatabaseFenceDeadline",
            Deadline,
        ), mock.patch.object(
            QuerySet,
            "bulk_create",
            autospec=True,
            side_effect=record_bulk_create,
        ):
            self._record_candidate_metadata(
                service,
                job,
                lease,
                generation,
                items,
                blobs,
            )

        bulk_events = [event for event in events if event[0] == "bulk"]
        self.assertEqual(len(bulk_events), 250)
        self.assertEqual(
            [event[1] for event in bulk_events[:125]],
            [ExportBlob] * 125,
        )
        self.assertEqual(
            [event[1] for event in bulk_events[125:]],
            [ExportItem] * 125,
        )
        self.assertTrue(all(
            event[2:] == (400, 400) for event in bulk_events
        ))
        for index, event in enumerate(events):
            if event[0] == "bulk":
                self.assertEqual(events[index + 1], ("checkpoint",))

    def test_large_candidate_db_work_is_chunked_without_n_plus_one(self):
        job, _lease = self._job(())
        generation = uuid.uuid4()
        published_at = timezone.now()
        blobs = [
            ExportBlob(
                job=job,
                snapshot_generation=generation,
                source_media_asset_id=index + 1,
                source_image_id=index + 1,
                source_relative_path="large/{}.png".format(index),
            )
            for index in range(1001)
        ]
        ExportBlob.objects.bulk_create(blobs, batch_size=400)
        ExportItem.objects.bulk_create([
            ExportItem(
                job=job,
                target_position=index,
                snapshot_generation=generation,
                blob=blob,
                pin_id=index + 1,
                pin_owner_id=self.owner.pk,
                owner_username=self.owner.username,
                is_public=True,
                published_at=published_at,
                original_filename="{}.png".format(index),
            )
            for index, blob in enumerate(blobs)
        ], batch_size=400)

        class Deadline(object):
            def __init__(self):
                self.calls = 0

            def checkpoint(self):
                self.calls += 1

        service = SnapshotService()
        current = ExportJob.objects.get(pk=job.pk)
        confirm_deadline = Deadline()
        with CaptureQueriesContext(connection) as confirm_queries:
            service._confirm_candidate_blobs(
                current,
                generation,
                confirm_deadline,
            )
        self.assertEqual(confirm_deadline.calls, 3)
        self.assertFalse(job.blobs.filter(confirmed=False).exists())

        with CaptureQueriesContext(connection) as chunk_queries:
            chunk_sizes = [
                len(chunk)
                for chunk in service._candidate_blob_chunks(
                    job.pk,
                    generation,
                )
            ]
        self.assertEqual(chunk_sizes, [400, 400, 201])
        self.assertEqual(len(chunk_queries), 4)

        delete_deadline = Deadline()
        with CaptureQueriesContext(connection) as delete_queries:
            with transaction.atomic():
                service._delete_candidate_rows(
                    current,
                    generation,
                    delete_deadline,
                )
        self.assertEqual(delete_deadline.calls, 6)
        self.assertEqual(job.items.count(), 0)
        self.assertEqual(job.blobs.count(), 0)
        self.assertLessEqual(len(delete_queries), 40)
        batched_id_reads = [
            query["sql"]
            for query in delete_queries
            if "LIMIT 400" in query["sql"]
            and (
                "exports_exportitem" in query["sql"]
                or "exports_exportblob" in query["sql"]
            )
        ]
        self.assertEqual(len(batched_id_reads), 8)

        in_clause_sizes = []
        for query in tuple(confirm_queries) + tuple(delete_queries):
            for values in re.findall(r"\bIN \(([^)]*)\)", query["sql"]):
                in_clause_sizes.append(values.count(",") + 1)
        self.assertTrue(in_clause_sizes)
        self.assertLessEqual(max(in_clause_sizes), 400)

    def test_staging_wrong_mode_fails_closed(self):
        pin = create_export_pin(self.owner, filename="staging-mode.png")
        job, lease = self._job((pin,))
        staging = Path(self._export_directory.name, ".staging")
        os.chmod(str(staging), 0o755)

        with self.assertRaises(ExportError) as raised:
            self._capture(job, lease)

        self.assertEqual(raised.exception.code, "export_storage_unsafe")

    def test_unreceipted_file_with_wrong_mode_is_not_deleted_on_busy(self):
        pin = create_export_pin(self.owner, filename="unsafe-cleanup.png")
        job, lease = self._job((pin,))

        def corrupt(point, context):
            if point == "before_blob_open_receipt":
                os.fchmod(context["destination_fd"], 0o640)
                raise DatabaseFenceBusy()

        with self.assertRaises(ExportError) as raised:
            self._capture(
                job,
                lease,
                fault_injector=corrupt,
                sleeper=lambda seconds: None,
            )

        job.refresh_from_db()
        candidate = self._candidate_path(job.candidate_snapshot_relative_path)
        self.assertEqual(raised.exception.code, "export_storage_unsafe")
        self.assertTrue(any(candidate.iterdir()))
        self.assertEqual(
            stat.S_IMODE(next(candidate.iterdir()).stat().st_mode),
            0o640,
        )

    def test_unreceipted_final_leaf_is_never_deleted(self):
        pin = create_export_pin(self.owner, filename="unsafe-final.png")
        job, lease = self._job((pin,))
        moved = [None]

        def move_before_receipt(point, context):
            if point != "before_blob_open_receipt":
                return
            current = ExportJob.objects.get(pk=job.pk)
            candidate = self._candidate_path(
                current.candidate_snapshot_relative_path
            )
            blob = context["blob"]
            part = candidate / "{}.part".format(blob.pk)
            final = candidate / str(blob.pk)
            os.rename(str(part), str(final))
            moved[0] = final
            raise DatabaseFenceBusy()

        with self.assertRaises(ExportError) as raised:
            self._capture(
                job,
                lease,
                fault_injector=move_before_receipt,
                sleeper=lambda seconds: None,
            )

        self.assertEqual(raised.exception.code, "export_storage_unsafe")
        self.assertTrue(moved[0].is_file())
        self.assertEqual(moved[0].stat().st_size, 0)

    def test_space_gate_database_and_file_operations_follow_lock_order(self):
        pin = create_export_pin(self.owner, filename="lock-order.png")
        job, lease = self._job((pin,))
        heartbeat = FakeHeartbeat()
        state = {"gate": 0}
        from exports.services import snapshot as snapshot_module
        original_gate = snapshot_module.media_global_writer_gate
        original_fence = snapshot_module.database_write_fence
        original_window = snapshot_module._database_write_fence_window
        original_verify_space = snapshot_module.verify_space
        original_create_file = snapshot_module.create_private_file_fs

        @contextmanager
        def gate(root):
            self.assertFalse(connection.in_atomic_block)
            with original_gate(root):
                state["gate"] += 1
                try:
                    yield
                finally:
                    state["gate"] -= 1

        @contextmanager
        def fence(using, models):
            self.assertEqual(state["gate"], 1)
            self.assertEqual(heartbeat.guard_depth, 1)
            with original_fence(using=using, models=models) as result:
                yield result

        @contextmanager
        def short_cas(database_connection):
            self.assertEqual(state["gate"], 1)
            self.assertEqual(heartbeat.guard_depth, 1)
            with original_window(database_connection) as result:
                yield result

        def verify_space(descriptor, required):
            self.assertEqual(heartbeat.guard_depth, 0)
            self.assertFalse(connection.in_atomic_block)
            if verify_space.calls == 0:
                self.assertEqual(state["gate"], 0)
            else:
                self.assertEqual(state["gate"], 1)
            verify_space.calls += 1
            return original_verify_space(descriptor, required)

        verify_space.calls = 0

        def create_file(directory, name):
            self.assertEqual(state["gate"], 1)
            self.assertEqual(heartbeat.guard_depth, 0)
            self.assertFalse(connection.in_atomic_block)
            return original_create_file(directory, name)

        with mock.patch(
            "exports.services.snapshot.media_global_writer_gate", gate
        ), mock.patch(
            "exports.services.snapshot.database_write_fence", fence
        ), mock.patch(
            "exports.services.snapshot._database_write_fence_window",
            short_cas,
        ), mock.patch(
            "exports.services.snapshot.verify_space", verify_space
        ), mock.patch(
            "exports.services.snapshot.create_private_file_fs", create_file
        ):
            with mock.patch("exports.services.file_ops._normalize_metadata"):
                SnapshotService().capture(
                    job,
                    lease,
                    heartbeat,
                    lambda: False,
                )

    def test_short_lease_cas_uses_nowait_worker_and_job_row_locks(self):
        pin = create_export_pin(self.owner, filename="nowait-cas.png")
        job, lease = self._job((pin,))
        observed = []
        original = QuerySet.select_for_update

        def select_for_update(queryset, *args, **kwargs):
            if queryset.model in (ExportWorkerLease, ExportJob):
                observed.append((queryset.model, kwargs.get("nowait")))
            return original(queryset, *args, **kwargs)

        with mock.patch.object(
            QuerySet,
            "select_for_update",
            select_for_update,
        ):
            with SnapshotService()._lease_cas(FakeHeartbeat(), lease) as current:
                self.assertEqual(current.pk, job.pk)

        self.assertEqual(observed, [
            (ExportWorkerLease, True),
            (ExportJob, True),
        ])

    def test_postgresql_nowait_lock_error_is_normalized_as_busy(self):
        pin = create_export_pin(self.owner, filename="nowait-busy.png")
        _job, lease = self._job((pin,))
        service = SnapshotService()

        class DriverLockError(Exception):
            pgcode = "55P03"

        lock_error = OperationalError("row lock unavailable")
        lock_error.__cause__ = DriverLockError()

        with mock.patch.object(
            connection,
            "vendor",
            "postgresql",
        ), mock.patch.object(
            service,
            "_lock_current_lease_nowait",
            side_effect=lock_error,
        ):
            with self.assertRaises(DatabaseFenceBusy):
                with service._lease_cas(FakeHeartbeat(), lease):
                    pass

    def test_adjustable_long_fs_barrier_allows_heartbeat_without_foreground_lock(self):
        pin = create_export_pin(self.owner, filename="fs-barrier.png")
        job, lease = self._job((pin,))
        heartbeat = FakeHeartbeat()
        entered = threading.Event()
        release = threading.Event()
        stopped = threading.Event()
        result = []

        def inject(point, context):
            del context
            if point == "before_directory_receipt":
                entered.set()
                if not release.wait(5):
                    raise AssertionError("FS barrier release timeout")

        def pulse():
            while not stopped.is_set():
                heartbeat()
                time.sleep(0.005)

        def capture():
            close_old_connections()
            try:
                with mock.patch("exports.services.file_ops._normalize_metadata"):
                    result.append(SnapshotService(
                        fault_injector=inject,
                    ).capture(job, lease, heartbeat, lambda: False))
            finally:
                close_old_connections()

        pulse_thread = threading.Thread(target=pulse)
        capture_thread = threading.Thread(target=capture)
        pulse_thread.start()
        capture_thread.start()
        try:
            self.assertTrue(entered.wait(5))
            before = heartbeat.pulses
            time.sleep(0.05)
            self.assertGreater(heartbeat.pulses, before)
            self.assertEqual(heartbeat.guard_depth, 0)
            release.set()
            capture_thread.join(10)
            self.assertFalse(capture_thread.is_alive())
            self.assertEqual(len(result), 1)
        finally:
            release.set()
            stopped.set()
            pulse_thread.join(5)
            capture_thread.join(5)
