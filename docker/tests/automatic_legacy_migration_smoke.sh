#!/bin/bash
set -euo pipefail

umask 077
export LC_ALL=C

if [ "$#" -ne 1 ]; then
    printf '%s\n' "smoke_image_argument_required" >&2
    exit 2
fi

image="$1"
script_directory="$(
    CDPATH= cd -- "$(dirname -- "$0")" && pwd -P
)"
fixture_script="${script_directory}/fixtures/create_legacy_fixture.py"
container_fixture_script="/tmp/create_legacy_fixture.py"
temp_base_input="${TMPDIR:-/tmp}"
temp_base=""
smoke_root=""
network_name=""
container_names=""
name_suffix="$$-${RANDOM}"

fail() {
    printf '%s\n' "$1" >&2
    exit 1
}

remember_container() {
    container_names="${container_names} $1"
}

cleanup() {
    local container_name
    local cleanup_root

    for container_name in ${container_names}; do
        docker rm -f "${container_name}" >/dev/null 2>&1 || true
    done
    if [ -n "${network_name}" ]; then
        docker network rm "${network_name}" >/dev/null 2>&1 || true
    fi
    cleanup_root="${smoke_root}"
    if [ -n "${cleanup_root}" ] && [ -d "${cleanup_root}" ] \
        && [ ! -L "${cleanup_root}" ]; then
        case "${cleanup_root}" in
            "${temp_base}"/svrx-pinry-smoke.*)
                rm -rf -- "${cleanup_root}"
                ;;
        esac
    fi
}

on_signal() {
    exit 130
}

trap cleanup EXIT
trap on_signal HUP INT TERM

expect_output() {
    local expected="$1"
    local error_code="$2"
    local output
    shift 2

    if ! output="$("$@" 2>/dev/null)"; then
        fail "${error_code}"
    fi
    if [ "${output}" != "${expected}" ]; then
        fail "${error_code}"
    fi
}

assert_container_running() {
    local container_name="$1"
    local running

    if ! running="$(
        docker inspect --format '{{.State.Running}}' \
            "${container_name}" 2>/dev/null
    )"; then
        fail "smoke_container_not_running"
    fi
    [ "${running}" = "true" ] \
        || fail "smoke_container_not_running"
}

assert_container_stable() {
    local container_name="$1"
    local seconds="${2:-30}"
    local before
    local after
    before="$(docker inspect --format '{{.RestartCount}}' "${container_name}")" \
        || fail "smoke_restart_count_failed"
    sleep "${seconds}"
    assert_container_running "${container_name}"
    after="$(docker inspect --format '{{.RestartCount}}' "${container_name}")" \
        || fail "smoke_restart_count_failed"
    [ "${before}" = "${after}" ] || fail "smoke_restart_loop_detected"
}

assert_no_host_ports() {
    local container_name="$1"
    local ports

    if ! ports="$(docker port "${container_name}" 2>/dev/null)"; then
        fail "smoke_port_inspection_failed"
    fi
    [ -z "${ports}" ] || fail "smoke_host_port_exposed"
}

fixture_mount() {
    printf '%s' \
        "type=bind,src=${fixture_script},"\
"dst=${container_fixture_script},readonly"
}

data_mount() {
    printf '%s' "type=bind,src=$1,dst=/data"
}

settings_mount() {
    printf '%s' \
        "type=bind,src=$1/local_settings.py,"\
"dst=/pinry/pinry/settings/local_settings.py,readonly"
}

prepare_data_root() {
    local data_root="$1"

    mkdir -p -- "${data_root}"
    chmod 0700 "${data_root}"
    expect_output \
        "FIXTURE_SETTINGS_OK" \
        "smoke_fixture_settings_failed" \
        docker run --rm --network none \
            --mount "$(fixture_mount)" \
            --mount "$(data_mount "${data_root}")" \
            "${image}" \
            python "${container_fixture_script}" \
                configure-settings \
                --data-root /data \
                --allow-host image-source
}

run_configured_helper() {
    local data_root="$1"
    shift

    docker run --rm --network none \
        --mount "$(fixture_mount)" \
        --mount "$(data_mount "${data_root}")" \
        --mount "$(settings_mount "${data_root}")" \
        "${image}" \
        python "${container_fixture_script}" "$@"
}

run_read_only_helper() {
    local data_root="$1"
    shift

    docker run --rm --network "${network_name}" \
        --mount "$(fixture_mount)" \
        --mount "type=bind,src=${data_root},dst=/data,readonly" \
        "${image}" \
        python "${container_fixture_script}" "$@"
}

