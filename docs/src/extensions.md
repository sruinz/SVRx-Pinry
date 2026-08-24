# 브라우저 확장 프로그램

기존 Pinry용 Chrome·Firefox 레거시 확장은 웹 페이지에서 이미지 한 개를
`/pin-creation/from-url` 화면으로 전달하는 단건 UI 흐름에 한해 사용할 수
있는 단건 Pin 생성용입니다.

- [Chrome — 기존 단건용](https://chrome.google.com/webstore/detail/jmhdcnmfkglikfjafdmdikoonedgijpa/)
- [Firefox — 기존 단건용](https://addons.mozilla.org/en-US/firefox/addon/add-to-pinry/)

이 기존 확장은 batch API, 토큰 기반 worker 또는 이미지 전체선택 기능을
제공하지 않습니다.

## SVRx Pinry Extension

[SVRx Pinry Extension](https://github.com/sruinz/SVRx-Pinry-Extention)은
현재 페이지에서 여러 이미지를 선택한 뒤 공통 보드·태그·공개 범위를
지정해 한 번에 Pin으로 만드는 전용 확장 프로그램입니다. 서버 주소와 API
토큰은 확장 프로그램의 설정 화면에서 저장합니다.

[Chrome 웹 스토어](https://chromewebstore.google.com/detail/svrx-pinry/kgncmldoobdakadnojepmalpbmacoonh?authuser=0&hl=ko)와
[Microsoft Edge Add-ons](https://microsoftedge.microsoft.com/addons/detail/svrx-pinry/gmbgeiddpdblpjdbceoofclpeiikobjj)에서
설치하거나 GitHub에서 수동 설치할 수 있습니다. Chrome·Edge를 공식 지원하며
Firefox는 공식 지원 대상이 아닙니다.
