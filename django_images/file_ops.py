import ctypes
import errno
import hashlib
import os
from pathlib import PurePosixPath
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


def _fsync_directory(path):
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rename_noreplace(part_path, destination):
    if not sys.platform.startswith("linux"):
        return False
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        return False
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        -100,
        os.fsencode(part_path),
        -100,
        os.fsencode(destination),
        1,
    )
    if result == 0:
        return True
    error_number = ctypes.get_errno()
    if error_number in (errno.ENOSYS, errno.EINVAL, errno.ENOTSUP):
        return False
    if error_number == errno.EEXIST:
        raise FileExistsError(error_number, os.strerror(error_number))
    raise OSError(error_number, os.strerror(error_number))


def publish_noreplace(part_path, destination, expected_sha256):
    if sha256_path(part_path) != expected_sha256:
        raise MediaPathError("part_hash_mismatch")
    try:
        if not _rename_noreplace(part_path, destination):
            os.link(part_path, destination)
            os.unlink(part_path)
        _fsync_directory(os.path.dirname(destination))
        return PublishResult(created=True)
    except FileExistsError:
        if sha256_path(destination) != expected_sha256:
            raise MediaPathError("media_path_conflict")
        os.unlink(part_path)
        _fsync_directory(os.path.dirname(destination))
        return PublishResult(reused=True)
