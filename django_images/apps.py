from __future__ import unicode_literals

from django.apps import AppConfig
from django.utils.translation import gettext_lazy


class DjangoImagesConfig(AppConfig):
    name = 'django_images'
    verbose_name = gettext_lazy('이미지')
