#!/usr/bin/env bash
set -euo pipefail

image=${1:?검증 이미지가 필요합니다}
repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
docker_binary="${DOCKER_BINARY:-docker}"
temporary_root="$(mktemp -d "${TMPDIR:-/tmp}/svrx-recovery-smoke.XXXXXX")"
run_token="$(python3 -c 'import secrets; print(secrets.token_hex(12))')"
container_name="svrx-recovery-smoke-${run_token}"
container_id=""

cleanup() {
    if [[ -n "$container_id" ]]; then
        actual_label="$($docker_binary inspect --format '{{index .Config.Labels "svrx.recovery-smoke"}}' "$container_id" 2>/dev/null || true)"
        if [[ "$actual_label" == "$run_token" ]]; then
            "$docker_binary" rm -f -v "$container_id" >/dev/null
        else
            echo '시험 컨테이너 정체성 불일치: 자동 정리를 중단합니다.' >&2
            return 1
        fi
        if "$docker_binary" inspect "$container_id" >/dev/null 2>&1; then
            echo '시험 컨테이너 정리 실패' >&2
            return 1
        fi
        echo "cleanup_verified=$container_name"
    fi
    rm -rf "$temporary_root"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

image_id="$($docker_binary image inspect --format '{{.Id}}' "$image")"
[[ "$image_id" =~ ^sha256:[0-9a-f]{64}$ ]]
echo "runtime_base_image=$image_id"
echo "source_revision=$(git -C "$repository_root" rev-parse HEAD)"

# 시험에 필요한 코드만 전송한다. 기반 이미지의 오래된 제품 코드는 실행하지 않는다.
python3 - "$repository_root" "$temporary_root/source.tar" <<'PY'
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile

root = Path(sys.argv[1])
paths = [
    "docker/__init__.py", "docker/scripts/__init__.py",
    "docker/scripts/supervisor.py", "docker/scripts/startup_recovery.py",
    "docker/scripts/migration_status.py", "django_images/__init__.py",
    "django_images/file_ops.py", "django_images/services/__init__.py",
    "django_images/services/startup_lock.py", "docker/nginx/nginx.conf",
    "docker/nginx/sites-enabled/default",
    "docker/tests/fixtures/startup_recovery_fixture.py",
    "docker/tests/startup_recovery_smoke.sh",
]
paths += [str(path.relative_to(root)) for path in sorted((root / "docker/migration").iterdir()) if path.is_file()]
manifest = {}
with tarfile.open(sys.argv[2], "w") as archive:
    for name in sorted(paths):
        path = root / name
        if not path.exists() and name.endswith("__init__.py"):
            continue
        assert path.is_file() and not path.is_symlink(), name
        payload = path.read_bytes()
        manifest[name] = hashlib.sha256(payload).hexdigest()
        info = tarfile.TarInfo(name)
        info.size = len(payload)
        info.mode = 0o644
        archive.addfile(info, io.BytesIO(payload))
    payload = json.dumps(manifest, sort_keys=True).encode()
    info = tarfile.TarInfo("source-manifest.json")
    info.size = len(payload)
    archive.addfile(info, io.BytesIO(payload))
print("source_manifest_sha256=" + hashlib.sha256(payload).hexdigest())
print("source_manifest=" + payload.decode())
PY

# 호스트 mount·공개 포트·외부 네트워크 없이 컨테이너 loopback만 사용한다.
container_id="$($docker_binary create --name "$container_name" \
    --label "svrx.recovery-smoke=$run_token" --network none --cpuset-cpus 0 \
    --memory 512m --pids-limit 96 --read-only \
    --tmpfs /tmp:rw,nosuid,nodev,size=128m \
    --tmpfs /data:rw,nosuid,nodev,noexec,size=1m \
    --tmpfs /overlay:rw,nosuid,nodev,size=32m \
    --entrypoint /bin/sh "$image_id" \
    -c 'exec timeout 240 sleep 240')"
"$docker_binary" start "$container_id" >/dev/null
"$docker_binary" exec -i "$container_id" tar -xf - -C /overlay < "$temporary_root/source.tar"
"$docker_binary" exec -w /overlay -e PYTHONPATH=/overlay \
    -e PYTHONDONTWRITEBYTECODE=1 "$container_id" \
    timeout 210 python3 -u docker/tests/fixtures/startup_recovery_fixture.py
