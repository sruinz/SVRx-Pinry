from django.urls import reverse
from django_images.test_helpers import TemporaryMediaMixin
from rest_framework.test import APITestCase

from core.models import Board
from core.tests.helpers import create_image, create_pin, create_user


class NestedUserPrivacyTests(TemporaryMediaMixin, APITestCase):
    def setUp(self):
        super(NestedUserPrivacyTests, self).setUp()
        self.user = create_user('nested-user-privacy')
        self.pin = create_pin(self.user, create_image(), [])
        self.board = Board.objects.create(
            name='nested-user-privacy',
            submitter=self.user,
        )

    def test_pin_detail_submitter_has_only_public_fields(self):
        response = self.client.get(
            reverse('pin-detail', kwargs={'pk': self.pin.pk}),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            set(response.data['submitter']),
            {'username', 'gravatar', 'resource_link'},
        )

    def test_board_detail_submitter_has_only_public_fields(self):
        response = self.client.get(
            reverse('board-detail', kwargs={'pk': self.board.pk}),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            set(response.data['submitter']),
            {'username', 'gravatar', 'resource_link'},
        )
