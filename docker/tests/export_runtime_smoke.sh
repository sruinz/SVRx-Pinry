#!/bin/bash
set -euo pipefail

umask 077
export LC_ALL=C

usage() {
    printf '%s\n' 'usage: export_runtime_smoke.sh IMAGE [PIN_COUNT]' >&2
    exit 2
}

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
    usage
fi

image="$1"
pin_count="${2:-12}"
case "${pin_count}" in
    ''|*[!0-9]*)
        printf '%s\n' 'PIN_COUNT must be a decimal integer' >&2
        exit 2
        ;;
esac
if [ "${#pin_count}" -gt 4 ]; then
    printf '%s\n' 'PIN_COUNT must be between 0 and 1000' >&2
    exit 2
fi
pin_count_value=$((10#${pin_count}))
if [ "${pin_count_value}" -gt 1000 ]; then
    printf '%s\n' 'PIN_COUNT must be between 0 and 1000' >&2
    exit 2
fi

for dependency in docker curl python3; do
    command -v "${dependency}" >/dev/null 2>&1 || {
        printf 'export_smoke_dependency_missing=%s\n' "${dependency}" >&2
        exit 1
    }
done

script_directory="$({
    CDPATH= cd -- "$(dirname -- "$0")" && pwd -P
})"
fixture_script="${script_directory}/fixtures/create_export_fixture.py"
if [ ! -f "${fixture_script}" ] || [ -L "${fixture_script}" ]; then
    printf '%s\n' 'export_smoke_fixture_invalid' >&2
    exit 1
fi
if ! docker info >/dev/null 2>&1; then
    printf '%s\n' 'export_smoke_docker_unavailable' >&2
    exit 1
fi
if ! docker image inspect "${image}" >/dev/null 2>&1; then
    printf '%s\n' 'export_smoke_image_unavailable' >&2
    exit 1
fi

smoke_parent="${TMPDIR:-/tmp}"
smoke_root="$(mktemp -d "${smoke_parent%/}/svrx-pinry-export-smoke.XXXXXX")"
data_root="${smoke_root}/data"
cookie_jar="${smoke_root}/cookies.txt"
fixture_json="${smoke_root}/fixture.json"
request_json="${smoke_root}/request.json"
response_json="${smoke_root}/response.json"
download_headers="${smoke_root}/download.headers"
archive_path="${smoke_root}/export.zip"
second_archive_path="${smoke_root}/export-recreated.zip"
diagnostic_json="${smoke_root}/diagnostic.json"
diagnostic_log="${smoke_root}/container.log"
recovery_before_json="${smoke_root}/recovery-before.json"
name_suffix="$(basename "${smoke_root}" | tr -c 'a-zA-Z0-9_.-' '-')-$$-${RANDOM}"
run_token="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
network_name="svrx-export-${name_suffix}"
first_container="svrx-export-app-a-${name_suffix}"
second_container="svrx-export-app-b-${name_suffix}"
network_id=""
first_container_id=""
second_container_id=""
active_container_id=""
base_url=""
csrf_token=""
diagnosing=0
started_at="$(date +%s)"

mkdir -p -- "${data_root}"
chmod 0700 "${data_root}"
: > "${cookie_jar}"
chmod 0600 "${cookie_jar}"

owned_container() {
    local container_id="$1"
    local identity
    [ -n "${container_id}" ] || return 1
    identity="$(docker container inspect --format \
        '{{.Id}}|{{index .Config.Labels "com.svrx.pinry.export-smoke.run"}}' \
        "${container_id}" 2>/dev/null)" || return 1
    [ "${identity}" = "${container_id}|${run_token}" ]
}

owned_network() {
    local identity
    [ -n "${network_id}" ] || return 1
    identity="$(docker network inspect --format \
        '{{.Id}}|{{index .Labels "com.svrx.pinry.export-smoke.run"}}' \
        "${network_id}" 2>/dev/null)" || return 1
    [ "${identity}" = "${network_id}|${run_token}" ]
}

cleanup() {
    if owned_container "${first_container_id}"; then
        docker rm -f "${first_container_id}" >/dev/null 2>&1 || true
    fi
    if owned_container "${second_container_id}"; then
        docker rm -f "${second_container_id}" >/dev/null 2>&1 || true
    fi
    if owned_network; then
        docker network rm "${network_id}" >/dev/null 2>&1 || true
    fi
    if [ -n "${smoke_root}" ] && [ -d "${smoke_root}" ] \
        && [ ! -L "${smoke_root}" ]; then
        case "${smoke_root}" in
            "${smoke_parent%/}"/svrx-pinry-export-smoke.*)
                rm -rf -- "${smoke_root}"
                ;;
        esac
    fi
}

on_signal() {
    exit 130
}

trap cleanup EXIT
trap on_signal HUP INT TERM

