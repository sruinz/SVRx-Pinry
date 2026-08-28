import ast
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
ACCEPTANCE_SCRIPT = (
    REPOSITORY_ROOT / "docker/tests/nas_legacy_clone_acceptance.sh"
)
FIXTURE_SCRIPT = (
    REPOSITORY_ROOT / "docker/tests/fixtures/create_legacy_fixture.py"
)


def _maintenance_status(state="migrating", **overrides):
    phases = {
        "starting": ("preparing", "이전 준비"),
        "recovering": ("recovery", "이전 상태 확인"),
        "migrating": ("copying", "이미지 파일 이전"),
        "starting_service": ("complete", "이전 완료"),
        "ready": ("complete", "준비 완료"),
        "failed": ("copying", "이전 실패"),
    }
    phase, phase_label = phases[state]
    terminal = state in ("starting_service", "ready")
    failed = state == "failed"
    status = {
        "schema_version": 1,
        "state": state,
        "phase": phase,
        "phase_label": phase_label,
        "run_id": "fixture-run",
        "attempt": 1,
        "resume_count": 1,
        "started_at": "2026-08-28T00:00:00Z",
        "phase_started_at": "2026-08-28T00:00:00Z",
        "heartbeat_at": (
            "2026-08-28T00:00:01Z" if terminal
            else "2026-08-28T00:00:00Z"
        ),
        "progress_at": (
            "2026-08-28T00:00:01Z" if terminal
            else "2026-08-28T00:00:00Z"
        ),
        "last_committed_batch": 7,
        "images_done": 1 if terminal else 0,
        "images_total": 1,
        "files_done": 1 if terminal else 0,
        "files_total": 1,
        "backfill_done": 1 if terminal else 0,
        "backfill_total": 1,
        "phase_percent": 100.0 if terminal else 0.0,
        "overall_percent": 100.0 if terminal else 0.0,
        "error_class": "fatal" if failed else None,
        "error_code": "fixture_failed" if failed else None,
    }
    status.update(overrides)
    return status


def _write_executable(path, payload):
    path.write_text(payload, encoding="utf-8")
    path.chmod(0o700)


def _write_fake_rsync(path):
    _write_executable(
        path,
        """#!/usr/bin/env python3
import os
from pathlib import Path
import shutil
import sys

with open(os.environ["PINRY_RSYNC_CAPTURE"], "ab") as stream:
    for argument in sys.argv[1:]:
        stream.write(argument.encode("utf-8") + b"\\0")

source = Path(sys.argv[-2].rstrip("/"))
destination = Path(sys.argv[-1].rstrip("/"))
if not destination.is_dir() or destination.is_symlink():
    raise SystemExit(24)
shutil.copystat(str(source), str(destination), follow_symlinks=False)
if os.environ.get("PINRY_RSYNC_FAIL") == "1":
    (destination / "partial-copy").write_bytes(b"partial")
    raise SystemExit(23)
for child in source.iterdir():
    target = destination / child.name
    if child.is_symlink():
        target.symlink_to(os.readlink(str(child)))
    elif child.is_dir():
        shutil.copytree(
            str(child),
            str(target),
            copy_function=shutil.copy2,
            symlinks=True,
        )
    else:
        shutil.copy2(str(child), str(target))
clone_mutation = os.environ.get("PINRY_RSYNC_MUTATE_CLONE")
if clone_mutation:
    target = destination / clone_mutation
    payload = bytearray(target.read_bytes())
    payload[0] ^= 1
    target.write_bytes(payload)
mutation = os.environ.get("PINRY_RSYNC_MUTATE_SOURCE")
if mutation:
    with open(mutation, "ab") as stream:
        stream.write(b"changed-during-copy")
same_metadata_mutation = os.environ.get(
    "PINRY_RSYNC_MUTATE_SOURCE_SAME_METADATA"
)
if same_metadata_mutation:
    source_stat = os.stat(same_metadata_mutation, follow_symlinks=False)
    payload = bytearray(Path(same_metadata_mutation).read_bytes())
    payload[0] ^= 1
    Path(same_metadata_mutation).write_bytes(payload)
    os.utime(
        same_metadata_mutation,
        ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns),
        follow_symlinks=False,
    )
directory_mutation = os.environ.get("PINRY_RSYNC_MUTATE_SOURCE_DIRECTORY")
if directory_mutation:
    os.chmod(directory_mutation, 0o700)
symlink_mutation = os.environ.get("PINRY_RSYNC_REPLACE_SOURCE_SYMLINK")
if symlink_mutation:
    os.unlink(symlink_mutation)
    os.symlink("static", symlink_mutation)
""",
    )


def _write_fake_docker(path):
    _write_executable(
        path,
        """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys
import time

arguments = sys.argv[1:]
with open(os.environ["PINRY_DOCKER_CAPTURE"], "a") as stream:
    stream.write(json.dumps(arguments, separators=(",", ":")) + "\\n")

if arguments[:2] == ["ps", "-q"]:
    if os.environ.get("PINRY_RUNNING_SOURCE_MOUNT") == "1":
        print("running-source")
    if os.environ.get("PINRY_RUNNING_SOURCE_PARENT_MOUNT") == "1":
        print("running-source-parent")
    if os.environ.get("PINRY_RUNNING_SOURCE_PARENT_READONLY_MOUNT") == "1":
        print("running-source-parent-readonly")
    for readonly_kind in ("data", "project", "root", "subtree"):
        if os.environ.get("PINRY_RUNNING_SOURCE_READONLY_{}_MOUNT".format(readonly_kind.upper())) == "1":
            print("running-source-readonly-{}".format(readonly_kind))
    if os.environ.get("PINRY_RUNNING_SOURCE_ANCESTOR_MOUNT") == "1":
        print("running-source-ancestor")
    if os.environ.get("PINRY_RUNNING_SOURCE_SUBTREE_MOUNT") == "1":
        print("running-source-subtree")
    if os.environ.get("PINRY_STOPPED_SOURCE_MOUNT") == "1":
        print("stopped-source")
    if os.environ.get("PINRY_RUNNING_SOURCE_VOLUME") == "1":
        print("running-source-volume")
    if os.environ.get("PINRY_RUNNING_SOURCE_ORDINARY_VOLUME") == "1":
        print("running-source-ordinary-volume")
    if os.environ.get("PINRY_RUNNING_SOURCE_ROOT_VOLUME") == "1":
        print("running-source-root-volume")
    if os.environ.get("PINRY_RUNNING_SOURCE_ROOT_MOUNT_VOLUME") == "1":
        print("running-source-root-mount-volume")
    raise SystemExit(0)

if arguments and arguments[0] == "inspect":
    joined = " ".join(arguments)
    if "State.Running" in joined and ".Mounts" in joined:
        container = arguments[-1]
        mounts = []
        if container in ("running-source", "stopped-source"):
            mounts.append({
                "Type": "bind",
                "Source": os.environ["PINRY_SOURCE_DATA"],
                "Destination": "/data",
                "RW": True,
            })
        if container in (
            "running-source-parent", "running-source-parent-readonly",
        ):
            mounts.append({
                "Type": "bind",
                "Source": str(
                    Path(os.environ["PINRY_SOURCE_DATA"]).parent.parent
                ),
                "Destination": "/host-data",
                "RW": container == "running-source-parent",
            })
        if container == "running-source-ancestor":
            mounts.append({
                "Type": "bind",
                "Source": str(
                    Path(os.environ["PINRY_SOURCE_DATA"])
                    .parent.parent.parent
                ),
                "Destination": "/host-root",
                "RW": True,
            })
        if container == "running-source-subtree":
            mounts.append({
                "Type": "bind",
                "Source": str(
                    Path(os.environ["PINRY_SOURCE_DATA"])
                    / "static"
                    / "media"
                ),
                "Destination": "/media",
                "RW": True,
            })
        if container.startswith("running-source-readonly-"):
            kind = container.rsplit("-", 1)[-1]
            source_data = Path(os.environ["PINRY_SOURCE_DATA"])
            sources = {
                "data": source_data,
                "project": source_data.parent.parent,
                "root": Path("/"),
                "subtree": source_data / "static" / "media",
            }
            mounts.append({
                "Type": "bind",
                "Source": str(sources[kind]),
                "Destination": "/readonly-source",
                "RW": False,
            })
        if container == "running-source-volume":
            mounts.append({
                "Type": "volume",
                "Name": "bind-source-volume",
                "Source": "/var/lib/docker/volumes/bind-source-volume/_data",
                "Destination": "/data",
                "RW": True,
            })
        if container == "running-source-ordinary-volume":
            mounts.append({
                "Type": "volume",
                "Name": "ordinary-source-volume",
                "Source": str(Path(os.environ["PINRY_SOURCE_DATA"]).parent.parent),
                "Destination": "/project",
                "RW": True,
            })
        if container == "running-source-root-volume":
            mounts.append({
                "Type": "volume",
                "Name": "bind-root-volume",
                "Source": "/var/lib/docker/volumes/bind-root-volume/_data",
                "Destination": "/project",
                "RW": True,
            })
        if container == "running-source-root-mount-volume":
            mounts.append({
                "Type": "volume",
                "Name": "ordinary-root-volume",
                "Source": "/",
                "Destination": "/project",
                "RW": True,
            })
        running = container != "stopped-source"
        print(
            json.dumps(running, separators=(",", ":"))
            + " "
            + json.dumps(mounts, separators=(",", ":"))
        )
        raise SystemExit(0)
    if "{{json .Mounts}}" in arguments:
        mounts = []
        if arguments[-1] in ("running-source", "stopped-source"):
            mounts.append({
                "Type": "bind",
                "Source": os.environ["PINRY_SOURCE_DATA"],
                "Destination": "/data",
                "RW": True,
            })
        print(json.dumps(mounts, separators=(",", ":")))
        raise SystemExit(0)
    if "{{.State.Running}}" in arguments:
        container = arguments[-1]
        exited = os.environ.get("PINRY_DOCKER_APP_EXITED")
        if (
            exited == "first" and container == "4" * 64
        ) or (
            exited == "second" and container == "5" * 64
        ):
            print("false")
        else:
            print("true")
        raise SystemExit(0)
    raise SystemExit(0)

if arguments[:2] == ["volume", "inspect"]:
    if arguments[-1] == "bind-source-volume":
        print(json.dumps({
            "device": os.environ["PINRY_SOURCE_DATA"],
            "o": "bind",
            "type": "none",
        }, separators=(",", ":")))
        raise SystemExit(0)
    if arguments[-1] == "ordinary-source-volume":
        print("{}")
        raise SystemExit(0)
    if arguments[-1] == "bind-root-volume":
        print(json.dumps({
            "device": "/", "o": "bind", "type": "none",
        }, separators=(",", ":")))
        raise SystemExit(0)
    if arguments[-1] == "ordinary-root-volume":
        print("{}")
        raise SystemExit(0)
    print("{}")
    raise SystemExit(0)

if arguments[:2] == ["network", "inspect"]:
    if os.environ.get("PINRY_DOCKER_NETWORK_INSPECT_FAIL") == "1":
        raise SystemExit(62)
    print(os.environ.get("PINRY_DOCKER_NETWORK_INTERNAL", "true"))
    raise SystemExit(0)

if arguments[:2] == ["image", "inspect"]:
    joined = " ".join(arguments)
    if "{{.Id}}" in joined:
        print("sha256:" + "1" * 64)
    elif "RepoDigests" in joined:
        if (
            os.environ.get("PINRY_REQUIRE_PINNED_IMAGE") == "1"
            and arguments[-1] != "sha256:" + "1" * 64
        ):
            raise SystemExit(51)
        print("fixture@sha256:" + "2" * 64)
    elif "org.opencontainers.image.revision" in joined:
        print(os.environ.get("PINRY_IMAGE_SOURCE_COMMIT", "a" * 40))
    raise SystemExit(0)

if arguments and arguments[0] == "run":
    joined = "\\n".join(arguments)
    if "-i" in arguments:
        sys.stdin.buffer.read()
    if (
        os.environ.get("PINRY_REQUIRE_PINNED_IMAGE") == "1"
        and "fixture-image:latest" in arguments
    ):
        raise SystemExit(52)
    if "NAS_SQLITE_VALIDATE" in joined:
        if os.environ.get("PINRY_SQLITE_FAIL") == "1":
            raise SystemExit(31)
        raise SystemExit(0)
    if "NAS_BACKUP_PRESERVATION_VALIDATE" in joined:
        error_code = os.environ.get("PINRY_BACKUP_PRESERVATION_ERROR")
        if error_code:
            print(error_code)
            raise SystemExit(73)
        print("NAS_BACKUP_PRESERVATION_OK")
        raise SystemExit(0)
    if "NAS_LEGACY_PIN_SAMPLE" in joined:
        legacy_sha256 = (
            "1a1a41ae64217858c74ee9ba52993e74"
            "cea29fa05237b910f176abbfd70199f5"
        )
        manifest = [{
            "kind": "image",
            "record_id": 1,
            "sha256": legacy_sha256,
        }]
        if os.environ.get("PINRY_LARGE_LEGACY_MANIFEST") == "1":
            manifest = [
                {
                    "kind": "image",
                    "record_id": record_id,
                    "sha256": legacy_sha256,
                }
                for record_id in range(1, 347)
            ] + [
                {
                    "kind": "thumbnail",
                    "record_id": record_id,
                    "sha256": legacy_sha256,
                }
                for record_id in range(1, 1039)
            ]
        print(json.dumps({
            "image_id": 1,
            "image_path": "legacy/private-photo-secret.png",
            "image_sha256": legacy_sha256,
            "manifest": manifest,
            "pin_id": 1,
            "pins": [{"image_id": 1, "pin_id": 1, "private": False}],
        }, sort_keys=True, separators=(",", ":")))
        raise SystemExit(0)
    if "NAS_CANONICAL_MEDIA_VALIDATE" in joined:
        error_code = os.environ.get("PINRY_CANONICAL_MEDIA_ERROR")
        if error_code:
            print(error_code)
            raise SystemExit(74)
        print(json.dumps({
            "image_id": 1,
            "image_path": (
                "originals/00000000-0000-4000-8000-000000000001/"
                "private-photo-secret.png"
            ),
            "image_sha256": (
                "1a1a41ae64217858c74ee9ba52993e74"
                "cea29fa05237b910f176abbfd70199f5"
            ),
            "pin_id": 1,
            "pins": [{"image_id": 1, "pin_id": 1, "private": False}],
            "images": [{
                "image_id": 1,
                "image_path": (
                    "originals/00000000-0000-4000-8000-000000000001/"
                    "private-photo-secret.png"
                ),
                "image_sha256": (
                    "1a1a41ae64217858c74ee9ba52993e74"
                    "cea29fa05237b910f176abbfd70199f5"
                ),
            }],
        }, sort_keys=True, separators=(",", ":")))
        raise SystemExit(0)
    if "NAS_EXISTING_PIN_ORM" in joined:
        print("NAS_EXISTING_PIN_ORM_OK")
        raise SystemExit(0)
    if "NAS_EXISTING_MEDIA_HTTP" in joined:
        print("NAS_EXISTING_MEDIA_HTTP_OK")
        raise SystemExit(0)
    if "NAS_ACCEPTANCE_METRICS" in joined:
        phase = arguments[-1]
        before_backup_count = int(os.environ.get(
            "PINRY_METRICS_BEFORE_BACKUP_COUNT", "0"
        ))
        before_source_commit = os.environ.get(
            "PINRY_METRICS_BEFORE_SOURCE_COMMIT", ""
        ) or None
        payload = {
            "backup_count": before_backup_count,
            "backup_present": bool(before_backup_count),
            "files": int(os.environ.get("PINRY_METRICS_FILES", "1")),
            "images": int(os.environ.get("PINRY_METRICS_IMAGES", "1")),
            "pins": int(os.environ.get("PINRY_METRICS_PINS", "1")),
            "planned_files": int(os.environ.get(
                "PINRY_METRICS_PLANNED_FILES", "1"
            )),
            "source_commit": before_source_commit,
        }
        if phase != "before":
            payload.update({
                "backup_count": int(os.environ.get(
                    "PINRY_METRICS_AFTER_BACKUP_COUNT", "1"
                )),
                "backup_present": os.environ.get(
                    "PINRY_METRICS_AFTER_BACKUP_PRESENT", "1"
                ) == "1",
                "files": int(os.environ.get(
                    "PINRY_METRICS_AFTER_FILES",
                    str(payload["planned_files"]),
                )),
                "source_commit": os.environ.get(
                    "PINRY_METRICS_AFTER_SOURCE_COMMIT", "a" * 40
                ) or None,
            })
        print(json.dumps(payload, separators=(",", ":")))
        raise SystemExit(0)
    if "NAS_WAIT_STATUS" in joined:
        wait_pid_path = os.environ.get("PINRY_WAIT_PID_PATH")
        if wait_pid_path:
            Path(wait_pid_path).write_text(str(os.getpid()))
            time.sleep(60)
        wait_error = os.environ.get("PINRY_WAIT_ERROR")
        if wait_error:
            print("WAIT_ERROR:" + wait_error)
            raise SystemExit(72)
        print(json.dumps({
            "batches": 7,
            "max_heartbeat_gap_seconds": 1.5,
        }, separators=(",", ":")))
        raise SystemExit(0)
    if "wait-http" in arguments:
        print("FIXTURE_HTTP_READY")
        raise SystemExit(0)
    if "assert-maintenance-http" in arguments:
        if (
            os.environ.get("PINRY_REJECT_TERMINAL_INITIAL") == "1"
            and "--require-terminal" in arguments
        ):
            print(
                "FIXTURE_ERROR:maintenance_progress_not_observed",
                file=sys.stderr,
            )
            raise SystemExit(71)
        terminal_error = os.environ.get(
            "PINRY_MAINTENANCE_TERMINAL_ERROR_CODE"
        )
        if terminal_error:
            print(
                "FIXTURE_ERROR:maintenance_failed:" + terminal_error,
                file=sys.stderr,
            )
            raise SystemExit(71)
        fixture_error = os.environ.get(
            "PINRY_MAINTENANCE_FIXTURE_ERROR"
        )
        if fixture_error:
            print("FIXTURE_ERROR:" + fixture_error, file=sys.stderr)
            raise SystemExit(71)
        print("FIXTURE_MAINTENANCE_HTTP_OK")
        raise SystemExit(0)
    if "api-check" in arguments:
        print("FIXTURE_API_OK")
        raise SystemExit(0)
    if "-d" in arguments:
        alias = None
        if "--network-alias" in arguments:
            index = arguments.index("--network-alias")
            alias = arguments[index + 1]
        if alias == "app":
            state_path = Path(os.environ["PINRY_DOCKER_STATE"])
            starts = int(state_path.read_text() or "0")
            starts += 1
            state_path.write_text(str(starts))
            if (
                os.environ.get("PINRY_DOCKER_APP_START_FAIL") == "1"
                and starts == 1
            ):
                mutation = os.environ.get(
                    "PINRY_DOCKER_APP_START_MUTATE_SOURCE"
                )
                if mutation:
                    with open(mutation, "ab") as stream:
                        stream.write(b"changed-during-acceptance")
                raise SystemExit(41)
            if (
                os.environ.get("PINRY_SECOND_START_MUTATE_CLONE") == "1"
                and starts == 2
            ):
                for argument in arguments:
                    if "dst=/data" not in argument:
                        continue
                    fields = dict(
                        field.split("=", 1)
                        for field in argument.split(",")
                        if "=" in field
                    )
                    with open(
                        os.path.join(fields["src"], "production.db"),
                        "ab",
                    ) as stream:
                        stream.write(b"noop-mutation")
            if (
                os.environ.get("PINRY_SECOND_START_MUTATE_MEDIA") == "1"
                and starts == 2
            ):
                for argument in arguments:
                    if "dst=/data" not in argument:
                        continue
                    fields = dict(
                        field.split("=", 1)
                        for field in argument.split(",")
                        if "=" in field
                    )
                    media_path = os.path.join(
                        fields["src"],
                        "static",
                        "media",
                        "private-photo-secret.png",
                    )
                    payload = bytearray(Path(media_path).read_bytes())
                    payload[0] ^= 1
                    Path(media_path).write_bytes(payload)
        if alias == "image-source":
            print("3" * 64)
        elif starts == 1:
            print("4" * 64)
        else:
            print("5" * 64)
        raise SystemExit(0)
    raise SystemExit(0)

if arguments[:2] == ["network", "create"]:
    if os.environ.get("PINRY_DOCKER_NETWORK_CREATE_FAIL") == "1":
        raise SystemExit(61)
    print("6" * 64)
    raise SystemExit(0)

if arguments and arguments[0] in (
    "port", "stop", "rm", "wait", "logs"
):
    raise SystemExit(0)

if arguments[:2] == ["network", "rm"]:
    raise SystemExit(0)

raise SystemExit(0)
""",
    )


