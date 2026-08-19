import copy
import datetime
import os
import queue
import threading
import uuid
from unittest import mock

from django.db import (
    OperationalError,
    close_old_connections,
    connection,
    connections,
    transaction,
)
from django.test import SimpleTestCase, TestCase, TransactionTestCase

from core.models import BatchImportItem, Image, Pin
from core.services.idempotency import (
    IdempotencyStore,
    StoredError,
    fingerprint_request,
)
from users.models import User

import manage


class MutableWallClock:
    def __init__(self, current):
        self.current = current

    def __call__(self):
        return self.current


class ManageSettingsArgumentTests(SimpleTestCase):
    def test_explicit_settings_argument_configures_environment_early(self):
        argument_forms = (
            ["--settings=pinry.settings.test_sqlite_file"],
            ["--settings", "pinry.settings.test_sqlite_file"],
        )

        for arguments in argument_forms:
            with self.subTest(arguments=arguments):
                with mock.patch.dict(os.environ, {}, clear=True):
                    manage._configure_settings_module(
                        ["manage.py", "test"] + arguments
                    )
                    self.assertEqual(
                        os.environ["DJANGO_SETTINGS_MODULE"],
                        "pinry.settings.test_sqlite_file",
                    )

    def test_default_settings_and_existing_environment_are_preserved(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            manage._configure_settings_module(["manage.py", "check"])
            self.assertEqual(
                os.environ["DJANGO_SETTINGS_MODULE"],
                "pinry.settings.development",
            )

        with mock.patch.dict(
            os.environ,
            {"DJANGO_SETTINGS_MODULE": "pinry.settings.production"},
            clear=True,
        ):
            manage._configure_settings_module(["manage.py", "check"])
            self.assertEqual(
                os.environ["DJANGO_SETTINGS_MODULE"],
                "pinry.settings.production",
            )

    def test_explicit_settings_override_existing_environment(self):
        with mock.patch.dict(
            os.environ,
            {"DJANGO_SETTINGS_MODULE": "pinry.settings.production"},
            clear=True,
        ):
            manage._configure_settings_module(
                [
                    "manage.py",
                    "check",
                    "--settings=pinry.settings.test_sqlite_file",
                ]
            )
            self.assertEqual(
                os.environ["DJANGO_SETTINGS_MODULE"],
                "pinry.settings.test_sqlite_file",
            )

    def test_empty_settings_argument_keeps_normal_default_resolution(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            manage._configure_settings_module(
                ["manage.py", "test", "--settings="]
            )
            self.assertEqual(
                os.environ["DJANGO_SETTINGS_MODULE"],
                "pinry.settings.development",
            )

        with mock.patch.dict(
            os.environ,
            {"DJANGO_SETTINGS_MODULE": "pinry.settings.production"},
            clear=True,
        ):
            manage._configure_settings_module(
                ["manage.py", "test", "--settings="]
            )
            self.assertEqual(
                os.environ["DJANGO_SETTINGS_MODULE"],
                "pinry.settings.production",
            )


class FingerprintRequestTests(SimpleTestCase):
    def test_canonicalizes_defaults_collections_and_ignores_noncontract_keys(self):
        payload = {
            "url": "https://example.com/이미지.png",
            "tags": ["풍경", "고양이", "풍경"],
            "board_ids": [7, 2, 7],
            "batch_id": "ignored-batch",
            "client_item_id": "ignored-item",
            "extra": {"token": "ignored-secret"},
        }
        original = copy.deepcopy(payload)

        digest = fingerprint_request(payload)

        self.assertEqual(
            digest,
            "144c0236f63a33b51569542e5835aeb4"
            "f0b29bc947dd883f002d5e4546faf5dc",
        )
        self.assertEqual(payload, original)

    def test_omitted_and_explicit_optional_defaults_match(self):
        minimal = {"url": "https://example.com/image.png"}
        explicit = {
            "url": "https://example.com/image.png",
            "referer": "",
            "description": "",
            "private": False,
            "tags": [],
            "board_ids": [],
        }

        self.assertEqual(
            fingerprint_request(minimal),
            fingerprint_request(explicit),
        )

    def test_key_order_collection_order_and_duplicates_do_not_change_digest(self):
        first = {
            "url": "https://example.com/image.png",
            "description": "caption",
            "tags": ["second", "first", "first"],
            "board_ids": [9, 3, 9],
        }
        second = {
            "board_ids": [3, 9],
            "tags": ["first", "second"],
            "description": "caption",
            "url": "https://example.com/image.png",
        }

        self.assertEqual(
            fingerprint_request(first),
            fingerprint_request(second),
        )

    def test_tags_sort_as_strings_and_board_ids_sort_as_integers(self):
        payload = {
            "url": "https://example.com/numeric.png",
            "tags": ["2", "10"],
            "board_ids": [10, 2],
        }

        self.assertEqual(
            fingerprint_request(payload),
            "073aafb9faacc0b56317e872de97c6ad"
            "562bb7b7e27c882a0f0685edc7ce3658",
        )

    def test_each_request_field_change_changes_digest(self):
        base = {
            "url": "https://example.com/image.png",
            "referer": "https://example.com/page",
            "description": "caption",
            "private": False,
            "tags": ["tag"],
            "board_ids": [3],
        }
        variants = (
            {"url": "https://example.com/other.png"},
            {"referer": "https://example.com/other-page"},
            {"description": "other caption"},
            {"private": True},
            {"tags": ["other-tag"]},
            {"board_ids": [4]},
        )
        expected = fingerprint_request(base)

        for change in variants:
            payload = dict(base)
            payload.update(change)
            with self.subTest(change=change):
                self.assertNotEqual(fingerprint_request(payload), expected)

    def test_batch_item_and_extra_values_never_change_digest(self):
        first = {
            "url": "https://example.com/image.png",
            "batch_id": "first-batch",
            "client_item_id": "first-item",
            "extra": "first",
        }
        second = {
            "url": "https://example.com/image.png",
            "batch_id": "second-batch",
            "client_item_id": "second-item",
            "extra": "second",
        }

        self.assertEqual(
            fingerprint_request(first),
            fingerprint_request(second),
        )


class IdempotencyStoreTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="idempotency-owner",
            email="owner@example.com",
        )
        self.other_user = User.objects.create_user(
            username="idempotency-other",
            email="other@example.com",
        )
        self.now = datetime.datetime(
            2026, 8, 20, 1, 2, 3, tzinfo=datetime.timezone.utc
        )
        self.clock = MutableWallClock(self.now)
        self.store = IdempotencyStore(
            wall_clock=self.clock,
            lease_seconds=300,
        )
        self.batch_id = uuid.UUID("10000000-0000-0000-0000-000000000001")
        self.item_id = uuid.UUID("20000000-0000-0000-0000-000000000001")
        self.payload = {"url": "https://example.com/image.png"}
        self.fingerprint = fingerprint_request(self.payload)

    def _claim(
        self,
        item_id=None,
        batch_id=None,
        fingerprint=None,
        user=None,
        now=None,
    ):
        return self.store.claim(
            user or self.user,
            batch_id or self.batch_id,
            item_id or self.item_id,
            fingerprint or self.fingerprint,
            now or self.now,
        )

    def _create_pin(self, user=None):
        image = Image.objects.create(
            image="test/original.png",
            original_filename="original.png",
        )
        return Pin.objects.create(
            submitter=user or self.user,
            image=image,
        )

    def test_new_claim_starts_pending_with_generation_one(self):
        result = self._claim()

        self.assertEqual(result.kind, "claimed")
        self.assertEqual(result.lease_generation, 1)
        self.assertIsNotNone(result.lease_uuid)
        self.assertIsNone(result.replay_pin_id)
        self.assertIsNone(result.error)
        self.assertIsNone(result.retry_after_seconds)
        row = BatchImportItem.objects.get(pk=result.row_id)
        self.assertEqual(row.submitter, self.user)
        self.assertEqual(row.batch_id, self.batch_id)
        self.assertEqual(row.client_item_id, self.item_id)
        self.assertEqual(row.request_fingerprint, self.fingerprint)
        self.assertEqual(row.state, BatchImportItem.PENDING)
        self.assertEqual(row.lease_uuid, result.lease_uuid)
        self.assertEqual(row.lease_generation, 1)
        self.assertEqual(
            row.lease_expires_at,
            self.now + datetime.timedelta(seconds=300),
        )
        self.assertIsNone(row.pin_id)
        self.assertIsNone(row.error_code)
        self.assertIsNone(row.retryable)

    def test_same_user_and_item_are_unique_but_other_user_may_reuse_item(self):
        first = self._claim()
        duplicate = self._claim(
            batch_id=uuid.UUID("10000000-0000-0000-0000-000000000002")
        )
        other = self._claim(user=self.other_user)

        self.assertEqual(first.kind, "claimed")
        self.assertEqual(duplicate.kind, "in_progress")
        self.assertEqual(other.kind, "claimed")
        self.assertEqual(BatchImportItem.objects.count(), 2)

    def test_database_has_submitter_item_unique_constraint(self):
        table = BatchImportItem._meta.db_table
        with connection.cursor() as cursor:
            constraints = connection.introspection.get_constraints(
                cursor, table
            )

        unique_columns = {
            tuple(details["columns"])
            for details in constraints.values()
            if details["unique"]
        }
        self.assertIn(
            ("submitter_id", "client_item_id"),
            unique_columns,
        )

    def test_same_url_with_different_item_ids_creates_two_claim_rows(self):
        first = self._claim()
        second = self._claim(
            item_id=uuid.UUID("20000000-0000-0000-0000-000000000002")
        )

        self.assertEqual(first.kind, "claimed")
        self.assertEqual(second.kind, "claimed")
        self.assertEqual(BatchImportItem.objects.count(), 2)

    def test_valid_pending_lease_returns_positive_ceiling_retry_after(self):
        self._claim()
        later = self.now + datetime.timedelta(seconds=1, microseconds=200000)

        result = self._claim(now=later)

        self.assertEqual(result.kind, "in_progress")
        self.assertEqual(result.retry_after_seconds, 299)
        self.assertIsNone(result.lease_uuid)
        self.assertIsNone(result.lease_generation)

    def test_expired_lease_is_reclaimed_with_new_token_and_generation(self):
        first = self._claim()
        reclaim_now = self.now + datetime.timedelta(seconds=301)
        BatchImportItem.objects.filter(pk=first.row_id).update(
            lease_expires_at=reclaim_now,
        )
        self.clock.current = reclaim_now

        second = self._claim(now=reclaim_now)

        self.assertEqual(second.kind, "claimed")
        self.assertNotEqual(second.lease_uuid, first.lease_uuid)
        self.assertEqual(second.lease_generation, 2)
        self.assertFalse(self.store.fence(first))
        self.assertTrue(self.store.fence(second))

    def test_retryable_failure_reclaims_and_resets_transition_fields(self):
        first = self._claim()
        self.clock.current = self.now + datetime.timedelta(seconds=1)
        self.assertTrue(
            self.store.record_failure(
                first,
                StoredError("image_download_failed", True),
            )
        )
        retry_batch = uuid.UUID("10000000-0000-0000-0000-000000000099")

        second = self._claim(
            batch_id=retry_batch,
            now=self.clock.current,
        )

        self.assertEqual(second.kind, "claimed")
        self.assertEqual(second.lease_generation, 2)
        row = BatchImportItem.objects.get(pk=first.row_id)
        self.assertEqual(row.state, BatchImportItem.PENDING)
        self.assertEqual(row.batch_id, self.batch_id)
        self.assertIsNone(row.pin_id)
        self.assertIsNone(row.error_code)
        self.assertIsNone(row.retryable)

    def test_nonretryable_failure_returns_only_stored_safe_error(self):
        claim = self._claim()
        error = StoredError("invalid_image_content", False)
        self.clock.current = self.now + datetime.timedelta(seconds=1)
        self.assertTrue(self.store.record_failure(claim, error))

        result = self._claim(now=self.clock.current)

        self.assertEqual(result.kind, "failed")
        self.assertEqual(
            result.error,
            StoredError("invalid_image_content", False),
        )
        self.assertIsNone(result.retry_after_seconds)
        with self.assertRaises(AttributeError):
            result.error.code = "changed"

    def test_unsafe_error_code_is_rejected_without_storing_raw_value(self):
        claim = self._claim()

        unsafe_codes = (
            "token=https://example.com/private?authorization=secret",
            "safe_code\n",
        )
        for code in unsafe_codes:
            with self.subTest(code=code):
                with self.assertRaises(ValueError):
                    StoredError(code, False)

        row = BatchImportItem.objects.get(pk=claim.row_id)
        self.assertEqual(row.state, BatchImportItem.PENDING)
        self.assertIsNone(row.error_code)

    def test_record_success_replays_pin_across_batch_ids(self):
        claim = self._claim()
        pin = self._create_pin()
        self.clock.current = self.now + datetime.timedelta(seconds=1)
        self.assertTrue(self.store.record_success(claim, pin))

        result = self._claim(
            batch_id=uuid.UUID("10000000-0000-0000-0000-000000000002"),
            now=self.clock.current,
        )

        self.assertEqual(result.kind, "replayed")
        self.assertEqual(result.replay_pin_id, pin.pk)
        self.assertIsNone(result.error)
        row = BatchImportItem.objects.get(pk=claim.row_id)
        self.assertEqual(row.batch_id, self.batch_id)
        self.assertEqual(row.state, BatchImportItem.SUCCEEDED)
        self.assertEqual(row.pin_id, pin.pk)
        self.assertIsNone(row.error_code)
        self.assertIsNone(row.retryable)
        self.assertIsNone(row.lease_uuid)
        self.assertIsNone(row.lease_expires_at)

    def test_record_success_rejects_pin_owned_by_another_submitter(self):
        claim = self._claim()
        other_pin = self._create_pin(user=self.other_user)

        self.assertFalse(self.store.record_success(claim, other_pin))

        row = BatchImportItem.objects.get(pk=claim.row_id)
        self.assertEqual(row.state, BatchImportItem.PENDING)
        self.assertEqual(row.submitter_id, self.user.pk)
        self.assertIsNone(row.pin_id)

    def test_record_success_rejects_unsaved_pin(self):
        claim = self._claim()
        image = Image.objects.create(
            image="test/unsaved-pin.png",
            original_filename="unsaved-pin.png",
        )
        unsaved_pin = Pin(submitter=self.user, image=image)

        self.assertFalse(self.store.record_success(claim, unsaved_pin))

        row = BatchImportItem.objects.get(pk=claim.row_id)
        self.assertEqual(row.state, BatchImportItem.PENDING)
        self.assertIsNone(row.pin_id)

    def test_mismatched_fingerprint_wins_before_all_state_handling(self):
        claim = self._claim()
        BatchImportItem.objects.filter(pk=claim.row_id).update(
            state="corrupt",
            lease_uuid=None,
            lease_expires_at=None,
        )

        result = self._claim(fingerprint="f" * 64)

        self.assertEqual(result.kind, "conflict")
        self.assertEqual(
            result.error,
            StoredError("idempotency_mismatch", False),
        )

    def test_deleted_success_pin_returns_terminal_tombstone_without_rewrite(self):
        claim = self._claim()
        pin = self._create_pin()
        self.assertTrue(self.store.record_success(claim, pin))
        pin.delete()
        before = BatchImportItem.objects.get(pk=claim.row_id)

        same = self._claim()
        mismatch = self._claim(fingerprint="f" * 64)

        self.assertEqual(same.kind, "failed")
        self.assertEqual(
            same.error,
            StoredError("pin_permanently_deleted", False),
        )
        self.assertEqual(mismatch.kind, "conflict")
        self.assertEqual(
            mismatch.error,
            StoredError("idempotency_mismatch", False),
        )
        after = BatchImportItem.objects.get(pk=claim.row_id)
        self.assertEqual(after.state, BatchImportItem.SUCCEEDED)
        self.assertIsNone(after.pin_id)
        self.assertEqual(after.updated_at, before.updated_at)

    def test_duplicate_insert_inside_outer_atomic_keeps_connection_usable(self):
        self._claim()

        with transaction.atomic():
            result = self._claim()
            count = BatchImportItem.objects.count()

        self.assertEqual(result.kind, "in_progress")
        self.assertEqual(count, 1)

    def test_fence_uses_fresh_clock_without_extending_lease(self):
        claim = self._claim()
        original_expiry = BatchImportItem.objects.get(
            pk=claim.row_id
        ).lease_expires_at
        self.clock.current = self.now + datetime.timedelta(seconds=10)

        self.assertTrue(self.store.fence(claim))

        row = BatchImportItem.objects.get(pk=claim.row_id)
        self.assertEqual(row.lease_expires_at, original_expiry)
        self.assertEqual(row.updated_at, self.clock.current)

    def test_expired_unreclaimed_worker_cannot_fence_fail_or_succeed(self):
        claim = self._claim()
        pin = self._create_pin()
        expiry = BatchImportItem.objects.get(
            pk=claim.row_id
        ).lease_expires_at
        self.clock.current = expiry

        self.assertFalse(self.store.fence(claim))
        self.assertFalse(
            self.store.record_failure(
                claim,
                StoredError("image_fetch_timeout", True),
            )
        )
        self.assertFalse(self.store.record_success(claim, pin))
        row = BatchImportItem.objects.get(pk=claim.row_id)
        self.assertEqual(row.state, BatchImportItem.PENDING)
        self.assertEqual(row.lease_uuid, claim.lease_uuid)
        self.assertIsNone(row.error_code)
        self.assertIsNone(row.pin_id)

    def test_stale_generation_cannot_write_after_reclaim(self):
        first = self._claim()
        reclaim_now = self.now + datetime.timedelta(seconds=301)
        BatchImportItem.objects.filter(pk=first.row_id).update(
            lease_expires_at=reclaim_now,
        )
        second = self._claim(now=reclaim_now)
        pin = self._create_pin()
        self.clock.current = reclaim_now + datetime.timedelta(seconds=1)

        self.assertFalse(self.store.fence(first))
        self.assertFalse(
            self.store.record_failure(
                first,
                StoredError("image_download_failed", True),
            )
        )
        self.assertFalse(self.store.record_success(first, pin))
        self.assertTrue(self.store.fence(second))

    def test_claim_and_wall_clock_reject_naive_datetimes(self):
        naive = datetime.datetime(2026, 8, 20, 1, 2, 3)

        with self.assertRaises(ValueError):
            self._claim(now=naive)

        claim = self._claim()
        self.clock.current = naive
        with self.assertRaises(ValueError):
            self.store.fence(claim)
        with self.assertRaises(ValueError):
            self.store.record_failure(
                claim,
                StoredError("image_download_failed", True),
            )
        with self.assertRaises(ValueError):
            self.store.record_success(claim, self._create_pin())

    def test_corrupt_state_combinations_fail_closed(self):
        corrupt_updates = (
            {"state": BatchImportItem.PENDING, "lease_uuid": None},
            {"state": BatchImportItem.PENDING, "lease_generation": 0},
            {
                "state": BatchImportItem.FAILED,
                "lease_uuid": None,
                "lease_expires_at": None,
                "error_code": None,
                "retryable": None,
            },
            {"state": "unknown", "lease_uuid": None, "lease_expires_at": None},
            {
                "state": BatchImportItem.SUCCEEDED,
                "pin": None,
                "error_code": "unexpected",
                "retryable": False,
                "lease_uuid": None,
                "lease_expires_at": None,
            },
        )

        for index, updates in enumerate(corrupt_updates, start=10):
            item_id = uuid.UUID(
                "20000000-0000-0000-0000-{:012d}".format(index)
            )
            claim = self._claim(item_id=item_id)
            BatchImportItem.objects.filter(pk=claim.row_id).update(**updates)

            result = self._claim(item_id=item_id)

            with self.subTest(updates=updates):
                self.assertEqual(result.kind, "failed")
                self.assertEqual(
                    result.error,
                    StoredError("internal_error", False),
                )
                self.assertIsNone(result.lease_uuid)

    def test_corrupt_pending_row_cannot_be_fenced_or_transitioned(self):
        claim = self._claim()
        pin = self._create_pin()
        BatchImportItem.objects.filter(pk=claim.row_id).update(
            error_code="unexpected",
            retryable=False,
        )

        self.assertFalse(self.store.fence(claim))
        self.assertFalse(
            self.store.record_failure(
                claim,
                StoredError("image_download_failed", True),
            )
        )
        self.assertFalse(self.store.record_success(claim, pin))
        row = BatchImportItem.objects.get(pk=claim.row_id)
        self.assertEqual(row.state, BatchImportItem.PENDING)
        self.assertEqual(row.error_code, "unexpected")
        self.assertFalse(row.retryable)
        self.assertIsNone(row.pin_id)

    def test_sqlite_operational_error_propagates_unchanged(self):
        original_create = BatchImportItem.objects.create
        expected = OperationalError("database is locked: private-token")

        def raise_operational_error(*args, **kwargs):
            raise expected

        BatchImportItem.objects.create = raise_operational_error
        try:
            with self.assertRaises(OperationalError) as caught:
                self._claim()
        finally:
            BatchImportItem.objects.create = original_create

        self.assertIs(caught.exception, expected)
        self.assertEqual(BatchImportItem.objects.count(), 0)


class IdempotencyConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        if connection.creation.is_in_memory_db(
            connection.settings_dict["NAME"]
        ):
            self.skipTest("This concurrency contract requires file SQLite.")
        self.user = User.objects.create_user(
            username="concurrent-owner",
            email="concurrent@example.com",
        )
        self.now = datetime.datetime(
            2026, 8, 20, 2, 3, 4, tzinfo=datetime.timezone.utc
        )
        self.batch_id = uuid.UUID("30000000-0000-0000-0000-000000000001")
        self.item_id = uuid.UUID("40000000-0000-0000-0000-000000000001")
        self.fingerprint = fingerprint_request(
            {"url": "https://example.com/concurrent.png"}
        )

    def _run_concurrent_claims(self):
        barrier = threading.Barrier(2)
        events = queue.Queue()

        def worker():
            close_old_connections()
            try:
                user = User.objects.get(pk=self.user.pk)
                store = IdempotencyStore(
                    wall_clock=lambda: self.now,
                    lease_seconds=300,
                )
                barrier.wait(timeout=5)
                result = store.claim(
                    user,
                    self.batch_id,
                    self.item_id,
                    self.fingerprint,
                    self.now,
                )
                events.put(("result", result))
            except Exception as error:
                events.put(("error", error))
            finally:
                connections["default"].close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertFalse(
            any(thread.is_alive() for thread in threads),
            "Concurrent claim threads did not finish within five seconds.",
        )
        collected = [events.get(timeout=1) for _ in range(2)]
        errors = [event for kind, event in collected if kind == "error"]
        self.assertEqual(
            errors,
            [],
            "Thread exceptions were not allowed: {!r}".format(errors),
        )
        return [event for kind, event in collected if kind == "result"]

    def test_two_threads_create_one_row_and_one_active_lease(self):
        results = self._run_concurrent_claims()

        self.assertEqual(
            sorted(result.kind for result in results),
            ["claimed", "in_progress"],
        )
        self.assertEqual(BatchImportItem.objects.count(), 1)
        self.assertEqual(
            BatchImportItem.objects.filter(
                state=BatchImportItem.PENDING,
                lease_uuid__isnull=False,
                lease_expires_at__gt=self.now,
            ).count(),
            1,
        )

    def test_two_threads_reclaim_expired_row_only_once(self):
        store = IdempotencyStore(
            wall_clock=lambda: self.now,
            lease_seconds=300,
        )
        first = store.claim(
            self.user,
            self.batch_id,
            self.item_id,
            self.fingerprint,
            self.now,
        )
        BatchImportItem.objects.filter(pk=first.row_id).update(
            lease_expires_at=self.now - datetime.timedelta(seconds=1)
        )

        results = self._run_concurrent_claims()

        self.assertEqual(
            sorted(result.kind for result in results),
            ["claimed", "in_progress"],
        )
        row = BatchImportItem.objects.get(pk=first.row_id)
        self.assertEqual(row.lease_generation, 2)
        claimed = [result for result in results if result.kind == "claimed"]
        self.assertEqual(len(claimed), 1)
        self.assertEqual(row.lease_uuid, claimed[0].lease_uuid)
