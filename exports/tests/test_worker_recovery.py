from contextlib import contextmanager
from datetime import timedelta
import hashlib
import os
from pathlib import Path
import threading
import uuid

from django.conf import settings
from django.db import connection
from django.test import TransactionTestCase
from django.utils import timezone
import mock

from core.services.database_fence import DatabaseFenceBusy
from exports.contracts import ERROR_CONTRACTS, LeaseToken, StopRequested
from exports.models import (
    ExportAttempt,
    ExportAttemptFile,
    ExportBlob,
    ExportItem,
    ExportJob,
    ExportSlot,
    ExportWorkerLease,
)
from exports.services.file_ops import (
    ClosedFileReceipt,
    ExportStorageError,
    OpenFileReceipt,
)
from exports.services.worker import (
    ExportWorker,
    LeaseHeartbeat,
    acquire_worker_lease,
    claim_retiring_recovery,
    claim_verifying_recovery,
    cleanup_expired_and_stale,
    handoff_after_normal_stop,
    requeue_expired_nonverifying_jobs,
)
from exports.services.snapshot import (
    snapshot_blob_name,
    snapshot_directory_name,
)

from .helpers import ExportStorageMixin, create_export_pin, create_export_user


class WorkerRecoveryTests(ExportStorageMixin, TransactionTestCase):
    def setUp(self):
        super(WorkerRecoveryTests, self).setUp()
        self.now = timezone.now()
        self.owner = create_export_user("worker-recovery-owner")

    def _worker(self):
        token = acquire_worker_lease(self.now)
        return token, LeaseHeartbeat(token, clock=lambda: self.now)

    def _active_job(self, state="archiving", **fields):
        defaults = {
            "owner": self.owner,
            "scope": "pins",
            "state": state,
            "requested_total": 1,
            "target_total": 1,
            "included_total": 1,
            "archive_total": 1,
            "bytes_total": 7,
            "attempt_generation": 0,
        }
        defaults.update(fields)
        job = ExportJob.objects.create(**defaults)
        ExportSlot.objects.update_or_create(
            owner=self.owner,
            defaults={"current_job": job},
        )
        return job

    def _assert_fourth_takeover_preserves_file_receipt(
        self,
        kind,
        state,
        force_invalid=False,
    ):
        old_worker_uuid = uuid.uuid4()
        old_job_uuid = uuid.uuid4()
        ExportWorkerLease.objects.create(
            pk=1,
            generation=8,
            lease_uuid=old_worker_uuid,
        )
        job = self._active_job(
            worker_generation=8,
            lease_uuid=old_job_uuid,
            resume_count=3,
        )
        staging = Path(settings.PINRY_EXPORT_ROOT, ".staging")
        staging.mkdir(mode=0o700)
        os.chmod(str(staging), 0o700)
        relative = "attempt-{}-0".format(job.pk)
        directory = staging / relative
        directory.mkdir(mode=0o700)
        os.chmod(str(directory), 0o700)
        directory_stat = directory.stat()
        path = directory / "archive.zip.part"
        content = "{}-{}".format(kind, state).encode("ascii")
        path.write_bytes(content)
        os.chmod(str(path), 0o600)
        descriptor = os.open(str(path), os.O_RDONLY)
        try:
            opened = OpenFileReceipt.from_fd(
                descriptor,
                os.getuid(),
                os.getgid(),
            )
            receipt = ClosedFileReceipt.from_open_fd(
                descriptor,
                opened,
                hashlib.sha256(content).hexdigest(),
            )
        finally:
            os.close(descriptor)
        attempt = ExportAttempt.objects.create(
            job=job,
            attempt_generation=0,
            lease_uuid=old_job_uuid,
            state="writing",
            relative_path=relative,
            dir_dev=directory_stat.st_dev,
            dir_ino=directory_stat.st_ino,
            dir_uid=directory_stat.st_uid,
            dir_gid=directory_stat.st_gid,
            dir_mode=0o700,
        )
        intent = None
        if state == "publishing":
            intent = "ready/{}.zip".format(job.pk)
        elif kind == "quarantine" and state == "writing":
            intent = "{}/quarantine.zip".format(relative)
        values = {
            "attempt": attempt,
            "kind": kind,
            "state": state,
            "receipt_level": "full",
            "relative_path": "{}/{}".format(relative, path.name),
            "intent_relative_path": intent,
            "receipt_dev": receipt.dev,
            "receipt_ino": receipt.ino,
            "receipt_uid": receipt.uid,
            "receipt_gid": receipt.gid,
            "receipt_mode": receipt.mode,
            "receipt_nlink": receipt.nlink,
            "receipt_size": receipt.size,
            "receipt_mtime_ns": receipt.mtime_ns,
            "receipt_ctime_ns": receipt.ctime_ns,
            "receipt_sha256": receipt.sha256,
        }
        if force_invalid:
            with connection.cursor() as cursor:
                cursor.execute("PRAGMA ignore_check_constraints = ON")
            try:
                attempt_file = ExportAttemptFile.objects.create(**values)
            finally:
                with connection.cursor() as cursor:
                    cursor.execute("PRAGMA ignore_check_constraints = OFF")
        else:
            attempt_file = ExportAttemptFile.objects.create(**values)
        expected = (
            attempt_file.relative_path,
            attempt_file.intent_relative_path,
            attempt_file.receipt_dev,
            attempt_file.receipt_ino,
            attempt_file.receipt_level,
            attempt_file.receipt_sha256,
        )
        worker = ExportWorker(clock=lambda: self.now)
        worker.worker_token = acquire_worker_lease(self.now)
        worker.heartbeat = LeaseHeartbeat(
            worker.worker_token,
            clock=lambda: self.now,
        )

        self.assertTrue(worker.run_once(lambda: False))

        job.refresh_from_db()
        attempt_file.refresh_from_db()
        actual = (
            attempt_file.relative_path,
            attempt_file.intent_relative_path,
            attempt_file.receipt_dev,
            attempt_file.receipt_ino,
            attempt_file.receipt_level,
            attempt_file.receipt_sha256,
        )
        self.assertEqual(job.state, "failed")
        self.assertEqual(job.error_code, "worker_repeated_failure")
        self.assertEqual(job.staging_cleanup_state, "pending")
        self.assertEqual(
            attempt_file.state,
            state if kind == "quarantine" else "retiring",
        )
        self.assertEqual(actual, expected)
        self.assertTrue(path.exists())
        self.assertEqual(path.read_bytes(), content)
        self.assertEqual(path.stat().st_dev, receipt.dev)
        self.assertEqual(path.stat().st_ino, receipt.ino)
        self.assertIsNone(worker.heartbeat.current_job_token)

    def test_fourth_archive_writing_takeover_preserves_receipt(self):
        self._assert_fourth_takeover_preserves_file_receipt(
            "archive", "writing",
        )

    def test_fourth_archive_closed_takeover_preserves_receipt(self):
        self._assert_fourth_takeover_preserves_file_receipt(
            "archive", "closed",
        )

    def test_fourth_archive_publishing_takeover_preserves_receipt(self):
        self._assert_fourth_takeover_preserves_file_receipt(
            "archive", "publishing",
        )

    def test_fourth_archive_ready_candidate_takeover_preserves_receipt(self):
        self._assert_fourth_takeover_preserves_file_receipt(
            "archive", "ready_candidate",
        )

    def test_fourth_quarantine_writing_takeover_preserves_receipt(self):
        self._assert_fourth_takeover_preserves_file_receipt(
            "quarantine", "writing",
        )

    def test_fourth_quarantine_closed_takeover_preserves_receipt(self):
        self._assert_fourth_takeover_preserves_file_receipt(
            "quarantine", "closed",
        )

    def test_invalid_quarantine_publishing_takeover_fails_closed(self):
        self._assert_fourth_takeover_preserves_file_receipt(
            "quarantine", "publishing", force_invalid=True,
        )

    def test_invalid_quarantine_ready_candidate_takeover_fails_closed(self):
        self._assert_fourth_takeover_preserves_file_receipt(
            "quarantine", "ready_candidate", force_invalid=True,
        )

    def test_old_generation_future_expiry_is_abnormal_retiring_takeover(self):
        old_worker_uuid = uuid.uuid4()
        old_job_uuid = uuid.uuid4()
        ExportWorkerLease.objects.create(
            pk=1,
            generation=4,
            lease_uuid=old_worker_uuid,
            lease_expires_at=self.now + timedelta(hours=1),
        )
        job = self._active_job(
            worker_generation=4,
            lease_uuid=old_job_uuid,
            lease_expires_at=self.now + timedelta(hours=1),
            resume_count=1,
        )
        ExportAttempt.objects.create(
            job=job,
            attempt_generation=0,
            lease_uuid=old_job_uuid,
            state="retiring",
            relative_path="attempt-{}-0".format(job.pk),
            dir_dev=1,
            dir_ino=2,
            dir_uid=3,
            dir_gid=4,
            dir_mode=0o700,
        )
        worker = acquire_worker_lease(self.now + timedelta(seconds=1))
        heartbeat = LeaseHeartbeat(worker, clock=lambda: self.now)

        outcome = claim_retiring_recovery(worker, heartbeat, self.now)

        job.refresh_from_db()
        self.assertFalse(outcome.terminalized)
        self.assertEqual(outcome.lease.attempt_generation, 0)
        self.assertEqual(job.resume_count, 2)
        self.assertEqual(job.state, "archiving")
        self.assertEqual(job.worker_generation, worker.worker_generation)

    def test_null_lease_normal_handoff_does_not_increment_resume_count(self):
        job = self._active_job(
            worker_generation=4,
            lease_uuid=None,
            resume_count=2,
        )
        ExportAttempt.objects.create(
            job=job,
            attempt_generation=0,
            lease_uuid=uuid.uuid4(),
            state="retiring",
            relative_path="attempt-{}-0".format(job.pk),
            dir_dev=1,
            dir_ino=2,
            dir_uid=3,
            dir_gid=4,
            dir_mode=0o700,
        )
        worker, heartbeat = self._worker()

        outcome = claim_retiring_recovery(worker, heartbeat, self.now)

        job.refresh_from_db()
        self.assertFalse(outcome.terminalized)
        self.assertEqual(job.resume_count, 2)
        self.assertEqual(job.attempt_generation, 0)

    def test_fourth_abnormal_retiring_takeover_terminalizes_without_token(self):
        old_worker_uuid = uuid.uuid4()
        old_job_uuid = uuid.uuid4()
        ExportWorkerLease.objects.create(
            pk=1, generation=8, lease_uuid=old_worker_uuid,
        )
        job = self._active_job(
            worker_generation=8,
            lease_uuid=old_job_uuid,
            resume_count=3,
        )
        ExportAttempt.objects.create(
            job=job,
            attempt_generation=0,
            lease_uuid=old_job_uuid,
            state="retiring",
            relative_path="attempt-{}-0".format(job.pk),
            dir_dev=1,
            dir_ino=2,
            dir_uid=3,
            dir_gid=4,
            dir_mode=0o700,
        )
        worker = acquire_worker_lease(self.now)
        heartbeat = LeaseHeartbeat(worker, clock=lambda: self.now)

        outcome = claim_retiring_recovery(worker, heartbeat, self.now)

        job.refresh_from_db()
        self.assertTrue(outcome.terminalized)
        self.assertIsNone(outcome.lease)
        self.assertIsNone(heartbeat.current_job_token)
        self.assertEqual(job.state, "failed")
        self.assertEqual(job.error_code, "worker_repeated_failure")
        self.assertEqual(
            (job.error_class, job.error_retryable),
            ERROR_CONTRACTS["worker_repeated_failure"][:2],
        )
        self.assertIsNone(ExportSlot.objects.get(owner=self.owner).current_job_id)

    def test_fourth_abnormal_verifying_takeover_terminalizes(self):
        old_worker_uuid = uuid.uuid4()
        old_job_uuid = uuid.uuid4()
        ExportWorkerLease.objects.create(
            pk=1, generation=2, lease_uuid=old_worker_uuid,
        )
        job = self._active_job(
            state="verifying",
            worker_generation=2,
            lease_uuid=old_job_uuid,
            resume_count=3,
            verifying_attempt_generation=0,
            verifying_size=10,
            verifying_sha256="a" * 64,
            verifying_completed_at=self.now,
        )
        worker = acquire_worker_lease(self.now)
        heartbeat = LeaseHeartbeat(worker, clock=lambda: self.now)

        outcome = claim_verifying_recovery(worker, heartbeat, self.now)

        job.refresh_from_db()
        self.assertTrue(outcome.terminalized)
        self.assertIsNone(outcome.lease)
        self.assertEqual(job.state, "failed")

    def test_cleaned_current_attempt_rotates_once_when_requeued(self):
        old_worker_uuid = uuid.uuid4()
        old_job_uuid = uuid.uuid4()
        ExportWorkerLease.objects.create(
            pk=1, generation=5, lease_uuid=old_worker_uuid,
        )
        job = self._active_job(
            worker_generation=5,
            lease_uuid=old_job_uuid,
            resume_count=0,
            attempt_generation=4,
            archive_done=1,
            bytes_done=7,
            verifying_attempt_generation=4,
            verifying_size=12,
            verifying_sha256="b" * 64,
            verifying_completed_at=self.now,
        )
        ExportAttempt.objects.create(
            job=job,
            attempt_generation=4,
            lease_uuid=old_job_uuid,
            state="cleaned",
            relative_path="attempt-{}-4".format(job.pk),
            dir_dev=1,
            dir_ino=2,
            dir_uid=3,
            dir_gid=4,
            dir_mode=0o700,
        )
        worker = acquire_worker_lease(self.now)

        outcome = requeue_expired_nonverifying_jobs(worker, self.now)

        job.refresh_from_db()
        self.assertEqual(outcome.requeued_count, 1)
        self.assertFalse(outcome.terminalized)
        self.assertEqual(job.state, "queued")
        self.assertEqual(job.attempt_generation, 5)
        self.assertEqual(job.resume_count, 1)
        self.assertEqual((job.archive_done, job.bytes_done), (0, 0))
        self.assertEqual(
            (
                job.verifying_attempt_generation,
                job.verifying_size,
                job.verifying_sha256,
                job.verifying_completed_at,
            ),
            (None, None, None, None),
        )

        repeated = requeue_expired_nonverifying_jobs(worker, self.now)
        job.refresh_from_db()
        self.assertEqual(repeated.requeued_count, 0)
        self.assertEqual(job.attempt_generation, 5)

    def test_fifty_thousand_item_totals_use_bounded_autocommit_reads(self):
        old_worker_uuid = uuid.uuid4()
        old_job_uuid = uuid.uuid4()
        ExportWorkerLease.objects.create(
            pk=1, generation=5, lease_uuid=old_worker_uuid,
        )
        job = self._active_job(
            worker_generation=5,
            lease_uuid=old_job_uuid,
            attempt_generation=4,
            requested_total=50000,
            target_total=50000,
            included_total=50000,
        )
        ExportAttempt.objects.create(
            job=job,
            attempt_generation=4,
            lease_uuid=old_job_uuid,
            state="cleaned",
            relative_path="attempt-{}-4".format(job.pk),
            dir_dev=1,
            dir_ino=2,
            dir_uid=3,
            dir_gid=4,
            dir_mode=0o700,
        )
        generation = uuid.uuid4()
        blob = ExportBlob.objects.create(
            job=job,
            snapshot_generation=generation,
            source_media_asset_id=1,
            source_image_id=1,
            source_relative_path="source",
            file_state="closed",
            snapshot_relative_path="snapshot/blob",
            size=7,
            receipt_dev=1,
            receipt_ino=2,
            receipt_uid=3,
            receipt_gid=4,
            receipt_mode=0o600,
            receipt_nlink=1,
            receipt_mtime_ns=5,
            receipt_ctime_ns=6,
            receipt_sha256="a" * 64,
        )
        for start in range(0, 50000, 400):
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
                    published_at=self.now,
                    original_filename="{}.png".format(index),
                )
                for index in range(start, min(start + 400, 50000))
            ], batch_size=400)
        worker = acquire_worker_lease(self.now)
        checkpoints = []

        outcome = requeue_expired_nonverifying_jobs(
            worker,
            self.now,
            checkpoint=lambda: checkpoints.append(connection.in_atomic_block),
        )

        job.refresh_from_db()
        self.assertEqual(outcome.requeued_count, 1)
        self.assertEqual((job.archive_total, job.bytes_total), (50000, 350000))
        self.assertGreaterEqual(len(checkpoints), 125)
        self.assertFalse(any(checkpoints))
        self.assertEqual(job.resume_count, 1)
        self.assertEqual((job.archive_done, job.bytes_done), (0, 0))
        self.assertEqual(
            (
                job.verifying_attempt_generation,
                job.verifying_size,
                job.verifying_sha256,
                job.verifying_completed_at,
            ),
            (None, None, None, None),
        )

        repeated = requeue_expired_nonverifying_jobs(worker, self.now)
        job.refresh_from_db()
        self.assertEqual(repeated.requeued_count, 0)
        self.assertEqual(job.attempt_generation, 5)

    def test_clean_handoff_preserves_preallocated_generation_and_resume_count(self):
        worker, heartbeat = self._worker()
        job = self._active_job(
            worker_generation=worker.worker_generation,
            lease_uuid=uuid.uuid4(),
            resume_count=2,
            attempt_generation=6,
        )
        lease = LeaseToken(
            worker.worker_generation,
            worker.worker_lease_uuid,
            job.pk,
            job.lease_uuid,
            6,
        )
        with heartbeat.job_token_transition(None) as transition:
            transition.publish(lease)

        handoff_after_normal_stop(lease, heartbeat, self.now)

        job.refresh_from_db()
        self.assertEqual(job.state, "queued")
        self.assertEqual(job.attempt_generation, 6)
        self.assertEqual(job.resume_count, 2)
        self.assertIsNone(job.lease_uuid)
        self.assertIsNone(heartbeat.current_job_token)

    def test_verifying_handoff_preserves_recovery_state(self):
        worker, heartbeat = self._worker()
        job = self._active_job(
            state="verifying",
            worker_generation=worker.worker_generation,
            lease_uuid=uuid.uuid4(),
            attempt_generation=2,
            verifying_attempt_generation=2,
            verifying_size=11,
            verifying_sha256="c" * 64,
            verifying_completed_at=self.now,
        )
        lease = LeaseToken(
            worker.worker_generation,
            worker.worker_lease_uuid,
            job.pk,
            job.lease_uuid,
            2,
        )
        with heartbeat.job_token_transition(None) as transition:
            transition.publish(lease)

        handoff_after_normal_stop(lease, heartbeat, self.now)

        job.refresh_from_db()
        self.assertEqual(job.state, "verifying")
        self.assertEqual(job.verifying_attempt_generation, 2)
        self.assertEqual(job.verifying_sha256, "c" * 64)
        self.assertIsNone(job.lease_uuid)

    def test_complete_terminal_maintenance_claim_does_not_count_resume(self):
        old_worker_uuid = uuid.uuid4()
        job_uuid = uuid.uuid4()
        ExportWorkerLease.objects.create(
            pk=1, generation=2, lease_uuid=old_worker_uuid,
        )
        job = ExportJob.objects.create(
            owner=self.owner,
            scope="pins",
            state="complete",
            completed_at=self.now,
            expires_at=self.now + timedelta(hours=24),
            ready_cleanup_state="retained",
            staging_cleanup_state="cleaned",
            ready_relative_path="ready/{}.zip".format(uuid.uuid4()),
            ready_display_name="ready.zip",
            ready_size=1,
            ready_sha256="d" * 64,
            ready_dev=1,
            ready_ino=2,
            ready_uid=3,
            ready_gid=4,
            ready_mode=0o600,
            ready_nlink=1,
            ready_mtime_ns=5,
            ready_ctime_ns=6,
            worker_generation=2,
            lease_uuid=job_uuid,
            attempt_generation=7,
            resume_count=2,
        )
        worker = acquire_worker_lease(self.now)
        heartbeat = LeaseHeartbeat(worker, clock=lambda: self.now)
        from exports.services.worker import claim_complete_maintenance

        lease = claim_complete_maintenance(worker, heartbeat, self.now)

        job.refresh_from_db()
        self.assertEqual(lease.job_id, job.pk)
        self.assertEqual(lease.attempt_generation, 7)
        self.assertEqual(job.resume_count, 2)

        heartbeat.renew_now(lease, now=self.now + timedelta(seconds=1))
        self.assertIsNone(heartbeat.current_job_token)

    def test_complete_maintenance_claim_uses_foreground_write_guard(self):
        job = ExportJob.objects.create(
            owner=self.owner,
            scope="pins",
            state="complete",
            completed_at=self.now,
            expires_at=self.now + timedelta(hours=24),
            ready_cleanup_state="retained",
            staging_cleanup_state="pending",
            ready_relative_path="ready/{}.zip".format(uuid.uuid4()),
            ready_display_name="ready.zip",
            ready_size=1,
            ready_sha256="0" * 64,
            ready_dev=1,
            ready_ino=2,
            ready_uid=3,
            ready_gid=4,
            ready_mode=0o600,
            ready_nlink=1,
            ready_mtime_ns=5,
            ready_ctime_ns=6,
            attempt_generation=0,
        )
        worker, heartbeat = self._worker()
        guard_depth = [0]
        original_guard = heartbeat.foreground_write_guard

        @contextmanager
        def tracking_guard():
            with original_guard():
                guard_depth[0] += 1
                try:
                    yield
                finally:
                    guard_depth[0] -= 1

        heartbeat.foreground_write_guard = tracking_guard
        from exports.services import worker as worker_services
        original_lock = worker_services.lock_current_worker_lease

        def checked_lock(*args, **kwargs):
            self.assertGreater(guard_depth[0], 0)
            return original_lock(*args, **kwargs)

        with mock.patch(
            "exports.services.worker.lock_current_worker_lease",
            side_effect=checked_lock,
        ):
            lease = worker_services.claim_complete_maintenance(
                worker,
                heartbeat,
                self.now,
            )

        self.assertEqual(lease.job_id, job.pk)

    def test_fourth_abnormal_nonverifying_requeue_terminalizes(self):
        old_worker_uuid = uuid.uuid4()
        old_job_uuid = uuid.uuid4()
        ExportWorkerLease.objects.create(
            pk=1, generation=2, lease_uuid=old_worker_uuid,
        )
        job = self._active_job(
            state="snapshotting",
            worker_generation=2,
            lease_uuid=old_job_uuid,
            lease_expires_at=self.now + timedelta(hours=1),
            resume_count=3,
        )
        worker = acquire_worker_lease(self.now)

        outcome = requeue_expired_nonverifying_jobs(worker, self.now)

        job.refresh_from_db()
        self.assertTrue(outcome.terminalized)
        self.assertEqual(outcome.requeued_count, 0)
        self.assertEqual(job.state, "failed")
        self.assertEqual(job.error_code, "worker_repeated_failure")

    def test_worker_nonverifying_requeue_uses_foreground_write_guard(self):
        old_worker_uuid = uuid.uuid4()
        ExportWorkerLease.objects.create(
            pk=1,
            generation=3,
            lease_uuid=old_worker_uuid,
        )
        job = self._active_job(
            state="snapshotting",
            worker_generation=3,
            lease_uuid=uuid.uuid4(),
        )
        worker = ExportWorker(clock=lambda: self.now)
        worker.worker_token = acquire_worker_lease(self.now)
        worker.heartbeat = LeaseHeartbeat(
            worker.worker_token,
            clock=lambda: self.now,
        )
        guard_depth = [0]
        original_guard = worker.heartbeat.foreground_write_guard

        @contextmanager
        def tracking_guard():
            with original_guard():
                guard_depth[0] += 1
                try:
                    yield
                finally:
                    guard_depth[0] -= 1

        worker.heartbeat.foreground_write_guard = tracking_guard
        from exports.services import worker as worker_services
        original_lock = worker_services.lock_current_worker_lease

        def checked_lock(*args, **kwargs):
            self.assertGreater(guard_depth[0], 0)
            return original_lock(*args, **kwargs)

        with mock.patch(
            "exports.services.worker.lock_current_worker_lease",
            side_effect=checked_lock,
        ):
            outcome = worker.requeue_expired_nonverifying_jobs(
                worker.worker_token,
                self.now,
            )

        job.refresh_from_db()
        self.assertEqual(outcome.requeued_count, 1)
        self.assertEqual(job.state, "queued")

    def test_run_once_prefers_retiring_before_verifying_and_fifo(self):
        retiring = self._active_job(state="archiving", lease_uuid=None)
        ExportAttempt.objects.create(
            job=retiring,
            attempt_generation=0,
            lease_uuid=uuid.uuid4(),
            state="retiring",
            relative_path="attempt-{}-0".format(retiring.pk),
            dir_dev=1,
            dir_ino=2,
            dir_uid=3,
            dir_gid=4,
            dir_mode=0o700,
        )
        verifying = ExportJob.objects.create(
            owner=self.owner,
            scope="pins",
            state="verifying",
            requested_total=1,
            target_total=1,
            included_total=1,
            archive_total=1,
            bytes_total=7,
            lease_uuid=None,
            attempt_generation=0,
            verifying_attempt_generation=0,
            verifying_size=1,
            verifying_sha256="e" * 64,
            verifying_completed_at=self.now,
        )
        queued = ExportJob.objects.create(
            owner=self.owner,
            scope="pins",
            state="queued",
            requested_total=1,
            target_total=1,
            included_total=1,
        )
        worker = ExportWorker(clock=lambda: self.now)
        worker.worker_token = acquire_worker_lease(self.now)
        worker.heartbeat = LeaseHeartbeat(
            worker.worker_token,
            clock=lambda: self.now,
        )
        dispatched = []
        worker._dispatch_claim = lambda lease, stop, mode: dispatched.append(
            (lease.job_id, mode),
        )

        self.assertTrue(worker.run_once(lambda: False))

        verifying.refresh_from_db()
        queued.refresh_from_db()
        self.assertEqual(dispatched, [(retiring.pk, "retiring")])
        self.assertEqual(verifying.state, "verifying")
        self.assertEqual(queued.state, "queued")

    def test_terminalized_run_once_does_not_claim_another_queued_job(self):
        old_worker_uuid = uuid.uuid4()
        old_job_uuid = uuid.uuid4()
        ExportWorkerLease.objects.create(
            pk=1, generation=4, lease_uuid=old_worker_uuid,
        )
        broken = self._active_job(
            state="archiving",
            worker_generation=4,
            lease_uuid=old_job_uuid,
            resume_count=3,
        )
        ExportAttempt.objects.create(
            job=broken,
            attempt_generation=0,
            lease_uuid=old_job_uuid,
            state="retiring",
            relative_path="attempt-{}-0".format(broken.pk),
            dir_dev=1,
            dir_ino=2,
            dir_uid=3,
            dir_gid=4,
            dir_mode=0o700,
        )
        queued = ExportJob.objects.create(
            owner=self.owner,
            scope="pins",
            state="queued",
            requested_total=1,
            target_total=1,
            included_total=1,
        )
        worker = ExportWorker(clock=lambda: self.now)
        worker.worker_token = acquire_worker_lease(self.now)
        worker.heartbeat = LeaseHeartbeat(
            worker.worker_token,
            clock=lambda: self.now,
        )
        worker.claim_next_job = mock.Mock()

        self.assertTrue(worker.run_once(lambda: False))

        broken.refresh_from_db()
        queued.refresh_from_db()
        self.assertEqual(broken.state, "failed")
        self.assertEqual(queued.state, "queued")
        worker.claim_next_job.assert_not_called()

    def test_owner_deleted_queued_terminalizes_before_another_claim(self):
        deleted_owner = create_export_user("worker-deleted-queued-owner")
        deleted_owner_id = deleted_owner.pk
        orphan = ExportJob.objects.create(
            owner=deleted_owner,
            scope="pins",
            state="queued",
            requested_total=1,
            target_total=1,
            included_total=1,
        )
        ExportSlot.objects.create(owner=deleted_owner, current_job=orphan)
        queued = ExportJob.objects.create(
            owner=self.owner,
            scope="pins",
            state="queued",
            requested_total=1,
            target_total=1,
            included_total=1,
        )
        deleted_owner.delete()
        worker = ExportWorker(clock=lambda: self.now)
        worker.worker_token = acquire_worker_lease(self.now)
        worker.heartbeat = LeaseHeartbeat(
            worker.worker_token,
            clock=lambda: self.now,
        )
        worker.claim_next_job = mock.Mock(return_value=None)

        self.assertTrue(worker.run_once(lambda: False))

        orphan.refresh_from_db()
        queued.refresh_from_db()
        self.assertEqual(orphan.state, "failed")
        self.assertEqual(orphan.error_code, "permission_changed")
        self.assertEqual(orphan.staging_cleanup_state, "pending")
        self.assertEqual(queued.state, "queued")
        self.assertFalse(ExportSlot.objects.filter(
            owner_id=deleted_owner_id,
        ).exists())
        self.assertIsNone(worker.heartbeat.current_job_token)
        worker.claim_next_job.assert_not_called()

    def test_owner_null_active_states_terminalize_one_per_worker_loop(self):
        jobs = []
        for state in ("queued", "snapshotting", "archiving", "verifying"):
            owner = create_export_user("worker-owner-null-{}".format(state))
            job = ExportJob.objects.create(
                owner=owner,
                scope="pins",
                state=state,
                requested_total=1,
                target_total=1,
                included_total=1,
                archive_total=1,
                bytes_total=7,
            )
            owner.delete()
            jobs.append(job)
        worker = ExportWorker(clock=lambda: self.now)
        worker.worker_token = acquire_worker_lease(self.now)
        worker.heartbeat = LeaseHeartbeat(
            worker.worker_token,
            clock=lambda: self.now,
        )

        for expected_count in range(1, 5):
            self.assertTrue(worker.run_once(lambda: False))
            self.assertEqual(
                ExportJob.objects.filter(
                    pk__in=[job.pk for job in jobs],
                    state="failed",
                    error_code="permission_changed",
                ).count(),
                expected_count,
            )

        self.assertIsNone(worker.heartbeat.current_job_token)

    def test_owner_delete_after_clean_handoff_terminalizes_on_next_loop(self):
        owner = create_export_user("worker-owner-after-handoff")
        job = ExportJob.objects.create(
            owner=owner,
            scope="pins",
            state="queued",
            requested_total=1,
            target_total=1,
            included_total=1,
        )
        worker = ExportWorker(clock=lambda: self.now)
        worker.worker_token = acquire_worker_lease(self.now)
        worker.heartbeat = LeaseHeartbeat(
            worker.worker_token,
            clock=lambda: self.now,
        )
        lease = worker.claim_next_job(
            worker.worker_token,
            worker.heartbeat,
            self.now,
        )

        handoff_after_normal_stop(lease, worker.heartbeat, self.now)
        owner.delete()
        self.assertTrue(worker.run_once(lambda: False))

        job.refresh_from_db()
        self.assertEqual(job.state, "failed")
        self.assertEqual(job.error_code, "permission_changed")
        self.assertIsNone(job.lease_uuid)
        self.assertIsNone(worker.heartbeat.current_job_token)

    def test_cleanup_does_not_nest_database_retry_budgets(self):
        token, heartbeat = self._worker()
        from exports.services import worker as worker_services
        original_retry = worker_services._retry_database
        depth = [0]
        maximum_depth = [0]

        def tracked_retry(*args, **kwargs):
            depth[0] += 1
            maximum_depth[0] = max(maximum_depth[0], depth[0])
            try:
                return original_retry(*args, **kwargs)
            finally:
                depth[0] -= 1

        with mock.patch.object(
                worker_services,
                "_retry_database",
                side_effect=tracked_retry,
        ):
            cleanup_expired_and_stale(
                token,
                heartbeat,
                lambda: False,
                now=self.now,
            )

        self.assertEqual(maximum_depth[0], 1)

    def test_stop_during_complete_maintenance_releases_terminal_lease(self):
        old_worker_uuid = uuid.uuid4()
        old_job_uuid = uuid.uuid4()
        ExportWorkerLease.objects.create(
            pk=1, generation=2, lease_uuid=old_worker_uuid,
        )
        job = ExportJob.objects.create(
            owner=self.owner,
            scope="pins",
            state="complete",
            completed_at=self.now,
            expires_at=self.now + timedelta(hours=24),
            ready_cleanup_state="retained",
            staging_cleanup_state="pending",
            ready_relative_path="ready/{}.zip".format(uuid.uuid4()),
            ready_display_name="ready.zip",
            ready_size=1,
            ready_sha256="f" * 64,
            ready_dev=1,
            ready_ino=2,
            ready_uid=3,
            ready_gid=4,
            ready_mode=0o600,
            ready_nlink=1,
            ready_mtime_ns=5,
            ready_ctime_ns=6,
            worker_generation=2,
            lease_uuid=old_job_uuid,
        )

        class StoppingArchive(object):
            def recover_complete(self, lease, heartbeat, stop_requested):
                del heartbeat, stop_requested
                raise StopRequested(lease)

        worker = ExportWorker(
            clock=lambda: self.now,
            archive_service=StoppingArchive(),
        )
        worker.worker_token = acquire_worker_lease(self.now)
        worker.heartbeat = LeaseHeartbeat(
            worker.worker_token,
            clock=lambda: self.now,
        )

        self.assertTrue(worker.run_once(lambda: False))

        job.refresh_from_db()
        self.assertEqual(job.state, "complete")
        self.assertIsNone(job.lease_uuid)
        self.assertIsNone(worker.heartbeat.current_job_token)


