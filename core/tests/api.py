from django.urls import resolve, reverse
from django.db import connection
import mock
from rest_framework import serializers as drf_serializers, status
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.test import APITestCase, APITransactionTestCase

from taggit.managers import _TaggableManager
from taggit.models import Tag

from .helpers import create_image, create_user, create_pin
from core.models import Pin, Image, Board
from core.serializers import PinSerializer
from core.services.media_storage import MediaStorageError
from core.services.pin_import import PinImportError, PinImportService
from core.services.safe_url_fetch import FetchedImage, SafeFetchError
from core.views import PinViewSet
from django_images.test_helpers import TemporaryMediaMixin


def _teardown_models():
    Pin.objects.all().delete()
    Image.objects.all().delete()
    Tag.objects.all().delete()
    Board.objects.all().delete()


def mock_requests_get(url, **kwargs):
    response = mock.Mock(content=open('docs/src/imgs/logo-dark.png', 'rb').read())
    return response


def mock_requests_get_with_non_image_content(url, **kwargs):
    response = mock.Mock(content=b"abcd")
    return response


class ImageTests(TemporaryMediaMixin, APITestCase):
    def test_post_create_unsupported(self):
        url = reverse("image-list")
        data = {}
        response = self.client.post(
            url,
            data=data,
            format='json',
        )
        self.assertEqual(response.status_code, 401, response.data)


class BoardPrivacyTests(TemporaryMediaMixin, APITestCase):

    def setUp(self):
        super(BoardPrivacyTests, self).setUp()
        self.owner = create_user("default")
        self.non_owner = create_user("non_owner")

        self.private_board = Board.objects.create(
            name="test_board",
            submitter=self.owner,
            private=True,
        )
        self.board_url = reverse("board-detail", kwargs={"pk": self.private_board.pk})
        self.boards_url = reverse("board-list")

    def tearDown(self):
        _teardown_models()

    def _create_pin_with_non_owner(self, private):
        image = create_image()
        pin = create_pin(self.non_owner, image=image, tags=[])
        pin.private = private
        pin.save()
        return pin

    def test_should_non_owner_and_anonymous_user_has_no_permission_to_list_private_board(self):
        resp = self.client.get(self.boards_url)
        self.assertEqual(len(resp.json()), 0, resp.json())

        self.client.login(username=self.non_owner.username, password='password')
        resp = self.client.get(self.boards_url)
        self.assertEqual(len(resp.json()), 0, resp.content)

    def test_should_owner_has_permission_to_list_private_board(self):
        self.client.login(username=self.non_owner.username, password='password')
        resp = self.client.get(self.boards_url)
        self.assertEqual(len(resp.json()), 0, resp.content)

    def test_should_non_owner_and_anonymous_user_has_no_permission_to_view_private_board(self):
        resp = self.client.get(self.board_url)
        self.assertEqual(resp.status_code, 404)

        self.client.login(username=self.non_owner.username, password='password')
        resp = self.client.get(self.board_url)
        self.assertEqual(resp.status_code, 404)

    def test_should_owner_has_permission_to_view_private_board(self):
        self.client.login(username=self.owner.username, password='password')
        resp = self.client.get(self.board_url)
        self.assertEqual(resp.status_code, 200)

    def test_should_owner_has_no_permission_to_add_private_pin_of_other_user_to_board(self):
        self.client.login(username=self.owner.username, password='password')

        private_pin_of_other_user = self._create_pin_with_non_owner(True)

        resp = self.client.patch(self.board_url, data={"pins_to_add": [private_pin_of_other_user.id, ]})
        self.assertEqual(resp.status_code, 200)

        resp = self.client.get(self.board_url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['total_pins'], 0, resp.json())

    def test_should_owner_has_permission_to_add_non_private_pin_of_other_user_to_board(self):
        self.client.login(username=self.owner.username, password='password')

        private_pin_of_other_user = self._create_pin_with_non_owner(False)

        resp = self.client.patch(self.board_url, data={"pins_to_add": [private_pin_of_other_user.id, ]})
        self.assertEqual(resp.status_code, 200)

        resp = self.client.get(self.board_url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['total_pins'], 1, resp.json())


