import ctypes
from dataclasses import dataclass, fields
import errno
import os
import re
import stat
import sys
import uuid

from django.core.management import CommandError
from django.db.models import Q

from django_images.file_ops import (
    MediaDirectory,
    MediaPathError,
    open_verified_media_root,
)
from django_images.models import Image, Thumbnail
from django_images.paths import FORMAT_EXTENSIONS
from django_images.services.media_migration_v2 import (
    load_auto_v2_archive_sources,
)


__all__ = (
    "ArchiveIntent",
    "ArchiveSession",
    "LegacyMediaArchive",
    "LinuxRenameNoReplaceAdapter",
    "MediaArchiveError",
    "open_archive_session",
    "validate_no_legacy_media_references",
)


RENAME_NOREPLACE = 1
_ROOT_PARENT_SENTINEL = "."
_UNSUPPORTED_ATOMIC_ERRNOS = frozenset((
    errno.ENOSYS,
    errno.EINVAL,
    errno.EOPNOTSUPP,
    errno.ENOTSUP,
))
_CONFLICT_ERRNOS = frozenset((
    errno.EEXIST,
    errno.EXDEV,
    errno.ENOENT,
    errno.ENOTDIR,
    errno.ELOOP,
))
_FIXED_SLOT_PATTERN = re.compile(
    r"^originals/"
    r"(?P<asset_uuid>"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12})/"
    r"original(?P<extension>\.[a-z0-9]+)\Z"
)


class MediaArchiveError(Exception):
    def __init__(self, code):
        super(MediaArchiveError, self).__init__(code)
        self.code = code


def _archive_error(code, cause=None):
    error = MediaArchiveError(code)
    if cause is not None:
        error.__cause__ = cause
    return error


def _archive_io_error(error):
    code = (
        "archive_state_conflict"
        if error.errno in _CONFLICT_ERRNOS
        else "archive_failed"
    )
    return _archive_error(code, error)


def _archive_path_error(error):
    cause = error.__cause__
    if isinstance(cause, OSError):
        return _archive_io_error(cause)
    return _archive_error("archive_state_conflict", error)


def _identity(file_stat):
    return file_stat.st_dev, file_stat.st_ino


def _valid_identity_number(value, inode=False):
    return (
        type(value) is int
        and value >= (1 if inode else 0)
    )


def _relative_components(relative_path, allow_root=False):
    if allow_root and relative_path == _ROOT_PARENT_SENTINEL:
        return ()
    if (
        not isinstance(relative_path, str)
        or not relative_path
        or relative_path.startswith("/")
        or "\\" in relative_path
        or "\x00" in relative_path
    ):
        raise _archive_error("archive_state_conflict")
    components = tuple(relative_path.split("/"))
    if any(component in ("", ".", "..") for component in components):
        raise _archive_error("archive_state_conflict")
    return components


def _split_relative_path(relative_path):
    components = _relative_components(relative_path)
    if len(components) == 1:
        return _ROOT_PARENT_SENTINEL, components[0]
    return "/".join(components[:-1]), components[-1]


def _validate_name(name):
    components = _relative_components(name)
    if len(components) != 1:
        raise _archive_error("archive_state_conflict")


@dataclass(frozen=True)
class ArchiveIntent(object):
    source_root_device: int
    source_root_inode: int
    source_parent_relative: str
    source_parent_device: int
    source_parent_inode: int
    source_name: str
    source_device: int
    source_inode: int
    destination_root_device: int
    destination_root_inode: int
    destination_parent_relative: str
    destination_parent_device: int
    destination_parent_inode: int
    destination_name: str

    def __post_init__(self):
        device_fields = (
            "source_root_device",
            "source_parent_device",
            "source_device",
            "destination_root_device",
            "destination_parent_device",
        )
        inode_fields = (
            "source_root_inode",
            "source_parent_inode",
            "source_inode",
            "destination_root_inode",
            "destination_parent_inode",
        )
        if any(
            not _valid_identity_number(getattr(self, name))
            for name in device_fields
        ) or any(
            not _valid_identity_number(getattr(self, name), inode=True)
            for name in inode_fields
        ):
            raise _archive_error("archive_state_conflict")
        _relative_components(self.source_parent_relative, allow_root=True)
        _relative_components(
            self.destination_parent_relative,
            allow_root=True,
        )
        _validate_name(self.source_name)
        _validate_name(self.destination_name)

    def as_dict(self):
        return {
            field.name: getattr(self, field.name)
            for field in fields(self)
        }

    @classmethod
    def from_dict(cls, value):
        names = tuple(field.name for field in fields(cls))
        if not isinstance(value, dict) or set(value) != set(names):
            raise _archive_error("archive_state_conflict")
        try:
            return cls(**{name: value[name] for name in names})
        except (KeyError, TypeError, ValueError) as error:
            raise _archive_error("archive_state_conflict", error)


