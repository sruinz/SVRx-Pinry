from datetime import datetime, timezone as dt_timezone
from unittest import mock

from django.test import override_settings
from django.urls import reverse
from django_images.test_helpers import TemporaryMediaMixin
from rest_framework.test import APITestCase

from core.models import Image, Pin
from core.tests.helpers import create_image, create_user


class PinSearchTests(TemporaryMediaMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.owner = create_user("search-owner")
        self.other = create_user("search-other")
        self.url = reverse("pin-list")
        self.pins = {}
        for name, animation, width, height in (
            ("wide", "static", 1200, 600),
            ("tall", "gif", 600, 1200),
            ("square", "webp", 800, 800),
            ("unknown", None, 900, 900),
            ("broken", "unreadable", 0, 0),
        ):
            image = create_image()
            Image.objects.filter(pk=image.pk).update(
                animation_status=animation, width=width, height=height,
            )
            self.pins[name] = Pin.objects.create(submitter=self.owner, image=image)
        self.pins["tall"].tags.add("alpha", "beta")
        self.pins["square"].tags.add("alpha")

    def ids(self, **params):
        response = self.client.get(self.url, params)
        self.assertEqual(response.status_code, 200, response.data)
        return [row["id"] for row in response.data["results"]]

    def test_animation_uses_recorded_badge_state_without_scanning(self):
        with mock.patch.object(Image, "_read_animation_status", side_effect=AssertionError("scan")):
            self.assertEqual(set(self.ids(animation="animated")), {
                self.pins["tall"].pk, self.pins["square"].pk,
            })
            self.assertEqual(self.ids(animation="static"), [self.pins["wide"].pk])
            self.assertEqual(len(self.ids()), 5)

    def test_aspect_and_minimum_dimensions_are_combined(self):
        self.assertEqual(self.ids(aspect="landscape", min_width="1000", min_height="600"), [self.pins["wide"].pk])
        self.assertEqual(self.ids(aspect="portrait"), [self.pins["tall"].pk])
        self.assertEqual(set(self.ids(aspect="square")), {self.pins["square"].pk, self.pins["unknown"].pk})
        self.assertEqual(self.ids(min_height="1201"), [])

    def test_tag_sort_and_pagination_remain_effective(self):
        self.assertEqual(self.ids(animation="animated", **{"tags__name": ["alpha", "beta"]}), [self.pins["tall"].pk])
        first = self.ids(animation="animated", sort="oldest", limit=1)
        second = self.ids(animation="animated", sort="oldest", limit=1, offset=1)
        self.assertEqual(first + second, [self.pins["tall"].pk, self.pins["square"].pk])

    def test_private_pins_never_leak_through_filters(self):
        private = Pin.objects.create(submitter=self.other, image=self.pins["tall"].image, private=True)
        self.assertNotIn(private.pk, self.ids(animation="animated"))
        self.client.force_authenticate(self.other)
        self.assertIn(private.pk, self.ids(animation="animated"))

    @override_settings(TIME_ZONE="Asia/Seoul")
    def test_date_range_includes_the_whole_server_local_day(self):
        moments = ("2026-09-12T14:59:59+00:00", "2026-09-12T15:00:00+00:00", "2026-09-13T14:59:59+00:00", "2026-09-13T15:00:00+00:00")
        names = ("wide", "tall", "square", "unknown")
        for name, value in zip(names, moments):
            Pin.objects.filter(pk=self.pins[name].pk).update(published=datetime.fromisoformat(value))
        Pin.objects.filter(pk=self.pins["broken"].pk).update(published=datetime(2020, 1, 1, tzinfo=dt_timezone.utc))
        self.assertEqual(set(self.ids(date_from="2026-09-13", date_to="2026-09-13")), {self.pins["tall"].pk, self.pins["square"].pk})

    @override_settings(TIME_ZONE="America/New_York")
    def test_date_end_uses_next_local_midnight_across_dst(self):
        Pin.objects.all().update(published=datetime(2020, 1, 1, tzinfo=dt_timezone.utc))
        Pin.objects.filter(pk=self.pins["wide"].pk).update(published=datetime.fromisoformat("2026-03-09T03:59:59+00:00"))
        Pin.objects.filter(pk=self.pins["tall"].pk).update(published=datetime.fromisoformat("2026-03-09T04:00:00+00:00"))
        self.assertEqual(self.ids(date_from="2026-03-08", date_to="2026-03-08"), [self.pins["wide"].pk])

    def test_invalid_and_repeated_parameters_return_field_errors(self):
        for query, field in (
            ("animation=other", "animation"), ("animation=", "animation"),
            ("aspect=wide", "aspect"), ("aspect=square&aspect=portrait", "aspect"),
            ("min_width=0", "min_width"), ("min_width=-1", "min_width"),
            ("min_width=1.5", "min_width"), ("min_width=2147483648", "min_width"),
            ("min_height=1&min_height=2", "min_height"),
            ("date_from=2026-02-30", "date_from"), ("date_to=2026-2-03", "date_to"),
            ("date_to=9999-12-31", "date_to"), ("date_from=0001-01-01", "date_from"),
            ("date_from=2026-09-14&date_to=2026-09-13", "date_to"),
        ):
            with self.subTest(query=query):
                response = self.client.get(self.url + "?" + query)
                self.assertEqual(response.status_code, 400, response.data)
                self.assertEqual(response.data["code"], "pin_search_invalid")
                self.assertIn(field, response.data["fields"])

    def test_supported_numeric_and_date_boundaries_are_evaluated(self):
        self.assertEqual(len(self.ids(min_width="1", min_height="1")), 4)
        self.assertEqual(self.ids(min_width="2147483647"), [])
        self.assertEqual(self.ids(min_height="2147483647"), [])
        for zone in ("Asia/Seoul", "America/New_York"):
            with self.subTest(zone=zone), override_settings(TIME_ZONE=zone):
                self.assertEqual(len(self.ids(date_from="0002-01-01", date_to="9998-12-31")), 5)
