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
import uuid

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import (
    close_old_connections,
    connection,
    connections,
    transaction,
)
from django.db.models.signals import post_save
from django.test import TransactionTestCase, override_settings
import mock
from PIL import Image as PILImage

from core import models as core_models
from core.models import MediaAsset, Pin
from core.services import media_asset_backfill
from core.services.idempotency import IdempotencyStore
from core.services.media_asset_backfill import (
    BackfillSummary,
    MediaAssetBackfiller,
)
from core.services.media_storage import MediaStorage
from core.services.pin_import import ImportMetadata, PinImportService
from core.services.safe_url_fetch import FetchedImage
from django_images import file_ops
from django_images.models import Image, Thumbnail
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
    ):
        if run_id is None:
            run_id, run_directory = self._new_run()
        return MediaAssetBackfiller(
            str(run_directory),
            MANIFEST_FILENAME,
            run_id,
            os.geteuid(),
            os.getegid(),
            batch_size=100,
            media_storage=storage,
            fault_injector=fault_injector,
        )

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

    def test_sqlite_database_fence_reserves_the_writer_slot(self):
        if connection.vendor != "sqlite":
            self.skipTest("SQLite writer reservation contract")
        acquire_fence = getattr(
            media_asset_backfill,
            "_acquire_database_write_fence",
            None,
        )
        self.assertIsNotNone(acquire_fence)
        database_name = connection.settings_dict["NAME"]
        probe = sqlite3.connect(
            database_name,
            timeout=0,
            isolation_level=None,
            uri=str(database_name).startswith("file:"),
        )
        try:
            with transaction.atomic():
                acquire_fence(connection)
                with self.assertRaisesRegex(
                    sqlite3.OperationalError,
                    "locked",
                ):
                    probe.execute("BEGIN IMMEDIATE")
        finally:
            probe.close()

    def test_postgresql_fence_uses_delete_order_before_row_work(self):
        acquire_fence = getattr(
            media_asset_backfill,
            "_acquire_database_write_fence",
            None,
        )
        self.assertIsNotNone(acquire_fence)
        statements = []

        class Operations(object):
            @staticmethod
            def quote_name(name):
                return '"{}"'.format(name)

        class Cursor(object):
            def __enter__(self):
                return self

            def __exit__(self, error_type, error, traceback):
                del error_type, error, traceback
                return False

            @staticmethod
            def execute(statement):
                statements.append(statement)

        class PostgreSQLConnection(object):
            vendor = "postgresql"
            ops = Operations()

            @staticmethod
            def cursor():
                return Cursor()

        acquire_fence(PostgreSQLConnection())

        table_names = (
            MediaAsset._meta.db_table,
            Pin._meta.db_table,
            Image._meta.db_table,
            Thumbnail._meta.db_table,
        )
        self.assertEqual(statements, [
            'LOCK TABLE "{}" IN EXCLUSIVE MODE'.format(table_name)
            for table_name in table_names
        ])

    def test_database_fence_precedes_plan_closure_without_row_locks(self):
        self._create_candidate()
        service = self._service()
        service.run()
        acquire_fence = getattr(
            media_asset_backfill,
            "_acquire_database_write_fence",
            None,
        )
        self.assertIsNotNone(acquire_fence)
        original_verify = service._verify_database_plan_closure
        events = []
        fence_active = {"value": False}

        def record_fence(database_connection):
            acquire_fence(database_connection)
            events.append("fence")
            fence_active["value"] = True

        def record_closure(plans, *args, **kwargs):
            if fence_active["value"]:
                events.append(("closure", kwargs.get("lock", False)))
            return original_verify(plans, *args, **kwargs)

        with mock.patch.object(
            media_asset_backfill,
            "_acquire_database_write_fence",
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
        ) as strict_open:
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
