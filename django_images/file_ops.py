import ctypes
import errno
import hashlib
import os
from pathlib import PurePosixPath
import stat
import sys
import time
import uuid

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised by platform gate tests
    fcntl = None


class MediaPathError(Exception):
    pass


class MediaLifecycleLockError(MediaPathError):
    def __init__(self, code, retryable=False):
        super(MediaLifecycleLockError, self).__init__(code)
        self.code = code
        self.retryable = retryable


class PublishResult(object):
    def __init__(
        self,
        created=False,
        reused=False,
        destination_stat=None,
    ):
        self.created = created
        self.reused = reused
        self.destination_stat = destination_stat


class PublishFailure(Exception):
    def __init__(self, result, cause):
        super(PublishFailure, self).__init__("publish_failed")
        self.result = result
        self.cause = cause


class DescriptorCloseNotAttempted(Exception):
    pass


class MediaLifecycleLock(object):
    def __init__(
        self,
        root_directory,
        exclusive,
        deadline=None,
        clock=None,
        sleeper=None,
        lock_filename="media-lifecycle.lock",
    ):
        self.root_directory = root_directory
        self.exclusive = exclusive
        self.deadline = deadline
        self.clock = time.monotonic if clock is None else clock
        self.sleeper = time.sleep if sleeper is None else sleeper
        self.lock_filename = lock_filename
        self._directory_descriptor = None
        self._lock_descriptor = None
        self._held = False

    def __enter__(self):
        try:
            lock_filename = self.lock_filename
            _require_lifecycle_lock_filename(lock_filename)
            self._check_deadline()
            if lock_filename == "media-lifecycle.lock":
                opened = _open_media_lifecycle_lock(self.root_directory)
            else:
                opened = _open_media_lifecycle_lock(
                    self.root_directory,
                    lock_filename,
                )
            self._directory_descriptor, self._lock_descriptor = opened
            operation = fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH
            while True:
                self._check_deadline()
                try:
                    fcntl.flock(
                        self._lock_descriptor,
                        operation | fcntl.LOCK_NB,
                    )
                except OSError as error:
                    if error.errno not in (errno.EACCES, errno.EAGAIN):
                        raise _lifecycle_lock_error(
                            "media_lifecycle_lock_failed"
                        ) from error
                    if self.deadline is None:
                        raise _lifecycle_lock_error(
                            "media_lifecycle_busy",
                            retryable=True,
                        )
                    now = self.clock()
                    remaining = self.deadline - now
                    if remaining <= 0:
                        raise _lifecycle_lock_error(
                            "media_lifecycle_busy",
                            retryable=True,
                        )
                    self.sleeper(min(0.01, remaining))
                    continue
                self._held = True
                self._check_deadline()
                if lock_filename == "media-lifecycle.lock":
                    _verify_held_media_lifecycle_lock(
                        self.root_directory,
                        self._directory_descriptor,
                        self._lock_descriptor,
                    )
                else:
                    _verify_held_media_lifecycle_lock(
                        self.root_directory,
                        self._directory_descriptor,
                        self._lock_descriptor,
                        lock_filename,
                    )
                self._check_deadline()
                return self
        except BaseException:
            self._close_preserving_active_error()
            raise

    def __exit__(self, error_type, error, traceback):
        del error, traceback
        try:
            self.close()
        except BaseException:
            if error_type is None:
                raise
        return False

    def _check_deadline(self):
        if self.deadline is not None and self.clock() >= self.deadline:
            raise _lifecycle_lock_error(
                "media_lifecycle_busy",
                retryable=True,
            )

    def _close_preserving_active_error(self):
        try:
            self.close()
        except BaseException:
            pass

    def close(self):
        first_error = None
        if self._lock_descriptor is not None:
            descriptor = self._lock_descriptor
            self._lock_descriptor = None
            held = self._held
            self._held = False
            if held:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except BaseException as error:
                    first_error = error
            try:
                os.close(descriptor)
            except BaseException as error:
                if first_error is None:
                    first_error = error
        if self._directory_descriptor is not None:
            descriptor = self._directory_descriptor
            self._directory_descriptor = None
            try:
                os.close(descriptor)
            except BaseException as error:
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error


def media_lifecycle_lock(
    root_directory,
    exclusive=False,
    deadline=None,
    clock=None,
    sleeper=None,
):
    return MediaLifecycleLock(
        root_directory,
        exclusive=exclusive,
        deadline=deadline,
        clock=clock,
        sleeper=sleeper,
    )


def media_dedup_lock(
    root_directory,
    submitter_id,
    content_sha256,
    deadline=None,
    clock=None,
    sleeper=None,
):
    if (
        type(submitter_id) is not int
        or submitter_id <= 0
        or not isinstance(content_sha256, str)
        or len(content_sha256) != 64
        or any(
            character not in "0123456789abcdef"
            for character in content_sha256
        )
    ):
        raise _lifecycle_lock_error("media_lifecycle_lock_failed")
    key = "{}:{}".format(submitter_id, content_sha256).encode("ascii")
    stripe = hashlib.sha256(key).digest()[0]
    lock_filename = "media-dedup-{:02x}.lock".format(stripe)
    return MediaLifecycleLock(
        root_directory,
        exclusive=True,
        deadline=deadline,
        clock=clock,
        sleeper=sleeper,
        lock_filename=lock_filename,
    )


