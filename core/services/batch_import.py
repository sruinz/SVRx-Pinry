import logging
import re
import time
import uuid

from django.conf import settings
from django.db import OperationalError, connection
from django.utils import timezone

from core.services.idempotency import StoredError, fingerprint_request
from core.services.media_storage import MediaStorageError
from core.services.pin_import import ImportMetadata, PinImportError
from core.services.safe_url_fetch import SafeFetchError


logger = logging.getLogger(__name__)

_ITEM_DEADLINE_SECONDS = 12
_SQLITE_BUSY_WORD = re.compile(r"\b(?:busy|locked)\b")
_SAFE_MESSAGES = {
    "batch_deadline_exceeded": (
        "The batch deadline does not allow another item."
    ),
    "in_progress": "This item is already being imported.",
    "idempotency_mismatch": (
        "This item identifier was already used for different input."
    ),
    "pin_permanently_deleted": (
        "The previously imported pin is no longer available."
    ),
    "lease_lost": "The import lease is no longer owned by this request.",
    "board_access_changed": (
        "Board access changed while the item was being imported."
    ),
    "database_busy": "The database is temporarily busy.",
    "internal_error": "The image import could not be completed safely.",
    "unsupported_http_stack": (
        "The configured HTTP stack is not supported."
    ),
    "invalid_url_policy": "The image URL is not allowed.",
    "blocked_address": "The image URL resolves to a blocked address.",
    "dns_rebinding_detected": (
        "The connected address did not match the validated address."
    ),
    "too_many_redirects": "The image URL redirected too many times.",
    "image_download_failed": "The image could not be downloaded.",
    "image_fetch_timeout": "The image download timed out.",
    "unsupported_content_encoding": (
        "The image response used an unsupported content encoding."
    ),
    "image_too_large": "The image exceeded the download size limit.",
    "image_too_many_pixels": "The image exceeded the pixel limit.",
    "invalid_image_content": "The response was not a valid image.",
    "unsupported_image_format": (
        "The response image format is not supported."
    ),
    "image_processing_timeout": (
        "The image could not be processed before the deadline."
    ),
    "media_path_conflict": (
        "The media destination could not be used safely."
    ),
    "image_processing_failed": (
        "The image could not be processed safely."
    ),
    "media_configuration_error": (
        "The image storage configuration is invalid."
    ),
    "media_publish_changed": (
        "The published media changed before it could be committed."
    ),
    "media_storage_failed": (
        "The media storage is temporarily unavailable."
    ),
    "media_storage_unsupported": (
        "Atomic media publishing is not supported on this system."
    ),
}
_FIXED_RETRYABILITY = {
    "batch_deadline_exceeded": True,
    "in_progress": True,
    "idempotency_mismatch": False,
    "pin_permanently_deleted": False,
    "lease_lost": True,
    "board_access_changed": False,
    "database_busy": True,
    "internal_error": False,
    "unsupported_http_stack": False,
    "invalid_url_policy": False,
    "blocked_address": False,
    "dns_rebinding_detected": False,
    "too_many_redirects": False,
    "image_fetch_timeout": True,
    "unsupported_content_encoding": False,
    "image_too_large": False,
    "image_too_many_pixels": False,
    "invalid_image_content": False,
    "unsupported_image_format": False,
    "image_processing_timeout": True,
    "media_path_conflict": False,
    "image_processing_failed": False,
    "media_configuration_error": False,
    "media_publish_changed": False,
    "media_storage_failed": True,
    "media_storage_unsupported": False,
}


class _ClaimFailure(object):
    def __init__(self, result):
        self.result = result


