# 시놀로지 이미지 빌드 및 실행 안내

이 문서는 Pinry Custom Docker 이미지를 시놀로지 NAS에서 직접 빌드하고 Compose로 실행하는 방법을 설명한다. 데이터베이스, 업로드 이미지, 비밀키와 기존 컨테이너 데이터는 산출물에 포함하지 않는다.

## 개발 PC에서 산출물 생성

저장소 루트에서 다음 명령을 실행한다.

```sh
./scripts/create_synology_output.sh
```

`output/svrx-pinry-server-<커밋 앞 12자리 SHA>/` 아래에 업로드 가능한 `pinry-custom/` 폴더와 `pinry-custom-<커밋 앞 12자리 SHA>.tar.gz` 파일이 생성된다. 이 경로는 canonical checkout과 linked worktree가 공통으로 사용하므로 같은 커밋의 산출물이 이미 있으면 덮어쓰지 않고 중단한다. 다른 위치가 필요하면 `./scripts/create_synology_output.sh /원하는/산출물/경로`처럼 경로를 명시한다. 패키지는 시작 시점의 Git `HEAD` 커밋 하나에서만 만든다. 생성기, `build-image.sh`, Compose 파일 또는 `.env.example`에 tracked 미커밋 변경이 있으면 서로 다른 revision이 섞이지 않도록 산출물을 만들기 전에 중단한다. 테스트, 일반 Markdown·문서 파일, GitHub 설정과 개발용 파일은 포함하지 않는다. 단, 법적 고지 파일인 `LICENSE.md`, `NOTICE.md`, `UPSTREAM.md`는 예외로 포함한다. 기존 산출물이 있으면 덮어쓰지 않고 중단한다.

산출물 구조는 다음과 같다.

```text
pinry-custom/
├── .env.example
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

Docker는 `context/`만 build context로 사용한다. 바깥의 `.env.example`, `BUILD_INFO`, `build-image.sh`와 `docker-compose.yml`은 최종 이미지에 복사되지 않는다. 실제 `.env` 파일은 산출물과 Git에 포함하지 않는다.

## 준비 사항

- DSM의 Container Manager가 설치되어 있어야 한다.
- SSH에서 현재 사용자가 `docker` 명령을 실행할 수 있어야 한다. 권한이 없다면 관리자 권한으로 아래 명령을 실행한다.
- NAS가 Docker Hub, Debian 저장소, Python 및 Node 패키지 저장소에 접근할 수 있어야 한다.

## 업로드와 압축 해제

개발 PC에서 생성된 `pinry-custom-<커밋>.tar.gz`를 NAS의 작업 폴더로 업로드한다.

```sh
tar -xzf pinry-custom-<커밋>.tar.gz
cd pinry-custom
chmod +x build-image.sh
```

폴더 자체를 File Station이나 `scp`로 업로드했다면 압축 해제 단계는 생략한다.

## 이미지 빌드

기본 이미지 이름은 `BUILD_INFO`에 기록된 `pinry-custom:latest`이다.

```sh
./build-image.sh
```

이미지 이름과 태그를 직접 지정할 수도 있다.

```sh
./build-image.sh registry.local/pinry-custom:nas
```

기본 태그를 사용하면 `.env`는 처음 한 번만 `pinry-custom:latest`로 설정하고 이후 빌드에서는 교체하지 않는다. 기존 `.env`가 `pinry-custom:<커밋>`을 가리키고 있다면 이번 한 번만 `pinry-custom:latest`로 변경한다.

사용자 지정 태그로 빌드한 경우에만 아래 환경설정 단계에서 `.env`의 `PINRY_IMAGE`도 같은 값으로 변경한다.

Docker 소켓 권한이 없는 환경에서는 다음과 같이 실행한다.

```sh
sudo ./build-image.sh
```

빌드가 끝나면 생성된 이미지를 확인한다.

```sh
docker image ls pinry-custom
```

`BUILD_INFO`의 `source_commit`은 이 이미지를 만든 40자리 Git 커밋이다. 빌드 스크립트는 같은 값을 이미지의 `PINRY_SOURCE_COMMIT` 환경값과 `org.opencontainers.image.revision` 레이블에 넣는다. 다음 세 명령이 모두 같은 full SHA를 가리켜야 한다.

```sh
sed -n 's/^source_commit=//p' BUILD_INFO
docker image inspect pinry-custom:latest \
  --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}'
