#!/bin/bash
set -euo pipefail

umask 077

data_root="${PINRY_DATA_ROOT:-/data}"
key_file="${data_root}/production_secret_key.txt"
temp_file=""
data_owner_uid=""
service_uid=""

cleanup() {
    if [ -n "${temp_file}" ] && [ -e "${temp_file}" ]; then
        rm -f "${temp_file}"
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

validate_regular_file() {
    local path="$1"
    local link_count
    local mode
    local mode_value

    [ ! -L "${path}" ] && [ -f "${path}" ]
    link_count="$(stat_value '%h' '%l' "${path}")"
    [ "${link_count}" = "1" ]
    mode="$(stat_value '%a' '%Lp' "${path}")"
    case "${mode}" in
        [0-7][0-7][0-7]|[0-7][0-7][0-7][0-7]) ;;
        *) return 1 ;;
    esac
    mode_value=$((8#${mode}))
    [ $((mode_value & 0022)) -eq 0 ]
    [ $((mode_value & 0111)) -eq 0 ]
    [ $((mode_value & 0400)) -ne 0 ]
}

allowed_owner() {
    local owner_uid="$1"

    if [ "${owner_uid}" = "$(id -u)" ] \
        || [ "${owner_uid}" = "${data_owner_uid}" ]; then
        return 0
    fi
    [ -n "${service_uid}" ] && [ "${owner_uid}" = "${service_uid}" ]
}

validate_key_file() {
    local path="$1"
    local byte_count
    local key
    local line_count
    local owner_uid

    validate_regular_file "${path}"
    owner_uid="$(stat_value '%u' '%u' "${path}")"
    allowed_owner "${owner_uid}"
    byte_count="$(LC_ALL=C wc -c < "${path}")"
    line_count="$(LC_ALL=C wc -l < "${path}")"
    [ "${byte_count}" -eq 66 ]
    [ "${line_count}" -eq 1 ]
    IFS= read -r key < "${path}"
    [[ "${key}" =~ ^[A-Za-z0-9]{65}$ ]]
}

[ -d "${data_root}" ] && [ ! -L "${data_root}" ]
data_owner_uid="$(stat_value '%u' '%u' "${data_root}")"
case "${data_owner_uid}" in
    ''|*[!0-9]*) exit 1 ;;
esac
service_uid="$(id -u www-data 2>/dev/null || true)"
case "${service_uid}" in
    ''|*[!0-9]*) service_uid="" ;;
esac

if [ -e "${key_file}" ] || [ -L "${key_file}" ]; then
    validate_key_file "${key_file}"
    temp_file="$(mktemp "${data_root}/.production_secret_key.txt.tmp-XXXXXX")"
    cp "${key_file}" "${temp_file}"
    chmod 0600 "${temp_file}"
    mv -f "${temp_file}" "${key_file}"
    temp_file=""
    validate_key_file "${key_file}"
    exit 0
fi

generated_key="$(pwgen -c -n -1 65)"
[[ "${generated_key}" =~ ^[A-Za-z0-9]{65}$ ]]
temp_file="$(mktemp "${data_root}/.production_secret_key.txt.tmp-XXXXXX")"
printf '%s\n' "${generated_key}" > "${temp_file}"
chmod 0600 "${temp_file}"
validate_key_file "${temp_file}"
mv -f "${temp_file}" "${key_file}"
temp_file=""
validate_key_file "${key_file}"
