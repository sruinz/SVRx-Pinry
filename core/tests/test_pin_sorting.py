from datetime import timedelta

from django.urls import reverse
from django.utils import timezone
from django_images.test_helpers import TemporaryMediaMixin
from rest_framework import status
from rest_framework.test import APITestCase

from core.models import Pin
from core.tests.helpers import create_image, create_user


class PinSortAPITests(TemporaryMediaMixin, APITestCase):
    def setUp(self):
        super(PinSortAPITests, self).setUp()
        self.owner = create_user("pin-sort-owner")
        self.other = create_user("pin-sort-other")
        self.image = create_image()
        self.url = reverse("pin-list")
        self.pins = [
            Pin.objects.create(submitter=self.owner, image=self.image)
            for _ in range(7)
        ]
        base = timezone.now() - timedelta(days=2)
        for index, pin in enumerate(self.pins):
            Pin.objects.filter(pk=pin.pk).update(
                published=base + timedelta(days=index // 2)
            )

    def ids(self, **params):
        response = self.client.get(self.url, params)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return [row["id"] for row in response.data["results"]]

    def test_missing_sort_keeps_legacy_descending_id(self):
        self.assertEqual(
            self.ids(),
            sorted((pin.pk for pin in self.pins), reverse=True),
        )

    def test_legacy_ordering_parameter_remains_compatible(self):
        self.assertEqual(
            self.ids(ordering="-id"),
            sorted((pin.pk for pin in self.pins), reverse=True),
        )

    def test_date_modes_use_id_as_the_tie_breaker(self):
        latest = self.ids(sort="latest")
        oldest = self.ids(sort="oldest")
        self.assertEqual(oldest, list(reversed(latest)))

    def test_invalid_sort_contract_is_code_only(self):
        invalid_queries = (
            "sort=random", "sort=random&random_seed=-1",
            "sort=latest&random_seed=1", "random_seed=1",
            "sort=random&random_seed=01",
            "sort=latest&sort=oldest",
            "sort=random&random_seed=1&random_seed=2",
            "sort=random&random_seed=2147483647",
            "sort=latest&ordering=-id",
        )
        for query in invalid_queries:
            response = self.client.get("{}?{}".format(self.url, query))
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertEqual(response.json(), {"code": "pin_sort_invalid"})

    def test_random_is_stable_across_offset_pages(self):
        first = self.client.get(self.url, {
            "sort": "random", "random_seed": "2147483646",
            "limit": 3, "offset": 0,
        })
        second = self.client.get(self.url, {
            "sort": "random", "random_seed": "2147483646",
            "limit": 3, "offset": 3,
        })
        third = self.client.get(self.url, {
            "sort": "random", "random_seed": "2147483646",
            "limit": 3, "offset": 6,
        })
        combined = [
            row["id"] for response in (first, second, third)
            for row in response.data["results"]
        ]
        self.assertEqual(len(combined), len(set(combined)))
        self.assertEqual(set(combined), {pin.pk for pin in self.pins})
        self.assertEqual(combined, self.ids(
            sort="random", random_seed="2147483646"
        ))

    def test_random_sort_does_not_expand_private_visibility(self):
        private_pin = Pin.objects.create(
            submitter=self.other, image=self.image, private=True
        )
        response = self.client.get(self.url, {
            "sort": "random", "random_seed": "17"
        })
        ids = {row["id"] for row in response.data["results"]}
        self.assertNotIn(private_pin.pk, ids)

    def test_random_coefficients_and_keys_stay_in_integer_range(self):
        from core.pin_sorting import PRIME, random_coefficients

        multiplier, increment = random_coefficients(2147483646)
        self.assertEqual((multiplier, increment), (12346, 1012239698))

        boundary_user = create_user("pin-sort-boundary")
        boundary_ids = (PRIME - 1, PRIME, PRIME + 1)
        for pin_id in boundary_ids:
            Pin.objects.create(
                pk=pin_id, submitter=boundary_user, image=self.image
            )
        response = self.client.get(self.url, {
            "sort": "random", "random_seed": "2147483646",
            "submitter__username": boundary_user.username,
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [row["id"] for row in response.data["results"]],
            [PRIME - 1, PRIME, PRIME + 1],
        )

    def test_random_sort_casts_pin_id_to_bigint_before_arithmetic(self):
        from django.db.backends.postgresql.base import DatabaseWrapper

        from core.pin_sorting import PinSort, apply_pin_sort

        query = apply_pin_sort(Pin.objects.all(), PinSort("random", 1)).query
        postgres = DatabaseWrapper({"NAME": "pinry"}, "postgresql")
        sql = query.get_compiler(connection=postgres).as_sql()[0]

        self.assertIn('(\"core_pin\".\"id\")::bigint', sql)
