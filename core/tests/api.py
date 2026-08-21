from io import BytesIO
import os
from pathlib import Path

from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import resolve, reverse
from django.db import connection
from django.db.models.query import QuerySet
import mock
from PIL import Image as PILImage
from rest_framework import serializers as drf_serializers, status
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.test import APITestCase, APITransactionTestCase

from taggit.models import Tag

from .helpers import create_image, create_user, create_pin
from core.models import Board, Image, MediaAsset, Pin
from core.serializers import (
    PinSerializer,
    URLImportConflict,
    URLImportInternalError,
    URLImportUnavailable,
)
from core.services.media_storage import MediaStorageError
from core.services.pin_import import PinImportError, PinImportService
from core.services.safe_url_fetch import (
    FetchedImage,
    SafeFetchError,
    SafeUrlFetcher,
)
from core.views import PinViewSet
from django_images.test_helpers import TemporaryMediaMixin
from django_images.models import Thumbnail
from django_images import file_ops


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


def _png_upload(filename="from-client.png", size=(32, 24), color="red"):
    output = BytesIO()
    PILImage.new("RGB", size, color).save(output, format="PNG")
    content = output.getvalue()
    return SimpleUploadedFile(filename, content, content_type="image/png")


def _media_files(media_root):
    root = Path(media_root)
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
        and path.relative_to(root).parts[0] != ".pinry-locks"
    }


