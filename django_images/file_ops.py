import ctypes
import errno
import hashlib
import os
from pathlib import PurePosixPath
import stat
import sys


class MediaPathError(Exception):
    pass


class PublishResult(object):
    def __init__(self, created=False, reused=False):
        self.created = created
        self.reused = reused


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
        if _link_descriptor_empty_path(
            file_descriptor, directory_descriptor, destination_name
        ):
            return
        if _link_descriptor_proc(
            file_descriptor, directory_descriptor, destination_name
        ):
            return
    elif sys.platform == "darwin":
        if _clone_descriptor_noreplace(
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
    part_path, destination, expected_sha256, part_descriptor=None
):
    part_parent = os.path.realpath(os.path.dirname(part_path))
    destination_parent = os.path.realpath(os.path.dirname(destination))
    if part_parent != destination_parent:
        raise MediaPathError("unsafe_staging_file")
    part_name = os.path.basename(part_path)
    destination_name = os.path.basename(destination)
    parent_descriptor = _open_directory(destination_parent)
    opened_part = False
    try:
        if part_descriptor is None:
            part_descriptor = _open_regular_nofollow(
                parent_descriptor, part_name
            )
            opened_part = True
        part_stat = os.fstat(part_descriptor)
        _require_owned_name(parent_descriptor, part_name, part_stat)
        if sha256_file_descriptor(part_descriptor) != expected_sha256:
            raise MediaPathError("part_hash_mismatch")
        try:
            _publish_descriptor(
                part_descriptor, parent_descriptor, destination_name
            )
        except FileExistsError:
            if _sha256_name(parent_descriptor, destination_name) != expected_sha256:
                raise MediaPathError("media_path_conflict")
            _unlink_owned_name(parent_descriptor, part_name, part_stat)
            os.fsync(parent_descriptor)
            return PublishResult(reused=True)
        destination_descriptor = _open_regular_nofollow(
            parent_descriptor, destination_name
        )
        try:
            if (
                sha256_file_descriptor(destination_descriptor)
                != expected_sha256
            ):
                raise MediaPathError("published_hash_mismatch")
            os.fsync(destination_descriptor)
        finally:
            os.close(destination_descriptor)
        _unlink_owned_name(parent_descriptor, part_name, part_stat)
        os.fsync(parent_descriptor)
        return PublishResult(created=True)
    finally:
        if opened_part:
            os.close(part_descriptor)
        os.close(parent_descriptor)


def _open_directory(path):
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return os.open(path, flags)


def _open_regular_nofollow(directory_descriptor, name):
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
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


def _sha256_name(directory_descriptor, name):
    descriptor = _open_regular_nofollow(directory_descriptor, name)
    try:
        return sha256_file_descriptor(descriptor)
    finally:
        os.close(descriptor)


def _unlink_owned_name(directory_descriptor, name, expected_stat):
    _require_owned_name(directory_descriptor, name, expected_stat)
    os.unlink(name, dir_fd=directory_descriptor)
