from django.contrib import admin, messages
from rest_framework.exceptions import ValidationError

from .models import User
from .views import _soft_delete_or_raise


@admin.register(User)
class UserAdmin(admin.ModelAdmin):

    list_display = [
        "email", "bank_user_id", "first_name", "last_name",
        "is_active", "is_staff", "is_superuser", "is_deleted", "date_joined",
    ]
    list_filter = ["is_active", "is_staff", "is_superuser", "is_deleted"]
    search_fields = ["email", "bank_user_id", "phone_number", "cnic", "passport_number"]

    readonly_fields = [
        "id", "bank_user_id", "password", "date_joined", "last_login",
        "is_deleted", "deleted_at",
    ]
    exclude = ["groups", "user_permissions"]
    actions = ["soft_delete_selected"]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description="Soft delete selected users (deactivate, never hard-delete)")
    def soft_delete_selected(self, request, queryset):
        deactivated, blocked = 0, []
        for user in queryset.filter(is_deleted=False):
            try:
                _soft_delete_or_raise(user)
                deactivated += 1
            except ValidationError:
                blocked.append(user.email)

        if blocked:
            self.message_user(
                request,
                "Cannot deactivate users who still hold an open account: "
                + ", ".join(blocked) + ". Close their accounts first.",
                level=messages.ERROR,
            )
        if deactivated:
            self.message_user(request, f"Deactivated {deactivated} user(s).", level=messages.SUCCESS)
