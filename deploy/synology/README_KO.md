# 시놀로지 이미지 빌드 및 실행 안내

이 문서는 SVRx Pinry Docker 이미지를 시놀로지 NAS에서 직접 빌드하고
Compose로 실행하는 방법을 설명한다. 데이터베이스, 업로드 이미지, 비밀키와
기존 컨테이너 데이터는 산출물에 포함하지 않는다.

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
`build-image.sh` 또는 Compose 파일에 tracked 미커밋 변경이 있으면 서로
다른 revision이 섞이지 않도록 생성 전에 중단한다. 테스트, 일반 문서,
GitHub 설정과 개발용 파일은 포함하지 않는다. 법적 고지 파일인
`LICENSE.md`, `NOTICE.md`, `UPSTREAM.md`는 이미지에 포함한다. 같은 커밋의
기존 기본 산출물이 있으면 덮어쓰지 않고 중단한다.

산출물 구조는 다음과 같다.

```text
svrx-pinry/
├── BUILD_INFO
├── build-image.sh
├── docker-compose.yml
└── context/
    ├── Dockerfile.autobuild
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

Docker는 `context/`만 build context로 사용한다. 바깥의 `BUILD_INFO`,
`build-image.sh`와 `docker-compose.yml`은 최종 이미지에 복사되지 않는다.

## 준비 사항

- DSM의 Container Manager가 설치되어 있어야 한다.
- SSH에서 현재 사용자가 `docker` 명령을 실행할 수 있어야 한다. 권한이
  없다면 관리자 권한으로 아래 명령을 실행한다.
- NAS가 Docker Hub, Debian 저장소, Python 및 Node 패키지 저장소에
  접근할 수 있어야 한다.

## 업로드와 압축 해제

개발 PC에서 생성한 `svrx-pinry-<커밋>.tar.gz`를 NAS 작업 폴더로
업로드한다.

```sh
tar -xzf svrx-pinry-<커밋>.tar.gz
cd svrx-pinry
chmod +x build-image.sh
```

폴더 자체를 File Station이나 `scp`로 업로드했다면 압축 해제 단계는
생략한다.

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
태그로 직접 변경한다.

Docker 소켓 권한이 없는 환경에서는 다음과 같이 실행한다.

```sh
sudo ./build-image.sh
```

빌드가 끝나면 생성된 이미지를 확인한다.

```sh
docker image ls svrx-pinry
```

`BUILD_INFO`의 `source_commit`은 이 이미지를 만든 40자리 Git 커밋이다.
빌드 스크립트는 같은 값을 이미지의 `PINRY_SOURCE_COMMIT` 환경값과
`org.opencontainers.image.revision` 레이블에 넣는다. 다음 세 명령이 모두
같은 full SHA를 가리켜야 한다.

```sh
sed -n 's/^source_commit=//p' BUILD_INFO
docker image inspect svrx-pinry:latest \
  --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}'
docker image inspect svrx-pinry:latest \
  --format '{{range .Config.Env}}{{println .}}{{end}}' \
  | grep '^PINRY_SOURCE_COMMIT='
```

## Compose 설정

이미지, 컨테이너 이름, 포트와 데이터 경로는 `docker-compose.yml`에 직접
기록되어 있다.

- 이미지: `svrx-pinry:latest`
- 서비스와 컨테이너 이름: `svrx-pinry`
- 접속 포트: `2048`
- 데이터 경로: `/volume1/docker/svrx-pinry/data`

값을 바꿔야 한다면 Compose 파일의 `image`, `container_name`, `ports`,
`volumes` 항목을 직접 수정한다. 별도의 `.env` 파일은 사용하지 않는다.

새로 설치하는 경우 데이터 폴더를 만든다.

```sh
mkdir -p /volume1/docker/svrx-pinry/data
```

SVRx Pinry는 최초 실행 시 이 폴더에 SQLite 데이터베이스, 이미지, 정적
파일, `local_settings.py`와 비밀키를 생성한다. 컨테이너를 교체해도 이
폴더를 삭제하지 않으면 데이터가 유지된다.

### 기존 `pinry-custom` 설치에서 전환

기존 기본 경로 `/volume1/docker/pinry-custom/data`의 데이터는 자동으로
이동하지 않는다. 기존 컨테이너가 2048 포트를 사용 중인 상태에서 새
컨테이너를 시작해서도 안 된다. 먼저 기존 서비스를 중지한다.

```sh
docker stop pinry-custom
```

SQLite 데이터의 일관성을 위해 반드시 중지한 상태에서 기존 `data` 폴더를
별도 위치에 백업한다. 아래 `data-backup`은 존재하지 않는 경로여야 한다.

```sh
cp -a /volume1/docker/pinry-custom/data \
  /volume1/docker/pinry-custom/data-backup
```

새 기본 경로를 사용할 경우 `data` 디렉터리 자체가 아니라 그 안의 내용을
복사한다. 이렇게 해야 `data/data`로 중첩되지 않는다.

```sh
mkdir -p /volume1/docker/svrx-pinry/data
cp -a /volume1/docker/pinry-custom/data/. \
  /volume1/docker/svrx-pinry/data/
```

복사된 내용을 확인한 뒤 기존 컨테이너를 제거한다. 바인드 마운트된 호스트의
`data` 폴더와 위 백업은 이 명령으로 삭제되지 않는다.

```sh
docker rm pinry-custom
```

새 경로로 복사하지 않고 기존 데이터를 그대로 사용할 수도 있다. 이 경우
Compose의 `volumes` 호스트 경로를 `/volume1/docker/pinry-custom/data`로
바꾸되, 기존 컨테이너의 중지와 제거는 동일하게 진행한다.

## Compose 실행

DSM Container Manager의 Compose 플러그인을 사용하는 경우 다음 명령으로
실행한다.

```sh
docker compose up -d
```

같은 `latest` 태그로 이미지를 다시 빌드한 뒤에는 새 이미지로 컨테이너를
확실히 교체한다.

```sh
docker compose up -d --force-recreate
```

구형 DSM에서 명령이 없으면 다음 형식을 사용한다.

```sh
docker-compose up -d --force-recreate
```

컨테이너를 교체한 뒤 브라우저를 강제 새로고침한다. 이전 UI가 계속 보이면
해당 사이트의 저장 데이터를 지우고 서비스 워커를 제거한 뒤 다시 접속한다.

상태와 로그는 다음 명령으로 확인한다.

```sh
docker compose ps
docker compose logs --tail=100 svrx-pinry
```

기본 접속 주소는 `http://NAS주소:2048`이다.

## 실행 버전과 마이그레이션 확인

컨테이너는 데이터베이스가 이미 있는지와 관계없이 매번 시작할 때
`python manage.py migrate --noinput`을 한 번 실행한다. 마이그레이션이
실패하면 nginx와 Gunicorn을 시작하지 않고 컨테이너가 종료된다.
`restart: unless-stopped` 정책으로 재시도가 반복될 수 있으므로 서비스가
올라오지 않으면 먼저 Compose 로그의 마이그레이션 오류를 확인한다.

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
아니다. 이전 산출물의 실제 실패 원인은 Debian Buster 저장소 종료에 따른
`apt-get update` 404였다. 현재 산출물은 Python 3.9의 Debian Bookworm
이미지를 사용하고 Bookworm 패키지명에 맞게 수정되어 있다.

컨테이너를 다시 만들기 전에 `/volume1/docker/svrx-pinry/data`를 별도
위치에 백업한다. `docker compose down`은 바인드 마운트 데이터 폴더를
삭제하지 않지만, 사용자가 NAS에서 해당 폴더를 직접 지우면 복구할 수 없다.
