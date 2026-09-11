from django.urls import include, path

from users.recovery import recovery_login, recovery_logout, recovery_settings


urlpatterns = [path('recovery/', include(([
    path('login/', recovery_login, name='login'),
    path('settings/', recovery_settings, name='settings'),
    path('logout/', recovery_logout, name='logout'),
], 'recovery')))]
