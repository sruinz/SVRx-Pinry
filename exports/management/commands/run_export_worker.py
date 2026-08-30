import signal
import threading

from django.core.management.base import BaseCommand, CommandError

from exports.services.worker import ExportWorker


class Command(BaseCommand):
    help = "Pin/Board 원본 내보내기 작업자를 실행합니다."

    def handle(self, *args, **options):
        del args, options
        stopped = threading.Event()

        def request_stop(signum, frame):
            del signum, frame
            stopped.set()

        previous_term = signal.signal(signal.SIGTERM, request_stop)
        previous_int = signal.signal(signal.SIGINT, request_stop)
        try:
            status = ExportWorker().run(stopped.is_set)
        finally:
            signal.signal(signal.SIGTERM, previous_term)
            signal.signal(signal.SIGINT, previous_int)
        if status:
            raise CommandError("내보내기 작업자가 안전하게 시작되지 못했습니다.")
