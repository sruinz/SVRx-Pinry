from django.core.cache import cache
from django.urls import reverse
from rest_framework.test import APITestCase
from taggit.models import Tag


class TagAutoCompleteAPITests(APITestCase):
    def setUp(self):
        cache.clear()
        self.url = reverse("tag-list")

    def tearDown(self):
        cache.clear()

    def test_new_tag_is_visible_after_an_empty_list_was_requested(self):
        initial_response = self.client.get(self.url)
        self.assertEqual(initial_response.status_code, 200)
        self.assertEqual(initial_response.json(), [])

        Tag.objects.create(name="2", slug="2")

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [{"name": "2"}])
