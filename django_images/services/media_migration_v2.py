import collections
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
import re
import stat
from types import MappingProxyType
import uuid
import warnings

from django.conf import settings
from django.core.management import CommandError
from django.db import transaction
from django.utils.text import get_valid_filename
from PIL import Image as PILImage
from PIL import ImageFile

from django_images.file_ops import (
    MediaPathError,
    OwnedStagingFile,
    create_owned_staging_file,
    open_or_create_media_directory_from,
    open_verified_media_file,
    open_verified_media_root,
    publish_preverified_noreplace,
    replace_preverified_destination,
    rename_media_noreplace,
    sha256_file_descriptor,
)
from django_images.models import Image, Thumbnail
from django_images.paths import (
    DERIVATIVE_NAMES,
    FORMAT_EXTENSIONS,
    canonical_derivative_path,
    canonical_original_path,
    pinry_direct_md5_root,
)
from django_images.services.migration_batch_log import (
    BatchIntent,
    BatchLimits,
    FileReceipt,
    JOURNAL_FILENAME,
    MigrationBatchJournal,
    MigrationBatchLogError,
    _durable_fsync,
    _durable_syncfs,
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
    archive_root_device: int = None
    archive_root_inode: int = None

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
        if self.archive_root_device is not None:
            value["archive_root_device"] = self.archive_root_device
            value["archive_root_inode"] = self.archive_root_inode
        return value

    def as_skeleton_dict(self):
        value = {
            "kind": self.kind,
            "old_path": self.old_path,
            "size": self.size,
            "source_device": self.source_device,
            "source_inode": self.source_inode,
            "width": self.width,
            "height": self.height,
        }
        if self.thumbnail_id is not None:
            value["thumbnail_id"] = self.thumbnail_id
            value["derivative_size"] = self.derivative_size
        if self.archive_root_device is not None:
            value["archive_root_device"] = self.archive_root_device
            value["archive_root_inode"] = self.archive_root_inode
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
            archive_root_device=value.get("archive_root_device"),
            archive_root_inode=value.get("archive_root_inode"),
        )

    @classmethod
    def from_skeleton_dict(cls, value):
        required = {
            "kind",
            "old_path",
            "size",
            "source_device",
            "source_inode",
            "width",
            "height",
        }
        allowed = required | {
            "thumbnail_id",
            "derivative_size",
            "archive_root_device",
            "archive_root_inode",
        }
        if (
            type(value) is not dict
            or not required.issubset(value)
            or not set(value).issubset(allowed)
        ):
            raise _command_error("invalid_auto_v2_manifest")
        return cls(
            kind=value["kind"],
            old_path=value["old_path"],
            new_path=None,
            operation=None,
            size=value["size"],
            sha256=None,
            image_format=None,
            width=value["width"],
            height=value["height"],
            source_device=value["source_device"],
            source_inode=value["source_inode"],
            thumbnail_id=value.get("thumbnail_id"),
            derivative_size=value.get("derivative_size"),
            archive_root_device=value.get("archive_root_device"),
            archive_root_inode=value.get("archive_root_inode"),
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
                archive_root_identity = _archive_root_identity(
                    receipt,
                    record.image.name,
                )
                inspected.append(
                    (
                        kind,
                        record,
                        thumbnail,
                        receipt.file_stat,
                        archive_root_identity,
                    )
                )
            except (MediaPathError, OSError) as error:
                raise _command_error("unsafe_media_file", error)
            except (
                PILImage.DecompressionBombError,
                PILImage.UnidentifiedImageError,
                Warning,
            ) as error:
                raise _command_error("invalid_legacy_media", error)
            finally:
                if receipt is not None:
                    receipt.close()

        files = []
        for (
            kind,
            record,
            thumbnail,
            source_stat,
            archive_root_identity,
        ) in inspected:
            old_path = record.image.name
            files.append(
                AutoV2MigrationFile(
                    kind=kind,
                    old_path=old_path,
                    new_path=None,
                    operation=None,
                    size=source_stat.st_size,
                    sha256=None,
                    image_format=None,
                    width=record.width,
                    height=record.height,
                    source_device=source_stat.st_dev,
                    source_inode=source_stat.st_ino,
                    thumbnail_id=thumbnail.pk if thumbnail else None,
                    derivative_size=thumbnail.size if thumbnail else None,
                    archive_root_device=(
                        archive_root_identity[0]
                        if archive_root_identity is not None
                        else None
                    ),
                    archive_root_inode=(
                        archive_root_identity[1]
                        if archive_root_identity is not None
                        else None
                    ),
                )
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
            generation=None,
            files=tuple(files),
            thumbnail_rows=thumbnail_rows,
            copy_required_bytes=0,
            reusable_destination_identities=(),
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

    def as_skeleton_dict(self):
        return {
            "image_id": self.image_id,
            "asset_uuid": self.asset_uuid,
            "original_filename": self.original_filename,
            "image_width": self.image_width,
            "image_height": self.image_height,
            "files": [
                file_plan.as_skeleton_dict() for file_plan in self.files
            ],
            "thumbnail_rows": [list(row) for row in self.thumbnail_rows],
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

    @classmethod
    def from_skeleton_dict(cls, value):
        required = {
            "image_id",
            "asset_uuid",
            "original_filename",
            "image_width",
            "image_height",
            "files",
            "thumbnail_rows",
        }
        if type(value) is not dict or set(value) != required:
            raise _command_error("invalid_auto_v2_manifest")
        plan = cls(
            image_id=value["image_id"],
            asset_uuid=value["asset_uuid"],
            original_filename=value["original_filename"],
            image_width=value["image_width"],
            image_height=value["image_height"],
            generation=None,
            files=tuple(
                AutoV2MigrationFile.from_skeleton_dict(file_info)
                for file_info in value["files"]
            ),
            thumbnail_rows=tuple(tuple(row) for row in value["thumbnail_rows"]),
            copy_required_bytes=0,
            reusable_destination_identities=(),
        )
        _validate_plan_skeleton(plan)
        return plan


def _validate_derivative_records(records):
    seen = set()
    for record in records:
        if record.size not in DERIVATIVE_NAMES:
            raise _command_error("unsupported_legacy_derivative_size")
        if record.size in seen:
            raise _command_error("duplicate_derivative_size")
        seen.add(record.size)


def _archive_root_identity(receipt, relative_path):
    direct_root = pinry_direct_md5_root(relative_path)
    archive_root = (
        direct_root
        if direct_root is not None
        else "image" if relative_path.startswith("image/") else None
    )
    if archive_root is None:
        return None
    parent = receipt.parent_directory
    if (
        not parent.names
        or parent.names[0] != archive_root
        or not parent.directory_stats
    ):
        raise _command_error("unsafe_media_file")
    archive_root_stat = parent.directory_stats[0]
    return archive_root_stat.st_dev, archive_root_stat.st_ino


def _inspect_receipt(receipt):
    receipt.verify_current()
    digest = sha256_file_descriptor(receipt.descriptor)
    with warnings.catch_warnings():
        # 기존 파일은 경고 구간까지 허용하되 Pillow hard limit은 유지한다.
        warnings.simplefilter("ignore", PILImage.DecompressionBombWarning)
        with os.fdopen(os.dup(receipt.descriptor), "rb") as source:
            source.seek(0)
            with PILImage.open(source) as image:
                image_format = image.format
                width, height = image.size
                image.verify()
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
    named_parent, named_leaf = original.new_path.rsplit("/", 1)
    django_normalized_path = "{}/{}".format(
        named_parent,
        get_valid_filename(named_leaf),
    )
    if (
        original.old_path in (fixed_path, django_normalized_path)
        and original.old_path != original.new_path
        and all(
            file_plan.old_path == file_plan.new_path
            for file_plan in derivative_files
        )
    ):
        return "fixed_slot"
    prefixed_md5 = (
        original.old_path.startswith("image/original/by-md5/")
        and all(
            file_plan.old_path.startswith("image/thumbnail/by-md5/")
            for file_plan in derivative_files
        )
    )
    pinry_direct_md5 = all(
        pinry_direct_md5_root(file_plan.old_path) is not None
        for file_plan in files
    )
    if (
        (prefixed_md5 or pinry_direct_md5)
        and all(
            file_plan.old_path != file_plan.new_path
            for file_plan in files
        )
    ):
        return "md5_legacy"
    raise _command_error("mixed_media_state")


def _validate_plan_skeleton(plan):  # noqa: C901
    try:
        canonical_uuid = str(uuid.UUID(plan.asset_uuid)) == plan.asset_uuid
    except (AttributeError, TypeError, ValueError):
        canonical_uuid = False
    if (
        type(plan.image_id) is not int
        or plan.image_id <= 0
        or not canonical_uuid
        or type(plan.original_filename) is not str
        or type(plan.image_width) is not int
        or type(plan.image_height) is not int
        or plan.image_width <= 0
        or plan.image_height <= 0
        or plan.generation is not None
        or not plan.files
        or plan.files[0].kind != "original"
        or plan.files[0].thumbnail_id is not None
        or plan.files[0].width != plan.image_width
        or plan.files[0].height != plan.image_height
        or plan.copy_required_bytes != 0
        or plan.reusable_destination_identities
    ):
        raise _command_error("invalid_auto_v2_manifest")
    derivative_ids = set()
    derivative_sizes = set()
    for file_plan in plan.files:
        archive_identity = (
            file_plan.archive_root_device,
            file_plan.archive_root_inode,
        )
        has_archive_root = (
            pinry_direct_md5_root(file_plan.old_path) is not None
            or file_plan.old_path.startswith("image/")
        )
        if (
            not _safe_relative_path(file_plan.old_path)
            or file_plan.new_path is not None
            or file_plan.operation is not None
            or file_plan.sha256 is not None
            or file_plan.image_format is not None
            or type(file_plan.size) is not int
            or file_plan.size < 0
            or type(file_plan.width) is not int
            or type(file_plan.height) is not int
            or file_plan.width <= 0
            or file_plan.height <= 0
            or type(file_plan.source_device) is not int
            or file_plan.source_device < 0
            or type(file_plan.source_inode) is not int
            or file_plan.source_inode <= 0
            or (
                has_archive_root
                and (
                    type(archive_identity[0]) is not int
                    or archive_identity[0] < 0
                    or type(archive_identity[1]) is not int
                    or archive_identity[1] <= 0
                )
            )
            or (not has_archive_root and archive_identity != (None, None))
        ):
            raise _command_error("invalid_auto_v2_manifest")
        if file_plan.kind == "original":
            if file_plan is not plan.files[0]:
                raise _command_error("invalid_auto_v2_manifest")
            continue
        if (
            file_plan.kind != "derivative"
            or type(file_plan.thumbnail_id) is not int
            or file_plan.thumbnail_id <= 0
            or file_plan.thumbnail_id in derivative_ids
            or file_plan.derivative_size not in DERIVATIVE_NAMES
            or file_plan.derivative_size in derivative_sizes
        ):
            raise _command_error("invalid_auto_v2_manifest")
        derivative_ids.add(file_plan.thumbnail_id)
        derivative_sizes.add(file_plan.derivative_size)
    expected_rows = tuple(
        (
            file_plan.thumbnail_id,
            file_plan.derivative_size,
            file_plan.old_path,
            file_plan.width,
            file_plan.height,
        )
        for file_plan in plan.files[1:]
    )
    if plan.thumbnail_rows != expected_rows:
        raise _command_error("manifest_plan_mismatch")


def _validate_plan(plan):  # noqa: C901
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
    prefixed_archive_identities = set()
    for file_plan in plan.files:
        direct_root = pinry_direct_md5_root(file_plan.old_path)
        prefixed_root = file_plan.old_path.startswith("image/")
        archive_identity = (
            file_plan.archive_root_device,
            file_plan.archive_root_inode,
        )
        if direct_root is not None:
            if (
                type(archive_identity[0]) is not int
                or archive_identity[0] < 0
                or type(archive_identity[1]) is not int
                or archive_identity[1] <= 0
            ):
                raise _command_error("invalid_auto_v2_manifest")
        elif prefixed_root:
            if archive_identity != (None, None) and (
                type(archive_identity[0]) is not int
                or archive_identity[0] < 0
                or type(archive_identity[1]) is not int
                or archive_identity[1] <= 0
            ):
                raise _command_error("invalid_auto_v2_manifest")
            prefixed_archive_identities.add(archive_identity)
        elif archive_identity != (None, None):
            raise _command_error("invalid_auto_v2_manifest")
    if len(prefixed_archive_identities) > 1:
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


def _validate_new_plan_archive_authority(plan):
    """현재 writer는 archive root identity가 빠진 계획을 만들지 않는다."""
    if not isinstance(plan, AutoV2MigrationPlan):
        raise _command_error("invalid_auto_v2_manifest")
    _validate_plan(plan)
    for file_plan in plan.files:
        if (
            pinry_direct_md5_root(file_plan.old_path) is None
            and not file_plan.old_path.startswith("image/")
        ):
            continue
        if (
            type(file_plan.archive_root_device) is not int
            or file_plan.archive_root_device < 0
            or type(file_plan.archive_root_inode) is not int
            or file_plan.archive_root_inode <= 0
        ):
            raise _command_error("invalid_auto_v2_manifest")


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


@dataclass(frozen=True)
class AutoV2ArchiveAuthority(object):
    summary: AutoV2PlanSummary
    fixed_slot_sources: tuple
    fixed_slot_files: tuple
    prefixed_files: tuple
    prefixed_root_identity: tuple
    direct_files: tuple
    direct_root_identities: tuple


@dataclass(frozen=True)
class AutoV2CompletionAuthority(object):
    summary: AutoV2PlanSummary
    plans: tuple
    archive_authority: AutoV2ArchiveAuthority

    @classmethod
    def load(cls, plan_manifest, batch_journal):
        if not isinstance(plan_manifest, AutoV2ManifestLog):
            raise _command_error("invalid_auto_v2_manifest")
        if not isinstance(batch_journal, MigrationBatchJournal):
            raise _command_error("linear_journal_invalid")
        base = plan_manifest.summary()
        try:
            batch_journal.state.require_source(
                "paths", base.plan_sha256, base.manifest_sha256
            )
        except MigrationBatchLogError as error:
            raise _command_error(error.code, error)
        if not batch_journal.is_phase_complete("paths"):
            raise _command_error("auto_v2_plan_incomplete")
        receipts_by_image = batch_journal.receipts_by_image("paths")
        plans = tuple(
            _join_plan_and_receipts(
                plan, receipts_by_image.get(plan.image_id, ())
            )
            for plan in plan_manifest.state.plans
        )
        planned_ids = {plan.image_id for plan in plans}
        if set(receipts_by_image) != planned_ids:
            raise _command_error("auto_v2_plan_incomplete")
        phase = batch_journal.state.phase_summaries["paths"]
        counts = _plan_counts(plans)
        copy_required_bytes = _copy_required_bytes(plans)
        if phase != {
            "image_count": len(plans),
            "md5_legacy": counts["md5_legacy"],
            "fixed_slot": counts["fixed_slot"],
            "named_canonical": counts["named_canonical"],
            "copy_required_bytes": copy_required_bytes,
        }:
            raise _command_error("manifest_plan_mismatch")
        for batch_id in batch_journal.committed_ids("paths"):
            intent = batch_journal.intent_for(batch_id)
            if any(
                receipt.database_signature != intent.post_signature
                for receipt in batch_journal.effective_receipts(batch_id)
            ):
                raise _command_error("linear_journal_batch_conflict")
        summary = AutoV2PlanSummary(
            run_id=base.run_id,
            plan_sha256=base.plan_sha256,
            manifest_sha256=base.manifest_sha256,
            image_count=len(plans),
            md5_legacy=counts["md5_legacy"],
            fixed_slot=counts["fixed_slot"],
            named_canonical=counts["named_canonical"],
            copy_required_bytes=copy_required_bytes,
        )
        archive_authority = _archive_authority_from_plans(summary, plans)
        return cls(summary, plans, archive_authority)


def _file_key_for_plan(plan, file_plan):
    if file_plan.thumbnail_id is None:
        return "original:{}".format(plan.image_id)
    return "thumbnail:{}:{}".format(
        plan.image_id, file_plan.thumbnail_id
    )


def _join_plan_and_receipts(plan, receipts):
    by_key = {}
    for receipt in receipts:
        if receipt.file_key in by_key:
            raise _command_error("linear_journal_batch_conflict")
        by_key[receipt.file_key] = receipt
    expected_keys = {
        _file_key_for_plan(plan, file_plan) for file_plan in plan.files
    }
    if set(by_key) != expected_keys:
        raise _command_error("auto_v2_plan_incomplete")
    files = []
    for file_plan in plan.files:
        receipt = by_key[_file_key_for_plan(plan, file_plan)]
        if (
            receipt.source_device != file_plan.source_device
            or receipt.source_inode != file_plan.source_inode
            or receipt.width != file_plan.width
            or receipt.height != file_plan.height
            or receipt.size != file_plan.size
        ):
            raise _command_error("manifest_plan_mismatch")
        files.append(AutoV2MigrationFile(
            kind=file_plan.kind,
            old_path=file_plan.old_path,
            new_path=receipt.relative_path,
            operation=receipt.operation,
            size=receipt.size,
            sha256=receipt.sha256,
            image_format=receipt.image_format,
            width=receipt.width,
            height=receipt.height,
            source_device=receipt.source_device,
            source_inode=receipt.source_inode,
            thumbnail_id=file_plan.thumbnail_id,
            derivative_size=file_plan.derivative_size,
            archive_root_device=file_plan.archive_root_device,
            archive_root_inode=file_plan.archive_root_inode,
        ))
    generation = _classify_generation_for_uuid(plan.asset_uuid, files)
    joined = AutoV2MigrationPlan(
        image_id=plan.image_id,
        asset_uuid=plan.asset_uuid,
        original_filename=plan.original_filename,
        image_width=plan.image_width,
        image_height=plan.image_height,
        generation=generation,
        files=tuple(files),
        thumbnail_rows=plan.thumbnail_rows,
        copy_required_bytes=sum(
            file_plan.size
            for file_plan in files
            if file_plan.operation == "copy"
        ),
    )
    _validate_plan(joined)
    return joined


@dataclass
class _PreparedFile(object):
    plan: object
    file_plan: object
    file_key: str
    staging: object
    destination: str
    operation: str
    size: int
    sha256: str
    image_format: str
    width: int
    height: int
    source_device: int
    source_inode: int
    destination_device: int = None
    destination_inode: int = None


class _AutoV2ManifestState(object):
    def __init__(self):
        self._frozen = False
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
        self.format_version = None

    def __setattr__(self, name, value):
        if getattr(self, "_frozen", False):
            raise AttributeError("manifest_state_is_read_only")
        object.__setattr__(self, name, value)

    def freeze(self):
        self.events = tuple(_freeze_manifest_value(event) for event in self.events)
        self.plans = tuple(self.plans)
        self.plan_by_image = MappingProxyType(dict(self.plan_by_image))
        self.latest_by_image = MappingProxyType(dict(self.latest_by_image))
        self.publish_intents_by_image = MappingProxyType({
            image_id: MappingProxyType(dict(intents))
            for image_id, intents in self.publish_intents_by_image.items()
        })
        self.published_by_image = MappingProxyType({
            image_id: MappingProxyType(dict(published))
            for image_id, published in self.published_by_image.items()
        })
        self._frozen = True
        return self


def _freeze_manifest_value(value):
    if isinstance(value, MappingProxyType):
        return value
    if isinstance(value, dict):
        return MappingProxyType({
            key: _freeze_manifest_value(nested)
            for key, nested in value.items()
        })
    if isinstance(value, list):
        return tuple(_freeze_manifest_value(nested) for nested in value)
    return value


def _copy_manifest_state(state):
    copied = _AutoV2ManifestState()
    copied.events = list(state.events)
    copied.plans = list(state.plans)
    copied.plan_by_image = dict(state.plan_by_image)
    copied.latest_by_image = dict(state.latest_by_image)
    copied.publish_intents_by_image = {
        image_id: dict(intents)
        for image_id, intents in state.publish_intents_by_image.items()
    }
    copied.published_by_image = {
        image_id: dict(published)
        for image_id, published in state.published_by_image.items()
    }
    copied.plan_complete = state.plan_complete
    copied.plan_end_offset = state.plan_end_offset
    copied.torn_tail = state.torn_tail
    copied.torn_offset = state.torn_offset
    copied.raw_bytes = state.raw_bytes
    copied.format_version = state.format_version
    return copied


def _apply_manifest_event(  # noqa: C901
    state,
    event,
    expected_run_id,
    offset,
    raw,
):
    _validate_manifest_event(event, expected_run_id)
    event_name = event["event"]
    format_version = event["format_version"]
    if state.format_version is None:
        state.format_version = format_version
    elif state.format_version != format_version:
        raise _command_error("manifest_plan_mismatch")
    if event_name in ("planned", "planned_skeleton"):
        if state.plan_complete:
            raise _command_error("invalid_auto_v2_manifest")
        plan = (
            AutoV2MigrationPlan.from_dict(event.get("plan"))
            if event_name == "planned"
            else AutoV2MigrationPlan.from_skeleton_dict(event.get("plan"))
        )
        if plan.image_id in state.plan_by_image:
            raise _command_error("manifest_plan_mismatch")
        state.plans.append(plan)
        state.plan_by_image[plan.image_id] = plan
    elif event_name == "plan_complete":
        if state.plan_complete:
            raise _command_error("invalid_auto_v2_manifest")
        _validate_plan_marker(event, state.plans)
        state.plan_complete = True
        state.plan_end_offset = offset
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
                file_plan.kind_key: file_plan for file_plan in plan.files
            }
            file_key = event.get("file_key")
            identity = (
                event.get("staging_device"),
                event.get("staging_inode"),
                event.get("destination_parent_device"),
                event.get("destination_parent_inode"),
                event.get("staging_name"),
            )
            current_intents = state.publish_intents_by_image.get(
                image_id, {}
            )
            if (
                file_key not in file_by_key
                or file_by_key[file_key].operation != "copy"
                or file_key in current_intents
                or any(type(value) is not int for value in identity[:4])
                or identity[0] < 0
                or identity[1] <= 0
                or identity[2] < 0
                or identity[3] <= 0
                or not _valid_staging_name(identity[4])
            ):
                raise _command_error("invalid_auto_v2_manifest")
            intents = dict(current_intents)
            intents[file_key] = identity
            state.publish_intents_by_image[image_id] = intents
        elif event_name == "published":
            plan = state.plan_by_image[image_id]
            file_by_key = {
                file_plan.kind_key: file_plan for file_plan in plan.files
            }
            file_key = event.get("file_key")
            destination_device = event.get("destination_device")
            destination_inode = event.get("destination_inode")
            current_published = state.published_by_image.get(image_id, {})
            if (
                file_key not in file_by_key
                or file_by_key[file_key].operation != "copy"
                or file_key in current_published
                or type(destination_device) is not int
                or type(destination_inode) is not int
                or destination_device < 0
                or destination_inode <= 0
            ):
                raise _command_error("invalid_auto_v2_manifest")
            intent = state.publish_intents_by_image.get(image_id, {}).get(
                file_key
            )
            if intent is not None and (
                destination_device,
                destination_inode,
            ) != intent[:2]:
                raise _command_error("manifest_plan_mismatch")
            published = dict(current_published)
            published[file_key] = (
                destination_device,
                destination_inode,
            )
            state.published_by_image[image_id] = published
        state.latest_by_image[image_id] = event_name
    state.events.append(event)


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
    def open(  # noqa: C901
        cls,
        run_directory,
        filename,
        run_id,
        service_uid,
        service_gid,
        create=True,
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
            or type(create) is not bool
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
                if not create:
                    raise
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

    def _load_state(self):  # noqa: C901
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
            _apply_manifest_event(
                state,
                event,
                self.run_id,
                offset + len(line),
                raw,
            )
            offset += len(line)
        return state.freeze()

    def append(self, event):
        if self.state.torn_tail is not None:
            raise _command_error(
                "media_manifest_torn_tail_requires_execute"
            )
        self._ensure_content_current()
        try:
            event = dict(event)
        except (TypeError, ValueError) as error:
            raise _command_error("invalid_auto_v2_manifest", error)
        if event.get("event") == "planned":
            if set(event) != {"event", "plan"}:
                raise _command_error("invalid_auto_v2_manifest")
            plan_payload = event.get("plan")
            plan = AutoV2MigrationPlan.from_dict(plan_payload)
            _validate_new_plan_archive_authority(plan)
            if plan.as_dict() != plan_payload:
                raise _command_error("invalid_auto_v2_manifest")
        event.update(
            {
                "format_version": 2,
                "target_signature": AUTO_V2_TARGET_SIGNATURE,
                "run_id": self.run_id,
            }
        )
        line = _json_line(event)
        try:
            canonical_event = json.loads(line.decode("utf-8"))
        except (TypeError, ValueError, UnicodeDecodeError) as error:
            raise _command_error("invalid_auto_v2_manifest", error)
        candidate = _copy_manifest_state(self.state)
        _apply_manifest_event(
            candidate,
            canonical_event,
            self.run_id,
            len(self.state.raw_bytes) + len(line),
            self.state.raw_bytes,
        )
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
        current = self._read_all()
        previous_size = len(self.state.raw_bytes)
        if (
            len(current) != previous_size + len(line)
            or current[:previous_size] != self.state.raw_bytes
            or current[previous_size:] != line
        ):
            raise _command_error("unsafe_auto_v2_manifest")
        candidate.raw_bytes = current
        self.state = candidate.freeze()

    def write_frozen_plan(self, plans):
        if self.state.raw_bytes or self.state.events:
            raise _command_error("auto_v2_plan_reset_forbidden")
        plans = tuple(plans)
        for plan in plans:
            _validate_plan_skeleton(plan)
        common = {
            "format_version": 3,
            "target_signature": AUTO_V2_TARGET_SIGNATURE,
            "run_id": self.run_id,
        }
        digest = hashlib.sha256()
        self._verify_current()
        os.lseek(self.descriptor, 0, os.SEEK_END)

        def write_event(event):
            line = _json_line(event)
            digest.update(line)
            view = memoryview(line)
            while view:
                written = os.write(self.descriptor, view)
                if written <= 0:
                    raise _command_error("unsafe_auto_v2_manifest")
                view = view[written:]

        for plan in plans:
            write_event(dict(
                common,
                event="planned_skeleton",
                plan=plan.as_skeleton_dict(),
            ))
        write_event(dict(
            common,
            event="plan_complete",
            image_count=len(plans),
            files_total=sum(len(plan.files) for plan in plans),
        ))
        _durable_fsync(self.descriptor, "plan_manifest")
        self.state = self._load_state()
        if hashlib.sha256(self.state.raw_bytes).digest() != digest.digest():
            raise _command_error("unsafe_auto_v2_manifest")
        return digest.hexdigest()

    def record_plan(self, plan):
        _validate_new_plan_archive_authority(plan)
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
                "plan_sha256": self._plan_sha256(),
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
                "plan_sha256": self._plan_sha256(),
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
                "plan_sha256": self._plan_sha256(),
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

    def _reset_incomplete_plan(self):
        if self.state.plan_complete:
            raise _command_error("auto_v2_plan_reset_forbidden")
        changed = bool(self.state.events or self.state.torn_tail is not None)
        if not changed:
            return False
        if self.state.torn_tail is not None:
            self.repair_torn_tail()
        self._ensure_content_current()
        if self.state.plan_complete or any(
            event["event"] != "planned" for event in self.state.events
        ):
            raise _command_error("auto_v2_plan_reset_forbidden")
        self._verify_current()
        os.ftruncate(self.descriptor, 0)
        os.fsync(self.descriptor)
        os.fsync(self.run_directory.descriptor)
        self.state = self._load_state()
        return True

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
        if self.state.format_version == 3:
            counts = {
                "md5_legacy": 0,
                "fixed_slot": 0,
                "named_canonical": 0,
            }
            copy_required_bytes = 0
        else:
            counts = marker
            copy_required_bytes = marker["copy_required_bytes"]
        return AutoV2PlanSummary(
            run_id=self.run_id,
            plan_sha256=self._plan_sha256(),
            manifest_sha256=hashlib.sha256(
                self.state.raw_bytes
            ).hexdigest(),
            image_count=marker["image_count"],
            md5_legacy=counts["md5_legacy"],
            fixed_slot=counts["fixed_slot"],
            named_canonical=counts["named_canonical"],
            copy_required_bytes=copy_required_bytes,
        )

    def _plan_sha256(self):
        if self.state.torn_tail is not None:
            raise _command_error(
                "media_manifest_torn_tail_requires_execute"
            )
        if not self.state.plan_complete:
            raise _command_error("auto_v2_plan_incomplete")
        return hashlib.sha256(
            self.state.raw_bytes[:self.state.plan_end_offset]
        ).hexdigest()

    def _ensure_content_current(self):
        if self._read_all() != self.state.raw_bytes:
            raise _command_error("unsafe_auto_v2_manifest")
        return True


def _validate_manifest_event(event, run_id):
    if not isinstance(event, dict):
        raise _command_error("invalid_auto_v2_manifest")
    format_version = event.get("format_version")
    if (
        format_version not in (2, 3)
        or event.get("target_signature") != AUTO_V2_TARGET_SIGNATURE
    ):
        raise _command_error("manifest_plan_mismatch")
    if event.get("run_id") != run_id:
        raise _command_error("manifest_run_id_mismatch")
    allowed = (
        (
            "planned",
            "plan_complete",
            "publish_intent",
            "published",
            "committed",
            "recovered_commit",
            "already_current",
        )
        if format_version == 2
        else ("planned_skeleton", "plan_complete")
    )
    if event.get("event") not in allowed:
        raise _command_error("invalid_auto_v2_manifest")


def _validate_plan_marker(event, plans):
    if event.get("format_version") == 3:
        if (
            set(event)
            != {
                "event",
                "format_version",
                "target_signature",
                "run_id",
                "image_count",
                "files_total",
            }
            or event.get("image_count") != len(plans)
            or event.get("files_total")
            != sum(len(plan.files) for plan in plans)
        ):
            raise _command_error("manifest_plan_mismatch")
        return
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
        create=False,
    ) as manifest:
        return manifest.summary()


def load_completed_auto_v2_summary(
    run_directory,
    filename,
    run_id,
    service_uid,
    service_gid,
    batch_journal=None,
    completion_authority=None,
):
    """execute terminal이 전체 완결된 auto-v2 typed 요약만 읽는다."""
    if completion_authority is not None:
        if not isinstance(
            completion_authority, AutoV2CompletionAuthority
        ):
            raise _command_error("invalid_auto_v2_manifest")
        return completion_authority.summary
    with AutoV2ManifestLog.open(
        run_directory,
        filename,
        run_id,
        service_uid,
        service_gid,
        create=False,
    ) as manifest:
        if batch_journal is not None:
            return AutoV2CompletionAuthority.load(
                manifest, batch_journal
            ).summary
        return _completed_auto_v2_summary(manifest)


def _completed_auto_v2_summary(manifest):
    if manifest.state.format_version != 2:
        raise _command_error("auto_v2_plan_incomplete")
    summary = manifest.summary()
    expected_terminal = {
        "md5_legacy": frozenset(("committed", "recovered_commit")),
        "fixed_slot": frozenset(("committed", "recovered_commit")),
        "named_canonical": frozenset(("already_current",)),
    }
    if any(
        manifest.state.latest_by_image.get(plan.image_id)
        not in expected_terminal[plan.generation]
        for plan in manifest.state.plans
    ):
        raise _command_error("auto_v2_plan_incomplete")
    return summary


def recover_incomplete_auto_v2_plan(
    run_directory, filename, run_id, service_uid, service_gid
):
    """완료 marker 이전의 계획 prefix만 같은 run에서 재계획하게 한다."""
    with AutoV2ManifestLog.open(
        run_directory,
        filename,
        run_id,
        service_uid,
        service_gid,
        create=False,
    ) as manifest:
        return manifest._reset_incomplete_plan()


def load_auto_v2_archive_sources(
    run_directory,
    filename,
    run_id,
    service_uid,
    service_gid,
    batch_journal=None,
    completion_authority=None,
):
    """완료된 auto-v2 계획에서 fixed-slot 구 원본만 반환한다."""
    return load_auto_v2_archive_authority(
        run_directory,
        filename,
        run_id,
        service_uid,
        service_gid,
        batch_journal=batch_journal,
        completion_authority=completion_authority,
    ).fixed_slot_sources


def load_auto_v2_archive_authority(
    run_directory,
    filename,
    run_id,
    service_uid,
    service_gid,
    batch_journal=None,
    completion_authority=None,
):
    """단일 manifest snapshot에서 archive 권위 전체를 반환한다."""
    if completion_authority is not None:
        if not isinstance(
            completion_authority, AutoV2CompletionAuthority
        ):
            raise _command_error("invalid_auto_v2_manifest")
        return completion_authority.archive_authority
    with AutoV2ManifestLog.open(
        run_directory,
        filename,
        run_id,
        service_uid,
        service_gid,
        create=False,
    ) as manifest:
        if batch_journal is not None:
            return AutoV2CompletionAuthority.load(
                manifest, batch_journal
            ).archive_authority
        summary = _completed_auto_v2_summary(manifest)
        plans = tuple(manifest.state.plans)
        canonical_originals = frozenset(
            plan.new_original for plan in plans
        )
        sources = []
        fixed_slot_files = []
        for plan in plans:
            if plan.generation != "fixed_slot":
                continue
            original = plan.files[0]
            if (
                original.kind != "original"
                or original.thumbnail_id is not None
                or original.operation != "copy"
                or original.old_path == original.new_path
                or original.old_path in canonical_originals
                or original.old_path in sources
            ):
                raise _command_error("manifest_plan_mismatch")
            sources.append(original.old_path)
            fixed_slot_files.append(original)
        prefixed_files = []
        prefixed_root_identity = None
        prefixed_root_identity_missing = False
        direct_files = []
        direct_root_identities = {}
        for plan in plans:
            if plan.generation != "md5_legacy":
                continue
            roots = tuple(
                pinry_direct_md5_root(file_plan.old_path)
                for file_plan in plan.files
            )
            if plan.files and all(root is not None for root in roots):
                for file_plan, root_name in zip(plan.files, roots):
                    identity = (
                        file_plan.archive_root_device,
                        file_plan.archive_root_inode,
                    )
                    if (
                        type(identity[0]) is not int
                        or identity[0] < 0
                        or type(identity[1]) is not int
                        or identity[1] <= 0
                        or (
                            root_name in direct_root_identities
                            and direct_root_identities[root_name] != identity
                        )
                    ):
                        raise _command_error("manifest_plan_mismatch")
                    direct_root_identities[root_name] = identity
                    direct_files.append(file_plan)
                continue
            paths = tuple(file_plan.old_path for file_plan in plan.files)
            if not (
                paths
                and paths[0].startswith("image/original/by-md5/")
                and all(
                    path.startswith("image/thumbnail/by-md5/")
                    for path in paths[1:]
                )
            ):
                raise _command_error("manifest_plan_mismatch")
            for file_plan in plan.files:
                identity = (
                    file_plan.archive_root_device,
                    file_plan.archive_root_inode,
                )
                if identity == (None, None):
                    if prefixed_root_identity is not None:
                        raise _command_error("manifest_plan_mismatch")
                    prefixed_root_identity_missing = True
                    continue
                if (
                    prefixed_root_identity_missing
                    or type(identity[0]) is not int
                    or identity[0] < 0
                    or type(identity[1]) is not int
                    or identity[1] <= 0
                    or (
                        prefixed_root_identity is not None
                        and prefixed_root_identity != identity
                    )
                ):
                    raise _command_error("manifest_plan_mismatch")
                prefixed_root_identity = identity
            prefixed_files.extend(plan.files)
        archive_paths = tuple(
            file_plan.old_path
            for file_plan in (
                fixed_slot_files + prefixed_files + direct_files
            )
        )
        if len(archive_paths) != len(set(archive_paths)):
            raise _command_error("manifest_plan_mismatch")
        return AutoV2ArchiveAuthority(
            summary=summary,
            fixed_slot_sources=tuple(sources),
            fixed_slot_files=tuple(sorted(
                fixed_slot_files,
                key=lambda file_plan: file_plan.old_path,
            )),
            prefixed_files=tuple(sorted(
                prefixed_files,
                key=lambda file_plan: file_plan.old_path,
            )),
            prefixed_root_identity=prefixed_root_identity,
            direct_files=tuple(sorted(
                direct_files, key=lambda file_plan: file_plan.old_path
            )),
            direct_root_identities=tuple(sorted(
                (
                    root_name,
                    identity[0],
                    identity[1],
                )
                for root_name, identity in direct_root_identities.items()
            )),
        )


def load_auto_v2_archive_direct_roots(
    run_directory,
    filename,
    run_id,
    service_uid,
    service_gid,
    batch_journal=None,
    completion_authority=None,
):
    """완료된 계획이 참조하는 실제 Pinry MD5 최상위 root를 반환한다."""
    authority = load_auto_v2_archive_authority(
        run_directory,
        filename,
        run_id,
        service_uid,
        service_gid,
        batch_journal=batch_journal,
        completion_authority=completion_authority,
    )
    return tuple(
        item[0] for item in authority.direct_root_identities
    )


def _archive_authority_from_plans(summary, plans):
    canonical_originals = frozenset(plan.new_original for plan in plans)
    sources = []
    fixed_slot_files = []
    prefixed_files = []
    prefixed_root_identity = None
    prefixed_root_identity_missing = False
    direct_files = []
    direct_root_identities = {}
    for plan in plans:
        if plan.generation == "fixed_slot":
            original = plan.files[0]
            if (
                original.kind != "original"
                or original.thumbnail_id is not None
                or original.operation != "copy"
                or original.old_path == original.new_path
                or original.old_path in canonical_originals
                or original.old_path in sources
            ):
                raise _command_error("manifest_plan_mismatch")
            sources.append(original.old_path)
            fixed_slot_files.append(original)
        if plan.generation != "md5_legacy":
            continue
        roots = tuple(
            pinry_direct_md5_root(file_plan.old_path)
            for file_plan in plan.files
        )
        if plan.files and all(root is not None for root in roots):
            for file_plan, root_name in zip(plan.files, roots):
                identity = (
                    file_plan.archive_root_device,
                    file_plan.archive_root_inode,
                )
                if (
                    type(identity[0]) is not int
                    or identity[0] < 0
                    or type(identity[1]) is not int
                    or identity[1] <= 0
                    or (
                        root_name in direct_root_identities
                        and direct_root_identities[root_name] != identity
                    )
                ):
                    raise _command_error("manifest_plan_mismatch")
                direct_root_identities[root_name] = identity
                direct_files.append(file_plan)
            continue
        paths = tuple(file_plan.old_path for file_plan in plan.files)
        if not (
            paths
            and paths[0].startswith("image/original/by-md5/")
            and all(
                path.startswith("image/thumbnail/by-md5/")
                for path in paths[1:]
            )
        ):
            raise _command_error("manifest_plan_mismatch")
        for file_plan in plan.files:
            identity = (
                file_plan.archive_root_device,
                file_plan.archive_root_inode,
            )
            if identity == (None, None):
                if prefixed_root_identity is not None:
                    raise _command_error("manifest_plan_mismatch")
                prefixed_root_identity_missing = True
                continue
            if (
                prefixed_root_identity_missing
                or type(identity[0]) is not int
                or identity[0] < 0
                or type(identity[1]) is not int
                or identity[1] <= 0
                or (
                    prefixed_root_identity is not None
                    and prefixed_root_identity != identity
                )
            ):
                raise _command_error("manifest_plan_mismatch")
            prefixed_root_identity = identity
        prefixed_files.extend(plan.files)
    archive_files = fixed_slot_files + prefixed_files + direct_files
    archive_paths = tuple(file_plan.old_path for file_plan in archive_files)
    if len(archive_paths) != len(set(archive_paths)):
        raise _command_error("manifest_plan_mismatch")
    return AutoV2ArchiveAuthority(
        summary=summary,
        fixed_slot_sources=tuple(sources),
        fixed_slot_files=tuple(sorted(
            fixed_slot_files, key=lambda value: value.old_path
        )),
        prefixed_files=tuple(sorted(
            prefixed_files, key=lambda value: value.old_path
        )),
        prefixed_root_identity=prefixed_root_identity,
        direct_files=tuple(sorted(
            direct_files, key=lambda value: value.old_path
        )),
        direct_root_identities=tuple(sorted(
            (root_name, identity[0], identity[1])
            for root_name, identity in direct_root_identities.items()
        )),
    )


class AutoV2MediaMigrator(object):
    def __init__(
        self,
        run_directory,
        filename,
        run_id,
        service_uid,
        service_gid,
        batch_size=50,
        fault_injector=None,
        progress_reporter=None,
        batch_limits=None,
        batch_journal=None,
    ):
        if type(batch_size) is not int or batch_size <= 0:
            raise _command_error("batch_size_must_be_positive")
        if progress_reporter is not None and not callable(progress_reporter):
            raise _command_error("invalid_progress_reporter")
        if batch_limits is not None and not isinstance(
            batch_limits, BatchLimits
        ):
            raise _command_error("linear_journal_limits_invalid")
        if batch_journal is not None and not isinstance(
            batch_journal, MigrationBatchJournal
        ):
            raise _command_error("linear_journal_invalid")
        self.run_directory = run_directory
        self.filename = filename
        self.run_id = run_id
        self.service_uid = service_uid
        self.service_gid = service_gid
        self.batch_size = batch_size
        self.fault_injector = fault_injector
        self.progress_reporter = progress_reporter
        self.batch_journal = batch_journal
        self._planning_reported = False
        self.batch_limits = batch_limits or BatchLimits(max_images=batch_size)
        self._frozen_plans = None
        self._resume_attempt = False

    def recover_execution_tail(self):
        """완료된 계획 뒤 torn execute event만 복구한다."""
        with AutoV2ManifestLog.open(
            self.run_directory,
            self.filename,
            self.run_id,
            self.service_uid,
            self.service_gid,
        ) as manifest:
            if not manifest.state.plan_complete:
                raise _command_error("auto_v2_plan_incomplete")
            if manifest.state.torn_tail is not None:
                manifest.repair_torn_tail()
            return manifest.summary()

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
            if manifest.state.format_version == 2:
                try:
                    completed_v2 = _completed_auto_v2_summary(manifest)
                except CommandError as error:
                    if str(error) != "auto_v2_plan_incomplete":
                        raise
                else:
                    return completed_v2
            if not manifest.state.events:
                plans = (
                    self._freeze_all_plans()
                    if self._frozen_plans is None
                    else self._frozen_plans
                )
                self._frozen_plans = tuple(plans)
                manifest.write_frozen_plan(plans)
                self._inject_fault("after_plan_complete")
            elif not manifest.state.plan_complete:
                raise _command_error("auto_v2_plan_incomplete")
            plans = list(manifest.state.plans)
            summary = manifest.summary()
            if not execute:
                return summary
            return self._execute_linear(manifest, plans, summary)

    def _freeze_all_plans(self):
        root_directory = None
        try:
            root_directory = open_verified_media_root(settings.MEDIA_ROOT)
            plans = []
            for images, by_image in self._iter_image_batches():
                for image in images:
                    plans.append(AutoV2MigrationPlan.for_image(
                        image,
                        root_directory,
                        derivative_records=by_image[image.pk],
                    ))
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

    def _iter_image_batches(self):
        last_pk = 0
        initial_max_pk = Image.objects.order_by("-pk").values_list(
            "pk", flat=True
        ).first() or 0
        while last_pk < initial_max_pk:
            images = list(
                Image.objects.filter(
                    pk__gt=last_pk,
                    pk__lte=initial_max_pk,
                ).order_by("pk")[:self.batch_limits.max_images]
            )
            if not images:
                return
            image_ids = [image.pk for image in images]
            derivatives = Thumbnail.objects.filter(
                original_id__in=image_ids
            ).order_by("original_id", "size", "pk")
            by_image = collections.defaultdict(list)
            for derivative in derivatives:
                by_image[derivative.original_id].append(derivative)
            yield images, by_image
            last_pk = images[-1].pk

    def _execute_linear(self, manifest, plans, summary):  # noqa: C901
        owned_journal = self.batch_journal is None
        journal = self.batch_journal
        try:
            if journal is None:
                journal = MigrationBatchJournal.open(
                    manifest.run_directory,
                    JOURNAL_FILENAME,
                    self.run_id,
                    self.service_uid,
                    self.service_gid,
                    summary.plan_sha256,
                    summary.manifest_sha256,
                )
            journal.freeze_work_totals(
                len(plans),
                sum(len(plan.files) for plan in plans),
                0,
            )
            if (
                manifest.state.format_version == 2
                and not journal.state.intents
            ):
                self._upgrade_v2_terminal_prefix(
                    manifest, journal, plans
                )
            journal.record_attempt(
                datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
            )
            self._resume_attempt = len(journal.state.attempts) > 1
            self._validate_image_plan_closure(plans)
            if journal.is_phase_complete("paths"):
                repaired = self._verify_committed_destinations(
                    journal, plans
                )
                if repaired:
                    journal.write_checkpoint()
                self._validate_image_plan_closure(plans)
                return self._summary_from_journal(summary, journal)

            committed = journal.committed_ids("paths")
            self._verify_committed_destinations(journal, plans)
            root = open_verified_media_root(settings.MEDIA_ROOT)
            try:
                completed_images = set(
                    journal.receipts_by_image("paths")
                )
                pending_plans = [
                    plan
                    for plan in plans
                    if plan.image_id not in completed_images
                ]
                first_batch_number = (
                    journal.last_committed_batch_for_phase("paths") + 1
                )
                for batch_number, batch in enumerate(
                    self._build_batches(pending_plans),
                    first_batch_number,
                ):
                    batch_id = self._batch_id(batch)
                    if batch_id in committed:
                        continue
                    existing = journal.intent_for(batch_id)
                    if existing is not None:
                        self._resume_intent_batch(
                            root, journal, existing, batch
                        )
                        continue
                    prepared = self._prepare_path_files(root, batch)
                    self._sync_staging_devices(prepared)
                    self._publish_batch_and_sync_directories(prepared)
                    self._report_linear_copy_progress(
                        journal, batch, prepared
                    )
                    self._verify_published_identities(root, prepared)
                    pre_signature = self._expected_batch_signature(
                        batch, prepared, use_new=False
                    )
                    post_signature = self._expected_batch_signature(
                        batch, prepared, use_new=True
                    )
                    current = self._current_batch_signature(batch)
                    if current != pre_signature:
                        raise _command_error(
                            "media_migration_database_changed"
                        )
                    receipts = tuple(
                        self._receipt_for_published(
                            item, post_signature
                        )
                        for item in prepared
                    )
                    intent = BatchIntent.for_values(
                        batch_id=batch_id,
                        batch_number=batch_number,
                        phase="paths",
                        first_pk=batch[0].image_id,
                        last_pk=batch[-1].image_id,
                        receipts=receipts,
                        pre_signature=pre_signature,
                        post_signature=post_signature,
                        images=len(batch),
                        files=len(receipts),
                        total_bytes=sum(item.size for item in prepared),
                        total_pixels=sum(
                            item.width * item.height for item in prepared
                        ),
                    )
                    journal.append_intent(intent)
                    self._inject_fault("after_batch_intent")
                    self._inject_fault("after_publish_intent")
                    self._apply_database_batch(
                        batch, prepared, post_signature
                    )
                    self._inject_fault("after_database_commit")
                    journal.append_commit(batch_id, post_signature)
                    self._inject_fault("after_batch_commit")
                    self._report_linear_batch_progress(journal)
            finally:
                root.close()
            self._validate_image_plan_closure(plans)
            phase_summary = self._paths_phase_summary(journal, plans)
            journal.append_phase_complete("paths", phase_summary)
            journal.write_checkpoint()
            self._report_progress({"phase": "finalizing"})
            return self._summary_from_journal(summary, journal)
        except MigrationBatchLogError as error:
            raise _command_error(error.code, error)
        finally:
            if owned_journal and journal is not None:
                journal.close()

    def _upgrade_v2_terminal_prefix(self, manifest, journal, plans):
        terminal = frozenset((
            "committed",
            "recovered_commit",
            "already_current",
        ))
        completed = []
        seen_pending = False
        for plan in plans:
            is_complete = (
                manifest.state.latest_by_image.get(plan.image_id) in terminal
            )
            if is_complete and seen_pending:
                raise _command_error("manifest_plan_mismatch")
            if is_complete:
                completed.append(plan)
            else:
                seen_pending = True
        if not completed:
            return
        self._report_progress({
            "phase": "upgrade_v2",
            "images_done": 0,
            "images_total": len(completed),
        })
        for batch_number, batch in enumerate(
            self._build_batches(completed), 1
        ):
            prepared = []
            for plan in batch:
                for file_plan in plan.files:
                    file_key = self._file_key(plan, file_plan)
                    candidate = self._rehash_resume_candidate(
                        file_plan.new_path, file_key
                    )
                    if (
                        candidate is None
                        or candidate[0] != file_plan.sha256
                        or candidate[1] != file_plan.size
                    ):
                        raise _command_error(
                            "media_verification_failed"
                        )
                    prepared.append(_PreparedFile(
                        plan=plan,
                        file_plan=file_plan,
                        file_key=file_key,
                        staging=None,
                        destination=file_plan.new_path,
                        operation=file_plan.operation,
                        size=file_plan.size,
                        sha256=file_plan.sha256,
                        image_format=file_plan.image_format,
                        width=file_plan.width,
                        height=file_plan.height,
                        source_device=file_plan.source_device,
                        source_inode=file_plan.source_inode,
                        destination_device=candidate[2],
                        destination_inode=candidate[3],
                    ))
            pre_signature = self._expected_batch_signature(
                batch, prepared, use_new=False
            )
            post_signature = self._expected_batch_signature(
                batch, prepared, use_new=True
            )
            if self._current_batch_signature(batch) != post_signature:
                raise _command_error("media_migration_database_changed")
            receipts = tuple(
                self._receipt_for_published(item, post_signature)
                for item in prepared
            )
            intent = BatchIntent.for_values(
                batch_id="upgrade-paths:{}-{}".format(
                    batch[0].image_id, batch[-1].image_id
                ),
                batch_number=batch_number,
                phase="paths",
                first_pk=batch[0].image_id,
                last_pk=batch[-1].image_id,
                receipts=receipts,
                pre_signature=pre_signature,
                post_signature=post_signature,
                images=len(batch),
                files=len(receipts),
                total_bytes=sum(item.size for item in prepared),
                total_pixels=sum(
                    item.width * item.height for item in prepared
                ),
            )
            journal.import_v2_batch(intent, committed=True)
        self._report_progress({
            "phase": "upgrade_v2",
            "images_done": len(completed),
            "images_total": len(completed),
        })

    def _build_batches(self, plans):
        batch = []
        total_bytes = 0
        total_pixels = 0
        for plan in plans:
            plan_bytes = sum(file_plan.size for file_plan in plan.files)
            plan_pixels = sum(
                file_plan.width * file_plan.height
                for file_plan in plan.files
            )
            exceeds = batch and (
                len(batch) + 1 > self.batch_limits.max_images
                or total_bytes + plan_bytes > self.batch_limits.max_bytes
                or total_pixels + plan_pixels > self.batch_limits.max_pixels
            )
            if exceeds:
                yield tuple(batch)
                batch = []
                total_bytes = 0
                total_pixels = 0
            batch.append(plan)
            total_bytes += plan_bytes
            total_pixels += plan_pixels
        if batch:
            yield tuple(batch)

    def _batch_id(self, batch):
        return "paths:{}-{}".format(
            batch[0].image_id, batch[-1].image_id
        )

    def _file_key(self, plan, file_plan):
        if file_plan.thumbnail_id is None:
            return "original:{}".format(plan.image_id)
        return "thumbnail:{}:{}".format(
            plan.image_id, file_plan.thumbnail_id
        )

    def _staging_name(self, file_key):
        value = uuid.uuid5(
            uuid.NAMESPACE_URL,
            "{}:{}".format(self.run_id, file_key),
        )
        return "auto-v2-{}.part".format(value)

    def _prepare_path_files(self, root, batch):
        prepared = []
        try:
            for plan in batch:
                for file_plan in plan.files:
                    file_key = self._file_key(plan, file_plan)
                    prepared.append(self._prepare_linear_file(
                        root, plan, file_plan, file_key
                    ))
            return prepared
        except BaseException:
            for item in prepared:
                self._discard_prepared(item)
            raise

    def _prepare_linear_file(self, root, plan, file_plan, file_key):
        source = None
        staging_directory = None
        staging = None
        try:
            source = open_verified_media_file(root, file_plan.old_path)
            source_stat = source.file_stat
            expected_identity = (
                file_plan.source_device,
                file_plan.source_inode,
            )
            if (
                (source_stat.st_dev, source_stat.st_ino)
                != expected_identity
                or source_stat.st_size != file_plan.size
            ):
                raise _command_error("media_verification_failed")
            staging_directory = open_or_create_media_directory_from(
                root, ".staging"
            )
            staging = create_owned_staging_file(
                staging_directory, self._staging_name(file_key)
            )
            prepared = self._stream_source_to_staging_and_inspect(
                source, staging, file_key
            )
            if (
                prepared[1] != file_plan.size
                or prepared[3] != file_plan.width
                or prepared[4] != file_plan.height
            ):
                raise _command_error("invalid_legacy_media")
            extension = FORMAT_EXTENSIONS.get(prepared[2])
            if extension is None:
                raise _command_error("invalid_legacy_media")
            destination = (
                canonical_original_path(
                    plan.asset_uuid,
                    plan.original_filename,
                    extension,
                )
                if file_plan.thumbnail_id is None
                else canonical_derivative_path(
                    plan.asset_uuid,
                    file_plan.derivative_size,
                    extension,
                )
            )
            source.verify_current()
            self._inject_fault("after_source_revalidation")
            root.verify_current()
            current_source = os.fstat(source.descriptor)
            if (
                current_source.st_dev,
                current_source.st_ino,
                current_source.st_size,
            ) != (
                source_stat.st_dev,
                source_stat.st_ino,
                source_stat.st_size,
            ):
                raise _command_error("media_verification_failed")
            item = _PreparedFile(
                plan=plan,
                file_plan=file_plan,
                file_key=file_key,
                staging=staging,
                destination=destination,
                operation=(
                    "verify"
                    if file_plan.old_path == destination
                    else "copy"
                ),
                size=prepared[1],
                sha256=prepared[0],
                image_format=prepared[2],
                width=prepared[3],
                height=prepared[4],
                source_device=source_stat.st_dev,
                source_inode=source_stat.st_ino,
            )
            staging = None
            staging_directory = None
            return item
        except (MediaPathError, OSError) as error:
            raise _command_error("media_verification_failed", error)
        finally:
            if source is not None:
                source.close()
            if staging is not None:
                try:
                    staging.cleanup()
                finally:
                    staging.close()
            if staging_directory is not None:
                staging_directory.close()

    def _open_source_descriptor(self, source, file_key):
        del file_key
        source.verify_current()
        return os.dup(source.descriptor)

    def _iter_source_chunks(self, source_fd, file_key):
        del file_key
        os.lseek(source_fd, 0, os.SEEK_SET)
        while True:
            chunk = os.read(source_fd, 64 * 1024)
            if not chunk:
                return
            yield chunk

    def _stream_source_to_staging_and_inspect(
        self, source, staging, file_key
    ):
        source_fd = self._open_source_descriptor(source, file_key)
        parser = ImageFile.Parser()
        digest = hashlib.sha256()
        size = 0
        try:
            os.ftruncate(staging.descriptor, 0)
            os.lseek(staging.descriptor, 0, os.SEEK_SET)
            for chunk in self._iter_source_chunks(source_fd, file_key):
                digest.update(chunk)
                parser.feed(chunk)
                size += len(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(staging.descriptor, view)
                    if written <= 0:
                        raise OSError("short staging write")
                    view = view[written:]
            with warnings.catch_warnings():
                warnings.simplefilter(
                    "ignore", PILImage.DecompressionBombWarning
                )
                image = parser.close()
            try:
                image_format = image.format
                width, height = image.size
            finally:
                image.close()
            try:
                os.fchmod(staging.descriptor, 0o600)
                os.fchown(
                    staging.descriptor,
                    self.service_uid,
                    self.service_gid,
                )
            except PermissionError:
                if (
                    os.geteuid() != self.service_uid
                    or os.getegid() != self.service_gid
                ):
                    raise
            staging.file_stat = os.fstat(staging.descriptor)
            return digest.hexdigest(), size, image_format, width, height
        except (
            PILImage.DecompressionBombError,
            PILImage.UnidentifiedImageError,
            OSError,
            Warning,
        ) as error:
            raise _command_error("invalid_legacy_media", error)
        finally:
            os.close(source_fd)

    def _sync_staging_devices(self, prepared):
        by_device = {}
        for item in prepared:
            current = os.fstat(item.staging.descriptor)
            by_device.setdefault(current.st_dev, item.staging.descriptor)
        for device in sorted(by_device):
            _durable_syncfs(by_device[device], "batch_file_data")

    def _publish_batch_and_sync_directories(self, prepared):
        mutated = {}
        opened_destinations = []
        try:
            for item in prepared:
                staging_stat = os.fstat(item.staging.descriptor)
                expected = (
                    staging_stat.st_dev,
                    staging_stat.st_ino,
                    staging_stat.st_size,
                )
                if item.operation == "verify":
                    current = os.stat(
                        item.staging.name,
                        dir_fd=item.staging.directory.descriptor,
                        follow_symlinks=False,
                    )
                    if (
                        current.st_dev,
                        current.st_ino,
                        current.st_size,
                    ) != expected:
                        raise _command_error("media_verification_failed")
                    os.unlink(
                        item.staging.name,
                        dir_fd=item.staging.directory.descriptor,
                    )
                    item.destination_device = item.source_device
                    item.destination_inode = item.source_inode
                    directory_stat = os.fstat(
                        item.staging.directory.descriptor
                    )
                    mutated[
                        (directory_stat.st_dev, directory_stat.st_ino)
                    ] = item.staging.directory
                else:
                    relative_directory, destination_name = (
                        item.destination.rsplit("/", 1)
                    )
                    destination_directory = (
                        open_or_create_media_directory_from(
                            item.staging.directory.anchor_directory,
                            relative_directory,
                        )
                    )
                    opened_destinations.append(destination_directory)
                    try:
                        self._inject_fault("before_atomic_publish")
                        result = publish_preverified_noreplace(
                            item.staging,
                            destination_directory,
                            destination_name,
                            expected,
                        )
                    except FileExistsError as error:
                        if not self._resume_attempt:
                            raise _command_error(
                                "media_path_conflict", error
                            )
                        candidate = self._rehash_resume_candidate(
                            item.destination, item.file_key
                        )
                        if (
                            candidate is None
                            or candidate[0] != item.sha256
                            or candidate[1] != item.size
                        ):
                            raise _command_error(
                                "media_path_conflict", error
                            )
                        current = os.stat(
                            item.staging.name,
                            dir_fd=item.staging.directory.descriptor,
                            follow_symlinks=False,
                        )
                        if (
                            current.st_dev,
                            current.st_ino,
                            current.st_size,
                        ) != expected:
                            raise _command_error(
                                "media_verification_failed"
                            )
                        os.unlink(
                            item.staging.name,
                            dir_fd=item.staging.directory.descriptor,
                        )
                        item.destination_device = candidate[2]
                        item.destination_inode = candidate[3]
                        directory_stat = os.fstat(
                            item.staging.directory.descriptor
                        )
                        mutated[
                            (
                                directory_stat.st_dev,
                                directory_stat.st_ino,
                            )
                        ] = item.staging.directory
                        continue
                    except (MediaPathError, OSError) as error:
                        raise _command_error(
                            "media_verification_failed", error
                        )
                    item.destination_device = (
                        result.destination_stat.st_dev
                    )
                    item.destination_inode = (
                        result.destination_stat.st_ino
                    )
                    for directory in result.mutated_directories:
                        directory_stat = os.fstat(directory.descriptor)
                        mutated[
                            (directory_stat.st_dev, directory_stat.st_ino)
                        ] = directory
            for identity in sorted(mutated):
                _durable_fsync(
                    mutated[identity].descriptor,
                    "publication_directory",
                )
            self._inject_fault("after_destination_rename")
            self._inject_fault("after_publish")
            self._inject_fault("after_publish_before_event")
            return prepared
        finally:
            for item in prepared:
                item.staging.close()
            for directory in opened_destinations:
                directory.close()
            closed = set()
            for item in prepared:
                directory = item.staging.directory
                if id(directory) not in closed:
                    closed.add(id(directory))
                    directory.close()

    def _discard_prepared(self, item):
        staging = item.staging
        try:
            if not staging._closed:
                staging.cleanup()
                staging.close()
        except BaseException:
            pass
        try:
            staging.directory.close()
        except BaseException:
            pass

    def _receipt_for_published(self, prepared, database_signature):
        return FileReceipt.for_values(
            prepared.file_key,
            prepared.destination,
            prepared.operation,
            prepared.size,
            prepared.image_format,
            prepared.width,
            prepared.height,
            prepared.source_device,
            prepared.source_inode,
            prepared.destination_device,
            prepared.destination_inode,
            prepared.sha256,
            database_signature,
        )

    def _signature_rows(self, batch, path_by_key):
        rows = []
        for plan in batch:
            rows.append((
                "image",
                plan.image_id,
                plan.asset_uuid,
                plan.original_filename,
                plan.image_width,
                plan.image_height,
                path_by_key["original:{}".format(plan.image_id)],
            ))
            for file_plan in plan.files[1:]:
                rows.append((
                    "thumbnail",
                    file_plan.thumbnail_id,
                    plan.image_id,
                    file_plan.derivative_size,
                    file_plan.width,
                    file_plan.height,
                    path_by_key[self._file_key(plan, file_plan)],
                ))
        return rows

    def _hash_signature_rows(self, rows):
        return hashlib.sha256(json.dumps(
            rows,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()

    def _expected_batch_signature(self, batch, prepared, use_new):
        by_key = {
            item.file_key: (
                item.destination if use_new else item.file_plan.old_path
            )
            for item in prepared
        }
        return self._hash_signature_rows(
            self._signature_rows(batch, by_key)
        )

    def _current_batch_signature(self, batch, lock=False):
        image_ids = [plan.image_id for plan in batch]
        images = Image.objects.filter(pk__in=image_ids).order_by("pk")
        thumbnails = Thumbnail.objects.filter(
            original_id__in=image_ids
        ).order_by("original_id", "size", "pk")
        if lock:
            images = images.select_for_update()
            thumbnails = thumbnails.select_for_update()
        path_by_key = {}
        current_images = list(images)
        current_thumbnails = list(thumbnails)
        if [image.pk for image in current_images] != image_ids:
            return "changed"
        for image in current_images:
            path_by_key["original:{}".format(image.pk)] = image.image.name
        for record in current_thumbnails:
            path_by_key["thumbnail:{}:{}".format(
                record.original_id, record.pk
            )] = record.image.name
        try:
            rows = self._signature_rows(batch, path_by_key)
        except KeyError:
            return "changed"
        metadata_rows = []
        by_image = {image.pk: image for image in current_images}
        by_thumbnail = {
            thumbnail.pk: thumbnail for thumbnail in current_thumbnails
        }
        for row in rows:
            if row[0] == "image":
                image = by_image[row[1]]
                metadata_rows.append((
                    "image", image.pk, str(image.asset_uuid),
                    image.original_filename, image.width, image.height,
                    image.image.name,
                ))
            else:
                thumbnail = by_thumbnail[row[1]]
                metadata_rows.append((
                    "thumbnail", thumbnail.pk, thumbnail.original_id,
                    thumbnail.size, thumbnail.width, thumbnail.height,
                    thumbnail.image.name,
                ))
        return self._hash_signature_rows(metadata_rows)

    def _apply_database_batch(self, batch, prepared, post_signature):
        destinations = {
            item.file_key: item.destination for item in prepared
        }
        with transaction.atomic():
            if self._current_batch_signature(batch, lock=True) != (
                self._expected_batch_signature(
                    batch, prepared, use_new=False
                )
            ):
                raise _command_error("media_migration_database_changed")
            self._inject_fault("before_database_update")
            root = None
            try:
                root = open_verified_media_root(settings.MEDIA_ROOT)
                self._verify_published_identities(root, prepared)
            finally:
                if root is not None:
                    root.close()
            for plan in batch:
                Image.objects.filter(pk=plan.image_id).update(
                    image=destinations[
                        "original:{}".format(plan.image_id)
                    ]
                )
                for file_plan in plan.files[1:]:
                    Thumbnail.objects.filter(
                        pk=file_plan.thumbnail_id,
                        original_id=plan.image_id,
                    ).update(
                        image=destinations[
                            self._file_key(plan, file_plan)
                        ]
                    )
            if self._current_batch_signature(batch, lock=True) != (
                post_signature
            ):
                raise _command_error("media_migration_database_changed")

    def _verify_published_identities(self, root, prepared):
        for item in prepared:
            destination = None
            try:
                destination = open_verified_media_file(
                    root, item.destination
                )
                current = destination.file_stat
                if (
                    current.st_dev != item.destination_device
                    or current.st_ino != item.destination_inode
                    or current.st_size != item.size
                ):
                    raise _command_error("media_verification_failed")
                destination.verify_current()
            except CommandError:
                raise
            except (MediaPathError, OSError) as error:
                raise _command_error("media_verification_failed", error)
            finally:
                if destination is not None:
                    destination.close()

    def _resume_intent_batch(self, root, journal, intent, batch):
        self._verify_and_repair_batch(
            root, journal, intent.batch_id, batch
        )
        receipts = journal.effective_receipts(intent.batch_id)
        recovery = journal.recover_batch(
            intent.batch_id, self._current_batch_signature(batch)
        )
        if recovery == "append_commit":
            journal.append_commit(intent.batch_id, intent.post_signature)
            return
        if recovery == "committed":
            return
        prepared = [
            self._prepared_from_receipt(batch, receipt)
            for receipt in receipts
        ]
        self._apply_database_batch(batch, prepared, intent.post_signature)
        self._inject_fault("after_database_commit")
        journal.append_commit(intent.batch_id, intent.post_signature)

    def _prepared_from_receipt(self, batch, receipt):
        by_key = {
            self._file_key(plan, file_plan): (plan, file_plan)
            for plan in batch
            for file_plan in plan.files
        }
        try:
            plan, file_plan = by_key[receipt.file_key]
        except KeyError:
            raise _command_error("linear_journal_batch_conflict")
        return _PreparedFile(
            plan=plan,
            file_plan=file_plan,
            file_key=receipt.file_key,
            staging=None,
            destination=receipt.relative_path,
            operation=receipt.operation,
            size=receipt.size,
            sha256=receipt.sha256,
            image_format=receipt.image_format,
            width=receipt.width,
            height=receipt.height,
            source_device=receipt.source_device,
            source_inode=receipt.source_inode,
            destination_device=receipt.destination_device,
            destination_inode=receipt.destination_inode,
        )

    def _verify_committed_destinations(self, journal, plans):
        by_id = {plan.image_id: plan for plan in plans}
        repaired = False
        root = None
        try:
            if journal.committed_ids("paths"):
                root = open_verified_media_root(settings.MEDIA_ROOT)
            for batch_id in journal.committed_ids("paths"):
                intent = journal.intent_for(batch_id)
                batch = tuple(
                    by_id[image_id]
                    for image_id in range(
                        intent.first_pk, intent.last_pk + 1
                    )
                    if image_id in by_id
                )
                repaired = self._verify_and_repair_batch(
                    root, journal, batch_id, batch
                ) or repaired
            return repaired
        finally:
            if root is not None:
                root.close()

    def _verify_and_repair_batch(self, root, journal, batch_id, batch):
        receipts = journal.effective_receipts(batch_id)
        by_key = {
            self._file_key(plan, file_plan): (plan, file_plan)
            for plan in batch
            for file_plan in plan.files
        }
        repaired = []
        changed = False
        for receipt in receipts:
            planned = by_key.get(receipt.file_key)
            if planned is None:
                raise _command_error("linear_journal_batch_conflict")
            candidate = self._rehash_resume_candidate(
                receipt.relative_path, receipt.file_key
            )
            if (
                candidate is not None
                and candidate[0] == receipt.sha256
                and candidate[1] == receipt.size
            ):
                if candidate[2:] == (
                    receipt.destination_device,
                    receipt.destination_inode,
                ):
                    repaired.append(receipt)
                    continue
                self._require_unchanged_source(
                    root, planned[1], receipt
                )
                repaired.append(self._receipt_with_destination_identity(
                    receipt, candidate[2], candidate[3]
                ))
                changed = True
                continue
            repaired.append(self._repair_destination_from_source(
                root, planned[0], planned[1], receipt
            ))
            changed = True
        if changed:
            try:
                journal.append_repair(batch_id, tuple(repaired))
            except BaseException as error:
                if not isinstance(error, Exception):
                    raise
                raise _command_error(
                    "linear_committed_repair_failed", error
                )
        return changed

    def _require_unchanged_source(self, root, file_plan, receipt):
        source = None
        try:
            source = open_verified_media_file(root, file_plan.old_path)
            current = source.file_stat
            if (
                current.st_dev != receipt.source_device
                or current.st_ino != receipt.source_inode
                or current.st_size != receipt.size
            ):
                raise _command_error("linear_committed_source_changed")
            source.verify_current()
        except CommandError:
            raise
        except (MediaPathError, OSError) as error:
            raise _command_error("linear_committed_source_changed", error)
        finally:
            if source is not None:
                source.close()

    def _repair_destination_from_source(
        self, root, plan, file_plan, receipt
    ):
        prepared = None
        destination_directory = None
        try:
            self._require_unchanged_source(root, file_plan, receipt)
            prepared = self._prepare_linear_file(
                root, plan, file_plan, receipt.file_key
            )
            if (
                prepared.destination != receipt.relative_path
                or prepared.operation != receipt.operation
                or prepared.size != receipt.size
                or prepared.sha256 != receipt.sha256
                or prepared.image_format != receipt.image_format
                or prepared.width != receipt.width
                or prepared.height != receipt.height
            ):
                raise _command_error("linear_committed_source_changed")
            self._sync_staging_devices((prepared,))
            relative_directory, destination_name = (
                receipt.relative_path.rsplit("/", 1)
            )
            destination_directory = open_or_create_media_directory_from(
                root, relative_directory
            )
            staging_stat = os.fstat(prepared.staging.descriptor)
            result = replace_preverified_destination(
                prepared.staging,
                destination_directory,
                destination_name,
                (
                    staging_stat.st_dev,
                    staging_stat.st_ino,
                    staging_stat.st_size,
                ),
            )
            directories = {
                identity: directory
                for identity, directory in zip(
                    result.mutated_directory_identities,
                    result.mutated_directories,
                )
            }
            for identity in sorted(directories):
                _durable_fsync(
                    directories[identity].descriptor,
                    "publication_directory",
                )
            self._inject_fault("after_repair_destination_fsync")
            return self._receipt_with_destination_identity(
                receipt,
                result.destination_stat.st_dev,
                result.destination_stat.st_ino,
            )
        except CommandError:
            raise
        except BaseException as error:
            if not isinstance(error, Exception):
                raise
            raise _command_error("linear_committed_repair_failed", error)
        finally:
            if prepared is not None:
                prepared.staging.close()
                prepared.staging.directory.close()
            if destination_directory is not None:
                destination_directory.close()

    def _receipt_with_destination_identity(
        self, receipt, destination_device, destination_inode
    ):
        return FileReceipt.for_values(
            receipt.file_key,
            receipt.relative_path,
            receipt.operation,
            receipt.size,
            receipt.image_format,
            receipt.width,
            receipt.height,
            receipt.source_device,
            receipt.source_inode,
            destination_device,
            destination_inode,
            receipt.sha256,
            receipt.database_signature,
        )

    def _rehash_resume_candidate(self, path, file_key):
        root = open_verified_media_root(settings.MEDIA_ROOT)
        receipt = None
        try:
            receipt = open_verified_media_file(root, path, missing_ok=True)
            if receipt is None:
                return None
            descriptor = os.dup(receipt.descriptor)
            try:
                digest = hashlib.sha256()
                size = 0
                os.lseek(descriptor, 0, os.SEEK_SET)
                while True:
                    chunk = os.read(descriptor, 64 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    size += len(chunk)
            finally:
                os.close(descriptor)
            receipt.verify_current()
            return (
                digest.hexdigest(),
                size,
                receipt.file_stat.st_dev,
                receipt.file_stat.st_ino,
            )
        except (MediaPathError, OSError) as error:
            raise _command_error("linear_committed_repair_failed", error)
        finally:
            if receipt is not None:
                receipt.close()
            root.close()

    def _paths_phase_summary(self, journal, plans):
        counts = {
            "md5_legacy": 0,
            "fixed_slot": 0,
            "named_canonical": 0,
        }
        copy_required_bytes = 0
        receipts_by_image = journal.receipts_by_image("paths")
        for plan in plans:
            completed = self._completed_plan_from_receipts(
                plan, receipts_by_image.get(plan.image_id, ())
            )
            counts[completed.generation] += 1
            copy_required_bytes += completed.copy_required_bytes
        return {
            "image_count": len(plans),
            "md5_legacy": counts["md5_legacy"],
            "fixed_slot": counts["fixed_slot"],
            "named_canonical": counts["named_canonical"],
            "copy_required_bytes": copy_required_bytes,
        }

    def _completed_plan_from_receipts(self, plan, receipts):
        by_key = {receipt.file_key: receipt for receipt in receipts}
        files = []
        for file_plan in plan.files:
            file_key = self._file_key(plan, file_plan)
            receipt = by_key.get(file_key)
            if receipt is None:
                raise _command_error("auto_v2_plan_incomplete")
            files.append(AutoV2MigrationFile(
                kind=file_plan.kind,
                old_path=file_plan.old_path,
                new_path=receipt.relative_path,
                operation=receipt.operation,
                size=receipt.size,
                sha256=receipt.sha256,
                image_format=receipt.image_format,
                width=receipt.width,
                height=receipt.height,
                source_device=receipt.source_device,
                source_inode=receipt.source_inode,
                thumbnail_id=file_plan.thumbnail_id,
                derivative_size=file_plan.derivative_size,
                archive_root_device=file_plan.archive_root_device,
                archive_root_inode=file_plan.archive_root_inode,
            ))
        generation = _classify_generation_for_uuid(plan.asset_uuid, files)
        return AutoV2MigrationPlan(
            image_id=plan.image_id,
            asset_uuid=plan.asset_uuid,
            original_filename=plan.original_filename,
            image_width=plan.image_width,
            image_height=plan.image_height,
            generation=generation,
            files=tuple(files),
            thumbnail_rows=plan.thumbnail_rows,
            copy_required_bytes=sum(
                file_plan.size
                for file_plan in files
                if file_plan.operation == "copy"
            ),
        )

    def _summary_from_journal(self, summary, journal):
        phase = journal.state.phase_summaries.get("paths")
        if phase is None:
            return summary
        return AutoV2PlanSummary(
            run_id=summary.run_id,
            plan_sha256=summary.plan_sha256,
            manifest_sha256=summary.manifest_sha256,
            image_count=phase["image_count"],
            md5_legacy=phase["md5_legacy"],
            fixed_slot=phase["fixed_slot"],
            named_canonical=phase["named_canonical"],
            copy_required_bytes=phase["copy_required_bytes"],
        )

    def _report_linear_batch_progress(self, journal):
        snapshot = journal.recovery_snapshot()
        self._report_progress({
            "phase": "database",
            "images_done": snapshot["images_done"],
            "images_total": snapshot["images_total"],
        })

    def _report_linear_copy_progress(self, journal, batch, prepared):
        snapshot = journal.recovery_snapshot()
        self._report_progress({
            "phase": "copying",
            "images_done": snapshot["images_done"] + len(batch),
            "images_total": snapshot["images_total"],
            "files_done": snapshot["files_done"] + len(prepared),
            "files_total": snapshot["files_total"],
        })

    def _execute(self, manifest, plans, plan_sha256):  # noqa: C901
        if manifest.summary().plan_sha256 != plan_sha256:
            raise _command_error("manifest_plan_mismatch")
        self._validate_image_plan_closure(plans)
        pending = []
        source_verifications = []
        destination_verifications = []
        already_current = []
        recovered = []
        images_total = len(plans)
        files_total = sum(len(plan.files) for plan in plans)
        images_done = 0
        files_done = 0
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
                images_done += 1
                files_done += len(plan.files)
                self._report_copying(
                    images_done,
                    images_total,
                    files_done,
                    files_total,
                )
            for plan in destination_verifications:
                self._verify_plan_destinations_from(
                    root_directory,
                    plan,
                    self._destination_identities(manifest, plan),
                )
                images_done += 1
                files_done += len(plan.files)
                self._report_copying(
                    images_done,
                    images_total,
                    files_done,
                    files_total,
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
                images_done += 1
                files_done += len(plan.files)
                self._report_copying(
                    images_done,
                    images_total,
                    files_done,
                    files_total,
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
        if not plans:
            self._report_copying(0, 0, 0, 0)

        if pending:
            self._inject_fault("before_database_transaction")
            self._commit_batches(manifest, pending)
        else:
            self._report_database_progress(manifest, plans)

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

    def _verify_named_staging_object(
        self,
        staging_directory,
        staging_name,
        descriptor,
        expected_identity,
        expected_link_count,
        expected_size=None,
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
            ) != expected_identity
            or (
                descriptor_stat.st_dev,
                descriptor_stat.st_ino,
            ) != expected_identity
            or named_stat.st_nlink != expected_link_count
            or descriptor_stat.st_nlink != expected_link_count
            or (
                expected_size is not None
                and (
                    named_stat.st_size != expected_size
                    or descriptor_stat.st_size != expected_size
                )
            )
        ):
            raise _command_error("media_verification_failed")
        return named_stat, descriptor_stat

    def _verify_intent_staging_identity(
        self,
        staging_directory,
        staging_name,
        descriptor,
        expected_identity,
        expected_link_count,
        expected_size=None,
    ):
        named_stat, descriptor_stat = self._verify_named_staging_object(
            staging_directory,
            staging_name,
            descriptor,
            expected_identity,
            expected_link_count,
            expected_size=expected_size,
        )
        for current_stat in (named_stat, descriptor_stat):
            if (
                current_stat.st_uid != self.service_uid
                or current_stat.st_gid != self.service_gid
                or stat.S_IMODE(current_stat.st_mode) != 0o600
            ):
                raise _command_error("media_verification_failed")
        return descriptor_stat

    def _set_new_staging_service_identity(
        self,
        staging_directory,
        staging_name,
        descriptor,
        expected_identity,
    ):
        named_stat, descriptor_stat = self._verify_named_staging_object(
            staging_directory,
            staging_name,
            descriptor,
            expected_identity,
            1,
            expected_size=0,
        )
        effective_uid = os.geteuid()
        if (
            effective_uid not in (0, self.service_uid)
            or named_stat.st_uid != effective_uid
            or descriptor_stat.st_uid != effective_uid
            or named_stat.st_gid != descriptor_stat.st_gid
        ):
            raise _command_error("media_verification_failed")
        try:
            os.fchmod(descriptor, 0o600)
            os.fchown(descriptor, self.service_uid, self.service_gid)
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
            os.fsync(staging_directory.descriptor)
        except OSError as error:
            raise _command_error("media_verification_failed", error)
        return self._verify_intent_staging_identity(
            staging_directory,
            staging_name,
            descriptor,
            expected_identity,
            1,
            expected_size=0,
        )

    def _recover_root_staging_service_identity(
        self,
        staging_directory,
        staging_name,
        descriptor,
        publish_intent,
        file_plan,
        expected_link_count,
        destination_directory,
        destination_name,
    ):
        named_stat, descriptor_stat = self._verify_named_staging_object(
            staging_directory,
            staging_name,
            descriptor,
            publish_intent[:2],
            expected_link_count,
            expected_size=file_plan.size,
        )
        expected_metadata = (
            self.service_uid,
            self.service_gid,
            0o600,
        )
        named_metadata = (
            named_stat.st_uid,
            named_stat.st_gid,
            stat.S_IMODE(named_stat.st_mode),
        )
        descriptor_metadata = (
            descriptor_stat.st_uid,
            descriptor_stat.st_gid,
            stat.S_IMODE(descriptor_stat.st_mode),
        )
        if (
            named_metadata == expected_metadata
            and descriptor_metadata == expected_metadata
        ):
            return descriptor_stat
        if (
            os.geteuid() != 0
            or named_metadata != descriptor_metadata
            or named_metadata != (0, 0, 0o600)
        ):
            raise _command_error("media_verification_failed")
        try:
            destination_directory.verify_current()
            try:
                os.stat(
                    destination_name,
                    dir_fd=destination_directory.descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                raise _command_error("destination_collision")
        except CommandError:
            raise
        except (MediaPathError, OSError) as error:
            raise _command_error("media_verification_failed", error)
        try:
            os.fchown(descriptor, self.service_uid, self.service_gid)
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
            os.fsync(staging_directory.descriptor)
        except OSError as error:
            raise _command_error("media_verification_failed", error)
        descriptor_stat = self._verify_intent_staging_identity(
            staging_directory,
            staging_name,
            descriptor,
            publish_intent[:2],
            expected_link_count,
            expected_size=file_plan.size,
        )
        _verify_staging(descriptor, file_plan)
        return descriptor_stat

    def _open_intent_staging(
        self,
        staging_directory,
        publish_intent,
        file_plan,
        expected_link_count,
        destination_directory,
        destination_name,
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
            self._verify_named_staging_object(
                staging_directory,
                publish_intent[4],
                descriptor,
                publish_intent[:2],
                expected_link_count,
                expected_size=file_plan.size,
            )
            _verify_staging(descriptor, file_plan)
            self._verify_named_staging_object(
                staging_directory,
                publish_intent[4],
                descriptor,
                publish_intent[:2],
                expected_link_count,
                expected_size=file_plan.size,
            )
            self._recover_root_staging_service_identity(
                staging_directory,
                publish_intent[4],
                descriptor,
                publish_intent,
                file_plan,
                expected_link_count,
                destination_directory,
                destination_name,
            )
            file_stat = self._verify_intent_staging_identity(
                staging_directory,
                publish_intent[4],
                descriptor,
                publish_intent[:2],
                expected_link_count,
                expected_size=file_plan.size,
            )
            _verify_staging(descriptor, file_plan)
            self._verify_intent_staging_identity(
                staging_directory,
                publish_intent[4],
                descriptor,
                publish_intent[:2],
                expected_link_count,
                expected_size=file_plan.size,
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
        staging_directory,
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
            os.fsync(destination.descriptor)
            destination.parent_directory.fsync_publish()
            staging_directory.fsync_publish()
        finally:
            destination.close()
        manifest.record_published(
            image_id,
            file_plan.kind_key,
            publish_intent[:2],
        )

    def _verify_atomic_publish_identity(
        self,
        staging_directory,
        staging_name,
        staging_descriptor,
        destination_directory,
        destination_name,
        expected_identity,
        file_plan,
    ):
        try:
            os.stat(
                staging_name,
                dir_fd=staging_directory.descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            raise _command_error("media_verification_failed")
        destination_stat = os.stat(
            destination_name,
            dir_fd=destination_directory.descriptor,
            follow_symlinks=False,
        )
        descriptor_stat = os.fstat(staging_descriptor)
        if (
            not stat.S_ISREG(destination_stat.st_mode)
            or not stat.S_ISREG(descriptor_stat.st_mode)
            or (
                destination_stat.st_dev,
                destination_stat.st_ino,
            ) != expected_identity
            or (
                descriptor_stat.st_dev,
                descriptor_stat.st_ino,
            ) != expected_identity
            or destination_stat.st_nlink != 1
            or descriptor_stat.st_nlink != 1
            or destination_stat.st_uid != self.service_uid
            or destination_stat.st_gid != self.service_gid
            or descriptor_stat.st_uid != self.service_uid
            or descriptor_stat.st_gid != self.service_gid
            or stat.S_IMODE(destination_stat.st_mode) != 0o600
            or stat.S_IMODE(descriptor_stat.st_mode) != 0o600
        ):
            raise _command_error("media_verification_failed")
        _verify_staging(staging_descriptor, file_plan)
        return destination_stat

    def _atomic_publish_staging(
        self,
        staging_directory,
        staging_name,
        staging_descriptor,
        destination_directory,
        destination_name,
        publish_intent,
        file_plan,
    ):
        self._verify_intent_staging_identity(
            staging_directory,
            staging_name,
            staging_descriptor,
            publish_intent[:2],
            1,
            expected_size=file_plan.size,
        )
        destination_directory.verify_current()
        try:
            os.stat(
                destination_name,
                dir_fd=destination_directory.descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            raise _command_error("destination_collision")

        try:
            self._inject_fault("before_atomic_publish")
            rename_media_noreplace(
                staging_directory,
                staging_name,
                destination_directory,
                destination_name,
            )
            destination_stat = self._verify_atomic_publish_identity(
                staging_directory,
                staging_name,
                staging_descriptor,
                destination_directory,
                destination_name,
                publish_intent[:2],
                file_plan,
            )
            os.fsync(staging_descriptor)
            destination_directory.verify_current()
            staging_directory.verify_current()
            destination_directory.fsync_publish()
            staging_directory.fsync_publish()
            return destination_stat
        except FileExistsError as error:
            raise _command_error("destination_collision", error)

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
                    destination_directory,
                    destination_name,
                )
                self._atomic_publish_staging(
                    staging_directory,
                    publish_intent[4],
                    staging_file.descriptor,
                    destination_directory,
                    destination_name,
                    publish_intent,
                    file_plan,
                )
            else:
                if (
                    not stat.S_ISREG(destination_stat.st_mode)
                    or (
                        destination_stat.st_dev,
                        destination_stat.st_ino,
                    ) != publish_intent[:2]
                    or destination_stat.st_nlink != 1
                    or destination_stat.st_uid != self.service_uid
                    or destination_stat.st_gid != self.service_gid
                    or stat.S_IMODE(destination_stat.st_mode) != 0o600
                ):
                    raise _command_error("destination_collision")
                try:
                    os.stat(
                        publish_intent[4],
                        dir_fd=staging_directory.descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    pass
                else:
                    raise _command_error("destination_collision")

            self._record_intent_destination(
                root_directory,
                manifest,
                image_id,
                file_plan,
                publish_intent,
                staging_directory,
            )
        except CommandError:
            raise
        except (MediaPathError, OSError) as error:
            if isinstance(error, FileExistsError):
                raise _command_error("destination_collision", error)
            raise _command_error("media_verification_failed", error)
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
        try:
            staging_directory = open_or_create_media_directory_from(
                root_directory, ".staging"
            )
            staging = create_owned_staging_file(
                staging_directory,
                "auto-v2-{}.part".format(uuid.uuid4()),
            )
            staging.file_stat = self._set_new_staging_service_identity(
                staging_directory,
                staging.name,
                staging.descriptor,
                (
                    staging.file_stat.st_dev,
                    staging.file_stat.st_ino,
                ),
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
            staging_stat = os.fstat(staging.descriptor)
            self._verify_intent_staging_identity(
                staging_directory,
                staging.name,
                staging.descriptor,
                (staging_stat.st_dev, staging_stat.st_ino),
                1,
                expected_size=file_plan.size,
            )
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
            publish_intent = (
                staging_stat.st_dev,
                staging_stat.st_ino,
                destination_parent_stat.st_dev,
                destination_parent_stat.st_ino,
                staging.name,
            )
            manifest.record_publish_intent(
                image_id,
                file_plan.kind_key,
                staging.name,
                publish_intent[:2],
                publish_intent[2:4],
            )
            intent_recorded = True
            self._inject_fault("after_publish_intent")
            self._atomic_publish_staging(
                staging_directory,
                staging.name,
                staging.descriptor,
                destination_directory,
                destination_name,
                publish_intent,
                file_plan,
            )
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
                    expected_identity=publish_intent[:2],
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
                if not intent_recorded:
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
                self._report_database_progress(manifest, all_plans)
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

    def _report_planning(self, plans):
        if self._planning_reported:
            return
        self._planning_reported = True
        self._report_progress(
            {
                "phase": "planning",
                "images_total": len(plans),
                "files_total": sum(len(plan.files) for plan in plans),
            }
        )

    def _report_copying(
        self,
        images_done,
        images_total,
        files_done,
        files_total,
    ):
        self._report_progress(
            {
                "phase": "copying",
                "images_done": images_done,
                "images_total": images_total,
                "files_done": files_done,
                "files_total": files_total,
            }
        )

    def _report_database_progress(self, manifest, plans):
        terminal = ("committed", "recovered_commit", "already_current")
        images_done = sum(
            manifest.state.latest_by_image.get(plan.image_id) in terminal
            for plan in plans
        )
        self._report_progress(
            {
                "phase": "database",
                "images_done": images_done,
                "images_total": len(plans),
            }
        )

    def _report_progress(self, event):
        if self.progress_reporter is None:
            return False
        try:
            self.progress_reporter(dict(event))
        except Exception:
            return False
        return True


def _verify_staging(descriptor, file_plan):
    file_stat = os.fstat(descriptor)
    if (
        file_stat.st_size != file_plan.size
        or sha256_file_descriptor(descriptor) != file_plan.sha256
    ):
        raise _command_error("media_verification_failed")
