#!/usr/bin/env python
import os
import sys


def _configure_settings_module(argv):
    settings_module = None
    for index, argument in enumerate(argv):
        if argument.startswith('--settings='):
            settings_module = argument.split('=', 1)[1]
            break
        if argument == '--settings' and index + 1 < len(argv):
            candidate = argv[index + 1]
            if not candidate.startswith('-'):
                settings_module = candidate
            break

    if settings_module:
        os.environ['DJANGO_SETTINGS_MODULE'] = settings_module
    else:
        os.environ.setdefault(
            'DJANGO_SETTINGS_MODULE',
            'pinry.settings.development',
        )


if __name__ == "__main__":
    _configure_settings_module(sys.argv)
    from django.core.management import execute_from_command_line
    if 'test' in sys.argv:
        from django.conf import settings
        settings.IS_TEST = True

    execute_from_command_line(sys.argv)
