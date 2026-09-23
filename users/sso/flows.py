import hashlib
import json
import secrets
from datetime import timedelta
from math import ceil
from urllib.parse import unquote, urlsplit

from django.contrib.auth import SESSION_KEY, login
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from users.models import AuthPolicy, AuthVerification, ExternalIdentity, SSOAttempt, SSOProvider, User, make_identity_digest
from users.sso import client
from users.sso.policy import identity_is_usable, password_login_allowed, sso_session_usable
from users.sso.secrets import decrypt_secret, encrypt_secret


class SSOActionDenied(PermissionDenied):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def safe_next(value):
    if not isinstance(value, str) or len(value) > 2048:
        return '/'
    decoded = unquote(value)
    if (not decoded.startswith('/') or decoded.startswith('//')
            or any(char in decoded for char in ('\\', '?', '#', '%'))
            or any(ord(char) < 32 or ord(char) == 127 for char in decoded)):
        return '/'
    return value


def callback_url(provider):
    base = provider.public_base_url
    parsed = urlsplit(base)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in ('', '/')):
        raise ValidationError('SSO 공개 서비스의 HTTPS 기준 URL을 확인해 주세요.')
    return base.rstrip('/') + reverse('sso:callback', args=[provider.pk])


def require_user(request):
    if (not request.user.is_authenticated or not request.user.is_active
            or request.session.get(SESSION_KEY) != str(request.user.pk)):
        raise PermissionDenied('로그인이 필요합니다.')


def mark_recent_auth(request, method, provider=None):
    value = {'user_id': str(request.user.pk), 'method': method,
             'verified_at': timezone.now().timestamp()}
    if provider is not None:
        value.update(provider_id=str(provider.pk), provider_revision=provider.revision)
    request.session['recent_auth'] = value


def require_recent_auth(request):
    require_user(request)
    value = request.session.get('recent_auth', {})
    timestamp = value.get('verified_at')
    if (value.get('user_id') != str(request.user.pk)
            or type(timestamp) not in (int, float)
            or not 0 <= timezone.now().timestamp() - timestamp <= 300):
        raise SSOActionDenied('recent_auth_required', '최근 5분 이내의 재인증이 필요합니다.')
    if value.get('method') == 'password':
        if password_login_allowed(request) and request.user.has_usable_password():
            return
    elif value.get('method') == 'sso':
        if sso_session_usable(request.user, value.get('provider_id'), value.get('provider_revision')):
            return
    raise SSOActionDenied('recent_auth_required', '사용할 수 있는 인증 수단으로 다시 인증해 주세요.')


def recent_auth_remaining_seconds(request):
    try:
        require_recent_auth(request)
    except SSOActionDenied:
        return 0
    timestamp = request.session['recent_auth']['verified_at']
    return max(0, min(300, ceil(300 - (timezone.now().timestamp() - timestamp))))


def begin_attempt(request, provider, purpose, next_path):
    provider.refresh_from_db()
    if not provider.enabled:
        raise ValidationError('비활성 SSO 제공자입니다.')
    if purpose not in SSOAttempt.Purpose.values:
        raise ValidationError('올바른 SSO 인증 목적이 필요합니다.')
    if purpose == SSOAttempt.Purpose.LINK:
        require_recent_auth(request)
    elif purpose == SSOAttempt.Purpose.REAUTH:
        require_user(request)
        if not ExternalIdentity.objects.filter(user=request.user, provider=provider).exists():
            raise PermissionDenied('먼저 연결한 SSO 제공자로 재인증해 주세요.')
    elif request.user.is_authenticated:
        raise PermissionDenied('기존 로그인 상태에서는 연결 또는 재인증을 사용해 주세요.')
    if purpose == SSOAttempt.Purpose.LOGIN:
        request.session.pop('sso_signup', None)
    browser = request.session.get('sso_browser')
    if not browser:
        browser = secrets.token_urlsafe(32)
        request.session['sso_browser'] = browser
    state, nonce, verifier = (secrets.token_urlsafe(48) for _ in range(3))
    redirect_uri = callback_url(provider)
    url = client.authorization_url(provider, redirect_uri, state, nonce, verifier)
    now = timezone.now()
    expired = list(SSOAttempt.objects.filter(expires_at__lte=now)
                   .order_by('pk').values_list('pk', flat=True)[:100])
    SSOAttempt.objects.filter(pk__in=expired).delete()
    SSOAttempt.objects.create(
        state_digest=_digest(state), browser_digest=_digest(browser), provider=provider,
        provider_revision=provider.revision, purpose=purpose,
        user=request.user if purpose != SSOAttempt.Purpose.LOGIN else None,
        expires_at=now + timedelta(minutes=5),
        protected_payload=encrypt_secret(json.dumps({
            'nonce': nonce, 'verifier': verifier, 'redirect_uri': redirect_uri,
            'next': safe_next(next_path),
        })),
    )
    return url


