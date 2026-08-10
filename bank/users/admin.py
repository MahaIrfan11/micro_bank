from django.contrib import admin
from django.contrib.auth.forms import UserChangeForm as BaseUserChangeForm
from django.contrib.auth.forms import UserCreationForm as BaseUserCreationForm
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import User


class UserCreationForm(BaseUserCreationForm):
    """Stock form hardcodes auth.User -- point it at our custom model."""

    class Meta(BaseUserCreationForm.Meta):
        model = User
        fields = ("email", "phone_number", "first_name", "last_name", "cnic", "passport_number")


class UserChangeForm(BaseUserChangeForm):
    class Meta(BaseUserChangeForm.Meta):
        model = User
        fields = "__all__"


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    """No username field -- fieldsets rebuilt around email."""

    model = User
    add_form = UserCreationForm
    form = UserChangeForm
    ordering = ["-date_joined"]

    list_display = [
        "bank_user_id", "email", "phone_number", "first_name", "last_name",
        "is_active", "is_staff", "is_deleted",
    ]
    list_filter = ["is_active", "is_staff", "is_deleted"]
    search_fields = ["email", "bank_user_id", "phone_number", "cnic", "passport_number"]
    readonly_fields = ["id", "bank_user_id", "date_joined", "last_login", "deleted_at"]

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Personal info", {
            "fields": ("first_name", "last_name", "phone_number", "cnic", "passport_number"),
        }),
        ("Status", {
            "fields": (
                "is_active", "is_staff", "is_superuser", "is_deleted", "deleted_at",
                "groups", "user_permissions",
            ),
        }),
        ("Dates", {"fields": ("last_login", "date_joined")}),
        ("Internal", {"fields": ("id", "bank_user_id")}),
    )
    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": (
                "email", "phone_number", "first_name", "last_name",
                "cnic", "passport_number", "password1", "password2",
            ),
        }),
    )
