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

    def test_database_enforces_reviewed_state_receipt_and_sha_contracts(self):
        user = self.apps.get_model("auth", "User").objects.create(username="review-owner")
        job_model = self.apps.get_model("exports", "ExportJob")
        lease_model = self.apps.get_model("exports", "ExportWorkerLease")
        attempt_model = self.apps.get_model("exports", "ExportAttempt")
        file_model = self.apps.get_model("exports", "ExportAttemptFile")
        blob_model = self.apps.get_model("exports", "ExportBlob")
        ready = {
            "completed_at": "2026-08-30T12:00:00Z", "expires_at": "2026-08-31T12:00:00Z",
            "ready_relative_path": "ready/export.zip", "ready_display_name": "export.zip",
            "ready_size": 10, "ready_sha256": "a" * 64, "ready_dev": 1,
            "ready_ino": 2, "ready_uid": 3, "ready_gid": 4, "ready_mode": 384,
            "ready_nlink": 1, "ready_mtime_ns": 5, "ready_ctime_ns": 6,
        }
        lease_model.objects.create(id=1, health_state="ready")
        lease_model.objects.filter(pk=1).update(health_state="stopped")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                lease_model.objects.filter(pk=1).update(health_state="healthy")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                job_model.objects.create(owner=user, scope="pins", state="failed")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                job_model.objects.create(owner=user, scope="pins", state="failed", error_code="export_not_ready", error_class="retryable", error_retryable=True)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                job_model.objects.create(owner=user, scope="pins", **ready)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                job_model.objects.create(owner=user, scope="pins", state="expired", ready_cleanup_state="pending")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                job_model.objects.create(owner=user, scope="pins", state="expired", ready_cleanup_state="cleaned", **ready)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                job_model.objects.create(owner=user, scope="pins", state="complete", ready_cleanup_state="retained", **dict(ready, ready_sha256="NOT-A-SHA"))
        job = job_model.objects.create(owner=user, scope="pins")
        attempt = attempt_model.objects.create(job=job, attempt_generation=0, lease_uuid=uuid.uuid4(), state="writing", relative_path="attempts/0")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                attempt_model.objects.create(job=job, attempt_generation=1, lease_uuid=uuid.uuid4(), state="cleaned", relative_path="attempts/1")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                file_model.objects.create(attempt=attempt, kind="quarantine", state="closed", receipt_level="open", relative_path="attempts/0/q", intent_relative_path="dest", receipt_dev=1, receipt_ino=2, receipt_uid=3, receipt_gid=4, receipt_mode=384, receipt_nlink=1)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                blob_model.objects.create(job=job, snapshot_generation=uuid.uuid4(), source_media_asset_id=1, source_image_id=1, source_relative_path="blob", receipt_sha256="UPPER")

    def test_database_accepts_terminal_ready_and_tombstone_matrix(self):
        user = self.apps.get_model("auth", "User").objects.create(username="matrix-owner")
        job_model = self.apps.get_model("exports", "ExportJob")
        attempt_model = self.apps.get_model("exports", "ExportAttempt")
        file_model = self.apps.get_model("exports", "ExportAttemptFile")
        errors = (
            ("source_missing", "fatal", False), ("source_changed", "retryable", True),
            ("source_unsafe", "operator_action_required", False), ("snapshot_failed", "retryable", True),
            ("insufficient_space", "operator_action_required", True), ("archive_failed", "retryable", True),
            ("permission_changed", "fatal", False), ("all_items_revoked", "fatal", False),
            ("worker_repeated_failure", "operator_action_required", True), ("export_storage_unsafe", "operator_action_required", False),
        )
        for code, error_class, retryable in errors:
            with self.subTest(code=code):
                job_model.objects.create(owner=user, scope="pins", state="failed", error_code=code, error_class=error_class, error_retryable=retryable)
        for code in ("invalid_target", "active_export_exists", "export_not_ready", "export_worker_unavailable", "export_temporarily_unavailable", "export_expired"):
            with self.subTest(http_code=code):
                with self.assertRaises(IntegrityError):
                    with transaction.atomic():
                        job_model.objects.create(owner=user, scope="pins", state="failed", error_code=code, error_class="retryable", error_retryable=True)
        ready = {"ready_relative_path": "ready/matrix.zip", "ready_display_name": "matrix.zip", "ready_size": 10, "ready_sha256": "a" * 64, "ready_dev": 1, "ready_ino": 2, "ready_uid": 3, "ready_gid": 4, "ready_mode": 384, "ready_nlink": 1, "ready_mtime_ns": 5, "ready_ctime_ns": 6}
        job_model.objects.create(owner=user, scope="pins", state="expired", ready_cleanup_state="pending", **ready)
        job_model.objects.create(owner=user, scope="pins", state="expired", ready_cleanup_state="blocked", **dict(ready, ready_relative_path="ready/blocked.zip"))
        job = job_model.objects.create(owner=user, scope="pins", state="expired", ready_cleanup_state="cleaned")
        attempt = attempt_model.objects.create(job=job, attempt_generation=0, lease_uuid=uuid.uuid4(), state="cleaned", relative_path="attempts/0", dir_dev=1, dir_ino=2, dir_uid=3, dir_gid=4, dir_mode=448)
        full = {"receipt_level": "full", "receipt_dev": 1, "receipt_ino": 2, "receipt_uid": 3, "receipt_gid": 4, "receipt_mode": 384, "receipt_nlink": 1, "receipt_size": 10, "receipt_mtime_ns": 5, "receipt_ctime_ns": 6}
        file_model.objects.create(attempt=attempt, kind="archive", state="closed", relative_path="attempts/0/closed", **full)
        file_model.objects.create(attempt=attempt, kind="quarantine", state="closed", relative_path="attempts/0/quarantine", **full)
        file_model.objects.create(attempt=attempt, kind="archive", state="cleaned", receipt_level="open", relative_path="attempts/0/open", receipt_dev=1, receipt_ino=2, receipt_uid=3, receipt_gid=4, receipt_mode=384, receipt_nlink=1)
        file_model.objects.create(attempt=attempt, kind="archive", state="cleaned", relative_path="attempts/0/full", **dict(full, receipt_sha256=None))
        file_model.objects.create(attempt=attempt, kind="archive", state="cleaned", relative_path="attempts/0/verified", **dict(full, receipt_sha256="b" * 64))

    def test_database_rejects_non_strict_sha_values_but_allows_null(self):
        user = self.apps.get_model("auth", "User").objects.create(username="sha-owner")
        job_model = self.apps.get_model("exports", "ExportJob")
        blob_model = self.apps.get_model("exports", "ExportBlob")
        attempt_model = self.apps.get_model("exports", "ExportAttempt")
        file_model = self.apps.get_model("exports", "ExportAttemptFile")
        ready = {"completed_at": "2026-08-30T12:00:00Z", "expires_at": "2026-08-31T12:00:00Z", "ready_relative_path": "ready/sha.zip", "ready_display_name": "sha.zip", "ready_size": 10, "ready_dev": 1, "ready_ino": 2, "ready_uid": 3, "ready_gid": 4, "ready_mode": 384, "ready_nlink": 1, "ready_mtime_ns": 5, "ready_ctime_ns": 6}
        invalid = ("a" * 64 + "\n", "a" * 63, "a" * 65, "A" * 64, "g" * 64, "a" * 62 + "\r\n")
        for value in invalid:
            with self.subTest(job_sha=repr(value)):
                with self.assertRaises(IntegrityError):
                    with transaction.atomic():
                        job_model.objects.create(owner=user, scope="pins", state="complete", ready_cleanup_state="retained", **dict(ready, ready_sha256=value))
        job = job_model.objects.create(owner=user, scope="pins")
        blob_model.objects.create(job=job, snapshot_generation=uuid.uuid4(), source_media_asset_id=1, source_image_id=1, source_relative_path="blob", receipt_sha256=None)
        attempt = attempt_model.objects.create(job=job, attempt_generation=0, lease_uuid=uuid.uuid4(), state="writing", relative_path="attempts/0")
        full = {"attempt": attempt, "kind": "archive", "state": "closed", "receipt_level": "full", "receipt_dev": 1, "receipt_ino": 2, "receipt_uid": 3, "receipt_gid": 4, "receipt_mode": 384, "receipt_nlink": 1, "receipt_size": 10, "receipt_mtime_ns": 5, "receipt_ctime_ns": 6}
        file_model.objects.create(relative_path="attempts/0/null", receipt_sha256=None, **full)
        for index, value in enumerate(invalid):
            with self.subTest(file_sha=repr(value)):
                with self.assertRaises(IntegrityError):
                    with transaction.atomic():
                        file_model.objects.create(relative_path="attempts/0/{}".format(index), receipt_sha256=value, **full)
