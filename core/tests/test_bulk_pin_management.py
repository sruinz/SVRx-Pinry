import hashlib
from pathlib import Path

from django.conf import settings
from django.db import connection, OperationalError
from django.test import override_settings, SimpleTestCase
from django.test.utils import CaptureQueriesContext
import mock
from rest_framework import status
from rest_framework.exceptions import UnsupportedMediaType
from rest_framework.test import APITestCase, APITransactionTestCase

from core.bulk_serializers import BulkPinRequestSerializer
from core.models import Board, Image, MediaAsset, Pin
from core.services.bulk_pin_management import BulkOperationError
from core.tests.helpers import create_image, create_user
from core.tests.test_pin_import_atomicity import _LinuxStrongPublishMixin
from core.views import PinViewSet
from django_images.models import Thumbnail
from django_images.test_helpers import TemporaryMediaMixin


class BulkPinRequestSerializerTests(SimpleTestCase):
    VALID_REQUESTS = (
        {"operation": "delete", "pin_ids": [1, 2]},
        {"operation": "add_to_board", "pin_ids": [1], "board_id": 7},
        {
            "operation": "move_between_boards", "pin_ids": [1],
            "source_board_id": 7, "target_board_id": 8,
        },
        {
            "operation": "update", "pin_ids": [1],
            "changes": {"private": True,
                        "tags": {"mode": "add", "values": ["풍경"]}},
        },
        {
            "operation": "delete_if_exclusive_to_board", "pin_ids": [1],
            "source_board_id": 7,
        },
    )

    def test_accepts_valid_operations(self):
        for payload in self.VALID_REQUESTS:
            serializer = BulkPinRequestSerializer(data=payload)
            self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_rejects_unknown_nested_key(self):
        payload = {"operation": "update", "pin_ids": [1],
                   "changes": {"private": True, "description": "금지"}}
        serializer = BulkPinRequestSerializer(data=payload)
        self.assertFalse(serializer.is_valid())
        self.assertEqual(serializer.errors["code"][0], "bulk_invalid_request")

    def test_rejects_operation_specific_missing_and_extra_keys(self):
        valid = {
            "delete": {"operation": "delete", "pin_ids": [1]},
            "add_to_board": {
                "operation": "add_to_board", "pin_ids": [1], "board_id": 7,
            },
            "move_between_boards": {
                "operation": "move_between_boards", "pin_ids": [1],
                "source_board_id": 7, "target_board_id": 8,
            },
            "update": {
                "operation": "update", "pin_ids": [1],
                "changes": {"private": True},
            },
            "delete_if_exclusive_to_board": {
                "operation": "delete_if_exclusive_to_board", "pin_ids": [1],
                "source_board_id": 7,
            },
        }
        required = {
            "delete": ("pin_ids",),
            "add_to_board": ("pin_ids", "board_id"),
            "move_between_boards": (
                "pin_ids", "source_board_id", "target_board_id",
            ),
            "update": ("pin_ids", "changes"),
            "delete_if_exclusive_to_board": ("pin_ids", "source_board_id"),
        }
        for operation, payload in valid.items():
            missing_operation = dict(payload)
            del missing_operation["operation"]
            serializer = BulkPinRequestSerializer(data=missing_operation)
            self.assertFalse(serializer.is_valid(), missing_operation)
            for key in required[operation]:
                missing = dict(payload)
                del missing[key]
                serializer = BulkPinRequestSerializer(data=missing)
                self.assertFalse(serializer.is_valid(), missing)
            extra = dict(payload)
            extra["unexpected"] = 1
            serializer = BulkPinRequestSerializer(data=extra)
            self.assertFalse(serializer.is_valid(), extra)

    def test_rejects_all_board_id_boolean_and_string_values(self):
        payloads = (
            {"operation": "add_to_board", "pin_ids": [1], "board_id": True},
            {"operation": "add_to_board", "pin_ids": [1], "board_id": "7"},
            {"operation": "move_between_boards", "pin_ids": [1],
             "source_board_id": True, "target_board_id": 8},
            {"operation": "move_between_boards", "pin_ids": [1],
             "source_board_id": 7, "target_board_id": "8"},
            {"operation": "move_between_boards", "pin_ids": [1],
             "source_board_id": 7, "target_board_id": True},
            {"operation": "delete_if_exclusive_to_board", "pin_ids": [1],
             "source_board_id": True},
            {"operation": "delete_if_exclusive_to_board", "pin_ids": [1],
             "source_board_id": "7"},
        )
        for payload in payloads:
            serializer = BulkPinRequestSerializer(data=payload)
            self.assertFalse(serializer.is_valid(), payload)
            self.assertEqual(serializer.errors["code"][0],
                             "bulk_invalid_request")

    def test_rejects_strict_values_and_exact_keys(self):
        invalid = (
            {"operation": "delete", "pin_ids": [True]},
            {"operation": "delete", "pin_ids": ["1"]},
            {"operation": "delete", "pin_ids": [0]},
            {"operation": "delete", "pin_ids": [-1]},
            {"operation": "delete", "pin_ids": []},
            {"operation": "delete", "pin_ids": [1, 1]},
            {"operation": "delete", "pin_ids": list(range(1, 52))},
            {"operation": "add_to_board", "pin_ids": [1], "board_id": True},
            {"operation": "add_to_board", "pin_ids": [1], "board_id": "7"},
            {"operation": "delete", "pin_ids": [1], "board_id": 7},
            {"operation": "add_to_board", "pin_ids": [1]},
            {"operation": "move_between_boards", "pin_ids": [1],
             "source_board_id": 7, "target_board_id": 7},
            {"operation": "update", "pin_ids": [1], "changes": {}},
            {"operation": "update", "pin_ids": [1],
             "changes": {"private": "true"}},
            {"operation": "update", "pin_ids": [1],
             "changes": {"tags": {"mode": "add", "values": []}}},
            {"operation": "update", "pin_ids": [1],
             "changes": {"tags": {"mode": "remove", "values": []}}},
            {"operation": "update", "pin_ids": [1],
             "changes": {"tags": {"mode": "add", "values": [1]}}},
            {"operation": "update", "pin_ids": [1],
             "changes": {"tags": {"mode": "replace", "values": [""]}}},
            {"operation": "update", "pin_ids": [1],
             "changes": {"tags": {"mode": "add", "values": ["x"] * 51}}},
        )
        for payload in invalid:
            serializer = BulkPinRequestSerializer(data=payload)
            self.assertFalse(serializer.is_valid(), payload)
            self.assertEqual(serializer.errors["code"][0],
                             "bulk_invalid_request")

    def test_normalizes_tags_and_allows_empty_replace(self):
        payload = {"operation": "update", "pin_ids": [1], "changes": {
            "tags": {"mode": "add", "values": [" 풍경 ", "풍경", "바다"]},
        }}
        serializer = BulkPinRequestSerializer(data=payload)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["changes"]["tags"], {
            "mode": "add", "values": ["풍경", "바다"]})

        empty = {"operation": "update", "pin_ids": [1], "changes": {
            "tags": {"mode": "replace", "values": []},
        }}
        serializer = BulkPinRequestSerializer(data=empty)
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_rejects_trimmed_empty_and_overlong_tags(self):
        for value in ("   ", "x" * 101):
            serializer = BulkPinRequestSerializer(data={
                "operation": "update", "pin_ids": [1],
                "changes": {"tags": {"mode": "replace", "values": [value]}},
            })
            self.assertFalse(serializer.is_valid())
            self.assertEqual(serializer.errors["code"][0],
                             "bulk_invalid_request")


