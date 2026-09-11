from .docker import *  # noqa: F401,F403


DEBUG = False
ROOT_URLCONF = 'users.recovery_urls'
WSGI_APPLICATION = 'pinry.recovery_wsgi.application'
MIDDLEWARE = [
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'users.recovery.RecoveryBoundaryMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
]
AUTHENTICATION_BACKENDS = ['users.recovery.RecoveryBackend']
SESSION_COOKIE_NAME = 'pinry_recovery_session'
SESSION_COOKIE_DOMAIN = None
SESSION_COOKIE_PATH = '/recovery/'
SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Strict'
SESSION_COOKIE_AGE = 900
SESSION_SAVE_EVERY_REQUEST = False
CSRF_COOKIE_NAME = 'pinry_recovery_csrf'
CSRF_COOKIE_DOMAIN = None
CSRF_COOKIE_PATH = '/recovery/'
CSRF_COOKIE_SECURE = True
CSRF_COOKIE_SAMESITE = 'Strict'
CSRF_TRUSTED_ORIGINS = []
USE_X_FORWARDED_HOST = False
SECURE_PROXY_SSL_HEADER = ('HTTP_X_PINRY_RECOVERY_TLS', 'https')
