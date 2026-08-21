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
output_root="${1:-${repository_root}/output/synology}"

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
    deploy/synology/docker-compose.synology.yml
    deploy/synology/.env.example
)
if ! git diff --quiet "${source_commit}" -- "${package_control_paths[@]}" \
    || ! git diff --cached --quiet "${source_commit}" -- \
        "${package_control_paths[@]}";
then
    echo "tracked_package_control_mismatch" >&2
    exit 1
fi
short_commit="${source_commit:0:12}"

mkdir -p "${output_root}"
output_root="$(cd "${output_root}" && pwd -P)"

package_name="pinry-custom"
package_directory="${output_root}/${package_name}"
archive_path="${output_root}/${package_name}-${short_commit}.tar.gz"

if [ -e "${package_directory}" ] || [ -e "${archive_path}" ]; then
    echo "output_already_exists=${package_name}" >&2
    exit 1
fi

temporary_root="$(
    mktemp -d "${output_root}/.${package_name}.tmp.XXXXXX"
)"
temporary_directory="${temporary_root}/${package_name}"
temporary_archive="$(
    mktemp "${output_root}/.${package_name}.archive.XXXXXX"
)"

cleanup_temporary_files() {
    if [ -d "${temporary_root}" ]; then
        rm -rf -- "${temporary_root}"
    fi
    if [ -f "${temporary_archive}" ]; then
        rm -f -- "${temporary_archive}"
    fi
}
trap cleanup_temporary_files EXIT

mkdir "${temporary_directory}"
mkdir "${temporary_directory}/context"
control_directory="${temporary_root}/controls"
mkdir "${control_directory}"
git archive --format=tar "${source_commit}" -- \
    deploy/synology/build-image.sh \
    deploy/synology/docker-compose.synology.yml \
    deploy/synology/.env.example \
    | tar -xf - -C "${control_directory}"
git archive --format=tar "${source_commit}" -- \
    Dockerfile.autobuild \
    requirements.txt \
    manage.py \
    core \
    django_images \
    pinry \
    pinry_plugins \
    users \
    pinry-spa \
    docker/nginx \
    docker/scripts \
    ':(exclude)core/tests' \
    ':(exclude)django_images/test_*.py' \
    ':(exclude)django_images/tests.py' \
    ':(exclude)pinry/settings/test_sqlite_file.py' \
    ':(exclude)pinry/settings/development.py' \
    ':(exclude)pinry_plugins/tests.py' \
    ':(exclude)users/tests.py' \
    ':(exclude)pinry-spa/.editorconfig' \
    ':(exclude)pinry-spa/.gitignore' \
    ':(exclude)pinry-spa/README.md' \
    ':(exclude)pinry-spa/tests' \
    ':(exclude)pinry-spa/jest.config.js' \
    ':(exclude,glob)**/.DS_Store' \
    | tar -xf - -C "${temporary_directory}/context"
printf 'Dockerfile.autobuild\n.dockerignore\n.DS_Store\n**/.DS_Store\n' \
    > "${temporary_directory}/context/.dockerignore"
install -m 0755 \
    "${control_directory}/deploy/synology/build-image.sh" \
    "${temporary_directory}/build-image.sh"
install -m 0644 \
    "${control_directory}/deploy/synology/docker-compose.synology.yml" \
    "${temporary_directory}/docker-compose.yml"
install -m 0644 \
    "${control_directory}/deploy/synology/.env.example" \
    "${temporary_directory}/.env.example"
printf 'source_commit=%s\ndefault_image=pinry-custom:latest\n' \
    "${source_commit}" \
    > "${temporary_directory}/BUILD_INFO"

COPYFILE_DISABLE=1 \
    tar --exclude='.DS_Store' --exclude='*/.DS_Store' \
    -czf "${temporary_archive}" \
    -C "${temporary_root}" "${package_name}"
mv "${temporary_directory}" "${package_directory}"
if mv "${temporary_archive}" "${archive_path}"; then
    :
else
    archive_status=$?
    mv "${package_directory}" "${temporary_directory}" || :
    exit "${archive_status}"
fi

printf 'upload_directory=%s\n' "${package_directory}"
printf 'upload_archive=%s\n' "${archive_path}"
