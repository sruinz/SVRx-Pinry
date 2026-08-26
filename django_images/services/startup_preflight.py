from dataclasses import dataclass
import errno
import os
import re
import resource
import sqlite3
import stat
from urllib.parse import quote
import uuid

from django.core.files.storage import FileSystemStorage
from django.db.migrations.loader import MigrationLoader
from django.utils.functional import LazyObject
from django.utils.text import get_valid_filename

from django_images import file_ops
from django_images.paths import (
    FORMAT_EXTENSIONS,
    PINRY_DIRECT_MD5_ROOTS,
    canonical_derivative_path,
    canonical_original_path,
    is_valid_original_leaf,
    pinry_direct_md5_root,
)
from django_images.services import sqlite_snapshot


MIB = 1024 * 1024
_REQUIRED_IMAGE_SIZES = frozenset(("thumbnail", "standard", "square"))
_ALLOWED_SIZE_OPTIONS = frozenset(("size", "crop", "upscale", "quality"))
_MD5_PATH = re.compile(
    r"^image/(?:original|thumbnail)/by-md5/"
    r"[0-9a-fA-F]/[0-9a-fA-F]/[0-9a-fA-F]{32}/[^/]+$"
)
_ORIGINAL_PATH = re.compile(
    r"^originals/"
    r"(?P<asset_uuid>"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12})/"
    r"(?P<leaf>[^/]+)$"
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
_PROBE_RESULT_DESCRIPTOR = 3
_DESCRIPTOR_FALLBACK_LIMIT = 1 << 20
_LEGACY_EVIDENCE_STAGE_CODES = {
    "database": "legacy_database_invalid",
    "database_schema": "legacy_database_schema_invalid",
    "media_files": "legacy_media_files_invalid",
    "media_root": "legacy_media_root_invalid",
    "media_rows": "legacy_media_rows_invalid",
    "migration_graph": "legacy_migration_graph_invalid",
}


class StartupPreflightError(Exception):
    def __init__(self, code):
        super(StartupPreflightError, self).__init__(code)
        self.code = code


@dataclass(frozen=True)
class MissingUnreferencedImage(object):
    image_id: int
    image_path: str
    thumbnail_rows: tuple
    image_signature: tuple = ()
    thumbnail_signatures: tuple = ()


@dataclass(frozen=True)
class LegacyEvidence(object):
    database_exists: bool
    database_bytes: int
    distinct_legacy_bytes: int
    has_md5_paths: bool
    has_fixed_slot_paths: bool
    has_named_canonical_paths: bool
    has_media_image_directory: bool
    has_media_rows: bool
    pending_migrations: tuple
    database_identity: object
    media_root_identity: object
    has_pinry_direct_md5_directory: bool = False
    missing_unreferenced_images: tuple = ()

    @property
    def pending_schema(self):
        return bool(self.pending_migrations)

    @property
    def has_legacy_evidence(self):
        return (
            self.has_md5_paths
            or self.has_fixed_slot_paths
            or self.has_media_image_directory
            or self.has_pinry_direct_md5_directory
            or bool(self.missing_unreferenced_images)
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
    stage = "database"
    media_directory = None
    database_root = None
    database_receipt = None
    database_guard = None
    connection = None
    try:
        normalized_database_path = _configured_path(database_path)
        if normalized_database_path is None:
            raise StartupPreflightError("legacy_evidence_invalid")
        stage = "media_root"
        media_directory, has_media_image_directory = _open_media_root(
            media_root
        )
        has_pinry_direct_md5_directory = _has_pinry_direct_md5_directory(
            media_directory
        )
        media_root_identity = _directory_identity(media_directory)
        stage = "database"
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
                has_media_rows=False,
                pending_migrations=(),
                database_identity=None,
                media_root_identity=media_root_identity,
                has_pinry_direct_md5_directory=(
                    has_pinry_direct_md5_directory
                ),
            )

        database_receipt.verify_current()
        database_guard = sqlite_snapshot._SourceMutationGuard(
            database_receipt.descriptor
        )
        database_uri = _database_receipt_uri(database_receipt, "ro")
        connection = sqlite3.connect(database_uri, uri=True)
        _verify_read_database(database_receipt, database_guard)
        connection.execute("PRAGMA query_only = ON")
        stage = "database_schema"
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        image_rows, thumbnail_rows = _read_media_rows(
            connection, tables
        )
        has_media_rows = bool(
            image_rows
            or thumbnail_rows
            or _table_has_rows(connection, tables, "core_mediaasset")
        )
        applied_migrations = set()
        if "django_migrations" in tables:
            applied_migrations = {
                (row[0], row[1])
                for row in connection.execute(
                    "SELECT app, name FROM django_migrations"
                )
            }
        _verify_read_database(database_receipt, database_guard)

        stage = "migration_graph"
        if disk_migration_graph is None:
            disk_migration_graph = MigrationLoader(None).graph
        disk_nodes = _migration_nodes(disk_migration_graph)
        pending_migrations = tuple(sorted(disk_nodes - applied_migrations))
        stage = "media_rows"
        classified = _classify_media_rows(image_rows, thumbnail_rows)
        stage = "media_files"
        missing_unreferenced_images = _missing_unreferenced_images(
            connection,
            tables,
            image_rows,
            thumbnail_rows,
            media_directory,
        )
        if missing_unreferenced_images:
            missing_ids = {
                candidate.image_id
                for candidate in missing_unreferenced_images
            }
            image_rows = tuple(
                row for row in image_rows if row["id"] not in missing_ids
            )
            thumbnail_rows = tuple(
                row for row in thumbnail_rows
                if row.get("original_id") not in missing_ids
            )
            classified = _classify_media_rows(
                image_rows,
                thumbnail_rows,
            )
        stage = "media_files"
        distinct_legacy_bytes = _distinct_legacy_bytes(
            media_directory,
            classified["copy_paths"],
        )
        stage = "database"
        _verify_read_database(database_receipt, database_guard)
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
            has_media_rows=has_media_rows,
            pending_migrations=pending_migrations,
            database_identity=_file_identity(database_receipt),
            media_root_identity=media_root_identity,
            has_pinry_direct_md5_directory=(
                has_pinry_direct_md5_directory
            ),
            missing_unreferenced_images=missing_unreferenced_images,
        )
        connection.close()
        connection = None
        _verify_read_database(database_receipt, database_guard)
        database_guard.close()
        database_guard = None
        return evidence
    except BaseException as error:
        if (
            isinstance(error, StartupPreflightError)
            and error.code != "legacy_evidence_invalid"
        ):
            raise
        if not isinstance(error, Exception):
            raise
        code = _LEGACY_EVIDENCE_STAGE_CODES.get(
            stage, "legacy_evidence_invalid"
        )
        raise StartupPreflightError(code) from error
    finally:
        if connection is not None:
            try:
                connection.close()
            except BaseException:
                pass
            if database_receipt is not None:
                try:
                    if database_guard is not None:
                        _verify_read_database(
                            database_receipt,
                            database_guard,
                        )
                    else:
                        database_receipt.verify_current()
                except BaseException:
                    pass
        if database_guard is not None:
            try:
                database_guard.close()
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


