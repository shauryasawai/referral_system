from django.contrib.auth import views as auth_views
from django.urls import path
from . import views

urlpatterns = [
    path("", auth_views.LoginView.as_view(template_name="referrals/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(next_page="login"), name="logout"),

    path("dashboard/", views.user_dashboard, name="user_dashboard"),
    path("dashboard/approve/<int:code_id>/", views.approve_code, name="approve_code"),
    path("dashboard/reject/<int:code_id>/", views.reject_code, name="reject_code"),
    path("dashboard/edit/<int:code_id>/", views.edit_code, name="edit_code"),
    path("dashboard/deactivate/<int:code_id>/", views.deactivate_code, name="deactivate_code"),
    path("dashboard/delete/<int:code_id>/", views.delete_code, name="delete_code"),

    path("manage-users/", views.manage_users, name="manage_users"),
    path("audit-log/", views.audit_log, name="audit_log"),
    path("change-password/", views.change_password, name="change_password"),
    path("ops-dashboard/", views.ops_dashboard, name="ops_dashboard"),

    path("apply-code/", views.apply_code_page, name="apply_code_page"),
    path("apply-code/submit/", views.apply_referral_code, name="apply_referral_code"),

    path("partner-requests/<int:request_id>/approve/", views.approve_partner_request, name="approve_partner_request"),
    path("partner-requests/<int:request_id>/reject/", views.reject_partner_request, name="reject_partner_request"),
    path("partner-requests/<int:request_id>/retry-delivery/", views.retry_partner_delivery, name="retry_partner_delivery"),

    path("api/partner-requests/", views.create_partner_request, name="api_create_partner_request"),

    # ─────────────────────────────────────────────────────────────────
    # Lead Gen Tool integration — server-to-server API, called by
    # leads/referral_hub_client.py. Auth via X-Internal-Api-Key, not
    # session auth, so these sit outside the login-required views above.
    # ─────────────────────────────────────────────────────────────────
    path("referral/starter-coupon", views.request_starter_coupon_api, name="api_starter_coupon"),
    path("referral/my-coupon", views.my_coupon_api, name="api_my_coupon"),
    path("referral/my-usage", views.my_usage_api, name="api_my_usage"),
    path("referral/partner-request", views.partner_request_api, name="api_partner_request"),
    path("referral/partner-request/status", views.partner_request_status_api, name="api_partner_request_status"),

    # ─────────────────────────────────────────────────────────────────
    # KareerTrek / Wix integration — server-to-server API. Coupons are now
    # always minted on the Wix side (see the "Plan ordered" Velo automation)
    # and mirrored in here; Referral Hub no longer mints reward coupons
    # itself, so the old kareertrek/purchase-reward-coupon endpoint has
    # been removed.
    # ─────────────────────────────────────────────────────────────────
    path("kareertrek/coupon-created", views.wix_coupon_created_api, name="api_wix_coupon_created"),
    path("kareertrek/coupon-used", views.wix_coupon_used_api, name="api_wix_coupon_used"),
]