import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.core.management import call_command, CommandError
from django.db import DatabaseError, close_old_connections
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token

from users.models import AuthPolicy, AuthVerification, AuthenticationThrottle, User
from users.recovery import recovery_peer_allowed
from users.sso.config import current_recovery_fingerprint


RECOVERY_SETTINGS = dict(
    ROOT_URLCONF='users.recovery_urls',
    MIDDLEWARE=[
        'django.contrib.sessions.middleware.SessionMiddleware',
        'django.contrib.auth.middleware.AuthenticationMiddleware',
        'users.recovery.RecoveryBoundaryMiddleware',
        'django.middleware.csrf.CsrfViewMiddleware',
    ],
    AUTHENTICATION_BACKENDS=['users.recovery.RecoveryBackend'],
    SESSION_COOKIE_NAME='pinry_recovery_session',
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_AGE=900,
    CSRF_COOKIE_NAME='pinry_recovery_csrf',
    CSRF_COOKIE_SECURE=True,
    USE_X_FORWARDED_HOST=False,
    SECURE_PROXY_SSL_HEADER=('HTTP_X_PINRY_RECOVERY_TLS', 'https'),
)


class RecoveryPeerTests(SimpleTestCase):
    def test_only_explicit_private_ranges_and_deny_precedence(self):
        for address, allow, deny, expected in [
            ('192.168.0.45', ['192.168.0.0/24'], ['192.168.0.45/32'], False),
            ('192.168.0.28', ['192.168.0.0/24'], ['192.168.0.45/32'], True),
            ('10.21.4.8', ['10.21.0.0/16'], [], True),
            ('fd12::3', ['fd12::/64'], [], True),
            ('127.0.0.1', ['127.0.0.0/8'], [], False),
            ('192.168.0.28', [], [], False),
            ('192.168.0.28', ['0.0.0.0/0'], [], False),
            ('192.168.0.28', ['192.168.0.0/24'], ['bad'], False),
            ('::ffff:192.168.0.28', ['192.168.0.0/24'], [], False),
            ('not-ip', ['192.168.0.0/24'], [], False),
        ]:
            with self.subTest(address=address, allow=allow, deny=deny):
                self.assertEqual(recovery_peer_allowed(address, allow, deny), expected)


