from django.conf import settings
from django.http import HttpResponseForbidden
from django.utils.deprecation import MiddlewareMixin
from rest_framework.authentication import TokenAuthentication
from rest_framework.exceptions import AuthenticationFailed


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
            accepted = request.path in self.acceptable_paths or any(
                request.path.startswith(prefix)
                for prefix in self.acceptable_prefixes
            )
            if not accepted and not self.authenticate_api_token(request):
                return HttpResponseForbidden()