diagnose() {
    [ "${diagnosing}" -eq 0 ] || return
    diagnosing=1
    set +e
    if [ -n "${base_url}" ] && [ -s "${cookie_jar}" ]; then
        curl --silent --show-error --connect-timeout 2 --max-time 5 \
            --cookie "${cookie_jar}" --cookie-jar "${cookie_jar}" \
            --output "${diagnostic_json}" \
            "${base_url}/api/v2/exports/latest/" >/dev/null 2>&1
        if [ -s "${diagnostic_json}" ]; then
            printf '%s\n' 'export_smoke_latest_summary:' >&2
            python3 - "${diagnostic_json}" <<'PY' >&2 2>/dev/null
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    payload = json.load(stream)
summary = {"schema_version": payload.get("schema_version")}
for name in ("latest_attempt", "downloadable_job"):
    value = payload.get(name)
    if isinstance(value, dict):
        error = value.get("error")
        summary[name] = {
            "id": value.get("id"),
            "state": value.get("state"),
            "phase_label": value.get("phase_label"),
            "counters": value.get("counters"),
            "error": ({
                "code": error.get("code"),
                "class": error.get("class"),
                "retryable": error.get("retryable"),
            } if isinstance(error, dict) else None),
        }
    else:
        summary[name] = None
print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
PY
        fi
    fi
    if owned_container "${active_container_id}"; then
        docker logs --tail 200 "${active_container_id}" \
            > "${diagnostic_log}" 2>/dev/null || true
        printf '%s\n' 'export_smoke_container_log_summary:' >&2
        python3 - "${diagnostic_log}" <<'PY' >&2 2>/dev/null
import re
import sys

with open(sys.argv[1], encoding="utf-8", errors="replace") as stream:
    content = stream.read()
blocked = (
    "export-smoke-password", "export-smoke-secret",
    "export-smoke@example.invalid", "export-smoke-zero@example.invalid",
    "내보내기 smoke 설명", "태그-", "csrftoken", "sessionid",
    "authorization", "password=", "token=", "cookie",
)
lowered = content.lower()
canary_detected = any(value.lower() in lowered for value in blocked)
safe = []
for line in content.splitlines():
    lowered_line = line.lower()
    if any(value.lower() in lowered_line for value in blocked):
        continue
    if "http://" in lowered_line or "https://" in lowered_line or "?" in line:
        continue
    if not any(marker in lowered_line for marker in (
        "export", "worker", "ready", "migration", "gunicorn", "nginx",
    )):
        continue
    if not re.fullmatch(r"[\x20-\x7e]{1,240}", line):
        continue
    safe.append(line)
print("lines={} bytes={} canary_detected={}".format(
    len(content.splitlines()), len(content.encode("utf-8")),
    int(canary_detected),
))
for line in safe[-20:]:
    print(line)
PY
        printf '%s\n' 'export_smoke_storage_metadata:' >&2
        docker exec "${active_container_id}" find /data/exports \
            -maxdepth 3 -printf '%M %u:%g %s %p\n' >&2 2>/dev/null
    fi
    set -e
}

fail() {
    printf '%s\n' "$1" >&2
    diagnose
    exit 1
}

http_request() {
    local method="$1"
    local path="$2"
    local output="$3"
    local body="${4:-}"
    local -a command=(
        curl --silent --show-error --connect-timeout 2 --max-time 30
        --request "${method}"
        --cookie "${cookie_jar}" --cookie-jar "${cookie_jar}"
        --header 'Accept: application/json'
        --output "${output}" --write-out '%{http_code}'
    )
    if [ -n "${body}" ]; then
        command+=(
            --header 'Content-Type: application/json'
            --header "X-CSRFToken: ${csrf_token}"
            --data-binary "@${body}"
        )
    fi
    command+=("${base_url}${path}")
    "${command[@]}"
}

csrf_from_cookie_jar() {
    awk '$6 == "csrftoken" { value = $7 } END { print value }' \
        "${cookie_jar}"
}

wait_ready() {
    local timeout="$1"
    local deadline=$(( $(date +%s) + timeout ))
    local code
    while [ "$(date +%s)" -lt "${deadline}" ]; do
        code="$(curl --silent --show-error --connect-timeout 2 --max-time 5 \
            --output /dev/null --write-out '%{http_code}' \
            "${base_url}/readyz" 2>/dev/null || true)"
        if [ "${code}" = "200" ]; then
            return 0
        fi
        sleep 1
    done
    return 1
}

start_app() {
    local container_name="$1"
    local container_role="$2"
    local container_id
    local endpoint
    local port

    container_id="$(docker run -d \
        --name "${container_name}" \
        --label "com.svrx.pinry.export-smoke.run=${run_token}" \
        --label "com.svrx.pinry.export-smoke.role=${container_role}" \
        --network "${network_id}" \
        --mount "type=bind,src=${data_root},dst=/data" \
        --mount "type=bind,src=${fixture_script},dst=/tmp/create_export_fixture.py,readonly" \
        --publish 127.0.0.1::80 \
        "${image}" \
        /pinry/docker/scripts/start.sh --migrate-legacy \
    )" || {
        fail 'export_smoke_container_start_failed'
    }
    if [ "${container_role}" = "first" ]; then
        first_container_id="${container_id}"
    else
        second_container_id="${container_id}"
    fi
    owned_container "${container_id}" \
        || fail 'export_smoke_container_identity_invalid'
    active_container_id="${container_id}"
    endpoint="$(docker port "${container_id}" 80/tcp | head -n 1)" \
        || fail 'export_smoke_port_lookup_failed'
    port="${endpoint##*:}"
    case "${port}" in
        ''|*[!0-9]*) fail 'export_smoke_port_lookup_failed' ;;
    esac
    base_url="http://127.0.0.1:${port}"
    wait_ready 180 || fail 'export_smoke_ready_timeout'
}

initialize_csrf() {
    local code
    code="$(http_request GET '/api/v2/version/' "${response_json}")" \
        || fail 'export_smoke_csrf_probe_failed'
    [ "${code}" = "200" ] || fail 'export_smoke_csrf_probe_failed'
    csrf_token="$(csrf_from_cookie_jar)"
    [ -n "${csrf_token}" ] || fail 'export_smoke_csrf_cookie_missing'
}

