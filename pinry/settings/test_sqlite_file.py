import copy
import os
import tempfile
import uuid

from .development import *  # noqa: F401,F403


DATABASES = copy.deepcopy(DATABASES)  # noqa: F405
test_database_name = os.environ.get("PINRY_TEST_DB_PATH")
if not test_database_name:
    test_database_name = os.path.join(
        tempfile.gettempdir(),
        "pinry-custom-test-{}-{}.sqlite3".format(
            os.getpid(),
            uuid.uuid4().hex,
        ),
    )

DATABASES["default"]["TEST"] = {"NAME": test_database_name}  # noqa: F405
DATABASES["default"]["OPTIONS"] = {"timeout": 0.5}  # noqa: F405
