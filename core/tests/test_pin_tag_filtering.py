from django.urls import reverse
from django_images.test_helpers import TemporaryMediaMixin
from rest_framework import status
from rest_framework.test import APITestCase

from core.models import Pin
from core.tests.helpers import create_image, create_user


class PinTagFilteringAPITests(TemporaryMediaMixin, APITestCase):
    def setUp(self):
        super(PinTagFilteringAPITests, self).setUp()
        self.owner = create_user("tag-filter-owner")
        self.image = create_image()
        self.url = reverse("pin-list")
        self.both = self.create_pin("alpha", "beta")
        self.alpha_only = self.create_pin("alpha")
        self.beta_only = self.create_pin("beta")

    def create_pin(self, *tags, **fields):
        pin = Pin.objects.create(
            submitter=self.owner,
            image=self.image,
            **fields
        )
        pin.tags.add(*tags)
        return pin

    def test_repeated_tag_names_require_every_tag(self):
        response = self.client.get(
            "{}?tags__name=alpha&tags__name=beta".format(self.url)
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(
            [row["id"] for row in response.data["results"]],
            [self.both.pk],
        )

    def test_single_tag_name_keeps_the_existing_exact_filter(self):
        response = self.client.get(
            "{}?tags__name=alpha".format(self.url)
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 2)
        self.assertEqual(
            {row["id"] for row in response.data["results"]},
            {self.both.pk, self.alpha_only.pk},
        )

    def test_empty_tag_name_keeps_the_existing_unfiltered_behavior(self):
        response = self.client.get(
            "{}?tags__name=".format(self.url)
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 3)
        self.assertEqual(
            {row["id"] for row in response.data["results"]},
            {self.both.pk, self.alpha_only.pk, self.beta_only.pk},
        )

    def test_tag_filter_preserves_explicit_sorting(self):
        second_both = self.create_pin("alpha", "beta")
        response = self.client.get(
            "{}?tags__name=alpha&tags__name=beta&sort=oldest".format(
                self.url
            )
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [row["id"] for row in response.data["results"]],
            [self.both.pk, second_both.pk],
        )

    def test_many_repeated_tags_do_not_exceed_the_sqlite_join_limit(self):
        tag_names = ["tag-{}".format(index) for index in range(32)]
        matching_pin = self.create_pin(*tag_names)
        query = "&".join(
            "tags__name={}".format(tag_name) for tag_name in tag_names
        )

        response = self.client.get("{}?{}".format(self.url, query))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["id"], matching_pin.pk)

    def test_repeated_tag_filter_keeps_private_pin_visibility(self):
        private_pin = self.create_pin("alpha", "beta", private=True)

        response = self.client.get(
            "{}?tags__name=alpha&tags__name=beta".format(self.url)
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        self.assertNotIn(
            private_pin.pk,
            [row["id"] for row in response.data["results"]],
        )

    def test_repeated_tag_filter_paginates_unique_pins(self):
        second_both = self.create_pin("alpha", "beta")

        first_page = self.client.get(
            "{}?tags__name=alpha&tags__name=beta&limit=1&offset=0".format(
                self.url
            )
        )
        second_page = self.client.get(
            "{}?tags__name=alpha&tags__name=beta&limit=1&offset=1".format(
                self.url
            )
        )

        self.assertEqual(first_page.data["count"], 2)
        self.assertEqual(second_page.data["count"], 2)
        page_ids = [
            first_page.data["results"][0]["id"],
            second_page.data["results"][0]["id"],
        ]
        self.assertEqual(set(page_ids), {self.both.pk, second_both.pk})
