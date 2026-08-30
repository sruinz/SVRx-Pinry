import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID
from xml.etree import ElementTree

from django.test import SimpleTestCase

from exports.services.metadata import (
    PortableNameAllocator, archive_display_name, format_utc, redact_url,
    render_xmp, zip_datetime,
)


RDF_NAMESPACE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
EXIF_NAMESPACE = "http://ns.adobe.com/exif/1.0/"
PHOTOSHOP_NAMESPACE = "http://ns.adobe.com/photoshop/1.0/"
DC_NAMESPACE = "http://purl.org/dc/elements/1.1/"
DIGIKAM_NAMESPACE = "http://www.digikam.org/ns/1.0/"


class _LogCapture(logging.Handler):
    def __init__(self):
        logging.Handler.__init__(self)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


class UrlMetadataTests(SimpleTestCase):
    def test_redact_url_removes_credentials_fragment_and_sensitive_decoded_keys(self):
        value = (
            "https://user:pw@example.test/a?keep=1&Tag=two&tag=&keep=1&"
            "%54oKeN=secret&flag=#fragment"
        )

        self.assertEqual(
            redact_url(value),
            ("https://example.test/a?keep=1&Tag=two&tag=&keep=1&flag=", True),
        )

    def test_redact_url_preserves_non_sensitive_duplicate_blank_query_values(self):
        self.assertEqual(
            redact_url("https://example.test/a?a=1&a=&b=two%20words"),
            ("https://example.test/a?a=1&a=&b=two+words", False),
        )

    def test_redact_url_fails_closed_without_logging_or_raising_secret(self):
        secret = "do-not-log-this-secret"
        capture = _LogCapture()
        logger = logging.getLogger()
        logger.addHandler(capture)
        try:
            values = (
                "ftp://example.test/a?token=" + secret,
                "https://[broken/a?token=" + secret,
                "https://example.test:bad/a?token=" + secret,
                "/relative/path?token=" + secret,
                None,
            )
            for value in values:
                self.assertEqual(redact_url(value), (None, True))
        finally:
            logger.removeHandler(capture)

        self.assertFalse(any(secret in message for message in capture.messages))


class TimeMetadataTests(SimpleTestCase):
    def test_format_utc_converts_timezone_and_keeps_microseconds(self):
        value = datetime(2026, 8, 1, 11, 3, 4, 123456, tzinfo=timezone(timedelta(hours=9)))

        self.assertEqual(format_utc(value), "2026-08-01T02:03:04.123456Z")

    def test_zip_datetime_removes_microseconds_clamps_and_rounds_down_to_even_seconds(self):
        self.assertEqual(
            zip_datetime(datetime(1970, 1, 1, tzinfo=timezone.utc)),
            (1980, 1, 1, 0, 0, 0),
        )
        self.assertEqual(
            zip_datetime(datetime(2108, 1, 1, tzinfo=timezone.utc)),
            (2107, 12, 31, 23, 59, 58),
        )
        self.assertEqual(
            zip_datetime(datetime(2026, 8, 1, 2, 3, 5, 999999, tzinfo=timezone.utc)),
            (2026, 8, 1, 2, 3, 4),
        )


class PortableNameTests(SimpleTestCase):
    def test_allocator_preserves_only_case_variant_of_the_canonical_extension(self):
        allocator = PortableNameAllocator()
        item_uuid = UUID("a13f9c2d-0000-4000-8000-000000000001")

        self.assertEqual(
            allocator.reserve(item_uuid, 1, "image.JPG", "image/jpeg"),
            ("originals/image.JPG", "originals/image.JPG.xmp"),
        )

    def test_allocator_uses_format_extensions_for_every_supported_mime_type(self):
        fixtures = (
            ("image/jpeg", "JPEG", ".jpg"),
            ("image/png", "PNG", ".png"),
            ("image/gif", "GIF", ".gif"),
            ("image/webp", "WEBP", ".webp"),
            ("image/bmp", "BMP", ".bmp"),
            ("image/tiff", "TIFF", ".tif"),
        )

        for index, (mime_type, _format_name, extension) in enumerate(fixtures, 1):
            allocator = PortableNameAllocator()
            image_path, xmp_path = allocator.reserve(
                UUID("00000000-0000-4000-8000-{:012d}".format(index)),
                index,
                "photo.exe",
                mime_type,
            )
            self.assertEqual(image_path, "originals/photo{}".format(extension))
            self.assertEqual(xmp_path, "originals/photo{}.xmp".format(extension))

    def test_allocator_normalizes_casefold_collisions_and_reserves_image_xmp_pair(self):
        allocator = PortableNameAllocator()
        first = allocator.reserve(
            UUID("a13f9c2d-0000-4000-8000-000000000001"),
            1,
            "image.jpg",
            "image/jpeg",
        )
        second = allocator.reserve(
            UUID("b24e0d3e-0000-4000-8000-000000000002"),
            2,
            "IMAGE.JPG",
            "image/jpeg",
        )

        self.assertEqual(first, ("originals/image.jpg", "originals/image.jpg.xmp"))
        self.assertEqual(
            second,
            ("originals/IMAGE__b24e0d3e.JPG", "originals/IMAGE__b24e0d3e.JPG.xmp"),
        )

    def test_allocator_normalizes_nfc_before_casefold_collision_check(self):
        allocator = PortableNameAllocator()

        first = allocator.reserve(
            UUID("a13f9c2d-0000-4000-8000-000000000001"),
            1,
            "caf\u00e9.jpg",
            "image/jpeg",
        )
        second = allocator.reserve(
            UUID("b24e0d3e-0000-4000-8000-000000000002"),
            2,
            "cafe\u0301.JPG",
            "image/jpeg",
        )

        self.assertEqual(first, ("originals/caf\u00e9.jpg", "originals/caf\u00e9.jpg.xmp"))
        self.assertEqual(
            second,
            ("originals/caf\u00e9__b24e0d3e.JPG", "originals/caf\u00e9__b24e0d3e.JPG.xmp"),
        )

    def test_allocator_extends_uuid_suffix_and_truncates_korean_stem_at_utf8_boundary(self):
        allocator = PortableNameAllocator()
        first_uuid = UUID("11111111-0000-4000-8000-000000000001")
        second_uuid = UUID("11111111-0000-4000-8000-000000000002")
        third_uuid = UUID("11111111-0000-4000-8000-000000000003")
        allocator.reserve(first_uuid, 1, "same.jpg", "image/jpeg")
        second_image_path, _second_xmp_path = allocator.reserve(
            second_uuid, 2, "same.jpg", "image/jpeg",
        )
        third_image_path, _third_xmp_path = allocator.reserve(
            third_uuid, 3, "same.jpg", "image/jpeg",
        )
        image_path, xmp_path = allocator.reserve(
            UUID("11111111-0000-4000-8000-000000000004"),
            4,
            "가" * 100 + ".jpg",
            "image/jpeg",
        )

        self.assertEqual(second_image_path, "originals/same__11111111.jpg")
        self.assertEqual(third_image_path, "originals/same__111111110.jpg")
        self.assertLessEqual(len(image_path.encode("utf-8")), 240)
        self.assertLessEqual(len(xmp_path.encode("utf-8")), 240)
        self.assertTrue(image_path.endswith(".jpg"))
        self.assertTrue(xmp_path.endswith(".jpg.xmp"))
        self.assertNotIn("\ufffd", image_path)

    def test_allocator_uses_pin_fallback_for_reserved_or_empty_stems(self):
        allocator = PortableNameAllocator()

        self.assertEqual(
            allocator.reserve(
                UUID("a13f9c2d-0000-4000-8000-000000000001"),
                42,
                "CON\n.exe",
                "image/jpeg",
            ),
            ("originals/pin-42.jpg", "originals/pin-42.jpg.xmp"),
        )


