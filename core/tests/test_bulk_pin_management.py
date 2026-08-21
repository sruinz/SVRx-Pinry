from django.test import SimpleTestCase

from core.bulk_serializers import BulkPinRequestSerializer


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