class ImageTests(TemporaryMediaMixin, APITestCase):
    def test_anonymous_image_list_is_not_available(self):
        owner = create_user("private-image-owner")
        image = create_image()
        Pin.objects.create(submitter=owner, image=image, private=True)

        response = self.client.get(reverse("image-list"))

        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_authenticated_post_create_is_not_available(self):
        user = create_user("image-create-closed")
        self.client.login(username=user.username, password="password")
        url = reverse("image-list")
        response = self.client.post(
            url,
            data={"image": _png_upload()},
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
        self.assertEqual(Image.objects.count(), 0)
        self.assertEqual(_media_files(self.temporary_media.name), {})


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

    def test_multipart_upload_creates_one_complete_owned_asset(self):
        board = Board.objects.create(
            submitter=self.user,
            name="multipart-board",
        )
        upload = _png_upload("camera original.png")
        original = upload.read()
        upload.seek(0)

        with self._strong_publish_support(), mock.patch.object(
            PinViewSet,
            "transport_class",
            side_effect=AssertionError("local upload built HTTP transport"),
        ) as transport, mock.patch.object(
            PinViewSet,
            "resolver_class",
            side_effect=AssertionError("local upload built resolver"),
        ) as resolver:
            response = self.client.post(
                reverse("pin-list"),
                {
                    "image_file": upload,
                    "referer": "https://page.example/",
                    "description": "multipart upload",
                    "private": "true",
                    "tags": ["alpha", "beta"],
                    "board_ids": [str(board.pk)],
                },
                format="multipart",
            )

        self.assertEqual(
            response.status_code,
            status.HTTP_201_CREATED,
            getattr(response, "data", None),
        )
        pin = Pin.objects.get(pk=response.json()["id"])
        image = Image.objects.get(pk=pin.image_id)
        asset = MediaAsset.objects.get(image=image)
        files = _media_files(self.temporary_media.name)
        self.assertEqual(Pin.objects.count(), 1)
        self.assertEqual(Image.objects.count(), 1)
        self.assertEqual(Thumbnail.objects.count(), 3)
        self.assertEqual(MediaAsset.objects.count(), 1)
        self.assertEqual(asset.submitter_id, self.user.pk)
        self.assertEqual(image.original_filename, "camera original.png")
        self.assertEqual(files[image.image.name], original)
        self.assertEqual(len(files), 4)
        self.assertEqual(set(pin.tags.names()), {"alpha", "beta"})
        self.assertTrue(board.pins.filter(pk=pin.pk).exists())
        self.assertTrue(pin.private)
        self.assertIsNone(pin.url)
        staging = Path(self.temporary_media.name, ".staging")
        self.assertEqual(
            list(staging.rglob("*.part")) if staging.exists() else [],
            [],
        )
        transport.assert_not_called()
        resolver.assert_not_called()

    def test_invalid_local_uploads_leave_no_database_or_media_state(self):
        valid = _png_upload().read()
        cases = (
            (
                SimpleUploadedFile(
                    "empty.png", b"", content_type="image/png"
                ),
                {},
                "invalid_image_content",
            ),
            (
                SimpleUploadedFile(
                    "invalid.png", b"not-an-image", content_type="image/png"
                ),
                {},
                "invalid_image_content",
            ),
            (
                SimpleUploadedFile(
                    "large.png", valid, content_type="image/png"
                ),
                {"PINRY_FETCH_MAX_BYTES": len(valid) - 1},
                "image_too_large",
            ),
            (
                SimpleUploadedFile(
                    "pixels.png", valid, content_type="image/png"
                ),
                {"PINRY_FETCH_MAX_PIXELS": 767},
                "image_too_many_pixels",
            ),
        )
        for upload, settings_override, expected_code in cases:
            with self.subTest(expected_code=expected_code), override_settings(
                **settings_override
            ):
                response = self.client.post(
                    reverse("pin-list"),
                    {"image_file": upload},
                    format="multipart",
                )

            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertEqual(response.json(), {
                "image_file": [expected_code]
            })
            self.assertEqual(Pin.objects.count(), 0)
            self.assertEqual(Image.objects.count(), 0)
            self.assertEqual(Thumbnail.objects.count(), 0)
            self.assertEqual(MediaAsset.objects.count(), 0)
            self.assertEqual(_media_files(self.temporary_media.name), {})

    def test_local_upload_rejects_remote_or_different_root_storage(self):
        foreign_root = Path(self.temporary_media.name, "foreign-storage")
        foreign_root.mkdir()
        remote_storage = mock.Mock()
        remote_storage.location = self.temporary_media.name
        remote_storage.save = mock.Mock()
        different_root_storage = FileSystemStorage(
            location=str(foreign_root)
        )
        different_root_storage.save = mock.Mock()
        cases = (
            (Image._meta.get_field("image"), remote_storage, "remote"),
            (
                Thumbnail._meta.get_field("image"),
                different_root_storage,
                "different-root",
            ),
        )

        for field, configured_storage, label in cases:
            with self.subTest(storage=label):
                with mock.patch.object(
                    field,
                    "storage",
                    configured_storage,
                ), self._strong_publish_support():
                    response = self.client.post(
                        reverse("pin-list"),
                        {"image_file": _png_upload()},
                        format="multipart",
                    )
                try:
                    self.assertEqual(
                        response.status_code,
                        status.HTTP_500_INTERNAL_SERVER_ERROR,
                    )
                    self.assertEqual(response.json(), {
                        "image_file": ["media_configuration_error"]
                    })
                    configured_storage.save.assert_not_called()
                    self.assertEqual(Pin.objects.count(), 0)
                    self.assertEqual(Image.objects.count(), 0)
                    self.assertEqual(Thumbnail.objects.count(), 0)
                    self.assertEqual(MediaAsset.objects.count(), 0)
                    self.assertEqual(
                        _media_files(self.temporary_media.name),
                        {},
                    )
                    self.assertEqual(
                        _media_files(str(foreign_root)),
                        {},
                    )
                finally:
                    for pin in list(Pin.objects.all()):
                        pin.delete()

    def test_local_upload_rejects_same_root_storage_subclass(self):
        class EvilFileSystemStorage(FileSystemStorage):
            def url(inner_self, name):
                return "evil://{}".format(name)

            def open(inner_self, name, mode="rb"):
                del name, mode
                return BytesIO(b"evil")

            def delete(inner_self, name):
                del name

            def save(inner_self, name, content, max_length=None):
                del content, max_length
                return name

        evil_storage = EvilFileSystemStorage(
            location=self.temporary_media.name,
        )
        image_field = Image._meta.get_field("image")

        with mock.patch.object(
            image_field,
            "storage",
            evil_storage,
        ), self._strong_publish_support():
            response = self.client.post(
                reverse("pin-list"),
                {"image_file": _png_upload()},
                format="multipart",
            )

        self.assertEqual(
            response.status_code,
            status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
        self.assertEqual(response.json(), {
            "image_file": ["media_configuration_error"]
        })
        self.assertEqual(Pin.objects.count(), 0)
        self.assertEqual(Image.objects.count(), 0)
        self.assertEqual(Thumbnail.objects.count(), 0)
        self.assertEqual(MediaAsset.objects.count(), 0)
        self.assertEqual(_media_files(self.temporary_media.name), {})

    def test_foreign_board_rejects_before_local_prepare(self):
        other = create_user("foreign-board-owner")
        board = Board.objects.create(submitter=other, name="foreign")

        with mock.patch.object(
            PinViewSet,
            "get_local_pin_import_service",
        ) as factory:
            response = self.client.post(
                reverse("pin-list"),
                {
                    "image_file": _png_upload(),
                    "tags": ["must-not-exist"],
                    "board_ids": [str(board.pk)],
                },
                format="multipart",
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json(), {
            "board_ids": ["board_access_denied"]
        })
        factory.assert_not_called()
        self.assertFalse(Tag.objects.filter(name="must-not-exist").exists())
        self.assertEqual(Pin.objects.count(), 0)
        self.assertEqual(Image.objects.count(), 0)
        self.assertEqual(MediaAsset.objects.count(), 0)
        self.assertEqual(_media_files(self.temporary_media.name), {})

    def test_local_publish_and_database_faults_leave_no_owned_state(self):
        real_create = QuerySet.create

        def fail_media_asset_create(queryset, **kwargs):
            if queryset.model is MediaAsset:
                raise RuntimeError("/private/database/token")
            return real_create(queryset, **kwargs)

        faults = (
            (
                mock.patch(
                    "core.services.media_storage.MediaStorage.publish",
                    side_effect=MediaStorageError(
                        "media_storage_failed",
                        "private media root",
                        True,
                    ),
                ),
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "media_storage_failed",
            ),
            (
                mock.patch.object(
                    QuerySet,
                    "create",
                    new=fail_media_asset_create,
                ),
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                "internal_error",
            ),
        )
        for fault, expected_status, expected_code in faults:
            with self.subTest(expected_code=expected_code), fault, \
                    self._strong_publish_support():
                response = self.client.post(
                    reverse("pin-list"),
                    {"image_file": _png_upload()},
                    format="multipart",
                )

            self.assertEqual(response.status_code, expected_status)
            self.assertEqual(response.json(), {
                "image_file": [expected_code]
            })
            self.assertNotIn("private", response.content.decode("utf-8"))
            self.assertEqual(Pin.objects.count(), 0)
            self.assertEqual(Image.objects.count(), 0)
            self.assertEqual(Thumbnail.objects.count(), 0)
            self.assertEqual(MediaAsset.objects.count(), 0)
            self.assertEqual(_media_files(self.temporary_media.name), {})

    def _strong_publish_support(self):
        if file_ops.sys.platform.startswith("linux"):
            return mock.patch(
                "django_images.file_ops.sys.platform",
                file_ops.sys.platform,
            )
        return mock.patch.multiple(
            "django_images.file_ops",
            sys=mock.Mock(platform="linux"),
            _publish_linux_descriptor=mock.Mock(
                side_effect=self._link_staging_descriptor
            ),
        )

    def _link_staging_descriptor(
        self,
        descriptor,
        destination_directory_descriptor,
        destination_name,
    ):
        expected = os.fstat(descriptor)
        staging_root = Path(self.temporary_media.name, ".staging")
        for candidate in staging_root.rglob("*.part"):
            current = os.stat(str(candidate), follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (
                expected.st_dev,
                expected.st_ino,
            ):
                continue
            os.link(
                str(candidate),
                destination_name,
                dst_dir_fd=destination_directory_descriptor,
                follow_symlinks=False,
            )
            return
        raise OSError("test staging descriptor path was not found")

    def test_image_by_id_cannot_attach_another_users_image(self):
        other = create_user("image-owner")
        image = create_image()
        create_pin(other, image=image, tags=[])

        response = self.client.post(
            reverse("pin-list"),
            {"image_by_id": image.pk, "description": "forged ownership"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Pin.objects.count(), 1)

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
        request = mock.Mock(user=self.user)
        for data in (
            {},
            {"url": "", "image_file": _png_upload()},
            {
                "url": "https://example.com/image.png",
                "image_file": _png_upload(),
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

    def test_missing_source_fails_before_import_service_creation(self):
        with mock.patch.object(
            PinViewSet,
            "get_pin_import_service",
            side_effect=AssertionError("URL service must not be built"),
        ):
            response = self.client.post(reverse("pin-list"), {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json(), {
            "url-or-image": [
                "Exactly one of url or image_file is required."
            ]
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
                        reverse("pin-list"),
                        payload,
                        format=(
                            "multipart"
                            if "image_file" in payload
                            else "json"
                        ),
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

    def test_url_post_validation_failure_does_not_create_service(self):
        secret = "/private/validation?token=do-not-leak"
        cases = (
            APIException({"url": [secret]}),
            RuntimeError(secret),
        )
        for error in cases:
            with self.subTest(error_type=type(error).__name__):
                with mock.patch.object(
                    PinViewSet,
                    "get_pin_import_service",
                ) as factory, mock.patch.object(
                    PinViewSet,
                    "get_local_pin_import_service",
                ), mock.patch.object(
                    PinSerializer,
                    "is_valid",
                    side_effect=error,
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
                self.assertNotIn(secret, response.content.decode("utf-8"))
                factory.assert_not_called()

    def test_url_post_perform_stage_exception_is_internal(self):
        secret = "/private/perform?token=do-not-leak"
        cases = (
            ValidationError({"url": [secret]}),
            URLImportUnavailable({"url": [secret]}),
            URLImportConflict({"url": [secret]}),
            URLImportInternalError({"url": [secret]}),
        )
        for error in cases:
            with self.subTest(error_type=type(error).__name__):
                service = _SinglePinImport()
                with mock.patch.object(
                    PinViewSet,
                    "get_pin_import_service",
                    return_value=service,
                ), mock.patch.object(
                    PinViewSet,
                    "perform_create",
                    side_effect=error,
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
                self.assertNotIn(secret, response.content.decode("utf-8"))
                self.assertEqual(service.closed, 1)

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

    def test_image_download_failed_keeps_safe_fetch_retryability(self):
        cases = (
            (404, status.HTTP_500_INTERNAL_SERVER_ERROR),
            (503, status.HTTP_503_SERVICE_UNAVAILABLE),
        )
        for source_status, expected_status in cases:
            with self.subTest(source_status=source_status):
                with self.assertRaises(SafeFetchError) as caught:
                    SafeUrlFetcher._verify_status(source_status)
                error = caught.exception
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

                self.assertEqual(error.code, "image_download_failed")
                self.assertEqual(response.status_code, expected_status)
                self.assertEqual(response.json(), {
                    "url": ["image_download_failed"]
                })
                self.assertEqual(service.closed, 1)

    def test_image_download_failed_requires_boolean_retryability(self):
        errors = (
            SafeFetchError("image_download_failed", "raw", None),
            SafeFetchError("image_download_failed", "raw", "yes"),
        )
        missing = SafeFetchError("image_download_failed", "raw", False)
        del missing.retryable
        for error in errors + (missing,):
            with self.subTest(retryable=getattr(error, "retryable", None)):
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

    def test_invalid_sources_fail_before_import_service_creation(self):
        cases = (
            {"url": ""},
            {"url": None},
            {
                "url": "https://example.com/image.png",
                "image_file": _png_upload(),
            },
        )
        for payload in cases:
            with self.subTest(payload=payload):
                with mock.patch.object(
                    PinViewSet,
                    "get_pin_import_service",
                ) as url_factory, mock.patch.object(
                    PinViewSet,
                    "get_local_pin_import_service",
                ) as local_factory:
                    response = self.client.post(
                        reverse("pin-list"),
                        payload,
                        format=(
                            "multipart"
                            if "image_file" in payload
                            else "json"
                        ),
                    )

                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertEqual(response.json(), {
                    "url-or-image": [
                        "Exactly one of url or image_file is required."
                    ]
                })
                url_factory.assert_not_called()
                local_factory.assert_not_called()
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

    def test_image_by_id_is_rejected_before_tag_creation(self):
        image = create_image()
        serializer = PinSerializer(
            data={
                "image_by_id": image.pk,
                "description": "must roll back",
                "tags": ["created-then-rolled-back"],
            },
            context={"request": mock.Mock(user=self.user)},
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("image_by_id", serializer.errors)
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

    def test_import_and_delete_callbacks_restore_atomic_read_actions(self):
        image = create_image()
        detail_url = reverse("pin-detail", args=[image.pk])
        callback_urls = {
            "list": reverse("pin-list"),
            "batch": "{}batch/".format(reverse("pin-list")),
            "detail": detail_url,
        }
        markers = {
            name: getattr(resolve(url).func, "_non_atomic_requests", set())
            for name, url in callback_urls.items()
        }

        self.assertEqual(markers["list"], {"default"})
        self.assertEqual(markers["batch"], {"default"})
        self.assertEqual(markers["detail"], {"default"})

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

    def test_registered_delete_takes_dedup_stripe_outside_atomic_requests(
        self,
    ):
        image = create_image()
        pin = create_pin(self.user, image, [])
        MediaAsset.objects.create(
            submitter=self.user,
            image=image,
            content_sha256="a" * 64,
        )
        observed = []

        class RecordingStripe(object):
            def __enter__(inner_self):
                observed.append((
                    connection.in_atomic_block,
                    connection.get_autocommit(),
                ))
                return inner_self

            def __exit__(inner_self, error_type, error, traceback):
                del error_type, error, traceback
                return False

        previous = connection.settings_dict["ATOMIC_REQUESTS"]
        connection.settings_dict["ATOMIC_REQUESTS"] = True
        try:
            with mock.patch(
                "core.models.media_dedup_lock",
                return_value=RecordingStripe(),
            ):
                response = self.client.delete(
                    reverse("pin-detail", args=[pin.pk])
                )
        finally:
            connection.settings_dict["ATOMIC_REQUESTS"] = previous

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(observed, [(False, True)])
        self.assertFalse(Pin.objects.filter(pk=pin.pk).exists())
        self.assertFalse(Image.objects.filter(pk=image.pk).exists())

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

    def test_should_reject_pin_create_with_existed_image_id(self):
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
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Pin.objects.count(), 1)

    def test_image_by_id_post_does_not_build_any_import_service(self):
        image = create_image()
        with mock.patch.object(
            PinViewSet,
            "get_pin_import_service",
            side_effect=AssertionError("URL service must not be built"),
        ), mock.patch.object(
            PinViewSet,
            "get_local_pin_import_service",
            side_effect=AssertionError("local service must not be built"),
        ):
            response = self.client.post(
                reverse("pin-list"),
                {"image_by_id": image.pk, "description": "existing image"},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

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
        self.assertEqual(Pin.objects.count(), 0)
