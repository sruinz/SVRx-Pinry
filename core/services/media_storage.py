from dataclasses import dataclass
from io import BytesIO
import logging
import os
from pathlib import PurePosixPath
import stat
import time
import uuid

from django.conf import settings
from django.core.files.images import ImageFile
from django.core.files.storage import FileSystemStorage
from PIL import Image as PILImage

from django_images.file_ops import (
    DescriptorCloseNotAttempted,
    MediaDirectory,
    MediaPathError,
    PublishFailure,
    create_owned_staging_file,
    media_dedup_lock,
    media_lifecycle_lock,
    open_media_root,
    open_or_create_media_directory_from,
    open_verified_media_root,
    publish_owned_noreplace,
    sha256_file_descriptor,
    unlink_published_name_if_current,
    verify_published_identity,
    verify_published_name,
    _close_descriptor,
    _identity,
    _open_child_directory_nofollow,
    _open_regular_nofollow,
)
from django_images.paths import (
    FORMAT_EXTENSIONS,
    canonical_original_path,
    sanitize_original_filename,
)
from django_images.models import Image, Thumbnail
from django_images.services.migration_batch_log import FileReceipt
from django_images.utils import scale_and_crop_iter, write_image_to_file


_KINDS = ("original", "thumbnail", "standard", "square")
_DERIVATIVE_KINDS = _KINDS[1:]
logger = logging.getLogger(__name__)


def _log_cleanup_incomplete(
    event,
    asset_uuid,
    run_uuid,
    kind,
    error_type,
    include_identifiers=True,
):
    try:
        if include_identifiers:
            logger.warning(
                "media_storage_cleanup_incomplete event=%s asset_uuid=%s "
                "run_uuid=%s kind=%s error_type=%s",
                event,
                str(asset_uuid),
                str(run_uuid),
                kind,
                error_type,
            )
        else:
            logger.warning(
                "media_storage_cleanup_incomplete event=%s kind=%s "
                "error_type=%s",
                event,
                kind,
                error_type,
            )
    except BaseException:
        pass


class MediaStorageError(Exception):
    def __init__(self, code, message, retryable):
        super(MediaStorageError, self).__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable

    def as_dict(self):
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }


@dataclass(frozen=True)
class PreparedFile:
    kind: str
    owned_staging_handle: object
    final_relative_path: str
    size: int
    sha256: str
    width: int
    height: int
    image_format: str


class PreparedAsset(object):
    def __init__(
        self,
        asset_uuid,
        original_filename,
        run_uuid,
        files,
        root_directory,
        staging_directory,
        include_cleanup_identifiers=True,
    ):
        self.asset_uuid = asset_uuid
        self.original_filename = original_filename
        self.run_uuid = run_uuid
        self.files = tuple(files)
        self.root_directory = root_directory
        self.staging_directory = staging_directory
        self._include_cleanup_identifiers = include_cleanup_identifiers
        self._cleaned = False

    def _log_cleanup(self, event, kind, error_type):
        _log_cleanup_incomplete(
            event,
            self.asset_uuid,
            self.run_uuid,
            kind,
            error_type,
            include_identifiers=self._include_cleanup_identifiers,
        )

    @property
    def is_open(self):
        return not self._cleaned

    @property
    def content_sha256(self):
        try:
            return next(
                entry.sha256
                for entry in self.files
                if entry.kind == "original"
            )
        except StopIteration:
            raise _media_conflict() from None

    def cleanup(self):
        if self._cleaned:
            return
        for prepared_file in reversed(self.files):
            try:
                removed = prepared_file.owned_staging_handle.cleanup()
                if removed is False:
                    self._log_cleanup(
                        "prepare_file_preserved",
                        prepared_file.kind,
                        "IdentityMismatch",
                    )
            except BaseException as error:
                self._log_cleanup(
                    "prepare_file_cleanup_failed",
                    prepared_file.kind,
                    type(error).__name__,
                )
        for prepared_file in reversed(self.files):
            try:
                prepared_file.owned_staging_handle.close()
            except BaseException as error:
                self._log_cleanup(
                    "prepare_file_close_failed",
                    prepared_file.kind,
                    type(error).__name__,
                )
        try:
            reason = self.staging_directory.remove_created_suffix(1)
            if reason is not None:
                self._log_cleanup(
                    "prepare_directory_preserved",
                    "staging",
                    reason,
                )
        except BaseException as error:
            self._log_cleanup(
                "prepare_directory_cleanup_failed",
                "staging",
                type(error).__name__,
            )
        try:
            self.staging_directory.close()
        except BaseException as error:
            self._log_cleanup(
                "prepare_directory_close_failed",
                "staging",
                type(error).__name__,
            )
        try:
            self.root_directory.close()
        except BaseException as error:
            self._log_cleanup(
                "prepare_directory_close_failed",
                "root",
                type(error).__name__,
            )
        self._cleaned = (
            all(
                prepared_file.owned_staging_handle._closed
                for prepared_file in self.files
            )
            and not self.staging_directory.descriptors
            and not self.root_directory.descriptors
        )


@dataclass(frozen=True)
class PublishedFile:
    kind: str
    final_relative_path: str
    size: int
    sha256: str
    width: int
    height: int
    image_format: str
    created: bool
    reused: bool
    destination_stat: object
    destination_directory: object
    destination_name: str


