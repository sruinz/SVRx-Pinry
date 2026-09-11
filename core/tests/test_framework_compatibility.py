from types import SimpleNamespace

from django.contrib.auth.models import AnonymousUser
from django.db import models
from django.urls import resolve, reverse
from django_images.test_helpers import TemporaryMediaMixin
from rest_framework.test import APITestCase

from core.models import Board, Pin
from core.permissions import OwnerOnly
from core.tests.helpers import create_image, create_user
from users.models import AdminBootstrapState, User
from users.views import login_user, logout_user


class PermissionCompatibilityTests(APITestCase):
    def test_owner_only_uses_the_authentication_boolean_property(self):
        permission = OwnerOnly()

        self.assertIs(
            permission.has_permission(
                SimpleNamespace(user=User(username="authenticated")),
                None,
            ),
            True,
        )
        self.assertIs(
            permission.has_permission(
                SimpleNamespace(user=AnonymousUser()),
                None,
            ),
            False,
        )


class UserURLCompatibilityTests(APITestCase):
    def test_login_and_logout_routes_keep_their_callbacks_and_names(self):
        cases = (
            ("/api/v2/profile/login/", "login", login_user),
            ("/api/v2/profile/logout/", "logout", logout_user),
        )

        for path, url_name, callback in cases:
            with self.subTest(path=path):
                match = resolve(path)
                self.assertEqual(match.namespace, "users")
                self.assertEqual(match.url_name, url_name)
                self.assertEqual(match.func, callback)

    def test_included_user_router_still_resolves(self):
        match = resolve("/api/v2/profile/public-users/")

        self.assertEqual(match.namespace, "users")
        self.assertEqual(match.url_name, "public-user-list")


class FilterCompatibilityTests(TemporaryMediaMixin, APITestCase):
    def setUp(self):
        super(FilterCompatibilityTests, self).setUp()
        self.owner = create_user("framework-filter-owner")
        self.other = create_user("framework-filter-other")
        image = create_image()
        self.pin = Pin.objects.create(submitter=self.owner, image=image)
        self.other_pin = Pin.objects.create(submitter=self.other, image=image)
        self.board = Board.objects.create(
            submitter=self.owner,
            name="framework-filter-owner",
        )
        self.other_board = Board.objects.create(
            submitter=self.other,
            name="framework-filter-other",
        )
        self.board.pins.add(self.pin)
        self.other_board.pins.add(self.other_pin)

    def test_pin_filters_keep_submitter_and_board_fields(self):
        by_submitter = self.client.get(
            reverse("pin-list"),
            {"submitter__username": self.owner.username},
        )
        by_board = self.client.get(
            reverse("pin-list"),
            {"pins__id": self.board.pk},
        )

        self.assertEqual(by_submitter.status_code, 200)
        self.assertEqual(
            [row["id"] for row in by_submitter.data["results"]],
            [self.pin.pk],
        )
        self.assertEqual(by_board.status_code, 200)
        self.assertEqual(
            [row["id"] for row in by_board.data["results"]],
            [self.pin.pk],
        )

    def test_board_filters_keep_submitter_field(self):
        board_response = self.client.get(
            "/api/v2/boards/",
            {"submitter__username": self.owner.username},
        )
        autocomplete_response = self.client.get(
            "/api/v2/boards-auto-complete/",
            {"submitter__username": self.owner.username},
        )

        self.assertEqual(board_response.status_code, 200)
        self.assertEqual(
            [row["id"] for row in board_response.data["results"]],
            [self.board.pk],
        )
        self.assertEqual(autocomplete_response.status_code, 200)
        self.assertEqual(
            [row["id"] for row in autocomplete_response.data],
            [self.board.pk],
        )

    def test_public_user_filter_keeps_username_field(self):
        response = self.client.get(
            reverse("users:public-user-list"),
            {"username": self.owner.username},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [row["username"] for row in response.data],
            [self.owner.username],
        )


class PrimaryKeyCompatibilityTests(APITestCase):
    def test_implicit_primary_keys_remain_auto_fields(self):
        for model in (Board, Pin, AdminBootstrapState):
            with self.subTest(model=model.__name__):
                self.assertIs(type(model._meta.pk), models.AutoField)
