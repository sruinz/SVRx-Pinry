#!/bin/bash
set -euo pipefail

umask 077
export LC_ALL=C

if [ "$#" -ne 1 ]; then
    printf '%s\n' 'usage: export_postgres_concurrency_smoke.sh IMAGE' >&2
    exit 2
fi

image="$1"
postgres_image="postgres:14-alpine"
for dependency in docker python3; do
    command -v "${dependency}" >/dev/null 2>&1 || {
        printf 'export_postgres_dependency_missing=%s\n' "${dependency}" >&2
        exit 1
    }
done
if ! docker info >/dev/null 2>&1; then
    printf '%s\n' 'export_postgres_docker_unavailable' >&2
    exit 1
fi

resolve_image_id() {
    local reference="$1"
    local resolved
    local hash
    resolved="$(docker image inspect --format '{{.Id}}' -- \
        "${reference}" 2>/dev/null)" || return 1
    hash="${resolved#sha256:}"
    [ "${hash}" != "${resolved}" ] && [ "${#hash}" -eq 64 ] \
        || return 1
    case "${hash}" in
        *[!0-9a-f]*) return 1 ;;
    esac
    printf '%s\n' "${resolved}"
}

if ! app_image_id="$(resolve_image_id "${image}")"; then
    printf '%s\n' 'export_postgres_app_image_unavailable' >&2
    exit 1
fi
if ! postgres_image_id="$(resolve_image_id "${postgres_image}")"; then
    if ! docker pull "${postgres_image}" >/dev/null \
        || ! postgres_image_id="$(resolve_image_id "${postgres_image}")"; then
        printf '%s\n' 'export_postgres_database_image_unavailable' >&2
        exit 1
    fi
fi

smoke_parent="${TMPDIR:-/tmp}"
smoke_root="$(mktemp -d "${smoke_parent%/}/svrx-pinry-export-postgres.XXXXXX")"
name_suffix="$(basename "${smoke_root}" | tr -c 'a-zA-Z0-9_.-' '-')-$$-${RANDOM}"
run_token="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
network_name="svrx-export-pg-net-${name_suffix}"
postgres_container="svrx-export-pg-${name_suffix}"
test_container="svrx-export-pg-test-${name_suffix}"
database_name="pinry_export_smoke"
database_user="pinry_export_smoke"
database_password="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
test_log="${smoke_root}/tests.log"
network_id=""
volume_name=""
volume_fingerprint=""
postgres_container_id=""
test_container_id=""

owned_container() {
    local container_id="$1"
    local expected_image_id="$2"
    local identity
    [ -n "${container_id}" ] || return 1
    identity="$(docker container inspect --format \
        '{{.Id}}|{{.Image}}|{{index .Config.Labels "com.svrx.pinry.export-postgres.run"}}' \
        "${container_id}" 2>/dev/null)" || return 1
    [ "${identity}" = \
        "${container_id}|${expected_image_id}|${run_token}" ]
}

owned_network() {
    local identity
    [ -n "${network_id}" ] || return 1
    identity="$(docker network inspect --format \
        '{{.Id}}|{{index .Labels "com.svrx.pinry.export-postgres.run"}}' \
        "${network_id}" 2>/dev/null)" || return 1
    [ "${identity}" = "${network_id}|${run_token}" ]
}

current_volume_fingerprint() {
    local current_name="$1"
    docker volume inspect --format \
        '{{.Name}}|{{.CreatedAt}}|{{.Mountpoint}}' \
        "${current_name}" 2>/dev/null
}

container_exists() {
    docker container inspect "$1" >/dev/null 2>&1
}

network_exists() {
    docker network inspect "$1" >/dev/null 2>&1
}

volume_exists() {
    docker volume inspect "$1" >/dev/null 2>&1
}

remove_owned_container() {
    local container_id="$1"
    local expected_image_id="$2"
    local remove_volumes="$3"
    if ! container_exists "${container_id}"; then
        if [ "${remove_volumes}" = "yes" ] && [ -n "${volume_name}" ] \
            && volume_exists "${volume_name}"; then
            printf 'export_postgres_anonymous_volume_leaked=%s\n' \
                "${volume_name}" >&2
            current_volume_fingerprint "${volume_name}" >&2 || true
            return 1
        fi
        return 0
    fi
    if ! owned_container "${container_id}" "${expected_image_id}"; then
        printf 'export_postgres_container_identity_changed=%s\n' \
            "${container_id}" >&2
        return 1
    fi
    if [ "${remove_volumes}" = "yes" ]; then
        if ! docker rm -f -v "${container_id}" >/dev/null 2>&1; then
            printf 'export_postgres_container_cleanup_failed=%s\n' \
                "${container_id}" >&2
            return 1
        fi
    elif [ "${remove_volumes}" = "no" ]; then
        if ! docker rm -f "${container_id}" >/dev/null 2>&1; then
            printf 'export_postgres_container_cleanup_failed=%s\n' \
                "${container_id}" >&2
            return 1
        fi
    else
        printf 'export_postgres_volume_cleanup_mode_invalid=%s\n' \
            "${remove_volumes}" >&2
        return 1
    fi
    if container_exists "${container_id}"; then
        printf 'export_postgres_container_leaked=%s\n' \
            "${container_id}" >&2
        return 1
    fi
    if [ "${remove_volumes}" = "yes" ] && [ -n "${volume_name}" ] \
        && volume_exists "${volume_name}"; then
        printf 'export_postgres_anonymous_volume_leaked=%s\n' \
            "${volume_name}" >&2
        current_volume_fingerprint "${volume_name}" >&2 || true
        return 1
    fi
}

