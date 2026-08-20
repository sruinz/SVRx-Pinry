import json
from io import BytesIO
import uuid

import mock
from django.db import OperationalError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import NoReverseMatch, resolve, reverse
from django.utils import timezone
from rest_framework.exceptions import ParseError
from rest_framework.test import APIRequestFactory, force_authenticate
from taggit.models import Tag

from core.batch_serializers import BatchImportRequestSerializer
from core.models import Board
from core.parsers import LimitedJSONParser
from core.services.batch_import import BatchImportService
from core.services.idempotency import ClaimResult, StoredError
from core.services.pin_import import PinImportError
from core.services.safe_url_fetch import SafeFetchError
from core.tests.helpers import create_user
from core.views import PinViewSet


class RecordingStream(object):
    def __init__(self, content, short_read=None):
        self._stream = BytesIO(content)
        self.short_read = short_read
        self.read_sizes = []
        self.total_read = 0

    def read(self, size=-1):
        self.read_sizes.append(size)
        if self.short_read is not None:
            size = min(size, self.short_read)
        chunk = self._stream.read(size)
        self.total_read += len(chunk)
        return chunk


class MaliciousStream(object):
    def __init__(self, value):
        self.value = value
        self.read_count = 0

    def read(self, _size=-1):
        self.read_count += 1
        return self.value


class LimitedJSONParserTests(SimpleTestCase):
    def test_valid_json_is_parsed_with_bounded_reads(self):
        body = json.dumps({"items": [1]}).encode("utf-8")
        stream = RecordingStream(body)

        parsed = LimitedJSONParser(max_bytes=64).parse(stream)

        self.assertEqual(parsed, {"items": [1]})
        self.assertTrue(stream.read_sizes)
        self.assertLessEqual(max(stream.read_sizes), 64 * 1024)
        self.assertLessEqual(stream.total_read, 65)

    def test_exact_limit_body_is_allowed(self):
        body = b'"' + (b"a" * 14) + b'"'
        stream = RecordingStream(body)

        parsed = LimitedJSONParser(max_bytes=16).parse(stream)

        self.assertEqual(parsed, "a" * 14)
        self.assertLessEqual(stream.total_read, 17)

    def test_body_over_limit_is_rejected_after_at_most_max_plus_one(self):
        stream = RecordingStream(b'"' + (b"a" * 32) + b'"')

        with self.assertRaises(ParseError) as raised:
            LimitedJSONParser(max_bytes=16).parse(stream)

        self.assertEqual(
            raised.exception.detail["code"],
            "batch_body_too_large",
        )
        self.assertLessEqual(stream.total_read, 17)

    def test_short_reads_are_repeated_until_complete_json(self):
        body = b'{"batch_id":"ok"}'
        stream = RecordingStream(body, short_read=2)

        parsed = LimitedJSONParser(max_bytes=64).parse(stream)

        self.assertEqual(parsed, {"batch_id": "ok"})
        self.assertGreater(len(stream.read_sizes), 2)

    def test_empty_body_is_reported_as_invalid_json(self):
        with self.assertRaises(ParseError) as raised:
            LimitedJSONParser(max_bytes=64).parse(RecordingStream(b""))

        self.assertEqual(
            raised.exception.detail["code"],
            "batch_invalid_json",
        )

    def test_stream_returning_more_than_requested_fails_closed(self):
        stream = MaliciousStream(b"x" * 128)

        with self.assertRaises(ParseError) as raised:
            LimitedJSONParser(max_bytes=16).parse(stream)

        self.assertEqual(
            raised.exception.detail["code"],
            "batch_body_too_large",
        )
        self.assertEqual(stream.read_count, 1)

    def test_stream_returning_non_bytes_fails_as_invalid_json(self):
        stream = MaliciousStream("not bytes")

        with self.assertRaises(ParseError) as raised:
            LimitedJSONParser(max_bytes=16).parse(stream)

        self.assertEqual(
            raised.exception.detail["code"],
            "batch_invalid_json",
        )
        self.assertEqual(stream.read_count, 1)

    def test_deeply_nested_json_is_reported_as_invalid_json(self):
        body = (b"[" * 2000) + (b"]" * 2000)
        stream = RecordingStream(body)

        with self.assertRaises(ParseError) as raised:
            LimitedJSONParser(max_bytes=len(body)).parse(stream)

        self.assertEqual(
            raised.exception.detail["code"],
            "batch_invalid_json",
        )
        self.assertLessEqual(stream.total_read, len(body))


