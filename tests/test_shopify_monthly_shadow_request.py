from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from src.box_runtime import BoxRuntime
from src.shopify_monthly_shadow_request import (
    ShopifyMonthlyShadowRequestError,
    build_shopify_monthly_shadow_request_template,
    verify_private_shopify_monthly_shadow_request,
)


ROOT = Path(__file__).resolve().parents[1]
BOX = ROOT / "examples" / "boxes" / "cn_dtc_shopify_stripe_store.json"


class ShopifyMonthlyShadowRequestTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(BOX, ROOT / "packs")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def _template(self, name: str = "request.json") -> Path:
        path = Path(self.temp.name) / name
        result = build_shopify_monthly_shadow_request_template(
            self.runtime,
            entity_id="cn_dtc_company",
            period="2026-07",
            output=path,
        )
        self.assertTrue(result["template_only"])
        self.assertFalse(result["ready_for_network_dispatch"])
        return path

    def _complete(self, path: Path) -> dict:
        request = json.loads(path.read_text(encoding="utf-8"))
        payload = request["payload"]
        payload["currency_minor_units"] = {"USD": 2}
        payload["shopify_monthly_request"]["shop_domain"] = (
            "private-opc-store.myshopify.com"
        )
        payload["processor_links"] = [{
            "entity_id": "cn_dtc_company",
            "shopify_transaction_id": "gid://shopify/OrderTransaction/7201",
            "stripe_source_object_id": "ch_month_7001",
            "evidence": {
                "source_file": "private-export://processor-links/2026-07",
                "batch_id": "approved-links-2026-07",
            },
        }]
        path.write_text(json.dumps(request), encoding="utf-8")
        if os.name != "nt":
            path.chmod(0o600)
        return request

    def test_init_generates_exact_private_month_bounds_without_credentials(self):
        path = self._template()
        request = json.loads(path.read_text(encoding="utf-8"))
        payload = request["payload"]
        self.assertEqual(
            payload["shopify_monthly_request"]["interval_start"],
            "2026-07-01T00:00:00Z",
        )
        self.assertEqual(
            payload["shopify_monthly_request"]["interval_end"],
            "2026-08-01T00:00:00Z",
        )
        self.assertEqual(payload["stripe_balance_request"]["created_gte"], 1782864000)
        self.assertEqual(payload["stripe_balance_request"]["created_lt"], 1785542400)
        self.assertEqual(payload["processor_links"], [])
        self.assertNotIn("token", path.read_text(encoding="utf-8").lower())
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        with self.assertRaisesRegex(
            ShopifyMonthlyShadowRequestError, "already exists",
        ):
            build_shopify_monthly_shadow_request_template(
                self.runtime,
                entity_id="cn_dtc_company",
                period="2026-07",
                output=path,
            )

    def test_completed_request_verifies_without_returning_store_or_raw_ids(self):
        path = self._template()
        self._complete(path)
        verified = verify_private_shopify_monthly_shadow_request(self.runtime, path)
        self.assertTrue(verified["valid"])
        self.assertEqual(verified["period"], "2026-07")
        self.assertEqual(verified["currency_count"], 1)
        self.assertEqual(verified["processor_link_count"], 1)
        self.assertTrue(verified["same_window_stripe_bounds"])
        self.assertFalse(verified["network_access_performed"])
        serialized = json.dumps(verified)
        self.assertNotIn("private-opc-store", serialized)
        self.assertNotIn("gid://", serialized)
        self.assertNotIn("ch_month_7001", serialized)

    def test_incomplete_mismatched_or_secret_bearing_request_fails_closed(self):
        incomplete = self._template("incomplete.json")
        with self.assertRaisesRegex(
            ShopifyMonthlyShadowRequestError, "shop_domain",
        ):
            verify_private_shopify_monthly_shadow_request(self.runtime, incomplete)

        mismatched = self._template("mismatched.json")
        request = self._complete(mismatched)
        request["payload"]["stripe_balance_request"]["created_lt"] -= 1
        mismatched.write_text(json.dumps(request), encoding="utf-8")
        if os.name != "nt":
            mismatched.chmod(0o600)
        with self.assertRaisesRegex(
            ShopifyMonthlyShadowRequestError, "exactly match",
        ):
            verify_private_shopify_monthly_shadow_request(self.runtime, mismatched)

        secret = self._template("secret.json")
        request = self._complete(secret)
        request["payload"]["processor_links"][0]["evidence"]["batch_id"] = (
            "sk_live_prohibited"
        )
        secret.write_text(json.dumps(request), encoding="utf-8")
        if os.name != "nt":
            secret.chmod(0o600)
        with self.assertRaisesRegex(
            ShopifyMonthlyShadowRequestError, "credentials",
        ):
            verify_private_shopify_monthly_shadow_request(self.runtime, secret)

    def test_entity_connector_binding_and_private_mode_are_enforced(self):
        multi = BoxRuntime(
            ROOT / "examples" / "boxes" / "cn_nl_us_dtc_entity_connectors.json",
            ROOT / "packs",
        )
        with self.assertRaisesRegex(
            ShopifyMonthlyShadowRequestError, "not bound",
        ):
            build_shopify_monthly_shadow_request_template(
                multi,
                entity_id="cn_ops",
                period="2026-07",
                output=Path(self.temp.name) / "wrong-entity.json",
            )

        path = self._template("mode.json")
        self._complete(path)
        if os.name != "nt":
            path.chmod(0o644)
            with self.assertRaisesRegex(
                ShopifyMonthlyShadowRequestError, "mode 0600",
            ):
                verify_private_shopify_monthly_shadow_request(self.runtime, path)


if __name__ == "__main__":
    unittest.main()
