from io import BytesIO, StringIO
from dataclasses import replace
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import uuid
from unittest import mock

from django.core.management import CommandError, call_command
from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils.text import get_valid_filename
from PIL import Image as PILImage
from PIL import ImageFile
from PIL import WebPImagePlugin

from django_images.file_ops import (
    MediaPathError,
    open_verified_media_file,
    open_verified_media_root,
    rename_media_noreplace as real_rename_media_noreplace,
)
from django_images.models import Image, Thumbnail
from django_images.paths import (
    canonical_derivative_path,
    canonical_original_path,
)
from django_images.services.media_migration_v2 import (
    AutoV2ManifestLog,
    AutoV2CompletionAuthority,
    AutoV2MediaMigrator,
    AutoV2MigrationFile,
    AutoV2MigrationPlan,
    AutoV2PlanSummary,
    _valid_staging_name,
    load_auto_v2_archive_authority,
    load_auto_v2_archive_direct_roots,
    load_auto_v2_archive_sources,
    load_completed_auto_v2_summary,
    load_auto_v2_plan,
    recover_incomplete_auto_v2_plan,
)
from django_images.services.migration_batch_log import (
    JOURNAL_FILENAME,
    MigrationBatchJournal,
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
        self.syncfs_patch = mock.patch(
            "django_images.services.media_migration_v2._durable_syncfs",
            side_effect=lambda descriptor, reason: os.fsync(descriptor),
        )
        self.syncfs_patch.start()
        self.addCleanup(self.syncfs_patch.stop)

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
        elif generation == "pinry-md5":
            original_path = (
                "a/b/ab0123456789abcdef0123456789abcd/upload.jpg"
            )
        elif generation == "fixed":
            original_path = "originals/{}/original.png".format(asset_uuid)
        elif generation == "django-normalized":
            parent, leaf = original_target.rsplit("/", 1)
            original_path = "{}/{}".format(
                parent, get_valid_filename(leaf)
            )
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
            elif generation == "pinry-md5":
                hash_value = {
                    "thumbnail": "cd0123456789abcdef0123456789abcd",
                    "standard": "ef0123456789abcdef0123456789abcd",
                    "square": "010123456789abcdef0123456789abcd",
                }[size]
                old_path = "{}/{}/{}/{}.jpg".format(
                    hash_value[0], hash_value[1], hash_value, size
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

    def test_freeze_uses_keyset_and_prefetches_derivatives_by_batch(self):
        for _index in range(120):
            self.make_image(generation="named")

        with CaptureQueriesContext(connection) as queries:
            plans = self.migrator(batch_size=50)._freeze_all_plans()

        self.assertEqual(len(plans), 120)
        self.assertLessEqual(len(queries), 12)
        self.assertFalse(
            any(" OFFSET " in query["sql"].upper() for query in queries)
        )

    def test_planner_reads_no_media_bytes_and_never_calls_legacy_inspector(self):
        self.make_image()

        with mock.patch(
            "django_images.services.media_migration_v2._inspect_receipt",
            side_effect=AssertionError("legacy inspector must not run"),
        ), mock.patch(
            "django_images.services.media_migration_v2.PILImage.open",
            side_effect=AssertionError("planner must not decode"),
        ):
            plans = self.migrator()._freeze_all_plans()

        self.assertTrue(plans)

    @mock.patch(
        "django_images.services.media_migration_v2._durable_fsync"
    )
    def test_fresh_plan_is_written_with_one_group_fsync(
        self, durable_fsync
    ):
        self.make_image()
        plans = self.migrator()._freeze_all_plans()
        with AutoV2ManifestLog.open(
            str(self.run_directory),
            MANIFEST_FILENAME,
            RUN_ID,
            self.service_uid,
            self.service_gid,
        ) as manifest:
            durable_fsync.reset_mock()
            manifest.write_frozen_plan(plans)

        self.assertEqual(
            [call.args[1] for call in durable_fsync.call_args_list],
            ["plan_manifest"],
        )

    def test_fresh_plan_builds_state_without_replay_or_raw_materialization(
        self,
    ):
        self.make_image()
        plans = self.migrator()._freeze_all_plans()
        with AutoV2ManifestLog.open(
            str(self.run_directory),
            MANIFEST_FILENAME,
            RUN_ID,
            self.service_uid,
            self.service_gid,
        ) as manifest:
            with mock.patch.object(
                manifest,
                "_load_state",
                side_effect=AssertionError("fresh plan must not replay"),
            ) as load_state, mock.patch.object(
                manifest,
                "_read_all",
                side_effect=AssertionError("fresh plan must not reread"),
            ) as read_all:
                plan_sha256 = manifest.write_frozen_plan(plans)

            self.assertEqual(load_state.call_count, 0)
            self.assertEqual(read_all.call_count, 0)
            self.assertIsNone(manifest.state.raw_bytes)
            self.assertEqual(manifest.summary().plan_sha256, plan_sha256)

    def test_fresh_execute_uses_sidecar_and_keeps_plan_immutable(self):
        image = self.make_image(sizes=())

        before_plan = self.migrator().run(execute=False)
        frozen_bytes = self.manifest_path.read_bytes()
        completed = self.migrator().run(execute=True)

        image.refresh_from_db()
        self.assertEqual(before_plan.plan_sha256, completed.plan_sha256)
        self.assertEqual(self.manifest_path.read_bytes(), frozen_bytes)
        self.assertTrue(Path(self.run_directory, JOURNAL_FILENAME).exists())
        self.assertEqual(
            [event["event"] for event in self.manifest_events()],
            ["planned_skeleton", "plan_complete"],
        )
        self.assertEqual(
            image.image.name,
            canonical_original_path(
                image.asset_uuid, image.original_filename, ".png"
            ),
        )

    def test_normal_run_streams_each_source_once_without_legacy_rehash(self):
        self.make_image(sizes=())
        migrator = self.migrator()

        with mock.patch.object(
            migrator,
            "_iter_source_chunks",
            wraps=migrator._iter_source_chunks,
        ) as source_chunks, mock.patch.object(
            migrator,
            "_rehash_resume_candidate",
            side_effect=AssertionError("normal run must not rehash"),
        ), mock.patch(
            "django_images.services.media_migration_v2._inspect_receipt",
            side_effect=AssertionError("normal run must not inspect twice"),
        ):
            migrator.run(execute=True)

        self.assertEqual(source_chunks.call_count, 1)

    def test_each_source_uses_one_parser_close(self):
        self.make_image(sizes=())
        parser = ImageFile.Parser()

        with mock.patch(
            "django_images.services.media_migration_v2.ImageFile.Parser",
            return_value=parser,
        ), mock.patch.object(
            parser, "close", wraps=parser.close
        ) as close:
            self.migrator().run(execute=True)

        self.assertEqual(close.call_count, 1)

    def test_batch_durability_precedes_intent_database_and_commit(self):
        from django_images.services import media_migration_v2 as service

        self.make_image(sizes=())
        migrator = self.migrator()
        events = []
        real_publish = service.publish_preverified_noreplace
        real_intent = MigrationBatchJournal.append_intent
        real_commit = MigrationBatchJournal.append_commit
        real_database = migrator._apply_database_batch

        def syncfs(descriptor, reason):
            events.append(reason)
            os.fsync(descriptor)

        def durable_fsync(descriptor, reason):
            if reason == "publication_directory":
                events.append(reason)
            os.fsync(descriptor)

        def publish(*args, **kwargs):
            events.append("publish")
            return real_publish(*args, **kwargs)

        def append_intent(journal, intent):
            events.append("intent")
            return real_intent(journal, intent)

        def apply_database(*args, **kwargs):
            events.append("database")
            return real_database(*args, **kwargs)

        def append_commit(journal, batch_id, database_signature):
            events.append("commit")
            return real_commit(journal, batch_id, database_signature)

        with mock.patch(
            "django_images.services.media_migration_v2._durable_syncfs",
            side_effect=syncfs,
        ), mock.patch(
            "django_images.services.media_migration_v2._durable_fsync",
            side_effect=durable_fsync,
        ), mock.patch(
            "django_images.services.media_migration_v2."
            "publish_preverified_noreplace",
            side_effect=publish,
        ), mock.patch.object(
            MigrationBatchJournal,
            "append_intent",
            autospec=True,
            side_effect=append_intent,
        ), mock.patch.object(
            migrator,
            "_apply_database_batch",
            side_effect=apply_database,
        ), mock.patch.object(
            MigrationBatchJournal,
            "append_commit",
            autospec=True,
            side_effect=append_commit,
        ):
            migrator.run(execute=True)

        order = [
            "batch_file_data",
            "publish",
            "publication_directory",
            "intent",
            "database",
            "commit",
        ]
        self.assertEqual(
            sorted(events.index(value) for value in order),
            [events.index(value) for value in order],
        )

    def test_syncfs_failure_records_no_batch_intent(self):
        image = self.make_image(sizes=())
        self.migrator().run(execute=False)

        with mock.patch(
            "django_images.services.media_migration_v2._durable_syncfs",
            side_effect=OSError("injected syncfs failure"),
        ), self.assertRaises(OSError):
            self.migrator().run(execute=True)

        self.assertFalse(any(
            event["event"] == "batch_intent"
            for event in self.journal_events()
        ))

        migrator = self.migrator()
        with mock.patch.object(
            migrator,
            "_iter_source_chunks",
            wraps=migrator._iter_source_chunks,
        ) as source_chunks:
            migrator.run(execute=True)

        self.assertEqual(source_chunks.call_count, 0)
        image.refresh_from_db()
        self.assertEqual(
            image.image.name,
            canonical_original_path(
                image.asset_uuid, image.original_filename, ".png"
            ),
        )

    def test_mid_batch_publish_crash_reuses_remaining_staging(self):
        image = self.make_image(sizes=("thumbnail",))
        publish_calls = []

        def crash_before_second_publish(point):
            if point != "before_atomic_publish":
                return
            publish_calls.append(point)
            if len(publish_calls) == 2:
                raise SimulatedProcessCrash()

        with self.assertRaises(SimulatedProcessCrash):
            self.migrator(
                fault_injector=crash_before_second_publish
            ).run(execute=True)

        migrator = self.migrator()
        with mock.patch.object(
            migrator,
            "_iter_source_chunks",
            wraps=migrator._iter_source_chunks,
        ) as source_chunks:
            migrator.run(execute=True)

        self.assertEqual(source_chunks.call_count, 1)
        image.refresh_from_db()
        self.assertEqual(
            image.image.name,
            canonical_original_path(
                image.asset_uuid, image.original_filename, ".png"
            ),
        )
        thumbnail = image.thumbnail_set.get(size="thumbnail")
        self.assertEqual(
            thumbnail.image.name,
            canonical_derivative_path(
                image.asset_uuid, "thumbnail", ".png"
            ),
        )

    def test_global_plan_closure_runs_exactly_before_and_after_batches(self):
        self.make_image(sizes=())
        self.make_image(sizes=())
        migrator = self.migrator(batch_size=1)

        with mock.patch.object(
            migrator,
            "_validate_image_plan_closure",
            wraps=migrator._validate_image_plan_closure,
        ) as closure:
            migrator.run(execute=True)

        self.assertEqual(closure.call_count, 2)

    def _assert_thumbnail_change_rejected(self, plans, before_signature):
        migrator = self.migrator()
        self.assertNotEqual(
            migrator._current_batch_signature(plans), before_signature
        )
        with self.assertRaisesRegex(
            CommandError, "^media_migration_database_changed$"
        ):
            migrator._validate_image_plan_closure(plans)

    def test_thumbnail_addition_breaks_batch_and_global_plan_closure(self):
        image = self.make_image(sizes=("thumbnail",))
        plans = self.migrator()._freeze_all_plans()
        before_signature = self.migrator()._current_batch_signature(plans)
        thumbnail = image.thumbnail_set.get(size="thumbnail")
        Thumbnail.objects.create(
            original=image,
            image=thumbnail.image.name,
            size="added-after-plan",
            width=thumbnail.width,
            height=thumbnail.height,
        )

        self._assert_thumbnail_change_rejected(plans, before_signature)

    def test_thumbnail_deletion_breaks_batch_and_global_plan_closure(self):
        image = self.make_image(sizes=("thumbnail",))
        plans = self.migrator()._freeze_all_plans()
        before_signature = self.migrator()._current_batch_signature(plans)
        image.thumbnail_set.get(size="thumbnail").delete()

        self._assert_thumbnail_change_rejected(plans, before_signature)

    def test_thumbnail_metadata_breaks_batch_and_global_plan_closure(self):
        image = self.make_image(sizes=("thumbnail",))
        plans = self.migrator()._freeze_all_plans()
        before_signature = self.migrator()._current_batch_signature(plans)
        image.thumbnail_set.filter(size="thumbnail").update(width=31)

        self._assert_thumbnail_change_rejected(plans, before_signature)

    def test_final_closure_rejects_committed_thumbnail_path_change(self):
        image = self.make_image(sizes=("thumbnail",))
        changed = []

        def change_committed_thumbnail(point):
            if point != "after_batch_commit" or changed:
                return
            thumbnail = image.thumbnail_set.get(size="thumbnail")
            Thumbnail.objects.filter(pk=thumbnail.pk).update(
                image="external/changed-after-commit.png"
            )
            changed.append(True)

        with self.assertRaisesRegex(
            CommandError, "^media_migration_database_changed$"
        ):
            self.migrator(
                fault_injector=change_committed_thumbnail
            ).run(execute=True)

        self.assertEqual(changed, [True])
        self.assertFalse(any(
            event["event"] == "phase_complete"
            for event in self.journal_events()
        ))

    def test_batch_closes_at_first_resource_limit(self):
        for _index in range(3):
            self.make_image(generation="named", sizes=())
        plans = self.migrator()._freeze_all_plans()
        plans = [
            replace(
                plan,
                files=(replace(plan.files[0], size=10, width=20000,
                               height=10000),),
                image_width=20000,
                image_height=10000,
            )
            for plan in plans
        ]

        batches = list(self.migrator()._build_batches(plans))

        self.assertEqual([len(batch) for batch in batches], [2, 1])

    def test_sidecar_only_loads_summary_and_archive_authority(self):
        image = self.make_image(sizes=())
        completed = self.migrator().run(execute=True)
        frozen_bytes = self.manifest_path.read_bytes()
        with AutoV2ManifestLog.open(
            str(self.run_directory),
            MANIFEST_FILENAME,
            RUN_ID,
            self.service_uid,
            self.service_gid,
            create=False,
        ) as manifest:
            journal = MigrationBatchJournal.open(
                manifest.run_directory,
                JOURNAL_FILENAME,
                RUN_ID,
                self.service_uid,
                self.service_gid,
                completed.plan_sha256,
                completed.manifest_sha256,
                create=False,
            )
            self.addCleanup(journal.close)
            summary = load_completed_auto_v2_summary(
                str(self.run_directory),
                MANIFEST_FILENAME,
                RUN_ID,
                self.service_uid,
                self.service_gid,
                batch_journal=journal,
            )
            authority = load_auto_v2_archive_authority(
                str(self.run_directory),
                MANIFEST_FILENAME,
                RUN_ID,
                self.service_uid,
                self.service_gid,
                batch_journal=journal,
            )

        self.assertEqual(summary.image_count, 1)
        self.assertEqual(authority.summary, summary)
        self.assertEqual(authority.prefixed_files[0].old_path, image.image.name)
        self.assertEqual(self.manifest_path.read_bytes(), frozen_bytes)

    def test_restart_after_database_commit_appends_commit_without_recopy(self):
        image = self.make_image(sizes=())

        def crash(point):
            if point == "after_database_commit":
                raise SimulatedProcessCrash()

        with self.assertRaises(SimulatedProcessCrash):
            self.migrator(fault_injector=crash).run(execute=True)
        image.refresh_from_db()
        migrated_path = image.image.name
        resumed = self.migrator()
        with mock.patch.object(
            resumed,
            "_stream_source_to_staging_and_inspect",
            side_effect=AssertionError("committed database must not recopy"),
        ):
            resumed.run(execute=True)

        image.refresh_from_db()
        self.assertEqual(image.image.name, migrated_path)
        self.assertEqual(
            [
                event["event"]
                for event in self.journal_events()
                if event["event"] in ("batch_intent", "batch_commit")
            ],
            ["batch_intent", "batch_commit"],
        )

    def test_restart_after_destination_rename_reuses_durable_orphan(self):
        image = self.make_image(sizes=())

        def crash(point):
            if point == "after_destination_rename":
                raise SimulatedProcessCrash()

        with self.assertRaises(SimulatedProcessCrash):
            self.migrator(fault_injector=crash).run(execute=True)
        image.refresh_from_db()
        self.assertTrue(image.image.name.startswith("image/"))
        resumed = self.migrator()
        with mock.patch.object(
            resumed,
            "_rehash_resume_candidate",
            wraps=resumed._rehash_resume_candidate,
        ) as rehash:
            resumed.run(execute=True)

        image.refresh_from_db()
        self.assertTrue(image.image.name.startswith("originals/"))
        self.assertEqual(rehash.call_count, 1)

    def test_committed_destination_corruption_repairs_from_source(self):
        image = self.make_image(sizes=())
        source = Path(self.temporary_media.name, image.image.name)
        expected = source.read_bytes()
        self.migrator().run(execute=True)
        image.refresh_from_db()
        destination = Path(self.temporary_media.name, image.image.name)
        before_inode = destination.stat().st_ino
        destination.write_bytes(b"corrupt")

        self.migrator().run(execute=True)

        self.assertEqual(destination.read_bytes(), expected)
        self.assertNotEqual(destination.stat().st_ino, before_inode)
        self.assertEqual(
            sum(
                event["event"] == "batch_repair"
                for event in self.journal_events()
            ),
            1,
        )

    def test_committed_missing_destination_repairs_from_source(self):
        image = self.make_image(sizes=())
        source = Path(self.temporary_media.name, image.image.name)
        expected = source.read_bytes()
        self.migrator().run(execute=True)
        image.refresh_from_db()
        destination = Path(self.temporary_media.name, image.image.name)
        destination.unlink()

        self.migrator().run(execute=True)

        self.assertEqual(destination.read_bytes(), expected)
        self.assertEqual(
            sum(
                event["event"] == "batch_repair"
                for event in self.journal_events()
            ),
            1,
        )

    def test_completion_authority_rejects_ambiguous_receipts(self):
        self.make_image(sizes=())
        summary = self.migrator().run(execute=True)
        with AutoV2ManifestLog.open(
            str(self.run_directory),
            MANIFEST_FILENAME,
            RUN_ID,
            self.service_uid,
            self.service_gid,
            create=False,
        ) as manifest:
            journal = MigrationBatchJournal.open(
                manifest.run_directory,
                JOURNAL_FILENAME,
                RUN_ID,
                self.service_uid,
                self.service_gid,
                summary.plan_sha256,
                summary.manifest_sha256,
                create=False,
            )
            self.addCleanup(journal.close)
            receipts = journal.receipts_by_image("paths")
            image_id = next(iter(receipts))
            receipt = receipts[image_id][0]
            mutations = (
                {},
                {image_id: (receipt, receipt)},
                {image_id: (replace(
                    receipt, source_inode=receipt.source_inode + 1
                ),)},
            )
            for mutation in mutations:
                with self.subTest(mutation=mutation):
                    with mock.patch.object(
                        journal,
                        "receipts_by_image",
                        return_value=mutation,
                    ), self.assertRaises(CommandError):
                        AutoV2CompletionAuthority.load(manifest, journal)

    def test_committed_destination_corruption_rejects_changed_source(self):
        image = self.make_image(sizes=())
        source = Path(self.temporary_media.name, image.image.name)
        self.migrator().run(execute=True)
        image.refresh_from_db()
        destination = Path(self.temporary_media.name, image.image.name)
        destination.write_bytes(b"corrupt")
        source.write_bytes(make_image_bytes("purple"))

        with self.assertRaisesRegex(
            CommandError, "linear_committed_source_changed"
        ):
            self.migrator().run(execute=True)

    def test_complete_v2_is_noop_without_creating_sidecar(self):
        image = self.make_image(sizes=())
        self.write_v2_original_plan(image, terminal=True)
        original = self.manifest_path.read_bytes()

        summary = self.migrator().run(execute=True)

        self.assertEqual(summary.image_count, 1)
        self.assertEqual(self.manifest_path.read_bytes(), original)
        self.assertFalse(Path(self.run_directory, JOURNAL_FILENAME).exists())

    def test_partial_v2_manifest_is_immutable_while_sidecar_resumes(self):
        image = self.make_image(sizes=())
        self.write_v2_original_plan(image, terminal=False)
        original = self.manifest_path.read_bytes()

        summary = self.migrator().run(execute=True)

        image.refresh_from_db()
        self.assertEqual(summary.image_count, 1)
        self.assertTrue(image.image.name.startswith("originals/"))
        self.assertEqual(self.manifest_path.read_bytes(), original)
        self.assertTrue(Path(self.run_directory, JOURNAL_FILENAME).exists())

    def test_copying_v2_imports_terminal_prefix_then_resumes_pending(self):
        first = self.make_image(sizes=())
        second = self.make_image(sizes=())
        self.write_v2_plans((first, second), completed=1)
        original = self.manifest_path.read_bytes()
        migrator = self.migrator(batch_size=1)

        with mock.patch.object(
            migrator,
            "_stream_source_to_staging_and_inspect",
            wraps=migrator._stream_source_to_staging_and_inspect,
        ) as stream:
            migrator.run(execute=True)

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertTrue(first.image.name.startswith("originals/"))
        self.assertTrue(second.image.name.startswith("originals/"))
        self.assertEqual(stream.call_count, 1)
        self.assertEqual(self.manifest_path.read_bytes(), original)
        self.assertEqual(
            [
                event["intent"]["batch_id"]
                for event in self.journal_events()
                if event["event"] == "batch_intent"
            ],
            [
                "upgrade-paths:{}-{}".format(first.pk, first.pk),
                "paths:{}-{}".format(second.pk, second.pk),
            ],
        )

    def test_v2_upgrade_rejects_changed_old_manifest_hash(self):
        image = self.make_image(sizes=())
        plan = self.write_v2_original_plan(image, terminal=False)

        def crash(point):
            if point == "after_destination_rename":
                raise SimulatedProcessCrash()

        with self.assertRaises(SimulatedProcessCrash):
            self.migrator(fault_injector=crash).run(execute=True)
        original = self.manifest_path.read_bytes()
        old_value = str(plan.files[0].source_inode).encode("ascii")
        replacement = str(plan.files[0].source_inode + 1).encode("ascii")
        if len(old_value) != len(replacement):
            replacement = str(plan.files[0].source_inode - 1).encode(
                "ascii"
            )
        changed = original.replace(
            b'"source_inode":' + old_value,
            b'"source_inode":' + replacement,
            1,
        )
        self.assertNotEqual(changed, original)
        self.manifest_path.write_bytes(changed)

        with self.assertRaisesRegex(
            CommandError, "linear_journal_source_changed"
        ):
            self.migrator().run(execute=True)

    def alternate_service_gid(self):
        for group_id in os.getgroups():
            if group_id != self.service_gid:
                return group_id
        self.skipTest("alternate service group is unavailable")

    def use_distinct_root_service_identity(self):
        if os.geteuid() != 0:
            self.skipTest("root-created staging ownership test")
        self.service_uid = 1000
        self.service_gid = 1000
        os.chown(
            str(self.run_directory), self.service_uid, self.service_gid
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

    def journal_events(self):
        path = Path(self.run_directory, JOURNAL_FILENAME)
        return [
            json.loads(line)["payload"]
            for line in path.read_text(encoding="utf-8").splitlines()
        ]

    def completed_authority(self):
        with AutoV2ManifestLog.open(
            str(self.run_directory),
            MANIFEST_FILENAME,
            RUN_ID,
            self.service_uid,
            self.service_gid,
            create=False,
        ) as manifest:
            summary = manifest.summary()
            journal = MigrationBatchJournal.open(
                manifest.run_directory,
                JOURNAL_FILENAME,
                RUN_ID,
                self.service_uid,
                self.service_gid,
                summary.plan_sha256,
                summary.manifest_sha256,
                create=False,
            )
            try:
                return AutoV2CompletionAuthority.load(manifest, journal)
            finally:
                journal.close()

    def make_v2_original_plan(self, image):
        old_path = image.image.name
        source = Path(self.temporary_media.name, old_path)
        content = source.read_bytes()
        source_stat = source.stat()
        image_root_stat = Path(
            self.temporary_media.name, "image"
        ).stat()
        new_path = canonical_original_path(
            image.asset_uuid, image.original_filename, ".png"
        )
        file_plan = AutoV2MigrationFile(
            kind="original",
            old_path=old_path,
            new_path=new_path,
            operation="copy",
            size=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            image_format="PNG",
            width=32,
            height=32,
            source_device=source_stat.st_dev,
            source_inode=source_stat.st_ino,
            archive_root_device=image_root_stat.st_dev,
            archive_root_inode=image_root_stat.st_ino,
        )
        plan = AutoV2MigrationPlan(
            image_id=image.pk,
            asset_uuid=str(image.asset_uuid),
            original_filename=image.original_filename,
            image_width=32,
            image_height=32,
            generation="md5_legacy",
            files=(file_plan,),
            thumbnail_rows=(),
            copy_required_bytes=len(content),
        )
        return plan, content, new_path

    def write_v2_plans(self, images, completed=0):
        values = tuple(
            self.make_v2_original_plan(image) for image in images
        )
        plans = tuple(value[0] for value in values)
        with AutoV2ManifestLog.open(
            str(self.run_directory),
            MANIFEST_FILENAME,
            RUN_ID,
            self.service_uid,
            self.service_gid,
        ) as manifest:
            for plan in plans:
                manifest.record_plan(plan)
            manifest.record_plan_complete(plans)
            for image, value in zip(images[:completed], values[:completed]):
                plan, content, new_path = value
                self.write_media(new_path, content)
                Image.objects.filter(pk=image.pk).update(image=new_path)
                manifest.record_result("committed", image.pk)
        return plans

    def write_v2_original_plan(self, image, terminal=False):
        return self.write_v2_plans(
            (image,), completed=1 if terminal else 0
        )[0]

    def primitive_window_staging_swap(self):
        swapped_names = []
        replacement_bytes = b"primitive-window-replacement"
        staging_directory = Path(self.temporary_media.name, ".staging")

        def swap_before_atomic_publish(point):
            if point != "before_atomic_publish" or swapped_names:
                return
            directory_descriptor = os.open(
                str(staging_directory),
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
            try:
                staging_names = [
                    name
                    for name in os.listdir(str(staging_directory))
                    if _valid_staging_name(name)
                ]
                self.assertEqual(len(staging_names), 1)
                name = staging_names[0]
                replacement_name = "swap-{}.part".format(uuid.uuid4())
                descriptor = os.open(
                    replacement_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=directory_descriptor,
                )
                try:
                    os.write(descriptor, replacement_bytes)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                os.replace(
                    replacement_name,
                    name,
                    src_dir_fd=directory_descriptor,
                    dst_dir_fd=directory_descriptor,
                )
                swapped_names.append(name)
            finally:
                os.close(directory_descriptor)

        return swap_before_atomic_publish, swapped_names, replacement_bytes

    def destination_replacement_after_atomic_publish(self):
        replacement_bytes = b"external-destination-replacement"
        replacement_identity = []

        def rename_then_replace(
            source_directory,
            source_name,
            destination_directory,
            destination_name,
        ):
            real_rename_media_noreplace(
                source_directory,
                source_name,
                destination_directory,
                destination_name,
            )
            if replacement_identity:
                return
            replacement_name = "external-{}.part".format(uuid.uuid4())
            descriptor = os.open(
                replacement_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=destination_directory.descriptor,
            )
            try:
                os.write(descriptor, replacement_bytes)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(
                replacement_name,
                destination_name,
                src_dir_fd=destination_directory.descriptor,
                dst_dir_fd=destination_directory.descriptor,
            )
            destination_stat = os.stat(
                destination_name,
                dir_fd=destination_directory.descriptor,
                follow_symlinks=False,
            )
            replacement_identity.append(
                (destination_stat.st_dev, destination_stat.st_ino)
            )

        return rename_then_replace, replacement_identity, replacement_bytes

    def test_md5_closure_plans_named_targets_from_database_name_and_real_format(self):
        image = self.make_image()

        plan = AutoV2MigrationPlan.for_image(image, self.open_root())

        self.assertIsNone(plan.generation)
        self.assertIsNone(plan.new_original)
        self.assertEqual({entry.operation for entry in plan.files}, {None})
        self.assertEqual({entry.new_path for entry in plan.files}, {None})
        self.assertTrue(
            all(entry.image_format is None for entry in plan.files)
        )
        image_root_stat = os.stat(
            str(Path(self.temporary_media.name, "image"))
        )
        self.assertEqual(
            {
                (entry.archive_root_device, entry.archive_root_inode)
                for entry in plan.files
            },
            {(image_root_stat.st_dev, image_root_stat.st_ino)},
        )

    def test_pinry_direct_md5_closure_plans_named_targets(self):
        image = self.make_image(generation="pinry-md5")

        plan = AutoV2MigrationPlan.for_image(image, self.open_root())

        self.assertIsNone(plan.generation)
        self.assertIsNone(plan.new_original)
        self.assertEqual({entry.operation for entry in plan.files}, {None})

    def test_fixed_slot_copies_original_and_verifies_canonical_derivatives(self):
        image = self.make_image(generation="fixed")

        plan = AutoV2MigrationPlan.for_image(image, self.open_root())

        self.assertIsNone(plan.generation)
        self.assertTrue(all(
            entry.operation is None and entry.new_path is None
            for entry in plan.files
        ))
        self.assertEqual(plan.fixed_slot_archive_sources, ())

    def test_django_normalized_original_is_migrated_and_archived(self):
        image = self.make_image(
            generation="django-normalized",
            original_name="스크린샷 2026-08-20 21.23.05.png",
        )
        old_path = image.image.name
        expected_path = canonical_original_path(
            image.asset_uuid,
            image.original_filename,
            ".png",
        )

        plan = AutoV2MigrationPlan.for_image(image, self.open_root())

        self.assertIsNone(plan.generation)
        self.assertTrue(all(
            entry.operation is None and entry.new_path is None
            for entry in plan.files
        ))

        self.migrator().run(execute=True)

        image.refresh_from_db()
        self.assertEqual(image.image.name, expected_path)
        self.assertTrue(Path(self.temporary_media.name, old_path).is_file())
        self.assertEqual(
            self.completed_authority().archive_authority.fixed_slot_sources,
            (old_path,),
        )

    def test_original_named_original_is_current_not_fixed_slot(self):
        image = self.make_image(generation="named", original_name="original.png")

        plan = AutoV2MigrationPlan.for_image(image, self.open_root())

        self.assertIsNone(plan.generation)
        self.assertFalse(plan.already_current)
        self.assertEqual(plan.fixed_slot_archive_sources, ())

    def test_named_closure_is_identity_verified_and_already_current(self):
        image = self.make_image(generation="named")

        plan = AutoV2MigrationPlan.for_image(image, self.open_root())

        self.assertFalse(plan.already_current)
        self.assertEqual({entry.operation for entry in plan.files}, {None})
        self.assertTrue(all(entry.source_inode > 0 for entry in plan.files))

    def test_existing_valid_image_above_pillow_warning_limit_is_migrated(self):
        image = self.make_image(generation="pinry-md5", sizes=())
        self.write_media(
            image.image.name,
            make_image_bytes("red", image_format="WEBP"),
        )
        expected_path = canonical_original_path(
            image.asset_uuid,
            image.original_filename,
            ".webp",
        )

        with mock.patch.object(PILImage, "MAX_IMAGE_PIXELS", 600):
            with mock.patch.object(
                WebPImagePlugin.WebPImageFile,
                "load",
                wraps=WebPImagePlugin.WebPImageFile.load,
                autospec=True,
            ) as load:
                self.migrator().run(execute=True)

        self.assertEqual(load.call_count, 1)

        image.refresh_from_db()
        self.assertEqual(image.image.name, expected_path)
        self.assertTrue(
            Path(self.temporary_media.name, expected_path).is_file()
        )

    def test_existing_image_above_pillow_hard_limit_is_rejected_safely(self):
        self.make_image(generation="pinry-md5", sizes=())

        with mock.patch.object(PILImage, "MAX_IMAGE_PIXELS", 500):
            with self.assertRaisesRegex(
                CommandError,
                "invalid_legacy_media",
            ):
                self.migrator().run(execute=True)

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

    def test_staging_name_validation_requires_exact_canonical_basename(self):
        canonical = "auto-v2-12345678-1234-5678-1234-567812345678.part"

        self.assertTrue(_valid_staging_name(canonical))
        for invalid in (
            canonical + "\n",
            canonical + "/child",
            canonical + ".other",
        ):
            with self.subTest(invalid=invalid):
                self.assertFalse(_valid_staging_name(invalid))

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
        self.assertEqual(executed.manifest_sha256, planned.manifest_sha256)
        self.assertEqual(
            self.completed_authority().summary,
            executed,
        )

    def test_public_plan_loader_never_creates_a_missing_manifest(self):
        self.assertFalse(self.manifest_path.exists())

        with self.assertRaisesRegex(
            CommandError,
            "^unsafe_auto_v2_manifest$",
        ):
            load_auto_v2_plan(
                str(self.run_directory),
                MANIFEST_FILENAME,
                RUN_ID,
                self.service_uid,
                self.service_gid,
            )

        self.assertFalse(self.manifest_path.exists())
        with self.assertRaisesRegex(
            CommandError,
            "^unsafe_auto_v2_manifest$",
        ):
            load_completed_auto_v2_summary(
                str(self.run_directory),
                MANIFEST_FILENAME,
                RUN_ID,
                self.service_uid,
                self.service_gid,
            )
        self.assertFalse(self.manifest_path.exists())

    def test_completed_summary_loader_rejects_plan_before_execution(self):
        self.make_image()
        planned = self.migrator().run(execute=False)
        self.assertEqual(planned.image_count, 1)
        self.assertEqual(
            [event["event"] for event in self.manifest_events()],
            ["planned_skeleton", "plan_complete"],
        )

        with self.assertRaisesRegex(
            CommandError,
            "^auto_v2_plan_incomplete$",
        ):
            load_completed_auto_v2_summary(
                str(self.run_directory),
                MANIFEST_FILENAME,
                RUN_ID,
                self.service_uid,
                self.service_gid,
            )

    def test_incomplete_plan_prefix_is_reset_for_same_run_replanning(self):
        first = self.make_image()
        self.make_image()
        plan = self.make_v2_original_plan(first)[0]
        with AutoV2ManifestLog.open(
            str(self.run_directory),
            MANIFEST_FILENAME,
            RUN_ID,
            self.service_uid,
            self.service_gid,
        ) as manifest:
            manifest.record_plan(plan)

        self.assertEqual(len(self.manifest_events()), 1)
        self.assertTrue(recover_incomplete_auto_v2_plan(
            str(self.run_directory),
            MANIFEST_FILENAME,
            RUN_ID,
            self.service_uid,
            self.service_gid,
        ))
        self.assertEqual(self.manifest_path.read_bytes(), b"")

        summary = self.migrator().run(execute=False)

        self.assertEqual(summary.image_count, 2)

    def test_incomplete_torn_plan_prefix_is_quarantined_then_reset(self):
        image = self.make_image()
        plan = self.make_v2_original_plan(image)[0]
        with AutoV2ManifestLog.open(
            str(self.run_directory),
            MANIFEST_FILENAME,
            RUN_ID,
            self.service_uid,
            self.service_gid,
        ) as manifest:
            manifest.record_plan(plan)
        with self.manifest_path.open("ab") as manifest:
            manifest.write(b'{"format_version":2')

        self.assertTrue(recover_incomplete_auto_v2_plan(
            str(self.run_directory),
            MANIFEST_FILENAME,
            RUN_ID,
            self.service_uid,
            self.service_gid,
        ))

        self.assertEqual(self.manifest_path.read_bytes(), b"")
        self.assertEqual(
            len(list(self.run_directory.glob("media-migration.jsonl.torn-*"))),
            1,
        )

    def test_complete_plan_is_never_reset(self):
        self.make_image()
        self.migrator().run(execute=False)
        before = self.manifest_path.read_bytes()

        with self.assertRaisesRegex(
            CommandError,
            "^auto_v2_plan_reset_forbidden$",
        ):
            recover_incomplete_auto_v2_plan(
                str(self.run_directory),
                MANIFEST_FILENAME,
                RUN_ID,
                self.service_uid,
                self.service_gid,
            )

        self.assertEqual(self.manifest_path.read_bytes(), before)

    def test_completed_plan_torn_tail_can_be_repaired_without_execution(self):
        self.make_image()
        planned = self.migrator().run(execute=False)
        with self.manifest_path.open("ab") as manifest:
            manifest.write(b'{"event":"committed"')

        recovered = self.migrator().recover_execution_tail()

        self.assertEqual(recovered.plan_sha256, planned.plan_sha256)
        self.assertEqual(
            recovered.manifest_sha256,
            planned.manifest_sha256,
        )
        self.assertEqual(
            len(list(self.run_directory.glob(
                "media-migration.jsonl.torn-*"
            ))),
            1,
        )

    def test_archive_source_loader_requires_terminal_results_and_filters_fixed_originals(self):
        first = self.make_image(generation="fixed", sizes=())
        self.make_image(generation="named", sizes=())
        second = self.make_image(generation="fixed", sizes=())
        expected = (
            "originals/{}/original.png".format(first.asset_uuid),
            "originals/{}/original.png".format(second.asset_uuid),
        )
        self.migrator().run(execute=False)

        with self.assertRaisesRegex(CommandError, "^auto_v2_plan_incomplete$"):
            load_auto_v2_archive_sources(
                str(self.run_directory),
                MANIFEST_FILENAME,
                RUN_ID,
                self.service_uid,
                self.service_gid,
            )

        self.migrator().run(execute=True)
        completion = self.completed_authority()
        with mock.patch.object(
            AutoV2ManifestLog,
            "open",
            wraps=AutoV2ManifestLog.open,
        ) as strict_open:
            sources = load_auto_v2_archive_sources(
                str(self.run_directory),
                MANIFEST_FILENAME,
                RUN_ID,
                self.service_uid,
                self.service_gid,
                completion_authority=completion,
            )

        self.assertIsInstance(sources, tuple)
        self.assertEqual(sources, expected)
        self.assertEqual(strict_open.call_count, 0)

    def test_archive_direct_root_loader_reports_every_manifest_root(self):
        self.make_image(generation="pinry-md5")
        self.migrator().run(execute=True)
        completion = self.completed_authority()

        roots = load_auto_v2_archive_direct_roots(
            str(self.run_directory),
            MANIFEST_FILENAME,
            RUN_ID,
            self.service_uid,
            self.service_gid,
            completion_authority=completion,
        )

        self.assertEqual(roots, ("0", "a", "c", "e"))

    def test_archive_source_loader_rejects_a_live_canonical_original(self):
        asset_uuid = "11111111-1111-4111-8111-111111111111"
        fixed_path = "originals/{}/original.png".format(asset_uuid)
        source = self.write_media(fixed_path, make_image_bytes("red"))
        source_stat = source.stat()
        fixed_file = AutoV2MigrationFile(
            kind="original",
            old_path=fixed_path,
            new_path=canonical_original_path(
                asset_uuid, "renamed.png", ".png"
            ),
            operation="copy",
            size=source_stat.st_size,
            sha256="0" * 64,
            image_format="PNG",
            width=32,
            height=32,
            source_device=source_stat.st_dev,
            source_inode=source_stat.st_ino,
        )
        canonical_file = AutoV2MigrationFile(
            kind="original",
            old_path=fixed_path,
            new_path=fixed_path,
            operation="verify",
            size=source_stat.st_size,
            sha256="0" * 64,
            image_format="PNG",
            width=32,
            height=32,
            source_device=source_stat.st_dev,
            source_inode=source_stat.st_ino,
        )
        plans = (
            AutoV2MigrationPlan(
                image_id=101,
                asset_uuid=asset_uuid,
                original_filename="renamed.png",
                image_width=32,
                image_height=32,
                generation="fixed_slot",
                files=(fixed_file,),
                thumbnail_rows=(),
                copy_required_bytes=source_stat.st_size,
            ),
            AutoV2MigrationPlan(
                image_id=102,
                asset_uuid=asset_uuid,
                original_filename="original.png",
                image_width=32,
                image_height=32,
                generation="named_canonical",
                files=(canonical_file,),
                thumbnail_rows=(),
                copy_required_bytes=0,
            ),
        )
        with AutoV2ManifestLog.open(
            str(self.run_directory),
            MANIFEST_FILENAME,
            RUN_ID,
            self.service_uid,
            self.service_gid,
        ) as manifest:
            for plan in plans:
                manifest.record_plan(plan)
            manifest.record_plan_complete(plans)
            manifest.record_result("committed", 101)
            manifest.record_result("already_current", 102)

        with self.assertRaisesRegex(CommandError, "^manifest_plan_mismatch$"):
            load_auto_v2_archive_sources(
                str(self.run_directory),
                MANIFEST_FILENAME,
                RUN_ID,
                self.service_uid,
                self.service_gid,
            )

    def test_plan_freezes_reusable_destination_identity_and_absence(self):
        image = self.make_image(sizes=())
        original_path = Path(self.temporary_media.name, image.image.name)
        new_path = canonical_original_path(
            image.asset_uuid, image.original_filename, ".png"
        )
        self.migrator().run(execute=False)
        self.write_media(new_path, original_path.read_bytes())

        with self.assertRaisesRegex(CommandError, "media_path_conflict"):
            self.migrator().run(execute=True)

        image.refresh_from_db()
        self.assertNotEqual(image.image.name, new_path)

        self.manifest_path.unlink()
        Path(self.run_directory, JOURNAL_FILENAME).unlink()
        for staging in Path(
            self.temporary_media.name, ".staging"
        ).glob("auto-v2-*.part"):
            staging.unlink()
        destination_path = Path(self.temporary_media.name, new_path)
        self.migrator().run(execute=False)
        replacement_path = destination_path.with_name("replacement.png")
        replacement_path.write_bytes(destination_path.read_bytes())
        os.replace(str(replacement_path), str(destination_path))

        with self.assertRaisesRegex(CommandError, "media_path_conflict"):
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

    def test_manifest_append_updates_state_without_full_replay(self):
        image = self.make_image(sizes=())
        plan = self.make_v2_original_plan(image)[0]

        with AutoV2ManifestLog.open(
            str(self.run_directory),
            MANIFEST_FILENAME,
            RUN_ID,
            self.service_uid,
            self.service_gid,
        ) as manifest:
            with mock.patch.object(
                manifest,
                "_read_all",
                wraps=manifest._read_all,
            ) as read_all, mock.patch.object(
                manifest,
                "_load_state",
                wraps=manifest._load_state,
            ) as load_state:
                manifest.record_plan(plan)
                manifest.record_plan_complete((plan,))

            self.assertEqual(read_all.call_count, 4)
            self.assertEqual(load_state.call_count, 0)
            self.assertEqual(manifest.state.plans, (plan,))
            self.assertTrue(manifest.state.plan_complete)
            self.assertEqual(len(manifest.state.events), 2)

        with AutoV2ManifestLog.open(
            str(self.run_directory),
            MANIFEST_FILENAME,
            RUN_ID,
            self.service_uid,
            self.service_gid,
            create=False,
        ) as reopened:
            self.assertEqual(reopened.state.plans, (plan,))
            self.assertTrue(reopened.state.plan_complete)

    def test_incremental_manifest_state_matches_full_replay(self):
        image = self.make_image(sizes=())
        plan = self.make_v2_original_plan(image)[0]
        file_key = plan.files[0].kind_key
        staging_name = "auto-v2-{}.part".format(uuid.uuid4())

        with AutoV2ManifestLog.open(
            str(self.run_directory),
            MANIFEST_FILENAME,
            RUN_ID,
            self.service_uid,
            self.service_gid,
        ) as manifest:
            manifest.record_plan(plan)
            manifest.record_plan_complete((plan,))
            manifest.record_publish_intent(
                plan.image_id,
                file_key,
                staging_name,
                (17, 19),
                (23, 29),
            )
            manifest.record_published(
                plan.image_id,
                file_key,
                (17, 19),
            )
            manifest.record_result("committed", plan.image_id)
            incremental = self._manifest_state_snapshot(manifest.state)

        with AutoV2ManifestLog.open(
            str(self.run_directory),
            MANIFEST_FILENAME,
            RUN_ID,
            self.service_uid,
            self.service_gid,
            create=False,
        ) as reopened:
            replayed = self._manifest_state_snapshot(reopened.state)

        self.assertEqual(incremental, replayed)

    def test_manifest_state_is_read_only_between_appends(self):
        image = self.make_image(sizes=())
        plan = self.make_v2_original_plan(image)[0]

        with AutoV2ManifestLog.open(
            str(self.run_directory),
            MANIFEST_FILENAME,
            RUN_ID,
            self.service_uid,
            self.service_gid,
        ) as manifest:
            manifest.record_plan(plan)
            manifest.record_plan_complete((plan,))

            with self.assertRaisesRegex(
                AttributeError,
                "^manifest_state_is_read_only$",
            ):
                manifest.state.plan_end_offset = 0
            with self.assertRaises(TypeError):
                manifest.state.latest_by_image[plan.image_id] = "committed"

            manifest.record_result("committed", plan.image_id)

        with AutoV2ManifestLog.open(
            str(self.run_directory),
            MANIFEST_FILENAME,
            RUN_ID,
            self.service_uid,
            self.service_gid,
            create=False,
        ) as reopened:
            self.assertEqual(
                reopened.state.latest_by_image[plan.image_id],
                "committed",
            )

    @staticmethod
    def _manifest_state_snapshot(state):
        return {
            "events": state.events,
            "plans": state.plans,
            "plan_by_image": state.plan_by_image,
            "latest_by_image": state.latest_by_image,
            "publish_intents_by_image": state.publish_intents_by_image,
            "published_by_image": state.published_by_image,
            "plan_complete": state.plan_complete,
            "plan_end_offset": state.plan_end_offset,
            "torn_tail": state.torn_tail,
            "torn_offset": state.torn_offset,
            "raw_bytes": state.raw_bytes,
        }

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

    def test_execute_reports_copy_and_database_progress(self):
        self.make_image()
        progress = []
        migrator = AutoV2MediaMigrator(
            str(self.run_directory),
            MANIFEST_FILENAME,
            RUN_ID,
            self.service_uid,
            self.service_gid,
            progress_reporter=progress.append,
        )

        migrator.run(execute=True)

        self.assertEqual(
            progress,
            [
                {
                    "phase": "copying",
                    "images_done": 1,
                    "images_total": 1,
                    "files_done": 4,
                    "files_total": 4,
                },
                {
                    "phase": "database",
                    "images_done": 1,
                    "images_total": 1,
                },
                {"phase": "finalizing"},
            ],
        )

    def test_progress_reporter_failure_does_not_abort_migration(self):
        self.make_image()
        reporter = mock.Mock(side_effect=OSError("closed output"))
        migrator = AutoV2MediaMigrator(
            str(self.run_directory),
            MANIFEST_FILENAME,
            RUN_ID,
            self.service_uid,
            self.service_gid,
            progress_reporter=reporter,
        )

        summary = migrator.run(execute=True)

        self.assertEqual(summary.image_count, 1)
        self.assertGreaterEqual(reporter.call_count, 3)

    def test_empty_migration_reports_phases_in_order(self):
        progress = []
        migrator = AutoV2MediaMigrator(
            str(self.run_directory),
            MANIFEST_FILENAME,
            RUN_ID,
            self.service_uid,
            self.service_gid,
            progress_reporter=progress.append,
        )

        summary = migrator.run(execute=True)

        self.assertEqual(summary.image_count, 0)
        self.assertEqual(
            progress,
            [{"phase": "finalizing"}],
        )

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
        self.assertEqual(len(media_root_calls), 3)

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

    def test_source_name_swap_after_stream_is_rejected_before_intent(self):
        image = self.make_image(sizes=())
        old_path = image.image.name
        source = Path(self.temporary_media.name, old_path)
        swapped = []

        def replace_source_name(point):
            if point != "after_source_revalidation" or swapped:
                return
            replacement = source.with_name("replacement.png")
            replacement.write_bytes(make_image_bytes("blue"))
            os.replace(str(replacement), str(source))
            swapped.append(True)

        with self.assertRaisesRegex(
            CommandError, "^media_verification_failed$"
        ):
            self.migrator(
                fault_injector=replace_source_name
            ).run(execute=True)

        self.assertEqual(len(swapped), 1)
        image.refresh_from_db()
        self.assertEqual(image.image.name, old_path)
        self.assertFalse(any(
            event["event"] == "batch_intent"
            for event in self.journal_events()
        ))

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
        self.assertNotEqual(planned.image.name, old_path)
        self.assertTrue(Image.objects.filter(pk=added[0]).exists())
        self.assertFalse(any(
            event["event"] == "phase_complete"
            for event in self.journal_events()
        ))

    def test_publish_and_database_commit_crashes_resume_same_plan(self):
        for crash_point in ("after_publish", "after_database_commit"):
            with self.subTest(crash_point=crash_point):
                Image.objects.all().delete()
                if self.manifest_path.exists():
                    self.manifest_path.unlink()
                journal_path = Path(
                    self.run_directory, JOURNAL_FILENAME
                )
                if journal_path.exists():
                    journal_path.unlink()
                checkpoint = Path(
                    self.run_directory, "migration-checkpoint.json"
                )
                if checkpoint.exists():
                    checkpoint.unlink()
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
                events = self.journal_events()
                self.assertEqual(
                    sum(event["event"] == "batch_commit"
                        for event in events),
                    1,
                )
                self.assertEqual(
                    [event["event"] for event in self.manifest_events()],
                    ["planned_skeleton", "plan_complete"],
                )

    def test_publish_before_published_event_crash_resumes_from_intent(self):
        image = self.make_image(sizes=())

        def crash(point):
            if point == "after_publish_before_event":
                raise SimulatedProcessCrash()

        with self.assertRaises(SimulatedProcessCrash):
            self.migrator(fault_injector=crash).run(execute=True)

        events = self.journal_events()
        self.assertNotIn(
            "batch_intent", {event["event"] for event in events}
        )
        destination_relative = canonical_original_path(
            image.asset_uuid, image.original_filename, ".png"
        )
        destination = Path(
            self.temporary_media.name, destination_relative
        )
        self.assertEqual(os.stat(str(destination)).st_nlink, 1)

        self.migrator().run(execute=True)

        image.refresh_from_db()
        self.assertEqual(
            image.image.name,
            canonical_original_path(
                image.asset_uuid, image.original_filename, ".png"
            ),
        )
        self.assertEqual(
            sum(
                event["event"] == "batch_intent"
                for event in self.journal_events()
            ),
            1,
        )

    def test_publish_intent_before_atomic_publish_resumes_from_staging(self):
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
        self.assertTrue(destination.exists())
        intent = next(
            event["intent"]
            for event in self.journal_events()
            if event["event"] == "batch_intent"
        )
        receipt = intent["receipts"][0]
        destination_stat = os.stat(str(destination))
        self.assertEqual(
            (destination_stat.st_dev, destination_stat.st_ino),
            (
                receipt["destination_device"],
                receipt["destination_inode"],
            ),
        )
        self.assertEqual(destination_stat.st_nlink, 1)
        self.assertEqual(destination_stat.st_uid, self.service_uid)
        self.assertEqual(stat.S_IMODE(destination_stat.st_mode), 0o600)

        self.migrator().run(execute=True)

        image.refresh_from_db()
        self.assertEqual(image.image.name, destination_relative)
        self.assertEqual(os.stat(str(destination)).st_nlink, 1)
        self.assertIn(
            "phase_complete",
            {event["event"] for event in self.journal_events()},
        )

    def test_new_staging_uses_service_identity_before_publish_intent(self):
        image = self.make_image(sizes=())
        service_gid = self.alternate_service_gid()
        os.chown(
            str(self.run_directory), self.service_uid, service_gid
        )

        def crash(point):
            if point == "after_publish_intent":
                raise SimulatedProcessCrash()

        migrator = AutoV2MediaMigrator(
            str(self.run_directory),
            MANIFEST_FILENAME,
            RUN_ID,
            self.service_uid,
            service_gid,
            fault_injector=crash,
        )
        with self.assertRaises(SimulatedProcessCrash):
            migrator.run(execute=True)

        image.refresh_from_db()
        destination = Path(
            self.temporary_media.name,
            canonical_original_path(
                image.asset_uuid, image.original_filename, ".png"
            ),
        )
        staging_stat = os.stat(str(destination))
        self.assertEqual(
            (staging_stat.st_uid, staging_stat.st_gid),
            (self.service_uid, service_gid),
        )
        self.assertEqual(stat.S_IMODE(staging_stat.st_mode), 0o600)
        image.refresh_from_db()
        self.assertNotEqual(
            image.image.name,
            canonical_original_path(
                image.asset_uuid, image.original_filename, ".png"
            ),
        )

    def test_resume_does_not_reown_published_destination(self):
        image = self.make_image(sizes=())

        def crash(point):
            if point == "after_publish_intent":
                raise SimulatedProcessCrash()

        with self.assertRaises(SimulatedProcessCrash):
            self.migrator(fault_injector=crash).run(execute=True)

        destination = Path(
            self.temporary_media.name,
            canonical_original_path(
                image.asset_uuid, image.original_filename, ".png"
            ),
        )
        original_group = os.stat(str(destination)).st_gid
        service_gid = self.alternate_service_gid()
        self.assertNotEqual(original_group, service_gid)
        os.chown(str(destination), self.service_uid, service_gid)

        self.migrator().run(execute=True)

        destination_stat = os.stat(str(destination))
        self.assertEqual(destination_stat.st_uid, self.service_uid)
        self.assertEqual(destination_stat.st_gid, service_gid)
        image.refresh_from_db()
        self.assertEqual(
            image.image.name,
            canonical_original_path(
                image.asset_uuid, image.original_filename, ".png"
            ),
        )
        events = {event["event"] for event in self.journal_events()}
        self.assertIn("batch_commit", events)

    def test_resume_does_not_reown_root_with_service_group(self):
        migrator = self.migrator()
        migrator.service_uid = 1000
        migrator.service_gid = 1000
        root_service_stat = mock.Mock(
            st_uid=0,
            st_gid=migrator.service_gid,
            st_mode=stat.S_IFREG | 0o600,
        )
        file_plan = mock.Mock(size=4)

        with mock.patch.object(
            migrator,
            "_verify_named_staging_object",
            return_value=(root_service_stat, root_service_stat),
        ), mock.patch(
            "django_images.services.media_migration_v2.os.geteuid",
            return_value=0,
        ), mock.patch(
            "django_images.services.media_migration_v2.os.fchown"
        ) as reown, mock.patch(
            "django_images.services.media_migration_v2.os.fchmod"
        ), mock.patch(
            "django_images.services.media_migration_v2.os.fsync"
        ), mock.patch(
            "django_images.services.media_migration_v2._verify_staging"
        ):
            with self.assertRaisesRegex(
                CommandError, "^media_verification_failed$"
            ):
                migrator._recover_root_staging_service_identity(
                    mock.Mock(),
                    "auto-v2-test.part",
                    123,
                    (1, 2, 3, 4, "auto-v2-test.part"),
                    file_plan,
                    1,
                    mock.Mock(),
                    "original.png",
                )

        reown.assert_not_called()

    def test_resume_repairs_destination_before_database_commit(self):
        image = self.make_image(sizes=())
        source = Path(self.temporary_media.name, image.image.name)
        expected = source.read_bytes()

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
        migrator = self.migrator()
        replacement = destination.with_name("external.png")
        replacement.write_bytes(b"external destination")
        os.replace(str(replacement), str(destination))

        migrator.run(execute=True)

        self.assertEqual(destination.read_bytes(), expected)
        image.refresh_from_db()
        self.assertEqual(image.image.name, destination_relative)

    def _leave_pre_intent_staging(self):
        if os.geteuid() == 0:
            self.use_distinct_root_service_identity()
        image = self.make_image(sizes=())

        def crash(point):
            if point == "after_staging_sync":
                raise SimulatedProcessCrash()

        with self.assertRaises(SimulatedProcessCrash):
            self.migrator(fault_injector=crash).run(execute=True)

        self.assertEqual(
            [event["event"] for event in self.manifest_events()],
            ["planned_skeleton", "plan_complete"],
        )
        self.assertFalse(any(
            event["event"] == "batch_intent"
            for event in self.journal_events()
        ))
        staging = Path(
            self.temporary_media.name,
            ".staging",
            self.migrator()._staging_name(
                "original:{}".format(image.pk)
            ),
        )
        self.assertTrue(staging.exists())
        return image, staging

    def test_resume_reowns_manifest_bound_root_staging_for_service(self):
        image, staging = self._leave_pre_intent_staging()
        staging_stat = os.stat(str(staging))
        self.assertEqual(
            (staging_stat.st_uid, staging_stat.st_gid),
            (self.service_uid, self.service_gid),
        )
        self.assertEqual(stat.S_IMODE(staging_stat.st_mode), 0o600)

        migrator = self.migrator()
        with mock.patch.object(
            migrator,
            "_iter_source_chunks",
            wraps=migrator._iter_source_chunks,
        ) as source_chunks:
            migrator.run(execute=True)

        self.assertEqual(source_chunks.call_count, 0)
        destination_relative = canonical_original_path(
            image.asset_uuid, image.original_filename, ".png"
        )
        destination = Path(
            self.temporary_media.name, destination_relative
        )
        destination_stat = os.stat(str(destination))
        self.assertEqual(
            (destination_stat.st_uid, destination_stat.st_gid),
            (self.service_uid, self.service_gid),
        )
        self.assertEqual(stat.S_IMODE(destination_stat.st_mode), 0o600)
        self.assertFalse(staging.exists())
        image.refresh_from_db()
        self.assertEqual(image.image.name, destination_relative)
        self.assertTrue(any(
            event["event"] == "batch_commit"
            for event in self.journal_events()
        ))

    def test_resume_does_not_reown_tampered_root_staging(self):
        image, staging = self._leave_pre_intent_staging()
        before_identity = os.stat(str(staging))
        content = bytearray(staging.read_bytes())
        content[0] ^= 1
        staging.write_bytes(content)

        with self.assertRaisesRegex(
            CommandError, "^media_verification_failed$"
        ):
            self.migrator().run(execute=True)

        staging_stat = os.stat(str(staging))
        self.assertEqual(
            (staging_stat.st_dev, staging_stat.st_ino),
            (before_identity.st_dev, before_identity.st_ino),
        )
        self.assertEqual(
            (staging_stat.st_uid, staging_stat.st_gid),
            (self.service_uid, self.service_gid),
        )
        image.refresh_from_db()
        self.assertNotEqual(
            image.image.name,
            canonical_original_path(
                image.asset_uuid, image.original_filename, ".png"
            ),
        )
        events = {event["event"] for event in self.journal_events()}
        self.assertNotIn("batch_intent", events)
        self.assertNotIn("batch_commit", events)

    def test_initial_publish_rejects_swap_in_exact_detach_primitive_window(self):
        image = self.make_image(sizes=())
        old_path = image.image.name
        destination_relative = canonical_original_path(
            image.asset_uuid, image.original_filename, ".png"
        )
        fault, swapped, replacement_bytes = (
            self.primitive_window_staging_swap()
        )

        with self.assertRaisesRegex(
            CommandError, "media_verification_failed"
        ):
            self.migrator(fault_injector=fault).run(execute=True)

        self.assertEqual(len(swapped), 1)
        staging = Path(self.temporary_media.name, ".staging", swapped[0])
        destination = Path(self.temporary_media.name, destination_relative)
        self.assertEqual(staging.read_bytes(), replacement_bytes)
        self.assertFalse(destination.exists())
        image.refresh_from_db()
        self.assertEqual(image.image.name, old_path)
        events = {event["event"] for event in self.journal_events()}
        self.assertNotIn("batch_intent", events)

    def test_resume_rejects_changed_preintent_orphan(self):
        image = self.make_image(sizes=())
        old_path = image.image.name

        def crash_after_publish(point):
            if point == "after_publish":
                raise SimulatedProcessCrash()

        with self.assertRaises(SimulatedProcessCrash):
            self.migrator(
                fault_injector=crash_after_publish
            ).run(execute=True)

        destination_relative = canonical_original_path(
            image.asset_uuid, image.original_filename, ".png"
        )
        destination = Path(self.temporary_media.name, destination_relative)
        replacement_bytes = b"changed durable orphan"
        destination.write_bytes(replacement_bytes)

        with self.assertRaisesRegex(
            CommandError, "media_path_conflict"
        ):
            self.migrator().run(execute=True)

        self.assertEqual(destination.read_bytes(), replacement_bytes)
        image.refresh_from_db()
        self.assertEqual(image.image.name, old_path)
        events = {event["event"] for event in self.journal_events()}
        self.assertNotIn("batch_intent", events)

    def test_initial_publish_does_not_reverse_external_destination_replacement(self):
        image = self.make_image(sizes=())
        old_path = image.image.name
        destination_relative = canonical_original_path(
            image.asset_uuid, image.original_filename, ".png"
        )
        destination = Path(self.temporary_media.name, destination_relative)
        rename, replacement_identity, replacement_bytes = (
            self.destination_replacement_after_atomic_publish()
        )

        with mock.patch(
            "django_images.file_ops."
            "rename_media_noreplace",
            side_effect=rename,
        ):
            with self.assertRaisesRegex(
                CommandError, "^media_verification_failed$"
            ):
                self.migrator().run(execute=True)

        self.assertTrue(destination.exists())
        self.assertEqual(destination.read_bytes(), replacement_bytes)
        destination_stat = os.stat(str(destination))
        self.assertEqual(
            (destination_stat.st_dev, destination_stat.st_ino),
            replacement_identity[0],
        )
        image.refresh_from_db()
        self.assertEqual(image.image.name, old_path)
        events = {event["event"] for event in self.journal_events()}
        self.assertNotIn("batch_intent", events)

        with self.assertRaisesRegex(CommandError, "^media_path_conflict$"):
            self.migrator().run(execute=True)
        self.assertEqual(destination.read_bytes(), replacement_bytes)
        destination_stat = os.stat(str(destination))
        self.assertEqual(
            (destination_stat.st_dev, destination_stat.st_ino),
            replacement_identity[0],
        )

    def test_resume_repairs_external_destination_replacement(self):
        image = self.make_image(sizes=())
        source = Path(self.temporary_media.name, image.image.name)
        expected = source.read_bytes()
        destination_relative = canonical_original_path(
            image.asset_uuid, image.original_filename, ".png"
        )
        destination = Path(self.temporary_media.name, destination_relative)

        def crash_after_intent(point):
            if point == "after_publish_intent":
                raise SimulatedProcessCrash()

        with self.assertRaises(SimulatedProcessCrash):
            self.migrator(
                fault_injector=crash_after_intent
            ).run(execute=True)

        replacement = destination.with_name("replacement.png")
        replacement.write_bytes(b"external replacement")
        os.replace(str(replacement), str(destination))

        self.migrator().run(execute=True)

        self.assertEqual(destination.read_bytes(), expected)
        image.refresh_from_db()
        self.assertEqual(image.image.name, destination_relative)
        self.assertEqual(
            sum(
                event["event"] == "batch_repair"
                for event in self.journal_events()
            ),
            1,
        )

    def test_initial_publish_fails_closed_without_atomic_rename_support(self):
        image = self.make_image(sizes=())
        old_path = image.image.name
        destination_relative = canonical_original_path(
            image.asset_uuid, image.original_filename, ".png"
        )

        with mock.patch(
            "django_images.file_ops."
            "rename_media_noreplace",
            side_effect=MediaPathError("atomic_rename_unsupported"),
        ):
            with self.assertRaisesRegex(
                CommandError, "media_verification_failed"
            ):
                self.migrator().run(execute=True)

        image.refresh_from_db()
        self.assertEqual(image.image.name, old_path)
        self.assertFalse(
            Path(self.temporary_media.name, destination_relative).exists()
        )
        events = {event["event"] for event in self.manifest_events()}
        self.assertNotIn("published", events)
        self.assertNotIn("committed", events)

    def test_initial_publish_fails_closed_across_filesystems(self):
        image = self.make_image(sizes=())
        old_path = image.image.name
        destination_relative = canonical_original_path(
            image.asset_uuid, image.original_filename, ".png"
        )

        with mock.patch(
            "django_images.file_ops."
            "rename_media_noreplace",
            side_effect=OSError(errno.EXDEV, os.strerror(errno.EXDEV)),
        ):
            with self.assertRaisesRegex(
                CommandError, "media_verification_failed"
            ):
                self.migrator().run(execute=True)

        image.refresh_from_db()
        self.assertEqual(image.image.name, old_path)
        self.assertFalse(
            Path(self.temporary_media.name, destination_relative).exists()
        )
        events = {event["event"] for event in self.manifest_events()}
        self.assertNotIn("published", events)
        self.assertNotIn("committed", events)

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
            replacement = destination.with_name("external.png")
            replacement.write_bytes(source.read_bytes())
            os.replace(str(replacement), str(destination))

        with self.assertRaisesRegex(
            CommandError, "media_verification_failed"
        ):
            self.migrator(
                fault_injector=create_external_destination
            ).run(execute=True)

        image.refresh_from_db()
        self.assertNotEqual(image.image.name, destination_relative)

    def test_recovered_commit_repairs_destination_identity_swap(self):
        image = self.make_image(sizes=())
        source = Path(self.temporary_media.name, image.image.name)

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

        self.migrator().run(execute=True)

        self.assertEqual(destination.read_bytes(), source.read_bytes())
        self.assertEqual(
            sum(
                event["event"] == "batch_repair"
                for event in self.journal_events()
            ),
            1,
        )

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
