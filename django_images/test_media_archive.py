import ctypes
import errno
import hashlib
import json
import os
import shutil
import stat
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest import mock
import uuid

from django.core.management import CommandError
from django.test import SimpleTestCase, TestCase
from django.utils.text import get_valid_filename

from django_images.file_ops import (
    open_verified_media_file,
    open_verified_media_root,
)
from django_images.models import Image, Thumbnail
from django_images.paths import (
    DERIVATIVE_NAMES,
    canonical_derivative_path,
    canonical_original_path,
)
from django_images.services.media_archive import (
    ArchiveIntent,
    LegacyMediaArchive,
    LinuxRenameNoReplaceAdapter,
    MediaArchiveError,
    build_archive_intent,
    fixed_slot_destination_path,
    open_archive_session,
    _validate_archive_intent_layout,
    validate_no_legacy_media_references,
)
from django_images.services.media_migration_v2 import (
    AUTO_V2_MANIFEST_FILENAME,
    AUTO_V2_TARGET_SIGNATURE,
    AutoV2ManifestLog,
    AutoV2MigrationFile,
    AutoV2MigrationPlan,
    load_auto_v2_archive_authority,
    load_completed_auto_v2_summary,
)


RUN_ID = "20260825T120000Z-12345678-1234-4678-9234-567812345678"