class PinPrivacyTests(TemporaryMediaMixin, APITestCase):

    def setUp(self):
        super(PinPrivacyTests, self).setUp()
        self.owner = create_user("default")
        self.non_owner = create_user("non_owner")

        with mock.patch('requests.get', mock_requests_get):
            image = create_image()
        self.private_pin = Pin.objects.create(
            submitter=self.owner,
            image=image,
            private=True,
        )
        self.private_pin_url = reverse("pin-detail", kwargs={"pk": self.private_pin.pk})

        self.board = Board.objects.create(name="test_board", submitter=self.owner)
        self.board.pins.add(self.private_pin)
        self.board.save()
        self.board_url = reverse("board-detail", kwargs={"pk": self.board.pk})

    def tearDown(self):
        _teardown_models()

    def test_should_non_owner_and_anonymous_user_has_no_permission_to_list_private_pin(self):
        resp = self.client.get(reverse("pin-list"))
        self.assertEqual(len(resp.json()['results']), 0, resp.content)

        self.client.login(username=self.non_owner.username, password='password')
        resp = self.client.get(reverse("pin-list"))
        self.assertEqual(len(resp.json()['results']), 0, resp.content)

    def test_should_owner_user_has_permission_to_list_private_pin(self):
        self.client.login(username=self.owner.username, password='password')
        resp = self.client.get(reverse("pin-list"))
        self.assertEqual(len(resp.json()['results']), 1, resp.content)

    def test_should_owner_has_permission_to_view_private_pin(self):
        self.client.login(username=self.owner.username, password='password')
        resp = self.client.get(self.private_pin_url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['id'], self.private_pin.id)

    def test_should_anonymous_user_has_no_permission_to_view_private_pin(self):
        resp = self.client.get(self.private_pin_url)
        self.assertEqual(resp.status_code, 404)

    def test_should_non_owner_has_no_permission_to_view_private_pin(self):
        self.client.login(username=self.non_owner.username, password='password')
        resp = self.client.get(self.private_pin_url)
        self.assertEqual(resp.status_code, 404)


class _SinglePinImport(object):
    def __init__(self, prepare_error=None):
        self.prepare_calls = []
        self.commit_calls = []
        self.closed = 0
        self.prepare_error = prepare_error

    def prepare_url(self, url, referer, deadline):
        self.in_atomic_block = connection.in_atomic_block
        self.prepare_autocommit = connection.get_autocommit()
        self.prepare_calls.append((url, referer, deadline))
        if self.prepare_error is not None:
            raise self.prepare_error
        return object()

    def commit(self, prepared, user, metadata, claim, deadline):
        self.commit_in_atomic_block = connection.in_atomic_block
        self.commit_autocommit = connection.get_autocommit()
        self.commit_calls.append((prepared, user, metadata, claim, deadline))
        pin = Pin.objects.create(
            submitter=user,
            image=create_image(),
            url=metadata.url,
            referer=metadata.referer,
            description=metadata.description,
            private=metadata.private,
        )
        if metadata.tags:
            pin.tags.add(*metadata.tags)
        return pin

    def close(self):
        self.closed += 1


class _SequenceClock(object):
    def __init__(self, values):
        self.values = list(values)

    def __call__(self):
        return self.values.pop(0)


class _LatePrepareAsset(object):
    def __init__(self):
        self.cleanup_calls = 0
        self.is_open = True

    def cleanup(self):
        self.cleanup_calls += 1
        self.is_open = False


class _LatePrepareFetcher(object):
    transport = None

    def fetch(self, url, referer=None, deadline=None):
        del url, referer, deadline
        return FetchedImage(
            content=b"image-bytes",
            image_format="PNG",
            width=1,
            height=1,
            final_url="https://cdn.example/image.png",
        )


