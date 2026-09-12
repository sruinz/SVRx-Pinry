import http.client
import ipaddress
import json
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError

from users.models import PRIVATE_NETWORKS, normalize_cidrs


CONNECT_TIMEOUT = 5
RESPONSE_TIMEOUT = 10
MAX_BODY = 1024 * 1024


def _parse_url(url):
    try:
        if not isinstance(url, str) or len(url) > 2048:
            raise ValueError
        if any(ord(character) <= 32 for character in url) or '\\' in url:
            raise ValueError
        parsed = urlsplit(url)
        if (
            parsed.scheme != 'https' or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.fragment or '%' in parsed.hostname
            or (parsed.port is not None and not 1 <= parsed.port <= 65535)
        ):
            raise ValueError
        return parsed
    except (ValueError, TypeError):
        raise ValidationError('올바른 HTTPS 제공자 주소가 필요합니다.') from None


def _origin(parsed):
    return parsed.hostname.lower(), parsed.port or 443


def _resolve_endpoint(provider, url):
    parsed = _parse_url(url)
    allowed = []
    for origin in provider.allowed_endpoint_origins:
        endpoint = _parse_url(origin)
        if endpoint.path not in ('', '/') or endpoint.query:
            raise ValidationError('허용 origin에는 경로와 쿼리를 넣을 수 없습니다.')
        allowed.append(_origin(endpoint))
    if _origin(parsed) not in allowed:
        raise ValidationError('허용되지 않은 제공자 목적지입니다.')
    networks = [
        ipaddress.ip_network(cidr)
        for cidr in normalize_cidrs(provider.internal_cidrs, private_only=True)
    ]
    # 슈퍼 관리자가 지정한 자체 호스팅 IdP의 동일 출처만 내부 DNS를 허용한다.
    configured = provider.discovery_url or provider.issuer
    private_host = bool(provider.kind in ('authentik', 'synology', 'oidc') and configured
                        and _origin(_parse_url(configured)) == _origin(parsed))
    try:
        addresses = socket.getaddrinfo(
            parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM,
        )
        if not addresses:
            raise ValueError
        for family, socktype, proto, canonname, sockaddr in addresses:
            address = ipaddress.ip_address(sockaddr[0])
            private = any(address in network for network in PRIVATE_NETWORKS)
            permitted_private = private and (private_host or any(address in network for network in networks))
            if (
                address.is_loopback or address.is_link_local
                or address.is_multicast or address.is_unspecified
                or address.is_reserved or getattr(address, 'ipv4_mapped', None)
                or not (address.is_global or permitted_private)
            ):
                raise ValueError
        return parsed, addresses
    except (OSError, ValueError):
        raise ValidationError('차단된 주소이거나 제공자 주소를 확인할 수 없습니다.') from None


def validate_endpoint(provider, url):
    _resolve_endpoint(provider, url)
    return url


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, parsed, address, context):
        super().__init__(
            parsed.hostname, parsed.port or 443, timeout=CONNECT_TIMEOUT,
            context=context,
        )
        self.address = address

    def connect(self):
        # 검증한 sockaddr로 직접 연결하고 인증서와 SNI에는 원래 호스트를 쓴다.
        family, socktype, proto, canonname, sockaddr = self.address
        raw_socket = socket.socket(family, socktype, proto)
        try:
            deadline = time.monotonic() + CONNECT_TIMEOUT
            raw_socket.settimeout(CONNECT_TIMEOUT)
            raw_socket.connect(sockaddr)
            raw_socket.settimeout(max(0.001, deadline - time.monotonic()))
            self.sock = self._context.wrap_socket(raw_socket, server_hostname=self.host)
        except Exception:
            raw_socket.close()
            raise


def _stop_response(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def request_json(provider, url, method='GET', headers=None, body=None):
    parsed, addresses = _resolve_endpoint(provider, url)
    connection = None
    response = None
    timer = None
    try:
        context = ssl.create_default_context(cafile=getattr(settings, 'SSO_CA_BUNDLE', None))
        connection = _PinnedHTTPSConnection(parsed, addresses[0], context)
        connection.connect()
        deadline = time.monotonic() + RESPONSE_TIMEOUT
        connection.sock.settimeout(RESPONSE_TIMEOUT)
        # 헤더·본문의 느린 바이트 전송도 전체 응답 제한 안에서 종료한다.
        timer = threading.Timer(RESPONSE_TIMEOUT, _stop_response, args=(connection.sock,))
        timer.daemon = True
        timer.start()
        path = parsed.path or '/'
        if parsed.query:
            path += '?' + parsed.query
        request_headers = {'Accept': 'application/json', 'User-Agent': 'Pinry-SSO'}
        request_headers.update(headers or {})
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError
        length = response.getheader('Content-Length')
        if length is not None and not 0 <= int(length) <= MAX_BODY:
            raise ValueError
        chunks = []
        size = 0
        while not response.isclosed():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            chunk = response.read1(min(65536, MAX_BODY + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_BODY:
                raise ValueError
        if time.monotonic() >= deadline:
            raise TimeoutError
        if length is not None and size != int(length):
            raise ValueError
        value = json.loads(b''.join(chunks))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (OSError, ValueError, http.client.HTTPException):
        raise ValidationError('SSO 제공자와 안전하게 통신하지 못했습니다.') from None
    finally:
        if timer is not None:
            timer.cancel()
        if response is not None:
            response.close()
        if connection is not None:
            connection.close()
