from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import os
import uuid

from django.conf import settings
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand, CommandError

from django_images.file_ops import (
    MediaPathError,
    migration_lock_path,
    resolve_data_path,
)
from django_images.services.media_migration import ManifestLog, MediaMigrator
from django_images.services.media_migration_v2 import AutoV2MediaMigrator


def _resolve_auto_v2_manifest_path(path, data_root):
    if (
        not isinstance(path, str)
        or not path
        or not isinstance(data_root, str)
        or not data_root
        or "\\" in path
    ):
        raise CommandError("manifest_path_escape")
    root = os.path.abspath(data_root)
    if os.path.isabs(path):
        raw_parts = path.split("/")[1:]
        candidate = path
    else:
        raw_parts = path.split("/")
        candidate = os.path.join(root, path)
    if any(part in ("", ".", "..") for part in raw_parts):
        raise CommandError("manifest_path_escape")
    try:
        inside = os.path.commonpath((root, candidate)) == root
    except ValueError:
        inside = False
    if not inside or candidate == root:
        raise CommandError("manifest_path_escape")
    return candidate


@contextmanager
def migration_lock(data_root):
    try:
        lock_path = migration_lock_path(data_root)
    except MediaPathError as error:
        raise CommandError(str(error))
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    lock_file = open(lock_path, "a+")
    try:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CommandError("media_migration_locked")
        yield
    finally:
        lock_file.close()


class Command(BaseCommand):
    help = "Plan media migration (dry-run by default; use --execute to write)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--execute",
            action="store_true",
            help="copy verified media and update database paths",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=100,
            help="images per database transaction (default: 100)",
        )
        parser.add_argument(
            "--manifest",
            help="JSONL audit manifest inside PINRY_DATA_ROOT",
        )
        parser.add_argument(
            "--target",
            choices=("fixed-v1", "auto-v2"),
            default="fixed-v1",
            help="migration target generation (default: fixed-v1)",
        )
        parser.add_argument(
            "--run-id",
            help="auto-v2 migration run identifier",
        )

    def handle(self, *args, **options):
        if options.get("target", "fixed-v1") == "auto-v2":
            return self._handle_auto_v2(options)
        data_root = settings.PINRY_DATA_ROOT
        run_id = None
        manifest_path = options.get("manifest")
        if not manifest_path:
            run_id = str(uuid.uuid4())
            manifest_path = self._default_manifest(data_root, run_id)
        try:
            manifest_path = resolve_data_path(manifest_path, data_root)
        except MediaPathError as error:
            raise CommandError(str(error))

        self.stdout.write(manifest_path)
        getattr(self.stdout, "_out", self.stdout).flush()
        execute = options["execute"]
        if execute:
            with migration_lock(data_root):
                self._run(manifest_path, options, run_id)
        else:
            self._run(manifest_path, options, run_id)

    def _handle_auto_v2(self, options):
        manifest_path = options.get("manifest")
        run_id = options.get("run_id")
        if not manifest_path:
            raise CommandError("auto_v2_manifest_required")
        if not run_id:
            raise CommandError("auto_v2_run_id_required")
        manifest_path = _resolve_auto_v2_manifest_path(
            manifest_path, settings.PINRY_DATA_ROOT
        )
        run_directory = os.path.dirname(manifest_path)
        filename = os.path.basename(manifest_path)

        def run():
            AutoV2MediaMigrator(
                run_directory,
                filename,
                run_id,
                os.geteuid(),
                os.getegid(),
                batch_size=options["batch_size"],
            ).run(execute=options["execute"])

        if options["execute"]:
            with migration_lock(settings.PINRY_DATA_ROOT):
                run()
        else:
            run()
        return None

    def _run(self, manifest_path, options, run_id):
        manifest = ManifestLog(manifest_path, run_id=run_id)
        MediaMigrator(
            default_storage,
            manifest,
            batch_size=options["batch_size"],
        ).run(execute=options["execute"])

    def _default_manifest(self, data_root, run_id):
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return os.path.join(
            data_root,
            "migrations",
            "media-migration-{}-{}.jsonl".format(timestamp, run_id),
        )
