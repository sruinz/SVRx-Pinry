import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace
import tempfile
from unittest import mock

from django.core.management import CommandError
from django.test import SimpleTestCase

from core.services.media_asset_backfill import BackfillSummary
from django_images.services import migration_state
from django_images.services.legacy_startup import (
    LegacyStartupCoordinator,
    LegacyStartupError,
    _atomic_write_summary,
)
from django_images.services.media_migration_v2 import AutoV2PlanSummary
from django_images.services.startup_preflight import (
    LegacyEvidence,
    PreflightResult,
    SpaceBudget,
)


RUN_ID = "20260825T120000Z-12345678-1234-4678-9234-567812345678"


class _Intent(object):
    def __init__(self, identity):
        self.identity = identity

    def as_dict(self):
        return {
            "source_root_device": 1,
            "source_root_inode": 2,
            "source_parent_relative": "originals",
            "source_parent_device": 3,
            "source_parent_inode": 4,
            "source_name": "{}.jpg".format(self.identity),
            "source_device": 5,
            "source_inode": self.identity,
            "destination_root_device": 6,
            "destination_root_inode": 7,
            "destination_parent_relative": "media/originals",
            "destination_parent_device": 8,
            "destination_parent_inode": 9,
            "destination_name": "{}.jpg".format(self.identity),
        }


