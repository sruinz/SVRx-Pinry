from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
import re
import stat
import uuid
import warnings

from django.conf import settings
from django.core.management import CommandError
from django.db import transaction
from PIL import Image as PILImage

from django_images.file_ops import (
    MediaPathError,
    OwnedStagingFile,
    create_owned_staging_file,
    open_or_create_media_directory_from,
    open_verified_media_file,
    open_verified_media_root,
    sha256_file_descriptor,
)
from django_images.models import Image, Thumbnail
from django_images.paths import (
    DERIVATIVE_NAMES,
    FORMAT_EXTENSIONS,
    canonical_derivative_path,
    canonical_original_path,
)


AUTO_V2_TARGET_SIGNATURE = "auto-v2"
AUTO_V2_MANIFEST_FILENAME = "media-migration.jsonl"
_RUN_ID_PATTERN = re.compile(
    r"^[0-9]{8}T[0-9]{6}Z-"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12})$"
)
_STAGING_NAME_PATTERN = re.compile(
    r"^auto-v2-"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12})\.part\Z"
)


def _command_error(code, cause=None):
    error = CommandError(code)
    if cause is not None:
        error.__cause__ = cause
    return error


def _valid_run_id(run_id):
    if not isinstance(run_id, str):
        return False
    match = _RUN_ID_PATTERN.match(run_id)
    if match is None:
        return False
    try:
        datetime.strptime(run_id[:16], "%Y%m%dT%H%M%SZ")
        return str(uuid.UUID(match.group(1))) == match.group(1)
    except (AttributeError, TypeError, ValueError):
        return False


def _valid_staging_name(name):
    if not isinstance(name, str):
        return False
    match = _STAGING_NAME_PATTERN.match(name)
    if match is None:
        return False
    try:
        return str(uuid.UUID(match.group(1))) == match.group(1)
    except (AttributeError, TypeError, ValueError):
        return False