class BulkExceptionNormalizationTests(SimpleTestCase):
    def test_normalizes_only_closed_safe_codes(self):
        from core.services.bulk_pin_management import (
            normalize_bulk_exception,
        )

        cases = (
            (
                OperationalError("database is locked at /private/path"),
                ("database_busy", True),
            ),
            (
                OperationalError("no such table: /private/path"),
                ("internal_error", False),
            ),
            (
                OperationalError("busywork failed at /private/path"),
                ("internal_error", False),
            ),
            (
                RuntimeError("secret token and /private/path"),
                ("internal_error", False),
            ),
        )
        for error, expected in cases:
            with self.subTest(error_type=type(error).__name__):
                self.assertEqual(normalize_bulk_exception(error), expected)

    def test_postgresql_uses_only_explicit_retryable_sqlstates(self):
        from core.services.bulk_pin_management import (
            normalize_bulk_exception,
        )

        cases = (
            ("40001", ("database_busy", True)),
            ("40P01", ("database_busy", True)),
            ("55P03", ("database_busy", True)),
            ("42P01", ("internal_error", False)),
        )
        for sqlstate, expected in cases:
            cause = OperationalError("opaque backend error")
            cause.pgcode = sqlstate
            error = OperationalError("secret /private/path")
            error.__cause__ = cause
            with self.subTest(sqlstate=sqlstate), mock.patch.object(
                connection,
                "vendor",
                "postgresql",
            ):
                self.assertEqual(normalize_bulk_exception(error), expected)


