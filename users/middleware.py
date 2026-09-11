from django.conf import settings
from django.contrib.auth import logout
from django.http import HttpResponseForbidden
from django.urls import Resolver404, resolve
from django.utils.deprecation import MiddlewareMixin
from rest_framework.authentication import TokenAuthentication
from rest_framework.exceptions import AuthenticationFailed

from users.models import ExternalIdentity, SSOProvider


class SSOSessionMiddleware(MiddlewareMixin):
    def process_request(self, request):
        if request.session.get('auth_method') != 'sso':
            return
        provider_id = request.session.get('sso_provider_id')
        revision = request.session.get('sso_provider_revision')
        if (not request.user.is_authenticated or not SSOProvider.objects.filter(
            pk=provider_id, revision=revision, enabled=True,
        ).exists() or not ExternalIdentity.objects.filter(
            user=request.user, provider_id=provider_id,
        ).exists()):
            logout(request)


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
            authenticated = TokenAuthentication().authenticate(request)
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
                    'sso:providers', 'sso:login', 'sso:callback',
                }
            except Resolver404:
                public_sso = False
            accepted = request.path in self.acceptable_paths or any(
                request.path.startswith(prefix)
                for prefix in self.acceptable_prefixes
            )
            if not accepted and not public_sso and not self.authenticate_api_token(request):
                return HttpResponseForbidden()
