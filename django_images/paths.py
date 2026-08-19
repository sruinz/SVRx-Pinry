import os
import unicodedata

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


class UnsupportedImageFormat(Exception):
    pass


def sanitize_original_filename(filename: str) -> str:
    basename = os.path.basename(filename)
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
