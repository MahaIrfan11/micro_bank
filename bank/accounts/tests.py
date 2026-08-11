import threading
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.test import TestCase, TransactionTestCase
from rest_framework.test import APIClient, APITestCase

from users.tests import make_user

from . import money
from .models import Account, Deposit, Entry, Transfer


class MoneyUtilsTests(TestCase):
    def test_parse_amount_rejects_float(self):
        with self.assertRaises(ValueError):
            money.parse_amount(10.75)

    def test_parse_amount_accepts_string(self):
        self.assertEqual(money.parse_amount("10.75"), Decimal("10.75"))

    def test_parse_amount_rejects_garbage(self):
        with self.assertRaises(ValueError):
            money.parse_amount("not-a-number")

    def test_to_minor_units_basic(self):
        self.assertEqual(money.to_minor_units("10.75"), 1075)

    def test_to_minor_units_rejects_sub_cent_precision(self):
        with self.assertRaises(ValueError):
            money.to_minor_units("10.755")

    def test_to_minor_units_rejects_zero(self):
        with self.assertRaises(ValueError):
            money.to_minor_units("0.00")

    def test_to_minor_units_rejects_negative(self):
        with self.assertRaises(ValueError):
            money.to_minor_units("-5.00")

    def test_to_minor_units_allows_non_positive_when_flagged(self):
        self.assertEqual(money.to_minor_units("0.00", require_positive=False), 0)

    def test_to_major_units_roundtrip(self):
        self.assertEqual(money.to_major_units(1075), Decimal("10.75"))

    def test_format_amount(self):
        self.assertEqual(money.format_amount(1075), "10.75")


class AccountModelTests(TestCase):
    def setUp(self):
        self.owner = make_user("owner1@example.com")

    def test_balance_cannot_go_negative(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Account.objects.create(owner=self.owner, account_type="CURRENT", balance_minor=-1)

    def test_account_number_is_generated(self):
        account = Account.objects.create(owner=self.owner, account_type="CURRENT")
        self.assertEqual(len(account.account_number), 12)
        self.assertTrue(account.account_number.isdigit())

    def test_one_account_per_type_per_owner(self):
        Account.objects.create(owner=self.owner, account_type="CURRENT")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Account.objects.create(owner=self.owner, account_type="CURRENT")

    def test_can_hold_one_current_and_one_savings(self):
        Account.objects.create(owner=self.owner, account_type="CURRENT")
        Account.objects.create(owner=self.owner, account_type="SAVINGS")
        self.assertEqual(Account.objects.filter(owner=self.owner).count(), 2)


class TransferAPITests(APITestCase):
    def setUp(self):
        self.alice = make_user("alice@example.com")
        self.bob = make_user("bob@example.com")
        self.acct_a = Account.objects.create(owner=self.alice, account_type="CURRENT", balance_minor=10000)
        self.acct_b = Account.objects.create(owner=self.bob, account_type="CURRENT", balance_minor=0)
        self.client.force_authenticate(user=self.alice)

    def _transfer(self, idem_key, source=None, destination=None, amount="30.00"):
        return self.client.post(
            "/api/accounts/transfers/",
            {
                "source_account": source or self.acct_a.account_number,
                "destination_account": destination or self.acct_b.account_number,
                "amount": amount,
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY=idem_key,
        )

    def test_requires_idempotency_key(self):
        resp = self.client.post("/api/accounts/transfers/", {
            "source_account": self.acct_a.account_number,
            "destination_account": self.acct_b.account_number,
            "amount": "30.00",
        }, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_happy_path_moves_money_and_writes_ledger(self):
        resp = self._transfer("key-1")
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data["status"], "COMPLETED")

        self.acct_a.refresh_from_db()
        self.acct_b.refresh_from_db()
        self.assertEqual(self.acct_a.balance_minor, 7000)
        self.assertEqual(self.acct_b.balance_minor, 3000)

        transfer = Transfer.objects.get(idempotency_key="key-1")
        entries = Entry.objects.filter(transfer=transfer)
        self.assertEqual(entries.count(), 2)
        self.assertEqual(entries.aggregate(Sum("amount_minor"))["amount_minor__sum"], 0)

    def test_idempotent_retry_does_not_double_move_money(self):
        first = self._transfer("key-2")
        second = self._transfer("key-2")
        self.assertEqual(first.data["id"], second.data["id"])

        self.acct_a.refresh_from_db()
        self.assertEqual(self.acct_a.balance_minor, 7000)  # not 4000
        self.assertEqual(Transfer.objects.filter(idempotency_key="key-2").count(), 1)

    def test_conflicting_key_reuse_is_rejected(self):
        self._transfer("key-3", amount="30.00")
        resp = self._transfer("key-3", amount="5.00")
        self.assertEqual(resp.status_code, 409)

    def test_self_transfer_rejected(self):
        resp = self._transfer("key-4", destination=self.acct_a.account_number)
        self.assertEqual(resp.status_code, 400)

    def test_insufficient_funds_fails_cleanly(self):
        resp = self._transfer("key-5", amount="99999.00")
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.data["status"], "FAILED")
        self.acct_a.refresh_from_db()
        self.assertEqual(self.acct_a.balance_minor, 10000)  # unchanged
        self.assertEqual(Entry.objects.filter(transfer__idempotency_key="key-5").count(), 0)

    def test_cannot_transfer_from_an_account_you_do_not_own(self):
        self.client.force_authenticate(user=self.bob)
        resp = self._transfer("key-6")
        self.assertEqual(resp.status_code, 400)


class DepositAPITests(APITestCase):
    def setUp(self):
        self.staff = make_user("staff2@example.com", is_staff=True)
        self.alice = make_user("alice2@example.com")
        self.account = Account.objects.create(owner=self.alice, account_type="CURRENT")
        self.client.force_authenticate(user=self.staff)

    def _deposit(self, idem_key, amount="50.00"):
        return self.client.post(
            f"/api/accounts/{self.account.account_number}/deposit/",
            {"amount": amount}, format="json", HTTP_IDEMPOTENCY_KEY=idem_key,
        )

    def test_requires_idempotency_key(self):
        resp = self.client.post(
            f"/api/accounts/{self.account.account_number}/deposit/",
            {"amount": "50.00"}, format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_happy_path(self):
        resp = self._deposit("dep-1")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data["balance"], "50.00")

    def test_idempotent_retry_does_not_double_deposit(self):
        self._deposit("dep-2")
        resp = self._deposit("dep-2")
        self.assertEqual(resp.data["balance"], "50.00")
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance_minor, 5000)
        self.assertEqual(Deposit.objects.filter(idempotency_key="dep-2").count(), 1)

    def test_conflicting_key_reuse_rejected(self):
        self._deposit("dep-3", amount="50.00")
        resp = self._deposit("dep-3", amount="10.00")
        self.assertEqual(resp.status_code, 409)

    def test_non_staff_forbidden(self):
        self.client.force_authenticate(user=self.alice)
        resp = self._deposit("dep-4")
        self.assertEqual(resp.status_code, 403)


