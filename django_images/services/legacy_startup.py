import json
import os
import stat
import uuid
from datetime import datetime

from django.conf import settings
from django.core.management import CommandError

from core.services.media_asset_backfill import (
    BackfillSummary,
    MANIFEST_FILENAME as BACKFILL_MANIFEST_FILENAME,
    MediaAssetBackfiller,
    SAFE_BACKFILL_REASON_CODES,
    load_completed_media_asset_backfill_summary,
    recover_incomplete_media_asset_plan,
)
from core.version import normalize_source_commit
from django_images import file_ops
from django_images.models import Image, Thumbnail
from django_images.services import migration_state, startup_preflight
from django_images.services.media_archive import LegacyMediaArchive
from django_images.services.media_migration_v2 import (
    AUTO_V2_MANIFEST_FILENAME,
    AutoV2CompletionAuthority,
    AutoV2ManifestLog,
    AutoV2MediaMigrator,
    AutoV2PlanSummary,
    load_completed_auto_v2_summary,
    recover_incomplete_auto_v2_plan,
)
from django_images.services.migration_batch_log import (
    JOURNAL_FILENAME,
    MigrationBatchJournal,
    MigrationBatchLogError,
)
from django_images.services.sqlite_snapshot import (
    protect_orphan_cleanup,
    snapshot_sqlite,
)


SUMMARY_FILENAME = "migration-summary.json"
_BACKUP_ROOT_NAME = "legacy-backup"

_PROGRESS_KEYS = {
    "preparing": frozenset(("phase",)),
    "snapshot": frozenset(("phase",)),
    "planning": frozenset((
        "phase",
        "run_id",
        "attempt",
        "resume_count",
        "started_at",
        "images_total",
        "files_total",
        "backfill_total",
    )),
    "recovery": frozenset((
        "phase",
        "run_id",
        "attempt",
        "resume_count",
        "started_at",
        "images_done",
        "images_total",
        "files_done",
        "files_total",
        "backfill_done",
        "backfill_total",
        "last_committed_batch",
    )),
    "upgrade_v2": frozenset((
        "phase", "images_done", "images_total",
    )),
    "copying": frozenset((
        "phase",
        "images_done",
        "images_total",
        "files_done",
        "files_total",
    )),
    "database": frozenset((
        "phase", "images_done", "images_total",
    )),
    "backfill_planning": frozenset((
        "phase", "backfill_total",
    )),
    "backfill_registering": frozenset((
        "phase",
        "backfill_done",
        "backfill_total",
        "last_committed_batch",
    )),
    "archive": frozenset(("phase",)),
    "finalizing": frozenset(("phase",)),
}

_SAFE_INTERNAL_ERROR_CODES = frozenset((
    "archive_failed",
    "archive_manifest_mismatch",
    "archive_state_conflict",
    "atomic_archive_unsupported",
    "legacy_media_still_referenced",
    "legacy_migration_flag_required",
    "legacy_migration_space_insufficient",
    "legacy_progress_event_invalid",
    "linear_batch_file_sync_failed",
    "linear_checkpoint_ahead",
    "linear_committed_repair_failed",
    "linear_committed_source_changed",
    "linear_journal_database_conflict",
    "linear_journal_invalid",
    "linear_journal_source_changed",
    "linear_journal_tail_repair_failed",
    "linear_work_totals_changed",
    "media_path_conflict",
    "media_storage_configuration_invalid",
    "migration_state_conflict",
    "migration_state_create_failed",
    "migration_state_identity_changed",
    "migration_state_invalid",
    "migration_state_invalid_transition",
    "migration_state_missing_or_invalid",
    "migration_state_phase_mismatch",
    "migration_state_plan_mismatch",
    "migration_state_root_invalid",
    "migration_state_write_failed",
    "paths_not_complete",
    "runtime_supervisor_failed",
    "sqlite_snapshot_failed",
    "unsafe_auto_v2_manifest",
    "unsafe_migration_summary",
))


class LegacyStartupError(Exception):
    def __init__(self, code):
        super(LegacyStartupError, self).__init__(code)
        self.code = code


