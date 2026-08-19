from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from django_images.file_ops import MediaPathError, resolve_data_path
from django_images.services.media_cleanup import LegacyMediaCleaner
from django_images.services.media_migration import ManifestLog


class Command(BaseCommand):
    help = "List verified legacy media (dry-run by default; use --execute)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--manifest",
            required=True,
            help="successful migration JSONL manifest inside PINRY_DATA_ROOT",
        )
        parser.add_argument(
            "--execute",
            action="store_true",
            help="unlink verified legacy files",
        )
        parser.add_argument(
            "--confirm-full-data-backup", action="store_true"
        )
        parser.add_argument(
            "--confirm-pinry-immich-validated", action="store_true"
        )
        parser.add_argument(
            "--confirm-db-rollback-needs-media-backup",
            action="store_true",
        )

    def handle(self, *args, **options):
        if options["execute"] and not all(
            (
                options["confirm_full_data_backup"],
                options["confirm_pinry_immich_validated"],
                options["confirm_db_rollback_needs_media_backup"],
            )
        ):
            raise CommandError("cleanup_acknowledgements_required")
        try:
            manifest_path = resolve_data_path(
                options["manifest"], settings.PINRY_DATA_ROOT
            )
        except MediaPathError as error:
            raise CommandError(str(error))
        summary = LegacyMediaCleaner(ManifestLog(manifest_path)).run(
            execute=options["execute"]
        )
        self._print_summary(summary)

    def _print_summary(self, summary):
        for path in summary.candidate_paths:
            self.stdout.write("candidate: {}".format(path))
        self.stdout.write(
            "candidates={} deleted={}".format(
                summary.candidates, summary.deleted
            )
        )
