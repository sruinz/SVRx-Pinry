from dataclasses import dataclass
from datetime import timedelta
import os
import stat
import time
import uuid

from django.conf import settings
from django.core.management import CommandError

from django_images.file_ops import (
    MediaPathError,
    _open_directory,
    _open_regular_nofollow,
    _unlink_owned_name,
    resolve_media_path,
    sha256_file_descriptor,
)
from django_images.models import Image, Thumbnail
from django_images.services.media_migration import (
    MigrationPlan,
    _canonical_plan_signature,
    _current_paths,
)


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
    absolute_path: str
    file_stat: object


@dataclass(frozen=True)
class _DirectoryCandidate(object):
    relative_path: str
    absolute_path: str
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
        candidates = self._validated_candidates()
        if execute:
            for candidate in candidates:
                _unlink_file(candidate)
        return CleanupSummary(
            candidate_paths=tuple(
                candidate.relative_path for candidate in candidates
            ),
            deleted=len(candidates) if execute else 0,
        )

    def _validated_candidates(self):
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
        current_references = _current_media_references(self.media_root)
        expected_old = {}
        for plan in plans:
            image = Image.objects.filter(pk=plan.image_id).first()
            if image is None:
                raise CommandError("manifest_plan_mismatch")
            try:
                current_plan = MigrationPlan.for_image(image)
            except CommandError:
                raise CommandError("manifest_plan_mismatch")
            if _canonical_plan_signature(
                plan
            ) != _canonical_plan_signature(current_plan):
                raise CommandError("manifest_plan_mismatch")
            expected_paths = {
                file_plan.kind_key: file_plan.new_path
                for file_plan in plan.files
            }
            if _current_paths(image) != expected_paths:
                raise CommandError("manifest_plan_mismatch")

            for file_plan in plan.files:
                _inspect_regular_file(
                    file_plan.new_path,
                    self.media_root,
                    expected_size=file_plan.size,
                    expected_sha256=file_plan.sha256,
                )
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
            absolute_path = _resolve(relative_path, self.media_root)
            _reject_symlink_components(relative_path, self.media_root)
            if not os.path.exists(absolute_path):
                continue
            candidates.append(
                _inspect_regular_file(
                    relative_path,
                    self.media_root,
                    expected_size=size,
                    expected_sha256=digest,
                )
            )
        return tuple(candidates)


class OrphanMediaCleaner(object):
    def __init__(self, media_root=None):
        self.media_root = media_root or settings.MEDIA_ROOT

    def run(self, execute=False, older_than=MINIMUM_AGE):
        if older_than < MINIMUM_AGE:
            raise CommandError("orphan_age_too_short")
        cutoff = time.time() - older_than.total_seconds()
        files, directories = self._validated_candidates(cutoff)
        if execute:
            for candidate in files:
                _unlink_file(candidate)
            for candidate in directories:
                _remove_empty_directory(candidate)
        paths = tuple(
            candidate.relative_path for candidate in files + directories
        )
        return CleanupSummary(
            candidate_paths=paths,
            deleted=len(paths) if execute else 0,
        )

    def _validated_candidates(self, cutoff):
        references = _current_media_references(self.media_root)
        protected_uuids = {
            str(asset_uuid)
            for asset_uuid in Image.objects.values_list(
                "asset_uuid", flat=True
            )
        }
        protected_directories = _referenced_uuid_directories(references)

        root_paths = {}
        for root_name in ORPHAN_ROOTS:
            absolute_path = _resolve(root_name, self.media_root)
            lexical_path = os.path.join(
                os.path.realpath(self.media_root), root_name
            )
            if not os.path.lexists(lexical_path):
                continue
            root_stat = os.lstat(lexical_path)
            if not stat.S_ISDIR(root_stat.st_mode):
                raise CommandError("unsafe_orphan_entry")
            if absolute_path != lexical_path:
                raise CommandError("unsafe_orphan_entry")
            root_paths[root_name] = absolute_path

        selected_files = []
        selected_directories = []
        staging_path = root_paths.get(".staging")
        if staging_path is not None:
            staging_tree = _scan_tree(
                ".staging", staging_path, self.media_root
            )
            staging_files, staging_directories = _staging_candidates(
                staging_tree, cutoff, references
            )
            selected_files.extend(staging_files)
            selected_directories.extend(staging_directories)

        trees_by_uuid = {}
        for root_name in ("originals", "derivatives"):
            root_path = root_paths.get(root_name)
            if root_path is None:
                continue
            for entry in sorted(os.scandir(root_path), key=lambda item: item.name):
                entry_stat = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(entry_stat.st_mode) or not (
                    stat.S_ISDIR(entry_stat.st_mode)
                    or stat.S_ISREG(entry_stat.st_mode)
                ):
                    raise CommandError("unsafe_orphan_entry")
                if not stat.S_ISDIR(entry_stat.st_mode):
                    continue
                if not _is_canonical_uuid(entry.name):
                    continue
                relative_path = "{}/{}".format(root_name, entry.name)
                tree = _scan_tree(
                    relative_path, entry.path, self.media_root
                )
                trees_by_uuid.setdefault(entry.name, []).append(tree)

        for asset_uuid, trees in sorted(trees_by_uuid.items()):
            if asset_uuid in protected_uuids:
                continue
            if any(
                (tree.directory.relative_path.split("/", 1)[0], asset_uuid)
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
                file_candidate for tree in trees for file_candidate in tree.files
            )
            selected_directories.extend(
                directory
                for tree in trees
                for directory in tree.directories + (tree.directory,)
            )

        selected_files.sort(key=lambda item: item.relative_path)
        selected_directories.sort(
            key=lambda item: (-item.relative_path.count("/"), item.relative_path)
        )
        return tuple(selected_files), tuple(selected_directories)


