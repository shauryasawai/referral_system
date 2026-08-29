import random
import string

from django.conf import settings
from django.db import models

PRODUCT_CHOICES = [
    ("careertrek", "CareerTrek"),
    ("marketscope", "Marketscope"),
    ("recruitscope", "Recruit Scope"),
]

CODE_TYPE_CHOICES = [
    ("partner", "Channel Partner"),
    ("customer", "End Customer"),
]

APPROVAL_STATUS_CHOICES = [
    ("pending", "Pending Approval"),
    ("approved", "Approved - Live"),
    ("rejected", "Rejected"),
]

STATUS_CHOICES = [
    ("pending", "Pending"),
    ("converted", "Converted"),
    ("rejected", "Rejected"),
]

# Every operation performed on a code is written to AuditLog for dispute investigation.
AUDIT_ACTION_CHOICES = [
    ("requested", "Code Requested"),
    ("approved", "Code Approved"),
    ("rejected", "Code Rejected"),
    ("edited", "Code Edited"),
    ("deactivated", "Code Deactivated"),
    ("deleted", "Code Deleted"),
    ("redeemed", "Code Redeemed At Checkout"),
    ("user_created", "User Account Created"),
    ("wix_sync_failed", "Wix Sync Failed"),
]

# Wix sync status, tracked per-code so failures are visible instead of silent.
WIX_SYNC_STATUS_CHOICES = [
    ("not_applicable", "Not Applicable"),  # product isn't hosted on Wix
    ("pending", "Not Yet Synced"),
    ("synced", "Synced to Wix"),
    ("failed", "Sync Failed"),
]

DEFAULT_CUSTOMER_DISCOUNT = 5  # % default for auto-generated customer codes


def generate_unique_code(prefix="REF"):
    """Generates a short unique code. Retries on the rare collision instead of trusting randomness alone."""
    for _ in range(20):
        suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
        code = f"{prefix}-{suffix}"
        if not ReferralCode.objects.filter(code=code).exists():
            return code
    raise RuntimeError("Could not generate a unique referral code, please retry.")


class ReferralCode(models.Model):
    code = models.CharField(max_length=30, unique=True)
    code_type = models.CharField(max_length=10, choices=CODE_TYPE_CHOICES)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="referral_codes")
    owner_name = models.CharField(max_length=150, blank=True)
    owner_email = models.EmailField(blank=True)
    product = models.CharField(max_length=20, choices=PRODUCT_CHOICES)
    discount_percent = models.PositiveIntegerField(default=DEFAULT_CUSTOMER_DISCOUNT)
    approval_status = models.CharField(max_length=10, choices=APPROVAL_STATUS_CHOICES, default="pending")

    # Deactivation is permanent by design: once a live code is switched off, it can never be
    # switched back on. The workflow is "request a new code" rather than "reactivate the old one".
    # This keeps the audit trail unambiguous for dispute investigation.
    active = models.BooleanField(default=True)
    deactivated_at = models.DateTimeField(null=True, blank=True)
    deactivated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="deactivated_codes"
    )

    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="approved_codes")
    approved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # --- Wix Studio (CareerTrek) coupon sync -------------------------------
    # Only populated for product == "careertrek". wix_coupon_id lets us target
    # the exact same Wix coupon on later actions (disable) rather than
    # re-deriving it. Sync failures never block the Django-side action; they
    # are recorded here and in AuditLog so an admin can retry/investigate.
    wix_coupon_id = models.CharField(max_length=100, blank=True, default="")
    wix_sync_status = models.CharField(max_length=15, choices=WIX_SYNC_STATUS_CHOICES, default="not_applicable")
    wix_sync_error = models.TextField(blank=True, default="")
    wix_last_synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def is_live(self):
        return self.approval_status == "approved" and self.active

    @property
    def display_status(self):
        """Single source of truth for the status label shown across every dashboard."""
        if self.approval_status == "approved" and not self.active:
            return "deactivated"
        return self.approval_status

    @property
    def display_status_label(self):
        return {
            "pending": "Pending Approval",
            "approved": "Approved - Live",
            "rejected": "Rejected",
            "deactivated": "Deactivated",
        }[self.display_status]

    def save(self, *args, **kwargs):
        if not self.code:
            prefix = "PTR" if self.code_type == "partner" else "CUS"
            self.code = generate_unique_code(prefix)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.code} ({self.get_code_type_display()})"


class Referral(models.Model):
    """A redemption record: one row per customer who successfully applied a code at checkout."""
    referral_code = models.ForeignKey(ReferralCode, on_delete=models.CASCADE, related_name="referrals")
    customer_name = models.CharField(max_length=150)
    customer_email = models.EmailField()
    product = models.CharField(max_length=20, choices=PRODUCT_CHOICES)
    discount_applied = models.PositiveIntegerField()
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="pending")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.customer_email} via {self.referral_code.code}"


class AuditLog(models.Model):
    """
    Immutable record of every operation: request, approval, rejection, edit, deactivation,
    deletion, redemption, user account creation, and Wix sync failures. Kept even after the
    code itself is deleted (code_snapshot preserves the code string) so disputes can always
    be investigated.
    """
    referral_code = models.ForeignKey(ReferralCode, null=True, blank=True, on_delete=models.SET_NULL, related_name="audit_entries")
    code_snapshot = models.CharField(max_length=30, blank=True)
    action = models.CharField(max_length=20, choices=AUDIT_ACTION_CHOICES)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="audit_actions")
    details = models.TextField(blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-timestamp"]

    def __str__(self):
        return f"{self.timestamp:%Y-%m-%d %H:%M} {self.code_snapshot} {self.action}"


class UserAccess(models.Model):
    """
    Per-user product permissions, set by an admin when the account is created on the
    Manage Users page. If no UserAccess row exists for a user (e.g. admin/ops accounts,
    or accounts created before this feature existed), that user is treated as having
    access to every product — the restriction only applies once an admin has explicitly
    scoped an account to specific products.
    """
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="access")
    allowed_products = models.JSONField(default=list, blank=True, help_text="Product codes this user may generate referral codes for.")

    def __str__(self):
        return f"{self.user.username}: {', '.join(self.allowed_products) or 'no products'}"