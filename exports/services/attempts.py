import errno
import os
import stat
import time

from django.conf import settings
from django.db import transaction

from core.services.database_fence import (
    DatabaseFenceBusy,
    DatabaseFenceDeadline,
    database_write_fence,
)
from exports.contracts import LeaseLost, StopRequested, lock_current_lease
from exports.models import (
    ExportAttempt,
    ExportAttemptFile,
    ExportJob,
    ExportWorkerLease,
)
from exports.services.file_ops import (
    ClosedFileReceipt,
    DirectoryReceipt,
    ExportStorageError,
    OpenFileReceipt,
    create_private_directory,
    create_private_file_fs,
    open_export_root,
    open_receipted_directory,
)
from exports.services.snapshot import open_staging_directory


ARCHIVE_PART_NAME = "archive.zip.part"


def _attempt_name(job_id, generation):
    return "attempt-{}-{}".format(job_id, generation)


def _attempt_receipt(attempt):
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


def _open_receipt(attempt_file):
    return OpenFileReceipt(
        attempt_file.receipt_dev,
        attempt_file.receipt_ino,
        attempt_file.receipt_uid,
        attempt_file.receipt_gid,
        attempt_file.receipt_mode,
        attempt_file.receipt_nlink,
    )


def closed_file_receipt(attempt_file):
    if attempt_file.receipt_level != "full":
        return None
    return ClosedFileReceipt(
        attempt_file.receipt_dev,
        attempt_file.receipt_ino,
        attempt_file.receipt_uid,
        attempt_file.receipt_gid,
        attempt_file.receipt_mode,
        attempt_file.receipt_nlink,
        attempt_file.receipt_size,
        attempt_file.receipt_mtime_ns,
        attempt_file.receipt_ctime_ns,
        attempt_file.receipt_sha256,
    )


def attempt_file_cleanup_receipt(attempt_file):
    if attempt_file.receipt_level == "open":
        if any(getattr(attempt_file, field) is not None for field in (
            "receipt_size",
            "receipt_mtime_ns",
            "receipt_ctime_ns",
            "receipt_sha256",
        )):
            raise ExportStorageError("export_storage_unsafe")
        return _open_receipt(attempt_file)
    if attempt_file.receipt_level == "full":
        receipt = closed_file_receipt(attempt_file)
        if receipt is not None:
            return receipt
    raise ExportStorageError("export_storage_unsafe")


