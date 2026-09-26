from datetime import timedelta

from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.models import Group, User
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
import json
from . import wix_sync
from .masking import mask_code, mask_email
import logging, hmac, hashlib
from .forms import AddUserForm, ApplyCodeForm, EditCodeForm, RequestCodeForm
from .models import (
    DEFAULT_CODE_VALIDITY_YEARS, AuditLog, DEFAULT_CUSTOMER_DISCOUNT, PRODUCT_CHOICES,
    PartnerOnboardingRequest, PurchaseRewardIssuance, Referral, ReferralCode, UserAccess,
    StarterCouponIssuance,
)

logger = logging.getLogger(__name__)
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


def _attach_purchase_rewards(codes):
    """
    Attach a `.purchase_reward` attribute (a PurchaseRewardIssuance or None)
    to each ReferralCode in `codes`, so templates can show the order/buyer
    that earned a Wix-minted reward coupon without a per-row query.
    `codes` must be a concrete list (not a lazy queryset) — callers materialize
    it with list() before passing it in.
    """
    reward_by_code_id = {
        pri.referral_code_id: pri
        for pri in PurchaseRewardIssuance.objects.filter(referral_code__in=codes)
    }
    for c in codes:
        c.purchase_reward = reward_by_code_id.get(c.id)
    return codes


