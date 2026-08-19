from collections import namedtuple
from io import BytesIO
import ipaddress
from time import monotonic
import unicodedata
from urllib.parse import urljoin, urlsplit
import warnings

from django.conf import settings
from PIL import Image as PILImage
import requests

from django_images.paths import FORMAT_EXTENSIONS


ResolvedTarget = namedtuple(
    "ResolvedTarget",
    ("scheme", "hostname", "port", "ip_address", "request_target"),
)
FetchedImage = namedtuple(
    "FetchedImage",
    ("content", "image_format", "width", "height", "final_url"),
)


class FetchLimits:
    def __init__(
        self,
        max_bytes=None,
        connect_timeout=None,
        read_timeout=None,
        total_timeout=None,
        max_redirects=None,
        max_pixels=None,
        private_allowlist=None,
    ):
        self.max_bytes = self._setting(
            max_bytes, "PINRY_FETCH_MAX_BYTES"
        )
        self.connect_timeout = self._setting(
            connect_timeout, "PINRY_FETCH_CONNECT_TIMEOUT"
        )
        self.read_timeout = self._setting(
            read_timeout, "PINRY_FETCH_READ_TIMEOUT"
        )
        self.total_timeout = self._setting(
            total_timeout, "PINRY_FETCH_TOTAL_TIMEOUT"
        )
        self.max_redirects = self._setting(
            max_redirects, "PINRY_FETCH_MAX_REDIRECTS"
        )
        self.max_pixels = self._setting(
            max_pixels, "PINRY_FETCH_MAX_PIXELS"
        )
        self.private_allowlist = self._setting(
            private_allowlist, "PINRY_FETCH_PRIVATE_ALLOWLIST"
        )

    @staticmethod
    def _setting(value, name):
        if value is not None:
            return value
        return getattr(settings, name)


class SafeFetchError(Exception):
    def __init__(self, code, message, retryable):
        super(SafeFetchError, self).__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable

    def as_dict(self):
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }


