from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from .models import User


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, min_length=8)
    cnic = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    passport_number = serializers.CharField(required=False, allow_null=True, allow_blank=True)

    class Meta:
        model = User
        fields = [
            "email",
            "password",
            "phone_number",
            "first_name",
            "last_name",
            "cnic",
            "passport_number",
        ]

    def validate(self, attrs):
        cnic = attrs.get("cnic") or None
        passport_number = attrs.get("passport_number") or None
        if not cnic and not passport_number:
            raise serializers.ValidationError(
                "Provide either a CNIC or a passport number."
            )
        attrs["cnic"] = cnic
        attrs["passport_number"] = passport_number

        # AUTH_PASSWORD_VALIDATORS is configured but nothing was calling it --
        # create_user() just does set_password(), no strength check. Run it
        # here. Unsaved user instance lets UserAttributeSimilarityValidator
        # check the password isn't just the email/name.
        temp_user = User(
            email=attrs.get("email"),
            first_name=attrs.get("first_name"),
            last_name=attrs.get("last_name"),
            phone_number=attrs.get("phone_number"),
        )
        try:
            validate_password(attrs["password"], user=temp_user)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"password": list(exc.messages)})

        return attrs

    def create(self, validated_data):
        return User.objects.create_user(**validated_data)


class AdminUserCreateSerializer(RegisterSerializer):
    """Same validation as public signup, plus staff can grant is_staff up front."""

    is_staff = serializers.BooleanField(required=False, default=False)

    class Meta(RegisterSerializer.Meta):
        fields = RegisterSerializer.Meta.fields + ["is_staff"]


class UserSerializer(serializers.ModelSerializer):
    """Self-service read view."""

    class Meta:
        model = User
        fields = [
            "bank_user_id",
            "email",
            "phone_number",
            "first_name",
            "last_name",
            "cnic",
            "passport_number",
        ]
        read_only_fields = fields


class UserUpdateSerializer(serializers.ModelSerializer):
    """Self-edit and admin-edit share this: name + phone only."""

    class Meta:
        model = User
        fields = ["first_name", "last_name", "phone_number"]


class AdminUserSerializer(serializers.ModelSerializer):
    """Admin-only read view; includes account-status fields."""

    class Meta:
        model = User
        fields = [
            "bank_user_id",
            "email",
            "phone_number",
            "first_name",
            "last_name",
            "cnic",
            "passport_number",
            "is_active",
            "is_deleted",
            "deleted_at",
            "date_joined",
        ]
        read_only_fields = fields
