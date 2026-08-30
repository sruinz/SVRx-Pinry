import uuid
from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import transaction


ACTIVE_STATES = frozenset(("queued", "snapshotting", "archiving", "verifying"))
TERMINAL_STATES = frozenset(("complete", "failed", "expired"))
JOB_STATES = tuple(sorted(ACTIVE_STATES | TERMINAL_STATES))
READY_FORBIDDEN_STATES = ("queued", "snapshotting", "archiving", "verifying", "failed")
SCOPE_VALUES = ("pins", "board")
ERROR_CLASSES = ("retryable", "operator_action_required", "fatal")
FILE_STATES = ("writing", "closed")
CLEANUP_STATES = ("pending", "cleaned", "blocked")
READY_CLEANUP_STATES = ("absent", "retained", "pending", "cleaned", "blocked")
ATTEMPT_STATES = ("writing", "closed", "verifying", "published", "retiring", "cleaned")
ATTEMPT_FILE_KINDS = ("archive", "quarantine")
ATTEMPT_FILE_STATES = (
    "writing", "closed", "verifying", "publishing", "ready_candidate",
    "published", "retiring", "cleaned",
)
RECEIPT_LEVELS = ("open", "full")
INCLUSION_STATES = ("included", "excluded")
WORKER_HEALTH_STATES = ("starting", "ready", "failed", "stopped")

ERROR_CONTRACTS = {
    "invalid_target": ("fatal", False, "내보낼 대상을 확인할 수 없습니다."),
    "active_export_exists": ("retryable", True, "이미 진행 중인 내보내기가 있습니다."),
    "export_not_ready": ("retryable", True, "아직 내보내기가 완료되지 않았습니다."),
    "source_missing": ("fatal", False, "원본 파일을 찾을 수 없습니다."),
    "source_changed": ("retryable", True, "원본이 변경되어 내보내기를 중단했습니다."),
    "source_unsafe": ("operator_action_required", False, "원본 저장 상태를 확인해야 합니다."),
    "snapshot_failed": ("retryable", True, "원본 스냅숏을 만들지 못했습니다."),
    "insufficient_space": ("operator_action_required", True, "내보내기에 필요한 저장 공간이 부족합니다."),
    "archive_failed": ("retryable", True, "ZIP 파일을 만들지 못했습니다."),
    "permission_changed": ("fatal", False, "현재 권한으로 다시 내보내세요."),
    "all_items_revoked": ("fatal", False, "내보낼 수 있는 Pin이 남지 않았습니다."),
    "worker_repeated_failure": ("operator_action_required", True, "내보내기 작업자를 확인해야 합니다."),
    "export_worker_unavailable": ("retryable", True, "내보내기 작업자를 기다리고 있습니다."),
    "export_temporarily_unavailable": ("retryable", True, "잠시 후 다시 시도하세요."),
    "export_storage_unsafe": ("operator_action_required", False, "내보내기 저장소를 안전하게 사용할 수 없습니다."),
    "export_expired": ("fatal", False, "다운로드 기간이 만료됐습니다."),
}
JOB_ERROR_CODES = (
    "source_missing", "source_changed", "source_unsafe", "snapshot_failed",
    "insufficient_space", "archive_failed", "permission_changed",
    "all_items_revoked", "worker_repeated_failure", "export_storage_unsafe",
)


@dataclass(frozen=True)
class WorkerLeaseToken(object):
    worker_generation: int
    worker_lease_uuid: uuid.UUID


@dataclass(frozen=True)
class LeaseToken(object):
    worker_generation: int
    worker_lease_uuid: uuid.UUID
    job_id: uuid.UUID
    job_lease_uuid: uuid.UUID
    attempt_generation: int

    def job_fence(self):
        return {
            "pk": self.job_id,
            "worker_generation": self.worker_generation,
            "lease_uuid": self.job_lease_uuid,
            "attempt_generation": self.attempt_generation,
        }


class LeaseLost(Exception):
    pass


class StopRequested(Exception):
    def __init__(self, lease):
        self.lease = lease
        super(StopRequested, self).__init__()


