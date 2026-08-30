from io import BytesIO

from django.conf import settings
from rest_framework import serializers
from rest_framework.parsers import JSONParser

from core.bulk_serializers import StrictPositiveIntegerField, StrictSerializer


MAX_EXPORT_JSON_BYTES = 1024 * 1024


class ExportJSONParser(JSONParser):
    def parse(self, stream, media_type=None, parser_context=None):
        body = stream.read(MAX_EXPORT_JSON_BYTES + 1)
        if len(body) > MAX_EXPORT_JSON_BYTES:
            raise serializers.ValidationError("invalid_target")
        return super(ExportJSONParser, self).parse(
            BytesIO(body),
            media_type=media_type,
            parser_context=parser_context,
        )


class ExportRequestSerializer(StrictSerializer):
    scope = serializers.ChoiceField(choices=("pins", "board"))
    board_id = StrictPositiveIntegerField(min_value=1, required=False)
    pin_ids = serializers.ListField(
        child=StrictPositiveIntegerField(min_value=1),
        required=False,
        allow_empty=False,
        max_length=settings.PINRY_EXPORT_MAX_PINS,
    )

    def validate_pin_ids(self, values):
        if len(values) != len(set(values)):
            raise serializers.ValidationError("invalid_target")
        return values

    def validate(self, attrs):
        scope = attrs.get("scope")
        expected = "board_id" if scope == "board" else "pin_ids"
        forbidden = "pin_ids" if expected == "board_id" else "board_id"
        if expected not in attrs or forbidden in attrs:
            raise serializers.ValidationError("invalid_target")
        return attrs
