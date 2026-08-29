import hashlib

from django.contrib.auth.models import User as BaseUser
from django.db import models
from django.db.models.signals import post_save
from django.dispatch import receiver


def create_token_if_necessary(user: BaseUser):
    from rest_framework.authtoken.models import Token
    token = Token.objects.filter(user=user).first()
    if token is not None:
        return token
    else:
        return Token.objects.create(user=user)


class User(BaseUser):

    @property
    def gravatar(self):
        return hashlib.md5(self.email.encode('utf-8')).hexdigest()

    class Meta:
        proxy = True


class AdminBootstrapState(models.Model):
    bootstrap_complete = models.BooleanField(default=False)

    @classmethod
    def claim_for_initial_admin(cls):
        claimed = cls.objects.filter(
            pk=1,
            bootstrap_complete=False,
        ).update(bootstrap_complete=True)
        if claimed != 1:
            return False
        return not BaseUser.objects.filter(is_active=True).exists()


@receiver(post_save, sender=User)
def create_profile(sender, instance: User, **kwargs):
    create_token_if_necessary(instance)
