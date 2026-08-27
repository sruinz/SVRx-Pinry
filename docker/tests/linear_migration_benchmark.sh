#!/bin/bash
set -euo pipefail

umask 077
export LC_ALL=C

if [ "$#" -ne 1 ]; then
    printf '%s\n' "benchmark_image_argument_required" >&2
    exit 2
fi
if ! command -v docker >/dev/null 2>&1; then
    printf '%s\n' "LINEAR_MIGRATION_BENCHMARK_SKIP_DOCKER_UNAVAILABLE"
    exit 77
fi
if ! docker info >/dev/null 2>&1; then
    printf '%s\n' "LINEAR_MIGRATION_BENCHMARK_SKIP_DOCKER_DAEMON_UNAVAILABLE"
    exit 77
fi

image="$1"
script_directory="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
fixture_script="${script_directory}/fixtures/create_legacy_fixture.py"
fixture_mount="type=bind,src=${fixture_script},dst=/tmp/create_legacy_fixture.py,readonly"
temporary_root="$(mktemp -d "${TMPDIR:-/tmp}/svrx-pinry-linear.XXXXXX")"
results_root="$(mktemp -d "${TMPDIR:-/tmp}/svrx-pinry-linear-results.XXXXXX")"
containers=""
derived_image="svrx-pinry-linear-strace-$$"

cleanup() {
    local name
    for name in ${containers}; do
        docker rm -f "${name}" >/dev/null 2>&1 || true
    done
    docker image rm "${derived_image}" >/dev/null 2>&1 || true
    case "${temporary_root}" in
        "${TMPDIR:-/tmp}"/svrx-pinry-linear.*)
            rm -rf -- "${temporary_root}"
            ;;
    esac
}

on_signal() {
    exit 130
}

trap cleanup EXIT
trap on_signal HUP INT TERM

fail() {
    printf '%s\n' "$1" >&2
    exit 1
}

expect_line() {
    local expected="$1"
    shift
    local output
    output="$("$@" 2>/dev/null)" || return 1
    [ "${output}" = "${expected}" ]
}

record_event() {
    local stage="$1"
    local status="$2"
    local epoch_ns

    epoch_ns="$(python3 -c 'import time; print(time.time_ns())')" \
        || fail "benchmark_event_timestamp_failed"
    printf '%s\t%s\t%s\n' "${epoch_ns}" "${stage}" "${status}" \
        >> "${results_root}/events.tsv"
}

prepare_case() {
    local count="$1"
    local root="$2"
    mkdir -p -- "${root}"
    chmod 0700 "${root}"
    expect_line FIXTURE_SETTINGS_OK \
        docker run --rm --network none \
        --mount "${fixture_mount}" \
        --mount "type=bind,src=${root},dst=/data" \
        "${image}" python /tmp/create_legacy_fixture.py \
        configure-settings --data-root /data \
        || fail "benchmark_settings_failed"
    expect_line FIXTURE_LINEAR_CREATE_OK \
        docker run --rm --network none \
        --mount "${fixture_mount}" \
        --mount "type=bind,src=${root},dst=/data" \
        --mount "type=bind,src=${root}/local_settings.py,dst=/pinry/pinry/settings/local_settings.py,readonly" \
        "${image}" python /tmp/create_legacy_fixture.py \
        create-linear --data-root /data --images "${count}" \
        --thumbnails 3 --receipt /data/linear-receipt.json \
        || fail "benchmark_fixture_failed_${count}"
}

sample_container() {
    local kind="$1"
    local container_id="$2"
    local output="$3"
    local stats
    local stats_id
    local cpu_percent
    local memory_usage
    local block_io
    local memory_used
    local block_read
    local block_write
    local process_identity
    local epoch_ns

    stats="$(
        docker stats --no-stream \
            --format '{{.ID}}|{{.CPUPerc}}|{{.MemUsage}}|{{.BlockIO}}' \
            "${container_id}"
    )" || fail "benchmark_stats_sample_failed"
    IFS='|' read -r stats_id cpu_percent memory_usage block_io <<EOF
${stats}
EOF
    memory_used="${memory_usage%% / *}"
    block_read="${block_io%% / *}"
    block_write="${block_io##* / }"
    cpu_percent="${cpu_percent%%%}"
    process_identity="$(
        docker exec "${container_id}" python -c '