remove_owned_network() {
    if ! network_exists "${network_id}"; then
        return 0
    fi
    if ! owned_network; then
        printf 'export_postgres_network_identity_changed=%s\n' \
            "${network_id}" >&2
        return 1
    fi
    if ! docker network rm "${network_id}" >/dev/null 2>&1; then
        printf 'export_postgres_network_cleanup_failed=%s\n' \
            "${network_id}" >&2
        return 1
    fi
    if network_exists "${network_id}"; then
        printf 'export_postgres_network_leaked=%s\n' "${network_id}" >&2
        return 1
    fi
}

cleanup() {
    local status=0
    if [ -n "${test_container_id}" ]; then
        if remove_owned_container \
            "${test_container_id}" "${app_image_id}" no; then
            test_container_id=""
        else
            status=1
        fi
    fi
    if [ -n "${postgres_container_id}" ]; then
        if remove_owned_container \
            "${postgres_container_id}" "${postgres_image_id}" yes; then
            postgres_container_id=""
        else
            status=1
        fi
    fi
    if [ -n "${network_id}" ]; then
        if remove_owned_network; then
            network_id=""
        else
            status=1
        fi
    fi
    if [ "${status}" -eq 0 ] && [ -n "${smoke_root}" ] \
        && { [ -e "${smoke_root}" ] || [ -L "${smoke_root}" ]; }; then
        if [ ! -d "${smoke_root}" ] || [ -L "${smoke_root}" ]; then
            printf 'export_postgres_host_cleanup_scope_invalid=%s\n' \
                "${smoke_root}" >&2
            status=1
        else
            case "${smoke_root}" in
                "${smoke_parent%/}"/svrx-pinry-export-postgres.*)
                    if ! rm -rf -- "${smoke_root}" \
                        || [ -e "${smoke_root}" ] \
                        || [ -L "${smoke_root}" ]; then
                        printf 'export_postgres_host_cleanup_failed=%s\n' \
                            "${smoke_root}" >&2
                        status=1
                    fi
                    ;;
                *)
                    printf 'export_postgres_host_cleanup_scope_invalid=%s\n' \
                        "${smoke_root}" >&2
                    status=1
                    ;;
            esac
        fi
    fi
    return "${status}"
}

cleanup_on_exit() {
    local original_status="$?"
    trap - EXIT
    if ! cleanup && [ "${original_status}" -eq 0 ]; then
        original_status=1
    fi
    exit "${original_status}"
}

finish_success() {
    local message="$1"
    if ! cleanup; then
        printf '%s\n' 'export_postgres_cleanup_failed' >&2
        return 1
    fi
    trap - EXIT HUP INT TERM
    printf '%s\n' "${message}"
}

on_signal() {
    exit 130
}

trap cleanup_on_exit EXIT
trap on_signal HUP INT TERM

fail() {
    printf '%s\n' "$1" >&2
    if owned_container "${postgres_container_id}" "${postgres_image_id}"; then
        printf '%s\n' 'export_postgres_container_log:' >&2
        docker logs --tail 200 "${postgres_container_id}" 2>/dev/null \
            | DATABASE_PASSWORD="${database_password}" python3 -c '
import os
import sys
secret = os.environ["DATABASE_PASSWORD"]
sys.stdout.write(sys.stdin.read().replace(secret, "[REDACTED]"))
' >&2 || true
    fi
    exit 1
}

network_id="$(docker network create \
    --label "com.svrx.pinry.export-postgres.run=${run_token}" \
    "${network_name}")" || fail 'export_postgres_network_create_failed'
owned_network || fail 'export_postgres_network_identity_invalid'

