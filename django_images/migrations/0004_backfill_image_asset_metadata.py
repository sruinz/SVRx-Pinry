# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import os.path
import unicodedata
import uuid

from django.db import migrations, models


def _legacy_filename(image_name):
    basename = os.path.basename(image_name or '')
    normalized = unicodedata.normalize('NFC', basename)
    return ''.join(
        character
        for character in normalized
        if unicodedata.category(character) != 'Cc'
    )[:255]


def backfill_asset_metadata(apps, schema_editor):
    Image = apps.get_model('django_images', 'Image')
    missing = models.Q(asset_uuid__isnull=True)
    missing |= models.Q(original_filename__isnull=True)
    rows = Image.objects.filter(missing).values_list(
        'id', 'asset_uuid', 'original_filename', 'image'
    )
    for image_id, asset_uuid, original_filename, image_name in rows.iterator():
        updates = {}
        if asset_uuid is None:
            updates['asset_uuid'] = uuid.uuid4()
        if original_filename is None:
            updates['original_filename'] = _legacy_filename(image_name)
        Image.objects.filter(id=image_id).update(**updates)


class Migration(migrations.Migration):

    dependencies = [
        ('django_images', '0003_image_asset_metadata_nullable'),
    ]

    operations = [
        migrations.RunPython(
            backfill_asset_metadata,
            migrations.RunPython.noop,
        ),
    ]