import hashlib
with open("/proc/1/stat", "r", encoding="ascii") as stream:
    value = stream.read().strip()
with open("/proc/1/cgroup", "rb") as stream:
    cgroup = stream.read()
start_ticks = value.rsplit(")", 1)[1].split()[19]
print("1\t%s\t%s" % (start_ticks, hashlib.sha256(cgroup).hexdigest()))
'
    )" || fail "benchmark_process_identity_failed"
    epoch_ns="$(python3 -c 'import time; print(time.time_ns())')" \
        || fail "benchmark_timestamp_failed"
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "${kind}" "${epoch_ns}" "${container_id}" "${stats_id}" \
        ${process_identity} "${cpu_percent}" "${memory_used}" \
        "${block_read}" "${block_write}" >> "${output}"
}

run_supervisor_observation() {
    local count="$1"
    local root="${temporary_root}/supervisor-${count}"
    local name="svrx-pinry-supervisor-${count}-$$"
    local started_at_epoch_ns
    local exit_code

    record_event "supervisor-${count}" started
    prepare_case "${count}" "${root}"
    started_at_epoch_ns="$(python3 -c 'import time; print(time.time_ns())')" \
        || fail "benchmark_supervisor_timestamp_failed_${count}"
    containers="${containers} ${name}"
    docker run -d --name "${name}" --network none \
        --mount "${fixture_mount}" \
        --mount "type=bind,src=${root},dst=/data" \
        --tmpfs /cross:rw,nosuid,nodev,noexec,size=16777216 \
        "${image}" /pinry/docker/scripts/start.sh --migrate-legacy \
        >/dev/null \
        || fail "benchmark_supervisor_start_failed_${count}"
    expect_line FIXTURE_SUPERVISOR_OBSERVATION_OK \
        docker exec "${name}" python /tmp/create_legacy_fixture.py \
        observe-maintenance-service \
        --base-url http://127.0.0.1 \
        --output /data/supervisor-observation.json \
        --started-at-epoch-ns "${started_at_epoch_ns}" \
        --images "${count}" --files "$((count * 4))" \
        --timeout 7500 --poll-interval 0.25 \
        || fail "benchmark_supervisor_observation_failed_${count}"
    docker stop --time 30 "${name}" >/dev/null \
        || fail "benchmark_supervisor_stop_failed_${count}"
    exit_code="$(docker wait "${name}")" \
        || fail "benchmark_supervisor_wait_failed_${count}"
    [ "${exit_code}" = 0 ] \
        || fail "benchmark_supervisor_process_failed_${count}"
    docker logs "${name}" \
        > "${results_root}/${count}-supervisor.log" 2>&1 \
        || fail "benchmark_supervisor_log_failed_${count}"
    cp "${root}/supervisor-observation.json" \
        "${results_root}/${count}-supervisor.json"
    cp "${root}/linear-receipt.json" \
        "${results_root}/${count}-supervisor-receipt.json"
    docker rm "${name}" >/dev/null
    record_event "supervisor-${count}" completed
}

