from dataclasses import dataclass
from datetime import timedelta
import hashlib
import math
import os
import stat
import time
import uuid
import warnings

from django.conf import settings
from django.core.management import CommandError
from django.db import DatabaseError
from django.db.models import Q
from PIL import Image as PILImage

from django_images.file_ops import (
    MediaPathError,
    _open_directory,
    _relative_components,
    _unlink_owned_name,
    media_lifecycle_lock,
    open_media_root,
    sha256_file_descriptor,
)
from django_images.models import Image, Thumbnail
from django_images.paths import DERIVATIVE_NAMES, FORMAT_EXTENSIONS
from django_images.services.media_migration import MigrationPlan, _current_paths


ORPHAN_ROOTS = (".staging", "originals", "derivatives")
MINIMUM_AGE_SECONDS = 24 * 60 * 60
_STAGING_KINDS = frozenset((
    "original.part", "thumbnail.part", "standard.part", "square.part",
))
_UUID_BATCH_SIZE = 400
_STAGING_BATCH_SIZE = 100
_NANOSECONDS_PER_SECOND = 1000000000
_FINAL_EXTENSIONS = frozenset(FORMAT_EXTENSIONS.values())
_SAFE_ORPHAN_CODES = frozenset((
    "invalid_orphan_age_setting",
    "media_path_escape",
    "orphan_age_too_short",
    "orphan_cleanup_failed",
    "orphan_cleanup_lock_failed",
    "orphan_database_unavailable",
    "unsafe_media_root",
    "unsafe_orphan_entry",
))


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
class _CleanupClosure(object):
    kind: str
    key: str
    files: tuple
    directories: tuple
    logical_paths: tuple = ()

    @property
    def entries(self):
        return self.files + self.directories

    @property
    def candidate_paths(self):
        return tuple(entry.relative_path for entry in self.entries)


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

    def run(self, execute=False, older_than=None):
        try:
            older_than = _orphan_age(older_than)
            cutoff_ns = _wall_time_ns() - int(
                older_than.total_seconds() * _NANOSECONDS_PER_SECOND
            )
            root_directory = open_media_root(self.media_root)
            try:
                deleted = 0
                paths = []
                for closure in self._candidate_closures(
                    root_directory.descriptor, cutoff_ns
                ):
                    paths.extend(closure.candidate_paths)
                    if execute:
                        deleted += self._execute_closure(
                            root_directory, closure, cutoff_ns
                        )
                return CleanupSummary(tuple(sorted(paths)), deleted)
            finally:
                root_directory.close()
        except DatabaseError:
            raise CommandError("orphan_database_unavailable") from None
        except MediaPathError:
            raise CommandError("orphan_cleanup_lock_failed") from None
        except CommandError as error:
            raise _safe_orphan_command_error(error) from None
        except OSError:
            raise CommandError("orphan_cleanup_failed") from None

    def _candidate_closures(self, root_descriptor, cutoff_ns):
        _validate_known_media_references(root_descriptor)
        roots = _open_orphan_roots(root_descriptor)
        try:
            _validate_orphan_structure(roots)
            staging = roots.get(".staging")
            if staging is not None:
                for batch in _staging_closure_batches(
                    _scan_staging_closures(staging[0], cutoff_ns)
                ):
                    protected = _protected_staging_closure_keys(batch)
                    for closure in batch:
                        if closure.key not in protected:
                            yield closure

            final_roots = {
                name: roots.get(name)
                for name in ("originals", "derivatives")
            }
            for asset_uuids in _iter_final_uuid_pages(final_roots):
                protected = _protected_final_uuids(asset_uuids)
                for asset_uuid in asset_uuids:
                    if asset_uuid in protected:
                        continue
                    closure = _scan_final_closure(
                        final_roots, asset_uuid, cutoff_ns
                    )
                    if closure is not None:
                        yield closure
        finally:
            _close_orphan_roots(roots)

    def _execute_closure(self, root_directory, closure, cutoff_ns):
        try:
            with media_lifecycle_lock(root_directory, exclusive=True):
                if _closure_is_referenced(closure):
                    return 0
                current = _rescan_closure(
                    root_directory.descriptor, closure, cutoff_ns
                )
                if current is None or not _same_closure(
                    closure, current
                ):
                    return 0
                for candidate in current.files:
                    _unlink_file(root_directory.descriptor, candidate)
                for candidate in current.directories:
                    _remove_empty_directory(
                        root_directory.descriptor, candidate
                    )
                return len(current.entries)
        except MediaPathError:
            raise CommandError("orphan_cleanup_lock_failed") from None


