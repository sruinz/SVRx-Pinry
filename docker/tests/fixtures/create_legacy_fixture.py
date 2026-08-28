#!/usr/bin/env python
"""SVRx Pinry 자동 레거시 이관 container smoke 보조 CLI."""

from __future__ import print_function

import argparse
import ast
import collections
from contextlib import ExitStack
from contextvars import ContextVar
import datetime
import errno
import fcntl
from functools import wraps
from io import BytesIO
import hashlib
import glob
import inspect
import json
import math
import os
from pathlib import Path
import pwd
import re
import resource
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
import uuid
from unittest import mock
from urllib.parse import quote, urlsplit


SUCCESS = {
    "configure-settings": "FIXTURE_SETTINGS_OK",
    "write-http-fixture": "FIXTURE_HTTP_IMAGE_OK",
    "create": "FIXTURE_CREATE_OK",
    "verify-migration": "FIXTURE_VERIFY_OK",
    "wait-http": "FIXTURE_HTTP_READY",
    "api-check": "FIXTURE_API_OK",
    "assert-runtime": "FIXTURE_RUNTIME_OK",
    "record-resume": "FIXTURE_RESUME_RECORDED",
    "record-linear-commit": "FIXTURE_LINEAR_COMMIT_RECORDED",
    "verify-linear-commit": "FIXTURE_LINEAR_COMMIT_VERIFIED",
    "assert-version-http": "FIXTURE_VERSION_HTTP_OK",
    "verify-resume-rehash": "FIXTURE_RESUME_REHASH_OK",
    "verify-resume": "FIXTURE_RESUME_OK",
    "verify-atomic-archive": "FIXTURE_ATOMIC_ARCHIVE_OK",
    "create-linear": "FIXTURE_LINEAR_CREATE_OK",
    "verify-linear-metrics": "FIXTURE_LINEAR_METRICS_OK",
    "merge-container-metrics": "FIXTURE_CONTAINER_METRICS_OK",
    "observe-maintenance-service": "FIXTURE_SUPERVISOR_OBSERVATION_OK",
    "assert-maintenance-http": "FIXTURE_MAINTENANCE_HTTP_OK",
    "assert-maintenance-fallback-http": (
        "FIXTURE_MAINTENANCE_FALLBACK_HTTP_OK"
    ),
    "benchmark-linear-service": "FIXTURE_LINEAR_BENCHMARK_OK",
    "prepare-linear-resume": "FIXTURE_LINEAR_RESUME_PREPARED",
    "prepare-linear-repair": "FIXTURE_LINEAR_REPAIR_PREPARED",
    "run-linear-repair-only": "FIXTURE_LINEAR_REPAIR_OK",
    "prepare-linear-tail-repair": "FIXTURE_LINEAR_TAIL_PREPARED",
    "run-linear-tail-repair-only": "FIXTURE_LINEAR_TAIL_REPAIRED",
    "exercise-linear-crash": "FIXTURE_LINEAR_CRASH_OK",
    "audit-linear-io": "FIXTURE_LINEAR_IO_AUDIT_OK",
    "corrupt-migration-manifest": "FIXTURE_MANIFEST_CORRUPTED",
}
FIXTURE_KINDS = (
    "legacy-md5",
    "transitional-fixed-slot",
    "pending-schema",
)
DERIVATIVE_KINDS = ("thumbnail", "standard", "square")
PINRY_DIRECT_MD5_ROOTS = tuple("0123456789abcdef")
_HOST_PATTERN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$"
)
_MAX_JSON_BYTES = 16 * 1024 * 1024
_FIXTURE_USERNAME = "fixture-reviewer"
_FIXTURE_PASSWORD = "fixture-review-password"
_MEDIA_MANIFEST = "media-migration.jsonl"
_BACKFILL_MANIFEST = "media-asset-backfill.jsonl"
_STATE_FILENAME = "migration-state.json"
_SUMMARY_FILENAME = "migration-summary.json"
_SNAPSHOT_FILENAME = "production.db.before-migration"
_LINEAR_IMAGE_COUNTS = frozenset((50, 350, 1000))
_ACTIVE_LINEAR_FILE_KEYS = ContextVar(
    "active_linear_file_keys", default=()
)
_LINEAR_CRASH_POINTS = frozenset((
    "before_destination_rename",
    "after_destination_rename",
    "after_batch_intent",
    "after_database_commit",
    "after_batch_commit",
    "after_archive_rename",
    "journal_intent_partial_write",
    "journal_intent_full_write_before_fsync",
    "journal_commit_partial_write",
    "journal_commit_full_write_before_fsync",
    "journal_repair_partial_write",
    "journal_repair_full_write_before_fsync",
    "journal_checksum_invalid",
))
_LINEAR_FAULT_ALIASES = {
    "before_destination_rename": "before_atomic_publish",
    "after_archive_rename": "after_archive_progress",
    "journal_checksum_invalid": "after_batch_intent",
}
_LINEAR_TAIL_AUDIT_POINTS = frozenset((
    "journal_commit_partial_write",
))
_PUBLIC_MAINTENANCE_STATUS_FIELDS = frozenset((
    "schema_version",
    "state",
    "phase",
    "phase_label",
    "run_id",
    "attempt",
    "resume_count",
    "started_at",
    "phase_started_at",
    "heartbeat_at",
    "progress_at",
    "last_committed_batch",
    "images_done",
    "images_total",
    "files_done",
    "files_total",
    "backfill_done",
    "backfill_total",
    "phase_percent",
    "overall_percent",
    "error_class",
    "error_code",
))
_MAINTENANCE_STATUS_STATES = frozenset((
    "starting",
    "recovering",
    "migrating",
    "starting_service",
    "ready",
    "failed",
))
_MAINTENANCE_PHASES = frozenset((
    "preparing",
    "recovery",
    "upgrade_v2",
    "snapshot",
    "planning",
    "copying",
    "database",
    "backfill_planning",
    "backfill_registering",
    "archive",
    "finalizing",
    "complete",
))
_MAINTENANCE_PHASE_TRANSITIONS = {
    "preparing": frozenset((
        "preparing", "snapshot", "planning", "recovery", "upgrade_v2",
        "copying", "database", "backfill_planning",
        "backfill_registering", "archive", "finalizing", "complete",
    )),
    "snapshot": frozenset((
        "snapshot", "planning", "recovery", "upgrade_v2", "copying",
        "database", "backfill_planning", "backfill_registering",
        "archive", "finalizing", "complete",
    )),
    "planning": frozenset((
        "planning", "recovery", "upgrade_v2", "copying", "database",
        "backfill_planning", "backfill_registering", "archive",
        "finalizing", "complete",
    )),
    "recovery": frozenset((
        "recovery", "upgrade_v2", "copying", "database",
        "backfill_planning", "backfill_registering", "archive",
        "finalizing", "complete",
    )),
    "upgrade_v2": frozenset((
        "upgrade_v2", "copying", "database", "backfill_planning",
        "backfill_registering", "archive", "finalizing", "complete",
    )),
    "copying": frozenset((
        "copying", "database", "backfill_planning",
        "backfill_registering", "archive", "finalizing", "complete",
    )),
    "database": frozenset((
        "database", "copying", "backfill_planning",
        "backfill_registering", "archive", "finalizing", "complete",
    )),
    "backfill_planning": frozenset((
        "backfill_planning", "backfill_registering", "archive",
        "finalizing", "complete",
    )),
    "backfill_registering": frozenset((
        "backfill_registering", "archive", "finalizing", "complete",
    )),
    "archive": frozenset(("archive", "finalizing", "complete")),
    "finalizing": frozenset(("finalizing", "complete")),
    "complete": frozenset(("complete",)),
}
_MAINTENANCE_ERROR_CLASSES = frozenset((
    "retryable",
    "operator_action_required",
    "fatal",
))
_MAINTENANCE_INTEGER_FIELDS = (
    "attempt",
    "resume_count",
    "last_committed_batch",
    "images_done",
    "files_done",
    "backfill_done",
)
_MAINTENANCE_TOTAL_FIELDS = (
    "images_total",
    "files_total",
    "backfill_total",
)
_MAINTENANCE_TIMESTAMP_FIELDS = (
    "started_at",
    "phase_started_at",
    "heartbeat_at",
    "progress_at",
)


def _is_repository_root(path):
    try:
        return (
            path.is_dir()
            and not path.is_symlink()
            and (path / "manage.py").is_file()
            and (path / "pinry" / "settings" / "base.py").is_file()
        )
    except OSError:
        return False


def _find_repository_root():
    candidates = [Path("/pinry")]
    script_path = Path(__file__).resolve()
    candidates.extend(script_path.parents)
    for candidate in candidates:
        if _is_repository_root(candidate):
            return candidate.resolve()
    raise RuntimeError("repository_root_missing")


_REPOSITORY_ROOT = _find_repository_root()
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))


class FixtureError(Exception):
    """stdout/stderr에 경로·인증 정보를 노출하지 않는 오류."""

    def __init__(self, code):
        super(FixtureError, self).__init__(code)
        self.code = code


def _repo_root():
    return _REPOSITORY_ROOT


def _contained_path(path, root):
    candidate = os.path.abspath(os.fspath(path))
    boundary = os.path.abspath(os.fspath(root))
    try:
        if os.path.commonpath((candidate, boundary)) != boundary:
            raise FixtureError("path_escape")
    except (TypeError, ValueError):
        raise FixtureError("path_escape") from None
    return candidate


def _write_all(descriptor, value):
    offset = 0
    while offset < len(value):
        written = os.write(descriptor, value[offset:])
        if written <= 0:
            raise FixtureError("write_failed")
        offset += written


