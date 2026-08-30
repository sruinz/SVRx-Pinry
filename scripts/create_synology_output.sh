#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C

if [ "$#" -gt 1 ]; then
    echo "usage: $0 [output-directory]" >&2
    exit 2
fi

script_directory="$(
    cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1
    pwd -P
)"
repository_root="$(cd "${script_directory}/.." && pwd -P)"
output_root="${1:-}"
output_parent=""

cd "${repository_root}"
source_commit="$(git rev-parse --verify 'HEAD^{commit}')"
if [ "${#source_commit}" -ne 40 ]; then
    echo "invalid_source_commit" >&2
    exit 1
fi
case "${source_commit}" in
    *[!0123456789abcdef]*)
        echo "invalid_source_commit" >&2
        exit 1
        ;;
esac
package_control_paths=(
    scripts/create_synology_output.sh
    deploy/synology/build-image.sh
    deploy/synology/docker-compose.sw-transition.yml
    deploy/synology/docker-compose.synology.yml
    deploy/synology/README_KO.md
    docker/tests/nas_legacy_clone_acceptance.sh
    docker/tests/fixtures/create_legacy_fixture.py
    LICENSE.md
    NOTICE.md
    UPSTREAM.md
)
package_source_paths=(
    Dockerfile.autobuild
    Dockerfile.sw-transition
    requirements.txt
    manage.py
    core
    exports
    django_images
    pinry
    pinry_plugins
    users
    pinry-spa
    docker/nginx
    docker/scripts
    docker/migration
    docker/sw-transition
)
if ! git diff --quiet "${source_commit}" -- \
        "${package_control_paths[@]}" "${package_source_paths[@]}" \
    || ! git diff --cached --quiet "${source_commit}" -- \
        "${package_control_paths[@]}" "${package_source_paths[@]}";
then
    echo "tracked_package_control_mismatch" >&2
    exit 1
fi
short_commit="${source_commit:0:12}"
if [ -z "${output_root}" ]; then
    common_git_directory="$(
        cd "$(git rev-parse --git-common-dir)" >/dev/null 2>&1
        pwd -P
    )"
    canonical_checkout="$(cd "${common_git_directory}/.." && pwd -P)"
    workspace_root="$(cd "${canonical_checkout}/.." && pwd -P)"
    output_parent="${workspace_root}/output"
    if [ -L "${output_parent}" ] \
        || { [ -e "${output_parent}" ] && [ ! -d "${output_parent}" ]; };
    then
        echo "output_parent_invalid=${output_parent}" >&2
        exit 1
    fi
    if [ ! -e "${output_parent}" ]; then
        mkdir "${output_parent}"
    fi
    canonical_output_parent="$(cd "${output_parent}" && pwd -P)"
    if [ "${canonical_output_parent}" != "${output_parent}" ]; then
        echo "output_parent_invalid=${output_parent}" >&2
        exit 1
    fi
    output_root="${canonical_output_parent}/svrx-pinry-server-${short_commit}"
else
    if [ -e "${output_root}" ] || [ -L "${output_root}" ]; then
        echo "output_already_exists=${output_root}" >&2
        exit 1
    fi
    output_name="$(basename "${output_root}")"
    case "${output_name}" in
        '' | . | .. | /)
            echo "output_path_invalid=${output_root}" >&2
            exit 1
            ;;
    esac
    output_parent="$(dirname "${output_root}")"
    if [ -L "${output_parent}" ] \
        || { [ -e "${output_parent}" ] && [ ! -d "${output_parent}" ]; };
    then
        echo "output_parent_invalid=${output_parent}" >&2
        exit 1
    fi
    if [ ! -e "${output_parent}" ]; then
        mkdir -p "${output_parent}"
    fi
    canonical_output_parent="$(cd "${output_parent}" && pwd -P)"
    output_parent="${canonical_output_parent}"
    output_root="${canonical_output_parent}/${output_name}"
fi

if [ -e "${output_root}" ] || [ -L "${output_root}" ]; then
    echo "output_already_exists=${output_root}" >&2
    exit 1
fi

package_name="svrx-pinry"
package_directory="${output_root}/${package_name}"
archive_path="${output_root}/${package_name}-${short_commit}.tar.gz"
transition_name="sw-transition"
transition_directory="${output_root}/${transition_name}"
accept_tools_name="accept-tools"
accept_tools_directory="${output_root}/${accept_tools_name}"

