import uuid

from django.contrib import admin
from django.contrib.admin.models import LogEntry
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.test import RequestFactory, TestCase
from django.urls import reverse

from .models import (
    AuthPolicy,
    ExternalIdentity,
    SSOProvider,
    User,
    make_identity_digest,
)
from .sso.config import read_policy, save_configuration


class SSOConfigurationTest(TestCase):
    def setUp(self):
        self.superuser = User.objects.create_superuser(
            "admin",
            "admin@example.com",
            "test-password",
        )

    def test_upgrade_defaults_keep_existing_login_available(self):
        policy = AuthPolicy.objects.get(pk=1)

        self.assertTrue(policy.password_login_enabled)
        self.assertTrue(policy.api_tokens_enabled)
        self.assertEqual(policy.recovery_allowed_cidrs, [])
        self.assertEqual(policy.recovery_denied_cidrs, [])
        self.assertEqual(policy.revision, 1)
        self.assertEqual(read_policy(), policy)

    def test_regular_staff_and_inactive_superuser_cannot_save_configuration(self):
        actors = [
            User.objects.create_user("regular", password="test-password"),
            User.objects.create_user(
                "staff",
                password="test-password",
                is_staff=True,
            ),
            User.objects.create_superuser(
                "inactive-admin",
                "inactive@example.com",
                "test-password",
                is_active=False,
            ),
        ]

        for actor in actors:
            with self.subTest(actor=actor.username):
                with self.assertRaises(PermissionDenied):
                    save_configuration(actor, {"recovery_allowed_cidrs": []})

    def test_saves_and_normalizes_private_recovery_networks(self):
        policy = save_configuration(
            self.superuser,
            {
                "recovery_allowed_cidrs": [
                    "10.0.0.0/08",
                    "fd12:3456::/048",
                ],
                "recovery_denied_cidrs": [
                    "192.168.1.10",
                    "203.0.113.0/24",
                ],
            },
        )

        self.assertEqual(
            policy.recovery_allowed_cidrs,
            ["10.0.0.0/8", "fd12:3456::/48"],
        )
        self.assertEqual(
            policy.recovery_denied_cidrs,
            ["192.168.1.10/32", "203.0.113.0/24"],
        )
        self.assertEqual(policy.revision, 2)

    def test_rejects_invalid_recovery_allowed_networks(self):
        invalid_values = [
            "",
            "not-a-network",
            "192.168.1.2/24",
            "0.0.0.0/0",
            "8.8.8.0/24",
            "::/0",
            "2001:4860::/32",
        ]

        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    save_configuration(
                        self.superuser,
                        {"recovery_allowed_cidrs": [value]},
                    )

        policy = read_policy()
        self.assertEqual(policy.recovery_allowed_cidrs, [])
        self.assertEqual(policy.revision, 1)

    def test_rejects_invalid_recovery_denied_networks(self):
        for value in ("", "not-a-network", "0.0.0.0/0", "::/0"):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    save_configuration(
                        self.superuser,
                        {"recovery_denied_cidrs": [value]},
                    )

    def test_registers_multiple_providers_of_the_same_kind(self):
        for name in ("Google A", "Google B"):
            save_configuration(
                self.superuser,
                {},
                {
                    "kind": SSOProvider.Kind.GOOGLE,
                    "name": name,
                    "client_id": "client-{}".format(name[-1].lower()),
                },
            )

        providers = list(SSOProvider.objects.order_by("name"))
        self.assertEqual(
            [provider.name for provider in providers],
            ["Google A", "Google B"],
        )
        self.assertEqual(
            [provider.kind for provider in providers],
            [SSOProvider.Kind.GOOGLE, SSOProvider.Kind.GOOGLE],
        )
        self.assertTrue(all(not provider.enabled for provider in providers))
        self.assertTrue(all(provider.revision == 1 for provider in providers))

    def test_updates_provider_but_refuses_kind_changes(self):
        save_configuration(
            self.superuser,
            {},
            {"kind": SSOProvider.Kind.GOOGLE, "name": "Google"},
        )
        provider = SSOProvider.objects.get()

        save_configuration(
            self.superuser,
            {},
            {"id": provider.pk, "name": "Google Workspace"},
        )
        provider.refresh_from_db()
        self.assertEqual(provider.name, "Google Workspace")
        self.assertEqual(provider.revision, 2)

        with self.assertRaises(ValidationError):
            save_configuration(
                self.superuser,
                {},
                {"id": provider.pk, "kind": SSOProvider.Kind.OIDC},
            )

        provider.refresh_from_db()
        self.assertEqual(provider.kind, SSOProvider.Kind.GOOGLE)
        self.assertEqual(provider.revision, 2)

    def test_rejects_public_internal_provider_network(self):
        with self.assertRaises(ValidationError):
            save_configuration(
                self.superuser,
                {},
                {
                    "kind": SSOProvider.Kind.OIDC,
                    "name": "Internal",
                    "internal_cidrs": ["8.8.8.0/24"],
                },
            )

    def test_refuses_disabling_password_until_recovery_is_verified(self):
        for field_name in ("password_login_enabled",):
            with self.subTest(field_name=field_name):
                with self.assertRaises(ValidationError):
                    save_configuration(self.superuser, {field_name: False})

        policy = read_policy()
        self.assertTrue(policy.password_login_enabled)
        self.assertTrue(policy.api_tokens_enabled)
        self.assertEqual(policy.revision, 1)

    def test_token_policy_can_be_disabled(self):
        policy = save_configuration(self.superuser, {'api_tokens_enabled': False})
        self.assertFalse(policy.api_tokens_enabled)

    def test_stale_revision_is_rejected(self):
        save_configuration(self.superuser, {'api_tokens_enabled': False})
        with self.assertRaises(ValidationError):
            save_configuration(self.superuser, {'api_tokens_enabled': True}, expected_revision=1)

    def test_provider_preset_and_write_only_secret_enable_real_configuration(self):
        import tempfile
        from pathlib import Path
        from django.test import override_settings
        from users.sso.secrets import decrypt_secret
        with tempfile.TemporaryDirectory() as directory, override_settings(
            SSO_SECRET_KEY_FILE=str(Path(directory) / 'key'),
        ):
            result = save_configuration(self.superuser, {}, {
                'kind': 'google', 'name': 'Google', 'enabled': True,
                'public_base_url': 'https://pinry.example', 'client_id': 'client',
                'client_secret': 'private-value',
            })
            provider = result._saved_provider
            self.assertTrue(provider.enabled)
            self.assertEqual(provider.allowed_endpoint_origins, [
                'https://accounts.google.com', 'https://oauth2.googleapis.com', 'https://www.googleapis.com',
            ])
            self.assertEqual(decrypt_secret(provider.encrypted_client_secret), 'private-value')
            saved = provider.encrypted_client_secret
            save_configuration(self.superuser, {}, {'id': provider.pk, 'client_secret': ''})
            provider.refresh_from_db()
            self.assertEqual(provider.encrypted_client_secret, saved)

    def test_provider_activation_failure_rolls_back_policy_changes(self):
        with self.assertRaises(ValidationError):
            save_configuration(
                self.superuser,
                {"recovery_allowed_cidrs": ["192.168.20.0/24"]},
                {
                    "kind": SSOProvider.Kind.GOOGLE,
                    "name": "Google",
                    "enabled": True,
                },
            )

        policy = read_policy()
        self.assertEqual(policy.recovery_allowed_cidrs, [])
        self.assertEqual(policy.revision, 1)
        self.assertFalse(SSOProvider.objects.exists())


class ExternalIdentityTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("identity-user")
        self.provider = SSOProvider.objects.create(
            id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
            kind=SSOProvider.Kind.OIDC,
            name="Identity Provider",
        )

    def test_identity_digest_uses_unambiguous_case_sensitive_values(self):
        digest = make_identity_digest(
            self.provider.pk,
            "https://idp.example/issuer",
            "CaseSensitive",
        )

        self.assertEqual(
            digest,
            "000a2a552d3c430e2de2920ce174e240"
            "18364d04b21870fdf2a2c02b4e7084d0",
        )
        self.assertNotEqual(
            digest,
            make_identity_digest(
                self.provider.pk,
                "https://idp.example/issuer",
                "casesensitive",
            ),
        )

    def test_creation_generates_digest_and_preserves_long_original_values(self):
        issuer = "https://idp.example/" + "i" * 2028

        identity = ExternalIdentity.objects.create(
            user=self.user,
            provider=self.provider,
            issuer=issuer,
            subject="CaseSensitive",
            identity_digest="not-user-controlled",
        )

        self.assertEqual(identity.issuer, issuer)
        self.assertEqual(identity.subject, "CaseSensitive")
        self.assertEqual(len(identity.identity_digest), 64)
        self.assertEqual(
            identity.identity_digest,
            make_identity_digest(self.provider.pk, issuer, "CaseSensitive"),
        )

    def test_case_variants_are_distinct_but_exact_duplicate_is_rejected(self):
        for subject in ("Subject", "subject"):
            ExternalIdentity.objects.create(
                user=self.user,
                provider=self.provider,
                issuer="https://idp.example/issuer",
                subject=subject,
            )

        self.assertEqual(ExternalIdentity.objects.count(), 2)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ExternalIdentity.objects.create(
                    user=self.user,
                    provider=self.provider,
                    issuer="https://idp.example/issuer",
                    subject="Subject",
                )


