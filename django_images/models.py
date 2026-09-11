import hashlib
import os.path
import uuid

from django.db import models, transaction
from django.core.files.uploadedfile import InMemoryUploadedFile
from django.dispatch import receiver

from importlib import import_module

from django.urls import reverse

from . import utils
from .animation import inspect_animation
from .settings import IMAGE_SIZES, IMAGE_PATH, IMAGE_AUTO_DELETE
from .startup_validation import STARTUP_VALIDATION_CONTRACT_VERSION


def hashed_upload_to(instance, filename, **kwargs):
    image_type = 'original' if isinstance(instance, Image) else 'thumbnail'
    prefix = 'image/%s/by-md5/' % (image_type,)
    hasher = hashlib.md5()
    for chunk in instance.image.chunks():
        hasher.update(chunk)
    hash_ = hasher.hexdigest()
    base, ext = os.path.splitext(filename)
    return '%(prefix)s%(first)s/%(second)s/%(hash)s/%(base)s%(ext)s' % {
        'prefix': prefix,
        'first': hash_[0],
        'second': hash_[1],
        'hash': hash_,
        'base': base,
        'ext': ext,
    }


if IMAGE_PATH is None:
    upload_to = hashed_upload_to
else:
    if callable(IMAGE_PATH):
        upload_to = IMAGE_PATH
    else:
        parts = IMAGE_PATH.split('.')
        module_name = '.'.join(parts[:-1])
        module = import_module(module_name)
        upload_to = getattr(module, parts[-1])


class Image(models.Model):
    image = models.ImageField(upload_to=upload_to,
                              height_field='height', width_field='width',
                              max_length=255)
    asset_uuid = models.UUIDField(default=uuid.uuid4, editable=False,
                                  unique=True)
    original_filename = models.CharField(editable=False, max_length=255)
    height = models.PositiveIntegerField(default=0, editable=False)
    width = models.PositiveIntegerField(default=0, editable=False)
    animation_status = models.CharField(max_length=16, null=True, editable=False)

    def _read_animation_status(self):
        if not self.image:
            return "unreadable"
        if not self.image._committed:
            return inspect_animation(self.image.file)
        if os.path.splitext(self.image.name)[1].lower() not in (".gif", ".webp"):
            return "static"
        try:
            with self.image.storage.open(self.image.name, "rb") as source:
                return inspect_animation(source)
        except OSError:
            return "unreadable"

    def save(self, *args, **kwargs):
        replacing_upload = self.image and not self.image._committed
        update_fields = kwargs.get("update_fields")
        writes_image = update_fields is None or "image" in update_fields
        if writes_image and (replacing_upload or (
            self._state.adding and self.animation_status is None
        )):
            self.animation_status = self._read_animation_status()
            if update_fields is not None:
                kwargs["update_fields"] = set(update_fields) | {"animation_status"}
        return super(Image, self).save(*args, **kwargs)

    @property
    def animation_format(self):
        if self.animation_status is None and self.pk and (
            os.path.splitext(self.image.name)[1].lower() in (".gif", ".webp")
        ):
            rows = Image.objects.using(self._state.db).filter(
                pk=self.pk, image=self.image.name
            )
            # 같은 원본을 참조하는 Pin이 이미 판별했다면 파일을 다시 열지 않는다.
            current = rows.values_list("animation_status", flat=True).first()
            if current is None:
                current = self._read_animation_status()
                # save()의 썸네일 삭제 신호를 발생시키지 않는 메타데이터 갱신이다.
                rows.filter(animation_status__isnull=True).update(animation_status=current)
            self.animation_status = current
        return {"gif": "GIF", "webp": "WEBP"}.get(self.animation_status)

    def get_by_size(self, size):
        return self.thumbnail_set.get(size=size)

    def get_absolute_url(self, size=None):
        if not size:
            return self.image.url
        try:
            return self.get_by_size(size).image.url
        except Thumbnail.DoesNotExist:
            return reverse('image-thumbnail', args=(self.id, size))


class ThumbnailManager(models.Manager):
    def get_or_create_at_sizes(self, image, sizes):
        sizes_to_create = list(sizes)
        sized = {}
        for size in sizes:
            if size not in IMAGE_SIZES:
                raise ValueError("Received unknown size: %s" % size)

            try:
                sized[size] = image.get_by_size(size)
            except Thumbnail.DoesNotExist:
                pass
            else:
                sizes_to_create.remove(size)

        if sizes_to_create:
            bufs = [
                utils.write_image_in_memory(img)
                for img in utils.scale_and_crop_iter(
                    image.image,
                    [IMAGE_SIZES[size] for size in sizes_to_create])
            ]
            for size, buf in zip(sizes_to_create, bufs):
                # and save to storage
                thumb_file = InMemoryUploadedFile(buf, "image", size,
                                                  None, buf.tell(), None)
                sized[size], created = image.thumbnail_set.get_or_create(
                    size=size, defaults={'image': thumb_file})

        # Make sure this is in the correct order
        return [sized[size] for size in sizes]


class Thumbnail(models.Model):
    original = models.ForeignKey(Image, on_delete=models.CASCADE)
    image = models.ImageField(upload_to=upload_to,
                              height_field='height', width_field='width',
                              max_length=255)
    size = models.CharField(max_length=100)
    height = models.PositiveIntegerField(default=0, editable=False)
    width = models.PositiveIntegerField(default=0, editable=False)

    objects = ThumbnailManager()

    class Meta:
        unique_together = ('original', 'size')

    def get_absolute_url(self):
        return self.image.url


class PendingMediaDeletion(models.Model):
    ORIGINAL = "original"
    THUMBNAIL = "thumbnail"
    KIND_CHOICES = (
        (ORIGINAL, "original"),
        (THUMBNAIL, "thumbnail"),
    )

    kind = models.CharField(max_length=16, choices=KIND_CHOICES)
    name = models.CharField(max_length=255)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("kind", "name")


class StartupValidationState(models.Model):
    contract_version = models.PositiveIntegerField(default=0)

    @classmethod
    def mark_current(cls):
        return cls.objects.update_or_create(
            pk=1,
            defaults={
                "contract_version": STARTUP_VALIDATION_CONTRACT_VERSION,
            },
        )

    @classmethod
    def invalidate(cls):
        return cls.objects.filter(pk=1).update(contract_version=0)


@receiver(models.signals.post_save)
def original_changed(sender, instance, created, **kwargs):
    if isinstance(instance, Image):
        instance.thumbnail_set.all().delete()


@receiver(models.signals.post_delete)
def delete_image_files(sender, instance, **kwargs):
    if isinstance(instance, (Image, Thumbnail)) and IMAGE_AUTO_DELETE:
        name = instance.image.name
        if not name:
            return
        kind = (
            PendingMediaDeletion.ORIGINAL
            if isinstance(instance, Image)
            else PendingMediaDeletion.THUMBNAIL
        )
        using = kwargs.get("using")
        pending, _ = PendingMediaDeletion.objects.using(using).get_or_create(
            kind=kind,
            name=name,
        )
        pending_id = pending.pk

        def delete_after_commit():
            from django_images.services.media_deletion import (
                process_pending_media_deletion,
            )

            process_pending_media_deletion(pending_id, using=using)

        transaction.on_commit(delete_after_commit, using=using)
