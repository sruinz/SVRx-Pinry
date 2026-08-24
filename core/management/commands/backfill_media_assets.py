import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core.services.media_asset_backfill import MediaAssetBackfiller
from django_images.file_ops import MediaPathError, resolve_data_path


class Command(BaseCommand):
    help = "레거시 미디어 레지스트리를 계획하고 선택적으로 등록합니다"

    def add_arguments(self, parser):
        parser.add_argument(
            "--execute",
            action="store_true",
            help="검증된 계획을 실제 레지스트리에 등록합니다",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=100,
            help="DB 조회 묶음 크기(기본값: 100)",
        )
        parser.add_argument(
            "--manifest",
            required=True,
            help="PINRY_DATA_ROOT 안의 보호된 JSONL manifest 경로",
        )
        parser.add_argument(
            "--run-id",
            required=True,
            help="manifest 상위 run directory와 같은 실행 식별자",
        )

    def handle(self, *args, **options):
        del args
        try:
            manifest_path = resolve_data_path(
                options["manifest"],
                settings.PINRY_DATA_ROOT,
            )
        except MediaPathError as error:
            raise CommandError(str(error))
        summary = MediaAssetBackfiller(
            os.path.dirname(manifest_path),
            os.path.basename(manifest_path),
            options["run_id"],
            os.geteuid(),
            os.getegid(),
            batch_size=options["batch_size"],
        ).run(execute=options["execute"])
        reasons = ",".join(
            "{}:{}".format(code, count)
            for code, count in sorted(summary.reason_counts.items())
        ) or "none"
        self.stdout.write(
            "scanned={} eligible={} registered={} "
            "already_registered={} skipped={} reasons={}".format(
                summary.scanned,
                summary.eligible,
                summary.registered,
                summary.already_registered,
                summary.skipped,
                reasons,
            )
        )
        return None
