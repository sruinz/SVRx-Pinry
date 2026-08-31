from dataclasses import dataclass
from datetime import timezone
import os
import stat
import time
from urllib.parse import quote

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import DatabaseError

from core.models import Pin
from core.services.database_fence import (
    DatabaseFenceBusy,
    DatabaseFenceDeadline,
    DatabaseFenceError,
    database_write_fence,
)
from exports.contracts import (
    ExportExpired,
    ExportNotFound,
    ExportNotReady,
    ExportTemporarilyUnavailable,
)
from exports.models import ExportItem, ExportJob
from exports.services.file_ops import (
    ClosedFileReceipt,
    DirectoryReceipt,
    ExportStorageError,
    OpenFileReceipt,
    open_export_root,
    open_receipted_directory,
)
from exports.services.permissions import revoked_item_ids


User = get_user_model()
_READY_FIELDS = (
    "ready_relative_path",
    "ready_display_name",
    "ready_size",
    "ready_sha256",
    "ready_dev",
    "ready_ino",
    "ready_uid",
    "ready_gid",
    "ready_mode",
    "ready_nlink",
    "ready_mtime_ns",
    "ready_ctime_ns",
)
_VALIDATION_RETRY_LIMIT = 3
_DISPLAY_NAME_MAX_BYTES = 180
_monotonic = time.monotonic
_sleeper = time.sleep


class _ReceiptMismatch(Exception):
    pass


@dataclass(frozen=True)
class _DownloadExpectation(object):
    job_id: object
    owner_id: int
    expires_at: object
    relative_path: str
    display_name: str
    receipt: ClosedFileReceipt

    @property
    def ready_values(self):
        return (
            self.relative_path,
            self.display_name,
            self.receipt.size,
            self.receipt.sha256,
            self.receipt.dev,
            self.receipt.ino,
            self.receipt.uid,
            self.receipt.gid,
            self.receipt.mode,
            self.receipt.nlink,
            self.receipt.mtime_ns,
            self.receipt.ctime_ns,
        )

    @property
    def physical_receipt(self):
        return ClosedFileReceipt(
            self.receipt.dev,
            self.receipt.ino,
            self.receipt.uid,
            self.receipt.gid,
            self.receipt.mode,
            self.receipt.nlink,
            self.receipt.size,
            self.receipt.mtime_ns,
            self.receipt.ctime_ns,
        )

    def cas_filter(self):
        values = dict(zip(_READY_FIELDS, self.ready_values))
        values.update({
            "pk": self.job_id,
            "owner_id": self.owner_id,
            "state": "complete",
            "ready_cleanup_state": "retained",
            "expires_at": self.expires_at,
        })
        return values


def _expectation_for(job_id, owner_id):
    try:
        job = ExportJob.objects.get(pk=job_id, owner_id=owner_id)
    except ExportJob.DoesNotExist:
        raise ExportNotFound() from None
    except DatabaseError:
        raise ExportTemporarilyUnavailable() from None
    if job.state == "expired":
        raise ExportExpired()
    if job.state != "complete":
        raise ExportNotReady()
    return _DownloadExpectation(
        job.pk,
        owner_id,
        job.expires_at,
        job.ready_relative_path,
        job.ready_display_name,
        ClosedFileReceipt(
            job.ready_dev,
            job.ready_ino,
            job.ready_uid,
            job.ready_gid,
            job.ready_mode,
            job.ready_nlink,
            job.ready_size,
            job.ready_mtime_ns,
            job.ready_ctime_ns,
            job.ready_sha256,
        ),
    )


def _open_ready_directory(root):
    try:
        current = os.stat(
            "ready",
            dir_fd=root.descriptor,
            follow_symlinks=False,
        )
    except OSError:
        raise _ReceiptMismatch() from None
    receipt = DirectoryReceipt(
        current.st_dev,
        current.st_ino,
        current.st_uid,
        current.st_gid,
        stat.S_IMODE(current.st_mode),
    )
    try:
        ready = open_receipted_directory(root, "ready", receipt)
        safe_receipt = DirectoryReceipt.from_fd(
            ready.descriptor,
            root.uid,
            root.gid,
        )
        if safe_receipt != receipt:
            ready.close()
            raise _ReceiptMismatch()
        return ready
    except ExportStorageError:
        raise _ReceiptMismatch() from None


def _observe_ready_file(expectation):
    canonical_relative_path = "ready/{}.zip".format(expectation.job_id)
    if expectation.relative_path != canonical_relative_path:
        raise _ReceiptMismatch()
    root = ready = None
    descriptor = None
    try:
        root = open_export_root(
            settings.PINRY_EXPORT_ROOT,
            os.getuid(),
            os.getgid(),
        )
        ready = _open_ready_directory(root)
        leaf = "{}.zip".format(expectation.job_id)
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        descriptor = os.open(leaf, flags, dir_fd=ready.descriptor)
        opened = OpenFileReceipt.from_fd(
            descriptor,
            ready.uid,
            ready.gid,
        )
        observed = ClosedFileReceipt.from_open_fd(descriptor, opened)
        named = os.stat(
            leaf,
            dir_fd=ready.descriptor,
            follow_symlinks=False,
        )
        if not observed.matches_stat(named):
            raise _ReceiptMismatch()
        ready.verify_identity()
    except (_ReceiptMismatch, ExportStorageError, OSError):
        raise _ReceiptMismatch() from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if ready is not None:
            ready.close()
        if root is not None:
            root.close()
    if observed != expectation.physical_receipt:
        raise _ReceiptMismatch()
    return observed