run_case() {
    local count="$1"
    local root="${temporary_root}/case-${count}"
    local name="svrx-pinry-linear-${count}-$$"
    local running
    local exit_code
    local sample="${results_root}/${count}-stats.tsv"
    local container_id
    record_event "case-${count}" started
    prepare_case "${count}" "${root}"
    containers="${containers} ${name}"
    docker run -d --name "${name}" --network none \
        --mount "${fixture_mount}" \
        --mount "type=bind,src=${root},dst=/data" \
        --mount "type=bind,src=${root}/local_settings.py,dst=/pinry/pinry/settings/local_settings.py,readonly" \
        "${image}" python /tmp/create_legacy_fixture.py \
        benchmark-linear-service --data-root /data \
        --output /data/linear-metrics.json \
        --completion-hold-seconds 3 >/dev/null \
        || fail "benchmark_start_failed_${count}"
    container_id="$(docker inspect --format '{{.Id}}' "${name}")" \
        || fail "benchmark_container_id_failed_${count}"
    printf '%b\n' \
        'sample_kind\tepoch_ns\tcontainer_id\tstats_id\tproc_pid\tproc_start_ticks\tcgroup_sha256\tcpu_percent\tmemory_used\tblock_read\tblock_write' \
        > "${sample}"
    sample_container periodic "${container_id}" "${sample}"
    while :; do
        running="$(docker inspect --format '{{.State.Running}}' "${name}")" \
            || fail "benchmark_inspect_failed_${count}"
        [ "${running}" = true ] \
            || fail "benchmark_final_sample_window_missing_${count}"
        if [ -s "${root}/linear-metrics.json" ]; then
            sample_container final "${container_id}" "${sample}"
            break
        fi
        sample_container periodic "${container_id}" "${sample}"
        sleep 1
    done
    exit_code="$(docker wait "${name}")"
    [ "${exit_code}" = 0 ] || fail "benchmark_process_failed_${count}"
    [ -s "${sample}" ] || fail "benchmark_stats_missing_${count}"
    docker logs "${name}" 2>&1 | grep -qx FIXTURE_LINEAR_BENCHMARK_OK \
        || fail "benchmark_marker_missing_${count}"
    run_supervisor_observation "${count}"
    if [ "${count}" = 1000 ]; then
        expect_line FIXTURE_CONTAINER_METRICS_OK \
            docker run --rm --network none \
            --mount "${fixture_mount}" \
            --mount "type=bind,src=${root},dst=/data" \
            --mount "type=bind,src=${results_root},dst=/results,readonly" \
            "${image}" python /tmp/create_legacy_fixture.py \
            merge-container-metrics \
            --status /data/linear-metrics.json \
            --stats "/results/${count}-stats.tsv" \
            --baseline /results/350.json \
            --supervisor "/results/${count}-supervisor.json" \
            || fail "benchmark_stats_invalid_${count}"
    else
        expect_line FIXTURE_CONTAINER_METRICS_OK \
            docker run --rm --network none \
            --mount "${fixture_mount}" \
            --mount "type=bind,src=${root},dst=/data" \
            --mount "type=bind,src=${results_root},dst=/results,readonly" \
            "${image}" python /tmp/create_legacy_fixture.py \
            merge-container-metrics \
            --status /data/linear-metrics.json \
            --stats "/results/${count}-stats.tsv" \
            --supervisor "/results/${count}-supervisor.json" \
            || fail "benchmark_stats_invalid_${count}"
    fi
    cp "${root}/linear-metrics.json" "${results_root}/${count}.json"
    cp "${root}/linear-receipt.json" \
        "${results_root}/${count}-receipt.json"
    expect_line FIXTURE_LINEAR_METRICS_OK \
        docker run --rm --network none \
        --mount "${fixture_mount}" \
        --mount "type=bind,src=${root},dst=/data,readonly" \
        "${image}" python /tmp/create_legacy_fixture.py \
        verify-linear-metrics --receipt /data/linear-receipt.json \
        --status /data/linear-metrics.json \
        || fail "benchmark_metrics_invalid_${count}"
    docker rm "${name}" >/dev/null
    record_event "case-${count}" completed
}

assert_performance_contract() {
    docker run --rm --network none \
        --mount "type=bind,src=${results_root},dst=/results,readonly" \
        --entrypoint python "${image}" -c '
import json
def load(n):
    with open("/results/%s.json" % n, "r", encoding="ascii") as stream:
        return json.load(stream)
r350, r1000 = load(350), load(1000)
for result in (r350, r1000):
    seconds_source = result["seconds_source"]
    heartbeat_source = result["heartbeat_source"]
    assert seconds_source == "fixture_coordinator"
    assert result["batch_timing_source"] == "generator_yield_lifetime"
    assert heartbeat_source == "nginx_public_status"
    assert result["batch_sample_count"] > 0
    assert result["supervisor_status_samples"] > 0
    assert result["supervisor_heartbeat_updates"] > 0
assert r350["seconds"] <= 2400
assert r1000["seconds"] <= 7200
assert r1000["seconds"] / max(r350["seconds"], 0.001) <= 3.4
assert r350["supervisor_seconds"] <= 2400
assert r1000["supervisor_seconds"] <= 7200
assert r1000["supervisor_seconds"] / max(
    r350["supervisor_seconds"], 0.001
) <= 3.4
assert r1000["ratio"] == round(
    r1000["seconds"] / r350["seconds"], 6
)
assert r1000["ratio"] <= 3.4
assert r1000["queries"] / max(r350["queries"], 1) <= 3.4
' >/dev/null || fail "benchmark_performance_contract_failed"
}