class PublishedAsset(object):
    def __init__(
        self,
        prepared,
        files,
        destination_directories,
        clock=time.monotonic,
        deadline=None,
    ):
        self.prepared = prepared
        self.asset_uuid = prepared.asset_uuid
        self.original_filename = prepared.original_filename
        self.files = tuple(files)
        self.destination_directories = tuple(destination_directories)
        self.clock = clock
        self.deadline = deadline
        self._released = False

    @property
    def original_path(self):
        return next(
            entry.final_relative_path
            for entry in self.files
            if entry.kind == "original"
        )

    @property
    def derivative_paths(self):
        return tuple(
            entry.final_relative_path
            for entry in self.files
            if entry.kind != "original"
        )

    def verify_current(self):
        if self._released:
            raise _publish_changed()
        try:
            for published_file in self.files:
                self._check_deadline()
                verify_published_name(
                    published_file.destination_directory,
                    published_file.destination_name,
                    published_file.destination_stat,
                    published_file.sha256,
                )
                self._check_deadline()
            for published_file in self.files:
                self._check_deadline()
                verify_published_identity(
                    published_file.destination_directory,
                    published_file.destination_name,
                    published_file.destination_stat,
                )
                self._check_deadline()
        except (MediaPathError, OSError):
            raise _publish_changed() from None
        return True

    def _check_deadline(self):
        if self.deadline is not None and self.clock() >= self.deadline:
            raise _processing_timeout()

    def compensate(self):
        if self._released:
            return
        for published_file in reversed(self.files):
            if not published_file.created:
                continue
            try:
                removed = unlink_published_name_if_current(
                    published_file.destination_directory,
                    published_file.destination_name,
                    published_file.destination_stat,
                    missing_ok=True,
                )
                if not removed:
                    self.prepared._log_cleanup(
                        "compensate_file_preserved",
                        published_file.kind,
                        "IdentityMismatch",
                    )
            except BaseException as error:
                self.prepared._log_cleanup(
                    "compensate_file_cleanup_failed",
                    published_file.kind,
                    type(error).__name__,
                )
        for index, directory in enumerate(
            reversed(self.destination_directories)
        ):
            kind = "derivatives" if index == 0 else "originals"
            try:
                reason = directory.remove_created_suffix(1)
                if reason is not None:
                    self.prepared._log_cleanup(
                        "compensate_directory_preserved",
                        kind,
                        reason,
                    )
            except BaseException as error:
                self.prepared._log_cleanup(
                    "compensate_directory_cleanup_failed",
                    kind,
                    type(error).__name__,
                )
        self._finish()

    def release(self):
        if self._released:
            return
        self._finish()

    def _finish(self):
        if self._released:
            return
        try:
            self.prepared.cleanup()
        except BaseException as error:
            self.prepared._log_cleanup(
                "prepared_cleanup_failed",
                "staging",
                type(error).__name__,
            )
        for index, directory in enumerate(
            reversed(self.destination_directories)
        ):
            kind = "derivatives" if index == 0 else "originals"
            try:
                directory.close()
            except BaseException as error:
                self.prepared._log_cleanup(
                    "published_directory_close_failed",
                    kind,
                    type(error).__name__,
                )
        self._released = (
            self.prepared._cleaned
            and all(
                not directory.descriptors
                for directory in self.destination_directories
            )
        )


@dataclass(frozen=True)
class ReusableFile:
    descriptor: int
    file_stat: object
    directory: object
    name: str
    sha256: str


class ReusableAsset(object):
    def __init__(self, files, directories, clock, deadline):
        self.files = list(files)
        self.directories = list(directories)
        self.clock = clock
        self.deadline = deadline
        self._released = False

    def verify_current(self):
        if self._released:
            raise _media_conflict()
        try:
            for _verification_pass in range(2):
                for reusable_file in self.files:
                    self._check_deadline()
                    reusable_file.directory.verify_current()
                    descriptor_stat = os.fstat(reusable_file.descriptor)
                    named_stat = os.stat(
                        reusable_file.name,
                        dir_fd=reusable_file.directory.descriptor,
                        follow_symlinks=False,
                    )
                    if (
                        not stat.S_ISREG(descriptor_stat.st_mode)
                        or not stat.S_ISREG(named_stat.st_mode)
                        or _identity(descriptor_stat)
                        != _identity(reusable_file.file_stat)
                        or _identity(named_stat)
                        != _identity(reusable_file.file_stat)
                        or descriptor_stat.st_size
                        != reusable_file.file_stat.st_size
                        or named_stat.st_size
                        != reusable_file.file_stat.st_size
                    ):
                        raise _media_conflict()
                    digest = sha256_file_descriptor(
                        reusable_file.descriptor
                    )
                    reusable_file.directory.verify_current()
                    current_descriptor_stat = os.fstat(
                        reusable_file.descriptor
                    )
                    current_named_stat = os.stat(
                        reusable_file.name,
                        dir_fd=reusable_file.directory.descriptor,
                        follow_symlinks=False,
                    )
                    if (
                        not stat.S_ISREG(current_descriptor_stat.st_mode)
                        or not stat.S_ISREG(current_named_stat.st_mode)
                        or _identity(current_descriptor_stat)
                        != _identity(reusable_file.file_stat)
                        or _identity(current_named_stat)
                        != _identity(reusable_file.file_stat)
                        or current_descriptor_stat.st_size
                        != reusable_file.file_stat.st_size
                        or current_named_stat.st_size
                        != reusable_file.file_stat.st_size
                        or digest != reusable_file.sha256
                    ):
                        raise _media_conflict()
                    self._check_deadline()
        except MediaStorageError:
            raise
        except (MediaPathError, OSError):
            raise _media_conflict() from None
        return True

    def release(self):
        if self._released:
            return
        first_error = None
        retained_files = []
        for reusable_file in reversed(self.files):
            try:
                _close_descriptor(reusable_file.descriptor)
            except DescriptorCloseNotAttempted as error:
                retained_files.append(reusable_file)
                if first_error is None:
                    first_error = error
            except BaseException as error:
                if first_error is None:
                    first_error = error
        self.files = list(reversed(retained_files))
        retained_directories = []
        for directory in reversed(self.directories):
            try:
                directory.close()
            except BaseException as error:
                if directory.descriptors:
                    retained_directories.append(directory)
                if first_error is None:
                    first_error = error
        self.directories = list(reversed(retained_directories))
        self._released = not self.files and not self.directories
        if first_error is not None:
            raise first_error

    def _check_deadline(self):
        if self.deadline is not None and self.clock() >= self.deadline:
            raise _processing_timeout()


