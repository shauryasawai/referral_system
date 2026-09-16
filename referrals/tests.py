# ---------------------------------------------------------------------------
# CareerTrek / Wix integration tests
# ---------------------------------------------------------------------------
import json
from django.test import override_settings
from django.test import TestCase
from django.contrib.auth.models import User
from referrals.models import ReferralCode, Referral, AuditLog
from refsystem import settings

TEST_API_KEY = settings.INTERNAL_API_KEY

@override_settings(INTERNAL_API_KEY=TEST_API_KEY)
class WixCouponCreatedApiTests(TestCase):
    url = "/careertrek/coupon-created"

    def _post(self, payload, key=TEST_API_KEY):
        return self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_X_INTERNAL_API_KEY=key,
        )

    def test_rejects_bad_api_key(self):
        resp = self._post({"wix_coupon_id": "c1", "code": "X", "discount_percent": 10}, key="wrong")
        self.assertEqual(resp.status_code, 401)

    def test_creates_new_code(self):
        resp = self._post({
            "wix_coupon_id": "wix-c1",
            "code": "WIXTEST-001",
            "discount_percent": 15,
            "product": "careertrek",
            "owner_email": "buyer@example.com",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "created")

        code = ReferralCode.objects.get(code="WIXTEST-001")
        self.assertEqual(code.origin_system, "wix")
        self.assertEqual(code.approval_status, "approved")
        self.assertTrue(code.active)
        self.assertEqual(code.wix_sync_status, "synced")
        self.assertEqual(code.wix_coupon_id, "wix-c1")
        self.assertTrue(
            AuditLog.objects.filter(referral_code=code, action="wix_coupon_synced_in").exists()
        )

    def test_replay_updates_not_duplicates(self):
        payload = {"wix_coupon_id": "wix-c2", "code": "WIXTEST-002", "discount_percent": 10}
        self._post(payload)
        payload["discount_percent"] = 20
        resp = self._post(payload)

        self.assertEqual(resp.json()["status"], "updated")
        self.assertEqual(ReferralCode.objects.filter(wix_coupon_id="wix-c2").count(), 1)
        self.assertEqual(ReferralCode.objects.get(wix_coupon_id="wix-c2").discount_percent, 20)

    def test_rejects_conflicting_code_string(self):
        user = User.objects.create_user(username="conflict_test_user")
        ReferralCode.objects.create(code="TAKEN-001", code_type="customer", requested_by=user, product="careertrek")
        resp = self._post({"wix_coupon_id": "wix-c3", "code": "TAKEN-001", "discount_percent": 10})
        self.assertEqual(resp.status_code, 409)

    def test_missing_fields_400(self):
        resp = self._post({"wix_coupon_id": "wix-c4"})
        self.assertEqual(resp.status_code, 400)

    def test_invalid_product_400(self):
        resp = self._post({"wix_coupon_id": "wix-c5", "code": "X", "discount_percent": 10, "product": "nope"})
        self.assertEqual(resp.status_code, 400)


@override_settings(INTERNAL_API_KEY=TEST_API_KEY)
class WixCouponUsedApiTests(TestCase):
    url = "/careertrek/coupon-used"

    def setUp(self):
        self.user = User.objects.create_user(username="usage_test_user")
        self.code = ReferralCode.objects.create(
            code="USEME-001", code_type="customer", requested_by=self.user,
            product="careertrek", discount_percent=10,
            approval_status="approved", active=True,
        )

    def _post(self, payload, key=TEST_API_KEY):
        return self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_X_INTERNAL_API_KEY=key,
        )

    def test_rejects_bad_api_key(self):
        resp = self._post({"code": "USEME-001"}, key="wrong")
        self.assertEqual(resp.status_code, 401)

    def test_unknown_code_404(self):
        resp = self._post({"code": "NOPE-001", "order_id": "o1"})
        self.assertEqual(resp.status_code, 404)

    def test_records_usage(self):
        resp = self._post({
            "code": "USEME-001", "order_id": "order-1",
            "customer_email": "buyer@example.com", "customer_name": "Buyer",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "recorded")

        referral = Referral.objects.get(referral_code=self.code)
        self.assertEqual(referral.status, "converted")
        self.assertEqual(referral.external_order_id, "order-1")
        self.assertTrue(AuditLog.objects.filter(referral_code=self.code, action="redeemed").exists())

    def test_duplicate_order_id_is_idempotent(self):
        payload = {"code": "USEME-001", "order_id": "order-2", "customer_email": "a@example.com"}
        self._post(payload)
        resp = self._post(payload)

        self.assertEqual(resp.json()["status"], "already recorded")
        self.assertEqual(Referral.objects.filter(referral_code=self.code, external_order_id="order-2").count(), 1)

    def test_different_order_ids_both_recorded(self):
        self._post({"code": "USEME-001", "order_id": "order-3"})
        self._post({"code": "USEME-001", "order_id": "order-4"})
        self.assertEqual(Referral.objects.filter(referral_code=self.code).count(), 2)