def _wall_time_ns():
    time_ns = getattr(time, "time_ns", None)
    if callable(time_ns):
        return time_ns()
    return int(time.time() * _NANOSECONDS_PER_SECOND)


def _safe_orphan_command_error(error):
    code = str(error).split(":", 1)[0]
    if code not in _SAFE_ORPHAN_CODES:
        code = "orphan_cleanup_failed"
    return CommandError(code)


def _open_orphan_roots(root_descriptor):
    roots = {}
    try:
        for root_name in ORPHAN_ROOTS:
            opened = _open_child_directory(
                root_descriptor, root_name, missing_ok=True
            )
            if opened is not None:
                roots[root_name] = opened
        return roots
    except BaseException:
        _close_orphan_roots(roots)
        raise


def _close_orphan_roots(roots):
    first_error = None
    for descriptor, _file_stat in roots.values():
        try:
            os.close(descriptor)
        except BaseException as error:
            if first_error is None:
                first_error = error
    if first_error is not None:
        raise first_error


def _iter_directory_names(directory_descriptor):
    try:
        with os.scandir(directory_descriptor) as entries:
            for entry in entries:
                _validate_entry_name(entry.name)
                yield entry.name
    except CommandError:
        raise
    except OSError:
        raise CommandError("unsafe_orphan_entry")


def _validate_orphan_structure(roots):
    staging = roots.get(".staging")
    if staging is not None:
        _validate_staging_structure(staging[0])
    for root_name in ("originals", "derivatives"):
        opened = roots.get(root_name)
        if opened is not None:
            _validate_final_structure(root_name, opened[0])


def _validate_staging_structure(staging_descriptor):
    for entry_name in _iter_directory_names(staging_descriptor):
        entry_stat = _stat_entry(staging_descriptor, entry_name)
        if stat.S_ISLNK(entry_stat.st_mode):
            raise CommandError("media_path_escape")
        relative_path = ".staging/{}".format(entry_name)
        if stat.S_ISREG(entry_stat.st_mode):
            if not _is_legacy_staging_path(relative_path):
                raise CommandError("unsafe_orphan_entry")
            continue
        if not stat.S_ISDIR(entry_stat.st_mode) or not _is_canonical_uuid(
            entry_name
        ):
            raise CommandError("unsafe_orphan_entry")
        run_descriptor = None
        try:
            run_descriptor, _run_stat = _open_child_directory(
                staging_descriptor, entry_name
            )
            for asset_name in _iter_directory_names(run_descriptor):
                asset_stat = _stat_entry(run_descriptor, asset_name)
                if stat.S_ISLNK(asset_stat.st_mode):
                    raise CommandError("media_path_escape")
                if (
                    not stat.S_ISDIR(asset_stat.st_mode)
                    or not _is_canonical_uuid(asset_name)
                ):
                    raise CommandError("unsafe_orphan_entry")
                asset_descriptor = None
                try:
                    asset_descriptor, _asset_stat = _open_child_directory(
                        run_descriptor, asset_name
                    )
                    _scan_staging_files(
                        asset_descriptor, entry_name, asset_name
                    )
                finally:
                    if asset_descriptor is not None:
                        os.close(asset_descriptor)
        finally:
            if run_descriptor is not None:
                os.close(run_descriptor)


