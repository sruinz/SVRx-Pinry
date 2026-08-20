from django.core.exceptions import FieldDoesNotExist
from django.db import connection
from django.test import TestCase

from core.models import Pin


class PinSchemaMigrationTest(TestCase):
    def test_latest_pin_model_and_table_have_no_trash_state(self):
        with self.assertRaises(FieldDoesNotExist):
            Pin._meta.get_field("trashed_at")
        with connection.cursor() as cursor:
            columns = {
                column.name
                for column in connection.introspection.get_table_description(
                    cursor, Pin._meta.db_table
                )
            }
        self.assertNotIn("trashed_at", columns)
