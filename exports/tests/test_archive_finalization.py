from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import threading
import uuid
import zipfile

from django.test import TransactionTestCase
from django.utils import timezone
import mock

from exports.contracts import ExportError, LeaseToken, StopRequested
from exports.models import ExportJob, ExportTarget, ExportWorkerLease
from exports.services.archive import ArchiveService
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
            self.lease = lease
            yield _Rotation(self)

    def renew_now(self, lease):
        self.lease = lease
        self.pulses += 1

    def __call__(self):
        self.pulses += 1


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
