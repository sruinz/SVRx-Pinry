import ctypes
from dataclasses import dataclass
import errno
import hashlib
import os
import stat
import sys

from django.conf import settings

from django_images.file_ops import (
    MediaDirectory,
    MediaPathError,
    media_global_writer_gate,
    open_verified_media_file,
    open_verified_media_root,
    rename_media_noreplace,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - Unix 전용 작업자 계약
    fcntl = None


_FICLONE = 0x40049409
_REFLINK_UNSUPPORTED = frozenset((
    errno.EINVAL,
    errno.ENOSYS,
    errno.ENOTSUP,
    errno.EOPNOTSUPP,
    errno.ENOTTY,
    errno.EXDEV,
))
_COPY_CHUNK_SIZE = 1024 * 1024


class ExportStorageError(Exception):
    def __init__(self, code):
        self.code = code
        super(ExportStorageError, self).__init__(code)
        self.__suppress_context__ = True


def _storage_error(code):
    return ExportStorageError(code)


def _mode(file_stat):
    return stat.S_IMODE(file_stat.st_mode)


def _identity(file_stat):
    return file_stat.st_dev, file_stat.st_ino


def _mtime_ns(file_stat):
    return file_stat.st_mtime_ns


def _ctime_ns(file_stat):
    return file_stat.st_ctime_ns


def _valid_sha256(value):
    return (
        value is None
        or (
            type(value) is str
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
        )
    )


def _require_leaf_name(name):
    if (
        not isinstance(name, str)
        or not name
        or name in (".", "..")
        or "/" in name
        or "\\" in name
        or "\x00" in name
    ):
        raise _storage_error("export_storage_unsafe")
    return name


@dataclass(frozen=True)
class SourceReceipt(object):
    dev: int
    ino: int
    uid: int
    gid: int
    mode: int
    nlink: int
    size: int
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def from_fd(cls, descriptor):
        try:
            current = os.fstat(descriptor)
        except OSError as error:
            raise _storage_error("source_unsafe") from error
        if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
            raise _storage_error("source_unsafe")
        return cls(
            current.st_dev,
            current.st_ino,
            current.st_uid,
            current.st_gid,
            _mode(current),
            current.st_nlink,
            current.st_size,
            _mtime_ns(current),
            _ctime_ns(current),
        )

    def verify_identity(self, descriptor):
        try:
            current = os.fstat(descriptor)
        except OSError as error:
            raise _storage_error("source_changed") from error
        if (
            not stat.S_ISREG(current.st_mode)
            or (
                current.st_dev,
                current.st_ino,
                current.st_uid,
                current.st_gid,
                _mode(current),
                current.st_nlink,
                current.st_size,
                _mtime_ns(current),
                _ctime_ns(current),
            ) != (
                self.dev,
                self.ino,
                self.uid,
                self.gid,
                self.mode,
                self.nlink,
                self.size,
                self.mtime_ns,
                self.ctime_ns,
            )
        ):
            raise _storage_error("source_changed")
        return True


@dataclass(frozen=True)
class OpenFileReceipt(object):
    dev: int
    ino: int
    uid: int
    gid: int
    mode: int
    nlink: int

    @classmethod
    def from_fd(cls, descriptor, expected_uid, expected_gid):
        try:
            current = os.fstat(descriptor)
        except OSError as error:
            raise _storage_error("export_storage_unsafe") from error
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_uid != expected_uid
            or current.st_gid != expected_gid
            or _mode(current) != 0o600
            or current.st_nlink != 1
        ):
            raise _storage_error("export_storage_unsafe")
        return cls(
            current.st_dev,
            current.st_ino,
            current.st_uid,
            current.st_gid,
            _mode(current),
            current.st_nlink,
        )

    def matches_stat(self, current):
        return (
            stat.S_ISREG(current.st_mode)
            and (
                current.st_dev,
                current.st_ino,
                current.st_uid,
                current.st_gid,
                _mode(current),
                current.st_nlink,
            ) == (
                self.dev,
                self.ino,
                self.uid,
                self.gid,
                self.mode,
                self.nlink,
            )
        )

    def verify_identity(self, descriptor):
        try:
            current = os.fstat(descriptor)
        except OSError as error:
            raise _storage_error("export_storage_unsafe") from error
        if not self.matches_stat(current):
            raise _storage_error("export_storage_unsafe")
        return True