write_auth_body() {
    local mode="$1"
    local username="$2"
    local output="$3"
    python3 - "${mode}" "${username}" "${output}" <<'PY'
import json
import os
import sys

mode, username, output = sys.argv[1:]
document = {
    "username": username,
    "password": "export-smoke-password",
}
if mode == "register":
    document.update({
        "email": "export-smoke-zero@example.invalid",
        "password_repeat": "export-smoke-password",
    })
with open(output, "w", encoding="utf-8") as stream:
    json.dump(document, stream, separators=(",", ":"))
os.chmod(output, 0o600)
PY
}

validate_auth_response() {
    local output="$1"
    local expected_username="$2"
    python3 - "${output}" "${expected_username}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    payload = json.load(stream)
if payload.get("username") != sys.argv[2]:
    raise SystemExit("export_smoke_auth_response_invalid")
PY
}

worker_identity() {
    local container_name="$1"
    docker exec "${container_name}" python -c '
import json
import os

candidates = []
for name in os.listdir("/proc"):
    if not name.isdigit():
        continue
    try:
        with open("/proc/{}/cmdline".format(name), "rb") as stream:
            arguments = stream.read().split(b"\0")
    except (IOError, OSError):
        continue
    if len(arguments) > 1 and arguments[1].endswith(b"/export_worker.py"):
        candidates.append(int(name))
if len(candidates) != 1:
    raise SystemExit("export_smoke_worker_process_count_invalid")
pid = candidates[0]
status = {}
with open("/proc/{}/status".format(pid), encoding="ascii") as stream:
    for line in stream:
        key, separator, value = line.partition(":")
        if separator:
            status[key] = value.strip()
uids = status.get("Uid", "").split()
gids = status.get("Gid", "").split()
groups = status.get("Groups", "").split()
print(json.dumps({
    "pid": pid,
    "uids": uids,
    "gids": gids,
    "groups": groups,
}, separators=(",", ":"), sort_keys=True))
'
}

validate_worker_identity() {
    local identity_file="$1"
    python3 - "${identity_file}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    payload = json.load(stream)
if payload.get("pid", 0) <= 1:
    raise SystemExit("export_smoke_worker_pid_invalid")
if payload.get("uids") != ["1000"] * 4:
    raise SystemExit("export_smoke_worker_uid_invalid")
if payload.get("gids") != ["1000"] * 4:
    raise SystemExit("export_smoke_worker_gid_invalid")
if payload.get("groups") != []:
    raise SystemExit("export_smoke_worker_groups_invalid")
PY
}

validate_fixture() {
    python3 - "${fixture_json}" "${pin_count_value}" <<'PY'
import json
import re
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    payload = json.load(stream)
count = int(sys.argv[2])
if set(payload) != {
    "schema_version", "username", "pin_ids", "pins",
    "shared_media_pin_ids",
}:
    raise SystemExit("export_smoke_fixture_keys_invalid")
if payload["schema_version"] != 1 or payload["username"] != "export-smoke-user":
    raise SystemExit("export_smoke_fixture_identity_invalid")
if len(payload["pin_ids"]) != count or len(payload["pins"]) != count:
    raise SystemExit("export_smoke_fixture_count_invalid")
if payload["pin_ids"] != [item.get("id") for item in payload["pins"]]:
    raise SystemExit("export_smoke_fixture_order_invalid")
for item in payload["pins"]:
    if set(item) != {"id", "published_at", "sha256", "size"}:
        raise SystemExit("export_smoke_fixture_item_keys_invalid")
    if type(item["id"]) is not int or type(item["size"]) is not int:
        raise SystemExit("export_smoke_fixture_item_type_invalid")
    if re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) is None:
        raise SystemExit("export_smoke_fixture_hash_invalid")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z", item["published_at"]) is None:
        raise SystemExit("export_smoke_fixture_date_invalid")
if count >= 2:
    shared = payload["shared_media_pin_ids"]
    if shared != payload["pin_ids"][:2]:
        raise SystemExit("export_smoke_fixture_shared_ids_invalid")
    if payload["pins"][0]["sha256"] != payload["pins"][1]["sha256"]:
        raise SystemExit("export_smoke_fixture_shared_hash_invalid")
PY
}

write_pin_request() {
    python3 - "${fixture_json}" "${request_json}" <<'PY'
import json
import os
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    fixture = json.load(stream)
with open(sys.argv[2], "w", encoding="utf-8") as stream:
    json.dump(
        {"scope": "pins", "pin_ids": fixture["pin_ids"]},
        stream,
        separators=(",", ":"),
    )
os.chmod(sys.argv[2], 0o600)
PY
}

latest_state() {
    local code
    code="$(http_request GET '/api/v2/exports/latest/' "${response_json}")" \
        || return 1
    [ "${code}" = "200" ] || return 1
    python3 - "${response_json}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    payload = json.load(stream)
attempt = payload.get("latest_attempt")
if not isinstance(attempt, dict):
    print("none none")
else:
    print("{} {}".format(attempt.get("id", "none"), attempt.get("state", "none")))
PY
}

