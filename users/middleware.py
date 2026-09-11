from django.conf import settings
from django.http import HttpResponseForbidden, HttpResponseNotFound
from django.urls import Resolver404, resolve
from django.utils.deprecation import MiddlewareMixin
from rest_framework.exceptions import AuthenticationFailed

from users.sso.authentication import PolicyTokenAuthentication
from users.sso.policy import enforce_session_policy, password_login_allowed, policy_request


class SSOSessionMiddleware(MiddlewareMixin):
    def __call__(self, request):
        # 요청 인자를 받지 않는 사용자 저장 signal도 같은 정책 스냅샷을 사용한다.
        token = policy_request.set(request)
        try:
            return super().__call__(request)
        finally:
            policy_request.reset(token)

    def process_request(self, request):
        if request.path == '/recovery' or request.path.startswith('/recovery/'):
            return HttpResponseNotFound()
        enforce_session_policy(request)
        if request.path == '/admin/login/':
            request.password_login_enabled = password_login_allowed(request)
        if (request.method == 'POST' and request.path in ('/admin/login/', '/api-auth/login/')
                and not password_login_allowed(request)):
            return HttpResponseForbidden('비밀번호 로그인이 비활성화되어 있습니다. SSO를 이용해 주세요.')


class Public(MiddlewareMixin):

    acceptable_paths = (
        "/api/v2/version/",
    )
    acceptable_prefixes = (
        "/api/v2/profile/",
    )

    def authenticate_api_token(self, request):
        if not request.path.startswith("/api/v2/"):
            return False
        try:
            authenticated = PolicyTokenAuthentication().authenticate(request)
        except AuthenticationFailed:
            return False
        if authenticated is None:
            return False
        request.user, request.auth = authenticated
        return True

    def process_request(self, request):
        if settings.PUBLIC is False and not request.user.is_authenticated:
            try:
                public_sso = resolve(request.path_info).view_name in {
                    'sso:providers', 'sso:login', 'sso:callback', 'login-page',
                }
            except Resolver404:
                public_sso = False
            accepted = request.path in self.acceptable_paths or any(
                request.path.startswith(prefix)
                for prefix in self.acceptable_prefixes
            )
            if not accepted and not public_sso and not self.authenticate_api_token(request):
                return HttpResponseForbidden()
