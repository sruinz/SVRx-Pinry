from datetime import timedelta
import os
from pathlib import Path
import threading
import uuid

from django.conf import settings
from django.db import IntegrityError, transaction
from django.test import TransactionTestCase
from django.utils import timezone
import mock

from core.services.database_fence import DatabaseFenceBusy
from exports.contracts import LeaseLost, LeaseToken
from exports.models import ExportJob, ExportSlot, ExportTarget, ExportWorkerLease
from exports.services.archive import ArchiveService
from exports.services.file_ops import (
    ExportStorageError,
    ExportWorkerLock,
    open_export_root,
)
from exports.services.worker import (
    ExportWorker,
    LeaseHeartbeat,
    acquire_worker_lease,
    claim_next_job,
    release_worker_lease,
)

from .helpers import (
    ExportStorageMixin,
    create_export_pin,
    create_export_user,
)


class MutableClock(object):
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


class WorkerLeaseTests(TransactionTestCase):
    def setUp(self):
        self.now = timezone.now()

    def _job(self, owner, created_at):
        job = ExportJob.objects.create(
            owner=owner,
            scope="pins",
            state="queued",
            requested_total=1,
            target_total=1,
            included_total=1,
            archive_total=1,
            bytes_total=7,
        )
        ExportJob.objects.filter(pk=job.pk).update(created_at=created_at)
        job.refresh_from_db()
        return job

    def test_new_os_lock_owner_takes_over_without_waiting_for_db_expiry(self):
        old_uuid = uuid.uuid4()
        ExportWorkerLease.objects.create(
            pk=1,
            generation=7,
            lease_uuid=old_uuid,
            lease_expires_at=self.now + timedelta(hours=1),
            heartbeat_at=self.now,
            health_state="ready",
        )

        token = acquire_worker_lease(self.now + timedelta(seconds=1))

        current = ExportWorkerLease.objects.get(pk=1)
        self.assertEqual(token.worker_generation, 8)
        self.assertNotEqual(token.worker_lease_uuid, old_uuid)
        self.assertEqual(current.generation, 8)
        self.assertEqual(current.lease_uuid, token.worker_lease_uuid)

    def test_worker_lease_singleton_rejects_noncanonical_primary_key(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ExportWorkerLease.objects.create(pk=2)

    def test_fifo_claim_publishes_token_only_after_commit(self):
        owner_a = create_export_user("worker-fifo-a")
        owner_b = create_export_user("worker-fifo-b")
        oldest = self._job(owner_a, self.now - timedelta(seconds=2))
        self._job(owner_b, self.now - timedelta(seconds=1))
        worker = acquire_worker_lease(self.now)
        heartbeat = LeaseHeartbeat(worker, clock=lambda: self.now)

        lease = claim_next_job(worker, heartbeat, self.now)

        self.assertEqual(lease.job_id, oldest.pk)
        self.assertEqual(heartbeat.current_job_token, lease)
        oldest.refresh_from_db()
        self.assertEqual(oldest.state, "snapshotting")
        self.assertEqual(oldest.worker_generation, worker.worker_generation)
        self.assertEqual(oldest.lease_uuid, lease.job_lease_uuid)
        self.assertEqual(oldest.attempt_generation, lease.attempt_generation)
        self.assertIsNone(claim_next_job(worker, heartbeat, self.now))

    def test_claim_transition_blocks_tick_until_new_token_is_published(self):
        owner = create_export_user("worker-publish-barrier")
        job = self._job(owner, self.now)
        worker = acquire_worker_lease(self.now)
        heartbeat = LeaseHeartbeat(worker, clock=lambda: self.now)
        entered = threading.Event()
        finished = threading.Event()
        errors = []

        with heartbeat.job_token_transition(None) as transition:
            lease = LeaseToken(
                worker.worker_generation,
                worker.worker_lease_uuid,
                job.pk,
                uuid.uuid4(),
                0,
            )
            ExportJob.objects.filter(pk=job.pk).update(
                state="snapshotting",
                worker_generation=worker.worker_generation,
                lease_uuid=lease.job_lease_uuid,
                lease_expires_at=self.now + timedelta(seconds=30),
            )

            def tick():
                entered.set()
                try:
                    heartbeat.tick(self.now)
                except Exception as error:  # pragma: no cover - 아래서 검증
                    errors.append(error)
                finally:
                    finished.set()

            thread = threading.Thread(target=tick)
            thread.start()
            self.assertTrue(entered.wait(1))
            self.assertFalse(finished.wait(0.05))
            transition.publish(lease)

        thread.join(1)
        self.assertFalse(errors)
        self.assertTrue(finished.is_set())

    def test_replace_and_clear_transitions_hide_old_token_from_tick(self):
        owner = create_export_user("worker-replace-clear")
        job = self._job(owner, self.now)
        worker = acquire_worker_lease(self.now)
        old = LeaseToken(
            worker.worker_generation,
            worker.worker_lease_uuid,
            job.pk,
            uuid.uuid4(),
            0,
        )
        ExportJob.objects.filter(pk=job.pk).update(
            state="archiving",
            worker_generation=worker.worker_generation,
            lease_uuid=old.job_lease_uuid,
            attempt_generation=0,
        )
        heartbeat = LeaseHeartbeat(worker, clock=lambda: self.now)
        with heartbeat.job_token_transition(None) as transition:
            transition.publish(old)
        new = LeaseToken(
            old.worker_generation,
            old.worker_lease_uuid,
            old.job_id,
            old.job_lease_uuid,
            1,
        )
        with heartbeat.job_token_transition(old) as transition:
            ExportJob.objects.filter(pk=job.pk).update(attempt_generation=1)
            entered = threading.Event()
            finished = threading.Event()

            def tick_during_replace():
                entered.set()
                heartbeat.tick(self.now)
                finished.set()

            thread = threading.Thread(target=tick_during_replace)
            thread.start()
            self.assertTrue(entered.wait(1))
            self.assertFalse(finished.wait(0.05))
            transition.replace(new)
        thread.join(1)
        self.assertTrue(finished.is_set())
        heartbeat.tick(self.now)
        self.assertEqual(heartbeat.current_job_token, new)

        with heartbeat.job_token_transition(new) as transition:
            ExportJob.objects.filter(pk=job.pk).update(
                lease_uuid=None,
                lease_expires_at=None,
            )
            entered = threading.Event()
            finished = threading.Event()

            def tick_during_clear():
                entered.set()
                heartbeat.tick(self.now)
                finished.set()

            thread = threading.Thread(target=tick_during_clear)
            thread.start()
            self.assertTrue(entered.wait(1))
            self.assertFalse(finished.wait(0.05))
            transition.clear()
        thread.join(1)
        self.assertTrue(finished.is_set())
        heartbeat.tick(self.now)
        self.assertIsNone(heartbeat.current_job_token)

    def test_same_generation_expiry_is_not_authority_loss(self):
        owner = create_export_user("worker-self-expiry")
        job = self._job(owner, self.now)
        worker = acquire_worker_lease(self.now)
        heartbeat = LeaseHeartbeat(worker, clock=lambda: self.now)
        lease = claim_next_job(worker, heartbeat, self.now)
        expired = self.now - timedelta(seconds=1)
        ExportWorkerLease.objects.filter(pk=1).update(
            lease_expires_at=expired,
        )
        ExportJob.objects.filter(pk=job.pk).update(lease_expires_at=expired)

        heartbeat.renew_now(lease, now=self.now + timedelta(seconds=1))

        job.refresh_from_db()
        worker_row = ExportWorkerLease.objects.get(pk=1)
        self.assertGreater(worker_row.lease_expires_at, self.now)
        self.assertGreater(job.lease_expires_at, self.now)

    def test_old_generation_heartbeat_fails_after_takeover(self):
        owner = create_export_user("worker-stale")
        self._job(owner, self.now)
        first_worker = acquire_worker_lease(self.now)
        first_heartbeat = LeaseHeartbeat(first_worker, clock=lambda: self.now)
        first_lease = claim_next_job(first_worker, first_heartbeat, self.now)
        acquire_worker_lease(self.now + timedelta(seconds=1))

        with self.assertRaises(LeaseLost):
            first_heartbeat.renew_now(
                first_lease,
                now=self.now + timedelta(seconds=2),
            )

    def test_database_busy_tick_is_not_reported_as_lease_loss(self):
        worker = acquire_worker_lease(self.now)
        heartbeat = LeaseHeartbeat(
            worker,
            clock=lambda: self.now,
            interval=0.01,
        )
        called = threading.Event()

        def busy(token, now):
            del token, now
            called.set()
            raise DatabaseFenceBusy()

        with mock.patch.object(heartbeat, "_renew", side_effect=busy):
            heartbeat.start()
            self.assertTrue(called.wait(1))
            heartbeat.close()

        self.assertIsNone(heartbeat.lost)

    def test_stale_worker_does_not_release_new_global_lease(self):
        stale = acquire_worker_lease(self.now)
        current = acquire_worker_lease(self.now + timedelta(seconds=1))

        with self.assertRaises(LeaseLost):
            release_worker_lease(stale, now=self.now + timedelta(seconds=2))

        row = ExportWorkerLease.objects.get(pk=1)
        self.assertEqual(row.generation, current.worker_generation)
        self.assertEqual(row.lease_uuid, current.worker_lease_uuid)


class WorkerLockTests(ExportStorageMixin, TransactionTestCase):
    def test_process_flock_rejects_a_second_worker(self):
        root = open_export_root(
            settings.PINRY_EXPORT_ROOT,
            os.getuid(),
            os.getgid(),
        )
        first = ExportWorkerLock.acquire(root, root.uid, root.gid)
        try:
            with self.assertRaises(ExportStorageError) as raised:
                ExportWorkerLock.acquire(root, root.uid, root.gid)
            self.assertEqual(raised.exception.code, "export_worker_unavailable")
        finally:
            first.close()
            root.close()


class WorkerDispatchIntegrationTests(ExportStorageMixin, TransactionTestCase):
    def setUp(self):
        super(WorkerDispatchIntegrationTests, self).setUp()
        self.now = timezone.now()
        staging = Path(settings.PINRY_EXPORT_ROOT, ".staging")
        staging.mkdir(mode=0o700)
        os.chmod(str(staging), 0o700)
        self.owner = create_export_user("worker-dispatch-owner")

    def _queued_job(self, filename):
        pin = create_export_pin(self.owner, filename=filename)
        size = os.path.getsize(pin.image.image.path)
        job = ExportJob.objects.create(
            owner=self.owner,
            scope="pins",
            state="queued",
            requested_total=1,
            target_total=1,
            included_total=1,
            bytes_total=size,
        )
        ExportTarget.objects.create(
            job=job,
            position=0,
            pin_id=pin.pk,
            pin_owner_id_snapshot=pin.submitter_id,
            pin_published_at_snapshot=pin.published,
        )
        ExportSlot.objects.update_or_create(
            owner=self.owner,
            defaults={"current_job": job},
        )
        return job

    def _worker(self, archive_service=None):
        worker = ExportWorker(
            clock=lambda: self.now,
            archive_service=archive_service,
        )
        worker.worker_token = worker.acquire_worker_lease(self.now)
        worker.heartbeat = LeaseHeartbeat(
            worker.worker_token,
            clock=lambda: self.now,
        )
        return worker

    def test_run_once_completes_job_and_releases_terminal_db_lease(self):
        job = self._queued_job("worker-complete.png")
        worker = self._worker()

        with mock.patch("exports.services.file_ops._normalize_metadata"):
            self.assertTrue(worker.run_once(lambda: False))

        job.refresh_from_db()
        self.assertEqual(job.state, "complete")
        self.assertEqual(job.staging_cleanup_state, "cleaned")
        self.assertIsNone(job.lease_uuid)
        self.assertIsNone(worker.heartbeat.current_job_token)
        self.assertTrue(Path(
            settings.PINRY_EXPORT_ROOT,
            job.ready_relative_path,
        ).exists())

    def test_complete_commit_crash_is_claimed_cleaned_and_released(self):
        job = self._queued_job("worker-complete-crash.png")

        def crash(point, context):
            del context
            if point == "after_complete_commit":
                raise RuntimeError("simulated process exit")

        first = self._worker(archive_service=ArchiveService(
            fault_injector=crash,
        ))
        with mock.patch("exports.services.file_ops._normalize_metadata"):
            with self.assertRaisesRegex(RuntimeError, "simulated process exit"):
                first.run_once(lambda: False)
        job.refresh_from_db()
        previous_resume_count = job.resume_count
        self.assertEqual(job.state, "complete")
        self.assertEqual(job.staging_cleanup_state, "pending")
        self.assertIsNotNone(job.lease_uuid)
        self.assertIsNone(first.heartbeat.current_job_token)

        second = self._worker()
        self.assertTrue(second.run_once(lambda: False))

        job.refresh_from_db()
        self.assertEqual(job.state, "complete")
        self.assertEqual(job.staging_cleanup_state, "cleaned")
        self.assertEqual(job.resume_count, previous_resume_count)
        self.assertIsNone(job.lease_uuid)
        self.assertIsNone(second.heartbeat.current_job_token)

    def test_new_success_expires_previous_then_next_maintenance_unlinks_it(self):
        first_job = self._queued_job("worker-previous.png")
        worker = self._worker()
        with mock.patch("exports.services.file_ops._normalize_metadata"):
            self.assertTrue(worker.run_once(lambda: False))
        first_job.refresh_from_db()
        first_path = Path(
            settings.PINRY_EXPORT_ROOT,
            first_job.ready_relative_path,
        )
        self.assertTrue(first_path.exists())

        second_job = self._queued_job("worker-current.png")
        with mock.patch("exports.services.file_ops._normalize_metadata"):
            self.assertTrue(worker.run_once(lambda: False))

        first_job.refresh_from_db()
        second_job.refresh_from_db()
        self.assertEqual(first_job.state, "expired")
        self.assertEqual(first_job.ready_cleanup_state, "pending")
        self.assertTrue(first_path.exists())
        self.assertEqual(second_job.state, "complete")

        self.assertTrue(worker.run_once(lambda: False))

        first_job.refresh_from_db()
        self.assertEqual(first_job.ready_cleanup_state, "cleaned")
        self.assertFalse(first_path.exists())
