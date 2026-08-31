from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
import os
import stat
import threading
import time
import uuid

from django.conf import settings
from django.db import DatabaseError, IntegrityError, close_old_connections
from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from core.services.database_fence import (
    DatabaseFenceBusy,
    DatabaseFenceDeadline,
    DatabaseFenceError,
    database_write_fence,
)
from exports.contracts import (
    ACTIVE_STATES,
    ERROR_CONTRACTS,
    TERMINAL_STATES,
    ExportError,
    LeaseLost,
    LeaseToken,
    StopRequested,
    WorkerLeaseToken,
    lock_current_lease,
    lock_current_worker_lease,
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
from exports.services.archive import ArchiveService
from exports.services.file_ops import (
    ClosedFileReceipt,
    DirectoryReceipt,
    ExportStorageError,
    ExportWorkerLock,
    OpenFileReceipt,
    create_private_directory,
    open_export_root,
    open_receipted_directory,
    remove_if_receipt_matches,
)
from exports.services.snapshot import (
    SnapshotService,
    open_staging_directory,
    snapshot_blob_name,
    snapshot_directory_name,
)


WORKER_LEASE_SECONDS = 30
HEARTBEAT_INTERVAL_SECONDS = 5
DATABASE_FENCE_MAX_SECONDS = 5
DATABASE_RETRY_MAX_SECONDS = 15
WORKER_HEALTH_STALE_SECONDS = 15
IDLE_WAIT_SECONDS = 1
MAX_ABNORMAL_RESUMES = 3
MAINTENANCE_BATCH_SIZE = 100
QUERY_BATCH_SIZE = 400

WORKER_FENCE_MODELS = (
    ExportWorkerLease,
    ExportJob,
    ExportSlot,
    ExportAttempt,
    ExportAttemptFile,
    ExportBlob,
    ExportItem,
)


class _CleanupBlocked(ExportStorageError):
    def __init__(self):
        super(_CleanupBlocked, self).__init__("export_storage_unsafe")


class _HeartbeatFailed(Exception):
    pass


class _DatabaseRetryStopped(Exception):
    pass


@contextmanager
def _worker_write_fence(using, monotonic=None):
    clock = time.monotonic if monotonic is None else monotonic
    with database_write_fence(using=using, models=WORKER_FENCE_MODELS):
        deadline = DatabaseFenceDeadline(
            clock,
            budget_seconds=DATABASE_FENCE_MAX_SECONDS,
        )
        yield deadline
        deadline.checkpoint()


@contextmanager
def _passthrough_guard():
    yield


@dataclass(frozen=True)
class RecoveryClaimOutcome(object):
    lease: object
    terminalized: bool


@dataclass(frozen=True)
class RequeueOutcome(object):
    requeued_count: int
    terminalized: bool


@dataclass(frozen=True)
class MaintenanceOutcome(object):
    did_work: bool
    claim_allowed: bool


def _database_error_is_busy(error):
    return isinstance(error, DatabaseError) and any(
        word in str(error).lower() for word in ("busy", "locked")
    )


def _retry_database(
    operation,
    monotonic=None,
    sleeper=None,
    retry_checkpoint=None,
):
    monotonic = time.monotonic if monotonic is None else monotonic
    sleeper = time.sleep if sleeper is None else sleeper
    deadline = monotonic() + DATABASE_RETRY_MAX_SECONDS
    while True:
        try:
            return operation()
        except DatabaseFenceBusy:
            pass
        except DatabaseError as error:
            if not _database_error_is_busy(error):
                raise
        if retry_checkpoint is not None:
            retry_checkpoint()
        if monotonic() >= deadline:
            raise DatabaseFenceBusy()
        sleeper(0.01)


def _retry_control(heartbeat, stop_requested=None, lease=None):
    def checkpoint():
        if heartbeat.lost is not None:
            raise heartbeat.lost
        if heartbeat.fatal is not None:
            raise _HeartbeatFailed()
        if stop_requested is not None and stop_requested():
            raise _DatabaseRetryStopped()
        if lease is None:
            heartbeat.renew_worker_now()
        else:
            heartbeat.renew_now(lease)
        if heartbeat.lost is not None:
            raise heartbeat.lost
        if heartbeat.fatal is not None:
            raise _HeartbeatFailed()
        if stop_requested is not None and stop_requested():
            raise _DatabaseRetryStopped()

    return checkpoint


def _worker_expiry(now):
    return now + timedelta(seconds=WORKER_LEASE_SECONDS)


def acquire_worker_lease(now, using="default", monotonic=None, sleeper=None):
    """호출자가 OS flock을 확보한 뒤 새 DB 작업자 세대를 확보한다."""

    new_uuid = uuid.uuid4()

    def acquire():
        with _worker_write_fence(using, monotonic):
            try:
                current = ExportWorkerLease.objects.using(
                    using,
                ).select_for_update().get(pk=1)
            except ExportWorkerLease.DoesNotExist:
                try:
                    ExportWorkerLease.objects.using(using).create(pk=1)
                except IntegrityError:
                    raise DatabaseFenceBusy() from None
                current = ExportWorkerLease.objects.using(
                    using,
                ).select_for_update().get(pk=1)
            generation = current.generation + 1
            updated = ExportWorkerLease.objects.using(using).filter(
                pk=1,
                generation=current.generation,
            ).update(
                generation=generation,
                lease_uuid=new_uuid,
                lease_expires_at=_worker_expiry(now),
                heartbeat_at=now,
                health_state="ready",
                error_code=None,
            )
            if updated != 1:
                raise DatabaseFenceBusy()
        return WorkerLeaseToken(generation, new_uuid)

    return _retry_database(acquire, monotonic=monotonic, sleeper=sleeper)


class _TokenTransition(object):
    def __init__(self, heartbeat, expected):
        self.heartbeat = heartbeat
        self.expected = expected
        self.changed = False

    def _change(self, token, operation):
        if self.changed:
            raise RuntimeError("job_token_transition_already_applied")
        if self.heartbeat._job_token != self.expected:
            raise LeaseLost()
        operation(token)
        self.changed = True

    def publish(self, token):
        if self.expected is not None or token is None:
            raise LeaseLost()
        self._change(token, lambda value: setattr(
            self.heartbeat, "_job_token", value,
        ))

    def replace(self, token):
        if self.expected is None or token is None:
            raise LeaseLost()
        self._change(token, lambda value: setattr(
            self.heartbeat, "_job_token", value,
        ))

    def clear(self):
        self._change(None, lambda value: setattr(
            self.heartbeat, "_job_token", value,
        ))


class LeaseHeartbeat(object):
    """프로세스 로컬 토큰과 heartbeat DB 쓰기 직렬화를 소유한다."""

    def __init__(
        self,
        worker_token,
        using="default",
        clock=None,
        monotonic=None,
        sleeper=None,
        interval=HEARTBEAT_INTERVAL_SECONDS,
    ):
        self.worker_token = worker_token
        self.using = using
        self.clock = timezone.now if clock is None else clock
        self.monotonic = time.monotonic if monotonic is None else monotonic
        self.sleeper = time.sleep if sleeper is None else sleeper
        self.interval = interval
        self._mutex = threading.RLock()
        self._job_token = None
        self._stop_event = threading.Event()
        self._thread = None
        self._lost = None
        self._fatal = None
        self._accept_ticks = True

    @property
    def current_job_token(self):
        with self._mutex:
            return self._job_token

    @property
    def lost(self):
        return self._lost

    @property
    def fatal(self):
        return self._fatal

    @contextmanager
    def foreground_write_guard(self):
        with self._mutex:
            yield

    @contextmanager
    def job_token_transition(self, expected_token):
        with self._mutex:
            if self._job_token != expected_token:
                raise LeaseLost()
            transition = _TokenTransition(self, expected_token)
            yield transition

    def _renew(self, token, now):
        def renew():
            with transaction.atomic(using=self.using):
                lock_current_worker_lease(
                    self.worker_token,
                    using=self.using,
                )
                updated_worker = ExportWorkerLease.objects.using(
                    self.using,
                ).filter(
                    pk=1,
                    generation=self.worker_token.worker_generation,
                    lease_uuid=self.worker_token.worker_lease_uuid,
                ).update(
                    lease_expires_at=_worker_expiry(now),
                    heartbeat_at=now,
                    health_state="ready",
                    error_code=None,
                )
                if updated_worker != 1:
                    raise LeaseLost()
                if token is not None:
                    updated_job = ExportJob.objects.using(self.using).filter(
                        **token.job_fence()
                    ).update(
                        lease_expires_at=_worker_expiry(now),
                        heartbeat_at=now,
                    )
                    if updated_job != 1:
                        raise LeaseLost()

        return _retry_database(
            renew,
            monotonic=self.monotonic,
            sleeper=self.sleeper,
        )

    def tick(self, now=None):
        with self._mutex:
            if not self._accept_ticks:
                return False
            current = self.clock() if now is None else now
            self._renew(self._job_token, current)
            return True

    def renew_now(self, token, now=None):
        with self._mutex:
            if self._job_token is not None and self._job_token != token:
                raise LeaseLost()
            current = self.clock() if now is None else now
            self._renew(token, current)
            return token

    def renew_worker_now(self, now=None):
        with self._mutex:
            current = self.clock() if now is None else now
            self._renew(None, current)

    def __call__(self):
        return self.tick()

    def _run(self):
        close_old_connections()
        try:
            while not self._stop_event.wait(self.interval):
                try:
                    self.tick()
                except LeaseLost as error:
                    self._lost = error
                    self._stop_event.set()
                    return
                except DatabaseFenceBusy:
                    continue
                except DatabaseError as error:
                    self._fatal = error
                    self._stop_event.set()
                    return
        finally:
            close_old_connections()

    def start(self):
        with self._mutex:
            if self._thread is not None:
                raise RuntimeError("heartbeat_already_started")
            self._thread = threading.Thread(
                target=self._run,
                name="pinry-export-heartbeat",
                daemon=True,
            )
            self._thread.start()

    def stop_accepting_ticks(self):
        with self._mutex:
            self._accept_ticks = False
            self._stop_event.set()

    def join(self):
        thread = self._thread
        if thread is not None:
            thread.join()

    def close(self):
        self.stop_accepting_ticks()
        self.join()


def _new_job_token(worker_lease, job):
    return LeaseToken(
        worker_lease.worker_generation,
        worker_lease.worker_lease_uuid,
        job.pk,
        uuid.uuid4(),
        job.attempt_generation,
    )


def _job_has_cleanup_work(job):
    if job.candidate_snapshot_generation is not None:
        return True
    return job.attempts.exclude(state="cleaned").exists()


def claim_next_job(
    worker_lease,
    heartbeat,
    now,
    using="default",
    monotonic=None,
    sleeper=None,
    stop_requested=None,
):
    if heartbeat.current_job_token is not None:
        return None

    def claim():
        with heartbeat.job_token_transition(None) as transition:
            with heartbeat.foreground_write_guard():
                with _worker_write_fence(
                    using,
                    monotonic=monotonic,
                ):
                    lock_current_worker_lease(worker_lease, using=using)
                    current_attempt = ExportAttempt.objects.using(
                        using,
                    ).filter(
                        job_id=OuterRef("pk"),
                        attempt_generation=OuterRef("attempt_generation"),
                    )
                    unclean_attempt = ExportAttempt.objects.using(
                        using,
                    ).filter(
                        job_id=OuterRef("pk"),
                    ).exclude(state="cleaned")
                    current = ExportJob.objects.using(
                        using,
                    ).select_for_update().annotate(
                        has_current_attempt=Exists(current_attempt),
                        has_unclean_attempt=Exists(unclean_attempt),
                    ).filter(
                        state="queued",
                        owner_id__isnull=False,
                        lease_uuid__isnull=True,
                        candidate_snapshot_generation__isnull=True,
                        has_current_attempt=False,
                        has_unclean_attempt=False,
                    ).order_by("created_at", "id").first()
                    if current is None:
                        return None
                    lease = _new_job_token(worker_lease, current)
                    current.state = (
                        "archiving"
                        if current.snapshot_generation is not None
                        else "snapshotting"
                    )
                    current.worker_generation = worker_lease.worker_generation
                    current.lease_uuid = lease.job_lease_uuid
                    current.lease_expires_at = _worker_expiry(now)
                    current.heartbeat_at = now
                    current.progress_at = now
                    if current.started_at is None:
                        current.started_at = now
                    current.save(update_fields=(
                        "state", "worker_generation", "lease_uuid",
                        "lease_expires_at", "heartbeat_at", "progress_at",
                        "started_at",
                    ))
            if lease is not None:
                transition.publish(lease)
            return lease

    return _retry_database(
        claim,
        monotonic=monotonic,
        sleeper=sleeper,
        retry_checkpoint=_retry_control(
            heartbeat,
            stop_requested=stop_requested,
        ),
    )


def _takeover_kind(job, worker_lease):
    if job.lease_uuid is None:
        return "normal"
    if job.worker_generation != worker_lease.worker_generation:
        return "abnormal"
    return None


def _mark_attempts_retiring_locked(job):
    attempts = ExportAttempt.objects.filter(
        job=job,
        state__in=("writing", "closed", "verifying"),
    )
    attempts.update(state="retiring")
    ExportAttemptFile.objects.filter(
        attempt__job=job,
        kind="archive",
        state__in=(
            "writing", "closed", "verifying", "publishing",
            "ready_candidate",
        ),
    ).update(state="retiring")


def _fail_locked(
    job,
    error_code,
    worker_token,
    job_token=None,
    now=None,
    using="default",
):
    """호출자가 연 atomic 블록 안에서 공통 terminal 전이를 적용한다."""

    lock_current_worker_lease(worker_token, using=using)
    current = ExportJob.objects.using(using).select_for_update().get(pk=job.pk)
    if job_token is not None:
        expected = job_token.job_fence()
        if any(getattr(current, field) != value for field, value in (
            ("pk", expected["pk"]),
            ("worker_generation", expected["worker_generation"]),
            ("lease_uuid", expected["lease_uuid"]),
            ("attempt_generation", expected["attempt_generation"]),
        )):
            raise LeaseLost()
    error_class, retryable, unused_message = ERROR_CONTRACTS[error_code]
    del unused_message
    current.state = "failed"
    current.error_code = error_code
    current.error_class = error_class
    current.error_retryable = retryable
    current.staging_cleanup_state = "pending"
    current.lease_uuid = None
    current.lease_expires_at = None
    current.heartbeat_at = now
    current.save(update_fields=(
        "state", "error_code", "error_class", "error_retryable",
        "staging_cleanup_state", "lease_uuid", "lease_expires_at",
        "heartbeat_at",
    ))
    _mark_attempts_retiring_locked(current)
    ExportSlot.objects.using(using).filter(current_job=current).update(
        current_job=None,
    )
    return current


def _claim_recovery(
    worker_lease,
    heartbeat,
    now,
    states,
    require_cleanup,
    using="default",
    monotonic=None,
    sleeper=None,
    stop_requested=None,
):
    def claim():
        with heartbeat.job_token_transition(None) as transition:
            with heartbeat.foreground_write_guard():
                with _worker_write_fence(
                    using,
                    monotonic=monotonic,
                ):
                    lock_current_worker_lease(worker_lease, using=using)
                    candidates = ExportJob.objects.using(
                        using,
                    ).select_for_update().filter(
                        state__in=states,
                    ).filter(
                        Q(lease_uuid__isnull=True)
                        | ~Q(worker_generation=worker_lease.worker_generation)
                    )
                    if require_cleanup:
                        unclean_attempt = ExportAttempt.objects.using(
                            using,
                        ).filter(
                            job_id=OuterRef("pk"),
                        ).exclude(state="cleaned")
                        candidates = candidates.annotate(
                            has_unclean_attempt=Exists(unclean_attempt),
                        ).filter(
                            Q(candidate_snapshot_generation__isnull=False)
                            | Q(has_unclean_attempt=True)
                        )
                    current = candidates.order_by(
                        "state", "created_at", "id",
                    ).first()
                    if current is None:
                        return RecoveryClaimOutcome(None, False)
                    takeover = _takeover_kind(current, worker_lease)
                    if current.owner_id is None:
                        _fail_locked(
                            current,
                            "permission_changed",
                            worker_lease,
                            now=now,
                            using=using,
                        )
                        return RecoveryClaimOutcome(None, True)
                    resume_count = current.resume_count
                    if takeover == "abnormal":
                        resume_count += 1
                        if resume_count > MAX_ABNORMAL_RESUMES:
                            _fail_locked(
                                current,
                                "worker_repeated_failure",
                                worker_lease,
                                now=now,
                                using=using,
                            )
                            return RecoveryClaimOutcome(None, True)
                    lease = _new_job_token(worker_lease, current)
                    current.resume_count = resume_count
                    current.worker_generation = worker_lease.worker_generation
                    current.lease_uuid = lease.job_lease_uuid
                    current.lease_expires_at = _worker_expiry(now)
                    current.heartbeat_at = now
                    current.save(update_fields=(
                        "resume_count", "worker_generation", "lease_uuid",
                        "lease_expires_at", "heartbeat_at",
                    ))
                    outcome = RecoveryClaimOutcome(lease, False)
            if outcome.lease is not None:
                transition.publish(outcome.lease)
            return outcome

    return _retry_database(
        claim,
        monotonic=monotonic,
        sleeper=sleeper,
        retry_checkpoint=_retry_control(
            heartbeat,
            stop_requested=stop_requested,
        ),
    )


def claim_retiring_recovery(
    worker_lease,
    heartbeat,
    now,
    using="default",
    monotonic=None,
    sleeper=None,
    stop_requested=None,
):
    return _claim_recovery(
        worker_lease,
        heartbeat,
        now,
        ("snapshotting", "archiving"),
        True,
        using=using,
        monotonic=monotonic,
        sleeper=sleeper,
        stop_requested=stop_requested,
    )


def claim_verifying_recovery(
    worker_lease,
    heartbeat,
    now,
    using="default",
    monotonic=None,
    sleeper=None,
    stop_requested=None,
):
    return _claim_recovery(
        worker_lease,
        heartbeat,
        now,
        ("verifying",),
        False,
        using=using,
        monotonic=monotonic,
        sleeper=sleeper,
        stop_requested=stop_requested,
    )


def _included_totals(job, using, checkpoint):
    items = ExportItem.objects.using(using).filter(
        job=job,
        inclusion_state="included",
    ).order_by("pk")
    total = 0
    size = 0
    last_pk = None
    while True:
        current = items
        if last_pk is not None:
            current = current.filter(pk__gt=last_pk)
        rows = list(current.values_list(
            "pk", "blob__size",
        )[:QUERY_BATCH_SIZE])
        checkpoint()
        if not rows:
            return total, size
        if any(value is None for unused_pk, value in rows):
            raise ExportStorageError("export_storage_unsafe")
        total += len(rows)
        size += sum(value for unused_pk, value in rows)
        last_pk = rows[-1][0]


def _reset_recovered_progress_locked(
    job,
    using,
    checkpoint=lambda: None,
    totals=None,
    has_items=None,
):
    if totals is None:
        totals = _included_totals(job, using, checkpoint)
    archive_total, bytes_total = totals
    if has_items is None:
        has_items = job.items.exists()
    if has_items:
        job.archive_total = archive_total
        job.bytes_total = bytes_total
    job.archive_done = 0
    job.bytes_done = 0
    job.verifying_attempt_generation = None
    job.verifying_size = None
    job.verifying_sha256 = None
    job.verifying_completed_at = None


def requeue_expired_nonverifying_jobs(
    worker_lease,
    now,
    using="default",
    monotonic=None,
    sleeper=None,
    checkpoint=None,
    write_guard=None,
    retry_checkpoint=None,
):
    checkpoint = (lambda: None) if checkpoint is None else checkpoint
    write_guard = _passthrough_guard if write_guard is None else write_guard

    def candidates():
        unclean_attempt = ExportAttempt.objects.using(using).filter(
            job_id=OuterRef("pk"),
        ).exclude(state="cleaned")
        current_tombstone = ExportAttempt.objects.using(using).filter(
            job_id=OuterRef("pk"),
            attempt_generation=OuterRef("attempt_generation"),
            state="cleaned",
        )
        return ExportJob.objects.using(using).annotate(
            has_unclean_attempt=Exists(unclean_attempt),
            has_current_tombstone=Exists(current_tombstone),
        ).filter(
            state__in=("snapshotting", "archiving"),
            candidate_snapshot_generation__isnull=True,
            has_unclean_attempt=False,
        ).filter(
            Q(lease_uuid__isnull=True)
            | ~Q(worker_generation=worker_lease.worker_generation)
        )

    def requeue():
        count = 0
        candidate = candidates().order_by(
            "state", "created_at", "id",
        ).first()
        if candidate is None:
            return RequeueOutcome(count, False)
        expected = (
            candidate.attempt_generation,
            candidate.worker_generation,
            candidate.lease_uuid,
            candidate.has_current_tombstone,
        )
        totals = None
        has_items = None
        if candidate.has_current_tombstone:
            totals = _included_totals(candidate, using, checkpoint)
            has_items = candidate.items.exists()
        with write_guard():
            with _worker_write_fence(
                using,
                monotonic=monotonic,
            ):
                lock_current_worker_lease(worker_lease, using=using)
                current = candidates().select_for_update().filter(
                    pk=candidate.pk,
                ).first()
                if current is None:
                    return RequeueOutcome(count, False)
                actual = (
                    current.attempt_generation,
                    current.worker_generation,
                    current.lease_uuid,
                    current.has_current_tombstone,
                )
                if actual != expected:
                    return RequeueOutcome(count, False)
                takeover = _takeover_kind(current, worker_lease)
                if current.owner_id is None:
                    _fail_locked(
                        current,
                        "permission_changed",
                        worker_lease,
                        now=now,
                        using=using,
                    )
                    return RequeueOutcome(count, True)
                resume_count = current.resume_count
                if takeover == "abnormal":
                    resume_count += 1
                    if resume_count > MAX_ABNORMAL_RESUMES:
                        _fail_locked(
                            current,
                            "worker_repeated_failure",
                            worker_lease,
                            now=now,
                            using=using,
                        )
                        return RequeueOutcome(count, True)
                tombstone = current.has_current_tombstone
                if tombstone:
                    current.attempt_generation += 1
                    _reset_recovered_progress_locked(
                        current,
                        using,
                        totals=totals,
                        has_items=has_items,
                    )
                current.state = "queued"
                current.resume_count = resume_count
                current.worker_generation = worker_lease.worker_generation
                current.lease_uuid = None
                current.lease_expires_at = None
                current.heartbeat_at = now
                update_fields = [
                    "state", "resume_count", "worker_generation",
                    "lease_uuid", "lease_expires_at", "heartbeat_at",
                ]
                if tombstone:
                    update_fields.extend((
                        "attempt_generation", "archive_total", "bytes_total",
                        "archive_done", "bytes_done",
                        "verifying_attempt_generation", "verifying_size",
                        "verifying_sha256", "verifying_completed_at",
                    ))
                current.save(update_fields=tuple(update_fields))
                count += 1
        return RequeueOutcome(count, False)

    return _retry_database(
        requeue,
        monotonic=monotonic,
        sleeper=sleeper,
        retry_checkpoint=retry_checkpoint,
    )


def handoff_after_normal_stop(
    lease,
    heartbeat,
    now=None,
    using="default",
    monotonic=None,
    sleeper=None,
):
    now = timezone.now() if now is None else now
    job = ExportJob.objects.using(using).get(pk=lease.job_id)
    tombstone = job.attempts.filter(
        attempt_generation=job.attempt_generation,
        state="cleaned",
    ).exists()
    totals = None
    has_items = None
    if tombstone:
        totals = _included_totals(
            job,
            using,
            lambda: heartbeat.renew_now(lease),
        )
        has_items = job.items.exists()

    def handoff():
        with heartbeat.job_token_transition(lease) as transition:
            with heartbeat.foreground_write_guard():
                with _worker_write_fence(using, monotonic) as deadline:
                    current = lock_current_lease(lease, using=using)
                    if current.state in ("snapshotting", "archiving"):
                        if _job_has_cleanup_work(current):
                            _mark_attempts_retiring_locked(current)
                            current.staging_cleanup_state = "pending"
                        else:
                            tombstone = current.attempts.filter(
                                attempt_generation=current.attempt_generation,
                                state="cleaned",
                            ).exists()
                            if tombstone:
                                current.attempt_generation += 1
                                _reset_recovered_progress_locked(
                                    current,
                                    using,
                                    deadline.checkpoint,
                                    totals=totals,
                                    has_items=has_items,
                                )
                            current.state = "queued"
                    elif current.state == "verifying":
                        pass
                    elif current.state not in TERMINAL_STATES:
                        current.state = "queued"
                    current.lease_uuid = None
                    current.lease_expires_at = None
                    current.heartbeat_at = now
                    current.save()
            transition.clear()

    _retry_database(
        handoff,
        monotonic=monotonic,
        sleeper=sleeper,
        retry_checkpoint=_retry_control(heartbeat, lease=lease),
    )


def fail_job(
    lease,
    error_code,
    heartbeat,
    now=None,
    using="default",
    monotonic=None,
    sleeper=None,
):
    now = timezone.now() if now is None else now
    worker_token = WorkerLeaseToken(
        lease.worker_generation,
        lease.worker_lease_uuid,
    )

    def fail():
        with heartbeat.job_token_transition(lease) as transition:
            with heartbeat.foreground_write_guard():
                with _worker_write_fence(using, monotonic):
                    job = ExportJob.objects.using(using).get(pk=lease.job_id)
                    failed = _fail_locked(
                        job,
                        error_code,
                        worker_token,
                        job_token=lease,
                        now=now,
                        using=using,
                    )
            transition.clear()
            return failed

    return _retry_database(
        fail,
        monotonic=monotonic,
        sleeper=sleeper,
        retry_checkpoint=_retry_control(heartbeat, lease=lease),
    )


def claim_complete_maintenance(
    worker_lease,
    heartbeat,
    now,
    using="default",
    monotonic=None,
    sleeper=None,
    stop_requested=None,
):
    if heartbeat.current_job_token is not None:
        raise LeaseLost()

    def claim():
        with heartbeat.foreground_write_guard():
            with _worker_write_fence(using, monotonic):
                lock_current_worker_lease(worker_lease, using=using)
                candidates = ExportJob.objects.using(
                    using,
                ).select_for_update().filter(
                    state="complete",
                ).filter(
                    Q(staging_cleanup_state__in=("pending", "blocked"))
                    | Q(lease_uuid__isnull=False)
                ).exclude(
                    staging_cleanup_state="blocked",
                ).order_by("created_at", "id")
                current = candidates.first()
                if current is not None:
                    if (
                        current.lease_uuid is not None
                        and current.worker_generation
                        == worker_lease.worker_generation
                    ):
                        current.lease_expires_at = _worker_expiry(now)
                        current.heartbeat_at = now
                        current.save(update_fields=(
                            "lease_expires_at", "heartbeat_at",
                        ))
                        return LeaseToken(
                            worker_lease.worker_generation,
                            worker_lease.worker_lease_uuid,
                            current.pk,
                            current.lease_uuid,
                            current.attempt_generation,
                        )
                    lease = _new_job_token(worker_lease, current)
                    current.worker_generation = worker_lease.worker_generation
                    current.lease_uuid = lease.job_lease_uuid
                    current.lease_expires_at = _worker_expiry(now)
                    current.heartbeat_at = now
                    current.save(update_fields=(
                        "worker_generation", "lease_uuid", "lease_expires_at",
                        "heartbeat_at",
                    ))
                    return lease
                return None

    return _retry_database(
        claim,
        monotonic=monotonic,
        sleeper=sleeper,
        retry_checkpoint=_retry_control(
            heartbeat,
            stop_requested=stop_requested,
        ),
    )


def release_job_lease(
    lease,
    heartbeat,
    now=None,
    using="default",
    monotonic=None,
    sleeper=None,
):
    now = timezone.now() if now is None else now
    expected = heartbeat.current_job_token
    if expected not in (None, lease):
        raise LeaseLost()

    def release():
        with heartbeat.job_token_transition(expected) as transition:
            with heartbeat.foreground_write_guard():
                with _worker_write_fence(using, monotonic):
                    current = lock_current_lease(lease, using=using)
                    current.lease_uuid = None
                    current.lease_expires_at = None
                    current.heartbeat_at = now
                    current.save(update_fields=(
                        "lease_uuid", "lease_expires_at", "heartbeat_at",
                    ))
            if expected is not None:
                transition.clear()

    _retry_database(
        release,
        monotonic=monotonic,
        sleeper=sleeper,
        retry_checkpoint=_retry_control(heartbeat, lease=lease),
    )


def _ready_snapshot(job):
    return (
        job.state,
        job.ready_cleanup_state,
        job.ready_relative_path,
        job.ready_size,
        job.ready_sha256,
        job.ready_dev,
        job.ready_ino,
        job.ready_uid,
        job.ready_gid,
        job.ready_mode,
        job.ready_nlink,
        job.ready_mtime_ns,
        job.ready_ctime_ns,
    )


def _ready_receipt(job):
    values = (
        job.ready_dev,
        job.ready_ino,
        job.ready_uid,
        job.ready_gid,
        job.ready_mode,
        job.ready_nlink,
        job.ready_size,
        job.ready_mtime_ns,
        job.ready_ctime_ns,
    )
    if any(value is None for value in values):
        raise ExportStorageError("export_storage_unsafe")
    return ClosedFileReceipt(*(values + (job.ready_sha256,)))


def _open_named_directory(root, name, create=False):
    try:
        current = os.stat(
            name,
            dir_fd=root.descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        if create:
            return create_private_directory(root, name, root.uid, root.gid)
        raise ExportStorageError("export_storage_unsafe") from None
    receipt = DirectoryReceipt(
        current.st_dev,
        current.st_ino,
        current.st_uid,
        current.st_gid,
        stat.S_IMODE(current.st_mode),
    )
    return open_receipted_directory(root, name, receipt)


def _remove_ready_file_fs(job):
    expected = "ready/{}.zip".format(job.pk)
    if job.ready_relative_path != expected:
        raise ExportStorageError("export_storage_unsafe")
    root = ready = None
    try:
        root = open_export_root(
            settings.PINRY_EXPORT_ROOT,
            os.getuid(),
            os.getgid(),
        )
        ready = _open_named_directory(root, "ready")
        removed = remove_if_receipt_matches(
            ready,
            "{}.zip".format(job.pk),
            _ready_receipt(job),
        )
        if not removed:
            ready.verify_identity()
            os.fsync(ready.descriptor)
        return removed
    finally:
        if ready is not None:
            ready.close()
        if root is not None:
            root.close()


_READY_FIELDS = (
    "ready_relative_path", "ready_display_name", "ready_size",
    "ready_sha256", "ready_dev", "ready_ino", "ready_uid", "ready_gid",
    "ready_mode", "ready_nlink", "ready_mtime_ns", "ready_ctime_ns",
)


def _mark_ready_cleanup(
    worker_lease,
    job,
    expected,
    state,
    heartbeat,
    using,
):
    with heartbeat.foreground_write_guard():
        with _worker_write_fence(using):
            lock_current_worker_lease(worker_lease, using=using)
            current = ExportJob.objects.using(using).select_for_update().get(
                pk=job.pk,
            )
            if _ready_snapshot(current) != expected:
                raise LeaseLost()
            current.ready_cleanup_state = state
            update_fields = ["ready_cleanup_state"]
            if state == "cleaned":
                for field in _READY_FIELDS:
                    setattr(current, field, None)
                update_fields.extend(_READY_FIELDS)
            current.save(update_fields=tuple(update_fields))


def _expire_complete_jobs(worker_lease, heartbeat, now, using):
    with heartbeat.foreground_write_guard():
        with _worker_write_fence(using) as deadline:
            lock_current_worker_lease(worker_lease, using=using)
            jobs = list(ExportJob.objects.using(using).select_for_update().filter(
                state="complete",
                ready_cleanup_state="retained",
            ).filter(
                Q(expires_at__lte=now) | Q(owner_id__isnull=True)
            ).order_by("created_at", "id")[:MAINTENANCE_BATCH_SIZE])
            for current in jobs:
                deadline.checkpoint()
                current.state = "expired"
                current.ready_cleanup_state = "pending"
                current.save(update_fields=("state", "ready_cleanup_state"))
            return len(jobs)


def _cleanup_one_ready(worker_lease, heartbeat, using):
    with heartbeat.foreground_write_guard():
        with _worker_write_fence(using):
            lock_current_worker_lease(worker_lease, using=using)
            job = ExportJob.objects.using(using).select_for_update().filter(
                state="expired",
                ready_cleanup_state="pending",
            ).order_by("created_at", "id").first()
            if job is None:
                return False
            expected = _ready_snapshot(job)
    try:
        _remove_ready_file_fs(job)
    except ExportStorageError:
        state = _classify_ready_cleanup_error(job)
        if state == "pending":
            return True
        _mark_ready_cleanup(
            worker_lease,
            job,
            expected,
            state,
            heartbeat,
            using,
        )
        return True
    _mark_ready_cleanup(
        worker_lease,
        job,
        expected,
        "cleaned",
        heartbeat,
        using,
    )
    return True


def _classify_ready_cleanup_error(job):
    expected = "ready/{}.zip".format(job.pk)
    if job.ready_relative_path != expected:
        return "blocked"
    try:
        receipt = _ready_receipt(job)
    except ExportStorageError:
        return "blocked"
    root = ready = None
    try:
        root = open_export_root(
            settings.PINRY_EXPORT_ROOT,
            os.getuid(),
            os.getgid(),
        )
        ready = _open_named_directory(root, "ready")
        try:
            current = os.stat(
                "{}.zip".format(job.pk),
                dir_fd=ready.descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return "pending"
        return "pending" if receipt.matches_stat(current) else "blocked"
    except ExportStorageError:
        return "blocked"
    finally:
        if ready is not None:
            ready.close()
        if root is not None:
            root.close()


def _file_receipt(model):
    open_values = (
        model.receipt_dev,
        model.receipt_ino,
        model.receipt_uid,
        model.receipt_gid,
        model.receipt_mode,
        model.receipt_nlink,
    )
    if any(value is None for value in open_values):
        return None
    if model.receipt_level == "open":
        return OpenFileReceipt(*open_values)
    full_values = (
        model.receipt_size,
        model.receipt_mtime_ns,
        model.receipt_ctime_ns,
        model.receipt_sha256,
    )
    if any(value is None for value in full_values[:3]):
        return None
    return ClosedFileReceipt(*(open_values + full_values))


def _attempt_directory_receipt(attempt):
    values = (
        attempt.dir_dev,
        attempt.dir_ino,
        attempt.dir_uid,
        attempt.dir_gid,
        attempt.dir_mode,
    )
    if any(value is None for value in values):
        return None
    return DirectoryReceipt(*values)


def _split_cleanup_path(relative_path, attempt):
    if relative_path == "ready/{}.zip".format(attempt.job_id):
        return "ready", "{}.zip".format(attempt.job_id)
    prefix = "{}/".format(attempt.relative_path)
    if relative_path and relative_path.startswith(prefix):
        leaf = relative_path[len(prefix):]
        if leaf and "/" not in leaf:
            return "attempt", leaf
    raise _CleanupBlocked()


def _open_cleanup_parent(root, attempt, kind):
    if kind == "ready":
        return _open_named_directory(root, "ready")
    staging = open_staging_directory(root)
    receipt = _attempt_directory_receipt(attempt)
    if receipt is None:
        staging.close()
        raise _CleanupBlocked()
    try:
        directory = open_receipted_directory(
            staging,
            attempt.relative_path,
            receipt,
        )
    except BaseException:
        staging.close()
        raise
    return staging, directory


def _cleanup_attempt_file_fs(attempt, attempt_file):
    receipt = _file_receipt(attempt_file)
    if receipt is None:
        raise _CleanupBlocked()
    paths = []
    for value in (
        attempt_file.relative_path,
        attempt_file.intent_relative_path,
    ):
        if value and value not in paths:
            paths.append(value)
    root = None
    opened = []
    matching = []
    foreign = False
    try:
        root = open_export_root(
            settings.PINRY_EXPORT_ROOT,
            os.getuid(),
            os.getgid(),
        )
        for relative in paths:
            kind, leaf = _split_cleanup_path(relative, attempt)
            parents = _open_cleanup_parent(root, attempt, kind)
            if isinstance(parents, tuple):
                staging, directory = parents
                opened.extend((directory, staging))
            else:
                directory = parents
                opened.append(directory)
            try:
                current = os.stat(
                    leaf,
                    dir_fd=directory.descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                continue
            if receipt.matches_stat(current):
                matching.append((directory, leaf))
            else:
                foreign = True
        if foreign or len(matching) > 1:
            raise _CleanupBlocked()
        if matching:
            directory, leaf = matching[0]
            try:
                remove_if_receipt_matches(directory, leaf, receipt)
            except ExportStorageError:
                try:
                    current = os.stat(
                        leaf,
                        dir_fd=directory.descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    raise
                if not receipt.matches_stat(current):
                    raise _CleanupBlocked() from None
                raise
    finally:
        for directory in opened:
            directory.close()
        if root is not None:
            root.close()


def _close_quarantine_receipt_fs(attempt, attempt_file):
    receipt = _file_receipt(attempt_file)
    if not isinstance(receipt, OpenFileReceipt):
        raise _CleanupBlocked()
    paths = []
    for value in (
        attempt_file.relative_path,
        attempt_file.intent_relative_path,
    ):
        if value and value not in paths:
            paths.append(value)
    root = None
    opened_directories = []
    matches = []
    foreign = False
    descriptor = None
    try:
        root = open_export_root(
            settings.PINRY_EXPORT_ROOT,
            os.getuid(),
            os.getgid(),
        )
        for relative in paths:
            kind, leaf = _split_cleanup_path(relative, attempt)
            parents = _open_cleanup_parent(root, attempt, kind)
            if isinstance(parents, tuple):
                staging, directory = parents
                opened_directories.extend((directory, staging))
            else:
                directory = parents
                opened_directories.append(directory)
            try:
                current = os.stat(
                    leaf,
                    dir_fd=directory.descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                continue
            if receipt.matches_stat(current):
                matches.append((relative, directory, leaf))
            else:
                foreign = True
        if foreign or len(matches) != 1:
            raise _CleanupBlocked()
        relative, directory, leaf = matches[0]
        descriptor = os.open(
            leaf,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=directory.descriptor,
        )
        opened = OpenFileReceipt.from_fd(
            descriptor,
            directory.uid,
            directory.gid,
        )
        if opened != receipt:
            raise _CleanupBlocked()
        return relative, ClosedFileReceipt.from_open_fd(
            descriptor,
            opened,
        )
    finally:
        if descriptor is not None:
            os.close(descriptor)
        for directory in opened_directories:
            directory.close()
        if root is not None:
            root.close()


def _set_closed_receipt(model, receipt):
    model.receipt_level = "full"
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


def _remove_attempt_directory_fs(attempt):
    receipt = _attempt_directory_receipt(attempt)
    if receipt is None:
        raise _CleanupBlocked()
    expected_name = "attempt-{}-{}".format(
        attempt.job_id,
        attempt.attempt_generation,
    )
    if attempt.relative_path != expected_name:
        raise _CleanupBlocked()
    root = staging = directory = None
    try:
        root = open_export_root(
            settings.PINRY_EXPORT_ROOT,
            os.getuid(),
            os.getgid(),
        )
        staging = open_staging_directory(root)
        try:
            current = os.stat(
                expected_name,
                dir_fd=staging.descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            staging.verify_identity()
            return
        if not receipt.matches_stat(current):
            raise _CleanupBlocked()
        directory = open_receipted_directory(
            staging,
            expected_name,
            receipt,
        )
        if os.listdir(directory.descriptor):
            raise _CleanupBlocked()
        directory.verify_identity()
        if os.listdir(directory.descriptor):
            raise _CleanupBlocked()
        os.rmdir(expected_name, dir_fd=staging.descriptor)
        os.fsync(staging.descriptor)
    finally:
        for current in (directory, staging, root):
            if current is not None:
                current.close()


def _terminal_attempt_cleanup(worker_lease, heartbeat, job, using):
    with heartbeat.foreground_write_guard():
        with _worker_write_fence(using):
            lock_current_worker_lease(worker_lease, using=using)
            current = ExportJob.objects.using(using).select_for_update().get(
                pk=job.pk,
            )
            attempt = current.attempts.select_for_update().exclude(
                state="cleaned",
            ).order_by("attempt_generation").first()
            if attempt is None:
                return False
            attempt_file = attempt.files.select_for_update().exclude(
                state="cleaned",
            ).order_by("kind", "pk").first()
            if attempt_file is not None:
                file_snapshot = (
                    attempt_file.kind,
                    attempt_file.state,
                    attempt_file.relative_path,
                    attempt_file.intent_relative_path,
                    attempt_file.receipt_level,
                    _file_receipt(attempt_file),
                )
                ready_handoff = (
                    attempt_file.kind == "archive"
                    and attempt_file.state == "published"
                    and attempt_file.relative_path == current.ready_relative_path
                    and current.ready_cleanup_state in (
                        "retained", "pending", "blocked",
                    )
                )
            else:
                file_snapshot = None
                ready_handoff = False
    needs_quarantine_close = (
        attempt_file is not None
        and attempt_file.kind == "quarantine"
        and attempt_file.state == "writing"
        and attempt_file.receipt_level == "open"
    )
    if needs_quarantine_close:
        matched_path, closed_receipt = _close_quarantine_receipt_fs(
            attempt,
            attempt_file,
        )
        with heartbeat.foreground_write_guard():
            with _worker_write_fence(using):
                lock_current_worker_lease(worker_lease, using=using)
                current_file = ExportAttemptFile.objects.using(
                    using,
                ).select_for_update().get(pk=attempt_file.pk)
                current_snapshot = (
                    current_file.kind,
                    current_file.state,
                    current_file.relative_path,
                    current_file.intent_relative_path,
                    current_file.receipt_level,
                    _file_receipt(current_file),
                )
                if current_snapshot != file_snapshot:
                    raise LeaseLost()
                current_file.state = "closed"
                current_file.relative_path = matched_path
                current_file.intent_relative_path = None
                _set_closed_receipt(current_file, closed_receipt)
                current_file.save()
        return True
    if attempt_file is not None and not ready_handoff:
        _cleanup_attempt_file_fs(attempt, attempt_file)
    if attempt_file is not None:
        with heartbeat.foreground_write_guard():
            with _worker_write_fence(using):
                lock_current_worker_lease(worker_lease, using=using)
                current_file = ExportAttemptFile.objects.using(
                    using,
                ).select_for_update().get(pk=attempt_file.pk)
                current_snapshot = (
                    current_file.kind,
                    current_file.state,
                    current_file.relative_path,
                    current_file.intent_relative_path,
                    current_file.receipt_level,
                    _file_receipt(current_file),
                )
                if current_snapshot != file_snapshot:
                    raise LeaseLost()
                current_file.state = "cleaned"
                current_file.intent_relative_path = None
                current_file.save(update_fields=(
                    "state", "intent_relative_path",
                ))
        return True
    _remove_attempt_directory_fs(attempt)
    with heartbeat.foreground_write_guard():
        with _worker_write_fence(using):
            lock_current_worker_lease(worker_lease, using=using)
            current_attempt = ExportAttempt.objects.using(
                using,
            ).select_for_update().get(pk=attempt.pk)
            if current_attempt.files.exclude(state="cleaned").exists():
                raise LeaseLost()
            current_attempt.state = "cleaned"
            current_attempt.save(update_fields=("state",))
    return True


def _blob_receipt(blob):
    values = (
        blob.receipt_dev,
        blob.receipt_ino,
        blob.receipt_uid,
        blob.receipt_gid,
        blob.receipt_mode,
        blob.receipt_nlink,
    )
    if any(value is None for value in values):
        return None
    if blob.file_state == "writing":
        return OpenFileReceipt(*values)
    full = (
        blob.size,
        blob.receipt_mtime_ns,
        blob.receipt_ctime_ns,
        blob.receipt_sha256,
    )
    if any(value is None for value in full[:3]):
        return None
    return ClosedFileReceipt(*(values + full))


def _cleanup_snapshot_tree_fs(job, candidate, blob=None):  # noqa: C901
    generation = (
        job.candidate_snapshot_generation if candidate
        else job.snapshot_generation
    )
    relative_path = (
        job.candidate_snapshot_relative_path if candidate
        else job.snapshot_relative_path
    )
    expected_name = snapshot_directory_name(job.pk, generation)
    if generation is None:
        return
    if relative_path != expected_name:
        raise _CleanupBlocked()
    values = (
        (
            job.candidate_snapshot_dir_dev,
            job.candidate_snapshot_dir_ino,
            job.candidate_snapshot_dir_uid,
            job.candidate_snapshot_dir_gid,
            job.candidate_snapshot_dir_mode,
        )
        if candidate else (
            job.snapshot_dir_dev,
            job.snapshot_dir_ino,
            job.snapshot_dir_uid,
            job.snapshot_dir_gid,
            job.snapshot_dir_mode,
        )
    )
    if candidate and any(value is None for value in values) and blob is not None:
        raise _CleanupBlocked()
    root = staging = directory = None
    try:
        root = open_export_root(
            settings.PINRY_EXPORT_ROOT,
            os.getuid(),
            os.getgid(),
        )
        staging = open_staging_directory(root)
        try:
            named = os.stat(
                expected_name,
                dir_fd=staging.descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            staging.verify_identity()
            return
        if any(value is None for value in values):
            if not candidate:
                raise _CleanupBlocked()
            receipt = DirectoryReceipt(
                named.st_dev,
                named.st_ino,
                named.st_uid,
                named.st_gid,
                stat.S_IMODE(named.st_mode),
            )
            if (
                receipt.uid != staging.uid
                or receipt.gid != staging.gid
                or receipt.mode != 0o700
            ):
                raise _CleanupBlocked()
        else:
            receipt = DirectoryReceipt(*values)
            if not receipt.matches_stat(named):
                raise _CleanupBlocked()
        directory = open_receipted_directory(
            staging,
            expected_name,
            receipt,
        )
        if blob is not None:
            receipt_file = _blob_receipt(blob)
            names = []
            for relative in (
                blob.part_relative_path,
                blob.snapshot_relative_path,
            ):
                prefix = "{}/".format(expected_name)
                if relative and relative.startswith(prefix):
                    leaf = relative[len(prefix):]
                    if leaf and "/" not in leaf and leaf not in names:
                        names.append(leaf)
            if not names:
                names = [
                    "{}.part".format(blob.pk),
                    snapshot_blob_name(blob.pk),
                ]
            matches = []
            foreign = False
            for name in names:
                try:
                    current = os.stat(
                        name,
                        dir_fd=directory.descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    continue
                if receipt_file is not None and receipt_file.matches_stat(current):
                    matches.append(name)
                else:
                    foreign = True
            if foreign or len(matches) > 1:
                raise _CleanupBlocked()
            if matches:
                remove_if_receipt_matches(
                    directory,
                    matches[0],
                    receipt_file,
                )
            return
        if os.listdir(directory.descriptor):
            raise _CleanupBlocked()
        directory.verify_identity()
        if os.listdir(directory.descriptor):
            raise _CleanupBlocked()
        os.rmdir(expected_name, dir_fd=staging.descriptor)
        os.fsync(staging.descriptor)
    finally:
        for current in (directory, staging, root):
            if current is not None:
                current.close()


def _terminal_snapshot_cleanup(worker_lease, heartbeat, job, using):
    with heartbeat.foreground_write_guard():
        with _worker_write_fence(using):
            lock_current_worker_lease(worker_lease, using=using)
            current = ExportJob.objects.using(using).select_for_update().get(
                pk=job.pk,
            )
            candidate = current.candidate_snapshot_generation is not None
            if not candidate and current.snapshot_generation is None:
                return False
            generation = (
                current.candidate_snapshot_generation if candidate
                else current.snapshot_generation
            )
            expected = (
                generation,
                current.candidate_snapshot_relative_path if candidate
                else current.snapshot_relative_path,
            )
            blob = current.blobs.select_for_update().filter(
                snapshot_generation=generation,
            ).exclude(
                cleanup_state="cleaned",
            ).order_by("pk").first()
            blob_snapshot = None if blob is None else (
                blob.pk,
                blob.file_state,
                blob.part_relative_path,
                blob.snapshot_relative_path,
                blob.cleanup_state,
                _blob_receipt(blob),
            )
    heartbeat.renew_worker_now()
    _cleanup_snapshot_tree_fs(current, candidate, blob=blob)
    heartbeat.renew_worker_now()
    with heartbeat.foreground_write_guard():
        with _worker_write_fence(using):
            lock_current_worker_lease(worker_lease, using=using)
            current = ExportJob.objects.using(using).select_for_update().get(
                pk=job.pk,
            )
            actual = (
                current.candidate_snapshot_generation if candidate
                else current.snapshot_generation,
                current.candidate_snapshot_relative_path if candidate
                else current.snapshot_relative_path,
            )
            if actual != expected:
                raise LeaseLost()
            if blob is not None:
                current_blob = current.blobs.select_for_update().get(
                    pk=blob.pk,
                )
                actual_blob = (
                    current_blob.pk,
                    current_blob.file_state,
                    current_blob.part_relative_path,
                    current_blob.snapshot_relative_path,
                    current_blob.cleanup_state,
                    _blob_receipt(current_blob),
                )
                if actual_blob != blob_snapshot:
                    raise LeaseLost()
                current_blob.cleanup_state = "cleaned"
                current_blob.save(update_fields=("cleanup_state",))
                return True
            if candidate:
                fields = (
                    "candidate_snapshot_generation",
                    "candidate_snapshot_relative_path",
                    "candidate_snapshot_dir_dev",
                    "candidate_snapshot_dir_ino",
                    "candidate_snapshot_dir_uid",
                    "candidate_snapshot_dir_gid",
                    "candidate_snapshot_dir_mode",
                )
            else:
                fields = (
                    "snapshot_generation", "snapshot_relative_path",
                    "snapshot_dir_dev", "snapshot_dir_ino",
                    "snapshot_dir_uid", "snapshot_dir_gid",
                    "snapshot_dir_mode",
                )
            for field in fields:
                setattr(current, field, None)
            current.save(update_fields=fields)
    return True


def _terminal_staging_cleanup(worker_lease, heartbeat, job, using):
    try:
        if _terminal_attempt_cleanup(worker_lease, heartbeat, job, using):
            return True
        job.refresh_from_db()
        if _terminal_snapshot_cleanup(worker_lease, heartbeat, job, using):
            return True
        with heartbeat.foreground_write_guard():
            with _worker_write_fence(using):
                lock_current_worker_lease(worker_lease, using=using)
                current = ExportJob.objects.using(
                    using,
                ).select_for_update().get(pk=job.pk)
                if (
                    current.attempts.exclude(state="cleaned").exists()
                    or current.blobs.exclude(cleanup_state="cleaned").exists()
                    or current.candidate_snapshot_generation is not None
                    or current.snapshot_generation is not None
                ):
                    raise _CleanupBlocked()
                current.staging_cleanup_state = "cleaned"
                current.save(update_fields=("staging_cleanup_state",))
        return True
    except _CleanupBlocked:
        with heartbeat.foreground_write_guard():
            with _worker_write_fence(using):
                lock_current_worker_lease(worker_lease, using=using)
                current = ExportJob.objects.using(
                    using,
                ).select_for_update().get(pk=job.pk)
                current.staging_cleanup_state = "blocked"
                current.save(update_fields=("staging_cleanup_state",))
        return True
    except (ExportStorageError, OSError):
        return True


def _cleanup_one_terminal_staging(worker_lease, heartbeat, using):
    with heartbeat.foreground_write_guard():
        with _worker_write_fence(using):
            lock_current_worker_lease(worker_lease, using=using)
            job = ExportJob.objects.using(using).select_for_update().filter(
                state__in=("failed", "expired"),
                staging_cleanup_state="pending",
            ).order_by("created_at", "id").first()
    if job is None:
        return False
    return _terminal_staging_cleanup(worker_lease, heartbeat, job, using)


def _delete_clean_orphans(worker_lease, heartbeat, using):
    unclean_blobs = ExportBlob.objects.using(using).filter(
        job_id=OuterRef("pk"),
    ).exclude(cleanup_state="cleaned")
    unclean_attempts = ExportAttempt.objects.using(using).filter(
        job_id=OuterRef("pk"),
    ).exclude(state="cleaned")
    unclean_files = ExportAttemptFile.objects.using(using).filter(
        attempt__job_id=OuterRef("pk"),
    ).exclude(state="cleaned")
    with heartbeat.foreground_write_guard():
        with _worker_write_fence(using):
            lock_current_worker_lease(worker_lease, using=using)
            current = ExportJob.objects.using(using).select_for_update().filter(
                owner_id__isnull=True,
                state__in=("failed", "expired"),
                staging_cleanup_state="cleaned",
                ready_cleanup_state__in=("absent", "cleaned"),
                snapshot_relative_path__isnull=True,
                candidate_snapshot_relative_path__isnull=True,
                ready_relative_path__isnull=True,
            ).annotate(
                has_unclean_blob=Exists(unclean_blobs),
                has_unclean_attempt=Exists(unclean_attempts),
                has_unclean_file=Exists(unclean_files),
            ).filter(
                has_unclean_blob=False,
                has_unclean_attempt=False,
                has_unclean_file=False,
            ).order_by("created_at", "id").first()
            if current is None:
                return 0
            current.delete()
            return 1


def _maintenance_claim_allowed(using):
    return not ExportJob.objects.using(using).filter(
        Q(staging_cleanup_state__in=("pending", "blocked"))
        & Q(state__in=("failed", "expired"))
        | Q(staging_cleanup_state="blocked", state="complete")
        | Q(ready_cleanup_state__in=("pending", "blocked"))
    ).exists()


def _terminalize_ownerless_active(worker_lease, heartbeat, now, using):
    with heartbeat.foreground_write_guard():
        with _worker_write_fence(using):
            lock_current_worker_lease(worker_lease, using=using)
            current = ExportJob.objects.using(using).select_for_update().filter(
                owner_id__isnull=True,
                state__in=ACTIVE_STATES,
            ).order_by("state", "created_at", "id").first()
            if current is None:
                return False
            _fail_locked(
                current,
                "permission_changed",
                worker_lease,
                now=now,
                using=using,
            )
            return True


def _cleanup_expired_and_stale_once(
    worker_lease,
    heartbeat,
    stop_requested,
    now=None,
    using="default",
    archive_service=None,
    monotonic=None,
    sleeper=None,
):
    now = timezone.now() if now is None else now
    archive_service = archive_service or ArchiveService(using=using)
    did_work = False
    if stop_requested is not None and stop_requested():
        return MaintenanceOutcome(False, False)

    if _terminalize_ownerless_active(
        worker_lease,
        heartbeat,
        now,
        using,
    ):
        return MaintenanceOutcome(True, False)

    complete_lease = claim_complete_maintenance(
        worker_lease,
        heartbeat,
        now,
        using=using,
        monotonic=monotonic,
        sleeper=sleeper,
        stop_requested=stop_requested,
    )
    if complete_lease is not None:
        try:
            archive_service.recover_complete(
                complete_lease,
                heartbeat,
                stop_requested,
            )
        except ExportError:
            pass
        finally:
            release_job_lease(
                complete_lease,
                heartbeat,
                now=now,
                using=using,
            )
        did_work = True
        return MaintenanceOutcome(
            did_work,
            _maintenance_claim_allowed(using),
        )

    expired_count = _expire_complete_jobs(
        worker_lease,
        heartbeat,
        now,
        using,
    )
    did_work = expired_count > 0
    if stop_requested is not None and stop_requested():
        return MaintenanceOutcome(did_work, False)
    if _cleanup_one_ready(worker_lease, heartbeat, using):
        did_work = True
    if stop_requested is not None and stop_requested():
        return MaintenanceOutcome(did_work, False)
    if _cleanup_one_terminal_staging(worker_lease, heartbeat, using):
        did_work = True
    if stop_requested is not None and stop_requested():
        return MaintenanceOutcome(did_work, False)
    if _delete_clean_orphans(worker_lease, heartbeat, using):
        did_work = True
    return MaintenanceOutcome(
        did_work,
        _maintenance_claim_allowed(using),
    )


def cleanup_expired_and_stale(
    worker_lease,
    heartbeat,
    stop_requested,
    now=None,
    using="default",
    archive_service=None,
    monotonic=None,
    sleeper=None,
):
    def maintain():
        return _cleanup_expired_and_stale_once(
            worker_lease,
            heartbeat,
            stop_requested,
            now=now,
            using=using,
            archive_service=archive_service,
            monotonic=monotonic,
            sleeper=sleeper,
        )

    try:
        return _retry_database(
            maintain,
            monotonic=monotonic,
            sleeper=sleeper,
            retry_checkpoint=_retry_control(
                heartbeat,
                stop_requested=stop_requested,
            ),
        )
    except _DatabaseRetryStopped:
        return MaintenanceOutcome(False, False)


def release_worker_lease(
    worker_lease,
    now=None,
    using="default",
    monotonic=None,
    sleeper=None,
):
    now = timezone.now() if now is None else now

    def release():
        with _worker_write_fence(using, monotonic):
            updated = ExportWorkerLease.objects.using(using).filter(
                pk=1,
                generation=worker_lease.worker_generation,
                lease_uuid=worker_lease.worker_lease_uuid,
            ).update(
                lease_uuid=None,
                lease_expires_at=None,
                heartbeat_at=now,
                health_state="stopped",
                error_code=None,
            )
            if updated != 1:
                raise LeaseLost()

    return _retry_database(
        release,
        monotonic=monotonic,
        sleeper=sleeper,
    )


class ExportWorker(object):
    def __init__(
        self,
        using="default",
        clock=None,
        monotonic=None,
        sleeper=None,
        snapshot_service=None,
        archive_service=None,
        heartbeat_factory=None,
    ):
        self.using = using
        self.clock = timezone.now if clock is None else clock
        self.monotonic = time.monotonic if monotonic is None else monotonic
        self.sleeper = time.sleep if sleeper is None else sleeper
        self.snapshot_service = snapshot_service or SnapshotService(using=using)
        self.archive_service = archive_service or ArchiveService(using=using)
        self.heartbeat_factory = heartbeat_factory or LeaseHeartbeat
        self.worker_token = None
        self.heartbeat = None

    def acquire_worker_lease(self, now):
        self.worker_token = acquire_worker_lease(
            now,
            using=self.using,
            monotonic=self.monotonic,
            sleeper=self.sleeper,
        )
        return self.worker_token

    def claim_next_job(
        self,
        worker_lease,
        heartbeat,
        now,
        stop_requested=None,
    ):
        return claim_next_job(
            worker_lease,
            heartbeat,
            now,
            using=self.using,
            monotonic=self.monotonic,
            sleeper=self.sleeper,
            stop_requested=stop_requested,
        )

    def claim_retiring_recovery(
        self,
        worker_lease,
        heartbeat,
        now,
        stop_requested=None,
    ):
        return claim_retiring_recovery(
            worker_lease,
            heartbeat,
            now,
            using=self.using,
            monotonic=self.monotonic,
            sleeper=self.sleeper,
            stop_requested=stop_requested,
        )

    def claim_verifying_recovery(
        self,
        worker_lease,
        heartbeat,
        now,
        stop_requested=None,
    ):
        return claim_verifying_recovery(
            worker_lease,
            heartbeat,
            now,
            using=self.using,
            monotonic=self.monotonic,
            sleeper=self.sleeper,
            stop_requested=stop_requested,
        )

    def requeue_expired_nonverifying_jobs(
        self,
        worker_lease,
        now,
        stop_requested=None,
    ):
        retry_checkpoint = _retry_control(
            self.heartbeat,
            stop_requested=stop_requested,
        )
        return requeue_expired_nonverifying_jobs(
            worker_lease,
            now,
            using=self.using,
            monotonic=self.monotonic,
            sleeper=self.sleeper,
            checkpoint=retry_checkpoint,
            write_guard=self.heartbeat.foreground_write_guard,
            retry_checkpoint=retry_checkpoint,
        )

    def cleanup_expired_and_stale(
        self,
        worker_lease,
        heartbeat,
        stop_requested,
    ):
        return cleanup_expired_and_stale(
            worker_lease,
            heartbeat,
            stop_requested,
            now=self.clock(),
            using=self.using,
            archive_service=self.archive_service,
            monotonic=self.monotonic,
            sleeper=self.sleeper,
        )

    def handoff_after_normal_stop(self, lease):
        return handoff_after_normal_stop(
            lease,
            self.heartbeat,
            self.clock(),
            using=self.using,
            monotonic=self.monotonic,
            sleeper=self.sleeper,
        )

    def fail_job(self, lease, error_code):
        return fail_job(
            lease,
            error_code,
            self.heartbeat,
            self.clock(),
            using=self.using,
            monotonic=self.monotonic,
            sleeper=self.sleeper,
        )

    def _release_terminal(self, lease):
        job = ExportJob.objects.using(self.using).get(pk=lease.job_id)
        if job.lease_uuid is None:
            return
        release_job_lease(
            lease,
            self.heartbeat,
            now=self.clock(),
            using=self.using,
            monotonic=self.monotonic,
            sleeper=self.sleeper,
        )

    def _raise_heartbeat_failure(self):
        if self.heartbeat.lost is not None:
            raise self.heartbeat.lost
        if self.heartbeat.fatal is not None:
            raise _HeartbeatFailed()

    def _dispatch_claim(self, lease, stop_requested, mode):
        if mode == "verifying":
            outcome = self.archive_service.recover_verifying(
                lease,
                self.heartbeat,
                stop_requested,
            )
            if outcome.job.state in TERMINAL_STATES:
                self._release_terminal(outcome.lease)
                return
            lease = outcome.lease
        elif mode == "retiring":
            while ExportAttempt.objects.using(self.using).filter(
                job_id=lease.job_id,
                state="retiring",
            ).exists():
                outcome = self.archive_service.recover_retiring(
                    lease,
                    self.heartbeat,
                    stop_requested,
                )
                lease = outcome.lease
            job = ExportJob.objects.using(self.using).get(pk=lease.job_id)
            if job.state == "snapshotting":
                self.snapshot_service.capture(
                    job,
                    lease,
                    self.heartbeat,
                    stop_requested,
                )
                job.refresh_from_db()
            if job.state == "archiving":
                outcome = self.archive_service.recover_archiving(
                    lease,
                    self.heartbeat,
                    stop_requested,
                )
                if outcome.job.state in TERMINAL_STATES:
                    self._release_terminal(outcome.lease)
                return
        job = ExportJob.objects.using(self.using).get(pk=lease.job_id)
        if job.snapshot_generation is None:
            self.snapshot_service.capture(
                job,
                lease,
                self.heartbeat,
                stop_requested,
            )
            job.refresh_from_db()
        if job.state == "archiving":
            outcome = self.archive_service.recover_archiving(
                lease,
                self.heartbeat,
                stop_requested,
            )
            if outcome.job.state in TERMINAL_STATES:
                self._release_terminal(outcome.lease)
            return
        completed = job
        if completed.state in TERMINAL_STATES:
            current = ExportJob.objects.using(self.using).get(pk=job.pk)
            terminal_lease = LeaseToken(
                lease.worker_generation,
                lease.worker_lease_uuid,
                lease.job_id,
                current.lease_uuid or lease.job_lease_uuid,
                current.attempt_generation,
            )
            if current.lease_uuid is not None:
                self._release_terminal(terminal_lease)

    def run_once(self, stop_requested):
        try:
            return self._run_once_active(stop_requested)
        except _DatabaseRetryStopped:
            return False
        except StopRequested as stopped:
            current = ExportJob.objects.using(self.using).get(
                pk=stopped.lease.job_id,
            )
            if current.state in TERMINAL_STATES:
                if current.lease_uuid is not None:
                    self._release_terminal(stopped.lease)
            else:
                self.handoff_after_normal_stop(stopped.lease)
            return True
        except LeaseLost:
            raise
        except ExportError as error:
            current = ExportJob.objects.using(self.using).get(
                pk=error.lease.job_id,
            )
            if current.state in TERMINAL_STATES:
                if current.lease_uuid is not None:
                    self._release_terminal(error.lease)
            else:
                self.fail_job(error.lease, error.code)
            return True

    def _run_once_active(self, stop_requested):  # noqa: C901
        self._raise_heartbeat_failure()
        maintenance = self.cleanup_expired_and_stale(
            self.worker_token,
            self.heartbeat,
            stop_requested,
        )
        if maintenance.did_work or not maintenance.claim_allowed:
            return maintenance.did_work
        if stop_requested():
            return False
        try:
            self._raise_heartbeat_failure()
            claim = self.claim_retiring_recovery(
                self.worker_token,
                self.heartbeat,
                self.clock(),
                stop_requested,
            )
            if claim.terminalized:
                return True
            lease = claim.lease
            mode = "retiring" if lease is not None else None
            if lease is None:
                self._raise_heartbeat_failure()
                claim = self.claim_verifying_recovery(
                    self.worker_token,
                    self.heartbeat,
                    self.clock(),
                    stop_requested,
                )
                if claim.terminalized:
                    return True
                lease = claim.lease
                mode = "verifying" if lease is not None else None
            if lease is None:
                self._raise_heartbeat_failure()
                requeue = self.requeue_expired_nonverifying_jobs(
                    self.worker_token,
                    self.clock(),
                    stop_requested,
                )
                if requeue.terminalized:
                    return True
                self._raise_heartbeat_failure()
                claim = self.claim_retiring_recovery(
                    self.worker_token,
                    self.heartbeat,
                    self.clock(),
                    stop_requested,
                )
                if claim.terminalized:
                    return True
                lease = claim.lease
                mode = "retiring" if lease is not None else None
            if lease is None:
                self._raise_heartbeat_failure()
                lease = self.claim_next_job(
                    self.worker_token,
                    self.heartbeat,
                    self.clock(),
                    stop_requested,
                )
            if lease is None:
                return maintenance.did_work
            self._dispatch_claim(lease, stop_requested, mode)
            return True
        except StopRequested as stopped:
            current = ExportJob.objects.using(self.using).get(
                pk=stopped.lease.job_id,
            )
            if current.state in TERMINAL_STATES:
                if current.lease_uuid is not None:
                    self._release_terminal(stopped.lease)
            else:
                self.handoff_after_normal_stop(stopped.lease)
            return True
        except LeaseLost:
            raise
        except ExportError as error:
            current = ExportJob.objects.using(self.using).get(
                pk=error.lease.job_id,
            )
            if current.state in TERMINAL_STATES:
                if current.lease_uuid is not None:
                    self._release_terminal(error.lease)
            else:
                self.fail_job(error.lease, error.code)
            return True

    def run(self, stop_requested):
        root = worker_lock = None
        lost = False
        storage_failure_code = None
        try:
            root = open_export_root(
                settings.PINRY_EXPORT_ROOT,
                os.getuid(),
                os.getgid(),
            )
            try:
                open_staging_directory(root).close()
            except ExportStorageError:
                create_private_directory(
                    root,
                    ".staging",
                    root.uid,
                    root.gid,
                ).close()
            worker_lock = ExportWorkerLock.acquire(root, root.uid, root.gid)
            self.acquire_worker_lease(self.clock())
            self.heartbeat = self.heartbeat_factory(
                self.worker_token,
                using=self.using,
                clock=self.clock,
                monotonic=self.monotonic,
                sleeper=self.sleeper,
            )
            self.heartbeat.start()
            while not stop_requested():
                close_old_connections()
                if self.heartbeat.lost is not None:
                    raise self.heartbeat.lost
                if self.heartbeat.fatal is not None:
                    raise _HeartbeatFailed()
                did_work = self.run_once(stop_requested)
                if not did_work:
                    self.sleeper(IDLE_WAIT_SECONDS)
            return 0
        except LeaseLost:
            lost = True
            return 1
        except _HeartbeatFailed:
            lost = True
            return 1
        except DatabaseFenceError:
            lost = True
            return 1
        except ExportStorageError as error:
            if error.code != "export_worker_unavailable":
                storage_failure_code = error.code
            return 1
        finally:
            if self.heartbeat is not None:
                self.heartbeat.stop_accepting_ticks()
                self.heartbeat.join()
                if self.heartbeat.lost is not None:
                    lost = True
                if self.heartbeat.fatal is not None:
                    lost = True
            if storage_failure_code is not None:
                self._record_storage_failure(storage_failure_code)
            if self.worker_token is not None and not lost:
                try:
                    release_worker_lease(
                        self.worker_token,
                        now=self.clock(),
                        using=self.using,
                        monotonic=self.monotonic,
                        sleeper=self.sleeper,
                    )
                except LeaseLost:
                    pass
            if worker_lock is not None:
                worker_lock.close()
            if root is not None:
                root.close()
            close_old_connections()

    def _record_storage_failure(self, code):
        if code != "export_storage_unsafe":
            return
        now = self.clock()

        def record():
            with _worker_write_fence(self.using, self.monotonic):
                values = {
                    "health_state": "failed",
                    "error_code": "export_storage_unsafe",
                    "heartbeat_at": now,
                    "lease_uuid": None,
                    "lease_expires_at": None,
                }
                if self.worker_token is not None:
                    ExportWorkerLease.objects.using(self.using).filter(
                        pk=1,
                        generation=self.worker_token.worker_generation,
                        lease_uuid=self.worker_token.worker_lease_uuid,
                    ).update(**values)
                    return
                updated = ExportWorkerLease.objects.using(self.using).filter(
                    pk=1,
                    lease_uuid__isnull=True,
                ).update(**values)
                if updated == 0 and not ExportWorkerLease.objects.using(
                    self.using,
                ).filter(pk=1).exists():
                    try:
                        ExportWorkerLease.objects.using(self.using).create(
                            pk=1,
                            **values
                        )
                    except IntegrityError:
                        raise DatabaseFenceBusy() from None

        try:
            _retry_database(
                record,
                monotonic=self.monotonic,
                sleeper=self.sleeper,
            )
        except (DatabaseError, DatabaseFenceError):
            pass


__all__ = (
    "DATABASE_FENCE_MAX_SECONDS",
    "ExportWorker",
    "HEARTBEAT_INTERVAL_SECONDS",
    "IDLE_WAIT_SECONDS",
    "LeaseHeartbeat",
    "MaintenanceOutcome",
    "RecoveryClaimOutcome",
    "RequeueOutcome",
    "WORKER_HEALTH_STALE_SECONDS",
    "WORKER_LEASE_SECONDS",
    "_fail_locked",
    "acquire_worker_lease",
    "claim_complete_maintenance",
    "claim_next_job",
    "claim_retiring_recovery",
    "claim_verifying_recovery",
    "cleanup_expired_and_stale",
    "fail_job",
    "handoff_after_normal_stop",
    "release_job_lease",
    "release_worker_lease",
    "requeue_expired_nonverifying_jobs",
)
