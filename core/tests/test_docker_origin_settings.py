import os
import runpy
import sys
from types import ModuleType
from unittest import TestCase, mock


class DockerOriginSettingsTests(TestCase):
    def load_settings(self, value, local_origins=None):
        local = ModuleType('pinry.settings.local_settings')
        local.SECRET_KEY = 'synthetic-origin-test-key'
        if local_origins is not None:
            local.CSRF_TRUSTED_ORIGINS = local_origins
        with mock.patch.dict(os.environ, {'PINRY_CSRF_TRUSTED_ORIGINS': value}):
            with mock.patch.dict(sys.modules, {'pinry.settings.local_settings': local}):
                return runpy.run_module('pinry.settings.docker')

    def test_no_origins_are_trusted_by_default(self):
        self.assertEqual(self.load_settings('')['CSRF_TRUSTED_ORIGINS'], [])

    def test_explicit_origins_keep_scheme_and_port(self):
        self.assertEqual(
            self.load_settings(' https://pinry.test, ,https://pinry.test:8443 ')['CSRF_TRUSTED_ORIGINS'],
            ['https://pinry.test', 'https://pinry.test:8443'],
        )

    def test_persistent_settings_keep_precedence(self):
        self.assertEqual(
            self.load_settings('https://env.test', ['https://saved.test'])['CSRF_TRUSTED_ORIGINS'],
            ['https://saved.test'],
        )
