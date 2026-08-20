from datetime import timedelta
import math

from django.core.management.base import BaseCommand, CommandError

from django_images.services.media_cleanup import (
    OrphanMediaCleaner,
    safe_cleanup_display,
)


class Command(BaseCommand):
    help = "List old orphan media (dry-run by default; use --execute)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--execute",
            action="store_true",
            help="unlink candidates and remove their empty directories",
        )
        parser.add_argument(
            "--older-than-hours",
            type=float,
            default=None,
            help="minimum orphan age in hours (default: configured minimum)",
        )

    def handle(self, *args, **options):
        hours = options["older_than_hours"]
        if hours is not None and not math.isfinite(hours):
            raise CommandError("invalid_orphan_age")
        try:
            summary = OrphanMediaCleaner().run(
                execute=options["execute"],
                older_than=(
                    timedelta(hours=hours) if hours is not None else None
                ),
            )
        except CommandError as error:
            raise CommandError(str(error).split(":", 1)[0]) from None
        for path in summary.candidate_paths:
            self.stdout.write(
                "candidate: {}".format(safe_cleanup_display(path))
            )
        self.stdout.write(
            "candidates={} deleted={}".format(
                summary.candidates, summary.deleted
            )
        )