assert_maintenance_http() {
    local data_root="$1"
    local base_url="$2"
    local expected_state="$3"
    local timeout="${4:-30}"
    local minimum_resume_count="${5:-}"
    local expected_error_code="${6:-}"

    if [ -n "${expected_error_code}" ]; then
        expect_output \
            "FIXTURE_MAINTENANCE_HTTP_OK" \
            "smoke_maintenance_http_invalid" \
            run_read_only_helper "${data_root}" \
                assert-maintenance-http \
                --base-url "${base_url}" \
                --expected-state "${expected_state}" \
                --timeout "${timeout}" \
                --expected-error-code "${expected_error_code}"
        return
    fi

    if [ -n "${minimum_resume_count}" ]; then
        expect_output \
            "FIXTURE_MAINTENANCE_HTTP_OK" \
            "smoke_maintenance_http_invalid" \
            run_read_only_helper "${data_root}" \
                assert-maintenance-http \
                --base-url "${base_url}" \
                --expected-state "${expected_state}" \
                --timeout "${timeout}" \
                --min-resume-count "${minimum_resume_count}"
        return
    fi
    expect_output \
        "FIXTURE_MAINTENANCE_HTTP_OK" \
        "smoke_maintenance_http_invalid" \
        run_read_only_helper "${data_root}" \
            assert-maintenance-http \
            --base-url "${base_url}" \
            --expected-state "${expected_state}" \
            --timeout "${timeout}"
}

assert_maintenance_fallback_http() {
    local data_root="$1"
    local base_url="$2"
    local status_mode="$3"

    expect_output \
        "FIXTURE_MAINTENANCE_FALLBACK_HTTP_OK" \
        "smoke_maintenance_fallback_invalid_${status_mode}" \
        run_read_only_helper "${data_root}" \
            assert-maintenance-fallback-http \
            --base-url "${base_url}" \
            --status-mode "${status_mode}" \
            --timeout 30
}

start_app() {
    local data_root="$1"
    local container_name="$2"

    remember_container "${container_name}"
    if ! docker run -d \
        --name "${container_name}" \
        --network "${network_name}" \
        --network-alias app \
        --mount "$(fixture_mount)" \
        --mount "$(data_mount "${data_root}")" \
        --tmpfs /cross:rw,nosuid,nodev,noexec,size=16777216 \
        "${image}" \
        /pinry/docker/scripts/start.sh --migrate-legacy \
        >/dev/null 2>&1; then
        fail "smoke_app_start_failed"
    fi
    assert_no_host_ports "${container_name}"
}

wait_for_app() {
    local timeout="$1"

    expect_output \
        "FIXTURE_HTTP_READY" \
        "smoke_app_not_ready" \
        docker run --rm --network "${network_name}" \
            --mount "$(fixture_mount)" \
            "${image}" \
            python "${container_fixture_script}" \
                wait-http \
                --url http://app/api/v2/version/ \
                --timeout "${timeout}"
}

stop_container() {
    local container_name="$1"
    local exit_code

    docker stop --time 30 "${container_name}" >/dev/null 2>&1 \
        || fail "smoke_app_stop_failed"
    exit_code="$(docker wait "${container_name}")" \
        || fail "smoke_app_wait_failed"
    [ "${exit_code}" = 0 ] || fail "smoke_app_exit_code_invalid"
}

stop_and_remove() {
    local container_name="$1"

    stop_container "${container_name}"
    docker rm "${container_name}" >/dev/null 2>&1 \
        || fail "smoke_app_remove_failed"
}

assert_no_backup() {
    local data_root="$1"

    [ ! -e "${data_root}/legacy-backup" ] \
        && [ ! -L "${data_root}/legacy-backup" ] \
        || fail "smoke_unexpected_backup"
}

assert_runtime() {
    local container_name="$1"

    expect_output \
        "FIXTURE_RUNTIME_OK" \
        "smoke_runtime_contract_failed" \
        docker exec "${container_name}" \
            python "${container_fixture_script}" \
                assert-runtime \
                --data-root /data \
                --project-settings \
                    /pinry/pinry/settings/local_settings.py
}

assert_atomic_archive() {
    local container_name="$1"

    expect_output \
        "FIXTURE_ATOMIC_ARCHIVE_OK" \
        "smoke_atomic_archive_failed" \
        docker exec "${container_name}" \
            python "${container_fixture_script}" \
                verify-atomic-archive \
                --data-root /data \
                --cross-root /cross
}