def _identity(provider, verified):
    identity = ExternalIdentity.objects.filter(identity_digest=make_identity_digest(
        provider.pk, verified.issuer, verified.subject,
    )).first()
    if identity is not None and (
        identity.provider_id != provider.pk or identity.issuer != verified.issuer
        or identity.subject != verified.subject
    ):
        raise ValidationError('SSO 외부 ID가 정확히 일치하지 않습니다.')
    return identity


def _finish_verified(request, provider, attempt, verified, signup_data=None):
    # 설정 저장과 같은 잠금 순서로 교환 이후의 상태를 최종 확인한다.
    policy = AuthPolicy.objects.select_for_update().get(pk=1)
    provider = SSOProvider.objects.select_for_update().get(pk=provider.pk)
    if not provider.enabled or provider.revision != attempt.provider_revision:
        raise ValidationError('SSO 제공자 설정이 변경되었습니다. 다시 시작해 주세요.')
    identity = _identity(provider, verified)
    if attempt.purpose == SSOAttempt.Purpose.LOGIN:
        if request.user.is_authenticated:
            raise PermissionDenied('로그인한 계정을 변경할 수 없습니다.')
        if signup_data is not None and identity is not None:
            raise ValidationError('이미 연결된 SSO 계정입니다. SSO 로그인을 다시 시작해 주세요.')
        if identity is None and verified.email_verified and verified.email:
            matches = list(User.objects.select_for_update().filter(email__iexact=verified.email)[:2])
            if len(matches) > 1:
                raise PermissionDenied('이메일이 중복되어 자동 연결할 수 없습니다. 관리자에게 문의해 주세요.')
            if matches:
                if signup_data is not None:
                    raise ValidationError('같은 이메일의 계정이 생성되었습니다. SSO 로그인을 다시 시작해 주세요.')
                user = matches[0]
                if not user.is_active or ExternalIdentity.objects.filter(user=user, provider=provider).exists():
                    raise PermissionDenied('비활성 계정이거나 기존 SSO 연결과 충돌합니다. 관리자에게 문의해 주세요.')
                identity = ExternalIdentity.objects.create(
                    user=user, provider=provider, issuer=verified.issuer, subject=verified.subject,
                )
        if identity is None:
            if not provider.allow_signup:
                raise PermissionDenied('연결된 계정이 없습니다. 기존 계정에서 먼저 연결해 주세요.')
            if signup_data is None:
                return None, provider
            user = User.objects.create_user(
                username=signup_data['username'], password=signup_data['password1'],
                email=(verified.email or '')[:254] if verified.email_verified else '',
            )
            ExternalIdentity.objects.create(user=user, provider=provider,
                                            issuer=verified.issuer, subject=verified.subject)
        else:
            user = User.objects.select_for_update().get(pk=identity.user_id)
    else:
        require_user(request)
        if attempt.user_id != request.user.pk:
            raise PermissionDenied('인증을 시작한 계정과 현재 계정이 다릅니다.')
        user = User.objects.select_for_update().get(pk=attempt.user_id)
        if attempt.purpose == SSOAttempt.Purpose.LINK:
            require_recent_auth(request)
            if identity is None:
                ExternalIdentity.objects.create(user=user, provider=provider,
                                                issuer=verified.issuer, subject=verified.subject)
            elif identity.user_id != user.pk:
                raise PermissionDenied('이미 다른 계정에 연결된 SSO 계정입니다.')
        elif identity is None or identity.user_id != user.pk:
            raise PermissionDenied('현재 계정에 연결된 SSO 계정으로 재인증해 주세요.')
    if not user.is_active:
        raise PermissionDenied('비활성 계정은 로그인할 수 없습니다.')
    AuthVerification.objects.create(
        user=user, kind=AuthVerification.Kind.SSO, provider=provider,
        policy_revision=policy.revision, provider_revision=provider.revision,
        deployment_fingerprint='',
    )
    return user, provider


