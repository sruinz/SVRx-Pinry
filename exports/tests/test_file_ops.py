import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

from django.test import SimpleTestCase, override_settings
import mock

from exports.services import file_ops
from exports.services.file_ops import (
    ClosedFileReceipt,
    DirectoryReceipt,
    ExportStorageError,
    ExportWorkerLock,
    OpenFileReceipt,
    SourceReceipt,
    SpaceBudget,
    clone_or_copy,
    create_private_directory,
    create_private_file_fs,
    observe_available_space,
    open_export_root,
    open_receipted_directory,
    remove_if_receipt_matches,
    rename_noreplace,
    verify_space,
)


class ExportFileOpsTests(SimpleTestCase):
    def setUp(self):
        super(ExportFileOpsTests, self).setUp()
        temporary_root = os.path.realpath(tempfile.gettempdir())
        self.temporary = tempfile.TemporaryDirectory(dir=temporary_root)
        self.addCleanup(self.temporary.cleanup)
        os.chmod(self.temporary.name, 0o700)
        self.uid = os.geteuid()
        self.gid = os.getegid()
        self.root = open_export_root(self.temporary.name, self.uid, self.gid)
        self.addCleanup(self.root.close)

    def _private_file(self, directory=None, name="destination.part"):
        directory = self.root if directory is None else directory
        descriptor, receipt = create_private_file_fs(directory, name)
        self.addCleanup(self._close_if_open, descriptor)
        return descriptor, receipt

    @staticmethod
    def _close_if_open(descriptor):
        try:
            os.close(descriptor)
        except OSError as error:
            if error.errno != errno.EBADF:
                raise

    def _source(self, content=b"source bytes"):
        path = Path(self.temporary.name, "source-{}.bin".format(
            hashlib.sha256(content).hexdigest()[:12]
        ))
        path.write_bytes(content)
        os.chmod(str(path), 0o600)
        descriptor = os.open(str(path), os.O_RDONLY)
        self.addCleanup(self._close_if_open, descriptor)
        return path, descriptor, SourceReceipt.from_fd(descriptor)

    def test_open_receipt_rejects_hardlink_added_after_capture(self):
        descriptor, receipt = self._private_file()

        receipt.verify_identity(descriptor)
        os.link(
            str(Path(self.temporary.name, "destination.part")),
            str(Path(self.temporary.name, "destination.link")),
        )

        with self.assertRaisesRegex(
            ExportStorageError, "^export_storage_unsafe$"
        ):
            receipt.verify_identity(descriptor)

    def test_open_receipt_rejects_wrong_owner_group_or_mode(self):
        descriptor, _receipt = self._private_file()
        for uid, gid in (
            (self.uid + 1, self.gid),
            (self.uid, self.gid + 1),
        ):
            with self.subTest(uid=uid, gid=gid), self.assertRaisesRegex(
                ExportStorageError, "^export_storage_unsafe$"
            ):
                OpenFileReceipt.from_fd(descriptor, uid, gid)

        os.fchmod(descriptor, 0o640)
        with self.assertRaisesRegex(
            ExportStorageError, "^export_storage_unsafe$"
        ):
            OpenFileReceipt.from_fd(descriptor, self.uid, self.gid)

    def test_closed_receipt_requires_the_original_open_inode(self):
        descriptor, open_receipt = self._private_file()
        os.write(descriptor, b"payload")
        replacement = os.open(
            str(Path(self.temporary.name, "replacement.part")),
            os.O_RDWR | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        self.addCleanup(self._close_if_open, replacement)

        with self.assertRaisesRegex(
            ExportStorageError, "^export_storage_unsafe$"
        ):
            ClosedFileReceipt.from_open_fd(
                replacement,
                open_receipt,
                sha256=None,
            )

    def test_closed_transition_allows_content_change_then_freezes_it(self):
        descriptor, open_receipt = self._private_file()
        os.write(descriptor, b"payload after open receipt")
        os.fsync(descriptor)

        closed = ClosedFileReceipt.from_open_fd(
            descriptor,
            open_receipt,
            sha256=None,
        )
        closed.verify_identity(descriptor)
        os.pwrite(descriptor, b"X", 0)
        os.fsync(descriptor)

        with self.assertRaisesRegex(
            ExportStorageError, "^export_storage_unsafe$"
        ):
            closed.verify_identity(descriptor)

    def test_directory_receipt_rejects_wrong_owner_and_mode(self):
        DirectoryReceipt.from_fd(self.root.descriptor, self.uid, self.gid)
        with self.assertRaisesRegex(
            ExportStorageError, "^export_storage_unsafe$"
        ):
            DirectoryReceipt.from_fd(
                self.root.descriptor,
                self.uid + 1,
                self.gid,
            )

        os.chmod(self.temporary.name, 0o750)
        with self.assertRaisesRegex(
            ExportStorageError, "^export_storage_unsafe$"
        ):
            DirectoryReceipt.from_fd(
                self.root.descriptor,
                self.uid,
                self.gid,
            )

    def test_open_export_root_rejects_symbolic_link(self):
        link = Path(self.temporary.name).with_name(
            Path(self.temporary.name).name + "-link"
        )
        os.symlink(self.temporary.name, str(link))
        self.addCleanup(link.unlink)

        with self.assertRaisesRegex(
            ExportStorageError, "^export_storage_unsafe$"
        ):
            open_export_root(str(link), self.uid, self.gid)

    def test_create_private_directory_is_fresh_and_receipted(self):
        child = create_private_directory(
            self.root,
            "snapshot-job-1",
            self.uid,
            self.gid,
        )
        self.addCleanup(child.close)

        child.verify_identity()
        self.assertEqual(
            stat.S_IMODE(os.fstat(child.descriptor).st_mode),
            0o700,
        )
        with self.assertRaises(FileExistsError):
            create_private_directory(
                self.root,
                "snapshot-job-1",
                self.uid,
                self.gid,
            )

    def test_create_private_directory_wrong_uid_removes_created_name(self):
        with self.assertRaisesRegex(
            ExportStorageError, "^export_storage_unsafe$"
        ):
            create_private_directory(
                self.root,
                "attempt-wrong-owner",
                self.uid + 1,
                self.gid,
            )

        self.assertFalse(
            Path(self.temporary.name, "attempt-wrong-owner").exists()
        )

    def test_existing_directory_requires_its_full_receipt(self):
        child = create_private_directory(
            self.root,
            "snapshot-reopen",
            self.uid,
            self.gid,
        )
        receipt = child.receipt
        child.close()

        reopened = open_receipted_directory(
            self.root,
            "snapshot-reopen",
            receipt,
        )
        self.addCleanup(reopened.close)
        reopened.verify_identity()

        replacement = Path(self.temporary.name, "snapshot-reopen")
        moved = Path(self.temporary.name, "snapshot-reopen-old")
        reopened.close()
        replacement.rename(moved)
        self.addCleanup(moved.rmdir)
        replacement.mkdir(mode=0o700)
        os.chmod(str(replacement), 0o700)
        with self.assertRaisesRegex(
            ExportStorageError,
            "^export_storage_unsafe$",
        ):
            open_receipted_directory(
                self.root,
                "snapshot-reopen",
                receipt,
            )

    def test_reopen_final_verify_base_exception_closes_descriptor(self):
        directory = create_private_directory(
            self.root,
            "snapshot-reopen-failure",
            self.uid,
            self.gid,
        )
        receipt = directory.receipt
        directory.close()
        opened = {}
        interruption = KeyboardInterrupt("final verify interrupted")
        original_verify = file_ops.ExportDirectory.verify_identity

        def fail_child_verify(candidate):
            if candidate.name == "snapshot-reopen-failure":
                opened["descriptor"] = candidate.descriptor
                raise interruption
            return original_verify(candidate)

        with mock.patch.object(
            file_ops.ExportDirectory,
            "verify_identity",
            autospec=True,
            side_effect=fail_child_verify,
        ), self.assertRaises(KeyboardInterrupt) as caught:
            open_receipted_directory(
                self.root,
                "snapshot-reopen-failure",
                receipt,
            )

        descriptor = opened["descriptor"]
        self.addCleanup(self._close_if_open, descriptor)
        self.assertIs(caught.exception, interruption)
        with self.assertRaises(OSError) as closed:
            os.fstat(descriptor)
        self.assertEqual(closed.exception.errno, errno.EBADF)

    def test_create_private_file_fsync_failure_removes_created_name(self):
        real_fsync = file_ops.os.fsync

        def fail_parent_fsync(descriptor):
            if descriptor == self.root.descriptor:
                raise OSError(errno.EIO, "parent fsync failed")
            return real_fsync(descriptor)

        with mock.patch(
            "exports.services.file_ops.os.fsync",
            side_effect=fail_parent_fsync,
        ), self.assertRaisesRegex(
            ExportStorageError, "^export_storage_unsafe$"
        ):
            create_private_file_fs(self.root, "uncommitted.part")

        self.assertFalse(
            Path(self.temporary.name, "uncommitted.part").exists()
        )

    def test_create_private_file_fchmod_failure_removes_created_name(self):
        with mock.patch(
            "exports.services.file_ops.os.fchmod",
            side_effect=OSError(errno.EPERM, "mode change failed"),
        ), self.assertRaisesRegex(
            ExportStorageError, "^export_storage_unsafe$"
        ):
            create_private_file_fs(self.root, "failed-mode.part")

        self.assertFalse(
            Path(self.temporary.name, "failed-mode.part").exists()
        )

    def test_create_private_directory_fsync_failure_removes_created_name(self):
        real_fsync = file_ops.os.fsync

        def fail_parent_fsync(descriptor):
            if descriptor == self.root.descriptor:
                raise OSError(errno.EIO, "parent fsync failed")
            return real_fsync(descriptor)

        with mock.patch(
            "exports.services.file_ops.os.fsync",
            side_effect=fail_parent_fsync,
        ), self.assertRaisesRegex(
            ExportStorageError, "^export_storage_unsafe$"
        ):
            create_private_directory(
                self.root,
                "uncommitted-directory",
                self.uid,
                self.gid,
            )

        self.assertFalse(
            Path(self.temporary.name, "uncommitted-directory").exists()
        )

    def test_create_private_directory_normalizes_enospc(self):
        with mock.patch(
            "exports.services.file_ops.os.mkdir",
            side_effect=OSError(errno.ENOSPC, "full"),
        ), self.assertRaisesRegex(
            ExportStorageError,
            "^insufficient_space$",
        ):
            create_private_directory(
                self.root,
                "no-space-directory",
                self.uid,
                self.gid,
            )

    @override_settings(PINRY_EXPORT_SPACE_MIN_MARGIN_BYTES=512)
    def test_space_budget_uses_larger_of_fixed_and_ten_percent_margin(self):
        fixed = SpaceBudget.for_export(1000, 2000, 300)
        proportional = SpaceBudget.for_export(10000, 2000, 300)

        self.assertEqual(fixed.margin_bytes, 512)
        self.assertEqual(fixed.required_bytes, 3812)
        self.assertEqual(proportional.margin_bytes, 1000)
        self.assertEqual(proportional.required_bytes, 13300)

    def test_available_space_uses_bavail_times_fragment_size(self):
        filesystem = type("Filesystem", (), {
            "f_bavail": 7,
            "f_frsize": 4096,
        })()
        with mock.patch(
            "exports.services.file_ops.os.fstatvfs",
            return_value=filesystem,
        ) as fstatvfs:
            available = observe_available_space(self.root.descriptor)

        self.assertEqual(available, 7 * 4096)
        fstatvfs.assert_called_once_with(self.root.descriptor)

    def test_verify_space_rejects_insufficient_available_bytes(self):
        filesystem = type("Filesystem", (), {
            "f_bavail": 1,
            "f_frsize": 1024,
        })()
        with mock.patch(
            "exports.services.file_ops.os.fstatvfs",
            return_value=filesystem,
        ), self.assertRaisesRegex(
            ExportStorageError, "^insufficient_space$"
        ):
            verify_space(self.root.descriptor, 1025)

    def test_non_linux_copy_is_offset_safe_and_preserves_source(self):
        content = (b"0123456789abcdef" * 70000) + b"tail"
        _path, source_fd, source_receipt = self._source(content)
        destination_fd, _receipt = self._private_file()
        os.lseek(source_fd, 3, os.SEEK_SET)
        os.lseek(destination_fd, 5, os.SEEK_SET)
        heartbeats = []
        before_copy = []

        with mock.patch(
            "exports.services.file_ops.sys.platform", "darwin"
        ), mock.patch.object(
            file_ops._DarwinMetadataAdapter,
            "normalize",
        ) as normalize:
            method, digest = clone_or_copy(
                source_fd,
                destination_fd,
                source_receipt,
                heartbeat=lambda: heartbeats.append(True),
                stop_requested=lambda: False,
                before_copy=lambda size: before_copy.append(size),
            )

        self.assertEqual(method, "copy")
        self.assertEqual(digest, hashlib.sha256(content).hexdigest())
        self.assertEqual(os.pread(destination_fd, len(content), 0), content)
        self.assertEqual(before_copy, [len(content)])
        self.assertGreaterEqual(len(heartbeats), 2)
        normalize.assert_called_once_with(destination_fd)
        source_receipt.verify_identity(source_fd)
        self.assertNotEqual(
            os.fstat(source_fd).st_ino,
            os.fstat(destination_fd).st_ino,
        )
        self.assertEqual(os.fstat(source_fd).st_nlink, 1)

    def test_linux_reflink_success_does_not_calculate_hash(self):
        content = b"reflink payload"
        _path, source_fd, source_receipt = self._source(content)
        destination_fd, _receipt = self._private_file()

        def emulate_reflink(destination, operation, source):
            self.assertEqual(operation, file_ops._FICLONE)
            os.ftruncate(destination, 0)
            os.pwrite(destination, os.pread(source, len(content), 0), 0)
            return 0

        with mock.patch(
            "exports.services.file_ops.sys.platform", "linux"
        ), mock.patch(
            "exports.services.file_ops.fcntl.ioctl",
            side_effect=emulate_reflink,
        ) as ioctl, mock.patch.object(
            file_ops._LinuxMetadataAdapter,
            "normalize",
        ):
            method, digest = clone_or_copy(
                source_fd,
                destination_fd,
                source_receipt,
                heartbeat=lambda: None,
                stop_requested=lambda: False,
                before_copy=lambda _size: self.fail("copy fallback called"),
            )

        self.assertEqual((method, digest), ("reflink", None))
        self.assertEqual(ioctl.call_count, 1)
        self.assertEqual(os.pread(destination_fd, len(content), 0), content)

    def test_initial_stop_prevents_copy_or_reflink_side_effects(self):
        _path, source_fd, source_receipt = self._source(b"initial stop")
        destination_fd, _receipt = self._private_file()

        with mock.patch(
            "exports.services.file_ops.sys.platform", "linux"
        ), mock.patch(
            "exports.services.file_ops.fcntl.ioctl",
            side_effect=lambda *_args: self.fail("reflink attempted"),
        ), mock.patch.object(
            file_ops._LinuxMetadataAdapter,
            "normalize",
            side_effect=lambda *_args: self.fail("metadata normalized"),
        ), mock.patch(
            "exports.services.file_ops.os.fsync",
            side_effect=lambda *_args: self.fail("destination synced"),
        ), self.assertRaisesRegex(
            ExportStorageError, "^snapshot_failed$"
        ):
            clone_or_copy(
                source_fd,
                destination_fd,
                source_receipt,
                heartbeat=lambda: None,
                stop_requested=lambda: True,
                before_copy=lambda _size: self.fail("copy prepared"),
            )

    def test_final_copy_heartbeat_stop_prevents_metadata_and_fsync(self):
        _path, source_fd, source_receipt = self._source(b"final stop")
        destination_fd, _receipt = self._private_file()
        stopped = {"value": False}

        def heartbeat():
            stopped["value"] = True

        with mock.patch(
            "exports.services.file_ops.sys.platform", "darwin"
        ), mock.patch.object(
            file_ops._DarwinMetadataAdapter,
            "normalize",
            side_effect=lambda *_args: self.fail("metadata normalized"),
        ), mock.patch(
            "exports.services.file_ops.os.fsync",
            side_effect=lambda *_args: self.fail("destination synced"),
        ), self.assertRaisesRegex(
            ExportStorageError, "^snapshot_failed$"
        ):
            clone_or_copy(
                source_fd,
                destination_fd,
                source_receipt,
                heartbeat=heartbeat,
                stop_requested=lambda: stopped["value"],
                before_copy=lambda _size: None,
            )

    def test_reflink_stop_prevents_metadata_and_fsync(self):
        content = b"reflink stop"
        _path, source_fd, source_receipt = self._source(content)
        destination_fd, _receipt = self._private_file()
        stopped = {"value": False}

        def emulate_reflink(destination, _operation, source):
            os.ftruncate(destination, 0)
            os.pwrite(destination, os.pread(source, len(content), 0), 0)
            stopped["value"] = True
            return 0

        with mock.patch(
            "exports.services.file_ops.sys.platform", "linux"
        ), mock.patch(
            "exports.services.file_ops.fcntl.ioctl",
            side_effect=emulate_reflink,
        ), mock.patch.object(
            file_ops._LinuxMetadataAdapter,
            "normalize",
            side_effect=lambda *_args: self.fail("metadata normalized"),
        ), mock.patch(
            "exports.services.file_ops.os.fsync",
            side_effect=lambda *_args: self.fail("destination synced"),
        ), self.assertRaisesRegex(
            ExportStorageError, "^snapshot_failed$"
        ):
            clone_or_copy(
                source_fd,
                destination_fd,
                source_receipt,
                heartbeat=lambda: None,
                stop_requested=lambda: stopped["value"],
                before_copy=lambda _size: self.fail("copy prepared"),
            )

    def test_linux_unsupported_reflink_errors_fall_back_to_copy(self):
        for error_number in (errno.ENOTTY, errno.EOPNOTSUPP):
            with self.subTest(error_number=error_number):
                content = "fallback-{}".format(error_number).encode("ascii")
                _path, source_fd, source_receipt = self._source(content)
                destination_fd, _receipt = self._private_file(
                    name="fallback-{}.part".format(error_number)
                )
                before_copy = []
                with mock.patch(
                    "exports.services.file_ops.sys.platform", "linux"
                ), mock.patch(
                    "exports.services.file_ops.fcntl.ioctl",
                    side_effect=OSError(error_number, "unsupported"),
                ), mock.patch.object(
                    file_ops._LinuxMetadataAdapter,
                    "normalize",
                ):
                    method, digest = clone_or_copy(
                        source_fd,
                        destination_fd,
                        source_receipt,
                        heartbeat=lambda: None,
                        stop_requested=lambda: False,
                        before_copy=lambda size: before_copy.append(size),
                    )

                self.assertEqual(method, "copy")
                self.assertEqual(
                    digest,
                    hashlib.sha256(content).hexdigest(),
                )
                self.assertEqual(before_copy, [len(content)])

    def test_linux_reflink_permission_and_io_errors_are_fatal(self):
        for error_number in (errno.EPERM, errno.EIO):
            with self.subTest(error_number=error_number):
                _path, source_fd, source_receipt = self._source(
                    "fatal-{}".format(error_number).encode("ascii")
                )
                destination_fd, _receipt = self._private_file(
                    name="fatal-{}.part".format(error_number)
                )
                before_copy = mock.Mock()
                with mock.patch(
                    "exports.services.file_ops.sys.platform", "linux"
                ), mock.patch(
                    "exports.services.file_ops.fcntl.ioctl",
                    side_effect=OSError(error_number, "fatal"),
                ), self.assertRaisesRegex(
                    ExportStorageError, "^snapshot_failed$"
                ):
                    clone_or_copy(
                        source_fd,
                        destination_fd,
                        source_receipt,
                        heartbeat=lambda: None,
                        stop_requested=lambda: False,
                        before_copy=before_copy,
                    )
                before_copy.assert_not_called()

    def test_copy_normalizes_enospc(self):
        _path, source_fd, source_receipt = self._source(b"payload")
        destination_fd, _receipt = self._private_file()
        with mock.patch(
            "exports.services.file_ops.sys.platform", "darwin"
        ), mock.patch(
            "exports.services.file_ops.os.pwrite",
            side_effect=OSError(errno.ENOSPC, "full"),
        ), self.assertRaisesRegex(
            ExportStorageError, "^insufficient_space$"
        ):
            clone_or_copy(
                source_fd,
                destination_fd,
                source_receipt,
                heartbeat=lambda: None,
                stop_requested=lambda: False,
                before_copy=lambda _size: None,
            )

    def test_copy_rejects_destination_mode_changed_after_open(self):
        _path, source_fd, source_receipt = self._source(b"payload")
        destination_fd, _receipt = self._private_file()

        with mock.patch(
            "exports.services.file_ops.sys.platform", "darwin"
        ), mock.patch.object(
            file_ops._DarwinMetadataAdapter,
            "normalize",
        ), self.assertRaisesRegex(
            ExportStorageError, "^export_storage_unsafe$"
        ):
            clone_or_copy(
                source_fd,
                destination_fd,
                source_receipt,
                heartbeat=lambda: None,
                stop_requested=lambda: False,
                before_copy=lambda _size: os.fchmod(
                    destination_fd,
                    0o640,
                ),
            )

    def test_linux_metadata_adapter_removes_and_rechecks_xattrs(self):
        listed = [["user.pinry"], []]
        with mock.patch(
            "exports.services.file_ops.os.listxattr",
            side_effect=lambda _fd: listed.pop(0),
            create=True,
        ) as listxattr, mock.patch(
            "exports.services.file_ops.os.removexattr",
            create=True,
        ) as removexattr:
            file_ops._LinuxMetadataAdapter.normalize(17)

        self.assertEqual(listxattr.call_count, 2)
        removexattr.assert_called_once_with(17, "user.pinry")

    def test_linux_metadata_adapter_fails_closed_if_recheck_is_not_empty(self):
        with mock.patch(
            "exports.services.file_ops.os.listxattr",
            side_effect=[["user.pinry"], ["user.pinry"]],
            create=True,
        ), mock.patch(
            "exports.services.file_ops.os.removexattr",
            create=True,
        ), self.assertRaisesRegex(
            ExportStorageError, "^export_storage_unsafe$"
        ):
            file_ops._LinuxMetadataAdapter.normalize(17)

    def test_darwin_metadata_adapter_uses_fd_acl_and_xattr_apis(self):
        acl_state = {"present": True}
        freed_acls = []
        names = [b"com.apple.pinry"]
        raw_names = b"com.apple.pinry\x00"

        def acl_get_fd_np(descriptor, acl_type):
            if descriptor != 17 or acl_type != 0x00000100:
                ctypes.set_errno(errno.EINVAL)
                return None
            if acl_state["present"]:
                ctypes.set_errno(0)
                return 101
            ctypes.set_errno(errno.ENOENT)
            return None

        def acl_get_entry(acl, entry_id, _entry):
            if acl != 101 or entry_id != 0:
                ctypes.set_errno(errno.EINVAL)
                return -1
            return 0

        def acl_init(count):
            if count != 0:
                ctypes.set_errno(errno.EINVAL)
                return None
            return 202

        def acl_set_fd_np(descriptor, acl, acl_type):
            if (
                descriptor != 17
                or acl != 202
                or acl_type != 0x00000100
            ):
                ctypes.set_errno(errno.EINVAL)
                return -1
            acl_state["present"] = False
            return 0

        def acl_free(acl):
            freed_acls.append(acl)
            return 0

        def flistxattr(_descriptor, buffer, _size, _options):
            if not names:
                return 0
            if buffer is None:
                return len(raw_names)
            ctypes.memmove(buffer, raw_names, len(raw_names))
            return len(raw_names)

        def fremovexattr(_descriptor, name, _options):
            names.remove(name)
            return 0

        libc = mock.Mock()
        libc.acl_get_fd_np = mock.Mock(side_effect=acl_get_fd_np)
        libc.acl_get_entry = mock.Mock(side_effect=acl_get_entry)
        libc.acl_init = mock.Mock(side_effect=acl_init)
        libc.acl_set_fd_np = mock.Mock(side_effect=acl_set_fd_np)
        libc.acl_free = mock.Mock(side_effect=acl_free)
        libc.flistxattr = mock.Mock(side_effect=flistxattr)
        libc.fremovexattr = mock.Mock(side_effect=fremovexattr)

        with mock.patch.object(
            file_ops.sys,
            "platform",
            "darwin",
        ), mock.patch.object(
            file_ops._DarwinMetadataAdapter,
            "_libc",
            return_value=libc,
        ):
            file_ops._normalize_metadata(17)

        self.assertFalse(acl_state["present"])
        self.assertEqual(freed_acls, [101, 202])
        self.assertEqual(names, [])

    def test_darwin_metadata_adapter_rejects_unknown_acl_result(self):
        libc = mock.Mock()
        libc.acl_get_fd_np = mock.Mock(return_value=1)
        libc.acl_get_entry = mock.Mock(return_value=1)
        libc.acl_init = mock.Mock(return_value=2)
        libc.acl_set_fd_np = mock.Mock(return_value=0)
        libc.acl_free = mock.Mock(return_value=0)
        libc.flistxattr = mock.Mock(return_value=0)
        libc.fremovexattr = mock.Mock(return_value=0)

        with mock.patch.object(
            file_ops._DarwinMetadataAdapter,
            "_libc",
            return_value=libc,
        ), self.assertRaisesRegex(
            ExportStorageError, "^export_storage_unsafe$"
        ):
            file_ops._DarwinMetadataAdapter.normalize(17)

    def test_darwin_acl_set_failure_frees_observed_and_empty_acl(self):
        freed_acls = []

        def fail_acl_set(_descriptor, _acl, _acl_type):
            ctypes.set_errno(errno.EPERM)
            return -1

        def acl_free(acl):
            freed_acls.append(acl)
            return 0

        libc = mock.Mock()
        libc.acl_get_fd_np = mock.Mock(return_value=101)
        libc.acl_get_entry = mock.Mock(return_value=0)
        libc.acl_init = mock.Mock(return_value=202)
        libc.acl_set_fd_np = mock.Mock(side_effect=fail_acl_set)
        libc.acl_free = mock.Mock(side_effect=acl_free)

        with self.assertRaisesRegex(
            ExportStorageError, "^export_storage_unsafe$"
        ):
            file_ops._DarwinMetadataAdapter._normalize_acl(libc, 17)

        self.assertEqual(freed_acls, [101, 202])

    @unittest.skipUnless(sys.platform == "darwin", "Darwin ACL integration")
    def test_darwin_metadata_adapter_removes_actual_extended_acl(self):
        path = Path(self.temporary.name, "darwin-acl.part")
        path.write_bytes(b"acl")
        os.chmod(str(path), 0o600)
        descriptor = os.open(str(path), os.O_RDWR)
        self.addCleanup(self._close_if_open, descriptor)

        subprocess.run(
            ["chmod", "+a", "everyone deny write", str(path)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(
            subprocess.run,
            ["chmod", "-N", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        libc = file_ops._DarwinMetadataAdapter._libc()
        self.assertTrue(
            file_ops._DarwinMetadataAdapter._acl_has_entry(
                libc,
                descriptor,
            )
        )

        file_ops._DarwinMetadataAdapter._normalize_acl(libc, descriptor)

        self.assertFalse(
            file_ops._DarwinMetadataAdapter._acl_has_entry(
                libc,
                descriptor,
            )
        )

    def test_remove_refuses_replaced_inode(self):
        descriptor, receipt = self._private_file()
        os.close(descriptor)
        original = Path(self.temporary.name, "destination.part")
        original.unlink()
        original.write_bytes(b"replacement")
        os.chmod(str(original), 0o600)

        with self.assertRaisesRegex(
            ExportStorageError, "^export_storage_unsafe$"
        ):
            remove_if_receipt_matches(
                self.root,
                "destination.part",
                receipt,
            )
        self.assertTrue(original.exists())

    def test_rename_noreplace_does_not_replace_destination(self):
        source_fd, _source_receipt = self._private_file(name="source.part")
        destination_fd, _destination_receipt = self._private_file(
            name="destination.zip"
        )
        os.write(source_fd, b"source")
        os.write(destination_fd, b"destination")

        with self.assertRaises(FileExistsError):
            rename_noreplace(
                self.root,
                "source.part",
                self.root,
                "destination.zip",
            )
        self.assertEqual(
            Path(self.temporary.name, "destination.zip").read_bytes(),
            b"destination",
        )

    def test_rename_noreplace_preserves_the_open_inode(self):
        descriptor, receipt = self._private_file(name="source.part")
        os.write(descriptor, b"source")

        rename_noreplace(
            self.root,
            "source.part",
            self.root,
            "published.zip",
        )

        receipt.verify_identity(descriptor)
        self.assertFalse(Path(self.temporary.name, "source.part").exists())
        self.assertEqual(
            Path(self.temporary.name, "published.zip").read_bytes(),
            b"source",
        )

    def test_worker_lock_rejects_hardlink_before_flock(self):
        lock_path = Path(self.temporary.name, ".worker.lock")
        lock_path.write_bytes(b"")
        os.chmod(str(lock_path), 0o600)
        os.link(str(lock_path), str(Path(self.temporary.name, "lock.link")))

        with mock.patch(
            "exports.services.file_ops.fcntl.flock"
        ) as flock, self.assertRaisesRegex(
            ExportStorageError, "^export_storage_unsafe$"
        ):
            ExportWorkerLock.acquire(self.root, self.uid, self.gid)
        flock.assert_not_called()

    def test_worker_lock_rejects_wrong_owner_before_flock(self):
        lock_path = Path(self.temporary.name, ".worker.lock")
        lock_path.write_bytes(b"")
        os.chmod(str(lock_path), 0o600)

        with mock.patch(
            "exports.services.file_ops.fcntl.flock"
        ) as flock, self.assertRaisesRegex(
            ExportStorageError, "^export_storage_unsafe$"
        ):
            ExportWorkerLock.acquire(self.root, self.uid + 1, self.gid)
        flock.assert_not_called()

    def test_worker_lock_rechecks_named_inode_after_flock(self):
        lock_path = Path(self.temporary.name, ".worker.lock")
        lock_path.write_bytes(b"")
        os.chmod(str(lock_path), 0o600)

        def replace_after_lock(_descriptor, _operation):
            lock_path.unlink()
            lock_path.write_bytes(b"replacement")
            os.chmod(str(lock_path), 0o600)

        with mock.patch(
            "exports.services.file_ops.fcntl.flock",
            side_effect=replace_after_lock,
        ), self.assertRaisesRegex(
            ExportStorageError, "^export_storage_unsafe$"
        ):
            ExportWorkerLock.acquire(self.root, self.uid, self.gid)

    def test_worker_lock_is_held_until_close(self):
        first = ExportWorkerLock.acquire(self.root, self.uid, self.gid)
        with self.assertRaisesRegex(
            ExportStorageError,
            "^export_worker_unavailable$",
        ):
            ExportWorkerLock.acquire(self.root, self.uid, self.gid)

        first.close()
        second = ExportWorkerLock.acquire(self.root, self.uid, self.gid)
        second.close()


class PostgreSQLTestSettingsTests(SimpleTestCase):
    environment_names = (
        "PINRY_TEST_POSTGRES_HOST",
        "PINRY_TEST_POSTGRES_PORT",
        "PINRY_TEST_POSTGRES_NAME",
        "PINRY_TEST_POSTGRES_USER",
        "PINRY_TEST_POSTGRES_PASSWORD",
    )

    def test_settings_reject_missing_connection_environment(self):
        environment = os.environ.copy()
        for name in self.environment_names:
            environment.pop(name, None)

        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import pinry.settings.test_postgres",
            ],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            b"PINRY_TEST_POSTGRES_HOST is required",
            result.stderr,
        )

    def test_settings_use_base_and_require_explicit_connection_values(self):
        values = {
            "PINRY_TEST_POSTGRES_HOST": "postgres.invalid",
            "PINRY_TEST_POSTGRES_PORT": "5544",
            "PINRY_TEST_POSTGRES_NAME": "pinry_test",
            "PINRY_TEST_POSTGRES_USER": "pinry_test_user",
            "PINRY_TEST_POSTGRES_PASSWORD": "secret-fixture",
        }
        environment = os.environ.copy()
        environment.update(values)
        output = subprocess.check_output(
            [
                sys.executable,
                "-c",
                (
                    "import json; "
                    "import pinry.settings.test_postgres as settings; "
                    "print(json.dumps({"
                    "'database': settings.DATABASES['default'], "
                    "'secret': bool(settings.SECRET_KEY), "
                    "'debug': settings.DEBUG, "
                    "'hosts': settings.ALLOWED_HOSTS, "
                    "'apps': settings.INSTALLED_APPS"
                    "}))"
                ),
            ],
            env=environment,
        )
        payload = json.loads(output.decode("utf-8"))

        database = payload["database"]
        self.assertEqual(database, {
            "ENGINE": "django.db.backends.postgresql",
            "HOST": "postgres.invalid",
            "PORT": "5544",
            "NAME": "pinry_test",
            "USER": "pinry_test_user",
            "PASSWORD": "secret-fixture",
        })
        self.assertTrue(payload["secret"])
        self.assertFalse(payload["debug"])
        self.assertEqual(payload["hosts"], ["testserver"])
        self.assertNotIn("django_extensions", payload["apps"])
