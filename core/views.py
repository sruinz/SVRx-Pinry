import logging
import re
import time
from functools import wraps

from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_page
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import viewsets, mixins, routers, status
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ParseError, PermissionDenied
from rest_framework.exceptions import UnsupportedMediaType
from rest_framework.exceptions import ValidationError
from rest_framework.filters import SearchFilter, OrderingFilter
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet
from taggit.models import Tag

from core import serializers as api
from core.batch_serializers import BatchImportRequestSerializer
from core.bulk_serializers import BulkPinRequestSerializer
from core.models import Image, Pin, Board
from core.parsers import LimitedJSONParser
from core.permissions import IsOwnerOrReadOnly, OwnerOnlyIfPrivate
from core.serializers import filter_private_pin, filter_private_board
from core.services.batch_import import BatchImportService
from core.services.bulk_pin_management import (
    BulkOperationError,
    BulkPinManagementService,
    normalize_bulk_exception,
    parse_selection_query,
)
from core.services.bounded_resolver import BoundedResolver
from core.services.idempotency import IdempotencyStore
from core.services.local_upload import LocalUploadError
from core.services.media_storage import MediaStorage
from core.services.pin_membership import (
    MembershipConflict,
    PinMembershipService,
)
from core.services.pin_import import PinImportError, PinImportService
from core.services.pinned_http import PinnedHTTPTransport
from core.services.safe_url_fetch import SafeFetchError, SafeUrlFetcher
from core.services.media_storage import MediaStorageError


logger = logging.getLogger(__name__)
_DECIMAL_CONTENT_LENGTH = re.compile(r"\A[0-9]+\Z")


class ImageViewSet(mixins.CreateModelMixin, GenericViewSet):
    queryset = Image.objects.all()
    serializer_class = api.ImageSerializer

    def create(self, request, *args, **kwargs):
        return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)


