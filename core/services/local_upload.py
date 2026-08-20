from collections import namedtuple
import hashlib

from core.services.image_inspection import (
    ImageInspectionError,
    inspect_image_bytes,
)


READ_SIZE = 64 * 1024
LocalUpload = namedtuple(
    "LocalUpload",
    ("content", "original_filename", "content_sha256", "inspected"),
)


class LocalUploadError(Exception):
    def __init__(self, code):
        super(LocalUploadError, self).__init__(_message_for_code(code))
        self.code = code

    def as_dict(self):
        return {
            "code": self.code,
            "message": _message_for_code(self.code),
            "retryable": False,
        }


def read_local_upload(uploaded_file, max_bytes, max_pixels, deadline, clock):
    content, content_sha256 = _read_bounded(
        uploaded_file, max_bytes, deadline, clock
    )
    try:
        inspected = inspect_image_bytes(
            content,
            None,
            max_pixels,
            deadline,
            clock,
        )
    except ImageInspectionError as error:
        raise LocalUploadError(error.code) from None

    return LocalUpload(
        content=content,
        original_filename=_filename(uploaded_file),
        content_sha256=content_sha256,
        inspected=inspected,
    )


def _read_bounded(uploaded_file, max_bytes, deadline, clock):
    content = []
    digest = hashlib.sha256()
    total_bytes = 0
    try:
        _check_deadline(deadline, clock)
        uploaded_file.seek(0)
        while True:
            _check_deadline(deadline, clock)
            chunk = uploaded_file.read(READ_SIZE)
            _check_deadline(deadline, clock)
            if not chunk:
                break
            total_bytes += len(chunk)
            if total_bytes > max_bytes:
                raise LocalUploadError("image_too_large")
            digest.update(chunk)
            content.append(chunk)
    except LocalUploadError:
        raise
    except Exception:
        raise LocalUploadError("invalid_image_content") from None
    return b"".join(content), digest.hexdigest()


def _check_deadline(deadline, clock):
    if clock() >= deadline:
        raise LocalUploadError("image_processing_timeout")


def _filename(uploaded_file):
    filename = getattr(uploaded_file, "name", "")
    return filename if isinstance(filename, str) else ""


def _message_for_code(code):
    messages = {
        "image_too_large": "The image exceeded the upload size limit.",
        "image_too_many_pixels": "The image exceeded the pixel limit.",
        "invalid_image_content": "The uploaded file was not a valid image.",
        "unsupported_image_format": "The uploaded image format is not supported.",
        "image_processing_timeout": "The image processing timed out.",
    }
    return messages[code]
