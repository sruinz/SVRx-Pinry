from dataclasses import dataclass
from datetime import timedelta
import os
import stat
import time
import uuid
import warnings

from django.conf import settings
from django.core.management import CommandError
from PIL import Image as PILImage

from django_images.file_ops import (
    MediaPathError,
    _open_directory,
    _relative_components,
    _unlink_owned_name,
    sha256_file_descriptor,
)
from django_images.models import Image, Thumbnail
from django_images.paths import DERIVATIVE_NAMES, FORMAT_EXTENSIONS
from django_images.services.media_migration import MigrationPlan, _current_paths


ORPHAN_ROOTS = (".staging", "originals", "derivatives")
MINIMUM_AGE = timedelta(hours=24)


@dataclass(frozen=True)
class CleanupSummary(object):
    candidate_paths: tuple = ()
    deleted: int = 0

    @property
    def candidates(self):
        return len(self.candidate_paths)


@dataclass(frozen=True)
class _FileCandidate(object):
    relative_path: str
    file_stat: object
    extension: str = None


@dataclass(frozen=True)
class _DirectoryCandidate(object):
    relative_path: str
    file_stat: object


@dataclass(frozen=True)
class _Tree(object):
    directory: _DirectoryCandidate
    files: tuple
    directories: tuple

    @property
    def all_entries(self):
        return self.files + self.directories


class LegacyMediaCleaner(object):
    def __init__(self, manifest, media_root=None):
        self.manifest = manifest
        self.media_root = media_root or settings.MEDIA_ROOT

    def run(self, execute=False):
        root_descriptor = _open_media_root(self.media_root)
        try:
            candidates = self._validated_candidates(root_descriptor)
            if execute:
                for candidate in candidates:
                    _unlink_file(root_descriptor, candidate)
            return CleanupSummary(
                candidate_paths=tuple(
                    candidate.relative_path for candidate in candidates
                ),
                deleted=len(candidates) if execute else 0,
            )
        finally:
            os.close(root_descriptor)

    def _validated_candidates(self, root_descriptor):
        state = self.manifest.state
        if state.torn_tail is not None:
            raise CommandError("media_manifest_torn_tail")
        latest_events = tuple(state.latest_by_image.values())
        if not latest_events:
            raise CommandError("manifest_not_committed")
        if any(
            event["event"] not in (
                "committed",
                "recovered_commit",
                "already_current",
            )
            for event in latest_events
        ):
            raise CommandError("manifest_not_committed")
        if any(
            event["event"] in ("committed", "recovered_commit")
            and not event["execute"]
            for event in latest_events
        ):
            raise CommandError("manifest_not_committed")

        plans = tuple(
            MigrationPlan.from_event(event)
            for event in latest_events
            if event["event"] in ("committed", "recovered_commit")
        )
        current_references = _current_media_references(root_descriptor)
        expected_old = {}
        for plan in plans:
            image = Image.objects.filter(pk=plan.image_id).first()
            if image is None:
                raise CommandError("manifest_plan_mismatch")
            inspections = []
            for file_plan in plan.files:
                inspections.append(_inspect_regular_file(
                    root_descriptor,
                    file_plan.new_path,
                    expected_size=file_plan.size,
                    expected_sha256=file_plan.sha256,
                    verify_image=True,
                ))
            _validate_current_plan(image, plan, inspections)

            for file_plan in plan.files:
                if file_plan.old_path == file_plan.new_path:
                    raise CommandError("manifest_plan_mismatch")
                if file_plan.old_path in current_references:
                    raise CommandError("legacy_media_still_referenced")
                previous = expected_old.get(file_plan.old_path)
                signature = (file_plan.size, file_plan.sha256)
                if previous is not None and previous != signature:
                    raise CommandError("manifest_plan_mismatch")
                expected_old[file_plan.old_path] = signature

        candidates = []
        for relative_path in sorted(expected_old):
            size, digest = expected_old[relative_path]
            candidate = _inspect_regular_file(
                root_descriptor,
                relative_path,
                expected_size=size,
                expected_sha256=digest,
                verify_image=True,
                missing_ok=True,
            )
            if candidate is None:
                continue
            candidates.append(candidate)
        return tuple(candidates)


