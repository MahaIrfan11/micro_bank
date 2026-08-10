import hashlib

from django.db import IntegrityError, transaction
from django.db.models import Q
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import generics, permissions, status
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import CursorPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from . import money
from .models import Account, Deposit, Entry, Transfer
from .serializers import (
    AccountSerializer,
    DepositSerializer,
    EntrySerializer,
    TransferResultSerializer,
    TransferSerializer,
)


class AccountListCreateView(generics.ListCreateAPIView):
    """Own accounts only; staff see every account (and can pass
    owner_bank_user_id to create one for someone else). New accounts
    always start at $0."""

    serializer_class = AccountSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = Account.objects.select_related("owner").filter(is_deleted=False)
        if not self.request.user.is_staff:
            qs = qs.filter(owner=self.request.user)
        return qs.order_by("-created_at")

    def perform_create(self, serializer):
        owner = serializer.validated_data.pop("owner_obj", None) or self.request.user
        try:
            serializer.save(owner=owner, balance_minor=0, currency=money.CURRENCY)
        except IntegrityError as exc:
            constraint = getattr(getattr(exc.__cause__, "diag", None), "constraint_name", "") or ""
            if constraint == "one_account_per_type_per_owner":
                account_type = serializer.validated_data.get("account_type")
                raise ValidationError(
                    {"account_type": f"This user already has a {account_type} account."}
                )
            raise


class AccountDetailView(generics.RetrieveAPIView):
    """Owner or staff. Non-owner gets 404, not 403 (no enumeration)."""

    serializer_class = AccountSerializer
    lookup_field = "account_number"
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = Account.objects.select_related("owner").filter(is_deleted=False)
        if not self.request.user.is_staff:
            qs = qs.filter(owner=self.request.user)
        return qs


class EntryCursorPagination(CursorPagination):
    page_size = 25
    max_page_size = 100
    page_size_query_param = "page_size"
    ordering = ("-created_at", "-id")  # -id breaks ties on identical timestamps


class AccountTransactionsView(generics.ListAPIView):
    """Owner or staff. Ledger history, newest first, cursor-paginated."""

    serializer_class = EntrySerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = EntryCursorPagination

    def get_queryset(self):
        account_number = self.kwargs["account_number"]
        accounts = Account.objects.filter(account_number=account_number, is_deleted=False)
        if not self.request.user.is_staff:
            accounts = accounts.filter(owner=self.request.user)
        account = accounts.first()
        if account is None:
            return Entry.objects.none()
        return (
            Entry.objects.filter(account=account)
            .select_related("transfer", "transfer__source_account", "transfer__destination_account")
            .order_by("-created_at")
        )


def _compute_request_hash(source_account_id, destination_account_id, amount_minor):
    raw = f"{source_account_id}:{destination_account_id}:{amount_minor}"
    return hashlib.sha256(raw.encode()).hexdigest()