validate_latest_complete() {
    local job_id="$1"
    python3 - "${response_json}" "${job_id}" "${pin_count_value}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    payload = json.load(stream)
job_id = sys.argv[2]
count = int(sys.argv[3])
if set(payload) != {"schema_version", "latest_attempt", "downloadable_job"}:
    raise SystemExit("export_smoke_latest_schema_invalid")
if payload["schema_version"] != 1:
    raise SystemExit("export_smoke_latest_version_invalid")
status_keys = {
    "schema_version", "id", "state", "phase_label", "scope",
    "phase_percent", "overall_percent", "counters", "created_at",
    "snapshot_at", "heartbeat_at", "completed_at", "expires_at",
    "resume_count", "error", "download_url",
}
counter_keys = {
    "requested_total", "target_total", "snapshot_done", "archive_total",
    "archive_done", "included_total", "excluded_total", "bytes_total",
    "bytes_done",
}
for name in ("latest_attempt", "downloadable_job"):
    status = payload.get(name)
    if not isinstance(status, dict) or set(status) != status_keys:
        raise SystemExit("export_smoke_latest_status_schema_invalid")
    if (
        status["schema_version"] != 1 or status["id"] != job_id
        or status["state"] != "complete" or status["phase_label"] != "완료"
        or status["scope"] != "pins" or status["phase_percent"] != 100.0
        or status["overall_percent"] != 100.0 or status["error"] is not None
        or status["download_url"] != "/api/v2/exports/{}/download/".format(job_id)
        or type(status["resume_count"]) is not int
    ):
        raise SystemExit("export_smoke_latest_complete_invalid")
    counters = status["counters"]
    if not isinstance(counters, dict) or set(counters) != counter_keys:
        raise SystemExit("export_smoke_latest_counters_schema_invalid")
    if (
        counters["requested_total"] != count
        or counters["target_total"] != count
        or counters["snapshot_done"] != count
        or counters["archive_total"] != count
        or counters["archive_done"] != count
        or counters["included_total"] != count
        or counters["excluded_total"] != 0
        or counters["bytes_total"] <= 0
        or counters["bytes_done"] != counters["bytes_total"]
    ):
        raise SystemExit("export_smoke_latest_counts_invalid")
PY
}

wait_for_active_job() {
    local job_id="$1"
    local deadline=$(( $(date +%s) + 60 ))
    local current_id
    local state
    while [ "$(date +%s)" -lt "${deadline}" ]; do
        read -r current_id state <<< "$(latest_state || printf '%s\n' 'none none')"
        if [ "${current_id}" = "${job_id}" ]; then
            case "${state}" in
                archiving|verifying) return 0 ;;
                complete|failed|expired) return 1 ;;
            esac
        fi
        sleep 0.05
    done
    return 1
}

wait_for_new_worker() {
    local old_pid="$1"
    local identity_file="$2"
    local deadline=$(( $(date +%s) + 90 ))
    local pid
    while [ "$(date +%s)" -lt "${deadline}" ]; do
        if worker_identity "${active_container_id}" > "${identity_file}" 2>/dev/null; then
            pid="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["pid"])' "${identity_file}")"
            if [ "${pid}" != "${old_pid}" ]; then
                validate_worker_identity "${identity_file}" || return 1
                return 0
            fi
        fi
        sleep 1
    done
    return 1
}

wait_for_complete() {
    local job_id="$1"
    local timeout=$((180 + pin_count_value * 8))
    local deadline=$(( $(date +%s) + timeout ))
    local current_id
    local state
    while [ "$(date +%s)" -lt "${deadline}" ]; do
        read -r current_id state <<< "$(latest_state || printf '%s\n' 'none none')"
        if [ "${current_id}" = "${job_id}" ]; then
            case "${state}" in
                complete) return 0 ;;
                failed|expired) return 1 ;;
            esac
        fi
        sleep 1
    done
    return 1
}

download_archive() {
    local job_id="$1"
    local destination="$2"
    local headers="$3"
    local code
    code="$(curl --silent --show-error --connect-timeout 2 --max-time 3600 \
        --cookie "${cookie_jar}" --cookie-jar "${cookie_jar}" \
        --dump-header "${headers}" --output "${destination}" \
        --write-out '%{http_code}' \
        "${base_url}/api/v2/exports/${job_id}/download/")" \
        || fail 'export_smoke_download_failed'
    [ "${code}" = "200" ] || fail 'export_smoke_download_status_invalid'
    python3 - "${headers}" <<'PY'
import sys

headers = open(sys.argv[1], encoding="iso-8859-1").read().lower()
if "content-type: application/zip" not in headers:
    raise SystemExit("export_smoke_download_content_type_invalid")
if "content-disposition: attachment;" not in headers:
    raise SystemExit("export_smoke_download_disposition_invalid")
PY
}

