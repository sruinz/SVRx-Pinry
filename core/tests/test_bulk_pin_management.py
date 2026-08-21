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
