import json
import logging
from urllib.parse import urlencode, urlsplit

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from users.models import ExternalIdentity, SSOProvider
from users.sso.policy import (api_token_allowed, identity_is_usable, password_login_allowed,
                              registration_allowed, request_policy)
from users.sso.flows import (begin_attempt, callback_url, finish_attempt, mark_recent_auth,
                            require_user, safe_next, unlink_identity, can_unlink_identity, SSOActionDenied,
                            recent_auth_remaining_seconds)
from users.sso.lan_recovery import direct_lan_allowed


logger = logging.getLogger(__name__)


def _data(request):
    if request.content_type == 'application/json':
        try:
            value = json.loads(request.body)
        except (ValueError, UnicodeError):
            raise ValidationError('올바른 JSON 요청이 필요합니다.') from None
        if not isinstance(value, dict):
            raise ValidationError('JSON 객체가 필요합니다.')
        return value
    return request.POST


def _begin(request, provider_id, purpose):
    provider = get_object_or_404(SSOProvider, pk=provider_id, enabled=True)
    public_home = None
    try:
        data = request.GET if request.method == 'GET' else _data(request)
        next_path = safe_next(data.get('next', '/'))
        try:
            public = urlsplit(callback_url(provider))
            current = urlsplit('https://' + request.get_host())
            # TLS 종료 프록시 뒤에서도 동작하도록 호스트·포트만 비교한다.
            # HTTPS 강제는 공개 프록시 정책이며 전달 헤더를 새로 신뢰하지 않는다.
            same_authority = ((current.hostname, current.port or 443)
                              == (public.hostname, public.port or 443))
        except ValueError:
            raise ValidationError('SSO 공개 주소와 접속 주소의 포트를 확인해 주세요.') from None
        if purpose == 'login' and request.user.is_authenticated:
            require_user(request)
            response = HttpResponseRedirect(next_path)
        elif not same_authority:
            public_home = provider.public_base_url.rstrip('/') + '/'
            if purpose != 'login':
                raise ValidationError('계정 연결·재인증은 공개 주소에서 같은 Pinry 계정으로 '
                                      '로그인한 뒤 프로필에서 다시 진행해 주세요.')
            # 내부 주소의 세션을 만들거나 옮기지 않고 공개 주소에서 인증을 시작한다.
            destination = (provider.public_base_url.rstrip('/')
                           + reverse('sso:login', args=[provider.pk])
                           + '?' + urlencode({'next': next_path}))
            response = HttpResponseRedirect(destination)
        else:
            response = HttpResponseRedirect(begin_attempt(request, provider, purpose, next_path))
    except (ValidationError, PermissionDenied) as error:
        response = render(request, 'sso/error.html', {
            'reason': ' '.join(error.messages) if isinstance(error, ValidationError) else str(error),
            'public_home': public_home,
        }, status=403 if isinstance(error, PermissionDenied) else 400)
    response['Cache-Control'] = 'no-store'
    response['Referrer-Policy'] = 'no-referrer'
    return response


@require_GET
def providers(request):
    response = JsonResponse({'providers': [
        {'id': str(provider.pk), 'name': provider.name, 'kind': provider.kind,
         'login_url': reverse('sso:login', args=[provider.pk])}
        for provider in SSOProvider.objects.filter(enabled=True)
    ], 'password_login_enabled': password_login_allowed(request),
        'allow_new_registrations': registration_allowed(request),
        'api_tokens_enabled': api_token_allowed(request),
        'recent_auth_remaining_seconds': recent_auth_remaining_seconds(request)})
    response['Cache-Control'] = 'no-store'
    if not request.user.is_authenticated and direct_lan_allowed(request):
        data = json.loads(response.content)
        data['recovery_login_url'] = reverse('sso:lan-recovery')
        response = JsonResponse(data)
        response['Cache-Control'] = 'no-store'
    return response


@require_GET
def login_page(request):
    if request.user.is_authenticated:
        response = HttpResponseRedirect('/')
    else:
        response = render(request, 'sso/login.html', {
            'providers': SSOProvider.objects.filter(enabled=True),
            'password_login_enabled': password_login_allowed(request),
            'recovery_login_allowed': direct_lan_allowed(request),
        })
    response['Cache-Control'] = 'no-store'
    response['Referrer-Policy'] = 'no-referrer'
    return response


@require_GET
def start_login(request, provider_id):
    return _begin(request, provider_id, 'login')


@require_POST
def start_link(request, provider_id):
    require_user(request)
    return _begin(request, provider_id, 'link')


@require_POST
def start_reauth(request, provider_id):
    require_user(request)
    return _begin(request, provider_id, 'reauth')


@require_GET
def callback(request, provider_id):
    try:
        provider = SSOProvider.objects.get(pk=provider_id)
        finish_attempt(request, provider, request.GET.get('state', ''), request.GET.get('code', ''))
        destination = request.sso_next_path
    except (ValidationError, PermissionDenied, SSOProvider.DoesNotExist) as error:
        reason = (' '.join(error.messages) if isinstance(error, ValidationError) else str(error))
        if isinstance(error, SSOProvider.DoesNotExist):
            reason = 'SSO 제공자 설정을 찾을 수 없습니다.'
        logger.warning('SSO 인증 실패 provider=%s category=%s', provider_id, type(error).__name__)
        messages.error(request, 'SSO 인증을 완료하지 못했습니다. ' + reason)
        destination = '/login/'
    response = HttpResponseRedirect(destination)
    response['Cache-Control'] = 'no-store'
    response['Referrer-Policy'] = 'no-referrer'
    return response


@require_POST
def password_reauth(request):
    require_user(request)
    try:
        password = _data(request).get('password')
    except ValidationError as error:
        return JsonResponse({'detail': error.messages}, status=400)
    if (not password_login_allowed(request) or not isinstance(password, str)
            or not request.user.check_password(password)):
        raise PermissionDenied('현재 계정의 비밀번호로 다시 인증해 주세요.')
    mark_recent_auth(request, 'password')
    return HttpResponse(status=204)


@require_GET
def identities(request):
    require_user(request)
    result = []
    policy = request_policy(request)
    for identity in ExternalIdentity.objects.filter(user=request.user).select_related('provider'):
        allowed = can_unlink_identity(request.user, identity, policy)
        result.append({'id': identity.pk, 'provider_id': str(identity.provider_id),
                       'provider_name': identity.provider.name, 'enabled': identity_is_usable(identity),
                       'unlink_allowed': allowed, 'unlink_reason': None if allowed else 'last_login_method'})
    response = JsonResponse(result, safe=False)
    response['Cache-Control'] = 'no-store'
    return response


@require_POST
def unlink(request, identity_id):
    try:
        unlink_identity(request, identity_id)
    except SSOActionDenied as error:
        return JsonResponse({'code': error.code, 'detail': str(error)}, status=403)
    return HttpResponse(status=204)
