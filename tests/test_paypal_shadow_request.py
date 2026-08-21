from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from src.box_runtime import BoxRuntime
from src.paypal_shadow_request import (
    PayPalShadowRequestError,
    build_paypal_shadow_request,
    read_private_paypal_shadow_request,
    validate_paypal_shadow_request,
    verify_private_paypal_shadow_request,
)


ROOT = Path(__file__).resolve().parents[1]
BOX = ROOT / "examples" / "boxes" / "us_dtc_paypal_c_corp.json"


class PayPalShadowRequestTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(BOX, ROOT / "packs")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def _request_path(self, *, period="2026-08") -> Path:
        path = Path(self.temp.name) / f"private-paypal-{period}.json"
        summary = build_paypal_shadow_request(
            self.runtime,
            entity_id="us_dtc_company",
            period=period,
            output=path,
        )
        self.assertFalse(summary["template_only"])
        self.assertTrue(summary["request_contract_complete"])
        self.assertTrue(summary["ready_for_network_dispatch"])
        self.assertEqual(summary["operator_edits_required"], [])
        self.assertTrue(summary["production_environment_bound"])
        return path

    def test_init_is_complete_private_production_and_exact_month_bound(self):
        path = self._request_path()
        request = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(request, {
            "pipeline_id": "paypal.transaction_close",
            "payload": {
                "entity_id": "us_dtc_company",
                "period": "2026-08",
                "paypal_request": {
                    "mode": "fetch",
                    "default_entity_id": "us_dtc_company",
                    "environment": "production",
                    "interval_start": "2026-08-01T00:00:00Z",
                    "interval_end": "2026-09-01T00:00:00Z",
                    "page_size": 500,
                    "max_pages": 20,
                },
            },
        })
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        verified = verify_private_paypal_shadow_request(self.runtime, path)
        self.assertTrue(verified["valid"])
        self.assertEqual(verified["period"], "2026-08")
        self.assertTrue(verified["exact_month_bounds"])
        self.assertTrue(verified["bounded_pagination"])
        self.assertFalse(verified["credentials_included"])
        self.assertFalse(verified["network_access_performed"])
        self.assertEqual(validate_paypal_shadow_request(self.runtime, request), verified)

    def test_december_rollover_is_generated_without_manual_edit(self):
        path = self._request_path(period="2026-12")
        request = json.loads(path.read_text(encoding="utf-8"))
        connector = request["payload"]["paypal_request"]
        self.assertEqual(connector["interval_start"], "2026-12-01T00:00:00Z")
        self.assertEqual(connector["interval_end"], "2027-01-01T00:00:00Z")

    def test_fixture_sandbox_wrong_window_pagination_and_secret_fail_closed(self):
        request = json.loads(self._request_path().read_text(encoding="utf-8"))
        connector = request["payload"]["paypal_request"]
        connector["mode"] = "fixture"
        with self.assertRaisesRegex(PayPalShadowRequestError, "production fetch"):
            validate_paypal_shadow_request(self.runtime, request)
        connector["mode"] = "fetch"
        connector["environment"] = "sandbox"
        with self.assertRaisesRegex(PayPalShadowRequestError, "production fetch"):
            validate_paypal_shadow_request(self.runtime, request)
        connector["environment"] = "production"
        connector["interval_end"] = "2026-09-02T00:00:00Z"
        with self.assertRaisesRegex(PayPalShadowRequestError, "exact month"):
            validate_paypal_shadow_request(self.runtime, request)
        connector["interval_end"] = "2026-09-01T00:00:00Z"
        connector["page_size"] = 499
        with self.assertRaisesRegex(PayPalShadowRequestError, "bounded pagination"):
            validate_paypal_shadow_request(self.runtime, request)
        connector["page_size"] = 500
        connector["client_id"] = "private-paypal-client"
        with self.assertRaisesRegex(PayPalShadowRequestError, "credentials"):
            validate_paypal_shadow_request(self.runtime, request)

    def test_cross_entity_non_private_and_overwrite_fail_closed(self):
        path = self._request_path()
        request = json.loads(path.read_text(encoding="utf-8"))
        request["payload"]["entity_id"] = "other"
        request["payload"]["paypal_request"]["default_entity_id"] = "other"
        with self.assertRaisesRegex(PayPalShadowRequestError, "Unknown legal entity"):
            validate_paypal_shadow_request(self.runtime, request)
        with self.assertRaisesRegex(PayPalShadowRequestError, "already exists"):
            build_paypal_shadow_request(
                self.runtime,
                entity_id="us_dtc_company",
                period="2026-08",
                output=path,
            )
        if os.name != "nt":
            path.chmod(0o644)
            with self.assertRaisesRegex(PayPalShadowRequestError, "0600"):
                read_private_paypal_shadow_request(path)


if __name__ == "__main__":
    unittest.main()