class OrphanMediaCleaner(object):
    def __init__(self, media_root=None):
        self.media_root = media_root or settings.MEDIA_ROOT

    def run(self, execute=False, older_than=MINIMUM_AGE):
        if older_than < MINIMUM_AGE:
            raise CommandError("orphan_age_too_short")
        root_descriptor = _open_media_root(self.media_root)
        try:
            cutoff = time.time() - older_than.total_seconds()
            files, directories = self._validated_candidates(
                root_descriptor, cutoff
            )
            if execute:
                for candidate in files:
                    _unlink_file(root_descriptor, candidate)
                for candidate in directories:
                    _remove_empty_directory(root_descriptor, candidate)
            paths = tuple(
                candidate.relative_path for candidate in files + directories
            )
            return CleanupSummary(
                candidate_paths=paths,
                deleted=len(paths) if execute else 0,
            )
        finally:
            os.close(root_descriptor)

    def _validated_candidates(self, root_descriptor, cutoff):
        references = _current_media_references(root_descriptor)
        protected_uuids = {
            str(asset_uuid)
            for asset_uuid in Image.objects.values_list(
                "asset_uuid", flat=True
            )
        }
        protected_directories = _referenced_uuid_directories(references)

        root_directories = {}
        try:
            for root_name in ORPHAN_ROOTS:
                opened = _open_child_directory(
                    root_descriptor, root_name, missing_ok=True
                )
                if opened is not None:
                    root_directories[root_name] = opened

            selected_files = []
            selected_directories = []
            staging = root_directories.get(".staging")
            if staging is not None:
                staging_tree = _scan_tree(
                    ".staging", staging[0], staging[1]
                )
                staging_files, staging_directories = _staging_candidates(
                    staging_tree, cutoff, references
                )
                selected_files.extend(staging_files)
                selected_directories.extend(staging_directories)

            trees_by_uuid = {}
            for root_name in ("originals", "derivatives"):
                opened_root = root_directories.get(root_name)
                if opened_root is None:
                    continue
                root_directory = opened_root[0]
                for entry_name in _list_directory(root_directory):
                    entry_stat = _stat_entry(root_directory, entry_name)
                    if stat.S_ISLNK(entry_stat.st_mode):
                        raise CommandError("media_path_escape")
                    if stat.S_ISREG(entry_stat.st_mode):
                        continue
                    if not stat.S_ISDIR(entry_stat.st_mode):
                        raise CommandError("unsafe_orphan_entry")
                    if not _is_canonical_uuid(entry_name):
                        continue
                    relative_path = "{}/{}".format(
                        root_name, entry_name
                    )
                    child_descriptor = None
                    try:
                        child_descriptor, child_stat = (
                            _open_child_directory(
                                root_directory, entry_name
                            )
                        )
                        tree = _scan_tree(
                            relative_path,
                            child_descriptor,
                            child_stat,
                        )
                    finally:
                        if child_descriptor is not None:
                            os.close(child_descriptor)
                    trees_by_uuid.setdefault(entry_name, []).append(tree)

            for asset_uuid, trees in sorted(trees_by_uuid.items()):
                if asset_uuid in protected_uuids:
                    continue
                if any(
                    (
                        tree.directory.relative_path.split("/", 1)[0],
                        asset_uuid,
                    )
                    in protected_directories
                    for tree in trees
                ):
                    continue
                entries = tuple(
                    entry
                    for tree in trees
                    for entry in tree.all_entries + (tree.directory,)
                )
                if not entries or any(
                    entry.file_stat.st_mtime >= cutoff for entry in entries
                ):
                    continue
                selected_files.extend(
                    file_candidate
                    for tree in trees
                    for file_candidate in tree.files
                )
                selected_directories.extend(
                    directory
                    for tree in trees
                    for directory in tree.directories + (tree.directory,)
                )
        finally:
            for descriptor, _ in root_directories.values():
                os.close(descriptor)

        selected_files.sort(key=lambda item: item.relative_path)
        selected_directories.sort(
            key=lambda item: (-item.relative_path.count("/"), item.relative_path)
        )
        return tuple(selected_files), tuple(selected_directories)


