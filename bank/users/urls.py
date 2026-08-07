from django.urls import path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .views import (
    RegisterView,
    MeView,
    AdminUserListView,
    AdminUserDetailView,
)

urlpatterns = [
    path("signup/", RegisterView.as_view()),
    path("login/", TokenObtainPairView.as_view()),
    path("login/refresh/", TokenRefreshView.as_view()),
    path("me/", MeView.as_view()),
    path("", AdminUserListView.as_view()),
    path("<str:bank_user_id>/", AdminUserDetailView.as_view()),
]