def _sync_approved_code_to_wix(code, actor):
    """
    Push a newly-approved code to Wix as a live coupon (KareerTrek only — see
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


def _check_api_key(request):
    header_key = request.headers.get("X-Internal-Api-Key")
    query_key = request.GET.get("api_key")
    return header_key == settings.INTERNAL_API_KEY or query_key == settings.INTERNAL_API_KEY


def _leadgen_system_user():
    """
    Service account used to attribute ReferralCodes auto-issued via the
    Lead Gen API (starter coupons) rather than through the admin UI.
    is_active=False so it can never be used to log in.
    """
    user, _ = User.objects.get_or_create(
        username="leadgen_system",
        defaults={"is_staff": False, "is_active": False},
    )
    return user

def _wix_system_user():
    """
    Service account used to attribute ReferralCodes and Referrals that
    originate on Wix's side (coupon created during a KareerTrek purchase,
    or a usage event) rather than through the admin UI. is_active=False so
    it can never be used to log in.
    """
    user, _ = User.objects.get_or_create(
        username="wix_system",
        defaults={"is_staff": False, "is_active": False},
    )
    return user

# ---------------------------------------------------------------------------
# Partner / customer dashboard — each user sees ONLY their own codes
# ---------------------------------------------------------------------------

def _time_of_day_greeting():
    """Small warmth touch, matching the same one added to Lead Gen's
    dashboard — 'Good morning/afternoon/evening' instead of a static
    'Welcome back' regardless of when someone actually opens the app."""
    hour = timezone.localtime().hour
    if hour < 12:
        return "Good morning"
    if hour < 18:
        return "Good afternoon"
    return "Good evening"


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
        "greeting": _time_of_day_greeting(),
    }

    if request.user.is_staff:
        all_codes = ReferralCode.objects.select_related("requested_by", "approved_by")
        context["admin_counts"] = _code_counts(all_codes)

        # Materialize so each code can carry its linked PurchaseRewardIssuance
        # (order id, buyer) for coupons minted automatically by the Wix
        # "Plan ordered" automation — the dashboard's only view into which
        # purchase earned a given reward coupon.
        all_codes_admin = _attach_purchase_rewards(list(all_codes))
        context["all_codes_admin"] = all_codes_admin

        partner_requests = PartnerOnboardingRequest.objects.filter(status="pending").order_by("created_at")
        context["partner_requests"] = partner_requests
        context["partner_request_count"] = partner_requests.count()
        context["product_choices"] = PRODUCT_CHOICES
        context["failed_deliveries"] = PartnerOnboardingRequest.objects.filter(
            status="approved",
            callback_delivered=False,
            referral_code__isnull=False,
        ).select_related("referral_code").order_by("-referral_code__approved_at")

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
        now = timezone.now()
        code.approval_status = "approved"
        code.approved_by = request.user
        code.approved_at = now
        code.expires_at = now + timedelta(days=365 * DEFAULT_CODE_VALIDITY_YEARS)
        code.save()
        _log("approved", code, request.user)

    # Wix sync happens after the DB transaction commits, so a Wix outage can
    # never roll back or block the approval itself.
    _sync_approved_code_to_wix(code, request.user)

    if code.wix_sync_status == "failed":
        messages.warning(
            request,
            f"{code.code} approved and live, but syncing to Wix failed: {code.wix_sync_error} "
            f"— it will not show in KareerTrek's coupons until this is retried.",
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

def _fmt_date(d):
    return d.strftime("%Y-%m-%d") if d else "never"

@login_required
def edit_code(request, code_id):
    """Owner can edit discount while their code is still pending. Staff can edit anytime,
    including renaming the code itself and adjusting its expiry. Deactivated codes can no
    longer be edited by anyone — the only path forward is requesting a fresh code."""
    code = get_object_or_404(ReferralCode, id=code_id)

    # requested_by is NOT always the code's real owner: approve_partner_request
    # sets it to the admin who clicked Approve, not the external partner
    # (who isn't a Referral Hub user at all — they only exist as a
    # PartnerOnboardingRequest row). Without this check, that admin would be
    # treated as the "owner" of every partner code they've ever approved,
    # and see it unmasked here indefinitely — exactly backwards from the
    # masking rule this page is supposed to enforce for staff viewing
    # someone else's code. A genuinely self-requested code (a customer or
    # Channel Partner using their own dashboard) never has a
    # PartnerOnboardingRequest linked to it, so this only changes the
    # outcome for the admin-approved case.
    is_owner = (
        code.requested_by_id == request.user.id
        and not PartnerOnboardingRequest.objects.filter(referral_code=code).exists()
    )

    editable = code.approval_status != "approved" or code.active  # blocks edits on deactivated codes
    if not editable:
        raise PermissionDenied("This code is deactivated and can no longer be edited. Request a new code instead.")
    if not (request.user.is_staff or (is_owner and code.approval_status == "pending")):
        raise PermissionDenied("You cannot edit this code.")

    if request.method == "POST":
        form = EditCodeForm(
            request.POST,
            instance=code,
            allow_code_edit=request.user.is_staff,
            initial={"code": code.code, "discount_percent": code.discount_percent, "expires_at": code.expires_at},
        )
        if form.is_valid():
            old_code, old_discount, old_expiry = code.code, code.discount_percent, code.expires_at
            code.code = form.cleaned_data["code"]
            code.discount_percent = form.cleaned_data["discount_percent"]
            code.expires_at = form.cleaned_data["expires_at"]
            code.save()

            changes = []
            if old_code != code.code:
                changes.append(f"code {mask_code(old_code)} to {mask_code(code.code)}")
            if old_discount != code.discount_percent:
                changes.append(f"discount {old_discount}% to {code.discount_percent}%")
            if old_expiry != code.expires_at:
                changes.append(f"expiry {_fmt_date(old_expiry)} to {_fmt_date(code.expires_at)}")
            _log("edited", code, request.user, "; ".join(changes) if changes else "No changes")

            # If this code is already live on Wix and the code string or discount changed,
            # push an update so Wix doesn't drift out of sync. (Expiry alone doesn't trigger
            # a Wix resync unless wix_sync/Wix's coupon model actually tracks expiry too —
            # see the earlier note on whether Wix should mirror expires_at.)
            if (old_code != code.code or old_discount != code.discount_percent) \
                    and code.wix_sync_status == "synced" and wix_sync.should_sync(code):
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
        form = EditCodeForm(
            initial={"code": code.code, "discount_percent": code.discount_percent, "expires_at": code.expires_at},
            instance=code,
            allow_code_edit=request.user.is_staff,
        )

    return render(request, "referrals/edit_code.html", {
        "code": code, "form": form, "is_owner": is_owner,
        "is_ops": _in_group(request.user, "Ops"),
    })


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
    _notify_leadgen_of_deactivation(code, request.user)

    if code.wix_sync_status == "failed":
        messages.warning(
            request,
            f"{code.code} deactivated here, but disabling the matching Wix coupon failed: "
            f"{code.wix_sync_error} — it may still be redeemable on KareerTrek until this is retried.",
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
    now = timezone.now()
    ops_counts = {
        "live": codes.filter(active=True).exclude(expires_at__lte=now).count(),
        "deactivated": codes.filter(active=False).count(),
        "expired": codes.filter(active=True, expires_at__lte=now).count(),
    }

    # Materialize so each code can carry its linked PurchaseRewardIssuance
    # (order id, buyer) for Wix-minted reward coupons — lets Ops trace a
    # code back to the purchase that earned it without a separate lookup.
    live_codes = _attach_purchase_rewards(list(codes))

    return render(request, "referrals/ops_dashboard.html", {
        "live_codes": live_codes,
        "ops_counts": ops_counts,
        "is_ops": True,
        "greeting": _time_of_day_greeting(),
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
    _log("redeemed", ref, None, f"Redeemed by {mask_email(form.cleaned_data['email'])}")
    return JsonResponse({"discount_percent": ref.discount_percent, "message": "Referral applied successfully"})


# ---------------------------------------------------------------------------
# Partner onboarding — inbound from Lead Gen ("Become a Channel Partner")
# ---------------------------------------------------------------------------

@csrf_exempt
def create_partner_request(request):
    if request.method != "POST" or not _check_api_key(request):
        return JsonResponse({"error": "unauthorized"}, status=401)
    data = json.loads(request.body)
    external_user_id = str(data["user_id"])

    existing = PartnerOnboardingRequest.objects.filter(
        source_system="leadgen", external_user_id=external_user_id
    ).select_related("referral_code").first()

    if existing and existing.status == "pending":
        return JsonResponse({"status": "pending", "request_id": existing.id})

    # An "approved" request only stays blocking while its code is still
    # live. Once that code is deactivated, status here never changes on its
    # own — nothing updates PartnerOnboardingRequest.status when the linked
    # ReferralCode is deactivated (only the code and, via the deactivation
    # webhook, Lead Gen's own PartnerApplication row change). Without this
    # check, a partner whose code was deactivated could never be re-queued
    # for approval: this same unique (source_system, external_user_id) row
    # would keep returning "approved" forever.
    if existing and existing.status == "approved" and existing.referral_code and existing.referral_code.is_live:
        return JsonResponse({"status": "approved", "request_id": existing.id})

    # No existing request, or the previous one was rejected, or its
    # approved code has since been deactivated — (re)queue it. update_or_create
    # reuses the same row rather than creating a second one, since
    # (source_system, external_user_id) is unique.
    req, _ = PartnerOnboardingRequest.objects.update_or_create(
        source_system="leadgen",
        external_user_id=external_user_id,
        defaults={
            "external_email": data["email"],
            "external_name": data.get("name", ""),
            "application_data": data.get("application_data", {}),
            "status": "pending",
            "product": "",
            "referral_code": None,
            "callback_delivered": False,
            "deactivation_delivered": False,
        },
    )
    return JsonResponse({"status": "queued", "request_id": req.id})


@login_required
@require_http_methods(["POST"])
def approve_partner_request(request, request_id):
    if not request.user.is_staff:
        raise PermissionDenied("Admin access only.")
    req = get_object_or_404(PartnerOnboardingRequest, id=request_id, status="pending")

    product = request.POST.get("product", "")
    if product not in dict(PRODUCT_CHOICES):
        messages.error(request, "Choose a valid product before approving.")
        return redirect("user_dashboard")

    try:
        discount_percent = int(request.POST.get("discount_percent", ""))
        if not (1 <= discount_percent <= 100):
            raise ValueError
    except ValueError:
        messages.error(request, "Discount must be a whole number between 1 and 100.")
        return redirect("user_dashboard")

    with transaction.atomic():
        now = timezone.now()
        code = ReferralCode.objects.create(
            code_type="partner",
            requested_by=request.user,
            owner_name=req.external_name,
            owner_email=req.external_email,
            product=product,
            discount_percent=discount_percent,
            approval_status="approved",
            approved_by=request.user,
            approved_at=now,
            expires_at=now + timedelta(days=365 * DEFAULT_CODE_VALIDITY_YEARS),
            # This code exists because of a Lead Gen partner-onboarding
            # application, not because someone used Referral Hub directly —
            # requested_by is only the approving admin (see edit_code's
            # is_owner fix), so origin_system is the only place this
            # distinction is actually recorded and shown on the dashboards.
            origin_system="leadgen",
        )
        req.status = "approved"
        req.product = product
        req.referral_code = code
        req.save(update_fields=["status", "product", "referral_code"])
        _log("approved", code, request.user,
             f"Partner onboarding for {mask_email(req.external_email)} ({discount_percent}% off {code.get_product_display()})")

    _sync_approved_code_to_wix(code, request.user)
    _deliver_code_to_leadgen(req)

    if code.wix_sync_status == "failed":
        messages.warning(request, f"{code.code} approved, but Wix sync failed: {code.wix_sync_error}")
    elif not req.callback_delivered:
        messages.warning(request, f"{code.code} approved, but delivering it to Lead Gen Tool failed — retry from the table below.")
    else:
        messages.success(request, f"{code.code} approved and sent to {req.external_email}.")

    return redirect("user_dashboard")


@login_required
@require_http_methods(["POST"])
def reject_partner_request(request, request_id):
    if not request.user.is_staff:
        raise PermissionDenied("Admin access only.")
    req = get_object_or_404(PartnerOnboardingRequest, id=request_id, status="pending")
    with transaction.atomic():
        req.status = "rejected"
        req.save(update_fields=["status"])
        _log("rejected", None, request.user, f"Partner onboarding request rejected for {mask_email(req.external_email)}")
    messages.success(request, f"Request from {req.external_email} rejected.")
    return redirect("user_dashboard")


@require_http_methods(["POST"])
def retry_partner_delivery(request, request_id):
    if not request.user.is_staff:
        raise PermissionDenied("Admin access only.")
    req = get_object_or_404(
        PartnerOnboardingRequest, id=request_id, status="approved",
        callback_delivered=False, referral_code__isnull=False,
    )
    _deliver_code_to_leadgen(req)
    if req.callback_delivered:
        messages.success(request, f"{req.referral_code.code} delivered to Lead Gen Tool.")
    else:
        messages.warning(request, f"Retry failed — {req.referral_code.code} still not delivered. Check Lead Gen Tool connectivity.")
    return redirect("user_dashboard")


def _deliver_code_to_leadgen(req):
    import requests

    base_url = settings.LEADGEN_BASE_URL
    if not base_url.lower().startswith("https://"):
        logger.error("Refused to deliver %s: LEADGEN_BASE_URL is not HTTPS", mask_code(req.referral_code.code))
        req.callback_delivered = False
        req.save(update_fields=["callback_delivered"])
        return

    payload = {
        "user_id": req.external_user_id,
        "code": req.referral_code.code,
        "discount_percent": req.referral_code.discount_percent,
        "product": req.product,
    }
    headers = {"X-Internal-Api-Key": settings.INTERNAL_API_KEY}

    # Optional: sign the body so Lead Gen Tool can verify it wasn't tampered
    # with, on top of TLS. Set LEADGEN_SHARED_SECRET in settings to enable.
    shared_secret = getattr(settings, "LEADGEN_SHARED_SECRET", None)
    if shared_secret:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        headers["X-Signature"] = hmac.new(shared_secret.encode(), body.encode(), hashlib.sha256).hexdigest()

    try:
        resp = requests.post(
            f"{base_url}/api/partner-code-assigned/",
            json=payload, headers=headers, timeout=20,
        )
        logger.info("Lead Gen delivery for %s: HTTP %s", mask_code(req.referral_code.code), resp.status_code)
        req.callback_delivered = resp.ok
        req.save(update_fields=["callback_delivered"])
    except requests.RequestException as e:
        logger.warning("Lead Gen delivery failed for %s: %s", mask_code(req.referral_code.code), e)
        req.callback_delivered = False
        req.save(update_fields=["callback_delivered"])


# ---------------------------------------------------------------------------
# Lead Gen Tool integration — inbound API called by leads/referral_hub_client.py
# All views here use X-Internal-Api-Key auth (no session/login), csrf_exempt.
# ---------------------------------------------------------------------------

@csrf_exempt
@require_http_methods(["POST"])
def request_starter_coupon_api(request):
    """
    POST /referral/starter-coupon
    Auto-issues (no admin approval step) a customer-type ReferralCode for a
    Lead Gen signup, and records the link in StarterCouponIssuance so
    /referral/my-coupon can look it up again later (e.g. after a missed
    webhook on the Lead Gen side).
    """
    if not _check_api_key(request):
        return JsonResponse({"error": "unauthorized"}, status=401)

    try:
        data = json.loads(request.body)
        user_id = str(data["user_id"])
    except (json.JSONDecodeError, KeyError):
        return JsonResponse({"error": "invalid payload"}, status=400)

    email = data.get("email", "")
    name = data.get("name", "")
    product = data.get("product", "kareertrek")  # default product for starter coupons

    if product not in dict(PRODUCT_CHOICES):
        return JsonResponse({"error": "invalid product"}, status=400)

    # Idempotent, but only while the existing coupon is still live: if this
    # Lead Gen user already has a starter coupon AND it hasn't been
    # deactivated/expired since, return the same one instead of minting a
    # second one. If it's no longer live, fall through and mint a fresh
    # coupon instead — the user has explicitly asked for a new one (via
    # Lead Gen's "Request New Coupon" flow) after the old one was
    # deactivated, and returning the same dead code here would make the
    # re-request a silent no-op.
    existing = StarterCouponIssuance.objects.filter(
        source_system="leadgen", external_user_id=user_id
    ).select_related("referral_code").first()
    if existing and existing.referral_code.is_live:
        code = existing.referral_code
        return JsonResponse({
            "owner_id": user_id,
            "coupon_id": code.id,
            "code": code.code,
            "discount_percent": code.discount_percent,
            "applicable_products": [code.product],
            "valid_until": code.expires_at.date().isoformat() if code.expires_at else None,
        })

    system_user = _leadgen_system_user()

    with transaction.atomic():
        now = timezone.now()
        code = ReferralCode.objects.create(
            code_type="customer",
            requested_by=system_user,
            owner_name=name,
            owner_email=email,
            product=product,
            discount_percent=DEFAULT_CUSTOMER_DISCOUNT,
            approval_status="approved",
            approved_by=system_user,
            approved_at=now,
            expires_at=now + timedelta(days=365 * DEFAULT_CODE_VALIDITY_YEARS),
            origin_system="leadgen",
        )
        if existing:
            # Re-point the same issuance row at the new code rather than
            # creating a second row — (source_system, external_user_id) is
            # unique, so there can only ever be one issuance per Lead Gen
            # user regardless of how many times their coupon is reissued.
            existing.referral_code = code
            existing.external_email = email
            existing.deactivation_delivered = False
            existing.save(update_fields=["referral_code", "external_email", "deactivation_delivered"])
            _log("approved", code, system_user,
                 f"Starter coupon re-issued for Lead Gen user {user_id} ({mask_email(email)}) — previous coupon was no longer live")
        else:
            StarterCouponIssuance.objects.create(
                source_system="leadgen",
                external_user_id=user_id,
                external_email=email,
                referral_code=code,
            )
            _log("approved", code, system_user, f"Starter coupon auto-issued for Lead Gen user {user_id} ({mask_email(email)})")

    _sync_approved_code_to_wix(code, system_user)

    return JsonResponse({
        "owner_id": user_id,
        "coupon_id": code.id,
        "code": code.code,
        "discount_percent": code.discount_percent,
        "applicable_products": [code.product],
        "valid_until": code.expires_at.date().isoformat() if code.expires_at else None,
    })


@csrf_exempt
@require_http_methods(["POST"])
def regenerate_starter_coupon_api(request):
    """
    POST /referral/regenerate-coupon
    Explicit user-initiated replacement of a coupon that may still be
    live — distinct from request_starter_coupon_api, which is idempotent
    by design and refuses to touch a code that's still working. This one
    always deactivates whatever's currently linked (if it's still live)
    and mints a brand-new code in its place, so a user can voluntarily
    rotate their coupon (it leaked, they just want a fresh one) without
    needing an admin to deactivate the old one first.
    """
    if not _check_api_key(request):
        return JsonResponse({"error": "unauthorized"}, status=401)

    try:
        data = json.loads(request.body)
        user_id = str(data["user_id"])
    except (json.JSONDecodeError, KeyError):
        return JsonResponse({"error": "invalid payload"}, status=400)

    email = data.get("email", "")
    name = data.get("name", "")
    product = data.get("product", "kareertrek")

    if product not in dict(PRODUCT_CHOICES):
        return JsonResponse({"error": "invalid product"}, status=400)

    existing = StarterCouponIssuance.objects.filter(
        source_system="leadgen", external_user_id=user_id
    ).select_related("referral_code").first()

    system_user = _leadgen_system_user()
    old_code = None

    with transaction.atomic():
        if existing and existing.referral_code.is_live:
            old_code = existing.referral_code
            old_code.active = False
            old_code.deactivated_at = timezone.now()
            old_code.save(update_fields=["active", "deactivated_at"])
            _log("deactivated", old_code, system_user,
                 f"Deactivated for regeneration (Lead Gen user {user_id})")

        now = timezone.now()
        code = ReferralCode.objects.create(
            code_type="customer",
            requested_by=system_user,
            owner_name=name,
            owner_email=email,
            product=product,
            discount_percent=DEFAULT_CUSTOMER_DISCOUNT,
            approval_status="approved",
            approved_by=system_user,
            approved_at=now,
            expires_at=now + timedelta(days=365 * DEFAULT_CODE_VALIDITY_YEARS),
            origin_system="leadgen",
        )
        if existing:
            existing.referral_code = code
            existing.external_email = email
            existing.deactivation_delivered = False
            existing.save(update_fields=["referral_code", "external_email", "deactivation_delivered"])
        else:
            StarterCouponIssuance.objects.create(
                source_system="leadgen",
                external_user_id=user_id,
                external_email=email,
                referral_code=code,
            )
        _log("approved", code, system_user,
             f"Starter coupon regenerated for Lead Gen user {user_id} ({mask_email(email)})")

    # Wix syncs happen after the transaction commits, same pattern as every
    # other approve/deactivate path — a Wix outage on either call must never
    # roll back the DB state that's already been committed.
    if old_code is not None:
        _sync_deactivated_code_to_wix(old_code, system_user)
    _sync_approved_code_to_wix(code, system_user)

    return JsonResponse({
        "owner_id": user_id,
        "coupon_id": code.id,
        "code": code.code,
        "discount_percent": code.discount_percent,
        "applicable_products": [code.product],
        "valid_until": code.expires_at.date().isoformat() if code.expires_at else None,
    })


@csrf_exempt
@require_http_methods(["GET"])
def my_coupon_api(request):
    """GET /referral/my-coupon?user_id=... — used by Lead Gen to (re)sync
    coupon state if a webhook was missed or the user hits refresh."""
    if not _check_api_key(request):
        return JsonResponse({"error": "unauthorized"}, status=401)

    user_id = request.GET.get("user_id", "")
    if not user_id:
        return JsonResponse({"error": "user_id required"}, status=400)

    issuance = StarterCouponIssuance.objects.filter(
        source_system="leadgen", external_user_id=user_id
    ).select_related("referral_code").first()

    if not issuance:
        return JsonResponse({"error": "not found"}, status=404)

    code = issuance.referral_code
    # display_status distinguishes "deactivated" from "expired" — the
    # previous "active" if is_live else "expired" check collapsed both
    # into "expired", so a deactivated coupon was misreported to Lead Gen
    # as merely expired. approval_status is always "approved" for starter
    # coupons (see request_starter_coupon_api), so display_status here is
    # one of "approved" (still live), "deactivated", or "expired".
    status_map = {"approved": "active", "deactivated": "deactivated", "expired": "expired"}
    return JsonResponse({
        "owner_id": user_id,
        "coupon_id": code.id,
        "code": code.code,
        "discount_percent": code.discount_percent,
        "applicable_products": [code.product],
        "valid_until": code.expires_at.date().isoformat() if code.expires_at else None,
        "status": status_map.get(code.display_status, code.display_status),
    })


@csrf_exempt
@require_http_methods(["GET"])
def my_usage_api(request):
    """GET /referral/my-usage?user_id=... — returns redemption events for
    this Lead Gen user's starter coupon. Referral Hub is the source of
    truth; Lead Gen only caches this for display."""
    if not _check_api_key(request):
        return JsonResponse({"error": "unauthorized"}, status=401)

    user_id = request.GET.get("user_id", "")
    if not user_id:
        return JsonResponse({"error": "user_id required"}, status=400)

    issuance = StarterCouponIssuance.objects.filter(
        source_system="leadgen", external_user_id=user_id
    ).select_related("referral_code").first()

    if not issuance:
        return JsonResponse({"total_referrals": 0, "successful_uses": 0, "events": []})

    referrals = Referral.objects.filter(referral_code=issuance.referral_code).order_by("-created_at")

    total_referrals = referrals.count()
    successful_uses = referrals.filter(status="converted").count()

    events = [
        {
            "event_id": f"ref-{r.id}",
            "customer_label": r.customer_email,
            "product": r.product,
            "occurred_at": r.created_at.isoformat(),
        }
        for r in referrals[:50]  # slice only here, for display purposes
    ]

    return JsonResponse({
        "total_referrals": total_referrals,
        "successful_uses": successful_uses,
        "events": events,
    })


@csrf_exempt
@require_http_methods(["POST"])
def partner_request_api(request):
    """POST /referral/partner-request — same payload/behavior as
    api/partner-requests/, exposed under the /referral/ prefix so it
    matches what referral_hub_client.submit_partner_request() calls."""
    return create_partner_request(request)


@csrf_exempt
@require_http_methods(["GET"])
def partner_request_status_api(request):
    """GET /referral/partner-request/status?user_id=... — used by Lead Gen
    to (re)sync partner application state, e.g. if a decision or
    deactivation webhook was missed or the user hits refresh."""
    if not _check_api_key(request):
        return JsonResponse({"error": "unauthorized"}, status=401)

    user_id = request.GET.get("user_id", "")
    if not user_id:
        return JsonResponse({"error": "user_id required"}, status=400)

    req = PartnerOnboardingRequest.objects.select_related("referral_code").filter(
        source_system="leadgen", external_user_id=user_id
    ).order_by("-created_at").first()

    if not req:
        return JsonResponse({"error": "not found"}, status=404)

    # req.status only ever moves pending -> approved/rejected — nothing
    # resets it when the underlying code is later deactivated (delivered
    # separately, via _deliver_partner_deactivation_to_leadgen) or deleted
    # (SET_NULLs req.referral_code without touching req.status at all). So
    # reporting req.status directly would say "approved" forever even
    # after the code stopped being valid. Check the code's actual
    # liveness instead, same fix as my_coupon_api's equivalent bug.
    if req.status == "approved":
        code = req.referral_code
        reported_status = "approved" if (code is not None and code.is_live) else "deactivated"
    else:
        reported_status = req.status

    response = {"status": reported_status}
    if req.referral_code is not None:
        response["code"] = req.referral_code.code
        response["discount_percent"] = req.referral_code.discount_percent
    return JsonResponse(response)


def _deliver_partner_deactivation_to_leadgen(req):
    """Symmetric to _deliver_code_to_leadgen, but for the deactivation event."""
    import requests

    base_url = settings.LEADGEN_BASE_URL
    if not base_url.lower().startswith("https://"):
        logger.error("Refused to deliver deactivation for %s: LEADGEN_BASE_URL is not HTTPS",
                      mask_code(req.referral_code.code))
        req.deactivation_delivered = False
        req.save(update_fields=["deactivation_delivered"])
        return

    payload = {"user_id": req.external_user_id, "code": req.referral_code.code}
    headers = {"X-Internal-Api-Key": settings.INTERNAL_API_KEY}

    shared_secret = getattr(settings, "LEADGEN_SHARED_SECRET", None)
    if shared_secret:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        headers["X-Signature"] = hmac.new(shared_secret.encode(), body.encode(), hashlib.sha256).hexdigest()

    try:
        resp = requests.post(
            f"{base_url}/api/partner-code-deactivated/",
            json=payload, headers=headers, timeout=20,
        )
        logger.info("Lead Gen deactivation delivery for %s: HTTP %s",
                     mask_code(req.referral_code.code), resp.status_code)
        req.deactivation_delivered = resp.ok
        req.save(update_fields=["deactivation_delivered"])
    except requests.RequestException as e:
        logger.warning("Lead Gen deactivation delivery failed for %s: %s",
                        mask_code(req.referral_code.code), e)
        req.deactivation_delivered = False
        req.save(update_fields=["deactivation_delivered"])


def _deliver_coupon_deactivation_to_leadgen(issuance):
    """Same idea, for starter (customer) coupons issued via the Lead Gen signup flow."""
    import requests

    base_url = settings.LEADGEN_BASE_URL
    if not base_url.lower().startswith("https://"):
        logger.error("Refused to deliver deactivation for %s: LEADGEN_BASE_URL is not HTTPS",
                      mask_code(issuance.referral_code.code))
        issuance.deactivation_delivered = False
        issuance.save(update_fields=["deactivation_delivered"])
        return

    payload = {"user_id": issuance.external_user_id, "code": issuance.referral_code.code}
    headers = {"X-Internal-Api-Key": settings.INTERNAL_API_KEY}

    shared_secret = getattr(settings, "LEADGEN_SHARED_SECRET", None)
    if shared_secret:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        headers["X-Signature"] = hmac.new(shared_secret.encode(), body.encode(), hashlib.sha256).hexdigest()

    try:
        resp = requests.post(
            f"{base_url}/api/coupon-deactivated/",
            json=payload, headers=headers, timeout=20,
        )
        logger.info("Lead Gen coupon deactivation delivery for %s: HTTP %s",
                     mask_code(issuance.referral_code.code), resp.status_code)
        issuance.deactivation_delivered = resp.ok
        issuance.save(update_fields=["deactivation_delivered"])
    except requests.RequestException as e:
        logger.warning("Lead Gen coupon deactivation delivery failed for %s: %s",
                        mask_code(issuance.referral_code.code), e)
        issuance.deactivation_delivered = False
        issuance.save(update_fields=["deactivation_delivered"])


def _notify_leadgen_of_deactivation(code, actor):
    """Called after a code is deactivated. Figures out whether this code
    came from a partner onboarding request or a starter-coupon issuance
    (or neither — e.g. a code created directly in the admin UI, which has
    nothing to notify) and pushes the deactivation to Lead Gen Tool."""
    partner_req = PartnerOnboardingRequest.objects.filter(
        referral_code=code, status="approved"
    ).first()
    if partner_req:
        _deliver_partner_deactivation_to_leadgen(partner_req)
        if not partner_req.deactivation_delivered:
            _log("leadgen_delivery_failed", code, actor,
                 f"Deactivation notice to Lead Gen Tool failed for partner request ({mask_code(code.code)})")
        return

    issuance = StarterCouponIssuance.objects.filter(referral_code=code).first()
    if issuance:
        _deliver_coupon_deactivation_to_leadgen(issuance)
        if not issuance.deactivation_delivered:
            _log("leadgen_delivery_failed", code, actor,
                 f"Deactivation notice to Lead Gen Tool failed for starter coupon ({mask_code(code.code)})")


# ---------------------------------------------------------------------------
# KareerTrek / Wix integration — inbound APIs for coupons created directly
# on Wix (including the automatic purchase-reward coupon minted by the
# "Plan ordered" Velo automation) and for usage events from purchases made
# on KareerTrek. Server-to-server, same X-Internal-Api-Key auth as the Lead
# Gen integration.
#
# NOTE: coupons are now always minted on the Wix side first (see the "Plan
# ordered" automation). Referral Hub no longer mints reward coupons itself
# and pushes them to Wix — the old /careertrek/purchase-reward-coupon
# endpoint that did that has been removed since nothing calls it anymore.
# wix_coupon_created_api below is the single inbound path for both manually
# created Wix coupons and automatic purchase-reward coupons.
# ---------------------------------------------------------------------------

@csrf_exempt
@require_http_methods(["POST"])
def wix_coupon_created_api(request):
    """
    POST /kareertrek/coupon-created
    Called when a coupon is created directly on Wix — either a manual coupon,
    or the automatic purchase-reward coupon minted by the "Plan ordered" Velo
    automation right after checkout. Mirrors it into ReferralCode so it shows
    up in the Hub's dashboards, audit log, and usage tracking.

    Idempotent on wix_coupon_id: replaying the same event updates the
    existing record instead of creating a duplicate.

    When order_id + user_id are included (the purchase-reward case), also
    records a PurchaseRewardIssuance so the coupon can be looked up by
    order/user and traced back to the purchase that earned it on the
    dashboards, and so a retried Automation run for the same order doesn't
    create a second linkage.
    """
    if not _check_api_key(request):
        return JsonResponse({"error": "unauthorized"}, status=401)

    try:
        data = json.loads(request.body)
        wix_coupon_id = str(data["wix_coupon_id"])
        code_str = data["code"]
        discount_percent = int(data["discount_percent"])
    except (json.JSONDecodeError, KeyError, ValueError):
        return JsonResponse({"error": "invalid payload"}, status=400)

    product = data.get("product", "kareertrek")
    if product not in dict(PRODUCT_CHOICES):
        return JsonResponse({"error": "invalid product"}, status=400)

    order_id = str(data["order_id"]) if data.get("order_id") else ""
    user_id = str(data["user_id"]) if data.get("user_id") else ""

    # Purchase-reward idempotency: if this order already has a reward coupon
    # on record, return it as-is instead of touching anything (covers the
    # Velo automation firing more than once for the same "Plan ordered" event).
    if order_id:
        existing_issuance = PurchaseRewardIssuance.objects.filter(
            source_system="wix", external_order_id=order_id
        ).select_related("referral_code").first()
        if existing_issuance:
            code = existing_issuance.referral_code
            return JsonResponse({"status": "already issued", "code": code.code, "coupon_id": code.id})

    system_user = _wix_system_user()
    existing = ReferralCode.objects.filter(wix_coupon_id=wix_coupon_id).first()

    if existing is None and ReferralCode.objects.filter(code=code_str).exists():
        # Code string collision against something already in the Hub —
        # reject rather than silently overwrite an unrelated code.
        return JsonResponse({"error": "code already exists in Referral Hub"}, status=409)

    with transaction.atomic():
        if existing:
            code = existing
            created = False
        else:
            now = timezone.now()
            code = ReferralCode(
                code=code_str,
                code_type="customer",
                requested_by=system_user,
                owner_name=data.get("owner_name", ""),
                owner_email=data.get("owner_email", ""),
                product=product,
                approval_status="approved",
                approved_by=system_user,
                approved_at=now,
                expires_at=now + timedelta(days=365 * DEFAULT_CODE_VALIDITY_YEARS),
                origin_system="wix",
            )
            created = True

        code.discount_percent = discount_percent
        code.wix_coupon_id = wix_coupon_id
        code.wix_sync_status = "synced"
        code.wix_sync_error = ""
        code.wix_last_synced_at = timezone.now()
        code.save()

        if order_id and user_id and not PurchaseRewardIssuance.objects.filter(
            source_system="wix", external_order_id=order_id
        ).exists():
            PurchaseRewardIssuance.objects.create(
                source_system="wix",
                external_user_id=user_id,
                external_order_id=order_id,
                external_email=data.get("owner_email", ""),
                referral_code=code,
            )

        _log(
            "wix_coupon_synced_in" if created else "wix_coupon_updated_in",
            code, system_user,
            f"Coupon {mask_code(code.code)} {'created' if created else 'updated'} from Wix (coupon id {wix_coupon_id})"
            + (f", purchase reward for order {order_id}" if order_id else ""),
        )

    return JsonResponse({"status": "created" if created else "updated", "code": code.code, "coupon_id": code.id})


@csrf_exempt
@require_http_methods(["POST"])
def wix_coupon_used_api(request):
    """
    POST /careertrek/coupon-used
    Called after a KareerTrek purchase succeeds with a Referral Hub-managed
    coupon applied. Records the usage directly as a converted Referral —
    Wix/KareerTrek is the source of truth that the purchase went through,
    so this doesn't route through the pending apply_referral_code flow.

    Idempotent on (code, order_id): a retried delivery is a no-op.
    """
    if not _check_api_key(request):
        return JsonResponse({"error": "unauthorized"}, status=401)

    try:
        data = json.loads(request.body)
        code_str = data["code"]
    except (json.JSONDecodeError, KeyError):
        return JsonResponse({"error": "invalid payload"}, status=400)

    try:
        ref_code = ReferralCode.objects.get(code=code_str)
    except ReferralCode.DoesNotExist:
        return JsonResponse({"error": "unknown code"}, status=404)

    external_order_id = data.get("order_id", "")
    customer_email = data.get("customer_email", "")
    customer_name = data.get("customer_name", "")
    product = data.get("product", ref_code.product)

    try:
        with transaction.atomic():
            referral = Referral.objects.create(
                referral_code=ref_code,
                customer_name=customer_name,
                customer_email=customer_email,
                product=product,
                discount_applied=ref_code.discount_percent,
                status="converted",
                external_order_id=external_order_id,
            )
            _log("redeemed", ref_code, None,
                 f"Redeemed on KareerTrek by {mask_email(customer_email) if customer_email else 'unknown'} (order {external_order_id or 'n/a'})")
    except IntegrityError:
        # Same order_id delivered twice — already recorded, treat as success.
        return JsonResponse({"status": "already recorded"})

    return JsonResponse({"status": "recorded", "referral_id": referral.id})