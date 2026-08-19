from io import BytesIO
import struct
import zlib

from django.conf import settings
from django.test import SimpleTestCase
from PIL import Image as PILImage

from core.services.safe_url_fetch import (
    FetchLimits,
    SafeFetchError,
    SafeUrlFetcher,
)
from django_images.paths import FORMAT_EXTENSIONS


PUBLIC_IP = "8.8.8.8"


def make_image_bytes(image_format="PNG", size=(2, 3)):
    output = BytesIO()
    PILImage.new("RGB", size, "red").save(output, format=image_format)
    return output.getvalue()


def make_oversized_png_header(width, height):
    def chunk(chunk_type, data):
        checksum = zlib.crc32(chunk_type + data) & 0xffffffff
        return (
            struct.pack(">I", len(data))
            + chunk_type
            + data
            + struct.pack(">I", checksum)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", b"")
        + chunk(b"IEND", b"")
    )


class FakeResolver:
    def __init__(self, results):
        self.results = results
        self.calls = []

    def resolve(self, hostname, port):
        self.calls.append((hostname, port))
        result = self.results[hostname]
        if isinstance(result, Exception):
            raise result
        return result


class FakeResponse:
    def __init__(
        self,
        content=None,
        status_code=200,
        headers=None,
        peer_ip=PUBLIC_IP,
        location=None,
        chunks=None,
    ):
        if content is None:
            content = make_image_bytes()
        self.status_code = status_code
        self.headers = headers or {}
        self.peer_ip = peer_ip
        self.location = location
        self.is_redirect = status_code in (301, 302, 303, 307, 308)
        self.chunks = [content] if chunks is None else chunks
        self.closed = False

    def iter_content(self, chunk_size):
        del chunk_size
        for chunk in self.chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    def close(self):
        self.closed = True


