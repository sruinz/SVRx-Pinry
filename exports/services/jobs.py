from dataclasses import dataclass
from datetime import timedelta
import os
import time

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import DatabaseError, IntegrityError
from django.db.models import Q
from django.utils import timezone
from taggit.models import Tag, TaggedItem

from core.models import Board, MediaAsset, Pin
from core.services.database_fence import (
    DatabaseFenceBusy,
    DatabaseFenceDeadline,
    DatabaseFenceError,
    database_write_fence,
)
from django_images.models import Image
from exports.contracts import ACTIVE_STATES
from exports.models import (
    ExportAttempt,
    ExportAttemptFile,
    ExportJob,
    ExportSlot,
    ExportTarget,
    ExportWorkerLease,
)
from exports.services.file_ops import (
    ExportStorageError,
    observe_available_space,
    open_export_root,
)
from exports.services.targeting import (
    ExportRequestError,
    TargetIdentityChanged,
    TargetingService,
)


User = get_user_model()


@dataclass(frozen=True)
class WorkerHealthSnapshot(object):
    health_state: str
    heartbeat_at: object
    error_code: object


class WorkerHealthService(object):
    @classmethod
    def snapshot(cls, now):
        del now
        try:
            lease = ExportWorkerLease.objects.get(pk=1)
        except ExportWorkerLease.DoesNotExist:
            return WorkerHealthSnapshot("stopped", None, None)
        return WorkerHealthSnapshot(
            lease.health_state,
            lease.heartbeat_at,
            lease.error_code,
        )

    @classmethod
    def require_available(cls, now):
        snapshot = cls.snapshot(now)
        if (
            snapshot.health_state == "ready"
            and snapshot.heartbeat_at is not None
            and snapshot.heartbeat_at > now - timedelta(seconds=15)
        ):
            return snapshot
        code = (
            "export_storage_unsafe"
            if snapshot.error_code == "export_storage_unsafe"
            else "export_worker_unavailable"
        )
        raise ExportRequestError(code, 503)


def _default_available_space_observer():
    root = open_export_root(
        settings.PINRY_EXPORT_ROOT,
        os.getuid(),
        os.getgid(),
    )
    try:
        return observe_available_space(root.descriptor)
    finally:
        root.close()


