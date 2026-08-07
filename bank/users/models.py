import uuid
import secrets
import string

from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models
from django.utils import timezone


def generate_bank_user_id():
    """Random, non-derivable bank identifier: 'MB' + 11 random chars.

    Never derived from email, CNIC, passport, name, or DOB. Generated
    once at account creation and never changed again.
    """
    alphabet = string.ascii_uppercase + string.digits
    return "MB" + "".join(secrets.choice(alphabet) for _ in range(11))


class UserManager(BaseUserManager):
    """Custom manager: AbstractUser's default manager assumes a
    `username` field, which this model does not have.

    Deliberately unfiltered (includes soft-deleted rows) -- filtering
    the default manager would silently break Django's authentication
    lookups and DRF's automatic uniqueness validation, which both use
    `_default_manager` internally.
    """

    use_in_migrations = True

    def _create_user(self, email, password, phone_number, first_name, last_name,
                      cnic, passport_number, **extra_fields):
        if not email:
            raise ValueError("Email is required")
        if not phone_number:
            raise ValueError("Phone number is required")
        if not first_name or not last_name:
            raise ValueError("First name and last name are required")

        cnic = cnic or None
        passport_number = passport_number or None
        if not cnic and not passport_number:
            raise ValueError("Either CNIC or passport number is required")

        bank_user_id = generate_bank_user_id()
        while self.model.objects.filter(bank_user_id=bank_user_id).exists():
            bank_user_id = generate_bank_user_id()

        email = self.normalize_email(email)
        user = self.model(
            email=email,
            phone_number=phone_number,
            first_name=first_name,
            last_name=last_name,
            cnic=cnic,
            passport_number=passport_number,
            bank_user_id=bank_user_id,
            **extra_fields,
        )
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, phone_number=None, first_name=None,
                     last_name=None, cnic=None, passport_number=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(
            email, password, phone_number, first_name, last_name,
            cnic, passport_number, **extra_fields,
        )

    def create_superuser(self, email, password=None, phone_number=None, first_name=None,
                          last_name=None, cnic=None, passport_number=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)

        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True")

        return self._create_user(
            email, password, phone_number, first_name, last_name,
            cnic, passport_number, **extra_fields,
        )


class User(AbstractUser):
    username = None

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    bank_user_id = models.CharField(max_length=13, unique=True, editable=False)

    email = models.EmailField(unique=True)
    phone_number = models.CharField(max_length=20, unique=True)
    first_name = models.CharField(max_length=150, blank=False)
    last_name = models.CharField(max_length=150, blank=False)

    cnic = models.CharField(max_length=32, unique=True, null=True, blank=True)
    passport_number = models.CharField(max_length=32, unique=True, null=True, blank=True)

    is_deleted = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["phone_number", "first_name", "last_name"]

    objects = UserManager()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(cnic__isnull=False) | models.Q(passport_number__isnull=False),
                name="cnic_or_passport_required",
            )
        ]

    def __str__(self):
        return self.email

    def soft_delete(self):
        """Never physically remove a banking user record. Deactivates
        login (is_active=False, enforced by Django's auth backend) and
        stamps deleted_at, but keeps the row -- and its unique email /
        phone / CNIC / passport / bank_user_id -- permanently reserved
        so none of it can be silently reused by a new signup.
        """
        self.is_deleted = True
        self.is_active = False
        self.deleted_at = timezone.now()
        self.save(using=self._db, update_fields=["is_deleted", "is_active", "deleted_at"])
