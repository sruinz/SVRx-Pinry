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
script_directory="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
repository_root="$(CDPATH= cd -- "${script_directory}/../.." && pwd -P)"
postgres_test_directory="${repository_root}/exports/tests"
postgres_test_settings="${repository_root}/pinry/settings/test_postgres.py"
if [ ! -d "${postgres_test_directory}" ] \
    || [ -L "${postgres_test_directory}" ]; then
    printf '%s\n' 'export_postgres_test_directory_invalid' >&2
    exit 1
fi
if [ ! -f "${postgres_test_settings}" ] \
    || [ -L "${postgres_test_settings}" ]; then
    printf '%s\n' 'export_postgres_test_settings_invalid' >&2
    exit 1
fi
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
lock_probe="${smoke_root}/actual_lock_retry.py"
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

container_state() {
    local container_id="$1"
    local listed_ids
    if docker container inspect "${container_id}" >/dev/null 2>&1; then
        printf '%s\n' exists
        return 0
    fi
    listed_ids="$(docker container ls --all --no-trunc \
        --filter "id=${container_id}" --format '{{.ID}}' 2>/dev/null)" \
        || return 1
    if [ -z "${listed_ids}" ]; then
        printf '%s\n' absent
    elif [ "${listed_ids}" = "${container_id}" ]; then
        printf '%s\n' exists
    else
        return 1
    fi
}

container_exists() {
    [ "$(container_state "$1")" = exists ]
}

container_absent() {
    [ "$(container_state "$1")" = absent ]
}

network_state() {
    local current_id="$1"
    local listed_ids
    if docker network inspect "${current_id}" >/dev/null 2>&1; then
        printf '%s\n' exists
        return 0
    fi
    listed_ids="$(docker network ls --no-trunc --filter "id=${current_id}" \
        --format '{{.ID}}' 2>/dev/null)" || return 1
    if [ -z "${listed_ids}" ]; then
        printf '%s\n' absent
    elif [ "${listed_ids}" = "${current_id}" ]; then
        printf '%s\n' exists
    else
        return 1
    fi
}

network_exists() {
    [ "$(network_state "$1")" = exists ]
}

network_absent() {
    [ "$(network_state "$1")" = absent ]
}

volume_state() {
    local current_name="$1"
    local listed_names
    if docker volume inspect "${current_name}" >/dev/null 2>&1; then
        printf '%s\n' exists
        return 0
    fi
    listed_names="$(docker volume ls --filter "name=${current_name}" \
        --format '{{.Name}}' 2>/dev/null)" || return 1
    if [ -z "${listed_names}" ]; then
        printf '%s\n' absent
    elif [ "${listed_names}" = "${current_name}" ]; then
        printf '%s\n' exists
    else
        return 1
    fi
}

volume_exists() {
    [ "$(volume_state "$1")" = exists ]
}

volume_absent() {
    [ "$(volume_state "$1")" = absent ]
}

remove_owned_container() {
    local container_id="$1"
    local expected_image_id="$2"
    local remove_volumes="$3"
    local status=0
    if ! container_exists "${container_id}"; then
        if ! container_absent "${container_id}"; then
            printf 'export_postgres_container_state_unknown=%s\n' \
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
        if [ "${remove_volumes}" = "yes" ] && [ -n "${volume_name}" ] \
            && ! volume_absent "${volume_name}"; then
            printf 'export_postgres_volume_state_unknown=%s\n' \
                "${volume_name}" >&2
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
            status=1
        fi
    elif [ "${remove_volumes}" = "no" ]; then
        if ! docker rm -f "${container_id}" >/dev/null 2>&1; then
            printf 'export_postgres_container_cleanup_failed=%s\n' \
                "${container_id}" >&2
            status=1
        fi
    else
        printf 'export_postgres_volume_cleanup_mode_invalid=%s\n' \
            "${remove_volumes}" >&2
        return 1
    fi
    if container_exists "${container_id}"; then
        printf 'export_postgres_container_leaked=%s\n' \
            "${container_id}" >&2
        status=1
    elif ! container_absent "${container_id}"; then
        printf 'export_postgres_container_state_unknown=%s\n' \
            "${container_id}" >&2
        status=1
    fi
    if [ "${remove_volumes}" = "yes" ] && [ -n "${volume_name}" ] \
        && volume_exists "${volume_name}"; then
        printf 'export_postgres_anonymous_volume_leaked=%s\n' \
            "${volume_name}" >&2
        current_volume_fingerprint "${volume_name}" >&2 || true
        status=1
    elif [ "${remove_volumes}" = "yes" ] && [ -n "${volume_name}" ] \
        && ! volume_absent "${volume_name}"; then
        printf 'export_postgres_volume_state_unknown=%s\n' \
            "${volume_name}" >&2
        status=1
    fi
    return "${status}"
}