run_api_check() {
    local mode="$1"
    local data_root="$2"
    local receipt_path="${3:-}"

    if [ "${mode}" = "new" ]; then
        expect_output \
            "FIXTURE_API_OK" \
            "smoke_api_contract_failed" \
            run_read_only_helper "${data_root}" \
                api-check \
                --mode new \
                --data-root /data \
                --base-url http://app \
                --remote-url http://image-source:8080/remote.png
        return
    fi
    expect_output \
        "FIXTURE_API_OK" \
        "smoke_api_contract_failed" \
        run_read_only_helper "${data_root}" \
            api-check \
            --mode migrated \
            --data-root /data \
            --base-url http://app \
            --remote-url http://image-source:8080/remote.png \
            --receipt "${receipt_path}"
}

run_new_mode() {
    local data_root="${smoke_root}/new-data"
    local first="svrx-pinry-smoke-${name_suffix}-new-a"
    local contender="svrx-pinry-smoke-${name_suffix}-new-lock"
    local replacement="svrx-pinry-smoke-${name_suffix}-new-b"

    prepare_data_root "${data_root}"
    chmod 01777 "${data_root}"
    start_app "${data_root}" "${first}"
    wait_for_app 120
    assert_container_running "${first}"
    assert_runtime "${first}"
    assert_atomic_archive "${first}"
    assert_no_backup "${data_root}"
    run_api_check new "${data_root}"

    remember_container "${contender}"
    if ! docker run -d \
        --name "${contender}" \
        --network "${network_name}" \
        --network-alias contender \
        --mount "$(fixture_mount)" \
        --mount "$(data_mount "${data_root}")" \
        "${image}" \
        /pinry/docker/scripts/start.sh --migrate-legacy \
        >/dev/null 2>&1; then
        fail "smoke_lock_contender_start_failed"
    fi
    assert_maintenance_http \
        "${data_root}" http://contender failed 30 "" startup_lock_busy
    assert_container_stable "${contender}" 30
    assert_maintenance_http \
        "${data_root}" http://contender failed 5 "" startup_lock_busy
    assert_container_running "${first}"
    wait_for_app 30
    stop_and_remove "${contender}"

    stop_and_remove "${first}"
    start_app "${data_root}" "${replacement}"
    wait_for_app 120
    assert_no_backup "${data_root}"
    run_api_check new "${data_root}"
    stop_and_remove "${replacement}"
}

create_fixture() {
    local kind="$1"
    local data_root="$2"
    local count="$3"
    local receipt="$4"

    expect_output \
        "FIXTURE_CREATE_OK" \
        "smoke_fixture_create_failed" \
        run_configured_helper "${data_root}" \
            create \
            --kind "${kind}" \
            --data-root /data \
            --count "${count}" \
            --receipt "${receipt}"
}

verify_fixture() {
    local container_name="$1"
    local receipt="$2"

    expect_output \
        "FIXTURE_VERIFY_OK" \
        "smoke_fixture_verify_failed" \
        docker exec "${container_name}" \
            python "${container_fixture_script}" \
                verify-migration \
                --data-root /data \
                --receipt "${receipt}"
}

run_migrated_mode() {
    local kind="$1"
    local label="$2"
    local data_root="${smoke_root}/${label}-data"
    local receipt="/data/fixture-receipt.json"
    local first="svrx-pinry-smoke-${name_suffix}-${label}-a"
    local replacement="svrx-pinry-smoke-${name_suffix}-${label}-b"

    prepare_data_root "${data_root}"
    create_fixture "${kind}" "${data_root}" 3 "${receipt}"
    start_app "${data_root}" "${first}"
    wait_for_app 180
    assert_container_running "${first}"
    assert_runtime "${first}"
    verify_fixture "${first}" "${receipt}"
    run_api_check migrated "${data_root}" "${receipt}"
    stop_and_remove "${first}"

    start_app "${data_root}" "${replacement}"
    wait_for_app 120
    verify_fixture "${replacement}" "${receipt}"
    stop_and_remove "${replacement}"
}

