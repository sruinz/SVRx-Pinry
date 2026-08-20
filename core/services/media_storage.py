from dataclasses import dataclass
from io import BytesIO
import logging
import os
from pathlib import PurePosixPath
import time
import uuid

from django.conf import settings
from django.core.files.images import ImageFile
from PIL import Image as PILImage

from django_images.file_ops import (
    MediaPathError,
    PublishFailure,
    create_owned_staging_file,
    media_lifecycle_lock,
    open_media_root,
    open_or_create_media_directory_from,
    publish_owned_noreplace,
    sha256_file_descriptor,
    unlink_published_name_if_current,
    verify_published_identity,
    verify_published_name,
)
from django_images.paths import FORMAT_EXTENSIONS, sanitize_original_filename
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
):
    try:
        logger.warning(
            "media_storage_cleanup_incomplete event=%s asset_uuid=%s "
            "run_uuid=%s kind=%s error_type=%s",
            event,
            str(asset_uuid),
            str(run_uuid),
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
    ):
        self.asset_uuid = asset_uuid
        self.original_filename = original_filename
        self.run_uuid = run_uuid
        self.files = tuple(files)
        self.root_directory = root_directory
        self.staging_directory = staging_directory
        self._cleaned = False

    @property
    def is_open(self):
        return not self._cleaned

    def cleanup(self):
        if self._cleaned:
            return
        for prepared_file in reversed(self.files):
            try:
                removed = prepared_file.owned_staging_handle.cleanup()
                if removed is False:
                    _log_cleanup_incomplete(
                        "prepare_file_preserved",
                        self.asset_uuid,
                        self.run_uuid,
                        prepared_file.kind,
                        "IdentityMismatch",
                    )
            except BaseException as error:
                _log_cleanup_incomplete(
                    "prepare_file_cleanup_failed",
                    self.asset_uuid,
                    self.run_uuid,
                    prepared_file.kind,
                    type(error).__name__,
                )
        for prepared_file in reversed(self.files):
            try:
                prepared_file.owned_staging_handle.close()
            except BaseException as error:
                _log_cleanup_incomplete(
                    "prepare_file_close_failed",
                    self.asset_uuid,
                    self.run_uuid,
                    prepared_file.kind,
                    type(error).__name__,
                )
        try:
            reason = self.staging_directory.remove_created_suffix(1)
            if reason is not None:
                _log_cleanup_incomplete(
                    "prepare_directory_preserved",
                    self.asset_uuid,
                    self.run_uuid,
                    "staging",
                    reason,
                )
        except BaseException as error:
            _log_cleanup_incomplete(
                "prepare_directory_cleanup_failed",
                self.asset_uuid,
                self.run_uuid,
                "staging",
                type(error).__name__,
            )
        try:
            self.staging_directory.close()
        except BaseException as error:
            _log_cleanup_incomplete(
                "prepare_directory_close_failed",
                self.asset_uuid,
                self.run_uuid,
                "staging",
                type(error).__name__,
            )
        try:
            self.root_directory.close()
        except BaseException as error:
            _log_cleanup_incomplete(
                "prepare_directory_close_failed",
                self.asset_uuid,
                self.run_uuid,
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
                    _log_cleanup_incomplete(
                        "compensate_file_preserved",
                        self.asset_uuid,
                        self.prepared.run_uuid,
                        published_file.kind,
                        "IdentityMismatch",
                    )
            except BaseException as error:
                _log_cleanup_incomplete(
                    "compensate_file_cleanup_failed",
                    self.asset_uuid,
                    self.prepared.run_uuid,
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
                    _log_cleanup_incomplete(
                        "compensate_directory_preserved",
                        self.asset_uuid,
                        self.prepared.run_uuid,
                        kind,
                        reason,
                    )
            except BaseException as error:
                _log_cleanup_incomplete(
                    "compensate_directory_cleanup_failed",
                    self.asset_uuid,
                    self.prepared.run_uuid,
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
            _log_cleanup_incomplete(
                "prepared_cleanup_failed",
                self.asset_uuid,
                self.prepared.run_uuid,
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
                _log_cleanup_incomplete(
                    "published_directory_close_failed",
                    self.asset_uuid,
                    self.prepared.run_uuid,
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

    def prepare(
        self,
        fetched,
        asset_uuid,
        original_filename,
        deadline=None,
    ):
        try:
            asset_uuid = self._asset_uuid(asset_uuid)
            image_format = self._image_format(fetched)
            derivative_options = self._derivative_options()
            if not isinstance(original_filename, str):
                raise _processing_failed()
            original_filename = sanitize_original_filename(
                original_filename
            )
            self._check_deadline(deadline)
        except MediaStorageError:
            raise
        except Exception:
            raise _processing_failed() from None

        run_uuid = uuid.uuid4()
        root_directory = None
        staging_directory = None
        prepared_files = []
        active_kind = "staging"
        try:
            root_directory = open_media_root(self.media_root)
            staging_directory = open_or_create_media_directory_from(
                root_directory,
                ".staging/{}/{}".format(run_uuid, asset_uuid),
            )
            active_kind = "original"
            extension = FORMAT_EXTENSIONS[image_format]
            original = self._stage_bytes(
                staging_directory,
                "original",
                fetched.content,
                "originals/{}/original{}".format(asset_uuid, extension),
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
            )
        except BaseException as error:
            if isinstance(error, (MediaPathError, OSError)):
                _log_cleanup_incomplete(
                    "prepare_failed",
                    asset_uuid,
                    run_uuid,
                    active_kind,
                    type(error).__name__,
                )
            self._cleanup_partial_prepare(
                prepared_files,
                staging_directory,
                root_directory,
                asset_uuid,
                run_uuid,
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
            self._validate_prepared(prepared)
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

    def _validate_prepared(self, prepared):
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
            extension = FORMAT_EXTENSIONS.get(prepared_file.image_format)
            if extension is None:
                raise _media_conflict()
            if expected_kind == "original":
                expected_path = "originals/{}/original{}".format(
                    asset_uuid,
                    extension,
                )
            else:
                expected_path = "derivatives/{}/{}{}".format(
                    asset_uuid,
                    expected_kind,
                    extension,
                )
            handle = prepared_file.owned_staging_handle
            if (
                prepared_file.final_relative_path != expected_path
                or handle.directory is not prepared.staging_directory
                or handle.name != "{}.part".format(expected_kind)
                or handle._closed
                or prepared_file.size <= 0
                or prepared_file.width <= 0
                or prepared_file.height <= 0
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
                    )
            except BaseException as error:
                _log_cleanup_incomplete(
                    "prepare_file_cleanup_failed",
                    asset_uuid,
                    run_uuid,
                    prepared_file.kind,
                    type(error).__name__,
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
                    )
            except BaseException as error:
                _log_cleanup_incomplete(
                    "prepare_directory_cleanup_failed",
                    asset_uuid,
                    run_uuid,
                    "staging",
                    type(error).__name__,
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
