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
파일이 생성된다. linked worktree에서 실행해도 같은 workspace의 공통
`output/` 경로를 사용한다.

다른 위치가 필요하면 경로를 직접 지정한다.

```sh
./scripts/create_synology_output.sh /원하는/산출물/경로
```

패키지는 시작 시점의 Git `HEAD` 커밋 하나에서만 만든다. 생성기,
`build-image.sh`, Compose, 이 안내 문서 또는 법적 고지 파일에 tracked
미커밋 변경이 있으면 서로 다른 revision이 섞이지 않도록 생성 전에
중단한다. 테스트, 설계 문서, GitHub 설정과 개발용 파일은 포함하지 않는다.
같은 커밋의 기존 기본 산출물이 있으면 덮어쓰지 않고 중단한다.

산출물과 압축 파일 내부의 최상위 구조는 모두 다음과 같다.

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

### 기존 설치의 데이터 경로 준비

기존 컨테이너가 같은 2048 포트를 사용 중인 상태에서 새 컨테이너를
시작하지 않는다. Container Manager에서 기존 project 또는 container를
먼저 중지한다.

기존 `/volume1/docker/pinry-custom/data`를 계속 바인드 마운트해도 되고,
File Station에서 그 폴더의 **내용 전체**를
`/volume1/docker/svrx-pinry/data`로 복사해도 된다. 새 경로로 복사할 때
`data` 폴더 자체를 넣어 `data/data`가 되지 않도록 주의한다. 이 작업 전에
중지된 기존 데이터 폴더 전체를 NAS 밖에도 백업한다.

File Station이 복사한 `local_settings.py`와 비밀키는 `data` 폴더와 소유자가
다르거나 권한 표시가 달라도 시작할 때 컨테이너 root 소유 `0600`으로 자동
정규화된다. 심볼릭 링크·하드 링크·일반 파일이 아닌 항목과 잘못된 내용은
원본을 바꾸지 않고 거부한다.

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

기존 데이터 또는 pending schema가 발견되면 다음 순서로 진행한다.

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

어느 단계든 실패하면 app 서버는 시작되지 않는다. 로그에는 비밀값이나
원본 파일명 대신 `legacy_migration_space_insufficient`,
`media_storage_configuration_invalid`, `migration_state_plan_mismatch`,
`archive_state_conflict`, `atomic_archive_unsupported` 같은 reason code만
남는다. `bootstrap_persistent_settings_invalid`는 `data` 폴더에 보존된 설정
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
    ├── image/  # 기존 MD5 미디어 폴더가 있을 때
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
4. backup의 `media/image/`가 있으면 원래
   `/volume1/docker/svrx-pinry/data/static/media/image/` 위치로 폴더 구조와
   함께 복원한다.
5. backup의 `media/fixed-slot-originals/originals/`가 있으면 그 아래 내용을
   원래 `/volume1/docker/svrx-pinry/data/static/media/originals/` 아래에 같은
   상대경로로 복원한다.
6. 기존 Pinry 이미지와 Compose를 다시 사용해 서비스를 시작한다. 현재
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
