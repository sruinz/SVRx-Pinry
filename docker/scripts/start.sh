#!/bin/bash
# -----------------------------------------------------------------------------
# docker-pinry /start script
#
# 서비스를 시작하기 전에 정적 파일과 데이터베이스를 준비한다.
#
# Authors: Isaac Bythewood
# Updated: Aug 19th, 2014
# -----------------------------------------------------------------------------
set -euo pipefail

PROJECT_ROOT="/pinry"

if [ "$#" -gt 1 ] || {
    [ "$#" -eq 1 ] && [ "$1" != "--migrate-legacy" ];
}; then
    echo "startup_argument_invalid" >&2
    exit 2
fi

exec python "${PROJECT_ROOT}/docker/scripts/startup.py" "$@"
