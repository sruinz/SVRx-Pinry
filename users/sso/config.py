import uuid
from urllib.parse import urlsplit

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from users.models import AuthPolicy, AuthVerification, ExternalIdentity, SSOProvider


POLICY_FIELDS = {
    'password_login_enabled',
    'api_tokens_enabled',
    'recovery_allowed_cidrs',
    'recovery_denied_cidrs',
}
PROVIDER_CREATE_FIELDS = {
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
}
PROVIDER_UPDATE_FIELDS = PROVIDER_CREATE_FIELDS - {'kind'}

PRESET_ORIGINS = {
    'google': ['https://accounts.google.com', 'https://oauth2.googleapis.com', 'https://www.googleapis.com'],
    'microsoft': ['https://login.microsoftonline.com'],
    'github': ['https://github.com', 'https://api.github.com'],
}


def expected_issuer(provider):
    if provider.kind == 'google':
        return 'https://accounts.google.com'
    if provider.kind == 'github':
        return 'https://github.com'
    if provider.kind == 'microsoft':
        try:
            return 'https://login.microsoftonline.com/' + str(uuid.UUID(provider.tenant_id)) + '/v2.0'
        except (ValueError, AttributeError):
            raise ValidationError('Microsoft의 명시적인 테넌트 UUID가 필요합니다.') from None
    return provider.issuer


def validate_provider(provider):
    from users.sso.flows import callback_url
    callback_url(provider)
    issuer = expected_issuer(provider)
    if not provider.client_id or not issuer:
        raise ValidationError('Client ID와 발급자 설정을 확인해 주세요.')
    for value in [issuer, provider.discovery_url or issuer]:
        parsed = urlsplit(value)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise ValidationError('제공자 주소는 사용자 정보 없는 HTTPS URL이어야 합니다.')
    if not isinstance(provider.allowed_endpoint_origins, list):
        raise ValidationError('허용 endpoint origin은 배열이어야 합니다.')
    for origin in provider.allowed_endpoint_origins:
        if not isinstance(origin, str):
            raise ValidationError('허용 endpoint origin은 HTTPS 문자열이어야 합니다.')
        try:
            parsed = urlsplit(origin)
        except ValueError:
            raise ValidationError('허용 endpoint origin을 확인해 주세요.') from None
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path not in ('', '/')):
            raise ValidationError('허용 endpoint origin을 확인해 주세요.')


def current_recovery_fingerprint():
    from pinry.recovery_config import deployment_configuration
    deployment = deployment_configuration()
    return deployment['fingerprint'] if deployment else None


def _require_sso_only_proof(actor, policy):
    from users.sso.policy import identity_is_usable
    identities = ExternalIdentity.objects.filter(user=actor).select_related('provider')
    verified = any(identity_is_usable(identity) and AuthVerification.objects.filter(
        user=actor, kind='sso', provider=identity.provider,
        provider_revision=identity.provider.revision, policy_revision=policy.revision,
    ).exists() for identity in identities)
    if not verified:
        raise ValidationError('비밀번호 로그인을 끄기 전에 현재 설정으로 관리자 SSO 로그인을 확인하세요.')


def read_policy():
    return AuthPolicy.objects.get(pk=1)


def _require_active_superuser(actor):
    if not (
        actor
        and actor.is_authenticated
        and actor.is_active
        and actor.is_superuser
    ):
        raise PermissionDenied('활성 슈퍼 관리자만 인증 설정을 변경할 수 있습니다.')


def _reject_unknown_fields(changes, allowed_fields):
    unknown_fields = set(changes) - allowed_fields
    if unknown_fields:
        raise ValidationError(
            '지원하지 않는 설정 필드입니다: {}'.format(
                ', '.join(sorted(str(field) for field in unknown_fields)),
            ),
        )


def _apply_changes(instance, changes):
    for field_name, value in changes.items():
        setattr(instance, field_name, value)


def _changed_fields(instance, original_values, field_names):
    return [
        field_name
        for field_name in field_names
        if getattr(instance, field_name) != original_values[field_name]
    ]


