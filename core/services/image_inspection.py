from collections import namedtuple
from io import BytesIO
import warnings

from PIL import Image as PILImage

from django_images.paths import FORMAT_EXTENSIONS


InspectedImage = namedtuple(
    "InspectedImage",
    ("content", "image_format", "width", "height", "final_url"),
)


class ImageInspectionError(Exception):
    def __init__(self, code):
        super(ImageInspectionError, self).__init__(
            _message_for_code(code)
        )
        self.code = code


def inspect_image_bytes(content, final_url, max_pixels, deadline, clock):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", PILImage.DecompressionBombWarning)
            _check_deadline(deadline, clock)
            with PILImage.open(BytesIO(content)) as image:
                _verify_pixel_count(image, max_pixels)
                image.verify()
            _check_deadline(deadline, clock)
            with PILImage.open(BytesIO(content)) as image:
                _verify_pixel_count(image, max_pixels)
                image.load()
                image_format = image.format
                width, height = image.size
            _check_deadline(deadline, clock)
    except ImageInspectionError:
        raise
    except (
        PILImage.DecompressionBombError,
        PILImage.DecompressionBombWarning,
    ):
        raise ImageInspectionError("image_too_many_pixels") from None
    except Exception:
        raise ImageInspectionError("invalid_image_content") from None

    if image_format not in FORMAT_EXTENSIONS:
        raise ImageInspectionError("unsupported_image_format")

    return InspectedImage(
        content=content,
        image_format=image_format,
        width=width,
        height=height,
        final_url=final_url,
    )


def _verify_pixel_count(image, max_pixels):
    width, height = image.size
    if width * height > max_pixels:
        raise ImageInspectionError("image_too_many_pixels")


def _check_deadline(deadline, clock):
    if clock() >= deadline:
        raise ImageInspectionError("image_processing_timeout")


def _message_for_code(code):
    messages = {
        "image_too_large": "The image exceeded the size limit.",
        "image_too_many_pixels": "The image exceeded the pixel limit.",
        "invalid_image_content": "The image content was not valid.",
        "unsupported_image_format": "The image format is not supported.",
        "image_processing_timeout": "The image processing timed out.",
    }
    return messages[code]