def _current_media_references(root_descriptor):
    references = set(Image.objects.values_list("image", flat=True))
    references.update(Thumbnail.objects.values_list("image", flat=True))
    for relative_path in references:
        _validate_reference_path(root_descriptor, relative_path)
    return references


def _referenced_uuid_directories(references):
    directories = set()
    for relative_path in references:
        parts = relative_path.split("/")
        if len(parts) >= 3 and parts[0] in ("originals", "derivatives"):
            directories.add((parts[0], parts[1]))
    return directories


def _inspect_regular_file(
    root_descriptor,
    relative_path,
    expected_size=None,
    expected_sha256=None,
    verify_image=False,
    missing_ok=False,
):
    parent_descriptor = None
    file_descriptor = None
    try:
        try:
            parent_descriptor, name = _open_trusted_parent(
                root_descriptor, relative_path
            )
        except FileNotFoundError as error:
            if missing_ok:
                return None
            raise CommandError(
                "media_verification_failed: {}".format(error)
            )
        except OSError:
            raise CommandError("media_path_escape")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        file_descriptor = os.open(
            name, flags, dir_fd=parent_descriptor
        )
        file_stat = os.fstat(file_descriptor)
        named_stat = os.stat(
            name, dir_fd=parent_descriptor, follow_symlinks=False
        )
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or not stat.S_ISREG(named_stat.st_mode)
            or not _same_identity(file_stat, named_stat)
            or file_stat.st_size != named_stat.st_size
        ):
            raise CommandError("media_verification_failed")
        if expected_size is not None and file_stat.st_size != expected_size:
            raise CommandError("media_hash_mismatch")
        if (
            expected_sha256 is not None
            and sha256_file_descriptor(file_descriptor) != expected_sha256
        ):
            raise CommandError("media_hash_mismatch")
        extension = None
        if verify_image:
            extension = _image_extension(file_descriptor)
        return _FileCandidate(relative_path, file_stat, extension)
    except FileNotFoundError as error:
        if missing_ok:
            return None
        raise CommandError("media_verification_failed: {}".format(error))
    except CommandError:
        raise
    except (MediaPathError, OSError) as error:
        raise CommandError("media_verification_failed: {}".format(error))
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        if parent_descriptor is not None:
            os.close(parent_descriptor)


def _image_extension(file_descriptor):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", PILImage.DecompressionBombWarning)
            with os.fdopen(os.dup(file_descriptor), "rb") as image_file:
                image_file.seek(0)
                with PILImage.open(image_file) as image:
                    image_format = image.format
                    image.verify()
            with os.fdopen(os.dup(file_descriptor), "rb") as image_file:
                image_file.seek(0)
                with PILImage.open(image_file) as image:
                    image.load()
        return FORMAT_EXTENSIONS[image_format]
    except (
        KeyError,
        OSError,
        PILImage.UnidentifiedImageError,
        Warning,
    ) as error:
        raise CommandError("media_verification_failed: {}".format(error))


