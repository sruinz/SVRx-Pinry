from django.contrib.auth.backends import BaseBackend

from users.models import User


class SSOBackend(BaseBackend):
    def get_user(self, user_id):
        return User.objects.filter(pk=user_id, is_active=True).first()
