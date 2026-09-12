#!/usr/bin/env python3
import io
import os
import re
import stat
import sys
import tempfile
import tokenize


MAX_SETTINGS_BYTES = 1024 * 1024
KEY_PATTERN = re.compile(br"[A-Za-z0-9]{65}\n\Z")


class SettingsFormatError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _normalize_settings_encoding(content):
    """정상 소스는 보존하고, 미선언 CP949 한글 주석만 복구한다."""
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(content).readline)
        content.decode(encoding)
    except (SyntaxError, UnicodeError, LookupError):
        try:
            # 선언된 인코딩이나 설정 문자열의 의미를 추측해서 바꾸지 않는다.
            if any(re.search(br'coding[:=]\s*[-\w.]+', line)
                   for line in content.splitlines()[:2]):
                raise ValueError
            text = content.decode('cp949')
            if text.encode('cp949') != content:
                raise ValueError
            for token in tokenize.generate_tokens(io.StringIO(text).readline):
                if not token.string.isascii():
                    if token.type != tokenize.COMMENT or any(
                        not char.isascii() and not '\uac00' <= char <= '\ud7a3'
                        for char in token.string
                    ):
                        raise ValueError
            content = text.encode('utf-8')
            if len(content) > MAX_SETTINGS_BYTES:
                raise ValueError
        except (ValueError, SyntaxError, tokenize.TokenError):
            raise SettingsFormatError('local_settings_encoding_invalid') from None
    try:
        compile(content, '<local_settings>', 'exec')
    except (SyntaxError, ValueError):
        raise SettingsFormatError('local_settings_syntax_invalid') from None
    return content


def _backup_settings(data_root, target, content):
    descriptor, backup = tempfile.mkstemp(
        dir=data_root, prefix=os.path.basename(target) + '.encoding-', suffix='.bak',
    )
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    if _read_source(backup, MAX_SETTINGS_BYTES) != content:
        raise OSError
    _sync_directory(data_root)


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_source(path, limit):
    source = os.lstat(path)
    if stat.S_ISLNK(source.st_mode):
        raise ValueError
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if (source.st_dev, source.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError
        if before.st_size <= 0 or before.st_size > limit:
            raise ValueError
        content = b""
        while len(content) <= limit:
            chunk = os.read(descriptor, min(65536, limit + 1 - len(content)))
            if not chunk:
                break
            content += chunk
        after = os.fstat(descriptor)
        identity = (before.st_dev, before.st_ino, before.st_size)
        if identity != (after.st_dev, after.st_ino, after.st_size):
            raise ValueError
        if not stat.S_ISREG(after.st_mode) or after.st_nlink != 1:
            raise ValueError
        if (
            before.st_mtime_ns != after.st_mtime_ns
            or before.st_ctime_ns != after.st_ctime_ns
        ):
            raise ValueError
        if len(content) != before.st_size:
            raise ValueError
        return content
    finally:
        os.close(descriptor)


def _validate_content(content, kind):
    if kind == "settings":
        if b"secret_key_place_holder" in content:
            raise ValueError
        return
    if kind == "key" and KEY_PATTERN.fullmatch(content):
        return
    raise ValueError


def _write_private_file(data_root, target, content):
    descriptor, temporary = tempfile.mkstemp(
        dir=data_root,
        prefix=".{}.tmp-".format(os.path.basename(target)),
    )
    try:
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(content):
            written = os.write(descriptor, content[offset:])
            if written <= 0:
                raise OSError
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, target)
        temporary = ""
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def normalize(data_root, target, kind):
    root_stat = os.lstat(data_root)
    if not stat.S_ISDIR(root_stat.st_mode):
        raise ValueError
    root = os.path.abspath(data_root)
    target_path = os.path.abspath(target)
    if os.path.dirname(target_path) != root:
        raise ValueError
    limit = MAX_SETTINGS_BYTES if kind == "settings" else 66
    content = _read_source(target_path, limit)
    _validate_content(content, kind)
    if kind == 'settings':
        normalized = _normalize_settings_encoding(content)
        if normalized != content:
            _backup_settings(root, target_path, content)
            if _read_source(target_path, limit) != content:
                raise OSError
            content = normalized
    _write_private_file(root, target_path, content)
    _sync_directory(root)
    final = os.lstat(target_path)
    if not stat.S_ISREG(final.st_mode) or final.st_nlink != 1:
        raise ValueError
    if final.st_uid != os.geteuid() or stat.S_IMODE(final.st_mode) != 0o600:
        raise ValueError
    if _read_source(target_path, limit) != content:
        raise ValueError


def main():
    if len(sys.argv) != 4 or sys.argv[3] not in ("settings", "key"):
        return 1
    try:
        normalize(sys.argv[1], sys.argv[2], sys.argv[3])
    except SettingsFormatError as error:
        print(error.code, file=sys.stderr)
        return 1
    except (OSError, ValueError):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
