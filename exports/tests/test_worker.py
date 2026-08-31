from datetime import timedelta
import os
from pathlib import Path
import sqlite3
import threading
import uuid

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import (
    DatabaseError,
    IntegrityError,
    OperationalError,
    connection,
    transaction,
)
from django.db.models.query import QuerySet
from django.test import TransactionTestCase
from django.utils import timezone
import mock

from core.services.database_fence import DatabaseFenceBusy
from core.models import Pin
from exports.contracts import ExportError, LeaseLost, LeaseToken, StopRequested
from exports.models import ExportJob, ExportSlot, ExportTarget, ExportWorkerLease
from exports.services.archive import ArchiveService
from exports.services.attempts import AttemptService
from exports.services.file_ops import (
    ExportStorageError,
    ExportWorkerLock,
    open_export_root,
)
from exports.services.snapshot import SnapshotService, open_staging_directory
from exports.services.worker import (
    ExportWorker,
    LeaseHeartbeat,
    acquire_worker_lease,
    claim_next_job,
    fail_job,
    handoff_after_normal_stop,
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

    def _terminal_interrupt_fixture(self, exception_type):
        owner = create_export_user(
            "worker-terminal-{}".format(exception_type),
        )
        job = self._job(owner, self.now)
        generation = uuid.uuid4()
        ExportJob.objects.filter(pk=job.pk).update(
            snapshot_generation=generation,
            snapshot_relative_path="snapshot-{}".format(job.pk),
            snapshot_dir_dev=1,
            snapshot_dir_ino=2,
            snapshot_dir_uid=3,
            snapshot_dir_gid=4,
            snapshot_dir_mode=0o700,
        )

        class TerminalInterruptArchive(object):
            def recover_archiving(
                inner_self,
                lease,
                heartbeat,
                stop_requested,
            ):
                del inner_self, stop_requested
                ExportJob.objects.filter(pk=lease.job_id).update(
                    state="complete",
                    completed_at=self.now,
                    expires_at=self.now + timedelta(hours=24),
                    staging_cleanup_state="pending",
                    ready_cleanup_state="retained",
                    ready_relative_path="ready/{}.zip".format(lease.job_id),
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
                )
                with heartbeat.job_token_transition(lease) as transition:
                    transition.clear()
                if exception_type == "stop":
                    raise StopRequested(lease)
                raise ExportError("archive_failed", lease)

        worker = ExportWorker(
            clock=lambda: self.now,
            sleeper=lambda seconds: None,
            archive_service=TerminalInterruptArchive(),
        )
        worker.worker_token = worker.acquire_worker_lease(self.now)
        worker.heartbeat = LeaseHeartbeat(
            worker.worker_token,
            clock=lambda: self.now,
        )
        return job, worker

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

    def test_committed_claim_survives_deadline_after_local_publish(self):
        owner = create_export_user("worker-claim-post-commit-deadline")
        job = self._job(owner, self.now)
        elapsed = [0.0]
        worker = acquire_worker_lease(self.now)
        heartbeat = LeaseHeartbeat(
            worker,
            clock=lambda: self.now,
            monotonic=lambda: elapsed[0],
            sleeper=lambda seconds: None,
        )
        from exports.services import worker as worker_services
        original_publish = worker_services._TokenTransition.publish
        publishes = [0]

        def expire_after_publish(transition, lease):
            result = original_publish(transition, lease)
            publishes[0] += 1
            elapsed[0] = 15.0
            return result

        with mock.patch.object(
                worker_services._TokenTransition,
                "publish",
                new=expire_after_publish,
        ):
            lease = claim_next_job(
                worker,
                heartbeat,
                self.now,
                monotonic=lambda: elapsed[0],
                sleeper=lambda seconds: None,
            )

        job.refresh_from_db()
        self.assertEqual(publishes[0], 1)
        self.assertEqual(job.state, "snapshotting")
        self.assertEqual(job.lease_uuid, lease.job_lease_uuid)
        self.assertEqual(heartbeat.current_job_token, lease)

    def test_terminal_release_survives_deadline_after_local_clear(self):
        owner = create_export_user("worker-release-post-commit-deadline")
        job = self._job(owner, self.now)
        elapsed = [0.0]
        worker = ExportWorker(
            clock=lambda: self.now,
            monotonic=lambda: elapsed[0],
            sleeper=lambda seconds: None,
        )
        worker.worker_token = worker.acquire_worker_lease(self.now)
        worker.heartbeat = LeaseHeartbeat(
            worker.worker_token,
            clock=lambda: self.now,
            monotonic=lambda: elapsed[0],
            sleeper=lambda seconds: None,
        )
        lease = worker.claim_next_job(
            worker.worker_token,
            worker.heartbeat,
            self.now,
        )
        ExportJob.objects.filter(pk=job.pk).update(
            state="failed",
            error_code="archive_failed",
            error_class="retryable",
            error_retryable=True,
            staging_cleanup_state="pending",
        )
        from exports.services import worker as worker_services
        original_clear = worker_services._TokenTransition.clear
        clears = [0]

        def expire_after_clear(transition):
            result = original_clear(transition)
            clears[0] += 1
            elapsed[0] = 15.0
            return result

        with mock.patch.object(
                worker_services._TokenTransition,
                "clear",
                new=expire_after_clear,
        ):
            worker._release_terminal(lease)

        job.refresh_from_db()
        self.assertEqual(clears[0], 1)
        self.assertIsNone(job.lease_uuid)
        self.assertIsNone(worker.heartbeat.current_job_token)

    def test_claim_transition_blocks_tick_until_new_token_is_published(self):
        owner = create_export_user("worker-publish-barrier")
        job = self._job(owner, self.now)
        worker = acquire_worker_lease(self.now)
        heartbeat = LeaseHeartbeat(worker, clock=lambda: self.now)
        from exports.services import worker as worker_services
        original_publish = worker_services._TokenTransition.publish
        observed = []

        def checked_publish(transition, lease):
            job.refresh_from_db()
            observed.append((connection.in_atomic_block, job.state))
            return original_publish(transition, lease)

        with mock.patch.object(
            worker_services._TokenTransition,
            "publish",
            new=checked_publish,
        ):
            lease = claim_next_job(worker, heartbeat, self.now)

        self.assertEqual(observed, [(False, "snapshotting")])
        self.assertEqual(heartbeat.current_job_token, lease)

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

    def test_nonbusy_database_error_is_reported_as_fatal(self):
        worker = acquire_worker_lease(self.now)
        heartbeat = LeaseHeartbeat(
            worker,
            clock=lambda: self.now,
            interval=0.01,
        )
        called = threading.Event()

        def fatal(token, now):
            del token, now
            called.set()
            raise DatabaseError("fatal database failure")

        with mock.patch.object(heartbeat, "_renew", side_effect=fatal):
            heartbeat.start()
            self.assertTrue(called.wait(1))
            heartbeat.close()

        self.assertIsNone(heartbeat.lost)
        self.assertIsInstance(heartbeat.fatal, DatabaseError)

    def test_claim_deadline_rolls_back_then_retries_outside_the_guards(self):
        owner = create_export_user("worker-claim-deadline")
        job = self._job(owner, self.now)
        worker = acquire_worker_lease(self.now)
        heartbeat_now = MutableClock(self.now)
        elapsed = [0.0]
        sleeps = []
        tick_errors = []
        tick_finished = threading.Event()
        heartbeat = LeaseHeartbeat(
            worker,
            clock=heartbeat_now,
            monotonic=lambda: elapsed[0],
            sleeper=lambda seconds: None,
        )
        original_save = ExportJob.save
        save_count = [0]

        def slow_save(instance, *args, **kwargs):
            result = original_save(instance, *args, **kwargs)
            save_count[0] += 1
            if save_count[0] == 1:
                elapsed[0] = 6.0
            return result

        def retry_sleep(seconds):
            sleeps.append(seconds)
            self.assertFalse(connection.in_atomic_block)
            heartbeat_now.value = self.now + timedelta(seconds=1)

            def tick():
                try:
                    heartbeat.tick()
                except Exception as error:  # pragma: no cover - assertion aid
                    tick_errors.append(error)
                finally:
                    tick_finished.set()

            thread = threading.Thread(target=tick)
            thread.start()
            self.assertTrue(tick_finished.wait(1))
            thread.join(1)

        with mock.patch.object(ExportJob, "save", new=slow_save):
            lease = claim_next_job(
                worker,
                heartbeat,
                self.now,
                monotonic=lambda: elapsed[0],
                sleeper=retry_sleep,
            )

        job.refresh_from_db()
        worker_row = ExportWorkerLease.objects.get(pk=1)
        self.assertEqual(save_count[0], 2)
        self.assertEqual(sleeps, [0.01])
        self.assertEqual(tick_errors, [])
        self.assertEqual(job.state, "snapshotting")
        self.assertEqual(job.lease_uuid, lease.job_lease_uuid)
        self.assertEqual(heartbeat.current_job_token, lease)
        self.assertEqual(worker_row.heartbeat_at, heartbeat_now.value)

    def test_claim_retry_stops_without_stale_write_or_next_claim(self):
        owner = create_export_user("worker-claim-stop")
        first = self._job(owner, self.now)
        second = self._job(owner, self.now + timedelta(seconds=1))
        elapsed = [0.0]
        stopped = threading.Event()
        original_save = ExportJob.save
        save_count = [0]
        worker = ExportWorker(
            clock=lambda: self.now,
            monotonic=lambda: elapsed[0],
            sleeper=lambda seconds: None,
        )
        worker.worker_token = worker.acquire_worker_lease(self.now)
        worker.heartbeat = LeaseHeartbeat(
            worker.worker_token,
            clock=lambda: self.now,
            monotonic=lambda: elapsed[0],
            sleeper=lambda seconds: None,
        )

        def slow_save(instance, *args, **kwargs):
            result = original_save(instance, *args, **kwargs)
            if instance.pk == first.pk:
                save_count[0] += 1
                elapsed[0] = 16.0
                stopped.set()
            return result

        with mock.patch.object(ExportJob, "save", new=slow_save):
            self.assertFalse(worker.run_once(stopped.is_set))

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(save_count[0], 1)
        self.assertEqual(first.state, "queued")
        self.assertEqual(second.state, "queued")
        self.assertIsNone(first.lease_uuid)
        self.assertIsNone(second.lease_uuid)
        self.assertIsNone(worker.heartbeat.current_job_token)

    def test_claim_retry_detects_worker_takeover_before_stale_write(self):
        owner = create_export_user("worker-claim-takeover")
        job = self._job(owner, self.now)
        first_worker = acquire_worker_lease(self.now)
        heartbeat = LeaseHeartbeat(first_worker, clock=lambda: self.now)
        original_save = ExportJob.save
        save_count = [0]

        def busy_once(instance, *args, **kwargs):
            if instance.pk == job.pk and save_count[0] == 0:
                save_count[0] += 1
                raise DatabaseFenceBusy()
            return original_save(instance, *args, **kwargs)

        def takeover(unused_seconds):
            acquire_worker_lease(self.now + timedelta(seconds=1))

        with mock.patch.object(ExportJob, "save", new=busy_once):
            with self.assertRaises(LeaseLost):
                claim_next_job(
                    first_worker,
                    heartbeat,
                    self.now,
                    sleeper=takeover,
                )

        job.refresh_from_db()
        self.assertEqual(save_count[0], 1)
        self.assertEqual(job.state, "queued")
        self.assertIsNone(job.lease_uuid)
        self.assertIsNone(heartbeat.current_job_token)

    def test_handoff_and_fail_clear_local_token_only_after_commit(self):
        owner = create_export_user("worker-real-clear-barriers")
        job = self._job(owner, self.now)
        worker = acquire_worker_lease(self.now)
        heartbeat = LeaseHeartbeat(worker, clock=lambda: self.now)
        observed = []
        from exports.services import worker as worker_services
        original_clear = worker_services._TokenTransition.clear

        def checked_clear(transition):
            job.refresh_from_db()
            observed.append((connection.in_atomic_block, job.state))
            return original_clear(transition)

        lease = claim_next_job(worker, heartbeat, self.now)
        with mock.patch.object(
            worker_services._TokenTransition,
            "clear",
            new=checked_clear,
        ):
            handoff_after_normal_stop(lease, heartbeat, self.now)
        lease = claim_next_job(worker, heartbeat, self.now)
        with mock.patch.object(
            worker_services._TokenTransition,
            "clear",
            new=checked_clear,
        ):
            fail_job(lease, "archive_failed", heartbeat, self.now)

        self.assertEqual(observed, [(False, "queued"), (False, "failed")])
        self.assertIsNone(heartbeat.current_job_token)

    def test_active_job_busy_retries_same_token_outside_transition_mutex(self):
        owner = create_export_user("worker-active-token-retry")
        job = self._job(owner, self.now)
        worker = acquire_worker_lease(self.now)
        heartbeat = LeaseHeartbeat(worker, clock=lambda: self.now)
        lease = claim_next_job(worker, heartbeat, self.now)
        original_save = ExportJob.save
        busy = [False]
        tick_finished = threading.Event()
        tick_errors = []

        def busy_once(instance, *args, **kwargs):
            if instance.pk == job.pk and not busy[0]:
                busy[0] = True
                raise DatabaseFenceBusy()
            return original_save(instance, *args, **kwargs)

        def retry_sleep(unused_seconds):
            self.assertFalse(connection.in_atomic_block)
            self.assertEqual(heartbeat.current_job_token, lease)

            def tick():
                try:
                    heartbeat.tick()
                except Exception as error:  # pragma: no cover - assertion aid
                    tick_errors.append(error)
                finally:
                    tick_finished.set()

            thread = threading.Thread(target=tick)
            thread.start()
            self.assertTrue(tick_finished.wait(1))
            thread.join(1)

        with mock.patch.object(ExportJob, "save", new=busy_once):
            failed = fail_job(
                lease,
                "archive_failed",
                heartbeat,
                self.now,
                sleeper=retry_sleep,
            )

        self.assertTrue(busy[0])
        self.assertEqual(tick_errors, [])
        self.assertEqual(failed.state, "failed")
        self.assertIsNone(heartbeat.current_job_token)

    def test_handoff_read_busy_retries_with_the_same_active_token(self):
        owner = create_export_user("worker-handoff-read-busy")
        job = self._job(owner, self.now)
        worker = acquire_worker_lease(self.now)
        heartbeat = LeaseHeartbeat(worker, clock=lambda: self.now)
        lease = claim_next_job(worker, heartbeat, self.now)
        original_get = QuerySet.get
        busy = [False]
        sleeps = []

        def busy_first_job_read(queryset, *args, **kwargs):
            if (
                queryset.model is ExportJob
                and kwargs.get("pk") == job.pk
                and not busy[0]
            ):
                busy[0] = True
                raise DatabaseError("database is locked")
            return original_get(queryset, *args, **kwargs)

        def retry_sleep(seconds):
            self.assertFalse(connection.in_atomic_block)
            self.assertEqual(heartbeat.current_job_token, lease)
            sleeps.append(seconds)

        with mock.patch.object(QuerySet, "get", new=busy_first_job_read):
            handoff_after_normal_stop(
                lease,
                heartbeat,
                self.now,
                sleeper=retry_sleep,
            )

        job.refresh_from_db()
        self.assertTrue(busy[0])
        self.assertEqual(sleeps, [0.01])
        self.assertEqual(job.state, "queued")
        self.assertIsNone(job.lease_uuid)
        self.assertIsNone(heartbeat.current_job_token)

    def test_terminal_release_read_busy_retries_with_the_same_lease(self):
        owner = create_export_user("worker-terminal-release-read-busy")
        job = self._job(owner, self.now)
        worker = ExportWorker(clock=lambda: self.now)
        worker.worker_token = worker.acquire_worker_lease(self.now)
        worker.heartbeat = LeaseHeartbeat(
            worker.worker_token,
            clock=lambda: self.now,
        )
        lease = worker.claim_next_job(
            worker.worker_token,
            worker.heartbeat,
            self.now,
        )
        ExportJob.objects.filter(pk=job.pk).update(
            state="failed",
            error_code="archive_failed",
            error_class="retryable",
            error_retryable=True,
            staging_cleanup_state="pending",
        )
        original_get = QuerySet.get
        busy = [False]
        sleeps = []

        def busy_first_job_read(queryset, *args, **kwargs):
            if (
                queryset.model is ExportJob
                and kwargs.get("pk") == job.pk
                and not busy[0]
            ):
                busy[0] = True
                raise DatabaseError("database is locked")
            return original_get(queryset, *args, **kwargs)

        worker.sleeper = lambda seconds: sleeps.append(seconds)
        with mock.patch.object(QuerySet, "get", new=busy_first_job_read):
            worker._release_terminal(lease)

        job.refresh_from_db()
        self.assertTrue(busy[0])
        self.assertEqual(sleeps, [0.01])
        self.assertIsNone(job.lease_uuid)
        self.assertIsNone(worker.heartbeat.current_job_token)

    def test_handoff_totals_busy_retries_inside_the_same_operation(self):
        owner = create_export_user("worker-handoff-totals-busy")
        job = self._job(owner, self.now)
        worker = acquire_worker_lease(self.now)
        heartbeat = LeaseHeartbeat(worker, clock=lambda: self.now)
        lease = claim_next_job(worker, heartbeat, self.now)
        job.attempts.create(
            attempt_generation=lease.attempt_generation,
            lease_uuid=lease.job_lease_uuid,
            state="cleaned",
            relative_path="attempt-{}-0".format(job.pk),
            dir_dev=1,
            dir_ino=2,
            dir_uid=3,
            dir_gid=4,
            dir_mode=0o700,
        )
        from exports.services import worker as worker_services
        original_totals = worker_services._included_totals
        calls = [0]
        sleeps = []

        def busy_first_totals(*args, **kwargs):
            calls[0] += 1
            if calls[0] == 1:
                raise DatabaseError("database is locked")
            return original_totals(*args, **kwargs)

        with mock.patch.object(
                worker_services,
                "_included_totals",
                side_effect=busy_first_totals,
        ):
            handoff_after_normal_stop(
                lease,
                heartbeat,
                self.now,
                sleeper=lambda seconds: sleeps.append(seconds),
            )

        job.refresh_from_db()
        self.assertEqual(calls[0], 2)
        self.assertEqual(sleeps, [0.01])
        self.assertEqual(job.state, "queued")
        self.assertEqual(job.attempt_generation, 1)
        self.assertIsNone(job.lease_uuid)
        self.assertIsNone(heartbeat.current_job_token)

    def test_active_retry_and_heartbeat_share_one_hard_budget(self):
        owner = create_export_user("worker-shared-retry-budget")
        job = self._job(owner, self.now)
        elapsed = [0.0]
        foreground_failed = [False]
        worker = acquire_worker_lease(self.now)
        heartbeat = LeaseHeartbeat(
            worker,
            clock=lambda: self.now,
            monotonic=lambda: elapsed[0],
            sleeper=lambda seconds: None,
        )
        lease = claim_next_job(worker, heartbeat, self.now)
        original_save = ExportJob.save
        original_get = QuerySet.get

        def busy_job_save(instance, *args, **kwargs):
            if instance.pk == job.pk:
                foreground_failed[0] = True
                elapsed[0] += 5.0
                raise DatabaseFenceBusy()
            return original_save(instance, *args, **kwargs)

        def busy_heartbeat_read(queryset, *args, **kwargs):
            if foreground_failed[0] and queryset.model is ExportWorkerLease:
                elapsed[0] += 5.0
                raise OperationalError("database is locked")
            return original_get(queryset, *args, **kwargs)

        with mock.patch.object(ExportJob, "save", new=busy_job_save):
            with mock.patch.object(QuerySet, "get", new=busy_heartbeat_read):
                with self.assertRaises(DatabaseFenceBusy):
                    fail_job(
                        lease,
                        "archive_failed",
                        heartbeat,
                        self.now,
                        monotonic=lambda: elapsed[0],
                        sleeper=lambda seconds: None,
                    )

        job.refresh_from_db()
        self.assertEqual(elapsed[0], 15.0)
        self.assertEqual(job.state, "snapshotting")
        self.assertEqual(job.lease_uuid, lease.job_lease_uuid)
        self.assertEqual(heartbeat.current_job_token, lease)

    def test_stop_is_observed_after_one_busy_retry_heartbeat_attempt(self):
        owner = create_export_user("worker-stop-during-retry-renew")
        job = self._job(owner, self.now)
        elapsed = [0.0]
        foreground_failed = [False]
        stopped = threading.Event()
        worker = ExportWorker(
            clock=lambda: self.now,
            monotonic=lambda: elapsed[0],
            sleeper=lambda seconds: None,
        )
        worker.worker_token = worker.acquire_worker_lease(self.now)
        worker.heartbeat = LeaseHeartbeat(
            worker.worker_token,
            clock=lambda: self.now,
            monotonic=lambda: elapsed[0],
            sleeper=lambda seconds: None,
        )
        original_save = ExportJob.save
        original_get = QuerySet.get

        def busy_claim_save(instance, *args, **kwargs):
            if instance.pk == job.pk:
                foreground_failed[0] = True
                elapsed[0] += 5.0
                raise DatabaseFenceBusy()
            return original_save(instance, *args, **kwargs)

        def stop_during_busy_heartbeat(queryset, *args, **kwargs):
            if foreground_failed[0] and queryset.model is ExportWorkerLease:
                elapsed[0] += 5.0
                stopped.set()
                raise OperationalError("database is locked")
            return original_get(queryset, *args, **kwargs)

        with mock.patch.object(ExportJob, "save", new=busy_claim_save):
            with mock.patch.object(
                    QuerySet,
                    "get",
                    new=stop_during_busy_heartbeat,
            ):
                self.assertFalse(worker.run_once(stopped.is_set))

        job.refresh_from_db()
        self.assertEqual(elapsed[0], 10.0)
        self.assertEqual(job.state, "queued")
        self.assertIsNone(job.lease_uuid)
        self.assertIsNone(worker.heartbeat.current_job_token)

    def test_run_once_stop_handoff_read_busy_does_not_escape(self):
        owner = create_export_user("worker-run-once-stop-read-busy")
        job = self._job(owner, self.now)
        stop_raised = [False]
        busy = [False]

        class StoppingSnapshot(object):
            def capture(inner_self, current, lease, heartbeat, stop_requested):
                del inner_self, current, heartbeat, stop_requested
                stop_raised[0] = True
                raise StopRequested(lease)

        worker = ExportWorker(
            clock=lambda: self.now,
            sleeper=lambda seconds: None,
            snapshot_service=StoppingSnapshot(),
        )
        worker.worker_token = worker.acquire_worker_lease(self.now)
        worker.heartbeat = LeaseHeartbeat(
            worker.worker_token,
            clock=lambda: self.now,
        )
        original_get = QuerySet.get

        def busy_first_handoff_read(queryset, *args, **kwargs):
            if (
                stop_raised[0]
                and queryset.model is ExportJob
                and kwargs.get("pk") == job.pk
                and not busy[0]
            ):
                busy[0] = True
                raise OperationalError("database is locked")
            return original_get(queryset, *args, **kwargs)

        with mock.patch.object(QuerySet, "get", new=busy_first_handoff_read):
            self.assertTrue(worker.run_once(lambda: False))

        job.refresh_from_db()
        self.assertTrue(busy[0])
        self.assertEqual(job.state, "queued")
        self.assertIsNone(job.lease_uuid)
        self.assertIsNone(worker.heartbeat.current_job_token)

    def test_run_once_export_error_read_busy_does_not_escape(self):
        owner = create_export_user("worker-run-once-failure-read-busy")
        job = self._job(owner, self.now)
        error_raised = [False]
        busy = [False]

        class FailingSnapshot(object):
            def capture(inner_self, current, lease, heartbeat, stop_requested):
                del inner_self, current, heartbeat, stop_requested
                error_raised[0] = True
                raise ExportError("archive_failed", lease)

        worker = ExportWorker(
            clock=lambda: self.now,
            sleeper=lambda seconds: None,
            snapshot_service=FailingSnapshot(),
        )
        worker.worker_token = worker.acquire_worker_lease(self.now)
        worker.heartbeat = LeaseHeartbeat(
            worker.worker_token,
            clock=lambda: self.now,
        )
        original_get = QuerySet.get

        def busy_first_failure_read(queryset, *args, **kwargs):
            if (
                error_raised[0]
                and queryset.model is ExportJob
                and kwargs.get("pk") == job.pk
                and not busy[0]
            ):
                busy[0] = True
                raise OperationalError("database is locked")
            return original_get(queryset, *args, **kwargs)

        with mock.patch.object(QuerySet, "get", new=busy_first_failure_read):
            self.assertTrue(worker.run_once(lambda: False))

        job.refresh_from_db()
        self.assertTrue(busy[0])
        self.assertEqual(job.state, "failed")
        self.assertEqual(job.error_code, "archive_failed")
        self.assertIsNone(job.lease_uuid)
        self.assertIsNone(worker.heartbeat.current_job_token)

    def test_terminal_stop_with_cleared_local_token_releases_db_lease(self):
        job, worker = self._terminal_interrupt_fixture("stop")

        self.assertTrue(worker.run_once(lambda: False))

        job.refresh_from_db()
        self.assertEqual(job.state, "complete")
        self.assertIsNone(job.lease_uuid)
        self.assertIsNone(worker.heartbeat.current_job_token)

    def test_terminal_error_with_cleared_local_token_releases_db_lease(self):
        job, worker = self._terminal_interrupt_fixture("error")

        self.assertTrue(worker.run_once(lambda: False))

        job.refresh_from_db()
        self.assertEqual(job.state, "complete")
        self.assertIsNone(job.lease_uuid)
        self.assertIsNone(worker.heartbeat.current_job_token)

    def test_retry_heartbeat_at_outer_deadline_rolls_back(self):
        owner = create_export_user("worker-renew-at-outer-deadline")
        job = self._job(owner, self.now)
        renewed_at = self.now + timedelta(seconds=1)
        elapsed = [0.0]
        foreground_failed = [False]
        worker = acquire_worker_lease(self.now)
        heartbeat = LeaseHeartbeat(
            worker,
            clock=lambda: renewed_at,
            monotonic=lambda: elapsed[0],
            sleeper=lambda seconds: None,
        )
        lease = claim_next_job(worker, heartbeat, self.now)
        original_save = ExportJob.save
        original_update = QuerySet.update

        def busy_job_save(instance, *args, **kwargs):
            if instance.pk == job.pk:
                foreground_failed[0] = True
                elapsed[0] = 14.0
                raise DatabaseFenceBusy()
            return original_save(instance, *args, **kwargs)

        def expire_during_renew(queryset, *args, **kwargs):
            result = original_update(queryset, *args, **kwargs)
            if (
                foreground_failed[0]
                and queryset.model is ExportWorkerLease
                and elapsed[0] < 15.0
            ):
                elapsed[0] = 15.0
            return result

        with mock.patch.object(ExportJob, "save", new=busy_job_save):
            with mock.patch.object(QuerySet, "update", new=expire_during_renew):
                with self.assertRaises(DatabaseFenceBusy):
                    fail_job(
                        lease,
                        "archive_failed",
                        heartbeat,
                        self.now,
                        monotonic=lambda: elapsed[0],
                        sleeper=lambda seconds: None,
                    )

        worker_row = ExportWorkerLease.objects.get(pk=1)
        job.refresh_from_db()
        self.assertEqual(elapsed[0], 15.0)
        self.assertEqual(worker_row.heartbeat_at, self.now)
        self.assertEqual(job.heartbeat_at, self.now)
        self.assertEqual(job.state, "snapshotting")
        self.assertEqual(job.lease_uuid, lease.job_lease_uuid)
        self.assertEqual(heartbeat.current_job_token, lease)

    def test_handoff_totals_cannot_overrun_outer_deadline(self):
        owner = create_export_user("worker-handoff-totals-deadline")
        job = self._job(owner, self.now)
        elapsed = [0.0]
        worker = acquire_worker_lease(self.now)
        heartbeat = LeaseHeartbeat(
            worker,
            clock=lambda: self.now,
            monotonic=lambda: elapsed[0],
            sleeper=lambda seconds: None,
        )
        lease = claim_next_job(worker, heartbeat, self.now)
        job.attempts.create(
            attempt_generation=lease.attempt_generation,
            lease_uuid=lease.job_lease_uuid,
            state="cleaned",
            relative_path="attempt-{}-deadline".format(job.pk),
            dir_dev=1,
            dir_ino=2,
            dir_uid=3,
            dir_gid=4,
            dir_mode=0o700,
        )

        def totals_at_deadline(*args, **kwargs):
            del args, kwargs
            elapsed[0] = 15.0
            return 0, 0

        from exports.services import worker as worker_services
        with mock.patch.object(
                worker_services,
                "_included_totals",
                side_effect=totals_at_deadline,
        ):
            with self.assertRaises(DatabaseFenceBusy):
                handoff_after_normal_stop(
                    lease,
                    heartbeat,
                    self.now,
                    monotonic=lambda: elapsed[0],
                    sleeper=lambda seconds: None,
                )

        job.refresh_from_db()
        self.assertEqual(elapsed[0], 15.0)
        self.assertEqual(job.state, "snapshotting")
        self.assertEqual(job.attempt_generation, lease.attempt_generation)
        self.assertEqual(job.lease_uuid, lease.job_lease_uuid)
        self.assertEqual(heartbeat.current_job_token, lease)

    def test_external_sqlite_writer_releases_before_heartbeat_and_retry(self):
        owner = create_export_user("worker-external-writer")
        job = self._job(owner, self.now)
        worker = acquire_worker_lease(self.now)
        heartbeat = LeaseHeartbeat(worker, clock=lambda: self.now)
        external = sqlite3.connect(
            connection.settings_dict["NAME"],
            timeout=0,
        )
        external.execute("BEGIN IMMEDIATE")
        original_renew = heartbeat.renew_worker_once
        released = []
        sleeps = []

        def release_then_renew(now=None, operation_deadline=None):
            if not released:
                external.commit()
                released.append(True)
            return original_renew(
                now,
                operation_deadline=operation_deadline,
            )

        heartbeat.renew_worker_once = release_then_renew
        try:
            lease = claim_next_job(
                worker,
                heartbeat,
                self.now,
                sleeper=lambda seconds: sleeps.append(seconds),
            )
        finally:
            external.close()

        job.refresh_from_db()
        self.assertEqual(released, [True])
        self.assertEqual(sleeps, [0.01])
        self.assertEqual(job.state, "snapshotting")
        self.assertEqual(job.lease_uuid, lease.job_lease_uuid)
        self.assertEqual(heartbeat.current_job_token, lease)

    def test_stale_worker_does_not_release_new_global_lease(self):
        stale = acquire_worker_lease(self.now)
        current = acquire_worker_lease(self.now + timedelta(seconds=1))

        with self.assertRaises(LeaseLost):
            release_worker_lease(stale, now=self.now + timedelta(seconds=2))

        row = ExportWorkerLease.objects.get(pk=1)
        self.assertEqual(row.generation, current.worker_generation)
        self.assertEqual(row.lease_uuid, current.worker_lease_uuid)


class WorkerLockTests(ExportStorageMixin, TransactionTestCase):
    def _budget_exhaustion_fixture(self, suffix):
        now = timezone.now()
        owner = create_export_user("worker-budget-{}".format(suffix))
        job = ExportJob.objects.create(
            owner=owner,
            scope="pins",
            state="queued",
            requested_total=1,
            target_total=1,
            included_total=1,
        )
        elapsed = [0.0]
        foreground_failed = [False]
        original_save = ExportJob.save
        original_get = QuerySet.get

        class FailingSnapshot(object):
            def capture(inner_self, current, lease, heartbeat, stop_requested):
                del inner_self, current, heartbeat, stop_requested
                raise ExportError("archive_failed", lease)

        def busy_terminal_save(instance, *args, **kwargs):
            if instance.pk == job.pk and instance.state == "failed":
                foreground_failed[0] = True
                elapsed[0] += 5.0
                raise DatabaseFenceBusy()
            return original_save(instance, *args, **kwargs)

        def busy_after_terminal_failure(queryset, *args, **kwargs):
            if foreground_failed[0] and queryset.model is ExportWorkerLease:
                elapsed[0] += 5.0
                raise OperationalError("database is locked")
            return original_get(queryset, *args, **kwargs)

        worker = ExportWorker(
            clock=lambda: now,
            monotonic=lambda: elapsed[0],
            sleeper=lambda seconds: None,
            snapshot_service=FailingSnapshot(),
        )
        return (
            worker,
            job,
            elapsed,
            mock.patch.object(ExportJob, "save", new=busy_terminal_save),
            mock.patch.object(
                QuerySet,
                "get",
                new=busy_after_terminal_failure,
            ),
        )

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

    def test_fatal_heartbeat_stops_main_loop_before_claim(self):
        class FatalHeartbeat(LeaseHeartbeat):
            def start(inner_self):
                inner_self._fatal = DatabaseError("fatal heartbeat")

        worker = ExportWorker(
            heartbeat_factory=FatalHeartbeat,
            sleeper=lambda seconds: None,
        )
        worker.run_once = mock.Mock(return_value=False)

        status = worker.run(lambda: False)

        self.assertEqual(status, 1)
        worker.run_once.assert_not_called()

    def test_database_retry_budget_exhaustion_is_explicit_worker_failure(self):
        worker, job, elapsed, save_patch, get_patch = (
            self._budget_exhaustion_fixture("run")
        )

        with save_patch:
            with get_patch:
                status = worker.run(lambda: False)

        job.refresh_from_db()
        self.assertEqual(status, 1)
        self.assertEqual(elapsed[0], 15.0)
        self.assertEqual(job.state, "snapshotting")
        self.assertIsNotNone(job.lease_uuid)
        self.assertEqual(
            worker.heartbeat.current_job_token.job_lease_uuid,
            job.lease_uuid,
        )

    def test_management_command_normalizes_worker_busy_failure(self):
        worker, job, elapsed, save_patch, get_patch = (
            self._budget_exhaustion_fixture("command")
        )

        with save_patch:
            with get_patch:
                with mock.patch(
                    "exports.management.commands.run_export_worker.ExportWorker",
                    return_value=worker,
                ):
                    with self.assertRaisesRegex(
                        CommandError,
                        "안전하게 시작되지 못했습니다",
                    ):
                        call_command("run_export_worker")

        job.refresh_from_db()
        self.assertEqual(elapsed[0], 15.0)
        self.assertEqual(job.state, "snapshotting")
        self.assertIsNotNone(job.lease_uuid)
        self.assertEqual(
            worker.heartbeat.current_job_token.job_lease_uuid,
            job.lease_uuid,
        )

    def test_fatal_after_retry_heartbeat_prevents_second_transaction(self):
        now = timezone.now()
        owner = create_export_user("worker-fatal-after-renew")
        job = ExportJob.objects.create(
            owner=owner,
            scope="pins",
            state="queued",
            requested_total=1,
            target_total=1,
            included_total=1,
        )
        elapsed = [0.0]
        save_count = [0]
        original_save = ExportJob.save

        class FatalAfterRenewHeartbeat(LeaseHeartbeat):
            def renew_worker_once(
                inner_self,
                now=None,
                operation_deadline=None,
            ):
                super(FatalAfterRenewHeartbeat, inner_self).renew_worker_once(
                    now,
                    operation_deadline=operation_deadline,
                )
                inner_self._fatal = DatabaseError("fatal after retry renew")

        def slow_save(instance, *args, **kwargs):
            result = original_save(instance, *args, **kwargs)
            if instance.pk == job.pk:
                save_count[0] += 1
                elapsed[0] = 6.0
            return result

        worker = ExportWorker(
            clock=lambda: now,
            monotonic=lambda: elapsed[0],
            sleeper=lambda seconds: None,
            heartbeat_factory=FatalAfterRenewHeartbeat,
        )
        with mock.patch.object(ExportJob, "save", new=slow_save):
            status = worker.run(lambda: False)

        job.refresh_from_db()
        self.assertEqual(status, 1)
        self.assertEqual(save_count[0], 1)
        self.assertEqual(job.state, "queued")
        self.assertIsNone(job.lease_uuid)

    def test_old_worker_storage_failure_cannot_overwrite_takeover(self):
        now = timezone.now()
        worker = ExportWorker(clock=lambda: now)
        worker.worker_token = acquire_worker_lease(now)

        def takeover_then_fail(*args, **kwargs):
            del args, kwargs
            current = acquire_worker_lease(now + timedelta(seconds=1))
            worker.current_takeover = current
            raise ExportStorageError("export_storage_unsafe")

        with mock.patch(
            "exports.services.worker.open_export_root",
            side_effect=takeover_then_fail,
        ):
            status = worker.run(lambda: False)

        row = ExportWorkerLease.objects.get(pk=1)
        self.assertEqual(status, 1)
        self.assertEqual(row.generation, worker.current_takeover.worker_generation)
        self.assertEqual(row.lease_uuid, worker.current_takeover.worker_lease_uuid)
        self.assertEqual(row.health_state, "ready")
        self.assertIsNone(row.error_code)


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

    def _worker(self, archive_service=None, snapshot_service=None):
        worker = ExportWorker(
            clock=lambda: self.now,
            archive_service=archive_service,
            snapshot_service=snapshot_service,
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
        observed = []
        from exports.services import worker as worker_services
        original_clear = worker_services._TokenTransition.clear

        def checked_clear(transition):
            job.refresh_from_db()
            observed.append((connection.in_atomic_block, job.state))
            return original_clear(transition)

        with mock.patch("exports.services.file_ops._normalize_metadata"):
            with mock.patch.object(
                    worker_services._TokenTransition,
                    "clear",
                    new=checked_clear,
            ):
                self.assertTrue(worker.run_once(lambda: False))

        job.refresh_from_db()
        self.assertEqual(job.state, "complete")
        self.assertEqual(job.staging_cleanup_state, "cleaned")
        self.assertIsNone(job.lease_uuid)
        self.assertIsNone(worker.heartbeat.current_job_token)
        self.assertIn((False, "complete"), observed)
        self.assertTrue(Path(
            settings.PINRY_EXPORT_ROOT,
            job.ready_relative_path,
        ).exists())

    def _assert_candidate_intent_crash_recovers(self, crash_point):
        job = self._queued_job("worker-candidate-intent.png")
        busy = [False]

        def crash(point, context):
            del context
            if point == "before_blob_open_receipt" and not busy[0]:
                busy[0] = True
                raise DatabaseFenceBusy()
            if point == crash_point:
                raise RuntimeError("candidate intent crash")

        first = self._worker(snapshot_service=SnapshotService(
            fault_injector=crash,
            sleeper=lambda seconds: None,
        ))
        with mock.patch("exports.services.file_ops._normalize_metadata"):
            with self.assertRaisesRegex(
                RuntimeError,
                "candidate intent crash",
            ):
                first.run_once(lambda: False)

        job.refresh_from_db()
        self.assertEqual(job.state, "snapshotting")
        self.assertIsNotNone(job.candidate_snapshot_generation)
        self.assertIsNotNone(job.candidate_snapshot_relative_path)
        second = self._worker()
        with mock.patch("exports.services.file_ops._normalize_metadata"):
            self.assertTrue(second.run_once(lambda: False))

        job.refresh_from_db()
        self.assertEqual(job.state, "complete")
        self.assertIsNone(job.candidate_snapshot_generation)
        self.assertIsNone(job.lease_uuid)
        self.assertIsNone(second.heartbeat.current_job_token)

    def test_candidate_intent_precommit_crash_recovers_through_worker(self):
        self._assert_candidate_intent_crash_recovers(
            "before_candidate_cleanup_intent_cas",
        )

    def test_candidate_intent_postcommit_crash_recovers_through_worker(self):
        self._assert_candidate_intent_crash_recovers(
            "after_candidate_cleanup_intent",
        )

    def _revoking_archive_service(self, job, crash_point=None):
        other = create_export_user(
            "worker-revocation-{}".format(job.pk),
        )
        pin_id = ExportTarget.objects.get(job=job).pin_id
        revoked = [False]

        def fault(point, context):
            del context
            if point == "before_final_permission_check" and not revoked[0]:
                Pin.objects.filter(pk=pin_id).update(
                    submitter=other,
                    private=True,
                )
                revoked[0] = True
            if point == crash_point:
                raise RuntimeError("revocation commit crash")

        return ArchiveService(fault_injector=fault)

    def test_final_revocation_commit_crash_recovers_through_worker(self):
        job = self._queued_job("worker-revoke-commit.png")
        first = self._worker(archive_service=self._revoking_archive_service(
            job,
            crash_point="after_post_build_revocation_batches",
        ))
        with mock.patch("exports.services.file_ops._normalize_metadata"):
            with self.assertRaisesRegex(
                RuntimeError,
                "revocation commit crash",
            ):
                first.run_once(lambda: False)

        job.refresh_from_db()
        old_attempt = job.attempts.get(attempt_generation=0)
        old_file = old_attempt.files.get(kind="archive")
        self.assertEqual(job.state, "verifying")
        self.assertEqual(job.attempt_generation, 0)
        self.assertEqual(old_attempt.state, "retiring")
        self.assertEqual(old_file.state, "retiring")
        second = self._worker()
        with mock.patch("exports.services.file_ops._normalize_metadata"):
            self.assertTrue(second.run_once(lambda: False))

        job.refresh_from_db()
        self.assertEqual(job.state, "failed")
        self.assertEqual(job.error_code, "all_items_revoked")
        self.assertIsNone(job.lease_uuid)
        self.assertIsNone(second.heartbeat.current_job_token)

    def test_token_rotation_commit_crash_recovers_same_generation_once(self):
        job = self._queued_job("worker-rotation-commit.png")
        first = self._worker(archive_service=self._revoking_archive_service(job))
        from exports.services import worker as worker_services
        original_replace = worker_services._TokenTransition.replace
        observed = []

        def crash_before_replace(transition, lease):
            if lease.job_id == job.pk and lease.attempt_generation == 1:
                job.refresh_from_db()
                observed.append((
                    connection.in_atomic_block,
                    job.attempt_generation,
                    first.heartbeat.current_job_token,
                ))
                raise RuntimeError("rotation commit crash")
            return original_replace(transition, lease)

        with mock.patch("exports.services.file_ops._normalize_metadata"):
            with mock.patch.object(
                    worker_services._TokenTransition,
                    "replace",
                    new=crash_before_replace,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "rotation commit crash",
                ):
                    first.run_once(lambda: False)

        job.refresh_from_db()
        self.assertEqual(job.state, "archiving")
        self.assertEqual(job.attempt_generation, 1)
        self.assertEqual(observed[0][:2], (False, 1))
        self.assertEqual(observed[0][2].attempt_generation, 0)
        second = self._worker()
        with mock.patch("exports.services.file_ops._normalize_metadata"):
            self.assertTrue(second.run_once(lambda: False))

        job.refresh_from_db()
        self.assertEqual(job.state, "failed")
        self.assertEqual(job.error_code, "all_items_revoked")
        self.assertEqual(job.attempt_generation, 1)
        self.assertIsNone(job.lease_uuid)
        self.assertIsNone(second.heartbeat.current_job_token)

    def test_owner_deleted_immediately_before_final_never_completes(self):
        job = self._queued_job("worker-owner-before-final.png")
        worker = self._worker()
        original_final = ArchiveService._final_fence
        deleted = [False]

        def delete_before_final(service, *args, **kwargs):
            if not deleted[0]:
                self.owner.delete()
                deleted[0] = True
            return original_final(service, *args, **kwargs)

        with mock.patch("exports.services.file_ops._normalize_metadata"):
            with mock.patch.object(
                    ArchiveService,
                    "_final_fence",
                    new=delete_before_final,
            ):
                self.assertTrue(worker.run_once(lambda: False))

        job.refresh_from_db()
        self.assertTrue(deleted[0])
        self.assertEqual(job.state, "failed")
        self.assertEqual(job.error_code, "permission_changed")
        self.assertIsNone(job.owner_id)
        self.assertIsNone(job.lease_uuid)
        self.assertIsNone(worker.heartbeat.current_job_token)

    def test_owner_deleted_immediately_after_complete_is_retention_cleaned(self):
        job = self._queued_job("worker-owner-after-final.png")

        def delete_after_complete(point, context):
            del context
            if point == "after_complete_commit":
                self.owner.delete()
                raise RuntimeError("post-complete owner delete")

        first = self._worker(archive_service=ArchiveService(
            fault_injector=delete_after_complete,
        ))
        with mock.patch("exports.services.file_ops._normalize_metadata"):
            with self.assertRaisesRegex(
                RuntimeError,
                "post-complete owner delete",
            ):
                first.run_once(lambda: False)

        job.refresh_from_db()
        ready_path = Path(
            settings.PINRY_EXPORT_ROOT,
            job.ready_relative_path,
        )
        self.assertEqual(job.state, "complete")
        self.assertIsNone(job.owner_id)
        self.assertTrue(ready_path.exists())
        second = self._worker()
        for unused_index in range(5):
            if not ExportJob.objects.filter(pk=job.pk).exists():
                break
            self.assertTrue(second.run_once(lambda: False))

        self.assertFalse(ExportJob.objects.filter(pk=job.pk).exists())
        self.assertFalse(ready_path.exists())
        self.assertIsNone(second.heartbeat.current_job_token)

    def test_run_once_retries_ownerless_maintenance_database_busy(self):
        job = self._queued_job("worker-ownerless-busy.png")
        self.owner.delete()
        worker = self._worker()
        original_save = ExportJob.save
        save_count = [0]
        sleeps = []

        def busy_once(instance, *args, **kwargs):
            if instance.pk == job.pk and save_count[0] == 0:
                save_count[0] += 1
                raise DatabaseFenceBusy()
            save_count[0] += 1
            return original_save(instance, *args, **kwargs)

        worker.sleeper = lambda seconds: sleeps.append(seconds)
        with mock.patch.object(ExportJob, "save", new=busy_once):
            self.assertTrue(worker.run_once(lambda: False))

        job.refresh_from_db()
        self.assertEqual(save_count[0], 2)
        self.assertEqual(sleeps, [0.01])
        self.assertEqual(job.state, "failed")
        self.assertEqual(job.error_code, "permission_changed")
        self.assertIsNone(worker.heartbeat.current_job_token)

    def test_101_orphans_are_drained_before_fifo_job_is_claimed(self):
        ExportJob.objects.bulk_create([
            ExportJob(
                owner=None,
                scope="pins",
                state="failed",
                error_code="archive_failed",
                error_class="retryable",
                error_retryable=True,
                staging_cleanup_state="cleaned",
                ready_cleanup_state="absent",
            )
            for unused_index in range(101)
        ])
        job = self._queued_job("worker-after-orphans.png")
        worker = self._worker()

        self.assertTrue(worker.run_once(lambda: False))

        job.refresh_from_db()
        self.assertEqual(job.state, "queued")
        self.assertEqual(
            ExportJob.objects.filter(owner_id__isnull=True).count(),
            100,
        )
        for unused_index in range(100):
            self.assertTrue(worker.run_once(lambda: False))
        job.refresh_from_db()
        self.assertEqual(job.state, "queued")
        self.assertFalse(ExportJob.objects.filter(owner_id__isnull=True).exists())

        with mock.patch("exports.services.file_ops._normalize_metadata"):
            self.assertTrue(worker.run_once(lambda: False))

        job.refresh_from_db()
        self.assertEqual(job.state, "complete")

    def test_pre_attempt_directory_handoff_recovers_without_generation_bump(self):
        job = self._queued_job("worker-pre-attempt.png")
        first = self._worker()
        lease = first.claim_next_job(
            first.worker_token,
            first.heartbeat,
            self.now,
        )
        with mock.patch("exports.services.file_ops._normalize_metadata"):
            first.snapshot_service.capture(
                job,
                lease,
                first.heartbeat,
                lambda: False,
            )
        job.refresh_from_db()
        self.assertEqual(job.state, "archiving")
        root = open_export_root(
            settings.PINRY_EXPORT_ROOT,
            os.getuid(),
            os.getgid(),
        )
        staging = open_staging_directory(root)
        directory = AttemptService().create_directory_fs(
            staging,
            job,
            lease,
        )
        directory.close()
        staging.close()
        root.close()
        handoff_after_normal_stop(lease, first.heartbeat, self.now)

        second = self._worker()
        with mock.patch("exports.services.file_ops._normalize_metadata"):
            self.assertTrue(second.run_once(lambda: False))

        job.refresh_from_db()
        self.assertEqual(job.state, "complete")
        self.assertEqual(job.attempt_generation, 0)
        self.assertEqual(job.attempts.count(), 1)

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