@dataclass(frozen=True)
class ClosedFileReceipt(object):
    dev: int
    ino: int
    uid: int
    gid: int
    mode: int
    nlink: int
    size: int
    mtime_ns: int
    ctime_ns: int
    sha256: object = None

    @classmethod
    def from_open_fd(cls, descriptor, open_receipt, sha256=None):
        if not isinstance(open_receipt, OpenFileReceipt):
            raise TypeError("open_receipt must be an OpenFileReceipt")
        if not _valid_sha256(sha256):
            raise ValueError("invalid_sha256")
        open_receipt.verify_identity(descriptor)
        try:
            current = os.fstat(descriptor)
        except OSError as error:
            raise _storage_error("export_storage_unsafe") from error
        if not open_receipt.matches_stat(current):
            raise _storage_error("export_storage_unsafe")
        return cls(
            open_receipt.dev,
            open_receipt.ino,
            open_receipt.uid,
            open_receipt.gid,
            open_receipt.mode,
            open_receipt.nlink,
            current.st_size,
            _mtime_ns(current),
            _ctime_ns(current),
            sha256,
        )

    def matches_stat(self, current):
        return (
            stat.S_ISREG(current.st_mode)
            and (
                current.st_dev,
                current.st_ino,
                current.st_uid,
                current.st_gid,
                _mode(current),
                current.st_nlink,
                current.st_size,
                _mtime_ns(current),
                _ctime_ns(current),
            ) == (
                self.dev,
                self.ino,
                self.uid,
                self.gid,
                self.mode,
                self.nlink,
                self.size,
                self.mtime_ns,
                self.ctime_ns,
            )
        )

    def verify_identity(self, descriptor):
        try:
            current = os.fstat(descriptor)
        except OSError as error:
            raise _storage_error("export_storage_unsafe") from error
        if not self.matches_stat(current):
            raise _storage_error("export_storage_unsafe")
        return True


@dataclass(frozen=True)
class DirectoryReceipt(object):
    dev: int
    ino: int
    uid: int
    gid: int
    mode: int

    @classmethod
    def from_fd(cls, descriptor, expected_uid, expected_gid):
        try:
            current = os.fstat(descriptor)
        except OSError as error:
            raise _storage_error("export_storage_unsafe") from error
        if (
            not stat.S_ISDIR(current.st_mode)
            or current.st_uid != expected_uid
            or current.st_gid != expected_gid
            or _mode(current) != 0o700
        ):
            raise _storage_error("export_storage_unsafe")
        return cls(
            current.st_dev,
            current.st_ino,
            current.st_uid,
            current.st_gid,
            _mode(current),
        )

    def matches_stat(self, current):
        return (
            stat.S_ISDIR(current.st_mode)
            and (
                current.st_dev,
                current.st_ino,
                current.st_uid,
                current.st_gid,
                _mode(current),
            ) == (
                self.dev,
                self.ino,
                self.uid,
                self.gid,
                self.mode,
            )
        )

    def verify_identity(self, descriptor):
        try:
            current = os.fstat(descriptor)
        except OSError as error:
            raise _storage_error("export_storage_unsafe") from error
        if not self.matches_stat(current):
            raise _storage_error("export_storage_unsafe")
        return True