def _validate_final_structure(root_name, root_descriptor):
    for asset_uuid in _iter_directory_names(root_descriptor):
        entry_stat = _stat_entry(root_descriptor, asset_uuid)
        if stat.S_ISLNK(entry_stat.st_mode):
            raise CommandError("media_path_escape")
        if not stat.S_ISDIR(entry_stat.st_mode):
            continue
        if not _is_canonical_uuid(asset_uuid):
            continue
        asset_descriptor = None
        try:
            asset_descriptor, _asset_stat = _open_child_directory(
                root_descriptor, asset_uuid
            )
            _scan_final_files(root_name, asset_uuid, asset_descriptor)
        finally:
            if asset_descriptor is not None:
                os.close(asset_descriptor)


def _scan_staging_files(directory_descriptor, run_uuid, asset_uuid):
    files = []
    for entry_name in _iter_directory_names(directory_descriptor):
        entry_stat = _stat_entry(directory_descriptor, entry_name)
        if stat.S_ISLNK(entry_stat.st_mode):
            raise CommandError("media_path_escape")
        if (
            not stat.S_ISREG(entry_stat.st_mode)
            or entry_name not in _STAGING_KINDS
        ):
            raise CommandError("unsafe_orphan_entry")
        files.append(_FileCandidate(
            ".staging/{}/{}/{}".format(
                run_uuid, asset_uuid, entry_name
            ),
            entry_stat,
        ))
    files.sort(key=lambda candidate: candidate.relative_path)
    return tuple(files)


def _scan_final_files(root_name, asset_uuid, directory_descriptor):
    files = []
    slots = set()
    for entry_name in _iter_directory_names(directory_descriptor):
        entry_stat = _stat_entry(directory_descriptor, entry_name)
        if stat.S_ISLNK(entry_stat.st_mode):
            raise CommandError("media_path_escape")
        if not stat.S_ISREG(entry_stat.st_mode):
            raise CommandError("unsafe_orphan_entry")
        stem, extension = os.path.splitext(entry_name)
        valid_slot = (
            stem == "original"
            if root_name == "originals"
            else stem in DERIVATIVE_NAMES
        )
        if not valid_slot or extension not in _FINAL_EXTENSIONS:
            raise CommandError("unsafe_orphan_entry")
        if stem in slots:
            raise CommandError("unsafe_orphan_entry")
        slots.add(stem)
        files.append(_FileCandidate(
            "{}/{}/{}".format(root_name, asset_uuid, entry_name),
            entry_stat,
            extension,
        ))
    files.sort(key=lambda candidate: candidate.relative_path)
    return tuple(files)


def _scan_staging_closures(staging_descriptor, cutoff_ns):
    for entry_name in sorted(_iter_directory_names(staging_descriptor)):
        entry_stat = _stat_entry(staging_descriptor, entry_name)
        relative_path = ".staging/{}".format(entry_name)
        if stat.S_ISREG(entry_stat.st_mode):
            if _mtime_ns(entry_stat) < cutoff_ns:
                yield _CleanupClosure(
                    "flat",
                    relative_path,
                    (_FileCandidate(relative_path, entry_stat),),
                    (),
                    (relative_path,),
                )
            continue

        run_descriptor = None
        try:
            run_descriptor, run_stat = _open_child_directory(
                staging_descriptor, entry_name
            )
            asset_names = sorted(_iter_directory_names(run_descriptor))
            if not asset_names:
                if _mtime_ns(run_stat) < cutoff_ns:
                    yield _CleanupClosure(
                        "run",
                        relative_path,
                        (),
                        (_DirectoryCandidate(relative_path, run_stat),),
                    )
                continue
            for asset_name in asset_names:
                asset_descriptor = None
                try:
                    asset_descriptor, asset_stat = _open_child_directory(
                        run_descriptor, asset_name
                    )
                    files = _scan_staging_files(
                        asset_descriptor, entry_name, asset_name
                    )
                finally:
                    if asset_descriptor is not None:
                        os.close(asset_descriptor)
                asset_path = ".staging/{}/{}".format(
                    entry_name, asset_name
                )
                entries = files + (
                    _DirectoryCandidate(asset_path, asset_stat),
                )
                if any(
                    _mtime_ns(candidate.file_stat) >= cutoff_ns
                    for candidate in entries
                ):
                    continue
                directories = [entries[-1]]
                if (
                    len(asset_names) == 1
                    and _mtime_ns(run_stat) < cutoff_ns
                ):
                    directories.append(
                        _DirectoryCandidate(relative_path, run_stat)
                    )
                logical_paths = tuple(
                    "{}/{}".format(asset_path, slot)
                    for slot in sorted(_STAGING_KINDS)
                )
                yield _CleanupClosure(
                    "staging",
                    asset_path,
                    files,
                    tuple(directories),
                    logical_paths,
                )
        finally:
            if run_descriptor is not None:
                os.close(run_descriptor)


