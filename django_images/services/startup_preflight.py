from dataclasses import dataclass
import os
import re
import sqlite3
import stat
from urllib.parse import quote
import uuid

from django.core.files.storage import FileSystemStorage
from django.db.migrations.loader import MigrationLoader
from django.utils.functional import LazyObject

from django_images import file_ops


MIB = 1024 * 1024
_REQUIRED_IMAGE_SIZES = frozenset(("thumbnail", "standard", "square"))
_ALLOWED_SIZE_OPTIONS = frozenset(("size", "crop", "upscale", "quality"))
_MD5_PATH = re.compile(
    r"^image/(?:original|thumbnail)/by-md5/"
    r"[0-9a-fA-F]/[0-9a-fA-F]/[0-9a-fA-F]{32}/[^/]+$"
)
_FIXED_SLOT_PATH = re.compile(
    r"^originals/"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}/original\.[a-z0-9]+$"
)
_NAMED_ORIGINAL_PATH = re.compile(
    r"^originals/"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}/(?!original\.)[^/]+\.[a-z0-9]+$"
)
_CANONICAL_DERIVATIVE_PATH = re.compile(
    r"^derivatives/"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}/"
    r"(?:thumbnail|standard|square)\.[a-z0-9]+$"
)
_PROBE_RESULT_OK = "ok"
_PROBE_RESULT_WRITE = "media_root_not_writable"
_PROBE_RESULT_LOCK = "media_lock_not_usable"
_ALLOWED_PROBE_RESULTS = frozenset((
    _PROBE_RESULT_OK,
    _PROBE_RESULT_WRITE,
    _PROBE_RESULT_LOCK,
))


class StartupPreflightError(Exception):
    def __init__(self, code):
        super(StartupPreflightError, self).__init__(code)
        self.code = code


@dataclass(frozen=True)
class LegacyEvidence(object):
    database_exists: bool
    database_bytes: int
    distinct_legacy_bytes: int
    has_md5_paths: bool
    has_fixed_slot_paths: bool
    has_named_canonical_paths: bool
    has_media_image_directory: bool
    pending_migrations: tuple

    @property
    def pending_schema(self):
        return bool(self.pending_migrations)

    @property
    def has_legacy_evidence(self):
        return (
            self.has_md5_paths
            or self.has_fixed_slot_paths
            or self.has_media_image_directory
        )


@dataclass(frozen=True)
class SpaceBudget(object):
    base_bytes: int
    margin_bytes: int
    required_bytes: int


@dataclass(frozen=True)
class PreflightResult(object):
    ok: bool
    reason_code: str = None
    field_classes: tuple = ()


def inspect_legacy_evidence(
    database_path, disk_migration_graph, media_root
):
    media_directory = None
    database_root = None
    database_receipt = None
    connection = None
    try:
        normalized_database_path = _configured_path(database_path)
        if normalized_database_path is None:
            raise StartupPreflightError("legacy_evidence_invalid")
        media_directory, has_media_image_directory = _open_media_root(
            media_root
        )
        database_root, database_receipt = _open_database(
            normalized_database_path
        )
        if database_receipt is None:
            return LegacyEvidence(
                database_exists=False,
                database_bytes=0,
                distinct_legacy_bytes=0,
                has_md5_paths=False,
                has_fixed_slot_paths=False,
                has_named_canonical_paths=False,
                has_media_image_directory=has_media_image_directory,
                pending_migrations=(),
            )

        database_receipt.verify_current()
        database_uri = "file:{}?mode=ro".format(
            quote(normalized_database_path, safe="/")
        )
        connection = sqlite3.connect(database_uri, uri=True)
        database_receipt.verify_current()
        connection.execute("PRAGMA query_only = ON")
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        paths = []
        for table_name in (
            "django_images_image",
            "django_images_thumbnail",
        ):
            if table_name in tables:
                paths.extend(
                    row[0]
                    for row in connection.execute(
                        "SELECT image FROM {}".format(table_name)
                    )
                )
        applied_migrations = set()
        if "django_migrations" in tables:
            applied_migrations = {
                (row[0], row[1])
                for row in connection.execute(
                    "SELECT app, name FROM django_migrations"
                )
            }
        database_receipt.verify_current()

        if disk_migration_graph is None:
            disk_migration_graph = MigrationLoader(None).graph
        disk_nodes = _migration_nodes(disk_migration_graph)
        pending_migrations = tuple(sorted(disk_nodes - applied_migrations))
        classified = _classify_paths(paths)
        distinct_legacy_bytes = _distinct_legacy_bytes(
            media_directory,
            classified["copy_paths"],
        )
        database_receipt.verify_current()
        database_bytes = database_receipt.file_stat.st_size
        evidence = LegacyEvidence(
            database_exists=True,
            database_bytes=database_bytes,
            distinct_legacy_bytes=distinct_legacy_bytes,
            has_md5_paths=classified["has_md5_paths"],
            has_fixed_slot_paths=classified["has_fixed_slot_paths"],
            has_named_canonical_paths=(
                classified["has_named_canonical_paths"]
            ),
            has_media_image_directory=has_media_image_directory,
            pending_migrations=pending_migrations,
        )
        connection.close()
        connection = None
        database_receipt.verify_current()
        return evidence
    except BaseException as error:
        if isinstance(error, StartupPreflightError):
            raise
        if not isinstance(error, Exception):
            raise
        raise StartupPreflightError("legacy_evidence_invalid") from error
    finally:
        if connection is not None:
            try:
                connection.close()
            except BaseException:
                pass
            if database_receipt is not None:
                try:
                    database_receipt.verify_current()
                except BaseException:
                    pass
        if database_receipt is not None:
            try:
                database_receipt.close()
            except BaseException:
                pass
        if database_root is not None:
            try:
                database_root.close()
            except BaseException:
                pass
        if media_directory is not None:
            try:
                media_directory.close()
            except BaseException:
                pass


