from django.contrib.auth.backends import BaseBackend
from rest_framework.authentication import BasicAuthentication, TokenAuthentication, get_authorization_header
from rest_framework.exceptions import AuthenticationFailed

from users.models import User
from users.sso.policy import api_token_allowed, password_login_allowed


class SSOBackend(BaseBackend):
    def get_user(self, user_id):
        return User.objects.filter(pk=user_id, is_active=True).first()


class PolicyBasicAuthentication(BasicAuthentication):
    def authenticate(self, request):
        parts = get_authorization_header(request).split()
        if parts and parts[0].lower() == b'basic':
            if not password_login_allowed(request):
                raise AuthenticationFailed('비밀번호 로그인이 비활성화되어 있습니다.')
        return super().authenticate(request)


class PolicyTokenAuthentication(TokenAuthentication):
    def authenticate(self, request):
        parts = get_authorization_header(request).split()
        if parts and parts[0].lower() == b'token':
            if not api_token_allowed(request):
                raise AuthenticationFailed('API 토큰 인증이 비활성화되어 있습니다.')
        return super().authenticate(request)
