import platform
import re

import django
import PIL
import rest_framework

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET


SOURCE_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")


def normalize_source_commit(value):
    if (
        type(value) is not str
        or SOURCE_COMMIT_PATTERN.fullmatch(value) is None
    ):
        return {
            "source_commit": "development",
            "display_version": "development",
        }
    return {
        "source_commit": value,
        "display_version": value[:12],
    }


@never_cache
@require_GET
def version(request):
    payload = normalize_source_commit(settings.PINRY_SOURCE_COMMIT)
    if request.user.is_authenticated:
        payload["dependencies"] = {
            "python": platform.python_version(),
            "django": django.get_version(),
            "drf": rest_framework.VERSION,
            "pillow": PIL.__version__,
        }
    return JsonResponse(payload)
