from io import BytesIO
import os
from pathlib import Path

import mock
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase
from django.urls import reverse
from PIL import Image as PILImage
from rest_framework import status
from rest_framework.test import APITransactionTestCase

from core.models import Image, Pin
from core.services.safe_url_fetch import SafeFetchError
from core.tests.helpers import create_user
from core.views import PinViewSet
from django_images.test_helpers import TemporaryMediaMixin


def make_ico_bytes():
    image = BytesIO()
    PILImage.new("RGB", (32, 32), "red").save(image, format="ICO")
    return image.getvalue()


def media_snapshot(media_root):
    root = Path(media_root)
    if not root.exists():
        return {}
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


class UnsupportedUrlImportService(object):
    def prepare_url(self, url, referer, deadline):
        del url, referer, deadline
        raise SafeFetchError(
            "unsupported_image_format", "unsupported", False
        )

    def close(self):
        pass


class TemporaryMediaMixinTest(SimpleTestCase):
    def test_cleanup_removes_temporary_media_root(self):
        class TemporaryMediaCase(TemporaryMediaMixin, SimpleTestCase):
            pass

        case = TemporaryMediaCase()
        case.setUp()
        media_root = case.temporary_media.name
        self.assertTrue(os.path.isdir(media_root))

        case.doCleanups()

        self.assertFalse(os.path.exists(media_root))

    def test_media_writes_leave_repository_media_tree_unchanged(self):
        repository_media_root = settings.MEDIA_ROOT
        files_before = media_snapshot(repository_media_root)

        class TemporaryMediaCase(TemporaryMediaMixin, SimpleTestCase):
            pass

        case = TemporaryMediaCase()
        case.setUp()
        temporary_media_root = case.temporary_media.name
        default_storage.save("probe.txt", ContentFile(b"probe"))
        self.assertEqual(
            Path(temporary_media_root, "probe.txt").read_bytes(), b"probe"
        )
        case.doCleanups()

        self.assertEqual(
            media_snapshot(repository_media_root), files_before
        )


class UnsupportedImageFormatAPITest(
    TemporaryMediaMixin, APITransactionTestCase
):
    def setUp(self):
        super(UnsupportedImageFormatAPITest, self).setUp()
        self.user = create_user("unsupported-format")
        self.client.login(username=self.user.username, password="password")
        self.ico_bytes = make_ico_bytes()

    def test_image_upload_returns_405_without_row_or_file(self):
        response = self.client.post(
            reverse("image-list"),
            {
                "image": SimpleUploadedFile(
                    "icon.ico", self.ico_bytes, content_type="image/x-icon"
                )
            },
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
        self.assertFalse(Image.objects.exists())
        self.assertEqual(media_snapshot(self.temporary_media.name), {})

    def test_url_pin_returns_400_without_pin_image_or_file(self):
        with mock.patch.object(
            PinViewSet,
            "get_pin_import_service",
            return_value=UnsupportedUrlImportService(),
        ):
            response = self.client.post(
                reverse("pin-list"),
                {
                    "url": "https://example.com/icon.ico",
                    "referer": "https://example.com/",
                    "description": "unsupported icon",
                },
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.json(), {"url": ["unsupported_image_format"]}
        )
        self.assertFalse(Pin.objects.exists())
        self.assertFalse(Image.objects.exists())
        self.assertEqual(media_snapshot(self.temporary_media.name), {})
