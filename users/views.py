import json

from django.core.exceptions import PermissionDenied
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseRedirect, HttpResponseBadRequest, HttpResponse
from django.urls import reverse
from django.utils.functional import lazy
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import mixins, routers
from rest_framework.permissions import BasePermission
from rest_framework.renderers import JSONRenderer
from rest_framework.viewsets import GenericViewSet

from users.models import User
from users.serializers import CurrentUserSerializer, PublicUserSerializer
from users.sso.policy import password_login_allowed, registration_allowed
from users.sso.flows import mark_recent_auth


def reverse_lazy(name=None, *args):
    return lazy(reverse, str)(name, args=args)


class PublicUserViewSet(
    mixins.RetrieveModelMixin,
    mixins.ListModelMixin,
    GenericViewSet,
):
    serializer_class = PublicUserSerializer
    filter_backends = (DjangoFilterBackend, )
    filterset_fields = ("username", )
    pagination_class = None

    def get_queryset(self):
        if self.action == "retrieve":
            return User.objects.all()
        username = self.request.GET.get("username", "")
        return User.objects.filter(username=username)


class UserViewSet(
    mixins.RetrieveModelMixin,
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    GenericViewSet,
):
    class Permission(BasePermission):
        def has_permission(self, request, view):
            if not request.method == "POST":
                return True
            return registration_allowed(request)

        def has_object_permission(self, request, view, obj):
            return request.user == obj

    permission_classes = [Permission, ]
    serializer_class = CurrentUserSerializer
    pagination_class = None

    def get_queryset(self):
        if self.request.user.is_anonymous:
            return User.objects.none()
        return User.objects.filter(id=self.request.user.id)


def login_user(request):
    if not password_login_allowed(request):
        raise PermissionDenied('비밀번호 로그인이 비활성화되어 있습니다.')
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return HttpResponseBadRequest()
    if 'username' not in data:
        return HttpResponseBadRequest(
            json.dumps({"username": "this field is required"})
        )
    if 'password' not in data:
        return HttpResponseBadRequest(
            json.dumps({"password": "this field is required"})
        )
    user = authenticate(
        request,
        username=data['username'],
        password=data['password']
    )
    if not user:
        return HttpResponseBadRequest(
            json.dumps({"password": "username and password doesn't match"})
        )
    if request.user.is_authenticated and request.user.pk != user.pk:
        raise PermissionDenied('로그인한 계정을 변경할 수 없습니다.')
    if not request.user.is_authenticated:
        login(request, user)
        request.session['auth_method'] = 'password'
    mark_recent_auth(request, 'password')
    current_user = User.objects.get(pk=user.pk)
    data = CurrentUserSerializer(
        current_user,
        context={'request': request},
    ).data
    return HttpResponse(
        JSONRenderer().render(data),
        content_type="application/json"
    )


@login_required
def logout_user(request):
    logout(request)
    messages.success(request, 'You have successfully logged out.')
    return HttpResponseRedirect('/')


drf_router = routers.DefaultRouter()
drf_router.register(r'users', UserViewSet, basename="user")
drf_router.register(r'public-users', PublicUserViewSet, basename="public-user")
