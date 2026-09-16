"""실제 정책·잠금·프로세스군·소켓·Nginx에 합성 자식을 연결하는 Linux 인수 시험."""
import concurrent.futures
from datetime import datetime, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import pwd
import re
import signal
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import threading
import time

from docker.scripts import migration_status, startup_recovery, supervisor
from django_images import file_ops
from django_images.services import startup_lock


ROOT = Path(__file__).resolve().parents[3]


def check(value, message):
    if not value:
        raise AssertionError(message)


def wait_for(predicate, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("상태 전환 기한 초과")


def verify_source():
    raw = (ROOT / "source-manifest.json").read_bytes()
    for name, expected in json.loads(raw).items():
        check(hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected,
              "소스 불일치: " + name)
    for module in (supervisor, migration_status, startup_recovery, startup_lock, file_ops):
        check(Path(module.__file__).resolve().is_relative_to(ROOT), "기반 이미지 소스 사용")
    print("verified_source_manifest_sha256=" + hashlib.sha256(raw).hexdigest(), flush=True)
    print("python=" + sys.version.split()[0], flush=True)
    subprocess.run(["nginx", "-v"], check=True)


class Scenario:
    def __init__(self, root, mode):
        self.root = root
        self.root.mkdir(mode=0o755)
        self.mode = mode
        self.runtime_dir = root / "runtime"
        self.data = root / "data"
        self.data.mkdir()
        account = pwd.getpwnam("www-data")
        self.uid, self.gid = account.pw_uid, account.pw_gid
        os.chown(self.data, self.uid, self.gid)
        with sqlite3.connect(str(self.data / "fixture.sqlite3")) as db:
            db.execute("CREATE TABLE synthetic (id INTEGER PRIMARY KEY, value TEXT)")
            db.execute("INSERT INTO synthetic VALUES (1, '보존 대상')")
        (self.data / "image.png").write_bytes(bytes.fromhex("89504e470d0a1a0a") + b"synthetic")
        (self.data / "settings.json").write_text('{"synthetic":true}')
        self.before = self.hashes()
        self.store = migration_status.MigrationStatusStore(
            str(self.runtime_dir), lambda: datetime.now(timezone.utc), self.uid, self.gid)
        self.runtime = supervisor.RuntimeSupervisor([], str(self.data), self.uid, self.gid, self.store)
        self.runtime.auth_recovery_config_path = root / "auth-recovery.conf"
        self.attempts = []
        self.children = []
        self.errors = []
        self.results = []
        self.configuration()
        original_spawn = self.runtime._spawn

        def spawn(role, command, pass_fds=(), cwd=None):
            if role == "nginx":
                command = ["nginx", "-p", str(root) + "/", "-c", str(root / "nginx.conf"), "-g", "daemon off;"]
            elif role == "migration":
                check(Path(self.store.marker_path).exists(), "작업자 생성 전 게이트 누락")
                self.runtime.startup_lock.verify_held()
                self.attempts.append((time.monotonic(), pass_fds[1], os.fstat(pass_fds[1]).st_ino))
                number = len(self.attempts)
                delay = 18 if mode == "timeout_success" and number == 1 else 1.5
                if mode == "protocol":
                    frame = b'bad frame\n'
                elif mode.startswith("error:"):
                    frame = json.dumps({"phase": "error", "error_code": mode.split(":", 1)[1]}, sort_keys=True, separators=(",", ":")).encode() + b"\n"
                else:
                    frame = b'{"phase":"complete"}\n'
                code = "import os,time;os.write({}, {!r});os.close({});time.sleep({})".format(pass_fds[0], frame, pass_fds[0], delay)
                command = [sys.executable, "-c", code]
                print("attempt={} mode={}".format(number, mode), flush=True)
            elif role == "gunicorn":
                if mode == "timeout_success":
                    command = [sys.executable, str(Path(__file__).resolve()), "app"]
                else:
                    command = [sys.executable, "-c", "import time;time.sleep(0.15)"]
            elif role == "export":
                command = [sys.executable, "-c", "import time;time.sleep(180)"]
            record = original_spawn(role, command, pass_fds, cwd=str(ROOT))
            self.children.append(record)
            return record

        self.runtime._spawn = spawn
        # 실제 내보내기 자식은 유지하되 본 시험 범위 밖 저장소 bootstrap만 합성한다.
        self.runtime._run_export_storage_bootstrap = lambda: True
        self.thread = threading.Thread(target=self.run)

    def hashes(self):
        return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.data.iterdir() if not path.name.startswith(".")}

    def configuration(self):
        source = (ROOT / "docker/nginx/sites-enabled/default").read_text()
        source = source.replace("listen 80 default;", "listen 127.0.0.1:8080;")
        source = source.replace("/run/svrx-pinry", str(self.runtime_dir))
        source = source.replace("/pinry/docker", str(ROOT / "docker"))
        source = source.replace("/pinry/pinry-spa/dist/", str(self.root) + "/spa/")
        (self.root / "server.conf").write_text(source)
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(self.root / "key.pem"), "-out", str(self.root / "cert.pem"), "-days", "1", "-subj", "/CN=recovery.test"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        config = """user www-data;
worker_processes 1;
pid ROOT/nginx.pid;
error_log ROOT/error.log warn;
events { worker_connections 128; }
http {
 access_log off;
 client_body_temp_path ROOT/body;
 proxy_temp_path ROOT/proxy;
 fastcgi_temp_path ROOT/fastcgi;
 uwsgi_temp_path ROOT/uwsgi;
 scgi_temp_path ROOT/scgi;
 include /etc/nginx/mime.types;
 RATE_ZONE
 PROXY_MAP
 include ROOT/server.conf;
 server {
  listen 127.0.0.1:8443 ssl;
  ssl_certificate ROOT/cert.pem;
  ssl_certificate_key ROOT/key.pem;
  location / { proxy_set_header Host $http_host; proxy_pass http://127.0.0.1:8080; }
 }
 server {
  listen 127.0.0.1:8444 ssl;
  ssl_certificate ROOT/cert.pem;
  ssl_certificate_key ROOT/key.pem;
  location / { proxy_set_header Host internal.invalid; proxy_pass http://127.0.0.1:8080; }
 }
}
""".replace("ROOT", str(self.root))
        main_config = (ROOT / "docker/nginx/nginx.conf").read_text()
        zones = [line.strip() for line in main_config.splitlines()
                 if "limit_req_zone" in line and "startup_recovery:" in line]
        check(len(zones) == 1, "제품 Nginx 공유 제한 설정 누락")
        proxy_map = re.search(
            r'(?m)^\s*map\s+"[^"\n]*"\s+\$pinry_proxied\s*\{[^{}]*\}', main_config,
        )
        check(proxy_map is not None, "제품 Nginx 프록시 경계 설정 누락")
        config = config.replace("RATE_ZONE", zones[0])
        config = config.replace("PROXY_MAP", proxy_map.group(0))
        (self.root / "nginx.conf").write_text(config)

    def run(self):
        try:
            self.results.append(self.runtime.run())
        except BaseException as error:
            self.errors.append(repr(error))

    def __enter__(self):
        self.thread.start()
        wait_for(lambda: (self.runtime_dir / "startup-recovery.sock").is_socket() or not self.thread.is_alive(), 30)
        check(not self.errors and self.thread.is_alive(), "기동 실패: " + str(self.errors or self.results))
        return self

    def __exit__(self, kind, value, trace):
        self.runtime.handle_signal(signal.SIGTERM, None)
        self.thread.join(20)
        check(not self.thread.is_alive(), "시작 관리자 종료 실패")
        check(not self.errors, str(self.errors))
        check(all(child.process.poll() is not None for child in self.children), "자식 회수 누락")
        check(not (self.runtime_dir / "startup-recovery.sock").exists(), "소켓 정리 누락")
        check(self.before == self.hashes(), "합성 DB·이미지·설정 해시 변경")
        print("data_sha256=" + json.dumps(self.before, sort_keys=True), flush=True)
        print("scenario_cleanup=" + self.mode, flush=True)

    def request(self, method="GET", path="/migration/recovery", headers=None, body=None, port=8080, control=True):
        connection = (http.client.HTTPConnection("127.0.0.1", port, timeout=8) if port == 8080 else
                      http.client.HTTPSConnection("127.0.0.1", port, timeout=8, context=ssl._create_unverified_context()))
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            data = response.read()
            fields = dict((key.lower(), val) for key, val in response.getheaders())
            if control:
                check(fields.get("content-type") == "application/json", "제어 JSON 누락")
                check(fields.get("cache-control") == "no-store", "제어 캐시 차단 누락")
                check(fields.get("x-content-type-options") == "nosniff", "nosniff 누락")
                check("access-control-allow-origin" not in fields, "CORS 허용")
                data = json.loads(data)
            return response.status, data, fields
        finally:
            connection.close()

    def state(self):
        status, data, _ = self.request()
        check(status == 200, "조회 실패: " + str(status))
        return data

    def post(self, state, extra=None, port=8080):
        headers = {"X-SVRX-Recovery-Token": state["token"] or "absent",
                   "X-SVRX-Recovery-Generation": state["generation"] or "absent"}
        headers.update(extra or {})
        return self.request("POST", "/migration/restart", headers, port=port)

    def raw_socket(self, request, partial=False):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(4)
            client.connect(str(self.runtime_dir / "startup-recovery.sock"))
            started = time.monotonic()
            client.sendall(request)
            if partial:
                time.sleep(0.55)
            response = b""
            while True:
                try:
                    chunk = client.recv(16384)
                except ConnectionResetError:
                    # 초과 본문을 읽지 않고 거부하면 응답 뒤 연결이 reset될 수 있다.
                    if response:
                        break
                    raise
                if not chunk:
                    break
                response += chunk
            head, body = response.split(b"\r\n\r\n", 1)
            fields = dict(line.lower().split(b": ", 1) for line in head.split(b"\r\n")[1:])
            check(len(body) == int(fields[b"content-length"]), "불완전한 소켓 응답")
            check(fields[b"content-type"] == b"application/json", "소켓 JSON 누락")
            json.loads(body)
            return int(head.split(b" ", 2)[1]), time.monotonic() - started