def _atomic_write(path, value, mode=0o600):
    path = os.path.abspath(os.fspath(path))
    parent = os.path.dirname(path)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    temporary = os.path.join(
        parent,
        ".{}.fixture.tmp".format(os.path.basename(path)),
    )
    descriptor = None
    parent_descriptor = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            mode,
        )
        os.fchmod(descriptor, mode)
        _write_all(descriptor, value)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, path)
        os.chmod(path, mode)
        parent_descriptor = os.open(
            parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        os.fsync(parent_descriptor)
    except FileExistsError:
        raise FixtureError("write_conflict") from None
    except FixtureError:
        raise
    except OSError:
        raise FixtureError("write_failed") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent_descriptor is not None:
            os.close(parent_descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _deterministic_png(index):
    from PIL import Image as PILImage

    width, height = 96, 72
    index_bytes = index.to_bytes(8, byteorder="big", signed=False)
    image = PILImage.new("RGB", (width, height))
    pixels = []
    for y_value in range(height):
        for x_value in range(width):
            pixels.append((
                (x_value * 17 + y_value * 3 + index * 29) % 256,
                (x_value * 5 + y_value * 13 + index * 47) % 256,
                (x_value * 11 + y_value * 7 + index * 61) % 256,
            ))
    for byte_index, byte_value in enumerate(index_bytes):
        pixels[byte_index] = (byte_value, byte_value ^ 0xA5, byte_index)
    image.putdata(pixels)
    output = BytesIO()
    try:
        image.save(
            output,
            format="PNG",
            compress_level=9,
            optimize=False,
        )
        return output.getvalue()
    finally:
        image.close()


def _deterministic_jpeg(index):
    from PIL import Image as PILImage

    width, height = 96, 72
    image = PILImage.new("RGB", (width, height))
    image.putdata([
        (
            (x_value * 17 + y_value * 3 + index * 29) % 256,
            (x_value * 5 + y_value * 13 + index * 47) % 256,
            (x_value * 11 + y_value * 7 + index * 61) % 256,
        )
        for y_value in range(height)
        for x_value in range(width)
    ])
    output = BytesIO()
    try:
        image.save(
            output,
            format="JPEG",
            quality=88,
            optimize=False,
            progressive=False,
        )
        return output.getvalue()
    finally:
        image.close()


def _fixed_uuid(index):
    return uuid.UUID(
        "10000000-0000-4000-8000-{:012d}".format(index + 1)
    )


def _sha256(value):
    return hashlib.sha256(value).hexdigest()


def _md5(value):
    return hashlib.md5(value).hexdigest()


def _configure_django(data_root):
    data_root = os.path.abspath(os.fspath(data_root))
    database_path = os.path.join(data_root, "production.db")
    media_root = os.path.join(data_root, "static", "media")
    os.environ.setdefault(
        "DJANGO_SETTINGS_MODULE",
        "pinry.settings.development",
    )
    import django

    django.setup()
    from django.conf import settings
    from django.db import connections

    settings.PINRY_DATA_ROOT = data_root
    settings.STATIC_ROOT = os.path.join(data_root, "static")
    settings.MEDIA_ROOT = media_root
    settings.DATABASES["default"]["NAME"] = database_path
    connection = connections["default"]
    connection.close()
    connection.settings_dict["NAME"] = database_path
    os.makedirs(media_root, mode=0o770, exist_ok=True)
    return connection, database_path, media_root


def _prepared_files(
    media_root,
    index,
    asset_uuid,
    original_filename,
    image_format="PNG",
):
    from core.services.image_inspection import InspectedImage
    from core.services.media_storage import MediaStorage

    if image_format == "PNG":
        content = _deterministic_png(index)
    elif image_format == "JPEG":
        content = _deterministic_jpeg(index)
    else:
        raise FixtureError("prepared_bytes_invalid")
    fetched = InspectedImage(
        content=content,
        image_format=image_format,
        width=96,
        height=72,
        final_url="fixture://deterministic/{}".format(index),
    )
    prepared = MediaStorage(media_root=media_root).prepare(
        fetched,
        asset_uuid,
        original_filename,
    )
    try:
        result = {}
        for entry in prepared.files:
            descriptor = entry.owned_staging_handle.descriptor
            value = os.pread(descriptor, entry.size, 0)
            if len(value) != entry.size or _sha256(value) != entry.sha256:
                raise FixtureError("prepared_bytes_invalid")
            result[entry.kind] = {
                "bytes": value,
                "sha256": entry.sha256,
                "width": entry.width,
                "height": entry.height,
                "format": entry.image_format,
                "canonical_path": entry.final_relative_path,
            }
        if set(result) != set(("original",) + DERIVATIVE_KINDS):
            raise FixtureError("prepared_bytes_invalid")
        return result
    finally:
        prepared.cleanup()


def _write_media_file(media_root, relative_path, value):
    absolute = _contained_path(
        os.path.join(media_root, relative_path),
        media_root,
    )
    parent = os.path.dirname(absolute)
    os.makedirs(parent, mode=0o750, exist_ok=True)
    descriptor = os.open(
        absolute,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o640,
    )
    try:
        _write_all(descriptor, value)
        os.fsync(descriptor)
        file_stat = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    return {
        "device": file_stat.st_dev,
        "inode": file_stat.st_ino,
        "size": file_stat.st_size,
    }


def _legacy_md5_path(kind, leaf, value):
    digest = _md5(value)
    return "{}/{}/{}/{}".format(
        digest[0],
        digest[1],
        digest,
        leaf,
    )


def _orphan_sentinel_bytes(root_name):
    for nonce in range(4096):
        value = "fixture-orphan-{}-{}".format(root_name, nonce).encode(
            "ascii"
        )
        if _md5(value).startswith(root_name):
            return value
    raise FixtureError("orphan_sentinel_generation_failed")


def _target_for_kind(kind):
    if kind == "legacy-md5":
        return (
            ("django_images", "0002_auto_20180826_0814"),
            ("core", "0010_auto_20210311_1521"),
        )
    return (
        ("django_images", "0005_enforce_image_asset_metadata"),
        ("core", "0013_remove_pin_trashed_at"),
    )


def _create_fixture(kind, data_root, count, receipt_path, linear=False):
    maximum = 1000 if linear else 512
    if kind not in FIXTURE_KINDS or count < 1 or count > maximum:
        raise FixtureError("create_arguments_invalid")
    data_root = os.path.abspath(os.fspath(data_root))
    receipt_path = _contained_path(receipt_path, data_root)
    connection, database_path, media_root = _configure_django(data_root)
    if os.path.exists(database_path):
        raise FixtureError("database_not_fresh")

    from django.contrib.auth.hashers import make_password
    from django.db.migrations.executor import MigrationExecutor

    targets = _target_for_kind(kind)
    executor = MigrationExecutor(connection)
    executor.migrate(targets)
    historical_apps = executor.loader.project_state(targets).apps
    User = historical_apps.get_model("users", "User")
    Image = historical_apps.get_model("django_images", "Image")
    Thumbnail = historical_apps.get_model("django_images", "Thumbnail")
    Pin = historical_apps.get_model("core", "Pin")
    Board = historical_apps.get_model("core", "Board")

    user = User.objects.create(
        username=_FIXTURE_USERNAME,
        password=make_password(_FIXTURE_PASSWORD),
        is_active=True,
    )
    board = Board.objects.create(
        submitter_id=user.pk,
        name="historical-fixture-board",
        private=False,
    )
    receipt_items = []
    for index in range(count):
        asset_uuid = _fixed_uuid(index)
        image_format = "JPEG" if linear and index % 2 == 0 else "PNG"
        canonical_extension = ".jpg" if image_format == "JPEG" else ".png"
        if linear and index % 4 in (0, 1):
            extension = ".png" if canonical_extension == ".jpg" else ".jpg"
        else:
            extension = canonical_extension
        index_token = "{:04d}".format(index) if linear else "{:03d}".format(index)
        original_filename = "fixture-original-{}{}".format(
            index_token, extension
        )
        prepared = _prepared_files(
            media_root,
            index,
            asset_uuid,
            original_filename,
            image_format=image_format,
        )
        if kind == "legacy-md5":
            original_path = _legacy_md5_path(
                "original",
                "legacy-original-{}{}".format(index_token, extension),
                prepared["original"]["bytes"],
            )
            image_values = {
                "image": original_path,
                "height": prepared["original"]["height"],
                "width": prepared["original"]["width"],
            }
        elif kind == "transitional-fixed-slot":
            original_path = "originals/{}/original.png".format(asset_uuid)
            image_values = {
                "image": original_path,
                "asset_uuid": asset_uuid,
                "original_filename": original_filename,
                "height": prepared["original"]["height"],
                "width": prepared["original"]["width"],
            }
        else:
            original_path = prepared["original"]["canonical_path"]
            image_values = {
                "image": original_path,
                "asset_uuid": asset_uuid,
                "original_filename": original_filename,
                "height": prepared["original"]["height"],
                "width": prepared["original"]["width"],
            }
        original_identity = _write_media_file(
            media_root,
            original_path,
            prepared["original"]["bytes"],
        )
        image = Image.objects.create(**image_values)
        derivative_receipt = []
        for derivative_kind in DERIVATIVE_KINDS:
            entry = prepared[derivative_kind]
            if kind == "legacy-md5":
                path = _legacy_md5_path(
                    derivative_kind,
                    "legacy-{}-{}{}".format(
                        derivative_kind,
                        index_token,
                        extension,
                    ),
                    entry["bytes"],
                )
            else:
                path = entry["canonical_path"]
            derivative_identity = _write_media_file(
                media_root, path, entry["bytes"]
            )
            thumbnail = Thumbnail.objects.create(
                original_id=image.pk,
                image=path,
                size=derivative_kind,
                height=entry["height"],
                width=entry["width"],
            )
            derivative_receipt.append({
                "id": thumbnail.pk,
                "kind": derivative_kind,
                "legacy_path": path,
                "sha256": entry["sha256"],
                "format": entry["format"],
                "width": entry["width"],
                "height": entry["height"],
                "source_identity": derivative_identity,
            })
        pin_url = "fixture-preserved-value-{:03d}".format(index)
        pin = Pin.objects.create(
            submitter_id=user.pk,
            image_id=image.pk,
            private=False,
            url=pin_url,
            referer=None,
            description="historical fixture {:03d}".format(index),
        )
        board.pins.add(pin)
        receipt_items.append({
            "image_id": image.pk,
            "pin_id": pin.pk,
            "pin_url": pin_url,
            "asset_uuid": (
                str(asset_uuid) if kind != "legacy-md5" else None
            ),
            "original_filename": (
                original_filename if kind != "legacy-md5" else None
            ),
            "legacy_original_path": original_path,
            "prior_image_media_url": "/media/{}".format(original_path),
            "original_sha256": prepared["original"]["sha256"],
            "original_format": prepared["original"]["format"],
            "original_width": prepared["original"]["width"],
            "original_height": prepared["original"]["height"],
            "source_identity": original_identity,
            "derivatives": derivative_receipt,
        })
    receipt = {
        "schema_version": 1,
        "kind": kind,
        "count": count,
        "board_id": board.pk,
        "archive_root_identity": None,
        "expected_direct_roots": None,
        "orphan_sentinel": None,
        "items": receipt_items,
    }
    if kind == "legacy-md5":
        referenced_roots = {
            item["legacy_original_path"].split("/", 1)[0]
            for item in receipt_items
        } | {
            derivative["legacy_path"].split("/", 1)[0]
            for item in receipt_items
            for derivative in item["derivatives"]
        }
        orphan_root = next(
            (
                root_name
                for root_name in PINRY_DIRECT_MD5_ROOTS
                if root_name not in referenced_roots
            ),
            PINRY_DIRECT_MD5_ROOTS[0],
        )
        sentinel_bytes = _orphan_sentinel_bytes(orphan_root)
        sentinel_path = _legacy_md5_path(
            "orphan",
            "orphan-sentinel.bin",
            sentinel_bytes,
        )
        sentinel_identity = _write_media_file(
            media_root, sentinel_path, sentinel_bytes
        )
        for root_name in PINRY_DIRECT_MD5_ROOTS:
            os.makedirs(
                os.path.join(media_root, root_name),
                mode=0o750,
                exist_ok=True,
            )
        root_identities = {}
        for root_name in PINRY_DIRECT_MD5_ROOTS:
            root_stat = os.stat(
                os.path.join(media_root, root_name),
                follow_symlinks=False,
            )
            root_identities[root_name] = {
                "device": root_stat.st_dev,
                "inode": root_stat.st_ino,
            }
        receipt["archive_root_identity"] = root_identities
        receipt["expected_direct_roots"] = list(PINRY_DIRECT_MD5_ROOTS)
        receipt["orphan_sentinel"] = {
            "path": sentinel_path,
            "sha256": _sha256(sentinel_bytes),
            "source_identity": sentinel_identity,
        }
    serialized = json.dumps(
        receipt,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii") + b"\n"
    _atomic_write(receipt_path, serialized, mode=0o600)
    try:
        account = pwd.getpwnam("www-data")
    except KeyError:
        account = None
    if account is not None:
        os.chown(receipt_path, account.pw_uid, account.pw_gid)
    connection.close()


def _create_linear_fixture(data_root, images, thumbnails, receipt_path):
    if images not in _LINEAR_IMAGE_COUNTS or thumbnails != 3:
        raise FixtureError("create_linear_arguments_invalid")
    _create_fixture(
        "legacy-md5",
        data_root,
        images,
        receipt_path,
        linear=True,
    )
    receipt, _payload, _file_stat = _load_json_file(receipt_path)
    expected_files = []
    formats = {"JPEG": 0, "PNG": 0}
    total_bytes = 0
    total_pixels = 0
    for item in receipt["items"]:
        files = [{
            "file_key": "original:{}".format(item["image_id"]),
            "format": item["original_format"],
            "relative_path": item["legacy_original_path"],
            "size": item["source_identity"]["size"],
            "source_identity": item["source_identity"],
            "pixels": item["original_width"] * item["original_height"],
        }]
        files.extend({
            "file_key": "thumbnail:{}:{}".format(
                item["image_id"], derivative["id"]
            ),
            "format": derivative["format"],
            "relative_path": derivative["legacy_path"],
            "size": derivative["source_identity"]["size"],
            "source_identity": derivative["source_identity"],
            "pixels": derivative["width"] * derivative["height"],
        } for derivative in item["derivatives"])
        for entry in files:
            formats[entry["format"]] += 1
            total_bytes += entry["size"]
            total_pixels += entry.pop("pixels")
            expected_files.append(entry)
    receipt.update({
        "images_total": images,
        "files_total": images * (thumbnails + 1),
        "thumbnails_per_image": thumbnails,
        "formats": formats,
        "total_bytes": total_bytes,
        "total_pixels": total_pixels,
        "expected_files": sorted(
            expected_files, key=lambda item: item["file_key"]
        ),
    })
    _atomic_write(
        receipt_path,
        json.dumps(
            receipt,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii") + b"\n",
        mode=0o600,
    )
    try:
        account = pwd.getpwnam("www-data")
    except KeyError:
        account = None
    if account is not None:
        os.chown(receipt_path, account.pw_uid, account.pw_gid)


def _verify_linear_metrics(receipt_path, status_path):
    receipt, _receipt_payload, _receipt_stat = _load_json_file(receipt_path)
    status, _status_payload, _status_stat = _load_json_file(status_path)
    metric_fields = {
        "images", "seconds", "queries", "source_bytes", "source_full_reads",
        "resume_rehash_reads", "pillow_decodes", "old_manifest_replays",
        "linear_journal_replays", "process_starts",
        "cpu_average_percent", "cpu_max_percent", "disk_read_bytes",
        "disk_write_bytes", "max_rss_bytes", "ratio",
        "batch_seconds_p50", "batch_seconds_p95", "batch_seconds_max",
        "batch_sample_count", "max_heartbeat_gap_seconds",
        "seconds_source", "batch_timing_source", "heartbeat_source",
        "supervisor_seconds", "supervisor_status_samples",
        "supervisor_heartbeat_updates",
    }
    try:
        expected_files = receipt["expected_files"]
        expected_sizes = {
            item["file_key"]: item["size"] for item in expected_files
        }
        expected_keys = set(expected_sizes)
        source_bytes = status["source_bytes"]
        full_reads = status["source_full_reads"]
        decodes = status["pillow_decodes"]
        rehash_reads = status["resume_rehash_reads"]
        starts = status["process_starts"]
    except (KeyError, TypeError):
        raise FixtureError("linear_metrics_invalid") from None
    if (
        not isinstance(receipt, dict)
        or receipt.get("images_total") not in _LINEAR_IMAGE_COUNTS
        or receipt.get("files_total") != receipt["images_total"] * 4
        or len(expected_sizes) != receipt["files_total"]
        or not isinstance(status, dict)
        or not metric_fields.issubset(status)
        or status.get("state") != "ready"
        or status.get("images") != receipt["images_total"]
        or status.get("images_total") != receipt["images_total"]
        or status.get("files_total") != receipt["files_total"]
        or status.get("images_done") != receipt["images_total"]
        or status.get("files_done") != receipt["files_total"]
        or source_bytes != expected_sizes
        or set(full_reads) != expected_keys
        or set(decodes) != expected_keys
        or any(type(value) is not int or value != 1
               for value in full_reads.values())
        or any(type(value) is not int or value != 1
               for value in decodes.values())
        or not isinstance(rehash_reads, dict)
        or not set(rehash_reads).issubset(expected_keys)
        or any(type(value) is not int or value != 1
               for value in rehash_reads.values())
        or type(starts) is not int
        or starts < 1
        or type(status["old_manifest_replays"]) is not int
        or status["old_manifest_replays"] < 0
        or status["old_manifest_replays"] > 2 * starts
        or type(status["linear_journal_replays"]) is not int
        or status["linear_journal_replays"] < 0
        or status["linear_journal_replays"] > starts
        or status.get("seconds_source") != "fixture_coordinator"
        or status.get("batch_timing_source")
        != "generator_yield_lifetime"
        or status.get("heartbeat_source") != "nginx_public_status"
        or type(status.get("batch_sample_count")) is not int
        or status["batch_sample_count"] <= 0
        or type(status.get("supervisor_status_samples")) is not int
        or status["supervisor_status_samples"] <= 0
        or type(status.get("supervisor_heartbeat_updates")) is not int
        or status["supervisor_heartbeat_updates"] <= 0
    ):
        raise FixtureError("linear_metrics_invalid")
    numeric = (
        "seconds", "cpu_average_percent", "cpu_max_percent", "ratio",
        "batch_seconds_p50", "batch_seconds_p95", "batch_seconds_max",
        "max_heartbeat_gap_seconds", "supervisor_seconds",
    )
    integers = (
        "queries", "disk_read_bytes", "disk_write_bytes", "max_rss_bytes",
    )
    if (
        any(
            type(status[name]) not in (int, float) or status[name] < 0
            for name in numeric
        )
        or status["seconds"] <= 0
        or status["supervisor_seconds"] <= 0
        or status["ratio"] <= 0
        or status["cpu_average_percent"] > status["cpu_max_percent"]
        or not (
            status["batch_seconds_p50"]
            <= status["batch_seconds_p95"]
            <= status["batch_seconds_max"]
            <= status["seconds"]
        )
        or status["max_heartbeat_gap_seconds"]
        > status["supervisor_seconds"]
        or any(
            type(status[name]) is not int or status[name] < 0
            for name in integers
        )
    ):
        raise FixtureError("linear_metrics_invalid")


def _verify_resume_rehash(receipt_path, status_path, expected_path):
    receipt, _payload, _file_stat = _load_json_file(receipt_path)
    status, _payload, _file_stat = _load_json_file(status_path)
    expected, _payload, _file_stat = _load_json_file(expected_path)
    receipt_keys = {
        item["file_key"] for item in receipt.get("expected_files", [])
    }
    expected_keys = expected.get("expected_resume_rehash_keys")
    reads = status.get("resume_rehash_reads")
    if (
        not isinstance(expected_keys, list)
        or not expected_keys
        or len(expected_keys) != len(set(expected_keys))
        or not set(expected_keys).issubset(receipt_keys)
        or not isinstance(reads, dict)
        or set(reads) != set(expected_keys)
        or any(type(value) is not int or value != 1
               for value in reads.values())
    ):
        raise FixtureError("resume_rehash_invalid")
    for name in (
        "seconds", "cpu_average_percent", "cpu_max_percent", "ratio",
        "batch_seconds_p50", "batch_seconds_p95", "batch_seconds_max",
        "max_heartbeat_gap_seconds",
    ):
        if type(status[name]) not in (int, float) or status[name] < 0:
            raise FixtureError("linear_metrics_invalid")
    for name in (
        "queries", "disk_read_bytes", "disk_write_bytes", "max_rss_bytes"
    ):
        if type(status[name]) is not int or status[name] < 0:
            raise FixtureError("linear_metrics_invalid")
    _validate_receipt_value(status)


def _assert_public_status(value, expected_state):
    if not isinstance(value, dict) or value.get("state") != expected_state:
        raise FixtureError("maintenance_status_invalid")

    def visit(current, field=None):
        if isinstance(current, dict):
            for key, nested in current.items():
                if (
                    not isinstance(key, str)
                    or key not in _PUBLIC_MAINTENANCE_STATUS_FIELDS
                ):
                    raise FixtureError("maintenance_status_private")
                visit(nested, key)
        elif isinstance(current, list):
            for nested in current:
                visit(nested, field)
        elif isinstance(current, str):
            if (
                field in _MAINTENANCE_TIMESTAMP_FIELDS
                and _is_maintenance_timestamp(current)
            ):
                return
            parsed = urlsplit(current)
            if (
                os.path.isabs(current)
                or current.startswith(("\\\\", "//"))
                or re.match(r"^[A-Za-z]:[\\/]", current)
                or parsed.scheme
                or re.fullmatch(r"[0-9a-fA-F]{32}|[0-9a-fA-F]{40}|"
                                r"[0-9a-fA-F]{64}", current)
            ):
                raise FixtureError("maintenance_status_private")

    visit(value)


def _is_maintenance_timestamp(value):
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.datetime.strptime(
            value, "%Y-%m-%dT%H:%M:%SZ"
        )
    except ValueError:
        return False
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ") == value


def _is_maintenance_percent(value):
    return (
        value is None
        or (
            type(value) in (int, float)
            and math.isfinite(value)
            and 0 <= value <= 100
        )
    )


def _assert_full_public_status(value, expected_state):  # noqa: C901
    _assert_public_status(value, expected_state)
    if set(value) != _PUBLIC_MAINTENANCE_STATUS_FIELDS:
        raise FixtureError("maintenance_status_invalid")
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
    ):
        raise FixtureError("maintenance_status_invalid")
    if value["state"] not in _MAINTENANCE_STATUS_STATES:
        raise FixtureError("maintenance_status_invalid")
    if value["phase"] not in _MAINTENANCE_PHASES:
        raise FixtureError("maintenance_status_invalid")
    if (
        not isinstance(value["phase_label"], str)
        or not 1 <= len(value["phase_label"]) <= 160
    ):
        raise FixtureError("maintenance_status_invalid")
    if value["run_id"] is not None and (
        not isinstance(value["run_id"], str)
        or not 1 <= len(value["run_id"]) <= 128
    ):
        raise FixtureError("maintenance_status_invalid")
    if any(
        type(value[field]) is not int or value[field] < 0
        for field in _MAINTENANCE_INTEGER_FIELDS
    ):
        raise FixtureError("maintenance_status_invalid")
    if any(
        value[field] is not None
        and (type(value[field]) is not int or value[field] < 0)
        for field in _MAINTENANCE_TOTAL_FIELDS
    ):
        raise FixtureError("maintenance_status_invalid")
    if any(
        value[field] is not None
        and not _is_maintenance_timestamp(value[field])
        for field in _MAINTENANCE_TIMESTAMP_FIELDS
    ):
        raise FixtureError("maintenance_status_invalid")
    if not all(
        _is_maintenance_timestamp(value[field])
        for field in ("phase_started_at", "heartbeat_at")
    ):
        raise FixtureError("maintenance_status_invalid")
    if not all(
        _is_maintenance_percent(value[field])
        for field in ("phase_percent", "overall_percent")
    ):
        raise FixtureError("maintenance_status_invalid")
    if (
        value["error_class"] is not None
        and value["error_class"] not in _MAINTENANCE_ERROR_CLASSES
    ):
        raise FixtureError("maintenance_status_invalid")
    if value["error_code"] is not None and (
        not isinstance(value["error_code"], str)
        or re.fullmatch(r"[a-z0-9_]{1,128}", value["error_code"])
        is None
    ):
        raise FixtureError("maintenance_status_invalid")
    if value["state"] == "failed":
        if value["error_class"] is None or value["error_code"] is None:
            raise FixtureError("maintenance_status_invalid")
    elif value["error_class"] is not None or value["error_code"] is not None:
        raise FixtureError("maintenance_status_invalid")
    for done_field, total_field in (
        ("images_done", "images_total"),
        ("files_done", "files_total"),
        ("backfill_done", "backfill_total"),
    ):
        total = value[total_field]
        if total is not None and value[done_field] > total:
            raise FixtureError("maintenance_status_invalid")


def _maintenance_progress(value):
    return {
        "phase": value["phase"],
        "heartbeat_at": value["heartbeat_at"],
        "progress_at": value["progress_at"],
        "attempt": value["attempt"],
        "resume_count": value["resume_count"],
        "last_committed_batch": value["last_committed_batch"],
        "images_done": value["images_done"],
        "files_done": value["files_done"],
        "backfill_done": value["backfill_done"],
        "phase_percent": value["phase_percent"],
        "overall_percent": value["overall_percent"],
    }


def _assert_progress_not_regressed(previous, current):
    numeric_fields = (
        "attempt",
        "resume_count",
        "last_committed_batch",
        "images_done",
        "files_done",
        "backfill_done",
    )
    if (
        any(current[field] < previous[field] for field in numeric_fields)
        or current["phase"] not in _MAINTENANCE_PHASE_TRANSITIONS[
            previous["phase"]
        ]
        or current["heartbeat_at"] < previous["heartbeat_at"]
    ):
        raise FixtureError("maintenance_progress_regressed")
    if (
        previous["progress_at"] is not None
        and current["progress_at"] is not None
        and current["progress_at"] < previous["progress_at"]
    ):
        raise FixtureError("maintenance_progress_regressed")
    for field in ("overall_percent",):
        if (
            previous[field] is not None
            and current[field] is not None
            and current[field] < previous[field]
        ):
            raise FixtureError("maintenance_progress_regressed")
    if (
        current["phase"] == previous["phase"]
        and previous["phase_percent"] is not None
        and current["phase_percent"] is not None
        and current["phase_percent"] < previous["phase_percent"]
    ):
        raise FixtureError("maintenance_progress_regressed")


def _maintenance_progress_advanced(previous, current):
    if current["phase"] != previous["phase"]:
        return True
    for field in (
        "last_committed_batch",
        "images_done",
        "files_done",
        "backfill_done",
        "phase_percent",
        "overall_percent",
    ):
        before = previous[field]
        after = current[field]
        if after is not None and (before is None or after > before):
            return True
    return False


def _assert_completed_status(
    value, previous, expected_images, expected_files,
):
    if (
        expected_images is None
        or expected_files is None
        or value["phase"] != "complete"
        or value["phase_percent"] != 100
        or value["overall_percent"] != 100
        or value["images_total"] != expected_images
        or value["images_done"] != expected_images
        or value["files_total"] != expected_files
        or value["files_done"] != expected_files
        or value["backfill_total"] is None
        or value["backfill_done"] != value["backfill_total"]
    ):
        raise FixtureError("maintenance_status_invalid")
    if previous is not None:
        _assert_progress_not_regressed(previous, _maintenance_progress(value))


def _assert_maintenance_http(  # noqa: C901
    base_url, expected_state, timeout, minimum_resume_count=None,
    expected_error_code=None, expected_images=None, expected_files=None,
    require_terminal=False,
):
    if expected_state not in ("recovering", "migrating", "failed"):
        raise FixtureError("maintenance_state_invalid")
    if timeout <= 0 or timeout > 600:
        raise FixtureError("http_timeout_invalid")
    base_url = _validate_http_url(base_url)
    import requests

    session = requests.Session()
    session.trust_env = False
    deadline = time.monotonic() + timeout

    def request(path, unavailable_code, retry_startup=False):
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FixtureError(unavailable_code)
            try:
                return session.get(
                    "{}{}".format(base_url, path),
                    timeout=min(remaining, 5.0),
                    allow_redirects=False,
                )
            except requests.RequestException:
                if not retry_startup or time.monotonic() >= deadline:
                    raise FixtureError(unavailable_code) from None
                time.sleep(0.05)

    try:
        page = request(
            "/migration/", "maintenance_page_unavailable",
            retry_startup=True,
        )
        if (
            page.status_code != 200
            or "기존 Pinry 데이터를 이전하고 있습니다." not in page.text
        ):
            raise FixtureError("maintenance_page_invalid")
        for path in ("/api/v2/version/", "/media/private"):
            response = request(path, "maintenance_blocking_unavailable")
            if (
                response.status_code != 503
                or not response.headers.get("Retry-After")
            ):
                raise FixtureError("maintenance_blocking_invalid")

        first = None
        previous = None
        while True:
            response = request(
                "/migration-status.json", "maintenance_status_unavailable"
            )
            try:
                status = response.json()
            except ValueError:
                raise FixtureError("maintenance_status_invalid") from None
            cache_control = response.headers.get("Cache-Control", "").lower()
            if response.status_code != 200 or "no-store" not in cache_control:
                raise FixtureError("maintenance_status_invalid")
            observed_state = status.get("state")
            if expected_state == "migrating" and observed_state == "failed":
                _assert_full_public_status(status, observed_state)
                current = _maintenance_progress(status)
                if previous is not None:
                    _assert_progress_not_regressed(previous, current)
                raise FixtureError(
                    "maintenance_failed:{}".format(status["error_code"])
                )
            if (
                expected_state == "migrating"
                and observed_state in (
                    "starting",
                    "recovering",
                    "migrating",
                    "starting_service",
                    "ready",
                )
            ):
                _assert_full_public_status(status, observed_state)
                current = _maintenance_progress(status)
                if previous is not None:
                    _assert_progress_not_regressed(previous, current)
                if observed_state in ("starting_service", "ready"):
                    _assert_completed_status(
                        status,
                        previous,
                        expected_images,
                        expected_files,
                    )
                    return
                if observed_state != "migrating":
                    previous = current
                    if time.monotonic() >= deadline:
                        raise FixtureError(
                            "maintenance_state_not_observed"
                        )
                    time.sleep(0.05)
                    continue
                if (
                    minimum_resume_count is not None
                    and status["resume_count"] < minimum_resume_count
                ):
                    raise FixtureError("maintenance_resume_count_invalid")
                if first is None:
                    first = current
                elif (
                    _maintenance_progress_advanced(first, current)
                    and not require_terminal
                ):
                    return
                previous = current
                if time.monotonic() >= deadline:
                    raise FixtureError(
                        "maintenance_progress_not_observed"
                    )
                time.sleep(0.05)
                continue
            if observed_state != expected_state:
                if time.monotonic() >= deadline:
                    raise FixtureError("maintenance_state_not_observed")
                time.sleep(0.05)
                continue
            _assert_full_public_status(status, expected_state)
            if (
                expected_error_code is not None
                and status.get("error_code") != expected_error_code
            ):
                raise FixtureError("maintenance_error_code_invalid")
            if (
                minimum_resume_count is not None
                and (
                    type(status.get("resume_count")) is not int
                    or status["resume_count"] < minimum_resume_count
                )
            ):
                raise FixtureError("maintenance_resume_count_invalid")
            if expected_state != "migrating":
                return
    finally:
        session.close()


def _assert_maintenance_fallback_http(base_url, status_mode, timeout):
    if status_mode not in ("missing", "corrupt"):
        raise FixtureError("maintenance_status_mode_invalid")
    if timeout <= 0 or timeout > 600:
        raise FixtureError("http_timeout_invalid")
    base_url = _validate_http_url(base_url)
    import requests

    session = requests.Session()
    session.trust_env = False
    deadline = time.monotonic() + timeout

    def request(path, retry_startup=False):
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FixtureError("maintenance_http_failed")
            try:
                return session.get(
                    "{}{}".format(base_url, path),
                    timeout=min(remaining, 5.0),
                    allow_redirects=False,
                )
            except requests.RequestException:
                if not retry_startup or time.monotonic() >= deadline:
                    raise FixtureError("maintenance_http_failed") from None
                time.sleep(0.05)

    try:
        page = request("/migration/", retry_startup=True)
        if (
            page.status_code != 200
            or "기존 Pinry 데이터를 이전하고 있습니다." not in page.text
        ):
            raise FixtureError("maintenance_page_invalid")
        for path in ("/api/v2/version/", "/media/private"):
            response = request(path)
            if (
                response.status_code != 503
                or not response.headers.get("Retry-After")
            ):
                raise FixtureError("maintenance_blocking_invalid")
        status = request("/migration-status.json")
        if status_mode == "missing":
            if status.status_code != 404:
                raise FixtureError("maintenance_status_fallback_invalid")
        else:
            if status.status_code != 200:
                raise FixtureError("maintenance_status_fallback_invalid")
            try:
                status.json()
            except ValueError:
                pass
            else:
                raise FixtureError("maintenance_status_fallback_invalid")
    finally:
        session.close()


def _observe_maintenance_service(  # noqa: C901
    base_url, output_path, started_at_epoch_ns, images, files,
    timeout, poll_interval,
):
    if (
        type(started_at_epoch_ns) is not int
        or started_at_epoch_ns <= 0
        or started_at_epoch_ns > time.time_ns()
        or images not in _LINEAR_IMAGE_COUNTS
        or files != images * 4
        or type(timeout) not in (int, float)
        or timeout <= 0
        or timeout > 10800
        or type(poll_interval) not in (int, float)
        or poll_interval < 0.01
        or poll_interval > 5
    ):
        raise FixtureError("supervisor_observation_arguments_invalid")
    base_url = _validate_http_url(base_url)
    output_path = os.path.abspath(os.fspath(output_path))
    allowed_states = frozenset((
        "starting", "recovering", "migrating",
        "starting_service", "ready", "failed",
    ))
    from urllib.error import HTTPError, URLError
    from urllib.request import HTTPRedirectHandler, Request, build_opener

    class NoRedirectHandler(HTTPRedirectHandler):
        def redirect_request(self, request, file_pointer, code, message,
                             headers, new_url):
            return None

    opener = build_opener(NoRedirectHandler())
    deadline = time.monotonic() + timeout
    observed_response = False
    status_samples = 0
    states = []
    heartbeat_values = []
    previous_heartbeat = None

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FixtureError("supervisor_observation_timeout")
        try:
            response = opener.open(
                Request(
                    "{}/migration-status.json".format(
                        base_url.rstrip("/")
                    ),
                    method="GET",
                ),
                timeout=min(remaining, 5.0),
            )
        except HTTPError as error:
            response = error
        except (OSError, URLError):
            if observed_response:
                raise FixtureError(
                    "supervisor_observation_http_invalid"
                ) from None
            time.sleep(poll_interval)
            continue
        status_code = response.getcode()
        if status_code == 404 and not observed_response:
            response.close()
            time.sleep(poll_interval)
            continue
        observed_response = True
        if (
            status_code != 200
            or "no-store" not in response.headers.get(
                "Cache-Control", ""
            ).lower()
        ):
            response.close()
            raise FixtureError("supervisor_observation_http_invalid")
        try:
            body = response.read(65537)
            if len(body) > 65536:
                raise ValueError
            status = json.loads(body.decode("utf-8"))
        except (UnicodeError, ValueError):
            raise FixtureError(
                "supervisor_observation_status_invalid"
            ) from None
        finally:
            response.close()
        state = status.get("state")
        if state not in allowed_states:
            raise FixtureError("supervisor_observation_status_invalid")
        try:
            _assert_public_status(status, state)
        except FixtureError:
            raise FixtureError(
                "supervisor_observation_status_invalid"
            ) from None
        heartbeat = status.get("heartbeat_at")
        if not isinstance(heartbeat, str) or not heartbeat:
            raise FixtureError("supervisor_observation_status_invalid")
        if previous_heartbeat is not None and heartbeat < previous_heartbeat:
            raise FixtureError("supervisor_observation_status_invalid")
        if heartbeat != previous_heartbeat:
            heartbeat_values.append(heartbeat)
            previous_heartbeat = heartbeat
        status_samples += 1
        if not states or states[-1] != state:
            states.append(state)
        if state == "failed":
            raise FixtureError("supervisor_migration_failed")
        if state == "ready":
            if (
                status.get("images_done") != images
                or status.get("images_total") != images
                or status.get("files_done") != files
                or status.get("files_total") != files
            ):
                raise FixtureError(
                    "supervisor_observation_totals_invalid"
                )
            break
        time.sleep(poll_interval)
    heartbeat_epochs = []
    try:
        for heartbeat in heartbeat_values:
            heartbeat_epochs.append(time.mktime(time.strptime(
                heartbeat, "%Y-%m-%dT%H:%M:%SZ"
            )))
    except (OverflowError, ValueError):
        raise FixtureError("supervisor_observation_status_invalid") from None
    heartbeat_gaps = [
        later - earlier
        for earlier, later in zip(
            heartbeat_epochs, heartbeat_epochs[1:]
        )
    ]
    if any(gap < 0 for gap in heartbeat_gaps):
        raise FixtureError("supervisor_observation_status_invalid")
    heartbeat_updates = max(0, len(heartbeat_values) - 1)
    if heartbeat_updates == 0:
        raise FixtureError("supervisor_heartbeat_not_observed")
    elapsed = (time.time_ns() - started_at_epoch_ns) / 1000000000.0
    if elapsed <= 0:
        raise FixtureError("supervisor_observation_clock_invalid")
    result = {
        "schema_version": 1,
        "source": "nginx_public_status",
        "state": "ready",
        "images_total": images,
        "files_total": files,
        "supervisor_seconds": round(elapsed, 6),
        "supervisor_status_samples": status_samples,
        "supervisor_heartbeat_updates": heartbeat_updates,
        "max_heartbeat_gap_seconds": round(
            max(heartbeat_gaps or (0.0,)), 6
        ),
    }
    _atomic_write(
        output_path,
        json.dumps(
            result,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii") + b"\n",
        mode=0o600,
    )


def _load_bounded_json(path, error_code):
    try:
        file_stat = os.stat(path, follow_symlinks=False)
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_size <= 0
            or file_stat.st_size > _MAX_JSON_BYTES
        ):
            raise FixtureError(error_code)
        with open(path, "r", encoding="ascii") as stream:
            value = json.load(stream)
    except FixtureError:
        raise
    except (OSError, UnicodeError, ValueError):
        raise FixtureError(error_code) from None
    if not isinstance(value, dict):
        raise FixtureError(error_code)
    return value


def _parse_docker_size(value):
    match = re.fullmatch(
        r"\s*([0-9]+(?:\.[0-9]+)?)\s*"
        r"(B|kB|KB|MB|GB|TB|KiB|MiB|GiB|TiB)\s*",
        value,
    )
    if not match:
        raise FixtureError("container_metrics_invalid")
    number = float(match.group(1))
    unit = match.group(2)
    multipliers = {
        "B": 1,
        "kB": 1000,
        "KB": 1000,
        "MB": 1000 ** 2,
        "GB": 1000 ** 3,
        "TB": 1000 ** 4,
        "KiB": 1024,
        "MiB": 1024 ** 2,
        "GiB": 1024 ** 3,
        "TiB": 1024 ** 4,
    }
    return int(number * multipliers[unit])


def _merge_container_metrics(  # noqa: C901
    status_path, stats_path, baseline_path=None, supervisor_path=None
):
    status_path = os.path.abspath(os.fspath(status_path))
    stats_path = os.path.abspath(os.fspath(stats_path))
    status = _load_bounded_json(status_path, "container_metrics_invalid")
    samples = []
    expected_header = (
        "sample_kind", "epoch_ns", "container_id", "stats_id",
        "proc_pid", "proc_start_ticks", "cgroup_sha256", "cpu_percent",
        "memory_used", "block_read", "block_write",
    )
    bound_identity = None
    previous_epoch = -1
    try:
        stats_stat = os.stat(stats_path, follow_symlinks=False)
        if (
            not stat.S_ISREG(stats_stat.st_mode)
            or stats_stat.st_size <= 0
            or stats_stat.st_size > _MAX_JSON_BYTES
        ):
            raise FixtureError("container_metrics_invalid")
        with open(stats_path, "r", encoding="ascii") as stream:
            rows = [line.rstrip("\n").split("\t") for line in stream]
        if not rows or tuple(rows[0]) != expected_header:
            raise FixtureError("container_metrics_invalid")
        for row in rows[1:]:
            if len(row) != len(expected_header):
                raise FixtureError("container_metrics_invalid")
            sample = dict(zip(expected_header, row))
            container_id = sample["container_id"]
            stats_id = sample["stats_id"]
            cgroup_sha256 = sample["cgroup_sha256"]
            if (
                sample["sample_kind"] not in ("periodic", "final")
                or not re.fullmatch(r"[0-9a-f]{64}", container_id)
                or not re.fullmatch(r"[0-9a-f]{12,64}", stats_id)
                or not container_id.startswith(stats_id)
                or sample["proc_pid"] != "1"
                or not re.fullmatch(r"[0-9a-f]{64}", cgroup_sha256)
            ):
                raise FixtureError("container_metrics_invalid")
            epoch = int(sample["epoch_ns"])
            start_ticks = int(sample["proc_start_ticks"])
            if epoch <= previous_epoch or start_ticks <= 0:
                raise FixtureError("container_metrics_invalid")
            previous_epoch = epoch
            identity = (container_id, start_ticks, cgroup_sha256)
            if bound_identity is None:
                bound_identity = identity
            elif identity != bound_identity:
                raise FixtureError("container_metrics_invalid")
            samples.append((
                float(sample["cpu_percent"]),
                _parse_docker_size(sample["memory_used"]),
                _parse_docker_size(sample["block_read"]),
                _parse_docker_size(sample["block_write"]),
                sample["sample_kind"],
            ))
    except FixtureError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError):
        raise FixtureError("container_metrics_invalid") from None
    if (
        not samples
        or sum(sample[4] == "final" for sample in samples) != 1
        or samples[-1][4] != "final"
        or not any(sample[4] == "periodic" for sample in samples)
    ):
        raise FixtureError("container_metrics_invalid")
    seconds = status.get("seconds")
    if type(seconds) not in (int, float) or seconds < 0:
        raise FixtureError("container_metrics_invalid")
    ratio = 1.0
    if baseline_path is not None:
        baseline = _load_bounded_json(
            os.path.abspath(os.fspath(baseline_path)),
            "container_metrics_invalid",
        )
        baseline_seconds = baseline.get("seconds")
        if (
            type(baseline_seconds) not in (int, float)
            or baseline_seconds <= 0
        ):
            raise FixtureError("container_metrics_invalid")
        ratio = seconds / baseline_seconds
    supervisor = None
    if supervisor_path is not None:
        supervisor = _load_bounded_json(
            os.path.abspath(os.fspath(supervisor_path)),
            "container_metrics_invalid",
        )
        expected_supervisor_fields = {
            "schema_version", "source", "state", "images_total",
            "files_total", "supervisor_seconds",
            "supervisor_status_samples", "supervisor_heartbeat_updates",
            "max_heartbeat_gap_seconds",
        }
        if (
            set(supervisor) != expected_supervisor_fields
            or supervisor.get("schema_version") != 1
            or supervisor.get("source") != "nginx_public_status"
            or supervisor.get("state") != "ready"
            or supervisor.get("images_total")
            != status.get("images_total")
            or supervisor.get("files_total") != status.get("files_total")
            or type(supervisor.get("supervisor_seconds"))
            not in (int, float)
            or supervisor["supervisor_seconds"] <= 0
            or type(supervisor.get("max_heartbeat_gap_seconds"))
            not in (int, float)
            or supervisor["max_heartbeat_gap_seconds"] < 0
            or supervisor["max_heartbeat_gap_seconds"]
            > supervisor["supervisor_seconds"]
            or type(supervisor.get("supervisor_status_samples")) is not int
            or supervisor["supervisor_status_samples"] <= 0
            or type(supervisor.get("supervisor_heartbeat_updates")) is not int
            or supervisor["supervisor_heartbeat_updates"] <= 0
        ):
            raise FixtureError("container_metrics_invalid")
    status.update({
        "cpu_average_percent": round(
            sum(sample[0] for sample in samples) / len(samples), 3
        ),
        "cpu_max_percent": round(max(sample[0] for sample in samples), 3),
        "max_rss_bytes": max(sample[1] for sample in samples),
        "disk_read_bytes": max(sample[2] for sample in samples),
        "disk_write_bytes": max(sample[3] for sample in samples),
        "ratio": round(ratio, 6),
        "container_id": bound_identity[0],
        "container_samples": len(samples),
        "final_sample_preserved": True,
    })
    if supervisor is not None:
        status.update({
            "supervisor_seconds": supervisor["supervisor_seconds"],
            "supervisor_status_samples": (
                supervisor["supervisor_status_samples"]
            ),
            "supervisor_heartbeat_updates": (
                supervisor["supervisor_heartbeat_updates"]
            ),
            "max_heartbeat_gap_seconds": (
                supervisor["max_heartbeat_gap_seconds"]
            ),
            "heartbeat_source": "nginx_public_status",
        })
    _atomic_write(
        status_path,
        json.dumps(
            status,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii") + b"\n",
        mode=0o600,
    )


def _corrupt_migration_manifest(data_root):
    data_root = os.path.abspath(os.fspath(data_root))
    matches = []
    for root, directories, files in os.walk(data_root, followlinks=False):
        directories[:] = sorted(
            name for name in directories
            if not os.path.islink(os.path.join(root, name))
        )
        if _MEDIA_MANIFEST in files:
            candidate = os.path.join(root, _MEDIA_MANIFEST)
            candidate_stat = os.stat(candidate, follow_symlinks=False)
            if stat.S_ISREG(candidate_stat.st_mode):
                matches.append(candidate)
    if len(matches) != 1:
        raise FixtureError("migration_manifest_target_invalid")
    _atomic_write(matches[0], b'{"corrupt":\n', mode=0o600)


def _argument_value(callable_value, arguments, keywords, name):
    try:
        return inspect.signature(callable_value).bind_partial(
            *arguments, **keywords
        ).arguments[name]
    except (KeyError, TypeError, ValueError):
        raise FixtureError("linear_metrics_instrumentation_invalid") from None


def _patched_callable(owner, name, wrapper_factory):
    descriptor = owner.__dict__.get(name)
    original = getattr(owner, name)
    wrapped = wrapper_factory(original)
    if isinstance(descriptor, staticmethod):
        replacement = staticmethod(wrapped)
    elif isinstance(descriptor, classmethod):
        replacement = classmethod(wrapped)
    else:
        replacement = wrapped
    return mock.patch.object(owner, name, replacement)


def _time_yielded_items(owner, name, durations):
    def factory(original):
        @wraps(original)
        def measured(*arguments, **keywords):
            for item in original(*arguments, **keywords):
                started = time.monotonic()
                yield item
                elapsed = time.monotonic() - started
                if elapsed < 0:
                    raise FixtureError(
                        "linear_metrics_instrumentation_invalid"
                    )
                durations.append(elapsed)

        return measured

    return _patched_callable(owner, name, factory)


def _count_method(owner, name, counters, counter_name):
    def factory(original):
        @wraps(original)
        def counted(*arguments, **keywords):
            counters[counter_name] += 1
            return original(*arguments, **keywords)

        return counted

    return _patched_callable(owner, name, factory)


def _count_keyed_method(owner, name, counter, key_argument):
    def factory(original):
        @wraps(original)
        def counted(*arguments, **keywords):
            key = _argument_value(
                original, arguments, keywords, key_argument
            )
            if not isinstance(key, str) or not key:
                raise FixtureError("linear_metrics_instrumentation_invalid")
            counter[key] += 1
            return original(*arguments, **keywords)

        return counted

    return _patched_callable(owner, name, factory)


def _count_stream_scope(owner, name, counter, key_argument):
    def factory(original):
        @wraps(original)
        def counted(*arguments, **keywords):
            key = _argument_value(
                original, arguments, keywords, key_argument
            )
            if not isinstance(key, str) or not key:
                raise FixtureError("linear_metrics_instrumentation_invalid")
            stack = _ACTIVE_LINEAR_FILE_KEYS.get()
            token = _ACTIVE_LINEAR_FILE_KEYS.set(stack + (key,))
            counter[key] += 1
            try:
                return original(*arguments, **keywords)
            finally:
                if _ACTIVE_LINEAR_FILE_KEYS.get() != stack + (key,):
                    raise FixtureError(
                        "linear_metrics_instrumentation_invalid"
                    )
                _ACTIVE_LINEAR_FILE_KEYS.reset(token)

        return counted

    return _patched_callable(owner, name, factory)


def _count_source_bytes(owner, name, counter, key_argument):
    def factory(original):
        @wraps(original)
        def counted(*arguments, **keywords):
            key = _argument_value(
                original, arguments, keywords, key_argument
            )
            active = _ACTIVE_LINEAR_FILE_KEYS.get()
            if not active or active[-1] != key:
                raise FixtureError("linear_metrics_instrumentation_invalid")
            for chunk in original(*arguments, **keywords):
                if not isinstance(chunk, bytes) or not chunk:
                    raise FixtureError(
                        "linear_metrics_instrumentation_invalid"
                    )
                counter[key] += len(chunk)
                yield chunk

        return counted

    return _patched_callable(owner, name, factory)


def _count_parser_close_by_active_file_key(owner, counter):
    def factory(original):
        @wraps(original)
        def counted(*arguments, **keywords):
            active = _ACTIVE_LINEAR_FILE_KEYS.get()
            if not active:
                raise FixtureError("linear_metrics_instrumentation_invalid")
            counter[active[-1]] += 1
            return original(*arguments, **keywords)

        return counted

    return _patched_callable(owner, "close", factory)


def _run_coordinator_for_fixture(
    data_root, fault_injector=None, connection=None, progress_reporter=None
):
    owns_connection = connection is None
    if owns_connection:
        connection, _database_path, _media_root = _configure_django(data_root)
    from django.core.management import call_command
    from django_images.services.legacy_startup import LegacyStartupCoordinator

    coordinator = LegacyStartupCoordinator(
        os.geteuid(),
        os.getegid(),
        fault_injector=fault_injector,
        progress_reporter=(
            progress_reporter
            if progress_reporter is not None
            else lambda _event: None
        ),
    )
    run = coordinator.prepare_before_schema()
    coordinator.prepare_migration_locks(run)
    if coordinator.schema_required(run):
        call_command("migrate", interactive=False, verbosity=0)
    coordinator.converge_after_schema(run)
    if owns_connection:
        connection.close()


def _process_io_bytes():
    counters = {"read_bytes": 0, "write_bytes": 0}
    try:
        with open("/proc/self/io", "r", encoding="ascii") as stream:
            for line in stream:
                name, separator, value = line.partition(":")
                if separator and name in counters:
                    counters[name] = int(value.strip())
    except (OSError, UnicodeError, ValueError):
        pass
    return counters


def _percentile(values, percentile):
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = max(
        0, min(len(ordered) - 1, (len(ordered) * percentile + 99) // 100 - 1)
    )
    return ordered[index]


def _benchmark_linear_service(
    data_root, output_path, completion_hold_seconds=0.0
):
    if (
        type(completion_hold_seconds) not in (int, float)
        or completion_hold_seconds < 0
        or completion_hold_seconds > 10
    ):
        raise FixtureError("linear_metrics_hold_invalid")
    data_root = os.path.abspath(os.fspath(data_root))
    output_path = _contained_path(output_path, data_root)
    counters = {
        "source_bytes": collections.Counter(),
        "source_full_reads": collections.Counter(),
        "resume_rehash_reads": collections.Counter(),
        "pillow_decodes": collections.Counter(),
        "old_manifest_replays": 0,
        "linear_journal_replays": 0,
        "process_starts": 1,
    }
    connection, _database_path, _media_root = _configure_django(data_root)
    from django.test.utils import CaptureQueriesContext
    from PIL import ImageFile
    from core.services.media_asset_backfill import (
        MediaAssetBackfiller,
        _BackfillManifestLog,
    )
    from django_images.services.media_migration_v2 import (
        AutoV2ManifestLog,
        AutoV2MediaMigrator,
    )
    from django_images.services.migration_batch_log import (
        MigrationBatchJournal,
    )

    started = time.monotonic()
    batch_seconds = []

    cpu_started = time.process_time()
    io_started = _process_io_bytes()
    with CaptureQueriesContext(connection) as queries, ExitStack() as stack:
        stack.enter_context(_count_stream_scope(
            AutoV2MediaMigrator,
            "_stream_source_to_staging_and_inspect",
            counters["source_full_reads"],
            "file_key",
        ))
        stack.enter_context(_count_source_bytes(
            AutoV2MediaMigrator,
            "_iter_source_chunks",
            counters["source_bytes"],
            "file_key",
        ))
        stack.enter_context(_count_keyed_method(
            AutoV2MediaMigrator,
            "_rehash_resume_candidate",
            counters["resume_rehash_reads"],
            "file_key",
        ))
        stack.enter_context(_count_parser_close_by_active_file_key(
            ImageFile.Parser, counters["pillow_decodes"]
        ))
        stack.enter_context(_count_method(
            AutoV2ManifestLog,
            "_load_state",
            counters,
            "old_manifest_replays",
        ))
        stack.enter_context(_count_method(
            _BackfillManifestLog,
            "_load_state",
            counters,
            "old_manifest_replays",
        ))
        stack.enter_context(_count_method(
            MigrationBatchJournal,
            "_load_state",
            counters,
            "linear_journal_replays",
        ))
        stack.enter_context(_time_yielded_items(
            AutoV2MediaMigrator, "_build_batches", batch_seconds
        ))
        stack.enter_context(_time_yielded_items(
            MediaAssetBackfiller,
            "_build_backfill_batches",
            batch_seconds,
        ))
        _run_coordinator_for_fixture(
            data_root,
            connection=connection,
        )
    elapsed = max(time.monotonic() - started, 0.000001)
    cpu_percent = (time.process_time() - cpu_started) * 100.0 / elapsed
    io_finished = _process_io_bytes()
    connection.close()
    result = dict(counters)
    for key in (
        "source_bytes", "source_full_reads", "resume_rehash_reads",
        "pillow_decodes",
    ):
        result[key] = dict(sorted(result[key].items()))
    receipt, _payload, _file_stat = _load_json_file(
        os.path.join(data_root, "linear-receipt.json")
    )
    result.update({
        "state": "ready",
        "images": receipt["images_total"],
        "images_total": receipt["images_total"],
        "files_total": receipt["files_total"],
        "images_done": receipt["images_total"],
        "files_done": receipt["files_total"],
        "seconds": round(elapsed, 3),
        "queries": len(queries),
        "cpu_average_percent": round(cpu_percent, 3),
        "cpu_max_percent": round(cpu_percent, 3),
        "disk_read_bytes": max(
            io_finished["read_bytes"] - io_started["read_bytes"], 0
        ),
        "disk_write_bytes": max(
            io_finished["write_bytes"] - io_started["write_bytes"], 0
        ),
        "max_rss_bytes": int(
            resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            * (1024 if sys.platform.startswith("linux") else 1)
        ),
        "ratio": 1.0,
        "batch_seconds_p50": round(_percentile(batch_seconds, 50), 6),
        "batch_seconds_p95": round(_percentile(batch_seconds, 95), 6),
        "batch_seconds_max": round(max(batch_seconds or (0.0,)), 6),
        "batch_sample_count": len(batch_seconds),
        "seconds_source": "fixture_coordinator",
        "batch_timing_source": "generator_yield_lifetime",
        "heartbeat_source": "not_measured",
        "max_heartbeat_gap_seconds": 0.0,
    })
    _atomic_write(
        output_path,
        json.dumps(
            result,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii") + b"\n",
        mode=0o600,
    )
    if completion_hold_seconds:
        time.sleep(completion_hold_seconds)


def _linear_journal(data_root, allow_torn_tail=False):
    matches = glob.glob(os.path.join(
        data_root, "**", "linear-migration-v1.jsonl"
    ), recursive=True)
    if len(matches) != 1:
        raise FixtureError("linear_journal_missing")
    raw, _file_stat = _read_regular_file(matches[0])
    lines = raw.splitlines(keepends=True)
    events = []
    for index, line in enumerate(lines):
        if not line.endswith(b"\n"):
            if allow_torn_tail and index == len(lines) - 1:
                break
            raise FixtureError("linear_journal_invalid")
        try:
            frame = json.loads(line.decode("ascii"))
            payload = frame["payload"]
            canonical = json.dumps(
                payload, ensure_ascii=True, sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
            if hashlib.sha256(canonical).hexdigest() != frame["checksum"]:
                raise ValueError
        except (KeyError, TypeError, ValueError, UnicodeError):
            raise FixtureError("linear_journal_invalid") from None
        events.append(payload)
    return matches[0], events


def _wait_for_fixture_exit(child_pid, expected):
    waited_pid, wait_status = os.waitpid(child_pid, 0)
    if (
        waited_pid != child_pid
        or not os.WIFEXITED(wait_status)
        or os.WEXITSTATUS(wait_status) != expected
    ):
        raise FixtureError("linear_fixture_fault_not_observed")


def _journal_ordinals(events):
    commits = [
        event for event in events if event.get("event") == "batch_commit"
    ]
    return [
        {
            "ordinal": ordinal,
            "batch_id": event.get("batch_id"),
            "sha256": hashlib.sha256(json.dumps(
                event, ensure_ascii=True, sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")).hexdigest(),
        }
        for ordinal, event in enumerate(commits, 1)
    ]


def _prepare_linear_resume(data_root, output_path):
    if not sys.platform.startswith("linux") or not hasattr(os, "fork"):
        raise FixtureError("linear_crash_linux_required")
    data_root = os.path.abspath(os.fspath(data_root))
    output_path = _contained_path(output_path, data_root)
    child_pid = os.fork()
    if child_pid == 0:
        def stop_after_publication(observed):
            if observed == "after_destination_rename":
                os._exit(97)

        try:
            _run_coordinator_for_fixture(
                data_root, fault_injector=stop_after_publication
            )
        except BaseException:
            os._exit(96)
        os._exit(0)
    _wait_for_fixture_exit(child_pid, 97)
    receipt, _payload, _file_stat = _load_json_file(
        os.path.join(data_root, "linear-receipt.json")
    )
    expected_keys = sorted(
        item["file_key"] for item in receipt["expected_files"]
    )
    media_root = os.path.join(data_root, "static", "media")
    published = []
    for root_name in ("originals", "derivatives"):
        root = os.path.join(media_root, root_name)
        for directory, _names, filenames in os.walk(root):
            published.extend(
                os.path.join(directory, filename) for filename in filenames
            )
    if not expected_keys or len(published) != len(expected_keys):
        raise FixtureError("linear_resume_precondition_failed")
    value = {
        "schema_version": 1,
        "expected_resume_rehash_keys": expected_keys,
    }
    _atomic_write(
        output_path,
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode(
            "ascii"
        ) + b"\n",
        mode=0o600,
    )


def _replace_with_corrupt_payload(path):
    current, current_stat = _read_regular_file(path)
    if not current:
        raise FixtureError("linear_repair_precondition_failed")
    replacement = os.path.join(
        os.path.dirname(path), ".fixture-corrupt-{}.tmp".format(
            uuid.uuid4().hex
        )
    )
    descriptor = os.open(
        replacement,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        corrupt = bytes(bytearray((value ^ 0x5A) for value in current))
        _write_all(descriptor, corrupt)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(replacement, path)
    directory_descriptor = os.open(
        os.path.dirname(path),
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
    replaced = os.stat(path, follow_symlinks=False)
    if replaced.st_ino == current_stat.st_ino:
        raise FixtureError("linear_repair_precondition_failed")
    return current, replaced


def _prepare_committed_repair(data_root, output_path):
    if not sys.platform.startswith("linux") or not hasattr(os, "fork"):
        raise FixtureError("linear_crash_linux_required")
    data_root = os.path.abspath(os.fspath(data_root))
    output_path = _contained_path(output_path, data_root)
    child_pid = os.fork()
    if child_pid == 0:
        def stop_after_commit(observed):
            if observed == "after_batch_commit":
                os._exit(97)

        try:
            _run_coordinator_for_fixture(
                data_root, fault_injector=stop_after_commit
            )
        except BaseException:
            os._exit(96)
        os._exit(0)
    _wait_for_fixture_exit(child_pid, 97)
    _journal_path, events = _linear_journal(data_root)
    intents = [
        event for event in events if event.get("event") == "batch_intent"
    ]
    commits = [
        event for event in events if event.get("event") == "batch_commit"
    ]
    repairs = [
        event for event in events if event.get("event") == "batch_repair"
    ]
    try:
        receipt = intents[0]["intent"]["receipts"][0]
        relative_path = receipt["relative_path"]
        file_key = receipt["file_key"]
        expected_sha256 = receipt["sha256"]
        expected_size = receipt["size"]
    except (IndexError, KeyError, TypeError):
        raise FixtureError("linear_repair_precondition_failed") from None
    if len(intents) != 1 or len(commits) != 1 or repairs:
        raise FixtureError("linear_repair_precondition_failed")
    media_root = os.path.join(data_root, "static", "media")
    destination = _contained_path(
        os.path.join(media_root, relative_path), media_root
    )
    original, corrupted_stat = _replace_with_corrupt_payload(destination)
    if (
        len(original) != expected_size
        or hashlib.sha256(original).hexdigest() != expected_sha256
    ):
        raise FixtureError("linear_repair_precondition_failed")
    value = {
        "schema_version": 1,
        "file_key": file_key,
        "relative_path": relative_path,
        "expected_size": expected_size,
        "expected_sha256": expected_sha256,
        "corrupt_destination_identity": {
            "device": corrupted_stat.st_dev,
            "inode": corrupted_stat.st_ino,
        },
        "intent_count": len(intents),
        "commit_count": len(commits),
        "repair_count": len(repairs),
        "commit_ordinals": _journal_ordinals(events),
    }
    _atomic_write(
        output_path,
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode(
            "ascii"
        ) + b"\n",
        mode=0o600,
    )


def _run_linear_repair_only(data_root, observation_path):
    data_root = os.path.abspath(os.fspath(data_root))
    observation_path = _contained_path(observation_path, data_root)
    before, _payload, _file_stat = _load_json_file(observation_path)
    _run_coordinator_for_fixture(data_root)
    _journal_path, events = _linear_journal(data_root)
    intents = [
        event for event in events if event.get("event") == "batch_intent"
    ]
    commits = [
        event for event in events if event.get("event") == "batch_commit"
    ]
    repairs = [
        event for event in events if event.get("event") == "batch_repair"
    ]
    destination = os.path.join(
        data_root, "static", "media", before["relative_path"]
    )
    destination_stat = os.stat(destination, follow_symlinks=False)
    if (
        len(intents) != before["intent_count"]
        or len(commits) != before["commit_count"]
        or len(repairs) != before["repair_count"] + 1
        or _journal_ordinals(events) != before["commit_ordinals"]
        or destination_stat.st_size != before["expected_size"]
        or destination_stat.st_ino
        == before["corrupt_destination_identity"]["inode"]
    ):
        raise FixtureError("linear_repair_contract_invalid")


def _prepare_linear_repair(data_root):
    child_pid = os.fork()
    if child_pid == 0:
        def crash_after_database_commit(observed):
            if observed == "after_database_commit":
                os._exit(97)

        try:
            _run_coordinator_for_fixture(
                data_root,
                fault_injector=crash_after_database_commit,
            )
        except BaseException:
            os._exit(96)
        os._exit(0)
    waited_pid, wait_status = os.waitpid(child_pid, 0)
    if (
        waited_pid != child_pid
        or not os.WIFEXITED(wait_status)
        or os.WEXITSTATUS(wait_status) != 97
    ):
        raise FixtureError("linear_repair_precondition_failed")

    connection, _database_path, media_root = _configure_django(data_root)
    from django_images.models import Image

    image = Image.objects.order_by("pk").first()
    if image is None or not isinstance(image.image.name, str):
        connection.close()
        raise FixtureError("linear_repair_precondition_failed")
    destination = _contained_path(
        os.path.join(media_root, image.image.name), media_root
    )
    try:
        destination_stat = os.stat(destination, follow_symlinks=False)
        if not stat.S_ISREG(destination_stat.st_mode):
            raise FixtureError("linear_repair_precondition_failed")
        with open(destination, "rb") as stream:
            payload = stream.read()
        directory = os.path.dirname(destination)
        replacement = os.path.join(
            directory, ".fixture-repair-{}.tmp".format(uuid.uuid4().hex)
        )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(replacement, flags, 0o600)
        try:
            _write_all(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(replacement, destination)
        directory_descriptor = os.open(
            directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        replaced_stat = os.stat(destination, follow_symlinks=False)
        if replaced_stat.st_ino == destination_stat.st_ino:
            raise FixtureError("linear_repair_precondition_failed")
        _journal_path, events = _linear_journal(data_root)
        return {
            "destination": destination,
            "replacement_inode": replaced_stat.st_ino,
            "commit_order": [
                event.get("batch_id") for event in events
                if event.get("event") == "batch_commit"
            ],
        }
    except FixtureError:
        raise
    except OSError:
        raise FixtureError("linear_repair_precondition_failed") from None
    finally:
        connection.close()


def _commit_order_matches(expected_order, events):
    return [
        event.get("batch_id") for event in events
        if event.get("event") == "batch_commit"
    ] == expected_order


def _verify_checksum_failure(
    error, journal_path, expected_raw, expected_stat
):
    from django_images.services.legacy_startup import LegacyStartupError
    from django_images.services.migration_batch_log import (
        MigrationBatchLogError,
    )

    if (
        not isinstance(error, LegacyStartupError)
        or error.code != "linear_journal_invalid"
        or not isinstance(error.__cause__, MigrationBatchLogError)
        or error.__cause__.code != "linear_journal_invalid"
    ):
        raise FixtureError("linear_checksum_failure_invalid")
    try:
        current_raw, current_stat = _read_regular_file(journal_path)
    except FixtureError:
        raise FixtureError("linear_checksum_journal_changed") from None
    expected_identity = (
        expected_stat.st_dev,
        expected_stat.st_ino,
        expected_stat.st_size,
        stat.S_IMODE(expected_stat.st_mode),
    )
    current_identity = (
        current_stat.st_dev,
        current_stat.st_ino,
        current_stat.st_size,
        stat.S_IMODE(current_stat.st_mode),
    )
    if current_raw != expected_raw or current_identity != expected_identity:
        raise FixtureError("linear_checksum_journal_changed")


def _crash_linear_coordinator(data_root, fault_point):  # noqa: C901
    if fault_point not in _LINEAR_CRASH_POINTS:
        raise FixtureError("linear_fault_point_invalid")
    if not sys.platform.startswith("linux") or not hasattr(os, "fork"):
        raise FixtureError("linear_crash_linux_required")
    requested = _LINEAR_FAULT_ALIASES.get(fault_point, fault_point)
    child_pid = os.fork()
    if child_pid == 0:
        def inject(observed):
            if observed == requested:
                os._exit(99)

        try:
            if (
                fault_point.startswith("journal_")
                and fault_point != "journal_checksum_invalid"
            ):
                _configure_django(data_root)
                from django_images.services import migration_batch_log

                parts = fault_point.split("_")
                event = "batch_{}".format(parts[1])
                mode = "_".join(parts[2:])
                original_write_all = migration_batch_log._write_all
                original_fsync = migration_batch_log._durable_fsync
                pending = {"descriptor": None}

                def frame_event(value):
                    try:
                        return json.loads(
                            value.rstrip(b"\n").decode("utf-8")
                        )["payload"]["event"]
                    except (KeyError, TypeError, ValueError, UnicodeError):
                        return None

                def crash_write_all(descriptor, value):
                    if frame_event(value) != event:
                        return original_write_all(descriptor, value)
                    if mode == "partial_write":
                        written = os.write(
                            descriptor, value[:max(1, len(value) // 2)]
                        )
                        if written <= 0:
                            os._exit(98)
                        os._exit(99)
                    original_write_all(descriptor, value)
                    pending["descriptor"] = descriptor

                def crash_fsync(descriptor, reason):
                    if (
                        mode == "full_write_before_fsync"
                        and descriptor == pending["descriptor"]
                        and reason == "journal_{}".format(parts[1])
                    ):
                        os._exit(99)
                    return original_fsync(descriptor, reason)

                with mock.patch.object(
                    migration_batch_log, "_write_all", crash_write_all
                ), mock.patch.object(
                    migration_batch_log, "_durable_fsync", crash_fsync
                ):
                    _run_coordinator_for_fixture(data_root)
            else:
                _run_coordinator_for_fixture(
                    data_root, fault_injector=inject
                )
        except BaseException:
            os._exit(98)
        os._exit(0)
    waited_pid, wait_status = os.waitpid(child_pid, 0)
    if (
        waited_pid != child_pid
        or not os.WIFEXITED(wait_status)
        or os.WEXITSTATUS(wait_status) != 99
    ):
        raise FixtureError("linear_fault_not_observed")


def _verify_linear_crash_resume_contract(
    fault_point, before_prefix, after_raw, tail_repairs
):
    expected_tail_repairs = (
        1 if fault_point.endswith("_partial_write") else 0
    )
    if (
        not after_raw.startswith(before_prefix)
        or tail_repairs != expected_tail_repairs
    ):
        raise FixtureError("linear_crash_resume_invalid")


def _exercise_linear_crash(  # noqa: C901
    data_root, fault_point, output_path
):
    data_root = os.path.abspath(os.fspath(data_root))
    output_path = _contained_path(output_path, data_root)
    repair_before = None
    if fault_point.startswith("journal_repair_"):
        repair_before = _prepare_linear_repair(data_root)
    _crash_linear_coordinator(data_root, fault_point)
    journal_path, before_events = _linear_journal(
        data_root, allow_torn_tail=True
    )
    before_raw, _before_stat = _read_regular_file(journal_path)
    before_prefix = b"".join(
        before_raw.splitlines(keepends=True)[:len(before_events)]
    )
    if fault_point == "journal_checksum_invalid":
        raw, _file_stat = _read_regular_file(journal_path)
        lines = raw.splitlines(keepends=True)
        frame = json.loads(lines[-1].decode("ascii"))
        frame["checksum"] = "0" * 64
        lines[-1] = json.dumps(
            frame, ensure_ascii=True, sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii") + b"\n"
        _atomic_write(journal_path, b"".join(lines), mode=0o600)
        expected_raw, expected_stat = _read_regular_file(journal_path)
        try:
            _run_coordinator_for_fixture(data_root)
        except Exception as error:
            _verify_checksum_failure(
                error, journal_path, expected_raw, expected_stat
            )
            result = {
                "schema_version": 1,
                "fault": fault_point,
                "child_exit_code": 99,
                "resumed": False,
                "checksum_fail_closed": True,
                "error_code": "linear_journal_invalid",
                "journal_unchanged": True,
            }
            _atomic_write(
                output_path,
                json.dumps(result, sort_keys=True,
                           separators=(",", ":")).encode("ascii") + b"\n",
                mode=0o600,
            )
            return
        raise FixtureError("linear_checksum_fail_open")
    _configure_django(data_root)
    from django_images.services import migration_batch_log
    original_fsync = migration_batch_log._durable_fsync
    tail_repairs = {"count": 0}

    def count_fsync(descriptor, reason):
        if reason == "journal_tail_repair":
            tail_repairs["count"] += 1
        return original_fsync(descriptor, reason)

    with mock.patch.object(
        migration_batch_log, "_durable_fsync", count_fsync
    ):
        _run_coordinator_for_fixture(data_root)
    _journal_path, after_events = _linear_journal(data_root)
    after_raw, _after_stat = _read_regular_file(journal_path)
    _verify_linear_crash_resume_contract(
        fault_point,
        before_prefix,
        after_raw,
        tail_repairs["count"],
    )
    receipt_path = os.path.join(data_root, "linear-receipt.json")
    _verify_migration(data_root, receipt_path)
    result = {
        "schema_version": 1,
        "fault": fault_point,
        "child_exit_code": 99,
        "resumed": True,
        "tail_repairs": tail_repairs["count"],
        "journal_prefix_size": len(before_prefix),
        "journal_prefix_sha256": hashlib.sha256(
            before_prefix
        ).hexdigest(),
        "journal_prefix_preserved": True,
        "fault_event_count_before": len(before_events),
        "fault_event_count_after": len(after_events),
        "duplicate_fault_event": any(
            sum(
                candidate.get("event") == event.get("event")
                and candidate.get("batch_id") == event.get("batch_id")
                for candidate in after_events
            ) > 1
            for event in before_events
            if event.get("event") in (
                "batch_intent", "batch_commit", "batch_repair"
            )
        ),
        "commit_ordinal_unchanged": (
            repair_before is None
            or _commit_order_matches(
                repair_before["commit_order"], after_events
            )
        ),
        "repair_destination_inode_unchanged": (
            repair_before is None
            or os.stat(
                repair_before["destination"], follow_symlinks=False
            ).st_ino == repair_before["replacement_inode"]
        ),
    }
    _atomic_write(
        output_path,
        json.dumps(
            result,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii") + b"\n",
        mode=0o600,
    )


def _load_tail_repair_observation(
    data_root, observation_path, verify_journal=False
):
    observation_path = _contained_path(observation_path, data_root)
    observation, _raw, observation_stat = _load_json_file(
        observation_path, maximum=65536
    )
    expected_keys = {
        "schema_version", "fault", "journal_identity", "torn_size",
        "last_valid_offset", "valid_prefix_sha256",
        "torn_journal_sha256",
    }
    identity = observation.get("journal_identity")
    valid_hash = re.compile(r"^[0-9a-f]{64}$")
    if (
        set(observation) != expected_keys
        or observation.get("schema_version") != 1
        or observation.get("fault") not in _LINEAR_TAIL_AUDIT_POINTS
        or not isinstance(identity, dict)
        or set(identity) != {"device", "inode"}
        or type(identity.get("device")) is not int
        or type(identity.get("inode")) is not int
        or identity["device"] < 0
        or identity["inode"] <= 0
        or type(observation.get("torn_size")) is not int
        or type(observation.get("last_valid_offset")) is not int
        or not 0 < observation["last_valid_offset"]
        < observation["torn_size"]
        or not valid_hash.fullmatch(
            observation.get("valid_prefix_sha256", "")
        )
        or not valid_hash.fullmatch(
            observation.get("torn_journal_sha256", "")
        )
        or stat.S_IMODE(observation_stat.st_mode) != 0o600
    ):
        raise FixtureError("linear_tail_observation_invalid")
    if verify_journal:
        journal_path, _events = _linear_journal(
            data_root, allow_torn_tail=True
        )
        raw, journal_stat = _read_regular_file(journal_path)
        offset = observation["last_valid_offset"]
        if (
            (journal_stat.st_dev, journal_stat.st_ino)
            != (identity["device"], identity["inode"])
            or len(raw) != observation["torn_size"]
            or raw.endswith(b"\n")
            or raw.rfind(b"\n") + 1 != offset
            or hashlib.sha256(raw[:offset]).hexdigest()
            != observation["valid_prefix_sha256"]
            or hashlib.sha256(raw).hexdigest()
            != observation["torn_journal_sha256"]
        ):
            raise FixtureError("linear_tail_observation_invalid")
    return observation


def _prepare_linear_tail_repair(data_root, fault_point, output_path):
    data_root = os.path.abspath(os.fspath(data_root))
    output_path = _contained_path(output_path, data_root)
    if fault_point not in _LINEAR_TAIL_AUDIT_POINTS:
        raise FixtureError("linear_fault_point_invalid")
    _crash_linear_coordinator(data_root, fault_point)
    journal_path, _events = _linear_journal(
        data_root, allow_torn_tail=True
    )
    raw, journal_stat = _read_regular_file(journal_path)
    last_valid_offset = raw.rfind(b"\n") + 1
    if (
        not 0 < last_valid_offset < len(raw)
        or raw.endswith(b"\n")
    ):
        raise FixtureError("linear_tail_observation_invalid")
    observation = {
        "schema_version": 1,
        "fault": fault_point,
        "journal_identity": {
            "device": journal_stat.st_dev,
            "inode": journal_stat.st_ino,
        },
        "torn_size": len(raw),
        "last_valid_offset": last_valid_offset,
        "valid_prefix_sha256": hashlib.sha256(
            raw[:last_valid_offset]
        ).hexdigest(),
        "torn_journal_sha256": hashlib.sha256(raw).hexdigest(),
    }
    _atomic_write(
        output_path,
        json.dumps(
            observation, ensure_ascii=True, sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii") + b"\n",
        mode=0o600,
    )
    _load_tail_repair_observation(
        data_root, output_path, verify_journal=True
    )


def _run_linear_tail_repair_only(
    data_root, observation_path, output_path
):
    data_root = os.path.abspath(os.fspath(data_root))
    output_path = _contained_path(output_path, data_root)
    observation = _load_tail_repair_observation(
        data_root, observation_path, verify_journal=True
    )
    _journal_path, before_events = _linear_journal(
        data_root, allow_torn_tail=True
    )
    _configure_django(data_root)
    from django_images.services import migration_batch_log
    original_fsync = migration_batch_log._durable_fsync
    tail_repairs = {"count": 0}

    def count_fsync(descriptor, reason):
        if reason == "journal_tail_repair":
            tail_repairs["count"] += 1
        return original_fsync(descriptor, reason)

    with mock.patch.object(
        migration_batch_log, "_durable_fsync", count_fsync
    ):
        _run_coordinator_for_fixture(data_root)
    _journal_path, after_events = _linear_journal(data_root)
    _verify_migration(
        data_root, os.path.join(data_root, "linear-receipt.json")
    )
    result = {
        "schema_version": 1,
        "fault": observation["fault"],
        "resumed": True,
        "tail_repairs": tail_repairs["count"],
        "duplicate_fault_event": any(
            sum(
                candidate.get("event") == event.get("event")
                and candidate.get("batch_id") == event.get("batch_id")
                for candidate in after_events
            ) > 1
            for event in before_events
            if event.get("event") in (
                "batch_intent", "batch_commit", "batch_repair"
            )
        ),
    }
    if result["tail_repairs"] != 1 or result["duplicate_fault_event"]:
        raise FixtureError("linear_tail_repair_invalid")
    _atomic_write(
        output_path,
        json.dumps(
            result, ensure_ascii=True, sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii") + b"\n",
        mode=0o600,
    )


def _audit_linear_io(  # noqa: C901
    data_root, output_path, mode="fresh", observation_path=None,
    trace_root=None,
):
    data_root = os.path.abspath(os.fspath(data_root))
    output_path = _contained_path(output_path, data_root)
    if trace_root is None:
        trace_root = os.path.join(data_root, "strace")
    else:
        trace_root = _contained_path(trace_root, data_root)
    trace_files = sorted(glob.glob(os.path.join(trace_root, "io.*")))
    if not trace_files:
        raise FixtureError("linear_strace_missing")
    receipt, _payload, _file_stat = _load_json_file(
        os.path.join(data_root, "linear-receipt.json")
    )
    media_root = os.path.join(data_root, "static", "media")
    source_path_to_key = {}
    source_identity_by_path = {}
    source_suffixes = set()
    for item in receipt["expected_files"]:
        relative_path = item["relative_path"].lstrip("/")
        source_path = _contained_path(
            os.path.join(media_root, relative_path), media_root
        )
        identity = item["source_identity"]
        if (
            type(identity.get("device")) is not int
            or type(identity.get("inode")) is not int
            or type(identity.get("size")) is not int
            or min(identity.values()) < 0
            or identity["inode"] == 0
        ):
            raise FixtureError("linear_strace_source_invalid")
        source_path_to_key[source_path] = item["file_key"]
        source_identity_by_path[source_path] = identity
        source_suffixes.add("/{}".format(relative_path))
    if mode not in ("fresh", "repair", "tail-repair"):
        raise FixtureError("linear_strace_mode_invalid")
    repair_observation = None
    tail_observation = None
    destination_path_to_key = {}
    destination_identity_by_path = {}
    destination_size_by_path = {}
    if mode == "repair":
        if observation_path is None:
            raise FixtureError("linear_strace_repair_observation_missing")
        observation_path = _contained_path(observation_path, data_root)
        repair_observation, _raw, _file_stat = _load_json_file(
            observation_path
        )
        try:
            repair_relative = repair_observation["relative_path"]
            repair_key = repair_observation["file_key"]
            repair_size = repair_observation["expected_size"]
            corrupt_identity = repair_observation[
                "corrupt_destination_identity"
            ]
        except (KeyError, TypeError):
            raise FixtureError("linear_strace_repair_observation_invalid") \
                from None
        repair_path = _contained_path(
            os.path.join(media_root, repair_relative), media_root
        )
        destination_path_to_key[repair_path] = repair_key
        destination_identity_by_path[repair_path] = corrupt_identity
        destination_size_by_path[repair_path] = repair_size
    elif mode == "tail-repair":
        if observation_path is None:
            raise FixtureError("linear_tail_observation_invalid")
        tail_observation = _load_tail_repair_observation(
            data_root, observation_path, verify_journal=False
        )
    result = {
        "actual_fsync_calls": 0,
        "successful_fsync_calls": 0,
        "actual_syncfs_calls": 0,
        "successful_syncfs_calls": 0,
        "journal_event_fsyncs": 0,
        "journal_intent_fsyncs": 0,
        "journal_commit_fsyncs": 0,
        "journal_repair_fsyncs": 0,
        "journal_tail_repair_fsyncs": 0,
        "journal_tail_truncate_count": 0,
        "journal_tail_truncate_offset": None,
        "max_journal_event_bytes": 0,
        "publication_directory_fsyncs": 0,
        "publication_staging_directory_fsyncs": 0,
        "publication_destination_directory_fsyncs": 0,
        "checkpoint_file_fsyncs": 0,
        "checkpoint_directory_fsyncs": 0,
        "checkpoint_events": [],
        "batch_file_syncfs": 0,
        "source_read_bytes": collections.Counter(),
        "source_read_passes": collections.Counter(),
        "destination_read_bytes": collections.Counter(),
        "destination_read_passes": collections.Counter(),
        "publication_mutation_windows": [],
    }
    fd_reads = collections.Counter()
    fd_read_keys = {}
    fd_read_kinds = {}
    fd_targets = {}
    journal_buffers = collections.defaultdict(bytes)
    pending_journal_events = collections.defaultdict(list)
    pending_journal_truncates = {}
    tail_fsync_expected = None
    staging_root = os.path.join(media_root, ".staging")
    destination_roots = (
        os.path.join(media_root, "originals"),
        os.path.join(media_root, "derivatives"),
    )
    legacy_backup_root = os.path.join(data_root, "legacy-backup")
    checkpoint_filename = "linear-migration-checkpoint-v1.json"
    checkpoint_temporary_pattern = re.compile(
        r"^\.linear-migration-checkpoint-v1\.json\."
        r"[0-9a-f]{32}\.tmp$"
    )
    pending_checkpoint_events = collections.deque()
    checkpoint_fsynced_temporary_paths = {}
    checkpoint_path_generations = collections.Counter()
    checkpoint_content_generations = collections.Counter()
    checkpoint_identity_content_generations = collections.Counter()
    pending_checkpoint_directories = {}
    sealed_checkpoint_identities = set()
    sealed_checkpoint_paths = {}
    pending_mutation_identities = set()
    pending_directory_fsyncs = collections.Counter()
    mutation_generations = collections.Counter()
    directory_fsync_generations = {}
    staging_syncfs_devices = set()

    def is_within(path, root):
        return path == root or path.startswith(root + os.sep)

    def source_key(path):
        if path in source_path_to_key:
            return source_path_to_key[path]
        if any(path.endswith(suffix) for suffix in source_suffixes):
            raise FixtureError("linear_strace_source_invalid")
        return None

    def destination_key(path):
        return destination_path_to_key.get(path)

    def device_value(major, minor, raw_device):
        if raw_device is not None:
            return int(raw_device)
        return os.makedev(int(major), int(minor, 0))

    def directory_identity(lifetime, path):
        target = fd_targets.get(lifetime)
        if (
            target is None
            or target["path"] != path
            or not target["is_directory"]
            or target.get("identity") is None
        ):
            raise FixtureError("linear_strace_mutation_identity_invalid")
        return target["identity"]

    def record_mutation(lifetime, path):
        if not (
            is_within(path, staging_root)
            or any(is_within(path, root) for root in destination_roots)
        ):
            return
        identity = directory_identity(lifetime, path)
        pending_mutation_identities.add(identity)
        mutation_generations[identity] += 1

    def record_path_mutation(path):
        directory = os.path.dirname(path)
        if not (
            is_within(directory, staging_root)
            or any(is_within(directory, root) for root in destination_roots)
        ):
            return
        try:
            directory_stat = os.stat(directory, follow_symlinks=False)
        except OSError:
            raise FixtureError(
                "linear_strace_mutation_identity_invalid"
            ) from None
        if not stat.S_ISDIR(directory_stat.st_mode):
            raise FixtureError("linear_strace_mutation_identity_invalid")
        identity = (directory_stat.st_dev, directory_stat.st_ino)
        pending_mutation_identities.add(identity)
        mutation_generations[identity] += 1

    def decode_trace_path(raw_path):
        try:
            path = ast.literal_eval('"{}"'.format(raw_path))
        except (SyntaxError, ValueError):
            raise FixtureError("linear_strace_invalid") from None
        if not isinstance(path, str) or "\x00" in path:
            raise FixtureError("linear_strace_invalid")
        return path

    def resolve_at_path(lifetime_prefix, raw_dirfd, raw_path):
        path = decode_trace_path(raw_path)
        if os.path.isabs(path):
            return os.path.normpath(path)
        descriptor_match = re.fullmatch(
            r'(\d+)<([^>]*)>', raw_dirfd
        )
        if descriptor_match:
            descriptor, directory = descriptor_match.groups()
            lifetime = (lifetime_prefix, int(descriptor))
            target = fd_targets.get(lifetime)
            if (
                target is None
                or target["path"] != directory
                or not target["is_directory"]
            ):
                raise FixtureError("linear_strace_fd_invalid")
            return os.path.normpath(os.path.join(directory, path))
        cwd_match = re.fullmatch(
            r'AT_FDCWD(?:<([^>]*)>)?', raw_dirfd
        )
        if cwd_match is None or cwd_match.group(1) is None:
            raise FixtureError("linear_strace_mutation_identity_invalid")
        full_path = os.path.normpath(
            os.path.join(cwd_match.group(1), path)
        )
        return full_path

    def record_at_path_mutation(lifetime_prefix, raw_dirfd, raw_path):
        full_path = resolve_at_path(
            lifetime_prefix, raw_dirfd, raw_path
        )
        descriptor_match = re.fullmatch(r'(\d+)<([^>]*)>', raw_dirfd)
        if descriptor_match:
            descriptor, directory = descriptor_match.groups()
            lifetime = (lifetime_prefix, int(descriptor))
            if os.path.dirname(full_path) == directory:
                record_mutation(lifetime, directory)
                return
        record_path_mutation(full_path)

    def checkpoint_parent_identity(lifetime_prefix, raw_dirfd, full_path):
        descriptor_match = re.fullmatch(r'(\d+)<([^>]*)>', raw_dirfd)
        if descriptor_match is None:
            return None
        descriptor, directory = descriptor_match.groups()
        if os.path.dirname(full_path) != directory:
            return None
        lifetime = (lifetime_prefix, int(descriptor))
        target = fd_targets.get(lifetime)
        if (
            target is None
            or target["path"] != directory
            or not target["is_directory"]
        ):
            return None
        return target.get("identity")

    def is_checkpoint_temporary(path):
        return (
            is_within(path, legacy_backup_root)
            and checkpoint_temporary_pattern.fullmatch(
                os.path.basename(path)
            ) is not None
        )

    def is_checkpoint_final(path):
        return (
            is_within(path, legacy_backup_root)
            and os.path.basename(path) == checkpoint_filename
        )

    def mutate_checkpoint_path(path):
        if is_checkpoint_temporary(path):
            checkpoint_path_generations[path] += 1

    def record_checkpoint_namespace_mutation(path):
        if path in sealed_checkpoint_paths:
            raise FixtureError(
                "linear_strace_checkpoint_fsync_mismatch"
            )
        for proof in pending_checkpoint_directories.values():
            if path in (
                proof["source_path"],
                proof["destination_path"],
            ):
                proof["namespace_mutated_after_rename"] = True

    def record_checkpoint_content_mutation(lifetime, path):
        target = fd_targets.get(lifetime)
        if (
            path in sealed_checkpoint_paths
            or (
                target is not None
                and target.get("identity")
                in sealed_checkpoint_identities
            )
        ):
            raise FixtureError(
                "linear_strace_checkpoint_fsync_mismatch"
            )
        proof = None
        for pending_proof in pending_checkpoint_directories.values():
            if (
                path in (
                    pending_proof["source_path"],
                    pending_proof["destination_path"],
                )
                or (
                    target is not None
                    and target.get("identity")
                    == pending_proof["identity"]
                )
            ):
                proof = pending_proof
                break
        checkpoint_path = None
        if target is not None:
            checkpoint_path = target.get("checkpoint_temporary_path")
        if (
            proof is None
            and checkpoint_path is None
            and not is_checkpoint_temporary(path)
        ):
            return
        if target is None or target["is_directory"]:
            raise FixtureError("linear_strace_fd_invalid")
        if proof is None and path not in (target["path"], checkpoint_path):
            raise FixtureError("linear_strace_fd_invalid")
        if checkpoint_path is not None:
            checkpoint_content_generations[checkpoint_path] += 1
        identity = target.get("identity")
        if identity is not None:
            checkpoint_identity_content_generations[identity] += 1
        if proof is not None:
            proof["mutated_after_rename"] = True

    def record_checkpoint_rename(
        source_path,
        destination_path,
        source_parent_identity,
        destination_parent_identity,
        flags=None,
    ):
        if not is_checkpoint_final(destination_path):
            return False
        parent = os.path.dirname(destination_path)
        proof = checkpoint_fsynced_temporary_paths.get(source_path)
        identity = destination_parent_identity
        key = None
        if identity is not None:
            key = (parent, identity[0], identity[1])
        target = None
        if proof is not None:
            target = fd_targets.get(proof["lifetime"])
        if (
            flags not in (None, "0")
            or not is_checkpoint_temporary(source_path)
            or os.path.dirname(source_path) != parent
            or proof is None
            or source_parent_identity is None
            or destination_parent_identity is None
            or source_parent_identity != destination_parent_identity
            or key in pending_checkpoint_directories
            or target is None
            or target["path"] != source_path
            or target.get("identity") != proof["identity"]
            or target.get("checkpoint_generation")
            != proof["generation"]
            or checkpoint_path_generations[source_path]
            != proof["generation"]
            or checkpoint_content_generations[source_path]
            != proof["content_generation"]
            or checkpoint_identity_content_generations[proof["identity"]]
            != proof["identity_content_generation"]
        ):
            raise FixtureError(
                "linear_strace_checkpoint_fsync_mismatch"
            )
        checkpoint_fsynced_temporary_paths.pop(source_path)
        sealed_checkpoint_paths.pop(destination_path, None)
        mutate_checkpoint_path(source_path)
        proof["source_path"] = source_path
        proof["destination_path"] = destination_path
        proof["mutated_after_rename"] = False
        proof["namespace_mutated_after_rename"] = False
        pending_checkpoint_directories[key] = proof
        return True

    def seal_mutations(event):
        if not pending_mutation_identities and not pending_directory_fsyncs:
            return
        if event not in ("batch_intent", "batch_repair"):
            raise FixtureError("linear_strace_mutation_event_invalid")
        if set(pending_directory_fsyncs) != pending_mutation_identities:
            raise FixtureError("linear_strace_mutation_fsync_mismatch")
        if any(value != 1 for value in pending_directory_fsyncs.values()):
            raise FixtureError("linear_strace_directory_fsync_duplicate")
        if any(
            directory_fsync_generations.get(identity)
            != mutation_generations[identity]
            for identity in pending_mutation_identities
        ):
            raise FixtureError("linear_strace_mutation_fsync_mismatch")
        identities = [
            [device, inode]
            for device, inode in sorted(pending_mutation_identities)
        ]
        result["publication_mutation_windows"].append({
            "event": event,
            "mutated_directory_identities": identities,
            "fsynced_directory_identities": identities,
        })
        pending_mutation_identities.clear()
        pending_directory_fsyncs.clear()
        directory_fsync_generations.clear()

    def parse_journal_bytes(lifetime, value):
        journal_buffers[lifetime] += value
        while b"\n" in journal_buffers[lifetime]:
            raw, remainder = journal_buffers[lifetime].split(b"\n", 1)
            journal_buffers[lifetime] = remainder
            try:
                frame = json.loads(raw.decode("utf-8"))
                payload = frame["payload"]
                checksum = frame["checksum"]
                if (
                    set(frame) != {"checksum", "payload"}
                    or hashlib.sha256(
                        json.dumps(
                            payload,
                            ensure_ascii=True,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("ascii")
                    ).hexdigest() != checksum
                    or not isinstance(payload.get("event"), str)
                ):
                    raise ValueError
            except (KeyError, TypeError, ValueError, UnicodeDecodeError):
                raise FixtureError("linear_strace_journal_invalid") from None
            result["max_journal_event_bytes"] = max(
                result["max_journal_event_bytes"], len(raw) + 1
            )
            pending_journal_events[lifetime].append(payload)

    for trace_path in trace_files:
        lifetime_prefix = os.path.basename(trace_path)
        try:
            lines = Path(trace_path).read_text(
                encoding="utf-8", errors="strict"
            ).splitlines()
        except (OSError, UnicodeError):
            raise FixtureError("linear_strace_invalid") from None
        for line in lines:
            if "<unfinished ...>" in line or "strace:" in line:
                raise FixtureError("linear_strace_invalid")
            if tail_fsync_expected is not None:
                immediate_sync = re.search(
                    r"\b(?:fsync|fdatasync)\((\d+)<([^>]*)>\)"
                    r"\s*=\s*0$",
                    line,
                )
                if (
                    immediate_sync is None
                    or (
                        lifetime_prefix,
                        int(immediate_sync.group(1)),
                    ) != tail_fsync_expected["lifetime"]
                    or immediate_sync.group(2)
                    != tail_fsync_expected["path"]
                ):
                    raise FixtureError(
                        "linear_strace_journal_fsync_mismatch"
                    )
            open_match = re.search(
                r'\bopenat\(.*?,\s*"(?:\\.|[^"\\])*",\s*'
                r'([^)]*)\)\s*=\s*(\d+)<([^>]*)>',
                line,
            )
            if open_match:
                flags, descriptor, target = open_match.groups()
                lifetime = (lifetime_prefix, int(descriptor))
                if lifetime in fd_targets:
                    raise FixtureError("linear_strace_fd_invalid")
                checkpoint_generation = None
                if (
                    is_checkpoint_temporary(target)
                    and "O_CREAT" in flags
                    and "O_EXCL" in flags
                ):
                    mutate_checkpoint_path(target)
                    checkpoint_generation = checkpoint_path_generations[
                        target
                    ]
                fd_targets[lifetime] = {
                    "path": target,
                    "is_directory": "O_DIRECTORY" in flags,
                    "identity_verified": (
                        target not in source_path_to_key
                        and target not in destination_identity_by_path
                    ),
                    "identity": None,
                    "stat_size": None,
                    "checkpoint_generation": checkpoint_generation,
                    "checkpoint_temporary_path": (
                        target if is_checkpoint_temporary(target) else None
                    ),
                    "cursor": 0,
                    "high_water": 0,
                }
                if (
                    "O_TRUNC" in flags
                    and not (
                        "O_CREAT" in flags and "O_EXCL" in flags
                    )
                ):
                    record_checkpoint_content_mutation(lifetime, target)
            dup_match = re.search(
                r'\bdup\((\d+)<([^>]*)>\)\s*=\s*(\d+)<([^>]*)>',
                line,
            )
            if dup_match:
                source_fd, source_path, duplicate_fd, duplicate_path = (
                    dup_match.groups()
                )
                source_lifetime = (lifetime_prefix, int(source_fd))
                duplicate_lifetime = (lifetime_prefix, int(duplicate_fd))
                target = fd_targets.get(source_lifetime)
                if (
                    target is None
                    or target["path"] != source_path
                    or source_path != duplicate_path
                    or duplicate_lifetime in fd_targets
                ):
                    raise FixtureError("linear_strace_fd_invalid")
                fd_targets[duplicate_lifetime] = dict(target)
            stat_match = re.search(
                r'\b(?:fstat|newfstatat)\((\d+)<([^>]*)>.*?'
                r'st_dev=(?:makedev\((\d+),\s*(0x[0-9a-f]+|\d+)\)'
                r'|(\d+)).*?st_ino=(\d+).*?st_size=(\d+)',
                line,
            )
            if stat_match:
                descriptor, path, major, minor, raw_device, inode, size = (
                    stat_match.groups()
                )
                lifetime = (lifetime_prefix, int(descriptor))
                target = fd_targets.get(lifetime)
                if target is None or target["path"] != path:
                    raise FixtureError("linear_strace_fd_invalid")
                device = device_value(major, minor, raw_device)
                target["identity"] = (device, int(inode))
                target["stat_size"] = int(size)
                expected_identity = source_identity_by_path.get(path)
                if expected_identity is not None:
                    if (
                        device != expected_identity["device"]
                        or int(inode) != expected_identity["inode"]
                        or int(size) != expected_identity["size"]
                    ):
                        raise FixtureError("linear_strace_source_invalid")
                    target["identity_verified"] = True
                destination_identity = destination_identity_by_path.get(path)
                if destination_identity is not None:
                    if (
                        device != destination_identity["device"]
                        or int(inode) != destination_identity["inode"]
                        or int(size) != destination_size_by_path[path]
                    ):
                        raise FixtureError(
                            "linear_strace_destination_invalid"
                        )
                    target["identity_verified"] = True
            truncate_match = re.search(
                r'\bftruncate\((\d+)<([^>]*)>,\s*(\d+)\)\s*=\s*0$',
                line,
            )
            if truncate_match and truncate_match.group(2).endswith(
                "/linear-migration-v1.jsonl"
            ):
                descriptor, path, raw_offset = truncate_match.groups()
                lifetime = (lifetime_prefix, int(descriptor))
                target = fd_targets.get(lifetime)
                offset = int(raw_offset)
                if (
                    target is None
                    or target["is_directory"]
                    or target["path"] != path
                ):
                    raise FixtureError("linear_strace_fd_invalid")
                if (
                    mode != "tail-repair"
                    or tail_observation is None
                    or target.get("identity") != (
                        tail_observation["journal_identity"]["device"],
                        tail_observation["journal_identity"]["inode"],
                    )
                    or target.get("stat_size")
                    != tail_observation["torn_size"]
                    or offset != tail_observation["last_valid_offset"]
                    or result["journal_tail_truncate_count"] != 0
                    or tail_fsync_expected is not None
                    or lifetime in pending_journal_truncates
                    or pending_journal_events[lifetime]
                    or journal_buffers[lifetime]
                ):
                    raise FixtureError(
                        "linear_strace_journal_fsync_mismatch"
                    )
                pending_journal_truncates[lifetime] = offset
                tail_fsync_expected = {
                    "lifetime": lifetime,
                    "path": path,
                }
                result["journal_tail_truncate_count"] = 1
                result["journal_tail_truncate_offset"] = offset
            if truncate_match:
                lifetime = (
                    lifetime_prefix, int(truncate_match.group(1))
                )
                record_checkpoint_content_mutation(
                    lifetime, truncate_match.group(2)
                )
            write_match = re.search(
                r'write\((\d+)<([^>]*)>,\s*("(?:\\.|[^"\\])*")'
                r",\s*\d+\)\s*=\s*(\d+)",
                line,
            )
            if write_match and write_match.group(2).endswith(
                "/linear-migration-v1.jsonl"
            ):
                try:
                    value = ast.literal_eval(write_match.group(3)).encode(
                        "latin1"
                    )[:int(write_match.group(4))]
                except (SyntaxError, ValueError, UnicodeError):
                    raise FixtureError("linear_strace_invalid") from None
                lifetime = (lifetime_prefix, int(write_match.group(1)))
                target = fd_targets.get(lifetime)
                if (
                    target is None
                    or target["is_directory"]
                    or target["path"] != write_match.group(2)
                ):
                    raise FixtureError("linear_strace_fd_invalid")
                if lifetime in pending_journal_truncates:
                    raise FixtureError(
                        "linear_strace_journal_fsync_mismatch"
                    )
                parse_journal_bytes(lifetime, value)

            content_write_match = re.search(
                r'\b(?:write|writev|pwrite64)\('
                r'(\d+)<([^>]*)>,.*\)\s*=\s*(\d+)$',
                line,
            )
            if (
                content_write_match
                and int(content_write_match.group(3)) > 0
            ):
                lifetime = (
                    lifetime_prefix, int(content_write_match.group(1))
                )
                record_checkpoint_content_mutation(
                    lifetime, content_write_match.group(2)
                )

            read_match = re.search(
                r"\bread\((\d+)<([^>]*)>,.*\)\s*=\s*(\d+)$",
                line,
            )
            if read_match:
                read_size = int(read_match.group(3))
                if read_size > 0:
                    lifetime = (lifetime_prefix, int(read_match.group(1)))
                    path = read_match.group(2)
                    key = source_key(path)
                    kind = "source"
                    if key is None:
                        key = destination_key(path)
                        kind = "destination"
                    if key is not None:
                        target = fd_targets.get(lifetime)
                        if (
                            target is None
                            or target["is_directory"]
                            or target["path"] != path
                            or not target["identity_verified"]
                            or target["cursor"] != target["high_water"]
                        ):
                            raise FixtureError("linear_strace_fd_invalid")
                        prior = fd_read_keys.setdefault(lifetime, key)
                        prior_kind = fd_read_kinds.setdefault(lifetime, kind)
                        if prior != key or prior_kind != kind:
                            raise FixtureError("linear_strace_fd_invalid")
                        fd_reads[lifetime] += read_size
                        target["cursor"] += read_size
                        target["high_water"] += read_size

            pread_match = re.search(
                r"\bpread64\((\d+)<([^>]*)>,.*?,\s*\d+,\s*(\d+)\)"
                r"\s*=\s*(\d+)$",
                line,
            )
            if pread_match and int(pread_match.group(4)) > 0:
                descriptor, path, raw_offset, raw_size = pread_match.groups()
                lifetime = (lifetime_prefix, int(descriptor))
                key = source_key(path)
                kind = "source"
                if key is None:
                    key = destination_key(path)
                    kind = "destination"
                if key is not None:
                    target = fd_targets.get(lifetime)
                    offset = int(raw_offset)
                    read_size = int(raw_size)
                    if (
                        target is None
                        or target["is_directory"]
                        or target["path"] != path
                        or not target["identity_verified"]
                        or offset != target["high_water"]
                    ):
                        raise FixtureError("linear_strace_source_reread")
                    prior = fd_read_keys.setdefault(lifetime, key)
                    prior_kind = fd_read_kinds.setdefault(lifetime, kind)
                    if prior != key or prior_kind != kind:
                        raise FixtureError("linear_strace_fd_invalid")
                    fd_reads[lifetime] += read_size
                    target["high_water"] += read_size

            seek_match = re.search(
                r"\blseek\((\d+)<([^>]*)>,\s*(-?\d+),\s*"
                r"(SEEK_SET|SEEK_CUR|SEEK_END)\)"
                r"\s*=\s*(-?\d+)", line,
            )
            if seek_match:
                descriptor, path, _requested, _mode, returned = (
                    seek_match.groups()
                )
                lifetime = (lifetime_prefix, int(descriptor))
                target = fd_targets.get(lifetime)
                if target is None or target["path"] != path:
                    raise FixtureError("linear_strace_fd_invalid")
                returned = int(returned)
                if returned < 0:
                    continue
                if (
                    source_key(path) is not None
                    or destination_key(path) is not None
                ) and returned < target["high_water"]:
                    raise FixtureError("linear_strace_source_reread")
                target["cursor"] = returned
                target["high_water"] = max(
                    target["high_water"], returned
                )

            rename_match = re.search(
                r'\brenameat2?\('
                r'(AT_FDCWD(?:<[^>]*>)?|\d+<[^>]*>),\s*'
                r'"((?:\\.|[^"\\])*)",\s*'
                r'(AT_FDCWD(?:<[^>]*>)?|\d+<[^>]*>),\s*'
                r'"((?:\\.|[^"\\])*)"'
                r'(?:,\s*([^)]*))?\)\s*=\s*0$',
                line,
            )
            if rename_match:
                (
                    source_dirfd, source_path,
                    destination_dirfd, destination_path,
                    rename_flags,
                ) = rename_match.groups()
                record_at_path_mutation(
                    lifetime_prefix, source_dirfd, source_path
                )
                record_at_path_mutation(
                    lifetime_prefix, destination_dirfd, destination_path
                )
                full_source_path = resolve_at_path(
                    lifetime_prefix, source_dirfd, source_path
                )
                full_destination_path = resolve_at_path(
                    lifetime_prefix, destination_dirfd, destination_path
                )
                checkpoint_rename = record_checkpoint_rename(
                    full_source_path,
                    full_destination_path,
                    checkpoint_parent_identity(
                        lifetime_prefix, source_dirfd, full_source_path
                    ),
                    checkpoint_parent_identity(
                        lifetime_prefix,
                        destination_dirfd,
                        full_destination_path,
                    ),
                    None if rename_flags is None else rename_flags.strip(),
                )
                if not checkpoint_rename:
                    record_checkpoint_namespace_mutation(full_source_path)
                    record_checkpoint_namespace_mutation(
                        full_destination_path
                    )
                    mutate_checkpoint_path(full_source_path)
                    mutate_checkpoint_path(full_destination_path)
            link_match = re.search(
                r'\blinkat\('
                r'(AT_FDCWD(?:<[^>]*>)?|\d+<[^>]*>),\s*'
                r'"((?:\\.|[^"\\])*)",\s*'
                r'(AT_FDCWD(?:<[^>]*>)?|\d+<[^>]*>),\s*'
                r'"((?:\\.|[^"\\])*)"'
                r'(?:,\s*[^)]*)?\)\s*=\s*0$', line,
            )
            if link_match:
                (
                    source_dirfd, source_path,
                    destination_dirfd, destination_path,
                ) = link_match.groups()
                record_at_path_mutation(
                    lifetime_prefix, destination_dirfd, destination_path
                )
                full_source_path = resolve_at_path(
                    lifetime_prefix, source_dirfd, source_path
                )
                full_destination_path = resolve_at_path(
                    lifetime_prefix, destination_dirfd, destination_path
                )
                record_checkpoint_namespace_mutation(full_source_path)
                record_checkpoint_namespace_mutation(
                    full_destination_path
                )
                mutate_checkpoint_path(full_destination_path)
            unlink_match = re.search(
                r'\bunlinkat\('
                r'(AT_FDCWD(?:<[^>]*>)?|\d+<[^>]*>),\s*'
                r'"((?:\\.|[^"\\])*)"'
                r'(?:,\s*[^)]*)?\)\s*=\s*0$', line,
            )
            if unlink_match:
                record_at_path_mutation(
                    lifetime_prefix, unlink_match.group(1),
                    unlink_match.group(2),
                )
                unlinked_path = resolve_at_path(
                    lifetime_prefix, unlink_match.group(1),
                    unlink_match.group(2),
                )
                record_checkpoint_namespace_mutation(unlinked_path)
                mutate_checkpoint_path(unlinked_path)
            plain_rename = re.search(
                r'\brename\("((?:\\.|[^"\\])*)",\s*'
                r'"((?:\\.|[^"\\])*)"\)\s*=\s*0$', line,
            )
            if plain_rename:
                source_path, destination_path = plain_rename.groups()
                source_path = os.path.normpath(
                    decode_trace_path(source_path)
                )
                destination_path = os.path.normpath(
                    decode_trace_path(destination_path)
                )
                record_path_mutation(source_path)
                record_path_mutation(destination_path)
                checkpoint_rename = record_checkpoint_rename(
                    source_path, destination_path, None, None
                )
                if not checkpoint_rename:
                    record_checkpoint_namespace_mutation(source_path)
                    record_checkpoint_namespace_mutation(destination_path)
                    mutate_checkpoint_path(source_path)
                    mutate_checkpoint_path(destination_path)
            plain_link = re.search(
                r'\blink\("((?:\\.|[^"\\])*)",\s*'
                r'"((?:\\.|[^"\\])*)"\)\s*=\s*0$', line,
            )
            if plain_link:
                source_path = os.path.normpath(
                    decode_trace_path(plain_link.group(1))
                )
                destination_path = os.path.normpath(
                    decode_trace_path(plain_link.group(2))
                )
                record_path_mutation(destination_path)
                record_checkpoint_namespace_mutation(source_path)
                record_checkpoint_namespace_mutation(destination_path)
                mutate_checkpoint_path(destination_path)
            plain_unlink = re.search(
                r'\bunlink\("((?:\\.|[^"\\])*)"\)\s*=\s*0$',
                line,
            )
            if plain_unlink:
                unlinked_path = os.path.normpath(
                    decode_trace_path(plain_unlink.group(1))
                )
                record_path_mutation(unlinked_path)
                record_checkpoint_namespace_mutation(unlinked_path)
                mutate_checkpoint_path(unlinked_path)

            sync_match = re.search(
                r"\b(fsync|fdatasync|syncfs)\((\d+)<([^>]*)>\)\s*=\s*(-?\d+)",
                line,
            )
            if sync_match:
                call, descriptor, path, returned = sync_match.groups()
                lifetime = (lifetime_prefix, int(descriptor))
                target = fd_targets.get(lifetime)
                if target is None or target["path"] != path:
                    raise FixtureError("linear_strace_fd_invalid")
                successful = int(returned) == 0
                if call == "syncfs":
                    result["actual_syncfs_calls"] += 1
                    if successful:
                        if target["is_directory"] or not is_within(
                            path, staging_root
                        ):
                            raise FixtureError(
                                "linear_strace_syncfs_invalid"
                            )
                        try:
                            device = os.stat(
                                staging_root, follow_symlinks=False
                            ).st_dev
                        except OSError:
                            raise FixtureError(
                                "linear_strace_syncfs_invalid"
                            ) from None
                        if device in staging_syncfs_devices:
                            raise FixtureError(
                                "linear_strace_syncfs_duplicate"
                            )
                        staging_syncfs_devices.add(device)
                        result["successful_syncfs_calls"] += 1
                        result["batch_file_syncfs"] += 1
                    continue
                result["actual_fsync_calls"] += 1
                if not successful:
                    continue
                result["successful_fsync_calls"] += 1
                if target["is_directory"]:
                    matching_checkpoint_keys = [
                        key for key in pending_checkpoint_directories
                        if key[0] == path
                    ]
                    if matching_checkpoint_keys:
                        identity = target.get("identity")
                        if identity is None:
                            raise FixtureError(
                                "linear_strace_checkpoint_fsync_mismatch"
                            )
                        checkpoint_key = (
                            path, identity[0], identity[1]
                        )
                        if checkpoint_key not in (
                            pending_checkpoint_directories
                        ):
                            raise FixtureError(
                                "linear_strace_checkpoint_fsync_mismatch"
                            )
                        proof = pending_checkpoint_directories.pop(
                            checkpoint_key
                        )
                        if (
                            proof["mutated_after_rename"]
                            or proof[
                                "namespace_mutated_after_rename"
                            ]
                            or checkpoint_identity_content_generations[
                                proof["identity"]
                            ] != proof["identity_content_generation"]
                        ):
                            raise FixtureError(
                                "linear_strace_checkpoint_fsync_mismatch"
                            )
                        event = proof["event"]
                        sealed_checkpoint_identities.add(
                            proof["identity"]
                        )
                        sealed_checkpoint_paths[
                            proof["destination_path"]
                        ] = proof["identity"]
                        result["checkpoint_directory_fsyncs"] += 1
                        result["checkpoint_events"].append(event)
                    if (
                        is_within(path, staging_root)
                        or any(
                            is_within(path, root)
                            for root in destination_roots
                        )
                    ):
                        identity = target.get("identity")
                        if identity is None:
                            raise FixtureError(
                                "linear_strace_mutation_identity_invalid"
                            )
                        pending_directory_fsyncs[identity] += 1
                        directory_fsync_generations[identity] = (
                            mutation_generations[identity]
                        )
                        if pending_directory_fsyncs[identity] > 1:
                            raise FixtureError(
                                "linear_strace_directory_fsync_duplicate"
                            )
                if (
                    not target["is_directory"]
                    and any(
                        is_within(path, root)
                        for root in (staging_root,) + destination_roots
                    )
                ):
                    raise FixtureError("linear_strace_media_file_fsync")
                if path.endswith("/linear-migration-v1.jsonl"):
                    if target["is_directory"]:
                        raise FixtureError("linear_strace_fd_invalid")
                    payloads = pending_journal_events[lifetime]
                    if not payloads:
                        if lifetime not in pending_journal_truncates:
                            raise FixtureError(
                                "linear_strace_journal_fsync_mismatch"
                            )
                        pending_journal_truncates.pop(lifetime)
                        tail_fsync_expected = None
                        result["journal_tail_repair_fsyncs"] += 1
                    else:
                        if (
                            len(payloads) != 1
                            or lifetime in pending_journal_truncates
                        ):
                            raise FixtureError(
                                "linear_strace_journal_fsync_mismatch"
                            )
                        result["journal_event_fsyncs"] += 1
                        event = payloads[0]["event"]
                        pending_journal_events[lifetime] = []
                        if event == "batch_intent":
                            result["journal_intent_fsyncs"] += 1
                            seal_mutations(event)
                            staging_syncfs_devices.clear()
                        elif event == "batch_commit":
                            result["journal_commit_fsyncs"] += 1
                        elif event == "batch_repair":
                            result["journal_repair_fsyncs"] += 1
                            seal_mutations(event)
                            if (
                                not pending_checkpoint_events
                                or pending_checkpoint_events[-1]
                                != "batch_repair"
                            ):
                                pending_checkpoint_events.append(
                                    "batch_repair"
                                )
                        elif event == "phase_complete":
                            phase = payloads[0].get("phase")
                            if phase not in ("paths", "backfill"):
                                raise FixtureError(
                                    "linear_strace_checkpoint_fsync_mismatch"
                                )
                            pending_checkpoint_events.append(
                                "phase_complete:{}".format(phase)
                            )
                if is_checkpoint_temporary(path):
                    checkpoint_generation = target.get(
                        "checkpoint_generation"
                    )
                    if (
                        target["is_directory"]
                        or path in checkpoint_fsynced_temporary_paths
                        or not pending_checkpoint_events
                        or target.get("identity") is None
                        or target["identity"][1] <= 0
                        or checkpoint_generation is None
                        or checkpoint_path_generations[path]
                        != checkpoint_generation
                    ):
                        raise FixtureError(
                            "linear_strace_checkpoint_fsync_mismatch"
                        )
                    checkpoint_fsynced_temporary_paths[path] = {
                        "event": pending_checkpoint_events.popleft(),
                        "identity": target["identity"],
                        "generation": checkpoint_generation,
                        "content_generation": (
                            checkpoint_content_generations[path]
                        ),
                        "identity_content_generation": (
                            checkpoint_identity_content_generations[
                                target["identity"]
                            ]
                        ),
                        "lifetime": lifetime,
                    }
                    result["checkpoint_file_fsyncs"] += 1
                if target["is_directory"] and is_within(path, staging_root):
                    result["publication_directory_fsyncs"] += 1
                    result["publication_staging_directory_fsyncs"] += 1
                elif target["is_directory"] and any(
                    is_within(path, root) for root in destination_roots
                ):
                    result["publication_directory_fsyncs"] += 1
                    result["publication_destination_directory_fsyncs"] += 1

            close_match = re.search(r"\bclose\((\d+)<", line)
            if close_match:
                lifetime = (lifetime_prefix, int(close_match.group(1)))
                fd_targets.pop(lifetime, None)
                key = fd_read_keys.pop(lifetime, None)
                kind = fd_read_kinds.pop(lifetime, None)
                if key is not None:
                    count = fd_reads.pop(lifetime, 0)
                    result["{}_read_bytes".format(kind)][key] += count
                    result["{}_read_passes".format(kind)][key] += 1
                if journal_buffers.get(lifetime):
                    raise FixtureError("linear_strace_journal_invalid")
                if pending_journal_events.get(lifetime):
                    raise FixtureError("linear_strace_journal_invalid")
                if lifetime in pending_journal_truncates:
                    raise FixtureError(
                        "linear_strace_journal_fsync_mismatch"
                    )
                journal_buffers.pop(lifetime, None)
                pending_journal_events.pop(lifetime, None)
                pending_journal_truncates.pop(lifetime, None)
    if fd_read_keys or any(journal_buffers.values()):
        raise FixtureError("linear_strace_fd_invalid")
    if pending_journal_truncates or tail_fsync_expected is not None:
        raise FixtureError("linear_strace_journal_fsync_mismatch")
    if pending_mutation_identities or pending_directory_fsyncs:
        raise FixtureError("linear_strace_mutation_fsync_mismatch")
    if (
        checkpoint_fsynced_temporary_paths
        or pending_checkpoint_directories
        or pending_checkpoint_events
    ):
        raise FixtureError("linear_strace_checkpoint_fsync_mismatch")
    for kind in ("source", "destination"):
        result["{}_read_bytes".format(kind)] = dict(sorted(
            result["{}_read_bytes".format(kind)].items()
        ))
        result["{}_read_passes".format(kind)] = dict(sorted(
            result["{}_read_passes".format(kind)].items()
        ))
    if mode == "repair":
        _journal_path, events = _linear_journal(data_root)
        intents = sum(
            event.get("event") == "batch_intent" for event in events
        )
        commits = sum(
            event.get("event") == "batch_commit" for event in events
        )
        repairs = sum(
            event.get("event") == "batch_repair" for event in events
        )
        destination_payload, _destination_stat = _read_regular_file(
            repair_path
        )
        expected_destination = {
            repair_observation["file_key"]:
            repair_observation["expected_size"]
        }
        if (
            result["journal_intent_fsyncs"] != 0
            or result["journal_commit_fsyncs"] != 0
            or result["journal_repair_fsyncs"] != 1
            or intents != repair_observation["intent_count"]
            or commits != repair_observation["commit_count"]
            or repairs != repair_observation["repair_count"] + 1
            or _journal_ordinals(events)
            != repair_observation["commit_ordinals"]
            or result["destination_read_bytes"]
            != expected_destination
            or result["destination_read_passes"] != {
                repair_observation["file_key"]: 1
            }
            or len(result["publication_mutation_windows"]) != 1
            or result["publication_mutation_windows"][0]["event"]
            != "batch_repair"
            or hashlib.sha256(destination_payload).hexdigest()
            != repair_observation["expected_sha256"]
        ):
            raise FixtureError("linear_strace_repair_contract_invalid")
    elif mode == "tail-repair":
        if (
            result["journal_tail_truncate_count"] != 1
            or result["journal_tail_truncate_offset"]
            != tail_observation["last_valid_offset"]
            or result["journal_tail_repair_fsyncs"] != 1
        ):
            raise FixtureError("linear_strace_journal_fsync_mismatch")
    _atomic_write(
        output_path,
        json.dumps(
            result,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii") + b"\n",
        mode=0o600,
    )


def _configure_settings(data_root, allow_hosts):
    data_root = os.path.abspath(os.fspath(data_root))
    os.makedirs(data_root, mode=0o770, exist_ok=True)
    normalized = []
    for host in allow_hosts:
        if not isinstance(host, str) or not _HOST_PATTERN.match(host):
            raise FixtureError("allow_host_invalid")
        value = host.rstrip(".").lower()
        if value not in normalized:
            normalized.append(value)
    template_path = (
        _repo_root() / "pinry/settings/local_settings.example.py"
    )
    try:
        contents = template_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise FixtureError("settings_template_invalid") from None
    marker = 'SECRET_KEY = "secret_key_place_holder"'
    if contents.count(marker) != 1:
        raise FixtureError("settings_template_invalid")
    contents = contents.replace(
        marker,
        "SECRET_KEY = "
        '"fixture-only-0123456789abcdef0123456789abcdef0123456789abcdef"',
    )
    contents += "\nPINRY_FETCH_PRIVATE_ALLOWLIST = {!r}\n".format(
        normalized
    )
    _atomic_write(
        os.path.join(data_root, "local_settings.py"),
        contents.encode("utf-8"),
        mode=0o600,
    )


def _read_regular_file(path, maximum=_MAX_JSON_BYTES):
    descriptor = None
    try:
        descriptor = os.open(
            os.path.abspath(os.fspath(path)),
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        file_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_nlink != 1
            or file_stat.st_size > maximum
        ):
            raise FixtureError("unsafe_fixture_file")
        chunks = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65536, maximum + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum:
                raise FixtureError("unsafe_fixture_file")
        return b"".join(chunks), file_stat
    except FixtureError:
        raise
    except (OSError, TypeError, ValueError):
        raise FixtureError("unsafe_fixture_file") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _load_json_file(path, maximum=_MAX_JSON_BYTES):
    payload, file_stat = _read_regular_file(path, maximum=maximum)
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise FixtureError("fixture_json_invalid") from None
    return value, payload, file_stat


def _load_json_lines(path, allow_torn_tail=False):
    payload, file_stat = _read_regular_file(path)
    lines = payload.splitlines(keepends=True)
    events = []
    consumed = 0
    for index, raw_line in enumerate(lines):
        complete = raw_line.endswith(b"\n")
        if not complete and allow_torn_tail and index == len(lines) - 1:
            break
        if not complete or not raw_line.strip():
            raise FixtureError("fixture_manifest_invalid")
        try:
            event = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            if allow_torn_tail and index == len(lines) - 1:
                break
            raise FixtureError("fixture_manifest_invalid") from None
        if not isinstance(event, dict):
            raise FixtureError("fixture_manifest_invalid")
        events.append(event)
        consumed += len(raw_line)
    return events, payload[:consumed], file_stat


def _file_sha256(path, limit=None):
    descriptor = None
    try:
        descriptor = os.open(
            os.path.abspath(os.fspath(path)),
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
            raise FixtureError("unsafe_fixture_file")
        remaining = file_stat.st_size if limit is None else limit
        if type(remaining) is not int or remaining < 0:
            raise FixtureError("unsafe_fixture_file")
        digest = hashlib.sha256()
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                raise FixtureError("unsafe_fixture_file")
            digest.update(chunk)
            remaining -= len(chunk)
        return digest.hexdigest(), file_stat
    except FixtureError:
        raise
    except (OSError, TypeError, ValueError):
        raise FixtureError("unsafe_fixture_file") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _validate_receipt_value(value, key_name=None):
    forbidden = ("password", "secret", "token", "username", "email")
    if key_name is not None and any(word in key_name.lower() for word in forbidden):
        raise FixtureError("receipt_contains_private_data")
    if isinstance(value, dict):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise FixtureError("receipt_invalid")
            _validate_receipt_value(nested, key)
    elif isinstance(value, list):
        for nested in value:
            _validate_receipt_value(nested)
    elif isinstance(value, str) and (
        "://" in value or value.startswith("/")
    ):
        raise FixtureError("receipt_contains_private_data")


def _load_receipt(path, data_root):
    receipt_path = _contained_path(path, data_root)
    receipt, _payload, file_stat = _load_json_file(receipt_path)
    if stat.S_IMODE(file_stat.st_mode) != 0o600:
        raise FixtureError("receipt_permissions_invalid")
    try:
        account = pwd.getpwnam("www-data")
    except KeyError:
        account = None
    if account is not None and (
        file_stat.st_uid != account.pw_uid
        or file_stat.st_gid != account.pw_gid
    ):
        raise FixtureError("receipt_permissions_invalid")
    base_keys = {
        "schema_version", "kind", "count", "board_id",
        "archive_root_identity", "expected_direct_roots",
        "orphan_sentinel", "items",
    }
    linear_keys = {
        "images_total", "files_total", "thumbnails_per_image", "formats",
        "total_bytes", "total_pixels", "expected_files",
    }
    if (
        not isinstance(receipt, dict)
        or set(receipt) not in (base_keys, base_keys | linear_keys)
        or receipt.get("schema_version") != 1
        or receipt.get("kind") not in FIXTURE_KINDS
        or type(receipt.get("count")) is not int
        or receipt.get("count") != len(receipt.get("items", ()))
    ):
        raise FixtureError("receipt_invalid")
    _validate_receipt_value(receipt)
    return receipt


def _open_sqlite_read_only(database_path):
    database_path = os.path.abspath(os.fspath(database_path))
    try:
        connection = sqlite3.connect(
            "file:{}?mode=ro".format(quote(database_path, safe="/")),
            uri=True,
            timeout=10,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        return connection
    except sqlite3.Error:
        raise FixtureError("database_read_failed") from None


def _sqlite_quick_check(database_path):
    connection = _open_sqlite_read_only(database_path)
    try:
        row = connection.execute("PRAGMA quick_check").fetchone()
        if row is None or row[0] != "ok":
            raise FixtureError("database_integrity_failed")
    except sqlite3.Error:
        raise FixtureError("database_integrity_failed") from None
    finally:
        connection.close()


def _migration_runs(data_root):
    backup_root = os.path.join(data_root, "legacy-backup")
    try:
        root_stat = os.stat(backup_root, follow_symlinks=False)
    except FileNotFoundError:
        return []
    except OSError:
        raise FixtureError("backup_inventory_invalid") from None
    if (
        not stat.S_ISDIR(root_stat.st_mode)
        or stat.S_IMODE(root_stat.st_mode) != 0o700
    ):
        raise FixtureError("backup_inventory_invalid")
    runs = []
    try:
        names = sorted(os.listdir(backup_root))
    except OSError:
        raise FixtureError("backup_inventory_invalid") from None
    for name in names:
        if name.startswith("."):
            raise FixtureError("backup_inventory_invalid")
        run_path = _contained_path(os.path.join(backup_root, name), backup_root)
        try:
            run_stat = os.stat(run_path, follow_symlinks=False)
        except OSError:
            raise FixtureError("backup_inventory_invalid") from None
        try:
            account = pwd.getpwnam("www-data")
        except KeyError:
            account = None
        if (
            not stat.S_ISDIR(run_stat.st_mode)
            or (
                account is not None
                and (
                    run_stat.st_uid != account.pw_uid
                    or run_stat.st_gid != account.pw_gid
                )
            )
        ):
            raise FixtureError("backup_inventory_invalid")
        state_path = os.path.join(run_path, _STATE_FILENAME)
        state, _raw_state, state_stat = _load_json_file(state_path)
        if (
            not isinstance(state, dict)
            or state.get("run_id") != name
            or not isinstance(state.get("phase"), str)
            or stat.S_IMODE(run_stat.st_mode) != 0o700
            or stat.S_IMODE(state_stat.st_mode) != 0o600
            or state_stat.st_uid != run_stat.st_uid
            or state_stat.st_gid != run_stat.st_gid
        ):
            raise FixtureError("backup_inventory_invalid")
        runs.append((name, run_path, state, run_stat))
    return runs


def _single_completed_run(data_root):
    runs = _migration_runs(data_root)
    if len(runs) != 1 or runs[0][2].get("phase") != "complete":
        raise FixtureError("migration_not_complete")
    return runs[0]


def _canonical_paths(asset_uuid, original_filename, image_format="PNG"):
    from django_images.paths import (
        canonical_derivative_path,
        canonical_original_path,
    )

    try:
        canonical_uuid = str(uuid.UUID(str(asset_uuid)))
        extension = ".jpg" if image_format == "JPEG" else ".png"
        original = canonical_original_path(
            canonical_uuid,
            original_filename,
            extension,
        )
        derivatives = {
            kind: canonical_derivative_path(canonical_uuid, kind, extension)
            for kind in DERIVATIVE_KINDS
        }
    except (TypeError, ValueError):
        raise FixtureError("canonical_path_invalid") from None
    return canonical_uuid, original, derivatives


def _assert_media_file(media_root, relative_path, expected_sha256):
    absolute = _contained_path(os.path.join(media_root, relative_path), media_root)
    digest, _file_stat = _file_sha256(absolute)
    if digest != expected_sha256:
        raise FixtureError("media_content_mismatch")


def _assert_backup_file(
    run_path, relative_path, expected_sha256, expected_identity=None
):
    absolute = _contained_path(os.path.join(run_path, relative_path), run_path)
    digest, file_stat = _file_sha256(absolute)
    if digest != expected_sha256:
        raise FixtureError("backup_content_mismatch")
    if expected_identity is not None and (
        file_stat.st_dev != expected_identity.get("device")
        or file_stat.st_ino != expected_identity.get("inode")
        or file_stat.st_size != expected_identity.get("size")
    ):
        raise FixtureError("backup_identity_mismatch")


def _assert_completed_manifests(run_path, receipt):
    run_stat = os.stat(run_path, follow_symlinks=False)
    media_events, media_raw, media_stat = _load_json_lines(
        os.path.join(run_path, _MEDIA_MANIFEST)
    )
    backfill_events, backfill_raw, backfill_stat = _load_json_lines(
        os.path.join(run_path, _BACKFILL_MANIFEST)
    )
    image_ids = {item["image_id"] for item in receipt["items"]}
    media_terminal = {
        event.get("image_id")
        for event in media_events
        if event.get("event") in (
            "committed", "recovered_commit", "already_current"
        )
    }
    registry_terminal = {
        event.get("image_id")
        for event in backfill_events
        if event.get("event") in (
            "registered", "already_registered", "recovered_registered"
        )
    }
    if not image_ids.issubset(media_terminal) or not image_ids.issubset(
        registry_terminal
    ):
        raise FixtureError("manifest_incomplete")
    summary, _summary_raw, summary_stat = _load_json_file(
        os.path.join(run_path, _SUMMARY_FILENAME)
    )
    for artifact_stat in (media_stat, backfill_stat, summary_stat):
        if (
            stat.S_IMODE(artifact_stat.st_mode) != 0o600
            or artifact_stat.st_uid != run_stat.st_uid
            or artifact_stat.st_gid != run_stat.st_gid
        ):
            raise FixtureError("migration_artifact_permissions_invalid")
    if (
        not isinstance(summary, dict)
        or summary.get("phase") != "complete"
        or summary.get("media_manifest_sha256") != _sha256(media_raw)
        or summary.get("backfill_manifest_sha256") != _sha256(backfill_raw)
    ):
        raise FixtureError("migration_summary_invalid")


def _assert_board_graph(connection, receipt):
    board_row = connection.execute(
        "SELECT submitter_id, name, private FROM core_board WHERE id = ?",
        (receipt["board_id"],),
    ).fetchone()
    pin_ids = {item["pin_id"] for item in receipt["items"]}
    board_pin_ids = {
        row[0]
        for row in connection.execute(
            "SELECT pin_id FROM core_board_pins WHERE board_id = ?",
            (receipt["board_id"],),
        )
    }
    if (
        board_row is None
        or board_row["name"] != "historical-fixture-board"
        or bool(board_row["private"])
        or board_pin_ids != pin_ids
    ):
        raise FixtureError("board_graph_invalid")
    return board_row["submitter_id"]


def _assert_snapshot_contract(snapshot_path, receipt):
    connection = _open_sqlite_read_only(snapshot_path)
    try:
        applied = {
            (row[0], row[1])
            for row in connection.execute(
                "SELECT app, name FROM django_migrations"
            )
        }
        if receipt["kind"] == "legacy-md5":
            required = {
                ("django_images", "0002_auto_20180826_0814"),
                ("core", "0010_auto_20210311_1521"),
            }
            forbidden = {
                ("django_images", "0003_image_asset_metadata_nullable"),
                ("core", "0011_pin_trashed_at"),
            }
        else:
            required = {
                ("django_images", "0005_enforce_image_asset_metadata"),
                ("core", "0013_remove_pin_trashed_at"),
            }
            forbidden = {
                ("django_images", "0006_pending_media_deletion"),
                ("core", "0014_media_asset"),
            }
        if not required.issubset(applied) or applied.intersection(forbidden):
            raise FixtureError("snapshot_migration_graph_invalid")
        image_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(django_images_image)"
            )
        }
        expected_metadata = receipt["kind"] != "legacy-md5"
        if ({"asset_uuid", "original_filename"}.issubset(image_columns)) != (
            expected_metadata
        ):
            raise FixtureError("snapshot_schema_invalid")
        board_submitter_id = _assert_board_graph(connection, receipt)
        for item in receipt["items"]:
            image_row = connection.execute(
                "SELECT image FROM django_images_image WHERE id = ?",
                (item["image_id"],),
            ).fetchone()
            pin_row = connection.execute(
                "SELECT submitter_id, image_id, url "
                "FROM core_pin WHERE id = ?",
                (item["pin_id"],),
            ).fetchone()
            thumbnail_ids = {
                row[0]
                for row in connection.execute(
                    "SELECT id FROM django_images_thumbnail "
                    "WHERE original_id = ?",
                    (item["image_id"],),
                )
            }
            if (
                image_row is None
                or image_row["image"] != item["legacy_original_path"]
                or pin_row is None
                or pin_row["submitter_id"] != board_submitter_id
                or pin_row["image_id"] != item["image_id"]
                or pin_row["url"] != item["pin_url"]
                or thumbnail_ids
                != {entry["id"] for entry in item["derivatives"]}
            ):
                raise FixtureError("snapshot_rows_invalid")
    except FixtureError:
        raise
    except (KeyError, TypeError, sqlite3.Error):
        raise FixtureError("snapshot_verification_failed") from None
    finally:
        connection.close()
    return board_submitter_id


def _verify_migration(data_root, receipt_path):  # noqa: C901
    data_root = os.path.abspath(os.fspath(data_root))
    receipt = _load_receipt(receipt_path, data_root)
    _run_id, run_path, _state, _run_stat = _single_completed_run(data_root)
    database_path = os.path.join(data_root, "production.db")
    snapshot_path = os.path.join(run_path, _SNAPSHOT_FILENAME)
    _sqlite_quick_check(database_path)
    _snapshot_sha256, snapshot_stat = _file_sha256(snapshot_path)
    _sqlite_quick_check(snapshot_path)
    snapshot_board_submitter_id = _assert_snapshot_contract(
        snapshot_path, receipt
    )
    if (
        stat.S_IMODE(snapshot_stat.st_mode) != 0o600
        or snapshot_stat.st_uid != _run_stat.st_uid
        or snapshot_stat.st_gid != _run_stat.st_gid
    ):
        raise FixtureError("migration_artifact_permissions_invalid")
    _assert_completed_manifests(run_path, receipt)
    media_root = os.path.join(data_root, "static", "media")
    connection = _open_sqlite_read_only(database_path)
    try:
        applied = {
            (row[0], row[1])
            for row in connection.execute(
                "SELECT app, name FROM django_migrations"
            )
        }
        if (
            ("django_images", "0006_pending_media_deletion") not in applied
            or ("core", "0016_board_display_order") not in applied
        ):
            raise FixtureError("migration_graph_incomplete")
        board_submitter_id = _assert_board_graph(connection, receipt)
        if board_submitter_id != snapshot_board_submitter_id:
            raise FixtureError("board_submitter_changed")
        for item in receipt["items"]:
            image_row = connection.execute(
                "SELECT id, image, asset_uuid, original_filename, height, width "
                "FROM django_images_image WHERE id = ?",
                (item["image_id"],),
            ).fetchone()
            if image_row is None:
                raise FixtureError("migrated_row_missing")
            filename = image_row["original_filename"]
            canonical_uuid, original_path, derivative_paths = _canonical_paths(
                image_row["asset_uuid"], filename, item["original_format"]
            )
            if (
                str(uuid.UUID(str(image_row["asset_uuid"]))) != canonical_uuid
                or image_row["image"] != original_path
                or image_row["height"] != item["original_height"]
                or image_row["width"] != item["original_width"]
            ):
                raise FixtureError("migrated_row_mismatch")
            if receipt["kind"] != "legacy-md5" and (
                item["asset_uuid"] != canonical_uuid
                or item["original_filename"] != filename
            ):
                raise FixtureError("migrated_metadata_mismatch")
            _assert_media_file(
                media_root, original_path, item["original_sha256"]
            )
            for derivative in item["derivatives"]:
                row = connection.execute(
                    "SELECT original_id, image, size, height, width "
                    "FROM django_images_thumbnail WHERE id = ?",
                    (derivative["id"],),
                ).fetchone()
                if (
                    row is None
                    or row["original_id"] != item["image_id"]
                    or row["size"] != derivative["kind"]
                    or row["image"] != derivative_paths[derivative["kind"]]
                    or row["height"] != derivative["height"]
                    or row["width"] != derivative["width"]
                ):
                    raise FixtureError("migrated_derivative_mismatch")
                _assert_media_file(
                    media_root, row["image"], derivative["sha256"]
                )
            pin_row = connection.execute(
                "SELECT submitter_id, image_id, url FROM core_pin WHERE id = ?",
                (item["pin_id"],),
            ).fetchone()
            if (
                pin_row is None
                or pin_row["submitter_id"] != board_submitter_id
                or pin_row["image_id"] != item["image_id"]
                or pin_row["url"] != item["pin_url"]
            ):
                raise FixtureError("migrated_pin_mismatch")
            registry = connection.execute(
                "SELECT submitter_id, content_sha256 FROM core_mediaasset "
                "WHERE image_id = ?",
                (item["image_id"],),
            ).fetchone()
            if (
                registry is None
                or registry["submitter_id"] != pin_row["submitter_id"]
                or registry["content_sha256"] != item["original_sha256"]
            ):
                raise FixtureError("migrated_registry_mismatch")
            current_url = "/media/{}".format(original_path)
            if receipt["kind"] == "pending-schema":
                if item["prior_image_media_url"] != current_url:
                    raise FixtureError("pending_schema_path_changed")
            elif item["prior_image_media_url"] == current_url:
                raise FixtureError("legacy_path_not_changed")
            old_original = os.path.join(
                media_root, item["legacy_original_path"]
            )
            if receipt["kind"] != "pending-schema" and os.path.lexists(
                old_original
            ):
                raise FixtureError("legacy_source_not_archived")
            if receipt["kind"] == "legacy-md5":
                for root_name, root_identity in (
                    receipt["archive_root_identity"].items()
                ):
                    archived_root = os.path.join(
                        run_path, "media", root_name
                    )
                    archived_root_stat = os.stat(
                        archived_root, follow_symlinks=False
                    )
                    if (
                        os.path.lexists(os.path.join(media_root, root_name))
                        or not stat.S_ISDIR(archived_root_stat.st_mode)
                        or archived_root_stat.st_dev
                        != root_identity.get("device")
                        or archived_root_stat.st_ino
                        != root_identity.get("inode")
                    ):
                        raise FixtureError("legacy_root_archive_mismatch")
                _assert_backup_file(
                    run_path,
                    "media/{}".format(item["legacy_original_path"]),
                    item["original_sha256"],
                    item["source_identity"],
                )
                for derivative in item["derivatives"]:
                    _assert_backup_file(
                        run_path,
                        "media/{}".format(derivative["legacy_path"]),
                        derivative["sha256"],
                        derivative["source_identity"],
                    )
            elif receipt["kind"] == "transitional-fixed-slot":
                _assert_backup_file(
                    run_path,
                    "media/fixed-slot-originals/{}".format(
                        item["legacy_original_path"]
                    ),
                    item["original_sha256"],
                    item["source_identity"],
                )
        if receipt["kind"] == "pending-schema" and os.path.lexists(
            os.path.join(run_path, "media")
        ):
            raise FixtureError("unexpected_archive_payload")
        if receipt["kind"] == "legacy-md5":
            if (
                receipt["expected_direct_roots"]
                != list(PINRY_DIRECT_MD5_ROOTS)
                or sorted(receipt["archive_root_identity"])
                != list(PINRY_DIRECT_MD5_ROOTS)
            ):
                raise FixtureError("legacy_root_archive_mismatch")
            orphan = receipt["orphan_sentinel"]
            if os.path.lexists(os.path.join(media_root, orphan["path"])):
                raise FixtureError("legacy_source_not_archived")
            _assert_backup_file(
                run_path,
                "media/{}".format(orphan["path"]),
                orphan["sha256"],
                orphan["source_identity"],
            )
    except FixtureError:
        raise
    except (KeyError, TypeError, ValueError, sqlite3.Error):
        raise FixtureError("migration_verification_failed") from None
    finally:
        connection.close()


def _validate_http_url(value, required_host=None):
    try:
        parsed = urlsplit(value)
    except (TypeError, ValueError):
        raise FixtureError("http_url_invalid") from None
    if (
        parsed.scheme != "http"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise FixtureError("http_url_invalid")
    if required_host is not None and parsed.hostname != required_host:
        raise FixtureError("http_url_invalid")
    return value.rstrip("/")


def _wait_http(url, timeout):
    if timeout <= 0 or timeout > 600:
        raise FixtureError("http_timeout_invalid")
    url = _validate_http_url(url)
    import requests

    deadline = time.monotonic() + timeout
    while True:
        try:
            response = requests.get(url, timeout=min(2.0, timeout))
            if response.status_code == 200:
                return
        except requests.RequestException:
            pass
        if time.monotonic() >= deadline:
            raise FixtureError("http_not_ready")
        time.sleep(0.1)


def _capture_receipt_media(data_root, receipt):
    database_path = os.path.join(data_root, "production.db")
    media_root = os.path.join(data_root, "static", "media")
    connection = _open_sqlite_read_only(database_path)
    captured = {}
    try:
        for item in receipt["items"]:
            rows = connection.execute(
                "SELECT image FROM django_images_image WHERE id = ? "
                "UNION ALL "
                "SELECT image FROM django_images_thumbnail "
                "WHERE original_id = ? ORDER BY image",
                (item["image_id"], item["image_id"]),
            ).fetchall()
            if len(rows) != 4:
                raise FixtureError("receipt_media_missing")
            captured[str(item["image_id"])] = tuple(
                (row["image"], _file_sha256(_contained_path(
                    os.path.join(media_root, row["image"]), media_root
                ))[0])
                for row in rows
            )
    except FixtureError:
        raise
    except sqlite3.Error:
        raise FixtureError("receipt_media_read_failed") from None
    finally:
        connection.close()
    return captured


def _capture_image_paths(connection, image_id):
    image_row = connection.execute(
        "SELECT image FROM django_images_image WHERE id = ?",
        (image_id,),
    ).fetchone()
    derivative_rows = connection.execute(
        "SELECT image FROM django_images_thumbnail "
        "WHERE original_id = ? ORDER BY image",
        (image_id,),
    ).fetchall()
    if image_row is None or len(derivative_rows) != 3:
        raise FixtureError("api_asset_closure_invalid")
    paths = (image_row["image"],) + tuple(
        row["image"] for row in derivative_rows
    )
    if any(not isinstance(path, str) or not path for path in paths):
        raise FixtureError("api_asset_closure_invalid")
    return paths


def _assert_asset_storage_present(
    data_root, relative_paths, expected_original_sha256
):
    from PIL import Image as PILImage

    media_root = os.path.join(data_root, "static", "media")
    for index, relative_path in enumerate(relative_paths):
        absolute = _contained_path(
            os.path.join(media_root, relative_path),
            media_root,
        )
        digest, file_stat = _file_sha256(absolute)
        if file_stat.st_size <= 0:
            raise FixtureError("api_asset_file_empty")
        if index == 0:
            if digest != expected_original_sha256:
                raise FixtureError("api_asset_original_mismatch")
            continue
        try:
            with open(absolute, "rb") as image_file:
                image = PILImage.open(image_file)
                try:
                    image.verify()
                finally:
                    image.close()
        except (OSError, ValueError):
            raise FixtureError("api_asset_derivative_invalid") from None


def _assert_asset_storage_removed(data_root, relative_paths):
    media_root = os.path.join(data_root, "static", "media")
    parent_paths = set()
    for relative_path in relative_paths:
        absolute = _contained_path(
            os.path.join(media_root, relative_path),
            media_root,
        )
        if os.path.lexists(absolute):
            raise FixtureError("api_asset_file_not_removed")
        parent_paths.add(os.path.dirname(absolute))
    if any(os.path.lexists(path) for path in parent_paths):
        raise FixtureError("api_asset_directory_not_removed")


def _ensure_fixture_authentication(  # noqa: C901
    session, base_url, mode, data_root
):
    basic_auth = (_FIXTURE_USERNAME, _FIXTURE_PASSWORD)
    profile_url = "{}/api/v2/profile/users/".format(base_url)

    def token_headers(
        auth=None, headers=None, expected_token=None,
        expected_username=None,
    ):
        try:
            response = session.get(
                profile_url,
                auth=auth,
                headers=headers,
                timeout=10,
                allow_redirects=False,
            )
            if response.status_code in (401, 403):
                return None
            if response.status_code != 200:
                raise FixtureError("api_auth_probe_failed")
            payload = response.json()
        except Exception:
            raise FixtureError("api_auth_probe_failed") from None
        if (
            not isinstance(payload, list)
            or len(payload) != 1
            or not isinstance(payload[0], dict)
            or not isinstance(payload[0].get("username"), str)
            or not payload[0]["username"]
            or not isinstance(payload[0].get("token"), str)
            or re.fullmatch(r"[0-9a-f]{40}", payload[0]["token"]) is None
        ):
            raise FixtureError("api_auth_probe_failed")
        if (
            expected_username is not None
            and payload[0]["username"] != expected_username
        ):
            return None
        if (
            expected_token is not None
            and payload[0]["token"] != expected_token
        ):
            raise FixtureError("api_auth_probe_failed")
        return {"Authorization": "Token {}".format(payload[0]["token"])}

    headers = token_headers(
        auth=basic_auth,
        expected_username=_FIXTURE_USERNAME,
    )
    if headers is not None:
        return headers
    try:
        connection = _open_sqlite_read_only(
            os.path.join(data_root, "production.db")
        )
        try:
            rows = connection.execute(
                "SELECT token.key "
                "FROM authtoken_token AS token "
                "JOIN auth_user AS user ON user.id = token.user_id "
                "WHERE user.is_active = 1 "
                "ORDER BY token.key LIMIT 32"
            ).fetchall()
        finally:
            connection.close()
    except (FixtureError, sqlite3.Error):
        raise FixtureError("api_auth_probe_failed") from None
    for row in rows:
        token = row[0]
        if (
            not isinstance(token, str)
            or re.fullmatch(r"[0-9a-f]{40}", token) is None
        ):
            continue
        headers = token_headers(
            headers={"Authorization": "Token {}".format(token)},
            expected_token=token,
        )
        if headers is not None:
            return headers
    if mode != "new":
        raise FixtureError("migrated_auth_failed")
    try:
        registered = session.post(
            profile_url,
            data={
                "username": _FIXTURE_USERNAME,
                "email": "fixture-reviewer@example.invalid",
                "password": _FIXTURE_PASSWORD,
                "password_repeat": _FIXTURE_PASSWORD,
            },
            timeout=10,
            allow_redirects=False,
        )
    except Exception:
        raise FixtureError("api_registration_failed") from None
    if registered.status_code not in (201, 400):
        raise FixtureError("api_registration_failed")
    headers = token_headers(
        auth=basic_auth,
        expected_username=_FIXTURE_USERNAME,
    )
    if headers is None:
        raise FixtureError("api_auth_probe_failed")
    return headers


def _response_pin_identity(response):
    try:
        payload = response.json()
        pin_id = payload["id"]
        image_id = payload["image"]["id"]
    except (KeyError, TypeError, ValueError):
        raise FixtureError("api_response_invalid") from None
    if type(pin_id) is not int or type(image_id) is not int:
        raise FixtureError("api_response_invalid")
    return pin_id, image_id


def _api_check(  # noqa: C901
    mode, base_url, remote_url, data_root, receipt_path=None
):
    base_url = _validate_http_url(base_url)
    remote_url = _validate_http_url(remote_url, required_host="image-source")
    data_root = os.path.abspath(os.fspath(data_root))
    receipt = None
    before_receipt = None
    if mode == "migrated":
        if receipt_path is None:
            raise FixtureError("receipt_required")
        receipt = _load_receipt(receipt_path, data_root)
        before_receipt = _capture_receipt_media(data_root, receipt)
    elif receipt_path is not None:
        raise FixtureError("receipt_not_allowed")

    import requests

    session = requests.Session()
    session.trust_env = False
    headers = _ensure_fixture_authentication(
        session, base_url, mode, data_root
    )
    created_pins = []
    connection = _open_sqlite_read_only(
        os.path.join(data_root, "production.db")
    )
    try:
        before_counts = tuple(connection.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM django_images_image), "
            "(SELECT COUNT(*) FROM core_mediaasset)"
        ).fetchone())
    finally:
        connection.close()
    try:
        local_response = session.post(
            "{}/api/v2/pins/".format(base_url),
            headers=headers,
            data={"private": "false", "description": "fixture local"},
            files={
                "image_file": (
                    "fixture-local.png",
                    _deterministic_png(0),
                    "image/png",
                )
            },
            timeout=30,
        )
        if local_response.status_code != 201:
            raise FixtureError("api_local_upload_failed")
        local_pin, local_image = _response_pin_identity(local_response)
        created_pins.append(local_pin)
        duplicate_response = session.post(
            "{}/api/v2/pins/".format(base_url),
            headers=headers,
            data={"private": "false", "description": "fixture duplicate"},
            files={
                "image_file": (
                    "fixture-local-copy.png",
                    _deterministic_png(0),
                    "image/png",
                )
            },
            timeout=30,
        )
        if duplicate_response.status_code != 201:
            raise FixtureError("api_duplicate_upload_failed")
        duplicate_pin, duplicate_image = _response_pin_identity(
            duplicate_response
        )
        created_pins.append(duplicate_pin)
        remote_response = session.post(
            "{}/api/v2/pins/".format(base_url),
            headers=headers,
            json={
                "url": remote_url,
                "referer": remote_url,
                "private": False,
                "description": "fixture remote",
            },
            timeout=30,
        )
        if remote_response.status_code != 201:
            raise FixtureError("api_remote_upload_failed")
        remote_pin, remote_image = _response_pin_identity(remote_response)
        created_pins.append(remote_pin)
        if (
            local_image != duplicate_image
            or remote_image == local_image
            or len({local_pin, duplicate_pin, remote_pin}) != 3
        ):
            raise FixtureError("api_dedup_failed")
        if mode == "migrated" and local_image != receipt["items"][0]["image_id"]:
            raise FixtureError("api_historical_dedup_failed")
        connection = _open_sqlite_read_only(
            os.path.join(data_root, "production.db")
        )
        try:
            local_paths = _capture_image_paths(connection, local_image)
            remote_paths = _capture_image_paths(connection, remote_image)
        finally:
            connection.close()
        _assert_asset_storage_present(
            data_root,
            local_paths,
            _sha256(_deterministic_png(0)),
        )
        _assert_asset_storage_present(
            data_root,
            remote_paths,
            _sha256(_deterministic_png(10000)),
        )
        for pin_id in tuple(created_pins):
            deleted = session.delete(
                "{}/api/v2/pins/{}/".format(base_url, pin_id),
                headers=headers,
                timeout=30,
            )
            if deleted.status_code != 204:
                raise FixtureError("api_delete_failed")
            created_pins.remove(pin_id)
        connection = _open_sqlite_read_only(
            os.path.join(data_root, "production.db")
        )
        try:
            local_existing = connection.execute(
                "SELECT 1 FROM django_images_image WHERE id = ?",
                (local_image,),
            ).fetchone()
            remote_existing = connection.execute(
                "SELECT 1 FROM django_images_image WHERE id = ?",
                (remote_image,),
            ).fetchone()
            after_counts = tuple(connection.execute(
                "SELECT "
                "(SELECT COUNT(*) FROM django_images_image), "
                "(SELECT COUNT(*) FROM core_mediaasset)"
            ).fetchone())
            if mode == "new" and local_existing is not None:
                raise FixtureError("api_delete_incomplete")
            if mode == "migrated" and local_existing is None:
                raise FixtureError("api_historical_asset_deleted")
            if remote_existing is not None:
                raise FixtureError("api_remote_delete_incomplete")
            if after_counts != before_counts:
                raise FixtureError("api_asset_count_changed")
        finally:
            connection.close()
        if mode == "new":
            _assert_asset_storage_removed(data_root, local_paths)
        _assert_asset_storage_removed(data_root, remote_paths)
        staging_root = os.path.join(data_root, "static", "media", ".staging")
        if os.path.isdir(staging_root):
            for _root, _directories, files in os.walk(staging_root):
                if any(name.endswith(".part") for name in files):
                    raise FixtureError("api_staging_leak")
        if receipt is not None and _capture_receipt_media(
            data_root, receipt
        ) != before_receipt:
            raise FixtureError("migrated_media_changed")
    except FixtureError:
        raise
    except requests.RequestException:
        raise FixtureError("api_request_failed") from None
    finally:
        for pin_id in created_pins:
            try:
                session.delete(
                    "{}/api/v2/pins/{}/".format(base_url, pin_id),
                    headers=headers,
                    timeout=10,
                )
            except requests.RequestException:
                pass
        session.close()


def _private_regular(path, owner_uid=None, owner_gid=None):
    try:
        file_stat = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return (
        stat.S_ISREG(file_stat.st_mode)
        and file_stat.st_nlink == 1
        and stat.S_IMODE(file_stat.st_mode) == 0o600
        and (owner_uid is None or file_stat.st_uid == owner_uid)
        and (owner_gid is None or file_stat.st_gid == owner_gid)
    )


def _runtime_service_child(data_root, service_uid, service_gid, write_fd):
    own_name = ".fixture-service-owned-{}".format(uuid.uuid4().hex)
    moved_name = "{}.moved".format(own_name)
    root_name = ".fixture-root-owned"
    root_moved = "{}.moved".format(root_name)
    descriptor = None
    root_descriptor = None
    try:
        os.setgroups([])
        os.setgid(service_gid)
        os.setuid(service_uid)
        if os.geteuid() != service_uid or os.getgroups():
            raise OSError("identity")
        root_descriptor = os.open(
            data_root,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        descriptor = os.open(
            own_name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=root_descriptor,
        )
        os.write(descriptor, b"service-owned")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
        descriptor = None
        os.rename(
            own_name,
            moved_name,
            src_dir_fd=root_descriptor,
            dst_dir_fd=root_descriptor,
        )
        os.unlink(moved_name, dir_fd=root_descriptor)
        for operation in ("open", "unlink", "rename"):
            try:
                if operation == "open":
                    opened = os.open(
                        root_name,
                        os.O_RDONLY,
                        dir_fd=root_descriptor,
                    )
                    os.close(opened)
                elif operation == "unlink":
                    os.unlink(root_name, dir_fd=root_descriptor)
                else:
                    os.rename(
                        root_name,
                        root_moved,
                        src_dir_fd=root_descriptor,
                        dst_dir_fd=root_descriptor,
                    )
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EPERM):
                    raise
            else:
                raise OSError("sticky_root_entry_accessible")
        os.write(write_fd, b"1")
    except BaseException:
        try:
            os.write(write_fd, b"0")
        except BaseException:
            pass
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except BaseException:
                pass
        if root_descriptor is not None:
            for name in (own_name, moved_name):
                try:
                    os.unlink(name, dir_fd=root_descriptor)
                except BaseException:
                    pass
            try:
                os.close(root_descriptor)
            except BaseException:
                pass
        try:
            os.close(write_fd)
        except BaseException:
            pass
        os._exit(0)


def _assert_runtime(data_root, project_settings):  # noqa: C901
    if not sys.platform.startswith("linux") or not hasattr(os, "fork"):
        raise FixtureError("runtime_linux_required")
    if os.geteuid() != 0:
        raise FixtureError("runtime_root_required")
    try:
        account = pwd.getpwnam("www-data")
    except KeyError:
        raise FixtureError("runtime_service_missing") from None
    service_uid, service_gid = account.pw_uid, account.pw_gid
    data_root = os.path.abspath(os.fspath(data_root))
    project_settings = os.path.abspath(os.fspath(project_settings))
    root_stat = os.stat(data_root, follow_symlinks=False)
    lock_path = os.path.join(data_root, ".svrx-pinry-startup.lock")
    persistent_settings = os.path.join(data_root, "local_settings.py")
    if (
        not stat.S_ISDIR(root_stat.st_mode)
        or root_stat.st_uid != 0
        or root_stat.st_gid != service_gid
        or stat.S_IMODE(root_stat.st_mode) != 0o1770
        or not _private_regular(persistent_settings, 0, 0)
        or not _private_regular(project_settings, service_uid, service_gid)
        or not _private_regular(lock_path, 0, 0)
    ):
        raise FixtureError("runtime_permissions_invalid")
    generated_secret = os.path.join(data_root, "production_secret_key.txt")
    if os.path.lexists(generated_secret) and not _private_regular(
        generated_secret, 0, 0
    ):
        raise FixtureError("runtime_permissions_invalid")
    pycache = os.path.join(os.path.dirname(project_settings), "__pycache__")
    if os.path.isdir(pycache):
        for name in os.listdir(pycache):
            if name.startswith("local_settings.") and name.endswith(".pyc"):
                if not _private_regular(
                    os.path.join(pycache, name), service_uid, service_gid
                ):
                    raise FixtureError("runtime_pyc_permissions_invalid")
    lock_descriptor = os.open(
        lock_path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                raise FixtureError("runtime_lifetime_lock_invalid") from None
        else:
            fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            raise FixtureError("runtime_lifetime_lock_invalid")
    finally:
        os.close(lock_descriptor)
    sentinel_path = os.path.join(data_root, ".fixture-root-owned")
    sentinel_moved = "{}.moved".format(sentinel_path)
    descriptor = os.open(
        sentinel_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.write(descriptor, b"root-owned")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    read_fd, write_fd = os.pipe()
    try:
        child_pid = os.fork()
        if child_pid == 0:
            os.close(read_fd)
            _runtime_service_child(data_root, service_uid, service_gid, write_fd)
        os.close(write_fd)
        write_fd = None
        result = os.read(read_fd, 2)
        waited_pid, wait_status = os.waitpid(child_pid, 0)
        if (
            result != b"1"
            or waited_pid != child_pid
            or not os.WIFEXITED(wait_status)
            or os.WEXITSTATUS(wait_status) != 0
        ):
            raise FixtureError("runtime_sticky_contract_invalid")
    finally:
        try:
            os.close(read_fd)
        except OSError:
            pass
        if write_fd is not None:
            os.close(write_fd)
        for path in (sentinel_path, sentinel_moved):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
    try:
        from django_images.services.startup_preflight import (
            validate_storage_runtime_preflight,
        )

        result = validate_storage_runtime_preflight(
            os.path.join(data_root, "static", "media"),
            service_uid,
            service_gid,
        )
        if not result.ok:
            raise FixtureError("runtime_storage_probe_failed")
    except FixtureError:
        raise
    except Exception:
        raise FixtureError("runtime_storage_probe_failed") from None
    import requests

    try:
        response = requests.get(
            "http://127.0.0.1/api/v2/version/",
            timeout=5,
        )
    except requests.RequestException:
        raise FixtureError("runtime_http_boot_failed") from None
    if response.status_code != 200:
        raise FixtureError("runtime_http_boot_failed")


def _stat_file_bytes(path):
    payload, file_stat = _read_regular_file(path, maximum=1024 * 1024)
    return payload, (file_stat.st_dev, file_stat.st_ino, file_stat.st_size)


def _verify_direct_rename_conflict(adapter, source, destination, expected_errno):
    source_parent = os.path.dirname(source)
    destination_parent = os.path.dirname(destination)
    source_name = os.path.basename(source)
    destination_name = os.path.basename(destination)
    source_before = _stat_file_bytes(source)
    destination_before = _stat_file_bytes(destination)
    source_fd = os.open(
        source_parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    destination_fd = os.open(
        destination_parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        try:
            adapter.rename_noreplace(
                source_fd,
                source_name,
                destination_fd,
                destination_name,
            )
        except OSError as error:
            if error.errno != expected_errno:
                raise FixtureError("atomic_archive_errno_invalid") from None
        else:
            raise FixtureError("atomic_archive_overwrite")
    finally:
        os.close(source_fd)
        os.close(destination_fd)
    if (
        _stat_file_bytes(source) != source_before
        or _stat_file_bytes(destination) != destination_before
    ):
        raise FixtureError("atomic_archive_clobbered")


def _verify_direct_cross_filesystem(adapter, source, destination):
    source_before = _stat_file_bytes(source)
    if os.path.lexists(destination):
        raise FixtureError("atomic_archive_destination_exists")
    source_fd = os.open(
        os.path.dirname(source),
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    destination_fd = os.open(
        os.path.dirname(destination),
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        try:
            adapter.rename_noreplace(
                source_fd,
                os.path.basename(source),
                destination_fd,
                os.path.basename(destination),
            )
        except OSError as error:
            if error.errno != errno.EXDEV:
                raise FixtureError("atomic_archive_errno_invalid") from None
        else:
            raise FixtureError("atomic_archive_crossed_filesystems")
    finally:
        os.close(source_fd)
        os.close(destination_fd)
    if _stat_file_bytes(source) != source_before or os.path.lexists(destination):
        raise FixtureError("atomic_archive_clobbered")


def _verify_atomic_archive(data_root, cross_root):
    if not sys.platform.startswith("linux"):
        raise FixtureError("atomic_archive_linux_required")
    data_root = os.path.abspath(os.fspath(data_root))
    cross_root = os.path.abspath(os.fspath(cross_root))
    data_stat = os.stat(data_root, follow_symlinks=False)
    cross_stat = os.stat(cross_root, follow_symlinks=False)
    if (
        not stat.S_ISDIR(data_stat.st_mode)
        or not stat.S_ISDIR(cross_stat.st_mode)
        or data_stat.st_dev == cross_stat.st_dev
    ):
        raise FixtureError("cross_filesystem_required")
    os.environ.setdefault(
        "DJANGO_SETTINGS_MODULE",
        "pinry.settings.development",
    )
    import django

    django.setup()
    from django_images.services.media_archive import (
        LinuxRenameNoReplaceAdapter,
        build_archive_intent,
        open_archive_session,
    )

    same_root = tempfile.mkdtemp(prefix=".fixture-atomic-", dir=data_root)
    cross_probe = tempfile.mkdtemp(prefix=".fixture-cross-", dir=cross_root)
    try:
        source_root = os.path.join(same_root, "source")
        destination_root = os.path.join(same_root, "destination")
        os.makedirs(os.path.join(source_root, "nested"), mode=0o700)
        os.makedirs(os.path.join(destination_root, "archive"), mode=0o700)
        source = os.path.join(source_root, "nested", "payload.bin")
        _atomic_write(source, b"same-filesystem", mode=0o600)
        source_stat = os.stat(source, follow_symlinks=False)
        intent = build_archive_intent(
            source_root,
            "nested/payload.bin",
            destination_root,
            "archive/payload.bin",
        )
        with open_archive_session(
            source_root, destination_root, intent
        ) as session:
            result = session.archive_noreplace(intent)
            if result.status != "archived":
                raise FixtureError("atomic_archive_failed")
        archived = os.path.join(destination_root, "archive", "payload.bin")
        archived_stat = os.stat(archived, follow_symlinks=False)
        if (
            os.path.lexists(source)
            or (archived_stat.st_dev, archived_stat.st_ino)
            != (source_stat.st_dev, source_stat.st_ino)
            or _read_regular_file(archived, maximum=1024)[0]
            != b"same-filesystem"
        ):
            raise FixtureError("atomic_archive_identity_invalid")

        conflict_source = os.path.join(source_root, "conflict-source.bin")
        conflict_destination = os.path.join(
            destination_root, "conflict-destination.bin"
        )
        _atomic_write(conflict_source, b"source", mode=0o600)
        _atomic_write(conflict_destination, b"destination", mode=0o600)
        adapter = LinuxRenameNoReplaceAdapter()
        _verify_direct_rename_conflict(
            adapter,
            conflict_source,
            conflict_destination,
            errno.EEXIST,
        )

        cross_source = os.path.join(source_root, "cross-source.bin")
        cross_destination = os.path.join(cross_probe, "cross-destination.bin")
        _atomic_write(cross_source, b"cross-source", mode=0o600)
        _verify_direct_cross_filesystem(
            adapter, cross_source, cross_destination
        )
    except FixtureError:
        raise
    except Exception:
        raise FixtureError("atomic_archive_failed") from None
    finally:
        shutil.rmtree(same_root, ignore_errors=True)
        shutil.rmtree(cross_probe, ignore_errors=True)


def _identity_dict(file_stat):
    return {
        "device": file_stat.st_dev,
        "inode": file_stat.st_ino,
        "size": file_stat.st_size,
    }


def _copying_observation(data_root):
    runs = _migration_runs(data_root)
    copying = [run for run in runs if run[2].get("phase") == "copying"]
    if len(copying) != 1:
        return None
    run_id, run_path, state, _run_stat = copying[0]
    manifest_path = os.path.join(run_path, _MEDIA_MANIFEST)
    snapshot_path = os.path.join(run_path, _SNAPSHOT_FILENAME)
    try:
        events, manifest_prefix, manifest_stat = _load_json_lines(
            manifest_path,
            allow_torn_tail=True,
        )
        if not any(
            event.get("event") in ("published", "committed")
            for event in events
        ):
            return None
        snapshot_sha256, snapshot_stat = _file_sha256(snapshot_path)
        state_payload = json.dumps(
            state,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii") + b"\n"
    except FixtureError:
        return None
    return {
        "schema_version": 1,
        "run_id": run_id,
        "run_count": len(runs),
        "state_sha256": _sha256(state_payload),
        "snapshot": {
            "identity": _identity_dict(snapshot_stat),
            "sha256": snapshot_sha256,
        },
        "manifest": {
            "identity": _identity_dict(manifest_stat),
            "observed_size": len(manifest_prefix),
            "prefix_sha256": _sha256(manifest_prefix),
        },
    }


def _record_resume(data_root, output_path, timeout):
    if timeout <= 0 or timeout > 600:
        raise FixtureError("resume_timeout_invalid")
    data_root = os.path.abspath(os.fspath(data_root))
    output_path = _contained_path(output_path, data_root)
    if os.path.lexists(output_path):
        observation, _raw, file_stat = _load_json_file(output_path)
        if stat.S_IMODE(file_stat.st_mode) != 0o600:
            raise FixtureError("resume_observation_permissions_invalid")
        _validate_resume_observation(observation)
        _verify_recorded_copying(data_root, observation)
        first_stopped = _copying_observation(data_root)
        time.sleep(0.1)
        second_stopped = _copying_observation(data_root)
        if first_stopped is None or first_stopped != second_stopped:
            raise FixtureError("resume_stopped_state_changed")
        prior_manifest = observation["manifest"]
        stopped_manifest = first_stopped["manifest"]
        if (
            first_stopped["run_id"] != observation["run_id"]
            or first_stopped["run_count"] != observation["run_count"]
            or first_stopped["snapshot"] != observation["snapshot"]
            or stopped_manifest["identity"]["device"]
            != prior_manifest["identity"]["device"]
            or stopped_manifest["identity"]["inode"]
            != prior_manifest["identity"]["inode"]
            or stopped_manifest["observed_size"]
            < prior_manifest["observed_size"]
        ):
            raise FixtureError("resume_stopped_state_changed")
        payload = json.dumps(
            first_stopped,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii") + b"\n"
        _atomic_write(output_path, payload, mode=0o600)
        return
    deadline = time.monotonic() + timeout
    observation = None
    while observation is None:
        observation = _copying_observation(data_root)
        if observation is not None:
            break
        if time.monotonic() >= deadline:
            raise FixtureError("resume_copying_not_observed")
        time.sleep(0.05)
    payload = json.dumps(
        observation,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii") + b"\n"
    _atomic_write(output_path, payload, mode=0o600)


def _linear_commit_snapshot(data_root, allow_torn_tail=False):
    matches = glob.glob(os.path.join(
        data_root, "**", "linear-migration-v1.jsonl"
    ), recursive=True)
    if len(matches) != 1:
        raise FixtureError("linear_journal_missing")
    raw, journal_stat = _read_regular_file(matches[0])
    lines = raw.splitlines(keepends=True)
    commits = []
    prefix_end = None
    consumed = 0
    for index, line in enumerate(lines):
        if not line.endswith(b"\n"):
            if allow_torn_tail and index == len(lines) - 1:
                break
            raise FixtureError("linear_journal_invalid")
        try:
            frame = json.loads(line.decode("ascii"))
            payload = frame["payload"]
            canonical = json.dumps(
                payload, ensure_ascii=True, sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
            if hashlib.sha256(canonical).hexdigest() != frame["checksum"]:
                raise ValueError
        except (KeyError, TypeError, ValueError, UnicodeError):
            raise FixtureError("linear_journal_invalid") from None
        consumed += len(line)
        if payload.get("event") == "batch_commit":
            commits.append(payload)
            if prefix_end is None:
                prefix_end = consumed
    prefix = None if prefix_end is None else raw[:prefix_end]
    return commits, prefix, journal_stat


def _record_linear_commit(data_root, output_path, timeout):
    if timeout <= 0 or timeout > 600:
        raise FixtureError("resume_timeout_invalid")
    data_root = os.path.abspath(os.fspath(data_root))
    output_path = _contained_path(output_path, data_root)
    if os.path.lexists(output_path):
        raise FixtureError("linear_commit_observation_exists")
    deadline = time.monotonic() + timeout
    while True:
        try:
            commits, journal_prefix, journal_stat = _linear_commit_snapshot(
                data_root, allow_torn_tail=True
            )
        except FixtureError as error:
            if str(error) != "linear_journal_missing":
                raise
        else:
            if commits:
                if len(commits) != 1:
                    raise FixtureError("first_linear_commit_missed")
                payload = {
                    "schema_version": 2,
                    "last_committed_batch": len(commits),
                    "batch_id": commits[0].get("batch_id"),
                    "commit_sha256": hashlib.sha256(
                        json.dumps(
                            commits[0], ensure_ascii=True, sort_keys=True,
                            separators=(",", ":"),
                        ).encode("ascii")
                    ).hexdigest(),
                    "journal_identity": {
                        "device": journal_stat.st_dev,
                        "inode": journal_stat.st_ino,
                    },
                    "journal_prefix_size": len(journal_prefix),
                    "journal_prefix_sha256": _sha256(journal_prefix),
                }
                _atomic_write(
                    output_path,
                    json.dumps(payload, sort_keys=True,
                               separators=(",", ":")).encode("ascii")
                    + b"\n",
                    mode=0o600,
                )
                return
        if time.monotonic() >= deadline:
            raise FixtureError("linear_commit_not_observed")
        time.sleep(0.05)


def _verify_linear_commit(data_root, input_path):
    data_root = os.path.abspath(os.fspath(data_root))
    input_path = _contained_path(input_path, data_root)
    observation, _payload, file_stat = _load_json_file(
        input_path, maximum=65536
    )
    if stat.S_IMODE(file_stat.st_mode) != 0o600:
        raise FixtureError("linear_commit_rewritten")
    expected_keys = {
        "schema_version",
        "last_committed_batch",
        "batch_id",
        "commit_sha256",
        "journal_identity",
        "journal_prefix_size",
        "journal_prefix_sha256",
    }
    identity = (
        observation.get("journal_identity")
        if isinstance(observation, dict) else None
    )
    if (
        not isinstance(observation, dict)
        or set(observation) != expected_keys
        or observation.get("schema_version") != 2
        or observation.get("last_committed_batch") != 1
        or type(observation.get("batch_id")) is not str
        or not observation.get("batch_id")
        or not isinstance(observation.get("commit_sha256"), str)
        or re.fullmatch(
            r"[0-9a-f]{64}", observation.get("commit_sha256", "")
        ) is None
        or not isinstance(identity, dict)
        or set(identity) != {"device", "inode"}
        or any(type(identity.get(key)) is not int for key in identity)
        or any(identity.get(key) < 0 for key in identity)
        or type(observation.get("journal_prefix_size")) is not int
        or observation.get("journal_prefix_size") <= 0
        or observation.get("journal_prefix_size") > _MAX_JSON_BYTES
        or not isinstance(observation.get("journal_prefix_sha256"), str)
        or re.fullmatch(
            r"[0-9a-f]{64}",
            observation.get("journal_prefix_sha256", ""),
        ) is None
    ):
        raise FixtureError("linear_commit_rewritten")
    commits, journal_prefix, journal_stat = _linear_commit_snapshot(
        data_root, allow_torn_tail=False
    )
    matching = [
        event for event in commits
        if event.get("batch_id") == observation.get("batch_id")
    ]
    digest = hashlib.sha256(json.dumps(
        matching[0] if matching else {}, ensure_ascii=True, sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")).hexdigest()
    if (
        len(matching) != 1
        or commits.index(matching[0]) != 0
        or digest != observation.get("commit_sha256")
        or journal_stat.st_dev != identity["device"]
        or journal_stat.st_ino != identity["inode"]
        or journal_prefix is None
        or len(journal_prefix) != observation["journal_prefix_size"]
        or _sha256(journal_prefix)
        != observation["journal_prefix_sha256"]
    ):
        raise FixtureError("linear_commit_rewritten")


def _assert_version_http(url, source_commit, display_version):
    import requests

    try:
        response = requests.get(url, timeout=10, allow_redirects=False)
        value = response.json()
    except (requests.RequestException, ValueError):
        raise FixtureError("version_http_invalid") from None
    if (
        response.status_code != 200
        or response.history
        or response.url != url
        or "Location" in response.headers
        or not isinstance(value, dict)
        or set(value) != {"source_commit", "display_version"}
        or value.get("source_commit") != source_commit
        or value.get("display_version") != display_version
    ):
        raise FixtureError("version_http_invalid")


def _validate_resume_observation(value):
    if (
        not isinstance(value, dict)
        or set(value) != {
            "schema_version", "run_id", "run_count", "state_sha256",
            "snapshot", "manifest"
        }
        or value.get("schema_version") != 1
        or not isinstance(value.get("run_id"), str)
        or type(value.get("run_count")) is not int
        or value.get("run_count") < 1
        or not re.match(r"^[0-9a-f]{64}$", value.get("state_sha256", ""))
    ):
        raise FixtureError("resume_observation_invalid")
    for field in ("snapshot", "manifest"):
        entry = value.get(field)
        if not isinstance(entry, dict):
            raise FixtureError("resume_observation_invalid")
        expected = (
            {"identity", "sha256"}
            if field == "snapshot"
            else {"identity", "observed_size", "prefix_sha256"}
        )
        if set(entry) != expected:
            raise FixtureError("resume_observation_invalid")
        identity = entry.get("identity")
        if (
            not isinstance(identity, dict)
            or set(identity) != {"device", "inode", "size"}
            or any(type(identity.get(key)) is not int or identity[key] < 0
                   for key in identity)
            or identity["inode"] < 1
        ):
            raise FixtureError("resume_observation_invalid")
    if (
        not re.match(
            r"^[0-9a-f]{64}$", value["snapshot"].get("sha256", "")
        )
        or type(value["manifest"].get("observed_size")) is not int
        or value["manifest"]["observed_size"] <= 0
        or not re.match(
            r"^[0-9a-f]{64}$",
            value["manifest"].get("prefix_sha256", ""),
        )
    ):
        raise FixtureError("resume_observation_invalid")


def _verify_recorded_copying(data_root, observation):
    runs = _migration_runs(data_root)
    if len(runs) != observation["run_count"]:
        raise FixtureError("resume_stopped_state_changed")
    matching = [run for run in runs if run[0] == observation["run_id"]]
    if len(matching) != 1 or matching[0][2].get("phase") != "copying":
        raise FixtureError("resume_stopped_state_changed")
    _run_id, run_path, state, _run_stat = matching[0]
    state_payload = json.dumps(
        state,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii") + b"\n"
    if _sha256(state_payload) != observation["state_sha256"]:
        raise FixtureError("resume_stopped_state_changed")
    snapshot_sha256, snapshot_stat = _file_sha256(
        os.path.join(run_path, _SNAPSHOT_FILENAME)
    )
    if (
        _identity_dict(snapshot_stat) != observation["snapshot"]["identity"]
        or snapshot_sha256 != observation["snapshot"]["sha256"]
    ):
        raise FixtureError("resume_stopped_state_changed")
    manifest_sha256, manifest_stat = _file_sha256(
        os.path.join(run_path, _MEDIA_MANIFEST),
        limit=observation["manifest"]["observed_size"],
    )
    if (
        manifest_stat.st_dev != observation["manifest"]["identity"]["device"]
        or manifest_stat.st_ino != observation["manifest"]["identity"]["inode"]
        or manifest_stat.st_size < observation["manifest"]["observed_size"]
        or manifest_sha256 != observation["manifest"]["prefix_sha256"]
    ):
        raise FixtureError("resume_stopped_state_changed")


def _verify_resume(data_root, input_path):
    data_root = os.path.abspath(os.fspath(data_root))
    input_path = _contained_path(input_path, data_root)
    observation, _raw, observation_stat = _load_json_file(input_path)
    if stat.S_IMODE(observation_stat.st_mode) != 0o600:
        raise FixtureError("resume_observation_permissions_invalid")
    _validate_resume_observation(observation)
    runs = _migration_runs(data_root)
    if len(runs) != observation["run_count"]:
        raise FixtureError("resume_backup_count_changed")
    matching = [run for run in runs if run[0] == observation["run_id"]]
    if len(matching) != 1 or matching[0][2].get("phase") != "complete":
        raise FixtureError("resume_not_complete")
    _run_id, run_path, state, _run_stat = matching[0]
    snapshot_path = os.path.join(run_path, _SNAPSHOT_FILENAME)
    manifest_path = os.path.join(run_path, _MEDIA_MANIFEST)
    snapshot_sha256, snapshot_stat = _file_sha256(snapshot_path)
    recorded_snapshot = observation["snapshot"]
    if (
        _identity_dict(snapshot_stat) != recorded_snapshot["identity"]
        or snapshot_sha256 != recorded_snapshot["sha256"]
    ):
        raise FixtureError("resume_snapshot_changed")
    manifest_sha256, manifest_stat = _file_sha256(
        manifest_path,
        limit=observation["manifest"]["observed_size"],
    )
    recorded_manifest = observation["manifest"]
    if (
        manifest_stat.st_dev != recorded_manifest["identity"]["device"]
        or manifest_stat.st_ino != recorded_manifest["identity"]["inode"]
        or manifest_stat.st_size < recorded_manifest["observed_size"]
        or manifest_sha256 != recorded_manifest["prefix_sha256"]
    ):
        raise FixtureError("resume_manifest_prefix_changed")
    media_events, media_raw, _media_stat = _load_json_lines(manifest_path)
    planned = {
        event.get("plan", {}).get("image_id")
        for event in media_events
        if event.get("event") == "planned"
    }
    terminal = {
        event.get("image_id")
        for event in media_events
        if event.get("event") in (
            "committed", "recovered_commit", "already_current"
        )
    }
    if not planned or None in planned or not planned.issubset(terminal):
        raise FixtureError("resume_manifest_incomplete")
    if state.get("manifests", {}).get("media", {}).get(
        "manifest_sha256"
    ) != _sha256(media_raw):
        raise FixtureError("resume_manifest_hash_mismatch")
    summary, _summary_raw, _summary_stat = _load_json_file(
        os.path.join(run_path, _SUMMARY_FILENAME)
    )
    if not isinstance(summary, dict) or summary.get("phase") != "complete":
        raise FixtureError("resume_summary_invalid")


def _build_parser():
    parser = argparse.ArgumentParser(
        description="SVRx Pinry historical fixture and smoke verifier"
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.required = True

    settings_parser = subparsers.add_parser("configure-settings")
    settings_parser.add_argument("--data-root", required=True)
    settings_parser.add_argument(
        "--allow-host",
        action="append",
        default=[],
    )

    image_parser = subparsers.add_parser("write-http-fixture")
    image_parser.add_argument("--output", required=True)

    create_parser = subparsers.add_parser("create")
    create_parser.add_argument(
        "--kind",
        choices=FIXTURE_KINDS,
        required=True,
    )
    create_parser.add_argument("--data-root", required=True)
    create_parser.add_argument("--count", type=int, default=1)
    create_parser.add_argument("--receipt", required=True)

    linear_parser = subparsers.add_parser("create-linear")
    linear_parser.add_argument("--data-root", required=True)
    linear_parser.add_argument("--images", type=int, required=True)
    linear_parser.add_argument("--thumbnails", type=int, required=True)
    linear_parser.add_argument("--receipt", required=True)

    metrics_parser = subparsers.add_parser("verify-linear-metrics")
    metrics_parser.add_argument("--receipt", required=True)
    metrics_parser.add_argument("--status", required=True)

    merge_parser = subparsers.add_parser("merge-container-metrics")
    merge_parser.add_argument("--status", required=True)
    merge_parser.add_argument("--stats", required=True)
    merge_parser.add_argument("--baseline")
    merge_parser.add_argument("--supervisor")

    observer_parser = subparsers.add_parser(
        "observe-maintenance-service"
    )
    observer_parser.add_argument("--base-url", required=True)
    observer_parser.add_argument("--output", required=True)
    observer_parser.add_argument(
        "--started-at-epoch-ns", type=int, required=True
    )
    observer_parser.add_argument("--images", type=int, required=True)
    observer_parser.add_argument("--files", type=int, required=True)
    observer_parser.add_argument("--timeout", type=float, default=7200.0)
    observer_parser.add_argument(
        "--poll-interval", type=float, default=0.25
    )

    maintenance_parser = subparsers.add_parser("assert-maintenance-http")
    maintenance_parser.add_argument("--base-url", required=True)
    maintenance_parser.add_argument("--expected-state", required=True)
    maintenance_parser.add_argument("--timeout", type=float, default=30.0)
    maintenance_parser.add_argument("--min-resume-count", type=int)
    maintenance_parser.add_argument("--expected-error-code")
    maintenance_parser.add_argument("--expected-images", type=int)
    maintenance_parser.add_argument("--expected-files", type=int)
    maintenance_parser.add_argument(
        "--require-terminal", action="store_true"
    )

    fallback_parser = subparsers.add_parser(
        "assert-maintenance-fallback-http"
    )
    fallback_parser.add_argument("--base-url", required=True)
    fallback_parser.add_argument(
        "--status-mode", choices=("missing", "corrupt"), required=True
    )
    fallback_parser.add_argument("--timeout", type=float, default=30.0)

    benchmark_parser = subparsers.add_parser("benchmark-linear-service")
    benchmark_parser.add_argument("--data-root", required=True)
    benchmark_parser.add_argument("--output", required=True)
    benchmark_parser.add_argument(
        "--completion-hold-seconds", type=float, default=0.0
    )

    resume_prep_parser = subparsers.add_parser("prepare-linear-resume")
    resume_prep_parser.add_argument("--data-root", required=True)
    resume_prep_parser.add_argument("--output", required=True)

    repair_prep_parser = subparsers.add_parser("prepare-linear-repair")
    repair_prep_parser.add_argument("--data-root", required=True)
    repair_prep_parser.add_argument("--output", required=True)

    repair_run_parser = subparsers.add_parser("run-linear-repair-only")
    repair_run_parser.add_argument("--data-root", required=True)
    repair_run_parser.add_argument("--observation", required=True)

    tail_prep_parser = subparsers.add_parser(
        "prepare-linear-tail-repair"
    )
    tail_prep_parser.add_argument("--data-root", required=True)
    tail_prep_parser.add_argument(
        "--fault", choices=sorted(_LINEAR_TAIL_AUDIT_POINTS),
        required=True,
    )
    tail_prep_parser.add_argument("--output", required=True)

    tail_run_parser = subparsers.add_parser(
        "run-linear-tail-repair-only"
    )
    tail_run_parser.add_argument("--data-root", required=True)
    tail_run_parser.add_argument("--observation", required=True)
    tail_run_parser.add_argument("--output", required=True)

    crash_parser = subparsers.add_parser("exercise-linear-crash")
    crash_parser.add_argument("--data-root", required=True)
    crash_parser.add_argument("--fault", required=True)
    crash_parser.add_argument("--output", required=True)

    audit_parser = subparsers.add_parser("audit-linear-io")
    audit_parser.add_argument("--data-root", required=True)
    audit_parser.add_argument("--output", required=True)
    audit_parser.add_argument(
        "--mode", choices=("fresh", "repair", "tail-repair"),
        default="fresh",
    )
    audit_parser.add_argument("--observation")
    audit_parser.add_argument("--trace-root")

    corrupt_parser = subparsers.add_parser("corrupt-migration-manifest")
    corrupt_parser.add_argument("--data-root", required=True)

    verify_parser = subparsers.add_parser("verify-migration")
    verify_parser.add_argument("--data-root", required=True)
    verify_parser.add_argument("--receipt", required=True)

    wait_parser = subparsers.add_parser("wait-http")
    wait_parser.add_argument("--url", required=True)
    wait_parser.add_argument("--timeout", type=float, default=60.0)

    api_parser = subparsers.add_parser("api-check")
    api_parser.add_argument(
        "--mode",
        choices=("new", "migrated"),
        required=True,
    )
    api_parser.add_argument("--base-url", required=True)
    api_parser.add_argument("--remote-url", required=True)
    api_parser.add_argument("--receipt")
    api_parser.add_argument("--data-root", required=True)

    runtime_parser = subparsers.add_parser("assert-runtime")
    runtime_parser.add_argument("--data-root", required=True)
    runtime_parser.add_argument(
        "--project-settings",
        default="/pinry/pinry/settings/local_settings.py",
    )

    record_parser = subparsers.add_parser("record-resume")
    record_parser.add_argument("--data-root", required=True)
    record_parser.add_argument("--output", required=True)
    record_parser.add_argument("--timeout", type=float, default=180.0)

    linear_commit_parser = subparsers.add_parser("record-linear-commit")
    linear_commit_parser.add_argument("--data-root", required=True)
    linear_commit_parser.add_argument("--output", required=True)
    linear_commit_parser.add_argument(
        "--timeout", type=float, default=180.0
    )

    verify_commit_parser = subparsers.add_parser("verify-linear-commit")
    verify_commit_parser.add_argument("--data-root", required=True)
    verify_commit_parser.add_argument("--input", required=True)

    version_parser = subparsers.add_parser("assert-version-http")
    version_parser.add_argument("--url", required=True)
    version_parser.add_argument("--source-commit", required=True)
    version_parser.add_argument("--display-version", required=True)

    rehash_parser = subparsers.add_parser("verify-resume-rehash")
    rehash_parser.add_argument("--receipt", required=True)
    rehash_parser.add_argument("--status", required=True)
    rehash_parser.add_argument("--expected", required=True)

    resume_parser = subparsers.add_parser("verify-resume")
    resume_parser.add_argument("--data-root", required=True)
    resume_parser.add_argument("--input", required=True)

    atomic_parser = subparsers.add_parser("verify-atomic-archive")
    atomic_parser.add_argument("--data-root", required=True)
    atomic_parser.add_argument("--cross-root", required=True)
    return parser


def _dispatch(arguments):  # noqa: C901
    if arguments.command == "configure-settings":
        _configure_settings(arguments.data_root, arguments.allow_host)
    elif arguments.command == "write-http-fixture":
        _atomic_write(
            arguments.output,
            _deterministic_png(10000),
            mode=0o644,
        )
    elif arguments.command == "create":
        _create_fixture(
            arguments.kind,
            arguments.data_root,
            arguments.count,
            arguments.receipt,
        )
    elif arguments.command == "create-linear":
        _create_linear_fixture(
            arguments.data_root,
            arguments.images,
            arguments.thumbnails,
            arguments.receipt,
        )
    elif arguments.command == "verify-linear-metrics":
        _verify_linear_metrics(arguments.receipt, arguments.status)
    elif arguments.command == "merge-container-metrics":
        _merge_container_metrics(
            arguments.status,
            arguments.stats,
            baseline_path=arguments.baseline,
            supervisor_path=arguments.supervisor,
        )
    elif arguments.command == "observe-maintenance-service":
        _observe_maintenance_service(
            arguments.base_url,
            arguments.output,
            arguments.started_at_epoch_ns,
            arguments.images,
            arguments.files,
            arguments.timeout,
            arguments.poll_interval,
        )
    elif arguments.command == "assert-maintenance-http":
        _assert_maintenance_http(
            arguments.base_url,
            arguments.expected_state,
            arguments.timeout,
            minimum_resume_count=arguments.min_resume_count,
            expected_error_code=arguments.expected_error_code,
            expected_images=arguments.expected_images,
            expected_files=arguments.expected_files,
            require_terminal=arguments.require_terminal,
        )
    elif arguments.command == "assert-maintenance-fallback-http":
        _assert_maintenance_fallback_http(
            arguments.base_url,
            arguments.status_mode,
            arguments.timeout,
        )
    elif arguments.command == "benchmark-linear-service":
        _benchmark_linear_service(
            arguments.data_root,
            arguments.output,
            completion_hold_seconds=arguments.completion_hold_seconds,
        )
    elif arguments.command == "prepare-linear-resume":
        _prepare_linear_resume(arguments.data_root, arguments.output)
    elif arguments.command == "prepare-linear-repair":
        _prepare_committed_repair(arguments.data_root, arguments.output)
    elif arguments.command == "run-linear-repair-only":
        _run_linear_repair_only(
            arguments.data_root, arguments.observation
        )
    elif arguments.command == "prepare-linear-tail-repair":
        _prepare_linear_tail_repair(
            arguments.data_root,
            arguments.fault,
            arguments.output,
        )
    elif arguments.command == "run-linear-tail-repair-only":
        _run_linear_tail_repair_only(
            arguments.data_root,
            arguments.observation,
            arguments.output,
        )
    elif arguments.command == "exercise-linear-crash":
        _exercise_linear_crash(
            arguments.data_root,
            arguments.fault,
            arguments.output,
        )
    elif arguments.command == "audit-linear-io":
        _audit_linear_io(
            arguments.data_root,
            arguments.output,
            mode=arguments.mode,
            observation_path=arguments.observation,
            trace_root=arguments.trace_root,
        )
    elif arguments.command == "corrupt-migration-manifest":
        _corrupt_migration_manifest(arguments.data_root)
    elif arguments.command == "verify-migration":
        _verify_migration(arguments.data_root, arguments.receipt)
    elif arguments.command == "wait-http":
        _wait_http(arguments.url, arguments.timeout)
    elif arguments.command == "api-check":
        _api_check(
            arguments.mode,
            arguments.base_url,
            arguments.remote_url,
            arguments.data_root,
            receipt_path=arguments.receipt,
        )
    elif arguments.command == "assert-runtime":
        _assert_runtime(arguments.data_root, arguments.project_settings)
    elif arguments.command == "record-resume":
        _record_resume(
            arguments.data_root,
            arguments.output,
            arguments.timeout,
        )
    elif arguments.command == "record-linear-commit":
        _record_linear_commit(
            arguments.data_root, arguments.output, arguments.timeout
        )
    elif arguments.command == "verify-linear-commit":
        _verify_linear_commit(arguments.data_root, arguments.input)
    elif arguments.command == "assert-version-http":
        _assert_version_http(
            arguments.url, arguments.source_commit,
            arguments.display_version,
        )
    elif arguments.command == "verify-resume-rehash":
        _verify_resume_rehash(
            arguments.receipt, arguments.status, arguments.expected
        )
    elif arguments.command == "verify-resume":
        _verify_resume(arguments.data_root, arguments.input)
    elif arguments.command == "verify-atomic-archive":
        _verify_atomic_archive(arguments.data_root, arguments.cross_root)
    else:
        raise FixtureError("command_not_implemented")


def main(arguments=None):
    parser = _build_parser()
    parsed = parser.parse_args(arguments)
    try:
        _dispatch(parsed)
    except FixtureError as error:
        print("FIXTURE_ERROR:{}".format(error.code), file=sys.stderr)
        return 1
    except Exception:
        print("FIXTURE_ERROR:unexpected", file=sys.stderr)
        return 1
    print(SUCCESS[parsed.command])
    return 0


if __name__ == "__main__":
    sys.exit(main())
