from contextlib import contextmanager
import os
from pathlib import Path
import threading
import uuid

from django.conf import settings
from django.test import TransactionTestCase
from django.utils import timezone
import mock

from core.services.database_fence import DatabaseFenceBusy
from exports.contracts import LeaseLost, LeaseToken, StopRequested
from exports.models import ExportJob, ExportTarget, ExportWorkerLease
from exports.services.archive import ArchiveService
from exports.services import attempts as attempt_services
from exports.services.attempts import ARCHIVE_PART_NAME, AttemptService
from exports.services.file_ops import (
    ExportStorageError,
    OpenFileReceipt,
    open_export_root,
    open_receipted_directory,
    remove_if_receipt_matches,
)
from exports.services.snapshot import SnapshotService, open_staging_directory

from .helpers import ExportStorageMixin, create_export_pin, create_export_user


class _Rotation(object):
    def __init__(self, heartbeat):
        self.heartbeat = heartbeat

    def replace(self, lease):
        self.heartbeat.lease = lease

    def clear(self):
        self.heartbeat.lease = None


class FakeHeartbeat(object):
    def __init__(self):
        self.guard_depth = 0
        self.pulses = 0
        self.lease = None
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

    @contextmanager
    def job_token_transition(self, lease):
        with self._lock:
            self.lease = lease
            yield _Rotation(self)

    def renew_now(self, lease):
        self.lease = lease
        self.pulses += 1


