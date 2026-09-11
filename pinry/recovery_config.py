"""복구 리스너와 Django가 공유하는 명시 배포 설정 검증."""
import hashlib
import os
from pathlib import Path
import re
import ssl
from urllib.parse import urlsplit


def _service_can_read(path, uid, gid):
    for target in [Path(path), *Path(path).parents]:
        info = target.stat()
        shift = 6 if info.st_uid == uid else 3 if info.st_gid == gid else 0
        needed = 4 if target == Path(path) else 1
        if not ((info.st_mode >> shift) & needed):
            return False
    return True


def deployment_configuration(environ=None, service_identity=None):
    environ = os.environ if environ is None else environ
    if environ.get('PINRY_RECOVERY_ENABLED', '').lower() not in ('true', '1'):
        return None
    try:
        origin = environ.get('PINRY_RECOVERY_ORIGIN', '')
        parsed = urlsplit(origin)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username
                or parsed.password or parsed.path or parsed.query or parsed.fragment
                or not re.fullmatch(r'https://[A-Za-z0-9.\-:\[\]]+', origin)
                or parsed.port == 0):
            return None
        host = '[{}]'.format(parsed.hostname) if ':' in parsed.hostname else parsed.hostname
        if parsed.port not in (None, 443):
            host += ':' + str(parsed.port)
        origin = 'https://' + host
        cert = environ.get('PINRY_RECOVERY_CERT_FILE', '')
        key = environ.get('PINRY_RECOVERY_KEY_FILE', '')
        for path in (cert, key):
            if not os.path.isabs(path) or not re.fullmatch(r'/[A-Za-z0-9_./\-]+', path):
                return None
            if service_identity and not _service_can_read(path, *service_identity):
                return None
        if Path(key).stat().st_mode & 0o007:
            return None
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key, password=lambda: '')
        certificate = Path(cert).read_bytes()
        fingerprint = hashlib.sha256('\0'.join((origin, cert, key)).encode() + b'\0' + certificate).hexdigest()
        return dict(origin=origin, host=host, hostname=parsed.hostname,
                    cert=cert, key=key, fingerprint=fingerprint)
    except (ValueError, OSError, ssl.SSLError):
        return None
