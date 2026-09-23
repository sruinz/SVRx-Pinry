import tempfile
from types import SimpleNamespace
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
        self.client.defaults['HTTP_HOST'] = 'pinry.example'
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
        self.assertEqual(response.json(), {'providers': [{'id': str(self.provider.pk), 'name': '시험',
                                           'kind': 'oidc',
                                           'login_url': f'/api/v2/sso/{self.provider.pk}/login/'}],
                                           'password_login_enabled': True, 'api_tokens_enabled': True,
                                           'recent_auth_remaining_seconds': 0})

    def test_provider_list_exposes_recent_reauthentication_window(self):
        self.reauthenticate()

        response = self.client.get('/api/v2/sso/providers/')

        self.assertGreater(response.json()['recent_auth_remaining_seconds'], 0)
        self.assertLessEqual(response.json()['recent_auth_remaining_seconds'], 300)

    def test_lan_login_moves_to_public_host_before_creating_attempt(self):
        self.connect()
        lan = Client(HTTP_HOST='192.168.0.45:2048')
        path = f'/api/v2/sso/{self.provider.pk}/login/'
        response = lan.get(path, {'next': '/profile/'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], f'https://pinry.example{path}?next=%2Fprofile%2F')
        self.assertEqual(response['Cache-Control'], 'no-store')
        self.assertEqual(response['Referrer-Policy'], 'no-referrer')
        self.assertFalse(SSOAttempt.objects.exists())
        self.assertNotIn('sso_browser', lan.session)

        public = Client(HTTP_HOST='pinry.example')
        start = public.get(response['Location'], secure=True)
        state = parse_qs(urlsplit(start['Location']).query)['state'][0]
        result = self.callback(state, client=public)
        self.assertEqual(result['Location'], '/profile/')
        self.assertEqual(public.session[SESSION_KEY], str(self.user.pk))
        self.assertNotIn(SESSION_KEY, lan.session)
        self.assertEqual(self.exchange_count, 1)

    def test_public_redirect_uses_configured_host_and_safe_next_only(self):
        path = f'/api/v2/sso/{self.provider.pk}/login/'
        for host in ('10.12.0.5:8080', '[fd00::5]:8080', 'evil.example', 'pinry.example:8080'):
            with self.subTest(host=host):
                response = Client(HTTP_HOST=host).get(path, {
                    'next': '//evil.example/', 'state': 'do-not-forward', 'code': 'do-not-forward',
                })
                self.assertEqual(response['Location'], f'https://pinry.example{path}?next=%2F')
        self.assertFalse(SSOAttempt.objects.exists())

    def test_public_host_behind_http_upstream_does_not_redirect_loop(self):
        for host in ('pinry.example', 'PINRY.EXAMPLE:443'):
            with self.subTest(host=host):
                response = Client(HTTP_HOST=host).get(f'/api/v2/sso/{self.provider.pk}/login/')
                self.assertTrue(response['Location'].startswith('https://idp.example/authorize?'))
        self.assertEqual(SSOAttempt.objects.count(), 2)

    def test_lan_manual_actions_require_restarting_on_public_profile(self):
        self.reauthenticate()
        self.connect()
        for purpose in ('link', 'reauth'):
            with self.subTest(purpose=purpose):
                response = self.client.post(f'/api/v2/sso/{self.provider.pk}/{purpose}/',
                                            {'next': '/profile/'}, HTTP_HOST='192.168.0.45:2048')
                self.assertEqual(response.status_code, 400)
                self.assertContains(response, '공개 주소', status_code=400)
                self.assertContains(response, 'href="https://pinry.example/"', status_code=400)
                self.assertContains(response, 'class="auth-page sso-error-page"', status_code=400)
                self.assertNotIn('Location', response)
        self.assertFalse(SSOAttempt.objects.exists())
        self.assertNotIn('sso_browser', self.client.session)

    def test_invalid_public_base_never_redirects_or_starts_attempt(self):
        for base in ('http://pinry.example', 'https://evil.example/redirect', 'https://user@evil.example'):
            with self.subTest(base=base):
                SSOProvider.objects.filter(pk=self.provider.pk).update(public_base_url=base)
                response = Client(HTTP_HOST='192.168.0.45:2048').get(
                    f'/api/v2/sso/{self.provider.pk}/login/')
                self.assertEqual(response.status_code, 400)
                self.assertNotIn('Location', response)
        self.assertFalse(SSOAttempt.objects.exists())

    def test_logged_in_user_does_not_see_login_page_or_recovery_link(self):
        self.client.force_login(self.user)
        response = self.client.get('/login/')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/')
        response = self.client.get('/api/v2/sso/providers/', HTTP_HOST='192.168.0.45:2048',
                                   REMOTE_ADDR='127.0.0.1', HTTP_X_PINRY_DIRECT_PEER='192.168.0.20',
                                   HTTP_X_PINRY_PROXIED='0')
        self.assertNotIn('recovery_login_url', response.json())

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

    def test_verified_email_connects_existing_account_without_password(self):
        self.identity = SimpleNamespace(**dict(vars(self.identity), email_verified=True))
        response = self.callback(self.start())
        self.assertEqual(response['Location'], '/')
        self.assertEqual(self.client.session[SESSION_KEY], str(self.user.pk))
        self.assertEqual(ExternalIdentity.objects.get().user_id, self.user.pk)
        self.assertEqual(User.objects.count(), 1)

    def test_verified_email_duplicate_or_existing_provider_link_rejected(self):
        self.identity = SimpleNamespace(**dict(vars(self.identity), email_verified=True))
        other = User.objects.create_user('duplicate', 'same@example.com', 'password')
        self.callback(self.start())
        self.assertNotIn(SESSION_KEY, self.client.session)
        self.assertFalse(ExternalIdentity.objects.exists())
        other.delete()
        self.connect(subject='different-subject')
        self.callback(self.start())
        self.assertNotIn(SESSION_KEY, self.client.session)
        self.assertEqual(ExternalIdentity.objects.count(), 1)

    def prepare_signup(self, browser=None):
        self.provider.allow_signup = True
        self.provider.save()
        self.identity = VerifiedIdentity('https://idp.example', 'new-subject', 'new.person@example.com', '', True)
        return self.callback(self.start(client=browser, next='/profile/'), client=browser)

    def signup_data(self, **changes):
        return dict({'username': 'new.person', 'password1': 'Pinry-only-secret-49!',
                     'password2': 'Pinry-only-secret-49!',
                     'signup_token': SSOAttempt.objects.latest('pk').state_digest}, **changes)

    def test_old_signup_form_cannot_complete_another_tabs_identity(self):
        self.prepare_signup()
        old_form = self.signup_data()
        self.identity = VerifiedIdentity('https://idp.example', 'another-subject', 'another@example.com', '', True)
        self.callback(self.start())
        response = self.client.post('/api/v2/sso/signup/', old_form)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(User.objects.count(), 1)
        self.assertFalse(ExternalIdentity.objects.exists())
        self.assertEqual(self.client.post('/api/v2/sso/signup/', self.signup_data())['Location'], '/')
        self.assertEqual(ExternalIdentity.objects.get().subject, 'another-subject')

    def test_signup_waits_for_required_account_details_before_creating_user(self):
        response = self.prepare_signup()
        self.assertEqual(response['Location'], '/api/v2/sso/signup/')
        self.assertNotIn(SESSION_KEY, self.client.session)
        self.assertEqual(User.objects.count(), 1)
        self.assertFalse(ExternalIdentity.objects.exists())
        page = self.client.get(response['Location'])
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context['form']['username'].value(), 'new.person')
        self.assertContains(page, 'csrfmiddlewaretoken')
        self.assertEqual(page['Cache-Control'], 'no-store')

    def test_signup_creates_unprivileged_user_with_chosen_password(self):
        self.prepare_signup()
        response = self.client.post('/api/v2/sso/signup/', self.signup_data())
        self.assertEqual(response['Location'], '/profile/')
        user = User.objects.get(pk=self.client.session[SESSION_KEY])
        self.assertFalse(user.is_superuser or user.is_staff)
        self.assertEqual(user.username, 'new.person')
        self.assertTrue(user.check_password('Pinry-only-secret-49!'))
        self.assertEqual(user.email, 'new.person@example.com')
        self.assertEqual(self.client.session['auth_method'], 'sso')
        self.assertEqual(ExternalIdentity.objects.get().user_id, user.pk)
        self.assertNotEqual(user.pk, self.user.pk)
        self.client.logout()
        self.assertEqual(self.callback(self.start())['Location'], '/')
        self.assertEqual(self.client.session[SESSION_KEY], str(user.pk))
        self.assertEqual(User.objects.count(), 2)

    def test_signup_invalid_details_do_not_create_or_modify_accounts(self):
        self.prepare_signup()
        for changes, field in (({'password1': '', 'password2': ''}, 'password1'),
                               ({'password2': 'different'}, 'password2'),
                               ({'password1': '12345678', 'password2': '12345678'}, 'password2'),
                               ({'username': 'Member'}, 'username'),
                               ({'username': 'invalid/name'}, 'username')):
            with self.subTest(changes=changes):
                response = self.client.post('/api/v2/sso/signup/', self.signup_data(**changes))
                self.assertEqual(response.status_code, 400)
                self.assertIn(field, response.context['form'].errors)
                self.assertNotContains(response, 'value="Pinry-only-secret-49!"', status_code=400)
                self.assertNotIn(SESSION_KEY, self.client.session)
                self.assertEqual(User.objects.count(), 1)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('member-password'))
        self.assertEqual(self.client.post('/api/v2/sso/signup/', self.signup_data())['Location'], '/profile/')

    def test_sso_only_signup_password_works_only_after_local_login_enabled(self):
        AuthPolicy.objects.filter(pk=1).update(password_login_enabled=False)
        self.prepare_signup()
        response = self.client.post('/api/v2/sso/signup/', self.signup_data())
        self.assertEqual(response['Location'], '/profile/')
        user_id = self.client.session[SESSION_KEY]
        self.client.logout()
        credentials = {'username': 'new.person', 'password': 'Pinry-only-secret-49!'}
        self.assertEqual(self.client.post('/api/v2/profile/login/', credentials,
                                         content_type='application/json').status_code, 403)
        AuthPolicy.objects.filter(pk=1).update(password_login_enabled=True)
        SSOProvider.objects.filter(pk=self.provider.pk).update(enabled=False)
        self.assertEqual(self.client.post('/api/v2/profile/login/', credentials,
                                         content_type='application/json').status_code, 200)
        self.assertEqual(self.client.session[SESSION_KEY], user_id)

    def test_signup_requires_verified_browser_and_exact_route(self):
        self.prepare_signup()
        stranger = Client(HTTP_HOST='pinry.example')
        self.assertEqual(stranger.post('/api/v2/sso/signup/', self.signup_data()).status_code, 400)
        self.assertEqual(self.client.get('/api/v2/sso/signup/extra/').status_code, 403)
        self.assertEqual(self.client.post('/api/v2/sso/signup/', self.signup_data(),
                                         HTTP_HOST='192.168.0.45:2048').status_code, 400)
        self.assertEqual(User.objects.count(), 1)

    def test_signup_post_requires_csrf_and_cannot_be_replayed(self):
        browser = Client(enforce_csrf_checks=True, HTTP_HOST='pinry.example')
        self.prepare_signup(browser)
        self.assertEqual(browser.post('/api/v2/sso/signup/', self.signup_data()).status_code, 403)
        browser.get('/api/v2/sso/signup/')
        data = self.signup_data(csrfmiddlewaretoken=browser.cookies['csrftoken'].value)
        self.assertEqual(browser.post('/api/v2/sso/signup/', data).status_code, 302)
        self.assertEqual(browser.post('/api/v2/sso/signup/', data).status_code, 403)
        self.assertEqual(User.objects.count(), 2)
        self.assertEqual(ExternalIdentity.objects.count(), 1)

    def test_expired_signup_and_changed_provider_cannot_create_user(self):
        for change in ('expiry', 'revision', 'disabled', 'signup_disabled'):
            with self.subTest(change=change):
                self.provider.enabled = True
                self.provider.revision = 1
                self.prepare_signup()
                if change == 'expiry':
                    SSOAttempt.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
                else:
                    values = {'revision': {'revision': 2}, 'disabled': {'enabled': False},
                              'signup_disabled': {'allow_signup': False}}
                    SSOProvider.objects.filter(pk=self.provider.pk).update(**values[change])
                self.assertEqual(self.client.post('/api/v2/sso/signup/', self.signup_data()).status_code, 400)
                self.assertEqual(User.objects.count(), 1)

    def test_signup_cannot_overwrite_account_created_while_form_was_open(self):
        self.prepare_signup()
        existing = User.objects.create_user('other-name', 'new.person@example.com', 'existing-password')
        response = self.client.post('/api/v2/sso/signup/', self.signup_data())
        self.assertEqual(response.status_code, 400)
        existing.refresh_from_db()
        self.assertTrue(existing.check_password('existing-password'))
        self.assertFalse(ExternalIdentity.objects.exists())
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_unverified_signup_email_is_not_saved_for_future_automatic_linking(self):
        self.provider.allow_signup = True
        self.provider.save()
        self.identity = VerifiedIdentity('https://idp.example', 'new-subject', 'claimed@example.com', '', False)
        self.callback(self.start())
        self.client.post('/api/v2/sso/signup/', self.signup_data())
        user = User.objects.get(pk=self.client.session[SESSION_KEY])
        self.assertEqual(user.email, '')

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
        self.assertContains(response, '재인증', status_code=403)
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
        self.callback(self.start())
        AuthPolicy.objects.filter(pk=1).update(password_login_enabled=False)
        response = self.client.post(f'/api/v2/sso/identities/{identity.pk}/unlink/')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.session['recent_auth']['method'], 'sso')
        self.assertTrue(ExternalIdentity.objects.filter(pk=identity.pk).exists())

    def test_password_reauth_through_login_keeps_existing_sso_session_method(self):
        self.connect()
        self.callback(self.start())
        response = self.client.post('/api/v2/profile/login/', {
            'username': self.user.username, 'password': 'member-password',
        }, content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.session['auth_method'], 'sso')
        self.assertEqual(self.client.session['recent_auth']['method'], 'password')

    def test_old_issuer_identity_is_not_an_alternative_login_method(self):
        identity = self.connect()
        other = SSOProvider.objects.create(kind='oidc', name='변경됨', enabled=True,
                                           issuer='https://changed.example', client_id='client',
                                           public_base_url='https://pinry.example')
        self.connect(subject='other', provider=other)
        self.callback(self.start())
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

    def test_unlink_reports_last_login_method_separately_from_recent_auth(self):
        identity = self.connect()
        self.callback(self.start())
        AuthPolicy.objects.filter(pk=1).update(password_login_enabled=False)
        listing = self.client.get('/api/v2/sso/identities/').json()[0]
        self.assertFalse(listing['unlink_allowed'])
        self.assertEqual(listing['unlink_reason'], 'last_login_method')
        response = self.client.post(f'/api/v2/sso/identities/{identity.pk}/unlink/')
        self.assertEqual(response.json()['code'], 'last_login_method')
        AuthPolicy.objects.filter(pk=1).update(password_login_enabled=True)
        session = self.client.session
        session.pop('recent_auth')
        session.save()
        self.assertTrue(self.client.get('/api/v2/sso/identities/').json()[0]['unlink_allowed'])
        response = self.client.post(f'/api/v2/sso/identities/{identity.pk}/unlink/')
        self.assertEqual(response.json()['code'], 'recent_auth_required')
        self.assertTrue(ExternalIdentity.objects.filter(pk=identity.pk).exists())

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
        self.callback(state)
        with patch('users.sso.flows.ExternalIdentity.objects.create', side_effect=IntegrityError):
            response = self.client.post('/api/v2/sso/signup/', self.signup_data())
        self.assertEqual(response.status_code, 400)
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
        for host in ('pinry.example', '192.168.0.45:2048'):
            for next_path, expected in (('/profile/', '/profile/'), ('//evil.example', '/')):
                with self.subTest(host=host, next_path=next_path):
                    response = self.client.get(f'/api/v2/sso/{self.provider.pk}/login/',
                                                {'next': next_path}, HTTP_HOST=host)
                    self.assertEqual(response.status_code, 302)
                    self.assertEqual(response['Location'], expected)
                    self.assertEqual(self.client.session[SESSION_KEY], str(self.user.pk))
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
                return finish_attempt(callback_request, provider, state, 'code')
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
                pending_user = first.result(timeout=10)
        self.assertEqual(network_transactions, [False])
        self.assertIsNone(pending_user)
        self.assertFalse(ExternalIdentity.objects.exists())
        self.assertEqual(AuthVerification.objects.count(), 0)
