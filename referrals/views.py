from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_http_methods
from .models import ReferralCode, Referral, PRODUCT_CHOICES, DEFAULT_CUSTOMER_DISCOUNT


def _in_group(user, group_name):
    return user.is_staff or user.groups.filter(name=group_name).exists()


@login_required
def user_dashboard(request):
    """Logged-in channel partner / customer: request a code, see status of past requests.
    Staff/superusers additionally see a pending-approval panel for ALL users' codes."""
    if request.method == "POST":
        product = request.POST.get("product")
        code_type = "partner" if _in_group(request.user, "ChannelPartner") else "customer"
        if product:
            ReferralCode.objects.create(
                code_type=code_type,
                requested_by=request.user,
                owner_name=request.user.get_full_name() or request.user.username,
                owner_email=request.user.email,
                product=product,
                discount_percent=DEFAULT_CUSTOMER_DISCOUNT,
            )
        return redirect("user_dashboard")

    my_codes = ReferralCode.objects.filter(requested_by=request.user).order_by("-created_at")
    context = {"products": PRODUCT_CHOICES, "my_codes": my_codes, "is_ops": _in_group(request.user, "Ops")}
    if request.user.is_staff:
        context["all_codes_admin"] = ReferralCode.objects.all().order_by("-created_at")
    return render(request, "referrals/dashboard.html", context)


@login_required
def approve_code(request, code_id):
    if not request.user.is_staff:
        raise PermissionDenied("Admin access only.")
    code = get_object_or_404(ReferralCode, id=code_id, approval_status="pending")
    code.approval_status = "approved"
    code.approved_by = request.user
    code.approved_at = timezone.now()
    code.save()
    messages.success(request, f"{code.code} approved and live.")
    return redirect("user_dashboard")


@login_required
def reject_code(request, code_id):
    if not request.user.is_staff:
        raise PermissionDenied("Admin access only.")
    code = get_object_or_404(ReferralCode, id=code_id, approval_status="pending")
    code.approval_status = "rejected"
    code.approved_by = request.user
    code.approved_at = timezone.now()
    code.save()
    messages.success(request, f"{code.code} rejected.")
    return redirect("user_dashboard")


@login_required
def edit_code(request, code_id):
    """Owner can edit their own code while it's still pending. Staff can edit any code anytime."""
    code = get_object_or_404(ReferralCode, id=code_id)
    is_owner = code.requested_by_id == request.user.id
    if not (request.user.is_staff or (is_owner and code.approval_status == "pending")):
        raise PermissionDenied("You cannot edit this code.")

    if request.method == "POST":
        discount = request.POST.get("discount_percent", "").strip()
        if discount.isdigit():
            code.discount_percent = int(discount)
        if request.user.is_staff:
            new_code = request.POST.get("code", "").strip().upper()
            if new_code and new_code != code.code:
                if ReferralCode.objects.filter(code=new_code).exclude(id=code.id).exists():
                    messages.error(request, "That code already exists, choose another.")
                    return render(request, "referrals/edit_code.html", {"code": code})
                code.code = new_code
        code.save()
        messages.success(request, "Code updated.")
        return redirect("user_dashboard")

    return render(request, "referrals/edit_code.html", {"code": code, "is_ops": _in_group(request.user, "Ops")})


@login_required
def toggle_active(request, code_id):
    """Deactivate/reactivate a live code. Owner (their own) or staff (any) can do this."""
    code = get_object_or_404(ReferralCode, id=code_id, approval_status="approved")
    if not (request.user.is_staff or code.requested_by_id == request.user.id):
        raise PermissionDenied("You cannot modify this code.")
    code.active = not code.active
    code.save()
    messages.success(request, f"{code.code} {'reactivated' if code.active else 'deactivated'}.")
    return redirect("user_dashboard")


@login_required
@require_http_methods(["POST"])
def delete_code(request, code_id):
    """Permanently remove a code and its history. Admin only, irreversible."""
    if not request.user.is_staff:
        raise PermissionDenied("Admin access only.")
    code = get_object_or_404(ReferralCode, id=code_id)
    code_str = code.code
    code.delete()
    messages.success(request, f"{code_str} permanently deleted.")
    return redirect("user_dashboard")


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
    return render(request, "referrals/change_password.html", {"form": form})


@login_required
def ops_dashboard(request):
    """Ops team: read-only view of approved AND active (currently usable) codes."""
    if not _in_group(request.user, "Ops"):
        raise PermissionDenied("Ops access only.")
    live_codes = ReferralCode.objects.filter(approval_status="approved").order_by("-active", "-approved_at")
    return render(request, "referrals/ops_dashboard.html", {"live_codes": live_codes, "is_ops": True})


def apply_code_page(request):
    return render(request, "referrals/apply_code.html", {"products": PRODUCT_CHOICES})


@require_http_methods(["POST"])
def apply_referral_code(request):
    code = request.POST.get("code", "").strip().upper()
    name = request.POST.get("name", "").strip()
    email = request.POST.get("email", "").strip()
    product = request.POST.get("product")
    try:
        ref = ReferralCode.objects.get(code=code, approval_status="approved", active=True)
    except ReferralCode.DoesNotExist:
        return JsonResponse({"error": "Invalid, unapproved or inactive referral code"}, status=404)
    if ref.product != product:
        return JsonResponse({"error": "Code not valid for selected product"}, status=400)
    Referral.objects.create(
        referral_code=ref, customer_name=name, customer_email=email,
        product=product, discount_applied=ref.discount_percent,
    )
    return JsonResponse({"discount_percent": ref.discount_percent, "message": "Referral applied successfully"})
