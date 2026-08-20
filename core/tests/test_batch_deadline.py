import uuid

from django.conf import settings
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from core.services.batch_import import BatchImportService
from core.services.idempotency import ClaimResult
from core.tests.test_batch_import import (
    FakeFetcher,
    FakeIdempotency,
    FakePin,
    FakePinImport,
    FakePrepared,
)


class CountingClock(object):
    def __init__(self, values):
        self.values = list(values)
        self.last = self.values[-1]
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.values:
            self.last = self.values.pop(0)
        return self.last


@override_settings(
    PINRY_BATCH_DEADLINE_SECONDS=45,
    PINRY_BATCH_RESULT_RESERVE_SECONDS=3,
)
class BatchDeadlineAdmissionTests(SimpleTestCase):
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
            "board_ids": (),
            "tags": (),
            "private": False,
            "referer": None,
            "description": "",
            "items": [
                {
                    "client_item_id": uuid.uuid4(),
                    "url": "https://example.com/{}.png".format(index),
                }
                for index in range(item_count)
            ],
        }

    def _service(self, claims, clock):
        fetcher = FakeFetcher()
        media_storage = object()
        idempotency = FakeIdempotency(claims)
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
            clock=clock,
            wall_clock=timezone.now,
        )
        return service, idempotency, pin_import

    def _assert_deadline_failure(self, result):
        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            result["error"],
            {
                "code": "batch_deadline_exceeded",
                "message": "The batch deadline does not allow another item.",
                "retryable": True,
            },
        )

    def test_exact_fifteen_seconds_admits_item_and_keeps_reserve(self):
        clock = CountingClock([30.0, 30.0])
        service, store, pin_import = self._service([self._claim()], clock)
        pin_import.prepare_results = [FakePrepared()]
        pin_import.commit_results = [FakePin(7)]

        result = service.process(object(), self._data(), started_at=0.0)

        self.assertEqual(result["results"][0]["status"], "created")
        self.assertEqual(len(store.claim_calls), 1)
        self.assertEqual(pin_import.prepare_calls[0][2], 42.0)
        self.assertEqual(pin_import.commit_calls[0][4], 42.0)

    def test_less_than_fifteen_seconds_closes_all_items_without_work(self):
        clock = CountingClock([30.000001, 0.0])
        service, store, pin_import = self._service([], clock)

        result = service.process(
            object(), self._data(item_count=3), started_at=0.0
        )

        self.assertEqual(clock.calls, 1)
        self.assertEqual(store.claim_calls, [])
        self.assertEqual(store.failure_calls, [])
        self.assertEqual(pin_import.prepare_calls, [])
        self.assertEqual(pin_import.commit_calls, [])
        for item in result["results"]:
            self._assert_deadline_failure(item)
        self.assertEqual(
            result["summary"],
            {"created": 0, "replayed": 0, "failed": 3, "conflict": 0},
        )

    def test_deadline_closure_stays_sticky_if_clock_moves_backwards(self):
        clock = CountingClock([30.000001, 0.0, 0.0])
        service, store, pin_import = self._service([], clock)

        result = service.process(
            object(), self._data(item_count=3), started_at=0.0
        )

        self.assertEqual(clock.calls, 1)
        self.assertEqual(store.claim_calls, [])
        self.assertEqual(pin_import.prepare_calls, [])
        for item in result["results"]:
            self._assert_deadline_failure(item)

    def test_next_item_is_rejected_after_first_consumes_reserve(self):
        clock = CountingClock([0.0, 0.0, 30.000001])
        service, store, pin_import = self._service([self._claim()], clock)
        pin_import.prepare_results = [FakePrepared()]
        pin_import.commit_results = [FakePin(7)]

        result = service.process(
            object(), self._data(item_count=2), started_at=0.0
        )

        self.assertEqual(
            [item["status"] for item in result["results"]],
            ["created", "failed"],
        )
        self._assert_deadline_failure(result["results"][1])
        self.assertEqual(len(store.claim_calls), 1)
        self.assertEqual(len(pin_import.prepare_calls), 1)
        self.assertEqual(len(pin_import.commit_calls), 1)

    def test_exact_boundary_after_first_item_still_admits_next(self):
        clock = CountingClock([0.0, 0.0, 30.0, 30.0])
        service, store, pin_import = self._service(
            [self._claim(1), self._claim(2)], clock
        )
        pin_import.prepare_results = [FakePrepared(), FakePrepared()]
        pin_import.commit_results = [FakePin(7), FakePin(8)]

        result = service.process(
            object(), self._data(item_count=2), started_at=0.0
        )

        self.assertEqual(
            [item["status"] for item in result["results"]],
            ["created", "created"],
        )
        self.assertEqual(len(store.claim_calls), 2)
        self.assertEqual(pin_import.prepare_calls[1][2], 42.0)
        self.assertEqual(pin_import.commit_calls[1][4], 42.0)

    def test_post_prepare_timeout_consumes_reserve_for_later_items(self):
        clock = CountingClock([0.0, 30.000001, 30.000001])
        service, store, pin_import = self._service([self._claim()], clock)
        prepared = FakePrepared()
        pin_import.prepare_results = [prepared]

        result = service.process(
            object(), self._data(item_count=2), started_at=0.0
        )

        self.assertEqual(result["results"][0]["status"], "failed")
        self.assertEqual(
            result["results"][0]["error"]["code"],
            "image_processing_timeout",
        )
        self._assert_deadline_failure(result["results"][1])
        self.assertEqual(prepared.cleanup_count, 1)
        self.assertEqual(len(store.claim_calls), 1)
        self.assertEqual(len(pin_import.prepare_calls), 1)
        self.assertEqual(pin_import.commit_calls, [])

    def test_invalid_timing_settings_fail_closed_without_clock_or_work(self):
        invalid_values = (
            True,
            False,
            None,
            "3",
            0,
            -1,
            float("nan"),
            float("inf"),
            float("-inf"),
            10 ** 10000,
        )
        setting_names = (
            "PINRY_BATCH_DEADLINE_SECONDS",
            "PINRY_BATCH_RESULT_RESERVE_SECONDS",
        )
        for setting_name in setting_names:
            for case_index, invalid_value in enumerate(invalid_values):
                configured = {
                    "PINRY_BATCH_DEADLINE_SECONDS": 45,
                    "PINRY_BATCH_RESULT_RESERVE_SECONDS": 3,
                    setting_name: invalid_value,
                }
                with self.subTest(
                    setting=setting_name,
                    case=case_index,
                ), override_settings(**configured):
                    clock = CountingClock([0.0])
                    service, store, pin_import = self._service([], clock)

                    result = service.process(
                        object(), self._data(item_count=2), started_at=0.0
                    )

                    self.assertEqual(clock.calls, 0)
                    self.assertEqual(store.claim_calls, [])
                    self.assertEqual(pin_import.prepare_calls, [])
                    self.assertEqual(pin_import.commit_calls, [])
                    for item in result["results"]:
                        self._assert_deadline_failure(item)

    @override_settings(
        PINRY_BATCH_DEADLINE_SECONDS=45.0,
        PINRY_BATCH_RESULT_RESERVE_SECONDS=3.0,
    )
    def test_finite_positive_float_settings_allow_exact_boundary(self):
        clock = CountingClock([30.0, 30.0])
        service, _store, pin_import = self._service([self._claim()], clock)
        pin_import.prepare_results = [FakePrepared()]
        pin_import.commit_results = [FakePin(7)]

        result = service.process(object(), self._data(), started_at=0.0)

        self.assertEqual(result["results"][0]["status"], "created")
        self.assertEqual(pin_import.commit_calls[0][4], 42.0)


class DefaultBatchSettingsTests(SimpleTestCase):
    def test_default_batch_limits_include_result_reserve(self):
        self.assertEqual(settings.PINRY_BATCH_DEADLINE_SECONDS, 45)
        self.assertEqual(settings.PINRY_BATCH_RESULT_RESERVE_SECONDS, 3)
        self.assertEqual(settings.PINRY_BATCH_MAX_BODY_BYTES, 1024 * 1024)
