import os
import unicodedata
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from xml.etree import ElementTree

from PIL import Image as PILImage

from django_images.paths import FORMAT_EXTENSIONS


SENSITIVE_QUERY_KEYS = frozenset((
    "access_token", "refresh_token", "token", "auth", "authorization",
    "api_key", "apikey", "key", "secret", "client_secret", "password",
    "passwd", "signature", "sig", "credential", "session", "sessionid",
    "jwt", "code", "x-amz-signature", "x-amz-credential",
    "x-amz-security-token", "x-goog-signature",
))
_PORTABLE_INVALID_CHARS = frozenset('<>:"/\\|?*')
_RESERVED_STEMS = frozenset(
    ("CON", "PRN", "AUX", "NUL")
    + tuple("COM{}".format(index) for index in range(1, 10))
    + tuple("LPT{}".format(index) for index in range(1, 10))
)
_ARCHIVE_PREFIX = "originals/"
_ARCHIVE_ENTRY_MAX_BYTES = 240
_DISPLAY_NAME_MAX_BYTES = 180
_XMP_META_NAMESPACE = "adobe:ns:meta/"
_RDF_NAMESPACE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
_EXIF_NAMESPACE = "http://ns.adobe.com/exif/1.0/"
_PHOTOSHOP_NAMESPACE = "http://ns.adobe.com/photoshop/1.0/"
_DC_NAMESPACE = "http://purl.org/dc/elements/1.1/"
_DIGIKAM_NAMESPACE = "http://www.digikam.org/ns/1.0/"


def redact_url(value):
    if not isinstance(value, str):
        return None, True
    try:
        parsed = urlsplit(value)
        if parsed.scheme.casefold() not in ("http", "https"):
            return None, True
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None, True
    if not hostname:
        return None, True

    host = hostname
    if ":" in host and not host.startswith("["):
        host = "[{}]".format(host)
    if port is not None:
        host = "{}:{}".format(host, port)

    redacted = "@" in parsed.netloc or bool(parsed.fragment)
    retained_pairs = []
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        if key.casefold() in SENSITIVE_QUERY_KEYS:
            redacted = True
        else:
            retained_pairs.append((key, item))
    query = urlencode(retained_pairs, doseq=True)
    return urlunsplit((parsed.scheme.casefold(), host, parsed.path, query, "")), redacted


def format_utc(value):
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def zip_datetime(value):
    value = value.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0)
    value = min(
        max(value, datetime(1980, 1, 1)),
        datetime(2107, 12, 31, 23, 59, 58),
    )
    value = value.replace(second=value.second - value.second % 2)
    return (
        value.year, value.month, value.day,
        value.hour, value.minute, value.second,
    )


def _canonical_extension(mime_type):
    PILImage.init()
    for image_format, current_mime_type in PILImage.MIME.items():
        if current_mime_type == mime_type:
            try:
                return FORMAT_EXTENSIONS[image_format]
            except KeyError:
                break
    raise ValueError("unsupported_mime_type")


def _safe_stem_and_extension(original_filename, canonical_extension):
    if not isinstance(original_filename, str):
        original_filename = ""
    basename = original_filename.replace("\\", "/").rsplit("/", 1)[-1]
    basename = unicodedata.normalize("NFC", basename)
    basename = "".join(
        character for character in basename
        if unicodedata.category(character) not in ("Cc", "Cs")
    )
    stem, original_extension = os.path.splitext(basename)
    stem = "".join(
        "_" if character in _PORTABLE_INVALID_CHARS else character
        for character in stem
    ).strip(" .")
    if original_extension.casefold() == canonical_extension.casefold():
        extension = original_extension
    else:
        extension = canonical_extension
    return stem, extension


def _is_reserved_stem(stem):
    return stem.split(".", 1)[0].upper() in _RESERVED_STEMS


def _usable_stem(stem):
    return bool(stem) and stem not in (".", "..") and not _is_reserved_stem(stem)


def _truncate_utf8(value, byte_limit):
    characters = []
    byte_count = 0
    for character in value:
        encoded = character.encode("utf-8")
        if byte_count + len(encoded) > byte_limit:
            break
        characters.append(character)
        byte_count += len(encoded)
    return "".join(characters).strip(" .")