@dataclass(frozen=True)
class ArchiveResult(object):
    status: str
    intent: ArchiveIntent


@dataclass(frozen=True)
class ArchiveConvergence(object):
    results: tuple
    progress: object


@dataclass(frozen=True)
class ArchivePlan(object):
    intents: tuple
    progress: object
    schema_only: bool = False


class LinuxRenameNoReplaceAdapter(object):
    def __init__(self, platform=None, libc_factory=None):
        self.platform = sys.platform if platform is None else platform
        self.libc_factory = (
            ctypes.CDLL if libc_factory is None else libc_factory
        )

    def rename_noreplace(
        self,
        source_parent_descriptor,
        source_name,
        destination_parent_descriptor,
        destination_name,
    ):
        if not self.platform.startswith("linux"):
            raise _archive_error("atomic_archive_unsupported")
        try:
            libc = self.libc_factory(None, use_errno=True)
        except (AttributeError, OSError, TypeError) as error:
            raise _archive_error("atomic_archive_unsupported", error)
        source_bytes = os.fsencode(source_name)
        destination_bytes = os.fsencode(destination_name)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is not None:
            renameat2.argtypes = (
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            )
            renameat2.restype = ctypes.c_int
            result = renameat2(
                source_parent_descriptor,
                source_bytes,
                destination_parent_descriptor,
                destination_bytes,
                RENAME_NOREPLACE,
            )
        else:
            result = self._syscall_rename_noreplace(
                libc,
                source_parent_descriptor,
                source_bytes,
                destination_parent_descriptor,
                destination_bytes,
            )
        if result != 0:
            error_number = ctypes.get_errno()
            if error_number in _UNSUPPORTED_ATOMIC_ERRNOS:
                raise _archive_error("atomic_archive_unsupported")
            raise OSError(error_number, "renameat2_failed")

    def _syscall_rename_noreplace(
        self,
        libc,
        source_parent_descriptor,
        source_bytes,
        destination_parent_descriptor,
        destination_bytes,
    ):
        try:
            machine = os.uname().machine.lower()
        except (AttributeError, OSError):
            raise _archive_error("atomic_archive_unsupported")
        syscall_number = {
            "aarch64": 276,
            "arm64": 276,
            "armv7l": 382,
            "i386": 353,
            "i686": 353,
            "ppc64": 357,
            "ppc64le": 357,
            "riscv64": 276,
            "s390x": 347,
            "x86_64": 316,
        }.get(machine)
        syscall = getattr(libc, "syscall", None)
        if syscall_number is None or syscall is None:
            raise _archive_error("atomic_archive_unsupported")
        syscall.restype = ctypes.c_long
        return syscall(
            ctypes.c_long(syscall_number),
            ctypes.c_int(source_parent_descriptor),
            ctypes.c_char_p(source_bytes),
            ctypes.c_int(destination_parent_descriptor),
            ctypes.c_char_p(destination_bytes),
            ctypes.c_uint(RENAME_NOREPLACE),
        )


class _ArchiveLeaf(object):
    def __init__(self, parent, name, descriptor, file_stat, kind):
        self.parent = parent
        self.name = name
        self.descriptor = descriptor
        self.file_stat = file_stat
        self.kind = kind

    def verify_descriptor(self):
        try:
            opened_stat = os.fstat(self.descriptor)
        except OSError as error:
            raise _archive_io_error(error)
        _validate_leaf_stat(opened_stat, self.kind)
        if _identity(opened_stat) != _identity(self.file_stat):
            raise _archive_error("archive_state_conflict")
        return True

    def verify_current(self):
        try:
            self.parent.verify_current()
            self.verify_descriptor()
            named_stat = os.stat(
                self.name,
                dir_fd=self.parent.descriptor,
                follow_symlinks=False,
            )
        except MediaArchiveError:
            raise
        except MediaPathError as error:
            raise _archive_path_error(error)
        except OSError as error:
            raise _archive_io_error(error)
        _validate_leaf_stat(named_stat, self.kind)
        if _identity(named_stat) != _identity(self.file_stat):
            raise _archive_error("archive_state_conflict")
        return True

    def close(self):
        descriptor = self.descriptor
        self.descriptor = None
        if descriptor is not None:
            os.close(descriptor)


