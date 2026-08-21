from django.db import connection
from django.test import SimpleTestCase
from django.test.utils import CaptureQueriesContext
from rest_framework import status
from rest_framework.test import APITestCase

from core.bulk_serializers import BulkPinRequestSerializer
from core.models import Board, Pin
from core.tests.helpers import create_image, create_user


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
