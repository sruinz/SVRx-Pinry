from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser
from django.middleware.csrf import get_token
from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase
from django.utils import translation

from users.sso.signup import SignupForm, signup


class SignupTemplateTests(SimpleTestCase):
    def test_fields_and_inline_errors_are_accessibly_connected(self):
        request = RequestFactory().get('/api/v2/sso/signup/')
        get_token(request)
        form = SignupForm({
            'signup_token': 'bound-attempt-token',
            'username': 'invalid/name',
            'password1': 'Pinry-only-secret-49!',
            'password2': 'different',
        })
        self.assertFalse(form.is_valid())
        self.assertIn('username', form.errors)
        self.assertIn('password2', form.errors)

        html = render_to_string('sso/signup.html', {
            'form': form,
            'provider': SimpleNamespace(name='회사 계정'),
            'password_login_enabled': False,
        }, request=request)

        self.assertIn('<label for="id_username">', html)
        self.assertIn('type="hidden" name="signup_token" value="bound-attempt-token"', html)
        self.assertIn('<label for="id_password1">', html)
        self.assertIn('<label for="id_password2">', html)
        self.assertIn('aria-describedby="username-help username-errors"', html)
        self.assertIn('aria-describedby="password-help password1-errors"', html)
        self.assertIn('aria-describedby="password2-errors"', html)
        self.assertIn('id="username-errors"', html)
        self.assertIn('id="password2-errors"', html)
        self.assertGreaterEqual(html.count('role="alert"'), 2)

    @patch('users.sso.signup.password_login_allowed', return_value=False)
    @patch('users.sso.signup.pending_signup')
    def test_signup_view_renders_rules_and_validation_in_korean(
            self, pending_signup_mock, _password_login_allowed_mock):
        provider = SimpleNamespace(name='회사 계정')
        pending_signup_mock.return_value = (
            SimpleNamespace(state_digest='bound-attempt-token', provider=provider),
            SimpleNamespace(email='new.person@example.com', email_verified=True),
            '/',
        )
        request = RequestFactory().post('/api/v2/sso/signup/', {
            'signup_token': 'bound-attempt-token',
            'username': 'invalid/name',
            'password1': 'password',
            'password2': 'password',
        })
        request.user = AnonymousUser()
        request.session = {}

        with translation.override('en'):
            response = signup(request)
        html = response.content.decode()

        self.assertEqual(response.status_code, 400)
        self.assertIn('유효한 사용자 이름을 입력하세요.', html)
        self.assertIn('너무 흔히 사용되는 비밀번호입니다.', html)
        self.assertIn('비밀번호는 최소 8자 이상이어야 합니다.', html)
        self.assertNotIn('Enter a valid username', html)
        self.assertNotIn('Your password', html)
