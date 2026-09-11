import importlib
from datetime import timedelta

from django.contrib.auth.models import User
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework.authtoken.models import Token


class AdminBootstrapMigrationTest(TransactionTestCase):
    migrate_from = [('users', '0001_initial')]
    migrate_to = [('users', '0002_admin_bootstrap')]

    def setUp(self):
        super(AdminBootstrapMigrationTest, self).setUp()
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.migrate_from)
        self.old_apps = self.executor.loader.project_state(
            self.migrate_from,
        ).apps

    def tearDown(self):
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.executor.loader.graph.leaf_nodes())
        super(AdminBootstrapMigrationTest, self).tearDown()

    def migrate_forward(self):
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.migrate_to)
        return self.executor.loader.project_state(self.migrate_to).apps

    def test_promotes_only_earliest_active_user_without_active_superuser(self):
        User = self.old_apps.get_model('auth', 'User')
        joined = timezone.now()
        inactive = User.objects.create(
            pk=5,
            username='inactive',
            is_active=False,
            date_joined=joined - timedelta(days=1),
        )
        earliest = User.objects.create(
            pk=10,
            username='earliest',
            is_active=True,
            date_joined=joined,
        )
        same_time = User.objects.create(
            pk=20,
            username='same-time',
            is_active=True,
            date_joined=joined,
        )
        later = User.objects.create(
            pk=30,
            username='later',
            is_active=True,
            date_joined=joined + timedelta(days=1),
        )

        apps = self.migrate_forward()
        MigratedUser = apps.get_model('auth', 'User')
        State = apps.get_model('users', 'AdminBootstrapState')

        promoted = MigratedUser.objects.get(pk=earliest.pk)
        self.assertTrue(promoted.is_staff)
        self.assertTrue(promoted.is_superuser)
        for user_id in (inactive.pk, same_time.pk, later.pk):
            user = MigratedUser.objects.get(pk=user_id)
            self.assertFalse(user.is_staff)
            self.assertFalse(user.is_superuser)
        self.assertTrue(State.objects.get(pk=1).bootstrap_complete)

    def test_preserves_permissions_when_active_superuser_exists(self):
        User = self.old_apps.get_model('auth', 'User')
        admin = User.objects.create(
            username='admin',
            is_active=True,
            is_staff=True,
            is_superuser=True,
        )
        regular = User.objects.create(
            username='regular',
            is_active=True,
            is_staff=False,
            is_superuser=False,
        )

        apps = self.migrate_forward()
        MigratedUser = apps.get_model('auth', 'User')
        State = apps.get_model('users', 'AdminBootstrapState')

        migrated_admin = MigratedUser.objects.get(pk=admin.pk)
        migrated_regular = MigratedUser.objects.get(pk=regular.pk)
        self.assertTrue(migrated_admin.is_staff)
        self.assertTrue(migrated_admin.is_superuser)
        self.assertFalse(migrated_regular.is_staff)
        self.assertFalse(migrated_regular.is_superuser)
        self.assertTrue(State.objects.get(pk=1).bootstrap_complete)

    def test_leaves_bootstrap_incomplete_without_active_users(self):
        User = self.old_apps.get_model('auth', 'User')
        inactive = User.objects.create(
            username='inactive',
            is_active=False,
            is_staff=False,
            is_superuser=True,
        )

        apps = self.migrate_forward()
        MigratedUser = apps.get_model('auth', 'User')
        State = apps.get_model('users', 'AdminBootstrapState')

        migrated_inactive = MigratedUser.objects.get(pk=inactive.pk)
        self.assertFalse(migrated_inactive.is_staff)
        self.assertTrue(migrated_inactive.is_superuser)
        self.assertFalse(State.objects.get(pk=1).bootstrap_complete)

    def test_completed_bootstrap_does_not_promote_again_after_demotion(self):
        User = self.old_apps.get_model('auth', 'User')
        first = User.objects.create(
            username='first',
            is_active=True,
        )

        apps = self.migrate_forward()
        MigratedUser = apps.get_model('auth', 'User')
        State = apps.get_model('users', 'AdminBootstrapState')
        MigratedUser.objects.filter(pk=first.pk).update(
            is_staff=False,
            is_superuser=False,
        )

        migration = importlib.import_module('users.migrations.0002_admin_bootstrap')
        with connection.schema_editor() as schema_editor:
            migration.bootstrap_existing_install(apps, schema_editor)

        demoted = MigratedUser.objects.get(pk=first.pk)
        self.assertFalse(demoted.is_staff)
        self.assertFalse(demoted.is_superuser)
        self.assertTrue(State.objects.get(pk=1).bootstrap_complete)


class SSOModelsMigrationTest(TransactionTestCase):
    migrate_from = [('users', '0002_admin_bootstrap')]
    migrate_to = [('users', '0003_sso_models')]

    def setUp(self):
        super().setUp()
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.migrate_from)
        self.user = User.objects.create_user(
            username='existing',
            password='test-password',
        )
        self.token = Token.objects.create(user=self.user)

    def tearDown(self):
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.executor.loader.graph.leaf_nodes())
        super().tearDown()

    def migrate_forward(self):
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.migrate_to)
        return self.executor.loader.project_state(self.migrate_to).apps

    def test_creates_safe_defaults_without_changing_existing_user_or_token(self):
        user_id = self.user.pk
        token_key = self.token.key

        apps = self.migrate_forward()
        Policy = apps.get_model('users', 'AuthPolicy')
        Identity = apps.get_model('users', 'ExternalIdentity')

        policy = Policy.objects.get(pk=1)
        self.assertTrue(policy.password_login_enabled)
        self.assertTrue(policy.api_tokens_enabled)
        self.assertEqual(policy.recovery_allowed_cidrs, [])
        self.assertEqual(policy.recovery_denied_cidrs, [])
        self.assertEqual(policy.revision, 1)
        self.assertTrue(
            User.objects.filter(pk=user_id, username='existing').exists(),
        )
        self.assertTrue(
            Token.objects.filter(user_id=user_id, key=token_key).exists(),
        )
        digest_field = Identity._meta.get_field('identity_digest')
        self.assertEqual(digest_field.max_length, 64)
        self.assertTrue(digest_field.unique)
        self.assertFalse(digest_field.editable)
        self.assertEqual(Identity._meta.constraints, [])
