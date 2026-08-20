# 시놀로지 이미지 빌드 안내

이 문서는 Pinry Custom Docker 이미지를 시놀로지 NAS에서 직접 빌드하는 방법을 설명한다. 데이터베이스, 업로드 이미지, 비밀키와 기존 컨테이너 데이터는 산출물에 포함하지 않는다.

## 개발 PC에서 산출물 생성

저장소 루트에서 다음 명령을 실행한다.

```sh
./scripts/create_synology_output.sh
```

`output/synology/` 아래에 업로드 가능한 `pinry-custom/` 폴더와 `pinry-custom-<커밋>.tar.gz` 파일이 생성된다. 패키지는 현재 Git `HEAD`에 커밋된 런타임 파일만 포함하며 미커밋 변경, 테스트, Markdown 문서, GitHub 설정과 개발용 파일은 포함하지 않는다. 기존 산출물이 있으면 덮어쓰지 않고 중단한다.

산출물 구조는 다음과 같다.

```text
pinry-custom/
├── BUILD_INFO
├── build-image.sh
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

Docker는 `context/`만 build context로 사용한다. 바깥의 `BUILD_INFO`와 `build-image.sh`는 최종 이미지에 복사되지 않는다.

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

Docker 소켓 권한이 없는 환경에서는 다음과 같이 실행한다.

```sh
sudo ./build-image.sh
```

빌드가 끝나면 생성된 이미지를 확인한다.

```sh
docker image ls pinry-custom
```

이 산출물은 이미지 빌드까지만 담당한다. 기존 `/data` 볼륨 연결, 컨테이너 교체, 데이터 이전과 백업은 별도 배포 절차에서 수행해야 한다.
