import logging
import re
import time

from django.conf import settings
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_page
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import viewsets, mixins, routers, status
from rest_framework.decorators import action
from rest_framework.exceptions import ParseError, PermissionDenied
from rest_framework.filters import SearchFilter, OrderingFilter
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet
from taggit.models import Tag

from core import serializers as api
from core.batch_serializers import BatchImportRequestSerializer
from core.models import Image, Pin, Board
from core.parsers import LimitedJSONParser
from core.permissions import IsOwnerOrReadOnly, OwnerOnlyIfPrivate
from core.serializers import filter_private_pin, filter_private_board
from core.services.batch_import import BatchImportService
from core.services.bounded_resolver import BoundedResolver
from core.services.idempotency import IdempotencyStore
from core.services.media_storage import MediaStorage
from core.services.pin_import import PinImportService
from core.services.pinned_http import PinnedHTTPTransport
from core.services.safe_url_fetch import SafeUrlFetcher


logger = logging.getLogger(__name__)
_DECIMAL_CONTENT_LENGTH = re.compile(r"\A[0-9]+\Z")


class ImageViewSet(mixins.CreateModelMixin, GenericViewSet):
    queryset = Image.objects.all()
    serializer_class = api.ImageSerializer

    def create(self, request, *args, **kwargs):
        return super(ImageViewSet, self).create(request, *args, **kwargs)


