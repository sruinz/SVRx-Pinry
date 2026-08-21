import datetime
from io import BytesIO
import os
from pathlib import Path
import queue
import tempfile
import threading
from types import SimpleNamespace
import unicodedata
import uuid

from django.db import (
    close_old_connections,
    connection,
    connections,
    OperationalError,
    transaction,
)
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TransactionTestCase
import mock
from rest_framework import status
from rest_framework.test import APIClient

from PIL import Image as PILImage

from core.services.batch_import import BatchImportService
from core.services.idempotency import (
    ClaimResult,
    IdempotencyStore,
    fingerprint_request,
)
from core.services.media_storage import MediaStorage, MediaStorageError
from core.services.pin_membership import PinMembershipService
from core.services.pin_import import (
    ImportMetadata,
    PinImportError,
    PinImportService,
)
from core.services.safe_url_fetch import FetchedImage
from core.models import BatchImportItem, Board, MediaAsset, Pin
from core.tests.helpers import create_image
from django_images import file_ops
from django_images.models import Image, Thumbnail
from django_images.paths import canonical_original_path
from django_images.test_helpers import TemporaryMediaMixin
from taggit.models import Tag
from users.models import User


class _PreparedAsset(object):
    def __init__(self):
        self.cleanup_calls = 0
        self.is_open = True
        self.content_sha256 = "a" * 64

    def cleanup(self):
        if not self.is_open:
            return
        self.cleanup_calls += 1
        self.is_open = False


class _NoopLifecycleContext(object):
    def __enter__(self):
        return self

    def __exit__(self, error_type, error, traceback):
        del error_type, error, traceback
        return False


class _LifecycleStorageMixin(object):
    def dedup_lock(
        self,
        prepared,
        submitter_id,
        content_sha256,
        deadline=None,
        clock=None,
    ):
        del prepared, submitter_id, content_sha256, deadline, clock
        return _NoopLifecycleContext()

    def lifecycle_lock(self, prepared, deadline=None, clock=None):
        del prepared, deadline, clock
        return _NoopLifecycleContext()


class _MediaStorage(_LifecycleStorageMixin):
    def __init__(self):
        self.publish_calls = 0

    def publish(self, prepared, deadline=None):
        del prepared, deadline
        self.publish_calls += 1
        raise AssertionError("publish must not run in a nested transaction")


class _PreparingMediaStorage(object):
    def __init__(self, result):
        self.result = result
        self.calls = []

    def prepare(
        self,
        fetched,
        asset_uuid,
        original_filename,
        deadline=None,
    ):
        self.calls.append(
            (fetched, asset_uuid, original_filename, deadline)
        )
        return self.result


class _Fetcher(object):
    def __init__(self, result):
        self.result = result
        self.calls = []

    def fetch(self, url, referer=None, deadline=None):
        self.calls.append((url, referer, deadline))
        return self.result


class _IdempotencyStore(object):
    def __init__(self):
        self.fence_calls = 0

    def fence(self, claim):
        del claim
        self.fence_calls += 1
        raise AssertionError("fence must not run in a nested transaction")


class _ManifestPreparedAsset(_PreparedAsset):
    def __init__(self, asset_uuid, original_filename):
        super(_ManifestPreparedAsset, self).__init__()
        self.asset_uuid = asset_uuid
        self.original_filename = original_filename


class _PublishedAsset(object):
    def __init__(self, prepared, events):
        self.prepared = prepared
        self.asset_uuid = prepared.asset_uuid
        self.original_filename = prepared.original_filename
        original_path = canonical_original_path(
            prepared.asset_uuid,
            prepared.original_filename,
            ".png",
        )
        originals_directory = SimpleNamespace(
            names=["originals", str(prepared.asset_uuid)]
        )
        derivatives_directory = SimpleNamespace(
            names=["derivatives", str(prepared.asset_uuid)]
        )
        self.destination_directories = (
            originals_directory,
            derivatives_directory,
        )
        self.files = (
            SimpleNamespace(
                kind="original",
                final_relative_path=original_path,
                width=640,
                height=480,
                image_format="PNG",
                destination_directory=originals_directory,
                destination_name=original_path.rsplit("/", 1)[-1],
            ),
            SimpleNamespace(
                kind="thumbnail",
                final_relative_path=(
                    "derivatives/{}/thumbnail.png".format(
                        prepared.asset_uuid
                    )
                ),
                width=240,
                height=180,
                image_format="PNG",
                destination_directory=derivatives_directory,
                destination_name="thumbnail.png",
            ),
            SimpleNamespace(
                kind="standard",
                final_relative_path=(
                    "derivatives/{}/standard.png".format(
                        prepared.asset_uuid
                    )
                ),
                width=600,
                height=450,
                image_format="PNG",
                destination_directory=derivatives_directory,
                destination_name="standard.png",
            ),
            SimpleNamespace(
                kind="square",
                final_relative_path=(
                    "derivatives/{}/square.png".format(
                        prepared.asset_uuid
                    )
                ),
                width=125,
                height=125,
                image_format="PNG",
                destination_directory=derivatives_directory,
                destination_name="square.png",
            ),
        )
        self.events = events
        self.verify_calls = 0
        self.compensate_calls = 0
        self.release_calls = 0
        self.released = False

    def verify_current(self):
        self.verify_calls += 1
        self.events.append("verify_current")

    def compensate(self):
        if self.released:
            return
        self.compensate_calls += 1
        self.events.append("compensate")
        self.prepared.cleanup()
        self.released = True

    def release(self):
        if self.released:
            return
        self.release_calls += 1
        self.events.append("release")
        self.prepared.cleanup()
        self.released = True


class _ExplodingCompensationPublishedAsset(_PublishedAsset):
    def compensate(self):
        self.compensate_calls += 1
        self.events.append("compensate")
        raise RuntimeError("cleanup-private-token")


class _ExplodingReleasePublishedAsset(_PublishedAsset):
    def release(self):
        self.release_calls += 1
        self.events.append("release")
        raise RuntimeError("release-private-token")


class _FailingSecondVerifyPublishedAsset(_PublishedAsset):
    def __init__(self, prepared, events):
        super(_FailingSecondVerifyPublishedAsset, self).__init__(
            prepared,
            events,
        )
        self.foreign_destination = b"foreign"

    def verify_current(self):
        super(_FailingSecondVerifyPublishedAsset, self).verify_current()
        if self.verify_calls == 2:
            raise MediaStorageError(
                "media_publish_changed",
                "The published media changed before commit.",
                False,
            )


class _ReceiptAwarePublishedAsset(_PublishedAsset):
    def __init__(self, prepared, events):
        super(_ReceiptAwarePublishedAsset, self).__init__(prepared, events)
        self.destinations = {
            "created": b"created",
            "reused": b"reused",
            "replacement": b"foreign",
        }

    def compensate(self):
        if self.released:
            return
        self.compensate_calls += 1
        self.events.append("compensate")
        self.destinations.pop("created", None)
        self.prepared.cleanup()
        self.released = True


class _PublishingMediaStorage(_LifecycleStorageMixin):
    def __init__(self, published, events):
        self.published = published
        self.events = events
        self.publish_calls = []

    def publish(self, prepared, deadline=None):
        self.publish_calls.append((prepared, deadline))
        self.events.append("publish")
        self.published.verify_current()
        return self.published


class _ActualLifecyclePublishingStorage(_PublishingMediaStorage):
    def __init__(self, published, events, root_directory):
        super(_ActualLifecyclePublishingStorage, self).__init__(
            published,
            events,
        )
        self.root_directory = root_directory

    def lifecycle_lock(self, prepared, deadline=None, clock=None):
        del prepared
        return file_ops.media_lifecycle_lock(
            self.root_directory,
            deadline=deadline,
            clock=clock,
        )


class _RaisingMediaStorage(_LifecycleStorageMixin):
    def __init__(self, error):
        self.error = error
        self.publish_calls = []

    def publish(self, prepared, deadline=None):
        self.publish_calls.append((prepared, deadline))
        raise self.error


class _SuccessfulIdempotencyStore(object):
    def __init__(self, events):
        self.events = events
        self.fence_calls = []
        self.success_calls = []

    def fence(self, claim):
        self.fence_calls.append(claim)
        self.events.append("fence")
        return True

    def record_success(self, claim, pin):
        self.success_calls.append((claim, pin))
        self.events.append("record_success")
        return True


class _ConfigurableIdempotencyStore(_SuccessfulIdempotencyStore):
    def __init__(self, events, fence_result=True, success_result=True):
        super(_ConfigurableIdempotencyStore, self).__init__(events)
        self.fence_result = fence_result
        self.success_result = success_result

    def fence(self, claim):
        super(_ConfigurableIdempotencyStore, self).fence(claim)
        return self.fence_result

    def record_success(self, claim, pin):
        super(_ConfigurableIdempotencyStore, self).record_success(
            claim,
            pin,
        )
        return self.success_result


class _ManualClock(object):
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class _LinuxStrongPublishMixin(object):
    def setUp(self):
        super(_LinuxStrongPublishMixin, self).setUp()
        if file_ops.sys.platform.startswith("linux"):
            return
        platform_patch = mock.patch(
            "django_images.file_ops.sys.platform",
            "linux",
        )
        publish_patch = mock.patch(
            "django_images.file_ops._publish_linux_descriptor",
            side_effect=self._link_staging_descriptor_for_test,
        )
        platform_patch.start()
        publish_patch.start()
        self.addCleanup(publish_patch.stop)
        self.addCleanup(platform_patch.stop)

    def _link_staging_descriptor_for_test(
        self,
        descriptor,
        destination_directory_descriptor,
        destination_name,
    ):
        expected = os.fstat(descriptor)
        staging_root = Path(self.temporary_media.name, ".staging")
        for candidate in staging_root.rglob("*.part"):
            current = os.stat(str(candidate), follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (
                expected.st_dev,
                expected.st_ino,
            ):
                continue
            os.link(
                str(candidate),
                destination_name,
                dst_dir_fd=destination_directory_descriptor,
                follow_symlinks=False,
            )
            return
        raise OSError("test staging descriptor path was not found")


def _png_fetched_image(final_url):
    output = BytesIO()
    PILImage.new("RGB", (640, 480), "red").save(output, format="PNG")
    return FetchedImage(
        content=output.getvalue(),
        image_format="PNG",
        width=640,
        height=480,
        final_url=final_url,
    )


def _uploaded_png(content, filename="local.png"):
    return SimpleUploadedFile(
        filename,
        content,
        content_type="image/png",
    )


def _file_snapshot(media_root):
    root = Path(media_root)
    snapshot = {}
    for path in root.rglob("*"):
        if (
            path.is_file()
            and ".pinry-locks" not in path.relative_to(root).parts
        ):
            relative_path = unicodedata.normalize(
                "NFC", path.relative_to(root).as_posix()
            )
            snapshot[relative_path] = path.read_bytes()
    return snapshot


class PinImportOutermostTransactionTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="atomic-owner",
            email="atomic-owner@example.com",
        )
        self.media_storage = _MediaStorage()
        self.idempotency = _IdempotencyStore()
        self.service = PinImportService(
            fetcher=object(),
            media_storage=self.media_storage,
            idempotency=self.idempotency,
            clock=lambda: 10.0,
        )
        self.prepared = _PreparedAsset()
        self.metadata = ImportMetadata(
            url="https://images.example/photo.png",
            referer="https://images.example/page",
            description="caption",
            private=False,
            tags=(),
            board_ids=(),
        )
        self.claim = ClaimResult(
            kind="claimed",
            row_id=1,
            lease_uuid=uuid.uuid4(),
            lease_generation=1,
        )

    def test_nested_atomic_fails_before_fence_publish_or_database_write(self):
        with transaction.atomic():
            with self.assertRaises(PinImportError) as caught:
                self.service.commit(
                    self.prepared,
                    self.user,
                    self.metadata,
                    self.claim,
                    deadline=20.0,
                )

        self.assertEqual(caught.exception.code, "internal_error")
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(self.idempotency.fence_calls, 0)
        self.assertEqual(self.media_storage.publish_calls, 0)
        self.assertEqual(self.prepared.cleanup_calls, 1)

    def test_autocommit_state_lookup_error_still_cleans_prepared_asset(self):
        lookup_error = RuntimeError("private-token")

        with mock.patch.object(
            connection,
            "get_autocommit",
            side_effect=lookup_error,
        ):
            with self.assertRaises(RuntimeError) as caught:
                self.service.commit(
                    self.prepared,
                    self.user,
                    self.metadata,
                    self.claim,
                    deadline=20.0,
                )

        self.assertIs(caught.exception, lookup_error)
        self.assertEqual(self.idempotency.fence_calls, 0)
        self.assertEqual(self.media_storage.publish_calls, 0)
        self.assertEqual(self.prepared.cleanup_calls, 1)

    def test_disabled_autocommit_fails_before_fence_publish_or_db_write(self):
        connection.set_autocommit(False)
        try:
            with self.assertRaises(PinImportError) as caught:
                self.service.commit(
                    self.prepared,
                    self.user,
                    self.metadata,
                    self.claim,
                    deadline=20.0,
                )
        finally:
            connection.rollback()
            connection.set_autocommit(True)

        self.assertEqual(caught.exception.code, "internal_error")
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(self.idempotency.fence_calls, 0)
        self.assertEqual(self.media_storage.publish_calls, 0)
        self.assertEqual(self.prepared.cleanup_calls, 1)


class PinImportPrepareTests(TransactionTestCase):
    def test_prepare_url_uses_final_url_basename_and_same_deadline(self):
        deadline = 20.0
        fetched = FetchedImage(
            content=b"image-bytes",
            image_format="PNG",
            width=10,
            height=20,
            final_url=(
                "https://cdn.example/folder/%ED%95%9C%EA%B8%80%2F"
                "photo%00.png?private-token=secret#ignored"
            ),
        )
        prepared = object()
        fetcher = _Fetcher(fetched)
        media_storage = _PreparingMediaStorage(prepared)
        service = PinImportService(
            fetcher=fetcher,
            media_storage=media_storage,
            idempotency=object(),
            clock=lambda: 10.0,
        )

        result = service.prepare_url(
            "https://origin.example/image",
            "https://origin.example/page",
            deadline,
        )

        self.assertIs(result, prepared)
        self.assertEqual(
            fetcher.calls,
            [(
                "https://origin.example/image",
                "https://origin.example/page",
                deadline,
            )],
        )
        self.assertEqual(len(media_storage.calls), 1)
        self.assertIs(media_storage.calls[0][0], fetched)
        self.assertIsInstance(media_storage.calls[0][1], uuid.UUID)
        self.assertEqual(media_storage.calls[0][2], "photo.png")
        self.assertIs(media_storage.calls[0][3], deadline)

    def test_late_prepare_is_cleaned_before_commit_can_begin(self):
        fetched = FetchedImage(
            content=b"image-bytes",
            image_format="PNG",
            width=10,
            height=20,
            final_url="https://cdn.example/photo.png",
        )
        prepared = _PreparedAsset()
        service = PinImportService(
            fetcher=_Fetcher(fetched),
            media_storage=_PreparingMediaStorage(prepared),
            idempotency=object(),
            clock=lambda: 20.0,
        )

        with self.assertRaises(PinImportError) as caught:
            service.prepare_url(
                "https://origin.example/image",
                "https://origin.example/page",
                deadline=20.0,
            )

        self.assertEqual(caught.exception.code, "image_processing_timeout")
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(prepared.cleanup_calls, 1)

    def test_post_prepare_clock_base_exception_cleans_and_reraises(self):
        fetched = FetchedImage(
            content=b"image-bytes",
            image_format="PNG",
            width=10,
            height=20,
            final_url="https://cdn.example/photo.png",
        )
        prepared = _PreparedAsset()
        original = KeyboardInterrupt()
        service = PinImportService(
            fetcher=_Fetcher(fetched),
            media_storage=_PreparingMediaStorage(prepared),
            idempotency=object(),
            clock=mock.Mock(side_effect=original),
        )

        with self.assertRaises(KeyboardInterrupt) as caught:
            service.prepare_url(
                "https://origin.example/image",
                "https://origin.example/page",
                deadline=20.0,
            )

        self.assertIs(caught.exception, original)
        self.assertEqual(prepared.cleanup_calls, 1)


class PinImportCommitTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="commit-owner",
            email="commit-owner@example.com",
        )
        self.board = Board.objects.create(
            submitter=self.user,
            name="references",
        )
        self.asset_uuid = uuid.UUID(
            "12345678-1234-5678-1234-567812345678"
        )
        self.prepared = _ManifestPreparedAsset(
            self.asset_uuid,
            "source-name.png",
        )
        self.events = []
        self.published = _PublishedAsset(self.prepared, self.events)
        self.media_storage = _PublishingMediaStorage(
            self.published,
            self.events,
        )
        self.idempotency = _SuccessfulIdempotencyStore(self.events)
        self.service = PinImportService(
            fetcher=object(),
            media_storage=self.media_storage,
            idempotency=self.idempotency,
            clock=lambda: 10.0,
            fault_injector=self.events.append,
        )
        self.claim = ClaimResult(
            kind="claimed",
            row_id=1,
            lease_uuid=uuid.uuid4(),
            lease_generation=1,
        )
        self.metadata = ImportMetadata(
            url="https://images.example/photo.png",
            referer="https://images.example/page",
            description="caption",
            private=True,
            tags=("design", "reference"),
            board_ids=(self.board.pk,),
        )

    def test_missing_lifecycle_lock_fails_closed_before_publish(self):
        published = self.published

        class MissingLifecycleStorage(object):
            def __init__(inner_self):
                inner_self.publish_calls = []

            def publish(inner_self, prepared, deadline=None):
                inner_self.publish_calls.append((prepared, deadline))
                return published

        storage = MissingLifecycleStorage()
        service = PinImportService(
            fetcher=object(),
            media_storage=storage,
            idempotency=self.idempotency,
            clock=lambda: 10.0,
        )

        with self.assertRaises(PinImportError) as caught:
            service.commit(
                self.prepared,
                self.user,
                self.metadata,
                self.claim,
                deadline=20.0,
            )

        self.assertEqual(caught.exception.code, "media_configuration_error")
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(storage.publish_calls, [])
        self.assertEqual(self.prepared.cleanup_calls, 1)
        self.assertEqual(Pin.objects.count(), 0)

    def test_commit_delegates_locked_boards_to_membership_helper(self):
        calls = []
        real_add = PinMembershipService.add_new_pin_to_locked_boards

        def record_add(service, pin, boards):
            calls.append((pin.pk, tuple(board.pk for board in boards)))
            return real_add(service, pin, boards)

        with mock.patch.object(
            PinMembershipService,
            "add_new_pin_to_locked_boards",
            autospec=True,
            side_effect=record_add,
        ):
            pin = self.service.commit(
                self.prepared,
                self.user,
                self.metadata,
                self.claim,
                deadline=20.0,
            )

        self.assertEqual(calls, [(pin.pk, (self.board.pk,))])
        self.assertTrue(self.board.pins.filter(pk=pin.pk).exists())

    def test_lifecycle_method_type_error_is_not_retried(self):
        class TypeErrorLifecycleStorage(_PublishingMediaStorage):
            def __init__(inner_self, published, events):
                super(TypeErrorLifecycleStorage, inner_self).__init__(
                    published,
                    events,
                )
                inner_self.lock_calls = 0

            def lifecycle_lock(
                inner_self, prepared, deadline=None, clock=None
            ):
                del prepared, deadline, clock
                inner_self.lock_calls += 1
                raise TypeError("private-body-error")

        storage = TypeErrorLifecycleStorage(self.published, self.events)
        service = PinImportService(
            fetcher=object(),
            media_storage=storage,
            idempotency=self.idempotency,
            clock=lambda: 10.0,
        )

        with self.assertRaises(PinImportError) as caught:
            service.commit(
                self.prepared,
                self.user,
                self.metadata,
                self.claim,
                deadline=20.0,
            )

        self.assertEqual(caught.exception.code, "media_configuration_error")
        self.assertEqual(storage.lock_calls, 1)
        self.assertEqual(storage.publish_calls, [])
        self.assertEqual(self.prepared.cleanup_calls, 1)

    def test_unsafe_lifecycle_lock_maps_to_closed_configuration_error(self):
        class UnsafeContext(object):
            def __enter__(inner_self):
                raise file_ops.MediaLifecycleLockError(
                    "media_lifecycle_lock_failed"
                )

            def __exit__(inner_self, error_type, error, traceback):
                del error_type, error, traceback
                return False

        storage = _PublishingMediaStorage(self.published, self.events)
        storage.lifecycle_lock = mock.Mock(return_value=UnsafeContext())
        self.service.media_storage = storage

        with self.assertRaises(PinImportError) as caught:
            self.service.commit(
                self.prepared,
                self.user,
                self.metadata,
                self.claim,
                deadline=20.0,
            )

        self.assertEqual(caught.exception.code, "media_configuration_error")
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(storage.publish_calls, [])
        self.assertEqual(self.prepared.cleanup_calls, 1)

    def test_busy_lifecycle_lock_maps_to_retryable_processing_timeout(self):
        class BusyContext(object):
            def __enter__(inner_self):
                raise file_ops.MediaLifecycleLockError(
                    "media_lifecycle_busy",
                    retryable=True,
                )

            def __exit__(inner_self, error_type, error, traceback):
                del error_type, error, traceback
                return False

        storage = _PublishingMediaStorage(self.published, self.events)
        storage.lifecycle_lock = mock.Mock(return_value=BusyContext())
        self.service.media_storage = storage

        with self.assertRaises(PinImportError) as caught:
            self.service.commit(
                self.prepared,
                self.user,
                self.metadata,
                self.claim,
                deadline=20.0,
            )

        self.assertEqual(caught.exception.code, "image_processing_timeout")
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(storage.publish_calls, [])
        self.assertEqual(self.prepared.cleanup_calls, 1)

    def test_lock_wraps_atomic_commit_and_later_callbacks(self):
        events = []
        prepared = _ManifestPreparedAsset(
            uuid.uuid4(),
            "source-name.png",
        )
        published = _PublishedAsset(prepared, events)

        class RecordingContext(object):
            def __init__(inner_self, name):
                inner_self.name = name

            def __enter__(inner_self):
                events.append("{}_enter".format(inner_self.name))
                return inner_self

            def __exit__(inner_self, error_type, error, traceback):
                del error, traceback
                events.append(
                    "{}_exit:{}".format(
                        inner_self.name,
                        error_type.__name__ if error_type else "none"
                    )
                )
                return False

        class RecordingStorage(_PublishingMediaStorage):
            def dedup_lock(
                inner_self,
                prepared_asset,
                submitter_id,
                content_sha256,
                deadline=None,
                clock=None,
            ):
                del prepared_asset, deadline, clock
                self.assertEqual(submitter_id, self.user.pk)
                self.assertEqual(content_sha256, "a" * 64)
                return RecordingContext("stripe")

            def lifecycle_lock(
                inner_self, prepared_asset, deadline=None, clock=None
            ):
                del prepared_asset, deadline, clock
                return RecordingContext("lifecycle")

        def register_callback(event):
            events.append(event)
            if event == "before_idempotency_success":
                transaction.on_commit(lambda: events.append("on_commit"))

        service = PinImportService(
            fetcher=object(),
            media_storage=RecordingStorage(published, events),
            idempotency=_SuccessfulIdempotencyStore(events),
            clock=lambda: 10.0,
            fault_injector=register_callback,
        )

        pin = service.commit(
            prepared,
            self.user,
            self.metadata,
            self.claim,
            deadline=20.0,
        )

        self.assertTrue(Pin.objects.filter(pk=pin.pk).exists())
        self.assertIn("stripe_enter", events)
        self.assertLess(
            events.index("stripe_enter"), events.index("lifecycle_enter")
        )
        self.assertLess(events.index("lifecycle_enter"), events.index("fence"))
        self.assertLess(events.index("fence"), events.index("publish"))
        self.assertLess(events.index("publish"), events.index("on_commit"))
        self.assertLess(
            events.index("on_commit"), events.index("lifecycle_exit:none")
        )
        self.assertLess(
            events.index("lifecycle_exit:none"), events.index("stripe_exit:none")
        )
        self.assertLess(events.index("stripe_exit:none"), events.index("release"))

    def test_callback_base_exception_unlocks_then_rethrows_without_compensation(self):
        events = []
        primary = KeyboardInterrupt()
        prepared = _ManifestPreparedAsset(uuid.uuid4(), "source-name.png")
        published = _PublishedAsset(prepared, events)

        class RecordingContext(object):
            def __enter__(inner_self):
                events.append("lock_enter")
                return inner_self

            def __exit__(inner_self, error_type, error, traceback):
                del error, traceback
                events.append("lock_exit:{}".format(error_type.__name__))
                return False

        class RecordingStorage(_PublishingMediaStorage):
            def lifecycle_lock(
                inner_self, prepared_asset, deadline=None, clock=None
            ):
                del prepared_asset, deadline, clock
                return RecordingContext()

        def register_callback(event):
            events.append(event)
            if event == "before_idempotency_success":
                def raise_primary():
                    events.append("on_commit_base_exception")
                    raise primary

                transaction.on_commit(raise_primary)

        service = PinImportService(
            fetcher=object(),
            media_storage=RecordingStorage(published, events),
            idempotency=_SuccessfulIdempotencyStore(events),
            clock=lambda: 10.0,
            fault_injector=register_callback,
        )

        with self.assertRaises(KeyboardInterrupt) as caught:
            service.commit(
                prepared,
                self.user,
                self.metadata,
                self.claim,
                deadline=20.0,
            )

        self.assertIs(caught.exception, primary)
        self.assertLess(
            events.index("on_commit_base_exception"),
            events.index("lock_exit:KeyboardInterrupt"),
        )
        self.assertLess(
            events.index("lock_exit:KeyboardInterrupt"),
            events.index("release"),
        )
        self.assertEqual(published.compensate_calls, 0)
        self.assertEqual(published.release_calls, 1)
        self.assertEqual(Pin.objects.count(), 1)

    def test_rollback_primary_error_survives_actual_lock_close_error(self):
        primary = ValueError("primary-transaction-error")
        real_flock = file_ops.fcntl.flock

        def fail_unlock(descriptor, operation):
            if operation == file_ops.fcntl.LOCK_UN:
                raise RuntimeError("secondary-lock-close-error")
            return real_flock(descriptor, operation)

        with tempfile.TemporaryDirectory() as media_root:
            root_directory = file_ops.open_media_root(media_root)
            try:
                prepared = _ManifestPreparedAsset(
                    uuid.uuid4(),
                    "source-name.png",
                )
                events = []
                published = _PublishedAsset(prepared, events)
                storage = _ActualLifecyclePublishingStorage(
                    published,
                    events,
                    root_directory,
                )

                def fail_after_image(event):
                    events.append(event)
                    if event == "after_image_row":
                        raise primary

                service = PinImportService(
                    fetcher=object(),
                    media_storage=storage,
                    idempotency=_SuccessfulIdempotencyStore(events),
                    clock=lambda: 10.0,
                    fault_injector=fail_after_image,
                )
                with mock.patch(
                    "django_images.file_ops.fcntl.flock",
                    side_effect=fail_unlock,
                ):
                    with self.assertRaises(ValueError) as caught:
                        service.commit(
                            prepared,
                            self.user,
                            self.metadata,
                            self.claim,
                            deadline=20.0,
                        )

                self.assertIs(caught.exception, primary)
                self.assertEqual(published.compensate_calls, 1)
                self.assertEqual(Pin.objects.count(), 0)
                with file_ops.media_lifecycle_lock(
                    root_directory,
                    exclusive=True,
                    deadline=file_ops.time.monotonic() + 1,
                ):
                    pass
            finally:
                root_directory.close()

    def test_committed_success_survives_actual_lock_close_error(self):
        real_flock = file_ops.fcntl.flock

        def fail_unlock(descriptor, operation):
            if operation == file_ops.fcntl.LOCK_UN:
                raise RuntimeError("secondary-lock-close-error")
            return real_flock(descriptor, operation)

        with tempfile.TemporaryDirectory() as media_root:
            root_directory = file_ops.open_media_root(media_root)
            try:
                prepared = _ManifestPreparedAsset(
                    uuid.uuid4(),
                    "source-name.png",
                )
                events = []
                published = _PublishedAsset(prepared, events)
                service = PinImportService(
                    fetcher=object(),
                    media_storage=_ActualLifecyclePublishingStorage(
                        published,
                        events,
                        root_directory,
                    ),
                    idempotency=_SuccessfulIdempotencyStore(events),
                    clock=lambda: 10.0,
                )
                with self.assertLogs(
                    "core.services.pin_import", level="WARNING"
                ), mock.patch(
                    "django_images.file_ops.fcntl.flock",
                    side_effect=fail_unlock,
                ):
                    pin = service.commit(
                        prepared,
                        self.user,
                        self.metadata,
                        self.claim,
                        deadline=20.0,
                    )

                self.assertTrue(Pin.objects.filter(pk=pin.pk).exists())
                self.assertEqual(published.compensate_calls, 0)
                self.assertEqual(published.release_calls, 1)
            finally:
                root_directory.close()

    def test_callback_base_exception_survives_actual_lock_close_error(self):
        primary = KeyboardInterrupt()
        real_flock = file_ops.fcntl.flock

        def fail_unlock(descriptor, operation):
            if operation == file_ops.fcntl.LOCK_UN:
                raise RuntimeError("secondary-lock-close-error")
            return real_flock(descriptor, operation)

        with tempfile.TemporaryDirectory() as media_root:
            root_directory = file_ops.open_media_root(media_root)
            try:
                prepared = _ManifestPreparedAsset(
                    uuid.uuid4(),
                    "source-name.png",
                )
                events = []
                published = _PublishedAsset(prepared, events)

                def register_callback(event):
                    events.append(event)
                    if event == "before_idempotency_success":
                        transaction.on_commit(lambda: self._raise(primary))

                service = PinImportService(
                    fetcher=object(),
                    media_storage=_ActualLifecyclePublishingStorage(
                        published,
                        events,
                        root_directory,
                    ),
                    idempotency=_SuccessfulIdempotencyStore(events),
                    clock=lambda: 10.0,
                    fault_injector=register_callback,
                )
                with mock.patch(
                    "django_images.file_ops.fcntl.flock",
                    side_effect=fail_unlock,
                ):
                    with self.assertRaises(KeyboardInterrupt) as caught:
                        service.commit(
                            prepared,
                            self.user,
                            self.metadata,
                            self.claim,
                            deadline=20.0,
                        )

                self.assertIs(caught.exception, primary)
                self.assertEqual(Pin.objects.count(), 1)
                self.assertEqual(published.compensate_calls, 0)
                self.assertEqual(published.release_calls, 1)
            finally:
                root_directory.close()

    def test_commit_maps_manifest_to_rows_and_releases_after_commit(self):
        deadline = 20.0

        pin = self.service.commit(
            self.prepared,
            self.user,
            self.metadata,
            self.claim,
            deadline,
        )

        pin.refresh_from_db()
        image = Image.objects.get(pk=pin.image_id)
        self.assertEqual(MediaAsset.objects.count(), 1)
        asset = MediaAsset.objects.get(image=image)
        self.assertEqual(asset.submitter_id, self.user.pk)
        self.assertEqual(asset.content_sha256, "a" * 64)
        self.assertEqual(image.asset_uuid, self.asset_uuid)
        self.assertEqual(image.original_filename, "source-name.png")
        self.assertEqual(
            image.image.name,
            "originals/{}/source-name.png".format(self.asset_uuid),
        )
        self.assertEqual((image.width, image.height), (640, 480))
        thumbnails = list(
            Thumbnail.objects.filter(original=image).order_by("size")
        )
        self.assertEqual(
            [thumbnail.size for thumbnail in thumbnails],
            ["square", "standard", "thumbnail"],
        )
        self.assertEqual(
            {
                thumbnail.size: (
                    thumbnail.image.name,
                    thumbnail.width,
                    thumbnail.height,
                )
                for thumbnail in thumbnails
            },
            {
                "thumbnail": (
                    "derivatives/{}/thumbnail.png".format(
                        self.asset_uuid
                    ),
                    240,
                    180,
                ),
                "standard": (
                    "derivatives/{}/standard.png".format(
                        self.asset_uuid
                    ),
                    600,
                    450,
                ),
                "square": (
                    "derivatives/{}/square.png".format(
                        self.asset_uuid
                    ),
                    125,
                    125,
                ),
            },
        )
        self.assertEqual(pin.submitter, self.user)
        self.assertEqual(pin.url, self.metadata.url)
        self.assertEqual(pin.referer, self.metadata.referer)
        self.assertEqual(pin.description, self.metadata.description)
        self.assertTrue(pin.private)
        self.assertEqual(
            set(pin.tags.names()),
            {"design", "reference"},
        )
        self.assertEqual(Tag.objects.count(), 2)
        self.assertTrue(self.board.pins.filter(pk=pin.pk).exists())
        self.assertEqual(self.published.verify_calls, 2)
        self.assertEqual(self.published.compensate_calls, 0)
        self.assertEqual(self.published.release_calls, 1)
        self.assertEqual(self.prepared.cleanup_calls, 1)
        self.assertEqual(
            self.media_storage.publish_calls,
            [(self.prepared, deadline)],
        )
        self.assertEqual(
            self.events,
            [
                "fence",
                "publish",
                "verify_current",
                "after_image_row",
                "after_thumbnail_rows",
                "after_media_asset_row",
                "after_pin_row",
                "after_tags",
                "after_boards",
                "before_idempotency_success",
                "record_success",
                "verify_current",
                "release",
            ],
        )
        self.assertEqual(Pin.objects.count(), 1)

    def test_later_on_commit_exception_keeps_committed_rows_and_releases(self):
        def inject_later_callback(event):
            self.events.append(event)
            if event == "before_idempotency_success":
                transaction.on_commit(
                    lambda: self._raise(RuntimeError("private-token"))
                )

        self.service.fault_injector = inject_later_callback

        with mock.patch(
            "core.services.pin_import.logger.warning"
        ) as warning:
            pin = self.service.commit(
                self.prepared,
                self.user,
                self.metadata,
                self.claim,
                deadline=20.0,
            )

        self.assertTrue(Pin.objects.filter(pk=pin.pk).exists())
        self.assertEqual(Image.objects.count(), 1)
        self.assertEqual(Thumbnail.objects.count(), 3)
        self.assertEqual(self.published.compensate_calls, 0)
        self.assertEqual(self.published.release_calls, 1)
        warning.assert_called_once_with(
            "pin_import_post_commit_callback_failed error_type=%s",
            "RuntimeError",
        )

    def test_later_on_commit_exception_is_logged_without_raw_message(self):
        def inject_later_callback(event):
            self.events.append(event)
            if event == "before_idempotency_success":
                transaction.on_commit(
                    lambda: self._raise(RuntimeError("private-token"))
                )

        self.service.fault_injector = inject_later_callback

        with self.assertLogs(
            "core.services.pin_import",
            level="WARNING",
        ) as captured:
            pin = self.service.commit(
                self.prepared,
                self.user,
                self.metadata,
                self.claim,
                deadline=20.0,
            )

        rendered = "\n".join(captured.output)
        self.assertTrue(Pin.objects.filter(pk=pin.pk).exists())
        self.assertIn("RuntimeError", rendered)
        self.assertNotIn("private-token", rendered)
        self.assertNotIn(self.metadata.url, rendered)

    def test_logger_failure_does_not_override_committed_success(self):
        def inject_later_callback(event):
            self.events.append(event)
            if event == "before_idempotency_success":
                transaction.on_commit(
                    lambda: self._raise(RuntimeError("private-token"))
                )

        self.service.fault_injector = inject_later_callback

        with mock.patch(
            "core.services.pin_import.logger.warning",
            side_effect=KeyboardInterrupt(),
        ):
            pin = self.service.commit(
                self.prepared,
                self.user,
                self.metadata,
                self.claim,
                deadline=20.0,
            )

        self.assertTrue(Pin.objects.filter(pk=pin.pk).exists())
        self.assertEqual(self.published.compensate_calls, 0)
        self.assertEqual(self.published.release_calls, 1)

    def test_release_failure_retries_and_keeps_committed_success(self):
        prepared = _ManifestPreparedAsset(
            uuid.uuid4(),
            "source-name.png",
        )
        events = []
        published = _ExplodingReleasePublishedAsset(prepared, events)
        storage = _PublishingMediaStorage(published, events)
        service = PinImportService(
            fetcher=object(),
            media_storage=storage,
            idempotency=_SuccessfulIdempotencyStore(events),
            clock=lambda: 10.0,
        )

        pin = service.commit(
            prepared,
            self.user,
            self.metadata,
            self.claim,
            deadline=20.0,
        )

        self.assertTrue(Pin.objects.filter(pk=pin.pk).exists())
        self.assertEqual(published.compensate_calls, 0)
        self.assertEqual(published.release_calls, 2)

    def test_committed_release_base_exception_retries_then_rethrows_first(self):
        primary = KeyboardInterrupt()

        with mock.patch.object(
            self.published,
            "release",
            side_effect=primary,
        ) as release:
            with self.assertRaises(KeyboardInterrupt) as caught:
                self.service.commit(
                    self.prepared,
                    self.user,
                    self.metadata,
                    self.claim,
                    deadline=20.0,
                )

        self.assertIs(caught.exception, primary)
        self.assertEqual(release.call_count, 2)
        self.assertEqual(Pin.objects.count(), 1)
        self.assertEqual(Image.objects.count(), 1)
        self.assertEqual(Thumbnail.objects.count(), 3)

    def test_rollback_cleanup_base_exception_does_not_mask_primary(self):
        primary = ValueError("primary-transaction-error")
        secondary = KeyboardInterrupt()

        def fail_after_image(event):
            self.events.append(event)
            if event == "after_image_row":
                raise primary

        self.service.fault_injector = fail_after_image
        with mock.patch.object(
            self.published,
            "compensate",
            side_effect=secondary,
        ) as compensate, mock.patch.object(
            self.published,
            "release",
            side_effect=secondary,
        ) as release:
            with self.assertRaises(ValueError) as caught:
                self.service.commit(
                    self.prepared,
                    self.user,
                    self.metadata,
                    self.claim,
                    deadline=20.0,
                )

        self.assertIs(caught.exception, primary)
        self.assertEqual(compensate.call_count, 1)
        self.assertEqual(release.call_count, 1)
        self.assertEqual(Pin.objects.count(), 0)
        self.assertEqual(Image.objects.count(), 0)

    def test_later_on_commit_base_exception_rethrows_without_compensate(self):
        def inject_later_callback(event):
            self.events.append(event)
            if event == "before_idempotency_success":
                transaction.on_commit(
                    lambda: self._raise(KeyboardInterrupt())
                )

        self.service.fault_injector = inject_later_callback

        with self.assertRaises(KeyboardInterrupt):
            self.service.commit(
                self.prepared,
                self.user,
                self.metadata,
                self.claim,
                deadline=20.0,
            )

        self.assertEqual(Pin.objects.count(), 1)
        self.assertEqual(Image.objects.count(), 1)
        self.assertEqual(Thumbnail.objects.count(), 3)
        self.assertEqual(self.published.compensate_calls, 0)
        self.assertEqual(self.published.release_calls, 1)

    def test_later_on_commit_system_exit_rethrows_without_compensate(self):
        primary = SystemExit("shutdown")

        def inject_later_callback(event):
            self.events.append(event)
            if event == "before_idempotency_success":
                transaction.on_commit(lambda: self._raise(primary))

        self.service.fault_injector = inject_later_callback

        with self.assertRaises(SystemExit) as caught:
            self.service.commit(
                self.prepared,
                self.user,
                self.metadata,
                self.claim,
                deadline=20.0,
            )

        self.assertIs(caught.exception, primary)
        self.assertEqual(Pin.objects.count(), 1)
        self.assertEqual(Image.objects.count(), 1)
        self.assertEqual(Thumbnail.objects.count(), 3)
        self.assertEqual(self.published.compensate_calls, 0)
        self.assertEqual(self.published.release_calls, 1)

    def test_actual_database_commit_failure_rolls_back_and_compensates(self):
        commit_error = OperationalError("database is locked: private-token")

        with mock.patch.object(
            connection,
            "commit",
            side_effect=commit_error,
        ):
            with self.assertRaises(OperationalError) as caught:
                self.service.commit(
                    self.prepared,
                    self.user,
                    self.metadata,
                    self.claim,
                    deadline=20.0,
                )

        self.assertIs(caught.exception, commit_error)
        self.assertEqual(Pin.objects.count(), 0)
        self.assertEqual(Image.objects.count(), 0)
        self.assertEqual(Thumbnail.objects.count(), 0)
        self.assertEqual(Tag.objects.count(), 0)
        self.assertFalse(self.board.pins.exists())
        self.assertEqual(self.published.compensate_calls, 1)
        self.assertEqual(self.published.release_calls, 0)

    def test_compensation_failure_attempts_release_and_keeps_primary(self):
        prepared = _ManifestPreparedAsset(
            uuid.uuid4(),
            "source-name.png",
        )
        events = []
        published = _ExplodingCompensationPublishedAsset(
            prepared,
            events,
        )
        media_storage = _PublishingMediaStorage(published, events)

        def fail_after_image(event):
            events.append(event)
            if event == "after_image_row":
                raise ValueError("primary-private-token")

        service = PinImportService(
            fetcher=object(),
            media_storage=media_storage,
            idempotency=_SuccessfulIdempotencyStore(events),
            clock=lambda: 10.0,
            fault_injector=fail_after_image,
        )

        with self.assertRaisesRegex(
            ValueError,
            "primary-private-token",
        ):
            service.commit(
                prepared,
                self.user,
                self.metadata,
                self.claim,
                deadline=20.0,
            )

        self.assertEqual(published.compensate_calls, 1)
        self.assertEqual(published.release_calls, 1)
        self.assertEqual(Pin.objects.count(), 0)
        self.assertEqual(Image.objects.count(), 0)

    def test_claim_none_skips_fence_and_success_transition(self):
        idempotency = _IdempotencyStore()
        service = PinImportService(
            fetcher=object(),
            media_storage=self.media_storage,
            idempotency=idempotency,
            clock=lambda: 10.0,
        )

        pin = service.commit(
            self.prepared,
            self.user,
            self.metadata,
            claim=None,
            deadline=20.0,
        )

        self.assertTrue(Pin.objects.filter(pk=pin.pk).exists())
        self.assertEqual(idempotency.fence_calls, 0)
        self.assertEqual(self.published.compensate_calls, 0)
        self.assertEqual(self.published.release_calls, 1)

    def test_deadline_crossed_after_image_hook_stops_before_more_rows(self):
        clock = _ManualClock(10.0)

        def expire_after_image(event):
            self.events.append(event)
            if event == "after_image_row":
                clock.now = 20.0

        self.service.clock = clock
        self.service.fault_injector = expire_after_image

        with self.assertRaises(PinImportError) as caught:
            self.service.commit(
                self.prepared,
                self.user,
                self.metadata,
                self.claim,
                deadline=20.0,
            )

        self.assertEqual(caught.exception.code, "image_processing_timeout")
        self.assertNotIn("after_thumbnail_rows", self.events)
        self.assertEqual(Pin.objects.count(), 0)
        self.assertEqual(Image.objects.count(), 0)
        self.assertEqual(self.published.compensate_calls, 1)

    def test_deadline_crossed_by_board_helper_rolls_back_all_memberships(self):
        second_board = Board.objects.create(
            submitter=self.user,
            name="second-board",
        )
        metadata = ImportMetadata(
            url=self.metadata.url,
            referer=self.metadata.referer,
            description=self.metadata.description,
            private=self.metadata.private,
            tags=self.metadata.tags,
            board_ids=(self.board.pk, second_board.pk),
        )
        clock = _ManualClock(10.0)
        self.service.clock = clock
        real_add = PinMembershipService.add_new_pin_to_locked_boards
        helper_calls = []

        def add_and_expire(service, pin, boards):
            helper_calls.append(tuple(board.pk for board in boards))
            result = real_add(service, pin, boards)
            clock.now = 20.0
            return result

        with mock.patch.object(
            PinMembershipService,
            "add_new_pin_to_locked_boards",
            autospec=True,
            side_effect=add_and_expire,
        ):
            with self.assertRaises(PinImportError) as caught:
                self.service.commit(
                    self.prepared,
                    self.user,
                    metadata,
                    self.claim,
                    deadline=20.0,
                )

        self.assertEqual(caught.exception.code, "image_processing_timeout")
        self.assertEqual(helper_calls, [(self.board.pk, second_board.pk)])
        self.assertEqual(Pin.objects.count(), 0)
        self.assertEqual(Image.objects.count(), 0)
        self.assertFalse(self.board.pins.exists())
        self.assertFalse(second_board.pins.exists())
        self.assertEqual(self.published.compensate_calls, 1)

    def test_deadline_crossed_by_record_success_rolls_back(self):
        clock = _ManualClock(10.0)

        class ExpiringSuccessStore(_SuccessfulIdempotencyStore):
            def record_success(inner_self, claim, pin):
                result = super(
                    ExpiringSuccessStore,
                    inner_self,
                ).record_success(claim, pin)
                clock.now = 20.0
                return result

        self.service.clock = clock
        self.service.idempotency = ExpiringSuccessStore(self.events)

        with self.assertRaises(PinImportError) as caught:
            self.service.commit(
                self.prepared,
                self.user,
                self.metadata,
                self.claim,
                deadline=20.0,
            )

        self.assertEqual(caught.exception.code, "image_processing_timeout")
        self.assertEqual(Pin.objects.count(), 0)
        self.assertEqual(Image.objects.count(), 0)
        self.assertEqual(self.published.compensate_calls, 1)

    def test_each_database_fault_hook_rolls_back_and_compensates(self):
        fault_events = (
            "after_image_row",
            "after_thumbnail_rows",
            "after_pin_row",
            "after_tags",
            "after_boards",
            "before_idempotency_success",
        )

        for event in fault_events:
            with self.subTest(event=event):
                prepared = _ManifestPreparedAsset(
                    uuid.uuid4(),
                    "source-name.png",
                )
                events = []
                published = _PublishedAsset(prepared, events)
                media_storage = _PublishingMediaStorage(
                    published,
                    events,
                )
                idempotency = _SuccessfulIdempotencyStore(events)

                def fail_at(current_event, expected=event):
                    events.append(current_event)
                    if current_event == expected:
                        raise RuntimeError("private-token")

                service = PinImportService(
                    fetcher=object(),
                    media_storage=media_storage,
                    idempotency=idempotency,
                    clock=lambda: 10.0,
                    fault_injector=fail_at,
                )

                with self.assertRaisesRegex(
                    RuntimeError,
                    "private-token",
                ):
                    service.commit(
                        prepared,
                        self.user,
                        self.metadata,
                        self.claim,
                        deadline=20.0,
                    )

                self.assertEqual(Pin.objects.count(), 0)
                self.assertEqual(Image.objects.count(), 0)
                self.assertEqual(Thumbnail.objects.count(), 0)
                self.assertEqual(Tag.objects.count(), 0)
                self.assertFalse(self.board.pins.exists())
                self.assertEqual(published.compensate_calls, 1)
                self.assertEqual(published.release_calls, 0)
                self.assertEqual(prepared.cleanup_calls, 1)

    def test_fence_failure_stops_before_board_query_and_publish(self):
        idempotency = _ConfigurableIdempotencyStore(
            self.events,
            fence_result=False,
        )
        self.service.idempotency = idempotency

        with self.assertRaises(PinImportError) as caught:
            self.service.commit(
                self.prepared,
                self.user,
                self.metadata,
                self.claim,
                deadline=20.0,
            )

        self.assertEqual(caught.exception.code, "lease_lost")
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(self.media_storage.publish_calls, [])
        self.assertEqual(self.prepared.cleanup_calls, 1)
        self.assertEqual(Pin.objects.count(), 0)

    def test_board_delete_or_owner_change_stops_before_publish(self):
        other_user = User.objects.create_user(
            username="changed-board-owner",
            email="changed-board-owner@example.com",
        )
        cases = ("deleted", "owner_changed")

        for state in cases:
            with self.subTest(state=state):
                board = Board.objects.create(
                    submitter=self.user,
                    name="board-{}".format(state),
                )
                metadata = ImportMetadata(
                    url=self.metadata.url,
                    referer=self.metadata.referer,
                    description=self.metadata.description,
                    private=self.metadata.private,
                    tags=self.metadata.tags,
                    board_ids=(board.pk,),
                )
                if state == "deleted":
                    board.delete()
                else:
                    Board.objects.filter(pk=board.pk).update(
                        submitter=other_user
                    )
                prepared = _ManifestPreparedAsset(
                    uuid.uuid4(),
                    "source-name.png",
                )
                events = []
                published = _PublishedAsset(prepared, events)
                storage = _PublishingMediaStorage(published, events)
                service = PinImportService(
                    fetcher=object(),
                    media_storage=storage,
                    idempotency=_SuccessfulIdempotencyStore(events),
                    clock=lambda: 10.0,
                )

                with self.assertRaises(PinImportError) as caught:
                    service.commit(
                        prepared,
                        self.user,
                        metadata,
                        self.claim,
                        deadline=20.0,
                    )

                self.assertEqual(
                    caught.exception.code,
                    "board_access_changed",
                )
                self.assertFalse(caught.exception.retryable)
                self.assertEqual(storage.publish_calls, [])
                self.assertEqual(prepared.cleanup_calls, 1)
                self.assertEqual(Pin.objects.count(), 0)

    def test_record_success_failure_rolls_back_and_compensates(self):
        self.service.idempotency = _ConfigurableIdempotencyStore(
            self.events,
            success_result=False,
        )

        with self.assertRaises(PinImportError) as caught:
            self.service.commit(
                self.prepared,
                self.user,
                self.metadata,
                self.claim,
                deadline=20.0,
            )

        self.assertEqual(caught.exception.code, "lease_lost")
        self.assertEqual(Pin.objects.count(), 0)
        self.assertEqual(Image.objects.count(), 0)
        self.assertEqual(Thumbnail.objects.count(), 0)
        self.assertEqual(Tag.objects.count(), 0)
        self.assertEqual(self.published.compensate_calls, 1)
        self.assertEqual(self.published.release_calls, 0)

    def test_invalid_manifest_rolls_back_and_compensates(self):
        self.published.files = self.published.files[:-1]

        with self.assertRaises(PinImportError) as caught:
            self.service.commit(
                self.prepared,
                self.user,
                self.metadata,
                self.claim,
                deadline=20.0,
            )

        self.assertEqual(caught.exception.code, "internal_error")
        self.assertEqual(Pin.objects.count(), 0)
        self.assertEqual(Image.objects.count(), 0)
        self.assertEqual(self.published.compensate_calls, 1)

    def test_manifest_path_outside_prepared_asset_namespace_is_rejected(self):
        self.published.files[0].final_relative_path = "../foreign.png"

        with self.assertRaises(PinImportError) as caught:
            self.service.commit(
                self.prepared,
                self.user,
                self.metadata,
                self.claim,
                deadline=20.0,
            )

        self.assertEqual(caught.exception.code, "internal_error")
        self.assertEqual(Pin.objects.count(), 0)
        self.assertEqual(Image.objects.count(), 0)
        self.assertEqual(self.published.compensate_calls, 1)

    def test_commit_rejects_original_leaf_not_derived_from_metadata(self):
        original = self.published.files[0]
        object.__setattr__(
            original,
            "final_relative_path",
            "originals/{}/forged.png".format(self.asset_uuid),
        )

        with self.assertRaises(PinImportError) as caught:
            self.service.commit(
                self.prepared,
                self.user,
                self.metadata,
                claim=None,
                deadline=20.0,
            )

        self.assertEqual(caught.exception.code, "internal_error")
        self.assertEqual(Image.objects.count(), 0)

    def test_commit_rejects_format_and_path_pair_not_bound_to_receipt(self):
        published = _ReceiptAwarePublishedAsset(
            self.prepared,
            self.events,
        )
        self.service.media_storage = _PublishingMediaStorage(
            published,
            self.events,
        )
        original = published.files[0]
        original.image_format = "JPEG"
        original.final_relative_path = "originals/{}/source-name.jpg".format(
            self.asset_uuid
        )

        with self.assertRaises(PinImportError) as caught:
            self.service.commit(
                self.prepared,
                self.user,
                self.metadata,
                claim=None,
                deadline=20.0,
            )

        self.assertEqual(caught.exception.code, "internal_error")
        self.assertEqual(Image.objects.count(), 0)
        self.assertEqual(Thumbnail.objects.count(), 0)
        self.assertEqual(Pin.objects.count(), 0)
        self.assertEqual(Tag.objects.count(), 0)
        self.assertFalse(self.board.pins.exists())
        self.assertEqual(published.compensate_calls, 1)
        self.assertEqual(published.destinations, {
            "reused": b"reused",
            "replacement": b"foreign",
        })

    def test_commit_rejects_asset_uuid_and_paths_not_bound_to_receipts(self):
        published = _ReceiptAwarePublishedAsset(
            self.prepared,
            self.events,
        )
        self.service.media_storage = _PublishingMediaStorage(
            published,
            self.events,
        )
        forged_uuid = uuid.UUID("87654321-4321-8765-4321-876543218765")
        self.prepared.asset_uuid = forged_uuid
        published.asset_uuid = forged_uuid
        for entry in published.files:
            parent = "originals" if entry.kind == "original" else "derivatives"
            entry.final_relative_path = "{}/{}/{}".format(
                parent,
                forged_uuid,
                entry.destination_name,
            )

        with self.assertRaises(PinImportError) as caught:
            self.service.commit(
                self.prepared,
                self.user,
                self.metadata,
                claim=None,
                deadline=20.0,
            )

        self.assertEqual(caught.exception.code, "internal_error")
        self.assertEqual(Image.objects.count(), 0)
        self.assertEqual(Thumbnail.objects.count(), 0)
        self.assertEqual(Pin.objects.count(), 0)
        self.assertEqual(Tag.objects.count(), 0)
        self.assertFalse(self.board.pins.exists())
        self.assertEqual(published.compensate_calls, 1)
        self.assertEqual(published.destinations, {
            "reused": b"reused",
            "replacement": b"foreign",
        })

    def test_publish_error_cleans_staging_and_creates_no_rows(self):
        storage_error = MediaStorageError(
            "media_storage_failed",
            "The media storage is unavailable.",
            True,
        )
        storage = _RaisingMediaStorage(storage_error)
        self.service.media_storage = storage

        with self.assertRaises(MediaStorageError) as caught:
            self.service.commit(
                self.prepared,
                self.user,
                self.metadata,
                self.claim,
                deadline=20.0,
            )

        self.assertIs(caught.exception, storage_error)
        self.assertEqual(self.prepared.cleanup_calls, 1)
        self.assertEqual(Pin.objects.count(), 0)
        self.assertEqual(Image.objects.count(), 0)

    def test_reused_and_replacement_destinations_survive_database_fault(self):
        prepared = _ManifestPreparedAsset(
            uuid.uuid4(),
            "source-name.png",
        )
        events = []
        published = _ReceiptAwarePublishedAsset(prepared, events)
        storage = _PublishingMediaStorage(published, events)

        def fail_after_image(event):
            events.append(event)
            if event == "after_image_row":
                raise RuntimeError("database-fault")

        service = PinImportService(
            fetcher=object(),
            media_storage=storage,
            idempotency=_SuccessfulIdempotencyStore(events),
            clock=lambda: 10.0,
            fault_injector=fail_after_image,
        )

        with self.assertRaises(RuntimeError):
            service.commit(
                prepared,
                self.user,
                self.metadata,
                self.claim,
                deadline=20.0,
            )

        self.assertEqual(
            published.destinations,
            {
                "reused": b"reused",
                "replacement": b"foreign",
            },
        )
        self.assertEqual(Pin.objects.count(), 0)

    def test_verify_failure_rolls_back_and_preserves_foreign(self):
        prepared = _ManifestPreparedAsset(
            uuid.uuid4(),
            "source-name.png",
        )
        events = []
        published = _FailingSecondVerifyPublishedAsset(prepared, events)
        storage = _PublishingMediaStorage(published, events)
        service = PinImportService(
            fetcher=object(),
            media_storage=storage,
            idempotency=_SuccessfulIdempotencyStore(events),
            clock=lambda: 10.0,
        )

        with self.assertRaises(MediaStorageError) as caught:
            service.commit(
                prepared,
                self.user,
                self.metadata,
                self.claim,
                deadline=20.0,
            )

        self.assertEqual(caught.exception.code, "media_publish_changed")
        self.assertEqual(Pin.objects.count(), 0)
        self.assertEqual(Image.objects.count(), 0)
        self.assertEqual(published.compensate_calls, 1)
        self.assertEqual(published.foreign_destination, b"foreign")

    @staticmethod
    def _raise(error):
        raise error


