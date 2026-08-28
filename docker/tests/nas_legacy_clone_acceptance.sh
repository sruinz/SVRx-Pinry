#!/bin/bash
set -euo pipefail

umask 077
export LC_ALL=C

source_input=""
run_root_input=""
run_id=""
image=""
result_root_input=""
expected_pins=""
expected_files=""
expected_images=""
expected_thumbnails=""
expected_active_files=""
expected_source_commit=""

argument_error() {
    printf '%s\n' "nas_arguments_invalid" >&2
    exit 2
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --source-project)
            [ -z "${source_input}" ] && [ "$#" -ge 2 ] \
                || argument_error
            source_input="$2"
            shift 2
            ;;
        --run-root)
            [ -z "${run_root_input}" ] && [ "$#" -ge 2 ] \
                || argument_error
            run_root_input="$2"
            shift 2
            ;;
        --run-id)
            [ -z "${run_id}" ] && [ "$#" -ge 2 ] || argument_error
            run_id="$2"
            shift 2
            ;;
        --image)
            [ -z "${image}" ] && [ "$#" -ge 2 ] || argument_error
            image="$2"
            shift 2
            ;;
        --result-root)
            [ -z "${result_root_input}" ] && [ "$#" -ge 2 ] \
                || argument_error
            result_root_input="$2"
            shift 2
            ;;
        --expected-pins)
            [ -z "${expected_pins}" ] && [ "$#" -ge 2 ] \
                || argument_error
            expected_pins="$2"
            shift 2
            ;;
        --expected-files)
            [ -z "${expected_files}" ] && [ "$#" -ge 2 ] \
                || argument_error
            expected_files="$2"
            shift 2
            ;;
        --expected-images)
            [ -z "${expected_images}" ] && [ "$#" -ge 2 ] \
                || argument_error
            expected_images="$2"
            shift 2
            ;;
        --expected-thumbnails)
            [ -z "${expected_thumbnails}" ] && [ "$#" -ge 2 ] \
                || argument_error
            expected_thumbnails="$2"
            shift 2
            ;;
        --expected-active-files)
            [ -z "${expected_active_files}" ] && [ "$#" -ge 2 ] \
                || argument_error
            expected_active_files="$2"
            shift 2
            ;;
        --expected-source-commit)
            [ -z "${expected_source_commit}" ] && [ "$#" -ge 2 ] \
                || argument_error
            expected_source_commit="$2"
            shift 2
            ;;
        *)
            argument_error
            ;;
    esac
done

[ -n "${source_input}" ] \
    && [ -n "${run_root_input}" ] \
    && [ -n "${run_id}" ] \
    && [ -n "${image}" ] \
    && [ -n "${result_root_input}" ] \
    && [ -n "${expected_pins}" ] \
    && [ -n "${expected_files}" ] \
    && [ -n "${expected_images}" ] \
    && [ -n "${expected_thumbnails}" ] \
    && [ -n "${expected_active_files}" ] \
    && [ -n "${expected_source_commit}" ] \
    || argument_error
[[ "${run_id}" =~ ^[A-Za-z0-9._-]+$ ]] || argument_error
[[ "${expected_pins}" =~ ^[1-9][0-9]*$ ]] || argument_error
[[ "${expected_files}" =~ ^[1-9][0-9]*$ ]] || argument_error
[[ "${expected_images}" =~ ^[1-9][0-9]*$ ]] || argument_error
[[ "${expected_thumbnails}" =~ ^[0-9]+$ ]] || argument_error
[[ "${expected_active_files}" =~ ^[1-9][0-9]*$ ]] || argument_error
[[ "${expected_source_commit}" =~ ^[0-9a-f]{40}$ ]] || argument_error

canonical_directory() {
    (
        CDPATH= cd -- "$1" 2>/dev/null
        pwd -P
    ) 2>/dev/null
}

early_failure() {
    printf '%s\n' "$1" >&2
    exit 1
}

final_component_is_symlink() {
    local path="$1"

    while [ "${path}" != "/" ] && [ "${path%/}" != "${path}" ]; do
        path="${path%/}"
    done
    [ -L "${path}" ]
}

[ -d "${source_input}" ] \
    && ! final_component_is_symlink "${source_input}" \
    || early_failure "nas_source_project_invalid"
[ -d "${run_root_input}" ] \
    && ! final_component_is_symlink "${run_root_input}" \
    || early_failure "nas_run_root_invalid"
[ -d "${result_root_input}" ] \
    && ! final_component_is_symlink "${result_root_input}" \
    || early_failure "nas_result_root_invalid"

source_project="$(canonical_directory "${source_input}")" \
    || early_failure "nas_source_project_invalid"
run_root="$(canonical_directory "${run_root_input}")" \
    || early_failure "nas_run_root_invalid"
result_root="$(canonical_directory "${result_root_input}")" \
    || early_failure "nas_result_root_invalid"
source_data="${source_project}/data"
clone_project="${run_root}/svrx-pinry-accept-${run_id}"
clone_data="${clone_project}/data"
result_name="svrx-pinry-accept-${run_id}.json"
result_json="${result_root}/${result_name}"
result_identity=""

[ -d "${source_data}" ] && [ ! -L "${source_data}" ] \
    && [ -f "${source_data}/production.db" ] \
    && [ ! -L "${source_data}/production.db" ] \
    && [ -d "${source_data}/static/media" ] \
    && [ ! -L "${source_data}/static/media" ] \
    || early_failure "nas_source_layout_invalid"

[ ! -e "${clone_project}" ] && [ ! -L "${clone_project}" ] \
    || early_failure "nas_clone_exists"
[ ! -e "${result_json}" ] && [ ! -L "${result_json}" ] \
    || early_failure "nas_result_exists"