class PinViewSet(viewsets.ModelViewSet):
    serializer_class = api.PinSerializer
    filter_backends = (DjangoFilterBackend, SearchFilter, OrderingFilter)
    filter_fields = ("submitter__username", 'tags__name', "pins__id")
    ordering_fields = ('-id', )
    ordering = ('-id', )
    permission_classes = [
        IsOwnerOrReadOnly("submitter"),
        OwnerOnlyIfPrivate("submitter"),
    ]
    batch_service_class = BatchImportService
    resolver_class = BoundedResolver
    transport_class = PinnedHTTPTransport
    fetcher_class = SafeUrlFetcher
    media_storage_class = MediaStorage
    idempotency_class = IdempotencyStore
    pin_import_service_class = PinImportService
    batch_clock = staticmethod(time.monotonic)

    def get_queryset(self):
        query = Pin.objects.filter(trashed_at__isnull=True)
        request = self.request
        return filter_private_pin(request, query)

    @staticmethod
    def _get_owned_pin(request, pin_id):
        return get_object_or_404(
            Pin.objects.all(), pk=pin_id, submitter=request.user
        )

    def destroy(self, request, *args, **kwargs):
        pin = self._get_owned_pin(request, kwargs["pk"])
        Pin.objects.filter(
            pk=pin.pk, trashed_at__isnull=True
        ).update(trashed_at=timezone.now())
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(
        detail=False,
        methods=["get"],
        permission_classes=[IsAuthenticated],
    )
    def trash(self, request):
        queryset = Pin.objects.filter(
            submitter=request.user,
            trashed_at__isnull=False,
        ).select_related("image", "submitter")
        queryset = self.filter_queryset(queryset)
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    @action(
        detail=True,
        methods=["post"],
        permission_classes=[IsAuthenticated],
    )
    def restore(self, request, pk=None):
        pin = self._get_owned_pin(request, pk)
        Pin.objects.filter(pk=pin.pk).update(trashed_at=None)
        pin.refresh_from_db()
        serializer = self.get_serializer(pin)
        return Response(serializer.data)

    @action(
        detail=True,
        methods=["delete"],
        permission_classes=[IsAuthenticated],
    )
    def permanent(self, request, pk=None):
        pin = self._get_owned_pin(request, pk)
        pin.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(
        detail=False,
        methods=["post"],
        parser_classes=[LimitedJSONParser],
        permission_classes=[IsAuthenticated],
    )
    def batch(self, request):
        started_at = self.batch_clock()
        content_length = self._validate_batch_content_length(request)
        payload = {} if content_length == 0 else request.data
        serializer = BatchImportRequestSerializer(
            data=payload,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        self._preflight_owned_boards(
            request.user,
            serializer.validated_data["board_ids"],
        )
        service = self.get_batch_service()
        try:
            result = service.process(
                request.user,
                serializer.validated_data,
                started_at,
            )
            return Response(result, status=status.HTTP_200_OK)
        finally:
            self._close_batch_service(service)

    def get_batch_service(self):
        clock = self.batch_clock
        transport = self.transport_class(clock=clock)
        try:
            resolver = self.resolver_class(clock=clock)
            fetcher = self.fetcher_class(
                resolver,
                transport,
                clock=clock,
            )
            media_storage = self.media_storage_class(clock=clock)
            idempotency = self.idempotency_class()
            pin_import = self.pin_import_service_class(
                fetcher,
                media_storage,
                idempotency,
                clock=clock,
            )
            return self.batch_service_class(
                fetcher,
                media_storage,
                idempotency,
                pin_import,
                clock=clock,
            )
        except BaseException:
            self._close_resource(transport, "batch_factory")
            raise

    @staticmethod
    def _validate_batch_content_length(request):
        raw_length = request.META.get("CONTENT_LENGTH")
        if (
            not isinstance(raw_length, str)
            or not _DECIMAL_CONTENT_LENGTH.match(raw_length)
        ):
            raise ParseError({
                "code": "batch_invalid_content_length",
                "message": "A valid Content-Length header is required.",
            })
        normalized_length = raw_length.lstrip("0") or "0"
        maximum = str(settings.PINRY_BATCH_MAX_BODY_BYTES)
        if (
            len(normalized_length) > len(maximum)
            or len(normalized_length) == len(maximum)
            and normalized_length > maximum
        ):
            raise ParseError({
                "code": "batch_body_too_large",
                "message": "The batch request body is too large.",
            })
        return int(normalized_length)

    @staticmethod
    def _preflight_owned_boards(user, board_ids):
        if not board_ids:
            return
        owned_count = Board.objects.filter(
            submitter=user,
            pk__in=board_ids,
        ).count()
        if owned_count != len(board_ids):
            raise PermissionDenied({
                "code": "batch_board_access_denied",
                "message": "One or more boards are not available.",
            })

    @classmethod
    def _close_batch_service(cls, service):
        cls._close_resource(service, "batch_service")

    @staticmethod
    def _close_resource(resource, event):
        try:
            resource.close()
        except BaseException as error:
            try:
                logger.error(
                    "batch_import_close_failed event=%s error_type=%s",
                    event,
                    type(error).__name__,
                )
            except BaseException:
                pass


class BoardViewSet(viewsets.ModelViewSet):
    serializer_class = api.BoardSerializer
    filter_backends = (DjangoFilterBackend, OrderingFilter, SearchFilter)
    search_fields = ("name", )
    filter_fields = ("submitter__username", )
    ordering_fields = ('-id', )
    ordering = ('-id', )
    permission_classes = [
        IsOwnerOrReadOnly("submitter"),
        OwnerOnlyIfPrivate("submitter"),
    ]

    def get_queryset(self):
        return filter_private_board(self.request, Board.objects.all())


class BoardAutoCompleteViewSet(
    mixins.ListModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class = api.BoardAutoCompleteSerializer
    filter_backends = (DjangoFilterBackend, OrderingFilter)
    filter_fields = ("submitter__username", )
    ordering_fields = ('-id', )
    ordering = ('-id', )
    pagination_class = None
    permission_classes = [OwnerOnlyIfPrivate("submitter"), ]

    def get_queryset(self):
        return filter_private_board(self.request, Board.objects.all())


class TagAutoCompleteViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    queryset = Tag.objects.all()
    serializer_class = api.TagAutoCompleteSerializer
    pagination_class = None

    @method_decorator(cache_page(60 * 5))
    def list(self, request, *args, **kwargs):
        return super(TagAutoCompleteViewSet, self).list(
            request,
            *args,
            **kwargs
        )


drf_router = routers.DefaultRouter()
drf_router.register(r'pins', PinViewSet, basename="pin")
drf_router.register(r'images', ImageViewSet)
drf_router.register(r'boards', BoardViewSet, basename="board")
drf_router.register(r'tags-auto-complete', TagAutoCompleteViewSet)
drf_router.register(
    r'boards-auto-complete',
    BoardAutoCompleteViewSet,
    basename="board",
)
