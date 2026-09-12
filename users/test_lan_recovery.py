from django.test import TestCase, override_settings

from users.models import AuthPolicy, User


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

    def test_external_proxy_or_spoofed_direct_header_is_rejected(self):
        for changes in ({'HTTP_HOST': 'pinry.example'}, {'HTTP_X_PINRY_PROXIED': '1'},
                        {'REMOTE_ADDR': '192.168.0.28'}, {'HTTP_X_PINRY_DIRECT_PEER': '8.8.8.8'},
                        {'HTTP_X_PINRY_PROXIED': ''}):
            with self.subTest(changes=changes):
                headers = dict(self.headers, **changes)
                self.assertEqual(self.client.get(self.url, **headers).status_code, 403)
                self.assertEqual(self.client.post(self.url, {'username': 'owner', 'password': 'test-password'},
                                                  **headers).status_code, 403)
                self.assertNotIn('_auth_user_id', self.client.session)

    def test_member_and_password_guessing_are_rejected(self):
        response = self.client.post(self.url, {'username': 'member', 'password': 'test-password'}, **self.headers)
        self.assertEqual(response.status_code, 403)
        for _ in range(4):
            self.client.post(self.url, {'username': 'owner', 'password': 'wrong'}, **self.headers)
        response = self.client.post(self.url, {'username': 'owner', 'password': 'test-password'}, **self.headers)
        self.assertEqual(response.status_code, 429)
        self.assertNotIn('_auth_user_id', self.client.session)
