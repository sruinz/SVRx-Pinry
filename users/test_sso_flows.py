import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Event
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

from django.contrib.auth import BACKEND_SESSION_KEY, SESSION_KEY
from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.backends.db import SessionStore
from django.core.exceptions import ValidationError
from django.db import IntegrityError, close_old_connections, connection
from django.test import Client, RequestFactory, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from users.models import AuthPolicy, AuthVerification, ExternalIdentity, SSOAttempt, SSOProvider, User
from users.sso.client import VerifiedIdentity


@override_settings(PUBLIC=False)
class SSOFlowTests(TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(override_settings(SSO_SECRET_KEY_FILE=str(Path(directory) / 'key')))
        self.provider = SSOProvider.objects.create(
            kind='oidc', name='시험', enabled=True, issuer='https://idp.example',
            public_base_url='https://pinry.example', client_id='private-client',
        )
        self.user = User.objects.create_user('member', 'same@example.com', 'member-password')
        self.identity = VerifiedIdentity('https://idp.example', 'subject-A', 'same@example.com', '시험')
        self.exchange_count = 0
        self.enterContext(patch('users.sso.client.authorization_url', side_effect=self.authorization))
        self.enterContext(patch('users.sso.client.exchange_identity', side_effect=self.exchange))

    def authorization(self, provider, redirect_uri, state, nonce, verifier):
        self.assertEqual(redirect_uri, f'https://pinry.example/api/v2/sso/{provider.pk}/callback/')
        self.assertGreaterEqual(len(nonce), 32)
        self.assertGreaterEqual(len(verifier), 43)
        return 'https://idp.example/authorize?' + urlencode({'state': state})

    def exchange(self, provider, redirect_uri, code, nonce, verifier):
        self.exchange_count += 1
        return self.identity

    def connect(self, user=None, subject='subject-A', provider=None):
        return ExternalIdentity.objects.create(
            user=user or self.user, provider=provider or self.provider,
            issuer='https://idp.example', subject=subject,
        )

    def start(self, purpose='login', client=None, **data):
        client = client or self.client
        path = f'/api/v2/sso/{self.provider.pk}/{purpose}/'
        response = client.get(path, data) if purpose == 'login' else client.post(path, data)
        self.assertEqual(response.status_code, 302, response.content)
        return parse_qs(urlsplit(response['Location']).query)['state'][0]

    def callback(self, state, client=None, provider=None, **data):
        return (client or self.client).get(
            f'/api/v2/sso/{(provider or self.provider).pk}/callback/',
            {'state': state, 'code': 'secret-code', **data},
        )

    def reauthenticate(self):
        self.client.force_login(self.user)
        response = self.client.post('/api/v2/sso/password/reauth/', {'password': 'member-password'})
        self.assertEqual(response.status_code, 204)

    def test_anonymous_link_does_not_start_account_linking(self):
        response = self.client.post(f'/api/v2/sso/{self.provider.pk}/link/', {})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(SSOAttempt.objects.count(), 0)

    def test_provider_list_exposes_only_enabled_display_fields(self):
        SSOProvider.objects.create(kind='oidc', name='숨김')
        response = self.client.get('/api/v2/sso/providers/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [{'id': str(self.provider.pk), 'name': '시험',
                                           'login_url': f'/api/v2/sso/{self.provider.pk}/login/'}])

    def test_login_uses_existing_exact_identity_and_session_backend(self):
        self.connect()
        state = self.start(next='/profile/')
        attempt = SSOAttempt.objects.get()
        self.assertNotIn(state, attempt.state_digest + attempt.protected_payload)
        response = self.callback(state)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/profile/')
        self.assertEqual(response['Cache-Control'], 'no-store')
        self.assertEqual(response['Referrer-Policy'], 'no-referrer')
        self.assertEqual(self.client.session[SESSION_KEY], str(self.user.pk))
        self.assertEqual(self.client.session[BACKEND_SESSION_KEY], 'users.sso.authentication.SSOBackend')
        self.assertEqual(self.client.session['auth_method'], 'sso')
        self.assertEqual(self.client.session['sso_provider_revision'], 1)
        self.assertTrue(AuthVerification.objects.filter(user=self.user, kind='sso').exists())

    def test_unknown_identity_does_not_link_by_matching_email(self):
        response = self.callback(self.start())
        self.assertEqual(response['Location'], '/login/')
        self.assertNotIn(SESSION_KEY, self.client.session)
        self.assertFalse(ExternalIdentity.objects.exists())
        self.assertEqual(User.objects.count(), 1)

    def test_signup_creates_unprivileged_user_without_password(self):
        self.provider.allow_signup = True
        self.provider.save()
        response = self.callback(self.start())
        self.assertEqual(response['Location'], '/')
        user = User.objects.get(pk=self.client.session[SESSION_KEY])
        self.assertFalse(user.is_superuser or user.is_staff or user.has_usable_password())
        self.assertNotEqual(user.pk, self.user.pk)

    def test_identity_subject_case_does_not_select_other_account(self):
        self.connect(subject='subject-a')
        self.callback(self.start())
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_state_replay_only_exchanges_once(self):
        self.connect()
        state = self.start()
        self.callback(state)
        self.callback(state)
        self.assertEqual(self.exchange_count, 1)

    def test_expired_state_never_exchanges(self):
        state = self.start()
        SSOAttempt.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        self.callback(state)
        self.assertEqual(self.exchange_count, 0)

    def test_wrong_browser_or_provider_does_not_consume_valid_state(self):
        self.connect()
        state = self.start()
        other = SSOProvider.objects.create(kind='oidc', name='다른 제공자', enabled=True)
        self.callback(state, client=Client())
        self.callback(state, provider=other)
        self.assertEqual(self.exchange_count, 0)
        self.callback(state)
        self.assertEqual(self.exchange_count, 1)

    def test_unknown_state_does_not_exchange(self):
        self.callback('unknown')
        self.assertEqual(self.exchange_count, 0)

    def test_provider_revision_changed_during_exchange_rejects_login(self):
        self.connect()
        state = self.start()

        def changed(*args):
            SSOProvider.objects.filter(pk=self.provider.pk).update(revision=2)
            return self.identity
        with patch('users.sso.client.exchange_identity', side_effect=changed):
            self.callback(state)
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_user_disabled_during_exchange_rejects_login(self):
        self.connect()
        state = self.start()

        def changed(*args):
            User.objects.filter(pk=self.user.pk).update(is_active=False)
            return self.identity
        with patch('users.sso.client.exchange_identity', side_effect=changed):
            self.callback(state)
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_provider_outage_consumes_attempt_and_redirects_without_query(self):
        state = self.start()
        with patch('users.sso.client.exchange_identity', side_effect=ValidationError('외부 오류')):
            response = self.callback(state)
        self.assertEqual(response['Location'], '/login/')
        self.assertEqual(response['Cache-Control'], 'no-store')
        self.assertIsNotNone(SSOAttempt.objects.get().consumed_at)

    def test_unsafe_next_paths_are_not_redirected(self):
        self.connect()
        for path in ('https://evil.example', '//evil.example', '/\\evil.example',
                     '/%2f%2fevil.example', '/profile/?code=secret', '/profile/#token'):
            with self.subTest(path=path):
                self.client.logout()
                self.assertEqual(self.callback(self.start(next=path))['Location'], '/')

    def test_link_requires_recent_reauthentication(self):
        self.client.force_login(self.user)
        response = self.client.post(f'/api/v2/sso/{self.provider.pk}/link/')
        self.assertEqual(response.status_code, 403)
        self.assertFalse(SSOAttempt.objects.exists())

    def test_link_requires_same_authenticated_user_at_callback(self):
        self.reauthenticate()
        state = self.start('link')
        other = User.objects.create_user('other', password='other-password')
        self.client.force_login(other)
        self.callback(state)
        self.assertFalse(ExternalIdentity.objects.exists())
        self.assertEqual(self.client.session[SESSION_KEY], str(other.pk))

    def test_explicit_link_preserves_user_and_creates_identity(self):
        self.reauthenticate()
        state = self.start('link')
        self.callback(state, purpose='login')
        self.assertEqual(ExternalIdentity.objects.get().user_id, self.user.pk)
        self.assertEqual(self.client.session[SESSION_KEY], str(self.user.pk))
        self.assertNotEqual(self.client.session.get('auth_method'), 'sso')

    def test_link_conflict_never_switches_to_other_account(self):
        other = User.objects.create_user('other')
        self.connect(user=other)
        self.reauthenticate()
        self.callback(self.start('link'))
        self.assertEqual(self.client.session[SESSION_KEY], str(self.user.pk))
        self.assertEqual(ExternalIdentity.objects.get().user_id, other.pk)

    def test_password_reauthentication_never_selects_another_username(self):
        other = User.objects.create_user('other', password='other-password')
        self.client.force_login(self.user)
        response = self.client.post('/api/v2/sso/password/reauth/', {
            'username': other.username, 'password': 'other-password',
        })
        self.assertEqual(response.status_code, 403)
        self.assertNotIn('recent_auth', self.client.session)
        self.assertEqual(self.client.session[SESSION_KEY], str(self.user.pk))

    def test_password_reauthentication_respects_policy_and_csrf(self):
        self.client.force_login(self.user)
        AuthPolicy.objects.filter(pk=1).update(password_login_enabled=False)
        response = self.client.post('/api/v2/sso/password/reauth/', {'password': 'member-password'})
        self.assertEqual(response.status_code, 403)
        browser = Client(enforce_csrf_checks=True)
        browser.force_login(self.user)
        AuthPolicy.objects.filter(pk=1).update(password_login_enabled=True)
        response = browser.post('/api/v2/sso/password/reauth/', {'password': 'member-password'})
        self.assertEqual(response.status_code, 403)

    def test_sso_reauthentication_requires_existing_identity_without_switching(self):
        self.connect()
        self.client.force_login(self.user)
        self.callback(self.start('reauth'))
        self.assertEqual(self.client.session['recent_auth']['method'], 'sso')
        self.assertEqual(self.client.session[SESSION_KEY], str(self.user.pk))
        self.identity = VerifiedIdentity('https://idp.example', 'other-subject', None, '')
        self.callback(self.start('reauth'))
        self.assertEqual(ExternalIdentity.objects.count(), 1)
        self.assertEqual(self.client.session[SESSION_KEY], str(self.user.pk))

    def test_last_usable_login_identity_cannot_be_unlinked(self):
        identity = self.connect()
        self.reauthenticate()
        AuthPolicy.objects.filter(pk=1).update(password_login_enabled=False)
        response = self.client.post(f'/api/v2/sso/identities/{identity.pk}/unlink/')
        self.assertEqual(response.status_code, 403)
        self.assertTrue(ExternalIdentity.objects.filter(pk=identity.pk).exists())

    def test_unlink_with_password_and_recent_auth_succeeds(self):
        identity = self.connect()
        self.reauthenticate()
        response = self.client.post(f'/api/v2/sso/identities/{identity.pk}/unlink/')
        self.assertEqual(response.status_code, 204)
        self.assertFalse(ExternalIdentity.objects.exists())

    def test_identity_list_and_unlink_are_owner_only(self):
        identity = self.connect()
        other = User.objects.create_user('other', password='other-password')
        self.client.force_login(other)
        response = self.client.get('/api/v2/sso/identities/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])
        response = self.client.post(f'/api/v2/sso/identities/{identity.pk}/unlink/')
        self.assertIn(response.status_code, (403, 404))
        self.assertTrue(ExternalIdentity.objects.filter(pk=identity.pk).exists())

    def test_changed_provider_invalidates_sso_session_on_next_request(self):
        self.connect()
        self.callback(self.start())
        SSOProvider.objects.filter(pk=self.provider.pk).update(revision=2)
        response = self.client.get('/api/v2/sso/identities/')
        self.assertEqual(response.status_code, 403)
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_private_site_does_not_exempt_sso_prefix(self):
        for path in ('/api/v2/sso/identities/', '/api/v2/sso/providers/extra/',
                     f'/api/v2/sso/{self.provider.pk}/reauth/'):
            self.assertEqual(self.client.get(path).status_code, 403)

    def test_api_token_alone_cannot_establish_password_reauthentication(self):
        response = self.client.post('/api/v2/sso/password/reauth/',
                                    {'password': 'member-password'},
                                    HTTP_AUTHORIZATION='Token ' + self.user.auth_token.key)
        self.assertEqual(response.status_code, 403)
        self.assertNotIn('recent_auth', self.client.session)

    def test_link_conflict_rolls_back_new_user_and_verification(self):
        self.provider.allow_signup = True
        self.provider.save()
        state = self.start()
        with patch('users.sso.flows.ExternalIdentity.objects.create', side_effect=IntegrityError):
            self.callback(state)
        self.assertEqual(User.objects.count(), 1)
        self.assertFalse(AuthVerification.objects.exists())
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_digest_match_requires_exact_raw_identity_tuple(self):
        identity = self.connect()
        ExternalIdentity.objects.filter(pk=identity.pk).update(subject='subject-a')
        self.callback(self.start())
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_old_reauthentication_cannot_start_link(self):
        self.reauthenticate()
        session = self.client.session
        recent = session['recent_auth']
        recent['verified_at'] -= 301
        session['recent_auth'] = recent
        session.save()
        response = self.client.post(f'/api/v2/sso/{self.provider.pk}/link/')
        self.assertEqual(response.status_code, 403)
        self.assertFalse(SSOAttempt.objects.exists())

    def test_disabled_provider_during_exchange_rejects_login(self):
        self.connect()
        state = self.start()

        def changed(*args):
            SSOProvider.objects.filter(pk=self.provider.pk).update(enabled=False)
            return self.identity
        with patch('users.sso.client.exchange_identity', side_effect=changed):
            self.callback(state)
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_unauthenticated_callback_cannot_turn_link_into_login(self):
        self.reauthenticate()
        state = self.start('link')
        self.client.logout()
        self.callback(state, purpose='login')
        self.assertFalse(ExternalIdentity.objects.exists())
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_sso_backend_cannot_authenticate_password_and_rejects_inactive_user(self):
        from users.sso.authentication import SSOBackend
        backend = SSOBackend()
        self.assertIsNone(backend.authenticate(None, username='member', password='member-password'))
        self.assertEqual(backend.get_user(self.user.pk).pk, self.user.pk)
        self.user.is_active = False
        self.user.save()
        self.assertIsNone(backend.get_user(self.user.pk))

    def test_authenticated_login_start_does_not_switch_account(self):
        self.client.force_login(self.user)
        response = self.client.get(f'/api/v2/sso/{self.provider.pk}/login/')
        self.assertEqual(response.status_code, 403)
        self.assertFalse(SSOAttempt.objects.exists())

    def test_expired_attempt_cleanup_is_bounded(self):
        from users.sso.secrets import encrypt_secret
        encrypted = encrypt_secret('{}')
        SSOAttempt.objects.bulk_create([
            SSOAttempt(state_digest=str(index), browser_digest='old', provider=self.provider,
                       provider_revision=1, purpose='login', protected_payload=encrypted,
                       expires_at=timezone.now() - timedelta(minutes=1))
            for index in range(105)
        ])
        self.start()
        self.assertEqual(SSOAttempt.objects.filter(expires_at__lte=timezone.now()).count(), 5)

    def test_error_callback_consumes_state_and_cleans_query(self):
        state = self.start()
        response = self.client.get(f'/api/v2/sso/{self.provider.pk}/callback/',
                                   {'state': state, 'error': 'access_denied'})
        self.assertEqual(response['Location'], '/login/')
        self.assertIsNotNone(SSOAttempt.objects.get().consumed_at)
        self.assertNotIn(SESSION_KEY, self.client.session)
        self.assertEqual(self.exchange_count, 0)


class SSOConcurrentAttemptTests(TransactionTestCase):
    def test_second_callback_cannot_exchange_while_first_exchange_is_waiting(self):
        from users.sso.flows import begin_attempt, finish_attempt
        AuthPolicy.objects.get_or_create(pk=1)
        provider = SSOProvider.objects.create(
            kind='oidc', name='경쟁 시험', enabled=True, allow_signup=True,
            public_base_url='https://pinry.example',
        )
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(override_settings(SSO_SECRET_KEY_FILE=str(Path(directory) / 'key')))
        request = RequestFactory().get('/')
        request.session = SessionStore()
        request.user = AnonymousUser()
        with patch('users.sso.client.authorization_url', side_effect=lambda p, r, s, n, v: s):
            state = begin_attempt(request, provider, 'login', '/')
        request.session.save()
        session_key = request.session.session_key
        entered, release = Event(), Event()
        network_transactions = []

        def exchange(*args):
            network_transactions.append(connection.in_atomic_block)
            entered.set()
            if not release.wait(10):
                raise AssertionError('시험 콜백이 해제되지 않았습니다.')
            return VerifiedIdentity('https://idp.example', 'concurrent', None, '')

        def finish():
            close_old_connections()
            callback_request = RequestFactory().get('/')
            callback_request.session = SessionStore(session_key=session_key)
            callback_request.user = AnonymousUser()
            try:
                return finish_attempt(callback_request, provider, state, 'code').pk
            finally:
                close_old_connections()

        with patch('users.sso.client.exchange_identity', side_effect=exchange):
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(finish)
                try:
                    self.assertTrue(entered.wait(10))
                    second = executor.submit(finish)
                    with self.assertRaises(ValidationError):
                        second.result(timeout=10)
                finally:
                    release.set()
                user_id = first.result(timeout=10)
        self.assertEqual(network_transactions, [False])
        self.assertEqual(ExternalIdentity.objects.get().user_id, user_id)
        self.assertEqual(AuthVerification.objects.count(), 1)