class BulkPinWriteAPITests(
    _LinuxStrongPublishMixin,
    TemporaryMediaMixin,
    APITransactionTestCase,
):
    def setUp(self):
        super(BulkPinWriteAPITests, self).setUp()
        self.owner = create_user("bulk-write-owner")
        self.other_user = create_user("bulk-write-other")
        self.source = Board.objects.create(
            submitter=self.owner,
            name="bulk-write-source",
        )
        self.target = Board.objects.create(
            submitter=self.owner,
            name="bulk-write-target",
        )
        self.other_board = Board.objects.create(
            submitter=self.owner,
            name="bulk-write-other-board",
        )
        self.image = create_image()
        self.first = self._create_pin(self.owner)
        self.second = self._create_pin(self.owner)
        self.outside = self._create_pin(self.owner)
        self.foreign = self._create_pin(self.other_user)
        self.source.pins.add(self.first)
        self.client.login(
            username=self.owner.username,
            password="password",
        )

    def _create_pin(self, user, private=False):
        return Pin.objects.create(
            submitter=user,
            image=self.image,
            private=private,
            description="original description",
            referer="https://example.com/original",
        )

    @staticmethod
    def _url():
        return "/api/v2/pins/bulk/"

    def test_bulk_requires_authentication(self):
        self.client.logout()

        response = self.client.post(
            self._url(),
            {"operation": "delete", "pin_ids": [self.first.pk]},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertTrue(Pin.objects.filter(pk=self.first.pk).exists())

    def test_invalid_request_is_code_only_and_changes_nothing(self):
        response = self.client.post(
            self._url(),
            {
                "operation": "update",
                "pin_ids": [self.first.pk],
                "changes": {"description": "forbidden"},
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data, {"code": "bulk_invalid_request"})
        self.first.refresh_from_db()
        self.assertEqual(self.first.description, "original description")

    def test_malformed_json_is_canonical_bulk_invalid_request(self):
        response = self.client.generic(
            "POST",
            self._url(),
            b'{"operation":"delete","pin_ids":[',
            content_type="application/json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data, {"code": "bulk_invalid_request"})
        self.assertTrue(Pin.objects.filter(pk=self.first.pk).exists())

    @override_settings(PINRY_BATCH_MAX_BODY_BYTES=64)
    def test_oversized_json_is_canonical_bulk_invalid_request(self):
        body = (
            '{{"operation":"delete","pin_ids":[{}]}}'.format(
                self.first.pk
            ).encode("ascii")
            + b" " * 128
        )

        response = self.client.generic(
            "POST",
            self._url(),
            body,
            content_type="application/json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data, {"code": "bulk_invalid_request"})
        self.assertTrue(Pin.objects.filter(pk=self.first.pk).exists())

    def test_unsupported_content_type_preserves_drf_415(self):
        response = self.client.generic(
            "POST",
            self._url(),
            b'{"operation":"delete","pin_ids":[1]}',
            content_type="text/plain",
        )

        self.assertEqual(
            response.status_code,
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        )
        self.assertEqual(
            response.data["detail"].code,
            "unsupported_media_type",
        )

    def test_delete_keeps_success_before_later_failure(self):
        with mock.patch.object(
            Pin,
            "delete",
            autospec=True,
            side_effect=[
                (1, {"core.Pin": 1}),
                RuntimeError("busy /private/path"),
            ],
        ):
            response = self.client.post(
                self._url(),
                {
                    "operation": "delete",
                    "pin_ids": [self.first.pk, self.second.pk],
                },
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {
            "operation": "delete",
            "succeeded": 1,
            "preserved": 0,
            "failed": 1,
            "results": [
                {"id": self.first.pk, "status": "deleted"},
                {
                    "id": self.second.pk,
                    "status": "failed",
                    "code": "internal_error",
                    "retryable": False,
                },
            ],
        })
        self.assertNotIn("private", str(response.data))
        self.assertNotIn("busy", str(response.data))

    def test_delete_maps_database_busy_without_raw_message(self):
        with mock.patch.object(
            Pin,
            "delete",
            autospec=True,
            side_effect=OperationalError(
                "database is locked at /private/database.sqlite3"
            ),
        ):
            response = self.client.post(
                self._url(),
                {"operation": "delete", "pin_ids": [self.first.pk]},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {
            "operation": "delete",
            "succeeded": 0,
            "preserved": 0,
            "failed": 1,
            "results": [{
                "id": self.first.pk,
                "status": "failed",
                "code": "database_busy",
                "retryable": True,
            }],
        })
        self.assertNotIn("sqlite3", str(response.data))

    def test_delete_marks_unstarted_items_after_deadline(self):
        clock = mock.Mock(side_effect=[10.0, 10.0, 23.0])
        with mock.patch.object(
            PinViewSet,
            "bulk_clock",
            clock,
        ), mock.patch.object(
            Pin,
            "delete",
            autospec=True,
            return_value=(1, {"core.Pin": 1}),
        ) as delete:
            response = self.client.post(
                self._url(),
                {
                    "operation": "delete",
                    "pin_ids": [self.first.pk, self.second.pk],
                },
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {
            "operation": "delete",
            "succeeded": 1,
            "preserved": 0,
            "failed": 1,
            "results": [
                {"id": self.first.pk, "status": "deleted"},
                {
                    "id": self.second.pk,
                    "status": "failed",
                    "code": "bulk_deadline_exceeded",
                    "retryable": True,
                },
            ],
        })
        self.assertEqual(delete.call_count, 1)
        self.assertGreater(
            23.0,
            10.0 + settings.PINRY_FETCH_TOTAL_TIMEOUT,
        )

    def test_add_preserves_other_boards_and_is_idempotent(self):
        self.other_board.pins.add(self.first, self.second)
        self.target.pins.add(self.second)
        payload = {
            "operation": "add_to_board",
            "pin_ids": [self.first.pk, self.second.pk],
            "board_id": self.target.pk,
        }

        first_response = self.client.post(
            self._url(), payload, format="json"
        )
        second_response = self.client.post(
            self._url(), payload, format="json"
        )

        self.assertEqual(first_response.status_code, status.HTTP_200_OK)
        self.assertEqual(first_response.data, {
            "operation": "add_to_board",
            "succeeded": 2,
            "preserved": 0,
            "failed": 0,
            "results": [
                {"id": self.first.pk, "status": "updated"},
                {"id": self.second.pk, "status": "unchanged"},
            ],
        })
        self.assertEqual(second_response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [item["status"] for item in second_response.data["results"]],
            ["unchanged", "unchanged"],
        )
        self.assertTrue(self.other_board.pins.filter(
            pk=self.first.pk
        ).exists())
        self.assertTrue(self.other_board.pins.filter(
            pk=self.second.pk
        ).exists())

    def test_move_supports_all_success_membership_states(self):
        source_only = self.first
        both = self.second
        target_only = self.outside
        self.source.pins.add(both)
        self.target.pins.add(both, target_only)

        response = self.client.post(
            self._url(),
            {
                "operation": "move_between_boards",
                "pin_ids": [source_only.pk, both.pk, target_only.pk],
                "source_board_id": self.source.pk,
                "target_board_id": self.target.pk,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {
            "operation": "move_between_boards",
            "succeeded": 3,
            "preserved": 0,
            "failed": 0,
            "results": [
                {"id": source_only.pk, "status": "moved"},
                {"id": both.pk, "status": "moved"},
                {"id": target_only.pk, "status": "unchanged"},
            ],
        })
        self.assertFalse(self.source.pins.filter(
            pk__in=[source_only.pk, both.pk, target_only.pk]
        ).exists())
        self.assertEqual(
            set(self.target.pins.filter(
                pk__in=[source_only.pk, both.pk, target_only.pk]
            ).values_list("pk", flat=True)),
            {source_only.pk, both.pk, target_only.pk},
        )

    def test_move_conflict_is_code_only_and_rolls_back_whole_chunk(self):
        response = self.client.post(
            self._url(),
            {
                "operation": "move_between_boards",
                "pin_ids": [self.first.pk, self.outside.pk],
                "source_board_id": self.source.pk,
                "target_board_id": self.target.pk,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data, {"code": "pin_membership_changed"})
        self.assertTrue(self.source.pins.filter(pk=self.first.pk).exists())
        self.assertFalse(self.target.pins.filter(pk=self.first.pk).exists())

    def test_move_hides_private_foreign_and_missing_pin(self):
        private_foreign = self._create_pin(self.other_user, private=True)
        self.source.pins.add(private_foreign)

        for pin_id in (private_foreign.pk, private_foreign.pk + 100000):
            with self.subTest(pin_id=pin_id):
                response = self.client.post(
                    self._url(),
                    {
                        "operation": "move_between_boards",
                        "pin_ids": [pin_id],
                        "source_board_id": self.source.pk,
                        "target_board_id": self.target.pk,
                    },
                    format="json",
                )
                self.assertEqual(
                    response.status_code,
                    status.HTTP_404_NOT_FOUND,
                )
                self.assertEqual(response.data, {"code": "pin_not_found"})

        self.assertTrue(self.source.pins.filter(
            pk=private_foreign.pk
        ).exists())
        self.assertFalse(self.target.pins.filter(
            pk=private_foreign.pk
        ).exists())

    def test_update_supports_all_tag_modes(self):
        cases = (
            ("add", ["new"], ["old"], {"old", "new"}),
            ("remove", ["old"], ["old", "keep"], {"keep"}),
            ("replace", ["new"], ["old"], {"new"}),
            ("replace", [], ["old"], set()),
        )
        for mode, values, before, expected in cases:
            with self.subTest(mode=mode, values=values):
                self.first.tags.set(*before)
                response = self.client.post(
                    self._url(),
                    {
                        "operation": "update",
                        "pin_ids": [self.first.pk],
                        "changes": {
                            "tags": {"mode": mode, "values": values},
                        },
                    },
                    format="json",
                )

                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.assertEqual(response.data, {
                    "operation": "update",
                    "succeeded": 1,
                    "preserved": 0,
                    "failed": 0,
                    "results": [{
                        "id": self.first.pk,
                        "status": "updated",
                    }],
                })
                self.assertEqual(set(self.first.tags.names()), expected)

    def test_update_private_only_preserves_description_and_referer(self):
        response = self.client.post(
            self._url(),
            {
                "operation": "update",
                "pin_ids": [self.first.pk, self.second.pk],
                "changes": {"private": True},
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {
            "operation": "update",
            "succeeded": 2,
            "preserved": 0,
            "failed": 0,
            "results": [
                {"id": self.first.pk, "status": "updated"},
                {"id": self.second.pk, "status": "updated"},
            ],
        })
        for pin in (self.first, self.second):
            pin.refresh_from_db()
            self.assertTrue(pin.private)
            self.assertEqual(pin.description, "original description")
            self.assertEqual(
                pin.referer,
                "https://example.com/original",
            )

    def test_update_failure_rolls_back_whole_chunk(self):
        real_save = Pin.save
        save_count = [0]

        def fail_after_second_save(pin, *args, **kwargs):
            real_save(pin, *args, **kwargs)
            save_count[0] += 1
            if save_count[0] == 2:
                raise RuntimeError("secret update failure /private/path")

        with mock.patch.object(
            Pin,
            "save",
            autospec=True,
            side_effect=fail_after_second_save,
        ):
            response = self.client.post(
                self._url(),
                {
                    "operation": "update",
                    "pin_ids": [self.first.pk, self.second.pk],
                    "changes": {"private": True},
                },
                format="json",
            )

        self.assertEqual(
            response.status_code,
            status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
        self.assertEqual(response.data, {"code": "internal_error"})
        self.first.refresh_from_db()
        self.second.refresh_from_db()
        self.assertFalse(self.first.private)
        self.assertFalse(self.second.private)

    def test_missing_or_non_owned_pin_rejects_atomic_operations(self):
        cases = ("delete", "add_to_board", "update")
        for operation in cases:
            for kind in ("missing", "non_owned"):
                with self.subTest(operation=operation, kind=kind):
                    candidate = self._create_pin(self.owner)
                    invalid_id = (
                        self.foreign.pk
                        if kind == "non_owned"
                        else self.foreign.pk + 100000
                    )
                    payload = {
                        "operation": operation,
                        "pin_ids": [candidate.pk, invalid_id],
                    }
                    if operation == "add_to_board":
                        payload["board_id"] = self.target.pk
                    elif operation == "update":
                        payload["changes"] = {"private": True}

                    response = self.client.post(
                        self._url(), payload, format="json"
                    )

                    self.assertEqual(
                        response.status_code,
                        status.HTTP_404_NOT_FOUND,
                    )
                    self.assertEqual(
                        response.data,
                        {"code": "pin_not_found"},
                    )
                    self.assertTrue(Pin.objects.filter(
                        pk=candidate.pk
                    ).exists())
                    candidate.refresh_from_db()
                    self.assertFalse(candidate.private)
                    self.assertFalse(self.target.pins.filter(
                        pk=candidate.pk
                    ).exists())

    def test_missing_or_non_owned_boards_return_same_code(self):
        foreign_board = Board.objects.create(
            submitter=self.other_user,
            name="bulk-write-foreign-board",
        )
        missing_id = foreign_board.pk + 100000
        cases = (
            {
                "operation": "add_to_board",
                "pin_ids": [self.first.pk],
                "board_id": foreign_board.pk,
            },
            {
                "operation": "move_between_boards",
                "pin_ids": [self.first.pk],
                "source_board_id": missing_id,
                "target_board_id": self.target.pk,
            },
            {
                "operation": "move_between_boards",
                "pin_ids": [self.first.pk],
                "source_board_id": self.source.pk,
                "target_board_id": foreign_board.pk,
            },
            {
                "operation": "delete_if_exclusive_to_board",
                "pin_ids": [self.first.pk],
                "source_board_id": missing_id,
            },
        )
        for payload in cases:
            with self.subTest(operation=payload["operation"]):
                response = self.client.post(
                    self._url(), payload, format="json"
                )
                self.assertEqual(
                    response.status_code,
                    status.HTTP_404_NOT_FOUND,
                )
                self.assertEqual(response.data, {"code": "board_not_found"})

        self.assertTrue(self.source.pins.filter(pk=self.first.pk).exists())
        self.assertFalse(self.target.pins.filter(pk=self.first.pk).exists())

    def test_conditional_delete_aggregates_deleted_and_preserved_codes(self):
        shared = self.second
        foreign = self.foreign
        missing_membership = self.outside
        self.source.pins.add(shared, foreign)
        self.other_board.pins.add(shared)

        response = self.client.post(
            self._url(),
            {
                "operation": "delete_if_exclusive_to_board",
                "pin_ids": [
                    self.first.pk,
                    shared.pk,
                    foreign.pk,
                    missing_membership.pk,
                ],
                "source_board_id": self.source.pk,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {
            "operation": "delete_if_exclusive_to_board",
            "succeeded": 1,
            "preserved": 3,
            "failed": 0,
            "results": [
                {"id": self.first.pk, "status": "deleted"},
                {
                    "id": shared.pk,
                    "status": "preserved",
                    "code": "shared_pin",
                },
                {
                    "id": foreign.pk,
                    "status": "preserved",
                    "code": "non_owned_pin",
                },
                {
                    "id": missing_membership.pk,
                    "status": "preserved",
                    "code": "source_membership_changed",
                },
            ],
        })
        self.assertFalse(Pin.objects.filter(pk=self.first.pk).exists())
        self.assertTrue(Pin.objects.filter(pk=shared.pk).exists())
        self.assertTrue(Pin.objects.filter(pk=foreign.pk).exists())
        self.assertTrue(Pin.objects.filter(
            pk=missing_membership.pk
        ).exists())

    def test_conditional_delete_registered_media_uses_hydrated_pin(self):
        image = create_image()
        image_id = image.pk
        pin = Pin.objects.create(submitter=self.owner, image=image)
        pin_id = pin.pk
        self.source.pins.add(pin)
        image.refresh_from_db()
        media_root = Path(self.temporary_media.name)
        content = (media_root / image.image.name).read_bytes()
        asset = MediaAsset.objects.create(
            submitter=self.owner,
            image=image,
            content_sha256=hashlib.sha256(content).hexdigest(),
        )
        asset_id = asset.pk
        asset_uuid = str(image.asset_uuid)
        asset_directories = (
            media_root / "originals" / asset_uuid,
            media_root / "derivatives" / asset_uuid,
        )
        file_names = {image.image.name}
        file_names.update(
            image.thumbnail_set.values_list("image", flat=True)
        )
        self.assertEqual(len(file_names), 4)
        self.assertTrue(all(
            (media_root / name).is_file() for name in file_names
        ))
        real_delete = Pin.delete_if_exclusive_to_board
        passed_image_ids = []

        def record_image_id(instance, *args, **kwargs):
            passed_image_ids.append(instance.image_id)
            return real_delete(instance, *args, **kwargs)

        with mock.patch.object(
            Pin,
            "delete_if_exclusive_to_board",
            autospec=True,
            side_effect=record_image_id,
        ):
            response = self.client.post(
                self._url(),
                {
                    "operation": "delete_if_exclusive_to_board",
                    "pin_ids": [pin_id],
                    "source_board_id": self.source.pk,
                },
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {
            "operation": "delete_if_exclusive_to_board",
            "succeeded": 1,
            "preserved": 0,
            "failed": 0,
            "results": [{"id": pin_id, "status": "deleted"}],
        })
        self.assertEqual(passed_image_ids, [image_id])
        self.assertFalse(Pin.objects.filter(pk=pin_id).exists())
        self.assertFalse(Image.objects.filter(pk=image_id).exists())
        self.assertFalse(MediaAsset.objects.filter(pk=asset_id).exists())
        self.assertFalse(
            Thumbnail.objects.filter(original_id=image_id).exists()
        )
        self.assertTrue(all(
            not (media_root / name).exists() for name in file_names
        ))
        self.assertTrue(all(
            not directory.exists() for directory in asset_directories
        ))
        self.assertTrue((media_root / "originals").is_dir())
        self.assertTrue((media_root / "derivatives").is_dir())

    def test_conditional_delete_hydrates_before_membership_race(self):
        image = create_image()
        image_id = image.pk
        pin = Pin.objects.create(submitter=self.owner, image=image)
        pin_id = pin.pk
        image.refresh_from_db()
        media_root = Path(self.temporary_media.name)
        content = (media_root / image.image.name).read_bytes()
        asset = MediaAsset.objects.create(
            submitter=self.owner,
            image=image,
            content_sha256=hashlib.sha256(content).hexdigest(),
        )
        asset_id = asset.pk
        file_names = {image.image.name}
        file_names.update(
            image.thumbnail_set.values_list("image", flat=True)
        )
        real_delete = Pin.delete_if_exclusive_to_board
        passed_image_ids = []
        clock_calls = [0]

        def add_membership_after_hydration():
            clock_calls[0] += 1
            if clock_calls[0] == 2:
                self.source.pins.add(pin)
            return 0.0

        def record_image_id(instance, *args, **kwargs):
            passed_image_ids.append(instance.image_id)
            return real_delete(instance, *args, **kwargs)

        with mock.patch.object(
            PinViewSet,
            "bulk_clock",
            side_effect=add_membership_after_hydration,
        ), mock.patch.object(
            Pin,
            "delete_if_exclusive_to_board",
            autospec=True,
            side_effect=record_image_id,
        ):
            response = self.client.post(
                self._url(),
                {
                    "operation": "delete_if_exclusive_to_board",
                    "pin_ids": [pin_id],
                    "source_board_id": self.source.pk,
                },
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {
            "operation": "delete_if_exclusive_to_board",
            "succeeded": 1,
            "preserved": 0,
            "failed": 0,
            "results": [{"id": pin_id, "status": "deleted"}],
        })
        self.assertEqual(clock_calls[0], 2)
        self.assertEqual(passed_image_ids, [image_id])
        self.assertFalse(Pin.objects.filter(pk=pin_id).exists())
        self.assertFalse(Image.objects.filter(pk=image_id).exists())
        self.assertFalse(MediaAsset.objects.filter(pk=asset_id).exists())
        self.assertFalse(
            Thumbnail.objects.filter(original_id=image_id).exists()
        )
        self.assertTrue(all(
            not (media_root / name).exists() for name in file_names
        ))

    def test_atomic_service_errors_are_code_only(self):
        cases = (
            (
                BulkOperationError("pin_not_found", 500),
                status.HTTP_404_NOT_FOUND,
                "pin_not_found",
            ),
            (
                OperationalError("database locked /private/path"),
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "database_busy",
            ),
            (
                OperationalError("no such table: /private/path"),
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                "internal_error",
            ),
            (
                RuntimeError("secret token /private/path"),
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                "internal_error",
            ),
            (
                BulkOperationError("secret /private/path", 418),
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                "internal_error",
            ),
        )
        payload = {
            "operation": "update",
            "pin_ids": [self.first.pk],
            "changes": {"private": True},
        }
        for error, expected_status, expected_code in cases:
            with self.subTest(error_type=type(error).__name__):
                with mock.patch.object(
                    PinViewSet,
                    "bulk_service_class",
                ) as service_class:
                    service_class.return_value.execute.side_effect = error
                    response = self.client.post(
                        self._url(), payload, format="json"
                    )

                self.assertEqual(response.status_code, expected_status)
                self.assertEqual(response.data, {"code": expected_code})
                self.assertNotIn("private/path", str(response.data))

    def test_service_factory_error_is_code_only(self):
        with mock.patch.object(
            PinViewSet,
            "bulk_service_class",
            side_effect=RuntimeError("secret factory /private/path"),
        ):
            response = self.client.post(
                self._url(),
                {
                    "operation": "update",
                    "pin_ids": [self.first.pk],
                    "changes": {"private": True},
                },
                format="json",
            )

        self.assertEqual(
            response.status_code,
            status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
        self.assertEqual(response.data, {"code": "internal_error"})

    def test_service_unsupported_media_type_is_internal_code_only(self):
        with mock.patch.object(
            PinViewSet,
            "bulk_service_class",
        ) as service_class:
            service_class.return_value.execute.side_effect = (
                UnsupportedMediaType("secret /private/path")
            )
            response = self.client.post(
                self._url(),
                {
                    "operation": "update",
                    "pin_ids": [self.first.pk],
                    "changes": {"private": True},
                },
                format="json",
            )

        self.assertEqual(
            response.status_code,
            status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
        self.assertEqual(response.data, {"code": "internal_error"})
        self.assertNotIn("secret", str(response.data))
        self.assertNotIn("private/path", str(response.data))


class BulkPinReadAPITests(APITestCase):
    def setUp(self):
        self.owner = create_user("bulk-read-owner")
        self.other_user = create_user("bulk-read-other")
        self.board = Board.objects.create(
            submitter=self.owner,
            name="bulk-read-board",
        )
        self.other_board = Board.objects.create(
            submitter=self.owner,
            name="bulk-read-other-board",
        )
        self.owned_pin = self._create_pin(self.owner)
        self.foreign_public = self._create_pin(self.other_user)
        self.foreign_private = self._create_pin(self.other_user, private=True)
        self.board.pins.add(
            self.owned_pin,
            self.foreign_public,
        )
        self.client.login(username=self.owner.username, password="password")

    @staticmethod
    def _create_pin(user, private=False):
        return Pin.objects.create(
            submitter=user,
            image=create_image(),
            private=private,
        )

    @staticmethod
    def _selection_url():
        return "/api/v2/pins/selection-ids/"

    def _preview_url(self, board):
        return "/api/v2/boards/{}/delete-preview/".format(board.pk)

    def test_owned_board_selection_returns_descending_visible_ids_and_owner_flags(self):
        response = self.client.get(
            self._selection_url(),
            {"board_id": self.board.pk},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], len(response.data["results"]))
        self.assertEqual(
            response.data["results"],
            [
                {"id": self.foreign_public.pk, "owned": False},
                {"id": self.owned_pin.pk, "owned": True},
            ],
        )

    def test_selection_without_board_returns_only_owned_pins(self):
        response = self.client.get(self._selection_url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data,
            {
                "count": 1,
                "results": [{"id": self.owned_pin.pk, "owned": True}],
            },
        )

    def test_selection_exclusive_owned_returns_only_pins_on_one_board(self):
        exclusive_pin = self._create_pin(self.owner)
        self.board.pins.add(exclusive_pin)
        self.other_board.pins.add(self.owned_pin)

        response = self.client.get(
            self._selection_url(),
            {"board_id": self.board.pk, "exclusive_owned": "true"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data,
            {
                "count": 1,
                "results": [{"id": exclusive_pin.pk, "owned": True}],
            },
        )

    def test_selection_rejects_invalid_query_schema_with_one_error_contract(self):
        invalid_queries = (
            "?unknown=value",
            "?board_id=1&board_id=2",
            "?board_id=%2B1",
            "?board_id=%201",
            "?board_id=1.0",
            "?board_id=text",
            "?board_id=0",
            "?board_id=-1",
            "?board_id=%D9%A1",
            "?exclusive_owned=true",
            "?board_id={}&exclusive_owned=false".format(self.board.pk),
            "?board_id={}&exclusive_owned=True".format(self.board.pk),
            "?board_id={}&exclusive_owned=true%20".format(self.board.pk),
            "?board_id={}&exclusive_owned=true&exclusive_owned=true".format(
                self.board.pk
            ),
        )

        for query in invalid_queries:
            with self.subTest(query=query):
                response = self.client.get(self._selection_url() + query)

                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertEqual(
                    response.data,
                    {"code": "selection_invalid_request"},
                )

    def test_selection_hides_private_foreign_pins(self):
        self.board.pins.add(self.foreign_private)

        response = self.client.get(
            self._selection_url(),
            {"board_id": self.board.pk},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn(
            self.foreign_private.pk,
            [item["id"] for item in response.data["results"]],
        )

    def test_selection_rejects_other_user_or_missing_board(self):
        foreign_board = Board.objects.create(
            submitter=self.other_user,
            name="foreign-bulk-read-board",
        )

        for board_id in (foreign_board.pk, foreign_board.pk + 100000):
            with self.subTest(board_id=board_id):
                response = self.client.get(
                    self._selection_url(),
                    {"board_id": board_id},
                )

                self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_selection_requires_authentication(self):
        self.client.logout()

        response = self.client.get(self._selection_url())

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_selection_rejects_more_than_fifty_thousand_ids_with_one_query(self):
        image = create_image()
        pins = [
            Pin(submitter=self.owner, image=image)
            for _ in range(50001)
        ]
        for start in range(0, len(pins), 500):
            Pin.objects.bulk_create(pins[start:start + 500], batch_size=1000)

        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(self._selection_url())

        selection_queries = [
            query["sql"] for query in captured.captured_queries
            if 'FROM "core_pin"' in query["sql"]
            and query["sql"].lstrip().startswith("SELECT")
        ]
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data, {"code": "selection_too_large"})
        self.assertNotIn("results", response.data)
        self.assertEqual(len(selection_queries), 1, selection_queries)

    def test_delete_preview_categories_are_disjoint(self):
        exclusive_pin = self._create_pin(self.owner)
        self.board.pins.add(exclusive_pin)
        self.other_board.pins.add(self.owned_pin)

        response = self.client.get(self._preview_url(self.board))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data,
            {
                "exclusive_owned_count": 1,
                "shared_owned_count": 1,
                "non_owned_count": 1,
            },
        )

    def test_delete_preview_rejects_query_parameters(self):
        response = self.client.get(self._preview_url(self.board) + "?unknown=value")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data,
            {"code": "selection_invalid_request"},
        )

    def test_delete_preview_rejects_other_user_or_missing_board(self):
        foreign_board = Board.objects.create(
            submitter=self.other_user,
            name="foreign-bulk-preview-board",
        )

        for board_id in (foreign_board.pk, foreign_board.pk + 100000):
            with self.subTest(board_id=board_id):
                response = self.client.get(
                    "/api/v2/boards/{}/delete-preview/".format(board_id)
                )

                self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_delete_preview_requires_authentication(self):
        self.client.logout()

        response = self.client.get(self._preview_url(self.board))

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
