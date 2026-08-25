#!/bin/bash
set -euo pipefail

umask 077

data_root="${PINRY_DATA_ROOT:-/data}"
project_root="${PINRY_PROJECT_ROOT:-/pinry}"
data_settings="${data_root}/local_settings.py"
key_file="${data_root}/production_secret_key.txt"
gen_key_script="${project_root}/docker/scripts/gen_key.sh"
settings_directory="${project_root}/pinry/settings"
settings_template="${settings_directory}/local_settings.example.py"
project_settings="${settings_directory}/local_settings.py"
data_temp=""
project_temp=""
service_uid=""
service_gid=""

cleanup() {
    if [ -n "${data_temp}" ] && [ -e "${data_temp}" ]; then
        rm -f "${data_temp}"
    fi
    if [ -n "${project_temp}" ] && [ -e "${project_temp}" ]; then
        rm -f "${project_temp}"
    fi
}
trap cleanup EXIT HUP INT TERM

stat_value() {
    local gnu_format="$1"
    local bsd_format="$2"
    local path="$3"
    local value

    if value="$(stat -c "${gnu_format}" "${path}" 2>/dev/null)"; then
        printf '%s\n' "${value}"
        return
    fi
    stat -f "${bsd_format}" "${path}"
}

allowed_owner() {
    local path_uid="$1"

    if [ "${path_uid}" = "$(id -u)" ]; then
        return 0
    fi
    [ -n "${service_uid}" ] && [ "${path_uid}" = "${service_uid}" ]
}

validate_regular_file() {
    local path="$1"
    local link_count
    local mode
    local mode_value
    local owner_uid

    [ ! -L "${path}" ] && [ -f "${path}" ]
    link_count="$(stat_value '%h' '%l' "${path}")"
    [ "${link_count}" = "1" ]
    owner_uid="$(stat_value '%u' '%u' "${path}")"
    allowed_owner "${owner_uid}"
    mode="$(stat_value '%a' '%Lp' "${path}")"
    case "${mode}" in
        [0-7][0-7][0-7]|[0-7][0-7][0-7][0-7]) ;;
        *) return 1 ;;
    esac
    mode_value=$((8#${mode}))
    [ $((mode_value & 0022)) -eq 0 ]
    [ $((mode_value & 0111)) -eq 0 ]
    [ $((mode_value & 0400)) -ne 0 ]
    [ "$(LC_ALL=C wc -c < "${path}")" -gt 0 ]
    [ "$(LC_ALL=C wc -c < "${path}")" -le 1048576 ]
}

validate_service_settings() {
    local path="$1"
    local mode

    validate_regular_file "${path}"
    [ "$(stat_value '%u' '%u' "${path}")" = "${service_uid}" ]
    [ "$(stat_value '%g' '%g' "${path}")" = "${service_gid}" ]
    mode="$(stat_value '%a' '%Lp' "${path}")"
    [ $((8#${mode})) -eq $((8#600)) ]
}

read_key() {
    local path="$1"
    local byte_count
    local key
    local line_count

    validate_regular_file "${path}"
    byte_count="$(LC_ALL=C wc -c < "${path}")"
    line_count="$(LC_ALL=C wc -l < "${path}")"
    [ "${byte_count}" -eq 66 ]
    [ "${line_count}" -eq 1 ]
    IFS= read -r key < "${path}"
    [[ "${key}" =~ ^[A-Za-z0-9]{65}$ ]]
    printf '%s' "${key}"
}

[ -d "${data_root}" ] && [ ! -L "${data_root}" ]
[ -d "${settings_directory}" ] && [ ! -L "${settings_directory}" ]
service_uid="$(id -u www-data)"
service_gid="$(id -g www-data)"
case "${service_uid}" in
    ''|*[!0-9]*) exit 1 ;;
esac
case "${service_gid}" in
    ''|*[!0-9]*) exit 1 ;;
esac

if [ -e "${data_settings}" ] || [ -L "${data_settings}" ]; then
    validate_regular_file "${data_settings}"
    if grep -q 'secret_key_place_holder' "${data_settings}"; then
        exit 1
    fi
    data_temp="$(mktemp "${data_root}/.local_settings.py.tmp-XXXXXX")"
    cp "${data_settings}" "${data_temp}"
    chmod 0600 "${data_temp}"
    mv -f "${data_temp}" "${data_settings}"
    data_temp=""
else
    /bin/bash "${gen_key_script}" >/dev/null 2>/dev/null
    secret_key="$(read_key "${key_file}")"
    validate_regular_file "${settings_template}"
    placeholder_count="$(
        grep -o 'secret_key_place_holder' "${settings_template}" | wc -l
    )"
    [ "${placeholder_count}" -eq 1 ]
    data_temp="$(mktemp "${data_root}/.local_settings.py.tmp-XXXXXX")"
    sed "s/secret_key_place_holder/${secret_key}/" \
        "${settings_template}" > "${data_temp}"
    if grep -q 'secret_key_place_holder' "${data_temp}"; then
        exit 1
    fi
    grep -Fq "${secret_key}" "${data_temp}"
    chmod 0600 "${data_temp}"
    mv -f "${data_temp}" "${data_settings}"
    data_temp=""
fi

validate_regular_file "${data_settings}"
chmod 0600 "${data_settings}"
if grep -q 'secret_key_place_holder' "${data_settings}"; then
    exit 1
fi

if [ -e "${project_settings}" ] || [ -L "${project_settings}" ]; then
    validate_regular_file "${project_settings}"
fi
project_temp="$(mktemp "${settings_directory}/.local_settings.py.tmp-XXXXXX")"
cp "${data_settings}" "${project_temp}"
chmod 0600 "${project_temp}"
chown "${service_uid}:${service_gid}" "${project_temp}"
cmp -s "${data_settings}" "${project_temp}"
validate_service_settings "${project_temp}"
mv -f "${project_temp}" "${project_settings}"
project_temp=""
validate_service_settings "${project_settings}"
cmp -s "${data_settings}" "${project_settings}"
