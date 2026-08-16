import random, string
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

DEFAULT_CUSTOMER_DISCOUNT = 5  # % default for auto-generated customer codes


def generate_unique_code(prefix="REF"):
    while True:
        suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
        code = f"{prefix}-{suffix}"
        if not ReferralCode.objects.filter(code=code).exists():
            return code


class ReferralCode(models.Model):
    code = models.CharField(max_length=30, unique=True)
    code_type = models.CharField(max_length=10, choices=CODE_TYPE_CHOICES)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="referral_codes")
    owner_name = models.CharField(max_length=150, blank=True)
    owner_email = models.EmailField(blank=True)
    product = models.CharField(max_length=20, choices=PRODUCT_CHOICES)
    discount_percent = models.PositiveIntegerField(default=DEFAULT_CUSTOMER_DISCOUNT)
    approval_status = models.CharField(max_length=10, choices=APPROVAL_STATUS_CHOICES, default="pending")
    active = models.BooleanField(default=True, help_text="Live codes can be deactivated without changing their approval history.")
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="approved_codes")
    approved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def is_live(self):
        return self.approval_status == "approved" and self.active

    def save(self, *args, **kwargs):
        if not self.code:
            prefix = "PTR" if self.code_type == "partner" else "CUS"
            self.code = generate_unique_code(prefix)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.code} ({self.get_code_type_display()})"


class Referral(models.Model):
    referral_code = models.ForeignKey(ReferralCode, on_delete=models.CASCADE, related_name="referrals")
    customer_name = models.CharField(max_length=150)
    customer_email = models.EmailField()
    product = models.CharField(max_length=20, choices=PRODUCT_CHOICES)
    discount_applied = models.PositiveIntegerField()
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="pending")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.customer_email} via {self.referral_code.code}"
