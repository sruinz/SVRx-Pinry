from django.urls import include, re_path

from users.views import login_user
from . import views


app_name = "users"
urlpatterns = [
    re_path(r'', include(views.drf_router.urls)),
    re_path(r'^login/$', login_user, name='login'),
    re_path(r'^logout/$', views.logout_user, name='logout'),
]
