<p align="center">
  <img src="docs/src/imgs/svrx-pinry-logo.png" width="128" height="128"
    alt="SVRx Pinry 로고">
</p>

<h1 align="center">SVRx Pinry Server</h1>

<p align="center">
  한국어와 대량 이미지 관리에 맞춘 셀프 호스팅 이미지 보드
</p>

SVRx Pinry Server는 개인 서버나 NAS에 설치해 이미지를 보드와 태그로
정리하는 서버입니다. SVRx Pinry 브라우저 확장 프로그램을 사용하면
웹페이지의 여러 이미지를 한 번에 저장할 수 있습니다.

이 프로젝트는 Pinry를 기반으로 만든 비공식 수정 배포판입니다.

## 주요 기능

- 한국어를 기본으로 제공하는 사용자 인터페이스
- 내 Pin과 보드 안에서 선택·범위 선택·전체 선택을 지원하는 대량 관리
- 선택한 Pin의 일괄 삭제, 보드 이동 및 정보 수정
- 보드 삭제 시 포함된 Pin의 삭제 여부를 사용자가 선택하는 안전한 흐름
- 삭제 확인 후 데이터베이스와 연결된 이미지 파일을 함께 정리하는 구조
- 원본 이미지와 썸네일·파생 이미지를 분리하는 미디어 저장소
- 원본 파일명 보존과 동일 이미지 중복 저장 방지
- Pin 생성 실패 시 준비된 임시 파일을 정리하는 업로드 처리
- 브라우저 확장 프로그램을 위한 일괄 Pin 생성 API
- 기존 Pinry 데이터의 자동 백업·마이그레이션과 한국어 진행 화면
- 관리자 페이지의 태그 검색·사용량 확인·중복 태그 병합
- 프로필과 버전 API에서 실행 중인 소스 커밋 확인
- Synology NAS에서 직접 빌드할 수 있는 전용 Docker 산출물

## 브라우저 확장 프로그램