docker image inspect pinry-custom:latest \
  --format '{{range .Config.Env}}{{println .}}{{end}}' \
  | grep '^PINRY_SOURCE_COMMIT='
```

## 환경설정 준비

예제 파일을 실제 Compose 환경 파일로 복사한다.

```sh
cp .env.example .env
```

기본값은 다음과 같다.

- 이미지: `pinry-custom:latest`
- 컨테이너 이름: `pinry-custom`
- 접속 포트: `2048`
- 데이터 경로: `/volume1/docker/pinry-custom/data`

공유 폴더나 외부 포트를 다르게 사용할 경우 `.env`만 수정한다. 실제 `.env`는 NAS에만 보관하고 GitHub나 다른 사용자에게 전달하지 않는다.

데이터 폴더를 만든다.

```sh
mkdir -p /volume1/docker/pinry-custom/data
```

Pinry는 최초 실행 시 이 폴더에 SQLite 데이터베이스, 이미지, 정적 파일, `local_settings.py`와 비밀키를 생성한다. 컨테이너를 교체해도 이 폴더를 삭제하지 않으면 데이터가 유지된다.

## Compose 실행

DSM Container Manager의 Compose 플러그인을 사용하는 경우 다음 명령으로 실행한다.

```sh
docker compose --env-file .env up -d
```

같은 `latest` 태그로 이미지를 다시 빌드한 뒤에는 새 이미지로 컨테이너를 확실히 교체한다.

```sh
docker compose --env-file .env up -d --force-recreate
```

구형 DSM에서 명령이 없으면 다음 형식을 사용한다.

```sh
docker-compose up -d --force-recreate
```

Compose v1은 같은 폴더의 `.env`를 자동으로 읽는다.

컨테이너를 교체한 뒤 브라우저를 강제 새로고침한다. 이전 휴지통 메뉴나 삭제 안내가 보이면 해당 사이트의 저장 데이터를 지우고 서비스 워커를 제거한 뒤 다시 접속한다. 새 삭제 확인 문구 `이 Pin을 삭제하시겠습니까?`가 보이는 것을 확인한 후 첫 삭제를 테스트한다.

상태와 로그는 다음 명령으로 확인한다.

```sh
docker compose ps
docker compose logs --tail=100 pinry
```

기본 접속 주소는 `http://NAS주소:2048`이다.

## 실행 버전과 마이그레이션 확인

컨테이너는 데이터베이스가 이미 있는지와 관계없이 매번 시작할 때 `python manage.py migrate --noinput`을 한 번 실행한다. 마이그레이션이 실패하면 nginx와 Gunicorn을 시작하지 않고 컨테이너가 종료된다. `restart: unless-stopped` 정책으로 재시도가 반복될 수 있으므로 서비스가 올라오지 않으면 먼저 Compose 로그의 마이그레이션 오류를 확인한다.

서비스가 시작된 뒤 다음 API에서 실행 중인 프로세스의 full SHA를 확인한다.

```sh
curl -s http://NAS주소:2048/api/v2/version/
```

응답의 `source_commit`은 `BUILD_INFO`와 이미지 레이블의 40자리 SHA와 같아야 한다. 로그인 후 상단의 **내 메뉴**에서 **프로필**을 열어 **빌드 정보**에 보이는 12자리 값도 같은 SHA의 앞 12자와 일치해야 한다. 네 값이 다르면 이전 산출물을 다시 빌드했거나 `latest` 이미지로 컨테이너를 교체하지 않은 것이다.

## 빌드 실패 참고

`DEPRECATED: The legacy builder is deprecated`는 경고이며 빌드 실패 원인이 아니다. 이전 산출물의 실제 실패 원인은 Debian Buster 저장소 종료로 인한 `apt-get update` 404였다. 현재 산출물은 Python 3.9의 Debian Bookworm 이미지를 사용하고 Bookworm 패키지명에 맞게 수정되어 있다.

컨테이너를 다시 만들기 전에 `/volume1/docker/pinry-custom/data`를 별도 위치에 백업한다. `docker compose down`은 바인드 마운트 데이터 폴더를 삭제하지 않지만, 사용자가 NAS에서 해당 폴더를 직접 지우면 복구할 수 없다.
