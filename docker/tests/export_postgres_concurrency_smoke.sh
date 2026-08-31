#!/bin/bash
set -euo pipefail

umask 077
export LC_ALL=C

if [ "$#" -ne 1 ]; then
    printf '%s\n' 'usage: export_postgres_concurrency_smoke.sh IMAGE' >&2
    exit 2
fi

image="$1"
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
if ! docker image inspect "${image}" >/dev/null 2>&1; then
    printf '%s\n' 'export_postgres_app_image_unavailable' >&2
    exit 1
fi

smoke_parent="${TMPDIR:-/tmp}"
smoke_root="$(mktemp -d "${smoke_parent%/}/svrx-pinry-export-postgres.XXXXXX")"
name_suffix="$(basename "${smoke_root}" | tr -c 'a-zA-Z0-9_.-' '-')-$$-${RANDOM}"
run_token="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
network_name="svrx-export-pg-net-${name_suffix}"
volume_name="svrx-export-pg-data-${name_suffix}"
postgres_container="svrx-export-pg-${name_suffix}"
test_container="svrx-export-pg-test-${name_suffix}"
database_name="pinry_export_smoke"
database_user="pinry_export_smoke"
database_password="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
test_log="${smoke_root}/tests.log"
network_id=""
volume_fingerprint=""
postgres_container_id=""
test_container_id=""

owned_container() {
    local container_id="$1"
    local identity
    [ -n "${container_id}" ] || return 1
    identity="$(docker container inspect --format \
        '{{.Id}}|{{index .Config.Labels "com.svrx.pinry.export-postgres.run"}}' \
        "${container_id}" 2>/dev/null)" || return 1
    [ "${identity}" = "${container_id}|${run_token}" ]
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
    docker volume inspect --format \
        '{{.Name}}|{{.CreatedAt}}|{{.Mountpoint}}|{{index .Labels "com.svrx.pinry.export-postgres.run"}}' \
        "${volume_name}" 2>/dev/null
}

owned_volume() {
    local current
    [ -n "${volume_fingerprint}" ] || return 1
    current="$(current_volume_fingerprint)" || return 1
    [ "${current}" = "${volume_fingerprint}" ] \
        && [ "${current##*|}" = "${run_token}" ]
}

cleanup() {
    if owned_container "${test_container_id}"; then
        docker rm -f "${test_container_id}" >/dev/null 2>&1 || true
    fi
    if owned_container "${postgres_container_id}"; then
        docker rm -f "${postgres_container_id}" >/dev/null 2>&1 || true
    fi
    if owned_network; then
        docker network rm "${network_id}" >/dev/null 2>&1 || true
    fi
    if owned_volume; then
        docker volume rm "${volume_name}" >/dev/null 2>&1 || true
    fi
    if [ -n "${smoke_root}" ] && [ -d "${smoke_root}" ] \
        && [ ! -L "${smoke_root}" ]; then
        case "${smoke_root}" in
            "${smoke_parent%/}"/svrx-pinry-export-postgres.*)
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

fail() {
    printf '%s\n' "$1" >&2
    if owned_container "${postgres_container_id}"; then
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

if docker volume inspect "${volume_name}" >/dev/null 2>&1; then
    fail 'export_postgres_volume_name_collision'
fi
docker volume create \
    --label "com.svrx.pinry.export-postgres.run=${run_token}" \
    "${volume_name}" >/dev/null \
    || fail 'export_postgres_volume_create_failed'
volume_fingerprint="$(current_volume_fingerprint)" \
    || fail 'export_postgres_volume_identity_missing'
owned_volume || {
    volume_fingerprint=""
    fail 'export_postgres_volume_identity_invalid'
}

postgres_container_id="$(docker run -d \
    --name "${postgres_container}" \
    --label "com.svrx.pinry.export-postgres.run=${run_token}" \
    --label 'com.svrx.pinry.export-postgres.role=database' \
    --network "${network_id}" \
    --network-alias export-postgres \
    --mount "type=volume,src=${volume_name},dst=/var/lib/postgresql/data" \
    --env "POSTGRES_DB=${database_name}" \
    --env "POSTGRES_USER=${database_user}" \
    --env "POSTGRES_PASSWORD=${database_password}" \
    postgres:14-alpine)" || {
    fail 'export_postgres_container_start_failed'
}
owned_container "${postgres_container_id}" \
    || fail 'export_postgres_container_identity_invalid'

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
    "${image}" \
    python manage.py test \
        exports.tests.test_concurrency \
        exports.tests.test_snapshot \
        exports.tests.test_archive_finalization \
        exports.tests.test_worker \
        exports.tests.test_worker_recovery \
        exports.tests.test_download \
        --settings=pinry.settings.test_postgres -v 2)" \
    || fail 'export_postgres_test_container_create_failed'
owned_container "${test_container_id}" \
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
owned_container "${test_container_id}" \
    || fail 'export_postgres_test_container_identity_changed'
test_exit_code="$(docker container inspect --format '{{.State.ExitCode}}' \
    "${test_container_id}")" \
    || fail 'export_postgres_test_exit_missing'
[ "${test_exit_code}" = "0" ] || fail 'export_postgres_test_failed'
docker rm "${test_container_id}" >/dev/null \
    || fail 'export_postgres_test_container_remove_failed'
test_container_id=""

printf '%s\n' \
    'EXPORT_POSTGRES_CONCURRENCY_SMOKE_OK backend=postgresql version=14 modules=6'