class ExportError(Exception):
    def __init__(self, code, lease):
        if code not in ERROR_CONTRACTS:
            raise ValueError("허용되지 않은 내보내기 오류 코드입니다.")
        self.code = code
        self.error_class, self.retryable, self.message = ERROR_CONTRACTS[code]
        self.lease = lease
        super(ExportError, self).__init__(self.message)


def _current_worker_lease(token, using, lock):
    from .models import ExportWorkerLease

    manager = ExportWorkerLease.objects.using(using)
    if lock:
        manager = manager.select_for_update()
    try:
        worker = manager.get(pk=1)
    except ExportWorkerLease.DoesNotExist:
        raise LeaseLost()
    if (worker.generation != token.worker_generation or
            worker.lease_uuid != token.worker_lease_uuid):
        raise LeaseLost()
    return worker


def lock_current_worker_lease(worker_token, using="default"):
    return _current_worker_lease(worker_token, using, lock=True)


def lock_current_lease(token, using="default"):
    from .models import ExportJob

    _current_worker_lease(token, using, lock=True)
    try:
        job = ExportJob.objects.using(using).select_for_update().get(
            **token.job_fence()
        )
    except ExportJob.DoesNotExist:
        raise LeaseLost()
    return job


def require_current_lease(token, using="default"):
    with transaction.atomic(using=using):
        return lock_current_lease(token, using=using)


def _iso(value):
    return value.isoformat().replace("+00:00", "Z") if value else None


def export_status(job, now, worker_heartbeat_at=None):
    state = job.state
    expired = state == "complete" and job.expires_at and job.expires_at <= now
    if expired:
        state = "expired"
    labels = {
        "queued": "대기 중", "snapshotting": "스냅숏 준비", "archiving": "압축 중",
        "verifying": "검증 중", "complete": "완료", "failed": "실패", "expired": "만료됨",
    }
    phase_label = labels[state]
    if (state == "queued" and worker_heartbeat_at and
            (not job.heartbeat_at or job.heartbeat_at < worker_heartbeat_at)):
        phase_label = "작업자 대기 중"
    counters = {
        "requested_total": job.requested_total,
        "target_total": job.target_total,
        "snapshot_done": job.snapshot_done,
        "archive_total": job.archive_total,
        "archive_done": job.archive_done,
        "included_total": job.included_total,
        "excluded_total": job.excluded_total,
        "bytes_total": job.bytes_total,
        "bytes_done": job.bytes_done,
    }
    phase_percent = None
    overall_percent = None
    if state == "complete":
        phase_percent = overall_percent = 100.0
    elif state == "snapshotting" and job.target_total:
        phase_percent = round(job.snapshot_done * 100.0 / job.target_total, 1)
        overall_percent = round(phase_percent * 0.2, 1)
    elif state == "archiving" and job.archive_total:
        phase_percent = round(job.archive_done * 100.0 / job.archive_total, 1)
        overall_percent = round(20 + phase_percent * 0.75, 1)
    elif state == "verifying":
        phase_percent, overall_percent = 50.0, 97.0
    error = None
    if job.error_code:
        error = {
            "code": job.error_code,
            "class": job.error_class,
            "retryable": job.error_retryable,
            "message": ERROR_CONTRACTS[job.error_code][2],
        }
    return {
        "schema_version": 1,
        "id": str(job.pk),
        "state": state,
        "phase_label": phase_label,
        "scope": job.scope,
        "phase_percent": phase_percent,
        "overall_percent": overall_percent,
        "counters": counters,
        "created_at": _iso(job.created_at),
        "snapshot_at": _iso(job.snapshot_at),
        "heartbeat_at": _iso(job.heartbeat_at),
        "completed_at": _iso(job.completed_at),
        "expires_at": _iso(job.expires_at),
        "resume_count": job.resume_count,
        "error": error,
        "download_url": (
            "/api/v2/exports/{}/download/".format(job.pk)
            if state == "complete" else None
        ),
    }