class RecoveryDeploymentTests(SimpleTestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.cert = str(Path(self.directory) / 'cert.pem')
        self.key = str(Path(self.directory) / 'key.pem')
        subprocess.run([
            'openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
            '-keyout', self.key, '-out', self.cert, '-days', '1',
            '-subj', '/CN=recovery.example',
        ], check=True, capture_output=True)
        self.environ = dict(
            PINRY_RECOVERY_ENABLED='true',
            PINRY_RECOVERY_ORIGIN='https://recovery.example:9443',
            PINRY_RECOVERY_CERT_FILE=self.cert,
            PINRY_RECOVERY_KEY_FILE=self.key,
        )
        self.enterContext(patch.dict(os.environ, self.environ))

    def test_verified_deployment_has_fingerprint_invalidated_by_origin_and_certificate(self):
        original = current_recovery_fingerprint()
        self.assertTrue(original)
        with patch.dict(os.environ, PINRY_RECOVERY_ORIGIN='https://recovery.example:9444'):
            self.assertNotEqual(current_recovery_fingerprint(), original)
        Path(self.cert).write_text('invalid certificate')
        self.assertIsNone(current_recovery_fingerprint())

    def test_disabled_or_invalid_deployment_fails_closed(self):
        for change in [
            {'PINRY_RECOVERY_ENABLED': 'false'},
            {'PINRY_RECOVERY_ORIGIN': 'http://recovery.example:9443'},
            {'PINRY_RECOVERY_ORIGIN': 'https://recovery.example:9443/path'},
            {'PINRY_RECOVERY_ORIGIN': 'https://recovery.example:9443;bad'},
            {'PINRY_RECOVERY_KEY_FILE': '/missing/key.pem'},
        ]:
            with self.subTest(change=change), patch.dict(os.environ, change):
                self.assertIsNone(current_recovery_fingerprint())

    def test_explicit_default_https_port_uses_browser_origin_and_host(self):
        from pinry.recovery_config import deployment_configuration
        with patch.dict(os.environ, PINRY_RECOVERY_ORIGIN='https://recovery.example:443'):
            deployment = deployment_configuration()
        self.assertEqual(deployment['origin'], 'https://recovery.example')
        self.assertEqual(deployment['host'], 'recovery.example')
        with patch.dict(os.environ, PINRY_RECOVERY_ORIGIN='https://recovery.example'):
            self.assertEqual(current_recovery_fingerprint(), deployment['fingerprint'])
        self.assertNotEqual(current_recovery_fingerprint(), deployment['fingerprint'])

    def test_uppercase_hostname_uses_browser_origin_and_host(self):
        from pinry.recovery_config import deployment_configuration
        with patch.dict(os.environ, PINRY_RECOVERY_ORIGIN='https://RECOVERY.example:9443'):
            deployment = deployment_configuration()
        self.assertEqual(deployment['origin'], 'https://recovery.example:9443')
        self.assertEqual(deployment['host'], 'recovery.example:9443')
        self.assertEqual(current_recovery_fingerprint(), deployment['fingerprint'])

    def test_key_path_change_invalidates_proof_and_service_cannot_read_owner_only_key(self):
        from pinry.recovery_config import deployment_configuration
        original = current_recovery_fingerprint()
        replacement = str(Path(self.directory) / 'replacement.pem')
        Path(replacement).write_bytes(Path(self.key).read_bytes())
        os.chmod(replacement, 0o600)
        with patch.dict(os.environ, PINRY_RECOVERY_KEY_FILE=replacement):
            self.assertNotEqual(current_recovery_fingerprint(), original)
        self.assertIsNone(deployment_configuration(service_identity=(65534, 65534)))

    def test_world_readable_private_key_is_rejected(self):
        os.chmod(self.key, 0o644)
        self.assertIsNone(current_recovery_fingerprint())

    def test_encrypted_key_does_not_prompt_during_service_startup(self):
        encrypted = subprocess.run([
            'openssl', 'pkey', '-in', self.key, '-aes256', '-passout', 'pass:synthetic-test',
        ], check=True, capture_output=True)
        Path(self.key).write_bytes(encrypted.stdout)
        result = subprocess.run([
            sys.executable, '-c',
            'from pinry.recovery_config import deployment_configuration; print(deployment_configuration())',
        ], input=b'', capture_output=True, timeout=5)
        self.assertEqual(result.stdout.strip(), b'None')
        self.assertEqual(result.stderr, b'')


@override_settings(**RECOVERY_SETTINGS)
class RecoveryRequestTests(TestCase):
    def setUp(self):
        self.policy = AuthPolicy.objects.get(pk=1)
        self.policy.recovery_allowed_cidrs = ['192.168.0.0/24']
        self.policy.recovery_denied_cidrs = ['192.168.0.45/32']
        self.policy.password_login_enabled = False
        self.policy.api_tokens_enabled = False
        self.policy.save()
        self.admin = User.objects.create_superuser('administrator', 'admin@example.test', 'secret-pass')
        self.enterContext(patch('users.recovery.deployment_configuration', return_value={
            'origin': 'https://recovery.example:9443', 'host': 'recovery.example:9443',
            'hostname': 'recovery.example', 'fingerprint': 'deployment-v1',
        }))
        self.client = Client(enforce_csrf_checks=True)
        self.headers = dict(
            HTTP_HOST='recovery.example:9443',
            HTTP_X_PINRY_RECOVERY_PEER='192.168.0.28',
            HTTP_X_PINRY_RECOVERY_TLS='https',
        )
        self.headers['pinry.recovery_listener'] = True

    def get(self, path='/recovery/login/', **headers):
        return self.client.get(path, **(self.headers | headers))

    def post(self, path='/recovery/login/', data=None, **headers):
        token = self.client.cookies.get(settings.CSRF_COOKIE_NAME)
        return self.client.post(path, data or {'username': 'administrator', 'password': 'secret-pass'},
                                **(self.headers | {
                                    'HTTP_ORIGIN': 'https://recovery.example:9443',
                                    'HTTP_X_CSRFTOKEN': token.value if token else '',
                                } | headers))

    def login(self):
        self.assertEqual(self.get().status_code, 200)
        return self.post()

    def test_recovery_pages_declare_mobile_viewport(self):
        self.assertContains(
            self.get(),
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            html=True,
        )
        self.assertEqual(self.login().status_code, 302)
        self.assertContains(
            self.get('/recovery/settings/'),
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            html=True,
        )

    def test_recovery_origin_host_works_when_public_hosts_are_restricted(self):
        public_settings = types.ModuleType('pinry.settings.docker')
        public_settings.ALLOWED_HOSTS = ['public.example']
        spec = importlib.util.spec_from_file_location(
            'pinry.settings.recovery_review', Path(__file__).resolve().parents[1] / 'pinry/settings/recovery.py',
        )
        recovery_settings = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'pinry.settings.docker': public_settings}), patch(
            'pinry.recovery_config.deployment_configuration', return_value={
                'hostname': 'recovery.example', 'host': 'recovery.example:9443',
                'origin': 'https://recovery.example:9443', 'fingerprint': 'deployment-v1',
            },
        ):
            spec.loader.exec_module(recovery_settings)
        with override_settings(ALLOWED_HOSTS=recovery_settings.ALLOWED_HOSTS):
            self.assertEqual(self.get().status_code, 200)
            self.assertIn(self.get(HTTP_HOST='public.example:9443').status_code, (400, 403))
            self.assertIn(self.get(HTTP_HOST='unrelated.example:9443').status_code, (400, 403))
            self.assertEqual(self.get(HTTP_HOST='recovery.example:9444').status_code, 403)
        self.assertEqual(public_settings.ALLOWED_HOSTS, ['public.example'])

    @override_settings(LANGUAGE_CODE='en-us', DEBUG=False)
    def test_default_form_validation_errors_are_korean(self):
        self.login()
        for revision, message in [('bad', '정수를 입력하세요.'), ('', '이 필드는 필수 항목입니다.')]:
            with self.subTest(revision=revision):
                response = self.post('/recovery/settings/', {'revision': revision})
                self.assertContains(response, message, status_code=400)

    @override_settings(LANGUAGE_CODE='en-us', DEBUG=False)
    def test_csrf_failure_guidance_is_korean(self):
        self.get()
        response = self.post(HTTP_X_CSRFTOKEN='')
        self.assertContains(response, 'CSRF 검증에 실패했습니다.', status_code=403)
        self.assertFalse(AuthVerification.objects.exists())

    def test_active_superuser_session_and_current_policy_proof_without_token(self):
        self.assertEqual(self.login().status_code, 302)
        session = self.client.session
        self.assertEqual(session['auth_method'], 'recovery')
        self.assertEqual(session.get_expiry_age(), 900)
        self.assertTrue(self.client.cookies['pinry_recovery_session']['secure'])
        self.assertFalse(Token.objects.filter(user=self.admin).exists())
        proof = AuthVerification.objects.get(user=self.admin, kind='recovery')
        self.assertEqual((proof.policy_revision, proof.deployment_fingerprint), (1, 'deployment-v1'))
        response = self.get('/recovery/settings/')
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'secret-pass')

    def test_wrong_host_tls_origin_and_untrusted_forwarding_are_denied(self):
        for headers in [
            {'HTTP_HOST': 'public.example:9443'}, {'HTTP_X_PINRY_RECOVERY_TLS': ''},
            {'HTTP_ORIGIN': 'https://public.example'},
            {'HTTP_X_PINRY_RECOVERY_PEER': '203.0.113.1', 'HTTP_X_FORWARDED_FOR': '192.168.0.28'},
            {'HTTP_X_PINRY_RECOVERY_PEER': '192.168.0.45'},
            {'HTTP_X_PINRY_RECOVERY_PEER': '', 'HTTP_X_REAL_IP': '192.168.0.28'},
        ]:
            with self.subTest(headers=headers):
                self.assertEqual(self.get(**headers).status_code, 403)
        self.assertEqual(self.get(**{'pinry.recovery_listener': False,
                                     'HTTP_PINRY_RECOVERY_LISTENER': 'true'}).status_code, 403)

    def test_recovery_login_never_creates_token_even_when_public_tokens_allowed(self):
        self.policy.api_tokens_enabled = True
        self.policy.save()
        other = User.objects.create_superuser('second', 'second@example.test', 'secret-pass')
        Token.objects.all().delete()
        self.login()
        self.assertEqual(self.post(data={'username': other.username, 'password': 'secret-pass'}).status_code, 302)
        self.assertFalse(Token.objects.exists())

    def test_post_requires_both_origin_and_csrf(self):
        self.get()
        for headers in [{'HTTP_ORIGIN': ''}, {'HTTP_ORIGIN': 'https://public.example'}, {'HTTP_X_CSRFTOKEN': ''}]:
            with self.subTest(headers=headers):
                self.assertEqual(self.post(**headers).status_code, 403)
        self.assertFalse(AuthVerification.objects.exists())

    def test_staff_inactive_and_unknown_accounts_have_same_login_error(self):
        User.objects.create_user('staff', password='secret-pass', is_staff=True)
        User.objects.create_superuser('inactive', 'inactive@example.test', 'secret-pass', is_active=False)
        self.get()
        responses = [self.post(data={'username': name, 'password': 'secret-pass'})
                     for name in ['staff', 'inactive', 'missing']]
        self.assertEqual([response.status_code for response in responses], [403, 403, 403])
        for response in responses:
            self.assertContains(response, '로그인할 수 없습니다.', status_code=403)
        self.assertFalse(AuthVerification.objects.exists())

    def test_cidr_and_active_status_are_rechecked_on_every_session_request(self):
        self.login()
        self.policy.recovery_denied_cidrs.append('192.168.0.28/32')
        self.policy.save()
        self.assertEqual(self.get('/recovery/settings/').status_code, 403)
        self.policy.recovery_denied_cidrs = []
        self.policy.save()
        self.admin.is_active = False
        self.admin.save()
        self.assertNotEqual(self.get('/recovery/settings/').status_code, 200)

    def test_recovery_routes_exclude_public_apis_and_callbacks(self):
        self.login()
        for path in ['/api/v2/pins/', '/api/v2/sso/callback/', '/admin/']:
            self.assertEqual(self.get(path).status_code, 404)

    def test_public_port_rejects_recovery_cookie_even_when_renamed(self):
        self.login()
        from pinry.settings.base import MIDDLEWARE, AUTHENTICATION_BACKENDS
        with override_settings(ROOT_URLCONF='pinry.urls', MIDDLEWARE=MIDDLEWARE,
                               AUTHENTICATION_BACKENDS=AUTHENTICATION_BACKENDS,
                               SESSION_COOKIE_NAME='sessionid', PUBLIC=False):
            public = Client()
            public.cookies['sessionid'] = self.client.cookies['pinry_recovery_session'].value
            self.assertEqual(public.get('/api/v2/pins/').status_code, 403)
            for path in ['/recovery/', '/recovery/login/', '/recovery/settings/']:
                self.assertEqual(public.get(path, **self.headers).status_code, 404)

    def test_peer_and_account_limits_block_sixth_attempt_then_expire(self):
        self.get()
        for count in range(5):
            self.assertEqual(self.post(data={'username': 'administrator', 'password': 'wrong'}).status_code, 403)
        self.assertEqual(self.post().status_code, 429)
        self.assertEqual(self.post(HTTP_X_PINRY_RECOVERY_PEER='192.168.0.29').status_code, 429)
        self.assertEqual(self.post(data={'username': 'other', 'password': 'anything'}).status_code, 429)
        self.assertEqual(AuthenticationThrottle.objects.filter(failure_count=5).count(), 2)
        self.assertFalse(AuthenticationThrottle.objects.filter(key_digest__contains='administrator').exists())
        past = timezone.now() - timedelta(minutes=6)
        AuthenticationThrottle.objects.update(window_started_at=past, blocked_until=past)
        self.assertEqual(self.post().status_code, 302)

    def test_policy_changes_are_post_only_and_use_revision_guard(self):
        self.login()
        self.assertEqual(self.get('/recovery/settings/?enable-password-login=1').status_code, 200)
        self.policy.refresh_from_db()
        self.assertFalse(self.policy.password_login_enabled)
        response = self.post('/recovery/settings/', {'revision': 1, 'password_login_enabled': 'on',
                             'recovery_allowed_cidrs': '192.168.0.0/24', 'recovery_denied_cidrs': '192.168.0.45/32'})
        self.assertEqual(response.status_code, 302)
        self.policy.refresh_from_db()
        self.assertTrue(self.policy.password_login_enabled)
        self.assertFalse(self.policy.api_tokens_enabled)
        self.assertEqual(self.policy.revision, 2)
        self.assertEqual(self.post('/recovery/settings/', {'revision': 1}).status_code, 400)