def _validate_leaf_stat(file_stat, expected_kind=None):
    if stat.S_ISREG(file_stat.st_mode):
        kind = "file"
        if file_stat.st_nlink != 1:
            raise _archive_error("archive_state_conflict")
    elif stat.S_ISDIR(file_stat.st_mode):
        kind = "directory"
    else:
        raise _archive_error("archive_state_conflict")
    if expected_kind is not None and kind != expected_kind:
        raise _archive_error("archive_state_conflict")
    return kind


def _open_archive_directory(
    root_directory, relative_path, create=False
):
    components = _relative_components(relative_path, allow_root=True)
    descriptors = []
    names = []
    directory_stats = []
    try:
        root_directory.verify_current()
        descriptors.append(os.dup(root_directory.descriptor))
        flags = os.O_RDONLY
        required_flags = (
            getattr(os, "O_DIRECTORY", None),
            getattr(os, "O_NOFOLLOW", None),
        )
        if any(type(flag) is not int for flag in required_flags):
            raise _archive_error("archive_state_conflict")
        flags |= os.O_DIRECTORY | os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        for name in components:
            parent_descriptor = descriptors[-1]
            try:
                named_stat = os.stat(
                    name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(name, 0o700, dir_fd=parent_descriptor)
                except FileExistsError:
                    pass
                named_stat = os.stat(
                    name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            if not stat.S_ISDIR(named_stat.st_mode):
                raise _archive_error("archive_state_conflict")
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
                os.close(descriptor)
                raise _archive_error("archive_state_conflict")
            names.append(name)
            directory_stats.append(opened_stat)
            descriptors.append(descriptor)
            if create:
                try:
                    os.fsync(descriptor)
                    os.fsync(parent_descriptor)
                except OSError as error:
                    raise _archive_error("archive_failed", error)
        directory = MediaDirectory(
            descriptors,
            names,
            directory_stats,
            created=[False for _name in names],
            anchor_directory=root_directory,
        )
        directory.verify_current()
        return directory
    except BaseException as error:
        while descriptors:
            try:
                os.close(descriptors.pop())
            except BaseException:
                pass
        if isinstance(error, MediaArchiveError):
            raise
        if isinstance(error, MediaPathError):
            raise _archive_path_error(error)
        if not isinstance(error, Exception):
            raise
        if isinstance(error, OSError):
            raise _archive_io_error(error)
        raise _archive_error("archive_state_conflict", error)


def _open_archive_leaf(parent, name, missing_ok=False):
    descriptor = None
    try:
        try:
            named_stat = os.stat(
                name,
                dir_fd=parent.descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            if missing_ok:
                return None
            raise _archive_error("archive_state_conflict")
        kind = _validate_leaf_stat(named_stat)
        flags = os.O_RDONLY | os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if kind == "directory":
            flags |= os.O_DIRECTORY
        elif hasattr(os, "O_NONBLOCK"):
            flags |= os.O_NONBLOCK
        descriptor = os.open(name, flags, dir_fd=parent.descriptor)
        opened_stat = os.fstat(descriptor)
        _validate_leaf_stat(opened_stat, kind)
        if _identity(opened_stat) != _identity(named_stat):
            raise _archive_error("archive_state_conflict")
        leaf = _ArchiveLeaf(parent, name, descriptor, opened_stat, kind)
        leaf.verify_current()
        descriptor = None
        return leaf
    except BaseException as error:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except BaseException:
                pass
        if isinstance(error, MediaArchiveError):
            raise
        if isinstance(error, MediaPathError):
            raise _archive_path_error(error)
        if not isinstance(error, Exception):
            raise
        if isinstance(error, OSError):
            raise _archive_io_error(error)
        raise _archive_error("archive_state_conflict", error)


def _require_absent(parent, name):
    try:
        parent.verify_current()
        os.stat(name, dir_fd=parent.descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return True
    except MediaPathError as error:
        raise _archive_path_error(error)
    except OSError as error:
        raise _archive_io_error(error)
    raise _archive_error("archive_state_conflict")


def _open_verified_root(path):
    try:
        return open_verified_media_root(path)
    except MediaPathError as error:
        raise _archive_path_error(error)
    except OSError as error:
        raise _archive_io_error(error)
    except (TypeError, ValueError) as error:
        raise _archive_error("archive_state_conflict", error)


def _same_filesystem(*file_stats):
    devices = {file_stat.st_dev for file_stat in file_stats}
    if len(devices) != 1:
        raise _archive_error("archive_state_conflict")


def _open_archive_resources(
    source_root,
    destination_root,
    source_parent_relative,
    source_name,
    destination_parent_relative,
    destination_name,
    source_missing_ok,
    destination_missing_ok,
):
    resources = [None] * 6
    try:
        resources[0] = _open_verified_root(source_root)
        resources[1] = _open_verified_root(destination_root)
        resources[2] = _open_archive_directory(
            resources[0], source_parent_relative
        )
        resources[3] = _open_archive_directory(
            resources[1], destination_parent_relative
        )
        resources[4] = _open_archive_leaf(
            resources[2], source_name, missing_ok=source_missing_ok
        )
        resources[5] = _open_archive_leaf(
            resources[3],
            destination_name,
            missing_ok=destination_missing_ok,
        )
        return tuple(resources)
    except BaseException:
        _close_archive_resources(*reversed(resources))
        raise


def fixed_slot_destination_path(source_relative):
    match = (
        _FIXED_SLOT_PATTERN.match(source_relative)
        if isinstance(source_relative, str)
        else None
    )
    if match is None or match.group("extension") not in frozenset(
        FORMAT_EXTENSIONS.values()
    ):
        raise _archive_error("archive_manifest_mismatch")
    try:
        canonical_uuid = str(uuid.UUID(match.group("asset_uuid")))
    except (AttributeError, TypeError, ValueError) as error:
        raise _archive_error("archive_manifest_mismatch", error)
    if canonical_uuid != match.group("asset_uuid"):
        raise _archive_error("archive_manifest_mismatch")
    return "media/fixed-slot-originals/{}".format(source_relative)


def build_archive_intent(
    source_root,
    source_relative,
    destination_root,
    destination_relative,
):
    source_parent_relative, source_name = _split_relative_path(
        source_relative
    )
    destination_parent_relative, destination_name = _split_relative_path(
        destination_relative
    )
    resources = None
    try:
        resources = _open_archive_resources(
            source_root,
            destination_root,
            source_parent_relative,
            source_name,
            destination_parent_relative,
            destination_name,
            source_missing_ok=False,
            destination_missing_ok=True,
        )
        (
            source_root_directory,
            destination_root_directory,
            source_parent,
            destination_parent,
            source_leaf,
            destination_leaf,
        ) = resources
        if destination_leaf is not None:
            raise _archive_error("archive_state_conflict")
        source_root_stat = os.fstat(source_root_directory.descriptor)
        destination_root_stat = os.fstat(
            destination_root_directory.descriptor
        )
        source_parent_stat = os.fstat(source_parent.descriptor)
        destination_parent_stat = os.fstat(destination_parent.descriptor)
        _same_filesystem(
            source_root_stat,
            destination_root_stat,
            source_parent_stat,
            destination_parent_stat,
            source_leaf.file_stat,
        )
        intent = ArchiveIntent(
            source_root_device=source_root_stat.st_dev,
            source_root_inode=source_root_stat.st_ino,
            source_parent_relative=source_parent_relative,
            source_parent_device=source_parent_stat.st_dev,
            source_parent_inode=source_parent_stat.st_ino,
            source_name=source_name,
            source_device=source_leaf.file_stat.st_dev,
            source_inode=source_leaf.file_stat.st_ino,
            destination_root_device=destination_root_stat.st_dev,
            destination_root_inode=destination_root_stat.st_ino,
            destination_parent_relative=destination_parent_relative,
            destination_parent_device=destination_parent_stat.st_dev,
            destination_parent_inode=destination_parent_stat.st_ino,
            destination_name=destination_name,
        )
        if source_leaf.kind != _intent_leaf_kind(intent):
            raise _archive_error("archive_state_conflict")
        return intent
    except MediaArchiveError:
        raise
    except MediaPathError as error:
        raise _archive_path_error(error)
    except OSError as error:
        raise _archive_io_error(error)
    except (TypeError, ValueError) as error:
        raise _archive_error("archive_state_conflict", error)
    finally:
        if resources is not None:
            _close_archive_resources(*reversed(resources))


def _ensure_archive_parent(root_path, relative_path):
    root_directory = None
    parent_directory = None
    try:
        root_directory = _open_verified_root(root_path)
        parent_directory = _open_archive_directory(
            root_directory, relative_path, create=True
        )
        parent_directory.verify_current()
    except MediaArchiveError:
        raise
    except MediaPathError as error:
        raise _archive_path_error(error)
    except OSError as error:
        raise _archive_io_error(error)
    finally:
        _close_archive_resources(parent_directory, root_directory)


def validate_no_legacy_media_references(using="default"):
    legacy_filter = Q(image="image") | Q(image__startswith="image/")
    try:
        has_references = (
            Image.objects.using(using).filter(legacy_filter).exists()
            or Thumbnail.objects.using(using).filter(legacy_filter).exists()
        )
    except Exception as error:
        raise _archive_error("archive_failed", error)
    if has_references:
        raise _archive_error("legacy_media_still_referenced")
    return True


def _load_fixed_slot_sources(
    run_directory,
    filename,
    run_id,
    service_uid,
    service_gid,
):
    try:
        sources = load_auto_v2_archive_sources(
            run_directory,
            filename,
            run_id,
            service_uid,
            service_gid,
        )
    except CommandError as error:
        code = str(error)
        allowed_codes = frozenset((
            "auto_v2_plan_incomplete",
            "invalid_auto_v2_manifest",
            "invalid_auto_v2_run_id",
            "manifest_plan_mismatch",
            "manifest_run_id_mismatch",
            "media_manifest_torn_tail_requires_execute",
            "unsafe_auto_v2_manifest",
        ))
        if code not in allowed_codes:
            code = "archive_manifest_mismatch"
        raise _archive_error(code, error)
    if (
        not isinstance(sources, tuple)
        or len(sources) != len(set(sources))
    ):
        raise _archive_error("archive_manifest_mismatch")
    for source in sources:
        fixed_slot_destination_path(source)
    return tuple(sorted(sources))


def _relative_leaf_path(parent_relative, name):
    if parent_relative == _ROOT_PARENT_SENTINEL:
        return name
    return "{}/{}".format(parent_relative, name)


def _intent_leaf_kind(intent):
    md5_layout = (
        intent.source_parent_relative == _ROOT_PARENT_SENTINEL
        and intent.source_name == "image"
        and intent.destination_parent_relative == "media"
        and intent.destination_name == "image"
    )
    return "directory" if md5_layout else "file"


def _validate_archive_intent_layout(intents, fixed_slot_sources):
    if not isinstance(intents, tuple):
        raise _archive_error("archive_state_conflict")
    pairs = tuple(
        (
            _relative_leaf_path(
                intent.source_parent_relative, intent.source_name
            ),
            _relative_leaf_path(
                intent.destination_parent_relative,
                intent.destination_name,
            ),
        )
        for intent in intents
        if isinstance(intent, ArchiveIntent)
    )
    fixed_pairs = tuple(
        (source, fixed_slot_destination_path(source))
        for source in sorted(fixed_slot_sources)
    )
    expected_pairs = fixed_pairs
    if pairs[:1] == (("image", "media/image"),):
        expected_pairs = (("image", "media/image"),) + fixed_pairs
    root_pairs = {
        (
            (
                intent.source_root_device,
                intent.source_root_inode,
            ),
            (
                intent.destination_root_device,
                intent.destination_root_inode,
            ),
        )
        for intent in intents
        if isinstance(intent, ArchiveIntent)
    }
    if (
        len(pairs) != len(intents)
        or len(intents) != len(set(intents))
        or pairs != expected_pairs
        or len(root_pairs) > 1
    ):
        raise _archive_error("archive_state_conflict")


def _verify_intent_roots(source_root, destination_root, intents):
    source_directory = None
    destination_directory = None
    try:
        source_directory = _open_verified_root(source_root)
        destination_directory = _open_verified_root(destination_root)
        source_stat = os.fstat(source_directory.descriptor)
        destination_stat = os.fstat(destination_directory.descriptor)
        _same_filesystem(source_stat, destination_stat)
        for intent in intents:
            if (
                _identity(source_stat)
                != (intent.source_root_device, intent.source_root_inode)
                or _identity(destination_stat)
                != (
                    intent.destination_root_device,
                    intent.destination_root_inode,
                )
            ):
                raise _archive_error("archive_state_conflict")
    except MediaArchiveError:
        raise
    except MediaPathError as error:
        raise _archive_path_error(error)
    except OSError as error:
        raise _archive_io_error(error)
    finally:
        _close_archive_resources(destination_directory, source_directory)


def _initial_progress(intents):
    return {
        "items": [
            {"intent": intent.as_dict(), "complete": False}
            for intent in intents
        ]
    }


def _progress_flags(intents, progress):
    if not isinstance(progress, dict) or set(progress) != {"items"}:
        raise _archive_error("archive_state_conflict")
    items = progress["items"]
    if not isinstance(items, list) or len(items) != len(intents):
        raise _archive_error("archive_state_conflict")
    flags = []
    seen_incomplete = False
    for intent, item in zip(intents, items):
        if (
            not isinstance(item, dict)
            or set(item) != {"intent", "complete"}
            or item["intent"] != intent.as_dict()
            or type(item["complete"]) is not bool
        ):
            raise _archive_error("archive_state_conflict")
        complete = item["complete"]
        if not complete:
            seen_incomplete = True
        elif seen_incomplete:
            raise _archive_error("archive_state_conflict")
        flags.append(complete)
    return tuple(flags)


def _validate_progress(intents, progress, previous_progress=None):
    flags = _progress_flags(intents, progress)
    if previous_progress is not None:
        previous_flags = _progress_flags(intents, previous_progress)
        if any(
            previous_complete and not current_complete
            for previous_complete, current_complete in zip(
                previous_flags, flags
            )
        ):
            raise _archive_error("archive_state_conflict")
    return flags


def _advance_progress(intents, progress, completed_index):
    flags = _validate_progress(intents, progress)
    if (
        type(completed_index) is not int
        or completed_index < 0
        or completed_index >= len(flags)
        or flags[completed_index]
        or completed_index != sum(1 for complete in flags if complete)
    ):
        raise _archive_error("archive_state_conflict")
    updated = {
        "items": [
            {
                "intent": dict(item["intent"]),
                "complete": item["complete"],
            }
            for item in progress["items"]
        ]
    }
    updated["items"][completed_index]["complete"] = True
    _validate_progress(
        intents,
        updated,
        previous_progress=progress,
    )
    return updated


class LegacyMediaArchive(object):
    def __init__(
        self,
        source_root,
        destination_root,
        run_directory,
        filename,
        run_id,
        service_uid,
        service_gid,
        using="default",
    ):
        self.source_root = source_root
        self.destination_root = destination_root
        self.run_directory = run_directory
        self.filename = filename
        self.run_id = run_id
        self.service_uid = service_uid
        self.service_gid = service_gid
        self.using = using

    def prepare(
        self,
        has_media_image_directory=False,
        schema_only=False,
        progress=None,
        previous_progress=None,
    ):
        if schema_only:
            return ArchivePlan((), None, schema_only=True)
        validate_no_legacy_media_references(using=self.using)
        fixed_sources = _load_fixed_slot_sources(
            self.run_directory,
            self.filename,
            self.run_id,
            self.service_uid,
            self.service_gid,
        )
        if progress is None:
            if type(has_media_image_directory) is not bool:
                raise _archive_error("archive_state_conflict")
            paths = (
                [("image", "media/image")]
                if has_media_image_directory
                else []
            )
            paths.extend(
                (source, fixed_slot_destination_path(source))
                for source in fixed_sources
            )
            for _source, destination in paths:
                parent, _name = _split_relative_path(destination)
                _ensure_archive_parent(self.destination_root, parent)
            intents = tuple(
                build_archive_intent(
                    self.source_root,
                    source,
                    self.destination_root,
                    destination,
                )
                for source, destination in paths
            )
            progress = _initial_progress(intents)
        else:
            if not isinstance(progress, dict) or set(progress) != {"items"}:
                raise _archive_error("archive_state_conflict")
            items = progress["items"]
            if not isinstance(items, list):
                raise _archive_error("archive_state_conflict")
            intents = tuple(
                ArchiveIntent.from_dict(item.get("intent"))
                if isinstance(item, dict)
                else ArchiveIntent.from_dict(None)
                for item in items
            )
        _validate_archive_intent_layout(intents, fixed_sources)
        _validate_progress(intents, progress, previous_progress)
        _verify_intent_roots(
            self.source_root, self.destination_root, intents
        )
        return ArchivePlan(intents, progress)

    def converge(
        self,
        plan,
        syscall_adapter=None,
        on_item_complete=None,
    ):
        if not isinstance(plan, ArchivePlan):
            raise _archive_error("archive_state_conflict")
        if plan.schema_only:
            return ArchiveConvergence((), None)
        if on_item_complete is not None and not callable(on_item_complete):
            raise _archive_error("archive_state_conflict")
        flags = _validate_progress(plan.intents, plan.progress)
        _verify_intent_roots(
            self.source_root, self.destination_root, plan.intents
        )
        progress = plan.progress
        results = []
        for index, intent in enumerate(plan.intents):
            with open_archive_session(
                self.source_root,
                self.destination_root,
                intent,
                syscall_adapter=syscall_adapter,
            ) as session:
                result = (
                    session.recover_archive(intent)
                    if flags[index]
                    else session._converge_archive(intent)
                )
            results.append(result)
            if not flags[index]:
                progress = _advance_progress(
                    plan.intents, progress, index
                )
                if on_item_complete is not None:
                    on_item_complete(intent, progress, result)
        return ArchiveConvergence(tuple(results), progress)


def _close_archive_resources(*resources):
    first_error = None
    for resource in resources:
        if resource is None:
            continue
        try:
            resource.close()
        except BaseException as error:
            if first_error is None:
                first_error = error
    if first_error is None or sys.exc_info()[0] is not None:
        return
    if isinstance(first_error, MediaArchiveError):
        raise first_error
    if isinstance(first_error, MediaPathError):
        raise _archive_path_error(first_error)
    if isinstance(first_error, OSError):
        raise _archive_io_error(first_error)
    if isinstance(first_error, Exception):
        raise _archive_error("archive_failed", first_error)
    raise first_error


class ArchiveSession(object):
    def __init__(self, intent, resources, syscall_adapter):
        self.intent = intent
        (
            self.source_root,
            self.destination_root,
            self.source_parent,
            self.destination_parent,
            self.source_leaf,
            self.destination_leaf,
        ) = resources
        self.syscall_adapter = syscall_adapter
        self._closed = False

    @property
    def source_parent_descriptor(self):
        return self.source_parent.descriptor

    @property
    def destination_parent_descriptor(self):
        return self.destination_parent.descriptor

    def __enter__(self):
        return self

    def __exit__(self, error_type, error, traceback):
        del error, traceback
        try:
            self.close()
        except BaseException:
            if error_type is None:
                raise
        return False

    def close(self):
        if self._closed:
            return
        self._closed = True
        _close_archive_resources(
            self.destination_leaf,
            self.source_leaf,
            self.destination_parent,
            self.source_parent,
            self.destination_root,
            self.source_root,
        )

    def _require_intent(self, intent):
        if self._closed or intent != self.intent:
            raise _archive_error("archive_state_conflict")

    def _verify_anchors(self):
        try:
            return self._verify_anchors_unmapped()
        except MediaArchiveError:
            raise
        except MediaPathError as error:
            raise _archive_path_error(error)
        except OSError as error:
            raise _archive_io_error(error)

    def _verify_anchors_unmapped(self):
        self.source_root.verify_current()
        self.destination_root.verify_current()
        self.source_parent.verify_current()
        self.destination_parent.verify_current()
        source_root_stat = os.fstat(self.source_root.descriptor)
        destination_root_stat = os.fstat(self.destination_root.descriptor)
        source_parent_stat = os.fstat(self.source_parent.descriptor)
        destination_parent_stat = os.fstat(
            self.destination_parent.descriptor
        )
        expected = self.intent
        if (
            _identity(source_root_stat)
            != (expected.source_root_device, expected.source_root_inode)
            or _identity(destination_root_stat)
            != (
                expected.destination_root_device,
                expected.destination_root_inode,
            )
            or _identity(source_parent_stat)
            != (
                expected.source_parent_device,
                expected.source_parent_inode,
            )
            or _identity(destination_parent_stat)
            != (
                expected.destination_parent_device,
                expected.destination_parent_inode,
            )
        ):
            raise _archive_error("archive_state_conflict")
        _same_filesystem(
            source_root_stat,
            destination_root_stat,
            source_parent_stat,
            destination_parent_stat,
        )
        return source_parent_stat, destination_parent_stat

    def _entry_state(self):
        source_parent_stat, destination_parent_stat = (
            self._verify_anchors()
        )
        source_present = self.source_leaf is not None
        destination_present = self.destination_leaf is not None
        expected_kind = _intent_leaf_kind(self.intent)
        if source_present:
            self.source_leaf.verify_current()
            if self.source_leaf.kind != expected_kind:
                raise _archive_error("archive_state_conflict")
            _same_filesystem(
                source_parent_stat, self.source_leaf.file_stat
            )
            if _identity(self.source_leaf.file_stat) != (
                self.intent.source_device,
                self.intent.source_inode,
            ):
                raise _archive_error("archive_state_conflict")
        else:
            _require_absent(self.source_parent, self.intent.source_name)
        if destination_present:
            self.destination_leaf.verify_current()
            if self.destination_leaf.kind != expected_kind:
                raise _archive_error("archive_state_conflict")
            _same_filesystem(
                destination_parent_stat, self.destination_leaf.file_stat
            )
            if _identity(self.destination_leaf.file_stat) != (
                self.intent.source_device,
                self.intent.source_inode,
            ):
                raise _archive_error("archive_state_conflict")
        else:
            _require_absent(
                self.destination_parent,
                self.intent.destination_name,
            )
        return source_present, destination_present

    def _fsync_parents(self):
        try:
            self._verify_anchors()
            os.fsync(self.source_parent.descriptor)
            os.fsync(self.destination_parent.descriptor)
            self._verify_anchors()
        except MediaArchiveError:
            raise
        except OSError as error:
            raise _archive_error("archive_failed", error)

    def _adopt_destination_after_rename(self):
        self.source_leaf.verify_descriptor()
        _require_absent(self.source_parent, self.intent.source_name)
        destination_leaf = _open_archive_leaf(
            self.destination_parent,
            self.intent.destination_name,
        )
        try:
            if (
                destination_leaf.kind != self.source_leaf.kind
                or _identity(destination_leaf.file_stat)
                != (self.intent.source_device, self.intent.source_inode)
            ):
                raise _archive_error("archive_state_conflict")
        except BaseException:
            destination_leaf.close()
            raise
        if self.destination_leaf is not None:
            self.destination_leaf.close()
        self.destination_leaf = destination_leaf

    def archive_noreplace(self, intent):
        self._require_intent(intent)
        source_present, destination_present = self._entry_state()
        if not source_present or destination_present:
            raise _archive_error("archive_state_conflict")
        try:
            self.syscall_adapter.rename_noreplace(
                self.source_parent.descriptor,
                self.intent.source_name,
                self.destination_parent.descriptor,
                self.intent.destination_name,
            )
        except MediaArchiveError:
            raise
        except OSError as error:
            if error.errno in _UNSUPPORTED_ATOMIC_ERRNOS:
                raise _archive_error("atomic_archive_unsupported", error)
            if error.errno in _CONFLICT_ERRNOS:
                raise _archive_error("archive_state_conflict", error)
            raise _archive_error("archive_failed", error)
        self._adopt_destination_after_rename()
        self._fsync_parents()
        return ArchiveResult("archived", self.intent)

    def recover_archive(self, intent):
        self._require_intent(intent)
        source_present, destination_present = self._entry_state()
        if source_present or not destination_present:
            raise _archive_error("archive_state_conflict")
        self._fsync_parents()
        return ArchiveResult("recovered", self.intent)

    def _converge_archive(self, intent):
        self._require_intent(intent)
        source_present, destination_present = self._entry_state()
        if source_present and not destination_present:
            return self.archive_noreplace(intent)
        if not source_present and destination_present:
            return self.recover_archive(intent)
        raise _archive_error("archive_state_conflict")


def open_archive_session(
    source_root,
    destination_root,
    intent,
    syscall_adapter=None,
):
    if not isinstance(intent, ArchiveIntent):
        raise _archive_error("archive_state_conflict")
    resources = None
    try:
        resources = _open_archive_resources(
            source_root,
            destination_root,
            intent.source_parent_relative,
            intent.source_name,
            intent.destination_parent_relative,
            intent.destination_name,
            source_missing_ok=True,
            destination_missing_ok=True,
        )
        session = ArchiveSession(
            intent,
            resources,
            (
                LinuxRenameNoReplaceAdapter()
                if syscall_adapter is None
                else syscall_adapter
            ),
        )
        session._verify_anchors()
        session._entry_state()
        resources = None
        return session
    except MediaArchiveError:
        raise
    except MediaPathError as error:
        raise _archive_path_error(error)
    except OSError as error:
        raise _archive_io_error(error)
    except (TypeError, ValueError) as error:
        raise _archive_error("archive_state_conflict", error)
    finally:
        if resources is not None:
            _close_archive_resources(*reversed(resources))
