from django.urls import reverse
from django_images.test_helpers import TemporaryMediaMixin
from rest_framework import status
from rest_framework.test import APITestCase

from core.models import Board, Pin
from core.tests.helpers import create_image, create_user


class BoardCoverReadTests(TemporaryMediaMixin, APITestCase):
    def setUp(self):
        super(BoardCoverReadTests, self).setUp()
        self.owner = create_user("board-cover-owner")
        self.board = Board.objects.create(
            name="board-cover-public",
            submitter=self.owner,
        )
        self.board_url = reverse(
            "board-detail",
            kwargs={"pk": self.board.pk},
        )
        self.older_public = self._create_pin(private=False)
        self.newer_public = self._create_pin(private=False)
        self.private_pin = self._create_pin(private=True)
        self.public_pin = self._create_pin(private=False)

    def _create_pin(self, private):
        return Pin.objects.create(
            submitter=self.owner,
            image=create_image(),
            private=private,
        )

    def test_manual_cover_preserves_cover_payload_and_exposes_manual_id(self):
        self.board.pins.add(self.older_public, self.newer_public)
        self.board.cover_pin = self.newer_public
        self.board.save(update_fields=("cover_pin",))

        response = self.client.get(self.board_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["cover"]["id"], self.newer_public.pk)
        self.assertEqual(
            response.json()["cover_pin_id"],
            self.newer_public.pk,
        )

    def test_public_board_auto_cover_never_leaks_owner_private_pin(self):
        self.board.pins.add(self.private_pin, self.public_pin)
        self.client.force_authenticate(self.owner)

        response = self.client.get(self.board_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["cover"]["id"], self.public_pin.pk)
        self.assertIsNone(response.json()["cover_pin_id"])
        self.assertNotIn(
            self.private_pin.image.image.url,
            str(response.json()),
        )

    def test_empty_board_keeps_null_cover_contract(self):
        response = self.client.get(self.board_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.json()["cover"])
        self.assertIsNone(response.json()["cover_pin_id"])

    def test_private_board_owner_can_use_private_pin_as_auto_cover(self):
        self.board.private = True
        self.board.save(update_fields=("private",))
        self.board.pins.add(self.private_pin, self.public_pin)
        self.client.force_authenticate(self.owner)

        response = self.client.get(self.board_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["cover"]["id"], self.private_pin.pk)
        self.assertIsNone(response.json()["cover_pin_id"])

    def test_private_board_owner_can_use_private_pin_as_manual_cover(self):
        self.board.private = True
        self.board.cover_pin = self.private_pin
        self.board.save(update_fields=("private", "cover_pin"))
        self.board.pins.add(self.private_pin, self.public_pin)
        self.client.force_authenticate(self.owner)

        response = self.client.get(self.board_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["cover"]["id"], self.private_pin.pk)
        self.assertEqual(response.json()["cover_pin_id"], self.private_pin.pk)

    def test_public_board_ignores_injected_private_manual_cover(self):
        self.board.pins.add(self.private_pin, self.public_pin)
        self.board.cover_pin = self.private_pin
        self.board.save(update_fields=("cover_pin",))
        self.client.force_authenticate(self.owner)

        response = self.client.get(self.board_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["cover"]["id"], self.public_pin.pk)
        self.assertIsNone(response.json()["cover_pin_id"])
        self.assertNotIn(
            self.private_pin.image.image.url,
            str(response.json()),
        )
