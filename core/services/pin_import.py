from dataclasses import dataclass
import logging
import time
from urllib.parse import unquote, urlsplit
import uuid

from django.conf import settings
from django.db import DEFAULT_DB_ALIAS, connections, transaction

from core.models import Board, MediaAsset, Pin
from core.services.local_upload import read_local_upload
from core.services.pin_membership import PinMembershipService
from django_images.file_ops import MediaLifecycleLockError, MediaPathError
from django_images.models import Image, Thumbnail
from django_images.paths import (
    FORMAT_EXTENSIONS,
    canonical_original_path,
    sanitize_original_filename,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ImportMetadata:
    url: str
    referer: str
    description: str
    private: bool
    tags: tuple
    board_ids: tuple


class PinImportError(Exception):
    def __init__(self, code, message, retryable):
        super(PinImportError, self).__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable

    def as_dict(self):
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }


class PinImportService(object):
    def __init__(
        self,
        fetcher,
        media_storage,
        idempotency,
        clock=time.monotonic,
        fault_injector=None,
    ):
        self.fetcher = fetcher
        self.media_storage = media_storage
        self.idempotency = idempotency
        self.clock = clock
        self.fault_injector = fault_injector
        self._closed = False

    def close(self):
        if self._closed:
            return
        self._closed = True
        close = getattr(getattr(self.fetcher, "transport", None), "close", None)
        if close is None:
            return
        try:
            close()
        except BaseException as error:
            self._log_committed_callback_error(error)

    def prepare_url(self, url, referer, deadline):
        fetched = self.fetcher.fetch(
            url,
            referer=referer,
            deadline=deadline,
        )
        basename = urlsplit(fetched.final_url).path.rsplit("/", 1)[-1]
        original_filename = sanitize_original_filename(
            unquote(basename)
        )
        prepared = self.media_storage.prepare(
            fetched,
            asset_uuid=uuid.uuid4(),
            original_filename=original_filename,
            deadline=deadline,
        )
        try:
            self._check_deadline(deadline)
        except BaseException:
            self._cleanup(prepared)
            raise
        return prepared

    def prepare_upload(self, uploaded_file, deadline):
        uploaded = read_local_upload(
            uploaded_file,
            settings.PINRY_FETCH_MAX_BYTES,
            settings.PINRY_FETCH_MAX_PIXELS,
            deadline,
            self.clock,
        )
        prepared = self.media_storage.prepare(
            uploaded.inspected,
            asset_uuid=uuid.uuid4(),
            original_filename=uploaded.original_filename,
            deadline=deadline,
        )
        try:
            self._check_deadline(deadline)
        except BaseException:
            self._cleanup(prepared)
            raise
        return prepared

    def commit(self, prepared, user, metadata, claim, deadline):
        database = connections[DEFAULT_DB_ALIAS]
        try:
            autocommit = database.get_autocommit()
            in_atomic_block = database.in_atomic_block
        except BaseException:
            self._cleanup(prepared)
            raise
        if not autocommit or in_atomic_block:
            self._cleanup(prepared)
            raise self._internal_error()

        published = None
        reusable = None
        pin = None
        commit_marker = [False]
        try:
            self._check_deadline(deadline)
            content_sha256 = self._content_sha256(prepared)
            with self._dedup_lock(
                prepared,
                user.pk,
                content_sha256,
                deadline,
            ), self._lifecycle_lock(prepared, deadline):
                with transaction.atomic(using=DEFAULT_DB_ALIAS):
                    transaction.on_commit(
                        lambda: commit_marker.__setitem__(0, True),
                        using=DEFAULT_DB_ALIAS,
                    )
                    self._check_deadline(deadline)
                    if (
                        claim is not None
                        and not self.idempotency.fence(claim)
                    ):
                        raise self._lease_lost()
                    self._check_deadline(deadline)
                    boards = self._owned_boards(user, metadata.board_ids)
                    self._check_deadline(deadline)
                    asset = (
                        MediaAsset.objects.using(DEFAULT_DB_ALIAS)
                        .select_for_update()
                        .select_related("image")
                        .filter(
                            submitter=user,
                            content_sha256=content_sha256,
                        )
                        .first()
                    )
                    if asset is not None:
                        image = asset.image
                        thumbnails = list(
                            Thumbnail.objects.using(DEFAULT_DB_ALIAS)
                            .select_for_update()
                            .filter(original=image)
                            .order_by("size")
                        )
                        reusable = self.media_storage.verify_reusable(
                            prepared,
                            image,
                            thumbnails,
                            deadline=deadline,
                        )
                        self._fault("after_reusable_verified")
                    else:
                        published = self.media_storage.publish(
                            prepared,
                            deadline=deadline,
                        )
                        self._check_deadline(deadline)
                        files = self._manifest_files(published, prepared)
                        original = files["original"]
                        image = Image.objects.using(
                            DEFAULT_DB_ALIAS
                        ).create(
                            image=original.final_relative_path,
                            asset_uuid=prepared.asset_uuid,
                            original_filename=prepared.original_filename,
                            width=original.width,
                            height=original.height,
                        )
                        self._fault("after_image_row")
                        self._check_deadline(deadline)
                        Thumbnail.objects.using(
                            DEFAULT_DB_ALIAS
                        ).bulk_create([
                            Thumbnail(
                                original=image,
                                image=files[kind].final_relative_path,
                                size=kind,
                                width=files[kind].width,
                                height=files[kind].height,
                            )
                            for kind in (
                                "thumbnail", "standard", "square"
                            )
                        ])
                        self._fault("after_thumbnail_rows")
                        self._check_deadline(deadline)
                        MediaAsset.objects.using(
                            DEFAULT_DB_ALIAS
                        ).create(
                            submitter=user,
                            image=image,
                            content_sha256=content_sha256,
                        )
                        self._fault("after_media_asset_row")
                    self._check_deadline(deadline)
                    pin = Pin.objects.using(DEFAULT_DB_ALIAS).create(
                        submitter=user,
                        image=image,
                        url=metadata.url,
                        referer=metadata.referer,
                        description=metadata.description,
                        private=metadata.private,
                    )
                    self._fault("after_pin_row")
                    self._check_deadline(deadline)
                    if metadata.tags:
                        pin.tags.add(*metadata.tags)
                    self._fault("after_tags")
                    self._check_deadline(deadline)
                    self._check_deadline(deadline)
                    PinMembershipService().add_new_pin_to_locked_boards(
                        pin,
                        boards,
                    )
                    self._fault("after_boards")
                    self._check_deadline(deadline)
                    self._fault("before_idempotency_success")
                    self._check_deadline(deadline)
                    if (
                        claim is not None
                        and not self.idempotency.record_success(claim, pin)
                    ):
                        raise self._lease_lost()
                    self._check_deadline(deadline)
                    (reusable or published).verify_current()
        except BaseException as error:
            if commit_marker[0]:
                return self._resolve_committed_error(
                    published,
                    reusable,
                    prepared,
                    error,
                    pin,
                )
            self._rollback_receipts(published, reusable, prepared)
            if isinstance(error, MediaLifecycleLockError):
                if error.retryable:
                    raise self._processing_timeout() from None
                raise self._configuration_error() from None
            if isinstance(error, MediaPathError):
                raise self._configuration_error() from None
            raise

        if not commit_marker[0]:
            self._rollback_receipts(published, reusable, prepared)
            raise self._internal_error()
        cleanup_error = self._finish_receipts(
            published,
            reusable,
            prepared,
        )
        if cleanup_error is not None:
            raise cleanup_error
        return pin

    @staticmethod
    def _content_sha256(prepared):
        try:
            content_sha256 = prepared.content_sha256
        except (AttributeError, TypeError):
            raise PinImportService._internal_error() from None
        if (
            type(content_sha256) is not str
            or len(content_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in content_sha256
            )
        ):
            raise PinImportService._internal_error()
        return content_sha256

    def _owned_boards(self, user, board_ids):
        expected = tuple(board_ids)
        boards = list(
            Board.objects.using(DEFAULT_DB_ALIAS).select_for_update().filter(
                submitter=user,
                pk__in=expected,
            ).order_by("pk")
        )
        if tuple(board.pk for board in boards) != expected:
            raise PinImportError(
                "board_access_changed",
                "The selected boards are no longer available.",
                False,
            )
        return boards

    def _lifecycle_lock(self, prepared, deadline):
        try:
            lifecycle_lock = self.media_storage.lifecycle_lock
            if not callable(lifecycle_lock):
                raise TypeError()
            context = lifecycle_lock(
                prepared,
                deadline=deadline,
                clock=self.clock,
            )
            if (
                not callable(getattr(context, "__enter__", None))
                or not callable(getattr(context, "__exit__", None))
            ):
                raise TypeError()
            return context
        except (AttributeError, TypeError):
            raise self._configuration_error() from None

    def _dedup_lock(
        self,
        prepared,
        submitter_id,
        content_sha256,
        deadline,
    ):
        try:
            dedup_lock = self.media_storage.dedup_lock
            if not callable(dedup_lock):
                raise TypeError()
            context = dedup_lock(
                prepared,
                submitter_id,
                content_sha256,
                deadline=deadline,
                clock=self.clock,
            )
            if (
                not callable(getattr(context, "__enter__", None))
                or not callable(getattr(context, "__exit__", None))
            ):
                raise TypeError()
            return context
        except (AttributeError, TypeError):
            raise self._configuration_error() from None

    @staticmethod
    def _manifest_files(published, prepared):
        try:
            files = {entry.kind: entry for entry in published.files}
            destination_directories = tuple(
                published.destination_directories
            )
            if (
                published.asset_uuid != prepared.asset_uuid
                or published.original_filename
                != prepared.original_filename
                or tuple(entry.kind for entry in published.files)
                != ("original", "thumbnail", "standard", "square")
                or len(destination_directories) != 2
            ):
                raise ValueError()
            for entry in files.values():
                extension = FORMAT_EXTENSIONS.get(entry.image_format)
                if extension is None:
                    raise ValueError()
                if entry.kind == "original":
                    expected_path = canonical_original_path(
                        prepared.asset_uuid,
                        prepared.original_filename,
                        extension,
                    )
                else:
                    expected_path = "derivatives/{}/{}{}".format(
                        prepared.asset_uuid,
                        entry.kind,
                        extension,
                    )
                expected_parts = expected_path.split("/")
                destination_directory = entry.destination_directory
                if (
                    not isinstance(entry.final_relative_path, str)
                    or entry.final_relative_path != expected_path
                    or entry.destination_name != expected_parts[-1]
                    or destination_directory.names != expected_parts[:-1]
                    or not any(
                        destination_directory is directory
                        for directory in destination_directories
                    )
                    or type(entry.width) is not int
                    or entry.width <= 0
                    or type(entry.height) is not int
                    or entry.height <= 0
                ):
                    raise ValueError()
        except (AttributeError, TypeError, ValueError):
            raise PinImportService._internal_error() from None
        return files

    def _check_deadline(self, deadline):
        if deadline is not None and self.clock() >= deadline:
            raise PinImportError(
                "image_processing_timeout",
                "The image could not be processed before the deadline.",
                True,
            )

    def _fault(self, event):
        if self.fault_injector is not None:
            self.fault_injector(event)

    @staticmethod
    def _cleanup(prepared):
        first_base_error = None
        for attempt in range(2):
            try:
                prepared.cleanup()
            except Exception:
                pass
            except BaseException as error:
                if first_base_error is None:
                    first_base_error = error
            try:
                if not prepared.is_open:
                    if first_base_error is None or attempt == 1:
                        break
            except Exception:
                pass
            except BaseException as error:
                if first_base_error is None:
                    first_base_error = error
        return first_base_error

    @staticmethod
    def _compensate(published):
        try:
            published.compensate()
        except BaseException:
            pass

    @staticmethod
    def _release(published, attempts=2):
        if published is None:
            return None
        first_base_error = None
        for _attempt in range(attempts):
            try:
                published.release()
            except Exception:
                pass
            except BaseException as error:
                if first_base_error is None:
                    first_base_error = error
        return first_base_error

    @classmethod
    def _finish_receipts(cls, published, reusable, prepared):
        if reusable is not None:
            release_error = cls._release(reusable)
            cleanup_error = cls._cleanup(prepared)
            return release_error or cleanup_error
        return cls._release(published)

    def _resolve_committed_error(
        self,
        published,
        reusable,
        prepared,
        error,
        pin,
    ):
        cleanup_error = self._finish_receipts(
            published,
            reusable,
            prepared,
        )
        if not isinstance(error, Exception):
            raise error
        if cleanup_error is not None:
            raise cleanup_error
        self._log_committed_callback_error(error)
        return pin

    @classmethod
    def _rollback_receipts(cls, published, reusable, prepared):
        if reusable is not None:
            cls._release(reusable)
            cls._cleanup(prepared)
        elif published is None:
            cls._cleanup(prepared)
        else:
            cls._compensate(published)
            cls._release(published, attempts=1)

    @staticmethod
    def _lease_lost():
        return PinImportError(
            "lease_lost",
            "The import lease is no longer owned by this request.",
            True,
        )

    @staticmethod
    def _internal_error():
        return PinImportError(
            "internal_error",
            "The image import could not be completed safely.",
            False,
        )

    @staticmethod
    def _processing_timeout():
        return PinImportError(
            "image_processing_timeout",
            "The image could not be processed before the deadline.",
            True,
        )

    @staticmethod
    def _configuration_error():
        return PinImportError(
            "media_configuration_error",
            "The image storage configuration is invalid.",
            False,
        )

    @staticmethod
    def _log_committed_callback_error(error):
        try:
            logger.warning(
                "pin_import_post_commit_callback_failed error_type=%s",
                type(error).__name__,
            )
        except BaseException:
            pass
