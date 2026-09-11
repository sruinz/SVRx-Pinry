import hashlib
import ipaddress
import json
import uuid

from django.conf import settings
from django.contrib.auth.models import User as BaseUser
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone


PRIVATE_NETWORKS = (
    ipaddress.ip_network('10.0.0.0/8'),
    ipaddress.ip_network('172.16.0.0/12'),
    ipaddress.ip_network('192.168.0.0/16'),
    ipaddress.ip_network('fc00::/7'),
)


def normalize_cidrs(values, private_only):
    if not isinstance(values, list):
        raise ValidationError('CIDR 목록은 배열이어야 합니다.')

    normalized = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValidationError('빈 CIDR 값은 허용되지 않습니다.')
        try:
            network = ipaddress.ip_network(value.strip(), strict=True)
        except ValueError as error:
            raise ValidationError('올바른 CIDR 또는 IP 주소가 아닙니다.') from error

        if network.prefixlen == 0:
            raise ValidationError('전체 주소 대역은 허용되지 않습니다.')
        if private_only and not any(
            network.version == private.version and network.subnet_of(private)
            for private in PRIVATE_NETWORKS
        ):
            raise ValidationError('RFC1918 또는 IPv6 ULA 대역만 허용됩니다.')
        normalized.append(str(network))
    return normalized


def make_identity_digest(provider_id, issuer, subject):
    serialized = json.dumps(
        [str(provider_id), issuer, subject],
        ensure_ascii=False,
        separators=(',', ':'),
    )
    return hashlib.sha256(serialized.encode('utf-8')).hexdigest()


def create_token_if_necessary(user: BaseUser):
    from rest_framework.authtoken.models import Token
    token = Token.objects.filter(user=user).first()
    if token is not None:
        return token
    else:
        return Token.objects.create(user=user)


class User(BaseUser):

    @property
    def gravatar(self):
        return hashlib.md5(self.email.encode('utf-8')).hexdigest()

    class Meta:
        proxy = True


class AdminBootstrapState(models.Model):
    bootstrap_complete = models.BooleanField(default=False)

    @classmethod
    def claim_for_initial_admin(cls):
        claimed = cls.objects.filter(
            pk=1,
            bootstrap_complete=False,
        ).update(bootstrap_complete=True)
        if claimed != 1:
            return False
        return not BaseUser.objects.filter(is_active=True).exists()


class AuthPolicy(models.Model):
    password_login_enabled = models.BooleanField(default=True)
    api_tokens_enabled = models.BooleanField(default=True)
    recovery_allowed_cidrs = models.JSONField(default=list, blank=True)
    recovery_denied_cidrs = models.JSONField(default=list, blank=True)
    revision = models.PositiveIntegerField(default=1, editable=False)

    def clean(self):
        super().clean()
        try:
            self.recovery_allowed_cidrs = normalize_cidrs(
                self.recovery_allowed_cidrs,
                private_only=True,
            )
        except ValidationError as error:
            raise ValidationError(
                {'recovery_allowed_cidrs': error.messages},
            ) from error
        try:
            self.recovery_denied_cidrs = normalize_cidrs(
                self.recovery_denied_cidrs,
                private_only=False,
            )
        except ValidationError as error:
            raise ValidationError(
                {'recovery_denied_cidrs': error.messages},
            ) from error

    def __str__(self):
        return '인증 정책'


class SSOProvider(models.Model):
    class Kind(models.TextChoices):
        AUTHENTIK = 'authentik', 'Authentik'
        SYNOLOGY = 'synology', 'Synology SSO Server'
        GOOGLE = 'google', 'Google'
        MICROSOFT = 'microsoft', 'Microsoft'
        GITHUB = 'github', 'GitHub'
        OIDC = 'oidc', '범용 OIDC'

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    kind = models.CharField(max_length=20, choices=Kind.choices)
    name = models.CharField(max_length=150)
    position = models.PositiveIntegerField(default=0)
    enabled = models.BooleanField(default=False)
    public_base_url = models.URLField(max_length=2048, blank=True)
    issuer = models.URLField(max_length=2048, blank=True)
    discovery_url = models.URLField(max_length=2048, blank=True)
    tenant_id = models.CharField(max_length=255, blank=True)
    client_id = models.CharField(max_length=255, blank=True)
    encrypted_client_secret = models.TextField(blank=True, editable=False)
    allowed_endpoint_origins = models.JSONField(default=list, blank=True)
    internal_cidrs = models.JSONField(default=list, blank=True)
    allow_signup = models.BooleanField(default=False)
    revision = models.PositiveIntegerField(default=1, editable=False)

    class Meta:
        ordering = ('position', 'id')

    def clean(self):
        super().clean()
        try:
            self.internal_cidrs = normalize_cidrs(
                self.internal_cidrs,
                private_only=True,
            )
        except ValidationError as error:
            raise ValidationError({'internal_cidrs': error.messages}) from error

    def __str__(self):
        return self.name


class ExternalIdentity(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='external_identities',
    )
    provider = models.ForeignKey(
        SSOProvider,
        on_delete=models.PROTECT,
        related_name='external_identities',
    )
    issuer = models.CharField(max_length=2048)
    subject = models.CharField(max_length=255)
    identity_digest = models.CharField(
        max_length=64,
        unique=True,
        editable=False,
    )

    def clean(self):
        self.identity_digest = make_identity_digest(
            self.provider_id,
            self.issuer,
            self.subject,
        )
        super().clean()

    def save(self, *args, **kwargs):
        self.identity_digest = make_identity_digest(
            self.provider_id,
            self.issuer,
            self.subject,
        )
        if kwargs.get('update_fields') is not None:
            kwargs['update_fields'] = set(kwargs['update_fields']) | {
                'identity_digest',
            }
        return super().save(*args, **kwargs)


class SSOAttempt(models.Model):
    class Purpose(models.TextChoices):
        LOGIN = 'login', '로그인'
        LINK = 'link', '연결'
        REAUTH = 'reauth', '재인증'

    state_digest = models.CharField(max_length=64, unique=True)
    browser_digest = models.CharField(max_length=64)
    provider = models.ForeignKey(
        SSOProvider,
        on_delete=models.PROTECT,
        related_name='attempts',
    )
    provider_revision = models.PositiveIntegerField()
    purpose = models.CharField(max_length=10, choices=Purpose.choices)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='sso_attempts',
        blank=True,
        null=True,
    )
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(blank=True, null=True)
    protected_payload = models.TextField()


class AuthVerification(models.Model):
    class Kind(models.TextChoices):
        SSO = 'sso', 'SSO'
        RECOVERY = 'recovery', '복구'

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='auth_verifications',
    )
    kind = models.CharField(max_length=10, choices=Kind.choices)
    provider = models.ForeignKey(
        SSOProvider,
        on_delete=models.SET_NULL,
        related_name='auth_verifications',
        blank=True,
        null=True,
    )
    policy_revision = models.PositiveIntegerField()
    provider_revision = models.PositiveIntegerField(blank=True, null=True)
    deployment_fingerprint = models.CharField(max_length=255)
    verified_at = models.DateTimeField(default=timezone.now)


class AuthenticationThrottle(models.Model):
    key_digest = models.CharField(max_length=64, unique=True)
    window_started_at = models.DateTimeField(default=timezone.now)
    failure_count = models.PositiveIntegerField(default=0)
    blocked_until = models.DateTimeField(blank=True, null=True)


@receiver(post_save, sender=User)
def create_profile(sender, instance: User, **kwargs):
    create_token_if_necessary(instance)
