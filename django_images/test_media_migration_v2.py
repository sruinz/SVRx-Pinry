from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import uuid
from unittest import mock

from django.core.management import CommandError, call_command
from django.test import TransactionTestCase, override_settings
from PIL import Image as PILImage

from django_images.file_ops import (
    MediaPathError,
    open_verified_media_file,
    open_verified_media_root,
)
from django_images.models import Image, Thumbnail
from django_images.paths import (
    canonical_derivative_path,
    canonical_original_path,
)
from django_images.services.media_migration_v2 import (
    AutoV2ManifestLog,
    AutoV2MediaMigrator,
    AutoV2MigrationPlan,
    AutoV2PlanSummary,
    load_auto_v2_plan,
)


RUN_ID = "20260824T120000Z-12345678-1234-5678-1234-567812345678"
MANIFEST_FILENAME = "media-migration.jsonl"


class SimulatedProcessCrash(BaseException):
    pass


def make_image_bytes(color, image_format="PNG", dimensions=(32, 32)):
    output = BytesIO()
    PILImage.new("RGB", dimensions, color).save(output, format=image_format)
    return output.getvalue()


class FakeDerivative(object):
    def __init__(self, pk, original, image_name, size):
        self.pk = pk
        self.original = original
        self.image = type("ImageName", (), {"name": image_name})()
        self.size = size
        self.width = 32
        self.height = 32


