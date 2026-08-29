from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.models import Group, User
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from . import wix_sync
from .forms import AddUserForm, ApplyCodeForm, EditCodeForm, RequestCodeForm
from .models import AuditLog, DEFAULT_CUSTOMER_DISCOUNT, PRODUCT_CHOICES, Referral, ReferralCode, UserAccess


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _in_group(user, group_name):
    return user.is_staff or user.groups.filter(name=group_name).exists()


def _log(action, code, actor, details=""):
    """Write one immutable audit entry. Called from every state-changing view."""
    AuditLog.objects.create(
        referral_code=code,
        code_snapshot=code.code if code else "",
        action=action,
        actor=actor,
        details=details,
    )


def _code_counts(queryset):
    """Shared stat summary used by both the partner/customer and admin dashboards."""
    return {
        "total": queryset.count(),
        "pending": queryset.filter(approval_status="pending").count(),
        "live": queryset.filter(approval_status="approved", active=True).count(),
        "deactivated": queryset.filter(approval_status="approved", active=False).count(),
        "rejected": queryset.filter(approval_status="rejected").count(),
    }


def _allowed_products(user):
    """Which product codes this user may generate referral codes for.
    No UserAccess row = unrestricted (covers admin accounts and pre-existing users)."""
    try:
        return user.access.allowed_products
    except UserAccess.DoesNotExist:
        return [c[0] for c in PRODUCT_CHOICES]


def _sync_approved_code_to_wix(code, actor):
    """
    Push a newly-approved code to Wix as a live coupon (CareerTrek only — see
    wix_sync.should_sync). Never blocks or rolls back the approval: this runs
    after the code is already committed as approved. Failures are recorded on
    the code itself and in the audit log so an admin can see and retry them.
    """
    if not wix_sync.should_sync(code):
        code.wix_sync_status = "not_applicable"
        code.save(update_fields=["wix_sync_status"])
        return

    success, wix_coupon_id, error = wix_sync.create_coupon(code)
    if success:
        code.wix_coupon_id = wix_coupon_id or ""
        code.wix_sync_status = "synced"
        code.wix_sync_error = ""
        code.wix_last_synced_at = timezone.now()
        code.save(update_fields=["wix_coupon_id", "wix_sync_status", "wix_sync_error", "wix_last_synced_at"])
    else:
        code.wix_sync_status = "failed"
        code.wix_sync_error = error or "Unknown error"
        code.save(update_fields=["wix_sync_status", "wix_sync_error"])
        _log("wix_sync_failed", code, actor, f"Create coupon failed: {error}")


def _sync_deactivated_code_to_wix(code, actor):
    """Disable the matching Wix coupon when a code is deactivated on our side."""
    if not wix_sync.should_sync(code):
        return

    success, error = wix_sync.disable_coupon(code)
    if success:
        code.wix_sync_status = "synced"
        code.wix_sync_error = ""
        code.wix_last_synced_at = timezone.now()
        code.save(update_fields=["wix_sync_status", "wix_sync_error", "wix_last_synced_at"])
    else:
        code.wix_sync_status = "failed"
        code.wix_sync_error = error or "Unknown error"
        code.save(update_fields=["wix_sync_status", "wix_sync_error"])
        _log("wix_sync_failed", code, actor, f"Disable coupon failed: {error}")


# ---------------------------------------------------------------------------
# Partner / customer dashboard — each user sees ONLY their own codes
# ---------------------------------------------------------------------------

