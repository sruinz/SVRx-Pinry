import json
import threading
import time
import ssl
import socket
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from joserfc import jwt
from joserfc.jwk import RSAKey


class SyntheticProvider:
    issuer = 'https://idp.example'

    def __init__(self):
        self.key = RSAKey.generate_key(2048, parameters={'kid': 'synthetic'})
        self.signing_key = self.key
        self.claims = {
            'iss': self.issuer, 'sub': 'external-123', 'aud': 'pinry-client',
            'nonce': 'one-use-nonce', 'iat': int(time.time()),
            'exp': int(time.time()) + 300, 'name': '합성 사용자',
        }
        self.metadata = {
            'issuer': self.issuer,
            'authorization_endpoint': self.issuer + '/authorize',
            'token_endpoint': self.issuer + '/token',
            'jwks_uri': self.issuer + '/jwks',
            'id_token_signing_alg_values_supported': ['RS256'],
            'token_endpoint_auth_methods_supported': ['client_secret_post'],
            'code_challenge_methods_supported': ['S256'],
        }
        self.user = {'id': 12345678, 'login': 'synthetic', 'email': None}
        self.requests = []

    @contextmanager
    def serve(self):
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.respond()

            def do_POST(self):
                self.respond()

            def log_message(self, *args):
                pass

            def respond(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                fixture.requests.append((self.path, parse_qs(body.decode())))
                if self.path.endswith('/.well-known/openid-configuration'):
                    value = fixture.metadata
                elif self.path == '/jwks':
                    value = {'keys': [fixture.key.as_dict(private=False)]}
                elif self.path == '/token':
                    value = {
                        'access_token': 'synthetic-access', 'token_type': 'Bearer',
                        'id_token': jwt.encode(
                            {'alg': 'RS256', 'kid': 'synthetic'},
                            fixture.claims, fixture.signing_key,
                        ),
                    }
                elif self.path == '/login/oauth/access_token':
                    value = {'access_token': 'synthetic-access', 'token_type': 'Bearer'}
                elif self.path == '/user':
                    value = fixture.user
                else:
                    self.send_error(404)
                    return
                payload = json.dumps(value).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield self
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def request_json(self, provider, url, method='GET', headers=None, body=None):
        # 시험에서만 전송 경계를 바꾸며 실제 HTTPS 검사는 별도로 수행한다.
        from users.sso.transport import validate_endpoint
        validate_endpoint(provider, url)
        parsed = urlsplit(url)
        connection = HTTPConnection('127.0.0.1', self.port, timeout=2)
        try:
            connection.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            connection.sock.settimeout(2)
            connection.sock.connect(('127.0.0.1', self.port))
            connection.request(method, parsed.path, body=body, headers=headers or {})
            response = connection.getresponse()
            if response.status != 200:
                raise AssertionError('등록되지 않은 합성 제공자 경로입니다.')
            return json.loads(response.read())
        finally:
            connection.close()


@contextmanager
def tls_server(status=200, payload=b'{"ok": true}', delay=0, header_delay=0):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'idp.example')])
    certificate = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName('idp.example')]), False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
        .sign(key, hashes.SHA256())
    )
    with tempfile.TemporaryDirectory() as directory:
        cert_path = Path(directory) / 'ca.pem'
        key_path = Path(directory) / 'key.pem'
        cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                requests.append(dict(self.headers))
                if header_delay:
                    header = b'HTTP/1.0 200 OK\r\nContent-Length: 12\r\nX-Slow: synthetic-header\r\n\r\n'
                    try:
                        for byte in header:
                            time.sleep(header_delay)
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                        self.wfile.write(payload)
                    except (OSError, ssl.SSLError):
                        pass
                    return
                self.send_response(status)
                self.send_header('Location', 'https://attacker.example/token')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                try:
                    if delay:
                        for byte in payload:
                            time.sleep(delay)
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                    else:
                        self.wfile.write(payload)
                except (OSError, ssl.SSLError):
                    pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_path, key_path)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.server_address[1], str(cert_path), requests
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
