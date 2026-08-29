from django import forms
from django.contrib.auth.models import User

from .models import PRODUCT_CHOICES, ReferralCode

DESIGNATION_CHOICES = [
    ("channel_partner", "Channel Partner"),
    ("customer", "End Customer"),
    ("ops", "Operations Team"),
    ("admin", "Admin"),
]


class RequestCodeForm(forms.Form):
    """Product choices are scoped per-request to whatever the logged-in user is allowed to generate."""
    product = forms.ChoiceField(choices=[])

    def __init__(self, *args, allowed_products=None, **kwargs):
        super().__init__(*args, **kwargs)
        allowed_products = allowed_products if allowed_products is not None else [c[0] for c in PRODUCT_CHOICES]
        choices = [("", "Select product")] + [c for c in PRODUCT_CHOICES if c[0] in allowed_products]
        self.fields["product"].choices = choices


class EditCodeForm(forms.Form):
    code = forms.CharField(max_length=30)
    discount_percent = forms.IntegerField(min_value=0, max_value=100)

    def __init__(self, *args, instance=None, allow_code_edit=False, **kwargs):
        self.instance = instance
        self.allow_code_edit = allow_code_edit
        super().__init__(*args, **kwargs)
        if not allow_code_edit:
            self.fields["code"].disabled = True

    def clean_code(self):
        value = self.cleaned_data["code"].strip().upper()
        if not self.allow_code_edit:
            return self.instance.code
        qs = ReferralCode.objects.filter(code=value)
        if self.instance:
            qs = qs.exclude(id=self.instance.id)
        if qs.exists():
            raise forms.ValidationError("That code already exists, choose another.")
        return value


class ApplyCodeForm(forms.Form):
    name = forms.CharField(max_length=150)
    email = forms.EmailField()
    product = forms.ChoiceField(choices=[("", "Select product")] + PRODUCT_CHOICES)
    code = forms.CharField(max_length=30)

    def clean_code(self):
        return self.cleaned_data["code"].strip().upper()


class AddUserForm(forms.Form):
    """Used by admins on the Manage Users page to create an account, set its designation,
    and — for Channel Partner / End Customer accounts — scope which products it can
    generate referral codes for."""
    username = forms.CharField(max_length=150)
    email = forms.EmailField(required=False)
    password = forms.CharField(widget=forms.PasswordInput, min_length=8)
    designation = forms.ChoiceField(choices=DESIGNATION_CHOICES)
    allowed_products = forms.MultipleChoiceField(
        choices=PRODUCT_CHOICES, required=False, widget=forms.CheckboxSelectMultiple
    )

    def clean_username(self):
        value = self.cleaned_data["username"].strip()
        if User.objects.filter(username=value).exists():
            raise forms.ValidationError("That username is already taken.")
        return value

    def clean(self):
        cleaned = super().clean()
        designation = cleaned.get("designation")
        if designation in ("channel_partner", "customer") and not cleaned.get("allowed_products"):
            self.add_error("allowed_products", "Select at least one product this user can generate codes for.")
        return cleaned
