import ctypes
import errno
import hashlib
import os
from pathlib import PurePosixPath
import stat
import sys
import uuid


class MediaPathError(Exception):
    pass


class PublishResult(object):
    def __init__(self, created=False, reused=False):
        self.created = created
        self.reused = reused


class MediaDirectory(object):
    def __init__(self, descriptors):
        self.descriptors = descriptors

    @property
    def descriptor(self):
        return self.descriptors[-1]

    def fsync_publish(self):
        for descriptor in reversed(self.descriptors):
            os.fsync(descriptor)

    def close(self):
        while self.descriptors:
            os.close(self.descriptors.pop())


class OwnedStagingFile(object):
    def __init__(self, directory, name, descriptor, file_stat, path):
        self.directory = directory
        self.name = name
        self.descriptor = descriptor
        self.file_stat = file_stat
        self.path = path

    def cleanup(self):
        _unlink_owned_name(
            self.directory.descriptor,
            self.name,
            self.file_stat,
        )
        os.fsync(self.directory.descriptor)

    def close(self):
        os.close(self.descriptor)
        self.directory.close()


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


def open_or_create_media_directory(media_root, relative_directory):
    components = _relative_components(relative_directory)
    descriptors = [_open_directory(os.path.realpath(media_root))]
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
            child_descriptor = _open_child_directory_nofollow(
                parent_descriptor,
                name,
                named_stat,
            )
            descriptors.append(child_descriptor)
            if created:
                os.fsync(child_descriptor)
                os.fsync(parent_descriptor)
        return MediaDirectory(descriptors)
    except BaseException:
        while descriptors:
            os.close(descriptors.pop())
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
        finally:
            if descriptor is not None:
                os.close(descriptor)
            directory.close()
        raise
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        directory.close()
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
    part_path,
    destination,
    expected_sha256,
    part_descriptor=None,
    part_directory_descriptor=None,
    destination_directory=None,
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
        try:
            _publish_descriptor(
                part_descriptor,
                destination_directory_descriptor,
                destination_name,
            )
        except FileExistsError:
            _verify_existing_destination(
                destination_directory_descriptor,
                destination_name,
                expected_sha256,
            )
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
            return PublishResult(reused=True)
        destination_descriptor = _open_regular_nofollow(
            destination_directory_descriptor,
            destination_name,
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
        return PublishResult(created=True)
    finally:
        if opened_part:
            os.close(part_descriptor)
        if opened_destination_directory:
            os.close(destination_directory_descriptor)
        if opened_part_directory:
            os.close(part_directory_descriptor)


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
        if sha256_file_descriptor(descriptor) != expected_sha256:
            raise MediaPathError("media_path_conflict")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _unlink_owned_name(directory_descriptor, name, expected_stat):
    _require_owned_name(directory_descriptor, name, expected_stat)
    os.unlink(name, dir_fd=directory_descriptor)
