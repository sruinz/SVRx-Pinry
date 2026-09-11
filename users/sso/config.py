from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from users.models import AuthPolicy, SSOProvider


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

    if provider_id is None:
        _reject_unknown_fields(changes, PROVIDER_CREATE_FIELDS)
        provider = SSOProvider(**changes)
        if provider.enabled:
            raise ValidationError('SSO 제공자를 아직 활성화할 수 없습니다.')
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

    original_values = {
        field_name: getattr(provider, field_name)
        for field_name in PROVIDER_UPDATE_FIELDS
    }
    was_enabled = provider.enabled
    _apply_changes(provider, changes)
    if not was_enabled and provider.enabled:
        raise ValidationError('SSO 제공자를 아직 활성화할 수 없습니다.')
    provider.full_clean()
    changed_fields = _changed_fields(
        provider,
        original_values,
        PROVIDER_UPDATE_FIELDS,
    )
    if changed_fields:
        provider.revision += 1
        provider.save(update_fields=changed_fields + ['revision'])
    return provider


@transaction.atomic
def save_configuration(actor, policy_changes, provider_changes=None):
    _require_active_superuser(actor)
    _reject_unknown_fields(policy_changes, POLICY_FIELDS)

    policy = AuthPolicy.objects.select_for_update().get(pk=1)
    original_values = {
        field_name: getattr(policy, field_name)
        for field_name in POLICY_FIELDS
    }
    _apply_changes(policy, policy_changes)

    if (
        original_values['password_login_enabled']
        and not policy.password_login_enabled
    ):
        raise ValidationError('비밀번호 로그인을 아직 끌 수 없습니다.')
    if original_values['api_tokens_enabled'] and not policy.api_tokens_enabled:
        raise ValidationError('API 토큰 인증을 아직 끌 수 없습니다.')

    policy.full_clean()
    changed_fields = _changed_fields(policy, original_values, POLICY_FIELDS)

    saved_provider = None
    if provider_changes is not None:
        saved_provider = _save_provider(provider_changes)

    if changed_fields:
        policy.revision += 1
        policy.save(update_fields=changed_fields + ['revision'])

    policy._saved_provider = saved_provider
    return policy
