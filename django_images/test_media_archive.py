import ctypes
import errno
import os
import stat
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest import mock
import uuid

from django.test import SimpleTestCase, TestCase

from django_images.file_ops import open_verified_media_root
from django_images.models import Image, Thumbnail
from django_images.paths import canonical_original_path
from django_images.services.media_archive import (
    ArchiveIntent,
    LegacyMediaArchive,
    LinuxRenameNoReplaceAdapter,
    MediaArchiveError,
    build_archive_intent,
    fixed_slot_destination_path,
    open_archive_session,
    validate_no_legacy_media_references,
)
from django_images.services.media_migration_v2 import (
    AUTO_V2_MANIFEST_FILENAME,
    AutoV2ManifestLog,
    AutoV2MigrationFile,
    AutoV2MigrationPlan,
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
            sha256="0" * 64,
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

    def prepare_plan(self, has_md5=False, schema_only=False, **kwargs):
        return self.archiver().prepare(
            has_media_image_directory=has_md5,
            schema_only=schema_only,
            **kwargs
        )

    def prepare(self, has_md5=False, schema_only=False):
        return self.prepare_plan(
            has_md5=has_md5, schema_only=schema_only
        ).intents

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
            "load_auto_v2_archive_sources"
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
