from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
import shutil
import uuid
import warnings

from django.conf import settings
from django.core.management import CommandError
from django.db import transaction
from PIL import Image as PILImage

from django_images.file_ops import (
    MediaPathError,
    publish_noreplace,
    resolve_media_path,
    sha256_path,
)
from django_images.models import Image, Thumbnail
from django_images.paths import (
    DERIVATIVE_NAMES,
    UnsupportedImageFormat,
    asset_upload_to,
)


@dataclass(frozen=True)
class MigrationFile(object):
    kind: str
    old_path: str
    new_path: str
    size: int
    sha256: str
    thumbnail_id: int = None
    derivative_size: str = None

    @property
    def kind_key(self):
        if self.thumbnail_id is not None:
            return "thumbnail:{}".format(self.thumbnail_id)
        return "original"

    def as_dict(self):
        value = {
            "kind": self.kind,
            "old_path": self.old_path,
            "new_path": self.new_path,
            "size": self.size,
            "sha256": self.sha256,
        }
        if self.thumbnail_id is not None:
            value["thumbnail_id"] = self.thumbnail_id
            value["derivative_size"] = self.derivative_size
        return value

    @classmethod
    def from_dict(cls, value):
        return cls(
            kind=value["kind"],
            old_path=value["old_path"],
            new_path=value["new_path"],
            size=value["size"],
            sha256=value["sha256"],
            thumbnail_id=value.get("thumbnail_id"),
            derivative_size=value.get("derivative_size"),
        )


@dataclass(frozen=True)
class MigrationPlan(object):
    image_id: int
    old_original: str
    new_original: str
    files: tuple

    @classmethod
    def for_image(cls, image):
        thumbnails = list(image.thumbnail_set.order_by("id"))
        invalid_sizes = [
            thumbnail.size
            for thumbnail in thumbnails
            if thumbnail.size not in DERIVATIVE_NAMES
        ]
        if invalid_sizes:
            raise CommandError("unsupported_legacy_derivative_size")

        records = [("original", image, None)] + [
            ("derivative", thumbnail, thumbnail)
            for thumbnail in thumbnails
        ]
        for _, record, _ in records:
            try:
                resolve_media_path(record.image.name, settings.MEDIA_ROOT)
            except MediaPathError as error:
                raise CommandError(str(error))

        files = []
        for kind, record, thumbnail in records:
            old_path = record.image.name
            try:
                new_path = asset_upload_to(
                    record, os.path.basename(old_path)
                )
                resolve_media_path(new_path, settings.MEDIA_ROOT)
                size, digest = _inspect_image_path(old_path)
            except MediaPathError as error:
                raise CommandError(str(error))
            except (
                OSError,
                PILImage.UnidentifiedImageError,
                UnsupportedImageFormat,
                Warning,
            ) as error:
                raise CommandError("invalid_legacy_media: {}".format(error))
            files.append(
                MigrationFile(
                    kind=kind,
                    old_path=old_path,
                    new_path=new_path,
                    size=size,
                    sha256=digest,
                    thumbnail_id=thumbnail.pk if thumbnail else None,
                    derivative_size=thumbnail.size if thumbnail else None,
                )
            )
        return cls(
            image_id=image.pk,
            old_original=files[0].old_path,
            new_original=files[0].new_path,
            files=tuple(files),
        )

    @classmethod
    def from_event(cls, event):
        files = tuple(
            MigrationFile.from_dict(value) for value in event["files"]
        )
        return cls(
            image_id=event["image_id"],
            old_original=event["old_original"],
            new_original=event["new_original"],
            files=files,
        )

    def as_event_fields(self):
        return {
            "image_id": self.image_id,
            "old_original": self.old_original,
            "new_original": self.new_original,
            "files": [file_plan.as_dict() for file_plan in self.files],
        }


@dataclass
class ManifestState(object):
    run_id: str = None
    latest_by_image: dict = None
    events: list = None

    def __post_init__(self):
        if self.latest_by_image is None:
            self.latest_by_image = {}
        if self.events is None:
            self.events = []


