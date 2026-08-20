import errno
import fcntl
import multiprocessing
import os
from pathlib import Path
import tempfile
import threading

from django.test import SimpleTestCase
import mock

from django_images import file_ops


def _acquire_dedup_lock_in_process(
    media_root, submitter_id, content_sha256, entered, release, errors
):
    root_directory = file_ops.open_media_root(media_root)
    try:
        with file_ops.media_dedup_lock(
            root_directory,
            submitter_id,
            content_sha256,
            deadline=file_ops.time.monotonic() + 5,
        ):
            entered.set()
            release.wait(5)
    except BaseException as error:
        errors.put((error.__class__.__name__, str(error)))
    finally:
        root_directory.close()


class _Clock(object):
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


class _SequenceClock(object):
    def __init__(self, values):
        self.values = list(values)
        self.last = self.values[-1]

    def __call__(self):
        if self.values:
            self.last = self.values.pop(0)
        return self.last


class MediaLifecycleLockTests(SimpleTestCase):
    def setUp(self):
        self.temporary_root = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_root.cleanup)
        self.root_path = Path(self.temporary_root.name)
        self.root_directory = file_ops.open_media_root(
            self.temporary_root.name
        )
        self.addCleanup(self.root_directory.close)

    @property
    def lock_directory_path(self):
        return self.root_path / ".pinry-locks"

    @property
    def lock_path(self):
        return self.lock_directory_path / "media-lifecycle.lock"

    def _create_lock_file(self):
        self.lock_directory_path.mkdir(mode=0o700)
        self.lock_path.touch(mode=0o600)
        os.chmod(str(self.lock_path), 0o600)

    def _assert_lock_error(self, expected_code, callable_):
        with self.assertRaises(file_ops.MediaPathError) as caught:
            callable_()
        self.assertEqual(str(caught.exception), expected_code)

    def test_two_shared_holders_enter_concurrently(self):
        first_entered = threading.Event()
        release = threading.Event()
        second_entered = threading.Event()
        errors = []

        def hold_shared(entered):
            try:
                with file_ops.media_lifecycle_lock(
                    self.root_directory,
                    deadline=file_ops.time.monotonic() + 2,
                ):
                    entered.set()
                    release.wait(2)
            except BaseException as error:
                errors.append(error)

        first = threading.Thread(target=hold_shared, args=(first_entered,))
        second = threading.Thread(target=hold_shared, args=(second_entered,))
        first.start()
        self.assertTrue(first_entered.wait(1))
        second.start()
        self.assertTrue(second_entered.wait(1))
        release.set()
        first.join(1)
        second.join(1)

        self.assertEqual(errors, [])
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())

    def test_shared_holder_blocks_exclusive_until_release(self):
        exclusive_entered = threading.Event()
        errors = []

        def hold_exclusive():
            try:
                with file_ops.media_lifecycle_lock(
                    self.root_directory,
                    exclusive=True,
                    deadline=file_ops.time.monotonic() + 2,
                ):
                    exclusive_entered.set()
            except BaseException as error:
                errors.append(error)

        with file_ops.media_lifecycle_lock(
            self.root_directory,
            deadline=file_ops.time.monotonic() + 2,
        ):
            thread = threading.Thread(target=hold_exclusive)
            thread.start()
            self.assertFalse(exclusive_entered.wait(0.05))

        self.assertTrue(exclusive_entered.wait(1))
        thread.join(1)
        self.assertEqual(errors, [])

    def test_name_swap_while_waiting_never_enters_old_inode_lock(self):
        self._create_lock_file()
        blocker = os.open(str(self.lock_path), os.O_RDWR)
        replacement = None
        opened = threading.Event()
        entered = threading.Event()
        errors = []
        original_open = file_ops._open_media_lifecycle_lock

        def open_and_signal(root_directory):
            result = original_open(root_directory)
            opened.set()
            return result

        def wait_on_old_inode():
            try:
                with file_ops.media_lifecycle_lock(
                    self.root_directory,
                    deadline=file_ops.time.monotonic() + 2,
                ):
                    entered.set()
            except BaseException as error:
                errors.append(error)

        try:
            fcntl.flock(blocker, fcntl.LOCK_EX)
            with mock.patch(
                "django_images.file_ops._open_media_lifecycle_lock",
                side_effect=open_and_signal,
            ):
                thread = threading.Thread(target=wait_on_old_inode)
                thread.start()
                self.assertTrue(opened.wait(1))
                self.lock_path.unlink()
                replacement = os.open(
                    str(self.lock_path),
                    os.O_RDWR | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                fcntl.flock(replacement, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(blocker, fcntl.LOCK_UN)
                thread.join(1)
        finally:
            if replacement is not None:
                fcntl.flock(replacement, fcntl.LOCK_UN)
                os.close(replacement)
            os.close(blocker)

        self.assertFalse(thread.is_alive())
        self.assertFalse(entered.is_set())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], file_ops.MediaPathError)
        self.assertEqual(str(errors[0]), "media_lifecycle_lock_failed")

    def test_lock_directory_swap_while_waiting_fails_closed(self):
        self._create_lock_file()
        moved_directory = self.root_path / ".pinry-locks-old"
        blocker = os.open(str(self.lock_path), os.O_RDWR)
        replacement = None
        opened = threading.Event()
        entered = threading.Event()
        errors = []
        original_open = file_ops._open_media_lifecycle_lock

        def open_and_signal(root_directory):
            result = original_open(root_directory)
            opened.set()
            return result

        def wait_on_old_directory():
            try:
                with file_ops.media_lifecycle_lock(
                    self.root_directory,
                    deadline=file_ops.time.monotonic() + 2,
                ):
                    entered.set()
            except BaseException as error:
                errors.append(error)

        try:
            fcntl.flock(blocker, fcntl.LOCK_EX)
            with mock.patch(
                "django_images.file_ops._open_media_lifecycle_lock",
                side_effect=open_and_signal,
            ):
                thread = threading.Thread(target=wait_on_old_directory)
                thread.start()
                self.assertTrue(opened.wait(1))
                self.lock_directory_path.rename(moved_directory)
                self.lock_directory_path.mkdir(mode=0o700)
                self.lock_path.touch(mode=0o600)
                os.chmod(str(self.lock_path), 0o600)
                replacement = os.open(str(self.lock_path), os.O_RDWR)
                fcntl.flock(replacement, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(blocker, fcntl.LOCK_UN)
                thread.join(1)
        finally:
            if replacement is not None:
                fcntl.flock(replacement, fcntl.LOCK_UN)
                os.close(replacement)
            os.close(blocker)

        self.assertFalse(thread.is_alive())
        self.assertFalse(entered.is_set())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], file_ops.MediaPathError)
        self.assertEqual(str(errors[0]), "media_lifecycle_lock_failed")

    def test_root_swap_after_open_fails_before_entering_lock(self):
        opened = threading.Event()
        continue_enter = threading.Event()
        entered = threading.Event()
        errors = []
        original_open = file_ops._open_media_lifecycle_lock

        def open_then_pause(root_directory):
            result = original_open(root_directory)
            opened.set()
            continue_enter.wait(1)
            return result

        def acquire():
            try:
                with file_ops.media_lifecycle_lock(
                    self.root_directory,
                    deadline=file_ops.time.monotonic() + 2,
                ):
                    entered.set()
            except BaseException as error:
                errors.append(error)

        moved_root = self.root_path.with_name(self.root_path.name + "-moved")
        try:
            with mock.patch(
                "django_images.file_ops._open_media_lifecycle_lock",
                side_effect=open_then_pause,
            ):
                thread = threading.Thread(target=acquire)
                thread.start()
                self.assertTrue(opened.wait(1))
                self.root_path.rename(moved_root)
                self.root_path.mkdir()
                continue_enter.set()
                thread.join(1)
        finally:
            continue_enter.set()
            if self.root_path.exists():
                self.root_path.rmdir()
            if moved_root.exists():
                moved_root.rename(self.root_path)

        self.assertFalse(thread.is_alive())
        self.assertFalse(entered.is_set())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], file_ops.MediaPathError)

    def test_symlink_hardlink_and_wrong_mode_lock_files_fail_closed(self):
        outside = Path(self.temporary_root.name).with_name(
            Path(self.temporary_root.name).name + "-outside-lock"
        )
        outside.write_bytes(b"outside")
        self.addCleanup(lambda: outside.exists() and outside.unlink())

        cases = ("symlink", "hardlink", "mode")
        for case in cases:
            with self.subTest(case=case):
                if self.lock_directory_path.exists():
                    for child in self.lock_directory_path.iterdir():
                        child.unlink()
                    self.lock_directory_path.rmdir()
                self.lock_directory_path.mkdir(mode=0o700)
                if case == "symlink":
                    self.lock_path.symlink_to(outside)
                else:
                    self.lock_path.write_bytes(b"")
                    os.chmod(str(self.lock_path), 0o600)
                    if case == "hardlink":
                        os.link(
                            str(self.lock_path),
                            str(self.lock_directory_path / "second-link"),
                        )
                    else:
                        os.chmod(str(self.lock_path), 0o700)

                self._assert_lock_error(
                    "media_lifecycle_lock_failed",
                    lambda: file_ops.media_lifecycle_lock(
                        self.root_directory,
                        deadline=file_ops.time.monotonic() + 1,
                    ).__enter__(),
                )
                self.assertEqual(outside.read_bytes(), b"outside")

    def test_symlinked_or_wrong_mode_lock_directory_fails_closed(self):
        outside = Path(self.temporary_root.name).with_name(
            Path(self.temporary_root.name).name + "-outside-directory"
        )
        outside.mkdir(mode=0o700)
        self.addCleanup(lambda: outside.exists() and outside.rmdir())

        self.lock_directory_path.symlink_to(outside, target_is_directory=True)
        self._assert_lock_error(
            "media_lifecycle_lock_failed",
            lambda: file_ops.media_lifecycle_lock(
                self.root_directory,
                deadline=file_ops.time.monotonic() + 1,
            ).__enter__(),
        )
        self.assertFalse((outside / "media-lifecycle.lock").exists())

        self.lock_directory_path.unlink()
        self.lock_directory_path.mkdir(mode=0o755)
        self._assert_lock_error(
            "media_lifecycle_lock_failed",
            lambda: file_ops.media_lifecycle_lock(
                self.root_directory,
                deadline=file_ops.time.monotonic() + 1,
            ).__enter__(),
        )

    def test_missing_required_descriptor_flags_is_unsupported(self):
        for flag_name in ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC"):
            with self.subTest(flag_name=flag_name), mock.patch.object(
                file_ops.os,
                flag_name,
                None,
            ):
                self._assert_lock_error(
                    "media_lifecycle_lock_unsupported",
                    lambda: file_ops.media_lifecycle_lock(
                        self.root_directory,
                        deadline=file_ops.time.monotonic() + 1,
                    ).__enter__(),
                )

    def test_raw_root_descriptor_is_unsupported(self):
        self._assert_lock_error(
            "media_lifecycle_lock_unsupported",
            lambda: file_ops.media_lifecycle_lock(
                self.root_directory.descriptor,
                deadline=file_ops.time.monotonic() + 1,
            ).__enter__(),
        )

    def test_created_lock_directory_and_file_have_exact_modes(self):
        with file_ops.media_lifecycle_lock(
            self.root_directory,
            deadline=file_ops.time.monotonic() + 1,
        ):
            self.assertEqual(
                file_ops.stat.S_IMODE(self.lock_directory_path.stat().st_mode),
                0o700,
            )
            self.assertEqual(
                file_ops.stat.S_IMODE(self.lock_path.stat().st_mode),
                0o600,
            )

    def test_existing_open_and_missing_create_use_safe_distinct_flags(self):
        real_open = file_ops.os.open
        calls = []

        def record_open(path, flags, *args, **kwargs):
            if path == "media-lifecycle.lock":
                calls.append(flags)
            return real_open(path, flags, *args, **kwargs)

        with mock.patch(
            "django_images.file_ops._require_lifecycle_lock_support"
        ), mock.patch(
            "django_images.file_ops.os.open", side_effect=record_open
        ):
            with file_ops.media_lifecycle_lock(
                self.root_directory,
                deadline=file_ops.time.monotonic() + 1,
            ):
                pass

        self.assertGreaterEqual(len(calls), 2)
        self.assertFalse(calls[0] & os.O_CREAT)
        self.assertTrue(calls[1] & os.O_CREAT)
        self.assertTrue(calls[1] & os.O_EXCL)

    def test_create_race_retries_existing_open_without_truncating(self):
        real_open = file_ops.os.open
        injected_race = [False]

        def race_open(path, flags, *args, **kwargs):
            if (
                path == "media-lifecycle.lock"
                and flags & os.O_CREAT
                and flags & os.O_EXCL
                and not injected_race[0]
            ):
                injected_race[0] = True
                descriptor = real_open(path, flags, *args, **kwargs)
                os.close(descriptor)
                raise FileExistsError(errno.EEXIST, "injected create race")
            return real_open(path, flags, *args, **kwargs)

        with mock.patch(
            "django_images.file_ops._require_lifecycle_lock_support"
        ), mock.patch(
            "django_images.file_ops.os.open", side_effect=race_open
        ):
            with file_ops.media_lifecycle_lock(
                self.root_directory,
                deadline=file_ops.time.monotonic() + 1,
            ):
                pass

        self.assertTrue(injected_race[0])
        self.assertEqual(self.lock_path.read_bytes(), b"")

    def test_exact_deadline_fails_before_creating_lock_path(self):
        self._assert_lock_error(
            "media_lifecycle_busy",
            lambda: file_ops.media_lifecycle_lock(
                self.root_directory,
                deadline=10.0,
                clock=lambda: 10.0,
            ).__enter__(),
        )
        self.assertFalse(self.lock_directory_path.exists())

    def test_deadline_crossed_immediately_after_flock_releases_lock(self):
        clock = _SequenceClock((9.0, 9.0, 10.0))
        lifecycle_lock = file_ops.media_lifecycle_lock(
            self.root_directory,
            exclusive=True,
            deadline=10.0,
            clock=clock,
        )

        self._assert_lock_error(
            "media_lifecycle_busy",
            lifecycle_lock.__enter__,
        )

        self.assertIsNone(lifecycle_lock._directory_descriptor)
        self.assertIsNone(lifecycle_lock._lock_descriptor)
        with file_ops.media_lifecycle_lock(
            self.root_directory,
            exclusive=True,
            deadline=file_ops.time.monotonic() + 1,
        ):
            pass

    def test_unbounded_contention_fails_fast_with_retryable_busy_code(self):
        self._create_lock_file()
        blocker = os.open(str(self.lock_path), os.O_RDWR)
        try:
            fcntl.flock(blocker, fcntl.LOCK_EX)
            with self.assertRaises(file_ops.MediaLifecycleLockError) as caught:
                file_ops.media_lifecycle_lock(
                    self.root_directory,
                    exclusive=True,
                ).__enter__()
        finally:
            fcntl.flock(blocker, fcntl.LOCK_UN)
            os.close(blocker)

        self.assertEqual(caught.exception.code, "media_lifecycle_busy")
        self.assertTrue(caught.exception.retryable)

    def test_poll_sleep_is_clamped_to_remaining_deadline(self):
        self._create_lock_file()
        blocker = os.open(str(self.lock_path), os.O_RDWR)
        clock = _Clock(9.995)
        sleeps = []

        def expire_after_sleep(seconds):
            sleeps.append(seconds)
            clock.value = 10.0

        try:
            fcntl.flock(blocker, fcntl.LOCK_EX)
            self._assert_lock_error(
                "media_lifecycle_busy",
                lambda: file_ops.media_lifecycle_lock(
                    self.root_directory,
                    deadline=10.0,
                    clock=clock,
                    sleeper=expire_after_sleep,
                ).__enter__(),
            )
        finally:
            fcntl.flock(blocker, fcntl.LOCK_UN)
            os.close(blocker)

        self.assertEqual(len(sleeps), 1)
        self.assertAlmostEqual(sleeps[0], 0.005)

    def test_postverify_base_exception_releases_lock_and_all_descriptors(self):
        primary = KeyboardInterrupt()
        lifecycle_lock = file_ops.media_lifecycle_lock(
            self.root_directory,
            exclusive=True,
            deadline=file_ops.time.monotonic() + 1,
        )

        with mock.patch(
            "django_images.file_ops._verify_held_media_lifecycle_lock",
            side_effect=primary,
        ):
            with self.assertRaises(KeyboardInterrupt) as caught:
                lifecycle_lock.__enter__()

        self.assertIs(caught.exception, primary)
        self.assertIsNone(lifecycle_lock._directory_descriptor)
        self.assertIsNone(lifecycle_lock._lock_descriptor)
        with file_ops.media_lifecycle_lock(
            self.root_directory,
            exclusive=True,
            deadline=file_ops.time.monotonic() + 1,
        ):
            pass

    def test_postverify_media_error_closes_the_actual_open_descriptors(self):
        opened_descriptors = []
        original_open = file_ops._open_media_lifecycle_lock

        def capture_descriptors(root_directory):
            descriptors = original_open(root_directory)
            opened_descriptors.extend(descriptors)
            return descriptors

        lifecycle_lock = file_ops.media_lifecycle_lock(
            self.root_directory,
            exclusive=True,
            deadline=file_ops.time.monotonic() + 1,
        )
        with mock.patch(
            "django_images.file_ops._open_media_lifecycle_lock",
            side_effect=capture_descriptors,
        ), mock.patch(
            "django_images.file_ops._verify_held_media_lifecycle_lock",
            side_effect=file_ops.MediaPathError("postverify-primary"),
        ):
            with self.assertRaisesRegex(
                file_ops.MediaPathError, "postverify-primary"
            ):
                lifecycle_lock.__enter__()

        self.assertEqual(len(opened_descriptors), 2)
        for descriptor in opened_descriptors:
            with self.assertRaises(OSError) as caught:
                os.fstat(descriptor)
            self.assertEqual(caught.exception.errno, errno.EBADF)

    def test_postverify_error_is_not_masked_by_unlock_error(self):
        primary = KeyboardInterrupt()
        real_flock = file_ops.fcntl.flock

        def fail_unlock(descriptor, operation):
            if operation == fcntl.LOCK_UN:
                raise RuntimeError("secondary-close-error")
            return real_flock(descriptor, operation)

        lifecycle_lock = file_ops.media_lifecycle_lock(
            self.root_directory,
            exclusive=True,
            deadline=file_ops.time.monotonic() + 1,
        )
        with mock.patch(
            "django_images.file_ops._verify_held_media_lifecycle_lock",
            side_effect=primary,
        ), mock.patch(
            "django_images.file_ops.fcntl.flock",
            side_effect=fail_unlock,
        ):
            with self.assertRaises(KeyboardInterrupt) as caught:
                lifecycle_lock.__enter__()

        self.assertIs(caught.exception, primary)
        self.assertIsNone(lifecycle_lock._directory_descriptor)
        self.assertIsNone(lifecycle_lock._lock_descriptor)

    def test_body_error_is_not_masked_by_unlock_error(self):
        primary = ValueError("primary-body-error")
        real_flock = file_ops.fcntl.flock

        def fail_unlock(descriptor, operation):
            if operation == fcntl.LOCK_UN:
                raise RuntimeError("secondary-close-error")
            return real_flock(descriptor, operation)

        with mock.patch(
            "django_images.file_ops.fcntl.flock",
            side_effect=fail_unlock,
        ):
            with self.assertRaises(ValueError) as caught:
                with file_ops.media_lifecycle_lock(
                    self.root_directory,
                    deadline=file_ops.time.monotonic() + 1,
                ):
                    raise primary

        self.assertIs(caught.exception, primary)


