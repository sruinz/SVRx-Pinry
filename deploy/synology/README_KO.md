# 시놀로지 이미지 빌드·실행·자동 이관 안내

이 문서는 SVRx Pinry Docker 이미지를 시놀로지 NAS에서 직접 빌드하고
Compose로 실행하는 방법과 기존 Pinry 데이터를 자동 이관하는 방법을
설명한다. 데이터베이스, 업로드 이미지, 비밀키와 기존 컨테이너 데이터는
산출물에 포함하지 않는다.

## 개발 PC에서 산출물 생성

저장소 루트에서 다음 명령을 실행한다.

```sh
./scripts/create_synology_output.sh
```

기본 산출물 경로는 canonical checkout의 저장소 루트 기준
`../output/svrx-pinry-server-<커밋 앞 12자리 SHA>/`이다. 그 아래에 NAS로
업로드할 `svrx-pinry/` 폴더와 `svrx-pinry-<커밋 앞 12자리 SHA>.tar.gz`
파일, 같은 커밋의 `sw-transition/` 빌드 context와 선택 검증용
`accept-tools/`가 생성된다. linked worktree에서 실행해도 같은 workspace의
공통 `output/` 경로를 사용한다.

다른 위치가 필요하면 경로를 직접 지정한다.

```sh
./scripts/create_synology_output.sh /원하는/산출물/경로
```

직접 지정하는 산출물 경로도 아직 존재하지 않는 새 경로여야 한다. 같은
경로에 빈 디렉터리, 파일, 내용이 있는 디렉터리 또는 심볼릭 링크가 하나라도
있으면 기존 항목을 변경하지 않고 중단한다. 기존 폴더 안에 산출물 네 항목을
추가하는 방식은 지원하지 않는다.

패키지는 시작 시점의 Git `HEAD` 커밋 하나에서만 만든다. 생성기,
`build-image.sh`, Compose, 이 안내 문서 또는 법적 고지 파일에 tracked
미커밋 변경이 있으면 서로 다른 revision이 섞이지 않도록 생성 전에
중단한다. 테스트, 설계 문서, GitHub 설정과 개발용 파일은 포함하지 않는다.
같은 커밋의 기존 기본 산출물이 있으면 덮어쓰지 않고 중단한다. 기본 경로와
직접 지정한 경로 모두 완성된 네 항목을 부모의 비공개 임시 디렉터리에 먼저
만들어 동기화한 뒤, 최상위 디렉터리를 한 번에 공개한다. 생성 도중 실패하거나
강제 종료되면 최종 산출물 경로는 생기지 않으므로 같은 명령을 즉시 다시
실행할 수 있다.

실패 시 외부에서 임시 디렉터리 안에 항목을 삽입했을 가능성이 있으면 안전을
위해 재귀 삭제하지 않고 `cleanup_deferred=<경로>`를 표준 오류에 표시한다.
이 숨김 임시 디렉터리는 최종 산출물이 아니며 다음 실행은 새 임시 디렉터리를
사용한다. 해당 생성 프로세스가 더 이상 실행 중이지 않은 것을 확인한 뒤
내용을 검토하고 수동으로 삭제할 수 있다.

전체 산출물의 최상위 구조는 다음과 같다.

```text
svrx-pinry-server-<커밋 앞 12자리 SHA>/
├── svrx-pinry/
├── svrx-pinry-<커밋 앞 12자리 SHA>.tar.gz
├── sw-transition/
└── accept-tools/
    ├── BUILD_INFO
    ├── nas_legacy_clone_acceptance.sh
    ├── fixtures/
    │   └── create_legacy_fixture.py
    ├── docker/tests/
    │   ├── export_runtime_smoke.sh
    │   ├── export_postgres_concurrency_smoke.sh
    │   └── fixtures/create_export_fixture.py
    ├── exports/tests/
    │   └── PostgreSQL 검증에 필요한 테스트 모듈
    └── pinry/settings/test_postgres.py
```

압축 파일에는 아래 `svrx-pinry/`만 들어 있고 `sw-transition/`과
`accept-tools/`는 포함되지 않는다. 본 이미지 업로드 폴더와 압축 파일
내부의 구조는 다음과 같다.

