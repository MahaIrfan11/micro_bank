import hashlib

from django.core.cache import cache
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


def _can_see_all_accounts(user):
    return user.is_staff or user.is_superuser


class AccountListCreateView(generics.ListCreateAPIView):
    """Own accounts only; staff/admin see every account (and can pass
    owner_bank_user_id to create one for someone else). New accounts
    always start at $0."""

    serializer_class = AccountSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = Account.objects.select_related("owner").filter(is_deleted=False)
        if not _can_see_all_accounts(self.request.user):
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
    """Owner or staff/admin. Non-owner gets 404, not 403 (no enumeration)."""

    serializer_class = AccountSerializer
    lookup_field = "account_number"
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = Account.objects.select_related("owner").filter(is_deleted=False)
        if not _can_see_all_accounts(self.request.user):
            qs = qs.filter(owner=self.request.user)
        return qs


class EntryCursorPagination(CursorPagination):
    page_size = 25
    max_page_size = 100
    page_size_query_param = "page_size"
    ordering = ("-created_at", "-id")  # -id breaks ties on identical timestamps


class AccountTransactionsView(generics.ListAPIView):
    """Owner or staff/admin. Ledger history, newest first, cursor-paginated."""

    serializer_class = EntrySerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = EntryCursorPagination

    def get_queryset(self):
        account_number = self.kwargs["account_number"]
        accounts = Account.objects.filter(account_number=account_number, is_deleted=False)
        if not _can_see_all_accounts(self.request.user):
            accounts = accounts.filter(owner=self.request.user)
        account = accounts.first()
        if account is None:
            return Entry.objects.none()
        return (
            Entry.objects.filter(account=account)
            .select_related(
                "transfer", "transfer__source_account", "transfer__destination_account", "deposit"
            )
            .order_by("-created_at")
        )


class TransferCursorPagination(CursorPagination):
    page_size = 25
    max_page_size = 100
    page_size_query_param = "page_size"
    ordering = ("-created_at", "-id")  # -id breaks ties on identical timestamps


class AccountTransfersView(generics.ListAPIView):
    """Owner or staff/admin. Every transfer attempt involving this account,
    newest first -- including FAILED ones (unlike /transactions/, which only
    shows Entry rows for money that actually moved)."""

    serializer_class = TransferResultSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = TransferCursorPagination

    def get_queryset(self):
        account_number = self.kwargs["account_number"]
        accounts = Account.objects.filter(account_number=account_number, is_deleted=False)
        if not _can_see_all_accounts(self.request.user):
            accounts = accounts.filter(owner=self.request.user)
        account = accounts.first()
        if account is None:
            return Transfer.objects.none()
        return (
            Transfer.objects.filter(Q(source_account=account) | Q(destination_account=account))
            .select_related("source_account", "destination_account")
            .order_by("-created_at")
        )


def _compute_request_hash(source_account_number, destination_account_number, amount_component):

    raw = f"{source_account_number}:{destination_account_number}:{amount_component}"
    return hashlib.sha256(raw.encode()).hexdigest()


# Cache window for a completed/failed transfer's response
TRANSFER_CACHE_TTL_SECONDS = 60 * 60 * 24


def _transfer_cache_key(idempotency_key):
    return f"transfer:idem:{idempotency_key}"


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

        try:
            amount_component = money.to_minor_units(str(request.data.get("amount", "")))
        except ValueError:
            amount_component = f"invalid:{request.data.get('amount')!r}"

        request_hash = _compute_request_hash(
            request.data.get("source_account", ""),
            request.data.get("destination_account", ""),
            amount_component,
        )
        cache_key = _transfer_cache_key(idempotency_key)

        # Redis fast path -- keyed on idempotency_key
        cached = cache.get(cache_key)
        if cached is not None:
            if cached["request_hash"] == request_hash:
                return Response(cached["data"], status=cached["status"])
            return Response(
                {"detail": "Idempotency-Key was already used with a different request."},
                status=status.HTTP_409_CONFLICT,
            )

        serializer = TransferSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)

        source_account = serializer.validated_data["source_account_obj"]
        destination_account = serializer.validated_data["destination_account_obj"]
        amount_minor = serializer.validated_data["amount_minor"]

        with transaction.atomic():
            try:
                # Savepoint -- failure here doesn't poison the outer transaction.
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
                existing = Transfer.objects.select_related(
                    "source_account", "destination_account"
                ).get(idempotency_key=idempotency_key)
                response_data = TransferResultSerializer(existing).data
                response_status = self._status_for(existing.status)
                cache.set(
                    cache_key,
                    {"request_hash": existing.request_hash, "data": response_data, "status": response_status},
                    TRANSFER_CACHE_TTL_SECONDS,
                )
                if existing.request_hash != request_hash:
                    return Response(
                        {"detail": "Idempotency-Key was already used with a different request."},
                        status=status.HTTP_409_CONFLICT,
                    )
                return Response(response_data, status=response_status)

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

        response_data = TransferResultSerializer(transfer).data
        response_status = self._status_for(transfer.status)
        cache.set(
            cache_key,
            {"request_hash": request_hash, "data": response_data, "status": response_status},
            TRANSFER_CACHE_TTL_SECONDS,
        )
        return Response(response_data, status=response_status)

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
        if _can_see_all_accounts(user):
            return qs
        return qs.filter(Q(source_account__owner=user) | Q(destination_account__owner=user))


class IsSuperUser(permissions.BasePermission):
    """Stricter than IsAdminUser (is_staff) -- only a true superuser is the bank."""

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.is_superuser)


class AccountDepositView(APIView):
    """Superuser-only, idempotent, and only into the caller's own account.
    This is the sole entry point where new money enters the system. Every
    customer account gets funded via TransferView instead (source = the
    superuser's own account), so that movement is an ordinary, ledger-visible
    transfer -- only the initial injection here stays outside the ledger."""

    permission_classes = [IsSuperUser]

    @extend_schema(
        request=DepositSerializer,
        responses={200: AccountSerializer, 400: None, 403: None, 404: None, 409: None},
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

        if account.owner_id != request.user.id:
            return Response(
                {"detail": "You can only deposit into your own account."},
                status=status.HTTP_403_FORBIDDEN,
            )

        with transaction.atomic():
            try:
                # Savepoint -- failure here doesn't poison the outer transaction.
                with transaction.atomic():
                    deposit = Deposit.objects.create(
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

            # Single-sided entry -- money entering from outside, no counterparty account.
            Entry.objects.create(
                deposit=deposit,
                account=account,
                amount_minor=amount_minor,
                balance_after_minor=account.balance_minor,
            )

        return Response(AccountSerializer(account).data, status=status.HTTP_200_OK)
