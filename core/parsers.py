from io import BytesIO

from django.conf import settings
from rest_framework.exceptions import ParseError
from rest_framework.parsers import JSONParser


_READ_CHUNK_BYTES = 64 * 1024


class LimitedJSONParser(JSONParser):
    def __init__(self, max_bytes=None):
        if max_bytes is None:
            max_bytes = settings.PINRY_BATCH_MAX_BODY_BYTES
        if type(max_bytes) is not int or max_bytes < 0:
            raise ValueError("max_bytes must be a non-negative integer")
        self.max_bytes = max_bytes

    def parse(self, stream, media_type=None, parser_context=None):
        total = 0
        chunks = []
        while total <= self.max_bytes:
            requested = min(
                _READ_CHUNK_BYTES,
                self.max_bytes + 1 - total,
            )
            try:
                chunk = stream.read(requested)
            except Exception:
                raise self._invalid_json() from None
            if not isinstance(chunk, (bytes, bytearray)):
                raise self._invalid_json()
            if not chunk:
                break
            if len(chunk) > requested or total + len(chunk) > self.max_bytes:
                raise self._body_too_large()
            chunks.append(bytes(chunk))
            total += len(chunk)

        body = b"".join(chunks)
        try:
            return super(LimitedJSONParser, self).parse(
                BytesIO(body),
                media_type=media_type,
                parser_context=parser_context,
            )
        except (ParseError, RecursionError):
            raise self._invalid_json() from None

    @staticmethod
    def _body_too_large():
        return ParseError({
            "code": "batch_body_too_large",
            "message": "The batch request body is too large.",
        })

    @staticmethod
    def _invalid_json():
        return ParseError({
            "code": "batch_invalid_json",
            "message": "The batch request body is not valid JSON.",
        })