class AutoV2MediaMigrationTest(TransactionTestCase):
    def setUp(self):
        super(AutoV2MediaMigrationTest, self).setUp()
        self.temporary_media = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(self.temporary_media.cleanup)
        self.temporary_data = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(self.temporary_data.cleanup)
        self.run_directory = Path(self.temporary_data.name, RUN_ID)
        self.run_directory.mkdir(mode=0o700)
        os.chmod(str(self.run_directory), 0o700)
        self.manifest_path = self.run_directory / MANIFEST_FILENAME
        self.service_uid = os.geteuid()
        self.service_gid = os.getegid()
        os.chown(
            str(self.run_directory), self.service_uid, self.service_gid
        )
        self.settings_override = override_settings(
            MEDIA_ROOT=self.temporary_media.name,
            PINRY_DATA_ROOT=self.temporary_data.name,
        )
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)

    def write_media(self, relative_path, content):
        target = Path(self.temporary_media.name, relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return target

    def make_image(self, generation="md5", sizes=None, original_name="사진.jpg"):
        sizes = ("thumbnail", "standard", "square") if sizes is None else sizes
        asset_uuid = uuid.uuid4()
        original_target = canonical_original_path(
            asset_uuid, original_name, ".png"
        )
        if generation == "md5":
            original_path = "image/original/by-md5/a/b/{}/upload.jpg".format(
                asset_uuid.hex
            )
        elif generation == "fixed":
            original_path = "originals/{}/original.png".format(asset_uuid)
        elif generation == "named":
            original_path = original_target
        else:
            raise AssertionError("unknown fixture generation")
        self.write_media(original_path, make_image_bytes("red"))
        image = Image.objects.create(
            image=original_path,
            asset_uuid=asset_uuid,
            original_filename=original_name,
            width=32,
            height=32,
        )
        colors = {"thumbnail": "green", "standard": "blue", "square": "yellow"}
        for size in sizes:
            target = canonical_derivative_path(asset_uuid, size, ".png")
            if generation == "md5":
                old_path = "image/thumbnail/by-md5/{}/{}/{}/{}.jpg".format(
                    size[0], size[-1], asset_uuid.hex, size
                )
            else:
                old_path = target
            self.write_media(old_path, make_image_bytes(colors[size]))
            Thumbnail.objects.create(
                original=image,
                image=old_path,
                size=size,
                width=32,
                height=32,
            )
        return image

    def open_root(self):
        root = open_verified_media_root(self.temporary_media.name)
        self.addCleanup(root.close)
        return root

    def migrator(self, fault_injector=None, batch_size=100):
        return AutoV2MediaMigrator(
            str(self.run_directory),
            MANIFEST_FILENAME,
            RUN_ID,
            self.service_uid,
            self.service_gid,
            batch_size=batch_size,
            fault_injector=fault_injector,
        )

    def database_paths(self):
        values = []
        for image in Image.objects.order_by("pk"):
            values.append(image.image.name)
            values.extend(
                thumbnail.image.name
                for thumbnail in image.thumbnail_set.order_by("pk")
            )
        return values

    def destination_entries(self):
        root = Path(self.temporary_media.name)
        destinations = []
        for top_level in ("originals", "derivatives"):
            directory = root / top_level
            if directory.exists():
                destinations.extend(
                    path.relative_to(root).as_posix()
                    for path in directory.rglob("*")
                    if path.is_file()
                )
        return sorted(destinations)

    def manifest_events(self):
        with self.manifest_path.open(encoding="utf-8") as manifest:
            return [json.loads(line) for line in manifest]

    def test_md5_closure_plans_named_targets_from_database_name_and_real_format(self):
        image = self.make_image()

        plan = AutoV2MigrationPlan.for_image(image, self.open_root())

        self.assertEqual(plan.generation, "md5_legacy")
        self.assertEqual(
            plan.new_original,
            canonical_original_path(image.asset_uuid, "사진.jpg", ".png"),
        )
        self.assertEqual({entry.operation for entry in plan.files}, {"copy"})
        self.assertEqual(
            {entry.new_path for entry in plan.files[1:]},
            {
                canonical_derivative_path(image.asset_uuid, size, ".png")
                for size in ("thumbnail", "standard", "square")
            },
        )
        self.assertTrue(all(entry.image_format == "PNG" for entry in plan.files))

    def test_fixed_slot_copies_original_and_verifies_canonical_derivatives(self):
        image = self.make_image(generation="fixed")

        plan = AutoV2MigrationPlan.for_image(image, self.open_root())

        self.assertEqual(plan.generation, "fixed_slot")
        self.assertEqual(plan.files[0].operation, "copy")
        self.assertEqual(
            {entry.operation for entry in plan.files[1:]}, {"verify"}
        )
        self.assertEqual(plan.fixed_slot_archive_sources, (plan.old_original,))

    def test_original_named_original_is_current_not_fixed_slot(self):
        image = self.make_image(generation="named", original_name="original.png")

        plan = AutoV2MigrationPlan.for_image(image, self.open_root())

        self.assertEqual(plan.generation, "named_canonical")
        self.assertTrue(plan.already_current)
        self.assertEqual(plan.fixed_slot_archive_sources, ())

    def test_named_closure_is_identity_verified_and_already_current(self):
        image = self.make_image(generation="named")

        plan = AutoV2MigrationPlan.for_image(image, self.open_root())

        self.assertTrue(plan.already_current)
        self.assertEqual({entry.operation for entry in plan.files}, {"verify"})
        self.assertTrue(all(entry.source_inode > 0 for entry in plan.files))

    def test_missing_derivative_is_allowed_but_not_backfill_eligible(self):
        image = self.make_image(sizes=("thumbnail", "square"))

        plan = AutoV2MigrationPlan.for_image(image, self.open_root())

        self.assertEqual(len(plan.files), 3)
        self.assertFalse(plan.backfill_eligible)

    def test_duplicate_and_unsupported_derivatives_are_rejected(self):
        image = self.make_image(sizes=("thumbnail",))
        record = image.thumbnail_set.get()
        duplicate = FakeDerivative(
            record.pk + 1000, image, record.image.name, "thumbnail"
        )
        with self.assertRaisesRegex(CommandError, "duplicate_derivative_size"):
            AutoV2MigrationPlan.for_image(
                image,
                self.open_root(),
                derivative_records=[record, duplicate],
            )

        unsupported = FakeDerivative(
            record.pk + 1001, image, record.image.name, "preview"
        )
        with self.assertRaisesRegex(
            CommandError, "unsupported_legacy_derivative_size"
        ):
            AutoV2MigrationPlan.for_image(
                image,
                self.open_root(),
                derivative_records=[unsupported],
            )

    def test_auto_v2_freezes_every_plan_before_first_destination_write(self):
        self.make_image()
        broken = self.make_image()
        broken.thumbnail_set.filter(size="square").update(size="preview")
        original_database_paths = self.database_paths()

        with self.assertRaisesRegex(
            CommandError, "unsupported_legacy_derivative_size"
        ):
            self.migrator().run(execute=True)

        self.assertEqual(self.destination_entries(), [])
        self.assertEqual(self.database_paths(), original_database_paths)

    def test_execute_rejects_image_added_after_plan_before_first_write(self):
        planned = self.make_image(sizes=())
        old_path = planned.image.name
        self.migrator().run(execute=False)
        self.make_image(sizes=())

        with self.assertRaisesRegex(
            CommandError, "media_migration_database_changed"
        ):
            self.migrator().run(execute=True)

        planned.refresh_from_db()
        self.assertEqual(planned.image.name, old_path)
        self.assertEqual(self.destination_entries(), [])

    def test_symlink_and_hardlink_sources_fail_closed(self):
        image = self.make_image(sizes=())
        original = Path(self.temporary_media.name, image.image.name)
        real = original.with_name("real.png")
        original.rename(real)
        original.symlink_to(real.name)
        with self.assertRaisesRegex(CommandError, "unsafe_media_file"):
            AutoV2MigrationPlan.for_image(image, self.open_root())

        original.unlink()
        os.link(str(real), str(original))
        with self.assertRaisesRegex(CommandError, "unsafe_media_file"):
            AutoV2MigrationPlan.for_image(image, self.open_root())

    def test_strict_media_root_rejects_root_and_component_symlinks(self):
        parent = Path(self.temporary_data.name, "media-parent")
        real = parent / "real" / "media"
        real.mkdir(parents=True)
        root_link = parent / "root-link"
        root_link.symlink_to(real, target_is_directory=True)
        with self.assertRaisesRegex(MediaPathError, "unsafe_media_directory"):
            open_verified_media_root(str(root_link))

        component_link = parent / "component"
        component_link.symlink_to(parent / "real", target_is_directory=True)
        with self.assertRaisesRegex(MediaPathError, "unsafe_media_directory"):
            open_verified_media_root(str(component_link / "media"))

    def test_media_receipt_detects_path_identity_swap(self):
        image = self.make_image(sizes=())
        root = self.open_root()
        receipt = open_verified_media_file(root, image.image.name)
        self.addCleanup(receipt.close)
        original = Path(self.temporary_media.name, image.image.name)
        replacement = original.with_name("replacement.png")
        replacement.write_bytes(make_image_bytes("black"))
        os.replace(str(replacement), str(original))

        with self.assertRaisesRegex(MediaPathError, "unsafe_media_file"):
            receipt.verify_current()

    def test_manifest_requires_fixed_name_run_parent_mode_owner_and_run_id(self):
        with AutoV2ManifestLog.open(
            str(self.run_directory),
            MANIFEST_FILENAME,
            RUN_ID,
            self.service_uid,
            self.service_gid,
        ):
            pass
        self.assertEqual(stat.S_IMODE(self.manifest_path.stat().st_mode), 0o600)

        with self.assertRaisesRegex(CommandError, "invalid_auto_v2_run_id"):
            AutoV2ManifestLog.open(
                str(self.run_directory),
                MANIFEST_FILENAME,
                str(uuid.uuid4()),
                self.service_uid,
                self.service_gid,
            )
        with self.assertRaisesRegex(CommandError, "invalid_auto_v2_run_id"):
            AutoV2ManifestLog.open(
                str(self.run_directory),
                "media-migration.jsonl",
                "20261340T256199Z-12345678-1234-5678-1234-567812345678",
                self.service_uid,
                self.service_gid,
            )
        with self.assertRaisesRegex(CommandError, "unsafe_auto_v2_manifest"):
            AutoV2ManifestLog.open(
                str(self.run_directory),
                "../escape.jsonl",
                RUN_ID,
                self.service_uid,
                self.service_gid,
            )

    def test_log_loader_and_service_reject_run_outside_data_root(self):
        self.make_image(sizes=())
        for boundary in ("log", "loader", "service"):
            with self.subTest(boundary=boundary):
                with tempfile.TemporaryDirectory(
                    dir="/private/tmp"
                ) as external_data:
                    external_run = Path(external_data, RUN_ID)
                    external_run.mkdir(mode=0o700)
                    os.chmod(str(external_run), 0o700)
                    os.chown(
                        str(external_run),
                        self.service_uid,
                        self.service_gid,
                    )

                    def cross_boundary():
                        if boundary == "log":
                            with AutoV2ManifestLog.open(
                                str(external_run),
                                MANIFEST_FILENAME,
                                RUN_ID,
                                self.service_uid,
                                self.service_gid,
                            ):
                                return None
                        if boundary == "loader":
                            return load_auto_v2_plan(
                                str(external_run),
                                MANIFEST_FILENAME,
                                RUN_ID,
                                self.service_uid,
                                self.service_gid,
                            )
                        return AutoV2MediaMigrator(
                            str(external_run),
                            MANIFEST_FILENAME,
                            RUN_ID,
                            self.service_uid,
                            self.service_gid,
                        ).run(execute=False)

                    with self.assertRaisesRegex(
                        CommandError, "unsafe_auto_v2_manifest"
                    ):
                        cross_boundary()

    def test_v1_or_changed_target_manifest_is_plan_mismatch(self):
        self.manifest_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": str(uuid.uuid4()),
                    "event": "planned",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        os.chmod(str(self.manifest_path), 0o600)
        os.chown(
            str(self.manifest_path), self.service_uid, self.service_gid
        )

        with self.assertRaisesRegex(CommandError, "manifest_plan_mismatch"):
            AutoV2ManifestLog.open(
                str(self.run_directory),
                MANIFEST_FILENAME,
                RUN_ID,
                self.service_uid,
                self.service_gid,
            )

    def test_plan_marker_and_plan_digest_are_stable_after_execute_events(self):
        self.make_image()
        planned = self.migrator().run(execute=False)

        self.assertIsInstance(planned, AutoV2PlanSummary)
        self.assertEqual(self.manifest_events()[-1]["event"], "plan_complete")
        loaded = load_auto_v2_plan(
            str(self.run_directory),
            MANIFEST_FILENAME,
            RUN_ID,
            self.service_uid,
            self.service_gid,
        )
        self.assertEqual(loaded.plan_sha256, planned.plan_sha256)

        executed = self.migrator().run(execute=True)

        self.assertEqual(executed.plan_sha256, planned.plan_sha256)
        self.assertNotEqual(executed.manifest_sha256, planned.manifest_sha256)

    def test_plan_freezes_reusable_destination_identity_and_absence(self):
        image = self.make_image(sizes=())
        original_path = Path(self.temporary_media.name, image.image.name)
        new_path = canonical_original_path(
            image.asset_uuid, image.original_filename, ".png"
        )
        self.migrator().run(execute=False)
        self.write_media(new_path, original_path.read_bytes())

        with self.assertRaisesRegex(CommandError, "destination_collision"):
            self.migrator().run(execute=True)

        image.refresh_from_db()
        self.assertNotEqual(image.image.name, new_path)

        self.manifest_path.unlink()
        destination_path = Path(self.temporary_media.name, new_path)
        self.migrator().run(execute=False)
        replacement_path = destination_path.with_name("replacement.png")
        replacement_path.write_bytes(destination_path.read_bytes())
        os.replace(str(replacement_path), str(destination_path))

        with self.assertRaisesRegex(CommandError, "destination_collision"):
            self.migrator().run(execute=True)

        image.refresh_from_db()
        self.assertNotEqual(image.image.name, new_path)

    def test_manifest_rejects_tampered_noncanonical_plan_before_execute(self):
        self.make_image()
        self.migrator().run(execute=False)
        events = self.manifest_events()
        events[0]["plan"]["files"][0]["new_path"] = "../escape.png"
        self.manifest_path.write_text(
            "".join(
                json.dumps(
                    event,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n"
                for event in events
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            CommandError, "manifest_plan_mismatch|invalid_auto_v2_manifest"
        ):
            load_auto_v2_plan(
                str(self.run_directory),
                MANIFEST_FILENAME,
                RUN_ID,
                self.service_uid,
                self.service_gid,
            )

    def test_open_manifest_detects_external_append_before_adding_event(self):
        self.make_image(generation="named")
        self.migrator().run(execute=False)
        manifest = AutoV2ManifestLog.open(
            str(self.run_directory),
            MANIFEST_FILENAME,
            RUN_ID,
            self.service_uid,
            self.service_gid,
        )
        self.addCleanup(manifest.close)
        with self.manifest_path.open("ab") as external:
            external.write(b"{}\n")
        changed_size = self.manifest_path.stat().st_size

        with self.assertRaisesRegex(CommandError, "unsafe_auto_v2_manifest"):
            manifest.record_result("already_current", Image.objects.get().pk)

        self.assertEqual(self.manifest_path.stat().st_size, changed_size)

    def test_execute_uses_one_shared_media_root_descriptor_for_named_closure(self):
        self.make_image(generation="named")
        self.make_image(generation="named")
        with mock.patch(
            "django_images.services.media_migration_v2.open_verified_media_root",
            wraps=open_verified_media_root,
        ) as strict_root:
            self.migrator().run(execute=True)

        media_root_calls = [
            call
            for call in strict_root.call_args_list
            if call.args[0] == self.temporary_media.name
        ]
        self.assertEqual(len(media_root_calls), 2)

    def test_execute_detects_media_root_replacement_during_shared_stage(self):
        self.make_image()
        old_paths = self.database_paths()
        configured_root = Path(self.temporary_media.name)
        retained_root = configured_root.with_name(
            configured_root.name + "-retained"
        )
        self.addCleanup(shutil.rmtree, str(retained_root), True)

        def replace_root(point):
            if point != "after_source_revalidation":
                return
            configured_root.rename(retained_root)
            configured_root.mkdir()

        with self.assertRaisesRegex(
            CommandError, "media_verification_failed|unsafe_media_directory"
        ):
            self.migrator(fault_injector=replace_root).run(execute=True)

        self.assertEqual(self.database_paths(), old_paths)

    def test_execute_updates_database_only_after_destinations_are_verified(self):
        image = self.make_image()

        summary = self.migrator(batch_size=1).run(execute=True)

        image.refresh_from_db()
        self.assertEqual(
            image.image.name,
            canonical_original_path(image.asset_uuid, "사진.jpg", ".png"),
        )
        self.assertEqual(summary.image_count, 1)
        self.assertTrue(
            all(
                thumbnail.image.name
                == canonical_derivative_path(
                    image.asset_uuid, thumbnail.size, ".png"
                )
                for thumbnail in image.thumbnail_set.all()
            )
        )

    def test_database_commit_rejects_destination_swap_after_batch_verify(self):
        image = self.make_image(sizes=())
        old_path = image.image.name
        destination_relative = canonical_original_path(
            image.asset_uuid, image.original_filename, ".png"
        )
        destination = Path(
            self.temporary_media.name, destination_relative
        )
        swapped = []

        def replace_destination(point):
            if point != "before_database_update" or swapped:
                return
            replacement = destination.with_name("replacement.png")
            replacement.write_bytes(destination.read_bytes())
            os.replace(str(replacement), str(destination))
            swapped.append(True)

        with self.assertRaisesRegex(
            CommandError, "media_verification_failed"
        ):
            self.migrator(
                fault_injector=replace_destination
            ).run(execute=True)

        image.refresh_from_db()
        self.assertEqual(image.image.name, old_path)
        self.assertNotIn(
            "committed",
            {event["event"] for event in self.manifest_events()},
        )

    def test_database_commit_rejects_image_added_during_commit(self):
        planned = self.make_image(sizes=())
        old_path = planned.image.name
        added = []

        def add_image(point):
            if point != "before_database_update" or added:
                return
            added.append(self.make_image(sizes=()).pk)

        with self.assertRaisesRegex(
            CommandError, "media_migration_database_changed"
        ):
            self.migrator(fault_injector=add_image).run(execute=True)

        planned.refresh_from_db()
        self.assertEqual(planned.image.name, old_path)
        self.assertFalse(Image.objects.filter(pk=added[0]).exists())
        self.assertNotIn(
            "committed",
            {event["event"] for event in self.manifest_events()},
        )

    def test_publish_and_database_commit_crashes_resume_same_plan(self):
        for crash_point in ("after_publish", "after_database_commit"):
            with self.subTest(crash_point=crash_point):
                Image.objects.all().delete()
                if self.manifest_path.exists():
                    self.manifest_path.unlink()
                image = self.make_image()

                def crash(point):
                    if point == crash_point:
                        raise SimulatedProcessCrash()

                with self.assertRaises(SimulatedProcessCrash):
                    self.migrator(fault_injector=crash).run(execute=True)

                recovered = self.migrator().run(execute=True)
                image.refresh_from_db()
                self.assertEqual(
                    image.image.name,
                    canonical_original_path(
                        image.asset_uuid, "사진.jpg", ".png"
                    ),
                )
                self.assertEqual(recovered.image_count, 1)
                expected_event = (
                    "recovered_commit"
                    if crash_point == "after_database_commit"
                    else "committed"
                )
                self.assertEqual(
                    self.manifest_events()[-1]["event"], expected_event
                )

    def test_publish_before_published_event_crash_resumes_from_intent(self):
        image = self.make_image(sizes=())

        def crash(point):
            if point == "after_publish_before_event":
                raise SimulatedProcessCrash()

        with self.assertRaises(SimulatedProcessCrash):
            self.migrator(fault_injector=crash).run(execute=True)

        events = self.manifest_events()
        self.assertIn("publish_intent", {event["event"] for event in events})
        self.assertNotIn("published", {event["event"] for event in events})

        self.migrator().run(execute=True)

        image.refresh_from_db()
        self.assertEqual(
            image.image.name,
            canonical_original_path(
                image.asset_uuid, image.original_filename, ".png"
            ),
        )
        self.assertEqual(self.manifest_events()[-1]["event"], "committed")

    def test_publish_intent_before_link_crash_resumes_from_staging(self):
        image = self.make_image(sizes=())

        def crash(point):
            if point == "after_publish_intent":
                raise SimulatedProcessCrash()

        with self.assertRaises(SimulatedProcessCrash):
            self.migrator(fault_injector=crash).run(execute=True)

        destination_relative = canonical_original_path(
            image.asset_uuid, image.original_filename, ".png"
        )
        destination = Path(
            self.temporary_media.name, destination_relative
        )
        self.assertFalse(destination.exists())
        intent = next(
            event
            for event in self.manifest_events()
            if event["event"] == "publish_intent"
        )
        staging = Path(
            self.temporary_media.name,
            ".staging",
            intent["staging_name"],
        )
        staging_stat = os.stat(str(staging))
        self.assertEqual(
            (staging_stat.st_dev, staging_stat.st_ino),
            (intent["staging_device"], intent["staging_inode"]),
        )
        self.assertEqual(staging_stat.st_nlink, 1)
        self.assertEqual(staging_stat.st_uid, self.service_uid)
        self.assertEqual(stat.S_IMODE(staging_stat.st_mode), 0o600)

        self.migrator().run(execute=True)

        image.refresh_from_db()
        self.assertRegex(
            intent["staging_name"],
            r"^auto-v2-[0-9a-f-]{36}\.part$",
        )
        self.assertFalse(
            Path(
                self.temporary_media.name,
                ".staging",
                intent["staging_name"],
            ).exists()
        )
        self.assertEqual(image.image.name, destination_relative)
        self.assertEqual(os.stat(str(destination)).st_nlink, 1)
        self.assertEqual(self.manifest_events()[-1]["event"], "committed")

    def test_publish_fsync_before_staging_unlink_crash_resumes(self):
        image = self.make_image(sizes=())

        def crash(point):
            if point == "after_publish_fsync_before_staging_unlink":
                raise SimulatedProcessCrash()

        with self.assertRaises(SimulatedProcessCrash):
            self.migrator(fault_injector=crash).run(execute=True)

        intent = next(
            event
            for event in self.manifest_events()
            if event["event"] == "publish_intent"
        )
        staging = Path(
            self.temporary_media.name,
            ".staging",
            intent["staging_name"],
        )
        destination_relative = canonical_original_path(
            image.asset_uuid, image.original_filename, ".png"
        )
        destination = Path(
            self.temporary_media.name, destination_relative
        )
        staging_stat = os.stat(str(staging))
        destination_stat = os.stat(str(destination))
        self.assertEqual(staging_stat.st_ino, destination_stat.st_ino)
        self.assertEqual(staging_stat.st_dev, destination_stat.st_dev)
        self.assertEqual(staging_stat.st_nlink, 2)

        self.migrator().run(execute=True)

        image.refresh_from_db()
        self.assertFalse(staging.exists())
        self.assertEqual(image.image.name, destination_relative)
        self.assertEqual(os.stat(str(destination)).st_nlink, 1)
        self.assertEqual(self.manifest_events()[-1]["event"], "committed")

    def test_publish_intent_does_not_adopt_external_destination(self):
        image = self.make_image(sizes=())
        source = Path(self.temporary_media.name, image.image.name)
        destination_relative = canonical_original_path(
            image.asset_uuid, image.original_filename, ".png"
        )
        destination = Path(
            self.temporary_media.name, destination_relative
        )

        def create_external_destination(point):
            if point != "after_publish_intent":
                return
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.read_bytes())

        with self.assertRaisesRegex(CommandError, "destination_collision"):
            self.migrator(
                fault_injector=create_external_destination
            ).run(execute=True)

        image.refresh_from_db()
        self.assertNotEqual(image.image.name, destination_relative)

    def test_recovered_commit_rejects_destination_identity_swap(self):
        image = self.make_image(sizes=())

        def crash(point):
            if point == "after_database_commit":
                raise SimulatedProcessCrash()

        with self.assertRaises(SimulatedProcessCrash):
            self.migrator(fault_injector=crash).run(execute=True)

        image.refresh_from_db()
        destination = Path(self.temporary_media.name, image.image.name)
        replacement = destination.with_name("replacement.png")
        replacement.write_bytes(destination.read_bytes())
        os.replace(str(replacement), str(destination))

        with self.assertRaisesRegex(
            CommandError, "media_verification_failed"
        ):
            self.migrator().run(execute=True)

    def test_torn_tail_is_quarantined_only_during_execute(self):
        self.make_image()
        planned = self.migrator().run(execute=False)
        with self.manifest_path.open("ab") as manifest:
            manifest.write(b'{"format_version":2')

        with self.assertRaisesRegex(
            CommandError, "media_manifest_torn_tail_requires_execute"
        ):
            self.migrator().run(execute=False)

        resumed = self.migrator().run(execute=True)

        self.assertEqual(resumed.plan_sha256, planned.plan_sha256)
        self.assertEqual(
            len(list(self.run_directory.glob("media-migration.jsonl.torn-*"))),
            1,
        )

    def test_torn_tail_repair_rejects_prefix_change_before_truncation(self):
        self.make_image()
        self.migrator().run(execute=False)
        with self.manifest_path.open("ab") as manifest_file:
            manifest_file.write(b'{"format_version":2')
        manifest = AutoV2ManifestLog.open(
            str(self.run_directory),
            MANIFEST_FILENAME,
            RUN_ID,
            self.service_uid,
            self.service_gid,
        )
        self.addCleanup(manifest.close)
        with self.manifest_path.open("r+b") as external:
            external.write(b"X")
            external.flush()
            os.fsync(external.fileno())
        changed = self.manifest_path.read_bytes()

        with self.assertRaisesRegex(CommandError, "unsafe_auto_v2_manifest"):
            manifest.repair_torn_tail()

        self.assertEqual(self.manifest_path.read_bytes(), changed)
        self.assertEqual(
            list(self.run_directory.glob("media-migration.jsonl.torn-*")), []
        )

    def test_command_requires_explicit_auto_v2_manifest_and_run_id(self):
        with self.assertRaisesRegex(CommandError, "auto_v2_manifest_required"):
            call_command("migrate_media", target="auto-v2", run_id=RUN_ID)
        with self.assertRaisesRegex(CommandError, "auto_v2_run_id_required"):
            call_command(
                "migrate_media",
                target="auto-v2",
                manifest=str(self.manifest_path),
            )

    def test_command_uses_verified_manifest_boundary_and_returns_none(self):
        self.make_image()
        stdout = StringIO()
        with mock.patch.object(
            AutoV2ManifestLog,
            "open",
            wraps=AutoV2ManifestLog.open,
        ) as strict_open:
            result = call_command(
                "migrate_media",
                target="auto-v2",
                manifest=str(self.manifest_path),
                run_id=RUN_ID,
                stdout=stdout,
            )

        self.assertIsNone(result)
        self.assertTrue(strict_open.called)
        self.assertNotIn(str(self.manifest_path), stdout.getvalue())

    def test_command_does_not_resolve_away_run_directory_symlink(self):
        self.make_image()
        linked_parent = Path(self.temporary_data.name, "linked-parent")
        linked_parent.symlink_to(self.temporary_data.name, target_is_directory=True)
        linked_manifest = linked_parent / RUN_ID / MANIFEST_FILENAME

        with self.assertRaisesRegex(
            CommandError, "unsafe_auto_v2_manifest"
        ):
            call_command(
                "migrate_media",
                target="auto-v2",
                manifest=str(linked_manifest),
                run_id=RUN_ID,
            )
