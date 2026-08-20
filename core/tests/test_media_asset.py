from django.db import IntegrityError, transaction
from django.test import TestCase

from core.models import Image, MediaAsset
from users.models import User


class MediaAssetTests(TestCase):
    def setUp(self):
        self.submitter = User.objects.create_user(username="asset-owner")
        self.other_submitter = User.objects.create_user(
            username="other-asset-owner"
        )

    def _create_image(self, name):
        return Image.objects.create(
            image="image/original/{}.png".format(name),
            original_filename="{}.png".format(name),
            width=1,
            height=1,
        )

    def test_same_submitter_and_content_hash_is_unique(self):
        content_sha256 = "a" * 64
        MediaAsset.objects.create(
            submitter=self.submitter,
            image=self._create_image("first"),
            content_sha256=content_sha256,
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            MediaAsset.objects.create(
                submitter=self.submitter,
                image=self._create_image("second"),
                content_sha256=content_sha256,
            )

    def test_different_submitters_may_use_same_content_hash(self):
        content_sha256 = "b" * 64

        first = MediaAsset.objects.create(
            submitter=self.submitter,
            image=self._create_image("first-owner"),
            content_sha256=content_sha256,
        )
        second = MediaAsset.objects.create(
            submitter=self.other_submitter,
            image=self._create_image("second-owner"),
            content_sha256=content_sha256,
        )

        self.assertNotEqual(first.image_id, second.image_id)
        self.assertEqual(MediaAsset.objects.count(), 2)

    def test_deleting_image_cascades_to_media_asset(self):
        image = self._create_image("cascade")
        asset = MediaAsset.objects.create(
            submitter=self.submitter,
            image=image,
            content_sha256="c" * 64,
        )

        image.delete()

        self.assertFalse(MediaAsset.objects.filter(pk=asset.pk).exists())
