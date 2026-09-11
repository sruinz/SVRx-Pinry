import errno
import os
import stat
import sys

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

    def verify_held(self):
        try:
            descriptor = self.fileno()
            _verify_fd_lock(descriptor)
            _verify_held_startup_lock(
                self._data_root, descriptor, self._lock_stat,
                self._owner_uid, self._owner_gid,
            )
            _verify_fd_lock(descriptor)
        except (OSError, ValueError) as error:
            raise StartupLockError("startup_lock_failed") from error

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


def _verify_fd_lock(descriptor):
    # 같은 파일의 다른 open description이나 해제된 FD는 증거가 없다.
    if not sys.platform.startswith("linux"):
        raise StartupLockError("startup_lock_failed")
    try:
        info = os.fstat(descriptor)
        path = "/proc/self/fdinfo/{}".format(descriptor)
        with open(path, "r", encoding="ascii") as source:
            payload = source.read(16385)
        locks = [
            line.split() for line in payload.splitlines()
            if line.startswith("lock:")
        ]
        if len(payload) > 16384 or len(locks) != 1:
            raise ValueError("잠금 증거 누락")
        fields = locks[0]
        if (
            len(fields) != 9 or not fields[1].endswith(":")
            or not fields[1][:-1].isdigit()
            or fields[2:5] != ["FLOCK", "ADVISORY", "WRITE"]
            or int(fields[5]) != os.getpid()
            or fields[7:] != ["0", "EOF"]
        ):
            raise ValueError("잠금 증거 불일치")
        major, minor, inode = fields[6].split(":")
        if (int(major, 16), int(minor, 16), int(inode)) != (
            os.major(info.st_dev), os.minor(info.st_dev), info.st_ino,
        ):
            raise ValueError("잠금 파일 불일치")
    except (OSError, UnicodeError, ValueError) as error:
        raise StartupLockError("startup_lock_failed") from error


def acquire_startup_lock(data_root, service_uid=None, service_gid=None):
    _require_startup_lock_support()
    if not (
        (service_uid is None and service_gid is None)
        or (
            type(service_uid) is int
            and service_uid >= 0
            and type(service_gid) is int
            and service_gid >= 0
        )
    ):
        raise StartupLockError("startup_lock_failed")
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
        _verify_named_startup_lock_structure(
            root_directory,
            lock_descriptor,
            opened_stat,
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
        if not created:
            _normalize_persistent_startup_lock(
                root_directory,
                lock_descriptor,
                opened_stat,
                owner_uid,
                owner_gid,
                service_uid,
                service_gid,
            )
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


def _verify_startup_lock_structure(file_stat):
    if (
        not stat.S_ISREG(file_stat.st_mode)
        or file_stat.st_nlink != 1
    ):
        raise StartupLockError("startup_lock_failed")


def _verify_startup_lock_stat(file_stat, owner_uid, owner_gid):
    _verify_startup_lock_structure(file_stat)
    if (
        stat.S_IMODE(file_stat.st_mode) != 0o600
        or file_stat.st_uid != owner_uid
        or file_stat.st_gid != owner_gid
    ):
        raise StartupLockError("startup_lock_failed")


def _identity(file_stat):
    return file_stat.st_dev, file_stat.st_ino


def _verify_named_startup_lock_structure(
    root_directory,
    lock_descriptor,
    expected_stat,
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
    _verify_startup_lock_structure(descriptor_stat)
    _verify_startup_lock_structure(named_stat)
    return descriptor_stat, named_stat


def _verify_named_startup_lock(
    root_directory,
    lock_descriptor,
    expected_stat,
    owner_uid,
    owner_gid,
):
    descriptor_stat, named_stat = _verify_named_startup_lock_structure(
        root_directory,
        lock_descriptor,
        expected_stat,
    )
    _verify_startup_lock_stat(descriptor_stat, owner_uid, owner_gid)
    _verify_startup_lock_stat(named_stat, owner_uid, owner_gid)


def _normalize_persistent_startup_lock(
    root_directory,
    lock_descriptor,
    expected_stat,
    owner_uid,
    owner_gid,
    service_uid,
    service_gid,
):
    descriptor_stat, named_stat = _verify_named_startup_lock_structure(
        root_directory,
        lock_descriptor,
        expected_stat,
    )
    try:
        _verify_startup_lock_stat(
            descriptor_stat,
            owner_uid,
            owner_gid,
        )
        _verify_startup_lock_stat(named_stat, owner_uid, owner_gid)
        return
    except StartupLockError:
        pass

    lock_owner = descriptor_stat.st_uid, descriptor_stat.st_gid
    named_owner = named_stat.st_uid, named_stat.st_gid
    lock_mode = stat.S_IMODE(descriptor_stat.st_mode)
    named_mode = stat.S_IMODE(named_stat.st_mode)
    try:
        root_stat = os.fstat(root_directory.descriptor)
    except OSError as error:
        raise StartupLockError("startup_lock_failed") from error
    data_owner = root_stat.st_uid, root_stat.st_gid
    current_owner = owner_uid, owner_gid
    service_owner = service_uid, service_gid
    same_owner_mode_drift = (
        lock_owner == current_owner
        and named_owner == current_owner
        and lock_mode == 0o700
        and named_mode == 0o700
    )
    root_service_owner_drift = (
        owner_uid == 0
        and service_uid is not None
        and service_owner != current_owner
        and data_owner == service_owner
        and lock_owner == service_owner
        and named_owner == service_owner
        and lock_mode in (0o600, 0o700)
        and named_mode == lock_mode
    )
    if (
        descriptor_stat.st_size != 0
        or named_stat.st_size != 0
        or not (same_owner_mode_drift or root_service_owner_drift)
    ):
        raise StartupLockError("startup_lock_failed")

    try:
        if lock_owner != current_owner:
            os.fchown(lock_descriptor, owner_uid, owner_gid)
        os.fchmod(lock_descriptor, 0o600)
        os.fsync(lock_descriptor)
        os.fsync(root_directory.descriptor)
    except OSError as error:
        raise StartupLockError("startup_lock_failed") from error
    _verify_named_startup_lock(
        root_directory,
        lock_descriptor,
        expected_stat,
        owner_uid,
        owner_gid,
    )


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
