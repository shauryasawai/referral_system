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
from django.views.decorators.csrf import csrf_exempt
from django.http import JsonResponse
from django.conf import settings
import json
from . import wix_sync
from .forms import AddUserForm, ApplyCodeForm, EditCodeForm, RequestCodeForm
from .models import (
    AuditLog, DEFAULT_CUSTOMER_DISCOUNT, PRODUCT_CHOICES,
    PartnerOnboardingRequest, Referral, ReferralCode, UserAccess,
    StarterCouponIssuance,
)


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


def _check_api_key(request):
    return request.headers.get("X-Internal-Api-Key") == settings.INTERNAL_API_KEY


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
    ).first()

    if existing and existing.status in ("pending", "approved"):
        return JsonResponse({"status": existing.status, "request_id": existing.id})

    req, _ = PartnerOnboardingRequest.objects.update_or_create(
        source_system="leadgen",
        external_user_id=external_user_id,
        defaults={
            "external_email": data["email"],
            "external_name": data.get("name", ""),
            "application_data": data.get("application_data", {}),  # NEW
            "status": "pending",
            "product": "",
            "referral_code": None,
            "callback_delivered": False,
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
        code = ReferralCode.objects.create(
            code_type="partner",
            requested_by=request.user,
            owner_name=req.external_name,
            owner_email=req.external_email,
            product=product,
            discount_percent=discount_percent,
            approval_status="approved",
            approved_by=request.user,
            approved_at=timezone.now(),
        )
        req.status = "approved"
        req.product = product
        req.referral_code = code
        req.save(update_fields=["status", "product", "referral_code"])
        _log("approved", code, request.user,
             f"Partner onboarding for {req.external_email} ({discount_percent}% off {code.get_product_display()})")

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
        _log("rejected", None, request.user, f"Partner onboarding request rejected for {req.external_email}")
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
    try:
        resp = requests.post(
            f"{settings.LEADGEN_BASE_URL}/api/partner-code-assigned/",
            json={
                "user_id": req.external_user_id,
                "code": req.referral_code.code,
                "discount_percent": req.referral_code.discount_percent,
                "product": req.product,
            },
            headers={"X-Internal-Api-Key": settings.INTERNAL_API_KEY},
            timeout=5,
        )
        print("LEADGEN DELIVERY:", resp.status_code, resp.text)  # TEMP DEBUG
        req.callback_delivered = resp.ok
        req.save(update_fields=["callback_delivered"])
    except requests.RequestException as e:
        print("LEADGEN DELIVERY FAILED:", repr(e))  # TEMP DEBUG
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
    product = data.get("product", "careertrek")  # default product for starter coupons

    if product not in dict(PRODUCT_CHOICES):
        return JsonResponse({"error": "invalid product"}, status=400)

    # Idempotent: if this Lead Gen user already has a starter coupon, return it
    # instead of minting a second one.
    existing = StarterCouponIssuance.objects.filter(
        source_system="leadgen", external_user_id=user_id
    ).select_related("referral_code").first()
    if existing:
        code = existing.referral_code
        return JsonResponse({
            "owner_id": user_id,
            "coupon_id": code.id,
            "code": code.code,
            "discount_percent": code.discount_percent,
            "applicable_products": [code.product],
            "valid_until": None,
        })

    system_user = _leadgen_system_user()

    with transaction.atomic():
        code = ReferralCode.objects.create(
            code_type="customer",
            requested_by=system_user,
            owner_name=name,
            owner_email=email,
            product=product,
            discount_percent=DEFAULT_CUSTOMER_DISCOUNT,
            approval_status="approved",
            approved_by=system_user,
            approved_at=timezone.now(),
        )
        StarterCouponIssuance.objects.create(
            source_system="leadgen",
            external_user_id=user_id,
            external_email=email,
            referral_code=code,
        )
        _log("approved", code, system_user, f"Starter coupon auto-issued for Lead Gen user {user_id} ({email})")

    _sync_approved_code_to_wix(code, system_user)

    return JsonResponse({
        "owner_id": user_id,
        "coupon_id": code.id,
        "code": code.code,
        "discount_percent": code.discount_percent,
        "applicable_products": [code.product],
        "valid_until": None,
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
    return JsonResponse({
        "owner_id": user_id,
        "coupon_id": code.id,
        "code": code.code,
        "discount_percent": code.discount_percent,
        "applicable_products": [code.product],
        "valid_until": None,
        "status": "active" if code.is_live else "expired",
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
    """GET /referral/partner-request/status?user_id=..."""
    if not _check_api_key(request):
        return JsonResponse({"error": "unauthorized"}, status=401)

    user_id = request.GET.get("user_id", "")
    if not user_id:
        return JsonResponse({"error": "user_id required"}, status=400)

    req = PartnerOnboardingRequest.objects.filter(
        source_system="leadgen", external_user_id=user_id
    ).order_by("-created_at").first()

    if not req:
        return JsonResponse({"error": "not found"}, status=404)

    return JsonResponse({"status": req.status})

def _deliver_partner_deactivation_to_leadgen(req):
    """Symmetric to _deliver_code_to_leadgen, but for the deactivation event."""
    import requests
    try:
        resp = requests.post(
            f"{settings.LEADGEN_BASE_URL}/api/partner-code-deactivated/",
            json={"user_id": req.external_user_id, "code": req.referral_code.code},
            headers={"X-Internal-Api-Key": settings.INTERNAL_API_KEY},
            timeout=5,
        )
        req.deactivation_delivered = resp.ok
        req.save(update_fields=["deactivation_delivered"])
    except requests.RequestException:
        req.deactivation_delivered = False
        req.save(update_fields=["deactivation_delivered"])


def _deliver_coupon_deactivation_to_leadgen(issuance):
    """Same idea, for starter (customer) coupons issued via the Lead Gen signup flow."""
    import requests
    try:
        resp = requests.post(
            f"{settings.LEADGEN_BASE_URL}/api/coupon-deactivated/",
            json={"user_id": issuance.external_user_id, "code": issuance.referral_code.code},
            headers={"X-Internal-Api-Key": settings.INTERNAL_API_KEY},
            timeout=5,
        )
        issuance.deactivation_delivered = resp.ok
        issuance.save(update_fields=["deactivation_delivered"])
    except requests.RequestException:
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
            _log("wix_sync_failed", code, actor,  # reuse existing action or add a new AUDIT_ACTION_CHOICES entry, see note below
                 "Deactivation notice to Lead Gen Tool failed for partner request")
        return

    issuance = StarterCouponIssuance.objects.filter(referral_code=code).first()
    if issuance:
        _deliver_coupon_deactivation_to_leadgen(issuance)