temporary_root=""
temporary_root_identity=""
temporary_archive=""
temporary_archive_identity=""
temporary_directory_identity=""
temporary_transition_directory_identity=""
temporary_accept_tools_directory_identity=""
output_parent_identity=""
temporary_path_identity() {
    python3 -c '
import os
import stat
import sys

path = sys.argv[1]
entry = os.lstat(path)
if stat.S_ISLNK(entry.st_mode):
    sys.exit(1)
print(f"{entry.st_dev}:{entry.st_ino}")
' "$1"
}

output_parent_identity="$(temporary_path_identity "${output_parent}")"

remove_owned_temporary_path() {
    python3 -c '
import os
import stat
import sys

path, identity, expected_kind = sys.argv[1:]
if not path or not identity:
    sys.exit(0)
try:
    expected_device, expected_inode = (int(value) for value in identity.split(":"))
    parent_path, name = os.path.split(path)
    parent_fd = os.open(parent_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        entry = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (entry.st_dev, entry.st_ino) != (expected_device, expected_inode):
            sys.exit(0)
        if expected_kind == "file":
            if stat.S_ISREG(entry.st_mode):
                os.unlink(name, dir_fd=parent_fd)
            sys.exit(0)
        if not stat.S_ISDIR(entry.st_mode):
            sys.exit(0)
        directory_fd = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        try:
            confirmed = os.fstat(directory_fd)
            if (confirmed.st_dev, confirmed.st_ino) != (expected_device, expected_inode):
                sys.exit(0)
            if os.listdir(directory_fd):
                print(f"cleanup_deferred={path}", file=sys.stderr)
                sys.exit(0)
        finally:
            os.close(directory_fd)
        try:
            os.rmdir(name, dir_fd=parent_fd)
        except OSError:
            print(f"cleanup_deferred={path}", file=sys.stderr)
    finally:
        os.close(parent_fd)
except (FileNotFoundError, NotADirectoryError, OSError, ValueError):
    sys.exit(0)
' "$1" "$2" "$3"
}

cleanup_temporary_files() {
    remove_owned_temporary_path \
        "${temporary_root}" "${temporary_root_identity}" directory
    remove_owned_temporary_path \
        "${temporary_archive}" "${temporary_archive_identity}" file
}
trap cleanup_temporary_files EXIT

is_unsymlinked_regular_file() {
    local root_path="$1"
    local remaining_path="$2"
    local current_path="${root_path}"
    local component

    if [ ! -d "${current_path}" ] || [ -L "${current_path}" ]; then
        return 1
    fi
    while [ "${remaining_path#*/}" != "${remaining_path}" ]; do
        component="${remaining_path%%/*}"
        case "${component}" in
            '' | . | ..)
                return 1
                ;;
        esac
        current_path="${current_path}/${component}"
        if [ ! -d "${current_path}" ] || [ -L "${current_path}" ]; then
            return 1
        fi
        remaining_path="${remaining_path#*/}"
    done
    case "${remaining_path}" in
        '' | . | ..)
            return 1
            ;;
    esac
    current_path="${current_path}/${remaining_path}"
    [ -f "${current_path}" ] && [ ! -L "${current_path}" ]
}

