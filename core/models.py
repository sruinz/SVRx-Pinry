import logging
import os
import threading
import time

from django.conf import settings
from django.db import connections, models, router, transaction
from django.dispatch import receiver
from django.utils.translation import gettext_lazy

from django_images.models import Image as BaseImage, Thumbnail
from django_images.file_ops import (
    media_dedup_lock,
    media_lifecycle_lock,
    open_media_root,
)
from django_images.paths import (
    FORMAT_EXTENSIONS,
    canonical_original_path,
    sanitize_original_filename,
)
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
        verbose_name = gettext_lazy("보드")
        verbose_name_plural = gettext_lazy("보드")
        unique_together = ("submitter", "name")
        indexes = [
            models.Index(fields=["submitter", "name"], name="board_owner_name_idx"),
            models.Index(
                fields=["submitter", "display_order", "id"],
                name="board_owner_order_id_idx",
            ),
        ]

    submitter = models.ForeignKey(User, on_delete=models.CASCADE)
    name = models.CharField(max_length=128, blank=False, null=False)
    private = models.BooleanField(default=False, blank=False)
    display_order = models.PositiveIntegerField(default=0)
    pins = models.ManyToManyField("Pin", related_name="pins", blank=True)
    cover_pin = models.ForeignKey(
        "Pin",
        null=True,
        blank=True,
        related_name="covering_boards",
        on_delete=models.SET_NULL,
    )

    published = models.DateTimeField(auto_now_add=True)


_pin_queryset_delete_state = threading.local()
_RETRY_REGISTERED_PIN_DELETE = object()


def _delete_pin_queryset_with_registry_guard(queryset):
    previous_depth = getattr(_pin_queryset_delete_state, "depth", 0)
    _pin_queryset_delete_state.depth = previous_depth + 1
    try:
        return models.QuerySet.delete(queryset)
    finally:
        if previous_depth:
            _pin_queryset_delete_state.depth = previous_depth
        else:
            del _pin_queryset_delete_state.depth


def _delete_pin_queryset_with_lifecycle_lock(queryset, using):
    root_directory = open_media_root(settings.MEDIA_ROOT)
    result = None
    try:
        clock = time.monotonic
        deadline = clock() + settings.PINRY_FETCH_TOTAL_TIMEOUT
        with transaction.atomic(using=using):
            with media_lifecycle_lock(
                root_directory,
                deadline=deadline,
                clock=clock,
            ):
                result = _delete_pin_queryset_with_registry_guard(
                    queryset.using(using)
                )
    except BaseException:
        try:
            root_directory.close()
        except BaseException:
            pass
        raise
    try:
        root_directory.close()
    except Exception as error:
        _log_pin_image_cleanup_failure(None, error)
    return result


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
        probe = self._chain()
        probe._for_write = True
        probe.query.select_for_update = False
        probe.query.select_related = False
        probe = probe.order_by()
        probe = probe.using(database_alias)
        pin_ids = list(
            probe
            .values_list("pk", flat=True)
            .distinct()[:2]
        )
        if not pin_ids:
            self._result_cache = None
            return 0, {}
        if len(pin_ids) != 1:
            result = _delete_pin_queryset_with_lifecycle_lock(
                self,
                database_alias,
            )
            self._result_cache = None
            return result
        candidates = (
            self.model._base_manager.using(database_alias)
            .filter(pk=pin_ids[0])
        )
        pin = candidates.first()
        if pin is None:
            self._result_cache = None
            return 0, {}
        result = pin.delete(using=database_alias)
        self._result_cache = None
        return result


