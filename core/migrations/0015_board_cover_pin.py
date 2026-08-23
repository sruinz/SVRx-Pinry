from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0014_media_asset"),
    ]

    operations = [
        migrations.AddField(
            model_name="board",
            name="cover_pin",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="covering_boards",
                to="core.Pin",
            ),
        ),
    ]
