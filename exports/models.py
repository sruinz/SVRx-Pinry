import re
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import F, Q

from .contracts import (
    ACTIVE_STATES, ATTEMPT_FILE_KINDS, ATTEMPT_FILE_STATES, ATTEMPT_STATES,
    CLEANUP_STATES, ERROR_CLASSES, ERROR_CONTRACTS, FILE_STATES,
    INCLUSION_STATES, JOB_ERROR_CODES, JOB_STATES, READY_CLEANUP_STATES,
    READY_FORBIDDEN_STATES, RECEIPT_LEVELS, SCOPE_VALUES, WORKER_HEALTH_STATES,
)


def _choices(values):
    return tuple((value, value) for value in values)


def _nonnegative(default=None, null=False):
    kwargs = {"validators": [MinValueValidator(0)], "null": null, "blank": null}
    if default is not None:
        kwargs["default"] = default
    return models.BigIntegerField(**kwargs)


def _all_or_none(fields):
    complete = Q(**{"{}__isnull".format(field): False for field in fields})
    empty = Q(**{"{}__isnull".format(field): True for field in fields})
    return complete | empty


def _is_lower_sha(value):
    return value is None or bool(re.match(r"^[0-9a-f]{64}$", value))


def _nullable_sha_check(field):
    return Q(**{"{}__isnull".format(field): True}) | Q(
        **{"{}__regex".format(field): r"^[0-9a-f]{64}$"}
    )


def _validate_all_or_none(instance, fields, name):
    values = [getattr(instance, field) for field in fields]
    if any(value is None for value in values) and any(value is not None for value in values):
        raise ValidationError({name: "영수증은 모두 기록하거나 모두 비워야 합니다."})


def _nonnegative_check(fields):
    condition = Q()
    for field in fields:
        condition &= Q(**{"{}__gte".format(field): 0})
    return condition


def _job_error_contract_check():
    condition = Q(pk__isnull=True)
    for code in JOB_ERROR_CODES:
        error_class, retryable, _ = ERROR_CONTRACTS[code]
        condition |= Q(
            error_code=code,
            error_class=error_class,
            error_retryable=retryable,
        )
    return Q(
        error_code__isnull=False,
        error_class__isnull=False,
        error_retryable__isnull=False,
    ) & condition


