import base64
from unittest.mock import patch

from django.test import TestCase, RequestFactory, override_settings
from django.db import DatabaseError, connection
from django.test.utils import CaptureQueriesContext
from django.core.exceptions import ValidationError
from rest_framework.authtoken.models import Token

from users.models import AuthPolicy, AuthVerification, ExternalIdentity, SSOProvider, User, create_token_if_necessary


class SSOPolicyTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser('owner', 'owner@example.com', 'test-password')

    def disable_password(self):
        AuthPolicy.objects.filter(pk=1).update(password_login_enabled=False)

    def test_disabled_password_routes_do_not_create_session(self):
        self.disable_password()
        for path in ['/api/v2/profile/login/', '/admin/login/', '/api-auth/login/']:
            with self.subTest(path=path):
                data = {'username': 'owner', 'password': 'test-password'}
                response = self.client.post(path, data, content_type=(
                    'application/json' if '/profile/' in path else 'application/x-www-form-urlencoded'
                ))
                self.assertEqual(response.status_code, 403)
                self.assertNotIn('_auth_user_id', self.client.session)

    def test_signup_cannot_create_password_session(self):
        self.disable_password()
        response = self.client.post('/api/v2/profile/users/', {
            'username': 'new-user', 'password': 'test-password',
            'password_repeat': 'test-password', 'email': 'new@example.com',
        }, content_type='application/json')
        self.assertEqual(response.status_code, 403)
        self.assertFalse(User.objects.filter(username='new-user').exists())

    def test_legacy_password_session_is_logged_out(self):
        self.client.force_login(self.user)
        self.disable_password()
        response = self.client.get('/api/v2/profile/users/')
        self.assertEqual(response.json(), [])
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_basic_auth_is_rejected(self):
        self.disable_password()
        encoded = base64.b64encode(b'owner:test-password').decode()
        response = self.client.get('/api/v2/profile/users/', HTTP_AUTHORIZATION='Basic ' + encoded)
        self.assertIn(response.status_code, (401, 403))

    def test_disabled_token_is_not_exposed_created_or_authenticated(self):
        token = Token.objects.get(user=self.user)
        AuthPolicy.objects.filter(pk=1).update(api_tokens_enabled=False)
        self.client.force_login(self.user)
        response = self.client.get('/api/v2/profile/users/')
        self.assertIsNone(response.json()[0]['token'])
        self.client.logout()
        for public in (True, False):
            with override_settings(PUBLIC=public):
                response = self.client.get('/api/v2/pins/', HTTP_AUTHORIZATION='Token ' + token.key)
                self.assertIn(response.status_code, (401, 403))
        self.assertTrue(Token.objects.filter(pk=token.pk).exists())
        fresh = User.objects.create_user('fresh', password='test-password')
        self.assertFalse(Token.objects.filter(user=fresh).exists())
        self.assertIsNone(create_token_if_necessary(fresh))

    def test_recovery_session_is_rejected_on_public_app(self):
        self.client.force_login(self.user)
        session = self.client.session
        session['auth_method'] = 'recovery'
        session.save()
        self.assertEqual(self.client.get('/api/v2/profile/users/').json(), [])
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_token_header_whitespace_cannot_bypass_policy(self):
        token = Token.objects.get(user=self.user)
        AuthPolicy.objects.filter(pk=1).update(api_tokens_enabled=False)
        response = self.client.get('/api/v2/profile/users/', HTTP_AUTHORIZATION='Token\t' + token.key)
        self.assertIn(response.status_code, (401, 403))

    def test_login_cannot_switch_authenticated_account(self):
        self.client.force_login(self.user)
        other = User.objects.create_user('other', password='test-password')
        response = self.client.post('/api/v2/profile/login/', {
            'username': other.username, 'password': 'test-password',
        }, content_type='application/json')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.session['_auth_user_id'], str(self.user.pk))

    def test_provider_response_includes_policy_without_private_fields(self):
        response = self.client.get('/api/v2/sso/providers/')
        self.assertEqual(response.json(), {
            'providers': [], 'password_login_enabled': True, 'api_tokens_enabled': True,
        })

    def test_login_error_destination_exists(self):
        response = self.client.get('/login/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '로그인')

    def test_provider_icons_use_kind_not_custom_name_and_expose_no_secrets(self):
        providers = [SSOProvider.objects.create(
            kind=kind, name='회사 계정', enabled=True, position=index,
            client_id='private-client', encrypted_client_secret='private-secret',
        ) for index, kind in enumerate(('authentik', 'synology', 'google', 'microsoft', 'github', 'oidc'))]
        SSOProvider.objects.create(kind='google', name='숨김', enabled=False)
        payload = self.client.get('/api/v2/sso/providers/').json()['providers']
        self.assertEqual(payload, [
            {'id': str(provider.pk), 'name': '회사 계정', 'kind': kind,
             'login_url': f'/api/v2/sso/{provider.pk}/login/'}
            for provider, kind in zip(providers, ('authentik', 'synology', 'google', 'microsoft', 'github', 'oidc'))
        ])

    def test_missing_policy_and_database_failure_never_enable_password(self):
        from users.sso.policy import password_login_allowed, api_token_allowed
        AuthPolicy.objects.all().delete()
        request = RequestFactory().get('/')
        self.assertFalse(password_login_allowed(request))
        self.assertFalse(api_token_allowed(request))
        with patch('users.sso.policy.read_policy', side_effect=DatabaseError):
            self.assertFalse(password_login_allowed(RequestFactory().get('/')))

    def test_policy_is_read_once_per_request(self):
        from users.sso.policy import password_login_allowed, api_token_allowed
        request = RequestFactory().get('/')
        with self.assertNumQueries(1):
            self.assertTrue(password_login_allowed(request))
            self.assertTrue(api_token_allowed(request))

    def test_login_signal_reuses_request_policy_snapshot(self):
        with CaptureQueriesContext(connection) as queries:
            response = self.client.post('/api/v2/profile/login/', {
                'username': 'owner', 'password': 'test-password',
            }, content_type='application/json')
        self.assertEqual(response.status_code, 200)
        policy_reads = [query for query in queries if 'FROM "users_authpolicy"' in query['sql']]
        self.assertEqual(len(policy_reads), 1)

    def test_admin_form_has_write_only_secret_callback_and_stale_revision_guard(self):
        from users.models import SSOProvider
        provider = SSOProvider.objects.create(kind='google', name='Google', public_base_url='https://pinry.example')
        self.client.force_login(self.user)
        url = f'/admin/users/ssoprovider/{provider.pk}/change/'
        response = self.client.get(url)
        self.assertContains(response, 'name="client_secret"')
        self.assertContains(response, f'https://pinry.example/api/v2/sso/{provider.pk}/callback/')
        self.assertContains(response, 'name="expected_revision"')

    def test_admin_login_shows_sso_policy(self):
        self.disable_password()
        response = self.client.get('/admin/login/')
        self.assertContains(response, 'SSO')
        self.assertNotContains(response, 'name="password"')

    def test_stale_admin_form_cannot_overwrite_newer_policy(self):
        self.client.force_login(self.user)
        AuthPolicy.objects.filter(pk=1).update(revision=2, api_tokens_enabled=False)
        response = self.client.post('/admin/users/authpolicy/1/change/', {
            'password_login_enabled': 'on', 'api_tokens_enabled': 'on',
            'recovery_allowed_cidrs': '[]', 'recovery_denied_cidrs': '[]', 'expected_revision': 1,
        }, follow=True)
        self.assertContains(response, '인증 정책이 변경되었습니다')
        self.assertFalse(AuthPolicy.objects.get(pk=1).api_tokens_enabled)

    def test_callback_error_consumed_at_real_login_page_without_query_secrets(self):
        provider = SSOProvider.objects.create(kind='google', name='Google', enabled=True)
        response = self.client.get(f'/api/v2/sso/{provider.pk}/callback/?code=hidden-code&state=hidden-state', follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'SSO 인증을 완료하지 못했습니다')
        self.assertContains(response, f'/api/v2/sso/{provider.pk}/login/')
        self.assertNotContains(response, 'hidden-code')
        self.assertNotContains(response, 'hidden-state')


class SSOOnlyProofTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser('owner', 'owner@example.com', 'test-password')
        self.provider = SSOProvider.objects.create(
            kind='google', name='Google', enabled=True, public_base_url='https://pinry.example',
            client_id='client', encrypted_client_secret='protected',
            allowed_endpoint_origins=['https://accounts.google.com'],
        )
        ExternalIdentity.objects.create(user=self.user, provider=self.provider,
                                        issuer='https://accounts.google.com', subject='subject')
        self.sso = AuthVerification.objects.create(user=self.user, kind='sso', provider=self.provider,
                                                   provider_revision=1, policy_revision=1)
        self.recovery = AuthVerification.objects.create(user=self.user, kind='recovery',
                                                        policy_revision=1, deployment_fingerprint='deployment-a')

    def switch(self, **kwargs):
        from users.sso.config import save_configuration
        return save_configuration(self.user, {'password_login_enabled': False}, **kwargs)

    def test_verified_admin_sso_does_not_require_separate_recovery_setup(self):
        self.recovery.delete()
        self.assertFalse(self.switch().password_login_enabled)

    def test_current_sso_and_recent_current_deployment_proof_allow_transition(self):
        with patch('users.sso.config.current_recovery_fingerprint', return_value='deployment-a'):
            self.assertFalse(self.switch().password_login_enabled)

    def test_stale_provider_evidence_is_rejected(self):
        self.provider.revision = 2
        self.provider.save()
        with self.assertRaises(ValidationError):
            self.switch()

    def test_changing_provider_and_disabling_password_cannot_reuse_old_proof(self):
        with patch('users.sso.config.current_recovery_fingerprint', return_value='deployment-a'):
            with self.assertRaises(ValidationError):
                self.switch(provider_changes={'id': self.provider.pk, 'client_id': 'changed'})
        self.provider.refresh_from_db()
        self.assertEqual(self.provider.client_id, 'client')

    def test_last_enabled_provider_cannot_be_disabled(self):
        from users.sso.config import save_configuration
        AuthPolicy.objects.filter(pk=1).update(password_login_enabled=False)
        with self.assertRaises(ValidationError):
            save_configuration(self.user, {}, {'id': self.provider.pk, 'enabled': False})

    def test_invalid_origin_is_reported_as_validation_error(self):
        from users.sso.config import save_configuration
        for origin in (3, 'https://['):
            with self.subTest(origin=origin), self.assertRaises(ValidationError):
                save_configuration(self.user, {}, {'id': self.provider.pk, 'allowed_endpoint_origins': [origin]})

    def test_admin_reports_malformed_origin_without_server_error(self):
        import json
        self.client.force_login(self.user)
        response = self.client.post(f'/admin/users/ssoprovider/{self.provider.pk}/change/', {
            'name': 'Google', 'position': 0, 'enabled': 'on', 'public_base_url': 'https://pinry.example',
            'client_id': 'client', 'allowed_endpoint_origins': json.dumps(['https://[']),
            'internal_cidrs': '[]', 'expected_revision': 1, 'expected_policy_revision': 1,
        }, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('allowed_endpoint_origins', response.context['adminform'].form.errors)
        self.provider.refresh_from_db()
        self.assertEqual(self.provider.allowed_endpoint_origins, ['https://accounts.google.com'])
