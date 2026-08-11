import uuid
import secrets
import string

from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import IntegrityError, models, transaction
from django.utils import timezone


def generate_bank_user_id():
    """'MB' + 11 random chars. Non-derivable, immutable."""
    alphabet = string.ascii_uppercase + string.digits
    return "MB" + "".join(secrets.choice(alphabet) for _ in range(11))


class UserManager(BaseUserManager):
    """AbstractUser's default manager assumes `username`; we don't have one."""

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

        email = self.normalize_email(email)

        # Retry on the actual DB collision rather than a racy pre-check.
        for _ in range(5):
            user = self.model(
                email=email,
                phone_number=phone_number,
                first_name=first_name,
                last_name=last_name,
                cnic=cnic,
                passport_number=passport_number,
                bank_user_id=generate_bank_user_id(),
                **extra_fields,
            )
            user.set_password(password)
            try:
                with transaction.atomic():
                    user.save(using=self._db)
                return user
            except IntegrityError as exc:
                constraint = getattr(getattr(exc.__cause__, "diag", None), "constraint_name", "") or ""
                if "bank_user_id" in constraint:
                    continue
                raise

        raise RuntimeError("Could not generate a unique bank_user_id after 5 attempts")

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
    # cnic here so createsuperuser prompts for it (satisfies cnic_or_passport_required).
    REQUIRED_FIELDS = ["phone_number", "first_name", "last_name", "cnic"]

    objects = UserManager()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(cnic__isnull=False) | models.Q(passport_number__isnull=False),
                name="cnic_or_passport_required",
            )
        ]
        indexes = [
            models.Index(fields=["date_joined"], name="user_date_joined_idx"),
        ]

    def __str__(self):
        return self.email

    def soft_delete(self):
        """Deactivate + flag deleted. Never hard-delete a user row."""
        self.is_deleted = True
        self.is_active = False
        self.deleted_at = timezone.now()
        self.save(update_fields=["is_deleted", "is_active", "deleted_at"])