class PinImportRealVerticalTests(
    _LinuxStrongPublishMixin,
    TemporaryMediaMixin,
    TransactionTestCase,
):
    def setUp(self):
        super(PinImportRealVerticalTests, self).setUp()
        self.user = User.objects.create_user(
            username="real-vertical-owner",
            email="real-vertical-owner@example.com",
        )
        self.board = Board.objects.create(
            submitter=self.user,
            name="real-vertical-board",
        )
        self.now = datetime.datetime(
            2026,
            8,
            20,
            4,
            5,
            6,
            tzinfo=datetime.timezone.utc,
        )
        self.fetched = _png_fetched_image(
            "https://cdn.example/final/%EC%9B%90%EB%B3%B8.png?secret=1"
        )
        self.fetcher = _Fetcher(self.fetched)
        self.storage = MediaStorage(
            media_root=self.temporary_media.name,
            clock=lambda: 10.0,
        )
        self.idempotency = IdempotencyStore(
            wall_clock=lambda: self.now,
            lease_seconds=300,
        )
        self.service = PinImportService(
            fetcher=self.fetcher,
            media_storage=self.storage,
            idempotency=self.idempotency,
            clock=lambda: 10.0,
        )
        self.batch_id = uuid.UUID(
            "30000000-0000-0000-0000-000000000001"
        )
        self.item_id = uuid.UUID(
            "40000000-0000-0000-0000-000000000001"
        )
        self.metadata = ImportMetadata(
            url="https://origin.example/photo",
            referer="https://origin.example/page",
            description="real vertical",
            private=False,
            tags=("archive", "reference"),
            board_ids=(self.board.pk,),
        )

    def test_prepare_rejects_media_root_different_from_model_storage(self):
        with tempfile.TemporaryDirectory() as foreign_root:
            storage = MediaStorage(
                media_root=foreign_root,
                clock=lambda: 10.0,
            )
            prepared_assets = []

            def cleanup_prepared():
                for prepared in prepared_assets:
                    prepared.cleanup()

            self.addCleanup(cleanup_prepared)
            with self.assertRaises(MediaStorageError) as caught:
                prepared_assets.append(storage.prepare(
                    self.fetched,
                    asset_uuid=uuid.uuid4(),
                    original_filename="foreign-root.png",
                    deadline=20.0,
                ))

            self.assertEqual(
                caught.exception.code,
                "media_configuration_error",
            )
            self.assertFalse(caught.exception.retryable)
            self.assertEqual(_file_snapshot(foreign_root), {})
            self.assertEqual(
                _file_snapshot(self.temporary_media.name),
                {},
            )

    def test_real_storage_and_idempotency_commit_one_complete_asset(self):
        fingerprint = self._fingerprint()
        claim = self.idempotency.claim(
            self.user,
            self.batch_id,
            self.item_id,
            fingerprint,
            self.now,
        )
        deadline = 20.0

        prepared = self.service.prepare_url(
            self.metadata.url,
            self.metadata.referer,
            deadline,
        )
        asset_uuid = prepared.asset_uuid
        pin = self.service.commit(
            prepared,
            self.user,
            self.metadata,
            claim,
            deadline,
        )

        row = BatchImportItem.objects.get(pk=claim.row_id)
        image = Image.objects.get(pk=pin.image_id)
        thumbnails = Thumbnail.objects.filter(original=image)
        snapshot = _file_snapshot(self.temporary_media.name)
        self.assertEqual(row.state, BatchImportItem.SUCCEEDED)
        self.assertEqual(row.pin_id, pin.pk)
        self.assertEqual(image.asset_uuid, asset_uuid)
        self.assertEqual(image.original_filename, "원본.png")
        self.assertEqual(thumbnails.count(), 3)
        self.assertEqual(
            set(thumbnails.values_list("size", flat=True)),
            {"thumbnail", "standard", "square"},
        )
        expected_paths = {
            "originals/{}/원본.png".format(asset_uuid),
            "derivatives/{}/thumbnail.png".format(asset_uuid),
            "derivatives/{}/standard.png".format(asset_uuid),
            "derivatives/{}/square.png".format(asset_uuid),
        }
        self.assertEqual(
            image.image.name,
            "originals/{}/원본.png".format(asset_uuid),
        )
        self.assertEqual(set(snapshot), expected_paths)
        self.assertEqual(snapshot[image.image.name], self.fetched.content)
        self.assertTrue(self.board.pins.filter(pk=pin.pk).exists())
        self.assertEqual(set(pin.tags.names()), {"archive", "reference"})
        self.assertEqual(Pin.objects.count(), 1)
        self.assertEqual(Image.objects.count(), 1)

    def test_batch_process_forwards_exact_context_and_commits_asset(self):
        data = self._batch_data()
        expected_metadata = ImportMetadata(
            url=self.metadata.url,
            referer=self.metadata.referer,
            description=self.metadata.description,
            private=self.metadata.private,
            tags=self.metadata.tags,
            board_ids=self.metadata.board_ids,
        )
        batch_service = self._batch_service()

        with mock.patch.object(
            self.service,
            "prepare_url",
            wraps=self.service.prepare_url,
        ) as prepare_url, mock.patch.object(
            self.service,
            "commit",
            wraps=self.service.commit,
        ) as commit:
            result = batch_service.process(
                self.user,
                data,
                started_at=10.0,
            )

        created = result["results"][0]
        pin = Pin.objects.get(pk=created["pin_id"])
        image = Image.objects.get(pk=pin.image_id)
        row = BatchImportItem.objects.get(
            submitter=self.user,
            client_item_id=self.item_id,
        )
        commit_args = commit.call_args[0]
        self.assertEqual(created["status"], "created")
        self.assertEqual(
            result["summary"],
            {"created": 1, "replayed": 0, "failed": 0, "conflict": 0},
        )
        prepare_url.assert_called_once_with(
            self.metadata.url,
            self.metadata.referer,
            22.0,
        )
        self.assertIs(commit_args[1], self.user)
        self.assertEqual(commit_args[2], expected_metadata)
        self.assertIsNotNone(commit_args[3])
        self.assertEqual(commit_args[3].kind, "claimed")
        self.assertEqual(commit_args[3].row_id, row.pk)
        self.assertEqual(commit_args[4], 22.0)
        self.assertEqual(row.request_fingerprint, self._fingerprint())
        self.assertEqual(row.state, BatchImportItem.SUCCEEDED)
        self.assertEqual(row.pin_id, pin.pk)
        self.assertEqual(row.batch_id, self.batch_id)
        self.assertEqual(pin.submitter, self.user)
        self.assertEqual(pin.url, self.metadata.url)
        self.assertEqual(pin.referer, self.metadata.referer)
        self.assertEqual(pin.description, self.metadata.description)
        self.assertEqual(pin.private, self.metadata.private)
        self.assertEqual(set(pin.tags.names()), set(self.metadata.tags))
        self.assertTrue(self.board.pins.filter(pk=pin.pk).exists())
        self.assertEqual(image.original_filename, "원본.png")
        self.assertEqual(Thumbnail.objects.filter(original=image).count(), 3)
        self.assertEqual(len(_file_snapshot(self.temporary_media.name)), 4)

    def test_batch_response_loss_replays_without_more_side_effects(self):
        hook_calls = []

        def lose_response(event):
            hook_calls.append(event)
            if event == "after_commit":
                raise RuntimeError("response lost")

        batch_service = self._batch_service(
            fault_injector=lose_response,
        )
        data = self._batch_data()
        with mock.patch.object(
            self.storage,
            "prepare",
            wraps=self.storage.prepare,
        ) as prepare, mock.patch.object(
            self.storage,
            "publish",
            wraps=self.storage.publish,
        ) as publish, mock.patch.object(
            self.service,
            "commit",
            wraps=self.service.commit,
        ) as commit:
            with self.assertRaisesRegex(RuntimeError, "response lost"):
                batch_service.process(
                    self.user,
                    data,
                    started_at=10.0,
                )
            replay = batch_service.process(
                self.user,
                data,
                started_at=10.0,
            )

        row = BatchImportItem.objects.get(
            submitter=self.user,
            client_item_id=self.item_id,
        )
        self.assertEqual(replay["results"][0]["status"], "replayed")
        self.assertEqual(replay["results"][0]["pin_id"], row.pin_id)
        self.assertEqual(len(self.fetcher.calls), 1)
        self.assertEqual(prepare.call_count, 1)
        self.assertEqual(publish.call_count, 1)
        self.assertEqual(commit.call_count, 1)
        self.assertEqual(hook_calls, ["after_commit"])
        self.assertEqual(row.state, BatchImportItem.SUCCEEDED)
        self.assertEqual(Pin.objects.count(), 1)
        self.assertEqual(Image.objects.count(), 1)
        self.assertEqual(Thumbnail.objects.count(), 3)
        self.assertEqual(len(_file_snapshot(self.temporary_media.name)), 4)

    def test_batch_post_prepare_timeout_retries_retained_descriptor(self):
        prepared_assets = []
        real_prepare = self.service.prepare_url

        def capture_prepared(url, referer, deadline):
            prepared = real_prepare(url, referer, deadline)
            prepared_assets.append(prepared)
            return prepared

        def cleanup_prepared_assets():
            for prepared in prepared_assets:
                prepared.cleanup()

        self.addCleanup(cleanup_prepared_assets)
        clock_values = iter((10.0, 22.0))
        batch_service = BatchImportService(
            fetcher=self.fetcher,
            media_storage=self.storage,
            idempotency=self.idempotency,
            pin_import=self.service,
            clock=lambda: next(clock_values),
            wall_clock=lambda: self.now,
        )

        with mock.patch.object(
            self.service,
            "prepare_url",
            side_effect=capture_prepared,
        ), mock.patch.object(
            self.service,
            "commit",
            wraps=self.service.commit,
        ) as commit, self._fail_first_descriptor_close():
            result = batch_service.process(
                self.user,
                self._batch_data(),
                started_at=10.0,
            )

        self.assertEqual(result["results"][0]["status"], "failed")
        self.assertEqual(
            result["results"][0]["error"]["code"],
            "image_processing_timeout",
        )
        self.assertEqual(commit.call_count, 0)
        self.assertEqual(len(prepared_assets), 1)
        self.assertFalse(prepared_assets[0].is_open)
        self.assertEqual(Pin.objects.count(), 0)
        self.assertEqual(Image.objects.count(), 0)
        self.assertEqual(_file_snapshot(self.temporary_media.name), {})

    def test_single_prepare_timeout_retries_actual_retained_descriptor(self):
        prepared_assets = []
        real_prepare = self.storage.prepare

        def capture_prepared(*args, **kwargs):
            prepared = real_prepare(*args, **kwargs)
            prepared_assets.append(prepared)
            return prepared

        service = PinImportService(
            fetcher=self.fetcher,
            media_storage=self.storage,
            idempotency=self.idempotency,
            clock=lambda: 20.0,
        )
        with mock.patch.object(
            self.storage,
            "prepare",
            side_effect=capture_prepared,
        ), mock.patch.object(
            self.storage,
            "publish",
            wraps=self.storage.publish,
        ) as publish, self._fail_first_descriptor_close():
            with self.assertRaises(PinImportError) as caught:
                service.prepare_url(
                    self.metadata.url,
                    self.metadata.referer,
                    deadline=20.0,
                )

        self.assertEqual(caught.exception.code, "image_processing_timeout")
        self.assertEqual(len(prepared_assets), 1)
        self.assertFalse(prepared_assets[0].is_open)
        self.assertEqual(publish.call_count, 0)
        self.assertEqual(Pin.objects.count(), 0)
        self.assertEqual(Image.objects.count(), 0)
        self.assertEqual(_file_snapshot(self.temporary_media.name), {})

    def test_prepublish_cleanup_retries_one_retained_descriptor(self):
        prepared = self.service.prepare_url(
            self.metadata.url,
            self.metadata.referer,
            deadline=20.0,
        )
        self.addCleanup(prepared.cleanup)

        with self._fail_first_descriptor_close():
            with transaction.atomic():
                with self.assertRaises(PinImportError) as caught:
                    self.service.commit(
                        prepared,
                        self.user,
                        self.metadata,
                        claim=None,
                        deadline=20.0,
                    )

        self.assertEqual(caught.exception.code, "internal_error")
        self.assertFalse(prepared.is_open)
        self.assertEqual(Pin.objects.count(), 0)
        self.assertEqual(Image.objects.count(), 0)
        self.assertEqual(_file_snapshot(self.temporary_media.name), {})

    def test_commit_release_retries_one_retained_descriptor(self):
        claim = self.idempotency.claim(
            self.user,
            self.batch_id,
            self.item_id,
            self._fingerprint(),
            self.now,
        )
        prepared = self.service.prepare_url(
            self.metadata.url,
            self.metadata.referer,
            deadline=20.0,
        )
        self.addCleanup(prepared.cleanup)

        with self._fail_first_descriptor_close():
            pin = self.service.commit(
                prepared,
                self.user,
                self.metadata,
                claim,
                deadline=20.0,
            )

        self.assertFalse(prepared.is_open)
        self.assertTrue(Pin.objects.filter(pk=pin.pk).exists())
        self.assertEqual(Image.objects.count(), 1)
        self.assertEqual(Thumbnail.objects.count(), 3)
        self.assertEqual(len(_file_snapshot(self.temporary_media.name)), 4)

    def test_rollback_release_retries_one_retained_descriptor(self):
        claim = self.idempotency.claim(
            self.user,
            self.batch_id,
            self.item_id,
            self._fingerprint(),
            self.now,
        )
        prepared = self.service.prepare_url(
            self.metadata.url,
            self.metadata.referer,
            deadline=20.0,
        )
        self.addCleanup(prepared.cleanup)

        def fail_after_image(event):
            if event == "after_image_row":
                raise ValueError("primary-private-token")

        self.service.fault_injector = fail_after_image
        with self._fail_first_descriptor_close():
            with self.assertRaisesRegex(
                ValueError,
                "primary-private-token",
            ):
                self.service.commit(
                    prepared,
                    self.user,
                    self.metadata,
                    claim,
                    deadline=20.0,
                )

        self.assertFalse(prepared.is_open)
        self.assertEqual(Pin.objects.count(), 0)
        self.assertEqual(Image.objects.count(), 0)
        self.assertEqual(Thumbnail.objects.count(), 0)
        self.assertEqual(_file_snapshot(self.temporary_media.name), {})

    def test_url_then_local_same_bytes_reuses_first_manifest(self):
        first_prepared = self.service.prepare_url(
            self.metadata.url,
            self.metadata.referer,
            deadline=20.0,
        )
        first = self.service.commit(
            first_prepared,
            self.user,
            self.metadata,
            claim=None,
            deadline=20.0,
        )
        local_metadata = ImportMetadata(
            url=None,
            referer="local-device",
            description="local metadata",
            private=True,
            tags=("local",),
            board_ids=(self.board.pk,),
        )
        second_prepared = self.service.prepare_upload(
            _uploaded_png(self.fetched.content, "renamed-local.png"),
            deadline=20.0,
        )
        self.assertNotEqual(
            first_prepared.asset_uuid,
            second_prepared.asset_uuid,
        )

        second = self.service.commit(
            second_prepared,
            self.user,
            local_metadata,
            claim=None,
            deadline=20.0,
        )

        image = Image.objects.get(pk=first.image_id)
        second.refresh_from_db()
        self.assertEqual(second.image_id, first.image_id)
        self.assertEqual(image.original_filename, "원본.png")
        self.assertEqual(second.description, "local metadata")
        self.assertTrue(second.private)
        self.assertEqual(set(second.tags.names()), {"local"})
        self.assertEqual(Pin.objects.count(), 2)
        self.assertEqual(Image.objects.count(), 1)
        self.assertEqual(Thumbnail.objects.count(), 3)
        self.assertEqual(MediaAsset.objects.count(), 1)
        self.assertEqual(len(_file_snapshot(self.temporary_media.name)), 4)

    def test_local_then_batch_same_bytes_reuses_local_manifest(self):
        local_metadata = ImportMetadata(
            url=None,
            referer=None,
            description="local first",
            private=False,
            tags=(),
            board_ids=(),
        )
        prepared = self.service.prepare_upload(
            _uploaded_png(self.fetched.content, "local-first.png"),
            deadline=20.0,
        )
        first = self.service.commit(
            prepared,
            self.user,
            local_metadata,
            claim=None,
            deadline=20.0,
        )

        result = self._batch_service().process(
            self.user,
            self._batch_data(),
            started_at=10.0,
        )

        second = Pin.objects.get(pk=result["results"][0]["pin_id"])
        image = Image.objects.get(pk=first.image_id)
        self.assertEqual(result["results"][0]["status"], "created")
        self.assertEqual(second.image_id, first.image_id)
        self.assertEqual(image.original_filename, "local-first.png")
        self.assertEqual(second.url, self.metadata.url)
        self.assertEqual(second.description, self.metadata.description)
        self.assertEqual(Pin.objects.count(), 2)
        self.assertEqual(Image.objects.count(), 1)
        self.assertEqual(MediaAsset.objects.count(), 1)
        self.assertEqual(len(_file_snapshot(self.temporary_media.name)), 4)

    def test_different_users_with_same_bytes_create_separate_assets(self):
        first = self._commit_initial_asset()
        other = User.objects.create_user(
            username="second-asset-owner",
            email="second-asset-owner@example.com",
        )
        second_prepared = self.service.prepare_upload(
            _uploaded_png(self.fetched.content, "other-owner.png"),
            deadline=20.0,
        )
        second = self.service.commit(
            second_prepared,
            other,
            ImportMetadata(
                url=None,
                referer=None,
                description="other owner",
                private=False,
                tags=(),
                board_ids=(),
            ),
            claim=None,
            deadline=20.0,
        )

        self.assertNotEqual(first.image_id, second.image_id)
        self.assertEqual(Pin.objects.count(), 2)
        self.assertEqual(Image.objects.count(), 2)
        self.assertEqual(Thumbnail.objects.count(), 6)
        self.assertEqual(MediaAsset.objects.count(), 2)
        self.assertEqual(len(_file_snapshot(self.temporary_media.name)), 8)

    def test_same_pixels_with_different_bytes_create_separate_assets(self):
        first_output = BytesIO()
        second_output = BytesIO()
        image = PILImage.new("RGB", (40, 30), "red")
        image.save(first_output, format="PNG", compress_level=0)
        image.save(second_output, format="PNG", compress_level=9)
        image.close()
        first_content = first_output.getvalue()
        second_content = second_output.getvalue()
        self.assertNotEqual(first_content, second_content)
        metadata = ImportMetadata(
            url=None,
            referer=None,
            description="same pixels",
            private=False,
            tags=(),
            board_ids=(),
        )

        first = self.service.commit(
            self.service.prepare_upload(
                _uploaded_png(first_content, "pixels-a.png"),
                deadline=20.0,
            ),
            self.user,
            metadata,
            claim=None,
            deadline=20.0,
        )
        second = self.service.commit(
            self.service.prepare_upload(
                _uploaded_png(second_content, "pixels-b.png"),
                deadline=20.0,
            ),
            self.user,
            metadata,
            claim=None,
            deadline=20.0,
        )

        self.assertNotEqual(first.image_id, second.image_id)
        self.assertEqual(Image.objects.count(), 2)
        self.assertEqual(MediaAsset.objects.count(), 2)
        self.assertEqual(len(_file_snapshot(self.temporary_media.name)), 8)

    def test_reuse_rejects_missing_existing_final_without_new_state(self):
        first = self._commit_initial_asset()
        image = Image.objects.get(pk=first.image_id)
        missing = Path(
            self.temporary_media.name,
            image.thumbnail_set.get(size="square").image.name,
        )
        missing.unlink()

        self._assert_reuse_conflict_preserves_current_files()

    def test_reuse_rejects_existing_hash_mismatch_without_overwrite(self):
        first = self._commit_initial_asset()
        image = Image.objects.get(pk=first.image_id)
        original = Path(self.temporary_media.name, image.image.name)
        original.write_bytes(b"foreign replacement bytes")

        self._assert_reuse_conflict_preserves_current_files()

    def test_reuse_oversized_final_is_rejected_before_hash_or_decode(self):
        first = self._commit_initial_asset()
        image = Image.objects.get(pk=first.image_id)
        original = Path(self.temporary_media.name, image.image.name)
        original.write_bytes(original.read_bytes() + (b"x" * 1024))
        prepared = self.service.prepare_upload(
            _uploaded_png(self.fetched.content, "oversized-final.png"),
            deadline=20.0,
        )
        final_descriptors = set()
        inspected_finals = []
        hashed_finals = []
        compared_finals = []
        real_open = file_ops._open_regular_nofollow
        real_inspect = self.storage._inspect
        real_hash = file_ops.sha256_file_descriptor
        real_compare = self.storage._same_descriptor_bytes

        def capture_open(directory_descriptor, name):
            descriptor = real_open(directory_descriptor, name)
            final_descriptors.add(descriptor)
            return descriptor

        def record_inspect(descriptor):
            if descriptor in final_descriptors:
                inspected_finals.append(descriptor)
            return real_inspect(descriptor)

        def record_hash(descriptor):
            if descriptor in final_descriptors:
                hashed_finals.append(descriptor)
            return real_hash(descriptor)

        def record_compare(left, right):
            if right in final_descriptors:
                compared_finals.append(right)
            return real_compare(left, right)

        with mock.patch(
            "core.services.media_storage._open_regular_nofollow",
            side_effect=capture_open,
        ), mock.patch.object(
            self.storage,
            "_inspect",
            side_effect=record_inspect,
        ), mock.patch(
            "core.services.media_storage.sha256_file_descriptor",
            side_effect=record_hash,
        ), mock.patch.object(
            self.storage,
            "_same_descriptor_bytes",
            side_effect=record_compare,
        ):
            with self.assertRaises(MediaStorageError) as caught:
                self.service.commit(
                    prepared,
                    self.user,
                    self.metadata,
                    claim=None,
                    deadline=20.0,
                )

        self.assertEqual(caught.exception.code, "media_path_conflict")
        self.assertEqual(len(final_descriptors), 1)
        self.assertEqual(inspected_finals, [])
        self.assertEqual(hashed_finals, [])
        self.assertEqual(compared_finals, [])
        self.assertFalse(prepared.is_open)
        self.assertEqual(Pin.objects.count(), 1)

    def test_reuse_corrupt_same_size_final_is_hashed_before_decode(self):
        first = self._commit_initial_asset()
        image = Image.objects.get(pk=first.image_id)
        original = Path(self.temporary_media.name, image.image.name)
        original.write_bytes(b"x" * len(original.read_bytes()))
        prepared = self.service.prepare_upload(
            _uploaded_png(self.fetched.content, "corrupt-final.png"),
            deadline=20.0,
        )
        final_descriptors = set()
        inspected_finals = []
        hashed_finals = []
        real_open = file_ops._open_regular_nofollow
        real_inspect = self.storage._inspect
        real_hash = file_ops.sha256_file_descriptor

        def capture_open(directory_descriptor, name):
            descriptor = real_open(directory_descriptor, name)
            final_descriptors.add(descriptor)
            return descriptor

        def record_inspect(descriptor):
            if descriptor in final_descriptors:
                inspected_finals.append(descriptor)
            return real_inspect(descriptor)

        def record_hash(descriptor):
            if descriptor in final_descriptors:
                hashed_finals.append(descriptor)
            return real_hash(descriptor)

        with mock.patch(
            "core.services.media_storage._open_regular_nofollow",
            side_effect=capture_open,
        ), mock.patch.object(
            self.storage,
            "_inspect",
            side_effect=record_inspect,
        ), mock.patch(
            "core.services.media_storage.sha256_file_descriptor",
            side_effect=record_hash,
        ):
            with self.assertRaises(MediaStorageError) as caught:
                self.service.commit(
                    prepared,
                    self.user,
                    self.metadata,
                    claim=None,
                    deadline=20.0,
                )

        self.assertEqual(caught.exception.code, "media_path_conflict")
        self.assertEqual(len(final_descriptors), 1)
        self.assertEqual(len(hashed_finals), 1)
        self.assertEqual(inspected_finals, [])
        self.assertFalse(prepared.is_open)
        self.assertEqual(Pin.objects.count(), 1)

    def test_reuse_rejects_database_dimension_mismatch(self):
        first = self._commit_initial_asset()
        Image.objects.filter(pk=first.image_id).update(width=641)

        self._assert_reuse_conflict_preserves_current_files()

    def test_reuse_rejects_noncanonical_database_path_and_preserves_external(
        self,
    ):
        first = self._commit_initial_asset()
        external = Path(self.temporary_media.name, "external.png")
        external.write_bytes(b"external-file")
        Image.objects.filter(pk=first.image_id).update(image="external.png")

        self._assert_reuse_conflict_preserves_current_files()
        self.assertEqual(external.read_bytes(), b"external-file")

    def test_reuse_name_replacement_after_receipt_rolls_back_without_delete(
        self,
    ):
        first = self._commit_initial_asset()
        image = Image.objects.get(pk=first.image_id)
        current = Path(self.temporary_media.name, image.image.name)
        held = current.with_name("held-{}".format(current.name))
        original_content = current.read_bytes()
        prepared = self.service.prepare_upload(
            _uploaded_png(self.fetched.content, "replacement-name.png"),
            deadline=20.0,
        )

        def replace_name(event):
            if event == "after_reusable_verified":
                current.rename(held)
                current.write_bytes(original_content)

        self.service.fault_injector = replace_name

        with self.assertRaises(MediaStorageError) as caught:
            self.service.commit(
                prepared,
                self.user,
                self.metadata,
                claim=None,
                deadline=20.0,
            )

        self.assertEqual(caught.exception.code, "media_path_conflict")
        self.assertFalse(prepared.is_open)
        self.assertEqual(Pin.objects.count(), 1)
        self.assertEqual(Image.objects.count(), 1)
        self.assertEqual(MediaAsset.objects.count(), 1)
        self.assertEqual(current.read_bytes(), original_content)
        self.assertEqual(held.read_bytes(), original_content)

    def test_reuse_file_mutation_after_receipt_rolls_back_without_delete(self):
        first = self._commit_initial_asset()
        image = Image.objects.get(pk=first.image_id)
        current = Path(self.temporary_media.name, image.image.name)
        replacement = b"foreign file mutation"
        prepared = self.service.prepare_upload(
            _uploaded_png(self.fetched.content, "mutated-name.png"),
            deadline=20.0,
        )

        def mutate_file(event):
            if event == "after_reusable_verified":
                current.write_bytes(replacement)

        self.service.fault_injector = mutate_file

        with self.assertRaises(MediaStorageError) as caught:
            self.service.commit(
                prepared,
                self.user,
                self.metadata,
                claim=None,
                deadline=20.0,
            )

        self.assertEqual(caught.exception.code, "media_path_conflict")
        self.assertFalse(prepared.is_open)
        self.assertEqual(Pin.objects.count(), 1)
        self.assertEqual(Image.objects.count(), 1)
        self.assertEqual(MediaAsset.objects.count(), 1)
        self.assertEqual(current.read_bytes(), replacement)

    def test_reuse_record_success_mutation_is_reverified_before_commit(self):
        first = self._commit_initial_asset()
        image = Image.objects.get(pk=first.image_id)
        original = Path(self.temporary_media.name, image.image.name)
        original_content = original.read_bytes()
        replacement = (
            bytes([original_content[0] ^ 1]) + original_content[1:]
        )
        prepared = self.service.prepare_upload(
            _uploaded_png(self.fetched.content, "record-success-race.png"),
            deadline=20.0,
        )
        claim = self.idempotency.claim(
            self.user,
            self.batch_id,
            self.item_id,
            self._fingerprint(),
            self.now,
        )
        real_store = self.idempotency

        class MutatingSuccessStore(object):
            def fence(inner_self, current_claim):
                return real_store.fence(current_claim)

            def record_success(inner_self, current_claim, pin):
                result = real_store.record_success(current_claim, pin)
                original.write_bytes(replacement)
                return result

        self.service.idempotency = MutatingSuccessStore()

        with self.assertRaises(MediaStorageError) as caught:
            self.service.commit(
                prepared,
                self.user,
                self.metadata,
                claim=claim,
                deadline=20.0,
            )

        row = BatchImportItem.objects.get(pk=claim.row_id)
        self.assertEqual(caught.exception.code, "media_path_conflict")
        self.assertEqual(row.state, BatchImportItem.PENDING)
        self.assertIsNone(row.pin_id)
        self.assertFalse(prepared.is_open)
        self.assertEqual(Pin.objects.count(), 1)
        self.assertEqual(Image.objects.count(), 1)
        self.assertEqual(MediaAsset.objects.count(), 1)
        self.assertEqual(original.read_bytes(), replacement)

    def test_reuse_root_replacement_after_receipt_rolls_back_without_delete(
        self,
    ):
        self._commit_initial_asset()
        snapshot = _file_snapshot(self.temporary_media.name)
        prepared = self.service.prepare_upload(
            _uploaded_png(self.fetched.content, "root-replaced.png"),
            deadline=20.0,
        )
        current_root = Path(self.temporary_media.name)
        held_root = current_root.with_name(
            "{}-held".format(current_root.name)
        )
        sentinel = current_root / "external-sentinel"

        def replace_root(event):
            if event == "after_reusable_verified":
                current_root.rename(held_root)
                current_root.mkdir()
                sentinel.write_bytes(b"external-root")

        self.service.fault_injector = replace_root

        try:
            with self.assertRaises(MediaStorageError) as caught:
                self.service.commit(
                    prepared,
                    self.user,
                    self.metadata,
                    claim=None,
                    deadline=20.0,
                )

            self.assertEqual(caught.exception.code, "media_path_conflict")
            self.assertFalse(prepared.is_open)
            self.assertEqual(Pin.objects.count(), 1)
            self.assertEqual(Image.objects.count(), 1)
            self.assertEqual(MediaAsset.objects.count(), 1)
            self.assertEqual(sentinel.read_bytes(), b"external-root")
            self.assertEqual(_file_snapshot(str(held_root)), snapshot)
        finally:
            if sentinel.exists():
                sentinel.unlink()
            if current_root.exists():
                current_root.rmdir()
            if held_root.exists():
                held_root.rename(current_root)

    def test_reuse_name_replacement_during_final_hash_is_rejected(self):
        first = self._commit_initial_asset()
        image = Image.objects.get(pk=first.image_id)
        current = Path(self.temporary_media.name, image.image.name)
        held = current.with_name("hash-held-{}".format(current.name))
        original_content = current.read_bytes()
        prepared = self.service.prepare_upload(
            _uploaded_png(self.fetched.content, "hash-name-race.png"),
            deadline=20.0,
        )
        real_hash = file_ops.sha256_file_descriptor
        state = {"active": False, "replaced": False}

        def activate_final_verify(event):
            if event == "after_reusable_verified":
                state["active"] = True

        def hash_then_replace_name(descriptor):
            digest = real_hash(descriptor)
            if state["active"] and not state["replaced"]:
                current.rename(held)
                current.write_bytes(original_content)
                state["replaced"] = True
            return digest

        self.service.fault_injector = activate_final_verify

        with mock.patch(
            "core.services.media_storage.sha256_file_descriptor",
            side_effect=hash_then_replace_name,
        ):
            with self.assertRaises(MediaStorageError) as caught:
                self.service.commit(
                    prepared,
                    self.user,
                    self.metadata,
                    claim=None,
                    deadline=20.0,
                )

        self.assertTrue(state["replaced"])
        self.assertEqual(caught.exception.code, "media_path_conflict")
        self.assertFalse(prepared.is_open)
        self.assertEqual(Pin.objects.count(), 1)
        self.assertEqual(Image.objects.count(), 1)
        self.assertEqual(MediaAsset.objects.count(), 1)
        self.assertEqual(current.read_bytes(), original_content)
        self.assertEqual(held.read_bytes(), original_content)

    def test_reuse_file_mutation_during_last_final_hash_is_rejected(self):
        first = self._commit_initial_asset()
        image = Image.objects.get(pk=first.image_id)
        current = Path(
            self.temporary_media.name,
            image.thumbnail_set.get(size="square").image.name,
        )
        original_content = current.read_bytes()
        replacement = bytes([original_content[0] ^ 1]) + original_content[1:]
        prepared = self.service.prepare_upload(
            _uploaded_png(self.fetched.content, "hash-file-race.png"),
            deadline=20.0,
        )
        real_hash = file_ops.sha256_file_descriptor
        state = {"active": False, "hashes": 0}

        def activate_final_verify(event):
            if event == "after_reusable_verified":
                state["active"] = True

        def hash_then_mutate_last(descriptor):
            digest = real_hash(descriptor)
            if state["active"]:
                state["hashes"] += 1
                if state["hashes"] == 4:
                    current.write_bytes(replacement)
            return digest

        self.service.fault_injector = activate_final_verify

        with mock.patch(
            "core.services.media_storage.sha256_file_descriptor",
            side_effect=hash_then_mutate_last,
        ):
            with self.assertRaises(MediaStorageError) as caught:
                self.service.commit(
                    prepared,
                    self.user,
                    self.metadata,
                    claim=None,
                    deadline=20.0,
                )

        self.assertEqual(state["hashes"], 8)
        self.assertEqual(caught.exception.code, "media_path_conflict")
        self.assertFalse(prepared.is_open)
        self.assertEqual(Pin.objects.count(), 1)
        self.assertEqual(Image.objects.count(), 1)
        self.assertEqual(MediaAsset.objects.count(), 1)
        self.assertEqual(current.read_bytes(), replacement)

    def test_reuse_final_growth_before_commit_is_rejected_before_hash(self):
        first = self._commit_initial_asset()
        image = Image.objects.get(pk=first.image_id)
        current = Path(self.temporary_media.name, image.image.name)
        prepared = self.service.prepare_upload(
            _uploaded_png(self.fetched.content, "grown-before-commit.png"),
            deadline=20.0,
        )
        real_hash = file_ops.sha256_file_descriptor
        state = {"final_verify": False, "hashes": 0}

        def grow_before_final_verify(event):
            if event == "after_reusable_verified":
                current.write_bytes(current.read_bytes() + b"x")
                state["final_verify"] = True

        def record_final_hash(descriptor):
            if state["final_verify"]:
                state["hashes"] += 1
            return real_hash(descriptor)

        self.service.fault_injector = grow_before_final_verify
        with mock.patch(
            "core.services.media_storage.sha256_file_descriptor",
            side_effect=record_final_hash,
        ):
            with self.assertRaises(MediaStorageError) as caught:
                self.service.commit(
                    prepared,
                    self.user,
                    self.metadata,
                    claim=None,
                    deadline=20.0,
                )

        self.assertEqual(caught.exception.code, "media_path_conflict")
        self.assertEqual(state["hashes"], 0)
        self.assertFalse(prepared.is_open)
        self.assertEqual(Pin.objects.count(), 1)

    def test_reuse_rollback_preserves_primary_and_existing_final_ownership(
        self,
    ):
        self._commit_initial_asset()
        before = _file_snapshot(self.temporary_media.name)
        prepared = self.service.prepare_upload(
            _uploaded_png(self.fetched.content, "rollback-reuse.png"),
            deadline=20.0,
        )
        primary = ValueError("primary-reuse-fault")
        real_close = file_ops._close_descriptor
        failed = {"value": False}

        def fail_reusable_close_once(descriptor):
            if not failed["value"]:
                failed["value"] = True
                raise file_ops.DescriptorCloseNotAttempted(
                    "secondary-reuse-close"
                )
            return real_close(descriptor)

        def fail_after_receipt(event):
            if event == "after_reusable_verified":
                raise primary

        self.service.fault_injector = fail_after_receipt

        with mock.patch(
            "core.services.media_storage._close_descriptor",
            side_effect=fail_reusable_close_once,
        ), self._fail_first_descriptor_close():
            with self.assertRaises(ValueError) as caught:
                self.service.commit(
                    prepared,
                    self.user,
                    self.metadata,
                    claim=None,
                    deadline=20.0,
                )

        self.assertIs(caught.exception, primary)
        self.assertTrue(failed["value"])
        self.assertFalse(prepared.is_open)
        self.assertEqual(Pin.objects.count(), 1)
        self.assertEqual(Image.objects.count(), 1)
        self.assertEqual(MediaAsset.objects.count(), 1)
        self.assertEqual(_file_snapshot(self.temporary_media.name), before)

    def test_partial_reuse_open_retries_retained_original_descriptor(self):
        first = self._commit_initial_asset()
        image = Image.objects.get(pk=first.image_id)
        Path(
            self.temporary_media.name,
            image.thumbnail_set.get(size="thumbnail").image.name,
        ).unlink()
        prepared = self.service.prepare_upload(
            _uploaded_png(self.fetched.content, "partial-open.png"),
            deadline=20.0,
        )
        opened = []
        close_calls = {}
        real_open = file_ops._open_regular_nofollow
        real_close = file_ops._close_descriptor

        def capture_open(directory_descriptor, name):
            descriptor = real_open(directory_descriptor, name)
            opened.append(descriptor)
            return descriptor

        def fail_first_original_close(descriptor):
            close_calls[descriptor] = close_calls.get(descriptor, 0) + 1
            if descriptor == opened[0] and close_calls[descriptor] == 1:
                raise file_ops.DescriptorCloseNotAttempted(
                    "retained-original"
                )
            return real_close(descriptor)

        def close_leaked_descriptors():
            for descriptor in opened:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

        self.addCleanup(close_leaked_descriptors)

        with mock.patch(
            "core.services.media_storage._open_regular_nofollow",
            side_effect=capture_open,
        ), mock.patch(
            "core.services.media_storage._close_descriptor",
            side_effect=fail_first_original_close,
        ):
            with self.assertRaises(MediaStorageError) as caught:
                self.service.commit(
                    prepared,
                    self.user,
                    self.metadata,
                    claim=None,
                    deadline=20.0,
                )

        self.assertEqual(caught.exception.code, "media_path_conflict")
        self.assertEqual(len(opened), 1)
        self.assertEqual(close_calls[opened[0]], 2)
        with self.assertRaises(OSError):
            os.fstat(opened[0])
        self.assertFalse(prepared.is_open)
        self.assertEqual(Pin.objects.count(), 1)
        self.assertEqual(Image.objects.count(), 1)
        self.assertEqual(MediaAsset.objects.count(), 1)

    def test_partial_reuse_decode_failure_retries_owned_descriptor(self):
        self._commit_initial_asset()
        prepared = self.service.prepare_upload(
            _uploaded_png(self.fetched.content, "partial-decode.png"),
            deadline=20.0,
        )
        opened = []
        close_calls = {}
        real_open = file_ops._open_regular_nofollow
        real_close = file_ops._close_descriptor
        real_inspect = self.storage._inspect

        def capture_open(directory_descriptor, name):
            descriptor = real_open(directory_descriptor, name)
            opened.append(descriptor)
            return descriptor

        def fail_opened_decode(descriptor):
            if opened and descriptor == opened[0]:
                raise OSError("decode-after-open")
            return real_inspect(descriptor)

        def fail_first_original_close(descriptor):
            close_calls[descriptor] = close_calls.get(descriptor, 0) + 1
            if descriptor == opened[0] and close_calls[descriptor] == 1:
                raise file_ops.DescriptorCloseNotAttempted(
                    "retained-original"
                )
            return real_close(descriptor)

        def close_leaked_descriptors():
            for descriptor in opened:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

        self.addCleanup(close_leaked_descriptors)

        with mock.patch(
            "core.services.media_storage._open_regular_nofollow",
            side_effect=capture_open,
        ), mock.patch.object(
            self.storage,
            "_inspect",
            side_effect=fail_opened_decode,
        ), mock.patch(
            "core.services.media_storage._close_descriptor",
            side_effect=fail_first_original_close,
        ):
            with self.assertRaises(MediaStorageError) as caught:
                self.service.commit(
                    prepared,
                    self.user,
                    self.metadata,
                    claim=None,
                    deadline=20.0,
                )

        self.assertEqual(caught.exception.code, "media_path_conflict")
        self.assertEqual(len(opened), 1)
        self.assertEqual(close_calls[opened[0]], 2)
        with self.assertRaises(OSError):
            os.fstat(opened[0])
        self.assertFalse(prepared.is_open)
        self.assertEqual(Pin.objects.count(), 1)
        self.assertEqual(Image.objects.count(), 1)
        self.assertEqual(MediaAsset.objects.count(), 1)

    def test_reuse_database_fault_hooks_roll_back_only_new_pin_state(self):
        first = self._commit_initial_asset()
        before = _file_snapshot(self.temporary_media.name)
        first_tags = set(first.tags.names())
        fault_points = (
            "after_pin_row",
            "after_tags",
            "after_boards",
            "before_idempotency_success",
        )

        for fault_point in fault_points:
            with self.subTest(fault_point=fault_point):
                prepared = self.service.prepare_upload(
                    _uploaded_png(
                        self.fetched.content,
                        "reuse-{}.png".format(fault_point),
                    ),
                    deadline=20.0,
                )
                primary = ValueError("reuse-{}".format(fault_point))

                def fail_selected(event):
                    if event == fault_point:
                        raise primary

                self.service.fault_injector = fail_selected

                with self.assertRaises(ValueError) as caught:
                    self.service.commit(
                        prepared,
                        self.user,
                        self.metadata,
                        claim=None,
                        deadline=20.0,
                    )

                self.assertIs(caught.exception, primary)
                self.assertFalse(prepared.is_open)
                self.assertEqual(Pin.objects.count(), 1)
                self.assertEqual(Image.objects.count(), 1)
                self.assertEqual(Thumbnail.objects.count(), 3)
                self.assertEqual(MediaAsset.objects.count(), 1)
                self.assertEqual(set(first.tags.names()), first_tags)
                self.assertEqual(
                    set(self.board.pins.values_list("pk", flat=True)),
                    {first.pk},
                )
                self.assertEqual(
                    _file_snapshot(self.temporary_media.name), before
                )

    def test_reuse_precommit_base_exceptions_preserve_primary_and_finals(self):
        self._commit_initial_asset()
        before = _file_snapshot(self.temporary_media.name)

        for primary in (KeyboardInterrupt(), SystemExit("shutdown")):
            with self.subTest(error_type=type(primary).__name__):
                prepared = self.service.prepare_upload(
                    _uploaded_png(
                        self.fetched.content,
                        "reuse-{}.png".format(type(primary).__name__),
                    ),
                    deadline=20.0,
                )

                def fail_after_pin(event):
                    if event == "after_pin_row":
                        raise primary

                self.service.fault_injector = fail_after_pin

                with self.assertRaises(type(primary)) as caught:
                    self.service.commit(
                        prepared,
                        self.user,
                        self.metadata,
                        claim=None,
                        deadline=20.0,
                    )

                self.assertIs(caught.exception, primary)
                self.assertFalse(prepared.is_open)
                self.assertEqual(Pin.objects.count(), 1)
                self.assertEqual(Image.objects.count(), 1)
                self.assertEqual(MediaAsset.objects.count(), 1)
                self.assertEqual(
                    _file_snapshot(self.temporary_media.name), before
                )

    def test_reuse_fence_and_record_success_failures_preserve_asset(self):
        self._commit_initial_asset()
        before = _file_snapshot(self.temporary_media.name)
        claim = ClaimResult(
            kind="claimed",
            row_id=1,
            lease_uuid=uuid.uuid4(),
            lease_generation=1,
        )

        cases = (
            (False, True),
            (True, False),
        )
        for fence_result, success_result in cases:
            with self.subTest(
                fence_result=fence_result,
                success_result=success_result,
            ):
                prepared = self.service.prepare_upload(
                    _uploaded_png(
                        self.fetched.content,
                        "reuse-idempotency.png",
                    ),
                    deadline=20.0,
                )
                events = []
                store = _ConfigurableIdempotencyStore(
                    events,
                    fence_result=fence_result,
                    success_result=success_result,
                )
                self.service.idempotency = store
                self.service.fault_injector = None

                with self.assertRaises(PinImportError) as caught:
                    self.service.commit(
                        prepared,
                        self.user,
                        self.metadata,
                        claim=claim,
                        deadline=20.0,
                    )

                self.assertEqual(caught.exception.code, "lease_lost")
                self.assertFalse(prepared.is_open)
                self.assertEqual(len(store.fence_calls), 1)
                self.assertEqual(
                    len(store.success_calls),
                    0 if not fence_result else 1,
                )
                self.assertEqual(Pin.objects.count(), 1)
                self.assertEqual(Image.objects.count(), 1)
                self.assertEqual(MediaAsset.objects.count(), 1)
                self.assertEqual(
                    _file_snapshot(self.temporary_media.name), before
                )

    def test_reuse_actual_commit_failure_rolls_back_pin_and_keeps_finals(self):
        self._commit_initial_asset()
        before = _file_snapshot(self.temporary_media.name)
        prepared = self.service.prepare_upload(
            _uploaded_png(self.fetched.content, "reuse-commit-fail.png"),
            deadline=20.0,
        )
        primary = OperationalError("database is locked: private-token")

        with mock.patch.object(connection, "commit", side_effect=primary):
            with self.assertRaises(OperationalError) as caught:
                self.service.commit(
                    prepared,
                    self.user,
                    self.metadata,
                    claim=None,
                    deadline=20.0,
                )

        self.assertIs(caught.exception, primary)
        self.assertFalse(prepared.is_open)
        self.assertEqual(Pin.objects.count(), 1)
        self.assertEqual(Image.objects.count(), 1)
        self.assertEqual(MediaAsset.objects.count(), 1)
        self.assertEqual(_file_snapshot(self.temporary_media.name), before)

    def test_reuse_committed_callback_exception_keeps_pin_and_finals(self):
        first = self._commit_initial_asset()
        before = _file_snapshot(self.temporary_media.name)
        prepared = self.service.prepare_upload(
            _uploaded_png(self.fetched.content, "callback-reuse.png"),
            deadline=20.0,
        )

        def register_failing_callback(event):
            if event == "before_idempotency_success":
                transaction.on_commit(
                    lambda: self._raise(RuntimeError("private-reuse"))
                )

        self.service.fault_injector = register_failing_callback

        with mock.patch(
            "core.services.pin_import.logger.warning"
        ) as warning:
            second = self.service.commit(
                prepared,
                self.user,
                self.metadata,
                claim=None,
                deadline=20.0,
            )

        self.assertEqual(second.image_id, first.image_id)
        self.assertFalse(prepared.is_open)
        self.assertEqual(Pin.objects.count(), 2)
        self.assertEqual(Image.objects.count(), 1)
        self.assertEqual(MediaAsset.objects.count(), 1)
        self.assertEqual(_file_snapshot(self.temporary_media.name), before)
        warning.assert_called_once_with(
            "pin_import_post_commit_callback_failed error_type=%s",
            "RuntimeError",
        )

    def test_reuse_callback_system_exit_rethrows_after_commit_and_cleanup(self):
        first = self._commit_initial_asset()
        before = _file_snapshot(self.temporary_media.name)
        prepared = self.service.prepare_upload(
            _uploaded_png(self.fetched.content, "exit-reuse.png"),
            deadline=20.0,
        )
        primary = SystemExit("reuse shutdown")

        def register_failing_callback(event):
            if event == "before_idempotency_success":
                transaction.on_commit(lambda: self._raise(primary))

        self.service.fault_injector = register_failing_callback

        with self.assertRaises(SystemExit) as caught:
            self.service.commit(
                prepared,
                self.user,
                self.metadata,
                claim=None,
                deadline=20.0,
            )

        self.assertIs(caught.exception, primary)
        self.assertFalse(prepared.is_open)
        self.assertEqual(Pin.objects.count(), 2)
        self.assertEqual(
            set(Pin.objects.values_list("image_id", flat=True)),
            {first.image_id},
        )
        self.assertEqual(Image.objects.count(), 1)
        self.assertEqual(MediaAsset.objects.count(), 1)
        self.assertEqual(_file_snapshot(self.temporary_media.name), before)

    def test_reuse_unlock_errors_after_commit_keep_pin_and_finals(self):
        first = self._commit_initial_asset()
        before = _file_snapshot(self.temporary_media.name)
        real_flock = file_ops.fcntl.flock

        for failing_unlock in (1, 2):
            with self.subTest(failing_unlock=failing_unlock):
                prepared = self.service.prepare_upload(
                    _uploaded_png(
                        self.fetched.content,
                        "unlock-{}.png".format(failing_unlock),
                    ),
                    deadline=20.0,
                )
                unlock_calls = {"value": 0}

                def fail_selected_unlock(descriptor, operation):
                    if operation == file_ops.fcntl.LOCK_UN:
                        unlock_calls["value"] += 1
                        if unlock_calls["value"] == failing_unlock:
                            raise RuntimeError("secondary-unlock")
                    return real_flock(descriptor, operation)

                with self.assertLogs(
                    "core.services.pin_import", level="WARNING"
                ), mock.patch(
                    "django_images.file_ops.fcntl.flock",
                    side_effect=fail_selected_unlock,
                ):
                    pin = self.service.commit(
                        prepared,
                        self.user,
                        self.metadata,
                        claim=None,
                        deadline=20.0,
                    )

                self.assertEqual(pin.image_id, first.image_id)
                self.assertFalse(prepared.is_open)
                self.assertEqual(unlock_calls["value"], 2)
                self.assertEqual(Image.objects.count(), 1)
                self.assertEqual(MediaAsset.objects.count(), 1)
                self.assertEqual(
                    _file_snapshot(self.temporary_media.name), before
                )

    def test_reuse_release_and_prepared_cleanup_errors_keep_committed_pin(self):
        first = self._commit_initial_asset()
        before = _file_snapshot(self.temporary_media.name)
        prepared = self.service.prepare_upload(
            _uploaded_png(self.fetched.content, "cleanup-reuse.png"),
            deadline=20.0,
        )
        real_close = file_ops._close_descriptor
        failed = {"value": False}

        def fail_reusable_close_once(descriptor):
            if not failed["value"]:
                failed["value"] = True
                raise file_ops.DescriptorCloseNotAttempted(
                    "secondary-reuse-close"
                )
            return real_close(descriptor)

        with mock.patch(
            "core.services.media_storage._close_descriptor",
            side_effect=fail_reusable_close_once,
        ), self._fail_first_descriptor_close():
            second = self.service.commit(
                prepared,
                self.user,
                self.metadata,
                claim=None,
                deadline=20.0,
            )

        self.assertTrue(failed["value"])
        self.assertEqual(second.image_id, first.image_id)
        self.assertFalse(prepared.is_open)
        self.assertEqual(Pin.objects.count(), 2)
        self.assertEqual(Image.objects.count(), 1)
        self.assertEqual(MediaAsset.objects.count(), 1)
        self.assertEqual(_file_snapshot(self.temporary_media.name), before)

    def test_reuse_cleanup_system_exit_retries_then_rethrows_first(self):
        first = self._commit_initial_asset()
        prepared = self.service.prepare_upload(
            _uploaded_png(self.fetched.content, "cleanup-exit.png"),
            deadline=20.0,
        )
        actual_cleanup = prepared.cleanup
        primary = SystemExit("cleanup shutdown")

        try:
            with mock.patch.object(
                prepared,
                "cleanup",
                side_effect=primary,
            ) as cleanup:
                with self.assertRaises(SystemExit) as caught:
                    self.service.commit(
                        prepared,
                        self.user,
                        self.metadata,
                        claim=None,
                        deadline=20.0,
                    )

            self.assertIs(caught.exception, primary)
            self.assertEqual(cleanup.call_count, 2)
            self.assertEqual(Pin.objects.count(), 2)
            self.assertEqual(
                set(Pin.objects.values_list("image_id", flat=True)),
                {first.image_id},
            )
            self.assertEqual(Image.objects.count(), 1)
            self.assertEqual(MediaAsset.objects.count(), 1)
        finally:
            actual_cleanup()

    def test_reuse_close_after_error_is_not_retried_as_owned_descriptor(self):
        first = self._commit_initial_asset()
        before = _file_snapshot(self.temporary_media.name)
        prepared = self.service.prepare_upload(
            _uploaded_png(self.fetched.content, "closed-reuse.png"),
            deadline=20.0,
        )
        captured = {}
        real_verify = self.storage.verify_reusable

        def capture_receipt(*args, **kwargs):
            captured["receipt"] = real_verify(*args, **kwargs)
            return captured["receipt"]

        def close_then_raise(descriptor):
            os.close(descriptor)
            raise RuntimeError("descriptor closed before error")

        with mock.patch.object(
            self.storage,
            "verify_reusable",
            side_effect=capture_receipt,
        ), mock.patch(
            "core.services.media_storage._close_descriptor",
            side_effect=close_then_raise,
        ):
            second = self.service.commit(
                prepared,
                self.user,
                self.metadata,
                claim=None,
                deadline=20.0,
            )

        receipt = captured["receipt"]
        self.assertEqual(second.image_id, first.image_id)
        self.assertTrue(receipt._released)
        self.assertEqual(receipt.files, [])
        self.assertEqual(receipt.directories, [])
        self.assertFalse(prepared.is_open)
        self.assertEqual(Pin.objects.count(), 2)
        self.assertEqual(_file_snapshot(self.temporary_media.name), before)

    def test_same_url_with_distinct_item_ids_reuses_one_asset(self):
        second_item_id = uuid.UUID(
            "40000000-0000-0000-0000-000000000002"
        )
        data = self._batch_data((self.item_id, second_item_id))

        with mock.patch.object(
            self.idempotency,
            "fence",
            wraps=self.idempotency.fence,
        ) as fence, mock.patch.object(
            self.idempotency,
            "record_success",
            wraps=self.idempotency.record_success,
        ) as record_success:
            result = self._batch_service().process(
                self.user,
                data,
                started_at=10.0,
            )

        rows = BatchImportItem.objects.filter(
            submitter=self.user,
            client_item_id__in=(self.item_id, second_item_id),
        )
        self.assertEqual(
            [item["status"] for item in result["results"]],
            ["created", "created"],
        )
        pins = Pin.objects.filter(pk__in=[
            item["pin_id"] for item in result["results"]
        ])
        self.assertEqual(rows.count(), 2)
        self.assertEqual(
            set(rows.values_list("state", flat=True)),
            {BatchImportItem.SUCCEEDED},
        )
        self.assertEqual(pins.count(), 2)
        self.assertEqual(Pin.objects.count(), 2)
        self.assertEqual(Image.objects.count(), 1)
        self.assertEqual(Thumbnail.objects.count(), 3)
        self.assertEqual(MediaAsset.objects.count(), 1)
        self.assertEqual(len(_file_snapshot(self.temporary_media.name)), 4)
        self.assertEqual(fence.call_count, 2)
        self.assertEqual(record_success.call_count, 2)

    def test_real_commit_survives_later_on_commit_callback_exception(self):
        def register_failing_callback(event):
            if event == "before_idempotency_success":
                transaction.on_commit(
                    lambda: self._raise(RuntimeError("private-token"))
                )

        self.service.fault_injector = register_failing_callback
        fingerprint = self._fingerprint()
        claim = self.idempotency.claim(
            self.user,
            self.batch_id,
            self.item_id,
            fingerprint,
            self.now,
        )
        prepared = self.service.prepare_url(
            self.metadata.url,
            self.metadata.referer,
            deadline=20.0,
        )

        with mock.patch(
            "core.services.pin_import.logger.warning"
        ) as warning:
            pin = self.service.commit(
                prepared,
                self.user,
                self.metadata,
                claim,
                deadline=20.0,
            )

        row = BatchImportItem.objects.get(pk=claim.row_id)
        replay = self.idempotency.claim(
            self.user,
            uuid.uuid4(),
            self.item_id,
            fingerprint,
            self.now + datetime.timedelta(seconds=1),
        )
        self.assertEqual(row.state, BatchImportItem.SUCCEEDED)
        self.assertEqual(row.pin_id, pin.pk)
        self.assertEqual(replay.kind, "replayed")
        self.assertEqual(replay.replay_pin_id, pin.pk)
        self.assertEqual(Pin.objects.count(), 1)
        self.assertEqual(Image.objects.count(), 1)
        self.assertEqual(Thumbnail.objects.count(), 3)
        self.assertEqual(len(_file_snapshot(self.temporary_media.name)), 4)
        warning.assert_called_once_with(
            "pin_import_post_commit_callback_failed error_type=%s",
            "RuntimeError",
        )

    def _fingerprint(self):
        return fingerprint_request({
            "url": self.metadata.url,
            "referer": self.metadata.referer,
            "description": self.metadata.description,
            "private": self.metadata.private,
            "tags": self.metadata.tags,
            "board_ids": self.metadata.board_ids,
        })

    def _commit_initial_asset(self):
        prepared = self.service.prepare_url(
            self.metadata.url,
            self.metadata.referer,
            deadline=20.0,
        )
        return self.service.commit(
            prepared,
            self.user,
            self.metadata,
            claim=None,
            deadline=20.0,
        )

    def _assert_reuse_conflict_preserves_current_files(self):
        before = _file_snapshot(self.temporary_media.name)
        prepared = self.service.prepare_upload(
            _uploaded_png(self.fetched.content, "fresh-name.png"),
            deadline=20.0,
        )

        with self.assertRaises(MediaStorageError) as caught:
            self.service.commit(
                prepared,
                self.user,
                self.metadata,
                claim=None,
                deadline=20.0,
            )

        self.assertEqual(caught.exception.code, "media_path_conflict")
        self.assertFalse(prepared.is_open)
        self.assertEqual(Pin.objects.count(), 1)
        self.assertEqual(Image.objects.count(), 1)
        self.assertEqual(Thumbnail.objects.count(), 3)
        self.assertEqual(MediaAsset.objects.count(), 1)
        self.assertEqual(_file_snapshot(self.temporary_media.name), before)

    def _batch_data(self, item_ids=None):
        if item_ids is None:
            item_ids = (self.item_id,)
        return {
            "batch_id": self.batch_id,
            "board_ids": list(self.metadata.board_ids),
            "tags": list(self.metadata.tags),
            "private": self.metadata.private,
            "referer": self.metadata.referer,
            "description": self.metadata.description,
            "items": [
                {
                    "client_item_id": item_id,
                    "url": self.metadata.url,
                }
                for item_id in item_ids
            ],
        }

    def _batch_service(self, fault_injector=None):
        return BatchImportService(
            fetcher=self.fetcher,
            media_storage=self.storage,
            idempotency=self.idempotency,
            pin_import=self.service,
            clock=lambda: 10.0,
            wall_clock=lambda: self.now,
            fault_injector=fault_injector,
        )

    @staticmethod
    def _fail_first_descriptor_close():
        real_close = file_ops._close_descriptor
        failed = {"value": False}

        def fail_once(descriptor):
            if not failed["value"]:
                failed["value"] = True
                raise file_ops.DescriptorCloseNotAttempted(
                    "injected pre-close failure"
                )
            return real_close(descriptor)

        return mock.patch(
            "django_images.file_ops._close_descriptor",
            side_effect=fail_once,
        )

    @staticmethod
    def _raise(error):
        raise error