class ExportDirectory(object):
    def __init__(
        self,
        descriptor,
        receipt,
        parent=None,
        name=None,
        verified_root=None,
    ):
        self._descriptor = descriptor
        self.receipt = receipt
        self.parent = parent
        self.name = name
        self.verified_root = verified_root
        self.uid = receipt.uid
        self.gid = receipt.gid
        self._closed = False

    @property
    def descriptor(self):
        if self._closed or self._descriptor is None:
            raise _storage_error("export_storage_unsafe")
        return self._descriptor

    def verify_identity(self):
        descriptor = self.descriptor
        if self.verified_root is not None:
            try:
                self.verified_root.verify_current()
            except (MediaPathError, OSError) as error:
                raise _storage_error("export_storage_unsafe") from error
        if self.parent is not None:
            self.parent.verify_identity()
            try:
                named = os.stat(
                    self.name,
                    dir_fd=self.parent.descriptor,
                    follow_symlinks=False,
                )
            except OSError as error:
                raise _storage_error("export_storage_unsafe") from error
            if not self.receipt.matches_stat(named):
                raise _storage_error("export_storage_unsafe")
        self.receipt.verify_identity(descriptor)
        return True

    def close(self):
        if self._closed:
            return
        self._closed = True
        descriptor = self._descriptor
        self._descriptor = None
        if self.verified_root is not None:
            self.verified_root.close()
        elif descriptor is not None:
            os.close(descriptor)

    def __enter__(self):
        return self

    def __exit__(self, error_type, error, traceback):
        del error_type, error, traceback
        self.close()
        return False


def open_export_root(path, uid, gid):
    verified_root = None
    try:
        verified_root = open_verified_media_root(path)
        receipt = DirectoryReceipt.from_fd(
            verified_root.descriptor,
            uid,
            gid,
        )
        directory = ExportDirectory(
            verified_root.descriptor,
            receipt,
            verified_root=verified_root,
        )
        directory.verify_identity()
        verified_root = None
        return directory
    except BaseException as error:
        if verified_root is not None:
            try:
                verified_root.close()
            except BaseException:
                pass
        if isinstance(error, ExportStorageError):
            raise
        if not isinstance(error, Exception):
            raise
        raise _storage_error("export_storage_unsafe") from None


def _directory_open_flags():
    required = (
        getattr(os, "O_DIRECTORY", None),
        getattr(os, "O_NOFOLLOW", None),
    )
    if any(type(flag) is not int for flag in required):
        raise _storage_error("export_storage_unsafe")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    return flags


def _open_named_directory(parent, name, receipt):
    descriptor = None
    try:
        named = os.stat(
            name,
            dir_fd=parent.descriptor,
            follow_symlinks=False,
        )
        if not receipt.matches_stat(named):
            raise _storage_error("export_storage_unsafe")
        descriptor = os.open(
            name,
            _directory_open_flags(),
            dir_fd=parent.descriptor,
        )
        receipt.verify_identity(descriptor)
        parent.verify_identity()
        return descriptor
    except BaseException:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except BaseException:
                pass
        raise


def open_receipted_directory(parent, name, receipt):
    _require_leaf_name(name)
    if not isinstance(parent, ExportDirectory):
        raise TypeError("parent must be an ExportDirectory")
    if not isinstance(receipt, DirectoryReceipt):
        raise TypeError("receipt must be a DirectoryReceipt")
    parent.verify_identity()
    try:
        descriptor = _open_named_directory(parent, name, receipt)
        directory = ExportDirectory(
            descriptor,
            receipt,
            parent=parent,
            name=name,
        )
        directory.verify_identity()
        return directory
    except ExportStorageError:
        raise
    except Exception:
        raise _storage_error("export_storage_unsafe") from None


def _remove_created_directory(parent, name, created_stat):
    try:
        current = os.stat(
            name,
            dir_fd=parent.descriptor,
            follow_symlinks=False,
        )
        if (
            stat.S_ISDIR(current.st_mode)
            and _identity(current) == _identity(created_stat)
        ):
            os.rmdir(name, dir_fd=parent.descriptor)
            os.fsync(parent.descriptor)
    except BaseException:
        pass