publish_entry() {
    local source_path="$1"
    local destination_path="$2"
    local source_identity="$3"
    local source_kind="$4"
    local source_parent_identity="$5"
    local destination_parent_identity="$6"
    python3 -c '
import ctypes
import os
import platform
import stat
import sys

(
    source_identity,
    source_kind,
    source_parent_identity,
    destination_parent_identity,
    source_path,
    destination_path,
) = sys.argv[1:]

def parse_identity(value):
    return tuple(int(part) for part in value.split(":"))

def open_parent(path, expected_identity):
    parent_path, name = os.path.split(path)
    if not parent_path or name in ("", ".", "..") or os.sep in name:
        raise OSError("invalid path")
    descriptor = os.open(
        parent_path,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    confirmed = os.fstat(descriptor)
    if (confirmed.st_dev, confirmed.st_ino) != parse_identity(
            expected_identity):
        os.close(descriptor)
        raise OSError("parent identity changed")
    return descriptor, name

try:
    source_parent_fd, source_name = open_parent(
        source_path, source_parent_identity
    )
    destination_parent_fd, destination_name = open_parent(
        destination_path, destination_parent_identity
    )
except (OSError, ValueError):
    sys.exit(1)

try:
    source_entry = os.stat(
        source_name,
        dir_fd=source_parent_fd,
        follow_symlinks=False,
    )
    if (source_entry.st_dev, source_entry.st_ino) != parse_identity(
            source_identity):
        sys.exit(1)
    if source_kind == "directory":
        if not stat.S_ISDIR(source_entry.st_mode):
            sys.exit(1)
    elif source_kind == "file":
        if not stat.S_ISREG(source_entry.st_mode):
            sys.exit(1)
    else:
        sys.exit(1)

    libc = ctypes.CDLL(None, use_errno=True)
    if platform.system() == "Darwin":
        rename = libc.renameatx_np
        no_replace = 4
    elif platform.system() == "Linux":
        rename = libc.renameat2
        no_replace = 1
    else:
        sys.exit(1)
    rename.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    rename.restype = ctypes.c_int
    if rename(
        source_parent_fd,
        os.fsencode(source_name),
        destination_parent_fd,
        os.fsencode(destination_name),
        no_replace,
    ) != 0:
        sys.exit(1)
    published = os.stat(
        destination_name,
        dir_fd=destination_parent_fd,
        follow_symlinks=False,
    )
    if (published.st_dev, published.st_ino) != (
            source_entry.st_dev, source_entry.st_ino):
        sys.exit(1)
finally:
    os.close(destination_parent_fd)
    os.close(source_parent_fd)
' "${source_identity}" "${source_kind}" \
    "${source_parent_identity}" "${destination_parent_identity}" \
    "${source_path}" "${destination_path}"
}

install_git_blob() {
    local source_path="$1"
    local destination_path="$2"
    local destination_mode="$3"

    git cat-file blob "${source_commit}:${source_path}" \
        | python3 -c '
import os
import shutil
import sys

destination_path, destination_mode = sys.argv[1:]
parent_path, name = os.path.split(destination_path)
if not parent_path or name in ("", ".", "..") or os.sep in name:
    sys.exit(1)
parent_fd = os.open(
    parent_path,
    os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
)
descriptor = None
try:
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=parent_fd,
    )
    with os.fdopen(descriptor, "wb", closefd=False) as destination:
        shutil.copyfileobj(sys.stdin.buffer, destination)
        destination.flush()
    os.fchmod(descriptor, int(destination_mode, 8))
finally:
    if descriptor is not None:
        os.close(descriptor)
    os.close(parent_fd)
' "${destination_path}" "${destination_mode}"
}

