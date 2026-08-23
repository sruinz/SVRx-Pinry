import os
import shutil
import tempfile
import uuid

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase, override_settings


class AssetMetadataMigrationTests(TransactionTestCase):
    migrate_from = ("django_images", "0002_auto_20180826_0814")
    migrate_to = ("django_images", "0005_enforce_image_asset_metadata")
    migrate_latest = [
        ("core", "0015_board_cover_pin"),
        ("django_images", "0006_pending_media_deletion"),
    ]

    def setUp(self):
        super(AssetMetadataMigrationTests, self).setUp()
        self.media_root = tempfile.mkdtemp()
        self.media_override = override_settings(MEDIA_ROOT=self.media_root)
        self.media_override.enable()

    def tearDown(self):
        self._migrate(self.migrate_latest)
        self.media_override.disable()
        shutil.rmtree(self.media_root)
        super(AssetMetadataMigrationTests, self).tearDown()

    def _migrate(self, target):
        targets = target if isinstance(target, list) else [target]
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(targets)
        state = self.executor.loader.project_state(targets)
        return state.apps

    def _write_media_file(self, path, contents):
        full_path = os.path.join(self.media_root, path)
        directory = os.path.dirname(full_path)
        if not os.path.exists(directory):
            os.makedirs(directory)
        with open(full_path, "wb") as media_file:
            media_file.write(contents)

    def _media_snapshot(self):
        snapshot = {}
        for directory, _, filenames in os.walk(self.media_root):
            for filename in filenames:
                full_path = os.path.join(directory, filename)
                path = os.path.relpath(full_path, self.media_root)
                with open(full_path, "rb") as media_file:
                    snapshot[path] = media_file.read()
        return snapshot

    def _create_legacy_files(self):
        files = {
            "8/a/hash-a/image.png": b"legacy original a",
            "8/a/hash-a/thumb.png": b"legacy derivative a",
            "9/b/hash-b/image.png": b"legacy original b",
            "9/b/hash-b/thumb.png": b"legacy derivative b",
        }
        for path, contents in files.items():
            self._write_media_file(path, contents)

    def test_backfills_unique_uuid_without_rewriting_legacy_path(self):
        old_paths = ["8/a/hash-a/image.png", "9/b/hash-b/image.png"]
        self._migrate(self.migrate_from)
        OldImage = self.executor.loader.project_state(
            [self.migrate_from]
        ).apps.get_model("django_images", "Image")
        for path in old_paths:
            OldImage.objects.create(image=path, width=10, height=10)

        self._create_legacy_files()
        self.legacy_media_snapshot = self._media_snapshot()

        apps = self._migrate(self.migrate_to)
        Image = apps.get_model("django_images", "Image")
        rows = list(Image.objects.order_by("id"))

        self.assertEqual([row.image for row in rows], old_paths)
        self.assertEqual({row.original_filename for row in rows}, {"image.png"})
        self.assertEqual(len({row.asset_uuid for row in rows}), 2)
        self.assertTrue(all(row.asset_uuid is not None for row in rows))
        self.assertEqual(self._media_snapshot(), self.legacy_media_snapshot)

    def test_backfill_uses_last_component_for_mixed_path_separators(self):
        self._migrate(self.migrate_from)
        OldImage = self.executor.loader.project_state(
            [self.migrate_from]
        ).apps.get_model("django_images", "Image")
        image = OldImage.objects.create(
            image="legacy\\windows/mixed\\photo.jpg",
            width=10,
            height=10,
        )

        apps = self._migrate(self.migrate_to)
        Image = apps.get_model("django_images", "Image")

        self.assertEqual(
            Image.objects.get(pk=image.pk).original_filename,
            "photo.jpg",
        )

    def test_resumes_partial_backfill_without_replacing_existing_values(self):
        decomposed_name = "\u110b\u1175\u1106\u1175\u110c\u1175.png"
        preserved_uuid = uuid.UUID("12345678-1234-5678-1234-567812345678")
        preserved_filename = "already-recorded.png"

        self._migrate(("django_images", "0003_image_asset_metadata_nullable"))
        apps = self.executor.loader.project_state(
            [("django_images", "0003_image_asset_metadata_nullable")]
        ).apps
        Image = apps.get_model("django_images", "Image")
        Thumbnail = apps.get_model("django_images", "Thumbnail")
        first = Image.objects.create(
            image="partial/a/%s" % decomposed_name,
            width=10,
            height=10,
            asset_uuid=preserved_uuid,
        )
        second = Image.objects.create(
            image="partial/b/%s" % decomposed_name,
            width=10,
            height=10,
            original_filename=preserved_filename,
        )
        first_thumbnail = Thumbnail.objects.create(
            original=first,
            image="partial/a/first-thumb.png",
            size="thumbnail",
            width=5,
            height=5,
        )
        second_thumbnail = Thumbnail.objects.create(
            original=second,
            image="partial/b/second-thumb.png",
            size="thumbnail",
            width=5,
            height=5,
        )

        for path, contents in {
            "partial/a/%s" % decomposed_name: b"partial original first",
            "partial/a/first-thumb.png": b"partial derivative first",
            "partial/b/second-thumb.png": b"partial derivative second",
        }.items():
            self._write_media_file(path, contents)
        self.legacy_media_snapshot = self._media_snapshot()

        apps = self._migrate(self.migrate_to)
        Image = apps.get_model("django_images", "Image")
        Thumbnail = apps.get_model("django_images", "Thumbnail")
        rows = list(Image.objects.order_by("id"))
        first_row, second_row = rows
        first_thumbnail_ids = list(
            Thumbnail.objects.filter(original_id=first.id).values_list("id", flat=True)
        )
        second_thumbnail_ids = list(
            Thumbnail.objects.filter(original_id=second.id).values_list("id", flat=True)
        )

        self.assertEqual(first_row.asset_uuid, preserved_uuid)
        self.assertEqual(first_row.original_filename, "이미지.png")
        self.assertIsNotNone(second_row.asset_uuid)
        self.assertNotEqual(first_row.asset_uuid, second_row.asset_uuid)
        self.assertEqual(second_row.original_filename, preserved_filename)
        self.assertEqual(first_thumbnail_ids, [first_thumbnail.id])
        self.assertEqual(second_thumbnail_ids, [second_thumbnail.id])
        self.assertEqual(self._media_snapshot(), self.legacy_media_snapshot)

        self._migrate(self.migrate_to)
        apps = self.executor.loader.project_state([self.migrate_to]).apps
        Image = apps.get_model("django_images", "Image")
        Thumbnail = apps.get_model("django_images", "Thumbnail")
        rows_after_retry = list(Image.objects.order_by("id"))

        self.assertEqual(rows_after_retry[0].asset_uuid, preserved_uuid)
        self.assertEqual(rows_after_retry[0].original_filename, "이미지.png")
        self.assertEqual(rows_after_retry[1].asset_uuid, second_row.asset_uuid)
        self.assertEqual(rows_after_retry[1].original_filename, preserved_filename)
        self.assertEqual(
            list(
                Thumbnail.objects.filter(original_id=first.id).values_list(
                    "id", flat=True
                )
            ),
            [first_thumbnail.id],
        )
        self.assertEqual(
            list(
                Thumbnail.objects.filter(original_id=second.id).values_list(
                    "id", flat=True
                )
            ),
            [second_thumbnail.id],
        )
        self.assertEqual(self._media_snapshot(), self.legacy_media_snapshot)