def create_private_directory(parent, name, uid, gid):
    _require_leaf_name(name)
    if not isinstance(parent, ExportDirectory):
        raise TypeError("parent must be an ExportDirectory")
    parent.verify_identity()
    created = False
    created_stat = None
    descriptor = None
    receipt = None
    try:
        os.mkdir(name, 0o700, dir_fd=parent.descriptor)
        created = True
        named = os.stat(
            name,
            dir_fd=parent.descriptor,
            follow_symlinks=False,
        )
        created_stat = named
        descriptor = os.open(
            name,
            _directory_open_flags(),
            dir_fd=parent.descriptor,
        )
        receipt = DirectoryReceipt.from_fd(descriptor, uid, gid)
        if not receipt.matches_stat(named):
            raise _storage_error("export_storage_unsafe")
        parent.verify_identity()
        os.fsync(parent.descriptor)
        directory = ExportDirectory(
            descriptor,
            receipt,
            parent=parent,
            name=name,
        )
        directory.verify_identity()
        descriptor = None
        return directory
    except FileExistsError:
        raise
    except BaseException as error:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except BaseException:
                pass
        if created and created_stat is not None:
            _remove_created_directory(parent, name, created_stat)
        if isinstance(error, ExportStorageError):
            raise
        if isinstance(error, OSError) and error.errno == errno.ENOSPC:
            raise _storage_error("insufficient_space") from None
        if not isinstance(error, Exception):
            raise
        raise _storage_error("export_storage_unsafe") from None


def _regular_create_flags():
    required = (getattr(os, "O_NOFOLLOW", None),)
    if any(type(flag) is not int for flag in required):
        raise _storage_error("export_storage_unsafe")
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    return flags


def _unlink_same_inode(directory, name, expected):
    try:
        current = os.stat(
            name,
            dir_fd=directory.descriptor,
            follow_symlinks=False,
        )
        if _identity(current) == _identity(expected):
            os.unlink(name, dir_fd=directory.descriptor)
            try:
                os.fsync(directory.descriptor)
            except BaseException:
                pass
    except BaseException:
        pass


def create_private_file_fs(directory, name):
    _require_leaf_name(name)
    if not isinstance(directory, ExportDirectory):
        raise TypeError("directory must be an ExportDirectory")
    directory.verify_identity()
    descriptor = None
    created_stat = None
    try:
        descriptor = os.open(
            name,
            _regular_create_flags(),
            0o600,
            dir_fd=directory.descriptor,
        )
        created_stat = os.fstat(descriptor)
        os.fchmod(descriptor, 0o600)
        receipt = OpenFileReceipt.from_fd(
            descriptor,
            directory.uid,
            directory.gid,
        )
        named = os.stat(
            name,
            dir_fd=directory.descriptor,
            follow_symlinks=False,
        )
        if not receipt.matches_stat(named):
            raise _storage_error("export_storage_unsafe")
        directory.verify_identity()
        os.fsync(directory.descriptor)
        return descriptor, receipt
    except FileExistsError:
        raise
    except BaseException as error:
        if created_stat is not None:
            _unlink_same_inode(directory, name, created_stat)
        if descriptor is not None:
            try:
                os.close(descriptor)
            except BaseException:
                pass
        if isinstance(error, ExportStorageError):
            raise
        if not isinstance(error, Exception):
            raise
        if isinstance(error, OSError) and error.errno == errno.ENOSPC:
            raise _storage_error("insufficient_space") from None
        raise _storage_error("export_storage_unsafe") from None


@dataclass(frozen=True)
class SpaceBudget(object):
    distinct_original_bytes: int
    archive_pin_bytes: int
    metadata_overhead: int
    margin_bytes: int
    required_bytes: int

    @classmethod
    def for_export(
        cls,
        distinct_original_bytes,
        archive_pin_bytes,
        metadata_overhead,
    ):
        values = (
            distinct_original_bytes,
            archive_pin_bytes,
            metadata_overhead,
        )
        if any(type(value) is not int or value < 0 for value in values):
            raise ValueError("space_budget_requires_nonnegative_integers")
        margin_bytes = max(
            settings.PINRY_EXPORT_SPACE_MIN_MARGIN_BYTES,
            (distinct_original_bytes + 9) // 10,
        )
        required_bytes = (
            distinct_original_bytes
            + archive_pin_bytes
            + metadata_overhead
            + margin_bytes
        )
        return cls(
            distinct_original_bytes,
            archive_pin_bytes,
            metadata_overhead,
            margin_bytes,
            required_bytes,
        )