class ConservationInvariantTests(APITestCase):
    """Total money must never change from transfers alone."""

    def test_total_balance_conserved_across_multiple_transfers(self):
        alice = make_user("alice4@example.com")
        bob = make_user("bob4@example.com")
        carol = make_user("carol4@example.com")
        acct_a = Account.objects.create(owner=alice, account_type="CURRENT", balance_minor=10000)
        acct_b = Account.objects.create(owner=bob, account_type="CURRENT", balance_minor=5000)
        acct_c = Account.objects.create(owner=carol, account_type="CURRENT", balance_minor=0)
        total_before = acct_a.balance_minor + acct_b.balance_minor + acct_c.balance_minor

        self.client.force_authenticate(user=alice)
        self.client.post("/api/accounts/transfers/", {
            "source_account": acct_a.account_number, "destination_account": acct_c.account_number,
            "amount": "25.00",
        }, format="json", HTTP_IDEMPOTENCY_KEY="cons-1")

        self.client.force_authenticate(user=bob)
        self.client.post("/api/accounts/transfers/", {
            "source_account": acct_b.account_number, "destination_account": acct_c.account_number,
            "amount": "15.00",
        }, format="json", HTTP_IDEMPOTENCY_KEY="cons-2")

        acct_a.refresh_from_db()
        acct_b.refresh_from_db()
        acct_c.refresh_from_db()
        total_after = acct_a.balance_minor + acct_b.balance_minor + acct_c.balance_minor
        self.assertEqual(total_before, total_after)
        self.assertEqual(Entry.objects.aggregate(Sum("amount_minor"))["amount_minor__sum"], 0)


class ConcurrencyTests(TransactionTestCase):
    """Real threads/connections -- TestCase's shared transaction would hide races."""

    def setUp(self):
        self.alice = make_user("alice3@example.com")
        self.bob = make_user("bob3@example.com")
        self.carol = make_user("carol3@example.com")
        self.account = Account.objects.create(owner=self.alice, account_type="CURRENT", balance_minor=10000)
        self.acct_bob = Account.objects.create(owner=self.bob, account_type="CURRENT")
        self.acct_carol = Account.objects.create(owner=self.carol, account_type="CURRENT")

    @staticmethod
    def _client_for(user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def test_two_concurrent_transfers_cannot_both_succeed_past_the_balance(self):
        """$100 balance, two $70 transfers at once -- only one can complete."""
        results = {}

        def make_transfer(key, destination, slot):
            client = self._client_for(self.alice)
            results[slot] = client.post(
                "/api/accounts/transfers/",
                {
                    "source_account": self.account.account_number,
                    "destination_account": destination,
                    "amount": "70.00",
                },
                format="json", HTTP_IDEMPOTENCY_KEY=key,
            )

        t1 = threading.Thread(target=make_transfer, args=("race-1", self.acct_bob.account_number, "a"))
        t2 = threading.Thread(target=make_transfer, args=("race-2", self.acct_carol.account_number, "b"))
        t1.start(); t2.start()
        t1.join(); t2.join()

        statuses = sorted(r.data["status"] for r in results.values())
        self.assertEqual(statuses, ["COMPLETED", "FAILED"])

        self.account.refresh_from_db()
        self.assertEqual(self.account.balance_minor, 3000)  # exactly one $70 debit
        self.assertGreaterEqual(self.account.balance_minor, 0)

    def test_concurrent_retries_of_the_same_idempotency_key_produce_one_transfer(self):
        results = []

        def retry():
            client = self._client_for(self.alice)
            results.append(client.post(
                "/api/accounts/transfers/",
                {
                    "source_account": self.account.account_number,
                    "destination_account": self.acct_bob.account_number,
                    "amount": "10.00",
                },
                format="json", HTTP_IDEMPOTENCY_KEY="same-key-race",
            ))

        threads = [threading.Thread(target=retry) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        for r in results:
            self.assertLess(
                r.status_code, 500,
                f"unexpected server error: {getattr(r, 'data', r.content)}",
            )

        self.assertEqual(Transfer.objects.filter(idempotency_key="same-key-race").count(), 1)
        self.assertEqual({r.data["id"] for r in results}, {str(Transfer.objects.get(idempotency_key="same-key-race").id)})

        self.account.refresh_from_db()
        self.assertEqual(self.account.balance_minor, 9000)  # exactly one $10 debit, not five
