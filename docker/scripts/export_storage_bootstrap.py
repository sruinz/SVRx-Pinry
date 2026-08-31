#!/usr/bin/env python
import argparse
import os
import stat
import sys


class ExportStorageBootstrapError(Exception):
    pass


def _positive_decimal(value):
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdecimal()
    ):
        raise argparse.ArgumentTypeError("positive decimal required")
    parsed = int(value, 10)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("positive decimal required")
    return parsed


def _parse_arguments(arguments):
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--uid", required=True, type=_positive_decimal)
    parser.add_argument("--gid", required=True, type=_positive_decimal)
    return parser.parse_args(arguments)


def _directory_flags():
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise ExportStorageBootstrapError()
    return (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )


def _same_directory(first, second):
    return (
        stat.S_ISDIR(first.st_mode)
        and stat.S_ISDIR(second.st_mode)
        and first.st_dev == second.st_dev
        and first.st_ino == second.st_ino
    )


def _verify_named_directory(parent_fd, name, descriptor):
    opened = os.fstat(descriptor)
    named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not _same_directory(opened, named):
        raise ExportStorageBootstrapError()
    return opened


def _open_absolute_directory(path):
    if not isinstance(path, str) or not os.path.isabs(path):
        raise ExportStorageBootstrapError()
    components = [part for part in path.split(os.sep) if part]
    if not components or any(part in (".", "..") for part in components):
        raise ExportStorageBootstrapError()
    flags = _directory_flags()
    descriptor = os.open(os.sep, flags)
    try:
        for component in components:
            child = os.open(component, flags, dir_fd=descriptor)
            try:
                _verify_named_directory(descriptor, component, child)
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_or_create_directory(parent_fd, name, uid, gid):
    if name not in ("exports", ".staging", "ready"):
        raise ExportStorageBootstrapError()
    flags = _directory_flags()
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    try:
        opened = _verify_named_directory(parent_fd, name, descriptor)
        os.fchown(descriptor, uid, gid)
        os.fchmod(descriptor, 0o700)
        normalized = _verify_named_directory(parent_fd, name, descriptor)
        if (
            normalized.st_dev != opened.st_dev
            or normalized.st_ino != opened.st_ino
            or normalized.st_uid != uid
            or normalized.st_gid != gid
            or stat.S_IMODE(normalized.st_mode) != 0o700
        ):
            raise ExportStorageBootstrapError()
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def bootstrap(data_root, uid, gid):
    data_fd = _open_absolute_directory(data_root)
    exports_fd = None
    staging_fd = None
    ready_fd = None
    try:
        exports_fd = _open_or_create_directory(
            data_fd, "exports", uid, gid
        )
        staging_fd = _open_or_create_directory(
            exports_fd, ".staging", uid, gid
        )
        ready_fd = _open_or_create_directory(
            exports_fd, "ready", uid, gid
        )
        _verify_named_directory(exports_fd, ".staging", staging_fd)
        _verify_named_directory(exports_fd, "ready", ready_fd)
        _verify_named_directory(data_fd, "exports", exports_fd)
    finally:
        for descriptor in (ready_fd, staging_fd, exports_fd, data_fd):
            if descriptor is not None:
                os.close(descriptor)


def _write_error():
    try:
        sys.stderr.write("export_storage_unsafe\n")
        sys.stderr.flush()
    except (IOError, OSError):
        pass


def main(arguments):
    parsed = _parse_arguments(arguments)
    try:
        os.umask(0o077)
        bootstrap(parsed.data_root, parsed.uid, parsed.gid)
        return 0
    except BaseException as error:
        if not isinstance(error, Exception):
            raise
        _write_error()
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
