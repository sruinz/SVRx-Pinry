from io import BytesIO
from concurrent.futures import ThreadPoolExecutor
import gzip
import os
from pathlib import Path
import shutil
import socketserver
import ssl
import subprocess
import tempfile
import threading
from time import monotonic, sleep
from unittest import mock
from http.server import BaseHTTPRequestHandler, HTTPServer
import sys

from django.test import SimpleTestCase
from PIL import Image as PILImage

from core.services import pinned_http
from core.services.pinned_http import (
    PinnedHTTPAdapter,
    PinnedHTTPTransport,
)
from core.services.safe_url_fetch import (
    FetchLimits,
    ResolvedTarget,
    SafeFetchError,
    SafeUrlFetcher,
)


LOOPBACK = "127.0.0.1"


def make_image_bytes():
    output = BytesIO()
    PILImage.new("RGB", (2, 3), "red").save(output, format="PNG")
    return output.getvalue()


def make_target(
    hostname="images.test",
    ip_address=LOOPBACK,
    port=443,
    request_target="/image.png",
    scheme="https",
):
    return ResolvedTarget(
        scheme=scheme,
        hostname=hostname,
        port=port,
        ip_address=ip_address,
        request_target=request_target,
    )


class ManualClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class SequenceClock:
    def __init__(self, values):
        self.values = list(values)

    def __call__(self):
        return self.values.pop(0)


class RecordingSocket:
    def __init__(self, peer_ip=LOOPBACK):
        self.peer_ip = peer_ip
        self.timeouts = []

    def getpeername(self):
        return self.peer_ip, 443

    def settimeout(self, timeout):
        self.timeouts.append(timeout)


class RecordingPool:
    def __init__(self):
        self.close_calls = 0

    def close(self):
        self.close_calls += 1


class RecordingRaw:
    def __init__(self, chunks, sock, after_read=None):
        self._connection = mock.Mock(sock=sock)
        self.chunks = list(chunks)
        self.after_read = after_read
        self.read_calls = []
        self.decode_content = True

    def read(self, amount, decode_content=True):
        self.read_calls.append((amount, decode_content))
        chunk = self.chunks.pop(0) if self.chunks else b""
        if self.after_read is not None:
            self.after_read()
        return chunk


class ResponseOwnedRaw(RecordingRaw):
    def __init__(self, chunks, sock, private_socket_name="_sock"):
        super(ResponseOwnedRaw, self).__init__(chunks, None)
        self._connection = mock.Mock(sock=None)
        socket_io = mock.Mock(spec=[])
        setattr(socket_io, private_socket_name, sock)
        self._fp = mock.Mock(
            fp=mock.Mock(raw=socket_io)
        )


class RecordingRequestsResponse:
    def __init__(self, raw, status_code=200, headers=None):
        self.raw = raw
        self.status_code = status_code
        self.headers = headers or {}
        self.closed = False

    @property
    def is_redirect(self):
        return (
            self.status_code in (301, 302, 303, 307, 308)
            and "Location" in self.headers
        )

    def close(self):
        self.closed = True


class RecordingSession:
    def __init__(self, response):
        self.response = response
        self.calls = []
        self.mounts = []
        self.trust_env = True
        self.max_redirects = 30
        self.verify = True

    def mount(self, prefix, adapter):
        self.mounts.append((prefix, adapter))

    def request(self, **kwargs):
        self.calls.append(kwargs)
        return self.response

    def close(self):
        pass