[SVRx Pinry Extension](https://github.com/sruinz/SVRx-Pinry-Extention)은
현재 페이지에서 여러 이미지를 선택하고, 공통 보드·태그·공개 범위를
지정해 한 번에 Pin으로 만드는 브라우저 확장 프로그램입니다.

서버 주소와 API 토큰은 확장 프로그램의 설정 화면에서 저장합니다.
[Chrome 웹 스토어](https://chromewebstore.google.com/detail/svrx-pinry/kgncmldoobdakadnojepmalpbmacoonh?authuser=0&hl=ko)와
[Microsoft Edge Add-ons](https://microsoftedge.microsoft.com/addons/detail/svrx-pinry/gmbgeiddpdblpjdbceoofclpeiikobjj)에서
설치하거나 GitHub에서 수동 설치할 수 있습니다. SVRx Pinry 확장은 Chrome·Edge를 공식
지원하고 Firefox는 공식 지원 대상이 아닙니다. 기존 Pinry용 Chrome·Firefox
레거시 확장은 단건 Pin 생성용입니다.

[확장 저장소](https://github.com/sruinz/SVRx-Pinry-Extention)의 소스로 수동
배포 후보를 만들려면 확장 저장소 루트에서 다음을 실행합니다.

```sh
bash scripts/package.sh
```

확장 ZIP은 서버 산출물과 같은 작업공간의 공통 `output/` 아래
`svrx-pinry-extension-<커밋 앞 12자리>/svrx-pinry-extension-0.1.0.zip`으로
생성됩니다.

## Synology Docker 이미지 만들기

현재 저장소는 완성된 Docker Hub 이미지를 전제로 하지 않습니다. 개발
PC에서 Synology용 산출물을 만든 뒤 NAS에 업로드해 이미지를 직접
빌드합니다.

저장소 루트에서 다음 명령을 실행합니다.

```sh
./scripts/create_synology_output.sh
```

기본 산출물은 저장소 루트 기준 `../output/`, 즉 저장소와 같은 작업공간의
공통 `output/` 폴더에 생성됩니다.

```text
<작업공간>/
├── <서버 저장소>/
└── output/
    ├── svrx-pinry-server-<서버 커밋 앞 12자리>/
    │   ├── svrx-pinry/
    │   ├── svrx-pinry-<서버 커밋 앞 12자리>.tar.gz
    │   ├── sw-transition/
    │   └── accept-tools/
    └── svrx-pinry-extension-<확장 커밋 앞 12자리>/
        └── svrx-pinry-extension-0.1.0.zip
```

생성된 압축 파일 또는 `svrx-pinry/` 폴더를 NAS에 업로드합니다. 이미지
빌드, Compose 실행 및 데이터 백업 방법은
[Synology 이미지 빌드 및 실행 안내](deploy/synology/README_KO.md)를
따르세요.

산출물에는 본 이미지 빌드·실행 파일, 기존 브라우저 캐시를
안전하게 전환하는 별도 `sw-transition/` context, 실제 NAS 복제본을
검증하는 선택 도구 `accept-tools/`가 들어갑니다. 데이터베이스,
업로드 이미지와 비밀키는 포함하지 않습니다.

기존 Pinry 또는 SVRx Pinry를 교체하며 같은 origin에 서비스 워커가
등록된 경우, NAS의 같은 작업 폴더에 main package와
`sw-transition/`을 **함께** 올립니다. `svrx-pinry-<커밋>.tar.gz`에는 main
package인 `svrx-pinry/`만 들어 있으므로, 압축 파일을 사용해도
`sw-transition/`은 디렉터리째 별도로 업로드해야 합니다. NAS에서 먼저
기존 main container를 중지해 2048 포트를 비운 뒤, Container Manager에서
`sw-transition/docker-compose.yml`을 사용하는 임시 project를 실행합니다.
이 project에는 데이터 마운트가 없으며 SSH 접속 없이 만들고 중지할 수
있습니다.

기존에 사용하던 모든 브라우저·확장 프로그램 클라이언트에서 전환 화면의
**전환 준비 완료**를 확인한 뒤 Container Manager에서 전환 project를
중지합니다. 그 다음 `svrx-pinry/`에서 본 이미지를 빌드하고 기존 Compose
project를 다시 실행합니다.
전체 명령과 이관 순서는
[Synology 이미지 빌드 및 실행 안내](deploy/synology/README_KO.md)를 따르세요.

## 최초 관리자 계정

신규 설치에서는 첫 번째로 성공한 웹 회원가입 계정이 자동으로 활성 상태의
Django 스태프 및 슈퍼유저가 됩니다. 설치한 서버를 운영할 사람이 다른
사용자보다 먼저 가입하세요.

기존 설치를 업그레이드할 때 활성 superuser가 이미 있으면 기존 권한을
변경하지 않습니다. 활성 superuser가 없으면 `(date_joined, id)` 순으로 가장
먼저 생성된 활성 계정 하나를 관리자 계정으로 한 번만 승격합니다. 활성
계정도 없다면 이후 첫 번째로 성공한 웹 회원가입 계정이 관리자 권한을
받습니다. 이 판정은 한 번만 완료되므로 관리자를 의도적으로 강등한 뒤에도
다른 계정이 자동으로 다시 승격되지는 않습니다.

현재 로그인한 활성 스태프 사용자는 자신의 프로필에 표시되는 **관리자
설정**을 통해 같은 서버의 `/admin/`으로 이동할 수 있습니다. 최초 관리자
지정 이후의 사용자 권한 변경은 이 Django 관리자 페이지에서 수행합니다.

## 관리자 태그 관리

관리자는 **관리자 설정 → Taggit → 태그**에서 태그를 검색하고 각 태그에
연결된 Pin 수를 확인할 수 있습니다. 개별 태그 화면에서는 이름을 변경하거나
태그를 삭제할 수 있습니다.

중복 태그를 합치려면 목록에서 두 개 이상의 태그를 선택한 뒤 **선택한 태그
병합**을 실행하고, 선택한 태그 중 유지할 대표 태그를 지정합니다. 모든 Pin
연결은 대표 태그로 이동하고 같은 Pin의 중복 연결은 하나로 정리되며, 나머지
태그는 삭제됩니다. 병합 작업은 태그 변경·삭제 권한이 모두 있는 관리자만
사용할 수 있으며 한 트랜잭션으로 처리됩니다. 대량 병합 전에는 데이터
폴더를 백업하세요.

## 기존 Pinry 데이터 자동 마이그레이션

Synology 산출물의 Compose 파일은 컨테이너를 시작할 때 기존 데이터를
자동으로 판별합니다. 빈 데이터 폴더를 사용하는 신규 설치나 이미 현재
형식으로 전환된 SVRx Pinry는 마이그레이션을 건너뛰고 정상 서비스를
시작합니다. 기존 Pinry 데이터나 갱신이 필요한 SVRx Pinry 데이터가 있으면
별도의 SSH 명령 없이 다음 작업을 자동으로 수행합니다.
미디어와 무관하다고 명시적으로 검증된 DB schema 갱신은 전체 이미지를
다시 검사하지 않고 표준 Django migration으로 빠르게 적용합니다.
현재는 최초 관리자 지정용 `users.0002_admin_bootstrap`만 이 경로로
처리하며, 알 수 없거나 미디어 관련 변경은 기존 안전 절차를 유지합니다.

1. 기존 SQLite 데이터베이스의 온라인 스냅샷과 무결성 검사를 수행합니다.
2. 기존 미디어를 UUID·원본 파일명 기반의 현재 저장 구조로 옮깁니다.
3. 확정된 배치 단위로 진행 상태를 기록해 정상 재시작 시 이어서 처리합니다.
4. 최종 검증이 끝나면 기존 파일을 `data/legacy-backup/<실행 ID>/`에
   보존하고 SVRx Pinry 서비스를 시작합니다.

마이그레이션 전에는 기존 컨테이너를 중지하고 데이터 폴더 전체를 별도
위치에 백업해야 합니다. 기존 데이터 경로를 계속 마운트하거나 그 **내용
전체**를 새 `data` 폴더로 복사할 수 있으며, `data/data`처럼 한 단계 더
중첩되지 않도록 주의합니다. 제공된 Compose 파일의 자동 마이그레이션
`command` 항목은 제거하지 않습니다.

작업 중에는 같은 접속 주소에 한국어 진행 화면이 표시되고 일반 API와
미디어 요청은 안전하게 차단됩니다. 브라우저를 닫아도 작업은 계속되므로
컨테이너·프로젝트를 강제 종료하거나 데이터 폴더를 수정하지 마세요. 정상
중지 후 다시 시작하면 마지막 확정 배치부터 재개합니다. 완료되면 별도
조작 없이 일반 서비스로 전환됩니다.

SA6400 검증 환경에서는 이미지 레코드 346개와 참조 미디어 파일 1,384개의
이관이 2분 이내에 완료되었습니다. 실제 소요 시간은 NAS 성능, 저장 장치와
이미지 크기에 따라 달라질 수 있습니다.

진행 상태는 `/migration/` 또는 `/migration-status.json`에서 확인할 수
있습니다. 오류는 **재시도 가능**, **사용자 조치 필요**, **치명적**으로
구분되며, 데이터가 불완전한 상태로 서비스를 시작하지 않고 원본과 백업을
보존합니다. 화면의 오류 코드와 DSM Container Manager 로그를 함께
확인하세요.

자세한 데이터 경로 준비, 서비스 워커 전환, 상태 경로, 백업 구조와 롤백
절차는 [Synology 이미지 빌드 및 실행 안내](deploy/synology/README_KO.md)에
정리되어 있습니다.

## 실행 버전 확인

산출물의 `BUILD_INFO`, Docker 이미지 레이블과 실행 중인 서버는 같은 Git
커밋을 표시합니다. 로그인 후 **내 메뉴 → 프로필 → 빌드 정보**에서 짧은
커밋 값을 확인하거나 다음 API를 사용할 수 있습니다.

```sh
curl -s http://NAS주소:2048/api/v2/version/
```

같은 `latest` 태그로 이미지를 다시 빌드했다면 Compose에서 컨테이너를
강제로 다시 생성해야 새 이미지가 실행됩니다.

```sh
docker compose up -d --force-recreate
```

## 데이터 보존

기본 Compose 설정은 `/volume1/docker/svrx-pinry/data`를 영구 데이터
경로로 사용합니다. 이 폴더에는 데이터베이스, 업로드 이미지와 서버 설정이
저장됩니다. 컨테이너나 이미지를 교체하기 전에 해당 폴더를 백업하고 직접
삭제하지 마세요.

## 라이선스와 출처

SVRx Pinry는 BSD 2-Clause 조건으로 배포됩니다.

- [라이선스](LICENSE.md)
- [오픈소스 고지](NOTICE.md)
- [Upstream 정보](UPSTREAM.md)
