from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from src.box_runtime import BoxRuntime
from src.xero_shadow_request import (
    XeroShadowRequestError,
    build_xero_shadow_request,
    read_private_xero_shadow_request,
    validate_xero_shadow_request,
    verify_private_xero_shadow_request,
)


ROOT = Path(__file__).resolve().parents[1]
BOX = ROOT / "examples" / "boxes" / "global_game_studio_xero.json"


class XeroShadowRequestTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(BOX, ROOT / "packs")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def _request_path(self, *, entity_id="cn_studio", period="2026-07") -> Path:
        path = Path(self.temp.name) / f"private-xero-{entity_id}-{period}.json"
        summary = build_xero_shadow_request(
            self.runtime,
            entity_id=entity_id,
            period=period,
            output=path,
        )
        self.assertFalse(summary["template_only"])
        self.assertTrue(summary["request_contract_complete"])
        self.assertTrue(summary["ready_for_network_dispatch"])
        self.assertEqual(summary["operator_edits_required"], [])
        self.assertFalse(summary["credential_configuration_checked"])
        return path

    def test_init_is_complete_private_and_exactly_entity_month_end_bound(self):
        path = self._request_path()
        request = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(request, {
            "pipeline_id": "finance.trial_balance_review",
            "payload": {
                "entity_id": "cn_studio",
                "period": "2026-07",
                "connector_id": "xero.trial_balance",
                "connector_request": {
                    "mode": "fetch",
                    "as_at": "2026-07-31",
                    "payments_only": False,
                },
            },
        })
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        verified = verify_private_xero_shadow_request(self.runtime, path)
        self.assertTrue(verified["valid"])
        self.assertEqual(verified["period"], "2026-07")
        self.assertEqual(verified["functional_currency"], "CNY")
        self.assertTrue(verified["month_end_as_at"])
        self.assertTrue(verified["payments_only_disabled"])
        self.assertFalse(verified["credentials_included"])
        self.assertFalse(verified["network_access_performed"])
        self.assertEqual(validate_xero_shadow_request(self.runtime, request), verified)

    def test_leap_year_month_end_and_second_entity_are_derived_from_box(self):
        path = self._request_path(entity_id="sg_publisher", period="2028-02")
        request = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(
            request["payload"]["connector_request"]["as_at"], "2028-02-29",
        )
        verified = verify_private_xero_shadow_request(self.runtime, path)
        self.assertEqual(verified["entity_id"], "sg_publisher")
        self.assertEqual(verified["functional_currency"], "USD")

    def test_fixture_non_month_end_cash_basis_and_inline_binding_fail_closed(self):
        path = self._request_path()
        request = json.loads(path.read_text(encoding="utf-8"))
        connector = request["payload"]["connector_request"]
        connector["mode"] = "fixture"
        with self.assertRaisesRegex(XeroShadowRequestError, "fetch mode"):
            validate_xero_shadow_request(self.runtime, request)
        connector["mode"] = "fetch"
        connector["as_at"] = "2026-07-30"
        with self.assertRaisesRegex(XeroShadowRequestError, "month-end"):
            validate_xero_shadow_request(self.runtime, request)
        connector["as_at"] = "2026-07-31"
        connector["payments_only"] = True
        with self.assertRaisesRegex(XeroShadowRequestError, "payments_only=false"):
            validate_xero_shadow_request(self.runtime, request)
        connector["payments_only"] = False
        connector["tenant_id"] = "xero-private-tenant"
        with self.assertRaisesRegex(XeroShadowRequestError, "tenant bindings"):
            validate_xero_shadow_request(self.runtime, request)

    def test_cross_entity_non_private_and_overwrite_fail_closed(self):
        path = self._request_path()
        request = json.loads(path.read_text(encoding="utf-8"))
        request["payload"]["entity_id"] = "other"
        with self.assertRaisesRegex(XeroShadowRequestError, "Unknown legal entity"):
            validate_xero_shadow_request(self.runtime, request)
        with self.assertRaisesRegex(XeroShadowRequestError, "already exists"):
            build_xero_shadow_request(
                self.runtime,
                entity_id="cn_studio",
                period="2026-07",
                output=path,
            )
        if os.name != "nt":
            path.chmod(0o644)
            with self.assertRaisesRegex(XeroShadowRequestError, "0600"):
                read_private_xero_shadow_request(path)


if __name__ == "__main__":
    unittest.main()