def budget_and_http(root):
    with Scenario(root, "budget") as fixture:
        state = fixture.state()
        check(state["available"] and state["remaining_attempts"] == 3, "최초 수락 조건")
        for method, path in (("GET", "/migration/recovery"), ("POST", "/migration/restart")):
            for headers in ({"Content-Length": "x"}, {"X-Large": "x" * 16384}):
                check(fixture.request(method, path, headers)[0] == 400, "공개 Nginx 선행 파싱 거부")
        print("public_parser_rejection=PASS", flush=True)
        check(fixture.state()["token"] == state["token"] and len(fixture.attempts) == 1, "GET 부작용")
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            statuses = list(pool.map(lambda _: fixture.post(state)[0], range(3)))
        check(statuses.count(202) == 1, "동시 요청 단일 수락: " + str(statuses))
        accepted_at = fixture.runtime.recovery_policy._last_accepted_at
        wait_for(lambda: len(fixture.attempts) == 2 and (fixture.runtime_dir / "startup-recovery.sock").is_socket())
        current = fixture.state()
        check(current["reason"] == "cooldown" and current["remaining_attempts"] == 2, "실패 후 예산 보존")
        check(fixture.post(state)[0] == 403, "이전 토큰 재사용")
        check(fixture.request("POST", "/migration/restart")[0] == 403, "토큰 누락")
        status, data, headers = fixture.post(current)
        check(status == 429 and data["reason"] == "cooldown" and int(headers["retry-after"]) > 0, "30초 대기")
        bad_origins = ["null", "http://evil.test", "http://recovery.test:9999", "https://recovery.test,https://evil.test"]
        for origin in bad_origins:
            check(fixture.post(current, {"Host": "recovery.test:8443", "Origin": origin})[0] == 403, "잘못된 Origin 허용")
        for fetch_site in ("cross-site", "same-site", "none"):
            check(fixture.post(current, {"Sec-Fetch-Site": fetch_site})[0] == 403, "Fetch Metadata 우회")
        for port, host, origin in ((8080, "recovery.test:8080", "http://recovery.test:8080"),
                                   (8080, "recovery.test", "http://recovery.test:80"),
                                   (8443, "recovery.test:8443", "https://recovery.test:8443"),
                                   (8443, "recovery.test", "https://recovery.test:443")):
            check(fixture.post(current, {"Host": host, "Origin": origin}, port)[0] == 429, "Host·포트 보존 및 기본 포트 정규화")
        check(fixture.post(current, {"Host": "recovery.test:8443", "Origin": "https://recovery.test:8443"}, 8444)[0] == 403, "Host 변경 프록시 허용")
        check(fixture.post(current, {"Host": "recovery.test", "Origin": "https://evil.test", "X-Forwarded-Host": "evil.test", "X-Forwarded-Proto": "https"}, 8443)[0] == 403, "전달 헤더 위조")
        for method, path in (("OPTIONS", "/migration/restart"), ("GET", "/migration/restart"), ("POST", "/migration/recovery")):
            check(fixture.request(method, path)[0] == 405, "메서드 거부")
        check(fixture.request("POST", "/migration/restart", body="x")[0] == 400, "본문 거부")
        prefix = b"POST /migration/restart HTTP/1.1\r\nHost: recovery.test\r\n"
        for suffix in (b"X-SVRX-Recovery-Token: a\r\nX-SVRX-Recovery-Token: b\r\n\r\n",
                       b"Content-Length: x\r\n\r\n", b"Transfer-Encoding: chunked\r\n\r\n"):
            check(fixture.raw_socket(prefix + suffix)[0] == 400, "잘못된 소켓 헤더 수락")
        check(fixture.raw_socket(prefix + b"X-Large: " + b"x" * 8192)[0] == 431, "8KiB 제한 누락")
        heartbeat = fixture.store._status["heartbeat_at"]
        status, duration = fixture.raw_socket(prefix + b"X-Partial: ", partial=True)
        check(status == 408 and duration < 2, "부분 요청 기한 연장")
        wait_for(lambda: fixture.store._status["heartbeat_at"] != heartbeat, 7)
        check(len(fixture.attempts) == 2, "거부 요청의 작업자 생성 부작용")
        for path in ("/api/v2/version/", "/media/probe.png"):
            check(fixture.request(path=path, control=False)[0] == 503, "실패 게이트 우회")
        # 여섯 요청을 경로당 세 개씩 보내면 독립 예산 구현에서는 모두 통과한다.
        time.sleep(3)
        barrier = threading.Barrier(6)
        def mixed(index):
            barrier.wait()
            return fixture.request() if index % 2 == 0 else fixture.post(current)
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            mixed_results = list(pool.map(mixed, range(6)))
        limited = sum(data.get("reason") == "rate_limited" for _, data, _ in mixed_results)
        check(limited >= 1, "두 경로 공유 Nginx 예산 누락")
        print("shared_rate_budget_limited={}".format(limited), flush=True)
        for target in (2, 3):
            remaining = accepted_at + 30 - time.monotonic()
            print("실시간 쿨다운 검증: 다음 수락까지 {:.2f}초".format(max(0, remaining)), flush=True)
            if remaining > 0:
                time.sleep(remaining + 0.05)
            current = fixture.state()
            check(fixture.post(current)[0] == 202, "쿨다운 이후 수락 실패")
            new_time = fixture.runtime.recovery_policy._last_accepted_at
            check(new_time - accepted_at >= 30, "30초 미만 수락")
            accepted_at = new_time
            wait_for(lambda: len(fixture.attempts) == target + 1 and (fixture.runtime_dir / "startup-recovery.sock").is_socket())
        state = fixture.state()
        check(state["reason"] == "exhausted" and state["remaining_attempts"] == 0 and state["token"] is None, "3회 소진")
        check(fixture.post(state)[0] == 409, "소진 후 재시도")
        check(len(fixture.attempts) == 4 and len(set(item[1:] for item in fixture.attempts)) == 1, "단일 FD와 정확한 시도 수")
        print("budget_http=PASS attempts=4 accepted=3", flush=True)