def _validate_current_plan(image, plan, inspections):
    thumbnails = list(image.thumbnail_set.order_by("id"))
    if any(thumbnail.size not in DERIVATIVE_NAMES for thumbnail in thumbnails):
        raise CommandError("manifest_plan_mismatch")
    expected_records = [("original", image, None)] + [
        ("derivative", thumbnail, thumbnail) for thumbnail in thumbnails
    ]
    if len(plan.files) != len(expected_records):
        raise CommandError("manifest_plan_mismatch")

    expected_paths = {}
    asset_uuid = str(image.asset_uuid)
    for file_plan, inspection, record in zip(
        plan.files, inspections, expected_records
    ):
        kind, database_record, thumbnail = record
        if kind == "original":
            canonical_path = "originals/{}/original{}".format(
                asset_uuid, inspection.extension
            )
            invalid_metadata = (
                file_plan.kind != "original"
                or file_plan.thumbnail_id is not None
                or file_plan.derivative_size is not None
            )
        else:
            canonical_path = "derivatives/{}/{}{}".format(
                asset_uuid, thumbnail.size, inspection.extension
            )
            invalid_metadata = (
                file_plan.kind != "derivative"
                or file_plan.thumbnail_id != thumbnail.pk
                or file_plan.derivative_size != thumbnail.size
            )
        if (
            invalid_metadata
            or file_plan.new_path != canonical_path
            or database_record.image.name != file_plan.new_path
        ):
            raise CommandError("manifest_plan_mismatch")
        expected_paths[file_plan.kind_key] = file_plan.new_path

    if (
        plan.new_original != plan.files[0].new_path
        or plan.old_original != plan.files[0].old_path
        or _current_paths(image) != expected_paths
    ):
        raise CommandError("manifest_plan_mismatch")


def _validate_reference_path(root_descriptor, relative_path):
    parent_descriptor = None
    try:
        parent_descriptor, name = _open_trusted_parent(
            root_descriptor, relative_path
        )
        try:
            file_stat = os.stat(
                name, dir_fd=parent_descriptor, follow_symlinks=False
            )
        except FileNotFoundError:
            return
        if not stat.S_ISREG(file_stat.st_mode):
            raise CommandError("media_path_escape")
    except FileNotFoundError:
        return
    except CommandError:
        raise
    except (MediaPathError, OSError):
        raise CommandError("media_path_escape")
    finally:
        if parent_descriptor is not None:
            os.close(parent_descriptor)


def _validate_entry_name(name):
    if (
        not isinstance(name, str)
        or not name
        or name in (".", "..")
        or "/" in name
        or "\\" in name
    ):
        raise CommandError("media_path_escape")


def _stat_entry(directory_descriptor, name):
    _validate_entry_name(name)
    try:
        return os.stat(
            name, dir_fd=directory_descriptor, follow_symlinks=False
        )
    except OSError as error:
        raise CommandError("unsafe_orphan_entry: {}".format(error))


def _list_directory(directory_descriptor):
    try:
        names = os.listdir(directory_descriptor)
    except OSError as error:
        raise CommandError("unsafe_orphan_entry: {}".format(error))
    for name in names:
        _validate_entry_name(name)
    return sorted(names)


def _open_child_directory(
    parent_descriptor, name, missing_ok=False
):
    _validate_entry_name(name)
    try:
        named_stat = os.stat(
            name, dir_fd=parent_descriptor, follow_symlinks=False
        )
    except FileNotFoundError:
        if missing_ok:
            return None
        raise CommandError("unsafe_orphan_entry")
    except OSError as error:
        raise CommandError("unsafe_orphan_entry: {}".format(error))
    if stat.S_ISLNK(named_stat.st_mode):
        raise CommandError("media_path_escape")
    if not stat.S_ISDIR(named_stat.st_mode):
        raise CommandError("unsafe_orphan_entry")

    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = None
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
        opened_stat = os.fstat(descriptor)
        if not stat.S_ISDIR(opened_stat.st_mode) or not _same_identity(
            named_stat, opened_stat
        ):
            raise CommandError("unsafe_orphan_entry")
        return descriptor, opened_stat
    except CommandError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as error:
        if descriptor is not None:
            os.close(descriptor)
        raise CommandError("unsafe_orphan_entry: {}".format(error))