@dataclass(frozen=True)
class _ReceiptStatFile:
    receipt: object
    directory: object
    name: str
    file_stat: object


class _ReceiptStatResource(object):
    def __init__(self, root_directory, files, directories):
        self._root_directory = root_directory
        self._files = tuple(files)
        self._directories = tuple(directories)
        self._closed = False

    def verify_current(self):
        if self._closed:
            raise _media_conflict()
        try:
            self._root_directory.verify_current()
            for item in self._files:
                item.directory.verify_current()
                current = os.stat(
                    item.name,
                    dir_fd=item.directory.descriptor,
                    follow_symlinks=False,
                )
                expected = item.receipt
                if (
                    not stat.S_ISREG(current.st_mode)
                    or current.st_nlink != 1
                    or (current.st_dev, current.st_ino)
                    != (
                        expected.destination_device,
                        expected.destination_inode,
                    )
                    or current.st_size != expected.size
                    or _identity(current) != _identity(item.file_stat)
                ):
                    raise _media_conflict()
            self._root_directory.verify_current()
        except MediaStorageError:
            raise
        except (MediaPathError, OSError):
            raise _media_conflict() from None
        return True

    def close(self):
        if self._closed:
            return
        first_error = None
        for directory in reversed(self._directories):
            try:
                directory.close()
            except BaseException as error:
                if first_error is None:
                    first_error = error
        try:
            self._root_directory.close()
        except BaseException as error:
            if first_error is None:
                first_error = error
        self._closed = (
            not self._root_directory.descriptors
            and all(not directory.descriptors for directory in self._directories)
        )
        if first_error is not None:
            raise first_error