class RecoveryCommandTests(TestCase):
    def run_command(self, *args, confirmation='YES\n'):
        output = io.StringIO()
        with patch('sys.stdin', io.StringIO(confirmation)):
            call_command('auth_recover', *args, stdout=output)
        return output.getvalue()

    def test_confirmation_and_explicit_options_only(self):
        policy = AuthPolicy.objects.get(pk=1)
        policy.password_login_enabled = False
        policy.api_tokens_enabled = False
        policy.save()
        self.run_command('--allow-cidr', '10.12.0.0/16', confirmation='no\n')
        policy.refresh_from_db()
        self.assertEqual(policy.recovery_allowed_cidrs, [])
        self.run_command('--allow-cidr', '10.12.0.0/16')
        policy.refresh_from_db()
        self.assertEqual(policy.recovery_allowed_cidrs, ['10.12.0.0/16'])
        self.assertFalse(policy.password_login_enabled)
        self.run_command('--enable-password-login')
        policy.refresh_from_db()
        self.assertTrue(policy.password_login_enabled)
        self.assertFalse(policy.api_tokens_enabled)
        self.assertEqual(policy.revision, 3)

    def test_invalid_or_missing_options_never_write(self):
        for args in [[], ['--allow-cidr', '0.0.0.0/0'], ['--allow-cidr', 'bad'], ['--allow-cidr', '8.8.8.0/24']]:
            with self.subTest(args=args), self.assertRaises(CommandError):
                self.run_command(*args)
        self.assertEqual(AuthPolicy.objects.get(pk=1).revision, 1)


@override_settings(AUTHENTICATION_BACKENDS=['users.recovery.RecoveryBackend'])
class RecoveryConcurrentThrottleTests(TransactionTestCase):
    def test_workers_do_not_lose_failures_or_exceed_failure_budget(self):
        from users.recovery import _limited_authenticate
        AuthPolicy.objects.get_or_create(pk=1)
        User.objects.create_superuser('administrator', 'admin@example.test', 'secret-pass')

        def attempt(_index):
            close_old_connections()
            request = RequestFactory().post('/recovery/login/')
            request.recovery_deployment = {'fingerprint': 'test'}
            request.recovery_peer = '10.1.2.3'
            try:
                user, limited = _limited_authenticate(request, 'administrator', 'wrong')
                return 429 if limited else 403
            except DatabaseError:
                return 503
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=8) as workers:
            results = list(workers.map(attempt, range(8)))
        failures = results.count(403)
        self.assertGreater(failures, 0)
        self.assertLessEqual(failures, 5)
        self.assertEqual(list(AuthenticationThrottle.objects.values_list('failure_count', flat=True)),
                         [failures, failures])
