import json
import uuid

from django import forms
from django.contrib import admin
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.template.response import TemplateResponse
from django.urls import path, reverse

from .models import AuthPolicy, SSOProvider
from .sso.config import save_configuration
from .sso.flows import callback_url
from .sso.policy import request_policy
from .sso.admin_guide import PROVIDER_GUIDES, SELF_HOSTED, guide_context
from .sso.config import PRESET_ORIGINS
from .sso.transport import _parse_url


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


class LineListField(forms.CharField):
    """화면에서는 줄 단위, 저장 시에는 기존 JSON 배열을 사용한다."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault('required', False)
        kwargs.setdefault('widget', forms.Textarea(attrs={'rows': 3, 'cols': 65}))
        super().__init__(*args, **kwargs)

    def prepare_value(self, value):
        return '\n'.join(value) if isinstance(value, list) else value

    def to_python(self, value):
        if not value:
            return []
        if isinstance(value, str):
            if value.lstrip().startswith('['):
                try:
                    value = json.loads(value)
                except ValueError:
                    raise ValidationError('대괄호와 따옴표 없이 한 줄에 하나씩 입력하세요.') from None
            else:
                value = value.splitlines()
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValidationError('한 줄에 하나의 문자열을 입력하세요.')
        return [item.strip() for item in value if item.strip()]


class AuthPolicyAdminForm(forms.ModelForm):
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)
    recovery_allowed_cidrs = LineListField(
        label='복구 허용 CIDR',
        help_text='선택 · 한 줄에 하나씩 입력하세요. 예: 192.168.0.0/24 또는 10.0.0.0/8. 자신의 내부망만 허용하세요. 비우면 허용 대역이 없습니다.')
    recovery_denied_cidrs = LineListField(
        label='복구 차단 CIDR',
        help_text='선택 · 한 줄에 하나씩 입력하세요. 예: 192.168.0.45/32. 허용 대역에 포함되어도 차단이 우선합니다.')

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
        self.fields['recovery_allowed_cidrs'].widget.attrs['placeholder'] = '192.168.0.0/24\n10.0.0.0/8'
        self.fields['recovery_denied_cidrs'].widget.attrs['placeholder'] = '192.168.0.45/32'


class SSOProviderAdminForm(forms.ModelForm):
    client_secret = forms.CharField(label='Client Secret', required=False, widget=forms.PasswordInput,
                                    help_text='입력·교체만 가능합니다. 비워두면 기존 비밀을 유지합니다.')
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)
    expected_policy_revision = forms.IntegerField(widget=forms.HiddenInput)
    allowed_endpoint_origins = LineListField(
        label='허용 서버 주소 (origin)',
        help_text='한 줄에 하나씩 HTTPS 서버 주소만 입력하세요(경로 제외). 예: https://auth.example.com. 기본 제공자는 신규 등록 시 기본값을 사용합니다.')
    internal_cidrs = LineListField(
        label='내부 IdP 허용 CIDR',
        help_text='선택 · IdP가 내부망에 있을 때만 필요한 대역을 한 줄씩 입력하세요. 예: 192.168.0.45/32. 복구 로그인 허용 대역과는 별개입니다.')

    class Media:
        js = ('js/sso_admin.js',)

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
        kind = self.instance.kind if not self.instance._state.adding else (
            self.data.get('kind') if self.is_bound else self.initial.get('kind', 'authentik'))
        guide = PROVIDER_GUIDES.get(kind, PROVIDER_GUIDES['authentik'])
        self.fields['public_base_url'].help_text = '활성화 시 필수 · 사용자가 로그인할 Pinry의 HTTPS 주소입니다. 내부망 접속 주소와 다를 수 있습니다. 경로는 넣지 마세요.'
        self.fields['public_base_url'].widget.attrs['placeholder'] = 'https://pins.example.com'
        self.fields['name'].help_text = '필수 · 로그인 버튼에 표시할 이름입니다. 예: 회사 계정'
        self.fields['name'].widget.attrs['placeholder'] = '회사 계정'
        self.fields['position'].help_text = '필수 · 작은 숫자가 먼저 표시됩니다. 예: 0, 10, 20'
        self.fields['enabled'].help_text = '처음에는 끈 상태로 저장해 확정 콜백 URI를 발급받고, 제공자 등록을 마친 후 활성화하세요.'
        self.fields['issuer'].help_text = '활성화 시 필수 · IdP Discovery 문서의 issuer 값을 그대로 입력하세요. 마지막 슬래시도 일치해야 합니다.'
        self.fields['discovery_url'].help_text = '선택 · IdP에서 제공한 Well-known URL입니다. 비우면 발급자 주소 뒤에 /.well-known/openid-configuration을 붙입니다.'
        self.fields['tenant_id'].help_text = '활성화 시 필수 · Entra의 디렉터리(테넌트) ID입니다. UUID만 허용하며 common은 사용할 수 없습니다.'
        self.fields['tenant_id'].widget.attrs['placeholder'] = '11111111-2222-4333-8444-555555555555'
        self.fields['client_id'].help_text = '활성화 시 필수 · 제공자의 앱 등록 화면에서 발급받은 클라이언트 ID입니다.'
        self.fields['client_secret'].widget.attrs['placeholder'] = '제공자에서 발급한 비밀 값 (저장된 값은 표시하지 않음)'
        self.fields['allowed_endpoint_origins'].widget.attrs['placeholder'] = guide['origin_example']
        self.fields['internal_cidrs'].widget.attrs['placeholder'] = '192.168.0.45/32'
        for name, example in (('issuer', 'issuer_example'), ('discovery_url', 'discovery_example'),
                              ('client_id', 'client_example')):
            self.fields[name].widget.attrs['placeholder'] = guide.get(example, '')
        for name in ('issuer', 'discovery_url', 'tenant_id', 'internal_cidrs'):
            relevant = kind == 'microsoft' if name == 'tenant_id' else kind in SELF_HOSTED
            if not relevant:
                self.fields[name].disabled = True
        # 기존 제공자의 종류는 관리자 화면과 서버 모두에서 변경하지 않는다.
        self.fields['public_base_url'].widget.attrs['data-provider-kind'] = kind
        for field in self.fields.values():
            field.error_messages['required'] = '이 항목을 입력하세요.'
            if isinstance(field, forms.URLField):
                field.error_messages['invalid'] = 'https://로 시작하는 올바른 URL을 입력하세요.'

    def clean(self):
        data = super().clean()
        kind = self.instance.kind if not self.instance._state.adding else data.get('kind')
        enabled = data.get('enabled')
        self._validate_addresses(data, kind, enabled)
        if kind == 'microsoft' and (enabled or data.get('tenant_id')):
            try:
                data['tenant_id'] = str(uuid.UUID(data.get('tenant_id', '')))
            except (ValueError, AttributeError):
                self.add_error('tenant_id', '11111111-2222-4333-8444-555555555555 형식의 테넌트 UUID를 입력하세요.')
        if enabled:
            if not data.get('client_id'):
                self.add_error('client_id', '활성화하려면 Client ID가 필요합니다.')
            if not data.get('client_secret') and not self.instance.encrypted_client_secret:
                self.add_error('client_secret', '활성화하려면 Client Secret이 필요합니다.')
            if not data.get('allowed_endpoint_origins') and not (self.instance._state.adding and PRESET_ORIGINS.get(kind)):
                self.add_error('allowed_endpoint_origins', '활성화하려면 허용 서버 주소가 필요합니다.')
        return data

    def _validate_addresses(self, data, kind, enabled):
        for name in ('public_base_url', 'issuer', 'discovery_url'):
            if name != 'public_base_url' and kind not in SELF_HOSTED:
                continue
            value = data.get(name)
            required = enabled and name != 'discovery_url'
            if not value:
                if required and name not in self.errors:
                    self.add_error(name, '활성화하려면 HTTPS 기준 URL을 입력하세요.')
                continue
            try:
                parsed = _parse_url(value)
                valid = not parsed.query
                if name == 'public_base_url':
                    valid = valid and parsed.path in ('', '/')
            except ValidationError:
                valid = False
            if not valid:
                self.add_error(name, '사용자 정보·쿼리 없는 HTTPS URL을 입력하세요. 공개 서비스 주소에는 경로를 넣지 마세요.')

    def clean_allowed_endpoint_origins(self):
        origins = self.cleaned_data['allowed_endpoint_origins']
        for value in origins:
            try:
                parsed = _parse_url(value)
                valid = not parsed.query and parsed.path in ('', '/')
            except ValidationError:
                valid = False
            if not valid:
                raise ValidationError('경로 없이 HTTPS 서버 주소를 한 줄에 하나씩 입력하세요.')
        return origins


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
    change_form_template = 'admin/users/ssoprovider/change_form.html'
    change_list_template = 'admin/users/ssoprovider/change_list.html'
    fields = PROVIDER_FIELDS + ('client_secret', 'callback_address', 'expected_revision',
                                'expected_policy_revision', 'revision',)
    readonly_fields = ('revision', 'callback_address')
    list_display = ('name', 'kind', 'position', 'enabled', 'revision')
    ordering = ('position', 'name')

    def get_urls(self):
        return [path('setup-guide/', self.admin_site.admin_view(self.setup_guide),
                     name='users_ssoprovider_setup_guide')] + super().get_urls()

    def setup_guide(self, request):
        if not self._has_configuration_permission(request):
            raise PermissionDenied
        provider = None
        if request.GET.get('provider'):
            try:
                provider_id = uuid.UUID(request.GET['provider'])
            except ValueError:
                from django.http import Http404
                raise Http404 from None
            provider = get_object_or_404(SSOProvider, pk=provider_id)
        return TemplateResponse(request, 'admin/users/ssoprovider/setup_guide.html', {
            **self.admin_site.each_context(request), 'title': 'SSO 설정 가이드',
            'guides': PROVIDER_GUIDES, 'guide_data': guide_context(request, provider),
            'provider': provider, 'opts': self.model._meta,
        })

    def changeform_view(self, request, object_id=None, form_url='', extra_context=None):
        context = dict(extra_context or {}, provider_guides=PROVIDER_GUIDES,
                       setup_guide_url=reverse('admin:users_ssoprovider_setup_guide'))
        if object_id:
            context['setup_guide_url'] += '?provider=' + str(object_id)
        return super().changeform_view(request, object_id, form_url, context)

    @admin.display(description='등록할 정확한 콜백 주소')
    def callback_address(self, obj):
        if obj is None or obj._state.adding:
            return '비활성 상태로 저장 후 확정 콜백 주소를 확인하세요.'
        try:
            saved = SSOProvider.objects.only('id', 'public_base_url').get(pk=obj.pk)
            return callback_url(saved)
        except (ValidationError, AttributeError, SSOProvider.DoesNotExist):
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