class MediaStorage(object):
    def __init__(
        self,
        media_root=None,
        clock=time.monotonic,
        fault_injector=None,
    ):
        self.media_root = media_root or settings.MEDIA_ROOT
        self.clock = clock
        self.fault_injector = fault_injector

    def lifecycle_lock(self, prepared, deadline=None, clock=None):
        if not isinstance(prepared, PreparedAsset) or not prepared.is_open:
            raise _media_conflict()
        return media_lifecycle_lock(
            prepared.root_directory,
            exclusive=False,
            deadline=deadline,
            clock=self.clock if clock is None else clock,
        )

    def dedup_lock(
        self,
        prepared,
        submitter_id,
        content_sha256,
        deadline=None,
        clock=None,
    ):
        if (
            not isinstance(prepared, PreparedAsset)
            or not prepared.is_open
            or prepared.content_sha256 != content_sha256
        ):
            raise _media_conflict()
        return media_dedup_lock(
            prepared.root_directory,
            submitter_id,
            content_sha256,
            deadline=deadline,
            clock=self.clock if clock is None else clock,
        )

    def verify_reusable(
        self,
        prepared,
        image,
        thumbnails,
        deadline=None,
    ):
        directories = []
        reusable_files = []
        try:
            self._validate_prepared(prepared, deadline)
            prepared_files = self._verified_prepared_files(
                prepared,
                deadline,
            )
            asset_uuid = self._asset_uuid(image.asset_uuid)
            original_filename = image.original_filename
            if (
                not isinstance(original_filename, str)
                or original_filename
                != sanitize_original_filename(original_filename)
            ):
                raise _media_conflict()
            manifest = self._reusable_manifest(image, thumbnails)
            originals_directory = self._open_existing_directory(
                prepared.root_directory,
                "originals/{}".format(asset_uuid),
            )
            directories.append(originals_directory)
            derivatives_directory = self._open_existing_directory(
                prepared.root_directory,
                "derivatives/{}".format(asset_uuid),
            )
            directories.append(derivatives_directory)

            for kind in _KINDS:
                self._check_deadline(deadline)
                prepared_file = prepared_files[kind]
                entry = manifest[kind]
                directory = (
                    originals_directory
                    if kind == "original"
                    else derivatives_directory
                )
                path_parts = entry["path"].split("/")
                expected_parent = (
                    "originals" if kind == "original" else "derivatives"
                )
                if path_parts[:2] != [expected_parent, str(asset_uuid)]:
                    raise _media_conflict()
                descriptor = _open_regular_nofollow(
                    directory.descriptor,
                    path_parts[2],
                )
                reusable_file = ReusableFile(
                    descriptor=descriptor,
                    file_stat=None,
                    directory=directory,
                    name=path_parts[2],
                    sha256=None,
                )
                reusable_files.append(reusable_file)
                file_stat = os.fstat(descriptor)
                named_stat = os.stat(
                    path_parts[2],
                    dir_fd=directory.descriptor,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISREG(file_stat.st_mode)
                    or not stat.S_ISREG(named_stat.st_mode)
                    or _identity(file_stat) != _identity(named_stat)
                    or prepared_file["size"] != file_stat.st_size
                    or prepared_file["size"] != named_stat.st_size
                ):
                    raise _media_conflict()
                digest = sha256_file_descriptor(descriptor)
                if (
                    prepared_file["sha256"] != digest
                    or not self._same_descriptor_bytes(
                        prepared_file["descriptor"], descriptor
                    )
                ):
                    raise _media_conflict()
                image_format, width, height = self._inspect(descriptor)
                directory.verify_current()
                current_file_stat = os.fstat(descriptor)
                current_named_stat = os.stat(
                    path_parts[2],
                    dir_fd=directory.descriptor,
                    follow_symlinks=False,
                )
                if kind == "original":
                    expected_path = canonical_original_path(
                        asset_uuid,
                        original_filename,
                        FORMAT_EXTENSIONS[image_format],
                    )
                else:
                    expected_path = "derivatives/{}/{}{}".format(
                        asset_uuid,
                        kind,
                        FORMAT_EXTENSIONS[image_format],
                    )
                if (
                    entry["path"] != expected_path
                    or type(entry["width"]) is not int
                    or type(entry["height"]) is not int
                    or (entry["width"], entry["height"])
                    != (width, height)
                    or prepared_file["format"] != image_format
                    or prepared_file["dimensions"] != (width, height)
                    or _identity(current_file_stat) != _identity(file_stat)
                    or _identity(current_named_stat) != _identity(file_stat)
                    or current_file_stat.st_size != file_stat.st_size
                    or current_named_stat.st_size != file_stat.st_size
                ):
                    raise _media_conflict()
                reusable_files[-1] = ReusableFile(
                    descriptor=descriptor,
                    file_stat=file_stat,
                    directory=directory,
                    name=path_parts[2],
                    sha256=digest,
                )
                self._check_deadline(deadline)
            reusable = ReusableAsset(
                reusable_files,
                directories,
                self.clock,
                deadline,
            )
            return reusable
        except BaseException as error:
            partial = ReusableAsset(
                reusable_files,
                directories,
                self.clock,
                deadline,
            )
            for _attempt in range(2):
                try:
                    partial.release()
                except BaseException:
                    pass
                if partial._released:
                    break
            if not isinstance(error, Exception):
                raise
            if isinstance(error, MediaStorageError):
                raise
            raise _media_conflict() from None

    def prepare_from_receipts(
        self,
        image,
        thumbnails,
        expected_receipts,
        database_signature,
    ):
        try:
            self._validate_storage_configuration()
            manifest = self._reusable_manifest(image, thumbnails)
            thumbnail_list = sorted(
                list(thumbnails), key=lambda value: (value.size, value.pk)
            )
            current_signature = (
                image.pk,
                str(image.asset_uuid),
                image.original_filename,
                image.image.name,
                image.width,
                image.height,
                tuple(
                    (
                        thumbnail.pk,
                        thumbnail.size,
                        thumbnail.image.name,
                        thumbnail.width,
                        thumbnail.height,
                    )
                    for thumbnail in thumbnail_list
                ),
            )
            if current_signature != database_signature:
                raise _media_conflict()
            receipts = tuple(expected_receipts)
            if not all(isinstance(receipt, FileReceipt) for receipt in receipts):
                raise _media_conflict()
            expected_keys = {
                "original:{}".format(image.pk): "original"
            }
            expected_keys.update({
                "thumbnail:{}:{}".format(image.pk, thumbnail.pk): (
                    thumbnail.size
                )
                for thumbnail in thumbnail_list
            })
            by_key = {
                receipt.file_key: receipt for receipt in receipts
            }
            if (
                len(receipts) != len(by_key)
                or set(by_key) != set(expected_keys)
            ):
                raise _media_conflict()
            ordered_receipts = []
            for file_key, kind in sorted(expected_keys.items()):
                receipt = by_key[file_key]
                entry = manifest[kind]
                extension = FORMAT_EXTENSIONS.get(receipt.image_format)
                if (
                    receipt.relative_path != entry["path"]
                    or receipt.width != entry["width"]
                    or receipt.height != entry["height"]
                    or extension is None
                    or not receipt.relative_path.endswith(extension)
                ):
                    raise _media_conflict()
                ordered_receipts.append(receipt)
            return self._prepare_receipt_stat_resource(ordered_receipts)
        except BaseException as error:
            if not isinstance(error, Exception):
                raise
            if isinstance(error, MediaStorageError):
                raise
            raise _media_conflict() from None

    def verify_receipts_current(self, expected_receipts):
        resource = None
        try:
            self._validate_storage_configuration()
            receipts = tuple(expected_receipts)
            if (
                not receipts
                or not all(
                    isinstance(receipt, FileReceipt)
                    for receipt in receipts
                )
                or len({receipt.file_key for receipt in receipts})
                != len(receipts)
                or len({receipt.relative_path for receipt in receipts})
                != len(receipts)
            ):
                raise _media_conflict()
            resource = self._prepare_receipt_stat_resource(receipts)
            return resource.verify_current()
        finally:
            if resource is not None:
                resource.close()

    def _prepare_receipt_stat_resource(self, receipts):
        root_directory = None
        directories = []
        try:
            root_directory = open_verified_media_root(self.media_root)
            directory_by_path = {}
            files = []
            for receipt in receipts:
                path = PurePosixPath(receipt.relative_path)
                parts = path.parts
                expected_parent = (
                    "originals"
                    if receipt.file_key.startswith("original:")
                    else "derivatives"
                )
                try:
                    canonical_uuid = str(uuid.UUID(parts[1]))
                except (IndexError, TypeError, ValueError):
                    raise _media_conflict() from None
                if (
                    len(parts) != 3
                    or str(path) != receipt.relative_path
                    or parts[0] != expected_parent
                    or parts[1] != canonical_uuid
                    or not parts[2]
                ):
                    raise _media_conflict()
                relative_directory, name = receipt.relative_path.rsplit(
                    "/", 1
                )
                directory = directory_by_path.get(relative_directory)
                if directory is None:
                    directory = self._open_existing_directory(
                        root_directory, relative_directory
                    )
                    directory_by_path[relative_directory] = directory
                    directories.append(directory)
                current = os.stat(
                    name,
                    dir_fd=directory.descriptor,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISREG(current.st_mode)
                    or current.st_nlink != 1
                    or (current.st_dev, current.st_ino)
                    != (
                        receipt.destination_device,
                        receipt.destination_inode,
                    )
                    or current.st_size != receipt.size
                ):
                    raise _media_conflict()
                files.append(_ReceiptStatFile(
                    receipt, directory, name, current
                ))
            resource = _ReceiptStatResource(
                root_directory, files, directories
            )
            root_directory = None
            directories = []
            resource.verify_current()
            return resource
        except BaseException as error:
            for directory in reversed(directories):
                try:
                    directory.close()
                except BaseException:
                    pass
            if root_directory is not None:
                try:
                    root_directory.close()
                except BaseException:
                    pass
            if not isinstance(error, Exception):
                raise
            if isinstance(error, MediaStorageError):
                raise
            raise _media_conflict() from None

    def prepare(
        self,
        fetched,
        asset_uuid,
        original_filename,
        deadline=None,
    ):
        try:
            self._validate_storage_configuration()
            inputs = self._prepare_inputs(
                fetched,
                asset_uuid,
                original_filename,
                deadline,
            )
        except MediaStorageError:
            raise
        except Exception:
            raise _processing_failed() from None
        try:
            root_directory = open_media_root(self.media_root)
        except BaseException as error:
            if not isinstance(error, Exception):
                raise
            raise self._map_error(error) from None
        return self._prepare_with_root(
            root_directory,
            fetched,
            inputs,
            deadline,
        )

    def prepare_from_root(
        self,
        root_directory,
        fetched,
        asset_uuid,
        original_filename,
        deadline=None,
    ):
        owned_root = None
        try:
            self._validate_storage_configuration_from_root(root_directory)
            inputs = self._prepare_inputs(
                fetched,
                asset_uuid,
                original_filename,
                deadline,
            )
            owned_root = root_directory.duplicate_owned()
            return self._prepare_with_root(
                owned_root,
                fetched,
                inputs,
                deadline,
                include_cleanup_identifiers=False,
            )
        except BaseException as error:
            if owned_root is not None and owned_root.descriptors:
                try:
                    owned_root.close()
                except BaseException:
                    pass
            if not isinstance(error, Exception):
                raise
            raise self._map_error(error) from None

    def _prepare_inputs(
        self,
        fetched,
        asset_uuid,
        original_filename,
        deadline,
    ):
        asset_uuid = self._asset_uuid(asset_uuid)
        image_format = self._image_format(fetched)
        derivative_options = self._derivative_options()
        if not isinstance(original_filename, str):
            raise _processing_failed()
        original_filename = sanitize_original_filename(original_filename)
        self._check_deadline(deadline)
        return (
            asset_uuid,
            image_format,
            derivative_options,
            original_filename,
        )

    def _prepare_with_root(
        self,
        root_directory,
        fetched,
        inputs,
        deadline,
        include_cleanup_identifiers=True,
    ):
        (
            asset_uuid,
            image_format,
            derivative_options,
            original_filename,
        ) = inputs
        run_uuid = uuid.uuid4()
        staging_directory = None
        prepared_files = []
        active_kind = "staging"
        try:
            root_directory.verify_current()
            staging_directory = open_or_create_media_directory_from(
                root_directory,
                ".staging/{}/{}".format(run_uuid, asset_uuid),
            )
            active_kind = "original"
            extension = FORMAT_EXTENSIONS[image_format]
            original_path = canonical_original_path(
                asset_uuid,
                original_filename,
                extension,
            )
            original = self._stage_bytes(
                staging_directory,
                "original",
                fetched.content,
                original_path,
                image_format,
                deadline,
            )
            prepared_files.append(original)
            if (original.width, original.height) != (
                fetched.width,
                fetched.height,
            ):
                raise _processing_failed()
            self._fault("after_original_write")
            self._check_deadline(deadline)

            source = ImageFile(BytesIO(fetched.content), "source")
            derivatives = scale_and_crop_iter(
                source,
                [options for _kind, options in derivative_options],
            )
            try:
                for kind, _options in derivative_options:
                    active_kind = kind
                    self._check_deadline(deadline)
                    self._fault("before_{}_resize".format(kind))
                    derivative = next(derivatives)
                    try:
                        self._check_deadline(deadline)
                        prepared_files.append(self._stage_image(
                            staging_directory,
                            kind,
                            derivative,
                            "derivatives/{}/{}{}".format(
                                asset_uuid,
                                kind,
                                extension,
                            ),
                            image_format,
                            deadline,
                        ))
                    finally:
                        derivative.close()
                    self._fault("after_{}_save".format(kind))
                    self._check_deadline(deadline)
            finally:
                derivatives.close()

            return PreparedAsset(
                asset_uuid=asset_uuid,
                original_filename=original_filename,
                run_uuid=run_uuid,
                files=prepared_files,
                root_directory=root_directory,
                staging_directory=staging_directory,
                include_cleanup_identifiers=include_cleanup_identifiers,
            )
        except BaseException as error:
            if isinstance(error, (MediaPathError, OSError)):
                _log_cleanup_incomplete(
                    "prepare_failed",
                    asset_uuid,
                    run_uuid,
                    active_kind,
                    type(error).__name__,
                    include_identifiers=include_cleanup_identifiers,
                )
            self._cleanup_partial_prepare(
                prepared_files,
                staging_directory,
                root_directory,
                asset_uuid,
                run_uuid,
                include_cleanup_identifiers,
            )
            if not isinstance(error, Exception):
                raise
            raise self._map_error(error) from None

    def publish(self, prepared, deadline=None):
        if not isinstance(prepared, PreparedAsset) or not prepared.is_open:
            raise _media_conflict()
        originals_directory = None
        derivatives_directory = None
        published_files = []
        try:
            self._validate_prepared(prepared, deadline)
            self._check_deadline(deadline)
            originals_directory = open_or_create_media_directory_from(
                prepared.root_directory,
                "originals/{}".format(prepared.asset_uuid),
            )
            derivatives_directory = open_or_create_media_directory_from(
                prepared.root_directory,
                "derivatives/{}".format(prepared.asset_uuid),
            )
            destination_directories = (
                originals_directory,
                derivatives_directory,
            )
            for prepared_file in prepared.files:
                self._check_deadline(deadline)
                directory = (
                    originals_directory
                    if prepared_file.kind == "original"
                    else derivatives_directory
                )
                destination_name = PurePosixPath(
                    prepared_file.final_relative_path
                ).name
                try:
                    result = publish_owned_noreplace(
                        prepared_file.owned_staging_handle,
                        directory,
                        destination_name,
                        prepared_file.sha256,
                    )
                except PublishFailure as error:
                    published_files.append(self._published_file(
                        prepared_file,
                        directory,
                        destination_name,
                        error.result,
                    ))
                    raise error.cause
                published_files.append(self._published_file(
                    prepared_file,
                    directory,
                    destination_name,
                    result,
                ))
                self._fault("after_{}_publish".format(prepared_file.kind))
                self._check_deadline(deadline)
            published = PublishedAsset(
                prepared,
                published_files,
                destination_directories,
                clock=self.clock,
                deadline=deadline,
            )
            published.verify_current()
            return published
        except BaseException as error:
            directories = tuple(
                directory for directory in (
                    originals_directory,
                    derivatives_directory,
                ) if directory is not None
            )
            partial = PublishedAsset(
                prepared,
                published_files,
                directories,
                clock=self.clock,
                deadline=deadline,
            )
            partial.compensate()
            if not isinstance(error, Exception):
                raise
            raise self._map_error(error) from None

    def _verified_prepared_files(self, prepared, deadline):
        verified = {}
        for prepared_file in prepared.files:
            self._check_deadline(deadline)
            descriptor = prepared_file.owned_staging_handle.descriptor
            file_stat = os.fstat(descriptor)
            image_format, width, height = self._inspect(descriptor)
            digest = sha256_file_descriptor(descriptor)
            if (
                file_stat.st_size != prepared_file.size
                or digest != prepared_file.sha256
                or image_format != prepared_file.image_format
                or (width, height)
                != (prepared_file.width, prepared_file.height)
            ):
                raise _media_conflict()
            verified[prepared_file.kind] = {
                "descriptor": descriptor,
                "size": file_stat.st_size,
                "sha256": digest,
                "format": image_format,
                "dimensions": (width, height),
            }
            self._check_deadline(deadline)
        return verified

    def _validate_storage_configuration(self):
        try:
            configured_root = os.path.realpath(settings.MEDIA_ROOT)
            if os.path.realpath(self.media_root) != configured_root:
                raise _configuration_error()
            for model in (Image, Thumbnail):
                storage = model._meta.get_field("image").storage
                if (
                    storage.__class__ is not FileSystemStorage
                    or os.path.realpath(storage.location)
                    != configured_root
                ):
                    raise _configuration_error()
        except MediaStorageError:
            raise
        except Exception:
            raise _configuration_error() from None

    def _validate_storage_configuration_from_root(self, root_directory):
        try:
            if (
                not isinstance(root_directory, MediaDirectory)
                or not root_directory.verified_root
            ):
                raise _configuration_error()
            root_directory.verify_current()
            configured_root = os.path.abspath(settings.MEDIA_ROOT)
            if (
                os.path.abspath(self.media_root) != configured_root
                or os.path.abspath(root_directory.root_path)
                != configured_root
            ):
                raise _configuration_error()
            for model in (Image, Thumbnail):
                storage = model._meta.get_field("image").storage
                if (
                    storage.__class__ is not FileSystemStorage
                    or os.path.abspath(storage.location)
                    != configured_root
                ):
                    raise _configuration_error()
        except MediaStorageError:
            raise
        except (AttributeError, MediaPathError, OSError):
            raise _media_conflict() from None
        except Exception:
            raise _configuration_error() from None

    @staticmethod
    def _reusable_manifest(image, thumbnails):
        try:
            original_path = image.image.name
            manifest = {
                "original": {
                    "path": original_path,
                    "width": image.width,
                    "height": image.height,
                }
            }
            thumbnail_list = list(thumbnails)
            if len(thumbnail_list) != len(_DERIVATIVE_KINDS):
                raise ValueError()
            for thumbnail in thumbnail_list:
                if (
                    thumbnail.original_id != image.pk
                    or thumbnail.size not in _DERIVATIVE_KINDS
                    or thumbnail.size in manifest
                ):
                    raise ValueError()
                manifest[thumbnail.size] = {
                    "path": thumbnail.image.name,
                    "width": thumbnail.width,
                    "height": thumbnail.height,
                }
            if tuple(sorted(manifest)) != tuple(sorted(_KINDS)):
                raise ValueError()
            for entry in manifest.values():
                path = entry["path"]
                if (
                    not isinstance(path, str)
                    or len(path.split("/")) != 3
                ):
                    raise ValueError()
            return manifest
        except (AttributeError, TypeError, ValueError):
            raise _media_conflict() from None

    @staticmethod
    def _open_existing_directory(root_directory, relative_directory):
        root_directory.verify_current()
        descriptors = [os.dup(root_directory.descriptor)]
        names = []
        directory_stats = []
        try:
            for name in relative_directory.split("/"):
                parent_descriptor = descriptors[-1]
                named_stat = os.stat(
                    name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                if not stat.S_ISDIR(named_stat.st_mode):
                    raise MediaPathError("unsafe_media_directory")
                child_descriptor = _open_child_directory_nofollow(
                    parent_descriptor,
                    name,
                    named_stat,
                )
                names.append(name)
                directory_stats.append(os.fstat(child_descriptor))
                descriptors.append(child_descriptor)
            return MediaDirectory(
                descriptors,
                names=names,
                directory_stats=directory_stats,
                created=[False for _name in names],
                root_path=root_directory.root_path,
                root_stat=root_directory.root_stat,
            )
        except BaseException:
            partial = MediaDirectory(
                descriptors,
                names=names,
                directory_stats=directory_stats,
                created=[False for _name in names],
                root_path=root_directory.root_path,
                root_stat=root_directory.root_stat,
            )
            try:
                partial.close()
            except BaseException:
                pass
            raise

    @staticmethod
    def _same_descriptor_bytes(first_descriptor, second_descriptor):
        offset = 0
        while True:
            first = os.pread(first_descriptor, 64 * 1024, offset)
            second = os.pread(second_descriptor, 64 * 1024, offset)
            if first != second:
                return False
            if not first:
                return True
            offset += len(first)

    def _stage_bytes(
        self,
        directory,
        kind,
        content,
        final_relative_path,
        expected_format,
        deadline,
    ):
        self._check_deadline(deadline)
        staging_name = "{}.part".format(kind)
        staging_path = ".staging/{}/{}/{}".format(
            directory.names[-2],
            directory.names[-1],
            staging_name,
        )
        handle = create_owned_staging_file(
            directory,
            staging_name,
            path=staging_path,
        )
        try:
            self._write_all(handle.descriptor, content)
            self._check_deadline(deadline)
            os.fsync(handle.descriptor)
            self._check_deadline(deadline)
            os.fsync(directory.descriptor)
            self._check_deadline(deadline)
            handle.file_stat = os.fstat(handle.descriptor)
            actual_format, width, height = self._inspect(
                handle.descriptor
            )
            self._check_deadline(deadline)
            if actual_format != expected_format:
                raise _processing_failed()
            size = os.fstat(handle.descriptor).st_size
            digest = sha256_file_descriptor(handle.descriptor)
            self._check_deadline(deadline)
            return PreparedFile(
                kind=kind,
                owned_staging_handle=handle,
                final_relative_path=final_relative_path,
                size=size,
                sha256=digest,
                width=width,
                height=height,
                image_format=actual_format,
            )
        except BaseException:
            try:
                handle.cleanup()
            except BaseException:
                pass
            try:
                handle.close()
            except BaseException:
                pass
            raise

    def _stage_image(
        self,
        directory,
        kind,
        image,
        final_relative_path,
        expected_format,
        deadline,
    ):
        self._check_deadline(deadline)
        staging_name = "{}.part".format(kind)
        staging_path = ".staging/{}/{}/{}".format(
            directory.names[-2],
            directory.names[-1],
            staging_name,
        )
        handle = create_owned_staging_file(
            directory,
            staging_name,
            path=staging_path,
        )
        try:
            with os.fdopen(os.dup(handle.descriptor), "w+b") as file_obj:
                write_image_to_file(image, file_obj)
                self._check_deadline(deadline)
                file_obj.flush()
                os.fsync(file_obj.fileno())
                self._check_deadline(deadline)
            os.fsync(directory.descriptor)
            self._check_deadline(deadline)
            handle.file_stat = os.fstat(handle.descriptor)
            actual_format, width, height = self._inspect(
                handle.descriptor
            )
            self._check_deadline(deadline)
            if actual_format != expected_format:
                raise _processing_failed()
            size = os.fstat(handle.descriptor).st_size
            digest = sha256_file_descriptor(handle.descriptor)
            self._check_deadline(deadline)
            return PreparedFile(
                kind=kind,
                owned_staging_handle=handle,
                final_relative_path=final_relative_path,
                size=size,
                sha256=digest,
                width=width,
                height=height,
                image_format=actual_format,
            )
        except BaseException:
            try:
                handle.cleanup()
            except BaseException:
                pass
            try:
                handle.close()
            except BaseException:
                pass
            raise

    @staticmethod
    def _write_all(descriptor, content):
        if not isinstance(content, bytes) or not content:
            raise _processing_failed()
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.ftruncate(descriptor, 0)
        content_view = memoryview(content)
        offset = 0
        while offset < len(content):
            written = os.write(descriptor, content_view[offset:])
            if written <= 0:
                raise OSError("short media write")
            offset += written

    @staticmethod
    def _inspect(descriptor):
        with os.fdopen(os.dup(descriptor), "rb") as file_obj:
            file_obj.seek(0)
            with PILImage.open(file_obj) as image:
                image.load()
                return image.format, image.width, image.height

    @staticmethod
    def _asset_uuid(value):
        try:
            parsed = value if isinstance(value, uuid.UUID) else uuid.UUID(value)
        except (AttributeError, TypeError, ValueError):
            raise _media_conflict() from None
        if str(parsed) != str(value):
            raise _media_conflict()
        return parsed

    @staticmethod
    def _image_format(fetched):
        image_format = getattr(fetched, "image_format", None)
        if image_format not in FORMAT_EXTENSIONS:
            raise _processing_failed()
        if (
            type(getattr(fetched, "width", None)) is not int
            or fetched.width <= 0
            or type(getattr(fetched, "height", None)) is not int
            or fetched.height <= 0
        ):
            raise _processing_failed()
        return image_format

    @staticmethod
    def _derivative_options():
        configured = settings.IMAGE_SIZES
        if not isinstance(configured, dict):
            raise _configuration_error()
        options = []
        for kind in _DERIVATIVE_KINDS:
            value = configured.get(kind)
            if not isinstance(value, dict):
                raise _configuration_error()
            if not set(value).issubset({
                "size", "crop", "upscale", "quality",
            }):
                raise _configuration_error()
            size = value.get("size")
            if (
                not isinstance(size, (list, tuple))
                or len(size) != 2
                or any(type(dimension) is not int for dimension in size)
                or any(dimension < 0 for dimension in size)
                or not any(size)
            ):
                raise _configuration_error()
            if "crop" in value and type(value["crop"]) is not bool:
                raise _configuration_error()
            if "upscale" in value and type(value["upscale"]) is not bool:
                raise _configuration_error()
            quality = value.get("quality")
            if quality is not None and (
                type(quality) is not int or not 1 <= quality <= 95
            ):
                raise _configuration_error()
            options.append((kind, dict(value)))
        return tuple(options)

    @staticmethod
    def _published_file(
        prepared_file,
        directory,
        destination_name,
        result,
    ):
        if result.destination_stat is None:
            raise _storage_failed()
        return PublishedFile(
            kind=prepared_file.kind,
            final_relative_path=prepared_file.final_relative_path,
            size=prepared_file.size,
            sha256=prepared_file.sha256,
            width=prepared_file.width,
            height=prepared_file.height,
            image_format=prepared_file.image_format,
            created=result.created,
            reused=result.reused,
            destination_stat=result.destination_stat,
            destination_directory=directory,
            destination_name=destination_name,
        )

    def _validate_prepared(self, prepared, deadline=None):
        asset_uuid = self._asset_uuid(prepared.asset_uuid)
        try:
            run_uuid = self._asset_uuid(prepared.run_uuid)
            prepared.root_directory.verify_current()
            prepared.staging_directory.verify_current()
        except (AttributeError, MediaPathError, MediaStorageError):
            raise _media_conflict() from None
        if prepared.original_filename != sanitize_original_filename(
            prepared.original_filename
        ):
            raise _media_conflict()
        if prepared.staging_directory.names != [
            ".staging",
            str(run_uuid),
            str(asset_uuid),
        ]:
            raise _media_conflict()
        if len(prepared.files) != len(_KINDS):
            raise _media_conflict()
        for expected_kind, prepared_file in zip(_KINDS, prepared.files):
            if prepared_file.kind != expected_kind:
                raise _media_conflict()
            handle = prepared_file.owned_staging_handle
            if (
                handle.directory is not prepared.staging_directory
                or handle.name != "{}.part".format(expected_kind)
                or handle._closed
                or prepared_file.size <= 0
                or prepared_file.width <= 0
                or prepared_file.height <= 0
            ):
                raise _media_conflict()
            self._check_deadline(deadline)
            try:
                actual_format, actual_width, actual_height = self._inspect(
                    handle.descriptor
                )
            except Exception:
                raise _media_conflict() from None
            self._check_deadline(deadline)
            extension = FORMAT_EXTENSIONS.get(actual_format)
            if extension is None:
                raise _media_conflict()
            if expected_kind == "original":
                try:
                    expected_path = canonical_original_path(
                        asset_uuid,
                        prepared.original_filename,
                        extension,
                    )
                except ValueError:
                    raise _media_conflict() from None
            else:
                expected_path = "derivatives/{}/{}{}".format(
                    asset_uuid,
                    expected_kind,
                    extension,
                )
            if (
                prepared_file.image_format != actual_format
                or prepared_file.width != actual_width
                or prepared_file.height != actual_height
                or prepared_file.final_relative_path != expected_path
            ):
                raise _media_conflict()

    def _check_deadline(self, deadline):
        if deadline is not None and self.clock() >= deadline:
            raise _processing_timeout()

    def _fault(self, event):
        if self.fault_injector is not None:
            self.fault_injector(event)

    @staticmethod
    def _cleanup_partial_prepare(
        files,
        staging_directory,
        root_directory,
        asset_uuid,
        run_uuid,
        include_cleanup_identifiers=True,
    ):
        for prepared_file in reversed(files):
            try:
                removed = prepared_file.owned_staging_handle.cleanup()
                if removed is False:
                    _log_cleanup_incomplete(
                        "prepare_file_preserved",
                        asset_uuid,
                        run_uuid,
                        prepared_file.kind,
                        "IdentityMismatch",
                        include_identifiers=include_cleanup_identifiers,
                    )
            except BaseException as error:
                _log_cleanup_incomplete(
                    "prepare_file_cleanup_failed",
                    asset_uuid,
                    run_uuid,
                    prepared_file.kind,
                    type(error).__name__,
                    include_identifiers=include_cleanup_identifiers,
                )
            try:
                prepared_file.owned_staging_handle.close()
            except BaseException as error:
                _log_cleanup_incomplete(
                    "prepare_file_close_failed",
                    asset_uuid,
                    run_uuid,
                    prepared_file.kind,
                    type(error).__name__,
                    include_identifiers=include_cleanup_identifiers,
                )
        if staging_directory is not None:
            try:
                reason = staging_directory.remove_created_suffix(1)
                if reason is not None:
                    _log_cleanup_incomplete(
                        "prepare_directory_preserved",
                        asset_uuid,
                        run_uuid,
                        "staging",
                        reason,
                        include_identifiers=include_cleanup_identifiers,
                    )
            except BaseException as error:
                _log_cleanup_incomplete(
                    "prepare_directory_cleanup_failed",
                    asset_uuid,
                    run_uuid,
                    "staging",
                    type(error).__name__,
                    include_identifiers=include_cleanup_identifiers,
                )
            try:
                staging_directory.close()
            except BaseException as error:
                _log_cleanup_incomplete(
                    "prepare_directory_close_failed",
                    asset_uuid,
                    run_uuid,
                    "staging",
                    type(error).__name__,
                    include_identifiers=include_cleanup_identifiers,
                )
        if root_directory is not None:
            try:
                root_directory.close()
            except BaseException as error:
                _log_cleanup_incomplete(
                    "prepare_directory_close_failed",
                    asset_uuid,
                    run_uuid,
                    "root",
                    type(error).__name__,
                    include_identifiers=include_cleanup_identifiers,
                )

    @staticmethod
    def _map_error(error):
        if isinstance(error, MediaStorageError):
            return error
        if isinstance(error, MediaPathError):
            if str(error) == "atomic_publish_unsupported":
                return _storage_unsupported()
            return _media_conflict()
        if isinstance(error, OSError):
            return _storage_failed()
        return _processing_failed()


def _processing_timeout():
    return MediaStorageError(
        "image_processing_timeout",
        "The image could not be processed before the deadline.",
        True,
    )


def _media_conflict():
    return MediaStorageError(
        "media_path_conflict",
        "The media destination could not be used safely.",
        False,
    )


def _processing_failed():
    return MediaStorageError(
        "image_processing_failed",
        "The image could not be processed safely.",
        False,
    )


def _configuration_error():
    return MediaStorageError(
        "media_configuration_error",
        "The image storage configuration is invalid.",
        False,
    )


def _publish_changed():
    return MediaStorageError(
        "media_publish_changed",
        "The published media changed before it could be committed.",
        False,
    )


def _storage_failed():
    return MediaStorageError(
        "media_storage_failed",
        "The media storage is temporarily unavailable.",
        True,
    )


def _storage_unsupported():
    return MediaStorageError(
        "media_storage_unsupported",
        "Atomic media publishing is not supported on this system.",
        False,
    )