def _write_fake_chmod(path):
    _write_executable(
        path,
        """#!/bin/sh
printf 'chmod leaked path: %s\\n' "$1" >&2
exit 1
""",
    )


def _write_fingerprint_guard(path):
    _write_executable(
        path,
        """#!/bin/sh
if [ "$1" = "-" ] && [ "$2" = "$PINRY_SOURCE_DATA" ]; then
    printf 'called\n' >"$PINRY_FINGERPRINT_MARKER"
    if [ "$PINRY_FAIL_SOURCE_FINGERPRINT" = "1" ]; then
        exit 97
    fi
fi
exec {} "$@"
""".format(shlex.quote(sys.executable)),
    )


def _write_result_publish_race_sitecustomize(path):
    path.write_text(
        """import ctypes
import errno
import os
import stat
import sys

_real_cdll = ctypes.CDLL
_real_ftruncate = os.ftruncate
_real_fsync = os.fsync
_real_link = os.link
_real_open = os.open
_real_replace = os.replace
_real_stat = os.stat
_real_unlink = os.unlink
_real_write = os.write
_exchange_call_count = 0
_result_fsync_count = 0
_result_stat_count = 0
_result_root = sys.argv[1] if len(sys.argv) > 1 else ""
_publish_state = sys.argv[3] if len(sys.argv) > 3 else ""
_truncate_race_injected = False


def _snapshot_running_result():
    marker_path = os.environ.get("PINRY_RESULT_RACE_MARKER")
    expected = os.environ.get("PINRY_RESULT_RACE_NAME")
    if not marker_path or not expected or os.path.exists(marker_path):
        return
    result_path = os.path.join(_result_root, expected)
    descriptor = _real_open(result_path, os.O_RDONLY)
    try:
        content = os.read(descriptor, 1024 * 1024)
    finally:
        os.close(descriptor)
    marker = _real_open(
        marker_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        _real_write(marker, content)
        _real_fsync(marker)
    finally:
        os.close(marker)


def raced_write(descriptor, content):
    if (
        os.environ.get("PINRY_RESULT_RACE_PHASE")
        == "terminal-final-write-fails"
        and _publish_state != "running"
    ):
        _snapshot_running_result()
        if b'"state":"running"' not in content:
            raise OSError(errno.EIO, os.strerror(errno.EIO))
    return _real_write(descriptor, content)


def _inject(destination, destination_dir_fd):
    expected = os.environ.get("PINRY_RESULT_RACE_NAME")
    if destination != expected:
        return
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = _real_open(
        destination,
        flags,
        0o600,
        dir_fd=destination_dir_fd,
    )
    try:
        os.write(descriptor, b"race-sentinel\\n")
        _real_fsync(descriptor)
    finally:
        os.close(descriptor)


def _replace_with_sentinel(destination, destination_dir_fd):
    os.unlink(destination, dir_fd=destination_dir_fd)
    _inject(destination, destination_dir_fd)


def raced_link(source, destination, *args, **kwargs):
    phase = os.environ.get("PINRY_RESULT_RACE_PHASE", "before-link")
    if phase == "before-link":
        _inject(destination, kwargs.get("dst_dir_fd"))
        return _real_link(source, destination, *args, **kwargs)
    result = _real_link(source, destination, *args, **kwargs)
    if phase == "after-link":
        _replace_with_sentinel(
            destination,
            kwargs.get("dst_dir_fd"),
        )
    return result


def raced_replace(source, destination, *args, **kwargs):
    _inject(destination, kwargs.get("dst_dir_fd"))
    return _real_replace(source, destination, *args, **kwargs)


def raced_stat(path, *args, **kwargs):
    global _result_stat_count
    result = _real_stat(path, *args, **kwargs)
    expected = os.environ.get("PINRY_RESULT_RACE_NAME")
    if path == expected:
        _result_stat_count += 1
        if (
            os.environ.get("PINRY_RESULT_RACE_PHASE") in (
                "after-terminal-identity",
                "after-terminal-identity-rollback-fails",
            )
            and _publish_state != "running"
            and _result_stat_count == 1
        ):
            marker_path = os.environ.get("PINRY_RESULT_RACE_MARKER")
            if marker_path:
                try:
                    marker = os.open(
                        marker_path,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                    )
                except FileExistsError:
                    return result
            else:
                marker = None
            _replace_with_sentinel(path, kwargs.get("dir_fd"))
            sentinel = _real_stat(
                path,
                dir_fd=kwargs.get("dir_fd"),
                follow_symlinks=False,
            )
            if marker is not None:
                try:
                    os.write(
                        marker,
                        "{}:{}".format(
                            sentinel.st_dev,
                            sentinel.st_ino,
                        ).encode("ascii"),
                    )
                    _real_fsync(marker)
                finally:
                    os.close(marker)
    return result


def raced_fsync(descriptor):
    global _result_fsync_count
    result = _real_fsync(descriptor)
    if (
        os.environ.get("PINRY_RESULT_RACE_PHASE")
        == "after-terminal-write"
        and _publish_state != "running"
    ):
        _result_fsync_count += 1
        if _result_fsync_count == 1:
            expected = os.environ.get("PINRY_RESULT_RACE_NAME")
            result_path = os.path.join(_result_root, expected)
            os.unlink(result_path)
            replacement = os.open(
                result_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            try:
                os.write(replacement, b"race-sentinel\\n")
                _real_fsync(replacement)
            finally:
                os.close(replacement)
    if (
        os.environ.get("PINRY_RESULT_RACE_PHASE")
        == "terminal-directory-fsync-fails"
        and _publish_state != "running"
        and stat.S_ISDIR(os.fstat(descriptor).st_mode)
    ):
        raise OSError(errno.EIO, os.strerror(errno.EIO))
    return result


def raced_ftruncate(descriptor, length):
    global _truncate_race_injected
    if (
        os.environ.get("PINRY_RESULT_RACE_PHASE")
        == "before-terminal-truncate"
        and _publish_state != "running"
        and not _truncate_race_injected
    ):
        _truncate_race_injected = True
        expected = os.environ.get("PINRY_RESULT_RACE_NAME")
        directory_fd = _real_open(
            _result_root,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            _replace_with_sentinel(expected, directory_fd)
            sentinel = _real_stat(
                expected,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        finally:
            os.close(directory_fd)
        marker_path = os.environ.get("PINRY_RESULT_RACE_MARKER")
        if marker_path:
            marker = _real_open(
                marker_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            try:
                os.write(
                    marker,
                    "{}:{}".format(
                        sentinel.st_dev,
                        sentinel.st_ino,
                    ).encode("ascii"),
                )
                _real_fsync(marker)
            finally:
                os.close(marker)
    return _real_ftruncate(descriptor, length)


def raced_open(path, flags, *args, **kwargs):
    if (
        os.environ.get("PINRY_RESULT_RACE_PHASE")
        == "before-terminal-truncate"
        and _publish_state != "running"
        and _truncate_race_injected
        and path == os.environ.get("PINRY_RESULT_RACE_NAME")
    ):
        marker_path = os.environ.get("PINRY_RESULT_REOPEN_MARKER")
        if marker_path:
            marker = _real_open(
                marker_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            os.close(marker)
    return _real_open(path, flags, *args, **kwargs)


def raced_unlink(path, *args, **kwargs):
    if (
        os.environ.get("PINRY_RESULT_RACE_PHASE")
        == "terminal-cleanup-unlink-fails"
        and _publish_state != "running"
        and path.startswith("." + os.environ["PINRY_RESULT_RACE_NAME"])
    ):
        raise OSError(errno.EIO, os.strerror(errno.EIO))
    return _real_unlink(path, *args, **kwargs)


class _SecondExchangeFails:
    def __init__(self, wrapped):
        self._wrapped = wrapped

    @property
    def argtypes(self):
        return self._wrapped.argtypes

    @argtypes.setter
    def argtypes(self, value):
        self._wrapped.argtypes = value

    @property
    def restype(self):
        return self._wrapped.restype

    @restype.setter
    def restype(self, value):
        self._wrapped.restype = value

    def __call__(self, *args):
        global _exchange_call_count
        _exchange_call_count += 1
        if _exchange_call_count == 2:
            ctypes.set_errno(errno.EIO)
            return -1
        return self._wrapped(*args)


class _SecondExchangeFailureLibc:
    def __init__(self, wrapped):
        self._wrapped = wrapped

    def __getattr__(self, name):
        value = getattr(self._wrapped, name)
        if name in ("renameatx_np", "renameat2", "syscall"):
            return _SecondExchangeFails(value)
        return value


def raced_cdll(*args, **kwargs):
    return _SecondExchangeFailureLibc(_real_cdll(*args, **kwargs))


os.link = raced_link
os.replace = raced_replace
os.ftruncate = raced_ftruncate
os.fsync = raced_fsync
os.open = raced_open
os.stat = raced_stat
os.unlink = raced_unlink
os.write = raced_write
if (
    os.environ.get("PINRY_RESULT_RACE_PHASE")
    == "after-terminal-identity-rollback-fails"
):
    ctypes.CDLL = raced_cdll
""",
        encoding="utf-8",
    )


