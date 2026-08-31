from datetime import timedelta
import hashlib
import os
from pathlib import Path
import uuid

from django.conf import settings
from django.test import TransactionTestCase
from django.utils import timezone
import mock

from core.services.database_fence import DatabaseFenceBusy
from exports.models import ExportJob
from exports.services.file_ops import ClosedFileReceipt, ExportStorageError
from exports.services.jobs import ExportRequestError, JobService
from exports.services.worker import (
    ExportWorker,
    LeaseHeartbeat,
    acquire_worker_lease,
    cleanup_expired_and_stale,
)

from .helpers import ExportStorageMixin, create_export_pin, create_export_user


class RetentionTests(ExportStorageMixin, TransactionTestCase):
    def setUp(self):
        super(RetentionTests, self).setUp()
        self.now = timezone.now()
        self.owner = create_export_user("retention-owner")
        self.pin = create_export_pin(self.owner, private=True)
        self.ready = Path(settings.PINRY_EXPORT_ROOT, "ready")
        self.ready.mkdir(mode=0o700)
        os.chmod(str(self.ready), 0o700)

    def _complete(self, owner, expires_at, content=b"ready", **fields):
        job_id = uuid.uuid4()
        path = self.ready / "{}.zip".format(job_id)
        path.write_bytes(content)
        os.chmod(str(path), 0o600)
        descriptor = os.open(str(path), os.O_RDONLY)
        try:
            receipt = ClosedFileReceipt.from_open_fd(
                descriptor,
                __import__(
                    "exports.services.file_ops",
                    fromlist=["OpenFileReceipt"],
                ).OpenFileReceipt.from_fd(
                    descriptor, os.getuid(), os.getgid(),
                ),
                hashlib.sha256(content).hexdigest(),
            )
        finally:
            os.close(descriptor)
        values = {
            "id": job_id,
            "owner": owner,
            "scope": "pins",
            "state": "complete",
            "completed_at": expires_at - timedelta(hours=24),
            "expires_at": expires_at,
            "staging_cleanup_state": "cleaned",
            "ready_cleanup_state": "retained",
            "ready_relative_path": "ready/{}.zip".format(job_id),
            "ready_display_name": "ready.zip",
            "ready_size": receipt.size,
            "ready_sha256": receipt.sha256,
            "ready_dev": receipt.dev,
            "ready_ino": receipt.ino,
            "ready_uid": receipt.uid,
            "ready_gid": receipt.gid,
            "ready_mode": receipt.mode,
            "ready_nlink": receipt.nlink,
            "ready_mtime_ns": receipt.mtime_ns,
            "ready_ctime_ns": receipt.ctime_ns,
        }
        values.update(fields)
        return ExportJob.objects.create(**values), path

    def _maintenance(self, now=None):
        current = self.now if now is None else now
        worker = acquire_worker_lease(current)
        heartbeat = LeaseHeartbeat(worker, clock=lambda: current)
        return cleanup_expired_and_stale(
            worker,
            heartbeat,
            lambda: False,
            now=current,
        )

    def test_complete_is_retained_before_exact_24_hour_boundary(self):
        job, path = self._complete(
            self.owner, self.now + timedelta(microseconds=1),
        )

        outcome = self._maintenance()

        job.refresh_from_db()
        self.assertEqual(job.state, "complete")
        self.assertEqual(job.ready_cleanup_state, "retained")
        self.assertTrue(path.exists())
        self.assertTrue(outcome.claim_allowed)

    def test_exact_expiry_commits_pending_before_unlink_and_cleans_receipt(self):
        job, path = self._complete(self.owner, self.now)

        outcome = self._maintenance()

        job.refresh_from_db()
        self.assertTrue(outcome.did_work)
        self.assertFalse(path.exists())
        self.assertEqual(job.state, "expired")
        self.assertEqual(job.ready_cleanup_state, "cleaned")
        self.assertIsNone(job.ready_relative_path)

    def test_new_failed_job_does_not_expire_previous_success(self):
        complete, path = self._complete(
            self.owner, self.now + timedelta(hours=1),
        )
        ExportJob.objects.create(
            owner=self.owner,
            scope="pins",
            state="failed",
            error_code="archive_failed",
            error_class="retryable",
            error_retryable=True,
            staging_cleanup_state="cleaned",
            ready_cleanup_state="absent",
        )

        self._maintenance()

        complete.refresh_from_db()
        self.assertEqual(complete.state, "complete")
        self.assertTrue(path.exists())

    def test_owner_null_complete_is_expired_and_cleaned_immediately(self):
        job, path = self._complete(
            None, self.now + timedelta(hours=24),
        )

        self._maintenance()

        self.assertFalse(ExportJob.objects.filter(pk=job.pk).exists())
        self.assertFalse(path.exists())

    def test_receipt_mismatch_preserves_file_and_blocks_claim(self):
        job, path = self._complete(self.owner, self.now)
        replacement = path.with_suffix(".replacement")
        replacement.write_bytes(b"foreign")
        os.chmod(str(replacement), 0o600)
        os.replace(str(replacement), str(path))

        outcome = self._maintenance()

        job.refresh_from_db()
        self.assertTrue(path.exists())
        self.assertEqual(path.read_bytes(), b"foreign")
        self.assertEqual(job.ready_cleanup_state, "blocked")
        self.assertFalse(outcome.claim_allowed)

    def test_transient_unlink_failure_stays_pending_and_blocks_claim(self):
        job, path = self._complete(self.owner, self.now)

        with mock.patch(
            "exports.services.worker.remove_if_receipt_matches",
            side_effect=ExportStorageError("export_storage_unsafe"),
        ):
            outcome = self._maintenance()

        job.refresh_from_db()
        self.assertTrue(path.exists())
        self.assertEqual(job.state, "expired")
        self.assertEqual(job.ready_cleanup_state, "pending")
        self.assertFalse(outcome.claim_allowed)

    def test_ready_cleanup_busy_retries_after_receipted_unlink(self):
        job, path = self._complete(self.owner, self.now)
        job.state = "expired"
        job.ready_cleanup_state = "pending"
        job.save(update_fields=("state", "ready_cleanup_state"))
        worker = acquire_worker_lease(self.now)
        heartbeat = LeaseHeartbeat(worker, clock=lambda: self.now)
        original_save = ExportJob.save
        busy = [False]
        sleeps = []

        def busy_once(instance, *args, **kwargs):
            if (
                instance.pk == job.pk
                and instance.ready_cleanup_state == "cleaned"
                and not busy[0]
            ):
                busy[0] = True
                raise DatabaseFenceBusy()
            return original_save(instance, *args, **kwargs)

        with mock.patch.object(ExportJob, "save", new=busy_once):
            outcome = cleanup_expired_and_stale(
                worker,
                heartbeat,
                lambda: False,
                now=self.now,
                sleeper=lambda seconds: sleeps.append(seconds),
            )

        job.refresh_from_db()
        self.assertTrue(busy[0])
        self.assertEqual(sleeps, [0.01])
        self.assertTrue(outcome.did_work)
        self.assertFalse(path.exists())
        self.assertEqual(job.ready_cleanup_state, "cleaned")

    def test_pending_cleanup_runs_before_active_claim_and_preserves_three_rows(self):
        expired, path = self._complete(self.owner, self.now)
        expired.state = "expired"
        expired.ready_cleanup_state = "pending"
        expired.save(update_fields=("state", "ready_cleanup_state"))
        self._complete(self.owner, self.now + timedelta(hours=1))
        active = ExportJob.objects.create(
            owner=self.owner,
            scope="pins",
            state="queued",
            requested_total=1,
            target_total=1,
            included_total=1,
        )

        outcome = self._maintenance()

        expired.refresh_from_db()
        active.refresh_from_db()
        self.assertEqual(ExportJob.objects.filter(owner=self.owner).count(), 3)
        self.assertEqual(expired.ready_cleanup_state, "cleaned")
        self.assertFalse(path.exists())
        self.assertEqual(active.state, "queued")
        self.assertTrue(outcome.did_work)

    def test_create_allows_three_row_window_then_blocked_row_fails_closed(self):
        expired, unused_path = self._complete(self.owner, self.now)
        del unused_path
        expired.state = "expired"
        expired.ready_cleanup_state = "pending"
        expired.save(update_fields=("state", "ready_cleanup_state"))
        success, unused_success_path = self._complete(
            self.owner,
            self.now + timedelta(hours=1),
        )
        del success, unused_success_path
        acquire_worker_lease(self.now)
        service = JobService(available_space_observer=lambda: 10 ** 12)

        created = service.create(
            self.owner,
            {"scope": "pins", "pin_ids": [self.pin.pk]},
            self.now,
        )

        self.assertEqual(created.state, "queued")
        self.assertEqual(ExportJob.objects.filter(owner=self.owner).count(), 3)
        expired.ready_cleanup_state = "blocked"
        expired.save(update_fields=("ready_cleanup_state",))
        with self.assertRaises(ExportRequestError) as raised:
            service.create(
                self.owner,
                {"scope": "pins", "pin_ids": [self.pin.pk]},
                self.now,
            )
        self.assertEqual(raised.exception.code, "export_storage_unsafe")

    def test_actual_second_success_allows_c_then_three_rows_reject_d(self):
        staging = Path(settings.PINRY_EXPORT_ROOT, ".staging")
        staging.mkdir(mode=0o700)
        os.chmod(str(staging), 0o700)
        service = JobService(available_space_observer=lambda: 10 ** 12)
        worker = ExportWorker(clock=lambda: self.now)
        worker.worker_token = worker.acquire_worker_lease(self.now)
        worker.heartbeat = LeaseHeartbeat(
            worker.worker_token,
            clock=lambda: self.now,
        )
        request = {"scope": "pins", "pin_ids": [self.pin.pk]}
        first = service.create(self.owner, request, self.now)
        with mock.patch("exports.services.file_ops._normalize_metadata"):
            self.assertTrue(worker.run_once(lambda: False))
        first.refresh_from_db()
        self.assertEqual(first.state, "complete")

        second = service.create(self.owner, request, self.now)
        with mock.patch("exports.services.file_ops._normalize_metadata"):
            self.assertTrue(worker.run_once(lambda: False))

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.state, "expired")
        self.assertEqual(first.ready_cleanup_state, "pending")
        self.assertEqual(second.state, "complete")
        third = service.create(self.owner, request, self.now)
        self.assertEqual(third.state, "queued")
        with self.assertRaises(ExportRequestError) as raised:
            service.create(self.owner, request, self.now)

        self.assertEqual(raised.exception.code, "export_storage_unsafe")
        self.assertEqual(ExportJob.objects.filter(owner=self.owner).count(), 3)
        first.refresh_from_db()
        self.assertEqual(first.ready_cleanup_state, "pending")

    def test_owner_bound_cleaned_failure_is_preserved_but_orphan_is_deleted(self):
        kept = ExportJob.objects.create(
            owner=self.owner,
            scope="pins",
            state="failed",
            error_code="archive_failed",
            error_class="retryable",
            error_retryable=True,
            staging_cleanup_state="cleaned",
            ready_cleanup_state="absent",
        )
        orphan = ExportJob.objects.create(
            owner=None,
            scope="pins",
            state="failed",
            error_code="archive_failed",
            error_class="retryable",
            error_retryable=True,
            staging_cleanup_state="cleaned",
            ready_cleanup_state="absent",
        )

        self._maintenance()

        self.assertTrue(ExportJob.objects.filter(pk=kept.pk).exists())
        self.assertFalse(ExportJob.objects.filter(pk=orphan.pk).exists())