class RecordingRenameNoReplaceAdapter(object):
    def __init__(self, error_number=None, crash_after_rename=False):
        self.error_number = error_number
        self.crash_after_rename = crash_after_rename
        self.calls = []
        self.descriptors_were_open = False

    def rename_noreplace(
        self,
        source_parent_descriptor,
        source_name,
        destination_parent_descriptor,
        destination_name,
    ):
        self.calls.append((
            source_parent_descriptor,
            source_name,
            destination_parent_descriptor,
            destination_name,
        ))
        os.fstat(source_parent_descriptor)
        os.fstat(destination_parent_descriptor)
        self.descriptors_were_open = True
        if self.error_number is not None:
            raise OSError(self.error_number, "injected")
        try:
            os.stat(
                destination_name,
                dir_fd=destination_parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            raise OSError(errno.EEXIST, "injected")
        os.rename(
            source_name,
            destination_name,
            src_dir_fd=source_parent_descriptor,
            dst_dir_fd=destination_parent_descriptor,
        )
        if self.crash_after_rename:
            raise SimulatedProcessCrash()


class SimulatedProcessCrash(Exception):
    pass


class FakeCFunction(object):
    def __init__(self, result=0, error_number=0):
        self.result = result
        self.error_number = error_number
        self.calls = []
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        self.calls.append(args)
        ctypes.set_errno(self.error_number)
        return self.result


class FakeLibc(object):
    def __init__(self, renameat2):
        self.renameat2 = renameat2


class MediaArchiveFilesystemTests(SimpleTestCase):
    def setUp(self):
        super(MediaArchiveFilesystemTests, self).setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.temporary_root = Path(os.path.realpath(temporary.name))
        self.source_root = self.temporary_root / "media"
        self.destination_root = self.temporary_root / "run"
        self.source_root.mkdir()
        self.destination_root.mkdir()

    def make_md5_intent(self):
        (self.source_root / "image").mkdir()
        (self.source_root / "image" / "orphan.bin").write_bytes(b"orphan")
        (self.destination_root / "media").mkdir()
        return build_archive_intent(
            str(self.source_root),
            "image",
            str(self.destination_root),
            "media/image",
        )

    def make_fixed_slot_intent(self):
        asset_uuid = "11111111-1111-4111-8111-111111111111"
        source_relative = "originals/{}/original.png".format(asset_uuid)
        destination_relative = fixed_slot_destination_path(source_relative)
        source = self.source_root / source_relative
        destination = self.destination_root / destination_relative
        source.parent.mkdir(parents=True)
        source.write_bytes(b"legacy-original")
        destination.parent.mkdir(parents=True)
        return build_archive_intent(
            str(self.source_root),
            source_relative,
            str(self.destination_root),
            destination_relative,
        )

    def assert_archive_error(self, code, callback):
        with self.assertRaisesRegex(MediaArchiveError, "^{}$".format(code)):
            callback()

    def test_fresh_md5_archive_uses_one_noreplace_call_and_fsyncs_both_parents(self):
        intent = self.make_md5_intent()
        adapter = RecordingRenameNoReplaceAdapter()
        events = []

        with open_archive_session(
            str(self.source_root),
            str(self.destination_root),
            intent,
            syscall_adapter=adapter,
        ) as session:
            source_parent_descriptor = session.source_parent_descriptor
            destination_parent_descriptor = (
                session.destination_parent_descriptor
            )

            def record_fsync(descriptor):
                if descriptor == source_parent_descriptor:
                    events.append("source_parent_fsync")
                elif descriptor == destination_parent_descriptor:
                    events.append("destination_parent_fsync")
                else:
                    events.append("other_fsync")

            with mock.patch(
                "django_images.services.media_archive.os.fsync",
                side_effect=record_fsync,
            ):
                result = session.archive_noreplace(intent)

            os.fstat(source_parent_descriptor)
            os.fstat(destination_parent_descriptor)

        self.assertEqual(result.status, "archived")
        self.assertEqual(len(adapter.calls), 1)
        self.assertTrue(adapter.descriptors_were_open)
        self.assertEqual(
            adapter.calls[0][1:],
            ("image", destination_parent_descriptor, "image"),
        )
        self.assertEqual(
            events,
            ["source_parent_fsync", "destination_parent_fsync"],
        )
        self.assertFalse((self.source_root / "image").exists())
        self.assertEqual(
            (self.destination_root / "media" / "image" / "orphan.bin")
            .read_bytes(),
            b"orphan",
        )
        with self.assertRaises(OSError):
            os.fstat(source_parent_descriptor)
        with self.assertRaises(OSError):
            os.fstat(destination_parent_descriptor)

    def test_fixed_slot_archive_preserves_the_relative_layout(self):
        intent = self.make_fixed_slot_intent()
        adapter = RecordingRenameNoReplaceAdapter()

        with open_archive_session(
            str(self.source_root),
            str(self.destination_root),
            intent,
            syscall_adapter=adapter,
        ) as session:
            result = session.archive_noreplace(intent)

        source_relative = "originals/{}/original.png".format(
            "11111111-1111-4111-8111-111111111111"
        )
        destination = self.destination_root / fixed_slot_destination_path(
            source_relative
        )
        self.assertEqual(result.status, "archived")
        self.assertFalse((self.source_root / source_relative).exists())
        self.assertEqual(destination.read_bytes(), b"legacy-original")
        self.assertEqual(os.stat(str(destination)).st_nlink, 1)

    def test_existing_destination_fails_without_calling_the_adapter(self):
        intent = self.make_fixed_slot_intent()
        destination = (
            self.destination_root
            / intent.destination_parent_relative
            / intent.destination_name
        )
        destination.write_bytes(b"external")
        adapter = RecordingRenameNoReplaceAdapter()

        def archive():
            with open_archive_session(
                str(self.source_root),
                str(self.destination_root),
                intent,
                syscall_adapter=adapter,
            ) as session:
                session.archive_noreplace(intent)

        self.assert_archive_error("archive_state_conflict", archive)
        self.assertEqual(adapter.calls, [])
        self.assertEqual(destination.read_bytes(), b"external")

    def test_destination_symlink_fails_without_following_it(self):
        intent = self.make_fixed_slot_intent()
        outside = self.temporary_root / "outside"
        outside.write_bytes(b"outside")
        destination = (
            self.destination_root
            / intent.destination_parent_relative
            / intent.destination_name
        )
        destination.symlink_to(outside)
        adapter = RecordingRenameNoReplaceAdapter()

        def archive():
            with open_archive_session(
                str(self.source_root),
                str(self.destination_root),
                intent,
                syscall_adapter=adapter,
            ) as session:
                session.archive_noreplace(intent)

        self.assert_archive_error("archive_state_conflict", archive)
        self.assertEqual(adapter.calls, [])
        self.assertEqual(outside.read_bytes(), b"outside")

    def test_source_symlink_substitution_fails_closed(self):
        intent = self.make_fixed_slot_intent()
        source = (
            self.source_root
            / intent.source_parent_relative
            / intent.source_name
        )
        outside = self.temporary_root / "outside"
        outside.write_bytes(b"outside")
        source.unlink()
        source.symlink_to(outside)

        self.assert_archive_error(
            "archive_state_conflict",
            lambda: open_archive_session(
                str(self.source_root),
                str(self.destination_root),
                intent,
                syscall_adapter=RecordingRenameNoReplaceAdapter(),
            ),
        )

    def test_source_hardlink_substitution_fails_closed(self):
        intent = self.make_fixed_slot_intent()
        source = (
            self.source_root
            / intent.source_parent_relative
            / intent.source_name
        )
        os.link(str(source), str(self.temporary_root / "second-link"))

        self.assert_archive_error(
            "archive_state_conflict",
            lambda: open_archive_session(
                str(self.source_root),
                str(self.destination_root),
                intent,
                syscall_adapter=RecordingRenameNoReplaceAdapter(),
            ),
        )

    def test_non_regular_fixed_slot_source_is_rejected(self):
        source_parent = self.source_root / "originals" / "unsafe"
        destination_parent = (
            self.destination_root
            / "media"
            / "fixed-slot-originals"
            / "originals"
            / "unsafe"
        )
        source_parent.mkdir(parents=True)
        destination_parent.mkdir(parents=True)
        os.mkfifo(str(source_parent / "original.png"))

        self.assert_archive_error(
            "archive_state_conflict",
            lambda: build_archive_intent(
                str(self.source_root),
                "originals/unsafe/original.png",
                str(self.destination_root),
                "media/fixed-slot-originals/originals/unsafe/original.png",
            ),
        )

    def test_md5_source_must_be_a_real_directory(self):
        (self.source_root / "image").write_bytes(b"not-a-directory")
        (self.destination_root / "media").mkdir()

        self.assert_archive_error(
            "archive_state_conflict",
            lambda: build_archive_intent(
                str(self.source_root),
                "image",
                str(self.destination_root),
                "media/image",
            ),
        )

    def test_exact_root_parent_and_source_identities_are_required(self):
        intent = self.make_fixed_slot_intent()
        changed_intents = (
            replace(intent, source_root_inode=intent.source_root_inode + 1),
            replace(intent, source_parent_inode=intent.source_parent_inode + 1),
            replace(intent, source_inode=intent.source_inode + 1),
            replace(
                intent,
                destination_root_inode=intent.destination_root_inode + 1,
            ),
            replace(
                intent,
                destination_parent_inode=(
                    intent.destination_parent_inode + 1
                ),
            ),
        )

        for changed in changed_intents:
            with self.subTest(changed=changed):
                self.assert_archive_error(
                    "archive_state_conflict",
                    lambda changed=changed: open_archive_session(
                        str(self.source_root),
                        str(self.destination_root),
                        changed,
                        syscall_adapter=RecordingRenameNoReplaceAdapter(),
                    ),
                )

    def test_leaf_on_a_different_filesystem_is_rejected_on_resume(self):
        intent = self.make_fixed_slot_intent()
        forged_device = intent.source_device + 1
        forged_intent = replace(intent, source_device=forged_device)
        real_stat = os.stat
        real_fstat = os.fstat
        source_root = open_verified_media_root(str(self.source_root))
        destination_root = open_verified_media_root(
            str(self.destination_root)
        )
        self.addCleanup(destination_root.close)
        self.addCleanup(source_root.close)

        def forge_leaf_device(file_stat):
            if (
                stat.S_ISREG(file_stat.st_mode)
                and file_stat.st_ino == intent.source_inode
            ):
                values = list(file_stat)
                values[stat.ST_DEV] = forged_device
                return os.stat_result(values)
            return file_stat

        def patched_stat(*args, **kwargs):
            return forge_leaf_device(real_stat(*args, **kwargs))

        def patched_fstat(*args, **kwargs):
            return forge_leaf_device(real_fstat(*args, **kwargs))

        adapter = RecordingRenameNoReplaceAdapter()

        def duplicate_root(path):
            root = (
                source_root
                if path == str(self.source_root)
                else destination_root
            )
            return root.duplicate_owned()

        def open_session():
            session = open_archive_session(
                str(self.source_root),
                str(self.destination_root),
                forged_intent,
                syscall_adapter=adapter,
            )
            session.close()

        with mock.patch(
            "django_images.services.media_archive.open_verified_media_root",
            side_effect=duplicate_root,
        ), mock.patch(
            "django_images.services.media_archive.os.stat",
            side_effect=patched_stat,
        ), mock.patch(
            "django_images.services.media_archive.os.fstat",
            side_effect=patched_fstat,
        ):
            self.assert_archive_error(
                "archive_state_conflict", open_session
            )

        self.assertEqual(adapter.calls, [])

    def test_parent_path_swap_after_open_is_detected_before_rename(self):
        intent = self.make_fixed_slot_intent()
        adapter = RecordingRenameNoReplaceAdapter()
        source_parent = self.source_root / intent.source_parent_relative
        moved_parent = source_parent.with_name("moved")

        with open_archive_session(
            str(self.source_root),
            str(self.destination_root),
            intent,
            syscall_adapter=adapter,
        ) as session:
            source_parent.rename(moved_parent)
            source_parent.mkdir()
            self.assert_archive_error(
                "archive_state_conflict",
                lambda: session.archive_noreplace(intent),
            )

        self.assertEqual(adapter.calls, [])

    def test_cross_filesystem_adapter_result_fails_closed(self):
        intent = self.make_fixed_slot_intent()
        adapter = RecordingRenameNoReplaceAdapter(error_number=errno.EXDEV)

        def archive():
            with open_archive_session(
                str(self.source_root),
                str(self.destination_root),
                intent,
                syscall_adapter=adapter,
            ) as session:
                session.archive_noreplace(intent)

        self.assert_archive_error("archive_state_conflict", archive)
        self.assertTrue(
            self.source_root.joinpath(
                intent.source_parent_relative,
                intent.source_name,
            ).exists()
        )

    def test_unsupported_atomic_archive_errors_are_normalized(self):
        unsupported = {
            errno.ENOSYS,
            errno.EINVAL,
            errno.EOPNOTSUPP,
        }
        for error_number in unsupported:
            with self.subTest(error_number=error_number):
                case_root = self.temporary_root / str(error_number)
                source_root = case_root / "source"
                destination_root = case_root / "destination"
                source_root.mkdir(parents=True)
                destination_root.mkdir()
                (source_root / "image").mkdir()
                (destination_root / "media").mkdir()
                intent = build_archive_intent(
                    str(source_root),
                    "image",
                    str(destination_root),
                    "media/image",
                )
                adapter = RecordingRenameNoReplaceAdapter(
                    error_number=error_number
                )

                def archive():
                    with open_archive_session(
                        str(source_root),
                        str(destination_root),
                        intent,
                        syscall_adapter=adapter,
                    ) as session:
                        session.archive_noreplace(intent)

                self.assert_archive_error(
                    "atomic_archive_unsupported", archive
                )

    def test_unexpected_syscall_error_is_non_identifying(self):
        intent = self.make_fixed_slot_intent()
        adapter = RecordingRenameNoReplaceAdapter(error_number=errno.EIO)

        def archive():
            with open_archive_session(
                str(self.source_root),
                str(self.destination_root),
                intent,
                syscall_adapter=adapter,
            ) as session:
                session.archive_noreplace(intent)

        self.assert_archive_error("archive_failed", archive)

    def test_unexpected_descriptor_io_error_is_archive_failed(self):
        intent = self.make_fixed_slot_intent()

        with mock.patch(
            "django_images.services.media_archive._open_archive_leaf",
            side_effect=OSError(errno.EIO, "injected"),
        ):
            self.assert_archive_error(
                "archive_failed",
                lambda: open_archive_session(
                    str(self.source_root),
                    str(self.destination_root),
                    intent,
                    syscall_adapter=RecordingRenameNoReplaceAdapter(),
                ),
            )

    def test_rename_complete_state_not_written_recovers_exact_identity(self):
        intent = self.make_fixed_slot_intent()
        crashing_adapter = RecordingRenameNoReplaceAdapter(
            crash_after_rename=True
        )

        with self.assertRaises(SimulatedProcessCrash):
            with open_archive_session(
                str(self.source_root),
                str(self.destination_root),
                intent,
                syscall_adapter=crashing_adapter,
            ) as session:
                session.archive_noreplace(intent)

        recovery_adapter = RecordingRenameNoReplaceAdapter()
        events = []
        with open_archive_session(
            str(self.source_root),
            str(self.destination_root),
            intent,
            syscall_adapter=recovery_adapter,
        ) as session:
            source_parent_descriptor = session.source_parent_descriptor
            destination_parent_descriptor = (
                session.destination_parent_descriptor
            )

            def record_fsync(descriptor):
                if descriptor == source_parent_descriptor:
                    events.append("source_parent_fsync")
                elif descriptor == destination_parent_descriptor:
                    events.append("destination_parent_fsync")

            with mock.patch(
                "django_images.services.media_archive.os.fsync",
                side_effect=record_fsync,
            ):
                result = session.recover_archive(intent)

        self.assertEqual(result.status, "recovered")
        self.assertEqual(recovery_adapter.calls, [])
        self.assertEqual(
            events,
            ["source_parent_fsync", "destination_parent_fsync"],
        )

    def test_both_source_and_destination_are_a_conflict(self):
        intent = self.make_fixed_slot_intent()
        destination = (
            self.destination_root
            / intent.destination_parent_relative
            / intent.destination_name
        )
        destination.write_bytes(b"external")

        def recover():
            with open_archive_session(
                str(self.source_root),
                str(self.destination_root),
                intent,
                syscall_adapter=RecordingRenameNoReplaceAdapter(),
            ) as session:
                session.recover_archive(intent)

        self.assert_archive_error("archive_state_conflict", recover)

    def test_both_source_and_destination_missing_are_a_conflict(self):
        intent = self.make_fixed_slot_intent()
        source = (
            self.source_root
            / intent.source_parent_relative
            / intent.source_name
        )
        source.unlink()

        def recover():
            with open_archive_session(
                str(self.source_root),
                str(self.destination_root),
                intent,
                syscall_adapter=RecordingRenameNoReplaceAdapter(),
            ) as session:
                session.recover_archive(intent)

        self.assert_archive_error("archive_state_conflict", recover)

    def test_recovery_rejects_a_different_destination_identity(self):
        intent = self.make_fixed_slot_intent()
        source = (
            self.source_root
            / intent.source_parent_relative
            / intent.source_name
        )
        destination = (
            self.destination_root
            / intent.destination_parent_relative
            / intent.destination_name
        )
        source.unlink()
        destination.write_bytes(b"external")

        def recover():
            with open_archive_session(
                str(self.source_root),
                str(self.destination_root),
                intent,
                syscall_adapter=RecordingRenameNoReplaceAdapter(),
            ) as session:
                session.recover_archive(intent)

        self.assert_archive_error("archive_state_conflict", recover)
        self.assertEqual(destination.read_bytes(), b"external")

    def test_recovery_rejects_a_hardlinked_destination(self):
        intent = self.make_fixed_slot_intent()
        adapter = RecordingRenameNoReplaceAdapter(crash_after_rename=True)
        with self.assertRaises(SimulatedProcessCrash):
            with open_archive_session(
                str(self.source_root),
                str(self.destination_root),
                intent,
                syscall_adapter=adapter,
            ) as session:
                session.archive_noreplace(intent)

        destination = (
            self.destination_root
            / intent.destination_parent_relative
            / intent.destination_name
        )
        os.link(str(destination), str(self.temporary_root / "second-link"))
        self.assert_archive_error(
            "archive_state_conflict",
            lambda: open_archive_session(
                str(self.source_root),
                str(self.destination_root),
                intent,
                syscall_adapter=RecordingRenameNoReplaceAdapter(),
            ),
        )

    def test_converge_distinguishes_fresh_and_recovery_states(self):
        intent = self.make_fixed_slot_intent()
        adapter = RecordingRenameNoReplaceAdapter()
        with open_archive_session(
            str(self.source_root),
            str(self.destination_root),
            intent,
            syscall_adapter=adapter,
        ) as session:
            fresh = session._converge_archive(intent)
        with open_archive_session(
            str(self.source_root),
            str(self.destination_root),
            intent,
            syscall_adapter=adapter,
        ) as session:
            recovered = session._converge_archive(intent)

        self.assertEqual(fresh.status, "archived")
        self.assertEqual(recovered.status, "recovered")
        self.assertEqual(len(adapter.calls), 1)

    def test_intent_round_trip_has_the_exact_state_contract(self):
        intent = self.make_fixed_slot_intent()
        payload = intent.as_dict()

        self.assertEqual(
            set(payload),
            {
                "source_root_device",
                "source_root_inode",
                "source_parent_relative",
                "source_parent_device",
                "source_parent_inode",
                "source_name",
                "source_device",
                "source_inode",
                "destination_root_device",
                "destination_root_inode",
                "destination_parent_relative",
                "destination_parent_device",
                "destination_parent_inode",
                "destination_name",
            },
        )
        self.assertEqual(ArchiveIntent.from_dict(payload), intent)
        payload["extra"] = "unsafe"
        self.assert_archive_error(
            "archive_state_conflict",
            lambda: ArchiveIntent.from_dict(payload),
        )

    def test_root_parent_uses_the_single_dot_sentinel(self):
        intent = self.make_md5_intent()

        self.assertEqual(intent.source_parent_relative, ".")
        self.assertEqual(ArchiveIntent.from_dict(intent.as_dict()), intent)

    def test_relative_path_escape_is_rejected(self):
        (self.source_root / "safe").write_bytes(b"safe")
        (self.destination_root / "media").mkdir()
        cases = (
            ("../safe", "media/safe"),
            ("/safe", "media/safe"),
            ("safe", "media/../safe"),
            ("safe", "media\\safe"),
        )

        for source_relative, destination_relative in cases:
            with self.subTest(
                source_relative=source_relative,
                destination_relative=destination_relative,
            ):
                self.assert_archive_error(
                    "archive_state_conflict",
                    lambda: build_archive_intent(
                        str(self.source_root),
                        source_relative,
                        str(self.destination_root),
                        destination_relative,
                    ),
                )

    def test_child_fstat_io_error_closes_new_descriptor(self):
        intent = self.make_fixed_slot_intent()
        descriptor_directory = "/dev/fd"
        if not os.path.isdir(descriptor_directory):
            self.skipTest("descriptor inventory is unavailable")
        target_identity = (
            os.stat(str(self.source_root / "originals")).st_dev,
            os.stat(str(self.source_root / "originals")).st_ino,
        )
        real_fstat = os.fstat
        failed = [False]

        def fail_child_fstat(descriptor):
            opened_stat = real_fstat(descriptor)
            if not failed[0] and (
                opened_stat.st_dev,
                opened_stat.st_ino,
            ) == target_identity:
                failed[0] = True
                raise OSError(errno.EIO, "injected")
            return opened_stat

        before = set(os.listdir(descriptor_directory))
        with mock.patch(
            "django_images.services.media_archive.os.fstat",
            side_effect=fail_child_fstat,
        ):
            self.assert_archive_error(
                "archive_failed",
                lambda: open_archive_session(
                    str(self.source_root),
                    str(self.destination_root),
                    intent,
                    syscall_adapter=RecordingRenameNoReplaceAdapter(),
                ),
            )

        self.assertTrue(failed[0])
        self.assertEqual(set(os.listdir(descriptor_directory)), before)

    def test_default_adapter_has_no_ordinary_rename_fallback(self):
        adapter = LinuxRenameNoReplaceAdapter(platform="unsupported")

        self.assert_archive_error(
            "atomic_archive_unsupported",
            lambda: adapter.rename_noreplace(1, "source", 2, "destination"),
        )

    def test_linux_adapter_calls_renameat2_with_noreplace_flag(self):
        renameat2 = FakeCFunction()
        adapter = LinuxRenameNoReplaceAdapter(
            platform="linux",
            libc_factory=lambda *_args, **_kwargs: FakeLibc(renameat2),
        )

        adapter.rename_noreplace(10, "source", 20, "destination")

        self.assertEqual(len(renameat2.calls), 1)
        call = renameat2.calls[0]
        self.assertEqual(call[0], 10)
        self.assertEqual(call[1], b"source")
        self.assertEqual(call[2], 20)
        self.assertEqual(call[3], b"destination")
        self.assertEqual(call[4], 1)

    def test_linux_adapter_libc_load_distinguishes_capability_errno(self):
        cases = (
            (errno.ENOSYS, "atomic_archive_unsupported"),
            (errno.EINVAL, "atomic_archive_unsupported"),
            (errno.EOPNOTSUPP, "atomic_archive_unsupported"),
            (errno.ENOTSUP, "atomic_archive_unsupported"),
            (errno.EIO, "archive_failed"),
        )
        for error_number, expected_code in cases:
            with self.subTest(error_number=error_number):
                def fail_load(*_args, **_kwargs):
                    raise OSError(error_number, "injected")

                adapter = LinuxRenameNoReplaceAdapter(
                    platform="linux",
                    libc_factory=fail_load,
                )
                self.assert_archive_error(
                    expected_code,
                    lambda: adapter.rename_noreplace(
                        10, "source", 20, "destination"
                    ),
                )

    def test_linux_adapter_uname_distinguishes_capability_errno(self):
        cases = (
            (errno.ENOSYS, "atomic_archive_unsupported"),
            (errno.EINVAL, "atomic_archive_unsupported"),
            (errno.EOPNOTSUPP, "atomic_archive_unsupported"),
            (errno.ENOTSUP, "atomic_archive_unsupported"),
            (errno.EIO, "archive_failed"),
        )
        for error_number, expected_code in cases:
            with self.subTest(error_number=error_number):
                adapter = LinuxRenameNoReplaceAdapter(
                    platform="linux",
                    libc_factory=(
                        lambda *_args, **_kwargs: FakeLibc(None)
                    ),
                )
                with mock.patch(
                    "django_images.services.media_archive.os.uname",
                    side_effect=OSError(error_number, "injected"),
                ):
                    self.assert_archive_error(
                        expected_code,
                        lambda: adapter.rename_noreplace(
                            10, "source", 20, "destination"
                        ),
                    )

    def test_linux_adapter_normalizes_filesystem_unsupported_errno(self):
        for error_number in (
            errno.ENOSYS,
            errno.EINVAL,
            errno.EOPNOTSUPP,
        ):
            with self.subTest(error_number=error_number):
                renameat2 = FakeCFunction(-1, error_number)
                adapter = LinuxRenameNoReplaceAdapter(
                    platform="linux",
                    libc_factory=(
                        lambda *_args, **_kwargs: FakeLibc(renameat2)
                    ),
                )
                self.assert_archive_error(
                    "atomic_archive_unsupported",
                    lambda: adapter.rename_noreplace(
                        10, "source", 20, "destination"
                    ),
                )

    def test_fixed_slot_destination_rejects_non_fixed_slot_paths(self):
        invalid = (
            "image/original/by-md5/a/b/hash/file.png",
            "originals/not-a-uuid/original.png",
            "originals/11111111-1111-4111-8111-111111111111/current.png",
            "../original.png",
        )

        for relative_path in invalid:
            with self.subTest(relative_path=relative_path):
                self.assert_archive_error(
                    "archive_manifest_mismatch",
                    lambda: fixed_slot_destination_path(relative_path),
                )


class MediaArchiveGateTests(TestCase):
    def setUp(self):
        super(MediaArchiveGateTests, self).setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data_root = Path(os.path.realpath(temporary.name))
        self.source_root = self.data_root / "media-root"
        self.run_directory = self.data_root / RUN_ID
        self.source_root.mkdir()
        self.run_directory.mkdir(mode=0o700)
        os.chmod(str(self.run_directory), 0o700)
        self.uid = os.geteuid()
        self.gid = os.getegid()
        self.settings_override = self.settings(
            MEDIA_ROOT=str(self.source_root),
            PINRY_DATA_ROOT=str(self.data_root),
        )
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)

    def assert_archive_error(self, code, callback):
        with self.assertRaisesRegex(MediaArchiveError, "^{}$".format(code)):
            callback()

    def make_fixed_source(self, asset_uuid, content=b"legacy"):
        relative_path = "originals/{}/original.png".format(asset_uuid)
        source = self.source_root / relative_path
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(content)
        return relative_path

    def fixed_plan(self, image_id, source_relative):
        asset_uuid = source_relative.split("/")[1]
        source_stat = os.stat(str(self.source_root / source_relative))
        new_path = canonical_original_path(
            asset_uuid,
            "renamed-{}.png".format(image_id),
            ".png",
        )
        file_plan = AutoV2MigrationFile(
            kind="original",
            old_path=source_relative,
            new_path=new_path,
            operation="copy",
            size=source_stat.st_size,
            sha256=hashlib.sha256(
                (self.source_root / source_relative).read_bytes()
            ).hexdigest(),
            image_format="PNG",
            width=1,
            height=1,
            source_device=source_stat.st_dev,
            source_inode=source_stat.st_ino,
        )
        return AutoV2MigrationPlan(
            image_id=image_id,
            asset_uuid=asset_uuid,
            original_filename="renamed-{}.png".format(image_id),
            image_width=1,
            image_height=1,
            generation="fixed_slot",
            files=(file_plan,),
            thumbnail_rows=(),
            copy_required_bytes=source_stat.st_size,
        )

    def django_normalized_fixed_plan(self, image_id, asset_uuid):
        original_filename = "스크린샷 2026-08-20 21.23.05.png"
        new_path = canonical_original_path(
            asset_uuid,
            original_filename,
            ".png",
        )
        parent, leaf = new_path.rsplit("/", 1)
        old_path = "{}/{}".format(parent, get_valid_filename(leaf))
        old_file = self.source_root / old_path
        old_file.parent.mkdir(parents=True, exist_ok=True)
        old_file.write_bytes(b"historical-django-normalized-original")
        new_file = self.source_root / new_path
        new_file.write_bytes(old_file.read_bytes())
        old_stat = os.stat(str(old_file))
        files = [AutoV2MigrationFile(
            kind="original",
            old_path=old_path,
            new_path=new_path,
            operation="copy",
            size=old_stat.st_size,
            sha256=hashlib.sha256(old_file.read_bytes()).hexdigest(),
            image_format="PNG",
            width=1,
            height=1,
            source_device=old_stat.st_dev,
            source_inode=old_stat.st_ino,
        )]
        thumbnail_rows = []
        image = Image.objects.create(
            image=new_path,
            asset_uuid=asset_uuid,
            original_filename=original_filename,
            width=1,
            height=1,
        )
        for size in sorted(DERIVATIVE_NAMES):
            derivative_path = canonical_derivative_path(
                asset_uuid, size, ".png"
            )
            derivative_file = self.source_root / derivative_path
            derivative_file.parent.mkdir(parents=True, exist_ok=True)
            derivative_file.write_bytes(size.encode("ascii"))
            derivative_stat = os.stat(str(derivative_file))
            thumbnail = Thumbnail.objects.create(
                original=image,
                image=derivative_path,
                size=size,
                width=1,
                height=1,
            )
            files.append(AutoV2MigrationFile(
                kind="derivative",
                old_path=derivative_path,
                new_path=derivative_path,
                operation="verify",
                size=derivative_stat.st_size,
                sha256=hashlib.sha256(
                    derivative_file.read_bytes()
                ).hexdigest(),
                image_format="PNG",
                width=1,
                height=1,
                source_device=derivative_stat.st_dev,
                source_inode=derivative_stat.st_ino,
                thumbnail_id=thumbnail.pk,
                derivative_size=size,
            ))
            thumbnail_rows.append((
                thumbnail.pk,
                size,
                derivative_path,
                1,
                1,
            ))
        return AutoV2MigrationPlan(
            image_id=image_id,
            asset_uuid=asset_uuid,
            original_filename=original_filename,
            image_width=1,
            image_height=1,
            generation="fixed_slot",
            files=tuple(files),
            thumbnail_rows=tuple(thumbnail_rows),
            copy_required_bytes=old_stat.st_size,
        )

    def named_plan(self, image_id, asset_uuid=None):
        asset_uuid = str(uuid.uuid4()) if asset_uuid is None else asset_uuid
        path = canonical_original_path(asset_uuid, "current.png", ".png")
        file_plan = AutoV2MigrationFile(
            kind="original",
            old_path=path,
            new_path=path,
            operation="verify",
            size=1,
            sha256="1" * 64,
            image_format="PNG",
            width=1,
            height=1,
            source_device=os.stat(str(self.source_root)).st_dev,
            source_inode=os.stat(str(self.source_root)).st_ino,
        )
        return AutoV2MigrationPlan(
            image_id=image_id,
            asset_uuid=asset_uuid,
            original_filename="current.png",
            image_width=1,
            image_height=1,
            generation="named_canonical",
            files=(file_plan,),
            thumbnail_rows=(),
            copy_required_bytes=0,
        )

    def direct_md5_plan(self, image_id, source_relative):
        asset_uuid = "11111111-1111-4111-8111-111111111111"
        source_path = self.source_root / source_relative
        source_stat = os.stat(str(source_path))
        archive_root_stat = os.stat(
            str(self.source_root / source_relative.split("/", 1)[0])
        )
        new_path = canonical_original_path(
            asset_uuid,
            "legacy-{}.png".format(image_id),
            ".png",
        )
        file_plan = AutoV2MigrationFile(
            kind="original",
            old_path=source_relative,
            new_path=new_path,
            operation="copy",
            size=source_stat.st_size,
            sha256=hashlib.sha256(source_path.read_bytes()).hexdigest(),
            image_format="PNG",
            width=1,
            height=1,
            source_device=source_stat.st_dev,
            source_inode=source_stat.st_ino,
            archive_root_device=archive_root_stat.st_dev,
            archive_root_inode=archive_root_stat.st_ino,
        )
        return AutoV2MigrationPlan(
            image_id=image_id,
            asset_uuid=asset_uuid,
            original_filename="legacy-{}.png".format(image_id),
            image_width=1,
            image_height=1,
            generation="md5_legacy",
            files=(file_plan,),
            thumbnail_rows=(),
            copy_required_bytes=source_stat.st_size,
        )

    def prefixed_md5_plan(self, image_id, source_relative):
        asset_uuid = "22222222-2222-4222-8222-222222222222"
        source_path = self.source_root / source_relative
        source_stat = os.stat(str(source_path))
        archive_root_stat = os.stat(str(self.source_root / "image"))
        file_plan = AutoV2MigrationFile(
            kind="original",
            old_path=source_relative,
            new_path=canonical_original_path(
                asset_uuid,
                "legacy-{}.png".format(image_id),
                ".png",
            ),
            operation="copy",
            size=source_stat.st_size,
            sha256=hashlib.sha256(source_path.read_bytes()).hexdigest(),
            image_format="PNG",
            width=1,
            height=1,
            source_device=source_stat.st_dev,
            source_inode=source_stat.st_ino,
            archive_root_device=archive_root_stat.st_dev,
            archive_root_inode=archive_root_stat.st_ino,
        )
        return AutoV2MigrationPlan(
            image_id=image_id,
            asset_uuid=asset_uuid,
            original_filename="legacy-{}.png".format(image_id),
            image_width=1,
            image_height=1,
            generation="md5_legacy",
            files=(file_plan,),
            thumbnail_rows=(),
            copy_required_bytes=source_stat.st_size,
        )

    def legacy_prefixed_md5_plan(self, image_id, source_relative):
        plan = self.prefixed_md5_plan(image_id, source_relative)
        return replace(
            plan,
            files=tuple(
                replace(
                    file_plan,
                    archive_root_device=None,
                    archive_root_inode=None,
                )
                for file_plan in plan.files
            ),
        )

    def write_manifest(self, plans, terminal=True):
        with AutoV2ManifestLog.open(
            str(self.run_directory),
            AUTO_V2_MANIFEST_FILENAME,
            RUN_ID,
            self.uid,
            self.gid,
        ) as manifest:
            for plan in plans:
                manifest.record_plan(plan)
            manifest.record_plan_complete(plans)
            if terminal:
                for plan in plans:
                    event_name = (
                        "already_current"
                        if plan.generation == "named_canonical"
                        else "committed"
                    )
                    manifest.record_result(event_name, plan.image_id)

    def write_legacy_manifest(self, plans, terminal=True):
        """현재 writer 검증을 거치지 않은 구 v2 event를 재현한다."""
        common = {
            "format_version": 2,
            "target_signature": AUTO_V2_TARGET_SIGNATURE,
            "run_id": RUN_ID,
        }
        events = [
            dict(common, event="planned", plan=plan.as_dict())
            for plan in plans
        ]
        counts = {
            generation: sum(
                1 for plan in plans if plan.generation == generation
            )
            for generation in (
                "md5_legacy",
                "fixed_slot",
                "named_canonical",
            )
        }
        events.append(dict(
            common,
            event="plan_complete",
            image_count=len(plans),
            md5_legacy=counts["md5_legacy"],
            fixed_slot=counts["fixed_slot"],
            named_canonical=counts["named_canonical"],
            copy_required_bytes=sum(
                plan.copy_required_bytes for plan in plans
            ),
        ))
        lines = [
            json.dumps(
                event,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8") + b"\n"
            for event in events
        ]
        plan_sha256 = hashlib.sha256(b"".join(lines)).hexdigest()
        if terminal:
            for plan in plans:
                event_name = (
                    "already_current"
                    if plan.generation == "named_canonical"
                    else "committed"
                )
                lines.append(json.dumps(
                    dict(
                        common,
                        event=event_name,
                        image_id=plan.image_id,
                        plan_sha256=plan_sha256,
                    ),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8") + b"\n")
        manifest_path = (
            self.run_directory / AUTO_V2_MANIFEST_FILENAME
        )
        manifest_path.write_bytes(b"".join(lines))
        os.chmod(str(manifest_path), 0o600)

    def archiver(self):
        return LegacyMediaArchive(
            str(self.source_root),
            str(self.run_directory),
            str(self.run_directory),
            AUTO_V2_MANIFEST_FILENAME,
            RUN_ID,
            self.uid,
            self.gid,
        )

    def prepare_plan(
        self, has_md5=False, has_direct=False, schema_only=False, **kwargs
    ):
        return self.archiver().prepare(
            has_media_image_directory=has_md5,
            has_pinry_direct_md5_directory=has_direct,
            schema_only=schema_only,
            **kwargs
        )

    def prepare(self, has_md5=False, schema_only=False):
        return self.prepare_plan(
            has_md5=has_md5, schema_only=schema_only
        ).intents

    def test_archive_freezes_orphan_direct_roots_without_manifest_rows(self):
        orphan = (
            self.source_root / "a" / "b"
            / "ab0123456789abcdef0123456789abcd" / "orphan.png"
        )
        orphan.parent.mkdir(parents=True)
        orphan.write_bytes(b"orphan")
        self.write_manifest((self.named_plan(1),))

        plan = self.prepare_plan(has_direct=True)

        self.assertEqual(
            tuple(
                (intent.source_name, intent.destination_name)
                for intent in plan.intents
            ),
            (("a", "a"),),
        )

    def test_django_normalized_source_is_prepared_archived_and_recovered(self):
        asset_uuid = "11111111-1111-4111-8111-111111111111"
        migration_plan = self.django_normalized_fixed_plan(1, asset_uuid)
        source = migration_plan.files[0].old_path
        canonical = migration_plan.files[0].new_path
        destination = "media/fixed-slot-originals/{}".format(source)
        self.write_manifest((migration_plan,))
        archiver = self.archiver()

        archive_plan = archiver.prepare()

        self.assertEqual(len(archive_plan.intents), 1)
        intent = archive_plan.intents[0]
        self.assertEqual(
            "{}/{}".format(
                intent.source_parent_relative, intent.source_name
            ),
            source,
        )
        self.assertEqual(
            "{}/{}".format(
                intent.destination_parent_relative,
                intent.destination_name,
            ),
            destination,
        )

        outcome = archiver.converge(
            archive_plan,
            syscall_adapter=RecordingRenameNoReplaceAdapter(),
        )

        self.assertEqual(outcome.results[0].status, "archived")
        self.assertFalse((self.source_root / source).exists())
        self.assertTrue((self.source_root / canonical).is_file())
        self.assertEqual(
            (self.run_directory / destination).read_bytes(),
            b"historical-django-normalized-original",
        )

        resumed_plan = archiver.prepare(recover_completed_fixed=True)
        resumed = archiver.converge(
            resumed_plan,
            syscall_adapter=RecordingRenameNoReplaceAdapter(),
        )

        self.assertTrue(all(
            item["complete"] for item in resumed_plan.progress["items"]
        ))
        self.assertEqual(resumed.results[0].status, "recovered")

    def test_archive_rejects_source_root_other_than_effective_media_root(self):
        source = self.make_fixed_source(
            "11111111-1111-4111-8111-111111111111"
        )
        self.write_manifest((self.fixed_plan(1, source),))
        alternate_root = self.data_root / "alternate-media-root"
        alternate_source = alternate_root / source
        alternate_source.parent.mkdir(parents=True)
        alternate_source.write_bytes(b"alternate")
        archiver = LegacyMediaArchive(
            str(alternate_root),
            str(self.run_directory),
            str(self.run_directory),
            AUTO_V2_MANIFEST_FILENAME,
            RUN_ID,
            self.uid,
            self.gid,
        )
        adapter = RecordingRenameNoReplaceAdapter()

        def archive():
            plan = archiver.prepare()
            archiver.converge(plan, syscall_adapter=adapter)

        self.assert_archive_error("archive_state_conflict", archive)
        self.assertEqual(adapter.calls, [])
        self.assertTrue(alternate_source.exists())
        self.assertFalse((self.run_directory / "media").exists())

    def test_archive_rejects_destination_root_other_than_run_directory(self):
        source = self.make_fixed_source(
            "11111111-1111-4111-8111-111111111111"
        )
        self.write_manifest((self.fixed_plan(1, source),))
        alternate_destination = self.data_root / "alternate-run"
        alternate_destination.mkdir(mode=0o700)
        archiver = LegacyMediaArchive(
            str(self.source_root),
            str(alternate_destination),
            str(self.run_directory),
            AUTO_V2_MANIFEST_FILENAME,
            RUN_ID,
            self.uid,
            self.gid,
        )
        adapter = RecordingRenameNoReplaceAdapter()

        def archive():
            plan = archiver.prepare()
            archiver.converge(plan, syscall_adapter=adapter)

        self.assert_archive_error("archive_state_conflict", archive)
        self.assertEqual(adapter.calls, [])
        self.assertTrue((self.source_root / source).exists())
        self.assertFalse((alternate_destination / "media").exists())

    def test_manifest_close_io_error_is_normalized(self):
        self.write_manifest(())
        real_close = AutoV2ManifestLog.close

        def close_then_fail(manifest):
            real_close(manifest)
            raise OSError(errno.EIO, "injected")

        with mock.patch.object(
            AutoV2ManifestLog,
            "close",
            autospec=True,
            side_effect=close_then_fail,
        ):
            self.assert_archive_error("archive_failed", self.prepare)

    def test_database_gate_requires_zero_image_root_references(self):
        legacy_image = Image.objects.create(
            image="image/original/by-md5/a/b/hash/file.png",
            asset_uuid=uuid.uuid4(),
            original_filename="legacy.png",
            width=1,
            height=1,
        )

        self.assert_archive_error(
            "legacy_media_still_referenced",
            validate_no_legacy_media_references,
        )

        legacy_image.image.name = canonical_original_path(
            legacy_image.asset_uuid, "legacy.png", ".png"
        )
        Image.objects.filter(pk=legacy_image.pk).update(
            image=legacy_image.image.name
        )
        thumbnail = Thumbnail.objects.create(
            original=legacy_image,
            image="image/thumbnail/by-md5/a/b/hash/thumb.png",
            size="thumbnail",
            width=1,
            height=1,
        )
        self.assert_archive_error(
            "legacy_media_still_referenced",
            validate_no_legacy_media_references,
        )

        Thumbnail.objects.filter(pk=thumbnail.pk).update(
            image="derivatives/{}/thumbnail.png".format(
                legacy_image.asset_uuid
            )
        )
        self.assertTrue(validate_no_legacy_media_references())

    def test_database_gate_rejects_pinry_direct_md5_references(self):
        Image.objects.create(
            image="a/b/ab0123456789abcdef0123456789abcd/photo.png",
            asset_uuid=uuid.uuid4(),
            original_filename="photo.png",
            width=1,
            height=1,
        )

        self.assert_archive_error(
            "legacy_media_still_referenced",
            validate_no_legacy_media_references,
        )

    def test_database_gate_rejects_exact_direct_root_reference(self):
        Image.objects.create(
            image="a",
            asset_uuid=uuid.uuid4(),
            original_filename="legacy.png",
            width=1,
            height=1,
        )

        self.assert_archive_error(
            "legacy_media_still_referenced",
            validate_no_legacy_media_references,
        )

    def test_database_gate_rejects_manifest_fixed_slot_reference(self):
        source = self.make_fixed_source(
            "11111111-1111-4111-8111-111111111111"
        )
        Image.objects.create(
            image=source,
            asset_uuid=uuid.uuid4(),
            original_filename="legacy.png",
            width=1,
            height=1,
        )

        self.assert_archive_error(
            "legacy_media_still_referenced",
            lambda: validate_no_legacy_media_references(
                fixed_slot_sources=(source,)
            ),
        )

    def test_prepare_archives_all_pinry_direct_md5_root_directories(self):
        source = "a/b/ab0123456789abcdef0123456789abcd/photo.png"
        source_file = self.source_root / source
        source_file.parent.mkdir(parents=True)
        source_file.write_bytes(b"legacy")
        orphan = (
            self.source_root
            / "f"
            / "0"
            / "f00123456789abcdef0123456789abcd"
            / "orphan.bin"
        )
        orphan.parent.mkdir(parents=True)
        orphan.write_bytes(b"orphan")
        self.write_manifest((self.direct_md5_plan(1, source),))

        authority = load_auto_v2_archive_authority(
            str(self.run_directory),
            AUTO_V2_MANIFEST_FILENAME,
            RUN_ID,
            self.uid,
            self.gid,
        )
        file_plan = authority.direct_files[0]
        root_stat = os.stat(str(self.source_root / "a"))
        source_stat = os.stat(str(source_file))
        self.assertEqual(
            authority.direct_root_identities,
            (("a", root_stat.st_dev, root_stat.st_ino),),
        )
        self.assertEqual(
            (file_plan.source_device, file_plan.source_inode),
            (source_stat.st_dev, source_stat.st_ino),
        )
        self.assertEqual(file_plan.size, source_stat.st_size)
        self.assertEqual(
            file_plan.sha256,
            hashlib.sha256(source_file.read_bytes()).hexdigest(),
        )
        root_directory = open_verified_media_root(str(self.source_root))
        receipt = open_verified_media_file(root_directory, source)
        try:
            self.assertEqual(
                (
                    receipt.parent_directory.directory_stats[0].st_dev,
                    receipt.parent_directory.directory_stats[0].st_ino,
                ),
                (root_stat.st_dev, root_stat.st_ino),
            )
        finally:
            receipt.close()
            root_directory.close()

        plan = self.prepare_plan()
        intents = plan.intents

        source_paths = tuple(intent.source_name for intent in intents)
        destination_paths = tuple(
            "{}/{}".format(
                intent.destination_parent_relative,
                intent.destination_name,
            )
            for intent in intents
        )
        self.assertEqual(source_paths, ("a", "f"))
        self.assertEqual(destination_paths, ("media/a", "media/f"))
        self.assert_archive_error(
            "archive_state_conflict",
            lambda: _validate_archive_intent_layout(
                intents[:1], (), ("a", "f")
            ),
        )

        outcome = self.archiver().converge(
            plan,
            syscall_adapter=RecordingRenameNoReplaceAdapter(),
        )

        self.assertEqual(
            tuple(result.status for result in outcome.results),
            ("archived", "archived"),
        )
        self.assertFalse((self.source_root / "a").exists())
        self.assertFalse((self.source_root / "f").exists())
        self.assertEqual(
            (self.run_directory / "media" / "a" / source.split("/", 1)[1])
            .read_bytes(),
            b"legacy",
        )
        self.assertEqual(
            (
                self.run_directory
                / "media"
                / "f"
                / "0"
                / "f00123456789abcdef0123456789abcd"
                / "orphan.bin"
            ).read_bytes(),
            b"orphan",
        )

    def test_prepare_rejects_replaced_fixed_slot_source(self):
        source = self.make_fixed_source(
            "11111111-1111-4111-8111-111111111111", b"legacy"
        )
        self.write_manifest((self.fixed_plan(1, source),))
        source_path = self.source_root / source
        source_path.unlink()
        source_path.write_bytes(b"forged")

        self.assert_archive_error("archive_manifest_mismatch", self.prepare)

    def test_prepare_rejects_replaced_prefixed_md5_source(self):
        source = (
            "image/original/by-md5/a/b/"
            "ab0123456789abcdef0123456789abcd/photo.png"
        )
        source_path = self.source_root / source
        source_path.parent.mkdir(parents=True)
        source_path.write_bytes(b"legacy")
        self.write_manifest((self.prefixed_md5_plan(1, source),))
        shutil.rmtree(str(self.source_root / "image"))
        source_path.parent.mkdir(parents=True)
        source_path.write_bytes(b"forged")

        self.assert_archive_error(
            "archive_manifest_mismatch",
            lambda: self.prepare(has_md5=True),
        )

    def test_prepare_rejects_replaced_prefixed_md5_root_with_same_file(self):
        source = (
            "image/original/by-md5/a/b/"
            "ab0123456789abcdef0123456789abcd/photo.png"
        )
        source_path = self.source_root / source
        source_path.parent.mkdir(parents=True)
        source_path.write_bytes(b"legacy")
        orphan = self.source_root / "image" / "orphan.bin"
        orphan.write_bytes(b"orphan")
        self.write_manifest((self.prefixed_md5_plan(1, source),))

        old_root = self.source_root / "image-old"
        (self.source_root / "image").rename(old_root)
        source_path.parent.mkdir(parents=True)
        (old_root / source.split("/", 1)[1]).rename(source_path)

        self.assert_archive_error(
            "archive_manifest_mismatch",
            lambda: self.prepare(has_md5=True),
        )

    def test_current_writer_rejects_prefixed_manifest_without_root_authority(
        self,
    ):
        source = (
            "image/original/by-md5/a/b/"
            "ab0123456789abcdef0123456789abcd/photo.png"
        )
        source_path = self.source_root / source
        source_path.parent.mkdir(parents=True)
        source_path.write_bytes(b"legacy")

        with self.assertRaisesRegex(
            CommandError,
            "^invalid_auto_v2_manifest$",
        ):
            self.write_manifest((self.legacy_prefixed_md5_plan(1, source),))

    def test_public_append_rejects_prefixed_plan_without_root_authority(self):
        source = (
            "image/original/by-md5/a/b/"
            "ab0123456789abcdef0123456789abcd/photo.png"
        )
        source_path = self.source_root / source
        source_path.parent.mkdir(parents=True)
        source_path.write_bytes(b"legacy")
        manifest_path = (
            self.run_directory / AUTO_V2_MANIFEST_FILENAME
        )

        with AutoV2ManifestLog.open(
            str(self.run_directory),
            AUTO_V2_MANIFEST_FILENAME,
            RUN_ID,
            self.uid,
            self.gid,
        ) as manifest:
            with self.assertRaisesRegex(
                CommandError,
                "^invalid_auto_v2_manifest$",
            ):
                manifest.append({
                    "event": "planned",
                    "plan": self.legacy_prefixed_md5_plan(
                        1, source
                    ).as_dict(),
                })

        self.assertEqual(manifest_path.read_bytes(), b"")

    def test_public_append_rejects_unrecognized_nested_plan_fields(self):
        source = "a/b/ab0123456789abcdef0123456789abcd/photo.png"
        source_path = self.source_root / source
        source_path.parent.mkdir(parents=True)
        source_path.write_bytes(b"legacy")
        plan = self.direct_md5_plan(1, source)
        manifest_path = (
            self.run_directory / AUTO_V2_MANIFEST_FILENAME
        )

        with AutoV2ManifestLog.open(
            str(self.run_directory),
            AUTO_V2_MANIFEST_FILENAME,
            RUN_ID,
            self.uid,
            self.gid,
        ) as manifest:
            for location in ("plan", "file"):
                with self.subTest(location=location):
                    payload = json.loads(json.dumps(plan.as_dict()))
                    if location == "plan":
                        payload["unrecognized"] = True
                    else:
                        payload["files"][0]["unrecognized"] = True
                    with self.assertRaisesRegex(
                        CommandError,
                        "^invalid_auto_v2_manifest$",
                    ):
                        manifest.append({
                            "event": "planned",
                            "plan": payload,
                        })

        self.assertEqual(manifest_path.read_bytes(), b"")

    def test_legacy_prefixed_registry_state_without_authority_fails_closed(
        self,
    ):
        source = (
            "image/original/by-md5/a/b/"
            "ab0123456789abcdef0123456789abcd/photo.png"
        )
        source_path = self.source_root / source
        source_path.parent.mkdir(parents=True)
        source_path.write_bytes(b"legacy")
        self.write_legacy_manifest((
            self.legacy_prefixed_md5_plan(1, source),
        ))

        self.assert_archive_error(
            "archive_manifest_mismatch",
            lambda: self.prepare_plan(has_md5=True),
        )

    def test_legacy_prefixed_archive_intent_resumes_with_frozen_identity(self):
        source = (
            "image/original/by-md5/a/b/"
            "ab0123456789abcdef0123456789abcd/photo.png"
        )
        source_path = self.source_root / source
        source_path.parent.mkdir(parents=True)
        source_path.write_bytes(b"legacy")
        self.write_legacy_manifest((
            self.legacy_prefixed_md5_plan(1, source),
        ))
        (self.run_directory / "media").mkdir()
        intent = build_archive_intent(
            str(self.source_root),
            "image",
            str(self.run_directory),
            "media/image",
        )
        progress = {
            "items": [{"intent": intent.as_dict(), "complete": False}],
        }

        resumed = self.prepare_plan(progress=progress)

        self.assertEqual(resumed.intents, (intent,))
        self.assertEqual(resumed.progress, progress)

    def test_legacy_completed_fixed_only_archive_recovers(self):
        source = self.make_fixed_source(
            "33333333-3333-4333-8333-333333333333",
            b"legacy-fixed",
        )
        self.write_manifest((self.fixed_plan(1, source),))
        archiver = self.archiver()
        initial = archiver.prepare()
        archiver.converge(
            initial,
            syscall_adapter=RecordingRenameNoReplaceAdapter(),
        )

        recovered = archiver.prepare(recover_completed_fixed=True)
        outcome = archiver.converge(
            recovered,
            syscall_adapter=RecordingRenameNoReplaceAdapter(),
        )

        self.assertTrue(all(
            item["complete"] for item in recovered.progress["items"]
        ))
        self.assertEqual(
            tuple(result.status for result in outcome.results),
            ("recovered",),
        )

    def test_legacy_completed_fixed_recovery_rejects_archived_image_root(
        self,
    ):
        source = self.make_fixed_source(
            "44444444-4444-4444-8444-444444444444",
            b"legacy-fixed",
        )
        orphan = self.source_root / "image" / "orphan.bin"
        orphan.parent.mkdir()
        orphan.write_bytes(b"orphan")
        self.write_manifest((self.fixed_plan(1, source),))
        archiver = self.archiver()
        initial = archiver.prepare(has_media_image_directory=True)
        archiver.converge(
            initial,
            syscall_adapter=RecordingRenameNoReplaceAdapter(),
        )

        self.assert_archive_error(
            "archive_state_conflict",
            lambda: archiver.prepare(recover_completed_fixed=True),
        )

    def test_legacy_completed_fixed_recovery_rejects_archived_direct_root(
        self,
    ):
        source = self.make_fixed_source(
            "55555555-5555-4555-8555-555555555555",
            b"legacy-fixed",
        )
        self.write_manifest((self.fixed_plan(1, source),))
        archiver = self.archiver()
        initial = archiver.prepare()
        archiver.converge(
            initial,
            syscall_adapter=RecordingRenameNoReplaceAdapter(),
        )
        (self.run_directory / "media" / "a").mkdir()

        self.assert_archive_error(
            "archive_state_conflict",
            lambda: archiver.prepare(recover_completed_fixed=True),
        )

    def test_direct_manifest_without_root_authority_is_rejected(self):
        source = "a/b/ab0123456789abcdef0123456789abcd/photo.png"
        source_path = self.source_root / source
        source_path.parent.mkdir(parents=True)
        source_path.write_bytes(b"legacy")
        plan = self.direct_md5_plan(1, source)
        legacy_plan = replace(
            plan,
            files=tuple(
                replace(
                    file_plan,
                    archive_root_device=None,
                    archive_root_inode=None,
                )
                for file_plan in plan.files
            ),
        )

        with self.assertRaisesRegex(
            CommandError,
            "^invalid_auto_v2_manifest$",
        ):
            self.write_manifest((legacy_plan,))

    def test_legacy_prefixed_manifest_rejects_mixed_root_authority(self):
        first = (
            "image/original/by-md5/a/b/"
            "ab0123456789abcdef0123456789abcd/first.png"
        )
        second = (
            "image/original/by-md5/c/d/"
            "cd0123456789abcdef0123456789abcd/second.png"
        )
        for path, content in ((first, b"first"), (second, b"second")):
            source = self.source_root / path
            source.parent.mkdir(parents=True)
            source.write_bytes(content)
        self.write_legacy_manifest((
            self.legacy_prefixed_md5_plan(1, first),
            self.prefixed_md5_plan(2, second),
        ))

        with self.assertRaisesRegex(
            CommandError,
            "^manifest_plan_mismatch$",
        ):
            load_auto_v2_archive_authority(
                str(self.run_directory),
                AUTO_V2_MANIFEST_FILENAME,
                RUN_ID,
                self.uid,
                self.gid,
            )

    def test_converge_rejects_direct_file_changed_after_intent(self):
        source = "a/b/ab0123456789abcdef0123456789abcd/photo.png"
        source_file = self.source_root / source
        source_file.parent.mkdir(parents=True)
        source_file.write_bytes(b"legacy")
        self.write_manifest((self.direct_md5_plan(1, source),))
        plan = self.prepare_plan()
        original_inode = source_file.stat().st_ino
        source_file.write_bytes(b"forged")
        self.assertEqual(source_file.stat().st_ino, original_inode)

        self.assert_archive_error(
            "archive_manifest_mismatch",
            lambda: self.archiver().converge(
                plan,
                syscall_adapter=RecordingRenameNoReplaceAdapter(),
            ),
        )

    def test_resume_rejects_direct_root_created_after_intent(self):
        source = "a/b/ab0123456789abcdef0123456789abcd/photo.png"
        source_file = self.source_root / source
        source_file.parent.mkdir(parents=True)
        source_file.write_bytes(b"legacy")
        self.write_manifest((self.direct_md5_plan(1, source),))
        plan = self.prepare_plan()
        late = (
            self.source_root / "f" / "0"
            / "f00123456789abcdef0123456789abcd" / "late.png"
        )
        late.parent.mkdir(parents=True)
        late.write_bytes(b"late")

        self.assert_archive_error(
            "archive_state_conflict",
            lambda: self.archiver().prepare(progress=plan.progress),
        )

    def test_resume_rejects_removed_orphan_direct_root_intent(self):
        source = "a/b/ab0123456789abcdef0123456789abcd/photo.png"
        source_file = self.source_root / source
        source_file.parent.mkdir(parents=True)
        source_file.write_bytes(b"legacy")
        orphan = (
            self.source_root / "f" / "0"
            / "f00123456789abcdef0123456789abcd" / "orphan.png"
        )
        orphan.parent.mkdir(parents=True)
        orphan.write_bytes(b"orphan")
        self.write_manifest((self.direct_md5_plan(1, source),))
        plan = self.prepare_plan()
        shortened_progress = {"items": plan.progress["items"][:1]}

        self.assert_archive_error(
            "archive_state_conflict",
            lambda: self.archiver().prepare(progress=shortened_progress),
        )

    def test_prepare_rejects_manifest_digest_other_than_frozen_state(self):
        self.write_manifest((self.named_plan(1),))
        summary = load_completed_auto_v2_summary(
            str(self.run_directory),
            AUTO_V2_MANIFEST_FILENAME,
            RUN_ID,
            self.uid,
            self.gid,
        )

        self.assert_archive_error(
            "archive_manifest_mismatch",
            lambda: self.archiver().prepare(
                expected_plan_sha256="0" * 64,
                expected_manifest_sha256=summary.manifest_sha256,
            ),
        )

    def test_prepare_rejects_replaced_pinry_direct_root(self):
        source = "a/b/ab0123456789abcdef0123456789abcd/photo.png"
        source_file = self.source_root / source
        source_file.parent.mkdir(parents=True)
        source_file.write_bytes(b"legacy")
        self.write_manifest((self.direct_md5_plan(1, source),))
        original_root = self.source_root / "a"
        original_root.rename(self.source_root / "a-old")
        original_root.mkdir()

        self.assert_archive_error(
            "archive_manifest_mismatch",
            self.prepare,
        )

    def test_prepare_rejects_non_directory_pinry_direct_root(self):
        source = "a/b/ab0123456789abcdef0123456789abcd/photo.png"
        source_file = self.source_root / source
        source_file.parent.mkdir(parents=True)
        source_file.write_bytes(b"legacy")
        plan = self.direct_md5_plan(1, source)
        self.write_manifest((plan,))

        for kind in ("file", "symlink"):
            with self.subTest(kind=kind):
                root_entry = self.source_root / "a"
                if root_entry.is_symlink() or root_entry.is_file():
                    root_entry.unlink()
                elif root_entry.exists():
                    shutil.rmtree(str(root_entry))
                if kind == "file":
                    root_entry.write_bytes(b"unsafe")
                else:
                    (self.source_root / "target").mkdir(exist_ok=True)
                    root_entry.symlink_to("target")

                self.assert_archive_error(
                    "archive_manifest_mismatch", self.prepare
                )

    def test_prepare_freezes_md5_first_then_sorted_fixed_slot_intents(self):
        later = self.make_fixed_source(
            "22222222-2222-4222-8222-222222222222", b"later"
        )
        earlier = self.make_fixed_source(
            "11111111-1111-4111-8111-111111111111", b"earlier"
        )
        (self.source_root / "image").mkdir()
        self.write_manifest((
            self.fixed_plan(2, later),
            self.named_plan(3),
            self.fixed_plan(1, earlier),
        ))

        intents = self.prepare(has_md5=True)

        source_paths = tuple(
            intent.source_name
            if intent.source_parent_relative == "."
            else "{}/{}".format(
                intent.source_parent_relative, intent.source_name
            )
            for intent in intents
        )
        destination_paths = tuple(
            "{}/{}".format(
                intent.destination_parent_relative,
                intent.destination_name,
            )
            for intent in intents
        )
        self.assertEqual(source_paths, ("image", earlier, later))
        self.assertEqual(
            destination_paths,
            (
                "media/image",
                fixed_slot_destination_path(earlier),
                fixed_slot_destination_path(later),
            ),
        )
        self.assertFalse((self.run_directory / "media" / "image").exists())

    def test_prepare_requires_a_complete_terminal_manifest(self):
        source = self.make_fixed_source(
            "11111111-1111-4111-8111-111111111111"
        )
        self.write_manifest((self.fixed_plan(1, source),), terminal=False)

        self.assert_archive_error(
            "auto_v2_plan_incomplete",
            self.prepare,
        )

    def test_prepare_checks_database_gate_before_freezing_intents(self):
        source = self.make_fixed_source(
            "11111111-1111-4111-8111-111111111111"
        )
        self.write_manifest((self.fixed_plan(1, source),))
        Image.objects.create(
            image="image/original/by-md5/a/b/hash/file.png",
            asset_uuid=uuid.uuid4(),
            original_filename="legacy.png",
            width=1,
            height=1,
        )

        self.assert_archive_error(
            "legacy_media_still_referenced",
            self.prepare,
        )
        self.assertFalse((self.run_directory / "media").exists())

    def test_schema_only_prepare_and_converge_do_not_open_or_archive_media(self):
        adapter = RecordingRenameNoReplaceAdapter()

        with mock.patch(
            "django_images.services.media_archive.open_archive_session"
        ) as session_opener, mock.patch(
            "django_images.services.media_archive."
            "load_auto_v2_archive_authority"
        ) as manifest_loader, mock.patch(
            "django_images.services.media_archive."
            "validate_no_legacy_media_references"
        ) as database_gate:
            plan = self.prepare_plan(schema_only=True)
            outcome = self.archiver().converge(
                plan,
                syscall_adapter=adapter,
            )

        self.assertEqual(plan.intents, ())
        self.assertEqual(outcome.results, ())
        self.assertIsNone(outcome.progress)
        self.assertEqual(adapter.calls, [])
        session_opener.assert_not_called()
        manifest_loader.assert_not_called()
        database_gate.assert_not_called()

    def test_destination_parent_fsync_failure_is_archive_failed(self):
        source = self.make_fixed_source(
            "11111111-1111-4111-8111-111111111111"
        )
        self.write_manifest((self.fixed_plan(1, source),))

        with mock.patch(
            "django_images.services.media_archive.os.fsync",
            side_effect=OSError(errno.EIO, "injected"),
        ):
            self.assert_archive_error("archive_failed", self.prepare)

    def test_progress_requires_the_exact_frozen_intent_order_and_set(self):
        first = self.make_fixed_source(
            "11111111-1111-4111-8111-111111111111", b"first"
        )
        second = self.make_fixed_source(
            "22222222-2222-4222-8222-222222222222", b"second"
        )
        self.write_manifest((
            self.fixed_plan(1, first),
            self.fixed_plan(2, second),
        ))
        plan = self.prepare_plan()
        progress = plan.progress

        self.assertEqual(
            tuple(item["complete"] for item in progress["items"]),
            (False, False),
        )
        invalid_items = (
            progress["items"][:-1],
            progress["items"] + [progress["items"][0]],
            list(reversed(progress["items"])),
            [progress["items"][0], progress["items"][0]],
        )
        for items in invalid_items:
            with self.subTest(items=items):
                self.assert_archive_error(
                    "archive_state_conflict",
                    lambda items=items: self.archiver().prepare(
                        progress={"items": items}
                    ),
                )

    def test_progress_never_regresses_complete_to_incomplete(self):
        source = self.make_fixed_source(
            "11111111-1111-4111-8111-111111111111"
        )
        self.write_manifest((self.fixed_plan(1, source),))
        initial = self.prepare_plan().progress
        completed = {
            "items": [
                {
                    "intent": dict(initial["items"][0]["intent"]),
                    "complete": True,
                }
            ]
        }

        self.archiver().prepare(progress=completed)
        self.assert_archive_error(
            "archive_state_conflict",
            lambda: self.archiver().prepare(
                progress=initial,
                previous_progress=completed,
            ),
        )

    def test_converge_persists_each_item_only_after_archive_fsync(self):
        first = self.make_fixed_source(
            "11111111-1111-4111-8111-111111111111", b"first"
        )
        second = self.make_fixed_source(
            "22222222-2222-4222-8222-222222222222", b"second"
        )
        self.write_manifest((
            self.fixed_plan(1, first),
            self.fixed_plan(2, second),
        ))
        plan = self.prepare_plan()
        adapter = RecordingRenameNoReplaceAdapter()
        callbacks = []

        def item_complete(intent, current_progress, result):
            destination = (
                self.run_directory
                / intent.destination_parent_relative
                / intent.destination_name
            )
            self.assertTrue(destination.exists())
            callbacks.append((
                result.status,
                tuple(
                    item["complete"]
                    for item in current_progress["items"]
                ),
            ))

        outcome = self.archiver().converge(
            plan,
            syscall_adapter=adapter,
            on_item_complete=item_complete,
        )

        self.assertEqual(len(adapter.calls), 2)
        self.assertEqual(
            callbacks,
            [
                ("archived", (True, False)),
                ("archived", (True, True)),
            ],
        )
        self.assertEqual(
            tuple(
                item["complete"] for item in outcome.progress["items"]
            ),
            (True, True),
        )

    def test_converge_rejects_completed_item_changed_by_callback(self):
        first = self.make_fixed_source(
            "11111111-1111-4111-8111-111111111111", b"first"
        )
        second = self.make_fixed_source(
            "22222222-2222-4222-8222-222222222222", b"second"
        )
        self.write_manifest((
            self.fixed_plan(1, first),
            self.fixed_plan(2, second),
        ))
        plan = self.prepare_plan()
        adapter = RecordingRenameNoReplaceAdapter()

        def change_first_destination(_intent, progress, _result):
            if tuple(
                item["complete"] for item in progress["items"]
            ) == (True, False):
                (
                    self.run_directory
                    / fixed_slot_destination_path(first)
                ).write_bytes(b"FORGE")

        self.assert_archive_error(
            "archive_manifest_mismatch",
            lambda: self.archiver().converge(
                plan,
                syscall_adapter=adapter,
                on_item_complete=change_first_destination,
            ),
        )
        self.assertEqual(len(adapter.calls), 1)
        self.assertTrue((self.source_root / second).exists())

    def test_converge_rejects_manifest_changed_by_callback(self):
        source = self.make_fixed_source(
            "11111111-1111-4111-8111-111111111111", b"first"
        )
        self.write_manifest((self.fixed_plan(1, source),))
        plan = self.prepare_plan()

        def change_manifest(_intent, _progress, _result):
            (
                self.run_directory / AUTO_V2_MANIFEST_FILENAME
            ).write_bytes(b"tampered-manifest")

        self.assert_archive_error(
            "media_manifest_torn_tail_requires_execute",
            lambda: self.archiver().converge(
                plan,
                syscall_adapter=RecordingRenameNoReplaceAdapter(),
                on_item_complete=change_manifest,
            ),
        )

    def test_completed_progress_is_reverified_as_exact_recovery(self):
        source = self.make_fixed_source(
            "11111111-1111-4111-8111-111111111111"
        )
        self.write_manifest((self.fixed_plan(1, source),))
        plan = self.prepare_plan()
        first_adapter = RecordingRenameNoReplaceAdapter()
        first = self.archiver().converge(
            plan,
            syscall_adapter=first_adapter,
        )
        resumed_plan = self.archiver().prepare(
            progress=first.progress
        )
        recovery_adapter = RecordingRenameNoReplaceAdapter()

        resumed = self.archiver().converge(
            resumed_plan,
            syscall_adapter=recovery_adapter,
        )

        self.assertEqual(len(first_adapter.calls), 1)
        self.assertEqual(recovery_adapter.calls, [])
        self.assertEqual(resumed.results[0].status, "recovered")

    def test_completed_progress_rejects_source_reappearance(self):
        source = self.make_fixed_source(
            "11111111-1111-4111-8111-111111111111"
        )
        self.write_manifest((self.fixed_plan(1, source),))
        plan = self.prepare_plan()
        first = self.archiver().converge(
            plan,
            syscall_adapter=RecordingRenameNoReplaceAdapter(),
        )
        source_path = self.source_root / source
        source_path.write_bytes(b"replacement")

        self.assert_archive_error(
            "archive_state_conflict",
            lambda: self.archiver().converge(
                self.archiver().prepare(progress=first.progress),
                syscall_adapter=RecordingRenameNoReplaceAdapter(),
            ),
        )

    def test_crash_after_item_fsync_before_progress_write_recovers(self):
        first_source = self.make_fixed_source(
            "11111111-1111-4111-8111-111111111111", b"first"
        )
        second_source = self.make_fixed_source(
            "22222222-2222-4222-8222-222222222222", b"second"
        )
        self.write_manifest((
            self.fixed_plan(1, first_source),
            self.fixed_plan(2, second_source),
        ))
        plan = self.prepare_plan()

        def crash_before_progress_write(_intent, _progress, _result):
            raise SimulatedProcessCrash()

        with self.assertRaises(SimulatedProcessCrash):
            self.archiver().converge(
                plan,
                syscall_adapter=RecordingRenameNoReplaceAdapter(),
                on_item_complete=crash_before_progress_write,
            )

        resumed_plan = self.archiver().prepare(progress=plan.progress)
        adapter = RecordingRenameNoReplaceAdapter()
        resumed = self.archiver().converge(
            resumed_plan, syscall_adapter=adapter
        )

        self.assertEqual(
            tuple(result.status for result in resumed.results),
            ("recovered", "archived"),
        )
        self.assertEqual(len(adapter.calls), 1)

    def test_failing_partial_open_closes_every_descriptor(self):
        intent = self.make_fixed_source(
            "11111111-1111-4111-8111-111111111111"
        )
        self.write_manifest((self.fixed_plan(1, intent),))
        intents = self.prepare()
        archive_intent = intents[0]
        destination = (
            self.run_directory
            / archive_intent.destination_parent_relative
            / archive_intent.destination_name
        )
        destination.symlink_to(self.data_root / "outside")
        descriptor_directory = "/dev/fd"
        if not os.path.isdir(descriptor_directory):
            self.skipTest("descriptor inventory is unavailable")
        before = set(os.listdir(descriptor_directory))

        self.assert_archive_error(
            "archive_state_conflict",
            lambda: open_archive_session(
                str(self.source_root),
                str(self.run_directory),
                archive_intent,
                syscall_adapter=RecordingRenameNoReplaceAdapter(),
            ),
        )

        self.assertEqual(set(os.listdir(descriptor_directory)), before)
