import os
from pathlib import Path
import tempfile
from unittest import mock

from django.test import SimpleTestCase

from django_images.file_ops import (
    create_owned_staging_file,
    open_or_create_media_directory_from,
    open_verified_media_root,
    publish_preverified_noreplace,
)


class PreverifiedPublicationTests(SimpleTestCase):
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
