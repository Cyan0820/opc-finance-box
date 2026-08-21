import tempfile
import unittest
from pathlib import Path

from src.box_compiler import compile_box_file, render_agent_prompts, render_box_readme, write_compiled_box


ROOT = Path(__file__).resolve().parents[1]
PACKS = ROOT / "packs"


class BoxCompilerTests(unittest.TestCase):
    def test_dynamic_entity_credential_bindings_are_explicit_deployment_contracts(self):
        cases = {
            "us_dtc_paypal_c_corp.json": (
                "connector.paypal", "OPC_PAYPAL_ENTITY_BINDINGS_JSON", 2,
            ),
            "us_dtc_woocommerce_c_corp.json": (
                "connector.woocommerce", "OPC_WOOCOMMERCE_ENTITY_BINDINGS_JSON", 2,
            ),
            "us_dtc_shopify_stripe_shipbob_c_corp.json": (
                "connector.shipbob", "OPC_SHIPBOB_ENTITY_BINDINGS_JSON", 1,
            ),
            "us_marketplace_amazon_seller_c_corp.json": (
                "connector.amazon_seller",
                "OPC_AMAZON_SELLER_ENTITY_BINDINGS_JSON", 3,
            ),
        }
        for filename, (pack_id, binding_name, alias_count) in cases.items():
            with self.subTest(filename=filename):
                compiled = compile_box_file(
                    ROOT / "examples" / "boxes" / filename, PACKS,
                )
                deployment = compiled["deployment_environment_contract"]
                self.assertEqual(deployment["schema_version"], 2)
                contract = next(
                    item for item in deployment["entity_credential_binding_contracts"]
                    if item["pack_id"] == pack_id
                )
                self.assertEqual(contract["binding_environment_name"], binding_name)
                self.assertEqual(
                    contract["dynamic_secret_alias_count_per_entity"], alias_count,
                )
                self.assertTrue(contract["dynamic_alias_values_injected_separately"])
                self.assertTrue(contract["selected_entity_slice_only_fingerprinted"])
                self.assertTrue(contract["unbound_or_incomplete_entity_fails_closed"])
                self.assertFalse(contract["multi_entity_legacy_fallback_allowed"])
                self.assertFalse(contract["legacy_root_environment_unlocks_access_receipt"])
                self.assertIn(
                    binding_name,
                    deployment["connector_private_binding_environment_names"],
                )
                environment = {
                    item["name"]: item for item in deployment["environment"]
                }
                self.assertTrue(environment[binding_name]["secret"])
                self.assertFalse(environment[binding_name]["contains_raw_secret_values"])

    def test_airwallex_box_compiles_hmac_webhook_and_async_refetch_boundary(self):
        compiled = compile_box_file(
            ROOT / "examples" / "boxes" / "sg_dtc_shopify_stripe_wise_airwallex_store.json",
            PACKS,
        )
        policy = compiled["runtime_security_policy"]
        webhook = policy["airwallex_spend_webhook"]
        self.assertTrue(webhook["enabled_for_box"])
        self.assertIn("/api/webhooks/airwallex/spend", policy["public_api_paths"])
        self.assertEqual(webhook["authentication"], "hmac_sha256_exact_timestamp_plus_raw_body")
        self.assertTrue(webhook["acknowledgement_after_durable_append"])
        self.assertFalse(webhook["acknowledgement_waits_for_provider_refetch"])
        self.assertFalse(webhook["expense_claims_created"])
        self.assertFalse(webhook["posting_performed"])
        self.assertFalse(webhook["payment_performed"])
        self.assertIn("amount_free", webhook["worker_shadow_observation"])
        deployment = compiled["deployment_environment_contract"]
        self.assertIn(
            "OPC_AIRWALLEX_WEBHOOK_SECRET",
            deployment["connector_secret_environment_names"],
        )
        connector = next(
            item for item in compiled["connectors"]
            if item["connector_id"] == "airwallex.approved_expenses"
        )
        self.assertIn("finance.expense_evidence_state_changes", connector["dataset_types"])
        pipeline = next(
            item for item in compiled["pipelines"]
            if item["pipeline_id"] == "finance.expense_evidence_review"
        )
        self.assertIn("airwallex_webhook_binding_and_quarantine_review", pipeline["review_gates"])
        promotion_gate = next(
            item for item in compiled["release_gates"]["automated_gates"]
            if item["gate"] == "stable_promotion_evidence_control"
        )
        self.assertIn("test_connector_shadow_release_promotion", promotion_gate["command"])
        connector_shadow_task = next(
            item for item in compiled["setup_tasks"]
            if item["task_id"] == "connector-shadow-registry-activation"
        )
        self.assertIn("connector-shadow-status", connector_shadow_task["command"])
        self.assertEqual(connector_shadow_task["directory_mode"], "0700")
        self.assertEqual(connector_shadow_task["artifact_mode"], "0600")
        self.assertFalse(connector_shadow_task["stable_promotion_performed"])
        connector_access_alert_task = next(
            item for item in compiled["setup_tasks"]
            if item["task_id"] == "connector-access-alert-routing"
        )
        self.assertIn(
            "connector-access-alerts",
            connector_access_alert_task["alert_command"],
        )
        self.assertFalse(connector_access_alert_task["alert_schedule_installed"])
        self.assertFalse(connector_access_alert_task["notifications_sent"])
        self.assertFalse(connector_access_alert_task["network_access_performed"])
        self.assertTrue(any(
            "schema v2 real_anonymized" in item
            for item in compiled["release_gates"]["manual_gates"]
        ))

    def test_game_box_compiles_services_workflows_and_entity_lock(self):
        compiled = compile_box_file(ROOT / "examples" / "boxes" / "global_game_studio.json", PACKS)
        self.assertEqual({entity["id"] for entity in compiled["entities"]}, {"cn_studio", "sg_publisher"})
        service_ids = {service["service_id"] for service in compiled["services"]}
        self.assertIn("game.analyze_kpis", service_ids)
        self.assertIn("tax.sg.build_calendar", service_ids)
        self.assertEqual(
            {item["connector_id"] for item in compiled["connectors"]},
            {
                "file.bank_statement",
                "file.general_ledger",
                "file.trial_balance",
                "file.commerce",
                "file.csv_commerce",
                "file.xlsx_commerce",
                "file.app_store_settlements",
                "file.google_play_settlements",
                "file.domestic_game_settlements",
            },
        )
        workflow_ids = {workflow["workflow_id"] for workflow in compiled["workflows"]}
        self.assertIn("game.investment_review", workflow_ids)
        self.assertIn("tax.cn.calendar", workflow_ids)
        self.assertFalse(compiled["deployment"]["ready_for_external_filing"])
        workflow_status = {
            workflow["workflow_id"]: workflow["implementation_status"]
            for workflow in compiled["workflows"]
        }
        self.assertEqual(workflow_status["game.revenue_close"], "executable")
        self.assertEqual(workflow_status["core.monthly_close"], "executable")
        self.assertNotIn("finance.bank_reconciliation", compiled["declared_only_capabilities"])
        self.assertNotIn("finance.multi_currency", compiled["declared_only_capabilities"])
        separate_books = next(
            item for item in compiled["capability_coverage"]
            if item["capability"] == "entity.separate_legal_books"
        )
        self.assertEqual(separate_books["providers"], ["runtime_guardrail"])
        revenue_coverage = next(
            item for item in compiled["capability_coverage"]
            if item["capability"] == "game.revenue_recognition"
        )
        self.assertIn("service", revenue_coverage["providers"])
        self.assertIn("game_channel_settlement", {
            item["object_id"] for item in compiled["data_model"]["objects"]
        })
        self.assertIn("finance.game_review", {
            item["contract_id"] for item in compiled["agent_contracts"]["contracts"]
        })
        self.assertEqual(compiled["job_plan"]["installation_status"], "not_installed")
        self.assertTrue(all(not job["enabled"] for job in compiled["job_plan"]["jobs"]))
        rotation_job = next(
            item for item in compiled["job_plan"]["jobs"]
            if item["job_id"] == "tax.applicability_registry_rotation_alerts"
        )
        self.assertEqual(rotation_job["cadence"], "daily")
        self.assertIn("tax-applicability-alerts", rotation_job["candidate_command"])
        self.assertFalse(rotation_job["notifications_sent"])
        pilot_rotation_job = next(
            item for item in compiled["job_plan"]["jobs"]
            if item["job_id"] == "pilot.readiness_review_rotation_alerts"
        )
        self.assertFalse(pilot_rotation_job["enabled"])
        self.assertEqual(pilot_rotation_job["cadence"], "daily")
        self.assertIn(
            "pilot-readiness-alerts", pilot_rotation_job["candidate_command"],
        )
        self.assertFalse(pilot_rotation_job["notifications_sent"])
        connector_access_rotation_job = next(
            item for item in compiled["job_plan"]["jobs"]
            if item["job_id"] == "connector.access_receipt_rotation_alerts"
        )
        self.assertFalse(connector_access_rotation_job["enabled"])
        self.assertEqual(connector_access_rotation_job["cadence"], "daily")
        self.assertIn(
            "connector-access-alerts",
            connector_access_rotation_job["candidate_command"],
        )
        self.assertFalse(connector_access_rotation_job["notifications_sent"])
        self.assertEqual(
            {item["entity_id"] for item in compiled["jurisdiction_rules"]["entities"]},
            {"cn_studio", "sg_publisher"},
        )
        applicability_gate = next(
            item for item in compiled["release_gates"]["automated_gates"]
            if item["gate"] == "tax_applicability_reviews"
        )
        self.assertEqual(
            {item["entity_id"] for item in applicability_gate["required_reviews"]},
            {"cn_studio", "sg_publisher"},
        )
        self.assertTrue(all(
            item["expected"]["decision"] == "approved-in-scope"
            and item["expected"]["applicability_gate_passed"]
            for item in applicability_gate["required_reviews"]
        ))
        applicability_tasks = [
            item for item in compiled["setup_tasks"]
            if item["category"] == "tax_applicability"
        ]
        self.assertEqual(len(applicability_tasks), 2)
        self.assertTrue(all(
            item["fingerprint_bound"]
            and item["preparer_reviewer_separation_required"]
            and "--facts-as-of <YYYY-MM-DD>" in item["workpaper_command"]
            and "--as-of <YYYY-MM-DD>" in item["verify_command"]
            and "tax-applicability-status" in item["registry_status_command"]
            and "tax-applicability-import" in item["registry_import_command"]
            and "tax-applicability-registry-seal" in item["registry_seal_command"]
            and "tax-applicability-registry-verify" in item["registry_verify_command"]
            and item["applicability_review_policy"]["max_age_days"] == 365
            and "tax-applicability-verify" in item["verify_command"]
            for item in applicability_tasks
        ))
        registry_task = next(
            item for item in compiled["setup_tasks"]
            if item["task_id"] == "tax-applicability-registry-activation"
        )
        self.assertIn("tax-applicability-alerts", registry_task["alert_command"])
        self.assertFalse(registry_task["alert_schedule_installed"])
        self.assertFalse(registry_task["notifications_sent"])
        self.assertIn("--as-of <YYYY-MM-DD>", applicability_gate["command"])
        self.assertTrue(applicability_gate["expected"]["calendar_release_allowed"])
        self.assertTrue(compiled["deployment"]["remote_binding_requires_authentication"])
        self.assertTrue(
            compiled["deployment"]["role_policy_supports_operator_reviewer_separation"]
        )
        environment = {
            item["name"]: item
            for item in compiled["deployment_environment_contract"]["environment"]
        }
        self.assertIn("OPC_TAX_APPLICABILITY_REVIEW_DIR", environment)
        connector_shadow_environment = environment[
            "OPC_CONNECTOR_SHADOW_REVIEW_DIR"
        ]
        self.assertTrue(
            connector_shadow_environment[
                "required_for_network_connector_stable_evidence"
            ]
        )
        self.assertIn("mode_0700", connector_shadow_environment["constraints"])
        self.assertIn(
            "schema_v2_real_anonymized_reviews_only",
            connector_shadow_environment["constraints"],
        )
        pilot_environment = environment["OPC_PILOT_READINESS_REVIEW"]
        self.assertTrue(pilot_environment["required_before_bounded_shadow"])
        self.assertEqual(
            pilot_environment["classification"],
            "read_only_private_pilot_review_path",
        )
        self.assertIn("mode_0600", pilot_environment["constraints"])
        self.assertIn("mounted_read_only", pilot_environment["constraints"])
        self.assertIn(
            "does_not_grant_posting_payment_or_filing_authorization",
            pilot_environment["constraints"],
        )
        handoff_environment = environment["OPC_PILOT_DATA_HANDOFF_REVIEW"]
        self.assertTrue(
            handoff_environment["required_before_real_data_intake"]
        )
        self.assertIn("mode_0600", handoff_environment["constraints"])
        self.assertIn("mounted_read_only", handoff_environment["constraints"])
        self.assertIn(
            "requires_matching_OPC_PILOT_READINESS_REVIEW",
            handoff_environment["constraints"],
        )
        shadow_environment = environment[
            "OPC_PILOT_SHADOW_RUN_REGISTRATION"
        ]
        self.assertTrue(
            shadow_environment["required_before_first_shadow_observation"]
        )
        self.assertIn("mode_0600", shadow_environment["constraints"])
        self.assertIn("mounted_read_only", shadow_environment["constraints"])
        self.assertIn(
            "requires_current_pipeline_run_ledger",
            shadow_environment["constraints"],
        )
        receipt_environment = environment[
            "OPC_TAX_APPLICABILITY_REGISTRY_RECEIPT"
        ]
        self.assertTrue(
            receipt_environment["required_when_tax_review_directory_configured"]
        )
        self.assertIn(
            "does_not_grant_filing_authorization",
            receipt_environment["constraints"],
        )
        self.assertNotIn(
            "connector-shadow-registry-activation",
            {item["task_id"] for item in compiled["setup_tasks"]},
        )
        self.assertTrue(any(
            task["task_id"] == "runtime-security:api-access"
            for task in compiled["setup_tasks"]
        ))
        pipeline = next(
            item for item in compiled["pipelines"]
            if item["pipeline_id"] == "game.channel_settlement_close"
        )
        self.assertEqual(pipeline["implementation_status"], "executable")
        self.assertEqual(set(pipeline["required_connectors_any"]), {
            "file.app_store_settlements", "file.google_play_settlements",
            "file.domestic_game_settlements",
        })
        self.assertEqual(pipeline["review_gates"], [
            "channel_contract_mapping", "game_principal_agent_assessment",
        ])
        job = next(
            item for item in compiled["job_plan"]["jobs"]
            if item["job_id"] == "game.channel_settlement_close"
        )
        self.assertEqual(job["candidate_pipelines"], ["game.channel_settlement_close"])
        template = next(
            item for item in compiled["pipeline_request_templates"]["templates"]
            if item["template_id"] == "game.channel_settlement_close:sg_publisher"
        )
        self.assertEqual(template["entity_scope"], "management")
        self.assertEqual(template["request"]["payload"]["entity_id"], "sg_publisher")
        self.assertFalse(template["runnable_without_configuration"])

    def test_dtc_box_compiles_commerce_workflows_only(self):
        compiled = compile_box_file(ROOT / "examples" / "boxes" / "cn_dtc_store.json", PACKS)
        workflow_ids = {workflow["workflow_id"] for workflow in compiled["workflows"]}
        self.assertIn("commerce.order_margin", workflow_ids)
        self.assertNotIn("game.revenue_close", workflow_ids)
        self.assertTrue(any(task["task_id"] == "tax-registration:cn_dtc_company" for task in compiled["setup_tasks"]))
        order_margin = next(
            workflow for workflow in compiled["workflows"]
            if workflow["workflow_id"] == "commerce.order_margin"
        )
        self.assertEqual(order_margin["implementation_status"], "executable")
        pipeline = next(
            item for item in compiled["pipelines"]
            if item["pipeline_id"] == "commerce.channel_close"
        )
        self.assertEqual(pipeline["implementation_status"], "executable")
        self.assertEqual(pipeline["review_gates"], [
            "commerce_source_mapping", "revenue_cutoff", "inventory_valuation_policy",
            "return_disposition_review", "import_landed_cost_policy", "sales_tax_nexus_review",
        ])
        self.assertEqual(set(pipeline["required_services"]), {
            "commerce.order_to_cash", "commerce.refund_summary",
            "commerce.reconcile_return_inventory",
            "commerce.build_import_landed_cost_candidates",
            "commerce.fulfillment_cost_summary", "dtc.destination_evidence",
        })
        job = next(
            item for item in compiled["job_plan"]["jobs"]
            if item["job_id"] == "commerce.channel_close"
        )
        self.assertEqual(job["candidate_pipelines"], ["commerce.channel_close"])
        template = next(
            item for item in compiled["pipeline_request_templates"]["templates"]
            if item["template_id"] == "commerce.channel_close:cn_dtc_company"
        )
        self.assertEqual(template["request"]["payload"]["entity_id"], "cn_dtc_company")
        self.assertFalse(template["runnable_without_configuration"])
        schedule = compiled["pipeline_schedule_template"]
        self.assertTrue(all(item["entity_id"] for item in schedule["jobs"]))
        excluded = {
            item["pipeline_id"]: item for item in schedule["excluded_templates"]
        }
        self.assertEqual(
            excluded["commerce.import_analyze"]["entity_scope"], "management",
        )
        self.assertIn("explicit legal entity", excluded["commerce.import_analyze"]["reason"])

    def test_accounting_close_pipeline_compiles_one_fail_closed_template_per_entity(self):
        compiled = compile_box_file(
            ROOT / "examples" / "boxes" / "global_game_studio.json", PACKS,
        )
        pipeline = next(
            item for item in compiled["pipelines"]
            if item["pipeline_id"] == "finance.accounting_close_review"
        )
        self.assertEqual(pipeline["implementation_status"], "executable")
        self.assertEqual(set(pipeline["required_connectors"]), {
            "file.general_ledger", "file.trial_balance",
        })
        self.assertEqual(pipeline["required_services"], [
            "core.reconcile_accounting_close_exports",
        ])
        templates = [
            item for item in compiled["pipeline_request_templates"]["templates"]
            if item["pipeline_id"] == "finance.accounting_close_review"
        ]
        self.assertEqual({item["entity_id"] for item in templates}, {"cn_studio", "sg_publisher"})
        self.assertTrue(all(not item["runnable_without_configuration"] for item in templates))
        for template in templates:
            payload = template["request"]["payload"]
            self.assertEqual(payload["entity_id"], template["entity_id"])
            self.assertEqual(payload["general_ledger_connector_id"], "file.general_ledger")
            self.assertEqual(payload["trial_balance_connector_id"], "file.trial_balance")
            self.assertEqual(len(payload["account_mappings"]), 1)

    def test_first_close_discovery_compiles_per_entity_without_mapping_guesses(self):
        compiled = compile_box_file(
            ROOT / "examples" / "boxes" / "global_game_studio.json", PACKS,
        )
        pipeline = next(
            item for item in compiled["pipelines"]
            if item["pipeline_id"] == "finance.first_close_discovery"
        )
        self.assertEqual(set(pipeline["required_connectors"]), {
            "file.general_ledger", "file.trial_balance",
        })
        self.assertEqual(set(pipeline["required_connectors_any"]), {
            "file.bank_statement", "wise.balance_statement",
        })
        self.assertEqual(set(pipeline["required_services"]), {
            "core.reconcile_bank_activity", "core.discover_first_close_configuration",
        })
        self.assertIn("bank_gl_account_mapping", {
            item["object_id"] for item in compiled["data_model"]["objects"]
        })
        templates = [
            item for item in compiled["pipeline_request_templates"]["templates"]
            if item["pipeline_id"] == "finance.first_close_discovery"
        ]
        self.assertEqual({item["entity_id"] for item in templates}, {"cn_studio", "sg_publisher"})
        for template in templates:
            payload = template["request"]["payload"]
            self.assertNotIn("account_mappings", payload)
            self.assertNotIn("bank_gl_mappings", payload)
            self.assertEqual(payload["bank_connector_id"], "file.bank_statement")
            self.assertFalse(template["runnable_without_configuration"])

    def test_stripe_box_compiles_evidence_reconciliation_and_briefing_contracts(self):
        compiled = compile_box_file(
            ROOT / "examples" / "boxes" / "cn_dtc_stripe_store.json", PACKS,
        )
        service_ids = {service["service_id"] for service in compiled["services"]}
        self.assertIn("stripe.summarize_balance_activity", service_ids)
        self.assertIn("stripe.reconcile_payouts", service_ids)
        workflow = next(
            item for item in compiled["workflows"]
            if item["workflow_id"] == "payments.stripe_cash_reconciliation"
        )
        self.assertEqual(workflow["implementation_status"], "executable")
        job = next(
            item for item in compiled["job_plan"]["jobs"]
            if item["job_id"] == "payments.stripe_cash_reconciliation"
        )
        self.assertFalse(job["enabled"])
        self.assertEqual(job["candidate_services"], ["stripe.reconcile_payouts"])
        self.assertEqual(job["candidate_pipelines"], ["stripe.daily_close"])
        pipeline = next(
            item for item in compiled["pipelines"] if item["pipeline_id"] == "stripe.daily_close"
        )
        self.assertEqual(pipeline["implementation_status"], "executable")
        self.assertFalse(pipeline["external_actions"])
        self.assertEqual(pipeline["review_gates"], ["stripe_mapping_approval"])
        self.assertIn("stripe_payout", {
            item["object_id"] for item in compiled["data_model"]["objects"]
        })
        self.assertIn("finance.stripe_review", {
            item["contract_id"] for item in compiled["agent_contracts"]["contracts"]
        })
        self.assertIn("stripe_cash_reconciliation", {
            item["panel_id"] for item in compiled["dashboard_layout"]["panels"]
        })

    def test_marketplace_box_compiles_contract_receivable_and_inventory_close(self):
        compiled = compile_box_file(
            ROOT / "examples" / "boxes" / "cn_marketplace_store.json", PACKS,
        )
        pipeline = next(
            item for item in compiled["pipelines"]
            if item["pipeline_id"] == "marketplace.channel_close"
        )
        self.assertEqual(pipeline["implementation_status"], "executable")
        self.assertEqual(set(pipeline["required_connectors_any"]), {
            "file.marketplace_commerce", "example.marketplace_api_payload",
        })
        self.assertEqual(set(pipeline["required_services"]), {
            "marketplace.reconcile_fees", "marketplace.reconcile_receivable",
            "commerce.reconcile_return_inventory",
            "commerce.build_import_landed_cost_candidates",
            "marketplace.reconcile_inventory",
        })
        self.assertEqual(pipeline["review_gates"], [
            "commerce_source_mapping", "marketplace_contract_mapping",
            "marketplace_inventory_mapping", "revenue_cutoff", "inventory_valuation_policy",
            "return_disposition_review", "import_landed_cost_policy",
        ])
        job = next(
            item for item in compiled["job_plan"]["jobs"]
            if item["job_id"] == "marketplace.channel_close"
        )
        self.assertEqual(job["candidate_pipelines"], ["marketplace.channel_close"])
        template = next(
            item for item in compiled["pipeline_request_templates"]["templates"]
            if item["template_id"] == "marketplace.channel_close:cn_marketplace_company"
        )
        self.assertFalse(template["runnable_without_configuration"])
        self.assertEqual(
            template["request"]["payload"]["platform_inventory"][0]["entity_id"],
            "cn_marketplace_company",
        )
        self.assertNotIn("commerce.channel_close", {
            item["pipeline_id"] for item in compiled["pipelines"]
        })
        self.assertNotIn("commerce.import_analyze", {
            item["pipeline_id"] for item in compiled["pipelines"]
        })

    def test_shopify_stripe_box_compiles_complete_order_to_cash_contract(self):
        compiled = compile_box_file(
            ROOT / "examples" / "boxes" / "cn_dtc_shopify_stripe_store.json", PACKS,
        )
        pipeline = next(
            item for item in compiled["pipelines"]
            if item["pipeline_id"] == "dtc.shopify_stripe_daily_close"
        )
        self.assertEqual(pipeline["implementation_status"], "executable")
        self.assertEqual(set(pipeline["required_connectors"]), {
            "shopify.orders", "stripe.balance_transactions", "stripe.payouts",
        })
        self.assertFalse(pipeline["external_actions"])
        self.assertEqual(set(pipeline["review_gates"]), {
            "shopify_mapping_approval", "processor_link_mapping_approval",
            "stripe_mapping_approval",
        })
        policy = compiled["pipeline_run_policy"]
        self.assertEqual(policy["persistence"], "explicit_opt_in")
        self.assertFalse(policy["release_candidate_is_external_authorization"])
        combined_policy = next(
            item for item in policy["pipelines"]
            if item["pipeline_id"] == "dtc.shopify_stripe_daily_close"
        )
        self.assertTrue(combined_policy["recordable"])
        self.assertEqual(set(combined_policy["required_review_gates"]), set(pipeline["review_gates"]))
        self.assertIn("PIPELINE_SCHEDULE_CLAIMED", policy["event_types"])
        schedule = compiled["pipeline_schedule_template"]
        scheduled_job = next(
            item for item in schedule["jobs"]
            if item["job_id"] == "dtc.shopify_stripe_daily_close:cn_dtc_company"
        )
        self.assertFalse(scheduled_job["enabled"])
        self.assertIsNone(scheduled_job["request_fingerprint"])
        self.assertIsNone(scheduled_job["approved_by"])
        self.assertEqual(schedule["schema_version"], 2)
        self.assertTrue(
            schedule["execution_contract"]["approval_is_bound_to_request_content_fingerprint"]
        )
        self.assertTrue(schedule["execution_contract"]["atomic_occurrence_lease"])
        self.assertFalse(schedule["execution_contract"]["external_actions_performed"])
        self.assertIn("pipeline-observability", schedule["cli"]["observability"])
        self.assertEqual(
            compiled["runtime_security_policy"]["route_classes"]["pipeline_observability_export"],
            "reader",
        )
        workflow = next(
            item for item in compiled["workflows"]
            if item["workflow_id"] == "commerce.shopify_stripe_order_to_cash"
        )
        self.assertEqual(workflow["implementation_status"], "executable")
        job = next(
            item for item in compiled["job_plan"]["jobs"]
            if item["job_id"] == "commerce.shopify_stripe_order_to_cash"
        )
        self.assertFalse(job["enabled"])
        self.assertEqual(job["candidate_pipelines"], ["dtc.shopify_stripe_daily_close"])
        object_ids = {item["object_id"] for item in compiled["data_model"]["objects"]}
        self.assertTrue({
            "shopify_order_evidence", "shopify_financial_transaction", "processor_evidence_link",
        } <= object_ids)
        self.assertIn("finance.shopify_stripe_review", {
            item["contract_id"] for item in compiled["agent_contracts"]["contracts"]
        })
        self.assertIn("shopify_stripe_order_to_cash", {
            item["panel_id"] for item in compiled["dashboard_layout"]["panels"]
        })
        combined_template = next(
            item for item in compiled["pipeline_request_templates"]["templates"]
            if item["template_id"] == "dtc.shopify_stripe_daily_close:cn_dtc_company"
        )
        self.assertFalse(combined_template["runnable_without_configuration"])
        self.assertEqual(
            combined_template["request"]["payload"]["entity_id"], "cn_dtc_company",
        )
        serialized_template = __import__("json").dumps(combined_template)
        self.assertNotIn("shpat_", serialized_template)
        self.assertNotIn("rk_", serialized_template)
        monthly = next(
            item for item in compiled["pipelines"]
            if item["pipeline_id"] == "dtc.shopify_stripe_month_close"
        )
        self.assertEqual(monthly["implementation_status"], "executable")
        self.assertEqual(set(monthly["required_connectors"]), {
            "shopify.monthly_order_evidence", "stripe.balance_transactions",
        })
        self.assertIn("tax_inclusive_policy_confirmed", monthly["review_gates"])
        monthly_workflow = next(
            item for item in compiled["workflows"]
            if item["workflow_id"] == "commerce.shopify_stripe_monthly_metrics"
        )
        self.assertEqual(monthly_workflow["cadence"], "monthly")
        monthly_job = next(
            item for item in compiled["job_plan"]["jobs"]
            if item["job_id"] == "commerce.shopify_stripe_monthly_metrics"
        )
        self.assertEqual(
            monthly_job["candidate_pipelines"], ["dtc.shopify_stripe_month_close"],
        )
        monthly_template = next(
            item for item in compiled["pipeline_request_templates"]["templates"]
            if item["template_id"] == "dtc.shopify_stripe_month_close:cn_dtc_company"
        )
        payload = monthly_template["request"]["payload"]
        self.assertEqual(
            payload["shopify_monthly_request"]["interval_start"],
            "REPLACE_WITH_MONTH_START_UTC",
        )
        self.assertEqual(
            payload["stripe_balance_request"]["created_gte"],
            "REPLACE_WITH_SAME_MONTH_START_UNIX_TIMESTAMP",
        )
        self.assertFalse(monthly_template["runnable_without_configuration"])

    def test_us_dtc_box_keeps_executable_commerce_separate_from_design_tax_readiness(self):
        compiled = compile_box_file(
            ROOT / "examples" / "boxes" / "us_dtc_shopify_stripe_c_corp.json", PACKS,
        )
        entity = compiled["entities"][0]
        self.assertEqual(entity["tax_pack"], "jurisdiction.us_federal")
        self.assertEqual(entity["tax_readiness"], "design")
        self.assertFalse(compiled["deployment"]["ready_for_external_filing"])
        service_ids = {
            item["service_id"] for item in compiled["services"]
            if item["pack_id"] == "jurisdiction.us_federal"
        }
        self.assertEqual(service_ids, {
            "tax.us_federal.registration_profile",
            "tax.us_federal.evidence_checklist",
            "tax.us_federal.build_calendar",
        })
        workflow = next(
            item for item in compiled["workflows"]
            if item["workflow_id"] == "tax.us_federal.c_corp_calendar"
        )
        self.assertEqual(workflow["implementation_status"], "executable")
        combined = next(
            item for item in compiled["pipelines"]
            if item["pipeline_id"] == "dtc.shopify_stripe_daily_close"
        )
        self.assertEqual(combined["implementation_status"], "executable")

    def test_hk_dtc_box_compiles_design_tax_services_and_manual_calendar(self):
        compiled = compile_box_file(
            ROOT / "examples" / "boxes" / "hk_dtc_shopify_stripe_corporation.json", PACKS,
        )
        entity = compiled["entities"][0]
        self.assertEqual(entity["tax_pack"], "jurisdiction.hk")
        self.assertEqual(entity["tax_readiness"], "design")
        self.assertFalse(compiled["deployment"]["ready_for_external_filing"])
        service_ids = {
            item["service_id"] for item in compiled["services"]
            if item["pack_id"] == "jurisdiction.hk"
        }
        self.assertEqual(service_ids, {
            "tax.hk.registration_profile", "tax.hk.evidence_checklist",
            "tax.hk.build_calendar",
        })
        workflow = next(
            item for item in compiled["workflows"]
            if item["workflow_id"] == "tax.hk.corporation_calendar"
        )
        self.assertEqual(workflow["implementation_status"], "executable")
        rules = compiled["jurisdiction_rules"]["entities"][0]
        calendar_rules = [
            rule for rule in rules["rules"] if rule["automation_level"] == "calendar"
        ]
        self.assertEqual(len(calendar_rules), 2)
        self.assertTrue(all(
            rule["schedule"]["kind"] == "manual_configuration"
            for rule in calendar_rules
        ))

    def test_compilation_is_reproducible_for_same_sources(self):
        first = compile_box_file(ROOT / "examples" / "boxes" / "cn_dtc_store.json", PACKS)
        second = compile_box_file(ROOT / "examples" / "boxes" / "cn_dtc_store.json", PACKS)
        self.assertEqual(first, second)

    def test_compilation_includes_business_specific_cfo_control_overlay(self):
        cases = (
            (
                "global_game_studio.json", "game_studio",
                "platform_settlement_completeness",
            ),
            (
                "cn_dtc_store.json", "dtc_store",
                "order_payment_refund_reconciliation",
            ),
            (
                "cn_marketplace_store.json", "marketplace_seller",
                "order_finance_inventory_three_way_scope",
            ),
        )
        for filename, model, objective in cases:
            compiled = compile_box_file(
                ROOT / "examples" / "boxes" / filename, PACKS,
            )
            overlay = compiled["cfo_control_overlay"]
            self.assertEqual(overlay["business_model_type_ids"], [model])
            self.assertIn(
                objective, overlay["monthly_control_objective_type_ids"],
            )
            self.assertEqual(
                overlay["runtime_fingerprint"],
                compiled["lock"]["runtime_fingerprint"],
            )
            self.assertFalse(overlay["financial_values_returned"])
            self.assertFalse(overlay["posting_payment_or_filing_authorized"])

    def test_compilation_includes_business_specific_cfo_metric_catalog(self):
        cases = (
            ("global_game_studio.json", "game_studio", "game_platform_net_revenue"),
            ("cn_dtc_store.json", "dtc_store", "dtc_inventory_days_on_hand"),
            (
                "cn_marketplace_store.json", "marketplace_seller",
                "marketplace_three_way_scope_match_rate",
            ),
        )
        for filename, model, metric_type_id in cases:
            compiled = compile_box_file(
                ROOT / "examples" / "boxes" / filename, PACKS,
            )
            catalog = compiled["cfo_metric_catalog"]
            metric_ids = {
                item["metric_type_id"]
                for item in catalog["metric_definitions"]
            }
            self.assertEqual(catalog["business_model_type_ids"], [model])
            self.assertIn("cash_runway_months", metric_ids)
            self.assertIn(metric_type_id, metric_ids)
            self.assertEqual(
                catalog["runtime_fingerprint"],
                compiled["lock"]["runtime_fingerprint"],
            )
            self.assertFalse(catalog["metric_values_returned"])
            self.assertFalse(catalog["formula_evaluated"])
            self.assertGreater(catalog["source_mapping_count"], 0)
            self.assertTrue(catalog["trusted_source_operand_assembly_available"])
            self.assertEqual(
                catalog["evaluation_contract"]["service_id"],
                "core.evaluate_cfo_metrics",
            )
            service_ids = {
                item["service_id"] for item in compiled["services"]
            }
            self.assertIn("core.evaluate_cfo_metrics", service_ids)

    def test_writer_creates_complete_editable_box_bundle(self):
        compiled = compile_box_file(ROOT / "examples" / "boxes" / "cn_dtc_store.json", PACKS)
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = write_compiled_box(compiled, temp_dir)
            self.assertEqual({path.name for path in paths}, {
                "box.lock.json", "setup-checklist.json", "service-catalog.json",
                "connector-catalog.json", "pipeline-catalog.json", "pipeline-request-templates.json",
                "pipeline-run-policy.json",
                "pipeline-schedule-template.json",
                "runtime-security-policy.json",
                "deployment-environment-contract.json",
                "runtime-data-contract.json",
                "connector-sync-policy.json",
                "workflow-plan.json", "job-plan.json",
                "data-model.json", "dashboard-layout.json", "agent-contracts.json",
                "cfo-control-overlay.json", "cfo-metric-catalog.json",
                "cfo-metric-evaluation-request.schema.json",
                "cfo-metric-operand-assembly.schema.json",
                "agent-prompts.md", "jurisdiction-rules.json",
                "tax-applicability-questionnaire.json",
                "tax-applicability-artifact.schema.json",
                "tax-applicability-artifact-security-policy.json", "README.md",
                "tax-applicability-registry-receipt.schema.json",
                "upgrade-policy.json",
                "release-gates.json",
                "stable-promotion-policy.json",
                "stable-promotion-evidence-templates.json",
                "stable-promotion-evidence.schema.json",
                "pilot-readiness-plan.json",
                "production-readiness-plan.json",
                "pilot-readiness-artifact.schema.json",
                "pilot-data-handoff-plan.json",
                "pilot-data-handoff-artifact.schema.json",
                "pilot-shadow-run-registration.schema.json",
                "pilot-shadow-observation-artifact.schema.json",
                "pilot-shadow-series-artifact.schema.json",
                "skill-catalog.json",
            })
            self.assertIn("中国主体独立站 OPC 样板", (Path(temp_dir) / "README.md").read_text(encoding="utf-8"))
            jobs = __import__("json").loads((Path(temp_dir) / "job-plan.json").read_text(encoding="utf-8"))
            self.assertEqual(jobs["installation_status"], "not_installed")
            templates = __import__("json").loads(
                (Path(temp_dir) / "pipeline-request-templates.json").read_text(encoding="utf-8")
            )
            self.assertFalse(templates["secret_values_included"])
            self.assertTrue(all(
                not item["runnable_without_configuration"] for item in templates["templates"]
            ))
            applicability = __import__("json").loads(
                (Path(temp_dir) / "tax-applicability-questionnaire.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(applicability["template_only"])
            self.assertFalse(applicability["raw_tax_identifiers_requested"])
            self.assertEqual(len(applicability["entities"]), 1)
            self.assertEqual(applicability["entities"][0]["question_count"], 5)
            self.assertTrue(all(
                question["answer"] is None
                and question["human_review_required"]
                and not question["system_determination_performed"]
                for question in applicability["entities"][0]["questions"]
            ))
            applicability_schema = __import__("json").loads(
                (Path(temp_dir) / "tax-applicability-artifact.schema.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                applicability_schema["$id"],
                "https://opc.finance/schemas/tax-applicability-artifact.schema.json",
            )
            self.assertEqual(len(applicability_schema["oneOf"]), 2)
            self.assertIn(
                "facts_as_of", applicability_schema["$defs"]["common"]["required"],
            )
            self.assertIn(
                "applicability_review_policy",
                applicability_schema["$defs"]["entity"]["required"],
            )
            self.assertIn(
                "expires_at", applicability_schema["$defs"]["reviewed"]["allOf"][1][
                    "required"
                ],
            )
            self.assertFalse(
                applicability_schema["$defs"]["question"]["additionalProperties"]
            )
            artifact_security = __import__("json").loads(
                (
                    Path(temp_dir)
                    / "tax-applicability-artifact-security-policy.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(artifact_security["write_policy"]["posix_mode"], "0600")
            self.assertFalse(
                artifact_security["read_policy"]["symbolic_links_allowed"]
            )
            self.assertFalse(
                artifact_security["read_policy"][
                    "posix_group_or_other_permissions_allowed"
                ]
            )
            self.assertTrue(
                artifact_security["registry_policy"][
                    "unexpected_entries_close_release_gate"
                ]
            )
            self.assertEqual(
                artifact_security["registry_policy"]["controlled_import_command"],
                "tax-applicability-import",
            )
            self.assertFalse(
                artifact_security["registry_policy"]["overwrite_allowed"]
            )
            self.assertTrue(
                artifact_security["registry_policy"][
                    "activation_receipt_required_for_runtime_release"
                ]
            )
            receipt_schema = __import__("json").loads(
                (
                    Path(temp_dir)
                    / "tax-applicability-registry-receipt.schema.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                receipt_schema["$id"],
                "https://opc.finance/schemas/"
                "tax-applicability-registry-receipt.schema.json",
            )
            self.assertIn(
                "controller_role_separation_verified", receipt_schema["required"]
            )
            pilot_plan = __import__("json").loads(
                (Path(temp_dir) / "pilot-readiness-plan.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertIn(
                "orders",
                {item["domain"] for item in pilot_plan["data_domain_requirements"]},
            )
            self.assertFalse(
                pilot_plan["control_boundary"]["credential_values_requested"]
            )
            self.assertEqual(
                pilot_plan["review_policy"]["expiry_effect"],
                "block_new_bounded_shadow_runs",
            )
            self.assertLess(
                pilot_plan["review_policy"]["review_due_after_days"],
                pilot_plan["review_policy"]["expires_after_days"],
            )
            production_plan = __import__("json").loads(
                (Path(temp_dir) / "production-readiness-plan.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                production_plan["artifact_type"], "production_readiness_plan",
            )
            self.assertEqual(len(production_plan["stages"]), 11)
            initialization = production_plan["first_customer_workspace"]
            self.assertEqual(
                initialization["artifact_type"],
                "first_customer_activation_workspace_contract",
            )
            self.assertIn("activation-init", initialization["init_command"])
            self.assertIn("mode_0700", initialization["root_constraints"])
            self.assertFalse(initialization["review_artifacts_created"])
            self.assertFalse(initialization["connector_baselines_created"])
            self.assertFalse(initialization["credentials_accepted"])
            self.assertFalse(initialization["financial_source_files_copied"])
            self.assertFalse(initialization["commands_executed"])
            self.assertFalse(initialization["external_actions_performed"])
            self.assertEqual(
                production_plan["stages"][2]["operator_contract"]["stage_id"],
                "tax_applicability",
            )
            self.assertIn(
                "tax-applicability-init",
                production_plan["stages"][2]["operator_contract"]["commands"][0],
            )
            self.assertTrue(all(
                stage["operator_contract"]["command_templates_only"]
                and not stage["operator_contract"]["external_actions_performed"]
                for stage in production_plan["stages"]
            ))
            self.assertFalse(
                production_plan["pack_contracts"]["stable_release_ready"]
            )
            self.assertFalse(
                production_plan["control_boundary"]["external_filing_authorized"]
            )
            pilot_schema = __import__("json").loads(
                (Path(temp_dir) / "pilot-readiness-artifact.schema.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                pilot_schema["$id"],
                "https://opc.finance/schemas/pilot-readiness-artifact.schema.json",
            )
            self.assertIn(
                "review_due_at", pilot_schema["$defs"]["review"]["required"],
            )
            self.assertIn(
                "expires_at", pilot_schema["$defs"]["review"]["required"],
            )
            handoff_plan = __import__("json").loads(
                (Path(temp_dir) / "pilot-data-handoff-plan.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(handoff_plan["entity_ids"], ["cn_dtc_company"])
            self.assertFalse(
                handoff_plan["control_boundary"]["raw_files_copied_by_manifest"]
            )
            handoff_schema = __import__("json").loads(
                (Path(temp_dir) / "pilot-data-handoff-artifact.schema.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                handoff_schema["$id"],
                "https://opc.finance/schemas/pilot-data-handoff-artifact.schema.json",
            )
            shadow_registration_schema = __import__("json").loads(
                (
                    Path(temp_dir)
                    / "pilot-shadow-run-registration.schema.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                shadow_registration_schema["$id"],
                "https://opc.finance/schemas/pilot-shadow-run-registration.schema.json",
            )
            self.assertFalse(
                shadow_registration_schema["properties"]["posting_authorized"]["const"]
            )
            self.assertFalse(
                shadow_registration_schema["properties"]["external_filing_authorized"]["const"]
            )
            shadow_observation_schema = __import__("json").loads(
                (
                    Path(temp_dir)
                    / "pilot-shadow-observation-artifact.schema.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                shadow_observation_schema["$id"],
                "https://opc.finance/schemas/pilot-shadow-observation-artifact.schema.json",
            )
            self.assertFalse(
                shadow_observation_schema["properties"]["posting_performed"]["const"]
            )
            shadow_series_schema = __import__("json").loads(
                (
                    Path(temp_dir)
                    / "pilot-shadow-series-artifact.schema.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                shadow_series_schema["$id"],
                "https://opc.finance/schemas/pilot-shadow-series-artifact.schema.json",
            )
            self.assertEqual(
                shadow_series_schema["properties"]["minimum_consecutive_periods"][
                    "const"
                ],
                2,
            )
            self.assertFalse(
                shadow_series_schema["properties"]["posting_performed"]["const"]
            )
            pilot_task = next(
                item for item in compiled["setup_tasks"]
                if item["task_id"] == "first-company-pilot-readiness"
            )
            self.assertFalse(pilot_task["ready_for_statutory_release"])
            self.assertFalse(pilot_task["external_actions_authorized"])
            handoff_task = next(
                item for item in compiled["setup_tasks"]
                if item["task_id"] == "first-company-controlled-data-handoff"
            )
            self.assertFalse(handoff_task["raw_files_copied"])
            self.assertFalse(handoff_task["data_import_performed"])
            self.assertFalse(handoff_task["external_actions_authorized"])
            shadow_registration_task = next(
                item for item in compiled["setup_tasks"]
                if item["task_id"] == "first-company-shadow-run-registration"
            )
            self.assertTrue(
                shadow_registration_task["exact_entity_coverage_required"]
            )
            self.assertTrue(
                shadow_registration_task["all_review_gates_approved_required"]
            )
            self.assertFalse(
                shadow_registration_task["financial_values_persisted"]
            )
            self.assertFalse(
                shadow_registration_task["external_actions_authorized"]
            )
            shadow_observation_task = next(
                item for item in compiled["setup_tasks"]
                if item["task_id"] == "first-company-shadow-observation-review"
            )
            self.assertTrue(
                shadow_observation_task["fourth_role_separation_required"]
            )
            self.assertTrue(
                shadow_observation_task["system_defect_blocks_next_shadow_period"]
            )
            self.assertFalse(
                shadow_observation_task["raw_financial_values_persisted"]
            )
            self.assertFalse(
                shadow_observation_task["ready_for_stable_promotion"]
            )
            run_policy = __import__("json").loads(
                (Path(temp_dir) / "pipeline-run-policy.json").read_text(encoding="utf-8")
            )
            self.assertEqual(run_policy["ledger"]["integrity"], "sha256_hash_chain")
            self.assertFalse(run_policy["external_actions_performed"])
            schedule = __import__("json").loads(
                (Path(temp_dir) / "pipeline-schedule-template.json").read_text(encoding="utf-8")
            )
            self.assertTrue(schedule["template_only"])
            self.assertEqual(schedule["installation_status"], "not_installed")
            self.assertTrue(all(not item["enabled"] for item in schedule["jobs"]))
            security_policy = __import__("json").loads(
                (Path(temp_dir) / "runtime-security-policy.json").read_text(encoding="utf-8")
            )
            self.assertFalse(security_policy["role_policy"]["operator_includes_reviewer"])
            self.assertEqual(security_policy["authenticated_actor_source"], "principal_id")
            deployment = __import__("json").loads(
                (Path(temp_dir) / "deployment-environment-contract.json").read_text(encoding="utf-8")
            )
            self.assertFalse(deployment["process"]["run_as_root"])
            self.assertFalse(deployment["secret_values_included"])
            self.assertEqual(
                deployment["connector_secret_environment_names"],
                sorted(deployment["connector_secret_environment_names"]),
            )
            environment = {item["name"]: item for item in deployment["environment"]}
            self.assertTrue(all(
                environment[name]["secret"]
                for name in deployment["connector_secret_environment_names"]
            ))
            self.assertTrue(
                environment["OPC_PILOT_READINESS_REVIEW"][
                    "required_before_bounded_shadow"
                ]
            )
            self.assertIn(
                "optional_pilot_readiness_review",
                deployment["filesystem"]["read_only_mounts"],
            )
            self.assertIn(
                "optional_connector_shadow_review_directory",
                deployment["filesystem"]["read_only_mounts"],
            )
            self.assertIn(
                "/api/box/connector-shadow",
                deployment["health"]["readiness_paths"],
            )
            self.assertIn(
                "optional_pilot_data_handoff_review",
                deployment["filesystem"]["read_only_mounts"],
            )
            self.assertIn(
                "optional_pilot_shadow_run_registration",
                deployment["filesystem"]["read_only_mounts"],
            )
            self.assertTrue(
                environment["OPC_PILOT_SHADOW_OBSERVATION_REVIEW"][
                    "required_before_next_shadow_period"
                ]
            )
            self.assertIn(
                "optional_pilot_shadow_entity_report_directory",
                deployment["filesystem"]["read_only_mounts"],
            )
            self.assertTrue(
                environment["OPC_PILOT_SHADOW_SERIES_REVIEW"][
                    "required_before_stable_promotion_evidence_preparation"
                ]
            )
            self.assertTrue(
                environment["OPC_STABLE_PROMOTION_ROOT"][
                    "required_to_project_stable_candidate_approvals"
                ]
            )
            self.assertIn(
                "optional_pilot_shadow_series_evidence_directory",
                deployment["filesystem"]["read_only_mounts"],
            )
            self.assertIn(
                "optional_read_only_activation_workspace_root",
                deployment["filesystem"]["read_only_mounts"],
            )
            self.assertIn(
                "optional_stable_promotion_ledger_directory",
                deployment["filesystem"]["read_only_mounts"],
            )
            self.assertIn(
                "/api/box/pilot-shadow-observation",
                deployment["health"]["readiness_paths"],
            )
            self.assertIn(
                "/api/box/pilot-shadow-series",
                deployment["health"]["readiness_paths"],
            )
            self.assertIn(
                "/api/box/pilot-shadow-periods",
                deployment["health"]["readiness_paths"],
            )
            runtime_data = __import__("json").loads(
                (Path(temp_dir) / "runtime-data-contract.json").read_text(encoding="utf-8")
            )
            self.assertEqual(runtime_data["layout"]["current_version"], 3)
            self.assertIn("release_promotion", runtime_data["layout"]["stores"])
            self.assertTrue(runtime_data["backup"]["service_stopped_confirmation_required"])
            self.assertTrue(runtime_data["restore"]["target_must_not_exist"])
            self.assertFalse(runtime_data["restore"]["http_restore_enabled"])
            sync_policy = __import__("json").loads(
                (Path(temp_dir) / "connector-sync-policy.json").read_text(encoding="utf-8")
            )
            self.assertEqual(sync_policy["incremental_connectors"], [])
            self.assertFalse(sync_policy["checkpoint_commit"]["automatic"])
            self.assertFalse(sync_policy["backfill"]["automatically_advances_incremental_checkpoint"])
            self.assertFalse(sync_policy["quarantine"]["raw_request_or_response_stored"])
            promotion = __import__("json").loads(
                (Path(temp_dir) / "stable-promotion-policy.json").read_text(encoding="utf-8")
            )
            self.assertEqual(promotion["approval_effect"], "stable_candidate_only")
            self.assertEqual(
                promotion["minimum_controls"]["shadow_close"][
                    "minimum_distinct_periods"
                ],
                2,
            )
            self.assertTrue(
                promotion["minimum_controls"]["consecutive_pilot_shadow_series"][
                    "exact_report_and_portfolio_content_binding_required"
                ]
            )
            self.assertIn(
                "shadow_continuity_reviewer",
                promotion["minimum_controls"]["required_role_separation"],
            )
            connector_shadow_control = promotion["minimum_controls"][
                "network_connector_shadow"
            ]
            self.assertEqual(connector_shadow_control["minimum_schema_version"], 2)
            self.assertEqual(
                connector_shadow_control["required_sample_classification"],
                "real_anonymized",
            )
            self.assertFalse(
                connector_shadow_control["legacy_or_demo_baseline_allowed"]
            )
            self.assertEqual(promotion["shadow_close_artifacts"]["commands"], [
                "shadow-close-template", "shadow-close-compare", "shadow-close-review",
                "shadow-close-verify", "shadow-close-portfolio-assemble",
                "shadow-close-portfolio-review", "shadow-close-portfolio-verify",
            ])
            self.assertEqual(promotion["shadow_close_artifacts"]["report_file_mode"], "0600")
            self.assertFalse(promotion["shadow_close_artifacts"]["overwrite_allowed"])
            self.assertFalse(
                promotion["shadow_close_artifacts"]["raw_financial_values_returned_to_stdout"]
            )
            self.assertFalse(
                promotion["shadow_close_artifacts"]
                ["portfolio_manifest_persists_raw_financial_values"]
            )
            self.assertTrue(
                promotion["shadow_close_artifacts"]
                ["portfolio_review_separate_from_entity_reviewers"]
            )
            connector_shadow = promotion["connector_shadow_artifacts"]
            self.assertEqual(connector_shadow["minimum_promotion_schema_version"], 2)
            self.assertTrue(connector_shadow["source_independence_attestation_required"])
            self.assertTrue(connector_shadow["anonymization_attestation_required"])
            self.assertFalse(
                connector_shadow["legacy_or_demo_artifacts_are_promotion_evidence"]
            )
            self.assertFalse(connector_shadow["raw_financial_values_persisted"])
            self.assertTrue(
                connector_shadow[
                    "observation_binds_complete_private_pipeline_result_sha256"
                ]
            )
            self.assertTrue(
                connector_shadow[
                    "independent_private_source_evidence_required_separately"
                ]
            )
            self.assertIn(
                "xero-shadow-observe",
                connector_shadow["xero_observation_command"],
            )
            self.assertIn(
                "xero-shadow-request-init",
                connector_shadow["xero_request_init_command"],
            )
            self.assertIn(
                "xero-shadow-request-verify",
                connector_shadow["xero_request_verify_command"],
            )
            self.assertIn(
                "wise-shadow-observe",
                connector_shadow["wise_observation_command"],
            )
            self.assertIn(
                "wise-shadow-request-init",
                connector_shadow["wise_request_init_command"],
            )
            self.assertIn(
                "wise-shadow-request-verify",
                connector_shadow["wise_request_verify_command"],
            )
            self.assertIn(
                "paypal-shadow-observe",
                connector_shadow["paypal_observation_command"],
            )
            self.assertIn(
                "paypal-shadow-request-init",
                connector_shadow["paypal_request_init_command"],
            )
            self.assertIn(
                "paypal-shadow-request-verify",
                connector_shadow["paypal_request_verify_command"],
            )
            self.assertIn(
                "woocommerce-shadow-observe",
                connector_shadow["woocommerce_observation_command"],
            )
            self.assertIn(
                "woocommerce-shadow-request-init",
                connector_shadow["woocommerce_request_init_command"],
            )
            self.assertIn(
                "woocommerce-shadow-request-verify",
                connector_shadow["woocommerce_request_verify_command"],
            )
            self.assertIn(
                "shipbob-shadow-observe",
                connector_shadow["shipbob_observation_command"],
            )
            self.assertIn(
                "shipbob-shadow-request-init",
                connector_shadow["shipbob_request_init_command"],
            )
            self.assertIn(
                "shipbob-shadow-request-verify",
                connector_shadow["shipbob_request_verify_command"],
            )
            self.assertIn(
                "amazon-seller-shadow-observe",
                connector_shadow["amazon_seller_observation_command"],
            )
            self.assertIn(
                "amazon-seller-shadow-request-init",
                connector_shadow["amazon_seller_request_init_command"],
            )
            self.assertIn(
                "amazon-seller-shadow-request-verify",
                connector_shadow["amazon_seller_request_verify_command"],
            )
            self.assertIn(
                "shopify-monthly-shadow-request-init",
                connector_shadow["shopify_monthly_request_init_command"],
            )
            self.assertIn(
                "shopify-monthly-shadow-observe",
                connector_shadow["shopify_monthly_observation_command"],
            )
            self.assertIn(
                "stripe-shadow-request-init",
                connector_shadow["stripe_request_init_command"],
            )
            self.assertIn(
                "stripe-shadow-request-verify",
                connector_shadow["stripe_request_verify_command"],
            )
            self.assertIn(
                "stripe-shadow-observe",
                connector_shadow["stripe_observation_command"],
            )
            access_probe = connector_shadow["provider_access_probe"]
            self.assertEqual(
                access_probe["supported_packs"],
                [
                    "connector.shopify", "connector.stripe",
                    "connector.wise", "connector.xero",
                    "connector.paypal", "connector.woocommerce",
                    "connector.shipbob", "connector.amazon_seller",
                ],
            )
            self.assertIn(
                "connector-access-probe", access_probe["probe_command"],
            )
            self.assertIn("--output", access_probe["probe_command"])
            self.assertIn(
                "connector-access-receipt-verify",
                access_probe["receipt_verify_command"],
            )
            self.assertEqual(access_probe["receipt_maximum_age_days"], 30)
            self.assertFalse(access_probe["receipt_is_digital_signature"])
            self.assertTrue(
                access_probe["current_receipt_required_for_shopify_and_stripe_shadow"]
            )
            self.assertTrue(
                access_probe["current_receipt_required_for_supported_pack_shadow"]
            )
            self.assertEqual(
                access_probe["multi_environment_credential_group_receipt_schema"],
                2,
            )
            self.assertTrue(
                access_probe[
                    "entity_credential_alias_binding_required_for_paypal_woocommerce_shipbob_and_amazon_seller"
                ]
            )
            self.assertTrue(
                access_probe["paypal_balance_values_requested_but_not_retained"]
            )
            self.assertFalse(
                access_probe["woocommerce_write_permission_provider_verified"]
            )
            self.assertTrue(access_probe["shipbob_exact_read_scope_set_required"])
            self.assertTrue(
                access_probe[
                    "amazon_seller_financial_values_requested_but_not_retained"
                ]
            )
            self.assertFalse(access_probe["amazon_seller_id_provider_verified"])
            self.assertIn(
                "--access-receipt", connector_shadow["wise_observation_command"],
            )
            self.assertIn(
                "--access-receipt", connector_shadow["xero_observation_command"],
            )
            self.assertIn(
                "--access-receipt", connector_shadow["paypal_observation_command"],
            )
            self.assertIn(
                "--access-receipt", connector_shadow["woocommerce_observation_command"],
            )
            self.assertIn(
                "--access-receipt", connector_shadow["shipbob_observation_command"],
            )
            self.assertIn(
                "--access-receipt",
                connector_shadow["amazon_seller_observation_command"],
            )
            self.assertTrue(access_probe["operator_network_opt_in_required"])
            self.assertFalse(access_probe["browser_initiation_allowed"])
            self.assertFalse(access_probe["provider_account_identifiers_returned"])
            self.assertFalse(access_probe["shadow_dispatch_authorized"])
            self.assertFalse(connector_shadow["external_actions_performed"])
            self.assertFalse(promotion["pack_manifest_changed_automatically"])
            promotion_templates = __import__("json").loads(
                (Path(temp_dir) / "stable-promotion-evidence-templates.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(promotion_templates["template_only"])
            self.assertFalse(promotion_templates["assessment_ready"])
            self.assertEqual(
                promotion_templates["runtime_fingerprint"],
                compiled["lock"]["runtime_fingerprint"],
            )
            self.assertTrue(promotion_templates["templates"])
            self.assertTrue(all(
                item["evidence"]["runtime_fingerprint"]
                == compiled["lock"]["runtime_fingerprint"]
                for item in promotion_templates["templates"]
            ))
            promotion_schema = __import__("json").loads(
                (Path(temp_dir) / "stable-promotion-evidence.schema.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertFalse(promotion_schema["additionalProperties"])
            prompts = (Path(temp_dir) / "agent-prompts.md").read_text(encoding="utf-8")
            self.assertIn("finance.commerce_review", prompts)
            self.assertIn("不要把缺失事实填成零", prompts)
            release = __import__("json").loads((Path(temp_dir) / "release-gates.json").read_text(encoding="utf-8"))
            self.assertEqual(release["release_status"], "not_approved")
            self.assertTrue(release["manual_gates"])
            self.assertIn(
                "runtime_data_layout_preflight",
                {gate["gate"] for gate in release["automated_gates"]},
            )
            self.assertIn(
                "connector_sync_checkpoint_and_quarantine_controls",
                {gate["gate"] for gate in release["automated_gates"]},
            )
            self.assertIn(
                "stable_promotion_evidence_control",
                {gate["gate"] for gate in release["automated_gates"]},
            )
            self.assertIn(
                "tax_applicability_reviews",
                {gate["gate"] for gate in release["automated_gates"]},
            )
            rc_gate = next(
                gate for gate in release["automated_gates"]
                if gate["gate"] == "technical_rc_product_matrix"
            )
            self.assertIn("release-candidate-audit", rc_gate["command"])
            self.assertTrue(rc_gate["expected"]["release_artifacts_verified"])
            self.assertEqual(
                rc_gate["expected"]["starter_matrix"][
                    "unavailable_combination_count"
                ],
                0,
            )
            registry_gate = next(
                gate for gate in release["automated_gates"]
                if gate["gate"] == "tax_applicability_registry_activation"
            )
            self.assertIn(
                "tax-applicability-registry-verify", registry_gate["command"]
            )
            self.assertFalse(
                registry_gate["expected"]["filing_authorization_granted"]
            )
            skills = __import__("json").loads((Path(temp_dir) / "skill-catalog.json").read_text(encoding="utf-8"))
            self.assertEqual(len(skills["skills"]), 3)
            self.assertEqual(skills["installation_status"], "not_auto_installed")

    def test_network_connector_box_compiles_incremental_sync_windows(self):
        compiled = compile_box_file(
            ROOT / "examples" / "boxes" / "cn_dtc_shopify_stripe_store.json", PACKS,
        )
        policy = compiled["connector_sync_policy"]
        self.assertEqual(policy["schema_version"], 2)
        self.assertEqual(policy["generated_plan_schema_version"], 2)
        self.assertFalse(policy["capture_policy"]["complete_update_capture_claimed"])
        self.assertEqual(
            {item["connector_id"] for item in policy["incremental_connectors"]},
            {
                "shopify.orders", "shopify.monthly_order_evidence",
                "stripe.balance_transactions", "stripe.payouts",
            },
        )
        self.assertTrue(compiled["deployment"]["connector_sync_control_available"])
        catalog = {item["connector_id"]: item for item in compiled["connectors"]}
        self.assertEqual(catalog["shopify.orders"]["sync_window"]["value_format"], "iso8601")
        self.assertEqual(
            catalog["shopify.monthly_order_evidence"]["sync_window"]["value_format"],
            "iso8601",
        )
        self.assertEqual(
            catalog["stripe.payouts"]["sync_window"]["value_format"], "unix_seconds",
        )

    def test_readme_never_claims_external_filing_is_enabled(self):
        compiled = compile_box_file(ROOT / "examples" / "boxes" / "global_game_studio.json", PACKS)
        self.assertIn("外部申报：未启用", render_box_readme(compiled))
        self.assertIn("不构成运行时授权", render_agent_prompts(compiled))


if __name__ == "__main__":
    unittest.main()
