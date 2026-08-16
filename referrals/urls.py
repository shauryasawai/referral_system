from django.contrib.auth import views as auth_views
from django.urls import path
from . import views

urlpatterns = [
    path("login/", auth_views.LoginView.as_view(template_name="referrals/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(next_page="login"), name="logout"),
    path("dashboard/", views.user_dashboard, name="user_dashboard"),
    path("dashboard/approve/<int:code_id>/", views.approve_code, name="approve_code"),
    path("dashboard/reject/<int:code_id>/", views.reject_code, name="reject_code"),
    path("dashboard/edit/<int:code_id>/", views.edit_code, name="edit_code"),
    path("dashboard/toggle/<int:code_id>/", views.toggle_active, name="toggle_active"),
    path("dashboard/delete/<int:code_id>/", views.delete_code, name="delete_code"),
    path("change-password/", views.change_password, name="change_password"),
    path("ops-dashboard/", views.ops_dashboard, name="ops_dashboard"),
    path("apply-code/", views.apply_code_page, name="apply_code_page"),
    path("apply-code/submit/", views.apply_referral_code, name="apply_referral_code"),
]
