from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image as PILImage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from core.models import Image
from core.serializers import ImageSerializer
from django_images.models import Thumbnail
from django_images.test_helpers import TemporaryMediaMixin


def image_upload(image_format, animated=False, filename=None):
    output = BytesIO()
    first = PILImage.new("RGB", (16, 16), "red")
    options = {}
    if animated:
        options = {
            "save_all": True,
            "append_images": [PILImage.new("RGB", (16, 16), "blue")],
            "duration": 100,
            "loop": 0,
        }
    first.save(output, format=image_format, **options)
    return SimpleUploadedFile(
        filename or "example." + image_format.lower(), output.getvalue()
    )


class AnimationMetadataTests(TemporaryMediaMixin, TestCase):
    def create_image(self, image_format, animated=False, filename=None):
        image = Image.objects.create(
            image=image_upload(image_format, animated, filename)
        )
        for size in ("standard", "thumbnail", "square"):
            Thumbnail.objects.create(
                original=image, size=size, image="thumb.jpg", width=16, height=16
            )
        return image

    def test_only_multiframe_gif_and_webp_get_animation_badges(self):
        for image_format, animated, expected in (
            ("GIF", False, None), ("GIF", True, "GIF"),
            ("WEBP", False, None), ("WEBP", True, "WEBP"),
            ("PNG", False, None), ("TIFF", True, None),
        ):
            with self.subTest(image_format=image_format, animated=animated):
                image = self.create_image(image_format, animated)
                data = ImageSerializer(image).data
                self.assertIn("animation_format", data)
                self.assertEqual(data["animation_format"], expected)
                image.refresh_from_db()
                self.assertIsNotNone(image.animation_status)

    def test_new_upload_is_classified_before_any_list_request(self):
        image = self.create_image("WEBP", True, "misleading.gif")
        image.refresh_from_db()
        self.assertEqual(getattr(image, "animation_status", None), "webp")

    def test_published_import_path_is_classified_on_registration(self):
        for image_format, expected in (("GIF", "gif"), ("WEBP", "webp")):
            with self.subTest(image_format=image_format):
                original = self.create_image(image_format, True)
                imported = Image.objects.create(
                    image=original.image.name, width=16, height=16,
                    original_filename="imported." + image_format.lower(),
                )
                imported.refresh_from_db()
                self.assertEqual(imported.animation_status, expected)

    def test_shared_original_uses_result_saved_by_other_pin(self):
        image = self.create_image("GIF", True)
        Image.objects.filter(pk=image.pk).update(animation_status=None)
        first = Image.objects.get(pk=image.pk)
        second = Image.objects.get(pk=image.pk)
        self.assertEqual(ImageSerializer(first).data["animation_format"], "GIF")
        with patch.object(second.image.storage, "open", side_effect=AssertionError("재검사")):
            self.assertEqual(ImageSerializer(second).data["animation_format"], "GIF")

    def test_corrupt_legacy_file_does_not_break_list(self):
        image = self.create_image("GIF", True)
        Image.objects.filter(pk=image.pk).update(animation_status=None)
        Path(image.image.path).write_bytes(b"not an image")
        image.refresh_from_db(fields=["animation_status"])
        self.assertIsNone(ImageSerializer(image).data["animation_format"])
        image.refresh_from_db(fields=["animation_status"])
        self.assertEqual(image.animation_status, "unreadable")

    def test_existing_image_is_checked_once_without_changing_files_or_thumbnails(self):
        for image_format, animated, expected in (
            ("GIF", True, "GIF"), ("GIF", False, None),
            ("WEBP", True, "WEBP"), ("WEBP", False, None),
        ):
            with self.subTest(image_format=image_format, animated=animated):
                image = self.create_image(image_format, animated)
                Image.objects.filter(pk=image.pk).update(animation_status=None)
                image.refresh_from_db()
                original = Path(image.image.path)
                contents, modified = original.read_bytes(), original.stat().st_mtime_ns
                thumbnails = list(image.thumbnail_set.values_list("pk", flat=True))
                self.assertEqual(ImageSerializer(image).data["animation_format"], expected)
                image.refresh_from_db()
                with patch.object(image.image.storage, "open", side_effect=AssertionError("재검사")):
                    self.assertEqual(ImageSerializer(image).data["animation_format"], expected)
                self.assertEqual(original.read_bytes(), contents)
                self.assertEqual(original.stat().st_mtime_ns, modified)
                self.assertEqual(list(image.thumbnail_set.values_list("pk", flat=True)), thumbnails)

    def test_missing_legacy_file_does_not_break_list_or_retry_each_time(self):
        image = self.create_image("GIF", True)
        Image.objects.filter(pk=image.pk).update(animation_status=None)
        Path(image.image.path).unlink()
        image.refresh_from_db(fields=["animation_status"])
        self.assertIsNone(ImageSerializer(image).data["animation_format"])
        image.refresh_from_db(fields=["animation_status"])
        with patch.object(image.image.storage, "open", side_effect=AssertionError("재검사")):
            self.assertIsNone(ImageSerializer(image).data["animation_format"])

    def test_replacing_upload_updates_classification(self):
        image = self.create_image("GIF", True)
        image.image = image_upload("WEBP", False)
        image.save()
        image.refresh_from_db()
        self.assertEqual(getattr(image, "animation_status", None), "static")

    def test_non_candidate_legacy_image_is_not_opened(self):
        image = self.create_image("PNG")
        Image.objects.filter(pk=image.pk).update(animation_status=None)
        image.refresh_from_db()
        with patch.object(image.image.storage, "open", side_effect=AssertionError("불필요한 검사")):
            self.assertIsNone(ImageSerializer(image).data["animation_format"])