create_archive() {
    local archive_path="$1"
    local archive_identity="$2"
    local archive_parent_identity="$3"
    local source_root="$4"
    local source_root_identity="$5"
    local archive_package_name="$6"
    python3 -c '
import os
import stat
import subprocess
import sys

(
    archive_identity,
    archive_parent_identity,
    source_root_identity,
    archive_path,
    source_root,
    package_name,
) = sys.argv[1:]

def parse_identity(value):
    return tuple(int(part) for part in value.split(":"))

archive_parent, archive_name = os.path.split(archive_path)
if not archive_parent or archive_name in ("", ".", ".."):
    sys.exit(1)
try:
    parent_fd = os.open(
        archive_parent,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    parent_entry = os.fstat(parent_fd)
    if (parent_entry.st_dev, parent_entry.st_ino) != parse_identity(
            archive_parent_identity):
        sys.exit(1)
    archive_fd = os.open(
        archive_name,
        os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_fd,
    )
    archive_entry = os.fstat(archive_fd)
    if not stat.S_ISREG(archive_entry.st_mode) or (
        archive_entry.st_dev,
        archive_entry.st_ino,
    ) != parse_identity(archive_identity):
        sys.exit(1)
    os.ftruncate(archive_fd, 0)
    source_fd = os.open(
        source_root,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    source_entry = os.fstat(source_fd)
    if (source_entry.st_dev, source_entry.st_ino) != parse_identity(
            source_root_identity):
        sys.exit(1)
    os.fchdir(source_fd)
    environment = os.environ.copy()
    environment["COPYFILE_DISABLE"] = "1"
    tar_process = subprocess.Popen(
        [
            "tar",
            "--no-xattrs",
            "--exclude=.DS_Store",
            "--exclude=*/.DS_Store",
            "--exclude=._*",
            "--exclude=*/._*",
            "-cf",
            "-",
            "-C",
            ".",
            f"{package_name}/BUILD_INFO",
            f"{package_name}/LICENSE.md",
            f"{package_name}/NOTICE.md",
            f"{package_name}/README_KO.md",
            f"{package_name}/UPSTREAM.md",
            f"{package_name}/build-image.sh",
            f"{package_name}/context",
            f"{package_name}/docker-compose.yml",
        ],
        env=environment,
        stdout=subprocess.PIPE,
    )
    try:
        completed = subprocess.run(
            ["gzip", "-n", "-c"],
            stdin=tar_process.stdout,
            stdout=archive_fd,
        )
    finally:
        if tar_process.stdout is not None:
            tar_process.stdout.close()
    tar_returncode = tar_process.wait()
    if tar_returncode != 0:
        sys.exit(tar_returncode)
    if completed.returncode != 0:
        sys.exit(completed.returncode)
finally:
    for descriptor_name in ("source_fd", "archive_fd", "parent_fd"):
        descriptor = locals().get(descriptor_name)
        if descriptor is not None:
            os.close(descriptor)
' "${archive_identity}" "${archive_parent_identity}" \
    "${source_root_identity}" "${archive_path}" "${source_root}" \
    "${archive_package_name}"
}

sync_directory_tree() {
    python3 -c '
import fcntl
import os
import platform
import stat
import sys

mode, root_path = sys.argv[1:]
if mode != "tree":
    sys.exit(2)

def sync_fd(fd, full=False):
    if full and platform.system() == "Darwin" \
            and hasattr(fcntl, "F_FULLFSYNC"):
        try:
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
            return
        except OSError:
            pass
    os.fsync(fd)

for current_root, directory_names, file_names in os.walk(
        root_path, topdown=False, followlinks=False):
    for file_name in file_names:
        file_path = os.path.join(current_root, file_name)
        entry = os.lstat(file_path)
        if not stat.S_ISREG(entry.st_mode):
            continue
        descriptor = os.open(
            file_path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            confirmed = os.fstat(descriptor)
            if not stat.S_ISREG(confirmed.st_mode) \
                    or (confirmed.st_dev, confirmed.st_ino) != (
                        entry.st_dev, entry.st_ino
                    ):
                sys.exit(1)
            sync_fd(descriptor, full=True)
        finally:
            os.close(descriptor)
    descriptor = os.open(
        current_root,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        sync_fd(descriptor)
    finally:
        os.close(descriptor)
' tree "$1"
}

sync_directory_entry() {
    python3 -c '
import os
import sys

mode, directory_path = sys.argv[1:]
if mode != "directory":
    sys.exit(2)
descriptor = os.open(
    directory_path,
    os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
)
try:
    os.fsync(descriptor)
finally:
    os.close(descriptor)
' directory "$1"
}

temporary_root="$(
    mktemp -d \
        "${output_parent}/.svrx-pinry-server-${short_commit}.tmp.XXXXXX"
)"
temporary_root_identity="$(temporary_path_identity "${temporary_root}")"
temporary_directory="${temporary_root}/${package_name}"
temporary_transition_directory="${temporary_root}/${transition_name}"
temporary_accept_tools_directory="${temporary_root}/${accept_tools_name}"
temporary_archive="$(
    mktemp "${temporary_root}/.${package_name}.archive.XXXXXX"
)"
temporary_archive_identity="$(temporary_path_identity "${temporary_archive}")"

mkdir "${temporary_directory}"
mkdir "${temporary_directory}/context"
mkdir "${temporary_transition_directory}"
mkdir "${temporary_accept_tools_directory}"
mkdir "${temporary_accept_tools_directory}/fixtures"
temporary_directory_identity="$(
    temporary_path_identity "${temporary_directory}"
)"
temporary_transition_directory_identity="$(
    temporary_path_identity "${temporary_transition_directory}"
)"
temporary_accept_tools_directory_identity="$(
    temporary_path_identity "${temporary_accept_tools_directory}"
)"
git archive --format=tar "${source_commit}" -- \
    Dockerfile.autobuild \
    LICENSE.md \
    NOTICE.md \
    UPSTREAM.md \
    requirements.txt \
    manage.py \
    core \
    exports \
    django_images \
    pinry \
    pinry_plugins \
    users \
    pinry-spa \
    docker/nginx \
    docker/scripts \
    docker/migration \
    ':(exclude)core/tests' \
    ':(exclude)exports/tests' \
    ':(exclude)django_images/test_*.py' \
    ':(exclude)django_images/tests.py' \
    ':(exclude)pinry/settings/test_sqlite_file.py' \
    ':(exclude)pinry/settings/development.py' \
    ':(exclude)pinry_plugins/tests.py' \
    ':(exclude)users/test_*.py' \
    ':(exclude)users/tests.py' \
    ':(exclude)pinry-spa/.editorconfig' \
    ':(exclude)pinry-spa/.gitignore' \
    ':(exclude)pinry-spa/README.md' \
    ':(exclude)pinry-spa/tests' \
    ':(exclude)pinry-spa/jest.config.js' \
    ':(exclude,glob)**/.DS_Store' \
    ':(exclude,glob)**/._*' \
    | tar -xf - -C "${temporary_directory}/context"
