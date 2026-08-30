import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from core.models import Board, Image, Pin
from exports.models import (
    ExportAttempt, ExportAttemptFile, ExportBlob, ExportItem, ExportJob,
    ExportSlot, ExportTarget, ExportWorkerLease,
)
from exports.contracts import (
    ExportError, LeaseToken, StopRequested, export_status,
)


class ExportModelConstraintTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="export-owner",
            password="password",
        )
        self.other_user = get_user_model().objects.create_user(
            username="other-owner",
            password="password",
        )

    def test_target_position_is_unique_within_job(self):
        job = ExportJob.objects.create(owner=self.user, scope="pins")
        ExportTarget.objects.create(job=job, position=0, pin_id=11)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ExportTarget.objects.create(job=job, position=0, pin_id=12)

    def test_target_identity_snapshot_is_all_or_none_in_model_and_database(self):
        job = ExportJob.objects.create(owner=self.user, scope="pins")
        published = timezone.now()

        ExportTarget(
            job=job,
            position=0,
            pin_id=11,
            pin_owner_id_snapshot=self.user.pk,
            pin_published_at_snapshot=published,
        ).full_clean()
        ExportTarget(job=job, position=1, pin_id=12).full_clean()

        for fields in (
            {"pin_owner_id_snapshot": self.user.pk},
            {"pin_published_at_snapshot": published},
        ):
            with self.subTest(fields=fields):
                with self.assertRaises(ValidationError):
                    ExportTarget(
                        job=job,
                        position=2,
                        pin_id=13,
                        **fields
                    ).full_clean()
                with self.assertRaises(IntegrityError):
                    with transaction.atomic():
                        ExportTarget.objects.create(
                            job=job,
                            position=2,
                            pin_id=13,
                            **fields
                        )

    def test_snapshot_models_do_not_reference_live_pin_board_or_image_rows(self):
        live_models = (Pin, Board, Image)

        self.assertFalse(any(
            field.remote_field and field.remote_field.model in live_models
            for model in (ExportTarget, ExportItem, ExportBlob)
            for field in model._meta.fields
        ))

    def test_slot_rejects_a_job_owned_by_another_user(self):
        other_job = ExportJob.objects.create(
            owner=self.other_user,
            scope="pins",
        )

        with self.assertRaises(ValidationError):
            ExportSlot(owner=self.user, current_job=other_job).full_clean()

    def test_complete_job_requires_an_entire_ready_receipt(self):
        job = ExportJob(
            owner=self.user,
            scope="pins",
            state="complete",
            ready_cleanup_state="retained",
            completed_at="2026-08-30T12:00:00Z",
            expires_at="2026-08-31T12:00:00Z",
            ready_relative_path="ready/export.zip",
            ready_display_name="export.zip",
            ready_size=10,
            ready_sha256="a" * 64,
            ready_dev=1,
        )

        with self.assertRaises(ValidationError):
            job.full_clean()

    def test_closed_blob_requires_a_complete_receipt_except_for_sha(self):
        job = ExportJob.objects.create(owner=self.user, scope="pins")
        blob = ExportBlob(
            job=job,
            snapshot_generation=uuid.uuid4(),
            source_media_asset_id=1,
            source_image_id=2,
            source_relative_path="snapshot/source",
            file_state="closed",
            receipt_dev=1,
        )

        with self.assertRaises(ValidationError):
            blob.full_clean()

    def test_queued_job_with_an_older_job_heartbeat_shows_worker_waiting(self):
        now = timezone.now()
        job = ExportJob(
            owner=self.user,
            scope="pins",
            heartbeat_at=now - timedelta(minutes=5),
        )

        status = export_status(job, now, worker_heartbeat_at=now)

        self.assertEqual(status["phase_label"], "작업자 대기 중")

    def test_optional_fields_allow_a_minimal_queued_job_to_validate(self):
        ExportJob(owner=self.user, scope="pins").full_clean()

    def test_worker_health_accepts_runtime_states_and_rejects_legacy_healthy(self):
        for health_state in ("starting", "ready", "failed", "stopped"):
            ExportWorkerLease(health_state=health_state).full_clean()
        with self.assertRaises(ValidationError):
            ExportWorkerLease(health_state="healthy").full_clean()

    def test_stop_and_export_errors_keep_the_current_lease(self):
        lease = LeaseToken(3, uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), 7)
        stop = StopRequested(lease=lease)
        error = ExportError("archive_failed", lease=lease)

        self.assertIs(stop.lease, lease)
        self.assertIs(error.lease, lease)
        self.assertEqual(error.code, "archive_failed")
        with self.assertRaises(ValueError):
            ExportError("not_allowed", lease=lease)
        with self.assertRaises(TypeError):
            StopRequested()
        with self.assertRaises(TypeError):
            ExportError("archive_failed")

    def test_ready_receipt_state_combinations_validate_exactly(self):
        ready = {
            "ready_relative_path": "ready/export.zip",
            "ready_display_name": "export.zip",
            "ready_size": 10,
            "ready_sha256": "a" * 64,
            "ready_dev": 1,
            "ready_ino": 2,
            "ready_uid": 3,
            "ready_gid": 4,
            "ready_mode": 0o600,
            "ready_nlink": 1,
            "ready_mtime_ns": 5,
            "ready_ctime_ns": 6,
        }
        ExportJob(owner=self.user, scope="pins").full_clean()
        ExportJob(owner=self.user, scope="pins", state="expired", ready_cleanup_state="cleaned").full_clean()
        with self.assertRaises(ValidationError):
            ExportJob(owner=self.user, scope="pins", **ready).full_clean()
        with self.assertRaises(ValidationError):
            ExportJob(owner=self.user, scope="pins", state="expired", ready_cleanup_state="pending").full_clean()
        with self.assertRaises(ValidationError):
            ExportJob(owner=self.user, scope="pins", state="expired", ready_cleanup_state="cleaned", **ready).full_clean()

    def test_closed_and_cleaned_receipts_validate_their_required_shapes(self):
        job = ExportJob.objects.create(owner=self.user, scope="pins")
        attempt = ExportAttempt(
            job=job, attempt_generation=0, lease_uuid=uuid.uuid4(),
            state="cleaned", relative_path="attempts/0",
        )
        with self.assertRaises(ValidationError):
            attempt.full_clean()
        file = ExportAttemptFile(
            attempt=ExportAttempt.objects.create(
                job=job, attempt_generation=1, lease_uuid=uuid.uuid4(),
                state="writing", relative_path="attempts/1",
            ),
            kind="quarantine", state="closed", receipt_level="open",
            relative_path="attempts/1/quarantine.zip", intent_relative_path="dest",
            receipt_dev=1, receipt_ino=2, receipt_uid=3, receipt_gid=4,
            receipt_mode=0o600, receipt_nlink=1,
        )
        with self.assertRaises(ValidationError):
            file.full_clean()

    def test_closed_archive_with_open_receipt_is_rejected_independently(self):
        job = ExportJob.objects.create(owner=self.user, scope="pins")
        file = ExportAttemptFile(
            attempt=ExportAttempt.objects.create(
                job=job, attempt_generation=2, lease_uuid=uuid.uuid4(),
                state="writing", relative_path="attempts/2",
            ),
            kind="archive", state="closed", receipt_level="open",
            relative_path="attempts/2/archive.zip", receipt_dev=1, receipt_ino=2,
            receipt_uid=3, receipt_gid=4, receipt_mode=0o600, receipt_nlink=1,
        )

        with self.assertRaises(ValidationError):
            file.full_clean()

    def test_closed_quarantine_with_intent_is_rejected_independently(self):
        job = ExportJob.objects.create(owner=self.user, scope="pins")
        file = ExportAttemptFile(
            attempt=ExportAttempt.objects.create(
                job=job, attempt_generation=3, lease_uuid=uuid.uuid4(),
                state="writing", relative_path="attempts/3",
            ),
            kind="quarantine", state="closed", receipt_level="full",
            relative_path="attempts/3/quarantine.zip", intent_relative_path="ready/dest.zip",
            receipt_dev=1, receipt_ino=2, receipt_uid=3, receipt_gid=4,
            receipt_mode=0o600, receipt_nlink=1, receipt_size=10,
            receipt_mtime_ns=5, receipt_ctime_ns=6,
        )

        with self.assertRaises(ValidationError):
            file.full_clean()

    def test_sha_validator_rejects_non_strict_values_and_allows_null(self):
        job = ExportJob.objects.create(owner=self.user, scope="pins")
        for value in ("a" * 64 + "\n", "a" * 63, "a" * 65, "A" * 64, "g" * 64, "a" * 62 + "\r\n"):
            with self.subTest(value=repr(value)):
                blob = ExportBlob(job=job, snapshot_generation=uuid.uuid4(), source_media_asset_id=1, source_image_id=1, source_relative_path="blob", receipt_sha256=value)
                with self.assertRaises(ValidationError):
                    blob.full_clean()
        ExportBlob(job=job, snapshot_generation=uuid.uuid4(), source_media_asset_id=2, source_image_id=2, source_relative_path="null", receipt_sha256=None).full_clean()
