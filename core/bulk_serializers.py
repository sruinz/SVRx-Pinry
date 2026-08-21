from rest_framework import serializers
from rest_framework.exceptions import ErrorDetail


OPERATION_KEYS = {
    "delete": frozenset(("operation", "pin_ids")),
    "add_to_board": frozenset(("operation", "pin_ids", "board_id")),
    "move_between_boards": frozenset(
        ("operation", "pin_ids", "source_board_id", "target_board_id")
    ),
    "update": frozenset(("operation", "pin_ids", "changes")),
    "delete_if_exclusive_to_board": frozenset(
        ("operation", "pin_ids", "source_board_id")
    ),
}


class StrictPositiveIntegerField(serializers.IntegerField):
    def to_internal_value(self, data):
        if type(data) is not int:
            raise serializers.ValidationError("invalid")
        return super(StrictPositiveIntegerField, self).to_internal_value(data)


class StrictBooleanField(serializers.BooleanField):
    def to_internal_value(self, data):
        if type(data) is not bool:
            raise serializers.ValidationError("invalid")
        return super(StrictBooleanField, self).to_internal_value(data)


class StrictTagNameField(serializers.CharField):
    def to_internal_value(self, data):
        if type(data) is not str:
            raise serializers.ValidationError("invalid")
        return super(StrictTagNameField, self).to_internal_value(data.strip())


class StrictSerializer(serializers.Serializer):
    def to_internal_value(self, data):
        if not isinstance(data, dict):
            raise serializers.ValidationError("invalid")
        writable = {
            field.field_name for field in self._writable_fields
        }
        if set(data) - writable:
            raise serializers.ValidationError("invalid")
        return super(StrictSerializer, self).to_internal_value(data)

    def is_valid(self, raise_exception=False):
        valid = super(StrictSerializer, self).is_valid(
            raise_exception=False
        )
        if not valid:
            self._errors = {"code": [ErrorDetail(
                "bulk_invalid_request", code="bulk_invalid_request"
            )]}
            if raise_exception:
                from rest_framework.exceptions import ValidationError
                raise ValidationError(self.errors)
        return valid


class BulkPinTagsSerializer(StrictSerializer):
    mode = serializers.ChoiceField(choices=("add", "remove", "replace"))
    values = serializers.ListField(
        child=StrictTagNameField(max_length=100),
        min_length=0,
        max_length=50,
    )

    def validate(self, attrs):
        values = attrs["values"]
        if attrs["mode"] in ("add", "remove") and not values:
            raise serializers.ValidationError("values cannot be empty")
        unique = []
        for value in values:
            if value not in unique:
                unique.append(value)
        attrs["values"] = unique
        return attrs


class BulkPinChangesSerializer(StrictSerializer):
    private = StrictBooleanField(required=False)
    tags = BulkPinTagsSerializer(required=False)

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError("changes cannot be empty")
        return attrs


class BulkPinRequestSerializer(StrictSerializer):
    operation = serializers.ChoiceField(choices=tuple(OPERATION_KEYS))
    pin_ids = serializers.ListField(
        child=StrictPositiveIntegerField(min_value=1),
        min_length=1,
        max_length=50,
    )
    board_id = StrictPositiveIntegerField(min_value=1, required=False)
    source_board_id = StrictPositiveIntegerField(min_value=1, required=False)
    target_board_id = StrictPositiveIntegerField(min_value=1, required=False)
    changes = BulkPinChangesSerializer(required=False)

    def validate_pin_ids(self, value):
        if len(value) != len(set(value)):
            raise serializers.ValidationError("pin_ids must be unique")
        return value

    def validate(self, attrs):
        operation = attrs.get("operation")
        if operation is None:
            raise serializers.ValidationError("operation is required")
        if set(self.initial_data) != OPERATION_KEYS[operation]:
            raise serializers.ValidationError("invalid operation keys")
        if operation == "update" and "changes" not in attrs:
            raise serializers.ValidationError("changes is required")
        if operation == "move_between_boards":
            if attrs["source_board_id"] == attrs["target_board_id"]:
                raise serializers.ValidationError("boards must differ")
        return attrs
