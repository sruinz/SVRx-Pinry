from contextlib import contextmanager
from datetime import timedelta
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from unittest import mock
import uuid

from django.core.management import CommandError, call_command
from django.db import connection, OperationalError
from django.test import TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from PIL import Image as PILImage

from django_images.models import Image, Thumbnail
from django_images.file_ops import (
    MediaPathError,
    media_lifecycle_lock,
    open_media_root,
)
from django_images.services import media_cleanup


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
            and ".pinry-locks" not in path.relative_to(root).parts
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

    def test_legacy_cleanup_parent_swap_cannot_unlink_outside_root(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        original_parent = Path(self.temporary_media.name, "legacy")
        moved_parent = Path(outside.name, "moved-legacy")
        database_before = tuple(
            Image.objects.values_list("id", "image", "asset_uuid")
        ), tuple(Thumbnail.objects.values_list("id", "image"))
        real_unlink = media_cleanup._unlink_file
        swapped = {"done": False}

        def restore_parent():
            if original_parent.is_symlink():
                original_parent.unlink()
            if moved_parent.exists():
                moved_parent.rename(original_parent)

        self.addCleanup(restore_parent)

        def swap_parent_then_unlink(*args):
            if not swapped["done"]:
                original_parent.rename(moved_parent)
                original_parent.symlink_to(
                    moved_parent, target_is_directory=True
                )
                swapped["done"] = True
            return real_unlink(*args)

        with mock.patch(
            "django_images.services.media_cleanup._unlink_file",
            side_effect=swap_parent_then_unlink,
        ):
            with self.assertRaisesRegex(CommandError, "unsafe_media_file"):
                call_command("cleanup_legacy_media", **self._execute_options())

        self.assertTrue(original_parent.is_symlink())
        self.assertEqual(
            (
                tuple(Image.objects.values_list("id", "image", "asset_uuid")),
                tuple(Thumbnail.objects.values_list("id", "image")),
            ),
            database_before,
        )
        for old_path, content in self.legacy_files.items():
            outside_path = moved_parent / Path(old_path).relative_to("legacy")
            self.assertEqual(outside_path.read_bytes(), content)
        for canonical_path, content in self.canonical_files.items():
            self.assertEqual(
                Path(self.temporary_media.name, canonical_path).read_bytes(),
                content,
            )
        self.assertEqual(
            {
                path: content
                for path, content in self._media_files().items()
                if path in self.canonical_files
            },
            self.canonical_files,
        )

    def test_legacy_cleanup_root_swap_does_not_follow_replacement(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        media_root = Path(self.temporary_media.name)
        moved_root = Path(outside.name, "original-root")
        replacement_root = Path(outside.name, "replacement-root")
        replacement_root.mkdir()
        for old_path in self.legacy_paths:
            replacement = replacement_root / old_path
            replacement.parent.mkdir(parents=True, exist_ok=True)
            os.link(media_root / old_path, replacement)
        database_before = tuple(
            Image.objects.values_list("id", "image", "asset_uuid")
        ), tuple(Thumbnail.objects.values_list("id", "image"))
        real_unlink = media_cleanup._unlink_file
        swapped = {"done": False}

        def restore_root():
            if media_root.is_symlink():
                media_root.unlink()
            if moved_root.exists():
                moved_root.rename(media_root)

        self.addCleanup(restore_root)

        def swap_root_then_unlink(*args):
            if not swapped["done"]:
                media_root.rename(moved_root)
                media_root.symlink_to(
                    replacement_root, target_is_directory=True
                )
                swapped["done"] = True
            return real_unlink(*args)

        with mock.patch(
            "django_images.services.media_cleanup._unlink_file",
            side_effect=swap_root_then_unlink,
        ):
            call_command("cleanup_legacy_media", **self._execute_options())

        self.assertTrue(media_root.is_symlink())
        self.assertEqual(
            (
                tuple(Image.objects.values_list("id", "image", "asset_uuid")),
                tuple(Thumbnail.objects.values_list("id", "image")),
            ),
            database_before,
        )
        for old_path, content in self.legacy_files.items():
            self.assertEqual(
                (replacement_root / old_path).read_bytes(), content
            )
            self.assertFalse((moved_root / old_path).exists())
        for canonical_path, content in self.canonical_files.items():
            self.assertEqual(
                (moved_root / canonical_path).read_bytes(), content
            )

    def test_legacy_cleanup_validates_the_opened_root_after_path_swap(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        media_root = Path(self.temporary_media.name)
        moved_root = Path(outside.name, "opened-root")
        replacement_root = Path(outside.name, "replacement-root")
        replacement_root.mkdir()
        for relative_path, content in self.canonical_files.items():
            replacement = replacement_root / relative_path
            replacement.parent.mkdir(parents=True, exist_ok=True)
            replacement.write_bytes(content)
        for relative_path in self.legacy_paths:
            replacement = replacement_root / relative_path
            replacement.parent.mkdir(parents=True, exist_ok=True)
            os.link(media_root / relative_path, replacement)
        for relative_path in self.canonical_files:
            (media_root / relative_path).unlink()

        opened_files_before = self._media_files()
        replacement_files_before = {
            path.relative_to(replacement_root).as_posix(): path.read_bytes()
            for path in replacement_root.rglob("*")
            if path.is_file() and not path.is_symlink()
        }
        database_before = tuple(
            Image.objects.values_list("id", "image", "asset_uuid")
        ), tuple(Thumbnail.objects.values_list("id", "image"))
        real_open_root = media_cleanup._open_media_root

        def restore_root():
            if media_root.is_symlink():
                media_root.unlink()
            if moved_root.exists():
                moved_root.rename(media_root)

        self.addCleanup(restore_root)

        def open_root_then_swap(path):
            descriptor = real_open_root(path)
            media_root.rename(moved_root)
            media_root.symlink_to(replacement_root, target_is_directory=True)
            return descriptor

        with mock.patch(
            "django_images.services.media_cleanup._open_media_root",
            side_effect=open_root_then_swap,
        ):
            with self.assertRaises(CommandError):
                call_command(
                    "cleanup_legacy_media", **self._execute_options()
                )

        self.assertTrue(media_root.is_symlink())
        self.assertEqual(
            {
                path.relative_to(moved_root).as_posix(): path.read_bytes()
                for path in moved_root.rglob("*")
                if path.is_file() and not path.is_symlink()
                and ".pinry-locks" not in path.relative_to(moved_root).parts
            },
            opened_files_before,
        )
        self.assertEqual(
            {
                path.relative_to(replacement_root).as_posix(): path.read_bytes()
                for path in replacement_root.rglob("*")
                if path.is_file() and not path.is_symlink()
            },
            replacement_files_before,
        )
        self.assertEqual(
            (
                tuple(Image.objects.values_list("id", "image", "asset_uuid")),
                tuple(Thumbnail.objects.values_list("id", "image")),
            ),
            database_before,
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

        self.old_staging = self._write_media(
            ".staging/media-migration-10101010-1010-4010-8010-101010101010.part",
            b"old",
        )
        self.fresh_staging = self._write_media(
            ".staging/media-migration-20202020-2020-4020-8020-202020202020.part",
            b"fresh",
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
        self.assertIn("candidate-", output)
        self.assertNotIn("?token=", output)
        self.assertNotIn(self.temporary_media.name, output)
        self.assertNotIn("image/original/by-md5", output)

    def test_orphan_cleanup_execute_removes_only_old_unreferenced_closure(self):
        files_before = self._media_files()
        expected = dict(files_before)
        del expected[self.old_staging.relative_to(self.temporary_media.name).as_posix()]
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

    def test_orphan_cleanup_parent_swap_cannot_unlink_outside_root(self):
        asset_parent = Path(self.temporary_media.name, ".staging", "asset")
        nested = asset_parent / "nested"
        nested.mkdir(parents=True)
        nested_file = nested / "old.part"
        nested_file.write_bytes(b"nested old")
        self._set_old(nested_file)
        self._set_old(nested)
        self._set_old(asset_parent)
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        moved_parent = Path(outside.name, "moved-asset")
        files_before = self._media_files()
        database_before = tuple(
            Image.objects.values_list("id", "image", "asset_uuid")
        ), tuple(Thumbnail.objects.values_list("id", "image"))
        real_unlink = media_cleanup._unlink_file
        swapped = {"done": False}

        def restore_parent():
            if asset_parent.is_symlink():
                asset_parent.unlink()
            if moved_parent.exists():
                moved_parent.rename(asset_parent)

        self.addCleanup(restore_parent)

        def swap_parent_then_unlink(*args):
            if not swapped["done"]:
                asset_parent.rename(moved_parent)
                asset_parent.symlink_to(
                    moved_parent, target_is_directory=True
                )
                swapped["done"] = True
            return real_unlink(*args)

        with mock.patch(
            "django_images.services.media_cleanup._unlink_file",
            side_effect=swap_parent_then_unlink,
        ):
            with self.assertRaisesRegex(CommandError, "unsafe_orphan_entry"):
                call_command(
                    "cleanup_orphan_media", execute=True, stdout=StringIO()
                )

        self.assertTrue(asset_parent.is_dir())
        self.assertFalse(moved_parent.exists())
        self.assertEqual(self._media_files(), files_before)
        self.assertEqual(
            (
                tuple(Image.objects.values_list("id", "image", "asset_uuid")),
                tuple(Thumbnail.objects.values_list("id", "image")),
            ),
            database_before,
        )
        self.assertEqual(self.old_staging.read_bytes(), b"old")
        self.assertTrue(self.orphan_original.exists())
        self.assertTrue(self.orphan_derivative.exists())
        self.assertTrue(self.legacy_path.exists())

    def test_orphan_cleanup_parent_swap_cannot_remove_outside_directory(self):
        for path in Path(self.temporary_media.name).rglob("*"):
            if path.is_file():
                os.utime(path, None)
        asset_parent = Path(self.temporary_media.name, ".staging", "asset")
        nested = asset_parent / "nested"
        empty_directory = nested / "empty"
        empty_directory.mkdir(parents=True)
        self._set_old(empty_directory)
        self._set_old(nested)
        self._set_old(asset_parent)
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        moved_parent = Path(outside.name, "moved-asset")
        files_before = self._media_files()
        database_before = tuple(
            Image.objects.values_list("id", "image", "asset_uuid")
        ), tuple(Thumbnail.objects.values_list("id", "image"))
        real_remove = media_cleanup._remove_empty_directory
        swapped = {"done": False}

        def restore_parent():
            if asset_parent.is_symlink():
                asset_parent.unlink()
            if moved_parent.exists():
                moved_parent.rename(asset_parent)

        self.addCleanup(restore_parent)

        def swap_parent_then_remove(*args):
            if not swapped["done"]:
                asset_parent.rename(moved_parent)
                asset_parent.symlink_to(
                    moved_parent, target_is_directory=True
                )
                swapped["done"] = True
            return real_remove(*args)

        with mock.patch(
            "django_images.services.media_cleanup._remove_empty_directory",
            side_effect=swap_parent_then_remove,
        ):
            with self.assertRaisesRegex(CommandError, "unsafe_orphan_entry"):
                call_command(
                    "cleanup_orphan_media", execute=True, stdout=StringIO()
                )

        self.assertTrue(asset_parent.is_dir())
        self.assertFalse(moved_parent.exists())
        self.assertEqual(self._media_files(), files_before)
        self.assertEqual(
            (
                tuple(Image.objects.values_list("id", "image", "asset_uuid")),
                tuple(Thumbnail.objects.values_list("id", "image")),
            ),
            database_before,
        )

    def test_orphan_cleanup_fails_closed_after_media_root_path_swap(self):
        for path in Path(self.temporary_media.name).rglob("*"):
            os.utime(path, None)
        swapped_uuid = uuid.UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
        opened_old = self._write_media(
            "originals/{}/original.png".format(swapped_uuid),
            make_image_bytes("black"),
        )
        young_uuid = uuid.UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")
        opened_young = self._write_media(
            "originals/{}/original.png".format(young_uuid), b"young"
        )
        self._set_old(opened_old)
        self._set_old(opened_old.parent)

        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        media_root = Path(self.temporary_media.name)
        moved_root = Path(outside.name, "opened-root")
        replacement_root = Path(outside.name, "replacement-root")
        replacement_old = replacement_root / opened_old.relative_to(media_root)
        replacement_old.parent.mkdir(parents=True)
        os.link(opened_old, replacement_old)
        self._set_old(replacement_old.parent)

        opened_files_before = self._media_files()
        replacement_files_before = {
            replacement_old.relative_to(replacement_root).as_posix():
                replacement_old.read_bytes()
        }
        database_before = tuple(
            Image.objects.values_list("id", "image", "asset_uuid")
        ), tuple(Thumbnail.objects.values_list("id", "image"))
        real_open_root = media_cleanup.open_media_root

        def restore_root():
            if media_root.is_symlink():
                media_root.unlink()
            if moved_root.exists():
                moved_root.rename(media_root)

        self.addCleanup(restore_root)

        def open_root_then_swap(path):
            descriptor = real_open_root(path)
            media_root.rename(moved_root)
            media_root.symlink_to(replacement_root, target_is_directory=True)
            return descriptor

        command_error = None
        with mock.patch(
            "django_images.services.media_cleanup.open_media_root",
            side_effect=open_root_then_swap,
        ):
            try:
                call_command(
                    "cleanup_orphan_media", execute=True, stdout=StringIO()
                )
            except CommandError as error:
                command_error = error

        self.assertTrue(media_root.is_symlink())
        self.assertEqual(
            {
                path.relative_to(moved_root).as_posix(): path.read_bytes()
                for path in moved_root.rglob("*")
                if path.is_file() and not path.is_symlink()
                and ".pinry-locks" not in path.relative_to(moved_root).parts
            },
            opened_files_before,
        )
        self.assertEqual(
            {
                path.relative_to(replacement_root).as_posix(): path.read_bytes()
                for path in replacement_root.rglob("*")
                if path.is_file() and not path.is_symlink()
            },
            replacement_files_before,
        )
        self.assertEqual(
            (moved_root / opened_young.relative_to(media_root)).read_bytes(),
            b"young",
        )
        self.assertEqual(
            (
                tuple(Image.objects.values_list("id", "image", "asset_uuid")),
                tuple(Thumbnail.objects.values_list("id", "image")),
            ),
            database_before,
        )
        self.assertEqual(
            str(command_error), "orphan_cleanup_lock_failed"
        )

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
        now_ns = time.time_ns()
        boundary = self._write_media(
            ".staging/media-migration-30303030-3030-4030-8030-303030303030.part",
            b"boundary",
        )
        cutoff_ns = now_ns - int(
            timedelta(hours=24).total_seconds() * 1000000000
        )
        os.utime(boundary, ns=(cutoff_ns, cutoff_ns))

        with mock.patch(
            "django_images.services.media_cleanup.time.time_ns",
            return_value=now_ns,
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

        with self.assertRaisesRegex(CommandError, "unsafe_orphan_entry"):
            call_command("cleanup_orphan_media", execute=True, stdout=StringIO())

        self.assertTrue(old_file.exists())
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


class OrphanMediaCleanupPreparedAssetTests(
    TemporaryCleanupRootsMixin, TransactionTestCase
):
    def _set_old(self, path):
        timestamp = time.time() - timedelta(hours=25).total_seconds()
        os.utime(path, (timestamp, timestamp))

    def test_nested_staging_keeps_every_present_slot_when_one_slot_is_young(self):
        run_uuid = uuid.UUID("11111111-1111-4111-8111-111111111111")
        asset_uuid = uuid.UUID("22222222-2222-4222-8222-222222222222")
        asset_directory = Path(
            self.temporary_media.name,
            ".staging",
            str(run_uuid),
            str(asset_uuid),
        )
        asset_directory.mkdir(parents=True)
        slots = []
        for kind in ("original", "thumbnail", "standard", "square"):
            path = asset_directory / "{}.part".format(kind)
            path.write_bytes(kind.encode("ascii"))
            slots.append(path)
        for path in slots[:-1]:
            self._set_old(path)
        self._set_old(asset_directory)
        self._set_old(asset_directory.parent)

        call_command("cleanup_orphan_media", execute=True, stdout=StringIO())

        self.assertTrue(asset_directory.is_dir())
        self.assertEqual(
            [path.name for path in sorted(asset_directory.iterdir())],
            ["original.part", "square.part", "standard.part", "thumbnail.part"],
        )

    @override_settings(PINRY_ORPHAN_MIN_AGE_SECONDS=26 * 60 * 60)
    def test_command_default_uses_configured_minimum_age(self):
        staging = self._write_media(
            ".staging/media-migration-33333333-3333-4333-8333-333333333333.part",
            b"old",
        )
        timestamp = time.time() - timedelta(hours=25).total_seconds()
        os.utime(staging, (timestamp, timestamp))

        call_command("cleanup_orphan_media", execute=True, stdout=StringIO())

        self.assertTrue(staging.exists())

    @override_settings(PINRY_ORPHAN_MIN_AGE_SECONDS=48 * 60 * 60)
    def test_direct_age_override_cannot_undercut_configured_minimum(self):
        staging = self._write_media(
            ".staging/media-migration-99999999-9999-4999-8999-999999999999.part",
            b"old",
        )
        timestamp = time.time() - timedelta(hours=25).total_seconds()
        os.utime(staging, (timestamp, timestamp))

        with self.assertRaisesRegex(CommandError, "orphan_age_too_short"):
            media_cleanup.OrphanMediaCleaner().run(
                execute=True,
                older_than=timedelta(hours=24),
            )

        self.assertTrue(staging.exists())

    def test_invalid_configured_orphan_ages_fail_closed(self):
        invalid_values = (
            True,
            False,
            None,
            "86400",
            0,
            -1,
            float("nan"),
            float("inf"),
            float("-inf"),
            10 ** 10000,
        )
        for index, value in enumerate(invalid_values):
            with self.subTest(index=index):
                with override_settings(
                    PINRY_ORPHAN_MIN_AGE_SECONDS=value
                ):
                    with self.assertRaisesRegex(
                        CommandError, "invalid_orphan_age_setting"
                    ):
                        media_cleanup.OrphanMediaCleaner().run(execute=True)

    def test_execute_creates_a_private_lifecycle_lock_but_dry_run_does_not(self):
        staging = self._write_media(
            ".staging/media-migration-44444444-4444-4444-8444-444444444444.part",
            b"old",
        )
        self._set_old(staging)
        lock_path = Path(
            self.temporary_media.name,
            ".pinry-locks",
            "media-lifecycle.lock",
        )

        call_command("cleanup_orphan_media", stdout=StringIO())
        self.assertFalse(lock_path.exists())

        call_command("cleanup_orphan_media", execute=True, stdout=StringIO())
        self.assertTrue(lock_path.is_file())
        self.assertEqual(lock_path.stat().st_mode & 0o077, 0)

    def test_final_uuid_database_queries_are_bounded_to_four_hundred(self):
        timestamp = time.time() - timedelta(hours=25).total_seconds()
        for index in range(1001):
            asset_uuid = uuid.UUID(int=index + 1)
            directory = Path(
                self.temporary_media.name,
                "originals",
                str(asset_uuid),
            )
            directory.mkdir(parents=True)
            os.utime(directory, (timestamp, timestamp))
        parameter_counts = []

        def capture_parameters(execute, sql, params, many, context):
            if params is not None:
                parameter_counts.append(len(params))
            return execute(sql, params, many, context)

        with connection.execute_wrapper(capture_parameters):
            with CaptureQueriesContext(connection) as queries:
                call_command("cleanup_orphan_media", stdout=StringIO())

        self.assertLessEqual(len(queries), 20)
        self.assertTrue(parameter_counts)
        self.assertLessEqual(max(parameter_counts), 400)

    def test_orphan_scan_does_not_call_recursive_tree_scanner(self):
        staging = self._write_media(
            ".staging/media-migration-aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa.part",
            b"old",
        )
        self._set_old(staging)

        with mock.patch(
            "django_images.services.media_cleanup._scan_tree",
            side_effect=AssertionError("recursive scan used"),
            create=True,
        ):
            call_command("cleanup_orphan_media", stdout=StringIO())

    def test_command_hides_raw_filesystem_error_details(self):
        with mock.patch(
            "django_images.services.media_cleanup.open_media_root",
            side_effect=CommandError("unsafe_media_root: /tmp/token=secret"),
        ):
            with self.assertRaises(CommandError) as raised:
                call_command("cleanup_orphan_media", stdout=StringIO())

        self.assertEqual(str(raised.exception), "unsafe_media_root")

    def test_exclusive_lifecycle_lock_times_out_in_a_second_thread(self):
        root = open_media_root(self.temporary_media.name)
        result = []

        def contend():
            contender = open_media_root(self.temporary_media.name)
            try:
                with media_lifecycle_lock(
                    contender,
                    exclusive=True,
                    deadline=time.monotonic() + 0.03,
                ):
                    result.append("acquired")
            except MediaPathError as error:
                result.append(str(error))
            finally:
                contender.close()

        try:
            with media_lifecycle_lock(root, exclusive=True):
                worker = threading.Thread(target=contend)
                worker.start()
                worker.join()
        finally:
            root.close()

        self.assertEqual(result, ["media_lifecycle_busy"])

    def test_staging_path_referenced_by_image_is_never_deleted(self):
        asset_uuid = uuid.UUID("55555555-5555-4555-8555-555555555555")
        relative_path = (
            ".staging/media-migration-66666666-6666-4666-8666-666666666666.part"
        )
        staging = self._write_media(relative_path, b"referenced")
        self._set_old(staging)
        Image.objects.create(
            image=relative_path,
            asset_uuid=asset_uuid,
            original_filename="referenced.png",
            width=1,
            height=1,
        )

        media_cleanup.OrphanMediaCleaner().run(execute=True)

        self.assertTrue(staging.exists())

    def test_one_slot_staging_closure_is_deleted_once_with_its_directories(self):
        run_uuid = uuid.UUID("77777777-7777-4777-8777-777777777777")
        asset_uuid = uuid.UUID("88888888-8888-4888-8888-888888888888")
        relative_path = ".staging/{}/{}/original.part".format(
            run_uuid, asset_uuid
        )
        staging = self._write_media(relative_path, b"one-slot")
        self._set_old(staging)
        self._set_old(staging.parent)
        self._set_old(staging.parent.parent)

        summary = media_cleanup.OrphanMediaCleaner().run(execute=True)

        self.assertEqual(summary.deleted, 3)
        self.assertEqual(len(summary.candidate_paths), 3)
        self.assertFalse(staging.parent.parent.exists())


class OrphanMediaCleanupClosureContractTests(
    TemporaryCleanupRootsMixin, TransactionTestCase
):
    def _set_old(self, path):
        timestamp = time.time() - timedelta(hours=25).total_seconds()
        os.utime(path, (timestamp, timestamp))

    def _write_nested_subset(self, run_uuid, asset_uuid, count):
        asset_directory = Path(
            self.temporary_media.name,
            ".staging",
            str(run_uuid),
            str(asset_uuid),
        )
        asset_directory.mkdir(parents=True)
        paths = []
        for kind in ("original", "thumbnail", "standard", "square")[:count]:
            path = asset_directory / "{}.part".format(kind)
            path.write_bytes(kind.encode("ascii"))
            self._set_old(path)
            paths.append(path)
        self._set_old(asset_directory)
        self._set_old(asset_directory.parent)
        return asset_directory, paths

    def _write_final_subset(self, asset_uuid, count):
        original_directory = Path(
            self.temporary_media.name, "originals", str(asset_uuid)
        )
        original_directory.mkdir(parents=True)
        paths = []
        if count:
            original = original_directory / "original.png"
            original.write_bytes(b"original")
            self._set_old(original)
            paths.append(original)
        derivative_directory = None
        for kind in ("thumbnail", "standard", "square")[:max(0, count - 1)]:
            if derivative_directory is None:
                derivative_directory = Path(
                    self.temporary_media.name,
                    "derivatives",
                    str(asset_uuid),
                )
                derivative_directory.mkdir(parents=True)
            derivative = derivative_directory / "{}.png".format(kind)
            derivative.write_bytes(kind.encode("ascii"))
            self._set_old(derivative)
            paths.append(derivative)
        self._set_old(original_directory)
        if derivative_directory is not None:
            self._set_old(derivative_directory)
        return original_directory, derivative_directory, paths

    def _run_with_lock_mutation(self, mutate):
        original_lock = media_cleanup.media_lifecycle_lock
        mutated = {"done": False}

        @contextmanager
        def mutating_lock(*args, **kwargs):
            with original_lock(*args, **kwargs):
                if not mutated["done"]:
                    mutated["done"] = True
                    mutate()
                yield

        with mock.patch(
            "django_images.services.media_cleanup.media_lifecycle_lock",
            side_effect=mutating_lock,
        ):
            return media_cleanup.OrphanMediaCleaner().run(execute=True)

    def test_nested_staging_zero_through_four_present_slots_converge(self):
        assets = []
        for count in range(5):
            run_uuid = uuid.UUID(int=100 + count)
            asset_uuid = uuid.UUID(int=200 + count)
            assets.append(self._write_nested_subset(
                run_uuid, asset_uuid, count
            )[0])

        summary = media_cleanup.OrphanMediaCleaner().run(execute=True)

        self.assertEqual(summary.deleted, 20)
        self.assertEqual(len(summary.candidate_paths), 20)
        for asset_directory in assets:
            self.assertFalse(asset_directory.parent.exists())

    def test_final_zero_through_four_present_slots_converge(self):
        directories = []
        for count in range(5):
            asset_uuid = uuid.UUID(int=300 + count)
            original, derivatives, _paths = self._write_final_subset(
                asset_uuid, count
            )
            directories.append((original, derivatives))

        summary = media_cleanup.OrphanMediaCleaner().run(execute=True)

        self.assertEqual(summary.deleted, 18)
        for original, derivatives in directories:
            self.assertFalse(original.exists())
            if derivatives is not None:
                self.assertFalse(derivatives.exists())

    def test_old_empty_run_left_before_asset_creation_converges(self):
        run_directory = Path(
            self.temporary_media.name,
            ".staging",
            "11111111-1111-4111-8111-111111111111",
        )
        run_directory.mkdir(parents=True)
        self._set_old(run_directory)

        summary = media_cleanup.OrphanMediaCleaner().run(execute=True)

        self.assertEqual(summary.deleted, 1)
        self.assertFalse(run_directory.exists())

    def test_final_unknown_deep_and_duplicate_slots_abort_before_deletion(self):
        cases = ("unknown", "deep", "duplicate")
        for index, case in enumerate(cases):
            with self.subTest(case=case):
                temporary_media = tempfile.TemporaryDirectory()
                self.addCleanup(temporary_media.cleanup)
                root = Path(temporary_media.name)
                staging = root / ".staging" / (
                    "media-migration-00000000-0000-4000-8000-{:012d}.part".format(
                        index + 1
                    )
                )
                staging.parent.mkdir(parents=True)
                staging.write_bytes(b"retained")
                self._set_old(staging)
                asset_uuid = uuid.UUID(int=400 + index)
                directory = root / "originals" / str(asset_uuid)
                directory.mkdir(parents=True)
                if case == "unknown":
                    invalid_paths = [directory / "token=secret.bin"]
                elif case == "deep":
                    invalid_paths = [directory / "nested" / "original.png"]
                else:
                    invalid_paths = [
                        directory / "original.png",
                        directory / "original.jpg",
                    ]
                for invalid in invalid_paths:
                    invalid.parent.mkdir(parents=True, exist_ok=True)
                    invalid.write_bytes(b"invalid")
                    self._set_old(invalid)
                for parent in sorted(
                    {path.parent for path in invalid_paths},
                    key=lambda value: len(value.parts),
                    reverse=True,
                ):
                    self._set_old(parent)
                self._set_old(directory)

                with override_settings(MEDIA_ROOT=temporary_media.name):
                    with self.assertRaisesRegex(
                        CommandError, "unsafe_orphan_entry"
                    ):
                        media_cleanup.OrphanMediaCleaner().run(execute=True)

                self.assertTrue(staging.exists())
                for invalid in invalid_paths:
                    self.assertTrue(invalid.exists())

    def test_missing_staging_slot_database_reference_protects_present_subset(self):
        run_uuid = uuid.UUID("22222222-2222-4222-8222-222222222222")
        asset_uuid = uuid.UUID("33333333-3333-4333-8333-333333333333")
        asset_directory, paths = self._write_nested_subset(
            run_uuid, asset_uuid, 1
        )
        Image.objects.create(
            image=".staging/{}/{}/square.part".format(run_uuid, asset_uuid),
            asset_uuid=asset_uuid,
            original_filename="referenced.png",
            width=1,
            height=1,
        )

        summary = media_cleanup.OrphanMediaCleaner().run(execute=True)

        self.assertEqual(summary.deleted, 0)
        self.assertTrue(asset_directory.exists())
        self.assertTrue(paths[0].exists())

    def test_path_reference_in_missing_final_root_protects_other_root(self):
        referenced_image = Image.objects.create(
            image="originals/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/missing.png",
            asset_uuid=uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
            original_filename="owner.png",
            width=1,
            height=1,
        )
        asset_uuid = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
        original_directory, _derivatives, paths = self._write_final_subset(
            asset_uuid, 1
        )
        Thumbnail.objects.create(
            original=referenced_image,
            image="derivatives/{}/thumbnail.png".format(asset_uuid),
            size="thumbnail",
            width=1,
            height=1,
        )

        summary = media_cleanup.OrphanMediaCleaner().run(execute=True)

        self.assertEqual(summary.deleted, 0)
        self.assertTrue(original_directory.exists())
        self.assertTrue(paths[0].exists())

    def test_execute_time_new_name_preserves_whole_staging_closure(self):
        run_uuid = uuid.UUID("44444444-4444-4444-8444-444444444444")
        asset_uuid = uuid.UUID("55555555-5555-4555-8555-555555555555")
        asset_directory, paths = self._write_nested_subset(
            run_uuid, asset_uuid, 1
        )

        def add_name():
            (asset_directory / "thumbnail.part").write_bytes(b"new")

        summary = self._run_with_lock_mutation(add_name)

        self.assertEqual(summary.deleted, 0)
        self.assertTrue(paths[0].exists())
        self.assertTrue((asset_directory / "thumbnail.part").exists())

    def test_execute_time_missing_name_preserves_remaining_staging_sibling(self):
        run_uuid = uuid.UUID("66666666-6666-4666-8666-666666666666")
        asset_uuid = uuid.UUID("77777777-7777-4777-8777-777777777777")
        asset_directory, paths = self._write_nested_subset(
            run_uuid, asset_uuid, 2
        )

        summary = self._run_with_lock_mutation(paths[0].unlink)

        self.assertEqual(summary.deleted, 0)
        self.assertTrue(asset_directory.exists())
        self.assertTrue(paths[1].exists())

    def test_execute_time_same_inode_write_preserves_staging_closure(self):
        run_uuid = uuid.UUID("88888888-8888-4888-8888-888888888888")
        asset_uuid = uuid.UUID("99999999-9999-4999-8999-999999999999")
        asset_directory, paths = self._write_nested_subset(
            run_uuid, asset_uuid, 1
        )

        def rewrite():
            with paths[0].open("ab") as stream:
                stream.write(b"changed")

        summary = self._run_with_lock_mutation(rewrite)

        self.assertEqual(summary.deleted, 0)
        self.assertTrue(asset_directory.exists())
        self.assertEqual(paths[0].read_bytes(), b"originalchanged")

    def test_execute_time_database_reference_preserves_final_closure(self):
        asset_uuid = uuid.UUID("12121212-1212-4212-8212-121212121212")
        original_directory, _derivatives, paths = self._write_final_subset(
            asset_uuid, 1
        )

        def add_reference():
            Image.objects.create(
                image="originals/{}/original.png".format(asset_uuid),
                asset_uuid=asset_uuid,
                original_filename="claimed.png",
                width=1,
                height=1,
            )

        summary = self._run_with_lock_mutation(add_reference)

        self.assertEqual(summary.deleted, 0)
        self.assertTrue(original_directory.exists())
        self.assertTrue(paths[0].exists())

    def test_database_error_is_safe_and_deletes_nothing(self):
        asset_uuid = uuid.UUID("13131313-1313-4313-8313-131313131313")
        _original, _derivatives, paths = self._write_final_subset(
            asset_uuid, 1
        )

        with mock.patch.object(
            Image.objects,
            "filter",
            side_effect=OperationalError(
                "database /tmp/private/token=secret is locked"
            ),
        ):
            with self.assertRaises(CommandError) as raised:
                call_command("cleanup_orphan_media", execute=True)

        self.assertEqual(
            str(raised.exception), "orphan_database_unavailable"
        )
        self.assertNotIn("secret", str(raised.exception))
        self.assertTrue(paths[0].exists())

    def test_execute_uses_one_exclusive_lock_per_flat_closure(self):
        for index in (1, 2):
            staging = self._write_media(
                ".staging/media-migration-14141414-1414-4414-8414-{:012d}.part".format(
                    index
                ),
                b"old",
            )
            self._set_old(staging)
        original_lock = media_cleanup.media_lifecycle_lock
        events = []

        @contextmanager
        def tracked_lock(*args, **kwargs):
            events.append("enter")
            with original_lock(*args, **kwargs):
                yield
            events.append("exit")

        with mock.patch(
            "django_images.services.media_cleanup.media_lifecycle_lock",
            side_effect=tracked_lock,
        ):
            media_cleanup.OrphanMediaCleaner().run(execute=True)

        self.assertEqual(events, ["enter", "exit", "enter", "exit"])

    def test_execute_revalidates_only_the_selected_staging_closure(self):
        staging = self._write_media(
            ".staging/media-migration-19191919-1919-4919-8919-191919191919.part",
            b"old",
        )
        self._set_old(staging)
        original_lock = media_cleanup.media_lifecycle_lock
        original_scan = media_cleanup._scan_staging_closures
        in_lock = {"value": False}

        @contextmanager
        def tracked_lock(*args, **kwargs):
            with original_lock(*args, **kwargs):
                in_lock["value"] = True
                try:
                    yield
                finally:
                    in_lock["value"] = False

        def guarded_scan(*args, **kwargs):
            if in_lock["value"]:
                raise AssertionError("global staging rescan under lock")
            return original_scan(*args, **kwargs)

        with mock.patch(
            "django_images.services.media_cleanup.media_lifecycle_lock",
            side_effect=tracked_lock,
        ), mock.patch(
            "django_images.services.media_cleanup._scan_staging_closures",
            side_effect=guarded_scan,
        ):
            summary = media_cleanup.OrphanMediaCleaner().run(execute=True)

        self.assertEqual(summary.deleted, 1)
        self.assertFalse(staging.exists())

    def test_nanosecond_cutoff_deletes_only_strictly_older_file(self):
        now_ns = 1700000000123456789
        cutoff_ns = now_ns - (24 * 60 * 60 * 1000000000)
        older = self._write_media(
            ".staging/media-migration-15151515-1515-4515-8515-151515151515.part",
            b"older",
        )
        boundary = self._write_media(
            ".staging/media-migration-16161616-1616-4616-8616-161616161616.part",
            b"boundary",
        )
        os.utime(older, ns=(cutoff_ns - 1, cutoff_ns - 1))
        os.utime(boundary, ns=(cutoff_ns, cutoff_ns))

        with mock.patch(
            "django_images.services.media_cleanup.time.time_ns",
            return_value=now_ns,
        ), mock.patch(
            "django_images.services.media_cleanup.time.time",
            return_value=now_ns / 1000000000.0,
        ):
            media_cleanup.OrphanMediaCleaner().run(execute=True)

        self.assertFalse(older.exists())
        self.assertTrue(boundary.exists())

    def test_owned_file_unlink_fsyncs_its_parent_directory(self):
        relative_path = (
            ".staging/media-migration-17171717-1717-4717-8717-171717171717.part"
        )
        path = self._write_media(relative_path, b"durable")
        candidate = media_cleanup._FileCandidate(
            relative_path, path.stat()
        )
        root = open_media_root(self.temporary_media.name)
        try:
            with mock.patch.object(
                media_cleanup.os,
                "fsync",
                wraps=os.fsync,
            ) as fsync:
                media_cleanup._unlink_file(root.descriptor, candidate)
        finally:
            root.close()

        self.assertEqual(fsync.call_count, 1)
        self.assertFalse(path.exists())

    def test_owned_directory_removal_fsyncs_its_parent_directory(self):
        relative_path = ".staging/18181818-1818-4818-8818-181818181818"
        path = Path(self.temporary_media.name, relative_path)
        path.mkdir(parents=True)
        candidate = media_cleanup._DirectoryCandidate(
            relative_path, path.stat()
        )
        root = open_media_root(self.temporary_media.name)
        try:
            with mock.patch.object(
                media_cleanup.os,
                "fsync",
                wraps=os.fsync,
            ) as fsync:
                media_cleanup._remove_empty_directory(
                    root.descriptor, candidate
                )
        finally:
            root.close()

        self.assertEqual(fsync.call_count, 1)
        self.assertFalse(path.exists())