def remove_confirmed_missing_unreferenced_images(
    database_path,
    media_root,
    candidates,
    expected_database_identity,
    expected_media_root_identity,
    backup_verifier,
):
    _require_cleanup_candidates(candidates)
    _require_cleanup_identity(
        expected_database_identity,
        "legacy_database_invalid",
    )
    _require_cleanup_identity(
        expected_media_root_identity,
        "legacy_media_root_invalid",
    )
    if not candidates:
        return 0
    _verify_cleanup_backup(backup_verifier)

    normalized_database_path = _configured_path(database_path)
    normalized_media_root = _configured_path(media_root)
    if normalized_database_path is None or normalized_media_root is None:
        raise StartupPreflightError("legacy_evidence_invalid")

    media_directory = None
    database_root = None
    database_receipt = None
    database_guard = None
    connection = None
    try:
        media_directory = file_ops.open_verified_media_root(
            normalized_media_root
        )
        database_root, database_receipt = _open_database(
            normalized_database_path
        )
        if database_receipt is None:
            raise StartupPreflightError("legacy_database_invalid")
        _verify_media_root_identity(
            media_directory,
            expected_media_root_identity,
        )
        _verify_database_identity(
            database_receipt,
            expected_database_identity,
        )
        file_ops.recover_media_lock_state(
            media_directory,
            os.geteuid(),
            os.getegid(),
        )
        with file_ops.media_lifecycle_lock(
            media_directory,
            exclusive=True,
        ):
            _verify_media_root_identity(
                media_directory,
                expected_media_root_identity,
            )
            _verify_database_identity(
                database_receipt,
                expected_database_identity,
            )
            database_guard = sqlite_snapshot._SourceMutationGuard(
                database_receipt.descriptor
            )
            try:
                _verify_cleanup_backup(backup_verifier)
                connection = _open_cleanup_connection(database_receipt)
                _verify_cleanup_identities(
                    database_receipt,
                    database_guard,
                    expected_database_identity,
                    media_directory,
                    expected_media_root_identity,
                )
                removed = _remove_cleanup_candidates(
                    connection,
                    media_directory,
                    database_receipt,
                    candidates,
                )
                _verify_cleanup_identities(
                    database_receipt,
                    database_guard,
                    expected_database_identity,
                    media_directory,
                    expected_media_root_identity,
                    database_mutated=True,
                )
                _verify_cleanup_backup(backup_verifier)
                connection.commit()
                _verify_cleanup_identities(
                    database_receipt,
                    database_guard,
                    expected_database_identity,
                    media_directory,
                    expected_media_root_identity,
                    database_mutated=True,
                )
                connection.close()
                connection = None
                _verify_cleanup_identities(
                    database_receipt,
                    database_guard,
                    expected_database_identity,
                    media_directory,
                    expected_media_root_identity,
                    database_mutated=True,
                )
            except BaseException:
                _rollback_cleanup(connection)
                raise
            finally:
                if connection is not None:
                    try:
                        connection.close()
                    except BaseException:
                        pass
                    connection = None
                if database_guard is not None:
                    database_guard.close()
                    database_guard = None
        return removed
    except StartupPreflightError:
        raise
    except sqlite_snapshot.SQLiteSnapshotError as error:
        raise StartupPreflightError("legacy_database_invalid") from error
    except file_ops.MediaPathError as error:
        raise StartupPreflightError(
            "legacy_media_files_invalid"
        ) from error
    except (OSError, sqlite3.Error) as error:
        raise StartupPreflightError("legacy_database_invalid") from error
    finally:
        if connection is not None:
            try:
                connection.close()
            except BaseException:
                pass
        if database_guard is not None:
            try:
                database_guard.close()
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


def _verify_cleanup_backup(backup_verifier):
    verify_current = getattr(backup_verifier, "verify_current", None)
    if not callable(verify_current):
        raise StartupPreflightError("legacy_database_invalid")
    try:
        verify_current()
    except StartupPreflightError:
        raise
    except sqlite_snapshot.SQLiteSnapshotError as error:
        raise StartupPreflightError("legacy_database_invalid") from error
    except (OSError, sqlite3.Error, TypeError, ValueError) as error:
        raise StartupPreflightError("legacy_database_invalid") from error


def _require_cleanup_candidates(candidates):
    if (
        not isinstance(candidates, tuple)
        or any(
            not isinstance(candidate, MissingUnreferencedImage)
            for candidate in candidates
        )
        or candidates != tuple(sorted(
            candidates,
            key=lambda candidate: candidate.image_id,
        ))
        or len(candidates)
        != len(set(candidate.image_id for candidate in candidates))
    ):
        raise StartupPreflightError("legacy_media_rows_invalid")


def _require_cleanup_identity(identity, error_code):
    if (
        not isinstance(identity, dict)
        or set(identity) != {"device", "inode"}
        or any(
            type(identity[field]) is not int or identity[field] < 0
            for field in ("device", "inode")
        )
    ):
        raise StartupPreflightError(error_code)


def _verify_read_database(database_receipt, database_guard):
    database_guard.verify_unchanged()
    database_receipt.verify_current()
    database_guard.verify_unchanged()


