#!/usr/bin/env bash
set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
nginx_binary="${NGINX_BINARY:-nginx}"

if ! command -v "$nginx_binary" >/dev/null 2>&1; then
    echo "SKIP: 실제 Nginx 실행 파일을 찾을 수 없습니다." >&2
    exit 77
fi

for dependency in curl python3 sed; do
    if ! command -v "$dependency" >/dev/null 2>&1; then
        echo "SKIP: $dependency 실행 파일을 찾을 수 없습니다." >&2
        exit 77
    fi
done

temporary_root="$(mktemp -d "${TMPDIR:-/tmp}/svrx-nginx-contract.XXXXXX")"
nginx_pid=""
upstream_pid=""

cleanup() {
    if [[ -n "$nginx_pid" ]] && kill -0 "$nginx_pid" 2>/dev/null; then
        kill -TERM "$nginx_pid" 2>/dev/null || true
        wait "$nginx_pid" 2>/dev/null || true
    fi
    if [[ -n "$upstream_pid" ]] && kill -0 "$upstream_pid" 2>/dev/null; then
        kill -TERM "$upstream_pid" 2>/dev/null || true
        wait "$upstream_pid" 2>/dev/null || true
    fi
    rm -rf "$temporary_root"
}
trap cleanup EXIT INT TERM

available_port() {
    python3 -c 'import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()'
}

listen_port="$(available_port)"
upstream_port="$(available_port)"
runtime_directory="$temporary_root/run"
spa_directory="$temporary_root/spa"
migration_directory="$temporary_root/docker/migration"
data_directory="$temporary_root/data/static"
ready_directory="$temporary_root/data/exports/ready"
mkdir -p "$runtime_directory" "$spa_directory" "$migration_directory" \
    "$data_directory/media" "$ready_directory" "$temporary_root/logs"

cp -R "$repository_root/docker/migration/." "$migration_directory/"
printf '%s\n' '<!doctype html><title>application</title>APP SHELL' \
    > "$spa_directory/index.html"
printf '%s\n' 'self.addEventListener("fetch", function () {});' \
    > "$spa_directory/service-worker.js"
printf '%s\n' 'static fixture' > "$data_directory/probe.txt"
printf '%s\n' 'media fixture' > "$data_directory/media/probe.txt"
known_zip="$temporary_root/known.zip"
outside_zip="$temporary_root/outside.zip"
printf '%s\n' 'PK known protected export bytes' > "$known_zip"
printf '%s\n' 'PK forbidden symlink target bytes' > "$outside_zip"
cp "$known_zip" "$ready_directory/test.zip"
ln -s "$outside_zip" "$ready_directory/symlink.zip"
printf '%s\n' '{"state":"migrating"}' \
    > "$runtime_directory/migration-status.json"
: > "$runtime_directory/maintenance"
: > "$temporary_root/upstream.requests"

cat > "$temporary_root/fake_upstream.py" <<'PY'
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
import sys


