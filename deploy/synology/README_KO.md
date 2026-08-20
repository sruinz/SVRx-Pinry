# 시놀로지 이미지 빌드 및 실행 안내

이 문서는 Pinry Custom Docker 이미지를 시놀로지 NAS에서 직접 빌드하고 Compose로 실행하는 방법을 설명한다. 데이터베이스, 업로드 이미지, 비밀키와 기존 컨테이너 데이터는 산출물에 포함하지 않는다.

## 개발 PC에서 산출물 생성

저장소 루트에서 다음 명령을 실행한다.

```sh
./scripts/create_synology_output.sh
```

`output/synology/` 아래에 업로드 가능한 `pinry-custom/` 폴더와 `pinry-custom-<커밋>.tar.gz` 파일이 생성된다. 패키지는 현재 Git `HEAD`에 커밋된 런타임 파일만 포함하며 미커밋 변경, 테스트, Markdown 문서, GitHub 설정과 개발용 파일은 포함하지 않는다. 기존 산출물이 있으면 덮어쓰지 않고 중단한다.

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

기본 이미지 이름은 `BUILD_INFO`에 기록된 `pinry-custom:<커밋>`이다.

```sh
./build-image.sh
```

이미지 이름과 태그를 직접 지정할 수도 있다.

```sh
./build-image.sh registry.local/pinry-custom:nas
```

사용자 지정 태그로 빌드했다면 아래 환경설정 단계에서 `.env`의 `PINRY_IMAGE`도 같은 값으로 변경한다.

Docker 소켓 권한이 없는 환경에서는 다음과 같이 실행한다.

```sh
sudo ./build-image.sh
```

빌드가 끝나면 생성된 이미지를 확인한다.

```sh
docker image ls pinry-custom
```

## 환경설정 준비

예제 파일을 실제 Compose 환경 파일로 복사한다.

```sh
cp .env.example .env
```

기본값은 다음과 같다.

- 이미지: 이번 산출물의 커밋 태그
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

구형 DSM에서 명령이 없으면 다음 형식을 사용한다.

```sh
docker-compose --env-file .env up -d
```

상태와 로그는 다음 명령으로 확인한다.

```sh
docker compose ps
docker compose logs --tail=100 pinry
```

기본 접속 주소는 `http://NAS주소:2048`이다.

## 빌드 실패 참고

`DEPRECATED: The legacy builder is deprecated`는 경고이며 빌드 실패 원인이 아니다. 이전 산출물의 실제 실패 원인은 Debian Buster 저장소 종료로 인한 `apt-get update` 404였다. 현재 산출물은 Python 3.9의 Debian Bookworm 이미지를 사용하고 Bookworm 패키지명에 맞게 수정되어 있다.

컨테이너를 다시 만들기 전에 `/volume1/docker/pinry-custom/data`를 별도 위치에 백업한다. `docker compose down`은 바인드 마운트 데이터 폴더를 삭제하지 않지만, 사용자가 NAS에서 해당 폴더를 직접 지우면 복구할 수 없다.
