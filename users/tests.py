import queue
import threading

import mock
from django.db import (
    DatabaseError,
    close_old_connections,
    connection,
    connections,
)
from django.db.models.query import QuerySet
from django.test import Client, TestCase, TransactionTestCase
from django.test.utils import override_settings
from django.urls import reverse
from rest_framework.reverse import reverse as drf_reverse

from .auth.backends import CombinedAuthBackend
from .models import AdminBootstrapState, User
from .serializers import CurrentUserSerializer


def mock_requests_get(url, headers=None):
    response = mock.Mock(content=open('docs/src/imgs/logo-dark.png', 'rb').read())
    return response


class CombinedAuthBackendTest(TestCase):
    def setUp(self):
        self.backend = CombinedAuthBackend()
        self.username = 'jdoe'
        self.email = 'jdoe@example.com'
        self.password = 'password'
        User.objects.create_user(username=self.username, email=self.email, password=self.password)

    def test_authenticate_username(self):
        self.assertTrue(self.backend.authenticate(username=self.username, password=self.password))

    def test_authenticate_email(self):
        self.assertTrue(self.backend.authenticate(username=self.email, password=self.password))

    def test_authenticate_wrong_password(self):
        self.assertIsNone(self.backend.authenticate(username=self.username, password='wrong-password'))

    def test_authenticate_unknown_user(self):
        self.assertIsNone(self.backend.authenticate(username='wrong-username', password='wrong-password'))

    def test_exact_email_shaped_username_wins_over_duplicate_email_aliases(self):
        from django.contrib.auth import authenticate

        exact = User.objects.create_user(
            username='owner@example.com',
            email='owner@example.com',
            password='exact-password',
        )
        User.objects.create_user(
            username='sso-account',
            email='owner@example.com',
        )

        user = authenticate(username='owner@example.com', password='exact-password')

        self.assertEqual(user.pk, exact.pk)

    def test_ambiguous_email_alias_fails_authentication(self):
        from django.contrib.auth import authenticate

        User.objects.create_user(
            username='first-account',
            email='shared@example.com',
            password='first-password',
        )
        User.objects.create_user(
            username='second-account',
            email='shared@example.com',
        )

        user = authenticate(username='shared@example.com', password='first-password')

        self.assertIsNone(user)

    @override_settings(PUBLIC=False)
    def test_inactive_password_session_is_rejected_by_private_site(self):
        from django.contrib.auth import BACKEND_SESSION_KEY

        response = self.client.post(
            '/api/v2/profile/login/',
            {'username': self.username, 'password': self.password},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.client.session[BACKEND_SESSION_KEY],
            'users.auth.backends.CombinedAuthBackend',
        )
        self.assertEqual(self.client.get('/api/v2/pins/').status_code, 200)

        User.objects.filter(username=self.username).update(is_active=False)
        response = self.client.get('/api/v2/pins/')

        self.assertEqual(response.status_code, 403)