class FakeTransport:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def request(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class SequenceClock:
    def __init__(self, values):
        self.values = iter(values)

    def __call__(self):
        return next(self.values)


class SafeUrlFetcherPolicyTests(SimpleTestCase):
    def make_fetcher(
        self,
        addresses=None,
        responses=None,
        limits=None,
        clock=None,
    ):
        resolver = FakeResolver(
            addresses or {"images.example": [PUBLIC_IP]}
        )
        transport = FakeTransport(responses or [FakeResponse()])
        kwargs = {
            "resolver": resolver,
            "transport": transport,
        }
        if limits is not None:
            kwargs["limits"] = limits
        if clock is not None:
            kwargs["clock"] = clock
        return SafeUrlFetcher(**kwargs), resolver, transport

    def assert_fetch_error(
        self, fetcher, url, code, retryable=False, **fetch_kwargs
    ):
        with self.assertRaises(SafeFetchError) as caught:
            fetcher.fetch(url, **fetch_kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(caught.exception.retryable, retryable)
        return caught.exception

    def test_default_limits_match_settings(self):
        limits = FetchLimits()

        self.assertEqual(limits.max_bytes, 25 * 1024 * 1024)
        self.assertEqual(limits.max_bytes, settings.PINRY_FETCH_MAX_BYTES)
        self.assertEqual(limits.connect_timeout, 3)
        self.assertEqual(limits.read_timeout, 8)
        self.assertEqual(limits.total_timeout, 12)
        self.assertEqual(limits.max_redirects, 3)
        self.assertEqual(limits.max_pixels, 100000000)
        self.assertEqual(settings.PINRY_FETCH_PRIVATE_ALLOWLIST, [])
        self.assertEqual(settings.PINRY_BATCH_DEADLINE_SECONDS, 45)
        self.assertEqual(settings.PINRY_BATCH_MAX_ITEMS, 10)
        self.assertEqual(settings.PINRY_BATCH_MAX_BODY_BYTES, 1024 * 1024)
        self.assertEqual(settings.PINRY_IMPORT_LEASE_SECONDS, 300)

    def test_rejects_blocked_ipv4_and_ipv6_before_transport(self):
        blocked_addresses = (
            "127.0.0.1",
            "10.0.0.7",
            "169.254.1.1",
            "224.0.0.1",
            "240.0.0.1",
            "0.0.0.0",
            "169.254.169.254",
            "100.64.0.1",
            "::1",
            "fc00::1",
            "fe80::1",
            "ff02::1",
            "100::1",
            "::",
            "fec0::1",
            "::ffff:10.0.0.7",
        )

        for address in blocked_addresses:
            with self.subTest(address=address):
                fetcher, resolver, transport = self.make_fetcher(
                    {"internal.example": [address]}
                )

                error = self.assert_fetch_error(
                    fetcher,
                    "https://internal.example/image.png",
                    "blocked_address",
                )

                self.assertFalse(error.retryable)
                self.assertEqual(
                    resolver.calls, [("internal.example", 443)]
                )
                self.assertEqual(transport.calls, [])

    def test_rejects_non_http_userinfo_and_malformed_urls(self):
        invalid_urls = (
            "file:///tmp/image.png",
            "ftp://images.example/image.png",
            "https://user:secret@images.example/image.png",
            "https://images.example/a b.png",
            "https://images.example/path\\image.png",
            "https://images.example/line\nbreak.png",
            "https://images.example:invalid/image.png",
            "https://images.example:70000/image.png",
            "https:///image.png",
        )

        for url in invalid_urls:
            with self.subTest(url=url):
                fetcher, resolver, transport = self.make_fetcher()

                self.assert_fetch_error(
                    fetcher, url, "invalid_url_policy"
                )

                self.assertEqual(resolver.calls, [])
                self.assertEqual(transport.calls, [])

    def test_normalizes_hostname_and_builds_origin_request_target(self):
        response = FakeResponse()
        fetcher, resolver, transport = self.make_fetcher(
            {"xn--bcher-kva.example": [PUBLIC_IP]}, [response]
        )

        fetched = fetcher.fetch(
            "https://BÜCHER.Example.:8443/path/image.png?size=2#ignored"
        )

        self.assertEqual(
            resolver.calls, [("xn--bcher-kva.example", 8443)]
        )
        target = transport.calls[0]["target"]
        self.assertEqual(target.scheme, "https")
        self.assertEqual(target.hostname, "xn--bcher-kva.example")
        self.assertEqual(target.port, 8443)
        self.assertEqual(target.ip_address, PUBLIC_IP)
        self.assertEqual(target.request_target, "/path/image.png?size=2")
        self.assertEqual(fetched.final_url, (
            "https://BÜCHER.Example.:8443/path/image.png?size=2#ignored"
        ))

    def test_rejects_ip_literal_unless_exactly_allowlisted(self):
        fetcher, resolver, transport = self.make_fetcher()

        self.assert_fetch_error(
            fetcher, "https://8.8.8.8/image.png", "blocked_address"
        )
        self.assertEqual(resolver.calls, [])
        self.assertEqual(transport.calls, [])

        limits = FetchLimits(private_allowlist=["10.0.0.7"])
        response = FakeResponse(peer_ip="10.0.0.7")
        fetcher, resolver, transport = self.make_fetcher(
            responses=[response], limits=limits
        )
        fetched = fetcher.fetch("https://10.0.0.7/image.png")

        self.assertEqual(fetched.image_format, "PNG")
        self.assertEqual(resolver.calls, [])
        self.assertEqual(
            transport.calls[0]["target"].ip_address, "10.0.0.7"
        )

    def test_exact_hostname_allowlist_bypasses_only_address_classification(self):
        limits = FetchLimits(private_allowlist=["internal.example"])
        response = FakeResponse(peer_ip="10.0.0.7")
        fetcher, resolver, transport = self.make_fetcher(
            {"internal.example": ["10.0.0.7", PUBLIC_IP]},
            [response],
            limits,
        )

        fetched = fetcher.fetch("https://internal.example/image.png")

        self.assertEqual(fetched.image_format, "PNG")
        self.assertEqual(
            transport.calls[0]["target"].ip_address, "10.0.0.7"
        )

        too_small = FetchLimits(
            max_bytes=1,
            private_allowlist=["internal.example"],
        )
        fetcher, resolver, transport = self.make_fetcher(
            {"internal.example": ["10.0.0.7"]},
            [FakeResponse(peer_ip="10.0.0.7")],
            too_small,
        )
        self.assert_fetch_error(
            fetcher,
            "https://internal.example/image.png",
            "image_too_large",
        )

    def test_allowlist_matching_is_exact_not_suffix_or_wildcard(self):
        for allowlist in (["example.com"], ["*.example.com"]):
            with self.subTest(allowlist=allowlist):
                limits = FetchLimits(private_allowlist=allowlist)
                fetcher, resolver, transport = self.make_fetcher(
                    {"sub.example.com": ["10.0.0.7"]},
                    limits=limits,
                )

                self.assert_fetch_error(
                    fetcher,
                    "https://sub.example.com/image.png",
                    "blocked_address",
                )
                self.assertEqual(transport.calls, [])

    def test_rejects_entire_mixed_dns_answer(self):
        fetcher, resolver, transport = self.make_fetcher(
            {"images.example": [PUBLIC_IP, "10.0.0.7"]}
        )

        self.assert_fetch_error(
            fetcher,
            "https://images.example/image.png",
            "blocked_address",
        )

        self.assertEqual(transport.calls, [])

    def test_ip_allowlist_does_not_bypass_mixed_hostname_answer(self):
        limits = FetchLimits(private_allowlist=["10.0.0.7"])
        fetcher, resolver, transport = self.make_fetcher(
            {"images.example": [PUBLIC_IP, "10.0.0.7"]},
            limits=limits,
        )

        self.assert_fetch_error(
            fetcher,
            "https://images.example/image.png",
            "blocked_address",
        )

        self.assertEqual(transport.calls, [])

    def test_redirects_are_resolved_again_and_fourth_is_rejected(self):
        responses = [
            FakeResponse(status_code=302, location="https://b.example/2"),
            FakeResponse(status_code=302, location="https://c.example/3"),
            FakeResponse(status_code=302, location="https://d.example/4"),
            FakeResponse(status_code=302, location="https://e.example/5"),
        ]
        fetcher, resolver, transport = self.make_fetcher(
            {
                "a.example": [PUBLIC_IP],
                "b.example": [PUBLIC_IP],
                "c.example": [PUBLIC_IP],
                "d.example": [PUBLIC_IP],
            },
            responses,
        )

        self.assert_fetch_error(
            fetcher,
            "https://a.example/1",
            "too_many_redirects",
        )

        self.assertEqual(
            [call[0] for call in resolver.calls],
            ["a.example", "b.example", "c.example", "d.example"],
        )
        self.assertEqual(len(transport.calls), 4)
        self.assertTrue(all(response.closed for response in responses))

    def test_redirect_to_private_address_is_blocked_before_second_request(self):
        redirect = FakeResponse(
            status_code=302,
            location="https://internal.example/secret.png",
        )
        fetcher, resolver, transport = self.make_fetcher(
            {
                "images.example": [PUBLIC_IP],
                "internal.example": ["10.0.0.7"],
            },
            [redirect],
        )

        self.assert_fetch_error(
            fetcher,
            "https://images.example/image.png",
            "blocked_address",
        )

        self.assertEqual(len(transport.calls), 1)
        self.assertTrue(redirect.closed)

    def test_rejects_peer_ip_that_differs_from_selected_address(self):
        response = FakeResponse(peer_ip="1.1.1.1")
        fetcher, resolver, transport = self.make_fetcher(
            responses=[response]
        )

        self.assert_fetch_error(
            fetcher,
            "https://images.example/image.png",
            "dns_rebinding_detected",
        )

        self.assertTrue(response.closed)

    def test_rejects_content_length_over_limit_without_streaming(self):
        response = FakeResponse(
            headers={"Content-Length": "5"},
            chunks=[AssertionError("body must not be read")],
        )
        limits = FetchLimits(max_bytes=4)
        fetcher, resolver, transport = self.make_fetcher(
            responses=[response], limits=limits
        )

        self.assert_fetch_error(
            fetcher,
            "https://images.example/image.png",
            "image_too_large",
        )

        self.assertTrue(response.closed)

    def test_rejects_stream_that_exceeds_limit_despite_short_header(self):
        response = FakeResponse(
            headers={"content-length": "2"},
            chunks=[b"ab", b"cde"],
        )
        limits = FetchLimits(max_bytes=4)
        fetcher, resolver, transport = self.make_fetcher(
            responses=[response], limits=limits
        )

        self.assert_fetch_error(
            fetcher,
            "https://images.example/image.png",
            "image_too_large",
        )

        self.assertTrue(response.closed)

    def test_rejects_non_identity_content_encoding_before_streaming(self):
        response = FakeResponse(
            headers={"Content-Encoding": "gzip"},
            chunks=[AssertionError("encoded body must not be read")],
        )
        fetcher, resolver, transport = self.make_fetcher(
            responses=[response]
        )

        self.assert_fetch_error(
            fetcher,
            "https://images.example/image.png",
            "unsupported_content_encoding",
        )

        self.assertTrue(response.closed)

    def test_allows_empty_and_identity_content_encoding(self):
        for content_encoding in ("", "identity", " Identity "):
            with self.subTest(content_encoding=content_encoding):
                response = FakeResponse(
                    headers={"content-encoding": content_encoding}
                )
                fetcher, resolver, transport = self.make_fetcher(
                    responses=[response]
                )

                fetched = fetcher.fetch(
                    "https://images.example/image.png"
                )

                self.assertEqual(fetched.image_format, "PNG")

    def test_http_error_retryability_is_stable(self):
        cases = (
            (408, True),
            (425, True),
            (429, True),
            (500, True),
            (503, True),
            (400, False),
            (404, False),
            (201, False),
        )

        for status_code, retryable in cases:
            with self.subTest(status_code=status_code):
                response = FakeResponse(status_code=status_code)
                fetcher, resolver, transport = self.make_fetcher(
                    responses=[response]
                )

                self.assert_fetch_error(
                    fetcher,
                    "https://images.example/image.png",
                    "image_download_failed",
                    retryable,
                )

                self.assertTrue(response.closed)

    def test_stream_failure_is_retryable_and_sanitized(self):
        secret = "Bearer internal-token-value"
        response = FakeResponse(chunks=[OSError(secret)])
        fetcher, resolver, transport = self.make_fetcher(
            responses=[response]
        )

        error = self.assert_fetch_error(
            fetcher,
            "https://images.example/image.png?token=url-secret",
            "image_download_failed",
            True,
        )

        serialized = repr(error.as_dict())
        self.assertNotIn("url-secret", serialized)
        self.assertNotIn("internal-token-value", serialized)
        self.assertTrue(response.closed)

    def test_policy_error_does_not_expose_url_userinfo(self):
        fetcher, resolver, transport = self.make_fetcher()

        error = self.assert_fetch_error(
            fetcher,
            "https://user:password@images.example/image.png?token=secret",
            "invalid_url_policy",
        )

        rendered = "{} {}".format(str(error), error.as_dict())
        self.assertNotIn("password", rendered)
        self.assertNotIn("token", rendered)

    def test_transport_network_error_does_not_expose_internal_exception(self):
        secret = "Bearer internal-token-value"
        fetcher, resolver, transport = self.make_fetcher(
            responses=[OSError(secret)]
        )

        error = self.assert_fetch_error(
            fetcher,
            "https://images.example/image.png?token=url-secret",
            "image_download_failed",
            True,
        )

        rendered = "{} {}".format(str(error), error.as_dict())
        self.assertNotIn(secret, rendered)
        self.assertNotIn("url-secret", rendered)

    def test_transport_timeout_maps_to_retryable_timeout(self):
        fetcher, resolver, transport = self.make_fetcher(
            responses=[TimeoutError("socket details")]
        )

        self.assert_fetch_error(
            fetcher,
            "https://images.example/image.png",
            "image_fetch_timeout",
            True,
        )

    def test_expired_deadline_stops_before_resolution(self):
        fetcher, resolver, transport = self.make_fetcher(
            clock=SequenceClock([2.0])
        )

        self.assert_fetch_error(
            fetcher,
            "https://images.example/image.png",
            "image_fetch_timeout",
            True,
            deadline=1.0,
        )

        self.assertEqual(resolver.calls, [])
        self.assertEqual(transport.calls, [])

    def test_deadline_is_rechecked_after_resolution(self):
        fetcher, resolver, transport = self.make_fetcher(
            clock=SequenceClock([0.0, 2.0])
        )

        self.assert_fetch_error(
            fetcher,
            "https://images.example/image.png",
            "image_fetch_timeout",
            True,
            deadline=1.0,
        )

        self.assertEqual(
            resolver.calls, [("images.example", 443)]
        )
        self.assertEqual(transport.calls, [])

    def test_invalid_referer_is_omitted_and_valid_referer_is_forwarded(self):
        invalid_referers = (
            "file:///tmp/private",
            "https://user:secret@example.com/",
            "https://example.com/\r\nX-Token: secret",
            "/relative/path",
        )
        for referer in invalid_referers:
            with self.subTest(referer=referer):
                fetcher, resolver, transport = self.make_fetcher()
                fetcher.fetch(
                    "https://images.example/image.png", referer=referer
                )
                self.assertIsNone(transport.calls[0]["referer"])

        fetcher, resolver, transport = self.make_fetcher()
        fetcher.fetch(
            "https://images.example/image.png",
            referer="https://page.example/gallery",
        )
        self.assertEqual(
            transport.calls[0]["referer"],
            "https://page.example/gallery",
        )

    def test_rejects_decodable_format_outside_storage_allowlist(self):
        ico_content = make_image_bytes("ICO", (32, 32))
        fetcher, resolver, transport = self.make_fetcher(
            responses=[FakeResponse(content=ico_content)]
        )

        self.assert_fetch_error(
            fetcher,
            "https://images.example/icon.ico",
            "unsupported_image_format",
        )

    def test_uses_storage_format_allowlist(self):
        ico_content = make_image_bytes("ICO", (32, 32))
        FORMAT_EXTENSIONS["ICO"] = ".ico"
        self.addCleanup(FORMAT_EXTENSIONS.pop, "ICO")
        fetcher, resolver, transport = self.make_fetcher(
            responses=[FakeResponse(content=ico_content)]
        )

        fetched = fetcher.fetch("https://images.example/icon.ico")

        self.assertEqual(fetched.image_format, "ICO")

    def test_rejects_invalid_image_content_without_internal_details(self):
        content = b"token=not-an-image"
        fetcher, resolver, transport = self.make_fetcher(
            responses=[FakeResponse(content=content)]
        )

        error = self.assert_fetch_error(
            fetcher,
            "https://images.example/image.png?token=url-secret",
            "invalid_image_content",
        )

        rendered = "{} {}".format(str(error), error.as_dict())
        self.assertNotIn("not-an-image", rendered)
        self.assertNotIn("url-secret", rendered)

    def test_rejects_pixels_over_configured_limit(self):
        original_max_pixels = PILImage.MAX_IMAGE_PIXELS
        limits = FetchLimits(max_pixels=5)
        fetcher, resolver, transport = self.make_fetcher(
            responses=[FakeResponse(content=make_image_bytes(size=(2, 3)))],
            limits=limits,
        )

        self.assert_fetch_error(
            fetcher,
            "https://images.example/image.png",
            "image_too_many_pixels",
        )

        self.assertEqual(PILImage.MAX_IMAGE_PIXELS, original_max_pixels)

    def test_decompression_bomb_warning_maps_to_pixel_error(self):
        original_max_pixels = PILImage.MAX_IMAGE_PIXELS
        content = make_oversized_png_header(9000, 10000)
        fetcher, resolver, transport = self.make_fetcher(
            responses=[FakeResponse(content=content)]
        )

        self.assert_fetch_error(
            fetcher,
            "https://images.example/image.png",
            "image_too_many_pixels",
        )

        self.assertEqual(PILImage.MAX_IMAGE_PIXELS, original_max_pixels)

    def test_success_returns_verified_image_metadata_and_closes_response(self):
        content = make_image_bytes("JPEG", (4, 5))
        response = FakeResponse(content=content)
        fetcher, resolver, transport = self.make_fetcher(
            responses=[response]
        )

        fetched = fetcher.fetch(
            "https://images.example/final.jpg?token=kept"
        )

        self.assertEqual(fetched.content, content)
        self.assertEqual(fetched.image_format, "JPEG")
        self.assertEqual((fetched.width, fetched.height), (4, 5))
        self.assertEqual(
            fetched.final_url,
            "https://images.example/final.jpg?token=kept",
        )
        self.assertTrue(response.closed)