class BatchImportRequestSerializerTests(TestCase):
    def _payload(self):
        return {
            "batch_id": str(uuid.uuid4()),
            "board_ids": [9, 2, 9],
            "tags": ["zeta", "alpha", "zeta"],
            "private": True,
            "referer": "https://example.com/gallery",
            "description": "archive",
            "items": [
                {
                    "client_item_id": str(uuid.uuid4()),
                    "url": "https://example.com/second.png",
                },
                {
                    "client_item_id": str(uuid.uuid4()),
                    "url": "https://example.com/first.png",
                },
            ],
        }

    def test_normalizes_shared_metadata_without_reordering_items(self):
        payload = self._payload()
        expected_item_ids = [
            item["client_item_id"] for item in payload["items"]
        ]
        serializer = BatchImportRequestSerializer(data=payload)

        self.assertTrue(serializer.is_valid(), serializer.errors)

        validated = serializer.validated_data
        self.assertEqual(validated["board_ids"], [2, 9])
        self.assertEqual(validated["tags"], ["alpha", "zeta"])
        self.assertEqual(
            [str(item["client_item_id"]) for item in validated["items"]],
            expected_item_ids,
        )

    def test_duplicate_client_item_id_is_rejected_without_tag_writes(self):
        payload = self._payload()
        payload["items"][1]["client_item_id"] = payload["items"][0][
            "client_item_id"
        ]
        serializer = BatchImportRequestSerializer(data=payload)

        self.assertFalse(serializer.is_valid())

        self.assertIn("items", serializer.errors)
        self.assertEqual(Tag.objects.count(), 0)
        self.assertEqual(Board.objects.count(), 0)

    def test_valid_serializer_does_not_create_tags(self):
        serializer = BatchImportRequestSerializer(data=self._payload())

        self.assertTrue(serializer.is_valid(), serializer.errors)

        self.assertEqual(Tag.objects.count(), 0)
        self.assertEqual(Board.objects.count(), 0)

    def test_eleven_items_are_rejected(self):
        payload = self._payload()
        payload["items"] = [
            {
                "client_item_id": str(uuid.uuid4()),
                "url": "https://example.com/{}.png".format(index),
            }
            for index in range(11)
        ]

        serializer = BatchImportRequestSerializer(data=payload)

        self.assertFalse(serializer.is_valid())
        self.assertIn("items", serializer.errors)

    def test_empty_items_are_rejected(self):
        payload = self._payload()
        payload["items"] = []

        serializer = BatchImportRequestSerializer(data=payload)

        self.assertFalse(serializer.is_valid())
        self.assertIn("items", serializer.errors)


class FakePrepared(object):
    def __init__(self):
        self.cleanup_count = 0

    def cleanup(self):
        self.cleanup_count += 1


class FakePin(object):
    def __init__(self, pk):
        self.pk = pk


class FakeFetcher(object):
    def __init__(self):
        self.transport = FakeTransport()


class FakeTransport(object):
    def __init__(self, close_error=None):
        self.close_count = 0
        self.close_error = close_error

    def close(self):
        self.close_count += 1
        if self.close_error is not None:
            raise self.close_error


class FakeIdempotency(object):
    def __init__(self, claim_results):
        self.claim_results = list(claim_results)
        self.claim_calls = []
        self.failure_calls = []
        self.failure_result = True
        self.failure_error = None
        self.claim_error = None

    def claim(self, user, batch_id, client_item_id, fingerprint, now):
        self.claim_calls.append({
            "user": user,
            "batch_id": batch_id,
            "client_item_id": client_item_id,
            "fingerprint": fingerprint,
            "now": now,
        })
        if self.claim_error is not None:
            raise self.claim_error
        return self.claim_results.pop(0)

    def record_failure(self, claim, error):
        self.failure_calls.append((claim, error))
        if self.failure_error is not None:
            raise self.failure_error
        return self.failure_result


class FakePinImport(object):
    def __init__(self, fetcher, media_storage, idempotency):
        self.fetcher = fetcher
        self.media_storage = media_storage
        self.idempotency = idempotency
        self.prepare_calls = []
        self.commit_calls = []
        self.prepare_results = []
        self.commit_results = []

    def prepare_url(self, url, referer, deadline):
        self.prepare_calls.append((url, referer, deadline))
        result = self.prepare_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def commit(self, prepared, user, metadata, claim, deadline):
        self.commit_calls.append(
            (prepared, user, metadata, claim, deadline)
        )
        result = self.commit_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class ManualClock(object):
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


class SequenceClock(object):
    def __init__(self, values):
        self.values = list(values)
        self.last = self.values[-1]

    def __call__(self):
        if self.values:
            self.last = self.values.pop(0)
        return self.last