class ExportJob(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    scope = models.CharField(max_length=8, choices=_choices(SCOPE_VALUES))
    state = models.CharField(max_length=16, default="queued", db_index=True, choices=_choices(JOB_STATES))
    board_id_snapshot = models.PositiveIntegerField(null=True, blank=True)
    board_name_snapshot = models.CharField(max_length=128, null=True, blank=True)
    board_private_snapshot = models.NullBooleanField(blank=True)
    board_owner_username_snapshot = models.CharField(max_length=150, null=True, blank=True)
    requested_total = models.PositiveIntegerField(default=0)
    target_total = models.PositiveIntegerField(default=0)
    snapshot_done = models.PositiveIntegerField(default=0)
    archive_total = models.PositiveIntegerField(default=0)
    archive_done = models.PositiveIntegerField(default=0)
    included_total = models.PositiveIntegerField(default=0)
    excluded_total = models.PositiveIntegerField(default=0)
    excluded_not_visible_total = models.PositiveIntegerField(default=0)
    excluded_permission_revoked_total = models.PositiveIntegerField(default=0)
    bytes_total = _nonnegative(default=0)
    bytes_done = _nonnegative(default=0)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    snapshot_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    progress_at = models.DateTimeField(null=True, blank=True)
    staging_cleanup_state = models.CharField(max_length=16, default="pending", choices=_choices(CLEANUP_STATES))
    ready_cleanup_state = models.CharField(max_length=16, default="absent", choices=_choices(READY_CLEANUP_STATES))
    snapshot_generation = models.UUIDField(null=True, blank=True)
    snapshot_relative_path = models.CharField(max_length=512, null=True, blank=True)
    snapshot_dir_dev = _nonnegative(null=True)
    snapshot_dir_ino = _nonnegative(null=True)
    snapshot_dir_uid = models.PositiveIntegerField(null=True, blank=True)
    snapshot_dir_gid = models.PositiveIntegerField(null=True, blank=True)
    snapshot_dir_mode = models.PositiveIntegerField(null=True, blank=True)
    candidate_snapshot_generation = models.UUIDField(null=True, blank=True)
    candidate_snapshot_relative_path = models.CharField(max_length=512, null=True, blank=True)
    candidate_snapshot_dir_dev = _nonnegative(null=True)
    candidate_snapshot_dir_ino = _nonnegative(null=True)
    candidate_snapshot_dir_uid = models.PositiveIntegerField(null=True, blank=True)
    candidate_snapshot_dir_gid = models.PositiveIntegerField(null=True, blank=True)
    candidate_snapshot_dir_mode = models.PositiveIntegerField(null=True, blank=True)
    lease_uuid = models.UUIDField(null=True, blank=True)
    worker_generation = _nonnegative(default=0)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    attempt_generation = _nonnegative(default=0)
    verifying_attempt_generation = _nonnegative(null=True)
    verifying_size = _nonnegative(null=True)
    verifying_sha256 = models.CharField(max_length=64, null=True, blank=True)
    verifying_completed_at = models.DateTimeField(null=True, blank=True)
    resume_count = models.PositiveSmallIntegerField(default=0)
    error_code = models.CharField(max_length=64, null=True, blank=True, choices=_choices(ERROR_CONTRACTS))
    error_class = models.CharField(max_length=32, null=True, blank=True, choices=_choices(ERROR_CLASSES))
    error_retryable = models.NullBooleanField(blank=True)
    ready_relative_path = models.CharField(max_length=512, null=True, blank=True)
    ready_display_name = models.CharField(max_length=255, null=True, blank=True)
    ready_size = _nonnegative(null=True)
    ready_sha256 = models.CharField(max_length=64, null=True, blank=True)
    ready_dev = _nonnegative(null=True)
    ready_ino = _nonnegative(null=True)
    ready_uid = models.PositiveIntegerField(null=True, blank=True)
    ready_gid = models.PositiveIntegerField(null=True, blank=True)
    ready_mode = models.PositiveIntegerField(null=True, blank=True)
    ready_nlink = models.PositiveIntegerField(null=True, blank=True)
    ready_mtime_ns = _nonnegative(null=True)
    ready_ctime_ns = _nonnegative(null=True)

    class Meta:
        indexes = (
            models.Index(fields=("state", "created_at", "id"), name="export_fifo_idx"),
            models.Index(fields=("owner", "created_at"), name="export_owner_idx"),
        )
        constraints = (
            models.CheckConstraint(check=Q(scope__in=SCOPE_VALUES), name="export_job_scope_valid"),
            models.CheckConstraint(check=Q(state__in=JOB_STATES), name="export_job_state_valid"),
            models.CheckConstraint(check=Q(snapshot_done__lte=F("target_total")), name="export_snapshot_total_valid"),
            models.CheckConstraint(check=Q(archive_done__lte=F("archive_total")), name="export_archive_total_valid"),
            models.CheckConstraint(check=Q(bytes_done__lte=F("bytes_total")), name="export_bytes_total_valid"),
            models.CheckConstraint(check=Q(included_total=F("requested_total") - F("excluded_total")), name="export_included_total_valid"),
            models.CheckConstraint(check=_nonnegative_check(("bytes_total", "bytes_done", "snapshot_dir_dev", "snapshot_dir_ino", "candidate_snapshot_dir_dev", "candidate_snapshot_dir_ino", "worker_generation", "attempt_generation", "verifying_attempt_generation", "verifying_size", "ready_size", "ready_dev", "ready_ino", "ready_mtime_ns", "ready_ctime_ns")), name="export_job_nonnegative"),
            models.CheckConstraint(check=Q(staging_cleanup_state__in=CLEANUP_STATES), name="export_staging_cleanup_valid"),
            models.CheckConstraint(check=Q(ready_cleanup_state__in=READY_CLEANUP_STATES), name="export_ready_cleanup_valid"),
            models.CheckConstraint(check=Q(error_code__in=ERROR_CONTRACTS) | Q(error_code__isnull=True), name="export_error_code_valid"),
            models.CheckConstraint(check=Q(error_class__in=ERROR_CLASSES) | Q(error_class__isnull=True), name="export_error_class_valid"),
            models.CheckConstraint(check=_all_or_none(("snapshot_generation", "snapshot_relative_path", "snapshot_dir_dev", "snapshot_dir_ino", "snapshot_dir_uid", "snapshot_dir_gid", "snapshot_dir_mode")), name="export_snapshot_receipt_full"),
            models.CheckConstraint(check=(
                _all_or_none(("candidate_snapshot_generation", "candidate_snapshot_relative_path", "candidate_snapshot_dir_dev", "candidate_snapshot_dir_ino", "candidate_snapshot_dir_uid", "candidate_snapshot_dir_gid", "candidate_snapshot_dir_mode")) |
                (Q(candidate_snapshot_generation__isnull=False, candidate_snapshot_relative_path__isnull=False) & Q(candidate_snapshot_dir_dev__isnull=True, candidate_snapshot_dir_ino__isnull=True, candidate_snapshot_dir_uid__isnull=True, candidate_snapshot_dir_gid__isnull=True, candidate_snapshot_dir_mode__isnull=True))
            ), name="export_candidate_snapshot_valid"),
            models.CheckConstraint(check=_all_or_none(("verifying_attempt_generation", "verifying_size", "verifying_sha256", "verifying_completed_at")), name="export_verifying_receipt_full"),
            models.CheckConstraint(check=_all_or_none(("ready_relative_path", "ready_display_name", "ready_size", "ready_sha256", "ready_dev", "ready_ino", "ready_uid", "ready_gid", "ready_mode", "ready_nlink", "ready_mtime_ns", "ready_ctime_ns")), name="export_ready_receipt_full"),
            models.CheckConstraint(check=_nullable_sha_check("verifying_sha256"), name="export_verifying_sha_valid"),
            models.CheckConstraint(check=_nullable_sha_check("ready_sha256"), name="export_ready_sha_valid"),
            models.CheckConstraint(check=(~Q(state="complete") | (Q(ready_cleanup_state="retained", completed_at__isnull=False, expires_at__isnull=False, ready_relative_path__isnull=False) & Q(error_code__isnull=True, error_class__isnull=True, error_retryable__isnull=True))), name="export_complete_state_valid"),
            models.CheckConstraint(check=(~Q(state="failed") | _job_error_contract_check()), name="export_failed_state_valid"),
            models.CheckConstraint(check=~Q(state__in=READY_FORBIDDEN_STATES) | Q(ready_relative_path__isnull=True), name="export_active_ready_empty"),
            models.CheckConstraint(check=~Q(state="expired") | (Q(ready_cleanup_state__in=("pending", "blocked"), ready_relative_path__isnull=False) | Q(ready_cleanup_state="cleaned", ready_relative_path__isnull=True)), name="export_expired_ready_valid"),
        )

    def clean(self):
        super(ExportJob, self).clean()
        snapshot_fields = ("snapshot_generation", "snapshot_relative_path", "snapshot_dir_dev", "snapshot_dir_ino", "snapshot_dir_uid", "snapshot_dir_gid", "snapshot_dir_mode")
        candidate_fields = ("candidate_snapshot_generation", "candidate_snapshot_relative_path", "candidate_snapshot_dir_dev", "candidate_snapshot_dir_ino", "candidate_snapshot_dir_uid", "candidate_snapshot_dir_gid", "candidate_snapshot_dir_mode")
        verifying_fields = ("verifying_attempt_generation", "verifying_size", "verifying_sha256", "verifying_completed_at")
        ready_fields = ("ready_relative_path", "ready_display_name", "ready_size", "ready_sha256", "ready_dev", "ready_ino", "ready_uid", "ready_gid", "ready_mode", "ready_nlink", "ready_mtime_ns", "ready_ctime_ns")
        _validate_all_or_none(self, snapshot_fields, "snapshot_receipt")
        candidate_values = [getattr(self, field) for field in candidate_fields]
        candidate_receipt = candidate_values[2:]
        if any(value is None for value in candidate_values[:2]) and any(value is not None for value in candidate_values):
            raise ValidationError("후보 스냅숏 세대와 경로는 함께 기록해야 합니다.")
        if all(value is not None for value in candidate_values[:2]):
            _validate_all_or_none(self, candidate_receipt, "candidate_snapshot_receipt")
        elif any(value is not None for value in candidate_receipt):
            raise ValidationError("후보 스냅숏 영수증에는 세대와 경로가 필요합니다.")
        _validate_all_or_none(self, verifying_fields, "verifying_receipt")
        _validate_all_or_none(self, ready_fields, "ready_receipt")
        if not _is_lower_sha(self.ready_sha256) or not _is_lower_sha(self.verifying_sha256):
            raise ValidationError("SHA-256은 소문자 64자리 16진수여야 합니다.")
        if self.snapshot_done > self.target_total or self.archive_done > self.archive_total or self.bytes_done > self.bytes_total:
            raise ValidationError("진행 수량이 전체 수량을 초과할 수 없습니다.")
        if self.included_total + self.excluded_total != self.requested_total:
            raise ValidationError("포함 및 제외 수량은 요청 수량과 같아야 합니다.")
        if self.state == "complete":
            if self.ready_cleanup_state != "retained" or any(getattr(self, name) is None for name in ready_fields):
                raise ValidationError("완료 작업에는 보존된 완전한 완료 파일 영수증이 필요합니다.")
            if any(value is not None for value in (self.error_code, self.error_class, self.error_retryable)):
                raise ValidationError("완료 작업에는 오류를 기록할 수 없습니다.")
        if self.state == "failed":
            if self.error_code not in JOB_ERROR_CODES or self.error_code is None:
                raise ValidationError("실패 작업에는 저장 가능한 오류 코드가 필요합니다.")
            error_class, retryable, _ = ERROR_CONTRACTS[self.error_code]
            if self.error_class != error_class or self.error_retryable != retryable:
                raise ValidationError("실패 오류 영수증이 계약과 다릅니다.")
        if self.state in ACTIVE_STATES | frozenset(("failed",)) and any(getattr(self, name) is not None for name in ready_fields):
            raise ValidationError("진행 또는 실패 작업에는 완료 파일 영수증을 둘 수 없습니다.")
        if self.state == "expired":
            if self.ready_cleanup_state in ("pending", "blocked") and any(getattr(self, name) is None for name in ready_fields):
                raise ValidationError("정리 대기 만료 작업에는 완료 파일 영수증이 필요합니다.")
            if self.ready_cleanup_state == "cleaned" and any(getattr(self, name) is not None for name in ready_fields):
                raise ValidationError("정리 완료 만료 작업에는 완료 파일 영수증을 둘 수 없습니다.")
            if self.ready_cleanup_state not in ("pending", "blocked", "cleaned"):
                raise ValidationError("만료 작업의 완료 파일 정리 상태가 올바르지 않습니다.")


class ExportTarget(models.Model):
    job = models.ForeignKey(ExportJob, related_name="targets", on_delete=models.CASCADE)
    position = models.PositiveIntegerField()
    pin_id = models.PositiveIntegerField()

    class Meta:
        unique_together = (("job", "position"), ("job", "pin_id"))
        ordering = ("position",)


class ExportBlob(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey(ExportJob, related_name="blobs", on_delete=models.CASCADE)
    snapshot_generation = models.UUIDField()
    source_media_asset_id = models.PositiveIntegerField()
    source_image_id = models.PositiveIntegerField()
    source_relative_path = models.CharField(max_length=512)
    expected_sha256 = models.CharField(max_length=64, null=True, blank=True)
    source_dev = _nonnegative(null=True)
    source_ino = _nonnegative(null=True)
    source_size = _nonnegative(null=True)
    source_mtime_ns = _nonnegative(null=True)
    source_ctime_ns = _nonnegative(null=True)
    file_state = models.CharField(max_length=16, default="writing", choices=_choices(FILE_STATES))
    part_relative_path = models.CharField(max_length=512, null=True, blank=True)
    snapshot_relative_path = models.CharField(max_length=512, null=True, blank=True)
    capture_method = models.CharField(max_length=16, null=True, blank=True)
    mime_type = models.CharField(max_length=64, null=True, blank=True)
    size = _nonnegative(null=True)
    receipt_dev = _nonnegative(null=True)
    receipt_ino = _nonnegative(null=True)
    receipt_uid = models.PositiveIntegerField(null=True, blank=True)
    receipt_gid = models.PositiveIntegerField(null=True, blank=True)
    receipt_mode = models.PositiveIntegerField(null=True, blank=True)
    receipt_nlink = models.PositiveIntegerField(null=True, blank=True)
    receipt_mtime_ns = _nonnegative(null=True)
    receipt_ctime_ns = _nonnegative(null=True)
    receipt_sha256 = models.CharField(max_length=64, null=True, blank=True)
    confirmed = models.BooleanField(default=False)
    cleanup_state = models.CharField(max_length=16, default="pending", choices=_choices(CLEANUP_STATES))

    class Meta:
        unique_together = (("job", "snapshot_generation", "source_media_asset_id"),)
        constraints = (
            models.CheckConstraint(check=Q(file_state__in=FILE_STATES), name="export_blob_state_valid"),
            models.CheckConstraint(check=Q(cleanup_state__in=CLEANUP_STATES), name="export_blob_cleanup_valid"),
            models.CheckConstraint(check=_nonnegative_check(("source_dev", "source_ino", "source_size", "source_mtime_ns", "source_ctime_ns", "size", "receipt_dev", "receipt_ino", "receipt_mtime_ns", "receipt_ctime_ns")), name="export_blob_nonnegative"),
            models.CheckConstraint(check=_all_or_none(("receipt_dev", "receipt_ino", "receipt_uid", "receipt_gid", "receipt_mode", "receipt_nlink", "size", "receipt_mtime_ns", "receipt_ctime_ns")), name="export_blob_receipt_full"),
            models.CheckConstraint(check=~Q(file_state="closed") | Q(receipt_dev__isnull=False), name="export_blob_closed_receipt_valid"),
            models.CheckConstraint(check=_nullable_sha_check("expected_sha256"), name="export_blob_expected_sha_valid"),
            models.CheckConstraint(check=_nullable_sha_check("receipt_sha256"), name="export_blob_receipt_sha_valid"),
        )

    def clean(self):
        super(ExportBlob, self).clean()
        fields = ("receipt_dev", "receipt_ino", "receipt_uid", "receipt_gid", "receipt_mode", "receipt_nlink", "size", "receipt_mtime_ns", "receipt_ctime_ns")
        _validate_all_or_none(self, fields, "receipt")
        if self.file_state == "closed" and any(getattr(self, name) is None for name in fields):
            raise ValidationError("닫힌 스냅숏에는 완전한 파일 영수증이 필요합니다.")
        if not _is_lower_sha(self.expected_sha256) or not _is_lower_sha(self.receipt_sha256):
            raise ValidationError("SHA-256은 소문자 64자리 16진수여야 합니다.")


class ExportItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey(ExportJob, related_name="items", on_delete=models.CASCADE)
    target_position = models.PositiveIntegerField()
    snapshot_generation = models.UUIDField()
    blob = models.ForeignKey(ExportBlob, related_name="items", on_delete=models.CASCADE)
    pin_id = models.PositiveIntegerField()
    pin_owner_id = models.PositiveIntegerField()
    owner_username = models.CharField(max_length=150)
    is_public = models.BooleanField()
    published_at = models.DateTimeField()
    description = models.TextField(null=True, blank=True)
    tags_json = models.TextField(default="[]")
    source_url = models.CharField(max_length=2048, null=True, blank=True)
    source_url_redacted = models.BooleanField(default=False)
    referer_url = models.CharField(max_length=2048, null=True, blank=True)
    referer_url_redacted = models.BooleanField(default=False)
    original_filename = models.CharField(max_length=255)
    archive_image_path = models.CharField(max_length=512, null=True, blank=True)
    archive_xmp_path = models.CharField(max_length=512, null=True, blank=True)
    inclusion_state = models.CharField(max_length=16, default="included", choices=_choices(INCLUSION_STATES))
    exclusion_reason = models.CharField(max_length=32, null=True, blank=True)

    class Meta:
        unique_together = (("job", "target_position"), ("job", "pin_id"))
        ordering = ("target_position",)
        constraints = (models.CheckConstraint(check=Q(inclusion_state__in=INCLUSION_STATES), name="export_item_inclusion_valid"),)


class ExportSlot(models.Model):
    owner = models.OneToOneField(settings.AUTH_USER_MODEL, primary_key=True, on_delete=models.CASCADE)
    current_job = models.ForeignKey(ExportJob, null=True, blank=True, on_delete=models.SET_NULL)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        super(ExportSlot, self).clean()
        if self.current_job_id and self.current_job.owner_id != self.owner_id:
            raise ValidationError("현재 내보내기 작업은 슬롯 소유자와 같아야 합니다.")


class ExportWorkerLease(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    generation = _nonnegative(default=0)
    lease_uuid = models.UUIDField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    health_state = models.CharField(max_length=16, default="starting", choices=_choices(WORKER_HEALTH_STATES))
    error_code = models.CharField(max_length=64, null=True, blank=True, choices=_choices(ERROR_CONTRACTS))

    class Meta:
        constraints = (
            models.CheckConstraint(check=Q(id=1), name="export_worker_singleton"),
            models.CheckConstraint(check=Q(generation__gte=0), name="export_worker_generation_valid"),
            models.CheckConstraint(check=Q(health_state__in=WORKER_HEALTH_STATES), name="export_worker_health_valid"),
            models.CheckConstraint(check=Q(error_code__in=ERROR_CONTRACTS) | Q(error_code__isnull=True), name="export_worker_error_valid"),
        )


class ExportAttempt(models.Model):
    job = models.ForeignKey(ExportJob, related_name="attempts", on_delete=models.CASCADE)
    attempt_generation = _nonnegative()
    lease_uuid = models.UUIDField()
    state = models.CharField(max_length=16, choices=_choices(ATTEMPT_STATES))
    relative_path = models.CharField(max_length=512)
    dir_dev = _nonnegative(null=True)
    dir_ino = _nonnegative(null=True)
    dir_uid = models.PositiveIntegerField(null=True, blank=True)
    dir_gid = models.PositiveIntegerField(null=True, blank=True)
    dir_mode = models.PositiveIntegerField(null=True, blank=True)
    exported_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = (("job", "attempt_generation"),)
        constraints = (
            models.CheckConstraint(check=Q(state__in=ATTEMPT_STATES), name="export_attempt_state_valid"),
            models.CheckConstraint(check=_nonnegative_check(("attempt_generation", "dir_dev", "dir_ino")), name="export_attempt_nonnegative"),
            models.CheckConstraint(check=_all_or_none(("dir_dev", "dir_ino", "dir_uid", "dir_gid", "dir_mode")), name="export_attempt_dir_receipt_full"),
            models.CheckConstraint(check=~Q(state__in=("closed", "verifying", "published")) | Q(exported_at__isnull=False), name="export_attempt_exported_at_valid"),
            models.CheckConstraint(check=~Q(state="cleaned") | Q(dir_dev__isnull=False), name="export_attempt_cleaned_receipt_valid"),
        )

    def clean(self):
        super(ExportAttempt, self).clean()
        _validate_all_or_none(self, ("dir_dev", "dir_ino", "dir_uid", "dir_gid", "dir_mode"), "directory_receipt")
        if self.state == "cleaned" and self.dir_dev is None:
            raise ValidationError("정리된 시도에는 디렉터리 영수증이 필요합니다.")
        if self.state in ("closed", "verifying", "published") and self.exported_at is None:
            raise ValidationError("닫힌 시도에는 내보낸 시각이 필요합니다.")


class ExportAttemptFile(models.Model):
    attempt = models.ForeignKey(ExportAttempt, related_name="files", on_delete=models.CASCADE)
    kind = models.CharField(max_length=16, choices=_choices(ATTEMPT_FILE_KINDS))
    state = models.CharField(max_length=16, choices=_choices(ATTEMPT_FILE_STATES))
    receipt_level = models.CharField(max_length=8, default="open", choices=_choices(RECEIPT_LEVELS))
    relative_path = models.CharField(max_length=512)
    intent_relative_path = models.CharField(max_length=512, null=True, blank=True)
    receipt_dev = _nonnegative()
    receipt_ino = _nonnegative()
    receipt_uid = models.PositiveIntegerField()
    receipt_gid = models.PositiveIntegerField()
    receipt_mode = models.PositiveIntegerField()
    receipt_nlink = models.PositiveIntegerField()
    receipt_size = _nonnegative(null=True)
    receipt_mtime_ns = _nonnegative(null=True)
    receipt_ctime_ns = _nonnegative(null=True)
    receipt_sha256 = models.CharField(max_length=64, null=True, blank=True)

    class Meta:
        unique_together = (("attempt", "relative_path"),)
        constraints = (
            models.CheckConstraint(check=Q(kind__in=ATTEMPT_FILE_KINDS), name="export_attempt_file_kind_valid"),
            models.CheckConstraint(check=Q(state__in=ATTEMPT_FILE_STATES), name="export_attempt_file_state_valid"),
            models.CheckConstraint(check=Q(receipt_level__in=RECEIPT_LEVELS), name="export_attempt_file_level_valid"),
            models.CheckConstraint(check=_nonnegative_check(("receipt_dev", "receipt_ino", "receipt_size", "receipt_mtime_ns", "receipt_ctime_ns")), name="export_attempt_file_nonnegative"),
            models.CheckConstraint(check=_all_or_none(("receipt_size", "receipt_mtime_ns", "receipt_ctime_ns")), name="export_attempt_file_receipt_full"),
            models.CheckConstraint(check=(Q(receipt_level="open", receipt_size__isnull=True, receipt_mtime_ns__isnull=True, receipt_ctime_ns__isnull=True, receipt_sha256__isnull=True) | Q(receipt_level="full", receipt_size__isnull=False, receipt_mtime_ns__isnull=False, receipt_ctime_ns__isnull=False)), name="export_attempt_file_level_receipt"),
            models.CheckConstraint(check=~Q(state__in=("verifying", "publishing", "ready_candidate", "published")) | Q(receipt_level="full", receipt_sha256__isnull=False), name="export_attempt_file_verified_receipt_valid"),
            models.CheckConstraint(check=~Q(state="closed") | Q(receipt_level="full"), name="export_attempt_file_closed_receipt_valid"),
            models.CheckConstraint(check=~(Q(kind="archive", state="publishing") | Q(kind="quarantine", state="writing")) | Q(intent_relative_path__isnull=False), name="export_attempt_file_intent_valid"),
            models.CheckConstraint(check=~(Q(state__in=("ready_candidate", "published", "cleaned")) | Q(kind="quarantine", state="closed")) | Q(intent_relative_path__isnull=True), name="export_attempt_file_final_intent_valid"),
            models.CheckConstraint(check=~Q(kind="quarantine") | Q(state__in=("writing", "closed", "cleaned")), name="export_quarantine_state_valid"),
            models.CheckConstraint(check=_nullable_sha_check("receipt_sha256"), name="export_attempt_file_sha_valid"),
        )

    def clean(self):
        super(ExportAttemptFile, self).clean()
        full_fields = ("receipt_size", "receipt_mtime_ns", "receipt_ctime_ns")
        _validate_all_or_none(self, full_fields, "receipt")
        if self.receipt_level == "open" and any(getattr(self, field) is not None for field in full_fields + ("receipt_sha256",)):
            raise ValidationError("열린 파일 영수증에는 크기, 시각, 해시를 기록할 수 없습니다.")
        if self.receipt_level == "full" and any(getattr(self, field) is None for field in full_fields):
            raise ValidationError("완전한 파일 영수증에는 크기와 시각이 필요합니다.")
        if self.state == "closed" and self.receipt_level != "full":
            raise ValidationError("닫힌 파일에는 완전한 영수증이 필요합니다.")
        if self.state in ("verifying", "publishing", "ready_candidate", "published") and (self.receipt_level != "full" or not _is_lower_sha(self.receipt_sha256) or self.receipt_sha256 is None):
            raise ValidationError("검증 이후 파일에는 해시가 있는 완전한 영수증이 필요합니다.")
        if not _is_lower_sha(self.receipt_sha256):
            raise ValidationError("SHA-256은 소문자 64자리 16진수여야 합니다.")
        if ((self.kind == "archive" and self.state == "publishing") or
                (self.kind == "quarantine" and self.state == "writing")) and not self.intent_relative_path:
            raise ValidationError("이동 중인 파일에는 목적 경로가 필요합니다.")
        if (self.state in ("ready_candidate", "published", "cleaned") or
                (self.kind == "quarantine" and self.state == "closed")) and self.intent_relative_path:
            raise ValidationError("확정 또는 정리된 파일에는 이동 목적 경로를 둘 수 없습니다.")