class PinImportConcurrencyTests(
    _LinuxStrongPublishMixin,
    TemporaryMediaMixin,
    TransactionTestCase,
):
    reset_sequences = True

    def setUp(self):
        super(PinImportConcurrencyTests, self).setUp()
        if connection.vendor != "sqlite":
            self.skipTest("This concurrency contract requires SQLite.")
        if connection.creation.is_in_memory_db(
            connection.settings_dict["NAME"]
        ):
            self.skipTest("This concurrency contract requires file SQLite.")
        self.user = User.objects.create_user(
            username="pipeline-concurrent-owner",
            email="pipeline-concurrent-owner@example.com",
        )
        self.board = Board.objects.create(
            submitter=self.user,
            name="pipeline-concurrent-board",
        )
        self.now = datetime.datetime(
            2026,
            8,
            20,
            5,
            6,
            7,
            tzinfo=datetime.timezone.utc,
        )
        self.batch_id = uuid.UUID(
            "50000000-0000-0000-0000-000000000001"
        )
        self.item_id = uuid.UUID(
            "60000000-0000-0000-0000-000000000001"
        )
        self.metadata = ImportMetadata(
            url="https://origin.example/concurrent",
            referer="https://origin.example/page",
            description="concurrent",
            private=False,
            tags=("concurrent",),
            board_ids=(self.board.pk,),
        )
        self.fetched = _png_fetched_image(
            "https://cdn.example/concurrent.png"
        )

    def test_two_batch_workers_publish_and_commit_exactly_one_asset(self):
        start_barrier = threading.Barrier(2)
        claim_barrier = threading.Barrier(2)
        events = queue.Queue()
        counts = {
            worker_id: {
                "fetch": 0,
                "prepare": 0,
                "publish": 0,
                "commit": 0,
            }
            for worker_id in range(2)
        }
        counts_lock = threading.Lock()
        data = {
            "batch_id": self.batch_id,
            "board_ids": list(self.metadata.board_ids),
            "tags": list(self.metadata.tags),
            "private": self.metadata.private,
            "referer": self.metadata.referer,
            "description": self.metadata.description,
            "items": [{
                "client_item_id": self.item_id,
                "url": self.metadata.url,
            }],
        }

        class CoordinatedStore(IdempotencyStore):
            def claim(
                inner_self,
                user,
                batch_id,
                client_item_id,
                fingerprint,
                now,
            ):
                result = super(CoordinatedStore, inner_self).claim(
                    user,
                    batch_id,
                    client_item_id,
                    fingerprint,
                    now,
                )
                claim_barrier.wait(timeout=5)
                return result

        class CountingFetcher(object):
            def __init__(inner_self, worker_id):
                inner_self.worker_id = worker_id

            def fetch(inner_self, url, referer=None, deadline=None):
                del url, referer, deadline
                with counts_lock:
                    counts[inner_self.worker_id]["fetch"] += 1
                return self.fetched

        class CountingStorage(MediaStorage):
            def __init__(inner_self, worker_id, **kwargs):
                inner_self.worker_id = worker_id
                super(CountingStorage, inner_self).__init__(**kwargs)

            def prepare(inner_self, *args, **kwargs):
                with counts_lock:
                    counts[inner_self.worker_id]["prepare"] += 1
                return super(CountingStorage, inner_self).prepare(
                    *args,
                    **kwargs
                )

            def publish(inner_self, prepared, deadline=None):
                with counts_lock:
                    counts[inner_self.worker_id]["publish"] += 1
                return super(CountingStorage, inner_self).publish(
                    prepared,
                    deadline=deadline,
                )

        class CountingPinImport(PinImportService):
            def __init__(inner_self, worker_id, *args, **kwargs):
                inner_self.worker_id = worker_id
                super(CountingPinImport, inner_self).__init__(
                    *args,
                    **kwargs
                )

            def commit(inner_self, *args, **kwargs):
                with counts_lock:
                    counts[inner_self.worker_id]["commit"] += 1
                return super(CountingPinImport, inner_self).commit(
                    *args,
                    **kwargs
                )

        def worker(worker_id):
            close_old_connections()
            try:
                user = User.objects.get(pk=self.user.pk)
                store = CoordinatedStore(
                    wall_clock=lambda: self.now,
                    lease_seconds=300,
                )
                storage = CountingStorage(
                    worker_id,
                    media_root=self.temporary_media.name,
                    clock=lambda: 10.0,
                )
                fetcher = CountingFetcher(worker_id)
                pin_import = CountingPinImport(
                    worker_id,
                    fetcher=fetcher,
                    media_storage=storage,
                    idempotency=store,
                    clock=lambda: 10.0,
                )
                service = BatchImportService(
                    fetcher=fetcher,
                    media_storage=storage,
                    idempotency=store,
                    pin_import=pin_import,
                    clock=lambda: 10.0,
                    wall_clock=lambda: self.now,
                )
                start_barrier.wait(timeout=5)
                result = service.process(
                    user,
                    data,
                    started_at=10.0,
                )
                events.put(("result", worker_id, result["results"][0]))
            except BaseException as error:
                events.put(("error", worker_id, error))
            finally:
                connections["default"].close()

        threads = [
            threading.Thread(target=worker, args=(worker_id,))
            for worker_id in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        collected = [events.get(timeout=1) for _ in range(2)]
        errors = [event for kind, _worker_id, event in collected
                  if kind == "error"]
        self.assertEqual(errors, [])
        results = {
            worker_id: event
            for kind, worker_id, event in collected
            if kind == "result"
        }
        created_worker = next(
            worker_id for worker_id, result in results.items()
            if result["status"] == "created"
        )
        losing_worker = 1 - created_worker
        self.assertEqual(results[losing_worker]["status"], "conflict")
        self.assertEqual(
            results[losing_worker]["error"]["code"],
            "in_progress",
        )
        self.assertEqual(
            counts[created_worker],
            {"fetch": 1, "prepare": 1, "publish": 1, "commit": 1},
        )
        self.assertEqual(
            counts[losing_worker],
            {"fetch": 0, "prepare": 0, "publish": 0, "commit": 0},
        )
        row = BatchImportItem.objects.get(
            submitter=self.user,
            client_item_id=self.item_id,
        )
        self.assertEqual(row.state, BatchImportItem.SUCCEEDED)
        self.assertEqual(
            results[created_worker]["pin_id"],
            row.pin_id,
        )
        self.assertEqual(Pin.objects.count(), 1)
        self.assertEqual(Image.objects.count(), 1)
        self.assertEqual(Thumbnail.objects.count(), 3)
        self.assertEqual(len(_file_snapshot(self.temporary_media.name)), 4)

    def test_same_hash_workers_create_two_pins_and_one_physical_asset(self):
        prepared_barrier = threading.Barrier(2)
        events = queue.Queue()

        def worker(worker_id):
            close_old_connections()
            try:
                user = User.objects.get(pk=self.user.pk)
                storage = MediaStorage(
                    media_root=self.temporary_media.name,
                    clock=lambda: 10.0,
                )
                fetcher = _Fetcher(self.fetched)
                service = PinImportService(
                    fetcher=fetcher,
                    media_storage=storage,
                    idempotency=IdempotencyStore(),
                    clock=lambda: 10.0,
                )
                prepared = service.prepare_url(
                    self.metadata.url,
                    self.metadata.referer,
                    deadline=20.0,
                )
                prepared_barrier.wait(timeout=5)
                pin = service.commit(
                    prepared,
                    user,
                    ImportMetadata(
                        url=self.metadata.url,
                        referer=self.metadata.referer,
                        description="worker-{}".format(worker_id),
                        private=False,
                        tags=(),
                        board_ids=(),
                    ),
                    claim=None,
                    deadline=20.0,
                )
                events.put(("pin", pin.pk))
            except BaseException as error:
                events.put(("error", error))
            finally:
                connections["default"].close()

        workers = [
            threading.Thread(target=worker, args=(worker_id,))
            for worker_id in range(2)
        ]
        for current in workers:
            current.start()
        for current in workers:
            current.join(timeout=10)

        self.assertFalse(any(current.is_alive() for current in workers))
        collected = [events.get(timeout=1) for _worker in workers]
        errors = [value for kind, value in collected if kind == "error"]
        self.assertEqual(errors, [])
        pin_ids = [value for kind, value in collected if kind == "pin"]
        self.assertEqual(len(pin_ids), 2)
        self.assertEqual(Pin.objects.filter(pk__in=pin_ids).count(), 2)
        self.assertEqual(Pin.objects.count(), 2)
        self.assertEqual(Image.objects.count(), 1)
        self.assertEqual(Thumbnail.objects.count(), 3)
        self.assertEqual(MediaAsset.objects.count(), 1)
        self.assertEqual(len(_file_snapshot(self.temporary_media.name)), 4)


class BoardPatchConditionalDeleteConcurrencyTests(
    TemporaryMediaMixin,
    TransactionTestCase,
):
    reset_sequences = True

    def setUp(self):
        super(BoardPatchConditionalDeleteConcurrencyTests, self).setUp()
        if connection.vendor != "sqlite":
            self.skipTest("This concurrency contract requires SQLite.")
        if connection.creation.is_in_memory_db(
            connection.settings_dict["NAME"]
        ):
            self.skipTest("This concurrency contract requires file SQLite.")
        self.owner = User.objects.create_user(
            username="board-patch-race-owner",
            email="board-patch-race-owner@example.com",
            password="password",
        )
        self.source = Board.objects.create(
            submitter=self.owner,
            name="board-patch-race-source",
        )
        self.target = Board.objects.create(
            submitter=self.owner,
            name="board-patch-race-target",
        )
        self.pin = Pin.objects.create(
            submitter=self.owner,
            image=create_image(),
        )
        self.source.pins.add(self.pin)

    def test_patch_racing_conditional_delete_preserves_committed_membership(
        self,
    ):
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA journal_mode")
            original_journal_mode = cursor.fetchone()[0].lower()
            cursor.execute("PRAGMA journal_mode = WAL")
            self.assertEqual(cursor.fetchone()[0].lower(), "wal")

        def restore_journal_mode():
            connections["default"].close()
            with connections["default"].cursor() as cursor:
                cursor.execute(
                    "PRAGMA journal_mode = {}".format(
                        original_journal_mode
                    )
                )
                self.assertEqual(
                    cursor.fetchone()[0].lower(),
                    original_journal_mode,
                )
            connections["default"].close()

        self.addCleanup(restore_journal_mode)

        owner = self.owner
        pin_id = self.pin.pk
        source_id = self.source.pk
        target_id = self.target.pk
        pin_snapshot_ready = threading.Barrier(2)
        patch_finished = threading.Event()
        outcomes = queue.Queue()
        original_lock = PinMembershipService.lock_source_board_and_pin

        def pause_delete_after_pin_snapshot(
            source_board_id,
            current_pin_id,
            using,
        ):
            result = original_lock(
                source_board_id,
                current_pin_id,
                using,
            )
            if threading.current_thread().name == "conditional-delete-worker":
                pin_snapshot_ready.wait(timeout=5)
                if not patch_finished.wait(5):
                    raise AssertionError("board patch did not finish")
            return result

        def conditional_delete_worker():
            close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(user=owner)
                client.raise_request_exception = False
                response = client.post(
                    "/api/v2/pins/bulk/",
                    {
                        "operation": "delete_if_exclusive_to_board",
                        "pin_ids": [pin_id],
                        "source_board_id": source_id,
                    },
                    format="json",
                )
                outcomes.put((
                    "delete",
                    response.status_code,
                    response.data,
                ))
            except BaseException as error:
                outcomes.put(("error", "delete", error))
            finally:
                connections["default"].close()

        def board_patch_worker():
            close_old_connections()
            try:
                pin_snapshot_ready.wait(timeout=5)
                client = APIClient()
                client.force_authenticate(user=owner)
                client.raise_request_exception = False
                response = client.patch(
                    "/api/v2/boards/{}/".format(target_id),
                    {"pins_to_add": [pin_id]},
                    format="json",
                )
                outcomes.put((
                    "patch",
                    response.status_code,
                    response.data,
                ))
            except BaseException as error:
                outcomes.put(("error", "patch", error))
            finally:
                connections["default"].close()
                patch_finished.set()

        with mock.patch.object(
            PinMembershipService,
            "lock_source_board_and_pin",
            side_effect=pause_delete_after_pin_snapshot,
        ):
            workers = [
                threading.Thread(
                    target=conditional_delete_worker,
                    name="conditional-delete-worker",
                ),
                threading.Thread(
                    target=board_patch_worker,
                    name="board-patch-worker",
                ),
            ]
            for current in workers:
                current.start()
            for current in workers:
                current.join(timeout=10)

        self.assertFalse(any(current.is_alive() for current in workers))
        collected = [outcomes.get(timeout=1) for _worker in workers]
        errors = [item for item in collected if item[0] == "error"]
        self.assertEqual(errors, [])
        results = {item[0]: item[1:] for item in collected}
        self.assertEqual(results["patch"][0], status.HTTP_200_OK)
        self.assertEqual(results["delete"], (
            status.HTTP_200_OK,
            {
                "operation": "delete_if_exclusive_to_board",
                "succeeded": 0,
                "preserved": 0,
                "failed": 1,
                "results": [{
                    "id": pin_id,
                    "status": "failed",
                    "code": "database_busy",
                    "retryable": True,
                }],
            },
        ))
        self.assertNotIn(
            "database is locked",
            str(results["delete"]).lower(),
        )
        self.assertTrue(Pin.objects.filter(pk=pin_id).exists())
        self.assertTrue(self.source.pins.filter(pk=pin_id).exists())
        self.assertTrue(self.target.pins.filter(pk=pin_id).exists())
        self.assertFalse(
            Board.pins.through.objects.exclude(
                board_id__in=Board.objects.values_list("pk", flat=True)
            ).exists()
        )
        self.assertFalse(
            Board.pins.through.objects.exclude(
                pin_id__in=Pin.objects.values_list("pk", flat=True)
            ).exists()
        )
