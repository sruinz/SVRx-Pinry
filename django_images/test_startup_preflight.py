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
        self, original_filename, extension=".png"
    ):
        connection = sqlite3.connect(str(self.database_path))
        try:
            connection.execute(
                "ALTER TABLE django_images_image "
                "ADD COLUMN asset_uuid VARCHAR(36)"
            )
            connection.execute(
                "ALTER TABLE django_images_image "
                "ADD COLUMN original_filename VARCHAR(255)"
            )
            connection.execute(
                "UPDATE django_images_image "
                "SET asset_uuid = ?, original_filename = ? WHERE id = 1",
                (ASSET_UUID, original_filename),
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
            self._inspect(_DiskGraph())

        args, kwargs = connect.call_args
        self.assertTrue(args[0].startswith("file:"))
        self.assertIn("mode=ro", args[0])
        self.assertEqual(kwargs, {"uri": True})

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

        self.assertEqual(caught.exception.code, "legacy_evidence_invalid")

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

        self.assertEqual(caught.exception.code, "legacy_evidence_invalid")

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
                    caught.exception.code, "legacy_evidence_invalid"
                )

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
