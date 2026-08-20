from contextlib import contextmanager
import ipaddress
import socket
import threading
from time import monotonic
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
import urllib3
from urllib3._collections import RecentlyUsedContainer
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import (
    ConnectTimeoutError,
    NewConnectionError,
    ReadTimeoutError,
)
from urllib3.util import connection

from core.services.safe_url_fetch import SafeFetchError


REQUESTS_VERSION = "2.27.1"
URLLIB3_VERSION = "1.26.20"
RAW_READ_SIZE = 64 * 1024


class _PinnedConnectionMixin:
    def __init__(self, *args, **kwargs):
        self.connect_ip = kwargs.pop("connect_ip")
        super(_PinnedConnectionMixin, self).__init__(*args, **kwargs)

    def _new_conn(self):
        extra_options = {}
        if self.source_address:
            extra_options["source_address"] = self.source_address
        if self.socket_options:
            extra_options["socket_options"] = self.socket_options
        try:
            return connection.create_connection(
                (self.connect_ip, self.port),
                self.timeout,
                **extra_options
            )
        except socket.timeout:
            raise ConnectTimeoutError(
                self, "The pinned connection timed out."
            ) from None
        except socket.error as error:
            raise NewConnectionError(
                self, "The pinned connection failed: {}".format(error)
            ) from error


class _PinnedHTTPConnection(_PinnedConnectionMixin, HTTPConnection):
    pass


class _PinnedHTTPSConnection(_PinnedConnectionMixin, HTTPSConnection):
    pass


class _PinnedConnectionPoolMixin:
    def _make_request(self, conn, method, url, **kwargs):
        response = super(_PinnedConnectionPoolMixin, self)._make_request(
            conn, method, url, **kwargs
        )
        try:
            response_socket = getattr(conn, "sock", None)
            if response_socket is None:
                response_socket = _http_client_response_socket(response)
            if response_socket is None:
                raise ValueError("The response socket was unavailable.")
            peer_ip = _peer_ip(response_socket)
            if ipaddress.ip_address(peer_ip) != ipaddress.ip_address(
                self.connect_ip
            ):
                raise _peer_mismatch()
        except SafeFetchError:
            response.close()
            conn.close()
            raise
        except Exception:
            response.close()
            conn.close()
            raise SafeFetchError(
                "image_download_failed",
                "The image could not be downloaded.",
                True,
            ) from None
        response._pinry_peer_ip = peer_ip
        response._pinry_response_socket = response_socket
        return response


class _PinnedHTTPConnectionPool(
    _PinnedConnectionPoolMixin, HTTPConnectionPool
):
    ConnectionCls = _PinnedHTTPConnection

    def __init__(self, host, port, connect_ip, **kwargs):
        self.connect_ip = connect_ip
        kwargs["connect_ip"] = connect_ip
        super(_PinnedHTTPConnectionPool, self).__init__(
            host, port, **kwargs
        )


class _PinnedHTTPSConnectionPool(
    _PinnedConnectionPoolMixin, HTTPSConnectionPool
):
    ConnectionCls = _PinnedHTTPSConnection

    def __init__(self, host, port, connect_ip, **kwargs):
        self.connect_ip = connect_ip
        kwargs["connect_ip"] = connect_ip
        kwargs["server_hostname"] = host
        super(_PinnedHTTPSConnectionPool, self).__init__(
            host,
            port,
            assert_hostname=host,
            **kwargs
        )