class PinnedHTTPTransportTests(SimpleTestCase):
    def make_transport(
        self,
        response,
        clock=None,
    ):
        session = RecordingSession(response)
        transport = PinnedHTTPTransport(
            session_factory=lambda: session,
            clock=clock or monotonic,
        )
        return transport, session

    def test_session_ignores_environment_and_never_follows_redirects(self):
        raw = RecordingRaw([], RecordingSocket())
        response = RecordingRequestsResponse(
            raw,
            status_code=302,
            headers={"Location": "https://redirect.test/image.png"},
        )
        transport, session = self.make_transport(response)
        target = make_target(port=8443, request_target="/start?x=1")

        result = transport.request(target, None, 3, 8, monotonic() + 12)

        self.assertFalse(session.trust_env)
        self.assertEqual(len(session.calls), 1)
        call = session.calls[0]
        self.assertEqual(
            call["url"], "https://images.test:8443/start?x=1"
        )
        self.assertEqual(call["headers"]["Host"], "images.test:8443")
        self.assertEqual(call["headers"]["Accept-Encoding"], "identity")
        self.assertFalse(call["allow_redirects"])
        self.assertTrue(call["stream"])
        self.assertTrue(result.is_redirect)
        self.assertEqual(
            result.location, "https://redirect.test/image.png"
        )

    def test_pool_uses_validated_ip_and_original_tls_identity(self):
        adapter = PinnedHTTPAdapter()
        target = make_target(ip_address="203.0.113.10", port=8443)

        with adapter.target(target):
            pool = adapter.get_connection(
                "https://images.test:8443/image.png"
            )

        self.assertEqual(pool.host, "images.test")
        self.assertEqual(pool.port, 8443)
        self.assertEqual(pool.connect_ip, "203.0.113.10")
        self.assertEqual(pool.assert_hostname, "images.test")
        self.assertEqual(pool.conn_kw["server_hostname"], "images.test")

    def test_pool_key_includes_scheme_hostname_port_and_ip(self):
        calls = []

        def pool_factory(scheme, hostname, port, ip_address):
            pool = mock.Mock()
            calls.append((scheme, hostname, port, ip_address, pool))
            return pool

        adapter = PinnedHTTPAdapter(pool_factory=pool_factory)
        first = make_target(ip_address="203.0.113.10")
        changed = make_target(ip_address="203.0.113.11")

        with adapter.target(first):
            first_pool = adapter.get_connection(
                "https://images.test/image.png"
            )
        with adapter.target(first):
            reused_pool = adapter.get_connection(
                "https://images.test/again.png"
            )
        with adapter.target(changed):
            changed_pool = adapter.get_connection(
                "https://images.test/image.png"
            )

        self.assertIs(first_pool, reused_pool)
        self.assertIsNot(first_pool, changed_pool)
        self.assertEqual(
            [call[:4] for call in calls],
            [
                ("https", "images.test", 443, "203.0.113.10"),
                ("https", "images.test", 443, "203.0.113.11"),
            ],
        )

    def test_pinned_pool_cache_evicts_and_closes_oldest_pool(self):
        pools = []

        def pool_factory(scheme, hostname, port, ip_address):
            del scheme, hostname, port, ip_address
            pool = RecordingPool()
            pools.append(pool)
            return pool

        adapter = PinnedHTTPAdapter(
            pool_factory=pool_factory,
            pool_connections=2,
        )
        self.addCleanup(adapter.close)
        targets = (
            make_target(hostname="one.test", ip_address="203.0.113.1"),
            make_target(hostname="two.test", ip_address="203.0.113.2"),
            make_target(hostname="three.test", ip_address="203.0.113.3"),
        )

        for target in targets:
            with adapter.target(target):
                adapter.get_connection(
                    "https://{}/image.png".format(target.hostname)
                )

        self.assertEqual(len(adapter._pinned_pools), 2)
        self.assertEqual(
            [pool.close_calls for pool in pools], [1, 0, 0]
        )

    def test_ipv6_connect_address_is_not_bracketed_but_host_is(self):
        adapter = PinnedHTTPAdapter()
        target = make_target(
            hostname="2001:db8::10",
            ip_address="2001:db8::10",
            port=8443,
        )
        raw = RecordingRaw([], RecordingSocket(peer_ip="2001:db8::10"))
        transport, session = self.make_transport(
            RecordingRequestsResponse(raw)
        )

        with adapter.target(target):
            pool = adapter.get_connection(
                "https://[2001:db8::10]:8443/image.png"
            )
        transport.request(target, None, 3, 8, monotonic() + 12)

        self.assertEqual(pool.connect_ip, "2001:db8::10")
        self.assertEqual(
            session.calls[0]["headers"]["Host"],
            "[2001:db8::10]:8443",
        )

    def test_peer_mismatch_closes_before_body_read(self):
        raw = RecordingRaw([b"secret body"], RecordingSocket("127.0.0.2"))
        response = RecordingRequestsResponse(raw)
        transport, session = self.make_transport(response)

        with self.assertRaises(SafeFetchError) as caught:
            transport.request(
                make_target(), None, 3, 8, monotonic() + 12
            )

        self.assertEqual(caught.exception.code, "dns_rebinding_detected")
        self.assertFalse(caught.exception.retryable)
        self.assertTrue(response.closed)
        self.assertEqual(raw.read_calls, [])

    def test_timeouts_use_remaining_deadline_and_raw_reads_do_not_decode(self):
        clock = ManualClock(90.0)
        sock = RecordingSocket()
        raw = RecordingRaw(
            [b"first", b"second", b""],
            sock,
            after_read=lambda: clock.advance(0.75),
        )
        transport, session = self.make_transport(
            RecordingRequestsResponse(raw),
            clock=clock,
        )

        response = transport.request(
            make_target(), None, 3, 8, deadline=92.0
        )
        chunks = list(response.iter_content(chunk_size=64 * 1024))

        self.assertEqual(session.calls[0]["timeout"], (2.0, 2.0))
        self.assertEqual(chunks, [b"first", b"second"])
        self.assertEqual(
            raw.read_calls,
            [
                (64 * 1024, False),
                (64 * 1024, False),
                (64 * 1024, False),
            ],
        )
        self.assertFalse(raw.decode_content)
        self.assertEqual(sock.timeouts, [2.0, 1.25, 0.5])

    def test_response_owned_socket_keeps_peer_check_and_read_deadline(self):
        clock = ManualClock(90.0)
        sock = RecordingSocket()
        raw = ResponseOwnedRaw([b"body", b""], sock)
        transport, session = self.make_transport(
            RecordingRequestsResponse(raw),
            clock=clock,
        )

        response = transport.request(
            make_target(), None, 3, 8, deadline=92.0
        )
        chunks = list(response.iter_content())

        self.assertEqual(chunks, [b"body"])
        self.assertEqual(response.peer_ip, LOOPBACK)
        self.assertEqual(sock.timeouts, [2.0, 2.0])

    def test_unrecognized_response_socket_chain_fails_closed(self):
        raw = ResponseOwnedRaw(
            [b"body"], RecordingSocket(), private_socket_name="socket"
        )
        response = RecordingRequestsResponse(raw)
        transport, session = self.make_transport(response)

        with self.assertRaises(SafeFetchError) as caught:
            transport.request(
                make_target(), None, 3, 8, monotonic() + 12
            )

        self.assertEqual(caught.exception.code, "image_download_failed")
        self.assertTrue(caught.exception.retryable)
        self.assertTrue(response.closed)
        self.assertEqual(raw.read_calls, [])

    def test_expired_deadline_opens_no_request(self):
        clock = ManualClock(10.0)
        raw = RecordingRaw([], RecordingSocket())
        transport, session = self.make_transport(
            RecordingRequestsResponse(raw),
            clock=clock,
        )

        with self.assertRaises(SafeFetchError) as caught:
            transport.request(make_target(), None, 3, 8, deadline=10.0)

        self.assertEqual(caught.exception.code, "image_fetch_timeout")
        self.assertEqual(session.calls, [])

    def test_connect_and_initial_read_timeouts_share_one_remaining_snapshot(self):
        clock = SequenceClock([90.0, 91.5])
        raw = RecordingRaw([], RecordingSocket())
        transport, session = self.make_transport(
            RecordingRequestsResponse(raw),
            clock=clock,
        )

        transport.request(
            make_target(), None, 3, 8, deadline=92.0
        )

        self.assertEqual(session.calls[0]["timeout"], (2.0, 2.0))
        self.assertEqual(clock.values, [91.5])

    def test_unsupported_http_stack_creates_no_session_or_pool(self):
        mismatches = (
            ("2.28.0", "1.26.20"),
            ("2.27.1", "1.26.19"),
        )
        for requests_version, urllib3_version in mismatches:
            with self.subTest(
                requests_version=requests_version,
                urllib3_version=urllib3_version,
            ), mock.patch.object(
                pinned_http.requests,
                "__version__",
                requests_version,
            ), mock.patch.object(
                pinned_http.urllib3,
                "__version__",
                urllib3_version,
            ), mock.patch.object(
                pinned_http.requests, "Session"
            ) as session_factory, mock.patch.object(
                pinned_http, "PinnedHTTPAdapter"
            ) as adapter_factory:
                with self.assertRaises(SafeFetchError) as caught:
                    PinnedHTTPTransport()

            self.assertEqual(
                caught.exception.code, "unsupported_http_stack"
            )
            self.assertFalse(caught.exception.retryable)
            session_factory.assert_not_called()
            adapter_factory.assert_not_called()

    def test_direct_adapter_stack_mismatch_creates_no_io_objects(self):
        with mock.patch.object(
            pinned_http.requests,
            "__version__",
            "9.9.9",
        ), mock.patch.object(
            pinned_http.urllib3,
            "__version__",
            "9.9.9",
        ), mock.patch.object(
            pinned_http.requests, "Session"
        ) as session_factory, mock.patch.object(
            pinned_http.requests.adapters, "PoolManager"
        ) as pool_manager, mock.patch.object(
            pinned_http.connection, "create_connection"
        ) as create_connection:
            with self.assertRaises(SafeFetchError) as caught:
                PinnedHTTPAdapter()

        self.assertEqual(
            caught.exception.code, "unsupported_http_stack"
        )
        self.assertFalse(caught.exception.retryable)
        session_factory.assert_not_called()
        pool_manager.assert_not_called()
        create_connection.assert_not_called()

    def test_import_creates_no_session_or_pool(self):
        script = (
            "import requests, urllib3\n"
            "def fail(*args, **kwargs):\n"
            "    raise AssertionError('I/O object created during import')\n"
            "requests.Session = fail\n"
            "urllib3.PoolManager = fail\n"
            "import core.services.pinned_http\n"
        )

        completed = subprocess.run(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_missing_private_socket_chain_fails_closed(self):
        raw = mock.Mock(spec=["decode_content"])
        response = RecordingRequestsResponse(raw)
        transport, session = self.make_transport(response)

        with self.assertRaises(SafeFetchError) as caught:
            transport.request(
                make_target(), None, 3, 8, monotonic() + 12
            )

        self.assertEqual(caught.exception.code, "image_download_failed")
        self.assertTrue(caught.exception.retryable)
        self.assertTrue(response.closed)


class CountingResolver:
    def __init__(self):
        self.calls = []

    def resolve(self, hostname, port, deadline):
        self.calls.append((hostname, port, deadline))
        return [LOOPBACK]


class _TLSHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self.server.requests.append((self.path, self.headers.get("Host")))
        if self.path == "/close":
            self.protocol_version = "HTTP/1.0"
            body = self.server.image_body
            self.send_response(200)
            self.send_header("Connection", "close")
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True
            return
        if self.path == "/blocked-body":
            body = self.server.image_body
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.flush()
            self.server.response_started.set()
            self.server.release_body.wait(timeout=5)
            self.wfile.write(body)
            return
        if self.path == "/slow-drip":
            body = b"abcde"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            for index, byte in enumerate(body):
                self.wfile.write(bytes([byte]))
                self.wfile.flush()
                if index < len(body) - 1:
                    sleep(0.08)
            return
        if self.path == "/redirect":
            location = "https://redirect.test:{}/image.png".format(
                self.server.server_port
            )
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/gzip":
            body = gzip.compress(b"x" * (1024 * 1024))
            self.send_response(200)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = self.server.image_body
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format_string, *args):
        del format_string, args


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True


class PinnedHTTPSIntegrationTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super(PinnedHTTPSIntegrationTests, cls).setUpClass()
        cls.temp_directory = tempfile.mkdtemp(prefix="pinry-pinned-tls-")
        cls._make_certificates(Path(cls.temp_directory))
        cls.server = _ThreadingHTTPServer((LOOPBACK, 0), _TLSHandler)
        cls.server.requests = []
        cls.server.server_names = []
        cls.server.image_body = make_image_bytes()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(
            os.path.join(cls.temp_directory, "server.pem"),
            os.path.join(cls.temp_directory, "server.key"),
        )

        def record_server_name(ssl_socket, server_name, ssl_context):
            del ssl_socket, ssl_context
            cls.server.server_names.append(server_name)

        context.set_servername_callback(record_server_name)
        cls.server.socket = context.wrap_socket(
            cls.server.socket, server_side=True
        )
        cls.server_thread = threading.Thread(
            target=cls.server.serve_forever
        )
        cls.server_thread.daemon = True
        cls.server_thread.start()
        cls.http_server = _ThreadingHTTPServer((LOOPBACK, 0), _TLSHandler)
        cls.http_server.requests = []
        cls.http_server.image_body = cls.server.image_body
        cls.http_server_thread = threading.Thread(
            target=cls.http_server.serve_forever
        )
        cls.http_server_thread.daemon = True
        cls.http_server_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.http_server.shutdown()
        cls.http_server.server_close()
        cls.http_server_thread.join(timeout=2)
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=2)
        shutil.rmtree(cls.temp_directory)
        super(PinnedHTTPSIntegrationTests, cls).tearDownClass()

    @classmethod
    def _make_certificates(cls, directory):
        openssl = shutil.which("openssl")
        if openssl is None:
            raise AssertionError("openssl is required for the TLS test")
        ca_config = directory / "ca.cnf"
        ca_config.write_text(
            "[req]\n"
            "distinguished_name=dn\n"
            "x509_extensions=ca_ext\n"
            "prompt=no\n"
            "[dn]\nCN=Pinry Test CA\n"
            "[ca_ext]\n"
            "basicConstraints=critical,CA:TRUE\n"
            "keyUsage=critical,keyCertSign,cRLSign\n"
        )
        server_config = directory / "server.cnf"
        server_config.write_text(
            "[req]\n"
            "distinguished_name=dn\n"
            "prompt=no\n"
            "[dn]\nCN=images.test\n"
            "[server_ext]\n"
            "basicConstraints=critical,CA:FALSE\n"
            "keyUsage=critical,digitalSignature,keyEncipherment\n"
            "extendedKeyUsage=serverAuth\n"
            "subjectAltName=DNS:images.test,DNS:redirect.test\n"
        )
        commands = (
            [
                openssl,
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "3650",
                "-sha256",
                "-config",
                str(ca_config),
                "-keyout",
                str(directory / "ca.key"),
                "-out",
                str(directory / "ca.pem"),
            ],
            [
                openssl,
                "req",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-sha256",
                "-config",
                str(server_config),
                "-keyout",
                str(directory / "server.key"),
                "-out",
                str(directory / "server.csr"),
            ],
            [
                openssl,
                "x509",
                "-req",
                "-days",
                "3650",
                "-sha256",
                "-in",
                str(directory / "server.csr"),
                "-CA",
                str(directory / "ca.pem"),
                "-CAkey",
                str(directory / "ca.key"),
                "-CAcreateserial",
                "-extfile",
                str(server_config),
                "-extensions",
                "server_ext",
                "-out",
                str(directory / "server.pem"),
            ],
        )
        for command in commands:
            subprocess.run(
                command,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    def make_fetcher(self, allowlist):
        resolver = CountingResolver()
        transport = PinnedHTTPTransport()
        transport.session.verify = os.path.join(
            self.temp_directory, "ca.pem"
        )
        limits = FetchLimits(private_allowlist=allowlist)
        return (
            SafeUrlFetcher(resolver, transport, limits=limits),
            resolver,
            transport,
        )

    def setUp(self):
        self.server.requests[:] = []
        self.server.server_names[:] = []
        self.http_server.requests[:] = []
        self.http_server.response_started = threading.Event()
        self.http_server.release_body = threading.Event()

    def test_pool_eviction_does_not_break_checked_out_response(self):
        adapter = PinnedHTTPAdapter(pool_connections=1)
        transport = PinnedHTTPTransport(
            adapter_factory=lambda: adapter
        )
        self.addCleanup(transport.close)
        resolver = CountingResolver()
        fetcher = SafeUrlFetcher(
            resolver,
            transport,
            limits=FetchLimits(private_allowlist=["images.test"]),
        )
        port = self.http_server.server_port
        first_target = make_target(
            hostname="images.test",
            port=port,
            request_target="/blocked-body",
            scheme="http",
        )
        with adapter.target(first_target):
            first_pool = adapter.get_connection(
                "http://images.test:{}/blocked-body".format(port)
            )

        executor = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(executor.shutdown, wait=True)
        self.addCleanup(self.http_server.release_body.set)
        future = executor.submit(
            fetcher.fetch,
            "http://images.test:{}/blocked-body".format(port),
        )
        self.assertTrue(
            self.http_server.response_started.wait(timeout=2)
        )
        second_target = make_target(
            hostname="other.test",
            port=port,
            request_target="/image.png",
            scheme="http",
        )
        with adapter.target(second_target):
            adapter.get_connection(
                "http://other.test:{}/image.png".format(port)
            )

        self.assertEqual(len(adapter._pinned_pools), 1)
        self.assertIsNone(first_pool.pool)
        self.http_server.release_body.set()
        fetched = future.result(timeout=2)

        self.assertEqual(fetched.content, self.http_server.image_body)

    def test_public_fetch_slow_drip_returns_stable_cooperative_timeout(self):
        transport = PinnedHTTPTransport()
        self.addCleanup(transport.close)
        fetcher = SafeUrlFetcher(
            CountingResolver(),
            transport,
            limits=FetchLimits(
                connect_timeout=1,
                read_timeout=1,
                total_timeout=0.2,
                private_allowlist=["images.test"],
            ),
        )
        url = "http://images.test:{}/slow-drip".format(
            self.http_server.server_port
        )

        with self.assertRaises(SafeFetchError) as caught:
            fetcher.fetch(url)

        self.assertEqual(caught.exception.code, "image_fetch_timeout")
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(
            caught.exception.message,
            "The image download timed out.",
        )

    def test_real_http10_close_response_is_fetched_over_pinned_http(self):
        fetcher, resolver, transport = self.make_fetcher(["images.test"])
        url = "http://images.test:{}/close".format(
            self.http_server.server_port
        )

        fetched = fetcher.fetch(url)

        self.assertEqual(fetched.content, self.http_server.image_body)
        self.assertEqual(len(resolver.calls), 1)
        self.assertEqual(
            self.http_server.requests,
            [
                (
                    "/close",
                    "images.test:{}".format(
                        self.http_server.server_port
                    ),
                )
            ],
        )
        transport.close()

    def test_real_https_http10_close_response_preserves_tls_identity(self):
        fetcher, resolver, transport = self.make_fetcher(["images.test"])
        url = "https://images.test:{}/close".format(
            self.server.server_port
        )

        fetched = fetcher.fetch(url)

        self.assertEqual(fetched.content, self.server.image_body)
        self.assertEqual(len(resolver.calls), 1)
        self.assertEqual(self.server.server_names, ["images.test"])
        self.assertEqual(
            self.server.requests,
            [
                (
                    "/close",
                    "images.test:{}".format(self.server.server_port),
                )
            ],
        )
        transport.close()

    def test_real_tls_pins_tcp_and_preserves_host_sni_and_hostname(self):
        fetcher, resolver, transport = self.make_fetcher(["images.test"])
        url = "https://images.test:{}/image.png".format(
            self.server.server_port
        )

        with mock.patch.dict(
            os.environ,
            {
                "HTTP_PROXY": "http://127.0.0.1:1",
                "HTTPS_PROXY": "http://127.0.0.1:1",
                "NO_PROXY": "",
            },
        ):
            fetched = fetcher.fetch(url)

        self.assertEqual(fetched.content, self.server.image_body)
        self.assertEqual(len(resolver.calls), 1)
        self.assertEqual(self.server.server_names, ["images.test"])
        self.assertEqual(
            self.server.requests,
            [
                (
                    "/image.png",
                    "images.test:{}".format(self.server.server_port),
                )
            ],
        )
        transport.close()

    def test_real_tls_revalidates_redirect_hostname_on_second_hop(self):
        fetcher, resolver, transport = self.make_fetcher(
            ["images.test", "redirect.test"]
        )
        url = "https://images.test:{}/redirect".format(
            self.server.server_port
        )

        fetched = fetcher.fetch(url)

        self.assertEqual(fetched.content, self.server.image_body)
        self.assertEqual(
            [call[0] for call in resolver.calls],
            ["images.test", "redirect.test"],
        )
        self.assertEqual(
            self.server.server_names,
            ["images.test", "redirect.test"],
        )
        self.assertEqual(
            [request[1] for request in self.server.requests],
            [
                "images.test:{}".format(self.server.server_port),
                "redirect.test:{}".format(self.server.server_port),
            ],
        )
        transport.close()

    def test_real_tls_rejects_certificate_for_different_hostname(self):
        fetcher, resolver, transport = self.make_fetcher(["wrong.test"])
        url = "https://wrong.test:{}/image.png".format(
            self.server.server_port
        )

        with self.assertRaises(SafeFetchError) as caught:
            fetcher.fetch(url)

        self.assertEqual(caught.exception.code, "image_download_failed")
        self.assertEqual(len(resolver.calls), 1)
        self.assertEqual(self.server.requests, [])
        transport.close()

    def test_real_tls_rejects_encoded_body_without_decompression(self):
        fetcher, resolver, transport = self.make_fetcher(["images.test"])
        url = "https://images.test:{}/gzip".format(
            self.server.server_port
        )

        with self.assertRaises(SafeFetchError) as caught:
            fetcher.fetch(url)

        self.assertEqual(
            caught.exception.code, "unsupported_content_encoding"
        )
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(len(resolver.calls), 1)
        transport.close()

    def test_real_tls_peer_mismatch_is_closed_by_pool_boundary(self):
        transport = PinnedHTTPTransport()
        transport.session.verify = os.path.join(
            self.temp_directory, "ca.pem"
        )
        target = make_target(
            hostname="images.test",
            ip_address="127.0.0.2",
            port=self.server.server_port,
        )
        original_create_connection = pinned_http.connection.create_connection
        requested_addresses = []

        def redirect_test_connection(address, *args, **kwargs):
            requested_addresses.append(address)
            return original_create_connection(
                (LOOPBACK, address[1]), *args, **kwargs
            )

        with mock.patch.object(
            pinned_http.connection,
            "create_connection",
            side_effect=redirect_test_connection,
        ):
            with self.assertRaises(SafeFetchError) as caught:
                transport.request(
                    target, None, 3, 8, monotonic() + 12
                )

        self.assertEqual(caught.exception.code, "dns_rebinding_detected")
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(
            requested_addresses,
            [("127.0.0.2", self.server.server_port)],
        )
        transport.close()