git archive --format=tar "${source_commit}" -- \
    Dockerfile.sw-transition \
    LICENSE.md \
    NOTICE.md \
    UPSTREAM.md \
    docker/sw-transition \
    pinry-spa/src/service-worker.js \
    ':(exclude,glob)**/.DS_Store' \
    ':(exclude,glob)**/._*' \
    | tar -xf - -C "${temporary_transition_directory}"
required_startup_paths=(
    docker/scripts/start.sh
    docker/scripts/startup.py
    docker/scripts/bootstrap.sh
    docker/scripts/gen_key.sh
    docker/scripts/normalize_persistent_file.py
    docker/scripts/_start_gunicorn.sh
    docker/scripts/migration_worker.py
    docker/scripts/migration_status.py
    docker/scripts/supervisor.py
)
for required_path in "${required_startup_paths[@]}"; do
    if ! is_unsymlinked_regular_file \
        "${temporary_directory}/context" "${required_path}";
    then
        echo "package_layout_changed" >&2
        exit 1
    fi
done
required_maintenance_paths=(
    docker/migration/index.html
    docker/migration/migration.css
    docker/migration/migration.js
    docker/migration/svrx-pinry-dark-ui.png
    docker/migration/svrx-pinry-light-ui.png
    pinry-spa/src/service-worker.js
)
for required_path in "${required_maintenance_paths[@]}"; do
    if ! is_unsymlinked_regular_file \
        "${temporary_directory}/context" "${required_path}";
    then
        echo "package_layout_changed" >&2
        exit 1
    fi
done
required_transition_paths=(
    Dockerfile.sw-transition
    LICENSE.md
    NOTICE.md
    UPSTREAM.md
    docker/sw-transition/index.html
    docker/sw-transition/nginx.conf
    pinry-spa/src/service-worker.js
)
for required_path in "${required_transition_paths[@]}"; do
    if ! is_unsymlinked_regular_file \
        "${temporary_transition_directory}" "${required_path}";
    then
        echo "package_layout_changed" >&2
        exit 1
    fi
done
install_git_blob \
    deploy/synology/docker-compose.sw-transition.yml \
    "${temporary_transition_directory}/docker-compose.yml" 0644
printf '%s\n' \
    Dockerfile.autobuild \
    .dockerignore \
    .DS_Store \
    '**/.DS_Store' \
    '._*' \
    '**/._*' \
    > "${temporary_directory}/context/.dockerignore"
install_git_blob \
    deploy/synology/build-image.sh \
    "${temporary_directory}/build-image.sh" 0755
install_git_blob \
    deploy/synology/docker-compose.synology.yml \
    "${temporary_directory}/docker-compose.yml" 0644
install_git_blob \
    deploy/synology/README_KO.md \
    "${temporary_directory}/README_KO.md" 0644
for legal_document in LICENSE.md NOTICE.md UPSTREAM.md; do
    install_git_blob \
        "${legal_document}" \
        "${temporary_directory}/${legal_document}" 0644