class BatchImportServiceTests(SimpleTestCase):
    def _claim(self, row_id=1):
        return ClaimResult(
            kind="claimed",
            row_id=row_id,
            lease_uuid=uuid.uuid4(),
            lease_generation=1,
        )

    def _data(self, item_count=1):
        return {
            "batch_id": uuid.uuid4(),
            "board_ids": [2, 9],
            "tags": ["alpha", "zeta"],
            "private": True,
            "referer": "https://example.com/gallery",
            "description": "archive",
            "items": [
                {
                    "client_item_id": uuid.uuid4(),
                    "url": "https://example.com/{}.png".format(index),
                }
                for index in range(item_count)
            ],
        }

    def _service(self, claim_results, clock=None, fault_injector=None):
        fetcher = FakeFetcher()
        media_storage = object()
        idempotency = FakeIdempotency(claim_results)
        pin_import = FakePinImport(
            fetcher,
            media_storage,
            idempotency,
        )
        service = BatchImportService(
            fetcher,
            media_storage,
            idempotency,
            pin_import,
            clock=clock or ManualClock(0),
            wall_clock=lambda: timezone.now(),
            fault_injector=fault_injector,
        )
        return service, idempotency, pin_import, fetcher.transport

    def test_created_and_failed_items_keep_order_and_summary(self):
        claims = [self._claim(1), self._claim(2)]
        service, store, pin_import, _transport = self._service(claims)
        first_prepared = FakePrepared()
        pin_import.prepare_results = [
            first_prepared,
            SafeFetchError(
                "invalid_image_content",
                "unsafe upstream detail",
                False,
            ),
        ]
        pin_import.commit_results = [FakePin(41)]
        data = self._data(item_count=2)

        result = service.process(object(), data, started_at=0)

        self.assertEqual(
            [item["status"] for item in result["results"]],
            ["created", "failed"],
        )
        self.assertEqual(result["results"][0]["pin_id"], 41)
        self.assertEqual(
            result["results"][1]["error"]["code"],
            "invalid_image_content",
        )
        self.assertNotIn("unsafe upstream detail", json.dumps(result))
        self.assertEqual(
            result["summary"],
            {"created": 1, "replayed": 0, "failed": 1, "conflict": 0},
        )
        self.assertEqual(len(store.failure_calls), 1)

    def test_claim_outcomes_skip_prepare_and_use_stable_statuses(self):
        claims = [
            ClaimResult(kind="replayed", replay_pin_id=7),
            ClaimResult(
                kind="in_progress",
                error=StoredError("in_progress", True),
                retry_after_seconds=4,
            ),
            ClaimResult(
                kind="conflict",
                error=StoredError("idempotency_mismatch", False),
            ),
            ClaimResult(
                kind="failed",
                error=StoredError("pin_permanently_deleted", False),
            ),
        ]
        service, _store, pin_import, _transport = self._service(claims)

        result = service.process(object(), self._data(4), started_at=0)

        self.assertEqual(
            [item["status"] for item in result["results"]],
            ["replayed", "conflict", "conflict", "failed"],
        )
        self.assertEqual(result["results"][0]["pin_id"], 7)
        self.assertEqual(
            result["results"][1]["error"]["retry_after_seconds"],
            4,
        )
        self.assertEqual(pin_import.prepare_calls, [])

    def test_stored_lease_loss_is_a_retryable_conflict(self):
        claim = ClaimResult(
            kind="failed",
            error=StoredError("lease_lost", True),
        )
        service, _store, pin_import, _transport = self._service([claim])

        result = service.process(object(), self._data(), started_at=0)

        self.assertEqual(result["results"][0]["status"], "conflict")
        self.assertEqual(
            result["results"][0]["error"],
            {
                "code": "lease_lost",
                "message": (
                    "The import lease is no longer owned by this request."
                ),
                "retryable": True,
            },
        )
        self.assertEqual(
            result["summary"],
            {"created": 0, "replayed": 0, "failed": 0, "conflict": 1},
        )
        self.assertEqual(pin_import.prepare_calls, [])

    def test_item_is_not_claimed_without_a_full_twelve_second_budget(self):
        clock = ManualClock(34)
        service, store, pin_import, _transport = self._service(
            [], clock=clock
        )

        result = service.process(object(), self._data(), started_at=0)

        self.assertEqual(store.claim_calls, [])
        self.assertEqual(pin_import.prepare_calls, [])
        self.assertEqual(result["results"][0]["status"], "failed")
        self.assertEqual(
            result["results"][0]["error"],
            {
                "code": "batch_deadline_exceeded",
                "message": "The batch deadline does not allow another item.",
                "retryable": True,
            },
        )

    def test_next_item_is_not_claimed_after_first_consumes_batch_budget(self):
        clock = SequenceClock([0, 0, 34])
        service, store, pin_import, _transport = self._service(
            [self._claim()], clock=clock
        )
        pin_import.prepare_results = [FakePrepared()]
        pin_import.commit_results = [FakePin(3)]

        result = service.process(
            object(), self._data(item_count=2), started_at=0
        )

        self.assertEqual(
            [item["status"] for item in result["results"]],
            ["created", "failed"],
        )
        self.assertEqual(
            result["results"][1]["error"]["code"],
            "batch_deadline_exceeded",
        )
        self.assertEqual(len(store.claim_calls), 1)
        self.assertEqual(len(pin_import.prepare_calls), 1)

    def test_one_absolute_deadline_is_reused_for_prepare_and_commit(self):
        service, _store, pin_import, _transport = self._service(
            [self._claim()]
        )
        prepared = FakePrepared()
        pin_import.prepare_results = [prepared]
        pin_import.commit_results = [FakePin(3)]

        service.process(object(), self._data(), started_at=0)

        self.assertEqual(pin_import.prepare_calls[0][2], 12)
        self.assertEqual(pin_import.commit_calls[0][4], 12)

    def test_claim_fingerprint_and_commit_metadata_include_every_field(self):
        batch_id = uuid.UUID("10000000-0000-0000-0000-000000000001")
        item_id = uuid.UUID("20000000-0000-0000-0000-000000000001")
        raw_data = {
            "batch_id": str(batch_id),
            "board_ids": [9, 2, 9],
            "tags": ["zeta", "alpha", "zeta"],
            "private": True,
            "referer": "https://example.com/gallery",
            "description": "archive",
            "items": [{
                "client_item_id": str(item_id),
                "url": "https://example.com/0.png",
            }],
        }
        serializer = BatchImportRequestSerializer(data=raw_data)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        claim = self._claim()
        service, store, pin_import, _transport = self._service([claim])
        pin_import.prepare_results = [FakePrepared()]
        pin_import.commit_results = [FakePin(3)]
        user = object()

        service.process(user, serializer.validated_data, started_at=0)

        claim_call = store.claim_calls[0]
        self.assertIs(claim_call["user"], user)
        self.assertEqual(claim_call["batch_id"], batch_id)
        self.assertEqual(claim_call["client_item_id"], item_id)
        self.assertEqual(
            claim_call["fingerprint"],
            "4d53f3cbaddb9415ba3a786c02028542f2b0a2dd2dcbe421c5dfb59a8ab0b026",
        )
        prepared, commit_user, metadata, commit_claim, deadline = (
            pin_import.commit_calls[0]
        )
        self.assertIsInstance(prepared, FakePrepared)
        self.assertIs(commit_user, user)
        self.assertIs(commit_claim, claim)
        self.assertEqual(deadline, 12)
        self.assertEqual(metadata.url, "https://example.com/0.png")
        self.assertEqual(metadata.referer, "https://example.com/gallery")
        self.assertEqual(metadata.description, "archive")
        self.assertIs(metadata.private, True)
        self.assertEqual(metadata.tags, ("alpha", "zeta"))
        self.assertEqual(metadata.board_ids, (2, 9))

    def test_record_failure_cas_loss_becomes_retryable_conflict(self):
        service, store, pin_import, _transport = self._service(
            [self._claim()]
        )
        store.failure_result = False
        pin_import.prepare_results = [
            SafeFetchError("invalid_image_content", "detail", False)
        ]

        result = service.process(object(), self._data(), started_at=0)

        self.assertEqual(result["results"][0]["status"], "conflict")
        self.assertEqual(
            result["results"][0]["error"]["code"],
            "lease_lost",
        )
        self.assertTrue(result["results"][0]["error"]["retryable"])

    def test_database_busy_during_claim_skips_network(self):
        service, store, pin_import, _transport = self._service([])
        store.claim_error = OperationalError("database is locked: secret")

        result = service.process(object(), self._data(), started_at=0)

        self.assertEqual(pin_import.prepare_calls, [])
        self.assertEqual(
            result["results"][0]["error"]["code"],
            "database_busy",
        )
        self.assertTrue(result["results"][0]["error"]["retryable"])
        self.assertNotIn("secret", json.dumps(result))

    def test_lease_lost_commit_error_does_not_record_failure(self):
        service, store, pin_import, _transport = self._service(
            [self._claim()]
        )
        pin_import.prepare_results = [FakePrepared()]
        pin_import.commit_results = [
            PinImportError("lease_lost", "detail", True)
        ]

        result = service.process(object(), self._data(), started_at=0)

        self.assertEqual(result["results"][0]["status"], "conflict")
        self.assertEqual(store.failure_calls, [])

    def test_database_busy_during_commit_is_recorded_as_retryable(self):
        service, store, pin_import, _transport = self._service(
            [self._claim()]
        )
        pin_import.prepare_results = [FakePrepared()]
        pin_import.commit_results = [
            OperationalError("database table is busy: secret")
        ]

        result = service.process(object(), self._data(), started_at=0)

        self.assertEqual(
            result["results"][0]["error"]["code"],
            "database_busy",
        )
        self.assertTrue(result["results"][0]["error"]["retryable"])
        self.assertEqual(store.failure_calls[0][1].code, "database_busy")
        self.assertNotIn("secret", json.dumps(result))

    def test_database_busy_while_recording_failure_is_stable(self):
        service, store, pin_import, _transport = self._service(
            [self._claim()]
        )
        store.failure_error = OperationalError("database is locked")
        pin_import.prepare_results = [
            SafeFetchError("invalid_image_content", "detail", False)
        ]

        result = service.process(object(), self._data(), started_at=0)

        self.assertEqual(
            result["results"][0]["error"]["code"],
            "database_busy",
        )
        self.assertTrue(result["results"][0]["error"]["retryable"])

    def test_internal_error_retryability_is_canonical(self):
        service, _store, pin_import, _transport = self._service(
            [self._claim()]
        )
        pin_import.prepare_results = [FakePrepared()]
        pin_import.commit_results = [
            PinImportError("internal_error", "detail", True)
        ]

        result = service.process(object(), self._data(), started_at=0)

        self.assertEqual(
            result["results"][0]["error"]["code"],
            "internal_error",
        )
        self.assertFalse(result["results"][0]["error"]["retryable"])

    def test_unsupported_http_stack_code_remains_non_retryable(self):
        service, _store, pin_import, _transport = self._service(
            [self._claim()]
        )
        pin_import.prepare_results = [
            SafeFetchError(
                "unsupported_http_stack",
                "unsafe dependency detail",
                False,
            )
        ]

        result = service.process(object(), self._data(), started_at=0)

        self.assertEqual(
            result["results"][0]["error"],
            {
                "code": "unsupported_http_stack",
                "message": "The configured HTTP stack is not supported.",
                "retryable": False,
            },
        )
        self.assertNotIn("unsafe dependency detail", json.dumps(result))

    def test_internal_exception_text_is_absent_from_response_and_log(self):
        service, _store, pin_import, _transport = self._service(
            [self._claim()]
        )
        pin_import.prepare_results = [
            RuntimeError("https://secret.example/token-value")
        ]

        with self.assertLogs(
            "core.services.batch_import", level="ERROR"
        ) as captured:
            result = service.process(
                object(), self._data(), started_at=0
            )

        combined = json.dumps(result) + "\n" + "\n".join(captured.output)
        self.assertNotIn("secret.example", combined)
        self.assertNotIn("token-value", combined)

    def test_after_commit_fault_escapes_without_failure_record(self):
        def fault(point):
            if point == "after_commit":
                raise RuntimeError("response lost")

        service, store, pin_import, _transport = self._service(
            [self._claim()], fault_injector=fault
        )
        pin_import.prepare_results = [FakePrepared()]
        pin_import.commit_results = [FakePin(8)]

        with self.assertRaisesRegex(RuntimeError, "response lost"):
            service.process(object(), self._data(), started_at=0)

        self.assertEqual(store.failure_calls, [])

    def test_response_loss_is_replayed_without_repeating_side_effects(self):
        hook_calls = []

        def fault(point):
            hook_calls.append(point)
            raise RuntimeError("response lost")

        service, store, pin_import, _transport = self._service(
            [
                self._claim(),
                ClaimResult(kind="replayed", replay_pin_id=8),
            ],
            fault_injector=fault,
        )
        pin_import.prepare_results = [FakePrepared()]
        pin_import.commit_results = [FakePin(8)]
        data = self._data()

        with self.assertRaisesRegex(RuntimeError, "response lost"):
            service.process(object(), data, started_at=0)
        replay = service.process(object(), data, started_at=0)

        self.assertEqual(replay["results"][0]["status"], "replayed")
        self.assertEqual(replay["results"][0]["pin_id"], 8)
        self.assertEqual(len(store.claim_calls), 2)
        self.assertEqual(len(pin_import.prepare_calls), 1)
        self.assertEqual(len(pin_import.commit_calls), 1)
        self.assertEqual(hook_calls, ["after_commit"])
        self.assertEqual(store.failure_calls, [])

    def test_close_is_idempotent_and_swallows_transport_errors(self):
        service, _store, _pin_import, transport = self._service([])
        transport.close_error = RuntimeError("secret")

        with self.assertLogs(
            "core.services.batch_import", level="ERROR"
        ) as captured:
            service.close()
        service.close()

        self.assertEqual(transport.close_count, 1)
        self.assertNotIn("secret", "\n".join(captured.output))

    def test_mismatched_dependency_graph_is_rejected(self):
        fetcher = FakeFetcher()
        media_storage = object()
        idempotency = FakeIdempotency([])
        pin_import = FakePinImport(fetcher, object(), idempotency)

        with self.assertRaises(ValueError):
            BatchImportService(
                fetcher,
                media_storage,
                idempotency,
                pin_import,
            )

    def test_malformed_claim_fails_closed_without_network(self):
        for malformed in (
            None,
            ClaimResult(kind="unknown"),
            ClaimResult(kind="replayed", replay_pin_id=0),
            ClaimResult(
                kind="in_progress",
                error=StoredError("in_progress", True),
                retry_after_seconds=0,
            ),
            ClaimResult(
                kind="conflict",
                error=StoredError("in_progress", True),
            ),
            ClaimResult(
                kind="conflict",
                error=StoredError("idempotency_mismatch", True),
            ),
            ClaimResult(
                kind="failed",
                error=StoredError("idempotency_mismatch", False),
            ),
            ClaimResult(
                kind="failed",
                error=StoredError("future_safe_code", False),
            ),
        ):
            with self.subTest(claim=malformed):
                service, _store, pin_import, _transport = self._service(
                    [malformed]
                )

                result = service.process(
                    object(), self._data(), started_at=0
                )

                self.assertEqual(pin_import.prepare_calls, [])
                self.assertEqual(result["results"][0]["status"], "failed")
                self.assertEqual(
                    result["results"][0]["error"]["code"],
                    "internal_error",
                )

    @mock.patch("core.services.batch_import.connection")
    def test_locked_text_is_not_database_busy_outside_sqlite(
        self, database_connection
    ):
        database_connection.vendor = "postgresql"
        service, store, pin_import, _transport = self._service([])
        store.claim_error = OperationalError("database is locked")

        with self.assertLogs(
            "core.services.batch_import", level="ERROR"
        ):
            result = service.process(
                object(), self._data(), started_at=0
            )

        self.assertEqual(pin_import.prepare_calls, [])
        self.assertEqual(
            result["results"][0]["error"]["code"],
            "internal_error",
        )
        self.assertFalse(result["results"][0]["error"]["retryable"])

    def test_unrelated_sqlite_error_containing_busy_prefix_is_internal(self):
        service, store, pin_import, _transport = self._service([])
        store.claim_error = OperationalError("busywork rule failed")

        with self.assertLogs(
            "core.services.batch_import", level="ERROR"
        ):
            result = service.process(
                object(), self._data(), started_at=0
            )

        self.assertEqual(pin_import.prepare_calls, [])
        self.assertEqual(
            result["results"][0]["error"]["code"],
            "internal_error",
        )
        self.assertFalse(result["results"][0]["error"]["retryable"])