```text
svrx-pinry/
├── BUILD_INFO
├── LICENSE.md
├── NOTICE.md
├── README_KO.md
├── UPSTREAM.md
├── build-image.sh
├── docker-compose.yml
└── context/
    ├── Dockerfile.autobuild
    ├── LICENSE.md
    ├── NOTICE.md
    ├── UPSTREAM.md
    ├── requirements.txt
    ├── manage.py
    ├── core/
    ├── django_images/
    ├── pinry/
    ├── pinry_plugins/
    ├── users/
    ├── pinry-spa/
    └── docker/
```

Docker는 `context/`만 build context로 사용한다. 바깥의 실행 안내와 법적
고지 파일은 최종 이미지에 복사되지 않는다. 이미지가 배포해야 하는 법적
고지는 `context/`에도 별도로 들어 있다.

`sw-transition/`은 본 이미지와 섞지 않는 별도 Container Manager project이다.
데이터 마운트 없이 기존 Pinry 또는 SVRx Pinry에서 서비스 워커를 등록한
것과 정확히 같은 origin에 임시로 실행해 브라우저의
기존 서비스 워커와 캐시를 네트워크 전용 버전으로 바꾸는 용도로만 사용한다.

`accept-tools/`는 실제 레거시 데이터의 **복제본** 이관 계약과 내보내기
런타임·PostgreSQL 동시성을 확인하는 선택 도구다. 일반 설치와 자동
이관에는 필요하지 않으며, 사용할 때만 디렉터리째 NAS에 별도로
업로드한다. `BUILD_INFO`는 본 패키지와 같은 40자리 source commit을
기록한다. 각 script는 `accept-tools/` 아래의 필요한 fixture와 테스트
모듈을 상대 경로로 찾아 사용한다.

운영 이미지용 `svrx-pinry/context/`에는 이 테스트 파일을 의도적으로 넣지
않는다. 따라서 `context/docker/tests/...`를 실행하면 파일을 찾을 수 없으며,
반드시 최상위 산출물의 `accept-tools/docker/tests/...`를 실행해야 한다.

## 선택: 실제 NAS 복제 acceptance

이 절차는 정상 설치 절차가 아니라 배포 후보 이미지를 실제 NAS 데이터의
격리된 복제본으로 검증하는 관리자용 절차다. Docker CLI를 실행할 수 있는
SSH 또는 동등한 터미널 환경이 필요하다. script는 source의 `data/`를 고유한
clone으로 복사하고 source fingerprint를 전후 비교하며, 실패해도 clone과
결과 JSON을 자동 삭제하지 않는다.

실행 전에 기존 Pinry container를 정상 중지한다. 실행 중인 다른 container가
source project나 `data/` 자체·하위 또는 더 넓은 조상 경로를 bind mount하면
읽기 전용 여부와 관계없이 중단한다. 단, `dirname(source_project)`와 정확히
같은 parent bind mount는 `RW=false`인 경우에만 허용한다. 같은 정확한 parent를
`RW=true`로 mount한 container가 하나라도 있으면 `nas_source_container_running`
으로 중단한다.

아래 명령의 수량은 검증된 기준 source workload다. Pin 345개, 물리 media
regular file 1,442개, Image 346행, Thumbnail 1,038행, DB가 참조하는 활성
원본·파생 파일 1,384개를 각각 독립적으로 고정한다. 다른 source를 검증할
때는 다섯 값을 그 source의 실제 값으로 모두 바꾼다. `run-id`에 해당하는
clone과 결과 JSON은 실행 전에 존재하지 않아야 한다.

```sh
cd /volume1/docker/svrx-pinry-upload
source_commit="$(sed -n 's/^source_commit=//p' accept-tools/BUILD_INFO)"
bash accept-tools/nas_legacy_clone_acceptance.sh \
  --source-project /volume1/docker/pinry \
  --run-root /volume1/docker \
  --run-id 20260828-01 \
  --image svrx-pinry:latest \
  --result-root /volume1/docker \
  --expected-pins 345 \
  --expected-files 1442 \
  --expected-images 346 \
  --expected-thumbnails 1038 \
  --expected-active-files 1384 \
  --expected-source-commit "${source_commit}"
```

성공하면 마지막 줄에 `NAS_LEGACY_CLONE_ACCEPTANCE_OK`가 표시되고
`svrx-pinry-accept-<run-id>.json`에 source·최종 수량, 실행 image identity,
소요 시간, 재시작 no-op과 백업 보존 결과가 기록된다. reference workload의
물리 1,442개에는 DB가 참조하지 않는 고아 파일 58개도 포함되며, 이 payload는
레거시 backup에 정확히 보존되어야 한다. DB snapshot은 무결성 검사와 핵심
row count(Pin 345, Image 346, Thumbnail 1,038)를 모두 통과해야 한다.