def _verify_media_root_identity(media_directory, expected_identity):
    try:
        media_directory.verify_current()
        current_identity = _directory_identity(media_directory)
    except (OSError, file_ops.MediaPathError) as error:
        raise StartupPreflightError(
            "legacy_media_root_invalid"
        ) from error
    if current_identity != expected_identity:
        raise StartupPreflightError("legacy_media_root_invalid")


def _verify_database_identity(
    database_receipt,
    expected_identity,
    mutated=False,
):
    try:
        if mutated:
            _verify_mutated_database_identity(database_receipt)
        else:
            database_receipt.verify_current()
        current_stat = os.fstat(database_receipt.descriptor)
    except (OSError, file_ops.MediaPathError) as error:
        raise StartupPreflightError("legacy_database_invalid") from error
    current_identity = {
        "device": current_stat.st_dev,
        "inode": current_stat.st_ino,
    }
    if current_identity != expected_identity:
        raise StartupPreflightError("legacy_database_invalid")


def _verify_cleanup_identities(
    database_receipt,
    database_guard,
    expected_database_identity,
    media_directory,
    expected_media_root_identity,
    database_mutated=False,
):
    database_guard.verify_unchanged()
    _verify_database_identity(
        database_receipt,
        expected_database_identity,
        mutated=database_mutated,
    )
    database_guard.verify_unchanged()
    _verify_media_root_identity(
        media_directory,
        expected_media_root_identity,
    )
    database_guard.verify_unchanged()


def _database_receipt_uri(database_receipt, mode):
    connection_path = os.path.join(
        sqlite_snapshot._directory_descriptor_path(
            database_receipt.parent_directory.descriptor
        ),
        database_receipt.name,
    )
    return "file:{}?mode={}".format(
        quote(connection_path, safe="/"),
        mode,
    )


def _open_cleanup_connection(database_receipt):
    database_uri = _database_receipt_uri(database_receipt, "rw")
    connection = None
    try:
        connection = sqlite3.connect(database_uri, uri=True)
        connection.execute("PRAGMA foreign_keys = ON")
        if connection.execute("PRAGMA foreign_keys").fetchone() != (1,):
            raise StartupPreflightError("legacy_database_invalid")
        connection.execute("BEGIN IMMEDIATE")
        return connection
    except BaseException:
        if connection is not None:
            connection.close()
        raise


def _remove_cleanup_candidates(
    connection,
    media_directory,
    database_receipt,
    candidates,
):
    database_receipt.verify_current()
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    image_rows, thumbnail_rows = _read_media_rows(connection, tables)
    _classify_media_rows(image_rows, thumbnail_rows)
    _validate_image_foreign_keys(connection, tables)
    _validate_media_delete_triggers(connection)
    _validate_cleanup_candidates(
        connection,
        tables,
        image_rows,
        thumbnail_rows,
        media_directory,
        candidates,
    )
    for candidate in candidates:
        _delete_cleanup_candidate(connection, candidate)
    database_receipt.verify_current()
    return len(candidates)


def _delete_cleanup_candidate(connection, candidate):
    for thumbnail_id, thumbnail_path in candidate.thumbnail_rows:
        deleted = connection.execute(
            "DELETE FROM django_images_thumbnail "
            "WHERE id = ? AND original_id = ? AND image = ?",
            (thumbnail_id, candidate.image_id, thumbnail_path),
        )
        if deleted.rowcount != 1:
            raise StartupPreflightError("legacy_media_rows_invalid")
    deleted = connection.execute(
        "DELETE FROM django_images_image "
        "WHERE id = ? AND image = ?",
        (candidate.image_id, candidate.image_path),
    )
    if deleted.rowcount != 1:
        raise StartupPreflightError("legacy_media_rows_invalid")


def _rollback_cleanup(connection):
    if connection is None:
        return
    try:
        connection.rollback()
    except BaseException:
        pass


def _verify_mutated_database_identity(receipt):
    try:
        receipt.root_directory.verify_current()
        receipt.parent_directory.verify_current()
        descriptor_stat = os.fstat(receipt.descriptor)
        named_stat = os.stat(
            receipt.name,
            dir_fd=receipt.parent_directory.descriptor,
            follow_symlinks=False,
        )
    except (OSError, file_ops.MediaPathError) as error:
        raise file_ops.MediaPathError("unsafe_media_file") from error
    expected_identity = _stat_identity(receipt.file_stat)
    if (
        not stat.S_ISREG(descriptor_stat.st_mode)
        or not stat.S_ISREG(named_stat.st_mode)
        or descriptor_stat.st_nlink != 1
        or named_stat.st_nlink != 1
        or _stat_identity(descriptor_stat) != expected_identity
        or _stat_identity(named_stat) != expected_identity
    ):
        raise file_ops.MediaPathError("unsafe_media_file")


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


