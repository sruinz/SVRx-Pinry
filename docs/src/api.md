# API 사용

SVRx Pinry는 API 토큰으로 외부 클라이언트와 브라우저 확장 프로그램을 연결할 수 있습니다.

## 토큰 확인

로그인한 뒤 **내 메뉴 → 프로필 → 토큰**에서 표시 또는 복사를 선택하세요.
토큰은 비밀번호처럼 취급하고 화면 캡처·로그·공개 저장소에 포함하지 마세요.
관리자가 API 토큰 인증을 비활성화한 경우 사용할 수 없습니다.

## 요청 예시

아래 주소를 자신의 서버 주소로, `YOUR_API_TOKEN`을 본인의 토큰으로 바꿉니다.

```sh
curl -X GET 'https://pinry.example.com/api/v2/profile/users/' \
  -H 'Authorization: Token YOUR_API_TOKEN'
```

공개 예제에는 실제 토큰이나 계정 정보를 사용하지 않습니다.

## API 참고

서버의 DRF API 화면 또는 [프런트엔드 API 호출 코드](https://github.com/sruinz/SVRx-Pinry/blob/main/pinry-spa/src/components/api.js)를 참고하세요.
브라우저 확장 프로그램은 [SVRx Pinry Extension](https://github.com/sruinz/SVRx-Pinry-Extention)에서 확인할 수 있습니다.