class AttemptServiceTests(ExportStorageMixin, TransactionTestCase):
    def setUp(self):
        super(AttemptServiceTests, self).setUp()
        staging_path = Path(self._export_directory.name, ".staging")
        staging_path.mkdir(mode=0o700)
        os.chmod(str(staging_path), 0o700)
        self.owner = create_export_user("attempt-owner")
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
            owner=self.owner,
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

    def _snapshot(self, filename):
        pin = create_export_pin(self.owner, filename=filename)
        size = os.path.getsize(pin.image.image.path)
        job = ExportJob.objects.create(
            owner=self.owner,
            scope="pins",
            state="snapshotting",
            requested_total=1,
            target_total=1,
            included_total=1,
            bytes_total=size,
            worker_generation=4,
            lease_uuid=self.job.lease_uuid,
            attempt_generation=0,
        )
        ExportTarget.objects.create(
            job=job,
            position=0,
            pin_id=pin.pk,
            pin_owner_id_snapshot=pin.submitter_id,
            pin_published_at_snapshot=pin.published,
        )
        lease = LeaseToken(
            4,
            self.lease.worker_lease_uuid,
            job.pk,
            job.lease_uuid,
            0,
        )
        heartbeat = FakeHeartbeat()
        with mock.patch("exports.services.file_ops._normalize_metadata"):
            SnapshotService().capture(job, lease, heartbeat, lambda: False)
        job.refresh_from_db()
        return job, lease, heartbeat

    def test_unreceipted_attempt_directory_recovery_removes_only_exact_empty_directory(self):
        job, lease, heartbeat = self._snapshot("unreceipted.png")
        stopped = []

        def stop(point, context):
            if point == "after_attempt_directory_create":
                stopped.append(context["directory"].receipt)
                raise StopRequested(lease)

        with self.assertRaises(StopRequested):
            ArchiveService(fault_injector=stop).build_and_publish(
                job, lease, heartbeat, lambda: False,
            )

        attempt_name = "attempt-{}-0".format(job.pk)
        attempt_path = Path(
            self._export_directory.name, ".staging", attempt_name,
        )
        neighbor = attempt_path.with_name(attempt_name + "-other")
        neighbor.mkdir(mode=0o700)
        os.chmod(str(neighbor), 0o700)
        self.assertEqual(len(stopped), 1)
        self.assertEqual(job.attempts.count(), 0)
        self.assertTrue(stopped[0].matches_stat(attempt_path.stat()))

        root, staging = self._directories()
        try:
            child = attempt_path / "child"
            child.write_bytes(b"do-not-delete")
            with self.assertRaises(ExportStorageError):
                self.service.remove_unreceipted_directory_fs(
                    staging, job, lease,
                )
            self.assertEqual(child.read_bytes(), b"do-not-delete")
            child.unlink()

            os.chmod(str(attempt_path), 0o755)
            with self.assertRaises(ExportStorageError):
                self.service.remove_unreceipted_directory_fs(
                    staging, job, lease,
                )
            os.chmod(str(attempt_path), 0o700)

            for owner_field in ("uid", "gid"):
                with self.subTest(owner_field=owner_field):
                    expected_owner = getattr(staging, owner_field)
                    setattr(staging, owner_field, expected_owner + 1)
                    try:
                        with self.assertRaises(ExportStorageError):
                            self.service.remove_unreceipted_directory_fs(
                                staging, job, lease,
                            )
                    finally:
                        setattr(staging, owner_field, expected_owner)

            saved = attempt_path.with_name(attempt_name + "-saved")
            attempt_path.rename(saved)
            attempt_path.write_bytes(b"not-a-directory")
            with self.assertRaises(ExportStorageError):
                self.service.remove_unreceipted_directory_fs(
                    staging, job, lease,
                )
            attempt_path.unlink()
            saved.rename(attempt_path)

            attempt_path.rename(saved)
            attempt_path.symlink_to(saved.name)
            with self.assertRaises(ExportStorageError):
                self.service.remove_unreceipted_directory_fs(
                    staging, job, lease,
                )
            attempt_path.unlink()
            saved.rename(attempt_path)

            original_open = open_receipted_directory
            swapped = []

            def swap_before_open(parent, name, receipt):
                os.rename(
                    name,
                    name + "-saved",
                    src_dir_fd=parent.descriptor,
                    dst_dir_fd=parent.descriptor,
                )
                os.mkdir(name, 0o700, dir_fd=parent.descriptor)
                swapped.append(True)
                return original_open(parent, name, receipt)

            with mock.patch(
                "exports.services.attempts.open_receipted_directory",
                side_effect=swap_before_open,
            ):
                with self.assertRaises(ExportStorageError):
                    self.service.remove_unreceipted_directory_fs(
                        staging, job, lease,
                    )
            self.assertEqual(swapped, [True])
            self.assertTrue(attempt_path.is_dir())
            self.assertTrue(saved.is_dir())
            attempt_path.rmdir()
            saved.rename(attempt_path)

            original_fsync = os.fsync
            parent_fsyncs = []

            def record_fsync(descriptor):
                parent_fsyncs.append(descriptor)
                return original_fsync(descriptor)

            with mock.patch(
                "exports.services.attempts.os.fsync",
                record_fsync,
            ):
                self.assertTrue(
                    self.service.remove_unreceipted_directory_fs(
                        staging, job, lease,
                    )
                )
            self.assertEqual(parent_fsyncs, [staging.descriptor])
        finally:
            staging.close()
            root.close()

        outcome = ArchiveService().recover_archiving(
            lease, heartbeat, lambda: False,
        )

        self.assertEqual(outcome.job.state, "complete")
        self.assertEqual(outcome.lease, lease)
        self.assertEqual(outcome.job.attempt_generation, 0)
        self.assertTrue(neighbor.is_dir())

    def test_open_receipt_cleanup_preserves_open_tombstone_after_enoent_resume(self):
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
            os.write(descriptor, b"partial archive")
            os.close(descriptor)
            descriptor = None
            ArchiveService()._mark_current_attempt_retiring(
                self.lease, self.heartbeat,
            )
            attempt.refresh_from_db()
            attempt_file.refresh_from_db()
            self.assertEqual(attempt.state, "retiring")
            self.assertEqual(attempt_file.state, "retiring")
            receipt = attempt_services.attempt_file_cleanup_receipt(
                attempt_file,
            )
            self.assertIsInstance(receipt, OpenFileReceipt)
            self.assertTrue(remove_if_receipt_matches(
                directory, ARCHIVE_PART_NAME, receipt,
            ))
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if directory is not None:
                directory.close()
            staging.close()
            root.close()

        ArchiveService()._cleanup_retiring(
            attempt, attempt_file, self.lease, self.heartbeat,
        )

        attempt.refresh_from_db()
        attempt_file.refresh_from_db()
        self.assertEqual(attempt.state, "cleaned")
        self.assertEqual(attempt_file.state, "cleaned")
        self.assertEqual(attempt_file.receipt_level, "open")
        self.assertIsNone(attempt_file.receipt_size)
        self.assertIsNone(attempt_file.receipt_mtime_ns)
        self.assertIsNone(attempt_file.receipt_ctime_ns)
        self.assertIsNone(attempt_file.receipt_sha256)
        self.assertFalse(Path(
            self._export_directory.name,
            ".staging",
            attempt.relative_path,
        ).exists())

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

    def test_db_only_helper_retries_transient_busy_with_same_receipt_token(self):
        root, staging = self._directories()
        directory = None
        try:
            directory = self.service.create_directory_fs(
                staging, self.job, self.lease,
            )
            original_fence = attempt_services.database_write_fence
            calls = []
            sleeps = []

            @contextmanager
            def busy_once(*args, **kwargs):
                calls.append((args, kwargs, directory.receipt, self.lease))
                if len(calls) == 1:
                    raise DatabaseFenceBusy()
                with original_fence(*args, **kwargs) as current:
                    yield current

            def sleep_outside_guard(seconds):
                self.assertEqual(self.heartbeat.guard_depth, 0)
                sleeps.append(seconds)

            service = AttemptService(sleeper=sleep_outside_guard)
            with mock.patch(
                "exports.services.attempts.database_write_fence",
                side_effect=busy_once,
            ):
                attempt = service.record_directory_receipt_db_only(
                    self.job, self.lease, directory, self.heartbeat,
                    stop_requested=lambda: False,
                )

            self.assertEqual(attempt.attempt_generation, 0)
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0][2:], calls[1][2:])
            self.assertEqual(sleeps, [0.01])
            self.assertEqual(self.heartbeat.pulses, 1)
        finally:
            if directory is not None:
                directory.close()
            staging.close()
            root.close()

    def test_db_only_helper_budget_and_token_change_stop_retry(self):
        root, staging = self._directories()
        directory = None
        try:
            directory = self.service.create_directory_fs(
                staging, self.job, self.lease,
            )
            clock = [0.0]
            calls = []

            @contextmanager
            def always_busy(*args, **kwargs):
                del args, kwargs
                calls.append(True)
                raise DatabaseFenceBusy()
                yield

            def monotonic():
                value = clock[0]
                clock[0] += 3.0
                return value

            with mock.patch(
                "exports.services.attempts.database_write_fence",
                side_effect=always_busy,
            ):
                with self.assertRaises(DatabaseFenceBusy):
                    AttemptService(
                        monotonic=monotonic,
                        sleeper=lambda seconds: None,
                    ).record_directory_receipt_db_only(
                        self.job, self.lease, directory, self.heartbeat,
                        stop_requested=lambda: False,
                    )
            self.assertEqual(calls, [True, True])
            self.assertEqual(self.job.attempts.count(), 0)

            original_fence = attempt_services.database_write_fence
            calls = []

            @contextmanager
            def busy_then_real(*args, **kwargs):
                calls.append(True)
                if len(calls) == 1:
                    raise DatabaseFenceBusy()
                with original_fence(*args, **kwargs) as current:
                    yield current

            def replace_token(seconds):
                del seconds
                ExportJob.objects.filter(pk=self.job.pk).update(
                    lease_uuid=uuid.uuid4(),
                )

            with mock.patch(
                "exports.services.attempts.database_write_fence",
                side_effect=busy_then_real,
            ):
                with self.assertRaises(LeaseLost):
                    AttemptService(
                        sleeper=replace_token,
                    ).record_directory_receipt_db_only(
                        self.job, self.lease, directory, self.heartbeat,
                        stop_requested=lambda: False,
                    )
            self.assertEqual(calls, [True, True])
            self.assertEqual(self.job.attempts.count(), 0)
        finally:
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
