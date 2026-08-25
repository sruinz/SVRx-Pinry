from contextlib import ExitStack
import hashlib
import os
import sqlite3
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
        self.backup_root = os.path.join(self.data_root, "legacy-backup")
        os.mkdir(self.backup_root, 0o700)
        self.source_path = os.path.join(self.data_root, "production.db")
        self.database_settings = {
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": self.source_path,
            },
        }

    def create_run(self):
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
        transition_state(run, "initialized", "snapshot_intent")
        return run

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

    def test_valid_crash_temp_is_promoted_once_without_new_backup(self):
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

        with self.configured(), mock.patch(
            "django_images.services.sqlite_snapshot._copy_database",
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

        copy_database.assert_not_called()
        self.assertEqual(first, second)
        self.assertFalse(os.path.exists(temp_path))
        self.assertTrue(os.path.isfile(os.path.join(
            run.path,
            "production.db.before-migration",
        )))

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
