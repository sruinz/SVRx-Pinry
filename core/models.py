import logging
import time

from django.conf import settings
from django.db import connections, models, router, transaction
from django.dispatch import receiver

from django_images.models import Image as BaseImage, Thumbnail
from django_images.file_ops import media_dedup_lock, open_media_root
from taggit.managers import TaggableManager

from users.models import User


logger = logging.getLogger(__name__)


def _log_pin_image_cleanup_failure(image_id, error):
    try:
        logger.warning(
            "pin_image_cleanup_failed",
            extra={
                "image_id": image_id,
                "media_error": error.__class__.__name__,
            },
        )
    except Exception:
        pass


class Image(BaseImage):
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


class MediaAsset(models.Model):
    submitter = models.ForeignKey(User, on_delete=models.CASCADE)
    image = models.OneToOneField(
        BaseImage,
        related_name="media_asset",
        on_delete=models.CASCADE,
    )
    content_sha256 = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = (("submitter", "content_sha256"),)


class Board(models.Model):
    class Meta:
        unique_together = ("submitter", "name")
        index_together = ("submitter", "name")

    submitter = models.ForeignKey(User, on_delete=models.CASCADE)
    name = models.CharField(max_length=128, blank=False, null=False)
    private = models.BooleanField(default=False, blank=False)
    pins = models.ManyToManyField("Pin", related_name="pins", blank=True)

    published = models.DateTimeField(auto_now_add=True)


class PinQuerySet(models.QuerySet):
    def delete(self):
        assert self.query.can_filter(), (
            "Cannot use 'limit' or 'offset' with delete."
        )
        if self._fields is not None:
            raise TypeError(
                "Cannot call delete() after .values() or .values_list()"
            )
        database_alias = self._db or router.db_for_write(self.model)
        pins = list(self.using(database_alias))
        if not pins or not MediaAsset.objects.using(database_alias).filter(
            image_id__in=[pin.image_id for pin in pins]
        ).exists():
            return super(PinQuerySet, self).delete()
        if len(pins) != 1:
            raise RuntimeError("registered_pin_bulk_delete_unsupported")
        result = pins[0].delete(using=database_alias)
        self._result_cache = None
        return result


class Pin(models.Model):
    submitter = models.ForeignKey(User, on_delete=models.CASCADE)
    private = models.BooleanField(default=False, blank=False)
    url = models.CharField(null=True, blank=True, max_length=2048)
    referer = models.CharField(null=True, blank=True, max_length=2048)
    description = models.TextField(blank=True, null=True)
    image = models.ForeignKey(Image, related_name='pin', on_delete=models.CASCADE)
    published = models.DateTimeField(auto_now_add=True)
    tags = TaggableManager()
    objects = PinQuerySet.as_manager()

    def tag_list(self):
        return self.tags.all()

    def __unicode__(self):
        return '%s - %s' % (self.submitter, self.published)

    def delete(self, using=None, keep_parents=False):
        if getattr(self, "_media_delete_managed", False):
            return super(Pin, self).delete(
                using=using,
                keep_parents=keep_parents,
            )
        return _delete_pin_with_registry_lock(
            self,
            using=using,
            keep_parents=keep_parents,
        )


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
    if getattr(instance, "_media_delete_managed", False):
        return
    image_id = instance.image_id
    using = kwargs.get("using")

    def delete_after_pin_commit():
        try:
            registry = (
                MediaAsset.objects.using(using)
                .filter(image_id=image_id)
                .values(
                    "pk",
                    "image_id",
                    "submitter_id",
                    "content_sha256",
                )
                .first()
            )
            if registry is None:
                _delete_legacy_unreferenced_image(image_id, using)
                return
            _delete_registered_unreferenced_image(
                image_id,
                registry,
                using,
            )
        except Exception as error:
            _log_pin_image_cleanup_failure(image_id, error)

    transaction.on_commit(delete_after_pin_commit, using=using)


