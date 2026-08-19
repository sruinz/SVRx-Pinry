import datetime
import hashlib
import json
import math
import re
import uuid

from dataclasses import dataclass

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone

from core.models import BatchImportItem, Pin


_ERROR_CODE_PATTERN = re.compile(r"\A[a-z][a-z0-9_]{0,63}\Z")
_CAS_REEVALUATION_LIMIT = 4


@dataclass(frozen=True)
class StoredError:
    code: str
    retryable: bool

    def __post_init__(self):
        if not isinstance(self.code, str) or not _ERROR_CODE_PATTERN.match(
            self.code
        ):
            raise ValueError("Stored error codes must use the safe code format.")
        if type(self.retryable) is not bool:
            raise ValueError("Stored error retryability must be boolean.")


@dataclass(frozen=True)
class ClaimResult:
    kind: str
    row_id: object = None
    lease_uuid: object = None
    lease_generation: object = None
    replay_pin_id: object = None
    error: object = None
    retry_after_seconds: object = None


def fingerprint_request(payload):
    canonical = {
        "url": payload["url"],
        "referer": payload.get("referer", ""),
        "description": payload.get("description", ""),
        "private": payload.get("private", False),
        "tags": sorted(set(payload.get("tags", []))),
        "board_ids": sorted(set(payload.get("board_ids", []))),
    }
    encoded = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class IdempotencyStore:
    def __init__(
        self,
        wall_clock=timezone.now,
        lease_seconds=None,
    ):
        if lease_seconds is None:
            lease_seconds = settings.PINRY_IMPORT_LEASE_SECONDS
        if lease_seconds <= 0:
            raise ValueError("The import lease must be positive.")
        self.wall_clock = wall_clock
        self.lease_seconds = lease_seconds

    def claim(
        self,
        user,
        batch_id,
        client_item_id,
        fingerprint,
        now,
    ):
        self._require_aware(now)
        lease_uuid = uuid.uuid4()
        lease_expires_at = now + datetime.timedelta(
            seconds=self.lease_seconds
        )
        insert_error = None
        try:
            with transaction.atomic():
                row = BatchImportItem.objects.create(
                    submitter=user,
                    batch_id=batch_id,
                    client_item_id=client_item_id,
                    request_fingerprint=fingerprint,
                    state=BatchImportItem.PENDING,
                    pin=None,
                    error_code=None,
                    retryable=None,
                    lease_uuid=lease_uuid,
                    lease_generation=1,
                    lease_expires_at=lease_expires_at,
                )
        except IntegrityError as error:
            insert_error = error
        else:
            return self._claimed_result(row.pk, lease_uuid, 1)

        for _attempt in range(_CAS_REEVALUATION_LIMIT):
            try:
                row = self._existing_rows().get(
                    submitter=user,
                    client_item_id=client_item_id,
                )
            except BatchImportItem.DoesNotExist:
                if insert_error is not None:
                    raise insert_error
                return self._internal_failure()

            result = self._evaluate_existing(row, fingerprint, now)
            if result is not None:
                return result

        return self._internal_failure(row_id=row.pk, retryable=True)

    def fence(self, claim):
        current_now = self._current_now()
        return self._owned_pending(claim, current_now).update(
            updated_at=current_now,
        ) == 1

    def record_failure(self, claim, error):
        current_now = self._current_now()
        if not isinstance(error, StoredError):
            error = StoredError(error.code, error.retryable)
        return self._owned_pending(claim, current_now).update(
            state=BatchImportItem.FAILED,
            pin_id=None,
            error_code=error.code,
            retryable=error.retryable,
            lease_uuid=None,
            lease_expires_at=None,
            updated_at=current_now,
        ) == 1

    def record_success(self, claim, pin):
        current_now = self._current_now()
        if pin.pk is None:
            return False
        database_pin = Pin.objects.filter(
            pk=pin.pk,
            submitter_id=OuterRef("submitter_id"),
        )
        owned = self._owned_pending(claim, current_now).annotate(
            pin_owned_by_submitter=Exists(database_pin),
        ).filter(
            pin_owned_by_submitter=True,
        )
        return owned.update(
            state=BatchImportItem.SUCCEEDED,
            pin_id=pin.pk,
            error_code=None,
            retryable=None,
            lease_uuid=None,
            lease_expires_at=None,
            updated_at=current_now,
        ) == 1

    def _evaluate_existing(self, row, fingerprint, now):
        if row.request_fingerprint != fingerprint:
            return ClaimResult(
                kind="conflict",
                row_id=row.pk,
                error=StoredError("idempotency_mismatch", False),
            )

        if row.state == BatchImportItem.SUCCEEDED:
            if not self._valid_succeeded(row):
                return self._internal_failure(row_id=row.pk)
            if row.pin_id is None:
                return ClaimResult(
                    kind="failed",
                    row_id=row.pk,
                    error=StoredError("pin_permanently_deleted", False),
                )
            return ClaimResult(
                kind="replayed",
                row_id=row.pk,
                replay_pin_id=row.pin_id,
            )

        if row.state == BatchImportItem.PENDING:
            if not self._valid_pending(row):
                return self._internal_failure(row_id=row.pk)
            if row.lease_expires_at > now:
                retry_after = max(
                    1,
                    int(
                        math.ceil(
                            (row.lease_expires_at - now).total_seconds()
                        )
                    ),
                )
                return ClaimResult(
                    kind="in_progress",
                    row_id=row.pk,
                    error=StoredError("in_progress", True),
                    retry_after_seconds=retry_after,
                )
            return self._reclaim(row, now, BatchImportItem.PENDING)

        if row.state == BatchImportItem.FAILED:
            if not self._valid_failed(row):
                return self._internal_failure(row_id=row.pk)
            if not row.retryable:
                return ClaimResult(
                    kind="failed",
                    row_id=row.pk,
                    error=StoredError(row.error_code, False),
                )
            return self._reclaim(row, now, BatchImportItem.FAILED)

        return self._internal_failure(row_id=row.pk)

    def _reclaim(self, row, now, expected_state):
        lease_uuid = uuid.uuid4()
        lease_generation = row.lease_generation + 1
        filters = {
            "pk": row.pk,
            "request_fingerprint": row.request_fingerprint,
            "state": expected_state,
            "lease_uuid": row.lease_uuid,
            "lease_generation": row.lease_generation,
        }
        if expected_state == BatchImportItem.PENDING:
            filters["lease_expires_at__lte"] = now
        else:
            filters["retryable"] = True

        updated = BatchImportItem.objects.filter(**filters).update(
            state=BatchImportItem.PENDING,
            pin_id=None,
            error_code=None,
            retryable=None,
            lease_uuid=lease_uuid,
            lease_generation=lease_generation,
            lease_expires_at=now + datetime.timedelta(
                seconds=self.lease_seconds
            ),
            updated_at=now,
        )
        if updated != 1:
            return None
        return self._claimed_result(
            row.pk,
            lease_uuid,
            lease_generation,
        )

    @staticmethod
    def _claimed_result(row_id, lease_uuid, lease_generation):
        return ClaimResult(
            kind="claimed",
            row_id=row_id,
            lease_uuid=lease_uuid,
            lease_generation=lease_generation,
        )

    @staticmethod
    def _internal_failure(row_id=None, retryable=False):
        return ClaimResult(
            kind="failed",
            row_id=row_id,
            error=StoredError("internal_error", retryable),
        )

    @staticmethod
    def _valid_generation(row):
        return isinstance(row.lease_generation, int) and (
            row.lease_generation >= 1
        )

    @classmethod
    def _valid_pending(cls, row):
        return (
            cls._valid_generation(row)
            and row.pin_id is None
            and row.error_code is None
            and row.retryable is None
            and row.lease_uuid is not None
            and row.lease_expires_at is not None
            and not timezone.is_naive(row.lease_expires_at)
        )

    @classmethod
    def _valid_succeeded(cls, row):
        return (
            cls._valid_generation(row)
            and (
                row.pin_id is None
                or row.pin_owned_by_submitter
            )
            and row.error_code is None
            and row.retryable is None
            and row.lease_uuid is None
            and row.lease_expires_at is None
        )

    @staticmethod
    def _existing_rows():
        database_pin = Pin.objects.filter(
            pk=OuterRef("pin_id"),
            submitter_id=OuterRef("submitter_id"),
        )
        return BatchImportItem.objects.annotate(
            pin_owned_by_submitter=Exists(database_pin),
        )

    @classmethod
    def _valid_failed(cls, row):
        return (
            cls._valid_generation(row)
            and row.pin_id is None
            and isinstance(row.error_code, str)
            and _ERROR_CODE_PATTERN.match(row.error_code) is not None
            and type(row.retryable) is bool
            and row.lease_uuid is None
            and row.lease_expires_at is None
        )

    @staticmethod
    def _owned_pending(claim, current_now):
        return BatchImportItem.objects.filter(
            pk=claim.row_id,
            state=BatchImportItem.PENDING,
            pin_id=None,
            error_code=None,
            retryable=None,
            lease_uuid=claim.lease_uuid,
            lease_generation=claim.lease_generation,
            lease_expires_at__gt=current_now,
        )

    def _current_now(self):
        current_now = self.wall_clock()
        self._require_aware(current_now)
        return current_now

    @staticmethod
    def _require_aware(value):
        if not isinstance(value, datetime.datetime) or timezone.is_naive(value):
            raise ValueError("Idempotency timestamps must be timezone-aware.")
