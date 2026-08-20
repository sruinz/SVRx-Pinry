from django.conf import settings
from rest_framework import serializers


class BatchImportItemSerializer(serializers.Serializer):
    client_item_id = serializers.UUIDField()
    url = serializers.URLField(max_length=2048)


class BatchImportRequestSerializer(serializers.Serializer):
    batch_id = serializers.UUIDField()
    board_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        allow_empty=True,
        max_length=100,
        required=False,
        default=list,
    )
    tags = serializers.ListField(
        child=serializers.CharField(max_length=100),
        allow_empty=True,
        max_length=100,
        required=False,
        default=list,
    )
    private = serializers.BooleanField(default=False)
    referer = serializers.URLField(
        max_length=2048,
        required=False,
        allow_blank=True,
        default="",
    )
    description = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
    )
    items = BatchImportItemSerializer(
        many=True,
        min_length=1,
        max_length=settings.PINRY_BATCH_MAX_ITEMS,
    )

    def validate(self, attrs):
        item_ids = [item["client_item_id"] for item in attrs["items"]]
        if len(item_ids) != len(set(item_ids)):
            raise serializers.ValidationError({
                "items": ["client_item_id values must be unique."],
            })
        attrs["tags"] = sorted(set(attrs["tags"]))
        attrs["board_ids"] = sorted(set(attrs["board_ids"]))
        return attrs