class _UrlPolicy:
    def __init__(self, resolver, allowlist):
        self.resolver = resolver
        self.allowed_hostnames, self.allowed_ip_addresses = (
            self._normalize_allowlist(allowlist)
        )

    def resolve_and_validate(self, url):
        parsed = self._parse_url(url)
        hostname = parsed[1]
        literal_address = self._ip_address(hostname)
        if literal_address is not None:
            normalized_address = str(literal_address)
            if normalized_address not in self.allowed_ip_addresses:
                raise self._blocked_address()
            return self._target(parsed, normalized_address)

        try:
            resolved = self.resolver.resolve(hostname, parsed[2])
            addresses = [
                ipaddress.ip_address(str(address).split("%", 1)[0])
                for address in resolved
            ]
        except SafeFetchError:
            raise
        except (TimeoutError, requests.exceptions.Timeout):
            raise _fetch_timeout() from None
        except Exception:
            raise _download_failed(True) from None

        if not addresses:
            raise _download_failed(True)
        if hostname not in self.allowed_hostnames:
            if any(self._is_blocked(address) for address in addresses):
                raise self._blocked_address()
        return self._target(parsed, str(addresses[0]))

    def verify_peer(self, target, peer_ip):
        try:
            selected = ipaddress.ip_address(target.ip_address)
            peer = ipaddress.ip_address(str(peer_ip).split("%", 1)[0])
        except ValueError:
            raise SafeFetchError(
                "dns_rebinding_detected",
                "The connected address did not match the validated address.",
                False,
            ) from None
        if peer != selected:
            raise SafeFetchError(
                "dns_rebinding_detected",
                "The connected address did not match the validated address.",
                False,
            )

    def redirect_url(self, current_url, location):
        if (
            not isinstance(location, str)
            or not location
            or self._has_unsafe_text(location)
        ):
            raise self._invalid_url()
        try:
            raw_parts = urlsplit(location)
            if raw_parts.scheme and not raw_parts.netloc:
                raise self._invalid_url()
            if location.startswith("//") and not raw_parts.netloc:
                raise self._invalid_url()
            redirect_url = urljoin(current_url, location)
        except SafeFetchError:
            raise
        except (TypeError, ValueError):
            raise self._invalid_url() from None
        self._parse_url(redirect_url)
        return redirect_url

    @classmethod
    def sanitize_referer(cls, referer):
        if not isinstance(referer, str) or cls._has_unsafe_text(referer):
            return None
        try:
            parts = urlsplit(referer)
        except (TypeError, ValueError):
            return None
        if "@" in parts.netloc:
            return None
        try:
            port = parts.port
        except ValueError:
            return None
        if parts.scheme.lower() not in ("http", "https"):
            return None
        if not parts.netloc or not parts.hostname:
            return None
        if port is not None and not 0 < port < 65536:
            return None
        return referer

    @classmethod
    def _parse_url(cls, url):
        if not isinstance(url, str) or cls._has_unsafe_text(url):
            raise cls._invalid_url()
        try:
            parts = urlsplit(url)
        except (TypeError, ValueError):
            raise cls._invalid_url() from None
        if "@" in parts.netloc:
            raise cls._invalid_url()
        try:
            port = parts.port
            raw_hostname = parts.hostname
        except (TypeError, ValueError):
            raise cls._invalid_url() from None
        scheme = parts.scheme.lower()
        if scheme not in ("http", "https"):
            raise cls._invalid_url()
        if not parts.netloc or not raw_hostname:
            raise cls._invalid_url()
        if port is not None and not 0 < port < 65536:
            raise cls._invalid_url()
        hostname = cls._normalize_hostname(raw_hostname)
        if port is None:
            port = 443 if scheme == "https" else 80
        request_target = parts.path or "/"
        if parts.query:
            request_target = "{}?{}".format(request_target, parts.query)
        return scheme, hostname, port, request_target

    @classmethod
    def _normalize_hostname(cls, hostname):
        literal_address = cls._ip_address(hostname)
        if literal_address is not None:
            return str(literal_address)
        candidate = hostname.rstrip(".")
        if not candidate:
            raise cls._invalid_url()
        try:
            normalized = candidate.encode("idna").decode("ascii").lower()
        except (UnicodeError, UnicodeDecodeError):
            raise cls._invalid_url() from None
        if len(normalized) > 253:
            raise cls._invalid_url()
        for label in normalized.split("."):
            if (
                not label
                or len(label) > 63
                or label.startswith("-")
                or label.endswith("-")
                or any(
                    not (character.isalnum() or character == "-")
                    for character in label
                )
            ):
                raise cls._invalid_url()
        return normalized

    @classmethod
    def _normalize_allowlist(cls, allowlist):
        hostnames = set()
        ip_addresses = set()
        for value in allowlist:
            if not isinstance(value, str) or cls._has_unsafe_text(value):
                continue
            address = cls._ip_address(value)
            if address is not None:
                ip_addresses.add(str(address))
                continue
            try:
                hostnames.add(cls._normalize_hostname(value))
            except SafeFetchError:
                continue
        return hostnames, ip_addresses

    @staticmethod
    def _ip_address(value):
        try:
            return ipaddress.ip_address(value)
        except ValueError:
            return None

    @staticmethod
    def _is_blocked(address):
        mapped_address = getattr(address, "ipv4_mapped", None)
        if mapped_address is not None and _UrlPolicy._is_blocked(
            mapped_address
        ):
            return True
        return (
            not address.is_global
            or address.is_loopback
            or address.is_private
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
            or getattr(address, "is_site_local", False)
        )

    @staticmethod
    def _has_unsafe_text(value):
        return "\\" in value or any(
            ord(character) <= 0x20
            or ord(character) == 0x7f
            or unicodedata.category(character) == "Cc"
            for character in value
        )

    @staticmethod
    def _target(parsed, ip_address):
        return ResolvedTarget(
            scheme=parsed[0],
            hostname=parsed[1],
            port=parsed[2],
            ip_address=ip_address,
            request_target=parsed[3],
        )

    @staticmethod
    def _invalid_url():
        return SafeFetchError(
            "invalid_url_policy",
            "The image URL is not allowed.",
            False,
        )

    @staticmethod
    def _blocked_address():
        return SafeFetchError(
            "blocked_address",
            "The image URL resolves to a blocked address.",
            False,
        )


