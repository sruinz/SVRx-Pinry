from pathlib import Path

from django.contrib import admin
from django.db import transaction
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITransactionTestCase

from core.admin import PinAdmin
from core.models import Board, Image, Pin
from core.tests.helpers import TEST_IMAGE_PATH, create_image, create_pin, create_user
from django_images.models import Thumbnail
from django_images.test_helpers import TemporaryMediaMixin


def media_snapshot(media_root):
    root = Path(media_root)
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


class PinTrashAPITest(TemporaryMediaMixin, APITransactionTestCase):
    def setUp(self):
        super(PinTrashAPITest, self).setUp()
        self.owner = create_user("trash-owner")
        self.other_user = create_user("trash-other")
        self.image = create_image()
        self.pin = create_pin(self.owner, self.image, [])
        self.client.login(username=self.owner.username, password="password")

    @staticmethod
    def _detail_action_url(pin, action):
        return "{}{}/".format(reverse("pin-detail", args=[pin.pk]), action)

    @staticmethod
    def _trash_url():
        return "{}trash/".format(reverse("pin-list"))

    def _active_pin_ids(self, query=""):
        response = self.client.get("{}{}".format(reverse("pin-list"), query))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return [item["id"] for item in response.json()["results"]]

    def test_delete_moves_pin_to_trash_without_deleting_media(self):
        files_before = media_snapshot(self.temporary_media.name)
        self.assertEqual(len(files_before), 4)

        response = self.client.delete(reverse("pin-detail", args=[self.pin.pk]))

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertTrue(Pin.objects.filter(pk=self.pin.pk).exists())
        self.pin.refresh_from_db()
        self.assertIsNotNone(self.pin.trashed_at)
        self.assertNotIn(self.pin.pk, self._active_pin_ids())
        detail = self.client.get(reverse("pin-detail", args=[self.pin.pk]))
        self.assertEqual(detail.status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(Image.objects.filter(pk=self.image.pk).exists())
        self.assertEqual(
            media_snapshot(self.temporary_media.name), files_before
        )

    def test_repeated_delete_preserves_original_trash_timestamp(self):
        detail_url = reverse("pin-detail", args=[self.pin.pk])
        self.assertEqual(
            self.client.delete(detail_url).status_code,
            status.HTTP_204_NO_CONTENT,
        )
        self.assertTrue(Pin.objects.filter(pk=self.pin.pk).exists())
        self.pin.refresh_from_db()
        first_trashed_at = self.pin.trashed_at

        response = self.client.delete(detail_url)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.trashed_at, first_trashed_at)

    def test_restore_is_idempotent_and_returns_pin_to_active_list(self):
        detail_url = reverse("pin-detail", args=[self.pin.pk])
        self.assertEqual(
            self.client.delete(detail_url).status_code,
            status.HTTP_204_NO_CONTENT,
        )
        restore_url = self._detail_action_url(self.pin, "restore")

        first = self.client.post(restore_url)
        second = self.client.post(restore_url)

        self.assertEqual(first.status_code, status.HTTP_200_OK)
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        self.assertEqual(first.json()["id"], self.pin.pk)
        self.assertEqual(second.json()["id"], self.pin.pk)
        self.pin.refresh_from_db()
        self.assertIsNone(self.pin.trashed_at)
        self.assertIn(self.pin.pk, self._active_pin_ids())

    def test_trash_list_is_paginated_and_contains_only_owner_pins(self):
        second_image = create_image()
        second_pin = create_pin(self.owner, second_image, [])
        other_image = create_image()
        other_pin = create_pin(self.other_user, other_image, [])

        for pin in (self.pin, second_pin):
            response = self.client.delete(reverse("pin-detail", args=[pin.pk]))
            self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.client.login(
            username=self.other_user.username, password="password"
        )
        response = self.client.delete(reverse("pin-detail", args=[other_pin.pk]))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.client.login(username=self.owner.username, password="password")

        first_page = self.client.get(
            "{}?limit=1&offset=0&ordering=-id".format(self._trash_url())
        )
        second_page = self.client.get(
            "{}?limit=1&offset=1&ordering=-id".format(self._trash_url())
        )

        self.assertEqual(first_page.status_code, status.HTTP_200_OK)
        self.assertEqual(second_page.status_code, status.HTTP_200_OK)
        self.assertEqual(first_page.json()["count"], 2)
        self.assertEqual(second_page.json()["count"], 2)
        self.assertEqual(
            [item["id"] for item in first_page.json()["results"]],
            [second_pin.pk],
        )
        self.assertEqual(
            [item["id"] for item in second_page.json()["results"]],
            [self.pin.pk],
        )
        self.assertNotIn(
            other_pin.pk,
            {
                item["id"]
                for item in first_page.json()["results"]
                + second_page.json()["results"]
            },
        )

    def test_active_list_search_and_board_summary_exclude_trashed_pin(self):
        self.pin.description = "trash-only-search-value"
        self.pin.save(update_fields=["description"])
        active_image = create_image()
        active_pin = create_pin(self.owner, active_image, [])
        active_pin.description = "active-value"
        active_pin.save(update_fields=["description"])
        board = Board.objects.create(name="trash-board", submitter=self.owner)
        board.pins.add(self.pin, active_pin)
        response = self.client.delete(
            reverse("pin-detail", args=[self.pin.pk])
        )
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertTrue(Pin.objects.filter(pk=self.pin.pk).exists())

        active_ids = self._active_pin_ids()
        search_ids = self._active_pin_ids("?search=trash-only-search-value")
        board_response = self.client.get(
            reverse("board-detail", args=[board.pk])
        )

        self.assertEqual(active_ids, [active_pin.pk])
        self.assertNotIn(self.pin.pk, search_ids)
        self.assertEqual(board_response.status_code, status.HTTP_200_OK)
        self.assertEqual(board_response.json()["total_pins"], 1)
        self.assertEqual(board_response.json()["cover"]["id"], active_pin.pk)

    def test_anonymous_custom_trash_actions_return_401(self):
        self.client.logout()

        responses = (
            self.client.get(self._trash_url()),
            self.client.post(self._detail_action_url(self.pin, "restore")),
            self.client.delete(self._detail_action_url(self.pin, "permanent")),
        )

        self.assertEqual(
            [response.status_code for response in responses],
            [
                status.HTTP_401_UNAUTHORIZED,
                status.HTTP_401_UNAUTHORIZED,
                status.HTTP_401_UNAUTHORIZED,
            ],
        )

    def test_other_user_and_missing_pin_actions_return_404(self):
        self.client.login(username=self.other_user.username, password="password")
        missing_id = self.pin.pk + 100000

        responses = (
            self.client.delete(reverse("pin-detail", args=[self.pin.pk])),
            self.client.post(self._detail_action_url(self.pin, "restore")),
            self.client.delete(self._detail_action_url(self.pin, "permanent")),
            self.client.post(
                "{}{}/restore/".format(reverse("pin-list"), missing_id)
            ),
            self.client.delete(
                "{}{}/permanent/".format(reverse("pin-list"), missing_id)
            ),
        )

        self.assertEqual(
            [response.status_code for response in responses],
            [status.HTTP_404_NOT_FOUND] * 5,
        )
        self.assertTrue(Pin.objects.filter(pk=self.pin.pk).exists())


