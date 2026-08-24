from datetime import timedelta

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from core.models import Board
from core.tests.helpers import create_user


class BoardSortAPITests(APITestCase):
    def setUp(self):
        super(BoardSortAPITests, self).setUp()
        self.owner = create_user("board-sort-owner")
        self.url = "/api/v2/boards/"
        self.first = Board.objects.create(
            submitter=self.owner,
            name="first board",
        )
        self.second = Board.objects.create(
            submitter=self.owner,
            name="second board",
        )

    def ids(self, **params):
        params.setdefault("submitter__username", self.owner.username)
        response = self.client.get(self.url, params)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return [item["id"] for item in response.json()["results"]]

    def test_custom_sort_places_unpositioned_new_board_first(self):
        self.first.display_order = 1
        self.first.save(update_fields=["display_order"])
        self.second.display_order = 2
        self.second.save(update_fields=["display_order"])
        newest = Board.objects.create(submitter=self.owner, name="newest")

        self.assertEqual(self.ids(sort="custom"), [newest.pk, self.first.pk, self.second.pk])

    def test_search_without_sort_keeps_legacy_id_desc(self):
        response = self.client.get(self.url, {"search": "board"})
        self.assertEqual([item["id"] for item in response.json()["results"]], [self.second.pk, self.first.pk])

    def test_duplicate_username_without_sort_keeps_legacy_filtering(self):
        response = self.client.get(
            "{}?submitter__username={}&submitter__username={}".format(
                self.url,
                self.owner.username,
                self.owner.username,
            )
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [item["id"] for item in response.json()["results"]],
            [self.second.pk, self.first.pk],
        )

    def test_date_modes_use_id_as_the_tie_breaker(self):
        published = timezone.now() - timedelta(days=1)
        Board.objects.filter(pk__in=(self.first.pk, self.second.pk)).update(
            published=published
        )

        self.assertEqual(
            self.ids(sort="latest"),
            [self.second.pk, self.first.pk],
        )
        self.assertEqual(
            self.ids(sort="oldest"),
            [self.first.pk, self.second.pk],
        )

    def test_random_sort_uses_the_pin_integer_contract(self):
        Board.objects.filter(submitter=self.owner).delete()
        for board_id in (1001, 1002, 1003):
            Board.objects.create(
                pk=board_id,
                submitter=self.owner,
                name="random-{}".format(board_id),
            )

        self.assertEqual(
            self.ids(sort="random", random_seed="2147483646"),
            [1001, 1002, 1003],
        )

    def test_invalid_sort_contract_is_code_only(self):
        username = self.owner.username
        invalid_queries = (
            "sort=unexpected&submitter__username={}".format(username),
            "sort=random&submitter__username={}".format(username),
            "sort=random&random_seed=-1&submitter__username={}".format(username),
            "sort=latest&random_seed=1&submitter__username={}".format(username),
            "random_seed=1&submitter__username={}".format(username),
            "sort=random&random_seed=01&submitter__username={}".format(username),
            "sort=latest&sort=oldest&submitter__username={}".format(username),
            "sort=random&random_seed=1&random_seed=2&submitter__username={}".format(username),
            "sort=random&random_seed=2147483647&submitter__username={}".format(username),
            "sort=latest&ordering=-id&submitter__username={}".format(username),
            "sort=latest",
        )
        for query in invalid_queries:
            response = self.client.get("{}?{}".format(self.url, query))
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertEqual(response.json(), {"code": "board_sort_invalid"})

    def test_board_serializer_does_not_expose_display_order(self):
        response = self.client.get(
            self.url,
            {"submitter__username": self.owner.username},
        )

        self.assertNotIn("display_order", response.json()["results"][0])


class BoardDisplayOrderMigrationTests(TransactionTestCase):
    migrate_from = [("core", "0015_board_cover_pin")]
    migrate_to = [("core", "0016_board_display_order")]

    def setUp(self):
        super(BoardDisplayOrderMigrationTests, self).setUp()
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.migrate_from)
        old_apps = self.executor.loader.project_state(self.migrate_from).apps
        User = old_apps.get_model("auth", "User")
        Board = old_apps.get_model("core", "Board")
        self.owner = User.objects.create(pk=101, username="migration-owner")
        self.other = User.objects.create(pk=102, username="migration-other")
        Board.objects.create(pk=11, submitter_id=self.owner.pk, name="first")
        Board.objects.create(pk=12, submitter_id=self.owner.pk, name="second")
        Board.objects.create(pk=21, submitter_id=self.other.pk, name="other")

        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.migrate_to)
        self.apps = self.executor.loader.project_state(self.migrate_to).apps

    def tearDown(self):
        self.executor.migrate(self.executor.loader.graph.leaf_nodes())
        super(BoardDisplayOrderMigrationTests, self).tearDown()

    def test_existing_boards_receive_user_specific_descending_id_positions(self):
        Board = self.apps.get_model("core", "Board")

        self.assertEqual(
            list(
                Board.objects.filter(submitter_id=self.owner.pk)
                .order_by("display_order")
                .values_list("id", "display_order")
            ),
            [(12, 1), (11, 2)],
        )
        self.assertEqual(
            list(
                Board.objects.filter(submitter_id=self.other.pk)
                .order_by("display_order")
                .values_list("id", "display_order")
            ),
            [(21, 1)],
        )
