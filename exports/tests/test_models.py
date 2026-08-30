import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from core.models import Board, Image, Pin
from exports.models import ExportBlob, ExportItem, ExportJob, ExportSlot, ExportTarget
from exports.contracts import export_status


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
