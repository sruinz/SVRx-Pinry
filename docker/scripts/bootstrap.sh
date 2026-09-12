#!/bin/bash
set -euo pipefail

umask 077

data_root="${PINRY_DATA_ROOT:-/data}"
project_root="${PINRY_PROJECT_ROOT:-/pinry}"
data_settings="${data_root}/local_settings.py"
key_file="${data_root}/production_secret_key.txt"
gen_key_script="${project_root}/docker/scripts/gen_key.sh"
normalize_script="${project_root}/docker/scripts/normalize_persistent_file.py"
settings_directory="${project_root}/pinry/settings"
settings_template="${settings_directory}/local_settings.example.py"
project_settings="${settings_directory}/local_settings.py"
data_temp=""
project_temp=""
service_uid=""
service_gid=""
bootstrap_failure_code="bootstrap_environment_invalid"

report_failure() {
    local status=$?

    trap - ERR
    printf '%s\n' "${bootstrap_failure_code}" >&2
    exit "${status}"
}

abort_current_stage() {
    trap - ERR
    printf '%s\n' "${bootstrap_failure_code}" >&2
    exit 1
}

trap report_failure ERR

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

validate_regular_file() {
    local path="$1"
    local allow_executable="${2:-0}"
    local link_count
    local mode
    local mode_value

    [ ! -L "${path}" ] && [ -f "${path}" ] || return 1
    link_count="$(stat_value '%h' '%l' "${path}")" || return 1
    [ "${link_count}" = "1" ] || return 1
    mode="$(stat_value '%a' '%Lp' "${path}")" || return 1
    case "${mode}" in
        [0-7][0-7][0-7]|[0-7][0-7][0-7][0-7]) ;;
        *) return 1 ;;
    esac
    mode_value=$((8#${mode}))
    [ $((mode_value & 0022)) -eq 0 ] || return 1
    if [ "${allow_executable}" != "1" ]; then
        [ $((mode_value & 0111)) -eq 0 ] || return 1
    fi
    [ $((mode_value & 0400)) -ne 0 ] || return 1
    [ "$(LC_ALL=C wc -c < "${path}")" -gt 0 ] || return 1
    [ "$(LC_ALL=C wc -c < "${path}")" -le 1048576 ] || return 1
}

validate_persistent_file() {
    local path="$1"
    local owner_uid
    local mode

    validate_regular_file "${path}" || return 1
    owner_uid="$(stat_value '%u' '%u' "${path}")" || return 1
    [ "${owner_uid}" = "$(id -u)" ] \
        || return 1
    mode="$(stat_value '%a' '%Lp' "${path}")" || return 1
    [ $((8#${mode})) -eq $((8#600)) ] || return 1
}

validate_service_settings() {
    local path="$1"
    local mode

    validate_regular_file "${path}" || return 1
    [ "$(stat_value '%u' '%u' "${path}")" = "${service_uid}" ] \
        || return 1
    [ "$(stat_value '%g' '%g' "${path}")" = "${service_gid}" ] \
        || return 1
    mode="$(stat_value '%a' '%Lp' "${path}")" || return 1
    [ $((8#${mode})) -eq $((8#600)) ] || return 1
}

read_key() {
    local path="$1"
    local byte_count
    local key
    local line_count

    validate_persistent_file "${path}" || return 1
    byte_count="$(LC_ALL=C wc -c < "${path}")" || return 1
    line_count="$(LC_ALL=C wc -l < "${path}")" || return 1
    [ "${byte_count}" -eq 66 ] || return 1
    [ "${line_count}" -eq 1 ] || return 1
    IFS= read -r key < "${path}" || return 1
    [[ "${key}" =~ ^[A-Za-z0-9]{65}$ ]] || return 1
    printf '%s' "${key}"
}

if [ ! -d "${data_root}" ] || [ -L "${data_root}" ]; then
    abort_current_stage
fi
if [ ! -d "${settings_directory}" ] || [ -L "${settings_directory}" ]; then
    abort_current_stage
fi
service_uid="$(id -u www-data)"
service_gid="$(id -g www-data)"
case "${service_uid}" in
    ''|*[!0-9]*) abort_current_stage ;;
esac
case "${service_gid}" in
    ''|*[!0-9]*) abort_current_stage ;;
esac
[ -f "${normalize_script}" ] && [ ! -L "${normalize_script}" ] \
    || abort_current_stage

bootstrap_failure_code="bootstrap_persistent_settings_invalid"
if [ -e "${data_settings}" ] || [ -L "${data_settings}" ]; then
    if ! settings_error="$(python3 "${normalize_script}" \
        "${data_root}" "${data_settings}" settings 2>&1 >/dev/null)"; then
        case "${settings_error}" in
            local_settings_encoding_invalid|local_settings_syntax_invalid)
                bootstrap_failure_code="${settings_error}" ;;
        esac
        abort_current_stage
    fi
    if [ -e "${key_file}" ] || [ -L "${key_file}" ]; then
        python3 "${normalize_script}" \
            "${data_root}" "${key_file}" key \
            >/dev/null 2>/dev/null || abort_current_stage
    fi
else
    /bin/bash "${gen_key_script}" >/dev/null 2>/dev/null
    secret_key="$(read_key "${key_file}")"
    validate_regular_file "${settings_template}" 1 || abort_current_stage
    placeholder_count="$(
        grep -o 'secret_key_place_holder' "${settings_template}" | wc -l
    )"
    [ "${placeholder_count}" -eq 1 ]
    data_temp="$(mktemp "${data_root}/.local_settings.py.tmp-XXXXXX")"
    sed "s/secret_key_place_holder/${secret_key}/" \
        "${settings_template}" > "${data_temp}"
    if grep -q 'secret_key_place_holder' "${data_temp}"; then
        abort_current_stage
    fi
    grep -Fq "${secret_key}" "${data_temp}"
    chmod 0600 "${data_temp}"
    mv -f "${data_temp}" "${data_settings}"
    data_temp=""
fi

validate_persistent_file "${data_settings}" || abort_current_stage
if grep -q 'secret_key_place_holder' "${data_settings}"; then
    abort_current_stage
fi

bootstrap_failure_code="bootstrap_project_settings_invalid"
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
trap - ERR
