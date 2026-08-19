from io import StringIO
from pathlib import Path
import tempfile

import mock
from django.apps import apps
from django.core.management import CommandError, call_command
from django.db import connections, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

from django_images.models import Image, Thumbnail
from django_images.services.media_deletion import (
    process_pending_media_deletion,
)
from django_images.test_helpers import TemporaryMediaMixin


def media_snapshot(media_root):
    root = Path(media_root)
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


class MediaDeletionJournalTest(TemporaryMediaMixin, TransactionTestCase):
    @staticmethod
    def _pending_deletions():
        return apps.get_model(
            "django_images", "PendingMediaDeletion"
        ).objects

    def _write_file(self, name, contents):
        path = Path(self.temporary_media.name, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)

    def _create_four_file_image(self, label):
        image = Image.objects.create(
            image="originals/{}/original.png".format(label),
            original_filename="{}.png".format(label),
            width=32,
            height=32,
        )
        names = [image.image.name]
        for size in ("thumbnail", "standard", "square"):
            name = "derivatives/{}/{}.png".format(label, size)
            Thumbnail.objects.create(
                original=image,
                image=name,
                size=size,
                width=32,
                height=32,
            )
            names.append(name)
        for index, name in enumerate(names):
            self._write_file(name, "file-{}".format(index).encode("ascii"))
        return image, media_snapshot(self.temporary_media.name)

    def test_rolled_back_image_delete_preserves_files_without_journal(self):
        image, files_before = self._create_four_file_image("rollback")
        image_id = image.pk

        with self.assertRaisesRegex(RuntimeError, "rollback image delete"):
            with transaction.atomic():
                image.delete()
                raise RuntimeError("rollback image delete")

        restored = Image.objects.get(pk=image_id)
        self.assertEqual(restored.thumbnail_set.count(), 3)
        self.assertEqual(
            media_snapshot(self.temporary_media.name), files_before
        )
        self.assertFalse(self._pending_deletions().exists())

    def test_committed_journal_survives_when_callbacks_do_not_run(self):
        image, files_before = self._create_four_file_image("process-stop")
        image_id = image.pk
        callbacks = []

        def stop_before_callback(callback, using=None):
            callbacks.append((callback, using))

        with mock.patch(
            "django.db.transaction.on_commit",
            side_effect=stop_before_callback,
        ):
            image.delete()

        self.assertFalse(Image.objects.filter(pk=image_id).exists())
        self.assertFalse(
            Thumbnail.objects.filter(original_id=image_id).exists()
        )
        self.assertEqual(len(callbacks), 4)
        self.assertEqual({using for _, using in callbacks}, {"default"})
        self.assertEqual(self._pending_deletions().count(), 4)
        self.assertEqual(
            media_snapshot(self.temporary_media.name), files_before
        )

        call_command(
            "retry_media_deletions", execute=True, stdout=StringIO()
        )

        self.assertFalse(self._pending_deletions().exists())
        self.assertEqual(media_snapshot(self.temporary_media.name), {})

    def test_duplicate_callbacks_share_one_journal_row(self):
        name = "legacy/shared/original.png"
        self._write_file(name, b"shared")
        images = [
            Image.objects.create(
                image=name,
                original_filename="original.png",
                width=32,
                height=32,
            )
            for _ in range(2)
        ]
        callbacks = []

        def stop_before_callback(callback, using=None):
            callbacks.append((callback, using))

        with mock.patch(
            "django.db.transaction.on_commit",
            side_effect=stop_before_callback,
        ):
            Image.objects.filter(
                pk__in=[image.pk for image in images]
            ).delete()

        self.assertEqual(len(callbacks), 2)
        self.assertEqual(
            self._pending_deletions().filter(
                kind="original", name=name
            ).count(),
            1,
        )
        self.assertEqual(
            media_snapshot(self.temporary_media.name), {name: b"shared"}
        )

        call_command(
            "retry_media_deletions", execute=True, stdout=StringIO()
        )

        self.assertFalse(self._pending_deletions().exists())
        self.assertEqual(media_snapshot(self.temporary_media.name), {})

    @mock.patch("django_images.models.IMAGE_AUTO_DELETE", False)
    def test_auto_delete_disabled_creates_no_journal_and_deletes_no_file(self):
        image, files_before = self._create_four_file_image("disabled")

        image.delete()

        self.assertEqual(
            media_snapshot(self.temporary_media.name), files_before
        )
        self.assertFalse(self._pending_deletions().exists())

    def test_retry_removes_journal_when_file_is_already_missing(self):
        pending = self._pending_deletions().create(
            kind="original",
            name="originals/missing/original.png",
        )

        first = StringIO()
        call_command(
            "retry_media_deletions", execute=True, stdout=first
        )
        second = StringIO()
        call_command(
            "retry_media_deletions", execute=True, stdout=second
        )

        self.assertFalse(
            self._pending_deletions().filter(pk=pending.pk).exists()
        )
        self.assertIn("pending=1 processed=1 remaining=0", first.getvalue())
        self.assertIn("pending=0 processed=0 remaining=0", second.getvalue())

    def test_retry_uses_exact_canonical_and_legacy_names_only(self):
        canonical = (
            "originals/12345678-1234-5678-1234-567812345678/original.png"
        )
        legacy = "image/thumbnail/by-md5/a/b/hash/thumbnail.png"
        unrelated = "originals/unrelated/original.png"
        for name in (canonical, legacy, unrelated):
            self._write_file(name, name.encode("ascii"))
        self._pending_deletions().create(
            kind="original", name=canonical
        )
        self._pending_deletions().create(
            kind="thumbnail", name=legacy
        )

        call_command(
            "retry_media_deletions", execute=True, stdout=StringIO()
        )

        self.assertFalse(self._pending_deletions().exists())
        self.assertEqual(
            media_snapshot(self.temporary_media.name),
            {unrelated: unrelated.encode("ascii")},
        )

    def test_retry_limit_processes_only_requested_rows(self):
        names = ["originals/limit/{}/original.png".format(index)
                 for index in range(3)]
        for name in names:
            self._write_file(name, b"pending")
            self._pending_deletions().create(kind="original", name=name)

        call_command(
            "retry_media_deletions",
            execute=True,
            limit=2,
            stdout=StringIO(),
        )

        self.assertEqual(self._pending_deletions().count(), 1)
        self.assertEqual(len(media_snapshot(self.temporary_media.name)), 1)

    def test_retry_rejects_non_positive_limit_without_changes(self):
        pending = self._pending_deletions().create(
            kind="original", name="originals/limit/original.png"
        )

        with self.assertRaisesRegex(CommandError, "invalid_limit"):
            call_command(
                "retry_media_deletions", execute=True, limit=0
            )

        self.assertTrue(
            self._pending_deletions().filter(pk=pending.pk).exists()
        )

    def test_retry_rejects_unknown_database_alias(self):
        with self.assertRaisesRegex(
            CommandError, "unknown_database_alias: missing"
        ):
            call_command("retry_media_deletions", database="missing")

    def test_invalid_storage_names_never_reach_storage(self):
        invalid_names = (
            "../outside",
            "/absolute",
            "",
            "safe/../target",
            "safe\\target",
            "safe//target",
            "./target",
            "safe/\x00target",
            "safe/\ntarget",
        )
        storage = mock.Mock()
        storage.exists.side_effect = AssertionError(
            "storage must not be called"
        )
        image_field = Image._meta.get_field("image")

        with mock.patch.object(image_field, "storage", storage):
            for name in invalid_names:
                with self.subTest(name=repr(name)):
                    pending = self._pending_deletions().create(
                        kind="original", name=name
                    )

                    processed = process_pending_media_deletion(pending.pk)

                    self.assertFalse(processed)
                    pending.refresh_from_db()
                    self.assertEqual(pending.attempts, 1)
                    self.assertEqual(
                        pending.last_error, "InvalidMediaName"
                    )

        storage.exists.assert_not_called()
        storage.delete.assert_not_called()

    def test_invalid_dot_segment_cannot_delete_normalized_filesystem_target(
        self,
    ):
        target = "target.dat"
        self._write_file(target, b"unrelated-target")
        pending = self._pending_deletions().create(
            kind="original", name="safe/../target.dat"
        )
        files_before = media_snapshot(self.temporary_media.name)
        stdout = StringIO()

        call_command(
            "retry_media_deletions", execute=True, stdout=stdout
        )

        pending.refresh_from_db()
        self.assertEqual(pending.attempts, 1)
        self.assertEqual(pending.last_error, "InvalidMediaName")
        self.assertEqual(
            media_snapshot(self.temporary_media.name), files_before
        )
        self.assertIn(
            "pending=1 processed=0 remaining=1", stdout.getvalue()
        )


