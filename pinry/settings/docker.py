import logging

from .base import *


# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.environ.get('SECRET_KEY', "PLEASE_REPLACE_ME")

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = False

# SECURITY WARNING: use your actual domain name in production!
ALLOWED_HOSTS = ['*']

# Database
# https://docs.djangoproject.com/en/1.10/ref/settings/#databases

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': 'postgres',
        'USER': 'postgres',
        'HOST': 'db',
        'PORT': 5432,
    }
}

USE_X_FORWARDED_HOST = True

# HTTPS 종료 프록시의 공개 Origin은 운영자가 명시한 값만 허용한다.
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get('PINRY_CSRF_TRUSTED_ORIGINS', '').split(',')
    if origin.strip()
]

REST_FRAMEWORK['DEFAULT_RENDERER_CLASSES'] = [
    'rest_framework.renderers.JSONRenderer',
]

# should not ignore import error in production, local_settings is required
from .local_settings import *

if not SECRET_KEY or SECRET_KEY == "PLEASE_REPLACE_ME":
    logging.warning("No usable SECRET_KEY is configured.")
