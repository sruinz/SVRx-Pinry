import json
import logging

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from users.models import ExternalIdentity, SSOProvider
from users.sso.policy import api_token_allowed, identity_is_usable, password_login_allowed
from users.sso.flows import begin_attempt, finish_attempt, mark_recent_auth, require_user, unlink_identity
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
    try:
        data = request.GET if request.method == 'GET' else _data(request)
        response = HttpResponseRedirect(begin_attempt(request, provider, purpose, data.get('next', '/')))
    except (ValidationError, PermissionDenied) as error:
        response = render(request, 'sso/error.html', {
            'reason': ' '.join(error.messages) if isinstance(error, ValidationError) else str(error),
        }, status=403 if isinstance(error, PermissionDenied) else 400)
    response['Cache-Control'] = 'no-store'
    return response


@require_GET
def providers(request):
    response = JsonResponse({'providers': [
        {'id': str(provider.pk), 'name': provider.name,
         'login_url': reverse('sso:login', args=[provider.pk])}
        for provider in SSOProvider.objects.filter(enabled=True)
    ], 'password_login_enabled': password_login_allowed(request),
        'api_tokens_enabled': api_token_allowed(request)})
    response['Cache-Control'] = 'no-store'
    if direct_lan_allowed(request):
        data = json.loads(response.content)
        data['recovery_login_url'] = reverse('sso:lan-recovery')
        response = JsonResponse(data)
        response['Cache-Control'] = 'no-store'
    return response


@require_GET
def login_page(request):
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
    return JsonResponse([
        {'id': identity.pk, 'provider_id': str(identity.provider_id),
         'provider_name': identity.provider.name, 'enabled': identity_is_usable(identity)}
        for identity in ExternalIdentity.objects.filter(user=request.user).select_related('provider')
    ], safe=False)


@require_POST
def unlink(request, identity_id):
    unlink_identity(request, identity_id)
    return HttpResponse(status=204)
