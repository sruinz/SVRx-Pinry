from io import StringIO
from pathlib import Path
import threading
import uuid
from unittest import skipUnless

import mock
from django.apps import apps
from django.contrib import admin
from django.core.management import call_command
from django.db import close_old_connections, connection, connections
from django.db import OperationalError, transaction
from django.db.models.query import QuerySet
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITransactionTestCase

from core.admin import PinAdmin
from core.models import BatchImportItem, Image, Pin
from core.services.idempotency import IdempotencyStore, StoredError
from core.tests.helpers import TEST_IMAGE_PATH, create_image, create_pin, create_user
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


class PinMediaLifecycleTest(TemporaryMediaMixin, APITransactionTestCase):
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
