from contextlib import ExitStack
import fcntl
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time

from django.core.files.storage import DefaultStorage, FileSystemStorage
from django.test import SimpleTestCase
import mock

from django_images import file_ops
from django_images.services import startup_lock, startup_preflight


MIB = 1024 * 1024
ASSET_UUID = "12345678-1234-5678-1234-567812345678"
ASSET_UUID_HEX = "12345678123456781234567812345678"
VALID_IMAGE_SIZES = {
    "thumbnail": {"size": [240, 0]},
    "standard": {"size": [600, 0]},
    "square": {"crop": True, "size": [125, 125]},
}


class _DiskGraph(object):
    def __init__(self, *nodes):
        self.nodes = {node: object() for node in nodes}


class LegacyEvidenceTests(SimpleTestCase):
    def setUp(self):
        self.temporary_root = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_root.cleanup)
        self.root_path = Path(os.path.realpath(self.temporary_root.name))
        self.media_root = self.root_path / "media"
        self.media_root.mkdir()
        self.database_path = self.root_path / "production.db"

    def _create_database(
        self, image_paths=(), thumbnail_paths=(), applied=()
    ):
        connection = sqlite3.connect(str(self.database_path))
        try:
            connection.executescript("""
                CREATE TABLE django_images_image (
                    id INTEGER PRIMARY KEY,
                    image VARCHAR(255) NOT NULL
                );
                CREATE TABLE django_images_thumbnail (
                    id INTEGER PRIMARY KEY,
                    image VARCHAR(255) NOT NULL,
                    original_id INTEGER,
                    size VARCHAR(100)
                );
                CREATE TABLE django_migrations (
                    id INTEGER PRIMARY KEY,
                    app VARCHAR(255) NOT NULL,
                    name VARCHAR(255) NOT NULL,
                    applied DATETIME NOT NULL
                );
            """)
            connection.executemany(
                "INSERT INTO django_images_image (image) VALUES (?)",
                [(path,) for path in image_paths],
            )
            connection.executemany(
                "INSERT INTO django_images_thumbnail (image) VALUES (?)",
                [(path,) for path in thumbnail_paths],
            )
            connection.executemany(
                """
                INSERT INTO django_migrations (app, name, applied)
                VALUES (?, ?, '2026-08-25 00:00:00')
                """,
                list(applied),
            )
            connection.commit()
        finally:
            connection.close()

    def _add_asset_metadata_and_derivatives(
        self,
        original_filename,
        extension=".png",
        database_asset_uuid=ASSET_UUID_HEX,
    ):
        connection = sqlite3.connect(str(self.database_path))
        try:
            connection.execute(
                "ALTER TABLE django_images_image "
                "ADD COLUMN asset_uuid CHAR(32)"
            )
            connection.execute(
                "ALTER TABLE django_images_image "
                "ADD COLUMN original_filename VARCHAR(255)"
            )
            connection.execute(
                "UPDATE django_images_image "
                "SET asset_uuid = ?, original_filename = ? WHERE id = 1",
                (database_asset_uuid, original_filename),
            )
            connection.executemany(
                "INSERT INTO django_images_thumbnail "
                "(image, original_id, size) VALUES (?, 1, ?)",
                [
                    (
                        "derivatives/{}/{}{}".format(
                            ASSET_UUID, size, extension
                        ),
                        size,
                    )
                    for size in ("thumbnail", "standard", "square")
                ],
            )
            connection.commit()
        finally:
            connection.close()

    def _add_old_schema_derivatives(self, extension=".png"):
        connection = sqlite3.connect(str(self.database_path))
        try:
            connection.executemany(
                "INSERT INTO django_images_thumbnail "
                "(image, original_id, size) VALUES (?, 1, ?)",
                [
                    (
                        "derivatives/{}/{}{}".format(
                            ASSET_UUID, size, extension
                        ),
                        size,
                    )
                    for size in ("thumbnail", "standard", "square")
                ],
            )
            connection.commit()
        finally:
            connection.close()

    def _add_reference_tables(
        self,
        pin_image_ids=(),
        media_asset_image_ids=(),
    ):
        connection = sqlite3.connect(str(self.database_path))
        try:
            connection.executescript("""
                CREATE TABLE core_pin (
                    id INTEGER PRIMARY KEY,
                    image_id INTEGER NOT NULL
                );
                CREATE TABLE core_mediaasset (
                    id INTEGER PRIMARY KEY,
                    image_id INTEGER NOT NULL
                );
            """)
            connection.executemany(
                "INSERT INTO core_pin (image_id) VALUES (?)",
                [(image_id,) for image_id in pin_image_ids],
            )
            connection.executemany(
                "INSERT INTO core_mediaasset (image_id) VALUES (?)",
                [(image_id,) for image_id in media_asset_image_ids],
            )
            connection.commit()
        finally:
            connection.close()

    def _write_media(self, relative_path, content):
        path = self.media_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def _inspect(self, graph):
        return startup_preflight.inspect_legacy_evidence(
            str(self.database_path),
            graph,
            str(self.media_root),
        )

    def _cleanup(self, evidence):
        backup_verifier = mock.Mock()
        return startup_preflight.remove_confirmed_missing_unreferenced_images(
            str(self.database_path),
            str(self.media_root),
            evidence.missing_unreferenced_images,
            evidence.database_identity,
            evidence.media_root_identity,
            backup_verifier,
        )

    def _copy_database(self, destination):
        source = sqlite3.connect(str(self.database_path))
        copied = sqlite3.connect(str(destination))
        try:
            source.backup(copied)
        finally:
            copied.close()
            source.close()

    def test_missing_database_never_connects_or_marks_schema_pending(self):
        graph = _DiskGraph(("django_images", "0001_initial"))

        with mock.patch.object(
            startup_preflight.sqlite3, "connect"
        ) as connect:
            evidence = self._inspect(graph)

        connect.assert_not_called()
        self.assertFalse(evidence.database_exists)
        self.assertEqual(evidence.database_bytes, 0)
        self.assertFalse(evidence.pending_schema)
        self.assertEqual(evidence.pending_migrations, ())
        self.assertFalse(evidence.has_legacy_evidence)
        self.assertFalse(evidence.has_media_rows)
        self.assertIsNone(evidence.database_identity)
        media_stat = os.stat(str(self.media_root))
        self.assertEqual(evidence.media_root_identity, {
            "device": media_stat.st_dev,
            "inode": media_stat.st_ino,
        })

    def test_fresh_runtime_directories_are_not_legacy_evidence(self):
        for name in (
            "originals",
            "derivatives",
            ".staging",
            ".pinry-locks",
        ):
            (self.media_root / name).mkdir()

        evidence = self._inspect(_DiskGraph())

        self.assertFalse(evidence.database_exists)
        self.assertFalse(evidence.has_pinry_direct_md5_directory)
        self.assertFalse(evidence.has_legacy_evidence)

    def test_pre_schema_md5_paths_and_pending_disk_node_are_detected(self):
        original = (
            "image/original/by-md5/a/b/"
            "ab0123456789abcdef0123456789abcd/photo.jpg"
        )
        thumbnail = (
            "image/thumbnail/by-md5/c/d/"
            "cd0123456789abcdef0123456789abcd/thumbnail.jpg"
        )
        self._create_database(
            image_paths=(original, original),
            thumbnail_paths=(thumbnail,),
            applied=(("django_images", "0001_initial"),),
        )
        self._write_media(original, b"original")
        self._write_media(thumbnail, b"thumb")
        graph = _DiskGraph(
            ("django_images", "0001_initial"),
            ("django_images", "0002_next"),
        )

        evidence = self._inspect(graph)

        self.assertTrue(evidence.database_exists)
        self.assertTrue(evidence.has_md5_paths)
        self.assertFalse(evidence.has_fixed_slot_paths)
        self.assertFalse(evidence.has_named_canonical_paths)
        self.assertEqual(
            evidence.pending_migrations,
            (("django_images", "0002_next"),),
        )
        self.assertTrue(evidence.pending_schema)
        self.assertEqual(evidence.distinct_legacy_bytes, 13)
        self.assertTrue(evidence.has_legacy_evidence)

    def test_pinry_direct_md5_paths_are_detected_as_legacy_evidence(self):
        original = (
            "a/b/ab0123456789abcdef0123456789abcd/photo.jpg"
        )
        thumbnail = (
            "c/d/cd0123456789abcdef0123456789abcd/thumbnail.jpg"
        )
        self._create_database(
            image_paths=(original,),
            thumbnail_paths=(thumbnail,),
        )
        self._write_media(original, b"original")
        self._write_media(thumbnail, b"thumb")

        evidence = self._inspect(_DiskGraph())

        self.assertTrue(evidence.has_md5_paths)
        self.assertTrue(evidence.has_legacy_evidence)
        self.assertFalse(evidence.has_media_image_directory)
        self.assertEqual(evidence.distinct_legacy_bytes, 13)

    def test_orphan_pinry_direct_root_is_independent_legacy_evidence(self):
        direct_root = self.media_root / "a"
        direct_root.mkdir()

        evidence = self._inspect(_DiskGraph())

        self.assertFalse(evidence.database_exists)
        self.assertTrue(evidence.has_pinry_direct_md5_directory)
        self.assertTrue(evidence.has_legacy_evidence)

    def test_pinry_direct_root_symlink_is_rejected_during_root_inspection(self):
        outside = self.root_path / "outside"
        outside.mkdir()
        os.symlink(str(outside), str(self.media_root / "a"))

        with self.assertRaises(
            startup_preflight.StartupPreflightError
        ) as caught:
            self._inspect(_DiskGraph())

        self.assertEqual(caught.exception.code, "legacy_media_root_invalid")

    def test_malformed_pinry_direct_row_is_rejected(self):
        self._create_database(image_paths=(
            "a/c/ab0123456789abcdef0123456789abcd/photo.jpg",
        ))

        with self.assertRaises(
            startup_preflight.StartupPreflightError
        ) as caught:
            self._inspect(_DiskGraph())

        self.assertEqual(caught.exception.code, "legacy_media_rows_invalid")

    def test_missing_pinry_direct_file_is_rejected(self):
        self._create_database(image_paths=(
            "a/b/ab0123456789abcdef0123456789abcd/photo.jpg",
        ))

        with self.assertRaises(
            startup_preflight.StartupPreflightError
        ) as caught:
            self._inspect(_DiskGraph())

        self.assertEqual(caught.exception.code, "legacy_media_files_invalid")

    def test_unknown_media_row_layouts_are_rejected(self):
        for row_kind in ("image", "thumbnail"):
            with self.subTest(row_kind=row_kind):
                if self.database_path.exists():
                    self.database_path.unlink()
                kwargs = {
                    "image_paths": ("uploads/photo.jpg",),
                    "thumbnail_paths": (),
                }
                if row_kind == "thumbnail":
                    kwargs = {
                        "image_paths": (),
                        "thumbnail_paths": ("uploads/thumb.jpg",),
                    }
                self._create_database(**kwargs)

                with self.assertRaises(
                    startup_preflight.StartupPreflightError
                ) as caught:
                    self._inspect(_DiskGraph())

                self.assertEqual(
                    caught.exception.code,
                    "legacy_media_rows_invalid",
                )

    def test_existing_canonical_image_is_explicit_media_row_evidence(self):
        self._create_database(
            image_paths=(
                "originals/{}/photo.jpg".format(ASSET_UUID),
            ),
        )

        evidence = self._inspect(_DiskGraph())

        self.assertTrue(evidence.has_media_rows)
        self.assertFalse(evidence.has_legacy_evidence)

    def test_existing_media_asset_is_explicit_media_row_evidence(self):
        self._create_database()
        connection = sqlite3.connect(str(self.database_path))
        try:
            connection.execute(
                "CREATE TABLE core_mediaasset (id INTEGER PRIMARY KEY)"
            )
            connection.execute("INSERT INTO core_mediaasset (id) VALUES (1)")
            connection.commit()
        finally:
            connection.close()

        evidence = self._inspect(_DiskGraph())

        self.assertTrue(evidence.has_media_rows)
        self.assertFalse(evidence.has_legacy_evidence)

    def test_original_leaf_uses_asset_metadata_to_distinguish_generation(self):
        cases = (
            (
                "named-original-leaf",
                "original.png",
                False,
                0,
            ),
            (
                "true-fixed-slot",
                "holiday.png",
                True,
                len(b"image-bytes"),
            ),
        )
        relative_path = "originals/{}/original.png".format(ASSET_UUID)
        for name, original_filename, fixed_slot, legacy_bytes in cases:
            with self.subTest(name=name):
                if self.database_path.exists():
                    self.database_path.unlink()
                self._create_database(image_paths=(relative_path,))
                self._add_asset_metadata_and_derivatives(original_filename)
                self._write_media(relative_path, b"image-bytes")

                evidence = self._inspect(_DiskGraph())

                self.assertFalse(evidence.has_md5_paths)
                self.assertEqual(evidence.has_fixed_slot_paths, fixed_slot)
                self.assertTrue(evidence.has_named_canonical_paths)
                self.assertEqual(evidence.distinct_legacy_bytes, legacy_bytes)

    def test_asset_metadata_uuid_accepts_raw_forms_and_rejects_corruption(self):
        cases = (
            ("hyphenated", ASSET_UUID, True),
            ("malformed", "not-a-uuid", False),
            ("non-string", sqlite3.Binary(b"not-a-text-uuid"), False),
        )
        relative_path = "originals/{}/original.png".format(ASSET_UUID)
        for name, database_asset_uuid, valid in cases:
            with self.subTest(name=name):
                if self.database_path.exists():
                    self.database_path.unlink()
                self._create_database(image_paths=(relative_path,))
                self._add_asset_metadata_and_derivatives(
                    "original.png",
                    database_asset_uuid=database_asset_uuid,
                )
                self._write_media(relative_path, b"image-bytes")

                if valid:
                    evidence = self._inspect(_DiskGraph())
                    self.assertFalse(evidence.has_fixed_slot_paths)
                    self.assertTrue(evidence.has_named_canonical_paths)
                else:
                    with self.assertRaises(
                        startup_preflight.StartupPreflightError
                    ) as caught:
                        self._inspect(_DiskGraph())
                    self.assertEqual(
                        caught.exception.code,
                        "legacy_media_rows_invalid",
                    )

    def test_named_canonical_fallback_accepts_empty_source_filename(self):
        relative_path = (
            "originals/{}/image-123456781234.png".format(ASSET_UUID)
        )
        self._create_database(image_paths=(relative_path,))
        self._add_asset_metadata_and_derivatives("")

        evidence = self._inspect(_DiskGraph())

        self.assertFalse(evidence.has_fixed_slot_paths)
        self.assertTrue(evidence.has_named_canonical_paths)

    def test_django_normalized_named_path_is_legacy_evidence(self):
        relative_path = (
            "originals/{}/"
            "스크린샷_2026-08-20_21.23.05.png".format(ASSET_UUID)
        )
        self._create_database(image_paths=(relative_path,))
        self._add_asset_metadata_and_derivatives(
            "스크린샷 2026-08-20 21.23.05.png",
        )
        self._write_media(relative_path, b"django-normalized")

        evidence = self._inspect(_DiskGraph())

        self.assertTrue(evidence.has_fixed_slot_paths)
        self.assertTrue(evidence.has_named_canonical_paths)
        self.assertTrue(evidence.has_legacy_evidence)
        self.assertEqual(
            evidence.distinct_legacy_bytes,
            len(b"django-normalized"),
        )

    def test_missing_unreferenced_image_is_recorded_for_safe_cleanup(self):
        relative_path = (
            "originals/{}/"
            "스크린샷_2026-08-20_21.23.05.png".format(ASSET_UUID)
        )
        self._create_database(image_paths=(relative_path,))
        self._add_asset_metadata_and_derivatives(
            "스크린샷 2026-08-20 21.23.05.png",
        )
        self._add_reference_tables()

        evidence = self._inspect(_DiskGraph())

        self.assertTrue(evidence.has_media_rows)
        self.assertTrue(evidence.has_legacy_evidence)
        self.assertFalse(evidence.has_fixed_slot_paths)
        self.assertFalse(evidence.has_named_canonical_paths)
        self.assertEqual(evidence.distinct_legacy_bytes, 0)
        self.assertEqual(len(evidence.missing_unreferenced_images), 1)
        candidate = evidence.missing_unreferenced_images[0]
        self.assertEqual(candidate.image_id, 1)
        self.assertEqual(candidate.image_path, relative_path)
        self.assertEqual(
            candidate.thumbnail_rows,
            tuple(
                (
                    index,
                    "derivatives/{}/{}.png".format(ASSET_UUID, size),
                )
                for index, size in enumerate(
                    ("thumbnail", "standard", "square"),
                    1,
                )
            ),
        )

    def test_missing_image_referenced_by_pin_is_not_cleanup_candidate(self):
        relative_path = (
            "originals/{}/"
            "스크린샷_2026-08-20_21.23.05.png".format(ASSET_UUID)
        )
        self._create_database(image_paths=(relative_path,))
        self._add_asset_metadata_and_derivatives(
            "스크린샷 2026-08-20 21.23.05.png",
        )
        self._add_reference_tables(pin_image_ids=(1,))

        with self.assertRaises(
            startup_preflight.StartupPreflightError
        ) as caught:
            self._inspect(_DiskGraph())

        self.assertEqual(caught.exception.code, "legacy_media_files_invalid")

    def test_missing_image_referenced_by_media_asset_is_not_cleanup_candidate(self):
        relative_path = (
            "originals/{}/"
            "스크린샷_2026-08-20_21.23.05.png".format(ASSET_UUID)
        )
        self._create_database(image_paths=(relative_path,))
        self._add_asset_metadata_and_derivatives(
            "스크린샷 2026-08-20 21.23.05.png",
        )
        self._add_reference_tables(media_asset_image_ids=(1,))

        with self.assertRaises(
            startup_preflight.StartupPreflightError
        ) as caught:
            self._inspect(_DiskGraph())

        self.assertEqual(caught.exception.code, "legacy_media_files_invalid")

    def test_partially_missing_unreferenced_image_is_not_cleanup_candidate(self):
        relative_path = (
            "originals/{}/"
            "스크린샷_2026-08-20_21.23.05.png".format(ASSET_UUID)
        )
        self._create_database(image_paths=(relative_path,))
        self._add_asset_metadata_and_derivatives(
            "스크린샷 2026-08-20 21.23.05.png",
        )
        self._add_reference_tables()
        self._write_media(relative_path, b"still-present")

        evidence = self._inspect(_DiskGraph())

        self.assertEqual(evidence.missing_unreferenced_images, ())
        self.assertTrue(evidence.has_fixed_slot_paths)
        self.assertEqual(
            evidence.distinct_legacy_bytes,
            len(b"still-present"),
        )

    def test_confirmed_missing_unreferenced_image_rows_are_removed(self):
        relative_path = (
            "originals/{}/"
            "스크린샷_2026-08-20_21.23.05.png".format(ASSET_UUID)
        )
        self._create_database(image_paths=(relative_path,))
        self._add_asset_metadata_and_derivatives(
            "스크린샷 2026-08-20 21.23.05.png",
        )
        self._add_reference_tables()
        evidence = self._inspect(_DiskGraph())

        removed = self._cleanup(evidence)

        self.assertEqual(removed, 1)
        connection = sqlite3.connect(str(self.database_path))
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM django_images_image"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM django_images_thumbnail"
                ).fetchone()[0],
                0,
            )
        finally:
            connection.close()

    def test_cleanup_repairs_existing_lock_mode_before_acquiring_lock(self):
        relative_path = (
            "originals/{}/"
            "스크린샷_2026-08-20_21.23.05.png".format(ASSET_UUID)
        )
        self._create_database(image_paths=(relative_path,))
        self._add_asset_metadata_and_derivatives(
            "스크린샷 2026-08-20 21.23.05.png",
        )
        self._add_reference_tables()
        evidence = self._inspect(_DiskGraph())
        lock_directory = self.media_root / ".pinry-locks"
        lock_directory.mkdir(mode=0o700)
        lifecycle_lock = lock_directory / "media-lifecycle.lock"
        lifecycle_lock.write_bytes(b"")
        lifecycle_lock.chmod(0o700)

        removed = self._cleanup(evidence)

        self.assertEqual(removed, 1)
        self.assertEqual(
            stat.S_IMODE(lifecycle_lock.stat().st_mode),
            0o600,
        )

    def test_cleanup_requires_expected_database_and_media_identities(self):
        relative_path = (
            "originals/{}/"
            "스크린샷_2026-08-20_21.23.05.png".format(ASSET_UUID)
        )
        self._create_database(image_paths=(relative_path,))
        self._add_asset_metadata_and_derivatives(
            "스크린샷 2026-08-20 21.23.05.png",
        )
        self._add_reference_tables()
        evidence = self._inspect(_DiskGraph())

        cases = (
            (None, evidence.media_root_identity, "legacy_database_invalid"),
            (evidence.database_identity, None, "legacy_media_root_invalid"),
        )
        for database_identity, media_identity, expected_code in cases:
            with self.subTest(expected_code=expected_code), self.assertRaises(
                startup_preflight.StartupPreflightError
            ) as caught:
                startup_preflight.remove_confirmed_missing_unreferenced_images(
                    str(self.database_path),
                    str(self.media_root),
                    evidence.missing_unreferenced_images,
                    database_identity,
                    media_identity,
                    mock.Mock(),
                )
            self.assertEqual(caught.exception.code, expected_code)

        connection = sqlite3.connect(str(self.database_path))
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM django_images_image"
                ).fetchone()[0],
                1,
            )
        finally:
            connection.close()

    def test_cleanup_requires_live_backup_verifier(self):
        relative_path = (
            "originals/{}/"
            "스크린샷_2026-08-20_21.23.05.png".format(ASSET_UUID)
        )
        self._create_database(image_paths=(relative_path,))
        self._add_asset_metadata_and_derivatives(
            "스크린샷 2026-08-20 21.23.05.png",
        )
        self._add_reference_tables()
        evidence = self._inspect(_DiskGraph())

        with self.assertRaisesRegex(
            startup_preflight.StartupPreflightError,
            "^legacy_database_invalid$",
        ):
            startup_preflight.remove_confirmed_missing_unreferenced_images(
                str(self.database_path),
                str(self.media_root),
                evidence.missing_unreferenced_images,
                evidence.database_identity,
                evidence.media_root_identity,
                None,
            )

        connection = sqlite3.connect(str(self.database_path))
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM django_images_image"
                ).fetchone()[0],
                1,
            )
        finally:
            connection.close()

    def test_cleanup_rolls_back_when_backup_changes_before_commit(self):
        relative_path = (
            "originals/{}/"
            "스크린샷_2026-08-20_21.23.05.png".format(ASSET_UUID)
        )
        self._create_database(image_paths=(relative_path,))
        self._add_asset_metadata_and_derivatives(
            "스크린샷 2026-08-20 21.23.05.png",
        )
        self._add_reference_tables()
        evidence = self._inspect(_DiskGraph())
        backup_verifier = mock.Mock()
        backup_verifier.verify_current.side_effect = (
            None,
            None,
            startup_preflight.StartupPreflightError(
                "legacy_database_invalid"
            ),
        )

        with self.assertRaisesRegex(
            startup_preflight.StartupPreflightError,
            "^legacy_database_invalid$",
        ):
            startup_preflight.remove_confirmed_missing_unreferenced_images(
                str(self.database_path),
                str(self.media_root),
                evidence.missing_unreferenced_images,
                evidence.database_identity,
                evidence.media_root_identity,
                backup_verifier,
            )

        self.assertGreaterEqual(backup_verifier.verify_current.call_count, 3)
        connection = sqlite3.connect(str(self.database_path))
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM django_images_image"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM django_images_thumbnail"
                ).fetchone()[0],
                3,
            )
        finally:
            connection.close()

    def test_cleanup_rejects_canonical_target_created_after_inspection(self):
        relative_path = (
            "originals/{}/"
            "스크린샷_2026-08-20_21.23.05.png".format(ASSET_UUID)
        )
        canonical_path = (
            "originals/{}/"
            "스크린샷 2026-08-20 21.23.05.jpg".format(ASSET_UUID)
        )
        self._create_database(image_paths=(relative_path,))
        self._add_asset_metadata_and_derivatives(
            "스크린샷 2026-08-20 21.23.05.png",
        )
        self._add_reference_tables()
        evidence = self._inspect(_DiskGraph())
        self._write_media(canonical_path, b"migration-copy")

        with self.assertRaises(
            startup_preflight.StartupPreflightError
        ) as caught:
            self._cleanup(evidence)

        self.assertEqual(
            caught.exception.code,
            "legacy_media_files_invalid",
        )
        connection = sqlite3.connect(str(self.database_path))
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM django_images_image"
                ).fetchone()[0],
                1,
            )
        finally:
            connection.close()

    def test_cleanup_rejects_unregistered_derivative_target(self):
        relative_path = (
            "originals/{}/"
            "스크린샷_2026-08-20_21.23.05.png".format(ASSET_UUID)
        )
        self._create_database(image_paths=(relative_path,))
        self._add_asset_metadata_and_derivatives(
            "스크린샷 2026-08-20 21.23.05.png",
        )
        self._add_reference_tables()
        evidence = self._inspect(_DiskGraph())
        self._write_media(
            "derivatives/{}/standard.webp".format(ASSET_UUID),
            b"migration-copy",
        )

        with self.assertRaises(
            startup_preflight.StartupPreflightError
        ) as caught:
            self._cleanup(evidence)

        self.assertEqual(
            caught.exception.code,
            "legacy_media_files_invalid",
        )

    def test_cleanup_rejects_reference_created_after_inspection(self):
        relative_path = (
            "originals/{}/"
            "스크린샷_2026-08-20_21.23.05.png".format(ASSET_UUID)
        )
        self._create_database(image_paths=(relative_path,))
        self._add_asset_metadata_and_derivatives(
            "스크린샷 2026-08-20 21.23.05.png",
        )
        self._add_reference_tables()
        evidence = self._inspect(_DiskGraph())
        connection = sqlite3.connect(str(self.database_path))
        try:
            connection.execute(
                "INSERT INTO core_pin (image_id) VALUES (1)"
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(
            startup_preflight.StartupPreflightError
        ) as caught:
            self._cleanup(evidence)

        self.assertEqual(
            caught.exception.code,
            "legacy_media_rows_invalid",
        )
        connection = sqlite3.connect(str(self.database_path))
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM django_images_image"
                ).fetchone()[0],
                1,
            )
        finally:
            connection.close()

    def test_cleanup_rejects_unknown_image_foreign_key(self):
        relative_path = (
            "originals/{}/"
            "스크린샷_2026-08-20_21.23.05.png".format(ASSET_UUID)
        )
        self._create_database(image_paths=(relative_path,))
        self._add_asset_metadata_and_derivatives(
            "스크린샷 2026-08-20 21.23.05.png",
        )
        self._add_reference_tables()
        evidence = self._inspect(_DiskGraph())
        connection = sqlite3.connect(str(self.database_path))
        try:
            connection.execute("""
                CREATE TABLE unexpected_image_reference (
                    id INTEGER PRIMARY KEY,
                    image_id INTEGER REFERENCES django_images_image(id)
                )
            """)
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(
            startup_preflight.StartupPreflightError
        ) as caught:
            self._cleanup(evidence)

        self.assertEqual(
            caught.exception.code,
            "legacy_media_rows_invalid",
        )
        connection = sqlite3.connect(str(self.database_path))
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM django_images_image"
                ).fetchone()[0],
                1,
            )
        finally:
            connection.close()

    def test_cleanup_rejects_cascading_thumbnail_foreign_key(self):
        relative_path = (
            "originals/{}/"
            "스크린샷_2026-08-20_21.23.05.png".format(ASSET_UUID)
        )
        self._create_database(image_paths=(relative_path,))
        self._add_asset_metadata_and_derivatives(
            "스크린샷 2026-08-20 21.23.05.png",
        )
        self._add_reference_tables()
        evidence = self._inspect(_DiskGraph())
        connection = sqlite3.connect(str(self.database_path))
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript("""
                CREATE TABLE unexpected_thumbnail_reference (
                    id INTEGER PRIMARY KEY,
                    thumbnail_id INTEGER REFERENCES
                        django_images_thumbnail(id) ON DELETE CASCADE
                );
                INSERT INTO unexpected_thumbnail_reference
                    (thumbnail_id) VALUES (1);
            """)
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(
            startup_preflight.StartupPreflightError
        ) as caught:
            self._cleanup(evidence)

        self.assertEqual(caught.exception.code, "legacy_media_rows_invalid")
        connection = sqlite3.connect(str(self.database_path))
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM django_images_thumbnail"
                ).fetchone()[0],
                3,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM unexpected_thumbnail_reference"
                ).fetchone()[0],
                1,
            )
        finally:
            connection.close()

    def test_cleanup_rejects_trigger_on_deleted_media_table(self):
        relative_path = (
            "originals/{}/"
            "스크린샷_2026-08-20_21.23.05.png".format(ASSET_UUID)
        )
        self._create_database(image_paths=(relative_path,))
        self._add_asset_metadata_and_derivatives(
            "스크린샷 2026-08-20 21.23.05.png",
        )
        self._add_reference_tables()
        evidence = self._inspect(_DiskGraph())
        connection = sqlite3.connect(str(self.database_path))
        try:
            connection.executescript("""
                CREATE TABLE cleanup_audit (value INTEGER NOT NULL);
                INSERT INTO cleanup_audit (value) VALUES (1);
                CREATE TRIGGER unexpected_thumbnail_delete
                AFTER DELETE ON django_images_thumbnail
                BEGIN
                    DELETE FROM cleanup_audit;
                END;
            """)
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(
            startup_preflight.StartupPreflightError
        ) as caught:
            self._cleanup(evidence)

        self.assertEqual(caught.exception.code, "legacy_media_rows_invalid")
        connection = sqlite3.connect(str(self.database_path))
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM django_images_thumbnail"
                ).fetchone()[0],
                3,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM cleanup_audit"
                ).fetchone()[0],
                1,
            )
        finally:
            connection.close()

    def test_cleanup_rejects_database_replaced_after_evidence(self):
        relative_path = (
            "originals/{}/"
            "스크린샷_2026-08-20_21.23.05.png".format(ASSET_UUID)
        )
        self._create_database(image_paths=(relative_path,))
        self._add_asset_metadata_and_derivatives(
            "스크린샷 2026-08-20 21.23.05.png",
        )
        self._add_reference_tables()
        evidence = self._inspect(_DiskGraph())
        replacement_path = self.root_path / "replacement.db"
        original_path = self.root_path / "original.db"
        self._copy_database(replacement_path)
        self.database_path.rename(original_path)
        replacement_path.rename(self.database_path)

        with self.assertRaises(
            startup_preflight.StartupPreflightError
        ) as caught:
            self._cleanup(evidence)

        self.assertEqual(caught.exception.code, "legacy_database_invalid")
        for path in (self.database_path, original_path):
            connection = sqlite3.connect(str(path))
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM django_images_image"
                    ).fetchone()[0],
                    1,
                )
            finally:
                connection.close()

    def test_cleanup_rejects_media_root_replaced_after_evidence(self):
        relative_path = (
            "originals/{}/"
            "스크린샷_2026-08-20_21.23.05.png".format(ASSET_UUID)
        )
        self._create_database(image_paths=(relative_path,))
        self._add_asset_metadata_and_derivatives(
            "스크린샷 2026-08-20 21.23.05.png",
        )
        self._add_reference_tables()
        evidence = self._inspect(_DiskGraph())
        original_root = self.root_path / "original-media"
        self.media_root.rename(original_root)
        self.media_root.mkdir()

        with self.assertRaises(
            startup_preflight.StartupPreflightError
        ) as caught:
            self._cleanup(evidence)

        self.assertEqual(caught.exception.code, "legacy_media_root_invalid")
        connection = sqlite3.connect(str(self.database_path))
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM django_images_image"
                ).fetchone()[0],
                1,
            )
        finally:
            connection.close()

    def test_cleanup_rolls_back_if_media_root_is_replaced_during_delete(self):
        relative_path = (
            "originals/{}/"
            "스크린샷_2026-08-20_21.23.05.png".format(ASSET_UUID)
        )
        self._create_database(image_paths=(relative_path,))
        self._add_asset_metadata_and_derivatives(
            "스크린샷 2026-08-20 21.23.05.png",
        )
        self._add_reference_tables()
        evidence = self._inspect(_DiskGraph())
        original_root = self.root_path / "original-media"
        real_delete = startup_preflight._delete_cleanup_candidate

        def replace_media_root(connection, candidate):
            result = real_delete(connection, candidate)
            self.media_root.rename(original_root)
            self.media_root.mkdir()
            return result

        with mock.patch.object(
            startup_preflight,
            "_delete_cleanup_candidate",
            side_effect=replace_media_root,
        ), self.assertRaises(
            startup_preflight.StartupPreflightError
        ) as caught:
            self._cleanup(evidence)

        self.assertEqual(caught.exception.code, "legacy_media_root_invalid")
        connection = sqlite3.connect(str(self.database_path))
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM django_images_image"
                ).fetchone()[0],
                1,
            )
        finally:
            connection.close()

    def test_cleanup_rejects_database_aba_during_connection_open(self):
        relative_path = (
            "originals/{}/"
            "스크린샷_2026-08-20_21.23.05.png".format(ASSET_UUID)
        )
        self._create_database(image_paths=(relative_path,))
        self._add_asset_metadata_and_derivatives(
            "스크린샷 2026-08-20 21.23.05.png",
        )
        self._add_reference_tables()
        evidence = self._inspect(_DiskGraph())
        replacement_path = self.root_path / "replacement.db"
        held_path = self.root_path / "held.db"
        self._copy_database(replacement_path)
        real_connect = sqlite3.connect
        swapped = []

        class RestoreAfterBegin(object):
            def __init__(self, wrapped):
                self.wrapped = wrapped

            def restore(self):
                if self.held_path.exists():
                    self.database_path.rename(self.replacement_path)
                    self.held_path.rename(self.database_path)

            def execute(self, statement, *arguments):
                result = self.wrapped.execute(statement, *arguments)
                if statement == "BEGIN IMMEDIATE":
                    self.restore()
                return result

            def close(self):
                try:
                    return self.wrapped.close()
                finally:
                    self.restore()

            def __getattr__(self, name):
                return getattr(self.wrapped, name)

        def swap_around_write_connect(*arguments, **keywords):
            uri = arguments[0] if arguments else keywords.get("database", "")
            if not swapped and "mode=rw" in uri:
                swapped.append(True)
                self.database_path.rename(held_path)
                replacement_path.rename(self.database_path)
                replacement_connection = real_connect(
                    *arguments,
                    **keywords
                )
                replacement_connection.execute(
                    "PRAGMA journal_mode = OFF"
                )
                wrapped = RestoreAfterBegin(
                    replacement_connection
                )
                wrapped.database_path = self.database_path
                wrapped.replacement_path = replacement_path
                wrapped.held_path = held_path
                return wrapped
            return real_connect(*arguments, **keywords)

        with mock.patch.object(
            startup_preflight.sqlite3,
            "connect",
            side_effect=swap_around_write_connect,
        ), self.assertRaises(
            startup_preflight.StartupPreflightError
        ) as caught:
            self._cleanup(evidence)

        self.assertEqual(swapped, [True])
        self.assertEqual(caught.exception.code, "legacy_database_invalid")
        for path in (self.database_path, replacement_path):
            connection = sqlite3.connect(str(path))
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM django_images_image"
                    ).fetchone()[0],
                    1,
                )
            finally:
                connection.close()

    def test_cleanup_holds_exclusive_lifecycle_lock_through_delete(self):
        relative_path = (
            "originals/{}/"
            "스크린샷_2026-08-20_21.23.05.png".format(ASSET_UUID)
        )
        canonical_path = (
            "originals/{}/"
            "스크린샷 2026-08-20 21.23.05.jpg".format(ASSET_UUID)
        )
        self._create_database(image_paths=(relative_path,))
        self._add_asset_metadata_and_derivatives(
            "스크린샷 2026-08-20 21.23.05.png",
        )
        self._add_reference_tables()
        evidence = self._inspect(_DiskGraph())
        real_delete = startup_preflight._delete_cleanup_candidate

        def attempt_competing_publish(connection, candidate):
            root = file_ops.open_verified_media_root(str(self.media_root))
            try:
                try:
                    with file_ops.media_lifecycle_lock(root):
                        self._write_media(canonical_path, b"raced-publish")
                except file_ops.MediaLifecycleLockError as error:
                    self.assertEqual(error.code, "media_lifecycle_busy")
            finally:
                root.close()
            return real_delete(connection, candidate)

        with mock.patch.object(
            startup_preflight,
            "_delete_cleanup_candidate",
            side_effect=attempt_competing_publish,
        ):
            removed = self._cleanup(evidence)

        self.assertEqual(removed, 1)
        self.assertFalse(self.media_root.joinpath(canonical_path).exists())

    def test_old_schema_fixed_slot_requires_canonical_derivative_closure(self):
        relative_path = "originals/{}/original.png".format(ASSET_UUID)
        self._create_database(image_paths=(relative_path,))
        self._add_old_schema_derivatives()
        self._write_media(relative_path, b"legacy-fixed-slot")

        evidence = self._inspect(_DiskGraph())

        self.assertTrue(evidence.has_fixed_slot_paths)
        self.assertTrue(evidence.has_named_canonical_paths)
        self.assertEqual(
            evidence.distinct_legacy_bytes,
            len(b"legacy-fixed-slot"),
        )

    def test_old_schema_non_fixed_original_remains_named_evidence(self):
        relative_path = "originals/{}/holiday.png".format(ASSET_UUID)
        self._create_database(image_paths=(relative_path,))
        self._write_media(relative_path, b"named-canonical")

        evidence = self._inspect(_DiskGraph())

        self.assertFalse(evidence.has_fixed_slot_paths)
        self.assertTrue(evidence.has_named_canonical_paths)
        self.assertEqual(evidence.distinct_legacy_bytes, 0)

    def test_effective_media_image_directory_is_independent_evidence(self):
        self._create_database()
        (self.media_root / "image").mkdir()

        evidence = self._inspect(_DiskGraph())

        self.assertTrue(evidence.has_media_image_directory)
        self.assertTrue(evidence.has_legacy_evidence)
        self.assertFalse(evidence.has_md5_paths)
        self.assertFalse(evidence.has_fixed_slot_paths)

    def test_none_graph_loads_disk_migrations_without_database_connection(self):
        self._create_database()
        graph = _DiskGraph(("django_images", "0001_initial"))
        loader = mock.Mock(graph=graph)

        with mock.patch(
            "django_images.services.startup_preflight.MigrationLoader",
            return_value=loader,
        ) as migration_loader:
            evidence = startup_preflight.inspect_legacy_evidence(
                str(self.database_path),
                None,
                str(self.media_root),
            )

        migration_loader.assert_called_once_with(None)
        self.assertEqual(
            evidence.pending_migrations,
            (("django_images", "0001_initial"),),
        )

    def test_existing_database_is_opened_only_with_read_only_uri(self):
        self._create_database()
        real_connect = sqlite3.connect

        with mock.patch.object(
            startup_preflight.sqlite3,
            "connect",
            wraps=real_connect,
        ) as connect:
            evidence = self._inspect(_DiskGraph())

        args, kwargs = connect.call_args
        self.assertTrue(args[0].startswith("file:"))
        self.assertIn("mode=ro", args[0])
        self.assertEqual(kwargs, {"uri": True})
        database_stat = os.stat(str(self.database_path))
        self.assertEqual(evidence.database_identity, {
            "device": database_stat.st_dev,
            "inode": database_stat.st_ino,
        })

    def test_pathlike_database_setting_uses_the_verified_normalized_path(self):
        self._create_database()

        evidence = startup_preflight.inspect_legacy_evidence(
            self.database_path,
            _DiskGraph(),
            str(self.media_root),
        )

        self.assertTrue(evidence.database_exists)

    def test_database_identity_is_rechecked_after_connection_close(self):
        self._create_database()
        decoy_path = self.root_path / "close-decoy.db"
        connection = sqlite3.connect(str(decoy_path))
        connection.execute(
            "CREATE TABLE django_migrations "
            "(id INTEGER, app TEXT, name TEXT, applied TEXT)"
        )
        connection.commit()
        connection.close()
        original_path = self.root_path / "close-original.db"
        real_connect = sqlite3.connect

        class SwapOnClose(object):
            def __init__(self, wrapped):
                self.wrapped = wrapped

            def execute(self, *args, **kwargs):
                return self.wrapped.execute(*args, **kwargs)

            def close(self):
                self.wrapped.close()
                self.database_path.rename(original_path)
                decoy_path.rename(self.database_path)

        def connect_then_wrap(database_uri, uri=False):
            wrapped = SwapOnClose(real_connect(database_uri, uri=uri))
            wrapped.database_path = self.database_path
            return wrapped

        with mock.patch.object(
            startup_preflight.sqlite3,
            "connect",
            side_effect=connect_then_wrap,
        ):
            with self.assertRaises(
                startup_preflight.StartupPreflightError
            ) as caught:
                self._inspect(_DiskGraph())

        self.assertEqual(caught.exception.code, "legacy_database_invalid")

    def test_database_namespace_change_during_read_fails_closed(self):
        self._create_database()
        decoy_path = self.root_path / "decoy.db"
        connection = sqlite3.connect(str(decoy_path))
        connection.execute(
            "CREATE TABLE django_migrations "
            "(id INTEGER, app TEXT, name TEXT, applied TEXT)"
        )
        connection.commit()
        connection.close()
        original_path = self.root_path / "original.db"
        real_connect = sqlite3.connect

        def replace_then_connect(database_uri, uri=False):
            self.database_path.rename(original_path)
            decoy_path.rename(self.database_path)
            return real_connect(database_uri, uri=uri)

        with mock.patch.object(
            startup_preflight.sqlite3,
            "connect",
            side_effect=replace_then_connect,
        ):
            with self.assertRaises(
                startup_preflight.StartupPreflightError
            ) as caught:
                self._inspect(_DiskGraph())

        self.assertEqual(caught.exception.code, "legacy_database_invalid")

    def test_database_aba_during_evidence_connection_fails_closed(self):
        self._create_database()
        replacement_path = self.root_path / "replacement.db"
        held_path = self.root_path / "held.db"
        self._copy_database(replacement_path)
        real_connect = sqlite3.connect
        swapped = []

        def swap_around_read_connect(*arguments, **keywords):
            uri = arguments[0] if arguments else keywords.get("database", "")
            if not swapped and "mode=ro" in uri:
                swapped.append(True)
                self.database_path.rename(held_path)
                replacement_path.rename(self.database_path)
                try:
                    return real_connect(*arguments, **keywords)
                finally:
                    self.database_path.rename(replacement_path)
                    held_path.rename(self.database_path)
            return real_connect(*arguments, **keywords)

        with mock.patch.object(
            startup_preflight.sqlite3,
            "connect",
            side_effect=swap_around_read_connect,
        ), self.assertRaises(
            startup_preflight.StartupPreflightError
        ) as caught:
            self._inspect(_DiskGraph())

        self.assertEqual(swapped, [True])
        self.assertEqual(caught.exception.code, "legacy_database_invalid")

    def test_database_symlink_hardlink_and_directory_are_rejected(self):
        outside = self.root_path / "outside.db"
        connection = sqlite3.connect(str(outside))
        connection.close()

        for case in ("symlink", "hardlink", "directory"):
            with self.subTest(case=case):
                if self.database_path.is_symlink():
                    self.database_path.unlink()
                elif self.database_path.is_dir():
                    self.database_path.rmdir()
                elif self.database_path.exists():
                    self.database_path.unlink()
                second_link = self.root_path / "second.db"
                if second_link.exists():
                    second_link.unlink()
                if case == "symlink":
                    self.database_path.symlink_to(outside)
                elif case == "directory":
                    self.database_path.mkdir()
                else:
                    os.link(str(outside), str(self.database_path))
                    os.link(str(outside), str(second_link))

                with self.assertRaises(
                    startup_preflight.StartupPreflightError
                ) as caught:
                    self._inspect(_DiskGraph())
                self.assertEqual(
                    caught.exception.code, "legacy_database_invalid"
                )

    def test_invalid_existing_volume_reports_the_failing_evidence_stage(self):
        cases = ("media-root-file", "corrupt-database", "invalid-graph")
        for case in cases:
            with self.subTest(case=case):
                if self.database_path.exists():
                    self.database_path.unlink()
                if self.media_root.is_file():
                    self.media_root.unlink()
                    self.media_root.mkdir()
                if case == "media-root-file":
                    self.media_root.rmdir()
                    self.media_root.write_bytes(b"not-a-directory")
                    expected = "legacy_media_root_invalid"
                    graph = _DiskGraph()
                elif case == "corrupt-database":
                    self.database_path.write_bytes(b"not-sqlite")
                    expected = "legacy_database_schema_invalid"
                    graph = _DiskGraph()
                else:
                    self._create_database()
                    expected = "legacy_migration_graph_invalid"
                    graph = mock.Mock(nodes={"invalid": object()})

                with self.assertRaises(
                    startup_preflight.StartupPreflightError
                ) as caught:
                    self._inspect(graph)

                self.assertEqual(caught.exception.code, expected)

    def test_database_budget_counts_main_file_not_wal_sidecar(self):
        self._create_database()
        database_bytes = self.database_path.stat().st_size
        Path(str(self.database_path) + "-wal").write_bytes(b"w" * 8192)

        evidence = self._inspect(_DiskGraph())

        self.assertEqual(evidence.database_bytes, database_bytes)


