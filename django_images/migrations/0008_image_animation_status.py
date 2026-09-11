from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("django_images", "0007_startup_validation_state"),
    ]

    operations = [
        migrations.AddField(
            model_name="image",
            name="animation_status",
            field=models.CharField(editable=False, max_length=16, null=True),
        ),
    ]
