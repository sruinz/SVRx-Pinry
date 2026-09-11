#!/bin/bash
set -eu
# TCP로 노출하지 않는다. 부모 supervisor가 root:www-data 0750 디렉터리를 준비한다.
exec gunicorn pinry.recovery_wsgi:application \
    --bind unix:/run/pinry-auth-recovery/application.sock --umask 007 \
    --workers 2 --timeout 60 --capture-output \
    --user www-data --group www-data \
    --env DJANGO_SETTINGS_MODULE=pinry.settings.recovery