## 선택: 내보내기 런타임·PostgreSQL 검증

새 배포 이미지를 실제 프로젝트에 적용하기 전에 격리된 임시 데이터로
내보내기 런타임과 PostgreSQL 동시성 계약을 확인할 수 있다. NAS에
`accept-tools/`를 디렉터리째 업로드한 뒤, 산출물 최상위 디렉터리에서
다음을 실행한다.

```sh
bash accept-tools/docker/tests/export_runtime_smoke.sh svrx-pinry:latest 12
bash accept-tools/docker/tests/export_postgres_concurrency_smoke.sh svrx-pinry:latest
```

사용자 지정 태그로 빌드했다면 두 명령의 `svrx-pinry:latest`를 방금
빌드한 같은 이미지 태그로 바꿔야 한다.
첫 명령의 `12`는 테스트할 Pin 수이며 0~1000 사이로 지정할 수 있다.
두 스크립트는 고유한 임시 컨테이너·네트워크와 테스트 데이터를 사용하며,
성공 시 자신이 만든 임시 자원을 정리한다. PostgreSQL 검증은
`postgres:14-alpine` 이미지가 없으면 Docker Hub에서 받으므로 NAS의 외부
네트워크 접근이 필요하다.

## 서비스 워커 전환 context 업로드와 실행

기존 Pinry 또는 SVRx Pinry를 교체하며 같은 origin에 서비스 워커가
등록된 경우에는 NAS의 같은 작업 폴더에 main package와
`sw-transition/` 디렉터리를 **함께** 업로드한다. 압축 파일
`svrx-pinry-<커밋 앞 12자리 SHA>.tar.gz`에는 `svrx-pinry/`만 들어 있고
`sw-transition/`은 포함하지 않는다. 따라서 압축 파일로 main package를
옮겼더라도 `sw-transition/`은 디렉터리째 별도로 업로드해야 한다.

기존 main project를 중지해 2048 포트를 비운 뒤 DSM Container Manager에서
다음 순서로 전환 project를 실행한다. 이 project는 데이터 마운트를 사용하지
않으며 기존 데이터 폴더를 읽거나 변경하지 않는다.

1. **프로젝트 → 생성**을 선택한다.
2. 프로젝트 이름을 `svrx-pinry-sw-transition`으로 입력한다.
3. 경로에서 업로드한 `sw-transition/` 폴더를 선택한다.
4. 폴더에 포함된 `docker-compose.yml`을 사용해 project를 빌드·실행한다.
5. 컨테이너 상태가 실행 중인지 확인한다.

전환 화면은 기존 Pinry 또는 SVRx Pinry에서 서비스 워커를 등록한 외부 URL과
**scheme(HTTP/HTTPS), host, port가 모두 정확히 같은 origin**으로 열어야
한다. HTTPS 리버스 프록시나 사용자 도메인을 사용했다면 기존 외부
URL을 유지한 채 리버스 프록시를 임시로 전환 project에 연결한다.
NAS IP의 직접 URL이나 다른 포트를 열면 기존 origin의 서비스 워커와
캐시는 전환되지 않는다.

기존 서비스에서 평문 HTTP만 사용했고 서비스 워커가 등록된 적이 없다면
전환할 대상이 없으므로 이 전환 단계는 필요하지 않다. `127.0.0.1`과
`localhost`를 사용하는 loopback 자동화 테스트는 브라우저의 보안 origin
예외에서 전환 스크립트만 확인하며 운영 HTTPS 설정을 검증하지 않는다.

기존에 사용하던 모든 브라우저 프로필에서 이 기존 외부 URL을 한 번 열어
전환 화면의 **전환 준비 완료**를 확인한다. 모든 프로필에서 이 문구를
확인한 뒤 Container Manager에서 전환 project를 중지한다. 이어서 main
package의 본 이미지를 빌드하고 기존 Compose project를 다시 실행한다.

터미널 사용이 가능한 관리자는 같은 작업을 아래 명령으로 수행할 수도 있다.
이는 선택 절차이며 서비스 워커 전환에 SSH 접속은 필수 조건이 아니다.

