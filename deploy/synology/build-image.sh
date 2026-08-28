#!/bin/sh
set -eu
export LC_ALL=C

if [ "$#" -gt 1 ]; then
    echo "usage: $0 [image:tag]" >&2
    exit 2
fi

script_directory="$(
    CDPATH= cd -- "$(dirname -- "$0")" >/dev/null 2>&1
    pwd -P
)"
build_context="${script_directory}/context"

is_unsymlinked_path() (
    root_path="$1"
    remaining_path="$2"
    expected_type="$3"
    current_path="${root_path}"

    if [ ! -d "${current_path}" ] || [ -L "${current_path}" ]; then
        exit 1
    fi
    while [ "${remaining_path#*/}" != "${remaining_path}" ]; do
        component="${remaining_path%%/*}"
        case "${component}" in
            '' | . | ..)
                exit 1
                ;;
        esac
        current_path="${current_path}/${component}"
        if [ ! -d "${current_path}" ] || [ -L "${current_path}" ]; then
            exit 1
        fi
        remaining_path="${remaining_path#*/}"
    done
    case "${remaining_path}" in
        '' | . | ..)
            exit 1
            ;;
    esac
    current_path="${current_path}/${remaining_path}"
    if [ -L "${current_path}" ]; then
        exit 1
    fi
    case "${expected_type}" in
        regular)
            [ -f "${current_path}" ]
            ;;
        directory)
            [ -d "${current_path}" ]
            ;;
        *)
            exit 1
            ;;
    esac
)

if ! is_unsymlinked_path \
    "${script_directory}" BUILD_INFO regular;
then
    echo "missing_build_input=BUILD_INFO" >&2
    exit 1
fi
for required_path in \
    Dockerfile.autobuild \
    requirements.txt \
    manage.py \
    pinry \
    docker/scripts/start.sh \
    docker/scripts/startup.py \
    docker/scripts/bootstrap.sh \
    docker/scripts/gen_key.sh \
    docker/scripts/normalize_persistent_file.py \
    docker/scripts/_start_gunicorn.sh
do
    required_type=regular
    if [ "${required_path}" = pinry ]; then
        required_type=directory
    fi
    if ! is_unsymlinked_path \
        "${build_context}" "${required_path}" "${required_type}";
    then
        echo "missing_build_input=${required_path}" >&2
        exit 1
    fi
done

for required_path in \
    docker/scripts/migration_worker.py \
    docker/scripts/migration_status.py \
    docker/scripts/supervisor.py \
    docker/migration/index.html \
    docker/migration/migration.css \
    docker/migration/migration.js \
    docker/migration/svrx-pinry-dark-ui.png \
    docker/migration/svrx-pinry-light-ui.png
do
    if ! is_unsymlinked_path \
        "${build_context}" "${required_path}" regular;
    then
        echo "synology_context_incomplete" >&2
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
    *[!0123456789abcdef]*)
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
