import ipaddress
from urllib.parse import urlsplit

from django.contrib.auth import login
from django.http import HttpResponseForbidden
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from users.models import PRIVATE_NETWORKS


def direct_lan_allowed(request):
    try:
        # 앱 포트는 루프백 전용이다. 컨테이너 nginx가 덮어쓴 경계 정보만 사용한다.
        upstream = ipaddress.ip_address(request.META.get('REMOTE_ADDR', ''))
        peer = ipaddress.ip_address(request.META.get('HTTP_X_PINRY_DIRECT_PEER', ''))
        host = ipaddress.ip_address(urlsplit('//' + request.get_host()).hostname)
        return (upstream.is_loopback and request.META.get('HTTP_X_PINRY_PROXIED') == '0'
                and any(peer in network for network in PRIVATE_NETWORKS)
                and any(host in network for network in PRIVATE_NETWORKS))
    except (ValueError, TypeError):
        return False


@never_cache
@require_http_methods(['GET', 'POST'])
def recovery_login(request):
    if not direct_lan_allowed(request):
        return HttpResponseForbidden('관리자 복구 로그인은 내부망 직접 접속에서만 사용할 수 있습니다.')
    if request.method == 'GET':
        return render(request, 'recovery/login.html')
    from users.recovery import RecoveryBackend, _limited_authenticate
    request.recovery_peer = request.META['HTTP_X_PINRY_DIRECT_PEER']
    request.recovery_deployment = {'fingerprint': 'lan-direct-v1'}
    user, limited = _limited_authenticate(
        request, request.POST.get('username', '')[:150], request.POST.get('password', ''),
        authenticator=RecoveryBackend().authenticate,
    )
    if not user:
        return render(request, 'recovery/login.html', {'error': '관리자 계정과 비밀번호를 확인하세요.'},
                      status=429 if limited else 403)
    request.session['auth_method'] = 'lan-recovery'
    login(request, user, backend='users.sso.authentication.SSOBackend')
    request.session['auth_method'] = 'lan-recovery'
    request.session.set_expiry(900)
    return redirect('/admin/')
