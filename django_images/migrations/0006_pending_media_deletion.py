# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("django_images", "0005_enforce_image_asset_metadata"),
    ]

    operations = [
        migrations.CreateModel(
            name="PendingMediaDeletion",
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
                    "kind",
                    models.CharField(
                        choices=[
                            ("original", "original"),
                            ("thumbnail", "thumbnail"),
                        ],
                        max_length=16,
                    ),
                ),
                ("name", models.CharField(max_length=255)),
                ("attempts", models.PositiveIntegerField(default=0)),
                (
                    "last_error",
                    models.CharField(
                        blank=True, default="", max_length=255
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "unique_together": {("kind", "name")},
            },
        ),
    ]
