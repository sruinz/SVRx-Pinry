from __future__ import unicode_literals

from django.apps import AppConfig
from django.utils.translation import gettext_lazy


class UsersConfig(AppConfig):
    name = 'users'
    verbose_name = gettext_lazy('사용자와 인증')
