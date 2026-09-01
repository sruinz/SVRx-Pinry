from django.conf import settings
from django.db import transaction
from django.db.models import Q
from rest_framework import serializers
from rest_framework.exceptions import APIException, ValidationError
from taggit.models import Tag

from core.models import Image, Board
from core.models import Pin
from django_images.models import Thumbnail
from django_images.paths import UnsupportedImageFormat
from core.services.local_upload import LocalUploadError
from core.services.media_storage import MediaStorageError
from core.services.board_cover import BoardCoverError
from core.services.pin_import import ImportMetadata, PinImportError
from core.services.safe_url_fetch import SafeFetchError
from users.models import User
from users.serializers import PublicUserSerializer


def filter_private_pin(request, query):
    if request.user.is_authenticated:
        query = query.exclude(~Q(submitter=request.user), private=True)
    else:
        query = query.exclude(private=True)
    return query.select_related('image', 'submitter')


def filter_private_board(request, query):
    if request.user.is_authenticated:
        query = query.exclude(~Q(submitter=request.user), private=True)
    else:
        query = query.exclude(private=True)
    return query


class PinBoardMembershipSerializer(serializers.ModelSerializer):
    contains_pin = serializers.BooleanField(read_only=True)

    class Meta:
        model = Board
        fields = (
            "id",
            "name",
            "contains_pin",
        )


class ThumbnailSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = Thumbnail
        fields = (
            "image",
            "width",
            "height",
        )


class ImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = Image
        fields = (
            "id",
            "image",
            "width",
            "height",
            "standard",
            "thumbnail",
            "square",
        )
        extra_kwargs = {
            "width": {"read_only": True},
            "height": {"read_only": True},
        }

    standard = ThumbnailSerializer(read_only=True)
    thumbnail = ThumbnailSerializer(read_only=True)
    square = ThumbnailSerializer(read_only=True)

    def create(self, validated_data):
        try:
            image = super(ImageSerializer, self).create(validated_data)
        except UnsupportedImageFormat:
            raise ValidationError(
                {"image": ["unsupported_image_format"]}
            )
        Thumbnail.objects.get_or_create_at_sizes(image, settings.IMAGE_SIZES.keys())
        return image


class TagSerializer(serializers.SlugRelatedField):
    class Meta:
        model = Tag
        fields = ("name",)

    queryset = Tag.objects.all()

    def __init__(self, **kwargs):
        super(TagSerializer, self).__init__(
            slug_field="name",
            **kwargs
        )

    def to_internal_value(self, data):
        if not isinstance(data, str):
            raise ValidationError("Invalid tag name.")
        return data


class URLImportUnavailable(APIException):
    status_code = 503


class URLImportConflict(APIException):
    status_code = 409


class URLImportInternalError(APIException):
    status_code = 500


class BoardCoverChanged(APIException):
    status_code = 409
    default_code = "board_cover_changed"

    def __init__(self):
        super(BoardCoverChanged, self).__init__({
            "code": "board_cover_changed"
        })


_URL_CLIENT_ERROR_CODES = frozenset((
    "invalid_url_policy",
    "blocked_address",
    "dns_rebinding_detected",
    "too_many_redirects",
    "unsupported_content_encoding",
    "image_too_large",
    "image_too_many_pixels",
))
_URL_RETRYABLE_ERROR_CODES = frozenset((
    "image_fetch_timeout",
    "image_processing_timeout",
    "media_storage_failed",
))
_URL_INTERNAL_ERROR_CODES = frozenset((
    "internal_error",
    "unsupported_http_stack",
    "media_path_conflict",
    "image_processing_failed",
    "media_configuration_error",
    "media_publish_changed",
    "media_storage_unsupported",
))


def raise_url_import_error(error, source_field="url"):
    code = getattr(error, "code", "internal_error")
    if type(code) is not str:
        raise URLImportInternalError({source_field: ["internal_error"]})
    detail = {source_field: [code]}
    if code == "invalid_image_content":
        if source_field == "url":
            raise ValidationError({"url": "invalid image content"})
        raise ValidationError(detail)
    if code == "unsupported_image_format":
        raise ValidationError(detail)
    if code in _URL_CLIENT_ERROR_CODES:
        raise ValidationError(detail)
    if code in ("lease_lost", "board_access_changed"):
        raise URLImportConflict(detail)
    if code == "image_download_failed":
        retryable = getattr(error, "retryable", None)
        if type(retryable) is not bool:
            raise URLImportInternalError({source_field: ["internal_error"]})
        if retryable:
            raise URLImportUnavailable(detail)
        raise URLImportInternalError(detail)
    if code in _URL_RETRYABLE_ERROR_CODES:
        raise URLImportUnavailable(detail)
    if code in _URL_INTERNAL_ERROR_CODES:
        raise URLImportInternalError(detail)
    raise URLImportInternalError({source_field: ["internal_error"]})


class PinSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = Pin
        fields = (
            settings.DRF_URL_FIELD_NAME,
            "private",
            "id",
            "submitter",
            "url",
            "description",
            "referer",
            "image",
            "image_file",
            "tags",
            "board_ids",
        )

    submitter = PublicUserSerializer(read_only=True)
    tags = TagSerializer(
        many=True,
        source="tag_list",
        required=False,
    )
    image = ImageSerializer(required=False, read_only=True)
    image_file = serializers.FileField(
        write_only=True,
        required=False,
        allow_empty_file=True,
    )
    board_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        write_only=True,
        required=False,
        allow_empty=True,
    )

    def validate(self, attrs):
        if self.instance is not None:
            return attrs
        if "image_by_id" in self.initial_data:
            raise ValidationError({
                "image_by_id": ["unsupported_pin_image_reference"]
            })
        has_url = bool(attrs.get("url"))
        has_image = "image_file" in attrs
        if (
            "url" in self.initial_data and not has_url
            or has_url == has_image
        ):
            raise ValidationError({
                "url-or-image": (
                    "Exactly one of url or image_file is required."
                )
            })
        board_ids = sorted(set(attrs.get("board_ids", ())))
        if board_ids:
            request = self.context.get("request")
            user = getattr(request, "user", None)
            owned_count = Board.objects.filter(
                submitter=user,
                pk__in=board_ids,
            ).count()
            if owned_count != len(board_ids):
                raise ValidationError({
                    "board_ids": ["board_access_denied"]
                })
        attrs["board_ids"] = board_ids
        return attrs

    def create(self, validated_data):
        submitter = self.context['request'].user
        tags = tuple(validated_data.pop('tag_list', ()))
        board_ids = tuple(validated_data.pop("board_ids", ()))
        service = self.context.get("pin_import_service")
        deadline = self.context.get("pin_import_deadline")
        if service is None or deadline is None:
            raise URLImportInternalError({"url": ["internal_error"]})
        if 'url' in validated_data:
            url = validated_data.pop('url')
            referer_provided = 'referer' in validated_data
            referer = validated_data.pop('referer', None)
            metadata = ImportMetadata(
                url=url,
                referer=referer,
                description=validated_data.get('description'),
                private=validated_data.get('private', False),
                tags=tags,
                board_ids=board_ids,
            )
            try:
                prepared = service.prepare_url(
                    url,
                    referer if referer_provided else url,
                    deadline,
                )
                return service.commit(
                    prepared,
                    submitter,
                    metadata,
                    None,
                    deadline,
                )
            except (PinImportError, SafeFetchError, MediaStorageError):
                raise
            except Exception:
                raise URLImportInternalError(
                    {"url": ["internal_error"]}
                ) from None

        uploaded_file = validated_data.pop("image_file")
        referer = validated_data.pop("referer", None)
        metadata = ImportMetadata(
            url=None,
            referer=referer,
            description=validated_data.get("description"),
            private=validated_data.get("private", False),
            tags=tags,
            board_ids=board_ids,
        )
        try:
            prepared = service.prepare_upload(uploaded_file, deadline)
            return service.commit(
                prepared,
                submitter,
                metadata,
                None,
                deadline,
            )
        except (
            LocalUploadError,
            MediaStorageError,
            PinImportError,
        ):
            raise
        except Exception:
            raise URLImportInternalError(
                {"image_file": ["internal_error"]}
            ) from None

    def update(self, instance, validated_data):
        tags = validated_data.pop('tag_list', None)
        if "private" in validated_data:
            service = self.context["board_cover_service"]
            request = self.context["request"]
            try:
                with service.pin_privacy_transition(
                    request.user,
                    [instance.pk],
                    validated_data["private"],
                ) as pins:
                    return self._update_locked_pin(
                        pins[instance.pk],
                        validated_data,
                        tags,
                    )
            except BoardCoverError as error:
                if error.code == "board_cover_changed":
                    raise BoardCoverChanged() from None
                raise
        with transaction.atomic():
            return self._update_locked_pin(instance, validated_data, tags)

    def _update_locked_pin(self, instance, validated_data, tags):
        if tags:
            instance.tags.set(*tags)
        else:
            instance.tags.set()
        # change for image-id or image is not allowed
        validated_data.pop('image_file', None)
        validated_data.pop('board_ids', None)
        return super(PinSerializer, self).update(instance, validated_data)


class PinIdListField(serializers.ListField):
    child = serializers.IntegerField(
        min_value=1
    )


class BoardCoverUpdateSerializer(serializers.Serializer):
    pin_id = serializers.IntegerField(
        min_value=1,
        allow_null=True,
        required=True,
    )

    def validate(self, attrs):
        if set(self.initial_data.keys()) != {"pin_id"}:
            raise ValidationError({"code": "board_cover_invalid"})
        raw_pin_id = self.initial_data["pin_id"]
        if raw_pin_id is not None and (
            type(raw_pin_id) is not int or raw_pin_id <= 0
        ):
            raise ValidationError({"code": "board_cover_invalid"})
        return attrs


