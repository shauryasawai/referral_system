from django.contrib import admin
from django.db.models.functions import TruncWeek, TruncMonth
from django.db.models import Count
from django.template.response import TemplateResponse
from django.urls import path
from django.utils import timezone
from .models import AuditLog, ReferralCode, Referral, UserAccess


@admin.register(ReferralCode)
class ReferralCodeAdmin(admin.ModelAdmin):
    list_display = ("code", "code_type", "requested_by", "product", "discount_percent", "approval_status", "active", "approved_by", "created_at")
    list_filter = ("code_type", "product", "approval_status", "active")
    search_fields = ("code", "requested_by__username", "owner_name", "owner_email")
    date_hierarchy = "created_at"
    readonly_fields = ("requested_by", "approved_by", "approved_at", "deactivated_by", "deactivated_at")
    actions = ["approve_codes", "reject_codes", "deactivate_codes"]

    @admin.action(description="Approve selected codes (goes live on Ops dashboard)")
    def approve_codes(self, request, queryset):
        updated = queryset.filter(approval_status="pending").update(
            approval_status="approved", approved_by=request.user, approved_at=timezone.now()
        )
        self.message_user(request, f"{updated} code(s) approved and pushed live to Ops.")

    @admin.action(description="Reject selected codes")
    def reject_codes(self, request, queryset):
        updated = queryset.filter(approval_status="pending").update(
            approval_status="rejected", approved_by=request.user, approved_at=timezone.now()
        )
        self.message_user(request, f"{updated} code(s) rejected.")

    @admin.action(description="Deactivate selected codes (permanent, cannot be undone)")
    def deactivate_codes(self, request, queryset):
        updated = queryset.filter(approval_status="approved", active=True).update(
            active=False, deactivated_by=request.user, deactivated_at=timezone.now()
        )
        self.message_user(request, f"{updated} code(s) deactivated permanently.")


@admin.register(Referral)
class ReferralAdmin(admin.ModelAdmin):
    list_display = ("customer_email", "referral_code", "product", "status", "discount_applied", "created_at")
    list_filter = ("status", "product")
    search_fields = ("customer_name", "customer_email", "referral_code__code")
    date_hierarchy = "created_at"

    def get_urls(self):
        return [path("mis-report/", self.admin_site.admin_view(self.mis_report), name="referrals_mis_report")] + super().get_urls()

    def mis_report(self, request):
        weekly = Referral.objects.annotate(period=TruncWeek("created_at")).values("period", "product").annotate(total=Count("id")).order_by("-period")
        monthly = Referral.objects.annotate(period=TruncMonth("created_at")).values("period", "product").annotate(total=Count("id")).order_by("-period")
        context = dict(self.admin_site.each_context(request), weekly=weekly, monthly=monthly, title="MIS Report")
        return TemplateResponse(request, "admin/referrals/mis_report.html", context)


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    """Read-only in Django admin too: audit records must not be editable or deletable by anyone,
    including admins, to keep them trustworthy for dispute investigation."""
    list_display = ("timestamp", "code_snapshot", "action", "actor", "details")
    list_filter = ("action",)
    search_fields = ("code_snapshot", "actor__username", "details")
    date_hierarchy = "timestamp"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(UserAccess)
class UserAccessAdmin(admin.ModelAdmin):
    list_display = ("user", "allowed_products")
    search_fields = ("user__username",)