class LegacyStartupCoordinator(object):
    def __init__(
        self,
        service_uid,
        service_gid,
        fault_injector=None,
        archive_adapter=None,
        progress_reporter=None,
    ):
        if (
            type(service_uid) is not int
            or service_uid < 0
            or type(service_gid) is not int
            or service_gid < 0
        ):
            raise LegacyStartupError("media_storage_configuration_invalid")
        if progress_reporter is not None and not callable(progress_reporter):
            raise LegacyStartupError("media_storage_configuration_invalid")
        self.service_uid = service_uid
        self.service_gid = service_gid
        self.fault_injector = fault_injector
        self.archive_adapter = archive_adapter
        self.progress_reporter = progress_reporter
        self._evidence = None
        self._allow_missing_media_layout = False
        self._media_summary = None
        self._backfill_summary = None
        self._child_progress_error = None

    def prepare_no_flag_before_schema(self):
        """No-flag startup의 pre-schema evidence를 read-only로 고정한다."""
        evidence = startup_preflight.inspect_legacy_evidence(
            self._database_path(),
            None,
            settings.MEDIA_ROOT,
        )
        self._evidence = evidence
        self._allow_missing_media_layout = not evidence.database_exists
        if evidence.has_legacy_evidence:
            raise LegacyStartupError("legacy_migration_flag_required")
        return evidence

    def prepare_before_schema(self):
        """증거·state·공간·snapshot을 수렴하고 run 또는 None을 반환한다."""
        backup_root = self._backup_root()
        read_only = migration_state.inspect_run_inventory_read_only(
            backup_root
        )
        evidence = startup_preflight.inspect_legacy_evidence(
            self._database_path(),
            None,
            settings.MEDIA_ROOT,
        )
        self._evidence = evidence
        self._allow_missing_media_layout = (
            not evidence.has_legacy_evidence
            and (
                evidence.pending_schema
                or not evidence.has_named_canonical_paths
            )
        )
        needs_run = evidence.has_legacy_evidence or evidence.pending_schema
        if not needs_run and not read_only.requires_migration_flag:
            return None

        initial_space = startup_preflight.calculate_initial_space(
            evidence.database_bytes,
            evidence.distinct_legacy_bytes,
        )
        resuming = bool(
            read_only.incomplete_count or read_only.creating_count
        )
        initial_space_checked = needs_run and not resuming
        if initial_space_checked:
            self._require_space(initial_space.required_bytes)
        migration_state.ensure_migration_backup_root(backup_root)
        inventory = migration_state.scan_run_inventory(backup_root)
        if inventory.invalid_count:
            raise LegacyStartupError("migration_state_missing_or_invalid")
        run = migration_state.resolve_or_create_run(
            inventory,
            evidence,
            evidence.pending_schema,
            normalize_source_commit(
                getattr(settings, "PINRY_SOURCE_COMMIT", None)
            )["source_commit"],
            self.service_uid,
            self.service_gid,
            initial_space=initial_space,
        )
        if run is None:
            return None

        status = migration_state.read_run_status(run)
        if status.phase != "complete":
            self._report_progress({"phase": "preparing"})
        if (
            status.phase in ("initialized", "snapshot_intent")
            and not initial_space_checked
        ):
            self._require_space(status.initial_required_bytes)
        if status.phase == "initialized" and evidence.database_identity is not None:
            intent = {
                "kind": "sqlite_snapshot",
                "source_device": evidence.database_identity["device"],
                "source_inode": evidence.database_identity["inode"],
            }
            self._transition(
                run,
                "initialized",
                "snapshot_intent",
                intent=intent,
            )
            self._fault("after_snapshot_intent")
            status = migration_state.read_run_status(run)
        if status.phase == "snapshot_intent":
            snapshot = snapshot_sqlite(
                self._database_path(),
                run,
                self.service_uid,
                self.service_gid,
            )
            if snapshot is None:
                raise LegacyStartupError("sqlite_snapshot_failed")
            self._fault("after_snapshot")
            self._cleanup_missing_unreferenced_images(
                run,
                status,
                evidence,
            )
            self._transition(
                run,
                "snapshot_intent",
                "snapshot_complete",
            )
            self._report_progress({"phase": "snapshot"})
            self._fault("after_snapshot_complete")
            status = migration_state.read_run_status(run)
        elif status.phase in ("snapshot_complete", "schema_complete"):
            self._cleanup_missing_unreferenced_images(
                run,
                status,
                evidence,
            )
        return run

    def _cleanup_missing_unreferenced_images(
        self,
        run,
        status,
        evidence,
    ):
        candidates = evidence.missing_unreferenced_images
        if not candidates or not self._media_plan_not_started(run, status):
            return 0
        expected_database = status.database_identity
        expected_media_root = status.media_root_identity
        if expected_database is None or expected_media_root is None:
            raise LegacyStartupError("migration_state_identity_changed")
        with protect_orphan_cleanup(
            self._database_path(),
            run,
            self.service_uid,
            self.service_gid,
            before_fallback_create=self._require_cleanup_snapshot_space,
        ) as backup_guard:
            cleanup = (
                startup_preflight.remove_confirmed_missing_unreferenced_images
            )
            removed = cleanup(
                self._database_path(),
                settings.MEDIA_ROOT,
                candidates,
                expected_database,
                expected_media_root,
                backup_guard,
            )
        self._fault("after_missing_unreferenced_image_cleanup")
        return removed

    def _require_cleanup_snapshot_space(self, database_bytes):
        budget = startup_preflight.calculate_initial_space(
            database_bytes,
            0,
        )
        self._require_space(budget.required_bytes)

    def _media_plan_not_started(self, run, status):
        if (
            status.media_plan_sha256 is not None
            or status.media_manifest_sha256 is not None
        ):
            return False
        run_directory = None
        receipt = None
        try:
            run_directory = file_ops.open_verified_media_root(run.path)
            receipt = file_ops.open_verified_media_file(
                run_directory,
                AUTO_V2_MANIFEST_FILENAME,
                missing_ok=True,
            )
            if receipt is None:
                return True
            receipt.verify_current()
            return False
        except (OSError, file_ops.MediaPathError) as error:
            raise LegacyStartupError("unsafe_auto_v2_manifest") from error
        finally:
            if receipt is not None:
                receipt.close()
            if run_directory is not None:
                run_directory.close()

    def schema_required(self, run):
        if run is None:
            return True
        return migration_state.read_run_status(run).phase in (
            "initialized",
            "snapshot_complete",
        )

    def prepare_migration_locks(self, run):
        """스키마 실행 전 이관 잠금을 startup identity로 고정한다."""
        if run is None:
            return None
        self._ensure_allowed_media_layout()
        self._configuration_preflight(os.geteuid(), os.getegid())
        self._seal_current_identities(run)
        return run

    def converge_after_schema(self, run):
        try:
            return self._converge_after_schema(run)
        except Exception as error:
            code = getattr(error, "code", None)
            if code is None and isinstance(error, CommandError):
                code = str(error)
            if isinstance(error, LegacyStartupError):
                code = error.code
            if code not in _SAFE_INTERNAL_ERROR_CODES:
                code = "legacy_startup_failed"
            raise LegacyStartupError(code) from error

    def _converge_after_schema(self, run):
        """preflight, path, registry, archive, complete를 순서대로 수렴한다."""
        if run is None and self._evidence is None:
            self._allow_missing_media_layout = False
        if run is None and self._allow_missing_media_layout:
            post_schema_evidence = (
                startup_preflight.inspect_legacy_evidence(
                    self._database_path(),
                    None,
                    settings.MEDIA_ROOT,
                )
            )
            if post_schema_evidence.has_media_rows:
                self._allow_missing_media_layout = False
        if run is not None:
            status = migration_state.read_run_status(run)
            if status.phase in ("initialized", "snapshot_complete"):
                self._transition(run, status.phase, "schema_complete")
                self._fault("after_schema_complete")
            elif status.phase in ("snapshot_intent",):
                raise LegacyStartupError("migration_state_phase_mismatch")

        self._ensure_allowed_media_layout()
        if run is None:
            self._configuration_preflight(
                self.service_uid,
                self.service_gid,
            )
            return None
        status = migration_state.read_run_status(run)
        if status.phase == "complete":
            self._configuration_preflight(
                self.service_uid,
                self.service_gid,
            )
            self._seal_current_identities(run)
            self._write_summary(run)
            return run
        self._configuration_preflight(os.geteuid(), os.getegid())
        self._seal_current_identities(run)
        self._converge_media(run)
        status = migration_state.read_run_status(run)
        if status.phase != "complete":
            raise LegacyStartupError("migration_state_phase_mismatch")
        self._configuration_preflight(
            self.service_uid,
            self.service_gid,
        )
        return run

    def adjust_ownership(self, startup_lock_descriptor):
        managed_paths = []
        for configured in (
            getattr(settings, "STATIC_ROOT", None),
            settings.MEDIA_ROOT,
            self._database_path(),
        ):
            if configured is not None and os.path.lexists(os.fspath(configured)):
                managed_paths.append(os.fspath(configured))
        startup_preflight.adjust_storage_ownership(
            settings.PINRY_DATA_ROOT,
            tuple(dict.fromkeys(managed_paths)),
            self.service_uid,
            self.service_gid,
            startup_lock_descriptor,
        )

    def runtime_check(self, service_uid, service_gid):
        """service identity로 실제 storage write·lock probe를 실행한다."""
        result = startup_preflight.validate_storage_runtime_preflight(
            settings.MEDIA_ROOT,
            service_uid,
            service_gid,
        )
        if not result.ok:
            raise LegacyStartupError(result.reason_code)
        return result

    def _converge_media(self, run):  # noqa: C901
        status = migration_state.read_run_status(run)
        migrator = AutoV2MediaMigrator(
            run.path,
            AUTO_V2_MANIFEST_FILENAME,
            run.run_id,
            self.service_uid,
            self.service_gid,
            fault_injector=self.fault_injector,
            progress_reporter=self._report_child_progress,
        )
        self._child_progress_error = None
        resume_torn_execution = False
        try:
            planned = self._run_child(
                lambda: migrator.run(execute=False)
            )
        except CommandError as error:
            reason = str(error)
            if reason not in (
                "auto_v2_plan_incomplete",
                "media_manifest_torn_tail_requires_execute",
            ):
                raise
            resume_torn_execution = (
                reason == "media_manifest_torn_tail_requires_execute"
                and status.phase == "copying"
                and status.media_plan_sha256 is not None
            )
            if not resume_torn_execution:
                if (
                    status.phase != "schema_complete"
                    or status.media_plan_sha256 is not None
                ):
                    raise
                recover_incomplete_auto_v2_plan(
                    run.path,
                    AUTO_V2_MANIFEST_FILENAME,
                    run.run_id,
                    self.service_uid,
                    self.service_gid,
                )
                planned = self._run_child(
                    lambda: migrator.run(execute=False)
                )
        if resume_torn_execution:
            recovered = migrator.recover_execution_tail()
            self._validate_summary_run(recovered, run)
            if recovered.plan_sha256 != status.media_plan_sha256:
                raise LegacyStartupError("migration_state_plan_mismatch")
            remaining = startup_preflight.calculate_remaining_space(
                recovered.copy_required_bytes,
                status.initial_margin_bytes,
            )
            self._require_space(remaining.required_bytes)
            expected_plan_sha256 = recovered.plan_sha256
            planned = recovered
        else:
            self._validate_summary_run(planned, run)
            remaining = startup_preflight.calculate_remaining_space(
                planned.copy_required_bytes,
                status.initial_margin_bytes,
            )
            self._require_space(remaining.required_bytes)
            expected_plan_sha256 = planned.plan_sha256
        create_journal = status.phase == "schema_complete"
        run_directory = file_ops.open_verified_media_root(run.path)
        try:
            journal = MigrationBatchJournal.open(
                run_directory,
                JOURNAL_FILENAME,
                run.run_id,
                self.service_uid,
                self.service_gid,
                planned.plan_sha256,
                planned.manifest_sha256,
                create=create_journal,
            )
        except BaseException:
            run_directory.close()
            raise
        try:
            self._child_progress_error = None
            journal.record_attempt(
                datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
            )
            backfiller = MediaAssetBackfiller(
                run.path,
                BACKFILL_MANIFEST_FILENAME,
                run.run_id,
                self.service_uid,
                self.service_gid,
                fault_injector=self.fault_injector,
                progress_reporter=self._report_child_progress,
                batch_journal=journal,
            )
            if journal.state.work_totals is None:
                if not create_journal:
                    raise MigrationBatchLogError("linear_journal_invalid")
                journal.freeze_work_totals(
                    planned.image_count,
                    self._planned_file_count(migrator, run, planned),
                    backfiller.count_planned_images(),
                )
                self._report_planning(journal.recovery_snapshot())
            self._report_recovery(journal.recovery_snapshot())
            migrator.batch_journal = journal
            if status.phase == "schema_complete":
                migration_state.persist_manifest_identity(
                    run,
                    "schema_complete",
                    "media",
                    planned.plan_sha256,
                    planned.manifest_sha256,
                )
                self._media_summary = planned
                self._transition(
                    run,
                    "schema_complete",
                    "copying",
                    plan_sha256=planned.plan_sha256,
                    manifest_sha256=planned.manifest_sha256,
                    batch_journal=journal,
                )
                self._fault("after_copying_intent")
            executed = self._run_child(
                lambda: migrator.run(execute=True, upgrade_v2=True)
            )
            self._validate_summary_run(executed, run)
            if executed.plan_sha256 != expected_plan_sha256:
                raise LegacyStartupError("migration_state_plan_mismatch")
            self._media_summary = executed
            status = migration_state.read_run_status(run)
            if status.phase == "copying":
                migration_state.persist_manifest_identity(
                    run,
                    "copying",
                    "media",
                    executed.plan_sha256,
                    executed.manifest_sha256,
                )
                self._fault("after_media_execute")
                self._transition(
                    run,
                    "copying",
                    "paths_complete",
                    plan_sha256=executed.plan_sha256,
                    manifest_sha256=executed.manifest_sha256,
                    batch_journal=journal,
                )
                self._fault("after_paths_complete")
            else:
                self._validate_summary_identity(
                    executed,
                    run,
                    (
                        status.media_plan_sha256,
                        status.media_manifest_sha256,
                    ),
                )
            self._converge_backfill(run, journal, backfiller)
            archive_evidence = self._seal_current_identities(run)
            completion_authority = self._load_completion_authority(
                run, journal
            )
            expected = self._converge_archive(
                run,
                journal,
                completion_authority,
                archive_evidence,
            )
            self._report_progress({"phase": "finalizing"})
            self._transition_complete(
                run,
                expected,
                journal,
                completion_authority=completion_authority,
            )
        finally:
            try:
                journal.close()
            finally:
                run_directory.close()

    def _planned_file_count(self, migrator, run, planned):
        plans = getattr(migrator, "_frozen_plans", None)
        if plans is None:
            with AutoV2ManifestLog.open(
                run.path,
                AUTO_V2_MANIFEST_FILENAME,
                run.run_id,
                self.service_uid,
                self.service_gid,
                create=False,
            ) as manifest:
                summary = manifest.summary()
                self._validate_summary_identity(
                    summary,
                    run,
                    (
                        planned.plan_sha256,
                        planned.manifest_sha256,
                    ),
                )
                plans = tuple(manifest.state.plans)
        return sum(len(plan.files) for plan in plans)

    @staticmethod
    def _report_planning_values(snapshot):
        return {
            "phase": "planning",
            "run_id": snapshot["run_id"],
            "attempt": snapshot["attempt"],
            "resume_count": snapshot["resume_count"],
            "started_at": snapshot["started_at"],
            "images_total": snapshot["images_total"],
            "files_total": snapshot["files_total"],
            "backfill_total": snapshot["backfill_total"],
        }

    def _report_planning(self, snapshot):
        self._report_progress(self._report_planning_values(snapshot))

    def _report_recovery(self, snapshot):
        self._report_progress(dict({"phase": "recovery"}, **snapshot))

    def _load_completion_authority(self, run, journal):
        with AutoV2ManifestLog.open(
            run.path,
            AUTO_V2_MANIFEST_FILENAME,
            run.run_id,
            self.service_uid,
            self.service_gid,
            create=False,
        ) as manifest:
            authority = AutoV2CompletionAuthority.load(
                manifest, journal
            )
        status = migration_state.read_run_status(run)
        self._validate_summary_identity(
            authority.summary,
            run,
            (
                status.media_plan_sha256,
                status.media_manifest_sha256,
            ),
        )
        self._media_summary = authority.summary
        return authority

    def _converge_backfill(self, run, journal, backfiller=None):
        if backfiller is None:
            backfiller = MediaAssetBackfiller(
                run.path,
                BACKFILL_MANIFEST_FILENAME,
                run.run_id,
                self.service_uid,
                self.service_gid,
                fault_injector=self.fault_injector,
                progress_reporter=self._report_child_progress,
                batch_journal=journal,
            )
        status = migration_state.read_run_status(run)
        resume_torn_execution = False
        try:
            planned = self._run_child(
                lambda: backfiller.run(execute=False)
            )
        except CommandError as error:
            reason = str(error)
            if reason not in (
                "media_asset_plan_incomplete",
                "media_asset_manifest_torn_tail_requires_execute",
            ):
                raise
            resume_torn_execution = (
                reason == "media_asset_manifest_torn_tail_requires_execute"
                and status.backfill_plan_sha256 is not None
            )
            if not resume_torn_execution:
                if status.backfill_plan_sha256 is not None:
                    raise
                recover_incomplete_media_asset_plan(
                    run.path,
                    BACKFILL_MANIFEST_FILENAME,
                    run.run_id,
                    self.service_uid,
                    self.service_gid,
                )
                planned = self._run_child(
                    lambda: backfiller.run(execute=False)
                )
        if resume_torn_execution:
            expected_plan_sha256 = status.backfill_plan_sha256
        else:
            self._validate_summary_run(planned, run)
            if status.phase == "paths_complete":
                migration_state.persist_manifest_identity(
                    run,
                    "paths_complete",
                    "backfill",
                    planned.plan_sha256,
                    planned.manifest_sha256,
                )
            expected_plan_sha256 = planned.plan_sha256
        executed = self._run_child(
            lambda: backfiller.run(execute=True)
        )
        self._validate_summary_run(executed, run)
        if executed.plan_sha256 != expected_plan_sha256:
            raise LegacyStartupError("migration_state_plan_mismatch")
        self._backfill_summary = executed
        status = migration_state.read_run_status(run)
        if status.phase == "paths_complete":
            migration_state.persist_manifest_identity(
                run,
                "paths_complete",
                "backfill",
                executed.plan_sha256,
                executed.manifest_sha256,
            )
            self._fault("after_backfill_execute")
            self._transition(
                run,
                "paths_complete",
                "registry_complete",
                plan_sha256=executed.plan_sha256,
                manifest_sha256=executed.manifest_sha256,
                batch_journal=journal,
            )
            self._fault("after_registry_complete")
        else:
            self._validate_summary_identity(
                executed,
                run,
                (
                    status.backfill_plan_sha256,
                    status.backfill_manifest_sha256,
                ),
            )

    def _converge_archive(
        self,
        run,
        journal,
        completion_authority,
        evidence,
    ):
        status = migration_state.read_run_status(run)
        if evidence.has_md5_paths or evidence.has_fixed_slot_paths:
            raise LegacyStartupError("legacy_media_still_referenced")
        archive = LegacyMediaArchive(
            settings.MEDIA_ROOT,
            run.path,
            run.path,
            AUTO_V2_MANIFEST_FILENAME,
            run.run_id,
            self.service_uid,
            self.service_gid,
            batch_journal=journal,
            completion_authority=completion_authority,
        )
        if status.phase == "archive_complete":
            if status.progress is None:
                plan = archive.prepare(
                    recover_completed_fixed=True,
                    expected_plan_sha256=status.media_plan_sha256,
                    expected_manifest_sha256=(
                        status.media_manifest_sha256
                    ),
                )
            else:
                plan = archive.prepare(
                    progress=status.progress,
                    expected_plan_sha256=status.media_plan_sha256,
                    expected_manifest_sha256=(
                        status.media_manifest_sha256
                    ),
                )
            archive.converge(
                plan,
                syscall_adapter=self.archive_adapter,
            )
            return "archive_complete"
        if status.phase == "registry_complete":
            plan = archive.prepare(
                has_media_image_directory=(
                    evidence.has_media_image_directory
                ),
                has_pinry_direct_md5_directory=(
                    evidence.has_pinry_direct_md5_directory
                ),
                expected_plan_sha256=status.media_plan_sha256,
                expected_manifest_sha256=(
                    status.media_manifest_sha256
                ),
            )
            if not plan.intents:
                return "registry_complete"
            first = self._first_incomplete(plan.progress)
            self._transition(
                run,
                "registry_complete",
                "archive_intent",
                intent=first,
                progress=plan.progress,
                batch_journal=journal,
                completion_authority=completion_authority,
            )
            self._fault("after_archive_intent")
        else:
            plan = archive.prepare(
                progress=status.progress,
                expected_plan_sha256=status.media_plan_sha256,
                expected_manifest_sha256=(
                    status.media_manifest_sha256
                ),
            )
            if self._first_incomplete(plan.progress) != status.intent:
                raise LegacyStartupError("archive_state_conflict")

        self._report_progress({"phase": "archive"})

        def record_completion(intent, progress, result):
            del intent, result
            next_intent = self._first_incomplete(progress)
            if next_intent is None:
                self._transition(
                    run,
                    "archive_intent",
                    "archive_complete",
                    progress=progress,
                    batch_journal=journal,
                    completion_authority=completion_authority,
                )
            else:
                self._transition(
                    run,
                    "archive_intent",
                    "archive_intent",
                    intent=next_intent,
                    progress=progress,
                    batch_journal=journal,
                    completion_authority=completion_authority,
                )
            self._fault("after_archive_progress")

        archive.converge(
            plan,
            syscall_adapter=self.archive_adapter,
            on_item_complete=record_completion,
        )
        if migration_state.read_run_status(run).phase != "archive_complete":
            raise LegacyStartupError("archive_state_conflict")
        return "archive_complete"

    def _configuration_preflight(self, lock_uid, lock_gid):
        image_storage = Image._meta.get_field("image").storage
        thumbnail_storage = Thumbnail._meta.get_field("image").storage
        result = startup_preflight.validate_storage_configuration_preflight(
            settings.MEDIA_ROOT,
            image_storage,
            thumbnail_storage,
            settings.IMAGE_SIZES,
            lock_uid,
            lock_gid,
        )
        if not result.ok:
            raise LegacyStartupError(result.reason_code)
        return result

    def _seal_current_identities(self, run):
        evidence = startup_preflight.inspect_legacy_evidence(
            self._database_path(),
            None,
            settings.MEDIA_ROOT,
        )
        migration_state.seal_run_identities(run, evidence)
        return evidence

    def _ensure_allowed_media_layout(self):
        try:
            os.stat(os.fspath(settings.MEDIA_ROOT), follow_symlinks=False)
            return
        except FileNotFoundError:
            pass
        if not self._allow_missing_media_layout:
            raise LegacyStartupError(
                "media_storage_configuration_invalid"
            )
        startup_preflight.ensure_media_root_layout(
            settings.PINRY_DATA_ROOT,
            settings.MEDIA_ROOT,
            self.service_uid,
            self.service_gid,
        )

    def _transition(self, run, expected, next_phase, **kwargs):
        batch_journal = kwargs.pop("batch_journal", None)
        completion_authority = kwargs.pop(
            "completion_authority", None
        )
        transitioned = migration_state.transition_state(
            run,
            expected,
            next_phase,
            **kwargs
        )
        self._write_summary(
            transitioned,
            batch_journal=batch_journal,
            completion_authority=completion_authority,
        )
        return transitioned

    def _transition_complete(
        self,
        run,
        expected,
        batch_journal,
        completion_authority=None,
    ):
        status = migration_state.read_run_status(run)
        if status.phase != expected:
            raise LegacyStartupError("migration_state_phase_mismatch")
        if (
            not isinstance(self._media_summary, AutoV2PlanSummary)
            or not isinstance(self._backfill_summary, BackfillSummary)
            or completion_authority is None
            or completion_authority.summary is not self._media_summary
        ):
            raise LegacyStartupError("migration_state_plan_mismatch")
        self._write_summary(
            run,
            phase_override="complete",
            batch_journal=batch_journal,
            completion_authority=completion_authority,
        )
        self._fault("after_complete_summary")
        status = migration_state.read_run_status(run)
        if status.phase != expected:
            raise LegacyStartupError("migration_state_phase_mismatch")
        return migration_state.transition_state(
            run,
            expected,
            "complete",
        )

    def _run_child(self, operation):
        try:
            result = operation()
        except BaseException:
            self._raise_child_progress_error()
            raise
        self._raise_child_progress_error()
        return result

    def _report_child_progress(self, event):
        try:
            phase = event.get("phase") if isinstance(event, dict) else None
            if phase == "finalizing":
                self._validate_progress_event(event)
                return False
            return self._report_progress(event)
        except BaseException as error:
            if self._child_progress_error is None:
                self._child_progress_error = error
            raise

    def _raise_child_progress_error(self):
        error = self._child_progress_error
        self._child_progress_error = None
        if error is not None:
            raise error

    @staticmethod
    def _validate_progress_event(event):
        if not isinstance(event, dict):
            raise LegacyStartupError("legacy_progress_event_invalid")
        phase = event.get("phase")
        if (
            phase not in _PROGRESS_KEYS
            or frozenset(event) != _PROGRESS_KEYS[phase]
        ):
            raise LegacyStartupError("legacy_progress_event_invalid")
        count_names = frozenset((
            "attempt",
            "resume_count",
            "images_done",
            "images_total",
            "files_done",
            "files_total",
            "backfill_done",
            "backfill_total",
            "last_committed_batch",
        ))
        if any(
            type(value) is not int or value < 0
            for name, value in event.items()
            if name in count_names
        ):
            raise LegacyStartupError("legacy_progress_event_invalid")
        if "attempt" in event and (
            event["attempt"] < 1
            or event["resume_count"] != event["attempt"] - 1
        ):
            raise LegacyStartupError("legacy_progress_event_invalid")
        for done_name, total_name in (
            ("images_done", "images_total"),
            ("files_done", "files_total"),
            ("backfill_done", "backfill_total"),
        ):
            if (
                done_name in event
                and event[done_name] > event[total_name]
            ):
                raise LegacyStartupError("legacy_progress_event_invalid")
        if "run_id" in event and not migration_state._is_run_id(
            event["run_id"]
        ):
            raise LegacyStartupError("legacy_progress_event_invalid")
        if "started_at" in event:
            try:
                parsed = datetime.strptime(
                    event["started_at"], "%Y-%m-%dT%H:%M:%SZ"
                )
            except (TypeError, ValueError):
                raise LegacyStartupError(
                    "legacy_progress_event_invalid"
                ) from None
            if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != event[
                "started_at"
            ]:
                raise LegacyStartupError("legacy_progress_event_invalid")

    def _report_progress(self, event):
        self._validate_progress_event(event)
        if self.progress_reporter is None:
            return False
        self.progress_reporter(dict(event))
        return True

    def _write_summary(
        self,
        run,
        phase_override=None,
        batch_journal=None,
        completion_authority=None,
    ):
        if phase_override not in (None, "complete"):
            raise LegacyStartupError("unsafe_migration_summary")
        status = migration_state.read_run_status(run)
        if phase_override == "complete":
            if (
                completion_authority is None
                or completion_authority.summary is not self._media_summary
            ):
                raise LegacyStartupError("migration_state_plan_mismatch")
            self._validate_summary_identity(
                self._media_summary,
                run,
                (
                    status.media_plan_sha256,
                    status.media_manifest_sha256,
                ),
            )
            self._validate_summary_identity(
                self._backfill_summary,
                run,
                (
                    status.backfill_plan_sha256,
                    status.backfill_manifest_sha256,
                ),
            )
        else:
            self._restore_summary_values(
                run,
                status,
                batch_journal=batch_journal,
                completion_authority=completion_authority,
            )
        media = self._media_summary
        backfill = self._backfill_summary
        reasons = {} if backfill is None else dict(backfill.reason_counts)
        if (
            any(code not in SAFE_BACKFILL_REASON_CODES for code in reasons)
            or any(type(count) is not int or count < 0 for count in reasons.values())
        ):
            raise LegacyStartupError("unsafe_migration_summary")
        payload = {
            "format_version": 1,
            "source_commit": normalize_source_commit(
                status.source_commit
            )["source_commit"],
            "run_id": status.run_id,
            "phase": status.phase if phase_override is None else phase_override,
            "backup_relative_name": _BACKUP_ROOT_NAME,
            "media_plan_sha256": status.media_plan_sha256,
            "media_manifest_sha256": status.media_manifest_sha256,
            "backfill_plan_sha256": status.backfill_plan_sha256,
            "backfill_manifest_sha256": status.backfill_manifest_sha256,
            "media_image_count": _safe_summary_count(
                0 if media is None else media.image_count
            ),
            "media_md5_legacy": _safe_summary_count(
                0 if media is None else media.md5_legacy
            ),
            "media_fixed_slot": _safe_summary_count(
                0 if media is None else media.fixed_slot
            ),
            "media_named_canonical": _safe_summary_count(
                0 if media is None else media.named_canonical
            ),
            "backfill_scanned": _safe_summary_count(
                0 if backfill is None else backfill.scanned
            ),
            "backfill_registered": _safe_summary_count(
                0 if backfill is None else backfill.registered
            ),
            "backfill_already_registered": _safe_summary_count(
                0 if backfill is None else backfill.already_registered
            ),
            "backfill_skipped": _safe_summary_count(
                0 if backfill is None else backfill.skipped
            ),
            "reason_counts": dict(sorted(reasons.items())),
        }
        _atomic_write_summary(run, payload)

    def _restore_summary_values(
        self,
        run,
        status,
        force_reload=False,
        batch_journal=None,
        completion_authority=None,
    ):
        media_hashes = (
            status.media_plan_sha256,
            status.media_manifest_sha256,
        )
        if (media_hashes[0] is None) != (media_hashes[1] is None):
            raise LegacyStartupError("migration_state_plan_mismatch")
        if (
            media_hashes[0] is not None
            and (force_reload or self._media_summary is None)
        ):
            try:
                summary = load_completed_auto_v2_summary(
                    run.path,
                    AUTO_V2_MANIFEST_FILENAME,
                    run.run_id,
                    self.service_uid,
                    self.service_gid,
                    batch_journal=batch_journal,
                    completion_authority=completion_authority,
                )
            except CommandError as error:
                raise LegacyStartupError(
                    "migration_state_plan_mismatch"
                ) from error
            self._validate_summary_identity(
                summary,
                run,
                media_hashes,
            )
            self._media_summary = summary
        elif self._media_summary is not None and media_hashes[0] is not None:
            self._validate_summary_identity(
                self._media_summary,
                run,
                media_hashes,
            )

        backfill_hashes = (
            status.backfill_plan_sha256,
            status.backfill_manifest_sha256,
        )
        if (backfill_hashes[0] is None) != (backfill_hashes[1] is None):
            raise LegacyStartupError("migration_state_plan_mismatch")
        if (
            backfill_hashes[0] is not None
            and (force_reload or self._backfill_summary is None)
        ):
            try:
                summary = load_completed_media_asset_backfill_summary(
                    run.path,
                    BACKFILL_MANIFEST_FILENAME,
                    run.run_id,
                    self.service_uid,
                    self.service_gid,
                    batch_journal=batch_journal,
                )
            except CommandError as error:
                raise LegacyStartupError(
                    "migration_state_plan_mismatch"
                ) from error
            self._validate_summary_identity(
                summary,
                run,
                backfill_hashes,
            )
            self._backfill_summary = summary
        elif self._backfill_summary is not None and backfill_hashes[0] is not None:
            self._validate_summary_identity(
                self._backfill_summary,
                run,
                backfill_hashes,
            )

    @classmethod
    def _validate_summary_identity(cls, summary, run, hashes):
        cls._validate_summary_run(summary, run)
        if (
            summary.plan_sha256 != hashes[0]
            or summary.manifest_sha256 != hashes[1]
        ):
            raise LegacyStartupError("migration_state_plan_mismatch")

    def _backup_root(self):
        return os.path.join(settings.PINRY_DATA_ROOT, _BACKUP_ROOT_NAME)

    @staticmethod
    def _database_path():
        return settings.DATABASES["default"]["NAME"]

    def _require_space(self, required_bytes):
        available = startup_preflight.available_space_bytes(
            settings.PINRY_DATA_ROOT
        )
        if available < required_bytes:
            raise LegacyStartupError("legacy_migration_space_insufficient")

    @staticmethod
    def _validate_summary_run(summary, run):
        if getattr(summary, "run_id", None) != run.run_id:
            raise LegacyStartupError("migration_state_plan_mismatch")

    @staticmethod
    def _first_incomplete(progress):
        if not isinstance(progress, dict):
            raise LegacyStartupError("archive_state_conflict")
        items = progress.get("items")
        if not isinstance(items, list):
            raise LegacyStartupError("archive_state_conflict")
        for item in items:
            if isinstance(item, dict) and item.get("complete") is False:
                intent = item.get("intent")
                if isinstance(intent, dict):
                    return intent
                break
        if all(
            isinstance(item, dict) and item.get("complete") is True
            for item in items
        ):
            return None
        raise LegacyStartupError("archive_state_conflict")

    def _fault(self, point):
        if self.fault_injector is not None:
            self.fault_injector(point)