class SpaceBudgetTests(SimpleTestCase):
    def test_initial_space_uses_five_percent_or_64_mib_margin(self):
        budget = startup_preflight.calculate_initial_space(
            database_bytes=20 * MIB,
            distinct_legacy_bytes=80 * MIB,
        )
        self.assertEqual(budget.base_bytes, 100 * MIB)
        self.assertEqual(budget.margin_bytes, 64 * MIB)
        self.assertEqual(budget.required_bytes, 164 * MIB)

    def test_initial_space_rounds_five_percent_up_to_integer_byte(self):
        base_bytes = (64 * MIB * 20) + 1

        budget = startup_preflight.calculate_initial_space(
            database_bytes=base_bytes,
            distinct_legacy_bytes=0,
        )

        self.assertEqual(budget.margin_bytes, (base_bytes + 19) // 20)
        self.assertEqual(
            budget.required_bytes, base_bytes + ((base_bytes + 19) // 20)
        )

    def test_remaining_space_reuses_the_exact_initial_margin(self):
        budget = startup_preflight.calculate_remaining_space(
            copy_required_bytes=123,
            initial_margin=456,
        )
        self.assertEqual(budget.base_bytes, 123)
        self.assertEqual(budget.margin_bytes, 456)
        self.assertEqual(budget.required_bytes, 579)

    def test_available_space_uses_bavail_times_frsize_only(self):
        filesystem = mock.Mock(
            f_bavail=7,
            f_frsize=4096,
            f_bfree=999999,
            f_bsize=8192,
        )
        with mock.patch.object(
            startup_preflight.os, "statvfs", return_value=filesystem
        ):
            available = startup_preflight.available_space_bytes("/unused")

        self.assertEqual(available, 7 * 4096)


class StoragePreflightTests(SimpleTestCase):
    def setUp(self):
        self.temporary_root = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_root.cleanup)
        self.media_root = Path(os.path.realpath(self.temporary_root.name))

    @property
    def lock_directory(self):
        return self.media_root / ".pinry-locks"

    def _storage(self, location=None):
        return FileSystemStorage(
            location=str(self.media_root if location is None else location),
            base_url="/media/",
        )

    def _validate(
        self,
        image_storage=None,
        thumbnail_storage=None,
        image_sizes=None,
    ):
        return startup_preflight.validate_storage_preflight(
            str(self.media_root),
            self._storage() if image_storage is None else image_storage,
            (
                self._storage()
                if thumbnail_storage is None
                else thumbnail_storage
            ),
            VALID_IMAGE_SIZES if image_sizes is None else image_sizes,
            os.geteuid(),
            os.getegid(),
        )

    def _write_lock(self, name, content=b"lock", mode=0o600):
        self.lock_directory.mkdir(mode=0o700, exist_ok=True)
        path = self.lock_directory / name
        path.write_bytes(content)
        os.chmod(str(path), mode)
        return path

    def _service_identity_probe_context(self):
        stack = ExitStack()
        if os.geteuid() != 0:
            stack.enter_context(mock.patch.object(
                startup_preflight,
                "_drop_service_identity",
            ))
            stack.enter_context(mock.patch.object(
                startup_preflight.os,
                "getgroups",
                return_value=[],
            ))
        return stack

    def test_valid_local_storage_sizes_and_service_probes_succeed(self):
        with self._service_identity_probe_context():
            result = self._validate()

        self.assertTrue(result.ok)
        self.assertIsNone(result.reason_code)
        self.assertEqual(result.field_classes, ())
        self.assertFalse(
            any(path.name.startswith(".svrx-pinry-write-probe-")
                for path in self.media_root.iterdir())
        )

    def test_configuration_preflight_never_forks_service_probe(self):
        with mock.patch.object(
            startup_preflight,
            "_run_service_probe",
        ) as service_probe:
            result = startup_preflight.validate_storage_configuration_preflight(
                str(self.media_root),
                self._storage(),
                self._storage(),
                VALID_IMAGE_SIZES,
                os.geteuid(),
                os.getegid(),
            )

        self.assertTrue(result.ok)
        service_probe.assert_not_called()

    def test_runtime_preflight_only_runs_the_service_identity_probe(self):
        with mock.patch.object(
            startup_preflight.file_ops,
            "recover_media_lock_state",
        ) as recover_lock_state, mock.patch.object(
            startup_preflight,
            "_run_service_probe",
            return_value="ok",
        ) as service_probe:
            result = startup_preflight.validate_storage_runtime_preflight(
                str(self.media_root),
                os.geteuid(),
                os.getegid(),
            )

        self.assertTrue(result.ok)
        recover_lock_state.assert_not_called()
        service_probe.assert_called_once_with(
            str(self.media_root),
            os.geteuid(),
            os.getegid(),
        )

    def test_missing_media_layout_is_created_only_below_verified_data_root(self):
        data_root = self.media_root / "data"
        data_root.mkdir()
        media_root = data_root / "static" / "media"

        startup_preflight.ensure_media_root_layout(
            str(data_root),
            str(media_root),
            os.geteuid(),
            os.getegid(),
        )

        self.assertTrue(media_root.is_dir())
        self.assertEqual(media_root.stat().st_uid, os.geteuid())
        self.assertEqual(media_root.stat().st_gid, os.getegid())
        self.assertEqual(stat.S_IMODE(media_root.stat().st_mode), 0o700)

    def test_media_layout_rejects_outside_and_symlinked_components(self):
        data_root = self.media_root / "data"
        data_root.mkdir()
        outside = self.media_root / "outside"
        outside.mkdir()
        linked = data_root / "linked"
        linked.symlink_to(outside, target_is_directory=True)

        for media_root in (outside / "media", linked / "media"):
            with self.subTest(media_root=media_root), self.assertRaisesRegex(
                startup_preflight.StartupPreflightError,
                "^media_storage_configuration_invalid$",
            ):
                startup_preflight.ensure_media_root_layout(
                    str(data_root),
                    str(media_root),
                    os.geteuid(),
                    os.getegid(),
                )

        self.assertFalse((outside / "media").exists())

    def test_selective_ownership_preserves_root_owned_startup_lock(self):
        data_root = self.media_root / "data"
        data_root.mkdir()
        managed = data_root / "static"
        nested = managed / "media" / "asset.bin"
        nested.parent.mkdir(parents=True)
        nested.write_bytes(b"asset")
        unmanaged = data_root / "unmanaged.bin"
        unmanaged.write_bytes(b"unmanaged")
        held = startup_lock.acquire_startup_lock(str(data_root))
        self.addCleanup(held.close)
        lock_path = data_root / startup_lock.STARTUP_LOCK_FILENAME
        lock_before = os.stat(str(lock_path), follow_symlinks=False)
        unmanaged_before = os.stat(str(unmanaged), follow_symlinks=False)
        lock_identity = (lock_before.st_dev, lock_before.st_ino)
        chowned_identities = []
        real_fchown = os.fchown

        def record_fchown(descriptor, uid, gid):
            current = os.fstat(descriptor)
            chowned_identities.append((current.st_dev, current.st_ino))
            return real_fchown(descriptor, uid, gid)

        with mock.patch.object(
            startup_preflight.os,
            "fchown",
            side_effect=record_fchown,
        ):
            startup_preflight.adjust_storage_ownership(
                str(data_root),
                (str(managed),),
                os.geteuid(),
                os.getegid(),
                held.fileno(),
            )

        lock_after = os.stat(str(lock_path), follow_symlinks=False)
        self.assertNotIn(lock_identity, chowned_identities)
        self.assertEqual(
            (
                lock_after.st_dev,
                lock_after.st_ino,
                lock_after.st_uid,
                lock_after.st_gid,
                stat.S_IMODE(lock_after.st_mode),
                lock_after.st_nlink,
            ),
            (
                lock_before.st_dev,
                lock_before.st_ino,
                lock_before.st_uid,
                lock_before.st_gid,
                stat.S_IMODE(lock_before.st_mode),
                lock_before.st_nlink,
            ),
        )
        self.assertEqual(
            (unmanaged.stat().st_dev, unmanaged.stat().st_ino),
            (unmanaged_before.st_dev, unmanaged_before.st_ino),
        )
        root_after = os.stat(str(data_root), follow_symlinks=False)
        self.assertEqual(root_after.st_uid, lock_before.st_uid)
        self.assertEqual(root_after.st_gid, os.getegid())
        self.assertEqual(stat.S_IMODE(root_after.st_mode), 0o1770)

    def test_ownership_never_normalizes_backup_root_or_archived_payload(self):
        data_root = self.media_root / "data"
        data_root.mkdir()
        backup_root = data_root / "legacy-backup"
        archived = backup_root / "old-run" / "media" / "asset.bin"
        archived.parent.mkdir(parents=True)
        archived.write_bytes(b"archived")
        held = startup_lock.acquire_startup_lock(str(data_root))
        self.addCleanup(held.close)
        backup_identity = (
            backup_root.stat().st_dev,
            backup_root.stat().st_ino,
        )
        archived_identity = (archived.stat().st_dev, archived.stat().st_ino)
        chowned_identities = []
        real_fchown = os.fchown

        def record_fchown(descriptor, uid, gid):
            current = os.fstat(descriptor)
            chowned_identities.append((current.st_dev, current.st_ino))
            return real_fchown(descriptor, uid, gid)

        with mock.patch.object(
            startup_preflight.os,
            "fchown",
            side_effect=record_fchown,
        ):
            startup_preflight.adjust_storage_ownership(
                str(data_root),
                (),
                os.geteuid(),
                os.getegid(),
                held.fileno(),
            )

        self.assertNotIn(backup_identity, chowned_identities)
        self.assertNotIn(archived_identity, chowned_identities)

    def test_ownership_normalizes_outside_path_to_unsafe_reason(self):
        data_root = self.media_root / "data"
        data_root.mkdir()
        outside = self.media_root / "outside"
        outside.mkdir()
        held = startup_lock.acquire_startup_lock(str(data_root))
        self.addCleanup(held.close)

        with self.assertRaisesRegex(
            startup_preflight.StartupPreflightError,
            "^unsafe_storage_ownership$",
        ):
            startup_preflight.adjust_storage_ownership(
                str(data_root),
                (str(outside),),
                os.geteuid(),
                os.getegid(),
                held.fileno(),
            )

    def test_default_storage_wrapper_resolves_to_exact_filesystem_storage(self):
        with self.settings(MEDIA_ROOT=str(self.media_root)):
            storage = DefaultStorage()
            with mock.patch.object(
                startup_preflight,
                "_run_service_probe",
                return_value="ok",
            ):
                result = startup_preflight.validate_storage_preflight(
                    str(self.media_root),
                    storage,
                    storage,
                    VALID_IMAGE_SIZES,
                    os.geteuid(),
                    os.getegid(),
                )

        self.assertTrue(result.ok)

    def test_filesystem_storage_subclass_is_not_treated_as_exact_local_class(self):
        class CustomFileSystemStorage(FileSystemStorage):
            pass

        result = self._validate(
            image_storage=CustomFileSystemStorage(
                location=str(self.media_root)
            )
        )

        self.assertEqual(
            result.reason_code,
            "media_storage_configuration_invalid",
        )
        self.assertEqual(result.field_classes, ("Image.image.storage",))

    def test_invalid_configuration_reports_only_fixed_field_classes(self):
        class SecretStorage(object):
            @property
            def location(self):
                raise RuntimeError("top-secret-storage-value")

        cases = (
            (
                "storage-class",
                SecretStorage(),
                self._storage(),
                VALID_IMAGE_SIZES,
                ("Image.image.storage",),
            ),
            (
                "storage-location",
                self._storage(self.media_root / "other"),
                self._storage(),
                VALID_IMAGE_SIZES,
                ("Image.image.storage",),
            ),
            (
                "image-sizes",
                self._storage(),
                self._storage(),
                {"thumbnail": {"size": [240, 0]}},
                ("IMAGE_SIZES",),
            ),
        )
        for name, image_storage, thumbnail_storage, sizes, fields in cases:
            with self.subTest(name=name):
                result = self._validate(
                    image_storage=image_storage,
                    thumbnail_storage=thumbnail_storage,
                    image_sizes=sizes,
                )
                self.assertFalse(result.ok)
                self.assertEqual(
                    result.reason_code,
                    "media_storage_configuration_invalid",
                )
                self.assertEqual(result.field_classes, fields)
                self.assertNotIn("top-secret", repr(result))

    def test_media_root_must_be_an_existing_real_directory(self):
        missing = self.media_root / "missing"
        outside = self.media_root / "outside"
        outside.mkdir()
        symlink = self.media_root / "linked"
        symlink.symlink_to(outside, target_is_directory=True)

        for path in (missing, symlink):
            with self.subTest(path=path.name):
                storage = FileSystemStorage(location=str(path))
                result = startup_preflight.validate_storage_preflight(
                    str(path),
                    storage,
                    storage,
                    VALID_IMAGE_SIZES,
                    os.geteuid(),
                    os.getegid(),
                )
                self.assertEqual(
                    result.reason_code,
                    "media_storage_configuration_invalid",
                )
                self.assertEqual(result.field_classes, ("MEDIA_ROOT",))

    def test_image_size_options_reject_unsupported_runtime_shapes(self):
        invalid_options = (
            {"size": [0, 0]},
            {"size": [0, 0], "crop": True},
            {"size": [100]},
            {"size": [100, True]},
            {"size": [100, 100], "crop": "yes"},
            {"size": [100, 100], "unknown": True},
            {"size": [100, 100], "quality": 96},
        )
        for options in invalid_options:
            with self.subTest(options=options):
                sizes = dict(VALID_IMAGE_SIZES)
                sizes["thumbnail"] = options
                result = self._validate(image_sizes=sizes)
                self.assertEqual(
                    result.reason_code,
                    "media_storage_configuration_invalid",
                )
                self.assertEqual(result.field_classes, ("IMAGE_SIZES",))

    def test_crop_allows_one_runtime_unbounded_dimension(self):
        for size in ([125, 0], [0, 125]):
            with self.subTest(size=size):
                sizes = dict(VALID_IMAGE_SIZES)
                sizes["square"] = {"size": size, "crop": True}
                with mock.patch.object(
                    startup_preflight,
                    "_run_service_probe",
                    return_value="ok",
                ):
                    result = self._validate(image_sizes=sizes)

                self.assertTrue(result.ok)

    def test_unknown_symlink_hardlink_and_wrong_name_change_nothing(self):
        outside = self.media_root / "outside"
        outside.write_bytes(b"outside")
        for case in ("unknown", "symlink", "hardlink", "wrong-name"):
            with self.subTest(case=case):
                if self.lock_directory.exists():
                    for child in self.lock_directory.iterdir():
                        child.unlink()
                    self.lock_directory.rmdir()
                known = self._write_lock(
                    "media-lifecycle.lock", b"known", mode=0o640
                )
                os.chmod(str(self.lock_directory), 0o755)
                if case == "unknown":
                    (self.lock_directory / "unknown").write_bytes(b"unknown")
                elif case == "wrong-name":
                    (self.lock_directory / "media-dedup-gg.lock").write_bytes(
                        b"wrong"
                    )
                elif case == "symlink":
                    (self.lock_directory / "media-dedup-00.lock").symlink_to(
                        outside
                    )
                else:
                    os.link(
                        str(known),
                        str(self.media_root / "known-second-link"),
                    )

                before_directory_mode = stat.S_IMODE(
                    self.lock_directory.stat().st_mode
                )
                before_file_mode = stat.S_IMODE(known.stat().st_mode)
                result = self._validate()

                self.assertEqual(result.reason_code, "unsafe_media_lock_state")
                self.assertEqual(
                    stat.S_IMODE(self.lock_directory.stat().st_mode),
                    before_directory_mode,
                )
                self.assertEqual(
                    stat.S_IMODE(known.stat().st_mode), before_file_mode
                )
                self.assertEqual(outside.read_bytes(), b"outside")

    def test_symlinked_lock_directory_is_never_repaired(self):
        outside_directory = self.media_root / "outside-locks"
        outside_directory.mkdir(mode=0o755)
        outside_lock = outside_directory / "media-lifecycle.lock"
        outside_lock.write_bytes(b"outside-lock")
        os.chmod(str(outside_lock), 0o640)
        self.lock_directory.symlink_to(
            outside_directory,
            target_is_directory=True,
        )

        result = self._validate()

        self.assertEqual(result.reason_code, "unsafe_media_lock_state")
        self.assertEqual(
            stat.S_IMODE(outside_directory.stat().st_mode), 0o755
        )
        self.assertEqual(stat.S_IMODE(outside_lock.stat().st_mode), 0o640)
        self.assertEqual(outside_lock.read_bytes(), b"outside-lock")

    def test_busy_known_lock_prevents_every_metadata_change(self):
        first = self._write_lock(
            "media-dedup-00.lock", b"first", mode=0o640
        )
        busy = self._write_lock(
            "media-lifecycle.lock", b"busy", mode=0o640
        )
        os.chmod(str(self.lock_directory), 0o755)
        blocker = os.open(str(busy), os.O_RDWR)
        try:
            fcntl.flock(blocker, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self._validate()
        finally:
            fcntl.flock(blocker, fcntl.LOCK_UN)
            os.close(blocker)

        self.assertEqual(result.reason_code, "unsafe_media_lock_state")
        self.assertEqual(stat.S_IMODE(first.stat().st_mode), 0o640)
        self.assertEqual(stat.S_IMODE(busy.stat().st_mode), 0o640)
        self.assertEqual(
            stat.S_IMODE(self.lock_directory.stat().st_mode), 0o755
        )

    def test_recovery_closes_file_descriptor_when_post_open_stat_fails(self):
        self._write_lock("media-lifecycle.lock")
        opened_descriptors = []
        real_open = file_ops._open_recovery_lock_file
        real_fstat = file_ops.os.fstat

        def capture_open(directory_descriptor, name):
            descriptor = real_open(directory_descriptor, name)
            opened_descriptors.append(descriptor)
            return descriptor

        def fail_opened_descriptor(descriptor):
            if descriptor in opened_descriptors:
                raise OSError("injected post-open stat failure")
            return real_fstat(descriptor)

        with mock.patch.object(
            file_ops,
            "_open_recovery_lock_file",
            side_effect=capture_open,
        ), mock.patch.object(
            file_ops.os,
            "fstat",
            side_effect=fail_opened_descriptor,
        ):
            result = self._validate()

        self.assertEqual(result.reason_code, "unsafe_media_lock_state")
        self.assertEqual(len(opened_descriptors), 1)
        with self.assertRaises(OSError) as caught:
            os.fstat(opened_descriptors[0])
        self.assertEqual(caught.exception.errno, 9)

    def test_safe_recovery_preserves_inode_and_content_then_repairs_metadata(self):
        lifecycle = self._write_lock(
            "media-lifecycle.lock", b"lifecycle-content", mode=0o640
        )
        dedup = self._write_lock(
            "media-dedup-00.lock", b"dedup-content", mode=0o640
        )
        os.chmod(str(self.lock_directory), 0o755)
        identities = {
            path.name: (path.stat().st_dev, path.stat().st_ino)
            for path in (lifecycle, dedup)
        }

        with self._service_identity_probe_context():
            result = self._validate()

        self.assertTrue(result.ok)
        self.assertEqual(lifecycle.read_bytes(), b"lifecycle-content")
        self.assertEqual(dedup.read_bytes(), b"dedup-content")
        self.assertEqual(
            (lifecycle.stat().st_dev, lifecycle.stat().st_ino),
            identities[lifecycle.name],
        )
        self.assertEqual(
            (dedup.stat().st_dev, dedup.stat().st_ino),
            identities[dedup.name],
        )
        self.assertEqual(stat.S_IMODE(lifecycle.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(dedup.stat().st_mode), 0o600)
        self.assertEqual(
            stat.S_IMODE(self.lock_directory.stat().st_mode), 0o700
        )
        self.assertEqual(lifecycle.stat().st_uid, os.geteuid())
        self.assertEqual(lifecycle.stat().st_gid, os.getegid())

    def test_recovery_releases_every_lock_before_service_probe(self):
        lifecycle = self._write_lock("media-lifecycle.lock")
        probe_calls = []

        def assert_lock_released(media_root, service_uid, service_gid):
            probe_calls.append((media_root, service_uid, service_gid))
            descriptor = os.open(str(lifecycle), os.O_RDWR)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
            return "ok"

        with mock.patch.object(
            startup_preflight,
            "_run_service_probe",
            side_effect=assert_lock_released,
        ):
            result = self._validate()

        self.assertTrue(result.ok)
        self.assertEqual(len(probe_calls), 1)

    def test_actual_service_child_distinguishes_write_failure(self):
        if os.geteuid() == 0:
            self.skipTest("root can write through owner permission probes")
        os.chmod(str(self.media_root), 0o500)
        self.addCleanup(lambda: os.chmod(str(self.media_root), 0o700))

        with self._service_identity_probe_context():
            result = self._validate()

        self.assertEqual(result.reason_code, "media_root_not_writable")

    def test_actual_service_child_distinguishes_lock_failure(self):
        with self._service_identity_probe_context(), mock.patch(
            "django_images.services.startup_preflight.file_ops."
            "media_dedup_lock",
            side_effect=file_ops.MediaPathError("sensitive-lock-error"),
        ):
            result = self._validate()

        self.assertEqual(result.reason_code, "media_lock_not_usable")
        self.assertNotIn("sensitive", repr(result))

    def test_privilege_drop_clears_groups_before_gid_and_uid(self):
        calls = []
        with mock.patch.object(
            startup_preflight.os,
            "setgroups",
            side_effect=lambda groups: calls.append(("groups", groups)),
        ), mock.patch.object(
            startup_preflight.os,
            "setgid",
            side_effect=lambda gid: calls.append(("gid", gid)),
        ), mock.patch.object(
            startup_preflight.os,
            "setuid",
            side_effect=lambda uid: calls.append(("uid", uid)),
        ):
            startup_preflight._drop_service_identity(123, 456)

        self.assertEqual(
            calls,
            [("groups", []), ("gid", 456), ("uid", 123)],
        )

    def test_service_probe_clears_groups_when_ids_already_match(self):
        calls = []
        groups = [777]

        def clear_groups(value):
            calls.append(("groups", value))
            groups[:] = []

        with mock.patch.object(
            startup_preflight.os,
            "geteuid",
            return_value=123,
        ), mock.patch.object(
            startup_preflight.os,
            "getegid",
            return_value=456,
        ), mock.patch.object(
            startup_preflight.os,
            "getgroups",
            side_effect=lambda: list(groups),
        ), mock.patch.object(
            startup_preflight.os,
            "setgroups",
            side_effect=clear_groups,
        ), mock.patch.object(
            startup_preflight.os,
            "setgid",
            side_effect=lambda gid: calls.append(("gid", gid)),
        ), mock.patch.object(
            startup_preflight.os,
            "setuid",
            side_effect=lambda uid: calls.append(("uid", uid)),
        ), mock.patch.object(
            startup_preflight,
            "_probe_media_root_write",
        ), mock.patch.object(
            startup_preflight,
            "_probe_media_locks",
        ):
            result = startup_preflight._service_probe_child(
                str(self.media_root),
                123,
                456,
            )

        self.assertEqual(result, "ok")
        self.assertEqual(
            calls,
            [("groups", []), ("gid", 456), ("uid", 123)],
        )

    def test_service_probe_rejects_groups_left_with_matching_ids(self):
        with mock.patch.object(
            startup_preflight,
            "_drop_service_identity",
        ), mock.patch.object(
            startup_preflight.os,
            "geteuid",
            return_value=123,
        ), mock.patch.object(
            startup_preflight.os,
            "getegid",
            return_value=456,
        ), mock.patch.object(
            startup_preflight.os,
            "getgroups",
            return_value=[777],
        ), mock.patch.object(
            startup_preflight,
            "_probe_media_root_write",
        ) as write_probe, mock.patch.object(
            startup_preflight,
            "_probe_media_locks",
        ) as lock_probe:
            result = startup_preflight._service_probe_child(
                str(self.media_root),
                123,
                456,
            )

        self.assertEqual(result, "media_root_not_writable")
        write_probe.assert_not_called()
        lock_probe.assert_not_called()

    def test_service_probe_rejects_ineffective_identity_drop(self):
        with mock.patch.object(
            startup_preflight,
            "_drop_service_identity",
        ), mock.patch.object(
            startup_preflight.os,
            "geteuid",
            return_value=999,
        ), mock.patch.object(
            startup_preflight.os,
            "getegid",
            return_value=998,
        ), mock.patch.object(
            startup_preflight,
            "_probe_media_root_write",
        ) as write_probe, mock.patch.object(
            startup_preflight,
            "_probe_media_locks",
        ) as lock_probe:
            result = startup_preflight._service_probe_child(
                str(self.media_root),
                123,
                456,
            )

        self.assertEqual(result, "media_root_not_writable")
        write_probe.assert_not_called()
        lock_probe.assert_not_called()

    def test_service_probe_rejects_bad_exit_or_unallowlisted_payload(self):
        child_pid = 123
        cases = (
            ("nonzero-exit", b"ok", 1 << 8),
            ("unallowlisted-payload", b"sensitive-value", 0),
        )
        for name, payload, wait_status in cases:
            with self.subTest(name=name), mock.patch.object(
                startup_preflight.os,
                "pipe",
                return_value=(10, 11),
            ), mock.patch.object(
                startup_preflight.os,
                "fork",
                return_value=child_pid,
            ), mock.patch.object(
                startup_preflight.os,
                "close",
            ), mock.patch.object(
                startup_preflight.os,
                "read",
                side_effect=(payload, b""),
            ), mock.patch.object(
                startup_preflight.os,
                "waitpid",
                return_value=(child_pid, wait_status),
            ):
                result = startup_preflight._run_service_probe(
                    str(self.media_root),
                    os.geteuid(),
                    os.getegid(),
                )

            self.assertEqual(result, "media_lock_not_usable")

    def test_pipe_creation_failure_is_normalized_to_allowlisted_reason(self):
        with mock.patch.object(
            startup_preflight.os,
            "pipe",
            side_effect=OSError("sensitive-pipe-error"),
        ):
            try:
                result = startup_preflight._run_service_probe(
                    str(self.media_root),
                    os.geteuid(),
                    os.getegid(),
                )
            except OSError:
                self.fail("pipe failure escaped the fixed reason contract")

        self.assertEqual(result, "media_lock_not_usable")

    def test_probe_child_closes_unrelated_inherited_descriptor(self):
        inherited = os.open(str(self.media_root), os.O_RDONLY)
        self.addCleanup(os.close, inherited)
        expected = os.fstat(inherited)

        def reject_open_descriptor(media_root, service_uid, service_gid):
            del media_root, service_uid, service_gid
            try:
                current = os.fstat(inherited)
            except OSError as error:
                if error.errno == 9:
                    return "ok"
                raise
            if (current.st_dev, current.st_ino) == (
                expected.st_dev,
                expected.st_ino,
            ):
                return "media_lock_not_usable"
            return "ok"

        with mock.patch.object(
            startup_preflight,
            "_service_probe_child",
            side_effect=reject_open_descriptor,
        ):
            result = startup_preflight._run_service_probe(
                str(self.media_root),
                os.geteuid(),
                os.getegid(),
            )

        self.assertEqual(result, "ok")

    def test_probe_child_closes_descriptor_above_lowered_soft_limit(self):
        project_root = str(Path(__file__).resolve().parent.parent)
        script = "\n".join((
            "import errno, fcntl, os, resource, sys",
            "from django_images.services import startup_preflight",
            "media_root = sys.argv[1]",
            "soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)",
            "target = 128 if hard == resource.RLIM_INFINITY else min(128, hard - 1)",
            "if target < 16:",
            "    print('unsupported', flush=True)",
            "    raise SystemExit(0)",
            "if soft <= target:",
            "    resource.setrlimit(resource.RLIMIT_NOFILE, (target + 1, hard))",
            "source = os.open(media_root, os.O_RDONLY)",
            "high_descriptor = fcntl.fcntl(source, fcntl.F_DUPFD, target)",
            "expected = os.fstat(high_descriptor)",
            "lowered_soft = max(8, min(64, high_descriptor - 1))",
            "resource.setrlimit(resource.RLIMIT_NOFILE, (lowered_soft, hard))",
            "def reject_retained_descriptor(*unused):",
            "    try:",
            "        current = os.fstat(high_descriptor)",
            "    except OSError as error:",
            "        if error.errno == errno.EBADF:",
            "            return 'ok'",
            "        raise",
            "    if (current.st_dev, current.st_ino) == (expected.st_dev, expected.st_ino):",
            "        return 'media_lock_not_usable'",
            "    return 'ok'",
            "startup_preflight._service_probe_child = reject_retained_descriptor",
            "result = startup_preflight._run_service_probe(",
            "    media_root, os.geteuid(), os.getegid()",
            ")",
            "print(result, flush=True)",
        ))
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(self.media_root)],
            cwd=project_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )

        stdout, stderr = process.communicate(timeout=10)

        self.assertEqual(process.returncode, 0)
        if stdout.strip() == "unsupported":
            self.skipTest("RLIMIT_NOFILE hard limit is too small")
        self.assertEqual(stdout.strip(), "ok")
        self.assertEqual(stderr, "")

    def test_probe_descriptor_enumeration_uses_macos_dev_fd(self):
        closed = []

        def list_descriptors(path):
            if path == "/proc/self/fd":
                raise FileNotFoundError(path)
            self.assertEqual(path, "/dev/fd")
            return ("0", "1", "2", "3", "91", "not-a-descriptor")

        with mock.patch.object(
            startup_preflight.os,
            "listdir",
            side_effect=list_descriptors,
        ), mock.patch.object(
            startup_preflight.os,
            "close",
            side_effect=closed.append,
        ), mock.patch.object(
            startup_preflight.os,
            "closerange",
        ) as closerange:
            startup_preflight._close_service_probe_descriptors()

        self.assertEqual(closed, [91])
        closerange.assert_not_called()

    def test_probe_descriptor_fallback_uses_hard_rlimit(self):
        with mock.patch.object(
            startup_preflight.os,
            "listdir",
            side_effect=OSError("descriptor directory unavailable"),
        ), mock.patch.object(
            startup_preflight.resource,
            "getrlimit",
            return_value=(32, 4096),
        ), mock.patch.object(
            startup_preflight.os,
            "closerange",
        ) as closerange:
            startup_preflight._close_service_probe_descriptors()

        closerange.assert_called_once_with(4, 4096)

    def test_parent_close_releases_startup_lock_while_probe_child_lives(self):
        project_root = str(Path(__file__).resolve().parent.parent)
        ready_path = self.media_root / "probe-ready"
        release_path = self.media_root / "probe-release"
        closed_path = self.media_root / "parent-lock-closed"
        script = "\n".join((
            "import os, pathlib, sys, time",
            "from django_images.services import startup_lock, startup_preflight",
            "data_root, ready_path, release_path, closed_path = sys.argv[1:]",
            "held = startup_lock.acquire_startup_lock(data_root)",
            "def close_parent_lock():",
            "    held.close()",
            "    pathlib.Path(closed_path).write_bytes(b'closed')",
            "os.register_at_fork(after_in_parent=close_parent_lock)",
            "def blocking_probe(*unused):",
            "    pathlib.Path(ready_path).write_bytes(b'ready')",
            "    while not pathlib.Path(release_path).exists():",
            "        time.sleep(0.01)",
            "    return 'ok'",
            "startup_preflight._service_probe_child = blocking_probe",
            "result = startup_preflight._run_service_probe(",
            "    data_root, os.geteuid(), os.getegid()",
            ")",
            "print(result, flush=True)",
        ))
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(self.media_root),
                str(ready_path),
                str(release_path),
                str(closed_path),
            ],
            cwd=project_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )

        def release_probe():
            if not release_path.exists():
                release_path.write_bytes(b"release")
            if process.poll() is None:
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()

        self.addCleanup(release_probe)
        deadline = time.monotonic() + 5
        while not (ready_path.exists() and closed_path.exists()):
            if process.poll() is not None or time.monotonic() >= deadline:
                stdout, stderr = process.communicate(timeout=5)
                self.fail(
                    "probe did not stay alive: stdout={!r} stderr={!r}".format(
                        stdout, stderr
                    )
                )
            time.sleep(0.01)

        self.assertIsNone(process.poll())
        try:
            with startup_lock.acquire_startup_lock(str(self.media_root)):
                pass
        except startup_lock.StartupLockError as error:
            self.fail("probe child retained startup lock: {}".format(error.code))

        release_path.write_bytes(b"release")
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(stdout.strip(), "ok")
        self.assertEqual(stderr, "")

    def test_write_probe_cleans_owned_file_when_fchmod_fails(self):
        with mock.patch.object(
            startup_preflight.os,
            "fchmod",
            side_effect=OSError("injected fchmod failure"),
        ):
            with self.assertRaises(OSError):
                startup_preflight._probe_media_root_write(
                    str(self.media_root)
                )

        self.assertFalse(any(
            path.name.startswith(".svrx-pinry-write-probe-")
            for path in self.media_root.iterdir()
        ))

    def test_write_probe_retries_first_fstat_to_clean_created_file(self):
        real_fstat = startup_preflight.os.fstat
        real_open_root = file_ops.open_verified_media_root
        opening_root = [False]
        failed_once = [False]

        def capture_root(path):
            opening_root[0] = True
            try:
                return real_open_root(path)
            finally:
                opening_root[0] = False

        def fail_first_probe_stat(descriptor):
            if not opening_root[0] and not failed_once[0]:
                failed_once[0] = True
                raise OSError("injected first fstat failure")
            return real_fstat(descriptor)

        with mock.patch.object(
            startup_preflight.file_ops,
            "open_verified_media_root",
            side_effect=capture_root,
        ), mock.patch.object(
            startup_preflight.os,
            "fstat",
            side_effect=fail_first_probe_stat,
        ):
            with self.assertRaises(OSError):
                startup_preflight._probe_media_root_write(
                    str(self.media_root)
                )

        self.assertTrue(failed_once[0])
        self.assertFalse(any(
            path.name.startswith(".svrx-pinry-write-probe-")
            for path in self.media_root.iterdir()
        ))

    def test_write_probe_cleans_owned_file_when_post_chmod_fstat_fails(self):
        real_fstat = startup_preflight.os.fstat
        real_open_root = file_ops.open_verified_media_root
        opening_root = [False]
        probe_stats = {}

        def capture_root(path):
            opening_root[0] = True
            try:
                return real_open_root(path)
            finally:
                opening_root[0] = False

        def fail_second_probe_stat(descriptor):
            if not opening_root[0]:
                count = probe_stats.get(descriptor, 0) + 1
                probe_stats[descriptor] = count
                if count == 2:
                    raise OSError("injected post-chmod fstat failure")
            return real_fstat(descriptor)

        with mock.patch.object(
            startup_preflight.file_ops,
            "open_verified_media_root",
            side_effect=capture_root,
        ), mock.patch.object(
            startup_preflight.os,
            "fstat",
            side_effect=fail_second_probe_stat,
        ):
            with self.assertRaises(OSError):
                startup_preflight._probe_media_root_write(
                    str(self.media_root)
                )

        self.assertFalse(any(
            path.name.startswith(".svrx-pinry-write-probe-")
            for path in self.media_root.iterdir()
        ))

    def test_probe_cleanup_never_unlinks_a_replacement_inode(self):
        flags = os.O_RDONLY | os.O_DIRECTORY
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        directory_descriptor = os.open(str(self.media_root), flags)
        self.addCleanup(os.close, directory_descriptor)
        probe_name = ".svrx-pinry-write-probe-test"
        probe_path = self.media_root / probe_name
        probe_path.write_bytes(b"owned")
        expected = probe_path.stat()
        preserved_path = self.media_root / "preserved-probe"
        probe_path.rename(preserved_path)
        probe_path.write_bytes(b"replacement")

        removed = startup_preflight._unlink_probe_if_owned(
            directory_descriptor,
            probe_name,
            expected,
        )

        self.assertFalse(removed)
        self.assertEqual(probe_path.read_bytes(), b"replacement")
        self.assertEqual(preserved_path.read_bytes(), b"owned")
