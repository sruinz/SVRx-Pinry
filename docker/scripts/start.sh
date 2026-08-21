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

bash "${PROJECT_ROOT}/docker/scripts/bootstrap.sh"

# If static files don't exist collect them
cd "${PROJECT_ROOT}"
python manage.py collectstatic --noinput

# 서비스 기동 전에 대기 중인 마이그레이션을 모두 적용한다.
python manage.py migrate --noinput

# Fix all settings after all commands are run
chown -R www-data:www-data /data

# start all process
/usr/sbin/nginx

cd "${PROJECT_ROOT}"
./docker/scripts/_start_gunicorn.sh
