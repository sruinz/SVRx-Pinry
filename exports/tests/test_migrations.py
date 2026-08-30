import uuid

from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class ExportInitialMigrationTests(TransactionTestCase):
    migrate_to = [("exports", "0001_initial")]

    def setUp(self):
        super(ExportInitialMigrationTests, self).setUp()
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.migrate_to)
        self.apps = self.executor.loader.project_state(self.migrate_to).apps

    def tearDown(self):
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.executor.loader.graph.leaf_nodes())
        super(ExportInitialMigrationTests, self).tearDown()

    def test_initial_schema_has_all_models_and_user_relationships(self):
        expected = {
            "ExportJob", "ExportTarget", "ExportBlob", "ExportItem",
            "ExportSlot", "ExportWorkerLease", "ExportAttempt",
            "ExportAttemptFile",
        }
        actual = {
            model.__name__ for model in self.apps.get_models()
            if model._meta.app_label == "exports"
        }
        self.assertEqual(actual, expected)

        job = self.apps.get_model("exports", "ExportJob")
        slot = self.apps.get_model("exports", "ExportSlot")
        self.assertEqual(job._meta.get_field("owner").remote_field.model._meta.label, "auth.User")
        self.assertEqual(slot._meta.get_field("owner").remote_field.model._meta.label, "auth.User")

    def test_database_rejects_invalid_singleton_enum_and_receipt_rows(self):
        user = self.apps.get_model("auth", "User").objects.create(
            username="migration-owner",
        )
        job_model = self.apps.get_model("exports", "ExportJob")
        target_model = self.apps.get_model("exports", "ExportTarget")
        lease_model = self.apps.get_model("exports", "ExportWorkerLease")
        attempt_model = self.apps.get_model("exports", "ExportAttempt")
        file_model = self.apps.get_model("exports", "ExportAttemptFile")
        job = job_model.objects.create(owner=user, scope="pins")
        target_model.objects.create(job=job, position=0, pin_id=11)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                target_model.objects.create(job=job, position=0, pin_id=11)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                lease_model.objects.create(id=2)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                job_model.objects.create(owner=user, scope="invalid")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                job_model.objects.create(owner=user, scope="pins", state="invalid")

        attempt = attempt_model.objects.create(
            job=job,
            attempt_generation=0,
            lease_uuid=uuid.uuid4(),
            state="writing",
            relative_path="attempts/0",
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                file_model.objects.create(
                    attempt=attempt,
                    kind="archive",
                    state="writing",
                    receipt_level="open",
                    relative_path="attempts/0/export.zip.part",
                    receipt_dev=1,
                    receipt_ino=None,
                    receipt_uid=1,
                    receipt_gid=1,
                    receipt_mode=0o600,
                    receipt_nlink=1,
                )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                file_model.objects.create(
                    attempt=attempt,
                    kind="invalid",
                    state="writing",
                    receipt_level="open",
                    relative_path="attempts/0/invalid.part",
                    receipt_dev=1,
                    receipt_ino=2,
                    receipt_uid=1,
                    receipt_gid=1,
                    receipt_mode=0o600,
                    receipt_nlink=1,
                )

    def test_user_deletion_keeps_job_and_receipt_history(self):
        user_model = self.apps.get_model("auth", "User")
        job_model = self.apps.get_model("exports", "ExportJob")
        attempt_model = self.apps.get_model("exports", "ExportAttempt")
        file_model = self.apps.get_model("exports", "ExportAttemptFile")
        user = user_model.objects.create(username="deleted-owner")
        job = job_model.objects.create(owner=user, scope="pins")
        attempt = attempt_model.objects.create(
            job=job,
            attempt_generation=0,
            lease_uuid=uuid.uuid4(),
            state="writing",
            relative_path="attempts/0",
        )
        receipt = file_model.objects.create(
            attempt=attempt,
            kind="archive",
            state="writing",
            receipt_level="open",
            relative_path="attempts/0/export.zip.part",
            receipt_dev=1,
            receipt_ino=2,
            receipt_uid=3,
            receipt_gid=4,
            receipt_mode=0o600,
            receipt_nlink=1,
        )

        user.delete()

        self.assertIsNone(job_model.objects.get(pk=job.pk).owner_id)
        self.assertTrue(file_model.objects.filter(pk=receipt.pk).exists())
