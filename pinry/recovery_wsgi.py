import os

from django.core.wsgi import get_wsgi_application

os.environ['DJANGO_SETTINGS_MODULE'] = 'pinry.settings.recovery'
_django_application = get_wsgi_application()


def application(environ, start_response):
    # 별도 UNIX socket WSGI에서만 설정하는 서버 환경 값이다.
    environ['pinry.recovery_listener'] = True
    return _django_application(environ, start_response)