class ArchiveDisplayNameTests(SimpleTestCase):
    def test_archive_display_name_uses_scope_and_utc_suffix(self):
        completed_at = datetime(2026, 8, 1, 11, 3, 4, tzinfo=timezone(timedelta(hours=9)))

        self.assertEqual(
            archive_display_name("board", "여행", completed_at),
            "여행-내보내기-20260801T020304Z.zip",
        )
        self.assertEqual(
            archive_display_name("pins", "ignored", completed_at),
            "선택-Pin-내보내기-20260801T020304Z.zip",
        )

    def test_archive_display_name_sanitizes_reserved_control_and_long_board_names(self):
        completed_at = datetime(2026, 8, 1, 2, 3, 4, tzinfo=timezone.utc)
        suffix = "-내보내기-20260801T020304Z.zip"

        self.assertEqual(
            archive_display_name("board", "CON\r\n", completed_at),
            "board" + suffix,
        )
        display_name = archive_display_name("board", "가" * 100, completed_at)
        self.assertLessEqual(len(display_name.encode("utf-8")), 180)
        self.assertTrue(display_name.endswith(suffix))

    def test_archive_display_name_does_not_duplicate_its_utc_suffix(self):
        completed_at = datetime(2026, 8, 1, 2, 3, 4, tzinfo=timezone.utc)

        self.assertEqual(
            archive_display_name("board", "여행-내보내기-20260801T020304Z", completed_at),
            "여행-내보내기-20260801T020304Z.zip",
        )


class XmpMetadataTests(SimpleTestCase):
    def test_render_xmp_emits_standard_namespaces_safe_description_and_sorted_tags(self):
        published_at = datetime(2026, 8, 1, 11, 3, 4, 123456, tzinfo=timezone(timedelta(hours=9)))
        description = "줄\t바꿈\n제어\x01한글😀"
        tags = ["zebra", "가", "apple\x01"]

        document = render_xmp(published_at, description, tags)
        root = ElementTree.fromstring(document)
        description_node = root.find(".//{{{}}}Description".format(RDF_NAMESPACE))

        self.assertTrue(document.startswith(b"<?xml"))
        self.assertIn(b"<x:xmpmeta", document)
        self.assertEqual(root.tag, "{adobe:ns:meta/}xmpmeta")
        self.assertEqual(description, "줄\t바꿈\n제어\x01한글😀")
        self.assertEqual(description_node.attrib["{{{}}}about".format(RDF_NAMESPACE)], "")
        self.assertEqual(
            description_node.attrib["{{{}}}DateTimeOriginal".format(EXIF_NAMESPACE)],
            "2026-08-01T02:03:04.123456Z",
        )
        self.assertEqual(
            description_node.attrib["{{{}}}DateCreated".format(PHOTOSHOP_NAMESPACE)],
            "2026-08-01T02:03:04.123456Z",
        )
        description_text = description_node.find(
            "{{{}}}description/{{{}}}Alt/{{{}}}li".format(
                DC_NAMESPACE, RDF_NAMESPACE, RDF_NAMESPACE,
            )
        )
        self.assertEqual(description_text.attrib["{http://www.w3.org/XML/1998/namespace}lang"], "x-default")
        self.assertEqual(description_text.text, "줄\t바꿈\n제어\ufffd한글😀")
        self.assertEqual(
            [node.text for node in description_node.findall(
                "{{{}}}TagsList/{{{}}}Seq/{{{}}}li".format(
                    DIGIKAM_NAMESPACE, RDF_NAMESPACE, RDF_NAMESPACE,
                )
            )],
            ["apple\ufffd", "zebra", "가"],
        )
