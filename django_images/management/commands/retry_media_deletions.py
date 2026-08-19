from django.core.management.base import BaseCommand, CommandError
from django.db import DEFAULT_DB_ALIAS, connections

from django_images.models import PendingMediaDeletion
from django_images.services.media_deletion import (
    process_pending_media_deletion,
)


class Command(BaseCommand):
    help = "List pending media deletions (dry-run by default; use --execute)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--execute",
            action="store_true",
            help="retry deletion of the exact journaled storage names",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="maximum number of pending rows to inspect",
        )
        parser.add_argument(
            "--database",
            default=DEFAULT_DB_ALIAS,
            help="database alias containing the pending journal",
        )

    def handle(self, *args, **options):
        limit = options["limit"]
        database = options["database"]
        if limit is not None and limit <= 0:
            raise CommandError("invalid_limit")
        if database not in connections:
            raise CommandError(
                "unknown_database_alias: {}".format(database)
            )

        pending_deletions = PendingMediaDeletion.objects.using(database)
        queryset = pending_deletions.order_by("id")
        if limit is not None:
            queryset = queryset[:limit]
        pending_rows = list(
            queryset.values("id", "kind", "name", "attempts")
        )
        for pending in pending_rows:
            self.stdout.write(
                "pending: kind={} name={} attempts={}".format(
                    pending["kind"],
                    pending["name"],
                    pending["attempts"],
                )
            )

        processed = 0
        if options["execute"]:
            for pending in pending_rows:
                if process_pending_media_deletion(
                    pending["id"], using=database
                ):
                    processed += 1

        self.stdout.write(
            "pending={} processed={} remaining={}".format(
                len(pending_rows),
                processed,
                pending_deletions.count(),
            )
        )
