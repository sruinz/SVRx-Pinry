from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import uuid
import zipfile

from django.db import connection
from django.test import TransactionTestCase
from django.utils import timezone
import mock

from core.models import Pin
from core.services.database_fence import DatabaseFenceBusy
from exports.contracts import ExportError, LeaseToken, StopRequested
from exports.models import (
    ExportAttempt,
    ExportAttemptFile,
    ExportBlob,
    ExportItem,
    ExportJob,
    ExportTarget,
    ExportWorkerLease,
)
from exports.services import archive as archive_services
from exports.services.archive import ArchiveService
from exports.services.file_ops import (
    ClosedFileReceipt,
    remove_if_receipt_matches,
)
from exports.services.snapshot import SnapshotService

from .helpers import ExportStorageMixin, create_export_pin, create_export_user


class _Rotation(object):
    def __init__(self, heartbeat):
        self.heartbeat = heartbeat

    def replace(self, lease):
        self.heartbeat.lease = lease

    def clear(self):
        self.heartbeat.lease = None


class ArchiveHeartbeat(object):
    def __init__(self):
        self.guard_depth = 0
        self.token_depth = 0
        self.pulses = 0
        self.lease = None
        self._lock = threading.RLock()

    @contextmanager
    def foreground_write_guard(self):
        with self._lock:
            self.guard_depth += 1
            try:
                yield
            finally:
                self.guard_depth -= 1

    @contextmanager
    def job_token_transition(self, lease):
        with self._lock:
            self.token_depth += 1
            try:
                self.lease = lease
                yield _Rotation(self)
            finally:
                self.token_depth -= 1

    def renew_now(self, lease):
        self.lease = lease
        self.pulses += 1

    def __call__(self):
        self.pulses += 1


