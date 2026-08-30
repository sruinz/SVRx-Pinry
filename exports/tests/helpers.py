import hashlib
import os
import tempfile
from io import BytesIO

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from PIL import Image as PillowImage
from PIL.PngImagePlugin import PngInfo

from core.models import Board, Image, MediaAsset, Pin
from users.models import User


def create_export_user(name):
    return User.objects.create_user(
        username=name,
        email="{}@example.test".format(name),
        password="password",
    )


def _png_bytes(color="red", marker="pin.png"):
    output = BytesIO()
    metadata = PngInfo()
    metadata.add_text("fixture", marker)
    PillowImage.new("RGB", (2, 2), color).save(
        output,
        format="PNG",
        pnginfo=metadata,
    )
    return output.getvalue()


def create_export_pin(owner, private=False, color="red", filename="pin.png"):
    content = _png_bytes(color, filename)
    image = Image.objects.create(
        image=SimpleUploadedFile(filename, content, content_type="image/png"),
        original_filename=filename,
    )
    MediaAsset.objects.create(
        submitter=owner,
        image=image,
        content_sha256=hashlib.sha256(content).hexdigest(),
    )
    return Pin.objects.create(
        submitter=owner,
        image=image,
        private=private,
    )


def create_export_board(owner, name="board", pins=()):
    board = Board.objects.create(submitter=owner, name=name)
    board.pins.set(tuple(pins))
    return board


class ExportStorageMixin(object):
    def setUp(self):
        self._media_directory = tempfile.TemporaryDirectory()
        self._export_directory = tempfile.TemporaryDirectory()
        media_root = os.path.realpath(self._media_directory.name)
        export_root = os.path.realpath(self._export_directory.name)
        os.chmod(media_root, 0o700)
        os.chmod(export_root, 0o700)
        self._settings = override_settings(
            MEDIA_ROOT=media_root,
            PINRY_EXPORT_ROOT=export_root,
            PINRY_EXPORT_SPACE_MIN_MARGIN_BYTES=0,
        )
        self._settings.enable()
        super(ExportStorageMixin, self).setUp()

    def tearDown(self):
        try:
            super(ExportStorageMixin, self).tearDown()
        finally:
            self._settings.disable()
            self._export_directory.cleanup()
            self._media_directory.cleanup()