run_resume_mode() {
    local data_root="${smoke_root}/resume-data"
    local receipt="/data/fixture-receipt.json"
    local observation="/data/resume-observation.json"
    local first="svrx-pinry-smoke-${name_suffix}-resume-a"
    local replacement="svrx-pinry-smoke-${name_suffix}-resume-b"
    local source_commit
    local display_version

    prepare_data_root "${data_root}"
    create_fixture legacy-md5 "${data_root}" 512 "${receipt}"
    start_app "${data_root}" "${first}"
    expect_output \
        "FIXTURE_LINEAR_COMMIT_RECORDED" \
        "smoke_resume_linear_commit_not_observed" \
        python3 "${fixture_script}" record-linear-commit \
            --data-root "${data_root}" \
            --output "${data_root}/linear-commit-observation.json" \
            --timeout 180
    assert_container_running "${first}"
    stop_container "${first}"
    expect_output \
        "FIXTURE_RESUME_RECORDED" \
        "smoke_resume_stopped_state_changed" \
        run_configured_helper "${data_root}" \
            record-resume \
            --data-root /data \
            --output "${observation}" \
            --timeout 1
    if ! docker start "${first}" >/dev/null 2>&1; then
        fail "smoke_resume_restart_failed"
    fi
    assert_maintenance_http "${data_root}" http://app migrating 180 1
    wait_for_app 300
    expect_output \
        "FIXTURE_LINEAR_COMMIT_VERIFIED" \
        "smoke_resume_first_commit_rewritten" \
        docker exec "${first}" \
            python "${container_fixture_script}" \
                verify-linear-commit \
                --data-root /data \
                --input /data/linear-commit-observation.json
    source_commit="$(
        docker exec "${first}" printenv PINRY_SOURCE_COMMIT
    )" || fail "smoke_resume_source_commit_missing"
    if [ "${source_commit}" = development ]; then
        display_version=development
    else
        case "${source_commit}" in
            *[!0-9a-f]*|'') fail "smoke_resume_source_commit_invalid" ;;
        esac
        [ "${#source_commit}" -eq 40 ] \
            || fail "smoke_resume_source_commit_invalid"
        display_version="${source_commit:0:12}"
    fi
    expect_output \
        "FIXTURE_VERSION_HTTP_OK" \
        "smoke_resume_version_contract_failed" \
        docker exec "${first}" \
            python "${container_fixture_script}" \
                assert-version-http \
                --url http://127.0.0.1/api/v2/version/ \
                --source-commit "${source_commit}" \
                --display-version "${display_version}"
    expect_output \
        "FIXTURE_RESUME_OK" \
        "smoke_resume_identity_failed" \
        docker exec "${first}" \
            python "${container_fixture_script}" \
                verify-resume \
                --data-root /data \
                --input "${observation}"
    verify_fixture "${first}" "${receipt}"
    stop_and_remove "${first}"

    start_app "${data_root}" "${replacement}"
    wait_for_app 180
    expect_output \
        "FIXTURE_RESUME_OK" \
        "smoke_resume_repeat_backup_failed" \
        docker exec "${replacement}" \
            python "${container_fixture_script}" \
                verify-resume \
                --data-root /data \
                --input "${observation}"
    verify_fixture "${replacement}" "${receipt}"
    stop_and_remove "${replacement}"
}

run_status_resilience_mode() {
    local mode="$1"
    local data_root="$2"
    local container_name="$3"

    if [ "${mode}" = missing ]; then
        docker exec "${container_name}" python -c '
import os
os.unlink("/run/svrx-pinry/migration-status.json")
' >/dev/null 2>&1 || fail "smoke_status_missing_setup_failed"
    elif [ "${mode}" = corrupt ]; then
        docker exec "${container_name}" python -c '
import os
path = "/run/svrx-pinry/migration-status.json"
with open(path, "wb") as stream:
    stream.write(b"{")
    stream.flush()
    os.fsync(stream.fileno())
' >/dev/null 2>&1 || fail "smoke_status_corrupt_setup_failed"
    else
        fail "smoke_status_mode_invalid"
    fi
    assert_container_running "${container_name}"
    assert_maintenance_fallback_http \
        "${data_root}" http://app "${mode}"
    assert_container_stable "${container_name}" 30
}

run_corrupt_manifest_mode() {
    local data_root="${smoke_root}/corrupt-manifest-data"
    local receipt="/data/fixture-receipt.json"
    local migrating="svrx-pinry-smoke-${name_suffix}-corrupt-source"
    local missing="svrx-pinry-smoke-${name_suffix}-status-missing"
    local corrupt="svrx-pinry-smoke-${name_suffix}-status-corrupt"

    prepare_data_root "${data_root}"
    create_fixture legacy-md5 "${data_root}" 512 "${receipt}"
    start_app "${data_root}" "${migrating}"
    assert_maintenance_http "${data_root}" http://app migrating 180
    stop_and_remove "${migrating}"
    expect_output \
        "FIXTURE_MANIFEST_CORRUPTED" \
        "smoke_corrupt_manifest_setup_failed" \
        run_configured_helper "${data_root}" \
            corrupt-migration-manifest --data-root /data

    start_app "${data_root}" "${missing}"
    assert_maintenance_http "${data_root}" http://app failed 60
    docker exec "${missing}" test -f /run/svrx-pinry/maintenance \
        || fail "smoke_corrupt_manifest_marker_missing"
    assert_container_stable "${missing}" 30
    run_status_resilience_mode missing "${data_root}" "${missing}"
    stop_and_remove "${missing}"

    start_app "${data_root}" "${corrupt}"
    assert_maintenance_http "${data_root}" http://app failed 60
    assert_container_stable "${corrupt}" 30
    run_status_resilience_mode corrupt "${data_root}" "${corrupt}"
    stop_and_remove "${corrupt}"
}

