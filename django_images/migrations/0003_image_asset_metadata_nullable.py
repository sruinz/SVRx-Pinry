# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('django_images', '0002_auto_20180826_0814'),
    ]

    operations = [
        migrations.AddField(
            model_name='image',
            name='asset_uuid',
            field=models.UUIDField(editable=False, null=True),
        ),
        migrations.AddField(
            model_name='image',
            name='original_filename',
            field=models.CharField(editable=False, max_length=255, null=True),
        ),
    ]