def _scan_tree(relative_path, directory_descriptor, directory_stat):
    files = []
    directories = []
    for entry_name in _list_directory(directory_descriptor):
        entry_relative = "{}/{}".format(relative_path, entry_name)
        entry_stat = _stat_entry(directory_descriptor, entry_name)
        if stat.S_ISLNK(entry_stat.st_mode):
            raise CommandError("media_path_escape")
        if stat.S_ISREG(entry_stat.st_mode):
            files.append(_FileCandidate(entry_relative, entry_stat))
            continue
        if not stat.S_ISDIR(entry_stat.st_mode):
            raise CommandError("unsafe_orphan_entry")
        child_descriptor = None
        try:
            child_descriptor, child_stat = _open_child_directory(
                directory_descriptor, entry_name
            )
            child = _scan_tree(
                entry_relative, child_descriptor, child_stat
            )
        finally:
            if child_descriptor is not None:
                os.close(child_descriptor)
        files.extend(child.files)
        directories.extend(child.directories)
        directories.append(child.directory)
    return _Tree(
        directory=_DirectoryCandidate(relative_path, directory_stat),
        files=tuple(files),
        directories=tuple(directories),
    )


def _staging_candidates(tree, cutoff, references):
    selected_files = tuple(
        candidate
        for candidate in tree.files
        if candidate.relative_path not in references
        and candidate.file_stat.st_mtime < cutoff
    )
    selected_paths = {
        candidate.relative_path for candidate in selected_files
    }
    selected_directories = []
    for directory in tree.directories:
        prefix = directory.relative_path + "/"
        descendants = tuple(
            file_candidate
            for file_candidate in tree.files
            if file_candidate.relative_path.startswith(prefix)
        )
        descendant_directories = tuple(
            child
            for child in tree.directories
            if child.relative_path.startswith(prefix)
        )
        if (
            directory.file_stat.st_mtime < cutoff
            and all(
                candidate.relative_path in selected_paths
                for candidate in descendants
            )
            and all(
                child.file_stat.st_mtime < cutoff
                for child in descendant_directories
            )
        ):
            selected_directories.append(directory)
    selected_directories.sort(
        key=lambda item: (-item.relative_path.count("/"), item.relative_path)
    )
    return selected_files, tuple(selected_directories)


def _same_identity(first, second):
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _open_media_root(media_root):
    try:
        return _open_directory(os.path.realpath(media_root))
    except OSError as error:
        raise CommandError("unsafe_media_root: {}".format(error))


def _open_trusted_parent(root_descriptor, relative_path):
    try:
        components = _relative_components(relative_path)
    except MediaPathError as error:
        raise CommandError(str(error))
    current = os.dup(root_descriptor)
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        for component in components[:-1]:
            next_descriptor = os.open(
                component, flags, dir_fd=current
            )
            os.close(current)
            current = next_descriptor
        return current, components[-1]
    except BaseException:
        os.close(current)
        raise


def _unlink_file(root_descriptor, candidate):
    parent_descriptor = None
    try:
        parent_descriptor, name = _open_trusted_parent(
            root_descriptor, candidate.relative_path
        )
        _unlink_owned_name(
            parent_descriptor,
            name,
            candidate.file_stat,
        )
    except (CommandError, MediaPathError, OSError) as error:
        raise CommandError("unsafe_media_file: {}".format(error))
    finally:
        if parent_descriptor is not None:
            os.close(parent_descriptor)


def _remove_empty_directory(root_descriptor, candidate):
    parent_descriptor = None
    try:
        parent_descriptor, name = _open_trusted_parent(
            root_descriptor, candidate.relative_path
        )
        current = os.stat(
            name, dir_fd=parent_descriptor, follow_symlinks=False
        )
        if not stat.S_ISDIR(current.st_mode) or not _same_identity(
            current, candidate.file_stat
        ):
            raise CommandError("unsafe_orphan_entry")
        os.rmdir(name, dir_fd=parent_descriptor)
    except CommandError:
        raise
    except OSError as error:
        raise CommandError("unsafe_orphan_entry: {}".format(error))
    finally:
        if parent_descriptor is not None:
            os.close(parent_descriptor)


def _is_canonical_uuid(value):
    try:
        return str(uuid.UUID(value)) == value
    except (TypeError, ValueError, AttributeError):
        return False