def _write_renameat2_fallback_sitecustomize(path):
    path.write_text(
        """import ctypes
import os
import secrets
import sys

_real_cdll = ctypes.CDLL


def _value(item):
    return getattr(item, "value", item)


class _FakeSyscall:
    restype = None

    def __call__(
        self,
        _number,
        first_directory,
        first_name,
        second_directory,
        second_name,
        flag,
    ):
        if _value(flag) != 2:
            return -1
        first_directory = _value(first_directory)
        second_directory = _value(second_directory)
        first_name = _value(first_name)
        second_name = _value(second_name)
        temporary_name = (
            b".fallback-exchange-" + secrets.token_hex(8).encode("ascii")
        )
        os.rename(
            first_name,
            temporary_name,
            src_dir_fd=first_directory,
            dst_dir_fd=first_directory,
        )
        os.rename(
            second_name,
            first_name,
            src_dir_fd=second_directory,
            dst_dir_fd=first_directory,
        )
        os.rename(
            temporary_name,
            second_name,
            src_dir_fd=first_directory,
            dst_dir_fd=second_directory,
        )
        marker = os.environ.get("PINRY_RENAMEAT2_FALLBACK_MARKER")
        if marker:
            with open(marker, "ab") as stream:
                stream.write(b"used\\n")
        return 0


class _LibcWithoutRenameat2:
    def __init__(self, wrapped):
        self._wrapped = wrapped
        self.syscall = _FakeSyscall()

    def __getattr__(self, name):
        if name == "renameat2":
            raise AttributeError(name)
        return getattr(self._wrapped, name)


def _cdll_without_renameat2(*args, **kwargs):
    return _LibcWithoutRenameat2(_real_cdll(*args, **kwargs))


ctypes.CDLL = _cdll_without_renameat2
sys.platform = "linux"
""",
        encoding="utf-8",
    )


def _nul_arguments(path):
    if not path.exists():
        return []
    return [
        value.decode("utf-8")
        for value in path.read_bytes().split(b"\0")
        if value
    ]


def _tree_snapshot(root):
    snapshot = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        file_stat = path.lstat()
        digest = None
        if path.is_file() and not path.is_symlink():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        snapshot.append((
            relative,
            stat.S_IFMT(file_stat.st_mode),
            stat.S_IMODE(file_stat.st_mode),
            file_stat.st_ino,
            file_stat.st_size,
            file_stat.st_mtime_ns,
            digest,
        ))
    return snapshot


def _payload_fingerprint(media_root):
    entries = []
    for root, directories, files in os.walk(str(media_root)):
        directories[:] = sorted(directories)
        for name in sorted(files):
            path = Path(root, name)
            if not path.is_file() or path.is_symlink():
                continue
            relative = os.fsencode(str(path.relative_to(media_root)))
            content_digest = hashlib.sha256(path.read_bytes()).digest()
            entries.append((relative, path.stat().st_size, content_digest))
    manifest = hashlib.sha256()
    for relative, size, content_digest in sorted(entries):
        manifest.update(b"\0".join((
            str(len(relative)).encode("ascii"),
            relative,
            str(size).encode("ascii"),
            content_digest,
        )) + b"\0")
    return len(entries), manifest.hexdigest()


def _logical_database_digest(database_path):
    def encode(value):
        if value is None:
            payload = b""
            prefix = b"n"
        elif isinstance(value, bytes):
            payload = value
            prefix = b"b"
        elif isinstance(value, str):
            payload = value.encode("utf-8")
            prefix = b"t"
        elif isinstance(value, int):
            payload = str(value).encode("ascii")
            prefix = b"i"
        elif isinstance(value, float):
            payload = value.hex().encode("ascii")
            prefix = b"f"
        else:
            raise TypeError(type(value).__name__)
        return prefix + str(len(payload)).encode("ascii") + b":" + payload

    connection = sqlite3.connect(str(database_path))
    try:
        schema = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite\\_%' ESCAPE '\\' "
            "ORDER BY type, name"
        ).fetchall()
        digest = hashlib.sha256()
        for row in schema:
            encoded = b"".join(encode(value) for value in row)
            digest.update(b"s" + str(len(encoded)).encode("ascii"))
            digest.update(b":" + encoded)
        table_names = sorted(
            row[1] for row in schema if row[0] == "table"
        )
        for table_name in table_names:
            quoted = '"{}"'.format(table_name.replace('"', '""'))
            encoded_rows = sorted(
                b"".join(encode(value) for value in row)
                for row in connection.execute(
                    "SELECT * FROM {}".format(quoted)
                ).fetchall()
            )
            digest.update(encode(table_name))
            digest.update(encode(len(encoded_rows)))
            for encoded in encoded_rows:
                digest.update(encode(encoded))
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = ? AND name = ?",
            ("table", "sqlite_sequence"),
        ).fetchone() is not None:
            sequence_rows = sorted(
                b"".join(encode(value) for value in row)
                for row in connection.execute(
                    "SELECT name, seq FROM sqlite_sequence"
                ).fetchall()
            )
            digest.update(encode("sqlite_sequence"))
            digest.update(encode(len(sequence_rows)))
            for encoded in sequence_rows:
                digest.update(encode(encoded))
        return digest.hexdigest()
    finally:
        connection.close()


def _create_backup_fixture(data_root, source_commit="a" * 40):
    run_root = data_root / "legacy-backup/fixture-run"
    media_root = run_root / "media"
    media_root.mkdir(parents=True)
    (media_root / "linked.png").write_bytes(b"linked")
    (media_root / "orphan.png").write_bytes(b"orphan")
    (run_root / "migration-summary.json").write_text(
        json.dumps({
            "format_version": 1,
            "run_id": "fixture-run",
            "phase": "complete",
            "backup_relative_name": "legacy-backup",
            "source_commit": source_commit,
        }),
        encoding="ascii",
    )
    database_path = run_root / "production.db.before-migration"
    connection = sqlite3.connect(str(database_path))
    try:
        connection.execute(
            "CREATE TABLE core_pin (id INTEGER PRIMARY KEY)"
        )
        connection.execute(
            "CREATE TABLE django_images_image (id INTEGER PRIMARY KEY)"
        )
        connection.execute(
            "CREATE TABLE django_images_thumbnail "
            "(id INTEGER PRIMARY KEY)"
        )
        connection.execute("INSERT INTO core_pin VALUES (1)")
        connection.execute("INSERT INTO django_images_image VALUES (1)")
        connection.execute("INSERT INTO django_images_thumbnail VALUES (1)")
        connection.commit()
    finally:
        connection.close()
    return run_root, media_root, database_path


def _create_canonical_media_fixture(data_root):
    asset_uuid = "00000000-0000-4000-8000-000000000001"
    original_relative = "originals/{}/source.png".format(asset_uuid)
    originals = data_root / "static/media/originals" / asset_uuid
    derivatives = data_root / "static/media/derivatives" / asset_uuid
    originals.mkdir(parents=True)
    derivatives.mkdir(parents=True)
    original_payload = b"canonical-original"
    (data_root / "static/media" / original_relative).write_bytes(
        original_payload
    )
    thumbnail_rows = []
    manifest = [{
        "kind": "image",
        "record_id": 1,
        "sha256": hashlib.sha256(original_payload).hexdigest(),
    }]
    for thumbnail_id, size in enumerate(
        ("thumbnail", "standard", "square"), start=1
    ):
        relative = "derivatives/{}/{}.png".format(asset_uuid, size)
        thumbnail_payload = ("canonical-{}".format(size)).encode("ascii")
        (data_root / "static/media" / relative).write_bytes(thumbnail_payload)
        thumbnail_rows.append((thumbnail_id, relative, size, 1, 1, 1))
        manifest.append({
            "kind": "thumbnail",
            "record_id": thumbnail_id,
            "sha256": hashlib.sha256(thumbnail_payload).hexdigest(),
        })

    database = data_root / "production.db"
    connection = sqlite3.connect(str(database))
    try:
        connection.execute(
            "CREATE TABLE django_images_image ("
            "id INTEGER PRIMARY KEY, image TEXT NOT NULL, "
            "asset_uuid TEXT NOT NULL, original_filename TEXT NOT NULL, "
            "height INTEGER NOT NULL, width INTEGER NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE django_images_thumbnail ("
            "id INTEGER PRIMARY KEY, image TEXT NOT NULL, "
            "size TEXT NOT NULL, original_id INTEGER NOT NULL, "
            "height INTEGER NOT NULL, width INTEGER NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE core_pin ("
            "id INTEGER PRIMARY KEY, image_id INTEGER NOT NULL, "
            "private INTEGER NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE core_mediaasset ("
            "id INTEGER PRIMARY KEY, image_id INTEGER NOT NULL, "
            "content_sha256 TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO django_images_image VALUES (?, ?, ?, ?, ?, ?)",
            (1, original_relative, asset_uuid, "source.png", 1, 1),
        )
        connection.executemany(
            "INSERT INTO django_images_thumbnail VALUES (?, ?, ?, ?, ?, ?)",
            thumbnail_rows,
        )
        connection.execute("INSERT INTO core_pin VALUES (1, 1, 0)")
        connection.execute(
            "INSERT INTO core_mediaasset VALUES (?, ?, ?)",
            (1, 1, hashlib.sha256(original_payload).hexdigest()),
        )
        connection.commit()
    finally:
        connection.close()
    return {
        "image_id": 1,
        "image_path": "legacy/source.png",
        "image_sha256": hashlib.sha256(original_payload).hexdigest(),
        "manifest": manifest,
        "pin_id": 1,
        "pins": [{"image_id": 1, "pin_id": 1, "private": False}],
    }


class NasLegacyCloneAcceptanceContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        self.temporary_root = Path(temporary.name)
        self.source_project = self.temporary_root / "secret-source-project"
        self.run_root = self.temporary_root / "runs"
        self.result_root = self.temporary_root / "results"
        self.fake_bin = self.temporary_root / "bin"
        self.run_root.mkdir()
        self.result_root.mkdir()
        self.fake_bin.mkdir()
        self._create_legacy_project(self.source_project)

        self.rsync_capture = self.temporary_root / "rsync.argv"
        self.docker_capture = self.temporary_root / "docker.jsonl"
        self.docker_state = self.temporary_root / "docker.state"
        self.docker_state.write_text("0", encoding="ascii")
        _write_fake_rsync(self.fake_bin / "rsync")
        _write_fake_docker(self.fake_bin / "docker")

        self.environment = os.environ.copy()
        self.environment.update({
            "PATH": "{}:{}".format(
                self.fake_bin,
                self.environment.get("PATH", ""),
            ),
            "PINRY_DOCKER_CAPTURE": str(self.docker_capture),
            "PINRY_DOCKER_STATE": str(self.docker_state),
            "PINRY_RSYNC_CAPTURE": str(self.rsync_capture),
            "PINRY_SOURCE_DATA": str(self.source_project / "data"),
        })

    @staticmethod
    def _create_legacy_project(project):
        media_root = project / "data/static/media"
        media_root.mkdir(parents=True)
        database_path = project / "data/production.db"
        connection = sqlite3.connect(str(database_path))
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                "CREATE TABLE parent (id INTEGER PRIMARY KEY)"
            )
            connection.execute(
                "CREATE TABLE child ("
                "id INTEGER PRIMARY KEY, "
                "parent_id INTEGER REFERENCES parent(id))"
            )
            connection.execute("INSERT INTO parent (id) VALUES (1)")
            connection.execute(
                "INSERT INTO child (id, parent_id) VALUES (1, 1)"
            )
            connection.commit()
        finally:
            connection.close()
        (media_root / "private-photo-secret.png").write_bytes(
            b"legacy-media"
        )
        (project / "legacy-project-secret.txt").write_text(
            "source project marker",
            encoding="ascii",
        )

    def _run(
        self,
        run_id="case-01",
        source_project=None,
        run_root=None,
        result_root=None,
        environment=None,
        expected_pins=1,
        expected_files=1,
        expected_images=1,
        expected_thumbnails=0,
        expected_active_files=1,
        expected_source_commit="a" * 40,
    ):
        source_project = source_project or self.source_project
        run_root = run_root or self.run_root
        result_root = result_root or self.result_root
        command = [
            "/bin/bash",
            str(ACCEPTANCE_SCRIPT),
            "--source-project",
            str(source_project),
            "--run-root",
            str(run_root),
            "--run-id",
            run_id,
            "--image",
            "fixture-image:latest",
            "--result-root",
            str(result_root),
        ]
        if expected_pins is not None:
            command.extend(["--expected-pins", str(expected_pins)])
        if expected_files is not None:
            command.extend(["--expected-files", str(expected_files)])
        if expected_images is not None:
            command.extend(["--expected-images", str(expected_images)])
        if expected_thumbnails is not None:
            command.extend([
                "--expected-thumbnails", str(expected_thumbnails),
            ])
        if expected_active_files is not None:
            command.extend([
                "--expected-active-files", str(expected_active_files),
            ])
        if expected_source_commit is not None:
            command.extend([
                "--expected-source-commit", expected_source_commit,
            ])
        return subprocess.run(
            command,
            cwd=str(REPOSITORY_ROOT),
            env=environment or self.environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def _docker_calls(self):
        if not self.docker_capture.exists():
            return []
        return [
            json.loads(line)
            for line in self.docker_capture.read_text().splitlines()
        ]

    def _result_path(self, run_id):
        return self.result_root / (
            "svrx-pinry-accept-{}.json".format(run_id)
        )

    def _clone_path(self, run_id):
        return self.run_root / "svrx-pinry-accept-{}".format(run_id)

    def _backup_verifier_source(self):
        calls = [
            call for call in self._docker_calls()
            if "_NAS_BACKUP_PRESERVATION_VALIDATE" in "\n".join(call)
        ]
        self.assertGreaterEqual(len(calls), 1)
        return calls[-1][calls[-1].index("-c") + 1]

    def _canonical_media_verifier_source(self):
        calls = [
            call for call in self._docker_calls()
            if "_NAS_CANONICAL_MEDIA_VALIDATE" in "\n".join(call)
        ]
        self.assertGreaterEqual(len(calls), 1)
        return calls[-1][calls[-1].index("-c") + 1]

    def _existing_pin_orm_verifier_source(self):
        calls = [
            call for call in self._docker_calls()
            if "_NAS_EXISTING_PIN_ORM" in "\n".join(call)
        ]
        self.assertGreaterEqual(len(calls), 1)
        return calls[-1][calls[-1].index("-c") + 1]

    def _run_backup_verifier(
        self,
        helper,
        data_root,
        expected_files,
        expected_digest,
        expected_database_digest,
    ):
        original = 'data_root = "/data"'
        self.assertIn(original, helper)
        helper = helper.replace(
            original,
            "data_root = {!r}".format(str(data_root)),
            1,
        )
        return subprocess.run(
            [
                sys.executable,
                "-c",
                helper,
                str(expected_files),
                expected_digest,
                "1",
                "1",
                "1",
                "a" * 40,
                expected_database_digest,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def _run_canonical_media_verifier(
        self, helper, data_root, legacy_pin_sample
    ):
        original = 'data_root = "/data"'
        self.assertIn(original, helper)
        helper = helper.replace(
            original,
            "data_root = {!r}".format(str(data_root)),
            1,
        )
        return subprocess.run(
            [
                sys.executable,
                "-c",
                helper,
            ],
            input=json.dumps(
                legacy_pin_sample,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def _assert_public_output(self, completed, result_path=None):
        public = completed.stdout + completed.stderr
        if result_path is not None and result_path.exists():
            public += result_path.read_bytes()
        for secret in (
            str(self.temporary_root),
            "secret-source-project",
            "private-photo-secret.png",
            "legacy-project-secret.txt",
            "fixture-secret-token",
        ):
            self.assertNotIn(secret.encode("utf-8"), public)
        self.assertNotIn(b"/", public)
        self.assertNotIn(b"Traceback", public)

    def _assert_canonical_result(self, result_path):
        raw = result_path.read_bytes()
        payload = json.loads(raw.decode("ascii"))
        expected = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii") + b"\n"
        self.assertEqual(raw, expected)
        self.assertEqual(set(payload), {
            "batches",
            "completed_at",
            "duration_seconds",
            "error_code",
            "final_counts",
            "image_digest",
            "image_id",
            "legacy_backup_present",
            "legacy_counts",
            "max_heartbeat_gap_seconds",
            "noop_restart",
            "restart_count",
            "run_id",
            "schema_version",
            "source_commit",
            "source_fingerprint_unchanged",
            "started_at",
            "state",
            "temporary_pin_check",
        })
        expected_count_fields = {
            "active_files",
            "files",
            "images",
            "pins",
            "thumbnails",
        }
        for field_name in ("legacy_counts", "final_counts"):
            if payload[field_name] is not None:
                self.assertEqual(
                    set(payload[field_name]),
                    expected_count_fields,
                )
        self.assertEqual(
            stat.S_IMODE(result_path.stat().st_mode),
            0o600,
        )
        return payload

    def test_rejects_same_or_nested_source_and_clone(self):
        same_run_root = self.temporary_root / "same-runs"
        same_run_root.mkdir()
        same_source = same_run_root / "svrx-pinry-accept-same"
        self._create_legacy_project(same_source)
        same = self._run(
            run_id="same",
            source_project=same_source,
            run_root=same_run_root,
        )
        nested = self._run(
            run_id="nested",
            run_root=self.source_project,
        )

        for completed in (same, nested):
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn(
                completed.stderr.decode("utf-8").strip(),
                {"nas_clone_exists", "nas_path_overlap"},
            )
        self.assertFalse(self.rsync_capture.exists())

    def test_rejects_invalid_run_id_before_side_effects(self):
        cases = (
            self._run(run_id="../escape"),
            self._run(run_id="space id"),
            self._run(run_id=""),
        )

        for completed in cases:
            self.assertEqual(completed.returncode, 2)
            self.assertEqual(
                completed.stderr.decode("utf-8").strip(),
                "nas_arguments_invalid",
            )
        self.assertFalse(self.rsync_capture.exists())
        self.assertFalse(self.docker_capture.exists())
        self.assertFalse(list(self.run_root.iterdir()))
        self.assertFalse(list(self.result_root.iterdir()))

    def test_rejects_unexpected_legacy_workload_before_app_start(self):
        completed = self._run(
            run_id="workload-mismatch",
            expected_pins=2,
            expected_files=1,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stderr.decode("utf-8").strip(),
            "nas_legacy_workload_mismatch",
        )
        self.assertFalse(any(
            call[:2] == ["network", "create"]
            for call in self._docker_calls()
        ))

    def test_rejects_wrong_image_thumbnail_or_active_file_contract(self):
        cases = (
            ("image-mismatch", {"expected_images": 2}),
            ("thumbnail-mismatch", {"expected_thumbnails": 1}),
            ("active-file-mismatch", {"expected_active_files": 2}),
        )

        for run_id, overrides in cases:
            with self.subTest(run_id=run_id):
                completed = self._run(run_id=run_id, **overrides)
                self.assertNotEqual(completed.returncode, 0)
                self.assertEqual(
                    completed.stderr.decode("utf-8").strip(),
                    "nas_legacy_workload_mismatch",
                )
        self.assertFalse(any(
            call[:2] == ["network", "create"]
            for call in self._docker_calls()
        ))

    def test_requires_complete_legacy_workload_contract(self):
        cases = (
            self._run(run_id="missing-images", expected_images=None),
            self._run(
                run_id="missing-thumbnails", expected_thumbnails=None,
            ),
            self._run(
                run_id="missing-active-files", expected_active_files=None,
            ),
        )

        for completed in cases:
            self.assertEqual(completed.returncode, 2)
            self.assertEqual(
                completed.stderr.decode("utf-8").strip(),
                "nas_arguments_invalid",
            )
        self.assertFalse(self.rsync_capture.exists())
        self.assertFalse(self.docker_capture.exists())

    def test_status_contract_uses_database_planned_file_count(self):
        media_root = self.source_project / "data/static/media"
        (media_root / "filesystem-only-secret.png").write_bytes(
            b"orphan-media"
        )
        environment = self.environment.copy()
        environment.update({
            "PINRY_METRICS_FILES": "2",
            "PINRY_METRICS_PLANNED_FILES": "1",
        })

        completed = self._run(
            run_id="planned-file-count",
            environment=environment,
            expected_files=2,
        )

        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8"),
        )
        maintenance_checks = [
            call for call in self._docker_calls()
            if "assert-maintenance-http" in call
        ]
        self.assertEqual(len(maintenance_checks), 1)
        files_index = maintenance_checks[0].index("--expected-files")
        self.assertEqual(maintenance_checks[0][files_index + 1], "1")
        wait_checks = [
            call for call in self._docker_calls()
            if "_NAS_WAIT_STATUS" in "\n".join(call)
        ]
        self.assertEqual(len(wait_checks), 2)
        self.assertTrue(all(call[-1] == "1" for call in wait_checks))

    def test_metrics_ignore_top_level_runtime_media_directories(self):
        media_root = self.source_project / "data/static/media"
        for directory_name in (".pinry-locks", ".staging"):
            directory = media_root / directory_name
            directory.mkdir()
            (directory / "runtime-state").write_bytes(b"internal")

        completed = self._run(run_id="ignore-runtime-media")

        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8"),
        )
        metrics_calls = [
            call for call in self._docker_calls()
            if "_NAS_ACCEPTANCE_METRICS" in "\n".join(call)
        ]
        self.assertGreaterEqual(len(metrics_calls), 1)
        helper = metrics_calls[0][metrics_calls[0].index("-c") + 1]
        data_root = self.source_project / "data"
        replacements = {
            'database_uri = "file:/data/production.db?mode=ro"': (
                "database_uri = {!r}".format(
                    "file:{}?mode=ro".format(data_root / "production.db")
                )
            ),
            'media_root = "/data/static/media"': (
                "media_root = {!r}".format(str(media_root))
            ),
            'backup_root = "/data/legacy-backup"': (
                "backup_root = {!r}".format(
                    str(data_root / "legacy-backup")
                )
            ),
        }
        for original, replacement in replacements.items():
            self.assertIn(original, helper)
            helper = helper.replace(original, replacement, 1)

        metrics = subprocess.run(
            [sys.executable, "-c", helper, "before"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        payload = json.loads(metrics.stdout)
        self.assertEqual(payload["files"], 1)

    def test_backup_verifier_rejects_missing_orphan_payload(self):
        completed = self._run(run_id="backup-missing-orphan")
        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8"),
        )
        helper = self._backup_verifier_source()
        data_root = self.temporary_root / "backup-missing-data"
        _run_root, media_root, _database = _create_backup_fixture(data_root)
        expected_files, expected_digest = _payload_fingerprint(media_root)
        expected_database_digest = _logical_database_digest(_database)
        (media_root / "orphan.png").unlink()

        verified = self._run_backup_verifier(
            helper,
            data_root,
            expected_files,
            expected_digest,
            expected_database_digest,
        )

        self.assertNotEqual(verified.returncode, 0)
        self.assertEqual(
            verified.stdout.decode("ascii").strip(),
            "nas_backup_payload_invalid",
        )

    def test_backup_verifier_accepts_exact_preserved_backup(self):
        completed = self._run(run_id="backup-valid")
        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8"),
        )
        helper = self._backup_verifier_source()
        data_root = self.temporary_root / "backup-valid-data"
        _run_root, media_root, _database = _create_backup_fixture(data_root)
        expected_files, expected_digest = _payload_fingerprint(media_root)
        expected_database_digest = _logical_database_digest(_database)

        verified = self._run_backup_verifier(
            helper,
            data_root,
            expected_files,
            expected_digest,
            expected_database_digest,
        )

        self.assertEqual(
            verified.returncode,
            0,
            verified.stderr.decode("utf-8"),
        )
        self.assertEqual(
            verified.stdout.decode("ascii").strip(),
            "NAS_BACKUP_PRESERVATION_OK",
        )

    def test_backup_verifier_rejects_same_size_modified_payload(self):
        completed = self._run(run_id="backup-modified-payload")
        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8"),
        )
        helper = self._backup_verifier_source()
        data_root = self.temporary_root / "backup-modified-data"
        _run_root, media_root, _database = _create_backup_fixture(data_root)
        expected_files, expected_digest = _payload_fingerprint(media_root)
        expected_database_digest = _logical_database_digest(_database)
        (media_root / "linked.png").write_bytes(b"LINKED")

        verified = self._run_backup_verifier(
            helper,
            data_root,
            expected_files,
            expected_digest,
            expected_database_digest,
        )

        self.assertNotEqual(verified.returncode, 0)
        self.assertEqual(
            verified.stdout.decode("ascii").strip(),
            "nas_backup_payload_invalid",
        )

    def test_backup_verifier_rejects_invalid_database_snapshot(self):
        completed = self._run(run_id="backup-invalid-snapshot")
        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8"),
        )
        helper = self._backup_verifier_source()
        data_root = self.temporary_root / "backup-invalid-data"
        _run_root, media_root, database = _create_backup_fixture(data_root)
        expected_files, expected_digest = _payload_fingerprint(media_root)
        expected_database_digest = _logical_database_digest(database)
        database.write_bytes(b"not-a-sqlite-database")

        verified = self._run_backup_verifier(
            helper,
            data_root,
            expected_files,
            expected_digest,
            expected_database_digest,
        )

        self.assertNotEqual(verified.returncode, 0)
        self.assertEqual(
            verified.stdout.decode("ascii").strip(),
            "nas_backup_snapshot_invalid",
        )

    def test_backup_verifier_rejects_same_count_database_mutation(self):
        completed = self._run(run_id="backup-same-count-db-mutation")
        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8"),
        )
        helper = self._backup_verifier_source()
        data_root = self.temporary_root / "backup-mutated-data"
        _run_root, media_root, database = _create_backup_fixture(data_root)
        expected_files, expected_digest = _payload_fingerprint(media_root)
        expected_database_digest = _logical_database_digest(database)
        connection = sqlite3.connect(str(database))
        try:
            connection.execute("UPDATE core_pin SET id = 2 WHERE id = 1")
            connection.commit()
        finally:
            connection.close()

        verified = self._run_backup_verifier(
            helper,
            data_root,
            expected_files,
            expected_digest,
            expected_database_digest,
        )

        self.assertNotEqual(verified.returncode, 0)
        self.assertEqual(
            verified.stdout.decode("ascii").strip(),
            "nas_backup_snapshot_invalid",
        )

    def test_backup_verifier_rejects_sqlite_sequence_mutation(self):
        completed = self._run(run_id="backup-sequence-mutation")
        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8"),
        )
        helper = self._backup_verifier_source()
        data_root = self.temporary_root / "backup-sequence-data"
        _run_root, media_root, database = _create_backup_fixture(data_root)
        connection = sqlite3.connect(str(database))
        try:
            connection.execute(
                "CREATE TABLE sequence_fixture "
                "(id INTEGER PRIMARY KEY AUTOINCREMENT)"
            )
            connection.execute("INSERT INTO sequence_fixture DEFAULT VALUES")
            connection.commit()
        finally:
            connection.close()
        expected_files, expected_digest = _payload_fingerprint(media_root)
        expected_database_digest = _logical_database_digest(database)
        connection = sqlite3.connect(str(database))
        try:
            connection.execute(
                "UPDATE sqlite_sequence SET seq = 999 "
                "WHERE name = 'sequence_fixture'"
            )
            connection.commit()
        finally:
            connection.close()

        verified = self._run_backup_verifier(
            helper,
            data_root,
            expected_files,
            expected_digest,
            expected_database_digest,
        )

        self.assertNotEqual(verified.returncode, 0)
        self.assertEqual(
            verified.stdout.decode("ascii").strip(),
            "nas_backup_snapshot_invalid",
        )

    def test_existing_pin_orm_uses_runtime_docker_settings(self):
        completed = self._run(run_id="orm-runtime-settings")
        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8"),
        )
        calls = [
            call for call in self._docker_calls()
            if "_NAS_EXISTING_PIN_ORM" in "\n".join(call)
        ]
        self.assertEqual(len(calls), 2)
        for call in calls:
            helper = call[call.index("-c") + 1]
            self.assertIn(
                'os.environ["DJANGO_SETTINGS_MODULE"] = "pinry.settings.docker"',
                helper,
            )
            self.assertNotIn("pinry.settings.development", helper)

    def test_reference_size_manifest_is_streamed_without_argv(self):
        environment = self.environment.copy()
        environment["PINRY_LARGE_LEGACY_MANIFEST"] = "1"
        completed = self._run(
            run_id="large-manifest-stdin",
            environment=environment,
        )
        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8"),
        )
        calls = self._docker_calls()
        streamed_markers = (
            "_NAS_CANONICAL_MEDIA_VALIDATE",
            "_NAS_EXISTING_PIN_ORM",
            "_NAS_EXISTING_MEDIA_HTTP",
        )
        for marker in streamed_markers:
            matching = [
                call for call in calls if marker in "\n".join(call)
            ]
            self.assertEqual(len(matching), 2)
            for call in matching:
                self.assertIn("-i", call)
                helper_index = call.index("-c") + 1
                self.assertEqual(call[helper_index + 1:], [])

    def test_canonical_media_verifier_rejects_same_count_corruption(self):
        completed = self._run(run_id="canonical-media-corruption")
        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8"),
        )
        helper = self._canonical_media_verifier_source()
        data_root = self.temporary_root / "canonical-media-data"
        data_root.mkdir()
        legacy_pin_sample = _create_canonical_media_fixture(data_root)

        valid = self._run_canonical_media_verifier(
            helper,
            data_root,
            legacy_pin_sample,
        )

        self.assertEqual(
            valid.returncode,
            0,
            valid.stderr.decode("utf-8"),
        )
        valid_payload = json.loads(valid.stdout.decode("ascii"))
        self.assertEqual(valid_payload["pin_id"], 1)
        self.assertEqual(valid_payload["image_id"], 1)
        self.assertEqual(valid_payload["public_pin_id"], 1)
        self.assertEqual(valid_payload["public_image_id"], 1)

        original = next(
            (data_root / "static/media/originals").glob("*/*.png")
        )
        original.write_bytes(b"corrupted-original")

        verified = self._run_canonical_media_verifier(
            helper,
            data_root,
            legacy_pin_sample,
        )

        self.assertNotEqual(verified.returncode, 0)
        self.assertEqual(
            verified.stdout.decode("ascii").strip(),
            "nas_canonical_media_invalid",
        )

    def test_canonical_media_verifier_rejects_same_count_path_substitution(self):
        completed = self._run(run_id="canonical-media-path-substitution")
        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8"),
        )
        helper = self._canonical_media_verifier_source()
        data_root = self.temporary_root / "canonical-media-substitution"
        data_root.mkdir()
        legacy_pin_sample = _create_canonical_media_fixture(data_root)
        standard = next(
            (data_root / "static/media/derivatives").glob("*/standard.png")
        )
        payload = standard.read_bytes()
        standard.unlink()
        (standard.parent / "orphan.png").write_bytes(payload)

        verified = self._run_canonical_media_verifier(
            helper,
            data_root,
            legacy_pin_sample,
        )

        self.assertNotEqual(verified.returncode, 0)
        self.assertEqual(
            verified.stdout.decode("ascii").strip(),
            "nas_canonical_media_invalid",
        )

    def test_canonical_media_verifier_rejects_derivative_content_mutation(self):
        completed = self._run(run_id="canonical-derivative-content")
        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8"),
        )
        helper = self._canonical_media_verifier_source()
        data_root = self.temporary_root / "canonical-derivative-content"
        data_root.mkdir()
        legacy_pin_sample = _create_canonical_media_fixture(data_root)
        standard = next(
            (data_root / "static/media/derivatives").glob("*/standard.png")
        )
        standard.write_bytes(b"same-row-count-corruption")

        verified = self._run_canonical_media_verifier(
            helper,
            data_root,
            legacy_pin_sample,
        )

        self.assertNotEqual(verified.returncode, 0)
        self.assertEqual(
            verified.stdout.decode("ascii").strip(),
            "nas_canonical_media_invalid",
        )

    def test_canonical_media_verifier_rejects_existing_pin_identity_mutation(
        self,
    ):
        completed = self._run(run_id="canonical-pin-identity")
        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8"),
        )
        helper = self._canonical_media_verifier_source()
        data_root = self.temporary_root / "canonical-pin-identity-data"
        data_root.mkdir()
        legacy_pin_sample = _create_canonical_media_fixture(data_root)
        connection = sqlite3.connect(str(data_root / "production.db"))
        try:
            connection.execute("INSERT INTO core_pin VALUES (2, 1, 1)")
            connection.commit()
        finally:
            connection.close()
        legacy_pin_sample["pins"] = [
            {"image_id": 1, "pin_id": 1, "private": False},
            {"image_id": 1, "pin_id": 2, "private": True},
        ]

        valid = self._run_canonical_media_verifier(
            helper,
            data_root,
            legacy_pin_sample,
        )
        self.assertEqual(valid.returncode, 0, valid.stderr.decode("utf-8"))

        connection = sqlite3.connect(str(data_root / "production.db"))
        try:
            connection.execute("UPDATE core_pin SET id = 3 WHERE id = 2")
            connection.commit()
        finally:
            connection.close()
        verified = self._run_canonical_media_verifier(
            helper,
            data_root,
            legacy_pin_sample,
        )
        self.assertNotEqual(verified.returncode, 0)
        self.assertEqual(
            verified.stdout.decode("ascii").strip(),
            "nas_canonical_media_invalid",
        )

    def test_canonical_media_verifier_rejects_existing_pin_privacy_mutation(self):
        completed = self._run(run_id="canonical-pin-privacy")
        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8"))
        helper = self._canonical_media_verifier_source()
        data_root = self.temporary_root / "canonical-pin-privacy-data"
        data_root.mkdir()
        legacy_pin_sample = _create_canonical_media_fixture(data_root)
        connection = sqlite3.connect(str(data_root / "production.db"))
        try:
            connection.execute("UPDATE core_pin SET private = 1 WHERE id = 1")
            connection.commit()
        finally:
            connection.close()
        verified = self._run_canonical_media_verifier(
            helper, data_root, legacy_pin_sample,
        )
        self.assertNotEqual(verified.returncode, 0)
        self.assertEqual(
            verified.stdout.decode("ascii").strip(),
            "nas_canonical_media_invalid",
        )

    def test_existing_pin_orm_validates_orphan_images_separately(self):
        completed = self._run(run_id="orm-orphan-image")
        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8"))
        fixture = {
            "pins": [{"pin_id": 1, "image_id": 1, "private": False}],
            "images": [
                {"image_id": 1, "image_path": "originals/a/source.png", "image_sha256": "a" * 64},
                {"image_id": 2, "image_path": "originals/b/orphan.png", "image_sha256": "b" * 64},
            ],
        }
        self.assertEqual(len(fixture["pins"]), 1)
        self.assertEqual(len(fixture["images"]), 2)
        helper = self._existing_pin_orm_verifier_source()
        self.assertIn("from django_images.models import Image", helper)
        self.assertIn("Image.objects.filter(pk__in=expected_images)", helper)
        self.assertNotIn("images = {pin.image_id: pin.image for pin in pins}", helper)

    def test_reports_backup_preservation_failure_in_canonical_result(self):
        environment = self.environment.copy()
        environment["PINRY_BACKUP_PRESERVATION_ERROR"] = (
            "nas_backup_payload_invalid"
        )

        completed = self._run(
            run_id="backup-preservation-failure",
            environment=environment,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stderr.decode("ascii").strip(),
            "nas_backup_payload_invalid",
        )
        payload = self._assert_canonical_result(
            self._result_path("backup-preservation-failure")
        )
        self.assertEqual(payload["state"], "failed")
        self.assertEqual(
            payload["error_code"],
            "nas_backup_payload_invalid",
        )

    def test_rejects_clone_with_existing_migration_evidence(self):
        cases = (
            {"PINRY_METRICS_BEFORE_BACKUP_COUNT": "1"},
            {"PINRY_METRICS_BEFORE_SOURCE_COMMIT": "a" * 40},
        )

        for index, overrides in enumerate(cases):
            with self.subTest(overrides=overrides):
                environment = self.environment.copy()
                environment.update(overrides)
                completed = self._run(
                    run_id="already-migrated-{}".format(index),
                    environment=environment,
                )

                self.assertNotEqual(completed.returncode, 0)
                self.assertEqual(
                    completed.stderr.decode("utf-8").strip(),
                    "nas_legacy_already_migrated",
                )
                self.assertFalse(any(
                    call[:2] == ["network", "create"]
                    for call in self._docker_calls()
                ))

    def test_rejects_migration_without_exactly_one_new_backup(self):
        environment = self.environment.copy()
        environment["PINRY_METRICS_AFTER_BACKUP_COUNT"] = "2"

        completed = self._run(
            run_id="extra-backup", environment=environment
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stderr.decode("utf-8").strip(),
            "nas_migration_counts_invalid",
        )

    def test_running_result_publish_does_not_replace_raced_file(self):
        site_directory = self.temporary_root / "race-site"
        site_directory.mkdir()
        _write_result_publish_race_sitecustomize(
            site_directory / "sitecustomize.py"
        )
        environment = self.environment.copy()
        environment["PYTHONPATH"] = str(site_directory)
        environment["PINRY_RESULT_RACE_NAME"] = (
            "svrx-pinry-accept-result-race.json"
        )

        completed = self._run(
            run_id="result-race",
            environment=environment,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stderr.decode("utf-8").strip(),
            "nas_result_publish_failed",
        )
        self.assertEqual(
            self._result_path("result-race").read_bytes(),
            b"race-sentinel\n",
        )
        self.assertFalse(self.rsync_capture.exists())

    def test_running_result_publish_rejects_replacement_after_link(self):
        site_directory = self.temporary_root / "after-link-race-site"
        site_directory.mkdir()
        _write_result_publish_race_sitecustomize(
            site_directory / "sitecustomize.py"
        )
        environment = self.environment.copy()
        environment.update({
            "PYTHONPATH": str(site_directory),
            "PINRY_RESULT_RACE_NAME": (
                "svrx-pinry-accept-after-link-race.json"
            ),
            "PINRY_RESULT_RACE_PHASE": "after-link",
        })

        completed = self._run(
            run_id="after-link-race",
            environment=environment,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stderr.decode("utf-8").strip(),
            "nas_result_publish_failed",
        )
        self.assertEqual(
            self._result_path("after-link-race").read_bytes(),
            b"race-sentinel\n",
        )
        self.assertFalse(self.rsync_capture.exists())

    def test_terminal_result_publish_rejects_identity_check_race(self):
        site_directory = self.temporary_root / "terminal-race-site"
        site_directory.mkdir()
        _write_result_publish_race_sitecustomize(
            site_directory / "sitecustomize.py"
        )
        environment = self.environment.copy()
        marker_path = self.temporary_root / "terminal-race.marker"
        environment.update({
            "PYTHONPATH": str(site_directory),
            "PINRY_RESULT_RACE_NAME": (
                "svrx-pinry-accept-terminal-race.json"
            ),
            "PINRY_RESULT_RACE_PHASE": "after-terminal-identity",
            "PINRY_RESULT_RACE_MARKER": str(marker_path),
        })

        completed = self._run(
            run_id="terminal-race",
            environment=environment,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stderr.decode("utf-8").strip(),
            "nas_result_publish_failed",
        )
        self.assertEqual(
            self._result_path("terminal-race").read_bytes(),
            b"race-sentinel\n",
        )
        expected_device, expected_inode = (
            int(value) for value in marker_path.read_text().split(":")
        )
        final_stat = self._result_path("terminal-race").stat()
        self.assertEqual(
            (final_stat.st_dev, final_stat.st_ino),
            (expected_device, expected_inode),
        )

    def test_terminal_result_publish_preserves_sentinel_if_rollback_fails(self):
        site_directory = self.temporary_root / "rollback-failure-site"
        site_directory.mkdir()
        _write_result_publish_race_sitecustomize(
            site_directory / "sitecustomize.py"
        )
        environment = self.environment.copy()
        marker_path = self.temporary_root / "rollback-failure.marker"
        environment.update({
            "PYTHONPATH": str(site_directory),
            "PINRY_RESULT_RACE_NAME": (
                "svrx-pinry-accept-rollback-failure.json"
            ),
            "PINRY_RESULT_RACE_PHASE": (
                "after-terminal-identity-rollback-fails"
            ),
            "PINRY_RESULT_RACE_MARKER": str(marker_path),
        })

        completed = self._run(
            run_id="rollback-failure",
            environment=environment,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stderr.decode("utf-8").strip(),
            "nas_result_publish_failed",
        )
        expected_identity = tuple(
            int(value) for value in marker_path.read_text().split(":")
        )
        preserved = [
            candidate
            for candidate in self.result_root.iterdir()
            if (
                candidate.stat().st_dev,
                candidate.stat().st_ino,
            ) == expected_identity
        ]
        self.assertEqual(len(preserved), 1)
        self.assertEqual(preserved[0].read_bytes(), b"race-sentinel\n")
        canonical = self._assert_canonical_result(
            self._result_path("rollback-failure")
        )
        self.assertEqual(canonical["state"], "succeeded")
        self.assertIsNotNone(canonical["completed_at"])
        self.assertIsNotNone(canonical["duration_seconds"])
        self.assertIsNone(canonical["error_code"])

    def test_terminal_result_publish_rejects_replacement_after_write(self):
        site_directory = self.temporary_root / "terminal-write-race-site"
        site_directory.mkdir()
        _write_result_publish_race_sitecustomize(
            site_directory / "sitecustomize.py"
        )
        environment = self.environment.copy()
        environment.update({
            "PYTHONPATH": str(site_directory),
            "PINRY_RESULT_RACE_NAME": (
                "svrx-pinry-accept-terminal-write-race.json"
            ),
            "PINRY_RESULT_RACE_PHASE": "after-terminal-write",
        })

        completed = self._run(
            run_id="terminal-write-race",
            environment=environment,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stderr.decode("utf-8").strip(),
            "nas_result_publish_failed",
        )
        self.assertEqual(
            self._result_path("terminal-write-race").read_bytes(),
            b"race-sentinel\n",
        )

    def test_terminal_write_failure_preserves_exact_running_result(self):
        site_directory = self.temporary_root / "terminal-write-failure-site"
        site_directory.mkdir()
        _write_result_publish_race_sitecustomize(
            site_directory / "sitecustomize.py"
        )
        snapshot_path = self.temporary_root / "running-result.snapshot"
        environment = self.environment.copy()
        environment.update({
            "PYTHONPATH": str(site_directory),
            "PINRY_RESULT_RACE_NAME": (
                "svrx-pinry-accept-terminal-write-failure.json"
            ),
            "PINRY_RESULT_RACE_PHASE": "terminal-final-write-fails",
            "PINRY_RESULT_RACE_MARKER": str(snapshot_path),
        })

        completed = self._run(
            run_id="terminal-write-failure",
            environment=environment,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stderr.decode("utf-8").strip(),
            "nas_result_publish_failed",
        )
        result_path = self._result_path("terminal-write-failure")
        self.assertEqual(result_path.read_bytes(), snapshot_path.read_bytes())
        canonical = self._assert_canonical_result(result_path)
        self.assertEqual(canonical["state"], "running")
        self.assertIsNone(canonical["completed_at"])
        self.assertIsNone(canonical["duration_seconds"])
        self.assertIsNone(canonical["error_code"])

    def test_terminal_result_publish_rolls_back_if_directory_fsync_fails(self):
        site_directory = self.temporary_root / "terminal-dir-fsync-site"
        site_directory.mkdir()
        _write_result_publish_race_sitecustomize(
            site_directory / "sitecustomize.py"
        )
        environment = self.environment.copy()
        environment.update({
            "PYTHONPATH": str(site_directory),
            "PINRY_RESULT_RACE_NAME": (
                "svrx-pinry-accept-terminal-dir-fsync.json"
            ),
            "PINRY_RESULT_RACE_PHASE": "terminal-directory-fsync-fails",
        })

        completed = self._run(
            run_id="terminal-dir-fsync",
            environment=environment,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stderr.decode("utf-8").strip(),
            "nas_result_publish_failed",
        )
        canonical = self._assert_canonical_result(
            self._result_path("terminal-dir-fsync")
        )
        self.assertEqual(canonical["state"], "running")
        self.assertIsNone(canonical["completed_at"])
        self.assertIsNone(canonical["duration_seconds"])
        self.assertIsNone(canonical["error_code"])

    def test_terminal_result_publish_ignores_old_result_cleanup_failure(self):
        site_directory = self.temporary_root / "terminal-cleanup-site"
        site_directory.mkdir()
        _write_result_publish_race_sitecustomize(
            site_directory / "sitecustomize.py"
        )
        environment = self.environment.copy()
        environment.update({
            "PYTHONPATH": str(site_directory),
            "PINRY_RESULT_RACE_NAME": (
                "svrx-pinry-accept-terminal-cleanup.json"
            ),
            "PINRY_RESULT_RACE_PHASE": "terminal-cleanup-unlink-fails",
        })

        completed = self._run(
            run_id="terminal-cleanup",
            environment=environment,
        )

        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8"),
        )
        canonical = self._assert_canonical_result(
            self._result_path("terminal-cleanup")
        )
        self.assertEqual(canonical["state"], "succeeded")
        self.assertIsNone(canonical["error_code"])

    def test_terminal_result_publish_never_truncates_public_result(self):
        site_directory = self.temporary_root / "before-truncate-race-site"
        site_directory.mkdir()
        _write_result_publish_race_sitecustomize(
            site_directory / "sitecustomize.py"
        )
        marker_path = self.temporary_root / "before-truncate-race.marker"
        reopen_marker = self.temporary_root / "before-truncate-reopen.marker"
        environment = self.environment.copy()
        environment.update({
            "PYTHONPATH": str(site_directory),
            "PINRY_RESULT_RACE_NAME": (
                "svrx-pinry-accept-before-truncate-race.json"
            ),
            "PINRY_RESULT_RACE_PHASE": "before-terminal-truncate",
            "PINRY_RESULT_RACE_MARKER": str(marker_path),
            "PINRY_RESULT_REOPEN_MARKER": str(reopen_marker),
        })

        completed = self._run(
            run_id="before-truncate-race",
            environment=environment,
        )

        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8"),
        )
        self.assertEqual(
            completed.stdout.decode("utf-8").strip(),
            "NAS_LEGACY_CLONE_ACCEPTANCE_OK",
        )
        result_path = self._result_path("before-truncate-race")
        canonical = self._assert_canonical_result(result_path)
        self.assertEqual(canonical["state"], "succeeded")
        self.assertFalse(marker_path.exists())
        self.assertFalse(reopen_marker.exists())

    def test_terminal_result_publish_uses_syscall_without_renameat2(self):
        site_directory = self.temporary_root / "renameat2-fallback-site"
        site_directory.mkdir()
        _write_renameat2_fallback_sitecustomize(
            site_directory / "sitecustomize.py"
        )
        marker_path = self.temporary_root / "renameat2-fallback.marker"
        environment = self.environment.copy()
        environment.update({
            "PYTHONPATH": str(site_directory),
            "PINRY_RENAMEAT2_FALLBACK_MARKER": str(marker_path),
        })

        completed = self._run(
            run_id="renameat2-fallback",
            environment=environment,
        )

        self.assertEqual(completed.returncode, 0)
        payload = self._assert_canonical_result(
            self._result_path("renameat2-fallback")
        )
        self.assertEqual(payload["state"], "succeeded")
        self.assertTrue(marker_path.is_file())
        self.assertIn(b"used\n", marker_path.read_bytes())

    def test_rejects_existing_clone_or_result(self):
        clone = self._clone_path("clone-exists")
        clone.mkdir()
        clone_completed = self._run(run_id="clone-exists")
        result = self._result_path("result-exists")
        result.write_bytes(b"do-not-replace\n")
        result_completed = self._run(run_id="result-exists")

        self.assertEqual(
            clone_completed.stderr.decode("utf-8").strip(),
            "nas_clone_exists",
        )
        self.assertEqual(
            result_completed.stderr.decode("utf-8").strip(),
            "nas_result_exists",
        )
        self.assertEqual(result.read_bytes(), b"do-not-replace\n")
        self.assertFalse(self.rsync_capture.exists())

    def test_rejects_symlink_source_run_or_result_root(self):
        source_link = self.temporary_root / "source-link"
        run_link = self.temporary_root / "run-link"
        result_link = self.temporary_root / "result-link"
        source_link.symlink_to(self.source_project, target_is_directory=True)
        run_link.symlink_to(self.run_root, target_is_directory=True)
        result_link.symlink_to(self.result_root, target_is_directory=True)

        cases = (
            self._run(run_id="source-link", source_project=source_link),
            self._run(run_id="run-link", run_root=run_link),
            self._run(run_id="result-link", result_root=result_link),
            self._run(
                run_id="source-link-slash",
                source_project="{}/".format(source_link),
            ),
            self._run(
                run_id="run-link-slash",
                run_root="{}/".format(run_link),
            ),
            self._run(
                run_id="result-link-slash",
                result_root="{}/".format(result_link),
            ),
        )
        self.assertEqual(
            [case.stderr.decode("utf-8").strip() for case in cases],
            [
                "nas_source_project_invalid",
                "nas_run_root_invalid",
                "nas_result_root_invalid",
                "nas_source_project_invalid",
                "nas_run_root_invalid",
                "nas_result_root_invalid",
            ],
        )
        self.assertTrue(all(case.returncode != 0 for case in cases))
        self.assertFalse(self.rsync_capture.exists())

    def test_rejects_running_container_bound_to_source_data(self):
        environment = self.environment.copy()
        environment["PINRY_RUNNING_SOURCE_MOUNT"] = "1"
        completed = self._run(
            run_id="running-source",
            environment=environment,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stderr.decode("utf-8").strip(),
            "nas_source_container_running",
        )
        self.assertFalse(self.rsync_capture.exists())
        result_path = self._result_path("running-source")
        self.assertTrue(result_path.is_file())
        payload = self._assert_canonical_result(result_path)
        self.assertEqual(payload["state"], "failed")
        self.assertEqual(
            payload["error_code"],
            "nas_source_container_running",
        )
        self._assert_public_output(completed, result_path)

    def test_rejects_running_container_bound_to_generic_source_parent_rw(self):
        environment = self.environment.copy()
        environment["PINRY_RUNNING_SOURCE_PARENT_MOUNT"] = "1"
        completed = self._run(
            run_id="running-source-parent",
            environment=environment,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stderr.decode("utf-8").strip(),
            "nas_source_container_running",
        )
        self.assertFalse(self.rsync_capture.exists())

    def test_allows_running_container_bound_to_generic_source_parent_readonly(
        self,
    ):
        environment = self.environment.copy()
        environment["PINRY_RUNNING_SOURCE_PARENT_READONLY_MOUNT"] = "1"
        completed = self._run(
            run_id="running-source-parent-readonly",
            environment=environment,
        )

        self.assertEqual(completed.returncode, 0)
        self.assertTrue(self.rsync_capture.is_file())
        payload = self._assert_canonical_result(
            self._result_path("running-source-parent-readonly")
        )
        self.assertEqual(payload["state"], "succeeded")

    def test_allows_readonly_bind_mounts_overlapping_source(self):
        for kind in ("data", "project", "root", "subtree"):
            with self.subTest(kind=kind):
                environment = self.environment.copy()
                environment[
                    "PINRY_RUNNING_SOURCE_READONLY_{}_MOUNT".format(kind.upper())
                ] = "1"
                completed = self._run(
                    run_id="readonly-source-{}".format(kind),
                    environment=environment,
                )
                self.assertEqual(
                    completed.returncode, 0, completed.stderr.decode("utf-8"),
                )

    def test_rejects_running_container_bound_below_source_data(self):
        environment = self.environment.copy()
        environment["PINRY_RUNNING_SOURCE_SUBTREE_MOUNT"] = "1"
        completed = self._run(
            run_id="running-source-subtree",
            environment=environment,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stderr.decode("utf-8").strip(),
            "nas_source_container_running",
        )
        self.assertFalse(self.rsync_capture.exists())

    def test_rejects_running_container_bound_above_generic_source_parent(self):
        environment = self.environment.copy()
        environment["PINRY_RUNNING_SOURCE_ANCESTOR_MOUNT"] = "1"
        completed = self._run(
            run_id="running-source-ancestor",
            environment=environment,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stderr.decode("utf-8").strip(),
            "nas_source_container_running",
        )
        self.assertFalse(self.rsync_capture.exists())

    def test_rejects_running_container_with_bind_backed_named_volume(self):
        environment = self.environment.copy()
        environment["PINRY_RUNNING_SOURCE_VOLUME"] = "1"
        completed = self._run(
            run_id="running-source-volume",
            environment=environment,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stderr.decode("utf-8").strip(),
            "nas_source_container_running",
        )
        self.assertFalse(self.rsync_capture.exists())
        volume_inspects = [
            call for call in self._docker_calls()
            if call[:2] == ["volume", "inspect"]
        ]
        self.assertEqual(len(volume_inspects), 1)
        self.assertIn("{{json .Options}}", volume_inspects[0])

    def test_rejects_running_container_with_named_volume_source_overlap(self):
        environment = self.environment.copy()
        environment["PINRY_RUNNING_SOURCE_ORDINARY_VOLUME"] = "1"
        completed = self._run(
            run_id="running-source-ordinary-volume",
            environment=environment,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stderr.decode("utf-8").strip(),
            "nas_source_container_running",
        )
        self.assertFalse(self.rsync_capture.exists())
        self.assertFalse([
            call for call in self._docker_calls()
            if call[:2] == ["volume", "inspect"]
        ])

    def test_rejects_named_volume_that_exposes_host_root(self):
        cases = (
            "PINRY_RUNNING_SOURCE_ROOT_VOLUME",
            "PINRY_RUNNING_SOURCE_ROOT_MOUNT_VOLUME",
        )
        for index, variable in enumerate(cases):
            with self.subTest(variable=variable):
                environment = self.environment.copy()
                environment[variable] = "1"
                completed = self._run(
                    run_id="running-root-volume-{}".format(index),
                    environment=environment,
                )
                self.assertNotEqual(completed.returncode, 0)
                self.assertEqual(
                    completed.stderr.decode("utf-8").strip(),
                    "nas_source_container_running",
                )
                self.assertFalse(self.rsync_capture.exists())

    def test_allows_container_stopped_after_ps_snapshot(self):
        environment = self.environment.copy()
        environment["PINRY_STOPPED_SOURCE_MOUNT"] = "1"
        completed = self._run(
            run_id="stopped-source",
            environment=environment,
        )

        self.assertEqual(completed.returncode, 0)
        self.assertTrue(self.rsync_capture.is_file())
        payload = self._assert_canonical_result(
            self._result_path("stopped-source")
        )
        self.assertEqual(payload["state"], "succeeded")
        self._assert_public_output(
            completed,
            self._result_path("stopped-source"),
        )

    def test_requires_legacy_project_data_layout(self):
        missing_database = self.temporary_root / "missing-database"
        (missing_database / "data/static/media").mkdir(parents=True)
        missing_media = self.temporary_root / "missing-media"
        (missing_media / "data").mkdir(parents=True)
        (missing_media / "data/production.db").write_bytes(b"not-sqlite")

        cases = (
            self._run(
                run_id="missing-database",
                source_project=missing_database,
            ),
            self._run(
                run_id="missing-media",
                source_project=missing_media,
            ),
        )
        for completed in cases:
            self.assertNotEqual(completed.returncode, 0)
            self.assertEqual(
                completed.stderr.decode("utf-8").strip(),
                "nas_source_layout_invalid",
            )
        self.assertFalse(self.rsync_capture.exists())

    def test_aborts_before_app_start_if_source_fingerprint_changes(self):
        environment = self.environment.copy()
        environment["PINRY_RSYNC_MUTATE_SOURCE"] = str(
            self.source_project
            / "data/static/media/private-photo-secret.png"
        )
        completed = self._run(
            run_id="source-change",
            environment=environment,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stderr.decode("utf-8").strip(),
            "nas_source_changed_during_copy",
        )
        self.assertFalse(any(
            call and call[0] == "run" for call in self._docker_calls()
        ))
        result = self._assert_canonical_result(
            self._result_path("source-change")
        )
        self.assertEqual(result["state"], "failed")
        self.assertEqual(
            result["error_code"],
            "nas_source_changed_during_copy",
        )
        self.assertFalse(result["source_fingerprint_unchanged"])
        self._assert_public_output(
            completed,
            self._result_path("source-change"),
        )

    def test_source_fingerprint_detects_non_media_same_metadata_content_change(
        self,
    ):
        target = self.source_project / "data/non-media.bin"
        target.write_bytes(b"unchanged-length")
        environment = self.environment.copy()
        environment["PINRY_RSYNC_MUTATE_SOURCE_SAME_METADATA"] = str(target)
        completed = self._run(
            run_id="source-content-same-metadata",
            environment=environment,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stderr.decode("utf-8").strip(),
            "nas_source_changed_during_copy",
        )

    def test_source_fingerprint_detects_directory_metadata_change(self):
        target = self.source_project / "data/static"
        target.chmod(0o755)
        environment = self.environment.copy()
        environment["PINRY_RSYNC_MUTATE_SOURCE_DIRECTORY"] = str(target)
        completed = self._run(
            run_id="source-directory-metadata",
            environment=environment,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stderr.decode("utf-8").strip(),
            "nas_source_changed_during_copy",
        )

    def test_source_fingerprint_detects_symlink_replacement(self):
        target = self.source_project / "data/source-link"
        target.symlink_to("production.db")
        environment = self.environment.copy()
        environment["PINRY_RSYNC_REPLACE_SOURCE_SYMLINK"] = str(target)
        completed = self._run(
            run_id="source-symlink-replacement",
            environment=environment,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stderr.decode("utf-8").strip(),
            "nas_source_changed_during_copy",
        )

    def test_rsync_is_archive_only_without_delete(self):
        source_before = _tree_snapshot(self.source_project)
        self.source_project.chmod(0o755)
        completed = self._run(run_id="archive-copy")

        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8"),
        )
        self.assertEqual(
            _nul_arguments(self.rsync_capture),
            [
                "-a",
                "{}/".format((self.source_project / "data").resolve()),
                "{}/".format(
                    (self._clone_path("archive-copy") / "data").resolve()
                ),
            ],
        )
        self.assertFalse(
            (
                self._clone_path("archive-copy")
                / "legacy-project-secret.txt"
            ).exists()
        )
        self.assertEqual(
            stat.S_IMODE(self._clone_path("archive-copy").stat().st_mode),
            0o700,
        )
        payload = self._assert_canonical_result(
            self._result_path("archive-copy")
        )
        self.assertEqual(payload["state"], "succeeded")
        self.assertIsNone(payload["error_code"])
        self.assertEqual(payload["legacy_counts"], {
            "active_files": 1,
            "files": 1,
            "images": 1,
            "pins": 1,
            "thumbnails": 0,
        })
        self.assertEqual(payload["final_counts"], {
            "active_files": 1,
            "files": 1,
            "images": 1,
            "pins": 1,
            "thumbnails": 0,
        })
        self.assertEqual(payload["batches"], 7)
        self.assertTrue(payload["noop_restart"])
        self.assertTrue(payload["legacy_backup_present"])
        self.assertEqual(payload["temporary_pin_check"], "passed")
        self.assertEqual(
            completed.stdout.decode("ascii").strip(),
            "NAS_LEGACY_CLONE_ACCEPTANCE_OK",
        )
        self.assertEqual(_tree_snapshot(self.source_project), source_before)
        self._assert_public_output(
            completed,
            self._result_path("archive-copy"),
        )

    def test_clone_verification_rejects_same_size_media_mutation(self):
        environment = self.environment.copy()
        environment["PINRY_RSYNC_MUTATE_CLONE"] = (
            "static/media/private-photo-secret.png"
        )

        completed = self._run(
            run_id="clone-media-mutation",
            environment=environment,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stderr.decode("utf-8").strip(),
            "nas_clone_verification_failed",
        )
        payload = self._assert_canonical_result(
            self._result_path("clone-media-mutation")
        )
        self.assertEqual(payload["state"], "failed")
        self.assertEqual(
            payload["error_code"],
            "nas_clone_verification_failed",
        )

    def test_clone_permission_failure_does_not_leak_path(self):
        _write_fake_chmod(self.fake_bin / "chmod")

        completed = self._run(run_id="clone-permission-failure")

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stderr.decode("utf-8").strip(),
            "nas_clone_permissions_failed",
        )
        self._assert_public_output(
            completed,
            self._result_path("clone-permission-failure"),
        )

    def test_rejects_stale_image_source_commit_before_clone(self):
        _write_fingerprint_guard(self.fake_bin / "python3")
        marker = self.temporary_root / "fingerprint-called"
        environment = self.environment.copy()
        environment.update({
            "PINRY_IMAGE_SOURCE_COMMIT": "b" * 40,
            "PINRY_FAIL_SOURCE_FINGERPRINT": "1",
            "PINRY_FINGERPRINT_MARKER": str(marker),
        })

        completed = self._run(
            run_id="stale-image", environment=environment
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stderr.decode("utf-8").strip(),
            "nas_image_source_commit_mismatch",
        )
        self.assertFalse(self.rsync_capture.exists())
        self.assertFalse(marker.exists())
        self.assertFalse(self._clone_path("stale-image").exists())
        result_path = self._result_path("stale-image")
        payload = self._assert_canonical_result(result_path)
        self.assertEqual(payload["source_commit"], "b" * 40)
        self._assert_public_output(
            completed, result_path
        )

    def test_rejects_missing_image_source_commit_before_clone(self):
        environment = self.environment.copy()
        environment["PINRY_IMAGE_SOURCE_COMMIT"] = ""

        completed = self._run(
            run_id="missing-image-revision", environment=environment
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stderr.decode("utf-8").strip(),
            "nas_image_source_commit_invalid",
        )
        self.assertFalse(self.rsync_capture.exists())
        self.assertFalse(
            self._clone_path("missing-image-revision").exists()
        )
        self._assert_public_output(
            completed, self._result_path("missing-image-revision")
        )

    def test_copy_failure_preserves_private_clone_root(self):
        self.source_project.chmod(0o755)
        environment = self.environment.copy()
        environment["PINRY_RSYNC_FAIL"] = "1"
        completed = self._run(
            run_id="copy-failure",
            environment=environment,
        )
        clone = self._clone_path("copy-failure")

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stderr.decode("utf-8").strip(),
            "nas_copy_failed",
        )
        self.assertTrue((clone / "data/partial-copy").is_file())
        self.assertEqual(stat.S_IMODE(clone.stat().st_mode), 0o700)
        result = self._assert_canonical_result(
            self._result_path("copy-failure")
        )
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["error_code"], "nas_copy_failed")
        self._assert_public_output(
            completed,
            self._result_path("copy-failure"),
        )

    def test_initial_observation_does_not_require_terminal_completion(self):
        environment = self.environment.copy()
        environment["PINRY_REJECT_TERMINAL_INITIAL"] = "1"

        completed = self._run(
            run_id="long-migration", environment=environment
        )

        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8"),
        )
        maintenance_calls = [
            call for call in self._docker_calls()
            if "assert-maintenance-http" in call
        ]
        self.assertEqual(len(maintenance_calls), 1)
        self.assertNotIn("--require-terminal", maintenance_calls[0])
        timeout_index = maintenance_calls[0].index("--timeout")
        self.assertLessEqual(
            int(maintenance_calls[0][timeout_index + 1]), 300
        )
        wait_calls = [
            call for call in self._docker_calls()
            if "_NAS_WAIT_STATUS" in "\n".join(call)
        ]
        self.assertEqual(len(wait_calls), 2)
        helper_index = wait_calls[0].index("-c")
        first_deadline = int(wait_calls[0][helper_index + 2])
        remaining = first_deadline - int(time.time())
        self.assertGreaterEqual(remaining, 2300)
        self.assertLessEqual(remaining, 2400)

    def test_ready_wait_allows_starting_service_transition(self):
        completed = self._run(run_id="starting-service-wait")
        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8"),
        )
        wait_calls = [
            call for call in self._docker_calls()
            if "_NAS_WAIT_STATUS" in "\n".join(call)
        ]
        self.assertEqual(len(wait_calls), 2)
        helpers = {
            call[call.index("-c") + 1]
            for call in wait_calls
        }
        self.assertEqual(len(helpers), 1)
        helper = helpers.pop().replace(
            "/tmp/create_legacy_fixture.py", str(FIXTURE_SCRIPT)
        )
        responses = [
            io.BytesIO(json.dumps(_maintenance_status(
                "starting_service",
                heartbeat_at="2026-08-28T00:00:00Z",
            )).encode("ascii")),
            io.BytesIO(json.dumps(
                _maintenance_status("ready")
            ).encode("ascii")),
        ]
        output = io.StringIO()

        with mock.patch(
            "urllib.request.urlopen", side_effect=responses
        ), mock.patch("time.sleep", return_value=None), mock.patch.object(
            sys,
            "argv",
            ["-c", str(int(time.time()) + 5), "1", "1"],
        ), contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as raised:
                exec(compile(helper, "<nas-wait-status>", "exec"), {})

        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(json.loads(output.getvalue()), {
            "batches": 7,
            "max_heartbeat_gap_seconds": 1.0,
        })

    def test_ready_wait_accepts_utf8_public_status(self):
        completed = self._run(run_id="ready-utf8-status")
        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8"),
        )
        wait_call = next(
            call for call in self._docker_calls()
            if "_NAS_WAIT_STATUS" in "\n".join(call)
        )
        helper = wait_call[wait_call.index("-c") + 1].replace(
            "/tmp/create_legacy_fixture.py", str(FIXTURE_SCRIPT)
        )
        response = io.BytesIO(json.dumps(
            _maintenance_status("ready"),
            ensure_ascii=False,
        ).encode("utf-8"))
        output = io.StringIO()

        with mock.patch(
            "urllib.request.urlopen", return_value=response
        ), mock.patch("time.sleep", return_value=None), mock.patch.object(
            sys,
            "argv",
            ["-c", str(int(time.time()) + 5), "1", "1"],
        ), contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as raised:
                exec(compile(helper, "<nas-wait-status>", "exec"), {})

        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(json.loads(output.getvalue()), {
            "batches": 7,
            "max_heartbeat_gap_seconds": 0.0,
        })

    def test_ready_wait_rejects_missing_batch_or_heartbeat(self):
        completed = self._run(run_id="ready-required-fields")
        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8"),
        )
        wait_calls = [
            call for call in self._docker_calls()
            if "_NAS_WAIT_STATUS" in "\n".join(call)
        ]
        helper = wait_calls[0][wait_calls[0].index("-c") + 1].replace(
            "/tmp/create_legacy_fixture.py", str(FIXTURE_SCRIPT)
        )
        base_status = _maintenance_status("ready")

        for missing in ("heartbeat_at", "last_committed_batch"):
            with self.subTest(missing=missing):
                status = dict(base_status)
                del status[missing]
                output = io.StringIO()
                response = io.BytesIO(json.dumps(status).encode("ascii"))
                with mock.patch(
                    "urllib.request.urlopen", return_value=response
                ), mock.patch("time.sleep", return_value=None):
                    with mock.patch.object(
                        sys,
                        "argv",
                        ["-c", str(int(time.time()) + 5), "1", "1"],
                    ), contextlib.redirect_stdout(output):
                        with self.assertRaises(SystemExit) as raised:
                            exec(
                                compile(
                                    helper, "<nas-wait-status>", "exec"
                                ),
                                {},
                            )

                self.assertEqual(raised.exception.code, 1)
                self.assertEqual(
                    output.getvalue().strip(),
                    "WAIT_ERROR:status_invalid",
                )

    def test_ready_wait_rejects_incomplete_public_schema(self):
        completed = self._run(run_id="ready-full-schema")
        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8"),
        )
        wait_calls = [
            call for call in self._docker_calls()
            if "_NAS_WAIT_STATUS" in "\n".join(call)
        ]
        helper = wait_calls[0][wait_calls[0].index("-c") + 1].replace(
            "/tmp/create_legacy_fixture.py", str(FIXTURE_SCRIPT)
        )
        response = io.BytesIO(json.dumps({
            "state": "ready",
            "heartbeat_at": "2026-08-28T00:00:01Z",
            "last_committed_batch": 7,
        }).encode("ascii"))

        output = io.StringIO()
        with mock.patch(
            "urllib.request.urlopen", return_value=response
        ), mock.patch("time.sleep", return_value=None), mock.patch.object(
            sys,
            "argv",
            ["-c", str(int(time.time()) + 5), "1", "1"],
        ), contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as raised:
                exec(compile(helper, "<nas-wait-status>", "exec"), {})

        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(
            output.getvalue().strip(), "WAIT_ERROR:status_invalid"
        )

    def test_ready_wait_preserves_failed_terminal_error_code(self):
        completed = self._run(run_id="ready-failed-terminal")
        self.assertEqual(completed.returncode, 0)
        wait_call = next(
            call for call in self._docker_calls()
            if "_NAS_WAIT_STATUS" in "\n".join(call)
        )
        helper = wait_call[wait_call.index("-c") + 1].replace(
            "/tmp/create_legacy_fixture.py", str(FIXTURE_SCRIPT)
        )
        response = io.BytesIO(json.dumps(
            _maintenance_status(
                "failed", error_code="legacy_startup_failed"
            )
        ).encode("ascii"))
        output = io.StringIO()

        with mock.patch(
            "urllib.request.urlopen", return_value=response
        ), mock.patch("time.sleep", return_value=None), mock.patch.object(
            sys,
            "argv",
            ["-c", str(int(time.time()) + 5), "1", "1"],
        ), contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as raised:
                exec(compile(helper, "<nas-wait-status>", "exec"), {})

        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(
            output.getvalue().strip(),
            "WAIT_ERROR:migration_failed:legacy_startup_failed",
        )

    def test_maintenance_failure_preserves_safe_diagnostic_code(self):
        cases = {
            "maintenance_page_unavailable": (
                "nas_maintenance_page_unavailable"
            ),
            "maintenance_blocking_unavailable": (
                "nas_maintenance_blocking_unavailable"
            ),
            "maintenance_status_unavailable": (
                "nas_maintenance_status_unavailable"
            ),
            "maintenance_state_not_observed": (
                "nas_maintenance_state_not_observed"
            ),
            "maintenance_status_invalid": (
                "nas_maintenance_status_invalid"
            ),
            "maintenance_progress_regressed": (
                "nas_maintenance_progress_regressed"
            ),
        }

        for fixture_error, expected_error in cases.items():
            with self.subTest(fixture_error=fixture_error):
                environment = self.environment.copy()
                environment["PINRY_MAINTENANCE_FIXTURE_ERROR"] = (
                    fixture_error
                )
                run_id = "maintenance-{}".format(fixture_error)
                completed = self._run(
                    run_id=run_id,
                    environment=environment,
                )

                self.assertNotEqual(completed.returncode, 0)
                self.assertEqual(
                    completed.stderr.decode("utf-8").strip(),
                    expected_error,
                )
                result = self._assert_canonical_result(
                    self._result_path(run_id)
                )
                self.assertEqual(result["error_code"], expected_error)
                self._assert_public_output(
                    completed,
                    self._result_path(run_id),
                )

    def test_maintenance_terminal_failure_preserves_public_error_code(self):
        environment = self.environment.copy()
        environment["PINRY_MAINTENANCE_TERMINAL_ERROR_CODE"] = (
            "legacy_startup_failed"
        )

        completed = self._run(
            run_id="maintenance-terminal-failed",
            environment=environment,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stderr.decode("utf-8").strip(),
            "nas_migration_legacy_startup_failed",
        )

    def test_ready_wait_failure_causes_are_mapped_separately(self):
        cases = {
            "status_unavailable": "nas_app_status_unavailable",
            "status_invalid": "nas_app_status_invalid",
            "progress_regressed": "nas_maintenance_progress_regressed",
            "migration_failed:legacy_startup_failed": (
                "nas_migration_legacy_startup_failed"
            ),
            "deadline_exceeded": "nas_app_not_ready",
        }

        for index, (wait_error, expected_error) in enumerate(cases.items()):
            with self.subTest(wait_error=wait_error):
                environment = self.environment.copy()
                environment["PINRY_WAIT_ERROR"] = wait_error
                completed = self._run(
                    run_id="wait-error-{}".format(index),
                    environment=environment,
                )

                self.assertNotEqual(completed.returncode, 0)
                self.assertEqual(
                    completed.stderr.decode("utf-8").strip(),
                    expected_error,
                )

    def test_write_probe_is_followed_by_full_existing_data_revalidation(self):
        completed = self._run(run_id="post-probe-revalidation")
        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8"),
        )
        calls = self._docker_calls()
        api_indices = [
            index for index, call in enumerate(calls)
            if "api-check" in call and "new" in call
        ]
        self.assertEqual(len(api_indices), 1)
        api_index = api_indices[0]
        for marker in (
            "_NAS_CANONICAL_MEDIA_VALIDATE",
            "_NAS_EXISTING_PIN_ORM",
            "_NAS_EXISTING_MEDIA_HTTP",
        ):
            indices = [
                index for index, call in enumerate(calls)
                if marker in "\n".join(call)
            ]
            self.assertEqual(len(indices), 2)
            self.assertLess(indices[0], api_index)
            self.assertGreater(indices[1], api_index)

    def test_mounts_only_clone_data_read_write(self):
        completed = self._run(run_id="mount-contract")
        clone_data = str(
            self._clone_path("mount-contract").resolve() / "data"
        )
        source_paths = {
            str(self.source_project.resolve()),
            str((self.source_project / "data").resolve()),
        }

        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8"),
        )
        mounts = []
        published = []
        app_names = []
        for call in self._docker_calls():
            if not call or call[0] != "run":
                continue
            self.assertNotIn("-p", call)
            self.assertNotIn("--publish", call)
            for index, argument in enumerate(call):
                if argument == "--mount":
                    mounts.append(call[index + 1])
                elif argument.startswith("--mount="):
                    mounts.append(argument.split("=", 1)[1])
                elif argument in ("-p", "--publish"):
                    published.append(call[index + 1])
            if "--network-alias" in call:
                alias_index = call.index("--network-alias")
                if call[alias_index + 1] == "app":
                    name_index = call.index("--name")
                    app_names.append(call[name_index + 1])

        self.assertFalse(published)
        data_mounts = [mount for mount in mounts if "dst=/data" in mount]
        read_write = [
            mount for mount in data_mounts if "readonly" not in mount
        ]
        self.assertEqual(
            set(read_write),
            {"type=bind,src={},dst=/data".format(clone_data)},
        )
        self.assertTrue(all(
            "src={}".format(source) not in mount
            for source in source_paths
            for mount in mounts
        ))
        self.assertEqual(len(app_names), 2)
        self.assertTrue(all(
            name.startswith("svrx-pinry-accept-mount-contract-")
            for name in app_names
        ))
        network_creates = [
            call for call in self._docker_calls()
            if call[:2] == ["network", "create"]
        ]
        self.assertEqual(len(network_creates), 1)
        self.assertIn("--internal", network_creates[0])
        label_index = network_creates[0].index("--label")
        ownership_label = network_creates[0][label_index + 1]
        self.assertTrue(ownership_label.startswith(
            "dev.svrx.pinry.nas_acceptance=mount-contract-"
        ))
        network_inspects = [
            call for call in self._docker_calls()
            if call[:2] == ["network", "inspect"]
        ]
        self.assertEqual(len(network_inspects), 1)
        self.assertIn("{{.Internal}}", network_inspects[0])
        detached_runs = [
            call for call in self._docker_calls()
            if call[:1] == ["run"] and "-d" in call
        ]
        self.assertEqual(len(detached_runs), 3)
        for call in detached_runs:
            label_index = call.index("--label")
            self.assertEqual(call[label_index + 1], ownership_label)
        remote_waits = [
            call for call in self._docker_calls()
            if "wait-http" in call
        ]
        self.assertEqual(len(remote_waits), 1)
        self.assertIn("--read-only", remote_waits[0])
        maintenance_checks = [
            call for call in self._docker_calls()
            if "assert-maintenance-http" in call
        ]
        self.assertEqual(len(maintenance_checks), 1)
        self.assertIn("--read-only", maintenance_checks[0])
        self.assertIn("--expected-state", maintenance_checks[0])
        state_index = maintenance_checks[0].index("--expected-state")
        self.assertEqual(maintenance_checks[0][state_index + 1], "migrating")
        images_index = maintenance_checks[0].index("--expected-images")
        files_index = maintenance_checks[0].index("--expected-files")
        self.assertEqual(maintenance_checks[0][images_index + 1], "1")
        self.assertEqual(maintenance_checks[0][files_index + 1], "1")
        helper_markers = set()
        for call in self._docker_calls():
            if "-c" not in call:
                continue
            helper = call[call.index("-c") + 1]
            for marker in (
                "_NAS_SQLITE_VALIDATE",
                "_NAS_BACKUP_PRESERVATION_VALIDATE",
                "_NAS_LEGACY_PIN_SAMPLE",
                "_NAS_CANONICAL_MEDIA_VALIDATE",
                "_NAS_EXISTING_PIN_ORM",
                "_NAS_EXISTING_MEDIA_HTTP",
                "_NAS_ACCEPTANCE_METRICS",
                "_NAS_WAIT_STATUS",
            ):
                if marker in helper:
                    ast.parse(helper, feature_version=(3, 7))
                    helper_markers.add(marker)
            if "--network-alias" in call:
                alias = call[call.index("--network-alias") + 1]
                if alias == "image-source":
                    syntax = subprocess.run(
                        ["/bin/sh", "-n", "-c", helper],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    self.assertEqual(
                        syntax.returncode,
                        0,
                        syntax.stderr.decode("utf-8"),
                    )
        self.assertEqual(helper_markers, {
            "_NAS_SQLITE_VALIDATE",
            "_NAS_BACKUP_PRESERVATION_VALIDATE",
            "_NAS_LEGACY_PIN_SAMPLE",
            "_NAS_CANONICAL_MEDIA_VALIDATE",
            "_NAS_EXISTING_PIN_ORM",
            "_NAS_EXISTING_MEDIA_HTTP",
            "_NAS_ACCEPTANCE_METRICS",
            "_NAS_WAIT_STATUS",
        })
        sqlite_calls = [
            call for call in self._docker_calls()
            if "_NAS_SQLITE_VALIDATE" in "\n".join(call)
        ]
        self.assertEqual(len(sqlite_calls), 1)
        sqlite_call = sqlite_calls[0]
        self.assertIn("--read-only", sqlite_call)
        network_index = sqlite_call.index("--network")
        self.assertEqual(sqlite_call[network_index + 1], "none")
        sqlite_mounts = [
            sqlite_call[index + 1]
            for index, argument in enumerate(sqlite_call)
            if argument == "--mount"
        ]
        self.assertEqual(
            sqlite_mounts,
            [
                "type=bind,src={},dst=/data,readonly".format(
                    clone_data
                ),
            ],
        )
        sqlite_helper = sqlite_call[sqlite_call.index("-c") + 1]
        self.assertIn("mode=ro", sqlite_helper)
        self.assertIn("PRAGMA integrity_check", sqlite_helper)
        self.assertIn("PRAGMA foreign_key_check", sqlite_helper)

    def test_rejects_network_without_internal_isolation(self):
        environment = self.environment.copy()
        environment["PINRY_DOCKER_NETWORK_INTERNAL"] = "false"

        completed = self._run(
            run_id="network-not-internal",
            environment=environment,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stderr.decode("ascii").strip(),
            "nas_network_isolation_failed",
        )

    def test_pins_all_runtime_containers_to_inspected_image_id(self):
        environment = self.environment.copy()
        environment["PINRY_REQUIRE_PINNED_IMAGE"] = "1"
        completed = self._run(
            run_id="pinned-image",
            environment=environment,
        )

        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8"),
        )
        image_id = "sha256:" + "1" * 64
        calls = self._docker_calls()
        digest_inspects = [
            call for call in calls
            if call[:2] == ["image", "inspect"]
            and "RepoDigests" in " ".join(call)
        ]
        self.assertEqual(len(digest_inspects), 1)
        self.assertEqual(digest_inspects[0][-1], image_id)
        runtime_calls = [call for call in calls if call[:1] == ["run"]]
        self.assertTrue(runtime_calls)
        self.assertTrue(all(image_id in call for call in runtime_calls))
        self.assertTrue(all(
            "fixture-image:latest" not in call for call in runtime_calls
        ))

    def test_failure_preserves_clone_and_writes_atomic_result(self):
        before = _tree_snapshot(self.source_project)
        environment = self.environment.copy()
        environment["PINRY_DOCKER_APP_START_FAIL"] = "1"
        completed = self._run(
            run_id="app-failure",
            environment=environment,
        )
        clone = self._clone_path("app-failure")
        result_path = self._result_path("app-failure")

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stderr.decode("utf-8").strip(),
            "nas_app_start_failed",
        )
        self.assertTrue(clone.is_dir())
        self.assertTrue((clone / "data/production.db").is_file())
        self.assertEqual(_tree_snapshot(self.source_project), before)
        payload = self._assert_canonical_result(result_path)
        self.assertEqual(payload["state"], "failed")
        self.assertEqual(payload["error_code"], "nas_app_start_failed")
        self.assertEqual(payload["source_commit"], "a" * 40)
        self.assertTrue(payload["source_fingerprint_unchanged"])
        self.assertFalse(list(self.result_root.glob(".*.tmp-*")))
        for call in self._docker_calls():
            if call[:2] == ["rm", "-f"]:
                self.assertIn("-v", call)
                self.assertNotIn(str(self.source_project), call)
                self.assertNotIn(str(clone), call)
        self._assert_public_output(completed, result_path)

    def test_first_app_exit_fails_without_waiting_for_deadline(self):
        environment = self.environment.copy()
        environment["PINRY_DOCKER_APP_EXITED"] = "first"

        completed = self._run(
            run_id="first-app-exited",
            environment=environment,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stderr.decode("utf-8").strip(),
            "nas_app_exited",
        )
        payload = self._assert_canonical_result(
            self._result_path("first-app-exited")
        )
        self.assertEqual(payload["state"], "failed")
        self.assertEqual(payload["error_code"], "nas_app_exited")
        self.assertEqual(payload["source_commit"], "a" * 40)

    def test_restart_app_exit_fails_without_waiting_for_deadline(self):
        environment = self.environment.copy()
        environment["PINRY_DOCKER_APP_EXITED"] = "second"

        completed = self._run(
            run_id="restart-app-exited",
            environment=environment,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stderr.decode("utf-8").strip(),
            "nas_app_exited",
        )
        payload = self._assert_canonical_result(
            self._result_path("restart-app-exited")
        )
        self.assertEqual(payload["state"], "failed")
        self.assertEqual(payload["error_code"], "nas_app_exited")
        self.assertEqual(payload["source_commit"], "a" * 40)

    def test_interrupt_stops_status_watcher_and_removes_private_output(self):
        wait_pid_path = self.temporary_root / "wait.pid"
        environment = self.environment.copy()
        environment["PINRY_WAIT_PID_PATH"] = str(wait_pid_path)
        run_id = "interrupted-watch"
        command = [
            "/bin/bash",
            str(ACCEPTANCE_SCRIPT),
            "--source-project",
            str(self.source_project),
            "--run-root",
            str(self.run_root),
            "--run-id",
            run_id,
            "--image",
            "fixture-image:latest",
            "--result-root",
            str(self.result_root),
            "--expected-pins",
            "1",
            "--expected-files",
            "1",
            "--expected-images",
            "1",
            "--expected-thumbnails",
            "0",
            "--expected-active-files",
            "1",
            "--expected-source-commit",
            "a" * 40,
        ]
        process = subprocess.Popen(
            command,
            cwd=str(REPOSITORY_ROOT),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(
            lambda: process.kill() if process.poll() is None else None
        )
        for _ in range(100):
            if wait_pid_path.is_file():
                break
            if process.poll() is not None:
                self.fail("status watcher did not start")
            time.sleep(0.05)
        else:
            self.fail("status watcher pid was not recorded")

        wait_pid = int(wait_pid_path.read_text())
        process.terminate()
        stdout, stderr = process.communicate(timeout=10)

        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(
            stderr.decode("utf-8").strip(),
            "nas_acceptance_interrupted",
        )
        self.assertEqual(stdout, b"")
        for _ in range(50):
            try:
                os.kill(wait_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail("status watcher survived the interrupted acceptance")
        clone = self._clone_path(run_id)
        self.assertFalse(list(clone.glob(".acceptance-watch-*")))
        self.assertFalse(list(self.result_root.glob(".*.wait-*")))
        self.assertFalse(list(self.result_root.glob(".*.maintenance-*")))

    def test_cleanup_never_removes_failed_network_collision(self):
        environment = self.environment.copy()
        environment["PINRY_DOCKER_NETWORK_CREATE_FAIL"] = "1"
        completed = self._run(
            run_id="network-collision",
            environment=environment,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stderr.decode("utf-8").strip(),
            "nas_network_create_failed",
        )
        calls = self._docker_calls()
        self.assertFalse(any(
            call[:2] == ["network", "rm"] for call in calls
        ))

    def test_cleanup_removes_only_successfully_created_container_ids(self):
        environment = self.environment.copy()
        environment["PINRY_DOCKER_APP_START_FAIL"] = "1"
        completed = self._run(
            run_id="container-collision",
            environment=environment,
        )

        self.assertNotEqual(completed.returncode, 0)
        cleanup_calls = [
            call for call in self._docker_calls()
            if call[:2] == ["rm", "-f"]
        ]
        self.assertEqual(len(cleanup_calls), 1)
        self.assertEqual(cleanup_calls[0][-1], "3" * 64)
        self.assertIn("-v", cleanup_calls[0])
        network_cleanup_calls = [
            call for call in self._docker_calls()
            if call[:2] == ["network", "rm"]
        ]
        self.assertEqual(network_cleanup_calls, [
            ["network", "rm", "6" * 64],
        ])

    def test_failure_rechecks_source_fingerprint_before_result(self):
        source_media = (
            self.source_project
            / "data/static/media/private-photo-secret.png"
        )
        environment = self.environment.copy()
        environment["PINRY_DOCKER_APP_START_FAIL"] = "1"
        environment["PINRY_DOCKER_APP_START_MUTATE_SOURCE"] = str(
            source_media
        )
        completed = self._run(
            run_id="late-source-change",
            environment=environment,
        )
        result_path = self._result_path("late-source-change")

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stderr.decode("utf-8").strip(),
            "nas_source_changed_during_acceptance",
        )
        payload = self._assert_canonical_result(result_path)
        self.assertEqual(
            payload["error_code"],
            "nas_source_changed_during_acceptance",
        )
        self.assertFalse(payload["source_fingerprint_unchanged"])
        self._assert_public_output(completed, result_path)

    def test_sqlite_failure_stops_before_network_or_app_start(self):
        environment = self.environment.copy()
        environment["PINRY_SQLITE_FAIL"] = "1"
        completed = self._run(
            run_id="sqlite-failure",
            environment=environment,
        )
        clone = self._clone_path("sqlite-failure")
        result_path = self._result_path("sqlite-failure")

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stderr.decode("utf-8").strip(),
            "nas_sqlite_validation_failed",
        )
        self.assertTrue((clone / "data/production.db").is_file())
        payload = self._assert_canonical_result(result_path)
        self.assertEqual(payload["state"], "failed")
        self.assertEqual(
            payload["error_code"],
            "nas_sqlite_validation_failed",
        )
        calls = self._docker_calls()
        self.assertFalse(any(call[:2] == ["network", "create"] for call in calls))
        self.assertFalse(any(
            "--network-alias" in call
            and call[call.index("--network-alias") + 1] == "app"
            for call in calls
        ))
        self._assert_public_output(completed, result_path)

    def test_noop_restart_rejects_clone_database_mutation(self):
        environment = self.environment.copy()
        environment["PINRY_SECOND_START_MUTATE_CLONE"] = "1"
        completed = self._run(
            run_id="noop-mutation",
            environment=environment,
        )
        result_path = self._result_path("noop-mutation")

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stderr.decode("utf-8").strip(),
            "nas_noop_restart_changed_data",
        )
        payload = self._assert_canonical_result(result_path)
        self.assertEqual(payload["state"], "failed")
        self.assertEqual(
            payload["error_code"],
            "nas_noop_restart_changed_data",
        )
        self.assertEqual(payload["restart_count"], 1)
        self.assertTrue(payload["legacy_backup_present"])
        self.assertEqual(payload["source_commit"], "a" * 40)
        self._assert_public_output(completed, result_path)

    def test_noop_restart_rejects_same_size_media_mutation(self):
        environment = self.environment.copy()
        environment["PINRY_SECOND_START_MUTATE_MEDIA"] = "1"
        completed = self._run(
            run_id="noop-media-mutation",
            environment=environment,
        )
        result_path = self._result_path("noop-media-mutation")

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stderr.decode("utf-8").strip(),
            "nas_noop_restart_changed_data",
        )
        payload = self._assert_canonical_result(result_path)
        self.assertEqual(payload["state"], "failed")
        self.assertEqual(
            payload["error_code"],
            "nas_noop_restart_changed_data",
        )
        self.assertEqual(payload["restart_count"], 1)
        self._assert_public_output(completed, result_path)