class AttemptService(object):
    FENCE_MODELS = (
        ExportWorkerLease, ExportJob, ExportAttempt, ExportAttemptFile,
    )

    def __init__(self, using="default", monotonic=None, sleeper=None):
        self.using = using
        self.monotonic = monotonic or time.monotonic
        self.sleeper = sleeper or time.sleep

    def _retry_db_only(
        self, operation, lease, heartbeat, stop_requested,
    ):
        deadline = DatabaseFenceDeadline(self.monotonic)
        while True:
            try:
                return operation()
            except DatabaseFenceBusy:
                deadline.checkpoint()
                if stop_requested is not None and stop_requested():
                    raise StopRequested(lease)
                heartbeat()
                self.sleeper(0.01)
                if stop_requested is not None and stop_requested():
                    raise StopRequested(lease)

    def create_directory_fs(self, staging, job, lease):
        if job.pk != lease.job_id:
            raise LeaseLost()
        return create_private_directory(
            staging,
            _attempt_name(job.pk, lease.attempt_generation),
            staging.uid,
            staging.gid,
        )

    def remove_unreceipted_directory_fs(self, staging, job, lease):
        if (
            job.pk != lease.job_id
            or job.state != "archiving"
            or job.attempt_generation != lease.attempt_generation
        ):
            raise LeaseLost()
        name = _attempt_name(job.pk, lease.attempt_generation)
        staging.verify_identity()
        try:
            named = os.stat(
                name,
                dir_fd=staging.descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            staging.verify_identity()
            return False
        if (
            not stat.S_ISDIR(named.st_mode)
            or named.st_uid != staging.uid
            or named.st_gid != staging.gid
            or stat.S_IMODE(named.st_mode) != 0o700
        ):
            raise ExportStorageError("export_storage_unsafe")
        receipt = DirectoryReceipt(
            named.st_dev,
            named.st_ino,
            named.st_uid,
            named.st_gid,
            stat.S_IMODE(named.st_mode),
        )
        directory = open_receipted_directory(staging, name, receipt)
        try:
            if os.listdir(directory.descriptor):
                raise ExportStorageError("export_storage_unsafe")
            directory.verify_identity()
            if os.listdir(directory.descriptor):
                raise ExportStorageError("export_storage_unsafe")
            os.rmdir(name, dir_fd=staging.descriptor)
            os.fsync(staging.descriptor)
            return True
        except ExportStorageError:
            raise
        except OSError:
            raise ExportStorageError("export_storage_unsafe") from None
        finally:
            directory.close()

    def record_directory_receipt_db_only(
        self, job, lease, directory, heartbeat, stop_requested=None,
    ):
        receipt = directory.receipt
        relative_path = _attempt_name(job.pk, lease.attempt_generation)

        def operation():
            with heartbeat.foreground_write_guard():
                with database_write_fence(
                    using=self.using, models=self.FENCE_MODELS,
                ):
                    current = lock_current_lease(lease, using=self.using)
                    if current.state != "archiving":
                        raise LeaseLost()
                    attempt, created = ExportAttempt.objects.using(
                        self.using,
                    ).select_for_update().get_or_create(
                        job=current,
                        attempt_generation=lease.attempt_generation,
                        defaults={
                            "lease_uuid": lease.job_lease_uuid,
                            "state": "writing",
                            "relative_path": relative_path,
                            "dir_dev": receipt.dev,
                            "dir_ino": receipt.ino,
                            "dir_uid": receipt.uid,
                            "dir_gid": receipt.gid,
                            "dir_mode": receipt.mode,
                        },
                    )
                    if not created and (
                        attempt.relative_path != relative_path
                        or _attempt_receipt(attempt) != receipt
                        or attempt.state != "writing"
                    ):
                        raise LeaseLost()
                    return attempt

        return self._retry_db_only(
            operation, lease, heartbeat, stop_requested,
        )

    def create_file_fs(self, directory):
        return create_private_file_fs(directory, ARCHIVE_PART_NAME)

    def record_open_file_receipt_db_only(
        self, attempt, lease, receipt, heartbeat, stop_requested=None,
    ):
        if not isinstance(receipt, OpenFileReceipt):
            raise TypeError("receipt must be an OpenFileReceipt")
        relative_path = "{}/{}".format(
            attempt.relative_path,
            ARCHIVE_PART_NAME,
        )

        def operation():
            with heartbeat.foreground_write_guard():
                with database_write_fence(
                    using=self.using, models=self.FENCE_MODELS,
                ):
                    lock_current_lease(lease, using=self.using)
                    current_attempt = ExportAttempt.objects.using(
                        self.using,
                    ).select_for_update().get(pk=attempt.pk)
                    if (
                        current_attempt.attempt_generation
                        != lease.attempt_generation
                        or current_attempt.state != "writing"
                    ):
                        raise LeaseLost()
                    attempt_file, created = ExportAttemptFile.objects.using(
                        self.using,
                    ).select_for_update().get_or_create(
                        attempt=current_attempt,
                        kind="archive",
                        relative_path=relative_path,
                        defaults={
                            "state": "writing",
                            "receipt_level": "open",
                            "receipt_dev": receipt.dev,
                            "receipt_ino": receipt.ino,
                            "receipt_uid": receipt.uid,
                            "receipt_gid": receipt.gid,
                            "receipt_mode": receipt.mode,
                            "receipt_nlink": receipt.nlink,
                        },
                    )
                    if not created and _open_receipt(attempt_file) != receipt:
                        raise LeaseLost()
                    return attempt_file

        return self._retry_db_only(
            operation, lease, heartbeat, stop_requested,
        )

    def close_file_fs(self, descriptor, open_receipt):
        try:
            os.fsync(descriptor)
            return ClosedFileReceipt.from_open_fd(
                descriptor,
                open_receipt,
            )
        except OSError as error:
            if error.errno == errno.ENOSPC:
                raise ExportStorageError("insufficient_space") from None
            raise ExportStorageError("archive_failed") from None
        finally:
            os.close(descriptor)

    def record_closed_file_receipt_db_only(
        self, attempt, attempt_file, receipt, lease, heartbeat,
        stop_requested=None,
    ):
        if not isinstance(receipt, ClosedFileReceipt):
            raise TypeError("receipt must be a ClosedFileReceipt")

        def operation():
            with heartbeat.foreground_write_guard():
                with database_write_fence(
                    using=self.using, models=self.FENCE_MODELS,
                ):
                    lock_current_lease(lease, using=self.using)
                    current_attempt = ExportAttempt.objects.using(
                        self.using,
                    ).select_for_update().get(pk=attempt.pk)
                    current_file = ExportAttemptFile.objects.using(
                        self.using,
                    ).select_for_update().get(pk=attempt_file.pk)
                    if (
                        current_attempt.state != "writing"
                        or current_attempt.exported_at is None
                        or current_file.state != "writing"
                        or _open_receipt(current_file) != OpenFileReceipt(
                            receipt.dev, receipt.ino, receipt.uid, receipt.gid,
                            receipt.mode, receipt.nlink,
                        )
                    ):
                        raise LeaseLost()
                    current_file.state = "closed"
                    current_file.receipt_level = "full"
                    current_file.receipt_size = receipt.size
                    current_file.receipt_mtime_ns = receipt.mtime_ns
                    current_file.receipt_ctime_ns = receipt.ctime_ns
                    current_file.receipt_sha256 = receipt.sha256
                    current_file.save()
                    current_attempt.state = "closed"
                    current_attempt.save(update_fields=("state",))
                    return current_file

        return self._retry_db_only(
            operation, lease, heartbeat, stop_requested,
        )

    def ensure_exported_at(
        self, attempt, lease, value, heartbeat, stop_requested=None,
    ):
        def operation():
            with heartbeat.foreground_write_guard():
                with database_write_fence(
                    using=self.using, models=self.FENCE_MODELS,
                ):
                    lock_current_lease(lease, using=self.using)
                    current = ExportAttempt.objects.using(
                        self.using,
                    ).select_for_update().get(pk=attempt.pk)
                    if current.state != "writing":
                        raise LeaseLost()
                    if current.exported_at is None:
                        current.exported_at = value
                        current.save(update_fields=("exported_at",))
                    return current.exported_at

        return self._retry_db_only(
            operation, lease, heartbeat, stop_requested,
        )

    def open_closed_file_for_validation(self, attempt_file, lease):
        with transaction.atomic(using=self.using):
            lock_current_lease(lease, using=self.using)
            current_file = ExportAttemptFile.objects.using(
                self.using,
            ).select_related("attempt").get(pk=attempt_file.pk)
            receipt = closed_file_receipt(current_file)
            directory_receipt = _attempt_receipt(current_file.attempt)
            if (
                current_file.state != "closed"
                or receipt is None
                or directory_receipt is None
            ):
                raise LeaseLost()
            attempt_name = current_file.attempt.relative_path
        root = open_export_root(
            settings.PINRY_EXPORT_ROOT,
            os.getuid(),
            os.getgid(),
        )
        staging = None
        directory = None
        descriptor = None
        try:
            staging = open_staging_directory(root)
            directory = open_receipted_directory(
                staging, attempt_name, directory_receipt,
            )
            flags = os.O_RDONLY | os.O_NOFOLLOW
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            descriptor = os.open(
                ARCHIVE_PART_NAME,
                flags,
                dir_fd=directory.descriptor,
            )
            receipt.verify_identity(descriptor)
            return descriptor
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
            raise
        finally:
            if directory is not None:
                directory.close()
            if staging is not None:
                staging.close()
            root.close()

    def record_verifying(
        self, attempt, attempt_file, size, sha256, exported_at,
        lease, heartbeat, stop_requested=None,
    ):
        def operation():
            with heartbeat.foreground_write_guard():
                with database_write_fence(
                    using=self.using, models=self.FENCE_MODELS,
                ):
                    current_job = lock_current_lease(
                        lease, using=self.using,
                    )
                    current_attempt = ExportAttempt.objects.using(
                        self.using,
                    ).select_for_update().get(pk=attempt.pk)
                    current_file = ExportAttemptFile.objects.using(
                        self.using,
                    ).select_for_update().get(pk=attempt_file.pk)
                    if (
                        current_job.state != "archiving"
                        or current_attempt.state != "closed"
                        or current_file.state != "closed"
                        or current_file.receipt_size != size
                        or current_attempt.exported_at != exported_at
                    ):
                        raise LeaseLost()
                    current_file.state = "verifying"
                    current_file.receipt_sha256 = sha256
                    current_file.save(update_fields=(
                        "state", "receipt_sha256",
                    ))
                    current_attempt.state = "verifying"
                    current_attempt.save(update_fields=("state",))
                    current_job.state = "verifying"
                    current_job.verifying_attempt_generation = (
                        lease.attempt_generation
                    )
                    current_job.verifying_size = size
                    current_job.verifying_sha256 = sha256
                    current_job.verifying_completed_at = exported_at
                    current_job.save(update_fields=(
                        "state", "verifying_attempt_generation",
                        "verifying_size", "verifying_sha256",
                        "verifying_completed_at",
                    ))
                    return current_attempt, current_file

        return self._retry_db_only(
            operation, lease, heartbeat, stop_requested,
        )


__all__ = (
    "ARCHIVE_PART_NAME",
    "AttemptService",
    "attempt_file_cleanup_receipt",
    "closed_file_receipt",
)