def observe_available_space(root_fd):
    try:
        filesystem = os.fstatvfs(root_fd)
    except OSError as error:
        raise _storage_error("export_storage_unsafe") from error
    return filesystem.f_bavail * filesystem.f_frsize


def verify_space(root_fd, required_bytes):
    if type(required_bytes) is not int or required_bytes < 0:
        raise ValueError("required_bytes_must_be_nonnegative")
    if observe_available_space(root_fd) < required_bytes:
        raise _storage_error("insufficient_space")


class _LinuxMetadataAdapter(object):
    @staticmethod
    def normalize(descriptor):
        listxattr = getattr(os, "listxattr", None)
        removexattr = getattr(os, "removexattr", None)
        if listxattr is None or removexattr is None:
            raise _storage_error("export_storage_unsafe")
        try:
            names = list(listxattr(descriptor))
            for name in names:
                removexattr(descriptor, name)
            if list(listxattr(descriptor)):
                raise _storage_error("export_storage_unsafe")
        except ExportStorageError:
            raise
        except (OSError, TypeError, ValueError):
            raise _storage_error("export_storage_unsafe") from None


class _DarwinMetadataAdapter(object):
    _ACL_TYPE_EXTENDED = 0x00000100
    _ACL_FIRST_ENTRY = 0

    @classmethod
    def _libc(cls):
        return ctypes.CDLL(None, use_errno=True)

    @classmethod
    def _xattr_names(cls, libc, descriptor):
        flistxattr = getattr(libc, "flistxattr", None)
        if flistxattr is None:
            raise _storage_error("export_storage_unsafe")
        flistxattr.argtypes = (
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_int,
        )
        flistxattr.restype = ctypes.c_ssize_t
        size = flistxattr(descriptor, None, 0, 0)
        if size < 0:
            raise _storage_error("export_storage_unsafe")
        if size == 0:
            return []
        buffer = ctypes.create_string_buffer(size)
        result = flistxattr(
            descriptor,
            ctypes.cast(buffer, ctypes.c_void_p),
            size,
            0,
        )
        if result < 0 or result > size:
            raise _storage_error("export_storage_unsafe")
        return [
            name for name in buffer.raw[:result].split(b"\x00") if name
        ]

    @classmethod
    def _normalize_xattrs(cls, libc, descriptor):
        fremovexattr = getattr(libc, "fremovexattr", None)
        if fremovexattr is None:
            raise _storage_error("export_storage_unsafe")
        fremovexattr.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
        )
        fremovexattr.restype = ctypes.c_int
        for name in cls._xattr_names(libc, descriptor):
            if fremovexattr(descriptor, name, 0) != 0:
                raise _storage_error("export_storage_unsafe")
        if cls._xattr_names(libc, descriptor):
            raise _storage_error("export_storage_unsafe")

    @classmethod
    def _acl_has_entry(cls, libc, descriptor):
        acl_get_fd_np = getattr(libc, "acl_get_fd_np", None)
        acl_get_entry = getattr(libc, "acl_get_entry", None)
        acl_free = getattr(libc, "acl_free", None)
        if None in (acl_get_fd_np, acl_get_entry, acl_free):
            raise _storage_error("export_storage_unsafe")
        acl_get_fd_np.argtypes = (ctypes.c_int, ctypes.c_uint)
        acl_get_fd_np.restype = ctypes.c_void_p
        acl_get_entry.argtypes = (
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_void_p),
        )
        acl_get_entry.restype = ctypes.c_int
        acl_free.argtypes = (ctypes.c_void_p,)
        acl_free.restype = ctypes.c_int
        ctypes.set_errno(0)
        acl = acl_get_fd_np(descriptor, cls._ACL_TYPE_EXTENDED)
        if not acl:
            if ctypes.get_errno() == errno.ENOENT:
                return False
            raise _storage_error("export_storage_unsafe")
        try:
            entry = ctypes.c_void_p()
            result = acl_get_entry(
                acl,
                cls._ACL_FIRST_ENTRY,
                ctypes.byref(entry),
            )
            if result != 0:
                raise _storage_error("export_storage_unsafe")
            return True
        finally:
            if acl_free(acl) != 0:
                raise _storage_error("export_storage_unsafe")

    @classmethod
    def _normalize_acl(cls, libc, descriptor):
        if not cls._acl_has_entry(libc, descriptor):
            return
        acl_delete_fd_np = getattr(libc, "acl_delete_fd_np", None)
        if acl_delete_fd_np is None:
            raise _storage_error("export_storage_unsafe")
        acl_delete_fd_np.argtypes = (ctypes.c_int, ctypes.c_uint)
        acl_delete_fd_np.restype = ctypes.c_int
        if acl_delete_fd_np(descriptor, cls._ACL_TYPE_EXTENDED) != 0:
            raise _storage_error("export_storage_unsafe")
        if cls._acl_has_entry(libc, descriptor):
            raise _storage_error("export_storage_unsafe")

    @classmethod
    def normalize(cls, descriptor):
        try:
            libc = cls._libc()
            cls._normalize_acl(libc, descriptor)
            cls._normalize_xattrs(libc, descriptor)
        except ExportStorageError:
            raise
        except (OSError, TypeError, ValueError, AttributeError):
            raise _storage_error("export_storage_unsafe") from None