class BatchImportService(object):
    def __init__(
        self,
        fetcher,
        media_storage,
        idempotency,
        pin_import,
        clock=time.monotonic,
        wall_clock=timezone.now,
        fault_injector=None,
    ):
        if (
            getattr(pin_import, "fetcher", None) is not fetcher
            or getattr(pin_import, "media_storage", None) is not media_storage
            or getattr(pin_import, "idempotency", None) is not idempotency
        ):
            raise ValueError("Batch import dependencies must share one graph.")
        self.fetcher = fetcher
        self.media_storage = media_storage
        self.idempotency = idempotency
        self.pin_import = pin_import
        self.clock = clock
        self.wall_clock = wall_clock
        self.fault_injector = fault_injector
        self._closed = False

    def process(self, user, validated_data, started_at):
        batch_deadline = (
            started_at + settings.PINRY_BATCH_DEADLINE_SECONDS
        )
        results = []
        for item in validated_data["items"]:
            item_started_at = self.clock()
            if batch_deadline - item_started_at < _ITEM_DEADLINE_SECONDS:
                results.append(self._error_result(
                    item["client_item_id"],
                    "failed",
                    StoredError("batch_deadline_exceeded", True),
                ))
                continue
            item_deadline = min(
                batch_deadline,
                item_started_at + _ITEM_DEADLINE_SECONDS,
            )
            metadata = ImportMetadata(
                url=item["url"],
                referer=validated_data["referer"],
                description=validated_data["description"],
                private=validated_data["private"],
                tags=tuple(validated_data["tags"]),
                board_ids=tuple(validated_data["board_ids"]),
            )
            claim = self._claim(
                user,
                validated_data["batch_id"],
                item["client_item_id"],
                metadata,
            )
            if isinstance(claim, _ClaimFailure):
                results.append(claim.result)
                continue
            mapped = self._map_claim(item["client_item_id"], claim)
            if mapped is not None:
                results.append(mapped)
                continue
            item_result = self.process_item(
                user,
                claim,
                metadata,
                item_deadline,
            )
            item_result["client_item_id"] = str(item["client_item_id"])
            results.append(item_result)

        summary = {
            "created": 0,
            "replayed": 0,
            "failed": 0,
            "conflict": 0,
        }
        for result in results:
            summary[result["status"]] += 1
        return {
            "batch_id": str(validated_data["batch_id"]),
            "results": results,
            "summary": summary,
        }

    def process_item(
        self,
        user,
        claim,
        metadata,
        item_deadline,
    ):
        try:
            prepared = self.pin_import.prepare_url(
                metadata.url,
                metadata.referer,
                item_deadline,
            )
            if self.clock() >= item_deadline:
                self._cleanup_prepared(prepared)
                raise PinImportError(
                    "image_processing_timeout",
                    "The image could not be processed before the deadline.",
                    True,
                )
            pin = self.pin_import.commit(
                prepared,
                user,
                metadata,
                claim,
                item_deadline,
            )
        except PinImportError as error:
            if error.code == "lease_lost":
                return self._error_result(
                    None,
                    "conflict",
                    StoredError("lease_lost", True),
                )
            return self._record_failure(None, claim, error)
        except (SafeFetchError, MediaStorageError) as error:
            return self._record_failure(None, claim, error)
        except OperationalError as error:
            mapped = self._database_error(error, "process_item")
            return self._record_failure(None, claim, mapped)
        except Exception as error:
            self._log_internal("process_item", error)
            return self._record_failure(
                None,
                claim,
                StoredError("internal_error", False),
            )

        self._fault("after_commit")
        return {
            "status": "created",
            "pin_id": pin.pk,
        }

    def close(self):
        if self._closed:
            return
        self._closed = True
        transport = getattr(self.fetcher, "transport", None)
        close = getattr(transport, "close", None)
        if close is None:
            return
        try:
            close()
        except BaseException as error:
            self._log_internal("transport_close", error)

    def _claim(self, user, batch_id, client_item_id, metadata):
        fingerprint = fingerprint_request({
            "url": metadata.url,
            "referer": metadata.referer,
            "description": metadata.description,
            "private": metadata.private,
            "tags": metadata.tags,
            "board_ids": metadata.board_ids,
        })
        try:
            return self.idempotency.claim(
                user,
                batch_id,
                client_item_id,
                fingerprint,
                self.wall_clock(),
            )
        except OperationalError as error:
            mapped = self._database_error(error, "claim")
            return _ClaimFailure(self._error_result(
                client_item_id,
                "failed",
                mapped,
            ))
        except Exception as error:
            self._log_internal("claim", error)
            return _ClaimFailure(self._error_result(
                client_item_id,
                "failed",
                StoredError("internal_error", False),
            ))

    def _map_claim(self, client_item_id, claim):
        kind = getattr(claim, "kind", None)
        if kind == "claimed":
            if (
                type(getattr(claim, "row_id", None)) is not int
                or claim.row_id <= 0
                or not isinstance(
                    getattr(claim, "lease_uuid", None), uuid.UUID
                )
                or type(getattr(claim, "lease_generation", None)) is not int
                or claim.lease_generation <= 0
            ):
                return self._internal_claim_error(client_item_id)
            return None
        if kind == "replayed":
            replay_pin_id = getattr(claim, "replay_pin_id", None)
            if type(replay_pin_id) is not int or replay_pin_id <= 0:
                return self._internal_claim_error(client_item_id)
            return {
                "client_item_id": str(client_item_id),
                "status": "replayed",
                "pin_id": replay_pin_id,
            }
        if kind == "in_progress":
            error = getattr(claim, "error", None)
            retry_after = getattr(claim, "retry_after_seconds", None)
            if (
                getattr(error, "code", None) != "in_progress"
                or getattr(error, "retryable", None) is not True
                or type(retry_after) is not int
                or retry_after <= 0
            ):
                return self._internal_claim_error(client_item_id)
            return self._error_result(
                client_item_id,
                "conflict",
                StoredError("in_progress", True),
                retry_after_seconds=retry_after,
            )
        if kind == "conflict":
            claimed_error = getattr(claim, "error", None)
            if (
                getattr(claimed_error, "code", None)
                != "idempotency_mismatch"
                or getattr(claimed_error, "retryable", None) is not False
            ):
                return self._internal_claim_error(client_item_id)
            return self._error_result(
                client_item_id,
                "conflict",
                StoredError("idempotency_mismatch", False),
            )
        if kind == "failed":
            error = self._safe_stored_error(getattr(claim, "error", None))
            if error.code in ("in_progress", "idempotency_mismatch"):
                return self._internal_claim_error(client_item_id)
            status = "conflict" if error.code == "lease_lost" else "failed"
            return self._error_result(
                client_item_id,
                status,
                error,
            )
        return self._internal_claim_error(client_item_id)

    @staticmethod
    def _internal_claim_error(client_item_id):
        return BatchImportService._error_result(
            client_item_id,
            "failed",
            StoredError("internal_error", False),
        )

    def _record_failure(self, client_item_id, claim, error):
        stored = self._safe_stored_error(error)
        try:
            recorded = self.idempotency.record_failure(claim, stored)
        except OperationalError as database_error:
            return self._error_result(
                client_item_id,
                "failed",
                self._database_error(database_error, "record_failure"),
            )
        except Exception as internal_error:
            self._log_internal("record_failure", internal_error)
            return self._error_result(
                client_item_id,
                "failed",
                StoredError("internal_error", False),
            )
        if not recorded:
            return self._error_result(
                client_item_id,
                "conflict",
                StoredError("lease_lost", True),
            )
        status = "conflict" if stored.code == "lease_lost" else "failed"
        return self._error_result(client_item_id, status, stored)

    @staticmethod
    def _safe_stored_error(error):
        code = getattr(error, "code", None)
        retryable = getattr(error, "retryable", None)
        if code not in _SAFE_MESSAGES or type(retryable) is not bool:
            return StoredError("internal_error", False)
        retryable = _FIXED_RETRYABILITY.get(code, retryable)
        return StoredError(code, retryable)

    @staticmethod
    def _error_result(
        client_item_id,
        status,
        error,
        retry_after_seconds=None,
    ):
        safe_error = BatchImportService._safe_stored_error(error)
        detail = {
            "code": safe_error.code,
            "message": _SAFE_MESSAGES[safe_error.code],
            "retryable": safe_error.retryable,
        }
        if retry_after_seconds is not None:
            detail["retry_after_seconds"] = retry_after_seconds
        result = {
            "status": status,
            "error": detail,
        }
        if client_item_id is not None:
            result["client_item_id"] = str(client_item_id)
        return result

    @staticmethod
    def _database_error(error, event):
        message = str(error).lower()
        if (
            connection.vendor == "sqlite"
            and _SQLITE_BUSY_WORD.search(message)
        ):
            return StoredError("database_busy", True)
        BatchImportService._log_internal(event, error)
        return StoredError("internal_error", False)

    @staticmethod
    def _cleanup_prepared(prepared):
        for _attempt in range(2):
            try:
                prepared.cleanup()
            except BaseException as error:
                BatchImportService._log_internal(
                    "prepared_cleanup",
                    error,
                )
            try:
                if not prepared.is_open:
                    break
            except AttributeError:
                break
            except BaseException as error:
                BatchImportService._log_internal(
                    "prepared_cleanup_state",
                    error,
                )

    def _fault(self, point):
        if self.fault_injector is not None:
            self.fault_injector(point)

    @staticmethod
    def _log_internal(event, error):
        try:
            logger.error(
                "batch_import_internal_error event=%s error_type=%s",
                event,
                type(error).__name__,
            )
        except BaseException:
            pass
