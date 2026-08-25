#!/usr/bin/env python
"""SVRx Pinry 자동 레거시 이관 container smoke 보조 CLI."""

from __future__ import print_function

import argparse
import errno
import fcntl
from io import BytesIO
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
import uuid
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
    "verify-resume": "FIXTURE_RESUME_OK",
    "verify-atomic-archive": "FIXTURE_ATOMIC_ARCHIVE_OK",
}
FIXTURE_KINDS = (
    "legacy-md5",
    "transitional-fixed-slot",
    "pending-schema",
)
DERIVATIVE_KINDS = ("thumbnail", "standard", "square")
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


def _prepared_files(media_root, index, asset_uuid, original_filename):
    from core.services.image_inspection import InspectedImage
    from core.services.media_storage import MediaStorage

    content = _deterministic_png(index)
    fetched = InspectedImage(
        content=content,
        image_format="PNG",
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
    legacy_kind = "original" if kind == "original" else "thumbnail"
    return "image/{}/by-md5/{}/{}/{}/{}".format(
        legacy_kind,
        digest[0],
        digest[1],
        digest,
        leaf,
    )


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


def _create_fixture(kind, data_root, count, receipt_path):
    if kind not in FIXTURE_KINDS or count < 1 or count > 512:
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
        original_filename = "fixture-original-{:03d}.png".format(index)
        prepared = _prepared_files(
            media_root,
            index,
            asset_uuid,
            original_filename,
        )
        if kind == "legacy-md5":
            original_path = _legacy_md5_path(
                "original",
                "legacy-original-{:03d}.png".format(index),
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
                    "legacy-{}-{:03d}.png".format(
                        derivative_kind,
                        index,
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
        "items": receipt_items,
    }
    if kind == "legacy-md5":
        legacy_root_stat = os.stat(
            os.path.join(media_root, "image"),
            follow_symlinks=False,
        )
        receipt["archive_root_identity"] = {
            "device": legacy_root_stat.st_dev,
            "inode": legacy_root_stat.st_ino,
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
    elif isinstance(value, str) and "://" in value:
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
    if (
        not isinstance(receipt, dict)
        or set(receipt) != {
            "schema_version", "kind", "count", "board_id",
            "archive_root_identity", "items"
        }
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


def _canonical_paths(asset_uuid, original_filename):
    from django_images.paths import (
        canonical_derivative_path,
        canonical_original_path,
    )

    try:
        canonical_uuid = str(uuid.UUID(str(asset_uuid)))
        original = canonical_original_path(
            canonical_uuid,
            original_filename,
            ".png",
        )
        derivatives = {
            kind: canonical_derivative_path(canonical_uuid, kind, ".png")
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
                image_row["asset_uuid"], filename
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
                archived_root = os.path.join(run_path, "media", "image")
                archived_root_stat = os.stat(
                    archived_root, follow_symlinks=False
                )
                root_identity = receipt["archive_root_identity"]
                if (
                    os.path.lexists(os.path.join(media_root, "image"))
                    or not stat.S_ISDIR(archived_root_stat.st_mode)
                    or archived_root_stat.st_dev != root_identity.get("device")
                    or archived_root_stat.st_ino != root_identity.get("inode")
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


def _ensure_fixture_authentication(session, base_url, mode):
    basic_auth = (_FIXTURE_USERNAME, _FIXTURE_PASSWORD)
    profile_url = "{}/api/v2/profile/users/".format(base_url)

    def token_headers():
        try:
            response = session.get(
                profile_url,
                auth=basic_auth,
                timeout=10,
            )
            payload = response.json() if response.status_code == 200 else None
        except Exception:
            raise FixtureError("api_auth_probe_failed") from None
        if (
            not isinstance(payload, list)
            or len(payload) != 1
            or payload[0].get("username") != _FIXTURE_USERNAME
            or not isinstance(payload[0].get("token"), str)
            or not payload[0]["token"]
        ):
            return None
        return {"Authorization": "Token {}".format(payload[0]["token"])}

    headers = token_headers()
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
        )
    except Exception:
        raise FixtureError("api_registration_failed") from None
    if registered.status_code not in (201, 400):
        raise FixtureError("api_registration_failed")
    headers = token_headers()
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
    headers = _ensure_fixture_authentication(session, base_url, mode)
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

    resume_parser = subparsers.add_parser("verify-resume")
    resume_parser.add_argument("--data-root", required=True)
    resume_parser.add_argument("--input", required=True)

    atomic_parser = subparsers.add_parser("verify-atomic-archive")
    atomic_parser.add_argument("--data-root", required=True)
    atomic_parser.add_argument("--cross-root", required=True)
    return parser


def _dispatch(arguments):
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
