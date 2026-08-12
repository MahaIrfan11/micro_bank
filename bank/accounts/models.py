import secrets
import uuid

from django.conf import settings
from django.db import IntegrityError, models, transaction


def generate_account_number():
    """12-digit random account number, unique, immutable."""
    return "".join(str(secrets.randbelow(10)) for _ in range(12))


class Account(models.Model):
    class AccountType(models.TextChoices):
        CURRENT = "CURRENT", "Current"
        SAVINGS = "SAVINGS", "Savings"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account_number = models.CharField(max_length=12, unique=True, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="accounts",
    )
    account_type = models.CharField(max_length=16, choices=AccountType.choices)

    currency = models.CharField(max_length=3, default="USD")
    balance_minor = models.BigIntegerField(default=0)

    is_active = models.BooleanField(default=True)
    is_deleted = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(balance_minor__gte=0),
                name="account_balance_never_negative",
            ),
            # One CURRENT + one SAVINGS per owner; scoped to non-deleted accounts.
            models.UniqueConstraint(
                fields=["owner", "account_type"],
                condition=models.Q(is_deleted=False),
                name="one_account_per_type_per_owner",
            ),
        ]

    def __str__(self):
        return f"{self.account_number} ({self.owner.email})"

    def save(self, *args, **kwargs):
        if self.account_number:
            super().save(*args, **kwargs)
            return

        # Retry on the actual DB collision rather than a racy pre-check
        # (same pattern as UserManager._create_user).
        for _ in range(5):
            self.account_number = generate_account_number()
            try:
                with transaction.atomic():
                    super().save(*args, **kwargs)
                return
            except IntegrityError as exc:
                constraint = getattr(getattr(exc.__cause__, "diag", None), "constraint_name", "") or ""
                if "account_number" in constraint:
                    continue
                raise

        raise RuntimeError("Could not generate a unique account_number after 5 attempts")


class Transfer(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        COMPLETED = "COMPLETED", "Completed"
        FAILED = "FAILED", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    idempotency_key = models.CharField(max_length=255, unique=True)
    request_hash = models.CharField(max_length=64)

    source_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="outgoing_transfers"
    )
    destination_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="incoming_transfers"
    )
    initiated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="initiated_transfers"
    )

    amount_minor = models.BigIntegerField()
    currency = models.CharField(max_length=3)

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    failure_reason = models.CharField(max_length=64, null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount_minor__gt=0),
                name="transfer_amount_positive",
            ),
            models.CheckConstraint(
                condition=~models.Q(source_account=models.F("destination_account")),
                name="transfer_cannot_be_to_self",
            ),
        ]

    def __str__(self):
        return f"{self.id} ({self.status})"


class Deposit(models.Model):
    """Idempotency record for deposits. Each successful deposit also gets a
    single-sided Entry (see below) so it's visible in transaction history."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    idempotency_key = models.CharField(max_length=255, unique=True)

    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="deposits")
    amount_minor = models.BigIntegerField()
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="deposits_made"
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount_minor__gt=0),
                name="deposit_amount_positive",
            ),
        ]

    def __str__(self):
        return f"{self.account.account_number}: +{self.amount_minor}"


class Entry(models.Model):
    """Append-only ledger line. Two per transfer (sum to zero), or one per
    deposit (money entering the system -- no counterparty to balance against)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    transfer = models.ForeignKey(
        Transfer, on_delete=models.PROTECT, related_name="entries", null=True, blank=True
    )
    deposit = models.ForeignKey(
        Deposit, on_delete=models.PROTECT, related_name="entries", null=True, blank=True
    )
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="entries")

    amount_minor = models.BigIntegerField()  # negative = debit, positive = credit
    balance_after_minor = models.BigIntegerField()

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["account", "-created_at"]),
        ]
        constraints = [
            # Every entry traces to exactly one origin -- a transfer or a deposit, never both/neither.
            models.CheckConstraint(
                condition=(
                    models.Q(transfer__isnull=False, deposit__isnull=True)
                    | models.Q(transfer__isnull=True, deposit__isnull=False)
                ),
                name="entry_has_exactly_one_origin",
            ),
        ]

    def __str__(self):
        return f"{self.account.account_number}: {self.amount_minor}"
