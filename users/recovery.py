import hashlib
import ipaddress
from datetime import timedelta

from django import forms
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.backends import ModelBackend
from django.core.exceptions import ValidationError
from django.db import DatabaseError, transaction
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import redirect, render
from django.utils import timezone, translation
from django.views.decorators.http import require_http_methods, require_POST

from pinry.recovery_config import deployment_configuration
from users.models import AuthPolicy, AuthVerification, AuthenticationThrottle, User, normalize_cidrs
from users.sso.config import save_configuration
from users.sso.policy import policy_request


def recovery_peer_allowed(address, allow, deny):
    try:
        peer = ipaddress.ip_address(address)
        allowed = [ipaddress.ip_network(value) for value in normalize_cidrs(allow, private_only=True)]
        denied = [ipaddress.ip_network(value) for value in normalize_cidrs(deny, private_only=False)]
        return any(peer in network for network in allowed) and not any(peer in network for network in denied)
    except (ValueError, TypeError, ValidationError):
        return False


class RecoveryBoundaryMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # HTTP 헤더로 설정할 수 없으며 전용 WSGI 진입점에서만 넣는다.
        if request.META.get('pinry.recovery_listener') is not True:
            return HttpResponseForbidden()
        deployment = deployment_configuration()
        try:
            policy = AuthPolicy.objects.get(pk=1)
        except (AuthPolicy.DoesNotExist, DatabaseError):
            return HttpResponseForbidden()
        peer = request.META.get('HTTP_X_PINRY_RECOVERY_PEER', '')
        origin = request.META.get('HTTP_ORIGIN')
        if (not deployment or request.get_host() != deployment['host'] or not request.is_secure()
                or not recovery_peer_allowed(peer, policy.recovery_allowed_cidrs, policy.recovery_denied_cidrs)
                or (origin is not None and origin != deployment['origin'])
                or (request.method not in ('GET', 'HEAD', 'OPTIONS') and origin != deployment['origin'])):
            return HttpResponseForbidden()
        request.recovery_deployment = deployment
        request.recovery_peer = peer
        request._auth_policy = policy
        if request.user.is_authenticated and (
            request.session.get('auth_method') != 'recovery'
            or not request.user.is_active or not request.user.is_superuser
            or request.session.get('recovery_fingerprint') != deployment['fingerprint']
        ):
            logout(request)
        token = policy_request.set(request)
        try:
            with translation.override('ko'):
                response = self.get_response(request)
            response['Cache-Control'] = 'no-store'
            response['X-Frame-Options'] = 'DENY'
            response['Referrer-Policy'] = 'same-origin'
            return response
        finally:
            policy_request.reset(token)


class RecoveryBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        if request is None or not getattr(request, 'recovery_deployment', None):
            return None
        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            User().set_password(password)
            return None
        if user.check_password(password) and user.is_active and user.is_superuser:
            return user
        return None


def _limited_authenticate(request, username, password, authenticator=authenticate):
    keys = sorted(hashlib.sha256(('recovery:' + kind + ':' + value).encode()).hexdigest()
                  for kind, value in [('peer', request.recovery_peer), ('account', username.casefold())])
    now = timezone.now()
    with transaction.atomic():
        rows = []
        for key in keys:
            AuthenticationThrottle.objects.get_or_create(key_digest=key)
            row = AuthenticationThrottle.objects.select_for_update().get(key_digest=key)
            if now >= row.window_started_at + timedelta(minutes=5):
                row.window_started_at, row.failure_count, row.blocked_until = now, 0, None
                row.save()
            rows.append(row)
        if any(row.failure_count >= 5 for row in rows):
            return None, True
        user = authenticator(request, username=username, password=password)
        if user is None:
            for row in rows:
                row.failure_count += 1
                if row.failure_count >= 5:
                    row.blocked_until = row.window_started_at + timedelta(minutes=5)
                row.save()
        return user, False


@require_http_methods(['GET', 'POST'])
def recovery_login(request):
    if request.method == 'GET':
        return render(request, 'recovery/login.html')
    username = request.POST.get('username', '')[:150]
    password = request.POST.get('password', '')
    try:
        user, limited = _limited_authenticate(request, username, password)
    except DatabaseError:
        return HttpResponse('로그인할 수 없습니다.', status=503)
    if not user:
        return render(request, 'recovery/login.html', {'error': '로그인할 수 없습니다.'},
                      status=429 if limited else 403)
    request.session['auth_method'] = 'recovery'
    login(request, user)
    request.session['auth_method'] = 'recovery'
    request.session['recovery_fingerprint'] = request.recovery_deployment['fingerprint']
    request.session.set_expiry(900)
    AuthVerification.objects.create(
        user=user, kind='recovery', policy_revision=request._auth_policy.revision,
        deployment_fingerprint=request.recovery_deployment['fingerprint'],
    )
    return redirect('recovery:settings')


class RecoveryPolicyForm(forms.Form):
    revision = forms.IntegerField(widget=forms.HiddenInput)
    password_login_enabled = forms.BooleanField(required=False, label='일반 비밀번호 로그인 다시 허용')
    recovery_allowed_cidrs = forms.CharField(required=False, widget=forms.Textarea, label='허용 CIDR (줄마다 하나)')
    recovery_denied_cidrs = forms.CharField(required=False, widget=forms.Textarea, label='차단 CIDR (NAS·게이트웨이·프록시)')

    def policy_changes(self):
        changes = {
            name: self.cleaned_data[name].split()
            for name in ('recovery_allowed_cidrs', 'recovery_denied_cidrs')
        }
        if self.cleaned_data['password_login_enabled']:
            changes['password_login_enabled'] = True
        return changes


@require_http_methods(['GET', 'POST'])
def recovery_settings(request):
    if not (request.user.is_authenticated and request.user.is_active and request.user.is_superuser
            and request.session.get('auth_method') == 'recovery'):
        return redirect('recovery:login')
    policy = request._auth_policy
    form = RecoveryPolicyForm(request.POST if request.method == 'POST' else None, initial={
        'revision': policy.revision,
        'recovery_allowed_cidrs': '\n'.join(policy.recovery_allowed_cidrs),
        'recovery_denied_cidrs': '\n'.join(policy.recovery_denied_cidrs),
    })
    if request.method == 'POST' and form.is_valid():
        try:
            save_configuration(request.user, form.policy_changes(), expected_revision=form.cleaned_data['revision'])
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return redirect('recovery:settings')
    return render(request, 'recovery/settings.html', {'form': form},
                  status=400 if request.method == 'POST' else 200)


@require_POST
def recovery_logout(request):
    logout(request)
    return redirect('recovery:login')