class SafeUrlFetcher:
    def __init__(self, resolver, transport, limits=None, clock=monotonic):
        self.limits = limits or FetchLimits()
        self.transport = transport
        self.clock = clock
        self.policy = _UrlPolicy(
            resolver, self.limits.private_allowlist
        )

    def fetch(self, url, referer=None, deadline=None):
        started_at = self.clock()
        fetch_deadline = started_at + self.limits.total_timeout
        if deadline is None:
            deadline = fetch_deadline
        else:
            deadline = min(deadline, fetch_deadline)
        safe_referer = self.policy.sanitize_referer(referer)
        current_url = url
        for redirect_count in range(self.limits.max_redirects + 1):
            self._check_deadline(deadline)
            target = self.policy.resolve_and_validate(current_url)
            self._check_deadline(deadline)
            response = self._request(target, safe_referer, deadline)
            try:
                self._check_deadline(deadline)
                self.policy.verify_peer(target, response.peer_ip)
                if response.is_redirect:
                    if redirect_count == self.limits.max_redirects:
                        raise SafeFetchError(
                            "too_many_redirects",
                            "The image URL redirected too many times.",
                            False,
                        )
                    current_url = self.policy.redirect_url(
                        current_url, response.location
                    )
                    self._check_deadline(deadline)
                    continue
                self._verify_status(response.status_code)
                content = self._read_bounded(response, deadline)
            finally:
                self._close(response)
            self._check_deadline(deadline)
            return self._verify_image(content, current_url, deadline)
        raise AssertionError("redirect loop did not terminate")

    def _request(self, target, referer, deadline):
        try:
            return self.transport.request(
                target=target,
                referer=referer,
                connect_timeout=self.limits.connect_timeout,
                read_timeout=self.limits.read_timeout,
                deadline=deadline,
            )
        except SafeFetchError:
            raise
        except (TimeoutError, requests.exceptions.Timeout):
            raise _fetch_timeout() from None
        except Exception:
            raise _download_failed(True) from None

    def _read_bounded(self, response, deadline):
        content_encoding = self._header(
            response.headers, "content-encoding"
        )
        if (
            content_encoding
            and content_encoding.strip().lower() != "identity"
        ):
            raise SafeFetchError(
                "unsupported_content_encoding",
                "The image response used an unsupported content encoding.",
                False,
            )
        content_length = self._header(response.headers, "content-length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except (TypeError, ValueError):
                declared_length = None
            if (
                declared_length is not None
                and declared_length >= 0
                and declared_length > self.limits.max_bytes
            ):
                raise _image_too_large()

        chunks = []
        total_bytes = 0
        try:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                self._check_deadline(deadline)
                if not chunk:
                    continue
                total_bytes += len(chunk)
                if total_bytes > self.limits.max_bytes:
                    raise _image_too_large()
                chunks.append(chunk)
            self._check_deadline(deadline)
        except SafeFetchError:
            raise
        except (TimeoutError, requests.exceptions.Timeout):
            raise _fetch_timeout() from None
        except Exception:
            raise _download_failed(True) from None
        return b"".join(chunks)

    def _verify_image(self, content, final_url, deadline):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter(
                    "error", PILImage.DecompressionBombWarning
                )
                self._check_deadline(deadline)
                with PILImage.open(BytesIO(content)) as image:
                    self._verify_pixel_count(image)
                    image.verify()
                self._check_deadline(deadline)
                with PILImage.open(BytesIO(content)) as image:
                    self._verify_pixel_count(image)
                    image.load()
                    image_format = image.format
                    width, height = image.size
                self._check_deadline(deadline)
        except SafeFetchError:
            raise
        except (
            PILImage.DecompressionBombError,
            PILImage.DecompressionBombWarning,
        ):
            raise _too_many_pixels() from None
        except Exception:
            raise SafeFetchError(
                "invalid_image_content",
                "The response was not a valid image.",
                False,
            ) from None
        if image_format not in FORMAT_EXTENSIONS:
            raise SafeFetchError(
                "unsupported_image_format",
                "The response image format is not supported.",
                False,
            )
        return FetchedImage(
            content=content,
            image_format=image_format,
            width=width,
            height=height,
            final_url=final_url,
        )

    def _verify_pixel_count(self, image):
        width, height = image.size
        if width * height > self.limits.max_pixels:
            raise _too_many_pixels()

    def _check_deadline(self, deadline):
        if self.clock() >= deadline:
            raise _fetch_timeout()

    @staticmethod
    def _header(headers, name):
        for header_name, value in headers.items():
            if header_name.lower() == name:
                return value
        return None

    @staticmethod
    def _verify_status(status_code):
        if status_code == 200:
            return
        retryable = status_code in (408, 425, 429) or 500 <= status_code < 600
        raise _download_failed(retryable)

    @staticmethod
    def _close(response):
        try:
            response.close()
        except Exception:
            pass


def _download_failed(retryable):
    return SafeFetchError(
        "image_download_failed",
        "The image could not be downloaded.",
        retryable,
    )


def _fetch_timeout():
    return SafeFetchError(
        "image_fetch_timeout",
        "The image download timed out.",
        True,
    )


def _image_too_large():
    return SafeFetchError(
        "image_too_large",
        "The image exceeded the download size limit.",
        False,
    )


def _too_many_pixels():
    return SafeFetchError(
        "image_too_many_pixels",
        "The image exceeded the pixel limit.",
        False,
    )
