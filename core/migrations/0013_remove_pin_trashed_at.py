from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("core", "0012_batch_import_item")]
    operations = [
        migrations.RemoveField(model_name="pin", name="trashed_at"),
    ]
