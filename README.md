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
- 프로필과 버전 API에서 실행 중인 소스 커밋 확인
- Synology NAS에서 직접 빌드할 수 있는 전용 Docker 산출물

## 브라우저 확장 프로그램

[SVRx Pinry Extension](https://github.com/sruinz/SVRx-Pinry-Extention)은
현재 페이지에서 여러 이미지를 선택하고, 공통 보드·태그·공개 범위를
지정해 한 번에 Pin으로 만드는 브라우저 확장 프로그램입니다.

서버 주소와 API 토큰은 확장 프로그램의 설정 화면에서 저장합니다.

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
    └── svrx-pinry-server-<커밋 앞 12자리>/
        ├── svrx-pinry/
        └── svrx-pinry-<커밋 앞 12자리>.tar.gz
```

생성된 압축 파일 또는 `svrx-pinry/` 폴더를 NAS에 업로드합니다. 이미지
빌드, Compose 실행 및 데이터 백업 방법은
[Synology 이미지 빌드 및 실행 안내](deploy/synology/README_KO.md)를
따르세요.

산출물에는 이미지 빌드와 실행에 필요한 파일만 들어갑니다. 데이터베이스,
업로드 이미지와 비밀키는 포함하지 않습니다.

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
