# Django 5.2.17에서 2026-09-11 10:31에 생성

import django.db.models.deletion
import django.utils.timezone
import uuid
from django.conf import settings
from django.db import migrations, models


def create_default_auth_policy(apps, schema_editor):
    AuthPolicy = apps.get_model('users', 'AuthPolicy')
    AuthPolicy.objects.using(schema_editor.connection.alias).create(pk=1)


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0002_admin_bootstrap'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='AuthenticationThrottle',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('key_digest', models.CharField(max_length=64, unique=True)),
                ('window_started_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('failure_count', models.PositiveIntegerField(default=0)),
                ('blocked_until', models.DateTimeField(blank=True, null=True)),
            ],
        ),
        migrations.CreateModel(
            name='AuthPolicy',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('password_login_enabled', models.BooleanField(default=True)),
                ('api_tokens_enabled', models.BooleanField(default=True)),
                ('recovery_allowed_cidrs', models.JSONField(blank=True, default=list)),
                ('recovery_denied_cidrs', models.JSONField(blank=True, default=list)),
                ('revision', models.PositiveIntegerField(default=1, editable=False)),
            ],
        ),
        migrations.RunPython(
            create_default_auth_policy,
            migrations.RunPython.noop,
        ),
        migrations.CreateModel(
            name='SSOProvider',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('kind', models.CharField(choices=[('authentik', 'Authentik'), ('synology', 'Synology SSO Server'), ('google', 'Google'), ('microsoft', 'Microsoft'), ('github', 'GitHub'), ('oidc', '범용 OIDC')], max_length=20)),
                ('name', models.CharField(max_length=150)),
                ('position', models.PositiveIntegerField(default=0)),
                ('enabled', models.BooleanField(default=False)),
                ('public_base_url', models.URLField(blank=True, max_length=2048)),
                ('issuer', models.URLField(blank=True, max_length=2048)),
                ('discovery_url', models.URLField(blank=True, max_length=2048)),
                ('tenant_id', models.CharField(blank=True, max_length=255)),
                ('client_id', models.CharField(blank=True, max_length=255)),
                ('encrypted_client_secret', models.TextField(blank=True, editable=False)),
                ('allowed_endpoint_origins', models.JSONField(blank=True, default=list)),
                ('internal_cidrs', models.JSONField(blank=True, default=list)),
                ('allow_signup', models.BooleanField(default=False)),
                ('revision', models.PositiveIntegerField(default=1, editable=False)),
            ],
            options={
                'ordering': ('position', 'id'),
            },
        ),
        migrations.CreateModel(
            name='SSOAttempt',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('state_digest', models.CharField(max_length=64, unique=True)),
                ('browser_digest', models.CharField(max_length=64)),
                ('provider_revision', models.PositiveIntegerField()),
                ('purpose', models.CharField(choices=[('login', '로그인'), ('link', '연결'), ('reauth', '재인증')], max_length=10)),
                ('expires_at', models.DateTimeField()),
                ('consumed_at', models.DateTimeField(blank=True, null=True)),
                ('protected_payload', models.TextField()),
                ('user', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='sso_attempts', to=settings.AUTH_USER_MODEL)),
                ('provider', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='attempts', to='users.ssoprovider')),
            ],
        ),
        migrations.CreateModel(
            name='AuthVerification',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('kind', models.CharField(choices=[('sso', 'SSO'), ('recovery', '복구')], max_length=10)),
                ('policy_revision', models.PositiveIntegerField()),
                ('provider_revision', models.PositiveIntegerField(blank=True, null=True)),
                ('deployment_fingerprint', models.CharField(max_length=255)),
                ('verified_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='auth_verifications', to=settings.AUTH_USER_MODEL)),
                ('provider', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='auth_verifications', to='users.ssoprovider')),
            ],
        ),
        migrations.CreateModel(
            name='ExternalIdentity',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('issuer', models.CharField(max_length=2048)),
                ('subject', models.CharField(max_length=255)),
                ('identity_digest', models.CharField(editable=False, max_length=64, unique=True)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='external_identities', to=settings.AUTH_USER_MODEL)),
                ('provider', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='external_identities', to='users.ssoprovider')),
            ],
        ),
    ]