def _save_provider(provider_changes):
    changes = dict(provider_changes)
    provider_id = changes.pop('id', None)
    secret = changes.pop('client_secret', '')
    expected_revision = changes.pop('expected_revision', None)

    if provider_id is None:
        _reject_unknown_fields(changes, PROVIDER_CREATE_FIELDS)
        provider = SSOProvider(**changes)
        if not provider.allowed_endpoint_origins:
            provider.allowed_endpoint_origins = PRESET_ORIGINS.get(provider.kind, [])[:]
        if secret:
            from users.sso.secrets import encrypt_secret
            provider.encrypted_client_secret = encrypt_secret(secret)
        if provider.enabled:
            validate_provider(provider)
            if not provider.encrypted_client_secret or not provider.allowed_endpoint_origins:
                raise ValidationError('Client Secret과 허용 endpoint origin이 필요합니다.')
        provider.full_clean()
        provider.save()
        return provider

    if 'kind' in changes:
        raise ValidationError('등록된 제공자의 종류는 변경할 수 없습니다.')
    _reject_unknown_fields(changes, PROVIDER_UPDATE_FIELDS)
    try:
        provider = SSOProvider.objects.select_for_update().get(pk=provider_id)
    except (SSOProvider.DoesNotExist, ValidationError) as error:
        raise ValidationError('등록된 SSO 제공자를 찾을 수 없습니다.') from error
    if expected_revision is not None and expected_revision != provider.revision:
        raise ValidationError('제공자 설정이 변경되었습니다. 새로고침 후 다시 저장해 주세요.')

    original_values = {
        field_name: getattr(provider, field_name)
        for field_name in PROVIDER_UPDATE_FIELDS
    }
    _apply_changes(provider, changes)
    if provider.enabled:
        validate_provider(provider)
        if not (secret or provider.encrypted_client_secret) or not provider.allowed_endpoint_origins:
            raise ValidationError('Client Secret과 허용 endpoint origin이 필요합니다.')
    provider.full_clean()
    changed_fields = _changed_fields(
        provider,
        original_values,
        PROVIDER_UPDATE_FIELDS,
    )
    if secret:
        from users.sso.secrets import encrypt_secret
        provider.encrypted_client_secret = encrypt_secret(secret)
        changed_fields.append('encrypted_client_secret')
    if changed_fields:
        provider.revision += 1
        provider.save(update_fields=changed_fields + ['revision'])
    return provider


@transaction.atomic
def save_configuration(actor, policy_changes, provider_changes=None, expected_revision=None):
    _require_active_superuser(actor)
    _reject_unknown_fields(policy_changes, POLICY_FIELDS)

    policy = AuthPolicy.objects.select_for_update().get(pk=1)
    if expected_revision is not None and expected_revision != policy.revision:
        raise ValidationError('인증 정책이 변경되었습니다. 새로고침 후 다시 저장해 주세요.')
    original_values = {
        field_name: getattr(policy, field_name)
        for field_name in POLICY_FIELDS
    }
    _apply_changes(policy, policy_changes)

    policy.full_clean()
    changed_fields = _changed_fields(policy, original_values, POLICY_FIELDS)

    saved_provider = None
    if provider_changes is not None:
        saved_provider = _save_provider(provider_changes)

    if not policy.password_login_enabled:
        if not SSOProvider.objects.filter(enabled=True).exists():
            raise ValidationError('마지막 활성 SSO 제공자를 비활성화할 수 없습니다.')
        if original_values['password_login_enabled']:
            # 함께 바뀐 다른 정책은 기존 성공 증거로 검증할 수 없다.
            if set(changed_fields) - {'password_login_enabled'}:
                raise ValidationError('다른 정책을 먼저 저장하고 SSO·복구 로그인을 다시 확인해 주세요.')
            _require_sso_only_proof(actor, policy)

    if changed_fields:
        policy.revision += 1
        policy.save(update_fields=changed_fields + ['revision'])

    policy._saved_provider = saved_provider
    return policy