def _atomic_write_summary(run, payload):
    directory = None
    temp_descriptor = None
    temp_identity = None
    temp_name = ".migration-summary.json.tmp-{}".format(uuid.uuid4())
    try:
        directory = file_ops.open_verified_media_root(run.path)
        directory_stat = os.fstat(directory.descriptor)
        if (
            (directory_stat.st_dev, directory_stat.st_ino)
            != (run.directory_device, run.directory_inode)
            or directory_stat.st_uid != run.service_uid
            or directory_stat.st_gid != run.service_gid
            or stat.S_IMODE(directory_stat.st_mode) != 0o700
        ):
            raise LegacyStartupError("unsafe_migration_summary")
        existing_identity = _verified_summary_target(directory, run)
        content = (
            json.dumps(payload, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        temp_descriptor = os.open(
            temp_name,
            flags,
            0o600,
            dir_fd=directory.descriptor,
        )
        created_stat = os.fstat(temp_descriptor)
        if (
            not stat.S_ISREG(created_stat.st_mode)
            or created_stat.st_nlink != 1
        ):
            raise LegacyStartupError("unsafe_migration_summary")
        temp_identity = (created_stat.st_dev, created_stat.st_ino)
        os.fchmod(temp_descriptor, 0o600)
        os.fchown(temp_descriptor, run.service_uid, run.service_gid)
        _write_all(temp_descriptor, content)
        os.fsync(temp_descriptor)
        temp_stat = os.fstat(temp_descriptor)
        if (temp_stat.st_dev, temp_stat.st_ino) != temp_identity:
            raise LegacyStartupError("unsafe_migration_summary")
        os.close(temp_descriptor)
        temp_descriptor = None
        directory.verify_current()
        if _verified_summary_target(directory, run) != existing_identity:
            raise LegacyStartupError("unsafe_migration_summary")
        named_temp = os.stat(
            temp_name,
            dir_fd=directory.descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(named_temp.st_mode)
            or named_temp.st_nlink != 1
            or (named_temp.st_dev, named_temp.st_ino)
            != (temp_stat.st_dev, temp_stat.st_ino)
        ):
            raise LegacyStartupError("unsafe_migration_summary")
        os.replace(
            temp_name,
            SUMMARY_FILENAME,
            src_dir_fd=directory.descriptor,
            dst_dir_fd=directory.descriptor,
        )
        os.fsync(directory.descriptor)
        _verified_summary_target(directory, run, required=True)
    except LegacyStartupError:
        raise
    except Exception as error:
        raise LegacyStartupError("unsafe_migration_summary") from error
    finally:
        if temp_descriptor is not None:
            os.close(temp_descriptor)
        if directory is not None:
            try:
                named = os.stat(
                    temp_name,
                    dir_fd=directory.descriptor,
                    follow_symlinks=False,
                )
                if (
                    temp_identity is not None
                    and stat.S_ISREG(named.st_mode)
                    and named.st_nlink == 1
                    and (named.st_dev, named.st_ino) == temp_identity
                ):
                    os.unlink(temp_name, dir_fd=directory.descriptor)
                    os.fsync(directory.descriptor)
            except OSError:
                pass
            directory.close()


def _verified_summary_target(directory, run, required=False):
    try:
        named_stat = os.stat(
            SUMMARY_FILENAME,
            dir_fd=directory.descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        if required:
            raise LegacyStartupError("unsafe_migration_summary")
        return None
    if (
        not stat.S_ISREG(named_stat.st_mode)
        or named_stat.st_nlink != 1
        or named_stat.st_uid != run.service_uid
        or named_stat.st_gid != run.service_gid
        or stat.S_IMODE(named_stat.st_mode) != 0o600
    ):
        raise LegacyStartupError("unsafe_migration_summary")
    descriptor = None
    try:
        descriptor = os.open(
            SUMMARY_FILENAME,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=directory.descriptor,
        )
        opened_stat = os.fstat(descriptor)
        identity = (opened_stat.st_dev, opened_stat.st_ino)
        if identity != (named_stat.st_dev, named_stat.st_ino):
            raise LegacyStartupError("unsafe_migration_summary")
        return identity
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _write_all(descriptor, content):
    offset = 0
    while offset < len(content):
        written = os.write(descriptor, content[offset:])
        if written <= 0:
            raise LegacyStartupError("unsafe_migration_summary")
        offset += written


def _safe_summary_count(value):
    if type(value) is not int or value < 0:
        raise LegacyStartupError("unsafe_migration_summary")
    return value
