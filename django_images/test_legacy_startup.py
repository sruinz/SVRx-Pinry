import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace
import tempfile
from unittest import mock
from contextlib import nullcontext

from django.core.management import CommandError
from django.test import SimpleTestCase

from core.services.media_asset_backfill import BackfillSummary
from django_images.services import migration_state
from django_images.services.legacy_startup import (
    LegacyStartupCoordinator,
    LegacyStartupError,
    _atomic_write_summary,
)
from django_images.services.migration_batch_log import (
    JOURNAL_FILENAME,
    MigrationBatchLogError,
)
from django_images.services.media_migration_v2 import AutoV2PlanSummary
from django_images.services.startup_preflight import (
    LegacyEvidence,
    MissingUnreferencedImage,
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

    def coordinator(self, fault_injector=None, progress_reporter=None):
        return LegacyStartupCoordinator(
            self.uid,
            self.gid,
            fault_injector=fault_injector,
            progress_reporter=progress_reporter,
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

    def test_progress_events_require_the_exact_public_schema(self):
        reported = []
        coordinator = self.coordinator(
            progress_reporter=lambda event: reported.append(event) or False,
        )
        valid = {
            "phase": "planning",
            "run_id": RUN_ID,
            "attempt": 1,
            "resume_count": 0,
            "started_at": "2026-08-27T00:00:00Z",
            "images_total": 2,
            "files_total": 8,
            "backfill_total": 2,
        }

        self.assertTrue(coordinator._report_progress(valid))
        self.assertEqual(reported, [valid])
        with self.assertRaisesRegex(
            LegacyStartupError,
            "^legacy_progress_event_invalid$",
        ):
            coordinator._report_progress(dict(valid, secret_path="/data/a.png"))
        invalid_values = (
            dict(valid, run_id="/data/a.png"),
            dict(valid, attempt=True),
            dict(valid, images_total="/data/a.png"),
            dict(valid, started_at="Traceback /data/a.png"),
        )
        for event in invalid_values:
            with self.subTest(event=event), self.assertRaisesRegex(
                LegacyStartupError,
                "^legacy_progress_event_invalid$",
            ):
                coordinator._report_progress(event)
        with self.assertRaisesRegex(
            LegacyStartupError,
            "^legacy_progress_event_invalid$",
        ):
            coordinator._report_progress({
                "phase": "copying",
                "images_done": 2,
                "images_total": 1,
                "files_done": 0,
                "files_total": 0,
            })

        database = {
            "phase": "database",
            "images_done": 1,
            "images_total": 2,
            "last_committed_batch": 1,
        }
        upgrade = {
            "phase": "upgrade_v2",
            "images_done": 2,
            "images_total": 2,
            "last_committed_batch": 2,
        }
        self.assertTrue(coordinator._report_progress(database))
        self.assertTrue(coordinator._report_progress(upgrade))
        for event in (
            {key: value for key, value in database.items()
             if key != "last_committed_batch"},
            {key: value for key, value in upgrade.items()
             if key != "last_committed_batch"},
        ):
            with self.assertRaisesRegex(
                LegacyStartupError,
                "^legacy_progress_event_invalid$",
            ):
                coordinator._report_progress(event)

        self.assertEqual(reported, [valid, database, upgrade])

    def test_progress_events_reject_invalid_or_decreasing_commit_ordinal(self):
        coordinator = self.coordinator()
        base = {
            "phase": "database",
            "images_done": 1,
            "images_total": 2,
            "last_committed_batch": 2,
        }

        self.assertFalse(coordinator._report_progress(base))
        for ordinal in (True, "2", -1, 1):
            with self.subTest(ordinal=ordinal), self.assertRaisesRegex(
                LegacyStartupError,
                "^legacy_progress_event_invalid$",
            ):
                coordinator._report_progress(dict(
                    base,
                    last_committed_batch=ordinal,
                ))

    def test_child_finalizing_is_filtered_and_invalid_child_event_surfaces(self):
        reported = []
        coordinator = self.coordinator(progress_reporter=reported.append)

        self.assertFalse(
            coordinator._report_child_progress({"phase": "finalizing"})
        )
        self.assertEqual(reported, [])
        with self.assertRaisesRegex(
            LegacyStartupError,
            "^legacy_progress_event_invalid$",
        ):
            coordinator._report_child_progress({
                "phase": "copying",
                "images_done": 0,
            })

    def test_repeated_upgrade_progress_events_are_forwarded(self):
        reported = []
        coordinator = self.coordinator(progress_reporter=reported.append)
        events = (
            {
                "phase": "upgrade_v2",
                "images_done": 1,
                "images_total": 2,
                "last_committed_batch": 1,
            },
            {
                "phase": "upgrade_v2",
                "images_done": 2,
                "images_total": 2,
                "last_committed_batch": 2,
            },
        )

        for event in events:
            self.assertTrue(coordinator._report_child_progress(event))

        self.assertEqual(reported, list(events))

    def test_swallowed_child_reporter_error_is_raised_after_child_run(self):
        coordinator = self.coordinator()

        def child_operation():
            try:
                coordinator._report_child_progress({
                    "phase": "copying",
                    "images_done": 0,
                })
            except Exception:
                pass
            return mock.sentinel.result

        with self.assertRaisesRegex(
            LegacyStartupError,
            "^legacy_progress_event_invalid$",
        ):
            coordinator._run_child(child_operation)

    def test_internal_errors_are_reduced_to_safe_codes(self):
        run = self._summary_run()
        migration_state.transition_state(
            run,
            "initialized",
            "schema_complete",
        )
        cases = (
            (
                MigrationBatchLogError("linear_journal_source_changed"),
                "linear_journal_source_changed",
            ),
            (
                LegacyStartupError("/data/private.png"),
                "legacy_startup_failed",
            ),
            (ValueError("/data/private.png"), "legacy_startup_failed"),
        )
        for error, expected in cases:
            coordinator = self.coordinator()
            with self.subTest(expected=expected), mock.patch.object(
                coordinator,
                "_converge_media",
                side_effect=error,
            ), mock.patch.object(
                coordinator,
                "_configuration_preflight",
            ), mock.patch.object(
                coordinator,
                "_seal_current_identities",
            ), self.assertRaisesRegex(
                LegacyStartupError,
                "^{}$".format(expected),
            ):
                coordinator.converge_after_schema(run)

    def test_shutdown_signals_are_not_reduced(self):
        run = self._summary_run()
        migration_state.transition_state(
            run,
            "initialized",
            "schema_complete",
        )
        for error_type in (KeyboardInterrupt, SystemExit):
            coordinator = self.coordinator()
            with self.subTest(error_type=error_type), mock.patch.object(
                coordinator,
                "_converge_media",
                side_effect=error_type(),
            ), mock.patch.object(
                coordinator,
                "_configuration_preflight",
            ), mock.patch.object(
                coordinator,
                "_seal_current_identities",
            ), self.assertRaises(error_type):
                coordinator.converge_after_schema(run)

    def test_completed_run_skips_linear_journal_and_migrators(self):
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
            run, "registry_complete", "complete"
        )
        coordinator = self.coordinator()
        self._set_terminal_summaries(coordinator, run)
        coordinator._write_summary(run)

        with mock.patch.object(
            coordinator,
            "_configuration_preflight",
        ), mock.patch.object(
            coordinator,
            "_seal_current_identities",
        ), mock.patch(
            "django_images.services.legacy_startup."
            "MigrationBatchJournal.open",
        ) as opened, mock.patch(
            "django_images.services.legacy_startup.AutoV2MediaMigrator",
        ) as migrator:
            coordinator.converge_after_schema(run)

        opened.assert_not_called()
        migrator.assert_not_called()

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

    def test_full_run_uses_exact_phase_and_service_order(self):  # noqa: C901
        events = []
        progress_events = []
        candidate = MissingUnreferencedImage(
            image_id=1,
            image_path="originals/asset/old.png",
            thumbnail_rows=(),
        )
        evidence = LegacyEvidence(
            **dict(
                self.evidence().__dict__,
                missing_unreferenced_images=(candidate,),
            )
        )
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
        media_execute = media_dry
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
        backfill_execute = backfill_dry

        class Journal(object):
            def __init__(self):
                self.state = SimpleNamespace(
                    work_totals=None,
                    attempts=[],
                )
                self.closed = 0

            def record_attempt(self, started_at):
                events.append("journal_attempt")
                self.state.attempts.append(started_at)

            def freeze_work_totals(
                self, images_total, files_total, backfill_total
            ):
                events.append("journal_freeze")
                self.state.work_totals = {
                    "images_total": images_total,
                    "files_total": files_total,
                    "backfill_total": backfill_total,
                }

            def recovery_snapshot(self):
                totals = self.state.work_totals
                return {
                    "run_id": RUN_ID,
                    "attempt": len(self.state.attempts),
                    "resume_count": len(self.state.attempts) - 1,
                    "started_at": "2026-08-27T00:00:00Z",
                    "images_done": 0,
                    "images_total": totals["images_total"],
                    "files_done": 0,
                    "files_total": totals["files_total"],
                    "backfill_done": 0,
                    "backfill_total": totals["backfill_total"],
                    "last_committed_batch": 0,
                }

            def close(self):
                events.append("journal_close")
                self.closed += 1

        journal = Journal()

        class Migrator(object):
            def __init__(self, reporter):
                self.reporter = reporter
                self.batch_journal = None
                self._frozen_plans = (
                    SimpleNamespace(files=(1, 2)),
                    SimpleNamespace(files=(3, 4)),
                )

            def run(self, execute=False, upgrade_v2=False):
                events.append("media_execute" if execute else "media_plan")
                if execute:
                    self.assert_linear_contract(upgrade_v2)
                    self.reporter({
                        "phase": "copying",
                        "images_done": 2,
                        "images_total": 2,
                        "files_done": 4,
                        "files_total": 4,
                    })
                    self.reporter({
                        "phase": "database",
                        "images_done": 2,
                        "images_total": 2,
                        "last_committed_batch": 1,
                    })
                    self.reporter({"phase": "finalizing"})
                return media_execute if execute else media_dry

            def assert_linear_contract(self, upgrade_v2):
                if self.batch_journal is not journal or not upgrade_v2:
                    raise AssertionError("linear migrator contract")

        class Backfiller(object):
            def __init__(self, reporter, injected_journal):
                self.reporter = reporter
                self.batch_journal = injected_journal

            def count_planned_images(self):
                events.append("backfill_count")
                return 2

            def run(self, execute=False):
                events.append(
                    "backfill_execute" if execute else "backfill_plan"
                )
                if self.batch_journal is not journal:
                    raise AssertionError("backfill journal identity")
                if execute:
                    self.reporter({
                        "phase": "backfill_registering",
                        "backfill_done": 2,
                        "backfill_total": 2,
                        "last_committed_batch": 2,
                    })
                    self.reporter({"phase": "finalizing"})
                else:
                    self.reporter({
                        "phase": "backfill_planning",
                        "backfill_total": 2,
                    })
                return backfill_execute if execute else backfill_dry

        intents = (_Intent(10), _Intent(11))
        progress = {
            "items": [
                {"intent": intent.as_dict(), "complete": False}
                for intent in intents
            ]
        }

        authority = SimpleNamespace(summary=media_execute)

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

        coordinator = self.coordinator(
            progress_reporter=lambda event: (
                progress_events.append(event),
                events.append("progress:{}".format(event["phase"])),
            )[0],
        )
        backup_guard = mock.Mock()
        archive_evidence = LegacyEvidence(
            **dict(
                evidence.__dict__,
                has_md5_paths=False,
                has_fixed_slot_paths=False,
            )
        )
        evidence_results = iter((
            evidence,
            evidence,
            evidence,
            archive_evidence,
        ))

        def inspect_evidence(*args):
            del args
            events.append("inspect_evidence")
            return next(evidence_results)

        archive_factory = mock.Mock(return_value=Archive())
        patches = (
            mock.patch(
                "django_images.services.legacy_startup."
                "startup_preflight.inspect_legacy_evidence",
                side_effect=inspect_evidence,
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
                    or SimpleNamespace(
                        sha256="7" * 64,
                        source_device=1,
                        source_inode=2,
                    )
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
                side_effect=lambda *args, **kwargs: Migrator(
                    kwargs["progress_reporter"]
                ),
            ),
            mock.patch(
                "django_images.services.legacy_startup.MediaAssetBackfiller",
                side_effect=lambda *args, **kwargs: Backfiller(
                    kwargs["progress_reporter"],
                    kwargs["batch_journal"],
                ),
            ),
            mock.patch(
                "django_images.services.legacy_startup.LegacyMediaArchive",
                new=archive_factory,
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
            mock.patch(
                "django_images.services.legacy_startup."
                "load_completed_auto_v2_summary",
                side_effect=lambda *args, **kwargs: (
                    media_execute
                    if kwargs["batch_journal"] is journal
                    and kwargs["completion_authority"] is authority
                    else (_ for _ in ()).throw(
                        AssertionError("summary authority identity")
                    )
                ),
            ),
            mock.patch(
                "django_images.services.legacy_startup."
                "load_completed_media_asset_backfill_summary",
                side_effect=lambda *args, **kwargs: (
                    backfill_execute
                    if kwargs["batch_journal"] is journal
                    else (_ for _ in ()).throw(
                        AssertionError("summary journal identity")
                    )
                ),
            ),
            mock.patch(
                "django_images.services.legacy_startup."
                "protect_orphan_cleanup",
                return_value=nullcontext(backup_guard),
            ),
            mock.patch(
                "django_images.services.legacy_startup."
                "startup_preflight."
                "remove_confirmed_missing_unreferenced_images",
                side_effect=lambda *args: events.append("orphan_cleanup") or 1,
            ),
            mock.patch(
                "django_images.services.legacy_startup."
                "MigrationBatchJournal.open",
                side_effect=lambda *args, **kwargs: (
                    events.append("journal_open") or journal
                ),
            ),
            mock.patch(
                "django_images.services.legacy_startup."
                "AutoV2ManifestLog.open",
                return_value=nullcontext(SimpleNamespace()),
            ),
            mock.patch(
                "django_images.services.legacy_startup."
                "AutoV2CompletionAuthority.load",
                side_effect=lambda manifest, injected_journal: (
                    events.append("authority_load") or authority
                    if injected_journal is journal
                    else (_ for _ in ()).throw(
                        AssertionError("authority journal identity")
                    )
                ),
            ),
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5], patches[6], patches[7], patches[8], patches[9], \
                patches[10], patches[11], patches[12], patches[13], \
                patches[14], patches[15], patches[16], patches[17], \
                patches[18]:
            run = coordinator.prepare_before_schema()
            coordinator.prepare_migration_locks(run)
            events.extend(("collectstatic", "migrate"))
            coordinator.converge_after_schema(run)

        self.assertEqual(migration_state.read_run_status(run).phase, "complete")
        self.assertEqual(
            [event["phase"] for event in progress_events],
            [
                "preparing",
                "snapshot",
                "planning",
                "recovery",
                "copying",
                "database",
                "backfill_planning",
                "backfill_registering",
                "archive",
                "finalizing",
            ],
        )
        self.assertNotIn("progress:complete", events)
        self.assertLess(events.index("journal_open"), events.index(
            "journal_attempt"
        ))
        self.assertLess(events.index("journal_attempt"), events.index(
            "backfill_count"
        ))
        self.assertLess(events.index("backfill_count"), events.index(
            "journal_freeze"
        ))
        self.assertLess(events.index("journal_freeze"), events.index(
            "progress:planning"
        ))
        self.assertLess(events.index("progress:recovery"), events.index(
            "media_execute"
        ))
        self.assertLess(events.index("authority_load"), events.index(
            "archive_prepare"
        ))
        self.assertEqual(events.count("authority_load"), 1)
        self.assertNotIn(
            "inspect_evidence",
            events[events.index("authority_load") + 1:],
        )
        self.assertIs(
            archive_factory.call_args.kwargs["batch_journal"],
            journal,
        )
        self.assertIs(
            archive_factory.call_args.kwargs["completion_authority"],
            authority,
        )
        self.assertLess(events.index("archive_converge"), events.index(
            "progress:finalizing"
        ))
        self.assertEqual(journal.closed, 1)
        summary_path = Path(run.path, "migration-summary.json")
        summary = json.loads(summary_path.read_text("utf-8"))
        self.assertEqual(summary["phase"], "complete")
        self.assertEqual(summary["reason_counts"], {"orphan": 1})
        self.assertEqual(oct(summary_path.stat().st_mode & 0o777), "0o644")

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

    def test_prepare_migration_locks_uses_startup_identity(self):
        run = self._summary_run()
        coordinator = self.coordinator()
        startup_identity = (os.geteuid(), os.getegid())

        with mock.patch.object(
            coordinator,
            "_configuration_preflight",
        ) as configuration, mock.patch.object(
            coordinator,
            "_seal_current_identities",
        ) as seal:
            result = coordinator.prepare_migration_locks(run)

        self.assertIs(result, run)
        configuration.assert_called_once_with(*startup_identity)
        seal.assert_called_once_with(run)

    def test_prepare_migration_locks_creates_allowed_missing_media_layout(
        self,
    ):
        self.media_root.rmdir()
        coordinator = self.coordinator()
        coordinator._allow_missing_media_layout = True
        run = mock.sentinel.run

        with mock.patch.object(
            coordinator,
            "_seal_current_identities",
        ) as seal:
            result = coordinator.prepare_migration_locks(run)

        self.assertIs(result, run)
        self.assertTrue(self.media_root.is_dir())
        self.assertEqual(
            stat.S_IMODE(self.media_root.stat().st_mode),
            0o700,
        )
        seal.assert_called_once_with(run)

    def test_migration_keeps_startup_lock_identity_until_complete(self):
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
        service_identity = (self.uid + 1000, self.gid + 1000)
        startup_identity = (os.geteuid(), os.getegid())
        configuration_identities = []
        coordinator = LegacyStartupCoordinator(*service_identity)

        def configure(*args):
            configuration_identities.append(args[-2:])
            return PreflightResult(ok=True)

        def converge_media(current_run):
            self.assertEqual(
                configuration_identities[-1],
                startup_identity,
            )
            migration_state.transition_state(
                current_run,
                "paths_complete",
                "registry_complete",
                plan_sha256="3" * 64,
                manifest_sha256="4" * 64,
            )
            migration_state.transition_state(
                current_run,
                "registry_complete",
                "complete",
            )

        with mock.patch(
            "django_images.services.legacy_startup."
            "startup_preflight.validate_storage_configuration_preflight",
            side_effect=configure,
        ), mock.patch.object(
            coordinator,
            "_seal_current_identities",
        ), mock.patch.object(
            coordinator,
            "_converge_media",
            side_effect=converge_media,
        ):
            coordinator.converge_after_schema(run)

        self.assertEqual(
            configuration_identities,
            [startup_identity, service_identity],
        )

    def test_complete_resume_keeps_runtime_lock_identity_on_summary_error(
        self,
    ):
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
        migration_state.transition_state(
            run,
            "registry_complete",
            "complete",
        )
        service_identity = (self.uid + 1000, self.gid + 1000)
        configuration_identities = []
        coordinator = LegacyStartupCoordinator(*service_identity)

        def configure(*args):
            configuration_identities.append(args[-2:])
            return PreflightResult(ok=True)

        with mock.patch(
            "django_images.services.legacy_startup."
            "startup_preflight.validate_storage_configuration_preflight",
            side_effect=configure,
        ), mock.patch.object(
            coordinator,
            "_seal_current_identities",
        ) as seal, mock.patch.object(
            coordinator,
            "_read_completed_summary",
            side_effect=LegacyStartupError("unsafe_migration_summary"),
        ), self.assertRaisesRegex(
            LegacyStartupError,
            "^unsafe_migration_summary$",
        ):
            coordinator.converge_after_schema(run)

        self.assertEqual(
            configuration_identities,
            [service_identity],
        )
        seal.assert_not_called()

    def test_failed_migration_does_not_handoff_runtime_lock_identity(self):
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
        service_identity = (self.uid + 1000, self.gid + 1000)
        startup_identity = (os.geteuid(), os.getegid())
        configuration_identities = []
        coordinator = LegacyStartupCoordinator(*service_identity)

        def configure(*args):
            configuration_identities.append(args[-2:])
            return PreflightResult(ok=True)

        with mock.patch(
            "django_images.services.legacy_startup."
            "startup_preflight.validate_storage_configuration_preflight",
            side_effect=configure,
        ), mock.patch.object(
            coordinator,
            "_seal_current_identities",
        ), mock.patch.object(
            coordinator,
            "_converge_media",
            side_effect=LegacyStartupError("migration_state_plan_mismatch"),
        ), self.assertRaisesRegex(
            LegacyStartupError,
            "^migration_state_plan_mismatch$",
        ):
            coordinator.converge_after_schema(run)

        self.assertEqual(
            configuration_identities,
            [startup_identity],
        )

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

    def _completed_run_with_summary(self):
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
        coordinator._write_summary(run)
        summary_path = Path(run.path, "migration-summary.json")
        os.chmod(str(summary_path), 0o644)
        return run, summary_path

    def test_completed_separate_start_uses_only_durable_summary(self):
        run, summary_path = self._completed_run_with_summary()
        original_summary = summary_path.read_bytes()
        coordinator = self.coordinator()

        with mock.patch(
            "django_images.services.legacy_startup."
            "startup_preflight.inspect_legacy_evidence",
            side_effect=AssertionError("completed run legacy scan"),
        ) as inspect_evidence, mock.patch(
            "django_images.services.legacy_startup."
            "AutoV2ManifestLog.open",
            side_effect=AssertionError("completed run manifest open"),
        ) as manifest_open, mock.patch(
            "django_images.services.legacy_startup."
            "MigrationBatchJournal.open",
            side_effect=AssertionError("completed run journal open"),
        ) as journal_open, mock.patch(
            "django_images.services.legacy_startup."
            "load_completed_auto_v2_summary",
            side_effect=AssertionError("completed run media reload"),
        ) as load_media, mock.patch(
            "django_images.services.legacy_startup."
            "load_completed_media_asset_backfill_summary",
            side_effect=AssertionError("completed run backfill reload"),
        ) as load_backfill, mock.patch.object(
            coordinator,
            "_seal_current_identities",
            side_effect=AssertionError("completed run legacy seal"),
        ) as seal, mock.patch(
            "django_images.services.legacy_startup."
            "startup_preflight.validate_storage_configuration_preflight",
            return_value=PreflightResult(ok=True),
        ):
            resumed = coordinator.prepare_before_schema()
            result = coordinator.converge_after_schema(resumed)

        self.assertEqual(result.run_id, run.run_id)
        self.assertEqual(summary_path.read_bytes(), original_summary)
        self.assertEqual(stat.S_IMODE(summary_path.stat().st_mode), 0o644)
        self.assertEqual(summary_path.stat().st_uid, os.geteuid())
        self.assertEqual(summary_path.stat().st_gid, os.getegid())
        inspect_evidence.assert_not_called()
        manifest_open.assert_not_called()
        journal_open.assert_not_called()
        load_media.assert_not_called()
        load_backfill.assert_not_called()
        seal.assert_not_called()

    def test_completed_separate_start_rejects_tampered_summary(self):
        run, summary_path = self._completed_run_with_summary()
        payload = json.loads(summary_path.read_text("utf-8"))
        payload["unexpected"] = True
        summary_path.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":"))
            + "\n",
            encoding="utf-8",
        )
        os.chmod(str(summary_path), 0o644)
        coordinator = self.coordinator()

        with mock.patch(
            "django_images.services.legacy_startup."
            "startup_preflight.inspect_legacy_evidence",
            side_effect=AssertionError("completed run legacy scan"),
        ), mock.patch(
            "django_images.services.legacy_startup."
            "startup_preflight.validate_storage_configuration_preflight",
            return_value=PreflightResult(ok=True),
        ), mock.patch.object(
            coordinator,
            "_seal_current_identities",
            side_effect=AssertionError("completed run legacy seal"),
        ), self.assertRaisesRegex(
            LegacyStartupError,
            "^unsafe_migration_summary$",
        ):
            resumed = coordinator.prepare_before_schema()
            coordinator.converge_after_schema(resumed)

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
                    os.chmod(str(summary), 0o600)

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

    def test_transition_complete_uses_prefrozen_terminal_objects_only(self):
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
        media_summary = coordinator._media_summary
        backfill_summary = coordinator._backfill_summary
        journal = mock.sentinel.journal
        authority = SimpleNamespace(summary=media_summary)

        with mock.patch(
            "django_images.services.legacy_startup."
            "load_completed_auto_v2_summary",
            side_effect=AssertionError("post-archive media reload"),
        ) as load_media, mock.patch(
            "django_images.services.legacy_startup."
            "load_completed_media_asset_backfill_summary",
            side_effect=AssertionError("post-archive backfill reload"),
        ) as load_backfill, mock.patch(
            "django_images.services.legacy_startup."
            "MigrationBatchJournal.open",
            side_effect=AssertionError("post-archive journal reopen"),
        ) as journal_open, mock.patch(
            "django_images.services.legacy_startup."
            "AutoV2ManifestLog.open",
            side_effect=AssertionError("post-archive manifest reopen"),
        ) as manifest_open, mock.patch(
            "django_images.services.legacy_startup."
            "startup_preflight.inspect_legacy_evidence",
            side_effect=AssertionError("post-archive legacy root scan"),
        ) as inspect_evidence:
            coordinator._transition_complete(
                run,
                "registry_complete",
                journal,
                completion_authority=authority,
            )

        load_media.assert_not_called()
        load_backfill.assert_not_called()
        journal_open.assert_not_called()
        manifest_open.assert_not_called()
        inspect_evidence.assert_not_called()
        self.assertIs(coordinator._media_summary, media_summary)
        self.assertIs(coordinator._backfill_summary, backfill_summary)
        self.assertIs(authority.summary, media_summary)

    def test_transition_complete_rejects_missing_prefrozen_backfill_summary(self):
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
        media_summary = coordinator._media_summary
        coordinator._backfill_summary = None
        journal = mock.sentinel.journal
        authority = SimpleNamespace(summary=media_summary)

        with mock.patch(
            "django_images.services.legacy_startup."
            "load_completed_auto_v2_summary",
            side_effect=AssertionError("post-archive media reload"),
        ) as load_media, mock.patch(
            "django_images.services.legacy_startup."
            "load_completed_media_asset_backfill_summary",
            side_effect=AssertionError("post-archive backfill reload"),
        ) as load_backfill, mock.patch(
            "django_images.services.legacy_startup."
            "MigrationBatchJournal.open",
            side_effect=AssertionError("post-archive journal reopen"),
        ) as journal_open, mock.patch(
            "django_images.services.legacy_startup."
            "startup_preflight.inspect_legacy_evidence",
            side_effect=AssertionError("post-archive legacy root scan"),
        ) as inspect_evidence, self.assertRaisesRegex(
            LegacyStartupError,
            "^migration_state_plan_mismatch$",
        ):
            coordinator._transition_complete(
                run,
                "registry_complete",
                journal,
                completion_authority=authority,
            )

        load_media.assert_not_called()
        load_backfill.assert_not_called()
        journal_open.assert_not_called()
        inspect_evidence.assert_not_called()
        self.assertEqual(
            migration_state.read_run_status(run).phase,
            "registry_complete",
        )

    def test_completed_separate_start_rejects_missing_summary(self):
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
            side_effect=AssertionError("completed run legacy scan"),
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
        ), mock.patch.object(
            coordinator,
            "_seal_current_identities",
            side_effect=AssertionError("completed run legacy seal"),
        ), self.assertRaisesRegex(
            LegacyStartupError,
            "^unsafe_migration_summary$",
        ):
            resumed = coordinator.prepare_before_schema()
            coordinator.converge_after_schema(resumed)

        self.assertFalse(Path(run.path, "migration-summary.json").exists())

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

    def test_schema_complete_resume_cleans_orphan_before_media_plan(self):
        run = self._summary_run()
        migration_state.transition_state(
            run,
            "initialized",
            "schema_complete",
        )
        candidate = MissingUnreferencedImage(
            image_id=1,
            image_path="originals/asset/old.png",
            thumbnail_rows=(),
        )
        evidence = LegacyEvidence(
            **dict(
                self.evidence().__dict__,
                missing_unreferenced_images=(candidate,),
            )
        )
        coordinator = self.coordinator()
        backup_guard = mock.Mock()

        with mock.patch(
            "django_images.services.legacy_startup."
            "startup_preflight.inspect_legacy_evidence",
            return_value=evidence,
        ), mock.patch(
            "django_images.services.legacy_startup."
            "protect_orphan_cleanup",
            return_value=nullcontext(backup_guard),
        ) as protect_cleanup, mock.patch(
            "django_images.services.legacy_startup."
            "startup_preflight.remove_confirmed_missing_unreferenced_images",
            return_value=1,
        ) as cleanup:
            resumed = coordinator.prepare_before_schema()

        self.assertEqual(resumed.run_id, run.run_id)
        protect_cleanup.assert_called_once_with(
            str(self.database_path),
            run,
            self.uid,
            self.gid,
            before_fallback_create=(
                coordinator._require_cleanup_snapshot_space
            ),
        )
        cleanup.assert_called_once_with(
            str(self.database_path),
            str(self.media_root),
            (candidate,),
            {"device": 1, "inode": 2},
            {"device": 3, "inode": 4},
            backup_guard,
        )

    def test_schema_complete_resume_never_cleans_after_manifest_event(self):
        run = self._summary_run()
        migration_state.transition_state(
            run,
            "initialized",
            "schema_complete",
        )
        Path(run.path, "media-migration.jsonl").write_bytes(b"event\n")
        candidate = MissingUnreferencedImage(
            image_id=1,
            image_path="originals/asset/old.png",
            thumbnail_rows=(),
        )
        evidence = LegacyEvidence(
            **dict(
                self.evidence().__dict__,
                missing_unreferenced_images=(candidate,),
            )
        )
        coordinator = self.coordinator()

        with mock.patch(
            "django_images.services.legacy_startup."
            "startup_preflight.inspect_legacy_evidence",
            return_value=evidence,
        ), mock.patch(
            "django_images.services.legacy_startup."
            "startup_preflight."
            "remove_confirmed_missing_unreferenced_images",
        ) as cleanup:
            resumed = coordinator.prepare_before_schema()

        self.assertEqual(resumed.run_id, run.run_id)
        cleanup.assert_not_called()

    def test_snapshot_complete_resume_never_cleans_after_manifest_creation(self):
        run = self._summary_run()
        migration_state.transition_state(
            run,
            "initialized",
            "snapshot_intent",
            intent={
                "kind": "sqlite_snapshot",
                "source_device": 1,
                "source_inode": 2,
            },
        )
        migration_state.transition_state(
            run,
            "snapshot_intent",
            "snapshot_complete",
        )
        Path(run.path, "media-migration.jsonl").write_bytes(b"")
        candidate = MissingUnreferencedImage(
            image_id=1,
            image_path="originals/asset/old.png",
            thumbnail_rows=(),
        )
        evidence = LegacyEvidence(
            **dict(
                self.evidence().__dict__,
                missing_unreferenced_images=(candidate,),
            )
        )
        coordinator = self.coordinator()

        with mock.patch(
            "django_images.services.legacy_startup."
            "startup_preflight.inspect_legacy_evidence",
            return_value=evidence,
        ), mock.patch(
            "django_images.services.legacy_startup."
            "protect_orphan_cleanup",
        ) as protect_cleanup, mock.patch(
            "django_images.services.legacy_startup."
            "startup_preflight.remove_confirmed_missing_unreferenced_images",
        ) as cleanup:
            resumed = coordinator.prepare_before_schema()

        self.assertEqual(resumed.run_id, run.run_id)
        protect_cleanup.assert_not_called()
        cleanup.assert_not_called()

    def test_media_root_identity_change_during_migration_fails_closed(self):
        run = self._summary_run()
        migration_state.transition_state(
            run,
            "initialized",
            "schema_complete",
        )
        coordinator = self.coordinator()
        with mock.patch(
            "django_images.services.legacy_startup."
            "startup_preflight.validate_storage_configuration_preflight",
            return_value=PreflightResult(ok=True),
        ), mock.patch.object(
            coordinator,
            "_converge_media",
            side_effect=migration_state.MigrationStateError(
                "migration_state_identity_changed"
            ),
        ), mock.patch.object(
            coordinator,
            "_seal_current_identities",
        ), self.assertRaisesRegex(
            LegacyStartupError,
            "^migration_state_identity_changed$",
        ):
            coordinator.converge_after_schema(run)

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
            _frozen_plans = (SimpleNamespace(files=(1,)),)

            @staticmethod
            def run(execute=False, upgrade_v2=False):
                if not execute:
                    raise CommandError(
                        "media_manifest_torn_tail_requires_execute"
                    )
                if not upgrade_v2:
                    raise AssertionError("upgrade_v2 contract")
                return executed

            @staticmethod
            def recover_execution_tail():
                calls.append("recover_execution_tail")
                return executed

        progress_events = []
        journal = mock.Mock()
        journal.state.work_totals = {
            "images_total": 1,
            "files_total": 1,
            "backfill_total": 1,
        }
        journal.recovery_snapshot.return_value = {
            "run_id": run.run_id,
            "attempt": 2,
            "resume_count": 1,
            "started_at": "2026-08-27T00:00:00Z",
            "images_done": 0,
            "images_total": 1,
            "files_done": 0,
            "files_total": 1,
            "backfill_done": 0,
            "backfill_total": 1,
            "last_committed_batch": 0,
        }
        authority = mock.sentinel.authority
        coordinator = self.coordinator(
            progress_reporter=progress_events.append,
        )
        with mock.patch(
            "django_images.services.legacy_startup.AutoV2MediaMigrator",
            return_value=Migrator(),
        ), mock.patch(
            "django_images.services.legacy_startup.MediaAssetBackfiller",
        ) as backfiller_factory, mock.patch(
            "django_images.services.legacy_startup."
            "MigrationBatchJournal.open",
            return_value=journal,
        ) as journal_open, mock.patch.object(
            coordinator,
            "_converge_backfill",
        ), mock.patch.object(
            coordinator,
            "_seal_current_identities",
            return_value=self.evidence(present=False),
        ), mock.patch.object(
            coordinator,
            "_load_completion_authority",
            return_value=authority,
        ), mock.patch.object(
            coordinator,
            "_converge_archive",
            return_value="paths_complete",
        ), mock.patch.object(
            coordinator,
            "_transition_complete",
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
        journal.freeze_work_totals.assert_not_called()
        journal_open.assert_called_once()
        self.assertFalse(journal_open.call_args.kwargs["create"])
        backfiller_factory.return_value.count_planned_images.assert_not_called()
        self.assertEqual(calls, ["recover_execution_tail"])
        remaining.assert_called_once_with(10, 64)
        self.assertEqual(
            [event["phase"] for event in progress_events],
            ["recovery", "finalizing"],
        )
        journal.close.assert_called_once_with()
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

    def test_restart_without_frozen_totals_fails_before_recount(self):
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
        planned = AutoV2PlanSummary(
            run_id=run.run_id,
            plan_sha256="1" * 64,
            manifest_sha256="2" * 64,
            image_count=1,
            md5_legacy=1,
            fixed_slot=0,
            named_canonical=0,
            copy_required_bytes=10,
        )
        migrator = mock.Mock()
        migrator.run.return_value = planned
        migrator._frozen_plans = (SimpleNamespace(files=(1,)),)
        journal = mock.Mock()
        journal.state.work_totals = None
        backfiller = mock.Mock()
        run_directory = mock.Mock()

        with mock.patch(
            "django_images.services.legacy_startup.AutoV2MediaMigrator",
            return_value=migrator,
        ), mock.patch(
            "django_images.services.legacy_startup.MediaAssetBackfiller",
            return_value=backfiller,
        ), mock.patch(
            "django_images.services.legacy_startup."
            "MigrationBatchJournal.open",
            return_value=journal,
        ), mock.patch(
            "django_images.services.legacy_startup."
            "file_ops.open_verified_media_root",
            return_value=run_directory,
        ), mock.patch(
            "django_images.services.legacy_startup."
            "startup_preflight.calculate_remaining_space",
            return_value=SpaceBudget(10, 64, 74),
        ), mock.patch(
            "django_images.services.legacy_startup."
            "startup_preflight.available_space_bytes",
            return_value=10_000,
        ), self.assertRaisesRegex(
            MigrationBatchLogError,
            "^linear_journal_invalid$",
        ):
            self.coordinator()._converge_media(run)

        backfiller.count_planned_images.assert_not_called()
        journal.freeze_work_totals.assert_not_called()
        journal.close.assert_called_once_with()
        run_directory.close.assert_called_once_with()

    def test_restart_without_journal_does_not_create_replacement_sidecar(self):
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
        planned = AutoV2PlanSummary(
            run_id=run.run_id,
            plan_sha256="1" * 64,
            manifest_sha256="2" * 64,
            image_count=1,
            md5_legacy=1,
            fixed_slot=0,
            named_canonical=0,
            copy_required_bytes=10,
        )
        migrator = mock.Mock()
        migrator.run.return_value = planned
        journal_path = Path(run.path, JOURNAL_FILENAME)

        with mock.patch(
            "django_images.services.legacy_startup.AutoV2MediaMigrator",
            return_value=migrator,
        ), mock.patch(
            "django_images.services.legacy_startup."
            "startup_preflight.calculate_remaining_space",
            return_value=SpaceBudget(10, 64, 74),
        ), mock.patch(
            "django_images.services.legacy_startup."
            "startup_preflight.available_space_bytes",
            return_value=10_000,
        ), self.assertRaisesRegex(
            MigrationBatchLogError,
            "^linear_journal_missing$",
        ):
            self.coordinator()._converge_media(run)

        self.assertFalse(journal_path.exists())
        migrator.run.assert_called_once_with(execute=False)

    def test_every_restart_phase_reuses_one_journal_and_authority(self):
        phases = (
            "copying",
            "paths_complete",
            "registry_complete",
            "archive_intent",
            "archive_complete",
        )
        for phase in phases:
            with self.subTest(phase=phase):
                run_path = Path(self.temporary.name, phase)
                run_path.mkdir(mode=0o700)
                run = SimpleNamespace(path=str(run_path), run_id=RUN_ID)
                status = SimpleNamespace(
                    phase=phase,
                    media_plan_sha256="1" * 64,
                    media_manifest_sha256="2" * 64,
                    initial_margin_bytes=64,
                )
                planned = AutoV2PlanSummary(
                    run_id=run.run_id,
                    plan_sha256="1" * 64,
                    manifest_sha256="2" * 64,
                    image_count=1,
                    md5_legacy=1,
                    fixed_slot=0,
                    named_canonical=0,
                    copy_required_bytes=10,
                )
                migrator = mock.Mock()
                migrator.run.return_value = planned
                journal = mock.Mock()
                journal.state.work_totals = {
                    "images_total": 1,
                    "files_total": 1,
                    "backfill_total": 1,
                }
                journal.recovery_snapshot.return_value = {
                    "run_id": run.run_id,
                    "attempt": 2,
                    "resume_count": 1,
                    "started_at": "2026-08-27T00:00:00Z",
                    "images_done": 1,
                    "images_total": 1,
                    "files_done": 1,
                    "files_total": 1,
                    "backfill_done": 1,
                    "backfill_total": 1,
                    "last_committed_batch": 1,
                }
                backfiller = mock.Mock()
                authority = mock.sentinel.authority
                archive_evidence = mock.sentinel.archive_evidence
                progress_events = []
                coordinator = self.coordinator(
                    progress_reporter=progress_events.append,
                )

                with mock.patch(
                    "django_images.services.legacy_startup."
                    "migration_state.read_run_status",
                    return_value=status,
                ), mock.patch(
                    "django_images.services.legacy_startup."
                    "migration_state.persist_manifest_identity",
                ), mock.patch(
                    "django_images.services.legacy_startup."
                    "AutoV2MediaMigrator",
                    return_value=migrator,
                ), mock.patch(
                    "django_images.services.legacy_startup."
                    "MediaAssetBackfiller",
                    return_value=backfiller,
                ), mock.patch(
                    "django_images.services.legacy_startup."
                    "MigrationBatchJournal.open",
                    return_value=journal,
                ) as opened, mock.patch.object(
                    coordinator,
                    "_converge_backfill",
                ) as converge_backfill, mock.patch.object(
                    coordinator,
                    "_seal_current_identities",
                    return_value=archive_evidence,
                ), mock.patch.object(
                    coordinator,
                    "_load_completion_authority",
                    return_value=authority,
                ) as load_authority, mock.patch.object(
                    coordinator,
                    "_converge_archive",
                    return_value=phase,
                ) as converge_archive, mock.patch.object(
                    coordinator,
                    "_transition_complete",
                ) as transition_complete, mock.patch.object(
                    coordinator,
                    "_transition",
                ), mock.patch(
                    "django_images.services.legacy_startup."
                    "startup_preflight.calculate_remaining_space",
                    return_value=SpaceBudget(10, 64, 74),
                ), mock.patch(
                    "django_images.services.legacy_startup."
                    "startup_preflight.available_space_bytes",
                    return_value=10_000,
                ):
                    coordinator._converge_media(run)

                opened.assert_called_once()
                self.assertFalse(opened.call_args.kwargs["create"])
                journal.record_attempt.assert_called_once()
                journal.freeze_work_totals.assert_not_called()
                backfiller.count_planned_images.assert_not_called()
                self.assertIs(migrator.batch_journal, journal)
                converge_backfill.assert_called_once_with(
                    run, journal, backfiller
                )
                load_authority.assert_called_once_with(run, journal)
                converge_archive.assert_called_once_with(
                    run, journal, authority, archive_evidence
                )
                transition_complete.assert_called_once_with(
                    run,
                    phase,
                    journal,
                    completion_authority=authority,
                )
                journal.close.assert_called_once_with()
                self.assertEqual(
                    [event["phase"] for event in progress_events],
                    ["recovery", "finalizing"],
                )

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
        backfiller = Backfiller()
        with mock.patch(
            "django_images.services.legacy_startup."
            "recover_incomplete_media_asset_plan",
        ) as reset:
            coordinator._converge_backfill(
                run,
                mock.sentinel.journal,
                backfiller,
            )

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
            "django_images.services.legacy_startup."
            "recover_incomplete_media_asset_plan",
        ) as reset, self.assertRaisesRegex(
            CommandError,
            "^media_asset_plan_incomplete$",
        ):
            self.coordinator()._converge_backfill(
                run,
                mock.sentinel.journal,
                backfiller,
            )

        reset.assert_not_called()
        backfiller.run.assert_called_once_with(execute=False)

    def test_zero_archive_intents_complete_without_adapter_call(self):
        progress_events = []
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

        coordinator = self.coordinator(
            progress_reporter=progress_events.append,
        )
        journal = mock.sentinel.journal
        authority = mock.sentinel.authority
        with mock.patch(
            "django_images.services.legacy_startup.LegacyMediaArchive",
            return_value=archive,
        ):
            expected = coordinator._converge_archive(
                run,
                journal,
                authority,
                evidence,
            )

        self.assertEqual(
            migration_state.read_run_status(run).phase,
            "registry_complete",
        )
        self.assertEqual(expected, "registry_complete")
        archive.converge.assert_not_called()
        self.assertEqual(
            archive.prepare.call_args.kwargs["expected_plan_sha256"],
            "1" * 64,
        )
        self.assertEqual(
            archive.prepare.call_args.kwargs["expected_manifest_sha256"],
            "2" * 64,
        )
        self.assertEqual(
            [event["phase"] for event in progress_events],
            [],
        )

    def test_archive_complete_resume_revalidates_archived_media(self):
        progress_events = []
        run = SimpleNamespace(path="/backup/run", run_id=RUN_ID)
        progress = {
            "items": [{
                "intent": _Intent(10).as_dict(),
                "complete": True,
            }],
        }
        status = SimpleNamespace(
            phase="archive_complete",
            progress=progress,
            media_plan_sha256="1" * 64,
            media_manifest_sha256="2" * 64,
        )
        plan = SimpleNamespace(intents=(_Intent(10),), progress=progress)
        archive = mock.Mock()
        archive.prepare.return_value = plan
        coordinator = self.coordinator(
            progress_reporter=progress_events.append,
        )
        journal = mock.sentinel.journal
        authority = mock.sentinel.authority
        evidence = self.evidence(present=False)
        archive_factory = mock.Mock(return_value=archive)

        with mock.patch(
            "django_images.services.legacy_startup."
            "migration_state.read_run_status",
            return_value=status,
        ), mock.patch(
            "django_images.services.legacy_startup.LegacyMediaArchive",
            new=archive_factory,
        ):
            expected = coordinator._converge_archive(
                run,
                journal,
                authority,
                evidence,
            )

        archive.prepare.assert_called_once_with(
            progress=progress,
            expected_plan_sha256="1" * 64,
            expected_manifest_sha256="2" * 64,
        )
        archive.converge.assert_called_once_with(
            plan,
            syscall_adapter=coordinator.archive_adapter,
        )
        self.assertEqual(expected, "archive_complete")
        self.assertIs(
            archive_factory.call_args.kwargs["batch_journal"],
            journal,
        )
        self.assertIs(
            archive_factory.call_args.kwargs["completion_authority"],
            authority,
        )
        self.assertEqual(progress_events, [])

    def test_legacy_archive_complete_recovers_fixed_only_archive(self):
        run = SimpleNamespace(path="/backup/run", run_id=RUN_ID)
        progress = {
            "items": [{
                "intent": _Intent(10).as_dict(),
                "complete": True,
            }],
        }
        status = SimpleNamespace(
            phase="archive_complete",
            progress=None,
            media_plan_sha256="1" * 64,
            media_manifest_sha256="2" * 64,
        )
        archive = mock.Mock()
        recovered = SimpleNamespace(
            intents=(_Intent(10),), progress=progress
        )
        archive.prepare.return_value = recovered
        coordinator = self.coordinator()
        journal = mock.sentinel.journal
        authority = mock.sentinel.authority
        evidence = self.evidence(present=False)

        with mock.patch(
            "django_images.services.legacy_startup."
            "migration_state.read_run_status",
            return_value=status,
        ), mock.patch(
            "django_images.services.legacy_startup.LegacyMediaArchive",
            return_value=archive,
        ):
            expected = coordinator._converge_archive(
                run,
                journal,
                authority,
                evidence,
            )

        archive.prepare.assert_called_once_with(
            recover_completed_fixed=True,
            expected_plan_sha256="1" * 64,
            expected_manifest_sha256="2" * 64,
        )
        archive.converge.assert_called_once_with(
            recovered,
            syscall_adapter=coordinator.archive_adapter,
        )
        self.assertEqual(expected, "archive_complete")

    def test_archive_rejects_fresh_legacy_database_references(self):
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
        archive_factory = mock.Mock()
        with mock.patch(
            "django_images.services.legacy_startup.LegacyMediaArchive",
            new=archive_factory,
        ), self.assertRaisesRegex(
            LegacyStartupError,
            "^legacy_media_still_referenced$",
        ):
            self.coordinator()._converge_archive(
                run,
                mock.sentinel.journal,
                mock.sentinel.authority,
                self.evidence(present=True),
            )

        archive_factory.assert_not_called()

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
        journal = mock.sentinel.journal
        authority = SimpleNamespace(summary=coordinator._media_summary)
        evidence = self.evidence(present=False)

        with mock.patch(
            "django_images.services.legacy_startup.LegacyMediaArchive",
            return_value=archive,
        ), mock.patch(
            "django_images.services.legacy_startup."
            "load_completed_auto_v2_summary",
            return_value=coordinator._media_summary,
        ), mock.patch(
            "django_images.services.legacy_startup."
            "load_completed_media_asset_backfill_summary",
            return_value=coordinator._backfill_summary,
        ), mock.patch(
            "django_images.services.legacy_startup._atomic_write_summary",
            side_effect=LegacyStartupError("unsafe_migration_summary"),
        ), self.assertRaisesRegex(
            LegacyStartupError,
            "^unsafe_migration_summary$",
        ):
            expected = coordinator._converge_archive(
                run,
                journal,
                authority,
                evidence,
            )
            coordinator._transition_complete(
                run,
                expected,
                journal,
                completion_authority=authority,
            )

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
        journal = mock.sentinel.journal
        authority = SimpleNamespace(summary=coordinator._media_summary)
        evidence = self.evidence(present=False)

        with mock.patch(
            "django_images.services.legacy_startup.LegacyMediaArchive",
            return_value=archive,
        ), mock.patch(
            "django_images.services.legacy_startup."
            "load_completed_auto_v2_summary",
            return_value=coordinator._media_summary,
        ), mock.patch(
            "django_images.services.legacy_startup."
            "load_completed_media_asset_backfill_summary",
            return_value=coordinator._backfill_summary,
        ), self.assertRaisesRegex(
            RuntimeError,
            "^simulated complete transition crash$",
        ):
            expected = coordinator._converge_archive(
                run,
                journal,
                authority,
                evidence,
            )
            coordinator._transition_complete(
                run,
                expected,
                journal,
                completion_authority=authority,
            )

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
        resumed_authority = SimpleNamespace(summary=resumed._media_summary)
        with mock.patch(
            "django_images.services.legacy_startup.LegacyMediaArchive",
            return_value=archive,
        ), mock.patch(
            "django_images.services.legacy_startup."
            "load_completed_auto_v2_summary",
            return_value=resumed._media_summary,
        ), mock.patch(
            "django_images.services.legacy_startup."
            "load_completed_media_asset_backfill_summary",
            return_value=resumed._backfill_summary,
        ):
            expected = resumed._converge_archive(
                run,
                journal,
                resumed_authority,
                evidence,
            )
            resumed._transition_complete(
                run,
                expected,
                journal,
                completion_authority=resumed_authority,
            )

        self.assertEqual(
            migration_state.read_run_status(run).phase,
            "complete",
        )