validate_archive() {
    local job_id="$1"
    local archive="$2"
    python3 - "${fixture_json}" "${archive}" "${job_id}" <<'PY'
from datetime import datetime, timezone
from xml.etree import ElementTree
import hashlib
import json
import posixpath
import sys
import zipfile

fixture_path, archive_path, job_id = sys.argv[1:]
with open(fixture_path, encoding="utf-8") as stream:
    fixture = json.load(stream)
expected = {item["id"]: item for item in fixture["pins"]}

def dos_time(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0)
    parsed = max(parsed, datetime(1980, 1, 1))
    parsed = min(parsed, datetime(2107, 12, 31, 23, 59, 58))
    parsed = parsed.replace(second=parsed.second - parsed.second % 2)
    return (parsed.year, parsed.month, parsed.day, parsed.hour, parsed.minute, parsed.second)

namespaces = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "exif": "http://ns.adobe.com/exif/1.0/",
    "photoshop": "http://ns.adobe.com/photoshop/1.0/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "digiKam": "http://www.digikam.org/ns/1.0/",
}
with zipfile.ZipFile(archive_path) as archive:
    if archive.testzip() is not None:
        raise SystemExit("export_smoke_zip_crc_invalid")
    infos = archive.infolist()
    names = [info.filename for info in infos]
    if len(names) != len(set(names)):
        raise SystemExit("export_smoke_zip_duplicate_entry")
    for name in names:
        components = name.split("/")
        if (
            name.startswith("/") or "\\" in name
            or posixpath.normpath(name) != name
            or any(component in ("", ".", "..") for component in components)
        ):
            raise SystemExit("export_smoke_zip_path_unsafe")
    if len(names) != 1 + len(expected) * 2 or names.count("manifest.json") != 1:
        raise SystemExit("export_smoke_zip_entry_count_invalid")
    manifest_info = archive.getinfo("manifest.json")
    if manifest_info.compress_type != zipfile.ZIP_DEFLATED:
        raise SystemExit("export_smoke_manifest_compression_invalid")
    manifest_bytes = archive.read(manifest_info)
    manifest_canaries = (
        b"export-smoke-password", b"export-smoke-secret",
        b"export-smoke@example.invalid", b"password=", b"token=",
        b"#fragment", b"csrftoken", b"sessionid",
    )
    if any(value in manifest_bytes for value in manifest_canaries):
        raise SystemExit("export_smoke_manifest_secret_redaction_invalid")
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    if set(manifest) != {
        "schema_version", "export_id", "exported_at", "snapshot_at",
        "scope", "counts", "exclusion_reasons", "items",
    }:
        raise SystemExit("export_smoke_manifest_schema_invalid")
    if manifest.get("schema_version") != 1 or manifest.get("export_id") != job_id:
        raise SystemExit("export_smoke_manifest_identity_invalid")
    if manifest.get("scope") != {"type": "pins", "board": None}:
        raise SystemExit("export_smoke_manifest_scope_invalid")
    count = len(expected)
    if manifest.get("counts") != {
        "requested_total": count,
        "target_total": count,
        "included_total": count,
        "excluded_total": 0,
    }:
        raise SystemExit("export_smoke_manifest_counts_invalid")
    if manifest.get("exclusion_reasons") != {
        "not_visible_at_request": 0,
        "permission_revoked": 0,
    }:
        raise SystemExit("export_smoke_manifest_exclusions_invalid")
    items = manifest.get("items")
    if not isinstance(items, list) or [item.get("pin_id") for item in items] != fixture["pin_ids"]:
        raise SystemExit("export_smoke_manifest_order_invalid")
    for index, item in enumerate(items):
        if set(item) != {
            "pin_id", "owner_username", "is_public", "published_at",
            "description", "tags", "source_url", "source_url_redacted",
            "referer_url", "referer_url_redacted", "original_filename",
            "archive_image_path", "archive_xmp_path", "mime_type", "size",
            "sha256",
        }:
            raise SystemExit("export_smoke_manifest_item_schema_invalid")
        record = expected.get(item.get("pin_id"))
        if record is None:
            raise SystemExit("export_smoke_manifest_pin_invalid")
        expected_description = "내보내기 smoke 설명 {:04d}".format(index)
        expected_tags = ["공통", "태그-{:04d}".format(index)]
        if item.get("owner_username") != fixture["username"] or item.get("is_public") is not False:
            raise SystemExit("export_smoke_manifest_owner_invalid")
        if item.get("published_at") != record["published_at"]:
            raise SystemExit("export_smoke_manifest_date_invalid")
        if item.get("description") != expected_description or item.get("tags") != expected_tags:
            raise SystemExit("export_smoke_manifest_metadata_invalid")
        if item.get("sha256") != record["sha256"] or item.get("size") != record["size"]:
            raise SystemExit("export_smoke_manifest_receipt_invalid")
        if item.get("mime_type") != "image/png":
            raise SystemExit("export_smoke_manifest_mime_invalid")
        expected_source = "https://example.invalid/source/{0}?photo={0}".format(index)
        expected_referer = "https://example.invalid/board/{}".format(index)
        if (
            item.get("source_url") != expected_source
            or item.get("source_url_redacted") is not True
            or item.get("referer_url") != expected_referer
            or item.get("referer_url_redacted") is not True
        ):
            raise SystemExit("export_smoke_manifest_url_redaction_invalid")
        image_name = item.get("archive_image_path")
        xmp_name = item.get("archive_xmp_path")
        if not isinstance(image_name, str) or xmp_name != image_name + ".xmp":
            raise SystemExit("export_smoke_archive_pair_invalid")
        image_info = archive.getinfo(image_name)
        xmp_info = archive.getinfo(xmp_name)
        if image_info.compress_type != zipfile.ZIP_STORED:
            raise SystemExit("export_smoke_original_compression_invalid")
        if xmp_info.compress_type != zipfile.ZIP_DEFLATED:
            raise SystemExit("export_smoke_xmp_compression_invalid")
        if image_info.date_time != dos_time(record["published_at"]) or xmp_info.date_time != dos_time(record["published_at"]):
            raise SystemExit("export_smoke_zip_mtime_invalid")
        original = archive.read(image_info)
        if hashlib.sha256(original).hexdigest() != record["sha256"]:
            raise SystemExit("export_smoke_original_hash_invalid")
        xmp_bytes = archive.read(xmp_info)
        xmp_canaries = manifest_canaries + (
            b"example.invalid", b"export-smoke-user",
        )
        if any(value in xmp_bytes for value in xmp_canaries):
            raise SystemExit("export_smoke_xmp_secret_redaction_invalid")
        root = ElementTree.fromstring(xmp_bytes)
        xmp_tag = "{adobe:ns:meta/}xmpmeta"
        rdf_tag = "{{{}}}RDF".format(namespaces["rdf"])
        description_tag = "{{{}}}Description".format(namespaces["rdf"])
        if root.tag != xmp_tag or root.attrib or len(root) != 1:
            raise SystemExit("export_smoke_xmp_root_invalid")
        rdf = root[0]
        if rdf.tag != rdf_tag or rdf.attrib or len(rdf) != 1:
            raise SystemExit("export_smoke_xmp_rdf_invalid")
        description = rdf[0]
        if description.tag != description_tag:
            raise SystemExit("export_smoke_xmp_structure_invalid")
        expected_attributes = {
            "{{{}}}about".format(namespaces["rdf"]): "",
            "{{{}}}DateTimeOriginal".format(namespaces["exif"]): record["published_at"],
            "{{{}}}DateCreated".format(namespaces["photoshop"]): record["published_at"],
        }
        if description.attrib != expected_attributes:
            raise SystemExit("export_smoke_xmp_date_invalid")
        description_children = list(description)
        expected_child_tags = [
            "{{{}}}description".format(namespaces["dc"]),
            "{{{}}}TagsList".format(namespaces["digiKam"]),
        ]
        if [child.tag for child in description_children] != expected_child_tags:
            raise SystemExit("export_smoke_xmp_children_invalid")
        description_node, tags_node = description_children
        if description_node.attrib or len(description_node) != 1:
            raise SystemExit("export_smoke_xmp_description_invalid")
        alt = description_node[0]
        if alt.tag != "{{{}}}Alt".format(namespaces["rdf"]) or alt.attrib or len(alt) != 1:
            raise SystemExit("export_smoke_xmp_description_invalid")
        text = alt[0]
        if (
            text.tag != "{{{}}}li".format(namespaces["rdf"])
            or text.attrib != {"{http://www.w3.org/XML/1998/namespace}lang": "x-default"}
            or text.text != expected_description or len(text) != 0
        ):
            raise SystemExit("export_smoke_xmp_description_invalid")
        if tags_node.attrib or len(tags_node) != 1:
            raise SystemExit("export_smoke_xmp_tags_invalid")
        sequence = tags_node[0]
        if sequence.tag != "{{{}}}Seq".format(namespaces["rdf"]) or sequence.attrib:
            raise SystemExit("export_smoke_xmp_tags_invalid")
        tags = list(sequence)
        if (
            [tag.tag for tag in tags] != ["{{{}}}li".format(namespaces["rdf"])] * len(expected_tags)
            or any(tag.attrib or len(tag) for tag in tags)
            or [tag.text for tag in tags] != expected_tags
        ):
            raise SystemExit("export_smoke_xmp_tags_invalid")
        if (
            any(node.text is not None or node.tail is not None for node in (
                root, rdf, description, description_node, alt,
                tags_node, sequence,
            ))
            or text.tail is not None
            or any(tag.tail is not None for tag in tags)
        ):
            raise SystemExit("export_smoke_xmp_unexpected_text")
