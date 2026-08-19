from io import BytesIO
import os
import unicodedata

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from PIL import Image as PILImage

from core.models import Image as CoreImage
from core.serializers import ImageSerializer
from django_images.models import Image, Thumbnail
from django_images.paths import canonical_extension, sanitize_original_filename
from django_images.test_helpers import TemporaryMediaMixin


def make_image_bytes(image_format):
    image = BytesIO()
    PILImage.new("RGB", (32, 32), "red").save(image, format=image_format)
    return image.getvalue()


class CanonicalExtensionTest(SimpleTestCase):
    def test_supported_formats_use_canonical_extensions(self):
        expected_extensions = {
            "JPEG": ".jpg",
            "PNG": ".png",
            "GIF": ".gif",
            "WEBP": ".webp",
            "BMP": ".bmp",
            "TIFF": ".tif",
        }

        for image_format, expected_extension in expected_extensions.items():
            with self.subTest(image_format=image_format):
                image = BytesIO(make_image_bytes(image_format))
                image.seek(3)

                self.assertEqual(
                    canonical_extension(image), expected_extension
                )
                self.assertEqual(image.tell(), 3)


class OriginalFilenameTest(SimpleTestCase):
    def test_filename_is_basename_without_controls_and_normalized_to_nfc(self):
        filename = "../../{}\x00.png".format(
            unicodedata.normalize("NFD", "사진")
        )

        self.assertEqual(sanitize_original_filename(filename), "사진.png")

    def test_filename_is_limited_to_255_characters(self):
        self.assertEqual(
            sanitize_original_filename("x" * 300),
            "x" * 255,
        )


class AssetStoragePathTest(TemporaryMediaMixin, TransactionTestCase):
    def setUp(self):
        super(AssetStoragePathTest, self).setUp()
        image = BytesIO()
        PILImage.new("RGB", (32, 32), "red").save(image, format="PNG")
        self.png_bytes = image.getvalue()

    def test_png_bytes_with_jpg_name_use_uuid_png_path(self):
        upload = SimpleUploadedFile("folder/photo.jpg", self.png_bytes)
        image = Image.objects.create(image=upload)
        Thumbnail.objects.get_or_create_at_sizes(
            image, ["thumbnail", "standard", "square"]
        )

        self.assertEqual(
            image.image.name,
            "originals/{}/original.png".format(image.asset_uuid),
        )
        self.assertEqual(
            {
                thumb.size: thumb.image.name
                for thumb in image.thumbnail_set.all()
            },
            {
                "thumbnail": "derivatives/{}/thumbnail.png".format(
                    image.asset_uuid
                ),
                "standard": "derivatives/{}/standard.png".format(
                    image.asset_uuid
                ),
                "square": "derivatives/{}/square.png".format(
                    image.asset_uuid
                ),
            },
        )

    def test_original_filename_is_sanitized_before_it_is_stored(self):
        filename = "../../{}\x00.png".format(
            unicodedata.normalize("NFD", "사진")
        )
        upload = SimpleUploadedFile(filename, self.png_bytes)

        image = Image.objects.create(image=upload)

        self.assertEqual(image.original_filename, "사진.png")

    def test_arbitrary_derivative_size_is_rejected_before_file_write(self):
        image = Image.objects.create(
            image=SimpleUploadedFile("source.png", self.png_bytes)
        )
        files_before = {
            os.path.relpath(os.path.join(root, filename), self.temporary_media.name)
            for root, _, filenames in os.walk(self.temporary_media.name)
            for filename in filenames
        }

        with self.assertRaisesRegex(
            ValueError, "Unsupported derivative size: arbitrary"
        ):
            Thumbnail.objects.create(
                original=image,
                size="arbitrary",
                image=SimpleUploadedFile("ignored.png", self.png_bytes),
            )

        files_after = {
            os.path.relpath(os.path.join(root, filename), self.temporary_media.name)
            for root, _, filenames in os.walk(self.temporary_media.name)
            for filename in filenames
        }
        self.assertEqual(files_after, files_before)
        self.assertFalse(image.thumbnail_set.filter(size="arbitrary").exists())


class ExistingStorageContractTest(TemporaryMediaMixin, TestCase):
    def setUp(self):
        super(ExistingStorageContractTest, self).setUp()
        self.image = CoreImage.objects.create(
            image="a/b/existing-hash/photo.jpg",
            original_filename="photo.jpg",
            width=32,
            height=32,
        )
        for size in ("thumbnail", "standard", "square"):
            Thumbnail.objects.create(
                original=self.image,
                size=size,
                image="a/b/existing-hash/{}.jpg".format(size),
                width=32,
                height=32,
            )

    def test_existing_hash_path_url_is_serialized_without_rewrite(self):
        data = ImageSerializer(self.image).data

        self.assertEqual(data["image"], self.image.image.url)
        self.assertEqual(
            self.image.image.name, "a/b/existing-hash/photo.jpg"
        )

    def test_image_serializer_keeps_original_and_derivative_fields(self):
        data = ImageSerializer(self.image).data

        self.assertTrue(
            {"image", "thumbnail", "standard", "square"}.issubset(data)
        )
        self.assertEqual(
            data["thumbnail"]["image"],
            self.image.thumbnail.image.url,
        )
        self.assertEqual(
            data["standard"]["image"],
            self.image.standard.image.url,
        )
        self.assertEqual(
            data["square"]["image"],
            self.image.square.image.url,
        )