if ! command -v docker >/dev/null 2>&1; then
    printf '%s\n' "AUTOMATIC_LEGACY_MIGRATION_SMOKE_SKIP_DOCKER_UNAVAILABLE"
    exit 77
fi
[ -f "${fixture_script}" ] && [ ! -L "${fixture_script}" ] \
    || fail "smoke_fixture_missing"
case "${temp_base_input}" in
    /*) ;;
    *) fail "smoke_temp_base_invalid" ;;
esac
[ -d "${temp_base_input}" ] \
    || fail "smoke_temp_base_invalid"
temp_base="$(
    CDPATH= cd -- "${temp_base_input}" && pwd -P
)" || fail "smoke_temp_base_invalid"
if ! docker info >/dev/null 2>&1; then
    printf '%s\n' "AUTOMATIC_LEGACY_MIGRATION_SMOKE_SKIP_DOCKER_DAEMON_UNAVAILABLE"
    exit 77
fi
docker image inspect "${image}" >/dev/null 2>&1 \
    || fail "smoke_image_unavailable"

smoke_root="$(mktemp -d "${temp_base}/svrx-pinry-smoke.XXXXXX")"
[ -d "${smoke_root}" ] && [ ! -L "${smoke_root}" ] \
    || fail "smoke_temp_root_invalid"
network_name="svrx-pinry-smoke-${name_suffix}"
docker network create "${network_name}" >/dev/null 2>&1 \
    || fail "smoke_network_create_failed"

runtime_container="svrx-pinry-smoke-${name_suffix}-runtime"
remember_container "${runtime_container}"
docker run --rm \
    --name "${runtime_container}" \
    --network none \
    --read-only \
    "${image}" \
    /bin/sh -c '
        grep -qx "VERSION_CODENAME=bookworm" /etc/os-release &&
        python -c "import sys; assert sys.version_info[:2] == (3, 9)"
    ' \
    >/dev/null 2>&1 \
    || fail "smoke_image_runtime_invalid"

fixture_root="${smoke_root}/http-fixture"
mkdir -p -- "${fixture_root}"
chmod 0700 "${fixture_root}"
expect_output \
    "FIXTURE_HTTP_IMAGE_OK" \
    "smoke_http_fixture_failed" \
    docker run --rm --network none \
        --mount "$(fixture_mount)" \
        --mount \
            "type=bind,src=${fixture_root},dst=/fixtures" \
        "${image}" \
        python "${container_fixture_script}" \
            write-http-fixture --output /fixtures/remote.png

http_container="svrx-pinry-smoke-${name_suffix}-http"
remember_container "${http_container}"
docker run -d \
    --name "${http_container}" \
    --network "${network_name}" \
    --network-alias image-source \
    --read-only \
    --mount \
        "type=bind,src=${fixture_root},dst=/fixtures,readonly" \
    "${image}" \
    /bin/sh -c \
        'exec python -m http.server 8080 --bind 0.0.0.0 '\
'--directory /fixtures >/dev/null 2>&1' \
    >/dev/null 2>&1 \
    || fail "smoke_http_server_start_failed"
assert_no_host_ports "${http_container}"
expect_output \
    "FIXTURE_HTTP_READY" \
    "smoke_http_server_not_ready" \
    docker run --rm --network "${network_name}" \
        --mount "$(fixture_mount)" \
        "${image}" \
        python "${container_fixture_script}" \
            wait-http \
            --url http://image-source:8080/remote.png \
            --timeout 30

run_new_mode
run_migrated_mode legacy-md5 legacy-md5
run_migrated_mode transitional-fixed-slot fixed-slot
run_migrated_mode pending-schema pending-schema
run_resume_mode
run_corrupt_manifest_mode

printf '%s\n' "AUTOMATIC_LEGACY_MIGRATION_SMOKE_OK"
