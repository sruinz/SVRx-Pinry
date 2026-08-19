#!/usr/bin/env python
import os
import sys


def _configure_settings_module(argv):
    settings_module = None
    arguments = argv[1:]
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == '--':
            break
        if argument.startswith('--settings='):
            candidate = argument.split('=', 1)[1]
            if candidate:
                settings_module = candidate
        elif argument == '--settings' and index + 1 < len(arguments):
            candidate = arguments[index + 1]
            if candidate and not candidate.startswith('-'):
                settings_module = candidate
                index += 1
        index += 1

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
