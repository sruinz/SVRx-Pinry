import re
from django.core.exceptions import PermissionDenied

from users.models import User


email_re = re.compile(
    r"(^[-!#$%&'*+/=?^_`{}|~0-9A-Z]+(\.[-!#$%&'*+/=?^_`{}|~0-9A-Z]+)*"  # dot-atom
    # quoted-string, see also http://tools.ietf.org/html/rfc2822#section-3.2.5
    r'|^"([\001-\010\013\014\016-\037!#-\[\]-\177]|\\[\001-\011\013\014\016-\177])*"'
    r')@((?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+(?:[A-Z]{2,6}\.?|[A-Z0-9-]{2,}\.?)$)'
    # domain
    r'|\[(25[0-5]|2[0-4]\d|[0-1]?\d?\d)(\.(25[0-5]|2[0-4]\d|[0-1]?\d?\d)){3}\]$',
    re.IGNORECASE
)   # literal form, ipv4 address (SMTP 4.1.3)


class CombinedAuthBackend(object):
    def authenticate(self, request=None, username=None, password=None, **kwargs):
        from users.sso.policy import password_login_allowed
        if not password_login_allowed(request):
            raise PermissionDenied('비밀번호 로그인이 비활성화되어 있습니다.')
        if not isinstance(username, str):
            return None
        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            if not email_re.match(username):
                return None
            try:
                user = User.objects.get(email=username)
            except (User.DoesNotExist, User.MultipleObjectsReturned):
                return None
        if user.is_active and user.check_password(password):
            return user
        return None

    def get_user(self, user_id):
        try:
            return User.objects.get(pk=user_id, is_active=True)
        except User.DoesNotExist:
            return None
