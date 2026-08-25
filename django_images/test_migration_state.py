import json
import os
import shutil
import stat
import tempfile
from unittest import mock

from django.test import SimpleTestCase

from django_images.services.migration_state import (
    PHASES,
    PHASE_TRANSITIONS,
    MigrationStateError,
    ensure_migration_backup_root,
    inspect_run_inventory_read_only,
    persist_manifest_identity,
    read_run_status,
    resolve_or_create_run,
    scan_run_inventory,
    seal_run_identities,
    transition_state,
)


class MigrationStateTests(SimpleTestCase):
    def setUp(self):
        super(MigrationStateTests, self).setUp()
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        temporary_root = os.path.realpath(self.temporary_directory.name)
        self.data_root = temporary_root
        self.settings_override = self.settings(PINRY_DATA_ROOT=self.data_root)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.backup_root = os.path.join(
            temporary_root,
            "legacy-backup",
        )
        os.mkdir(self.backup_root, 0o700)
        self.uid = os.getuid()
        self.gid = os.getgid()

    def evidence(self, present=True):
        return {
            "has_legacy_evidence": present,
            "database_identity": {"device": 10, "inode": 20},
            "media_root_identity": {"device": 30, "inode": 40},
        }

    def create_run(self, source_commit="source-commit"):
        return resolve_or_create_run(
            scan_run_inventory(self.backup_root),
            self.evidence(),
            False,
            source_commit,
            self.uid,
            self.gid,
        )

    def archive_intent(self, identity):
        return {
            "source_root_device": 1,
            "source_root_inode": 2,
            "source_parent_relative": "originals",
            "source_parent_device": 3,
            "source_parent_inode": 4,
            "source_name": "{}.jpg".format(identity),
            "source_device": 5,
            "source_inode": identity,
            "destination_root_device": 6,
            "destination_root_inode": 7,
            "destination_parent_relative": "media/originals",
            "destination_parent_device": 8,
            "destination_parent_inode": 9,
            "destination_name": "{}.jpg".format(identity),
        }

    def write_run_state(self, run_id, phase, creating=False, payload=None):
        directory_name = run_id
        if creating:
            directory_name = ".creating-{}".format(run_id)
        run_path = os.path.join(self.backup_root, directory_name)
        os.mkdir(run_path, 0o700)
        state = {
            "format_version": 2,
            "run_id": run_id,
            "phase": phase,
            "source_commit": "source-commit",
            "database_identity": {"device": 10, "inode": 20},
            "media_root_identity": {"device": 30, "inode": 40},
            "initial_space": {
                "margin_bytes": 64,
                "required_bytes": 128,
            },
            "manifests": {
                "media": {"plan_sha256": None, "manifest_sha256": None},
                "backfill": {"plan_sha256": None, "manifest_sha256": None},
            },
            "intent": None,
            "progress": None,
        }
        if payload is not None:
            state.update(payload)
        state_path = os.path.join(run_path, "migration-state.json")
        with open(state_path, "w", encoding="utf-8") as state_file:
            json.dump(state, state_file)
        os.chmod(state_path, 0o600)
        return run_path

    def test_read_only_inventory_treats_missing_backup_root_as_empty(self):
        os.rmdir(self.backup_root)

        inventory = inspect_run_inventory_read_only(self.backup_root)

        self.assertFalse(inventory.root_exists)
        self.assertEqual(inventory.completed_count, 0)
        self.assertEqual(inventory.incomplete_count, 0)
        self.assertEqual(inventory.creating_count, 0)
        self.assertEqual(inventory.invalid_count, 0)
        self.assertFalse(inventory.requires_migration_flag)
        self.assertFalse(os.path.exists(self.backup_root))

    def test_flag_path_can_create_exact_backup_root_descriptor_safely(self):
        os.rmdir(self.backup_root)

        ensure_migration_backup_root(self.backup_root)

        root_stat = os.stat(self.backup_root, follow_symlinks=False)
        self.assertTrue(stat.S_ISDIR(root_stat.st_mode))
        self.assertEqual(stat.S_IMODE(root_stat.st_mode), 0o700)
        self.assertEqual(scan_run_inventory(self.backup_root).incomplete, ())

    def test_backup_root_creation_rejects_symlink_without_touching_target(self):
        os.rmdir(self.backup_root)
        outside = os.path.join(self.data_root, "outside")
        os.mkdir(outside, 0o755)
        os.symlink(outside, self.backup_root)
        before = os.stat(outside, follow_symlinks=False)

        with self.assertRaisesRegex(
            MigrationStateError,
            "^migration_state_root_invalid$",
        ):
            ensure_migration_backup_root(self.backup_root)

        after = os.stat(outside, follow_symlinks=False)
        self.assertEqual(
            (after.st_dev, after.st_ino, stat.S_IMODE(after.st_mode)),
            (before.st_dev, before.st_ino, stat.S_IMODE(before.st_mode)),
        )

    def test_read_only_inventory_never_promotes_or_fsyncs_creating_run(self):
        run_id = "20260825T010101Z-11111111-1111-4111-8111-111111111111"
        creating_path = self.write_run_state(
            run_id,
            "initialized",
            creating=True,
        )

        with mock.patch(
            "django_images.services.migration_state.os.rename"
        ) as rename, mock.patch(
            "django_images.services.migration_state.os.fsync"
        ) as fsync:
            inventory = inspect_run_inventory_read_only(self.backup_root)

        rename.assert_not_called()
        fsync.assert_not_called()
        self.assertTrue(inventory.requires_migration_flag)
        self.assertEqual(inventory.creating_count, 1)
        self.assertTrue(os.path.isdir(creating_path))
        self.assertFalse(os.path.exists(os.path.join(
            self.backup_root,
            run_id,
        )))

    def test_read_only_inventory_flags_wrong_root_mode_without_repair(self):
        os.chmod(self.backup_root, 0o755)

        with mock.patch(
            "django_images.services.migration_state.os.fchmod"
        ) as fchmod, mock.patch(
            "django_images.services.migration_state.os.fsync"
        ) as fsync:
            inventory = inspect_run_inventory_read_only(self.backup_root)

        self.assertEqual(inventory.invalid_count, 1)
        self.assertTrue(inventory.requires_migration_flag)
        self.assertEqual(
            stat.S_IMODE(os.stat(self.backup_root).st_mode),
            0o755,
        )
        fchmod.assert_not_called()
        fsync.assert_not_called()

    def test_read_only_inventory_reports_invalid_and_incomplete_runs(self):
        incomplete_id = (
            "20260825T010101Z-11111111-1111-4111-8111-111111111111"
        )
        invalid_id = (
            "20260825T010102Z-22222222-2222-4222-8222-222222222222"
        )
        self.write_run_state(incomplete_id, "copying")
        os.mkdir(os.path.join(self.backup_root, invalid_id), 0o700)

        inventory = inspect_run_inventory_read_only(self.backup_root)

        self.assertEqual(inventory.incomplete_count, 1)
        self.assertEqual(inventory.invalid_count, 1)
        self.assertTrue(inventory.requires_migration_flag)

    def test_explicit_false_evidence_and_no_pending_schema_creates_no_run(self):
        evidence = type("Evidence", (), {
            "has_legacy_evidence": False,
            "database_identity": None,
            "media_root_identity": None,
        })()

        run = resolve_or_create_run(
            scan_run_inventory(self.backup_root),
            evidence,
            False,
            "source-commit",
            self.uid,
            self.gid,
        )

        self.assertIsNone(run)
        self.assertEqual(os.listdir(self.backup_root), [])

    def test_truthy_object_without_explicit_evidence_flag_is_rejected(self):
        with self.assertRaisesRegex(
            MigrationStateError,
            "^migration_state_invalid$",
        ):
            resolve_or_create_run(
                scan_run_inventory(self.backup_root),
                object(),
                False,
                "source-commit",
                self.uid,
                self.gid,
            )

    def test_initial_space_is_persisted_and_exposed_by_public_status(self):
        run = resolve_or_create_run(
            scan_run_inventory(self.backup_root),
            self.evidence(),
            False,
            "source-commit",
            self.uid,
            self.gid,
            initial_space={
                "margin_bytes": 67108864,
                "required_bytes": 67109000,
            },
        )

        status = read_run_status(run)

        self.assertEqual(status.phase, "initialized")
        self.assertEqual(status.initial_margin_bytes, 67108864)
        self.assertEqual(status.initial_required_bytes, 67109000)
        with open(run.state_path, "r", encoding="utf-8") as state_file:
            persisted = json.load(state_file)
        self.assertEqual(persisted["initial_space"], {
            "margin_bytes": 67108864,
            "required_bytes": 67109000,
        })

    def test_missing_identities_can_be_sealed_once_then_cannot_change(self):
        run = resolve_or_create_run(
            scan_run_inventory(self.backup_root),
            {
                "has_legacy_evidence": True,
                "database_identity": None,
                "media_root_identity": None,
            },
            False,
            "source-commit",
            self.uid,
            self.gid,
        )
        sealed_evidence = {
            "has_legacy_evidence": True,
            "database_identity": {"device": 101, "inode": 201},
            "media_root_identity": {"device": 301, "inode": 401},
        }

        seal_run_identities(run, sealed_evidence)
        seal_run_identities(run, sealed_evidence)

        status = read_run_status(run)
        self.assertEqual(
            status.database_identity,
            {"device": 101, "inode": 201},
        )
        self.assertEqual(
            status.media_root_identity,
            {"device": 301, "inode": 401},
        )
        changed = dict(sealed_evidence)
        changed["media_root_identity"] = {"device": 301, "inode": 402}
        with self.assertRaisesRegex(
            MigrationStateError,
            "^migration_state_identity_changed$",
        ):
            seal_run_identities(run, changed)

    def test_manifest_identity_can_be_persisted_without_advancing_phase(self):
        run = self.create_run()
        transition_state(run, "initialized", "schema_complete")

        persist_manifest_identity(
            run,
            "schema_complete",
            "media",
            "1" * 64,
            "2" * 64,
        )
        persist_manifest_identity(
            run,
            "schema_complete",
            "media",
            "1" * 64,
            "3" * 64,
        )

        status = read_run_status(run)
        self.assertEqual(status.phase, "schema_complete")
        self.assertEqual(status.media_plan_sha256, "1" * 64)
        self.assertEqual(status.media_manifest_sha256, "3" * 64)
        with self.assertRaisesRegex(
            MigrationStateError,
            "^migration_state_plan_mismatch$",
        ):
            persist_manifest_identity(
                run,
                "schema_complete",
                "media",
                "4" * 64,
                "5" * 64,
            )

    def test_single_incomplete_run_is_resumed(self):
        first = self.create_run()

        resumed = resolve_or_create_run(
            scan_run_inventory(self.backup_root),
            self.evidence(),
            False,
            "different-commit",
            self.uid,
            self.gid,
        )

        self.assertEqual(resumed.run_id, first.run_id)
        self.assertEqual(resumed.state["source_commit"], "source-commit")

    def test_multiple_incomplete_runs_fail_closed(self):
        self.write_run_state(
            "20260825T010101Z-11111111-1111-4111-8111-111111111111",
            "initialized",
        )
        self.write_run_state(
            "20260825T010102Z-22222222-2222-4222-8222-222222222222",
            "copying",
        )

        with self.assertRaisesRegex(
            MigrationStateError,
            "^migration_state_conflict$",
        ):
            resolve_or_create_run(
                scan_run_inventory(self.backup_root),
                self.evidence(),
                False,
                "source-commit",
                self.uid,
                self.gid,
            )

    def test_completed_run_does_not_suppress_restored_evidence(self):
        completed_id = (
            "20260825T010101Z-11111111-1111-4111-8111-111111111111"
        )
        self.write_run_state(completed_id, "complete")

        created = resolve_or_create_run(
            scan_run_inventory(self.backup_root),
            self.evidence(),
            False,
            "new-commit",
            self.uid,
            self.gid,
        )

        self.assertNotEqual(created.run_id, completed_id)
        self.assertEqual(created.state["source_commit"], "new-commit")

    def test_invalid_historical_run_blocks_only_when_evidence_remains(self):
        run_id = "20260825T010101Z-11111111-1111-4111-8111-111111111111"
        os.mkdir(os.path.join(self.backup_root, run_id), 0o700)

        inventory = scan_run_inventory(self.backup_root)
        self.assertIsNone(resolve_or_create_run(
            inventory,
            self.evidence(present=False),
            False,
            "source-commit",
            self.uid,
            self.gid,
        ))
        with self.assertRaisesRegex(
            MigrationStateError,
            "^migration_state_missing_or_invalid$",
        ):
            resolve_or_create_run(
                inventory,
                self.evidence(),
                False,
                "source-commit",
                self.uid,
                self.gid,
            )

    def test_one_valid_creating_run_is_promoted(self):
        run_id = "20260825T010101Z-11111111-1111-4111-8111-111111111111"
        self.write_run_state(run_id, "initialized", creating=True)

        inventory = scan_run_inventory(self.backup_root)

        self.assertEqual([run.run_id for run in inventory.incomplete], [run_id])
        self.assertTrue(os.path.isdir(os.path.join(self.backup_root, run_id)))
        self.assertFalse(os.path.exists(os.path.join(
            self.backup_root,
            ".creating-{}".format(run_id),
        )))

    def test_multiple_or_invalid_creating_runs_fail_closed(self):
        first = "20260825T010101Z-11111111-1111-4111-8111-111111111111"
        second = "20260825T010102Z-22222222-2222-4222-8222-222222222222"
        self.write_run_state(first, "initialized", creating=True)
        self.write_run_state(second, "initialized", creating=True)

        with self.assertRaisesRegex(
            MigrationStateError,
            "^migration_state_conflict$",
        ):
            scan_run_inventory(self.backup_root)

        os.rename(
            os.path.join(self.backup_root, ".creating-{}".format(second)),
            os.path.join(self.backup_root, second),
        )
        state_path = os.path.join(
            self.backup_root,
            ".creating-{}".format(first),
            "migration-state.json",
        )
        with open(state_path, "w", encoding="utf-8") as state_file:
            state_file.write("not-json")
        with self.assertRaisesRegex(
            MigrationStateError,
            "^migration_state_conflict$",
        ):
            scan_run_inventory(self.backup_root)

    def test_transition_graph_rejects_unknown_and_skipped_phases(self):
        run = self.create_run()
        self.assertEqual(len(PHASES), 10)

        for next_phase in ("copying", "unknown"):
            with self.assertRaisesRegex(
                MigrationStateError,
                "^migration_state_invalid_transition$",
            ):
                transition_state(run, "initialized", next_phase)

        transition_state(
            run,
            "initialized",
            "snapshot_intent",
            intent={
                "kind": "sqlite_snapshot",
                "source_device": 1,
                "source_inode": 2,
            },
        )
        with self.assertRaisesRegex(
            MigrationStateError,
            "^migration_state_phase_mismatch$",
        ):
            transition_state(run, "initialized", "snapshot_complete")

    def test_phase_constants_match_approved_state_machine(self):
        self.assertEqual(PHASES, (
            "initialized",
            "snapshot_intent",
            "snapshot_complete",
            "schema_complete",
            "copying",
            "paths_complete",
            "registry_complete",
            "archive_intent",
            "archive_complete",
            "complete",
        ))
        self.assertEqual(PHASE_TRANSITIONS, {
            "initialized": {"snapshot_intent", "schema_complete"},
            "snapshot_intent": {"snapshot_complete"},
            "snapshot_complete": {"schema_complete"},
            "schema_complete": {"copying", "paths_complete"},
            "copying": {"copying", "paths_complete"},
            "paths_complete": {"registry_complete"},
            "registry_complete": {"archive_intent", "complete"},
            "archive_intent": {"archive_intent", "archive_complete"},
            "archive_complete": {"complete"},
            "complete": set(),
        })

    def test_archive_same_phase_update_preserves_each_item_progress(self):
        run = self.create_run()
        first_intent = self.archive_intent(10)
        second_intent = self.archive_intent(11)
        transition_state(run, "initialized", "schema_complete")
        transition_state(run, "schema_complete", "paths_complete")
        transition_state(run, "paths_complete", "registry_complete")
        transition_state(
            run,
            "registry_complete",
            "archive_intent",
            intent=first_intent,
            progress={
                "items": [{"intent": first_intent, "complete": False}],
            },
        )

        updated = transition_state(
            run,
            "archive_intent",
            "archive_intent",
            intent=second_intent,
            progress={
                "items": [
                    {"intent": first_intent, "complete": True},
                    {"intent": second_intent, "complete": False},
                ],
            },
        )

        self.assertEqual(updated.state["phase"], "archive_intent")
        self.assertEqual(updated.state["progress"]["items"][0]["complete"], True)
        with open(updated.state_path, "r", encoding="utf-8") as state_file:
            self.assertEqual(json.load(state_file), updated.state)

    def test_state_update_uses_exclusive_private_temp_and_fsync_order(self):
        run = self.create_run()
        events = []
        real_open = os.open
        real_fsync = os.fsync
        real_replace = os.replace

        def record_open(path, flags, mode=0o777, *args, **kwargs):
            descriptor = real_open(path, flags, mode, *args, **kwargs)
            if str(path).startswith(".migration-state.json.tmp-"):
                events.append(("open", flags, mode, descriptor))
            return descriptor

        def record_fsync(descriptor):
            file_mode = os.fstat(descriptor).st_mode
            events.append((
                "fsync-dir" if stat.S_ISDIR(file_mode) else "fsync-file",
                descriptor,
            ))
            return real_fsync(descriptor)

        def record_replace(source, destination, *args, **kwargs):
            events.append(("replace", source, destination))
            return real_replace(source, destination, *args, **kwargs)

        with mock.patch(
            "django_images.services.migration_state.os.open",
            side_effect=record_open,
        ), mock.patch(
            "django_images.services.migration_state.os.fsync",
            side_effect=record_fsync,
        ), mock.patch(
            "django_images.services.migration_state.os.replace",
            side_effect=record_replace,
        ):
            transition_state(run, "initialized", "schema_complete")

        temp_open = next(event for event in events if event[0] == "open")
        self.assertTrue(temp_open[1] & os.O_EXCL)
        self.assertEqual(temp_open[2], 0o600)
        ordered = [event[0] for event in events]
        self.assertLess(ordered.index("fsync-file"), ordered.index("replace"))
        self.assertLess(ordered.index("replace"), ordered.index("fsync-dir"))
        self.assertEqual(stat.S_IMODE(os.stat(run.state_path).st_mode), 0o600)

    def test_state_preserves_separate_plan_and_current_manifest_hashes(self):
        run = self.create_run()
        transition_state(
            run,
            "initialized",
            "schema_complete",
            plan_sha256={"media": "1" * 64, "backfill": "2" * 64},
            manifest_sha256={"media": "3" * 64, "backfill": "4" * 64},
        )

        self.assertEqual(
            run.state["manifests"]["media"]["plan_sha256"],
            "1" * 64,
        )
        self.assertEqual(
            run.state["manifests"]["media"]["manifest_sha256"],
            "3" * 64,
        )
        self.assertEqual(
            run.state["manifests"]["backfill"]["plan_sha256"],
            "2" * 64,
        )
        self.assertEqual(run.state["database_identity"]["inode"], 20)
        self.assertEqual(run.state["media_root_identity"]["inode"], 40)

    def test_initial_state_chown_failure_leaves_no_root_owned_file(self):
        inventory = scan_run_inventory(self.backup_root)
        real_fchown = os.fchown
        calls = []

        def fail_state_chown(descriptor, uid, gid):
            calls.append(descriptor)
            if len(calls) == 2:
                raise OSError("private-owner-error")
            return real_fchown(descriptor, uid, gid)

        with mock.patch(
            "django_images.services.migration_state.os.fchown",
            side_effect=fail_state_chown,
        ), self.assertRaisesRegex(
            MigrationStateError,
            "^migration_state_create_failed$",
        ):
            resolve_or_create_run(
                inventory,
                self.evidence(),
                False,
                "source-commit",
                self.uid,
                self.gid,
            )

        creating = [
            name for name in os.listdir(self.backup_root)
            if name.startswith(".creating-")
        ]
        self.assertEqual(len(creating), 1)
        self.assertEqual(
            os.listdir(os.path.join(self.backup_root, creating[0])),
            [],
        )

    def test_run_directory_chown_failure_leaves_no_root_owned_directory(self):
        inventory = scan_run_inventory(self.backup_root)
        with mock.patch(
            "django_images.services.migration_state.os.fchown",
            side_effect=OSError("private-owner-error"),
        ), self.assertRaisesRegex(
            MigrationStateError,
            "^migration_state_create_failed$",
        ):
            resolve_or_create_run(
                inventory,
                self.evidence(),
                False,
                "source-commit",
                self.uid,
                self.gid,
            )

        self.assertEqual(os.listdir(self.backup_root), [])

    def test_transition_chown_failure_leaves_no_root_owned_temp(self):
        run = self.create_run()
        with mock.patch(
            "django_images.services.migration_state.os.fchown",
            side_effect=OSError("private-owner-error"),
        ), self.assertRaisesRegex(
            MigrationStateError,
            "^migration_state_write_failed$",
        ):
            transition_state(run, "initialized", "schema_complete")

        self.assertEqual(
            [
                name for name in os.listdir(run.path)
                if name.startswith(".migration-state.json.tmp-")
            ],
            [],
        )

    def test_inventory_rejects_backup_root_outside_configured_data_root(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        outside_root = os.path.realpath(outside.name)
        outside_backup = os.path.join(outside_root, "legacy-backup")
        os.mkdir(outside_backup, 0o700)

        with self.assertRaisesRegex(
            MigrationStateError,
            "^migration_state_root_invalid$",
        ) as captured:
            scan_run_inventory(outside_backup)

        self.assertNotIn(outside_backup, str(captured.exception))

    def test_inventory_rejects_symlinked_configured_backup_root(self):
        configured_root = os.path.join(self.data_root, "configured")
        os.mkdir(configured_root, 0o700)
        outside_root = os.path.join(self.data_root, "outside")
        os.mkdir(outside_root, 0o700)
        outside_backup = os.path.join(outside_root, "legacy-backup")
        os.mkdir(outside_backup, 0o700)
        configured_backup = os.path.join(configured_root, "legacy-backup")
        os.symlink(outside_backup, configured_backup)

        with self.settings(PINRY_DATA_ROOT=configured_root), self.assertRaisesRegex(
            MigrationStateError,
            "^migration_state_root_invalid$",
        ):
            scan_run_inventory(configured_backup)

    def test_resume_rejects_database_or_media_root_identity_change(self):
        run = self.create_run()

        for identity_name, replacement in (
            ("database_identity", {"device": 10, "inode": 21}),
            ("media_root_identity", {"device": 30, "inode": 41}),
        ):
            evidence = self.evidence()
            evidence[identity_name] = replacement
            with self.subTest(identity_name=identity_name), self.assertRaisesRegex(
                MigrationStateError,
                "^migration_state_identity_changed$",
            ):
                resolve_or_create_run(
                    scan_run_inventory(self.backup_root),
                    evidence,
                    False,
                    "source-commit",
                    self.uid,
                    self.gid,
                )
        self.assertEqual(run.state["phase"], "initialized")

    def test_plan_hash_cannot_change_after_first_persist(self):
        run = self.create_run()
        transition_state(
            run,
            "initialized",
            "schema_complete",
        )
        transition_state(
            run,
            "schema_complete",
            "copying",
            plan_sha256="1" * 64,
        )

        with self.assertRaisesRegex(
            MigrationStateError,
            "^migration_state_plan_mismatch$",
        ):
            transition_state(
                run,
                "copying",
                "copying",
                plan_sha256="2" * 64,
            )

        inventory = scan_run_inventory(self.backup_root)
        self.assertEqual(
            inventory.incomplete[0].state["manifests"]["media"][
                "plan_sha256"
            ],
            "1" * 64,
        )

    def test_creating_run_requires_strict_identity_intent_progress_structure(self):
        cases = (
            ("database_identity", "malformed"),
            ("media_root_identity", "missing"),
            ("intent", "malformed"),
            ("progress", "malformed"),
        )
        run_ids = (
            "20260825T010101Z-11111111-1111-4111-8111-111111111111",
            "20260825T010102Z-22222222-2222-4222-8222-222222222222",
            "20260825T010103Z-33333333-3333-4333-8333-333333333333",
            "20260825T010104Z-44444444-4444-4444-8444-444444444444",
        )
        for run_id, case in zip(run_ids, cases):
            field_name, mutation = case
            creating_path = self.write_run_state(
                run_id,
                "initialized",
                creating=True,
            )
            state_path = os.path.join(creating_path, "migration-state.json")
            with open(state_path, "r", encoding="utf-8") as state_file:
                state = json.load(state_file)
            if mutation == "missing":
                del state[field_name]
            else:
                state[field_name] = []
            with open(state_path, "w", encoding="utf-8") as state_file:
                json.dump(state, state_file)

            try:
                with self.subTest(field_name=field_name), self.assertRaisesRegex(
                    MigrationStateError,
                    "^migration_state_conflict$",
                ):
                    scan_run_inventory(self.backup_root)
            finally:
                for directory_name in (
                    ".creating-{}".format(run_id),
                    run_id,
                ):
                    path = os.path.join(self.backup_root, directory_name)
                    if os.path.exists(path):
                        shutil.rmtree(path)

    def test_snapshot_intent_creating_run_requires_exact_source_identity(self):
        run_id = "20260825T010101Z-11111111-1111-4111-8111-111111111111"
        valid_intent = {
            "kind": "sqlite_snapshot",
            "source_device": 1,
            "source_inode": 2,
        }
        invalid_intents = (
            {},
            dict(valid_intent, unexpected=True),
            dict(valid_intent, kind="other"),
            dict(valid_intent, source_device=True),
            dict(valid_intent, source_inode="2"),
        )

        for intent in invalid_intents:
            self.write_run_state(
                run_id,
                "snapshot_intent",
                creating=True,
                payload={"intent": intent},
            )
            try:
                with self.subTest(intent=intent), self.assertRaisesRegex(
                    MigrationStateError,
                    "^migration_state_conflict$",
                ):
                    scan_run_inventory(self.backup_root)
            finally:
                creating_path = os.path.join(
                    self.backup_root,
                    ".creating-{}".format(run_id),
                )
                if os.path.exists(creating_path):
                    shutil.rmtree(creating_path)

    def test_archive_intent_creating_run_requires_bound_incomplete_progress(self):
        run_id = "20260825T010101Z-11111111-1111-4111-8111-111111111111"
        intent = self.archive_intent(10)
        valid_progress = {
            "items": [{"intent": intent, "complete": False}],
        }
        invalid_payloads = (
            {"intent": {}, "progress": valid_progress},
            {
                "intent": dict(intent, unexpected=True),
                "progress": valid_progress,
            },
            {"intent": intent, "progress": {"items": []}},
            {
                "intent": intent,
                "progress": {
                    "items": [{"intent": intent, "complete": True}],
                },
            },
            {
                "intent": intent,
                "progress": {
                    "items": [{"intent": intent, "complete": "no"}],
                },
            },
        )

        for payload in invalid_payloads:
            self.write_run_state(
                run_id,
                "archive_intent",
                creating=True,
                payload=payload,
            )
            try:
                with self.subTest(payload=payload), self.assertRaisesRegex(
                    MigrationStateError,
                    "^migration_state_conflict$",
                ):
                    scan_run_inventory(self.backup_root)
            finally:
                creating_path = os.path.join(
                    self.backup_root,
                    ".creating-{}".format(run_id),
                )
                if os.path.exists(creating_path):
                    shutil.rmtree(creating_path)

    def test_non_intent_phase_rejects_intent_or_progress(self):
        run_ids = (
            "20260825T010101Z-11111111-1111-4111-8111-111111111111",
            "20260825T010102Z-22222222-2222-4222-8222-222222222222",
        )
        payloads = (
            {"intent": {"kind": "sqlite_snapshot"}},
            {"progress": {"items": []}},
        )
        for run_id, payload in zip(run_ids, payloads):
            self.write_run_state(
                run_id,
                "initialized",
                creating=True,
                payload=payload,
            )
            try:
                with self.subTest(payload=payload), self.assertRaisesRegex(
                    MigrationStateError,
                    "^migration_state_conflict$",
                ):
                    scan_run_inventory(self.backup_root)
            finally:
                creating_path = os.path.join(
                    self.backup_root,
                    ".creating-{}".format(run_id),
                )
                if os.path.exists(creating_path):
                    shutil.rmtree(creating_path)
