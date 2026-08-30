from concurrent.futures import ThreadPoolExecutor

from django.db import close_old_connections
from django.test import TransactionTestCase
from django.utils import timezone

from exports.models import ExportJob, ExportSlot, ExportTarget, ExportWorkerLease
from exports.services.jobs import ExportRequestError, JobService
from exports.services.targeting import TargetingService

from .helpers import ExportStorageMixin, create_export_pin, create_export_user


class ExportCreateConcurrencyTests(ExportStorageMixin, TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        super(ExportCreateConcurrencyTests, self).setUp()
        self.owner = create_export_user("race-owner")
        self.pin = create_export_pin(self.owner)
        self.now = timezone.now()
        ExportWorkerLease.objects.create(
            pk=1,
            health_state="ready",
            heartbeat_at=self.now,
        )

    def _create(self):
        close_old_connections()
        try:
            service = JobService(
                targeting=TargetingService(size_observer=lambda identity: 1),
                available_space_observer=lambda: 10 ** 12,
            )
            try:
                job = service.create(
                    self.owner,
                    {"scope": "pins", "pin_ids": [self.pin.pk]},
                    self.now,
                )
                return 202, job.pk
            except ExportRequestError as error:
                return error.status_code, None
        finally:
            close_old_connections()

    def test_two_file_sqlite_connections_commit_one_job_and_one_conflict(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(self._create) for _index in range(2)]
            results = [future.result(timeout=20) for future in futures]

        self.assertEqual(sorted(status for status, _job_id in results), [202, 409])
        success_id = next(job_id for status, job_id in results if status == 202)
        self.assertEqual(ExportJob.objects.filter(owner=self.owner).count(), 1)
        self.assertEqual(ExportTarget.objects.count(), 1)
        self.assertEqual(
            ExportSlot.objects.get(owner=self.owner).current_job_id,
            success_id,
        )
