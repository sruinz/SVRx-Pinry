import uuid
from unittest.mock import patch

from django.test import TestCase
from django.contrib import admin

from users.admin import AuthPolicyAdminForm, SSOProviderAdminForm
from users.models import SSOProvider, User


class SSOAdminGuideTest(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser('guide-admin', 'guide@example.com', 'test-password')

    def provider_data(self, **changes):
        data = dict(kind='google', name='Google', position=0, client_id='test-client',
                    public_base_url='https://pins.example.com', expected_revision=1,
                    expected_policy_revision=1)
        data.update(changes)
        return data

    def test_authentik_discovery_supplies_issuer_and_origin(self):
        with patch('users.sso.transport.request_json', return_value={
            'issuer': 'https://auth.example.com/application/o/pins/',
            'authorization_endpoint': 'https://auth.example.com/application/o/authorize/',
            'token_endpoint': 'https://auth.example.com/application/o/token/',
            'jwks_uri': 'https://auth.example.com/application/o/pins/jwks/',
        }):
            form = SSOProviderAdminForm(data=self.provider_data(
                kind='authentik', enabled=True, client_secret='example-secret',
                discovery_url='https://auth.example.com/application/o/pins/.well-known/openid-configuration'))
            self.assertTrue(form.is_valid(), form.errors)
            self.assertEqual(form.cleaned_data['issuer'], 'https://auth.example.com/application/o/pins/')
            self.assertEqual(form.cleaned_data['allowed_endpoint_origins'], ['https://auth.example.com'])

    def test_changed_discovery_updates_automatic_origin_but_rejects_custom_conflict(self):
        provider = SSOProvider.objects.create(
            kind='authentik', name='test', issuer='https://old.example.com/',
            discovery_url='https://old.example.com/.well-known/openid-configuration',
            allowed_endpoint_origins=['https://old.example.com'])
        metadata = {'issuer': 'https://new.example.com/',
                    'authorization_endpoint': 'https://new.example.com/authorize',
                    'token_endpoint': 'https://new.example.com/token', 'jwks_uri': 'https://new.example.com/jwks'}
        data = self.provider_data(kind='authentik', issuer=provider.issuer,
                                  discovery_url='https://new.example.com/.well-known/openid-configuration',
                                  allowed_endpoint_origins='https://old.example.com')
        with patch('users.sso.transport.request_json', return_value=metadata):
            form = SSOProviderAdminForm(data=data, instance=provider)
            self.assertTrue(form.is_valid(), form.errors)
            self.assertEqual(form.cleaned_data['allowed_endpoint_origins'], ['https://new.example.com'])
            provider.refresh_from_db()
            data['allowed_endpoint_origins'] = 'https://custom.example.com'
            form = SSOProviderAdminForm(data=data, instance=provider)
            self.assertFalse(form.is_valid())
            self.assertIn('discovery_url', form.errors)

    def test_policy_accepts_lines_and_renders_existing_lists_as_lines(self):
        form = AuthPolicyAdminForm(data=dict(
            password_login_enabled=True, api_tokens_enabled=True,
            expected_revision=1, recovery_allowed_cidrs='192.168.0.0/24\n10.0.0.0/8'))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['recovery_allowed_cidrs'], ['192.168.0.0/24', '10.0.0.0/8'])
        instance = form.save(commit=False)
        self.assertEqual(AuthPolicyAdminForm(instance=instance)['recovery_allowed_cidrs'].value(),
                         '192.168.0.0/24\n10.0.0.0/8')

    def test_policy_rejects_public_network_on_its_field(self):
        form = AuthPolicyAdminForm(data=dict(expected_revision=1, recovery_allowed_cidrs='8.8.8.0/24'))
        self.assertFalse(form.is_valid())
        self.assertIn('recovery_allowed_cidrs', form.errors)

    def test_origin_lines_are_lists_and_paths_are_field_errors(self):
        form = SSOProviderAdminForm(data=self.provider_data(
            allowed_endpoint_origins='https://idp.example.com\nhttps://api.example.com'))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['allowed_endpoint_origins'],
                         ['https://idp.example.com', 'https://api.example.com'])
        invalid = SSOProviderAdminForm(data=self.provider_data(
            allowed_endpoint_origins='https://idp.example.com/path'))
        self.assertFalse(invalid.is_valid())
        self.assertIn('allowed_endpoint_origins', invalid.errors)

    def test_enabled_provider_errors_keep_input_and_identify_field(self):
        form = SSOProviderAdminForm(data=self.provider_data(
            enabled=True, public_base_url='', kind='microsoft', tenant_id='common'))
        self.assertFalse(form.is_valid())
        self.assertIn('public_base_url', form.errors)
        self.assertIn('tenant_id', form.errors)
        self.assertEqual(form['client_id'].value(), 'test-client')

    def test_irrelevant_tenant_is_not_required_for_google(self):
        form = SSOProviderAdminForm(data=self.provider_data(tenant_id='not-a-uuid'))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['tenant_id'], '')

    def test_guide_restricted_to_active_superuser(self):
        url = '/admin/users/ssoprovider/setup-guide/'
        self.assertEqual(self.client.get(url).status_code, 302)
        staff = User.objects.create_user('guide-staff', is_staff=True)
        self.client.force_login(staff)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_saved_guide_separates_current_origin_from_actual_callback(self):
        provider = SSOProvider.objects.create(
            id=uuid.UUID('00000000-0000-4000-8000-000000000001'),
            kind='authentik', name='Test', public_base_url='https://pins.example.com',
            issuer='https://auth.example.com/application/o/pins/', encrypted_client_secret='DO-NOT-EXPOSE')
        self.client.force_login(self.admin)
        response = self.client.get('/admin/users/ssoprovider/setup-guide/', {'provider': str(provider.pk)},
                                   secure=True, HTTP_HOST='testserver')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'https://testserver')
        self.assertContains(response, 'https://pins.example.com')
        self.assertContains(response, '00000000-0000-4000-8000-000000000001')
        self.assertContains(response, 'https://auth.example.com/application/o/pins/.well-known/openid-configuration')
        self.assertNotContains(response, 'DO-NOT-EXPOSE')

    def test_new_guide_has_no_fabricated_provider_identifier(self):
        self.client.force_login(self.admin)
        response = self.client.get('/admin/users/ssoprovider/setup-guide/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '저장 후')
        self.assertNotContains(response, 'client_secret')

    def test_unsaved_provider_never_displays_temporary_callback(self):
        provider = SSOProvider(kind='google', name='Google', public_base_url='https://pins.example.com')
        label = admin.site._registry[SSOProvider].callback_address(provider)
        self.assertNotIn(str(provider.pk), label)
        self.assertIn('저장', label)

    def test_failed_edit_callback_uses_persisted_public_url(self):
        provider = SSOProvider.objects.create(kind='google', name='Google', public_base_url='https://saved.example.com')
        provider.public_base_url = 'https://unsaved.example.com'
        label = admin.site._registry[SSOProvider].callback_address(provider)
        self.assertIn('https://saved.example.com/', label)
        self.assertNotIn('unsaved.example.com', label)

    def test_runtime_invalid_urls_are_reported_on_input_field(self):
        for name in ('public_base_url', 'issuer', 'discovery_url', 'allowed_endpoint_origins'):
            for value in ('https://idp.example.com:bad', 'https://idp.example.com:99999',
                          'https://idp.exam\tple.com', 'https://idp.example.com\\other'):
                with self.subTest(name=name, value=value):
                    data = self.provider_data(kind='oidc')
                    data[name] = value
                    form = SSOProviderAdminForm(data=data)
                    self.assertFalse(form.is_valid())
                    self.assertIn(name, form.errors)

    def test_origin_example_matches_selected_provider(self):
        form = SSOProviderAdminForm(data=self.provider_data())
        self.assertIn('accounts.google.com', form.fields['allowed_endpoint_origins'].widget.attrs['placeholder'])

    def test_equivalent_current_and_saved_origin_has_no_mismatch_warning(self):
        provider = SSOProvider.objects.create(kind='google', name='Test', public_base_url='https://testserver:443/')
        self.client.force_login(self.admin)
        response = self.client.get('/admin/users/ssoprovider/setup-guide/', {'provider': str(provider.pk)}, secure=True)
        self.assertNotContains(response, '현재 접속 주소와 공개 서비스 주소가 다릅니다')
