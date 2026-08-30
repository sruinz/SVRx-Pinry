from contextlib import contextmanager
from io import BytesIO, StringIO
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import threading
import time
from types import SimpleNamespace
import uuid

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import (
    close_old_connections,
    connection,
    connections,
    OperationalError,
    transaction,
)
from django.db.models.signals import post_save
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
import mock
from PIL import Image as PILImage
from PIL import ImageFile

from core import models as core_models
from core.models import Board, MediaAsset, Pin
from core.services import database_fence
from core.services import media_asset_backfill
from core.services.database_fence import (
    DatabaseFenceBusy,
    DatabaseFenceDeadline,
    DatabaseFenceError,
    database_write_fence,
)
from core.services.bulk_pin_management import BulkPinManagementService
from core.services.idempotency import IdempotencyStore
from core.services.media_asset_backfill import (
    BackfillSummary,
    MediaAssetBackfiller,
    SAFE_BACKFILL_REASON_CODES,
    load_completed_media_asset_backfill_summary,
    load_media_asset_plan,
    recover_incomplete_media_asset_plan,
)
from core.services.media_storage import MediaStorage
from core.services.pin_import import ImportMetadata, PinImportService
from core.services.safe_url_fetch import FetchedImage
from django_images import file_ops
from django_images.models import Image, Thumbnail
from django_images.services.migration_batch_log import (
    BatchIntent,
    BatchLimits,
    FileReceipt,
    JOURNAL_FILENAME,
    MigrationBatchJournal,
)
from django_images.test_helpers import TemporaryMediaMixin
from users.models import User


MANIFEST_FILENAME = "media-asset-backfill.jsonl"


def _png_bytes(color="red", size=(640, 480), compress_level=6):
    output = BytesIO()
    image = PILImage.new("RGB", size, color)
    try:
        image.save(
            output,
            format="PNG",
            compress_level=compress_level,
        )
    finally:
        image.close()
    return output.getvalue()


def _fetched(content=None):
    content = _png_bytes() if content is None else content
    with PILImage.open(BytesIO(content)) as image:
        image.load()
        return FetchedImage(
            content=content,
            image_format=image.format,
            width=image.width,
            height=image.height,
            final_url="https://fixture.invalid/media.png",
        )


def _visible_media_snapshot(media_root):
    root = Path(media_root)
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
        and ".pinry-locks" not in path.relative_to(root).parts
        and ".staging" not in path.relative_to(root).parts
    }


