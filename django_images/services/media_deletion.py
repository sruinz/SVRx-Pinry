import logging
import os
import unicodedata
import uuid

from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.db.models import F, Q
from django.utils import timezone

from django_images.file_ops import (
    media_lifecycle_lock,
    open_media_root,
    remove_empty_media_directory,
    remove_media_file,
)
from django_images.models import Image, PendingMediaDeletion, Thumbnail
from django_images.paths import (
    DERIVATIVE_NAMES,
    FORMAT_EXTENSIONS,
    is_valid_original_leaf,
)


logger = logging.getLogger(__name__)


class InvalidMediaName(ValueError):
    pass


def _validate_storage_name(name):
    if not name or name.startswith("/") or "\\" in name:
        raise InvalidMediaName
    if any(unicodedata.category(character) == "Cc" for character in name):
        raise InvalidMediaName
    if any(component in ("", ".", "..") for component in name.split("/")):
        raise InvalidMediaName


def _storage_for_kind(kind):
    if kind == PendingMediaDeletion.ORIGINAL:
        model = Image
    elif kind == PendingMediaDeletion.THUMBNAIL:
        model = Thumbnail
    else:
        raise ValueError("invalid_media_kind")
    return model._meta.get_field("image").storage


def _canonical_asset_directory(kind, name):
    components = name.split("/")
    if len(components) != 3:
        return None
    root_name, asset_uuid_text, leaf = components
    try:
        asset_uuid = uuid.UUID(asset_uuid_text)
    except (AttributeError, TypeError, ValueError):
        return None
    if str(asset_uuid) != asset_uuid_text:
        return None
    if (
        kind == PendingMediaDeletion.ORIGINAL
        and root_name == "originals"
        and is_valid_original_leaf(asset_uuid_text, leaf)
    ):
        return asset_uuid, "originals/{}".format(asset_uuid_text)
    stem, extension = os.path.splitext(leaf)
    if (
        kind == PendingMediaDeletion.THUMBNAIL
        and root_name == "derivatives"
        and stem in DERIVATIVE_NAMES
        and extension in FORMAT_EXTENSIONS.values()
    ):
        return asset_uuid, "derivatives/{}".format(asset_uuid_text)
    return None


def _filesystem_media_root(storage):
    if not isinstance(storage, FileSystemStorage):
        return None
    storage_root = os.path.realpath(storage.location)
    configured_root = os.path.realpath(settings.MEDIA_ROOT)
    if storage_root != configured_root:
        return None
    return configured_root


def _media_path_is_referenced(name, using):
    return (
        Image.objects.using(using).filter(image=name).exists()
        or Thumbnail.objects.using(using).filter(image=name).exists()
    )


def _asset_uuid_is_referenced(asset_uuid, using):
    asset_uuid_text = str(asset_uuid)
    original_prefix = "originals/{}/".format(asset_uuid_text)
    derivative_prefix = "derivatives/{}/".format(asset_uuid_text)
    return (
        Image.objects.using(using).filter(
            Q(asset_uuid=asset_uuid)
            | Q(image__startswith=original_prefix)
            | Q(image__startswith=derivative_prefix)
        ).exists()
        or Thumbnail.objects.using(using).filter(
            Q(image__startswith=original_prefix)
            | Q(image__startswith=derivative_prefix)
        ).exists()
    )


def _delete_canonical_media(
    name,
    asset_uuid,
    relative_directory,
    media_root,
    using,
):
    root_directory = open_media_root(media_root)
    try:
        with media_lifecycle_lock(root_directory, exclusive=True):
            if _media_path_is_referenced(name, using):
                return
            remove_media_file(root_directory, name)
            if not _asset_uuid_is_referenced(asset_uuid, using):
                remove_empty_media_directory(
                    root_directory,
                    relative_directory,
                )
    finally:
        root_directory.close()


def _process_pending(pending, using):
    _validate_storage_name(pending.name)
    if _media_path_is_referenced(pending.name, using):
        return
    storage = _storage_for_kind(pending.kind)
    canonical = _canonical_asset_directory(pending.kind, pending.name)
    media_root = _filesystem_media_root(storage)
    if canonical is not None and media_root is not None:
        asset_uuid, relative_directory = canonical
        _delete_canonical_media(
            pending.name,
            asset_uuid,
            relative_directory,
            media_root,
            using,
        )
        return
    if storage.exists(pending.name):
        storage.delete(pending.name)


def _log_failure(error_name):
    try:
        logger.warning(
            "media_deletion_failed",
            extra={"media_error": error_name},
        )
    except Exception:
        pass


def _record_failure(pending_id, using, error_name):
    try:
        PendingMediaDeletion.objects.using(using).filter(
            pk=pending_id
        ).update(
            attempts=F("attempts") + 1,
            last_error=error_name,
            updated_at=timezone.now(),
        )
    except Exception as error:
        _log_failure(error.__class__.__name__[:255])


def process_pending_media_deletion(pending_id, using=None):
    try:
        pending = (
            PendingMediaDeletion.objects.using(using)
            .filter(pk=pending_id)
            .first()
        )
    except Exception as error:
        _log_failure(error.__class__.__name__[:255])
        return False
    if pending is None:
        return False

    try:
        _process_pending(pending, using)
        pending.delete(using=using)
    except Exception as error:
        error_name = error.__class__.__name__[:255]
        _record_failure(pending.pk, using, error_name)
        _log_failure(error_name)
        return False

    return True
