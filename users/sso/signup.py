import json
import re
import unicodedata
from urllib.parse import urlsplit

from django import forms
from django.contrib.auth import login, password_validation
from django.contrib.auth.forms import UsernameField
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.utils import timezone, translation
from django.utils.crypto import constant_time_compare
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from users.models import AuthPolicy, SSOAttempt, User
from users.sso.client import VerifiedIdentity
from users.sso.flows import _digest, _finish_verified, callback_url, mark_recent_auth, safe_next
from users.sso.policy import password_login_allowed
from users.sso.secrets import decrypt_secret, encrypt_secret


def suggested_username(email):
    value = unicodedata.normalize('NFKC', (email or '').split('@')[0])
    return re.sub(r'[^\w.+-]', '', value)[:150] or 'user'


class SignupForm(forms.Form):
    signup_token = forms.CharField(max_length=64, widget=forms.HiddenInput)
    username = UsernameField(label='아이디', max_length=150,
                             validators=User._meta.get_field('username').validators,
                             widget=forms.TextInput(attrs={'autocomplete': 'username',
                                 'aria-describedby': 'username-help username-errors'}))
    password1 = forms.CharField(label='Pinry 비밀번호', strip=False, max_length=128,
                               widget=forms.PasswordInput(attrs={'autocomplete': 'new-password',
                                   'aria-describedby': 'password-help password1-errors'}))
    password2 = forms.CharField(label='비밀번호 확인', strip=False, max_length=128,
                               widget=forms.PasswordInput(attrs={'autocomplete': 'new-password',
                                   'aria-describedby': 'password2-errors'}))

    def __init__(self, *args, email='', **kwargs):
        super().__init__(*args, **kwargs)
        self.email = email
        self.fields['password1'].help_text = password_validation.password_validators_help_text_html()
        for field in self.fields.values():
            field.error_messages['required'] = '이 항목을 입력하세요.'

    def clean_username(self):
        username = self.cleaned_data['username']
        if User.objects.filter(username__iexact=username).exists():
            raise ValidationError('이미 사용 중인 아이디입니다. 다른 아이디나 숫자를 붙인 아이디를 입력하세요.')
        return username

    def clean_password2(self):
        password = self.cleaned_data['password2']
        if password != self.cleaned_data.get('password1'):
            raise ValidationError('비밀번호가 일치하지 않습니다.')
        user = User(username=self.cleaned_data.get('username', ''), email=self.email)
        with translation.override('ko'):
            password_validation.validate_password(password, user)
        return password


def pending_signup(request, lock=False):
    if request.user.is_authenticated:
        raise PermissionDenied('로그인한 상태에서는 신규 가입을 진행할 수 없습니다.')
    browser = request.session.get('sso_browser')
    pending_id = request.session.get('sso_signup')
    if not browser or type(pending_id) is not int:
        raise ValidationError('가입 인증이 없거나 만료되었습니다. SSO 로그인을 다시 시작해 주세요.')
    query = SSOAttempt.objects.select_related('provider')
    if lock:
        query = query.select_for_update()
    attempt = query.filter(pk=pending_id, browser_digest=_digest(browser), purpose='login',
                           consumed_at__isnull=False, expires_at__gt=timezone.now()).first()
    if attempt is None:
        raise ValidationError('가입 인증이 만료되었습니다. SSO 로그인을 다시 시작해 주세요.')
    provider = attempt.provider
    if not provider.enabled or not provider.allow_signup or provider.revision != attempt.provider_revision:
        raise ValidationError('SSO 가입 설정이 변경되었습니다. SSO 로그인을 다시 시작해 주세요.')
    public = urlsplit(callback_url(provider))
    current = urlsplit('https://' + request.get_host())
    if (current.hostname, current.port or 443) != (public.hostname, public.port or 443):
        raise ValidationError('가입을 시작한 공개 주소에서 다시 진행해 주세요.')
    payload = json.loads(decrypt_secret(attempt.protected_payload))
    if 'signup' not in payload:
        raise ValidationError('이미 사용한 가입 인증입니다. SSO 로그인을 다시 시작해 주세요.')
    return attempt, VerifiedIdentity(**payload['signup']), payload['next']


@transaction.atomic
def complete_signup(request, form):
    # 관리자 설정 저장과 동일하게 정책 → 제공자 순서로 잠근다.
    AuthPolicy.objects.select_for_update().get(pk=1)
    attempt, verified, next_path = pending_signup(request, lock=True)
    # 폼 검증 이후 발생한 중복도 다시 검사하며 실패 시 가입 전체를 되돌린다.
    if User.objects.filter(username__iexact=form.cleaned_data['username']).exists():
        raise ValidationError('이미 사용 중인 아이디입니다. 다른 아이디를 입력하세요.')
    claimed = SSOAttempt.objects.filter(pk=attempt.pk, protected_payload=attempt.protected_payload).update(
        protected_payload=encrypt_secret('{}'))
    if claimed != 1:
        raise ValidationError('이미 처리 중인 가입입니다. 다시 로그인해 주세요.')
    user, provider = _finish_verified(request, attempt.provider, attempt, verified, form.cleaned_data)
    return user, provider, next_path


@sensitive_post_parameters('password1', 'password2')
@require_http_methods(['GET', 'POST'])
def signup(request):
    if request.user.is_authenticated and request.method == 'GET':
        response = HttpResponseRedirect('/')
    else:
        try:
            attempt, verified, _ = pending_signup(request)
            if request.method == 'POST' and not constant_time_compare(
                    request.POST.get('signup_token', ''), attempt.state_digest):
                raise ValidationError('다른 탭에서 가입 인증이 변경되었습니다. SSO 로그인을 다시 시작해 주세요.')
            form = SignupForm(request.POST if request.method == 'POST' else None,
                              initial={'username': suggested_username(verified.email),
                                       'signup_token': attempt.state_digest},
                              email=verified.email if verified.email_verified else '')
            user = None
            if request.method == 'POST' and form.is_valid():
                try:
                    user, provider, next_path = complete_signup(request, form)
                except (ValidationError, PermissionDenied) as error:
                    form.add_error(None, error if isinstance(error, ValidationError) else str(error))
                except IntegrityError:
                    form.add_error(None, '아이디 또는 SSO 계정이 이미 사용 중입니다. 다시 확인해 주세요.')
            if user is not None:
                request.session.pop('sso_signup', None)
                login(request, user, backend='users.sso.authentication.SSOBackend')
                request.session['auth_method'] = 'sso'
                request.session['sso_provider_id'] = str(provider.pk)
                request.session['sso_provider_revision'] = provider.revision
                mark_recent_auth(request, 'sso', provider)
                response = HttpResponseRedirect(safe_next(next_path))
            else:
                with translation.override('ko'):
                    response = render(request, 'sso/signup.html', {
                        'form': form, 'provider': attempt.provider,
                        'password_login_enabled': password_login_allowed(request),
                    }, status=400 if form.is_bound else 200)
        except (ValidationError, PermissionDenied) as error:
            response = render(request, 'sso/error.html', {
                'reason': ' '.join(error.messages) if isinstance(error, ValidationError) else str(error),
            }, status=403 if isinstance(error, PermissionDenied) else 400)
    response['Cache-Control'] = 'no-store'
    response['Referrer-Policy'] = 'no-referrer'
    return response