class LegacyStartupCoordinatorTests(SimpleTestCase):
    def setUp(self):
        super(LegacyStartupCoordinatorTests, self).setUp()
        self.temporary = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.data_root = Path(self.temporary.name, "data")
        self.data_root.mkdir(mode=0o700)
        self.media_root = self.data_root / "static" / "media"
        self.media_root.mkdir(mode=0o700, parents=True)
        self.database_path = self.data_root / "production.db"
        self.backup_root = self.data_root / "legacy-backup"
        self.uid = os.geteuid()
        self.gid = os.getegid()
        self.settings_override = self.settings(
            PINRY_DATA_ROOT=str(self.data_root),
            MEDIA_ROOT=str(self.media_root),
            DATABASES={
                "default": {
                    "ENGINE": "django.db.backends.sqlite3",
                    "NAME": str(self.database_path),
                }
            },
            PINRY_SOURCE_COMMIT="a" * 40,
            IMAGE_SIZES={
                "thumbnail": {"size": [32, 32]},
                "standard": {"size": [64, 64]},
                "square": {"size": [32, 32]},
            },
        )
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)

    @staticmethod
    def evidence(present=True, database_exists=True, pending=()):
        return LegacyEvidence(
            database_exists=database_exists,
            database_bytes=100 if database_exists else 0,
            distinct_legacy_bytes=200 if present else 0,
            has_md5_paths=present,
            has_fixed_slot_paths=False,
            has_named_canonical_paths=False,
            has_media_image_directory=present,
            has_media_rows=present,
            pending_migrations=tuple(pending),
            database_identity=(
                {"device": 1, "inode": 2}
                if database_exists else None
            ),
            media_root_identity={"device": 3, "inode": 4},
        )

    def coordinator(self, fault_injector=None):
        return LegacyStartupCoordinator(
            self.uid,
            self.gid,
            fault_injector=fault_injector,
        )

    def test_fresh_empty_install_never_creates_backup_run(self):
        evidence = self.evidence(present=False, database_exists=False)
        coordinator = self.coordinator()

        with mock.patch(
            "django_images.services.legacy_startup."
            "startup_preflight.inspect_legacy_evidence",
            return_value=evidence,
        ), mock.patch(
            "django_images.services.legacy_startup."
            "startup_preflight.validate_storage_configuration_preflight",
            return_value=PreflightResult(ok=True),
        ) as configuration, mock.patch(
            "django_images.services.legacy_startup."
            "startup_preflight.available_space_bytes",
        ) as available:
            run = coordinator.prepare_before_schema()
            coordinator.converge_after_schema(run)

        self.assertIsNone(run)
        self.assertFalse(self.backup_root.exists())
        available.assert_not_called()
        configuration.assert_called_once()

    def test_no_flag_fresh_install_creates_missing_media_layout(self):
        self.media_root.rmdir()
        before_schema = self.evidence(
            present=False,
            database_exists=False,
        )
        after_schema = self.evidence(
            present=False,
            database_exists=True,
        )
        coordinator = self.coordinator()

        with mock.patch(
            "django_images.services.legacy_startup."
            "startup_preflight.inspect_legacy_evidence",
            side_effect=(before_schema, after_schema),
        ), mock.patch(
            "django_images.services.legacy_startup."
            "startup_preflight.validate_storage_configuration_preflight",
            return_value=PreflightResult(ok=True),
        ):
            coordinator.prepare_no_flag_before_schema()
            coordinator.converge_after_schema(None)

        self.assertTrue(self.media_root.is_dir())
        self.assertEqual(
            stat.S_IMODE(self.media_root.stat().st_mode),
            0o700,
        )

    def test_no_flag_existing_database_never_creates_missing_media_layout(self):
        self.media_root.rmdir()
        evidence = self.evidence(
            present=False,
            database_exists=True,
        )
        coordinator = self.coordinator()

        with mock.patch(
            "django_images.services.legacy_startup."
            "startup_preflight.inspect_legacy_evidence",
            return_value=evidence,
        ), self.assertRaisesRegex(
            LegacyStartupError,
            "^media_storage_configuration_invalid$",
        ):
            coordinator.prepare_no_flag_before_schema()
            coordinator.converge_after_schema(None)

        self.assertFalse(self.media_root.exists())

    def test_no_flag_fresh_candidate_rejects_rows_created_during_schema(self):
        self.media_root.rmdir()
        before_schema = self.evidence(
            present=False,
            database_exists=False,
        )
        after_schema = LegacyEvidence(
            **dict(
                self.evidence(
                    present=False,
                    database_exists=True,
                ).__dict__,
                has_media_rows=True,
            )
        )
        coordinator = self.coordinator()

        with mock.patch(
            "django_images.services.legacy_startup."
            "startup_preflight.inspect_legacy_evidence",
            side_effect=(before_schema, after_schema),
        ), mock.patch(
            "django_images.services.legacy_startup."
            "startup_preflight.validate_storage_configuration_preflight",
            return_value=PreflightResult(ok=True),
        ), self.assertRaisesRegex(
            LegacyStartupError,
            "^media_storage_configuration_invalid$",
        ):
            coordinator.prepare_no_flag_before_schema()
            coordinator.converge_after_schema(None)

        self.assertFalse(self.media_root.exists())

    def test_no_flag_legacy_evidence_requires_explicit_flag_before_schema(self):
        evidence = self.evidence(present=True)

        with mock.patch(
            "django_images.services.legacy_startup."
            "startup_preflight.inspect_legacy_evidence",
            return_value=evidence,
        ), self.assertRaisesRegex(
            LegacyStartupError,
            "^legacy_migration_flag_required$",
        ):
            self.coordinator().prepare_no_flag_before_schema()

    def test_flag_fresh_current_empty_database_creates_missing_media_layout(
        self,
    ):
        self.media_root.rmdir()
        evidence = self.evidence(present=False, database_exists=True)
        coordinator = self.coordinator()

        with mock.patch(
            "django_images.services.legacy_startup."
            "startup_preflight.inspect_legacy_evidence",
            return_value=evidence,
        ), mock.patch(
            "django_images.services.legacy_startup."
            "startup_preflight.validate_storage_configuration_preflight",
            return_value=PreflightResult(ok=True),
        ):
            run = coordinator.prepare_before_schema()
            coordinator.converge_after_schema(run)

        self.assertIsNone(run)
        self.assertFalse(self.backup_root.exists())
        self.assertTrue(self.media_root.is_dir())
        self.assertEqual(
            stat.S_IMODE(self.media_root.stat().st_mode),
            0o700,
        )

    def test_adjust_ownership_excludes_backup_root_and_payload(self):
        self.database_path.write_bytes(b"database")
        self.backup_root.mkdir()
        held_lock = mock.Mock()
        coordinator = self.coordinator()

        with mock.patch(
            "django_images.services.legacy_startup."
            "startup_preflight.adjust_storage_ownership",
        ) as adjust:
            coordinator.adjust_ownership(held_lock)

        args = adjust.call_args.args
        self.assertNotIn(str(self.backup_root), args[1])
        self.assertEqual(adjust.call_args.kwargs, {})

    def test_full_run_uses_exact_phase_and_service_order(self):
        events = []
        evidence = self.evidence()
        media_dry = AutoV2PlanSummary(
            run_id=RUN_ID,
            plan_sha256="1" * 64,
            manifest_sha256="2" * 64,
            image_count=2,
            md5_legacy=1,
            fixed_slot=1,
            named_canonical=0,
            copy_required_bytes=300,
        )
        media_execute = AutoV2PlanSummary(
            **dict(media_dry.__dict__, manifest_sha256="3" * 64)
        )
        backfill_dry = BackfillSummary(
            run_id=RUN_ID,
            plan_sha256="4" * 64,
            manifest_sha256="5" * 64,
            scanned=2,
            eligible=1,
            registered=0,
            already_registered=0,
            skipped=1,
            reason_counts={"orphan": 1},
        )
        backfill_execute = BackfillSummary(
            **dict(
                backfill_dry.__dict__,
                manifest_sha256="6" * 64,
                registered=1,
            )
        )

        class Migrator(object):
            def run(self, execute=False):
                events.append("media_execute" if execute else "media_plan")
                return media_execute if execute else media_dry

        class Backfiller(object):
            def run(self, execute=False):
                events.append(
                    "backfill_execute" if execute else "backfill_plan"
                )
                return backfill_execute if execute else backfill_dry

        intents = (_Intent(10), _Intent(11))
        progress = {
            "items": [
                {"intent": intent.as_dict(), "complete": False}
                for intent in intents
            ]
        }

        class Archive(object):
            def prepare(self, **kwargs):
                del kwargs
                events.append("archive_prepare")
                return SimpleNamespace(intents=intents, progress=progress)

            def converge(self, plan, syscall_adapter, on_item_complete):
                del syscall_adapter
                events.append("archive_converge")
                current = plan.progress
                for index, intent in enumerate(plan.intents):
                    current = {
                        "items": [
                            {
                                "intent": item["intent"],
                                "complete": position <= index,
                            }
                            for position, item in enumerate(current["items"])
                        ]
                    }
                    on_item_complete(
                        intent,
                        current,
                        SimpleNamespace(status="archived"),
                    )
                return SimpleNamespace(results=(), progress=current)

        real_transition = migration_state.transition_state
        real_seal = migration_state.seal_run_identities

        def transition(run, expected, next_phase, **kwargs):
            events.append("state:{}".format(next_phase))
            return real_transition(
                run, expected, next_phase, **kwargs
            )

        def seal(run, current_evidence):
            events.append("identity_seal")
            return real_seal(run, current_evidence)

        coordinator = self.coordinator()
        patches = (
            mock.patch(
                "django_images.services.legacy_startup."
                "startup_preflight.inspect_legacy_evidence",
                return_value=evidence,
            ),
            mock.patch(
                "django_images.services.legacy_startup."
                "startup_preflight.calculate_initial_space",
                return_value=SpaceBudget(300, 64, 364),
            ),
            mock.patch(
                "django_images.services.legacy_startup."
                "startup_preflight.calculate_remaining_space",
                return_value=SpaceBudget(300, 64, 364),
            ),
            mock.patch(
                "django_images.services.legacy_startup."
                "startup_preflight.available_space_bytes",
                side_effect=lambda path: events.append("space") or 10_000,
            ),
            mock.patch(
                "django_images.services.legacy_startup."
                "snapshot_sqlite",
                side_effect=lambda *args: (
                    events.append("snapshot")
                    or SimpleNamespace(sha256="7" * 64)
                ),
            ),
            mock.patch(
                "django_images.services.legacy_startup."
                "startup_preflight.validate_storage_configuration_preflight",
                side_effect=lambda *args: (
                    events.append("storage_configuration")
                    or PreflightResult(ok=True)
                ),
            ),
            mock.patch(
                "django_images.services.legacy_startup.AutoV2MediaMigrator",
                return_value=Migrator(),
            ),
            mock.patch(
                "django_images.services.legacy_startup.MediaAssetBackfiller",
                return_value=Backfiller(),
            ),
            mock.patch(
                "django_images.services.legacy_startup.LegacyMediaArchive",
                return_value=Archive(),
            ),
            mock.patch(
                "django_images.services.legacy_startup."
                "migration_state.transition_state",
                side_effect=transition,
            ),
            mock.patch(
                "django_images.services.migration_state._new_run_id",
                return_value=RUN_ID,
            ),
            mock.patch(
                "django_images.services.legacy_startup."
                "migration_state.seal_run_identities",
                side_effect=seal,
            ),
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5], patches[6], patches[7], patches[8], patches[9], \
                patches[10], patches[11]:
            run = coordinator.prepare_before_schema()
            events.extend(("collectstatic", "migrate"))
            coordinator.converge_after_schema(run)

        self.assertEqual(migration_state.read_run_status(run).phase, "complete")
        self.assertEqual(events, [
            "space",
            "state:snapshot_intent",
            "snapshot",
            "state:snapshot_complete",
            "collectstatic",
            "migrate",
            "state:schema_complete",
            "storage_configuration",
            "identity_seal",
            "media_plan",
            "space",
            "state:copying",
            "media_execute",
            "state:paths_complete",
            "identity_seal",
            "backfill_plan",
            "backfill_execute",
            "state:registry_complete",
            "identity_seal",
            "archive_prepare",
            "state:archive_intent",
            "archive_converge",
            "state:archive_intent",
            "state:archive_complete",
            "state:complete",
        ])
        summary_path = Path(run.path, "migration-summary.json")
        summary = json.loads(summary_path.read_text("utf-8"))
        self.assertEqual(summary["phase"], "complete")
        self.assertEqual(summary["reason_counts"], {"orphan": 1})
        self.assertEqual(oct(summary_path.stat().st_mode & 0o777), "0o600")

    def test_runtime_probe_failure_is_normalized(self):
        with mock.patch(
            "django_images.services.legacy_startup."
            "startup_preflight.validate_storage_runtime_preflight",
            return_value=PreflightResult(
                ok=False,
                reason_code="media_root_not_writable",
            ),
        ):
            with self.assertRaisesRegex(
                LegacyStartupError,
                "^media_root_not_writable$",
            ):
                self.coordinator().runtime_check(self.uid, self.gid)

    def _summary_run(self):
        self.backup_root.mkdir(mode=0o700, exist_ok=True)
        return migration_state.resolve_or_create_run(
            migration_state.scan_run_inventory(str(self.backup_root)),
            self.evidence(),
            False,
            "a" * 40,
            self.uid,
            self.gid,
            initial_space=SpaceBudget(300, 64, 364),
        )

    @staticmethod
    def _set_terminal_summaries(coordinator, run):
        coordinator._media_summary = AutoV2PlanSummary(
            run_id=run.run_id,
            plan_sha256="1" * 64,
            manifest_sha256="2" * 64,
            image_count=1,
            md5_legacy=1,
            fixed_slot=0,
            named_canonical=0,
            copy_required_bytes=1,
        )
        coordinator._backfill_summary = BackfillSummary(
            run_id=run.run_id,
            plan_sha256="3" * 64,
            manifest_sha256="4" * 64,
            scanned=1,
            eligible=1,
            registered=1,
            already_registered=0,
            skipped=0,
            reason_counts={},
        )

    def test_summary_rejects_symlink_hardlink_and_wrong_mode_target(self):
        mutators = (
            "symlink",
            "hardlink",
            "wrong_mode",
        )
        for mutator in mutators:
            with self.subTest(mutator=mutator):
                run = self._summary_run()
                summary = Path(run.path, "migration-summary.json")
                retained = summary.with_name(summary.name + ".retained")
                retained.write_text("retained", encoding="utf-8")
                os.chmod(str(retained), 0o600)
                if mutator == "symlink":
                    summary.symlink_to(retained)
                elif mutator == "hardlink":
                    os.link(str(retained), str(summary))
                else:
                    summary.write_text("wrong mode", encoding="utf-8")
                    os.chmod(str(summary), 0o644)

                with self.assertRaisesRegex(
                    LegacyStartupError,
                    "^unsafe_migration_summary$",
                ):
                    _atomic_write_summary(run, {"phase": "initialized"})

                if summary.is_symlink() or summary.exists():
                    summary.unlink()
                retained.unlink()
                migration_state.transition_state(
                    run,
                    "initialized",
                    "schema_complete",
                )
                migration_state.transition_state(
                    run,
                    "schema_complete",
                    "paths_complete",
                    plan_sha256="1" * 64,
                    manifest_sha256="2" * 64,
                )
                migration_state.transition_state(
                    run,
                    "paths_complete",
                    "registry_complete",
                    plan_sha256="3" * 64,
                    manifest_sha256="4" * 64,
                )
                migration_state.transition_state(
                    run,
                    "registry_complete",
                    "complete",
                )

    def test_summary_cleanup_never_unlinks_replacement_temp_inode(self):
        run = self._summary_run()
        temp_name = ".migration-summary.json.tmp-{}".format(
            "11111111-1111-4111-8111-111111111111"
        )
        temp_path = Path(run.path, temp_name)
        retained = temp_path.with_name(temp_path.name + ".retained")

        def swap_then_fail(*args, **kwargs):
            del args, kwargs
            temp_path.rename(retained)
            temp_path.write_bytes(b"replacement-sentinel")
            os.chmod(str(temp_path), 0o600)
            raise OSError("replace failed")

        with mock.patch(
            "django_images.services.legacy_startup.uuid.uuid4",
            return_value="11111111-1111-4111-8111-111111111111",
        ), mock.patch(
            "django_images.services.legacy_startup.os.replace",
            side_effect=swap_then_fail,
        ), self.assertRaisesRegex(
            LegacyStartupError,
            "^unsafe_migration_summary$",
        ):
            _atomic_write_summary(run, {"phase": "initialized"})

        self.assertEqual(temp_path.read_bytes(), b"replacement-sentinel")
        self.assertTrue(retained.exists())

    def test_summary_cleanup_removes_owned_temp_after_fchown_failure(self):
        run = self._summary_run()

        with mock.patch(
            "django_images.services.legacy_startup.os.fchown",
            side_effect=OSError("injected ownership failure"),
        ), self.assertRaisesRegex(
            LegacyStartupError,
            "^unsafe_migration_summary$",
        ):
            _atomic_write_summary(run, {"phase": "initialized"})

        self.assertEqual(
            list(Path(run.path).glob(".migration-summary.json.tmp-*")),
            [],
        )

    def test_summary_normalizes_source_commit_again_before_writing(self):
        source_sentinel = "https://source.invalid/private-user"
        self.backup_root.mkdir(mode=0o700)
        run = migration_state.resolve_or_create_run(
            migration_state.scan_run_inventory(str(self.backup_root)),
            self.evidence(),
            False,
            source_sentinel,
            self.uid,
            self.gid,
            initial_space=SpaceBudget(300, 64, 364),
        )

        self.coordinator()._write_summary(run)

        raw_summary = Path(
            run.path,
            "migration-summary.json",
        ).read_text("utf-8")
        self.assertNotIn(source_sentinel, raw_summary)
        self.assertEqual(
            json.loads(raw_summary)["source_commit"],
            "development",
        )

    def test_summary_rejects_non_integer_aggregate_count(self):
        run = self._summary_run()
        coordinator = self.coordinator()
        coordinator._media_summary = AutoV2PlanSummary(
            run_id=run.run_id,
            plan_sha256="1" * 64,
            manifest_sha256="2" * 64,
            image_count="private-filename.jpg",
            md5_legacy=0,
            fixed_slot=0,
            named_canonical=0,
            copy_required_bytes=0,
        )

        with self.assertRaisesRegex(
            LegacyStartupError,
            "^unsafe_migration_summary$",
        ):
            coordinator._write_summary(run)

        self.assertFalse(Path(run.path, "migration-summary.json").exists())

    def test_summary_accepts_shared_backfill_collision_reason_codes(self):
        run = self._summary_run()
        coordinator = self.coordinator()
        coordinator._backfill_summary = BackfillSummary(
            run_id=run.run_id,
            plan_sha256="1" * 64,
            manifest_sha256="2" * 64,
            scanned=3,
            eligible=0,
            registered=0,
            already_registered=0,
            skipped=3,
            reason_counts={
                "existing_registry_collision": 1,
                "duplicate_registry_collision": 2,
            },
        )

        coordinator._write_summary(run)

        summary = json.loads(
            Path(run.path, "migration-summary.json").read_text("utf-8")
        )
        self.assertEqual(summary["reason_counts"], {
            "duplicate_registry_collision": 2,
            "existing_registry_collision": 1,
        })

    def test_summary_restores_typed_counts_from_persisted_manifest_hashes(self):
        run = self._summary_run()
        migration_state.transition_state(
            run,
            "initialized",
            "schema_complete",
        )
        migration_state.transition_state(
            run,
            "schema_complete",
            "paths_complete",
            plan_sha256="1" * 64,
            manifest_sha256="2" * 64,
        )
        migration_state.transition_state(
            run,
            "paths_complete",
            "registry_complete",
            plan_sha256="3" * 64,
            manifest_sha256="4" * 64,
        )
        media = AutoV2PlanSummary(
            run_id=run.run_id,
            plan_sha256="1" * 64,
            manifest_sha256="2" * 64,
            image_count=7,
            md5_legacy=2,
            fixed_slot=3,
            named_canonical=2,
            copy_required_bytes=50,
        )
        backfill = BackfillSummary(
            run_id=run.run_id,
            plan_sha256="3" * 64,
            manifest_sha256="4" * 64,
            scanned=7,
            eligible=4,
            registered=4,
            already_registered=1,
            skipped=2,
            reason_counts={"duplicate_registry_collision": 2},
        )
        coordinator = self.coordinator()

        with mock.patch(
            "django_images.services.legacy_startup."
            "load_completed_auto_v2_summary",
            return_value=media,
            create=True,
        ) as load_media, mock.patch(
            "django_images.services.legacy_startup."
            "load_completed_media_asset_backfill_summary",
            return_value=backfill,
            create=True,
        ) as load_backfill:
            coordinator._write_summary(run)

        load_media.assert_called_once()
        load_backfill.assert_called_once()
        summary = json.loads(
            Path(run.path, "migration-summary.json").read_text("utf-8")
        )
        self.assertEqual(summary["media_image_count"], 7)
        self.assertEqual(summary["backfill_registered"], 4)
        self.assertEqual(
            summary["reason_counts"],
            {"duplicate_registry_collision": 2},
        )

    def test_summary_rejects_restored_manifest_hash_mismatch(self):
        run = self._summary_run()
        migration_state.transition_state(
            run,
            "initialized",
            "schema_complete",
        )
        migration_state.transition_state(
            run,
            "schema_complete",
            "paths_complete",
            plan_sha256="1" * 64,
            manifest_sha256="2" * 64,
        )
        mismatched = AutoV2PlanSummary(
            run_id=run.run_id,
            plan_sha256="1" * 64,
            manifest_sha256="9" * 64,
            image_count=1,
            md5_legacy=1,
            fixed_slot=0,
            named_canonical=0,
            copy_required_bytes=1,
        )

        with mock.patch(
            "django_images.services.legacy_startup."
            "load_completed_auto_v2_summary",
            return_value=mismatched,
            create=True,
        ), self.assertRaisesRegex(
            LegacyStartupError,
            "^migration_state_plan_mismatch$",
        ):
            self.coordinator()._write_summary(run)

    def test_complete_resume_repairs_missing_summary_from_typed_manifests(self):
        run = self._summary_run()
        migration_state.transition_state(
            run, "initialized", "schema_complete"
        )
        migration_state.transition_state(
            run,
            "schema_complete",
            "paths_complete",
            plan_sha256="1" * 64,
            manifest_sha256="2" * 64,
        )
        migration_state.transition_state(
            run,
            "paths_complete",
            "registry_complete",
            plan_sha256="3" * 64,
            manifest_sha256="4" * 64,
        )
        migration_state.transition_state(
            run,
            "registry_complete",
            "complete",
        )
        coordinator = self.coordinator()
        self._set_terminal_summaries(coordinator, run)
        media = coordinator._media_summary
        backfill = coordinator._backfill_summary
        coordinator._media_summary = None
        coordinator._backfill_summary = None

        with mock.patch(
            "django_images.services.legacy_startup."
            "startup_preflight.inspect_legacy_evidence",
            return_value=self.evidence(present=False),
        ), mock.patch(
            "django_images.services.legacy_startup."
            "startup_preflight.validate_storage_configuration_preflight",
            return_value=PreflightResult(ok=True),
        ), mock.patch(
            "django_images.services.legacy_startup."
            "load_completed_auto_v2_summary",
            return_value=media,
        ), mock.patch(
            "django_images.services.legacy_startup."
            "load_completed_media_asset_backfill_summary",
            return_value=backfill,
        ):
            coordinator.converge_after_schema(run)

        summary = json.loads(
            Path(run.path, "migration-summary.json").read_text("utf-8")
        )
        self.assertEqual(summary["phase"], "complete")
        self.assertEqual(summary["media_image_count"], 1)
        self.assertEqual(summary["backfill_registered"], 1)

    def test_resume_at_schema_complete_never_repeats_snapshot_or_first_space(self):
        run = self._summary_run()
        migration_state.transition_state(
            run,
            "initialized",
            "schema_complete",
        )
        evidence = self.evidence()
        coordinator = self.coordinator()

        with mock.patch(
            "django_images.services.legacy_startup."
            "startup_preflight.inspect_legacy_evidence",
            return_value=evidence,
        ), mock.patch(
            "django_images.services.legacy_startup."
            "startup_preflight.available_space_bytes",
        ) as available, mock.patch(
            "django_images.services.legacy_startup.snapshot_sqlite",
        ) as snapshot:
            resumed = coordinator.prepare_before_schema()

        self.assertEqual(resumed.run_id, run.run_id)
        self.assertFalse(coordinator.schema_required(resumed))
        available.assert_not_called()
        snapshot.assert_not_called()

    def test_media_root_identity_change_before_backfill_fails_closed(self):
        run = self._summary_run()
        migration_state.transition_state(
            run,
            "initialized",
            "schema_complete",
        )
        original = self.evidence()
        changed = LegacyEvidence(
            **dict(original.__dict__, media_root_identity={
                "device": 3,
                "inode": 999,
            })
        )

        def complete_media(current_run):
            migration_state.transition_state(
                current_run,
                "schema_complete",
                "paths_complete",
                plan_sha256="1" * 64,
                manifest_sha256="2" * 64,
            )

        coordinator = self.coordinator()
        with mock.patch(
            "django_images.services.legacy_startup."
            "startup_preflight.inspect_legacy_evidence",
            side_effect=(original, changed),
        ), mock.patch(
            "django_images.services.legacy_startup."
            "startup_preflight.validate_storage_configuration_preflight",
            return_value=PreflightResult(ok=True),
        ), mock.patch.object(
            coordinator,
            "_converge_media",
            side_effect=complete_media,
        ), mock.patch.object(
            coordinator,
            "_converge_backfill",
        ) as backfill, self.assertRaisesRegex(
            migration_state.MigrationStateError,
            "^migration_state_identity_changed$",
        ):
            coordinator.converge_after_schema(run)

        backfill.assert_not_called()

    def test_copying_torn_tail_resumes_without_resetting_completed_plan(self):
        run = self._summary_run()
        migration_state.transition_state(
            run,
            "initialized",
            "schema_complete",
        )
        migration_state.transition_state(
            run,
            "schema_complete",
            "copying",
            plan_sha256="1" * 64,
            manifest_sha256="2" * 64,
        )
        executed = AutoV2PlanSummary(
            run_id=run.run_id,
            plan_sha256="1" * 64,
            manifest_sha256="3" * 64,
            image_count=1,
            md5_legacy=1,
            fixed_slot=0,
            named_canonical=0,
            copy_required_bytes=10,
        )
        calls = []

        class Migrator(object):
            @staticmethod
            def run(execute=False):
                if not execute:
                    raise CommandError(
                        "media_manifest_torn_tail_requires_execute"
                    )
                return executed

            @staticmethod
            def recover_execution_tail():
                calls.append("recover_execution_tail")
                return executed

        coordinator = self.coordinator()
        with mock.patch(
            "django_images.services.legacy_startup.AutoV2MediaMigrator",
            return_value=Migrator(),
        ), mock.patch(
            "django_images.services.legacy_startup."
            "recover_incomplete_auto_v2_plan",
        ) as reset, mock.patch(
            "django_images.services.legacy_startup."
            "startup_preflight.calculate_remaining_space",
            return_value=SpaceBudget(10, 64, 74),
        ) as remaining, mock.patch(
            "django_images.services.legacy_startup."
            "startup_preflight.available_space_bytes",
            return_value=10_000,
        ):
            coordinator._converge_media(run)

        reset.assert_not_called()
        self.assertEqual(calls, ["recover_execution_tail"])
        remaining.assert_called_once_with(10, 64)
        self.assertEqual(
            migration_state.read_run_status(run).phase,
            "paths_complete",
        )

    def test_copying_phase_never_resets_an_incomplete_plan_prefix(self):
        run = self._summary_run()
        migration_state.transition_state(
            run,
            "initialized",
            "schema_complete",
        )
        migration_state.transition_state(
            run,
            "schema_complete",
            "copying",
            plan_sha256="1" * 64,
            manifest_sha256="2" * 64,
        )
        migrator = mock.Mock()
        migrator.run.side_effect = CommandError("auto_v2_plan_incomplete")

        with mock.patch(
            "django_images.services.legacy_startup.AutoV2MediaMigrator",
            return_value=migrator,
        ), mock.patch(
            "django_images.services.legacy_startup."
            "recover_incomplete_auto_v2_plan",
        ) as reset, self.assertRaisesRegex(
            CommandError,
            "^auto_v2_plan_incomplete$",
        ):
            self.coordinator()._converge_media(run)

        reset.assert_not_called()
        migrator.run.assert_called_once_with(execute=False)

    def test_backfill_torn_tail_resumes_without_resetting_completed_plan(self):
        run = self._summary_run()
        migration_state.transition_state(
            run,
            "initialized",
            "schema_complete",
        )
        migration_state.transition_state(
            run,
            "schema_complete",
            "paths_complete",
            plan_sha256="1" * 64,
            manifest_sha256="2" * 64,
        )
        migration_state.persist_manifest_identity(
            run,
            "paths_complete",
            "backfill",
            "3" * 64,
            "4" * 64,
        )
        executed = BackfillSummary(
            run_id=run.run_id,
            plan_sha256="3" * 64,
            manifest_sha256="5" * 64,
            scanned=1,
            eligible=1,
            registered=1,
            already_registered=0,
            skipped=0,
            reason_counts={},
        )

        class Backfiller(object):
            @staticmethod
            def run(execute=False):
                if not execute:
                    raise CommandError(
                        "media_asset_manifest_torn_tail_requires_execute"
                    )
                return executed

        coordinator = self.coordinator()
        self._set_terminal_summaries(coordinator, run)
        coordinator._backfill_summary = None
        with mock.patch(
            "django_images.services.legacy_startup.MediaAssetBackfiller",
            return_value=Backfiller(),
        ), mock.patch(
            "django_images.services.legacy_startup."
            "recover_incomplete_media_asset_plan",
        ) as reset:
            coordinator._converge_backfill(run)

        reset.assert_not_called()
        self.assertEqual(
            migration_state.read_run_status(run).phase,
            "registry_complete",
        )

    def test_authoritative_backfill_plan_never_resets_incomplete_prefix(self):
        run = self._summary_run()
        migration_state.transition_state(
            run,
            "initialized",
            "schema_complete",
        )
        migration_state.transition_state(
            run,
            "schema_complete",
            "paths_complete",
            plan_sha256="1" * 64,
            manifest_sha256="2" * 64,
        )
        migration_state.persist_manifest_identity(
            run,
            "paths_complete",
            "backfill",
            "3" * 64,
            "4" * 64,
        )
        backfiller = mock.Mock()
        backfiller.run.side_effect = CommandError(
            "media_asset_plan_incomplete"
        )

        with mock.patch(
            "django_images.services.legacy_startup.MediaAssetBackfiller",
            return_value=backfiller,
        ), mock.patch(
            "django_images.services.legacy_startup."
            "recover_incomplete_media_asset_plan",
        ) as reset, self.assertRaisesRegex(
            CommandError,
            "^media_asset_plan_incomplete$",
        ):
            self.coordinator()._converge_backfill(run)

        reset.assert_not_called()
        backfiller.run.assert_called_once_with(execute=False)

    def test_zero_archive_intents_complete_without_adapter_call(self):
        run = self._summary_run()
        migration_state.transition_state(
            run,
            "initialized",
            "schema_complete",
        )
        migration_state.transition_state(
            run,
            "schema_complete",
            "paths_complete",
            plan_sha256="1" * 64,
            manifest_sha256="2" * 64,
        )
        migration_state.transition_state(
            run,
            "paths_complete",
            "registry_complete",
            plan_sha256="3" * 64,
            manifest_sha256="4" * 64,
        )
        archive = mock.Mock()
        archive.prepare.return_value = SimpleNamespace(
            intents=(),
            progress=None,
        )
        evidence = self.evidence(present=False)

        coordinator = self.coordinator()
        self._set_terminal_summaries(coordinator, run)
        with mock.patch(
            "django_images.services.legacy_startup."
            "startup_preflight.inspect_legacy_evidence",
            return_value=evidence,
        ), mock.patch(
            "django_images.services.legacy_startup."
            "startup_preflight.validate_storage_configuration_preflight",
            return_value=PreflightResult(ok=True),
        ), mock.patch(
            "django_images.services.legacy_startup.LegacyMediaArchive",
            return_value=archive,
        ):
            coordinator.converge_after_schema(run)

        self.assertEqual(
            migration_state.read_run_status(run).phase,
            "complete",
        )
        archive.converge.assert_not_called()

    def test_complete_summary_failure_leaves_state_resumable(self):
        run = self._summary_run()
        migration_state.transition_state(
            run, "initialized", "schema_complete"
        )
        migration_state.transition_state(
            run,
            "schema_complete",
            "paths_complete",
            plan_sha256="1" * 64,
            manifest_sha256="2" * 64,
        )
        migration_state.transition_state(
            run,
            "paths_complete",
            "registry_complete",
            plan_sha256="3" * 64,
            manifest_sha256="4" * 64,
        )
        coordinator = self.coordinator()
        self._set_terminal_summaries(coordinator, run)
        archive = mock.Mock()
        archive.prepare.return_value = SimpleNamespace(
            intents=(),
            progress=None,
        )

        with mock.patch(
            "django_images.services.legacy_startup."
            "startup_preflight.inspect_legacy_evidence",
            return_value=self.evidence(present=False),
        ), mock.patch(
            "django_images.services.legacy_startup.LegacyMediaArchive",
            return_value=archive,
        ), mock.patch(
            "django_images.services.legacy_startup._atomic_write_summary",
            side_effect=LegacyStartupError("unsafe_migration_summary"),
        ), self.assertRaisesRegex(
            LegacyStartupError,
            "^unsafe_migration_summary$",
        ):
            coordinator._converge_archive(run)

        self.assertEqual(
            migration_state.read_run_status(run).phase,
            "registry_complete",
        )

    def test_complete_summary_is_durable_before_state_transition_fault(self):
        run = self._summary_run()
        migration_state.transition_state(
            run, "initialized", "schema_complete"
        )
        migration_state.transition_state(
            run,
            "schema_complete",
            "paths_complete",
            plan_sha256="1" * 64,
            manifest_sha256="2" * 64,
        )
        migration_state.transition_state(
            run,
            "paths_complete",
            "registry_complete",
            plan_sha256="3" * 64,
            manifest_sha256="4" * 64,
        )

        def crash(point):
            if point == "after_complete_summary":
                raise RuntimeError("simulated complete transition crash")

        coordinator = self.coordinator(fault_injector=crash)
        self._set_terminal_summaries(coordinator, run)
        archive = mock.Mock()
        archive.prepare.return_value = SimpleNamespace(
            intents=(),
            progress=None,
        )

        with mock.patch(
            "django_images.services.legacy_startup."
            "startup_preflight.inspect_legacy_evidence",
            return_value=self.evidence(present=False),
        ), mock.patch(
            "django_images.services.legacy_startup.LegacyMediaArchive",
            return_value=archive,
        ), self.assertRaisesRegex(
            RuntimeError,
            "^simulated complete transition crash$",
        ):
            coordinator._converge_archive(run)

        self.assertEqual(
            migration_state.read_run_status(run).phase,
            "registry_complete",
        )
        summary = json.loads(
            Path(run.path, "migration-summary.json").read_text("utf-8")
        )
        self.assertEqual(summary["phase"], "complete")

        resumed = self.coordinator()
        self._set_terminal_summaries(resumed, run)
        with mock.patch(
            "django_images.services.legacy_startup."
            "startup_preflight.inspect_legacy_evidence",
            return_value=self.evidence(present=False),
        ), mock.patch(
            "django_images.services.legacy_startup.LegacyMediaArchive",
            return_value=archive,
        ):
            resumed._converge_archive(run)

        self.assertEqual(
            migration_state.read_run_status(run).phase,
            "complete",
        )