class FakeMonotonic(object):
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class ArchiveServiceTests(ExportStorageMixin, TransactionTestCase):
    def setUp(self):
        super(ArchiveServiceTests, self).setUp()
        staging = Path(self._export_directory.name, ".staging")
        staging.mkdir(mode=0o700)
        os.chmod(str(staging), 0o700)
        self.owner = create_export_user("archive-owner")
        self.other = create_export_user("archive-other")
        self.worker_uuid = uuid.uuid4()
        self.job_uuid = uuid.uuid4()
        ExportWorkerLease.objects.create(
            pk=1,
            generation=9,
            lease_uuid=self.worker_uuid,
            health_state="ready",
            heartbeat_at=timezone.now(),
        )

    def _snapshot(self, pins):
        sizes = [os.path.getsize(pin.image.image.path) for pin in pins]
        job = ExportJob.objects.create(
            owner=self.owner,
            scope="pins",
            state="snapshotting",
            requested_total=len(pins),
            target_total=len(pins),
            included_total=len(pins),
            bytes_total=sum(sizes),
            worker_generation=9,
            lease_uuid=self.job_uuid,
            attempt_generation=0,
        )
        ExportTarget.objects.bulk_create([
            ExportTarget(
                job=job,
                position=position,
                pin_id=pin.pk,
                pin_owner_id_snapshot=pin.submitter_id,
                pin_published_at_snapshot=pin.published,
            )
            for position, pin in enumerate(pins)
        ])
        lease = LeaseToken(
            9, self.worker_uuid, job.pk, self.job_uuid, 0,
        )
        heartbeat = ArchiveHeartbeat()
        heartbeat.lease = lease
        with mock.patch("exports.services.file_ops._normalize_metadata"):
            SnapshotService().capture(
                job, lease, heartbeat, lambda: False,
            )
        job.refresh_from_db()
        return job, lease, heartbeat

    def _archive_path(self, job):
        return Path(self._export_directory.name, job.ready_relative_path)

    def _large_pin(self, filename):
        pin = create_export_pin(self.owner, filename=filename)
        path = Path(pin.image.image.path)
        content = path.read_bytes() + b"x" * (1024 * 1024 + 17)
        path.write_bytes(content)
        asset = pin.image.media_asset
        asset.content_sha256 = hashlib.sha256(content).hexdigest()
        asset.save(update_fields=("content_sha256",))
        return pin

    @staticmethod
    def _in_widths(sql):
        widths = []
        for body in re.findall(r"\bIN\s*\(([^()]*)\)", sql, re.I):
            widths.append(body.count("%s") + body.count("?"))
        return [width for width in widths if width]

    def _bulk_finalization_fixture(self, count, initially_excluded=0):
        blob_size = 7
        exported_at = timezone.now()
        generation = uuid.uuid4()
        digest = "a" * 64
        seed = create_export_pin(
            self.other, private=True, filename="bulk-revoked.png",
        )
        remaining = count - 1
        for start in range(0, remaining, 400):
            batch_size = min(400, remaining - start)
            Pin.objects.bulk_create([
                Pin(
                    submitter=self.other,
                    image_id=seed.image_id,
                    private=True,
                )
                for unused in range(batch_size)
            ], batch_size=400)
        pin_ids = list(Pin.objects.filter(
            submitter=self.other, image_id=seed.image_id,
        ).order_by("pk").values_list("pk", flat=True))
        self.assertEqual(len(pin_ids), count)
        included = count - initially_excluded
        job = ExportJob.objects.create(
            owner=self.owner,
            scope="pins",
            state="verifying",
            requested_total=count,
            target_total=count,
            snapshot_done=count,
            archive_total=included,
            archive_done=included,
            included_total=included,
            excluded_total=initially_excluded,
            excluded_permission_revoked_total=initially_excluded,
            bytes_total=included * blob_size,
            bytes_done=included * blob_size,
            worker_generation=9,
            lease_uuid=self.job_uuid,
            attempt_generation=0,
            verifying_attempt_generation=0,
            verifying_size=123,
            verifying_sha256=digest,
            verifying_completed_at=exported_at,
        )
        blob = ExportBlob.objects.create(
            job=job,
            snapshot_generation=generation,
            source_media_asset_id=seed.image.media_asset.pk,
            source_image_id=seed.image_id,
            source_relative_path="bulk/source.png",
            expected_sha256=digest,
            file_state="closed",
            snapshot_relative_path="bulk/snapshot.png",
            capture_method="copy",
            mime_type="image/png",
            size=blob_size,
            receipt_dev=1,
            receipt_ino=2,
            receipt_uid=3,
            receipt_gid=4,
            receipt_mode=0o600,
            receipt_nlink=1,
            receipt_mtime_ns=5,
            receipt_ctime_ns=6,
            receipt_sha256=digest,
            confirmed=True,
        )
        item_ids = []
        for start in range(0, count, 400):
            items = []
            for position, pin_id in enumerate(
                pin_ids[start:start + 400], start=start,
            ):
                item = ExportItem(
                    job=job,
                    target_position=position,
                    snapshot_generation=generation,
                    blob=blob,
                    pin_id=pin_id,
                    pin_owner_id=self.other.pk,
                    owner_username=self.other.username,
                    is_public=False,
                    published_at=exported_at,
                    original_filename="bulk-revoked.png",
                )
                items.append(item)
                item_ids.append(item.pk)
            ExportItem.objects.bulk_create(items, batch_size=400)
        if initially_excluded:
            stale_ids = item_ids[:initially_excluded]
            ExportItem.objects.filter(pk__in=stale_ids).update(
                inclusion_state="excluded",
                exclusion_reason="permission_revoked",
            )
        attempt = ExportAttempt.objects.create(
            job=job,
            attempt_generation=0,
            lease_uuid=self.job_uuid,
            state="verifying",
            relative_path="attempts/{}/0".format(job.pk),
            exported_at=exported_at,
        )
        candidate = ExportAttemptFile.objects.create(
            attempt=attempt,
            kind="archive",
            state="ready_candidate",
            receipt_level="full",
            relative_path="attempts/{}/0/archive.zip".format(job.pk),
            receipt_dev=11,
            receipt_ino=12,
            receipt_uid=13,
            receipt_gid=14,
            receipt_mode=0o600,
            receipt_nlink=1,
            receipt_size=123,
            receipt_mtime_ns=15,
            receipt_ctime_ns=16,
            receipt_sha256=digest,
        )
        lease = LeaseToken(
            9, self.worker_uuid, job.pk, self.job_uuid, 0,
        )
        heartbeat = ArchiveHeartbeat()
        heartbeat.lease = lease
        return {
            "job": job,
            "attempt": attempt,
            "candidate": candidate,
            "exported_at": exported_at,
            "lease": lease,
            "heartbeat": heartbeat,
            "item_ids": item_ids,
            "blob_size": blob_size,
        }

    def _bulk_blob_cleanup_fixture(self, count, include_shared=True):
        generation = uuid.uuid4()
        digest = "b" * 64
        snapshot_name = "snapshot-{}".format(generation)
        snapshot_path = Path(
            self._export_directory.name, ".staging", snapshot_name,
        )
        snapshot_path.mkdir(mode=0o700)
        os.chmod(str(snapshot_path), 0o700)
        receipt = os.stat(str(snapshot_path))
        shared_item_count = 2 if include_shared else 0
        requested = count + shared_item_count
        included = 1 if include_shared else 0
        excluded = requested - included
        job = ExportJob.objects.create(
            owner=self.owner,
            scope="pins",
            state="archiving",
            requested_total=requested,
            target_total=requested,
            snapshot_done=requested,
            archive_total=included,
            included_total=included,
            excluded_total=excluded,
            excluded_permission_revoked_total=excluded,
            bytes_total=included * 7,
            worker_generation=9,
            lease_uuid=self.job_uuid,
            attempt_generation=0,
            snapshot_generation=generation,
            snapshot_relative_path=snapshot_name,
            snapshot_dir_dev=receipt.st_dev,
            snapshot_dir_ino=receipt.st_ino,
            snapshot_dir_uid=receipt.st_uid,
            snapshot_dir_gid=receipt.st_gid,
            snapshot_dir_mode=receipt.st_mode & 0o777,
        )
        blob_ids = []
        for start in range(0, count, 400):
            blobs = []
            for position in range(start, min(start + 400, count)):
                blob = ExportBlob(
                    job=job,
                    snapshot_generation=generation,
                    source_media_asset_id=position + 1,
                    source_image_id=position + 1,
                    source_relative_path="source/{}.png".format(position),
                    expected_sha256=digest,
                    file_state="closed",
                    capture_method="copy",
                    mime_type="image/png",
                    size=7,
                    receipt_dev=101,
                    receipt_ino=position + 201,
                    receipt_uid=301,
                    receipt_gid=401,
                    receipt_mode=0o600,
                    receipt_nlink=1,
                    receipt_mtime_ns=501,
                    receipt_ctime_ns=601,
                    receipt_sha256=digest,
                    confirmed=True,
                )
                blob.snapshot_relative_path = str(blob.pk)
                blobs.append(blob)
                blob_ids.append(blob.pk)
            ExportBlob.objects.bulk_create(blobs, batch_size=400)
            ExportItem.objects.bulk_create([
                ExportItem(
                    job=job,
                    target_position=position,
                    snapshot_generation=generation,
                    blob_id=blob_ids[position],
                    pin_id=position + 1,
                    pin_owner_id=self.other.pk,
                    owner_username=self.other.username,
                    is_public=False,
                    published_at=timezone.now(),
                    original_filename="excluded.png",
                    inclusion_state="excluded",
                    exclusion_reason="permission_revoked",
                )
                for position in range(start, min(start + 400, count))
            ], batch_size=400)
        shared_blob = None
        if include_shared:
            shared_blob = ExportBlob.objects.create(
                job=job,
                snapshot_generation=generation,
                source_media_asset_id=count + 1,
                source_image_id=count + 1,
                source_relative_path="source/shared.png",
                expected_sha256=digest,
                file_state="closed",
                snapshot_relative_path="shared",
                capture_method="copy",
                mime_type="image/png",
                size=7,
                receipt_dev=101,
                receipt_ino=count + 201,
                receipt_uid=301,
                receipt_gid=401,
                receipt_mode=0o600,
                receipt_nlink=1,
                receipt_mtime_ns=501,
                receipt_ctime_ns=601,
                receipt_sha256=digest,
                confirmed=True,
            )
            ExportItem.objects.bulk_create([
                ExportItem(
                    job=job,
                    target_position=count,
                    snapshot_generation=generation,
                    blob=shared_blob,
                    pin_id=count + 1,
                    pin_owner_id=self.other.pk,
                    owner_username=self.other.username,
                    is_public=False,
                    published_at=timezone.now(),
                    original_filename="shared-excluded.png",
                    inclusion_state="excluded",
                    exclusion_reason="permission_revoked",
                ),
                ExportItem(
                    job=job,
                    target_position=count + 1,
                    snapshot_generation=generation,
                    blob=shared_blob,
                    pin_id=count + 2,
                    pin_owner_id=self.owner.pk,
                    owner_username=self.owner.username,
                    is_public=True,
                    published_at=timezone.now(),
                    original_filename="shared-included.png",
                ),
            ], batch_size=400)
        lease = LeaseToken(
            9, self.worker_uuid, job.pk, self.job_uuid, 0,
        )
        heartbeat = ArchiveHeartbeat()
        heartbeat.lease = lease
        return {
            "job": job,
            "lease": lease,
            "heartbeat": heartbeat,
            "blob_ids": blob_ids,
            "shared_blob": shared_blob,
            "snapshot_path": snapshot_path,
        }

    def _retiring_attempt_with_quarantine(self):
        pin = create_export_pin(self.owner, filename="cleanup-children.png")
        job, lease, heartbeat = self._snapshot((pin,))
        ready = Path(self._export_directory.name, "ready")
        ready.mkdir(mode=0o700, exist_ok=True)
        os.chmod(str(ready), 0o700)
        collision = ready / "{}.zip".format(job.pk)
        collision.write_bytes(b"cleanup collision")
        os.chmod(str(collision), 0o600)

        def stop_after_ready(point, context):
            del context
            if point == "after_ready_candidate":
                raise StopRequested(lease)

        with self.assertRaises(StopRequested):
            ArchiveService(
                fault_injector=stop_after_ready,
            ).build_and_publish(
                job, lease, heartbeat, lambda: False,
            )
        attempt = job.attempts.get(attempt_generation=0)
        archive = attempt.files.get(kind="archive")
        quarantine = attempt.files.get(kind="quarantine")
        ArchiveService()._mark_current_attempt_retiring(lease, heartbeat)
        attempt.refresh_from_db()
        archive.refresh_from_db()
        quarantine.refresh_from_db()
        self.assertEqual(attempt.state, "retiring")
        self.assertEqual(archive.state, "retiring")
        self.assertEqual(quarantine.state, "closed")
        return job, attempt, archive, quarantine, lease, heartbeat

    def test_build_preserves_original_and_writes_pin_specific_xmp_manifest(self):
        first = create_export_pin(self.owner, filename="shared.PNG")
        second = first.__class__.objects.create(
            submitter=self.owner,
            image=first.image,
            description="두 번째 설명",
        )
        first.description = "첫 번째 설명"
        first.save(update_fields=("description",))
        first.tags.add("첫째")
        second.tags.add("둘째")
        original = Path(first.image.image.path).read_bytes()
        original_hash = hashlib.sha256(original).hexdigest()
        job, lease, heartbeat = self._snapshot((first, second))

        completed = ArchiveService().build_and_publish(
            job, lease, heartbeat, lambda: False,
        )

        self.assertEqual(completed.state, "complete")
        self.assertEqual(completed.archive_done, 2)
        self.assertEqual(Path(first.image.image.path).read_bytes(), original)
        with zipfile.ZipFile(self._archive_path(completed)) as archive:
            self.assertIsNone(archive.testzip())
            items = list(completed.items.order_by("target_position"))
            expected_names = [
                items[0].archive_image_path,
                items[0].archive_xmp_path,
                items[1].archive_image_path,
                items[1].archive_xmp_path,
                "manifest.json",
            ]
            self.assertEqual(archive.namelist(), expected_names)
            self.assertEqual(
                hashlib.sha256(archive.read(expected_names[0])).hexdigest(),
                original_hash,
            )
            self.assertEqual(archive.read(expected_names[0]), archive.read(expected_names[2]))
            self.assertNotEqual(archive.read(expected_names[1]), archive.read(expected_names[3]))
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        self.assertEqual(
            [item["description"] for item in manifest["items"]],
            ["첫 번째 설명", "두 번째 설명"],
        )
        self.assertEqual(manifest["exported_at"], completed.completed_at.strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ",
        ))

    def test_final_permission_revocation_retires_candidate_and_rebuilds(self):
        safe = create_export_pin(self.owner, filename="safe.png")
        revoked = create_export_pin(
            self.other, private=False, filename="revoked-secret.png",
        )
        revoked.description = "revoked secret metadata"
        revoked.save(update_fields=("description",))
        job, lease, heartbeat = self._snapshot((safe, revoked))
        revoked_item = job.items.get(pin_id=revoked.pk)
        revoked_blob = revoked_item.blob
        revoked_blob_path = Path(
            self._export_directory.name, ".staging",
            revoked_blob.snapshot_relative_path,
        )
        fired = []

        def revoke(point, context):
            del context
            if point == "before_final_permission_check" and not fired:
                revoked.private = True
                revoked.save(update_fields=("private",))
                fired.append(True)

        completed = ArchiveService(fault_injector=revoke).build_and_publish(
            job, lease, heartbeat, lambda: False,
        )

        completed.refresh_from_db()
        self.assertEqual(completed.state, "complete")
        self.assertEqual(completed.excluded_permission_revoked_total, 1)
        self.assertEqual(completed.attempt_generation, 1)
        self.assertEqual(completed.archive_done, completed.archive_total)
        revoked_blob.refresh_from_db()
        self.assertEqual(revoked_blob.cleanup_state, "cleaned")
        self.assertEqual(revoked_blob.file_state, "closed")
        self.assertIsNotNone(revoked_blob.snapshot_relative_path)
        self.assertIsNotNone(revoked_blob.receipt_ino)
        self.assertFalse(revoked_blob_path.exists())
        with zipfile.ZipFile(self._archive_path(completed)) as archive:
            self.assertNotIn(revoked_item.archive_image_path, archive.namelist())
            payload = b"".join(archive.read(name) for name in archive.namelist())
        self.assertNotIn(b"revoked secret metadata", payload)

    def test_first_permission_check_with_no_items_creates_no_attempt(self):
        revoked = create_export_pin(
            self.other, private=False, filename="only-revoked.png",
        )
        job, lease, heartbeat = self._snapshot((revoked,))
        revoked.private = True
        revoked.save(update_fields=("private",))

        with self.assertRaises(ExportError) as raised:
            ArchiveService().build_and_publish(
                job, lease, heartbeat, lambda: False,
            )

        self.assertEqual(raised.exception.code, "all_items_revoked")
        self.assertEqual(job.attempts.count(), 0)

    def test_recover_verifying_resumes_publishing_on_either_side_of_rename(self):
        for fault_point in ("after_publishing_intent", "after_candidate_rename"):
            with self.subTest(fault_point=fault_point):
                pin = create_export_pin(
                    self.owner, filename="recover-{}.png".format(fault_point),
                )
                job, lease, heartbeat = self._snapshot((pin,))

                def stop(point, context):
                    del context
                    if point == fault_point:
                        raise StopRequested(lease)

                with self.assertRaises(StopRequested):
                    ArchiveService(fault_injector=stop).build_and_publish(
                        job, lease, heartbeat, lambda: False,
                    )
                attempt = job.attempts.get()
                provenance = attempt.lease_uuid

                outcome = ArchiveService().recover_verifying(
                    lease, heartbeat, lambda: False,
                )

                attempt.refresh_from_db()
                self.assertEqual(outcome.job.state, "complete")
                self.assertEqual(attempt.lease_uuid, provenance)
                self.assertTrue(self._archive_path(outcome.job).is_file())

    def test_ready_collision_is_quarantined_once_across_publish_faults(self):
        points = (
            "after_publishing_intent",
            "after_quarantine_move",
            "after_quarantine_closed",
            "after_candidate_rename",
            "after_ready_candidate",
        )
        for point in points:
            with self.subTest(point=point):
                pin = create_export_pin(
                    self.owner, filename="collision-{}.png".format(point),
                )
                job, lease, heartbeat = self._snapshot((pin,))
                ready = Path(self._export_directory.name, "ready")
                ready.mkdir(mode=0o700, exist_ok=True)
                os.chmod(str(ready), 0o700)
                collision = ready / "{}.zip".format(job.pk)
                collision.write_bytes(b"preexisting-safe-collision")
                os.chmod(str(collision), 0o600)
                fired = []

                def stop(fault_point, context):
                    del context
                    if fault_point == point and not fired:
                        fired.append(True)
                        raise StopRequested(lease)

                service = ArchiveService(fault_injector=stop)
                with self.assertRaises(StopRequested):
                    service.build_and_publish(
                        job, lease, heartbeat, lambda: False,
                    )

                outcome = ArchiveService().recover_verifying(
                    lease, heartbeat, lambda: False,
                )
                attempt = job.attempts.get()
                quarantine = attempt.files.get(kind="quarantine")
                quarantine_path = Path(
                    self._export_directory.name, ".staging",
                    quarantine.relative_path,
                )

                self.assertEqual(outcome.job.state, "complete")
                self.assertEqual(attempt.lease_uuid, lease.job_lease_uuid)
                self.assertEqual(quarantine.state, "closed")
                self.assertEqual(
                    quarantine_path.read_bytes(),
                    b"preexisting-safe-collision",
                )
                self.assertNotEqual(
                    self._archive_path(outcome.job).read_bytes(),
                    b"preexisting-safe-collision",
                )

    def test_publishing_export_error_records_retiring_before_cleanup(self):
        pin = create_export_pin(self.owner, filename="retiring.png")
        job, lease, heartbeat = self._snapshot((pin,))

        def fail(point, context):
            del context
            if point == "after_publishing_intent":
                raise ExportError("archive_failed", lease)

        with self.assertRaises(ExportError):
            ArchiveService(fault_injector=fail).build_and_publish(
                job, lease, heartbeat, lambda: False,
            )

        attempt = job.attempts.get()
        self.assertEqual(attempt.state, "retiring")
        self.assertEqual(attempt.files.get(kind="archive").state, "retiring")

    def test_writing_fault_retires_open_receipt_and_rebuilds_with_next_generation(self):
        pin = self._large_pin("writing-fault.png")
        job, lease, heartbeat = self._snapshot((pin,))
        chunks = []

        def stop(point, context):
            if point == "after_archive_chunk":
                chunks.append((
                    context["chunk_size"],
                    context["bytes_written"],
                ))
                raise StopRequested(lease)

        with self.assertRaises(StopRequested):
            ArchiveService(fault_injector=stop).build_and_publish(
                job, lease, heartbeat, lambda: False,
            )

        retired = job.attempts.get(attempt_generation=0)
        retired_file = retired.files.get(kind="archive")
        provenance = retired.lease_uuid
        self.assertEqual(chunks, [(1024 * 1024, 1024 * 1024)])
        self.assertEqual(retired.state, "writing")
        self.assertEqual(retired_file.state, "writing")
        self.assertEqual(retired_file.receipt_level, "open")
        removal_states = []
        rmdir_states = []
        original_remove = remove_if_receipt_matches
        original_rmdir = os.rmdir

        def remove_after_intent(directory, name, receipt):
            retired.refresh_from_db()
            retired_file.refresh_from_db()
            removal_states.append((retired.state, retired_file.state))
            return original_remove(directory, name, receipt)

        def rmdir_after_children(name, *args, **kwargs):
            if name == os.path.basename(retired.relative_path):
                retired_file.refresh_from_db()
                rmdir_states.append(retired_file.state)
            return original_rmdir(name, *args, **kwargs)

        with mock.patch(
            "exports.services.archive.remove_if_receipt_matches",
            side_effect=remove_after_intent,
        ), mock.patch(
            "exports.services.archive.os.rmdir",
            side_effect=rmdir_after_children,
        ):
            outcome = ArchiveService().recover_archiving(
                lease, heartbeat, lambda: False,
            )

        retired.refresh_from_db()
        retired_file.refresh_from_db()
        rebuilt = ExportAttempt.objects.exclude(pk=retired.pk).get(job=job)
        self.assertEqual(removal_states, [("retiring", "retiring")])
        self.assertEqual(rmdir_states, ["cleaned"])
        self.assertEqual(retired.state, "cleaned")
        self.assertEqual(retired_file.state, "cleaned")
        self.assertEqual(retired_file.receipt_level, "open")
        self.assertIsNone(retired_file.receipt_size)
        self.assertEqual(retired.lease_uuid, provenance)
        self.assertEqual(outcome.job.state, "complete")
        self.assertEqual(outcome.job.attempt_generation, 1)
        self.assertEqual(outcome.lease.attempt_generation, 1)
        self.assertIsNot(outcome.lease, lease)
        self.assertEqual(rebuilt.attempt_generation, 1)

    def test_archiving_recovery_retries_database_busy_outside_guard(self):
        pin = create_export_pin(self.owner, filename="recovery-busy.png")
        job, lease, heartbeat = self._snapshot((pin,))
        original_lock = archive_services.lock_current_lease
        locks = []
        sleeps = []

        def busy_once(*args, **kwargs):
            locks.append((args, kwargs))
            if len(locks) == 1:
                raise DatabaseFenceBusy()
            return original_lock(*args, **kwargs)

        def sleep_outside_guard(seconds):
            self.assertEqual(heartbeat.guard_depth, 0)
            sleeps.append(seconds)

        with mock.patch(
            "exports.services.archive.lock_current_lease",
            side_effect=busy_once,
        ):
            outcome = ArchiveService(
                sleeper=sleep_outside_guard,
            ).recover_archiving(
                lease, heartbeat, lambda: False,
            )

        self.assertEqual(outcome.job.state, "complete")
        self.assertGreater(len(locks), 1)
        self.assertEqual(sleeps, [0.01])

    def test_closed_receipt_fault_retires_without_reusing_closed_zip(self):
        pin = create_export_pin(self.owner, filename="closed-fault.png")
        job, lease, heartbeat = self._snapshot((pin,))

        def stop(point, context):
            del context
            if point == "after_archive_closed_receipt":
                raise StopRequested(lease)

        with self.assertRaises(StopRequested):
            ArchiveService(fault_injector=stop).build_and_publish(
                job, lease, heartbeat, lambda: False,
            )

        retired = job.attempts.get(attempt_generation=0)
        retired_file = retired.files.get(kind="archive")
        old_receipt = (
            retired_file.receipt_dev,
            retired_file.receipt_ino,
            retired_file.receipt_size,
            retired_file.receipt_mtime_ns,
            retired_file.receipt_ctime_ns,
            retired_file.receipt_sha256,
        )
        provenance = retired.lease_uuid
        self.assertEqual(retired.state, "closed")
        self.assertEqual(retired_file.state, "closed")
        self.assertEqual(retired_file.receipt_level, "full")

        outcome = ArchiveService().recover_archiving(
            lease, heartbeat, lambda: False,
        )

        retired.refresh_from_db()
        retired_file.refresh_from_db()
        rebuilt = ExportAttempt.objects.exclude(pk=retired.pk).get(job=job)
        self.assertEqual(retired.state, "cleaned")
        self.assertEqual(retired_file.state, "cleaned")
        self.assertEqual(retired.lease_uuid, provenance)
        self.assertEqual((
            retired_file.receipt_dev,
            retired_file.receipt_ino,
            retired_file.receipt_size,
            retired_file.receipt_mtime_ns,
            retired_file.receipt_ctime_ns,
            retired_file.receipt_sha256,
        ), old_receipt)
        self.assertEqual(outcome.job.state, "complete")
        self.assertEqual(outcome.job.attempt_generation, 1)
        self.assertEqual(outcome.lease.attempt_generation, 1)
        self.assertEqual(rebuilt.attempt_generation, 1)

    def test_verifying_before_publishing_intent_recovers_with_existing_fault_boundary(self):
        pin = create_export_pin(self.owner, filename="verifying-fault.png")
        job, lease, heartbeat = self._snapshot((pin,))

        def stop(point, context):
            del context
            if point == "before_final_permission_check":
                raise StopRequested(lease)

        with self.assertRaises(StopRequested):
            ArchiveService(fault_injector=stop).build_and_publish(
                job, lease, heartbeat, lambda: False,
            )

        attempt = job.attempts.get(attempt_generation=0)
        attempt_file = attempt.files.get(kind="archive")
        provenance = attempt.lease_uuid
        exported_at = attempt.exported_at
        receipt = (
            attempt_file.receipt_dev,
            attempt_file.receipt_ino,
            attempt_file.receipt_uid,
            attempt_file.receipt_gid,
            attempt_file.receipt_mode,
            attempt_file.receipt_nlink,
            attempt_file.receipt_size,
            attempt_file.receipt_mtime_ns,
            attempt_file.receipt_sha256,
        )
        self.assertEqual(attempt.state, "verifying")
        self.assertEqual(attempt_file.state, "verifying")

        outcome = ArchiveService().recover_verifying(
            lease, heartbeat, lambda: False,
        )

        attempt.refresh_from_db()
        attempt_file.refresh_from_db()
        self.assertEqual(outcome.job.state, "complete")
        self.assertEqual(outcome.job.completed_at, exported_at)
        self.assertEqual(outcome.job.attempt_generation, 0)
        self.assertEqual(attempt.lease_uuid, provenance)
        self.assertEqual((
            attempt_file.receipt_dev,
            attempt_file.receipt_ino,
            attempt_file.receipt_uid,
            attempt_file.receipt_gid,
            attempt_file.receipt_mode,
            attempt_file.receipt_nlink,
            attempt_file.receipt_size,
            attempt_file.receipt_mtime_ns,
            attempt_file.receipt_sha256,
        ), receipt)
        self.assertEqual((
            outcome.job.ready_dev,
            outcome.job.ready_ino,
            outcome.job.ready_uid,
            outcome.job.ready_gid,
            outcome.job.ready_mode,
            outcome.job.ready_nlink,
            outcome.job.ready_size,
            outcome.job.ready_mtime_ns,
            outcome.job.ready_ctime_ns,
            outcome.job.ready_sha256,
        ), (
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
        ))

    def test_fifty_thousand_final_permission_queries_use_at_most_four_hundred_ids(self):
        fixture = self._bulk_finalization_fixture(50000)
        widths = {
            "live_pin": [],
            "included_item": [],
            "item_update": [],
            "blob_lookup": [],
        }
        unbounded_counter_queries = []

        def observe(execute, sql, params, many, context):
            upper = sql.upper()
            in_widths = self._in_widths(sql)
            if "CORE_PIN" in upper and upper.lstrip().startswith("SELECT"):
                widths["live_pin"].extend(in_widths)
            if (
                "EXPORTS_EXPORTITEM" in upper
                and "EXPORTS_EXPORTBLOB" in upper
                and upper.lstrip().startswith("SELECT")
            ):
                widths["included_item"].extend(in_widths)
                widths["blob_lookup"].extend(in_widths)
            if (
                "EXPORTS_EXPORTITEM" in upper
                and upper.lstrip().startswith("UPDATE")
            ):
                widths["item_update"].extend(in_widths)
            if (
                "EXPORTS_EXPORTITEM" in upper
                and upper.lstrip().startswith("SELECT COUNT")
            ):
                unbounded_counter_queries.append(sql)
            return execute(sql, params, many, context)

        service = ArchiveService()
        with connection.execute_wrapper(observe):
            rotated = service._final_fence(
                fixture["attempt"],
                fixture["candidate"],
                fixture["exported_at"],
                fixture["lease"],
                fixture["heartbeat"],
                lambda: False,
            )

        self.assertIsNotNone(rotated)
        for name, observed in widths.items():
            self.assertTrue(observed, name)
            self.assertLessEqual(max(observed), 400, name)
        self.assertEqual(len(widths["live_pin"]), 125)
        self.assertEqual(len(widths["included_item"]), 125)
        self.assertEqual(len(widths["item_update"]), 125)
        self.assertEqual(unbounded_counter_queries, [])

    def test_fifty_thousand_final_revocations_checkpoint_each_real_orm_batch(self):
        fixture = self._bulk_finalization_fixture(
            50000, initially_excluded=1,
        )
        events = []

        class RecordingDeadline(object):
            def __init__(self, monotonic):
                del monotonic

            def checkpoint(self):
                events.append("checkpoint")

        def observe(execute, sql, params, many, context):
            upper = sql.upper()
            if (
                "CORE_PIN" in upper
                and upper.lstrip().startswith("SELECT")
                and self._in_widths(sql)
            ):
                events.append("live_pin")
            elif (
                "EXPORTS_EXPORTITEM" in upper
                and "EXPORTS_EXPORTBLOB" in upper
                and upper.lstrip().startswith("SELECT")
                and self._in_widths(sql)
            ):
                events.append("included_item")
            elif (
                "EXPORTS_EXPORTITEM" in upper
                and upper.lstrip().startswith("UPDATE")
                and self._in_widths(sql)
            ):
                events.append("item_update")
            return execute(sql, params, many, context)

        with mock.patch(
            "exports.services.archive.DatabaseFenceDeadline",
            RecordingDeadline,
        ), connection.execute_wrapper(observe):
            rotated = ArchiveService()._final_fence(
                fixture["attempt"],
                fixture["candidate"],
                fixture["exported_at"],
                fixture["lease"],
                fixture["heartbeat"],
                lambda: False,
            )

        expected = []
        for unused in range(125):
            expected.extend(("live_pin", "checkpoint"))
        for unused in range(125):
            expected.extend((
                "included_item", "item_update", "checkpoint",
            ))
        expected.append("checkpoint")
        self.assertEqual(events, expected)
        self.assertEqual(rotated.attempt_generation, 1)
        job = ExportJob.objects.get(pk=fixture["job"].pk)
        self.assertEqual(job.included_total, 0)
        self.assertEqual(job.excluded_total, 50000)
        self.assertEqual(job.excluded_permission_revoked_total, 50000)
        self.assertEqual(job.archive_total, 0)
        self.assertEqual(job.archive_done, 0)
        self.assertEqual(job.bytes_total, 0)
        self.assertEqual(job.bytes_done, 0)
        self.assertEqual(
            job.items.filter(inclusion_state="excluded").count(),
            50000,
        )

    def test_final_revocation_deadline_rolls_back_db_and_keeps_old_tokens(self):
        fixture = self._bulk_finalization_fixture(801)
        thresholds = [5, 1]
        busy_points = []
        stop_observations = []
        sleeper_observations = []
        stop_state = {"armed": False}

        class BusyDeadline(object):
            def __init__(self, monotonic):
                del monotonic
                self.threshold = thresholds.pop(0)
                self.checkpoints = 0

            def checkpoint(self):
                self.checkpoints += 1
                if self.checkpoints == self.threshold:
                    busy_points.append(self.checkpoints)
                    raise DatabaseFenceBusy()

        def stop_requested():
            stop_observations.append((
                fixture["heartbeat"].guard_depth,
                fixture["heartbeat"].token_depth,
            ))
            return stop_state["armed"]

        def sleep_outside_guards(seconds):
            sleeper_observations.append((
                seconds,
                fixture["heartbeat"].guard_depth,
                fixture["heartbeat"].token_depth,
            ))
            stop_state["armed"] = True

        service = ArchiveService(sleeper=sleep_outside_guards)
        with mock.patch(
            "exports.services.archive.DatabaseFenceDeadline",
            BusyDeadline,
        ):
            with self.assertRaises(StopRequested) as raised:
                service._final_fence(
                    fixture["attempt"],
                    fixture["candidate"],
                    fixture["exported_at"],
                    fixture["lease"],
                    fixture["heartbeat"],
                    stop_requested,
                )

        self.assertEqual(raised.exception.lease, fixture["lease"])
        self.assertEqual(busy_points, [5, 1])
        self.assertEqual(stop_observations, [(0, 0), (0, 0)])
        self.assertEqual(sleeper_observations, [(0.01, 0, 0)])
        self.assertEqual(fixture["heartbeat"].pulses, 1)
        self.assertEqual(fixture["heartbeat"].lease, fixture["lease"])
        self.assertEqual(fixture["heartbeat"].guard_depth, 0)
        self.assertEqual(fixture["heartbeat"].token_depth, 0)
        job = ExportJob.objects.get(pk=fixture["job"].pk)
        self.assertEqual(job.state, "verifying")
        self.assertEqual(job.included_total, 801)
        self.assertEqual(job.excluded_total, 0)
        self.assertEqual(job.excluded_permission_revoked_total, 0)
        self.assertEqual(job.archive_total, 801)
        self.assertEqual(job.archive_done, 801)
        self.assertEqual(job.bytes_total, 801 * fixture["blob_size"])
        self.assertEqual(job.bytes_done, 801 * fixture["blob_size"])
        self.assertEqual(job.attempt_generation, 0)
        self.assertEqual(job.worker_generation, 9)
        self.assertEqual(job.lease_uuid, self.job_uuid)
        worker = ExportWorkerLease.objects.get(pk=1)
        self.assertEqual(worker.generation, 9)
        self.assertEqual(worker.lease_uuid, self.worker_uuid)
        fixture["attempt"].refresh_from_db()
        fixture["candidate"].refresh_from_db()
        self.assertEqual(fixture["attempt"].state, "verifying")
        self.assertEqual(fixture["candidate"].state, "ready_candidate")
        self.assertEqual(
            job.items.filter(inclusion_state="included").count(),
            801,
        )

    def test_fifty_thousand_blob_cleanup_uses_keyset_batches_of_at_most_four_hundred(self):
        fixture = self._bulk_blob_cleanup_fixture(50000)
        removed = []
        keyset_selects = []
        keyset_widths = []
        cas_selects = []
        cas_select_widths = []
        updates = []
        update_widths = []
        unbounded_selects = []

        def cooperative_remove(directory, name, receipt):
            directory.verify_identity()
            self.assertIsInstance(receipt, ClosedFileReceipt)
            self.assertEqual(fixture["heartbeat"].guard_depth, 0)
            self.assertEqual(fixture["heartbeat"].token_depth, 0)
            self.assertFalse(connection.in_atomic_block)
            removed.append(name)
            return True

        def observe(execute, sql, params, many, context):
            upper = sql.upper()
            widths = self._in_widths(sql)
            if (
                upper.lstrip().startswith("SELECT")
                and "EXPORTS_EXPORTBLOB" in upper
            ):
                if "LIMIT 400" in upper and "ORDER BY" in upper:
                    keyset_selects.append(sql)
                    keyset_widths.extend(widths)
                elif widths:
                    cas_selects.append(sql)
                    cas_select_widths.extend(widths)
                else:
                    unbounded_selects.append(sql)
            elif (
                upper.lstrip().startswith("UPDATE")
                and "EXPORTS_EXPORTBLOB" in upper
            ):
                updates.append(sql)
                update_widths.extend(widths)
            return execute(sql, params, many, context)

        with mock.patch(
            "exports.services.archive.remove_if_receipt_matches",
            side_effect=cooperative_remove,
        ), connection.execute_wrapper(observe):
            ArchiveService()._cleanup_excluded_blobs(
                fixture["lease"],
                fixture["heartbeat"],
                lambda: False,
            )

        self.assertEqual(len(keyset_selects), 126)
        self.assertTrue(all("LIMIT 400" in sql.upper() for sql in keyset_selects))
        self.assertEqual(len(cas_selects), 125)
        self.assertEqual(len(updates), 125)
        self.assertLessEqual(max(keyset_widths), 400)
        self.assertLessEqual(max(cas_select_widths), 400)
        self.assertLessEqual(max(update_widths), 400)
        self.assertEqual(unbounded_selects, [])
        self.assertEqual(len(removed), 50000)
        self.assertEqual(len(set(removed)), 50000)
        self.assertNotIn(str(fixture["shared_blob"].pk), removed)
        candidates = ExportBlob.objects.filter(job=fixture["job"]).exclude(
            pk=fixture["shared_blob"].pk,
        )
        self.assertEqual(candidates.filter(
            cleanup_state="cleaned",
            file_state="closed",
            snapshot_relative_path__isnull=False,
            receipt_dev__isnull=False,
            receipt_ino__isnull=False,
            receipt_uid__isnull=False,
            receipt_gid__isnull=False,
            receipt_mode__isnull=False,
            receipt_nlink__isnull=False,
            size__isnull=False,
            receipt_mtime_ns__isnull=False,
            receipt_ctime_ns__isnull=False,
            receipt_sha256__isnull=False,
        ).count(), 50000)
        fixture["shared_blob"].refresh_from_db()
        self.assertEqual(fixture["shared_blob"].cleanup_state, "pending")
        self.assertEqual(fixture["shared_blob"].file_state, "closed")
        self.assertEqual(fixture["shared_blob"].snapshot_relative_path, "shared")

    def test_virtual_forty_five_second_blob_cleanup_pulses_and_checks_stop_within_five_seconds(self):
        fixture = self._bulk_blob_cleanup_fixture(
            30, include_shared=False,
        )
        clock = FakeMonotonic()
        heartbeat_times = [clock()]
        stop_times = []
        stop_state = {"requested": False}

        class TimedHeartbeat(ArchiveHeartbeat):
            def renew_now(self, lease):
                self.assert_current_lease(lease)
                heartbeat_times.append(clock())
                super(TimedHeartbeat, self).renew_now(lease)

            def __call__(self):
                heartbeat_times.append(clock())
                super(TimedHeartbeat, self).__call__()

            @staticmethod
            def assert_current_lease(lease):
                if lease != fixture["lease"]:
                    raise AssertionError("cleanup renewed a stale lease")

        heartbeat = TimedHeartbeat()
        heartbeat.lease = fixture["lease"]

        def stop_requested():
            stop_times.append(clock())
            return stop_state["requested"]

        def cooperative_fault(point, context):
            if point not in (
                "before_cleanup_unlink",
                "after_cleanup_parent_fsync",
            ):
                return
            clock.advance(1.0)
            context["checkpoint"]()
            if clock() >= 45.0:
                stop_state["requested"] = True

        def cooperative_remove(directory, name, receipt):
            del name
            directory.verify_identity()
            self.assertIsInstance(receipt, ClosedFileReceipt)
            return True

        service = ArchiveService(
            monotonic=clock,
            sleeper=lambda seconds: self.fail(
                "cleanup must not use real sleep: {}".format(seconds),
            ),
            fault_injector=cooperative_fault,
        )
        with mock.patch(
            "exports.services.archive.remove_if_receipt_matches",
            side_effect=cooperative_remove,
        ):
            with self.assertRaises(StopRequested) as raised:
                service._cleanup_excluded_blobs(
                    fixture["lease"], heartbeat, stop_requested,
                )

        self.assertEqual(raised.exception.lease, fixture["lease"])
        self.assertGreaterEqual(clock(), 45.0)
        self.assertGreater(len(stop_times), 45)
        gaps = [
            later - earlier
            for earlier, later in zip(
                heartbeat_times, heartbeat_times[1:] + [clock()],
            )
        ]
        self.assertLessEqual(max(gaps), 5.0)
        self.assertEqual(heartbeat.lease, fixture["lease"])

    def test_cleanup_filesystem_calls_run_outside_foreground_token_and_database_fence(self):
        (
            job,
            attempt,
            archive,
            quarantine,
            lease,
            heartbeat,
        ) = self._retiring_attempt_with_quarantine()
        del job, quarantine
        fence_depth = {"value": 0}
        observations = []
        original_fence = archive_services.database_write_fence
        original_remove = remove_if_receipt_matches
        original_rmdir = os.rmdir

        def assert_outside(label):
            observations.append((
                label,
                heartbeat.guard_depth,
                heartbeat.token_depth,
                fence_depth["value"],
                connection.in_atomic_block,
            ))

        @contextmanager
        def tracking_fence(*args, **kwargs):
            fence_depth["value"] += 1
            try:
                with original_fence(*args, **kwargs) as current:
                    yield current
            finally:
                fence_depth["value"] -= 1

        def observe_fault(point, context):
            if point in (
                "before_cleanup_unlink",
                "after_cleanup_parent_fsync",
            ):
                self.assertTrue(callable(context["checkpoint"]))
                assert_outside(point)

        def observe_remove(directory, name, receipt):
            assert_outside("remove")
            return original_remove(directory, name, receipt)

        def observe_rmdir(name, *args, **kwargs):
            assert_outside("rmdir")
            return original_rmdir(name, *args, **kwargs)

        service = ArchiveService(fault_injector=observe_fault)
        with mock.patch(
            "exports.services.archive.database_write_fence",
            tracking_fence,
        ), mock.patch(
            "exports.services.archive.remove_if_receipt_matches",
            side_effect=observe_remove,
        ), mock.patch(
            "exports.services.archive.os.rmdir",
            side_effect=observe_rmdir,
        ):
            service._cleanup_retiring(
                attempt, archive, lease, heartbeat, lambda: False,
            )

        self.assertTrue(observations)
        self.assertTrue(all(
            guard == token == fence == 0 and not in_atomic
            for label, guard, token, fence, in_atomic in observations
        ))

    def test_retiring_cleanup_marks_attempt_cleaned_only_after_all_children(self):
        (
            job,
            attempt,
            archive,
            quarantine,
            lease,
            heartbeat,
        ) = self._retiring_attempt_with_quarantine()
        del job
        directory_cleanup_states = []
        removed = []
        original_remove = remove_if_receipt_matches
        original_rmdir = os.rmdir

        def observe_remove(directory, name, receipt):
            removed.append(name)
            return original_remove(directory, name, receipt)

        def rmdir_after_children(name, *args, **kwargs):
            directory_cleanup_states.append(list(
                attempt.files.order_by("kind").values_list(
                    "kind", "state",
                )
            ))
            return original_rmdir(name, *args, **kwargs)

        with mock.patch(
            "exports.services.archive.remove_if_receipt_matches",
            side_effect=observe_remove,
        ), mock.patch(
            "exports.services.archive.os.rmdir",
            side_effect=rmdir_after_children,
        ):
            ArchiveService()._cleanup_retiring(
                attempt, archive, lease, heartbeat, lambda: False,
            )

        attempt.refresh_from_db()
        archive.refresh_from_db()
        quarantine.refresh_from_db()
        self.assertEqual(archive.state, "cleaned")
        self.assertEqual(quarantine.state, "cleaned")
        self.assertEqual(attempt.state, "cleaned")
        self.assertEqual(len(removed), 2)
        self.assertEqual(directory_cleanup_states, [[
            ("archive", "cleaned"),
            ("quarantine", "cleaned"),
        ]])
