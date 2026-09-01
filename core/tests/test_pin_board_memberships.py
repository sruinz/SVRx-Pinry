from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import resolve
from django_images.test_helpers import TemporaryMediaMixin
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.test import APITestCase, APIRequestFactory

from core.models import Board, Pin
from core.permissions import IsOwnerOrReadOnly, OwnerOnlyIfPrivate
from core.tests.helpers import create_image, create_user


class PinBoardMembershipAPITests(TemporaryMediaMixin, APITestCase):
    def setUp(self):
        super(PinBoardMembershipAPITests, self).setUp()
        self.viewer = create_user("board-membership-viewer")
        self.pin_owner = create_user("board-membership-pin-owner")
        self.other_owner = create_user("board-membership-other-owner")
        self.image = create_image()
        self.public_foreign_pin = Pin.objects.create(
            submitter=self.pin_owner,
            image=self.image,
        )
        self.private_foreign_pin = Pin.objects.create(
            submitter=self.pin_owner,
            image=self.image,
            private=True,
        )
        self.old_included_board = Board.objects.create(
            submitter=self.viewer,
            name="old included",
            private=True,
        )
        self.middle_missing_board = Board.objects.create(
            submitter=self.viewer,
            name="middle missing",
        )
        self.new_included_board = Board.objects.create(
            submitter=self.viewer,
            name="new included",
        )
        self.other_board = Board.objects.create(
            submitter=self.other_owner,
            name="foreign board",
        )
        self.old_included_board.pins.add(self.public_foreign_pin)
        self.new_included_board.pins.add(self.public_foreign_pin)
        self.other_board.pins.add(self.public_foreign_pin)
        self.client.force_authenticate(user=self.viewer)

    @staticmethod
    def _url(pin_id):
        return "/api/v2/pins/{}/board-memberships/".format(pin_id)

    def test_returns_only_viewers_boards_with_membership_in_descending_id_order(self):
        response = self.client.get(self._url(self.public_foreign_pin.pk))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data,
            {
                "boards": [
                    {
                        "id": self.new_included_board.pk,
                        "name": "new included",
                        "contains_pin": True,
                    },
                    {
                        "id": self.middle_missing_board.pk,
                        "name": "middle missing",
                        "contains_pin": False,
                    },
                    {
                        "id": self.old_included_board.pk,
                        "name": "old included",
                        "contains_pin": True,
                    },
                ]
            },
        )

    def test_private_owned_pin_returns_the_owners_board_memberships(self):
        private_owned_pin = Pin.objects.create(
            submitter=self.viewer,
            image=self.image,
            private=True,
        )
        self.old_included_board.pins.add(private_owned_pin)

        response = self.client.get(self._url(private_owned_pin.pk))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data,
            {
                "boards": [
                    {
                        "id": self.new_included_board.pk,
                        "name": "new included",
                        "contains_pin": False,
                    },
                    {
                        "id": self.middle_missing_board.pk,
                        "name": "middle missing",
                        "contains_pin": False,
                    },
                    {
                        "id": self.old_included_board.pk,
                        "name": "old included",
                        "contains_pin": True,
                    },
                ]
            },
        )

    def test_action_keeps_authentication_and_submitter_object_permissions(self):
        callback = resolve(self._url(self.public_foreign_pin.pk)).func
        view = callback.cls(**callback.initkwargs)
        permissions = view.get_permissions()

        self.assertEqual(
            [type(permission) for permission in permissions],
            [IsAuthenticated, IsOwnerOrReadOnly, OwnerOnlyIfPrivate],
        )
        unsafe_request = APIRequestFactory().post("/")
        unsafe_request.user = self.viewer
        for permission in permissions[1:]:
            with self.subTest(permission=type(permission).__name__):
                self.assertFalse(
                    permission.has_object_permission(
                        unsafe_request,
                        view,
                        self.private_foreign_pin,
                    )
                )

    def test_anonymous_request_is_rejected(self):
        self.client.force_authenticate(user=None)

        response = self.client.get(self._url(self.public_foreign_pin.pk))

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(response["Cache-Control"], "private, no-store")

    def test_other_users_submitter_filter_does_not_find_target(self):
        response = self.client.get(
            self._url(self.public_foreign_pin.pk),
            {"submitter__username": self.other_owner.username},
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_private_foreign_pin_is_not_found(self):
        response = self.client.get(self._url(self.private_foreign_pin.pk))

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response["Cache-Control"], "private, no-store")

    def test_missing_pin_is_not_found(self):
        missing_pin_id = self.private_foreign_pin.pk + 1000
        self.assertFalse(Pin.objects.filter(pk=missing_pin_id).exists())

        response = self.client.get(self._url(missing_pin_id))

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response["Cache-Control"], "private, no-store")

    def test_success_response_is_private_and_not_cacheable(self):
        response = self.client.get(self._url(self.public_foreign_pin.pk))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Cache-Control"], "private, no-store")

    def test_board_count_does_not_increase_the_two_query_contract(self):
        for index in range(25):
            board = Board.objects.create(
                submitter=self.viewer,
                name="extra board {:02d}".format(index),
            )
            if index % 2 == 0:
                board.pins.add(self.public_foreign_pin)

        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(
                self._url(self.public_foreign_pin.pk)
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["boards"]), 28)
        self.assertEqual(
            len(captured.captured_queries),
            2,
            [query["sql"] for query in captured.captured_queries],
        )
