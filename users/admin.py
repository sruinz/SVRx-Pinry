from django import forms
from django.contrib import admin

from .models import AuthPolicy, SSOProvider
from .sso.config import save_configuration


POLICY_FIELDS = (
    'password_login_enabled',
    'api_tokens_enabled',
    'recovery_allowed_cidrs',
    'recovery_denied_cidrs',
)
PROVIDER_FIELDS = (
    'kind',
    'name',
    'position',
    'enabled',
    'public_base_url',
    'issuer',
    'discovery_url',
    'tenant_id',
    'client_id',
    'allowed_endpoint_origins',
    'internal_cidrs',
    'allow_signup',
)


class AuthPolicyAdminForm(forms.ModelForm):
    class Meta:
        model = AuthPolicy
        fields = POLICY_FIELDS

    def clean_password_login_enabled(self):
        enabled = self.cleaned_data['password_login_enabled']
        if self.instance.password_login_enabled and not enabled:
            raise forms.ValidationError('비밀번호 로그인을 아직 끌 수 없습니다.')
        return enabled

    def clean_api_tokens_enabled(self):
        enabled = self.cleaned_data['api_tokens_enabled']
        if self.instance.api_tokens_enabled and not enabled:
            raise forms.ValidationError('API 토큰 인증을 아직 끌 수 없습니다.')
        return enabled


class SSOProviderAdminForm(forms.ModelForm):
    class Meta:
        model = SSOProvider
        fields = PROVIDER_FIELDS

    def clean_enabled(self):
        enabled = self.cleaned_data['enabled']
        if not self.instance.enabled and enabled:
            raise forms.ValidationError('SSO 제공자를 아직 활성화할 수 없습니다.')
        return enabled


class ActiveSuperuserAdminMixin:
    def _has_configuration_permission(self, request):
        return bool(
            request.user.is_active
            and request.user.is_superuser
        )

    def has_module_permission(self, request):
        return self._has_configuration_permission(request)

    def has_view_permission(self, request, obj=None):
        return self._has_configuration_permission(request)

    def has_change_permission(self, request, obj=None):
        return self._has_configuration_permission(request)


@admin.register(AuthPolicy)
class AuthPolicyAdmin(ActiveSuperuserAdminMixin, admin.ModelAdmin):
    form = AuthPolicyAdminForm
    fields = POLICY_FIELDS + ('revision',)
    readonly_fields = ('revision',)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        saved_policy = save_configuration(
            request.user,
            {field_name: getattr(obj, field_name) for field_name in POLICY_FIELDS},
        )
        for field_name in POLICY_FIELDS + ('revision',):
            setattr(obj, field_name, getattr(saved_policy, field_name))


@admin.register(SSOProvider)
class SSOProviderAdmin(ActiveSuperuserAdminMixin, admin.ModelAdmin):
    form = SSOProviderAdminForm
    fields = PROVIDER_FIELDS + ('revision',)
    readonly_fields = ('revision',)
    list_display = ('name', 'kind', 'position', 'enabled', 'revision')
    ordering = ('position', 'name')

    def has_add_permission(self, request):
        return self._has_configuration_permission(request)

    def has_delete_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        if obj is None:
            return self.readonly_fields
        return self.readonly_fields + ('kind',)

    def save_model(self, request, obj, form, change):
        field_names = PROVIDER_FIELDS
        if change:
            field_names = tuple(
                field_name
                for field_name in PROVIDER_FIELDS
                if field_name != 'kind'
            )
        provider_changes = {
            field_name: getattr(obj, field_name)
            for field_name in field_names
        }
        if change:
            provider_changes['id'] = obj.pk

        saved_policy = save_configuration(
            request.user,
            {},
            provider_changes,
        )
        saved_provider = saved_policy._saved_provider
        for field in saved_provider._meta.concrete_fields:
            setattr(obj, field.attname, getattr(saved_provider, field.attname))
        obj._state.adding = False
        obj._state.db = saved_provider._state.db
