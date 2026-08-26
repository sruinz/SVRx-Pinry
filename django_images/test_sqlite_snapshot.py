from contextlib import ExitStack
import hashlib
import json
import os
import sqlite3
import stat
import tempfile
from unittest import mock

from django.test import SimpleTestCase

from django_images.services import sqlite_snapshot
from django_images.services.migration_state import (
    resolve_or_create_run,
    scan_run_inventory,
    transition_state,
)
from django_images.services.sqlite_snapshot import (
    SQLiteSnapshotError,
    snapshot_sqlite,
)


class SQLiteSnapshotTests(SimpleTestCase):
    def setUp(self):
        super(SQLiteSnapshotTests, self).setUp()
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.data_root = os.path.realpath(self.temporary_directory.name)
        self.settings_override = self.settings(PINRY_DATA_ROOT=self.data_root)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.backup_root = os.path.join(self.data_root, "legacy-backup")
        os.mkdir(self.backup_root, 0o700)
        self.source_path = os.path.join(self.data_root, "production.db")
        self.database_settings = {
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": self.source_path,
            },
        }

    def create_run(self, database_identity=None):
        run = resolve_or_create_run(
            scan_run_inventory(self.backup_root),
            {
                "present": True,
                "database_identity": database_identity,
                "media_root_identity": None,
            },
            False,
            "source-commit",
            os.getuid(),
            os.getgid(),
        )
        intent = None
        try:
            source_stat = os.lstat(self.source_path)
        except OSError:
            source_stat = None
        if source_stat is not None and stat.S_ISREG(source_stat.st_mode):
            intent = {
                "kind": "sqlite_snapshot",
                "source_device": source_stat.st_dev,
                "source_inode": source_stat.st_ino,
            }
        if intent is not None:
            transition_state(
                run,
                "initialized",
                "snapshot_intent",
                intent=intent,
            )
        return run

    def create_completed_snapshot_run(self, phase="snapshot_complete"):
        if not os.path.exists(self.source_path):
            sqlite3.connect(self.source_path).close()
        source_stat = os.stat(self.source_path)
        run = self.create_run(database_identity={
            "device": source_stat.st_dev,
            "inode": source_stat.st_ino,
        })
        with self.configured():
            info = snapshot_sqlite(
                self.source_path,
                run,
                os.getuid(),
                os.getgid(),
            )
        transition_state(run, "snapshot_intent", "snapshot_complete")
        if phase == "schema_complete":
            transition_state(run, "snapshot_complete", "schema_complete")
        return run, info

    def make_wal_database(self):
        connection = sqlite3.connect(self.source_path)
        self.addCleanup(connection.close)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE records (value TEXT NOT NULL)")
        connection.execute("INSERT INTO records VALUES ('committed')")
        connection.commit()
        connection.execute("INSERT INTO records VALUES ('pending')")
        return connection

    def configured(self, databases=None, data_root=None):
        configured = ExitStack()
        configured.enter_context(mock.patch.object(
            sqlite_snapshot.settings,
            "PINRY_DATA_ROOT",
            self.data_root if data_root is None else data_root,
        ))
        configured.enter_context(mock.patch.object(
            sqlite_snapshot.settings,
            "DATABASES",
            self.database_settings if databases is None else databases,
        ))
        return configured

    def test_online_backup_captures_consistent_committed_wal_state(self):
        self.make_wal_database()
        run = self.create_run()

        with self.configured():
            info = snapshot_sqlite(
                self.source_path,
                run,
                os.getuid(),
                os.getgid(),
            )

        snapshot_path = os.path.join(
            run.path,
            "production.db.before-migration",
        )
        snapshot = sqlite3.connect(
            "file:{}?mode=ro".format(snapshot_path),
            uri=True,
        )
        self.addCleanup(snapshot.close)
        self.assertEqual(snapshot.execute("PRAGMA quick_check").fetchone()[0], "ok")
        self.assertEqual(snapshot.execute("SELECT value FROM records").fetchall(), [
            ("committed",),
        ])
        with open(snapshot_path, "rb") as snapshot_file:
            self.assertEqual(
                info.sha256,
                hashlib.sha256(snapshot_file.read()).hexdigest(),
            )
        self.assertEqual(info.size, os.path.getsize(snapshot_path))
        receipt_path = os.path.join(
            run.path,
            ".production.db.before-migration.receipt",
        )
        self.assertTrue(os.path.isfile(receipt_path))
        with open(receipt_path, "r", encoding="utf-8") as receipt_file:
            self.assertEqual(json.load(receipt_file), {
                "size": info.size,
                "sha256": info.sha256,
                "source_device": info.source_device,
                "source_inode": info.source_inode,
            })

    def test_completed_snapshot_verifier_accepts_both_cleanup_phases(self):
        connection = sqlite3.connect(self.source_path)
        connection.execute("CREATE TABLE records (value TEXT)")
        connection.execute("INSERT INTO records VALUES ('before-cleanup')")
        connection.commit()
        connection.close()
        source_stat = os.stat(self.source_path)
        run = self.create_run(database_identity={
            "device": source_stat.st_dev,
            "inode": source_stat.st_ino,
        })

        with self.configured():
            expected = snapshot_sqlite(
                self.source_path,
                run,
                os.getuid(),
                os.getgid(),
            )
        transition_state(run, "snapshot_intent", "snapshot_complete")
        verifier = getattr(
            sqlite_snapshot,
            "verify_completed_sqlite_snapshot",
            None,
        )
        self.assertIsNotNone(verifier)

        self.assertEqual(
            verifier(run, os.getuid(), os.getgid()),
            expected,
        )
        transition_state(run, "snapshot_complete", "schema_complete")
        self.assertEqual(
            verifier(run, os.getuid(), os.getgid()),
            expected,
        )

    def test_completed_snapshot_verifier_rejects_snapshot_intent_phase(self):
        sqlite3.connect(self.source_path).close()
        source_stat = os.stat(self.source_path)
        run = self.create_run(database_identity={
            "device": source_stat.st_dev,
            "inode": source_stat.st_ino,
        })
        with self.configured():
            snapshot_sqlite(
                self.source_path,
                run,
                os.getuid(),
                os.getgid(),
            )

        with self.assertRaisesRegex(
            SQLiteSnapshotError,
            "^sqlite_snapshot_state_invalid$",
        ):
            sqlite_snapshot.verify_completed_sqlite_snapshot(
                run,
                os.getuid(),
                os.getgid(),
            )

    def test_completed_snapshot_verifier_rejects_state_source_mismatch(self):
        sqlite3.connect(self.source_path).close()
        source_stat = os.stat(self.source_path)
        run = self.create_run(database_identity={
            "device": source_stat.st_dev,
            "inode": source_stat.st_ino,
        })
        with self.configured():
            snapshot_sqlite(
                self.source_path,
                run,
                os.getuid(),
                os.getgid(),
            )
        transition_state(run, "snapshot_intent", "snapshot_complete")
        with open(run.state_path, "r", encoding="utf-8") as state_file:
            state = json.load(state_file)
        state["database_identity"] = {
            "device": source_stat.st_dev,
            "inode": source_stat.st_ino + 1,
        }
        with open(run.state_path, "w", encoding="utf-8") as state_file:
            json.dump(state, state_file)

        with self.assertRaisesRegex(
            SQLiteSnapshotError,
            "^sqlite_snapshot_conflict$",
        ):
            sqlite_snapshot.verify_completed_sqlite_snapshot(
                run,
                os.getuid(),
                os.getgid(),
            )

    def test_completed_snapshot_verifier_rejects_final_temp_collision(self):
        run, _info = self.create_completed_snapshot_run()
        temp_path = os.path.join(
            run.path,
            ".production.db.before-migration.tmp",
        )
        with open(temp_path, "wb"):
            pass
        os.chmod(temp_path, 0o600)

        with self.assertRaisesRegex(
            SQLiteSnapshotError,
            "^sqlite_snapshot_conflict$",
        ):
            sqlite_snapshot.verify_completed_sqlite_snapshot(
                run,
                os.getuid(),
                os.getgid(),
            )

    def test_completed_snapshot_verifier_requires_final_snapshot(self):
        run, _info = self.create_completed_snapshot_run()
        os.unlink(os.path.join(run.path, "production.db.before-migration"))

        with self.assertRaisesRegex(
            SQLiteSnapshotError,
            "^sqlite_snapshot_conflict$",
        ):
            sqlite_snapshot.verify_completed_sqlite_snapshot(
                run,
                os.getuid(),
                os.getgid(),
            )

    def test_completed_snapshot_verifier_requires_durable_receipt(self):
        run, _info = self.create_completed_snapshot_run()
        os.unlink(os.path.join(
            run.path,
            ".production.db.before-migration.receipt",
        ))

        with self.assertRaisesRegex(
            SQLiteSnapshotError,
            "^sqlite_snapshot_conflict$",
        ):
            sqlite_snapshot.verify_completed_sqlite_snapshot(
                run,
                os.getuid(),
                os.getgid(),
            )

    def test_completed_snapshot_verifier_rejects_wrong_service_owner(self):
        run, _info = self.create_completed_snapshot_run()

        with self.assertRaisesRegex(
            SQLiteSnapshotError,
            "^sqlite_snapshot_state_invalid$",
        ):
            sqlite_snapshot.verify_completed_sqlite_snapshot(
                run,
                os.getuid() + 1,
                os.getgid(),
            )

    def test_completed_snapshot_verifier_rejects_non_private_snapshot(self):
        run, _info = self.create_completed_snapshot_run()
        snapshot_path = os.path.join(
            run.path,
            "production.db.before-migration",
        )
        os.chmod(snapshot_path, 0o644)
        with self.assertRaisesRegex(
            SQLiteSnapshotError,
            "^unsafe_sqlite_snapshot$",
        ):
            sqlite_snapshot.verify_completed_sqlite_snapshot(
                run,
                os.getuid(),
                os.getgid(),
            )
        os.chmod(snapshot_path, 0o600)
        hardlink_path = os.path.join(run.path, "snapshot-hardlink")
        os.link(snapshot_path, hardlink_path)
        with self.assertRaisesRegex(
            SQLiteSnapshotError,
            "^unsafe_sqlite_snapshot$",
        ):
            sqlite_snapshot.verify_completed_sqlite_snapshot(
                run,
                os.getuid(),
                os.getgid(),
            )

    def test_completed_snapshot_verifier_rejects_non_private_receipt(self):
        run, _info = self.create_completed_snapshot_run()
        receipt_path = os.path.join(
            run.path,
            ".production.db.before-migration.receipt",
        )
        os.chmod(receipt_path, 0o644)
        with self.assertRaisesRegex(
            SQLiteSnapshotError,
            "^sqlite_snapshot_conflict$",
        ):
            sqlite_snapshot.verify_completed_sqlite_snapshot(
                run,
                os.getuid(),
                os.getgid(),
            )
        os.chmod(receipt_path, 0o600)
        hardlink_path = os.path.join(run.path, "receipt-hardlink")
        os.link(receipt_path, hardlink_path)
        with self.assertRaisesRegex(
            SQLiteSnapshotError,
            "^sqlite_snapshot_conflict$",
        ):
            sqlite_snapshot.verify_completed_sqlite_snapshot(
                run,
                os.getuid(),
                os.getgid(),
            )

    def test_completed_snapshot_verifier_binds_receipt_size_and_hash(self):
        run, info = self.create_completed_snapshot_run()
        receipt_path = os.path.join(
            run.path,
            ".production.db.before-migration.receipt",
        )
        with open(receipt_path, "r", encoding="utf-8") as receipt_file:
            receipt = json.load(receipt_file)
        for field_name, changed_value in (
            ("size", info.size + 1),
            ("sha256", "0" * 64),
        ):
            changed = dict(receipt)
            changed[field_name] = changed_value
            with open(receipt_path, "w", encoding="utf-8") as receipt_file:
                json.dump(changed, receipt_file)
            with self.subTest(field_name=field_name), self.assertRaisesRegex(
                SQLiteSnapshotError,
                "^sqlite_snapshot_conflict$",
            ):
                sqlite_snapshot.verify_completed_sqlite_snapshot(
                    run,
                    os.getuid(),
                    os.getgid(),
                )

    def test_completed_snapshot_verifier_rejects_valid_sqlite_replacement(self):
        run, _info = self.create_completed_snapshot_run()
        snapshot_path = os.path.join(
            run.path,
            "production.db.before-migration",
        )
        replacement_path = os.path.join(run.path, "replacement.db")
        replacement = sqlite3.connect(replacement_path)
        replacement.execute("CREATE TABLE replacement (value TEXT)")
        replacement.commit()
        replacement.close()
        os.chmod(replacement_path, 0o600)
        os.replace(replacement_path, snapshot_path)

        with self.assertRaisesRegex(
            SQLiteSnapshotError,
            "^sqlite_snapshot_conflict$",
        ):
            sqlite_snapshot.verify_completed_sqlite_snapshot(
                run,
                os.getuid(),
                os.getgid(),
            )

    def test_completed_snapshot_verifier_rejects_replacement_during_quick_check(self):
        run, _info = self.create_completed_snapshot_run()
        snapshot_path = os.path.join(
            run.path,
            "production.db.before-migration",
        )
        replacement_path = os.path.join(run.path, "replacement.db")
        with open(snapshot_path, "rb") as snapshot_file:
            snapshot_bytes = snapshot_file.read()
        with open(replacement_path, "wb") as replacement_file:
            replacement_file.write(snapshot_bytes)
        os.chmod(replacement_path, 0o600)
        real_connect = sqlite_snapshot.sqlite3.connect
        replaced = []

        def replace_before_connect(*arguments, **keyword_arguments):
            if not replaced:
                replaced.append(True)
                os.replace(replacement_path, snapshot_path)
            return real_connect(*arguments, **keyword_arguments)

        with mock.patch(
            "django_images.services.sqlite_snapshot.sqlite3.connect",
            side_effect=replace_before_connect,
        ), self.assertRaisesRegex(
            SQLiteSnapshotError,
            "^sqlite_snapshot_conflict$",
        ):
            sqlite_snapshot.verify_completed_sqlite_snapshot(
                run,
                os.getuid(),
                os.getgid(),
            )

    def test_completed_snapshot_verifier_rejects_replacement_during_hash(self):
        run, _info = self.create_completed_snapshot_run()
        snapshot_path = os.path.join(
            run.path,
            "production.db.before-migration",
        )
        replacement_path = os.path.join(run.path, "replacement.db")
        with open(snapshot_path, "rb") as snapshot_file:
            snapshot_bytes = snapshot_file.read()
        with open(replacement_path, "wb") as replacement_file:
            replacement_file.write(snapshot_bytes)
        os.chmod(replacement_path, 0o600)
        real_hash_descriptor = sqlite_snapshot._hash_descriptor

        def replace_before_hash(descriptor):
            os.replace(replacement_path, snapshot_path)
            return real_hash_descriptor(descriptor)

        with mock.patch(
            "django_images.services.sqlite_snapshot._hash_descriptor",
            side_effect=replace_before_hash,
        ), self.assertRaisesRegex(
            SQLiteSnapshotError,
            "^sqlite_snapshot_conflict$",
        ):
            sqlite_snapshot.verify_completed_sqlite_snapshot(
                run,
                os.getuid(),
                os.getgid(),
            )

    def test_completed_snapshot_verifier_rejects_receipt_replacement_during_read(self):
        run, _info = self.create_completed_snapshot_run()
        receipt_path = os.path.join(
            run.path,
            ".production.db.before-migration.receipt",
        )
        replacement_path = os.path.join(run.path, "replacement.receipt")
        with open(receipt_path, "rb") as receipt_file:
            receipt_bytes = receipt_file.read()
        with open(replacement_path, "wb") as replacement_file:
            replacement_file.write(receipt_bytes)
        os.chmod(replacement_path, 0o600)
        real_read_descriptor = sqlite_snapshot._read_descriptor

        def replace_before_read(descriptor):
            os.replace(replacement_path, receipt_path)
            return real_read_descriptor(descriptor)

        with mock.patch(
            "django_images.services.sqlite_snapshot._read_descriptor",
            side_effect=replace_before_read,
        ), self.assertRaisesRegex(
            SQLiteSnapshotError,
            "^sqlite_snapshot_conflict$",
        ):
            sqlite_snapshot.verify_completed_sqlite_snapshot(
                run,
                os.getuid(),
                os.getgid(),
            )

    def test_completed_snapshot_guard_rechecks_held_entries_before_commit(self):
        run, expected = self.create_completed_snapshot_run()
        guard_factory = getattr(
            sqlite_snapshot,
            "open_verified_completed_sqlite_snapshot",
            None,
        )
        self.assertIsNotNone(guard_factory)
        snapshot_path = os.path.join(
            run.path,
            "production.db.before-migration",
        )

        with guard_factory(
            run,
            os.getuid(),
            os.getgid(),
        ) as guard:
            self.assertEqual(guard.info, expected)
            os.unlink(snapshot_path)
            with self.assertRaisesRegex(
                SQLiteSnapshotError,
                "^sqlite_snapshot_conflict$",
            ):
                guard.verify_current()

    def test_completed_snapshot_guard_rechecks_receipt_before_commit(self):
        run, expected = self.create_completed_snapshot_run()
        receipt_path = os.path.join(
            run.path,
            ".production.db.before-migration.receipt",
        )

        with sqlite_snapshot.open_verified_completed_sqlite_snapshot(
            run,
            os.getuid(),
            os.getgid(),
        ) as guard:
            self.assertEqual(guard.info, expected)
            os.unlink(receipt_path)
            with self.assertRaisesRegex(
                SQLiteSnapshotError,
                "^sqlite_snapshot_conflict$",
            ):
                guard.verify_current()

    def test_completed_snapshot_guard_rechecks_temp_before_commit(self):
        run, expected = self.create_completed_snapshot_run()
        temp_path = os.path.join(
            run.path,
            ".production.db.before-migration.tmp",
        )

        with sqlite_snapshot.open_verified_completed_sqlite_snapshot(
            run,
            os.getuid(),
            os.getgid(),
        ) as guard:
            self.assertEqual(guard.info, expected)
            with open(temp_path, "wb"):
                pass
            os.chmod(temp_path, 0o600)
            with self.assertRaisesRegex(
                SQLiteSnapshotError,
                "^sqlite_snapshot_conflict$",
            ):
                guard.verify_current()

    def test_completed_snapshot_guard_supports_explicit_private_filenames(self):
        run, expected = self.create_completed_snapshot_run()
        snapshot_name = "production.db.before-orphan-cleanup"
        receipt_name = ".production.db.before-orphan-cleanup.receipt"
        for source_name, destination_name in (
            ("production.db.before-migration", snapshot_name),
            (
                ".production.db.before-migration.receipt",
                receipt_name,
            ),
        ):
            with open(
                os.path.join(run.path, source_name),
                "rb",
            ) as source_file:
                content = source_file.read()
            destination_path = os.path.join(run.path, destination_name)
            with open(destination_path, "wb") as destination_file:
                destination_file.write(content)
            os.chmod(destination_path, 0o600)

        with sqlite_snapshot.open_verified_completed_sqlite_snapshot(
            run,
            os.getuid(),
            os.getgid(),
            snapshot_filename=snapshot_name,
            receipt_filename=receipt_name,
        ) as guard:
            self.assertEqual(guard.info, expected)
            self.assertEqual(guard.verify_current(), expected)

    def test_descriptor_file_path_opens_linux_magic_link_then_binds_inode(self):
        sqlite3.connect(self.source_path).close()
        descriptor = os.open(self.source_path, os.O_RDONLY)
        self.addCleanup(os.close, descriptor)
        real_open = sqlite_snapshot.os.open
        proc_path = "/proc/self/fd/{}".format(descriptor)

        def open_linux_magic_link(path, flags, *arguments, **keywords):
            if path == proc_path:
                self.assertFalse(flags & sqlite_snapshot._NOFOLLOW)
                return os.dup(descriptor)
            return real_open(path, flags, *arguments, **keywords)

        with mock.patch(
            "django_images.services.sqlite_snapshot.os.open",
            side_effect=open_linux_magic_link,
        ):
            self.assertEqual(
                sqlite_snapshot._descriptor_file_path(descriptor),
                proc_path,
            )

    def test_orphan_cleanup_fallback_preserves_unreceipted_original(self):
        connection = sqlite3.connect(self.source_path)
        connection.execute("CREATE TABLE records (value TEXT)")
        connection.execute("INSERT INTO records VALUES ('before-cleanup')")
        connection.commit()
        connection.close()
        source_stat = os.stat(self.source_path)
        run = self.create_run(database_identity={
            "device": source_stat.st_dev,
            "inode": source_stat.st_ino,
        })
        with self.configured():
            expected = snapshot_sqlite(
                self.source_path,
                run,
                os.getuid(),
                os.getgid(),
            )
        original_path = os.path.join(
            run.path,
            "production.db.before-migration",
        )
        original_stat = os.stat(original_path)
        with open(original_path, "rb") as original_file:
            original_bytes = original_file.read()
        os.unlink(os.path.join(
            run.path,
            ".production.db.before-migration.receipt",
        ))
        checked_sizes = []

        with self.configured(), sqlite_snapshot.protect_orphan_cleanup(
            self.source_path,
            run,
            os.getuid(),
            os.getgid(),
            before_fallback_create=checked_sizes.append,
        ) as guard:
            self.assertEqual(guard.info, expected)
            self.assertEqual(guard.verify_current(), expected)

        self.assertEqual(checked_sizes, [os.path.getsize(self.source_path)])
        self.assertEqual(os.stat(original_path).st_ino, original_stat.st_ino)
        with open(original_path, "rb") as original_file:
            self.assertEqual(original_file.read(), original_bytes)
        self.assertFalse(os.path.exists(os.path.join(
            run.path,
            ".production.db.before-migration.receipt",
        )))
        cleanup_path = os.path.join(
            run.path,
            "production.db.before-orphan-cleanup",
        )
        self.assertTrue(os.path.isfile(cleanup_path))
        cleanup_stat = os.stat(cleanup_path)
        self.assertFalse(os.path.exists(os.path.join(
            run.path,
            ".production.db.before-orphan-cleanup.tmp",
        )))
        with open(os.path.join(
            run.path,
            ".production.db.before-orphan-cleanup.receipt",
        ), "r", encoding="utf-8") as receipt_file:
            self.assertEqual(json.load(receipt_file), {
                "format_version": 1,
                "kind": "missing_unreferenced_image_cleanup",
                "run_id": run.run_id,
                "sha256": expected.sha256,
                "size": expected.size,
                "source_device": expected.source_device,
                "source_inode": expected.source_inode,
            })
        reuse_checks = []
        with self.configured(), sqlite_snapshot.protect_orphan_cleanup(
            self.source_path,
            run,
            os.getuid(),
            os.getgid(),
            before_fallback_create=reuse_checks.append,
        ) as guard:
            self.assertEqual(guard.info, expected)
        self.assertEqual(reuse_checks, [])
        self.assertEqual(os.stat(cleanup_path).st_ino, cleanup_stat.st_ino)

    def test_orphan_cleanup_fallback_recovers_receipted_temp(self):
        connection = sqlite3.connect(self.source_path)
        connection.execute("CREATE TABLE records (value TEXT)")
        connection.commit()
        connection.close()
        run, expected = self.create_completed_snapshot_run()
        os.unlink(os.path.join(
            run.path,
            ".production.db.before-migration.receipt",
        ))
        first_checks = []
        with self.configured(), mock.patch(
            "django_images.services.sqlite_snapshot._promote_snapshot",
            side_effect=SQLiteSnapshotError(
                "simulated_cleanup_promotion_crash"
            ),
        ), self.assertRaisesRegex(
            SQLiteSnapshotError,
            "^simulated_cleanup_promotion_crash$",
        ):
            with sqlite_snapshot.protect_orphan_cleanup(
                self.source_path,
                run,
                os.getuid(),
                os.getgid(),
                before_fallback_create=first_checks.append,
            ):
                pass

        self.assertEqual(first_checks, [os.path.getsize(self.source_path)])
        self.assertTrue(os.path.isfile(os.path.join(
            run.path,
            ".production.db.before-orphan-cleanup.tmp",
        )))
        self.assertTrue(os.path.isfile(os.path.join(
            run.path,
            ".production.db.before-orphan-cleanup.receipt",
        )))
        retry_checks = []
        with self.configured(), sqlite_snapshot.protect_orphan_cleanup(
            self.source_path,
            run,
            os.getuid(),
            os.getgid(),
            before_fallback_create=retry_checks.append,
        ) as guard:
            self.assertEqual(guard.info, expected)

        self.assertEqual(retry_checks, [])
        self.assertTrue(os.path.isfile(os.path.join(
            run.path,
            "production.db.before-orphan-cleanup",
        )))
        self.assertFalse(os.path.exists(os.path.join(
            run.path,
            ".production.db.before-orphan-cleanup.tmp",
        )))

    def test_orphan_cleanup_fallback_recreates_unreceipted_temp(self):
        sqlite3.connect(self.source_path).close()
        run, expected = self.create_completed_snapshot_run()
        os.unlink(os.path.join(
            run.path,
            ".production.db.before-migration.receipt",
        ))
        temp_path = os.path.join(
            run.path,
            ".production.db.before-orphan-cleanup.tmp",
        )
        with open(temp_path, "wb") as temp_file:
            temp_file.write(b"interrupted")
        os.chmod(temp_path, 0o600)
        stale_inode = os.stat(temp_path).st_ino

        with self.configured(), sqlite_snapshot.protect_orphan_cleanup(
            self.source_path,
            run,
            os.getuid(),
            os.getgid(),
        ) as guard:
            self.assertEqual(guard.info, expected)

        self.assertFalse(os.path.exists(temp_path))
        final_path = os.path.join(
            run.path,
            "production.db.before-orphan-cleanup",
        )
        self.assertNotEqual(os.stat(final_path).st_ino, stale_inode)

    def test_orphan_cleanup_fallback_rejects_other_artifact_combinations(self):
        sqlite3.connect(self.source_path).close()
        run, info = self.create_completed_snapshot_run()
        original_path = os.path.join(
            run.path,
            "production.db.before-migration",
        )
        with open(original_path, "rb") as original_file:
            snapshot_bytes = original_file.read()
        os.unlink(os.path.join(
            run.path,
            ".production.db.before-migration.receipt",
        ))
        cleanup_names = (
            "production.db.before-orphan-cleanup",
            ".production.db.before-orphan-cleanup.tmp",
            ".production.db.before-orphan-cleanup.receipt",
        )
        receipt = {
            "format_version": 1,
            "kind": "missing_unreferenced_image_cleanup",
            "run_id": run.run_id,
            "sha256": info.sha256,
            "size": info.size,
            "source_device": info.source_device,
            "source_inode": info.source_inode,
        }
        for combination in (
            (True, False, False),
            (False, False, True),
            (True, True, False),
            (True, True, True),
        ):
            for filename in cleanup_names:
                try:
                    os.unlink(os.path.join(run.path, filename))
                except FileNotFoundError:
                    pass
            for present, filename in zip(combination, cleanup_names):
                if not present:
                    continue
                path = os.path.join(run.path, filename)
                if filename.endswith(".receipt"):
                    with open(path, "w", encoding="utf-8") as artifact:
                        json.dump(receipt, artifact)
                else:
                    with open(path, "wb") as artifact:
                        artifact.write(snapshot_bytes)
                os.chmod(path, 0o600)
            with self.subTest(combination=combination), self.configured(), self.assertRaisesRegex(
                SQLiteSnapshotError,
                "^sqlite_snapshot_conflict$",
            ):
                with sqlite_snapshot.protect_orphan_cleanup(
                    self.source_path,
                    run,
                    os.getuid(),
                    os.getgid(),
                ):
                    pass

    def test_orphan_cleanup_invalid_original_receipt_forbids_fallback(self):
        sqlite3.connect(self.source_path).close()
        run, _info = self.create_completed_snapshot_run()
        receipt_path = os.path.join(
            run.path,
            ".production.db.before-migration.receipt",
        )
        with open(receipt_path, "w", encoding="utf-8") as receipt_file:
            receipt_file.write("{}")

        with self.configured(), self.assertRaisesRegex(
            SQLiteSnapshotError,
            "^sqlite_snapshot_conflict$",
        ):
            with sqlite_snapshot.protect_orphan_cleanup(
                self.source_path,
                run,
                os.getuid(),
                os.getgid(),
            ):
                pass

        self.assertFalse(os.path.exists(os.path.join(
            run.path,
            "production.db.before-orphan-cleanup",
        )))

    def test_completed_snapshot_verifier_runs_quick_check(self):
        run, _info = self.create_completed_snapshot_run()
        snapshot_path = os.path.join(
            run.path,
            "production.db.before-migration",
        )
        with open(snapshot_path, "wb") as snapshot_file:
            snapshot_file.write(b"not-sqlite")

        with self.assertRaisesRegex(
            SQLiteSnapshotError,
            "^corrupt_sqlite_snapshot$",
        ):
            sqlite_snapshot.verify_completed_sqlite_snapshot(
                run,
                os.getuid(),
                os.getgid(),
            )

    def test_completed_snapshot_verifier_rejects_run_path_replacement(self):
        run, _info = self.create_completed_snapshot_run()
        moved_path = "{}.moved".format(run.path)
        os.rename(run.path, moved_path)
        os.mkdir(run.path, 0o700)
        try:
            with self.assertRaisesRegex(
                SQLiteSnapshotError,
                "^sqlite_snapshot_state_invalid$",
            ):
                sqlite_snapshot.verify_completed_sqlite_snapshot(
                    run,
                    os.getuid(),
                    os.getgid(),
                )
        finally:
            os.rmdir(run.path)
            os.rename(moved_path, run.path)

    def test_missing_database_is_a_noop(self):
        run = self.create_run()
        with self.configured():
            self.assertIsNone(snapshot_sqlite(
                self.source_path,
                run,
                os.getuid(),
                os.getgid(),
            ))
        self.assertFalse(os.path.exists(os.path.join(
            run.path,
            "production.db.before-migration",
        )))

    def test_source_symlink_and_non_regular_file_have_distinct_reasons(self):
        real_database = os.path.join(self.data_root, "real.db")
        sqlite3.connect(real_database).close()
        os.symlink(real_database, self.source_path)
        run = self.create_run()
        with self.configured(), self.assertRaisesRegex(
            SQLiteSnapshotError,
            "^unsafe_sqlite_source_symlink$",
        ):
            snapshot_sqlite(
                self.source_path,
                run,
                os.getuid(),
                os.getgid(),
            )

        os.unlink(self.source_path)
        os.mkdir(self.source_path)
        with self.configured(), self.assertRaisesRegex(
            SQLiteSnapshotError,
            "^unsafe_sqlite_source_non_regular$",
        ):
            snapshot_sqlite(
                self.source_path,
                run,
                os.getuid(),
                os.getgid(),
            )

    def test_source_hardlink_is_rejected(self):
        sqlite3.connect(self.source_path).close()
        os.link(
            self.source_path,
            os.path.join(self.data_root, "production-hardlink.db"),
        )
        run = self.create_run()

        with self.configured(), self.assertRaisesRegex(
            SQLiteSnapshotError,
            "^unsafe_sqlite_source_hardlink$",
        ):
            snapshot_sqlite(
                self.source_path,
                run,
                os.getuid(),
                os.getgid(),
            )

    def test_source_outside_data_root_is_rejected_without_disclosure(self):
        outside_directory = tempfile.TemporaryDirectory()
        self.addCleanup(outside_directory.cleanup)
        outside_path = os.path.join(
            os.path.realpath(outside_directory.name),
            "secret.db",
        )
        sqlite3.connect(outside_path).close()
        run = self.create_run()

        with self.configured(databases={
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": outside_path,
            },
        }), self.assertRaisesRegex(
            SQLiteSnapshotError,
            "^sqlite_source_outside_data_root$",
        ) as captured:
            snapshot_sqlite(
                outside_path,
                run,
                os.getuid(),
                os.getgid(),
            )
        self.assertNotIn(outside_path, str(captured.exception))

    def test_unsupported_backend_is_rejected_with_stable_reason(self):
        sqlite3.connect(self.source_path).close()
        run = self.create_run()
        with self.configured(databases={
            "default": {
                "ENGINE": "django.db.backends.postgresql",
                "NAME": self.source_path,
            },
        }), self.assertRaisesRegex(
            SQLiteSnapshotError,
            "^unsupported_legacy_database_backend$",
        ):
            snapshot_sqlite(
                self.source_path,
                run,
                os.getuid(),
                os.getgid(),
            )

    def test_corrupt_existing_snapshot_is_rejected(self):
        sqlite3.connect(self.source_path).close()
        run = self.create_run()
        snapshot_path = os.path.join(
            run.path,
            "production.db.before-migration",
        )
        with open(snapshot_path, "wb") as snapshot_file:
            snapshot_file.write(b"not-sqlite")
        os.chmod(snapshot_path, 0o600)

        with self.configured(), self.assertRaisesRegex(
            SQLiteSnapshotError,
            "^corrupt_sqlite_snapshot$",
        ):
            snapshot_sqlite(
                self.source_path,
                run,
                os.getuid(),
                os.getgid(),
            )

    def test_snapshot_hardlink_is_rejected(self):
        sqlite3.connect(self.source_path).close()
        run = self.create_run()
        outside_snapshot = os.path.join(self.data_root, "outside-snapshot.db")
        source = sqlite3.connect(
            "file:{}?mode=ro".format(self.source_path),
            uri=True,
        )
        destination = sqlite3.connect(outside_snapshot)
        source.backup(destination)
        destination.close()
        source.close()
        os.chmod(outside_snapshot, 0o600)
        os.link(outside_snapshot, os.path.join(
            run.path,
            "production.db.before-migration",
        ))

        with self.configured(), self.assertRaisesRegex(
            SQLiteSnapshotError,
            "^unsafe_sqlite_snapshot$",
        ):
            snapshot_sqlite(
                self.source_path,
                run,
                os.getuid(),
                os.getgid(),
            )

    def test_final_and_temp_snapshot_collision_fails_closed(self):
        sqlite3.connect(self.source_path).close()
        run = self.create_run()
        source = sqlite3.connect(
            "file:{}?mode=ro".format(self.source_path),
            uri=True,
        )
        for filename in (
            "production.db.before-migration",
            ".production.db.before-migration.tmp",
        ):
            destination_path = os.path.join(run.path, filename)
            destination = sqlite3.connect(destination_path)
            source.backup(destination)
            destination.close()
            os.chmod(destination_path, 0o600)
        source.close()

        with self.configured(), self.assertRaisesRegex(
            SQLiteSnapshotError,
            "^sqlite_snapshot_conflict$",
        ):
            snapshot_sqlite(
                self.source_path,
                run,
                os.getuid(),
                os.getgid(),
            )

    def test_unreceipted_crash_temp_is_recreated_once(self):
        connection = sqlite3.connect(self.source_path)
        connection.execute("CREATE TABLE records (value TEXT)")
        connection.execute("INSERT INTO records VALUES ('once')")
        connection.commit()
        connection.close()
        run = self.create_run()
        temp_path = os.path.join(
            run.path,
            ".production.db.before-migration.tmp",
        )
        source = sqlite3.connect(
            "file:{}?mode=ro".format(self.source_path),
            uri=True,
        )
        destination = sqlite3.connect(temp_path)
        source.backup(destination)
        destination.close()
        source.close()
        os.chmod(temp_path, 0o600)

        original_copy = sqlite_snapshot._copy_database
        with self.configured(), mock.patch(
            "django_images.services.sqlite_snapshot._copy_database",
            wraps=original_copy,
        ) as copy_database:
            first = snapshot_sqlite(
                self.source_path,
                run,
                os.getuid(),
                os.getgid(),
            )
            second = snapshot_sqlite(
                self.source_path,
                run,
                os.getuid(),
                os.getgid(),
            )

        copy_database.assert_called_once()
        self.assertEqual(first, second)
        self.assertFalse(os.path.exists(temp_path))
        self.assertTrue(os.path.isfile(os.path.join(
            run.path,
            "production.db.before-migration",
        )))

    def test_unreceipted_final_is_left_for_separate_cleanup_snapshot(self):
        connection = sqlite3.connect(self.source_path)
        connection.execute("CREATE TABLE records (value TEXT)")
        connection.commit()
        connection.close()
        run = self.create_run()
        final_path = os.path.join(
            run.path,
            "production.db.before-migration",
        )
        source = sqlite3.connect(
            "file:{}?mode=ro".format(self.source_path),
            uri=True,
        )
        destination = sqlite3.connect(final_path)
        source.backup(destination)
        destination.close()
        source.close()
        os.chmod(final_path, 0o600)

        with self.configured():
            info = snapshot_sqlite(
                self.source_path,
                run,
                os.getuid(),
                os.getgid(),
            )

        receipt_path = os.path.join(
            run.path,
            ".production.db.before-migration.receipt",
        )
        self.assertFalse(os.path.exists(receipt_path))
        self.assertEqual(info.size, os.path.getsize(final_path))

    def test_interrupted_zero_byte_temp_without_receipt_is_recreated(self):
        connection = sqlite3.connect(self.source_path)
        connection.execute("CREATE TABLE records (value TEXT)")
        connection.execute("INSERT INTO records VALUES ('completed')")
        connection.commit()
        connection.close()
        run = self.create_run()
        real_copy = sqlite_snapshot._copy_database
        copy_attempts = []

        def interrupt_first_backup(*arguments):
            copy_attempts.append(True)
            if len(copy_attempts) == 1:
                raise sqlite3.OperationalError("interrupted backup")
            return real_copy(*arguments)

        with self.configured(), mock.patch(
            "django_images.services.sqlite_snapshot._copy_database",
            side_effect=interrupt_first_backup,
        ):
            with self.assertRaisesRegex(
                SQLiteSnapshotError,
                "^sqlite_snapshot_failed$",
            ):
                snapshot_sqlite(
                    self.source_path,
                    run,
                    os.getuid(),
                    os.getgid(),
                )

            temp_path = os.path.join(
                run.path,
                ".production.db.before-migration.tmp",
            )
            self.assertEqual(os.path.getsize(temp_path), 0)
            self.assertFalse(os.path.exists(os.path.join(
                run.path,
                ".production.db.before-migration.receipt",
            )))
            info = snapshot_sqlite(
                self.source_path,
                run,
                os.getuid(),
                os.getgid(),
            )

        self.assertEqual(len(copy_attempts), 2)
        self.assertGreater(info.size, 0)
        snapshot = sqlite3.connect(os.path.join(
            run.path,
            "production.db.before-migration",
        ))
        self.addCleanup(snapshot.close)
        self.assertEqual(
            snapshot.execute("SELECT value FROM records").fetchall(),
            [("completed",)],
        )

    def test_unreceipted_temp_removal_is_relative_and_parent_fsynced(self):
        sqlite3.connect(self.source_path).close()
        run = self.create_run()
        temp_name = ".production.db.before-migration.tmp"
        temp_path = os.path.join(run.path, temp_name)
        with open(temp_path, "wb"):
            pass
        os.chmod(temp_path, 0o600)
        events = []
        real_unlink = os.unlink
        real_fsync = os.fsync

        def record_unlink(path, *args, **kwargs):
            result = real_unlink(path, *args, **kwargs)
            if path == temp_name:
                events.append(("unlink", kwargs.get("dir_fd")))
            return result

        def record_fsync(descriptor):
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                events.append(("directory-fsync", descriptor))
            return real_fsync(descriptor)

        with self.configured(), mock.patch(
            "django_images.services.sqlite_snapshot.os.unlink",
            side_effect=record_unlink,
        ), mock.patch(
            "django_images.services.sqlite_snapshot.os.fsync",
            side_effect=record_fsync,
        ), mock.patch(
            "django_images.services.sqlite_snapshot._create_snapshot_temp",
            side_effect=SQLiteSnapshotError("stop_after_cleanup"),
        ), self.assertRaisesRegex(
            SQLiteSnapshotError,
            "^stop_after_cleanup$",
        ):
            snapshot_sqlite(
                self.source_path,
                run,
                os.getuid(),
                os.getgid(),
            )

        unlink_event = next(event for event in events if event[0] == "unlink")
        self.assertIsNotNone(unlink_event[1])
        unlink_index = events.index(unlink_event)
        self.assertIn(
            ("directory-fsync", unlink_event[1]),
            events[unlink_index + 1:],
        )
        self.assertFalse(os.path.exists(temp_path))

    def test_completed_empty_backup_receipt_allows_temp_recovery(self):
        sqlite3.connect(self.source_path).close()
        self.assertEqual(os.path.getsize(self.source_path), 0)
        source_stat = os.stat(self.source_path)
        run = self.create_run()
        receipt_path = os.path.join(
            run.path,
            ".production.db.before-migration.receipt",
        )

        with self.configured(), mock.patch(
            "django_images.services.sqlite_snapshot._promote_snapshot",
            side_effect=SQLiteSnapshotError("simulated_promotion_crash"),
        ), self.assertRaisesRegex(
            SQLiteSnapshotError,
            "^simulated_promotion_crash$",
        ):
            snapshot_sqlite(
                self.source_path,
                run,
                os.getuid(),
                os.getgid(),
            )

        self.assertTrue(os.path.isfile(receipt_path))
        with open(receipt_path, "r", encoding="utf-8") as receipt_file:
            receipt = json.load(receipt_file)
        temp_path = os.path.join(
            run.path,
            ".production.db.before-migration.tmp",
        )
        with open(temp_path, "rb") as snapshot_file:
            snapshot_bytes = snapshot_file.read()
        self.assertEqual(receipt, {
            "sha256": hashlib.sha256(snapshot_bytes).hexdigest(),
            "size": len(snapshot_bytes),
            "source_device": source_stat.st_dev,
            "source_inode": source_stat.st_ino,
        })

        with self.configured(), mock.patch(
            "django_images.services.sqlite_snapshot._copy_database",
        ) as copy_database:
            info = snapshot_sqlite(
                self.source_path,
                run,
                os.getuid(),
                os.getgid(),
            )

        copy_database.assert_not_called()
        self.assertEqual(info.size, len(snapshot_bytes))
        self.assertEqual(info.sha256, hashlib.sha256(snapshot_bytes).hexdigest())
        self.assertTrue(os.path.isfile(receipt_path))
        with open(receipt_path, "r", encoding="utf-8") as receipt_file:
            self.assertEqual(json.load(receipt_file), receipt)
        self.assertTrue(os.path.isfile(os.path.join(
            run.path,
            "production.db.before-migration",
        )))

    def test_completion_receipt_is_private_exclusive_and_durable(self):
        connection = sqlite3.connect(self.source_path)
        connection.execute("CREATE TABLE records (value TEXT)")
        connection.commit()
        connection.close()
        run = self.create_run()
        receipt_name = ".production.db.before-migration.receipt"
        receipt_path = os.path.join(run.path, receipt_name)
        receipt_descriptors = set()
        events = []
        real_open = os.open
        real_fsync = os.fsync

        def record_open(path, flags, mode=0o777, *args, **kwargs):
            descriptor = real_open(path, flags, mode, *args, **kwargs)
            if path == receipt_name:
                receipt_descriptors.add(descriptor)
                events.append(("receipt-open", flags, mode))
            return descriptor

        def record_fsync(descriptor):
            if descriptor in receipt_descriptors:
                events.append(("receipt-fsync",))
            elif stat.S_ISDIR(os.fstat(descriptor).st_mode):
                events.append(("directory-fsync",))
            return real_fsync(descriptor)

        with self.configured(), mock.patch(
            "django_images.services.sqlite_snapshot.os.open",
            side_effect=record_open,
        ), mock.patch(
            "django_images.services.sqlite_snapshot.os.fsync",
            side_effect=record_fsync,
        ), mock.patch(
            "django_images.services.sqlite_snapshot._promote_snapshot",
            side_effect=SQLiteSnapshotError("simulated_promotion_crash"),
        ), self.assertRaisesRegex(
            SQLiteSnapshotError,
            "^simulated_promotion_crash$",
        ):
            snapshot_sqlite(
                self.source_path,
                run,
                os.getuid(),
                os.getgid(),
            )

        receipt_opens = [
            event for event in events if event[0] == "receipt-open"
        ]
        exclusive_receipt_opens = [
            event for event in receipt_opens if event[1] & os.O_EXCL
        ]
        self.assertEqual(len(exclusive_receipt_opens), 1)
        receipt_open = exclusive_receipt_opens[0]
        self.assertEqual(receipt_open[2], 0o600)
        event_names = [event[0] for event in events]
        receipt_fsync_index = event_names.index("receipt-fsync")
        self.assertIn("directory-fsync", event_names[receipt_fsync_index + 1:])
        receipt_stat = os.stat(receipt_path)
        self.assertEqual(stat.S_IMODE(receipt_stat.st_mode), 0o600)
        self.assertEqual(receipt_stat.st_uid, os.getuid())
        self.assertEqual(receipt_stat.st_gid, os.getgid())

    def test_receipted_final_is_reverified_and_receipt_is_retained(self):
        connection = sqlite3.connect(self.source_path)
        connection.execute("CREATE TABLE records (value TEXT)")
        connection.commit()
        connection.close()
        run = self.create_run()
        receipt_path = os.path.join(
            run.path,
            ".production.db.before-migration.receipt",
        )

        with self.configured():
            first = snapshot_sqlite(
                self.source_path,
                run,
                os.getuid(),
                os.getgid(),
            )

        self.assertTrue(os.path.isfile(os.path.join(
            run.path,
            "production.db.before-migration",
        )))
        self.assertTrue(os.path.isfile(receipt_path))
        with open(receipt_path, "rb") as receipt_file:
            original_receipt = receipt_file.read()
        with self.configured(), mock.patch(
            "django_images.services.sqlite_snapshot._copy_database",
        ) as copy_database:
            second = snapshot_sqlite(
                self.source_path,
                run,
                os.getuid(),
                os.getgid(),
            )

        copy_database.assert_not_called()
        self.assertEqual(second, first)
        with open(receipt_path, "rb") as receipt_file:
            self.assertEqual(receipt_file.read(), original_receipt)

    def test_receipted_final_resume_rejects_snapshot_replacement_after_check(self):
        sqlite3.connect(self.source_path).close()
        run = self.create_run()
        with self.configured():
            snapshot_sqlite(
                self.source_path,
                run,
                os.getuid(),
                os.getgid(),
            )
        snapshot_path = os.path.join(
            run.path,
            "production.db.before-migration",
        )
        replacement_path = os.path.join(run.path, "replacement.db")
        with open(snapshot_path, "rb") as snapshot_file:
            snapshot_bytes = snapshot_file.read()
        with open(replacement_path, "wb") as replacement_file:
            replacement_file.write(snapshot_bytes)
        os.chmod(replacement_path, 0o600)
        real_verify_run = sqlite_snapshot._verify_named_run_identity
        replaced = []

        def replace_after_run_check(*arguments, **keywords):
            result = real_verify_run(*arguments, **keywords)
            if not replaced:
                replaced.append(True)
                os.replace(replacement_path, snapshot_path)
            return result

        with self.configured(), mock.patch(
            "django_images.services.sqlite_snapshot._verify_named_run_identity",
            side_effect=replace_after_run_check,
        ), self.assertRaisesRegex(
            SQLiteSnapshotError,
            "^sqlite_snapshot_conflict$",
        ):
            snapshot_sqlite(
                self.source_path,
                run,
                os.getuid(),
                os.getgid(),
            )

    def test_receipted_final_resume_rejects_receipt_replacement_after_check(self):
        sqlite3.connect(self.source_path).close()
        run = self.create_run()
        with self.configured():
            snapshot_sqlite(
                self.source_path,
                run,
                os.getuid(),
                os.getgid(),
            )
        receipt_path = os.path.join(
            run.path,
            ".production.db.before-migration.receipt",
        )
        replacement_path = os.path.join(run.path, "replacement.receipt")
        with open(receipt_path, "rb") as receipt_file:
            receipt_bytes = receipt_file.read()
        with open(replacement_path, "wb") as replacement_file:
            replacement_file.write(receipt_bytes)
        os.chmod(replacement_path, 0o600)
        real_verify_run = sqlite_snapshot._verify_named_run_identity
        replaced = []

        def replace_after_run_check(*arguments, **keywords):
            result = real_verify_run(*arguments, **keywords)
            if not replaced:
                replaced.append(True)
                os.replace(replacement_path, receipt_path)
            return result

        with self.configured(), mock.patch(
            "django_images.services.sqlite_snapshot._verify_named_run_identity",
            side_effect=replace_after_run_check,
        ), self.assertRaisesRegex(
            SQLiteSnapshotError,
            "^sqlite_snapshot_conflict$",
        ):
            snapshot_sqlite(
                self.source_path,
                run,
                os.getuid(),
                os.getgid(),
            )

    def test_receipt_metadata_mismatch_fails_closed(self):
        connection = sqlite3.connect(self.source_path)
        connection.execute("CREATE TABLE records (value TEXT)")
        connection.commit()
        connection.close()
        run = self.create_run()
        receipt_path = os.path.join(
            run.path,
            ".production.db.before-migration.receipt",
        )

        with self.configured(), mock.patch(
            "django_images.services.sqlite_snapshot._promote_snapshot",
            side_effect=SQLiteSnapshotError("simulated_promotion_crash"),
        ), self.assertRaisesRegex(
            SQLiteSnapshotError,
            "^simulated_promotion_crash$",
        ):
            snapshot_sqlite(
                self.source_path,
                run,
                os.getuid(),
                os.getgid(),
            )

        self.assertTrue(os.path.isfile(receipt_path))
        with open(receipt_path, "r", encoding="utf-8") as receipt_file:
            receipt = json.load(receipt_file)
        mutations = (
            ("size", receipt["size"] + 1),
            ("sha256", "0" * 64),
            ("source_device", receipt["source_device"] + 1),
            ("source_inode", receipt["source_inode"] + 1),
            ("unexpected", 1),
        )
        for field_name, value in mutations:
            changed = dict(receipt)
            changed[field_name] = value
            with open(receipt_path, "w", encoding="utf-8") as receipt_file:
                json.dump(changed, receipt_file)
            with self.subTest(field_name=field_name), self.configured(), self.assertRaisesRegex(
                SQLiteSnapshotError,
                "^sqlite_snapshot_conflict$",
            ):
                snapshot_sqlite(
                    self.source_path,
                    run,
                    os.getuid(),
                    os.getgid(),
                )

        self.assertFalse(os.path.exists(os.path.join(
            run.path,
            "production.db.before-migration",
        )))

    def test_receipt_without_snapshot_is_a_conflict(self):
        sqlite3.connect(self.source_path).close()
        source_stat = os.stat(self.source_path)
        run = self.create_run()
        receipt_path = os.path.join(
            run.path,
            ".production.db.before-migration.receipt",
        )
        with open(receipt_path, "w", encoding="utf-8") as receipt_file:
            json.dump({
                "sha256": hashlib.sha256(b"").hexdigest(),
                "size": 0,
                "source_device": source_stat.st_dev,
                "source_inode": source_stat.st_ino,
            }, receipt_file)
        os.chmod(receipt_path, 0o600)

        with self.configured(), self.assertRaisesRegex(
            SQLiteSnapshotError,
            "^sqlite_snapshot_conflict$",
        ):
            snapshot_sqlite(
                self.source_path,
                run,
                os.getuid(),
                os.getgid(),
            )

    def test_snapshot_chown_failure_leaves_no_root_owned_temp(self):
        sqlite3.connect(self.source_path).close()
        run = self.create_run()

        with self.configured(), mock.patch(
            "django_images.services.sqlite_snapshot.os.fchown",
            side_effect=OSError("private-owner-error"),
        ), self.assertRaisesRegex(
            SQLiteSnapshotError,
            "^sqlite_snapshot_failed$",
        ):
            snapshot_sqlite(
                self.source_path,
                run,
                os.getuid(),
                os.getgid(),
            )

        self.assertFalse(os.path.exists(os.path.join(
            run.path,
            ".production.db.before-migration.tmp",
        )))

    def test_snapshot_rejects_service_identity_mismatch_before_creation(self):
        sqlite3.connect(self.source_path).close()
        run = self.create_run()

        with self.configured(), self.assertRaisesRegex(
            SQLiteSnapshotError,
            "^sqlite_snapshot_state_invalid$",
        ):
            snapshot_sqlite(
                self.source_path,
                run,
                os.getuid() + 1,
                os.getgid(),
            )

        self.assertFalse(os.path.exists(os.path.join(
            run.path,
            ".production.db.before-migration.tmp",
        )))

    def test_run_path_swap_cannot_redirect_snapshot_bytes(self):
        connection = sqlite3.connect(self.source_path)
        connection.execute("CREATE TABLE records (value TEXT)")
        connection.commit()
        connection.close()
        run = self.create_run()
        original_copy = sqlite_snapshot._copy_database
        moved_path = "{}.moved".format(run.path)

        def swap_run_path(source_path, *destination):
            os.rename(run.path, moved_path)
            os.mkdir(run.path, 0o700)
            return original_copy(source_path, *destination)

        with self.configured(), mock.patch(
            "django_images.services.sqlite_snapshot._copy_database",
            side_effect=swap_run_path,
        ), self.assertRaisesRegex(
            SQLiteSnapshotError,
            "^sqlite_snapshot_state_invalid$",
        ):
            snapshot_sqlite(
                self.source_path,
                run,
                os.getuid(),
                os.getgid(),
            )

        self.assertEqual(os.listdir(run.path), [])

    def test_source_identity_change_does_not_leave_adoptable_temp(self):
        sqlite3.connect(self.source_path).close()
        run = self.create_run()
        original_copy = sqlite_snapshot._copy_database
        moved_source = "{}.moved".format(self.source_path)

        def swap_source_after_backup(*arguments):
            result = original_copy(*arguments)
            os.rename(self.source_path, moved_source)
            sqlite3.connect(self.source_path).close()
            return result

        with self.configured(), mock.patch(
            "django_images.services.sqlite_snapshot._copy_database",
            side_effect=swap_source_after_backup,
        ), self.assertRaisesRegex(
            SQLiteSnapshotError,
            "^sqlite_source_identity_changed$",
        ):
            snapshot_sqlite(
                self.source_path,
                run,
                os.getuid(),
                os.getgid(),
            )

        self.assertFalse(os.path.exists(os.path.join(
            run.path,
            ".production.db.before-migration.tmp",
        )))

    def test_snapshot_intent_rejects_replaced_source_on_resume(self):
        sqlite3.connect(self.source_path).close()
        original_stat = os.stat(self.source_path)
        run = resolve_or_create_run(
            scan_run_inventory(self.backup_root),
            {
                "present": True,
                "database_identity": None,
                "media_root_identity": None,
            },
            False,
            "source-commit",
            os.getuid(),
            os.getgid(),
        )
        transition_state(
            run,
            "initialized",
            "snapshot_intent",
            intent={
                "kind": "sqlite_snapshot",
                "source_device": original_stat.st_dev,
                "source_inode": original_stat.st_ino,
            },
        )
        os.rename(self.source_path, "{}.old".format(self.source_path))
        sqlite3.connect(self.source_path).close()

        with self.configured(), self.assertRaisesRegex(
            SQLiteSnapshotError,
            "^sqlite_source_identity_changed$",
        ):
            snapshot_sqlite(
                self.source_path,
                run,
                os.getuid(),
                os.getgid(),
            )

    def test_snapshot_rejects_run_outside_configured_backup_root(self):
        sqlite3.connect(self.source_path).close()
        source_stat = os.stat(self.source_path)
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        outside_root = os.path.realpath(outside.name)
        outside_backup = os.path.join(outside_root, "legacy-backup")
        os.mkdir(outside_backup, 0o700)
        with self.settings(PINRY_DATA_ROOT=outside_root):
            run = resolve_or_create_run(
                scan_run_inventory(outside_backup),
                {
                    "present": True,
                    "database_identity": None,
                    "media_root_identity": None,
                },
                False,
                "source-commit",
                os.getuid(),
                os.getgid(),
            )
            transition_state(
                run,
                "initialized",
                "snapshot_intent",
                intent={
                    "kind": "sqlite_snapshot",
                    "source_device": source_stat.st_dev,
                    "source_inode": source_stat.st_ino,
                },
            )

        with self.configured(), self.assertRaisesRegex(
            SQLiteSnapshotError,
            "^sqlite_snapshot_state_invalid$",
        ):
            snapshot_sqlite(
                self.source_path,
                run,
                os.getuid(),
                os.getgid(),
            )
        self.assertFalse(os.path.exists(os.path.join(
            run.path,
            "production.db.before-migration",
        )))

    def test_source_swap_and_inode_restore_during_backup_fails_closed(self):
        trusted = sqlite3.connect(self.source_path)
        trusted.execute("CREATE TABLE records (value TEXT)")
        trusted.execute("INSERT INTO records VALUES ('trusted')")
        trusted.commit()
        trusted.close()
        decoy_path = os.path.join(self.data_root, "decoy.db")
        decoy = sqlite3.connect(decoy_path)
        decoy.execute("CREATE TABLE records (value TEXT)")
        decoy.execute("INSERT INTO records VALUES ('decoy')")
        decoy.commit()
        decoy.close()
        run = self.create_run()
        original_copy = sqlite_snapshot._copy_database
        held_path = "{}.held".format(self.source_path)

        def swap_during_backup(*arguments):
            os.rename(self.source_path, held_path)
            os.rename(decoy_path, self.source_path)
            try:
                return original_copy(*arguments)
            finally:
                os.rename(self.source_path, decoy_path)
                os.rename(held_path, self.source_path)

        with self.configured(), mock.patch(
            "django_images.services.sqlite_snapshot._copy_database",
            side_effect=swap_during_backup,
        ), self.assertRaisesRegex(
            SQLiteSnapshotError,
            "^sqlite_source_identity_changed$",
        ):
            snapshot_sqlite(
                self.source_path,
                run,
                os.getuid(),
                os.getgid(),
            )

        self.assertFalse(os.path.exists(os.path.join(
            run.path,
            "production.db.before-migration",
        )))

    def test_source_swap_before_connect_and_restore_after_connect_fails_closed(self):
        trusted = sqlite3.connect(self.source_path)
        trusted.execute("CREATE TABLE records (value TEXT)")
        trusted.execute("INSERT INTO records VALUES ('trusted')")
        trusted.commit()
        trusted.close()
        decoy_path = os.path.join(self.data_root, "decoy.db")
        decoy = sqlite3.connect(decoy_path)
        decoy.execute("CREATE TABLE records (value TEXT)")
        decoy.execute("INSERT INTO records VALUES ('decoy')")
        decoy.commit()
        decoy.close()
        run = self.create_run()
        real_connect = sqlite3.connect
        held_path = "{}.held".format(self.source_path)
        source_opened = []

        def swap_around_source_connect(*arguments, **keywords):
            uri = arguments[0] if arguments else keywords.get("database", "")
            if not source_opened and "mode=ro" in uri:
                source_opened.append(True)
                os.rename(self.source_path, held_path)
                os.rename(decoy_path, self.source_path)
                try:
                    return real_connect(*arguments, **keywords)
                finally:
                    os.rename(self.source_path, decoy_path)
                    os.rename(held_path, self.source_path)
            return real_connect(*arguments, **keywords)

        with self.configured(), mock.patch(
            "django_images.services.sqlite_snapshot.sqlite3.connect",
            side_effect=swap_around_source_connect,
        ), self.assertRaisesRegex(
            SQLiteSnapshotError,
            "^sqlite_source_identity_changed$",
        ):
            snapshot_sqlite(
                self.source_path,
                run,
                os.getuid(),
                os.getgid(),
            )

        self.assertEqual(source_opened, [True])
        self.assertFalse(os.path.exists(os.path.join(
            run.path,
            "production.db.before-migration",
        )))

    def test_unrelated_matching_fd_cannot_authorize_decoy_connection(self):
        trusted = sqlite3.connect(self.source_path)
        trusted.execute("CREATE TABLE records (value TEXT)")
        trusted.execute("INSERT INTO records VALUES ('trusted')")
        trusted.commit()
        trusted.close()
        decoy_path = os.path.join(self.data_root, "decoy.db")
        decoy = sqlite3.connect(decoy_path)
        decoy.execute("CREATE TABLE records (value TEXT)")
        decoy.execute("INSERT INTO records VALUES ('decoy')")
        decoy.commit()
        decoy.close()
        run = self.create_run()
        real_connect = sqlite3.connect
        held_path = "{}.held".format(self.source_path)
        unrelated_descriptors = []

        def swap_and_open_unrelated_matching_fd(*arguments, **keywords):
            uri = arguments[0] if arguments else keywords.get("database", "")
            if not unrelated_descriptors and "mode=ro" in uri:
                os.rename(self.source_path, held_path)
                os.rename(decoy_path, self.source_path)
                try:
                    connection = real_connect(*arguments, **keywords)
                finally:
                    os.rename(self.source_path, decoy_path)
                    os.rename(held_path, self.source_path)
                unrelated_descriptors.append(os.open(
                    self.source_path,
                    os.O_RDONLY,
                ))
                return connection
            return real_connect(*arguments, **keywords)

        try:
            with self.configured(), mock.patch(
                "django_images.services.sqlite_snapshot.sqlite3.connect",
                side_effect=swap_and_open_unrelated_matching_fd,
            ), self.assertRaisesRegex(
                SQLiteSnapshotError,
                "^sqlite_source_identity_changed$",
            ):
                snapshot_sqlite(
                    self.source_path,
                    run,
                    os.getuid(),
                    os.getgid(),
                )
        finally:
            for descriptor in unrelated_descriptors:
                os.close(descriptor)

        self.assertFalse(os.path.exists(os.path.join(
            run.path,
            "production.db.before-migration",
        )))

    def test_missing_snapshot_intent_identity_cannot_adopt_valid_temp(self):
        run = self.create_run()
        source = sqlite3.connect(self.source_path)
        source.execute("CREATE TABLE records (value TEXT)")
        source.commit()
        temp_path = os.path.join(
            run.path,
            ".production.db.before-migration.tmp",
        )
        destination = sqlite3.connect(temp_path)
        source.backup(destination)
        destination.close()
        source.close()
        os.chmod(temp_path, 0o600)
        state_path = os.path.join(run.path, "migration-state.json")
        with open(state_path, "r", encoding="utf-8") as state_file:
            state = json.load(state_file)
        state["phase"] = "snapshot_intent"
        with open(state_path, "w", encoding="utf-8") as state_file:
            json.dump(state, state_file)

        with self.configured(), self.assertRaisesRegex(
            SQLiteSnapshotError,
            "^sqlite_snapshot_state_invalid$",
        ):
            snapshot_sqlite(
                self.source_path,
                run,
                os.getuid(),
                os.getgid(),
            )
        self.assertFalse(os.path.exists(os.path.join(
            run.path,
            "production.db.before-migration",
        )))

    def test_empty_snapshot_intent_cannot_adopt_valid_final(self):
        sqlite3.connect(self.source_path).close()
        run = resolve_or_create_run(
            scan_run_inventory(self.backup_root),
            {
                "present": True,
                "database_identity": None,
                "media_root_identity": None,
            },
            False,
            "source-commit",
            os.getuid(),
            os.getgid(),
        )
        state_path = os.path.join(run.path, "migration-state.json")
        with open(state_path, "r", encoding="utf-8") as state_file:
            state = json.load(state_file)
        state["phase"] = "snapshot_intent"
        state["intent"] = {}
        with open(state_path, "w", encoding="utf-8") as state_file:
            json.dump(state, state_file)
        final_path = os.path.join(
            run.path,
            "production.db.before-migration",
        )
        source = sqlite3.connect(self.source_path)
        destination = sqlite3.connect(final_path)
        source.backup(destination)
        destination.close()
        source.close()
        os.chmod(final_path, 0o600)

        with self.configured(), self.assertRaisesRegex(
            SQLiteSnapshotError,
            "^sqlite_snapshot_state_invalid$",
        ):
            snapshot_sqlite(
                self.source_path,
                run,
                os.getuid(),
                os.getgid(),
            )