PY
}

network_id="$(docker network create \
    --label "com.svrx.pinry.export-smoke.run=${run_token}" \
    "${network_name}")" || {
    fail 'export_smoke_network_create_failed'
}
owned_network || fail 'export_smoke_network_identity_invalid'
start_app "${first_container}" first
initialize_csrf

auth_body="${smoke_root}/auth.json"
if [ "${pin_count_value}" -eq 0 ]; then
    write_auth_body register 'export-smoke-zero-user' "${auth_body}"
    auth_code="$(http_request POST '/api/v2/users/' "${response_json}" "${auth_body}")" \
        || fail 'export_smoke_registration_failed'
    [ "${auth_code}" = "201" ] || fail 'export_smoke_registration_failed'
    validate_auth_response "${response_json}" 'export-smoke-zero-user' \
        || fail 'export_smoke_registration_response_invalid'
    csrf_token="$(csrf_from_cookie_jar)"
    [ -n "${csrf_token}" ] || fail 'export_smoke_csrf_cookie_missing'
    python3 - "${request_json}" <<'PY'
import json
import os
import sys

with open(sys.argv[1], "w", encoding="utf-8") as stream:
    json.dump({"scope": "pins", "pin_ids": []}, stream, separators=(",", ":"))
os.chmod(sys.argv[1], 0o600)
PY
    for path in '/api/v2/exports/preview/' '/api/v2/exports/'; do
        code="$(http_request POST "${path}" "${response_json}" "${request_json}")" \
            || fail 'export_smoke_zero_request_failed'
        [ "${code}" = "400" ] || fail 'export_smoke_zero_status_invalid'
        python3 - "${response_json}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    payload = json.load(stream)
if payload != {"code": "invalid_target"}:
    raise SystemExit("export_smoke_zero_error_invalid")
PY
    done
    docker exec --user 1000:1000 "${active_container_id}" python -c '
import json
import os
import django
django.setup()
from exports.models import ExportJob, ExportTarget
print(json.dumps({
    "jobs": ExportJob.objects.count(),
    "targets": ExportTarget.objects.count(),
    "staging": os.listdir("/data/exports/.staging"),
    "ready": os.listdir("/data/exports/ready"),
}, separators=(",", ":"), sort_keys=True))
' > "${diagnostic_json}" || fail 'export_smoke_zero_storage_probe_failed'
    python3 - "${diagnostic_json}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    payload = json.load(stream)
if payload != {"jobs": 0, "targets": 0, "staging": [], "ready": []}:
    raise SystemExit("export_smoke_zero_side_effect_detected")
PY
    printf '%s\n' 'EXPORT_RUNTIME_SMOKE_OK pin_count=0 invalid_target=verified'
    exit 0
fi

if ! docker exec --user 1000:1000 "${active_container_id}" \
    python /tmp/create_export_fixture.py "${pin_count_value}" \
    > "${fixture_json}"; then
    fail 'export_smoke_fixture_create_failed'
