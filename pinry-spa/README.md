# SVRx Pinry 프런트엔드

Vue 3와 Vite 기반 웹 인터페이스입니다. 설치·운영 안내는 [서버 README](../README.md)를 참고하세요.

## 개발 환경

Node.js 24와 pnpm 9.15.9를 사용합니다.

```sh
npm install -g pnpm@9.15.9
pnpm install --frozen-lockfile
pnpm serve
```

## 검사와 제품 빌드

```sh
pnpm test:unit --runInBand
pnpm lint
pnpm build
```

린트는 자동 수정하지 않으며, 제품 빌드는 `dist/`에 생성하고 브랜드 자산 검사를 수행합니다.
