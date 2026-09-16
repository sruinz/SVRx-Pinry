import errno
from http import HTTPStatus
import hmac
import ipaddress
import json
import math
import os
import re
import secrets
import select
import socket
import stat
import time
from urllib.parse import urlsplit


COOLDOWN_SECONDS = 30
MAX_ACCEPTED_ATTEMPTS = 3
RECOVERABLE_CODES = frozenset(("worker_exit_timeout", "gunicorn_start_failed"))
BLOCKED_REASONS = frozenset((
    "shutting_down", "failure_not_recoverable", "status_unverified",
    "identity_unverified", "worker_incomplete", "nginx_unavailable",
    "children_pending", "lock_or_gate_unverified",
))


class RecoveryPolicy:
    def __init__(self, clock=time.monotonic, token_factory=secrets.token_urlsafe):
        self._clock = clock
        self._token_factory = token_factory
        self._accepted_count = 0
        self._last_accepted_at = None
        self._failure_code = None
        self._generation = None
        self._token = None
        self._automatic_at = None

    @property
    def accepted_count(self):
        return self._accepted_count

    def enter_failure(self, code, automatic=False):
        self._failure_code = code
        self._generation = self._token_factory(32)
        self._token = self._token_factory(32)
        self._automatic_at = self._clock() + COOLDOWN_SECONDS if automatic else None

    def leave_failure(self):
        self._failure_code = None
        self._generation = None
        self._token = None
        self._automatic_at = None

    def snapshot(self, eligible):
        reason, retry_after = self._availability(eligible)
        available = reason == "available"
        payload = self._payload(
            available=available,
            reason=reason,
            retry_after=retry_after,
            disclose_token=reason in ("available", "cooldown"),
        )
        if isinstance(eligible, str) and eligible in BLOCKED_REASONS:
            payload["blocked_reason"] = eligible
        if self._automatic_at is not None and reason in ("available", "cooldown"):
            payload["automatic_retry_after_seconds"] = max(
                retry_after, 0, int(math.ceil(self._automatic_at - self._clock())),
            )
        return payload

    def accept_automatic(self, eligible):
        if self._automatic_at is None or self._clock() < self._automatic_at:
            return False
        if self._availability(eligible)[0] != "available":
            return False
        status, _ = self.accept(self._token, self._generation, eligible)
        return status == 202

    def accept(self, token, generation, eligible):
        reason = self._base_unavailability(eligible)
        if reason is not None:
            return 409, self._payload(
                available=False,
                reason=reason,
                retry_after=0,
                disclose_token=False,
            )

        if self._accepted_count >= MAX_ACCEPTED_ATTEMPTS:
            return 409, self._payload(
                available=False,
                reason="exhausted",
                retry_after=0,
                disclose_token=False,
            )

        if not isinstance(token, str) or not isinstance(generation, str):
            return 403, self._invalid_token_payload()
        token_matches = hmac.compare_digest(token, self._token)
        generation_matches = hmac.compare_digest(generation, self._generation)
        if not token_matches or not generation_matches:
            return 403, self._invalid_token_payload()

        retry_after = self._retry_after_seconds()
        if retry_after:
            return 429, self._payload(
                available=False,
                reason="cooldown",
                retry_after=retry_after,
                disclose_token=False,
            )

        self._accepted_count += 1
        self._last_accepted_at = self._clock()
        self._token = None
        self._automatic_at = None
        return 202, self._payload(
            available=False,
            reason="accepted",
            retry_after=COOLDOWN_SECONDS,
            disclose_token=False,
        )

    def _availability(self, eligible):
        reason = self._base_unavailability(eligible)
        if reason is not None:
            return reason, 0
        if self._accepted_count >= MAX_ACCEPTED_ATTEMPTS:
            return "exhausted", 0

        retry_after = self._retry_after_seconds()
        if retry_after:
            return "cooldown", retry_after
        return "available", 0

    def _base_unavailability(self, eligible):
        if (
            self._failure_code not in RECOVERABLE_CODES
            or self._generation is None
            or self._token is None
        ):
            return "unavailable"
        if eligible is not True:
            return "unsafe_state"
        return None

    def _retry_after_seconds(self):
        if self._last_accepted_at is None:
            return 0
        elapsed = self._clock() - self._last_accepted_at
        return max(0, int(math.ceil(COOLDOWN_SECONDS - elapsed)))

    def _invalid_token_payload(self):
        return self._payload(
            available=False,
            reason="invalid_token",
            retry_after=0,
            disclose_token=False,
        )

    def _payload(self, available, reason, retry_after, disclose_token):
        return {
            "schema_version": 1,
            "available": available,
            "reason": reason,
            "remaining_attempts": max(
                0, MAX_ACCEPTED_ATTEMPTS - self._accepted_count
            ),
            "retry_after_seconds": retry_after,
            "generation": self._generation,
            "token": self._token if disclose_token else None,
        }