paths_overlap() {
    [ "$1" = "$2" ] \
        || [[ "$1" == "$2"/* ]] \
        || [[ "$2" == "$1"/* ]]
}

if paths_overlap "${source_project}" "${clone_project}" \
    || paths_overlap "${source_project}" "${result_json}" \
    || paths_overlap "${clone_project}" "${result_json}"; then
    early_failure "nas_path_overlap"
fi

for dependency in python3 rsync docker; do
    command -v "${dependency}" >/dev/null 2>&1 \
        || early_failure "nas_dependency_unavailable"
done

script_directory="$(
    CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null
    pwd -P
)" || early_failure "nas_fixture_missing"
fixture_script="${script_directory}/fixtures/create_legacy_fixture.py"
[ -f "${fixture_script}" ] && [ ! -L "${fixture_script}" ] \
    || early_failure "nas_fixture_missing"

created_container_ids=""
created_network_id=""
network_name=""
result_active=0
started_at=""
completed_at=""
duration_seconds=""
legacy_metrics=""
final_metrics=""
batches=""
max_heartbeat_gap=""
restart_count="0"
noop_restart=""
legacy_backup_present=""
temporary_pin_check=""
source_fingerprint_unchanged=""
source_fingerprint_ready=0
source_final_check_complete=0
source_commit=""
image_id=""
image_digest=""
runtime_image=""
remote_container_id=""
started_app_id=""
first_app_id=""
second_app_id=""
watch_pid=""
watch_output_file=""
api_stdout_file=""
api_stderr_file=""
wait_metrics=""
legacy_database_logical_sha=""
legacy_pin_sample=""
canonical_pin_sample=""
existing_pin_site_public=""

remember_container_id() {
    created_container_ids="${created_container_ids} $1"
}

protect_clone_root() {
    [ -d "${clone_project}" ] && [ ! -L "${clone_project}" ] \
        && chmod 0700 "${clone_project}"
}

stop_watch_process() {
    if [ -n "${watch_pid}" ]; then
        kill "${watch_pid}" >/dev/null 2>&1 || true
        wait "${watch_pid}" >/dev/null 2>&1 || true
        watch_pid=""
    fi
}

remove_watch_output() {
    if [ -n "${watch_output_file}" ]; then
        rm -f "${watch_output_file}" >/dev/null 2>&1 || true
        watch_output_file=""
    fi
}

remove_api_output() {
    if [ -n "${api_stdout_file}" ]; then
        rm -f "${api_stdout_file}" >/dev/null 2>&1 || true
        api_stdout_file=""
    fi
    if [ -n "${api_stderr_file}" ]; then
        rm -f "${api_stderr_file}" >/dev/null 2>&1 || true
        api_stderr_file=""
    fi
}

cleanup() {
    local container_id

    stop_watch_process
    remove_watch_output
    remove_api_output
    protect_clone_root >/dev/null 2>&1 || true
    for container_id in ${created_container_ids}; do
        docker rm -f -v "${container_id}" >/dev/null 2>&1 || true
    done
    if [ -n "${created_network_id}" ]; then
        docker network rm "${created_network_id}" >/dev/null 2>&1 \
            || true
    fi
}

timestamp() {
    date -u '+%Y-%m-%dT%H:%M:%SZ'
}

publish_result() {
    local state="$1"
    local error_code="$2"

    python3 - \
        "${result_root}" \
        "${result_name}" \
        "${state}" \
        "${error_code}" \
        "${run_id}" \
        "${image_id}" \
        "${image_digest}" \
        "${started_at}" \
        "${completed_at}" \
        "${duration_seconds}" \
        "${legacy_metrics}" \
        "${final_metrics}" \
        "${batches}" \
        "${max_heartbeat_gap}" \
        "${restart_count}" \
        "${noop_restart}" \
        "${legacy_backup_present}" \
        "${temporary_pin_check}" \
        "${source_fingerprint_unchanged}" \
        "${source_commit}" \
        "${result_identity}" \
        2>/dev/null <<'PY'
import json
import ctypes
import os
import re
import secrets
import stat
import sys

(
    result_root,
    result_name,
    state,
    error_code,
    run_id,
    image_id,
    image_digest,
    started_at,
    completed_at,
    duration_seconds,
    legacy_raw,
    final_raw,
    batches,
    max_heartbeat_gap,
    restart_count,
    noop_restart,
    legacy_backup_present,
    temporary_pin_check,
    source_fingerprint_unchanged,
    source_commit,
    expected_result_identity,
) = sys.argv[1:]

run_pattern = re.compile(r"^[A-Za-z0-9._-]+$")
error_pattern = re.compile(r"^nas_[a-z0-9_]+$")
timestamp_pattern = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
)
sha_pattern = re.compile(r"^sha256:[0-9a-f]{64}$")
commit_pattern = re.compile(r"^(?:development|[0-9a-f]{40,64})$")
identity_pattern = re.compile(r"^[0-9]+:[0-9]+$")

if (
    state not in ("running", "succeeded", "failed")
    or run_pattern.fullmatch(run_id) is None
    or timestamp_pattern.fullmatch(started_at) is None
):
    raise SystemExit(1)
if state == "failed":
    if error_pattern.fullmatch(error_code) is None:
        raise SystemExit(1)
elif error_code:
    raise SystemExit(1)
if completed_at and timestamp_pattern.fullmatch(completed_at) is None:
    raise SystemExit(1)
if image_id and sha_pattern.fullmatch(image_id) is None:
    raise SystemExit(1)
if image_digest and sha_pattern.fullmatch(image_digest) is None:
    raise SystemExit(1)
if source_commit and commit_pattern.fullmatch(source_commit) is None:
    raise SystemExit(1)
if state == "running":
    if expected_result_identity:
        raise SystemExit(1)
elif identity_pattern.fullmatch(expected_result_identity) is None:
    raise SystemExit(1)


def optional_int(value):
    if not value:
        return None
    parsed = int(value)
    if parsed < 0:
        raise ValueError
    return parsed


def optional_float(value):
    if not value:
        return None
    parsed = float(value)
    if parsed < 0 or not parsed < float("inf"):
        raise ValueError
    return parsed


def optional_bool(value):
    if not value:
        return None
    if value == "true":
        return True
    if value == "false":
        return False
    raise ValueError


def counts(raw):
    empty = {
        "active_files": None,
        "files": None,
        "images": None,
        "pins": None,
        "thumbnails": None,
    }
    if not raw:
        return empty
    value = json.loads(raw)
    expected = {
        "backup_count",
        "backup_present",
        "files",
        "images",
        "pins",
        "planned_files",
        "source_commit",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError
    for name in (
        "backup_count", "files", "images", "pins", "planned_files",
    ):
        item = value[name]
        if type(item) is not int or item < 0:
            raise ValueError
    if type(value["backup_present"]) is not bool:
        raise ValueError
    thumbnails = value["planned_files"] - value["images"]
    if thumbnails < 0:
        raise ValueError
    commit = value["source_commit"]
    if commit is not None and (
        not isinstance(commit, str)
        or commit_pattern.fullmatch(commit) is None
    ):
        raise ValueError
    return {
        "active_files": value["planned_files"],
        "files": value["files"],
        "images": value["images"],
        "pins": value["pins"],
        "thumbnails": thumbnails,
    }


try:
    duration = optional_int(duration_seconds)
    batch_count = optional_int(batches)
    restart_total = optional_int(restart_count)
    heartbeat_gap = optional_float(max_heartbeat_gap)
    noop_value = optional_bool(noop_restart)
    backup_value = optional_bool(legacy_backup_present)
    fingerprint_value = optional_bool(source_fingerprint_unchanged)
    legacy_counts = counts(legacy_raw)
    final_counts = counts(final_raw)
except (TypeError, ValueError, json.JSONDecodeError):
    raise SystemExit(1)

if temporary_pin_check not in ("", "passed"):
    raise SystemExit(1)
if state == "running" and (
    completed_at
    or duration is not None
    or error_code
):
    raise SystemExit(1)
if state != "running" and (
    not completed_at or duration is None
):
    raise SystemExit(1)

payload = {
    "batches": batch_count,
    "completed_at": completed_at or None,
    "duration_seconds": duration,
    "error_code": error_code or None,
    "final_counts": final_counts,
    "image_digest": image_digest or None,
    "image_id": image_id or None,
    "legacy_backup_present": backup_value,
    "legacy_counts": legacy_counts,
    "max_heartbeat_gap_seconds": heartbeat_gap,
    "noop_restart": noop_value,
    "restart_count": restart_total,
    "run_id": run_id,
    "schema_version": 1,
    "source_commit": source_commit or None,
    "source_fingerprint_unchanged": fingerprint_value,
    "started_at": started_at,
    "state": state,
    "temporary_pin_check": temporary_pin_check or None,
}
encoded = json.dumps(
    payload,
    ensure_ascii=True,
    sort_keys=True,
    separators=(",", ":"),
).encode("ascii") + b"\n"


def write_all(descriptor, content):
    written = 0
    while written < len(content):
        count = os.write(descriptor, content[written:])
        if count <= 0:
            raise OSError("result_write_failed")
        written += count


def exchange_entries(directory_fd, first_name, second_name):
    libc = ctypes.CDLL(None, use_errno=True)
    first_bytes = os.fsencode(first_name)
    second_bytes = os.fsencode(second_name)
    if sys.platform == "darwin":
        exchange = getattr(libc, "renameatx_np", None)
        if exchange is None:
            raise OSError("atomic_exchange_unsupported")
        exchange.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        exchange.restype = ctypes.c_int
        result = exchange(
            directory_fd,
            first_bytes,
            directory_fd,
            second_bytes,
            0x00000002,
        )
    elif sys.platform.startswith("linux"):
        exchange = getattr(libc, "renameat2", None)
        if exchange is not None:
            exchange.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            exchange.restype = ctypes.c_int
            result = exchange(
                directory_fd,
                first_bytes,
                directory_fd,
                second_bytes,
                0x00000002,
            )
        else:
            machine = os.uname().machine.lower()
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
                raise OSError("atomic_exchange_unsupported")
            syscall.restype = ctypes.c_long
            result = syscall(
                ctypes.c_long(syscall_number),
                ctypes.c_int(directory_fd),
                ctypes.c_char_p(first_bytes),
                ctypes.c_int(directory_fd),
                ctypes.c_char_p(second_bytes),
                ctypes.c_uint(0x00000002),
            )
    else:
        raise OSError("atomic_exchange_unsupported")
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))

directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
nofollow = getattr(os, "O_NOFOLLOW", 0)
directory_fd = os.open(result_root, directory_flags | nofollow)
temporary_name = ".{}.tmp-{}".format(
    result_name,
    secrets.token_hex(8),
)
temporary_fd = None
temporary_name_owned = False
try:
    temporary_fd = os.open(
        temporary_name,
        os.O_RDWR | os.O_CREAT | os.O_EXCL | nofollow,
        0o600,
        dir_fd=directory_fd,
    )
    temporary_name_owned = True
    os.fchmod(temporary_fd, 0o600)
    write_all(temporary_fd, encoded)
    os.fsync(temporary_fd)
    temporary_entry = os.fstat(temporary_fd)
    if (
        not stat.S_ISREG(temporary_entry.st_mode)
        or temporary_entry.st_nlink != 1
        or stat.S_IMODE(temporary_entry.st_mode) != 0o600
    ):
        raise OSError("temporary_result_invalid")
    temporary_identity = (
        temporary_entry.st_dev,
        temporary_entry.st_ino,
    )
    if state == "running":
        os.link(
            temporary_name,
            result_name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        existing = os.stat(
            result_name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(existing.st_mode)
            or existing.st_nlink != 2
            or stat.S_IMODE(existing.st_mode) != 0o600
            or (existing.st_dev, existing.st_ino)
            != temporary_identity
        ):
            raise OSError("result_invalid")
        os.unlink(temporary_name, dir_fd=directory_fd)
        temporary_name_owned = False
        print("{}:{}".format(*temporary_identity))
    else:
        existing = os.stat(
            result_name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        expected_device, expected_inode = (
            int(value) for value in expected_result_identity.split(":")
        )
        if (
            not stat.S_ISREG(existing.st_mode)
            or existing.st_nlink != 1
            or stat.S_IMODE(existing.st_mode) != 0o600
            or (existing.st_dev, existing.st_ino)
            != (expected_device, expected_inode)
        ):
            raise OSError("result_identity_changed")
        exchange_entries(
            directory_fd,
            temporary_name,
            result_name,
        )
        temporary_name_owned = False
        previous = os.stat(
            temporary_name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(previous.st_mode)
            or (previous.st_dev, previous.st_ino)
            != (expected_device, expected_inode)
        ):
            exchange_entries(
                directory_fd,
                temporary_name,
                result_name,
            )
            temporary_name_owned = True
            raise OSError("result_identity_changed")
        published = os.stat(
            result_name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(published.st_mode)
            or published.st_nlink != 1
            or stat.S_IMODE(published.st_mode) != 0o600
            or (published.st_dev, published.st_ino)
            != temporary_identity
        ):
            exchange_entries(
                directory_fd,
                temporary_name,
                result_name,
            )
            temporary_name_owned = True
            raise OSError("result_publish_changed")
        try:
            os.fsync(directory_fd)
        except BaseException:
            exchange_entries(
                directory_fd,
                temporary_name,
                result_name,
            )
            temporary_name_owned = True
            raise
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
            os.fsync(directory_fd)
        except OSError:
            pass
    if state == "running":
        os.fsync(directory_fd)
finally:
    if temporary_fd is not None:
        os.close(temporary_fd)
    if temporary_name_owned:
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
    os.close(directory_fd)
PY
}

fatal() {
    local code="$1"
    local failure_source_fingerprint=""

    if [ "${result_active}" -eq 1 ]; then
        if [ "${source_fingerprint_ready}" -eq 1 ] \
            && [ "${source_final_check_complete}" -eq 0 ] \
            && [ "${code}" != "nas_source_changed_during_copy" ]; then
            source_final_check_complete=1
            if failure_source_fingerprint="$(
                fingerprint_data "${source_data}"
            )" && [ "${failure_source_fingerprint}" \
                = "${initial_fingerprint}" ]; then
                source_fingerprint_unchanged="true"
            else
                source_fingerprint_unchanged="false"
                code="nas_source_changed_during_acceptance"
            fi
        fi
        completed_at="$(timestamp)" || completed_at=""
        duration_seconds="${SECONDS}"
        if ! publish_result "failed" "${code}"; then
            code="nas_result_publish_failed"
        fi
    fi
    printf '%s\n' "${code}" >&2
    exit 1
}

on_signal() {
    trap - HUP INT TERM
    fatal "nas_acceptance_interrupted"
}

trap cleanup EXIT
trap on_signal HUP INT TERM

printf -v random_suffix '%04x%04x' "${RANDOM}" "${RANDOM}"
SECONDS=0
started_at="$(timestamp)" || early_failure "nas_clock_failed"
if ! result_identity="$(publish_result "running" "")" \
    || [[ ! "${result_identity}" =~ ^[0-9]+:[0-9]+$ ]]; then
    early_failure "nas_result_publish_failed"
fi
result_active=1

container_snapshot_overlaps_source() {
    python3 - "${source_project}" "${source_data}" "$1" \
        >/dev/null 2>&1 <<'PY'
import json
import os
import sys

project = os.path.realpath(sys.argv[1])
data = os.path.realpath(sys.argv[2])
project_parent = os.path.dirname(project)
raw = sys.argv[3]
decoder = json.JSONDecoder()
running, offset = decoder.raw_decode(raw)
while offset < len(raw) and raw[offset].isspace():
    offset += 1
mounts, offset = decoder.raw_decode(raw, offset)
if raw[offset:].strip():
    raise SystemExit(2)
if type(running) is not bool:
    raise SystemExit(2)
if not isinstance(mounts, list):
    raise SystemExit(2)
if not running:
    raise SystemExit(0)


def paths_overlap(left, right):
    left_prefix = left if left.endswith(os.sep) else left + os.sep
    right_prefix = right if right.endswith(os.sep) else right + os.sep
    return (
        left == right
        or left.startswith(right_prefix)
        or right.startswith(left_prefix)
    )


for mount in mounts:
    if not isinstance(mount, dict):
        raise SystemExit(2)
    if mount.get("Type") != "bind":
        continue
    source = mount.get("Source")
    if not isinstance(source, str) or not source:
        raise SystemExit(2)
    read_write = mount.get("RW")
    if type(read_write) is not bool:
        raise SystemExit(2)
    if not read_write:
        continue
    source = os.path.realpath(source)
    if source == project_parent:
        raise SystemExit(10)
    if paths_overlap(source, project) or paths_overlap(source, data):
        raise SystemExit(10)
raise SystemExit(0)
PY
}

container_snapshot_volume_mounts() {
    python3 - "${source_project}" "${source_data}" "$1" <<'PY'
import json
import os
import sys

project = os.path.realpath(sys.argv[1])
data = os.path.realpath(sys.argv[2])
raw = sys.argv[3]
decoder = json.JSONDecoder()
running, offset = decoder.raw_decode(raw)
while offset < len(raw) and raw[offset].isspace():
    offset += 1
mounts, offset = decoder.raw_decode(raw, offset)
if raw[offset:].strip() or type(running) is not bool or not isinstance(mounts, list):
    raise SystemExit(2)
if not running:
    raise SystemExit(0)


def paths_overlap(left, right):
    left_prefix = left if left.endswith(os.sep) else left + os.sep
    right_prefix = right if right.endswith(os.sep) else right + os.sep
    return (
        left == right
        or left.startswith(right_prefix)
        or right.startswith(left_prefix)
    )


for mount in mounts:
    if not isinstance(mount, dict):
        raise SystemExit(2)
    if mount.get("Type") != "volume" or mount.get("RW") is not True:
        continue
    name = mount.get("Name")
    source = mount.get("Source")
    if (
        not isinstance(name, str)
        or not name
        or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for character in name)
        or not isinstance(source, str)
        or not source
    ):
        raise SystemExit(2)
    source = os.path.realpath(source)
    if paths_overlap(source, project) or paths_overlap(source, data):
        raise SystemExit(10)
    print(name)
PY
}

volume_options_overlap_source() {
    python3 - "${source_project}" "${source_data}" "$1" >/dev/null <<'PY'
import json
import os
import sys

project = os.path.realpath(sys.argv[1])
data = os.path.realpath(sys.argv[2])
try:
    options = json.loads(sys.argv[3])
except (TypeError, ValueError):
    raise SystemExit(2)
if options is None:
    options = {}
if not isinstance(options, dict):
    raise SystemExit(2)
device = options.get("device")
mount_options = options.get("o", "")
mount_type = options.get("type", "")
if not isinstance(mount_options, str) or not isinstance(mount_type, str):
    raise SystemExit(2)
is_bind = mount_type == "none" or "bind" in mount_options.split(",") or "rbind" in mount_options.split(",")
if not is_bind:
    raise SystemExit(0)
if not isinstance(device, str) or not device:
    raise SystemExit(2)
device = os.path.realpath(device)


def paths_overlap(left, right):
    left_prefix = left if left.endswith(os.sep) else left + os.sep
    right_prefix = right if right.endswith(os.sep) else right + os.sep
    return (
        left == right
        or left.startswith(right_prefix)
        or right.startswith(left_prefix)
    )


raise SystemExit(
    10 if paths_overlap(device, project) or paths_overlap(device, data) else 0
)
PY
}

if ! running_containers="$(docker ps -q 2>/dev/null)"; then
    fatal "nas_docker_inspection_failed"
fi
for container_id in ${running_containers}; do
    if ! container_snapshot="$(
        docker inspect \
            --format '{{json .State.Running}} {{json .Mounts}}' \
            "${container_id}" 2>/dev/null
    )"; then
        fatal "nas_docker_inspection_failed"
    fi
    if container_snapshot_overlaps_source "${container_snapshot}"; then
        :
    else
        mount_status="$?"
        if [ "${mount_status}" -eq 10 ]; then
            fatal "nas_source_container_running"
        fi
        fatal "nas_docker_inspection_failed"
    fi
    volume_mount_status=0
    volume_mounts="$(
        container_snapshot_volume_mounts "${container_snapshot}" 2>/dev/null
    )" || volume_mount_status="$?"
    if [ "${volume_mount_status}" -eq 10 ]; then
        fatal "nas_source_container_running"
    fi
    if [ "${volume_mount_status}" -ne 0 ]; then
        fatal "nas_docker_inspection_failed"
    fi
    while IFS= read -r volume_name; do
        [ -n "${volume_name}" ] || continue
        if ! volume_options="$(
            docker volume inspect --format '{{json .Options}}' "${volume_name}" 2>/dev/null
        )"; then
            fatal "nas_docker_inspection_failed"
        fi
        if volume_options_overlap_source "${volume_options}"; then
            :
        else
            volume_status="$?"
            if [ "${volume_status}" -eq 10 ]; then
                fatal "nas_source_container_running"
            fi
            fatal "nas_docker_inspection_failed"
        fi
    done <<<"${volume_mounts}"
done

if ! image_id="$(
    docker image inspect --format '{{.Id}}' "${image}" 2>/dev/null
)"; then
    fatal "nas_image_unavailable"
fi
[[ "${image_id}" =~ ^sha256:[0-9a-f]{64}$ ]] \
    || fatal "nas_image_identity_invalid"
runtime_image="${image_id}"
if ! raw_digest="$(
    docker image inspect \
        --format '{{if .RepoDigests}}{{index .RepoDigests 0}}{{end}}' \
        "${runtime_image}" 2>/dev/null
)"; then
    fatal "nas_image_identity_invalid"
fi
image_digest="${raw_digest##*@}"
if [ -n "${image_digest}" ]; then
    [[ "${image_digest}" =~ ^sha256:[0-9a-f]{64}$ ]] \
        || fatal "nas_image_identity_invalid"
else
    image_digest="${image_id}"
fi
if ! image_source_commit="$(
    docker image inspect \
        --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
        "${runtime_image}" 2>/dev/null
)" || [[ ! "${image_source_commit}" =~ ^[0-9a-f]{40}$ ]]; then
    fatal "nas_image_source_commit_invalid"
fi
source_commit="${image_source_commit}"
if [ "${image_source_commit}" != "${expected_source_commit}" ]; then
    fatal "nas_image_source_commit_mismatch"
fi

fingerprint_data() {
    python3 - "$1" 2>/dev/null <<'PY'
import hashlib
import os
import stat
import sys

data_root = os.path.realpath(sys.argv[1])
database_path = os.path.join(data_root, "production.db")
media_root = os.path.join(data_root, "static", "media")
nofollow = getattr(os, "O_NOFOLLOW", 0)

descriptor = os.open(database_path, os.O_RDONLY | nofollow)
try:
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode):
        raise OSError("database_invalid")
    database_hash = hashlib.sha256()
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        database_hash.update(chunk)
    after = os.fstat(descriptor)
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise OSError("database_changed")
finally:
    os.close(descriptor)

inventory_hash = hashlib.sha256()


def fingerprint_entry(path, relative):
    entry_stat = os.stat(path, follow_symlinks=False)
    mode = entry_stat.st_mode
    metadata = (
        stat.S_IMODE(mode), entry_stat.st_uid, entry_stat.st_gid,
        entry_stat.st_nlink, entry_stat.st_size, entry_stat.st_mtime_ns,
        entry_stat.st_ctime_ns, entry_stat.st_dev, entry_stat.st_ino,
    )
    if stat.S_ISREG(mode):
        descriptor = os.open(path, os.O_RDONLY | nofollow)
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (entry_stat.st_dev, entry_stat.st_ino):
                raise OSError("entry_changed")
            digest = hashlib.sha256()
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        current = os.stat(path, follow_symlinks=False)
        if (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
        ) != (
            current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns,
        ):
            raise OSError("entry_changed")
        kind = b"file"
        payload = digest.digest()
    elif stat.S_ISDIR(mode):
        kind = b"directory"
        payload = b""
    elif stat.S_ISLNK(mode):
        kind = b"symlink"
        payload = os.fsencode(os.readlink(path))
    else:
        kind = b"special"
        payload = str(entry_stat.st_rdev).encode("ascii")
    relative_bytes = os.fsencode(relative)
    fields = [kind, relative_bytes]
    fields.extend(str(value).encode("ascii") for value in metadata)
    fields.append(payload)
    inventory_hash.update(b"\0".join(fields) + b"\0")
    if stat.S_ISDIR(mode):
        with os.scandir(path) as iterator:
            children = sorted(iterator, key=lambda entry: os.fsencode(entry.name))
        for child in children:
            child_relative = os.path.join(relative, child.name) if relative else child.name
            fingerprint_entry(child.path, child_relative)


fingerprint_entry(data_root, "")

media_files = []
for root, directories, files in os.walk(media_root, followlinks=False):
    directories[:] = sorted(directories)
    for name in sorted(files):
        path = os.path.join(root, name)
        before = os.stat(path, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            continue
        descriptor = os.open(path, os.O_RDONLY | nofollow)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino)
                != (before.st_dev, before.st_ino)
            ):
                raise OSError("media_changed")
            content_hash = hashlib.sha256()
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                content_hash.update(chunk)
            after = os.fstat(descriptor)
            current = os.stat(path, follow_symlinks=False)
            if (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ) or (
                current.st_dev,
                current.st_ino,
                current.st_size,
                current.st_mtime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ):
                raise OSError("media_changed")
        finally:
            os.close(descriptor)
        media_files.append((
            os.fsencode(os.path.relpath(path, media_root)),
            before.st_size,
            content_hash.digest(),
        ))

media_hash = hashlib.sha256()
for relative, size, content_digest in sorted(media_files):
    media_hash.update(b"\0".join((
        str(len(relative)).encode("ascii"),
        relative,
        str(size).encode("ascii"),
        content_digest,
    )) + b"\0")

print(
    database_hash.hexdigest(),
    inventory_hash.hexdigest(),
    media_hash.hexdigest(),
)
PY
}

payload_fingerprint() {
    python3 - "$1" 2>/dev/null <<'PY'
import hashlib
import os
import stat
import sys

media_root = os.path.abspath(os.fspath(sys.argv[1]))
root_stat = os.stat(media_root, follow_symlinks=False)
if not stat.S_ISDIR(root_stat.st_mode):
    raise OSError("media_root_invalid")

nofollow = getattr(os, "O_NOFOLLOW", 0)
entries = []
for root, directories, files in os.walk(media_root, followlinks=False):
    retained = []
    for name in sorted(directories):
        path = os.path.join(root, name)
        entry_stat = os.stat(path, follow_symlinks=False)
        if stat.S_ISLNK(entry_stat.st_mode):
            raise OSError("media_symlink_invalid")
        if not stat.S_ISDIR(entry_stat.st_mode):
            raise OSError("media_directory_invalid")
        if (
            root == media_root
            and name in (".pinry-locks", ".staging")
        ):
            continue
        retained.append(name)
    directories[:] = retained
    for name in sorted(files):
        path = os.path.join(root, name)
        before = os.stat(path, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            raise OSError("media_file_invalid")
        descriptor = os.open(path, os.O_RDONLY | nofollow)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino)
                != (before.st_dev, before.st_ino)
            ):
                raise OSError("media_changed")
            content_hash = hashlib.sha256()
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                content_hash.update(chunk)
            after = os.fstat(descriptor)
            current = os.stat(path, follow_symlinks=False)
            if (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ) or (
                current.st_dev,
                current.st_ino,
                current.st_size,
                current.st_mtime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ):
                raise OSError("media_changed")
        finally:
            os.close(descriptor)
        entries.append((
            os.fsencode(os.path.relpath(path, media_root)),
            before.st_size,
            content_hash.digest(),
        ))

manifest = hashlib.sha256()
for relative, size, content_digest in sorted(entries):
    manifest.update(b"\0".join((
        str(len(relative)).encode("ascii"),
        relative,
        str(size).encode("ascii"),
        content_digest,
    )) + b"\0")
print(len(entries), manifest.hexdigest())
PY
}

logical_database_digest() {
    python3 - "$1" 2>/dev/null <<'PY'
import hashlib
import sqlite3
import sys


def encode(value):
    if value is None:
        prefix, payload = b"n", b""
    elif isinstance(value, bytes):
        prefix, payload = b"b", value
    elif isinstance(value, str):
        prefix, payload = b"t", value.encode("utf-8")
    elif isinstance(value, int):
        prefix, payload = b"i", str(value).encode("ascii")
    elif isinstance(value, float):
        prefix, payload = b"f", value.hex().encode("ascii")
    else:
        raise TypeError(type(value).__name__)
    return prefix + str(len(payload)).encode("ascii") + b":" + payload


connection = sqlite3.connect("file:{}?mode=ro".format(sys.argv[1]), uri=True)
try:
    connection.execute("PRAGMA query_only = ON")
    schema = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE name NOT GLOB ? ORDER BY type, name",
        ("sqlite_*",),
    ).fetchall()
    digest = hashlib.sha256()
    for row in schema:
        encoded = b"".join(encode(value) for value in row)
        digest.update(b"s" + str(len(encoded)).encode("ascii"))
        digest.update(b":" + encoded)
    table_names = sorted(row[1] for row in schema if row[0] == "table")
    for table_name in table_names:
        quote_character = chr(34)
        quoted = "{}{}{}".format(
            quote_character,
            table_name.replace(quote_character, quote_character * 2),
            quote_character,
        )
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
    print(digest.hexdigest())
finally:
    connection.close()
PY
}

fingerprint_clone_logical_data() {
    local database_digest=""
    local tree_digest=""
    local media_fingerprint=""
    local data_root="$1"

    database_digest="$(
        logical_database_digest "${data_root}/production.db"
    )" || return 1
    media_fingerprint="$(
        payload_fingerprint "${data_root}/static/media"
    )" || return 1
    tree_digest="$(python3 - "${data_root}" 2>/dev/null <<'PY'
import hashlib
import os
import stat
import sys

root = os.path.realpath(sys.argv[1])
root_stat = os.stat(root, follow_symlinks=False)
if not stat.S_ISDIR(root_stat.st_mode):
    raise OSError("data_root_invalid")

nofollow = getattr(os, "O_NOFOLLOW", 0)
database_files = {
    "production.db",
    "production.db-journal",
    "production.db-shm",
    "production.db-wal",
}
volatile_directories = {
    "static/media/.pinry-locks",
    "static/media/.staging",
}
digest = hashlib.sha256()


def add_fields(*fields):
    digest.update(b"\0".join(fields) + b"\0")


def visit(path, relative):
    entry_stat = os.stat(path, follow_symlinks=False)
    mode = entry_stat.st_mode
    relative_bytes = os.fsencode(relative)
    stable_metadata = (
        str(stat.S_IMODE(mode)).encode("ascii"),
        str(entry_stat.st_uid).encode("ascii"),
        str(entry_stat.st_gid).encode("ascii"),
        str(entry_stat.st_nlink).encode("ascii"),
    )
    if stat.S_ISREG(mode):
        if relative in database_files:
            if relative == "production.db":
                add_fields(b"database", relative_bytes, *stable_metadata)
            return
        descriptor = os.open(path, os.O_RDONLY | nofollow)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino)
                != (entry_stat.st_dev, entry_stat.st_ino)
            ):
                raise OSError("entry_changed")
            content = hashlib.sha256()
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                content.update(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        current = os.stat(path, follow_symlinks=False)
        if (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
        ) != (
            current.st_dev,
            current.st_ino,
            current.st_size,
            current.st_mtime_ns,
        ):
            raise OSError("entry_changed")
        add_fields(
            b"file", relative_bytes, *stable_metadata,
            str(after.st_size).encode("ascii"), content.digest()
        )
        return
    if stat.S_ISLNK(mode):
        add_fields(
            b"symlink", relative_bytes, *stable_metadata,
            os.fsencode(os.readlink(path)),
        )
        return
    if not stat.S_ISDIR(mode):
        raise OSError("special_entry_invalid")
    add_fields(b"directory", relative_bytes, *stable_metadata)
    if relative in volatile_directories:
        return
    with os.scandir(path) as iterator:
        children = sorted(iterator, key=lambda item: os.fsencode(item.name))
    for child in children:
        child_relative = (
            os.path.join(relative, child.name) if relative else child.name
        )
        visit(child.path, child_relative)


visit(root, "")
print(digest.hexdigest())
PY
)" || return 1
    printf '%s %s %s\n' \
        "${database_digest}" "${tree_digest}" "${media_fingerprint}"
}

validate_backup_preservation() {
    local verifier_status=0
    local verifier_output=""

    verifier_output="$(
        docker run --rm --network none --read-only \
            --mount "${clone_mount_read_only}" \
            --entrypoint python "${runtime_image}" -c '
_NAS_BACKUP_PRESERVATION_VALIDATE = True
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
from urllib.parse import quote

data_root = "/data"
expected_files = int(sys.argv[1])
expected_digest = sys.argv[2]
expected_pins = int(sys.argv[3])
expected_images = int(sys.argv[4])
expected_thumbnails = int(sys.argv[5])
expected_source_commit = sys.argv[6]
expected_database_digest = sys.argv[7]


def fail(code):
    print(code)
    raise SystemExit(1)


def encode_database_value(value):
    if value is None:
        prefix, payload = b"n", b""
    elif isinstance(value, bytes):
        prefix, payload = b"b", value
    elif isinstance(value, str):
        prefix, payload = b"t", value.encode("utf-8")
    elif isinstance(value, int):
        prefix, payload = b"i", str(value).encode("ascii")
    elif isinstance(value, float):
        prefix, payload = b"f", value.hex().encode("ascii")
    else:
        raise TypeError(type(value).__name__)
    return prefix + str(len(payload)).encode("ascii") + b":" + payload


def logical_database_digest(connection):
    schema = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE name NOT GLOB ? ORDER BY type, name",
        ("sqlite_*",),
    ).fetchall()
    digest = hashlib.sha256()
    for row in schema:
        encoded = b"".join(encode_database_value(value) for value in row)
        digest.update(b"s" + str(len(encoded)).encode("ascii"))
        digest.update(b":" + encoded)
    table_names = sorted(row[1] for row in schema if row[0] == "table")
    for table_name in table_names:
        quote_character = chr(34)
        quoted = "{}{}{}".format(
            quote_character,
            table_name.replace(quote_character, quote_character * 2),
            quote_character,
        )
        encoded_rows = sorted(
            b"".join(encode_database_value(value) for value in row)
            for row in connection.execute(
                "SELECT * FROM {}".format(quoted)
            ).fetchall()
        )
        digest.update(encode_database_value(table_name))
        digest.update(encode_database_value(len(encoded_rows)))
        for encoded in encoded_rows:
            digest.update(encode_database_value(encoded))
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = ? AND name = ?",
        ("table", "sqlite_sequence"),
    ).fetchone() is not None:
        sequence_rows = sorted(
            b"".join(encode_database_value(value) for value in row)
            for row in connection.execute(
                "SELECT name, seq FROM sqlite_sequence"
            ).fetchall()
        )
        digest.update(encode_database_value("sqlite_sequence"))
        digest.update(encode_database_value(len(sequence_rows)))
        for encoded in sequence_rows:
            digest.update(encode_database_value(encoded))
    return digest.hexdigest()


def payload_fingerprint(media_root):
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    entries = []
    root_stat = os.stat(media_root, follow_symlinks=False)
    if not stat.S_ISDIR(root_stat.st_mode):
        fail("nas_backup_payload_invalid")
    for root, directories, files in os.walk(
        media_root, followlinks=False,
    ):
        retained = []
        for name in sorted(directories):
            path = os.path.join(root, name)
            entry_stat = os.stat(path, follow_symlinks=False)
            if not stat.S_ISDIR(entry_stat.st_mode):
                fail("nas_backup_payload_invalid")
            retained.append(name)
        directories[:] = retained
        for name in sorted(files):
            path = os.path.join(root, name)
            before = os.stat(path, follow_symlinks=False)
            if not stat.S_ISREG(before.st_mode):
                fail("nas_backup_payload_invalid")
            descriptor = os.open(path, os.O_RDONLY | nofollow)
            try:
                opened = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or (opened.st_dev, opened.st_ino)
                    != (before.st_dev, before.st_ino)
                ):
                    fail("nas_backup_payload_invalid")
                content_hash = hashlib.sha256()
                while True:
                    chunk = os.read(descriptor, 1024 * 1024)
                    if not chunk:
                        break
                    content_hash.update(chunk)
                after = os.fstat(descriptor)
                current = os.stat(path, follow_symlinks=False)
                if (
                    before.st_dev,
                    before.st_ino,
                    before.st_size,
                    before.st_mtime_ns,
                ) != (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                ) or (
                    current.st_dev,
                    current.st_ino,
                    current.st_size,
                    current.st_mtime_ns,
                ) != (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                ):
                    fail("nas_backup_payload_invalid")
            finally:
                os.close(descriptor)
            entries.append((
                os.fsencode(os.path.relpath(path, media_root)),
                before.st_size,
                content_hash.digest(),
            ))
    manifest = hashlib.sha256()
    for relative, size, content_digest in sorted(entries):
        manifest.update(b"\0".join((
            str(len(relative)).encode("ascii"),
            relative,
            str(size).encode("ascii"),
            content_digest,
        )) + b"\0")
    return len(entries), manifest.hexdigest()


if (
    expected_files < 0
    or expected_pins < 0
    or expected_images < 0
    or expected_thumbnails < 0
    or re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None
    or re.fullmatch(r"[0-9a-f]{64}", expected_database_digest) is None
    or re.fullmatch(r"[0-9a-f]{40}", expected_source_commit) is None
):
    fail("nas_backup_verification_failed")

backup_root = os.path.join(data_root, "legacy-backup")
try:
    backup_stat = os.stat(backup_root, follow_symlinks=False)
    if not stat.S_ISDIR(backup_stat.st_mode):
        fail("nas_backup_run_invalid")
    run_names = []
    for name in sorted(os.listdir(backup_root)):
        path = os.path.join(backup_root, name)
        entry_stat = os.stat(path, follow_symlinks=False)
        if not stat.S_ISDIR(entry_stat.st_mode):
            fail("nas_backup_run_invalid")
        run_names.append(name)
except OSError:
    fail("nas_backup_run_invalid")
if len(run_names) != 1:
    fail("nas_backup_run_invalid")

run_root = os.path.join(backup_root, run_names[0])
summary_path = os.path.join(run_root, "migration-summary.json")
try:
    summary_stat = os.stat(summary_path, follow_symlinks=False)
    if not stat.S_ISREG(summary_stat.st_mode):
        fail("nas_backup_run_invalid")
    with open(summary_path, encoding="ascii") as stream:
        summary = json.load(stream)
except (OSError, UnicodeError, ValueError):
    fail("nas_backup_run_invalid")
if (
    not isinstance(summary, dict)
    or summary.get("format_version") != 1
    or summary.get("run_id") != run_names[0]
    or summary.get("phase") != "complete"
    or summary.get("backup_relative_name") != "legacy-backup"
    or summary.get("source_commit") != expected_source_commit
):
    fail("nas_backup_run_invalid")

try:
    actual_files, actual_digest = payload_fingerprint(
        os.path.join(run_root, "media")
    )
except OSError:
    fail("nas_backup_payload_invalid")
if actual_files != expected_files or actual_digest != expected_digest:
    fail("nas_backup_payload_invalid")

snapshot_path = os.path.join(
    run_root, "production.db.before-migration",
)
try:
    snapshot_stat = os.stat(snapshot_path, follow_symlinks=False)
    if not stat.S_ISREG(snapshot_stat.st_mode):
        fail("nas_backup_snapshot_invalid")
    connection = sqlite3.connect(
        "file:{}?mode=ro".format(quote(snapshot_path, safe="/")),
        uri=True,
    )
    try:
        connection.execute("PRAGMA query_only = ON")
        integrity = connection.execute(
            "PRAGMA integrity_check"
        ).fetchall()
        foreign_keys = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = '"'"'table'"'"'"
            ).fetchall()
        }
        expected_counts = {
            "core_pin": expected_pins,
            "django_images_image": expected_images,
            "django_images_thumbnail": expected_thumbnails,
        }
        if integrity != [("ok",)] or foreign_keys:
            fail("nas_backup_snapshot_invalid")
        if logical_database_digest(connection) != expected_database_digest:
            fail("nas_backup_snapshot_invalid")
        for table, expected in expected_counts.items():
            if table not in tables:
                fail("nas_backup_snapshot_invalid")
            actual = connection.execute(
                "SELECT COUNT(*) FROM {}".format(table)
            ).fetchone()[0]
            if actual != expected:
                fail("nas_backup_snapshot_invalid")
    finally:
        connection.close()
except (OSError, sqlite3.Error, ValueError):
    fail("nas_backup_snapshot_invalid")

print("NAS_BACKUP_PRESERVATION_OK")
' \
            "${legacy_payload_files}" \
            "${legacy_payload_sha}" \
            "${legacy_pins}" \
            "${legacy_images}" \
            "${legacy_thumbnails}" \
            "${image_source_commit}" \
            "${legacy_database_logical_sha}" \
            2>/dev/null
    )" || verifier_status="$?"
    if [ "${verifier_status}" -ne 0 ] \
        || [ "${verifier_output}" != "NAS_BACKUP_PRESERVATION_OK" ]; then
        case "${verifier_output}" in
            nas_backup_run_invalid|nas_backup_payload_invalid|\
                nas_backup_snapshot_invalid)
                fatal "${verifier_output}"
                ;;
            *)
                fatal "nas_backup_verification_failed"
                ;;
        esac
    fi
}

validate_canonical_media() {
    local verifier_output=""
    local verifier_status=0
    verifier_output="$(
        printf '%s' "${legacy_pin_sample}" | docker run --rm -i \
            --network none --read-only \
            --mount "${clone_mount_read_only}" \
            --entrypoint python "${runtime_image}" -c '
_NAS_CANONICAL_MEDIA_VALIDATE = True
import hashlib
import json
import os
import sqlite3
import stat
import sys
import unicodedata
import uuid
import warnings

from PIL import Image as PILImage
from django_images.services.migration_batch_log import (
    MigrationBatchJournal,
    MigrationBatchLogError,
)

data_root = "/data"
media_root = os.path.join(data_root, "static", "media")
allowed_extensions = frozenset((
    ".jpg", ".png", ".gif", ".webp", ".bmp", ".tif",
))
derivative_names = frozenset(("thumbnail", "standard", "square"))
format_extensions = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "GIF": ".gif",
    "WEBP": ".webp",
    "BMP": ".bmp",
    "TIFF": ".tif",
}
invalid_chars = frozenset("<>:\"/\\|?*")
reserved_stems = frozenset(
    ("CON", "PRN", "AUX", "NUL")
    + tuple("COM{}".format(index) for index in range(1, 10))
    + tuple("LPT{}".format(index) for index in range(1, 10))
)


def fail():
    print("nas_canonical_media_invalid")
    raise SystemExit(1)


def canonical_uuid(value):
    try:
        return str(uuid.UUID(str(value)))
    except (AttributeError, TypeError, ValueError):
        fail()


def sanitize_filename(filename):
    basename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    basename = unicodedata.normalize("NFC", basename)
    return "".join(
        character for character in basename
        if unicodedata.category(character) != "Cc"
    )[:255]


def usable_stem(stem):
    return (
        bool(stem)
        and stem not in (".", "..")
        and stem.split(".", 1)[0].upper() not in reserved_stems
    )


def canonical_original_path(asset_uuid, filename, extension):
    if extension not in allowed_extensions:
        fail()
    asset_uuid_text = canonical_uuid(asset_uuid)
    asset_uuid_hex = uuid.UUID(asset_uuid_text).hex
    basename = sanitize_filename(filename)
    dot_index = basename.rfind(".")
    stem = basename[:dot_index] if dot_index >= 0 else basename
    stem = "".join(
        "_" if character in invalid_chars else character
        for character in stem
    ).strip(" .")
    prefix = "originals/{}/".format(asset_uuid_text)
    fallback = "image-{}".format(asset_uuid_hex[:12])
    if not usable_stem(stem):
        stem = fallback
    character_limit = 255 - len(prefix) - len(extension)
    byte_limit = 255 - len(extension.encode("utf-8"))
    fitted = []
    byte_count = 0
    for character in stem:
        encoded = character.encode("utf-8")
        if len(fitted) >= character_limit or byte_count + len(encoded) > byte_limit:
            break
        fitted.append(character)
        byte_count += len(encoded)
    stem = "".join(fitted).strip(" .")
    if not usable_stem(stem):
        stem = fallback[:character_limit]
    leaf = "{}{}".format(stem, extension)
    if (
        not usable_stem(stem)
        or len(leaf.encode("utf-8")) > 255
        or len(prefix + leaf) > 255
        or any(
            unicodedata.category(character) == "Cc"
            or character in invalid_chars
            for character in unicodedata.normalize("NFC", leaf)
        )
    ):
        fail()
    return prefix + leaf


def safe_file_sha256(path):
    before = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or before.st_size <= 0:
        fail()
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            fail()
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    current = os.stat(path, follow_symlinks=False)
    identities = (
        before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
    )
    if identities != (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
    ) or identities != (
        current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns,
    ):
        fail()
    return digest.hexdigest()


def safe_read_file(path):
    before = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        fail()
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            fail()
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    current = os.stat(path, follow_symlinks=False)
    identity = (
        before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
    )
    if identity != (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
    ) or identity != (
        current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns,
    ):
        fail()
    return b"".join(chunks)


def safe_media_identity(kind, relative_path, db_width, db_height):
    path = os.path.join(media_root, *relative_path.split("/"))
    try:
        before = os.stat(path, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            fail()
        descriptor = os.open(
            path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino)
                != (before.st_dev, before.st_ino)
                or opened.st_nlink != 1
            ):
                fail()
            digest = hashlib.sha256()
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            os.lseek(descriptor, 0, os.SEEK_SET)
            with warnings.catch_warnings():
                warnings.simplefilter(
                    "ignore", PILImage.DecompressionBombWarning,
                )
                with os.fdopen(os.dup(descriptor), "rb") as file_obj:
                    with PILImage.open(file_obj) as image:
                        image_format = image.format
                        width, height = image.size
                        image.verify()
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        current = os.stat(path, follow_symlinks=False)
    except SystemExit:
        raise
    except Exception:
        fail()
    identity = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_nlink,
        before.st_mtime_ns,
    )
    if (
        identity != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_nlink,
            opened.st_mtime_ns,
        )
        or identity != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_nlink,
            after.st_mtime_ns,
        )
        or identity != (
            current.st_dev,
            current.st_ino,
            current.st_size,
            current.st_nlink,
            current.st_mtime_ns,
        )
        or (width, height) != (db_width, db_height)
        or format_extensions.get(image_format)
        != os.path.splitext(relative_path)[1]
    ):
        fail()
    return [
        kind,
        relative_path,
        opened.st_dev,
        opened.st_ino,
        opened.st_size,
        opened.st_nlink,
        digest.hexdigest(),
        image_format,
        width,
        height,
    ]


try:
    sample = json.loads(sys.stdin.read())
except (TypeError, ValueError):
    fail()
if (
    not isinstance(sample, dict)
    or set(sample) != {
        "image_id", "image_path", "image_sha256", "manifest", "pin_id",
        "pins",
    }
    or type(sample["pin_id"]) is not int
    or sample["pin_id"] <= 0
    or type(sample["image_id"]) is not int
    or sample["image_id"] <= 0
    or not isinstance(sample["image_path"], str)
    or not isinstance(sample["image_sha256"], str)
    or len(sample["image_sha256"]) != 64
    or any(character not in "0123456789abcdef" for character in sample["image_sha256"])
):
    fail()
legacy_hashes = {}
if not isinstance(sample["manifest"], list):
    fail()
for entry in sample["manifest"]:
    if (
        not isinstance(entry, dict)
        or set(entry) != {"kind", "record_id", "sha256"}
        or entry["kind"] not in ("image", "thumbnail")
        or type(entry["record_id"]) is not int
        or entry["record_id"] <= 0
        or not isinstance(entry["sha256"], str)
        or len(entry["sha256"]) != 64
        or any(character not in "0123456789abcdef" for character in entry["sha256"])
        or (entry["kind"], entry["record_id"]) in legacy_hashes
    ):
        fail()
    legacy_hashes[(entry["kind"], entry["record_id"])] = entry["sha256"]
legacy_pins = {}
if not isinstance(sample["pins"], list):
    fail()
for entry in sample["pins"]:
    if (
        not isinstance(entry, dict)
        or set(entry) != {"image_id", "pin_id", "private"}
        or type(entry["pin_id"]) is not int
        or entry["pin_id"] <= 0
        or type(entry["image_id"]) is not int
        or entry["image_id"] <= 0
        or type(entry["private"]) is not bool
        or entry["pin_id"] in legacy_pins
    ):
        fail()
    legacy_pins[entry["pin_id"]] = (
        entry["image_id"], entry["private"],
    )

database_path = os.path.join(data_root, "production.db")
try:
    connection = sqlite3.connect(
        "file:{}?mode=ro".format(database_path), uri=True,
    )
    connection.execute("PRAGMA query_only = ON")
    integrity = connection.execute("PRAGMA integrity_check").fetchall()
    foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    if integrity != [("ok",)] or foreign_keys:
        fail()
    image_rows = connection.execute(
        "SELECT id, image, asset_uuid, original_filename, height, width "
        "FROM django_images_image ORDER BY id"
    ).fetchall()
    thumbnail_rows = connection.execute(
        "SELECT id, image, size, original_id, height, width "
        "FROM django_images_thumbnail ORDER BY id"
    ).fetchall()
    pin_rows = connection.execute(
        "SELECT id, image_id, private FROM core_pin ORDER BY id"
    ).fetchall()
    pin_owner_rows = connection.execute(
        "SELECT image_id, submitter_id FROM core_pin "
        "ORDER BY image_id, submitter_id"
    ).fetchall()
    asset_rows = connection.execute(
        "SELECT id, image_id, submitter_id, content_sha256 "
        "FROM core_mediaasset ORDER BY image_id"
    ).fetchall()
finally:
    try:
        connection.close()
    except (AttributeError, sqlite3.Error):
        pass

images = {}
expected_files = {}
expected_directories = {"originals", "derivatives"}
for row in image_rows:
    if (
        len(row) != 6
        or type(row[0]) is not int
        or row[0] <= 0
        or row[0] in images
        or not isinstance(row[1], str)
        or not isinstance(row[3], str)
        or type(row[4]) is not int
        or row[4] <= 0
        or type(row[5]) is not int
        or row[5] <= 0
    ):
        fail()
    asset_uuid = canonical_uuid(row[2])
    extension = os.path.splitext(row[1])[1]
    expected_path = canonical_original_path(asset_uuid, row[3], extension)
    if row[1] != expected_path or row[1] in expected_files:
        fail()
    images[row[0]] = {
        "asset_uuid": asset_uuid,
        "path": row[1],
        "original_filename": row[3],
        "width": row[5],
        "height": row[4],
    }
    expected_files[row[1]] = ("image", row[0])
    expected_directories.add("originals/{}".format(asset_uuid))

thumbnail_keys = set()
thumbnail_signatures = {image_id: [] for image_id in images}
for row in thumbnail_rows:
    if (
        len(row) != 6
        or type(row[0]) is not int
        or row[0] <= 0
        or not isinstance(row[1], str)
        or row[2] not in derivative_names
        or type(row[3]) is not int
        or row[3] not in images
        or type(row[4]) is not int
        or row[4] <= 0
        or type(row[5]) is not int
        or row[5] <= 0
        or (row[3], row[2]) in thumbnail_keys
    ):
        fail()
    thumbnail_keys.add((row[3], row[2]))
    extension = os.path.splitext(row[1])[1]
    if extension not in allowed_extensions:
        fail()
    asset_uuid = images[row[3]]["asset_uuid"]
    expected_path = "derivatives/{}/{}{}".format(
        asset_uuid, row[2], extension,
    )
    if row[1] != expected_path or row[1] in expected_files:
        fail()
    expected_files[row[1]] = ("thumbnail", row[0])
    expected_directories.add("derivatives/{}".format(asset_uuid))
    thumbnail_signatures[row[3]].append([
        row[0], row[2], row[1], row[5], row[4],
    ])
if thumbnail_keys != {
    (image_id, size)
    for image_id in images
    for size in derivative_names
}:
    fail()

database_signatures = {}
file_rows = {}
for image_id, image in images.items():
    signatures = sorted(
        thumbnail_signatures[image_id],
        key=lambda signature: (signature[1], signature[0]),
    )
    database_signatures[image_id] = [
        image_id,
        image["asset_uuid"],
        image["original_filename"],
        image["path"],
        image["width"],
        image["height"],
        signatures,
    ]
    signatures_by_size = {
        signature[1]: signature for signature in signatures
    }
    file_rows[image_id] = [(
        "original",
        image["path"],
        image["width"],
        image["height"],
    )] + [
        (
            size,
            signatures_by_size[size][2],
            signatures_by_size[size][3],
            signatures_by_size[size][4],
        )
        for size in ("thumbnail", "standard", "square")
    ]
expected_receipt_keys = {
    "original:{}".format(image_id)
    for image_id in images
} | {
    "thumbnail:{}:{}".format(row[3], row[0])
    for row in thumbnail_rows
}

assets = {}
assets_by_pk = {}
for asset_id, image_id, submitter_id, content_sha256 in asset_rows:
    signature = (asset_id, image_id, submitter_id, content_sha256)
    if (
        type(asset_id) is not int
        or asset_id <= 0
        or asset_id in assets_by_pk
        or type(image_id) is not int
        or image_id not in images
        or image_id in assets
        or type(submitter_id) is not int
        or submitter_id <= 0
        or not isinstance(content_sha256, str)
        or len(content_sha256) != 64
        or any(character not in "0123456789abcdef" for character in content_sha256)
    ):
        fail()
    assets[image_id] = signature
    assets_by_pk[asset_id] = signature

owners_by_image = {}
for image_id, submitter_id in pin_owner_rows:
    if (
        type(image_id) is not int
        or image_id not in images
        or type(submitter_id) is not int
        or submitter_id <= 0
    ):
        fail()
    owners_by_image.setdefault(image_id, set()).add(submitter_id)

safe_skip_reasons = frozenset((
    "extra_derivative",
    "file_identity_mismatch",
    "invalid_dimensions",
    "invalid_media_file",
    "invalid_named_leaf",
    "missing_derivative",
    "multi_owner",
    "orphan",
    "pipeline_closure_mismatch",
    "processing_pixel_limit_exceeded",
    "unsafe_media_file",
    "existing_registry_collision",
    "duplicate_registry_collision",
))
preliminary_reasons = frozenset((
    "multi_owner",
    "orphan",
))
structural_reasons = frozenset((
    "extra_derivative",
    "invalid_dimensions",
    "invalid_named_leaf",
    "missing_derivative",
    "multi_owner",
    "orphan",
))


def valid_digest(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


backup_root = os.path.join(data_root, "legacy-backup")
try:
    run_names = []
    for name in sorted(os.listdir(backup_root)):
        path = os.path.join(backup_root, name)
        path_stat = os.stat(path, follow_symlinks=False)
        if not stat.S_ISDIR(path_stat.st_mode):
            fail()
        run_names.append(name)
except OSError:
    fail()
if len(run_names) != 1:
    fail()
run_name = run_names[0]
run_root = os.path.join(backup_root, run_name)
try:
    summary = json.loads(
        safe_read_file(
            os.path.join(run_root, "migration-summary.json")
        ).decode("ascii")
    )
except (OSError, UnicodeError, ValueError):
    fail()
summary_keys = {
    "format_version",
    "source_commit",
    "run_id",
    "phase",
    "backup_relative_name",
    "media_plan_sha256",
    "media_manifest_sha256",
    "backfill_plan_sha256",
    "backfill_manifest_sha256",
    "media_image_count",
    "media_md5_legacy",
    "media_fixed_slot",
    "media_named_canonical",
    "backfill_scanned",
    "backfill_registered",
    "backfill_already_registered",
    "backfill_skipped",
    "reason_counts",
}
count_keys = {
    "media_image_count",
    "media_md5_legacy",
    "media_fixed_slot",
    "media_named_canonical",
    "backfill_scanned",
    "backfill_registered",
    "backfill_already_registered",
    "backfill_skipped",
}
if (
    not isinstance(summary, dict)
    or set(summary) != summary_keys
    or summary["format_version"] != 1
    or summary["run_id"] != run_name
    or summary["phase"] != "complete"
    or summary["backup_relative_name"] != "legacy-backup"
    or any(
        type(summary[key]) is not int or summary[key] < 0
        for key in count_keys
    )
    or not valid_digest(summary["media_plan_sha256"])
    or not valid_digest(summary["media_manifest_sha256"])
    or not valid_digest(summary["backfill_plan_sha256"])
    or not valid_digest(summary["backfill_manifest_sha256"])
    or summary["media_image_count"] != len(images)
    or summary["media_image_count"] != (
        summary["media_md5_legacy"]
        + summary["media_fixed_slot"]
        + summary["media_named_canonical"]
    )
):
    fail()

try:
    manifest_raw = safe_read_file(
        os.path.join(run_root, "media-asset-backfill.jsonl")
    )
except OSError:
    fail()
if (
    not manifest_raw
    or not manifest_raw.endswith(b"\n")
    or hashlib.sha256(manifest_raw).hexdigest()
    != summary["backfill_manifest_sha256"]
):
    fail()

plans = {}
terminals = {}
plan_complete = False
plan_sha256 = None
offset = 0
base_event_keys = {
    "event", "format_version", "target_signature", "run_id",
}
plan_keys = {
    "image_id",
    "submitter_id",
    "content_sha256",
    "database_signature",
    "file_identities",
    "owner_ids",
    "registry_signature",
    "key_registry_signature",
    "preliminary_reason",
}
for raw_line in manifest_raw.splitlines(True):
    try:
        event = json.loads(raw_line.decode("ascii"))
    except (UnicodeError, ValueError):
        fail()
    if (
        not isinstance(event, dict)
        or event.get("format_version") != 2
        or event.get("target_signature") != "media-asset-backfill-v2"
        or event.get("run_id") != run_name
    ):
        fail()
    event_name = event.get("event")
    if event_name == "planned":
        if plan_complete or set(event) != base_event_keys | {"plan"}:
            fail()
        plan = event["plan"]
        if not isinstance(plan, dict) or set(plan) != plan_keys:
            fail()
        image_id = plan["image_id"]
        owner_ids = plan["owner_ids"]
        database_signature = plan["database_signature"]
        if (
            type(image_id) is not int
            or image_id <= 0
            or image_id in plans
            or not isinstance(database_signature, list)
            or database_signature != database_signatures.get(image_id)
            or not isinstance(owner_ids, list)
            or len(owner_ids) > 2
            or owner_ids != sorted(set(owner_ids))
            or any(
                type(owner_id) is not int or owner_id <= 0
                for owner_id in owner_ids
            )
        ):
            fail()
        for signature_name in (
            "registry_signature", "key_registry_signature",
        ):
            signature = plan[signature_name]
            if signature is not None and (
                not isinstance(signature, list)
                or len(signature) != 4
                or any(type(value) is not int for value in signature[:3])
                or not valid_digest(signature[3])
            ):
                fail()
        registry_signature = plan["registry_signature"]
        key_registry_signature = plan["key_registry_signature"]
        preliminary_reason = plan["preliminary_reason"]
        recorded_file_identities = plan["file_identities"]
        if (
            not isinstance(recorded_file_identities, list)
            or len(recorded_file_identities) > 4
            or (
                preliminary_reason is None
                and len(recorded_file_identities) != 4
            )
            or (
                isinstance(preliminary_reason, str)
                and preliminary_reason in structural_reasons
                and recorded_file_identities != []
            )
        ):
            fail()
        actual_file_identities = [
            safe_media_identity(*row)
            for row in file_rows[image_id][
                :len(recorded_file_identities)
            ]
        ]
        if recorded_file_identities != actual_file_identities:
            fail()
        if preliminary_reason is None:
            if (
                type(plan["submitter_id"]) is not int
                or plan["submitter_id"] <= 0
                or owner_ids != [plan["submitter_id"]]
                or not valid_digest(plan["content_sha256"])
                or plan["content_sha256"]
                != actual_file_identities[0][6]
                or (
                    registry_signature is not None
                    and key_registry_signature != registry_signature
                )
                or (
                    registry_signature is not None
                    and registry_signature[1:] != [
                        image_id,
                        plan["submitter_id"],
                        plan["content_sha256"],
                    ]
                )
                or (
                    registry_signature is None
                    and key_registry_signature is not None
                    and tuple(key_registry_signature[2:]) != (
                        plan["submitter_id"], plan["content_sha256"],
                    )
                )
            ):
                fail()
        elif (
            preliminary_reason not in preliminary_reasons
            or plan["submitter_id"] is not None
            or plan["content_sha256"] is not None
            or registry_signature is not None
            or key_registry_signature is not None
            or (
                preliminary_reason == "orphan"
                and owner_ids != []
            )
            or (
                preliminary_reason == "multi_owner"
                and len(owner_ids) != 2
            )
            or (
                preliminary_reason not in ("orphan", "multi_owner")
                and len(owner_ids) != 1
            )
        ):
            fail()
        plans[image_id] = plan
    elif event_name == "plan_complete":
        if (
            plan_complete
            or set(event) != base_event_keys | {"scanned"}
            or event["scanned"] != len(plans)
        ):
            fail()
        plan_complete = True
        plan_sha256 = hashlib.sha256(
            manifest_raw[:offset + len(raw_line)]
        ).hexdigest()
    else:
        if not plan_complete or event_name not in (
            "registered",
            "recovered_registered",
            "already_registered",
            "skipped",
        ):
            fail()
        expected_keys = base_event_keys | {"image_id", "plan_sha256"}
        if event_name == "skipped":
            expected_keys.add("reason_code")
        if (
            set(event) != expected_keys
            or type(event["image_id"]) is not int
            or event["image_id"] not in plans
            or event["image_id"] in terminals
            or event["plan_sha256"] != plan_sha256
        ):
            fail()
        terminals[event["image_id"]] = event
    offset += len(raw_line)

plan_image_ids = tuple(plans)
if (
    not plan_complete
    or set(plans) != set(images)
    or plan_sha256 != summary["backfill_plan_sha256"]
    or set(terminals) != set(plan_image_ids[:len(terminals)])
    or (
        not terminals
        and summary["backfill_manifest_sha256"] != plan_sha256
    )
):
    fail()
for image_id, plan in plans.items():
    if sorted(owners_by_image.get(image_id, set()))[:2] != plan["owner_ids"]:
        fail()

decisions = {}
groups = {}
for image_id, plan in plans.items():
    if plan["preliminary_reason"] is not None:
        decisions[image_id] = plan["preliminary_reason"]
    elif plan["registry_signature"] is not None:
        decisions[image_id] = "already_registered"
    elif plan["key_registry_signature"] is not None:
        decisions[image_id] = "existing_registry_collision"
    else:
        key = plan["submitter_id"], plan["content_sha256"]
        groups.setdefault(key, []).append(image_id)
for image_ids in groups.values():
    decision = (
        "register"
        if len(image_ids) == 1
        else "duplicate_registry_collision"
    )
    for image_id in image_ids:
        decisions[image_id] = decision

expected_asset_ids = set()
reason_counts = {}
for image_id, decision in decisions.items():
    if decision == "register":
        expected_asset_ids.add(image_id)
    elif decision == "already_registered":
        expected_asset_ids.add(image_id)
    else:
        if decision not in safe_skip_reasons:
            fail()
        reason_counts[decision] = reason_counts.get(decision, 0) + 1
    if image_id in terminals:
        terminal = terminals[image_id]
        event_name = terminal["event"]
        if decision == "register":
            if event_name not in ("registered", "recovered_registered"):
                fail()
        elif decision == "already_registered":
            if event_name != "already_registered":
                fail()
        elif (
            event_name != "skipped"
            or terminal.get("reason_code") != decision
        ):
            fail()
if set(assets) != expected_asset_ids:
    fail()
for image_id, decision in decisions.items():
    plan = plans[image_id]
    if decision == "register":
        if assets[image_id][1:] != (
            image_id, plan["submitter_id"], plan["content_sha256"],
        ):
            fail()
    elif decision == "already_registered":
        if assets[image_id] != tuple(plan["registry_signature"]):
            fail()
    elif decision == "existing_registry_collision":
        target_signature = tuple(plan["key_registry_signature"])
        target_image_id = target_signature[1]
        target_plan = plans.get(target_image_id)
        if (
            assets_by_pk.get(target_signature[0]) != target_signature
            or decisions.get(target_image_id) != "already_registered"
            or target_plan is None
            or tuple(target_plan["registry_signature"] or ())
            != target_signature
        ):
            fail()
for image_id in expected_asset_ids:
    if assets[image_id][3] != plans[image_id]["content_sha256"]:
        fail()
expected_summary = {
    "backfill_scanned": len(plans),
    "backfill_registered": sum(
        decision == "register" for decision in decisions.values()
    ),
    "backfill_already_registered": sum(
        decision == "already_registered" for decision in decisions.values()
    ),
    "backfill_skipped": sum(reason_counts.values()),
}
if (
    not isinstance(summary["reason_counts"], dict)
    or any(
        summary[key] != value for key, value in expected_summary.items()
    )
    or summary["reason_counts"] != dict(sorted(reason_counts.items()))
):
    fail()
if len(terminals) < len(plans):
    try:
        journal_raw = safe_read_file(
            os.path.join(run_root, "linear-migration-v1.jsonl")
        )
        journal_state = MigrationBatchJournal._load_state(
            journal_raw, run_name,
        )
    except (OSError, MigrationBatchLogError):
        fail()
    expected_backfill_phase = {
        "scanned": expected_summary["backfill_scanned"],
        "registered": expected_summary["backfill_registered"],
        "already_registered": expected_summary[
            "backfill_already_registered"
        ],
        "skipped": expected_summary["backfill_skipped"],
        "reason_counts": dict(sorted(reason_counts.items())),
    }
    paths_phase = journal_state.phase_summaries.get("paths")
    phase_targets = {
        "paths": len(images),
        "backfill": len(plans),
    }
    phase_batches_valid = True
    upgraded_terminal_ids = set()
    upgraded_receipt_keys = set()
    expected_upgraded_receipt_keys = {
        receipt_key
        for receipt_key in expected_receipt_keys
        if int(receipt_key.split(":")[1]) in terminals
    }
    for phase, image_total in phase_targets.items():
        phase_intents = [
            (batch_id, intent)
            for batch_id, intent in journal_state.intents.items()
            if intent.phase == phase
        ]
        phase_receipts = [
            receipt
            for batch_id, intent in phase_intents
            for receipt in journal_state.receipt_overlays[batch_id]
        ]
        receipt_keys = [receipt.file_key for receipt in phase_receipts]
        if (
            sum(intent.images for _, intent in phase_intents)
            != image_total
            or sum(intent.files for _, intent in phase_intents)
            != len(expected_files)
            or len(receipt_keys) != len(set(receipt_keys))
            or set(receipt_keys) != expected_receipt_keys
        ):
            phase_batches_valid = False
        if phase == "backfill":
            for batch_id, intent in phase_intents:
                if not batch_id.startswith("upgrade-backfill:"):
                    continue
                receipt_image_ids = set()
                for receipt in journal_state.receipt_overlays[batch_id]:
                    upgraded_receipt_keys.add(receipt.file_key)
                    parts = receipt.file_key.split(":")
                    if not (
                        (len(parts) == 2 and parts[0] == "original")
                        or (len(parts) == 3 and parts[0] == "thumbnail")
                    ):
                        phase_batches_valid = False
                        continue
                    try:
                        receipt_image_id = int(parts[1])
                    except ValueError:
                        phase_batches_valid = False
                        continue
                    if receipt_image_id not in plans:
                        phase_batches_valid = False
                        continue
                    receipt_image_ids.add(receipt_image_id)
                if (
                    batch_id != "upgrade-backfill:{}-{}".format(
                        intent.first_pk, intent.last_pk,
                    )
                    or len(receipt_image_ids) != intent.images
                    or min(receipt_image_ids, default=0) != intent.first_pk
                    or max(receipt_image_ids, default=0) != intent.last_pk
                ):
                    phase_batches_valid = False
                upgraded_terminal_ids.update(receipt_image_ids)
    if (
        journal_state.source_bindings != {
            "paths": {
                "plan_sha256": summary["media_plan_sha256"],
                "manifest_sha256": summary["media_manifest_sha256"],
            },
            "backfill": {
                "plan_sha256": plan_sha256,
                "manifest_sha256": summary[
                    "backfill_manifest_sha256"
                ],
            },
        }
        or journal_state.work_totals != {
            "images_total": len(images),
            "files_total": len(expected_files),
            "backfill_total": len(plans),
        }
        or not journal_state.attempts
        or set(journal_state.intents) != journal_state.commits
        or not phase_batches_valid
        or upgraded_terminal_ids != set(terminals)
        or upgraded_receipt_keys != expected_upgraded_receipt_keys
        or set(journal_state.phase_summaries) != {"paths", "backfill"}
        or journal_state.phase_summaries.get("backfill")
        != expected_backfill_phase
        or not isinstance(paths_phase, dict)
        or paths_phase.get("image_count") != summary["media_image_count"]
        or paths_phase.get("md5_legacy") != summary["media_md5_legacy"]
        or paths_phase.get("fixed_slot") != summary["media_fixed_slot"]
        or paths_phase.get("named_canonical")
        != summary["media_named_canonical"]
    ):
        fail()

pins = {}
public_sample = None
for pin_id, image_id, private in pin_rows:
    if (
        type(pin_id) is not int
        or pin_id <= 0
        or pin_id in pins
        or type(image_id) is not int
        or image_id not in images
        or private not in (0, 1, False, True)
    ):
        fail()
    pins[pin_id] = (image_id, bool(private))
    if not bool(private) and public_sample is None:
        public_sample = (pin_id, image_id)
if (
    pins != legacy_pins
    or pins.get(sample["pin_id"], (None, None))[0] != sample["image_id"]
    or sample["image_id"] not in images
):
    fail()

try:
    top_entries = set(os.listdir(media_root))
except OSError:
    fail()
allowed_top = {"originals", "derivatives", ".pinry-locks", ".staging"}
if not top_entries.issubset(allowed_top):
    fail()
for runtime_name in top_entries.intersection({".pinry-locks", ".staging"}):
    runtime_stat = os.stat(
        os.path.join(media_root, runtime_name), follow_symlinks=False,
    )
    if not stat.S_ISDIR(runtime_stat.st_mode):
        fail()

actual_directories = set()
actual_files = {}
for root_name in ("originals", "derivatives"):
    root_path = os.path.join(media_root, root_name)
    root_stat = os.stat(root_path, follow_symlinks=False)
    if not stat.S_ISDIR(root_stat.st_mode):
        fail()
    for current, directories, files in os.walk(root_path, followlinks=False):
        relative_directory = os.path.relpath(current, media_root).replace(os.sep, "/")
        actual_directories.add(relative_directory)
        for directory in directories:
            directory_path = os.path.join(current, directory)
            directory_stat = os.stat(directory_path, follow_symlinks=False)
            if not stat.S_ISDIR(directory_stat.st_mode):
                fail()
        for filename in files:
            path = os.path.join(current, filename)
            relative = os.path.relpath(path, media_root).replace(os.sep, "/")
            if relative in actual_files:
                fail()
            actual_files[relative] = safe_file_sha256(path)
if (
    actual_directories != expected_directories
    or set(actual_files) != set(expected_files)
    or set(legacy_hashes) != set(expected_files.values())
):
    fail()
for relative, record_key in expected_files.items():
    if actual_files.get(relative) != legacy_hashes[record_key]:
        fail()
for image_id, plan in plans.items():
    if (
        plan["preliminary_reason"] is None
        and actual_files.get(images[image_id]["path"])
        != plan["content_sha256"]
    ):
        fail()
for image_id, signature in assets.items():
    if actual_files.get(images[image_id]["path"]) != signature[3]:
        fail()

sample_path = images[sample["image_id"]]["path"]
sample_sha256 = actual_files.get(sample_path)
if sample_sha256 != sample["image_sha256"]:
    fail()
result = {
    "image_id": sample["image_id"],
    "image_path": sample_path,
    "image_sha256": sample_sha256,
    "pin_id": sample["pin_id"],
    "public_image_id": None if public_sample is None else public_sample[1],
    "public_pin_id": None if public_sample is None else public_sample[0],
    "pins": [
        {"image_id": value[0], "pin_id": pin_id, "private": value[1]}
        for pin_id, value in sorted(pins.items())
    ],
    "images": [
        {
            "image_id": image_id,
            "image_path": images[image_id]["path"],
            "image_sha256": actual_files[images[image_id]["path"]],
        }
        for image_id in sorted(images)
    ],
}
print(json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
' 2>/dev/null
    )" || verifier_status="$?"
    if [ "${verifier_status}" -ne 0 ] || [ -z "${verifier_output}" ]; then
        if [ "${verifier_output}" = "nas_canonical_media_invalid" ]; then
            fatal "nas_canonical_media_invalid"
        fi
        fatal "nas_canonical_media_verification_failed"
    fi
    canonical_pin_sample="${verifier_output}"
}

validate_existing_pin_orm() {
    local verifier_output=""
    local verifier_status=0
    [ -f "${clone_data}/local_settings.py" ] \
        && [ ! -L "${clone_data}/local_settings.py" ] \
        || fatal "nas_existing_pin_settings_invalid"
    verifier_output="$(
        printf '%s' "${canonical_pin_sample}" | docker run --rm -i \
            --network none --read-only \
            --mount "${clone_mount_read_only}" \
            --mount "${clone_settings_mount_read_only}" \
            --entrypoint python "${runtime_image}" -c '
_NAS_EXISTING_PIN_ORM = True
import hashlib
import json
import os
import sys


def fail():
    print("nas_existing_pin_orm_invalid")
    raise SystemExit(1)


try:
    sample = json.loads(sys.stdin.read())
except (TypeError, ValueError):
    fail()
if (
    not isinstance(sample, dict)
    or not isinstance(sample.get("pins"), list)
    or not isinstance(sample.get("images"), list)
):
    fail()
os.chdir("/pinry")
sys.path.insert(0, "/pinry")
os.environ["DJANGO_SETTINGS_MODULE"] = "pinry.settings.docker"
try:
    import django
    django.setup()
    from core.models import Pin
    from django.conf import settings
    from django_images.models import Image
    if type(settings.PUBLIC) is not bool:
        fail()
    expected_pins = {
        entry["pin_id"]: (entry["image_id"], entry["private"])
        for entry in sample["pins"]
    }
    expected_images = {
        entry["image_id"]: entry for entry in sample["images"]
    }
    if (
        len(expected_pins) != len(sample["pins"])
        or len(expected_images) != len(sample["images"])
    ):
        fail()
    pins = list(Pin.objects.select_related("image").filter(pk__in=expected_pins))
    if {
        pin.pk: (pin.image_id, bool(pin.private)) for pin in pins
    } != expected_pins:
        fail()
    images = {
        image.pk: image
        for image in Image.objects.filter(pk__in=expected_images)
    }
    if set(images) != set(expected_images):
        fail()
    for image_id, image in images.items():
        expected = expected_images[image_id]
        if image.image.name != expected["image_path"]:
            fail()
        digest = hashlib.sha256()
        with image.image.storage.open(image.image.name, "rb") as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        if digest.hexdigest() != expected["image_sha256"]:
            fail()
except Exception:
    fail()
print("NAS_EXISTING_PIN_ORM_OK:{}".format(
    "true" if settings.PUBLIC else "false",
))
' 2>/dev/null
    )" || verifier_status="$?"
    if [ "${verifier_status}" -ne 0 ]; then
        fatal "nas_existing_pin_orm_failed"
    fi
    local detected_site_public=""
    case "${verifier_output}" in
        NAS_EXISTING_PIN_ORM_OK:true)
            detected_site_public="true"
            ;;
        NAS_EXISTING_PIN_ORM_OK:false)
            detected_site_public="false"
            ;;
        *)
            fatal "nas_existing_pin_orm_failed"
            ;;
    esac
    if [ -n "${existing_pin_site_public}" ] \
        && [ "${existing_pin_site_public}" != "${detected_site_public}" ]; then
        fatal "nas_existing_pin_orm_failed"
    fi
    existing_pin_site_public="${detected_site_public}"
}

validate_existing_pin_http() {
    local verifier_output=""
    local verifier_status=0
    verifier_output="$(
        printf '%s' "${canonical_pin_sample}" | docker run --rm -i \
            --network "${network_name}" \
            --read-only \
            --env "PINRY_SITE_PUBLIC=${existing_pin_site_public}" \
            --entrypoint python "${runtime_image}" -c '
_NAS_EXISTING_MEDIA_HTTP = True
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


def fail():
    print("nas_existing_pin_http_invalid")
    raise SystemExit(1)


def get(path):
    try:
        with urllib.request.urlopen(
            "http://app{}".format(path), timeout=10,
        ) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    except (OSError, urllib.error.URLError, ValueError):
        fail()


try:
    sample = json.loads(sys.stdin.read())
except (TypeError, ValueError):
    fail()
required = {
    "image_id", "image_path", "image_sha256", "pin_id",
    "public_image_id", "public_pin_id", "pins", "images",
}
if not isinstance(sample, dict) or set(sample) != required:
    fail()
site_public_value = os.environ.get("PINRY_SITE_PUBLIC")
if site_public_value not in ("true", "false"):
    fail()
site_public = site_public_value == "true"
path = urllib.parse.quote(sample["image_path"], safe="/")
media_status, payload = get("/media/{}".format(path))
if media_status != 200:
    fail()
if hashlib.sha256(payload).hexdigest() != sample["image_sha256"]:
    fail()

version_status, version_payload = get("/api/v2/version/")
if version_status != 200:
    fail()
try:
    version = json.loads(version_payload.decode("utf-8"))
except (UnicodeError, ValueError):
    fail()
if not isinstance(version, dict):
    fail()

public_pin_id = sample["public_pin_id"]
public_image_id = sample["public_image_id"]
if (public_pin_id is None) != (public_image_id is None):
    fail()
if site_public and public_pin_id is not None:
    if type(public_pin_id) is not int or type(public_image_id) is not int:
        fail()
    detail_status, detail_payload = get(
        "/api/v2/pins/{}/".format(public_pin_id),
    )
    if site_public:
        if detail_status != 200:
            fail()
        try:
            pin = json.loads(detail_payload.decode("utf-8"))
        except (UnicodeError, ValueError):
            fail()
        image = pin.get("image") if isinstance(pin, dict) else None
        if (
            pin.get("id") != public_pin_id
            or not isinstance(image, dict)
            or image.get("id") != public_image_id
        ):
            fail()
if not site_public:
    existing_pin_id = sample["pin_id"]
    if type(existing_pin_id) is not int:
        fail()
    detail_status, _ = get(
        "/api/v2/pins/{}/".format(existing_pin_id),
    )
    list_status, _ = get("/api/v2/pins/")
    boundary_status, _ = get("/api/v2/version/not-allowed/")
    if (
        detail_status != 403
        or list_status != 403
        or boundary_status != 403
    ):
        fail()
print("NAS_EXISTING_MEDIA_HTTP_OK")
' 2>/dev/null
    )" || verifier_status="$?"
    if [ "${verifier_status}" -ne 0 ] \
        || [ "${verifier_output}" != "NAS_EXISTING_MEDIA_HTTP_OK" ]; then
        fatal "nas_existing_pin_http_failed"
    fi
}

if ! initial_fingerprint="$(fingerprint_data "${source_data}")"; then
    fatal "nas_source_fingerprint_failed"
fi
source_fingerprint_ready=1
read -r initial_database_sha initial_inventory_sha initial_media_sha \
    <<<"${initial_fingerprint}"

if ! mkdir -m 0700 -- "${clone_project}" 2>/dev/null; then
    fatal "nas_clone_create_failed"
fi
if ! mkdir -m 0700 -- "${clone_data}" 2>/dev/null; then
    fatal "nas_clone_create_failed"
fi
copy_status=0
rsync -a "${source_data}/" "${clone_data}/" \
    >/dev/null 2>&1 || copy_status=$?
if ! protect_clone_root >/dev/null 2>&1; then
    fatal "nas_clone_permissions_failed"
fi
if [ "${copy_status}" -ne 0 ]; then
    fatal "nas_copy_failed"
fi

if ! copied_fingerprint="$(fingerprint_data "${source_data}")"; then
    source_fingerprint_unchanged="false"
    fatal "nas_source_changed_during_copy"
fi
if [ "${copied_fingerprint}" != "${initial_fingerprint}" ]; then
    source_fingerprint_unchanged="false"
    fatal "nas_source_changed_during_copy"
fi
source_fingerprint_unchanged="true"

[ -d "${clone_data}" ] && [ ! -L "${clone_data}" ] \
    && [ -f "${clone_data}/production.db" ] \
    && [ ! -L "${clone_data}/production.db" ] \
    && [ -d "${clone_data}/static/media" ] \
    && [ ! -L "${clone_data}/static/media" ] \
    || fatal "nas_clone_verification_failed"
if ! clone_fingerprint="$(fingerprint_data "${clone_data}")"; then
    fatal "nas_clone_verification_failed"
fi
read -r clone_database_sha _clone_inventory_sha clone_media_sha \
    <<<"${clone_fingerprint}"
[ "${clone_database_sha}" = "${initial_database_sha}" ] \
    && [ "${clone_media_sha}" = "${initial_media_sha}" ] \
    || fatal "nas_clone_verification_failed"

clone_mount_read_only="type=bind,src=${clone_data},dst=/data,readonly"
clone_mount_read_write="type=bind,src=${clone_data},dst=/data"
clone_settings_mount_read_only="type=bind,src=${clone_data}/local_settings.py,"
clone_settings_mount_read_only+="dst=/pinry/pinry/settings/local_settings.py,readonly"
fixture_mount="type=bind,src=${fixture_script},"
fixture_mount+="dst=/tmp/create_legacy_fixture.py,readonly"

if ! fixture_settings_output="$(
    docker run --rm --network none \
        --mount "${fixture_mount}" \
        --mount "${clone_mount_read_write}" \
        "${runtime_image}" \
        python /tmp/create_legacy_fixture.py configure-settings \
            --data-root /data \
            --allow-host image-source \
            --preserve-existing \
        2>/dev/null
)" || [ "${fixture_settings_output}" != "FIXTURE_SETTINGS_OK" ]; then
    fatal "nas_fixture_settings_failed"
fi

if ! docker run --rm --network none --read-only \
    --mount "${clone_mount_read_only}" \
    --entrypoint python "${runtime_image}" -c '
_NAS_SQLITE_VALIDATE = True
import sqlite3

connection = sqlite3.connect(
    "file:/data/production.db?mode=ro",
    uri=True,
)
try:
    connection.execute("PRAGMA query_only = ON")
    integrity = connection.execute("PRAGMA integrity_check").fetchall()
    foreign_keys = connection.execute(
        "PRAGMA foreign_key_check"
    ).fetchall()
finally:
    connection.close()
if integrity != [("ok",)] or foreign_keys:
    raise SystemExit(1)
' >/dev/null 2>&1; then
    fatal "nas_sqlite_validation_failed"
fi
if ! legacy_database_logical_sha="$(
    logical_database_digest "${clone_data}/production.db"
)" || [[ ! "${legacy_database_logical_sha}" =~ ^[0-9a-f]{64}$ ]]; then
    fatal "nas_legacy_database_digest_failed"
fi

collect_metrics() {
    local phase="$1"

    docker run --rm --network none --read-only \
        --mount "${clone_mount_read_only}" \
        --entrypoint python "${runtime_image}" -c '
_NAS_ACCEPTANCE_METRICS = True
import json
import os
import re
import sqlite3
import stat
import sys

phase = sys.argv[1]
database_uri = "file:/data/production.db?mode=ro"
connection = sqlite3.connect(database_uri, uri=True)
try:
    connection.execute("PRAGMA query_only = ON")
    tables = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = '"'"'table'"'"'"
        ).fetchall()
    }

    def count_table(name):
        if name not in tables:
            return 0
        return connection.execute(
            "SELECT COUNT(*) FROM {}".format(name)
        ).fetchone()[0]

    images = count_table("django_images_image")
    pins = count_table("core_pin")
    planned_files = images + count_table("django_images_thumbnail")
finally:
    connection.close()

files = 0
media_root = "/data/static/media"
for root, directories, names in os.walk(media_root, followlinks=False):
    directories[:] = sorted(
        name for name in directories
        if root != media_root
        or name not in (".pinry-locks", ".staging")
    )
    for name in names:
        file_stat = os.stat(
            os.path.join(root, name),
            follow_symlinks=False,
        )
        if stat.S_ISREG(file_stat.st_mode):
            files += 1

backup_root = "/data/legacy-backup"
backup_count = 0
commits = []
if os.path.isdir(backup_root) and not os.path.islink(backup_root):
    for name in sorted(os.listdir(backup_root)):
        run_path = os.path.join(backup_root, name)
        if not os.path.isdir(run_path) or os.path.islink(run_path):
            continue
        backup_count += 1
        summary_path = os.path.join(run_path, "migration-summary.json")
        try:
            with open(summary_path, encoding="ascii") as stream:
                summary = json.load(stream)
        except (OSError, ValueError):
            continue
        commit = summary.get("source_commit")
        if (
            summary.get("phase") == "complete"
            and isinstance(commit, str)
            and re.fullmatch(
                r"(?:development|[0-9a-f]{40,64})",
                commit,
            )
        ):
            commits.append(commit)

payload = {
    "backup_count": backup_count,
    "backup_present": bool(backup_count),
    "files": files,
    "images": images,
    "pins": pins,
    "planned_files": planned_files,
    "source_commit": commits[-1] if commits else None,
}
print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
' "${phase}"
}

validate_metrics() {
    python3 - "$1" >/dev/null 2>&1 <<'PY'
import json
import re
import sys

value = json.loads(sys.argv[1])
if set(value) != {
    "backup_count",
    "backup_present",
    "files",
    "images",
    "pins",
    "planned_files",
    "source_commit",
}:
    raise SystemExit(1)
for name in (
    "backup_count", "files", "images", "pins", "planned_files",
):
    if type(value[name]) is not int or value[name] < 0:
        raise SystemExit(1)
if type(value["backup_present"]) is not bool:
    raise SystemExit(1)
commit = value["source_commit"]
if commit is not None and (
    not isinstance(commit, str)
    or re.fullmatch(r"(?:development|[0-9a-f]{40,64})", commit) is None
):
    raise SystemExit(1)
PY
}

if ! legacy_metrics="$(collect_metrics before 2>/dev/null)" \
    || ! validate_metrics "${legacy_metrics}"; then
    fatal "nas_legacy_metrics_failed"
fi
legacy_expected_status=0
legacy_expected_values="$(python3 - \
    "${legacy_metrics}" "${expected_pins}" "${expected_files}" \
    "${expected_images}" "${expected_thumbnails}" \
    "${expected_active_files}" \
    2>/dev/null <<'PY'
import json
import sys

value = json.loads(sys.argv[1])
expected_pins = int(sys.argv[2])
expected_files = int(sys.argv[3])
expected_images = int(sys.argv[4])
expected_thumbnails = int(sys.argv[5])
expected_active_files = int(sys.argv[6])
if (
    value["backup_count"] != 0
    or value["backup_present"]
    or value["source_commit"] is not None
):
    raise SystemExit(2)
if (
    value["pins"] != expected_pins
    or value["files"] != expected_files
    or value["images"] != expected_images
    or value["planned_files"] != expected_active_files
    or value["planned_files"] - value["images"] != expected_thumbnails
    or expected_images + expected_thumbnails != expected_active_files
):
    raise SystemExit(1)
print(
    value["pins"],
    value["images"],
    value["files"],
    value["planned_files"],
)
PY
)" || legacy_expected_status="$?"
if [ "${legacy_expected_status}" -ne 0 ]; then
    if [ "${legacy_expected_status}" -eq 2 ]; then
        fatal "nas_legacy_already_migrated"
    fi
    fatal "nas_legacy_workload_mismatch"
fi
read -r legacy_pins legacy_images legacy_files legacy_planned_files \
    <<<"${legacy_expected_values}"
if ! legacy_payload_fingerprint="$(
    payload_fingerprint "${clone_data}/static/media"
)"; then
    fatal "nas_legacy_payload_fingerprint_failed"
fi
read -r legacy_payload_files legacy_payload_sha \
    <<<"${legacy_payload_fingerprint}"
if [[ ! "${legacy_payload_files}" =~ ^[0-9]+$ ]] \
    || [[ ! "${legacy_payload_sha}" =~ ^[0-9a-f]{64}$ ]] \
    || [ "${legacy_payload_files}" != "${legacy_files}" ]; then
    fatal "nas_legacy_payload_fingerprint_failed"
fi
legacy_thumbnails="$((legacy_planned_files - legacy_images))"
if [ "${legacy_thumbnails}" -lt 0 ]; then
    fatal "nas_legacy_metrics_failed"
fi
if ! legacy_pin_sample="$(
    docker run --rm --network none --read-only \
        --mount "${clone_mount_read_only}" \
        --entrypoint python "${runtime_image}" -c '
_NAS_LEGACY_PIN_SAMPLE = True
import hashlib
import json
import os
import sqlite3
import stat

data_root = "/data"
connection = sqlite3.connect(
    "file:/data/production.db?mode=ro", uri=True,
)
try:
    connection.execute("PRAGMA query_only = ON")
    row = connection.execute(
        "SELECT p.id, p.image_id, i.image "
        "FROM core_pin AS p "
        "JOIN django_images_image AS i ON i.id = p.image_id "
        "ORDER BY p.id LIMIT 1"
    ).fetchone()
    image_rows = connection.execute(
        "SELECT id, image FROM django_images_image ORDER BY id"
    ).fetchall()
    thumbnail_rows = connection.execute(
        "SELECT id, image FROM django_images_thumbnail ORDER BY id"
    ).fetchall()
    pin_rows = connection.execute(
        "SELECT id, image_id, private FROM core_pin ORDER BY id"
    ).fetchall()
finally:
    connection.close()
if (
    row is None
    or type(row[0]) is not int
    or type(row[1]) is not int
    or not isinstance(row[2], str)
):
    raise SystemExit(1)
media_root = os.path.join(data_root, "static", "media")


def hash_relative(relative):
    relative = relative.replace("\\", "/")
    parts = relative.split("/")
    if (
        os.path.isabs(relative)
        or not parts
        or any(part in ("", ".", "..") for part in parts)
    ):
        raise SystemExit(1)
    path = os.path.join(media_root, *parts)
    if os.path.commonpath((media_root, os.path.realpath(path))) != media_root:
        raise SystemExit(1)
    before = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or before.st_size <= 0:
        raise SystemExit(1)
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise SystemExit(1)
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        before.st_size,
        before.st_mtime_ns,
        before.st_dev,
        before.st_ino,
    ) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_dev,
        after.st_ino,
    ):
        raise SystemExit(1)
    return relative, digest.hexdigest()


manifest = []
for kind, rows in (("image", image_rows), ("thumbnail", thumbnail_rows)):
    for record_id, relative in rows:
        if type(record_id) is not int or record_id <= 0 or not isinstance(relative, str):
            raise SystemExit(1)
        _normalized, sha256 = hash_relative(relative)
        manifest.append({
            "kind": kind,
            "record_id": record_id,
            "sha256": sha256,
        })
pins = []
for pin_id, image_id, private in pin_rows:
    if (
        type(pin_id) is not int
        or pin_id <= 0
        or type(image_id) is not int
        or image_id <= 0
        or private not in (0, 1, False, True)
    ):
        raise SystemExit(1)
    pins.append({
        "image_id": image_id,
        "pin_id": pin_id,
        "private": bool(private),
    })
relative, sample_sha256 = hash_relative(row[2])
print(json.dumps({
    "image_id": row[1],
    "image_path": relative,
    "image_sha256": sample_sha256,
    "manifest": manifest,
    "pin_id": row[0],
    "pins": pins,
}, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
' 2>/dev/null
)" || [ -z "${legacy_pin_sample}" ]; then
    fatal "nas_legacy_pin_sample_failed"
fi

network_name="svrx-pinry-accept-${run_id}-${random_suffix}"
remote_name="${network_name}-http"
first_app_name="${network_name}-app-a"
second_app_name="${network_name}-app-b"
ownership_label="dev.svrx.pinry.nas_acceptance=${run_id}-${random_suffix}"

if ! network_id_candidate="$(
    docker network create \
        --internal \
        --label "${ownership_label}" \
        "${network_name}" 2>/dev/null
)"; then
    fatal "nas_network_create_failed"
fi
if [[ "${network_id_candidate}" =~ ^[0-9a-f]{64}$ ]]; then
    created_network_id="${network_id_candidate}"
else
    fatal "nas_network_create_failed"
fi
if ! network_internal="$(
    docker network inspect --format '{{.Internal}}' \
        "${created_network_id}" 2>/dev/null
)" || [ "${network_internal}" != "true" ]; then
    fatal "nas_network_isolation_failed"
fi

if ! remote_container_id="$(docker run -d \
    --name "${remote_name}" \
    --label "${ownership_label}" \
    --network "${network_name}" \
    --network-alias image-source \
    --read-only \
    --tmpfs /fixtures:rw,nosuid,nodev,noexec,size=16777216 \
    --mount "${fixture_mount}" \
    "${runtime_image}" /bin/sh -c \
    'python /tmp/create_legacy_fixture.py write-http-fixture \
        --output /fixtures/remote.png >/dev/null \
        && exec python -m http.server 8080 --bind 0.0.0.0 \
        --directory /fixtures >/dev/null 2>&1' \
    2>/dev/null)"; then
    fatal "nas_http_fixture_failed"
fi
if [[ "${remote_container_id}" =~ ^[0-9a-f]{64}$ ]]; then
    remember_container_id "${remote_container_id}"
else
    fatal "nas_http_fixture_failed"
fi
if ! exposed_ports="$(docker port "${remote_container_id}" 2>/dev/null)" \
    || [ -n "${exposed_ports}" ]; then
    fatal "nas_host_port_exposed"
fi
if ! remote_ready="$(
    docker run --rm \
        --network "${network_name}" \
        --read-only \
        --mount "${fixture_mount}" \
        "${runtime_image}" \
        python /tmp/create_legacy_fixture.py wait-http \
            --url http://image-source:8080/remote.png \
            --timeout 30 \
        2>/dev/null
)" || [ "${remote_ready}" != "FIXTURE_HTTP_READY" ]; then
    fatal "nas_http_fixture_not_ready"
fi

start_app() {
    local container_name="$1"
    local failure_code="$2"
    local container_id_candidate=""

    if ! container_id_candidate="$(docker run -d \
        --name "${container_name}" \
        --label "${ownership_label}" \
        --network "${network_name}" \
        --network-alias app \
        --mount "${clone_mount_read_write}" \
        "${runtime_image}" \
        /pinry/docker/scripts/start.sh --migrate-legacy \
        2>/dev/null)"; then
        fatal "${failure_code}"
    fi
    if [[ "${container_id_candidate}" =~ ^[0-9a-f]{64}$ ]]; then
        remember_container_id "${container_id_candidate}"
        started_app_id="${container_id_candidate}"
    else
        fatal "${failure_code}"
    fi
    if ! app_ports="$(docker port "${started_app_id}" 2>/dev/null)" \
        || [ -n "${app_ports}" ]; then
        fatal "nas_host_port_exposed"
    fi
}

run_with_app_watch() {
    local app_id="$1"
    local output_file="$2"
    local command_status=0
    local app_state=""
    shift 2

    [ -z "${watch_pid}" ] || return 91
    "$@" >"${output_file}" 2>&1 &
    watch_pid="$!"
    while kill -0 "${watch_pid}" >/dev/null 2>&1; do
        if ! app_state="$(
            docker inspect --format '{{.State.Running}}' \
                "${app_id}" 2>/dev/null
        )"; then
            stop_watch_process
            return 91
        fi
        case "${app_state}" in
            true)
                ;;
            false)
                stop_watch_process
                return 90
                ;;
            *)
                stop_watch_process
                return 91
                ;;
        esac
        sleep 1
    done

    if wait "${watch_pid}"; then
        command_status=0
    else
        command_status="$?"
    fi
    watch_pid=""
    if ! app_state="$(
        docker inspect --format '{{.State.Running}}' \
            "${app_id}" 2>/dev/null
    )"; then
        return 91
    fi
    case "${app_state}" in
        true)
            return "${command_status}"
            ;;
        false)
            return 90
            ;;
        *)
            return 91
            ;;
    esac
}

wait_for_ready() {
    local deadline_epoch="$1"
    local expected_images="$2"
    local expected_files="$3"
    local app_id="$4"
    local command_status=0
    local wait_output=""

    wait_metrics=""
    if ! watch_output_file="$(
        mktemp "${clone_project}/.acceptance-watch-XXXXXX"
    )"; then
        wait_metrics="WAIT_ERROR:watch_unavailable"
        return 1
    fi
    run_with_app_watch "${app_id}" "${watch_output_file}" \
        docker run --rm \
        --network "${network_name}" \
        --read-only \
        --mount "${fixture_mount}" \
        --entrypoint python "${runtime_image}" -c '
_NAS_WAIT_STATUS = True
import datetime
import importlib.util
import json
import sys
import time
import urllib.error
import urllib.request

deadline = int(sys.argv[1])
expected_images = int(sys.argv[2])
expected_files = int(sys.argv[3])
spec = importlib.util.spec_from_file_location(
    "svrx_pinry_legacy_fixture",
    "/tmp/create_legacy_fixture.py",
)
contract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(contract)
last_heartbeat = None
previous = None
max_gap = 0.0
max_batch = 0
status_observed = False


def fail(reason):
    print("WAIT_ERROR:" + reason)
    raise SystemExit(1)


while time.time() < deadline:
    try:
        with urllib.request.urlopen(
            "http://app/migration-status.json",
            timeout=5,
        ) as response:
            status = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError):
        time.sleep(1)
        continue
    except (UnicodeError, ValueError):
        fail("status_invalid")
    status_observed = True
    state = status.get("state") if isinstance(status, dict) else None
    if state not in contract._MAINTENANCE_STATUS_STATES:
        fail("status_invalid")
    try:
        contract._assert_full_public_status(status, state)
        current = contract._maintenance_progress(status)
        if previous is not None:
            contract._assert_progress_not_regressed(previous, current)
        if state in ("starting_service", "ready"):
            contract._assert_completed_status(
                status, previous, expected_images, expected_files,
            )
    except contract.FixtureError as error:
        if error.code == "maintenance_progress_regressed":
            fail("progress_regressed")
        fail("status_invalid")
    batch = status["last_committed_batch"]
    max_batch = max(max_batch, batch)
    heartbeat = status["heartbeat_at"]
    try:
        parsed = datetime.datetime.strptime(
            heartbeat,
            "%Y-%m-%dT%H:%M:%SZ",
        )
    except ValueError:
        fail("status_invalid")
    if last_heartbeat is not None and parsed > last_heartbeat:
        max_gap = max(
            max_gap,
            (parsed - last_heartbeat).total_seconds(),
        )
    last_heartbeat = parsed
    if state == "failed":
        fail("migration_failed:" + status["error_code"])
    if state == "ready":
        print(json.dumps({
            "batches": max_batch,
            "max_heartbeat_gap_seconds": max_gap,
        }, sort_keys=True, separators=(",", ":")))
        raise SystemExit(0)
    if state not in (
        "starting",
        "recovering",
        "migrating",
        "starting_service",
    ):
        fail("status_invalid")
    previous = current
    time.sleep(1)
if status_observed:
    fail("deadline_exceeded")
fail("status_unavailable")
' "${deadline_epoch}" "${expected_images}" "${expected_files}" \
        || command_status="$?"
    wait_output="$(cat "${watch_output_file}" 2>/dev/null || true)"
    remove_watch_output
    case "${command_status}" in
        90)
            wait_metrics="WAIT_ERROR:app_exited"
            return 1
            ;;
        91)
            wait_metrics="WAIT_ERROR:app_state_unavailable"
            return 1
            ;;
        *)
            wait_metrics="${wait_output}"
            return "${command_status}"
            ;;
    esac
}

validate_wait_metrics() {
    python3 - "$1" 2>/dev/null <<'PY'
import json
import math
import sys

value = json.loads(sys.argv[1])
if set(value) != {"batches", "max_heartbeat_gap_seconds"}:
    raise SystemExit(1)
if type(value["batches"]) is not int or value["batches"] < 0:
    raise SystemExit(1)
gap = value["max_heartbeat_gap_seconds"]
if type(gap) not in (int, float) or gap < 0 or not math.isfinite(gap):
    raise SystemExit(1)
print(value["batches"], gap)
PY
}

map_maintenance_failure_code() {
    local public_error=""

    case "$1" in
        FIXTURE_ERROR:maintenance_http_failed)
            printf '%s\n' "nas_maintenance_http_failed"
            ;;
        FIXTURE_ERROR:maintenance_page_unavailable)
            printf '%s\n' "nas_maintenance_page_unavailable"
            ;;
        FIXTURE_ERROR:maintenance_blocking_unavailable)
            printf '%s\n' "nas_maintenance_blocking_unavailable"
            ;;
        FIXTURE_ERROR:maintenance_status_unavailable)
            printf '%s\n' "nas_maintenance_status_unavailable"
            ;;
        FIXTURE_ERROR:maintenance_page_invalid)
            printf '%s\n' "nas_maintenance_page_invalid"
            ;;
        FIXTURE_ERROR:maintenance_blocking_invalid)
            printf '%s\n' "nas_maintenance_blocking_invalid"
            ;;
        FIXTURE_ERROR:maintenance_status_invalid)
            printf '%s\n' "nas_maintenance_status_invalid"
            ;;
        FIXTURE_ERROR:maintenance_status_private)
            printf '%s\n' "nas_maintenance_status_private"
            ;;
        FIXTURE_ERROR:maintenance_state_not_observed)
            printf '%s\n' "nas_maintenance_state_not_observed"
            ;;
        FIXTURE_ERROR:maintenance_progress_not_observed)
            printf '%s\n' "nas_maintenance_progress_not_observed"
            ;;
        FIXTURE_ERROR:maintenance_progress_regressed)
            printf '%s\n' "nas_maintenance_progress_regressed"
            ;;
        FIXTURE_ERROR:maintenance_failed:*)
            public_error="${1#FIXTURE_ERROR:maintenance_failed:}"
            if [[ "${public_error}" =~ ^[a-z0-9_]{1,128}$ ]]; then
                printf 'nas_migration_%s\n' "${public_error}"
            else
                printf '%s\n' "nas_maintenance_contract_failed"
            fi
            ;;
        *)
            printf '%s\n' "nas_maintenance_contract_failed"
            ;;
    esac
}

map_wait_failure_code() {
    local public_error=""

    case "$1" in
        WAIT_ERROR:status_unavailable)
            printf '%s\n' "nas_app_status_unavailable"
            ;;
        WAIT_ERROR:app_exited)
            printf '%s\n' "nas_app_exited"
            ;;
        WAIT_ERROR:app_state_unavailable|WAIT_ERROR:watch_unavailable)
            printf '%s\n' "nas_app_state_unavailable"
            ;;
        WAIT_ERROR:status_invalid)
            printf '%s\n' "nas_app_status_invalid"
            ;;
        WAIT_ERROR:progress_regressed)
            printf '%s\n' "nas_maintenance_progress_regressed"
            ;;
        WAIT_ERROR:deadline_exceeded)
            printf '%s\n' "nas_app_not_ready"
            ;;
        WAIT_ERROR:migration_failed:*)
            public_error="${1#WAIT_ERROR:migration_failed:}"
            if [[ "${public_error}" =~ ^[a-z0-9_]{1,128}$ ]]; then
                printf 'nas_migration_%s\n' "${public_error}"
            else
                printf '%s\n' "nas_app_status_invalid"
            fi
            ;;
        *)
            printf '%s\n' "nas_app_not_ready"
            ;;
    esac
}

classify_api_contract() {
    local command_status="$1"
    local stdout_path="$2"
    local stderr_path="$3"

    python3 - "${command_status}" "${stdout_path}" "${stderr_path}" \
        2>/dev/null <<'PY'
import os
import stat
import sys

status = int(sys.argv[1])
paths = sys.argv[2:]
payloads = []
for path in paths:
    file_stat = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size > 4096:
        print("nas_api_contract_failed")
        raise SystemExit(0)
    with open(path, "rb") as stream:
        payloads.append(stream.read(4097))
stdout, stderr = payloads
if status == 0 and stdout == b"FIXTURE_API_OK\n" and not stderr:
    print("ok")
    raise SystemExit(0)
allowed = {
    b"FIXTURE_ERROR:api_auth_probe_failed\n": "nas_api_auth_probe_failed",
    b"FIXTURE_ERROR:api_registration_failed\n": "nas_api_registration_failed",
    b"FIXTURE_ERROR:api_local_upload_failed\n": "nas_api_local_upload_failed",
    b"FIXTURE_ERROR:api_duplicate_upload_failed\n": "nas_api_duplicate_upload_failed",
    b"FIXTURE_ERROR:api_remote_upload_failed\n": "nas_api_remote_upload_failed",
    b"FIXTURE_ERROR:api_response_invalid\n": "nas_api_response_invalid",
    b"FIXTURE_ERROR:api_dedup_failed\n": "nas_api_dedup_failed",
    b"FIXTURE_ERROR:api_historical_dedup_failed\n": "nas_api_historical_dedup_failed",
    b"FIXTURE_ERROR:api_asset_closure_invalid\n": "nas_api_asset_closure_invalid",
    b"FIXTURE_ERROR:api_asset_file_empty\n": "nas_api_asset_file_empty",
    b"FIXTURE_ERROR:api_asset_original_mismatch\n": "nas_api_asset_original_mismatch",
    b"FIXTURE_ERROR:api_asset_derivative_invalid\n": "nas_api_asset_derivative_invalid",
    b"FIXTURE_ERROR:api_delete_failed\n": "nas_api_delete_failed",
    b"FIXTURE_ERROR:api_delete_incomplete\n": "nas_api_delete_incomplete",
    b"FIXTURE_ERROR:api_historical_asset_deleted\n": "nas_api_historical_asset_deleted",
    b"FIXTURE_ERROR:api_remote_delete_incomplete\n": "nas_api_remote_delete_incomplete",
    b"FIXTURE_ERROR:api_asset_file_not_removed\n": "nas_api_asset_file_not_removed",
    b"FIXTURE_ERROR:api_asset_directory_not_removed\n": "nas_api_asset_directory_not_removed",
    b"FIXTURE_ERROR:api_asset_count_changed\n": "nas_api_asset_count_changed",
    b"FIXTURE_ERROR:api_staging_leak\n": "nas_api_staging_leak",
    b"FIXTURE_ERROR:api_request_failed\n": "nas_api_request_failed",
    b"FIXTURE_ERROR:migrated_media_changed\n": "nas_migrated_media_changed",
    b"FIXTURE_ERROR:database_read_failed\n": "nas_database_read_failed",
    b"FIXTURE_ERROR:path_escape\n": "nas_path_escape",
    b"FIXTURE_ERROR:unsafe_fixture_file\n": "nas_unsafe_fixture_file",
}
if status != 0 and not stdout and stderr in allowed:
    print(allowed[stderr])
else:
    print("nas_api_contract_failed")
PY
}

start_app "${first_app_name}" "nas_app_start_failed"
first_app_id="${started_app_id}"
migration_deadline_epoch="$(( $(date +%s) + 2400 ))"
maintenance_timeout="$(( migration_deadline_epoch - $(date +%s) ))"
if [ "${maintenance_timeout}" -le 0 ]; then
    fatal "nas_app_not_ready"
fi
if [ "${maintenance_timeout}" -gt 300 ]; then
    maintenance_timeout="300"
fi
maintenance_command_status=0
if ! watch_output_file="$(
    mktemp "${clone_project}/.acceptance-watch-XXXXXX"
)"; then
    fatal "nas_app_state_unavailable"
fi
run_with_app_watch "${first_app_id}" "${watch_output_file}" \
    docker run --rm \
        --network "${network_name}" \
        --read-only \
        --mount "${fixture_mount}" \
        "${runtime_image}" \
        python /tmp/create_legacy_fixture.py assert-maintenance-http \
            --base-url http://app \
            --expected-state migrating \
            --timeout "${maintenance_timeout}" \
            --expected-images "${legacy_images}" \
            --expected-files "${legacy_planned_files}" \
    || maintenance_command_status="$?"
maintenance_output="$(
    cat "${watch_output_file}" 2>/dev/null || true
)"
remove_watch_output
if [ "${maintenance_command_status}" -eq 90 ]; then
    fatal "nas_app_exited"
fi
if [ "${maintenance_command_status}" -eq 91 ]; then
    fatal "nas_app_state_unavailable"
fi
if [ "${maintenance_command_status}" -ne 0 ] \
    || [ "${maintenance_output}" != "FIXTURE_MAINTENANCE_HTTP_OK" ]; then
    fatal "$(map_maintenance_failure_code "${maintenance_output}")"
fi
wait_command_status=0
wait_for_ready \
    "${migration_deadline_epoch}" "${legacy_images}" \
    "${legacy_planned_files}" "${first_app_id}" \
    || wait_command_status="$?"
if [ "${wait_command_status}" -ne 0 ]; then
    fatal "$(map_wait_failure_code "${wait_metrics}")"
fi
if ! wait_values="$(validate_wait_metrics "${wait_metrics}")"; then
    fatal "nas_app_status_invalid"
fi
read -r batches max_heartbeat_gap <<<"${wait_values}"

validate_canonical_media
validate_existing_pin_orm
validate_existing_pin_http

if ! api_stdout_file="$(
    mktemp "${clone_project}/.acceptance-api-stdout-XXXXXX"
)" || ! api_stderr_file="$(
    mktemp "${clone_project}/.acceptance-api-stderr-XXXXXX"
)"; then
    remove_api_output
    fatal "nas_api_contract_failed"
fi
api_command_status=0
docker run --rm \
        --network "${network_name}" \
        --read-only \
        --mount "${fixture_mount}" \
        --mount "${clone_mount_read_only}" \
        "${runtime_image}" \
        python /tmp/create_legacy_fixture.py api-check \
            --mode new \
            --data-root /data \
            --base-url http://app \
            --remote-url http://image-source:8080/remote.png \
            --require-existing-auth \
        >"${api_stdout_file}" 2>"${api_stderr_file}" \
    || api_command_status="$?"
if ! api_contract_result="$(
    classify_api_contract \
        "${api_command_status}" "${api_stdout_file}" "${api_stderr_file}"
)"; then
    api_contract_result="nas_api_contract_failed"
fi
remove_api_output
if [ "${api_contract_result}" != "ok" ]; then
    fatal "${api_contract_result}"
fi
temporary_pin_check="passed"

validate_canonical_media
validate_existing_pin_orm
validate_existing_pin_http

if ! final_metrics="$(collect_metrics after 2>/dev/null)" \
    || ! validate_metrics "${final_metrics}"; then
    fatal "nas_final_metrics_failed"
fi
if ! result_values="$(
    python3 - "${final_metrics}" 2>/dev/null <<'PY'
import json
import sys

value = json.loads(sys.argv[1])
print(
    "true" if value["backup_present"] else "false",
    value["source_commit"] or "",
)
PY
)"; then
    fatal "nas_final_metrics_failed"
fi
read -r legacy_backup_present migration_source_commit \
    <<<"${result_values}"
if ! python3 - \
    "${legacy_metrics}" "${final_metrics}" "${image_source_commit}" \
    >/dev/null 2>&1 <<'PY'
import json
import sys

before = json.loads(sys.argv[1])
after = json.loads(sys.argv[2])
expected_source_commit = sys.argv[3]
for name in ("images", "pins", "planned_files"):
    if before[name] != after[name]:
        raise SystemExit(1)
if (
    after["files"] != before["planned_files"]
    or after["planned_files"] != before["planned_files"]
    or after["backup_count"] != before["backup_count"] + 1
    or not after["backup_present"]
    or after["source_commit"] != expected_source_commit
):
    raise SystemExit(1)
PY
then
    fatal "nas_migration_counts_invalid"
fi
validate_backup_preservation

if ! docker stop --time 30 "${first_app_id}" >/dev/null 2>&1 \
    || ! docker rm -v "${first_app_id}" >/dev/null 2>&1; then
    fatal "nas_app_stop_failed"
fi
if ! before_restart_fingerprint="$(
    fingerprint_clone_logical_data "${clone_data}"
)"; then
    fatal "nas_noop_restart_verification_failed"
fi

restart_count="1"
start_app "${second_app_name}" "nas_app_restart_failed"
second_app_id="${started_app_id}"
restart_deadline_epoch="$(( $(date +%s) + 300 ))"
restart_wait_status=0
wait_for_ready \
    "${restart_deadline_epoch}" "${legacy_images}" \
    "${legacy_planned_files}" "${second_app_id}" \
    || restart_wait_status="$?"
restart_wait="${wait_metrics}"
if [ "${restart_wait_status}" -ne 0 ]; then
    fatal "$(map_wait_failure_code "${restart_wait}")"
fi
if ! validate_wait_metrics "${restart_wait}" >/dev/null; then
    fatal "nas_noop_restart_failed"
fi
if ! noop_metrics_value="$(collect_metrics noop 2>/dev/null)" \
    || ! validate_metrics "${noop_metrics_value}"; then
    fatal "nas_noop_metrics_failed"
fi
if ! python3 - "${final_metrics}" "${noop_metrics_value}" \
    >/dev/null 2>&1 <<'PY'
import json
import sys

before = json.loads(sys.argv[1])
after = json.loads(sys.argv[2])
if before != after:
    raise SystemExit(1)
PY
then
    fatal "nas_noop_restart_changed_data"
fi

if ! docker stop --time 30 "${second_app_id}" >/dev/null 2>&1 \
    || ! docker rm -v "${second_app_id}" >/dev/null 2>&1; then
    fatal "nas_app_stop_failed"
fi
if ! after_restart_fingerprint="$(
    fingerprint_clone_logical_data "${clone_data}"
)"; then
    fatal "nas_noop_restart_verification_failed"
fi
if [ "${before_restart_fingerprint}" != "${after_restart_fingerprint}" ]; then
    fatal "nas_noop_restart_changed_data"
fi
noop_restart="true"

if ! final_source_fingerprint="$(fingerprint_data "${source_data}")" \
    || [ "${final_source_fingerprint}" != "${initial_fingerprint}" ]; then
    source_fingerprint_unchanged="false"
    source_final_check_complete=1
    fatal "nas_source_changed_during_acceptance"
fi
source_fingerprint_unchanged="true"
source_final_check_complete=1

completed_at="$(timestamp)" || fatal "nas_clock_failed"
duration_seconds="${SECONDS}"
if ! publish_result "succeeded" ""; then
    fatal "nas_result_publish_failed"
fi
printf '%s\n' "NAS_LEGACY_CLONE_ACCEPTANCE_OK"