class PinMediaLifecycleTest(TemporaryMediaMixin, APITransactionTestCase):
    def setUp(self):
        super(PinMediaLifecycleTest, self).setUp()
        self.owner = create_user("media-owner")
        self.other_user = create_user("media-other")
        self.client.login(username=self.owner.username, password="password")

    @staticmethod
    def _permanent_url(pin):
        return "{}permanent/".format(reverse("pin-detail", args=[pin.pk]))

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

    def _move_to_trash(self, pin):
        response = self.client.delete(reverse("pin-detail", args=[pin.pk]))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertTrue(Pin.objects.filter(pk=pin.pk).exists())

    def test_permanent_delete_preserves_shared_image_rows_and_file_bytes(self):
        image = create_image()
        owner_pin = create_pin(self.owner, image, [])
        other_pin = create_pin(self.other_user, image, [])
        files_before = self._assert_four_image_files(image)
        self.client.login(
            username=self.other_user.username, password="password"
        )
        self._move_to_trash(other_pin)
        self.client.login(username=self.owner.username, password="password")
        self._move_to_trash(owner_pin)

        response = self.client.delete(self._permanent_url(owner_pin))

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Pin.objects.filter(pk=owner_pin.pk).exists())
        self.assertTrue(Pin.objects.filter(pk=other_pin.pk).exists())
        self.assertTrue(Image.objects.filter(pk=image.pk).exists())
        self.assertEqual(
            Thumbnail.objects.filter(original_id=image.pk).count(), 3
        )
        self.assertEqual(
            media_snapshot(self.temporary_media.name), files_before
        )

    def test_permanent_delete_of_last_reference_removes_rows_and_files(self):
        image = create_image()
        pin = create_pin(self.owner, image, [])
        self._assert_four_image_files(image)
        self._move_to_trash(pin)

        response = self.client.delete(self._permanent_url(pin))

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Pin.objects.filter(pk=pin.pk).exists())
        self.assertFalse(Image.objects.filter(pk=image.pk).exists())
        self.assertFalse(
            Thumbnail.objects.filter(original_id=image.pk).exists()
        )
        self.assertEqual(media_snapshot(self.temporary_media.name), {})

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

    def test_rolled_back_pin_delete_keeps_database_rows_and_media(self):
        image = create_image()
        pin = create_pin(self.owner, image, [])
        image_id = image.pk
        pin_id = pin.pk
        files_before = self._assert_four_image_files(image)

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