def calculate_initial_space(database_bytes, distinct_legacy_bytes):
    base_bytes = database_bytes + distinct_legacy_bytes
    margin_bytes = max(64 * MIB, (base_bytes + 19) // 20)
    return SpaceBudget(
        base_bytes=base_bytes,
        margin_bytes=margin_bytes,
        required_bytes=base_bytes + margin_bytes,
    )


def calculate_remaining_space(copy_required_bytes, initial_margin):
    return SpaceBudget(
        base_bytes=copy_required_bytes,
        margin_bytes=initial_margin,
        required_bytes=copy_required_bytes + initial_margin,
    )


def available_space_bytes(path):
    filesystem = os.statvfs(path)
    return filesystem.f_bavail * filesystem.f_frsize


def validate_storage_preflight(
    media_root,
    image_storage,
    thumbnail_storage,
    image_sizes,
    service_uid,
    service_gid,
):
    invalid_fields = []
    normalized_media_root = _configured_path(media_root)
    if normalized_media_root is None or not _is_verified_directory(
        normalized_media_root
    ):
        invalid_fields.append("MEDIA_ROOT")
    if not _storage_matches(image_storage, normalized_media_root):
        invalid_fields.append("Image.image.storage")
    if not _storage_matches(thumbnail_storage, normalized_media_root):
        invalid_fields.append("Thumbnail.image.storage")
    if not _valid_image_sizes(image_sizes):
        invalid_fields.append("IMAGE_SIZES")
    if (
        type(service_uid) is not int
        or service_uid < 0
        or type(service_gid) is not int
        or service_gid < 0
    ):
        invalid_fields.append("service_identity")
    if invalid_fields:
        return PreflightResult(
            ok=False,
            reason_code="media_storage_configuration_invalid",
            field_classes=tuple(invalid_fields),
        )

    root_directory = None
    try:
        root_directory = file_ops.open_verified_media_root(
            normalized_media_root
        )
        file_ops.recover_media_lock_state(
            root_directory,
            service_uid,
            service_gid,
        )
    except Exception:
        return PreflightResult(
            ok=False,
            reason_code="unsafe_media_lock_state",
        )
    finally:
        if root_directory is not None:
            try:
                root_directory.close()
            except BaseException:
                pass

    probe_result = _run_service_probe(
        normalized_media_root,
        service_uid,
        service_gid,
    )
    if probe_result != _PROBE_RESULT_OK:
        return PreflightResult(ok=False, reason_code=probe_result)
    return PreflightResult(ok=True)


def _open_media_root(media_root):
    normalized = _configured_path(media_root)
    if normalized is None:
        raise StartupPreflightError("legacy_evidence_invalid")
    try:
        os.stat(normalized, follow_symlinks=False)
    except FileNotFoundError:
        return None, False
    directory = file_ops.open_verified_media_root(normalized)
    try:
        directory.verify_current()
        try:
            image_stat = os.stat(
                "image",
                dir_fd=directory.descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return directory, False
        if not stat.S_ISDIR(image_stat.st_mode):
            raise StartupPreflightError("legacy_evidence_invalid")
        return directory, True
    except BaseException:
        directory.close()
        raise


def _open_database(database_path):
    normalized = _configured_path(database_path)
    if normalized is None:
        raise StartupPreflightError("legacy_evidence_invalid")
    parent_path, leaf_name = os.path.split(normalized)
    if not parent_path or not leaf_name:
        raise StartupPreflightError("legacy_evidence_invalid")
    root_directory = file_ops.open_verified_media_root(parent_path)
    try:
        receipt = file_ops.open_verified_media_file(
            root_directory,
            leaf_name,
            missing_ok=True,
        )
        return root_directory, receipt
    except BaseException:
        root_directory.close()
        raise


def _migration_nodes(graph):
    nodes = getattr(graph, "nodes", None)
    if nodes is None:
        raise StartupPreflightError("legacy_evidence_invalid")
    node_set = set(nodes)
    if any(
        type(node) is not tuple
        or len(node) != 2
        or type(node[0]) is not str
        or not node[0]
        or type(node[1]) is not str
        or not node[1]
        for node in node_set
    ):
        raise StartupPreflightError("legacy_evidence_invalid")
    return node_set


def _classify_paths(paths):
    has_md5_paths = False
    has_fixed_slot_paths = False
    has_named_canonical_paths = False
    copy_paths = set()
    for path in paths:
        if type(path) is not str or not path:
            raise StartupPreflightError("legacy_evidence_invalid")
        if _MD5_PATH.fullmatch(path):
            has_md5_paths = True
            copy_paths.add(path)
        elif _FIXED_SLOT_PATH.fullmatch(path):
            has_fixed_slot_paths = True
            copy_paths.add(path)
        elif (
            _NAMED_ORIGINAL_PATH.fullmatch(path)
            or _CANONICAL_DERIVATIVE_PATH.fullmatch(path)
        ):
            has_named_canonical_paths = True
    return {
        "has_md5_paths": has_md5_paths,
        "has_fixed_slot_paths": has_fixed_slot_paths,
        "has_named_canonical_paths": has_named_canonical_paths,
        "copy_paths": copy_paths,
    }


def _distinct_legacy_bytes(media_directory, copy_paths):
    if not copy_paths:
        return 0
    if media_directory is None:
        return 0
    total = 0
    for relative_path in sorted(copy_paths):
        receipt = file_ops.open_verified_media_file(
            media_directory,
            relative_path,
            missing_ok=True,
        )
        if receipt is None:
            continue
        try:
            receipt.verify_current()
            total += receipt.file_stat.st_size
        finally:
            receipt.close()
    return total


def _configured_path(value):
    try:
        value = os.fspath(value)
    except TypeError:
        return None
    if (
        type(value) is not str
        or not value
        or not os.path.isabs(value)
        or "\\" in value
        or "\x00" in value
    ):
        return None
    normalized = os.path.normpath(value)
    if normalized != value:
        return None
    return normalized


def _is_verified_directory(path):
    directory = None
    try:
        directory = file_ops.open_verified_media_root(path)
        directory.verify_current()
        return True
    except Exception:
        return False
    finally:
        if directory is not None:
            try:
                directory.close()
            except BaseException:
                pass


def _storage_matches(storage, media_root):
    if media_root is None:
        return False
    try:
        resolved_storage = storage
        if isinstance(storage, LazyObject):
            storage._setup()
            resolved_storage = storage._wrapped
        if type(resolved_storage) is not FileSystemStorage:
            return False
        location = _configured_path(resolved_storage.location)
        return location == media_root
    except Exception:
        return False


def _valid_image_sizes(image_sizes):
    if type(image_sizes) is not dict:
        return False
    if set(image_sizes) != _REQUIRED_IMAGE_SIZES:
        return False
    return all(
        _valid_image_size_options(image_sizes[name])
        for name in sorted(_REQUIRED_IMAGE_SIZES)
    )


def _valid_image_size_options(options):
    if type(options) is not dict or set(options) - _ALLOWED_SIZE_OPTIONS:
        return False
    size = options.get("size")
    if (
        type(size) not in (list, tuple)
        or len(size) != 2
        or any(type(value) is not int or value < 0 for value in size)
        or size == [0, 0]
        or size == (0, 0)
    ):
        return False
    for boolean_name in ("crop", "upscale"):
        if (
            boolean_name in options
            and type(options[boolean_name]) is not bool
        ):
            return False
    if options.get("crop", False) and any(value == 0 for value in size):
        return False
    if "quality" in options:
        quality = options["quality"]
        if quality is not None and (
            type(quality) is not int or not 1 <= quality <= 95
        ):
            return False
    return True


def _run_service_probe(media_root, service_uid, service_gid):
    if not callable(getattr(os, "fork", None)):
        return _PROBE_RESULT_LOCK
    read_descriptor, write_descriptor = os.pipe()
    try:
        child_pid = os.fork()
    except BaseException:
        os.close(read_descriptor)
        os.close(write_descriptor)
        return _PROBE_RESULT_LOCK
    if child_pid == 0:  # pragma: no cover - assertions run in parent
        try:
            os.close(read_descriptor)
            result = _service_probe_child(
                media_root,
                service_uid,
                service_gid,
            )
            try:
                os.write(write_descriptor, result.encode("ascii"))
            except BaseException:
                pass
        finally:
            try:
                os.close(write_descriptor)
            except BaseException:
                pass
            os._exit(0)

    os.close(write_descriptor)
    try:
        payload = b""
        while len(payload) <= 64:
            chunk = os.read(read_descriptor, 65 - len(payload))
            if not chunk:
                break
            payload += chunk
    except BaseException:
        payload = b""
    finally:
        os.close(read_descriptor)
    try:
        waited_pid, wait_status = os.waitpid(child_pid, 0)
    except BaseException:
        return _PROBE_RESULT_LOCK
    if (
        waited_pid != child_pid
        or not os.WIFEXITED(wait_status)
        or os.WEXITSTATUS(wait_status) != 0
    ):
        return _PROBE_RESULT_LOCK
    try:
        result = payload.decode("ascii")
    except UnicodeDecodeError:
        return _PROBE_RESULT_LOCK
    if result not in _ALLOWED_PROBE_RESULTS:
        return _PROBE_RESULT_LOCK
    return result


def _service_probe_child(media_root, service_uid, service_gid):
    try:
        identity_changed = os.geteuid() == 0 or (
            os.geteuid() != service_uid or os.getegid() != service_gid
        )
        if identity_changed:
            _drop_service_identity(service_uid, service_gid)
            if (
                os.geteuid() != service_uid
                or os.getegid() != service_gid
                or os.getgroups()
            ):
                return _PROBE_RESULT_WRITE
        _probe_media_root_write(media_root)
    except BaseException:
        return _PROBE_RESULT_WRITE
    try:
        _probe_media_locks(media_root)
    except BaseException:
        return _PROBE_RESULT_LOCK
    return _PROBE_RESULT_OK


def _drop_service_identity(service_uid, service_gid):
    os.setgroups([])
    os.setgid(service_gid)
    os.setuid(service_uid)


def _probe_media_root_write(media_root):
    root_directory = file_ops.open_verified_media_root(media_root)
    descriptor = None
    expected_stat = None
    name = ".svrx-pinry-write-probe-{}".format(uuid.uuid4().hex)
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        descriptor = os.open(
            name,
            flags,
            0o600,
            dir_fd=root_directory.descriptor,
        )
        os.fchmod(descriptor, 0o600)
        expected_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(expected_stat.st_mode)
            or expected_stat.st_nlink != 1
            or stat.S_IMODE(expected_stat.st_mode) != 0o600
            or expected_stat.st_uid != os.geteuid()
            or expected_stat.st_gid != os.getegid()
        ):
            raise OSError("invalid probe identity")
        os.fsync(descriptor)
        root_directory.verify_current()
        if not _unlink_probe_if_owned(
            root_directory.descriptor,
            name,
            expected_stat,
        ):
            raise OSError("probe identity changed")
        os.fsync(root_directory.descriptor)
        expected_stat = None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except BaseException:
                pass
        if expected_stat is not None:
            _unlink_probe_if_owned(
                root_directory.descriptor,
                name,
                expected_stat,
            )
        root_directory.close()


def _unlink_probe_if_owned(directory_descriptor, name, expected_stat):
    try:
        current = os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return True
    if (
        not stat.S_ISREG(current.st_mode)
        or current.st_nlink != 1
        or (current.st_dev, current.st_ino)
        != (expected_stat.st_dev, expected_stat.st_ino)
    ):
        return False
    os.unlink(name, dir_fd=directory_descriptor)
    return True


def _probe_media_locks(media_root):
    root_directory = file_ops.open_verified_media_root(media_root)
    try:
        with file_ops.media_dedup_lock(
            root_directory,
            1,
            "0" * 64,
        ):
            with file_ops.media_lifecycle_lock(
                root_directory,
                exclusive=True,
            ):
                pass
    finally:
        root_directory.close()
