# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('django_images', '0004_backfill_image_asset_metadata'),
    ]

    operations = [
        migrations.AlterField(
            model_name='image',
            name='asset_uuid',
            field=models.UUIDField(
                default=uuid.uuid4,
                editable=False,
                unique=True,
            ),
        ),
        migrations.AlterField(
            model_name='image',
            name='original_filename',
            field=models.CharField(editable=False, max_length=255),
        ),
    ]