class CreateUserTest(TestCase):
    def register(self, username, **extra_data):
        data = {
            'username': username,
            'email': '{}@example.com'.format(username),
            'password': 'password',
            'password_repeat': 'password',
        }
        data.update(extra_data)
        return self.client.post(
            reverse('users:user-list'),
            data=data,
        )

    def test_create_post(self):
        response = self.register('jdoe')
        self.assertEqual(response.status_code, 201)

    def test_first_registered_user_becomes_admin(self):
        response = self.register('first')
        first_user = User.objects.get(username='first')

        self.assertEqual(response.status_code, 201)
        self.assertTrue(first_user.is_staff)
        self.assertTrue(first_user.is_superuser)

    def test_second_registered_user_remains_regular(self):
        self.register('first')

        response = self.register('second')
        second_user = User.objects.get(username='second')

        self.assertEqual(response.status_code, 201)
        self.assertFalse(second_user.is_staff)
        self.assertFalse(second_user.is_superuser)

    def test_registration_after_first_admin_demotion_remains_regular(self):
        self.register('first')
        first_user = User.objects.get(username='first')
        first_user.is_staff = False
        first_user.is_superuser = False
        first_user.save(update_fields=['is_staff', 'is_superuser'])

        response = self.register('second')
        second_user = User.objects.get(username='second')

        self.assertEqual(response.status_code, 201)
        self.assertFalse(second_user.is_staff)
        self.assertFalse(second_user.is_superuser)

    def test_registration_ignores_submitted_admin_flags(self):
        self.register('first')

        response = self.register(
            'second',
            is_staff=True,
            is_superuser=True,
        )
        second_user = User.objects.get(username='second')

        self.assertEqual(response.status_code, 201)
        self.assertFalse(second_user.is_staff)
        self.assertFalse(second_user.is_superuser)

    def test_active_existing_user_completes_bootstrap_without_promotion(self):
        User.objects.create_user(username='cli-user', password='password')
        AdminBootstrapState.objects.update_or_create(
            pk=1,
            defaults={'bootstrap_complete': False},
        )

        response = self.register('web-user')
        web_user = User.objects.get(username='web-user')

        self.assertEqual(response.status_code, 201)
        self.assertFalse(web_user.is_staff)
        self.assertFalse(web_user.is_superuser)
        self.assertTrue(
            AdminBootstrapState.objects.get(pk=1).bootstrap_complete,
        )

    def test_failed_first_registration_rolls_back_bootstrap_claim(self):
        claim_states_during_save = []

        def fail_save(*args, **kwargs):
            claim_states_during_save.append(
                AdminBootstrapState.objects.get(pk=1).bootstrap_complete,
            )
            raise DatabaseError('save failed')

        with mock.patch.object(User, 'save', side_effect=fail_save):
            with self.assertRaises(DatabaseError):
                self.register('first')

        self.assertEqual(claim_states_during_save, [True])
        self.assertFalse(
            AdminBootstrapState.objects.get(pk=1).bootstrap_complete,
        )
        self.assertFalse(User.objects.exists())

    @override_settings(ALLOW_NEW_REGISTRATIONS=False)
    def test_create_post_not_allowed(self):
        data = {
            'username': 'jdoe',
            'email': 'jdoe@example.com',
            'password': 'password',
            'password_repeat': 'password',

        }
        response = self.client.post(
            reverse('users:user-list'),
            data=data,
        )
        self.assertEqual(response.status_code, 401)


