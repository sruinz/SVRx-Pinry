import hashlib

from django.db import connection
from django.test import TestCase
from django.urls import reverse
import mock
from rest_framework import status
from rest_framework.test import APITestCase

from core.models import Board
from core.services.board_ordering import BoardOrderService
from core.services.pin_membership import PinMembershipService
from core.tests.helpers import create_user
from users.models import User


class BoardOrderAPITests(APITestCase):
    def setUp(self):
        super(BoardOrderAPITests, self).setUp()
        self.owner = create_user("board-order-owner")
        self.other = create_user("board-order-other")
        self.first = Board.objects.create(
            submitter=self.owner,
            name="first",
            display_order=1,
        )
        self.second = Board.objects.create(
            submitter=self.owner,
            name="second",
            display_order=2,
        )
        self.third = Board.objects.create(
            submitter=self.owner,
            name="third",
            display_order=3,
        )
        self.foreign = Board.objects.create(
            submitter=self.other,
            name="foreign",
            display_order=1,
        )
        self.order_url = reverse("board-order")
        self.client.login(
            username=self.owner.username,
            password="password",
        )

    def _positions(self):
        return dict(
            Board.objects.filter(submitter=self.owner)
            .values_list("pk", "display_order")
        )

    def _put(self, version, board_ids, **extra):
        payload = {"version": version, "board_ids": board_ids}
        payload.update(extra)
        return self.client.put(self.order_url, payload, format="json")

    def test_get_returns_canonical_order_and_exact_version(self):
        response = self.client.get(self.order_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        board_ids = [self.first.pk, self.second.pk, self.third.pk]
        digest_input = (
            b"svrx-board-order-v1\0"
            + str(self.owner.pk).encode("ascii")
            + b"\0"
            + ",".join(str(board_id) for board_id in board_ids).encode(
                "ascii"
            )
        )
        self.assertEqual(
            response.json(),
            {
                "version": hashlib.sha256(digest_input).hexdigest(),
                "board_ids": board_ids,
            },
        )

    def test_anonymous_get_and_put_are_rejected(self):
        snapshot = self.client.get(self.order_url).json()
        self.client.logout()

        get_response = self.client.get(self.order_url)
        put_response = self._put(
            snapshot["version"],
            snapshot["board_ids"],
        )

        self.assertEqual(
            get_response.status_code,
            status.HTTP_401_UNAUTHORIZED,
        )
        self.assertEqual(
            put_response.status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

    def test_put_reorders_every_owned_board_atomically(self):
        snapshot = self.client.get(self.order_url).json()
        requested = list(reversed(snapshot["board_ids"]))

        response = self.client.put(
            self.order_url,
            {"version": snapshot["version"], "board_ids": requested},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["board_ids"], requested)
        self.assertEqual(
            list(
                Board.objects.filter(submitter=self.owner)
                .order_by("display_order", "-id")
                .values_list("id", flat=True)
            ),
            requested,
        )

    def test_put_rejects_bool_duplicate_and_extra_key_without_changes(self):
        snapshot = self.client.get(self.order_url).json()
        original_positions = self._positions()
        invalid_payloads = (
            {
                "version": snapshot["version"],
                "board_ids": [True, self.second.pk, self.third.pk],
            },
            {
                "version": snapshot["version"],
                "board_ids": [self.first.pk, self.first.pk, self.third.pk],
            },
            {
                "version": snapshot["version"],
                "board_ids": snapshot["board_ids"],
                "unexpected": True,
            },
        )

        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                response = self.client.put(
                    self.order_url,
                    payload,
                    format="json",
                )
                self.assertEqual(
                    response.status_code,
                    status.HTTP_400_BAD_REQUEST,
                )
                self.assertEqual(
                    response.json(),
                    {"code": "board_order_invalid"},
                )
                self.assertEqual(self._positions(), original_positions)

    def test_put_rejects_missing_fields_and_malformed_version(self):
        snapshot = self.client.get(self.order_url).json()
        original_positions = self._positions()
        invalid_payloads = (
            {"board_ids": snapshot["board_ids"]},
            {"version": snapshot["version"]},
            {
                "version": "0" * 63,
                "board_ids": snapshot["board_ids"],
            },
        )

        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                response = self.client.put(
                    self.order_url,
                    payload,
                    format="json",
                )
                self.assertEqual(
                    response.status_code,
                    status.HTTP_400_BAD_REQUEST,
                )
                self.assertEqual(
                    response.json(),
                    {"code": "board_order_invalid"},
                )
                self.assertEqual(self._positions(), original_positions)

    def test_put_rejects_missing_and_foreign_ids_without_changes(self):
        snapshot = self.client.get(self.order_url).json()
        original_positions = self._positions()
        changed_sets = (
            snapshot["board_ids"][:-1],
            snapshot["board_ids"][:-1] + [self.foreign.pk],
        )

        for board_ids in changed_sets:
            with self.subTest(board_ids=board_ids):
                response = self._put(snapshot["version"], board_ids)
                self.assertEqual(
                    response.status_code,
                    status.HTTP_409_CONFLICT,
                )
                self.assertEqual(
                    response.json(),
                    {"code": "board_order_changed"},
                )
                self.assertEqual(self._positions(), original_positions)

    def test_put_rejects_stale_version_without_partial_position_changes(self):
        snapshot = self.client.get(self.order_url).json()
        Board.objects.filter(pk=self.first.pk).update(display_order=3)
        Board.objects.filter(pk=self.second.pk).update(display_order=2)
        Board.objects.filter(pk=self.third.pk).update(display_order=1)
        positions_before_request = self._positions()

        response = self._put(
            snapshot["version"],
            [self.second.pk, self.first.pk, self.third.pk],
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(
            response.json(),
            {"code": "board_order_changed"},
        )
        self.assertEqual(self._positions(), positions_before_request)

    def test_put_accepts_stale_version_when_retry_already_matches_target(self):
        snapshot = self.client.get(self.order_url).json()
        requested = list(reversed(snapshot["board_ids"]))
        first_response = self._put(snapshot["version"], requested)

        retry_response = self._put(snapshot["version"], requested)

        self.assertEqual(first_response.status_code, status.HTTP_200_OK)
        self.assertEqual(retry_response.status_code, status.HTTP_200_OK)
        self.assertEqual(retry_response.json(), first_response.json())
        self.assertEqual(retry_response.json()["board_ids"], requested)


class BoardMutationLockingTests(TestCase):
    def setUp(self):
        super(BoardMutationLockingTests, self).setUp()
        self.owner = create_user("board-mutation-lock-owner")
        self.first = Board.objects.create(
            submitter=self.owner,
            name="lock-first",
            display_order=1,
        )
        self.second = Board.objects.create(
            submitter=self.owner,
            name="lock-second",
            display_order=2,
        )

    def _positions(self):
        return dict(
            Board.objects.filter(submitter=self.owner)
            .values_list("pk", "display_order")
        )

    def test_order_save_locks_user_before_boards(self):
        service = BoardOrderService()
        snapshot = service.snapshot(self.owner)
        events = []
        real_user_lock = User.objects.select_for_update
        real_board_lock = Board.objects.select_for_update

        def record_user_lock(*args, **kwargs):
            events.append(("user", connection.in_atomic_block))
            return real_user_lock(*args, **kwargs)

        def record_board_lock(*args, **kwargs):
            events.append(("board", connection.in_atomic_block))
            return real_board_lock(*args, **kwargs)

        with mock.patch.object(
            User.objects,
            "select_for_update",
            side_effect=record_user_lock,
        ), mock.patch.object(
            Board.objects,
            "select_for_update",
            side_effect=record_board_lock,
        ):
            service.save(
                self.owner,
                snapshot["version"],
                list(reversed(snapshot["board_ids"])),
            )

        self.assertEqual(events, [("user", True), ("board", True)])

    def test_order_save_rolls_back_if_bulk_update_fails_partway(self):
        service = BoardOrderService()
        snapshot = service.snapshot(self.owner)
        original_positions = self._positions()

        def partially_update_then_fail(boards, fields):
            boards[0].save(update_fields=fields)
            raise RuntimeError("forced bulk update failure")

        with mock.patch.object(
            Board.objects,
            "bulk_update",
            side_effect=partially_update_then_fail,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "forced bulk update failure",
            ):
                service.save(
                    self.owner,
                    snapshot["version"],
                    list(reversed(snapshot["board_ids"])),
                )

        self.assertEqual(self._positions(), original_positions)

    def test_delete_locks_user_before_board_and_member_pins(self):
        service = PinMembershipService()
        events = []
        real_user_lock = service._lock_user
        real_board_lock = service._lock_owned_boards
        real_pin_lock = service._lock_pins

        def record_user(*args, **kwargs):
            events.append("user")
            return real_user_lock(*args, **kwargs)

        def record_boards(*args, **kwargs):
            events.append("boards")
            return real_board_lock(*args, **kwargs)

        def record_pins(*args, **kwargs):
            events.append("pins")
            return real_pin_lock(*args, **kwargs)

        with mock.patch.object(
            service,
            "_lock_user",
            side_effect=record_user,
        ), mock.patch.object(
            service,
            "_lock_owned_boards",
            side_effect=record_boards,
        ), mock.patch.object(
            service,
            "_lock_pins",
            side_effect=record_pins,
        ):
            service.delete_board(self.owner, self.first.pk)

        self.assertEqual(events, ["user", "boards", "pins"])
