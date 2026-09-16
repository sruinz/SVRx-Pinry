"""격리 컨테이너 PID 1에서 실제 60초 초과·고아 좀비·재기동을 검증한다."""
from datetime import datetime, timezone
import http.client
import json
import os
from pathlib import Path
import pwd
import signal
import sys
import threading
import time

sys.path.insert(0, "/pinry")
from docker.scripts import migration_status, supervisor  # noqa: E402


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def request(method, path, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", 80, timeout=3)
    try:
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        body = response.read()
        return response.status, json.loads(body) if body else None
    finally:
        connection.close()


def main():
    check(os.getpid() == 1, "이 시험은 격리 컨테이너 PID 1이어야 합니다.")
    mode = sys.argv[1] if len(sys.argv) > 1 else "automatic"
    check(mode in ("automatic", "manual"), "잘못된 시험 모드")
    account = pwd.getpwnam("www-data")
    store = migration_status.MigrationStatusStore(
        migration_status.STATUS_DIRECTORY, lambda: datetime.now(timezone.utc),
        account.pw_uid, account.pw_gid,
    )
    runtime = supervisor.RuntimeSupervisor([], "/data", account.pw_uid, account.pw_gid, store)
    original_spawn, original_hold = runtime._spawn, runtime._hold_failed
    attempts, locks, failures, children, errors = [], [], [], [], []
    observed = []
    finished = threading.Event()
    ready = [False]
    sentinel = Path("/data/recovery-test-sentinel")
    sentinel.write_bytes(b"synthetic-data-must-survive")

    def spawn(role, command, pass_fds=(), cwd=None):
        if role == "migration":
            locks.append((pass_fds[1], os.fstat(pass_fds[1]).st_ino))
        if role == "gunicorn":
            attempts.append(time.monotonic())
            if len(attempts) == 1:
                # master 종료 뒤에도 그룹에 남는 좀비를 PID 1이 회수해야 한다.
                command = [sys.executable, "-c", (
                    "import os,time; pid=os.fork(); "
                    "os._exit(0) if pid == 0 else None; "
                    "open('/tmp/recovery-orphan.pid','w').write(str(pid)); "
                    "time.sleep(300)"
                )]
        child = original_spawn(role, command, pass_fds=pass_fds, cwd=cwd)
        children.append(child)
        return child

    def hold(code):
        failures.append(code)
        check(code == "gunicorn_start_failed", "예상하지 않은 기동 오류: " + code)
        check(time.monotonic() - attempts[0] >= 60, "실제 60초 준비 제한을 거치지 않음")
        orphan = int(Path("/tmp/recovery-orphan.pid").read_text())
        check(supervisor._read_proc_identity(orphan)["state"] == "Z", "좀비 재현 실패")
        print("readiness_timeout_and_orphan_zombie=REPRODUCED", flush=True)
        return original_hold(code)

    def served():
        check(len(attempts) == 2 and len(locks) == 2, "재기동 횟수 불일치")
        check(len(set(locks)) == 1, "기동 잠금이 교체됨")
        check(runtime.recovery_policy.accepted_count == 1, "재시도 예산 불일치")
        check(not Path(store.marker_path).exists(), "준비 완료 후 게이트가 남음")
        check(request("GET", "/api/v2/version/")[0] == 200, "실제 Django API가 복구되지 않음")
        check(sentinel.read_bytes() == b"synthetic-data-must-survive", "합성 데이터 변경")
        orphan = int(Path("/tmp/recovery-orphan.pid").read_text())
        check(not Path("/proc", str(orphan)).exists(), "좀비 회수 누락")
        ready[0] = True
        return 0

    def observe():
        deadline = time.monotonic() + 220
        submitted = False
        try:
            while not finished.wait(0.5):
                if time.monotonic() >= deadline:
                    raise AssertionError("기동 복구 기한 초과")
                try:
                    status, state = request("GET", "/migration/recovery")
                except (OSError, ValueError, http.client.HTTPException):
                    continue
                if status != 200:
                    continue
                observed.append(state["reason"])
                if mode == "manual" and not submitted and state["available"]:
                    status, _ = request("POST", "/migration/restart", {
                        "X-SVRX-Recovery-Token": state["token"],
                        "X-SVRX-Recovery-Generation": state["generation"],
                    })
                    check(status == 202, "수동 재시동 접수 실패")
                    submitted = True
        except BaseException as error:
            errors.append(str(error))
            runtime.handle_signal(signal.SIGTERM, None)

    runtime._spawn, runtime._hold_failed, runtime._serve_application = spawn, hold, served
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, runtime.handle_signal)
    observer = threading.Thread(target=observe)
    observer.start()
    try:
        result = runtime.run()
    finally:
        finished.set()
        observer.join(5)
    check(not errors, str(errors))
    check(result == 0 and ready[0], "실제 서비스 복구 실패")
    check(failures == ["gunicorn_start_failed"], "예상하지 않은 반복 실패")
    check("available" in observed, "복구 가능 상태를 관찰하지 못함")
    check(all(child.process.poll() is not None for child in children), "자식 정리 누락")
    print("readiness_recovery_{}=PASS attempts=2 accepted=1".format(mode), flush=True)


if __name__ == "__main__":
    main()