class BatchEndpointContractTests(SimpleTestCase):
    def test_batch_route_is_registered_by_the_pin_router(self):
        try:
            url = reverse("pin-batch")
        except NoReverseMatch:
            self.fail("pin-batch route is not registered")

        self.assertEqual(url, "/api/v2/pins/batch/")


class FakeViewBatchService(object):
    def __init__(self, result=None, process_error=None, close_error=None):
        self.result = result or {
            "batch_id": "batch",
            "results": [],
            "summary": {
                "created": 0,
                "replayed": 0,
                "failed": 0,
                "conflict": 0,
            },
        }
        self.process_error = process_error
        self.close_error = close_error
        self.process_calls = []
        self.close_count = 0

    def process(self, user, validated_data, started_at):
        self.process_calls.append((user, validated_data, started_at))
        if self.process_error is not None:
            raise self.process_error
        return self.result

    def close(self):
        self.close_count += 1
        if self.close_error is not None:
            raise self.close_error


class BatchActionTests(TestCase):
    _MISSING = object()

    def setUp(self):
        self.factory = APIRequestFactory()
        self.user = create_user("batch_owner")
        self.other_user = create_user("batch_other")
        self.view = resolve("/api/v2/pins/batch/").func

    def _payload(self, board_ids=None):
        return {
            "batch_id": str(uuid.uuid4()),
            "board_ids": board_ids or [],
            "tags": ["zeta", "alpha", "zeta"],
            "private": False,
            "referer": "https://example.com/gallery",
            "description": "archive",
            "items": [{
                "client_item_id": str(uuid.uuid4()),
                "url": "https://example.com/image.png",
            }],
        }

    def _request(
        self,
        payload=None,
        body=None,
        content_length=_MISSING,
        authenticate=True,
    ):
        if body is None:
            body = json.dumps(payload).encode("utf-8")
        if content_length is self._MISSING:
            content_length = str(len(body))
        request = self.factory.generic(
            "POST",
            "/api/v2/pins/batch/",
            body,
            content_type="application/json",
            CONTENT_LENGTH=content_length,
        )
        stream = RecordingStream(body)
        request._stream = stream
        if content_length is None:
            request.META.pop("CONTENT_LENGTH", None)
        if authenticate:
            force_authenticate(request, user=self.user)
        return request, stream

    def test_unauthenticated_request_does_not_create_service_or_read_body(
        self,
    ):
        request, stream = self._request(
            payload=self._payload(), authenticate=False
        )
        with mock.patch.object(
            PinViewSet,
            "get_batch_service",
            side_effect=AssertionError("service must not be built"),
        ) as factory:
            response = self.view(request)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(stream.total_read, 0)
        self.assertEqual(factory.call_count, 0)

    def test_invalid_content_length_is_rejected_without_read_or_service(self):
        for header in (None, "", "1.5", "-1", "+1", " 1"):
            with self.subTest(header=header):
                request, stream = self._request(
                    payload=self._payload(), content_length=header
                )
                with mock.patch.object(
                    PinViewSet,
                    "get_batch_service",
                    side_effect=AssertionError("service must not be built"),
                ) as factory:
                    response = self.view(request)

                self.assertEqual(response.status_code, 400)
                self.assertEqual(
                    response.data["code"],
                    "batch_invalid_content_length",
                )
                self.assertEqual(stream.total_read, 0)
                self.assertEqual(factory.call_count, 0)

    def test_extremely_long_decimal_content_length_is_rejected_without_read(
        self,
    ):
        request, stream = self._request(
            body=b"{}", content_length="9" * 5000
        )
        with mock.patch.object(
            PinViewSet,
            "get_batch_service",
            side_effect=AssertionError("service must not be built"),
        ) as factory:
            response = self.view(request)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "batch_body_too_large")
        self.assertEqual(stream.total_read, 0)
        self.assertEqual(factory.call_count, 0)

    @override_settings(PINRY_BATCH_MAX_BODY_BYTES=16)
    def test_declared_oversize_is_rejected_without_read_or_service(self):
        request, stream = self._request(
            body=b"{}", content_length="17"
        )
        with mock.patch.object(
            PinViewSet,
            "get_batch_service",
            side_effect=AssertionError("service must not be built"),
        ) as factory:
            response = self.view(request)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "batch_body_too_large")
        self.assertEqual(stream.total_read, 0)
        self.assertEqual(factory.call_count, 0)

    @override_settings(PINRY_BATCH_MAX_BODY_BYTES=16)
    def test_raw_stream_parser_limit_ignores_false_small_header(self):
        request, stream = self._request(
            body=b'"' + (b"x" * 20) + b'"',
            content_length="1",
        )
        with mock.patch.object(
            PinViewSet,
            "get_batch_service",
            side_effect=AssertionError("service must not be built"),
        ) as factory:
            response = self.view(request)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "batch_body_too_large")
        self.assertLessEqual(stream.total_read, 17)
        self.assertEqual(factory.call_count, 0)

    def test_wsgi_false_small_header_is_invalid_json_without_service(self):
        body = json.dumps(self._payload()).encode("utf-8")
        request = self.factory.generic(
            "POST",
            "/api/v2/pins/batch/",
            body,
            content_type="application/json",
            CONTENT_LENGTH="1",
        )
        backing_stream = RecordingStream(body)
        request._stream.stream = backing_stream
        force_authenticate(request, user=self.user)

        with mock.patch.object(
            PinViewSet,
            "get_batch_service",
            side_effect=AssertionError("service must not be built"),
        ) as factory:
            response = self.view(request)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "batch_invalid_json")
        self.assertLessEqual(backing_stream.total_read, 1)
        self.assertEqual(factory.call_count, 0)

    def test_deeply_nested_json_is_rejected_before_service(self):
        body = (b"[" * 2000) + (b"]" * 2000)
        request, stream = self._request(body=body)

        with mock.patch.object(
            PinViewSet,
            "get_batch_service",
            side_effect=AssertionError("service must not be built"),
        ) as factory:
            response = self.view(request)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "batch_invalid_json")
        self.assertLessEqual(stream.total_read, len(body))
        self.assertEqual(factory.call_count, 0)

    def test_zero_content_length_reaches_serializer_without_stream_read(self):
        request, stream = self._request(body=b"", content_length="0")
        with mock.patch.object(
            PinViewSet,
            "get_batch_service",
            side_effect=AssertionError("service must not be built"),
        ) as factory:
            response = self.view(request)

        self.assertEqual(response.status_code, 400)
        self.assertIn("batch_id", response.data)
        self.assertIn("items", response.data)
        self.assertEqual(stream.total_read, 0)
        self.assertEqual(factory.call_count, 0)

    def test_invalid_serializer_does_not_create_tags_or_service(self):
        payload = self._payload()
        payload["items"] = []
        request, _stream = self._request(payload=payload)
        with mock.patch.object(
            PinViewSet,
            "get_batch_service",
            side_effect=AssertionError("service must not be built"),
        ) as factory:
            response = self.view(request)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(Tag.objects.count(), 0)
        self.assertEqual(factory.call_count, 0)

    def test_invalid_item_uuid_rejects_whole_request_before_service(self):
        payload = self._payload()
        payload["items"][0]["client_item_id"] = "not-a-uuid"
        request, _stream = self._request(payload=payload)
        with mock.patch.object(
            PinViewSet,
            "get_batch_service",
            side_effect=AssertionError("service must not be built"),
        ) as factory:
            response = self.view(request)

        self.assertEqual(response.status_code, 400)
        self.assertIn("client_item_id", response.data["items"][0])
        self.assertEqual(Tag.objects.count(), 0)
        self.assertEqual(factory.call_count, 0)

    def test_too_many_or_duplicate_items_reject_before_service(self):
        too_many = self._payload()
        too_many["items"] = [
            {
                "client_item_id": str(uuid.uuid4()),
                "url": "https://example.com/{}.png".format(index),
            }
            for index in range(11)
        ]
        duplicate = self._payload()
        duplicate["items"].append(dict(duplicate["items"][0]))

        for payload in (too_many, duplicate):
            with self.subTest(item_count=len(payload["items"])):
                request, _stream = self._request(payload=payload)
                with mock.patch.object(
                    PinViewSet,
                    "get_batch_service",
                    side_effect=AssertionError(
                        "service must not be built"
                    ),
                ) as factory:
                    response = self.view(request)

                self.assertEqual(response.status_code, 400)
                self.assertIn("items", response.data)
                self.assertEqual(factory.call_count, 0)

    def test_non_owned_board_rejects_whole_batch_before_service(self):
        board = Board.objects.create(
            submitter=self.other_user,
            name="not-owned",
        )
        request, _stream = self._request(
            payload=self._payload([board.pk])
        )
        with mock.patch.object(
            PinViewSet,
            "get_batch_service",
            side_effect=AssertionError("service must not be built"),
        ) as factory:
            response = self.view(request)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["code"], "batch_board_access_denied")
        self.assertEqual(factory.call_count, 0)

    def test_valid_batch_normalizes_then_processes_and_closes_once(self):
        board = Board.objects.create(
            submitter=self.user,
            name="owned",
        )
        payload = self._payload([board.pk, board.pk])
        request, _stream = self._request(payload=payload)
        expected = {
            "batch_id": payload["batch_id"],
            "results": [],
            "summary": {
                "created": 0,
                "replayed": 0,
                "failed": 0,
                "conflict": 0,
            },
        }
        service = FakeViewBatchService(result=expected)
        with mock.patch.object(
            PinViewSet, "get_batch_service", return_value=service
        ):
            response = self.view(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, expected)
        self.assertEqual(service.close_count, 1)
        validated = service.process_calls[0][1]
        self.assertEqual(validated["tags"], ["alpha", "zeta"])
        self.assertEqual(validated["board_ids"], [board.pk])

    def test_process_exception_still_closes_service_once(self):
        service = FakeViewBatchService(
            process_error=RuntimeError("response lost")
        )
        request, _stream = self._request(payload=self._payload())
        with mock.patch.object(
            PinViewSet, "get_batch_service", return_value=service
        ):
            with self.assertRaisesRegex(RuntimeError, "response lost"):
                self.view(request)

        self.assertEqual(service.close_count, 1)

    def test_close_exception_does_not_replace_success_response(self):
        service = FakeViewBatchService(
            close_error=RuntimeError("close failed")
        )
        request, _stream = self._request(payload=self._payload())
        with mock.patch.object(
            PinViewSet, "get_batch_service", return_value=service
        ):
            with self.assertLogs("core.views", level="ERROR") as captured:
                response = self.view(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(service.close_count, 1)
        self.assertNotIn("close failed", "\n".join(captured.output))


class FactoryTransport(FakeTransport):
    latest = None

    def __init__(self, clock):
        super(FactoryTransport, self).__init__()
        self.clock = clock
        FactoryTransport.latest = self


class FactoryResolver(object):
    def __init__(self, clock):
        self.clock = clock


class FactoryFetcher(object):
    def __init__(self, resolver, transport, clock):
        self.resolver = resolver
        self.transport = transport
        self.clock = clock


class FactoryMediaStorage(object):
    def __init__(self, clock):
        self.clock = clock


class FactoryIdempotency(object):
    pass


class FactoryPinImport(object):
    def __init__(
        self, fetcher, media_storage, idempotency, clock
    ):
        self.fetcher = fetcher
        self.media_storage = media_storage
        self.idempotency = idempotency
        self.clock = clock


class FactoryBatchService(object):
    def __init__(
        self,
        fetcher,
        media_storage,
        idempotency,
        pin_import,
        clock,
    ):
        self.fetcher = fetcher
        self.media_storage = media_storage
        self.idempotency = idempotency
        self.pin_import = pin_import
        self.clock = clock


class FactoryView(PinViewSet):
    transport_class = FactoryTransport
    resolver_class = FactoryResolver
    fetcher_class = FactoryFetcher
    media_storage_class = FactoryMediaStorage
    idempotency_class = FactoryIdempotency
    pin_import_service_class = FactoryPinImport
    batch_service_class = FactoryBatchService
    batch_clock = staticmethod(lambda: 123)


class FailingFactoryView(FactoryView):
    class fetcher_class(object):
        def __init__(self, resolver, transport, clock):
            del resolver, transport, clock
            raise RuntimeError("factory failed")


class BatchServiceFactoryTests(SimpleTestCase):
    def test_factory_builds_one_shared_request_scoped_graph(self):
        service = FactoryView().get_batch_service()

        self.assertIs(service.pin_import.fetcher, service.fetcher)
        self.assertIs(
            service.pin_import.media_storage,
            service.media_storage,
        )
        self.assertIs(
            service.pin_import.idempotency,
            service.idempotency,
        )
        self.assertIs(service.fetcher.transport, FactoryTransport.latest)
        self.assertIs(service.clock, service.pin_import.clock)
        self.assertIs(service.clock, service.fetcher.clock)

    def test_factory_failure_closes_already_created_transport(self):
        with self.assertRaisesRegex(RuntimeError, "factory failed"):
            FailingFactoryView().get_batch_service()

        self.assertEqual(FactoryTransport.latest.close_count, 1)