```sh
cd sw-transition
docker build -f Dockerfile.sw-transition -t svrx-pinry-sw-transition:latest .
docker run --rm --name svrx-pinry-sw-transition -p 2048:80 svrx-pinry-sw-transition:latest
```

```sh
cd ../svrx-pinry
chmod +x build-image.sh
./build-image.sh
docker compose up -d --force-recreate
```

## 준비 사항

- DSM의 Container Manager가 설치되어 있어야 한다.
- NAS가 Docker Hub, Debian 저장소, Python 및 Node 패키지 저장소에
  접근할 수 있어야 한다.
- 기존 데이터를 이관한다면 기존 컨테이너를 먼저 중지하고, 데이터 폴더
  전체를 NAS 밖의 다른 저장 장치에도 백업할 것을 권장한다.

이미지 빌드에는 NAS에서 Docker 명령을 실행할 수 있는 환경이 필요하지만,
기존 데이터의 판별·DB snapshot·미디어 이관·보관에는 별도 SSH 명령이나
Django 관리 명령이 필요하지 않다. Compose를 시작하면 고정된 이관 command가
모든 단계를 자동으로 수행한다.

## 업로드와 압축 해제

`svrx-pinry/` 폴더 자체를 DSM File Station으로 업로드하면 압축 해제하지
않아도 된다. 압축 파일을 업로드했다면 작업 폴더에서 다음과 같이 푼다.

```sh
tar -xzf svrx-pinry-<커밋>.tar.gz
cd svrx-pinry
chmod +x build-image.sh
```

## 이미지 빌드

기본 이미지 이름은 `BUILD_INFO`에 기록된 `svrx-pinry:latest`이다.

```sh
./build-image.sh
```

이미지 이름과 태그를 직접 지정할 수도 있다.

```sh
./build-image.sh registry.local/svrx-pinry:nas
```

사용자 지정 태그로 빌드했다면 `docker-compose.yml`의 `image` 값도 같은
태그로 직접 변경한다. Docker 소켓 권한이 없으면 관리자 권한으로 실행한다.

빌드가 끝나면 생성된 이미지를 확인한다.

```sh
docker image ls svrx-pinry
```

`BUILD_INFO`의 `source_commit`은 이 이미지를 만든 40자리 Git 커밋이다.
빌드 스크립트는 같은 값을 이미지의 `PINRY_SOURCE_COMMIT` 환경값과
`org.opencontainers.image.revision` 레이블에 넣는다.

## Compose 설정

이미지, 컨테이너 이름, 포트, 데이터 경로와 자동 이관 command는
`docker-compose.yml`에 직접 기록되어 있다.

- 이미지: `svrx-pinry:latest`
- 서비스와 컨테이너 이름: `svrx-pinry`
- 접속 포트: `2048`
- 데이터 경로: `/volume1/docker/svrx-pinry/data`
- 시작 command: `/pinry/docker/scripts/start.sh --migrate-legacy`

값을 바꿔야 한다면 Compose 파일의 `image`, `container_name`, `ports`,
`volumes` 항목만 직접 수정한다. 자동 이관 command는 제거하지 않는다.
별도의 `.env` 파일은 사용하지 않는다.

새로 설치하는 경우 Container Manager 또는 File Station에서
`/volume1/docker/svrx-pinry/data` 폴더를 만든다. 최초 시작 시 이 폴더에
SQLite 데이터베이스, 이미지, 정적 파일, `local_settings.py`와 비밀키가
생성된다. 컨테이너를 교체해도 이 폴더를 삭제하지 않으면 데이터가 유지된다.

## Pin·보드 원본 내보내기 운영

로그인한 사용자는 자신이 소유한 보드 또는 선택한 Pin에서 **미리보기 →
내보내기**를 실행하고, 이후 **내 메뉴 → 내보내기**에서 상태와 완료 파일을
확인한다. 브라우저를 닫아도 작업은 계속된다. Container Manager가 컨테이너를
정상 중지한 뒤 다시 시작하면 최근 확정된 스냅숏과 시도 지점에서 재개하며,
생성 중인 부분 ZIP은 다운로드 대상으로 공개하지 않는다.