remove_owned_network() {
    local remove_status=0
    if ! network_exists "${network_id}"; then
        if network_absent "${network_id}"; then
            return 0
        fi
        printf 'export_postgres_network_state_unknown=%s\n' \
            "${network_id}" >&2
        return 1
    fi
    if ! owned_network; then
        printf 'export_postgres_network_identity_changed=%s\n' \
            "${network_id}" >&2
        return 1
    fi
    if ! docker network rm "${network_id}" >/dev/null 2>&1; then
        printf 'export_postgres_network_cleanup_failed=%s\n' \
            "${network_id}" >&2
        remove_status=1
    fi
    if network_exists "${network_id}"; then
        printf 'export_postgres_network_leaked=%s\n' "${network_id}" >&2
        return 1
    fi
    if ! network_absent "${network_id}"; then
        printf 'export_postgres_network_state_unknown=%s\n' \
            "${network_id}" >&2
        return 1
    fi
    return "${remove_status}"
}

cleanup() {
    local status=0
    if [ -n "${test_container_id}" ]; then
        if remove_owned_container \
            "${test_container_id}" "${app_image_id}" no; then
            test_container_id=""
        else
            status=1
            if container_absent "${test_container_id}"; then
                test_container_id=""
            fi
        fi
    fi
    if [ -n "${postgres_container_id}" ]; then
        if remove_owned_container \
            "${postgres_container_id}" "${postgres_image_id}" yes; then
            postgres_container_id=""
        else
            status=1
            if container_absent "${postgres_container_id}"; then
                postgres_container_id=""
            fi
        fi
    fi
    if [ -n "${network_id}" ]; then
        if remove_owned_network; then
            network_id=""
        else
            status=1
            if network_absent "${network_id}"; then
                network_id=""
            fi
        fi
    fi
    if [ -n "${smoke_root}" ] \
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
    trap '' HUP INT TERM
    if ! cleanup && [ "${original_status}" -eq 0 ]; then
        original_status=1
    fi
    exit "${original_status}"
}