fi
validate_fixture || fail 'export_smoke_fixture_invalid'
username="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["username"])' "${fixture_json}")"
write_auth_body login "${username}" "${auth_body}"
auth_code="$(http_request POST '/api/v2/users/login/' "${response_json}" "${auth_body}")" \
    || fail 'export_smoke_login_failed'
[ "${auth_code}" = "200" ] || fail 'export_smoke_login_failed'
validate_auth_response "${response_json}" "${username}" \
    || fail 'export_smoke_login_response_invalid'
csrf_token="$(csrf_from_cookie_jar)"
[ -n "${csrf_token}" ] || fail 'export_smoke_csrf_cookie_missing'
write_pin_request

preview_code="$(http_request POST '/api/v2/exports/preview/' "${response_json}" "${request_json}")" \
    || fail 'export_smoke_preview_failed'
[ "${preview_code}" = "200" ] || fail 'export_smoke_preview_status_invalid'
python3 - "${response_json}" "${pin_count_value}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    payload = json.load(stream)
count = int(sys.argv[2])
if set(payload) != {
    "schema_version", "as_of", "scope", "requested_total", "eligible_total",
    "excluded_total", "owned_private_total", "estimated_original_files",
    "estimated_original_bytes", "estimated_zip_bytes",
}:
    raise SystemExit("export_smoke_preview_keys_invalid")
if (
    payload["schema_version"] != 1 or payload["scope"] != "pins"
    or payload["requested_total"] != count or payload["eligible_total"] != count
    or payload["excluded_total"] != 0 or payload["owned_private_total"] != count
):
    raise SystemExit("export_smoke_preview_counts_invalid")
PY

before_identity="${smoke_root}/worker-before.json"
after_identity="${smoke_root}/worker-after.json"
worker_identity "${active_container_id}" > "${before_identity}" \
    || fail 'export_smoke_worker_identity_missing'
validate_worker_identity "${before_identity}" \
    || fail 'export_smoke_worker_identity_invalid'
old_worker_pid="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["pid"])' "${before_identity}")"

create_code="$(http_request POST '/api/v2/exports/' "${response_json}" "${request_json}")" \
    || fail 'export_smoke_create_failed'
[ "${create_code}" = "202" ] || fail 'export_smoke_create_status_invalid'
job_id="$(python3 - "${response_json}" "${pin_count_value}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    payload = json.load(stream)
count = int(sys.argv[2])
if set(payload) != {
    "schema_version", "id", "state", "scope", "requested_total",
    "target_total", "excluded_total", "status_url",
}:
    raise SystemExit("export_smoke_create_keys_invalid")
if (
    payload["schema_version"] != 1 or payload["state"] != "queued"
    or payload["scope"] != "pins" or payload["requested_total"] != count
    or payload["target_total"] != count or payload["excluded_total"] != 0
    or payload["status_url"] != "/api/v2/exports/{}/".format(payload["id"])
):
    raise SystemExit("export_smoke_create_payload_invalid")
print(payload["id"])
PY
)" || fail 'export_smoke_create_payload_invalid'

wait_for_active_job "${job_id}" \
    || fail 'export_smoke_worker_restart_window_missed'
docker exec "${active_container_id}" kill -STOP "${old_worker_pid}" >/dev/null \
    || fail 'export_smoke_worker_stop_failed'
docker exec --user 1000:1000 "${active_container_id}" python -c '
import json
import sys
import django
django.setup()
from exports.models import ExportAttempt, ExportJob, ExportWorkerLease
job = ExportJob.objects.get(pk=sys.argv[1])
worker = ExportWorkerLease.objects.get(pk=1)
if job.state not in ("archiving", "verifying"):
    raise SystemExit("export_smoke_interrupt_state_not_active")
if job.lease_uuid is None or job.worker_generation != worker.generation:
    raise SystemExit("export_smoke_interrupt_lease_invalid")
attempts = list(ExportAttempt.objects.filter(job=job).order_by(
    "attempt_generation",
).values_list("attempt_generation", "state", "relative_path"))
if (
    not attempts or attempts[-1][0] != job.attempt_generation
    or job.snapshot_generation is None
):
    raise SystemExit("export_smoke_interrupt_attempt_invalid")
print(json.dumps({
    "state": job.state,
    "worker_generation": job.worker_generation,
    "attempt_generation": job.attempt_generation,
    "snapshot_generation": (
        str(job.snapshot_generation) if job.snapshot_generation else None
    ),
    "attempts": attempts,
    "snapshot_done": job.snapshot_done,
    "archive_done": job.archive_done,
}, separators=(",", ":"), sort_keys=True))
' "${job_id}" > "${recovery_before_json}" \
    || fail 'export_smoke_interrupt_state_invalid'
wait_ready 15 || fail 'export_smoke_ready_dropped_during_worker_restart'
docker exec "${active_container_id}" sh -c '
kill -TERM "$1"
kill -CONT "$1"
' sh "${old_worker_pid}" >/dev/null \
    || fail 'export_smoke_worker_sigterm_failed'
wait_for_new_worker "${old_worker_pid}" "${after_identity}" \
    || fail 'export_smoke_worker_restart_failed'
wait_ready 15 || fail 'export_smoke_ready_dropped_after_worker_restart'
wait_for_complete "${job_id}" || fail 'export_smoke_completion_failed'

validate_latest_complete "${job_id}" \
    || fail 'export_smoke_latest_complete_invalid'