class AdminBootstrapConcurrencyTest(TransactionTestCase):
    def setUp(self):
        super(AdminBootstrapConcurrencyTest, self).setUp()
        from users.models import AuthPolicy
        AuthPolicy.objects.get_or_create(pk=1)
        if connection.vendor != 'sqlite':
            self.skipTest('This concurrency contract requires SQLite.')
        if connection.creation.is_in_memory_db(
            connection.settings_dict['NAME'],
        ):
            self.skipTest('This concurrency contract requires file SQLite.')
        AdminBootstrapState.objects.update_or_create(
            pk=1,
            defaults={'bootstrap_complete': False},
        )

    def test_two_concurrent_registrations_create_exactly_one_admin(self):
        start_barrier = threading.Barrier(2)
        after_state_read_barrier = threading.Barrier(2)
        before_state_update_barrier = threading.Barrier(2)
        outcomes = queue.Queue()
        original_get_or_create = QuerySet.get_or_create
        original_update = QuerySet.update

        def pause_after_state_read(queryset, *args, **kwargs):
            result = original_get_or_create(queryset, *args, **kwargs)
            if queryset.model is AdminBootstrapState:
                after_state_read_barrier.wait(timeout=5)
            return result

        def pause_before_state_update(queryset, **kwargs):
            if queryset.model is AdminBootstrapState:
                before_state_update_barrier.wait(timeout=5)
            return original_update(queryset, **kwargs)

        def register(username):
            close_old_connections()
            try:
                client = Client()
                start_barrier.wait(timeout=5)
                response = client.post(
                    reverse('users:user-list'),
                    data={
                        'username': username,
                        'email': '{}@example.com'.format(username),
                        'password': 'password',
                        'password_repeat': 'password',
                    },
                )
                outcomes.put(('response', username, response.status_code))
            except BaseException as error:
                outcomes.put(('error', username, error))
            finally:
                connections['default'].close()

        with mock.patch.object(
            QuerySet,
            'get_or_create',
            autospec=True,
            side_effect=pause_after_state_read,
        ), mock.patch.object(
            QuerySet,
            'update',
            autospec=True,
            side_effect=pause_before_state_update,
        ):
            threads = [
                threading.Thread(target=register, args=(username,))
                for username in ('first', 'second')
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

        self.assertFalse(
            any(thread.is_alive() for thread in threads),
            'Concurrent registration threads did not finish.',
        )
        collected = [outcomes.get(timeout=1) for _ in range(2)]
        errors = [item for item in collected if item[0] == 'error']
        self.assertEqual(errors, [])
        self.assertEqual(
            sorted(item[2] for item in collected if item[0] == 'response'),
            [201, 201],
        )
        self.assertEqual(User.objects.count(), 2)
        self.assertEqual(
            User.objects.filter(is_staff=True, is_superuser=True).count(),
            1,
        )
        self.assertTrue(
            AdminBootstrapState.objects.get(pk=1).bootstrap_complete,
        )


class LogoutViewTest(TestCase):
    def setUp(self):
        User.objects.create_user(username='jdoe', password='password')
        self.client.login(username='jdoe', password='password')

    def test_logout_view(self):
        response = self.client.get(reverse('users:logout'))
        self.assertEqual(response.status_code, 302)


class ProfileViewTest(TestCase):
    def setUp(self):
        from rest_framework.authtoken.models import Token

        self.first_user = User.objects.create_user(username='jdoe', password='password')
        self.first_user.is_staff = True
        self.first_user.save(update_fields=['is_staff'])
        self.token = Token.objects.get(user=self.first_user)
        self.client.login(username='jdoe', password='password')

    def test_public_user_list_has_only_public_fields(self):
        url = drf_reverse('users:public-user-list')
        response = self.client.get(f"{url}?username={self.first_user.username}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            set(response.data[0]),
            {'username', 'gravatar', 'resource_link'},
        )

    def test_public_user_resource_link_returns_public_detail(self):
        list_url = drf_reverse('users:public-user-list')
        list_response = self.client.get(
            f"{list_url}?username={self.first_user.username}",
        )

        self.assertEqual(list_response.status_code, 200)
        detail_response = self.client.get(
            list_response.data[0]['resource_link'],
        )

        self.assertEqual(detail_response.status_code, 200)
        self.assertEqual(
            set(detail_response.data),
            {'username', 'gravatar', 'resource_link'},
        )

    def test_public_user_list_without_exact_username_is_empty(self):
        response = self.client.get(drf_reverse('users:public-user-list'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, [])

    def test_current_user_list_includes_private_fields_and_admin_access(self):
        response = self.client.get(drf_reverse('users:user-list'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            set(response.data[0]),
            {
                'username',
                'token',
                'email',
                'gravatar',
                'can_access_admin',
                'resource_link',
            },
        )
        self.assertEqual(response.data[0]['email'], self.first_user.email)
        self.assertEqual(response.data[0]['token'], self.token.key)
        self.assertIs(response.data[0]['can_access_admin'], True)

    def test_login_response_has_only_current_user_read_fields(self):
        self.client.logout()

        response = self.client.post(
            reverse('users:login'),
            data={
                'username': self.first_user.username,
                'password': 'password',
            },
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            set(response.json()),
            {
                'username',
                'token',
                'email',
                'gravatar',
                'can_access_admin',
                'resource_link',
            },
        )

    def test_active_non_staff_cannot_access_admin(self):
        self.first_user.is_staff = False

        self.assertIs(
            CurrentUserSerializer().get_can_access_admin(self.first_user),
            False,
        )

    def test_inactive_staff_cannot_access_admin(self):
        self.first_user.is_active = False

        self.assertIs(
            CurrentUserSerializer().get_can_access_admin(self.first_user),
            False,
        )
