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
cd "${script_directory}"

for required_path in \
    BUILD_INFO \
    Dockerfile.autobuild \
    requirements.txt \
    manage.py \
    pinry \
    docker/scripts/start.sh
do
    if [ ! -e "${required_path}" ]; then
        echo "missing_build_input=${required_path}" >&2
        exit 1
    fi
done

if ! command -v docker >/dev/null 2>&1; then
    echo "docker_not_found" >&2
    exit 1
fi

default_image="$(sed -n 's/^default_image=//p' BUILD_INFO)"
if [ -z "${default_image}" ]; then
    echo "invalid_build_info" >&2
    exit 1
fi
image_tag="${1:-${default_image}}"

docker build \
    --pull \
    --file Dockerfile.autobuild \
    --tag "${image_tag}" \
    .

printf 'image_built=%s\n' "${image_tag}"