내보내기를 확정하면 그 시점의 대상 Pin 목록을 고정하므로 이후 보드에 새로
추가한 Pin은 진행 중인 ZIP에 포함되지 않는다. 대상 Pin의 설명·태그와 원본
파일은 백그라운드 작업자가 안전한 스냅숏을 만들 때 확정된다. 이미 복제한
항목은 원본 Pin이 삭제되어도 스냅숏 시점의 내용으로 계속 처리한다. 스냅숏
전이나 생성 중에 본인 Pin의 원본이 사라지거나 바뀌어 일관성을 보장할 수
없으면 부분 결과 없이 해당 작업을 실패로 표시한다.

완료 ZIP은 완료 시각부터 **최대 24시간** 다운로드할 수 있다. 24시간은
실행 주기나 일일 횟수 제한이 아니므로 진행 중인 작업이 없으면 새 내보내기를
시작할 수 있다. 새 내보내기가 성공하면 이전 성공 ZIP을 즉시 교체하여 더는
다운로드할 수 없고, 새 작업이 실패하면 이전 성공 ZIP은 원래 만료 시각까지
유지된다. 만료 ZIP과 오래된 작업 파일은 작업자가 보관 정책에 따라 정리한다.

ZIP에는 Pin마다 원본 이미지와 XMP 사이드카가 한 쌍씩 들어가며 Pin
메타데이터는 `manifest.json`에도 기록된다. 원본 이미지 바이트와 이미지에
이미 들어 있던 EXIF·IPTC·XMP는 변경하지 않는다. Pin 등록일
(`Pin.published`)·설명·태그는 XMP와 매니페스트에 보존하고, ZIP 이미지와 XMP
엔트리의 수정 시각도 ZIP 형식이 표현할 수 있는 범위에서 Pin 등록일을 쓴다.
정확한 UTC 등록일의 권위 값은 XMP와 `manifest.json`이다.

ZIP의 `originals/`는 UUID별 하위 폴더가 없는 단일 폴더다. 가능한 경우 원본
파일명을 유지하고 운영체제에서 사용할 수 없는 문자는 안전하게 정규화한다.
대소문자를 구분하지 않았을 때 같은 파일명이 겹치면 확장자 앞에
`__<짧은 식별자>`를 붙여 덮어쓰기를 막는다. 같은 원본 이미지를 공유하는
Pin도 각각 이미지·XMP 한 쌍을 가지며, 파일과 Pin의 정확한 연결은
`manifest.json`을 기준으로 확인한다.

스냅숏과 최종 게시 직전 권한 검사에서 삭제되었거나 비공개로 바뀐 타인의
Pin은 안전하게 제외한다. 본인 Pin의 원본이 소실되었거나 바뀌어 안전한
스냅숏을 만들 수 없으면 부분 ZIP 없이 작업 전체를 실패로 표시한다. 계정마다
한 번에 하나의 작업을 요청할 수 있고 서버는 ZIP 생성 작업을 하나씩 순서대로
처리한다. 화면의 **작업자 대기 중**은 작업자가 시작하는 중이거나 앞선 작업의
완료를 기다리는 상태이지 서비스 장애를 뜻하지 않는다. 저장소 오류·용량
부족·만료가 표시되면 화면의 재시도 가능 여부를 먼저 확인하고 DSM
**Container Manager → 컨테이너 → svrx-pinry → 로그**에서 오류 코드를
확인한다. 로그나 문의 자료에 사용자 설명·태그, 쿠키·토큰, ZIP 본문을
복사하지 않는다.

내보내기에는 예상 ZIP 크기 외에 스냅숏과 안전한 게시를 위한 여유 공간이
필요하다. `/data/exports`와 하위 `.staging`, `ready`를 수동 삭제하거나
소유자·권한을 변경하지 않는다. 이 경로는 Compose의 데이터 bind mount
안에 있으므로 데이터 폴더 백업에 포함한다. 배포용 Synology 산출물과 Docker
build context에는 데이터베이스, 업로드 미디어, export ZIP, 사용자 계정,
비밀키가 포함되지 않는다.

### 기존 설치의 데이터 경로 준비

기존 컨테이너가 같은 2048 포트를 사용 중인 상태에서 새 컨테이너를
시작하지 않는다. Container Manager에서 기존 project 또는 container를
먼저 중지한다.

기존 Compose 파일에서 컨테이너의 `/data`로 바인드된 실제 NAS
경로를 먼저 확인한다. 기본 Pinry 예시 경로는
`/volume1/docker/pinry/data`이지만 기존 project 설정에 따라 다를 수 있다.
확인한 기존 경로를 계속 바인드 마운트해도 되고, File Station에서
그 폴더의 **내용 전체**를 `/volume1/docker/svrx-pinry/data`로
복사해도 된다. 새 경로로 복사할 때
`data` 폴더 자체를 넣어 `data/data`가 되지 않도록 주의한다. 이 작업 전에
중지된 기존 데이터 폴더 전체를 NAS 밖에도 백업한다.

