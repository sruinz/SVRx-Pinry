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


class BoardCoverWriteAPITests(TemporaryMediaMixin, APITestCase):
    def setUp(self):
        super(BoardCoverWriteAPITests, self).setUp()
        self.owner = create_user("board-cover-write-owner")
        self.other_user = create_user("board-cover-write-other")
        self.board = Board.objects.create(
            name="board-cover-write-public",
            submitter=self.owner,
        )
        self.cover_url = "/api/v2/boards/{}/cover/".format(
            self.board.pk,
        )
        self.public_pin = self._create_pin(self.owner, private=False)
        self.private_pin = self._create_pin(self.owner, private=True)
        self.foreign_private = self._create_pin(
            self.other_user,
            private=True,
        )
        self.public_non_member = self._create_pin(
            self.owner,
            private=False,
        )

    def _create_pin(self, submitter, private):
        return Pin.objects.create(
            submitter=submitter,
            image=create_image(),
            private=private,
        )

    def test_owner_sets_member_and_resets_to_auto(self):
        self.client.force_authenticate(self.owner)
        self.board.pins.add(self.public_pin)

        response = self.client.patch(
            self.cover_url,
            {"pin_id": self.public_pin.pk},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["cover"]["id"], self.public_pin.pk)
        self.assertEqual(response.json()["cover_pin_id"], self.public_pin.pk)
        self.assertEqual(
            set(response.json()),
            {
                "resource_link",
                "id",
                "name",
                "private",
                "total_pins",
                "cover",
                "cover_pin_id",
                "published",
                "submitter",
            },
        )

        response = self.client.patch(
            self.cover_url,
            {"pin_id": None},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.json()["cover_pin_id"])
        self.board.refresh_from_db()
        self.assertIsNone(self.board.cover_pin_id)

    def test_non_member_and_hidden_pin_share_non_disclosing_error(self):
        self.client.force_authenticate(self.owner)
        self.board.pins.add(self.foreign_private)

        responses = (
            self.client.patch(
                self.cover_url,
                {"pin_id": self.public_non_member.pk},
                format="json",
            ),
            self.client.patch(
                self.cover_url,
                {"pin_id": self.foreign_private.pk},
                format="json",
            ),
            self.client.patch(
                self.cover_url,
                {"pin_id": 999999},
                format="json",
            ),
        )

        for response in responses:
            self.assertEqual(response.status_code, 400)
            self.assertEqual(
                response.json(),
                {"code": "board_cover_invalid"},
            )

    def test_request_body_requires_exact_nullable_positive_integer(self):
        self.client.force_authenticate(self.owner)
        self.board.pins.add(self.public_pin)
        invalid_payloads = (
            {},
            {"pin_id": self.public_pin.pk, "extra": True},
            {"pin_id": True},
            {"pin_id": 0},
            {"pin_id": -1},
            {"pin_id": str(self.public_pin.pk)},
            {"pin_id": float(self.public_pin.pk)},
        )

        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                response = self.client.patch(
                    self.cover_url,
                    payload,
                    format="json",
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(
                    response.json(),
                    {"code": "board_cover_invalid"},
                )

    def test_public_board_rejects_owner_private_member(self):
        self.client.force_authenticate(self.owner)
        self.board.pins.add(self.private_pin)

        response = self.client.patch(
            self.cover_url,
            {"pin_id": self.private_pin.pk},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json(),
            {"code": "board_cover_private_pin"},
        )

    def test_private_board_accepts_owner_private_member(self):
        self.board.private = True
        self.board.save(update_fields=("private",))
        self.board.pins.add(self.private_pin)
        self.client.force_authenticate(self.owner)

        response = self.client.patch(
            self.cover_url,
            {"pin_id": self.private_pin.pk},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["cover_pin_id"], self.private_pin.pk)

    def test_non_owner_gets_403_for_public_board_and_404_for_private_board(self):
        self.board.pins.add(self.public_pin)
        self.client.force_authenticate(self.other_user)

        response = self.client.patch(
            self.cover_url,
            {"pin_id": self.public_pin.pk},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        self.board.private = True
        self.board.save(update_fields=("private",))
        response = self.client.patch(
            self.cover_url,
            {"pin_id": self.public_pin.pk},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