def _delete_pin_with_registry_lock(pin, using=None, keep_parents=False):
    if pin.pk is None:
        return super(Pin, pin).delete(
            using=using,
            keep_parents=keep_parents,
        )
    database_alias = using or router.db_for_write(Pin, instance=pin)
    registry = (
        MediaAsset.objects.using(database_alias)
        .filter(image_id=pin.image_id)
        .values(
            "pk",
            "image_id",
            "submitter_id",
            "content_sha256",
        )
        .first()
    )
    if registry is None:
        return super(Pin, pin).delete(
            using=database_alias,
            keep_parents=keep_parents,
        )

    database = connections[database_alias]
    if not database.get_autocommit() or database.in_atomic_block:
        raise RuntimeError("registered_pin_delete_requires_autocommit")

    root_directory = open_media_root(settings.MEDIA_ROOT)
    result = None
    commit_marker = [False]
    try:
        clock = time.monotonic
        deadline = clock() + settings.PINRY_FETCH_TOTAL_TIMEOUT
        with media_dedup_lock(
            root_directory,
            registry["submitter_id"],
            registry["content_sha256"],
            deadline=deadline,
            clock=clock,
        ):
            with transaction.atomic(using=database_alias):
                transaction.on_commit(
                    lambda: commit_marker.__setitem__(0, True),
                    using=database_alias,
                )
                current_registry = (
                    MediaAsset.objects.select_for_update()
                    .using(database_alias)
                    .filter(
                        pk=registry["pk"],
                        image_id=registry["image_id"],
                    )
                    .first()
                )
                current_pin = (
                    Pin.objects.select_for_update()
                    .using(database_alias)
                    .filter(pk=pin.pk, image_id=registry["image_id"])
                    .first()
                )
                image = (
                    BaseImage.objects.select_for_update()
                    .using(database_alias)
                    .filter(pk=registry["image_id"])
                    .first()
                )
                if (
                    current_registry is None
                    or current_pin is None
                    or image is None
                    or current_registry.submitter_id
                    != registry["submitter_id"]
                    or current_registry.content_sha256
                    != registry["content_sha256"]
                ):
                    raise RuntimeError("registered_media_identity_changed")
                current_pin._media_delete_managed = True
                result = current_pin.delete(
                    using=database_alias,
                    keep_parents=keep_parents,
                )
                if not Pin.objects.filter(
                    image_id=registry["image_id"]
                ).using(database_alias).exists():
                    image.delete(using=database_alias)
    except BaseException as error:
        try:
            root_directory.close()
        except BaseException:
            pass
        if commit_marker[0]:
            pin.pk = None
            if isinstance(error, Exception):
                _log_pin_image_cleanup_failure(registry["image_id"], error)
                return result
        raise

    pin.pk = None
    try:
        root_directory.close()
    except Exception as error:
        _log_pin_image_cleanup_failure(registry["image_id"], error)
    return result


def _delete_legacy_unreferenced_image(image_id, using):
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


def _delete_registered_unreferenced_image(image_id, registry, using):
    root_directory = open_media_root(settings.MEDIA_ROOT)
    try:
        clock = time.monotonic
        deadline = clock() + settings.PINRY_FETCH_TOTAL_TIMEOUT
        with media_dedup_lock(
            root_directory,
            registry["submitter_id"],
            registry["content_sha256"],
            deadline=deadline,
            clock=clock,
        ):
            with transaction.atomic(using=using):
                current_registry = (
                    MediaAsset.objects.select_for_update()
                    .using(using)
                    .filter(
                        pk=registry["pk"],
                        image_id=image_id,
                    )
                    .first()
                )
                if (
                    current_registry is None
                    or current_registry.image_id != registry["image_id"]
                    or current_registry.submitter_id
                    != registry["submitter_id"]
                    or current_registry.content_sha256
                    != registry["content_sha256"]
                ):
                    return
                image = (
                    BaseImage.objects.select_for_update()
                    .using(using)
                    .filter(pk=image_id)
                    .first()
                )
                if image is None:
                    return
                if Pin.objects.filter(
                    image_id=image_id
                ).using(using).exists():
                    return
                image.delete(using=using)
    finally:
        root_directory.close()