class PinnedHTTPAdapter(HTTPAdapter):
    def __init__(self, pool_factory=None, *args, **kwargs):
        _validate_http_stack()
        self._pinned_pools_lock = threading.RLock()
        self._request_target = threading.local()
        self._pool_factory = pool_factory
        super(PinnedHTTPAdapter, self).__init__(*args, **kwargs)
        self._pinned_pools = RecentlyUsedContainer(
            maxsize=self._pool_connections,
            dispose_func=_close_pool,
        )

    @contextmanager
    def target(self, target):
        previous = getattr(self._request_target, "value", None)
        self._request_target.value = target
        try:
            yield
        finally:
            if previous is None:
                try:
                    del self._request_target.value
                except AttributeError:
                    pass
            else:
                self._request_target.value = previous

    def send(
        self,
        request,
        stream=False,
        timeout=None,
        verify=True,
        cert=None,
        proxies=None,
    ):
        with self._pinned_pools_lock:
            return super(PinnedHTTPAdapter, self).send(
                request,
                stream=stream,
                timeout=timeout,
                verify=verify,
                cert=cert,
                proxies=proxies,
            )

    def get_connection(self, url, proxies=None):
        target = getattr(self._request_target, "value", None)
        if target is None or proxies:
            raise requests.exceptions.InvalidURL(
                "A validated pinned target is required."
            )
        self._verify_url_target(url, target)
        key = (
            target.scheme,
            target.hostname,
            target.port,
            target.ip_address,
        )
        with self._pinned_pools_lock:
            pool = self._pinned_pools.get(key)
            if pool is None:
                pool = self._new_pinned_pool(*key)
                self._pinned_pools[key] = pool
        return pool

    def close(self):
        with self._pinned_pools_lock:
            self._pinned_pools.clear()
        super(PinnedHTTPAdapter, self).close()

    def _new_pinned_pool(self, scheme, hostname, port, ip_address):
        if self._pool_factory is not None:
            return self._pool_factory(
                scheme, hostname, port, ip_address
            )
        options = {
            "maxsize": self._pool_maxsize,
            "block": self._pool_block,
            "retries": self.max_retries,
        }
        if scheme == "https":
            return _PinnedHTTPSConnectionPool(
                hostname,
                port,
                connect_ip=ip_address,
                **options
            )
        if scheme == "http":
            return _PinnedHTTPConnectionPool(
                hostname,
                port,
                connect_ip=ip_address,
                **options
            )
        raise requests.exceptions.InvalidURL(
            "The pinned request scheme is invalid."
        )

    @staticmethod
    def _verify_url_target(url, target):
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except (TypeError, ValueError):
            raise requests.exceptions.InvalidURL(
                "The pinned request URL is invalid."
            ) from None
        if port is None:
            port = 443 if parsed.scheme == "https" else 80
        if (
            parsed.scheme != target.scheme
            or parsed.hostname != target.hostname
            or port != target.port
        ):
            raise requests.exceptions.InvalidURL(
                "The pinned request URL did not match its target."
            )


class TransportResponse:
    def __init__(
        self,
        response,
        peer_ip,
        response_socket,
        read_timeout,
        deadline,
        clock,
    ):
        self._response = response
        self._socket = response_socket
        self._read_timeout = read_timeout
        self._deadline = deadline
        self._clock = clock
        self.status_code = response.status_code
        self.headers = response.headers
        self.peer_ip = peer_ip
        self.is_redirect = response.is_redirect
        self.location = response.headers.get("Location")
        self._response.raw.decode_content = False

    def iter_content(self, chunk_size=RAW_READ_SIZE):
        del chunk_size
        while True:
            if (
                getattr(self._response.raw, "closed", False)
                and getattr(
                    self._response.raw, "length_remaining", None
                ) == 0
            ):
                return
            if self._socket is None:
                raise SafeFetchError(
                    "image_download_failed",
                    "The image could not be downloaded.",
                    True,
                )
            timeout = _remaining_timeout(
                self._read_timeout,
                self._deadline,
                self._clock,
            )
            self._socket.settimeout(timeout)
            try:
                chunk = self._response.raw.read(
                    RAW_READ_SIZE, decode_content=False
                )
            except ReadTimeoutError:
                raise requests.exceptions.Timeout(
                    "The pinned response read timed out."
                ) from None
            if not chunk:
                return
            yield chunk

    def close(self):
        self._response.close()