def _normalize_metadata(descriptor):
    if sys.platform.startswith("linux"):
        _LinuxMetadataAdapter.normalize(descriptor)
        return
    if sys.platform == "darwin":
        _DarwinMetadataAdapter.normalize(descriptor)
        return
    raise _storage_error("export_storage_unsafe")


def _copy_and_hash(
    source_fd,
    destination_fd,
    size,
    heartbeat,
    stop_requested,
):
    digest = hashlib.sha256()
    offset = 0
    try:
        os.ftruncate(destination_fd, 0)
        while offset < size:
            if stop_requested():
                raise _storage_error("snapshot_failed")
            chunk = os.pread(
                source_fd,
                min(_COPY_CHUNK_SIZE, size - offset),
                offset,
            )
            if not chunk:
                raise _storage_error("source_changed")
            digest.update(chunk)
            written = 0
            while written < len(chunk):
                count = os.pwrite(
                    destination_fd,
                    chunk[written:],
                    offset + written,
                )
                if count <= 0:
                    raise _storage_error("snapshot_failed")
                written += count
            offset += len(chunk)
            heartbeat()
        os.ftruncate(destination_fd, size)
    except ExportStorageError:
        raise
    except OSError as error:
        if error.errno == errno.ENOSPC:
            raise _storage_error("insufficient_space") from None
        raise _storage_error("snapshot_failed") from None
    return "copy", digest.hexdigest()


def _destination_open_identity(source_receipt, destination_fd):
    try:
        destination = os.fstat(destination_fd)
    except OSError as error:
        raise _storage_error("export_storage_unsafe") from error
    if (
        not stat.S_ISREG(destination.st_mode)
        or destination.st_nlink != 1
        or _mode(destination) != 0o600
        or _identity(destination) == (source_receipt.dev, source_receipt.ino)
    ):
        raise _storage_error("export_storage_unsafe")
    return (
        destination.st_dev,
        destination.st_ino,
        destination.st_uid,
        destination.st_gid,
        _mode(destination),
        destination.st_nlink,
    )


