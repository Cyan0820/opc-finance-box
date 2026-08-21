from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from src.box_runtime import BoxRuntime
from src.box_pipeline import dispatch_box_pipeline_request
from src.connector_sdk import ConnectorError
from src.default_connectors import build_box_connector_registry
from src.default_services import build_default_service_registry


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "packs" / "connectors" / "shopify" / "fixture-monthly-order-evidence.json"


class ShopifyMonthlyMetricSourceTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(
            ROOT / "examples" / "boxes" / "cn_dtc_shopify_stripe_store.json",
            ROOT / "packs",
        )
        self.connectors = build_box_connector_registry(self.runtime)
        self.services = build_default_service_registry()
        self.request = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def _batch(self, request=None):
        return self.connectors.dispatch(
            self.runtime, "shopify.monthly_order_evidence", request or self.request,
        )["batch"]

    def _scope(self, batch):
        return self.services.dispatch(
            self.runtime,
            "shopify.build_monthly_commerce_scope",
            {
                "orders": batch["datasets"]["commerce.shopify_orders"],
                "refunds": batch["datasets"]["commerce.shopify_refunds"],
                "source_scope": batch["source"],
            },
            entity_id="cn_dtc_company",
        )["output"]

    def test_dual_population_proves_month_and_captures_old_order_refund(self):
        batch = self._batch()
        self.assertTrue(batch["quality"]["ready"], batch)
        self.assertEqual(batch["source"]["canonical_month_period"], "2026-07")
        self.assertEqual(batch["source"]["created_population_count"], 1)
        self.assertEqual(batch["source"]["updated_since_month_start_population_count"], 1)
        orders = {
            row["order_id"]: row for row in batch["datasets"]["commerce.shopify_orders"]
        }
        self.assertEqual(orders["gid://shopify/Order/7001"]["source_populations"], ["created"])
        self.assertEqual(orders["gid://shopify/Order/6001"]["source_populations"], ["updated"])

        output = self._scope(batch)
        self.assertTrue(output["ready"], output)
        row = output["monthly_commerce_scope"][0]
        self.assertEqual(row["period"], "2026-07")
        self.assertEqual(row["gross_order_sales_ex_tax_including_shipping"], "105.00")
        self.assertEqual(row["discounts_and_refunds_ex_tax"], "30.00")
        self.assertEqual(row["gross_merchandise_sales_ex_tax"], "100.00")
        self.assertEqual(row["refunds_ex_tax"], "20.00")
        self.assertEqual(row["created_order_count"], 1)
        self.assertEqual(row["refund_event_count"], 1)
        self.assertFalse(output["tax_inclusive_policy_auto_confirmed"])
        self.assertFalse(output["return_authorization_and_receipt_scope_auto_confirmed"])

    def test_window_close_capture_and_batch_identity_fail_closed(self):
        request = copy.deepcopy(self.request)
        request["interval_end"] = "2026-08-02T00:00:00Z"
        with self.assertRaisesRegex(ConnectorError, "first day of the next month"):
            self._batch(request)

        request = copy.deepcopy(self.request)
        request["source_observed_at"] = "2026-08-04T01:00:01Z"
        with self.assertRaisesRegex(ConnectorError, "72-hour"):
            self._batch(request)

        first = self._batch()
        request = copy.deepcopy(self.request)
        request["source_observed_at"] = "2026-08-01T02:00:00Z"
        second = self._batch(request)
        self.assertNotEqual(first["batch_id"], second["batch_id"])

    def test_refund_components_transaction_and_tax_policy_are_not_inferred(self):
        request = copy.deepcopy(self.request)
        refund = request["updated_objects"][0]["refunds"][0]
        refund["transactions"]["nodes"][0]["status"] = "FAILURE"
        output = self._scope(self._batch(request))
        self.assertFalse(output["ready"])
        self.assertTrue(any("successful transactions" in item for item in output["blockers"]))

        request = copy.deepcopy(self.request)
        request["created_objects"][0]["taxesIncluded"] = True
        output = self._scope(self._batch(request))
        self.assertFalse(output["ready"])
        self.assertTrue(any("tax-inclusive" in item for item in output["blockers"]))

    def test_nested_connection_pagination_is_rejected_instead_of_truncated(self):
        request = copy.deepcopy(self.request)
        request["updated_objects"][0]["refunds"][0]["refundLineItems"]["pageInfo"][
            "hasNextPage"
        ] = True
        batch = self._batch(request)
        self.assertFalse(batch["quality"]["ready"])
        self.assertTrue(any(
            "incomplete" in item["reason"] for item in batch["quality"]["rejected_rows"]
        ))

    def _pipeline_request(self):
        return {
            "pipeline_id": "dtc.shopify_stripe_month_close",
            "payload": {
                "entity_id": "cn_dtc_company",
                "currency_minor_units": {"USD": 2},
                "shopify_monthly_request": copy.deepcopy(self.request),
                "stripe_balance_request": {
                    "mode": "fixture",
                    "created_gte": 1782864000,
                    "created_lt": 1785542400,
                    "objects": [
                        {
                            "id": "txn_month_charge_7001",
                            "object": "balance_transaction",
                            "amount": 10450,
                            "available_on": 1784000000,
                            "created": 1783700000,
                            "currency": "usd",
                            "description": "July Shopify collection",
                            "exchange_rate": None,
                            "fee": 300,
                            "fee_details": [],
                            "net": 10150,
                            "reporting_category": "charge",
                            "source": "ch_month_7001",
                            "status": "available",
                            "type": "charge",
                        },
                        {
                            "id": "txn_month_refund_6001",
                            "object": "balance_transaction",
                            "amount": -2200,
                            "available_on": 1785000000,
                            "created": 1784600000,
                            "currency": "usd",
                            "description": "July Shopify refund",
                            "exchange_rate": None,
                            "fee": 0,
                            "fee_details": [],
                            "net": -2200,
                            "reporting_category": "refund",
                            "source": "re_month_6001",
                            "status": "available",
                            "type": "refund",
                        },
                    ],
                },
                "processor_links": [
                    {
                        "entity_id": "cn_dtc_company",
                        "shopify_transaction_id": "gid://shopify/OrderTransaction/7201",
                        "stripe_source_object_id": "ch_month_7001",
                        "evidence": {
                            "source_file": "fixture:processor-links",
                            "batch_id": "links-2026-07",
                        },
                    },
                    {
                        "entity_id": "cn_dtc_company",
                        "shopify_transaction_id": "gid://shopify/OrderTransaction/6201",
                        "stripe_source_object_id": "re_month_6001",
                        "evidence": {
                            "source_file": "fixture:processor-links",
                            "batch_id": "links-2026-07",
                        },
                    },
                ],
            },
        }

    def test_month_pipeline_proves_same_window_and_attaches_trusted_metric_operands(self):
        result = dispatch_box_pipeline_request(self.runtime, self._pipeline_request())
        self.assertTrue(result["ready"], result)
        self.assertTrue(result["lineage"]["canonical_month_scope"])
        self.assertEqual(result["lineage"]["period"], "2026-07")
        reconciliation = result["services"][
            "shopify_stripe_activity_reconciliation"
        ]["output"]
        self.assertTrue(reconciliation["ready"], reconciliation)
        assembly = result["cfo_metric_operand_assembly"]
        self.assertEqual(assembly["coverage_status"], "executable")
        self.assertEqual(assembly["assembly_count"], 1)
        candidate = assembly["assemblies"][0]
        self.assertEqual(candidate["period"], "2026-07")
        self.assertEqual(candidate["operand_values"], {
            "discounts_and_refunds_ex_tax": "30",
            "gross_merchandise_sales_ex_tax": "100",
            "gross_order_sales_ex_tax_including_shipping": "105",
            "refunds_ex_tax": "20",
        })
        self.assertEqual(candidate["confirmed_control_type_ids"], [
            "order_and_refund_period_scope_aligned",
        ])
        self.assertIn("tax_inclusive_policy_confirmed", candidate["pending_control_type_ids"])
        self.assertIn(
            "return_authorization_and_receipt_scope_aligned",
            candidate["pending_control_type_ids"],
        )

    def test_month_pipeline_rejects_mismatched_stripe_window(self):
        request = self._pipeline_request()
        request["payload"]["stripe_balance_request"]["created_gte"] += 1
        result = dispatch_box_pipeline_request(self.runtime, request)
        self.assertFalse(result["ready"])
        self.assertEqual(result["blocked_at"], "canonical_month_scope_gate")
        self.assertEqual(result["cfo_metric_operand_assembly"]["assembly_count"], 0)


if __name__ == "__main__":
    unittest.main()