def _resolve(relative_path, media_root):
    try:
        return resolve_media_path(relative_path, media_root)
    except MediaPathError as error:
        raise CommandError(str(error))


def _current_media_references(media_root):
    references = set(Image.objects.values_list("image", flat=True))
    references.update(Thumbnail.objects.values_list("image", flat=True))
    for relative_path in references:
        _resolve(relative_path, media_root)
    return references


def _referenced_uuid_directories(references):
    directories = set()
    for relative_path in references:
        parts = relative_path.split("/")
        if len(parts) >= 3 and parts[0] in ("originals", "derivatives"):
            directories.add((parts[0], parts[1]))
    return directories


def _reject_symlink_components(relative_path, media_root):
    current = os.path.realpath(media_root)
    for component in relative_path.split("/"):
        current = os.path.join(current, component)
        try:
            file_stat = os.lstat(current)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(file_stat.st_mode):
            raise CommandError("unsafe_media_file")


def _inspect_regular_file(
    relative_path, media_root, expected_size=None, expected_sha256=None
):
    absolute_path = _resolve(relative_path, media_root)
    _reject_symlink_components(relative_path, media_root)
    parent_descriptor = None
    file_descriptor = None
    try:
        parent_descriptor = _open_directory(os.path.dirname(absolute_path))
        file_descriptor = _open_regular_nofollow(
            parent_descriptor, os.path.basename(absolute_path)
        )
        file_stat = os.fstat(file_descriptor)
        if expected_size is not None and file_stat.st_size != expected_size:
            raise CommandError("media_hash_mismatch")
        if (
            expected_sha256 is not None
            and sha256_file_descriptor(file_descriptor) != expected_sha256
        ):
            raise CommandError("media_hash_mismatch")
        return _FileCandidate(relative_path, absolute_path, file_stat)
    except CommandError:
        raise
    except (MediaPathError, OSError) as error:
        raise CommandError("media_verification_failed: {}".format(error))
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        if parent_descriptor is not None:
            os.close(parent_descriptor)


def _scan_tree(relative_path, absolute_path, media_root):
    _resolve(relative_path, media_root)
    _reject_symlink_components(relative_path, media_root)
    directory_stat = os.lstat(absolute_path)
    if not stat.S_ISDIR(directory_stat.st_mode):
        raise CommandError("unsafe_orphan_entry")
    files = []
    directories = []
    for entry in sorted(os.scandir(absolute_path), key=lambda item: item.name):
        entry_relative = "{}/{}".format(relative_path, entry.name)
        _resolve(entry_relative, media_root)
        entry_stat = entry.stat(follow_symlinks=False)
        if stat.S_ISLNK(entry_stat.st_mode):
            raise CommandError("unsafe_orphan_entry")
        if stat.S_ISREG(entry_stat.st_mode):
            files.append(_FileCandidate(entry_relative, entry.path, entry_stat))
            continue
        if not stat.S_ISDIR(entry_stat.st_mode):
            raise CommandError("unsafe_orphan_entry")
        child = _scan_tree(entry_relative, entry.path, media_root)
        files.extend(child.files)
        directories.extend(child.directories)
        directories.append(child.directory)
    return _Tree(
        directory=_DirectoryCandidate(
            relative_path, absolute_path, directory_stat
        ),
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


def _unlink_file(candidate):
    parent_descriptor = None
    try:
        parent_descriptor = _open_directory(
            os.path.dirname(candidate.absolute_path)
        )
        _unlink_owned_name(
            parent_descriptor,
            os.path.basename(candidate.absolute_path),
            candidate.file_stat,
        )
    except (MediaPathError, OSError) as error:
        raise CommandError("unsafe_media_file: {}".format(error))
    finally:
        if parent_descriptor is not None:
            os.close(parent_descriptor)


def _remove_empty_directory(candidate):
    try:
        current = os.lstat(candidate.absolute_path)
        if not stat.S_ISDIR(current.st_mode) or not _same_identity(
            current, candidate.file_stat
        ):
            raise CommandError("unsafe_orphan_entry")
        os.rmdir(candidate.absolute_path)
    except CommandError:
        raise
    except OSError as error:
        raise CommandError("unsafe_orphan_entry: {}".format(error))


def _is_canonical_uuid(value):
    try:
        return str(uuid.UUID(value)) == value
    except (TypeError, ValueError, AttributeError):
        return False
