# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("django_images", "0006_pending_media_deletion"),
    ]

    operations = [
        migrations.CreateModel(
            name="StartupValidationState",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "contract_version",
                    models.PositiveIntegerField(default=0),
                ),
            ],
        ),
    ]
