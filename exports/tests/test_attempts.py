from contextlib import contextmanager
import os
from pathlib import Path
import threading
import uuid

from django.conf import settings
from django.test import TransactionTestCase
from django.utils import timezone

from exports.contracts import LeaseToken
from exports.models import ExportJob, ExportWorkerLease
from exports.services.attempts import AttemptService
from exports.services.file_ops import open_export_root
from exports.services.snapshot import open_staging_directory

from .helpers import ExportStorageMixin, create_export_user


class FakeHeartbeat(object):
    def __init__(self):
        self.guard_depth = 0
        self.pulses = 0
        self._lock = threading.RLock()

    @contextmanager
    def foreground_write_guard(self):
        with self._lock:
            self.guard_depth += 1
            try:
                yield
            finally:
                self.guard_depth -= 1

    def __call__(self):
        self.pulses += 1


class AttemptServiceTests(ExportStorageMixin, TransactionTestCase):
    def setUp(self):
        super(AttemptServiceTests, self).setUp()
        staging_path = Path(self._export_directory.name, ".staging")
        staging_path.mkdir(mode=0o700)
        os.chmod(str(staging_path), 0o700)
        owner = create_export_user("attempt-owner")
        worker_uuid = uuid.uuid4()
        job_uuid = uuid.uuid4()
        ExportWorkerLease.objects.create(
            pk=1,
            generation=4,
            lease_uuid=worker_uuid,
            health_state="ready",
            heartbeat_at=timezone.now(),
        )
        self.job = ExportJob.objects.create(
            owner=owner,
            scope="pins",
            state="archiving",
            requested_total=1,
            target_total=1,
            included_total=1,
            archive_total=1,
            worker_generation=4,
            lease_uuid=job_uuid,
            attempt_generation=0,
        )
        self.lease = LeaseToken(
            4, worker_uuid, self.job.pk, job_uuid, 0,
        )
        self.heartbeat = FakeHeartbeat()
        self.service = AttemptService()

    def _directories(self):
        root = open_export_root(
            settings.PINRY_EXPORT_ROOT, os.getuid(), os.getgid(),
        )
        staging = open_staging_directory(root)
        return root, staging

    def test_open_receipt_is_persisted_before_archive_bytes(self):
        root, staging = self._directories()
        directory = None
        descriptor = None
        try:
            directory = self.service.create_directory_fs(
                staging, self.job, self.lease,
            )
            attempt = self.service.record_directory_receipt_db_only(
                self.job, self.lease, directory, self.heartbeat,
            )
            descriptor, open_receipt = self.service.create_file_fs(directory)
            attempt_file = self.service.record_open_file_receipt_db_only(
                attempt, self.lease, open_receipt, self.heartbeat,
            )

            self.assertEqual(os.fstat(descriptor).st_size, 0)
            self.assertEqual(attempt.state, "writing")
            self.assertEqual(attempt_file.state, "writing")
            self.assertEqual(attempt_file.receipt_level, "open")
            self.assertIsNone(attempt_file.receipt_size)
            self.assertEqual(self.heartbeat.guard_depth, 0)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if directory is not None:
                directory.close()
            staging.close()
            root.close()

    def test_close_receipt_and_validation_reopen_preserve_raw_descriptor_owner(self):
        root, staging = self._directories()
        directory = None
        validation_fd = None
        try:
            directory = self.service.create_directory_fs(
                staging, self.job, self.lease,
            )
            attempt = self.service.record_directory_receipt_db_only(
                self.job, self.lease, directory, self.heartbeat,
            )
            descriptor, open_receipt = self.service.create_file_fs(directory)
            attempt_file = self.service.record_open_file_receipt_db_only(
                attempt, self.lease, open_receipt, self.heartbeat,
            )
            os.write(descriptor, b"zip-bytes")
            exported_at = timezone.now()
            recorded = self.service.ensure_exported_at(
                attempt, self.lease, exported_at, self.heartbeat,
            )
            closed = self.service.close_file_fs(descriptor, open_receipt)
            attempt_file = self.service.record_closed_file_receipt_db_only(
                attempt, attempt_file, closed, self.lease, self.heartbeat,
            )
            validation_fd = self.service.open_closed_file_for_validation(
                attempt_file, self.lease,
            )

            self.assertEqual(os.pread(validation_fd, 9, 0), b"zip-bytes")
            self.assertEqual(attempt_file.state, "closed")
            self.assertEqual(attempt_file.receipt_level, "full")
            self.assertEqual(recorded, exported_at)
        finally:
            if validation_fd is not None:
                os.close(validation_fd)
            if directory is not None:
                directory.close()
            staging.close()
            root.close()
