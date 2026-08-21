from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from src.amazon_seller_shadow_request import (
    AmazonSellerShadowRequestError,
    build_amazon_seller_shadow_request,
    read_private_amazon_seller_shadow_request,
    validate_amazon_seller_shadow_request,
    verify_private_amazon_seller_shadow_request,
)
from src.box_runtime import BoxRuntime


ROOT = Path(__file__).resolve().parents[1]
BOX = ROOT / "examples" / "boxes" / "us_marketplace_amazon_seller_c_corp.json"
MARKETPLACE = "ATVPDKIKX0DER"


class AmazonSellerShadowRequestTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(BOX, ROOT / "packs")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def _request_path(self, *, period="2026-07") -> Path:
        path = Path(self.temp.name) / f"private-amazon-{period}.json"
        summary = build_amazon_seller_shadow_request(
            self.runtime,
            entity_id="us_amazon_marketplace_company",
            period=period,
            marketplace_id=MARKETPLACE,
            output=path,
        )
        self.assertFalse(summary["template_only"])
        self.assertTrue(summary["request_contract_complete"])
        self.assertTrue(summary["ready_for_network_dispatch"])
        self.assertEqual(summary["operator_edits_required"], [])
        self.assertFalse(summary["marketplace_value_returned"])
        return path

    def test_init_is_complete_private_production_and_exact_closed_month_bound(self):
        path = self._request_path()
        request = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(request, {
            "pipeline_id": "amazon_seller.marketplace_close",
            "payload": {
                "entity_id": "us_amazon_marketplace_company",
                "period": "2026-07",
                "amazon_seller_marketplace_request": {
                    "mode": "fetch",
                    "default_entity_id": "us_amazon_marketplace_company",
                    "environment": "production",
                    "marketplace_id": MARKETPLACE,
                    "interval_start": "2026-07-01T00:00:00Z",
                    "interval_end": "2026-08-01T00:00:00Z",
                    "orders_time_basis": "created",
                    "max_order_pages": 20,
                    "max_inventory_pages": 20,
                    "max_transaction_pages": 20,
                },
            },
        })
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        verified = verify_private_amazon_seller_shadow_request(self.runtime, path)
        self.assertTrue(verified["valid"])
        self.assertEqual(verified["period"], "2026-07")
        self.assertTrue(verified["marketplace_scope_declared"])
        self.assertTrue(verified["bounded_three_source_pagination"])
        self.assertFalse(verified["marketplace_value_returned"])
        self.assertFalse(verified["network_access_performed"])
        self.assertEqual(validate_amazon_seller_shadow_request(self.runtime, request), verified)

    def test_december_rollover_and_current_open_month_boundary(self):
        request = json.loads(self._request_path(period="2025-12").read_text())
        connector = request["payload"]["amazon_seller_marketplace_request"]
        self.assertEqual(connector["interval_start"], "2025-12-01T00:00:00Z")
        self.assertEqual(connector["interval_end"], "2026-01-01T00:00:00Z")
        with self.assertRaisesRegex(AmazonSellerShadowRequestError, "completed calendar month"):
            build_amazon_seller_shadow_request(
                self.runtime,
                entity_id="us_amazon_marketplace_company",
                period="2026-08",
                marketplace_id=MARKETPLACE,
                output=Path(self.temp.name) / "open-month.json",
            )

    def test_fixture_updated_scope_pagination_marketplace_and_secret_fail_closed(self):
        request = json.loads(self._request_path().read_text(encoding="utf-8"))
        connector = request["payload"]["amazon_seller_marketplace_request"]
        connector["mode"] = "fixture"
        with self.assertRaisesRegex(AmazonSellerShadowRequestError, "production fetch"):
            validate_amazon_seller_shadow_request(self.runtime, request)
        connector["mode"] = "fetch"
        connector["orders_time_basis"] = "updated"
        with self.assertRaisesRegex(AmazonSellerShadowRequestError, "created-order scope"):
            validate_amazon_seller_shadow_request(self.runtime, request)
        connector["orders_time_basis"] = "created"
        connector["max_inventory_pages"] = 19
        with self.assertRaisesRegex(AmazonSellerShadowRequestError, "bounded three-source"):
            validate_amazon_seller_shadow_request(self.runtime, request)
        connector["max_inventory_pages"] = 20
        connector["marketplace_id"] = "bad"
        with self.assertRaisesRegex(AmazonSellerShadowRequestError, "marketplace_id"):
            validate_amazon_seller_shadow_request(self.runtime, request)
        connector["marketplace_id"] = MARKETPLACE
        connector["seller_id"] = "PRIVATE-SELLER"
        with self.assertRaisesRegex(AmazonSellerShadowRequestError, "credentials"):
            validate_amazon_seller_shadow_request(self.runtime, request)

    def test_cross_entity_non_private_and_overwrite_fail_closed(self):
        path = self._request_path()
        request = json.loads(path.read_text(encoding="utf-8"))
        request["payload"]["entity_id"] = "other"
        request["payload"]["amazon_seller_marketplace_request"][
            "default_entity_id"
        ] = "other"
        with self.assertRaisesRegex(AmazonSellerShadowRequestError, "Unknown legal entity"):
            validate_amazon_seller_shadow_request(self.runtime, request)
        with self.assertRaisesRegex(AmazonSellerShadowRequestError, "already exists"):
            build_amazon_seller_shadow_request(
                self.runtime,
                entity_id="us_amazon_marketplace_company",
                period="2026-07",
                marketplace_id=MARKETPLACE,
                output=path,
            )
        if os.name != "nt":
            path.chmod(0o644)
            with self.assertRaisesRegex(AmazonSellerShadowRequestError, "0600"):
                read_private_amazon_seller_shadow_request(path)


if __name__ == "__main__":
    unittest.main()
