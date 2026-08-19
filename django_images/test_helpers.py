import tempfile

from django.test import override_settings


class TemporaryMediaMixin:
    def setUp(self):
        super(TemporaryMediaMixin, self).setUp()
        self.temporary_media = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_media.cleanup)
        self.media_override = override_settings(
            MEDIA_ROOT=self.temporary_media.name
        )
        self.media_override.enable()
        self.addCleanup(self.media_override.disable)
