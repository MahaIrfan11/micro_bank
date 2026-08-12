from django.contrib.auth import get_user_model
from rest_framework import serializers

from . import money
from .models import Account, Entry, Transfer

User = get_user_model()


class AccountSerializer(serializers.ModelSerializer):
    balance = serializers.SerializerMethodField()
    owner_email = serializers.EmailField(source="owner.email", read_only=True)
    owner_name = serializers.SerializerMethodField()
    # Staff-only: create the account for someone else instead of the caller.
    owner_bank_user_id = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = Account
        fields = [
            "account_number", "account_type", "owner_email", "owner_name", "balance", "currency",
            "is_active", "created_at", "owner_bank_user_id",
        ]
        read_only_fields = [
            "account_number", "owner_email", "owner_name", "balance", "currency",
            "is_active", "created_at",
        ]

    def get_balance(self, obj):
        return money.format_amount(obj.balance_minor)

    def get_owner_name(self, obj):
        return f"{obj.owner.first_name} {obj.owner.last_name}"

    def validate(self, attrs):
        bank_user_id = attrs.pop("owner_bank_user_id", None)
        if bank_user_id:
            request = self.context["request"]
            if not request.user.is_staff:
                raise serializers.ValidationError(
                    {"owner_bank_user_id": "Only staff can create an account for another user."}
                )
            try:
                attrs["owner_obj"] = User.objects.get(bank_user_id=bank_user_id, is_deleted=False)
            except User.DoesNotExist:
                raise serializers.ValidationError({"owner_bank_user_id": "No user with that bank_user_id."})
        return attrs


class EntrySerializer(serializers.ModelSerializer):
    amount = serializers.SerializerMethodField()
    balance_after = serializers.SerializerMethodField()
    transfer_id = serializers.SerializerMethodField()
    direction = serializers.SerializerMethodField()
    counterparty_account_number = serializers.SerializerMethodField()

    class Meta:
        model = Entry
        fields = [
            "id",
            "transfer_id",
            "direction",
            "amount",
            "balance_after",
            "counterparty_account_number",
            "created_at",
        ]
        read_only_fields = fields

    def get_amount(self, obj):
        return money.format_amount(abs(obj.amount_minor))

    def get_balance_after(self, obj):
        return money.format_amount(obj.balance_after_minor)

    def get_direction(self, obj):
        return "credit" if obj.amount_minor > 0 else "debit"

    def get_transfer_id(self, obj):
        # None for deposit-originated entries -- there's no Transfer to point at.
        return obj.transfer_id

    def get_counterparty_account_number(self, obj):
        if obj.transfer_id is None:
            # Deposit-originated -- money entering the system, no counterparty account.
            return "EXTERNAL"
        transfer = obj.transfer
        if obj.amount_minor > 0:
            return transfer.source_account.account_number
        return transfer.destination_account.account_number


class TransferSerializer(serializers.Serializer):
    """Validates shape + business rules only. Accounts aren't locked
    here -- the view locks them separately under SELECT FOR UPDATE."""

    source_account = serializers.CharField()
    destination_account = serializers.CharField()
    amount = serializers.CharField()

    def validate(self, attrs):
        request = self.context["request"]

        if attrs["source_account"] == attrs["destination_account"]:
            raise serializers.ValidationError("Cannot transfer to the same account.")

        try:
            source_account = Account.objects.get(
                account_number=attrs["source_account"], is_deleted=False
            )
        except Account.DoesNotExist:
            raise serializers.ValidationError({"source_account": "Account not found."})

        try:
            destination_account = Account.objects.get(
                account_number=attrs["destination_account"], is_deleted=False
            )
        except Account.DoesNotExist:
            raise serializers.ValidationError({"destination_account": "Account not found."})

        if source_account.owner_id != request.user.id:
            raise serializers.ValidationError(
                {"source_account": "You can only transfer from an account you own."}
            )

        if not source_account.is_active or not destination_account.is_active:
            raise serializers.ValidationError("Both accounts must be active.")

        try:
            amount_minor = money.to_minor_units(attrs["amount"])
        except ValueError as exc:
            raise serializers.ValidationError({"amount": str(exc)})

        attrs["source_account_obj"] = source_account
        attrs["destination_account_obj"] = destination_account
        attrs["amount_minor"] = amount_minor
        return attrs


class TransferResultSerializer(serializers.ModelSerializer):
    source_account = serializers.CharField(source="source_account.account_number", read_only=True)
    destination_account = serializers.CharField(
        source="destination_account.account_number", read_only=True
    )
    amount = serializers.SerializerMethodField()

    class Meta:
        model = Transfer
        fields = [
            "id",
            "status",
            "failure_reason",
            "source_account",
            "destination_account",
            "amount",
            "currency",
            "created_at",
        ]
        read_only_fields = fields

    def get_amount(self, obj):
        return money.format_amount(obj.amount_minor)


class DepositSerializer(serializers.Serializer):
    amount = serializers.CharField()

    def validate_amount(self, value):
        try:
            return money.to_minor_units(value)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc))
