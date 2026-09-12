from django.urls import path

from users.sso import views
from users.sso.lan_recovery import recovery_login


app_name = 'sso'
urlpatterns = [
    path('recovery/', recovery_login, name='lan-recovery'),
    path('providers/', views.providers, name='providers'),
    path('password/reauth/', views.password_reauth, name='password-reauth'),
    path('identities/', views.identities, name='identities'),
    path('identities/<int:identity_id>/unlink/', views.unlink, name='unlink'),
    path('<uuid:provider_id>/login/', views.start_login, name='login'),
    path('<uuid:provider_id>/callback/', views.callback, name='callback'),
    path('<uuid:provider_id>/link/', views.start_link, name='link'),
    path('<uuid:provider_id>/reauth/', views.start_reauth, name='reauth'),
]