def _verify_destination(source_receipt, destination_fd, open_identity):
    try:
        destination = os.fstat(destination_fd)
    except OSError as error:
        raise _storage_error("snapshot_failed") from error
    if (
        not stat.S_ISREG(destination.st_mode)
        or (
            destination.st_dev,
            destination.st_ino,
            destination.st_uid,
            destination.st_gid,
            _mode(destination),
            destination.st_nlink,
        ) != open_identity
        or _identity(destination) == (source_receipt.dev, source_receipt.ino)
        or destination.st_size != source_receipt.size
    ):
        raise _storage_error("export_storage_unsafe")


def clone_or_copy(
    source_fd,
    destination_fd,
    source_receipt,
    heartbeat,
    stop_requested,
    before_copy,
):
    if not isinstance(source_receipt, SourceReceipt):
        raise TypeError("source_receipt must be a SourceReceipt")
    source_receipt.verify_identity(source_fd)
    destination_identity = _destination_open_identity(
        source_receipt,
        destination_fd,
    )
    try:
        if sys.platform.startswith("linux"):
            try:
                fcntl.ioctl(destination_fd, _FICLONE, source_fd)
                method, digest = "reflink", None
            except OSError as error:
                if error.errno == errno.ENOSPC:
                    raise _storage_error("insufficient_space") from None
                if error.errno not in _REFLINK_UNSUPPORTED:
                    raise _storage_error("snapshot_failed") from None
                before_copy(source_receipt.size)
                method, digest = _copy_and_hash(
                    source_fd,
                    destination_fd,
                    source_receipt.size,
                    heartbeat,
                    stop_requested,
                )
        else:
            before_copy(source_receipt.size)
            method, digest = _copy_and_hash(
                source_fd,
                destination_fd,
                source_receipt.size,
                heartbeat,
                stop_requested,
            )
        source_receipt.verify_identity(source_fd)
        _verify_destination(
            source_receipt,
            destination_fd,
            destination_identity,
        )
        _normalize_metadata(destination_fd)
        try:
            os.fsync(destination_fd)
        except OSError as error:
            if error.errno == errno.ENOSPC:
                raise _storage_error("insufficient_space") from None
            raise _storage_error("snapshot_failed") from None
        source_receipt.verify_identity(source_fd)
        _verify_destination(
            source_receipt,
            destination_fd,
            destination_identity,
        )
        return method, digest
    except ExportStorageError:
        raise
    except OSError as error:
        if error.errno == errno.ENOSPC:
            raise _storage_error("insufficient_space") from None
        raise _storage_error("snapshot_failed") from None


def rename_noreplace(
    source_directory,
    source_name,
    destination_directory,
    destination_name,
):
    _require_leaf_name(source_name)
    _require_leaf_name(destination_name)
    source_directory.verify_identity()
    destination_directory.verify_identity()
    source_adapter = None
    destination_adapter = None
    try:
        source_adapter = MediaDirectory([
            os.dup(source_directory.descriptor)
        ])
        destination_adapter = MediaDirectory([
            os.dup(destination_directory.descriptor)
        ])
        rename_media_noreplace(
            source_adapter,
            source_name,
            destination_adapter,
            destination_name,
        )
        for descriptor in dict.fromkeys((
            source_directory.descriptor,
            destination_directory.descriptor,
        )):
            os.fsync(descriptor)
        source_directory.verify_identity()
        destination_directory.verify_identity()
    except FileExistsError:
        raise
    except OSError as error:
        if error.errno == errno.ENOSPC:
            raise _storage_error("insufficient_space") from None
        raise _storage_error("export_storage_unsafe") from None
    except MediaPathError:
        raise _storage_error("export_storage_unsafe") from None
    finally:
        if source_adapter is not None:
            source_adapter.close()
        if destination_adapter is not None:
            destination_adapter.close()


def _open_named_regular(directory, name):
    flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    return os.open(name, flags, dir_fd=directory.descriptor)