class MediaDeletionDatabaseAliasTest(
    TemporaryMediaMixin, TransactionTestCase
):
    databases = {"default"}

    @classmethod
    def setUpClass(cls):
        cls.other_database = tempfile.TemporaryDirectory()
        connections.databases["other"] = {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": str(Path(cls.other_database.name, "other.sqlite3")),
        }
        cls.databases = {"default", "other"}
        super(MediaDeletionDatabaseAliasTest, cls).setUpClass()
        other = connections["other"]
        executor = MigrationExecutor(other)
        executor.migrate(executor.loader.graph.leaf_nodes())

    @classmethod
    def tearDownClass(cls):
        try:
            super(MediaDeletionDatabaseAliasTest, cls).tearDownClass()
        finally:
            connections["other"].close()
            del connections["other"]
            del connections.databases["other"]
            cls.other_database.cleanup()
            cls.databases = {"default"}

    @staticmethod
    def _pending_deletions(alias):
        return apps.get_model(
            "django_images", "PendingMediaDeletion"
        ).objects.using(alias)

    def _write_file(self, name, contents):
        path = Path(self.temporary_media.name, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)

    def _create_pending(self, alias, label):
        name = "originals/{}/original.png".format(label)
        contents = "{}-bytes".format(label).encode("ascii")
        self._write_file(name, contents)
        pending = self._pending_deletions(alias).create(
            kind="original", name=name
        )
        return pending, contents

    def test_dry_run_reports_only_selected_database(self):
        default, default_bytes = self._create_pending(
            "default", "default-dry-run"
        )
        other, other_bytes = self._create_pending(
            "other", "other-dry-run"
        )
        stdout = StringIO()

        call_command(
            "retry_media_deletions", database="other", stdout=stdout
        )

        output = stdout.getvalue()
        self.assertIn(other.name, output)
        self.assertNotIn(default.name, output)
        self.assertIn("pending=1 processed=0 remaining=1", output)
        self.assertTrue(
            self._pending_deletions("default")
            .filter(pk=default.pk)
            .exists()
        )
        self.assertTrue(
            self._pending_deletions("other")
            .filter(pk=other.pk)
            .exists()
        )
        self.assertEqual(
            media_snapshot(self.temporary_media.name),
            {
                default.name: default_bytes,
                other.name: other_bytes,
            },
        )

    def test_execute_changes_only_selected_database(self):
        default, default_bytes = self._create_pending(
            "default", "default-execute"
        )
        other, _ = self._create_pending("other", "other-execute")
        stdout = StringIO()

        call_command(
            "retry_media_deletions",
            database="other",
            execute=True,
            stdout=stdout,
        )

        self.assertTrue(
            self._pending_deletions("default")
            .filter(pk=default.pk)
            .exists()
        )
        self.assertFalse(
            self._pending_deletions("other")
            .filter(pk=other.pk)
            .exists()
        )
        self.assertEqual(
            media_snapshot(self.temporary_media.name),
            {default.name: default_bytes},
        )
        self.assertIn(
            "pending=1 processed=1 remaining=0", stdout.getvalue()
        )

    def test_limit_and_remaining_count_use_selected_database(self):
        default, default_bytes = self._create_pending(
            "default", "default-limit"
        )
        other_rows = [
            self._create_pending("other", "other-limit-{}".format(index))
            for index in range(3)
        ]
        stdout = StringIO()

        call_command(
            "retry_media_deletions",
            database="other",
            execute=True,
            limit=2,
            stdout=stdout,
        )

        remaining_other = other_rows[2]
        self.assertEqual(
            list(
                self._pending_deletions("other").values_list(
                    "name", flat=True
                )
            ),
            [remaining_other[0].name],
        )
        self.assertTrue(
            self._pending_deletions("default")
            .filter(pk=default.pk)
            .exists()
        )
        self.assertEqual(
            media_snapshot(self.temporary_media.name),
            {
                default.name: default_bytes,
                remaining_other[0].name: remaining_other[1],
            },
        )
        self.assertIn(
            "pending=2 processed=2 remaining=1", stdout.getvalue()
        )

    def test_crash_recovery_command_keeps_signal_database_alias(self):
        default, default_bytes = self._create_pending(
            "default", "default-crash"
        )
        other_name = "originals/other-crash/original.png"
        other_bytes = b"other-crash-bytes"
        self._write_file(other_name, other_bytes)
        image = Image.objects.using("other").create(
            image=other_name,
            original_filename="original.png",
            width=32,
            height=32,
        )
        callbacks = []

        def stop_before_callback(callback, using=None):
            callbacks.append((callback, using))

        with mock.patch(
            "django.db.transaction.on_commit",
            side_effect=stop_before_callback,
        ):
            image.delete(using="other")

        self.assertEqual({using for _, using in callbacks}, {"other"})
        self.assertTrue(
            self._pending_deletions("other")
            .filter(kind="original", name=other_name)
            .exists()
        )
        self.assertEqual(
            media_snapshot(self.temporary_media.name),
            {
                default.name: default_bytes,
                other_name: other_bytes,
            },
        )

        call_command(
            "retry_media_deletions",
            database="other",
            execute=True,
            stdout=StringIO(),
        )

        self.assertFalse(self._pending_deletions("other").exists())
        self.assertTrue(
            self._pending_deletions("default")
            .filter(pk=default.pk)
            .exists()
        )
        self.assertEqual(
            media_snapshot(self.temporary_media.name),
            {default.name: default_bytes},
        )