class Pin(models.Model):
    class Meta:
        verbose_name = gettext_lazy("Pin")
        verbose_name_plural = gettext_lazy("Pin")

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

    def delete_if_exclusive_to_board(
        self,
        submitter_id,
        source_board_id,
        using=None,
        keep_parents=False,
    ):
        condition = {
            "submitter_id": submitter_id,
            "source_board_id": source_board_id,
        }
        return _delete_pin_with_registry_lock(
            self,
            using=using,
            keep_parents=keep_parents,
            exclusive_condition=condition,
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
    retryable = models.BooleanField(null=True, blank=True)
    lease_uuid = models.UUIDField(null=True, blank=True)
    lease_generation = models.PositiveIntegerField(default=0)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = (("submitter", "client_item_id"),)


@receiver(models.signals.pre_delete, sender=Pin)
def prevent_unmanaged_registered_pin_delete(sender, instance, using, **kwargs):
    if (
        not getattr(_pin_queryset_delete_state, "depth", 0)
        or getattr(instance, "_media_delete_managed", False)
    ):
        return
    if MediaAsset.objects.using(using).filter(
        image_id=instance.image_id
    ).exists():
        raise RuntimeError("registered_pin_bulk_delete_unsupported")


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


def _locked_exclusive_pin_status(pin, condition, using):
    from core.services.pin_membership import PinMembershipService

    source_board, current_pin = (
        PinMembershipService.lock_source_board_and_pin(
            condition["source_board_id"],
            pin.pk,
            using,
        )
    )
    if source_board is None or current_pin is None:
        return current_pin, ("preserved", "source_membership_changed")

    through = Board.pins.through
    source_membership = through.objects.using(using).filter(
        board_id=condition["source_board_id"],
        pin_id=current_pin.pk,
    )
    if not source_membership.exists():
        return current_pin, ("preserved", "source_membership_changed")
    if current_pin.submitter_id != condition["submitter_id"]:
        return current_pin, ("preserved", "non_owned_pin")

    board_ids = list(
        through.objects.using(using)
        .filter(pin_id=current_pin.pk)
        .order_by("board_id")
        .values_list("board_id", flat=True)[:2]
    )
    if len(board_ids) > 1:
        return current_pin, ("preserved", "shared_pin")
    return current_pin, None


def _require_pin_delete_autocommit(using):
    database = connections[using]
    if not database.get_autocommit() or database.in_atomic_block:
        raise RuntimeError("registered_pin_delete_requires_autocommit")


def _delete_legacy_pin_with_registry_lock(
    pin,
    using,
    keep_parents,
    exclusive_condition,
):
    if exclusive_condition is not None:
        _require_pin_delete_autocommit(using)
    root_directory = open_media_root(settings.MEDIA_ROOT)
    result = None
    deleted = False
    try:
        clock = time.monotonic
        deadline = clock() + settings.PINRY_FETCH_TOTAL_TIMEOUT
        with transaction.atomic(using=using):
            with media_lifecycle_lock(
                root_directory,
                deadline=deadline,
                clock=clock,
            ):
                registry = (
                    MediaAsset.objects.select_for_update()
                    .using(using)
                    .filter(image_id=pin.image_id)
                    .first()
                )
                if registry is not None:
                    result = _RETRY_REGISTERED_PIN_DELETE
                elif exclusive_condition is not None:
                    current_pin, condition_result = (
                        _locked_exclusive_pin_status(
                            pin,
                            exclusive_condition,
                            using,
                        )
                    )
                    if condition_result is not None:
                        result = condition_result
                    else:
                        super(Pin, current_pin).delete(
                            using=using,
                            keep_parents=keep_parents,
                        )
                        result = ("deleted", None)
                        deleted = True
                else:
                    result = super(Pin, pin).delete(
                        using=using,
                        keep_parents=keep_parents,
                    )
    except BaseException:
        try:
            root_directory.close()
        except BaseException:
            pass
        raise

    if deleted:
        pin.pk = None
    try:
        root_directory.close()
    except Exception as error:
        _log_pin_image_cleanup_failure(pin.image_id, error)
    return result


def _lock_registered_asset(registry, using):
    return (
        MediaAsset.objects.select_for_update()
        .using(using)
        .filter(
            pk=registry["pk"],
            image_id=registry["image_id"],
        )
        .first()
    )


class _RegisteredPinBoardSetChanged(Exception):
    pass


def _registered_pin_board_ids(pin_id, using):
    return list(
        Board.objects.using(using)
        .filter(
            models.Q(pins__pk=pin_id)
            | models.Q(cover_pin_id=pin_id)
        )
        .order_by("pk")
        .values_list("pk", flat=True)
        .distinct()
    )


def _lock_registered_pin_boards(pin_id, using):
    board_ids = _registered_pin_board_ids(pin_id, using)
    if not board_ids:
        return board_ids
    list(
        Board.objects.select_for_update()
        .using(using)
        .filter(pk__in=board_ids)
        .order_by("pk")
    )
    return board_ids


def _delete_locked_registered_pin(
    pin,
    registry,
    media_manifest,
    using,
    keep_parents,
    exclusive_condition,
):
    if exclusive_condition is not None:
        current_pin, condition_result = _locked_exclusive_pin_status(
            pin,
            exclusive_condition,
            using,
        )
        if condition_result is not None:
            return condition_result, False
        current_registry = _lock_registered_asset(registry, using)
    else:
        current_registry = _lock_registered_asset(registry, using)
        locked_board_ids = _lock_registered_pin_boards(pin.pk, using)
        current_pin = (
            Pin.objects.select_for_update()
            .using(using)
            .filter(pk=pin.pk, image_id=registry["image_id"])
            .order_by("pk")
            .first()
        )
        if (
            _registered_pin_board_ids(pin.pk, using)
            != locked_board_ids
        ):
            raise _RegisteredPinBoardSetChanged()
    image = (
        BaseImage.objects.select_for_update()
        .using(using)
        .filter(pk=registry["image_id"])
        .first()
    )
    current_manifest = _registered_media_manifest(
        image,
        using,
        lock_thumbnails=True,
    )
    if (
        current_registry is None
        or current_pin is None
        or current_pin.image_id != registry["image_id"]
        or image is None
        or current_registry.submitter_id != registry["submitter_id"]
        or current_registry.content_sha256 != registry["content_sha256"]
        or current_manifest != media_manifest
    ):
        raise RuntimeError("registered_media_identity_changed")

    current_pin._media_delete_managed = True
    delete_result = current_pin.delete(
        using=using,
        keep_parents=keep_parents,
    )
    if not Pin.objects.filter(
        image_id=registry["image_id"]
    ).using(using).exists():
        image.delete(using=using)
    if exclusive_condition is not None:
        return ("deleted", None), True
    return delete_result, True


def _delete_registered_pin_with_board_retry(
    pin,
    registry,
    media_manifest,
    using,
    keep_parents,
    exclusive_condition,
    commit_marker,
    deadline,
    clock,
):
    while True:
        if exclusive_condition is None and clock() >= deadline:
            raise RuntimeError(
                "registered_pin_delete_board_retry_timeout"
            )
        try:
            with transaction.atomic(using=using):
                transaction.on_commit(
                    lambda: commit_marker.__setitem__(0, True),
                    using=using,
                )
                return _delete_locked_registered_pin(
                    pin,
                    registry,
                    media_manifest,
                    using,
                    keep_parents,
                    exclusive_condition,
                )
        except _RegisteredPinBoardSetChanged:
            if clock() >= deadline:
                raise RuntimeError(
                    "registered_pin_delete_board_retry_timeout"
                )


def _delete_pin_with_registry_lock(
    pin,
    using=None,
    keep_parents=False,
    exclusive_condition=None,
):
    if pin.pk is None:
        if exclusive_condition is not None:
            return "preserved", "source_membership_changed"
        return super(Pin, pin).delete(
            using=using,
            keep_parents=keep_parents,
        )
    database_alias = using or router.db_for_write(Pin, instance=pin)
    while True:
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
        if registry is not None:
            break
        result = _delete_legacy_pin_with_registry_lock(
            pin,
            database_alias,
            keep_parents,
            exclusive_condition,
        )
        if result is not _RETRY_REGISTERED_PIN_DELETE:
            return result
    image_snapshot = (
        BaseImage.objects.using(database_alias)
        .filter(pk=registry["image_id"])
        .first()
    )
    media_manifest = _registered_media_manifest(
        image_snapshot,
        database_alias,
    )
    if media_manifest is None:
        raise RuntimeError("registered_media_identity_changed")

    _require_pin_delete_autocommit(database_alias)

    root_directory = open_media_root(settings.MEDIA_ROOT)
    result = None
    deleted = False
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
            result, deleted = _delete_registered_pin_with_board_retry(
                pin,
                registry,
                media_manifest,
                database_alias,
                keep_parents,
                exclusive_condition,
                commit_marker,
                deadline,
                clock,
            )
    except BaseException as error:
        try:
            root_directory.close()
        except BaseException:
            pass
        if commit_marker[0]:
            if deleted:
                pin.pk = None
            if isinstance(error, Exception):
                _log_pin_image_cleanup_failure(registry["image_id"], error)
                return result
        raise

    if deleted:
        pin.pk = None
    try:
        root_directory.close()
    except Exception as error:
        _log_pin_image_cleanup_failure(registry["image_id"], error)
    return result


def _delete_legacy_unreferenced_image(image_id, using):
    root_directory = open_media_root(settings.MEDIA_ROOT)
    try:
        clock = time.monotonic
        deadline = clock() + settings.PINRY_FETCH_TOTAL_TIMEOUT
        with transaction.atomic(using=using):
            with media_lifecycle_lock(
                root_directory,
                deadline=deadline,
                clock=clock,
            ):
                registry = (
                    MediaAsset.objects.select_for_update()
                    .using(using)
                    .filter(image_id=image_id)
                    .first()
                )
                if registry is not None:
                    return
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
    finally:
        root_directory.close()


def _delete_registered_unreferenced_image(image_id, registry, using):
    image_snapshot = (
        BaseImage.objects.using(using).filter(pk=image_id).first()
    )
    media_manifest = _registered_media_manifest(image_snapshot, using)
    if media_manifest is None:
        return
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
                if _registered_media_manifest(
                    image,
                    using,
                    lock_thumbnails=True,
                ) != media_manifest:
                    return
                if Pin.objects.filter(
                    image_id=image_id
                ).using(using).exists():
                    return
                image.delete(using=using)
    finally:
        root_directory.close()


def _registered_media_manifest(image, using, lock_thumbnails=False):
    if image is None:
        return None
    try:
        asset_uuid = str(image.asset_uuid)
        original_name = image.image.name
        original_filename = image.original_filename
        original_extension = os.path.splitext(original_name)[1]
        if (
            original_extension not in FORMAT_EXTENSIONS.values()
            or original_filename
            != sanitize_original_filename(original_filename)
            or original_name
            != canonical_original_path(
                asset_uuid,
                original_filename,
                original_extension,
            )
        ):
            return None
        thumbnails = Thumbnail.objects.using(using).filter(
            original_id=image.pk
        ).order_by("size")
        if lock_thumbnails:
            thumbnails = thumbnails.select_for_update()
        manifest = tuple(thumbnails.values_list(
            "size",
            "image",
            "width",
            "height",
        ))
        if tuple(entry[0] for entry in manifest) != (
            "square",
            "standard",
            "thumbnail",
        ):
            return None
        for size, name, width, height in manifest:
            extension = os.path.splitext(name)[1]
            if (
                extension not in FORMAT_EXTENSIONS.values()
                or name != "derivatives/{}/{}{}".format(
                    asset_uuid,
                    size,
                    extension,
                )
                or type(width) is not int
                or width <= 0
                or type(height) is not int
                or height <= 0
            ):
                return None
        return (
            image.asset_uuid,
            original_name,
            original_filename,
            manifest,
        )
    except (AttributeError, TypeError, ValueError):
        return None
