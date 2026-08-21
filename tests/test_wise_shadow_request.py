from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from src.box_runtime import BoxRuntime
from src.wise_shadow_request import (
    WiseShadowRequestError,
    build_wise_shadow_request,
    read_private_wise_shadow_request,
    validate_wise_shadow_request,
    verify_private_wise_shadow_request,
)


ROOT = Path(__file__).resolve().parents[1]
BOX = ROOT / "examples" / "boxes" / "sg_dtc_wise_store.json"


class WiseShadowRequestTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(BOX, ROOT / "packs")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def _request_path(self) -> Path:
        path = Path(self.temp.name) / "private-wise-request.json"
        summary = build_wise_shadow_request(
            self.runtime,
            entity_id="sg_store",
            period="2026-07",
            output=path,
        )
        self.assertFalse(summary["template_only"])
        self.assertTrue(summary["request_contract_complete"])
        self.assertTrue(summary["ready_for_network_dispatch"])
        self.assertEqual(summary["operator_edits_required"], [])
        self.assertFalse(summary["credential_configuration_checked"])
        return path

    def test_init_is_complete_private_and_exactly_entity_currency_month_bound(self):
        path = self._request_path()
        request = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(request, {
            "pipeline_id": "finance.bank_statement_close",
            "payload": {
                "entity_id": "sg_store",
                "period": "2026-07",
                "connector_id": "wise.balance_statement",
                "connector_request": {
                    "mode": "fetch",
                    "default_entity_id": "sg_store",
                    "currency": "SGD",
                    "interval_start": "2026-07-01T00:00:00Z",
                    "interval_end": "2026-08-01T00:00:00Z",
                },
            },
        })
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        verified = verify_private_wise_shadow_request(self.runtime, path)
        self.assertTrue(verified["valid"])
        self.assertEqual(verified["period"], "2026-07")
        self.assertEqual(verified["currency"], "SGD")
        self.assertFalse(verified["credentials_included"])
        self.assertFalse(verified["network_access_performed"])
        self.assertEqual(validate_wise_shadow_request(self.runtime, request), verified)

    def test_fixture_wrong_currency_cross_month_and_inline_binding_fail_closed(self):
        path = self._request_path()
        request = json.loads(path.read_text(encoding="utf-8"))
        connector = request["payload"]["connector_request"]
        connector["mode"] = "fixture"
        with self.assertRaisesRegex(WiseShadowRequestError, "fetch mode"):
            validate_wise_shadow_request(self.runtime, request)
        connector["mode"] = "fetch"
        connector["currency"] = "USD"
        with self.assertRaisesRegex(WiseShadowRequestError, "functional currency"):
            validate_wise_shadow_request(self.runtime, request)
        connector["currency"] = "SGD"
        connector["interval_end"] = "2026-08-02T00:00:00Z"
        with self.assertRaisesRegex(WiseShadowRequestError, "exact month"):
            validate_wise_shadow_request(self.runtime, request)
        connector["interval_end"] = "2026-08-01T00:00:00Z"
        connector["profile_id"] = 123456
        with self.assertRaisesRegex(WiseShadowRequestError, "account bindings"):
            validate_wise_shadow_request(self.runtime, request)

    def test_cross_entity_and_non_private_files_fail_closed(self):
        path = self._request_path()
        request = json.loads(path.read_text(encoding="utf-8"))
        request["payload"]["entity_id"] = "other"
        request["payload"]["connector_request"]["default_entity_id"] = "other"
        with self.assertRaisesRegex(WiseShadowRequestError, "Unknown legal entity"):
            validate_wise_shadow_request(self.runtime, request)
        with self.assertRaisesRegex(WiseShadowRequestError, "already exists"):
            build_wise_shadow_request(
                self.runtime,
                entity_id="sg_store",
                period="2026-07",
                output=path,
            )
        if os.name != "nt":
            path.chmod(0o644)
            with self.assertRaisesRegex(WiseShadowRequestError, "0600"):
                read_private_wise_shadow_request(path)


if __name__ == "__main__":
    unittest.main()