@login_required
def user_dashboard(request):
    # Ops-only members don't request codes — their job is monitoring, not generating.
    # Send them straight to the Ops dashboard instead of the request-a-code page.
    if _in_group(request.user, "Ops") and not request.user.is_staff:
        return redirect("ops_dashboard")

    allowed_products = _allowed_products(request.user)

    if request.method == "POST":
        form = RequestCodeForm(request.POST, allowed_products=allowed_products)
        if form.is_valid():
            code_type = "partner" if _in_group(request.user, "ChannelPartner") else "customer"
            code = ReferralCode.objects.create(
                code_type=code_type,
                requested_by=request.user,
                owner_name=request.user.get_full_name() or request.user.username,
                owner_email=request.user.email,
                product=form.cleaned_data["product"],
                discount_percent=DEFAULT_CUSTOMER_DISCOUNT,
            )
            _log("requested", code, request.user, f"Requested for {code.get_product_display()}")
            messages.success(request, f"Code {code.code} requested — awaiting admin approval.")
            return redirect("user_dashboard")
    else:
        form = RequestCodeForm(allowed_products=allowed_products)

    my_codes = ReferralCode.objects.filter(requested_by=request.user)
    context = {
        "form": form,
        "my_codes": my_codes,
        "my_counts": _code_counts(my_codes),
        "is_ops": _in_group(request.user, "Ops"),
        "is_partner": _in_group(request.user, "ChannelPartner") and not request.user.is_staff,
        "has_no_products": not allowed_products,
    }

    if request.user.is_staff:
        all_codes = ReferralCode.objects.select_related("requested_by", "approved_by")
        context["all_codes_admin"] = all_codes
        context["admin_counts"] = _code_counts(all_codes)

    return render(request, "referrals/dashboard.html", context)


# ---------------------------------------------------------------------------
# Admin-only state changes (approve / reject / edit / deactivate / delete)
# ---------------------------------------------------------------------------

@login_required
@require_http_methods(["POST"])
def approve_code(request, code_id):
    if not request.user.is_staff:
        raise PermissionDenied("Admin access only.")
    code = get_object_or_404(ReferralCode, id=code_id, approval_status="pending")
    with transaction.atomic():
        code.approval_status = "approved"
        code.approved_by = request.user
        code.approved_at = timezone.now()
        code.save()
        _log("approved", code, request.user)

    # Wix sync happens after the DB transaction commits, so a Wix outage can
    # never roll back or block the approval itself.
    _sync_approved_code_to_wix(code, request.user)

    if code.wix_sync_status == "failed":
        messages.warning(
            request,
            f"{code.code} approved and live, but syncing to Wix failed: {code.wix_sync_error} "
            f"— it will not show in CareerTrek's coupons until this is retried.",
        )
    else:
        messages.success(request, f"{code.code} approved and live.")
    return redirect("user_dashboard")


@login_required
@require_http_methods(["POST"])
def reject_code(request, code_id):
    if not request.user.is_staff:
        raise PermissionDenied("Admin access only.")
    code = get_object_or_404(ReferralCode, id=code_id, approval_status="pending")
    with transaction.atomic():
        code.approval_status = "rejected"
        code.approved_by = request.user
        code.approved_at = timezone.now()
        code.save()
        _log("rejected", code, request.user)
    messages.success(request, f"{code.code} rejected.")
    return redirect("user_dashboard")


