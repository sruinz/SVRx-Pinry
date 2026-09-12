"""관리자 입력 도움말. 인증 경로나 제공자 설정은 변경하지 않는다."""

from django.core.exceptions import ValidationError
from django.urls import reverse
from urllib.parse import urlsplit

from .config import PRESET_ORIGINS, expected_issuer
from .flows import callback_url


SELF_HOSTED = ('authentik', 'synology', 'oidc')
PROVIDER_GUIDES = {
    'authentik': {
        'label': 'Authentik', 'issuer_example': 'https://auth.example.com/application/o/pinry/',
        'discovery_example': 'https://auth.example.com/application/o/pinry/.well-known/openid-configuration',
        'client_example': 'Authentik에서 발급한 Client ID',
        'steps': ['Applications에서 OAuth2/OpenID 제공자와 애플리케이션을 만드세요.',
                  'Confidential 클라이언트를 사용하고 아래 확정 리디렉션 URI를 정확히 등록하세요.',
                  'Client ID·Client Secret과 Discovery 문서의 issuer 값을 입력하세요.'],
        'note': '전역 issuer 모드에서는 발급자와 Discovery 경로가 다릅니다. 애플리케이션의 Discovery URL을 직접 입력하세요.',
        'docs': 'https://docs.goauthentik.io/add-secure-apps/providers/oauth2',
    },
    'synology': {
        'label': 'Synology SSO Server', 'issuer_example': 'https://sso.example.com/webman/sso',
        'discovery_example': 'https://sso.example.com/webman/sso/.well-known/openid-configuration',
        'client_example': 'SSO Server의 응용 프로그램 ID',
        'steps': ['SSO Server에서 OIDC 서버를 활성화하세요.',
                  'OIDC 응용 프로그램을 추가하고 아래 확정 리디렉션 URI를 등록하세요.',
                  '응용 프로그램 ID·비밀, Well-known URL과 해당 문서의 issuer를 입력하세요.'],
        'note': '주소 예시는 설치 환경에 따라 다릅니다. SSO Server가 표시하는 실제 Well-known URL을 우선 사용하세요.',
        'docs': 'https://kb.synology.com/en-global/DSM/help/SSOServer/sso_server_application_list?version=7',
    },
    'oidc': {
        'label': '범용 OIDC', 'issuer_example': 'https://idp.example.com/realms/pinry',
        'discovery_example': 'https://idp.example.com/realms/pinry/.well-known/openid-configuration',
        'client_example': 'pinry',
        'steps': ['제공자에서 서버용 OIDC 클라이언트를 만드세요.',
                  '아래 확정 리디렉션 URI와 openid profile email 범위를 등록하세요.',
                  'Client ID·비밀, Discovery URL과 문서의 issuer를 입력하세요.'],
        'note': '현재 Pinry는 PKCE S256을 지원하는 제공자가 필요합니다. issuer는 문서와 마지막 슬래시까지 같아야 합니다.',
        'docs': 'https://openid.net/specs/openid-connect-discovery-1_0.html',
    },
    'google': {
        'label': 'Google', 'client_example': '123456789-example.apps.googleusercontent.com',
        'steps': ['Google Cloud에서 OAuth 동의 화면과 웹 애플리케이션 클라이언트를 설정하세요.',
                  '승인된 리디렉션 URI에 아래 확정 주소를 등록하세요.',
                  'Client ID와 Client Secret을 입력하세요. 테스트 모드라면 테스트 사용자도 등록하세요.'],
        'note': '발급자와 Discovery URL은 자동 적용됩니다.',
        'docs': 'https://developers.google.com/identity/openid-connect/openid-connect',
    },
    'microsoft': {
        'label': 'Microsoft Entra ID', 'client_example': '11111111-2222-4333-8444-555555555555',
        'steps': ['Entra 관리 센터에서 앱을 등록하고 웹 플랫폼을 선택하세요.',
                  '아래 확정 리디렉션 URI와 애플리케이션(클라이언트) ID, 디렉터리(테넌트) ID를 확인하세요.',
                  '클라이언트 비밀을 만들고 비밀 ID가 아닌 비밀 값을 입력하세요.'],
        'note': '현재 Pinry는 명시적인 테넌트 UUID를 사용합니다. common·organizations·도메인 이름은 입력하지 마세요.',
        'docs': 'https://learn.microsoft.com/en-us/entra/identity-platform/v2-protocols-oidc',
    },
    'github': {
        'label': 'GitHub', 'client_example': 'Ov23liExampleClientID',
        'steps': ['GitHub Settings → Developer settings → OAuth Apps에서 앱을 만드세요.',
                  'Homepage URL에는 Pinry 공개 주소, Authorization callback URL에는 아래 확정 주소를 등록하세요.',
                  'Client ID와 생성한 Client Secret을 입력하세요.'],
        'note': 'GitHub OAuth 로그인에는 Discovery URL이 없습니다. read:user user:email 범위를 사용합니다.',
        'docs': 'https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/creating-an-oauth-app',
    },
}

for _kind, _guide in PROVIDER_GUIDES.items():
    _guide['origin_example'] = '\n'.join(PRESET_ORIGINS.get(_kind, [])) or (
        'https://' + urlsplit(_guide['issuer_example']).netloc)


def _origin(value):
    parsed = urlsplit(value)
    return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80)


def guide_context(request, provider=None):
    current_origin = request.build_absolute_uri('/').rstrip('/')
    data = {'current_origin': current_origin, 'kind': provider.kind if provider else 'authentik',
            'issuer': provider.issuer if provider else '', 'tenant_id': provider.tenant_id if provider else '',
            'discovery_url': '', 'callback': '', 'preview_callback': '',
            'public_base_url': provider.public_base_url if provider else '', 'origin_mismatch': False}
    if provider:
        data['preview_callback'] = current_origin + reverse('sso:callback', args=[provider.pk])
        try:
            data['callback'] = callback_url(provider)
            data['origin_mismatch'] = _origin(current_origin) != _origin(provider.public_base_url)
        except (ValidationError, ValueError):
            pass
        try:
            issuer = expected_issuer(provider)
            if issuer and provider.kind != 'github':
                data['discovery_url'] = (provider.discovery_url if provider.kind in SELF_HOSTED else '') or (
                    issuer.rstrip('/') + '/.well-known/openid-configuration')
        except ValidationError:
            pass
    return data
