from django.conf import settings
from django.contrib.auth import login
from django.db import transaction
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from rest_framework.exceptions import PermissionDenied

from users.models import AdminBootstrapState, User, create_token_if_necessary
from users.sso.policy import api_token_allowed, password_login_allowed


class PublicUserSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = User
        fields = (
            'username',
            'gravatar',
            settings.DRF_URL_FIELD_NAME,
        )
        extra_kwargs = {
            settings.DRF_URL_FIELD_NAME: {
                "view_name": "users:public-user-detail",
            },
        }


class CurrentUserSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = User
        fields = (
            'username',
            'token',
            'email',
            'gravatar',
            'can_access_admin',
            'password',
            'password_repeat',
            settings.DRF_URL_FIELD_NAME,
        )
        extra_kwargs = {
            settings.DRF_URL_FIELD_NAME: {
                "view_name": "users:user-detail",
            },
        }

    password = serializers.CharField(
        write_only=True,
        required=True,
        allow_blank=False,
        min_length=6,
        max_length=32,
    )
    password_repeat = serializers.CharField(
        write_only=True,
        required=True,
        allow_blank=False,
        min_length=6,
        max_length=32,
    )
    token = serializers.SerializerMethodField(read_only=True)
    can_access_admin = serializers.SerializerMethodField(read_only=True)

    def create(self, validated_data):
        if not password_login_allowed(self.context['request']):
            raise PermissionDenied('비밀번호 회원가입이 비활성화되어 있습니다.')
        if validated_data['password'] != validated_data['password_repeat']:
            raise ValidationError(
                detail={
                    "password_repeat": "Tow password doesn't match",
                }
            )
        validated_data.pop('password_repeat')
        password = validated_data.pop('password')
        with transaction.atomic():
            is_initial_admin = AdminBootstrapState.claim_for_initial_admin()
            if is_initial_admin:
                validated_data['is_staff'] = True
                validated_data['is_superuser'] = True
            user = super(CurrentUserSerializer, self).create(
                validated_data,
            )
            user.set_password(password)
            user.save()
        login(
            self.context['request'],
            user=user,
            backend=settings.AUTHENTICATION_BACKENDS[0],
        )
        self.context['request'].session['auth_method'] = 'password'
        return user

    def get_token(self, obj: User):
        request = self.context['request']
        if request.user == obj and api_token_allowed(request):
            token = create_token_if_necessary(obj, request)
            return token.key if token else None
        return None

    def get_can_access_admin(self, obj):
        return bool(obj.is_active and obj.is_staff)