class BoardOrderRequestSerializer(serializers.Serializer):
    version = serializers.RegexField(
        r"\A[0-9a-f]{64}\Z",
        trim_whitespace=False,
    )
    board_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        allow_empty=True,
    )

    def validate(self, attrs):
        if set(self.initial_data.keys()) != {"version", "board_ids"}:
            raise ValidationError({"code": "board_order_invalid"})
        raw_version = self.initial_data["version"]
        raw_board_ids = self.initial_data["board_ids"]
        if type(raw_version) is not str or not isinstance(
            raw_board_ids,
            list,
        ):
            raise ValidationError({"code": "board_order_invalid"})
        if any(
            type(board_id) is not int or board_id <= 0
            for board_id in raw_board_ids
        ):
            raise ValidationError({"code": "board_order_invalid"})
        if len(raw_board_ids) != len(set(raw_board_ids)):
            raise ValidationError({"code": "board_order_invalid"})
        return attrs


class BoardAutoCompleteSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = Board
        fields = (
            settings.DRF_URL_FIELD_NAME,
            'id',
            'name',
        )


class BoardSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = Board
        fields = (
            settings.DRF_URL_FIELD_NAME,
            "id",
            "name",
            "private",
            "total_pins",
            "cover",
            "cover_pin_id",
            "published",
            "submitter",
            "pins_to_add",
            "pins_to_remove",
        )
        read_only_fields = ('submitter', 'published')
        extra_kwargs = {
            'submitter': {"view_name": "users:user-detail"},
        }

    submitter = PublicUserSerializer(read_only=True)
    total_pins = serializers.SerializerMethodField(
        read_only=True,
    )
    cover = serializers.SerializerMethodField(
        read_only=True,
    )
    cover_pin_id = serializers.SerializerMethodField(
        read_only=True,
    )
    pins_to_add = PinIdListField(
        max_length=10,
        write_only=True,
        required=False,
        allow_empty=False,
        help_text="only patch method works for this field",
    )
    pins_to_remove = PinIdListField(
        max_length=10,
        write_only=True,
        required=False,
        allow_empty=False,
        help_text="only patch method works for this field"
    )

    def get_total_pins(self, instance):
        query = instance.pins.all()
        request = self.context['request']
        query = filter_private_pin(request, query)
        return query.count()

    def _cover_resolution(self, instance):
        cache_name = "_serialized_cover_resolution"
        if not hasattr(instance, cache_name):
            service = self.context["board_cover_service"]
            setattr(
                instance,
                cache_name,
                service.resolve(instance, self.context["request"]),
            )
        return getattr(instance, cache_name)

    def get_cover(self, instance: Board) -> dict or None:
        pin, _manual_id = self._cover_resolution(instance)
        if pin is None:
            return None
        return PinSerializer(pin, context=self.context).data

    def get_cover_pin_id(self, instance):
        _pin, manual_id = self._cover_resolution(instance)
        return manual_id

    def update(self, instance: Board, validated_data):
        pins_to_add = validated_data.pop("pins_to_add", [])
        pins_to_remove = validated_data.pop("pins_to_remove", [])
        with transaction.atomic():
            instance = Board.objects.select_for_update().get(pk=instance.pk)
            pin_ids = set(pins_to_add) | set(pins_to_remove)
            if instance.cover_pin_id is not None:
                pin_ids.add(instance.cover_pin_id)
            list(
                Pin.objects.select_for_update()
                .filter(pk__in=sorted(pin_ids))
                .order_by("pk")
            )
            board = Board.objects.filter(
                submitter=instance.submitter,
                name=validated_data.get('name', None)
            ).first()
            if board and board.id != instance.id:
                raise ValidationError(
                    detail={'name': "Board with this name already exists"}
                )
            instance = super(BoardSerializer, self).update(
                instance,
                validated_data,
            )
            membership_service = self.context["pin_membership_service"]
            instance = membership_service.update_board_membership(
                self.context["request"].user,
                instance.pk,
                pins_to_add,
                pins_to_remove,
            )
            cover_service = self.context["board_cover_service"]
            cover_service.reconcile_locked_board(instance)
            return instance

    def create(self, validated_data):
        validated_data.pop('pins_to_remove', None)
        validated_data.pop('pins_to_add', None)
        user = self.context['request'].user
        with transaction.atomic():
            locked_user = (
                User.objects.select_for_update().get(pk=user.pk)
            )
            if Board.objects.filter(
                name=validated_data['name'],
                submitter=locked_user,
            ).exists():
                raise ValidationError(
                    detail={
                        "name": "board with this name already exists."
                    }
                )
            validated_data['submitter'] = locked_user
            return super(BoardSerializer, self).create(validated_data)


class TagAutoCompleteSerializer(serializers.ModelSerializer):

    class Meta:
        model = Tag
        fields = ('name', )
