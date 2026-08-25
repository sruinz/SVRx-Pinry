import json
import os
import shutil
import stat
import tempfile
from unittest import mock

from django.test import SimpleTestCase

from django_images.services.migration_state import (
    PHASES,
    MigrationStateError,
    resolve_or_create_run,
    scan_run_inventory,
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
            "present": present,
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

        transition_state(run, "initialized", "snapshot_intent")
        with self.assertRaisesRegex(
            MigrationStateError,
            "^migration_state_phase_mismatch$",
        ):
            transition_state(run, "initialized", "snapshot_complete")

    def test_archive_same_phase_update_preserves_each_item_progress(self):
        run = self.create_run()
        transition_state(run, "initialized", "schema_complete")
        transition_state(run, "schema_complete", "paths_complete")
        transition_state(run, "paths_complete", "registry_complete")
        transition_state(
            run,
            "registry_complete",
            "archive_intent",
            intent={"kind": "media-root", "source_device": 1},
            progress={"items": [{"kind": "media-root", "complete": False}]},
        )

        updated = transition_state(
            run,
            "archive_intent",
            "archive_intent",
            intent={"kind": "fixed-slot", "source_device": 2},
            progress={
                "items": [
                    {"kind": "media-root", "complete": True},
                    {"kind": "fixed-slot", "complete": False},
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
