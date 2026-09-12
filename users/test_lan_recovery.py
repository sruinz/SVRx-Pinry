from django.test import TestCase, override_settings

from users.models import AuthPolicy, SSOProvider, User


@override_settings(PUBLIC=False, ALLOWED_HOSTS=['*'])
class LANRecoveryTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_superuser('owner', 'owner@example.com', 'test-password')
        User.objects.create_user('member', password='test-password')
        AuthPolicy.objects.filter(pk=1).update(password_login_enabled=False)
        self.headers = dict(HTTP_HOST='192.168.0.45:8000', REMOTE_ADDR='127.0.0.1',
                            HTTP_X_PINRY_DIRECT_PEER='192.168.0.28', HTTP_X_PINRY_PROXIED='0')
        self.url = '/api/v2/sso/recovery/'

    def test_direct_lan_admin_login_and_session(self):
        response = self.client.get('/login/', **self.headers)
        self.assertContains(response, self.url)
        self.assertContains(self.client.get(self.url, **self.headers), 'name="password"')
        response = self.client.post(self.url, {'username': 'owner', 'password': 'test-password'}, **self.headers)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.session['_auth_user_id'], str(self.owner.pk))
        self.assertEqual(self.client.get('/admin/', **self.headers).status_code, 200)
        self.client.get('/login/', HTTP_HOST='pinry.example')
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_failed_recovery_login_keeps_retry_form_and_public_login_link(self):
        response = self.client.post(
            self.url,
            {'username': 'owner', 'password': 'wrong'},
            **self.headers,
        )

        self.assertEqual(response.status_code, 403)
        self.assertContains(response, '관리자 계정과 비밀번호를 확인하세요.', status_code=403)
        self.assertContains(response, 'name="csrfmiddlewaretoken"', status_code=403)
        self.assertContains(response, 'name="username"', status_code=403)
        self.assertContains(response, 'maxlength="150"', status_code=403)
        self.assertContains(response, 'autocomplete="username"', status_code=403)
        self.assertContains(response, 'name="password"', status_code=403)
        self.assertContains(response, 'autocomplete="current-password"', status_code=403)
        self.assertContains(response, 'href="/login/"', status_code=403)

    def test_public_login_preserves_provider_names_icons_and_conditional_links(self):
        providers = [SSOProvider.objects.create(
            kind=kind,
            name=f'회사 {kind}',
            enabled=True,
            position=index,
        ) for index, kind in enumerate(
            ('authentik', 'synology', 'google', 'microsoft', 'github', 'oidc'),
        )]

        AuthPolicy.objects.filter(pk=1).update(password_login_enabled=True)
        response = self.client.get('/login/', **self.headers)
        for provider in providers:
            with self.subTest(kind=provider.kind):
                self.assertContains(response, f'회사 {provider.kind}')
                self.assertContains(response, f'/static/auth/providers/{provider.kind}.svg')
        self.assertContains(response, '홈에서 비밀번호 로그인')
        self.assertContains(response, '관리자 복구 로그인')

        AuthPolicy.objects.filter(pk=1).update(password_login_enabled=False)
        response = self.client.get('/login/', HTTP_HOST='pinry.example')
        self.assertContains(response, '비밀번호 로그인이 비활성화되어 있습니다.')
        self.assertNotContains(response, '홈에서 비밀번호 로그인')
        self.assertNotContains(response, '관리자 복구 로그인')

    def test_external_proxy_or_spoofed_direct_header_is_rejected(self):
        for changes in ({'HTTP_HOST': 'pinry.example'}, {'HTTP_X_PINRY_PROXIED': '1'},
                        {'REMOTE_ADDR': '192.168.0.28'}, {'HTTP_X_PINRY_DIRECT_PEER': '8.8.8.8'},
                        {'HTTP_X_PINRY_PROXIED': ''}):
            with self.subTest(changes=changes):
                headers = dict(self.headers, **changes)
                self.assertEqual(self.client.get(self.url, **headers).status_code, 403)
                self.assertEqual(self.client.post(self.url, {'username': 'owner', 'password': 'test-password'},
                                                  **headers).status_code, 403)
                self.assertNotContains(self.client.get(self.url, **headers), 'name="password"', status_code=403)
                self.assertNotIn('_auth_user_id', self.client.session)

    def test_member_and_password_guessing_are_rejected(self):
        response = self.client.post(self.url, {'username': 'member', 'password': 'test-password'}, **self.headers)
        self.assertEqual(response.status_code, 403)
        for _ in range(4):
            self.client.post(self.url, {'username': 'owner', 'password': 'wrong'}, **self.headers)
        response = self.client.post(self.url, {'username': 'owner', 'password': 'test-password'}, **self.headers)
        self.assertEqual(response.status_code, 429)
        self.assertNotIn('_auth_user_id', self.client.session)
