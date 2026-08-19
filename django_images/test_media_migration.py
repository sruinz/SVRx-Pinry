from io import BytesIO, StringIO
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
from unittest import mock

from django.core.files.storage import default_storage
from django.core.management import CommandError, call_command
from django.test import TestCase, TransactionTestCase, override_settings
from PIL import Image as PILImage

from django_images.file_ops import (
    MediaPathError,
    migration_lock_path,
    publish_noreplace,
    resolve_media_path,
)
from django_images.models import Image, Thumbnail
from django_images.services.media_migration import ManifestLog, MediaMigrator


def make_image_bytes(color):
    image = BytesIO()
    PILImage.new("RGB", (32, 32), color).save(image, format="PNG")
    return image.getvalue()


class InjectedInterruption(Exception):
    pass


class MediaMigrationCommandTest(TransactionTestCase):
    def setUp(self):
        super(MediaMigrationCommandTest, self).setUp()
        self.temporary_media = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_media.cleanup)
        self.temporary_data = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_data.cleanup)
        self.settings_override = override_settings(
            MEDIA_ROOT=self.temporary_media.name,
            PINRY_DATA_ROOT=self.temporary_data.name,
        )
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.manifest_path = os.path.join(
            self.temporary_data.name, "migrations", "test.jsonl"
        )
        self.image = self._create_legacy_image("first")
        self.old_original = self.image.image.name
        self.old_files = self._all_media_files()

    def _create_legacy_image(self, prefix):
        original = "legacy/{}/original.jpg".format(prefix)
        self._write_media(original, make_image_bytes("red"))
        image = Image.objects.create(
            image=original,
            asset_uuid="12345678-1234-5678-1234-567812345678"
            if prefix == "first"
            else "87654321-4321-8765-4321-876543218765",
            original_filename="{}.jpg".format(prefix),
            width=32,
            height=32,
        )
        colors = {
            "thumbnail": "green",
            "standard": "blue",
            "square": "yellow",
        }
        for size, color in colors.items():
            relative_name = "legacy/{}/{}.jpg".format(prefix, size)
            self._write_media(relative_name, make_image_bytes(color))
            Thumbnail.objects.create(
                original=image,
                image=relative_name,
                size=size,
                width=32,
                height=32,
            )
        return image

    def _write_media(self, relative_name, content):
        path = Path(self.temporary_media.name, relative_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def _all_media_files(self):
        root = Path(self.temporary_media.name)
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }

    def _manifest_events(self):
        with open(self.manifest_path, encoding="utf-8") as manifest:
            return [json.loads(line) for line in manifest]

    def _canonical_paths(self, image=None):
        image = image or self.image
        asset_uuid = str(image.asset_uuid)
        paths = {
            "original": "originals/{}/original.png".format(asset_uuid)
        }
        paths.update(
            {
                thumbnail.size: "derivatives/{}/{}.png".format(
                    asset_uuid, thumbnail.size
                )
                for thumbnail in image.thumbnail_set.all()
            }
        )
        return paths

    def test_dry_run_writes_plan_but_changes_no_database_or_media(self):
        call_command("migrate_media", manifest=self.manifest_path)
        self.image.refresh_from_db()

        self.assertEqual(self.image.image.name, self.old_original)
        self.assertEqual(self._all_media_files(), self.old_files)
        events = self._manifest_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["schema_version"], 1)
        self.assertEqual(events[0]["event"], "planned")
        self.assertEqual(events[0]["image_id"], self.image.id)
        self.assertFalse(events[0]["execute"])
        self.assertEqual(events[0]["old_original"], self.old_original)
        self.assertEqual(
            events[0]["new_original"],
            self._canonical_paths()["original"],
        )
        self.assertEqual(
            events[0]["files"][0]["sha256"],
            hashlib.sha256(self.old_files[self.old_original]).hexdigest(),
        )

    def test_execute_copies_and_verifies_complete_closure_before_db_switch(self):
        call_command(
            "migrate_media", execute=True, manifest=self.manifest_path
        )
        self.image.refresh_from_db()
        canonical = self._canonical_paths()

        self.assertEqual(self.image.image.name, canonical["original"])
        self.assertEqual(
            {
                thumbnail.size: thumbnail.image.name
                for thumbnail in self.image.thumbnail_set.all()
            },
            {key: canonical[key] for key in canonical if key != "original"},
        )
        files = self._all_media_files()
        self.assertEqual(
            files[self.old_original], self.old_files[self.old_original]
        )
        for old_path, content in self.old_files.items():
            name = (
                "original"
                if old_path == self.old_original
                else Path(old_path).stem
            )
            self.assertEqual(files[canonical[name]], content)
            with PILImage.open(BytesIO(files[canonical[name]])) as image:
                image.load()
        self.assertEqual(
            [event["event"] for event in self._manifest_events()],
            ["planned", "copied", "committed"],
        )
        self.assertFalse(any(".part-" in name for name in files))

    def test_destination_conflict_keeps_batch_db_and_all_sources_unchanged(self):
        canonical = self._canonical_paths()
        conflict_path = canonical["square"]
        conflict_bytes = make_image_bytes("purple")
        self._write_media(conflict_path, conflict_bytes)

        with self.assertRaisesRegex(CommandError, "media_path_conflict"):
            call_command(
                "migrate_media", execute=True, manifest=self.manifest_path
            )

        self.image.refresh_from_db()
        self.assertEqual(self.image.image.name, self.old_original)
        self.assertEqual(
            {
                thumbnail.size: thumbnail.image.name
                for thumbnail in self.image.thumbnail_set.all()
            },
            {
                "thumbnail": "legacy/first/thumbnail.jpg",
                "standard": "legacy/first/standard.jpg",
                "square": "legacy/first/square.jpg",
            },
        )
        files = self._all_media_files()
        self.assertEqual(files[conflict_path], conflict_bytes)
        for old_path, content in self.old_files.items():
            self.assertEqual(files[old_path], content)

    def test_second_copy_failure_keeps_entire_batch_database_unchanged(self):
        second = self._create_legacy_image("second")
        second_old_original = second.image.name
        second_canonical = self._canonical_paths(second)
        self._write_media(second_canonical["square"], b"conflict")

        with self.assertRaisesRegex(CommandError, "media_path_conflict"):
            call_command(
                "migrate_media", execute=True, manifest=self.manifest_path
            )

        self.image.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(self.image.image.name, self.old_original)
        self.assertEqual(second.image.name, second_old_original)
        for old_path, content in self.old_files.items():
            self.assertEqual(self._all_media_files()[old_path], content)

    def test_interrupted_after_commit_recovers_from_same_manifest(self):
        manifest = ManifestLog(self.manifest_path)

        def interrupt(point):
            if point == "after_database_commit":
                raise InjectedInterruption()

        with self.assertRaises(InjectedInterruption):
            MediaMigrator(
                default_storage,
                manifest,
                fault_injector=interrupt,
            ).run(execute=True)

        self.image.refresh_from_db()
        self.assertEqual(
            self.image.image.name, self._canonical_paths()["original"]
        )
        self.assertEqual(self._manifest_events()[-1]["event"], "copied")

        call_command(
            "migrate_media", execute=True, manifest=self.manifest_path
        )

        events = self._manifest_events()
        self.assertEqual(events[-1]["event"], "recovered_commit")
        for file_info in events[-1]["files"]:
            content = self._all_media_files()[file_info["new_path"]]
            self.assertEqual(
                hashlib.sha256(content).hexdigest(), file_info["sha256"]
            )

    def test_interrupted_commit_with_mixed_database_paths_aborts(self):
        manifest = ManifestLog(self.manifest_path)

        def interrupt(point):
            if point == "after_database_commit":
                raise InjectedInterruption()

        with self.assertRaises(InjectedInterruption):
            MediaMigrator(
                default_storage,
                manifest,
                fault_injector=interrupt,
            ).run(execute=True)
        square = self.image.thumbnail_set.get(size="square")
        Thumbnail.objects.filter(pk=square.pk).update(
            image="legacy/first/square.jpg"
        )

        with self.assertRaisesRegex(CommandError, "mixed_media_state"):
            call_command(
                "migrate_media", execute=True, manifest=self.manifest_path
            )

        self.assertEqual(self._manifest_events()[-1]["event"], "copied")

    def test_mixed_asset_aborts_before_processing_next_asset(self):
        canonical = self._canonical_paths()
        self._write_media(
            canonical["original"], self.old_files[self.old_original]
        )
        Image.objects.filter(pk=self.image.pk).update(
            image=canonical["original"]
        )
        second = self._create_legacy_image("second")

        with self.assertRaisesRegex(CommandError, "mixed_media_state"):
            call_command(
                "migrate_media", execute=True, manifest=self.manifest_path
            )

        second.refresh_from_db()
        self.assertEqual(second.image.name, "legacy/second/original.jpg")
        self.assertFalse(
            Path(
                self.temporary_media.name,
                self._canonical_paths(second)["original"],
            ).exists()
        )

    def test_committed_manifest_rerun_is_noop_after_full_validation(self):
        call_command(
            "migrate_media", execute=True, manifest=self.manifest_path
        )
        events_before = self._manifest_events()
        files_before = self._all_media_files()

        call_command(
            "migrate_media", execute=True, manifest=self.manifest_path
        )

        self.assertEqual(self._manifest_events(), events_before)
        self.assertEqual(self._all_media_files(), files_before)

    def test_committed_manifest_hash_mismatch_aborts_without_overwrite(self):
        call_command(
            "migrate_media", execute=True, manifest=self.manifest_path
        )
        canonical_original = self._canonical_paths()["original"]
        changed = make_image_bytes("purple")
        self._write_media(canonical_original, changed)

        with self.assertRaisesRegex(CommandError, "media_hash_mismatch"):
            call_command(
                "migrate_media", execute=True, manifest=self.manifest_path
            )

        self.assertEqual(
            self._all_media_files()[canonical_original], changed
        )
        self.assertEqual(
            self._all_media_files()[self.old_original],
            self.old_files[self.old_original],
        )

    def test_image_without_derivatives_migrates_only_original(self):
        self.image.thumbnail_set.all().delete()
        source_files = self._all_media_files()

        call_command(
            "migrate_media", execute=True, manifest=self.manifest_path
        )

        self.image.refresh_from_db()
        self.assertEqual(
            self.image.image.name, self._canonical_paths()["original"]
        )
        files = self._all_media_files()
        self.assertEqual(files[self.old_original], source_files[self.old_original])
        self.assertEqual(len(files), 2)

    def test_fully_canonical_asset_is_recorded_as_already_current(self):
        canonical = self._canonical_paths()
        for old_path, content in list(self.old_files.items()):
            name = (
                "original"
                if old_path == self.old_original
                else Path(old_path).stem
            )
            self._write_media(canonical[name], content)
        Image.objects.filter(pk=self.image.pk).update(
            image=canonical["original"]
        )
        for thumbnail in self.image.thumbnail_set.all():
            Thumbnail.objects.filter(pk=thumbnail.pk).update(
                image=canonical[thumbnail.size]
            )
        files_before = self._all_media_files()

        call_command(
            "migrate_media", execute=True, manifest=self.manifest_path
        )

        self.assertEqual(
            self._manifest_events()[-1]["event"], "already_current"
        )
        self.assertEqual(self._all_media_files(), files_before)

    def test_unsupported_derivative_aborts_before_any_canonical_write(self):
        relative_name = "legacy/first/arbitrary.jpg"
        self._write_media(relative_name, make_image_bytes("black"))
        Thumbnail.objects.create(
            original=self.image,
            image=relative_name,
            size="arbitrary",
            width=32,
            height=32,
        )
        files_before = self._all_media_files()

        with self.assertRaisesRegex(
            CommandError, "unsupported_legacy_derivative_size"
        ):
            call_command(
                "migrate_media", execute=True, manifest=self.manifest_path
            )

        self.assertEqual(self._all_media_files(), files_before)

    def test_invalid_source_format_fails_closed_as_command_error(self):
        Path(self.temporary_media.name, self.old_original).write_bytes(
            b"not-an-image"
        )
        files_before = self._all_media_files()

        with self.assertRaisesRegex(CommandError, "invalid_legacy_media"):
            call_command(
                "migrate_media", execute=True, manifest=self.manifest_path
            )

        self.image.refresh_from_db()
        self.assertEqual(self.image.image.name, self.old_original)
        self.assertEqual(self._all_media_files(), files_before)

    def test_source_escape_is_rejected_before_manifest_or_canonical_write(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        outside_file = Path(outside.name, "outside.png")
        outside_file.write_bytes(make_image_bytes("white"))
        link = Path(self.temporary_media.name, "escape")
        link.symlink_to(outside.name, target_is_directory=True)
        Image.objects.filter(pk=self.image.pk).update(image="escape/outside.png")

        with self.assertRaisesRegex(CommandError, "media_path_escape"):
            call_command("migrate_media", manifest=self.manifest_path)

        self.assertFalse(os.path.exists(self.manifest_path))
        self.assertEqual(outside_file.read_bytes(), make_image_bytes("white"))

    def test_manifest_outside_data_root_is_rejected_without_write(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        manifest_path = os.path.join(outside.name, "manifest.jsonl")

        with self.assertRaisesRegex(CommandError, "manifest_path_escape"):
            call_command("migrate_media", manifest=manifest_path)

        self.assertFalse(os.path.exists(manifest_path))

    def test_manifest_symlink_escape_is_rejected_without_target_write(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        manifest_directory = Path(self.temporary_data.name, "audit")
        manifest_directory.mkdir()
        outside_target = Path(outside.name, "manifest.jsonl")
        manifest_path = manifest_directory / "manifest.jsonl"
        manifest_path.symlink_to(outside_target)

        with self.assertRaisesRegex(CommandError, "manifest_path_escape"):
            call_command("migrate_media", manifest=str(manifest_path))

        self.assertFalse(outside_target.exists())

    def test_manifest_run_id_path_injection_is_rejected_before_media_write(self):
        call_command("migrate_media", manifest=self.manifest_path)
        event = self._manifest_events()[0]
        event["run_id"] = "../../outside"
        Path(self.manifest_path).write_text(json.dumps(event) + "\n")
        files_before = self._all_media_files()

        with self.assertRaisesRegex(CommandError, "invalid_media_manifest"):
            call_command(
                "migrate_media", execute=True, manifest=self.manifest_path
            )

        self.assertEqual(self._all_media_files(), files_before)

    def test_execute_lock_is_nonblocking_but_dry_run_does_not_take_it(self):
        lock_path = migration_lock_path(self.temporary_data.name)
        self.assertEqual(
            lock_path,
            os.path.join(
                os.path.realpath(self.temporary_data.name),
                "migrations",
                "media-migration.lock",
            ),
        )
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        with open(lock_path, "a+") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(CommandError, "media_migration_locked"):
                call_command(
                    "migrate_media",
                    execute=True,
                    manifest=self.manifest_path,
                )
            call_command("migrate_media", manifest=self.manifest_path)

    def test_default_manifest_is_inside_data_root_and_printed(self):
        stdout = StringIO()

        call_command("migrate_media", stdout=stdout)

        output = stdout.getvalue().strip()
        self.assertTrue(
            output.startswith(os.path.realpath(self.temporary_data.name) + os.sep),
            repr(output),
        )
        self.assertTrue(output.endswith(".jsonl"))
        self.assertTrue(os.path.isfile(output))
        events = [json.loads(line) for line in Path(output).read_text().splitlines()]
        self.assertIn(events[0]["run_id"], os.path.basename(output))


class MediaFileOperationTest(TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)

    def test_resolve_media_path_rejects_absolute_parent_and_symlink_escape(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        Path(self.root.name, "link").symlink_to(
            outside.name, target_is_directory=True
        )

        for relative_name in ("/absolute.png", "../parent.png", "link/a.png"):
            with self.subTest(relative_name=relative_name):
                with self.assertRaises(MediaPathError):
                    resolve_media_path(relative_name, self.root.name)

    def test_migration_lock_rejects_symlinked_migrations_directory(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        Path(self.root.name, "migrations").symlink_to(
            outside.name, target_is_directory=True
        )

        with self.assertRaisesRegex(
            MediaPathError, "migration_lock_path_escape"
        ):
            migration_lock_path(self.root.name)

    def test_publish_link_fallback_reuses_only_identical_destination(self):
        destination = os.path.join(self.root.name, "destination.png")
        expected = make_image_bytes("red")
        first_part = os.path.join(self.root.name, "first.part")
        second_part = os.path.join(self.root.name, "second.part")
        Path(first_part).write_bytes(expected)
        Path(second_part).write_bytes(expected)
        digest = hashlib.sha256(expected).hexdigest()

        with mock.patch("django_images.file_ops.sys.platform", "darwin"):
            first = publish_noreplace(first_part, destination, digest)
            second = publish_noreplace(second_part, destination, digest)

        self.assertTrue(first.created)
        self.assertTrue(second.reused)
        self.assertEqual(Path(destination).read_bytes(), expected)
        self.assertFalse(os.path.exists(first_part))
        self.assertFalse(os.path.exists(second_part))

    def test_publish_conflict_preserves_destination_and_part(self):
        destination = os.path.join(self.root.name, "destination.png")
        destination_bytes = make_image_bytes("red")
        part_bytes = make_image_bytes("blue")
        part = os.path.join(self.root.name, "candidate.part")
        Path(destination).write_bytes(destination_bytes)
        Path(part).write_bytes(part_bytes)

        with mock.patch("django_images.file_ops.sys.platform", "darwin"):
            with self.assertRaisesRegex(MediaPathError, "media_path_conflict"):
                publish_noreplace(
                    part,
                    destination,
                    hashlib.sha256(part_bytes).hexdigest(),
                )

        self.assertEqual(Path(destination).read_bytes(), destination_bytes)
        self.assertEqual(Path(part).read_bytes(), part_bytes)