def _staging_closure_batches(closures):
    batch = []
    logical_path_count = 0
    for closure in closures:
        closure_path_count = len(closure.logical_paths)
        if batch and (
            len(batch) >= _STAGING_BATCH_SIZE
            or logical_path_count + closure_path_count > _UUID_BATCH_SIZE
        ):
            yield tuple(batch)
            batch = []
            logical_path_count = 0
        batch.append(closure)
        logical_path_count += closure_path_count
    if batch:
        yield tuple(batch)


def _next_final_uuid_page(final_roots, cursor):
    selected = []
    selected_set = set()
    for opened in final_roots.values():
        if opened is None:
            continue
        descriptor = opened[0]
        for entry_name in _iter_directory_names(descriptor):
            if entry_name <= cursor or not _is_canonical_uuid(entry_name):
                continue
            entry_stat = _stat_entry(descriptor, entry_name)
            if not stat.S_ISDIR(entry_stat.st_mode):
                continue
            if entry_name in selected_set:
                continue
            if len(selected) < _UUID_BATCH_SIZE:
                selected.append(entry_name)
                selected.sort()
                selected_set.add(entry_name)
            elif entry_name < selected[-1]:
                selected_set.remove(selected.pop())
                selected.append(entry_name)
                selected.sort()
                selected_set.add(entry_name)
    return tuple(selected)


def _iter_final_uuid_pages(final_roots):
    cursor = ""
    while True:
        page = _next_final_uuid_page(final_roots, cursor)
        if not page:
            return
        yield page
        cursor = page[-1]


def _scan_final_closure(final_roots, asset_uuid, cutoff_ns):
    files = []
    directories = []
    for root_name in ("originals", "derivatives"):
        opened = final_roots.get(root_name)
        if opened is None:
            continue
        opened_asset = _open_child_directory(
            opened[0], asset_uuid, missing_ok=True
        )
        if opened_asset is None:
            continue
        asset_descriptor, asset_stat = opened_asset
        try:
            files.extend(_scan_final_files(
                root_name, asset_uuid, asset_descriptor
            ))
        finally:
            os.close(asset_descriptor)
        directories.append(_DirectoryCandidate(
            "{}/{}".format(root_name, asset_uuid), asset_stat
        ))
    if not directories:
        return None
    entries = tuple(files) + tuple(directories)
    if any(
        _mtime_ns(candidate.file_stat) >= cutoff_ns
        for candidate in entries
    ):
        return None
    files.sort(key=lambda candidate: candidate.relative_path)
    directories.sort(
        key=lambda candidate: (
            -candidate.relative_path.count("/"),
            candidate.relative_path,
        )
    )
    return _CleanupClosure(
        "final", asset_uuid, tuple(files), tuple(directories)
    )


def _protected_staging_closure_keys(closures):
    by_path = {}
    for closure in closures:
        for path in closure.logical_paths:
            by_path[path] = closure.key
    if not by_path:
        return set()
    protected = set()
    paths = tuple(sorted(by_path))
    for model in (Image, Thumbnail):
        for relative_path in model.objects.filter(
            image__in=paths
        ).values_list("image", flat=True).iterator():
            key = by_path.get(relative_path)
            if key is not None:
                protected.add(key)
    return protected