def remove_if_receipt_matches(directory, name, receipt):
    _require_leaf_name(name)
    if not isinstance(receipt, (OpenFileReceipt, ClosedFileReceipt)):
        raise TypeError("receipt must be a file receipt")
    directory.verify_identity()
    descriptor = None
    try:
        named = os.stat(
            name,
            dir_fd=directory.descriptor,
            follow_symlinks=False,
        )
        if not receipt.matches_stat(named):
            raise _storage_error("export_storage_unsafe")
        descriptor = _open_named_regular(directory, name)
        receipt.verify_identity(descriptor)
        named_again = os.stat(
            name,
            dir_fd=directory.descriptor,
            follow_symlinks=False,
        )
        if not receipt.matches_stat(named_again):
            raise _storage_error("export_storage_unsafe")
        os.unlink(name, dir_fd=directory.descriptor)
        os.fsync(directory.descriptor)
        return True
    except FileNotFoundError:
        return False
    except ExportStorageError:
        raise
    except OSError:
        raise _storage_error("export_storage_unsafe") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


class ExportWorkerLock(object):
    filename = ".worker.lock"

    def __init__(self, descriptor):
        self.descriptor = descriptor
        self._closed = False

    @classmethod
    def acquire(cls, export_root, uid, gid):  # noqa: C901
        if fcntl is None:
            raise _storage_error("export_storage_unsafe")
        export_root.verify_identity()
        flags = os.O_RDWR | os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        descriptor = None
        created = False
        held = False
        try:
            try:
                descriptor = os.open(
                    cls.filename,
                    flags | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=export_root.descriptor,
                )
                created = True
                os.fchmod(descriptor, 0o600)
            except FileExistsError:
                descriptor = os.open(
                    cls.filename,
                    flags,
                    dir_fd=export_root.descriptor,
                )
            receipt = OpenFileReceipt.from_fd(descriptor, uid, gid)
            named = os.stat(
                cls.filename,
                dir_fd=export_root.descriptor,
                follow_symlinks=False,
            )
            if not receipt.matches_stat(named):
                raise _storage_error("export_storage_unsafe")
            if created:
                os.fsync(export_root.descriptor)
            export_root.verify_identity()
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
            except OSError as error:
                if error.errno in (errno.EACCES, errno.EAGAIN):
                    raise _storage_error("export_worker_unavailable") from None
                raise _storage_error("export_storage_unsafe") from None
            named = os.stat(
                cls.filename,
                dir_fd=export_root.descriptor,
                follow_symlinks=False,
            )
            receipt.verify_identity(descriptor)
            if not receipt.matches_stat(named):
                raise _storage_error("export_storage_unsafe")
            export_root.verify_identity()
            lock = cls(descriptor)
            descriptor = None
            return lock
        except BaseException as error:
            if descriptor is not None:
                if held:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                    except BaseException:
                        pass
                try:
                    os.close(descriptor)
                except BaseException:
                    pass
            if isinstance(error, (ExportStorageError, FileExistsError)):
                raise
            if not isinstance(error, Exception):
                raise
            raise _storage_error("export_storage_unsafe") from None

    def close(self):
        if self._closed:
            return
        self._closed = True
        descriptor = self.descriptor
        self.descriptor = None
        first_error = None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except BaseException as error:
            first_error = error
        try:
            os.close(descriptor)
        except BaseException as error:
            if first_error is None:
                first_error = error
        if first_error is not None:
            raise first_error

    def __enter__(self):
        return self

    def __exit__(self, error_type, error, traceback):
        del error_type, error, traceback
        self.close()
        return False


__all__ = (
    "ClosedFileReceipt",
    "DirectoryReceipt",
    "ExportStorageError",
    "ExportWorkerLock",
    "OpenFileReceipt",
    "SourceReceipt",
    "SpaceBudget",
    "clone_or_copy",
    "create_private_directory",
    "create_private_file_fs",
    "media_global_writer_gate",
    "observe_available_space",
    "open_export_root",
    "open_receipted_directory",
    "open_verified_media_file",
    "open_verified_media_root",
    "remove_if_receipt_matches",
    "rename_media_noreplace",
    "rename_noreplace",
    "verify_space",
)
