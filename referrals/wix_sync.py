"""
Sync layer between our ReferralCode model and Wix's native Coupons API.

Only CareerTrek codes are synced (that's the only product actually hosted on
Wix Studio). Every other product is a no-op here.

Docs: https://dev.wix.com/docs/api-reference/business-solutions/coupons/coupons/introduction
Create: https://dev.wix.com/docs/api-reference/business-solutions/coupons/coupons/create-a-coupon
Scope values: https://dev.wix.com/docs/api-reference/business-solutions/coupons/coupons/valid-scope-values

The correct base endpoint is https://www.wixapis.com/stores/v2/coupons

{
  "specification": {
    "name": "111",
    "code": "ABC",
    "active": true,
    "startTime": 1554126716133,
    "scope": { "namespace": "stores" },
    "percentOffRate": 5
  }
}

The create response is flat: {"id": "<coupon-guid>"} — no wrapper key.

"""

import logging

import requests
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

WIX_API_BASE = "https://www.wixapis.com"
COUPONS_ENDPOINT = f"{WIX_API_BASE}/stores/v2/coupons"

# Only these products are actually hosted on Wix Studio today.
WIX_SYNCED_PRODUCTS = {"careertrek"}

# Which Wix business solution CareerTrek's checkout actually uses. Confirmed
# via a real checkout test: CareerTrek sells via Wix PRICING PLANS
# (subscriptions), not Wix Stores products — a "stores"-scoped coupon is
# rejected there with "Not applicable to plans". Valid namespace values per
# https://dev.wix.com/docs/api-reference/business-solutions/coupons/coupons/valid-scope-values
# are: stores, bookings, events, pricingPlans, restaurants.
WIX_COUPON_NAMESPACE = "pricingPlans"


def _headers():
    return {
        "Authorization": settings.WIX_API_KEY,
        "wix-site-id": settings.WIX_SITE_ID,
        "Content-Type": "application/json",
    }


def _credentials_configured():
    return bool(getattr(settings, "WIX_API_KEY", "") and getattr(settings, "WIX_SITE_ID", ""))


def should_sync(referral_code):
    """Only CareerTrek codes touch Wix at all."""
    return referral_code.product in WIX_SYNCED_PRODUCTS


def create_coupon(referral_code):
    """
    Called when an admin approves a code. Creates a live, active coupon in
    Wix Stores mirroring our code + discount percent.

    Returns (success, wix_coupon_id, error_message).
    """
    if not should_sync(referral_code):
        return True, None, None  # not a Wix product, nothing to do — not a failure

    if not _credentials_configured():
        msg = "Wix API credentials not configured (WIX_API_KEY / WIX_SITE_ID)."
        logger.error(msg)
        return False, None, msg

    # Wix requires startTime as milliseconds-since-epoch (a raw number, not a
    # string), plus name, code, and either scope or minimumSubtotal alongside
    # exactly one discount type field. Everything lives directly inside
    # "specification" — there is NO top-level "coupon" wrapper on create.
    # We scope to "all products in Wix Stores" (namespace only, no
    # group/entityId) so the coupon applies store-wide on CareerTrek.
    payload = {
        "specification": {
            "name": f"{referral_code.get_code_type_display()} referral - {referral_code.code}",
            "code": referral_code.code,
            "active": True,
            "startTime": int(timezone.now().timestamp() * 1000),
            "scope": {"namespace": WIX_COUPON_NAMESPACE},
            # percentOffRate is 0-100, not a 0-1 fraction.
            "percentOffRate": referral_code.discount_percent,
            # CareerTrek's plans are recurring subscriptions. Wix requires
            # appliesToSubscriptions=true whenever discountedCycleCount is
            # set (confirmed directly from the API's own validation error:
            # "discountedCycleCount can only be set when appliesToSubscriptions
            # is set to true") — the two fields must be set together, not
            # separately as earlier doc text suggested.
            "appliesToSubscriptions": True,
            # Referral discount is intentionally one-time: applies to the
            # customer's first billing cycle only, not every renewal.
            "discountedCycleCount": 1,
        }
    }

    try:
        resp = requests.post(COUPONS_ENDPOINT, json=payload, headers=_headers(), timeout=10)
    except requests.RequestException as exc:
        msg = f"Network error calling Wix Coupons API: {exc}"
        logger.exception(msg)
        return False, None, msg

    if resp.status_code not in (200, 201):
        msg = f"Wix Coupons API returned {resp.status_code}: {resp.text[:500]}"
        logger.error(msg)
        return False, None, msg

    try:
        wix_coupon_id = resp.json()["id"]
    except (KeyError, ValueError) as exc:
        msg = f"Unexpected Wix response shape: {exc}; body: {resp.text[:500]}"
        logger.error(msg)
        return False, None, msg

    return True, wix_coupon_id, None


def disable_coupon(referral_code):
    """
    Called when an admin/owner deactivates a code. Disables (does not delete)
    the matching Wix coupon so redemption history is preserved on Wix's side too.

    Returns (success, error_message).
    """
    if not should_sync(referral_code):
        return True, None

    if not referral_code.wix_coupon_id:
        # Nothing to disable — either it never synced, or it predates this feature.
        msg = "No wix_coupon_id on record; nothing to disable in Wix."
        logger.warning(msg)
        return False, msg

    if not _credentials_configured():
        msg = "Wix API credentials not configured (WIX_API_KEY / WIX_SITE_ID)."
        logger.error(msg)
        return False, msg

    # Same "specification" top-level shape as create; update additionally
    # requires "fieldMask" to say which paths are being changed.
    url = f"{COUPONS_ENDPOINT}/{referral_code.wix_coupon_id}"
    payload = {
        "specification": {"active": False},
        "fieldMask": {"paths": ["active"]},
    }

    try:
        resp = requests.patch(url, json=payload, headers=_headers(), timeout=10)
    except requests.RequestException as exc:
        msg = f"Network error calling Wix Coupons API: {exc}"
        logger.exception(msg)
        return False, msg

    if resp.status_code not in (200,):
        msg = f"Wix Coupons API returned {resp.status_code}: {resp.text[:500]}"
        logger.error(msg)
        return False, msg

    return True, None