from django import forms
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.http import HttpResponseRedirect

from .models import AuthPolicy, SSOProvider
from .sso.config import save_configuration
from .sso.flows import callback_url
from .sso.policy import request_policy


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
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)

    class Meta:
        model = AuthPolicy
        fields = POLICY_FIELDS

        labels = {'password_login_enabled': '비밀번호 로그인 허용', 'api_tokens_enabled': 'API 토큰 인증 허용',
                  'recovery_allowed_cidrs': '복구 허용 CIDR', 'recovery_denied_cidrs': '복구 차단 CIDR'}
        help_texts = {'password_login_enabled': '끄기 전에 현재 관리자 SSO 로그인과 복구 로그인 확인이 필요합니다.',
                      'api_tokens_enabled': '끄면 기존 토큰을 보존하면서 인증·노출·발급을 모두 차단합니다.'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['expected_revision'].initial = self.instance.revision


class SSOProviderAdminForm(forms.ModelForm):
    client_secret = forms.CharField(label='Client Secret', required=False, widget=forms.PasswordInput,
                                    help_text='입력·교체만 가능합니다. 비워두면 기존 비밀을 유지합니다.')
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)
    expected_policy_revision = forms.IntegerField(widget=forms.HiddenInput)

    class Meta:
        model = SSOProvider
        fields = PROVIDER_FIELDS
        labels = dict(zip(PROVIDER_FIELDS, (
            '제공자 종류', '표시 이름', '표시 순서', '활성화', '공개 서비스 기준 URL', '발급자',
            'Discovery URL', 'Microsoft 테넌트 UUID', 'Client ID', '허용 endpoint origin',
            '내부 IdP 허용 CIDR', '신규 SSO 사용자 가입 허용',
        )))
        help_texts = {'issuer': '발급자·Client ID·비밀 변경 후 기존 연결과 관리자 로그인 확인을 재검증하세요.',
                      'allowed_endpoint_origins': 'Google·Microsoft·GitHub 신규 등록은 빈 목록에 기본 origin을 적용합니다.',
                      'allow_signup': '기본은 기존 사용자 연결만 허용합니다. 신규 계정에는 관리자 권한을 부여하지 않습니다.'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['expected_revision'].initial = self.instance.revision
        self.fields['expected_policy_revision'].initial = request_policy().revision


class ActiveSuperuserAdminMixin:
    def changeform_view(self, request, object_id=None, form_url='', extra_context=None):
        try:
            return super().changeform_view(request, object_id, form_url, extra_context)
        except ValidationError as error:
            self.message_user(request, ' '.join(error.messages), level='error')
            return HttpResponseRedirect(request.path)

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
    fields = POLICY_FIELDS + ('expected_revision', 'revision',)
    readonly_fields = ('revision',)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        saved_policy = save_configuration(
            request.user,
            {field_name: getattr(obj, field_name) for field_name in POLICY_FIELDS},
            expected_revision=form.cleaned_data['expected_revision'],
        )
        for field_name in POLICY_FIELDS + ('revision',):
            setattr(obj, field_name, getattr(saved_policy, field_name))


@admin.register(SSOProvider)
class SSOProviderAdmin(ActiveSuperuserAdminMixin, admin.ModelAdmin):
    form = SSOProviderAdminForm
    fields = PROVIDER_FIELDS + ('client_secret', 'callback_address', 'expected_revision',
                                'expected_policy_revision', 'revision',)
    readonly_fields = ('revision', 'callback_address')
    list_display = ('name', 'kind', 'position', 'enabled', 'revision')
    ordering = ('position', 'name')

    @admin.display(description='등록할 정확한 콜백 주소')
    def callback_address(self, obj):
        try:
            return callback_url(obj)
        except (ValidationError, AttributeError):
            return '공개 서비스 HTTPS URL을 저장하면 복사할 콜백 주소가 표시됩니다.'

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
            provider_changes['expected_revision'] = form.cleaned_data['expected_revision']
        provider_changes['client_secret'] = form.cleaned_data['client_secret']

        saved_policy = save_configuration(
            request.user,
            {},
            provider_changes,
            expected_revision=form.cleaned_data['expected_policy_revision'],
        )
        saved_provider = saved_policy._saved_provider
        for field in saved_provider._meta.concrete_fields:
            setattr(obj, field.attname, getattr(saved_provider, field.attname))
        obj._state.adding = False
        obj._state.db = saved_provider._state.db
