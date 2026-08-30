import os

from .base import *  # noqa: F401,F403


def _required_environment(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError("{} is required".format(name))
    return value


SECRET_KEY = "pinry-postgresql-test-only-secret-key"
DEBUG = False
ALLOWED_HOSTS = ["testserver"]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "HOST": _required_environment("PINRY_TEST_POSTGRES_HOST"),
        "PORT": _required_environment("PINRY_TEST_POSTGRES_PORT"),
        "NAME": _required_environment("PINRY_TEST_POSTGRES_NAME"),
        "USER": _required_environment("PINRY_TEST_POSTGRES_USER"),
        "PASSWORD": _required_environment("PINRY_TEST_POSTGRES_PASSWORD"),
    }
}