done
printf 'source_commit=%s\ndefault_image=svrx-pinry:latest\n' \
    "${source_commit}" \
    > "${temporary_directory}/BUILD_INFO"
install -m 0644 \
    "${temporary_directory}/BUILD_INFO" \
    "${temporary_accept_tools_directory}/BUILD_INFO"
install_git_blob \
    docker/tests/nas_legacy_clone_acceptance.sh \
    "${temporary_accept_tools_directory}/nas_legacy_clone_acceptance.sh" 0755
install_git_blob \
    docker/tests/fixtures/create_legacy_fixture.py \
    "${temporary_accept_tools_directory}/fixtures/create_legacy_fixture.py" 0644

archive_parent_identity="${temporary_root_identity}"
create_archive \
    "${temporary_archive}" "${temporary_archive_identity}" \
    "${archive_parent_identity}" \
    "${temporary_root}" "${temporary_root_identity}" \
    "${package_name}"
shopt -s nullglob dotglob
package_entries=("${temporary_directory}"/*)
shopt -u dotglob nullglob
if [ "${#package_entries[@]}" -ne 8 ] \
    || [ ! -f "${temporary_directory}/BUILD_INFO" ] \
    || [ ! -f "${temporary_directory}/LICENSE.md" ] \
    || [ ! -f "${temporary_directory}/NOTICE.md" ] \
    || [ ! -f "${temporary_directory}/README_KO.md" ] \
    || [ ! -f "${temporary_directory}/UPSTREAM.md" ] \
    || [ ! -f "${temporary_directory}/build-image.sh" ] \
    || [ ! -d "${temporary_directory}/context" ] \
    || [ ! -f "${temporary_directory}/docker-compose.yml" ]; then
    echo "package_layout_changed" >&2
    exit 1
fi
shopt -s nullglob dotglob
accept_tools_entries=("${temporary_accept_tools_directory}"/*)
accept_tools_fixture_entries=(
    "${temporary_accept_tools_directory}/fixtures"/*
)
shopt -u dotglob nullglob
if [ "${#accept_tools_entries[@]}" -ne 3 ] \
    || [ "${#accept_tools_fixture_entries[@]}" -ne 1 ] \
    || [ ! -f "${temporary_accept_tools_directory}/BUILD_INFO" ] \
    || [ ! -f "${temporary_accept_tools_directory}/nas_legacy_clone_acceptance.sh" ] \
    || [ ! -d "${temporary_accept_tools_directory}/fixtures" ] \
    || [ ! -f "${temporary_accept_tools_directory}/fixtures/create_legacy_fixture.py" ]; then
    echo "package_layout_changed" >&2
    exit 1
fi
staged_archive_path="${temporary_root}/$(basename "${archive_path}")"
if publish_entry \
    "${temporary_archive}" "${staged_archive_path}" \
    "${temporary_archive_identity}" file \
    "${temporary_root_identity}" "${temporary_root_identity}";
then
    temporary_archive="${staged_archive_path}"
else
    echo "publish_destination_raced=${archive_path}" >&2
    exit 1
fi
shopt -s dotglob nullglob
bundle_entries=("${temporary_root}"/*)
shopt -u dotglob nullglob
if [ "${#bundle_entries[@]}" -ne 4 ] \
    || [ ! -d "${temporary_directory}" ] \
    || [ -L "${temporary_directory}" ] \
    || [ ! -d "${temporary_transition_directory}" ] \
    || [ -L "${temporary_transition_directory}" ] \
    || [ ! -d "${temporary_accept_tools_directory}" ] \
    || [ -L "${temporary_accept_tools_directory}" ] \
    || [ ! -f "${staged_archive_path}" ] \
    || [ -L "${staged_archive_path}" ]; then
    echo "package_layout_changed" >&2
    exit 1
fi
sync_directory_tree "${temporary_root}"
if publish_entry \
    "${temporary_root}" "${output_root}" \
    "${temporary_root_identity}" directory \
    "${output_parent_identity}" "${output_parent_identity}";
then
    :
else
    echo "publish_destination_raced=${output_root}" >&2
    exit 1
fi
sync_directory_entry "${output_parent}"

printf 'upload_directory=%s\n' "${package_directory}"
printf 'upload_archive=%s\n' "${archive_path}"
