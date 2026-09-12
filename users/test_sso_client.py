import importlib
import os
import socket
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase
from joserfc import jwt
from joserfc.jwk import OctKey, RSAKey

from users.models import SSOAttempt, SSOProvider
from users.sso_test_utils import SyntheticProvider, tls_server


PUBLIC_ADDRESS = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))]


class SSOClientTests(SimpleTestCase):
    def setUp(self):
        self.client_module = importlib.import_module('users.sso.client')
        self.fixture = self.enterContext(SyntheticProvider().serve())
        self.enterContext(patch('socket.getaddrinfo', return_value=PUBLIC_ADDRESS))
        self.enterContext(patch(
            'users.sso.client.request_json', side_effect=self.fixture.request_json,
        ))
        self.provider = SSOProvider(
            kind='oidc', issuer=self.fixture.issuer, client_id='pinry-client',
            allowed_endpoint_origins=[self.fixture.issuer],
        )

    def exchange(self):
        return self.client_module.exchange_identity(
            self.provider, 'https://pinry.example/callback', 'one-use-code',
            'one-use-nonce', 'a' * 64,
        )

    def test_signed_identity_and_optional_email(self):
        identity = self.exchange()
        self.assertEqual(identity.subject, 'external-123')
        self.assertEqual(identity.issuer, 'https://idp.example')
        self.assertIsNone(identity.email)
        self.assertEqual(identity.display_name, '합성 사용자')
        token_request = next(body for path, body in self.fixture.requests if path == '/token')
        self.assertEqual(token_request['code_verifier'], ['a' * 64])
        self.assertEqual(token_request['redirect_uri'], ['https://pinry.example/callback'])

    def test_email_verification_requires_signed_boolean_true(self):
        self.fixture.claims['email'] = 'member@example.com'
        for value in (True, False, 'true', 1, None):
            with self.subTest(value=value):
                self.fixture.claims['email_verified'] = value
                self.assertEqual(self.exchange().email_verified, value is True)

    def test_authentik_hs256_uses_client_secret_and_rejects_wrong_signature(self):
        self.fixture.metadata['id_token_signing_alg_values_supported'] = ['HS256']
        self.provider.encrypted_client_secret = 'encrypted-placeholder'
        secret = 'synthetic-client-secret-at-least-32-bytes'
        token = {'id_token': jwt.encode({'alg': 'HS256'}, self.fixture.claims, OctKey.import_key(secret.encode()))}
        with patch('users.sso.client.decrypt_secret', return_value=secret):
            identity = self.client_module._oidc_identity(self.provider, self.fixture.metadata, token, 'one-use-nonce')
            self.assertEqual(identity.subject, 'external-123')
        with patch('users.sso.client.decrypt_secret', return_value='different-client-secret-at-least-32-bytes'):
            from joserfc.errors import JoseError
            with self.assertRaises(JoseError):
                self.client_module._oidc_identity(self.provider, self.fixture.metadata, token, 'one-use-nonce')

    def test_all_oidc_presets_verify_their_fixed_or_configured_issuer(self):
        for kind, issuer in (
            ('authentik', 'https://idp.example'),
            ('synology', 'https://idp.example'),
            ('oidc', 'https://idp.example'),
            ('google', 'https://accounts.google.com'),
            ('microsoft', 'https://login.microsoftonline.com/12345678-1234-1234-1234-123456789abc/v2.0'),
        ):
            with self.subTest(kind=kind):
                self.provider.kind = kind
                self.provider.tenant_id = '12345678-1234-1234-1234-123456789abc'
                self.provider.allowed_endpoint_origins = ['https://idp.example', 'https://accounts.google.com', 'https://login.microsoftonline.com']
                self.fixture.metadata['issuer'] = issuer
                self.fixture.claims['iss'] = issuer
                self.assertEqual(self.exchange().issuer, issuer)

    def test_authorization_uses_pkce_s256_and_nonce(self):
        url = self.client_module.authorization_url(
            self.provider, 'https://pinry.example/callback', 'random-state',
            'one-use-nonce', 'a' * 64,
        )
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query['code_challenge_method'], ['S256'])
        self.assertEqual(query['code_challenge'], ['_-BU_nrgy23GXDr5th1SCfQ5hR20PQulmXM33xVGaOs'])
        self.assertEqual(query['nonce'], ['one-use-nonce'])
        self.assertEqual(query['state'], ['random-state'])
        self.assertEqual(query['response_type'], ['code'])

    def test_invalid_signature_is_rejected(self):
        self.fixture.signing_key = RSAKey.generate_key(2048)
        with self.assertRaises(ValidationError):
            self.exchange()

    def test_invalid_or_missing_claims_are_rejected(self):
        for claim, value in [
            ('iss', 'https://attacker.example'), ('aud', 'another-client'),
            ('nonce', 'wrong'), ('exp', 1), ('iat', 99999999999),
            ('sub', ''), ('sub', 123), ('sub', 'x' * 256),
            ('azp', 'another-client'), ('aud', ['pinry-client', 'another-client']),
        ]:
            with self.subTest(claim=claim, value=value):
                original = dict(self.fixture.claims)
                self.fixture.claims[claim] = value
                with self.assertRaises(ValidationError):
                    self.exchange()
                self.fixture.claims = original
        for claim in ('sub', 'iss', 'aud', 'exp', 'iat', 'nonce'):
            with self.subTest(missing=claim):
                original = self.fixture.claims.pop(claim)
                with self.assertRaises(ValidationError):
                    self.exchange()
                self.fixture.claims[claim] = original

    def test_discovery_cannot_change_issuer_or_contact_unlisted_origin(self):
        self.fixture.metadata['issuer'] = 'https://attacker.example'
        with self.assertRaises(ValidationError):
            self.exchange()
        self.fixture.metadata['issuer'] = self.fixture.issuer
        for key in ('authorization_endpoint', 'token_endpoint', 'jwks_uri'):
            with self.subTest(endpoint=key):
                original = self.fixture.metadata[key]
                self.fixture.metadata[key] = 'https://attacker.example/path'
                with self.assertRaises(ValidationError):
                    self.exchange()
                self.fixture.metadata[key] = original

    def test_non_finite_and_non_numeric_token_dates_are_rejected(self):
        original = dict(self.fixture.claims)
        for claim in ('exp', 'iat', 'nbf', 'auth_time'):
            for value in (float('nan'), float('inf'), float('-inf'), False, True, None, '123', [], {}):
                with self.subTest(claim=claim, value=value):
                    self.fixture.claims = dict(original, **{claim: value})
                    with self.assertRaises(ValidationError):
                        self.exchange()

    def test_integer_and_finite_float_token_dates_are_accepted(self):
        now = 2000000000
        with patch('time.time', return_value=now):
            for dates in (
                {'exp': now + 1, 'iat': now, 'nbf': now, 'auth_time': now - 1},
                {'exp': now + 0.5, 'iat': now - 0.5, 'nbf': now - 0.5, 'auth_time': now - 1.5},
            ):
                with self.subTest(dates=dates):
                    self.fixture.claims.update(dates)
                    self.assertEqual(self.exchange().subject, 'external-123')

    def test_expired_and_future_token_date_boundaries_are_rejected(self):
        now = 2000000000
        original = dict(self.fixture.claims, exp=now + 1, iat=now, nbf=now)
        with patch('time.time', return_value=now):
            for claim, value in (
                ('exp', now - 1), ('exp', now - 0.5),
                ('iat', now + 1), ('iat', now + 0.5),
                ('nbf', now + 1), ('nbf', now + 0.5),
            ):
                with self.subTest(claim=claim, value=value):
                    self.fixture.claims = dict(original, **{claim: value})
                    with self.assertRaises(ValidationError):
                        self.exchange()

    def test_unsupported_pkce_is_not_retried(self):
        self.fixture.metadata['code_challenge_methods_supported'] = ['plain']
        with self.assertRaises(ValidationError):
            self.exchange()
        self.assertFalse(any(path == '/token' for path, body in self.fixture.requests))

    def test_microsoft_requires_explicit_tenant_uuid(self):
        self.provider.kind = 'microsoft'
        for tenant in ('', 'common', 'organizations', '../evil', 'tenant.example'):
            with self.subTest(tenant=tenant), self.assertRaises(ValidationError):
                self.provider.tenant_id = tenant
                self.exchange()

    def test_github_uses_only_numeric_id(self):
        self.provider.kind = 'github'
        self.provider.allowed_endpoint_origins = ['https://github.com', 'https://api.github.com']
        identity = self.exchange()
        self.assertEqual(identity.subject, '12345678')
        self.assertEqual(identity.issuer, 'https://github.com')
        self.assertIsNone(identity.email)
        for value in (None, '', '12345678', True, 0, -1):
            with self.subTest(id=value):
                self.fixture.user['id'] = value
                with self.assertRaises(ValidationError):
                    self.exchange()


