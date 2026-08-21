from io import StringIO
import hashlib
from pathlib import Path
import queue
import threading
import traceback
import uuid
from unittest import skipUnless

import mock
from django.apps import apps
from django.contrib import admin
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import close_old_connections, connection, connections
from django.db import OperationalError, transaction
from django.db.models.query import QuerySet
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APITransactionTestCase

from core.admin import PinAdmin
from core.models import BatchImportItem, Board, Image, MediaAsset, Pin
from core.services.idempotency import IdempotencyStore, StoredError
from core.services.media_storage import MediaStorage
from core.services.pin_membership import PinMembershipService
from core.services.pin_import import ImportMetadata, PinImportService
from core.tests.helpers import TEST_IMAGE_PATH, create_image, create_pin, create_user
from core.tests.test_pin_import_atomicity import (
    _LinuxStrongPublishMixin,
    _SQLiteConcurrencyHarness,
)
from django_images import file_ops
from django_images.file_ops import remove_media_file
from django_images.models import Image as BaseImage, Thumbnail
from django_images.test_helpers import TemporaryMediaMixin


def media_snapshot(media_root):
    root = Path(media_root)
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
        and path.relative_to(root).parts[0] != ".pinry-locks"
    }


class PinDeletionAPITest(TemporaryMediaMixin, APITransactionTestCase):
    def setUp(self):
        super(PinDeletionAPITest, self).setUp()
        self.owner = create_user("trash-owner")
        self.other_user = create_user("trash-other")
        self.image = create_image()
        self.pin = create_pin(self.owner, self.image, [])
        self.client.login(username=self.owner.username, password="password")

    def test_delete_removes_owned_pin_immediately(self):
        response = self.client.delete(reverse("pin-detail", args=[self.pin.pk]))

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Pin.objects.filter(pk=self.pin.pk).exists())

    def test_removed_trash_routes_return_404(self):
        detail = reverse("pin-detail", args=[self.pin.pk])
        responses = (
            self.client.get("{}trash/".format(reverse("pin-list"))),
            self.client.post("{}restore/".format(detail)),
            self.client.delete("{}permanent/".format(detail)),
        )
        self.assertEqual(
            [response.status_code for response in responses],
            [status.HTTP_404_NOT_FOUND] * 3,
        )
        self.assertTrue(Pin.objects.filter(pk=self.pin.pk).exists())

    def test_non_owner_cannot_delete_pin(self):
        self.client.login(username=self.other_user.username, password="password")
        response = self.client.delete(reverse("pin-detail", args=[self.pin.pk]))

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(Pin.objects.filter(pk=self.pin.pk).exists())

    def test_missing_pin_delete_returns_404(self):
        response = self.client.delete(
            reverse("pin-detail", args=[self.pin.pk + 100000])
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_direct_delete_preserves_succeeded_item_as_tombstone(self):
        batch_id = uuid.uuid4()
        client_item_id = uuid.uuid4()
        fingerprint = "a" * 64
        item = BatchImportItem.objects.create(
            submitter=self.owner,
            batch_id=batch_id,
            client_item_id=client_item_id,
            request_fingerprint=fingerprint,
            state=BatchImportItem.SUCCEEDED,
            pin=self.pin,
            lease_generation=7,
        )

        response = self.client.delete(reverse("pin-detail", args=[self.pin.pk]))

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        item.refresh_from_db()
        self.assertEqual(item.state, BatchImportItem.SUCCEEDED)
        self.assertEqual(item.request_fingerprint, fingerprint)
        self.assertEqual(item.lease_generation, 7)
        self.assertIsNone(item.pin_id)

        store = IdempotencyStore()
        same = store.claim(
            self.owner, batch_id, client_item_id, fingerprint, timezone.now()
        )
        mismatch = store.claim(
            self.owner, batch_id, client_item_id, "b" * 64, timezone.now()
        )
        self.assertEqual(same.kind, "failed")
        self.assertEqual(
            same.error, StoredError("pin_permanently_deleted", False)
        )
        self.assertEqual(mismatch.kind, "conflict")
        self.assertEqual(
            mismatch.error, StoredError("idempotency_mismatch", False)
        )

    def test_post_commit_cleanup_error_does_not_flip_success_or_leak_detail(
        self
    ):
        secret = "/private/media?token=secret"
        with mock.patch.object(
            BaseImage, "delete", side_effect=RuntimeError(secret)
        ):
            with self.assertLogs("core.models", level="WARNING") as captured:
                response = self.client.delete(
                    reverse("pin-detail", args=[self.pin.pk])
                )
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Pin.objects.filter(pk=self.pin.pk).exists())
        output = "\n".join(captured.output)
        self.assertEqual(len(captured.records), 1)
        self.assertEqual(
            captured.records[0].getMessage(), "pin_image_cleanup_failed"
        )
        self.assertEqual(captured.records[0].media_error, "RuntimeError")
        self.assertNotIn(secret, output)
        self.assertNotIn(secret, captured.records[0].getMessage())

    def test_cleanup_and_logger_errors_do_not_flip_delete_success(self):
        with mock.patch.object(
            BaseImage,
            "delete",
            side_effect=RuntimeError("secret cleanup"),
        ), mock.patch(
            "core.models.logger.warning",
            side_effect=RuntimeError("secret logger"),
        ):
            response = self.client.delete(
                reverse("pin-detail", args=[self.pin.pk])
            )

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Pin.objects.filter(pk=self.pin.pk).exists())
        self.assertTrue(BaseImage.objects.filter(pk=self.image.pk).exists())
        self.assertFalse(
            apps.get_model(
                "django_images", "PendingMediaDeletion"
            ).objects.exists()
        )


