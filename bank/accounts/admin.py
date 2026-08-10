from django.contrib import admin

from . import money
from .models import Account, Deposit, Entry, Transfer


class ReadOnlyAdminMixin:

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = [
        "account_number", "account_type", "owner_email", "balance_display", "currency",
        "is_active", "is_deleted", "created_at",
    ]
    list_filter = ["account_type", "is_active", "is_deleted", "currency"]
    search_fields = ["account_number", "owner__email"]
    # balance_minor stays readonly here -- the only sanctioned way to move
    # money is through TransferView/AccountDepositView, never a manual edit.
    readonly_fields = ["id", "account_number", "balance_minor", "created_at", "updated_at"]

    @admin.display(description="Owner")
    def owner_email(self, obj):
        return obj.owner.email

    @admin.display(description="Balance")
    def balance_display(self, obj):
        return money.format_amount(obj.balance_minor)


@admin.register(Transfer)
class TransferAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = [
        "id", "status", "source_account", "destination_account",
        "amount_display", "currency", "created_at",
    ]
    list_filter = ["status", "currency"]
    search_fields = [
        "id", "idempotency_key",
        "source_account__account_number", "destination_account__account_number",
    ]

    @admin.display(description="Amount")
    def amount_display(self, obj):
        return money.format_amount(obj.amount_minor)


@admin.register(Deposit)
class DepositAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ["id", "account", "amount_display", "created_by", "created_at"]
    search_fields = ["idempotency_key", "account__account_number"]

    @admin.display(description="Amount")
    def amount_display(self, obj):
        return money.format_amount(obj.amount_minor)


@admin.register(Entry)
class EntryAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = [
        "id", "account", "transfer", "amount_display", "balance_after_display", "created_at",
    ]
    search_fields = ["account__account_number", "transfer__id"]

    @admin.display(description="Amount")
    def amount_display(self, obj):
        return money.format_amount(obj.amount_minor)

    @admin.display(description="Balance after")
    def balance_after_display(self, obj):
        return money.format_amount(obj.balance_after_minor)
