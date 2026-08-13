import threading
from decimal import Decimal

from django.core.cache import cache
from django.db import IntegrityError, connection, transaction
from django.db.models import Sum
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework.test import APIClient, APITestCase

from users.tests import make_user

from . import money
from .models import Account, Deposit, Entry, Transfer


LOCMEM_CACHES = {
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}
}


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


@override_settings(CACHES=LOCMEM_CACHES)
class TransferAPITests(APITestCase):

    def setUp(self):
        cache.clear()
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

    @override_settings(CACHES=LOCMEM_CACHES)
    def test_retry_is_served_from_cache_with_zero_db_queries(self):
        from .views import _compute_request_hash, _transfer_cache_key

        first = self._transfer("key-cache-1")
        self.assertEqual(first.status_code, 201, first.data)

        # The cached entry exists, keyed on the idempotency key alone, and
        # carries both the request's fingerprint and the response to replay.
        request_hash = _compute_request_hash(
            self.acct_a.account_number, self.acct_b.account_number, 3000
        )
        cached = cache.get(_transfer_cache_key("key-cache-1"))
        self.assertIsNotNone(cached)
        self.assertEqual(cached["request_hash"], request_hash)
        self.assertEqual(cached["data"]["id"], first.data["id"])

        # A retry is answered entirely from cache -- no Postgres round trip at all.
        with CaptureQueriesContext(connection) as ctx:
            second = self._transfer("key-cache-1")
        self.assertEqual(second.status_code, 201)
        self.assertEqual(second.data["id"], first.data["id"])
        self.assertEqual(len(ctx.captured_queries), 0)
        self.assertEqual(Transfer.objects.filter(idempotency_key="key-cache-1").count(), 1)

    @override_settings(CACHES=LOCMEM_CACHES)
    def test_conflicting_payload_gets_409_from_cache_with_zero_db_queries(self):
        """Same key, different amount -- still 409, and once the canonical
        transfer is cached, later conflicts don't need Postgres to know that."""
        first = self._transfer("key-cache-2", amount="30.00")
        self.assertEqual(first.status_code, 201, first.data)

        with CaptureQueriesContext(connection) as ctx:
            resp = self._transfer("key-cache-2", amount="5.00")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(len(ctx.captured_queries), 0)

        # And a genuinely matching retry still replays the original -- the
        # cache entry wasn't corrupted by the conflicting attempt in between.
        third = self._transfer("key-cache-2", amount="30.00")
        self.assertEqual(third.status_code, 201)
        self.assertEqual(third.data["id"], first.data["id"])

    @override_settings(CACHES=LOCMEM_CACHES)
    def test_first_ever_conflicting_payload_is_still_caught_by_postgres(self):
        """Cold cache (e.g. after a restart) -- the DB's UNIQUE constraint is
        still the real backstop, cache or no cache."""
        first = self._transfer("key-cache-cold", amount="30.00")
        self.assertEqual(first.status_code, 201, first.data)

        cache.clear()  # simulate a cold cache -- TTL expiry, eviction, Redis restart
        resp = self._transfer("key-cache-cold", amount="5.00")
        self.assertEqual(resp.status_code, 409)

    @override_settings(CACHES=LOCMEM_CACHES)
    def test_failed_transfer_is_also_cached(self):
        """Insufficient-funds failures are stable replay targets too."""
        first = self._transfer("key-cache-3", amount="99999.00")
        self.assertEqual(first.status_code, 422)
        self.assertEqual(first.data["status"], "FAILED")

        second = self._transfer("key-cache-3", amount="99999.00")
        self.assertEqual(second.status_code, 422)
        self.assertEqual(second.data["id"], first.data["id"])