docker exec --user 1000:1000 "${active_container_id}" python -c '
import json
import sys
import django
django.setup()
from exports.models import ExportAttempt, ExportItem, ExportJob, ExportTarget
job = ExportJob.objects.get(pk=sys.argv[1])
attempts = list(ExportAttempt.objects.filter(job=job).order_by(
    "attempt_generation",
).values_list("attempt_generation", "state", "relative_path"))
print(json.dumps({
    "attempts": attempts,
    "state": job.state,
    "worker_generation": job.worker_generation,
    "attempt_generation": job.attempt_generation,
    "snapshot_generation": (
        str(job.snapshot_generation) if job.snapshot_generation else None
    ),
    "ready_cleanup_state": job.ready_cleanup_state,
    "ready_relative_path": job.ready_relative_path,
    "items": ExportItem.objects.filter(job=job).count(),
    "targets": list(ExportTarget.objects.filter(job=job).order_by("position").values_list("position", flat=True)),
}, separators=(",", ":"), sort_keys=True))
' "${job_id}" > "${diagnostic_json}" || fail 'export_smoke_job_audit_failed'
python3 - "${diagnostic_json}" "${recovery_before_json}" "${pin_count_value}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    payload = json.load(stream)
with open(sys.argv[2], encoding="utf-8") as stream:
    before = json.load(stream)
count = int(sys.argv[3])
generations = [entry[0] for entry in payload.get("attempts", [])]
if not generations or len(generations) != len(set(generations)):
    raise SystemExit("export_smoke_attempt_generation_invalid")
before_attempts = {entry[0]: entry[2] for entry in before.get("attempts", [])}
after_attempts = {entry[0]: entry[2] for entry in payload["attempts"]}
if any(
    after_attempts.get(generation) != path
    for generation, path in before_attempts.items()
):
    raise SystemExit("export_smoke_recovery_tombstone_missing")
if (
    payload.get("state") != "complete"
    or payload.get("worker_generation", 0) <= before.get("worker_generation", 0)
    or payload.get("attempt_generation") != before.get("attempt_generation")
    or payload.get("ready_cleanup_state") != "retained"
    or not payload.get("ready_relative_path")
):
    raise SystemExit("export_smoke_recovery_transition_invalid")
if (
    before.get("snapshot_generation") is not None
    and payload.get("snapshot_generation") != before["snapshot_generation"]
):
    raise SystemExit("export_smoke_recovery_snapshot_changed")
if payload.get("items") != count or payload.get("targets") != list(range(count)):
    raise SystemExit("export_smoke_job_rows_invalid")
PY

download_archive "${job_id}" "${archive_path}" "${download_headers}"
validate_archive "${job_id}" "${archive_path}" \
    || fail 'export_smoke_archive_invalid'

direct_body="${smoke_root}/direct.body"
direct_code="$(curl --silent --show-error --connect-timeout 2 --max-time 10 \
    --output "${direct_body}" --write-out '%{http_code}' \
    "${base_url}/__protected_exports/${job_id}.zip")" \
    || fail 'export_smoke_internal_probe_failed'
[ "${direct_code}" = "404" ] || fail 'export_smoke_internal_uri_exposed'

docker exec "${active_container_id}" sh -c '
set -eu
printf %s symlink-sentinel > /data/export-smoke-symlink-sentinel
ln -s /data/export-smoke-symlink-sentinel /data/exports/ready/export-smoke-symlink.zip
' >/dev/null || fail 'export_smoke_symlink_fixture_failed'
symlink_body="${smoke_root}/symlink.body"
symlink_code="$(curl --silent --show-error --connect-timeout 2 --max-time 10 \
    --output "${symlink_body}" --write-out '%{http_code}' \
    "${base_url}/__protected_exports/export-smoke-symlink.zip")" \
    || fail 'export_smoke_symlink_probe_failed'
case "${symlink_code}" in
    403|404) ;;
    *) fail 'export_smoke_symlink_exposed' ;;
esac
if [ "$(cat "${symlink_body}")" = 'symlink-sentinel' ]; then
    fail 'export_smoke_symlink_bytes_exposed'
fi

archive_sha="$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "${archive_path}")"
archive_size="$(wc -c < "${archive_path}" | tr -d ' ')"
archive_entries=$((1 + pin_count_value * 2))

owned_container "${first_container_id}" \
    || fail 'export_smoke_first_container_identity_changed'
docker rm -f "${first_container_id}" >/dev/null \
    || fail 'export_smoke_first_container_remove_failed'
first_container_id=""
active_container_id=""
start_app "${second_container}" second
latest="$(latest_state)" || fail 'export_smoke_recreated_latest_failed'
read -r recreated_job recreated_state <<< "${latest}"
if [ "${recreated_job}" != "${job_id}" ] || [ "${recreated_state}" != "complete" ]; then
    fail 'export_smoke_recreated_latest_invalid'
fi
validate_latest_complete "${job_id}" \
    || fail 'export_smoke_recreated_latest_schema_invalid'
download_archive "${job_id}" "${second_archive_path}" "${download_headers}"
cmp -s "${archive_path}" "${second_archive_path}" \
    || fail 'export_smoke_recreated_download_changed'
second_sha="$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "${second_archive_path}")"
[ "${second_sha}" = "${archive_sha}" ] \
    || fail 'export_smoke_recreated_hash_changed'
job_resume_count="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["latest_attempt"]["resume_count"])' "${response_json}")"

elapsed_seconds=$(( $(date +%s) - started_at ))
printf '%s\n' \
    "EXPORT_RUNTIME_SMOKE_OK requested=${pin_count_value} included=${pin_count_value} excluded=0 zip_pins=${pin_count_value} zip_entries=${archive_entries} zip_bytes=${archive_size} download_sha256=${archive_sha} worker_restarts=1 job_resume_count=${job_resume_count} elapsed_seconds=${elapsed_seconds}"