class TransferView(APIView):
    """Idempotent, conservation-safe, concurrency-safe transfer."""

    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        request=TransferSerializer,
        responses={
            201: TransferResultSerializer,
            400: None,
            409: None,
            422: TransferResultSerializer,
        },
        parameters=[
            OpenApiParameter(
                name="Idempotency-Key",
                location=OpenApiParameter.HEADER,
                required=True,
                type=str,
                description="Client-generated key. Same key + same payload replays the "
                            "original result instead of moving money twice.",
            ),
        ],
    )
    def post(self, request):
        idempotency_key = request.headers.get("Idempotency-Key")
        if not idempotency_key:
            return Response(
                {"detail": "Idempotency-Key header is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = TransferSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)

        source_account = serializer.validated_data["source_account_obj"]
        destination_account = serializer.validated_data["destination_account_obj"]
        amount_minor = serializer.validated_data["amount_minor"]

        request_hash = _compute_request_hash(
            source_account.id, destination_account.id, amount_minor
        )

        with transaction.atomic():
            try:
                # Nested atomic() = savepoint; a failure here doesn't
                # poison the outer transaction.
                with transaction.atomic():
                    transfer = Transfer.objects.create(
                        idempotency_key=idempotency_key,
                        request_hash=request_hash,
                        source_account=source_account,
                        destination_account=destination_account,
                        initiated_by=request.user,
                        amount_minor=amount_minor,
                        currency=money.CURRENCY,
                        status=Transfer.Status.PENDING,
                    )
            except IntegrityError:
                # Key already used; conflicting row is guaranteed committed.
                existing = Transfer.objects.select_related(
                    "source_account", "destination_account"
                ).get(idempotency_key=idempotency_key)
                if existing.request_hash != request_hash:
                    return Response(
                        {"detail": "Idempotency-Key was already used with a different request."},
                        status=status.HTTP_409_CONFLICT,
                    )
                return Response(
                    TransferResultSerializer(existing).data,
                    status=self._status_for(existing.status),
                )

            # Lock both accounts in a fixed (id-ascending) order to avoid deadlock.
            locked = {
                a.id: a
                for a in Account.objects.select_for_update()
                .filter(id__in=[source_account.id, destination_account.id])
                .order_by("id")
            }
            locked_source = locked[source_account.id]
            locked_destination = locked[destination_account.id]

            if locked_source.balance_minor >= amount_minor:
                locked_source.balance_minor -= amount_minor
                locked_destination.balance_minor += amount_minor
                locked_source.save(update_fields=["balance_minor", "updated_at"])
                locked_destination.save(update_fields=["balance_minor", "updated_at"])

                Entry.objects.create(
                    transfer=transfer,
                    account=locked_source,
                    amount_minor=-amount_minor,
                    balance_after_minor=locked_source.balance_minor,
                )
                Entry.objects.create(
                    transfer=transfer,
                    account=locked_destination,
                    amount_minor=amount_minor,
                    balance_after_minor=locked_destination.balance_minor,
                )

                transfer.status = Transfer.Status.COMPLETED
                transfer.save(update_fields=["status", "updated_at"])
            else:
                transfer.status = Transfer.Status.FAILED
                transfer.failure_reason = "insufficient_funds"
                transfer.save(update_fields=["status", "failure_reason", "updated_at"])

        return Response(
            TransferResultSerializer(transfer).data,
            status=self._status_for(transfer.status),
        )

    @staticmethod
    def _status_for(transfer_status):
        return {
            Transfer.Status.COMPLETED: status.HTTP_201_CREATED,
            Transfer.Status.FAILED: status.HTTP_422_UNPROCESSABLE_ENTITY,
        }[transfer_status]


class TransferDetailView(generics.RetrieveAPIView):
    """Transfer status by id, restricted to the two accounts involved."""

    serializer_class = TransferResultSerializer
    lookup_field = "id"
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        qs = Transfer.objects.select_related("source_account", "destination_account")
        if user.is_staff:
            return qs
        return qs.filter(Q(source_account__owner=user) | Q(destination_account__owner=user))


class AccountDepositView(APIView):
    """Staff-only, idempotent. Seeds test balances -- not part of the
    double-entry ledger, so deliberately outside the conservation invariant."""

    permission_classes = [permissions.IsAdminUser]

    @extend_schema(
        request=DepositSerializer,
        responses={200: AccountSerializer, 400: None, 404: None, 409: None},
        parameters=[
            OpenApiParameter(
                name="Idempotency-Key",
                location=OpenApiParameter.HEADER,
                required=True,
                type=str,
                description="Client-generated key. Same key + same payload replays the "
                            "original result instead of depositing twice.",
            ),
        ],
    )
    def post(self, request, account_number):
        idempotency_key = request.headers.get("Idempotency-Key")
        if not idempotency_key:
            return Response(
                {"detail": "Idempotency-Key header is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = DepositSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        amount_minor = serializer.validated_data["amount"]

        try:
            account = Account.objects.get(account_number=account_number, is_deleted=False)
        except Account.DoesNotExist:
            return Response({"detail": "Account not found."}, status=status.HTTP_404_NOT_FOUND)

        with transaction.atomic():
            try:
                # Nested atomic() = savepoint; a failure here doesn't
                # poison the outer transaction (same pattern as TransferView).
                with transaction.atomic():
                    Deposit.objects.create(
                        idempotency_key=idempotency_key,
                        account=account,
                        amount_minor=amount_minor,
                        created_by=request.user,
                    )
            except IntegrityError:
                # Key already used; conflicting row is guaranteed committed.
                existing = Deposit.objects.select_related("account").get(idempotency_key=idempotency_key)
                if existing.account_id != account.id or existing.amount_minor != amount_minor:
                    return Response(
                        {"detail": "Idempotency-Key was already used with a different request."},
                        status=status.HTTP_409_CONFLICT,
                    )
                # Already applied -- return current state, don't deposit again.
                return Response(AccountSerializer(existing.account).data, status=status.HTTP_200_OK)

            # New deposit -- only now lock the account and apply it.
            account = Account.objects.select_for_update().get(pk=account.pk)
            account.balance_minor += amount_minor
            account.save(update_fields=["balance_minor", "updated_at"])

        return Response(AccountSerializer(account).data, status=status.HTTP_200_OK)