def success_and_errors(root):
    with Scenario(root / "success", "timeout_success") as fixture:
        state = fixture.state()
        check(fixture.store._status["error_code"] == "worker_exit_timeout" and state["available"], "실제 15초 종료 기한 실패")
        check(fixture.post(state)[0] == 202, "종료 기한 후 수락")
        check(fixture.request()[0] == 503, "다음 기동 중 소켓 닫힘")
        wait_for(lambda: not Path(fixture.store.marker_path).exists(), 12)
        for path in ("/migration", "/migration/", "/migration/index.html"):
            status, _, headers = fixture.request(path=path, control=False)
            check(status == 302 and headers["location"] == "/", "정상 페이지 리디렉션")
        check(fixture.request()[0] == 409 and fixture.post(state)[0] == 409, "정상 상태 제어 거부")
        check(len(fixture.attempts) == 2 and len(set(item[1:] for item in fixture.attempts)) == 1, "복구 후 잠금 재사용")
        print("real_timeout_success=PASS attempts=2", flush=True)
    for index, mode in enumerate(("protocol", "error:legacy_database_invalid", "error:bootstrap_persistent_settings_invalid", "error:unsafe_storage_ownership")):
        with Scenario(root / ("error" + str(index)), mode) as fixture:
            state = fixture.state()
            check(not state["available"] and state["token"] is None, "금지 오류 토큰 노출")
            check(fixture.post(state)[0] == 409 and len(fixture.attempts) == 1, "금지 오류 수락")
            print("forbidden_error=PASS " + mode, flush=True)


def application():
    from http.server import BaseHTTPRequestHandler, HTTPServer
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"source_commit":"development","display_version":"development"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass
    HTTPServer(("127.0.0.1", 8000), Handler).serve_forever()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "app":
        application()
    else:
        verify_source()
        with tempfile.TemporaryDirectory(prefix="svrx-recovery-") as temporary:
            os.chmod(temporary, 0o755)
            root = Path(temporary)
            budget_and_http(root / "budget")
            success_and_errors(root)
        verify_source()
        print("startup_recovery_smoke=PASS", flush=True)
