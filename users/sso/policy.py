from contextvars import ContextVar

from django.contrib.auth import logout
from django.core.exceptions import ValidationError
from django.db import DatabaseError

from users.models import AuthPolicy, ExternalIdentity, SSOProvider
from users.sso.config import read_policy


policy_request = ContextVar('sso_policy_request', default=None)


def request_policy(request=None):
    if request is None:
        request = policy_request.get()
    if request is not None and hasattr(request, '_auth_policy'):
        return request._auth_policy
    try:
        policy = read_policy()
    except (AuthPolicy.DoesNotExist, DatabaseError):
        policy = None
    if request is not None:
        request._auth_policy = policy
    return policy


def password_login_allowed(request=None):
    policy = request_policy(request)
    return bool(policy and policy.password_login_enabled)


def api_token_allowed(request=None):
    if request is None:
        request = policy_request.get()
    if request is not None and getattr(request, 'recovery_deployment', None):
        return False
    if request is not None and getattr(request, 'session', {}).get('auth_method') in ('recovery', 'lan-recovery'):
        return False
    policy = request_policy(request)
    return bool(policy and policy.api_tokens_enabled)


def identity_is_usable(identity):
    from users.sso.config import expected_issuer, validate_provider
    try:
        validate_provider(identity.provider)
        return identity.provider.enabled and identity.issuer == expected_issuer(identity.provider)
    except ValidationError:
        return False


def sso_session_usable(user, provider_id, revision):
    try:
        provider = SSOProvider.objects.filter(pk=provider_id, revision=revision, enabled=True).first()
    except (ValueError, ValidationError):
        return False
    if provider is None:
        return False
    return any(identity_is_usable(identity) for identity in
               ExternalIdentity.objects.filter(user=user, provider=provider).select_related('provider'))


def enforce_session_policy(request):
    method = request.session.get('auth_method', 'password')
    if method == 'lan-recovery':
        from users.sso.lan_recovery import direct_lan_allowed
        if not (direct_lan_allowed(request) and request.user.is_authenticated
                and request.user.is_active and request.user.is_superuser):
            logout(request)
    elif method == 'recovery':
        logout(request)
    elif request.user.is_authenticated:
        if method == 'sso':
            if not sso_session_usable(request.user, request.session.get('sso_provider_id'),
                                      request.session.get('sso_provider_revision')):
                logout(request)
        elif method != 'password' or not password_login_allowed(request):
            logout(request)