run_strace_audit() {
    local root="${temporary_root}/audit-50"
    local dockerfile="${temporary_root}/Dockerfile.strace"
    local name="svrx-pinry-linear-audit-$$"
    local exit_code
    cat > "${dockerfile}" <<'EOF'
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends strace \
    && rm -rf /var/lib/apt/lists/*
EOF
    docker build --build-arg "BASE_IMAGE=${image}" \
        -f "${dockerfile}" -t "${derived_image}" . >/dev/null \
        || fail "benchmark_strace_image_failed"
    prepare_case 50 "${root}"
    mkdir -p "${root}/strace"
    containers="${containers} ${name}"
    docker run -d --name "${name}" --network none \
        --cap-add SYS_PTRACE --security-opt seccomp=unconfined \
        --mount "${fixture_mount}" \
        --mount "type=bind,src=${root},dst=/data" \
        --mount "type=bind,src=${root}/local_settings.py,dst=/pinry/pinry/settings/local_settings.py,readonly" \
        --entrypoint strace "${derived_image}" \
        -ff -ttt -yy -s 0 \
        -e trace=write,writev,pwrite64,read,pread64,lseek,openat,close,dup,dup2,dup3,fstat,newfstatat,fsync,fdatasync,syncfs,rename,renameat,renameat2,link,linkat,unlink,unlinkat,ftruncate \
        -o /data/strace/io \
        python /pinry/docker/scripts/startup.py --migrate-legacy >/dev/null \
        || fail "benchmark_strace_start_failed"
    expect_line FIXTURE_HTTP_READY \
        docker exec "${name}" python /tmp/create_legacy_fixture.py \
        wait-http --url http://127.0.0.1/api/v2/version/ --timeout 300 \
        || fail "benchmark_strace_ready_marker_missing"
    docker stop --time 30 "${name}" >/dev/null \
        || fail "benchmark_strace_stop_failed"
    exit_code="$(docker wait "${name}")"
    [ "${exit_code}" = 0 ] || fail "benchmark_strace_process_failed"
    expect_line FIXTURE_LINEAR_IO_AUDIT_OK \
        docker run --rm --network none \
        --mount "${fixture_mount}" \
        --mount "type=bind,src=${root},dst=/data" \
        "${image}" python /tmp/create_legacy_fixture.py \
        audit-linear-io --data-root /data --output /data/io-audit.json \
        || fail "benchmark_strace_parse_failed"
    docker run --rm --network none \
        --mount "type=bind,src=${root},dst=/data,readonly" \
        --entrypoint python "${image}" -c '
import json
with open("/data/linear-receipt.json", encoding="ascii") as f: receipt=json.load(f)
with open("/data/io-audit.json", encoding="ascii") as f: audit=json.load(f)
sizes={item["file_key"]:item["size"] for item in receipt["expected_files"]}
assert audit["actual_fsync_calls"] == audit["successful_fsync_calls"]
assert audit["actual_syncfs_calls"] == audit["successful_syncfs_calls"]
assert audit["journal_intent_fsyncs"] == audit["journal_commit_fsyncs"] > 0
assert audit["journal_repair_fsyncs"] == 0
assert audit["journal_tail_repair_fsyncs"] == 0
assert audit["max_journal_event_bytes"] > 65535
assert audit["batch_file_syncfs"] == audit["journal_intent_fsyncs"]
assert audit["publication_staging_directory_fsyncs"] > 0
assert audit["publication_destination_directory_fsyncs"] > 0
assert audit["checkpoint_file_fsyncs"] == 2
assert audit["checkpoint_directory_fsyncs"] == 2
assert audit["checkpoint_events"] == [
    "phase_complete:paths", "phase_complete:backfill",
]
assert audit["source_read_bytes"] == sizes
assert set(audit["source_read_passes"]) == set(sizes)
assert all(value == 1 for value in audit["source_read_passes"].values())
assert audit["publication_mutation_windows"]
for window in audit["publication_mutation_windows"]:
    assert window["event"] == "batch_intent"
    assert window["mutated_directory_identities"] == window["fsynced_directory_identities"]
    assert len(window["mutated_directory_identities"]) >= 2
' >/dev/null || fail "benchmark_strace_contract_failed"
    cp "${root}/linear-receipt.json" \
        "${results_root}/strace-50-receipt.json"
    cp "${root}/io-audit.json" "${results_root}/strace-50-io-audit.json"
    docker rm "${name}" >/dev/null
}

run_repair_strace_audit() {
    local root="${temporary_root}/repair-50"

    prepare_case 50 "${root}"
    expect_line FIXTURE_LINEAR_REPAIR_PREPARED \
        docker run --rm --network none \
        --mount "${fixture_mount}" \
        --mount "type=bind,src=${root},dst=/data" \
        --mount "type=bind,src=${root}/local_settings.py,dst=/pinry/pinry/settings/local_settings.py,readonly" \
        "${derived_image}" python /tmp/create_legacy_fixture.py \
        prepare-linear-repair --data-root /data \
        --output /data/repair-observation.json \
        || fail "benchmark_repair_prepare_failed"
    mkdir -p "${root}/strace"
    expect_line FIXTURE_LINEAR_REPAIR_OK \
        docker run --rm --network none \
        --cap-add SYS_PTRACE --security-opt seccomp=unconfined \
        --mount "${fixture_mount}" \
        --mount "type=bind,src=${root},dst=/data" \
        --mount "type=bind,src=${root}/local_settings.py,dst=/pinry/pinry/settings/local_settings.py,readonly" \
        --entrypoint strace "${derived_image}" \
        -ff -ttt -yy -s 0 \
        -e trace=write,writev,pwrite64,read,pread64,lseek,openat,close,dup,dup2,dup3,fstat,newfstatat,fsync,fdatasync,syncfs,rename,renameat,renameat2,link,linkat,unlink,unlinkat,ftruncate \
        -o /data/strace/io \
        python /tmp/create_legacy_fixture.py run-linear-repair-only \
        --data-root /data --observation /data/repair-observation.json \
        || fail "benchmark_repair_trace_failed"
    expect_line FIXTURE_LINEAR_IO_AUDIT_OK \
        docker run --rm --network none \
        --mount "${fixture_mount}" \
        --mount "type=bind,src=${root},dst=/data" \
        "${image}" python /tmp/create_legacy_fixture.py \
        audit-linear-io --data-root /data --mode repair \
        --observation /data/repair-observation.json \
        --output /data/repair-io-audit.json \
        || fail "benchmark_repair_audit_failed"
    docker run --rm --network none \
        --mount "type=bind,src=${root},dst=/data,readonly" \
        --entrypoint python "${image}" -c '
import json
with open("/data/repair-io-audit.json", encoding="ascii") as stream:
    audit = json.load(stream)
assert audit["journal_intent_fsyncs"] == 0
assert audit["journal_commit_fsyncs"] == 0
assert audit["journal_repair_fsyncs"] == 1
assert audit["journal_tail_repair_fsyncs"] == 0
assert audit["checkpoint_file_fsyncs"] == 1
assert audit["checkpoint_directory_fsyncs"] == 1
assert audit["checkpoint_events"] == ["batch_repair"]
assert len(audit["destination_read_passes"]) == 1
assert set(audit["destination_read_passes"].values()) == {1}
assert len(audit["publication_mutation_windows"]) == 1
window = audit["publication_mutation_windows"][0]
assert window["event"] == "batch_repair"
assert window["mutated_directory_identities"] == window["fsynced_directory_identities"]
assert len(window["mutated_directory_identities"]) == 1
' >/dev/null || fail "benchmark_repair_contract_failed"
    cp "${root}/repair-observation.json" \
        "${results_root}/repair-observation.json"
    cp "${root}/repair-io-audit.json" \
        "${results_root}/repair-io-audit.json"
}

run_tail_repair_strace_audit() {
    local root="${temporary_root}/tail-repair-50"

    prepare_case 50 "${root}"
    expect_line FIXTURE_LINEAR_TAIL_PREPARED \
        docker run --rm --network none \
        --mount "${fixture_mount}" \
        --mount "type=bind,src=${root},dst=/data" \
        --mount "type=bind,src=${root}/local_settings.py,dst=/pinry/pinry/settings/local_settings.py,readonly" \
        "${derived_image}" python /tmp/create_legacy_fixture.py \
        prepare-linear-tail-repair --data-root /data \
        --fault journal_commit_partial_write \
        --output /data/tail-repair-observation.json \
        || fail "benchmark_tail_repair_prepare_failed"
    mkdir -p "${root}/strace-tail-resume"
    expect_line FIXTURE_LINEAR_TAIL_REPAIRED \
        docker run --rm --network none \
        --cap-add SYS_PTRACE --security-opt seccomp=unconfined \
        --mount "${fixture_mount}" \
        --mount "type=bind,src=${root},dst=/data" \
        --mount "type=bind,src=${root}/local_settings.py,dst=/pinry/pinry/settings/local_settings.py,readonly" \
        --entrypoint strace "${derived_image}" \
        -ff -ttt -yy -s 0 \
        -e trace=write,writev,pwrite64,read,pread64,lseek,openat,close,dup,dup2,dup3,fstat,newfstatat,fsync,fdatasync,syncfs,rename,renameat,renameat2,link,linkat,unlink,unlinkat,ftruncate \
        -o /data/strace-tail-resume/io \
        python /tmp/create_legacy_fixture.py \
        run-linear-tail-repair-only --data-root /data \
        --observation /data/tail-repair-observation.json \
        --output /data/tail-repair-result.json \
        || fail "benchmark_tail_repair_trace_failed"
    expect_line FIXTURE_LINEAR_IO_AUDIT_OK \
        docker run --rm --network none \
        --mount "${fixture_mount}" \
        --mount "type=bind,src=${root},dst=/data" \
        "${image}" python /tmp/create_legacy_fixture.py \
        audit-linear-io --data-root /data --mode tail-repair \
        --trace-root /data/strace-tail-resume \
        --observation /data/tail-repair-observation.json \
        --output /data/tail-repair-io-audit.json \
        || fail "benchmark_tail_repair_audit_failed"
    docker run --rm --network none \
        --mount "type=bind,src=${root},dst=/data,readonly" \
        --entrypoint python "${image}" -c '
import json
with open("/data/tail-repair-observation.json", encoding="ascii") as stream:
    observation = json.load(stream)
with open("/data/tail-repair-result.json", encoding="ascii") as stream:
    result = json.load(stream)
with open("/data/tail-repair-io-audit.json", encoding="ascii") as stream:
    audit = json.load(stream)
assert result["resumed"] is True
assert result["tail_repairs"] == 1
assert result["duplicate_fault_event"] is False
assert audit["journal_tail_truncate_count"] == 1
assert audit["journal_tail_truncate_offset"] == observation["last_valid_offset"]
assert audit["journal_tail_repair_fsyncs"] == 1
' >/dev/null || fail "benchmark_tail_repair_contract_failed"
    cp "${root}/tail-repair-observation.json" \
        "${results_root}/tail-repair-observation.json"
    cp "${root}/tail-repair-result.json" \
        "${results_root}/tail-repair-result.json"
    cp "${root}/tail-repair-io-audit.json" \
        "${results_root}/tail-repair-io-audit.json"
}

run_resume_rehash_case() {
    local root="${temporary_root}/resume-rehash-50"
    local name="svrx-pinry-linear-resume-rehash-$$"
    local exit_code

    prepare_case 50 "${root}"
    expect_line FIXTURE_LINEAR_RESUME_PREPARED \
        docker run --rm --network none \
        --mount "${fixture_mount}" \
        --mount "type=bind,src=${root},dst=/data" \
        --mount "type=bind,src=${root}/local_settings.py,dst=/pinry/pinry/settings/local_settings.py,readonly" \
        "${image}" python /tmp/create_legacy_fixture.py \
        prepare-linear-resume --data-root /data \
        --output /data/resume-rehash-expected.json \
        || fail "benchmark_resume_prepare_failed"
    containers="${containers} ${name}"
    docker run -d --name "${name}" --network none \
        --mount "${fixture_mount}" \
        --mount "type=bind,src=${root},dst=/data" \
        --mount "type=bind,src=${root}/local_settings.py,dst=/pinry/pinry/settings/local_settings.py,readonly" \
        "${image}" python /tmp/create_legacy_fixture.py \
        benchmark-linear-service --data-root /data \
        --output /data/resume-rehash-metrics.json >/dev/null \
        || fail "benchmark_resume_start_failed"
    exit_code="$(docker wait "${name}")"
    [ "${exit_code}" = 0 ] || fail "benchmark_resume_process_failed"
    expect_line FIXTURE_RESUME_REHASH_OK \
        docker run --rm --network none \
        --mount "${fixture_mount}" \
        --mount "type=bind,src=${root},dst=/data,readonly" \
        "${image}" python /tmp/create_legacy_fixture.py \
        verify-resume-rehash --receipt /data/linear-receipt.json \
        --status /data/resume-rehash-metrics.json \
        --expected /data/resume-rehash-expected.json \
            || fail "benchmark_resume_rehash_contract_failed"
    cp "${root}/resume-rehash-expected.json" \
        "${results_root}/resume-rehash-expected.json"
    cp "${root}/resume-rehash-metrics.json" \
        "${results_root}/resume-rehash-metrics.json"
    docker rm "${name}" >/dev/null
}

run_crash_case() {
    local fault="$1"
    local root="${temporary_root}/crash-${fault}"

    prepare_case 50 "${root}"
    expect_line FIXTURE_LINEAR_CRASH_OK \
        docker run --rm --network none \
        --mount "${fixture_mount}" \
        --mount "type=bind,src=${root},dst=/data" \
        --mount "type=bind,src=${root}/local_settings.py,dst=/pinry/pinry/settings/local_settings.py,readonly" \
        "${image}" python /tmp/create_legacy_fixture.py \
        exercise-linear-crash --data-root /data --fault "${fault}" \
        --output "/data/crash-${fault}.json" \
        || fail "benchmark_crash_contract_failed_${fault}"
    docker run --rm --network none \
        --mount "type=bind,src=${root},dst=/data,readonly" \
        --entrypoint python "${image}" -c '
import json, sys
fault=sys.argv[1]
with open("/data/crash-%s.json" % fault, encoding="ascii") as f:
    result=json.load(f)
if fault == "journal_checksum_invalid":
    assert result == {"schema_version":1,"fault":fault,"child_exit_code":99,"resumed":False,"checksum_fail_closed":True,"error_code":"linear_journal_invalid","journal_unchanged":True}
else:
    assert result["resumed"] is True
    assert result["duplicate_fault_event"] is False
    assert result["journal_prefix_preserved"] is True
    if "partial_write" in fault:
        assert result["tail_repairs"] == 1
    else:
        assert result["tail_repairs"] == 0
    if fault.startswith("journal_repair_"):
        assert result["commit_ordinal_unchanged"] is True
        assert result["repair_destination_inode_unchanged"] is True
' "${fault}" >/dev/null \
        || fail "benchmark_crash_result_invalid_${fault}"
    cp "${root}/crash-${fault}.json" \
        "${results_root}/crash-${fault}.json"
}

run_crash_matrix() {
    local fault
    for fault in \
        before_destination_rename \
        after_destination_rename \
        after_batch_intent \
        after_database_commit \
        after_batch_commit \
        after_archive_rename \
        journal_intent_partial_write \
        journal_intent_full_write_before_fsync \
        journal_commit_partial_write \
        journal_commit_full_write_before_fsync \
        journal_repair_partial_write \
        journal_repair_full_write_before_fsync \
        journal_checksum_invalid
    do
        run_crash_case "${fault}"
    done
}

docker image inspect "${image}" >/dev/null 2>&1 \
    || fail "benchmark_image_unavailable"
mkdir -p -- "${results_root}"
printf '%s\n' $'epoch_ns\tstage\tstatus' > "${results_root}/events.tsv"
run_crash_matrix
run_strace_audit
run_repair_strace_audit
run_tail_repair_strace_audit
run_resume_rehash_case
for count in 50 350 1000; do
    run_case "${count}"
done
assert_performance_contract
printf '%s\n' "LINEAR_MIGRATION_RESULTS=${results_root}"
printf '%s\n' "LINEAR_MIGRATION_BENCHMARK_OK"