File Station이 복사한 `local_settings.py`와 비밀키는 `data` 폴더와 소유자가
다르거나 권한 표시가 달라도 시작할 때 컨테이너 root 소유 `0600`으로 자동
정규화된다. 심볼릭 링크·하드 링크·일반 파일이 아닌 항목과 잘못된 내용은
원본을 바꾸지 않고 거부한다.

### 기존 Pinry 데이터 최초 이전

기존 Pinry 또는 SVRx Pinry에서 같은 origin에 서비스 워커가 등록된
경우에는 먼저 서비스 워커 전환 이미지를 같은 origin에 실행하고,
기존에 사용하던 브라우저 프로필로
**전환 준비 완료**를 확인한다. 이 단계는 데이터 폴더를 열지 않는다.

기존 데이터가 있으면 첫 실행 동안 같은 주소에 이전 진행 화면이
표시된다. 브라우저를 닫아도 작업은 계속되며, 완료 전에는 컨테이너를
중지하거나 project·데이터 폴더를 삭제하지 않는다. 정상 중지 후 다시
시작하면 마지막 확정 배치부터 이어서 작업한다. 오류 화면이 표시되면
오류 코드를 복사하고 DSM Container Manager의 컨테이너 로그를 확인한다.
완료 후 생성된 레거시 백업은 충분히 검증한 뒤 사용자가 직접 삭제할
때까지 보존된다.

검증 목표는 동일 NAS에서 **350개 40분 이내**, **1,000개 2시간 이내**다.
이 시간은 검증 기준이며 모든 장비에서의 보장값이 아니다.
스토리지·CPU·이미지 크기에 따라 소요 시간이 달라질 수 있다.

| 경로 | 의미 |
| --- | --- |
| `/migration/` | 한국어 이전 진행·정체·오류 화면 |
| `/migration-status.json` | 비밀값을 제외한 공개 진행 상태 |
| `/healthz` | Nginx 프로세스가 응답하는지 확인 |
| `/readyz` | 일반 SVRx Pinry 서비스가 준비됐는지 확인 |

이전 중에는 일반 API와 미디어 요청에 `503` 응답과 재시도 안내가
반환된다. 오류 화면은 **재시도 가능**, **사용자 조치 필요**, **치명적**
세 분류로만 안내한다. 내부 traceback, 절대 경로, 원본 파일명은 공개
상태나 화면에 표시하지 않는다.

## 자동 이관과 서버 시작

Container Manager에서 `docker-compose.yml`로 project를 생성하거나 기존
project를 다시 빌드한 이미지로 재생성한다. 명령줄을 사용한다면 다음과
같다.

```sh
docker compose up -d --force-recreate
```

구형 DSM에서는 `docker-compose` 명령을 사용할 수 있다. 이 명령은 사용자가
매번 편집할 이관용 옵션이 아니다. Compose 파일에 고정된 command가 시작할
때마다 실행되며, 이관할 증거가 없거나 이전 이관이 이미 완료되었으면
멱등적인 no-op으로 끝난 뒤 정상 서비스를 시작한다.

이전 시험 이미지가 archive root identity 또는 완료 진행표를 남기지 못한
prefixed·direct MD5 상태는 정상 폴더와 교체된 폴더를 안전하게 구분할 수
없으므로 `archive_manifest_mismatch` 또는 `archive_state_conflict`로 중단한다.
root archive가 없고 파일 identity를 검증할 수 있는 fixed-slot-only 완료 상태는
안전하게 재개한다. 현재 산출물은 필요한 정보를 archive 전에 기록하므로 새
이관에서는 같은 불완전 상태를 만들지 않는다.

기존 미디어 증거 또는 안전한 빠른 경로로 분류되지 않은
pending schema가 발견되면 다음 순서로 진행한다.