class PortableNameAllocator(object):
    def __init__(self):
        self._reserved_names = set()

    def reserve(self, item_uuid, pin_id, original_filename, mime_type):
        canonical_extension = _canonical_extension(mime_type)
        stem, extension = _safe_stem_and_extension(
            original_filename, canonical_extension,
        )
        if not _usable_stem(stem):
            stem = "pin-{}".format(pin_id)

        uuid_hex = str(item_uuid).replace("-", "")
        if len(uuid_hex) != 32:
            raise ValueError("invalid_item_uuid")
        for suffix_length in range(0, len(uuid_hex) + 1):
            suffix = "" if suffix_length == 0 else "__{}".format(
                uuid_hex[:max(8, suffix_length)]
            )
            if suffix_length and suffix_length < 8:
                continue
            byte_limit = (
                _ARCHIVE_ENTRY_MAX_BYTES
                - len(_ARCHIVE_PREFIX.encode("utf-8"))
                - len((suffix + extension + ".xmp").encode("utf-8"))
            )
            candidate_stem = _truncate_utf8(stem, byte_limit)
            if not _usable_stem(candidate_stem):
                candidate_stem = _truncate_utf8("pin-{}".format(pin_id), byte_limit)
            image_path = "{}{}{}{}".format(
                _ARCHIVE_PREFIX, candidate_stem, suffix, extension,
            )
            xmp_path = "{}.xmp".format(image_path)
            image_key = image_path.casefold()
            xmp_key = xmp_path.casefold()
            if image_key not in self._reserved_names and xmp_key not in self._reserved_names:
                self._reserved_names.add(image_key)
                self._reserved_names.add(xmp_key)
                return image_path, xmp_path
        raise ValueError("duplicate_item_uuid")


def _safe_display_stem(value):
    if not isinstance(value, str):
        return "board"
    value = unicodedata.normalize("NFC", value)
    value = "".join(
        character for character in value
        if unicodedata.category(character) not in ("Cc", "Cs")
    )
    value = "".join(
        "_" if character in _PORTABLE_INVALID_CHARS else character
        for character in value
    ).strip(" .")
    if not _usable_stem(value):
        return "board"
    return value


def archive_display_name(scope, board_name, completed_at):
    timestamp = completed_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = "-내보내기-{}.zip".format(timestamp)
    if scope == "board":
        prefix = _safe_display_stem(board_name)
        repeated_suffix = suffix[:-4]
        while prefix.endswith(repeated_suffix):
            prefix = prefix[:-len(repeated_suffix)].strip(" .-")
        if not _usable_stem(prefix):
            prefix = "board"
    elif scope == "pins":
        prefix = "선택-Pin"
    else:
        raise ValueError("invalid_export_scope")
    prefix = _truncate_utf8(
        prefix,
        _DISPLAY_NAME_MAX_BYTES - len(suffix.encode("utf-8")),
    )
    if not _usable_stem(prefix):
        prefix = "board" if scope == "board" else "선택-Pin"
    return "{}{}".format(prefix, suffix)


def xml_safe(value):
    return "".join(
        character if (
            ord(character) in (0x9, 0xA, 0xD)
            or 0x20 <= ord(character) <= 0xD7FF
            or 0xE000 <= ord(character) <= 0xFFFD
            or 0x10000 <= ord(character) <= 0x10FFFF
        ) else "\ufffd"
        for character in value
    )


def render_xmp(published_at, description, tags):
    ElementTree.register_namespace("x", _XMP_META_NAMESPACE)
    ElementTree.register_namespace("rdf", _RDF_NAMESPACE)
    ElementTree.register_namespace("exif", _EXIF_NAMESPACE)
    ElementTree.register_namespace("photoshop", _PHOTOSHOP_NAMESPACE)
    ElementTree.register_namespace("dc", _DC_NAMESPACE)
    ElementTree.register_namespace("digiKam", _DIGIKAM_NAMESPACE)

    timestamp = format_utc(published_at)
    root = ElementTree.Element("{{{}}}xmpmeta".format(_XMP_META_NAMESPACE))
    rdf = ElementTree.SubElement(root, "{{{}}}RDF".format(_RDF_NAMESPACE))
    description_node = ElementTree.SubElement(
        rdf,
        "{{{}}}Description".format(_RDF_NAMESPACE),
        {
            "{{{}}}about".format(_RDF_NAMESPACE): "",
            "{{{}}}DateTimeOriginal".format(_EXIF_NAMESPACE): timestamp,
            "{{{}}}DateCreated".format(_PHOTOSHOP_NAMESPACE): timestamp,
        },
    )
    if description:
        description_alt = ElementTree.SubElement(
            description_node, "{{{}}}description".format(_DC_NAMESPACE),
        )
        alt = ElementTree.SubElement(
            description_alt, "{{{}}}Alt".format(_RDF_NAMESPACE),
        )
        item = ElementTree.SubElement(
            alt,
            "{{{}}}li".format(_RDF_NAMESPACE),
            {"{http://www.w3.org/XML/1998/namespace}lang": "x-default"},
        )
        item.text = xml_safe(description)
    if tags:
        tags_list = ElementTree.SubElement(
            description_node, "{{{}}}TagsList".format(_DIGIKAM_NAMESPACE),
        )
        sequence = ElementTree.SubElement(
            tags_list, "{{{}}}Seq".format(_RDF_NAMESPACE),
        )
        for tag in sorted(tags):
            item = ElementTree.SubElement(
                sequence, "{{{}}}li".format(_RDF_NAMESPACE),
            )
            item.text = xml_safe(tag)
    return ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)
