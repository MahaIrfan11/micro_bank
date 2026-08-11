from rest_framework import generics, permissions
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import CursorPagination
from rest_framework.response import Response

from .models import User
from .serializers import (
    AdminUserCreateSerializer,
    AdminUserSerializer,
    RegisterSerializer,
    UserSerializer,
    UserUpdateSerializer,
)


def _soft_delete_or_raise(instance):
    """Refuse to delete a user who still holds any open account."""
    if instance.accounts.filter(is_deleted=False).exists():
        raise ValidationError(
            {"detail": "Cannot delete a user who still has an account. Close their accounts first."}
        )
    instance.soft_delete()


class RegisterView(generics.CreateAPIView):
    serializer_class = RegisterSerializer
    permission_classes = [permissions.AllowAny]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(UserSerializer(user).data, status=201)


class MeView(generics.RetrieveUpdateDestroyAPIView):
    """Self-service only -- get_object always returns request.user."""

    permission_classes = [permissions.IsAuthenticated]

    def get_object(self):
        return self.request.user

    def get_serializer_class(self):
        if self.request.method in ("PUT", "PATCH"):
            return UserUpdateSerializer
        return UserSerializer

    def perform_destroy(self, instance):
        _soft_delete_or_raise(instance)


class UserCursorPagination(CursorPagination):
    page_size = 25
    max_page_size = 100
    page_size_query_param = "page_size"
    ordering = ("-date_joined", "-id")  # -id breaks ties on identical timestamps


class AdminUserListView(generics.ListCreateAPIView):
    """Staff only. GET lists (?include_deleted=true shows soft-deleted too);
    POST lets staff create a user account directly."""

    permission_classes = [permissions.IsAdminUser]
    pagination_class = UserCursorPagination

    def get_serializer_class(self):
        if self.request.method == "POST":
            return AdminUserCreateSerializer
        return AdminUserSerializer

    def get_queryset(self):
        qs = User.objects.all().order_by("-date_joined")
        if self.request.query_params.get("include_deleted") != "true":
            qs = qs.filter(is_deleted=False)
        return qs

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(AdminUserSerializer(user).data, status=201)


class AdminUserDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Staff only. Full CRUD by bank_user_id, not the internal UUID."""

    queryset = User.objects.all()
    lookup_field = "bank_user_id"
    permission_classes = [permissions.IsAdminUser]

    def get_serializer_class(self):
        if self.request.method in ("PUT", "PATCH"):
            return UserUpdateSerializer
        return AdminUserSerializer

    def perform_destroy(self, instance):
        _soft_delete_or_raise(instance)