class TerminalCleanupTests(ExportStorageMixin, TransactionTestCase):
    def setUp(self):
        super(TerminalCleanupTests, self).setUp()
        self.now = timezone.now()
        self.owner = create_export_user("terminal-cleanup-owner")
        self.staging = Path(settings.PINRY_EXPORT_ROOT, ".staging")
        self.staging.mkdir(mode=0o700)
        os.chmod(str(self.staging), 0o700)

    def _fixture(self):
        job = ExportJob.objects.create(
            owner=self.owner,
            scope="pins",
            state="failed",
            error_code="archive_failed",
            error_class="retryable",
            error_retryable=True,
            staging_cleanup_state="pending",
            attempt_generation=0,
        )
        relative = "attempt-{}-0".format(job.pk)
        directory = self.staging / relative
        directory.mkdir(mode=0o700)
        os.chmod(str(directory), 0o700)
        directory_stat = directory.stat()
        path = directory / "archive.zip.part"
        content = b"terminal cleanup"
        path.write_bytes(content)
        os.chmod(str(path), 0o600)
        descriptor = os.open(str(path), os.O_RDONLY)
        try:
            opened = OpenFileReceipt.from_fd(
                descriptor,
                os.getuid(),
                os.getgid(),
            )
            receipt = ClosedFileReceipt.from_open_fd(
                descriptor,
                opened,
                hashlib.sha256(content).hexdigest(),
            )
        finally:
            os.close(descriptor)
        attempt = ExportAttempt.objects.create(
            job=job,
            attempt_generation=0,
            lease_uuid=uuid.uuid4(),
            state="retiring",
            relative_path=relative,
            dir_dev=directory_stat.st_dev,
            dir_ino=directory_stat.st_ino,
            dir_uid=directory_stat.st_uid,
            dir_gid=directory_stat.st_gid,
            dir_mode=0o700,
        )
        attempt_file = ExportAttemptFile.objects.create(
            attempt=attempt,
            kind="archive",
            state="retiring",
            receipt_level="full",
            relative_path="{}/archive.zip.part".format(relative),
            receipt_dev=receipt.dev,
            receipt_ino=receipt.ino,
            receipt_uid=receipt.uid,
            receipt_gid=receipt.gid,
            receipt_mode=receipt.mode,
            receipt_nlink=receipt.nlink,
            receipt_size=receipt.size,
            receipt_mtime_ns=receipt.mtime_ns,
            receipt_ctime_ns=receipt.ctime_ns,
            receipt_sha256=receipt.sha256,
        )
        return job, attempt, attempt_file, path, directory

    def _worker(self):
        token = acquire_worker_lease(self.now)
        return token, LeaseHeartbeat(token, clock=lambda: self.now)

    def _snapshot_fixture(self, count=2):
        generation = uuid.uuid4()
        job = ExportJob.objects.create(
            owner=self.owner,
            scope="pins",
            state="failed",
            error_code="archive_failed",
            error_class="retryable",
            error_retryable=True,
            staging_cleanup_state="pending",
        )
        name = snapshot_directory_name(job.pk, generation)
        directory = self.staging / name
        directory.mkdir(mode=0o700)
        os.chmod(str(directory), 0o700)
        directory_stat = directory.stat()
        job.snapshot_relative_path = name
        job.snapshot_generation = generation
        job.snapshot_dir_dev = directory_stat.st_dev
        job.snapshot_dir_ino = directory_stat.st_ino
        job.snapshot_dir_uid = directory_stat.st_uid
        job.snapshot_dir_gid = directory_stat.st_gid
        job.snapshot_dir_mode = 0o700
        job.save()
        blobs = []
        paths = []
        for index in range(count):
            blob = ExportBlob.objects.create(
                job=job,
                snapshot_generation=generation,
                source_media_asset_id=index + 1,
                source_image_id=index + 1,
                source_relative_path="source-{}".format(index),
            )
            path = directory / snapshot_blob_name(blob.pk)
            content = "snapshot-{}".format(index).encode("ascii")
            path.write_bytes(content)
            os.chmod(str(path), 0o600)
            descriptor = os.open(str(path), os.O_RDONLY)
            try:
                opened = OpenFileReceipt.from_fd(
                    descriptor,
                    os.getuid(),
                    os.getgid(),
                )
                receipt = ClosedFileReceipt.from_open_fd(
                    descriptor,
                    opened,
                    hashlib.sha256(content).hexdigest(),
                )
            finally:
                os.close(descriptor)
            blob.file_state = "closed"
            blob.snapshot_relative_path = "{}/{}".format(name, path.name)
            blob.size = receipt.size
            blob.receipt_dev = receipt.dev
            blob.receipt_ino = receipt.ino
            blob.receipt_uid = receipt.uid
            blob.receipt_gid = receipt.gid
            blob.receipt_mode = receipt.mode
            blob.receipt_nlink = receipt.nlink
            blob.receipt_mtime_ns = receipt.mtime_ns
            blob.receipt_ctime_ns = receipt.ctime_ns
            blob.receipt_sha256 = receipt.sha256
            blob.save()
            blobs.append(blob)
            paths.append(path)
        return job, blobs, paths, directory

    def _cleanup(self, token, heartbeat):
        return cleanup_expired_and_stale(
            token,
            heartbeat,
            lambda: False,
            now=self.now,
        )

    def _ready_directory(self):
        ready = Path(settings.PINRY_EXPORT_ROOT, "ready")
        ready.mkdir(mode=0o700, exist_ok=True)
        os.chmod(str(ready), 0o700)
        return ready

    @staticmethod
    def _refresh_full_receipt(attempt_file, path):
        descriptor = os.open(str(path), os.O_RDONLY)
        try:
            opened = OpenFileReceipt.from_fd(
                descriptor,
                os.getuid(),
                os.getgid(),
            )
            receipt = ClosedFileReceipt.from_open_fd(
                descriptor,
                opened,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        finally:
            os.close(descriptor)
        for field in (
            "dev", "ino", "uid", "gid", "mode", "nlink",
            "size", "mtime_ns", "ctime_ns", "sha256",
        ):
            setattr(
                attempt_file,
                "receipt_{}".format(field),
                getattr(receipt, field),
            )
        attempt_file.receipt_level = "full"
        attempt_file.save()

    @staticmethod
    def _set_quarantine_open_receipt(attempt_file, source, intent_relative):
        descriptor = os.open(str(source), os.O_RDONLY)
        try:
            receipt = OpenFileReceipt.from_fd(
                descriptor,
                os.getuid(),
                os.getgid(),
            )
        finally:
            os.close(descriptor)
        attempt_file.kind = "quarantine"
        attempt_file.state = "writing"
        attempt_file.receipt_level = "open"
        attempt_file.intent_relative_path = intent_relative
        attempt_file.receipt_dev = receipt.dev
        attempt_file.receipt_ino = receipt.ino
        attempt_file.receipt_uid = receipt.uid
        attempt_file.receipt_gid = receipt.gid
        attempt_file.receipt_mode = receipt.mode
        attempt_file.receipt_nlink = receipt.nlink
        attempt_file.receipt_size = None
        attempt_file.receipt_mtime_ns = None
        attempt_file.receipt_ctime_ns = None
        attempt_file.receipt_sha256 = None
        attempt_file.save()

    def test_terminal_cleanup_unlinks_then_cas_and_finishes_in_batches(self):
        job, attempt, attempt_file, path, directory = self._fixture()
        token, heartbeat = self._worker()
        guard_depth = [0]
        original_guard = heartbeat.foreground_write_guard

        @contextmanager
        def tracking_guard():
            with original_guard():
                guard_depth[0] += 1
                try:
                    yield
                finally:
                    guard_depth[0] -= 1

        heartbeat.foreground_write_guard = tracking_guard
        from exports.services import worker as worker_services
        original_remove = worker_services.remove_if_receipt_matches

        def checked_remove(*args, **kwargs):
            self.assertEqual(guard_depth[0], 0)
            self.assertFalse(connection.in_atomic_block)
            return original_remove(*args, **kwargs)

        with mock.patch(
            "exports.services.worker.remove_if_receipt_matches",
            side_effect=checked_remove,
        ):
            first = self._cleanup(token, heartbeat)
        attempt_file.refresh_from_db()
        self.assertFalse(first.claim_allowed)
        self.assertFalse(path.exists())
        self.assertEqual(attempt_file.state, "cleaned")

        self._cleanup(token, heartbeat)
        attempt.refresh_from_db()
        self.assertFalse(directory.exists())
        self.assertEqual(attempt.state, "cleaned")

        final = self._cleanup(token, heartbeat)
        job.refresh_from_db()
        self.assertEqual(job.staging_cleanup_state, "cleaned")
        self.assertTrue(final.claim_allowed)

    def test_virtual_45_second_fs_success_starts_fresh_cas_budget(self):
        job, unused_attempt, attempt_file, path, unused_directory = (
            self._fixture()
        )
        del unused_attempt, unused_directory
        elapsed = [0.0]
        token = acquire_worker_lease(self.now)
        heartbeat = LeaseHeartbeat(
            token,
            clock=lambda: self.now,
            monotonic=lambda: elapsed[0],
            sleeper=lambda seconds: None,
        )
        from exports.services import worker as worker_services
        original_cleanup = worker_services._cleanup_attempt_file_fs

        def slow_cleanup(*args, **kwargs):
            result = original_cleanup(*args, **kwargs)
            elapsed[0] = 45.0
            return result

        with mock.patch.object(
                worker_services,
                "_cleanup_attempt_file_fs",
                side_effect=slow_cleanup,
        ):
            outcome = cleanup_expired_and_stale(
                token,
                heartbeat,
                lambda: False,
                now=self.now,
                monotonic=lambda: elapsed[0],
                sleeper=lambda seconds: None,
            )

        attempt_file.refresh_from_db()
        self.assertEqual(elapsed[0], 45.0)
        self.assertFalse(path.exists())
        self.assertEqual(attempt_file.state, "cleaned")
        self.assertTrue(outcome.did_work)

    def test_virtual_45_second_unlink_allows_heartbeat_writer_and_stop(self):
        job, attempt, attempt_file, path, directory = self._fixture()
        queued = ExportJob.objects.create(
            owner=self.owner,
            scope="pins",
            state="queued",
            requested_total=1,
            target_total=1,
            included_total=1,
        )
        wall = [self.now]
        stop = threading.Event()
        stop_observed = threading.Event()
        entered = threading.Event()
        release = threading.Event()
        renewed = threading.Event()
        errors = []
        results = []
        worker = ExportWorker(clock=lambda: wall[0])
        worker.worker_token = acquire_worker_lease(self.now)
        worker.heartbeat = LeaseHeartbeat(
            worker.worker_token,
            clock=lambda: wall[0],
            interval=0.01,
        )
        original_renew = worker.heartbeat._renew

        def tracked_renew(token, now):
            result = original_renew(token, now)
            if now >= self.now + timedelta(seconds=45):
                renewed.set()
            return result

        worker.heartbeat._renew = tracked_renew
        from exports.services import worker as worker_services
        original_remove = worker_services.remove_if_receipt_matches

        def blocked_remove(*args, **kwargs):
            entered.set()
            self.assertTrue(
                release.wait(5),
                "가상 NAS 장벽 해제 신호를 받지 못했습니다.",
            )
            return original_remove(*args, **kwargs)

        def run_once():
            try:
                def stop_requested():
                    requested = stop.is_set()
                    if requested:
                        stop_observed.set()
                    return requested

                results.append(worker.run_once(stop_requested))
            except Exception as error:  # pragma: no cover - assertion aid
                errors.append(error)

        worker.heartbeat.start()
        thread = threading.Thread(target=run_once)
        try:
            with mock.patch(
                "exports.services.worker.remove_if_receipt_matches",
                side_effect=blocked_remove,
            ):
                thread.start()
                self.assertTrue(
                    entered.wait(5),
                    "가상 NAS syscall 장벽에 진입하지 못했습니다.",
                )
                wall[0] = self.now + timedelta(seconds=45)
                pin = create_export_pin(
                    self.owner,
                    filename="writer-during-unlink.png",
                )
                self.assertTrue(
                    renewed.wait(5),
                    "가상 45초 뒤 heartbeat가 진행되지 않았습니다.",
                )
                stop.set()
                release.set()
                thread.join(5)
        finally:
            release.set()
            worker.heartbeat.close()
            thread.join(5)

        self.assertFalse(thread.is_alive(), "worker thread가 종료되지 않았습니다.")

        job.refresh_from_db()
        attempt.refresh_from_db()
        attempt_file.refresh_from_db()
        queued.refresh_from_db()
        pin.refresh_from_db()
        worker_row = ExportWorkerLease.objects.get(pk=1)
        self.assertEqual(errors, [])
        self.assertEqual(results, [True])
        self.assertTrue(stop_observed.is_set())
        self.assertFalse(path.exists())
        self.assertTrue(directory.exists())
        self.assertEqual(attempt_file.state, "cleaned")
        self.assertEqual(attempt.state, "retiring")
        self.assertEqual(job.staging_cleanup_state, "pending")
        self.assertEqual(queued.state, "queued")
        self.assertGreaterEqual(worker_row.heartbeat_at, wall[0])
        self.assertTrue(pin.pk)

    def test_terminal_snapshot_cleanup_persists_one_blob_per_step(self):
        job, blobs, paths, directory = self._snapshot_fixture()
        token, heartbeat = self._worker()

        outcome = self._cleanup(token, heartbeat)

        job.refresh_from_db()
        states = list(job.blobs.order_by("pk").values_list(
            "cleanup_state", flat=True,
        ))
        self.assertEqual(states.count("cleaned"), 1)
        self.assertEqual(sum(path.exists() for path in paths), 1)
        self.assertTrue(directory.exists())
        self.assertIsNotNone(job.snapshot_generation)
        self.assertFalse(outcome.claim_allowed)

    def test_attempt_cleanup_busy_retries_after_releasing_guard(self):
        job, unused_attempt, attempt_file, path, unused_directory = self._fixture()
        del unused_attempt, unused_directory
        token, heartbeat = self._worker()
        original_save = ExportAttemptFile.save
        busy = [False]
        sleeps = []

        def busy_once(instance, *args, **kwargs):
            if instance.pk == attempt_file.pk and not busy[0]:
                busy[0] = True
                raise DatabaseFenceBusy()
            return original_save(instance, *args, **kwargs)

        def retry_sleep(seconds):
            sleeps.append(seconds)
            self.assertFalse(connection.in_atomic_block)
            finished = threading.Event()
            errors = []

            def tick():
                try:
                    heartbeat.tick()
                except Exception as error:  # pragma: no cover - assertion aid
                    errors.append(error)
                finally:
                    finished.set()

            thread = threading.Thread(target=tick)
            thread.start()
            self.assertTrue(finished.wait(1))
            thread.join(1)
            self.assertEqual(errors, [])

        with mock.patch.object(ExportAttemptFile, "save", new=busy_once):
            outcome = cleanup_expired_and_stale(
                token,
                heartbeat,
                lambda: False,
                now=self.now,
                sleeper=retry_sleep,
            )

        job.refresh_from_db()
        attempt_file.refresh_from_db()
        self.assertTrue(busy[0])
        self.assertEqual(sleeps, [0.01])
        self.assertFalse(path.exists())
        self.assertEqual(attempt_file.state, "cleaned")
        self.assertFalse(outcome.claim_allowed)

    def test_snapshot_cleanup_busy_retries_after_receipted_unlink(self):
        job, blobs, paths, unused_directory = self._snapshot_fixture(count=1)
        del unused_directory
        token, heartbeat = self._worker()
        original_save = ExportBlob.save
        busy = [False]
        sleeps = []

        def busy_once(instance, *args, **kwargs):
            if instance.pk == blobs[0].pk and not busy[0]:
                busy[0] = True
                raise DatabaseFenceBusy()
            return original_save(instance, *args, **kwargs)

        with mock.patch.object(ExportBlob, "save", new=busy_once):
            cleanup_expired_and_stale(
                token,
                heartbeat,
                lambda: False,
                now=self.now,
                sleeper=lambda seconds: sleeps.append(seconds),
            )

        blobs[0].refresh_from_db()
        self.assertTrue(busy[0])
        self.assertEqual(sleeps, [0.01])
        self.assertFalse(paths[0].exists())
        self.assertEqual(blobs[0].cleanup_state, "cleaned")

    def test_orphan_delete_busy_retries_without_leaking_from_worker(self):
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
        token, heartbeat = self._worker()
        original_delete = ExportJob.delete
        busy = [False]
        sleeps = []

        def busy_once(instance, *args, **kwargs):
            if instance.pk == orphan.pk and not busy[0]:
                busy[0] = True
                raise DatabaseFenceBusy()
            return original_delete(instance, *args, **kwargs)

        with mock.patch.object(ExportJob, "delete", new=busy_once):
            outcome = cleanup_expired_and_stale(
                token,
                heartbeat,
                lambda: False,
                now=self.now,
                sleeper=lambda seconds: sleeps.append(seconds),
            )

        self.assertTrue(busy[0])
        self.assertEqual(sleeps, [0.01])
        self.assertTrue(outcome.did_work)
        self.assertFalse(ExportJob.objects.filter(pk=orphan.pk).exists())

    def test_terminal_cleanup_transient_unlink_failure_stays_pending(self):
        job, attempt, attempt_file, path, unused_directory = self._fixture()
        del attempt, unused_directory
        token, heartbeat = self._worker()

        with mock.patch(
            "exports.services.worker.remove_if_receipt_matches",
            side_effect=ExportStorageError("export_storage_unsafe"),
        ):
            outcome = self._cleanup(token, heartbeat)

        job.refresh_from_db()
        attempt_file.refresh_from_db()
        self.assertTrue(path.exists())
        self.assertEqual(job.staging_cleanup_state, "pending")
        self.assertEqual(attempt_file.state, "retiring")
        self.assertFalse(outcome.claim_allowed)

    def test_terminal_cleanup_receipt_mismatch_is_blocked(self):
        job, attempt, attempt_file, path, unused_directory = self._fixture()
        del attempt, attempt_file, unused_directory
        replacement = path.with_suffix(".replacement")
        replacement.write_bytes(b"foreign")
        os.chmod(str(replacement), 0o600)
        os.replace(str(replacement), str(path))
        token, heartbeat = self._worker()

        outcome = self._cleanup(token, heartbeat)

        job.refresh_from_db()
        self.assertEqual(path.read_bytes(), b"foreign")
        self.assertEqual(job.staging_cleanup_state, "blocked")
        self.assertFalse(outcome.claim_allowed)

    def test_publishing_source_match_and_foreign_intent_preserves_both(self):
        job, attempt, attempt_file, source, unused_directory = self._fixture()
        del unused_directory
        ready = self._ready_directory()
        intent = ready / "{}.zip".format(job.pk)
        intent.write_bytes(b"foreign intent")
        os.chmod(str(intent), 0o600)
        attempt_file.state = "retiring"
        attempt_file.intent_relative_path = "ready/{}.zip".format(job.pk)
        attempt_file.save(update_fields=("state", "intent_relative_path"))
        token, heartbeat = self._worker()

        outcome = self._cleanup(token, heartbeat)

        job.refresh_from_db()
        attempt_file.refresh_from_db()
        self.assertTrue(source.exists())
        self.assertTrue(intent.exists())
        self.assertEqual(job.staging_cleanup_state, "blocked")
        self.assertEqual(attempt_file.state, "retiring")
        self.assertEqual(
            attempt_file.intent_relative_path,
            "ready/{}.zip".format(job.pk),
        )
        self.assertFalse(outcome.claim_allowed)

    def test_publishing_intent_match_and_foreign_source_preserves_both(self):
        job, attempt, attempt_file, source, unused_directory = self._fixture()
        del unused_directory
        ready = self._ready_directory()
        intent = ready / "{}.zip".format(job.pk)
        os.replace(str(source), str(intent))
        source.write_bytes(b"foreign source")
        os.chmod(str(source), 0o600)
        attempt_file.state = "retiring"
        attempt_file.intent_relative_path = "ready/{}.zip".format(job.pk)
        self._refresh_full_receipt(attempt_file, intent)
        token, heartbeat = self._worker()

        outcome = self._cleanup(token, heartbeat)

        job.refresh_from_db()
        attempt_file.refresh_from_db()
        self.assertTrue(source.exists())
        self.assertTrue(intent.exists())
        self.assertEqual(job.staging_cleanup_state, "blocked")
        self.assertEqual(attempt_file.state, "retiring")
        self.assertIsNotNone(attempt_file.intent_relative_path)
        self.assertFalse(outcome.claim_allowed)

    def test_quarantine_open_receipt_is_closed_before_terminal_unlink(self):
        job, attempt, attempt_file, source, unused_directory = self._fixture()
        del unused_directory
        ready = self._ready_directory()
        ready_path = ready / "{}.zip".format(job.pk)
        os.replace(str(source), str(ready_path))
        descriptor = os.open(str(ready_path), os.O_RDONLY)
        try:
            opened = OpenFileReceipt.from_fd(
                descriptor,
                os.getuid(),
                os.getgid(),
            )
        finally:
            os.close(descriptor)
        attempt_file.kind = "quarantine"
        attempt_file.state = "writing"
        attempt_file.receipt_level = "open"
        attempt_file.relative_path = "ready/{}.zip".format(job.pk)
        attempt_file.intent_relative_path = (
            "{}/quarantine-ready-0.zip".format(attempt.relative_path)
        )
        attempt_file.receipt_dev = opened.dev
        attempt_file.receipt_ino = opened.ino
        attempt_file.receipt_uid = opened.uid
        attempt_file.receipt_gid = opened.gid
        attempt_file.receipt_mode = opened.mode
        attempt_file.receipt_nlink = opened.nlink
        attempt_file.receipt_size = None
        attempt_file.receipt_mtime_ns = None
        attempt_file.receipt_ctime_ns = None
        attempt_file.receipt_sha256 = None
        attempt_file.save()
        token, heartbeat = self._worker()

        first = self._cleanup(token, heartbeat)

        attempt_file.refresh_from_db()
        self.assertTrue(ready_path.exists())
        self.assertEqual(attempt_file.state, "closed")
        self.assertEqual(attempt_file.receipt_level, "full")
        self.assertIsNotNone(attempt_file.receipt_size)
        self.assertIsNotNone(attempt_file.receipt_ctime_ns)
        self.assertFalse(first.claim_allowed)

    def test_quarantine_matching_source_and_foreign_intent_blocks_both(self):
        job, attempt, attempt_file, source, directory = self._fixture()
        intent = directory / "quarantine.zip"
        intent.write_bytes(b"foreign intent")
        os.chmod(str(intent), 0o600)
        relative = "{}/{}".format(attempt.relative_path, intent.name)
        self._set_quarantine_open_receipt(attempt_file, source, relative)
        token, heartbeat = self._worker()

        outcome = self._cleanup(token, heartbeat)

        job.refresh_from_db()
        attempt_file.refresh_from_db()
        self.assertTrue(source.exists())
        self.assertTrue(intent.exists())
        self.assertEqual(job.staging_cleanup_state, "blocked")
        self.assertEqual(attempt_file.state, "writing")
        self.assertEqual(attempt_file.intent_relative_path, relative)
        self.assertFalse(outcome.claim_allowed)

    def test_quarantine_matching_intent_and_foreign_source_blocks_both(self):
        job, attempt, attempt_file, source, directory = self._fixture()
        intent = directory / "quarantine.zip"
        os.replace(str(source), str(intent))
        source.write_bytes(b"foreign source")
        os.chmod(str(source), 0o600)
        relative = "{}/{}".format(attempt.relative_path, intent.name)
        self._set_quarantine_open_receipt(attempt_file, intent, relative)
        token, heartbeat = self._worker()

        outcome = self._cleanup(token, heartbeat)

        job.refresh_from_db()
        attempt_file.refresh_from_db()
        self.assertTrue(source.exists())
        self.assertTrue(intent.exists())
        self.assertEqual(job.staging_cleanup_state, "blocked")
        self.assertEqual(attempt_file.state, "writing")
        self.assertEqual(attempt_file.intent_relative_path, relative)
        self.assertFalse(outcome.claim_allowed)
