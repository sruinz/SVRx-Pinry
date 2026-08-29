from django.db import migrations, models


def bootstrap_existing_install(apps, schema_editor):
    database_alias = schema_editor.connection.alias
    State = apps.get_model('users', 'AdminBootstrapState')
    User = apps.get_model('auth', 'User')
    state, _ = State.objects.using(database_alias).get_or_create(pk=1)

    if state.bootstrap_complete:
        return

    active_users = User.objects.using(database_alias).filter(is_active=True)
    if active_users.filter(is_superuser=True).exists():
        State.objects.using(database_alias).filter(pk=1).update(
            bootstrap_complete=True,
        )
        return

    first_user = active_users.order_by('date_joined', 'id').first()
    if first_user is None:
        return

    User.objects.using(database_alias).filter(pk=first_user.pk).update(
        is_staff=True,
        is_superuser=True,
    )
    State.objects.using(database_alias).filter(pk=1).update(
        bootstrap_complete=True,
    )


class Migration(migrations.Migration):
    dependencies = [
        ('users', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='AdminBootstrapState',
            fields=[
                (
                    'id',
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                ('bootstrap_complete', models.BooleanField(default=False)),
            ],
        ),
        migrations.RunPython(
            bootstrap_existing_install,
            migrations.RunPython.noop,
        ),
    ]
