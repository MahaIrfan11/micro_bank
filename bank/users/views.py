from rest_framework import generics, permissions
from rest_framework.response import Response

from .models import User
from .serializers import (
    RegisterSerializer,
    UserSerializer,
    UserUpdateSerializer,
    AdminUserSerializer,
)


class RegisterView(generics.CreateAPIView):
    queryset = User.objects.all()
    serializer_class = RegisterSerializer
    permission_classes = [permissions.AllowAny]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(UserSerializer(user).data, status=201)


class MeView(generics.RetrieveUpdateDestroyAPIView):
    """Self-service only. There is no path from this view to reach
    another user's data -- get_object always returns request.user."""

    permission_classes = [permissions.IsAuthenticated]

    def get_object(self):
        return self.request.user

    def get_serializer_class(self):
        if self.request.method in ("PUT", "PATCH"):
            return UserUpdateSerializer
        return UserSerializer

    def perform_destroy(self, instance):
        instance.soft_delete()


class AdminUserListView(generics.ListAPIView):
    """Staff only. Lists active users by default;
    ?include_deleted=true also returns soft-deleted accounts."""

    serializer_class = AdminUserSerializer
    permission_classes = [permissions.IsAdminUser]

    def get_queryset(self):
        qs = User.objects.all().order_by("-date_joined")
        if self.request.query_params.get("include_deleted") != "true":
            qs = qs.filter(is_deleted=False)
        return qs


class AdminUserDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Staff only. Full read/edit/soft-delete on any user, looked up
    by their public bank_user_id (never the internal UUID pk)."""

    queryset = User.objects.all()
    lookup_field = "bank_user_id"
    permission_classes = [permissions.IsAdminUser]

    def get_serializer_class(self):
        if self.request.method in ("PUT", "PATCH"):
            return UserUpdateSerializer
        return AdminUserSerializer

    def perform_destroy(self, instance):
        instance.soft_delete()
