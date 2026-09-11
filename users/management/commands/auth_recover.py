import sys

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from users.models import AuthPolicy, normalize_cidrs


class Command(BaseCommand):
    help = '서버 셸에서 명시한 인증 정책만 복구합니다.'

    def add_arguments(self, parser):
        parser.add_argument('--allow-cidr', action='append')
        parser.add_argument('--enable-password-login', action='store_true')

    def handle(self, *args, **options):
        changes = {}
        if options['allow_cidr'] is not None:
            try:
                changes['recovery_allowed_cidrs'] = normalize_cidrs(options['allow_cidr'], private_only=True)
            except ValidationError as error:
                raise CommandError('올바른 RFC1918 또는 ULA CIDR을 입력하세요.') from error
        if options['enable_password_login']:
            changes['password_login_enabled'] = True
        if not changes:
            raise CommandError('--allow-cidr 또는 --enable-password-login이 필요합니다.')
        self.stdout.write('다음 명시 설정만 변경합니다:')
        if 'recovery_allowed_cidrs' in changes:
            self.stdout.write('복구 허용 CIDR 교체: ' + ', '.join(changes['recovery_allowed_cidrs']))
        if 'password_login_enabled' in changes:
            self.stdout.write('일반 비밀번호 로그인: 허용')
        self.stdout.write('계속하려면 YES를 입력하세요.')
        if sys.stdin.readline().rstrip('\r\n') != 'YES':
            self.stdout.write('변경을 취소했습니다.')
            return
        with transaction.atomic():
            policy = AuthPolicy.objects.select_for_update().get(pk=1)
            changed = []
            for field, value in changes.items():
                if getattr(policy, field) != value:
                    setattr(policy, field, value)
                    changed.append(field)
            policy.full_clean()
            if changed:
                policy.revision += 1
                policy.save(update_fields=changed + ['revision'])
        self.stdout.write('인증 정책 복구를 저장했습니다.')