class JobService(object):
    FENCE_MODELS = (
        User,
        Board,
        Board.pins.through,
        Pin,
        Image,
        MediaAsset,
        Tag,
        TaggedItem,
        ExportJob,
        ExportSlot,
        ExportTarget,
    )
    IDENTITY_RETRY_LIMIT = 3

    def __init__(
        self,
        targeting=None,
        available_space_observer=None,
        monotonic=None,
        sleeper=None,
        health_clock=None,
    ):
        self.targeting = targeting or TargetingService()
        self.available_space_observer = (
            _default_available_space_observer
            if available_space_observer is None
            else available_space_observer
        )
        self.monotonic = time.monotonic if monotonic is None else monotonic
        self.sleeper = time.sleep if sleeper is None else sleeper
        self.health_clock = (
            timezone.now if health_clock is None else health_clock
        )

    def _delete_retired_rows_if_cleanup_complete(self, user, checkpoint):
        candidates = (
            ExportJob.objects.filter(
                owner_id=user.pk,
                state__in=("failed", "expired"),
                staging_cleanup_state="cleaned",
                ready_cleanup_state__in=("absent", "cleaned"),
                snapshot_relative_path__isnull=True,
                candidate_snapshot_relative_path__isnull=True,
                ready_relative_path__isnull=True,
            )
            .order_by("created_at", "id")
        )
        for job in candidates:
            if job.blobs.exclude(cleanup_state="cleaned").exists():
                continue
            if ExportAttempt.objects.filter(job=job).exclude(
                state="cleaned"
            ).exists():
                continue
            if ExportAttemptFile.objects.filter(
                attempt__job=job
            ).exclude(state="cleaned").exists():
                continue
            job.delete()
            checkpoint()

    def _owner_safety(self, user):
        owner_jobs = ExportJob.objects.filter(owner_id=user.pk)
        if owner_jobs.filter(
            Q(staging_cleanup_state="blocked")
            | Q(ready_cleanup_state="blocked")
        ).exists():
            raise ExportRequestError("export_storage_unsafe", 503)
        if owner_jobs.count() >= 3:
            raise ExportRequestError("export_storage_unsafe", 503)
        if owner_jobs.filter(state__in=ACTIVE_STATES).exists():
            raise ExportRequestError("active_export_exists", 409)

    def _commit_locked(
        self,
        user,
        request_data,
        now,
        observed,
        available_bytes,
        checkpoint,
    ):
        WorkerHealthService.require_available(self.health_clock())
        if not User.objects.filter(pk=user.pk).exists():
            raise ExportRequestError("invalid_target", 404)
        self._delete_retired_rows_if_cleanup_complete(user, checkpoint)
        self._owner_safety(user)
        captured = self.targeting.capture_locked(
            user,
            request_data,
            now,
            checkpoint,
            observed_sizes=observed.sizes,
            expected_snapshot=observed.snapshot,
        )
        if not captured.pin_ids:
            raise ExportRequestError("invalid_target", 400)
        if captured.space_budget.required_bytes > available_bytes:
            raise ExportRequestError("insufficient_space", 503)
        ExportSlot.objects.get_or_create(owner_id=user.pk)
        job = ExportJob.objects.create(
            owner_id=user.pk,
            **captured.job_fields
        )
        claimed = ExportSlot.objects.filter(
            owner_id=user.pk,
            current_job__isnull=True,
        ).update(current_job=job, updated_at=now)
        if claimed != 1:
            raise ExportRequestError("active_export_exists", 409)
        ExportTarget.objects.bulk_create(tuple(
            ExportTarget(
                job=job,
                position=position,
                pin_id=identity.pin_id,
                pin_owner_id_snapshot=identity.owner_id,
                pin_published_at_snapshot=identity.published,
            )
            for position, identity in enumerate(captured.identities)
        ))
        checkpoint()
        return job

    def _occupied_slot(self, user):
        try:
            return ExportSlot.objects.filter(
                owner_id=user.pk,
                current_job__isnull=False,
            ).exists()
        except DatabaseError:
            return None

    def create(self, user, request_data, now):
        WorkerHealthService.require_available(self.health_clock())
        for _attempt in range(self.IDENTITY_RETRY_LIMIT):
            snapshot = self.targeting.snapshot_for_observation(
                user,
                request_data,
            )
            observed = self.targeting.observe_snapshot(snapshot)
            try:
                available_bytes = self.available_space_observer()
            except ExportStorageError as error:
                raise ExportRequestError(error.code, 503) from None
            if type(available_bytes) is not int or available_bytes < 0:
                raise ExportRequestError("export_storage_unsafe", 503)
            deadline = DatabaseFenceDeadline(self.monotonic)
            while True:
                try:
                    with database_write_fence(
                        using="default",
                        models=self.FENCE_MODELS,
                    ):
                        job = self._commit_locked(
                            user,
                            request_data,
                            now,
                            observed,
                            available_bytes,
                            deadline.checkpoint,
                        )
                        deadline.checkpoint()
                    return job
                except TargetIdentityChanged:
                    break
                except ExportRequestError:
                    raise
                except (DatabaseFenceError, DatabaseError) as error:
                    occupied = self._occupied_slot(user)
                    if occupied is True:
                        raise ExportRequestError(
                            "active_export_exists", 409
                        ) from None
                    if type(error) is DatabaseFenceBusy:
                        try:
                            deadline.checkpoint()
                        except DatabaseFenceDeadline:
                            raise ExportRequestError(
                                "export_temporarily_unavailable", 503
                            ) from None
                        self.sleeper(0.005)
                        continue
                    if isinstance(error, IntegrityError):
                        raise ExportRequestError(
                            "export_temporarily_unavailable", 503
                        ) from None
                    raise ExportRequestError(
                        "export_temporarily_unavailable", 503
                    ) from None
        raise ExportRequestError("export_temporarily_unavailable", 503)


__all__ = (
    "ExportRequestError",
    "JobService",
    "WorkerHealthService",
    "WorkerHealthSnapshot",
)
