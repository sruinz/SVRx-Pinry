from io import StringIO
import errno
import os
from pathlib import Path
import tempfile
import threading
import uuid
from unittest import skipUnless

import mock
from django.apps import apps
from django.core.management import CommandError, call_command
from django.db import close_old_connections, connections, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.utils import ConnectionHandler
from django.test import SimpleTestCase, TransactionTestCase

from django_images import file_ops
from django_images.file_ops import (
    MediaPathError,
    media_lifecycle_lock,
    open_media_root,
    remove_empty_media_directory,
    remove_media_file,
)
from django_images.models import Image, PendingMediaDeletion, Thumbnail
from django_images.services.media_deletion import (
    process_pending_media_deletion,
)
from django_images.services import media_deletion as media_deletion_service
from django_images.test_helpers import TemporaryMediaMixin


def media_snapshot(media_root):
    root = Path(media_root)
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
        and path.relative_to(root).parts[0] != ".pinry-locks"
    }


class EmptyMediaDirectoryRemovalTest(SimpleTestCase):
    asset_uuid = "12345678-1234-5678-1234-567812345678"

    def setUp(self):
        self.temporary_media = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_media.cleanup)

    @property
    def relative_directory(self):
        return "originals/{}".format(self.asset_uuid)

    @property
    def leaf(self):
        return Path(
            self.temporary_media.name,
            "originals",
            self.asset_uuid,
        )

    def _remove(self, relative_directory=None):
        root = open_media_root(self.temporary_media.name)
        try:
            return remove_empty_media_directory(
                root,
                relative_directory or self.relative_directory,
            )
        finally:
            root.close()

    def _assert_kind_roots_preserved(self):
        root = Path(self.temporary_media.name)
        self.assertTrue(root.is_dir())
        for name in ("originals", "derivatives"):
            path = root / name
            if path.exists():
                self.assertTrue(path.is_dir())

    def test_remove_empty_media_directory_removes_leaf_and_fsyncs_parent(self):
        self.leaf.mkdir(parents=True)

        removed = self._remove()

        self.assertTrue(removed)
        self.assertFalse(self.leaf.exists())
        self.assertTrue(
            Path(self.temporary_media.name, "originals").is_dir()
        )
        self._assert_kind_roots_preserved()

    def test_remove_empty_media_directory_treats_missing_leaf_as_success(self):
        Path(self.temporary_media.name, "originals").mkdir()

        self.assertTrue(self._remove())

        self._assert_kind_roots_preserved()

    def test_remove_empty_media_directory_preserves_nonempty_leaf(self):
        foreign = self.leaf / "foreign.dat"
        foreign.parent.mkdir(parents=True)
        foreign.write_bytes(b"foreign-bytes")

        self.assertFalse(self._remove())

        self.assertEqual(foreign.read_bytes(), b"foreign-bytes")
        self._assert_kind_roots_preserved()

    def test_remove_empty_media_directory_rejects_one_component_root_target(self):
        for name in ("originals", "derivatives"):
            Path(self.temporary_media.name, name).mkdir()

        for name in ("originals", "derivatives"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(
                    MediaPathError, "unsafe_media_directory"
                ):
                    self._remove(name)

        self._assert_kind_roots_preserved()

    def test_remove_empty_media_directory_rejects_symlinked_parent(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        foreign = Path(outside.name, self.asset_uuid, "foreign.dat")
        foreign.parent.mkdir()
        foreign.write_bytes(b"outside-parent")
        Path(self.temporary_media.name, "originals").symlink_to(
            outside.name, target_is_directory=True
        )

        with self.assertRaises((MediaPathError, OSError)):
            self._remove()

        self.assertEqual(foreign.read_bytes(), b"outside-parent")

    def test_remove_empty_media_directory_rejects_symlinked_leaf(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        foreign = Path(outside.name, "foreign.dat")
        foreign.write_bytes(b"outside-leaf")
        parent = Path(self.temporary_media.name, "originals")
        parent.mkdir()
        self.leaf.symlink_to(outside.name, target_is_directory=True)

        with self.assertRaises((MediaPathError, OSError)):
            self._remove()

        self.assertEqual(foreign.read_bytes(), b"outside-leaf")
        self.assertTrue(self.leaf.is_symlink())

    def test_remove_empty_media_directory_parent_swap_preserves_replacement(self):
        self.leaf.mkdir(parents=True)
        moved_parent = Path(self.temporary_media.name, "originals-old")
        replacement = Path(
            self.temporary_media.name,
            "originals",
            self.asset_uuid,
        )
        foreign = replacement / "foreign.dat"
        real_open = file_ops._open_child_directory_nofollow
        calls = {"count": 0}

        def swap_after_leaf_open(parent_descriptor, name, named_stat):
            descriptor = real_open(parent_descriptor, name, named_stat)
            calls["count"] += 1
            if calls["count"] == 2:
                self.leaf.parent.rename(moved_parent)
                replacement.mkdir(parents=True)
                foreign.write_bytes(b"replacement")
            return descriptor

        with mock.patch(
            "django_images.file_ops._open_child_directory_nofollow",
            side_effect=swap_after_leaf_open,
        ):
            with self.assertRaisesRegex(
                MediaPathError, "unsafe_media_directory"
            ):
                self._remove()

        self.assertEqual(foreign.read_bytes(), b"replacement")
        self.assertTrue(Path(moved_parent, self.asset_uuid).is_dir())

    def test_remove_empty_media_directory_propagates_rmdir_io_failure(self):
        self.leaf.mkdir(parents=True)

        with mock.patch(
            "django_images.file_ops.os.rmdir",
            side_effect=OSError(errno.EIO, "secret path"),
        ):
            with self.assertRaises(OSError) as caught:
                self._remove()

        self.assertEqual(caught.exception.errno, errno.EIO)
        self.assertTrue(self.leaf.is_dir())

    def test_remove_empty_media_directory_propagates_parent_fsync_failure(self):
        self.leaf.mkdir(parents=True)

        with mock.patch(
            "django_images.file_ops.os.fsync",
            side_effect=OSError(errno.EIO, "secret path"),
        ):
            with self.assertRaises(OSError) as caught:
                self._remove()

        self.assertEqual(caught.exception.errno, errno.EIO)
        self.assertFalse(self.leaf.exists())
        self._assert_kind_roots_preserved()

    def test_remove_media_file_fsyncs_exact_uuid_directory(self):
        leaf = self.leaf / "original.png"
        leaf.parent.mkdir(parents=True)
        leaf.write_bytes(b"original")
        expected_identity = (
            leaf.parent.stat().st_dev,
            leaf.parent.stat().st_ino,
        )
        fsynced = []
        real_fsync = file_ops.os.fsync

        def record_fsync(descriptor):
            file_stat = os.fstat(descriptor)
            fsynced.append((file_stat.st_dev, file_stat.st_ino))
            return real_fsync(descriptor)

        root = open_media_root(self.temporary_media.name)
        try:
            with mock.patch(
                "django_images.file_ops.os.fsync",
                side_effect=record_fsync,
            ):
                removed = remove_media_file(
                    root,
                    "{}/original.png".format(self.relative_directory),
                )
        finally:
            root.close()

        self.assertTrue(removed)
        self.assertFalse(leaf.exists())
        self.assertEqual(fsynced, [expected_identity])


class MediaDeletionJournalTest(TemporaryMediaMixin, TransactionTestCase):
    @staticmethod
    def _pending_deletions():
        return apps.get_model(
            "django_images", "PendingMediaDeletion"
        ).objects

    def _write_file(self, name, contents):
        path = Path(self.temporary_media.name, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)

    def _create_four_file_image(self, label):
        image = Image.objects.create(
            image="originals/{}/original.png".format(label),
            original_filename="{}.png".format(label),
            width=32,
            height=32,
        )
        names = [image.image.name]
        for size in ("thumbnail", "standard", "square"):
            name = "derivatives/{}/{}.png".format(label, size)
            Thumbnail.objects.create(
                original=image,
                image=name,
                size=size,
                width=32,
                height=32,
            )
            names.append(name)
        for index, name in enumerate(names):
            self._write_file(name, "file-{}".format(index).encode("ascii"))
        return image, media_snapshot(self.temporary_media.name)

    def _canonical_name(self, asset_uuid=None, kind="original"):
        asset_uuid = asset_uuid or uuid.uuid4()
        root = "originals" if kind == "original" else "derivatives"
        leaf = "original.png" if kind == "original" else "thumbnail.png"
        return asset_uuid, "{}/{}/{}".format(root, asset_uuid, leaf)

    def test_canonical_pending_deletion_removes_empty_uuid_parent(self):
        asset_uuid, name = self._canonical_name()
        self._write_file(name, b"pending")
        pending = self._pending_deletions().create(
            kind="original", name=name
        )

        processed = process_pending_media_deletion(pending.pk)

        self.assertTrue(processed)
        self.assertFalse(Path(self.temporary_media.name, name).exists())
        self.assertFalse(
            Path(self.temporary_media.name, "originals", str(asset_uuid))
            .exists()
        )
        self.assertTrue(
            Path(self.temporary_media.name, "originals").is_dir()
        )
        self.assertFalse(self._pending_deletions().exists())

    def test_canonical_missing_file_retry_removes_empty_uuid_parent(self):
        asset_uuid, name = self._canonical_name()
        directory = Path(
            self.temporary_media.name, "originals", str(asset_uuid)
        )
        directory.mkdir(parents=True)
        pending = self._pending_deletions().create(
            kind="original", name=name
        )

        processed = process_pending_media_deletion(pending.pk)

        self.assertTrue(processed)
        self.assertFalse(directory.exists())
        self.assertFalse(self._pending_deletions().exists())

    def test_canonical_original_reused_by_live_image_is_preserved(self):
        asset_uuid, name = self._canonical_name()
        self._write_file(name, b"reused-original")
        pending = self._pending_deletions().create(
            kind="original", name=name
        )
        Image.objects.create(
            image=name,
            asset_uuid=asset_uuid,
            original_filename="live.png",
            width=32,
            height=32,
        )

        processed = process_pending_media_deletion(pending.pk)

        self.assertTrue(processed)
        self.assertEqual(
            Path(self.temporary_media.name, name).read_bytes(),
            b"reused-original",
        )
        self.assertFalse(self._pending_deletions().exists())

    def test_canonical_derivative_reused_by_live_thumbnail_is_preserved(self):
        asset_uuid, name = self._canonical_name(kind="thumbnail")
        self._write_file(name, b"reused-thumbnail")
        image = Image.objects.create(
            image="legacy/live.png",
            asset_uuid=asset_uuid,
            original_filename="live.png",
            width=32,
            height=32,
        )
        Thumbnail.objects.create(
            original=image,
            image=name,
            size="thumbnail",
            width=32,
            height=32,
        )
        pending = self._pending_deletions().create(
            kind="thumbnail", name=name
        )

        processed = process_pending_media_deletion(pending.pk)

        self.assertTrue(processed)
        self.assertEqual(
            Path(self.temporary_media.name, name).read_bytes(),
            b"reused-thumbnail",
        )
        self.assertFalse(self._pending_deletions().exists())

    def test_canonical_delete_preserves_directory_owned_by_same_uuid(self):
        asset_uuid, name = self._canonical_name()
        self._write_file(name, b"stale")
        Image.objects.create(
            image="legacy/current.png",
            asset_uuid=asset_uuid,
            original_filename="current.png",
            width=32,
            height=32,
        )
        pending = self._pending_deletions().create(
            kind="original", name=name
        )
        directory = Path(
            self.temporary_media.name, "originals", str(asset_uuid)
        )

        processed = process_pending_media_deletion(pending.pk)

        self.assertTrue(processed)
        self.assertFalse(Path(self.temporary_media.name, name).exists())
        self.assertTrue(directory.is_dir())
        self.assertFalse(self._pending_deletions().exists())

    def test_canonical_delete_defers_while_shared_lifecycle_lock_is_held(self):
        _asset_uuid, name = self._canonical_name()
        self._write_file(name, b"locked")
        pending = self._pending_deletions().create(
            kind="original", name=name
        )
        root = open_media_root(self.temporary_media.name)
        try:
            with media_lifecycle_lock(root):
                processed = process_pending_media_deletion(pending.pk)
        finally:
            root.close()

        self.assertFalse(processed)
        self.assertEqual(
            Path(self.temporary_media.name, name).read_bytes(), b"locked"
        )
        pending.refresh_from_db()
        self.assertEqual(pending.attempts, 1)
        self.assertEqual(pending.last_error, "MediaLifecycleLockError")

    def test_canonical_delete_preserves_foreign_entry_and_resolves_journal(self):
        asset_uuid, name = self._canonical_name()
        self._write_file(name, b"pending")
        foreign = Path(
            self.temporary_media.name,
            "originals",
            str(asset_uuid),
            "foreign.dat",
        )
        foreign.write_bytes(b"foreign")
        pending = self._pending_deletions().create(
            kind="original", name=name
        )

        processed = process_pending_media_deletion(pending.pk)

        self.assertTrue(processed)
        self.assertEqual(foreign.read_bytes(), b"foreign")
        self.assertFalse(Path(self.temporary_media.name, name).exists())
        self.assertFalse(self._pending_deletions().exists())

    def test_canonical_original_symlinked_parent_preserves_outside_file(self):
        asset_uuid, name = self._canonical_name()
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        outside_file = Path(outside.name, str(asset_uuid), "original.png")
        outside_file.parent.mkdir()
        outside_file.write_bytes(b"outside-original")
        parent = Path(self.temporary_media.name, "originals")
        parent.symlink_to(outside.name, target_is_directory=True)
        pending = self._pending_deletions().create(
            kind="original", name=name
        )

        processed = process_pending_media_deletion(pending.pk)

        self.assertFalse(processed)
        self.assertEqual(outside_file.read_bytes(), b"outside-original")
        self.assertTrue(parent.is_symlink())
        pending.refresh_from_db()
        self.assertEqual(pending.attempts, 1)
        self.assertTrue(pending.last_error)

    def test_canonical_derivative_symlinked_parent_preserves_outside_file(self):
        asset_uuid, name = self._canonical_name(kind="thumbnail")
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        outside_file = Path(outside.name, str(asset_uuid), "thumbnail.png")
        outside_file.parent.mkdir()
        outside_file.write_bytes(b"outside-thumbnail")
        parent = Path(self.temporary_media.name, "derivatives")
        parent.symlink_to(outside.name, target_is_directory=True)
        pending = self._pending_deletions().create(
            kind="thumbnail", name=name
        )

        processed = process_pending_media_deletion(pending.pk)

        self.assertFalse(processed)
        self.assertEqual(outside_file.read_bytes(), b"outside-thumbnail")
        self.assertTrue(parent.is_symlink())
        pending.refresh_from_db()
        self.assertEqual(pending.attempts, 1)
        self.assertTrue(pending.last_error)

    def test_canonical_symlinked_leaf_is_preserved_with_journal(self):
        asset_uuid, name = self._canonical_name()
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        outside_file = Path(outside.name, "outside.png")
        outside_file.write_bytes(b"outside-leaf")
        leaf = Path(self.temporary_media.name, name)
        leaf.parent.mkdir(parents=True)
        leaf.symlink_to(outside_file)
        pending = self._pending_deletions().create(
            kind="original", name=name
        )

        processed = process_pending_media_deletion(pending.pk)

        self.assertFalse(processed)
        self.assertEqual(outside_file.read_bytes(), b"outside-leaf")
        self.assertTrue(leaf.is_symlink())
        pending.refresh_from_db()
        self.assertEqual(pending.last_error, "MediaPathError")

    def test_canonical_leaf_name_swap_preserves_original_and_replacement(self):
        _asset_uuid, name = self._canonical_name()
        leaf = Path(self.temporary_media.name, name)
        moved = leaf.with_name("original-old.png")
        self._write_file(name, b"original")
        pending = self._pending_deletions().create(
            kind="original", name=name
        )
        real_open = file_ops._open_regular_nofollow

        def swap_after_open(directory_descriptor, leaf_name):
            descriptor = real_open(directory_descriptor, leaf_name)
            leaf.rename(moved)
            leaf.write_bytes(b"replacement")
            return descriptor

        with mock.patch(
            "django_images.file_ops._open_regular_nofollow",
            side_effect=swap_after_open,
        ):
            processed = process_pending_media_deletion(pending.pk)

        self.assertFalse(processed)
        self.assertEqual(moved.read_bytes(), b"original")
        self.assertEqual(leaf.read_bytes(), b"replacement")
        pending.refresh_from_db()
        self.assertEqual(pending.last_error, "MediaPathError")

    def test_canonical_parent_swap_preserves_original_and_replacement(self):
        asset_uuid, name = self._canonical_name()
        kind_root = Path(self.temporary_media.name, "originals")
        moved_root = Path(self.temporary_media.name, "originals-old")
        replacement = kind_root / str(asset_uuid) / "original.png"
        self._write_file(name, b"original")
        pending = self._pending_deletions().create(
            kind="original", name=name
        )
        real_open = file_ops._open_child_directory_nofollow
        calls = {"count": 0}

        def swap_after_asset_open(parent_descriptor, child_name, named_stat):
            descriptor = real_open(
                parent_descriptor, child_name, named_stat
            )
            calls["count"] += 1
            if calls["count"] == 2:
                kind_root.rename(moved_root)
                replacement.parent.mkdir(parents=True)
                replacement.write_bytes(b"replacement")
            return descriptor

        with mock.patch(
            "django_images.file_ops._open_child_directory_nofollow",
            side_effect=swap_after_asset_open,
        ):
            processed = process_pending_media_deletion(pending.pk)

        self.assertFalse(processed)
        self.assertEqual(
            Path(moved_root, str(asset_uuid), "original.png").read_bytes(),
            b"original",
        )
        self.assertEqual(replacement.read_bytes(), b"replacement")
        pending.refresh_from_db()
        self.assertEqual(pending.last_error, "MediaPathError")

    def test_canonical_prune_failure_keeps_journal_until_retry(self):
        asset_uuid, name = self._canonical_name()
        self._write_file(name, b"pending")
        pending = self._pending_deletions().create(
            kind="original", name=name
        )

        with mock.patch(
            "django_images.services.media_deletion."
            "remove_empty_media_directory",
            side_effect=OSError(errno.EIO, "secret directory"),
            create=True,
        ):
            first = process_pending_media_deletion(pending.pk)

        self.assertFalse(first)
        pending.refresh_from_db()
        self.assertEqual(pending.attempts, 1)
        self.assertEqual(pending.last_error, "OSError")
        self.assertTrue(
            Path(self.temporary_media.name, "originals", str(asset_uuid))
            .is_dir()
        )

        self.assertTrue(process_pending_media_deletion(pending.pk))
        self.assertFalse(self._pending_deletions().exists())

    def test_canonical_parent_fsync_failure_keeps_journal_until_retry(self):
        asset_uuid, name = self._canonical_name()
        self._write_file(name, b"pending")
        pending = self._pending_deletions().create(
            kind="original", name=name
        )

        with mock.patch(
            "django_images.file_ops.os.fsync",
            side_effect=OSError(errno.EIO, "secret directory"),
        ):
            first = process_pending_media_deletion(pending.pk)

        self.assertFalse(first)
        pending.refresh_from_db()
        self.assertEqual(pending.attempts, 1)
        self.assertEqual(pending.last_error, "OSError")
        self.assertFalse(Path(self.temporary_media.name, name).exists())
        self.assertTrue(
            Path(self.temporary_media.name, "originals", str(asset_uuid))
            .is_dir()
        )

        self.assertTrue(process_pending_media_deletion(pending.pk))
        self.assertFalse(self._pending_deletions().exists())

    def test_noncanonical_pending_deletion_never_prunes_parent(self):
        name = "legacy/asset/original.png"
        self._write_file(name, b"legacy")
        pending = self._pending_deletions().create(
            kind="original", name=name
        )
        parent = Path(self.temporary_media.name, "legacy", "asset")

        self.assertTrue(process_pending_media_deletion(pending.pk))

        self.assertTrue(parent.is_dir())
        self.assertFalse(self._pending_deletions().exists())

    def test_kind_root_mismatch_never_prunes_parent(self):
        asset_uuid, name = self._canonical_name(kind="thumbnail")
        self._write_file(name, b"mismatch")
        pending = self._pending_deletions().create(
            kind="original", name=name
        )
        parent = Path(
            self.temporary_media.name, "derivatives", str(asset_uuid)
        )

        self.assertTrue(process_pending_media_deletion(pending.pk))

        self.assertTrue(parent.is_dir())
        self.assertFalse(self._pending_deletions().exists())

    def test_remote_pending_kind_mismatch_uses_remote_storage_only(self):
        asset_uuid, name = self._canonical_name(kind="thumbnail")
        local_file = Path(self.temporary_media.name, name)
        self._write_file(name, b"local-foreign")
        pending = self._pending_deletions().create(
            kind="original", name=name
        )
        remote_storage = mock.Mock()
        remote_storage.exists.return_value = True
        image_field = Image._meta.get_field("image")

        with mock.patch.object(image_field, "storage", remote_storage):
            processed = process_pending_media_deletion(pending.pk)

        self.assertTrue(processed)
        self.assertEqual(local_file.read_bytes(), b"local-foreign")
        self.assertTrue(
            Path(
                self.temporary_media.name,
                "derivatives",
                str(asset_uuid),
            ).is_dir()
        )
        remote_storage.exists.assert_called_once_with(name)
        remote_storage.delete.assert_called_once_with(name)
        self.assertFalse(self._pending_deletions().exists())

    def test_local_pending_kind_mismatch_rejects_symlinked_parent(self):
        asset_uuid, name = self._canonical_name(kind="thumbnail")
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        outside_file = Path(
            outside.name, str(asset_uuid), "thumbnail.png"
        )
        outside_file.parent.mkdir()
        outside_file.write_bytes(b"outside-thumbnail")
        parent = Path(self.temporary_media.name, "derivatives")
        parent.symlink_to(outside.name, target_is_directory=True)
        pending = self._pending_deletions().create(
            kind="original", name=name
        )
        remote_storage = mock.Mock()
        thumbnail_field = Thumbnail._meta.get_field("image")

        with mock.patch.object(
            thumbnail_field, "storage", remote_storage
        ):
            processed = process_pending_media_deletion(pending.pk)

        self.assertFalse(processed)
        self.assertEqual(outside_file.read_bytes(), b"outside-thumbnail")
        self.assertTrue(parent.is_symlink())
        remote_storage.exists.assert_not_called()
        remote_storage.delete.assert_not_called()
        pending.refresh_from_db()
        self.assertEqual(pending.attempts, 1)
        self.assertTrue(pending.last_error)

    def test_original_kind_mismatch_preserves_live_thumbnail_exact_path(self):
        asset_uuid, name = self._canonical_name(kind="thumbnail")
        self._write_file(name, b"live-thumbnail")
        image = Image.objects.create(
            image="legacy/live.png",
            asset_uuid=asset_uuid,
            original_filename="live.png",
            width=32,
            height=32,
        )
        thumbnail = Thumbnail.objects.create(
            original=image,
            image=name,
            size="thumbnail",
            width=32,
            height=32,
        )
        pending = self._pending_deletions().create(
            kind="original", name=name
        )

        processed = process_pending_media_deletion(pending.pk)

        self.assertTrue(processed)
        self.assertTrue(Thumbnail.objects.filter(pk=thumbnail.pk).exists())
        self.assertEqual(
            Path(self.temporary_media.name, name).read_bytes(),
            b"live-thumbnail",
        )
        self.assertFalse(self._pending_deletions().exists())

    def test_thumbnail_kind_mismatch_preserves_live_image_exact_path(self):
        asset_uuid, name = self._canonical_name()
        self._write_file(name, b"live-original")
        image = Image.objects.create(
            image=name,
            asset_uuid=asset_uuid,
            original_filename="live.png",
            width=32,
            height=32,
        )
        pending = self._pending_deletions().create(
            kind="thumbnail", name=name
        )

        processed = process_pending_media_deletion(pending.pk)

        self.assertTrue(processed)
        self.assertTrue(Image.objects.filter(pk=image.pk).exists())
        self.assertEqual(
            Path(self.temporary_media.name, name).read_bytes(),
            b"live-original",
        )
        self.assertFalse(self._pending_deletions().exists())

    def test_mismatch_rechecks_reference_after_shared_publish(self):
        asset_uuid, name = self._canonical_name(kind="thumbnail")
        pending = self._pending_deletions().create(
            kind="original", name=name
        )
        foreign = Path(self.temporary_media.name, "foreign.dat")
        foreign.write_bytes(b"foreign")
        initial_checked = threading.Event()
        publish_done = threading.Event()
        finished = threading.Event()
        result = {}
        errors = []
        real_is_referenced = media_deletion_service._media_path_is_referenced

        def pause_after_initial_check(media_name, using):
            referenced = real_is_referenced(media_name, using)
            if not initial_checked.is_set():
                initial_checked.set()
                if not publish_done.wait(2):
                    raise AssertionError("shared publish did not finish")
            return referenced

        def process_mismatch():
            close_old_connections()
            try:
                result["processed"] = process_pending_media_deletion(
                    pending.pk
                )
            except BaseException as error:
                errors.append(error)
            finally:
                close_old_connections()
                finished.set()

        with mock.patch(
            "django_images.services.media_deletion."
            "_media_path_is_referenced",
            side_effect=pause_after_initial_check,
        ):
            worker = threading.Thread(target=process_mismatch)
            worker.start()
            self.assertTrue(initial_checked.wait(1))
            root = open_media_root(self.temporary_media.name)
            try:
                with media_lifecycle_lock(root):
                    self._write_file(name, b"published-thumbnail")
                    image = Image.objects.create(
                        image="legacy/published.png",
                        asset_uuid=asset_uuid,
                        original_filename="published.png",
                        width=32,
                        height=32,
                    )
                    thumbnail = Thumbnail.objects.create(
                        original=image,
                        image=name,
                        size="thumbnail",
                        width=32,
                        height=32,
                    )
            finally:
                root.close()
                publish_done.set()
            worker.join(2)

        self.assertTrue(finished.is_set())
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(result["processed"])
        self.assertTrue(Thumbnail.objects.filter(pk=thumbnail.pk).exists())
        self.assertEqual(
            Path(self.temporary_media.name, name).read_bytes(),
            b"published-thumbnail",
        )
        self.assertEqual(foreign.read_bytes(), b"foreign")
        self.assertFalse(self._pending_deletions().exists())

    @skipUnless(
        hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"),
        "FIFO and nonblocking open support required",
    )
    def test_canonical_fifo_leaf_fails_without_blocking_worker(self):
        _asset_uuid, name = self._canonical_name()
        leaf = Path(self.temporary_media.name, name)
        leaf.parent.mkdir(parents=True)
        os.mkfifo(str(leaf))
        pending = self._pending_deletions().create(
            kind="original", name=name
        )
        started = threading.Event()
        finished = threading.Event()
        result = {}
        errors = []

        def process_fifo():
            close_old_connections()
            try:
                started.set()
                result["processed"] = process_pending_media_deletion(
                    pending.pk
                )
            except BaseException as error:
                errors.append(error)
            finally:
                close_old_connections()
                finished.set()

        worker = threading.Thread(target=process_fifo)
        worker.daemon = True
        worker.start()
        self.assertTrue(started.wait(1))
        completed_without_unblock = finished.wait(1)
        unblock_descriptor = None
        try:
            if not completed_without_unblock:
                unblock_descriptor = os.open(
                    str(leaf), os.O_RDWR | os.O_NONBLOCK
                )
            worker.join(2)
        finally:
            if unblock_descriptor is not None:
                os.close(unblock_descriptor)

        self.assertTrue(completed_without_unblock)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertFalse(result["processed"])
        self.assertTrue(leaf.is_fifo())
        pending.refresh_from_db()
        self.assertEqual(pending.attempts, 1)
        self.assertEqual(pending.last_error, "MediaPathError")

    @skipUnless(
        hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"),
        "FIFO and nonblocking open support required",
    )
    def test_regular_leaf_swapped_to_fifo_before_open_does_not_block(self):
        _asset_uuid, name = self._canonical_name()
        leaf = Path(self.temporary_media.name, name)
        moved = leaf.with_name("original-old.png")
        self._write_file(name, b"original")
        pending = self._pending_deletions().create(
            kind="original", name=name
        )
        swapped = threading.Event()
        finished = threading.Event()
        result = {}
        errors = []
        real_open = file_ops._open_regular_nofollow

        def swap_before_open(directory_descriptor, leaf_name):
            leaf.rename(moved)
            os.mkfifo(str(leaf))
            swapped.set()
            return real_open(directory_descriptor, leaf_name)

        def process_fifo_swap():
            close_old_connections()
            try:
                result["processed"] = process_pending_media_deletion(
                    pending.pk
                )
            except BaseException as error:
                errors.append(error)
            finally:
                close_old_connections()
                finished.set()

        unblock_descriptor = None
        with mock.patch(
            "django_images.file_ops._open_regular_nofollow",
            side_effect=swap_before_open,
        ):
            worker = threading.Thread(target=process_fifo_swap)
            worker.daemon = True
            worker.start()
            self.assertTrue(swapped.wait(1))
            completed_without_unblock = finished.wait(1)
            try:
                if not completed_without_unblock:
                    unblock_descriptor = os.open(
                        str(leaf), os.O_RDWR | os.O_NONBLOCK
                    )
                worker.join(2)
            finally:
                if unblock_descriptor is not None:
                    os.close(unblock_descriptor)

        self.assertTrue(completed_without_unblock)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertFalse(result["processed"])
        self.assertEqual(moved.read_bytes(), b"original")
        self.assertTrue(leaf.is_fifo())
        pending.refresh_from_db()
        self.assertEqual(pending.attempts, 1)
        self.assertEqual(pending.last_error, "MediaPathError")

    def test_non_filesystem_storage_skips_local_directory_cleanup(self):
        asset_uuid, name = self._canonical_name()
        local_directory = Path(
            self.temporary_media.name, "originals", str(asset_uuid)
        )
        local_directory.mkdir(parents=True)
        storage = mock.Mock()
        storage.exists.return_value = True
        pending = self._pending_deletions().create(
            kind="original", name=name
        )
        image_field = Image._meta.get_field("image")

        with mock.patch.object(image_field, "storage", storage):
            processed = process_pending_media_deletion(pending.pk)

        self.assertTrue(processed)
        self.assertTrue(local_directory.is_dir())
        storage.delete.assert_called_once_with(name)
        self.assertFalse(self._pending_deletions().exists())

    def test_one_storage_failure_does_not_stop_independent_journals(self):
        first_name = "legacy/first.png"
        second_name = "legacy/second.png"
        first = self._pending_deletions().create(
            kind="original", name=first_name
        )
        self._pending_deletions().create(
            kind="original", name=second_name
        )
        storage = mock.Mock()
        storage.exists.return_value = True

        def delete(name):
            if name == first_name:
                raise OSError("secret first path")

        storage.delete.side_effect = delete
        image_field = Image._meta.get_field("image")

        with mock.patch.object(image_field, "storage", storage):
            call_command(
                "retry_media_deletions", execute=True, stdout=StringIO()
            )

        first.refresh_from_db()
        self.assertEqual(first.attempts, 1)
        self.assertEqual(first.last_error, "OSError")
        self.assertEqual(
            list(self._pending_deletions().values_list("name", flat=True)),
            [first_name],
        )

    def test_pending_query_failure_does_not_escape(self):
        with mock.patch.object(
            PendingMediaDeletion.objects,
            "using",
            side_effect=RuntimeError("secret query"),
        ):
            with self.assertLogs(
                "django_images.services.media_deletion", level="WARNING"
            ) as captured:
                processed = process_pending_media_deletion(123)

        self.assertFalse(processed)
        self.assertEqual(captured.records[0].getMessage(), "media_deletion_failed")
        self.assertEqual(captured.records[0].media_error, "RuntimeError")
        self.assertNotIn("secret query", "\n".join(captured.output))

    def test_pending_query_and_logger_failures_do_not_escape(self):
        with mock.patch.object(
            PendingMediaDeletion.objects,
            "using",
            side_effect=RuntimeError("secret query"),
        ), mock.patch(
            "django_images.services.media_deletion.logger.warning",
            side_effect=RuntimeError("secret logger"),
        ):
            processed = process_pending_media_deletion(123)

        self.assertFalse(processed)

    def test_failure_recording_error_does_not_escape(self):
        pending = self._pending_deletions().create(
            kind="original", name="legacy/failure.png"
        )
        storage = mock.Mock()
        storage.exists.side_effect = OSError("secret storage")
        image_field = Image._meta.get_field("image")

        with mock.patch.object(image_field, "storage", storage), mock.patch(
            "django.db.models.query.QuerySet.update",
            side_effect=RuntimeError("secret update"),
        ):
            processed = process_pending_media_deletion(pending.pk)

        self.assertFalse(processed)
        self.assertTrue(
            self._pending_deletions().filter(pk=pending.pk).exists()
        )

    def test_processing_update_and_logger_failures_do_not_escape(self):
        pending = self._pending_deletions().create(
            kind="original", name="legacy/failure.png"
        )
        storage = mock.Mock()
        storage.exists.side_effect = OSError("secret storage")
        image_field = Image._meta.get_field("image")

        with mock.patch.object(image_field, "storage", storage), mock.patch(
            "django.db.models.query.QuerySet.update",
            side_effect=RuntimeError("secret update"),
        ), mock.patch(
            "django_images.services.media_deletion.logger.warning",
            side_effect=RuntimeError("secret logger"),
        ):
            processed = process_pending_media_deletion(pending.pk)

        self.assertFalse(processed)
        self.assertTrue(
            self._pending_deletions().filter(pk=pending.pk).exists()
        )

    def test_pending_delete_error_does_not_escape(self):
        pending = self._pending_deletions().create(
            kind="original", name="legacy/deleted.png"
        )

        with mock.patch.object(
            PendingMediaDeletion,
            "delete",
            side_effect=RuntimeError("secret delete"),
        ):
            processed = process_pending_media_deletion(pending.pk)

        self.assertFalse(processed)
        self.assertTrue(
            self._pending_deletions().filter(pk=pending.pk).exists()
        )

    def test_pending_delete_and_logger_failures_do_not_escape(self):
        pending = self._pending_deletions().create(
            kind="original", name="legacy/deleted.png"
        )

        with mock.patch.object(
            PendingMediaDeletion,
            "delete",
            side_effect=RuntimeError("secret delete"),
        ), mock.patch(
            "django_images.services.media_deletion.logger.warning",
            side_effect=RuntimeError("secret logger"),
        ):
            processed = process_pending_media_deletion(pending.pk)

        self.assertFalse(processed)
        pending.refresh_from_db()
        self.assertEqual(pending.attempts, 1)
        self.assertEqual(pending.last_error, "RuntimeError")

    def test_rolled_back_image_delete_preserves_files_without_journal(self):
        image, files_before = self._create_four_file_image("rollback")
        image_id = image.pk

        with self.assertRaisesRegex(RuntimeError, "rollback image delete"):
            with transaction.atomic():
                image.delete()
                raise RuntimeError("rollback image delete")

        restored = Image.objects.get(pk=image_id)
        self.assertEqual(restored.thumbnail_set.count(), 3)
        self.assertEqual(
            media_snapshot(self.temporary_media.name), files_before
        )
        self.assertFalse(self._pending_deletions().exists())

    def test_committed_journal_survives_when_callbacks_do_not_run(self):
        image, files_before = self._create_four_file_image("process-stop")
        image_id = image.pk
        callbacks = []

        def stop_before_callback(callback, using=None):
            callbacks.append((callback, using))

        with mock.patch(
            "django.db.transaction.on_commit",
            side_effect=stop_before_callback,
        ):
            image.delete()

        self.assertFalse(Image.objects.filter(pk=image_id).exists())
        self.assertFalse(
            Thumbnail.objects.filter(original_id=image_id).exists()
        )
        self.assertEqual(len(callbacks), 4)
        self.assertEqual({using for _, using in callbacks}, {"default"})
        self.assertEqual(self._pending_deletions().count(), 4)
        self.assertEqual(
            media_snapshot(self.temporary_media.name), files_before
        )

        call_command(
            "retry_media_deletions", execute=True, stdout=StringIO()
        )

        self.assertFalse(self._pending_deletions().exists())
        self.assertEqual(media_snapshot(self.temporary_media.name), {})

    def test_duplicate_callbacks_share_one_journal_row(self):
        name = "legacy/shared/original.png"
        self._write_file(name, b"shared")
        images = [
            Image.objects.create(
                image=name,
                original_filename="original.png",
                width=32,
                height=32,
            )
            for _ in range(2)
        ]
        callbacks = []

        def stop_before_callback(callback, using=None):
            callbacks.append((callback, using))

        with mock.patch(
            "django.db.transaction.on_commit",
            side_effect=stop_before_callback,
        ):
            Image.objects.filter(
                pk__in=[image.pk for image in images]
            ).delete()

        self.assertEqual(len(callbacks), 2)
        self.assertEqual(
            self._pending_deletions().filter(
                kind="original", name=name
            ).count(),
            1,
        )
        self.assertEqual(
            media_snapshot(self.temporary_media.name), {name: b"shared"}
        )

        call_command(
            "retry_media_deletions", execute=True, stdout=StringIO()
        )

        self.assertFalse(self._pending_deletions().exists())
        self.assertEqual(media_snapshot(self.temporary_media.name), {})

    @mock.patch("django_images.models.IMAGE_AUTO_DELETE", False)
    def test_auto_delete_disabled_creates_no_journal_and_deletes_no_file(self):
        image, files_before = self._create_four_file_image("disabled")

        image.delete()

        self.assertEqual(
            media_snapshot(self.temporary_media.name), files_before
        )
        self.assertFalse(self._pending_deletions().exists())

    def test_retry_removes_journal_when_file_is_already_missing(self):
        pending = self._pending_deletions().create(
            kind="original",
            name="originals/missing/original.png",
        )

        first = StringIO()
        call_command(
            "retry_media_deletions", execute=True, stdout=first
        )
        second = StringIO()
        call_command(
            "retry_media_deletions", execute=True, stdout=second
        )

        self.assertFalse(
            self._pending_deletions().filter(pk=pending.pk).exists()
        )
        self.assertIn("pending=1 processed=1 remaining=0", first.getvalue())
        self.assertIn("pending=0 processed=0 remaining=0", second.getvalue())

    def test_retry_uses_exact_canonical_and_legacy_names_only(self):
        canonical = (
            "originals/12345678-1234-5678-1234-567812345678/original.png"
        )
        legacy = "image/thumbnail/by-md5/a/b/hash/thumbnail.png"
        unrelated = "originals/unrelated/original.png"
        for name in (canonical, legacy, unrelated):
            self._write_file(name, name.encode("ascii"))
        self._pending_deletions().create(
            kind="original", name=canonical
        )
        self._pending_deletions().create(
            kind="thumbnail", name=legacy
        )

        call_command(
            "retry_media_deletions", execute=True, stdout=StringIO()
        )

        self.assertFalse(self._pending_deletions().exists())
        self.assertEqual(
            media_snapshot(self.temporary_media.name),
            {unrelated: unrelated.encode("ascii")},
        )

    def test_retry_limit_processes_only_requested_rows(self):
        names = ["originals/limit/{}/original.png".format(index)
                 for index in range(3)]
        for name in names:
            self._write_file(name, b"pending")
            self._pending_deletions().create(kind="original", name=name)

        call_command(
            "retry_media_deletions",
            execute=True,
            limit=2,
            stdout=StringIO(),
        )

        self.assertEqual(self._pending_deletions().count(), 1)
        self.assertEqual(len(media_snapshot(self.temporary_media.name)), 1)

    def test_retry_rejects_non_positive_limit_without_changes(self):
        pending = self._pending_deletions().create(
            kind="original", name="originals/limit/original.png"
        )

        with self.assertRaisesRegex(CommandError, "invalid_limit"):
            call_command(
                "retry_media_deletions", execute=True, limit=0
            )

        self.assertTrue(
            self._pending_deletions().filter(pk=pending.pk).exists()
        )

    def test_retry_rejects_unknown_database_alias(self):
        with self.assertRaisesRegex(
            CommandError, "unknown_database_alias: missing"
        ):
            call_command("retry_media_deletions", database="missing")

    def test_invalid_storage_names_never_reach_storage(self):
        invalid_names = (
            "../outside",
            "/absolute",
            "",
            "safe/../target",
            "safe\\target",
            "safe//target",
            "./target",
            "safe/\x00target",
            "safe/\ntarget",
        )
        storage = mock.Mock()
        storage.exists.side_effect = AssertionError(
            "storage must not be called"
        )
        image_field = Image._meta.get_field("image")

        with mock.patch.object(image_field, "storage", storage):
            for name in invalid_names:
                with self.subTest(name=repr(name)):
                    pending = self._pending_deletions().create(
                        kind="original", name=name
                    )

                    processed = process_pending_media_deletion(pending.pk)

                    self.assertFalse(processed)
                    pending.refresh_from_db()
                    self.assertEqual(pending.attempts, 1)
                    self.assertEqual(
                        pending.last_error, "InvalidMediaName"
                    )

        storage.exists.assert_not_called()
        storage.delete.assert_not_called()

    def test_invalid_dot_segment_cannot_delete_normalized_filesystem_target(
        self,
    ):
        target = "target.dat"
        self._write_file(target, b"unrelated-target")
        pending = self._pending_deletions().create(
            kind="original", name="safe/../target.dat"
        )
        files_before = media_snapshot(self.temporary_media.name)
        stdout = StringIO()

        call_command(
            "retry_media_deletions", execute=True, stdout=stdout
        )

        pending.refresh_from_db()
        self.assertEqual(pending.attempts, 1)
        self.assertEqual(pending.last_error, "InvalidMediaName")
        self.assertEqual(
            media_snapshot(self.temporary_media.name), files_before
        )
        self.assertIn(
            "pending=1 processed=0 remaining=1", stdout.getvalue()
        )


class MediaDeletionDatabaseAliasTest(
    TemporaryMediaMixin, TransactionTestCase
):
    databases = {"default"}

    @classmethod
    def setUpClass(cls):
        cls.other_database = tempfile.TemporaryDirectory()
        other_settings = {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": str(Path(cls.other_database.name, "other.sqlite3")),
        }
        connections.databases["other"] = ConnectionHandler(
            {"default": other_settings}
        )["default"].settings_dict
        cls.databases = {"default", "other"}
        super(MediaDeletionDatabaseAliasTest, cls).setUpClass()
        other = connections["other"]
        executor = MigrationExecutor(other)
        executor.migrate(executor.loader.graph.leaf_nodes())

    @classmethod
    def tearDownClass(cls):
        try:
            super(MediaDeletionDatabaseAliasTest, cls).tearDownClass()
        finally:
            connections["other"].close()
            del connections["other"]
            del connections.databases["other"]
            cls.other_database.cleanup()
            cls.databases = {"default"}

    @staticmethod
    def _pending_deletions(alias):
        return apps.get_model(
            "django_images", "PendingMediaDeletion"
        ).objects.using(alias)

    def _write_file(self, name, contents):
        path = Path(self.temporary_media.name, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)

    def _create_pending(self, alias, label):
        name = "originals/{}/original.png".format(label)
        contents = "{}-bytes".format(label).encode("ascii")
        self._write_file(name, contents)
        pending = self._pending_deletions(alias).create(
            kind="original", name=name
        )
        return pending, contents

    def test_dry_run_reports_only_selected_database(self):
        default, default_bytes = self._create_pending(
            "default", "default-dry-run"
        )
        other, other_bytes = self._create_pending(
            "other", "other-dry-run"
        )
        stdout = StringIO()

        call_command(
            "retry_media_deletions", database="other", stdout=stdout
        )

        output = stdout.getvalue()
        self.assertIn(other.name, output)
        self.assertNotIn(default.name, output)
        self.assertIn("pending=1 processed=0 remaining=1", output)
        self.assertTrue(
            self._pending_deletions("default")
            .filter(pk=default.pk)
            .exists()
        )
        self.assertTrue(
            self._pending_deletions("other")
            .filter(pk=other.pk)
            .exists()
        )
        self.assertEqual(
            media_snapshot(self.temporary_media.name),
            {
                default.name: default_bytes,
                other.name: other_bytes,
            },
        )

    def test_execute_changes_only_selected_database(self):
        default, default_bytes = self._create_pending(
            "default", "default-execute"
        )
        other, _ = self._create_pending("other", "other-execute")
        stdout = StringIO()

        call_command(
            "retry_media_deletions",
            database="other",
            execute=True,
            stdout=stdout,
        )

        self.assertTrue(
            self._pending_deletions("default")
            .filter(pk=default.pk)
            .exists()
        )
        self.assertFalse(
            self._pending_deletions("other")
            .filter(pk=other.pk)
            .exists()
        )
        self.assertEqual(
            media_snapshot(self.temporary_media.name),
            {default.name: default_bytes},
        )
        self.assertIn(
            "pending=1 processed=1 remaining=0", stdout.getvalue()
        )

    def test_limit_and_remaining_count_use_selected_database(self):
        default, default_bytes = self._create_pending(
            "default", "default-limit"
        )
        other_rows = [
            self._create_pending("other", "other-limit-{}".format(index))
            for index in range(3)
        ]
        stdout = StringIO()

        call_command(
            "retry_media_deletions",
            database="other",
            execute=True,
            limit=2,
            stdout=stdout,
        )

        remaining_other = other_rows[2]
        self.assertEqual(
            list(
                self._pending_deletions("other").values_list(
                    "name", flat=True
                )
            ),
            [remaining_other[0].name],
        )
        self.assertTrue(
            self._pending_deletions("default")
            .filter(pk=default.pk)
            .exists()
        )
        self.assertEqual(
            media_snapshot(self.temporary_media.name),
            {
                default.name: default_bytes,
                remaining_other[0].name: remaining_other[1],
            },
        )
        self.assertIn(
            "pending=2 processed=2 remaining=1", stdout.getvalue()
        )

    def test_crash_recovery_command_keeps_signal_database_alias(self):
        default, default_bytes = self._create_pending(
            "default", "default-crash"
        )
        other_name = "originals/other-crash/original.png"
        other_bytes = b"other-crash-bytes"
        self._write_file(other_name, other_bytes)
        image = Image.objects.using("other").create(
            image=other_name,
            original_filename="original.png",
            width=32,
            height=32,
        )
        callbacks = []

        def stop_before_callback(callback, using=None):
            callbacks.append((callback, using))

        with mock.patch(
            "django.db.transaction.on_commit",
            side_effect=stop_before_callback,
        ):
            image.delete(using="other")

        self.assertEqual({using for _, using in callbacks}, {"other"})
        self.assertTrue(
            self._pending_deletions("other")
            .filter(kind="original", name=other_name)
            .exists()
        )
        self.assertEqual(
            media_snapshot(self.temporary_media.name),
            {
                default.name: default_bytes,
                other_name: other_bytes,
            },
        )

        call_command(
            "retry_media_deletions",
            database="other",
            execute=True,
            stdout=StringIO(),
        )

        self.assertFalse(self._pending_deletions("other").exists())
        self.assertTrue(
            self._pending_deletions("default")
            .filter(pk=default.pk)
            .exists()
        )
        self.assertEqual(
            media_snapshot(self.temporary_media.name),
            {default.name: default_bytes},
        )

    def test_other_database_canonical_exact_path_reference_is_preserved(self):
        asset_uuid = uuid.uuid4()
        name = "originals/{}/live.png".format(asset_uuid)
        self._write_file(name, b"other-live")
        pending = self._pending_deletions("other").create(
            kind="original", name=name
        )
        Image.objects.using("other").create(
            image=name,
            asset_uuid=asset_uuid,
            original_filename="live.png",
            width=32,
            height=32,
        )

        processed = process_pending_media_deletion(
            pending.pk, using="other"
        )

        self.assertTrue(processed)
        self.assertEqual(
            Path(self.temporary_media.name, name).read_bytes(),
            b"other-live",
        )
        self.assertFalse(self._pending_deletions("other").exists())

    def test_other_database_same_uuid_owner_preserves_empty_directory(self):
        asset_uuid = uuid.uuid4()
        name = "originals/{}/stale.png".format(asset_uuid)
        directory = Path(self.temporary_media.name, "originals", str(asset_uuid))
        self._write_file(name, b"other-stale")
        pending = self._pending_deletions("other").create(
            kind="original", name=name
        )
        Image.objects.using("other").create(
            image="legacy/current.png",
            asset_uuid=asset_uuid,
            original_filename="current.png",
            width=32,
            height=32,
        )

        processed = process_pending_media_deletion(
            pending.pk, using="other"
        )

        self.assertTrue(processed)
        self.assertFalse(Path(self.temporary_media.name, name).exists())
        self.assertTrue(directory.is_dir())
        self.assertFalse(self._pending_deletions("other").exists())