class PinMediaLifecycleTest(
    _LinuxStrongPublishMixin,
    TemporaryMediaMixin,
    APITransactionTestCase,
):
    def setUp(self):
        super(PinMediaLifecycleTest, self).setUp()
        self.owner = create_user("media-owner")
        self.other_user = create_user("media-other")
        self.client.login(username=self.owner.username, password="password")

    def _assert_four_image_files(self, image):
        image.refresh_from_db()
        expected_paths = {image.image.name}
        expected_paths.update(
            image.thumbnail_set.values_list("image", flat=True)
        )
        snapshot = media_snapshot(self.temporary_media.name)
        self.assertEqual(len(expected_paths), 4)
        self.assertEqual(set(snapshot), expected_paths)
        self.assertEqual(
            snapshot[image.image.name], Path(TEST_IMAGE_PATH).read_bytes()
        )
        return snapshot

    @staticmethod
    def _pending_deletions():
        return apps.get_model(
            "django_images", "PendingMediaDeletion"
        ).objects

    def _asset_directories(self, image):
        asset_uuid = str(image.asset_uuid)
        root = Path(self.temporary_media.name)
        return (
            root / "originals" / asset_uuid,
            root / "derivatives" / asset_uuid,
        )

    def _register_asset(self, image, submitter=None):
        content = Path(self.temporary_media.name, image.image.name).read_bytes()
        return MediaAsset.objects.create(
            submitter=submitter or self.owner,
            image=image,
            content_sha256=hashlib.sha256(content).hexdigest(),
        )

    def _assert_delete_continues_after_storage_error(
        self, fail_at
    ):
        image = create_image()
        pin = create_pin(self.owner, image, [])
        files_before = self._assert_four_image_files(image)
        original_name = image.image.name
        real_delete = remove_media_file
        attempted = []

        def fail_one_delete(root_directory, name):
            attempted.append(name)
            if len(attempted) == fail_at:
                raise OSError("secret storage location")
            return real_delete(root_directory, name)

        with mock.patch(
            "django_images.services.media_deletion.remove_media_file",
            side_effect=fail_one_delete,
        ):
            response = self.client.delete(reverse("pin-detail", args=[pin.pk]))

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Pin.objects.filter(pk=pin.pk).exists())
        self.assertFalse(Image.objects.filter(pk=image.pk).exists())
        self.assertFalse(
            Thumbnail.objects.filter(original_id=image.pk).exists()
        )
        self.assertEqual(len(attempted), 4)
        self.assertEqual(set(attempted), set(files_before))
        failed_name = attempted[fail_at - 1]
        self.assertEqual(
            media_snapshot(self.temporary_media.name),
            {failed_name: files_before[failed_name]},
        )
        pending = self._pending_deletions().get()
        self.assertEqual(pending.name, failed_name)
        self.assertEqual(
            pending.kind,
            "original" if failed_name == original_name else "thumbnail",
        )
        self.assertEqual(pending.attempts, 1)
        self.assertEqual(pending.last_error, "OSError")
        return pending

    def test_conditional_delete_preserves_pin_shared_after_selection(self):
        image = create_image()
        pin = create_pin(self.owner, image, [])
        asset = self._register_asset(image)
        source = Board.objects.create(
            submitter=self.owner,
            name="conditional-shared-source",
        )
        other_board = Board.objects.create(
            submitter=self.owner,
            name="conditional-shared-other",
        )
        source.pins.add(pin)
        other_board.pins.add(pin)
        files_before = self._assert_four_image_files(image)
        directories = self._asset_directories(image)

        result = pin.delete_if_exclusive_to_board(
            self.owner.pk,
            source.pk,
        )

        self.assertEqual(result, ("preserved", "shared_pin"))
        self.assertTrue(Pin.objects.filter(pk=pin.pk).exists())
        self.assertTrue(Image.objects.filter(pk=image.pk).exists())
        self.assertTrue(MediaAsset.objects.filter(pk=asset.pk).exists())
        self.assertEqual(
            Thumbnail.objects.filter(original_id=image.pk).count(),
            3,
        )
        self.assertEqual(
            media_snapshot(self.temporary_media.name),
            files_before,
        )
        self.assertTrue(all(path.is_dir() for path in directories))

    def test_conditional_delete_uses_autocommit(self):
        image = create_image()
        pin = create_pin(self.owner, image, [])
        asset = self._register_asset(image)
        source = Board.objects.create(
            submitter=self.owner,
            name="conditional-autocommit-source",
        )
        source.pins.add(pin)
        files_before = self._assert_four_image_files(image)

        with transaction.atomic():
            with self.assertRaisesMessage(
                RuntimeError,
                "registered_pin_delete_requires_autocommit",
            ):
                pin.delete_if_exclusive_to_board(
                    self.owner.pk,
                    source.pk,
                )

        self.assertTrue(Pin.objects.filter(pk=pin.pk).exists())
        self.assertTrue(Image.objects.filter(pk=image.pk).exists())
        self.assertTrue(MediaAsset.objects.filter(pk=asset.pk).exists())
        self.assertEqual(
            media_snapshot(self.temporary_media.name),
            files_before,
        )

    def test_conditional_delete_exclusive_registered_pin_removes_media(self):
        image = create_image()
        image_id = image.pk
        pin = create_pin(self.owner, image, [])
        pin_id = pin.pk
        asset = self._register_asset(image)
        source = Board.objects.create(
            submitter=self.owner,
            name="conditional-exclusive-source",
        )
        source.pins.add(pin)
        self._assert_four_image_files(image)
        directories = self._asset_directories(image)

        result = pin.delete_if_exclusive_to_board(
            self.owner.pk,
            source.pk,
        )

        self.assertEqual(result, ("deleted", None))
        self.assertIsNone(pin.pk)
        self.assertFalse(Pin.objects.filter(pk=pin_id).exists())
        self.assertFalse(Image.objects.filter(pk=image_id).exists())
        self.assertFalse(MediaAsset.objects.filter(pk=asset.pk).exists())
        self.assertFalse(
            Thumbnail.objects.filter(original_id=image_id).exists()
        )
        self.assertEqual(media_snapshot(self.temporary_media.name), {})
        self.assertTrue(all(not path.exists() for path in directories))
        root = Path(self.temporary_media.name)
        self.assertTrue((root / "originals").is_dir())
        self.assertTrue((root / "derivatives").is_dir())

    def test_conditional_delete_preserves_media_shared_by_another_pin(self):
        image = create_image()
        pin = create_pin(self.owner, image, [])
        pin_id = pin.pk
        other_pin = create_pin(self.other_user, image, [])
        asset = self._register_asset(image)
        source = Board.objects.create(
            submitter=self.owner,
            name="conditional-shared-image-source",
        )
        source.pins.add(pin)
        files_before = self._assert_four_image_files(image)
        directories = self._asset_directories(image)

        result = pin.delete_if_exclusive_to_board(
            self.owner.pk,
            source.pk,
        )

        self.assertEqual(result, ("deleted", None))
        self.assertFalse(Pin.objects.filter(pk=pin_id).exists())
        self.assertTrue(Pin.objects.filter(pk=other_pin.pk).exists())
        self.assertTrue(Image.objects.filter(pk=image.pk).exists())
        self.assertTrue(MediaAsset.objects.filter(pk=asset.pk).exists())
        self.assertEqual(
            Thumbnail.objects.filter(original_id=image.pk).count(),
            3,
        )
        self.assertEqual(
            media_snapshot(self.temporary_media.name),
            files_before,
        )
        self.assertTrue(all(path.is_dir() for path in directories))

    def test_conditional_delete_exclusive_legacy_pin_removes_media(self):
        image = create_image()
        image_id = image.pk
        pin = create_pin(self.owner, image, [])
        pin_id = pin.pk
        source = Board.objects.create(
            submitter=self.owner,
            name="conditional-legacy-source",
        )
        source.pins.add(pin)
        self._assert_four_image_files(image)
        directories = self._asset_directories(image)

        result = pin.delete_if_exclusive_to_board(
            self.owner.pk,
            source.pk,
        )

        self.assertEqual(result, ("deleted", None))
        self.assertIsNone(pin.pk)
        self.assertFalse(Pin.objects.filter(pk=pin_id).exists())
        self.assertFalse(Image.objects.filter(pk=image_id).exists())
        self.assertFalse(
            Thumbnail.objects.filter(original_id=image_id).exists()
        )
        self.assertEqual(media_snapshot(self.temporary_media.name), {})
        self.assertTrue(all(not path.exists() for path in directories))

    @skipUnless(
        connection.vendor == "sqlite",
        "SQLite file database write serialization observation",
    )
    def test_conditional_delete_serializes_competing_board_add(self):
        image = create_image()
        pin = create_pin(self.owner, image, [])
        pin_id = pin.pk
        self._register_asset(image)
        source = Board.objects.create(
            submitter=self.owner,
            name="conditional-race-source",
        )
        target = Board.objects.create(
            submitter=self.owner,
            name="conditional-race-target",
        )
        source.pins.add(pin)
        through = Board.pins.through
        through_table = connection.ops.quote_name(through._meta.db_table)
        board_column = connection.ops.quote_name(
            through._meta.get_field("board").column
        )
        pin_column = connection.ops.quote_name(
            through._meta.get_field("pin").column
        )
        insert_membership = "INSERT INTO {} ({}, {}) VALUES (%s, %s)".format(
            through_table,
            board_column,
            pin_column,
        )
        start = threading.Barrier(2)
        pin_row_deleted = threading.Event()
        add_started = threading.Event()
        add_finished = threading.Event()
        release_delete = threading.Event()
        outcomes = queue.Queue()
        original_delete = Pin.delete

        def pause_after_managed_pin_delete(instance, *args, **kwargs):
            result = original_delete(instance, *args, **kwargs)
            if getattr(instance, "_media_delete_managed", False):
                pin_row_deleted.set()
                if not release_delete.wait(5):
                    raise AssertionError("conditional delete was not released")
            return result

        def delete_exclusive_pin():
            close_old_connections()
            try:
                start.wait(timeout=5)
                current = Pin.objects.get(pk=pin_id)
                result = current.delete_if_exclusive_to_board(
                    self.owner.pk,
                    source.pk,
                )
                outcomes.put(("delete", result))
            except BaseException as error:
                outcomes.put((
                    "error",
                    ("delete", error, traceback.format_exc()),
                ))
            finally:
                connections["default"].close()

        def add_pin_to_other_board():
            close_old_connections()
            writer_connection = connections["default"]
            try:
                with writer_connection.cursor() as cursor:
                    cursor.execute("PRAGMA busy_timeout = 5000")
                start.wait(timeout=5)
                if not pin_row_deleted.wait(5):
                    raise AssertionError("conditional delete did not write")
                add_started.set()
                with writer_connection.cursor() as cursor:
                    cursor.execute(
                        insert_membership,
                        [target.pk, pin_id],
                    )
                outcomes.put(("add", "committed"))
            except BaseException as error:
                outcomes.put(("add", error.__class__.__name__))
            finally:
                writer_connection.close()
                add_finished.set()

        with mock.patch.object(
            Pin,
            "delete",
            new=pause_after_managed_pin_delete,
        ):
            workers = [
                threading.Thread(target=delete_exclusive_pin),
                threading.Thread(target=add_pin_to_other_board),
            ]
            for current in workers:
                current.start()
            self.assertTrue(pin_row_deleted.wait(5))
            self.assertTrue(add_started.wait(5))
            add_finished_while_locked = add_finished.wait(0.2)
            try:
                release_delete.set()
            finally:
                for current in workers:
                    current.join(timeout=10)

        self.assertFalse(any(current.is_alive() for current in workers))
        collected = [outcomes.get(timeout=1) for _worker in workers]
        errors = [value for kind, value in collected if kind == "error"]
        self.assertEqual(errors, [])
        delete_results = [
            value for kind, value in collected if kind == "delete"
        ]
        add_results = [value for kind, value in collected if kind == "add"]
        self.assertFalse(add_finished_while_locked, add_results)
        self.assertEqual(delete_results, [("deleted", None)])
        self.assertEqual(add_results, ["IntegrityError"])
        self.assertFalse(Pin.objects.filter(pk=pin_id).exists())
        self.assertFalse(
            through.objects.filter(
                board_id=target.pk,
                pin_id=pin_id,
            ).exists()
        )
        self.assertFalse(target.pins.filter(pk=pin_id).exists())

    @skipUnless(
        connection.vendor == "sqlite",
        "SQLite file database write serialization observation",
    )
    def test_conditional_bulk_delete_racing_membership_add_is_safe(self):
        if connection.creation.is_in_memory_db(
            connection.settings_dict["NAME"]
        ):
            self.skipTest("This concurrency contract requires file SQLite.")
        harness = _SQLiteConcurrencyHarness(self)
        image = create_image()
        pin = create_pin(self.owner, image, [])
        pin_id = pin.pk
        self._register_asset(image)
        source = Board.objects.create(
            submitter=self.owner,
            name="conditional-service-race-source",
        )
        other_board = Board.objects.create(
            submitter=self.owner,
            name="conditional-service-race-target",
        )
        source.pins.add(pin)
        secret_values = (
            "bulk-secret-token",
            "/private/media/bulk-source.png",
            "bulk-source.png",
            "OperationalError",
            "database is locked",
        )
        Pin.objects.filter(pk=pin_id).update(
            description=" ".join(secret_values),
            referer=secret_values[1],
        )
        pin_snapshot_ready = harness.barrier(2)
        membership_finished = harness.release_event()
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
                if not membership_finished.wait(5):
                    raise AssertionError("membership add did not finish")
            return result

        def delete_worker():
            close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(user=self.owner)
                client.raise_request_exception = False
                response = client.post(
                    "/api/v2/pins/bulk/",
                    {
                        "operation": "delete_if_exclusive_to_board",
                        "pin_ids": [pin_id],
                        "source_board_id": source.pk,
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
                close_old_connections()

        def membership_worker():
            close_old_connections()
            try:
                pin_snapshot_ready.wait(timeout=5)
                result = PinMembershipService().add_owned_pins(
                    self.owner,
                    other_board.pk,
                    [pin_id],
                )
                outcomes.put(("membership", result))
            except BaseException as error:
                outcomes.put(("error", "membership", error))
            finally:
                close_old_connections()
                membership_finished.set()

        with mock.patch.object(
            PinMembershipService,
            "lock_source_board_and_pin",
            side_effect=pause_delete_after_pin_snapshot,
        ):
            workers = [
                threading.Thread(
                    target=delete_worker,
                    name="conditional-delete-worker",
                ),
                threading.Thread(
                    target=membership_worker,
                    name="membership-add-worker",
                ),
            ]
            harness.start(workers)
            collected = harness.join_and_collect(outcomes, len(workers))

        errors = [item for item in collected if item[0] == "error"]
        self.assertEqual(errors, [], collected)
        results = {item[0]: item[1:] for item in collected}
        self.assertEqual(results["membership"], (["added"],))
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
        response_body = str(results["delete"])
        for secret in secret_values:
            self.assertNotIn(secret, response_body)
        self.assertTrue(Pin.objects.filter(pk=pin_id).exists())
        self.assertTrue(source.pins.filter(pk=pin_id).exists())
        self.assertTrue(other_board.pins.filter(pk=pin_id).exists())
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

    def test_direct_delete_preserves_shared_image_rows_files_and_uuid_directories(
        self,
    ):
        image = create_image()
        owner_pin = create_pin(self.owner, image, [])
        other_pin = create_pin(self.other_user, image, [])
        item = BatchImportItem.objects.create(
            submitter=self.owner,
            batch_id=uuid.uuid4(),
            client_item_id=uuid.uuid4(),
            request_fingerprint="d" * 64,
            state=BatchImportItem.SUCCEEDED,
            pin=owner_pin,
            lease_generation=1,
        )
        files_before = self._assert_four_image_files(image)
        asset_directories = self._asset_directories(image)
        self.assertTrue(all(path.is_dir() for path in asset_directories))
        response = self.client.delete(
            reverse("pin-detail", args=[owner_pin.pk])
        )

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Pin.objects.filter(pk=owner_pin.pk).exists())
        item.refresh_from_db()
        self.assertIsNone(item.pin_id)
        self.assertTrue(Pin.objects.filter(pk=other_pin.pk).exists())
        self.assertTrue(Image.objects.filter(pk=image.pk).exists())
        self.assertEqual(
            Thumbnail.objects.filter(original_id=image.pk).count(), 3
        )
        self.assertEqual(
            media_snapshot(self.temporary_media.name), files_before
        )
        self.assertTrue(all(path.is_dir() for path in asset_directories))
        self.assertFalse(self._pending_deletions().exists())

    def test_direct_delete_of_last_reference_removes_rows_files_and_uuid_directories(
        self,
    ):
        image = create_image()
        pin = create_pin(self.owner, image, [])
        self._assert_four_image_files(image)
        asset_directories = self._asset_directories(image)
        self.assertTrue(all(path.is_dir() for path in asset_directories))
        response = self.client.delete(reverse("pin-detail", args=[pin.pk]))

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Pin.objects.filter(pk=pin.pk).exists())
        self.assertFalse(Image.objects.filter(pk=image.pk).exists())
        self.assertFalse(
            Thumbnail.objects.filter(original_id=image.pk).exists()
        )
        self.assertEqual(media_snapshot(self.temporary_media.name), {})
        self.assertTrue(
            all(not path.exists() for path in asset_directories)
        )
        root = Path(self.temporary_media.name)
        self.assertTrue((root / "originals").is_dir())
        self.assertTrue((root / "derivatives").is_dir())

    def test_registered_last_delete_takes_stripe_before_atomic_recheck(self):
        image = create_image()
        pin = create_pin(self.owner, image, [])
        asset = self._register_asset(image)
        events = []

        class RecordingStripe(object):
            def __enter__(inner_self):
                events.append(("stripe_enter", connection.in_atomic_block))
                return inner_self

            def __exit__(inner_self, error_type, error, traceback):
                del error, traceback
                events.append((
                    "stripe_exit",
                    error_type.__name__ if error_type else None,
                ))
                return False

        with mock.patch(
            "core.models.media_dedup_lock",
            return_value=RecordingStripe(),
            create=True,
        ) as dedup_lock:
            pin.delete()

        self.assertTrue(events, "registered delete did not enter dedup stripe")
        self.assertEqual(events[0], ("stripe_enter", False))
        self.assertEqual(events[-1], ("stripe_exit", None))
        root_directory = dedup_lock.call_args[0][0]
        self.assertEqual(dedup_lock.call_args[0][1:], (
            asset.submitter_id,
            asset.content_sha256,
        ))
        self.assertFalse(root_directory.descriptors)
        self.assertFalse(Pin.objects.filter(pk=pin.pk).exists())
        self.assertFalse(Image.objects.filter(pk=image.pk).exists())
        self.assertFalse(MediaAsset.objects.filter(pk=asset.pk).exists())
        self.assertEqual(media_snapshot(self.temporary_media.name), {})

    def test_registered_shared_delete_preserves_asset_rows_files_and_dirs(
        self,
    ):
        image = create_image()
        first_pin = create_pin(self.owner, image, [])
        second_pin = create_pin(self.owner, image, [])
        asset = self._register_asset(image)
        files_before = self._assert_four_image_files(image)
        directories = self._asset_directories(image)

        first_pin.delete()

        self.assertFalse(Pin.objects.filter(pk=first_pin.pk).exists())
        self.assertTrue(Pin.objects.filter(pk=second_pin.pk).exists())
        self.assertTrue(Image.objects.filter(pk=image.pk).exists())
        self.assertTrue(MediaAsset.objects.filter(pk=asset.pk).exists())
        self.assertEqual(
            Thumbnail.objects.filter(original_id=image.pk).count(), 3
        )
        self.assertEqual(
            media_snapshot(self.temporary_media.name), files_before
        )
        self.assertTrue(all(path.is_dir() for path in directories))

    def test_registered_last_delete_removes_asset_rows_files_and_dirs(self):
        image = create_image()
        pin = create_pin(self.owner, image, [])
        asset = self._register_asset(image)
        self._assert_four_image_files(image)
        directories = self._asset_directories(image)

        pin.delete()

        self.assertFalse(Pin.objects.filter(pk=pin.pk).exists())
        self.assertFalse(Image.objects.filter(pk=image.pk).exists())
        self.assertFalse(MediaAsset.objects.filter(pk=asset.pk).exists())
        self.assertFalse(
            Thumbnail.objects.filter(original_id=image.pk).exists()
        )
        self.assertEqual(media_snapshot(self.temporary_media.name), {})
        self.assertTrue(all(not path.exists() for path in directories))

    def test_registered_delete_rejects_existing_database_transaction(self):
        image = create_image()
        pin = create_pin(self.owner, image, [])
        asset = self._register_asset(image)
        files_before = self._assert_four_image_files(image)

        with mock.patch("core.models.media_dedup_lock") as dedup_lock:
            with transaction.atomic():
                with self.assertRaisesRegex(
                    RuntimeError,
                    "registered_pin_delete_requires_autocommit",
                ):
                    pin.delete()

        dedup_lock.assert_not_called()
        self.assertTrue(Pin.objects.filter(pk=pin.pk).exists())
        self.assertTrue(Image.objects.filter(pk=image.pk).exists())
        self.assertTrue(MediaAsset.objects.filter(pk=asset.pk).exists())
        self.assertEqual(
            media_snapshot(self.temporary_media.name), files_before
        )

    def test_registered_committed_delete_survives_stripe_unlock_error(self):
        image = create_image()
        pin = create_pin(self.owner, image, [])
        asset = self._register_asset(image)
        self._assert_four_image_files(image)

        class UnlockFailureStripe(object):
            def __enter__(inner_self):
                return inner_self

            def __exit__(inner_self, error_type, error, traceback):
                del error_type, error, traceback
                raise RuntimeError("private unlock error")

        with mock.patch(
            "core.models.media_dedup_lock",
            return_value=UnlockFailureStripe(),
        ), self.assertLogs("core.models", level="WARNING"):
            result = pin.delete()

        self.assertGreater(result[0], 0)
        self.assertIsNone(pin.pk)
        self.assertFalse(Image.objects.filter(pk=image.pk).exists())
        self.assertFalse(MediaAsset.objects.filter(pk=asset.pk).exists())
        self.assertEqual(media_snapshot(self.temporary_media.name), {})

    def test_registered_committed_delete_survives_root_close_error(self):
        image = create_image()
        pin = create_pin(self.owner, image, [])
        asset = self._register_asset(image)
        root_directory = file_ops.open_media_root(
            self.temporary_media.name
        )
        real_close = root_directory.close

        def close_then_fail():
            real_close()
            raise RuntimeError("private root close error")

        with mock.patch(
            "core.models.open_media_root",
            return_value=root_directory,
        ), mock.patch.object(
            root_directory,
            "close",
            side_effect=close_then_fail,
        ), self.assertLogs("core.models", level="WARNING"):
            result = pin.delete()

        self.assertGreater(result[0], 0)
        self.assertIsNone(pin.pk)
        self.assertFalse(root_directory.descriptors)
        self.assertFalse(Image.objects.filter(pk=image.pk).exists())
        self.assertFalse(MediaAsset.objects.filter(pk=asset.pk).exists())
        self.assertEqual(media_snapshot(self.temporary_media.name), {})

    def test_registered_delete_preserves_primary_when_root_close_fails(self):
        image = create_image()
        pin = create_pin(self.owner, image, [])
        asset = self._register_asset(image)
        files_before = self._assert_four_image_files(image)
        root_directory = file_ops.open_media_root(
            self.temporary_media.name
        )
        real_close = root_directory.close
        primary = KeyboardInterrupt()

        def close_then_fail():
            real_close()
            raise RuntimeError("secondary root close error")

        with mock.patch(
            "core.models.open_media_root",
            return_value=root_directory,
        ), mock.patch.object(
            root_directory,
            "close",
            side_effect=close_then_fail,
        ), mock.patch.object(
            BaseImage,
            "delete",
            side_effect=primary,
        ):
            with self.assertRaises(KeyboardInterrupt) as caught:
                pin.delete()

        self.assertIs(caught.exception, primary)
        self.assertFalse(root_directory.descriptors)
        self.assertTrue(Pin.objects.filter(pk=pin.pk).exists())
        self.assertTrue(Image.objects.filter(pk=image.pk).exists())
        self.assertTrue(MediaAsset.objects.filter(pk=asset.pk).exists())
        self.assertEqual(
            media_snapshot(self.temporary_media.name), files_before
        )

    def test_registered_identity_change_fails_closed_without_pin_delete(self):
        image = create_image()
        pin = create_pin(self.owner, image, [])
        asset = self._register_asset(image)
        files_before = self._assert_four_image_files(image)

        class RegistryReplacingStripe(object):
            def __enter__(inner_self):
                MediaAsset.objects.filter(pk=asset.pk).update(
                    content_sha256="b" * 64
                )
                return inner_self

            def __exit__(inner_self, error_type, error, traceback):
                del error_type, error, traceback
                return False

        with mock.patch(
            "core.models.media_dedup_lock",
            return_value=RegistryReplacingStripe(),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "registered_media_identity_changed",
            ):
                pin.delete()

        asset.refresh_from_db()
        self.assertEqual(asset.content_sha256, "b" * 64)
        self.assertTrue(Pin.objects.filter(pk=pin.pk).exists())
        self.assertTrue(Image.objects.filter(pk=image.pk).exists())
        self.assertEqual(
            media_snapshot(self.temporary_media.name), files_before
        )

    def test_registered_delete_rechecks_image_manifest_before_file_delete(
        self,
    ):
        image = create_image()
        pin = create_pin(self.owner, image, [])
        asset = self._register_asset(image)
        self._assert_four_image_files(image)
        foreign_uuid = uuid.uuid4()
        foreign_name = "originals/{}/foreign.png".format(foreign_uuid)
        foreign_path = Path(self.temporary_media.name, foreign_name)
        foreign_path.parent.mkdir(parents=True)
        foreign_path.write_bytes(b"foreign-original")
        files_before = media_snapshot(self.temporary_media.name)

        class ImageManifestReplacingStripe(object):
            def __enter__(inner_self):
                BaseImage.objects.filter(pk=image.pk).update(
                    asset_uuid=foreign_uuid,
                    image=foreign_name,
                    original_filename="foreign.png",
                )
                return inner_self

            def __exit__(inner_self, error_type, error, traceback):
                del error_type, error, traceback
                return False

        caught = None
        try:
            with mock.patch(
                "core.models.media_dedup_lock",
                return_value=ImageManifestReplacingStripe(),
            ):
                pin.delete()
        except RuntimeError as error:
            caught = error

        self.assertEqual(
            media_snapshot(self.temporary_media.name),
            files_before,
        )
        self.assertIsNotNone(caught)
        self.assertEqual(str(caught), "registered_media_identity_changed")
        self.assertTrue(Pin.objects.filter(pk=pin.pk).exists())
        self.assertTrue(Image.objects.filter(pk=image.pk).exists())
        self.assertTrue(MediaAsset.objects.filter(pk=asset.pk).exists())
        self.assertFalse(self._pending_deletions().exists())

    def test_registered_delete_rechecks_each_thumbnail_manifest_field(self):
        mutation_fields = ("size", "image", "width", "height")

        for field_name in mutation_fields:
            with self.subTest(field_name=field_name):
                case_owner = create_user(
                    "thumbnail-manifest-{}".format(field_name)
                )
                image = create_image()
                pin = create_pin(case_owner, image, [])
                asset = self._register_asset(image, submitter=case_owner)
                thumbnail = image.thumbnail_set.get(size="thumbnail")
                if field_name == "size":
                    replacement = "changed-size"
                elif field_name == "image":
                    foreign_uuid = uuid.uuid4()
                    replacement = (
                        "derivatives/{}/thumbnail.png".format(foreign_uuid)
                    )
                    foreign_path = Path(
                        self.temporary_media.name,
                        replacement,
                    )
                    foreign_path.parent.mkdir(parents=True)
                    foreign_path.write_bytes(b"foreign-thumbnail")
                else:
                    replacement = getattr(thumbnail, field_name) + 1
                files_before = media_snapshot(self.temporary_media.name)

                class ThumbnailManifestReplacingStripe(object):
                    def __enter__(inner_self):
                        Thumbnail.objects.filter(pk=thumbnail.pk).update(
                            **{field_name: replacement}
                        )
                        return inner_self

                    def __exit__(
                        inner_self,
                        error_type,
                        error,
                        traceback,
                    ):
                        del error_type, error, traceback
                        return False

                with mock.patch(
                    "core.models.media_dedup_lock",
                    return_value=ThumbnailManifestReplacingStripe(),
                ):
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "registered_media_identity_changed",
                    ):
                        pin.delete()

                self.assertTrue(Pin.objects.filter(pk=pin.pk).exists())
                self.assertTrue(Image.objects.filter(pk=image.pk).exists())
                self.assertTrue(MediaAsset.objects.filter(pk=asset.pk).exists())
                self.assertEqual(
                    media_snapshot(self.temporary_media.name),
                    files_before,
                )

    def test_registered_last_delete_racing_same_hash_reupload_is_consistent(
        self,
    ):
        image = create_image()
        pin = create_pin(self.owner, image, [])
        self._register_asset(image)
        original_content = Path(
            self.temporary_media.name,
            image.image.name,
        ).read_bytes()
        start = threading.Barrier(2)
        events = queue.Queue()

        def delete_last_pin():
            close_old_connections()
            try:
                start.wait(timeout=5)
                Pin.objects.get(pk=pin.pk).delete()
                events.put(("deleted", pin.pk))
            except BaseException as error:
                events.put((
                    "error",
                    ("delete", error, traceback.format_exc()),
                ))
            finally:
                connections["default"].close()

        def reupload_same_hash():
            close_old_connections()
            try:
                user = type(self.owner).objects.get(pk=self.owner.pk)
                storage = MediaStorage(
                    media_root=self.temporary_media.name,
                    clock=lambda: 10.0,
                )
                service = PinImportService(
                    fetcher=None,
                    media_storage=storage,
                    idempotency=IdempotencyStore(),
                    clock=lambda: 10.0,
                )
                prepared = service.prepare_upload(
                    SimpleUploadedFile(
                        "reupload.png",
                        original_content,
                        content_type="image/png",
                    ),
                    deadline=20.0,
                )
                start.wait(timeout=5)
                uploaded_pin = service.commit(
                    prepared,
                    user,
                    ImportMetadata(
                        url=None,
                        referer=None,
                        description="raced reupload",
                        private=False,
                        tags=(),
                        board_ids=(),
                    ),
                    claim=None,
                    deadline=20.0,
                )
                events.put(("uploaded", uploaded_pin.pk))
            except BaseException as error:
                events.put((
                    "error",
                    ("upload", error, traceback.format_exc()),
                ))
            finally:
                connections["default"].close()

        workers = [
            threading.Thread(target=delete_last_pin),
            threading.Thread(target=reupload_same_hash),
        ]
        for current in workers:
            current.start()
        for current in workers:
            current.join(timeout=10)

        self.assertFalse(any(current.is_alive() for current in workers))
        collected = [events.get(timeout=1) for _worker in workers]
        errors = [value for kind, value in collected if kind == "error"]
        self.assertEqual(errors, [])
        self.assertEqual(
            sorted(kind for kind, _value in collected),
            ["deleted", "uploaded"],
        )
        live_pin = Pin.objects.get()
        live_image = Image.objects.get(pk=live_pin.image_id)
        self.assertFalse(Pin.objects.filter(pk=pin.pk).exists())
        self.assertEqual(live_pin.description, "raced reupload")
        self.assertEqual(Image.objects.count(), 1)
        self.assertEqual(Thumbnail.objects.count(), 3)
        self.assertEqual(MediaAsset.objects.count(), 1)
        self.assertEqual(len(media_snapshot(self.temporary_media.name)), 4)
        self._assert_four_image_files(live_image)
        self.assertFalse(self._pending_deletions().exists())

    def test_first_storage_failure_is_journaled_without_stopping_cleanup(self):
        self._assert_delete_continues_after_storage_error(1)

    def test_middle_storage_failure_is_retried_by_management_command(self):
        pending = (
            self._assert_delete_continues_after_storage_error(2)
        )
        expected_pending = list(
            self._pending_deletions().values_list(
                "kind", "name", "attempts", "last_error"
            )
        )
        files_before_dry_run = media_snapshot(self.temporary_media.name)
        dry_run = StringIO()

        call_command("retry_media_deletions", stdout=dry_run)

        self.assertEqual(
            list(
                self._pending_deletions().values_list(
                    "kind", "name", "attempts", "last_error"
                )
            ),
            expected_pending,
        )
        self.assertEqual(
            media_snapshot(self.temporary_media.name), files_before_dry_run
        )
        self.assertIn("pending=1 processed=0 remaining=1", dry_run.getvalue())

        execute = StringIO()
        call_command(
            "retry_media_deletions", execute=True, stdout=execute
        )

        self.assertFalse(self._pending_deletions().exists())
        self.assertEqual(media_snapshot(self.temporary_media.name), {})
        storage_model = (
            BaseImage if pending.kind == "original" else Thumbnail
        )
        storage = storage_model._meta.get_field("image").storage
        self.assertFalse(storage.exists(pending.name))
        self.assertIn("pending=1 processed=1 remaining=0", execute.getvalue())

    def test_last_storage_failure_is_journaled_without_stopping_cleanup(self):
        self._assert_delete_continues_after_storage_error(4)

    def test_malformed_legacy_name_cannot_delete_unrelated_file(self):
        target = Path(self.temporary_media.name, "target.dat")
        target.write_bytes(b"unrelated-target")
        image = Image.objects.create(
            image="safe/../target.dat",
            original_filename="legacy.dat",
            width=32,
            height=32,
        )
        pin = create_pin(self.owner, image, [])
        response = self.client.delete(reverse("pin-detail", args=[pin.pk]))

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Pin.objects.filter(pk=pin.pk).exists())
        self.assertFalse(Image.objects.filter(pk=image.pk).exists())
        self.assertEqual(target.read_bytes(), b"unrelated-target")
        pending = self._pending_deletions().get(
            kind="original", name="safe/../target.dat"
        )
        self.assertEqual(pending.attempts, 1)
        self.assertEqual(pending.last_error, "InvalidMediaName")

    def test_queryset_delete_preserves_media_while_reference_remains(self):
        image = create_image()
        first_pin = create_pin(self.owner, image, [])
        second_pin = create_pin(self.other_user, image, [])
        files_before = self._assert_four_image_files(image)

        Pin.objects.filter(pk=first_pin.pk).delete()

        self.assertFalse(Pin.objects.filter(pk=first_pin.pk).exists())
        self.assertTrue(Pin.objects.filter(pk=second_pin.pk).exists())
        self.assertTrue(Image.objects.filter(pk=image.pk).exists())
        self.assertEqual(
            Thumbnail.objects.filter(original_id=image.pk).count(), 3
        )
        self.assertEqual(
            media_snapshot(self.temporary_media.name), files_before
        )

    def test_registered_queryset_delete_locks_before_pin_row_mutation(self):
        image = create_image()
        pin = create_pin(self.owner, image, [])
        asset = self._register_asset(image)
        events = []

        class RecordingStripe(object):
            def __enter__(inner_self):
                events.append((
                    connection.in_atomic_block,
                    Pin.objects.filter(pk=pin.pk).exists(),
                ))
                return inner_self

            def __exit__(inner_self, error_type, error, traceback):
                del error_type, error, traceback
                return False

        with mock.patch(
            "core.models.media_dedup_lock",
            return_value=RecordingStripe(),
        ):
            Pin.objects.filter(pk=pin.pk).delete()

        self.assertEqual(events, [(False, True)])
        self.assertFalse(Pin.objects.filter(pk=pin.pk).exists())
        self.assertFalse(Image.objects.filter(pk=image.pk).exists())
        self.assertFalse(MediaAsset.objects.filter(pk=asset.pk).exists())
        self.assertEqual(media_snapshot(self.temporary_media.name), {})

    def test_registered_queryset_uses_distinct_pin_cardinality_for_joins(self):
        image = create_image()
        pin = create_pin(self.owner, image, ["first", "second"])
        asset = self._register_asset(image)
        queryset = Pin.objects.filter(
            tags__name__in=("first", "second")
        )
        self.assertEqual(
            list(queryset.values_list("pk", flat=True)),
            [pin.pk, pin.pk],
        )

        queryset.delete()

        self.assertFalse(Pin.objects.filter(pk=pin.pk).exists())
        self.assertFalse(Image.objects.filter(pk=image.pk).exists())
        self.assertFalse(MediaAsset.objects.filter(pk=asset.pk).exists())
        self.assertEqual(media_snapshot(self.temporary_media.name), {})

    def test_registered_queryset_cardinality_query_reads_at_most_two_ids(self):
        pins = []
        for index in range(3):
            owner = create_user("bounded-cardinality-{}".format(index))
            image = create_image()
            pins.append(create_pin(owner, image, []))
            self._register_asset(image, submitter=owner)
        queryset = Pin.objects.filter(
            pk__in=[pin.pk for pin in pins]
        ).order_by("pk")

        with CaptureQueriesContext(connection) as captured:
            with self.assertRaisesRegex(
                RuntimeError,
                "registered_pin_bulk_delete_unsupported",
            ):
                queryset.delete()

        cardinality_queries = [
            query["sql"] for query in captured.captured_queries
            if 'FROM "core_pin"' in query["sql"]
            and query["sql"].lstrip().startswith("SELECT")
        ]
        self.assertTrue(any(
            'SELECT DISTINCT "core_pin"."id"' in query
            and "LIMIT 2" in query
            for query in cardinality_queries
        ), cardinality_queries)
        self.assertEqual(
            set(Pin.objects.values_list("pk", flat=True)),
            {pin.pk for pin in pins},
        )

    def test_registered_queryset_preserves_slice_and_values_guards(self):
        image = create_image()
        pin = create_pin(self.owner, image, [])
        asset = self._register_asset(image)
        files_before = self._assert_four_image_files(image)

        with self.assertRaisesRegex(
            AssertionError,
            "Cannot use 'limit' or 'offset' with delete",
        ):
            Pin.objects.filter(pk=pin.pk)[:1].delete()
        with self.assertRaisesRegex(
            TypeError,
            "Cannot call delete.*values",
        ):
            Pin.objects.filter(pk=pin.pk).values("pk").delete()

        self.assertTrue(Pin.objects.filter(pk=pin.pk).exists())
        self.assertTrue(Image.objects.filter(pk=image.pk).exists())
        self.assertTrue(MediaAsset.objects.filter(pk=asset.pk).exists())
        self.assertEqual(
            media_snapshot(self.temporary_media.name),
            files_before,
        )

    def test_registered_queryset_delete_clears_evaluated_cache(self):
        image = create_image()
        pin = create_pin(self.owner, image, [])
        self._register_asset(image)
        queryset = Pin.objects.filter(pk=pin.pk)
        self.assertEqual(list(queryset), [pin])
        self.assertIsNotNone(queryset._result_cache)

        queryset.delete()

        self.assertIsNone(queryset._result_cache)
        self.assertEqual(list(queryset), [])

    def test_registered_queryset_delete_ignores_select_for_update_probe(self):
        image = create_image()
        pin = create_pin(self.owner, image, [])
        asset = self._register_asset(image)
        self._assert_four_image_files(image)

        with mock.patch.object(
            connection.features,
            "has_select_for_update",
            True,
        ), mock.patch.object(
            connection.ops,
            "for_update_sql",
            return_value="",
        ):
            Pin.objects.select_for_update().filter(pk=pin.pk).delete()

        self.assertFalse(Pin.objects.filter(pk=pin.pk).exists())
        self.assertFalse(Image.objects.filter(pk=image.pk).exists())
        self.assertFalse(MediaAsset.objects.filter(pk=asset.pk).exists())
        self.assertEqual(media_snapshot(self.temporary_media.name), {})

    def test_stale_missing_single_probe_does_not_delete_new_matches(self):
        candidate_image = create_image()
        candidate = create_pin(self.owner, candidate_image, [])
        candidate.description = "stale-probe-target"
        candidate.save(update_fields=("description",))
        first_image = create_image()
        first = create_pin(self.owner, first_image, [])
        first_asset = self._register_asset(first_image)
        second_image = create_image()
        second = create_pin(self.other_user, second_image, [])
        second_asset = self._register_asset(
            second_image,
            submitter=self.other_user,
        )
        files_before = media_snapshot(self.temporary_media.name)
        real_using = Pin._base_manager.using
        swapped = {"value": False}

        def swap_matches_before_candidate_fetch(database_alias):
            if not swapped["value"]:
                swapped["value"] = True
                with connection.cursor() as cursor:
                    cursor.execute(
                        'DELETE FROM "core_pin" WHERE "id" = %s',
                        (candidate.pk,),
                    )
                    cursor.execute(
                        'UPDATE "core_pin" SET "description" = %s '
                        'WHERE "id" IN (%s, %s)',
                        (
                            "stale-probe-target",
                            first.pk,
                            second.pk,
                        ),
                    )
            return real_using(database_alias)

        queryset = Pin.objects.filter(description="stale-probe-target")
        with mock.patch.object(
            Pin._base_manager,
            "using",
            side_effect=swap_matches_before_candidate_fetch,
        ):
            result = queryset.delete()

        self.assertEqual(result, (0, {}))
        self.assertTrue(swapped["value"])
        self.assertFalse(Pin.objects.filter(pk=candidate.pk).exists())
        self.assertEqual(
            set(Pin.objects.values_list("pk", flat=True)),
            {first.pk, second.pk},
        )
        self.assertTrue(Image.objects.filter(pk=first_image.pk).exists())
        self.assertTrue(Image.objects.filter(pk=second_image.pk).exists())
        self.assertTrue(MediaAsset.objects.filter(pk=first_asset.pk).exists())
        self.assertTrue(MediaAsset.objects.filter(pk=second_asset.pk).exists())
        self.assertEqual(
            media_snapshot(self.temporary_media.name),
            files_before,
        )

    def test_stale_legacy_single_probe_deletes_only_probed_pk(self):
        candidate_image = create_image()
        candidate = create_pin(self.owner, candidate_image, [])
        candidate.description = "stale-legacy-target"
        candidate.save(update_fields=("description",))
        first_image = create_image()
        first = create_pin(self.owner, first_image, [])
        first_asset = self._register_asset(first_image)
        second_image = create_image()
        second = create_pin(self.other_user, second_image, [])
        second_asset = self._register_asset(
            second_image,
            submitter=self.other_user,
        )
        first_paths = {
            first_image.image.name,
            *first_image.thumbnail_set.values_list("image", flat=True),
        }
        second_paths = {
            second_image.image.name,
            *second_image.thumbnail_set.values_list("image", flat=True),
        }
        real_using = Pin._base_manager.using
        swapped = {"value": False}

        def add_matches_before_candidate_fetch(database_alias):
            if not swapped["value"]:
                swapped["value"] = True
                with connection.cursor() as cursor:
                    cursor.execute(
                        'UPDATE "core_pin" SET "description" = %s '
                        'WHERE "id" IN (%s, %s)',
                        (
                            "stale-legacy-target",
                            first.pk,
                            second.pk,
                        ),
                    )
            return real_using(database_alias)

        queryset = Pin.objects.filter(description="stale-legacy-target")
        with mock.patch.object(
            Pin._base_manager,
            "using",
            side_effect=add_matches_before_candidate_fetch,
        ):
            queryset.delete()

        self.assertTrue(swapped["value"])
        self.assertFalse(Pin.objects.filter(pk=candidate.pk).exists())
        self.assertFalse(Image.objects.filter(pk=candidate_image.pk).exists())
        self.assertEqual(
            set(Pin.objects.values_list("pk", flat=True)),
            {first.pk, second.pk},
        )
        self.assertTrue(Image.objects.filter(pk=first_image.pk).exists())
        self.assertTrue(Image.objects.filter(pk=second_image.pk).exists())
        self.assertTrue(MediaAsset.objects.filter(pk=first_asset.pk).exists())
        self.assertTrue(MediaAsset.objects.filter(pk=second_asset.pk).exists())
        self.assertEqual(
            set(media_snapshot(self.temporary_media.name)),
            first_paths | second_paths,
        )

    def test_legacy_multi_queryset_deletes_all_matching_pins(self):
        pins = []
        images = []
        for index in range(3):
            image = create_image()
            pin = create_pin(self.owner, image, [])
            pin.description = "three-legacy-candidates"
            pin.save(update_fields=("description",))
            images.append(image)
            pins.append(pin)

        result = Pin.objects.filter(
            description="three-legacy-candidates"
        ).delete()

        self.assertEqual(result, (3, {"core.Pin": 3}))
        self.assertFalse(Pin.objects.filter(
            pk__in=[pin.pk for pin in pins]
        ).exists())
        self.assertFalse(Image.objects.filter(
            pk__in=[image.pk for image in images]
        ).exists())
        self.assertEqual(media_snapshot(self.temporary_media.name), {})

    def test_mixed_multi_queryset_fails_before_any_candidate_delete(self):
        pins = []
        images = []
        for index in range(3):
            image = create_image()
            pin = create_pin(self.owner, image, [])
            pin.description = "mixed-multi-candidates"
            pin.save(update_fields=("description",))
            images.append(image)
            pins.append(pin)
        asset = self._register_asset(images[-1])
        files_before = media_snapshot(self.temporary_media.name)

        with self.assertRaisesRegex(
            RuntimeError,
            "registered_pin_bulk_delete_unsupported",
        ):
            Pin.objects.filter(
                description="mixed-multi-candidates"
            ).delete()

        self.assertEqual(
            set(Pin.objects.values_list("pk", flat=True)),
            {pin.pk for pin in pins},
        )
        self.assertEqual(
            set(Image.objects.values_list("pk", flat=True)),
            {image.pk for image in images},
        )
        self.assertTrue(MediaAsset.objects.filter(pk=asset.pk).exists())
        self.assertEqual(
            media_snapshot(self.temporary_media.name),
            files_before,
        )

    def test_registered_image_cascade_delete_is_not_queryset_guarded(self):
        image = create_image()
        pin = create_pin(self.owner, image, [])
        asset = self._register_asset(image)
        self._assert_four_image_files(image)
        caught = None

        try:
            image.delete()
        except RuntimeError as error:
            caught = error

        self.assertIsNone(caught)
        self.assertFalse(Pin.objects.filter(pk=pin.pk).exists())
        self.assertFalse(Image.objects.filter(pk=image.pk).exists())
        self.assertFalse(MediaAsset.objects.filter(pk=asset.pk).exists())
        self.assertEqual(media_snapshot(self.temporary_media.name), {})

    def test_registered_user_cascade_delete_is_not_queryset_guarded(self):
        owner_id = self.owner.pk
        image = create_image()
        pin = create_pin(self.owner, image, [])
        asset = self._register_asset(image)
        self._assert_four_image_files(image)
        caught = None

        try:
            self.owner.delete()
        except RuntimeError as error:
            caught = error

        self.assertIsNone(caught)
        self.assertFalse(type(self.owner).objects.filter(pk=owner_id).exists())
        self.assertFalse(Pin.objects.filter(pk=pin.pk).exists())
        self.assertFalse(Image.objects.filter(pk=image.pk).exists())
        self.assertFalse(MediaAsset.objects.filter(pk=asset.pk).exists())
        self.assertEqual(media_snapshot(self.temporary_media.name), {})

    def test_registered_multi_queryset_fails_before_partial_delete(self):
        image = create_image()
        first_pin = create_pin(self.owner, image, [])
        second_pin = create_pin(self.owner, image, [])
        asset = self._register_asset(image)
        files_before = self._assert_four_image_files(image)
        stripe_entries = {"value": 0}

        class SecondStripeFailure(object):
            def __enter__(inner_self):
                stripe_entries["value"] += 1
                if stripe_entries["value"] == 2:
                    raise RuntimeError("second stripe failed")
                return inner_self

            def __exit__(inner_self, error_type, error, traceback):
                del error_type, error, traceback
                return False

        with mock.patch(
            "core.models.media_dedup_lock",
            side_effect=lambda *_args, **_kwargs: SecondStripeFailure(),
        ):
            with self.assertRaises(RuntimeError):
                Pin.objects.filter(
                    pk__in=(first_pin.pk, second_pin.pk)
                ).order_by("pk").delete()

        self.assertEqual(stripe_entries["value"], 0)
        self.assertEqual(
            set(Pin.objects.values_list("pk", flat=True)),
            {first_pin.pk, second_pin.pk},
        )
        self.assertTrue(Image.objects.filter(pk=image.pk).exists())
        self.assertTrue(MediaAsset.objects.filter(pk=asset.pk).exists())
        self.assertEqual(
            media_snapshot(self.temporary_media.name), files_before
        )

    def test_admin_delete_of_last_reference_removes_image_and_files(self):
        image = create_image()
        pin = create_pin(self.owner, image, [])
        self._assert_four_image_files(image)
        pin_admin = PinAdmin(Pin, admin.site)

        pin_admin.delete_model(None, pin)

        self.assertFalse(Pin.objects.filter(pk=pin.pk).exists())
        self.assertFalse(Image.objects.filter(pk=image.pk).exists())
        self.assertFalse(
            Thumbnail.objects.filter(original_id=image.pk).exists()
        )
        self.assertEqual(media_snapshot(self.temporary_media.name), {})

    def test_rolled_back_pin_delete_keeps_rows_media_and_uuid_directories(
        self,
    ):
        image = create_image()
        pin = create_pin(self.owner, image, [])
        image_id = image.pk
        pin_id = pin.pk
        files_before = self._assert_four_image_files(image)
        asset_directories = self._asset_directories(image)
        self.assertTrue(all(path.is_dir() for path in asset_directories))

        with self.assertRaisesRegex(RuntimeError, "rollback pin delete"):
            with transaction.atomic():
                pin.delete()
                raise RuntimeError("rollback pin delete")

        restored_pin = Pin.objects.get(pk=pin_id)
        restored_image = Image.objects.get(pk=image_id)
        self.assertEqual(restored_pin.image_id, restored_image.pk)
        self.assertEqual(restored_image.thumbnail_set.count(), 3)
        self.assertEqual(
            media_snapshot(self.temporary_media.name), files_before
        )
        self.assertTrue(all(path.is_dir() for path in asset_directories))

    def test_reference_created_before_commit_prevents_media_deletion(self):
        image = create_image()
        pin = create_pin(self.owner, image, [])
        image_id = image.pk
        files_before = self._assert_four_image_files(image)

        with transaction.atomic():
            pin.delete()
            self.assertTrue(Image.objects.filter(pk=image_id).exists())
            current_image = Image.objects.get(pk=image_id)
            replacement = create_pin(self.other_user, current_image, [])

        self.assertFalse(Pin.objects.filter(pk=pin.pk).exists())
        self.assertTrue(Pin.objects.filter(pk=replacement.pk).exists())
        self.assertTrue(Image.objects.filter(pk=image_id).exists())
        self.assertEqual(
            media_snapshot(self.temporary_media.name), files_before
        )

    @skipUnless(
        connection.vendor == "sqlite",
        "SQLite lock/write serialization observation",
    )
    def test_sqlite_serializes_reference_insert_after_empty_check(self):
        image = create_image()
        pin = create_pin(self.owner, image, [])
        image_id = image.pk
        pin_id = pin.pk
        reference_checked = threading.Event()
        writer_finished = threading.Event()
        writer_result = {}
        original_exists = QuerySet.exists

        def pause_after_empty_reference_check(queryset):
            result = original_exists(queryset)
            if queryset.model is Pin and not result:
                reference_checked.set()
                if not writer_finished.wait(2):
                    raise AssertionError("concurrent Pin writer did not finish")
            return result

        def create_reference_on_separate_connection():
            close_old_connections()
            writer_connection = connections["default"]
            try:
                if not reference_checked.wait(2):
                    raise AssertionError("reference check did not run")
                with writer_connection.cursor() as cursor:
                    cursor.execute("PRAGMA busy_timeout = 100")
                replacement = Pin.objects.create(
                    submitter_id=self.other_user.pk,
                    image_id=image_id,
                )
                writer_result["committed"] = True
                writer_result["pin_id"] = replacement.pk
            except Exception as error:
                writer_result["committed"] = False
                writer_result["error"] = error
            finally:
                writer_connection.close()
                writer_finished.set()

        writer = threading.Thread(
            target=create_reference_on_separate_connection
        )
        writer.start()
        self.addCleanup(writer.join, 2)

        with mock.patch.object(
            QuerySet,
            "exists",
            new=pause_after_empty_reference_check,
        ):
            Pin.objects.get(pk=pin_id).delete()
        writer.join(2)

        self.assertFalse(writer.is_alive())
        self.assertFalse(writer_result["committed"])
        self.assertIsInstance(writer_result["error"], OperationalError)
        self.assertFalse(Image.objects.filter(pk=image_id).exists())

    def test_cleanup_locks_image_before_recheck_and_instance_delete(self):
        image = create_image()
        pin = create_pin(self.owner, image, [])
        image_id = image.pk
        events = []
        original_select_for_update = BaseImage.objects.select_for_update
        original_pin_filter = Pin.objects.filter
        original_image_delete = BaseImage.delete

        def observe_lock(*args, **kwargs):
            events.append(("lock", connection.in_atomic_block))
            return original_select_for_update(*args, **kwargs)

        def observe_reference_check(*args, **kwargs):
            if kwargs == {"image_id": image_id}:
                events.append(("reference_check", connection.in_atomic_block))
            return original_pin_filter(*args, **kwargs)

        def observe_instance_delete(instance, *args, **kwargs):
            if instance.pk == image_id:
                events.append(("instance_delete", connection.in_atomic_block))
            return original_image_delete(instance, *args, **kwargs)

        with mock.patch.object(
            BaseImage.objects,
            "select_for_update",
            side_effect=observe_lock,
        ), mock.patch.object(
            Pin.objects,
            "filter",
            side_effect=observe_reference_check,
        ), mock.patch.object(
            BaseImage,
            "delete",
            new=observe_instance_delete,
        ):
            pin.delete()

        self.assertEqual(
            events,
            [
                ("lock", True),
                ("reference_check", True),
                ("instance_delete", True),
            ],
        )
        self.assertFalse(Image.objects.filter(pk=image_id).exists())

    def test_multiple_callbacks_treat_already_deleted_image_as_noop(self):
        image = create_image()
        first_pin = create_pin(self.owner, image, [])
        second_pin = create_pin(self.other_user, image, [])
        image_id = image.pk
        pin_ids = [first_pin.pk, second_pin.pk]
        self._assert_four_image_files(image)

        Pin.objects.filter(pk__in=pin_ids).delete()

        self.assertFalse(Pin.objects.filter(pk__in=pin_ids).exists())
        self.assertFalse(Image.objects.filter(pk=image_id).exists())
        self.assertFalse(
            Thumbnail.objects.filter(original_id=image_id).exists()
        )
        self.assertEqual(media_snapshot(self.temporary_media.name), {})
