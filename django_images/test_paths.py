from io import BytesIO
import os
from pathlib import Path
import uuid
import unicodedata

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from PIL import Image as PILImage

from core.models import Image as CoreImage
from core.serializers import ImageSerializer
from django_images.models import Image, Thumbnail
from django_images.paths import (
    canonical_extension,
    canonical_original_path,
    is_valid_original_leaf,
    sanitize_original_filename,
)
from django_images.test_helpers import TemporaryMediaMixin


def make_image_bytes(image_format):
    image = BytesIO()
    PILImage.new("RGB", (32, 32), "red").save(image, format=image_format)
    return image.getvalue()


def media_snapshot(media_root):
    root = Path(media_root)
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


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

    def test_filename_uses_last_component_for_mixed_path_separators(self):
        filename = "C:\\fakepath/mixed\\{}\x00.png".format(
            unicodedata.normalize("NFD", "사진")
        )

        self.assertEqual(sanitize_original_filename(filename), "사진.png")


class CanonicalOriginalPathTest(SimpleTestCase):
    asset_uuid = uuid.UUID(
        "8dbc3cf8-0348-43ff-8e72-dc1c4774012a"
    )

    def test_uses_original_stem_and_actual_extension(self):
        self.assertEqual(
            canonical_original_path(
                self.asset_uuid, "folder/여행 사진.jpg", ".png"
            ),
            "originals/{}/여행 사진.png".format(self.asset_uuid),
        )

    def test_invalid_names_use_exact_uuid_fallback(self):
        invalid = (
            "", ".", "..", ".png", "CON.txt", "con.backup.jpg",
            "LPT9.backup.png",
        )
        for original_name in invalid:
            with self.subTest(original_name=original_name):
                self.assertEqual(
                    canonical_original_path(
                        self.asset_uuid, original_name, ".png"
                    ),
                    "originals/{}/image-8dbc3cf80348.png".format(
                        self.asset_uuid
                    ),
                )

    def test_portable_characters_and_unicode_are_normalized(self):
        original_name = unicodedata.normalize(
            "NFD", "사진<촬영>:2026?.jpg"
        )
        path = canonical_original_path(
            self.asset_uuid, original_name, ".png"
        )

        self.assertEqual(
            path,
            "originals/{}/사진_촬영__2026_.png".format(
                self.asset_uuid
            ),
        )
        self.assertEqual(path, unicodedata.normalize("NFC", path))

    def test_long_multibyte_name_fits_both_limits(self):
        path = canonical_original_path(
            self.asset_uuid,
            ("한글. " * 100) + "source.jpg",
            ".webp",
        )
        leaf = path.rsplit("/", 1)[-1]

        self.assertLessEqual(len(path), 255)
        self.assertLessEqual(len(leaf.encode("utf-8")), 255)
        self.assertTrue(leaf.endswith(".webp"))
        self.assertEqual(leaf[:-5], leaf[:-5].strip(" ."))
        self.assertTrue(is_valid_original_leaf(self.asset_uuid, leaf))

    def test_leaf_validator_rejects_noncanonical_names(self):
        invalid = (
            "photo.PNG", "CON.png", "name?.png", " name.png",
        )
        for leaf in invalid:
            with self.subTest(leaf=leaf):
                self.assertFalse(
                    is_valid_original_leaf(self.asset_uuid, leaf)
                )

        self.assertTrue(
            is_valid_original_leaf(self.asset_uuid, "original.png")
        )
        self.assertTrue(
            is_valid_original_leaf(self.asset_uuid, "사진.png")
        )
        self.assertTrue(is_valid_original_leaf(
            self.asset_uuid,
            unicodedata.normalize("NFD", "사진.png"),
        ))


class AssetStoragePathTest(TemporaryMediaMixin, TransactionTestCase):
    def setUp(self):
        super(AssetStoragePathTest, self).setUp()
        image = BytesIO()
        PILImage.new("RGB", (640, 480), "red").save(image, format="PNG")
        self.png_bytes = image.getvalue()

    def test_png_bytes_with_jpg_name_use_uuid_png_path(self):
        upload = SimpleUploadedFile("folder/photo.jpg", self.png_bytes)
        image = Image.objects.create(image=upload)
        Thumbnail.objects.get_or_create_at_sizes(
            image, ["thumbnail", "standard", "square"]
        )
        image.refresh_from_db()

        original_path = "originals/{}/original.png".format(
            image.asset_uuid
        )
        derivative_paths = {
            "thumbnail": "derivatives/{}/thumbnail.png".format(
                image.asset_uuid
            ),
            "standard": "derivatives/{}/standard.png".format(
                image.asset_uuid
            ),
            "square": "derivatives/{}/square.png".format(
                image.asset_uuid
            ),
        }

        self.assertEqual(image.image.name, original_path)
        self.assertEqual(image.original_filename, "photo.jpg")
        self.assertEqual(
            {
                thumb.size: thumb.image.name
                for thumb in image.thumbnail_set.all()
            },
            derivative_paths,
        )
        stored_files = media_snapshot(self.temporary_media.name)
        self.assertEqual(
            set(stored_files),
            {original_path, *derivative_paths.values()},
        )
        self.assertEqual(stored_files[original_path], self.png_bytes)

        expected_sizes = {
            "thumbnail": (240, 180),
            "standard": (600, 450),
            "square": (125, 125),
        }
        for size, path in derivative_paths.items():
            with self.subTest(size=size):
                with PILImage.open(BytesIO(stored_files[path])) as derivative:
                    derivative.load()
                    self.assertEqual(derivative.format, "PNG")
                    self.assertEqual(derivative.size, expected_sizes[size])

    def test_original_filename_is_sanitized_before_it_is_stored(self):
        filename = "../../{}\x00.png".format(
            unicodedata.normalize("NFD", "사진")
        )
        upload = SimpleUploadedFile(filename, self.png_bytes)

        image = Image.objects.create(image=upload)
        image.refresh_from_db()

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
