import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import UUID
from xml.etree import ElementTree

from django.test import SimpleTestCase

from django_images.paths import FORMAT_EXTENSIONS
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

    def test_redact_url_fails_closed_for_malformed_authority_and_retained_query(self):
        secret = "do-not-expose-this-secret"
        capture = _LogCapture()
        logger = logging.getLogger()
        logger.addHandler(capture)
        try:
            values = (
                "https://example.test:/a?token=" + secret,
                "https://exa mple.test/a?token=" + secret,
                "https://exa\x01mple.test/a?token=" + secret,
                "https://exa%ZZmple.test/a?token=" + secret,
                "https://example.test/a?keep=%ZZ&token=" + secret,
                "https://example.test/a?keep=\ud800{}&token=hidden".format(secret),
                "https://[broken/a?token=" + secret,
                "https://example.test:65536/a?token=" + secret,
            )
            for value in values:
                self.assertEqual(redact_url(value), (None, True))
        finally:
            logger.removeHandler(capture)

        self.assertFalse(any(secret in message for message in capture.messages))

    def test_redact_url_rejects_hostname_characters_outside_reg_name_or_ip_literal(self):
        for value in (
                "https://exa<mple.test/a?keep=1",
                "https://exa|mple.test/a?keep=1",
                "https://exa\\mple.test/a?keep=1"):
            self.assertEqual(redact_url(value), (None, True))

    def test_redact_url_rejects_unencodable_or_control_path_characters(self):
        self.assertEqual(
            redact_url("https://example.test/a\ud800?token=hidden"),
            (None, True),
        )
        self.assertEqual(
            redact_url("https://example.test/a\x01?token=hidden"),
            (None, True),
        )

    def test_redact_url_preserves_valid_percent_encoding_while_removing_credentials(self):
        self.assertEqual(
            redact_url("https://example.test/a%20b?keep=%E2%9C%93&blank=&keep=%2525"),
            ("https://example.test/a%20b?keep=%E2%9C%93&blank=&keep=%2525", False),
        )
        self.assertEqual(
            redact_url("https://user:p%40ss@example.test/a?keep=%2F&%54oken=secret"),
            ("https://example.test/a?keep=%2F", True),
        )

    def test_redact_url_rejects_raw_c0_and_surrogate_before_urlsplit(self):
        templates = (
            "https://exa{}mple.test/a?keep=1",
            "https://example.test/a{}b?keep=1",
            "https://example.test/a?keep=a{}b",
        )

        capture = _LogCapture()
        logger = logging.getLogger()
        logger.addHandler(capture)
        try:
            for character in ("\t", "\n", "\r", "\x00", "\ud800"):
                for template in templates:
                    self.assertEqual(
                        redact_url(template.format(character)),
                        (None, True),
                    )
        finally:
            logger.removeHandler(capture)

        self.assertEqual(capture.messages, [])

    def test_redact_url_preserves_percent_encoded_controls_as_url_data(self):
        self.assertEqual(
            redact_url("https://example.test/a%09b?tab=%09&lf=%0A&cr=%0D"),
            ("https://example.test/a%09b?tab=%09&lf=%0A&cr=%0D", False),
        )

    def test_redact_url_allows_rfc_reg_name_and_ip_literal_variants(self):
        fixtures = (
            "https://%65xample.test/a",
            "https://b%C3%BCcher.example/a",
            "https://bücher.example/a",
            "https://127.0.0.1/a",
            "https://[2001:db8::1]/a",
            "https://[fe80::1%25eth0]/a",
            "https://[v1.fe]/a",
        )

        for value in fixtures:
            self.assertEqual(redact_url(value), (value, False))

    def test_redact_url_rejects_malformed_ip_literal_variants(self):
        for value in (
                "https://[v1.]/a",
                "https://[vG.fe]/a",
                "https://[fe80::1%eth0]/a",
                "https://[2001:db8:::1]/a"):
            self.assertEqual(redact_url(value), (None, True))


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
            ("image/jpeg", "JPEG"),
            ("image/png", "PNG"),
            ("image/gif", "GIF"),
            ("image/webp", "WEBP"),
            ("image/bmp", "BMP"),
            ("image/tiff", "TIFF"),
        )

        for index, (mime_type, format_name) in enumerate(fixtures, 1):
            allocator = PortableNameAllocator()
            image_path, xmp_path = allocator.reserve(
                UUID("00000000-0000-4000-8000-{:012d}".format(index)),
                index,
                "photo.exe",
                mime_type,
            )
            extension = FORMAT_EXTENSIONS[format_name]
            self.assertEqual(image_path, "originals/photo{}".format(extension))
            self.assertEqual(xmp_path, "originals/photo{}.xmp".format(extension))

    def test_allocator_follows_a_runtime_format_extension_change(self):
        with patch.dict(FORMAT_EXTENSIONS, {"JPEG": ".jpeg-test"}):
            image_path, xmp_path = PortableNameAllocator().reserve(
                UUID("a13f9c2d-0000-4000-8000-000000000001"),
                1,
                "photo.exe",
                "image/jpeg",
            )

        self.assertEqual(image_path, "originals/photo.jpeg-test")
        self.assertEqual(xmp_path, "originals/photo.jpeg-test.xmp")

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

    def test_allocator_reserves_sidecar_path_against_later_image_path(self):
        allocator = PortableNameAllocator()
        allocator.reserve(
            UUID("a13f9c2d-0000-4000-8000-000000000001"),
            1,
            "same.jpg",
            "image/jpeg",
        )

        with patch.dict(FORMAT_EXTENSIONS, {"PNG": ".xmp"}):
            image_path, xmp_path = allocator.reserve(
                UUID("b24e0d3e-0000-4000-8000-000000000002"),
                2,
                "same.jpg.xmp",
                "image/png",
            )

        self.assertEqual(image_path, "originals/same.jpg__b24e0d3e.xmp")
        self.assertEqual(xmp_path, "originals/same.jpg__b24e0d3e.xmp.xmp")

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

    def test_archive_display_name_does_not_duplicate_its_utc_suffix_without_zip(self):
        completed_at = datetime(2026, 8, 1, 2, 3, 4, tzinfo=timezone.utc)

        self.assertEqual(
            archive_display_name("board", "여행-내보내기-20260801T020304Z", completed_at),
            "여행-내보내기-20260801T020304Z.zip",
        )

    def test_archive_display_name_does_not_duplicate_its_complete_utc_zip_suffix(self):
        completed_at = datetime(2026, 8, 1, 2, 3, 4, tzinfo=timezone.utc)

        self.assertEqual(
            archive_display_name("board", "여행-내보내기-20260801T020304Z.zip", completed_at),
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

    def test_render_xmp_round_trips_allowed_whitespace_and_replaces_invalid_text(self):
        published_at = datetime(2026, 8, 1, 2, 3, 4, tzinfo=timezone.utc)
        description = "description\tline\ncarriage\rcontrol\x01surrogate\ud800"
        tag = "tag\tline\ncarriage\rcontrol\x01surrogate\ud800"

        document = render_xmp(published_at, description, [tag])
        description_node = ElementTree.fromstring(document).find(
            ".//{{{}}}Description".format(RDF_NAMESPACE)
        )
        description_text = description_node.find(
            "{{{}}}description/{{{}}}Alt/{{{}}}li".format(
                DC_NAMESPACE, RDF_NAMESPACE, RDF_NAMESPACE,
            )
        )
        tag_text = description_node.find(
            "{{{}}}TagsList/{{{}}}Seq/{{{}}}li".format(
                DIGIKAM_NAMESPACE, RDF_NAMESPACE, RDF_NAMESPACE,
            )
        )
        expected_description = "description\tline\ncarriage\rcontrol\ufffdsurrogate\ufffd"
        expected_tag = "tag\tline\ncarriage\rcontrol\ufffdsurrogate\ufffd"

        self.assertIn(b"&#13;", document)
        self.assertEqual(description_text.text, expected_description)
        self.assertEqual(tag_text.text, expected_tag)