@login_required
def edit_code(request, code_id):
    """Owner can edit discount while their code is still pending. Staff can edit anytime,
    including renaming the code itself. Deactivated codes can no longer be edited by anyone —
    the only path forward is requesting a fresh code."""
    code = get_object_or_404(ReferralCode, id=code_id)
    is_owner = code.requested_by_id == request.user.id
    editable = code.approval_status != "approved" or code.active  # blocks edits on deactivated codes
    if not editable:
        raise PermissionDenied("This code is deactivated and can no longer be edited. Request a new code instead.")
    if not (request.user.is_staff or (is_owner and code.approval_status == "pending")):
        raise PermissionDenied("You cannot edit this code.")

    if request.method == "POST":
        form = EditCodeForm(request.POST, instance=code, allow_code_edit=request.user.is_staff)
        if form.is_valid():
            old_code, old_discount = code.code, code.discount_percent
            code.code = form.cleaned_data["code"]
            code.discount_percent = form.cleaned_data["discount_percent"]
            code.save()
            changes = []
            if old_code != code.code:
                changes.append(f"code {old_code} to {code.code}")
            if old_discount != code.discount_percent:
                changes.append(f"discount {old_discount}% to {code.discount_percent}%")
            _log("edited", code, request.user, "; ".join(changes) if changes else "No changes")

            # If this code is already live on Wix and either the code string or the
            # discount changed, push an update so Wix doesn't drift out of sync.
            # (Only reachable by staff, since owners can only edit while pending.)
            if changes and code.wix_sync_status == "synced" and wix_sync.should_sync(code):
                success, error = wix_sync.disable_coupon(code)  # retire the old coupon...
                if success:
                    _sync_approved_code_to_wix(code, request.user)  # ...and create a fresh one
                else:
                    code.wix_sync_status = "failed"
                    code.wix_sync_error = error or "Unknown error"
                    code.save(update_fields=["wix_sync_status", "wix_sync_error"])
                    _log("wix_sync_failed", code, request.user, f"Update after edit failed: {error}")

            messages.success(request, "Code updated.")
            return redirect("user_dashboard")
    else:
        form = EditCodeForm(initial={"code": code.code, "discount_percent": code.discount_percent},
                             instance=code, allow_code_edit=request.user.is_staff)

    return render(request, "referrals/edit_code.html", {"code": code, "form": form, "is_ops": _in_group(request.user, "Ops")})


@login_required
@require_http_methods(["POST"])
def deactivate_code(request, code_id):
    """One-directional: once deactivated a code can never be turned back on."""
    code = get_object_or_404(ReferralCode, id=code_id, approval_status="approved", active=True)
    if not (request.user.is_staff or code.requested_by_id == request.user.id):
        raise PermissionDenied("You cannot modify this code.")
    with transaction.atomic():
        code.active = False
        code.deactivated_at = timezone.now()
        code.deactivated_by = request.user
        code.save()
        _log("deactivated", code, request.user)

    _sync_deactivated_code_to_wix(code, request.user)

    if code.wix_sync_status == "failed":
        messages.warning(
            request,
            f"{code.code} deactivated here, but disabling the matching Wix coupon failed: "
            f"{code.wix_sync_error} — it may still be redeemable on CareerTrek until this is retried.",
        )
    else:
        messages.success(request, f"{code.code} deactivated permanently. Request a new code if you need one.")
    return redirect("user_dashboard")


@login_required
@require_http_methods(["POST"])
def delete_code(request, code_id):
    """Permanently remove a code. Admin only, irreversible. The audit trail survives via code_snapshot."""
    if not request.user.is_staff:
        raise PermissionDenied("Admin access only.")
    code = get_object_or_404(ReferralCode, id=code_id)
    with transaction.atomic():
        _log("deleted", code, request.user)
        code_str = code.code
        code.delete()
    messages.success(request, f"{code_str} permanently deleted.")
    return redirect("user_dashboard")


# ---------------------------------------------------------------------------
# Manage Users — admin only, create accounts, set designation and product access
# ---------------------------------------------------------------------------

@login_required
def manage_users(request):
    if not request.user.is_staff:
        raise PermissionDenied("Admin access only.")

    if request.method == "POST":
        form = AddUserForm(request.POST)
        if form.is_valid():
            designation = form.cleaned_data["designation"]
            with transaction.atomic():
                user = User.objects.create_user(
                    username=form.cleaned_data["username"],
                    email=form.cleaned_data["email"],
                    password=form.cleaned_data["password"],
                )
                if designation == "admin":
                    user.is_staff = True
                    user.is_superuser = True
                    user.save()
                elif designation == "ops":
                    user.groups.add(Group.objects.get_or_create(name="Ops")[0])
                elif designation == "channel_partner":
                    user.groups.add(Group.objects.get_or_create(name="ChannelPartner")[0])
                # "customer" gets no group — matches existing default-customer logic

                if designation in ("channel_partner", "customer"):
                    UserAccess.objects.create(user=user, allowed_products=form.cleaned_data["allowed_products"])

                _log("user_created", None, request.user,
                     f"Created {user.username} as {dict(form.fields['designation'].choices)[designation]}"
                     + (f", products: {', '.join(form.cleaned_data['allowed_products'])}" if designation in ("channel_partner", "customer") else ""))

            messages.success(request, f"User {user.username} created.")
            return redirect("manage_users")
    else:
        form = AddUserForm()

    users = User.objects.select_related("access").prefetch_related("groups").exclude(id=request.user.id).order_by("username")
    rows = []
    for u in users:
        if u.is_superuser:
            role = "Admin"
        elif u.groups.filter(name="Ops").exists():
            role = "Operations Team"
        elif u.groups.filter(name="ChannelPartner").exists():
            role = "Channel Partner"
        else:
            role = "End Customer"
        try:
            products = ", ".join(dict(PRODUCT_CHOICES)[p] for p in u.access.allowed_products) or "None set"
        except UserAccess.DoesNotExist:
            products = "All (unrestricted)"
        rows.append({"user": u, "role": role, "products": products})

    return render(request, "referrals/manage_users.html", {"form": form, "rows": rows, "is_ops": _in_group(request.user, "Ops")})


