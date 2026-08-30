from contextlib import contextmanager
from datetime import timedelta
import hashlib
import os
from pathlib import Path
import uuid

from django.conf import settings
from django.db import connection
from django.test import TransactionTestCase
from django.utils import timezone
import mock

from exports.contracts import ERROR_CONTRACTS, LeaseToken, StopRequested
from exports.models import (
    ExportAttempt,
    ExportAttemptFile,
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

from .helpers import ExportStorageMixin, create_export_user


class WorkerRecoveryTests(TransactionTestCase):
    def setUp(self):
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

    def _cleanup(self, token, heartbeat):
        return cleanup_expired_and_stale(
            token,
            heartbeat,
            lambda: False,
            now=self.now,
        )

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
