import os
from pathlib import Path
import tempfile
from unittest import mock

from django.test import SimpleTestCase

from django_images.file_ops import (
    MediaPathError,
    create_owned_staging_file,
    open_or_create_media_directory_from,
    open_verified_media_root,
    publish_preverified_noreplace,
    replace_preverified_destination,
)


class PreverifiedPublicationTests(SimpleTestCase):
    def make_fixture(self, staging_name="preverified.part"):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root_path = os.path.realpath(temporary.name)
        root = open_verified_media_root(root_path)
        self.addCleanup(root.close)
        staging_directory = open_or_create_media_directory_from(
            root, ".staging"
        )
        self.addCleanup(staging_directory.close)
        destination_directory = open_or_create_media_directory_from(
            root, "originals"
        )
        self.addCleanup(destination_directory.close)
        staging = create_owned_staging_file(
            staging_directory, staging_name
        )
        self.addCleanup(staging.close)
        os.write(staging.descriptor, b"verified")
        staging.file_stat = os.fstat(staging.descriptor)
        return root_path, staging_directory, destination_directory, staging

    def test_preverified_publish_performs_no_hash_or_fsync(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root_path = os.path.realpath(temporary.name)
        root = open_verified_media_root(root_path)
        self.addCleanup(root.close)
        staging_directory = open_or_create_media_directory_from(
            root, ".staging"
        )
        self.addCleanup(staging_directory.close)
        destination_directory = open_or_create_media_directory_from(
            root, "originals"
        )
        self.addCleanup(destination_directory.close)
        staging = create_owned_staging_file(
            staging_directory, "preverified.part"
        )
        self.addCleanup(staging.close)
        os.write(staging.descriptor, b"verified")
        staging.file_stat = os.fstat(staging.descriptor)
        expected_identity = (
            staging.file_stat.st_dev,
            staging.file_stat.st_ino,
            staging.file_stat.st_size,
        )

        with mock.patch(
            "django_images.file_ops.sha256_file_descriptor",
            side_effect=AssertionError("publish must not hash"),
        ), mock.patch(
            "django_images.file_ops.os.fsync",
            side_effect=AssertionError("batch layer owns durability"),
        ), mock.patch.object(
            staging.directory,
            "fsync_publish",
            side_effect=AssertionError("batch layer owns directory fsync"),
        ):
            result = publish_preverified_noreplace(
                staging,
                destination_directory,
                "published.png",
                expected_identity,
            )

        self.assertEqual(result.operation, "published")
        self.assertEqual(
            set(result.mutated_directory_identities),
            {
                (
                    os.fstat(staging_directory.descriptor).st_dev,
                    os.fstat(staging_directory.descriptor).st_ino,
                ),
                (
                    os.fstat(destination_directory.descriptor).st_dev,
                    os.fstat(destination_directory.descriptor).st_ino,
                ),
            },
        )
        self.assertEqual(
            Path(root_path, "originals", "published.png").read_bytes(),
            b"verified",
        )

    def test_preverified_publish_rejects_identity_without_size(self):
        _root, _staging_dir, destination, staging = self.make_fixture()
        expected_identity = (
            staging.file_stat.st_dev,
            staging.file_stat.st_ino,
        )

        with self.assertRaisesRegex(
            MediaPathError, "^unsafe_staging_file$"
        ):
            publish_preverified_noreplace(
                staging,
                destination,
                "published.png",
                expected_identity,
            )

    def test_preverified_publish_rejects_wrong_expected_size(self):
        _root, _staging_dir, destination, staging = self.make_fixture()
        expected_identity = (
            staging.file_stat.st_dev,
            staging.file_stat.st_ino,
            staging.file_stat.st_size + 1,
        )

        with self.assertRaisesRegex(
            MediaPathError, "^unsafe_staging_file$"
        ):
            publish_preverified_noreplace(
                staging,
                destination,
                "published.png",
                expected_identity,
            )

    def test_preverified_replace_performs_no_hash_or_fsync(self):
        root_path, _staging_dir, destination, staging = self.make_fixture()
        expected_identity = (
            staging.file_stat.st_dev,
            staging.file_stat.st_ino,
            staging.file_stat.st_size,
        )

        with mock.patch(
            "django_images.file_ops.sha256_file_descriptor",
            side_effect=AssertionError("replace must not hash"),
        ), mock.patch(
            "django_images.file_ops.os.fsync",
            side_effect=AssertionError("batch layer owns durability"),
        ), mock.patch.object(
            staging.directory,
            "fsync_publish",
            side_effect=AssertionError("batch layer owns directory fsync"),
        ):
            result = replace_preverified_destination(
                staging,
                destination,
                "replaced.png",
                expected_identity,
            )

        self.assertEqual(result.operation, "replaced")
        self.assertEqual(
            Path(root_path, "originals", "replaced.png").read_bytes(),
            b"verified",
        )

    def test_preverified_replace_rejects_named_identity_race(self):
        _root, staging_directory, destination, staging = self.make_fixture()
        expected_identity = (
            staging.file_stat.st_dev,
            staging.file_stat.st_ino,
            staging.file_stat.st_size,
        )
        real_rename = os.rename

        def replace_name_before_rename(*args, **kwargs):
            replacement = create_owned_staging_file(
                staging_directory, "replacement.part"
            )
            try:
                os.write(replacement.descriptor, b"replacement")
                real_rename(
                    replacement.name,
                    staging.name,
                    src_dir_fd=staging_directory.descriptor,
                    dst_dir_fd=staging_directory.descriptor,
                )
            finally:
                replacement.close()
            return real_rename(*args, **kwargs)

        with mock.patch(
            "django_images.file_ops.os.rename",
            side_effect=replace_name_before_rename,
        ), self.assertRaisesRegex(MediaPathError, "^media_path_conflict$"):
            replace_preverified_destination(
                staging,
                destination,
                "replaced.png",
                expected_identity,
            )

    def test_preverified_replace_rejects_size_race(self):
        _root, _staging_dir, destination, staging = self.make_fixture()
        expected_identity = (
            staging.file_stat.st_dev,
            staging.file_stat.st_ino,
            staging.file_stat.st_size,
        )
        real_rename = os.rename

        def grow_before_rename(*args, **kwargs):
            os.ftruncate(staging.descriptor, staging.file_stat.st_size + 1)
            return real_rename(*args, **kwargs)

        with mock.patch(
            "django_images.file_ops.os.rename",
            side_effect=grow_before_rename,
        ), self.assertRaisesRegex(MediaPathError, "^media_path_conflict$"):
            replace_preverified_destination(
                staging,
                destination,
                "replaced.png",
                expected_identity,
            )