def finish_attempt(request, provider, state, code):
    browser = request.session.get('sso_browser')
    if not state or not browser or len(state) > 256:
        raise ValidationError('SSO 인증 상태를 확인할 수 없습니다.')
    now = timezone.now()
    eligible = SSOAttempt.objects.filter(
        state_digest=_digest(state), browser_digest=_digest(browser), provider=provider,
        consumed_at__isnull=True, expires_at__gt=now,
    )
    attempt = eligible.first()
    if attempt is None or eligible.update(consumed_at=now) != 1:
        raise ValidationError('만료되었거나 이미 사용한 SSO 인증입니다.')
    if not code:
        raise ValidationError('SSO 인증이 취소되었거나 인증 코드가 없습니다.')
    if not provider.enabled or provider.revision != attempt.provider_revision:
        raise ValidationError('SSO 제공자 설정이 변경되었습니다.')
    if attempt.purpose != SSOAttempt.Purpose.LOGIN:
        require_user(request)
        if request.user.pk != attempt.user_id:
            raise PermissionDenied('인증을 시작한 계정과 현재 계정이 다릅니다.')
    payload = json.loads(decrypt_secret(attempt.protected_payload))
    # 일회성 소비를 먼저 확정하고 네트워크 요청 중에는 DB 잠금을 잡지 않는다.
    verified = client.exchange_identity(
        provider, payload['redirect_uri'], code, payload['nonce'], payload['verifier'],
    )
    try:
        with transaction.atomic():
            user, current_provider = _finish_verified(request, provider, attempt, verified)
    except IntegrityError:
        raise PermissionDenied('이미 연결된 외부 계정입니다. 다시 확인해 주세요.') from None
    if user is None:
        # OAuth 코드는 이미 소비했다. 가입 폼에는 토큰 대신 현재 브라우저의 임시 참조만 사용한다.
        attempt.protected_payload = encrypt_secret(json.dumps({
            'signup': {'issuer': verified.issuer, 'subject': verified.subject,
                       'email': verified.email, 'email_verified': verified.email_verified,
                       'display_name': verified.display_name},
            'next': safe_next(payload['next']),
        }))
        attempt.expires_at = timezone.now() + timedelta(minutes=10)
        attempt.save(update_fields=['protected_payload', 'expires_at'])
        request.session['sso_signup'] = attempt.pk
        request.sso_next_path = reverse('sso:signup')
        return None
    if attempt.purpose == SSOAttempt.Purpose.LOGIN:
        login(request, user, backend='users.sso.authentication.SSOBackend')
        request.session['auth_method'] = 'sso'
        request.session['sso_provider_id'] = str(current_provider.pk)
        request.session['sso_provider_revision'] = current_provider.revision
    if attempt.purpose in (SSOAttempt.Purpose.LOGIN, SSOAttempt.Purpose.REAUTH):
        mark_recent_auth(request, 'sso', current_provider)
    request.sso_next_path = safe_next(payload['next'])
    return user


def can_unlink_identity(user, identity, policy):
    alternatives = ExternalIdentity.objects.filter(user=user, provider__enabled=True).exclude(pk=identity.pk)
    usable = any(identity_is_usable(item) for item in alternatives.select_related('provider'))
    return (policy.password_login_enabled and user.has_usable_password()) or usable


@transaction.atomic
def unlink_identity(request, identity_id):
    require_user(request)
    policy = AuthPolicy.objects.select_for_update().get(pk=1)
    user = User.objects.select_for_update().get(pk=request.user.pk)
    identity = ExternalIdentity.objects.filter(pk=identity_id, user=user).first()
    if identity is None:
        raise SSOActionDenied('identity_not_found', '내 계정의 SSO 연결만 해제할 수 있습니다.')
    if not can_unlink_identity(user, identity, policy):
        raise SSOActionDenied('last_login_method', '마지막으로 사용할 수 있는 로그인 수단은 해제할 수 없습니다.')
    require_recent_auth(request)
    identity.delete()
