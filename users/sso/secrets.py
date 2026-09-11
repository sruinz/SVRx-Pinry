import os
import stat
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ValidationError

from users.models import SSOAttempt, SSOProvider


def _read_key(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        metadata = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.geteuid()
        ):
            raise ValueError
        key = stream.read(45)
        Fernet(key)
        return key


def _key(create):
    path = Path(getattr(
        settings, 'SSO_SECRET_KEY_FILE',
        Path(settings.PINRY_DATA_ROOT) / 'sso-secret.key',
    ))
    try:
        try:
            return _read_key(path)
        except FileNotFoundError:
            if not create:
                raise ValueError
        if (
            SSOProvider.objects.exclude(encrypted_client_secret='').exists()
            or SSOAttempt.objects.exclude(protected_payload='').exists()
        ):
            raise ValueError
        # O_EXCL/0600 임시 파일을 완성한 뒤 원자 게시해 반쪽 키 읽기를 막는다.
        descriptor, temporary = tempfile.mkstemp(prefix='.sso-key-', dir=path.parent)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(Fernet.generate_key())
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path, follow_symlinks=False)
            except FileExistsError:
                pass
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            os.unlink(temporary)
        return _read_key(path)
    except (OSError, ValueError):
        raise ValidationError('SSO 암호화 키 파일과 권한을 확인해 주세요.') from None


def encrypt_secret(value):
    return Fernet(_key(create=True)).encrypt(value.encode('utf-8')).decode('ascii')


def decrypt_secret(value):
    key = _key(create=False)
    try:
        return Fernet(key).decrypt(value.encode('ascii')).decode('utf-8')
    except (InvalidToken, UnicodeError, ValueError):
        raise ValidationError('SSO 비밀 정보를 복호화할 수 없습니다.') from None