class PinnedHTTPTransport:
    def __init__(
        self,
        session_factory=None,
        adapter_factory=None,
        clock=monotonic,
    ):
        _validate_http_stack()
        if session_factory is None:
            session_factory = requests.Session
        if adapter_factory is None:
            adapter_factory = PinnedHTTPAdapter
        self.clock = clock
        session = session_factory()
        try:
            session.trust_env = False
            adapter = adapter_factory()
            session.mount("http://", adapter)
            session.mount("https://", adapter)
        except BaseException:
            try:
                session.close()
            except BaseException:
                pass
            raise
        self.session = session
        self.adapter = adapter

    def request(
        self,
        target,
        referer,
        connect_timeout,
        read_timeout,
        deadline,
    ):
        remaining = _remaining(deadline, self.clock)
        request_timeout = min(connect_timeout, remaining)
        initial_read_timeout = min(read_timeout, remaining)
        headers = {
            "Accept-Encoding": "identity",
            "Host": _host_header(target),
        }
        if referer is not None:
            headers["Referer"] = referer
        with self.adapter.target(target):
            response = self.session.request(
                method="GET",
                url=_origin_url(target),
                headers=headers,
                timeout=(request_timeout, initial_read_timeout),
                stream=True,
                allow_redirects=False,
            )
        try:
            response.raw.decode_content = False
            peer_ip = _recorded_peer_ip(response)
            response_socket = _recorded_response_socket(response)
            if response_socket is None:
                response_socket = _response_socket(
                    response, required=False
                )
            if peer_ip is None:
                if response_socket is None:
                    raise SafeFetchError(
                        "image_download_failed",
                        "The image could not be downloaded.",
                        True,
                    )
                peer_ip = _peer_ip(response_socket)
            if ipaddress.ip_address(peer_ip) != ipaddress.ip_address(
                target.ip_address
            ):
                raise _peer_mismatch()
        except SafeFetchError:
            response.close()
            raise
        except Exception:
            response.close()
            raise SafeFetchError(
                "image_download_failed",
                "The image could not be downloaded.",
                True,
            ) from None
        return TransportResponse(
            response=response,
            peer_ip=peer_ip,
            response_socket=response_socket,
            read_timeout=read_timeout,
            deadline=deadline,
            clock=self.clock,
        )

    def close(self):
        self.session.close()


def _validate_http_stack():
    if (
        requests.__version__ != REQUESTS_VERSION
        or urllib3.__version__ != URLLIB3_VERSION
    ):
        raise SafeFetchError(
            "unsupported_http_stack",
            "The configured HTTP stack is not supported.",
            False,
        )


def _remaining_timeout(configured_timeout, deadline, clock):
    return min(configured_timeout, _remaining(deadline, clock))


def _remaining(deadline, clock):
    remaining = deadline - clock()
    if remaining <= 0:
        raise SafeFetchError(
            "image_fetch_timeout",
            "The image download timed out.",
            True,
        )
    return remaining


def _host_header(target):
    hostname = target.hostname
    try:
        if ipaddress.ip_address(hostname).version == 6:
            hostname = "[{}]".format(hostname)
    except ValueError:
        pass
    default_port = 443 if target.scheme == "https" else 80
    if target.port != default_port:
        return "{}:{}".format(hostname, target.port)
    return hostname


def _origin_url(target):
    hostname = target.hostname
    try:
        if ipaddress.ip_address(hostname).version == 6:
            hostname = "[{}]".format(hostname)
    except ValueError:
        pass
    default_port = 443 if target.scheme == "https" else 80
    if target.port != default_port:
        authority = "{}:{}".format(hostname, target.port)
    else:
        authority = hostname
    return "{}://{}{}".format(
        target.scheme, authority, target.request_target
    )


def _response_socket(response, required=True):
    raw_connection = getattr(response.raw, "_connection", None)
    response_socket = getattr(raw_connection, "sock", None)
    if response_socket is None:
        http_response = getattr(response.raw, "_fp", None)
        response_socket = _http_client_response_socket(http_response)
    if response_socket is None and required:
        raise SafeFetchError(
            "image_download_failed",
            "The image could not be downloaded.",
            True,
        )
    return response_socket


def _recorded_peer_ip(response):
    original_response = getattr(response.raw, "_original_response", None)
    return getattr(original_response, "_pinry_peer_ip", None)


def _recorded_response_socket(response):
    original_response = getattr(response.raw, "_original_response", None)
    return getattr(
        original_response, "_pinry_response_socket", None
    )


def _http_client_response_socket(response):
    response_file = getattr(response, "fp", None)
    socket_io = getattr(response_file, "raw", None)
    return getattr(socket_io, "_sock", None)


def _peer_ip(response_socket):
    peer = response_socket.getpeername()
    if not isinstance(peer, tuple) or not peer:
        raise _peer_mismatch()
    return str(ipaddress.ip_address(str(peer[0]).split("%", 1)[0]))


def _peer_mismatch():
    return SafeFetchError(
        "dns_rebinding_detected",
        "The connected address did not match the validated address.",
        False,
    )


def _close_pool(pool):
    try:
        pool.close()
    except Exception:
        pass