# ---------------------------------------------------------------------------
# Audit log — admin only, read-only, for dispute investigation
# ---------------------------------------------------------------------------

@login_required
def audit_log(request):
    if not request.user.is_staff:
        raise PermissionDenied("Admin access only.")
    entries = AuditLog.objects.select_related("actor", "referral_code")
    q = request.GET.get("q", "").strip()
    if q:
        entries = entries.filter(Q(code_snapshot__icontains=q) | Q(actor__username__icontains=q))
    return render(request, "referrals/audit_log.html", {"entries": entries[:300], "q": q})


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------

@login_required
def change_password(request):
    if request.method == "POST":
        form = PasswordChangeForm(request.user, request.POST)
        if form.is_valid():
            user = form.save()
            update_session_auth_hash(request, user)
            messages.success(request, "Password updated successfully.")
            return redirect("user_dashboard")
    else:
        form = PasswordChangeForm(request.user)
    return render(request, "referrals/change_password.html", {"form": form, "is_ops": _in_group(request.user, "Ops")})


# ---------------------------------------------------------------------------
# Ops — read-only reference view of live + deactivated codes
# ---------------------------------------------------------------------------

@login_required
def ops_dashboard(request):
    if not _in_group(request.user, "Ops"):
        raise PermissionDenied("Ops access only.")
    codes = ReferralCode.objects.filter(approval_status="approved").select_related("requested_by", "approved_by")
    return render(request, "referrals/ops_dashboard.html", {
        "live_codes": codes,
        "ops_counts": {"live": codes.filter(active=True).count(), "deactivated": codes.filter(active=False).count()},
        "is_ops": True,
    })


# ---------------------------------------------------------------------------
# Public checkout — apply a code, no login required
# ---------------------------------------------------------------------------

def apply_code_page(request):
    return render(request, "referrals/apply_code.html", {"products": PRODUCT_CHOICES})


@require_http_methods(["POST"])
def apply_referral_code(request):
    form = ApplyCodeForm(request.POST)
    if not form.is_valid():
        first_error = next(iter(form.errors.values()))[0]
        return JsonResponse({"error": first_error}, status=400)

    try:
        ref = ReferralCode.objects.get(code=form.cleaned_data["code"], approval_status="approved", active=True)
    except ReferralCode.DoesNotExist:
        return JsonResponse({"error": "Invalid, unapproved or deactivated referral code"}, status=404)
    if ref.product != form.cleaned_data["product"]:
        return JsonResponse({"error": "Code not valid for selected product"}, status=400)

    Referral.objects.create(
        referral_code=ref,
        customer_name=form.cleaned_data["name"],
        customer_email=form.cleaned_data["email"],
        product=form.cleaned_data["product"],
        discount_applied=ref.discount_percent,
    )
    _log("redeemed", ref, None, f"Redeemed by {form.cleaned_data['email']}")
    return JsonResponse({"discount_percent": ref.discount_percent, "message": "Referral applied successfully"})