postgres_container_id="$(docker run -d \
    --name "${postgres_container}" \
    --label "com.svrx.pinry.export-postgres.run=${run_token}" \
    --label 'com.svrx.pinry.export-postgres.role=database' \
    --network "${network_id}" \
    --network-alias export-postgres \
    --mount 'type=volume,dst=/var/lib/postgresql/data' \
    --env "POSTGRES_DB=${database_name}" \
    --env "POSTGRES_USER=${database_user}" \
    --env "POSTGRES_PASSWORD=${database_password}" \
    "${postgres_image_id}")" || {
    fail 'export_postgres_container_start_failed'
}
owned_container "${postgres_container_id}" "${postgres_image_id}" \
    || fail 'export_postgres_container_identity_invalid'
volume_name="$(docker container inspect --format \
    '{{range .Mounts}}{{if eq .Destination "/var/lib/postgresql/data"}}{{.Name}}{{end}}{{end}}' \
    "${postgres_container_id}")" \
    || fail 'export_postgres_volume_identity_missing'
[ -n "${volume_name}" ] \
    || fail 'export_postgres_volume_identity_missing'
volume_mount_identity="$(docker container inspect --format \
    '{{range .Mounts}}{{if eq .Destination "/var/lib/postgresql/data"}}{{.Type}}|{{.Name}}{{end}}{{end}}' \
    "${postgres_container_id}")" \
    || fail 'export_postgres_volume_identity_missing'
[ "${volume_mount_identity}" = "volume|${volume_name}" ] \
    || fail 'export_postgres_volume_identity_invalid'
volume_fingerprint="$(current_volume_fingerprint "${volume_name}")" \
    || fail 'export_postgres_volume_identity_missing'
[ "${volume_fingerprint%%|*}" = "${volume_name}" ] \
    || fail 'export_postgres_volume_identity_invalid'

ready_deadline=$(( $(date +%s) + 90 ))
while ! docker exec "${postgres_container_id}" pg_isready \
    --username "${database_user}" --dbname "${database_name}" \
    >/dev/null 2>&1; do
    if [ "$(date +%s)" -ge "${ready_deadline}" ]; then
        fail 'export_postgres_ready_timeout'
    fi
    sleep 1
done

postgres_version="$(docker exec "${postgres_container_id}" postgres --version)" \
    || fail 'export_postgres_version_probe_failed'
case "${postgres_version}" in
    *' 14.'*) ;;
    *) fail 'export_postgres_version_invalid' ;;
esac

test_container_id="$(docker create \
    --name "${test_container}" \
    --label "com.svrx.pinry.export-postgres.run=${run_token}" \
    --label 'com.svrx.pinry.export-postgres.role=tests' \
    --network "${network_id}" \
    --mount "type=tmpfs,destination=/data,tmpfs-mode=0700" \
    --tmpfs /tmp:rw,nosuid,nodev,exec,size=1073741824 \
    --env 'PINRY_TEST_POSTGRES_HOST=export-postgres' \
    --env 'PINRY_TEST_POSTGRES_PORT=5432' \
    --env "PINRY_TEST_POSTGRES_NAME=${database_name}" \
    --env "PINRY_TEST_POSTGRES_USER=${database_user}" \
    --env "PINRY_TEST_POSTGRES_PASSWORD=${database_password}" \
    "${app_image_id}" \
    python manage.py test \
        exports.tests.test_concurrency \
        exports.tests.test_snapshot \
        exports.tests.test_archive_finalization \
        exports.tests.test_worker \
        exports.tests.test_worker_recovery \
        exports.tests.test_download \
        --settings=pinry.settings.test_postgres -v 2)" \
    || fail 'export_postgres_test_container_create_failed'
owned_container "${test_container_id}" "${app_image_id}" \
    || fail 'export_postgres_test_container_identity_invalid'
set +e
docker start --attach "${test_container_id}" > "${test_log}" 2>&1
test_start_code=$?
set -e
DATABASE_PASSWORD="${database_password}" python3 - "${test_log}" <<'PY'
import os
import sys

with open(sys.argv[1], encoding="utf-8", errors="replace") as stream:
    content = stream.read()
sys.stdout.write(content.replace(os.environ["DATABASE_PASSWORD"], "[REDACTED]"))
PY
[ "${test_start_code}" -eq 0 ] || fail 'export_postgres_test_failed'
owned_container "${test_container_id}" "${app_image_id}" \
    || fail 'export_postgres_test_container_identity_changed'
test_exit_code="$(docker container inspect --format '{{.State.ExitCode}}' \
    "${test_container_id}")" \
    || fail 'export_postgres_test_exit_missing'
[ "${test_exit_code}" = "0" ] || fail 'export_postgres_test_failed'
remove_owned_container "${test_container_id}" "${app_image_id}" no \
    || fail 'export_postgres_test_container_remove_failed'
test_container_id=""

finish_success \
    'EXPORT_POSTGRES_CONCURRENCY_SMOKE_OK backend=postgresql version=14 modules=6'