class SSOConfigurationAdminTest(TestCase):
    def setUp(self):
        self.superuser = User.objects.create_superuser(
            "admin",
            "admin@example.com",
            "test-password",
        )
        self.staff = User.objects.create_user(
            "staff",
            password="test-password",
            is_staff=True,
        )

    def policy_change_url(self):
        return reverse("admin:users_authpolicy_change", args=[1])

    def provider_form_data(self, **overrides):
        data = {
            "kind": SSOProvider.Kind.GOOGLE,
            "name": "Google",
            "position": "0",
            "public_base_url": "",
            "issuer": "",
            "discovery_url": "",
            "tenant_id": "",
            "client_id": "client-id",
            "allowed_endpoint_origins": "[]",
            "internal_cidrs": "[]",
            "_save": "저장",
            "expected_revision": "1",
            "expected_policy_revision": "1",
        }
        data.update(overrides)
        return data

    def test_staff_cannot_change_authentication_policy(self):
        self.client.force_login(self.staff)

        response = self.client.post(self.policy_change_url(), {})

        self.assertEqual(response.status_code, 403)

    def test_admin_policy_form_reports_missing_sso_proof(self):
        self.client.force_login(self.superuser)

        response = self.client.post(
            self.policy_change_url(),
            {
                "api_tokens_enabled": "on",
                "expected_revision": "1",
                "recovery_allowed_cidrs": "[]",
                "recovery_denied_cidrs": "[]",
                "_save": "저장",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertContains(self.client.get(response['Location']), "마지막 활성 SSO 제공자")
        self.assertTrue(read_policy().password_login_enabled)

    def test_admin_provider_form_reports_invalid_public_url(self):
        self.client.force_login(self.superuser)

        response = self.client.post(
            reverse("admin:users_ssoprovider_add"),
            self.provider_form_data(enabled="on"),
        )

        self.assertEqual(response.status_code, 302)
        self.assertContains(self.client.get(response['Location']), "HTTPS 기준 URL")
        self.assertFalse(SSOProvider.objects.exists())

    def test_admin_save_uses_configuration_revision(self):
        self.client.force_login(self.superuser)

        response = self.client.post(
            self.policy_change_url(),
            {
                "password_login_enabled": "on",
                "expected_revision": "1",
                "api_tokens_enabled": "on",
                "recovery_allowed_cidrs": '["192.168.50.0/24"]',
                "recovery_denied_cidrs": "[]",
                "_save": "저장",
            },
        )

        self.assertEqual(response.status_code, 302)
        policy = read_policy()
        self.assertEqual(policy.recovery_allowed_cidrs, ["192.168.50.0/24"])
        self.assertEqual(policy.revision, 2)

    def test_admin_can_register_disabled_provider(self):
        self.client.force_login(self.superuser)

        response = self.client.post(
            reverse("admin:users_ssoprovider_add"),
            self.provider_form_data(),
        )

        self.assertEqual(response.status_code, 302)
        provider = SSOProvider.objects.get()
        self.assertEqual(provider.name, "Google")
        self.assertFalse(provider.enabled)

    def test_admin_neither_displays_nor_accepts_encrypted_secret(self):
        request = RequestFactory().get("/admin/users/ssoprovider/add/")
        request.user = self.superuser
        provider_admin = admin.site._registry[SSOProvider]

        form_class = provider_admin.get_form(request)

        self.assertNotIn("encrypted_client_secret", form_class.base_fields)

    def test_provider_kind_is_editable_only_during_creation(self):
        provider = SSOProvider.objects.create(
            kind=SSOProvider.Kind.GOOGLE,
            name="Google",
            client_id="client-id",
        )
        request = RequestFactory().get("/admin/users/ssoprovider/")
        request.user = self.superuser
        provider_admin = admin.site._registry[SSOProvider]

        add_form = provider_admin.get_form(request)
        change_form = provider_admin.get_form(request, obj=provider)

        self.assertIn("kind", add_form.base_fields)
        self.assertNotIn("kind", change_form.base_fields)

        self.client.force_login(self.superuser)
        response = self.client.post(
            reverse("admin:users_ssoprovider_change", args=[provider.pk]),
            self.provider_form_data(kind=SSOProvider.Kind.OIDC),
        )

        self.assertEqual(response.status_code, 302)
        provider.refresh_from_db()
        self.assertEqual(provider.kind, SSOProvider.Kind.GOOGLE)
        self.assertEqual(provider.revision, 1)
        self.assertNotIn(
            "Kind",
            LogEntry.objects.get(object_id=str(provider.pk)).change_message,
        )