class _LatePrepareStorage(object):
    def __init__(self, prepared):
        self.prepared = prepared
        self.publish_calls = 0

    def prepare(self, fetched, asset_uuid, original_filename, deadline=None):
        del fetched, asset_uuid, original_filename, deadline
        return self.prepared

    def publish(self, prepared, deadline=None):
        del prepared, deadline
        self.publish_calls += 1
        raise AssertionError("commit must not be entered after late prepare")


class _LatePrepareIdempotency(object):
    def __init__(self):
        self.fence_calls = 0

    def fence(self, claim):
        del claim
        self.fence_calls += 1
        raise AssertionError("fence must not run after late prepare")


class _AtomicCheckingBatchService(object):
    def __init__(self):
        self.in_atomic_block = None
        self.closed = 0

    def process(self, user, validated_data, started_at):
        del user, started_at
        self.in_atomic_block = connection.in_atomic_block
        self.autocommit = connection.get_autocommit()
        self.prepare_in_atomic_block = connection.in_atomic_block
        self.prepare_autocommit = connection.get_autocommit()
        self.commit_in_atomic_block = connection.in_atomic_block
        self.commit_autocommit = connection.get_autocommit()
        return {
            "batch_id": str(validated_data["batch_id"]),
            "results": [],
            "summary": {
                "created": 0,
                "replayed": 0,
                "failed": 0,
                "conflict": 0,
            },
        }

    def close(self):
        self.closed += 1