def validate_storage_configuration_preflight(
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

    return PreflightResult(ok=True)


def validate_storage_runtime_preflight(
    media_root,
    service_uid,
    service_gid,
):
    normalized_media_root = _configured_path(media_root)
    if (
        normalized_media_root is None
        or not _is_verified_directory(normalized_media_root)
        or type(service_uid) is not int
        or service_uid < 0
        or type(service_gid) is not int
        or service_gid < 0
    ):
        return PreflightResult(
            ok=False,
            reason_code="media_storage_configuration_invalid",
        )
    probe_result = _run_service_probe(
        normalized_media_root,
        service_uid,
        service_gid,
    )
    if probe_result != _PROBE_RESULT_OK:
        return PreflightResult(ok=False, reason_code=probe_result)
    return PreflightResult(ok=True)


def validate_storage_preflight(
    media_root,
    image_storage,
    thumbnail_storage,
    image_sizes,
    service_uid,
    service_gid,
):
    configuration = validate_storage_configuration_preflight(
        media_root,
        image_storage,
        thumbnail_storage,
        image_sizes,
        service_uid,
        service_gid,
    )
    if not configuration.ok:
        return configuration
    return validate_storage_runtime_preflight(
        media_root,
        service_uid,
        service_gid,
    )


def ensure_media_root_layout(
    data_root,
    media_root,
    service_uid,
    service_gid,
):
    """보호 data root 아래의 누락 directory component만 생성한다."""
    if (
        type(service_uid) is not int
        or service_uid < 0
        or type(service_gid) is not int
        or service_gid < 0
    ):
        raise StartupPreflightError(
            "media_storage_configuration_invalid"
        )
    root_directory = None
    descriptor = None
    try:
        data_root, relative_components = _contained_components(
            data_root,
            media_root,
        )
        root_directory = file_ops.open_verified_media_root(data_root)
        descriptor = os.dup(root_directory.descriptor)
        for component in relative_components:
            created = False
            try:
                named_stat = os.stat(
                    component,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                os.mkdir(component, 0o700, dir_fd=descriptor)
                created = True
                named_stat = os.stat(
                    component,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
            if not stat.S_ISDIR(named_stat.st_mode):
                raise StartupPreflightError(
                    "media_storage_configuration_invalid"
                )
            child = os.open(
                component,
                _directory_open_flags(),
                dir_fd=descriptor,
            )
            try:
                opened_stat = os.fstat(child)
                if (
                    not stat.S_ISDIR(opened_stat.st_mode)
                    or _stat_identity(opened_stat)
                    != _stat_identity(named_stat)
                ):
                    raise StartupPreflightError(
                        "media_storage_configuration_invalid"
                    )
                if created:
                    os.fchown(child, service_uid, service_gid)
                    os.fchmod(child, 0o700)
                    os.fsync(child)
                    os.fsync(descriptor)
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        root_directory.verify_current()
    except StartupPreflightError:
        raise
    except Exception as error:
        raise StartupPreflightError(
            "media_storage_configuration_invalid"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if root_directory is not None:
            root_directory.close()


def adjust_storage_ownership(
    data_root,
    managed_paths,
    service_uid,
    service_gid,
    startup_lock_descriptor,
):
    """필요한 data child만 nofollow로 순회해 service owner로 맞춘다."""
    if (
        type(service_uid) is not int
        or service_uid < 0
        or type(service_gid) is not int
        or service_gid < 0
        or type(startup_lock_descriptor) is not int
        or type(managed_paths) not in (list, tuple)
    ):
        raise StartupPreflightError("unsafe_storage_ownership")
    root_directory = None
    try:
        normalized_root = _configured_path(data_root)
        if normalized_root is None:
            raise StartupPreflightError("unsafe_storage_ownership")
        root_directory = file_ops.open_verified_media_root(normalized_root)
        lock_stat = _verify_startup_lock_receipt(
            root_directory,
            startup_lock_descriptor,
        )
        components_by_path = []
        for managed_path in managed_paths:
            _root, components = _contained_components(
                normalized_root,
                managed_path,
            )
            if components[0] == ".svrx-pinry-startup.lock":
                raise StartupPreflightError("unsafe_storage_ownership")
            components_by_path.append(components)
        os.fchown(
            root_directory.descriptor,
            lock_stat[2],
            service_gid,
        )
        os.fchmod(root_directory.descriptor, 0o1770)
        for components in components_by_path:
            _chown_managed_path(
                root_directory,
                components,
                service_uid,
                service_gid,
            )
        os.fsync(root_directory.descriptor)
        root_directory.verify_current()
        normalized_root_stat = os.fstat(root_directory.descriptor)
        if (
            normalized_root_stat.st_uid != lock_stat[2]
            or normalized_root_stat.st_gid != service_gid
            or stat.S_IMODE(normalized_root_stat.st_mode) != 0o1770
        ):
            raise StartupPreflightError("unsafe_storage_ownership")
        if _lock_receipt(os.fstat(startup_lock_descriptor)) != lock_stat:
            raise StartupPreflightError("unsafe_storage_ownership")
        if _verify_startup_lock_receipt(
            root_directory,
            startup_lock_descriptor,
        ) != lock_stat:
            raise StartupPreflightError("unsafe_storage_ownership")
    except StartupPreflightError as error:
        if error.code == "unsafe_storage_ownership":
            raise
        raise StartupPreflightError("unsafe_storage_ownership") from error
    except Exception as error:
        raise StartupPreflightError("unsafe_storage_ownership") from error
    finally:
        if root_directory is not None:
            root_directory.close()


def _directory_identity(directory):
    if directory is None:
        return None
    file_stat = os.fstat(directory.descriptor)
    return {"device": file_stat.st_dev, "inode": file_stat.st_ino}


def _file_identity(receipt):
    file_stat = receipt.file_stat
    return {"device": file_stat.st_dev, "inode": file_stat.st_ino}


def _stat_identity(file_stat):
    return file_stat.st_dev, file_stat.st_ino


def _directory_open_flags():
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    return flags


def _file_open_flags():
    flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    return flags


def _contained_components(data_root, target_path):
    normalized_root = _configured_path(data_root)
    normalized_target = _configured_path(target_path)
    if normalized_root is None or normalized_target is None:
        raise StartupPreflightError(
            "media_storage_configuration_invalid"
        )
    try:
        if (
            normalized_target == normalized_root
            or os.path.commonpath((normalized_root, normalized_target))
            != normalized_root
        ):
            raise StartupPreflightError(
                "media_storage_configuration_invalid"
            )
        relative = os.path.relpath(normalized_target, normalized_root)
    except ValueError:
        raise StartupPreflightError(
            "media_storage_configuration_invalid"
        ) from None
    components = tuple(relative.split(os.sep))
    if any(component in ("", ".", "..") for component in components):
        raise StartupPreflightError(
            "media_storage_configuration_invalid"
        )
    return normalized_root, components


def _lock_receipt(file_stat):
    if (
        not stat.S_ISREG(file_stat.st_mode)
        or file_stat.st_nlink != 1
        or stat.S_IMODE(file_stat.st_mode) != 0o600
    ):
        raise StartupPreflightError("unsafe_storage_ownership")
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_uid,
        file_stat.st_gid,
        stat.S_IMODE(file_stat.st_mode),
        file_stat.st_nlink,
    )


def _verify_startup_lock_receipt(root_directory, lock_descriptor):
    try:
        root_directory.verify_current()
        descriptor_stat = os.fstat(lock_descriptor)
        named_stat = os.stat(
            ".svrx-pinry-startup.lock",
            dir_fd=root_directory.descriptor,
            follow_symlinks=False,
        )
    except Exception as error:
        raise StartupPreflightError("unsafe_storage_ownership") from error
    descriptor_receipt = _lock_receipt(descriptor_stat)
    named_receipt = _lock_receipt(named_stat)
    if descriptor_receipt != named_receipt:
        raise StartupPreflightError("unsafe_storage_ownership")
    return descriptor_receipt


def _open_child_directory(parent_descriptor, name, named_stat):
    descriptor = os.open(
        name,
        _directory_open_flags(),
        dir_fd=parent_descriptor,
    )
    try:
        opened_stat = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(opened_stat.st_mode)
            or _stat_identity(opened_stat) != _stat_identity(named_stat)
        ):
            raise StartupPreflightError("unsafe_storage_ownership")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _chown_managed_path(
    root_directory,
    components,
    service_uid,
    service_gid,
):
    descriptor = os.dup(root_directory.descriptor)
    try:
        for component in components[:-1]:
            try:
                named_stat = os.stat(
                    component,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return
            if not stat.S_ISDIR(named_stat.st_mode):
                raise StartupPreflightError("unsafe_storage_ownership")
            child = _open_child_directory(
                descriptor,
                component,
                named_stat,
            )
            os.close(descriptor)
            descriptor = child
        leaf = components[-1]
        try:
            leaf_stat = os.stat(
                leaf,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return
        if stat.S_ISDIR(leaf_stat.st_mode):
            child = _open_child_directory(descriptor, leaf, leaf_stat)
            try:
                _chown_tree_descriptor(
                    child,
                    service_uid,
                    service_gid,
                )
            finally:
                os.close(child)
        elif stat.S_ISREG(leaf_stat.st_mode) and leaf_stat.st_nlink == 1:
            child = os.open(
                leaf,
                _file_open_flags(),
                dir_fd=descriptor,
            )
            try:
                opened_stat = os.fstat(child)
                if (
                    not stat.S_ISREG(opened_stat.st_mode)
                    or opened_stat.st_nlink != 1
                    or _stat_identity(opened_stat)
                    != _stat_identity(leaf_stat)
                ):
                    raise StartupPreflightError(
                        "unsafe_storage_ownership"
                    )
                os.fchown(child, service_uid, service_gid)
                os.fsync(child)
            finally:
                os.close(child)
        else:
            raise StartupPreflightError("unsafe_storage_ownership")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _chown_tree_descriptor(descriptor, service_uid, service_gid):
    for name in sorted(os.listdir(descriptor)):
        named_stat = os.stat(
            name,
            dir_fd=descriptor,
            follow_symlinks=False,
        )
        if stat.S_ISDIR(named_stat.st_mode):
            child = _open_child_directory(descriptor, name, named_stat)
            try:
                _chown_tree_descriptor(child, service_uid, service_gid)
            finally:
                os.close(child)
            continue
        if not stat.S_ISREG(named_stat.st_mode) or named_stat.st_nlink != 1:
            raise StartupPreflightError("unsafe_storage_ownership")
        child = os.open(
            name,
            _file_open_flags(),
            dir_fd=descriptor,
        )
        try:
            opened_stat = os.fstat(child)
            if (
                not stat.S_ISREG(opened_stat.st_mode)
                or opened_stat.st_nlink != 1
                or _stat_identity(opened_stat)
                != _stat_identity(named_stat)
            ):
                raise StartupPreflightError("unsafe_storage_ownership")
            os.fchown(child, service_uid, service_gid)
            os.fsync(child)
        finally:
            os.close(child)
    os.fchown(descriptor, service_uid, service_gid)
    os.fsync(descriptor)


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


def _has_pinry_direct_md5_directory(media_directory):
    if media_directory is None:
        return False
    found = False
    for root_name in PINRY_DIRECT_MD5_ROOTS:
        try:
            root_stat = os.stat(
                root_name,
                dir_fd=media_directory.descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(root_stat.st_mode):
            raise StartupPreflightError("legacy_evidence_invalid")
        found = True
    media_directory.verify_current()
    return found


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


def _read_media_rows(connection, tables):
    image_rows = ()
    thumbnail_rows = ()
    if "django_images_image" in tables:
        image_rows = _read_table_rows(
            connection,
            "django_images_image",
            ("id", "image"),
            ("asset_uuid", "original_filename", "width", "height"),
        )
    if "django_images_thumbnail" in tables:
        thumbnail_rows = _read_table_rows(
            connection,
            "django_images_thumbnail",
            ("id", "image"),
            ("original_id", "size", "width", "height"),
        )
    return image_rows, thumbnail_rows


def _read_table_rows(connection, table_name, required, optional):
    columns = {
        row[1]
        for row in connection.execute(
            "PRAGMA table_info({})".format(table_name)
        )
    }
    if not set(required).issubset(columns):
        raise StartupPreflightError("legacy_evidence_invalid")
    selected = tuple(required) + tuple(
        name for name in optional if name in columns
    )
    rows = connection.execute(
        "SELECT {} FROM {}".format(", ".join(selected), table_name)
    )
    return tuple(dict(zip(selected, row)) for row in rows)


def _table_has_rows(connection, tables, table_name):
    if table_name not in tables:
        return False
    return connection.execute(
        "SELECT 1 FROM {} LIMIT 1".format(table_name)
    ).fetchone() is not None


def _missing_unreferenced_images(
    connection,
    tables,
    image_rows,
    thumbnail_rows,
    media_directory,
):
    if "core_pin" not in tables or media_directory is None:
        return ()
    pin_image_ids = _read_reference_image_ids(
        connection,
        tables,
        "core_pin",
        required=True,
    )
    asset_image_ids = _read_reference_image_ids(
        connection,
        tables,
        "core_mediaasset",
        required=False,
    )
    referenced_ids = pin_image_ids | asset_image_ids
    image_ids = tuple(row.get("id") for row in image_rows)
    if (
        any(type(image_id) is not int or image_id <= 0 for image_id in image_ids)
        or len(image_ids) != len(set(image_ids))
    ):
        raise StartupPreflightError("legacy_media_rows_invalid")

    thumbnails_by_original = {}
    for row in thumbnail_rows:
        original_id = row.get("original_id")
        if type(original_id) is int:
            thumbnails_by_original.setdefault(original_id, []).append(row)
    path_counts = {}
    for row in tuple(image_rows) + tuple(thumbnail_rows):
        path = row.get("image")
        path_counts[path] = path_counts.get(path, 0) + 1

    candidates = []
    for row in image_rows:
        image_id = row["id"]
        if image_id in referenced_ids:
            continue
        related = tuple(sorted(
            thumbnails_by_original.get(image_id, ()),
            key=lambda item: item["id"],
        ))
        if any(
            type(item.get("id")) is not int or item["id"] <= 0
            for item in related
        ):
            raise StartupPreflightError("legacy_media_rows_invalid")
        paths = (row["image"],) + tuple(
            item["image"] for item in related
        )
        if any(path_counts.get(path) != 1 for path in paths):
            continue
        checked_paths = _missing_candidate_checked_paths(row, related)
        if checked_paths is None:
            continue
        if any(
            path_counts.get(path, 0) > (1 if path in paths else 0)
            for path in checked_paths
        ):
            continue
        if not all(
            _verified_media_file_missing(media_directory, path)
            for path in checked_paths
        ):
            continue
        candidates.append(MissingUnreferencedImage(
            image_id=image_id,
            image_path=row["image"],
            thumbnail_rows=tuple(
                (item["id"], item["image"])
                for item in related
            ),
            image_signature=_row_signature(row),
            thumbnail_signatures=tuple(
                _row_signature(item) for item in related
            ),
        ))
    candidates = tuple(sorted(
        candidates,
        key=lambda candidate: candidate.image_id,
    ))
    checked_path_owners = {}
    rows_by_id = {row["id"]: row for row in image_rows}
    related_by_id = {
        image_id: tuple(sorted(rows, key=lambda item: item["id"]))
        for image_id, rows in thumbnails_by_original.items()
    }
    for candidate in candidates:
        for path in _missing_candidate_checked_paths(
            rows_by_id[candidate.image_id],
            related_by_id.get(candidate.image_id, ()),
        ):
            checked_path_owners.setdefault(path, set()).add(
                candidate.image_id
            )
    return tuple(
        candidate
        for candidate in candidates
        if all(
            len(checked_path_owners[path]) == 1
            for path in _missing_candidate_checked_paths(
                rows_by_id[candidate.image_id],
                related_by_id.get(candidate.image_id, ()),
            )
        )
    )


def _validate_cleanup_candidates(
    connection,
    tables,
    image_rows,
    thumbnail_rows,
    media_directory,
    candidates,
):
    pin_image_ids = _read_reference_image_ids(
        connection,
        tables,
        "core_pin",
        required=True,
    )
    asset_image_ids = _read_reference_image_ids(
        connection,
        tables,
        "core_mediaasset",
        required=False,
    )
    referenced_ids = pin_image_ids | asset_image_ids
    rows_by_id = {row.get("id"): row for row in image_rows}
    thumbnails_by_original = {}
    path_counts = {}
    for row in tuple(image_rows) + tuple(thumbnail_rows):
        path = row.get("image")
        path_counts[path] = path_counts.get(path, 0) + 1
    for row in thumbnail_rows:
        original_id = row.get("original_id")
        if type(original_id) is int:
            thumbnails_by_original.setdefault(original_id, []).append(row)

    checked_path_owners = {}
    checked_paths_by_id = {}
    for candidate in candidates:
        row = rows_by_id.get(candidate.image_id)
        if row is None or row.get("image") != candidate.image_path:
            raise StartupPreflightError("legacy_media_rows_invalid")
        if _row_signature(row) != candidate.image_signature:
            raise StartupPreflightError("legacy_media_rows_invalid")
        if candidate.image_id in referenced_ids:
            raise StartupPreflightError("legacy_media_rows_invalid")
        related = tuple(sorted(
            thumbnails_by_original.get(candidate.image_id, ()),
            key=lambda item: item.get("id"),
        ))
        signature = tuple(
            (item.get("id"), item.get("image"))
            for item in related
        )
        if signature != candidate.thumbnail_rows:
            raise StartupPreflightError("legacy_media_rows_invalid")
        if tuple(
            _row_signature(item) for item in related
        ) != candidate.thumbnail_signatures:
            raise StartupPreflightError("legacy_media_rows_invalid")
        paths = (candidate.image_path,) + tuple(
            path for _thumbnail_id, path in candidate.thumbnail_rows
        )
        if any(path_counts.get(path) != 1 for path in paths):
            raise StartupPreflightError("legacy_media_rows_invalid")
        checked_paths = _missing_candidate_checked_paths(row, related)
        if checked_paths is None:
            raise StartupPreflightError("legacy_media_rows_invalid")
        if any(
            path_counts.get(path, 0) > (1 if path in paths else 0)
            for path in checked_paths
        ):
            raise StartupPreflightError("legacy_media_rows_invalid")
        checked_paths_by_id[candidate.image_id] = checked_paths
        for path in checked_paths:
            checked_path_owners.setdefault(path, set()).add(
                candidate.image_id
            )

    if any(
        len(owners) != 1 for owners in checked_path_owners.values()
    ):
        raise StartupPreflightError("legacy_media_rows_invalid")
    for checked_paths in checked_paths_by_id.values():
        if not all(
            _verified_media_file_missing(media_directory, path)
            for path in checked_paths
        ):
            raise StartupPreflightError("legacy_media_files_invalid")


def _missing_candidate_checked_paths(image_row, thumbnail_rows):
    asset_uuid = _canonical_database_uuid(image_row.get("asset_uuid"))
    original_filename = image_row.get("original_filename")
    image_path = image_row.get("image")
    if (
        asset_uuid is None
        or type(original_filename) is not str
        or type(image_path) is not str
        or not image_path
    ):
        return None
    try:
        target_paths = [
            canonical_original_path(
                asset_uuid,
                original_filename,
                extension,
            )
            for extension in sorted(set(FORMAT_EXTENSIONS.values()))
        ]
        for row in thumbnail_rows:
            thumbnail_path = row.get("image")
            if (
                type(thumbnail_path) is not str
                or not thumbnail_path
            ):
                return None
        for size in sorted(_REQUIRED_IMAGE_SIZES):
            target_paths.extend(
                canonical_derivative_path(asset_uuid, size, extension)
                for extension in sorted(set(FORMAT_EXTENSIONS.values()))
            )
    except ValueError:
        return None
    source_paths = [image_path] + [
        row["image"] for row in thumbnail_rows
    ]
    return tuple(sorted(set(source_paths + target_paths)))


def _row_signature(row):
    return tuple(sorted(row.items()))


def _validate_image_foreign_keys(connection, tables):
    allowed = frozenset((
        ("core_mediaasset", "image_id", "id"),
        ("core_pin", "image_id", "id"),
        ("django_images_thumbnail", "original_id", "id"),
    ))
    references = set()
    for table_name in tables:
        if type(table_name) is not str or not table_name:
            raise StartupPreflightError("legacy_media_rows_invalid")
        escaped_name = table_name.replace('"', '""')
        for row in connection.execute(
            'PRAGMA foreign_key_list("{}")'.format(escaped_name)
        ):
            if len(row) < 5:
                raise StartupPreflightError("legacy_media_rows_invalid")
            target_table, source_column, target_column = row[2:5]
            if any(
                type(value) is not str or not value
                for value in (
                    target_table,
                    source_column,
                    target_column,
                )
            ):
                raise StartupPreflightError("legacy_media_rows_invalid")
            target_table = target_table.lower()
            if target_table == "django_images_thumbnail":
                raise StartupPreflightError("legacy_media_rows_invalid")
            if target_table == "django_images_image":
                references.add((
                    table_name.lower(),
                    source_column.lower(),
                    target_column.lower(),
                ))
    if not references.issubset(allowed):
        raise StartupPreflightError("legacy_media_rows_invalid")


def _validate_media_delete_triggers(connection):
    protected_tables = frozenset((
        "django_images_image",
        "django_images_thumbnail",
    ))
    for name, table_name in connection.execute(
        "SELECT name, tbl_name FROM sqlite_master WHERE type = 'trigger'"
    ):
        if (
            type(name) is not str
            or not name
            or type(table_name) is not str
            or not table_name
        ):
            raise StartupPreflightError("legacy_media_rows_invalid")
        if table_name.lower() in protected_tables:
            raise StartupPreflightError("legacy_media_rows_invalid")


def _read_reference_image_ids(
    connection,
    tables,
    table_name,
    required,
):
    if table_name not in tables:
        if required:
            raise StartupPreflightError("legacy_media_rows_invalid")
        return set()
    columns = {
        row[1]
        for row in connection.execute(
            "PRAGMA table_info({})".format(table_name)
        )
    }
    if "image_id" not in columns:
        raise StartupPreflightError("legacy_media_rows_invalid")
    values = tuple(
        row[0]
        for row in connection.execute(
            "SELECT image_id FROM {}".format(table_name)
        )
    )
    if any(type(value) is not int or value <= 0 for value in values):
        raise StartupPreflightError("legacy_media_rows_invalid")
    return set(values)


def _verified_media_file_missing(media_directory, relative_path):
    receipt = file_ops.open_verified_media_file(
        media_directory,
        relative_path,
        missing_ok=True,
    )
    if receipt is None:
        return True
    try:
        receipt.verify_current()
        return False
    finally:
        receipt.close()


def _classify_media_rows(image_rows, thumbnail_rows):  # noqa: C901
    has_md5_paths = False
    has_fixed_slot_paths = False
    has_named_canonical_paths = False
    copy_paths = set()
    thumbnails_by_original = {}
    for row in thumbnail_rows:
        path = row["image"]
        if type(path) is not str or not path:
            raise StartupPreflightError("legacy_evidence_invalid")
        if _MD5_PATH.fullmatch(path) or pinry_direct_md5_root(path):
            has_md5_paths = True
            copy_paths.add(path)
        elif _pinry_direct_md5_candidate(path):
            raise StartupPreflightError("legacy_evidence_invalid")
        elif _CANONICAL_DERIVATIVE_PATH.fullmatch(path):
            has_named_canonical_paths = True
        else:
            raise StartupPreflightError("legacy_evidence_invalid")
        original_id = row.get("original_id")
        if type(original_id) is int:
            thumbnails_by_original.setdefault(original_id, []).append(row)

    for row in image_rows:
        path = row["image"]
        if type(path) is not str or not path:
            raise StartupPreflightError("legacy_evidence_invalid")
        if _MD5_PATH.fullmatch(path) or pinry_direct_md5_root(path):
            has_md5_paths = True
            copy_paths.add(path)
            continue
        if _pinry_direct_md5_candidate(path):
            raise StartupPreflightError("legacy_evidence_invalid")
        match = _ORIGINAL_PATH.fullmatch(path)
        if match is None:
            raise StartupPreflightError("legacy_evidence_invalid")
        asset_uuid = match.group("asset_uuid")
        leaf = match.group("leaf")
        valid_original_leaf = is_valid_original_leaf(asset_uuid, leaf)
        extension = os.path.splitext(leaf)[1]
        derivative_rows = thumbnails_by_original.get(row["id"], ())
        if not _canonical_derivative_closure(
            asset_uuid, derivative_rows
        ):
            raise StartupPreflightError("legacy_evidence_invalid")

        metadata_available = (
            "asset_uuid" in row or "original_filename" in row
        )
        if metadata_available:
            database_asset_uuid = _canonical_database_uuid(
                row.get("asset_uuid")
            )
            original_filename = row.get("original_filename")
            if (
                database_asset_uuid != asset_uuid
                or type(original_filename) is not str
            ):
                raise StartupPreflightError("legacy_evidence_invalid")
            try:
                expected_path = canonical_original_path(
                    database_asset_uuid,
                    original_filename,
                    extension,
                )
            except ValueError as error:
                raise StartupPreflightError(
                    "legacy_evidence_invalid"
                ) from error
            if path == expected_path:
                has_named_canonical_paths = True
                continue
            expected_parent, expected_leaf = expected_path.rsplit("/", 1)
            legacy_storage_path = "{}/{}".format(
                expected_parent,
                get_valid_filename(expected_leaf),
            )
            if path == legacy_storage_path:
                has_fixed_slot_paths = True
                has_named_canonical_paths = True
                copy_paths.add(path)
                continue
            if (
                not valid_original_leaf
                or leaf != "original{}".format(extension)
            ):
                raise StartupPreflightError("legacy_evidence_invalid")
        elif not valid_original_leaf:
            raise StartupPreflightError("legacy_evidence_invalid")
        elif leaf != "original{}".format(extension):
            has_named_canonical_paths = True
            continue
        elif not derivative_rows:
            raise StartupPreflightError("legacy_evidence_invalid")

        has_fixed_slot_paths = True
        copy_paths.add(path)

    return {
        "has_md5_paths": has_md5_paths,
        "has_fixed_slot_paths": has_fixed_slot_paths,
        "has_named_canonical_paths": has_named_canonical_paths,
        "copy_paths": copy_paths,
    }


def _pinry_direct_md5_candidate(path):
    return path.split("/", 1)[0] in PINRY_DIRECT_MD5_ROOTS


def _canonical_database_uuid(value):
    if type(value) is not str:
        return None
    try:
        parsed = uuid.UUID(str(value))
    except (AttributeError, ValueError):
        return None
    if value.lower() not in (parsed.hex, str(parsed)):
        return None
    return str(parsed)


def _canonical_derivative_closure(asset_uuid, rows):
    seen_sizes = set()
    for row in rows:
        path = row["image"]
        size = row.get("size")
        if (
            type(path) is not str
            or type(size) is not str
            or size in seen_sizes
        ):
            return False
        extension = os.path.splitext(path)[1]
        try:
            expected_path = canonical_derivative_path(
                asset_uuid, size, extension
            )
        except ValueError:
            return False
        if path != expected_path:
            return False
        seen_sizes.add(size)
    return True


def _distinct_legacy_bytes(media_directory, copy_paths):
    if not copy_paths:
        return 0
    if media_directory is None:
        raise StartupPreflightError("legacy_evidence_invalid")
    total = 0
    for relative_path in sorted(copy_paths):
        receipt = file_ops.open_verified_media_file(
            media_directory,
            relative_path,
        )
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
    if "quality" in options:
        quality = options["quality"]
        if quality is not None and (
            type(quality) is not int or not 1 <= quality <= 95
        ):
            return False
    return True


def _run_service_probe(  # noqa: C901
    media_root, service_uid, service_gid
):
    if not callable(getattr(os, "fork", None)):
        return _PROBE_RESULT_LOCK
    try:
        read_descriptor, write_descriptor = os.pipe()
    except Exception:
        return _PROBE_RESULT_LOCK
    try:
        child_pid = os.fork()
    except BaseException:
        os.close(read_descriptor)
        os.close(write_descriptor)
        return _PROBE_RESULT_LOCK
    if child_pid == 0:  # pragma: no cover - assertions run in parent
        result_descriptor = None
        try:
            result_descriptor = _prepare_service_probe_child(
                read_descriptor,
                write_descriptor,
            )
            result = _service_probe_child(
                media_root,
                service_uid,
                service_gid,
            )
            try:
                os.write(result_descriptor, result.encode("ascii"))
            except BaseException:
                pass
        finally:
            if result_descriptor is not None:
                try:
                    os.close(result_descriptor)
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


def _prepare_service_probe_child(read_descriptor, write_descriptor):
    os.close(read_descriptor)
    if write_descriptor != _PROBE_RESULT_DESCRIPTOR:
        os.dup2(
            write_descriptor,
            _PROBE_RESULT_DESCRIPTOR,
            inheritable=False,
        )
        os.close(write_descriptor)
    else:
        os.set_inheritable(_PROBE_RESULT_DESCRIPTOR, False)
    _close_service_probe_descriptors()
    return _PROBE_RESULT_DESCRIPTOR


def _close_service_probe_descriptors():
    for descriptor_directory in ("/proc/self/fd", "/dev/fd"):
        try:
            entries = os.listdir(descriptor_directory)
        except OSError:
            continue
        for entry in entries:
            try:
                descriptor = int(entry)
            except (TypeError, ValueError):
                continue
            if descriptor <= _PROBE_RESULT_DESCRIPTOR:
                continue
            try:
                os.close(descriptor)
            except OSError as error:
                if error.errno != errno.EBADF:
                    raise
        return
    os.closerange(
        _PROBE_RESULT_DESCRIPTOR + 1,
        _service_probe_descriptor_limit(),
    )


def _service_probe_descriptor_limit():
    try:
        limit = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
    except (AttributeError, OSError, ValueError):
        limit = resource.RLIM_INFINITY
    if limit == resource.RLIM_INFINITY or limit <= _PROBE_RESULT_DESCRIPTOR:
        try:
            limit = os.sysconf("SC_OPEN_MAX")
        except (AttributeError, OSError, ValueError):
            limit = 0
        limit = max(limit, _DESCRIPTOR_FALLBACK_LIMIT)
    return max(int(limit), _PROBE_RESULT_DESCRIPTOR + 1)


def _service_probe_child(media_root, service_uid, service_gid):
    try:
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
    probe_created = False
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
        probe_created = True
        expected_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(expected_stat.st_mode)
            or expected_stat.st_nlink != 1
            or expected_stat.st_uid != os.geteuid()
            or expected_stat.st_gid != os.getegid()
        ):
            raise OSError("invalid probe identity")
        os.fchmod(descriptor, 0o600)
        current_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(current_stat.st_mode)
            or current_stat.st_nlink != 1
            or stat.S_IMODE(current_stat.st_mode) != 0o600
            or current_stat.st_uid != os.geteuid()
            or current_stat.st_gid != os.getegid()
            or (current_stat.st_dev, current_stat.st_ino)
            != (expected_stat.st_dev, expected_stat.st_ino)
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
        probe_created = False
        os.fsync(root_directory.descriptor)
        expected_stat = None
    finally:
        if probe_created and expected_stat is None and descriptor is not None:
            try:
                expected_stat = os.fstat(descriptor)
            except BaseException:
                pass
        if descriptor is not None:
            try:
                os.close(descriptor)
            except BaseException:
                pass
        if probe_created and expected_stat is not None:
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
