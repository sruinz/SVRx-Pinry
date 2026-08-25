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

stop_and_remove() {
    local container_name="$1"

    docker stop --time 30 "${container_name}" >/dev/null 2>&1 \
        || fail "smoke_app_stop_failed"
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
    local contender_log="${smoke_root}/new-lock.log"
    local exit_code
    local running
    local attempt

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
        --mount "$(fixture_mount)" \
        --mount "$(data_mount "${data_root}")" \
        "${image}" \
        /pinry/docker/scripts/start.sh --migrate-legacy \
        >/dev/null 2>&1; then
        fail "smoke_lock_contender_start_failed"
    fi
    attempt=0
    running="true"
    while [ "${running}" = "true" ] && [ "${attempt}" -lt 60 ]; do
        sleep 1
        if ! running="$(
            docker inspect --format '{{.State.Running}}' \
                "${contender}" 2>/dev/null
        )"; then
            fail "smoke_lock_contender_inspection_failed"
        fi
        attempt=$((attempt + 1))
    done
    [ "${running}" = "false" ] || fail "smoke_lifetime_lock_failed"
    exit_code="$(
        docker inspect --format '{{.State.ExitCode}}' \
            "${contender}" 2>/dev/null
    )" || fail "smoke_lock_contender_inspection_failed"
    [ "${exit_code}" = "1" ] || fail "smoke_lifetime_lock_failed"
    docker logs "${contender}" >"${contender_log}" 2>&1 \
        || fail "smoke_lock_contender_log_failed"
    [ "$(LC_ALL=C wc -l <"${contender_log}")" -eq 1 ] \
        || fail "smoke_lifetime_lock_failed"
    [ "$(tr -d '\r\n' <"${contender_log}")" = "startup_lock_busy" ] \
        || fail "smoke_lifetime_lock_failed"
    assert_container_running "${first}"
    wait_for_app 30
    docker rm "${contender}" >/dev/null 2>&1 \
        || fail "smoke_lock_contender_remove_failed"

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
    local exit_code

    prepare_data_root "${data_root}"
    create_fixture legacy-md5 "${data_root}" 512 "${receipt}"
    start_app "${data_root}" "${first}"

    expect_output \
        "FIXTURE_RESUME_RECORDED" \
        "smoke_resume_copying_not_observed" \
        run_configured_helper "${data_root}" \
            record-resume \
            --data-root /data \
            --output "${observation}" \
            --timeout 180
    assert_container_running "${first}"
    docker kill --signal=STOP "${first}" >/dev/null 2>&1 \
        || fail "smoke_resume_stop_signal_failed"
    assert_container_running "${first}"
    expect_output \
        "FIXTURE_RESUME_RECORDED" \
        "smoke_resume_stopped_state_changed" \
        run_configured_helper "${data_root}" \
            record-resume \
            --data-root /data \
            --output "${observation}" \
            --timeout 1
    docker kill --signal=KILL "${first}" >/dev/null 2>&1 \
        || fail "smoke_resume_kill_failed"
    exit_code="$(docker wait "${first}" 2>/dev/null)" \
        || fail "smoke_resume_wait_failed"
    [ "${exit_code}" = "137" ] || fail "smoke_resume_exit_code_invalid"
    if ! docker start "${first}" >/dev/null 2>&1; then
        fail "smoke_resume_restart_failed"
    fi
    wait_for_app 300
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

command -v docker >/dev/null 2>&1 || fail "smoke_docker_unavailable"
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
docker info >/dev/null 2>&1 || fail "smoke_docker_daemon_unavailable"
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

printf '%s\n' "AUTOMATIC_LEGACY_MIGRATION_SMOKE_OK"