class PinTests(TemporaryMediaMixin, APITransactionTestCase):
    _JSON_TYPE = "application/json"

    def setUp(self):
        super(PinTests, self).setUp()
        self.user = create_user("default")
        self.client.login(username=self.user.username, password='password')

    def tearDown(self):
        _teardown_models()

    def test_serializer_validation_does_not_create_tags(self):
        request = mock.Mock(user=self.user)
        serializer = PinSerializer(
            data={
                "url": "https://example.com/image.png",
                "tags": ["not-created-during-validation"],
            },
            context={"request": request},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertFalse(Tag.objects.filter(
            name="not-created-during-validation"
        ).exists())

    def test_serializer_requires_exactly_one_image_source(self):
        image = create_image()
        request = mock.Mock(user=self.user)
        for data in (
            {},
            {"url": "", "image_by_id": image.pk},
            {
                "url": "https://example.com/image.png",
                "image_by_id": image.pk,
            },
        ):
            with self.subTest(data=data):
                serializer = PinSerializer(
                    data=data,
                    context={"request": request},
                )

                self.assertFalse(serializer.is_valid())
                self.assertIn("url-or-image", serializer.errors)
                self.assertEqual(Pin.objects.count(), 0)

    def test_missing_source_keeps_legacy_scalar_error_body(self):
        with mock.patch.object(
            PinViewSet,
            "get_pin_import_service",
            side_effect=AssertionError("URL service must not be built"),
        ):
            response = self.client.post(reverse("pin-list"), {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json(), {
            "url-or-image": "Either url or image_by_id is required."
        })

    def test_serializer_uses_injected_service_for_url_import(self):
        service = _SinglePinImport()
        request = mock.Mock(user=self.user)
        serializer = PinSerializer(
            data={
                "url": "https://example.com/image.png",
                "referer": "https://example.com/",
                "description": "through the shared service",
                "private": True,
            },
            context={
                "request": request,
                "pin_import_service": service,
                "pin_import_deadline": 123.0,
            },
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        pin = serializer.save()

        self.assertEqual(service.prepare_calls, [(
            "https://example.com/image.png",
            "https://example.com/",
            123.0,
        )])
        self.assertEqual(len(service.commit_calls), 1)
        self.assertEqual(service.commit_calls[0][2].url, pin.url)
        self.assertIsNone(service.commit_calls[0][3])
        self.assertEqual(service.commit_calls[0][4], 123.0)

    def test_url_post_uses_request_scoped_service_and_closes_it(self):
        service = _SinglePinImport()
        with mock.patch.object(
            PinViewSet,
            "get_pin_import_service",
            return_value=service,
            create=True,
        ):
            response = self.client.post(
                reverse("pin-list"),
                {
                    "url": "https://example.com/image.png",
                    "description": "request scoped",
                },
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(len(service.prepare_calls), 1)
        self.assertEqual(len(service.commit_calls), 1)
        self.assertEqual(service.closed, 1)

    def test_url_post_preserves_referer_fetch_and_response_meaning(self):
        cases = (
            ({}, "https://example.com/image.png", None),
            ({"referer": None}, None, None),
            ({"referer": ""}, "", ""),
            (
                {"referer": "https://page.example/"},
                "https://page.example/",
                "https://page.example/",
            ),
        )
        for extra, expected_fetch, expected_referer in cases:
            with self.subTest(extra=extra):
                service = _SinglePinImport()
                payload = {"url": "https://example.com/image.png"}
                payload.update(extra)
                with mock.patch.object(
                    PinViewSet,
                    "get_pin_import_service",
                    return_value=service,
                ):
                    response = self.client.post(
                        reverse("pin-list"), payload, format="json"
                    )

                self.assertEqual(response.status_code, status.HTTP_201_CREATED)
                self.assertEqual(service.prepare_calls[0][1], expected_fetch)
                self.assertEqual(
                    service.commit_calls[0][2].referer, expected_referer
                )
                self.assertEqual(response.json()["referer"], expected_referer)
                self.assertEqual(
                    Pin.objects.get(pk=response.json()["id"]).referer,
                    expected_referer,
                )

    def test_url_post_maps_safe_and_internal_errors_without_raw_details(self):
        client_codes = (
            "invalid_url_policy", "blocked_address", "dns_rebinding_detected",
            "too_many_redirects", "unsupported_content_encoding",
            "image_too_large", "image_too_many_pixels",
        )
        cases = [
            (SafeFetchError(code, "127.0.0.1", False),
             status.HTTP_400_BAD_REQUEST, {"url": [code]})
            for code in client_codes
        ] + [
            (
                SafeFetchError("image_fetch_timeout", "timeout", True),
                status.HTTP_503_SERVICE_UNAVAILABLE,
                {"url": ["image_fetch_timeout"]},
            ),
            (
                RuntimeError("/private/secret?token=do-not-leak"),
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                {"url": ["internal_error"]},
            ),
            (
                SafeFetchError("unsupported_http_stack", "secret", False),
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                {"url": ["unsupported_http_stack"]},
            ),
        ]
        for error, expected_status, expected_body in cases:
            with self.subTest(error=type(error).__name__):
                service = _SinglePinImport(error)
                with mock.patch.object(
                    PinViewSet,
                    "get_pin_import_service",
                    return_value=service,
                ):
                    response = self.client.post(
                        reverse("pin-list"),
                        {"url": "https://example.com/image.png"},
                        format="json",
                    )

                self.assertEqual(response.status_code, expected_status)
                self.assertEqual(response.json(), expected_body)
                self.assertEqual(service.closed, 1)

    def test_url_post_factory_and_clock_failures_use_safe_api_boundary(self):
        secret = "/private/secret?token=do-not-leak"
        cases = (
            (
                SafeFetchError("unsupported_http_stack", secret, False),
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                {"url": ["unsupported_http_stack"]},
            ),
            (
                RuntimeError(secret),
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                {"url": ["internal_error"]},
            ),
            (
                ValidationError({"url": [secret]}),
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                {"url": ["internal_error"]},
            ),
            (
                APIException({"url": [secret]}),
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                {"url": ["internal_error"]},
            ),
        )
        for factory_error, expected_status, expected_body in cases:
            with self.subTest(factory_error=type(factory_error).__name__):
                with mock.patch.object(
                    PinViewSet,
                    "get_pin_import_service",
                    side_effect=factory_error,
                ) as factory:
                    response = self.client.post(
                        reverse("pin-list"),
                        {"url": "https://example.com/image.png"},
                        format="json",
                    )

                self.assertEqual(response.status_code, expected_status)
                self.assertEqual(response.json(), expected_body)
                self.assertNotIn(secret, response.content.decode("utf-8"))
                factory.assert_called_once_with()

        factory = mock.Mock()
        with mock.patch.object(
            PinViewSet,
            "batch_clock",
            side_effect=RuntimeError(secret),
        ), mock.patch.object(
            PinViewSet,
            "get_pin_import_service",
            factory,
        ):
            response = self.client.post(
                reverse("pin-list"),
                {"url": "https://example.com/image.png"},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertEqual(response.json(), {"url": ["internal_error"]})
        self.assertNotIn(secret, response.content.decode("utf-8"))
        factory.assert_not_called()

        with mock.patch.object(
            PinViewSet,
            "batch_clock",
            side_effect=ValidationError({"url": [secret]}),
        ), mock.patch.object(
            PinViewSet,
            "get_pin_import_service",
            factory,
        ):
            response = self.client.post(
                reverse("pin-list"),
                {"url": "https://example.com/image.png"},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertEqual(response.json(), {"url": ["internal_error"]})
        self.assertNotIn(secret, response.content.decode("utf-8"))
        factory.assert_not_called()

    def test_url_post_factory_base_exception_reraises_without_service(self):
        for error_type in (KeyboardInterrupt, SystemExit):
            with self.subTest(error_type=error_type.__name__):
                factory = mock.Mock()
                with mock.patch.object(
                    PinViewSet,
                    "batch_clock",
                    side_effect=error_type(),
                ), mock.patch.object(
                    PinViewSet,
                    "get_pin_import_service",
                    factory,
                ):
                    with self.assertRaises(error_type):
                        self.client.post(
                            reverse("pin-list"),
                            {"url": "https://example.com/image.png"},
                            format="json",
                        )

                factory.assert_not_called()

    def test_url_post_closes_after_response_failure_and_close_error(self):
        service = _SinglePinImport()
        with mock.patch.object(
            PinViewSet,
            "get_pin_import_service",
            return_value=service,
        ), mock.patch.object(
            PinSerializer,
            "to_representation",
            side_effect=RuntimeError("response serialization failure"),
        ):
            response = self.client.post(
                reverse("pin-list"),
                {"url": "https://example.com/image.png"},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertEqual(response.json(), {"url": ["internal_error"]})
        self.assertEqual(service.closed, 1)

        service = _SinglePinImport()
        close = mock.Mock(side_effect=RuntimeError("close failure"))
        service.close = close
        with mock.patch.object(
            PinViewSet,
            "get_pin_import_service",
            return_value=service,
        ):
            response = self.client.post(
                reverse("pin-list"),
                {"url": "https://example.com/image.png"},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        close.assert_called_once_with()

    def test_url_post_response_api_exception_is_internal_after_commit(self):
        secret = "/private/response?token=do-not-leak"
        service = _SinglePinImport()
        with mock.patch.object(
            PinViewSet,
            "get_pin_import_service",
            return_value=service,
        ), mock.patch.object(
            PinSerializer,
            "to_representation",
            side_effect=ValidationError({"url": [secret]}),
        ):
            response = self.client.post(
                reverse("pin-list"),
                {"url": "https://example.com/image.png"},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertEqual(response.json(), {"url": ["internal_error"]})
        self.assertNotIn(secret, response.content.decode("utf-8"))
        self.assertEqual(service.closed, 1)
        self.assertEqual(Pin.objects.count(), 1)

    def test_url_post_unknown_typed_error_codes_are_internal(self):
        secret_code = "/private/code?token=do-not-leak"
        cases = (
            SafeFetchError(secret_code, "raw", False),
            MediaStorageError(secret_code, "raw", True),
            PinImportError(secret_code, "raw", False),
            SafeFetchError(123, "raw", False),
            MediaStorageError(123, "raw", True),
            PinImportError(123, "raw", False),
        )
        for error in cases:
            with self.subTest(error_type=type(error).__name__, code=error.code):
                service = _SinglePinImport(error)
                with mock.patch.object(
                    PinViewSet,
                    "get_pin_import_service",
                    return_value=service,
                ):
                    response = self.client.post(
                        reverse("pin-list"),
                        {"url": "https://example.com/image.png"},
                        format="json",
                    )

                self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
                self.assertEqual(response.json(), {
                    "url": ["internal_error"]
                })
                self.assertNotIn(secret_code, response.content.decode("utf-8"))
                self.assertEqual(service.closed, 1)

    def test_url_post_closes_then_reraises_base_exception(self):
        for error_type in (KeyboardInterrupt, SystemExit):
            with self.subTest(error_type=error_type.__name__):
                service = _SinglePinImport(error_type())
                with mock.patch.object(
                    PinViewSet,
                    "get_pin_import_service",
                    return_value=service,
                ):
                    with self.assertRaises(error_type):
                        self.client.post(
                            reverse("pin-list"),
                            {"url": "https://example.com/image.png"},
                            format="json",
                        )

                self.assertEqual(service.closed, 1)

    def test_invalid_url_sources_fail_before_fetch_and_close_created_service(self):
        image = create_image()
        cases = (
            {"url": ""},
            {"url": None},
            {
                "url": "https://example.com/image.png",
                "image_by_id": image.pk,
            },
        )
        for payload in cases:
            with self.subTest(payload=payload):
                service = _SinglePinImport()
                with mock.patch.object(
                    PinViewSet,
                    "get_pin_import_service",
                    return_value=service,
                ):
                    response = self.client.post(
                        reverse("pin-list"), payload, format="json"
                    )

                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertEqual(response.json(), {
                    "url-or-image": [
                        "Either url or image_by_id is required."
                    ]
                })
                self.assertEqual(service.prepare_calls, [])
                self.assertEqual(service.commit_calls, [])
                self.assertEqual(service.closed, 1)
                self.assertEqual(Pin.objects.count(), 0)

    def test_late_real_prepare_returns_503_without_commit_or_db_write(self):
        prepared = _LatePrepareAsset()
        storage = _LatePrepareStorage(prepared)
        idempotency = _LatePrepareIdempotency()
        clock = _SequenceClock((10.0, 23.0))
        service = PinImportService(
            fetcher=_LatePrepareFetcher(),
            media_storage=storage,
            idempotency=idempotency,
            clock=clock,
        )
        with mock.patch.object(PinViewSet, "batch_clock", clock), mock.patch.object(
            PinViewSet,
            "get_pin_import_service",
            return_value=service,
        ):
            response = self.client.post(
                reverse("pin-list"),
                {"url": "https://example.com/image.png"},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertEqual(response.json(), {
            "url": ["image_processing_timeout"]
        })
        self.assertEqual(prepared.cleanup_calls, 1)
        self.assertEqual(storage.publish_calls, 0)
        self.assertEqual(idempotency.fence_calls, 0)
        self.assertEqual(Pin.objects.count(), 0)

    def test_image_by_id_tag_write_failure_rolls_back_pin_and_tags(self):
        image = create_image()
        serializer = PinSerializer(
            data={
                "image_by_id": image.pk,
                "description": "must roll back",
                "tags": ["created-then-rolled-back"],
            },
            context={"request": mock.Mock(user=self.user)},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        real_set = _TaggableManager.set

        def set_then_fail(manager, *tags, **kwargs):
            real_set(manager, *tags, **kwargs)
            raise RuntimeError("after tag write")

        with mock.patch.object(
            _TaggableManager,
            "set",
            side_effect=set_then_fail,
            autospec=True,
        ):
            with self.assertRaises(RuntimeError):
                serializer.save()

        self.assertEqual(Pin.objects.count(), 0)
        self.assertFalse(Tag.objects.filter(
            name="created-then-rolled-back"
        ).exists())

    def test_patch_tag_write_failure_rolls_back_tags_and_body(self):
        image = create_image()
        pin = create_pin(self.user, image, ["old-tag"])
        original_description = pin.description
        serializer = PinSerializer(
            pin,
            data={
                "tags": ["new-tag"],
                "description": "must roll back",
            },
            partial=True,
            context={"request": mock.Mock(user=self.user)},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        with mock.patch.object(
            drf_serializers.ModelSerializer,
            "update",
            side_effect=RuntimeError("after tag replacement"),
        ):
            with self.assertRaises(RuntimeError):
                serializer.save()

        pin.refresh_from_db()
        self.assertEqual(pin.description, original_description)
        self.assertEqual(list(pin.tags.values_list("name", flat=True)), ["old-tag"])
        self.assertFalse(Tag.objects.filter(name="new-tag").exists())

    def test_url_post_runs_import_outside_atomic_requests_transaction(self):
        service = _SinglePinImport()
        previous = connection.settings_dict["ATOMIC_REQUESTS"]
        connection.settings_dict["ATOMIC_REQUESTS"] = True
        try:
            with mock.patch.object(
                PinViewSet,
                "get_pin_import_service",
                return_value=service,
                create=True,
            ):
                response = self.client.post(
                    reverse("pin-list"),
                    {"url": "https://example.com/image.png"},
                    format="json",
                )
        finally:
            connection.settings_dict["ATOMIC_REQUESTS"] = previous

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertFalse(service.in_atomic_block)
        self.assertTrue(service.prepare_autocommit)
        self.assertFalse(service.commit_in_atomic_block)
        self.assertTrue(service.commit_autocommit)

    def test_batch_post_runs_import_outside_atomic_requests_transaction(self):
        service = _AtomicCheckingBatchService()
        previous = connection.settings_dict["ATOMIC_REQUESTS"]
        connection.settings_dict["ATOMIC_REQUESTS"] = True
        try:
            with mock.patch.object(
                PinViewSet,
                "get_batch_service",
                return_value=service,
            ):
                response = self.client.post(
                    "{}batch/".format(reverse("pin-list")),
                    {
                        "batch_id": "11111111-1111-1111-1111-111111111111",
                        "items": [{
                            "client_item_id": (
                                "22222222-2222-2222-2222-222222222222"
                            ),
                            "url": "https://example.com/image.png",
                        }],
                    },
                    format="json",
                )
        finally:
            connection.settings_dict["ATOMIC_REQUESTS"] = previous

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(service.in_atomic_block)
        self.assertTrue(service.autocommit)
        self.assertFalse(service.prepare_in_atomic_block)
        self.assertTrue(service.prepare_autocommit)
        self.assertFalse(service.commit_in_atomic_block)
        self.assertTrue(service.commit_autocommit)
        self.assertEqual(service.closed, 1)

    def test_only_import_callbacks_are_marked_non_atomic(self):
        image = create_image()
        detail_url = reverse("pin-detail", args=[image.pk])
        callback_urls = {
            "list": reverse("pin-list"),
            "batch": "{}batch/".format(reverse("pin-list")),
            "detail": detail_url,
            "trash": "{}trash/".format(reverse("pin-list")),
            "restore": "{}restore/".format(detail_url),
            "permanent": "{}permanent/".format(detail_url),
        }
        markers = {
            name: getattr(resolve(url).func, "_non_atomic_requests", set())
            for name, url in callback_urls.items()
        }

        self.assertEqual(markers["list"], {"default"})
        self.assertEqual(markers["batch"], {"default"})
        for name in ("detail", "trash", "restore", "permanent"):
            self.assertEqual(markers[name], set())

        pin = create_pin(self.user, image, [])
        observed = []

        def query_detail():
            observed.append(connection.in_atomic_block)
            return Pin.objects.filter(pk=pin.pk)

        previous = connection.settings_dict["ATOMIC_REQUESTS"]
        connection.settings_dict["ATOMIC_REQUESTS"] = True
        try:
            with mock.patch.object(
                PinViewSet,
                "get_queryset",
                side_effect=query_detail,
            ):
                response = self.client.get(
                    reverse("pin-detail", args=[pin.pk])
                )
        finally:
            connection.settings_dict["ATOMIC_REQUESTS"] = previous

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(observed, [True])

    def test_should_not_create_pin_if_url_content_invalid(self):
        url = 'http://testserver.com/mocked/logo-01.png'
        create_url = reverse("pin-list")
        referer = 'http://testserver.com/'
        post_data = {
            'url': url,
            'private': False,
            'referer': referer,
            'description': 'That\'s an Apple!'
        }
        service = _SinglePinImport(SafeFetchError(
            "invalid_image_content", "invalid", False
        ))
        with mock.patch.object(
            PinViewSet, "get_pin_import_service", return_value=service
        ):
            response = self.client.post(
                create_url, data=post_data, format="json"
            )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_should_create_pin(self):
        url = 'http://testserver.com/mocked/logo-01.png'
        create_url = reverse("pin-list")
        referer = 'http://testserver.com/'
        post_data = {
            'url': url,
            'private': False,
            'referer': referer,
            'description': 'That\'s an Apple!'
        }
        service = _SinglePinImport()
        with mock.patch.object(
            PinViewSet, "get_pin_import_service", return_value=service
        ):
            response = self.client.post(
                create_url, data=post_data, format="json"
            )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        pin = Pin.objects.get(url=url)
        self.assertIsNotNone(pin.image.image)

    def test_post_create_url_with_empty_tags(self):
        url = 'http://testserver.com/mocked/logo-02.png'
        create_url = reverse("pin-list")
        referer = 'http://testserver.com/'
        post_data = {
            'url': url,
            'referer': referer,
            'description': 'That\'s an Apple!',
            'tags': []
        }
        service = _SinglePinImport()
        with mock.patch.object(
            PinViewSet, "get_pin_import_service", return_value=service
        ):
            response = self.client.post(
                create_url, data=post_data, format="json"
            )
        self.assertEqual(
            response.status_code, status.HTTP_201_CREATED, response.json()
        )
        self.assertEqual(Image.objects.count(), 1)
        pin = Pin.objects.get(url=url)
        self.assertIsNotNone(pin.image.image)
        self.assertEqual(pin.tags.count(), 0)

    def test_should_post_create_pin_with_existed_image(self):
        image = create_image()
        create_pin(self.user, image=image, tags=[])
        create_url = reverse("pin-list")
        referer = 'http://testserver.com/'
        post_data = {
            'referer': referer,
            'image_by_id': image.pk,
            'description': 'That\'s something else (probably a CC logo)!',
            'tags': ['random', 'tags'],
        }
        response = self.client.post(create_url, data=post_data, format="json")
        resp_data = response.json()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, resp_data)
        self.assertEqual(
            resp_data['description'],
            'That\'s something else (probably a CC logo)!',
            resp_data
        )
        self.assertEquals(Pin.objects.count(), 2)

    def test_image_by_id_post_does_not_build_url_import_service(self):
        image = create_image()
        with mock.patch.object(
            PinViewSet,
            "get_pin_import_service",
            side_effect=AssertionError("URL service must not be built"),
        ):
            response = self.client.post(
                reverse("pin-list"),
                {"image_by_id": image.pk, "description": "existing image"},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_patch_detail_unauthenticated(self):
        image = create_image()
        pin = create_pin(self.user, image, [])
        self.client.logout()
        uri = reverse("pin-detail", kwargs={"pk": pin.pk})
        response = self.client.patch(uri, format='json', data={})
        self.assertEqual(response.status_code, 401, response.data)

    def test_patch_detail(self):
        image = create_image()
        pin = create_pin(self.user, image, [])
        uri = reverse("pin-detail", kwargs={"pk": pin.pk})
        new = {'description': 'Updated description'}

        response = self.client.patch(
            uri, new, format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.json())
        self.assertEqual(Pin.objects.count(), 1)
        self.assertEqual(Pin.objects.get(pk=pin.pk).description, new['description'])

    def test_delete_detail_unauthenticated(self):
        image = create_image()
        pin = create_pin(self.user, image, [])
        uri = reverse("pin-detail", kwargs={"pk": pin.pk})
        self.client.logout()
        resp = self.client.delete(uri)
        self.assertEqual(resp.status_code, 401, resp.data)

    def test_delete_detail(self):
        image = create_image()
        pin = create_pin(self.user, image, [])
        uri = reverse("pin-detail", kwargs={"pk": pin.pk})
        response = self.client.delete(uri)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(Pin.objects.count(), 1)
        pin.refresh_from_db()
        self.assertIsNotNone(pin.trashed_at)
