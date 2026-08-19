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

    def handle(self, *args, **options):
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

        execute = options["execute"]
        if execute:
            with migration_lock(data_root):
                self._run(manifest_path, options, run_id)
        else:
            self._run(manifest_path, options, run_id)
        self.stdout.write(manifest_path)

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
