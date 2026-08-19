import PIL.Image
import requests

from io import BytesIO

from django.conf import settings
from django.core.files.uploadedfile import InMemoryUploadedFile
from django.db import models, transaction
from django.dispatch import receiver

from django_images.models import Image as BaseImage, Thumbnail
from taggit.managers import TaggableManager

from users.models import User


class ImageManager(models.Manager):
    _default_ua = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 5.1) '
                      'AppleWebKit/537.36 (KHTML, like Gecko) '
                      'Chrome/48.0.2564.82 Safari/537.36',
    }

    @staticmethod
    def _is_valid_image(fp):
        fp.seek(0)
        try:
            PIL.Image.open(fp)
        except PIL.UnidentifiedImageError:
            fp.seek(0)
            return False
        else:
            fp.seek(0)
            return True

    # FIXME: Move this into an asynchronous task
    def create_for_url(self, url, referer=None):
        file_name = url.split("/")[-1].split('#')[0].split('?')[0]
        buf = BytesIO()
        headers = dict(self._default_ua)
        if referer is not None:
            headers["Referer"] = referer
        response = requests.get(url, headers=headers)
        buf.write(response.content)
        if not self._is_valid_image(buf):
            return None
        obj = InMemoryUploadedFile(buf, 'image', file_name,
                                   None, buf.tell(), None)
        # create the image and its thumbnails in one transaction, removing
        # a chance of getting Database into a inconsistent state when we
        # try to create thumbnails one by one later
        image = self.create(image=obj)
        Thumbnail.objects.get_or_create_at_sizes(image, settings.IMAGE_SIZES.keys())
        return image


class Image(BaseImage):
    objects = ImageManager()

    class Sizes:
        standard = "standard"
        thumbnail = "thumbnail"
        square = "square"

    class Meta:
        proxy = True

    @property
    def standard(self):
        return Thumbnail.objects.get(
            original=self, size=self.Sizes.standard
        )

    @property
    def thumbnail(self):
        return Thumbnail.objects.get(
            original=self, size=self.Sizes.thumbnail
        )

    @property
    def square(self):
        return Thumbnail.objects.get(
            original=self, size=self.Sizes.square
        )


class Board(models.Model):
    class Meta:
        unique_together = ("submitter", "name")
        index_together = ("submitter", "name")

    submitter = models.ForeignKey(User, on_delete=models.CASCADE)
    name = models.CharField(max_length=128, blank=False, null=False)
    private = models.BooleanField(default=False, blank=False)
    pins = models.ManyToManyField("Pin", related_name="pins", blank=True)

    published = models.DateTimeField(auto_now_add=True)


class Pin(models.Model):
    submitter = models.ForeignKey(User, on_delete=models.CASCADE)
    private = models.BooleanField(default=False, blank=False)
    url = models.CharField(null=True, blank=True, max_length=2048)
    referer = models.CharField(null=True, blank=True, max_length=2048)
    description = models.TextField(blank=True, null=True)
    image = models.ForeignKey(Image, related_name='pin', on_delete=models.CASCADE)
    published = models.DateTimeField(auto_now_add=True)
    trashed_at = models.DateTimeField(blank=True, null=True)
    tags = TaggableManager()

    def tag_list(self):
        return self.tags.all()

    def __unicode__(self):
        return '%s - %s' % (self.submitter, self.published)


class BatchImportItem(models.Model):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STATE_CHOICES = (
        (PENDING, PENDING),
        (SUCCEEDED, SUCCEEDED),
        (FAILED, FAILED),
    )

    submitter = models.ForeignKey(User, on_delete=models.CASCADE)
    batch_id = models.UUIDField()
    client_item_id = models.UUIDField()
    request_fingerprint = models.CharField(max_length=64)
    state = models.CharField(max_length=16, choices=STATE_CHOICES)
    pin = models.ForeignKey(
        Pin,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    error_code = models.CharField(max_length=64, null=True, blank=True)
    retryable = models.NullBooleanField()
    lease_uuid = models.UUIDField(null=True, blank=True)
    lease_generation = models.PositiveIntegerField(default=0)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = (("submitter", "client_item_id"),)


@receiver(models.signals.post_delete, sender=Pin)
def delete_unreferenced_pin_image(sender, instance, **kwargs):
    image_id = instance.image_id
    using = kwargs.get("using")

    def delete_after_pin_commit():
        with transaction.atomic(using=using):
            image = (
                BaseImage.objects.select_for_update()
                .using(using)
                .filter(pk=image_id)
                .first()
            )
            if image is None:
                return
            if Pin.objects.filter(image_id=image_id).using(using).exists():
                return
            image.delete(using=using)

    transaction.on_commit(delete_after_pin_commit, using=using)