1. 컨테이너 수명 전체의 startup lock과 저장소 설정을 검증한다.
2. 기존 DB·미디어 증거를 읽고 run을 만들거나 중단된 같은 run을 재개한다.
3. schema 변경 전에 SQLite online snapshot을 만든 뒤 무결성을 확인한다.
4. 정적 파일 수집과 DB schema migration을 수행한다.
5. 기존 MD5·fixed-slot 경로를 현재 UUID·원본 파일명 경로로 이관한다.
6. 안전하게 소유 관계를 판별할 수 있는 파일을 `MediaAsset`에 등록한다.
7. 모든 참조와 manifest를 검증한 뒤 기존 미디어를 backup 아래로 원자
   이동한다.
8. service 계정의 실제 읽기·쓰기 검사를 통과한 뒤 nginx와 Gunicorn을
   시작한다.

미디어와 무관하다고 명시적으로 검증된 schema migration은 이 절차의
전체 미디어 검증을 시작하지 않고 표준 Django migration으로
처리한다. 현재 빠른 경로의 정확한 허용 목록은
`django_images.0007_startup_validation_state`,
`users.0002_admin_bootstrap`, `exports.0001_initial`,
`exports.0002_exporttarget_identity_snapshot`이다. 첫 시작에서 전체 검증과
실행 계정의 저장소 검사를 통과하면 DB에 검증 계약 버전을
기록한다. 이후 스키마가 같은 재시작은 핀과 미디어 전체를 다시
순회하지 않고 표식과 실제 쓰기·lock probe만 검사한다. 완료되지
않은 기존 run이 있거나 다른 migration이 함께
남아 있으면 기존 안전 절차를 그대로 재개한다.

어느 단계든 실패하면 app 서버는 시작되지 않는다. 로그에는 비밀값이나
원본 파일명 대신 `legacy_migration_space_insufficient`,
`media_storage_configuration_invalid`, `migration_state_plan_mismatch`,
`archive_state_conflict`, `atomic_archive_unsupported` 같은 reason code만
남는다. 이관 증거 검사에서 중단되면 다음처럼 검사 단계별 reason code가
표시된다.

- `legacy_database_invalid`: 기존 DB 파일의 형식·위치·identity가 안전하지 않다.
- `legacy_database_schema_invalid`: 기존 SQLite DB를 읽을 수 없거나 필요한
  테이블 구조가 유효하지 않다.
- `legacy_media_root_invalid`: 미디어 root가 디렉터리가 아니거나 안전하게
  고정할 수 없는 항목이 있다.
- `legacy_media_rows_invalid`: 기존 Image·Thumbnail row의 미디어 경로가
  허용된 레거시 또는 현재 형식이 아니다.
- `legacy_media_files_invalid`: DB가 참조하는 레거시 파일을 안전하게 읽을 수
  없다.
- `legacy_migration_graph_invalid`: 이미지에 포함된 Django migration graph를
  안전하게 검사할 수 없다.

완전히 빈 신규 `data` 폴더에서는 이관 검사가 no-op으로 끝나야 한다. 위
코드가 표시되면 신규 설치로 생각한 폴더에도 기존 DB·미디어·불완전한 복사
항목이 남아 있는 것이므로 해당 코드에 맞는 항목을 확인한다.

`bootstrap_persistent_settings_invalid`는 `data` 폴더에 보존된 설정
또는 비밀키가 심볼릭 링크·하드 링크·일반 파일이 아닌 형식이거나 내용 검증에
실패했다는 뜻이다. File Station으로 복사하면서 달라진 소유자와 권한은 시작할
때 컨테이너 root 소유 `0600`으로 자동 정규화된다. 따라서 `data` 공유 폴더는
관리자만 쓸 수 있게 유지하고, 복사 중에는 기존 컨테이너를 정지해야 한다.
`bootstrap_project_settings_invalid`는 컨테이너 내부 설정 복사본을 안전한
권한으로 만들지 못했다는 뜻이다. 원인을 해결하고 같은 Compose project를
다시 시작하면 새 run을 만들지 않고 기록된 snapshot·state·manifest에서
재개한다.

## 이관 backup 구조와 성공 확인

이관 run은 기본 데이터 폴더의 `legacy-backup/<run-id>/` 아래에 남는다.
항목이 없었던 단계의 파일이나 폴더는 생성되지 않을 수 있다.

```text
/data/legacy-backup/<run-id>/
├── migration-state.json
├── production.db.before-migration  # 기존 DB가 있을 때
├── media-migration.jsonl
├── media-asset-backfill.jsonl
├── migration-summary.json
└── media/
    ├── 0/ ... f/  # 실제 존재했던 Pinry MD5 최상위 폴더
    ├── image/  # 이전 호환 MD5 경로가 있었을 때만
    └── fixed-slot-originals/
        └── originals/<uuid>/original.<ext>
```

