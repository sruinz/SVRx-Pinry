#!/bin/sh
set -eu

if [ "$#" -gt 1 ]; then
    echo "usage: $0 [image:tag]" >&2
    exit 2
fi

script_directory="$(
    CDPATH= cd -- "$(dirname -- "$0")" >/dev/null 2>&1
    pwd -P
)"
build_context="${script_directory}/context"

if [ ! -f "${script_directory}/BUILD_INFO" ]; then
    echo "missing_build_input=BUILD_INFO" >&2
    exit 1
fi
for required_path in \
    Dockerfile.autobuild \
    requirements.txt \
    manage.py \
    pinry \
    docker/scripts/start.sh
do
    if [ ! -e "${build_context}/${required_path}" ]; then
        echo "missing_build_input=${required_path}" >&2
        exit 1
    fi
done

if ! command -v docker >/dev/null 2>&1; then
    echo "docker_not_found" >&2
    exit 1
fi

source_commit_count="$(
    awk '/^source_commit=/{count++} END{print count + 0}' \
        "${script_directory}/BUILD_INFO"
)"
if [ "${source_commit_count}" -ne 1 ]; then
    echo "invalid_build_info" >&2
    exit 1
fi
source_commit="$(
    sed -n 's/^source_commit=//p' "${script_directory}/BUILD_INFO"
)"
if [ "${#source_commit}" -ne 40 ]; then
    echo "invalid_build_info" >&2
    exit 1
fi
case "${source_commit}" in
    *[!0-9a-f]*)
        echo "invalid_build_info" >&2
        exit 1
        ;;
esac

default_image_count="$(
    awk '/^default_image=/{count++} END{print count + 0}' \
        "${script_directory}/BUILD_INFO"
)"
if [ "${default_image_count}" -ne 1 ]; then
    echo "invalid_build_info" >&2
    exit 1
fi
default_image="$(
    sed -n 's/^default_image=//p' "${script_directory}/BUILD_INFO"
)"
if [ -z "${default_image}" ]; then
    echo "invalid_build_info" >&2
    exit 1
fi
image_tag="${1:-${default_image}}"

cd "${build_context}"
docker build \
    --pull \
    --file Dockerfile.autobuild \
    --build-arg "PINRY_SOURCE_COMMIT=${source_commit}" \
    --label "org.opencontainers.image.revision=${source_commit}" \
    --tag "${image_tag}" \
    .

printf 'image_built=%s\n' "${image_tag}"
