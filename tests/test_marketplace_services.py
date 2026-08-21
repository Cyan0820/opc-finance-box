import unittest
from pathlib import Path

from src.box_runtime import BoxRuntime
from src.default_connectors import build_default_connector_registry
from src.default_services import build_default_service_registry


ROOT = Path(__file__).resolve().parents[1]


class MarketplaceServicesTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(ROOT / "examples" / "boxes" / "cn_marketplace_store.json", ROOT / "packs")
        self.registry = build_default_service_registry()

    def test_marketplace_pack_exposes_import_and_three_reconciliations(self):
        service_ids = {item["service_id"] for item in self.registry.catalog(self.runtime)}
        self.assertTrue({
            "marketplace.reconcile_fees",
            "marketplace.reconcile_inventory",
            "marketplace.reconcile_receivable",
        }.issubset(service_ids))
        connector_ids = {
            item["connector_id"] for item in build_default_connector_registry().catalog(self.runtime)
        }
        self.assertIn("file.marketplace_commerce", connector_ids)
        self.assertIn("example.marketplace_api_payload", connector_ids)

    def test_inventory_reconciliation_preserves_difference_without_adjusting(self):
        result = self.registry.dispatch(
            self.runtime,
            "marketplace.reconcile_inventory",
            {
                "platform_inventory": [{
                    "entity_id": "cn_marketplace_company", "sku": "SKU-1", "warehouse": "FBA-1",
                    "quantity": 10, "evidence": ["platform report"],
                }],
                "ledger_inventory": [{
                    "entity_id": "cn_marketplace_company", "sku": "SKU-1", "warehouse": "FBA-1",
                    "quantity": 9, "evidence": ["inventory ledger"],
                }],
            },
        )["output"]
        self.assertFalse(result["ready"])
        self.assertEqual(result["rows"][0]["difference"], 1)
        self.assertFalse(result["posting_or_inventory_adjustment_performed"])

    def test_fee_and_receivable_services_expose_distinct_candidate_contracts(self):
        request = __import__("json").loads(
            (ROOT / "examples" / "pipelines" / "marketplace_channel_close_fixture.json").read_text()
        )["payload"]
        from src.default_connectors import build_box_connector_registry
        batch = build_box_connector_registry(self.runtime).dispatch(
            self.runtime, request["connector_id"], request["connector_request"],
        )["batch"]
        payload = {
            "orders": batch["datasets"]["commerce.orders"],
            "settlements": batch["datasets"]["commerce.settlements"],
        }
        fees = self.registry.dispatch(
            self.runtime, "marketplace.reconcile_fees", payload,
            entity_ids=["cn_marketplace_company"],
        )["output"]
        receivable = self.registry.dispatch(
            self.runtime, "marketplace.reconcile_receivable", payload,
            entity_ids=["cn_marketplace_company"],
        )["output"]
        self.assertTrue(fees["ready"])
        self.assertTrue(receivable["ready"])
        self.assertIn("channel_and_payment_fees", fees["fee_reconciliation"][0])
        self.assertEqual(
            fees["fee_reconciliation"][0]["gross_merchandise_sales_ex_tax"], 100.0,
        )
        self.assertEqual(fees["fee_reconciliation"][0]["net_revenue_ex_tax"], 80.0)
        self.assertIn("order_to_reported_difference", receivable["receivable_reconciliation"][0])
        self.assertFalse(fees["contract_interpretation_performed"])
        self.assertFalse(receivable["collection_or_writeoff_performed"])


if __name__ == "__main__":
    unittest.main()
