from io import StringIO
from pathlib import Path

import mock
from django.apps import apps
from django.core.management import CommandError, call_command
from django.db import transaction
from django.test import TransactionTestCase

from django_images.models import Image, Thumbnail
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
