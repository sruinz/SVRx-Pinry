import errno
import os
import stat

from django_images import file_ops

try:
    import fcntl
except ImportError:  # pragma: no cover - platform gate
    fcntl = None


STARTUP_LOCK_FILENAME = ".svrx-pinry-startup.lock"


class StartupLockError(Exception):
    def __init__(self, code, retryable=False):
        super(StartupLockError, self).__init__(code)
        self.code = code
        self.retryable = retryable


class StartupLock(object):
    def __init__(
        self,
        data_root,
        lock_descriptor,
        lock_stat,
        owner_uid,
        owner_gid,
    ):
        self._data_root = data_root
        self._lock_descriptor = lock_descriptor
        self._lock_stat = lock_stat
        self._owner_uid = owner_uid
        self._owner_gid = owner_gid

    def __enter__(self):
        if self._lock_descriptor is None:
            raise StartupLockError("startup_lock_failed")
        return self

    def __exit__(self, error_type, error, traceback):
        del error, traceback
        try:
            self.close()
        except BaseException:
            if error_type is None:
                raise
        return False

    def fileno(self):
        if self._lock_descriptor is None:
            raise ValueError("startup lock is closed")
        return self._lock_descriptor

    def set_inheritable(self, inheritable):
        if type(inheritable) is not bool:
            raise TypeError("inheritable must be a bool")
        descriptor = self.fileno()
        if not inheritable:
            os.set_inheritable(descriptor, False)
            return
        try:
            _verify_held_startup_lock(
                self._data_root,
                descriptor,
                self._lock_stat,
                self._owner_uid,
                self._owner_gid,
            )
            os.set_inheritable(descriptor, True)
            _verify_held_startup_lock(
                self._data_root,
                descriptor,
                self._lock_stat,
                self._owner_uid,
                self._owner_gid,
            )
        except BaseException:
            try:
                os.set_inheritable(descriptor, False)
            except BaseException:
                pass
            raise

    def close(self):
        first_error = None
        if self._lock_descriptor is not None:
            descriptor = self._lock_descriptor
            self._lock_descriptor = None
            try:
                os.close(descriptor)
            except BaseException as error:
                first_error = error
        if self._data_root is not None:
            data_root = self._data_root
            self._data_root = None
            try:
                data_root.close()
            except BaseException as error:
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error


def acquire_startup_lock(data_root):
    _require_startup_lock_support()
    owner_uid = os.geteuid()
    owner_gid = os.getegid()
    root_directory = None
    lock_descriptor = None
    created = False
    opened_stat = None
    try:
        root_directory = file_ops.open_verified_media_root(data_root)
        lock_descriptor, created = _open_startup_lock(
            root_directory.descriptor
        )
        os.set_inheritable(lock_descriptor, False)
        opened_stat = os.fstat(lock_descriptor)
        if created:
            os.fchown(lock_descriptor, owner_uid, owner_gid)
            os.fchmod(lock_descriptor, 0o600)
            os.fsync(lock_descriptor)
            os.fsync(root_directory.descriptor)
            opened_stat = os.fstat(lock_descriptor)
        _verify_named_startup_lock(
            root_directory,
            lock_descriptor,
            opened_stat,
            owner_uid,
            owner_gid,
        )
        try:
            fcntl.flock(
                lock_descriptor,
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN):
                raise StartupLockError(
                    "startup_lock_busy", retryable=True
                )
            raise StartupLockError("startup_lock_failed") from error
        _verify_held_startup_lock(
            root_directory,
            lock_descriptor,
            opened_stat,
            owner_uid,
            owner_gid,
        )
        acquired = StartupLock(
            root_directory,
            lock_descriptor,
            opened_stat,
            owner_uid,
            owner_gid,
        )
        root_directory = None
        lock_descriptor = None
        return acquired
    except BaseException as error:
        if lock_descriptor is not None:
            try:
                os.close(lock_descriptor)
            except BaseException:
                pass
        if root_directory is not None:
            try:
                root_directory.close()
            except BaseException:
                pass
        if isinstance(error, StartupLockError):
            raise
        if not isinstance(error, Exception):
            raise
        raise StartupLockError("startup_lock_failed") from error


def _require_startup_lock_support():
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
        or not callable(getattr(os, "getegid", None))
        or not callable(getattr(os, "fchmod", None))
        or not callable(getattr(os, "fchown", None))
        or os.open not in getattr(os, "supports_dir_fd", set())
        or os.stat not in getattr(os, "supports_dir_fd", set())
        or os.stat not in getattr(os, "supports_follow_symlinks", set())
    ):
        raise StartupLockError("startup_lock_unsupported")


def _open_startup_lock(directory_descriptor):
    existing_flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
    if hasattr(os, "O_NONBLOCK"):
        existing_flags |= os.O_NONBLOCK
    create_flags = existing_flags | os.O_CREAT | os.O_EXCL
    for _attempt in range(3):
        try:
            return (
                os.open(
                    STARTUP_LOCK_FILENAME,
                    existing_flags,
                    dir_fd=directory_descriptor,
                ),
                False,
            )
        except FileNotFoundError:
            try:
                return (
                    os.open(
                        STARTUP_LOCK_FILENAME,
                        create_flags,
                        0o600,
                        dir_fd=directory_descriptor,
                    ),
                    True,
                )
            except FileExistsError:
                continue
    raise StartupLockError("startup_lock_failed")


def _verify_startup_lock_stat(file_stat, owner_uid, owner_gid):
    if (
        not stat.S_ISREG(file_stat.st_mode)
        or stat.S_IMODE(file_stat.st_mode) != 0o600
        or file_stat.st_nlink != 1
        or file_stat.st_uid != owner_uid
        or file_stat.st_gid != owner_gid
    ):
        raise StartupLockError("startup_lock_failed")


def _identity(file_stat):
    return file_stat.st_dev, file_stat.st_ino


def _verify_named_startup_lock(
    root_directory,
    lock_descriptor,
    expected_stat,
    owner_uid,
    owner_gid,
):
    try:
        root_directory.verify_current()
        descriptor_stat = os.fstat(lock_descriptor)
        named_stat = os.stat(
            STARTUP_LOCK_FILENAME,
            dir_fd=root_directory.descriptor,
            follow_symlinks=False,
        )
    except (OSError, file_ops.MediaPathError) as error:
        raise StartupLockError("startup_lock_failed") from error
    if (
        _identity(descriptor_stat) != _identity(expected_stat)
        or _identity(named_stat) != _identity(expected_stat)
    ):
        raise StartupLockError("startup_lock_failed")
    _verify_startup_lock_stat(descriptor_stat, owner_uid, owner_gid)
    _verify_startup_lock_stat(named_stat, owner_uid, owner_gid)


def _verify_held_startup_lock(
    root_directory,
    lock_descriptor,
    expected_stat,
    owner_uid,
    owner_gid,
):
    try:
        _verify_named_startup_lock(
            root_directory,
            lock_descriptor,
            expected_stat,
            owner_uid,
            owner_gid,
        )
    except StartupLockError:
        raise
    except BaseException as error:
        if not isinstance(error, Exception):
            raise
        raise StartupLockError("startup_lock_failed") from error
