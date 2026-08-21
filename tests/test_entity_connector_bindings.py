from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.activation_workspace import initialize_activation_workspace
from src.box_compiler import build_pipeline_runtime_catalog
from src.box_runtime import BoxRuntime
from src.connector_shadow_artifacts import build_connector_shadow_baseline_plan
from src.default_connectors import build_box_connector_registry
from src.pilot_readiness import build_pilot_readiness_plan
from src.production_readiness import build_production_readiness_plan


ROOT = Path(__file__).resolve().parents[1]
BOX = ROOT / "examples" / "boxes" / "cn_nl_us_dtc_entity_connectors.json"


class EntityConnectorBindingTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(BOX, ROOT / "packs")

    def test_runtime_pipeline_and_readiness_projects_exact_entity_scopes(self):
        connectors = build_box_connector_registry(self.runtime).catalog(self.runtime)
        scopes_by_pack: dict[str, set[tuple[str, ...]]] = {}
        for connector in connectors:
            scopes_by_pack.setdefault(connector["pack_id"], set()).add(
                tuple(connector["entity_ids"])
            )
        self.assertEqual(scopes_by_pack["connector.xero"], {("cn_ops",)})
        self.assertEqual(scopes_by_pack["connector.shopify"], {("nl_sales",)})
        self.assertEqual(scopes_by_pack["connector.stripe"], {("nl_sales",)})
        self.assertEqual(scopes_by_pack["connector.wise"], {("us_ops",)})

        catalog = build_pipeline_runtime_catalog(self.runtime)
        templates = catalog["request_templates"]["templates"]
        self.assertEqual({
            item["entity_id"] for item in templates
            if item["pipeline_id"] == "dtc.shopify_stripe_daily_close"
        }, {"nl_sales"})
        trial_balance = {
            item["entity_id"]: item["request"]["payload"]["connector_id"]
            for item in templates
            if item["pipeline_id"] == "finance.trial_balance_review"
        }
        self.assertEqual(trial_balance["cn_ops"], "xero.trial_balance")
        self.assertEqual(trial_balance["nl_sales"], "file.trial_balance")
        self.assertEqual(trial_balance["us_ops"], "file.trial_balance")

        pilot = build_pilot_readiness_plan(self.runtime)
        pilot_scopes = {
            item["connector_id"]: tuple(item["entity_ids"])
            for item in pilot["network_connector_requirements"]
        }
        for connector in connectors:
            if connector["network_access"]:
                self.assertEqual(
                    pilot_scopes[connector["connector_id"]],
                    tuple(connector["entity_ids"]),
                )
        production = build_production_readiness_plan(self.runtime)
        self.assertTrue(all(
            item.get("entity_ids")
            for item in production["connector_requirements"]
        ))

    def test_shadow_plan_and_activation_create_only_bound_entity_workpapers(self):
        plan = build_connector_shadow_baseline_plan(self.runtime)
        scopes = {
            (item["pipeline_id"], tuple(item["entity_ids"]))
            for item in plan["profiles"]
        }
        self.assertEqual(scopes, {
            ("finance.trial_balance_review", ("cn_ops",)),
            ("dtc.shopify_stripe_month_close", ("nl_sales",)),
            ("stripe.daily_close", ("nl_sales",)),
            ("finance.bank_statement_close", ("us_ops",)),
        })
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir).resolve() / "activation"
            result = initialize_activation_workspace(
                self.runtime,
                BOX,
                workspace,
                period="2026-08",
                facts_as_of="2026-08-14",
                prepared_by="entity-binding-auditor",
            )
            self.assertEqual(result["connector_baseline_workpaper_count"], 4)
            self.assertEqual({path.name for path in (
                workspace / "connector-shadow" / "workpapers"
            ).glob("*.json")}, {
                "cn_ops--finance-trial_balance_review.json",
                "nl_sales--dtc-shopify_stripe_month_close.json",
                "nl_sales--stripe-daily_close.json",
                "us_ops--finance-bank_statement_close.json",
            })


if __name__ == "__main__":
    unittest.main()