def _ready_values(job):
    return tuple(getattr(job, field) for field in _READY_FIELDS)


def _classify_changed_job(current, expectation):
    if (
        current is None
        or current.owner_id != expectation.owner_id
        or not User.objects.filter(pk=expectation.owner_id).exists()
    ):
        return "not_found", None
    if current.state == "expired":
        return "expired", None
    if current.state != "complete":
        return "not_ready", None
    if current.ready_cleanup_state != "retained":
        return "expired", None
    if (
        current.expires_at != expectation.expires_at
        or _ready_values(current) != expectation.ready_values
    ):
        return "retry", None
    return "retry", None


def _mark_blocked_if_unchanged(expectation, deadline):
    with database_write_fence(
        using="default",
        models=(ExportJob,),
    ):
        updated = ExportJob.objects.filter(
            **expectation.cas_filter()
        ).update(
            state="expired",
            ready_cleanup_state="blocked",
        )
        deadline.checkpoint()
        if updated == 1:
            return "expired", None
        current = ExportJob.objects.filter(pk=expectation.job_id).first()
        return _classify_changed_job(current, expectation)


def _final_authorize(expectation, observed, clock, deadline):
    with database_write_fence(
        using="default",
        models=(User, Pin, ExportJob, ExportItem),
    ):
        try:
            current = ExportJob.objects.select_for_update().get(
                pk=expectation.job_id,
            )
        except ExportJob.DoesNotExist:
            return "not_found", None
        authorization_now = clock()
        if (
            current.owner_id != expectation.owner_id
            or not User.objects.filter(pk=expectation.owner_id).exists()
        ):
            return "not_found", None
        if current.state == "expired":
            return "expired", None
        if current.state != "complete":
            return "not_ready", None
        if current.ready_cleanup_state != "retained":
            return "expired", None
        if (
            current.expires_at != expectation.expires_at
            or _ready_values(current) != expectation.ready_values
            or observed != expectation.physical_receipt
        ):
            return "retry", None
        if (
            current.expires_at is None
            or current.expires_at <= authorization_now
        ):
            current.state = "expired"
            current.ready_cleanup_state = "pending"
            current.save(update_fields=("state", "ready_cleanup_state"))
            deadline.checkpoint()
            return "expired", None
        revoked = revoked_item_ids(
            current,
            inclusion_state="included",
            using="default",
            checkpoint=deadline.checkpoint,
        )
        owner_exists = User.objects.filter(
            pk=expectation.owner_id,
        ).exists()
        deadline.checkpoint()
        decision_now = clock()
        if (
            current.owner_id != expectation.owner_id
            or not owner_exists
        ):
            return "not_found", None
        if (
            current.expires_at <= decision_now
            or revoked
        ):
            current.state = "expired"
            current.ready_cleanup_state = "pending"
            current.save(update_fields=("state", "ready_cleanup_state"))
            deadline.checkpoint()
            return "expired", None
        return "authorized", current


def _retry_database(operation):
    deadline = DatabaseFenceDeadline(_monotonic)
    while True:
        try:
            return operation(deadline)
        except DatabaseFenceDeadline:
            raise ExportTemporarilyUnavailable() from None
        except DatabaseFenceBusy:
            try:
                deadline.checkpoint()
            except DatabaseFenceDeadline:
                raise ExportTemporarilyUnavailable() from None
            _sleeper(0.005)
        except (DatabaseFenceError, DatabaseError):
            raise ExportTemporarilyUnavailable() from None


def authorize_download(job_id, owner_id, clock):
    if not callable(clock):
        raise TypeError("clock must be callable")
    for unused_attempt in range(_VALIDATION_RETRY_LIMIT):
        expectation = _expectation_for(job_id, owner_id)
        try:
            observed = _observe_ready_file(expectation)
        except _ReceiptMismatch:
            outcome, job = _retry_database(
                lambda deadline: _mark_blocked_if_unchanged(
                    expectation,
                    deadline,
                )
            )
        else:
            outcome, job = _retry_database(
                lambda deadline: _final_authorize(
                    expectation,
                    observed,
                    clock,
                    deadline,
                )
            )
        if outcome == "retry":
            continue
        if outcome == "not_found":
            raise ExportNotFound()
        if outcome == "not_ready":
            raise ExportNotReady()
        if outcome == "expired":
            raise ExportExpired()
        return job
    raise ExportTemporarilyUnavailable()


def content_disposition(job):
    fallback = "svrx-pinry-export-{}.zip".format(job.pk)
    completed_at = job.completed_at
    if completed_at is None:
        display_name = fallback
    else:
        suffix = "-내보내기-{}.zip".format(
            completed_at.astimezone(timezone.utc).strftime(
                "%Y%m%dT%H%M%SZ"
            )
        )
        raw_name = job.ready_display_name or "export"
        prefix = (
            raw_name[:-len(suffix)]
            if raw_name.endswith(suffix)
            else raw_name
        )
        byte_limit = _DISPLAY_NAME_MAX_BYTES - len(
            suffix.encode("utf-8")
        )
        prefix = prefix.encode("utf-8", "replace")[:byte_limit].decode(
            "utf-8",
            "ignore",
        )
        display_name = "{}{}".format(prefix, suffix)
    return "attachment; filename=\"{}\"; filename*=UTF-8''{}".format(
        fallback,
        quote(display_name, safe=""),
    )


__all__ = (
    "authorize_download",
    "content_disposition",
)
