from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from src.box_runtime import BoxRuntime
from src.connector_access_probe import write_connector_access_probe_receipt
from src.connector_access_registry import (
    ConnectorAccessRegistryError,
    build_connector_access_alerts,
    build_connector_access_registry,
)
from src.connector_http import HttpResponse


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "examples" / "boxes" / "cn_dtc_shopify_stripe_store.json"


class ConnectorAccessRegistryTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(CONFIG, ROOT / "packs")
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()

    def tearDown(self):
        self.temp.cleanup()

    def _scope(self, pack_id: str = "connector.stripe") -> dict:
        return {
            "pack_id": pack_id,
            "entity_id": "cn_dtc_company",
            "request": self.root / f"{pack_id}-request.json",
            "receipt": self.root / f"{pack_id}-receipt.json",
        }

    @staticmethod
    def _write_private(path: Path, value: dict) -> None:
        path.write_text(json.dumps(value), encoding="utf-8")
        os.chmod(path, 0o600)

    def _write_stripe_request(self, path: Path) -> None:
        self._write_private(path, {
            "schema_version": 1,
            "pack_id": "connector.stripe",
            "entity_id": "cn_dtc_company",
            "account_binding": {
                "mode": "own_account",
                "account_id": "acct_123456789ABC",
            },
        })

    def test_missing_and_request_only_states_are_safe_and_actionable(self):
        scope = self._scope()
        missing = build_connector_access_registry(
            self.runtime, [scope, scope], as_of="2026-08-16", environ={},
        )
        self.assertEqual(missing["summary"]["expected_scope_count"], 1)
        self.assertEqual(missing["counts"]["not_initialized"], 1)
        self.assertFalse(missing["summary"]["ready_for_bounded_shadow_dispatch"])
        missing_alerts = build_connector_access_alerts(missing)
        self.assertEqual(missing_alerts["warning_count"], 1)
        self.assertEqual(
            missing_alerts["alerts"][0]["alert_id"],
            "connector-access:connector.stripe:cn_dtc_company:not_initialized",
        )
        self.assertFalse(missing_alerts["notifications_sent"])
        self.assertFalse(missing_alerts["schedule_installed"])

        self._write_stripe_request(scope["request"])
        awaiting = build_connector_access_registry(
            self.runtime, [scope], as_of="2026-08-16", environ={},
        )
        self.assertEqual(awaiting["scopes"][0]["status"], "awaiting_current_credential")
        ready = build_connector_access_registry(
            self.runtime,
            [scope],
            as_of="2026-08-16",
            environ={"OPC_STRIPE_RESTRICTED_KEY": "rk_test_1234567890PRIVATE"},
        )
        self.assertEqual(ready["scopes"][0]["status"], "ready_for_authorized_probe")
        serialized = json.dumps(ready)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn("acct_", serialized)
        self.assertNotIn("rk_test_", serialized)

    def test_current_receipt_is_shared_scope_state_not_promotion_evidence(self):
        scope = self._scope()
        self._write_stripe_request(scope["request"])
        responses = [
            HttpResponse(200, {}, b'{"object":"account","id":"acct_123456789ABC"}'),
            HttpResponse(200, {}, b'{"object":"list","data":[],"has_more":false}'),
            HttpResponse(200, {}, b'{"object":"list","data":[],"has_more":false}'),
        ]
        environment = {
            "OPC_STRIPE_RESTRICTED_KEY": "rk_test_1234567890PRIVATE",
        }
        write_connector_access_probe_receipt(
            self.runtime,
            scope["request"],
            scope["receipt"],
            allow_network=True,
            environ=environment,
            transport=lambda request: responses.pop(0),
            sleeper=lambda seconds: None,
            observed_at="2026-08-16T12:00:00+00:00",
        )
        registry = build_connector_access_registry(
            self.runtime,
            [scope, scope],
            as_of="2026-08-16",
            environ=environment,
        )
        self.assertEqual(registry["schema_version"], 3)
        self.assertEqual(registry["counts"]["current"], 1)
        self.assertTrue(registry["summary"]["all_expected_access_current"])
        self.assertTrue(registry["summary"]["ready_for_bounded_shadow_dispatch"])
        self.assertTrue(
            registry["control_boundary"][
                "one_current_scope_may_gate_multiple_compatible_pipelines"
            ]
        )
        self.assertFalse(
            registry["control_boundary"]["receipt_is_stable_promotion_evidence"]
        )

        renewal_due = build_connector_access_registry(
            self.runtime,
            [scope],
            as_of="2026-09-08",
            environ=environment,
        )
        self.assertEqual(renewal_due["scopes"][0]["status"], "renewal_due")
        self.assertEqual(renewal_due["scopes"][0]["days_until_expiry"], 7)
        self.assertEqual(
            renewal_due["scopes"][0]["next_cli_command"],
            "connector-access-receipt-renew",
        )
        self.assertEqual(renewal_due["counts"]["current"], 0)
        self.assertEqual(renewal_due["counts"]["renewal_due"], 1)
        self.assertEqual(renewal_due["summary"]["current_scope_count"], 1)
        self.assertEqual(renewal_due["summary"]["renewal_due_count"], 1)
        self.assertTrue(renewal_due["summary"]["ready_for_bounded_shadow_dispatch"])
        due_alerts = build_connector_access_alerts(renewal_due)
        self.assertEqual(due_alerts["warning_count"], 1)
        self.assertEqual(due_alerts["critical_count"], 0)
        self.assertEqual(due_alerts["alerts"][0]["days_until_expiry"], 7)
        self.assertEqual(
            due_alerts["alerts"][0]["next_cli_command"],
            "connector-access-receipt-renew",
        )

        changed = build_connector_access_registry(
            self.runtime,
            [scope],
            as_of="2026-08-16",
            environ={"OPC_STRIPE_RESTRICTED_KEY": "rk_test_1234567890CHANGED"},
        )
        self.assertEqual(
            changed["scopes"][0]["status"],
            "renewal_required",
        )
        self.assertEqual(
            changed["scopes"][0]["next_cli_command"],
            "connector-access-receipt-renew",
        )
        self.assertEqual(changed["summary"]["renewal_required_count"], 1)
        changed_alerts = build_connector_access_alerts(changed)
        self.assertEqual(changed_alerts["critical_count"], 1)
        self.assertFalse(changed_alerts["ready_for_bounded_shadow_dispatch"])
        serialized_alerts = json.dumps(changed_alerts)
        self.assertNotIn(str(self.root), serialized_alerts)
        self.assertNotIn("acct_", serialized_alerts)
        self.assertNotIn("rk_test_", serialized_alerts)

        stale = build_connector_access_registry(
            self.runtime,
            [scope],
            as_of="2026-10-01",
            environ=environment,
        )
        self.assertEqual(stale["scopes"][0]["status"], "renewal_required")
        self.assertEqual(stale["counts"]["renewal_required"], 1)

        tampered = json.loads(scope["receipt"].read_text(encoding="utf-8"))
        tampered["summary"]["ready_for_private_shadow_request"] = False
        self._write_private(scope["receipt"], tampered)
        invalid = build_connector_access_registry(
            self.runtime,
            [scope],
            as_of="2026-08-16",
            environ=environment,
        )
        self.assertEqual(invalid["scopes"][0]["status"], "blocked_invalid_receipt")
        self.assertEqual(invalid["counts"]["blocked_invalid_receipt"], 1)

    def test_paypal_scope_requires_selected_entity_binding_and_alias_values(self):
        runtime = BoxRuntime(
            ROOT / "examples" / "boxes" / "us_dtc_paypal_c_corp.json",
            ROOT / "packs",
        )
        scope = {
            "pack_id": "connector.paypal",
            "entity_id": "us_dtc_company",
            "request": self.root / "paypal-request.json",
            "receipt": self.root / "paypal-receipt.json",
        }
        self._write_private(scope["request"], {
            "schema_version": 1,
            "pack_id": "connector.paypal",
            "entity_id": "us_dtc_company",
            "account_binding": {"mode": "entity_environment_binding"},
        })
        legacy = build_connector_access_registry(
            runtime,
            [scope],
            environ={
                "OPC_PAYPAL_CLIENT_ID": "legacy-client",
                "OPC_PAYPAL_CLIENT_SECRET": "legacy-secret",
            },
        )
        self.assertEqual(
            legacy["scopes"][0]["status"], "awaiting_current_credential",
        )
        environment = {
            "OPC_PAYPAL_ENTITY_BINDINGS_JSON": json.dumps({
                "us_dtc_company": {
                    "environment": "production",
                    "app_id": "APPID_1234",
                    "account_id": "2ABCD3EFGH4JK",
                    "client_id_env": "OPC_PAYPAL_US_CLIENT_ID",
                    "client_secret_env": "OPC_PAYPAL_US_CLIENT_SECRET",
                },
            }),
            "OPC_PAYPAL_US_CLIENT_ID": "private-client",
            "OPC_PAYPAL_US_CLIENT_SECRET": "private-secret",
        }
        ready = build_connector_access_registry(
            runtime, [scope], environ=environment,
        )
        self.assertEqual(
            ready["scopes"][0]["status"], "ready_for_authorized_probe",
        )
        serialized = json.dumps(ready)
        self.assertNotIn("private-client", serialized)
        self.assertNotIn("2ABCD3EFGH4JK", serialized)

    def test_orphan_invalid_and_conflicting_scope_fail_closed(self):
        scope = self._scope()
        self._write_private(scope["receipt"], {"not": "a receipt"})
        orphan = build_connector_access_registry(self.runtime, [scope], environ={})
        self.assertEqual(orphan["scopes"][0]["status"], "blocked_orphan_receipt")

        self._write_private(scope["request"], {"not": "a request"})
        invalid = build_connector_access_registry(self.runtime, [scope], environ={})
        self.assertEqual(invalid["scopes"][0]["status"], "blocked_invalid_request")

        conflicting = dict(scope)
        conflicting["request"] = self.root / "other-request.json"
        with self.assertRaisesRegex(ConnectorAccessRegistryError, "conflicting"):
            build_connector_access_registry(self.runtime, [scope, conflicting])
        with self.assertRaisesRegex(ConnectorAccessRegistryError, "between 1 and 365"):
            build_connector_access_registry(
                self.runtime, [scope], maximum_age_days=0,
            )
        with self.assertRaisesRegex(
            ConnectorAccessRegistryError, "warning_days_before_expiry",
        ):
            build_connector_access_registry(
                self.runtime,
                [scope],
                maximum_age_days=30,
                warning_days_before_expiry=31,
            )
        with self.assertRaisesRegex(ConnectorAccessRegistryError, "schema v3"):
            build_connector_access_alerts({"schema_version": 2})

    def test_multi_reference_wise_scope_requires_current_token_and_entity_binding(self):
        runtime = BoxRuntime(
            ROOT / "examples" / "boxes" / "sg_dtc_wise_store.json",
            ROOT / "packs",
        )
        scope = {
            "pack_id": "connector.wise",
            "entity_id": "sg_store",
            "request": self.root / "wise-request.json",
            "receipt": self.root / "wise-receipt.json",
        }
        self._write_private(scope["request"], {
            "schema_version": 1,
            "pack_id": "connector.wise",
            "entity_id": "sg_store",
            "account_binding": {"mode": "entity_environment_binding"},
        })
        bindings = json.dumps({
            "sg_store": {
                "profile_id": 123456,
                "business_name": "OPC Wise Demo Pte Ltd",
                "access_contract": "personal_token_eligible",
                "balances": {"SGD": {
                    "balance_id": 987654,
                    "account_reference_masked": "Wise SGD ••7654",
                }},
            },
        })
        environment = {
            "OPC_WISE_ACCESS_TOKEN": "wise_private_token",
            "OPC_WISE_ENTITY_BINDINGS_JSON": bindings,
        }
        responses = [
            HttpResponse(200, {}, b'{"id":123456,"type":"BUSINESS","businessName":"OPC Wise Demo Pte Ltd"}'),
            HttpResponse(200, {}, b'{"id":987654,"currency":"SGD","type":"STANDARD"}'),
        ]
        write_connector_access_probe_receipt(
            runtime,
            scope["request"],
            scope["receipt"],
            allow_network=True,
            environ=environment,
            transport=lambda request: responses.pop(0),
            sleeper=lambda seconds: None,
            observed_at="2026-08-16T12:00:00+00:00",
        )
        current = build_connector_access_registry(
            runtime, [scope], as_of="2026-08-16", environ=environment,
        )
        self.assertEqual(current["counts"]["current"], 1)
        missing = build_connector_access_registry(
            runtime,
            [scope],
            as_of="2026-08-16",
            environ={"OPC_WISE_ACCESS_TOKEN": "wise_private_token"},
        )
        self.assertEqual(
            missing["scopes"][0]["status"],
            "blocked_missing_current_credential",
        )
        changed = json.loads(bindings)
        changed["sg_store"]["balances"]["SGD"]["balance_id"] = 111111
        rotated = build_connector_access_registry(
            runtime,
            [scope],
            as_of="2026-08-16",
            environ={
                **environment,
                "OPC_WISE_ENTITY_BINDINGS_JSON": json.dumps(changed),
            },
        )
        self.assertEqual(rotated["scopes"][0]["status"], "renewal_required")
        alerts = build_connector_access_alerts(rotated)
        self.assertEqual(alerts["critical_count"], 1)
        self.assertNotIn("123456", json.dumps(alerts))


if __name__ == "__main__":
    unittest.main()