class MediaDedupLockTests(SimpleTestCase):
    CONTENT_HASH = "0" * 64
    OTHER_CONTENT_HASH = "1" * 64

    def setUp(self):
        self.temporary_root = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_root.cleanup)
        self.root_path = Path(self.temporary_root.name)
        self.root_directory = file_ops.open_media_root(
            self.temporary_root.name
        )
        self.addCleanup(self.root_directory.close)

    @property
    def lock_directory_path(self):
        return self.root_path / ".pinry-locks"

    @property
    def lock_path(self):
        return self.lock_directory_path / "media-dedup-7c.lock"

    def _assert_lock_error(self, expected_code, callable_):
        with self.assertRaises(file_ops.MediaLifecycleLockError) as caught:
            callable_()
        self.assertEqual(caught.exception.code, expected_code)
        return caught.exception

    def _create_lock_file(self):
        self.lock_directory_path.mkdir(mode=0o700, exist_ok=True)
        self.lock_path.touch(mode=0o600)
        os.chmod(str(self.lock_path), 0o600)

    def test_known_key_uses_descriptor_anchored_fixed_stripe_name(self):
        with file_ops.media_dedup_lock(
            self.root_directory,
            1,
            self.CONTENT_HASH,
            deadline=file_ops.time.monotonic() + 1,
        ):
            self.assertTrue(self.lock_path.is_file())

    def test_generalized_lock_rejects_names_outside_fixed_set(self):
        escaped_path = self.root_path / "escaped.lock"
        lifecycle_lock = file_ops.MediaLifecycleLock(
            self.root_directory,
            exclusive=True,
            lock_filename="../escaped.lock",
        )

        self._assert_lock_error(
            "media_lifecycle_lock_failed",
            lifecycle_lock.__enter__,
        )
        self.assertFalse(escaped_path.exists())

    def test_all_created_names_are_the_fixed_256_stripe_set(self):
        for submitter_id in range(1, 3001):
            with file_ops.media_dedup_lock(
                self.root_directory,
                submitter_id,
                self.CONTENT_HASH,
                deadline=file_ops.time.monotonic() + 2,
            ):
                pass

        expected = {
            "media-dedup-{:02x}.lock".format(index)
            for index in range(256)
        }
        actual = {
            path.name for path in self.lock_directory_path.iterdir()
        }
        self.assertEqual(actual, expected)

    def test_same_key_serializes_threads(self):
        entered = threading.Event()
        errors = []

        def acquire_same_key():
            try:
                with file_ops.media_dedup_lock(
                    self.root_directory,
                    1,
                    self.CONTENT_HASH,
                    deadline=file_ops.time.monotonic() + 2,
                ):
                    entered.set()
            except BaseException as error:
                errors.append(error)

        with file_ops.media_dedup_lock(
            self.root_directory,
            1,
            self.CONTENT_HASH,
            deadline=file_ops.time.monotonic() + 2,
        ):
            thread = threading.Thread(target=acquire_same_key)
            thread.start()
            self.assertFalse(entered.wait(0.05))

        self.assertTrue(entered.wait(1))
        thread.join(1)
        self.assertEqual(errors, [])
        self.assertFalse(thread.is_alive())

    def test_same_key_serializes_processes(self):
        context = multiprocessing.get_context("fork")
        entered = context.Event()
        release = context.Event()
        errors = context.Queue()

        with file_ops.media_dedup_lock(
            self.root_directory,
            1,
            self.CONTENT_HASH,
            deadline=file_ops.time.monotonic() + 5,
        ):
            process = context.Process(
                target=_acquire_dedup_lock_in_process,
                args=(
                    self.temporary_root.name,
                    1,
                    self.CONTENT_HASH,
                    entered,
                    release,
                    errors,
                ),
            )
            process.start()
            self.assertFalse(entered.wait(0.1))

        self.assertTrue(entered.wait(2))
        release.set()
        process.join(2)
        self.assertFalse(process.is_alive())
        self.assertEqual(process.exitcode, 0)
        self.assertTrue(errors.empty())

    def test_different_stripes_enter_concurrently(self):
        entered = threading.Event()
        errors = []

        def acquire_other_stripe():
            try:
                with file_ops.media_dedup_lock(
                    self.root_directory,
                    1,
                    self.OTHER_CONTENT_HASH,
                    deadline=file_ops.time.monotonic() + 1,
                ):
                    entered.set()
            except BaseException as error:
                errors.append(error)

        with file_ops.media_dedup_lock(
            self.root_directory,
            1,
            self.CONTENT_HASH,
            deadline=file_ops.time.monotonic() + 1,
        ):
            thread = threading.Thread(target=acquire_other_stripe)
            thread.start()
            self.assertTrue(entered.wait(0.5))

        thread.join(1)
        self.assertEqual(errors, [])

    def test_rejects_invalid_submitter_and_hash_without_creating_files(self):
        invalid_values = (
            (0, self.CONTENT_HASH),
            (-1, self.CONTENT_HASH),
            (True, self.CONTENT_HASH),
            ("1", self.CONTENT_HASH),
            (1, "a" * 63),
            (1, "A" * 64),
            (1, "g" * 64),
            (1, b"a" * 64),
        )
        for submitter_id, content_sha256 in invalid_values:
            with self.subTest(
                submitter_id=submitter_id,
                content_sha256=content_sha256,
            ):
                error = self._assert_lock_error(
                    "media_lifecycle_lock_failed",
                    lambda: file_ops.media_dedup_lock(
                        self.root_directory,
                        submitter_id,
                        content_sha256,
                    ),
                )
                self.assertFalse(error.retryable)
        self.assertFalse(self.lock_directory_path.exists())

    def test_exact_deadline_is_retryable_and_creates_no_lock_path(self):
        error = self._assert_lock_error(
            "media_lifecycle_busy",
            lambda: file_ops.media_dedup_lock(
                self.root_directory,
                1,
                self.CONTENT_HASH,
                deadline=10.0,
                clock=lambda: 10.0,
            ).__enter__(),
        )
        self.assertTrue(error.retryable)
        self.assertFalse(self.lock_directory_path.exists())

    def test_unbounded_contention_fails_fast_with_retryable_busy_code(self):
        self._create_lock_file()
        blocker = os.open(str(self.lock_path), os.O_RDWR)
        try:
            fcntl.flock(blocker, fcntl.LOCK_EX)
            error = self._assert_lock_error(
                "media_lifecycle_busy",
                lambda: file_ops.media_dedup_lock(
                    self.root_directory,
                    1,
                    self.CONTENT_HASH,
                ).__enter__(),
            )
        finally:
            fcntl.flock(blocker, fcntl.LOCK_UN)
            os.close(blocker)

        self.assertTrue(error.retryable)

    def test_unsafe_lock_entries_fail_closed_and_are_nonretryable(self):
        outside = self.root_path.with_name(self.root_path.name + "-outside")
        outside.write_bytes(b"outside")
        self.addCleanup(lambda: outside.exists() and outside.unlink())

        for case in ("symlink", "fifo", "hardlink", "mode"):
            with self.subTest(case=case):
                if self.lock_directory_path.exists():
                    for child in self.lock_directory_path.iterdir():
                        child.unlink()
                    self.lock_directory_path.rmdir()
                self.lock_directory_path.mkdir(mode=0o700)
                if case == "symlink":
                    self.lock_path.symlink_to(outside)
                elif case == "fifo":
                    os.mkfifo(str(self.lock_path), 0o600)
                else:
                    self._create_lock_file()
                    if case == "hardlink":
                        os.link(
                            str(self.lock_path),
                            str(self.lock_directory_path / "second-link"),
                        )
                    else:
                        os.chmod(str(self.lock_path), 0o640)

                error = self._assert_lock_error(
                    "media_lifecycle_lock_failed",
                    lambda: file_ops.media_dedup_lock(
                        self.root_directory,
                        1,
                        self.CONTENT_HASH,
                        deadline=file_ops.time.monotonic() + 1,
                    ).__enter__(),
                )
                self.assertFalse(error.retryable)
                self.assertEqual(outside.read_bytes(), b"outside")

    def test_postflock_error_closes_all_open_descriptors(self):
        opened_descriptors = []
        original_open = file_ops._open_media_lifecycle_lock

        def capture_descriptors(root_directory, lock_filename):
            descriptors = original_open(root_directory, lock_filename)
            opened_descriptors.extend(descriptors)
            return descriptors

        dedup_lock = file_ops.media_dedup_lock(
            self.root_directory,
            1,
            self.CONTENT_HASH,
            deadline=file_ops.time.monotonic() + 1,
        )
        with mock.patch(
            "django_images.file_ops._open_media_lifecycle_lock",
            side_effect=capture_descriptors,
        ), mock.patch(
            "django_images.file_ops._verify_held_media_lifecycle_lock",
            side_effect=file_ops.MediaPathError("postverify-primary"),
        ):
            with self.assertRaisesRegex(
                file_ops.MediaPathError, "postverify-primary"
            ):
                dedup_lock.__enter__()

        self.assertEqual(len(opened_descriptors), 2)
        for descriptor in opened_descriptors:
            with self.assertRaises(OSError) as caught:
                os.fstat(descriptor)
            self.assertEqual(caught.exception.errno, errno.EBADF)

    def test_raw_root_descriptor_is_unsupported_and_nonretryable(self):
        error = self._assert_lock_error(
            "media_lifecycle_lock_unsupported",
            lambda: file_ops.media_dedup_lock(
                self.root_directory.descriptor,
                1,
                self.CONTENT_HASH,
            ).__enter__(),
        )
        self.assertFalse(error.retryable)