class ManifestLog(object):
    def __init__(self, path, run_id=None):
        self.path = path
        self.state = self.load(path)
        if run_id is not None and not _is_canonical_uuid(run_id):
            raise CommandError("invalid_media_manifest_run_id")
        if self.state.run_id and run_id and self.state.run_id != run_id:
            raise CommandError("manifest_run_id_mismatch")
        self.run_id = self.state.run_id or run_id or str(uuid.uuid4())

    @staticmethod
    def load(path):
        state = ManifestState()
        if not os.path.exists(path):
            return state
        with open(path, encoding="utf-8") as manifest:
            for line_number, line in enumerate(manifest, 1):
                try:
                    event = json.loads(line)
                except (TypeError, ValueError):
                    raise CommandError(
                        "invalid_media_manifest_line:{}".format(line_number)
                    )
                _validate_manifest_event(event, line_number)
                run_id = event.get("run_id")
                if state.run_id is not None and state.run_id != run_id:
                    raise CommandError("manifest_run_id_mismatch")
                state.run_id = run_id
                state.events.append(event)
                state.latest_by_image[event["image_id"]] = event
        return state

    def record(self, event_name, plan, batch_id, execute):
        event = {
            "schema_version": 1,
            "run_id": self.run_id,
            "batch_id": batch_id,
            "event": event_name,
            "execute": bool(execute),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        event.update(plan.as_event_fields())
        directory = os.path.dirname(self.path)
        os.makedirs(directory, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as manifest:
            manifest.write(json.dumps(event, sort_keys=True) + "\n")
            manifest.flush()
            os.fsync(manifest.fileno())
        self.state.run_id = self.run_id
        self.state.events.append(event)
        self.state.latest_by_image[plan.image_id] = event
        return event


@dataclass
class MigrationSummary(object):
    planned: int = 0
    copied: int = 0
    committed: int = 0
    recovered: int = 0
    already_current: int = 0


def _is_canonical_uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (TypeError, ValueError, AttributeError):
        return False


def _validate_manifest_event(event, line_number):
    required = {
        "schema_version",
        "run_id",
        "batch_id",
        "event",
        "execute",
        "recorded_at",
        "image_id",
        "old_original",
        "new_original",
        "files",
    }
    valid_events = {
        "planned",
        "copied",
        "committed",
        "recovered_commit",
        "already_current",
    }
    invalid = (
        not isinstance(event, dict)
        or not required.issubset(event)
        or event.get("schema_version") != 1
        or not _is_canonical_uuid(event.get("run_id"))
        or event.get("event") not in valid_events
        or not isinstance(event.get("execute"), bool)
        or not isinstance(event.get("image_id"), int)
        or isinstance(event.get("image_id"), bool)
        or event.get("image_id", 0) <= 0
        or not isinstance(event.get("files"), list)
        or not event.get("files")
    )
    if invalid:
        _raise_invalid_manifest(line_number)
    files = event["files"]
    if not _valid_manifest_file(files[0], original=True):
        invalid = True
    if any(
        not _valid_manifest_file(file_info, original=False)
        for file_info in files[1:]
    ):
        invalid = True
    if invalid:
        _raise_invalid_manifest(line_number)
    keys = [
        "original"
        if file_info.get("thumbnail_id") is None
        else "thumbnail:{}".format(file_info["thumbnail_id"])
        for file_info in files
    ]
    if len(keys) != len(set(keys)):
        invalid = True
    if (
        event["old_original"] != files[0].get("old_path")
        or event["new_original"] != files[0].get("new_path")
    ):
        invalid = True
    if invalid:
        _raise_invalid_manifest(line_number)


def _raise_invalid_manifest(line_number):
    raise CommandError(
        "invalid_media_manifest_line:{}".format(line_number)
    )


def _valid_manifest_file(file_info, original):
    if not isinstance(file_info, dict):
        return False
    required = {"kind", "old_path", "new_path", "size", "sha256"}
    if not required.issubset(file_info):
        return False
    size = file_info["size"]
    digest = file_info["sha256"]
    if (
        not isinstance(file_info["old_path"], str)
        or not isinstance(file_info["new_path"], str)
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size < 0
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        return False
    if original:
        return (
            file_info["kind"] == "original"
            and file_info.get("thumbnail_id") is None
            and file_info.get("derivative_size") is None
        )
    return (
        file_info["kind"] == "derivative"
        and isinstance(file_info.get("thumbnail_id"), int)
        and not isinstance(file_info.get("thumbnail_id"), bool)
        and file_info.get("thumbnail_id", 0) > 0
        and file_info.get("derivative_size") in DERIVATIVE_NAMES
    )


def _inspect_absolute_image(path):
    size = os.path.getsize(path)
    digest = sha256_path(path)
    with warnings.catch_warnings():
        warnings.simplefilter("error", PILImage.DecompressionBombWarning)
        with PILImage.open(path) as image:
            image.verify()
        with PILImage.open(path) as image:
            image.load()
    return size, digest


def _inspect_image_path(relative_name):
    path = resolve_media_path(relative_name, settings.MEDIA_ROOT)
    return _inspect_absolute_image(path)


class MediaMigrator(object):
    def __init__(
        self, storage, manifest, batch_size=100, fault_injector=None
    ):
        if batch_size <= 0:
            raise CommandError("batch_size_must_be_positive")
        self.storage = storage
        self.manifest = manifest
        self.batch_size = batch_size
        self.fault_injector = fault_injector
        self.summary = MigrationSummary()

    def run(self, execute=False):
        for batch_id, images in self._batches():
            pending = []
            for image in images:
                latest = self.manifest.state.latest_by_image.get(image.pk)
                if latest is not None:
                    plan = MigrationPlan.from_event(latest)
                    action = self._resume_action(image, plan, latest)
                    if action == "skip":
                        continue
                    if action == "recovered":
                        self.manifest.record(
                            "recovered_commit", plan, batch_id, execute
                        )
                        self.summary.recovered += 1
                        continue
                    self._verify_sources(plan)
                    if execute:
                        pending.append(plan)
                    continue

                plan = MigrationPlan.for_image(image)
                current_flags = [
                    file_plan.old_path == file_plan.new_path
                    for file_plan in plan.files
                ]
                if all(current_flags):
                    self.manifest.record(
                        "already_current", plan, batch_id, execute
                    )
                    self.summary.already_current += 1
                    continue
                if any(current_flags):
                    raise CommandError("mixed_media_state")
                self.manifest.record("planned", plan, batch_id, execute)
                self.summary.planned += 1
                if execute:
                    pending.append(plan)

            if not execute or not pending:
                continue
            prepared = []
            for plan in pending:
                self._copy_and_verify(plan)
                self.manifest.record("copied", plan, batch_id, execute)
                self.summary.copied += 1
                prepared.append(plan)
            with transaction.atomic():
                self._update_paths_with_queryset_update(prepared)
            self._inject_fault("after_database_commit")
            for plan in prepared:
                self.manifest.record("committed", plan, batch_id, execute)
                self.summary.committed += 1
        return self.summary

    def _batches(self):
        start = 0
        while True:
            images = list(
                Image.objects.order_by("pk")[start:start + self.batch_size]
            )
            if not images:
                return
            yield str((start // self.batch_size) + 1), images
            start += self.batch_size

    def _resume_action(self, image, plan, latest):
        current = _current_paths(image)
        old = {file_plan.kind_key: file_plan.old_path for file_plan in plan.files}
        new = {file_plan.kind_key: file_plan.new_path for file_plan in plan.files}
        if current == new:
            self._verify_destinations(plan)
            if latest["event"] in ("committed", "recovered_commit"):
                return "skip"
            if latest["event"] == "already_current":
                return "skip"
            return "recovered"
        if current == old:
            if latest["event"] in ("committed", "recovered_commit"):
                raise CommandError("mixed_media_state")
            return "continue"
        raise CommandError("mixed_media_state")

    def _verify_sources(self, plan):
        for file_plan in plan.files:
            self._verify_file(file_plan, file_plan.old_path)

    def _verify_destinations(self, plan):
        for file_plan in plan.files:
            self._verify_file(file_plan, file_plan.new_path)

    def _verify_file(self, file_plan, relative_name):
        try:
            path = resolve_media_path(relative_name, settings.MEDIA_ROOT)
            size, digest = _inspect_absolute_image(path)
        except (
            MediaPathError,
            OSError,
            PILImage.UnidentifiedImageError,
            Warning,
        ) as error:
            raise CommandError("media_verification_failed: {}".format(error))
        if size != file_plan.size or digest != file_plan.sha256:
            raise CommandError("media_hash_mismatch")

    def _copy_and_verify(self, plan):
        for file_plan in plan.files:
            source = self._resolve_media(file_plan.old_path)
            destination = self._resolve_media(file_plan.new_path)
            parent = os.path.dirname(destination)
            os.makedirs(parent, exist_ok=True)
            destination = self._resolve_media(file_plan.new_path)
            part_path = destination + ".part-" + self.manifest.run_id
            if not os.path.exists(part_path):
                with open(source, "rb") as source_file:
                    with open(part_path, "xb") as part_file:
                        shutil.copyfileobj(source_file, part_file)
                        part_file.flush()
                        os.fsync(part_file.fileno())
            self._verify_absolute_part(file_plan, part_path)
            try:
                publish_noreplace(
                    part_path, destination, file_plan.sha256
                )
            except MediaPathError as error:
                raise CommandError(str(error))
            self._verify_file(file_plan, file_plan.new_path)

    def _verify_absolute_part(self, file_plan, part_path):
        try:
            size, digest = _inspect_absolute_image(part_path)
        except (OSError, PILImage.UnidentifiedImageError, Warning) as error:
            raise CommandError("media_verification_failed: {}".format(error))
        if size != file_plan.size or digest != file_plan.sha256:
            raise CommandError("media_hash_mismatch")

    def _update_paths_with_queryset_update(self, plans):
        for plan in plans:
            updated = Image.objects.filter(pk=plan.image_id).update(
                image=plan.new_original
            )
            if updated != 1:
                raise CommandError("media_database_changed")
            for file_plan in plan.files:
                if file_plan.thumbnail_id is not None:
                    updated = Thumbnail.objects.filter(
                        pk=file_plan.thumbnail_id
                    ).update(image=file_plan.new_path)
                    if updated != 1:
                        raise CommandError("media_database_changed")

    def _resolve_media(self, relative_name):
        try:
            return resolve_media_path(relative_name, settings.MEDIA_ROOT)
        except MediaPathError as error:
            raise CommandError(str(error))

    def _inject_fault(self, point):
        if self.fault_injector is not None:
            self.fault_injector(point)


def _current_paths(image):
    current = {"original": image.image.name}
    current.update(
        {
            "thumbnail:{}".format(thumbnail.pk): thumbnail.image.name
            for thumbnail in image.thumbnail_set.order_by("id")
        }
    )
    return current