@override_settings(CACHES=LOCMEM_CACHES)
class AccountTransfersListTests(APITestCase):
    """GET /accounts/<n>/transfers/ -- every attempt (including FAILED),
    unlike /transactions/ which only shows completed money movements.
    CACHES overridden + cache.clear() in setUp -- see TransferAPITests for why."""

    def setUp(self):
        cache.clear()
        self.alice = make_user("alice6@example.com")
        self.bob = make_user("bob6@example.com")
        self.carol = make_user("carol6@example.com")
        self.acct_a = Account.objects.create(owner=self.alice, account_type="CURRENT", balance_minor=10000)
        self.acct_b = Account.objects.create(owner=self.bob, account_type="CURRENT", balance_minor=0)
        self.client.force_authenticate(user=self.alice)

    def _transfer(self, idem_key, source, destination, amount):
        return self.client.post(
            "/api/accounts/transfers/",
            {"source_account": source, "destination_account": destination, "amount": amount},
            format="json",
            HTTP_IDEMPOTENCY_KEY=idem_key,
        )

    def test_lists_both_successful_and_failed_transfers(self):
        self._transfer("tlist-1", self.acct_a.account_number, self.acct_b.account_number, "30.00")
        self._transfer("tlist-2", self.acct_a.account_number, self.acct_b.account_number, "99999.00")

        resp = self.client.get(f"/api/accounts/{self.acct_a.account_number}/transfers/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data["results"]), 2)
        statuses = {t["status"] for t in resp.data["results"]}
        self.assertEqual(statuses, {"COMPLETED", "FAILED"})
        failed = next(t for t in resp.data["results"] if t["status"] == "FAILED")
        self.assertEqual(failed["failure_reason"], "insufficient_funds")

    def test_shows_transfers_where_account_is_either_side(self):
        self._transfer("tlist-3", self.acct_a.account_number, self.acct_b.account_number, "10.00")

        # setUp authenticates as alice, but acct_b belongs to bob -- switch
        # to its actual owner, since a non-owner/non-staff user correctly
        # gets an empty (not 403) result from this endpoint by design.
        self.client.force_authenticate(user=self.bob)
        resp = self.client.get(f"/api/accounts/{self.acct_b.account_number}/transfers/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data["results"]), 1)
        self.assertEqual(resp.data["results"][0]["destination_account"], self.acct_b.account_number)

    def test_newest_first(self):
        self._transfer("tlist-4", self.acct_a.account_number, self.acct_b.account_number, "10.00")
        self._transfer("tlist-5", self.acct_a.account_number, self.acct_b.account_number, "10.00")

        resp = self.client.get(f"/api/accounts/{self.acct_a.account_number}/transfers/")
        ids = [t["id"] for t in resp.data["results"]]
        transfer_4 = Transfer.objects.get(idempotency_key="tlist-4")
        transfer_5 = Transfer.objects.get(idempotency_key="tlist-5")
        self.assertEqual(ids, [str(transfer_5.id), str(transfer_4.id)])

    def test_non_owner_non_staff_sees_empty_result_not_someone_elses_data(self):
        self._transfer("tlist-6", self.acct_a.account_number, self.acct_b.account_number, "10.00")

        self.client.force_authenticate(user=self.carol)
        resp = self.client.get(f"/api/accounts/{self.acct_a.account_number}/transfers/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["results"], [])

    def test_staff_and_superuser_can_see_any_accounts_transfers(self):
        self._transfer("tlist-7", self.acct_a.account_number, self.acct_b.account_number, "10.00")

        admin = make_user("admin7@example.com", is_staff=False, is_superuser=True)
        self.client.force_authenticate(user=admin)
        resp = self.client.get(f"/api/accounts/{self.acct_a.account_number}/transfers/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data["results"]), 1)


@override_settings(CACHES=LOCMEM_CACHES)
class DepositAPITests(APITestCase):
    """Only a superuser can deposit, and only into their own (bank) account.
    CACHES overridden + cache.clear() in setUp -- one test here also calls
    /transfers/ with a hardcoded Idempotency-Key; see TransferAPITests."""

    def setUp(self):
        cache.clear()
        self.admin = make_user("admin2@example.com", is_staff=True, is_superuser=True)
        self.staff = make_user("staff2@example.com", is_staff=True)
        self.alice = make_user("alice2@example.com")
        self.bank_account = Account.objects.create(owner=self.admin, account_type="CURRENT")
        self.account = Account.objects.create(owner=self.alice, account_type="CURRENT")
        self.client.force_authenticate(user=self.admin)

    def _deposit(self, idem_key, amount="50.00", account=None):
        account = account or self.bank_account
        return self.client.post(
            f"/api/accounts/{account.account_number}/deposit/",
            {"amount": amount}, format="json", HTTP_IDEMPOTENCY_KEY=idem_key,
        )

    def test_requires_idempotency_key(self):
        resp = self.client.post(
            f"/api/accounts/{self.bank_account.account_number}/deposit/",
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
        self.bank_account.refresh_from_db()
        self.assertEqual(self.bank_account.balance_minor, 5000)
        self.assertEqual(Deposit.objects.filter(idempotency_key="dep-2").count(), 1)
        self.assertEqual(Entry.objects.filter(deposit__idempotency_key="dep-2").count(), 1)

    def test_deposit_writes_a_single_sided_entry_visible_in_transactions(self):
        self._deposit("dep-entry-1", amount="500.00")

        entry = Entry.objects.get(deposit__idempotency_key="dep-entry-1")
        self.assertEqual(entry.account_id, self.bank_account.id)
        self.assertEqual(entry.amount_minor, 50000)
        self.assertIsNone(entry.transfer_id)

        resp = self.client.get(f"/api/accounts/{self.bank_account.account_number}/transactions/")
        self.assertEqual(resp.status_code, 200)
        results = resp.data["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["direction"], "credit")
        self.assertEqual(results[0]["amount"], "500.00")
        self.assertEqual(results[0]["counterparty_account_number"], "EXTERNAL")

    def test_conflicting_key_reuse_rejected(self):
        self._deposit("dep-3", amount="50.00")
        resp = self._deposit("dep-3", amount="10.00")
        self.assertEqual(resp.status_code, 409)

    def test_non_staff_forbidden(self):
        self.client.force_authenticate(user=self.alice)
        resp = self._deposit("dep-4")
        self.assertEqual(resp.status_code, 403)

    def test_staff_but_not_superuser_forbidden(self):
        """is_staff alone is no longer enough -- must be a real superuser."""
        self.client.force_authenticate(user=self.staff)
        resp = self._deposit("dep-5")
        self.assertEqual(resp.status_code, 403)

    def test_admin_cannot_deposit_into_someone_elses_account(self):
        """Admin can only fund their own (bank) account -- customers get
        funded via a transfer from that account instead."""
        resp = self._deposit("dep-6", account=self.account)
        self.assertEqual(resp.status_code, 403)
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance_minor, 0)

    def test_customer_funded_via_transfer_from_bank_account(self):
        """The intended real flow: admin deposits into the bank account,
        then moves money to a customer with an ordinary transfer -- which
        means it shows up in both accounts' ledgers."""
        self._deposit("dep-7", amount="100.00")

        resp = self.client.post(
            "/api/accounts/transfers/",
            {
                "source_account": self.bank_account.account_number,
                "destination_account": self.account.account_number,
                "amount": "40.00",
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="fund-alice-1",
        )
        self.assertEqual(resp.status_code, 201, resp.data)

        self.account.refresh_from_db()
        self.assertEqual(self.account.balance_minor, 4000)
        self.assertEqual(Entry.objects.filter(account=self.account).count(), 1)


@override_settings(CACHES=LOCMEM_CACHES)
class AdminAccountVisibilityTests(APITestCase):
    """CACHES overridden -- see TransferAPITests for why.

    A true superuser sees every account/transfer even if is_staff wasn't
    also set (e.g. provisioned by hand rather than via create_superuser)."""

    def setUp(self):
        cache.clear()
        self.superuser_only = make_user(
            "superonly@example.com", is_staff=False, is_superuser=True
        )
        self.staff_only = make_user(
            "staffonly@example.com", is_staff=True, is_superuser=False
        )
        self.alice = make_user("alice5@example.com")
        self.bob = make_user("bob5@example.com")
        self.acct_a = Account.objects.create(
            owner=self.alice, account_type="CURRENT", balance_minor=10000
        )
        self.acct_b = Account.objects.create(
            owner=self.bob, account_type="CURRENT", balance_minor=0
        )

        self.client.force_authenticate(user=self.alice)
        transfer_resp = self.client.post(
            "/api/accounts/transfers/",
            {
                "source_account": self.acct_a.account_number,
                "destination_account": self.acct_b.account_number,
                "amount": "10.00",
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="visibility-setup-1",
        )
        self.transfer_id = transfer_resp.data["id"]

    def test_superuser_without_is_staff_can_list_all_accounts(self):
        self.client.force_authenticate(user=self.superuser_only)
        resp = self.client.get("/api/accounts/")
        self.assertEqual(resp.status_code, 200)
        numbers = {a["account_number"] for a in resp.data}
        self.assertIn(self.acct_a.account_number, numbers)
        self.assertIn(self.acct_b.account_number, numbers)

    def test_staff_without_superuser_can_still_list_all_accounts(self):
        """Existing is_staff behaviour must keep working alongside the fix."""
        self.client.force_authenticate(user=self.staff_only)
        resp = self.client.get("/api/accounts/")
        self.assertEqual(resp.status_code, 200)
        numbers = {a["account_number"] for a in resp.data}
        self.assertIn(self.acct_a.account_number, numbers)
        self.assertIn(self.acct_b.account_number, numbers)

    def test_regular_user_still_only_sees_own_accounts(self):
        self.client.force_authenticate(user=self.alice)
        resp = self.client.get("/api/accounts/")
        self.assertEqual(resp.status_code, 200)
        numbers = {a["account_number"] for a in resp.data}
        self.assertIn(self.acct_a.account_number, numbers)
        self.assertNotIn(self.acct_b.account_number, numbers)

    def test_superuser_without_is_staff_can_retrieve_any_account_detail(self):
        self.client.force_authenticate(user=self.superuser_only)
        resp = self.client.get(f"/api/accounts/{self.acct_b.account_number}/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["account_number"], self.acct_b.account_number)

    def test_superuser_without_is_staff_can_see_any_accounts_transactions(self):
        self.client.force_authenticate(user=self.superuser_only)
        resp = self.client.get(f"/api/accounts/{self.acct_b.account_number}/transactions/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data["results"]), 1)

    def test_superuser_without_is_staff_can_see_any_transfer(self):
        self.client.force_authenticate(user=self.superuser_only)
        resp = self.client.get(f"/api/accounts/transfers/{self.transfer_id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["id"], self.transfer_id)


@override_settings(CACHES=LOCMEM_CACHES)
class ConservationInvariantTests(APITestCase):
    """Total money must never change from transfers alone.
    CACHES overridden + cache.clear() -- see TransferAPITests for why."""

    def test_total_balance_conserved_across_multiple_transfers(self):
        cache.clear()
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


@override_settings(CACHES=LOCMEM_CACHES)
class ConcurrencyTests(TransactionTestCase):
    """Real threads/connections -- TestCase's shared transaction would hide races.
    CACHES overridden + cache.clear() in setUp -- see TransferAPITests for why."""

    def setUp(self):
        cache.clear()
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


class AccountAdminTests(TestCase):
    """The Django admin must never permanently delete an account row."""

    def setUp(self):
        self.staff_admin = make_user(
            "adminpanel1@example.com", is_staff=True, is_superuser=True
        )
        self.owner = make_user("panelowner1@example.com")
        self.empty_account = Account.objects.create(
            owner=self.owner, account_type="CURRENT", balance_minor=0
        )
        self.funded_account = Account.objects.create(
            owner=self.owner, account_type="SAVINGS", balance_minor=5000
        )
        self.client.force_login(self.staff_admin)

    def test_delete_confirmation_page_is_forbidden(self):
        url = reverse("admin:accounts_account_delete", args=[self.empty_account.pk])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 403)

    def test_delete_selected_bulk_action_is_not_offered(self):
        resp = self.client.get(reverse("admin:accounts_account_changelist"))
        self.assertEqual(resp.status_code, 200)
        # Exact attribute match -- "delete_selected" alone would also match
        # inside our own "soft_delete_selected" action's <option value=...>.
        self.assertNotContains(resp, 'value="delete_selected"')

    def test_soft_delete_action_closes_a_zero_balance_account(self):
        resp = self.client.post(
            reverse("admin:accounts_account_changelist"),
            {"action": "soft_delete_selected", "_selected_action": [str(self.empty_account.pk)]},
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.empty_account.refresh_from_db()
        self.assertTrue(self.empty_account.is_deleted)
        self.assertFalse(self.empty_account.is_active)
        self.assertIsNotNone(self.empty_account.deleted_at)
        # Row still exists -- this was a soft delete, not a real one.
        self.assertTrue(Account.objects.filter(pk=self.empty_account.pk).exists())

    def test_soft_delete_action_refuses_a_funded_account(self):
        resp = self.client.post(
            reverse("admin:accounts_account_changelist"),
            {"action": "soft_delete_selected", "_selected_action": [str(self.funded_account.pk)]},
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.funded_account.refresh_from_db()
        self.assertFalse(self.funded_account.is_deleted)
        self.assertEqual(self.funded_account.balance_minor, 5000)
