from django.conf import settings
from django.http import HttpResponseForbidden
from django.utils.deprecation import MiddlewareMixin


class Public(MiddlewareMixin):

    acceptable_paths = (
        "/api/v2/version/",
    )
    acceptable_prefixes = (
        "/api/v2/profile/",
    )

    def process_request(self, request):
        if settings.PUBLIC is False and not request.user.is_authenticated:
            accepted = request.path in self.acceptable_paths or any(
                request.path.startswith(prefix)
                for prefix in self.acceptable_prefixes
            )
            if not accepted:
                return HttpResponseForbidden()
