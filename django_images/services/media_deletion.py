import logging

from django.db.models import F
from django.utils import timezone

from django_images.models import Image, PendingMediaDeletion, Thumbnail


logger = logging.getLogger(__name__)


def _storage_for_kind(kind):
    if kind == PendingMediaDeletion.ORIGINAL:
        model = Image
    elif kind == PendingMediaDeletion.THUMBNAIL:
        model = Thumbnail
    else:
        raise ValueError("invalid_media_kind")
    return model._meta.get_field("image").storage


def process_pending_media_deletion(pending_id, using=None):
    pending = (
        PendingMediaDeletion.objects.using(using)
        .filter(pk=pending_id)
        .first()
    )
    if pending is None:
        return False

    try:
        storage = _storage_for_kind(pending.kind)
        if storage.exists(pending.name):
            storage.delete(pending.name)
    except Exception as error:
        error_name = error.__class__.__name__[:255]
        PendingMediaDeletion.objects.using(using).filter(
            pk=pending.pk
        ).update(
            attempts=F("attempts") + 1,
            last_error=error_name,
            updated_at=timezone.now(),
        )
        logger.warning(
            "media_deletion_failed",
            extra={
                "media_kind": pending.kind,
                "media_name": pending.name,
                "media_error": error_name,
            },
        )
        return False

    pending.delete(using=using)
    return True
