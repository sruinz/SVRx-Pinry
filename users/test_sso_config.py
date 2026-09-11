from django.contrib import admin
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import RequestFactory, TestCase
from django.urls import reverse

from .models import AuthPolicy, SSOProvider, User
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

    def test_refuses_disabling_login_methods_until_enforcement_exists(self):
        for field_name in ("password_login_enabled", "api_tokens_enabled"):
            with self.subTest(field_name=field_name):
                with self.assertRaises(ValidationError):
                    save_configuration(self.superuser, {field_name: False})

        policy = read_policy()
        self.assertTrue(policy.password_login_enabled)
        self.assertTrue(policy.api_tokens_enabled)
        self.assertEqual(policy.revision, 1)

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
        }
        data.update(overrides)
        return data

    def test_staff_cannot_change_authentication_policy(self):
        self.client.force_login(self.staff)

        response = self.client.post(self.policy_change_url(), {})

        self.assertEqual(response.status_code, 403)

    def test_admin_policy_form_reports_temporary_guard_as_form_error(self):
        self.client.force_login(self.superuser)

        response = self.client.post(
            self.policy_change_url(),
            {
                "api_tokens_enabled": "on",
                "recovery_allowed_cidrs": "[]",
                "recovery_denied_cidrs": "[]",
                "_save": "저장",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "비밀번호 로그인을 아직 끌 수 없습니다")
        self.assertTrue(read_policy().password_login_enabled)

    def test_admin_provider_form_reports_activation_guard_as_form_error(self):
        self.client.force_login(self.superuser)

        response = self.client.post(
            reverse("admin:users_ssoprovider_add"),
            self.provider_form_data(enabled="on"),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "SSO 제공자를 아직 활성화할 수 없습니다")
        self.assertFalse(SSOProvider.objects.exists())

    def test_admin_save_uses_configuration_revision(self):
        self.client.force_login(self.superuser)

        response = self.client.post(
            self.policy_change_url(),
            {
                "password_login_enabled": "on",
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
