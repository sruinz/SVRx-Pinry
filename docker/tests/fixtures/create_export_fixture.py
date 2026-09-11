#!/usr/bin/env python3
"""격리된 런타임 smoke용 사용자와 Pin 원본을 만든다."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from datetime import datetime, timedelta, timezone
from io import BytesIO


_FIXTURE_USERNAME = "export-smoke-user"
_FIXTURE_PASSWORD = "export-smoke-password"


def _is_repository_root(path):
    try:
        return (
            path.is_dir()
            and not path.is_symlink()
            and (path / "manage.py").is_file()
            and (path / "pinry" / "settings" / "base.py").is_file()
        )
    except OSError:
        return False


def _find_repository_root():
    candidates = [Path("/pinry")]
    candidates.extend(Path(__file__).resolve().parents)
    for candidate in candidates:
        if _is_repository_root(candidate):
            return candidate.resolve()
    return None


_REPOSITORY_ROOT = _find_repository_root()
if _REPOSITORY_ROOT is not None:
    _repository_root_text = str(_REPOSITORY_ROOT)
    while _repository_root_text in sys.path:
        sys.path.remove(_repository_root_text)
    sys.path.insert(0, _repository_root_text)


def _pin_count(value):
    try:
        parsed = int(value, 10)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("PIN_COUNT must be a decimal integer")
    if str(parsed) != value or parsed < 1 or parsed > 1000:
        raise argparse.ArgumentTypeError("PIN_COUNT must be between 1 and 1000")
    return parsed


def _arguments(argv):
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("pin_count", type=_pin_count)
    return parser.parse_args(argv)


def _payload_size(pin_count):
    if pin_count <= 12:
        return 1024 * 1024
    if pin_count <= 350:
        return 16 * 1024
    return 4 * 1024


def _png_bytes(index, pin_count):
    from PIL import Image as PillowImage
    from PIL.PngImagePlugin import PngInfo

    seed = hashlib.sha256(
        "svrx-pinry-export-fixture-{}".format(index).encode("ascii")
    ).hexdigest()
    marker = (seed * ((_payload_size(pin_count) // len(seed)) + 1))[
        :_payload_size(pin_count)
    ]
    metadata = PngInfo()
    metadata.add_text("svrx-pinry-fixture", marker)
    output = BytesIO()
    PillowImage.new(
        "RGB",
        (2, 2),
        ((index * 53) % 256, (index * 97) % 256, (index * 193) % 256),
    ).save(output, format="PNG", pnginfo=metadata)
    return output.getvalue()


def _create_image(owner, index, pin_count):
    from django.core.files.uploadedfile import SimpleUploadedFile

    from core.models import Image, MediaAsset

    filename = "export-smoke-{:04d}.png".format(index)
    content = _png_bytes(index, pin_count)
    image = Image.objects.create(
        image=SimpleUploadedFile(filename, content, content_type="image/png"),
        original_filename=filename,
    )
    MediaAsset.objects.create(
        submitter=owner,
        image=image,
        content_sha256=hashlib.sha256(content).hexdigest(),
    )
    return image, content


def _published_at(index):
    return datetime(
        2026,
        1,
        2,
        3,
        4,
        5,
        123456,
        tzinfo=timezone.utc,
    ) + timedelta(seconds=index * 3)


def _format_utc(value):
    return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def create_fixture(pin_count):
    import django

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "pinry.settings.docker")
    django.setup()

    from django.db import transaction

    from core.models import Pin
    from users.models import User

    if os.geteuid() == 0 or os.getegid() == 0:
        raise RuntimeError("export_fixture_must_run_as_service_user")

    with transaction.atomic():
        if User.objects.filter(username=_FIXTURE_USERNAME).exists():
            raise RuntimeError("export_fixture_user_already_exists")
        owner = User.objects.create_user(
            username=_FIXTURE_USERNAME,
            email="export-smoke@example.invalid",
            password=_FIXTURE_PASSWORD,
        )
        records = []
        shared_image = None
        shared_content = None
        shared_pin_ids = []
        for index in range(pin_count):
            if index == 1:
                image, content = shared_image, shared_content
            else:
                image, content = _create_image(owner, index, pin_count)
                if index == 0:
                    shared_image, shared_content = image, content
            pin = Pin.objects.create(
                submitter=owner,
                image=image,
                private=True,
                description="내보내기 smoke 설명 {:04d}".format(index),
                url=(
                    "https://example.invalid/source/{0}?photo={0}"
                    "&token=export-smoke-secret#fragment"
                ).format(index),
                referer=(
                    "https://example.invalid/board/{0}"
                    "?password=export-smoke-secret"
                ).format(index),
            )
            published_at = _published_at(index)
            Pin.objects.filter(pk=pin.pk).update(published=published_at)
            pin.tags.add("공통", "태그-{:04d}".format(index))
            digest = hashlib.sha256(content).hexdigest()
            records.append({
                "id": pin.pk,
                "published_at": _format_utc(published_at),
                "sha256": digest,
                "size": len(content),
            })
            if index < 2:
                shared_pin_ids.append(pin.pk)

    return {
        "schema_version": 1,
        "username": _FIXTURE_USERNAME,
        "pin_ids": [record["id"] for record in records],
        "pins": records,
        "shared_media_pin_ids": shared_pin_ids,
    }


def main(argv):
    arguments = _arguments(argv)
    document = create_fixture(arguments.pin_count)
    json.dump(
        document,
        sys.stdout,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