class PinViewSet(viewsets.ModelViewSet):
    _NON_ATOMIC_ACTIONS = frozenset(
        ("create", "batch", "bulk", "destroy")
    )
    _BULK_ERROR_STATUS = {
        "board_not_found": status.HTTP_404_NOT_FOUND,
        "pin_not_found": status.HTTP_404_NOT_FOUND,
        "pin_membership_changed": status.HTTP_409_CONFLICT,
        "database_busy": status.HTTP_503_SERVICE_UNAVAILABLE,
        "internal_error": status.HTTP_500_INTERNAL_SERVER_ERROR,
    }
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
    bulk_service_class = BulkPinManagementService
    bulk_clock = staticmethod(time.monotonic)
    bulk_pin_management_service_class = BulkPinManagementService

    @classmethod
    def as_view(cls, actions=None, **initkwargs):
        view = super(PinViewSet, cls).as_view(
            actions=actions,
            **initkwargs
        )
        if not actions or not cls._NON_ATOMIC_ACTIONS.intersection(
            actions.values()
        ):
            return view

        @wraps(view)
        def selectively_atomic(request, *args, **kwargs):
            action_name = actions.get(request.method.lower())
            database = transaction.get_connection()
            if (
                action_name in cls._NON_ATOMIC_ACTIONS
                or not database.settings_dict.get("ATOMIC_REQUESTS")
            ):
                return view(request, *args, **kwargs)
            with transaction.atomic(using=database.alias):
                return view(request, *args, **kwargs)

        return transaction.non_atomic_requests(selectively_atomic)

    def create(self, request, *args, **kwargs):
        service = None
        source_field = "url"
        self._pin_import_deadline = None
        try:
            try:
                serializer = self.get_serializer(data=request.data)
                serializer.is_valid(raise_exception=True)
                source_field = (
                    "image_file"
                    if "image_file" in serializer.validated_data
                    else "url"
                )
            except ValidationError:
                raise
            except Exception:
                raise api.URLImportInternalError(
                    {"url": ["internal_error"]}
                ) from None
            try:
                self._pin_import_deadline = (
                    self.batch_clock() + settings.PINRY_FETCH_TOTAL_TIMEOUT
                )
                if source_field == "image_file":
                    service = self.get_local_pin_import_service()
                else:
                    service = self.get_pin_import_service()
                self._pin_import_service = service
                serializer.context["pin_import_service"] = service
                serializer.context["pin_import_deadline"] = (
                    self._pin_import_deadline
                )
            except (
                LocalUploadError,
                MediaStorageError,
                PinImportError,
                SafeFetchError,
            ) as error:
                api.raise_url_import_error(error, source_field)
            except Exception:
                raise api.URLImportInternalError(
                    {source_field: ["internal_error"]}
                ) from None
            try:
                self.perform_create(serializer)
            except (
                LocalUploadError,
                MediaStorageError,
                PinImportError,
                SafeFetchError,
            ) as error:
                api.raise_url_import_error(error, source_field)
            except Exception:
                raise api.URLImportInternalError(
                    {source_field: ["internal_error"]}
                ) from None
            try:
                headers = self.get_success_headers(serializer.data)
                return Response(
                    serializer.data,
                    status=status.HTTP_201_CREATED,
                    headers=headers,
                )
            except Exception:
                raise api.URLImportInternalError(
                    {source_field: ["internal_error"]}
                ) from None
        finally:
            self._pin_import_service = None
            self._pin_import_deadline = None
            if service is not None:
                self._close_pin_import_service(service)

    def get_serializer_context(self):
        context = super(PinViewSet, self).get_serializer_context()
        service = getattr(self, "_pin_import_service", None)
        if service is not None:
            context["pin_import_service"] = service
            context["pin_import_deadline"] = self._pin_import_deadline
        return context

    def get_queryset(self):
        return filter_private_pin(self.request, Pin.objects.all())

    @staticmethod
    def _get_owned_pin(request, pin_id):
        return get_object_or_404(
            Pin.objects.all(), pk=pin_id, submitter=request.user
        )

    def destroy(self, request, *args, **kwargs):
        pin = self._get_owned_pin(request, kwargs["pk"])
        pin.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(
        detail=False,
        methods=["get"],
        permission_classes=[IsAuthenticated],
        url_path="selection-ids",
    )
    def selection_ids(self, request):
        try:
            selection = parse_selection_query(request.query_params)
            result = self.bulk_pin_management_service_class().selection_ids(
                request.user,
                **selection
            )
        except BulkOperationError as error:
            return Response({"code": error.code}, status=error.status_code)
        return Response(result)

    @action(
        detail=False,
        methods=["post"],
        url_path="bulk",
        url_name="bulk",
        parser_classes=[LimitedJSONParser],
        permission_classes=[IsAuthenticated],
    )
    def bulk(self, request):
        try:
            serializer = BulkPinRequestSerializer(data=request.data)
            if not serializer.is_valid():
                return Response(
                    {"code": "bulk_invalid_request"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            service = self.bulk_service_class(clock=self.bulk_clock)
            result = service.execute(
                request.user,
                serializer.validated_data,
                self.bulk_clock(),
            )
        except UnsupportedMediaType:
            raise
        except ParseError:
            return Response(
                {"code": "bulk_invalid_request"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except BulkOperationError as error:
            error_status = self._BULK_ERROR_STATUS.get(error.code)
            if error_status is None:
                return Response(
                    {"code": "internal_error"},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )
            return Response(
                {"code": error.code},
                status=error_status,
            )
        except Exception as error:
            code, retryable = normalize_bulk_exception(error)
            del retryable
            error_status = (
                status.HTTP_503_SERVICE_UNAVAILABLE
                if code == "database_busy"
                else status.HTTP_500_INTERNAL_SERVER_ERROR
            )
            return Response({"code": code}, status=error_status)
        return Response(result, status=status.HTTP_200_OK)

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
        pin_import = self.get_pin_import_service()
        try:
            return self.batch_service_class(
                pin_import.fetcher,
                pin_import.media_storage,
                pin_import.idempotency,
                pin_import,
                clock=self.batch_clock,
            )
        except BaseException:
            self._close_pin_import_service(pin_import)
            raise

    def get_pin_import_service(self):
        clock = self.batch_clock
        transport = self.transport_class(clock=clock)
        try:
            resolver = self.resolver_class(clock=clock)
            fetcher = self.fetcher_class(resolver, transport, clock=clock)
            media_storage = self.media_storage_class(clock=clock)
            idempotency = self.idempotency_class()
            return self.pin_import_service_class(
                fetcher,
                media_storage,
                idempotency,
                clock=clock,
            )
        except BaseException:
            self._close_resource(transport, "pin_import_factory")
            raise

    def get_local_pin_import_service(self):
        clock = self.batch_clock
        media_storage = self.media_storage_class(clock=clock)
        idempotency = self.idempotency_class()
        return self.pin_import_service_class(
            None,
            media_storage,
            idempotency,
            clock=clock,
        )

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

    @classmethod
    def _close_pin_import_service(cls, service):
        if callable(getattr(service, "close", None)):
            cls._close_resource(service, "pin_import_service")
            return
        cls._close_resource(
            getattr(getattr(service, "fetcher", None), "transport", None),
            "pin_import_transport",
        )

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
    bulk_pin_management_service_class = BulkPinManagementService
    pin_membership_service_class = PinMembershipService

    def get_serializer_context(self):
        context = super(BoardViewSet, self).get_serializer_context()
        context["pin_membership_service"] = (
            self.pin_membership_service_class()
        )
        return context

    def get_queryset(self):
        return filter_private_board(self.request, Board.objects.all())

    @action(
        detail=True,
        methods=["get"],
        permission_classes=[IsAuthenticated],
        url_path="delete-preview",
    )
    def delete_preview(self, request, pk=None):
        try:
            if request.query_params:
                raise BulkOperationError("selection_invalid_request", 400)
            result = self.bulk_pin_management_service_class().board_delete_preview(
                request.user,
                pk,
            )
        except BulkOperationError as error:
            return Response({"code": error.code}, status=error.status_code)
        return Response(result)

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        try:
            self.pin_membership_service_class().delete_board(
                request.user,
                instance.pk,
            )
        except MembershipConflict as error:
            if error.code == "board_not_found":
                raise NotFound()
            raise
        return Response(status=status.HTTP_204_NO_CONTENT)


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
