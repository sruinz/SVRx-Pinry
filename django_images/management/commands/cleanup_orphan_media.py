from datetime import timedelta
import math

from django.core.management.base import BaseCommand, CommandError

from django_images.services.media_cleanup import OrphanMediaCleaner


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
            default=24,
            help="minimum orphan age in hours (default: 24)",
        )

    def handle(self, *args, **options):
        hours = options["older_than_hours"]
        if not math.isfinite(hours):
            raise CommandError("invalid_orphan_age")
        summary = OrphanMediaCleaner().run(
            execute=options["execute"],
            older_than=timedelta(hours=hours),
        )
        for path in summary.candidate_paths:
            self.stdout.write("candidate: {}".format(path))
        self.stdout.write(
            "candidates={} deleted={}".format(
                summary.candidates, summary.deleted
            )
        )
