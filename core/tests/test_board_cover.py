import queue
import threading

from django.db import close_old_connections, connection, OperationalError
from django.urls import reverse
from django_images.test_helpers import TemporaryMediaMixin
import mock
from rest_framework import status
from rest_framework.test import APIClient, APITestCase, APITransactionTestCase

from core.models import Board, Pin
from core.services.board_cover import BoardCoverError, BoardCoverService
from core.services.bulk_pin_management import normalize_bulk_exception
from core.tests.helpers import create_image, create_user
from core.tests.test_pin_import_atomicity import _SQLiteConcurrencyHarness


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


class BoardCoverPrivacyTransitionTests(TemporaryMediaMixin, APITestCase):
    def setUp(self):
        super(BoardCoverPrivacyTransitionTests, self).setUp()
        self.owner = create_user("board-cover-privacy-owner")
        self.board = Board.objects.create(
            name="board-cover-privacy",
            submitter=self.owner,
        )
        self.board_url = reverse(
            "board-detail",
            kwargs={"pk": self.board.pk},
        )
        self.private_pin = self._create_pin(private=True)
        self.public_pin = self._create_pin(private=False)
        self.pin_url = reverse(
            "pin-detail",
            kwargs={"pk": self.public_pin.pk},
        )
        self.client.force_authenticate(self.owner)

    def _create_pin(self, private):
        return Pin.objects.create(
            submitter=self.owner,
            image=create_image(),
            private=private,
        )

    def test_private_board_becoming_public_clears_private_manual_cover(self):
        self.board.private = True
        self.board.cover_pin = self.private_pin
        self.board.pins.add(self.private_pin, self.public_pin)
        self.board.save(update_fields=("private", "cover_pin"))

        response = self.client.patch(
            self.board_url,
            {"private": False},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.json()["cover_pin_id"])
        self.assertEqual(response.json()["cover"]["id"], self.public_pin.pk)
        self.board.refresh_from_db()
        self.private_pin.refresh_from_db()
        self.assertIsNone(self.board.cover_pin_id)
        self.assertFalse(self.board.private)
        self.assertTrue(self.private_pin.private)

    def test_private_board_becoming_public_keeps_public_manual_cover(self):
        self.board.private = True
        self.board.cover_pin = self.public_pin
        self.board.pins.add(self.public_pin)
        self.board.save(update_fields=("private", "cover_pin"))

        response = self.client.patch(
            self.board_url,
            {"private": False},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["cover_pin_id"], self.public_pin.pk)
        self.public_pin.refresh_from_db()
        self.assertFalse(self.public_pin.private)

    def test_public_cover_becoming_private_clears_manual_cover(self):
        self.board.pins.add(self.public_pin)
        self.board.cover_pin = self.public_pin
        self.board.save(update_fields=("cover_pin",))

        response = self.client.patch(
            self.pin_url,
            {"private": True},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.board.refresh_from_db()
        self.public_pin.refresh_from_db()
        self.assertIsNone(self.board.cover_pin_id)
        self.assertTrue(self.public_pin.private)
        self.assertFalse(self.board.private)

    def test_private_board_keeps_cover_when_cover_becomes_private(self):
        self.board.private = True
        self.board.cover_pin = self.public_pin
        self.board.pins.add(self.public_pin)
        self.board.save(update_fields=("private", "cover_pin"))

        response = self.client.patch(
            self.pin_url,
            {"private": True},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.board.refresh_from_db()
        self.assertEqual(self.board.cover_pin_id, self.public_pin.pk)

    def test_public_pin_transition_does_not_restore_cleared_manual_cover(self):
        self.board.pins.add(self.public_pin)
        self.board.cover_pin = self.public_pin
        self.board.save(update_fields=("cover_pin",))

        private_response = self.client.patch(
            self.pin_url,
            {"private": True},
            format="json",
        )
        public_response = self.client.patch(
            self.pin_url,
            {"private": False},
            format="json",
        )

        self.assertEqual(private_response.status_code, status.HTTP_200_OK)
        self.assertEqual(public_response.status_code, status.HTTP_200_OK)
        self.board.refresh_from_db()
        self.assertIsNone(self.board.cover_pin_id)

    def test_pin_privacy_patch_preserves_scalar_and_tag_update_contract(self):
        self.public_pin.tags.set("old")

        response = self.client.patch(
            self.pin_url,
            {
                "private": True,
                "description": "changed",
                "tags": ["new"],
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.public_pin.refresh_from_db()
        self.assertTrue(self.public_pin.private)
        self.assertEqual(self.public_pin.description, "changed")
        self.assertEqual(set(self.public_pin.tags.names()), {"new"})

    def test_pin_cover_change_conflict_is_exact_code_only_409(self):
        with mock.patch.object(
            BoardCoverService,
            "pin_privacy_transition",
            side_effect=BoardCoverError("board_cover_changed"),
        ):
            response = self.client.patch(
                self.pin_url,
                {"private": True},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.json(), {"code": "board_cover_changed"})


class BoardCoverConcurrencyTests(
    TemporaryMediaMixin,
    APITransactionTestCase,
):
    def setUp(self):
        super(BoardCoverConcurrencyTests, self).setUp()
        self.owner = create_user("board-cover-concurrency-owner")
        self.board = Board.objects.create(
            name="board-cover-concurrency",
            submitter=self.owner,
        )
        self.pin = Pin.objects.create(
            submitter=self.owner,
            image=create_image(),
        )
        self.board.pins.add(self.pin)
        self.board_url = reverse(
            "board-detail",
            kwargs={"pk": self.board.pk},
        )
        self.cover_url = "/api/v2/boards/{}/cover/".format(
            self.board.pk,
        )
        self.pin_url = reverse(
            "pin-detail",
            kwargs={"pk": self.pin.pk},
        )

    def _run_two_workers(self, requests):
        if connection.vendor != "sqlite":
            self.skipTest("This concurrency contract requires SQLite.")
        if connection.creation.is_in_memory_db(
            connection.settings_dict["NAME"]
        ):
            self.skipTest("This concurrency contract requires file SQLite.")
        harness = _SQLiteConcurrencyHarness(self)
        start = harness.barrier(2)
        outcomes = queue.Queue()

        def worker(name, request):
            close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(user=self.owner)
                start.wait(timeout=5)
                try:
                    response = request(client)
                except OperationalError as error:
                    code, retryable = normalize_bulk_exception(error)
                    if code != "database_busy" or not retryable:
                        raise
                    outcomes.put((name, status.HTTP_503_SERVICE_UNAVAILABLE))
                else:
                    outcomes.put((name, response.status_code))
            except BaseException as error:
                outcomes.put(("error", name, error))
            finally:
                close_old_connections()

        workers = [
            threading.Thread(
                target=worker,
                args=(name, request),
                name="{}-worker".format(name),
            )
            for name, request in requests
        ]
        harness.start(workers)
        collected = harness.join_and_collect(outcomes, len(workers))
        errors = [item for item in collected if item[0] == "error"]
        self.assertEqual(errors, [])
        allowed = {
            status.HTTP_200_OK,
            status.HTTP_400_BAD_REQUEST,
            status.HTTP_409_CONFLICT,
            status.HTTP_503_SERVICE_UNAVAILABLE,
        }
        self.assertTrue(all(item[1] in allowed for item in collected))

    def assert_public_cover_invariant(self):
        self.board.refresh_from_db()
        if self.board.cover_pin_id is None:
            return
        cover = Pin.objects.get(pk=self.board.cover_pin_id)
        self.assertFalse(self.board.private)
        self.assertFalse(cover.private)
        self.assertTrue(self.board.pins.filter(pk=cover.pk).exists())

    def test_set_cover_racing_pin_private_never_commits_private_public_cover(self):
        self._run_two_workers((
            (
                "cover",
                lambda client: client.patch(
                    self.cover_url,
                    {"pin_id": self.pin.pk},
                    format="json",
                ),
            ),
            (
                "privacy",
                lambda client: client.patch(
                    self.pin_url,
                    {"private": True},
                    format="json",
                ),
            ),
        ))

        self.assert_public_cover_invariant()

    def test_set_cover_racing_membership_remove_never_commits_non_member_cover(self):
        self._run_two_workers((
            (
                "cover",
                lambda client: client.patch(
                    self.cover_url,
                    {"pin_id": self.pin.pk},
                    format="json",
                ),
            ),
            (
                "membership",
                lambda client: client.patch(
                    self.board_url,
                    {"pins_to_remove": [self.pin.pk]},
                    format="json",
                ),
            ),
        ))

        self.board.refresh_from_db()
        if self.board.cover_pin_id is not None:
            self.assertTrue(
                self.board.pins.filter(pk=self.board.cover_pin_id).exists()
            )