class EndpointTests(SimpleTestCase):
    def setUp(self):
        self.transport = importlib.import_module('users.sso.transport')
        self.provider = SSOProvider(allowed_endpoint_origins=['https://idp.example'])
        self.enterContext(patch('socket.getaddrinfo', return_value=PUBLIC_ADDRESS))

    def test_unlisted_metadata_endpoint_is_rejected(self):
        for url in (
            'http://169.254.169.254/latest/meta-data/',
            'https://other.example/token', 'https://idp.example:8443/token',
            'https://idp.example:0/token',
            'https://user:secret@idp.example/token', 'https://idp.example/token#fragment',
        ):
            with self.subTest(url=url), self.assertRaises(ValidationError):
                self.transport.validate_endpoint(self.provider, url)

    def test_sensitive_addresses_always_rejected(self):
        self.provider.internal_cidrs = ['10.0.0.0/8']
        for address in ('127.0.0.1', '169.254.169.254', '::1', '::ffff:127.0.0.1', '0.0.0.0', '100.100.100.200', '192.0.2.1'):
            resolved = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 443))]
            with self.subTest(address=address), patch('socket.getaddrinfo', return_value=resolved):
                with self.assertRaises(ValidationError):
                    self.transport.validate_endpoint(self.provider, 'https://idp.example/token')

    def test_private_network_requires_explicit_cidr_and_port(self):
        resolved = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.20.1.2', 8443))]
        with patch('socket.getaddrinfo', return_value=resolved):
            with self.assertRaises(ValidationError):
                self.transport.validate_endpoint(self.provider, 'https://idp.example/token')
            self.provider.internal_cidrs = ['10.20.0.0/16']
            self.provider.allowed_endpoint_origins = ['https://idp.example:8443']
            self.assertEqual(
                self.transport.validate_endpoint(self.provider, 'https://idp.example:8443/token'),
                'https://idp.example:8443/token',
            )

    def test_configured_self_hosted_discovery_allows_its_private_host_without_cidr(self):
        self.provider.kind = 'authentik'
        self.provider.discovery_url = 'https://idp.example/.well-known/openid-configuration'
        resolved = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.168.0.45', 443))]
        with patch('socket.getaddrinfo', return_value=resolved):
            self.assertEqual(self.transport.validate_endpoint(self.provider, 'https://idp.example/token'),
                             'https://idp.example/token')
            self.provider.allowed_endpoint_origins.append('https://other.example')
            with self.assertRaises(ValidationError):
                self.transport.validate_endpoint(self.provider, 'https://other.example/token')

    def test_mixed_public_and_private_dns_answer_is_rejected(self):
        private = (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.0.0.1', 443))
        with patch('socket.getaddrinfo', return_value=PUBLIC_ADDRESS + [private]):
            with self.assertRaises(ValidationError):
                self.transport.validate_endpoint(self.provider, 'https://idp.example/token')


class SecretTests(TestCase):
    def setUp(self):
        self.secrets = importlib.import_module('users.sso.secrets')
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.key_path = Path(directory) / 'sso.key'
        self.enterContext(self.settings(SSO_SECRET_KEY_FILE=str(self.key_path)))

    def test_roundtrip_survives_module_reload_and_key_is_private(self):
        encrypted = self.secrets.encrypt_secret('합성-client-secret')
        key = self.key_path.read_bytes()
        self.assertNotIn('client-secret', encrypted)
        importlib.reload(self.secrets)
        self.assertEqual(self.secrets.decrypt_secret(encrypted), '합성-client-secret')
        self.assertEqual(self.key_path.read_bytes(), key)
        self.assertEqual(self.key_path.stat().st_mode & 0o777, 0o600)

    def test_missing_key_with_existing_ciphertext_does_not_rotate(self):
        SSOProvider.objects.create(kind='oidc', name='합성', encrypted_client_secret='ciphertext')
        with self.assertRaises(ValidationError):
            self.secrets.encrypt_secret('new-value')
        self.assertFalse(self.key_path.exists())

    def test_missing_key_does_not_create_on_decrypt(self):
        with self.assertRaises(ValidationError):
            self.secrets.decrypt_secret('ciphertext')
        self.assertFalse(self.key_path.exists())

    def test_existing_attempt_ciphertext_also_blocks_key_regeneration(self):
        from django.utils import timezone
        provider = SSOProvider.objects.create(kind='oidc', name='합성')
        SSOAttempt.objects.create(
            provider=provider, provider_revision=1, purpose='login',
            state_digest='s' * 64, browser_digest='b' * 64,
            expires_at=timezone.now(), protected_payload='encrypted-attempt',
        )
        with self.assertRaises(ValidationError):
            self.secrets.encrypt_secret('new-value')
        self.assertFalse(self.key_path.exists())

    def test_symlink_and_insecure_mode_and_corrupt_key_are_rejected(self):
        encrypted = self.secrets.encrypt_secret('secret')
        self.key_path.chmod(0o644)
        with self.assertRaises(ValidationError):
            self.secrets.decrypt_secret(encrypted)
        self.key_path.chmod(0o600)
        target = self.key_path.with_suffix('.saved')
        self.key_path.rename(target)
        self.key_path.symlink_to(target)
        with self.assertRaises(ValidationError):
            self.secrets.encrypt_secret('secret')
        self.key_path.unlink()
        self.key_path.write_bytes(b'invalid')
        self.key_path.chmod(0o600)
        with self.assertRaises(ValidationError):
            self.secrets.encrypt_secret('secret')

    def test_tampered_ciphertext_is_rejected(self):
        encrypted = self.secrets.encrypt_secret('secret')
        with self.assertRaises(ValidationError):
            self.secrets.decrypt_secret(encrypted[:-8] + 'AAAAAAAA')

    def test_key_with_trailing_data_is_not_silently_accepted(self):
        encrypted = self.secrets.encrypt_secret('secret')
        self.key_path.write_bytes(self.key_path.read_bytes() + b'extra-data')
        with self.assertRaises(ValidationError):
            self.secrets.decrypt_secret(encrypted)

    def test_concurrent_initialization_uses_one_complete_key(self):
        # 작업 스레드의 DB 읽기만 고정하고 파일 생성·게시 경합은 실제 실행한다.
        with patch.object(SSOProvider.objects, 'exclude') as providers, patch.object(SSOAttempt.objects, 'exclude') as attempts:
            providers.return_value.exists.return_value = False
            attempts.return_value.exists.return_value = False
            with ThreadPoolExecutor(max_workers=8) as pool:
                values = list(pool.map(self.secrets.encrypt_secret, ['secret'] * 16))
        for value in values:
            self.assertEqual(self.secrets.decrypt_secret(value), 'secret')
        self.assertEqual(os.stat(self.key_path).st_mode & 0o777, 0o600)


class TLSTransportTests(SimpleTestCase):
    def setUp(self):
        self.transport = importlib.import_module('users.sso.transport')
        self.provider = SSOProvider(allowed_endpoint_origins=['https://idp.example'])

    def request(self, port, hostname='idp.example'):
        url = 'https://' + hostname + '/token'
        address = (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', port))
        # 시험용 주소 치환은 이 경계에만 존재한다. TLS·HTTP 처리는 실제 실행한다.
        with patch('users.sso.transport._resolve_endpoint', return_value=(urlsplit(url), [address])):
            with patch('socket.getaddrinfo', side_effect=AssertionError('두 번째 DNS 해석')):
                return self.transport.request_json(self.provider, url)

    def test_trusted_ca_with_original_hostname_and_no_dns_reresolution(self):
        with tls_server() as (port, ca_file, requests):
            with self.settings(SSO_CA_BUNDLE=ca_file), patch.dict(os.environ, {'HTTPS_PROXY': 'http://127.0.0.1:1'}):
                self.assertEqual(self.request(port), {'ok': True})
            self.assertEqual(requests[0]['Host'], 'idp.example')

    def test_untrusted_certificate_is_rejected(self):
        with tls_server() as (port, ca_file, requests):
            with self.settings(SSO_CA_BUNDLE=None), self.assertRaises(ValidationError):
                self.request(port)
            self.assertFalse(requests)

    def test_wrong_certificate_hostname_is_rejected(self):
        with tls_server() as (port, ca_file, requests):
            with self.settings(SSO_CA_BUNDLE=ca_file), self.assertRaises(ValidationError):
                self.request(port, hostname='other.example')
            self.assertFalse(requests)

    def test_redirect_and_large_or_invalid_body_are_rejected(self):
        for status, payload in ((302, b'{}'), (200, b'x' * (1024 * 1024 + 1)), (200, b'not-json'), (200, b'[]')):
            with self.subTest(status=status, size=len(payload)), tls_server(status, payload) as (port, ca_file, requests):
                with self.settings(SSO_CA_BUNDLE=ca_file), self.assertRaises(ValidationError):
                    self.request(port)
                self.assertEqual(len(requests), 1)

    def test_slow_response_hits_total_deadline(self):
        with tls_server(payload=b'{"ok": true}', delay=0.1) as (port, ca_file, requests):
            with self.settings(SSO_CA_BUNDLE=ca_file), patch('users.sso.transport.RESPONSE_TIMEOUT', 0.2):
                with self.assertRaises(ValidationError):
                    self.request(port)

    def test_slow_headers_also_hit_total_deadline(self):
        with tls_server(header_delay=0.02) as (port, ca_file, requests):
            with self.settings(SSO_CA_BUNDLE=ca_file), patch('users.sso.transport.RESPONSE_TIMEOUT', 0.2):
                started = time.monotonic()
                with self.assertRaises(ValidationError):
                    self.request(port)
                self.assertLess(time.monotonic() - started, 0.7)