다음 항목을 모두 확인한다.

1. Container Manager에서 `svrx-pinry` container 상태가 실행 중이다.
2. 브라우저에서 `http://NAS주소:2048`에 접속되고 기존 Pin 이미지와 새 Pin
   생성·삭제가 정상이다.
3. File Station에서 최신 run의 `migration-state.json`과
   `migration-summary.json`의 `phase`가 `complete`이다.
4. 컨테이너를 다시 시작해도 새 backup run이 추가되지 않고 정상 시작한다.

상세 manifest에는 로컬 이미지 경로와 식별 정보가 포함될 수 있으므로
외부에 공개하지 않는다. 이관기는 snapshot, manifest, canonical copy와
backup을 자동 삭제하지 않는다. 충분히 검증한 뒤 불필요하다고 판단한
backup은 사용자가 직접 삭제한다.

## 수동 롤백

롤백은 자동 버튼으로 제공하지 않는다. 다음 절차는 이관 전 상태로 돌아갈
필요가 있을 때만 수행한다.

1. Container Manager에서 SVRx Pinry project를 중지한다.
2. 현재 `/volume1/docker/svrx-pinry/data` 전체를 다른 위치에 한 번 더
   복사한다. 이 사본은 이관 후 변경분을 보존하기 위한 것이다.
3. 되돌릴 `<run-id>`를 고르고 `production.db.before-migration`을
   File Station의 `/volume1/docker/svrx-pinry/data/production.db`
   (컨테이너 내부 `/data/production.db`) 위치로 복원한다. 현재 DB는 바로
   덮어쓰지 말고 별도 이름이나 폴더로 보관한다.
4. backup의 `media/0/`부터 `media/f/` 중 존재하는 폴더를 각각 원래
   `/volume1/docker/svrx-pinry/data/static/media/0/`부터
   `/volume1/docker/svrx-pinry/data/static/media/f/`의 같은 이름 위치로
   복원한다. 이것이 실제 Pinry의 MD5 미로형 폴더 구조이다.
5. backup의 `media/image/`가 있으면 이전 호환 경로이므로 원래
   `/volume1/docker/svrx-pinry/data/static/media/image/` 위치로 폴더 구조와
   함께 복원한다.
6. backup의 `media/fixed-slot-originals/originals/`가 있으면 그 아래 내용을
   원래 `/volume1/docker/svrx-pinry/data/static/media/originals/` 아래에 같은
   상대경로로 복원한다.
7. 기존 Pinry 이미지와 Compose를 다시 사용해 서비스를 시작한다. 현재
   SVRx Pinry Compose를 시작하면 자동 이관이 다시 실행된다.

서버가 정상 시작한 뒤 만든 신규 Pin과 수정 사항은 이관 전 DB snapshot에
없으므로 롤백하면 사라진다. 이관이 만든 canonical copy는 롤백 시 자동으로
삭제되지 않으며, 경로 충돌을 피하기 위해 임의로 일괄 삭제하지 않는다.
상세 manifest와 별도 backup을 확인한 뒤 필요한 파일만 사용자가 정리한다.

## 실행 버전 확인

서비스가 시작된 뒤 다음 API에서 실행 중인 프로세스의 full SHA를 확인한다.

```sh
curl -s http://NAS주소:2048/api/v2/version/
```

응답의 `source_commit`은 `BUILD_INFO`와 이미지 레이블의 40자리 SHA와
같아야 한다. 로그인 후 **내 메뉴 → 프로필 → 빌드 정보**에 보이는 12자리
값도 같은 SHA의 앞 12자와 일치해야 한다. 값이 다르면 이전 산출물을 다시
빌드했거나 `latest` 이미지로 컨테이너를 교체하지 않은 것이다.

## 빌드 실패 참고

`DEPRECATED: The legacy builder is deprecated`는 경고이며 빌드 실패 원인이
아니다. 현재 산출물은 Python 3.9의 Debian Bookworm 이미지를 사용한다.

컨테이너를 다시 만들기 전에 데이터 폴더를 별도 위치에 백업한다.
`docker compose down`은 바인드 마운트 데이터 폴더를 삭제하지 않지만,
사용자가 NAS에서 해당 폴더를 직접 지우면 복구할 수 없다.
