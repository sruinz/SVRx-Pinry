from io import BytesIO, StringIO
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest import mock, skipUnless

from django.core.files.storage import default_storage
from django.core.management import CommandError, call_command
from django.test import TestCase, TransactionTestCase, override_settings
from PIL import Image as PILImage

from django_images import file_ops
from django_images.file_ops import (
    MediaPathError,
    migration_lock_path,
    open_staging_noreplace,
    publish_noreplace,
    resolve_media_path,
)
from django_images.models import Image, Thumbnail
from django_images.services.media_migration import (
    ManifestLog,
    MediaMigrator,
    MigrationPlan,
)


def make_image_bytes(color):
    image = BytesIO()
    PILImage.new("RGB", (32, 32), color).save(image, format="PNG")
    return image.getvalue()


class InjectedInterruption(Exception):
    pass


class SimulatedProcessCrash(BaseException):
    pass


class FlushRecordingIO(StringIO):
    def __init__(self):
        super(FlushRecordingIO, self).__init__()
        self.flush_count = 0

    def flush(self):
        self.flush_count += 1
        return super(FlushRecordingIO, self).flush()


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
        return self._manifest_events_for(self.manifest_path)

    def _manifest_events_for(self, path):
        with open(path, encoding="utf-8") as manifest:
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

    def _database_paths(self, image=None):
        image = image or self.image
        image.refresh_from_db()
        paths = {"original": image.image.name}
        paths.update(
            {
                thumbnail.size: thumbnail.image.name
                for thumbnail in image.thumbnail_set.order_by("id")
            }
        )
        return paths

    def _staging_files(self):
        staging = Path(self.temporary_media.name, ".staging")
        if not staging.exists():
            return []
        return sorted(
            path for path in staging.rglob("*") if path.is_file()
        )

    def test_migration_plan_keeps_legacy_fixed_original_slot(self):
        plan = MigrationPlan.for_image(self.image)

        self.assertEqual(
            plan.new_original,
            "originals/{}/original.png".format(self.image.asset_uuid),
        )
        self.assertEqual(
            {entry.new_path for entry in plan.files},
            set(self._canonical_paths().values()),
        )

    def _assert_crash_window_resumes(self, crash_point):
        if crash_point == "after_partial_copy":
            noisy = BytesIO()
            pixels = os.urandom(512 * 512 * 3)
            PILImage.frombytes("RGB", (512, 512), pixels).save(
                noisy,
                format="PNG",
            )
            source_bytes = noisy.getvalue()
            Path(
                self.temporary_media.name,
                self.old_original,
            ).write_bytes(source_bytes)
            self.old_files[self.old_original] = source_bytes
        manifest = ManifestLog(self.manifest_path)
        database_before = self._database_paths()

        def crash(point):
            if point == crash_point:
                raise SimulatedProcessCrash()

        with self.assertRaises(SimulatedProcessCrash):
            MediaMigrator(
                default_storage,
                manifest,
                fault_injector=crash,
            ).run(execute=True)

        self.assertEqual(self._database_paths(), database_before)
        residue = self._staging_files()
        self.assertEqual(len(residue), 1)
        self.assertEqual(
            residue[0].relative_to(self.temporary_media.name).parts[0],
            ".staging",
        )
        if crash_point == "after_partial_copy":
            self.assertLess(
                residue[0].stat().st_size,
                len(self.old_files[self.old_original]),
            )
        self.assertEqual(self._manifest_events()[-1]["event"], "planned")
        residue_stat = residue[0].stat()

        call_command(
            "migrate_media", execute=True, manifest=self.manifest_path
        )

        self.assertEqual(
            self._database_paths()["original"],
            self._canonical_paths()["original"],
        )
        self.assertTrue(residue[0].exists())
        self.assertEqual(residue[0].stat().st_ino, residue_stat.st_ino)
        for old_path, content in self.old_files.items():
            self.assertEqual(
                Path(self.temporary_media.name, old_path).read_bytes(),
                content,
            )

    def _assert_retry_fsyncs_media_root_before_database_update(self, reused):
        self.image.thumbnail_set.all().delete()
        canonical = self._canonical_paths()["original"]
        destination = Path(self.temporary_media.name, canonical)
        media_root_stat = Path(self.temporary_media.name).stat()
        media_root_identity = (
            media_root_stat.st_dev,
            media_root_stat.st_ino,
        )
        database_before = self._database_paths()
        real_fsync = file_ops.os.fsync
        failed_root_fsync = []

        def fail_first_media_root_fsync(descriptor):
            file_stat = os.fstat(descriptor)
            identity = (file_stat.st_dev, file_stat.st_ino)
            if identity == media_root_identity and not failed_root_fsync:
                failed_root_fsync.append(True)
                raise OSError("injected media root fsync failure")
            return real_fsync(descriptor)

        with mock.patch(
            "django_images.file_ops.os.fsync",
            side_effect=fail_first_media_root_fsync,
        ):
            with self.assertRaisesRegex(OSError, "injected media root"):
                call_command(
                    "migrate_media",
                    execute=True,
                    manifest=self.manifest_path,
                )

        self.assertTrue(Path(self.temporary_media.name, "originals").is_dir())
        self.assertFalse(destination.exists())
        self.assertEqual(self._database_paths(), database_before)
        self.assertEqual(
            Path(self.temporary_media.name, self.old_original).read_bytes(),
            self.old_files[self.old_original],
        )

        reused_inode = None
        if reused:
            destination.parent.mkdir(parents=True)
            destination.write_bytes(self.old_files[self.old_original])
            reused_inode = destination.stat().st_ino

        events = []
        real_update = MediaMigrator._update_paths_with_queryset_update

        def record_fsync(descriptor):
            file_stat = os.fstat(descriptor)
            events.append(("fsync", file_stat.st_dev, file_stat.st_ino))
            return real_fsync(descriptor)

        def record_update(migrator, plans):
            events.append(("database_update",))
            return real_update(migrator, plans)

        with mock.patch(
            "django_images.file_ops.os.fsync",
            side_effect=record_fsync,
        ):
            with mock.patch.object(
                MediaMigrator,
                "_update_paths_with_queryset_update",
                autospec=True,
                side_effect=record_update,
            ):
                call_command(
                    "migrate_media",
                    execute=True,
                    manifest=self.manifest_path,
                )

        file_identity = (
            "fsync",
            destination.stat().st_dev,
            destination.stat().st_ino,
        )
        uuid_identity = (
            "fsync",
            destination.parent.stat().st_dev,
            destination.parent.stat().st_ino,
        )
        top_identity = (
            "fsync",
            destination.parent.parent.stat().st_dev,
            destination.parent.parent.stat().st_ino,
        )
        root_identity = ("fsync",) + media_root_identity
        file_index = events.index(file_identity)
        uuid_index = events.index(uuid_identity, file_index + 1)
        top_index = events.index(top_identity, uuid_index + 1)
        database_index = events.index(("database_update",))
        self.assertIn(root_identity, events[top_index + 1:database_index])
        root_index = events.index(root_identity, top_index + 1)
        self.assertLess(file_index, uuid_index)
        self.assertLess(uuid_index, top_index)
        self.assertLess(top_index, root_index)
        self.assertLess(root_index, database_index)
        self.assertEqual(self._database_paths()["original"], canonical)
        self.assertEqual(
            Path(self.temporary_media.name, self.old_original).read_bytes(),
            self.old_files[self.old_original],
        )
        if reused:
            self.assertEqual(destination.stat().st_ino, reused_inode)

    def test_retry_after_failed_media_root_fsync_publishes_before_db_switch(
        self,
    ):
        self._assert_retry_fsyncs_media_root_before_database_update(False)

    def test_retry_after_failed_media_root_fsync_reuses_before_db_switch(
        self,
    ):
        self._assert_retry_fsyncs_media_root_before_database_update(True)

    def test_resume_uses_new_staging_after_crash_just_after_create(self):
        self._assert_crash_window_resumes("after_staging_create")

    def test_resume_uses_new_staging_after_crash_during_partial_copy(self):
        self._assert_crash_window_resumes("after_partial_copy")

    def test_resume_uses_new_staging_after_crash_after_file_fsync(self):
        self._assert_crash_window_resumes("after_staging_fsync")

    def test_resume_uses_new_staging_after_crash_after_image_verify(self):
        self._assert_crash_window_resumes("after_staging_verify")

    def test_handled_copy_error_removes_only_current_owned_staging(self):
        preserved = Path(
            self.temporary_media.name, ".staging", "preserved.part"
        )
        preserved.parent.mkdir()
        preserved.write_bytes(b"previous crash")
        preserved_stat = preserved.stat()
        manifest = ManifestLog(self.manifest_path)

        def interrupt(point):
            if point == "after_partial_copy":
                raise InjectedInterruption()

        with self.assertRaises(InjectedInterruption):
            MediaMigrator(
                default_storage,
                manifest,
                fault_injector=interrupt,
            ).run(execute=True)

        self.assertEqual(self._staging_files(), [preserved])
        self.assertEqual(preserved.read_bytes(), b"previous crash")
        self.assertEqual(preserved.stat().st_ino, preserved_stat.st_ino)
        self.assertEqual(self._database_paths()["original"], self.old_original)

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

    def test_file_and_directory_fsync_chain_precedes_database_update(self):
        events = []
        real_fsync = file_ops.os.fsync
        real_update = MediaMigrator._update_paths_with_queryset_update

        def record_fsync(descriptor):
            file_stat = os.fstat(descriptor)
            events.append(
                ("fsync", file_stat.st_dev, file_stat.st_ino)
            )
            return real_fsync(descriptor)

        def record_update(migrator, plans):
            events.append(("database_update",))
            return real_update(migrator, plans)

        with mock.patch(
            "django_images.file_ops.os.fsync",
            side_effect=record_fsync,
        ):
            with mock.patch.object(
                MediaMigrator,
                "_update_paths_with_queryset_update",
                autospec=True,
                side_effect=record_update,
            ):
                call_command(
                    "migrate_media",
                    execute=True,
                    manifest=self.manifest_path,
                )

        database_index = events.index(("database_update",))
        for kind in ("original", "thumbnail"):
            destination = Path(
                self.temporary_media.name,
                self._canonical_paths()[kind],
            )
            uuid_directory = destination.parent
            top_directory = uuid_directory.parent
            file_identity = (
                "fsync",
                destination.stat().st_dev,
                destination.stat().st_ino,
            )
            uuid_identity = (
                "fsync",
                uuid_directory.stat().st_dev,
                uuid_directory.stat().st_ino,
            )
            top_identity = (
                "fsync",
                top_directory.stat().st_dev,
                top_directory.stat().st_ino,
            )
            file_index = events.index(file_identity)
            uuid_index = events.index(uuid_identity, file_index + 1)
            top_index = events.index(top_identity, uuid_index + 1)
            self.assertLess(file_index, uuid_index)
            self.assertLess(uuid_index, top_index)
            self.assertLess(top_index, database_index)

    def test_reused_destination_fsync_chain_precedes_database_update(self):
        destination = Path(
            self.temporary_media.name,
            self._canonical_paths()["original"],
        )
        destination.parent.mkdir(parents=True)
        destination.write_bytes(self.old_files[self.old_original])
        events = []
        real_fsync = file_ops.os.fsync
        real_update = MediaMigrator._update_paths_with_queryset_update

        def record_fsync(descriptor):
            file_stat = os.fstat(descriptor)
            events.append(
                ("fsync", file_stat.st_dev, file_stat.st_ino)
            )
            return real_fsync(descriptor)

        def record_update(migrator, plans):
            events.append(("database_update",))
            return real_update(migrator, plans)

        with mock.patch(
            "django_images.file_ops.os.fsync",
            side_effect=record_fsync,
        ):
            with mock.patch.object(
                MediaMigrator,
                "_update_paths_with_queryset_update",
                autospec=True,
                side_effect=record_update,
            ):
                call_command(
                    "migrate_media",
                    execute=True,
                    manifest=self.manifest_path,
                )

        file_identity = (
            "fsync",
            destination.stat().st_dev,
            destination.stat().st_ino,
        )
        uuid_identity = (
            "fsync",
            destination.parent.stat().st_dev,
            destination.parent.stat().st_ino,
        )
        top_identity = (
            "fsync",
            destination.parent.parent.stat().st_dev,
            destination.parent.parent.stat().st_ino,
        )
        file_index = events.index(file_identity)
        uuid_index = events.index(uuid_identity, file_index + 1)
        top_index = events.index(top_identity, uuid_index + 1)
        database_index = events.index(("database_update",))
        self.assertLess(file_index, uuid_index)
        self.assertLess(uuid_index, top_index)
        self.assertLess(top_index, database_index)

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

    def test_symlinked_staging_namespace_is_rejected_without_consuming_it(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        outside_path = Path(outside.name, "outside.png")
        outside_content = self.old_files[self.old_original]
        outside_path.write_bytes(outside_content)
        manifest = ManifestLog(self.manifest_path)
        staging_path = Path(self.temporary_media.name, ".staging")
        staging_path.symlink_to(outside.name, target_is_directory=True)
        destination = Path(
            self.temporary_media.name,
            self._canonical_paths()["original"],
        )
        database_before = self._database_paths()

        with self.assertRaisesRegex(CommandError, "media_path_escape"):
            MediaMigrator(default_storage, manifest).run(execute=True)

        self.assertEqual(self._database_paths(), database_before)
        self.assertTrue(staging_path.is_symlink())
        self.assertEqual(os.readlink(str(staging_path)), outside.name)
        self.assertEqual(outside_path.read_bytes(), outside_content)
        self.assertFalse(destination.exists())
        for old_path, content in self.old_files.items():
            self.assertEqual(
                Path(self.temporary_media.name, old_path).read_bytes(),
                content,
            )

    @skipUnless(sys.platform == "darwin", "requires macOS fclonefileat")
    def test_publish_alias_swap_cannot_publish_foreign_inode_or_switch_database(
        self,
    ):
        manifest = ManifestLog(self.manifest_path)
        destination = Path(
            self.temporary_media.name,
            self._canonical_paths()["original"],
        )
        attacker_directory = tempfile.TemporaryDirectory()
        self.addCleanup(attacker_directory.cleanup)
        attacker_path = Path(attacker_directory.name, "attacker.png")
        retained_path = Path(attacker_directory.name, "retained.png")
        attacker_path.write_bytes(self.old_files[self.old_original])
        os.link(attacker_path, retained_path)
        attacker_inode = attacker_path.stat().st_ino
        database_before = self._database_paths()
        real_link = os.link
        real_unlink_owned_name = file_ops._unlink_owned_name

        def swap_alias_before_publish(source, target, *args, **kwargs):
            if (
                isinstance(source, str)
                and source.startswith(".publish-")
                and target == destination.name
            ):
                alias_path = destination.parent / source
                alias_path.unlink()
                real_link(attacker_path, alias_path)
            return real_link(source, target, *args, **kwargs)

        def interrupt_before_part_cleanup(
            directory_descriptor, name, expected_stat
        ):
            if name.startswith("media-migration-"):
                raise InjectedInterruption()
            return real_unlink_owned_name(
                directory_descriptor, name, expected_stat
            )

        with mock.patch("django_images.file_ops.sys.platform", "darwin"):
            with mock.patch(
                "django_images.file_ops.os.link",
                side_effect=swap_alias_before_publish,
            ):
                with mock.patch(
                    "django_images.file_ops._unlink_owned_name",
                    side_effect=interrupt_before_part_cleanup,
                ):
                    with self.assertRaises(InjectedInterruption):
                        MediaMigrator(default_storage, manifest).run(
                            execute=True
                        )

        self.assertEqual(self._database_paths(), database_before)
        self.assertTrue(attacker_path.exists())
        self.assertTrue(retained_path.exists())
        self.assertEqual(attacker_path.stat().st_ino, attacker_inode)
        self.assertEqual(retained_path.stat().st_ino, attacker_inode)
        self.assertTrue(destination.exists())
        self.assertEqual(
            destination.read_bytes(), self.old_files[self.old_original]
        )
        self.assertNotEqual(destination.stat().st_ino, attacker_inode)
        staging_files = self._staging_files()
        self.assertEqual(len(staging_files), 1)
        self.assertEqual(
            staging_files[0].read_bytes(),
            self.old_files[self.old_original],
        )
        for old_path, content in self.old_files.items():
            self.assertEqual(
                Path(self.temporary_media.name, old_path).read_bytes(),
                content,
            )

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

    def test_torn_commit_event_tail_is_quarantined_before_recovery(self):
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
        torn_tail = b'{"schema_version": 1, "event": "comm'
        with open(self.manifest_path, "ab") as manifest_file:
            manifest_file.write(torn_tail)

        call_command(
            "migrate_media", execute=True, manifest=self.manifest_path
        )

        manifest_bytes = Path(self.manifest_path).read_bytes()
        self.assertTrue(manifest_bytes.endswith(b"\n"))
        self.assertNotIn(torn_tail, manifest_bytes)
        self.assertEqual(self._manifest_events()[-1]["event"], "recovered_commit")
        quarantined = list(
            Path(self.manifest_path).parent.glob("test.jsonl.torn-*")
        )
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].read_bytes(), torn_tail)

    def test_complete_invalid_json_tail_remains_fail_closed(self):
        call_command("migrate_media", manifest=self.manifest_path)
        with open(self.manifest_path, "ab") as manifest_file:
            manifest_file.write(b'{"schema_version":\n')
        files_before = self._all_media_files()

        with self.assertRaisesRegex(
            CommandError, "invalid_media_manifest_line:2"
        ):
            call_command(
                "migrate_media", execute=True, manifest=self.manifest_path
            )

        self.assertEqual(self._all_media_files(), files_before)

    def test_invalid_json_in_middle_of_manifest_remains_fail_closed(self):
        call_command("migrate_media", manifest=self.manifest_path)
        valid_line = Path(self.manifest_path).read_bytes()
        Path(self.manifest_path).write_bytes(
            valid_line + b'{"schema_version":\n' + valid_line
        )

        with self.assertRaisesRegex(
            CommandError, "invalid_media_manifest_line:2"
        ):
            ManifestLog.load(self.manifest_path)

    def test_dry_run_does_not_repair_torn_manifest_tail(self):
        call_command("migrate_media", manifest=self.manifest_path)
        torn_tail = b'{"schema_version": 1'
        with open(self.manifest_path, "ab") as manifest_file:
            manifest_file.write(torn_tail)
        manifest_before = Path(self.manifest_path).read_bytes()

        call_command("migrate_media", manifest=self.manifest_path)

        self.assertEqual(Path(self.manifest_path).read_bytes(), manifest_before)
        self.assertFalse(
            list(Path(self.manifest_path).parent.glob("test.jsonl.torn-*"))
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

    def test_manifest_noncanonical_destination_is_rejected_before_copy(self):
        call_command("migrate_media", manifest=self.manifest_path)
        event = self._manifest_events()[0]
        noncanonical = "quarantine/{}/original.png".format(self.image.pk)
        event["new_original"] = noncanonical
        event["files"][0]["new_path"] = noncanonical
        Path(self.manifest_path).write_text(json.dumps(event) + "\n")
        database_before = self._database_paths()
        files_before = self._all_media_files()

        with self.assertRaisesRegex(CommandError, "manifest_plan_mismatch"):
            call_command(
                "migrate_media", execute=True, manifest=self.manifest_path
            )

        self.assertEqual(self._database_paths(), database_before)
        self.assertEqual(self._all_media_files(), files_before)
        self.assertFalse(
            Path(self.temporary_media.name, noncanonical).exists()
        )

    def test_followup_event_must_keep_initial_plan(self):
        call_command("migrate_media", manifest=self.manifest_path)
        planned = self._manifest_events()[0]
        copied = json.loads(json.dumps(planned))
        copied["event"] = "copied"
        copied["new_original"] = "quarantine/original.png"
        copied["files"][0]["new_path"] = copied["new_original"]
        with open(self.manifest_path, "a") as manifest_file:
            manifest_file.write(json.dumps(copied) + "\n")

        with self.assertRaisesRegex(CommandError, "manifest_plan_mismatch"):
            ManifestLog.load(self.manifest_path)

    def test_followup_event_must_follow_valid_state_transition(self):
        call_command("migrate_media", manifest=self.manifest_path)
        planned = self._manifest_events()[0]
        committed = dict(planned, event="committed", execute=True)
        with open(self.manifest_path, "a") as manifest_file:
            manifest_file.write(json.dumps(committed) + "\n")

        with self.assertRaisesRegex(CommandError, "manifest_state_transition"):
            ManifestLog.load(self.manifest_path)

    def test_manifest_derivative_closure_must_match_current_database(self):
        call_command("migrate_media", manifest=self.manifest_path)
        event = self._manifest_events()[0]
        derivative = event["files"][1]
        derivative["derivative_size"] = "standard"
        derivative["new_path"] = derivative["new_path"].replace(
            "thumbnail.png", "standard.png"
        )
        Path(self.manifest_path).write_text(json.dumps(event) + "\n")

        with self.assertRaisesRegex(CommandError, "manifest_plan_mismatch"):
            call_command(
                "migrate_media", execute=True, manifest=self.manifest_path
            )

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

    def test_automatic_manifest_path_is_flushed_before_interrupted_work(self):
        stdout = FlushRecordingIO()

        with mock.patch.object(
            MediaMigrator,
            "run",
            side_effect=InjectedInterruption(),
        ):
            with self.assertRaises(InjectedInterruption):
                call_command("migrate_media", execute=True, stdout=stdout)

        manifest_path = stdout.getvalue().strip()
        self.assertTrue(
            manifest_path.startswith(
                os.path.realpath(self.temporary_data.name) + os.sep
            ),
            repr(manifest_path),
        )
        self.assertTrue(manifest_path.endswith(".jsonl"))
        self.assertGreater(stdout.flush_count, 0)

        call_command("migrate_media", manifest=manifest_path)

        self.assertTrue(Path(manifest_path).is_file())
        self.assertEqual(self._manifest_events_for(manifest_path)[0]["event"], "planned")

    def test_explicit_manifest_path_is_flushed_before_interrupted_work(self):
        stdout = FlushRecordingIO()

        with mock.patch.object(
            MediaMigrator,
            "run",
            side_effect=InjectedInterruption(),
        ):
            with self.assertRaises(InjectedInterruption):
                call_command(
                    "migrate_media",
                    execute=True,
                    manifest=self.manifest_path,
                    stdout=stdout,
                )

        self.assertEqual(
            stdout.getvalue().strip(),
            os.path.realpath(self.manifest_path),
        )
        self.assertGreater(stdout.flush_count, 0)


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

    def test_linux_publish_falls_back_to_proc_descriptor_link(self):
        with mock.patch(
            "django_images.file_ops.sys.platform",
            "linux",
        ), mock.patch(
            "django_images.file_ops._link_descriptor_empty_path",
            return_value=False,
        ) as empty_path, mock.patch(
            "django_images.file_ops._link_descriptor_proc",
            return_value=True,
        ) as proc_path:
            file_ops._publish_linux_descriptor(10, 11, "image.png")

        empty_path.assert_called_once_with(10, 11, "image.png")
        proc_path.assert_called_once_with(10, 11, "image.png")

    def test_linux_publish_stops_after_empty_path_link(self):
        with mock.patch(
            "django_images.file_ops.sys.platform",
            "linux",
        ), mock.patch(
            "django_images.file_ops._link_descriptor_empty_path",
            return_value=True,
        ) as empty_path, mock.patch(
            "django_images.file_ops._link_descriptor_proc",
            return_value=True,
        ) as proc_path:
            file_ops._publish_linux_descriptor(10, 11, "image.png")

        empty_path.assert_called_once_with(10, 11, "image.png")
        proc_path.assert_not_called()

    def test_linux_publish_fails_closed_without_descriptor_link(self):
        with mock.patch(
            "django_images.file_ops.sys.platform",
            "linux",
        ), mock.patch(
            "django_images.file_ops._link_descriptor_empty_path",
            return_value=False,
        ), mock.patch(
            "django_images.file_ops._link_descriptor_proc",
            return_value=False,
        ):
            with self.assertRaisesRegex(
                MediaPathError,
                "atomic_publish_unsupported",
            ):
                file_ops._publish_linux_descriptor(10, 11, "image.png")

    def test_path_publish_attempts_every_owned_close_after_one_error(self):
        staging = Path(self.root.name, ".staging")
        destination_parent = Path(self.root.name, "originals", "asset")
        staging.mkdir()
        destination_parent.mkdir(parents=True)
        part = staging / "candidate.part"
        part.write_bytes(make_image_bytes("red"))
        destination = destination_parent / "original.png"
        real_close = file_ops.os.close
        attempted = []
        failed_descriptor = []

        def fail_first_close(descriptor):
            attempted.append(descriptor)
            if not failed_descriptor:
                failed_descriptor.append(descriptor)
                raise OSError("injected close failure")
            return real_close(descriptor)

        try:
            with mock.patch(
                "django_images.file_ops._publish_verified_descriptor",
                return_value=file_ops.PublishResult(created=True),
            ), mock.patch(
                "django_images.file_ops.os.close",
                side_effect=fail_first_close,
            ):
                with self.assertRaisesRegex(OSError, "close failure"):
                    publish_noreplace(
                        str(part),
                        str(destination),
                        hashlib.sha256(part.read_bytes()).hexdigest(),
                    )

            self.assertEqual(len(attempted), 3)
        finally:
            if failed_descriptor:
                real_close(failed_descriptor[0])

    def test_unique_staging_preserves_error_and_attempts_directory_close(self):
        directory = mock.Mock()
        directory.descriptor = 10

        with mock.patch(
            "django_images.file_ops.open_or_create_media_directory",
            return_value=directory,
        ), mock.patch(
            "django_images.file_ops.os.open",
            return_value=11,
        ), mock.patch(
            "django_images.file_ops.os.fstat",
            side_effect=OSError("injected fstat failure"),
        ), mock.patch(
            "django_images.file_ops.os.close",
            side_effect=OSError("injected close failure"),
        ):
            with self.assertRaisesRegex(OSError, "fstat failure"):
                file_ops.create_unique_staging_file(self.root.name)

        directory.close.assert_called_once_with()

    @skipUnless(sys.platform == "darwin", "requires macOS fclonefileat")
    def test_publish_darwin_fd_clone_reuses_only_identical_destination(self):
        staging = Path(self.root.name, ".staging")
        destination_parent = Path(self.root.name, "originals", "asset")
        staging.mkdir()
        destination_parent.mkdir(parents=True)
        destination = str(destination_parent / "destination.png")
        expected = make_image_bytes("red")
        first_part = str(staging / "first.part")
        second_part = str(staging / "second.part")
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

    def test_staging_creation_fsync_error_removes_only_created_inode(self):
        staging = Path(self.root.name, ".staging")
        staging.mkdir()
        real_fsync = file_ops.os.fsync
        fsync_calls = []

        def fail_first_fsync(descriptor):
            fsync_calls.append(descriptor)
            if len(fsync_calls) == 1:
                raise OSError("injected staging directory fsync failure")
            return real_fsync(descriptor)

        with mock.patch(
            "django_images.file_ops.os.fsync",
            side_effect=fail_first_fsync,
        ):
            with self.assertRaisesRegex(OSError, "injected staging"):
                file_ops.create_unique_staging_file(self.root.name)

        self.assertEqual(list(staging.iterdir()), [])

    def test_publish_descriptor_supports_staging_and_destination_parents(
        self,
    ):
        staging = Path(self.root.name, ".staging")
        destination_parent = Path(
            self.root.name,
            "originals",
            "12345678-1234-5678-1234-567812345678",
        )
        staging.mkdir()
        destination_parent.mkdir(parents=True)
        part = staging / "attempt.part"
        destination = destination_parent / "original.png"
        expected = make_image_bytes("red")
        part.write_bytes(expected)

        result = publish_noreplace(
            str(part),
            str(destination),
            hashlib.sha256(expected).hexdigest(),
        )

        self.assertTrue(result.created)
        self.assertEqual(destination.read_bytes(), expected)
        self.assertFalse(part.exists())

    def test_unsupported_fd_publish_fails_closed_without_consuming_names(self):
        staging = Path(self.root.name, ".staging")
        destination_parent = Path(self.root.name, "originals", "asset")
        staging.mkdir()
        destination_parent.mkdir(parents=True)
        destination = str(destination_parent / "destination.png")
        part = str(staging / "candidate.part")
        unrelated = os.path.join(self.root.name, "unrelated.png")
        expected = make_image_bytes("red")
        Path(part).write_bytes(expected)
        Path(unrelated).write_bytes(expected)

        with mock.patch("django_images.file_ops.sys.platform", "darwin"):
            with mock.patch(
                "django_images.file_ops._clone_descriptor_noreplace",
                return_value=False,
                create=True,
            ):
                with self.assertRaisesRegex(
                    MediaPathError, "atomic_publish_unsupported"
                ):
                    publish_noreplace(
                        part,
                        destination,
                        hashlib.sha256(expected).hexdigest(),
                    )

        self.assertEqual(Path(part).read_bytes(), expected)
        self.assertEqual(Path(unrelated).read_bytes(), expected)
        self.assertFalse(Path(destination).exists())

    def test_publish_conflict_preserves_destination_and_part(self):
        staging = Path(self.root.name, ".staging")
        destination_parent = Path(self.root.name, "originals", "asset")
        staging.mkdir()
        destination_parent.mkdir(parents=True)
        destination = str(destination_parent / "destination.png")
        destination_bytes = make_image_bytes("red")
        part_bytes = make_image_bytes("blue")
        part = str(staging / "candidate.part")
        Path(destination).write_bytes(destination_bytes)
        Path(part).write_bytes(part_bytes)

        with self.assertRaisesRegex(MediaPathError, "media_path_conflict"):
            publish_noreplace(
                part,
                destination,
                hashlib.sha256(part_bytes).hexdigest(),
            )

        self.assertEqual(Path(destination).read_bytes(), destination_bytes)
        self.assertEqual(Path(part).read_bytes(), part_bytes)

    def test_publish_rejects_part_name_replaced_after_open(self):
        staging = Path(self.root.name, ".staging")
        destination_parent = Path(self.root.name, "originals", "asset")
        staging.mkdir()
        destination_parent.mkdir(parents=True)
        destination = str(destination_parent / "destination.png")
        part = str(staging / "candidate.part")
        expected = make_image_bytes("red")
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        outside_path = Path(outside.name, "outside.png")
        outside_path.write_bytes(expected)
        part_descriptor = open_staging_noreplace(part)
        self.addCleanup(os.close, part_descriptor)
        os.write(part_descriptor, expected)
        os.fsync(part_descriptor)
        os.unlink(part)
        Path(part).symlink_to(outside_path)

        with self.assertRaisesRegex(MediaPathError, "unsafe_staging_file"):
            publish_noreplace(
                part,
                destination,
                hashlib.sha256(expected).hexdigest(),
                part_descriptor=part_descriptor,
            )

        self.assertTrue(Path(part).is_symlink())
        self.assertEqual(outside_path.read_bytes(), expected)
        self.assertFalse(os.path.exists(destination))
