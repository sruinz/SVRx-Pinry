from types import SimpleNamespace

from django.middleware.csrf import get_token
from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase

from users.sso.signup import SignupForm


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
