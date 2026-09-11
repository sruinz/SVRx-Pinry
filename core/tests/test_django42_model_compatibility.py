import hashlib
from pathlib import Path
import uuid

from django.db import IntegrityError, connection, models, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TransactionTestCase
from django_images.models import Image as BaseImage, Thumbnail
from django_images.test_helpers import TemporaryMediaMixin

from core.models import BatchImportItem, Board, MediaAsset, Pin
from core.tests.helpers import create_image, create_user
from exports.models import ExportJob


class NullableBooleanModelCompatibilityTests(SimpleTestCase):
    def test_nullable_boolean_fields_use_the_supported_field_type(self):
        for model, field_name in (
            (BatchImportItem, "retryable"),
            (ExportJob, "board_private_snapshot"),
            (ExportJob, "error_retryable"),
        ):
            with self.subTest(model=model.__name__, field=field_name):
                field = model._meta.get_field(field_name)
                self.assertIs(type(field), models.BooleanField)
                self.assertTrue(field.null)
                self.assertTrue(field.blank)


class PinQuerySetDjango42CompatibilityTests(
    TemporaryMediaMixin,
    TransactionTestCase,
):
    def test_ordered_queryset_delete_removes_pin_relations_and_media(self):
        owner = create_user("django42-queryset-delete")
        image = create_image()
        pin = Pin.objects.create(submitter=owner, image=image)
        board = Board.objects.create(
            submitter=owner,
            name="django42-queryset-delete",
            cover_pin=pin,
        )
        board.pins.add(pin)
        content = Path(
            self.temporary_media.name,
            image.image.name,
        ).read_bytes()
        MediaAsset.objects.create(
            submitter=owner,
            image=image,
            content_sha256=hashlib.sha256(content).hexdigest(),
        )
        media_paths = {
            image.image.name,
            *image.thumbnail_set.values_list("image", flat=True),
        }

        Pin.objects.filter(pk=pin.pk).order_by("-published").delete()

        board.refresh_from_db()
        self.assertIsNone(board.cover_pin_id)
        self.assertFalse(board.pins.filter(pk=pin.pk).exists())
        self.assertFalse(Pin.objects.filter(pk=pin.pk).exists())
        self.assertFalse(BaseImage.objects.filter(pk=image.pk).exists())
        self.assertFalse(
            Thumbnail.objects.filter(original_id=image.pk).exists()
        )
        for relative_path in media_paths:
            self.assertFalse(
                Path(self.temporary_media.name, relative_path).exists()
            )


class NullableBooleanMigrationTests(TransactionTestCase):
    migrate_from = [
        ("django_images", "0007_startup_validation_state"),
        ("core", "0016_board_display_order"),
        ("exports", "0002_exporttarget_identity_snapshot"),
    ]
    migrate_to = [
        ("django_images", "0007_startup_validation_state"),
        ("core", "0017_alter_batchimportitem_retryable"),
        (
            "exports",
            "0004_remove_exportjob_export_failed_state_valid_and_more",
        ),
    ]

    def setUp(self):
        super(NullableBooleanMigrationTests, self).setUp()
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.migrate_from)
        old_apps = self.executor.loader.project_state(self.migrate_from).apps
        user_model = old_apps.get_model("users", "User")
        batch_item_model = old_apps.get_model("core", "BatchImportItem")
        export_job_model = old_apps.get_model("exports", "ExportJob")
        owner = user_model.objects.create(username="nullable-migration")
        self.expected = (None, False, True)
        self.batch_item_ids = []
        self.export_job_ids = []
        for index, value in enumerate(self.expected):
            batch_item = batch_item_model.objects.create(
                submitter_id=owner.pk,
                batch_id=uuid.uuid4(),
                client_item_id=uuid.uuid4(),
                request_fingerprint=str(index) * 64,
                state="pending",
                retryable=value,
            )
            export_job = export_job_model.objects.create(
                owner_id=owner.pk,
                scope="pins",
                board_private_snapshot=value,
                error_retryable=value,
            )
            self.batch_item_ids.append(batch_item.pk)
            self.export_job_ids.append(export_job.pk)

        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.migrate_to)
        self.apps = self.executor.loader.project_state(self.migrate_to).apps

    def tearDown(self):
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.executor.loader.graph.leaf_nodes())
        super(NullableBooleanMigrationTests, self).tearDown()

    def test_null_false_and_true_values_survive_the_field_alterations(self):
        batch_item_model = self.apps.get_model("core", "BatchImportItem")
        export_job_model = self.apps.get_model("exports", "ExportJob")

        self.assertEqual(
            tuple(
                batch_item_model.objects.get(pk=pk).retryable
                for pk in self.batch_item_ids
            ),
            self.expected,
        )
        self.assertEqual(
            tuple(
                export_job_model.objects.get(pk=pk).board_private_snapshot
                for pk in self.export_job_ids
            ),
            self.expected,
        )
        self.assertEqual(
            tuple(
                export_job_model.objects.get(pk=pk).error_retryable
                for pk in self.export_job_ids
            ),
            self.expected,
        )

    def test_failed_job_contract_remains_enforced_after_migration(self):
        export_job_model = self.apps.get_model("exports", "ExportJob")
        owner_id = export_job_model.objects.get(
            pk=self.export_job_ids[0]
        ).owner_id

        export_job_model.objects.create(
            owner_id=owner_id,
            scope="pins",
            state="failed",
            error_code="source_missing",
            error_class="fatal",
            error_retryable=False,
        )
        for invalid_fields in (
            {},
            {
                "error_code": "source_missing",
                "error_class": "fatal",
                "error_retryable": True,
            },
        ):
            with self.subTest(invalid_fields=invalid_fields):
                with self.assertRaises(IntegrityError):
                    with transaction.atomic():
                        export_job_model.objects.create(
                            owner_id=owner_id,
                            scope="pins",
                            state="failed",
                            **invalid_fields
                        )