finish_success() {
    local message="$1"
    trap '' HUP INT TERM
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

cat > "${lock_probe}" <<'PY'
import os
import threading
import time

os.environ.setdefault(
    "DJANGO_SETTINGS_MODULE",
    "pinry.settings.test_postgres",
)

import django

django.setup()

from django.contrib.auth import get_user_model
from django.db import (
    DatabaseError,
    close_old_connections,
    connection,
    transaction,
)
from django.utils import timezone

from core.models import Board, MediaAsset, Pin
from core.services.database_fence import database_write_fence
from django_images.models import Image
from exports.models import (
    ExportJob,
    ExportSlot,
    ExportTarget,
    ExportWorkerLease,
)
from exports.services.jobs import JobService
from exports.services.targeting import TargetingService
from exports.tests.helpers import create_export_pin, create_export_user
from taggit.models import Tag, TaggedItem


User = get_user_model()
expected_tables = {
    "auth_user",
    "core_board",
    "core_board_pins",
    "core_pin",
    "django_images_image",
    "core_mediaasset",
    "taggit_tag",
    "taggit_taggeditem",
    "exports_exportjob",
    "exports_exportslot",
    "exports_exporttarget",
}
actual_tables = {model._meta.db_table for model in JobService.FENCE_MODELS}
if actual_tables != expected_tables:
    raise AssertionError("postgres_fence_model_list_mismatch")
if ExportWorkerLease._meta.db_table in actual_tables:
    raise AssertionError("postgres_worker_lease_must_not_be_fenced")
expected_models = {
    User,
    Board,
    Board.pins.through,
    Pin,
    Image,
    MediaAsset,
    Tag,
    TaggedItem,
    ExportJob,
    ExportSlot,
    ExportTarget,
}
if set(JobService.FENCE_MODELS) != expected_models:
    raise AssertionError("postgres_fence_model_identity_mismatch")

if connection.vendor != "postgresql":
    raise AssertionError("postgres_backend_required")
with connection.cursor() as cursor:
    cursor.execute("SELECT current_setting('server_version_num')")
    version_number = int(cursor.fetchone()[0])
    cursor.execute("SELECT pg_backend_pid()")
    contender_backend_pid = int(cursor.fetchone()[0])
if version_number // 10000 != 14:
    raise AssertionError("postgres_14_required")

owner = create_export_user("actual-lock-owner")
pin = create_export_pin(owner, filename="actual-lock.png")
now = timezone.now()
ExportWorkerLease.objects.create(
    pk=1,
    health_state="ready",
    heartbeat_at=now,
)

lock_holder_acquired = threading.Event()
release_lock_holder = threading.Event()
lock_holder_done = threading.Event()
retry_observed = threading.Event()
holder_backend_pid = []
holder_errors = []


def hold_user_table_lock():
    close_old_connections()
    try:
        with transaction.atomic(using="default"):
            with connection.cursor() as cursor:
                cursor.execute(
                    'LOCK TABLE "auth_user" IN ROW EXCLUSIVE MODE'
                )
                cursor.execute("SELECT pg_backend_pid()")
                holder_backend_pid.append(int(cursor.fetchone()[0]))
            lock_holder_acquired.set()
            if not release_lock_holder.wait(10):
                raise AssertionError("postgres_lock_holder_release_timeout")
    except BaseException as error:
        holder_errors.append(error)
        lock_holder_acquired.set()
    finally:
        close_old_connections()
        lock_holder_done.set()


holder = threading.Thread(
    target=hold_user_table_lock,
    name="postgres-actual-lock-holder",
)
holder.start()
try:
    if not lock_holder_acquired.wait(10):
        raise AssertionError("postgres_lock_holder_acquire_timeout")
    if holder_errors:
        raise holder_errors[0]
    if len(holder_backend_pid) != 1:
        raise AssertionError("postgres_lock_holder_backend_missing")
    if holder_backend_pid[0] == contender_backend_pid:
        raise AssertionError("postgres_lock_connections_not_independent")

    try:
        with transaction.atomic(using="default"):
            with connection.cursor() as cursor:
                cursor.execute(
                    'LOCK TABLE "auth_user" IN EXCLUSIVE MODE NOWAIT'
                )
        raise AssertionError("postgres_nowait_false_success")
    except DatabaseError as error:
        sqlstates = set()
        for candidate in (
            error,
            getattr(error, "__cause__", None),
            getattr(error, "__context__", None),
        ):
            if candidate is None:
                continue
            sqlstate = getattr(candidate, "pgcode", None)
            if sqlstate is None:
                sqlstate = getattr(candidate, "sqlstate", None)
            sqlstates.add(sqlstate)
        if "55P03" not in sqlstates:
            raise AssertionError("postgres_nowait_sqlstate_invalid")

    retry_count = [0]

    def release_after_observed_retry(seconds):
        if seconds < 0:
            raise AssertionError("postgres_retry_sleep_invalid")
        retry_count[0] += 1
        retry_observed.set()
        release_lock_holder.set()
        if not lock_holder_done.wait(10):
            raise AssertionError("postgres_lock_holder_commit_timeout")
        time.sleep(seconds)

    service = JobService(
        targeting=TargetingService(size_observer=lambda identity: 1),
        available_space_observer=lambda: 10 ** 12,
        sleeper=release_after_observed_retry,
    )
    job = service.create(
        owner,
        {"scope": "pins", "pin_ids": [pin.pk]},
        now,
    )
finally:
    release_lock_holder.set()
    lock_holder_done.wait(10)
    holder.join(1)

if holder.is_alive():
    raise AssertionError("postgres_lock_holder_join_timeout")
if holder_errors:
    raise holder_errors[0]
if not retry_observed.is_set() or retry_count[0] != 1:
    raise AssertionError("postgres_actual_lock_retry_not_observed")
if job.state != "queued":
    raise AssertionError("postgres_retry_job_state_invalid")
if ExportJob.objects.filter(pk=job.pk, owner=owner).count() != 1:
    raise AssertionError("postgres_retry_job_missing")
if ExportTarget.objects.filter(job=job, pin_id=pin.pk).count() != 1:
    raise AssertionError("postgres_retry_target_missing")
if ExportSlot.objects.filter(owner=owner, current_job=job).count() != 1:
    raise AssertionError("postgres_retry_slot_missing")

with database_write_fence(
    using="default",
    models=JobService.FENCE_MODELS,
):
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT relation_class.relname, held_lock.mode, held_lock.granted
              FROM pg_catalog.pg_locks AS held_lock
              JOIN pg_catalog.pg_class AS relation_class
                ON relation_class.oid = held_lock.relation
             WHERE held_lock.pid = pg_backend_pid()
               AND held_lock.locktype = 'relation'
            """
        )
        held_relation_locks = cursor.fetchall()
exclusive_tables = {
    relation_name
    for relation_name, mode, granted in held_relation_locks
    if mode == "ExclusiveLock" and granted is True
}
if exclusive_tables != expected_tables:
    raise AssertionError("postgres_exclusive_fence_lock_set_mismatch")

print(
    "EXPORT_POSTGRES_ACTUAL_LOCK_RETRY_OK "
    "backend=postgresql version=14 fence_models={} locked_tables={} "
    "retries={}".format(
        len(actual_tables),
        len(exclusive_tables),
        retry_count[0],
    )
)
PY
chmod 0400 "${lock_probe}"

test_container_id="$(docker create \
    --name "${test_container}" \
    --label "com.svrx.pinry.export-postgres.run=${run_token}" \
    --label 'com.svrx.pinry.export-postgres.role=tests' \
    --network "${network_id}" \
    --mount "type=tmpfs,destination=/data,tmpfs-mode=0700" \
    --mount "type=bind,src=${lock_probe},dst=/pinry/actual_lock_retry.py,readonly" \
    --mount "type=bind,src=${postgres_test_directory},dst=/pinry/exports/tests,readonly" \
    --mount "type=bind,src=${postgres_test_settings},dst=/pinry/pinry/settings/test_postgres.py,readonly" \
    --tmpfs /tmp:rw,nosuid,nodev,exec,size=1073741824 \
    --env 'DJANGO_SETTINGS_MODULE=pinry.settings.test_postgres' \
    --env 'PINRY_TEST_POSTGRES_HOST=export-postgres' \
    --env 'PINRY_TEST_POSTGRES_PORT=5432' \
    --env "PINRY_TEST_POSTGRES_NAME=${database_name}" \
    --env "PINRY_TEST_POSTGRES_USER=${database_user}" \
    --env "PINRY_TEST_POSTGRES_PASSWORD=${database_password}" \
    "${app_image_id}" \
    sh -c 'python manage.py migrate --noinput --verbosity 0 \
        --settings=pinry.settings.test_postgres \
      && python /pinry/actual_lock_retry.py \
      && python manage.py test \
        exports.tests.test_concurrency \
        exports.tests.test_snapshot \
        exports.tests.test_archive_finalization \
        exports.tests.test_worker \
        exports.tests.test_worker_recovery \
        exports.tests.test_download \
        exports.tests.test_api.ExportAPITests.test_postgresql_fence_model_list_is_complete_and_excludes_worker_lease \
        --settings=pinry.settings.test_postgres --noinput -v 2')" \
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
grep -Eq '^EXPORT_POSTGRES_ACTUAL_LOCK_RETRY_OK backend=postgresql version=14 fence_models=11 locked_tables=11 retries=[1-9][0-9]*$' \
    "${test_log}" || fail 'export_postgres_actual_lock_retry_missing'
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
    'EXPORT_POSTGRES_CONCURRENCY_SMOKE_OK backend=postgresql version=14 modules=6 fence_contract=1 actual_lock_retry=1'