def _protected_final_uuids(asset_uuids):
    asset_uuids = tuple(asset_uuids)
    if not asset_uuids:
        return set()
    candidate_set = set(asset_uuids)
    protected = {
        str(asset_uuid)
        for asset_uuid in Image.objects.filter(
            asset_uuid__in=asset_uuids
        ).values_list("asset_uuid", flat=True).iterator()
    }
    for model in (Image, Thumbnail):
        for root_name in ("originals", "derivatives"):
            path_filter = Q()
            for asset_uuid in asset_uuids:
                path_filter |= Q(image__startswith="{}/{}/".format(
                    root_name, asset_uuid
                ))
            references = model.objects.filter(path_filter).values_list(
                "image", flat=True
            ).iterator()
            for relative_path in references:
                parts = relative_path.split("/")
                if (
                    len(parts) >= 2
                    and parts[0] == root_name
                    and parts[1] in candidate_set
                ):
                    protected.add(parts[1])
    return protected


def _closure_is_referenced(closure):
    if closure.kind == "final":
        return closure.key in _protected_final_uuids((closure.key,))
    if closure.kind in ("staging", "flat"):
        return closure.key in _protected_staging_closure_keys((closure,))
    return False


def _rescan_closure(root_descriptor, closure, cutoff_ns):
    roots = _open_orphan_roots(root_descriptor)
    try:
        if closure.kind == "final":
            final_roots = {
                name: roots.get(name)
                for name in ("originals", "derivatives")
            }
            return _scan_final_closure(
                final_roots, closure.key, cutoff_ns
            )
        staging = roots.get(".staging")
        if staging is None:
            return None
        return _rescan_staging_closure(
            staging[0], closure, cutoff_ns
        )
    finally:
        _close_orphan_roots(roots)


def _rescan_staging_closure(
    staging_descriptor, closure, cutoff_ns
):
    parts = closure.key.split("/")
    if closure.kind == "flat":
        if len(parts) != 2 or not _is_legacy_staging_path(closure.key):
            return None
        file_stat = _optional_entry_stat(staging_descriptor, parts[1])
        if file_stat is None:
            return None
        if stat.S_ISLNK(file_stat.st_mode):
            raise CommandError("media_path_escape")
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or _mtime_ns(file_stat) >= cutoff_ns
        ):
            return None
        return _CleanupClosure(
            "flat",
            closure.key,
            (_FileCandidate(closure.key, file_stat),),
            (),
            (closure.key,),
        )

    if len(parts) not in (2, 3) or parts[0] != ".staging":
        return None
    opened_run = _open_child_directory(
        staging_descriptor, parts[1], missing_ok=True
    )
    if opened_run is None:
        return None
    run_descriptor, run_stat = opened_run
    try:
        asset_names = sorted(_iter_directory_names(run_descriptor))
        if closure.kind == "run":
            if (
                len(parts) != 2
                or asset_names
                or _mtime_ns(run_stat) >= cutoff_ns
            ):
                return None
            return _CleanupClosure(
                "run",
                closure.key,
                (),
                (_DirectoryCandidate(closure.key, run_stat),),
            )
        if (
            closure.kind != "staging"
            or len(parts) != 3
            or parts[2] not in asset_names
        ):
            return None
        opened_asset = _open_child_directory(
            run_descriptor, parts[2], missing_ok=True
        )
        if opened_asset is None:
            return None
        asset_descriptor, asset_stat = opened_asset
        try:
            files = _scan_staging_files(
                asset_descriptor, parts[1], parts[2]
            )
        finally:
            os.close(asset_descriptor)
        asset_path = ".staging/{}/{}".format(parts[1], parts[2])
        entries = files + (
            _DirectoryCandidate(asset_path, asset_stat),
        )
        if any(
            _mtime_ns(candidate.file_stat) >= cutoff_ns
            for candidate in entries
        ):
            return None
        directories = [entries[-1]]
        if len(asset_names) == 1 and _mtime_ns(run_stat) < cutoff_ns:
            directories.append(_DirectoryCandidate(
                ".staging/{}".format(parts[1]), run_stat
            ))
        return _CleanupClosure(
            "staging",
            asset_path,
            files,
            tuple(directories),
            tuple(
                "{}/{}".format(asset_path, slot)
                for slot in sorted(_STAGING_KINDS)
            ),
        )
    finally:
        os.close(run_descriptor)


