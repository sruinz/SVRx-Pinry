import re
import uuid
from dataclasses import dataclass
from urllib.parse import urlencode

from authlib.oauth2.client import OAuth2Client
from authlib.oauth2.rfc6749.errors import OAuth2Error
from authlib.oidc.core import CodeIDToken
from django.core.exceptions import ValidationError
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet

from users.models import SSOProvider
from users.sso.secrets import decrypt_secret
from users.sso.transport import request_json, validate_endpoint


@dataclass(frozen=True)
class VerifiedIdentity:
    issuer: str
    subject: str
    email: str | None
    display_name: str


def _metadata(provider):
    if provider.kind == SSOProvider.Kind.GITHUB:
        return {
            'issuer': 'https://github.com',
            'authorization_endpoint': 'https://github.com/login/oauth/authorize',
            'token_endpoint': 'https://github.com/login/oauth/access_token',
            'userinfo_endpoint': 'https://api.github.com/user',
            'token_endpoint_auth_methods_supported': ['client_secret_post'],
        }
    if provider.kind == SSOProvider.Kind.GOOGLE:
        issuer = 'https://accounts.google.com'
    elif provider.kind == SSOProvider.Kind.MICROSOFT:
        try:
            tenant = str(uuid.UUID(provider.tenant_id))
        except (ValueError, AttributeError):
            raise ValidationError('Microsoft의 명시적인 테넌트 UUID가 필요합니다.') from None
        issuer = 'https://login.microsoftonline.com/' + tenant + '/v2.0'
    elif provider.kind in (
        SSOProvider.Kind.AUTHENTIK, SSOProvider.Kind.SYNOLOGY, SSOProvider.Kind.OIDC,
    ):
        issuer = provider.issuer
    else:
        raise ValidationError('지원하지 않는 SSO 제공자 종류입니다.')
    if not issuer or len(issuer) > 2048:
        raise ValidationError('올바른 SSO 발급자가 필요합니다.')
    discovery = provider.discovery_url or issuer.rstrip('/') + '/.well-known/openid-configuration'
    if provider.kind in (SSOProvider.Kind.GOOGLE, SSOProvider.Kind.MICROSOFT):
        discovery = issuer.rstrip('/') + '/.well-known/openid-configuration'
    metadata = request_json(provider, discovery)
    if metadata.get('issuer') != issuer:
        raise ValidationError('SSO Discovery 발급자가 설정과 다릅니다.')
    for field in ('authorization_endpoint', 'token_endpoint', 'jwks_uri'):
        validate_endpoint(provider, metadata.get(field))
    methods = metadata.get('code_challenge_methods_supported')
    if methods is not None and 'S256' not in methods:
        raise ValidationError('SSO 제공자가 PKCE S256을 지원하지 않습니다.')
    return metadata


def _client(provider, redirect_uri, metadata):
    secret = decrypt_secret(provider.encrypted_client_secret) if provider.encrypted_client_secret else None
    methods = metadata.get('token_endpoint_auth_methods_supported', ['client_secret_basic'])
    method = next((value for value in ('client_secret_basic', 'client_secret_post') if value in methods), None)
    if method is None:
        raise ValidationError('지원하지 않는 SSO 클라이언트 인증 방식입니다.')
    return OAuth2Client(
        session=_ProviderSession(provider), client_id=provider.client_id,
        client_secret=secret, redirect_uri=redirect_uri,
        scope='read:user user:email' if provider.kind == SSOProvider.Kind.GITHUB else 'openid profile email',
        token_endpoint_auth_method=method, code_challenge_method='S256',
    )


class _TokenResponse:
    status_code = 200

    def __init__(self, value):
        self.value = value

    def json(self):
        return self.value


class _ProviderSession:
    def __init__(self, provider):
        self.provider = provider

    def post(self, url, data, headers, auth):
        url, headers, body = auth.prepare('POST', url, dict(headers), urlencode(data))
        value = request_json(self.provider, url, method='POST', headers=headers, body=body)
        return _TokenResponse(value)


def _require_verifier(verifier):
    if not isinstance(verifier, str) or not re.fullmatch(r'[A-Za-z0-9._~-]{43,128}', verifier):
        raise ValidationError('올바른 PKCE 검증 값이 필요합니다.')


def authorization_url(provider, redirect_uri, state, nonce, verifier):
    _require_verifier(verifier)
    if not state or not nonce:
        raise ValidationError('SSO state와 nonce가 필요합니다.')
    metadata = _metadata(provider)
    endpoint = validate_endpoint(provider, metadata['authorization_endpoint'])
    client = _client(provider, redirect_uri, metadata)
    url, state = client.create_authorization_url(
        endpoint, state=state, nonce=nonce, code_verifier=verifier,
    )
    return url


def _oidc_identity(provider, metadata, token, nonce):
    keys = request_json(provider, metadata['jwks_uri'])
    supported = metadata.get('id_token_signing_alg_values_supported', ['RS256'])
    algorithms = [alg for alg in ('RS256', 'RS384', 'RS512', 'PS256', 'PS384', 'PS512', 'ES256', 'ES384', 'ES512', 'EdDSA') if alg in supported]
    if not algorithms:
        raise ValidationError('지원하지 않는 ID 토큰 서명 방식입니다.')
    decoded = jwt.decode(token['id_token'], KeySet.import_key_set(keys), algorithms=algorithms)
    claims = CodeIDToken(
        decoded.claims, decoded.header,
        options={
            'iss': {'essential': True, 'value': metadata['issuer']},
            'aud': {'essential': True, 'value': provider.client_id},
            'nonce': {'essential': True, 'value': nonce},
        },
        params={'nonce': nonce, 'client_id': provider.client_id, 'access_token': token.get('access_token')},
    )
    claims.validate(leeway=0)
    subject = claims.get('sub')
    if not isinstance(subject, str) or not 1 <= len(subject) <= 255:
        raise ValidationError('올바른 SSO 외부 사용자 ID가 필요합니다.')
    return VerifiedIdentity(
        issuer=metadata['issuer'], subject=subject,
        email=claims.get('email') if isinstance(claims.get('email'), str) else None,
        display_name=claims.get('name') if isinstance(claims.get('name'), str) else '',
    )


def exchange_identity(provider, redirect_uri, code, nonce, verifier):
    _require_verifier(verifier)
    if not nonce or not code:
        raise ValidationError('SSO 코드와 nonce가 필요합니다.')
    try:
        metadata = _metadata(provider)
        client = _client(provider, redirect_uri, metadata)
        token = client.fetch_token(
            metadata['token_endpoint'], grant_type='authorization_code',
            code=code, code_verifier=verifier,
        )
        if provider.kind == SSOProvider.Kind.GITHUB:
            user = request_json(
                provider, metadata['userinfo_endpoint'],
                headers={'Authorization': 'Bearer ' + token['access_token']},
            )
            subject = user.get('id')
            if type(subject) is not int or subject <= 0 or len(str(subject)) > 255:
                raise ValidationError('GitHub 숫자 사용자 ID가 필요합니다.')
            return VerifiedIdentity(
                issuer=metadata['issuer'], subject=str(subject),
                email=user.get('email') if isinstance(user.get('email'), str) else None,
                display_name=user.get('name') or user.get('login') or '',
            )
        return _oidc_identity(provider, metadata, token, nonce)
    except (JoseError, OAuth2Error, KeyError, TypeError, ValueError):
        raise ValidationError('SSO 제공자의 인증 응답을 검증하지 못했습니다.') from None
