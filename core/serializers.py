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
from core.services.media_storage import MediaStorageError
from core.services.pin_import import ImportMetadata, PinImportError
from core.services.safe_url_fetch import SafeFetchError
from users.serializers import UserSerializer
from users.models import User


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


def raise_url_import_error(error):
    code = getattr(error, "code", "internal_error")
    if type(code) is not str:
        raise URLImportInternalError({"url": ["internal_error"]})
    detail = {"url": [code]}
    if code == "invalid_image_content":
        raise ValidationError({"url": "invalid image content"})
    if code == "unsupported_image_format":
        raise ValidationError({"url": ["unsupported_image_format"]})
    if code in _URL_CLIENT_ERROR_CODES:
        raise ValidationError(detail)
    if code in ("lease_lost", "board_access_changed"):
        raise URLImportConflict(detail)
    if code == "image_download_failed":
        retryable = getattr(error, "retryable", None)
        if type(retryable) is not bool:
            raise URLImportInternalError({"url": ["internal_error"]})
        if retryable:
            raise URLImportUnavailable(detail)
        raise URLImportInternalError(detail)
    if code in _URL_RETRYABLE_ERROR_CODES:
        raise URLImportUnavailable(detail)
    if code in _URL_INTERNAL_ERROR_CODES:
        raise URLImportInternalError(detail)
    raise URLImportInternalError({"url": ["internal_error"]})


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
            "image_by_id",
            "tags",
        )

    submitter = UserSerializer(read_only=True)
    tags = TagSerializer(
        many=True,
        source="tag_list",
        required=False,
    )
    image = ImageSerializer(required=False, read_only=True)
    image_by_id = serializers.PrimaryKeyRelatedField(
        queryset=Image.objects.all(),
        write_only=True,
        required=False,
    )

    def validate(self, attrs):
        if self.instance is not None:
            return attrs
        has_url = bool(attrs.get("url"))
        has_image = "image_by_id" in attrs
        if (
            "url" in self.initial_data and not has_url
            or has_url == has_image
        ):
            raise ValidationError({
                "url-or-image": "Either url or image_by_id is required."
            })
        return attrs

    def create(self, validated_data):
        submitter = self.context['request'].user
        tags = tuple(validated_data.pop('tag_list', ()))
        if 'url' in validated_data:
            url = validated_data.pop('url')
            referer_provided = 'referer' in validated_data
            referer = validated_data.pop('referer', None)
            service = self.context.get("pin_import_service")
            deadline = self.context.get("pin_import_deadline")
            if service is None or deadline is None:
                raise URLImportInternalError({"url": ["internal_error"]})
            metadata = ImportMetadata(
                url=url,
                referer=referer,
                description=validated_data.get('description'),
                private=validated_data.get('private', False),
                tags=tags,
                board_ids=(),
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

        image = validated_data.pop("image_by_id")
        with transaction.atomic():
            pin = Pin.objects.create(
                submitter=submitter,
                image=image,
                **validated_data
            )
            if tags:
                pin.tags.set(*tags)
        return pin

    def update(self, instance, validated_data):
        tags = validated_data.pop('tag_list', None)
        with transaction.atomic():
            if tags:
                instance.tags.set(*tags)
            else:
                instance.tags.set()
            # change for image-id or image is not allowed
            validated_data.pop('image_by_id', None)
            return super(PinSerializer, self).update(instance, validated_data)


class PinIdListField(serializers.ListField):
    child = serializers.IntegerField(
        min_value=1
    )


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
            "published",
            "submitter",
            "pins_to_add",
            "pins_to_remove",
        )
        read_only_fields = ('submitter', 'published')
        extra_kwargs = {
            'submitter': {"view_name": "users:user-detail"},
        }

    submitter = UserSerializer(read_only=True)
    total_pins = serializers.SerializerMethodField(
        read_only=True,
    )
    cover = serializers.SerializerMethodField(
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

    def get_cover(self, instance: Board) -> dict or None:
        request = self.context['request']
        pin = filter_private_pin(request, instance.pins.all()).first()
        if pin is None:
            return None
        return PinSerializer(pin, context=self.context).data

    @staticmethod
    def _get_list(pins_id, submitter: User):
        pins = Pin.objects.filter(id__in=pins_id)
        valid_pins = []
        for pin in pins:
            if pin.private and pin.submitter != submitter:
                continue
            valid_pins.append(pin)
        return valid_pins

    def update(self, instance: Board, validated_data):
        pins_to_add = validated_data.pop("pins_to_add", [])
        pins_to_remove = validated_data.pop("pins_to_remove", [])
        board = Board.objects.filter(
            submitter=instance.submitter,
            name=validated_data.get('name', None)
        ).first()
        if board and board.id != instance.id:
            raise ValidationError(
                detail={'name': "Board with this name already exists"}
            )
        instance = super(BoardSerializer, self).update(instance, validated_data)
        changed = False
        if pins_to_add:
            changed = True
            for pin in self._get_list(pins_to_add, instance.submitter):
                instance.pins.add(pin)
        if pins_to_remove:
            changed = True
            for pin in self._get_list(pins_to_remove, instance.submitter):
                instance.pins.remove(pin)
        if changed:
            instance.save()
        return instance

    def create(self, validated_data):
        validated_data.pop('pins_to_remove', None)
        validated_data.pop('pins_to_add', None)
        user = self.context['request'].user
        if Board.objects.filter(name=validated_data['name'], submitter=user).exists():
            raise ValidationError(
                detail={"name": "board with this name already exists."}
            )
        validated_data['submitter'] = user
        return super(BoardSerializer, self).create(validated_data)


class TagAutoCompleteSerializer(serializers.ModelSerializer):

    class Meta:
        model = Tag
        fields = ('name', )
