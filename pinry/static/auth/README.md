# 로그인 화면 자산

- `brand.png`: 기존 SVRx Pinry 다크 화면용 로고를 재사용한다.
- `providers/*.svg`: 사용자 프로젝트 Dock마루의 `frontend/src/components/SSOButton.jsx`에서 제공자 아이콘을 추출했다. 모양은 변경하지 않았으며 독립 SVG로 저장하고 `currentColor`를 밝은 전경색으로 고정했다. `synology`와 `oidc`는 해당 프로젝트의 식별 아이콘이며 공식 로고라는 의미가 아니다.
- `icons/*.svg`: Lucide React 0.303.0의 X, ChevronDown, LockKeyhole을 정적 SVG로 렌더링했다.
- 모든 파일은 이미지 안에 포함해 같은 서버에서 제공한다. 로그인 화면이 외부 아이콘 CDN에 의존하지 않는다.
- 원문 라이선스는 `LICENSE.DOCKMARU`와 `LICENSE.LUCIDE`에 보존한다. 제공자 상표의 권리는 각각의 권리자에게 있다.