def _close_descriptor(descriptor):
    os.close(descriptor)


def _close_all(*close_functions):
    first_error = None
    for close_function in close_functions:
        try:
            close_function()
        except BaseException as error:
            if first_error is None:
                first_error = error
    if first_error is not None:
        raise first_error


class MediaDirectory(object):
    def __init__(
        self,
        descriptors,
        names=None,
        directory_stats=None,
        created=None,
        root_path=None,
        root_stat=None,
    ):
        self.descriptors = descriptors
        self.names = list(names or [])
        self.directory_stats = list(directory_stats or [])
        self.created = list(created or [])
        self.root_path = root_path
        self.root_stat = root_stat
        self.removed = [False for _name in self.names]

    @property
    def descriptor(self):
        return self.descriptors[-1]

    def fsync_publish(self):
        for descriptor in reversed(self.descriptors):
            os.fsync(descriptor)

    def verify_current(self):
        if not self.descriptors:
            raise MediaPathError("unsafe_media_directory")
        if self.root_path is not None and self.root_stat is not None:
            try:
                current_root = os.stat(
                    self.root_path,
                    follow_symlinks=False,
                )
            except OSError as error:
                raise MediaPathError("unsafe_media_directory") from error
            if (
                not stat.S_ISDIR(current_root.st_mode)
                or _identity(current_root) != _identity(self.root_stat)
            ):
                raise MediaPathError("unsafe_media_directory")
        for index, name in enumerate(self.names):
            if self.removed[index]:
                continue
            try:
                current = os.stat(
                    name,
                    dir_fd=self.descriptors[index],
                    follow_symlinks=False,
                )
            except OSError as error:
                raise MediaPathError("unsafe_media_directory") from error
            expected = self.directory_stats[index]
            if (
                not stat.S_ISDIR(current.st_mode)
                or _identity(current) != _identity(expected)
            ):
                raise MediaPathError("unsafe_media_directory")
        return True

    def remove_created_suffix(self, keep_components):
        if keep_components < 0:
            raise ValueError("keep_components must not be negative")
        incomplete_reason = None
        for index in range(len(self.names) - 1, keep_components - 1, -1):
            if self.removed[index]:
                continue
            if not self.created[index]:
                break
            parent_descriptor = self.descriptors[index]
            name = self.names[index]
            expected = self.directory_stats[index]
            try:
                current = os.stat(
                    name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                self.removed[index] = True
                continue
            except OSError as error:
                return type(error).__name__
            if (
                not stat.S_ISDIR(current.st_mode)
                or _identity(current) != _identity(expected)
            ):
                return "IdentityMismatch"
            try:
                os.rmdir(name, dir_fd=parent_descriptor)
            except FileNotFoundError:
                self.removed[index] = True
                continue
            except OSError as error:
                if error.errno in (errno.EEXIST, errno.ENOTEMPTY):
                    return "DirectoryNotEmpty"
                return type(error).__name__
            self.removed[index] = True
            try:
                os.fsync(parent_descriptor)
            except OSError as error:
                if incomplete_reason is None:
                    incomplete_reason = type(error).__name__
        return incomplete_reason

    def close(self):
        first_error = None
        retained = []
        while self.descriptors:
            descriptor = self.descriptors.pop()
            try:
                _close_descriptor(descriptor)
            except DescriptorCloseNotAttempted as error:
                retained.append(descriptor)
                if first_error is None:
                    first_error = error
            except BaseException as error:
                if first_error is None:
                    first_error = error
        self.descriptors.extend(reversed(retained))
        if first_error is not None:
            raise first_error


class OwnedStagingFile(object):
    def __init__(
        self,
        directory,
        name,
        descriptor,
        file_stat,
        path,
        owns_directory=True,
        idempotent_cleanup=False,
    ):
        self.directory = directory
        self.name = name
        self.descriptor = descriptor
        self.file_stat = file_stat
        self.path = path
        self.owns_directory = owns_directory
        self.idempotent_cleanup = idempotent_cleanup
        self._cleaned = False
        self._cleanup_removed = None
        self._closed = False

    def cleanup(self):
        if self._cleaned:
            return self._cleanup_removed
        if self.idempotent_cleanup:
            status = _unlink_owned_name_if_current_status(
                self.directory.descriptor,
                self.name,
                self.file_stat,
            )
            removed = status != "preserved"
        else:
            _unlink_owned_name(
                self.directory.descriptor,
                self.name,
                self.file_stat,
            )
            status = "removed"
            removed = True
        if status == "removed":
            os.fsync(self.directory.descriptor)
        self._cleanup_removed = removed
        self._cleaned = True
        return removed

    def close(self):
        if self._closed:
            if self.owns_directory and self.directory.descriptors:
                self.directory.close()
            return
        self._closed = True

        def close_file():
            try:
                _close_descriptor(self.descriptor)
            except DescriptorCloseNotAttempted:
                self._closed = False
                raise

        close_functions = [close_file]
        if self.owns_directory:
            close_functions.append(self.directory.close)
        _close_all(*close_functions)


def sha256_path(path):
    digest = hashlib.sha256()
    with open(path, "rb") as file_obj:
        while True:
            chunk = file_obj.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def sha256_file_descriptor(file_descriptor):
    digest = hashlib.sha256()
    with os.fdopen(os.dup(file_descriptor), "rb") as file_obj:
        file_obj.seek(0)
        while True:
            chunk = file_obj.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _relative_components(relative_name):
    if not isinstance(relative_name, str) or not relative_name:
        raise MediaPathError("media_path_escape")
    raw_parts = relative_name.split("/")
    path = PurePosixPath(relative_name)
    if path.is_absolute() or "\\" in relative_name:
        raise MediaPathError("media_path_escape")
    if any(part in ("", ".", "..") for part in raw_parts):
        raise MediaPathError("media_path_escape")
    return path.parts


def _ensure_inside(path, root, error_code):
    real_root = os.path.realpath(root)
    real_path = os.path.realpath(path)
    try:
        inside = os.path.commonpath((real_root, real_path)) == real_root
    except ValueError:
        inside = False
    if not inside or real_path == real_root:
        raise MediaPathError(error_code)
    return real_path


def resolve_media_path(relative_name, media_root):
    components = _relative_components(relative_name)
    candidate = os.path.join(os.path.realpath(media_root), *components)
    return _ensure_inside(candidate, media_root, "media_path_escape")


def resolve_data_path(path, data_root, error_code="manifest_path_escape"):
    real_root = os.path.realpath(data_root)
    if os.path.isabs(path):
        candidate = path
        raw_parts = path.split("/")[1:]
    else:
        raw_parts = _relative_components(path)
        candidate = os.path.join(real_root, *raw_parts)
    if any(part in ("", ".", "..") for part in raw_parts):
        raise MediaPathError(error_code)
    return _ensure_inside(candidate, real_root, error_code)


def migration_lock_path(data_root):
    return resolve_data_path(
        "migrations/media-migration.lock",
        data_root,
        error_code="migration_lock_path_escape",
    )


def open_media_root(media_root):
    root_path = os.path.realpath(media_root)
    descriptor = _open_directory(root_path)
    try:
        root_stat = os.fstat(descriptor)
        if not stat.S_ISDIR(root_stat.st_mode):
            raise MediaPathError("unsafe_media_directory")
        return MediaDirectory(
            [descriptor],
            root_path=root_path,
            root_stat=root_stat,
        )
    except BaseException:
        os.close(descriptor)
        raise


def remove_media_file(root_directory, relative_name):
    components = _relative_components(relative_name)
    if len(components) != 3:
        raise MediaPathError("unsafe_media_file")
    root_directory.verify_current()
    parent_name, directory_name, leaf_name = components
    try:
        parent_stat = os.stat(
            parent_name,
            dir_fd=root_directory.descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        os.fsync(root_directory.descriptor)
        return True
    parent_descriptor = _open_child_directory_nofollow(
        root_directory.descriptor,
        parent_name,
        parent_stat,
    )
    try:
        try:
            directory_stat = os.stat(
                directory_name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            os.fsync(parent_descriptor)
            return True
        directory_descriptor = _open_child_directory_nofollow(
            parent_descriptor,
            directory_name,
            directory_stat,
        )
        try:
            try:
                leaf_stat = os.stat(
                    leaf_name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                os.fsync(directory_descriptor)
                return True
            if not stat.S_ISREG(leaf_stat.st_mode):
                raise MediaPathError("unsafe_media_file")
            leaf_descriptor = _open_regular_nofollow(
                directory_descriptor,
                leaf_name,
            )
            try:
                if _identity(os.fstat(leaf_descriptor)) != _identity(
                    leaf_stat
                ):
                    raise MediaPathError("unsafe_media_file")
                root_directory.verify_current()
                current_parent = os.stat(
                    parent_name,
                    dir_fd=root_directory.descriptor,
                    follow_symlinks=False,
                )
                current_directory = os.stat(
                    directory_name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                current_leaf = os.stat(
                    leaf_name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
                if (
                    _identity(current_parent) != _identity(parent_stat)
                    or _identity(current_directory)
                    != _identity(directory_stat)
                    or _identity(current_leaf) != _identity(leaf_stat)
                ):
                    raise MediaPathError("unsafe_media_file")
                _unlink_owned_name(
                    directory_descriptor,
                    leaf_name,
                    leaf_stat,
                )
                os.fsync(directory_descriptor)
                return True
            finally:
                os.close(leaf_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        os.close(parent_descriptor)


def remove_empty_media_directory(root_directory, relative_directory):
    components = _relative_components(relative_directory)
    if len(components) != 2:
        raise MediaPathError("unsafe_media_directory")
    root_directory.verify_current()
    parent_name, leaf_name = components
    try:
        parent_stat = os.stat(
            parent_name,
            dir_fd=root_directory.descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return True
    parent_descriptor = _open_child_directory_nofollow(
        root_directory.descriptor,
        parent_name,
        parent_stat,
    )
    try:
        try:
            leaf_stat = os.stat(
                leaf_name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            os.fsync(parent_descriptor)
            return True
        leaf_descriptor = _open_child_directory_nofollow(
            parent_descriptor,
            leaf_name,
            leaf_stat,
        )
        try:
            root_directory.verify_current()
            current_parent = os.stat(
                parent_name,
                dir_fd=root_directory.descriptor,
                follow_symlinks=False,
            )
            current_leaf = os.stat(
                leaf_name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if (
                _identity(current_parent) != _identity(parent_stat)
                or _identity(current_leaf) != _identity(leaf_stat)
            ):
                raise MediaPathError("unsafe_media_directory")
            try:
                os.rmdir(leaf_name, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass
            except OSError as error:
                if error.errno in (errno.EEXIST, errno.ENOTEMPTY):
                    return False
                raise
            os.fsync(parent_descriptor)
            return True
        finally:
            os.close(leaf_descriptor)
    finally:
        os.close(parent_descriptor)


def _lifecycle_lock_error(code, retryable=False):
    return MediaLifecycleLockError(code, retryable=retryable)


def _require_lifecycle_lock_filename(lock_filename):
    dedup_prefix = "media-dedup-"
    dedup_suffix = ".lock"
    if lock_filename == "media-lifecycle.lock":
        return
    if not isinstance(lock_filename, str):
        raise _lifecycle_lock_error("media_lifecycle_lock_failed")
    if not (
        lock_filename.startswith(dedup_prefix)
        and lock_filename.endswith(dedup_suffix)
    ):
        raise _lifecycle_lock_error("media_lifecycle_lock_failed")
    stripe = lock_filename[len(dedup_prefix):-len(dedup_suffix)]
    if (
        len(stripe) != 2
        or any(character not in "0123456789abcdef" for character in stripe)
    ):
        raise _lifecycle_lock_error("media_lifecycle_lock_failed")


def _require_lifecycle_lock_support():
    required_flags = (
        getattr(os, "O_DIRECTORY", None),
        getattr(os, "O_NOFOLLOW", None),
        getattr(os, "O_CLOEXEC", None),
    )
    if (
        fcntl is None
        or not callable(getattr(fcntl, "flock", None))
        or any(type(flag) is not int for flag in required_flags)
        or not callable(getattr(os, "geteuid", None))
        or os.open not in getattr(os, "supports_dir_fd", set())
        or os.mkdir not in getattr(os, "supports_dir_fd", set())
        or os.stat not in getattr(os, "supports_dir_fd", set())
        or os.stat not in getattr(os, "supports_follow_symlinks", set())
    ):
        raise _lifecycle_lock_error("media_lifecycle_lock_unsupported")


def _open_media_lifecycle_lock(  # noqa: C901
    root_directory, lock_filename="media-lifecycle.lock"
):
    _require_lifecycle_lock_support()
    if not isinstance(root_directory, MediaDirectory):
        raise _lifecycle_lock_error("media_lifecycle_lock_unsupported")
    try:
        root_directory.verify_current()
        root_descriptor = root_directory.descriptor
        root_stat = os.fstat(root_descriptor)
    except BaseException as error:
        if not isinstance(error, Exception):
            raise
        raise _lifecycle_lock_error("media_lifecycle_lock_failed") from error
    if not stat.S_ISDIR(root_stat.st_mode):
        raise _lifecycle_lock_error("media_lifecycle_lock_failed")

    directory_descriptor = None
    lock_descriptor = None
    try:
        try:
            lock_directory_stat = os.stat(
                ".pinry-locks",
                dir_fd=root_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            try:
                os.mkdir(".pinry-locks", 0o700, dir_fd=root_descriptor)
            except FileExistsError:
                pass
            lock_directory_stat = os.stat(
                ".pinry-locks",
                dir_fd=root_descriptor,
                follow_symlinks=False,
            )
        _verify_lifecycle_lock_directory(lock_directory_stat)
        flags = (
            os.O_RDONLY
            | os.O_DIRECTORY
            | os.O_NOFOLLOW
            | os.O_CLOEXEC
        )
        directory_descriptor = os.open(
            ".pinry-locks", flags, dir_fd=root_descriptor
        )
        opened_directory_stat = os.fstat(directory_descriptor)
        if _identity(lock_directory_stat) != _identity(opened_directory_stat):
            raise _lifecycle_lock_error("media_lifecycle_lock_failed")
        _verify_lifecycle_lock_directory(opened_directory_stat)

        lock_descriptor = _open_lifecycle_lock_file(
            directory_descriptor,
            lock_filename,
        )
        opened_lock_stat = os.fstat(lock_descriptor)
        named_lock_stat = os.stat(
            lock_filename,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if _identity(opened_lock_stat) != _identity(named_lock_stat):
            raise _lifecycle_lock_error("media_lifecycle_lock_failed")
        _verify_lifecycle_lock_file(opened_lock_stat)
        return directory_descriptor, lock_descriptor
    except BaseException as error:
        if lock_descriptor is not None:
            try:
                os.close(lock_descriptor)
            except BaseException:
                pass
        if directory_descriptor is not None:
            try:
                os.close(directory_descriptor)
            except BaseException:
                pass
        if isinstance(error, MediaLifecycleLockError):
            raise
        if not isinstance(error, Exception):
            raise
        raise _lifecycle_lock_error("media_lifecycle_lock_failed") from error


def _open_lifecycle_lock_file(
    directory_descriptor, lock_filename="media-lifecycle.lock"
):
    existing_flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
    create_flags = existing_flags | os.O_CREAT | os.O_EXCL
    for _attempt in range(3):
        try:
            return os.open(
                lock_filename,
                existing_flags,
                dir_fd=directory_descriptor,
            )
        except FileNotFoundError:
            try:
                return os.open(
                    lock_filename,
                    create_flags,
                    0o600,
                    dir_fd=directory_descriptor,
                )
            except FileExistsError:
                continue
    raise _lifecycle_lock_error("media_lifecycle_lock_failed")


def _verify_lifecycle_lock_directory(file_stat):
    if (
        not stat.S_ISDIR(file_stat.st_mode)
        or file_stat.st_uid != os.geteuid()
        or stat.S_IMODE(file_stat.st_mode) != 0o700
    ):
        raise _lifecycle_lock_error("media_lifecycle_lock_failed")


def _verify_lifecycle_lock_file(file_stat):
    if (
        not stat.S_ISREG(file_stat.st_mode)
        or file_stat.st_uid != os.geteuid()
        or stat.S_IMODE(file_stat.st_mode) != 0o600
        or file_stat.st_nlink != 1
    ):
        raise _lifecycle_lock_error("media_lifecycle_lock_failed")


def _verify_held_media_lifecycle_lock(
    root_directory,
    directory_descriptor,
    lock_descriptor,
    lock_filename="media-lifecycle.lock",
):
    try:
        root_directory.verify_current()
        directory_stat = os.fstat(directory_descriptor)
        lock_stat = os.fstat(lock_descriptor)
        named_directory = os.stat(
            ".pinry-locks",
            dir_fd=root_directory.descriptor,
            follow_symlinks=False,
        )
        named_lock = os.stat(
            lock_filename,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if (
            _identity(directory_stat) != _identity(named_directory)
            or _identity(lock_stat) != _identity(named_lock)
        ):
            raise _lifecycle_lock_error("media_lifecycle_lock_failed")
        _verify_lifecycle_lock_directory(directory_stat)
        _verify_lifecycle_lock_file(lock_stat)
    except BaseException as error:
        if isinstance(error, MediaLifecycleLockError):
            raise
        if not isinstance(error, Exception):
            raise
        raise _lifecycle_lock_error("media_lifecycle_lock_failed") from error


def open_or_create_media_directory_from(root_directory, relative_directory):
    if not isinstance(root_directory, MediaDirectory):
        raise TypeError("root_directory must be a MediaDirectory")
    if root_directory.names:
        raise MediaPathError("unsafe_media_directory")
    root_directory.verify_current()
    components = _relative_components(relative_directory)
    descriptors = [os.dup(root_directory.descriptor)]
    names = []
    directory_stats = []
    created_components = []
    try:
        for name in components:
            parent_descriptor = descriptors[-1]
            created = False
            try:
                named_stat = os.stat(
                    name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                try:
                    os.mkdir(name, 0o755, dir_fd=parent_descriptor)
                    created = True
                except FileExistsError:
                    pass
                named_stat = os.stat(
                    name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            if stat.S_ISLNK(named_stat.st_mode):
                raise MediaPathError("media_path_escape")
            if not stat.S_ISDIR(named_stat.st_mode):
                raise MediaPathError("unsafe_media_directory")
            names.append(name)
            directory_stats.append(named_stat)
            created_components.append(created)
            child_descriptor = _open_child_directory_nofollow(
                parent_descriptor,
                name,
                named_stat,
            )
            descriptors.append(child_descriptor)
            directory_stats[-1] = os.fstat(child_descriptor)
            if created:
                os.fsync(child_descriptor)
                os.fsync(parent_descriptor)
        return MediaDirectory(
            descriptors,
            names=names,
            directory_stats=directory_stats,
            created=created_components,
            root_path=root_directory.root_path,
            root_stat=root_directory.root_stat,
        )
    except BaseException:
        partial_directory = MediaDirectory(
            descriptors,
            names=names,
            directory_stats=directory_stats,
            created=created_components,
            root_path=root_directory.root_path,
            root_stat=root_directory.root_stat,
        )
        try:
            partial_directory.remove_created_suffix(1)
        except BaseException:
            pass
        try:
            partial_directory.close()
        except BaseException:
            pass
        raise


def open_or_create_media_directory(media_root, relative_directory):
    root_directory = open_media_root(media_root)
    try:
        child_directory = open_or_create_media_directory_from(
            root_directory,
            relative_directory,
        )
    except BaseException:
        try:
            root_directory.close()
        except BaseException:
            pass
        raise
    try:
        root_directory.close()
    except BaseException:
        try:
            child_directory.close()
        except BaseException:
            pass
        raise
    return child_directory


def create_owned_staging_file(directory, name, path=None):
    if not isinstance(directory, MediaDirectory):
        raise TypeError("directory must be a MediaDirectory")
    if len(_relative_components(name)) != 1:
        raise MediaPathError("unsafe_staging_file")
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = None
    file_stat = None
    try:
        descriptor = os.open(
            name,
            flags,
            0o600,
            dir_fd=directory.descriptor,
        )
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise MediaPathError("unsafe_staging_file")
        os.fsync(directory.descriptor)
        return OwnedStagingFile(
            directory,
            name,
            descriptor,
            file_stat,
            path or name,
            owns_directory=False,
            idempotent_cleanup=True,
        )
    except BaseException:
        if descriptor is not None and file_stat is not None:
            try:
                _unlink_owned_name_if_current(
                    directory.descriptor,
                    name,
                    file_stat,
                )
                os.fsync(directory.descriptor)
            except BaseException:
                pass
        if descriptor is not None:
            try:
                _close_descriptor(descriptor)
            except BaseException:
                pass
        raise


def create_unique_staging_file(media_root):
    directory = open_or_create_media_directory(media_root, ".staging")
    name = "media-migration-{}.part".format(uuid.uuid4())
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = None
    file_stat = None
    try:
        descriptor = os.open(
            name,
            flags,
            0o600,
            dir_fd=directory.descriptor,
        )
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise MediaPathError("unsafe_staging_file")
        os.fsync(directory.descriptor)
        path = os.path.join(
            os.path.realpath(media_root),
            ".staging",
            name,
        )
        return OwnedStagingFile(
            directory,
            name,
            descriptor,
            file_stat,
            path,
        )
    except Exception:
        try:
            if descriptor is not None and file_stat is not None:
                _unlink_owned_name(
                    directory.descriptor,
                    name,
                    file_stat,
                )
                os.fsync(directory.descriptor)
        except BaseException:
            pass
        close_functions = []
        if descriptor is not None:
            close_functions.append(
                lambda: _close_descriptor(descriptor)
            )
        close_functions.append(directory.close)
        try:
            _close_all(*close_functions)
        except BaseException:
            pass
        raise
    except BaseException:
        close_functions = []
        if descriptor is not None:
            close_functions.append(
                lambda: _close_descriptor(descriptor)
            )
        close_functions.append(directory.close)
        try:
            _close_all(*close_functions)
        except BaseException:
            pass
        raise


def _link_descriptor_empty_path(
    file_descriptor, directory_descriptor, destination_name
):
    if not sys.platform.startswith("linux"):
        return False
    libc = ctypes.CDLL(None, use_errno=True)
    linkat = getattr(libc, "linkat", None)
    if linkat is None:
        return False
    linkat.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
    )
    linkat.restype = ctypes.c_int
    result = linkat(
        file_descriptor,
        b"",
        directory_descriptor,
        os.fsencode(destination_name),
        0x1000,
    )
    if result == 0:
        return True
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise FileExistsError(error_number, os.strerror(error_number))
    if error_number in (
        errno.EINVAL,
        errno.ENOENT,
        errno.ENOSYS,
        errno.ENOTSUP,
        errno.EPERM,
    ):
        return False
    raise OSError(error_number, os.strerror(error_number))


def _link_descriptor_proc(
    file_descriptor, directory_descriptor, destination_name
):
    if not sys.platform.startswith("linux"):
        return False
    try:
        os.link(
            "/proc/self/fd/{}".format(file_descriptor),
            destination_name,
            dst_dir_fd=directory_descriptor,
            follow_symlinks=True,
        )
    except FileExistsError:
        raise
    except OSError as error:
        if error.errno in (
            errno.EACCES,
            errno.ENOENT,
            errno.ENOSYS,
            errno.ENOTSUP,
            errno.EPERM,
            errno.EXDEV,
        ):
            return False
        raise
    return True


def _clone_descriptor_noreplace(
    file_descriptor, directory_descriptor, destination_name
):
    if sys.platform != "darwin":
        return False
    libc = ctypes.CDLL(None, use_errno=True)
    fclonefileat = getattr(libc, "fclonefileat", None)
    if fclonefileat is None:
        return False
    fclonefileat.argtypes = (
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint32,
    )
    fclonefileat.restype = ctypes.c_int
    result = fclonefileat(
        file_descriptor,
        directory_descriptor,
        os.fsencode(destination_name),
        0,
    )
    if result == 0:
        return True
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise FileExistsError(error_number, os.strerror(error_number))
    if error_number in (
        errno.EINVAL,
        errno.ENOSYS,
        errno.ENOTSUP,
        errno.EPERM,
        errno.EXDEV,
    ):
        return False
    raise OSError(error_number, os.strerror(error_number))


def _publish_descriptor(
    file_descriptor, directory_descriptor, destination_name
):
    if sys.platform.startswith("linux"):
        return _publish_linux_descriptor(
            file_descriptor,
            directory_descriptor,
            destination_name,
        )
    elif sys.platform == "darwin":
        if _clone_descriptor_noreplace(
            file_descriptor, directory_descriptor, destination_name
        ):
            return
    raise MediaPathError("atomic_publish_unsupported")


def _publish_linux_descriptor(
    file_descriptor,
    directory_descriptor,
    destination_name,
):
    if not sys.platform.startswith("linux"):
        raise MediaPathError("atomic_publish_unsupported")
    if _link_descriptor_empty_path(
        file_descriptor, directory_descriptor, destination_name
    ):
        return
    if _link_descriptor_proc(
        file_descriptor, directory_descriptor, destination_name
    ):
        return
    raise MediaPathError("atomic_publish_unsupported")


def open_staging_noreplace(part_path):
    parent = os.path.dirname(part_path)
    name = os.path.basename(part_path)
    parent_descriptor = _open_directory(parent)
    try:
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(
                name, flags, 0o600, dir_fd=parent_descriptor
            )
        except FileExistsError:
            raise MediaPathError("unsafe_staging_file")
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            os.close(descriptor)
            raise MediaPathError("unsafe_staging_file")
        return descriptor
    finally:
        os.close(parent_descriptor)


def publish_noreplace(
    part_path,
    destination,
    expected_sha256,
    part_descriptor=None,
    part_directory_descriptor=None,
    destination_directory=None,
    capture_partial=False,
):
    part_name = os.path.basename(part_path)
    destination_name = os.path.basename(destination)
    opened_part_directory = False
    if part_directory_descriptor is None:
        part_directory_descriptor = _open_directory(
            os.path.realpath(os.path.dirname(part_path))
        )
        opened_part_directory = True
    opened_destination_directory = False
    if destination_directory is None:
        destination_directory_descriptor = _open_directory(
            os.path.realpath(os.path.dirname(destination))
        )
        opened_destination_directory = True
    else:
        destination_directory_descriptor = destination_directory.descriptor
    opened_part = False
    try:
        if part_descriptor is None:
            part_descriptor = _open_regular_nofollow(
                part_directory_descriptor, part_name
            )
            opened_part = True
        part_stat = os.fstat(part_descriptor)
        _require_owned_name(
            part_directory_descriptor,
            part_name,
            part_stat,
        )
        if sha256_file_descriptor(part_descriptor) != expected_sha256:
            raise MediaPathError("part_hash_mismatch")
        return _publish_verified_descriptor(
            part_descriptor,
            part_directory_descriptor,
            part_name,
            part_stat,
            destination_directory_descriptor,
            destination_directory,
            destination_name,
            expected_sha256,
            capture_partial,
        )
    finally:
        close_functions = []
        if opened_part:
            close_functions.append(
                lambda: _close_descriptor(part_descriptor)
            )
        if opened_destination_directory:
            close_functions.append(
                lambda: _close_descriptor(destination_directory_descriptor)
            )
        if opened_part_directory:
            close_functions.append(
                lambda: _close_descriptor(part_directory_descriptor)
            )
        _close_all(*close_functions)


def _publish_verified_descriptor(
    part_descriptor,
    part_directory_descriptor,
    part_name,
    part_stat,
    destination_directory_descriptor,
    destination_directory,
    destination_name,
    expected_sha256,
    capture_partial,
):
    try:
        _publish_descriptor(
            part_descriptor,
            destination_directory_descriptor,
            destination_name,
        )
    except FileExistsError:
        destination_stat = _verify_existing_destination(
            destination_directory_descriptor,
            destination_name,
            expected_sha256,
        )
        result = PublishResult(
            reused=True,
            destination_stat=destination_stat,
        )
    else:
        result = _verify_created_destination(
            destination_directory_descriptor,
            destination_name,
            expected_sha256,
            capture_partial,
        )
    return _finish_published_name(
        result,
        destination_directory_descriptor,
        destination_directory,
        part_directory_descriptor,
        part_name,
        part_stat,
        capture_partial,
    )


def _verify_created_destination(
    destination_directory_descriptor,
    destination_name,
    expected_sha256,
    capture_partial,
):
    destination_descriptor = _open_regular_nofollow(
        destination_directory_descriptor,
        destination_name,
    )
    result = None
    try:
        destination_stat = os.fstat(destination_descriptor)
        result = PublishResult(
            created=True,
            destination_stat=destination_stat,
        )
        if sha256_file_descriptor(destination_descriptor) != expected_sha256:
            raise MediaPathError("published_hash_mismatch")
        os.fsync(destination_descriptor)
        return result
    except BaseException as error:
        _raise_publish_failure(result, error, capture_partial)
    finally:
        os.close(destination_descriptor)


def _finish_published_name(
    result,
    destination_directory_descriptor,
    destination_directory,
    part_directory_descriptor,
    part_name,
    part_stat,
    capture_partial,
):
    try:
        if destination_directory is None:
            os.fsync(destination_directory_descriptor)
        else:
            destination_directory.fsync_publish()
        _unlink_owned_name(
            part_directory_descriptor,
            part_name,
            part_stat,
        )
        os.fsync(part_directory_descriptor)
    except BaseException as error:
        _raise_publish_failure(result, error, capture_partial)
    return result


def _raise_publish_failure(result, error, capture_partial):
    if capture_partial and result is not None:
        raise PublishFailure(result, error) from error
    raise error


def publish_owned_noreplace(
    staging_file,
    destination_directory,
    destination_name,
    expected_sha256,
):
    if not isinstance(staging_file, OwnedStagingFile):
        raise TypeError("staging_file must be an OwnedStagingFile")
    if not isinstance(destination_directory, MediaDirectory):
        raise TypeError("destination_directory must be a MediaDirectory")
    if staging_file._closed:
        raise MediaPathError("unsafe_staging_file")
    if len(_relative_components(destination_name)) != 1:
        raise MediaPathError("media_path_escape")
    if not sys.platform.startswith("linux"):
        raise MediaPathError("atomic_publish_unsupported")
    staging_file.directory.verify_current()
    destination_directory.verify_current()
    part_stat = os.fstat(staging_file.descriptor)
    _require_owned_name(
        staging_file.directory.descriptor,
        staging_file.name,
        part_stat,
    )
    if sha256_file_descriptor(staging_file.descriptor) != expected_sha256:
        raise MediaPathError("part_hash_mismatch")
    return _publish_owned_verified_descriptor(
        staging_file.descriptor,
        staging_file.directory.descriptor,
        staging_file.name,
        part_stat,
        destination_directory,
        destination_name,
        expected_sha256,
    )


def _publish_owned_verified_descriptor(
    part_descriptor,
    part_directory_descriptor,
    part_name,
    part_stat,
    destination_directory,
    destination_name,
    expected_sha256,
):
    try:
        _publish_linux_descriptor(
            part_descriptor,
            destination_directory.descriptor,
            destination_name,
        )
    except FileExistsError:
        destination_stat = _verify_existing_destination(
            destination_directory.descriptor,
            destination_name,
            expected_sha256,
        )
        result = PublishResult(
            reused=True,
            destination_stat=destination_stat,
        )
    else:
        result = PublishResult(
            created=True,
            destination_stat=part_stat,
        )
        try:
            destination_directory.verify_current()
            _require_owned_name(
                destination_directory.descriptor,
                destination_name,
                part_stat,
            )
            os.fsync(part_descriptor)
            destination_directory.verify_current()
            _require_owned_name(
                destination_directory.descriptor,
                destination_name,
                part_stat,
            )
        except BaseException as error:
            raise PublishFailure(result, error) from error
    return _finish_published_name(
        result,
        destination_directory.descriptor,
        destination_directory,
        part_directory_descriptor,
        part_name,
        part_stat,
        True,
    )


def _open_directory(path):
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return os.open(path, flags)


def _open_child_directory_nofollow(
    parent_descriptor, name, named_stat
):
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = None
    try:
        descriptor = os.open(
            name,
            flags,
            dir_fd=parent_descriptor,
        )
        opened_stat = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(opened_stat.st_mode)
            or _identity(opened_stat) != _identity(named_stat)
        ):
            raise MediaPathError("unsafe_media_directory")
        return descriptor
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        raise


def _open_regular_nofollow(directory_descriptor, name):
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    try:
        descriptor = os.open(name, flags, dir_fd=directory_descriptor)
    except OSError as error:
        raise MediaPathError("unsafe_staging_file") from error
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise MediaPathError("unsafe_staging_file")
    return descriptor


def _identity(file_stat):
    return file_stat.st_dev, file_stat.st_ino


def _require_owned_name(directory_descriptor, name, expected_stat):
    try:
        current = os.stat(
            name, dir_fd=directory_descriptor, follow_symlinks=False
        )
    except OSError as error:
        raise MediaPathError("unsafe_staging_file") from error
    if not stat.S_ISREG(current.st_mode) or _identity(current) != _identity(
        expected_stat
    ):
        raise MediaPathError("unsafe_staging_file")


def _verify_existing_destination(
    directory_descriptor, name, expected_sha256
):
    descriptor = _open_regular_nofollow(directory_descriptor, name)
    try:
        destination_stat = os.fstat(descriptor)
        if sha256_file_descriptor(descriptor) != expected_sha256:
            raise MediaPathError("media_path_conflict")
        os.fsync(descriptor)
        return destination_stat
    finally:
        os.close(descriptor)


def _unlink_owned_name(directory_descriptor, name, expected_stat):
    _require_owned_name(directory_descriptor, name, expected_stat)
    os.unlink(name, dir_fd=directory_descriptor)


def _unlink_owned_name_if_current(
    directory_descriptor,
    name,
    expected_stat,
    missing_ok=False,
):
    status = _unlink_owned_name_if_current_status(
        directory_descriptor,
        name,
        expected_stat,
    )
    return status == "removed" or (
        missing_ok and status == "missing"
    )


def _unlink_owned_name_if_current_status(
    directory_descriptor,
    name,
    expected_stat,
):
    try:
        current = os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "preserved"
    if (
        not stat.S_ISREG(current.st_mode)
        or _identity(current) != _identity(expected_stat)
    ):
        return "preserved"
    try:
        os.unlink(name, dir_fd=directory_descriptor)
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "preserved"
    return "removed"


def verify_published_name(
    directory,
    name,
    expected_stat,
    expected_sha256,
):
    if not isinstance(directory, MediaDirectory):
        raise TypeError("directory must be a MediaDirectory")
    directory.verify_current()
    descriptor = _open_regular_nofollow(directory.descriptor, name)
    try:
        current = os.fstat(descriptor)
        if (
            _identity(current) != _identity(expected_stat)
            or current.st_size != expected_stat.st_size
            or sha256_file_descriptor(descriptor) != expected_sha256
        ):
            raise MediaPathError("media_path_conflict")
        verify_published_identity(directory, name, expected_stat)
    finally:
        os.close(descriptor)
    return True


def verify_published_identity(directory, name, expected_stat):
    if not isinstance(directory, MediaDirectory):
        raise TypeError("directory must be a MediaDirectory")
    directory.verify_current()
    _require_owned_name(directory.descriptor, name, expected_stat)
    return True


def unlink_published_name_if_current(
    directory,
    name,
    expected_stat,
    missing_ok=False,
):
    if not isinstance(directory, MediaDirectory):
        raise TypeError("directory must be a MediaDirectory")
    try:
        directory.verify_current()
    except MediaPathError:
        return False
    status = _unlink_owned_name_if_current_status(
        directory.descriptor,
        name,
        expected_stat,
    )
    if status == "removed":
        try:
            directory.fsync_publish()
        except OSError:
            pass
    return status == "removed" or (
        missing_ok and status == "missing"
    )