def _json_line(event):
    return (
        json.dumps(
            event,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


@dataclass(frozen=True)
class AutoV2MigrationFile(object):
    kind: str
    old_path: str
    new_path: str
    operation: str
    size: int
    sha256: str
    image_format: str
    width: int
    height: int
    source_device: int
    source_inode: int
    thumbnail_id: int = None
    derivative_size: str = None

    @property
    def kind_key(self):
        if self.thumbnail_id is None:
            return "original"
        return "thumbnail:{}".format(self.thumbnail_id)

    def as_dict(self):
        value = {
            "kind": self.kind,
            "old_path": self.old_path,
            "new_path": self.new_path,
            "operation": self.operation,
            "size": self.size,
            "sha256": self.sha256,
            "image_format": self.image_format,
            "width": self.width,
            "height": self.height,
            "source_device": self.source_device,
            "source_inode": self.source_inode,
        }
        if self.thumbnail_id is not None:
            value["thumbnail_id"] = self.thumbnail_id
            value["derivative_size"] = self.derivative_size
        return value

    @classmethod
    def from_dict(cls, value):
        required = {
            "kind",
            "old_path",
            "new_path",
            "operation",
            "size",
            "sha256",
            "image_format",
            "width",
            "height",
            "source_device",
            "source_inode",
        }
        if not isinstance(value, dict) or not required.issubset(value):
            raise _command_error("invalid_auto_v2_manifest")
        return cls(
            kind=value["kind"],
            old_path=value["old_path"],
            new_path=value["new_path"],
            operation=value["operation"],
            size=value["size"],
            sha256=value["sha256"],
            image_format=value["image_format"],
            width=value["width"],
            height=value["height"],
            source_device=value["source_device"],
            source_inode=value["source_inode"],
            thumbnail_id=value.get("thumbnail_id"),
            derivative_size=value.get("derivative_size"),
        )


@dataclass(frozen=True)
class AutoV2MigrationPlan(object):
    image_id: int
    asset_uuid: str
    original_filename: str
    image_width: int
    image_height: int
    generation: str
    files: tuple
    thumbnail_rows: tuple
    copy_required_bytes: int = 0
    reusable_destination_identities: tuple = ()

    @property
    def old_original(self):
        return self.files[0].old_path

    @property
    def new_original(self):
        return self.files[0].new_path

    @property
    def already_current(self):
        return self.generation == "named_canonical"

    @property
    def backfill_eligible(self):
        return {
            row[1] for row in self.thumbnail_rows
        } == set(DERIVATIVE_NAMES)

    @property
    def fixed_slot_archive_sources(self):
        if (
            self.generation == "fixed_slot"
            and self.files[0].old_path != self.files[0].new_path
        ):
            return (self.files[0].old_path,)
        return ()

    @classmethod
    def for_image(
        cls, image, root_directory, derivative_records=None
    ):
        if derivative_records is None:
            derivative_records = list(
                image.thumbnail_set.order_by("pk")
            )
        else:
            derivative_records = list(derivative_records)
        _validate_derivative_records(derivative_records)
        records = [("original", image, None)] + [
            ("derivative", record, record)
            for record in derivative_records
        ]
        inspected = []
        for kind, record, thumbnail in records:
            receipt = None
            try:
                receipt = open_verified_media_file(
                    root_directory, record.image.name
                )
                details = _inspect_receipt(receipt)
                database_width = record.width
                database_height = record.height
                if (
                    database_width != details[3]
                    or database_height != details[4]
                ):
                    raise _command_error("invalid_legacy_media")
                inspected.append(
                    (kind, record, thumbnail, receipt.file_stat, details)
                )
            except (MediaPathError, OSError) as error:
                raise _command_error("unsafe_media_file", error)
            except (PILImage.UnidentifiedImageError, Warning) as error:
                raise _command_error("invalid_legacy_media", error)
            finally:
                if receipt is not None:
                    receipt.close()

        files = []
        reusable_destination_identities = []
        for kind, record, thumbnail, source_stat, details in inspected:
            image_format, extension, digest, width, height, file_size = details
            if kind == "original":
                try:
                    new_path = canonical_original_path(
                        image.asset_uuid,
                        image.original_filename,
                        extension,
                    )
                except ValueError as error:
                    raise _command_error("invalid_legacy_media", error)
            else:
                try:
                    new_path = canonical_derivative_path(
                        image.asset_uuid,
                        thumbnail.size,
                        extension,
                    )
                except ValueError as error:
                    raise _command_error("invalid_legacy_media", error)
            old_path = record.image.name
            operation = "verify" if old_path == new_path else "copy"
            if operation == "copy":
                destination = None
                try:
                    destination = open_verified_media_file(
                        root_directory, new_path, missing_ok=True
                    )
                    if destination is not None:
                        _verify_receipt_details(
                            destination,
                            file_size,
                            digest,
                            image_format,
                            width,
                            height,
                            "destination_collision",
                        )
                        reusable_destination_identities.append(
                            (
                                new_path,
                                destination.file_stat.st_dev,
                                destination.file_stat.st_ino,
                            )
                        )
                except (MediaPathError, OSError) as error:
                    raise _command_error("destination_collision", error)
                finally:
                    if destination is not None:
                        destination.close()
            files.append(
                AutoV2MigrationFile(
                    kind=kind,
                    old_path=old_path,
                    new_path=new_path,
                    operation=operation,
                    size=file_size,
                    sha256=digest,
                    image_format=image_format,
                    width=width,
                    height=height,
                    source_device=source_stat.st_dev,
                    source_inode=source_stat.st_ino,
                    thumbnail_id=thumbnail.pk if thumbnail else None,
                    derivative_size=thumbnail.size if thumbnail else None,
                )
            )

        generation = _classify_generation_for_uuid(
            str(image.asset_uuid), files
        )
        copy_required_bytes = sum(
            file_plan.size
            for file_plan in files
            if file_plan.operation == "copy"
            and file_plan.new_path
            not in {
                identity[0]
                for identity in reusable_destination_identities
            }
        )
        thumbnail_rows = tuple(
            (
                record.pk,
                record.size,
                record.image.name,
                record.width,
                record.height,
            )
            for record in derivative_records
        )
        return cls(
            image_id=image.pk,
            asset_uuid=str(image.asset_uuid),
            original_filename=image.original_filename,
            image_width=image.width,
            image_height=image.height,
            generation=generation,
            files=tuple(files),
            thumbnail_rows=thumbnail_rows,
            copy_required_bytes=copy_required_bytes,
            reusable_destination_identities=tuple(
                reusable_destination_identities
            ),
        )

    def as_dict(self):
        return {
            "image_id": self.image_id,
            "asset_uuid": self.asset_uuid,
            "original_filename": self.original_filename,
            "image_width": self.image_width,
            "image_height": self.image_height,
            "generation": self.generation,
            "files": [file_plan.as_dict() for file_plan in self.files],
            "thumbnail_rows": [list(row) for row in self.thumbnail_rows],
            "copy_required_bytes": self.copy_required_bytes,
            "reusable_destination_identities": [
                list(identity)
                for identity in self.reusable_destination_identities
            ],
        }

    @classmethod
    def from_dict(cls, value):
        required = {
            "image_id",
            "asset_uuid",
            "original_filename",
            "image_width",
            "image_height",
            "generation",
            "files",
            "thumbnail_rows",
            "copy_required_bytes",
            "reusable_destination_identities",
        }
        if not isinstance(value, dict) or not required.issubset(value):
            raise _command_error("invalid_auto_v2_manifest")
        files = tuple(
            AutoV2MigrationFile.from_dict(file_info)
            for file_info in value["files"]
        )
        plan = cls(
            image_id=value["image_id"],
            asset_uuid=value["asset_uuid"],
            original_filename=value["original_filename"],
            image_width=value["image_width"],
            image_height=value["image_height"],
            generation=value["generation"],
            files=files,
            thumbnail_rows=tuple(
                tuple(row) for row in value["thumbnail_rows"]
            ),
            copy_required_bytes=value["copy_required_bytes"],
            reusable_destination_identities=tuple(
                tuple(identity)
                for identity in value["reusable_destination_identities"]
            ),
        )
        _validate_plan(plan)
        return plan


def _validate_derivative_records(records):
    seen = set()
    for record in records:
        if record.size not in DERIVATIVE_NAMES:
            raise _command_error("unsupported_legacy_derivative_size")
        if record.size in seen:
            raise _command_error("duplicate_derivative_size")
        seen.add(record.size)


def _inspect_receipt(receipt):
    receipt.verify_current()
    digest = sha256_file_descriptor(receipt.descriptor)
    with warnings.catch_warnings():
        warnings.simplefilter("error", PILImage.DecompressionBombWarning)
        with os.fdopen(os.dup(receipt.descriptor), "rb") as source:
            source.seek(0)
            with PILImage.open(source) as image:
                image.verify()
        with os.fdopen(os.dup(receipt.descriptor), "rb") as source:
            source.seek(0)
            with PILImage.open(source) as image:
                image.load()
                image_format = image.format
                width, height = image.size
    try:
        extension = FORMAT_EXTENSIONS[image_format]
    except KeyError as error:
        raise _command_error("invalid_legacy_media", error)
    receipt.verify_current()
    return (
        image_format,
        extension,
        digest,
        width,
        height,
        receipt.file_stat.st_size,
    )


def _verify_receipt_details(
    receipt,
    expected_size,
    expected_sha256,
    expected_format,
    expected_width,
    expected_height,
    error_code,
    expected_identity=None,
):
    receipt.verify_current()
    details = _inspect_receipt(receipt)
    current_identity = (
        receipt.file_stat.st_dev,
        receipt.file_stat.st_ino,
    )
    if (
        details[5] != expected_size
        or details[2] != expected_sha256
        or details[0] != expected_format
        or details[3] != expected_width
        or details[4] != expected_height
        or (
            expected_identity is not None
            and current_identity != expected_identity
        )
    ):
        raise _command_error(error_code)
    return True


def _classify_generation_for_uuid(asset_uuid, files):
    if all(file_plan.old_path == file_plan.new_path for file_plan in files):
        return "named_canonical"
    original = files[0]
    derivative_files = files[1:]
    fixed_path = "originals/{}/original{}".format(
        asset_uuid,
        FORMAT_EXTENSIONS[original.image_format],
    )
    if (
        original.old_path == fixed_path
        and original.old_path != original.new_path
        and all(
            file_plan.old_path == file_plan.new_path
            for file_plan in derivative_files
        )
    ):
        return "fixed_slot"
    if (
        original.old_path.startswith("image/original/by-md5/")
        and all(
            file_plan.old_path.startswith("image/thumbnail/by-md5/")
            for file_plan in derivative_files
        )
        and all(
            file_plan.old_path != file_plan.new_path
            for file_plan in files
        )
    ):
        return "md5_legacy"
    raise _command_error("mixed_media_state")


def _validate_plan(plan):
    invalid = (
        type(plan.image_id) is not int
        or plan.image_id <= 0
        or not isinstance(plan.asset_uuid, str)
        or not isinstance(plan.original_filename, str)
        or plan.generation
        not in ("md5_legacy", "fixed_slot", "named_canonical")
        or not plan.files
        or plan.files[0].kind != "original"
        or plan.files[0].thumbnail_id is not None
        or any(
            file_plan.operation not in ("copy", "verify")
            or type(file_plan.size) is not int
            or file_plan.size < 0
            or not isinstance(file_plan.sha256, str)
            or len(file_plan.sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in file_plan.sha256
            )
            or type(file_plan.source_device) is not int
            or type(file_plan.source_inode) is not int
            or file_plan.source_inode <= 0
            for file_plan in plan.files
        )
    )
    try:
        canonical_uuid = str(uuid.UUID(plan.asset_uuid)) == plan.asset_uuid
    except (AttributeError, TypeError, ValueError):
        canonical_uuid = False
    if invalid or not canonical_uuid:
        raise _command_error("invalid_auto_v2_manifest")
    original = plan.files[0]
    if (
        original.image_format not in FORMAT_EXTENSIONS
        or type(original.width) is not int
        or type(original.height) is not int
        or original.width <= 0
        or original.height <= 0
        or original.width != plan.image_width
        or original.height != plan.image_height
    ):
        raise _command_error("invalid_auto_v2_manifest")
    try:
        expected_original = canonical_original_path(
            plan.asset_uuid,
            plan.original_filename,
            FORMAT_EXTENSIONS[original.image_format],
        )
    except (KeyError, ValueError) as error:
        raise _command_error("invalid_auto_v2_manifest", error)
    if original.new_path != expected_original:
        raise _command_error("manifest_plan_mismatch")

    derivative_files = plan.files[1:]
    derivative_ids = set()
    derivative_sizes = set()
    for file_plan in plan.files:
        if (
            not _safe_relative_path(file_plan.old_path)
            or not _safe_relative_path(file_plan.new_path)
            or file_plan.operation
            != (
                "verify"
                if file_plan.old_path == file_plan.new_path
                else "copy"
            )
            or file_plan.image_format not in FORMAT_EXTENSIONS
            or type(file_plan.width) is not int
            or type(file_plan.height) is not int
            or file_plan.width <= 0
            or file_plan.height <= 0
        ):
            raise _command_error("invalid_auto_v2_manifest")
    for file_plan in derivative_files:
        if (
            file_plan.kind != "derivative"
            or type(file_plan.thumbnail_id) is not int
            or file_plan.thumbnail_id <= 0
            or file_plan.derivative_size not in DERIVATIVE_NAMES
            or file_plan.thumbnail_id in derivative_ids
            or file_plan.derivative_size in derivative_sizes
        ):
            raise _command_error("invalid_auto_v2_manifest")
        derivative_ids.add(file_plan.thumbnail_id)
        derivative_sizes.add(file_plan.derivative_size)
        expected_path = canonical_derivative_path(
            plan.asset_uuid,
            file_plan.derivative_size,
            FORMAT_EXTENSIONS[file_plan.image_format],
        )
        if file_plan.new_path != expected_path:
            raise _command_error("manifest_plan_mismatch")
    expected_rows = tuple(
        (
            file_plan.thumbnail_id,
            file_plan.derivative_size,
            file_plan.old_path,
            file_plan.width,
            file_plan.height,
        )
        for file_plan in derivative_files
    )
    if plan.thumbnail_rows != expected_rows:
        raise _command_error("manifest_plan_mismatch")
    try:
        generation = _classify_generation_for_uuid(
            plan.asset_uuid, plan.files
        )
    except CommandError as error:
        raise _command_error("manifest_plan_mismatch", error)
    reusable_paths = set()
    copy_paths = {
        file_plan.new_path
        for file_plan in plan.files
        if file_plan.operation == "copy"
    }
    for identity in plan.reusable_destination_identities:
        if (
            type(identity) is not tuple
            or len(identity) != 3
            or not _safe_relative_path(identity[0])
            or type(identity[1]) is not int
            or type(identity[2]) is not int
            or identity[1] < 0
            or identity[2] <= 0
            or identity[0] not in copy_paths
            or identity[0] in reusable_paths
        ):
            raise _command_error("invalid_auto_v2_manifest")
        reusable_paths.add(identity[0])
    expected_copy_bytes = sum(
        file_plan.size
        for file_plan in plan.files
        if file_plan.operation == "copy"
        and file_plan.new_path not in reusable_paths
    )
    if (
        generation != plan.generation
        or type(plan.copy_required_bytes) is not int
        or plan.copy_required_bytes != expected_copy_bytes
    ):
        raise _command_error("manifest_plan_mismatch")


def _safe_relative_path(value):
    if (
        not isinstance(value, str)
        or not value
        or value.startswith("/")
        or "\\" in value
    ):
        return False
    return all(
        component not in ("", ".", "..")
        for component in value.split("/")
    )


@dataclass(frozen=True)
class AutoV2PlanSummary(object):
    run_id: str
    plan_sha256: str
    manifest_sha256: str
    image_count: int
    md5_legacy: int
    fixed_slot: int
    named_canonical: int
    copy_required_bytes: int


class _AutoV2ManifestState(object):
    def __init__(self):
        self.events = []
        self.plans = []
        self.plan_by_image = {}
        self.latest_by_image = {}
        self.publish_intents_by_image = {}
        self.published_by_image = {}
        self.plan_complete = False
        self.plan_end_offset = None
        self.torn_tail = None
        self.torn_offset = None
        self.raw_bytes = b""


def _verify_contained_run_directory(data_directory, run_directory):
    try:
        data_directory.verify_current()
        run_directory.verify_current()
        data_names = tuple(data_directory.names)
        run_names = tuple(run_directory.names)
        if (
            len(run_names) <= len(data_names)
            or run_names[:len(data_names)] != data_names
        ):
            raise _command_error("unsafe_auto_v2_manifest")
        data_stat = os.fstat(data_directory.descriptor)
        contained_stat = os.fstat(
            run_directory.descriptors[len(data_names)]
        )
    except (IndexError, OSError, MediaPathError) as error:
        raise _command_error("unsafe_auto_v2_manifest", error)
    if (
        data_stat.st_dev,
        data_stat.st_ino,
    ) != (
        contained_stat.st_dev,
        contained_stat.st_ino,
    ):
        raise _command_error("unsafe_auto_v2_manifest")
    return True


class AutoV2ManifestLog(object):
    def __init__(
        self,
        data_directory,
        run_directory,
        filename,
        run_id,
        service_uid,
        service_gid,
        descriptor,
        file_stat,
    ):
        self.data_directory = data_directory
        self.run_directory = run_directory
        self.filename = filename
        self.run_id = run_id
        self.service_uid = service_uid
        self.service_gid = service_gid
        self.descriptor = descriptor
        self.file_stat = file_stat
        self.state = self._load_state()
        self._closed = False

    @classmethod
    def open(
        cls, run_directory, filename, run_id, service_uid, service_gid
    ):
        if not _valid_run_id(run_id):
            raise _command_error("invalid_auto_v2_run_id")
        if filename != AUTO_V2_MANIFEST_FILENAME:
            raise _command_error("unsafe_auto_v2_manifest")
        if (
            type(service_uid) is not int
            or type(service_gid) is not int
            or service_uid < 0
            or service_gid < 0
        ):
            raise _command_error("unsafe_auto_v2_manifest")
        if os.path.basename(run_directory) != run_id:
            raise _command_error("manifest_run_id_mismatch")
        data_directory = None
        directory = None
        descriptor = None
        try:
            data_directory = open_verified_media_root(
                settings.PINRY_DATA_ROOT
            )
            directory = open_verified_media_root(run_directory)
            _verify_contained_run_directory(data_directory, directory)
            directory_stat = os.fstat(directory.descriptor)
            if (
                directory_stat.st_uid != service_uid
                or directory_stat.st_gid != service_gid
                or stat.S_IMODE(directory_stat.st_mode) != 0o700
            ):
                raise _command_error("unsafe_auto_v2_manifest")
            existing_flags = os.O_RDWR | os.O_NOFOLLOW
            if hasattr(os, "O_CLOEXEC"):
                existing_flags |= os.O_CLOEXEC
            try:
                descriptor = os.open(
                    filename,
                    existing_flags,
                    dir_fd=directory.descriptor,
                )
            except FileNotFoundError:
                descriptor = os.open(
                    filename,
                    existing_flags | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=directory.descriptor,
                )
                os.fchmod(descriptor, 0o600)
                os.fchown(descriptor, service_uid, service_gid)
                os.fsync(descriptor)
                os.fsync(directory.descriptor)
            file_stat = os.fstat(descriptor)
            named_stat = os.stat(
                filename,
                dir_fd=directory.descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(file_stat.st_mode)
                or file_stat.st_nlink != 1
                or file_stat.st_uid != service_uid
                or file_stat.st_gid != service_gid
                or stat.S_IMODE(file_stat.st_mode) != 0o600
                or (file_stat.st_dev, file_stat.st_ino)
                != (named_stat.st_dev, named_stat.st_ino)
            ):
                raise _command_error("unsafe_auto_v2_manifest")
            opened = cls(
                data_directory,
                directory,
                filename,
                run_id,
                service_uid,
                service_gid,
                descriptor,
                file_stat,
            )
            descriptor = None
            data_directory = None
            directory = None
            return opened
        except BaseException as error:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except BaseException:
                    pass
            if directory is not None:
                try:
                    directory.close()
                except BaseException:
                    pass
            if data_directory is not None:
                try:
                    data_directory.close()
                except BaseException:
                    pass
            if isinstance(error, CommandError):
                raise
            if not isinstance(error, Exception):
                raise
            raise _command_error("unsafe_auto_v2_manifest", error)

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
        first_error = None
        descriptor = self.descriptor
        self.descriptor = None
        try:
            os.close(descriptor)
        except BaseException as error:
            first_error = error
        try:
            self.run_directory.close()
        except BaseException as error:
            if first_error is None:
                first_error = error
        try:
            self.data_directory.close()
        except BaseException as error:
            if first_error is None:
                first_error = error
        if first_error is not None:
            raise first_error

    def _verify_current(self):
        try:
            _verify_contained_run_directory(
                self.data_directory, self.run_directory
            )
            directory_stat = os.fstat(self.run_directory.descriptor)
            descriptor_stat = os.fstat(self.descriptor)
            named_stat = os.stat(
                self.filename,
                dir_fd=self.run_directory.descriptor,
                follow_symlinks=False,
            )
        except (OSError, MediaPathError) as error:
            raise _command_error("unsafe_auto_v2_manifest", error)
        if (
            not stat.S_ISDIR(directory_stat.st_mode)
            or directory_stat.st_uid != self.service_uid
            or directory_stat.st_gid != self.service_gid
            or stat.S_IMODE(directory_stat.st_mode) != 0o700
            or not stat.S_ISREG(descriptor_stat.st_mode)
            or descriptor_stat.st_nlink != 1
            or descriptor_stat.st_uid != self.service_uid
            or descriptor_stat.st_gid != self.service_gid
            or stat.S_IMODE(descriptor_stat.st_mode) != 0o600
            or (descriptor_stat.st_dev, descriptor_stat.st_ino)
            != (self.file_stat.st_dev, self.file_stat.st_ino)
            or (named_stat.st_dev, named_stat.st_ino)
            != (self.file_stat.st_dev, self.file_stat.st_ino)
        ):
            raise _command_error("unsafe_auto_v2_manifest")
        return True

    def _read_all(self):
        self._verify_current()
        chunks = []
        position = 0
        while True:
            chunk = os.pread(self.descriptor, 1024 * 1024, position)
            if not chunk:
                break
            chunks.append(chunk)
            position += len(chunk)
        self._verify_current()
        return b"".join(chunks)

    def _load_state(self):
        raw = self._read_all()
        state = _AutoV2ManifestState()
        state.raw_bytes = raw
        offset = 0
        lines = raw.splitlines(True)
        for line_number, line in enumerate(lines, 1):
            if not line.endswith(b"\n"):
                if line_number != len(lines):
                    raise _command_error("invalid_auto_v2_manifest")
                state.torn_tail = line
                state.torn_offset = offset
                break
            try:
                event = json.loads(line.decode("utf-8"))
            except (TypeError, ValueError, UnicodeDecodeError) as error:
                raise _command_error("invalid_auto_v2_manifest", error)
            _validate_manifest_event(event, self.run_id)
            event_name = event["event"]
            if event_name == "planned":
                if state.plan_complete:
                    raise _command_error("invalid_auto_v2_manifest")
                plan = AutoV2MigrationPlan.from_dict(event.get("plan"))
                if plan.image_id in state.plan_by_image:
                    raise _command_error("manifest_plan_mismatch")
                state.plans.append(plan)
                state.plan_by_image[plan.image_id] = plan
            elif event_name == "plan_complete":
                if state.plan_complete:
                    raise _command_error("invalid_auto_v2_manifest")
                _validate_plan_marker(event, state.plans)
                state.plan_complete = True
                state.plan_end_offset = offset + len(line)
            else:
                if not state.plan_complete:
                    raise _command_error("invalid_auto_v2_manifest")
                image_id = event.get("image_id")
                if image_id not in state.plan_by_image:
                    raise _command_error("manifest_plan_mismatch")
                if event.get("plan_sha256") != hashlib.sha256(
                    raw[:state.plan_end_offset]
                ).hexdigest():
                    raise _command_error("manifest_plan_mismatch")
                previous = state.latest_by_image.get(image_id)
                if previous in ("committed", "recovered_commit", "already_current"):
                    raise _command_error("invalid_auto_v2_manifest")
                if event_name == "publish_intent":
                    plan = state.plan_by_image[image_id]
                    file_by_key = {
                        file_plan.kind_key: file_plan
                        for file_plan in plan.files
                    }
                    file_key = event.get("file_key")
                    identity = (
                        event.get("staging_device"),
                        event.get("staging_inode"),
                        event.get("destination_parent_device"),
                        event.get("destination_parent_inode"),
                        event.get("staging_name"),
                    )
                    intents = state.publish_intents_by_image.setdefault(
                        image_id, {}
                    )
                    if (
                        file_key not in file_by_key
                        or file_by_key[file_key].operation != "copy"
                        or file_key in intents
                        or any(
                            type(value) is not int for value in identity[:4]
                        )
                        or identity[0] < 0
                        or identity[1] <= 0
                        or identity[2] < 0
                        or identity[3] <= 0
                        or not _valid_staging_name(identity[4])
                    ):
                        raise _command_error("invalid_auto_v2_manifest")
                    intents[file_key] = identity
                elif event_name == "published":
                    plan = state.plan_by_image[image_id]
                    file_by_key = {
                        file_plan.kind_key: file_plan
                        for file_plan in plan.files
                    }
                    file_key = event.get("file_key")
                    destination_device = event.get("destination_device")
                    destination_inode = event.get("destination_inode")
                    published = state.published_by_image.setdefault(
                        image_id, {}
                    )
                    if (
                        file_key not in file_by_key
                        or file_by_key[file_key].operation != "copy"
                        or file_key in published
                        or type(destination_device) is not int
                        or type(destination_inode) is not int
                        or destination_device < 0
                        or destination_inode <= 0
                    ):
                        raise _command_error("invalid_auto_v2_manifest")
                    intent = state.publish_intents_by_image.get(
                        image_id, {}
                    ).get(file_key)
                    if intent is not None and (
                        destination_device,
                        destination_inode,
                    ) != intent[:2]:
                        raise _command_error("manifest_plan_mismatch")
                    published[file_key] = (
                        destination_device,
                        destination_inode,
                    )
                state.latest_by_image[image_id] = event_name
            state.events.append(event)
            offset += len(line)
        return state

    def append(self, event):
        if self.state.torn_tail is not None:
            raise _command_error(
                "media_manifest_torn_tail_requires_execute"
            )
        self._ensure_content_current()
        event = dict(event)
        event.update(
            {
                "format_version": 2,
                "target_signature": AUTO_V2_TARGET_SIGNATURE,
                "run_id": self.run_id,
            }
        )
        line = _json_line(event)
        self._verify_current()
        os.lseek(self.descriptor, 0, os.SEEK_END)
        view = memoryview(line)
        while view:
            written = os.write(self.descriptor, view)
            if written <= 0:
                raise _command_error("unsafe_auto_v2_manifest")
            view = view[written:]
        os.fsync(self.descriptor)
        self._verify_current()
        self.state = self._load_state()

    def record_plan(self, plan):
        self.append({"event": "planned", "plan": plan.as_dict()})

    def record_plan_complete(self, plans):
        counts = _plan_counts(plans)
        self.append(
            {
                "event": "plan_complete",
                "image_count": len(plans),
                "md5_legacy": counts["md5_legacy"],
                "fixed_slot": counts["fixed_slot"],
                "named_canonical": counts["named_canonical"],
                "copy_required_bytes": _copy_required_bytes(plans),
            }
        )

    def record_result(self, event_name, image_id):
        if event_name not in (
            "committed",
            "recovered_commit",
            "already_current",
        ):
            raise _command_error("invalid_auto_v2_manifest")
        self.append(
            {
                "event": event_name,
                "image_id": image_id,
                "plan_sha256": self.summary().plan_sha256,
            }
        )

    def record_published(self, image_id, file_key, destination_identity):
        self.append(
            {
                "event": "published",
                "image_id": image_id,
                "file_key": file_key,
                "destination_device": destination_identity[0],
                "destination_inode": destination_identity[1],
                "plan_sha256": self.summary().plan_sha256,
            }
        )

    def record_publish_intent(
        self,
        image_id,
        file_key,
        staging_name,
        staging_identity,
        destination_parent_identity,
    ):
        self.append(
            {
                "event": "publish_intent",
                "image_id": image_id,
                "file_key": file_key,
                "staging_name": staging_name,
                "staging_device": staging_identity[0],
                "staging_inode": staging_identity[1],
                "destination_parent_device": (
                    destination_parent_identity[0]
                ),
                "destination_parent_inode": (
                    destination_parent_identity[1]
                ),
                "plan_sha256": self.summary().plan_sha256,
            }
        )

    def repair_torn_tail(self):
        if self.state.torn_tail is None:
            return None
        self._ensure_content_current()
        quarantine_name = "{}.torn-{}".format(
            self.filename, uuid.uuid4()
        )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        quarantine_descriptor = None
        try:
            quarantine_descriptor = os.open(
                quarantine_name,
                flags,
                0o600,
                dir_fd=self.run_directory.descriptor,
            )
            os.fchmod(quarantine_descriptor, 0o600)
            os.fchown(
                quarantine_descriptor,
                self.service_uid,
                self.service_gid,
            )
            view = memoryview(self.state.torn_tail)
            while view:
                written = os.write(quarantine_descriptor, view)
                if written <= 0:
                    raise _command_error("unsafe_auto_v2_manifest")
                view = view[written:]
            os.fsync(quarantine_descriptor)
        finally:
            if quarantine_descriptor is not None:
                os.close(quarantine_descriptor)
        self._ensure_content_current()
        os.ftruncate(self.descriptor, self.state.torn_offset)
        os.fsync(self.descriptor)
        os.fsync(self.run_directory.descriptor)
        self.state = self._load_state()
        return quarantine_name

    def summary(self):
        if self.state.torn_tail is not None:
            raise _command_error(
                "media_manifest_torn_tail_requires_execute"
            )
        if not self.state.plan_complete:
            raise _command_error("auto_v2_plan_incomplete")
        self._ensure_content_current()
        marker = next(
            event
            for event in self.state.events
            if event["event"] == "plan_complete"
        )
        return AutoV2PlanSummary(
            run_id=self.run_id,
            plan_sha256=hashlib.sha256(
                self.state.raw_bytes[:self.state.plan_end_offset]
            ).hexdigest(),
            manifest_sha256=hashlib.sha256(
                self.state.raw_bytes
            ).hexdigest(),
            image_count=marker["image_count"],
            md5_legacy=marker["md5_legacy"],
            fixed_slot=marker["fixed_slot"],
            named_canonical=marker["named_canonical"],
            copy_required_bytes=marker["copy_required_bytes"],
        )

    def _ensure_content_current(self):
        if self._read_all() != self.state.raw_bytes:
            raise _command_error("unsafe_auto_v2_manifest")
        return True


def _validate_manifest_event(event, run_id):
    if not isinstance(event, dict):
        raise _command_error("invalid_auto_v2_manifest")
    if (
        event.get("format_version") != 2
        or event.get("target_signature") != AUTO_V2_TARGET_SIGNATURE
    ):
        raise _command_error("manifest_plan_mismatch")
    if event.get("run_id") != run_id:
        raise _command_error("manifest_run_id_mismatch")
    if event.get("event") not in (
        "planned",
        "plan_complete",
        "publish_intent",
        "published",
        "committed",
        "recovered_commit",
        "already_current",
    ):
        raise _command_error("invalid_auto_v2_manifest")


def _validate_plan_marker(event, plans):
    counts = _plan_counts(plans)
    if (
        event.get("image_count") != len(plans)
        or event.get("md5_legacy") != counts["md5_legacy"]
        or event.get("fixed_slot") != counts["fixed_slot"]
        or event.get("named_canonical") != counts["named_canonical"]
        or event.get("copy_required_bytes")
        != _copy_required_bytes(plans)
    ):
        raise _command_error("manifest_plan_mismatch")


def _plan_counts(plans):
    return {
        generation: sum(
            1 for plan in plans if plan.generation == generation
        )
        for generation in (
            "md5_legacy",
            "fixed_slot",
            "named_canonical",
        )
    }


def _copy_required_bytes(plans):
    return sum(plan.copy_required_bytes for plan in plans)


def load_auto_v2_plan(
    run_directory, filename, run_id, service_uid, service_gid
):
    """완성되고 fsync된 auto-v2 계획만 읽어 요약을 반환한다."""
    with AutoV2ManifestLog.open(
        run_directory,
        filename,
        run_id,
        service_uid,
        service_gid,
    ) as manifest:
        return manifest.summary()


class AutoV2MediaMigrator(object):
    def __init__(
        self,
        run_directory,
        filename,
        run_id,
        service_uid,
        service_gid,
        batch_size=100,
        fault_injector=None,
    ):
        if type(batch_size) is not int or batch_size <= 0:
            raise _command_error("batch_size_must_be_positive")
        self.run_directory = run_directory
        self.filename = filename
        self.run_id = run_id
        self.service_uid = service_uid
        self.service_gid = service_gid
        self.batch_size = batch_size
        self.fault_injector = fault_injector

    def run(self, execute=False):
        with AutoV2ManifestLog.open(
            self.run_directory,
            self.filename,
            self.run_id,
            self.service_uid,
            self.service_gid,
        ) as manifest:
            if manifest.state.torn_tail is not None:
                if not execute:
                    raise _command_error(
                        "media_manifest_torn_tail_requires_execute"
                    )
                manifest.repair_torn_tail()
            if not manifest.state.events:
                plans = self._freeze_all_plans()
                for plan in plans:
                    manifest.record_plan(plan)
                manifest.record_plan_complete(plans)
                self._inject_fault("after_plan_complete")
            elif not manifest.state.plan_complete:
                raise _command_error("auto_v2_plan_incomplete")
            plans = list(manifest.state.plans)
            summary = manifest.summary()
            if not execute:
                return summary
            self._execute(manifest, plans, summary.plan_sha256)
            return manifest.summary()

    def _freeze_all_plans(self):
        root_directory = None
        try:
            root_directory = open_verified_media_root(settings.MEDIA_ROOT)
            plans = []
            for image in Image.objects.order_by("pk"):
                plans.append(
                    AutoV2MigrationPlan.for_image(image, root_directory)
                )
                self._inject_fault("after_plan_image")
                root_directory.verify_current()
            root_directory.verify_current()
            return plans
        except CommandError:
            raise
        except MediaPathError as error:
            raise _command_error("unsafe_media_directory", error)
        finally:
            if root_directory is not None:
                root_directory.close()

    def _execute(self, manifest, plans, plan_sha256):
        if manifest.summary().plan_sha256 != plan_sha256:
            raise _command_error("manifest_plan_mismatch")
        self._validate_image_plan_closure(plans)
        pending = []
        source_verifications = []
        destination_verifications = []
        already_current = []
        recovered = []
        for plan in plans:
            state = self._database_state(plan)
            latest = manifest.state.latest_by_image.get(plan.image_id)
            if plan.already_current:
                if state not in ("old", "new"):
                    raise _command_error(
                        "media_migration_database_changed"
                    )
                if latest not in (None, "already_current"):
                    raise _command_error("manifest_plan_mismatch")
                source_verifications.append(plan)
                if latest is None:
                    already_current.append(plan)
                continue
            if state == "new":
                if latest not in (
                    None,
                    "published",
                    "committed",
                    "recovered_commit",
                ):
                    raise _command_error("manifest_plan_mismatch")
                destination_verifications.append(plan)
                if latest not in ("committed", "recovered_commit"):
                    recovered.append(plan)
                continue
            if state != "old":
                raise _command_error("media_migration_database_changed")
            if latest not in (None, "publish_intent", "published"):
                raise _command_error("manifest_plan_mismatch")
            pending.append(plan)

        root_directory = None
        try:
            if plans:
                root_directory = open_verified_media_root(
                    settings.MEDIA_ROOT
                )
            for plan in source_verifications:
                self._verify_plan_sources_from(root_directory, plan)
            for plan in destination_verifications:
                self._verify_plan_destinations_from(
                    root_directory,
                    plan,
                    self._destination_identities(manifest, plan),
                )
            for plan in pending:
                reusable_destinations = {
                    identity[0]: (identity[1], identity[2])
                    for identity in plan.reusable_destination_identities
                }
                reusable_destinations.update(
                    {
                        file_key: identity
                        for file_key, identity in manifest.state
                        .published_by_image.get(plan.image_id, {}).items()
                    }
                )
                publish_intents = manifest.state.publish_intents_by_image.get(
                    plan.image_id, {}
                )
                for file_plan in plan.files:
                    self._prepare_file(
                        root_directory,
                        manifest,
                        plan.image_id,
                        file_plan,
                        reusable_destinations.get(
                            file_plan.new_path,
                            reusable_destinations.get(file_plan.kind_key),
                        ),
                        publish_intents.get(file_plan.kind_key),
                    )
            if root_directory is not None:
                root_directory.verify_current()
        except CommandError:
            raise
        except MediaPathError as error:
            raise _command_error("media_verification_failed", error)
        finally:
            if root_directory is not None:
                root_directory.close()

        for plan in already_current:
            manifest.record_result("already_current", plan.image_id)
        for plan in recovered:
            manifest.record_result("recovered_commit", plan.image_id)
        if pending:
            self._inject_fault("before_database_transaction")
            self._commit_batches(manifest, pending)

    def _prepare_file(
        self,
        root_directory,
        manifest,
        image_id,
        file_plan,
        reusable_destination_identity,
        publish_intent,
    ):
        source = None
        try:
            source = open_verified_media_file(
                root_directory, file_plan.old_path
            )
            _verify_receipt_details(
                source,
                file_plan.size,
                file_plan.sha256,
                file_plan.image_format,
                file_plan.width,
                file_plan.height,
                "media_verification_failed",
                expected_identity=(
                    file_plan.source_device,
                    file_plan.source_inode,
                ),
            )
            self._inject_fault("after_source_revalidation")
            if file_plan.operation == "verify":
                return
            if (
                publish_intent is not None
                and reusable_destination_identity is None
            ):
                self._resume_publish_intent(
                    root_directory,
                    manifest,
                    image_id,
                    file_plan,
                    publish_intent,
                )
                self._inject_fault("after_publish")
                return
            existing = open_verified_media_file(
                root_directory, file_plan.new_path, missing_ok=True
            )
            if existing is not None:
                try:
                    if reusable_destination_identity is None:
                        raise _command_error("destination_collision")
                    _verify_receipt_details(
                        existing,
                        file_plan.size,
                        file_plan.sha256,
                        file_plan.image_format,
                        file_plan.width,
                        file_plan.height,
                        "destination_collision",
                        expected_identity=reusable_destination_identity,
                    )
                    return
                finally:
                    existing.close()
            if reusable_destination_identity is not None:
                raise _command_error("destination_collision")
            destination_identity = self._copy_source(
                root_directory,
                manifest,
                image_id,
                source,
                file_plan,
            )
            self._inject_fault("after_publish_before_event")
            manifest.record_published(
                image_id, file_plan.kind_key, destination_identity
            )
            self._inject_fault("after_publish")
        except CommandError:
            raise
        except (MediaPathError, OSError) as error:
            raise _command_error("media_verification_failed", error)
        finally:
            if source is not None:
                source.close()

    def _verify_intent_staging_identity(
        self,
        staging_directory,
        staging_name,
        descriptor,
        publish_intent,
        expected_link_count,
    ):
        try:
            staging_directory.verify_current()
            named_stat = os.stat(
                staging_name,
                dir_fd=staging_directory.descriptor,
                follow_symlinks=False,
            )
            descriptor_stat = os.fstat(descriptor)
        except (MediaPathError, OSError) as error:
            raise _command_error("media_verification_failed", error)
        if (
            not stat.S_ISREG(named_stat.st_mode)
            or not stat.S_ISREG(descriptor_stat.st_mode)
            or (
                named_stat.st_dev,
                named_stat.st_ino,
            ) != publish_intent[:2]
            or (
                descriptor_stat.st_dev,
                descriptor_stat.st_ino,
            ) != publish_intent[:2]
            or named_stat.st_nlink != expected_link_count
            or descriptor_stat.st_nlink != expected_link_count
            or named_stat.st_uid != self.service_uid
            or stat.S_IMODE(named_stat.st_mode) != 0o600
        ):
            raise _command_error("media_verification_failed")
        return descriptor_stat

    def _open_intent_staging(
        self,
        staging_directory,
        publish_intent,
        file_plan,
        expected_link_count,
    ):
        flags = os.O_RDONLY | os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NONBLOCK"):
            flags |= os.O_NONBLOCK
        descriptor = None
        try:
            descriptor = os.open(
                publish_intent[4],
                flags,
                dir_fd=staging_directory.descriptor,
            )
            file_stat = self._verify_intent_staging_identity(
                staging_directory,
                publish_intent[4],
                descriptor,
                publish_intent,
                expected_link_count,
            )
            _verify_staging(descriptor, file_plan)
            self._verify_intent_staging_identity(
                staging_directory,
                publish_intent[4],
                descriptor,
                publish_intent,
                expected_link_count,
            )
            return OwnedStagingFile(
                staging_directory,
                publish_intent[4],
                descriptor,
                file_stat,
                publish_intent[4],
                owns_directory=False,
                idempotent_cleanup=True,
            )
        except BaseException:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except BaseException:
                    pass
            raise

    def _record_intent_destination(
        self,
        root_directory,
        manifest,
        image_id,
        file_plan,
        publish_intent,
    ):
        destination = open_verified_media_file(
            root_directory, file_plan.new_path
        )
        try:
            parent_stat = os.fstat(
                destination.parent_directory.descriptor
            )
            if (
                parent_stat.st_dev,
                parent_stat.st_ino,
            ) != publish_intent[2:4]:
                raise _command_error("destination_collision")
            _verify_receipt_details(
                destination,
                file_plan.size,
                file_plan.sha256,
                file_plan.image_format,
                file_plan.width,
                file_plan.height,
                "destination_collision",
                expected_identity=publish_intent[:2],
            )
        finally:
            destination.close()
        manifest.record_published(
            image_id,
            file_plan.kind_key,
            publish_intent[:2],
        )

    def _resume_publish_intent(
        self,
        root_directory,
        manifest,
        image_id,
        file_plan,
        publish_intent,
    ):
        staging_directory = None
        destination_directory = None
        staging_file = None
        try:
            staging_directory = open_or_create_media_directory_from(
                root_directory, ".staging"
            )
            relative_directory, destination_name = (
                file_plan.new_path.rsplit("/", 1)
            )
            destination_directory = open_or_create_media_directory_from(
                root_directory, relative_directory
            )
            staging_directory.verify_current()
            destination_directory.verify_current()
            destination_parent_stat = os.fstat(
                destination_directory.descriptor
            )
            if (
                destination_parent_stat.st_dev,
                destination_parent_stat.st_ino,
            ) != publish_intent[2:4]:
                raise _command_error("destination_collision")
            try:
                destination_stat = os.stat(
                    destination_name,
                    dir_fd=destination_directory.descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                destination_stat = None

            if destination_stat is None:
                staging_file = self._open_intent_staging(
                    staging_directory,
                    publish_intent,
                    file_plan,
                    1,
                )
                destination_directory.verify_current()
                try:
                    os.link(
                        publish_intent[4],
                        destination_name,
                        src_dir_fd=staging_directory.descriptor,
                        dst_dir_fd=destination_directory.descriptor,
                        follow_symlinks=False,
                    )
                except FileExistsError as error:
                    raise _command_error("destination_collision", error)
                destination_stat = os.stat(
                    destination_name,
                    dir_fd=destination_directory.descriptor,
                    follow_symlinks=False,
                )
            elif (
                not stat.S_ISREG(destination_stat.st_mode)
                or (
                    destination_stat.st_dev,
                    destination_stat.st_ino,
                ) != publish_intent[:2]
            ):
                raise _command_error("destination_collision")
            elif destination_stat.st_nlink == 1:
                try:
                    os.stat(
                        publish_intent[4],
                        dir_fd=staging_directory.descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    self._record_intent_destination(
                        root_directory,
                        manifest,
                        image_id,
                        file_plan,
                        publish_intent,
                    )
                    return
                raise _command_error("destination_collision")
            elif destination_stat.st_nlink == 2:
                staging_file = self._open_intent_staging(
                    staging_directory,
                    publish_intent,
                    file_plan,
                    2,
                )
            else:
                raise _command_error("destination_collision")

            self._verify_intent_staging_identity(
                staging_directory,
                publish_intent[4],
                staging_file.descriptor,
                publish_intent,
                2,
            )
            destination_stat = os.stat(
                destination_name,
                dir_fd=destination_directory.descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(destination_stat.st_mode)
                or (
                    destination_stat.st_dev,
                    destination_stat.st_ino,
                ) != publish_intent[:2]
                or destination_stat.st_nlink != 2
            ):
                raise _command_error("destination_collision")
            os.fsync(staging_file.descriptor)
            destination_directory.fsync_publish()
            self._inject_fault(
                "after_publish_fsync_before_staging_unlink"
            )
            destination_directory.verify_current()
            if not staging_file.cleanup():
                raise _command_error("media_verification_failed")
            staging_stat = os.fstat(staging_file.descriptor)
            destination_stat = os.stat(
                destination_name,
                dir_fd=destination_directory.descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(destination_stat.st_mode)
                or (
                    destination_stat.st_dev,
                    destination_stat.st_ino,
                ) != publish_intent[:2]
                or destination_stat.st_nlink != 1
                or (
                    staging_stat.st_dev,
                    staging_stat.st_ino,
                ) != publish_intent[:2]
                or staging_stat.st_nlink != 1
            ):
                raise _command_error("media_verification_failed")
            self._record_intent_destination(
                root_directory,
                manifest,
                image_id,
                file_plan,
                publish_intent,
            )
        finally:
            if staging_file is not None:
                staging_file.close()
            if destination_directory is not None:
                destination_directory.close()
            if staging_directory is not None:
                staging_directory.close()

    def _copy_source(
        self, root_directory, manifest, image_id, source, file_plan
    ):
        staging_directory = None
        destination_directory = None
        staging = None
        intent_recorded = False
        published = False
        try:
            staging_directory = open_or_create_media_directory_from(
                root_directory, ".staging"
            )
            staging = create_owned_staging_file(
                staging_directory,
                "auto-v2-{}.part".format(uuid.uuid4()),
            )
            with os.fdopen(os.dup(source.descriptor), "rb") as source_file:
                source_file.seek(0)
                with os.fdopen(os.dup(staging.descriptor), "wb") as target:
                    while True:
                        chunk = source_file.read(64 * 1024)
                        if not chunk:
                            break
                        target.write(chunk)
                    target.flush()
                    os.fsync(target.fileno())
            _verify_staging(staging.descriptor, file_plan)
            relative_directory, destination_name = file_plan.new_path.rsplit(
                "/", 1
            )
            destination_directory = open_or_create_media_directory_from(
                root_directory, relative_directory
            )
            existing = open_verified_media_file(
                root_directory, file_plan.new_path, missing_ok=True
            )
            if existing is not None:
                existing.close()
                raise _command_error("destination_collision")
            staging_stat = os.fstat(staging.descriptor)
            destination_parent_stat = os.fstat(
                destination_directory.descriptor
            )
            manifest.record_publish_intent(
                image_id,
                file_plan.kind_key,
                staging.name,
                (staging_stat.st_dev, staging_stat.st_ino),
                (
                    destination_parent_stat.st_dev,
                    destination_parent_stat.st_ino,
                ),
            )
            intent_recorded = True
            self._inject_fault("after_publish_intent")
            try:
                os.link(
                    staging.name,
                    destination_name,
                    src_dir_fd=staging_directory.descriptor,
                    dst_dir_fd=destination_directory.descriptor,
                    follow_symlinks=False,
                )
            except FileExistsError as error:
                raise _command_error("destination_collision", error)
            named_destination_stat = os.stat(
                destination_name,
                dir_fd=destination_directory.descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(named_destination_stat.st_mode)
                or (
                    named_destination_stat.st_dev,
                    named_destination_stat.st_ino,
                )
                != (staging_stat.st_dev, staging_stat.st_ino)
            ):
                raise _command_error("media_verification_failed")
            os.fsync(staging.descriptor)
            destination_directory.fsync_publish()
            self._inject_fault(
                "after_publish_fsync_before_staging_unlink"
            )
            if not staging.cleanup():
                raise _command_error("media_verification_failed")
            cleaned_staging_stat = os.fstat(staging.descriptor)
            if (
                cleaned_staging_stat.st_dev,
                cleaned_staging_stat.st_ino,
            ) != (staging_stat.st_dev, staging_stat.st_ino) or (
                cleaned_staging_stat.st_nlink != 1
            ):
                raise _command_error("media_verification_failed")
            published = True
            destination = open_verified_media_file(
                root_directory, file_plan.new_path
            )
            try:
                _verify_receipt_details(
                    destination,
                    file_plan.size,
                    file_plan.sha256,
                    file_plan.image_format,
                    file_plan.width,
                    file_plan.height,
                    "media_verification_failed",
                )
                destination_identity = (
                    destination.file_stat.st_dev,
                    destination.file_stat.st_ino,
                )
            finally:
                destination.close()
            return destination_identity
        finally:
            if staging is not None:
                if not published and not intent_recorded:
                    staging.cleanup()
                staging.close()
            if destination_directory is not None:
                destination_directory.close()
            if staging_directory is not None:
                staging_directory.close()

    def _verify_plan_sources_from(self, root, plan):
        for file_plan in plan.files:
            receipt = open_verified_media_file(root, file_plan.old_path)
            try:
                _verify_receipt_details(
                    receipt,
                    file_plan.size,
                    file_plan.sha256,
                    file_plan.image_format,
                    file_plan.width,
                    file_plan.height,
                    "media_verification_failed",
                    expected_identity=(
                        file_plan.source_device,
                        file_plan.source_inode,
                    ),
                )
                self._inject_fault("after_source_revalidation")
            finally:
                receipt.close()
        root.verify_current()

    def _destination_identities(self, manifest, plan):
        planned_reusable = {
            identity[0]: (identity[1], identity[2])
            for identity in plan.reusable_destination_identities
        }
        published = manifest.state.published_by_image.get(
            plan.image_id, {}
        )
        identities = {}
        for file_plan in plan.files:
            if file_plan.operation == "verify":
                identity = (
                    file_plan.source_device,
                    file_plan.source_inode,
                )
            else:
                identity = published.get(
                    file_plan.kind_key,
                    planned_reusable.get(file_plan.new_path),
                )
            if identity is None:
                raise _command_error("media_verification_failed")
            identities[file_plan.kind_key] = identity
        return identities

    def _verify_plan_destinations_from(
        self, root, plan, destination_identities
    ):
        for file_plan in plan.files:
            receipt = open_verified_media_file(root, file_plan.new_path)
            try:
                _verify_receipt_details(
                    receipt,
                    file_plan.size,
                    file_plan.sha256,
                    file_plan.image_format,
                    file_plan.width,
                    file_plan.height,
                    "media_verification_failed",
                    expected_identity=destination_identities[
                        file_plan.kind_key
                    ],
                )
                self._inject_fault("after_destination_revalidation")
            finally:
                receipt.close()
        root.verify_current()

    def _open_plan_destination_receipts(
        self, root, plan, destination_identities
    ):
        receipt_records = []
        try:
            for file_plan in plan.files:
                receipt = open_verified_media_file(
                    root, file_plan.new_path
                )
                receipt_records.append(
                    (
                        receipt,
                        file_plan,
                        destination_identities[file_plan.kind_key],
                    )
                )
            self._verify_destination_receipts(root, receipt_records)
            return receipt_records
        except BaseException:
            self._close_destination_receipts(
                receipt_records, suppress_errors=True
            )
            raise

    def _verify_destination_receipts(self, root, receipt_records):
        for receipt, file_plan, expected_identity in receipt_records:
            _verify_receipt_details(
                receipt,
                file_plan.size,
                file_plan.sha256,
                file_plan.image_format,
                file_plan.width,
                file_plan.height,
                "media_verification_failed",
                expected_identity=expected_identity,
            )
        root.verify_current()

    def _close_destination_receipts(
        self, receipt_records, suppress_errors=False
    ):
        first_error = None
        for receipt, _file_plan, _expected_identity in reversed(
            receipt_records
        ):
            try:
                receipt.close()
            except BaseException as error:
                if first_error is None:
                    first_error = error
        if first_error is not None and not suppress_errors:
            raise first_error

    def _database_state(self, plan, lock=False):
        queryset = Image.objects
        if lock:
            queryset = queryset.select_for_update()
        try:
            image = queryset.get(pk=plan.image_id)
        except Image.DoesNotExist:
            return "changed"
        thumbnails = Thumbnail.objects.filter(
            original_id=plan.image_id
        ).order_by("pk")
        if lock:
            thumbnails = thumbnails.select_for_update()
        thumbnails = list(thumbnails)
        metadata_matches = (
            str(image.asset_uuid) == plan.asset_uuid
            and image.original_filename == plan.original_filename
            and image.width == plan.image_width
            and image.height == plan.image_height
            and tuple(
                (
                    record.pk,
                    record.size,
                    record.width,
                    record.height,
                )
                for record in thumbnails
            )
            == tuple(
                (row[0], row[1], row[3], row[4])
                for row in plan.thumbnail_rows
            )
        )
        if not metadata_matches:
            return "changed"
        current = {"original": image.image.name}
        current.update(
            {
                "thumbnail:{}".format(record.pk): record.image.name
                for record in thumbnails
            }
        )
        old = {
            file_plan.kind_key: file_plan.old_path
            for file_plan in plan.files
        }
        new = {
            file_plan.kind_key: file_plan.new_path
            for file_plan in plan.files
        }
        if current == old:
            return "old"
        if current == new:
            return "new"
        return "changed"

    def _validate_image_plan_closure(self, plans, lock=False):
        queryset = Image.objects.order_by("pk")
        if lock:
            queryset = queryset.select_for_update()
        current_image_ids = tuple(
            queryset.values_list("pk", flat=True)
        )
        planned_image_ids = tuple(plan.image_id for plan in plans)
        if current_image_ids != planned_image_ids:
            raise _command_error("media_migration_database_changed")

    def _commit_batches(self, manifest, plans):
        root = None
        try:
            root = open_verified_media_root(settings.MEDIA_ROOT)
            all_plans = list(manifest.state.plans)
            self._validate_image_plan_closure(all_plans)
            for start in range(0, len(plans), self.batch_size):
                batch = plans[start:start + self.batch_size]
                receipt_records = []
                try:
                    for plan in batch:
                        receipt_records.extend(
                            self._open_plan_destination_receipts(
                                root,
                                plan,
                                self._destination_identities(
                                    manifest, plan
                                ),
                            )
                        )
                    with transaction.atomic():
                        self._inject_fault("before_database_update")
                        self._validate_image_plan_closure(
                            all_plans, lock=True
                        )
                        self._verify_destination_receipts(
                            root, receipt_records
                        )
                        for plan in batch:
                            if self._database_state(plan, lock=True) != "old":
                                raise _command_error(
                                    "media_migration_database_changed"
                                )
                            original = plan.files[0]
                            if original.old_path != original.new_path:
                                updated = Image.objects.filter(
                                    pk=plan.image_id,
                                    image=original.old_path,
                                ).update(image=original.new_path)
                                if updated != 1:
                                    raise _command_error(
                                        "media_migration_database_changed"
                                    )
                            for file_plan in plan.files[1:]:
                                if file_plan.old_path == file_plan.new_path:
                                    continue
                                updated = Thumbnail.objects.filter(
                                    pk=file_plan.thumbnail_id,
                                    original_id=plan.image_id,
                                    image=file_plan.old_path,
                                ).update(image=file_plan.new_path)
                                if updated != 1:
                                    raise _command_error(
                                        "media_migration_database_changed"
                                    )
                        self._verify_destination_receipts(
                            root, receipt_records
                        )
                        self._validate_image_plan_closure(
                            all_plans, lock=True
                        )
                except BaseException:
                    self._close_destination_receipts(
                        receipt_records, suppress_errors=True
                    )
                    raise
                else:
                    self._close_destination_receipts(receipt_records)
                self._inject_fault("after_database_commit")
                for plan in batch:
                    manifest.record_result(
                        "committed", plan.image_id
                    )
        except CommandError:
            raise
        except (MediaPathError, OSError) as error:
            raise _command_error("media_verification_failed", error)
        finally:
            if root is not None:
                root.close()

    def _inject_fault(self, point):
        if self.fault_injector is not None:
            self.fault_injector(point)


def _verify_staging(descriptor, file_plan):
    file_stat = os.fstat(descriptor)
    if (
        file_stat.st_size != file_plan.size
        or sha256_file_descriptor(descriptor) != file_plan.sha256
    ):
        raise _command_error("media_verification_failed")
    with warnings.catch_warnings():
        warnings.simplefilter("error", PILImage.DecompressionBombWarning)
        with os.fdopen(os.dup(descriptor), "rb") as staging:
            staging.seek(0)
            with PILImage.open(staging) as image:
                image.load()
                if (
                    image.format != file_plan.image_format
                    or image.size
                    != (file_plan.width, file_plan.height)
                ):
                    raise _command_error("media_verification_failed")
