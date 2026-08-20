import os
import unicodedata
import uuid

from PIL import Image as PILImage


FORMAT_EXTENSIONS = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "GIF": ".gif",
    "WEBP": ".webp",
    "BMP": ".bmp",
    "TIFF": ".tif",
}
DERIVATIVE_NAMES = frozenset(("thumbnail", "standard", "square"))
_IMAGE_FIELD_MAX_LENGTH = 255
_LEAF_MAX_BYTES = 255
_PORTABLE_INVALID_CHARS = frozenset('<>:"/\\|?*')
_RESERVED_STEMS = frozenset(
    ("CON", "PRN", "AUX", "NUL")
    + tuple("COM{}".format(index) for index in range(1, 10))
    + tuple("LPT{}".format(index) for index in range(1, 10))
)
_CANONICAL_EXTENSIONS = frozenset(FORMAT_EXTENSIONS.values())


class UnsupportedImageFormat(Exception):
    pass


def sanitize_original_filename(filename: str) -> str:
    basename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    basename = unicodedata.normalize("NFC", basename)
    basename = "".join(
        character
        for character in basename
        if unicodedata.category(character) != "Cc"
    )
    return basename[:255]


def canonical_extension(file_obj) -> str:
    position = file_obj.tell()
    try:
        file_obj.seek(0)
        try:
            with PILImage.open(file_obj) as image:
                image_format = image.format
        except (OSError, PILImage.UnidentifiedImageError):
            raise UnsupportedImageFormat(None)
    finally:
        file_obj.seek(position)
    try:
        return FORMAT_EXTENSIONS[image_format]
    except KeyError:
        raise UnsupportedImageFormat(image_format)


def _asset_uuid_parts(asset_uuid):
    try:
        parsed = uuid.UUID(str(asset_uuid))
    except (AttributeError, TypeError, ValueError):
        raise ValueError("invalid_asset_uuid") from None
    return str(parsed), parsed.hex


def _reserved_stem(stem):
    return stem.split(".", 1)[0].upper() in _RESERVED_STEMS


def _usable_stem(stem):
    return (
        bool(stem)
        and stem not in (".", "..")
        and not _reserved_stem(stem)
    )


def _physical_stem(original_filename):
    basename = sanitize_original_filename(original_filename)
    dot_index = basename.rfind(".")
    stem = basename[:dot_index] if dot_index >= 0 else basename
    stem = "".join(
        "_" if character in _PORTABLE_INVALID_CHARS else character
        for character in stem
    )
    return stem.strip(" .")


def _fit_stem(stem, prefix, extension):
    character_limit = (
        _IMAGE_FIELD_MAX_LENGTH - len(prefix) - len(extension)
    )
    byte_limit = _LEAF_MAX_BYTES - len(extension.encode("utf-8"))
    characters = []
    byte_count = 0
    for character in stem:
        encoded = character.encode("utf-8")
        if (
            len(characters) >= character_limit
            or byte_count + len(encoded) > byte_limit
        ):
            break
        characters.append(character)
        byte_count += len(encoded)
    return "".join(characters).strip(" .")


def is_valid_original_leaf(asset_uuid, leaf):
    try:
        asset_uuid_text, _asset_uuid_hex = _asset_uuid_parts(asset_uuid)
    except ValueError:
        return False
    if not isinstance(leaf, str) or not leaf:
        return False
    normalized_leaf = unicodedata.normalize("NFC", leaf)
    if any(
        unicodedata.category(character) == "Cc"
        or character in _PORTABLE_INVALID_CHARS
        for character in normalized_leaf
    ):
        return False
    stem, extension = os.path.splitext(normalized_leaf)
    if extension not in _CANONICAL_EXTENSIONS:
        return False
    if stem != stem.strip(" .") or not _usable_stem(stem):
        return False
    relative_path = "originals/{}/{}".format(
        asset_uuid_text, normalized_leaf
    )
    return (
        len(relative_path) <= _IMAGE_FIELD_MAX_LENGTH
        and len(leaf.encode("utf-8")) <= _LEAF_MAX_BYTES
    )


def canonical_original_path(asset_uuid, original_filename, extension):
    asset_uuid_text, asset_uuid_hex = _asset_uuid_parts(asset_uuid)
    if extension not in _CANONICAL_EXTENSIONS:
        raise ValueError("invalid_original_extension")
    prefix = "originals/{}/".format(asset_uuid_text)
    fallback = "image-{}".format(asset_uuid_hex[:12])
    stem = _physical_stem(original_filename)
    if not _usable_stem(stem):
        stem = fallback
    stem = _fit_stem(stem, prefix, extension)
    if not _usable_stem(stem):
        stem = _fit_stem(fallback, prefix, extension)
    leaf = "{}{}".format(stem, extension)
    if not is_valid_original_leaf(asset_uuid_text, leaf):
        raise ValueError("invalid_original_filename")
    return "{}{}".format(prefix, leaf)


def asset_upload_to(instance, filename: str) -> str:
    extension = canonical_extension(instance.image)
    if instance._meta.model_name == "image":
        instance.original_filename = sanitize_original_filename(filename)
        return "originals/{}/original{}".format(
            instance.asset_uuid, extension
        )
    if instance.size not in DERIVATIVE_NAMES:
        raise ValueError(
            "Unsupported derivative size: {}".format(instance.size)
        )
    return "derivatives/{}/{}{}".format(
        instance.original.asset_uuid,
        instance.size,
        extension,
    )
