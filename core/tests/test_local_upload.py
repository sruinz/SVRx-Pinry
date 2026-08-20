from io import BytesIO
import hashlib

from django.core.files.uploadedfile import (
    InMemoryUploadedFile,
    TemporaryUploadedFile,
)
from django.test import SimpleTestCase
from PIL import Image as PILImage

from core.services.local_upload import LocalUploadError, read_local_upload


READ_SIZE = 64 * 1024


def make_image_bytes(image_format="PNG", size=(2, 3)):
    output = BytesIO()
    PILImage.new("RGB", size, "red").save(output, format=image_format)
    return output.getvalue()


class ManualClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class RecordingStream(BytesIO):
    def __init__(self, content, error=None):
        super(RecordingStream, self).__init__(content)
        self.read_sizes = []
        self.error = error

    def read(self, size=-1):
        self.read_sizes.append(size)
        if self.error is not None:
            raise self.error
        return super(RecordingStream, self).read(size)


class LocalUploadTests(SimpleTestCase):
    def make_in_memory_upload(self, content, filename="from-client.png"):
        stream = RecordingStream(content)
        upload = InMemoryUploadedFile(
            stream,
            "image_file",
            filename,
            "image/png",
            len(content),
            None,
        )
        self.replace_chunks(upload, content)
        return upload, stream

    def make_temporary_upload(self, content, filename="from-client.png"):
        upload = TemporaryUploadedFile(
            filename,
            "image/png",
            len(content),
            None,
        )
        upload.write(content)
        upload.seek(0)
        original_read = upload.file.read
        read_sizes = []

        def record_read(size=-1):
            read_sizes.append(size)
            return original_read(size)

        upload.file.read = record_read
        self.replace_chunks(upload, content)
        self.addCleanup(upload.close)
        return upload, read_sizes

    @staticmethod
    def replace_chunks(upload, content):
        upload.chunk_calls = 0

        def chunks():
            upload.chunk_calls += 1
            return [content]

        upload.chunks = chunks

    def assert_successful_upload(self, upload, read_sizes, content):
        upload.seek(1)

        result = read_local_upload(
            upload,
            max_bytes=len(content),
            max_pixels=6,
            deadline=10.0,
            clock=ManualClock(0.0),
        )

        self.assertEqual(result.content, content)
        self.assertEqual(result.original_filename, "from-client.png")
        self.assertEqual(
            result.content_sha256,
            hashlib.sha256(content).hexdigest(),
        )
        self.assertEqual(result.inspected.image_format, "PNG")
        self.assertEqual(
            (result.inspected.width, result.inspected.height), (2, 3)
        )
        self.assertTrue(read_sizes)
        self.assertTrue(all(size <= READ_SIZE for size in read_sizes))
        self.assertEqual(upload.chunk_calls, 0)

    def test_in_memory_upload_rewinds_reads_bounded_and_uses_server_metadata(self):
        content = make_image_bytes()
        upload, stream = self.make_in_memory_upload(content)

        self.assert_successful_upload(upload, stream.read_sizes, content)

    def test_temporary_upload_rewinds_reads_bounded_and_uses_server_metadata(self):
        content = make_image_bytes()
        upload, read_sizes = self.make_temporary_upload(content)

        self.assert_successful_upload(upload, read_sizes, content)

    def test_exact_byte_limit_is_accepted_without_calling_chunks(self):
        content = make_image_bytes()
        upload, stream = self.make_in_memory_upload(content)

        result = read_local_upload(
            upload,
            max_bytes=len(content),
            max_pixels=6,
            deadline=10.0,
            clock=ManualClock(0.0),
        )

        self.assertEqual(result.content, content)
        self.assertTrue(all(size <= READ_SIZE for size in stream.read_sizes))
        self.assertEqual(upload.chunk_calls, 0)

    def test_limit_plus_one_stops_without_another_read(self):
        content = b"a" * (READ_SIZE + 1)
        upload_factories = (
            self.make_in_memory_upload,
            self.make_temporary_upload,
        )
        for factory in upload_factories:
            with self.subTest(factory=factory.__name__):
                upload, read_source = factory(content)
                read_sizes = getattr(
                    read_source, "read_sizes", read_source
                )

                with self.assertRaises(LocalUploadError) as caught:
                    read_local_upload(
                        upload,
                        max_bytes=READ_SIZE,
                        max_pixels=6,
                        deadline=10.0,
                        clock=ManualClock(0.0),
                    )

                self.assertEqual(caught.exception.code, "image_too_large")
                self.assertEqual(read_sizes, [READ_SIZE, READ_SIZE])
                self.assertEqual(upload.chunk_calls, 0)

    def assert_safe_error(self, upload, code, **kwargs):
        with self.assertRaises(LocalUploadError) as caught:
            read_local_upload(upload, **kwargs)
        self.assertEqual(caught.exception.code, code)
        rendered = "{} {}".format(caught.exception, caught.exception.as_dict())
        self.assertNotIn("private-name", rendered)
        self.assertNotIn("internal detail", rendered)

    def test_rejects_empty_invalid_pixel_and_unsupported_content_safely(self):
        cases = (
            (b"", "invalid_image_content", 6),
            (b"internal detail", "invalid_image_content", 6),
            (make_image_bytes(size=(2, 3)), "image_too_many_pixels", 5),
            (make_image_bytes("ICO", (32, 32)), "unsupported_image_format", 2000),
        )
        for content, code, max_pixels in cases:
            with self.subTest(code=code):
                upload, stream = self.make_in_memory_upload(
                    content, "private-name.ico"
                )
                self.assert_safe_error(
                    upload,
                    code,
                    max_bytes=max(len(content), 1),
                    max_pixels=max_pixels,
                    deadline=10.0,
                    clock=ManualClock(0.0),
                )
                self.assertTrue(
                    all(size <= READ_SIZE for size in stream.read_sizes)
                )

    def test_expired_deadline_and_read_error_are_safely_mapped(self):
        content = make_image_bytes()
        upload, stream = self.make_in_memory_upload(content, "private-name.png")
        self.assert_safe_error(
            upload,
            "image_processing_timeout",
            max_bytes=len(content),
            max_pixels=6,
            deadline=0.0,
            clock=ManualClock(0.0),
        )
        self.assertEqual(stream.read_sizes, [])

        upload, stream = self.make_in_memory_upload(content, "private-name.png")
        stream.error = OSError("internal detail")
        self.assert_safe_error(
            upload,
            "invalid_image_content",
            max_bytes=len(content),
            max_pixels=6,
            deadline=10.0,
            clock=ManualClock(0.0),
        )