class Handler(BaseHTTPRequestHandler):
    def _respond(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)
        with open(sys.argv[2], "a") as capture:
            capture.write("{} {}\n".format(self.command, self.path))
        protected_exports = {
            "/api/test-protected-export/": "test.zip",
            "/api/test-protected-export-symlink/": "symlink.zip",
        }
        if self.path in protected_exports:
            self.send_response(200)
            self.send_header(
                "X-Accel-Redirect",
                "/__protected_exports/{}".format(
                    protected_exports[self.path]
                ),
            )
            self.send_header(
                "Content-Disposition",
                'attachment; filename="pinry-export.zip"',
            )
            self.send_header("Cache-Control", "private, no-store")
            self.send_header("Content-Type", "application/zip")
            self.end_headers()
            return
        body = json.dumps({
            "source_commit": "contract",
            "display_version": "contract",
            "path": self.path,
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    do_GET = _respond
    do_HEAD = _respond
    do_POST = _respond
    do_PUT = _respond

    def log_message(self, _format, *_arguments):
        return


HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
PY

python3 "$temporary_root/fake_upstream.py" \
    "$upstream_port" "$temporary_root/upstream.requests" &
upstream_pid=$!

sed \
    -e "s/listen 80 default;/listen 127.0.0.1:${listen_port};/" \
    -e "s#/pinry/pinry-spa/dist/#${spa_directory}/#g" \
    -e "s#/pinry/docker/migration/#${migration_directory}/#g" \
    -e "s#/pinry/docker#${temporary_root}/docker#g" \
    -e "s#/run/svrx-pinry#${runtime_directory}#g" \
    -e "s#/data/static/media#${data_directory}/media#g" \
    -e "s#/data/static#${data_directory}#g" \
    -e "s#/data/exports/ready/#${ready_directory}/#g" \
    -e "s#127\.0\.0\.1:8000#127.0.0.1:${upstream_port}#g" \
    "$repository_root/docker/nginx/sites-enabled/default" \
    > "$temporary_root/server.conf"

cat > "$temporary_root/nginx.conf" <<EOF
worker_processes 1;
pid $temporary_root/nginx.pid;
error_log $temporary_root/logs/main-error.log warn;
events { worker_connections 64; }
http {
    include mime.types;
    default_type application/octet-stream;
    include $temporary_root/server.conf;
}
EOF

mime_source=""
for candidate in /etc/nginx/mime.types /usr/local/etc/nginx/mime.types \
    /opt/homebrew/etc/nginx/mime.types; do
    if [[ -f "$candidate" ]]; then
        mime_source="$candidate"
        break
    fi
done
if [[ -z "$mime_source" ]]; then
    printf '%s\n' 'types { text/html html; text/css css; application/javascript js; application/json json; }' \
        > "$temporary_root/mime.types"
else
    cp "$mime_source" "$temporary_root/mime.types"
fi

"$nginx_binary" -p "$temporary_root/" -c "$temporary_root/nginx.conf" \
    -g 'daemon off;' > "$temporary_root/logs/stdout.log" \
    2> "$temporary_root/logs/stderr.log" &
nginx_pid=$!

base_url="http://127.0.0.1:${listen_port}"
for _attempt in $(seq 1 100); do
    if curl --silent --fail --output /dev/null "$base_url/healthz"; then
        break
    fi
    if ! kill -0 "$nginx_pid" 2>/dev/null; then
        cat "$temporary_root/logs/stderr.log" >&2
        exit 1
    fi
    sleep 0.05
done

headers="$temporary_root/headers"
body="$temporary_root/body"
status=""

request() {
    local method="$1"
    local path="$2"
    local data="${3:-}"
    local curl_arguments=(--silent --show-error --dump-header "$headers" \
        --output "$body")
    if [[ "$method" == HEAD ]]; then
        curl_arguments+=(--head)
    else
        curl_arguments+=(--request "$method")
    fi
    if [[ -n "$data" && "$method" != HEAD ]]; then
        curl_arguments+=(--data-binary "$data")
    fi
    status="$(curl "${curl_arguments[@]}" --write-out '%{http_code}' \
        "$base_url$path")"
}

assert_status() {
    local expected="$1"
    if [[ "$status" != "$expected" ]]; then
        echo "expected HTTP $expected, received $status" >&2
        cat "$headers" >&2
        cat "$body" >&2
        exit 1
    fi
}

assert_header() {
    local name="$1"
    local expected="$2"
    if ! awk -v name="$name" -v expected="$expected" '
        BEGIN { IGNORECASE = 1; found = 0 }
        {
            sub(/\r$/, "")
            separator = index($0, ":")
            if (separator == 0) next
            actual_name = substr($0, 1, separator - 1)
            actual_value = substr($0, separator + 1)
            sub(/^[[:space:]]+/, "", actual_value)
            if (tolower(actual_name) == tolower(name) && actual_value == expected) {
                found = 1
            }
        }
        END { exit(found ? 0 : 1) }
    ' "$headers"; then
        echo "missing header $name: $expected" >&2
        cat "$headers" >&2
        exit 1
    fi
}

for method in GET HEAD; do
    request "$method" /migration
    assert_status 308
    assert_header Location "$base_url/migration/"
    assert_header Cache-Control no-store

    for path in /migration/ /migration-status.json /healthz; do
        request "$method" "$path"
        assert_status 200
        assert_header Allow 'GET, HEAD'
        assert_header Cache-Control no-store
    done

    request "$method" /migration/
    assert_status 200
    assert_header Content-Type 'text/html; charset=utf-8'

    request "$method" /migration/migration.css
    assert_status 200
    assert_header Content-Type 'text/css; charset=utf-8'

    request "$method" /migration/migration.js
    assert_status 200
    assert_header Content-Type 'application/javascript; charset=utf-8'

    request "$method" /readyz
    assert_status 503
    assert_header Retry-After 5
    assert_header Cache-Control no-store
done

for method in POST PUT; do
    for path in /migration /migration/ /migration-status.json /healthz /readyz; do
        request "$method" "$path"
        assert_status 405
        assert_header Allow 'GET, HEAD'
        assert_header Cache-Control no-store
    done
done

for method in GET HEAD; do
    request "$method" /service-worker.js
    assert_status 200
    assert_header Content-Type application/javascript
    assert_header Cache-Control no-store
    assert_header Service-Worker-Allowed /
    assert_header X-Content-Type-Options nosniff
done
for method in POST PUT; do
    request "$method" /service-worker.js
    assert_status 405
    assert_header Allow 'GET, HEAD'
done

for method in GET HEAD; do
    for path in / /boards/example; do
        request "$method" "$path"
        assert_status 200
        assert_header Cache-Control no-store
        assert_header Content-Type 'text/html; charset=utf-8'
        if [[ "$method" == GET ]] && ! grep -q '기존 Pinry 데이터를 이전하고 있습니다.' "$body"; then
            echo "maintenance fallback body is missing" >&2
            exit 1
        fi
    done
done

for method in POST PUT; do
    for path in / /boards/example; do
        request "$method" "$path"
        assert_status 503
        assert_header Retry-After 5
        assert_header Cache-Control no-store
    done
done

for method in GET HEAD POST PUT; do
    for path in /api/v2/version/ /api/v2/pins/batch/ /admin/ \
        /media/probe.txt /static/probe.txt; do
        request "$method" "$path" '{}'
        assert_status 503
        assert_header Retry-After 5
        assert_header Cache-Control no-store
    done
done

if [[ -s "$temporary_root/upstream.requests" ]]; then
    echo "maintenance gate leaked a request to the upstream" >&2
    cat "$temporary_root/upstream.requests" >&2
    exit 1
fi

rm "$runtime_directory/maintenance"

request GET /service-worker.js
assert_status 200
assert_header Content-Type application/javascript
assert_header Cache-Control no-store
assert_header Service-Worker-Allowed /

request GET /
assert_status 200
if ! grep -q 'APP SHELL' "$body"; then
    echo "marker-off SPA response is missing" >&2
    exit 1
fi

request POST /api/v2/pins/batch/ '{"items":[]}'
assert_status 200
request GET /readyz
assert_status 200

request GET /__protected_exports/test.zip
assert_status 404
if cmp -s "$body" "$known_zip"; then
    echo "direct protected export request exposed archive bytes" >&2
    exit 1
fi

request GET /api/test-protected-export/
assert_status 200
assert_header Content-Disposition 'attachment; filename="pinry-export.zip"'
assert_header Cache-Control 'private, no-store'
assert_header Content-Type application/zip
if ! cmp -s "$body" "$known_zip"; then
    echo "authorized protected export response did not match archive" >&2
    exit 1
fi

request GET /api/test-protected-export-symlink/
if [[ "$status" != 403 && "$status" != 404 ]]; then
    echo "protected export symlink returned HTTP $status" >&2
    cat "$headers" >&2
    exit 1
fi
if cmp -s "$body" "$outside_zip"; then
    echo "protected export symlink exposed target bytes" >&2
    exit 1
fi

if ! grep -Fxq 'POST /api/v2/pins/batch/' \
        "$temporary_root/upstream.requests"; then
    echo "marker-off exact batch request did not reach the upstream" >&2
    cat "$temporary_root/upstream.requests" >&2
    exit 1
fi
if ! grep -Fxq 'GET /api/v2/version/' \
        "$temporary_root/upstream.requests"; then
    echo "marker-off readiness request did not reach the upstream" >&2
    cat "$temporary_root/upstream.requests" >&2
    exit 1
fi

echo "PASS: Nginx 유지보수 게이트 실제 요청 계약"