class MediaAssetBackfillTests(TemporaryMediaMixin, TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        super(MediaAssetBackfillTests, self).setUp()
        self.strict_media = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(self.strict_media.cleanup)
        self.temporary_media = self.strict_media
        self.strict_media_override = override_settings(
            MEDIA_ROOT=self.temporary_media.name,
        )
        self.strict_media_override.enable()
        self.addCleanup(self.strict_media_override.disable)
        self.data_directory = tempfile.TemporaryDirectory(
            dir="/private/tmp"
        )
        self.addCleanup(self.data_directory.cleanup)
        self.data_override = override_settings(
            PINRY_DATA_ROOT=self.data_directory.name,
        )
        self.data_override.enable()
        self.addCleanup(self.data_override.disable)
        self.run_number = 0
        self.owner = User.objects.create_user(
            username="backfill-owner",
            email="backfill-owner@example.invalid",
        )
        self.other_owner = User.objects.create_user(
            username="backfill-other-owner",
            email="backfill-other-owner@example.invalid",
        )

    def _new_run(self):
        self.run_number += 1
        run_id = str(uuid.UUID(int=self.run_number))
        run_directory = Path(self.data_directory.name, run_id)
        run_directory.mkdir(mode=0o700)
        os.chmod(str(run_directory), 0o700)
        os.chown(str(run_directory), os.geteuid(), os.getegid())
        return run_id, run_directory

    def _service(
        self,
        run_id=None,
        run_directory=None,
        fault_injector=None,
        storage=None,
        batch_size=100,
        **kwargs
    ):
        if run_id is None:
            run_id, run_directory = self._new_run()
        return MediaAssetBackfiller(
            str(run_directory),
            MANIFEST_FILENAME,
            run_id,
            os.geteuid(),
            os.getegid(),
            batch_size=batch_size,
            media_storage=storage,
            fault_injector=fault_injector,
            **kwargs
        )

    def _create_bulk_candidates(self, count):
        images = []
        for index in range(count):
            asset_uuid = uuid.UUID(int=index + 1000)
            images.append(Image(
                image="originals/{}/legacy.png".format(asset_uuid),
                asset_uuid=asset_uuid,
                original_filename="legacy.png",
                width=10,
                height=10,
            ))
        Image.objects.bulk_create(images)
        images = list(Image.objects.order_by("pk"))
        thumbnails = []
        pins = []
        for image in images:
            for size in ("thumbnail", "standard", "square"):
                thumbnails.append(Thumbnail(
                    original=image,
                    size=size,
                    image="derivatives/{}/{}.png".format(
                        image.asset_uuid, size
                    ),
                    width=10,
                    height=10,
                ))
            pins.append(Pin(submitter=self.owner, image=image))
        Thumbnail.objects.bulk_create(thumbnails)
        Pin.objects.bulk_create(pins)
        return images

    @staticmethod
    def _fake_candidate_resources(
        image,
        thumbnails,
        submitter_id,
        root_directory,
        database_signature,
        phase,
        expected_receipts=None,
    ):
        del thumbnails, root_directory, phase, expected_receipts

        class Resources(object):
            def __init__(self):
                self._closed = False
                self.closure = media_asset_backfill.VerifiedClosure(
                    image_id=image.pk,
                    submitter_id=submitter_id,
                    content_sha256="{:064x}".format(image.pk),
                    database_signature=database_signature,
                    file_identities=tuple(
                        (kind, image.image.name)
                        for kind in (
                            "original", "thumbnail", "standard", "square"
                        )
                    ),
                )

            def close(self):
                self._closed = True

        return Resources()

    @staticmethod
    def _journal_without_paths_complete():
        journal = object.__new__(MigrationBatchJournal)
        state = type("JournalState", (), {})()
        state.phase_summaries = {}
        journal.state = state
        return journal

    def _completed_path_journal(
        self,
        service,
        backfill_total=None,
        coordinator_attempt=True,
    ):
        images = list(Image.objects.order_by("pk"))
        receipts = []
        for image in images:
            rows = [("original", None, image)]
            rows.extend(
                (thumbnail.size, thumbnail, thumbnail)
                for thumbnail in Thumbnail.objects.filter(
                    original=image
                ).order_by("size", "pk")
            )
            for kind, thumbnail, row in rows:
                target = Path(self.temporary_media.name, row.image.name)
                file_stat = target.stat()
                content = target.read_bytes()
                receipts.append(FileReceipt.for_values(
                    file_key=(
                        "original:{}".format(image.pk)
                        if thumbnail is None
                        else "thumbnail:{}:{}".format(
                            image.pk, thumbnail.pk
                        )
                    ),
                    relative_path=row.image.name,
                    operation="verify",
                    size=file_stat.st_size,
                    image_format="PNG",
                    width=row.width,
                    height=row.height,
                    source_device=file_stat.st_dev,
                    source_inode=file_stat.st_ino,
                    destination_device=file_stat.st_dev,
                    destination_inode=file_stat.st_ino,
                    sha256=hashlib.sha256(content).hexdigest(),
                    database_signature="b" * 64,
                ))
        run_directory = file_ops.open_verified_media_root(
            service.run_directory
        )
        self.addCleanup(run_directory.close)
        journal = MigrationBatchJournal.open(
            run_directory,
            JOURNAL_FILENAME,
            service.run_id,
            service.service_uid,
            service.service_gid,
            "1" * 64,
            "2" * 64,
        )
        self.addCleanup(journal.close)
        if coordinator_attempt:
            journal.record_attempt("2026-08-27T00:00:00Z")
        journal.freeze_work_totals(
            len(images),
            len(receipts),
            len(images) if backfill_total is None else backfill_total,
        )
        if images:
            journal.append_intent(BatchIntent.for_values(
                batch_id="paths:{}-{}".format(
                    images[0].pk, images[-1].pk
                ),
                batch_number=1,
                phase="paths",
                first_pk=images[0].pk,
                last_pk=images[-1].pk,
                receipts=tuple(receipts),
                pre_signature="a" * 64,
                post_signature="b" * 64,
                images=len(images),
                files=len(receipts),
                total_bytes=sum(receipt.size for receipt in receipts),
                total_pixels=sum(
                    receipt.width * receipt.height for receipt in receipts
                ),
            ))
            journal.append_commit(
                "paths:{}-{}".format(images[0].pk, images[-1].pk),
                "b" * 64,
            )
        journal.append_phase_complete("paths", {
            "image_count": len(images),
            "md5_legacy": 0,
            "fixed_slot": 0,
            "named_canonical": len(images),
            "copy_required_bytes": 0,
        })
        return journal

    def test_backfill_refuses_to_start_before_paths_complete(self):
        journal = self._journal_without_paths_complete()

        for execute in (False, True):
            with self.subTest(execute=execute), self.assertRaisesRegex(
                CommandError, "^paths_not_complete$"
            ):
                self._service(batch_journal=journal).run(execute=execute)

    def test_plan_uses_keyset_and_bulk_queries(self):
        self._create_bulk_candidates(120)
        service = self._service(batch_size=50)

        with mock.patch.object(
            service,
            "_open_candidate_resources",
            side_effect=self._fake_candidate_resources,
        ), CaptureQueriesContext(connection) as queries:
            service.run(execute=False)

        self.assertLessEqual(len(queries), 18)
        self.assertFalse(any(
            " OFFSET " in query["sql"].upper() for query in queries
        ))

    def test_registry_rows_materialized_once_across_plan_batches(self):
        images = self._create_bulk_candidates(120)
        MediaAsset.objects.bulk_create([
            MediaAsset(
                image=image,
                submitter=self.owner,
                content_sha256="{:064x}".format(image.pk),
            )
            for image in images
        ])
        service = self._service(batch_size=50)

        with mock.patch.object(
            service,
            "_open_candidate_resources",
            side_effect=self._fake_candidate_resources,
        ), CaptureQueriesContext(connection) as queries:
            service.run(execute=False)

        table_name = MediaAsset._meta.db_table
        registry_queries = [
            query["sql"].rstrip(";")
            for query in queries
            if table_name in query["sql"]
            and query["sql"].lstrip().upper().startswith("SELECT")
        ]
        row_counts = []
        with connection.cursor() as cursor:
            for sql in registry_queries:
                cursor.execute(
                    "SELECT COUNT(*) FROM ({}) registry_rows".format(sql)
                )
                row_counts.append(cursor.fetchone()[0])

        self.assertEqual(row_counts, [50, 50, 20])

    def test_count_planned_images_opens_no_media_and_matches_frozen_plan(self):
        self._create_bulk_candidates(5)
        service = self._service(batch_size=2)

        with mock.patch.object(
            service,
            "_open_candidate_resources",
            side_effect=AssertionError("count opened candidate media"),
        ):
            estimated = service.count_planned_images()
        with mock.patch.object(
            service,
            "_open_candidate_resources",
            side_effect=self._fake_candidate_resources,
        ):
            planned = service.run(execute=False)

        self.assertEqual(estimated, planned.scanned)

    def test_backfill_plan_uses_one_group_fsync(self):
        self._create_bulk_candidates(3)
        service = self._service(batch_size=2)

        with mock.patch.object(
            service,
            "_open_candidate_resources",
            side_effect=self._fake_candidate_resources,
        ), mock.patch(
            "core.services.media_asset_backfill._durable_fsync"
        ) as durable_fsync:
            service.run(execute=False)

        self.assertEqual(
            [call.args[1] for call in durable_fsync.call_args_list],
            ["plan_manifest"],
        )

    def test_frozen_plan_streams_without_rematerializing_or_replaying(self):
        self._create_bulk_candidates(2)
        service = self._service(batch_size=2)
        with mock.patch.object(
            service,
            "_open_candidate_resources",
            side_effect=self._fake_candidate_resources,
        ):
            plans = list(service._freeze_all_plans("plan"))
        iterated_before_write = {"value": False}
        write_count = {"value": 0}

        def stream():
            yield plans[0]
            if write_count["value"] != 1:
                iterated_before_write["value"] = True
            yield plans[1]

        with media_asset_backfill._BackfillManifestLog.open(
            service.run_directory,
            MANIFEST_FILENAME,
            service.run_id,
            service.service_uid,
            service.service_gid,
        ) as manifest:
            real_write = manifest._write_frozen_line
            real_read = manifest._read_all
            replayed = {"value": False}

            def write_line(line):
                real_write(line)
                write_count["value"] += 1

            def read_all():
                replayed["value"] = True
                return real_read()

            with mock.patch.object(
                manifest,
                "_write_frozen_line",
                side_effect=write_line,
            ), mock.patch.object(
                manifest,
                "_read_all",
                side_effect=read_all,
            ):
                digest = manifest.write_frozen_plan(stream())

            self.assertFalse(iterated_before_write["value"])
            self.assertFalse(replayed["value"])
            self.assertEqual(digest, manifest.plan_sha256())
            self.assertEqual(len(manifest.state.plans), 2)

    def test_fresh_run_streams_freeze_into_manifest_without_plan_copy(self):
        self._create_bulk_candidates(3)
        service = self._service(batch_size=1)
        frozen = {"count": 0}
        observed = {}
        real_freeze = service._freeze_plan
        real_write_plan = (
            media_asset_backfill._BackfillManifestLog.write_frozen_plan
        )
        real_write_line = (
            media_asset_backfill._BackfillManifestLog._write_frozen_line
        )
        real_decisions = service._decisions

        def freeze(*args, **kwargs):
            frozen["count"] += 1
            return real_freeze(*args, **kwargs)

        def write_plan(manifest, plans):
            observed["materialized_input"] = isinstance(
                plans, (list, tuple)
            )
            result = real_write_plan(manifest, plans)
            observed["state_plans"] = manifest.state.plans
            return result

        def write_line(manifest, line):
            if "first_write_frozen" not in observed:
                observed["first_write_frozen"] = frozen["count"]
            return real_write_line(manifest, line)

        def decisions(plans):
            observed["reused_state_plans"] = (
                plans is observed["state_plans"]
            )
            return real_decisions(plans)

        with mock.patch.object(
            service,
            "_open_candidate_resources",
            side_effect=self._fake_candidate_resources,
        ), mock.patch.object(
            service,
            "_freeze_plan",
            side_effect=freeze,
        ), mock.patch.object(
            media_asset_backfill._BackfillManifestLog,
            "write_frozen_plan",
            autospec=True,
            side_effect=write_plan,
        ), mock.patch.object(
            media_asset_backfill._BackfillManifestLog,
            "_write_frozen_line",
            autospec=True,
            side_effect=write_line,
        ), mock.patch.object(
            service,
            "_decisions",
            side_effect=decisions,
        ):
            summary = service.run(execute=False)

        self.assertEqual(summary.scanned, 3)
        self.assertFalse(observed["materialized_input"])
        self.assertEqual(observed["first_write_frozen"], 1)
        self.assertTrue(observed["reused_state_plans"])

    def test_receipt_backfill_never_reopens_or_decodes_media(self):
        self._create_candidate()
        service = self._service(batch_size=1)
        journal = self._completed_path_journal(service)
        service.batch_journal = journal

        with mock.patch.object(
            MediaStorage,
            "prepare_from_root",
            side_effect=AssertionError("receipt path reopened media"),
        ), mock.patch(
            "core.services.media_asset_backfill.PILImage.open",
            side_effect=AssertionError("receipt path decoded media"),
        ):
            summary = service.run(execute=True)

        self.assertEqual(summary.registered, 1)
        self.assertEqual(MediaAsset.objects.count(), 1)

    def test_missing_path_receipt_fails_before_any_media_read(self):
        self._create_candidate(content=_png_bytes("red", size=(30, 20)))
        service = self._service(batch_size=1)
        service.batch_journal = self._completed_path_journal(service)
        self._create_candidate(content=_png_bytes("blue", size=(30, 20)))
        real_open = media_asset_backfill.open_verified_media_file
        real_hash = media_asset_backfill.sha256_file_descriptor
        real_decode = PILImage.open

        with mock.patch(
            "core.services.media_asset_backfill.open_verified_media_file",
            wraps=real_open,
        ) as media_open, mock.patch(
            "core.services.media_asset_backfill.sha256_file_descriptor",
            wraps=real_hash,
        ) as media_hash, mock.patch(
            "core.services.media_asset_backfill.PILImage.open",
            wraps=real_decode,
        ) as media_decode, self.assertRaisesRegex(
            CommandError, "^linear_journal_batch_conflict$"
        ):
            service.run(execute=True)

        self.assertEqual(media_open.call_count, 0)
        self.assertEqual(media_hash.call_count, 0)
        self.assertEqual(media_decode.call_count, 0)

    def test_receipt_stat_mismatch_aborts_instead_of_recording_skip(self):
        candidate = self._create_candidate(
            content=_png_bytes("red", size=(30, 20))
        )
        service = self._service(batch_size=1)
        service.batch_journal = self._completed_path_journal(service)
        original = Path(
            self.temporary_media.name,
            candidate["image"].image.name,
        )
        replacement = original.with_name("replacement.png")
        replacement.write_bytes(b"X" * original.stat().st_size)
        os.replace(str(replacement), str(original))

        with self.assertRaisesRegex(
            CommandError, "^registry_plan_identity_changed$"
        ):
            service.run(execute=True)

        self.assertEqual(MediaAsset.objects.count(), 0)
        self.assertFalse(
            service.batch_journal.is_phase_complete("backfill")
        )

    def test_receipt_media_root_mismatch_aborts_instead_of_recording_skip(
        self,
    ):
        self._create_candidate()
        alternate = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(alternate.cleanup)
        service = self._service(
            batch_size=1,
            storage=MediaStorage(media_root=alternate.name),
        )
        service.batch_journal = self._completed_path_journal(service)

        with self.assertRaisesRegex(
            CommandError, "^media_configuration_error$"
        ):
            service.run(execute=True)

        self.assertEqual(MediaAsset.objects.count(), 0)
        self.assertFalse(
            service.batch_journal.is_phase_complete("backfill")
        )

    def test_backfill_batch_closes_at_first_resource_limit(self):
        service = self._service(
            batch_limits=BatchLimits(
                max_images=10,
                max_bytes=100,
                max_pixels=500000000,
            )
        )
        candidates = []
        for image_id in (1, 2, 3):
            receipt = SimpleNamespace(
                size=10,
                width=20000,
                height=10000,
            )
            resources = SimpleNamespace(receipts=(receipt,))
            candidates.append(media_asset_backfill.BatchCandidate(
                SimpleNamespace(image_id=image_id),
                resources,
                {},
            ))

        batches = list(service._build_backfill_batches(candidates))

        self.assertEqual([len(batch) for batch in batches], [2, 1])

    def test_backfill_global_closure_runs_exactly_twice(self):
        self._create_candidate(content=_png_bytes("red", size=(30, 20)))
        self._create_candidate(content=_png_bytes("blue", size=(30, 20)))
        service = self._service(batch_size=1)
        service.batch_journal = self._completed_path_journal(service)

        with mock.patch.object(
            service,
            "_verify_database_plan_closure",
            wraps=service._verify_database_plan_closure,
        ) as closure:
            service.run(execute=True)

        self.assertEqual(closure.call_count, 2)

    def test_final_closure_rejects_receipt_identity_changed_after_commit(
        self,
    ):
        candidate = self._create_candidate(
            content=_png_bytes("red", size=(30, 20))
        )
        service = self._service(batch_size=1)
        service.batch_journal = self._completed_path_journal(service)
        original = Path(
            self.temporary_media.name,
            candidate["image"].image.name,
        )
        real_closure = service._verify_database_plan_closure
        closure_calls = {"count": 0}

        def replace_before_final_closure(*args, **kwargs):
            closure_calls["count"] += 1
            if closure_calls["count"] == 2:
                replacement = original.with_name("replacement.png")
                replacement.write_bytes(b"X" * original.stat().st_size)
                os.replace(str(replacement), str(original))
            return real_closure(*args, **kwargs)

        with mock.patch.object(
            service,
            "_verify_database_plan_closure",
            side_effect=replace_before_final_closure,
        ), self.assertRaisesRegex(
            CommandError, "^registry_plan_identity_changed$"
        ):
            service.run(execute=True)

        self.assertEqual(closure_calls["count"], 2)
        self.assertEqual(MediaAsset.objects.count(), 1)
        self.assertFalse(
            service.batch_journal.is_phase_complete("backfill")
        )

    def test_linear_backfill_keeps_plan_manifest_bytes_and_loads_sidecar(self):
        self._create_candidate()
        service = self._service(batch_size=1)
        service.batch_journal = self._completed_path_journal(service)
        service.run(execute=False)
        manifest_path = Path(service.run_directory, MANIFEST_FILENAME)
        before = manifest_path.read_bytes()

        executed = service.run(execute=True)
        loaded = load_completed_media_asset_backfill_summary(
            service.run_directory,
            MANIFEST_FILENAME,
            service.run_id,
            service.service_uid,
            service.service_gid,
            batch_journal=service.batch_journal,
        )

        self.assertEqual(manifest_path.read_bytes(), before)
        self.assertEqual(loaded, executed)

    def test_linear_backfill_reports_global_batch_progress(self):
        self._create_candidate()
        events = []
        service = self._service(
            batch_size=1,
            progress_reporter=events.append,
        )
        service.batch_journal = self._completed_path_journal(service)

        service.run(execute=True)

        self.assertEqual(
            [event["phase"] for event in events],
            ["backfill_planning", "backfill_registering", "finalizing"],
        )
        registering = events[1]
        self.assertEqual(registering["backfill_done"], 1)
        self.assertEqual(registering["backfill_total"], 1)
        self.assertEqual(registering["last_committed_batch"], 2)

    def _assert_linear_resume_boundary(
        self, fault_point, expected_database_applies, expected_commits
    ):
        self._create_candidate()
        crashed = {"value": False}

        def crash_once(point):
            if point == fault_point and not crashed["value"]:
                crashed["value"] = True
                raise RuntimeError("simulated linear crash")

        service = self._service(batch_size=1, fault_injector=crash_once)
        service.batch_journal = self._completed_path_journal(service)
        with self.assertRaisesRegex(RuntimeError, "simulated linear crash"):
            service.run(execute=True)
        service.fault_injector = None

        with mock.patch.object(
            service,
            "_apply_database_batch",
            wraps=service._apply_database_batch,
        ) as database_apply, mock.patch.object(
            service.batch_journal,
            "append_commit",
            wraps=service.batch_journal.append_commit,
        ) as append_commit:
            summary = service.run(execute=True)

        self.assertEqual(database_apply.call_count, expected_database_applies)
        self.assertEqual(append_commit.call_count, expected_commits)
        self.assertEqual(summary.registered, 1)
        self.assertEqual(MediaAsset.objects.count(), 1)

    def test_injected_backfill_does_not_duplicate_coordinator_attempt(self):
        self._create_candidate()
        service = self._service(batch_size=1)
        service.batch_journal = self._completed_path_journal(service)

        summary = service.run(execute=True)

        self.assertEqual(summary.registered, 1)
        self.assertEqual(len(service.batch_journal.state.attempts), 1)

    def test_injected_backfill_requires_attempt_before_work(self):
        self._create_candidate()
        service = self._service(batch_size=1)
        journal = self._completed_path_journal(
            service, coordinator_attempt=False
        )
        service.batch_journal = journal

        with mock.patch.object(
            service,
            "_freeze_all_plans",
            wraps=service._freeze_all_plans,
        ) as freeze_plans, mock.patch.object(
            service,
            "_verify_database_plan_closure",
            wraps=service._verify_database_plan_closure,
        ) as database_closure, mock.patch.object(
            service,
            "_apply_database_batch",
            wraps=service._apply_database_batch,
        ) as database_batch, mock.patch.object(
            MediaStorage,
            "prepare_from_receipts",
            autospec=True,
            wraps=MediaStorage.prepare_from_receipts,
        ) as prepare_from_receipts, mock.patch.object(
            journal,
            "append_intent",
            wraps=journal.append_intent,
        ) as append_intent, mock.patch.object(
            journal,
            "import_v2_batch",
            wraps=journal.import_v2_batch,
        ) as import_v2_batch, self.assertRaisesRegex(
            CommandError, "^linear_journal_invalid$"
        ):
            service.run(execute=True)

        self.assertEqual(freeze_plans.call_count, 0)
        self.assertEqual(database_closure.call_count, 0)
        self.assertEqual(database_batch.call_count, 0)
        self.assertEqual(prepare_from_receipts.call_count, 0)
        self.assertEqual(append_intent.call_count, 0)
        self.assertEqual(import_v2_batch.call_count, 0)
        self.assertEqual(MediaAsset.objects.count(), 0)

    def test_injected_dry_run_requires_attempt_before_work(self):
        self._create_candidate()
        service = self._service(batch_size=1)
        journal = self._completed_path_journal(
            service, coordinator_attempt=False
        )
        service.batch_journal = journal

        with mock.patch.object(
            service,
            "_path_receipts",
            wraps=service._path_receipts,
        ) as path_receipts, mock.patch.object(
            media_asset_backfill._BackfillManifestLog,
            "open",
            wraps=media_asset_backfill._BackfillManifestLog.open,
        ) as manifest_open, mock.patch.object(
            service,
            "_freeze_all_plans",
            wraps=service._freeze_all_plans,
        ) as freeze_plans, mock.patch.object(
            service,
            "_verify_database_plan_closure",
            wraps=service._verify_database_plan_closure,
        ) as database_closure, mock.patch.object(
            service,
            "_apply_database_batch",
            wraps=service._apply_database_batch,
        ) as database_batch, mock.patch.object(
            journal,
            "append_intent",
            wraps=journal.append_intent,
        ) as append_intent, mock.patch.object(
            journal,
            "append_commit",
            wraps=journal.append_commit,
        ) as append_commit, mock.patch.object(
            journal,
            "import_v2_batch",
            wraps=journal.import_v2_batch,
        ) as import_v2_batch, self.assertRaisesRegex(
            CommandError, "^linear_journal_invalid$"
        ):
            service.run(execute=False)

        self.assertEqual(path_receipts.call_count, 0)
        self.assertEqual(manifest_open.call_count, 0)
        self.assertEqual(freeze_plans.call_count, 0)
        self.assertEqual(database_closure.call_count, 0)
        self.assertEqual(database_batch.call_count, 0)
        self.assertEqual(append_intent.call_count, 0)
        self.assertEqual(append_commit.call_count, 0)
        self.assertEqual(import_v2_batch.call_count, 0)
        self.assertEqual(MediaAsset.objects.count(), 0)

    def test_backfill_resume_after_intent_reapplies_database_batch(self):
        self._assert_linear_resume_boundary(
            "after_backfill_intent", 1, 1
        )

    def test_backfill_resume_after_database_commit_repairs_commit_only(self):
        self._assert_linear_resume_boundary(
            "after_backfill_database_commit", 0, 1
        )

    def test_commit_only_resume_reuses_coordinator_attempt(self):
        self._create_candidate()
        crashed = {"value": False}

        def crash_once(point):
            if (
                point == "after_backfill_database_commit"
                and not crashed["value"]
            ):
                crashed["value"] = True
                raise RuntimeError("database committed")

        service = self._service(batch_size=1, fault_injector=crash_once)
        service.batch_journal = self._completed_path_journal(service)
        with self.assertRaisesRegex(RuntimeError, "database committed"):
            service.run(execute=True)
        service.fault_injector = None

        summary = service.run(execute=True)

        self.assertEqual(summary.registered, 1)
        self.assertEqual(len(service.batch_journal.state.attempts), 1)
        self.assertTrue(service.batch_journal.is_phase_complete("backfill"))

    def test_commit_only_resume_rejects_replaced_receipt_identity(self):
        candidate = self._create_candidate(
            content=_png_bytes("red", size=(30, 20))
        )
        crashed = {"value": False}

        def crash_once(point):
            if point == "after_backfill_database_commit" and not crashed[
                "value"
            ]:
                crashed["value"] = True
                raise RuntimeError("database committed")

        service = self._service(batch_size=1, fault_injector=crash_once)
        service.batch_journal = self._completed_path_journal(service)
        with self.assertRaisesRegex(RuntimeError, "database committed"):
            service.run(execute=True)
        service.fault_injector = None
        original = Path(
            self.temporary_media.name,
            candidate["image"].image.name,
        )
        replacement = original.with_name("replacement.png")
        replacement.write_bytes(b"X" * original.stat().st_size)
        os.replace(str(replacement), str(original))

        with self.assertRaisesRegex(
            CommandError, "^registry_plan_identity_changed$"
        ):
            service.run(execute=True)

        self.assertFalse(
            service.batch_journal.is_phase_complete("backfill")
        )

    def test_fresh_batch_rejects_post_state_without_durable_intent(self):
        candidate = self._create_candidate()
        service = self._service(batch_size=1)
        service.batch_journal = self._completed_path_journal(service)
        service.run(execute=False)
        original = next(
            receipt
            for receipt in service.batch_journal.receipts_by_image(
                "paths"
            )[candidate["image"].pk]
            if receipt.file_key.startswith("original:")
        )
        MediaAsset.objects.create(
            image=candidate["image"],
            submitter=self.owner,
            content_sha256=original.sha256,
        )

        with self.assertRaisesRegex(
            CommandError, "^linear_journal_database_conflict$"
        ):
            service.run(execute=True)

        self.assertFalse(any(
            intent.phase == "backfill"
            for intent in service.batch_journal.state.intents.values()
        ))

    def test_backfill_resume_after_commit_repeats_no_batch_work(self):
        self._assert_linear_resume_boundary(
            "after_backfill_commit", 0, 0
        )

    def test_completed_backfill_noop_does_not_record_attempt(self):
        self._create_candidate()
        service = self._service(batch_size=1)
        service.batch_journal = self._completed_path_journal(service)
        service.run(execute=True)
        before = len(service.batch_journal.state.attempts)

        service.run(execute=True)

        self.assertEqual(before, 1)
        self.assertEqual(
            len(service.batch_journal.state.attempts), before
        )

    def test_partial_v2_backfill_imports_prefix_and_preserves_bytes(self):
        self._create_candidate(content=_png_bytes("red", size=(30, 20)))
        second = self._create_candidate(
            content=_png_bytes("blue", size=(30, 20))
        )
        service = self._service(batch_size=1)
        service.run(execute=False)
        original_record = media_asset_backfill._BackfillManifestLog.record_result
        recorded = {"value": False}

        def record_then_crash(manifest, *args, **kwargs):
            result = original_record(manifest, *args, **kwargs)
            if not recorded["value"]:
                recorded["value"] = True
                raise RuntimeError("partial v2 crash")
            return result

        with mock.patch.object(
            media_asset_backfill._BackfillManifestLog,
            "record_result",
            new=record_then_crash,
        ), self.assertRaisesRegex(RuntimeError, "partial v2 crash"):
            service.run(execute=True)
        manifest_path = Path(service.run_directory, MANIFEST_FILENAME)
        before = manifest_path.read_bytes()
        service.batch_journal = self._completed_path_journal(service)
        prepared_image_ids = []
        real_prepare = MediaStorage.prepare_from_receipts

        def prepare(storage, image, *args, **kwargs):
            prepared_image_ids.append(image.pk)
            return real_prepare(storage, image, *args, **kwargs)

        with mock.patch.object(
            MediaStorage,
            "prepare_from_receipts",
            autospec=True,
            side_effect=prepare,
        ):
            summary = service.run(execute=True)

        self.assertEqual(manifest_path.read_bytes(), before)
        self.assertEqual(prepared_image_ids, [second["image"].pk])
        self.assertEqual(summary.registered, 2)
        self.assertEqual(len(service.batch_journal.state.attempts), 1)
        self.assertTrue(service.batch_journal.is_phase_complete("backfill"))

    def test_registry_complete_v2_upgrade_never_reopens_candidates(self):
        self._create_candidate()
        service = self._service(batch_size=1)
        service.run(execute=True)
        manifest_path = Path(service.run_directory, MANIFEST_FILENAME)
        before = manifest_path.read_bytes()
        service.batch_journal = self._completed_path_journal(service)

        with mock.patch.object(
            MediaStorage,
            "prepare_from_receipts",
            side_effect=AssertionError("complete v2 reopened candidate"),
        ), mock.patch.object(
            service,
            "_verify_database_plan_closure",
            wraps=service._verify_database_plan_closure,
        ) as closure:
            summary = service.run(execute=True)

        self.assertEqual(manifest_path.read_bytes(), before)
        self.assertEqual(summary.registered, 1)
        self.assertEqual(closure.call_count, 1)
        self.assertEqual(
            service.batch_journal.committed_ids("backfill"), frozenset()
        )
        self.assertTrue(service.batch_journal.is_phase_complete("backfill"))

    def test_partial_v2_import_intent_is_repaired_before_resume(self):
        self._create_candidate(content=_png_bytes("red", size=(30, 20)))
        self._create_candidate(content=_png_bytes("blue", size=(30, 20)))
        service = self._service(batch_size=1)
        service.run(execute=False)
        original_record = media_asset_backfill._BackfillManifestLog.record_result
        recorded = {"value": False}

        def record_then_crash(manifest, *args, **kwargs):
            result = original_record(manifest, *args, **kwargs)
            if not recorded["value"]:
                recorded["value"] = True
                raise RuntimeError("partial v2 crash")
            return result

        with mock.patch.object(
            media_asset_backfill._BackfillManifestLog,
            "record_result",
            new=record_then_crash,
        ), self.assertRaisesRegex(RuntimeError, "partial v2 crash"):
            service.run(execute=True)
        service.batch_journal = self._completed_path_journal(service)
        real_append_commit = service.batch_journal.append_commit
        interrupted = {"value": False}

        def interrupt_import(batch_id, post_signature):
            if (
                batch_id.startswith("upgrade-backfill:")
                and not interrupted["value"]
            ):
                interrupted["value"] = True
                raise RuntimeError("upgrade import crash")
            return real_append_commit(batch_id, post_signature)

        with mock.patch.object(
            service.batch_journal,
            "append_commit",
            side_effect=interrupt_import,
        ), self.assertRaisesRegex(RuntimeError, "upgrade import crash"):
            service.run(execute=True)

        summary = service.run(execute=True)

        self.assertEqual(summary.registered, 2)
        self.assertEqual(MediaAsset.objects.count(), 2)
        self.assertTrue(service.batch_journal.is_phase_complete("backfill"))

    def test_v2_upgrade_rejects_terminal_database_conflict(self):
        candidate = self._create_candidate()
        service = self._service(batch_size=1)
        service.run(execute=True)
        MediaAsset.objects.filter(image=candidate["image"]).update(
            content_sha256="0" * 64
        )
        service.batch_journal = self._completed_path_journal(service)

        with self.assertRaisesRegex(
            CommandError, "^linear_journal_database_conflict$"
        ):
            service.run(execute=True)

    def test_completed_summary_requires_complete_matching_sidecar(self):
        self._create_candidate()
        service = self._service(batch_size=1)
        service.batch_journal = self._completed_path_journal(service)
        service.run(execute=False)

        with self.assertRaisesRegex(
            CommandError, "^media_asset_plan_incomplete$"
        ):
            load_completed_media_asset_backfill_summary(
                service.run_directory,
                MANIFEST_FILENAME,
                service.run_id,
                service.service_uid,
                service.service_gid,
                batch_journal=service.batch_journal,
            )

        service.run(execute=True)
        service.batch_journal.state.phase_summaries["backfill"][
            "registered"
        ] = 0
        with self.assertRaisesRegex(
            CommandError, "^manifest_plan_mismatch$"
        ):
            load_completed_media_asset_backfill_summary(
                service.run_directory,
                MANIFEST_FILENAME,
                service.run_id,
                service.service_uid,
                service.service_gid,
                batch_journal=service.batch_journal,
            )

    def test_completed_linear_backfill_rejects_missing_registry_row(self):
        candidate = self._create_candidate()
        service = self._service(batch_size=1)
        service.batch_journal = self._completed_path_journal(service)
        service.run(execute=True)
        MediaAsset.objects.filter(image=candidate["image"]).delete()

        with self.assertRaisesRegex(
            CommandError, "^linear_journal_database_conflict$"
        ):
            service.run(execute=True)

    def _create_candidate(
        self,
        owners=None,
        content=None,
        original_filename="legacy.png",
    ):
        if owners is None:
            owners = (self.owner,)
        fetched = _fetched(content)
        asset_uuid = uuid.uuid4()
        storage = MediaStorage(media_root=self.temporary_media.name)
        prepared = storage.prepare(
            fetched,
            asset_uuid=asset_uuid,
            original_filename=original_filename,
        )
        file_rows = {}
        try:
            for prepared_file in prepared.files:
                path = Path(
                    self.temporary_media.name,
                    prepared_file.final_relative_path,
                )
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(os.pread(
                    prepared_file.owned_staging_handle.descriptor,
                    prepared_file.size,
                    0,
                ))
                file_rows[prepared_file.kind] = prepared_file
        finally:
            prepared.cleanup()

        original = file_rows["original"]
        image = Image.objects.create(
            image=original.final_relative_path,
            asset_uuid=asset_uuid,
            original_filename=original_filename,
            width=original.width,
            height=original.height,
        )
        for kind in ("thumbnail", "standard", "square"):
            prepared_file = file_rows[kind]
            Thumbnail.objects.create(
                original=image,
                size=kind,
                image=prepared_file.final_relative_path,
                width=prepared_file.width,
                height=prepared_file.height,
            )
        pins = tuple(
            Pin.objects.create(submitter=owner, image=image)
            for owner in owners
        )
        return {
            "image": image,
            "pins": pins,
            "fetched": fetched,
            "paths": {
                kind: file_rows[kind].final_relative_path
                for kind in file_rows
            },
            "dimensions": {
                kind: (file_rows[kind].width, file_rows[kind].height)
                for kind in file_rows
            },
        }

    def _create_missing_derivative_candidate(self, content):
        candidate = self._create_candidate(content=content)
        Thumbnail.objects.filter(
            original=candidate["image"],
            size="square",
        ).delete()
        return candidate

    def _execute_with_after_registry_commit_contender(
        self,
        service,
        operation,
    ):
        contender_result = {}

        def run_contender():
            close_old_connections()
            try:
                contender_result["value"] = operation()
            except BaseException as error:
                contender_result["error"] = getattr(
                    error,
                    "code",
                    error.__class__.__name__,
                )
            finally:
                connections["default"].close()

        def contend_after_registry_commit(event):
            if event != "after_registry_commit":
                return
            contender = threading.Thread(target=run_contender)
            contender.start()
            contender.join(2)
            if contender.is_alive():
                raise AssertionError("legacy Pin contender did not finish")

        service.fault_injector = contend_after_registry_commit
        command_error = None
        summary = None
        try:
            summary = service.run(execute=True)
        except CommandError as error:
            command_error = str(error)
        return command_error, summary, contender_result

    def _assert_writer_gate_blocks_legacy_pin_operation(
        self,
        service,
        operation,
        eligible,
        legacy_candidates,
        pin_ids,
        expected_contender,
    ):
        command_error, summary, contender_result = (
            self._execute_with_after_registry_commit_contender(
                service,
                operation,
            )
        )
        legacy_image_ids = tuple(
            candidate["image"].pk for candidate in legacy_candidates
        )
        self.assertEqual({
            "command_error": command_error,
            "registered": None if summary is None else summary.registered,
            "contender": contender_result,
            "pin_ids": set(Pin.objects.filter(pk__in=pin_ids).values_list(
                "pk",
                flat=True,
            )),
            "eligible_registered": MediaAsset.objects.filter(
                image=eligible["image"],
            ).exists(),
            "legacy_registered": MediaAsset.objects.filter(
                image_id__in=legacy_image_ids,
            ).exists(),
            "registry_count": MediaAsset.objects.count(),
        }, {
            "command_error": None,
            "registered": 1,
            "contender": expected_contender,
            "pin_ids": set(pin_ids),
            "eligible_registered": True,
            "legacy_registered": False,
            "registry_count": 1,
        })

    @staticmethod
    def _assert_code(code, function):
        with self_context_raises(CommandError) as caught:
            function()
        if str(caught.exception) != code:
            raise AssertionError(
                "expected {!r}, got {!r}".format(
                    code,
                    str(caught.exception),
                )
            )

    def test_sole_owner_exact_pipeline_closure_is_dry_then_registered(self):
        candidate = self._create_candidate()
        service = self._service()

        dry = service.run()

        self.assertIsInstance(dry, BackfillSummary)
        self.assertEqual(dry.scanned, 1)
        self.assertEqual(dry.eligible, 1)
        self.assertEqual(dry.registered, 0)
        self.assertEqual(dry.already_registered, 0)
        self.assertEqual(dry.skipped, 0)
        self.assertEqual(dry.reason_counts, {})
        self.assertFalse(MediaAsset.objects.exists())

        executed = service.run(execute=True)

        registry = MediaAsset.objects.get()
        self.assertEqual(registry.image_id, candidate["image"].pk)
        self.assertEqual(registry.submitter_id, self.owner.pk)
        self.assertEqual(
            registry.content_sha256,
            hashlib.sha256(candidate["fetched"].content).hexdigest(),
        )
        self.assertEqual(executed.registered, 1)
        self.assertEqual(executed.manifest_sha256, hashlib.sha256(
            Path(
                service.run_directory,
                MANIFEST_FILENAME,
            ).read_bytes()
        ).hexdigest())

        loaded = load_media_asset_plan(
            service.run_directory,
            MANIFEST_FILENAME,
            service.run_id,
            os.geteuid(),
            os.getegid(),
        )
        self.assertEqual(loaded, executed)
        self.assertEqual(
            load_completed_media_asset_backfill_summary(
                service.run_directory,
                MANIFEST_FILENAME,
                service.run_id,
                os.geteuid(),
                os.getegid(),
            ),
            executed,
        )

    def test_public_plan_loader_never_creates_a_missing_manifest(self):
        run_id, run_directory = self._new_run()
        manifest = run_directory / MANIFEST_FILENAME

        with self.assertRaisesRegex(
            CommandError,
            "^unsafe_media_asset_manifest$",
        ):
            load_media_asset_plan(
                str(run_directory),
                MANIFEST_FILENAME,
                run_id,
                os.geteuid(),
                os.getegid(),
            )

        self.assertFalse(manifest.exists())
        with self.assertRaisesRegex(
            CommandError,
            "^unsafe_media_asset_manifest$",
        ):
            load_completed_media_asset_backfill_summary(
                str(run_directory),
                MANIFEST_FILENAME,
                run_id,
                os.geteuid(),
                os.getegid(),
            )
        self.assertFalse(manifest.exists())

    def test_public_recovery_never_creates_a_missing_manifest(self):
        run_id, run_directory = self._new_run()
        manifest = run_directory / MANIFEST_FILENAME

        with self.assertRaisesRegex(
            CommandError,
            "^unsafe_media_asset_manifest$",
        ):
            recover_incomplete_media_asset_plan(
                str(run_directory),
                MANIFEST_FILENAME,
                run_id,
                os.geteuid(),
                os.getegid(),
            )

        self.assertFalse(manifest.exists())

    def test_public_plan_loader_rejects_torn_terminal_tail(self):
        self._create_candidate()
        service = self._service()
        service.run(execute=True)
        manifest = Path(service.run_directory, MANIFEST_FILENAME)
        before = manifest.read_bytes() + b'{"format_version":2'
        with manifest.open("ab") as file_obj:
            file_obj.write(b'{"format_version":2')

        with self.assertRaisesRegex(
            CommandError,
            "^media_asset_manifest_torn_tail_requires_execute$",
        ):
            load_media_asset_plan(
                service.run_directory,
                MANIFEST_FILENAME,
                service.run_id,
                os.geteuid(),
                os.getegid(),
            )

        self.assertEqual(manifest.read_bytes(), before)

    def test_completed_summary_loader_rejects_plan_before_execution(self):
        self._create_candidate()
        service = self._service()
        service.run(execute=False)

        with self.assertRaisesRegex(
            CommandError,
            "^media_asset_plan_incomplete$",
        ):
            load_completed_media_asset_backfill_summary(
                service.run_directory,
                MANIFEST_FILENAME,
                service.run_id,
                os.geteuid(),
                os.getegid(),
            )

    def test_incomplete_plan_prefix_is_reset_for_same_run_replanning(self):
        self._create_candidate(content=_png_bytes("red"))
        self._create_candidate(content=_png_bytes("blue"))
        service = self._service()
        original_write = (
            media_asset_backfill._BackfillManifestLog._write_frozen_line
        )
        recorded = {"value": False}

        def write_then_crash(manifest, line):
            original_write(manifest, line)
            if not recorded["value"]:
                recorded["value"] = True
                raise RuntimeError("plan interrupted")

        with mock.patch.object(
            media_asset_backfill._BackfillManifestLog,
            "_write_frozen_line",
            new=write_then_crash,
        ):
            with self.assertRaisesRegex(RuntimeError, "plan interrupted"):
                service.run(execute=False)

        manifest_path = Path(service.run_directory, MANIFEST_FILENAME)
        self.assertTrue(recover_incomplete_media_asset_plan(
            service.run_directory,
            MANIFEST_FILENAME,
            service.run_id,
            service.service_uid,
            service.service_gid,
        ))
        self.assertEqual(manifest_path.read_bytes(), b"")

        summary = service.run(execute=False)

        self.assertEqual(summary.scanned, 2)

    def test_incomplete_torn_plan_prefix_is_quarantined_then_reset(self):
        self._create_candidate()
        service = self._service()
        original_write = (
            media_asset_backfill._BackfillManifestLog._write_frozen_line
        )

        def write_then_crash(manifest, line):
            original_write(manifest, line)
            raise RuntimeError("plan interrupted")

        with mock.patch.object(
            media_asset_backfill._BackfillManifestLog,
            "_write_frozen_line",
            new=write_then_crash,
        ):
            with self.assertRaisesRegex(RuntimeError, "plan interrupted"):
                service.run(execute=False)
        manifest_path = Path(service.run_directory, MANIFEST_FILENAME)
        with manifest_path.open("ab") as manifest:
            manifest.write(b'{"format_version":2')

        self.assertTrue(recover_incomplete_media_asset_plan(
            service.run_directory,
            MANIFEST_FILENAME,
            service.run_id,
            service.service_uid,
            service.service_gid,
        ))

        self.assertEqual(manifest_path.read_bytes(), b"")
        self.assertEqual(
            len(list(Path(service.run_directory).glob(
                "media-asset-backfill.jsonl.torn-*"
            ))),
            1,
        )

    def test_complete_plan_is_never_reset(self):
        self._create_candidate()
        service = self._service()
        service.run(execute=False)
        manifest_path = Path(service.run_directory, MANIFEST_FILENAME)
        before = manifest_path.read_bytes()

        with self.assertRaisesRegex(
            CommandError,
            "^media_asset_plan_reset_forbidden$",
        ):
            recover_incomplete_media_asset_plan(
                service.run_directory,
                MANIFEST_FILENAME,
                service.run_id,
                service.service_uid,
                service.service_gid,
            )

        self.assertEqual(manifest_path.read_bytes(), before)

    def test_invalid_candidates_are_skipped_with_stable_reason_codes(self):
        orphan = self._create_candidate(owners=())
        del orphan
        self._create_candidate(owners=(self.owner, self.other_owner))

        wrong_leaf = self._create_candidate()
        wrong_path = "originals/{}/wrong-name.png".format(
            wrong_leaf["image"].asset_uuid
        )
        Path(
            self.temporary_media.name,
            wrong_leaf["paths"]["original"],
        ).rename(Path(self.temporary_media.name, wrong_path))
        Image.objects.filter(pk=wrong_leaf["image"].pk).update(
            image=wrong_path,
        )

        missing_row = self._create_candidate()
        Thumbnail.objects.filter(
            original=missing_row["image"],
            size="square",
        ).delete()

        extra_row = self._create_candidate()
        Thumbnail.objects.create(
            original=extra_row["image"],
            size="preview",
            image="derivatives/{}/preview.png".format(
                extra_row["image"].asset_uuid
            ),
            width=10,
            height=10,
        )

        zero_dimensions = self._create_candidate()
        Image.objects.filter(pk=zero_dimensions["image"].pk).update(
            width=0,
        )

        missing_file = self._create_candidate()
        Path(
            self.temporary_media.name,
            missing_file["paths"]["standard"],
        ).unlink()

        mismatched_pipeline = self._create_candidate()
        square_size = mismatched_pipeline["dimensions"]["square"]
        Path(
            self.temporary_media.name,
            mismatched_pipeline["paths"]["square"],
        ).write_bytes(_png_bytes("blue", square_size))

        summary = self._service().run()

        self.assertEqual(summary.scanned, 8)
        self.assertEqual(summary.eligible, 0, summary)
        self.assertEqual(summary.skipped, 8)
        self.assertEqual(summary.reason_counts, {
            "extra_derivative": 1,
            "invalid_dimensions": 1,
            "invalid_named_leaf": 1,
            "missing_derivative": 1,
            "multi_owner": 1,
            "orphan": 1,
            "pipeline_closure_mismatch": 1,
            "unsafe_media_file": 1,
        })
        self.assertFalse(MediaAsset.objects.exists())

    @override_settings(PINRY_FETCH_MAX_PIXELS=2000)
    def test_processing_pixel_limit_is_skipped_before_full_decode(self):
        candidate = self._create_candidate(
            content=_png_bytes(size=(32, 32)),
        )

        with mock.patch.object(PILImage, "MAX_IMAGE_PIXELS", 600):
            with mock.patch.object(
                ImageFile.ImageFile,
                "load",
                side_effect=AssertionError(
                    "backfill decoded oversized pixels"
                ),
            ):
                summary = self._service().run(execute=True)

        self.assertEqual(summary.scanned, 1)
        self.assertEqual(summary.eligible, 0)
        self.assertEqual(summary.skipped, 1)
        self.assertEqual(
            summary.reason_counts,
            {"processing_pixel_limit_exceeded": 1},
        )
        self.assertIn(
            "processing_pixel_limit_exceeded",
            SAFE_BACKFILL_REASON_CODES,
        )
        self.assertFalse(MediaAsset.objects.exists())
        self.assertTrue(Image.objects.filter(pk=candidate["image"].pk).exists())
        self.assertEqual(
            Thumbnail.objects.filter(original=candidate["image"]).count(),
            3,
        )
        self.assertEqual(
            Pin.objects.filter(image=candidate["image"]).count(),
            len(candidate["pins"]),
        )
        for relative_path in candidate["paths"].values():
            self.assertTrue(
                Path(self.temporary_media.name, relative_path).is_file()
            )

    @override_settings(PINRY_FETCH_MAX_PIXELS=2000)
    def test_processing_pixel_limit_skip_detects_replaced_original(self):
        candidate = self._create_candidate(
            content=_png_bytes("red", size=(32, 32)),
        )
        service = self._service()

        with mock.patch.object(PILImage, "MAX_IMAGE_PIXELS", 600):
            service.run()
            original = Path(
                self.temporary_media.name,
                candidate["paths"]["original"],
            )
            replacement = original.with_name("replacement.png")
            replacement.write_bytes(_png_bytes("blue", size=(32, 32)))
            os.replace(str(replacement), str(original))

            with self.assertRaisesRegex(
                CommandError,
                "^registry_plan_identity_changed$",
            ):
                service.run(execute=True)

    def test_file_receipt_rejects_hardlink_symlink_and_fifo_before_prepare(self):
        cases = ("hardlink", "symlink", "fifo")
        for index, kind in enumerate(cases):
            with self.subTest(kind=kind):
                candidate = self._create_candidate()
                target = Path(
                    self.temporary_media.name,
                    candidate["paths"]["thumbnail"],
                )
                retained = target.with_name(
                    "retained-{}-{}".format(index, kind)
                )
                target.rename(retained)
                if kind == "hardlink":
                    os.link(str(retained), str(target))
                elif kind == "symlink":
                    target.symlink_to(retained)
                else:
                    os.mkfifo(str(target))
                summary = self._service().run()
                self.assertGreaterEqual(
                    summary.reason_counts.get("unsafe_media_file", 0),
                    1,
                )
                if target.exists() or target.is_symlink():
                    target.unlink()
                retained.rename(target)

    def test_existing_registry_distinguishes_owner_image_and_collision(self):
        content = _png_bytes("green")
        registered = self._create_candidate(content=content)
        legacy = self._create_candidate(content=content)
        digest = hashlib.sha256(content).hexdigest()
        MediaAsset.objects.create(
            submitter=self.owner,
            image=registered["image"],
            content_sha256=digest,
        )

        summary = self._service().run(execute=True)

        self.assertEqual(summary.already_registered, 1)
        self.assertEqual(summary.skipped, 1)
        self.assertEqual(
            summary.reason_counts,
            {"existing_registry_collision": 1},
        )
        self.assertIn(
            "existing_registry_collision",
            SAFE_BACKFILL_REASON_CODES,
        )
        self.assertEqual(MediaAsset.objects.count(), 1)
        self.assertFalse(
            MediaAsset.objects.filter(image=legacy["image"]).exists()
        )

    def test_same_owner_same_hash_without_registry_skips_whole_group(self):
        content = _png_bytes("purple")
        self._create_candidate(content=content)
        self._create_candidate(content=content)

        summary = self._service().run(execute=True)

        self.assertEqual(summary.eligible, 0)
        self.assertEqual(summary.registered, 0)
        self.assertEqual(summary.skipped, 2)
        self.assertEqual(
            summary.reason_counts,
            {"duplicate_registry_collision": 2},
        )
        self.assertIn(
            "duplicate_registry_collision",
            SAFE_BACKFILL_REASON_CODES,
        )
        self.assertFalse(MediaAsset.objects.exists())

    def test_same_hash_for_different_owners_creates_independent_registries(self):
        content = _png_bytes("orange")
        first = self._create_candidate(
            owners=(self.owner,),
            content=content,
        )
        second = self._create_candidate(
            owners=(self.other_owner,),
            content=content,
        )

        summary = self._service().run(execute=True)

        self.assertEqual(summary.registered, 2)
        self.assertEqual(MediaAsset.objects.count(), 2)
        self.assertTrue(MediaAsset.objects.filter(
            image=first["image"],
            submitter=self.owner,
        ).exists())
        self.assertTrue(MediaAsset.objects.filter(
            image=second["image"],
            submitter=self.other_owner,
        ).exists())

    def test_every_database_and_file_identity_change_aborts_whole_run(self):
        mutations = (
            lambda candidate: Image.objects.filter(
                pk=candidate["image"].pk
            ).update(width=candidate["image"].width + 1),
            lambda candidate: Thumbnail.objects.filter(
                original=candidate["image"], size="thumbnail"
            ).update(width=1),
            lambda candidate: Thumbnail.objects.filter(
                original=candidate["image"], size="standard"
            ).update(size="changed"),
            self._replace_thumbnail_row,
            self._replace_original_inode,
            lambda candidate: Pin.objects.filter(
                pk=candidate["pins"][0].pk
            ).update(submitter=self.other_owner),
        )
        for mutation in mutations:
            with self.subTest(mutation=getattr(mutation, "__name__", "field")):
                candidate = self._create_candidate()
                service = self._service()
                service.run()
                mutation(candidate)

                with self.assertRaisesRegex(
                    CommandError,
                    "^registry_plan_identity_changed$",
                ):
                    service.run(execute=True)

    def test_image_added_after_plan_aborts_before_any_registry_write(self):
        self._create_candidate()
        service = self._service()
        service.run()
        self._create_candidate(
            owners=(self.other_owner,),
            content=_png_bytes("navy"),
        )

        with self.assertRaisesRegex(
            CommandError,
            "^registry_plan_identity_changed$",
        ):
            service.run(execute=True)

        self.assertFalse(MediaAsset.objects.exists())

    def test_precommit_fence_rejects_image_added_during_registry_insert(self):
        self._create_candidate()
        service = self._service()
        service.run()
        injected = {"value": False}

        def add_image(event):
            if event == "before_registry_insert" and not injected["value"]:
                injected["value"] = True
                self._create_candidate(
                    owners=(self.other_owner,),
                    content=_png_bytes("navy"),
                )

        service.fault_injector = add_image
        with self.assertRaisesRegex(
            CommandError,
            "^registry_plan_identity_changed$",
        ):
            service.run(execute=True)

        self.assertFalse(MediaAsset.objects.exists())
        self.assertEqual(Image.objects.count(), 1)

    def test_precommit_fence_rejects_other_planned_row_mutation(self):
        self._create_candidate(content=_png_bytes("red"))
        other = self._create_candidate(
            owners=(self.other_owner,),
            content=_png_bytes("navy"),
        )
        original_width = other["image"].width
        service = self._service()
        service.run()
        injected = {"value": False}

        def mutate_other(event):
            if event == "before_registry_insert" and not injected["value"]:
                injected["value"] = True
                Image.objects.filter(pk=other["image"].pk).update(
                    width=original_width + 1,
                )

        service.fault_injector = mutate_other
        with self.assertRaisesRegex(
            CommandError,
            "^registry_plan_identity_changed$",
        ):
            service.run(execute=True)

        self.assertFalse(MediaAsset.objects.exists())
        self.assertEqual(
            Image.objects.values_list("width", flat=True).get(
                pk=other["image"].pk
            ),
            original_width,
        )

    def test_registry_phase_rolls_back_first_candidate_on_later_change(self):
        self._create_candidate(content=_png_bytes("red"))
        other = self._create_candidate(
            owners=(self.other_owner,),
            content=_png_bytes("navy"),
        )
        original_width = other["image"].width
        service = self._service()
        service.run()
        inserts = {"count": 0}

        def mutate_on_second_insert(event):
            if event != "before_registry_insert":
                return
            inserts["count"] += 1
            if inserts["count"] == 2:
                Image.objects.filter(pk=other["image"].pk).update(
                    width=original_width + 1,
                )

        service.fault_injector = mutate_on_second_insert
        with self.assertRaisesRegex(
            CommandError,
            "^registry_plan_identity_changed$",
        ):
            service.run(execute=True)

        self.assertFalse(MediaAsset.objects.exists())
        self.assertEqual(
            Image.objects.values_list("width", flat=True).get(
                pk=other["image"].pk
            ),
            original_width,
        )

    def test_database_write_fence_reserves_the_sqlite_writer_slot(self):
        if connection.vendor != "sqlite":
            self.skipTest("SQLite writer reservation contract")
        database_name = connection.settings_dict["NAME"]
        probe = sqlite3.connect(
            database_name,
            timeout=0,
            isolation_level=None,
            uri=str(database_name).startswith("file:"),
        )
        try:
            with database_write_fence(
                using="default",
                models=(MediaAsset, Pin, Image, Thumbnail),
            ):
                self.assertTrue(connection.in_atomic_block)
                with self.assertRaisesRegex(
                    sqlite3.OperationalError,
                    "locked",
                ):
                    probe.execute("BEGIN IMMEDIATE")
        finally:
            probe.close()

    @override_settings(PINRY_FETCH_TOTAL_TIMEOUT=0.15)
    def test_busy_fence_releases_global_gate_before_outer_second_delete(self):
        if connection.vendor != "sqlite":
            self.skipTest("SQLite two-connection writer contract")
        if connection.creation.is_in_memory_db(
            connection.settings_dict["NAME"]
        ):
            self.skipTest("This concurrency contract requires file SQLite.")

        self._create_candidate(content=_png_bytes("red"))
        first = self._create_missing_derivative_candidate(
            _png_bytes("navy")
        )
        second = self._create_missing_derivative_candidate(
            _png_bytes("green")
        )
        service = self._service()
        service.run()
        first_pin_id = first["pins"][0].pk
        second_pin_id = second["pins"][0].pk

        first_deleted = threading.Event()
        release_second_delete = threading.Event()
        second_delete_started = threading.Event()
        fence_entered = threading.Event()
        outer_finished = threading.Event()
        backfill_finished = threading.Event()
        result = {}

        def run_outer_transaction():
            close_old_connections()
            try:
                with transaction.atomic():
                    Pin.objects.get(pk=first_pin_id).delete()
                    first_deleted.set()
                    if not release_second_delete.wait(5):
                        raise AssertionError(
                            "second legacy Pin delete was not released"
                        )
                    second_delete_started.set()
                    Pin.objects.get(pk=second_pin_id).delete()
                result["outer"] = "committed"
            except BaseException as error:
                result["outer_error"] = getattr(
                    error,
                    "code",
                    error.__class__.__name__,
                )
            finally:
                connections["default"].close()
                outer_finished.set()

        def run_backfill():
            close_old_connections()
            database = connections["default"]
            try:
                with database.cursor() as cursor:
                    cursor.execute("PRAGMA busy_timeout = 1000")
                try:
                    service.run(execute=True)
                    result["backfill"] = "completed"
                except BaseException as error:
                    result["backfill_error"] = {
                        "text": str(error),
                        "code": getattr(error, "code", None),
                        "retryable": getattr(error, "retryable", None),
                        "cause": getattr(error, "__cause__", None),
                    }
                with database.cursor() as cursor:
                    cursor.execute("PRAGMA busy_timeout")
                    result["backfill_busy_timeout"] = cursor.fetchone()[0]
                    cursor.execute("SELECT 1")
                    result["backfill_connection_reused"] = (
                        cursor.fetchone()[0]
                    )
            finally:
                database.close()
                backfill_finished.set()

        original_fence = media_asset_backfill.database_write_fence

        @contextmanager
        def observe_fence(using, models):
            if threading.current_thread().name == "backfill-fence-worker":
                fence_entered.set()
            with original_fence(using=using, models=models) as database:
                yield database

        outer = threading.Thread(
            target=run_outer_transaction,
            name="outer-legacy-delete-worker",
        )
        backfill = threading.Thread(
            target=run_backfill,
            name="backfill-fence-worker",
        )
        outer_done = False
        backfill_done = False
        try:
            with mock.patch.object(
                media_asset_backfill,
                "database_write_fence",
                side_effect=observe_fence,
            ):
                outer.start()
                self.assertTrue(first_deleted.wait(5))
                backfill.start()
                self.assertTrue(fence_entered.wait(5))
                release_second_delete.set()
                self.assertTrue(second_delete_started.wait(5))
                outer_done = outer_finished.wait(3)
                backfill_done = backfill_finished.wait(3)
        finally:
            release_second_delete.set()
            outer.join(5)
            backfill.join(5)

        self.assertTrue(outer_done)
        self.assertTrue(backfill_done)
        self.assertFalse(outer.is_alive())
        self.assertFalse(backfill.is_alive())
        self.assertEqual(result.get("outer"), "committed")
        self.assertNotIn("outer_error", result)
        self.assertEqual(result.get("backfill_error"), {
            "text": "database_busy",
            "code": "database_busy",
            "retryable": True,
            "cause": None,
        })
        self.assertNotIn("backfill", result)
        self.assertEqual(result.get("backfill_busy_timeout"), 1000)
        self.assertEqual(result.get("backfill_connection_reused"), 1)
        self.assertFalse(
            Pin.objects.filter(pk__in=(first_pin_id, second_pin_id)).exists()
        )
        self.assertFalse(MediaAsset.objects.exists())
        self.assertEqual(Image.objects.count(), 1)
        manifest = Path(service.run_directory, MANIFEST_FILENAME)
        events = [
            json.loads(line)["event"]
            for line in manifest.read_text("utf-8").splitlines()
        ]
        self.assertFalse(set(events) & {
            "registered",
            "recovered_registered",
            "already_registered",
            "skipped",
        })

    def test_database_fence_precedes_plan_closure_without_row_locks(self):
        self._create_candidate()
        service = self._service()
        service.run()
        original_fence = media_asset_backfill.database_write_fence
        original_verify = service._verify_database_plan_closure
        events = []
        fence_active = {"value": False}

        @contextmanager
        def record_fence(using, models):
            with original_fence(using=using, models=models) as database:
                events.append("fence")
                fence_active["value"] = True
                yield database

        def record_closure(plans, *args, **kwargs):
            if fence_active["value"]:
                events.append(("closure", kwargs.get("lock", False)))
            return original_verify(plans, *args, **kwargs)

        with mock.patch.object(
            media_asset_backfill,
            "database_write_fence",
            side_effect=record_fence,
        ), mock.patch.object(
            service,
            "_verify_database_plan_closure",
            side_effect=record_closure,
        ):
            service.run(execute=True)

        self.assertEqual(events[0], "fence")
        self.assertTrue(any(event[0] == "closure" for event in events[1:]))
        self.assertFalse(any(
            event == ("closure", True) for event in events
        ))

    def test_global_writer_gate_spans_registry_commit_to_terminal_phase(self):
        candidate = self._create_candidate(content=_png_bytes("red"))
        service = self._service()
        service.run()
        planned_digest = hashlib.sha256(
            candidate["fetched"].content
        ).hexdigest()
        planned_key = "{}:{}".format(
            self.owner.pk,
            planned_digest,
        ).encode("ascii")
        planned_stripe = hashlib.sha256(planned_key).digest()[0]
        writer_content = None
        for color in ("navy", "green", "blue", "purple"):
            content = _png_bytes(color)
            digest = hashlib.sha256(content).hexdigest()
            writer_key = "{}:{}".format(
                self.other_owner.pk,
                digest,
            ).encode("ascii")
            if hashlib.sha256(writer_key).digest()[0] != planned_stripe:
                writer_content = content
                break
        self.assertIsNotNone(writer_content)

        writer_digest = hashlib.sha256(writer_content).hexdigest()
        writer_result = {}

        def run_writer():
            close_old_connections()
            root_directory = None
            try:
                root_directory = file_ops.open_media_root(
                    self.temporary_media.name
                )
                deadline = time.monotonic() + 0.1
                with file_ops.media_dedup_lock(
                    root_directory,
                    self.other_owner.pk,
                    writer_digest,
                    deadline=deadline,
                ), file_ops.media_lifecycle_lock(
                    root_directory,
                    deadline=deadline,
                ):
                    image = Image.objects.create(
                        image="legacy/concurrent.png",
                        asset_uuid=uuid.uuid4(),
                        original_filename="concurrent.png",
                        width=1,
                        height=1,
                    )
                    writer_result["image_id"] = image.pk
            except BaseException as error:
                writer_result["error"] = getattr(
                    error,
                    "code",
                    error.__class__.__name__,
                )
            finally:
                if root_directory is not None:
                    root_directory.close()
                connections["default"].close()

        def contend_after_registry_commit(event):
            if event != "after_registry_commit":
                return
            writer = threading.Thread(target=run_writer)
            writer.start()
            writer.join(2)
            if writer.is_alive():
                raise AssertionError("concurrent writer did not finish")

        service.fault_injector = contend_after_registry_commit
        command_error = None
        try:
            summary = service.run(execute=True)
        except CommandError as error:
            command_error = str(error)
            summary = None

        self.assertEqual(
            (
                command_error,
                None if summary is None else summary.registered,
                writer_result,
                MediaAsset.objects.count(),
                Image.objects.count(),
            ),
            (
                None,
                1,
                {"error": "media_lifecycle_busy"},
                1,
                1,
            ),
        )

    @override_settings(PINRY_FETCH_TOTAL_TIMEOUT=0.1)
    def test_global_writer_gate_blocks_legacy_cleanup_between_phases(self):
        self._create_candidate(content=_png_bytes("red"))
        orphan = self._create_candidate(
            owners=(),
            content=_png_bytes("navy"),
        )
        service = self._service()
        service.run()
        cleanup_result = {}

        def run_cleanup():
            close_old_connections()
            try:
                core_models._delete_legacy_unreferenced_image(
                    orphan["image"].pk,
                    "default",
                )
                cleanup_result["deleted"] = True
            except BaseException as error:
                cleanup_result["error"] = getattr(
                    error,
                    "code",
                    error.__class__.__name__,
                )
            finally:
                connections["default"].close()

        def contend_after_registry_commit(event):
            if event != "after_registry_commit":
                return
            cleanup = threading.Thread(target=run_cleanup)
            cleanup.start()
            cleanup.join(2)
            if cleanup.is_alive():
                raise AssertionError("legacy cleanup did not finish")

        service.fault_injector = contend_after_registry_commit
        command_error = None
        try:
            summary = service.run(execute=True)
        except CommandError as error:
            command_error = str(error)
            summary = None

        self.assertEqual(
            (
                command_error,
                None if summary is None else summary.registered,
                cleanup_result,
                MediaAsset.objects.count(),
                Image.objects.count(),
            ),
            (
                None,
                1,
                {"error": "media_lifecycle_busy"},
                1,
                2,
            ),
        )

    @override_settings(PINRY_FETCH_TOTAL_TIMEOUT=0.1)
    def test_global_writer_gate_blocks_actual_legacy_pin_delete(self):
        eligible = self._create_candidate(content=_png_bytes("red"))
        legacy = self._create_missing_derivative_candidate(
            _png_bytes("navy")
        )
        service = self._service()
        service.run()
        pin_id = legacy["pins"][0].pk

        self._assert_writer_gate_blocks_legacy_pin_operation(
            service,
            lambda: Pin.objects.get(pk=pin_id).delete(),
            eligible,
            (legacy,),
            (pin_id,),
            {"error": "media_lifecycle_busy"},
        )

    @override_settings(PINRY_FETCH_TOTAL_TIMEOUT=0.1)
    def test_global_writer_gate_blocks_single_legacy_queryset_delete(self):
        eligible = self._create_candidate(content=_png_bytes("red"))
        legacy = self._create_missing_derivative_candidate(
            _png_bytes("green")
        )
        service = self._service()
        service.run()
        pin_id = legacy["pins"][0].pk

        self._assert_writer_gate_blocks_legacy_pin_operation(
            service,
            lambda: Pin.objects.filter(pk=pin_id).delete(),
            eligible,
            (legacy,),
            (pin_id,),
            {"error": "media_lifecycle_busy"},
        )

    @override_settings(PINRY_FETCH_TOTAL_TIMEOUT=0.1)
    def test_global_writer_gate_blocks_multi_legacy_queryset_delete(self):
        eligible = self._create_candidate(content=_png_bytes("red"))
        first = self._create_missing_derivative_candidate(
            _png_bytes("navy")
        )
        second = self._create_missing_derivative_candidate(
            _png_bytes("green")
        )
        service = self._service()
        service.run()
        pin_ids = (first["pins"][0].pk, second["pins"][0].pk)

        self._assert_writer_gate_blocks_legacy_pin_operation(
            service,
            lambda: Pin.objects.filter(pk__in=pin_ids).delete(),
            eligible,
            (first, second),
            pin_ids,
            {"error": "media_lifecycle_busy"},
        )

    @override_settings(PINRY_FETCH_TOTAL_TIMEOUT=0.1)
    def test_global_writer_gate_blocks_conditional_legacy_service_delete(self):
        eligible = self._create_candidate(content=_png_bytes("red"))
        legacy = self._create_missing_derivative_candidate(
            _png_bytes("purple")
        )
        source = Board.objects.create(
            submitter=self.owner,
            name="backfill-conditional-source",
        )
        source.pins.add(legacy["pins"][0])
        service = self._service()
        service.run()
        pin_id = legacy["pins"][0].pk
        owner_id = self.owner.pk

        def delete_through_service():
            return BulkPinManagementService().execute(
                User.objects.get(pk=owner_id),
                {
                    "operation": "delete_if_exclusive_to_board",
                    "pin_ids": [pin_id],
                    "source_board_id": source.pk,
                },
                time.monotonic(),
            )

        self._assert_writer_gate_blocks_legacy_pin_operation(
            service,
            delete_through_service,
            eligible,
            (legacy,),
            (pin_id,),
            {"value": {
                "operation": "delete_if_exclusive_to_board",
                "succeeded": 0,
                "preserved": 0,
                "failed": 1,
                "results": [{
                    "id": pin_id,
                    "status": "failed",
                    "code": "internal_error",
                    "retryable": False,
                }],
            }},
        )
        self.assertTrue(source.pins.filter(pk=pin_id).exists())

    def test_precommit_fence_rechecks_owner_after_registry_insert(self):
        candidate = self._create_candidate()
        service = self._service()
        service.run()

        def mutate_owner(sender, instance, created, **kwargs):
            del sender, instance, kwargs
            if created:
                Pin.objects.filter(pk=candidate["pins"][0].pk).update(
                    submitter=self.other_owner,
                )

        post_save.connect(mutate_owner, sender=MediaAsset, weak=False)
        try:
            with self.assertRaisesRegex(
                CommandError,
                "^registry_plan_identity_changed$",
            ):
                service.run(execute=True)
        finally:
            post_save.disconnect(mutate_owner, sender=MediaAsset)

        self.assertFalse(MediaAsset.objects.exists())
        self.assertEqual(
            Pin.objects.values_list("submitter_id", flat=True).get(
                pk=candidate["pins"][0].pk
            ),
            self.owner.pk,
        )

    def test_manifest_terminal_event_must_match_frozen_group_decision(self):
        candidate = self._create_candidate()
        service = self._service()
        summary = service.run()
        manifest = Path(service.run_directory, MANIFEST_FILENAME)
        forged = {
            "event": "skipped",
            "format_version": 2,
            "image_id": candidate["image"].pk,
            "plan_sha256": summary.plan_sha256,
            "reason_code": "orphan",
            "run_id": service.run_id,
            "target_signature": "media-asset-backfill-v2",
        }
        with manifest.open("ab") as file_obj:
            file_obj.write((
                json.dumps(
                    forged,
                    sort_keys=True,
                    separators=(",", ":"),
                ) + "\n"
            ).encode("utf-8"))
            file_obj.flush()
            os.fsync(file_obj.fileno())

        with self.assertRaisesRegex(
            CommandError,
            "^manifest_plan_mismatch$",
        ):
            service.run(execute=True)

    def _replace_thumbnail_row(self, candidate):
        thumbnail = Thumbnail.objects.get(
            original=candidate["image"],
            size="square",
        )
        values = {
            "image": thumbnail.image.name,
            "width": thumbnail.width,
            "height": thumbnail.height,
        }
        thumbnail.delete()
        Thumbnail.objects.create(
            original=candidate["image"],
            size="square",
            **values
        )

    def _replace_original_inode(self, candidate):
        path = Path(
            self.temporary_media.name,
            candidate["paths"]["original"],
        )
        replacement = path.with_name("replacement.png")
        replacement.write_bytes(path.read_bytes())
        os.replace(str(replacement), str(path))

    def test_plan_and_execute_each_share_one_strict_root(self):
        self._create_candidate()
        self._create_candidate(
            owners=(self.other_owner,),
            content=_png_bytes("blue"),
        )
        service = self._service()
        roots = []
        real_open = file_ops.open_verified_media_root

        def record_root(path):
            directory = real_open(path)
            if path == self.temporary_media.name:
                roots.append(directory)
            return directory

        with mock.patch(
            "core.services.media_asset_backfill.open_verified_media_root",
            side_effect=record_root,
        ):
            service.run()
            self.assertEqual(len(roots), 1)
            self.assertFalse(roots[0].descriptors)
            service.run(execute=True)

        self.assertEqual(len(roots), 2)
        self.assertFalse(roots[1].descriptors)

    def test_initial_symlinked_media_root_fails_with_sanitized_identity_code(self):
        self._create_candidate()
        media_root = Path(self.temporary_media.name)
        retained = media_root.with_name(media_root.name + "-retained")
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        media_root.rename(retained)
        media_root.symlink_to(outside.name, target_is_directory=True)
        try:
            with self.assertRaisesRegex(
                CommandError,
                "^registry_plan_identity_changed$",
            ):
                self._service().run()
        finally:
            media_root.unlink()
            retained.rename(media_root)

    def test_root_swap_immediately_before_prepare_creates_nothing_outside(self):
        self._create_candidate()
        media_root = Path(self.temporary_media.name)
        retained = media_root.with_name(media_root.name + "-retained")
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        swapped = {"value": False}

        def swap(event):
            if event != "before_plan_prepare" or swapped["value"]:
                return
            swapped["value"] = True
            media_root.rename(retained)
            media_root.symlink_to(outside.name, target_is_directory=True)

        try:
            with self.assertRaisesRegex(
                CommandError,
                "^registry_plan_identity_changed$",
            ):
                self._service(fault_injector=swap).run()
            self.assertEqual(list(Path(outside.name).rglob("*")), [])
        finally:
            if media_root.is_symlink():
                media_root.unlink()
            if retained.exists():
                retained.rename(media_root)

    def test_torn_tail_is_quarantined_and_execute_resumes(self):
        self._create_candidate()
        service = self._service()
        service.run()
        manifest = Path(service.run_directory, MANIFEST_FILENAME)
        with manifest.open("ab") as file_obj:
            file_obj.write(b'{"event":"secret-torn-tail"')
            file_obj.flush()
            os.fsync(file_obj.fileno())

        summary = service.run(execute=True)

        self.assertEqual(summary.registered, 1)
        quarantine = list(Path(service.run_directory).glob(
            MANIFEST_FILENAME + ".torn-*"
        ))
        self.assertEqual(len(quarantine), 1)
        self.assertEqual(
            stat.S_IMODE(os.stat(str(quarantine[0])).st_mode),
            0o600,
        )

    def test_crash_after_insert_before_event_recovers_registered_state(self):
        self._create_candidate()
        crashed = {"value": False}

        def crash_once(event):
            if event == "after_registry_commit" and not crashed["value"]:
                crashed["value"] = True
                raise RuntimeError("sentinel-secret-after-commit")

        service = self._service(fault_injector=crash_once)
        service.run()
        with self.assertRaisesRegex(RuntimeError, "sentinel-secret"):
            service.run(execute=True)
        self.assertEqual(MediaAsset.objects.count(), 1)

        service.fault_injector = None
        summary = service.run(execute=True)

        self.assertEqual(summary.registered, 1)
        events = [
            json.loads(line)
            for line in Path(
                service.run_directory,
                MANIFEST_FILENAME,
            ).read_text().splitlines()
        ]
        self.assertIn("recovered_registered", {
            event["event"] for event in events
        })

    def test_recovery_rechecks_registry_at_terminal_event_boundary(self):
        self._create_candidate()
        crashed = {"value": False}

        def crash_once(event):
            if event == "after_registry_commit" and not crashed["value"]:
                crashed["value"] = True
                raise RuntimeError("simulated crash")

        service = self._service(fault_injector=crash_once)
        service.run()
        with self.assertRaisesRegex(RuntimeError, "simulated crash"):
            service.run(execute=True)
        self.assertEqual(MediaAsset.objects.count(), 1)
        service.fault_injector = None
        original_record = media_asset_backfill._BackfillManifestLog.record_result

        def delete_then_record(
            manifest,
            event_name,
            image_id,
            reason_code=None,
            **kwargs
        ):
            if event_name == "recovered_registered":
                MediaAsset.objects.all().delete()
            return original_record(
                manifest,
                event_name,
                image_id,
                reason_code=reason_code,
                **kwargs
            )

        with mock.patch.object(
            media_asset_backfill._BackfillManifestLog,
            "record_result",
            new=delete_then_record,
        ):
            with self.assertRaisesRegex(
                CommandError,
                "^registry_plan_identity_changed$",
            ):
                service.run(execute=True)

        events = [
            json.loads(line)
            for line in Path(
                service.run_directory,
                MANIFEST_FILENAME,
            ).read_text().splitlines()
        ]
        self.assertTrue(MediaAsset.objects.exists())
        self.assertNotIn("recovered_registered", {
            event["event"] for event in events
        })

    def test_registry_event_is_fsynced_before_verifier_returns(self):
        self._create_candidate()
        crashed = {"value": False}

        def crash_once(event):
            if event == "after_registry_commit" and not crashed["value"]:
                crashed["value"] = True
                raise RuntimeError("simulated crash")

        service = self._service(fault_injector=crash_once)
        service.run()
        with self.assertRaisesRegex(RuntimeError, "simulated crash"):
            service.run(execute=True)
        service.fault_injector = None
        manifest_path = Path(
            service.run_directory,
            MANIFEST_FILENAME,
        )
        original_verify = service._verify_registry_event
        observed = {"terminal_before_delete": None}

        def verify_then_delete(*args, **kwargs):
            result = original_verify(*args, **kwargs)
            events = [
                json.loads(line)
                for line in manifest_path.read_text().splitlines()
            ]
            observed["terminal_before_delete"] = any(
                event["event"] == "recovered_registered"
                for event in events
            )
            MediaAsset.objects.all().delete()
            return result

        with mock.patch.object(
            service,
            "_verify_registry_event",
            side_effect=verify_then_delete,
        ):
            with self.assertRaisesRegex(
                CommandError,
                "^registry_plan_identity_changed$",
            ):
                service.run(execute=True)

        self.assertTrue(observed["terminal_before_delete"])
        self.assertTrue(MediaAsset.objects.exists())

    def test_partial_terminal_crash_resumes_after_all_registry_commits(self):
        self._create_candidate(content=_png_bytes("red"))
        self._create_candidate(
            owners=(self.other_owner,),
            content=_png_bytes("navy"),
        )
        service = self._service()
        service.run()
        original_record = media_asset_backfill._BackfillManifestLog.record_result
        crashed = {"value": False}

        def record_then_crash(
            manifest,
            event_name,
            image_id,
            reason_code=None,
            **kwargs
        ):
            result = original_record(
                manifest,
                event_name,
                image_id,
                reason_code=reason_code,
                **kwargs
            )
            if (
                event_name in ("registered", "recovered_registered")
                and not crashed["value"]
            ):
                crashed["value"] = True
                raise RuntimeError("partial terminal crash")
            return result

        with mock.patch.object(
            media_asset_backfill._BackfillManifestLog,
            "record_result",
            new=record_then_crash,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "partial terminal crash",
            ):
                service.run(execute=True)

        self.assertEqual(MediaAsset.objects.count(), 2)
        manifest_path = Path(service.run_directory, MANIFEST_FILENAME)
        terminal_events = [
            json.loads(line)
            for line in manifest_path.read_text().splitlines()
            if json.loads(line)["event"] in (
                "registered",
                "recovered_registered",
            )
        ]
        self.assertEqual(len(terminal_events), 1)

        summary = service.run(execute=True)

        self.assertEqual(summary.registered, 2)
        self.assertEqual(MediaAsset.objects.count(), 2)
        terminal_events = [
            json.loads(line)
            for line in manifest_path.read_text().splitlines()
            if json.loads(line)["event"] in (
                "registered",
                "recovered_registered",
            )
        ]
        self.assertEqual(len(terminal_events), 2)

    def test_manifest_rejects_symlink_hardlink_bad_mode_and_bad_owner(self):
        mutators = (
            self._manifest_symlink,
            self._manifest_hardlink,
            lambda path: os.chmod(str(path), 0o644),
        )
        for mutator in mutators:
            with self.subTest(mutator=mutator.__name__):
                candidate = self._create_candidate()
                del candidate
                service = self._service()
                service.run()
                manifest = Path(service.run_directory, MANIFEST_FILENAME)
                cleanup = mutator(manifest)
                try:
                    with self.assertRaisesRegex(
                        CommandError,
                        "^unsafe_media_asset_manifest$",
                    ):
                        service.run(execute=True)
                finally:
                    if cleanup is not None:
                        cleanup()

        service = self._service()
        service.run()
        manifest = Path(service.run_directory, MANIFEST_FILENAME)
        real_fstat = os.fstat

        def wrong_owner(descriptor):
            result = real_fstat(descriptor)
            if stat.S_ISREG(result.st_mode):
                values = list(result)
                values[4] = result.st_uid + 1
                return os.stat_result(values)
            return result

        with mock.patch(
            "core.services.media_asset_backfill.os.fstat",
            side_effect=wrong_owner,
        ):
            with self.assertRaisesRegex(
                CommandError,
                "^unsafe_media_asset_manifest$",
            ):
                service.run(execute=True)

    @staticmethod
    def _manifest_symlink(path):
        retained = path.with_name(path.name + ".retained")
        path.rename(retained)
        path.symlink_to(retained)

        def cleanup():
            path.unlink()
            retained.rename(path)

        return cleanup

    @staticmethod
    def _manifest_hardlink(path):
        linked = path.with_name(path.name + ".linked")
        os.link(str(path), str(linked))

        def cleanup():
            linked.unlink()

        return cleanup

    def test_manifest_rejects_foreign_run_target_and_middle_corruption(self):
        mutations = (
            lambda event: event.update(run_id=str(uuid.uuid4())),
            lambda event: event.update(target_signature="auto-v2"),
            None,
        )
        expected_codes = (
            "manifest_run_id_mismatch",
            "manifest_target_mismatch",
            "invalid_media_asset_manifest",
        )
        for mutation, expected in zip(mutations, expected_codes):
            with self.subTest(expected=expected):
                self._create_candidate()
                service = self._service()
                service.run()
                manifest = Path(service.run_directory, MANIFEST_FILENAME)
                lines = manifest.read_text().splitlines()
                if mutation is None:
                    lines[0] = "{middle-corruption"
                else:
                    event = json.loads(lines[0])
                    mutation(event)
                    lines[0] = json.dumps(event, sort_keys=True)
                manifest.write_text("\n".join(lines) + "\n")
                os.chmod(str(manifest), 0o600)

                with self.assertRaisesRegex(
                    CommandError,
                    "^{}$".format(expected),
                ):
                    service.run(execute=True)

    def test_success_skip_error_and_keyboard_interrupt_cleanup_staging(self):
        self._create_candidate()
        successful = self._service()
        successful.run(execute=True)
        self.assertEqual(self._staging_files(), [])

        self._create_candidate(owners=())
        skipped = self._service()
        skipped.run(execute=True)
        self.assertEqual(self._staging_files(), [])

        interrupted = {"value": False}

        def interrupt(event):
            if event == "after_plan_prepare" and not interrupted["value"]:
                interrupted["value"] = True
                raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            self._service(fault_injector=interrupt).run()
        self.assertEqual(self._staging_files(), [])

    def test_owned_root_cleanup_warnings_are_sanitized(self):
        candidate = self._create_candidate(
            original_filename="sentinel-private-original.png"
        )
        service = self._service()

        with mock.patch(
            "django_images.file_ops.OwnedStagingFile.cleanup",
            side_effect=OSError("sentinel-private-cleanup-error"),
        ), mock.patch(
            "core.services.media_storage.logger.warning",
        ) as warning:
            service.run()

        rendered = "\n".join(
            call.args[0] % call.args[1:]
            for call in warning.call_args_list
        )
        self.assertEqual(warning.call_count, 5)
        forbidden = (
            str(candidate["image"].asset_uuid),
            candidate["image"].original_filename,
            candidate["paths"]["original"],
            str(self.owner.pk),
            "sentinel-private-cleanup-error",
        )
        for value in forbidden:
            self.assertNotIn(value, rendered)
        self.assertIn("media_storage_cleanup_incomplete", rendered)
        self.assertIn("prepare_file_cleanup_failed", rendered)

    def _staging_files(self):
        staging = Path(self.temporary_media.name, ".staging")
        if not staging.exists():
            return []
        return [path for path in staging.rglob("*") if path.is_file()]

    def test_backfilled_asset_reuses_upload_and_last_pin_delete_removes_all(self):
        candidate = self._create_candidate(
            original_filename="sentinel-private-original.png"
        )
        original_pin = candidate["pins"][0]
        self._service().run(execute=True)
        service = PinImportService(
            fetcher=object(),
            media_storage=MediaStorage(
                media_root=self.temporary_media.name,
                clock=lambda: 10.0,
            ),
            idempotency=IdempotencyStore(),
            clock=lambda: 10.0,
        )
        prepared = service.prepare_upload(
            SimpleUploadedFile(
                "renamed.png",
                candidate["fetched"].content,
                content_type="image/png",
            ),
            deadline=20.0,
        )
        reused_pin = service.commit(
            prepared,
            self.owner,
            ImportMetadata(
                url=None,
                referer=None,
                description="reused",
                private=False,
                tags=(),
                board_ids=(),
            ),
            claim=None,
            deadline=20.0,
        )
        self.assertEqual(reused_pin.image_id, original_pin.image_id)
        self.assertEqual(Image.objects.count(), 1)
        self.assertEqual(len(_visible_media_snapshot(
            self.temporary_media.name
        )), 4)

        original_pin.delete()
        self.assertTrue(Image.objects.filter(pk=reused_pin.image_id).exists())
        reused_pin.delete()

        self.assertFalse(Image.objects.exists())
        self.assertFalse(Thumbnail.objects.exists())
        self.assertFalse(MediaAsset.objects.exists())
        self.assertEqual(
            _visible_media_snapshot(self.temporary_media.name),
            {},
        )

    def test_collision_skip_preserves_legacy_delete_and_orphan_is_unchanged(self):
        content = _png_bytes("cyan")
        first = self._create_candidate(content=content)
        second = self._create_candidate(content=content)
        orphan = self._create_candidate(owners=(), content=_png_bytes("gray"))
        orphan_snapshot = {
            path: Path(self.temporary_media.name, path).read_bytes()
            for path in orphan["paths"].values()
        }

        self._service().run(execute=True)

        self.assertFalse(MediaAsset.objects.exists())
        first["pins"][0].delete()
        self.assertFalse(Image.objects.filter(pk=first["image"].pk).exists())
        self.assertTrue(Image.objects.filter(pk=second["image"].pk).exists())
        self.assertTrue(Image.objects.filter(pk=orphan["image"].pk).exists())
        for path, content_bytes in orphan_snapshot.items():
            self.assertEqual(
                Path(self.temporary_media.name, path).read_bytes(),
                content_bytes,
            )

    def test_command_is_dry_by_default_and_output_is_sanitized(self):
        sentinel = "sentinel-private-original.png"
        candidate = self._create_candidate(original_filename=sentinel)
        run_id, run_directory = self._new_run()
        manifest = run_directory / MANIFEST_FILENAME
        stdout = StringIO()
        stderr = StringIO()

        result = call_command(
            "backfill_media_assets",
            manifest=str(manifest),
            run_id=run_id,
            stdout=stdout,
            stderr=stderr,
        )

        self.assertIsNone(result)
        self.assertFalse(MediaAsset.objects.exists())
        output = stdout.getvalue() + stderr.getvalue()
        forbidden = (
            str(candidate["image"].pk),
            str(candidate["pins"][0].pk),
            str(self.owner.pk),
            sentinel,
            candidate["paths"]["original"],
            self.temporary_media.name,
            "fixture.invalid",
        )
        for value in forbidden:
            self.assertNotIn(value, output)
        self.assertIn("scanned=1", output)
        self.assertIn("registered=0", output)

    def test_fail_closed_error_suppresses_sensitive_storage_cause(self):
        self._create_candidate()
        sentinel = "sentinel-secret /private/source/path"
        original_open = file_ops.open_verified_media_root

        def guarded_open(path):
            if os.fspath(path) == self.temporary_media.name:
                raise file_ops.MediaPathError(sentinel)
            return original_open(path)

        with mock.patch(
            "core.services.media_asset_backfill.open_verified_media_root",
            side_effect=guarded_open,
        ):
            with self.assertRaisesRegex(
                CommandError,
                "^registry_plan_identity_changed$",
            ) as caught:
                self._service().run()

        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertNotIn(sentinel, str(caught.exception))

    def test_invalid_existing_registry_fails_closed(self):
        candidate = self._create_candidate()
        MediaAsset.objects.create(
            submitter=self.owner,
            image=candidate["image"],
            content_sha256="0" * 64,
        )

        with self.assertRaisesRegex(
            CommandError,
            "^invalid_existing_registry$",
        ):
            self._service().run()


class DatabaseWriteFenceTests(SimpleTestCase):
    class Operations(object):
        @staticmethod
        def quote_name(name):
            return '"{}"'.format(name)

    class Connection(object):
        def __init__(
            self,
            vendor,
            statements,
            execute_error=None,
            restore_error=None,
            busy_timeout=875,
        ):
            self.vendor = vendor
            self.statements = statements
            self.execute_error = execute_error
            self.restore_error = restore_error
            self.busy_timeout = busy_timeout
            self.in_atomic_block = False
            self.closed = False
            self.ops = DatabaseWriteFenceTests.Operations()

        def cursor(self):
            connection = self

            class Cursor(object):
                def __enter__(self):
                    return self

                def __exit__(self, error_type, error, traceback):
                    del error_type, error, traceback
                    return False

                @staticmethod
                def fetchone():
                    return (connection.busy_timeout,)

                @staticmethod
                def execute(statement):
                    connection.statements.append(statement)
                    if (
                        statement.startswith("UPDATE ")
                        or statement.startswith("LOCK TABLE ")
                    ) and connection.execute_error is not None:
                        raise connection.execute_error
                    if (
                        statement == "PRAGMA busy_timeout = {}".format(
                            connection.busy_timeout
                        )
                        and connection.restore_error is not None
                    ):
                        raise connection.restore_error

            return Cursor()

        def close(self):
            self.closed = True

    @staticmethod
    def _atomic(connection, statements, exit_error=None):
        @contextmanager
        def atomic(using):
            statements.append("atomic_enter:{}".format(using))
            connection.in_atomic_block = True
            try:
                yield
            except BaseException:
                statements.append("atomic_rollback")
                raise
            else:
                statements.append("atomic_commit")
                if exit_error is not None:
                    raise exit_error
            finally:
                connection.in_atomic_block = False

        return atomic

    @contextmanager
    def _patched_fence(self, connection, exit_error=None):
        with mock.patch.object(
            database_fence,
            "connections",
            {"fence": connection},
        ), mock.patch.object(
            database_fence.transaction,
            "atomic",
            side_effect=self._atomic(
                connection,
                connection.statements,
                exit_error=exit_error,
            ),
        ):
            yield

    def test_deadline_is_a_busy_fence_error(self):
        self.assertTrue(issubclass(DatabaseFenceDeadline, DatabaseFenceBusy))

    def test_deadline_uses_five_second_monotonic_budget(self):
        values = iter((10.0, 14.999, 15.0))
        deadline = DatabaseFenceDeadline(lambda: next(values))

        deadline.checkpoint()
        with self.assertRaisesRegex(
            DatabaseFenceBusy,
            "^database_busy$",
        ) as caught:
            deadline.checkpoint()

        self.assertTrue(caught.exception.retryable)

    def test_sqlite_fence_owns_transaction_through_commit_then_restores(self):
        statements = []
        connection = self.Connection("sqlite", statements)

        with self._patched_fence(connection):
            with database_write_fence(
                using="fence",
                models=(MediaAsset, Pin, Image, Thumbnail),
            ) as entered_connection:
                self.assertIs(entered_connection, connection)
                self.assertTrue(connection.in_atomic_block)
                statements.append("yield")

        table_name = connection.ops.quote_name(MediaAsset._meta.db_table)
        primary_key = connection.ops.quote_name(
            MediaAsset._meta.pk.column
        )
        self.assertEqual(statements, [
            "PRAGMA busy_timeout",
            "PRAGMA busy_timeout = 0",
            "atomic_enter:fence",
            "UPDATE {table} SET {pk} = {pk} WHERE 0 = 1".format(
                table=table_name,
                pk=primary_key,
            ),
            "yield",
            "atomic_commit",
            "PRAGMA busy_timeout = 875",
        ])

    def test_sqlite_fence_restores_after_base_exception(self):
        statements = []
        connection = self.Connection("sqlite", statements)

        with self._patched_fence(connection), self.assertRaises(
            KeyboardInterrupt
        ):
            with database_write_fence(
                using="fence",
                models=(MediaAsset,),
            ):
                raise KeyboardInterrupt()

        self.assertEqual(statements[-2:], [
            "atomic_rollback",
            "PRAGMA busy_timeout = 875",
        ])
        self.assertFalse(connection.closed)

    def test_sqlite_fence_preserves_busy_when_restore_fails_and_closes(self):
        statements = []
        connection = self.Connection(
            "sqlite",
            statements,
            execute_error=OperationalError(
                "database is locked: sentinel /private/database.sqlite3"
            ),
            restore_error=OperationalError("restore sentinel"),
        )

        with self._patched_fence(connection), self.assertRaisesRegex(
            DatabaseFenceBusy, "^database_busy$"
        ) as caught:
            with database_write_fence(
                using="fence",
                models=(MediaAsset,),
            ):
                pass

        self.assertTrue(connection.closed)
        self.assertTrue(caught.exception.retryable)
        self.assertIsNone(caught.exception.__cause__)
        self.assertNotIn("sentinel", str(caught.exception))

    def test_sqlite_restore_failure_without_active_error_is_sanitized(self):
        statements = []
        connection = self.Connection(
            "sqlite",
            statements,
            restore_error=OperationalError(
                "sentinel /private/database.sqlite3"
            ),
        )

        with self._patched_fence(connection), self.assertRaisesRegex(
            DatabaseFenceError, "^database_fence_failed$"
        ) as caught:
            with database_write_fence(
                using="fence",
                models=(MediaAsset,),
            ):
                pass

        self.assertTrue(connection.closed)
        self.assertIsNone(caught.exception.__cause__)
        self.assertNotIn("sentinel", str(caught.exception))

    def test_sqlite_commit_busy_is_restored_then_normalized(self):
        statements = []
        connection = self.Connection("sqlite", statements)
        failure = OperationalError(
            "database is locked: sentinel /private/database.sqlite3"
        )

        with self._patched_fence(
            connection,
            exit_error=failure,
        ), self.assertRaisesRegex(
            DatabaseFenceBusy, "^database_busy$"
        ):
            with database_write_fence(
                using="fence",
                models=(MediaAsset,),
            ):
                pass

        self.assertEqual(statements[-2:], [
            "atomic_commit",
            "PRAGMA busy_timeout = 875",
        ])

    def test_postgresql_fence_locks_sorted_unique_tables(self):
        statements = []
        connection = self.Connection("postgresql", statements)
        models = (Thumbnail, MediaAsset, Pin, Image, MediaAsset)

        with self._patched_fence(connection):
            with database_write_fence(using="fence", models=models):
                self.assertTrue(connection.in_atomic_block)

        table_names = sorted({model._meta.db_table for model in models})
        self.assertEqual(
            [statement for statement in statements if statement.startswith(
                "LOCK TABLE "
            )],
            [
                'LOCK TABLE "{}" IN EXCLUSIVE MODE NOWAIT'.format(
                    table_name
                )
                for table_name in table_names
            ],
        )

    def test_postgresql_busy_sqlstates_are_sanitized(self):
        for sqlstate in ("40001", "40P01", "55P03"):
            statements = []
            cause = OperationalError("opaque backend failure")
            cause.pgcode = sqlstate
            failure = OperationalError(
                "sentinel /private/database.sql table=core_mediaasset"
            )
            failure.__cause__ = cause
            connection = self.Connection(
                "postgresql",
                statements,
                execute_error=failure,
            )

            with self.subTest(sqlstate=sqlstate), self._patched_fence(
                connection
            ), self.assertRaisesRegex(
                DatabaseFenceBusy, "^database_busy$"
            ) as caught:
                with database_write_fence(
                    using="fence",
                    models=(MediaAsset,),
                ):
                    pass

            self.assertTrue(caught.exception.retryable)
            self.assertIsNone(caught.exception.__cause__)
            self.assertNotIn("sentinel", str(caught.exception))

    def test_postgresql_commit_busy_is_normalized(self):
        statements = []
        connection = self.Connection("postgresql", statements)
        cause = OperationalError("opaque backend failure")
        cause.pgcode = "40001"
        failure = OperationalError(
            "sentinel /private/database.sql table=core_mediaasset"
        )
        failure.__cause__ = cause

        with self._patched_fence(
            connection,
            exit_error=failure,
        ), self.assertRaisesRegex(
            DatabaseFenceBusy,
            "^database_busy$",
        ) as caught:
            with database_write_fence(
                using="fence",
                models=(MediaAsset,),
            ):
                pass

        self.assertTrue(caught.exception.retryable)
        self.assertIsNone(caught.exception.__cause__)
        self.assertNotIn("sentinel", str(caught.exception))

    def test_fence_rejects_an_existing_transaction(self):
        statements = []
        connection = self.Connection("sqlite", statements)
        connection.in_atomic_block = True

        with mock.patch.object(
            database_fence,
            "connections",
            {"fence": connection},
        ), self.assertRaisesRegex(
            RuntimeError,
            "^database_write_fence_requires_top_level_transaction$",
        ):
            with database_write_fence(
                using="fence",
                models=(MediaAsset,),
            ):
                pass

        self.assertEqual(statements, [])


class self_context_raises(object):
    def __init__(self, exception_type):
        self.exception_type = exception_type
        self.exception = None

    def __enter__(self):
        return self

    def __exit__(self, error_type, error, traceback):
        del traceback
        if error_type is None:
            raise AssertionError("expected exception was not raised")
        if not issubclass(error_type, self.exception_type):
            return False
        self.exception = error
        return True