def _optional_entry_stat(directory_descriptor, name):
    _validate_entry_name(name)
    try:
        return os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return None
    except OSError:
        raise CommandError("unsafe_orphan_entry")


def _same_closure(expected, current):
    if expected.kind != current.kind or expected.key != current.key:
        return False
    expected_entries = {
        entry.relative_path: entry.file_stat for entry in expected.entries
    }
    current_entries = {
        entry.relative_path: entry.file_stat for entry in current.entries
    }
    return (
        set(expected_entries) == set(current_entries)
        and all(
            _same_receipt(expected_entries[path], current_entries[path])
            for path in expected_entries
        )
    )


def _mtime_ns(file_stat):
    value = getattr(file_stat, "st_mtime_ns", None)
    if value is not None:
        return value
    return int(file_stat.st_mtime * _NANOSECONDS_PER_SECOND)


def _orphan_age(older_than):
    configured_seconds = getattr(
        settings,
        "PINRY_ORPHAN_MIN_AGE_SECONDS",
        MINIMUM_AGE_SECONDS,
    )
    if not _is_valid_orphan_age(configured_seconds):
        raise CommandError("invalid_orphan_age_setting")
    if older_than is None:
        seconds = configured_seconds
    elif isinstance(older_than, timedelta):
        seconds = older_than.total_seconds()
    else:
        seconds = older_than
    if not _is_valid_orphan_age(seconds) or seconds < configured_seconds:
        raise CommandError("orphan_age_too_short")
    return timedelta(seconds=seconds)


def _is_valid_orphan_age(seconds):
    if (
        isinstance(seconds, bool)
        or not isinstance(seconds, (int, float))
        or seconds < MINIMUM_AGE_SECONDS
    ):
        return False
    try:
        return math.isfinite(seconds)
    except (TypeError, OverflowError):
        return False


def safe_cleanup_display(relative_path):
    try:
        encoded = relative_path.encode("utf-8", "surrogatepass")
    except (AttributeError, UnicodeError):
        encoded = b"invalid"
    return "candidate-{}".format(
        hashlib.sha256(encoded).hexdigest()[:16]
    )


def _same_receipt(first, second):
    return (
        _same_identity(first, second)
        and first.st_size == second.st_size
        and _mtime_value(first) == _mtime_value(second)
    )


def _mtime_value(file_stat):
    return getattr(file_stat, "st_mtime_ns", file_stat.st_mtime)


def _current_media_references(root_descriptor):
    references = set(Image.objects.values_list("image", flat=True))
    references.update(Thumbnail.objects.values_list("image", flat=True))
    for relative_path in references:
        _validate_reference_path(root_descriptor, relative_path)
    return references


def _validate_known_media_references(root_descriptor):
    for model in (Image, Thumbnail):
        for relative_path in model.objects.values_list(
            "image", flat=True
        ).iterator():
            _validate_reference_path(root_descriptor, relative_path)


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


def _is_legacy_staging_path(relative_path):
    parts = relative_path.split("/")
    if len(parts) != 2 or parts[0] != ".staging":
        return False
    prefix = "media-migration-"
    suffix = ".part"
    name = parts[1]
    return (
        name.startswith(prefix)
        and name.endswith(suffix)
        and _is_canonical_uuid(name[len(prefix):-len(suffix)])
    )


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
        os.fsync(parent_descriptor)
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
        os.fsync(parent_descriptor)
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
