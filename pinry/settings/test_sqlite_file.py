import os
import tempfile
import uuid

from .development import *  # noqa: F401,F403


base_database_name = os.path.join(
    tempfile.gettempdir(),
    "pinry-custom-base-{}-{}.sqlite3".format(
        os.getpid(),
        uuid.uuid4().hex,
    ),
)
test_database_name = os.environ.get("PINRY_TEST_DB_PATH")
if not test_database_name:
    test_database_name = os.path.join(
        tempfile.gettempdir(),
        "pinry-custom-test-{}-{}.sqlite3".format(
            os.getpid(),
            uuid.uuid4().hex,
        ),
    )

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": base_database_name,
        "OPTIONS": {"timeout": 0.5},
        "TEST": {"NAME": test_database_name},
    }
}
