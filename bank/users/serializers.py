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
        return attrs

    def create(self, validated_data):
        return User.objects.create_user(**validated_data)


class UserSerializer(serializers.ModelSerializer):
    """Self-service read view -- what a user sees of their own record."""

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
    """Used for both self-edit (/me/) and admin edit -- the editable
    surface is identical: name and phone. Everything identity/KYC
    related is immutable through this API."""

    class Meta:
        model = User
        fields = ["first_name", "last_name", "phone_number"]


class AdminUserSerializer(serializers.ModelSerializer):
    """Admin-only read view -- includes account-status fields a
    regular user never needs to see about themselves."""

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