class _RequestError(Exception):
    def __init__(self, status):
        self.status = status


class RecoveryServer:
    def __init__(self, runtime_directory, owner_uid, nginx_gid, policy,
                 eligibility_check, clock=time.monotonic):
        self.runtime_directory = os.fspath(runtime_directory)
        self.path = os.path.join(self.runtime_directory, "startup-recovery.sock")
        self.owner_uid = owner_uid
        self.nginx_gid = nginx_gid
        self.policy = policy
        self.eligibility_check = eligibility_check
        self.clock = clock
        self._listener = None
        self._identity = None

    def open(self):
        if self._listener is not None:
            raise OSError(errno.EALREADY, "recovery socket already open")
        parent = os.lstat(self.runtime_directory)
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != self.owner_uid
                or parent.st_mode & 0o022):
            raise OSError(errno.EACCES, "unsafe recovery directory")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener = listener
        try:
            # bind 자체가 기존 경로를 거부한다. 생성 순간부터 접근을 제한한다.
            previous_umask = os.umask(0o177)
            try:
                listener.bind(self.path)
            finally:
                os.umask(previous_umask)
            created = os.lstat(self.path)
            self._identity = (created.st_dev, created.st_ino)
            if not stat.S_ISSOCK(created.st_mode) or created.st_uid != self.owner_uid:
                raise OSError(errno.EACCES, "unexpected recovery socket owner")
            os.chmod(self.path, 0o600)
            os.chown(self.path, -1, self.nginx_gid)
            os.chmod(self.path, 0o660)
            listener.setblocking(False)
            listener.listen(4)
        except OSError:
            self.close()
            raise

    def close(self):
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        identity, self._identity = self._identity, None
        if identity is None:
            return
        try:
            current = os.lstat(self.path)
            if stat.S_ISSOCK(current.st_mode) and (current.st_dev, current.st_ino) == identity:
                os.unlink(self.path)
        except FileNotFoundError:
            pass

    def poll_once(self):
        if self._listener is None:
            return False
        try:
            client, _ = self._listener.accept()
        except (BlockingIOError, InterruptedError):
            return False
        accepted = False
        deadline = self.clock() + 0.5
        with client:
            client.setblocking(False)
            try:
                method, path, headers = self._read_request(client, deadline)
                self._validate_source(headers)
                expected_method = {
                    "/migration/recovery": "GET", "/migration/restart": "POST",
                }.get(path)
                if expected_method is None:
                    raise _RequestError(404)
                if method != expected_method:
                    raise _RequestError(405)
                if self.clock() >= deadline:
                    raise _RequestError(408)
                eligible = self._eligible()
                if self.clock() >= deadline:
                    raise _RequestError(408)
                if method == "GET":
                    status, payload = 200, self.policy.snapshot(eligible)
                else:
                    status, payload = self.policy.accept(
                        headers.get("x-svrx-recovery-token"),
                        headers.get("x-svrx-recovery-generation"), eligible,
                    )
                    accepted = status == 202
            except _RequestError as error:
                status, payload = error.status, {"reason": HTTPStatus(error.status).phrase}
            except OSError:
                return accepted
            try:
                self._send_response(client, status, payload, deadline)
            except (OSError, _RequestError):
                pass
        return accepted

    def _eligible(self):
        try:
            result = self.eligibility_check()
            if isinstance(result, str) and result in BLOCKED_REASONS:
                return result
            return result is True
        except Exception:
            return False

    def _wait(self, client, deadline, writing=False):
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise _RequestError(408)
        readable, writable, _ = select.select(
            [] if writing else [client], [client] if writing else [], [], remaining,
        )
        if not (readable or writable) or self.clock() >= deadline:
            raise _RequestError(408)

    def _read_request(self, client, deadline):
        data = bytearray()
        while True:
            self._wait(client, deadline)
            try:
                chunk = client.recv(8193 - len(data))
            except (BlockingIOError, InterruptedError):
                continue
            if not chunk:
                raise _RequestError(400)
            data.extend(chunk)
            end = data.find(b"\r\n\r\n")
            header_end = end + 4 if end >= 0 else len(data)
            if header_end > 8192:
                raise _RequestError(431)
            if re.search(rb"(?<!\r)\n|\r(?!\n|$)", data):
                raise _RequestError(400)
            if end >= 0:
                if len(data) != header_end:
                    raise _RequestError(400)
                break
        try:
            lines = bytes(data[:end]).decode("ascii").split("\r\n")
        except UnicodeDecodeError:
            raise _RequestError(400)
        match = re.fullmatch(
            r"([!#$%&'*+.^_`|~0-9A-Za-z-]+) ([^\x00-\x20\x7f]+) HTTP/1\.[01]",
            lines[0],
        )
        if match is None:
            raise _RequestError(400)
        method, path = match.groups()
        headers = {}
        for line in lines[1:]:
            name, separator, value = line.partition(":")
            if not separator or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
                raise _RequestError(400)
            if any(ord(char) < 32 and char != "\t" or ord(char) == 127 for char in value):
                raise _RequestError(400)
            name = name.lower()
            if name in headers:
                raise _RequestError(400)
            headers[name] = value.strip(" \t")
        if "transfer-encoding" in headers or headers.get("content-length", "0") != "0":
            raise _RequestError(400)
        return method, path, headers

    @staticmethod
    def _authority(value, default_port):
        if not value or any(char in value for char in "/?#@,\\ \t\r\n"):
            raise ValueError("invalid authority")
        parsed = urlsplit("//" + value)
        if not parsed.hostname or value.endswith(":"):
            raise ValueError("invalid host")
        if value.startswith("["):
            ipaddress.IPv6Address(parsed.hostname)
            suffix = value[value.index("]") + 1:]
            if suffix and not re.fullmatch(r":[0-9]+", suffix):
                raise ValueError("invalid port")
        elif not re.fullmatch(r"[A-Za-z0-9.-]+(?::[0-9]+)?", value):
            raise ValueError("invalid host")
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("invalid port")
        return parsed.hostname.lower(), port if port is not None else default_port

    def _validate_source(self, headers):
        host = headers.get("host", "")
        try:
            self._authority(host, 80)
        except ValueError:
            raise _RequestError(400)
        if headers.get("sec-fetch-site", "same-origin") != "same-origin":
            raise _RequestError(403)
        if "origin" not in headers:
            return
        origin = headers["origin"]
        try:
            parsed = urlsplit(origin)
            if parsed.scheme not in ("http", "https") or not origin.startswith(parsed.scheme + "://"):
                raise ValueError("invalid origin scheme")
            authority = origin[len(parsed.scheme) + 3:]
            default_port = 80 if parsed.scheme == "http" else 443
            if self._authority(authority, default_port) != self._authority(host, default_port):
                raise ValueError("different origin")
        except ValueError:
            raise _RequestError(403)

    def _send_response(self, client, status, payload, deadline):
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = [
            "HTTP/1.1 {} {}".format(status, HTTPStatus(status).phrase),
            "Content-Type: application/json", "Cache-Control: no-store",
            "X-Content-Type-Options: nosniff", "Connection: close",
            "Content-Length: {}".format(len(body)),
        ]
        if status == 429:
            headers.append("Retry-After: {}".format(payload["retry_after_seconds"]))
        response = ("\r\n".join(headers) + "\r\n\r\n").encode("ascii") + body
        # 기한 만료 응답은 추가 대기 없이 한 번만 비차단 전송한다.
        if self.clock() >= deadline:
            client.send(response)
            return
        while response:
            self._wait(client, deadline, writing=True)
            try:
                sent = client.send(response)
            except (BlockingIOError, InterruptedError):
                continue
            if not sent:
                return
            response = response[sent:]
