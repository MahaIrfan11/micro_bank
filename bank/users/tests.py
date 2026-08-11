import itertools

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APITestCase

from accounts.models import Account

User = get_user_model()

_seq = itertools.count(1)


def make_user(email, **overrides):
    """Unique phone/cnic per call; no password-strength check here."""
    n = next(_seq)
    defaults = dict(
        password="irrelevant-for-orm-created-users",
        phone_number=f"+1555{n:07d}",
        first_name="Test",
        last_name="User",
        cnic=f"{n:013d}",
    )
    defaults.update(overrides)
    return User.objects.create_user(email=email, **defaults)


class UserManagerTests(TestCase):
    def test_requires_cnic_or_passport(self):
        with self.assertRaises(ValueError):
            User.objects.create_user(
                email="nodoc@example.com", password="x", phone_number="15550000001",
                first_name="A", last_name="B",
            )

    def test_bank_user_id_generated(self):
        user = make_user("idcheck@example.com")
        self.assertTrue(user.bank_user_id.startswith("MB"))
        self.assertEqual(len(user.bank_user_id), 13)

    def test_soft_delete_deactivates_without_removing_the_row(self):
        user = make_user("softdel@example.com")
        user.soft_delete()
        user.refresh_from_db()
        self.assertTrue(user.is_deleted)
        self.assertFalse(user.is_active)
        self.assertIsNotNone(user.deleted_at)
        self.assertTrue(User.objects.filter(pk=user.pk).exists())


class SignupAPITests(APITestCase):
    def test_malformed_json_returns_400_not_500(self):
        resp = self.client.post("/api/users/signup/", data="not json", content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    def test_signup_enforces_password_strength(self):
        resp = self.client.post("/api/users/signup/", {
            "email": "weak@example.com", "password": "password", "phone_number": "5551234567",
            "first_name": "Weak", "last_name": "Pass", "cnic": "1111111111111",
        }, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("password", resp.data)

    def test_signup_requires_cnic_or_passport(self):
        resp = self.client.post("/api/users/signup/", {
            "email": "nodoc2@example.com", "password": "GoodPass123!", "phone_number": "5551234568",
            "first_name": "No", "last_name": "Doc",
        }, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_signup_then_login(self):
        resp = self.client.post("/api/users/signup/", {
            "email": "newuser@example.com", "password": "GoodPass123!", "phone_number": "5551234569",
            "first_name": "New", "last_name": "User", "cnic": "2222222222222",
        }, format="json")
        self.assertEqual(resp.status_code, 201, resp.data)

        resp = self.client.post("/api/users/login/", {
            "email": "newuser@example.com", "password": "GoodPass123!",
        }, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("access", resp.data)


class DeleteUserAPITests(APITestCase):
    def test_cannot_delete_user_with_open_account(self):
        user = make_user("hasaccount@example.com")
        Account.objects.create(owner=user, account_type="CURRENT")
        self.client.force_authenticate(user=user)

        resp = self.client.delete("/api/users/me/")
        self.assertEqual(resp.status_code, 400)
        user.refresh_from_db()
        self.assertFalse(user.is_deleted)

    def test_can_delete_user_without_account(self):
        user = make_user("noaccount@example.com")
        self.client.force_authenticate(user=user)

        resp = self.client.delete("/api/users/me/")
        self.assertEqual(resp.status_code, 204)
        user.refresh_from_db()
        self.assertTrue(user.is_deleted)


class AdminUserCreateAPITests(APITestCase):
    def test_staff_can_create_user(self):
        staff = make_user("staff1@example.com", is_staff=True)
        self.client.force_authenticate(user=staff)

        resp = self.client.post("/api/users/", {
            "email": "created@example.com", "password": "GoodPass123!", "phone_number": "5551234570",
            "first_name": "Created", "last_name": "ByStaff", "cnic": "3333333333333",
        }, format="json")
        self.assertEqual(resp.status_code, 201, resp.data)

    def test_non_staff_cannot_use_admin_create_endpoint(self):
        user = make_user("regular1@example.com")
        self.client.force_authenticate(user=user)

        resp = self.client.post("/api/users/", {
            "email": "created2@example.com", "password": "GoodPass123!", "phone_number": "5551234571",
            "first_name": "X", "last_name": "Y", "cnic": "4444444444444",
        }, format="json")
        self.assertEqual(resp.status_code, 403)
