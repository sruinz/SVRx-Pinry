import json
import os
from pathlib import Path
import socket
import stat
import tempfile
import threading
import time
import unittest
from unittest import mock

if __package__:
    from .test_startup_recovery import startup_recovery, RecoveryPolicy
else:
    from test_startup_recovery import startup_recovery, RecoveryPolicy


class RecoverySocketTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(startup_recovery, "RecoveryServer"))
        self.directory = tempfile.TemporaryDirectory(prefix="recovery-", dir="/tmp")
        self.addCleanup(self.directory.cleanup)
        self.policy = RecoveryPolicy()
        self.policy.enter_failure("gunicorn_start_failed")
        self.eligible = True
        self.server = startup_recovery.RecoveryServer(
            self.directory.name, os.getuid(), os.getgid(), self.policy,
            lambda: self.eligible,
        )
        self.addCleanup(self.server.close)
        self.server.open()
        self.path = os.path.join(self.directory.name, "startup-recovery.sock")

    def client(self):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(client.close)
        client.settimeout(2)
        client.connect(self.path)
        return client

    def request(self, raw):
        client = self.client()
        errors = []

        def send_request():
            try:
                client.sendall(raw)
            except OSError as error:
                errors.append(error)

        sender = threading.Thread(target=send_request)
        sender.start()
        try:
            accepted = self.server.poll_once()
        finally:
            sender.join(2)
        self.assertFalse(sender.is_alive())
        self.assertEqual(errors, [])
        response = b""
        while True:
            chunk = client.recv(8192)
            if not chunk:
                break
            response += chunk
        head, body = response.split(b"\r\n\r\n", 1)
        status = int(head.split(b" ")[1])
        self.assertIn(b"Cache-Control: no-store", head)
        self.assertIn(b"X-Content-Type-Options: nosniff", head)
        self.assertIn(b"Content-Type: application/json", head)
        self.assertIn(b"Connection: close", head)
        self.assertNotIn(b"Access-Control-Allow", head)
        return accepted, status, json.loads(body), head

    def post(self, state):
        return (
            "POST /migration/restart HTTP/1.1\r\nHost: pinry.test\r\n"
            "X-SVRX-Recovery-Token: {token}\r\n"
            "X-SVRX-Recovery-Generation: {generation}\r\n\r\n"
        ).format(**state).encode("ascii")

    def test_get_then_post_consumes_once_and_duplicate_is_refused(self):
        accepted, status, state, _ = self.request(
            b"GET /migration/recovery HTTP/1.1\r\nHost: pinry.test\r\n\r\n"
        )
        self.assertFalse(accepted)
        self.assertEqual(status, 200)
        self.assertTrue(state["available"])
        self.assertEqual(self.policy.accepted_count, 0)
        self.assertEqual(self.request(self.post(state))[:2], (True, 202))
        self.assertEqual(self.request(self.post(state))[:2], (False, 409))
        self.assertEqual(self.policy.accepted_count, 1)

    def test_disconnected_client_does_not_undo_acceptance(self):
        raw = self.post(self.policy.snapshot(True))
        client = self.client()
        client.sendall(raw)
        client.close()
        self.assertTrue(self.server.poll_once())
        self.assertEqual(self.policy.accepted_count, 1)
        self.assertFalse(self.server.poll_once())

    def test_poll_processes_only_one_waiting_request(self):
        raw = self.post(self.policy.snapshot(True))
        first, second = self.client(), self.client()
        first.sendall(raw)
        second.sendall(raw)
        self.assertTrue(self.server.poll_once())
        self.assertIn(b"202", first.recv(8192).split(b"\r\n", 1)[0])
        second.setblocking(False)
        with self.assertRaises(BlockingIOError):
            second.recv(8192)
        self.assertFalse(self.server.poll_once())
        self.assertIn(b"409", second.recv(8192).split(b"\r\n", 1)[0])
        self.assertEqual(self.policy.accepted_count, 1)

    def test_cooldown_response_has_retry_after_and_preserves_budget(self):
        state = self.policy.snapshot(True)
        self.assertTrue(self.request(self.post(state))[0])
        self.policy.enter_failure("gunicorn_start_failed")
        accepted, status, payload, headers = self.request(self.post(self.policy.snapshot(True)))
        self.assertFalse(accepted)
        self.assertEqual(status, 429)
        self.assertEqual(payload["retry_after_seconds"], 30)
        self.assertIn(b"Retry-After: 30", headers)
        self.assertEqual(self.policy.accepted_count, 1)

    def test_missing_credentials_and_eligibility_exception_fail_closed(self):
        self.assertEqual(self.request(
            b"POST /migration/restart HTTP/1.1\r\nHost: pinry.test\r\n\r\n"
        )[:2], (False, 403))
        self.eligible = False
        self.assertEqual(self.request(self.post(self.policy.snapshot(True)))[:2], (False, 409))
        def unsafe():
            raise OSError("unsafe")
        self.server.eligibility_check = unsafe
        self.assertEqual(self.request(self.post(self.policy.snapshot(True)))[:2], (False, 409))
        _, status, payload, _ = self.request(b"GET /migration/recovery HTTP/1.1\r\nHost: pinry.test\r\n\r\n")
        self.assertEqual(status, 200)
        self.assertIsNone(payload["token"])

    def test_malformed_requests_do_not_consume_budget(self):
        cases = (
            (b"GET /other HTTP/1.1\r\nHost: pinry.test\r\n\r\n", 404),
            (b"OPTIONS /migration/restart HTTP/1.1\r\nHost: pinry.test\r\n\r\n", 405),
            (b"GET /migration/restart HTTP/1.1\r\nHost: pinry.test\r\n\r\n", 405),
            (b"get /migration/recovery HTTP/1.1\r\nHost: pinry.test\r\n\r\n", 405),
            (b"CUSTOM-METHOD /migration/recovery HTTP/1.1\r\nHost: pinry.test\r\n\r\n", 405),
            (b"GET  /migration/recovery HTTP/1.1\r\nHost: pinry.test\r\n\r\n", 400),
            (b"GET /migration/recovery HTTP/1.1\nHost: pinry.test\n\n", 400),
            (b"GET /migration/recovery HTTP/1.1\r\nHost: pinry.test\r\n X: a\r\n\r\n", 400),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(self.request(raw)[:2], (False, expected))
        for header in (
            b"Content-Length: 1", b"Content-Length: -1", b"Content-Length: +0",
            b"Content-Length: 0\r\nContent-Length: 0", b"Transfer-Encoding: chunked",
            b"hOsT: pinry.test", b"Origin: http://pinry.test\r\norigin: http://pinry.test",
            b"Sec-Fetch-Site: same-origin\r\nsec-fetch-site: same-origin",
            b"X-SVRX-Recovery-Token: a\r\nx-svrx-recovery-token: a",
            b"X-SVRX-Recovery-Generation: a\r\nx-svrx-recovery-generation: a",
            b"Host : pinry.test", b"X: a\x00b", b"X: \xff",
        ):
            with self.subTest(header=header):
                raw = b"GET /migration/recovery HTTP/1.1\r\nHost: pinry.test\r\n" + header + b"\r\n\r\n"
                self.assertEqual(self.request(raw)[:2], (False, 400))
        raw = b"GET /migration/recovery HTTP/1.1\r\nHost: pinry.test\r\n\r\nx"
        self.assertEqual(self.request(raw)[:2], (False, 400))
        self.assertEqual(self.policy.accepted_count, 0)

    def test_origin_and_fetch_metadata_validation_also_protect_get(self):
        for origin in (
            "null", "http://evil.test", "https://pinry.test:444", "ftp://pinry.test",
            "http://pinry.test/", "http://user@pinry.test", "http://pinry.test#x",
            "http://pinry.test?x", "http://pinry.test, http://evil.test",
            "http://pinry.test:", "http://pinry.test:bad",
        ):
            with self.subTest(origin=origin):
                raw = ("GET /migration/recovery HTTP/1.1\r\nHost: pinry.test\r\nOrigin: " + origin + "\r\n\r\n").encode()
                self.assertEqual(self.request(raw)[:2], (False, 403))
        for site in ("cross-site", "same-site", "none", "same-origin, same-origin"):
            raw = ("GET /migration/recovery HTTP/1.1\r\nHost: pinry.test\r\nSec-Fetch-Site: " + site + "\r\n\r\n").encode()
            self.assertEqual(self.request(raw)[:2], (False, 403))
        for host, origin in (("pinry.test:80", "http://PINRY.test"), ("pinry.test", "https://pinry.test:443"), ("[::1]:80", "http://[::1]")):
            raw = ("GET /migration/recovery HTTP/1.1\r\nHost: " + host + "\r\nOrigin: " + origin + "\r\nSec-Fetch-Site: same-origin\r\n\r\n").encode()
            self.assertEqual(self.request(raw)[:2], (False, 200))

    def test_invalid_host_is_rejected_without_origin(self):
        for host in ("", "pinry.test/path", "a@pinry.test", "a,b", "pinry.test:", "pinry.test:99999", "pinry.test#x", "pinry.test?x", "a b"):
            raw = ("GET /migration/recovery HTTP/1.1\r\nHost: " + host + "\r\n\r\n").encode()
            self.assertEqual(self.request(raw)[:2], (False, 400))

    def test_header_size_limit(self):
        prefix = b"GET /migration/recovery HTTP/1.1\r\nHost: pinry.test\r\nX: "
        raw = prefix + b"a" * (8192 - len(prefix) - 4) + b"\r\n\r\n"
        self.assertEqual(self.request(raw)[:2], (False, 200))
        self.assertEqual(self.request(raw[:-4] + b"a\r\n\r\n")[:2], (False, 431))

    def test_idle_poll_is_immediate_and_partial_request_has_total_deadline(self):
        start = time.monotonic()
        self.assertFalse(self.server.poll_once())
        self.assertLess(time.monotonic() - start, 0.1)
        client = self.client()
        stopped = threading.Event()

        def send_slowly():
            while not stopped.is_set():
                try:
                    client.sendall(b"G")
                except OSError:
                    break
                stopped.wait(0.08)

        sender = threading.Thread(target=send_slowly)
        sender.start()
        start = time.monotonic()
        try:
            self.assertFalse(self.server.poll_once())
            elapsed = time.monotonic() - start
            self.assertGreaterEqual(elapsed, 0.4)
            self.assertLess(elapsed, 0.8)
        finally:
            stopped.set()
            sender.join(2)
        self.assertFalse(sender.is_alive())
        self.assertIn(b"408", client.recv(8192).split(b"\r\n", 1)[0])
        self.assertEqual(self.policy.accepted_count, 0)

    def test_response_backpressure_uses_remaining_request_deadline(self):
        # 과대 시험 토큰으로 실제 송신 버퍼를 채우고 비수신 클라이언트를 재현한다.
        self.policy = RecoveryPolicy(token_factory=lambda size: "a" * 2000000)
        self.policy.enter_failure("gunicorn_start_failed")
        self.server.policy = self.policy
        client = self.client()
        client.sendall(b"GET /migration/recovery HTTP/1.1\r\nHost: pinry.test\r\n")

        def finish_headers():
            time.sleep(0.2)
            client.sendall(b"\r\n")

        sender = threading.Thread(target=finish_headers)
        sender.start()
        start = time.monotonic()
        try:
            self.assertFalse(self.server.poll_once())
            elapsed = time.monotonic() - start
            self.assertGreaterEqual(elapsed, 0.4)
            self.assertLess(elapsed, 0.65)
        finally:
            sender.join(2)
        self.assertFalse(sender.is_alive())

    def test_socket_permissions_close_and_replacement_preservation(self):
        metadata = os.lstat(self.path)
        self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o660)
        self.assertEqual(metadata.st_gid, os.getgid())
        self.assertEqual(stat.S_IMODE(os.stat(self.directory.name).st_mode), 0o700)
        os.unlink(self.path)
        Path(self.path).write_text("replacement")
        self.server.close()
        self.assertEqual(Path(self.path).read_text(), "replacement")

    def test_close_preserves_replacement_symlink_and_socket(self):
        os.unlink(self.path)
        replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(replacement.close)
        replacement.bind(self.path)
        before = os.lstat(self.path)
        self.server.close()
        self.assertEqual(os.lstat(self.path).st_ino, before.st_ino)
        os.unlink(self.path)
        self.server.open()
        os.unlink(self.path)
        os.symlink("missing", self.path)
        self.server.close()
        self.assertTrue(os.path.islink(self.path))

    def test_group_setup_failure_removes_only_created_socket(self):
        self.server.close()
        with mock.patch.object(startup_recovery.os, "chown", side_effect=OSError("denied")):
            with self.assertRaises(OSError):
                self.server.open()
        self.assertFalse(os.path.lexists(self.path))
        self.assertFalse(self.server.poll_once())

    def test_existing_paths_and_untrusted_parent_are_preserved(self):
        self.server.close()
        for kind in ("file", "symlink", "socket"):
            with self.subTest(kind=kind):
                other = None
                if kind == "file":
                    Path(self.path).write_text("existing")
                elif kind == "symlink":
                    os.symlink("missing", self.path)
                else:
                    other = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    other.bind(self.path)
                before = os.lstat(self.path)
                try:
                    with self.assertRaises(OSError):
                        self.server.open()
                    self.server.close()
                    self.assertEqual(os.lstat(self.path).st_ino, before.st_ino)
                finally:
                    if other:
                        other.close()
                    os.unlink(self.path)
        os.chmod(self.directory.name, 0o770)
        with self.assertRaises(OSError):
            self.server.open()
        os.chmod(self.directory.name, 0o700)
        self.server.owner_uid = os.getuid() + 1
        with self.assertRaises(OSError):
            self.server.open()
        self.server.owner_uid = os.getuid()
        os.chmod(self.directory.name, 0o702)
        with self.assertRaises(OSError):
            self.server.open()
        os.chmod(self.directory.name, 0o700)
        alias = self.directory.name + "-link"
        os.symlink(self.directory.name, alias)
        try:
            server = startup_recovery.RecoveryServer(alias, os.getuid(), os.getgid(), self.policy, lambda: True)
            with self.assertRaises(OSError):
                server.open()
        finally:
            os.unlink(alias)


if __name__ == "__main__":
    unittest.main()
