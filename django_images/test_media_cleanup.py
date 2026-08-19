from datetime import timedelta
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import tempfile
import time
from unittest import mock
import uuid

from django.core.management import CommandError, call_command
from django.test import TransactionTestCase, override_settings
from PIL import Image as PILImage

from django_images.models import Image, Thumbnail


def make_image_bytes(color):
    image = BytesIO()
    PILImage.new("RGB", (32, 32), color).save(image, format="PNG")
    return image.getvalue()


class TemporaryCleanupRootsMixin(object):
    def setUp(self):
        super(TemporaryCleanupRootsMixin, self).setUp()
        self.temporary_media = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_media.cleanup)
        self.temporary_data = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_data.cleanup)
        self.settings_override = override_settings(
            MEDIA_ROOT=self.temporary_media.name,
            PINRY_DATA_ROOT=self.temporary_data.name,
            IMAGE_AUTO_DELETE=False,
        )
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)

    def _write_media(self, relative_name, content):
        path = Path(self.temporary_media.name, relative_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def _media_files(self):
        root = Path(self.temporary_media.name)
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file() and not path.is_symlink()
        }


class LegacyMediaCleanupCommandTest(
    TemporaryCleanupRootsMixin, TransactionTestCase
):
    def setUp(self):
        super(LegacyMediaCleanupCommandTest, self).setUp()
        self.manifest_path = os.path.join(
            self.temporary_data.name, "migrations", "cleanup.jsonl"
        )
        asset_uuid = uuid.UUID("12345678-1234-5678-1234-567812345678")
        original_name = "legacy/first/original.jpg"
        self._write_media(original_name, make_image_bytes("red"))
        self.image = Image.objects.create(
            image=original_name,
            asset_uuid=asset_uuid,
            original_filename="first.jpg",
            width=32,
            height=32,
        )
        for size, color in (("thumbnail", "green"), ("square", "blue")):
            name = "legacy/first/{}.jpg".format(size)
            self._write_media(name, make_image_bytes(color))
            Thumbnail.objects.create(
                original=self.image,
                image=name,
                size=size,
                width=32,
                height=32,
            )
        self.legacy_files = self._media_files()
        self.legacy_paths = tuple(sorted(self.legacy_files))
        call_command(
            "migrate_media",
            execute=True,
            manifest=self.manifest_path,
            stdout=StringIO(),
        )
        self.canonical_files = {
            name: content
            for name, content in self._media_files().items()
            if name not in self.legacy_files
        }
        self.database_paths = self._database_paths()

    def _database_paths(self):
        self.image.refresh_from_db()
        paths = {"original": self.image.image.name}
        paths.update(
            {
                thumbnail.size: thumbnail.image.name
                for thumbnail in self.image.thumbnail_set.order_by("id")
            }
        )
        return paths

    def _execute_options(self):
        return {
            "manifest": self.manifest_path,
            "execute": True,
            "confirm_full_data_backup": True,
            "confirm_pinry_immich_validated": True,
            "confirm_db_rollback_needs_media_backup": True,
        }

    def _manifest_events(self):
        return [
            json.loads(line)
            for line in Path(self.manifest_path).read_text().splitlines()
        ]

    def test_legacy_cleanup_requires_every_acknowledgement(self):
        options = self._execute_options()
        acknowledgement_names = (
            "confirm_full_data_backup",
            "confirm_pinry_immich_validated",
            "confirm_db_rollback_needs_media_backup",
        )
        files_before = self._media_files()

        for missing in acknowledgement_names:
            with self.subTest(missing=missing):
                incomplete = dict(options)
                incomplete.pop(missing)
                with self.assertRaisesRegex(
                    CommandError, "cleanup_acknowledgements_required"
                ):
                    call_command("cleanup_legacy_media", **incomplete)
                self.assertEqual(self._media_files(), files_before)
                self.assertEqual(self._database_paths(), self.database_paths)

    def test_legacy_cleanup_dry_run_reports_without_changing_files_or_db(self):
        files_before = self._media_files()
        stdout = StringIO()

        call_command(
            "cleanup_legacy_media",
            manifest=self.manifest_path,
            stdout=stdout,
        )

        self.assertEqual(self._media_files(), files_before)
        self.assertEqual(self._database_paths(), self.database_paths)
        for old_path in self.legacy_paths:
            self.assertIn(old_path, stdout.getvalue())

    def test_legacy_cleanup_execute_unlinks_only_verified_old_files(self):
        call_command("cleanup_legacy_media", **self._execute_options())

        self.assertEqual(self._media_files(), self.canonical_files)
        self.assertEqual(self._database_paths(), self.database_paths)
        for path, content in self.canonical_files.items():
            self.assertEqual(
                Path(self.temporary_media.name, path).read_bytes(), content
            )

    def test_legacy_cleanup_hash_mismatch_deletes_nothing(self):
        canonical_original = self.database_paths["original"]
        self._write_media(canonical_original, make_image_bytes("purple"))
        files_before = self._media_files()

        with self.assertRaisesRegex(CommandError, "media_hash_mismatch"):
            call_command("cleanup_legacy_media", **self._execute_options())

        self.assertEqual(self._media_files(), files_before)
        self.assertEqual(self._database_paths(), self.database_paths)

    def test_legacy_cleanup_changed_old_file_deletes_nothing(self):
        changed_path = self.legacy_paths[1]
        self._write_media(changed_path, make_image_bytes("orange"))
        files_before = self._media_files()

        with self.assertRaisesRegex(CommandError, "media_hash_mismatch"):
            call_command("cleanup_legacy_media", **self._execute_options())

        self.assertEqual(self._media_files(), files_before)
        self.assertEqual(self._database_paths(), self.database_paths)

    def test_legacy_cleanup_database_closure_mismatch_deletes_nothing(self):
        extra_path = "derivatives/{}/extra.png".format(self.image.asset_uuid)
        self._write_media(extra_path, make_image_bytes("black"))
        Thumbnail.objects.create(
            original=self.image,
            image=extra_path,
            size="extra",
            width=32,
            height=32,
        )
        files_before = self._media_files()
        paths_before = self._database_paths()

        with self.assertRaisesRegex(CommandError, "manifest_plan_mismatch"):
            call_command("cleanup_legacy_media", **self._execute_options())

        self.assertEqual(self._media_files(), files_before)
        self.assertEqual(self._database_paths(), paths_before)

    def test_legacy_cleanup_rejects_old_path_escape_without_deleting(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        outside_path = Path(outside.name, "outside.png")
        outside_bytes = self.canonical_files[self.database_paths["original"]]
        outside_path.write_bytes(outside_bytes)
        Path(self.temporary_media.name, "escape").symlink_to(
            outside.name, target_is_directory=True
        )
        events = self._manifest_events()
        escaped = "escape/outside.png"
        for event in events:
            event["old_original"] = escaped
            event["files"][0]["old_path"] = escaped
        Path(self.manifest_path).write_text(
            "".join(json.dumps(event) + "\n" for event in events)
        )
        files_before = self._media_files()

        with self.assertRaisesRegex(CommandError, "media_path_escape"):
            call_command("cleanup_legacy_media", **self._execute_options())

        self.assertEqual(self._media_files(), files_before)
        self.assertEqual(outside_path.read_bytes(), outside_bytes)

    def test_legacy_cleanup_rejects_incomplete_manifest(self):
        incomplete_path = os.path.join(
            self.temporary_data.name, "migrations", "incomplete.jsonl"
        )
        first_event = self._manifest_events()[0]
        Path(incomplete_path).write_text(json.dumps(first_event) + "\n")
        options = self._execute_options()
        options["manifest"] = incomplete_path
        files_before = self._media_files()

        with self.assertRaisesRegex(CommandError, "manifest_not_committed"):
            call_command("cleanup_legacy_media", **options)

        self.assertEqual(self._media_files(), files_before)

    def test_legacy_cleanup_rejects_nonexecuted_commit_events(self):
        events = self._manifest_events()
        for event in events:
            event["execute"] = False
        Path(self.manifest_path).write_text(
            "".join(json.dumps(event) + "\n" for event in events)
        )
        files_before = self._media_files()

        with self.assertRaisesRegex(CommandError, "manifest_not_committed"):
            call_command("cleanup_legacy_media", **self._execute_options())

        self.assertEqual(self._media_files(), files_before)

    def test_legacy_cleanup_rejects_manifest_outside_data_root(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        outside_manifest = Path(outside.name, "cleanup.jsonl")
        outside_manifest.write_bytes(Path(self.manifest_path).read_bytes())
        options = self._execute_options()
        options["manifest"] = str(outside_manifest)
        files_before = self._media_files()

        with self.assertRaisesRegex(CommandError, "manifest_path_escape"):
            call_command("cleanup_legacy_media", **options)

        self.assertEqual(self._media_files(), files_before)


class OrphanMediaCleanupCommandTest(
    TemporaryCleanupRootsMixin, TransactionTestCase
):
    def setUp(self):
        super(OrphanMediaCleanupCommandTest, self).setUp()
        self.old_timestamp = time.time() - timedelta(hours=25).total_seconds()
        self.orphan_uuid = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        self.mixed_uuid = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
        referenced_uuid = uuid.UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
        path_referenced_uuid = uuid.UUID(
            "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
        )

        self.old_staging = self._write_media(".staging/old.part", b"old")
        self.fresh_staging = self._write_media(
            ".staging/fresh.part", b"fresh"
        )
        self.orphan_original = self._write_media(
            "originals/{}/original.png".format(self.orphan_uuid),
            make_image_bytes("red"),
        )
        self.orphan_derivative = self._write_media(
            "derivatives/{}/thumbnail.png".format(self.orphan_uuid),
            make_image_bytes("green"),
        )
        self.mixed_original = self._write_media(
            "originals/{}/original.png".format(self.mixed_uuid),
            make_image_bytes("blue"),
        )
        self.mixed_derivative = self._write_media(
            "derivatives/{}/thumbnail.png".format(self.mixed_uuid),
            make_image_bytes("yellow"),
        )
        referenced_path = "originals/{}/original.png".format(referenced_uuid)
        self._write_media(referenced_path, make_image_bytes("purple"))
        self.referenced_image = Image.objects.create(
            image=referenced_path,
            asset_uuid=referenced_uuid,
            original_filename="referenced.png",
            width=32,
            height=32,
        )
        path_referenced = "derivatives/{}/thumbnail.png".format(
            path_referenced_uuid
        )
        self._write_media(path_referenced, make_image_bytes("orange"))
        Thumbnail.objects.create(
            original=self.referenced_image,
            image=path_referenced,
            size="thumbnail",
            width=32,
            height=32,
        )
        self.legacy_path = self._write_media(
            "image/original/by-md5/a/b/hash/original.png",
            make_image_bytes("white"),
        )

        old_paths = (
            self.old_staging,
            self.orphan_original,
            self.orphan_derivative,
            self.mixed_original,
            Path(self.temporary_media.name, referenced_path),
            Path(self.temporary_media.name, path_referenced),
            self.legacy_path,
        )
        for path in old_paths:
            self._set_old(path)
        for relative_directory in (
            "originals/{}".format(self.orphan_uuid),
            "derivatives/{}".format(self.orphan_uuid),
            "originals/{}".format(self.mixed_uuid),
            "originals/{}".format(referenced_uuid),
            "derivatives/{}".format(path_referenced_uuid),
        ):
            self._set_old(Path(self.temporary_media.name, relative_directory))

    def _set_old(self, path):
        os.utime(path, (self.old_timestamp, self.old_timestamp))

    def test_orphan_cleanup_default_dry_run_reports_only_old_unreferenced(self):
        files_before = self._media_files()
        stdout = StringIO()

        call_command("cleanup_orphan_media", stdout=stdout)

        self.assertEqual(self._media_files(), files_before)
        output = stdout.getvalue()
        self.assertIn(".staging/old.part", output)
        self.assertIn(str(self.orphan_uuid), output)
        self.assertNotIn(".staging/fresh.part", output)
        self.assertNotIn(str(self.mixed_uuid), output)
        self.assertNotIn(str(self.referenced_image.asset_uuid), output)
        self.assertNotIn("image/original/by-md5", output)

    def test_orphan_cleanup_execute_removes_only_old_unreferenced_closure(self):
        files_before = self._media_files()
        expected = dict(files_before)
        del expected[".staging/old.part"]
        del expected[
            "originals/{}/original.png".format(self.orphan_uuid)
        ]
        del expected[
            "derivatives/{}/thumbnail.png".format(self.orphan_uuid)
        ]

        call_command("cleanup_orphan_media", execute=True, stdout=StringIO())

        self.assertEqual(self._media_files(), expected)
        self.assertFalse(self.old_staging.exists())
        self.assertFalse(self.orphan_original.parent.exists())
        self.assertFalse(self.orphan_derivative.parent.exists())
        self.assertTrue(self.fresh_staging.exists())
        self.assertTrue(self.mixed_original.exists())
        self.assertTrue(self.mixed_derivative.exists())
        self.assertTrue(self.legacy_path.exists())

    def test_orphan_cleanup_rejects_age_below_twenty_four_hours(self):
        files_before = self._media_files()

        with self.assertRaisesRegex(CommandError, "orphan_age_too_short"):
            call_command(
                "cleanup_orphan_media",
                execute=True,
                older_than_hours=23,
            )

        self.assertEqual(self._media_files(), files_before)

    def test_orphan_cleanup_preserves_file_exactly_at_cutoff(self):
        now = time.time()
        boundary = self._write_media(".staging/boundary.part", b"boundary")
        cutoff = now - timedelta(hours=24).total_seconds()
        os.utime(boundary, (cutoff, cutoff))

        with mock.patch(
            "django_images.services.media_cleanup.time.time",
            return_value=now,
        ):
            call_command(
                "cleanup_orphan_media", execute=True, stdout=StringIO()
            )

        self.assertEqual(boundary.read_bytes(), b"boundary")

    def test_orphan_cleanup_does_not_remove_parent_of_young_empty_directory(
        self,
    ):
        parent = Path(self.temporary_media.name, ".staging", "old-parent")
        young_directory = parent / "young-empty"
        young_directory.mkdir(parents=True)
        old_file = parent / "old.part"
        old_file.write_bytes(b"old nested")
        self._set_old(old_file)
        self._set_old(parent)

        call_command("cleanup_orphan_media", execute=True, stdout=StringIO())

        self.assertFalse(old_file.exists())
        self.assertTrue(parent.is_dir())
        self.assertTrue(young_directory.is_dir())

    def test_orphan_cleanup_rejects_symlink_before_any_deletion(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        outside_path = Path(outside.name, "outside.part")
        outside_path.write_bytes(b"outside")
        link = Path(self.temporary_media.name, ".staging", "linked.part")
        link.symlink_to(outside_path)
        os.utime(
            link,
            (self.old_timestamp, self.old_timestamp),
            follow_symlinks=False,
        )
        files_before = self._media_files()

        with self.assertRaisesRegex(CommandError, "media_path_escape"):
            call_command("cleanup_orphan_media", execute=True)

        self.assertEqual(self._media_files(), files_before)
        self.assertTrue(link.is_symlink())
        self.assertEqual(outside_path.read_bytes(), b"outside")
        self.assertTrue(self.old_staging.exists())
        self.assertTrue(self.orphan_original.exists())

    def test_orphan_cleanup_rejects_symlinked_allowed_root(self):
        staging = Path(self.temporary_media.name, ".staging")
        staging.rename(Path(self.temporary_media.name, ".staging-retained"))
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        outside_path = Path(outside.name, "outside.part")
        outside_path.write_bytes(b"outside")
        staging.symlink_to(outside.name, target_is_directory=True)
        files_before = self._media_files()

        with self.assertRaisesRegex(CommandError, "media_path_escape"):
            call_command("cleanup_orphan_media", execute=True)

        self.assertEqual(self._media_files(), files_before)
        self.assertEqual(outside_path.read_bytes(), b"outside")
        self.assertTrue(self.orphan_original.exists())

    def test_orphan_cleanup_rejects_escaped_database_reference(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        outside_path = Path(outside.name, "outside.png")
        outside_path.write_bytes(make_image_bytes("black"))
        Path(self.temporary_media.name, "escape").symlink_to(
            outside.name, target_is_directory=True
        )
        Image.objects.filter(pk=self.referenced_image.pk).update(
            image="escape/outside.png"
        )
        files_before = self._media_files()

        with self.assertRaisesRegex(CommandError, "media_path_escape"):
            call_command("cleanup_orphan_media", execute=True)

        self.assertEqual(self._media_files(), files_before)
        self.assertEqual(
            outside_path.read_bytes(), make_image_bytes("black")
        )
        self.assertTrue(self.old_staging.exists())
        self.assertTrue(self.orphan_original.exists())
