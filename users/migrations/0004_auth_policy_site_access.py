from django.conf import settings
from django.db import migrations, models


def preserve_legacy_site_access_settings(apps, schema_editor):
    AuthPolicy = apps.get_model('users', 'AuthPolicy')
    AuthPolicy.objects.using(schema_editor.connection.alias).filter(pk=1).update(
        allow_new_registrations=bool(getattr(settings, 'ALLOW_NEW_REGISTRATIONS', True)),
        public_pins_enabled=bool(getattr(settings, 'PUBLIC', True)),
    )


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0003_sso_models'),
    ]

    operations = [
        migrations.AddField(
            model_name='authpolicy',
            name='allow_new_registrations',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='authpolicy',
            name='public_pins_enabled',
            field=models.BooleanField(default=True),
        ),
        migrations.RunPython(
            preserve_legacy_site_access_settings,
            migrations.RunPython.noop,
        ),
    ]
