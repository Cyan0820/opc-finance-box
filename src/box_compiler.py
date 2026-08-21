from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .box_api import build_box_context
from .box_runtime import BoxRuntime
from .cfo_control_overlay import build_cfo_control_overlay
from .cfo_metric_catalog import build_cfo_metric_catalog
from .connector_entity_credentials import (
    AMAZON_SELLER_BINDINGS_ENV,
    PAYPAL_BINDINGS_ENV,
    SHIPBOB_BINDINGS_ENV,
    WOOCOMMERCE_BINDINGS_ENV,
)
from .default_connectors import build_box_connector_registry
from .default_services import build_default_service_registry
from .pack_services import PackServiceRegistry
from .box_upgrade import build_upgrade_policy
from .runtime_storage import CURRENT_LAYOUT_VERSION, MANIFEST_NAME, STORE_CONTRACT
from .release_promotion import (
    stable_promotion_evidence_template_catalog,
    stable_promotion_policy,
)
from .resource_paths import find_resource_root
from .tax_pack_lifecycle import build_tax_applicability_questionnaire
from .pilot_readiness import build_pilot_readiness_plan
from .production_readiness import build_production_readiness_plan
from .pilot_data_handoff import build_pilot_data_handoff_plan


WORKFLOW_BLUEPRINTS = (
    {
        "workflow_id": "core.monthly_close",
        "display_name": "月结与经营简报",
        "capability": "finance.record_to_report",
        "cadence": "monthly",
        "outputs": ["reconciliation_status", "draft_financials", "founder_briefing"],
        "human_gate": "period_close",
    },
    {
        "workflow_id": "core.cash_watch",
        "display_name": "现金安全与应收风险",
        "capability": "finance.cash_forecast",
        "cadence": "daily",
        "outputs": ["cash_risk_alerts", "runway_scenarios", "collection_priorities"],
        "human_gate": None,
    },
    {
        "workflow_id": "game.channel_settlement_close",
        "display_name": "游戏渠道结算导入、合同映射与应收核对",
        "capability": "game.channel_settlement",
        "cadence": "monthly",
        "outputs": ["channel_settlement_reconciliation", "receivable_review_candidate"],
        "human_gate": "channel_contract_mapping",
    },
    {
        "workflow_id": "game.revenue_close",
        "display_name": "游戏渠道结算与收入确认",
        "capability": "game.revenue_recognition",
        "cadence": "monthly",
        "outputs": ["channel_reconciliation", "revenue_workpaper"],
        "human_gate": "accounting_policy_decision",
    },
    {
        "workflow_id": "game.investment_review",
        "display_name": "投放经营与资源决策",
        "capability": "game.user_acquisition_finance",
        "cadence": "weekly",
        "outputs": ["budget_variance", "payback_review", "profit_cash_impact"],
        "human_gate": "user_acquisition_budget_change",
    },
    {
        "workflow_id": "commerce.order_margin",
        "display_name": "订单、结算与贡献利润",
        "capability": "commerce.product_margin",
        "cadence": "daily",
        "outputs": ["order_settlement_reconciliation", "contribution_margin"],
        "human_gate": None,
    },
    {
        "workflow_id": "commerce.channel_close",
        "display_name": "通用电商订单、退货入库、结算、目的地和贡献候选关账",
        "capability": "channel.dtc_payment_reconciliation",
        "cadence": "daily",
        "outputs": [
            "order_settlement_reconciliation", "refund_summary",
            "return_inventory_reconciliation", "import_landed_cost_candidates",
            "fulfillment_cost_summary",
            "destination_tax_evidence",
        ],
        "human_gate": "commerce_source_mapping",
    },
    {
        "workflow_id": "commerce.revenue_cutoff",
        "display_name": "退款、履约与收入截止",
        "capability": "commerce.refunds",
        "cadence": "monthly",
        "outputs": ["refund_exceptions", "cutoff_workpaper"],
        "human_gate": "revenue_cutoff",
    },
    {
        "workflow_id": "marketplace.channel_close",
        "display_name": "第三方平台费用、应收、退货入库与库存专用关账",
        "capability": "channel.marketplace_receivable",
        "cadence": "daily",
        "outputs": [
            "marketplace_fee_reconciliation", "marketplace_receivable_reconciliation",
            "return_inventory_reconciliation", "import_landed_cost_candidates",
            "marketplace_inventory_reconciliation",
        ],
        "human_gate": "marketplace_contract_mapping",
    },
    {
        "workflow_id": "marketplace.amazon_seller_transaction_close",
        "display_name": "Amazon Seller Finances 交易、费用、退款与结算引用复核",
        "capability": "connector.amazon_seller_transaction_activity",
        "cadence": "daily",
        "outputs": [
            "transaction_status_summary", "currency_separated_financial_activity",
            "fee_refund_candidates", "settlement_completeness_candidates",
            "founder_amazon_seller_briefing",
        ],
        "human_gate": "amazon_seller_transaction_mapping_review",
    },
    {
        "workflow_id": "marketplace.amazon_seller_marketplace_close",
        "display_name": "Amazon Seller 订单、FBA 当前库存与 Finances 三源完整性复核",
        "capability": "connector.amazon_seller_marketplace_evidence",
        "cadence": "daily",
        "outputs": [
            "order_finance_completeness_candidates",
            "fba_inventory_scope_candidates",
            "currency_separated_financial_activity",
            "founder_amazon_marketplace_briefing",
        ],
        "human_gate": "amazon_seller_order_finance_completeness_review",
    },
    {
        "workflow_id": "payments.stripe_cash_reconciliation",
        "display_name": "Stripe 余额活动、打款与银行到账核对",
        "capability": "connector.stripe_payouts",
        "cadence": "daily",
        "outputs": ["payout_bank_candidates", "settlement_exceptions", "founder_risk_briefing"],
        "human_gate": "stripe_mapping_approval",
    },
    {
        "workflow_id": "payments.paypal_transaction_close",
        "display_name": "PayPal 交易、费用、退款与余额转出证据复核",
        "capability": "connector.paypal_transaction_activity",
        "cadence": "daily",
        "outputs": [
            "transaction_event_summary", "fee_summary", "refund_reversal_candidates",
            "founder_paypal_briefing",
        ],
        "human_gate": "paypal_transaction_event_mapping_review",
    },
    {
        "workflow_id": "commerce.woocommerce_order_refund_close",
        "display_name": "WooCommerce 修改订单、状态、税额与退款证据复核",
        "capability": "connector.woocommerce_order_refund_activity",
        "cadence": "daily",
        "outputs": [
            "order_status_summary", "currency_separated_order_facts",
            "refund_completeness_candidates", "founder_woocommerce_briefing",
        ],
        "human_gate": "woocommerce_order_status_mapping_review",
    },
    {
        "workflow_id": "commerce.shopify_stripe_order_to_cash",
        "display_name": "Shopify 订单、Stripe 收付与银行到账证据链",
        "capability": "integration.shopify_stripe_order_to_cash",
        "cadence": "daily",
        "outputs": [
            "shopify_order_payment_review", "processor_activity_reconciliation",
            "payout_bank_candidates", "founder_risk_briefing",
        ],
        "human_gate": "processor_link_mapping_approval",
    },
    {
        "workflow_id": "commerce.shopify_stripe_monthly_metrics",
        "display_name": "Shopify 月末订单/退款双窗口与 Stripe 同期指标复核",
        "capability": "integration.shopify_stripe_monthly_close",
        "cadence": "monthly",
        "outputs": [
            "canonical_month_scope_proof", "tax_exclusive_sales_refund_operands",
            "processor_activity_reconciliation", "cfo_metric_candidates",
        ],
        "human_gate": "tax_inclusive_policy_confirmed",
    },
    {
        "workflow_id": "commerce.shipbob_fulfillment_close",
        "display_name": "ShipBob 订单、履约成本与退货处置证据复核",
        "capability": "connector.shipbob_fulfillment_evidence",
        "cadence": "daily",
        "outputs": [
            "fulfillment_status_summary", "fulfillment_invoice_candidates",
            "return_disposition_candidates", "founder_fulfillment_briefing",
        ],
        "human_gate": "shipbob_fulfillment_cost_review",
    },
    {
        "workflow_id": "tax.cn.calendar",
        "display_name": "中国大陆属地税务日历",
        "capability": "tax.cn.filing_calendar",
        "cadence": "monthly",
        "outputs": ["local_configuration_tasks", "deadline_candidates"],
        "human_gate": "tax_workpaper_approval",
    },
    {
        "workflow_id": "tax.sg.calendar",
        "display_name": "新加坡税务候选日历",
        "capability": "tax.sg.review_calendar_skeleton",
        "cadence": "monthly",
        "outputs": ["eci_candidate", "annual_return_candidate", "gst_candidate"],
        "human_gate": "tax_advisor_review",
    },
    {
        "workflow_id": "tax.us_federal.c_corp_calendar",
        "display_name": "美国联邦 C corporation 分类、EIN 与 Form 1120 候选日历",
        "capability": "tax.us_federal.review_calendar_skeleton",
        "cadence": "monthly",
        "outputs": [
            "classification_and_ein_gaps", "form_1120_calendar_configuration",
            "estimated_tax_calendar_configuration",
        ],
        "human_gate": "tax_advisor_review",
    },
    {
        "workflow_id": "tax.hk.corporation_calendar",
        "display_name": "香港法团 BRN、BIR51 与预缴利得税候选日历",
        "capability": "tax.hk.review_calendar_skeleton",
        "cadence": "monthly",
        "outputs": [
            "registration_and_brn_gaps", "bir51_calendar_configuration",
            "provisional_profits_tax_calendar_configuration",
        ],
        "human_gate": "tax_advisor_review",
    },
    {
        "workflow_id": "tax.uk_limited_company.calendar",
        "display_name": "英国 Ltd Corporation Tax、VAT 与 Companies House 候选日历",
        "capability": "tax.uk_limited_company.review_calendar_skeleton",
        "cadence": "monthly",
        "outputs": [
            "company_and_corporation_tax_registration_gaps",
            "ct600_and_payment_calendar",
            "vat_registration_and_return_calendar",
            "companies_house_accounts_calendar",
        ],
        "human_gate": "tax_advisor_review",
    },
    {
        "workflow_id": "entity.management_consolidation",
        "display_name": "多主体管理合并与抵销复核",
        "capability": "entity.management_consolidation",
        "cadence": "monthly",
        "outputs": ["translated_entity_view", "approved_eliminations", "management_totals"],
        "human_gate": "consolidation_adjustment",
    },
    {
        "workflow_id": "entity.month_close_portfolio",
        "display_name": "多主体月结准备度与创始人组合简报",
        "capability": "entity.management_consolidation",
        "cadence": "monthly",
        "outputs": [
            "per_entity_close_readiness", "native_currency_close_candidates",
            "explicit_fx_management_portfolio", "founder_portfolio_briefing",
        ],
        "human_gate": "month_close_portfolio_review",
    },
)

PIPELINE_CATALOG = (
    {
        "pipeline_id": "finance.expense_evidence_review",
        "display_name": "Airwallex 已批准企业卡费用证据复核",
        "capability": "connector.airwallex_approved_expenses",
        "entity_scope": "statutory",
        "stages": [
            "expense_connector", "quality_gate", "entity_scope", "expense_evidence_review",
        ],
        "required_connectors": ["airwallex.approved_expenses"],
        "required_services": ["airwallex.review_expense_evidence"],
        "review_gates": [
            "airwallex_entity_account_binding_review",
            "airwallex_update_capture_review",
            "airwallex_webhook_binding_and_quarantine_review",
            "airwallex_expense_evidence_review",
            "expense_accounting_mapping_review",
        ],
        "external_actions": False,
    },
    {
        "pipeline_id": "finance.bank_statement_close",
        "display_name": "主体银行对账单导入与余额调节候选",
        "capability": "finance.bank_reconciliation",
        "entity_scope": "statutory",
        "stages": [
            "bank_statement_connector", "quality_gate", "entity_scope",
            "bank_reconciliation_candidate",
        ],
        "required_connectors_any": ["file.bank_statement", "wise.balance_statement"],
        "required_services": ["core.reconcile_bank_activity"],
        "review_gates": [
            "bank_statement_mapping_review", "bank_balance_reconciliation",
        ],
        "external_actions": False,
    },
    {
        "pipeline_id": "finance.trial_balance_review",
        "display_name": "主体会计导出试算平衡复核",
        "capability": "finance.record_to_report",
        "entity_scope": "statutory",
        "stages": [
            "accounting_export_connector", "quality_gate", "entity_period_scope",
            "trial_balance_validation",
        ],
        "required_connectors_any": ["file.trial_balance", "xero.trial_balance"],
        "required_services": ["core.validate_trial_balance_import"],
        "review_gates": [
            "accounting_export_mapping_review", "trial_balance_control_total_review",
        ],
        "external_actions": False,
    },
    {
        "pipeline_id": "finance.accounting_close_review",
        "display_name": "主体总账明细、试算平衡与报表映射复核",
        "capability": "finance.record_to_report",
        "entity_scope": "statutory",
        "stages": [
            "general_ledger_connector", "trial_balance_connector", "quality_gate",
            "entity_period_scope", "journal_and_trial_balance_validation",
            "ledger_trial_balance_reconciliation", "financial_statement_candidates",
        ],
        "required_connectors": ["file.general_ledger", "file.trial_balance"],
        "required_services": ["core.reconcile_accounting_close_exports"],
        "review_gates": [
            "accounting_export_mapping_review", "trial_balance_control_total_review",
            "financial_statement_mapping_review", "accounting_policy_decision",
        ],
        "external_actions": False,
    },
    {
        "pipeline_id": "finance.month_close_control",
        "display_name": "银行、总账与试算平衡三方月结控制",
        "capability": "finance.record_to_report",
        "entity_scope": "statutory",
        "stages": [
            "bank_statement_connector", "general_ledger_connector",
            "trial_balance_connector", "quality_gate", "entity_period_scope",
            "bank_reconciliation_candidate", "accounting_close_reconciliation",
            "explicit_bank_gl_mapping", "month_close_control_candidate",
            "founder_monthly_briefing",
        ],
        "required_connectors": ["file.general_ledger", "file.trial_balance"],
        "required_connectors_any": ["file.bank_statement", "wise.balance_statement"],
        "required_services": [
            "core.reconcile_bank_activity", "core.reconcile_accounting_close_exports",
            "core.build_month_close_control",
        ],
        "review_gates": [
            "bank_statement_mapping_review", "bank_balance_reconciliation",
            "accounting_export_mapping_review", "trial_balance_control_total_review",
            "financial_statement_mapping_review", "accounting_policy_decision",
            "month_close_control_review",
        ],
        "external_actions": False,
    },
    {
        "pipeline_id": "finance.first_close_discovery",
        "display_name": "首月结三来源发现与 fail-closed 映射起点",
        "capability": "finance.record_to_report",
        "entity_scope": "statutory",
        "stages": [
            "bank_statement_connector", "general_ledger_connector",
            "trial_balance_connector", "quality_gate", "entity_period_scope",
            "bank_source_inventory", "ledger_trial_movement_reconciliation",
            "fail_closed_mapping_starters",
        ],
        "required_connectors": ["file.general_ledger", "file.trial_balance"],
        "required_connectors_any": ["file.bank_statement", "wise.balance_statement"],
        "required_services": [
            "core.reconcile_bank_activity", "core.discover_first_close_configuration",
        ],
        "review_gates": [
            "bank_statement_mapping_review", "accounting_export_mapping_review",
            "trial_balance_control_total_review", "first_close_configuration_review",
        ],
        "external_actions": False,
    },
    {
        "pipeline_id": "finance.multi_entity_month_close_portfolio",
        "display_name": "多主体月结候选准备度与显式汇率组合视图",
        "capability": "entity.management_consolidation",
        "entity_scope": "management",
        "stages": [
            "single_entity_close_candidate_scope", "per_entity_readiness",
            "explicit_fx_review", "management_portfolio_candidate",
            "founder_portfolio_briefing",
        ],
        "required_connectors": [],
        "required_services": ["entity.build_month_close_portfolio"],
        "review_gates": ["month_close_portfolio_review"],
        "external_actions": False,
    },
    {
        "pipeline_id": "commerce.import_analyze",
        "display_name": "Commerce 导入、质量门与确定性利润分析",
        "capability": "channel.dtc_order_import",
        "entity_scope": "management",
        "stages": ["connector", "quality_gate", "deterministic_analysis"],
        "required_connectors_any": [
            "file.commerce", "file.csv_commerce", "file.xlsx_commerce",
            "example.commerce_api_payload",
        ],
        "required_services": ["commerce.analyze"],
        "review_gates": [],
        "external_actions": False,
    },
    {
        "pipeline_id": "game.channel_settlement_close",
        "display_name": "游戏渠道结算文件、合同映射与应收关账候选",
        "capability": "game.channel_settlement",
        "entity_scope": "management",
        "stages": [
            "game_settlement_connector", "quality_gate", "contract_mapping",
            "settlement_reconciliation",
        ],
        "required_connectors_any": [
            "file.app_store_settlements", "file.google_play_settlements",
            "file.domestic_game_settlements",
        ],
        "required_services": ["game.reconcile_channel_settlements"],
        "review_gates": ["channel_contract_mapping", "game_principal_agent_assessment"],
        "external_actions": False,
    },
    {
        "pipeline_id": "commerce.channel_close",
        "display_name": "通用电商/独立站文件或 API Payload 关账候选",
        "capability": "channel.dtc_payment_reconciliation",
        "entity_scope": "management",
        "stages": [
            "commerce_connector", "quality_gate", "entity_scope",
            "order_settlement_reconciliation", "refund_summary",
            "return_inventory_reconciliation", "import_landed_cost_candidates",
            "fulfillment_cost_summary",
            "destination_evidence",
        ],
        "required_connectors_any": [
            "file.commerce", "file.csv_commerce", "file.xlsx_commerce",
            "example.commerce_api_payload",
        ],
        "required_services": [
            "commerce.order_to_cash", "commerce.refund_summary",
            "commerce.reconcile_return_inventory",
            "commerce.build_import_landed_cost_candidates",
            "commerce.fulfillment_cost_summary", "dtc.destination_evidence",
        ],
        "review_gates": [
            "commerce_source_mapping", "revenue_cutoff", "inventory_valuation_policy",
            "return_disposition_review", "import_landed_cost_policy", "sales_tax_nexus_review",
        ],
        "external_actions": False,
    },
    {
        "pipeline_id": "marketplace.channel_close",
        "display_name": "第三方 Marketplace 费用、应收与库存关账候选",
        "capability": "channel.marketplace_receivable",
        "entity_scope": "management",
        "stages": [
            "marketplace_connector", "quality_gate", "entity_scope",
            "marketplace_fee_reconciliation", "marketplace_receivable_reconciliation",
            "return_inventory_reconciliation", "import_landed_cost_candidates",
            "marketplace_inventory_reconciliation",
        ],
        "required_connectors_any": [
            "file.marketplace_commerce", "example.marketplace_api_payload",
        ],
        "required_services": [
            "marketplace.reconcile_fees", "marketplace.reconcile_receivable",
            "commerce.reconcile_return_inventory",
            "commerce.build_import_landed_cost_candidates",
            "marketplace.reconcile_inventory",
        ],
        "review_gates": [
            "commerce_source_mapping", "marketplace_contract_mapping",
            "marketplace_inventory_mapping", "revenue_cutoff", "inventory_valuation_policy",
            "return_disposition_review", "import_landed_cost_policy",
        ],
        "external_actions": False,
    },
    {
        "pipeline_id": "stripe.daily_close",
        "display_name": "Stripe 余额活动、Payout 与银行到账日结候选",
        "capability": "connector.stripe_payouts",
        "entity_scope": "statutory",
        "stages": [
            "stripe_balance_connector", "stripe_payout_connector", "quality_gate",
            "balance_activity_summary", "payout_bank_reconciliation",
        ],
        "required_connectors": ["stripe.balance_transactions", "stripe.payouts"],
        "required_services": [
            "stripe.summarize_balance_activity", "stripe.reconcile_payouts",
        ],
        "review_gates": ["stripe_mapping_approval"],
        "external_actions": False,
    },
    {
        "pipeline_id": "amazon_seller.transaction_close",
        "display_name": "Amazon Seller Finances 交易、费用与结算引用日结候选",
        "capability": "connector.amazon_seller_transaction_activity",
        "entity_scope": "statutory",
        "stages": [
            "amazon_seller_transaction_connector", "quality_gate", "entity_scope",
            "transaction_activity_summary", "founder_amazon_seller_briefing",
        ],
        "required_connectors": ["amazon_seller.transaction_activity"],
        "required_services": ["amazon_seller.summarize_transaction_activity"],
        "review_gates": [
            "amazon_seller_entity_account_binding_review",
            "amazon_seller_marketplace_scope_review",
            "amazon_seller_transaction_mapping_review",
            "amazon_seller_fee_tax_policy_review",
            "amazon_seller_settlement_completeness_review",
        ],
        "external_actions": False,
    },
    {
        "pipeline_id": "amazon_seller.marketplace_close",
        "display_name": "Amazon Seller 订单、FBA 当前库存与 Finances 三源日结候选",
        "capability": "connector.amazon_seller_marketplace_evidence",
        "entity_scope": "statutory",
        "stages": [
            "amazon_seller_marketplace_connector", "quality_gate", "entity_scope",
            "orders_inventory_finances_reconciliation", "founder_marketplace_briefing",
        ],
        "required_connectors": ["amazon_seller.marketplace_evidence"],
        "required_services": ["amazon_seller.reconcile_marketplace_evidence"],
        "review_gates": [
            "amazon_seller_entity_account_binding_review",
            "amazon_seller_marketplace_scope_review",
            "amazon_seller_orders_scope_review",
            "amazon_seller_transaction_mapping_review",
            "amazon_seller_order_finance_completeness_review",
            "amazon_seller_inventory_scope_review",
            "amazon_seller_inventory_reconciliation_review",
            "amazon_seller_fee_tax_policy_review",
            "amazon_seller_settlement_completeness_review",
        ],
        "external_actions": False,
    },
    {
        "pipeline_id": "paypal.transaction_close",
        "display_name": "PayPal 余额影响交易、费用与退款日结候选",
        "capability": "connector.paypal_transaction_activity",
        "entity_scope": "statutory",
        "stages": [
            "paypal_transaction_connector", "quality_gate", "entity_scope",
            "transaction_activity_summary", "founder_paypal_briefing",
        ],
        "required_connectors": ["paypal.transaction_activity"],
        "required_services": ["paypal.summarize_transaction_activity"],
        "review_gates": [
            "paypal_entity_account_binding_review", "paypal_transaction_event_mapping_review",
            "paypal_fee_treatment_review", "paypal_refund_reversal_review",
        ],
        "external_actions": False,
    },
    {
        "pipeline_id": "woocommerce.order_refund_close",
        "display_name": "WooCommerce 修改订单与退款日结候选",
        "capability": "connector.woocommerce_order_refund_activity",
        "entity_scope": "statutory",
        "stages": [
            "woocommerce_order_refund_connector", "quality_gate", "entity_scope",
            "order_refund_activity_summary", "founder_woocommerce_briefing",
        ],
        "required_connectors": ["woocommerce.order_refund_activity"],
        "required_services": ["woocommerce.summarize_order_refund_activity"],
        "review_gates": [
            "woocommerce_site_entity_binding_review",
            "woocommerce_order_status_mapping_review",
            "woocommerce_refund_completeness_review",
            "woocommerce_tax_and_revenue_policy_review",
        ],
        "external_actions": False,
    },
    {
        "pipeline_id": "dtc.shopify_stripe_daily_close",
        "display_name": "Shopify + Stripe 订单到银行到账日结候选",
        "capability": "integration.shopify_stripe_order_to_cash",
        "entity_scope": "statutory",
        "stages": [
            "shopify_orders_connector", "stripe_balance_connector", "stripe_payout_connector",
            "quality_gate", "shopify_order_activity", "stripe_balance_activity",
            "shopify_stripe_activity_reconciliation", "stripe_payout_bank_reconciliation",
        ],
        "required_connectors": [
            "shopify.orders", "stripe.balance_transactions", "stripe.payouts",
        ],
        "optional_connectors": ["wise.balance_statement"],
        "required_services": [
            "shopify.summarize_order_activity", "stripe.summarize_balance_activity",
            "dtc.reconcile_shopify_stripe_activity", "stripe.reconcile_payouts",
        ],
        "review_gates": [
            "shopify_mapping_approval", "processor_link_mapping_approval",
            "stripe_mapping_approval",
        ],
        "external_actions": False,
    },
    {
        "pipeline_id": "dtc.shopify_stripe_month_close",
        "display_name": "Shopify + Stripe 可核验月度销售与退款关账候选",
        "capability": "integration.shopify_stripe_monthly_close",
        "entity_scope": "statutory",
        "stages": [
            "shopify_monthly_order_evidence_connector", "stripe_balance_connector",
            "quality_gate", "canonical_month_scope_gate", "shopify_monthly_commerce_scope",
            "stripe_balance_activity", "shopify_stripe_activity_reconciliation",
        ],
        "required_connectors": [
            "shopify.monthly_order_evidence", "stripe.balance_transactions",
        ],
        "required_services": [
            "shopify.build_monthly_commerce_scope", "stripe.summarize_balance_activity",
            "dtc.reconcile_shopify_stripe_activity",
        ],
        "review_gates": [
            "shopify_mapping_approval", "processor_link_mapping_approval",
            "stripe_mapping_approval", "tax_inclusive_policy_confirmed",
            "return_authorization_and_receipt_scope_aligned",
        ],
        "external_actions": False,
    },
    {
        "pipeline_id": "commerce.shipbob_fulfillment_close",
        "display_name": "ShipBob 履约成本与退货证据日结候选",
        "capability": "connector.shipbob_fulfillment_evidence",
        "entity_scope": "statutory",
        "stages": [
            "shipbob_fulfillment_connector", "quality_gate", "entity_scope",
            "fulfillment_and_return_evidence_summary", "founder_fulfillment_briefing",
        ],
        "required_connectors": ["shipbob.fulfillment"],
        "required_services": ["shipbob.summarize_fulfillment_evidence"],
        "review_gates": [
            "shipbob_entity_binding_review", "shipbob_order_mapping_review",
            "shipbob_fulfillment_cost_review", "return_disposition_review",
        ],
        "external_actions": False,
    },
)

# These controls are enforced directly by BoxRuntime / EntityRegistry rather than dispatched services.
RUNTIME_CAPABILITIES = {
    "connector.batch_evidence_contract",
    "entity.separate_legal_books",
    "finance.multi_currency",
}


DATA_MODEL_CATALOG = (
    {
        "object_id": "legal_entity", "capability": None,
        "required_fields": ["entity_id", "legal_name", "jurisdiction", "functional_currency", "tax_pack"],
        "control": "法定账、银行、税务和审批不得跨主体合并写入",
    },
    {
        "object_id": "evidence_reference", "capability": "audit.evidence_lineage",
        "required_fields": ["source_id", "source_type", "source_reference", "entity_id", "captured_at"],
        "control": "结论必须能回到原始来源；hash 不能替代业务证据",
    },
    {
        "object_id": "bank_transaction", "capability": "finance.bank_reconciliation",
        "required_fields": [
            "bank_transaction_id", "entity_id", "transaction_date", "account_masked",
            "currency", "direction", "amount", "evidence",
        ],
        "control": "完整账号不进入标准数据集；只生成候选匹配，余额确认、付款和核销状态需显式记录",
    },
    {
        "object_id": "external_trial_balance", "capability": "finance.record_to_report",
        "required_fields": [
            "line_id", "entity_id", "period", "currency", "account_code", "account_name",
            "opening_debit", "opening_credit", "period_debit", "period_credit",
            "closing_debit", "closing_credit", "evidence",
        ],
        "control": "只在同主体、同期间、同币种内校验借贷；平衡不等于映射完整、已过账或已关账",
    },
    {
        "object_id": "external_general_ledger_line", "capability": "finance.record_to_report",
        "required_fields": [
            "journal_line_id", "entity_id", "journal_id", "line_number", "posting_date",
            "period", "currency", "account_code", "account_name", "debit", "credit", "evidence",
        ],
        "control": "逐凭证逐币种借贷平衡；必须与试算本期发生逐科目勾稽，不能跨主体/币种轧差",
    },
    {
        "object_id": "financial_statement_account_mapping", "capability": "finance.record_to_report",
        "required_fields": [
            "account_code", "statement_group", "statement_line_id", "statement_line_name",
        ],
        "control": "报表分类必须显式配置并复核，不根据科目名称、编码前缀或金额方向自动猜测",
    },
    {
        "object_id": "bank_gl_account_mapping", "capability": "finance.record_to_report",
        "required_fields": [
            "entity_id", "period", "account_masked", "currency", "gl_account_code",
            "bank_source_fingerprint", "transaction_review", "reconciling_items",
        ],
        "control": "银行账户到 GL 现金科目必须显式配置；来源变化使旧复核失效，调节项须逐项证据和状态",
    },
    {
        "object_id": "entity_month_close_portfolio_candidate",
        "capability": "entity.management_consolidation",
        "required_fields": [
            "period", "entity_ids", "source_run_ids", "native_currency_summaries",
            "approved_fx_rates", "per_entity_readiness", "candidate_status",
        ],
        "control": (
            "只组合同期单主体月结候选；原币永不直接轧差，"
            "跨币种总额必须绑定期间、来源、复核人与证据"
        ),
    },
    {
        "object_id": "purchase_invoice_payment", "capability": "finance.procure_to_pay",
        "required_fields": ["entity_id", "currency", "counterparty_id", "period", "amount", "evidence"],
        "control": "采购、验收、发票、审批和付款不能因金额相等而自动视为同一事实",
    },
    {
        "object_id": "voucher_and_period", "capability": "finance.record_to_report",
        "required_fields": ["entity_id", "period", "currency", "lines", "status", "evidence"],
        "control": "草稿、复核、过账和关账是不同状态；关账必须人工批准",
    },
    {
        "object_id": "cash_forecast", "capability": "finance.cash_forecast",
        "required_fields": ["entity_id", "as_of", "currency", "opening_cash", "scenarios", "assumptions"],
        "control": "预测不是银行余额或融资承诺",
    },
    {
        "object_id": "commerce_order", "capability": "commerce.order_to_cash",
        "required_fields": ["order_id", "entity_id", "period", "channel", "destination_country", "currency"],
        "control": "订单金额、税额、退款和结算必须分字段保留",
    },
    {
        "object_id": "channel_settlement", "capability": "commerce.order_to_cash",
        "required_fields": ["settlement_id", "entity_id", "period", "channel", "currency", "payout"],
        "control": "渠道结算与银行到账分别核对，不按跨币种总额勾稽",
    },
    {
        "object_id": "commerce_return",
        "capability": "commerce.return_inventory_reconciliation",
        "required_fields": [
            "return_id", "order_id", "entity_id", "period", "channel", "sku", "currency",
            "authorized_quantity", "refunded_quantity", "refund_amount_ex_tax",
            "refunded_tax", "evidence",
        ],
        "control": "退货授权、退款数量与金额分别保留；无仓库实收不能自动解释成已完成退货",
    },
    {
        "object_id": "commerce_return_receipt",
        "capability": "commerce.return_inventory_reconciliation",
        "required_fields": [
            "receipt_id", "return_id", "entity_id", "period", "sku", "warehouse",
            "received_quantity", "disposition", "evidence",
        ],
        "control": "实收按仓库和处置状态形成补库存候选；未经复核不改变库存数量、价值或总账",
    },
    {
        "object_id": "commerce_import_cost",
        "capability": "commerce.import_landed_cost",
        "required_fields": [
            "entry_line_id", "import_entry_id", "entity_id", "period", "sku", "warehouse",
            "origin_country", "destination_country", "currency", "quantity", "declared_value",
            "inbound_freight", "insurance", "customs_duty", "import_tax", "brokerage", "evidence",
        ],
        "control": "关税与进口费用只形成 landed-cost 候选；分类、税率、进口税可抵扣和入账均需人工政策门",
    },
    {
        "object_id": "stripe_balance_transaction", "capability": "connector.stripe_balance_transactions",
        "required_fields": ["balance_transaction_id", "entity_id", "currency", "amount_minor", "fee_minor", "net_minor", "evidence"],
        "control": "保留最小货币单位和 reporting_category；余额活动不自动等同收入或凭证",
    },
    {
        "object_id": "stripe_payout", "capability": "connector.stripe_payouts",
        "required_fields": ["payout_id", "entity_id", "currency", "amount_minor", "arrival_date", "status", "evidence"],
        "control": "Payout、余额交易与银行到账逐笔形成候选；人工确认前不核销或过账",
    },
    {
        "object_id": "paypal_balance_activity",
        "capability": "connector.paypal_transaction_activity",
        "required_fields": [
            "paypal_transaction_key", "entity_id", "event_code", "activity_class",
            "amount", "amount_currency", "fee", "fee_currency", "evidence",
        ],
        "control": "PayPal T-code 和金额只形成处理器资金活动证据；原始 ID、客户资料和自由文本不进入标准数据集，不自动认定收入、银行到账或凭证",
    },
    {
        "object_id": "amazon_seller_financial_transaction",
        "capability": "connector.amazon_seller_transaction_activity",
        "required_fields": [
            "amazon_transaction_key", "entity_id", "marketplace_id", "transaction_type",
            "transaction_status", "posted_at", "amount", "currency", "related_keys",
            "financial_components", "evidence",
        ],
        "control": "Amazon Finances 交易、层级费用与结算引用只形成 Marketplace 财务证据；客户、地址、店名、商品/SKU/ASIN、自由文本与原始 ID 不进入标准数据集，不自动认定收入、税负、结算或凭证",
    },
    {
        "object_id": "amazon_seller_order_evidence",
        "capability": "connector.amazon_seller_marketplace_evidence",
        "required_fields": [
            "amazon_order_key", "entity_id", "marketplace_id", "created_at",
            "last_updated_at", "fulfillment_status", "fulfilled_by", "items", "evidence",
        ],
        "control": "Orders v2026 仅请求 FULFILLMENT 并保留哈希关联键、状态和数量；不请求或保留买家、收件人、金额、税、支付、促销、包裹或商品身份，不自动认定收入与税负",
    },
    {
        "object_id": "amazon_seller_fba_inventory_observation",
        "capability": "connector.amazon_seller_marketplace_evidence",
        "required_fields": [
            "amazon_sku_key", "entity_id", "marketplace_id", "observed_updated_at",
            "total_quantity", "fulfillable_quantity", "reserved_quantity", "evidence",
        ],
        "control": "FBA Inventory v1 是抓取时点的当前数量观察，不是历史月末库存；不包含商品名、店铺或原始 SKU/ASIN/FNSKU，不自动形成库存价值、COGS 或调整",
    },
    {
        "object_id": "woocommerce_order_change_evidence",
        "capability": "connector.woocommerce_order_refund_activity",
        "required_fields": [
            "woocommerce_order_key", "entity_id", "modified_at", "status",
            "currency", "total", "total_tax", "destination_country", "evidence",
        ],
        "control": "WooCommerce 订单状态、金额和目的地只形成商店证据；客户、地址、商品名、SKU、meta 与原始 ID 不进入标准数据集，不自动认定收入、税负或支付结算",
    },
    {
        "object_id": "woocommerce_refund_event_evidence",
        "capability": "connector.woocommerce_order_refund_activity",
        "required_fields": [
            "woocommerce_refund_key", "parent_order_key", "entity_id",
            "created_at", "amount", "evidence",
        ],
        "control": "退款事件按修改窗口与订单终身退款总额分别保留；完整性和会计处理需人工复核，不调用退款或库存写接口",
    },
    {
        "object_id": "shopify_order_evidence", "capability": "connector.shopify_orders",
        "required_fields": [
            "order_id", "entity_id", "shop_currency", "presentment_currency",
            "money", "destination_country", "evidence",
        ],
        "control": "Shopify 原始、当前、实收和退款 MoneyBag 分别保留；缺失 COGS、履约或收入政策时不得计算利润或认定收入",
    },
    {
        "object_id": "shopify_financial_transaction", "capability": "connector.shopify_orders",
        "required_fields": [
            "transaction_id", "order_id", "entity_id", "kind", "status", "amount_set", "evidence",
        ],
        "control": "只有明确成功的 SALE、CAPTURE 和 REFUND 可进入收付核对；交易状态不能由订单状态推断",
    },
    {
        "object_id": "processor_evidence_link", "capability": "integration.shopify_stripe_order_to_cash",
        "required_fields": [
            "entity_id", "shopify_transaction_id", "stripe_source_object_id", "evidence",
        ],
        "control": "跨处理器匹配必须使用显式一对一证据链接和配置的货币小数位，不按金额猜关联",
    },
    {
        "object_id": "shipbob_fulfillment_evidence",
        "capability": "connector.shipbob_fulfillment_evidence",
        "required_fields": [
            "shipment_key", "order_key", "entity_id", "status",
            "fulfillment_center_key", "fulfillment_invoice", "evidence",
        ],
        "control": "订单、发货、履约账单按主体保留；客户身份、地址、原始运单号和源 ID 不进入标准数据集",
    },
    {
        "object_id": "shipbob_return_disposition_evidence",
        "capability": "connector.shipbob_fulfillment_evidence",
        "required_fields": [
            "return_key", "inventory_key", "entity_id", "sku", "quantity",
            "requested_action", "action_summary", "evidence",
        ],
        "control": "退货处置仅形成复核候选；不得自动补库存、报废、确认收入、生成凭证或调用写接口",
    },
    {
        "object_id": "inventory_movement", "capability": "commerce.inventory_cost",
        "required_fields": ["movement_id", "entity_id", "sku", "occurred_at", "quantity", "currency"],
        "control": "FIFO 或移动加权平均必须显式选择；负库存阻断成本结转",
    },
    {
        "object_id": "game_channel_settlement", "capability": "game.channel_settlement",
        "required_fields": ["id", "entity_id", "project_code", "channel", "period", "currency", "contract_formula"],
        "control": "分成与费用按合同公式复算，不从历史金额猜费率",
    },
    {
        "object_id": "game_revenue_policy", "capability": "game.revenue_recognition",
        "required_fields": ["id", "entity_id", "game", "channel", "presentation", "recognition_method", "status"],
        "control": "总额/净额和履约判断经批准后才可用于收入确认",
    },
    {
        "object_id": "game_cohort_finance", "capability": "game.ltv_roi_review",
        "required_fields": ["entity_id", "project_code", "cohort", "currency", "spend", "revenue", "observation_days"],
        "control": "短观察期 LTV 只能作为候选，不自动批准投放预算",
    },
    {
        "object_id": "tax_rule_and_workpaper", "capability": "tax.*",
        "required_fields": ["entity_id", "tax_pack", "pack_version", "rules_verified_at", "official_sources", "status"],
        "control": "地区规则、候选工作底稿、人工复核和实际提交回执必须分层记录",
    },
)


AGENT_CONTRACT_CATALOG = (
    {
        "contract_id": "finance.orchestrator", "capability": "agent.plans",
        "purpose": "把当前财务事实拆成可执行、阻塞、待确认和已完成动作",
        "required_inputs": ["goal", "legal_entity_scope", "current_finance_facts", "period_state"],
        "allowed_outputs": ["plan_snapshot", "blockers", "questions", "candidate_deliverables"],
        "prohibited_claims": ["把旧快照当真实状态", "把草稿说成已过账或已申报", "绕过人工 review gate"],
    },
    {
        "contract_id": "finance.cash_watch", "capability": "finance.cash_forecast",
        "purpose": "解释现金安全、应收优先级与情景风险",
        "required_inputs": ["entity_currency_opening_cash", "dated_cash_flows", "assumptions"],
        "allowed_outputs": ["scenario_forecast", "risk_alerts", "collection_priorities"],
        "prohibited_claims": ["混加币种", "把预测当银行余额", "自行承诺融资或付款"],
    },
    {
        "contract_id": "finance.bank_review", "capability": "finance.bank_reconciliation",
        "purpose": "按法律主体、银行账户、期间和币种核对流水与期末余额",
        "required_inputs": [
            "entity_scoped_bank_statement", "masked_account_reference",
            "statement_ending_balance", "ledger_ending_balance_and_reconciling_items",
        ],
        "allowed_outputs": [
            "candidate_transactions", "account_currency_summary",
            "pending_items", "balance_reconciliation_questions",
        ],
        "prohibited_claims": [
            "保留完整账号", "跨主体或币种净额调节", "自动认领流水",
            "在人工余额复核前声称银行账已调节", "自动核销或过账",
        ],
    },
    {
        "contract_id": "finance.accounting_export_review", "capability": "finance.record_to_report",
        "purpose": "按法律主体、期间和币种复核会计系统导出的试算平衡",
        "required_inputs": [
            "entity_scoped_accounting_export", "explicit_period", "currency_separated_lines",
            "account_code_and_name", "source_evidence",
        ],
        "allowed_outputs": [
            "control_total_summary", "unbalanced_scopes", "mapping_review_questions",
            "close_readiness_evidence",
        ],
        "prohibited_claims": [
            "跨主体或币种轧差", "从科目名称猜测会计政策", "修改总账或期初余额",
            "自动过账", "仅因借贷平衡就声称完整或已经关账",
        ],
    },
    {
        "contract_id": "finance.accounting_close_review", "capability": "finance.record_to_report",
        "purpose": "把外部总账明细、试算平衡与显式科目映射勾稽成逐币种报表候选",
        "required_inputs": [
            "entity_period_scoped_general_ledger", "entity_period_scoped_trial_balance",
            "explicit_account_to_statement_mappings", "source_evidence",
        ],
        "allowed_outputs": [
            "journal_balance_exceptions", "ledger_trial_balance_differences",
            "mapping_coverage", "currency_separated_statement_candidates", "review_questions",
        ],
        "prohibited_claims": [
            "根据科目名称猜报表分类", "跨主体或币种轧差", "修改外部或内部总账",
            "自动过账或关账", "把候选报表称为已批准、已审计或已申报报表",
        ],
    },
    {
        "contract_id": "finance.month_close_control", "capability": "finance.record_to_report",
        "purpose": "把银行期末余额、总账现金科目和试算平衡绑定成可复核的三方月结控制",
        "required_inputs": [
            "entity_period_scoped_bank_statement", "current_bank_source_fingerprint",
            "entity_period_scoped_general_ledger_and_trial_balance",
            "explicit_bank_account_to_gl_cash_account_mappings",
            "reviewed_transaction_and_reconciling_item_evidence",
        ],
        "allowed_outputs": [
            "account_currency_close_controls", "stale_review_exceptions",
            "bank_gl_balance_differences", "currency_separated_founder_monthly_briefing",
        ],
        "prohibited_claims": [
            "按金额相等认领交易", "跨主体或币种轧差", "由系统手填或猜测账面余额",
            "自动批准调节项", "修改总账、过账、关账或申报",
        ],
    },
    {
        "contract_id": "finance.first_close_discovery", "capability": "finance.record_to_report",
        "purpose": "从首月结三类只读来源盘点精确账户范围并生成可编辑、默认阻塞的映射起点",
        "required_inputs": [
            "entity_period_scoped_bank_statement", "entity_period_scoped_general_ledger",
            "entity_period_scoped_trial_balance", "source_evidence",
        ],
        "allowed_outputs": [
            "bank_account_inventory", "trial_balance_account_inventory",
            "ledger_trial_movement_exceptions", "fail_closed_mapping_starters",
            "first_close_configuration_tasks",
        ],
        "prohibited_claims": [
            "从科目名称猜报表分类", "猜测银行账户对应的现金科目",
            "自动完成流水复核", "把配置起点说成已映射、已过账或已关账",
        ],
    },
    {
        "contract_id": "finance.multi_entity_month_close_portfolio",
        "capability": "entity.management_consolidation",
        "purpose": "把同期、逐主体的月结候选与已复核汇率组合为创始人管理视图",
        "required_inputs": [
            "all_selected_entity_month_close_candidates", "source_pipeline_run_evidence",
            "currency_separated_native_summaries", "approved_period_specific_fx_rates",
        ],
        "allowed_outputs": [
            "per_entity_statutory_readiness", "native_currency_candidates",
            "pre_elimination_reporting_currency_totals", "founder_portfolio_briefing",
        ],
        "prohibited_claims": [
            "把未就绪主体排除后声称组合完整", "直接相加不同币种原币金额",
            "猜测、倒推或自动批准汇率", "声称已完成抵销或法定合并报表",
            "修改单主体账簿、过账、关账或申报",
        ],
    },
    {
        "contract_id": "finance.game_review", "capability": "game.project_profitability",
        "purpose": "解释渠道结算、项目贡献与投放回报",
        "required_inputs": ["entity_scoped_settlements", "direct_costs", "project_codes", "currencies"],
        "allowed_outputs": ["reconciliation_exceptions", "project_contribution", "decision_questions"],
        "prohibited_claims": ["从金额猜合同费率", "把短期 cohort 当成熟 LTV", "跨主体抵销法定收入"],
    },
    {
        "contract_id": "finance.commerce_review", "capability": "commerce.product_margin",
        "purpose": "解释订单、退款、履约、结算与商品贡献利润",
        "required_inputs": ["orders", "settlements", "refunds", "fulfillment_costs", "inventory_costs"],
        "allowed_outputs": ["order_settlement_exceptions", "margin_view", "destination_evidence_gaps"],
        "prohibited_claims": ["按目的地自动认定纳税义务", "把支付平台税款字段当最终税额", "用零替代缺失成本"],
    },
    {
        "contract_id": "finance.stripe_review", "capability": "connector.stripe_payouts",
        "purpose": "解释 Stripe 余额活动、手续费、退款、打款与银行到账候选",
        "required_inputs": ["entity_scoped_balance_transactions", "payouts", "bank_transactions_in_minor_units"],
        "allowed_outputs": ["candidate_matches", "currency_separated_facts", "settlement_exceptions", "review_questions"],
        "prohibited_claims": ["把 Stripe 活动直接认定收入", "混加币种", "猜测货币小数位", "自动核销或过账"],
    },
    {
        "contract_id": "finance.shopify_stripe_review",
        "capability": "integration.shopify_stripe_order_to_cash",
        "purpose": "贯通 Shopify 订单收退款、Stripe 余额活动、Payout 与银行到账证据",
        "required_inputs": [
            "entity_scoped_shopify_orders_transactions_refunds",
            "stripe_balance_transactions_and_payouts", "explicit_processor_links",
            "currency_minor_unit_configuration", "bank_receipt_evidence",
        ],
        "allowed_outputs": [
            "order_payment_exceptions", "processor_match_candidates",
            "payout_bank_candidates", "currency_separated_founder_briefing",
        ],
        "prohibited_claims": [
            "从金额或日期猜跨处理器链接", "把订单总额直接认定收入",
            "用零替代 COGS、履约成本或税务事实", "自动核销、过账或提交外部动作",
        ],
    },
    {
        "contract_id": "finance.shopify_stripe_monthly_metric_review",
        "capability": "integration.shopify_stripe_monthly_close",
        "purpose": "复核月末捕获的 Shopify 税外销售/退款范围与同期 Stripe 处理器证据",
        "required_inputs": [
            "close_captured_created_order_population",
            "orders_updated_since_month_start_population",
            "complete_refund_components_and_successful_transactions",
            "same_half_open_month_stripe_balance_activity", "explicit_processor_links",
        ],
        "allowed_outputs": [
            "canonical_month_scope_proof", "dtc_metric_operand_candidates",
            "processor_reconciliation_exceptions", "pending_human_controls",
        ],
        "prohibited_claims": [
            "把当前订单快照倒推成任意历史月", "自动分摊含税订单",
            "把退款等同于实物退货已验收", "自动认定收入、过账或申报",
        ],
    },
    {
        "contract_id": "finance.tax_preparer", "capability": "tax.*",
        "purpose": "整理规则版本、主体事实、候选税务工作底稿和缺口",
        "required_inputs": ["entity_tax_pack", "verified_rules", "statutory_facts", "registrations"],
        "allowed_outputs": ["candidate_workpaper", "evidence_checklist", "review_questions"],
        "prohibited_claims": ["声称已申报", "无属地事实自动套税率", "无授权执行外部提交"],
    },
)

PRODUCT_SKILLS = (
    {
        "name": "build-opc-finance-box",
        "path": "skills/build-opc-finance-box",
        "purpose": "装配、编译、评测、诊断和升级 Finance Box",
    },
    {
        "name": "review-opc-month-close",
        "path": "skills/review-opc-month-close",
        "purpose": "按主体、期间、币种、证据与 review gate 审查月结",
    },
    {
        "name": "add-opc-tax-pack",
        "path": "skills/add-opc-tax-pack",
        "purpose": "以官方来源创建和演进纳税地区 Pack",
    },
)


def _enabled_workflows(
    capabilities: set[str],
    executable_capabilities: set[str],
) -> list[dict[str, Any]]:
    return [
        {
            **dict(item),
            "implementation_status": (
                "executable" if item["capability"] in executable_capabilities else "blueprint_only"
            ),
        }
        for item in WORKFLOW_BLUEPRINTS
        if item["capability"] in capabilities
    ]


def _enabled_pipelines(
    capabilities: set[str],
    services: list[dict[str, Any]],
    connectors: list[dict[str, Any]],
    entities: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    service_ids = {item["service_id"] for item in services}
    connector_ids = {item["connector_id"] for item in connectors}
    entity_ids = [str(item["id"]) for item in entities]
    connector_entity_ids = {
        item["connector_id"]: set(item.get("entity_ids") or entity_ids)
        for item in connectors
    }
    output = []
    for item in PIPELINE_CATALOG:
        if item["capability"] not in capabilities:
            continue
        required_services = set(item.get("required_services") or [])
        required_connectors = set(item.get("required_connectors") or [])
        connectors_any = set(item.get("required_connectors_any") or [])
        optional_connectors = set(item.get("optional_connectors") or [])
        providers_available = (
            required_services <= service_ids
            and required_connectors <= connector_ids
            and (not connectors_any or bool(connectors_any & connector_ids))
        )
        executable = providers_available
        available_connectors = sorted(
            (required_connectors | connectors_any | optional_connectors) & connector_ids
        )
        available_connectors_by_entity = {
            entity_id: sorted(
                connector_id for connector_id in available_connectors
                if entity_id in connector_entity_ids.get(connector_id, set())
            )
            for entity_id in entity_ids
        }
        eligible_entity_ids = [
            entity_id for entity_id in entity_ids
            if required_connectors <= set(available_connectors_by_entity[entity_id])
            and (
                not connectors_any
                or bool(connectors_any & set(available_connectors_by_entity[entity_id]))
            )
        ]
        if executable and item.get("entity_scope") == "statutory" and not eligible_entity_ids:
            executable = False
        review_gates = list(item.get("review_gates") or [])
        if item["pipeline_id"] == "finance.trial_balance_review" and "xero.trial_balance" in available_connectors:
            review_gates.extend([
                "xero_entity_binding_review", "xero_trial_balance_mapping_review",
            ])
        if item["pipeline_id"] in {
            "finance.bank_statement_close", "finance.month_close_control",
            "finance.first_close_discovery",
        } and "wise.balance_statement" in available_connectors:
            review_gates.extend([
                "wise_entity_profile_binding_review",
                "wise_balance_account_mapping_review",
                "wise_statement_access_review",
            ])
        if (
            item["pipeline_id"] == "dtc.shopify_stripe_daily_close"
            and "wise.balance_statement" in available_connectors
        ):
            review_gates.extend([
                "wise_entity_profile_binding_review",
                "wise_balance_account_mapping_review",
                "wise_statement_access_review",
            ])
        implementation_status = "executable" if executable else "blocked_missing_provider"
        if (
            providers_available
            and item.get("entity_scope") == "statutory"
            and not eligible_entity_ids
        ):
            implementation_status = "blocked_connector_binding"
        output.append({
            **dict(item),
            "review_gates": list(dict.fromkeys(review_gates)),
            "implementation_status": implementation_status,
            "available_connectors": available_connectors,
            "available_connectors_by_entity": available_connectors_by_entity,
            "eligible_entity_ids": eligible_entity_ids,
        })
    return output


def _build_pipeline_request_templates(
    pipelines: list[dict[str, Any]], entities: list[dict[str, Any]],
    reporting_currency: str,
) -> dict[str, Any]:
    """Build secret-free, fail-closed request starters for every dispatchable pipeline."""
    templates: list[dict[str, Any]] = []
    for pipeline in pipelines:
        if pipeline["implementation_status"] != "executable":
            continue
        pipeline_id = pipeline["pipeline_id"]
        if pipeline_id == "commerce.import_analyze":
            templates.append({
                "template_id": "commerce.import_analyze",
                "pipeline_id": pipeline_id,
                "entity_scope": "management",
                "runnable_without_configuration": False,
                "required_configuration": [
                    "replace connector_id with one enabled Commerce Connector",
                    "supply that Connector's validated connector_request",
                    "confirm every imported row has an allowed legal entity",
                ],
                "request": {
                    "pipeline_id": pipeline_id,
                    "payload": {
                        "connector_id": "REPLACE_WITH_ENABLED_COMMERCE_CONNECTOR",
                        "connector_request": {},
                    },
                },
            })
            continue
        if pipeline_id == "finance.multi_entity_month_close_portfolio":
            entity_ids = [entity["id"] for entity in entities]
            controls = [{
                "entity_id": entity["id"],
                "period": "REPLACE_WITH_YYYY-MM",
                "source_pipeline_id": "finance.month_close_control",
                "source_run_id": "REPLACE_WITH_24_CHARACTER_MONTH_CLOSE_RUN_ID",
                "source_attempt_id": "REPLACE_WITH_24_CHARACTER_PIPELINE_LEDGER_ATTEMPT_ID",
                "source_evidence": ["REPLACE_WITH_PIPELINE_LEDGER_EVIDENCE_REFERENCE"],
                "close_control_ready_for_review": True,
                "candidate_only": True,
                "posting_performed": False,
                "period_close_performed": False,
                "blockers": [],
                "currency_summaries": [{
                    "currency": entity["functional_currency"],
                    "bank_account_count": "REPLACE_WITH_SOURCE_BANK_ACCOUNT_COUNT",
                    "cash": "REPLACE_WITH_SOURCE_CASH_CANDIDATE",
                    "assets": "REPLACE_WITH_SOURCE_ASSETS_CANDIDATE",
                    "liabilities": "REPLACE_WITH_SOURCE_LIABILITIES_CANDIDATE",
                    "revenue": "REPLACE_WITH_SOURCE_REVENUE_CANDIDATE",
                    "expenses": "REPLACE_WITH_SOURCE_EXPENSES_CANDIDATE",
                    "profit_before_tax_candidate": "REPLACE_WITH_SOURCE_PROFIT_CANDIDATE",
                }],
            } for entity in entities]
            fx_rates = {
                currency: {
                    "period": "REPLACE_WITH_YYYY-MM",
                    "pnl_rate": "REPLACE_WITH_APPROVED_PERIOD_AVERAGE_RATE",
                    "closing_rate": "REPLACE_WITH_APPROVED_PERIOD_END_RATE",
                    "source_reference": "REPLACE_WITH_FX_SOURCE_REFERENCE",
                    "review_status": "approved",
                    "reviewed_by": "REPLACE_WITH_FX_REVIEWER",
                    "evidence": ["REPLACE_WITH_FX_REVIEW_EVIDENCE"],
                }
                for currency in sorted({
                    str(entity["functional_currency"]).upper() for entity in entities
                    if str(entity["functional_currency"]).upper() != reporting_currency.upper()
                })
            }
            templates.append({
                "template_id": pipeline_id,
                "pipeline_id": pipeline_id,
                "entity_scope": "management",
                "runnable_without_configuration": False,
                "required_configuration": [
                    "select at least two configured legal entities and one common month",
                    "copy each entity's candidate summary and run evidence from finance.month_close_control",
                    "do not omit a selected entity or combine original-currency amounts",
                    "supply approved period-average and closing FX rates for every non-reporting currency",
                    "obtain month-close portfolio review; this is pre-elimination and never closes a legal entity",
                ],
                "request": {
                    "pipeline_id": pipeline_id,
                    "payload": {
                        "period": "REPLACE_WITH_YYYY-MM",
                        "entity_ids": entity_ids,
                        "entity_close_controls": controls,
                        "fx_rates": fx_rates,
                    },
                },
            })
            continue
        for entity in entities:
            entity_id = entity["id"]
            if (
                pipeline.get("entity_scope") == "statutory"
                and entity_id not in (pipeline.get("eligible_entity_ids") or [])
            ):
                continue
            entity_available_connectors = set(
                (pipeline.get("available_connectors_by_entity") or {}).get(entity_id, [])
            )
            if pipeline_id == "finance.expense_evidence_review":
                request = {
                    "pipeline_id": pipeline_id,
                    "payload": {
                        "entity_id": entity_id,
                        "connector_request": {
                            "mode": "fetch",
                            "from_created_at": "REPLACE_WITH_WINDOW_START_UTC",
                            "to_created_at": "REPLACE_WITH_WINDOW_END_UTC",
                            "currency_minor_units": {
                                entity["functional_currency"]: 2,
                            },
                            "max_pages": 50,
                        },
                    },
                }
                required = [
                    "configure OPC_AIRWALLEX_CLIENT_ID and OPC_AIRWALLEX_API_KEY outside this JSON",
                    "configure OPC_AIRWALLEX_ENTITY_BINDINGS_JSON with the exact legal entity and account",
                    "configure OPC_AIRWALLEX_WEBHOOK_SECRET and TLS/IP-filtered ingress for the signed Spend webhook",
                    "run airwallex-webhook-process separately; webhook delivery never waits for refetch",
                    "use a Spend Read scoped key and replace the half-open UTC window",
                    "declare minor units for every possible billing and transaction currency",
                    "review rejected rows, receipts, business purpose and accounting mapping without posting",
                ]
            elif pipeline_id == "finance.bank_statement_close":
                wise_available = "wise.balance_statement" in entity_available_connectors
                request = {
                    "pipeline_id": pipeline_id,
                    "payload": {
                        "entity_id": entity_id,
                        "period": "REPLACE_WITH_YYYY-MM",
                        "connector_id": (
                            "wise.balance_statement" if wise_available else "file.bank_statement"
                        ),
                        "connector_request": (
                            {
                                "mode": "fetch",
                                "currency": entity["functional_currency"],
                                "interval_start": "REPLACE_WITH_PERIOD_START_UTC",
                                "interval_end": "REPLACE_WITH_NEXT_PERIOD_START_UTC",
                            }
                            if wise_available else {
                                "path": "REPLACE_WITH_BANK_CSV_OR_XLSX_PATH",
                                "default_currency": "REPLACE_WITH_CURRENCY_WHEN_SOURCE_OMITS_IT",
                                "account_reference": "REPLACE_WITH_MASKED_ACCOUNT_REFERENCE_WHEN_SOURCE_OMITS_IT",
                            }
                        ),
                    },
                }
                required = (
                    [
                        "configure OPC_WISE_ACCESS_TOKEN outside this JSON",
                        "configure OPC_WISE_ENTITY_BINDINGS_JSON with this legal entity's exact BUSINESS profile and currency balance",
                        "replace the period and its half-open UTC interval start/end",
                        "confirm personal-token jurisdiction eligibility or an approved Wise partner access contract",
                        "complete SCA outside the Box when Wise requires it, then review account mapping and running balances",
                    ]
                    if wise_available else [
                        "replace the period and bank CSV/XLSX path",
                        "provide currency and a masked account reference when the export omits them",
                        "confirm header mapping, legal entity and account scope",
                        "complete account-and-currency balance reconciliation before treating the close as confirmed",
                    ]
                )
            elif pipeline_id == "finance.trial_balance_review":
                if "xero.trial_balance" in entity_available_connectors:
                    request = {
                        "pipeline_id": pipeline_id,
                        "payload": {
                            "entity_id": entity_id,
                            "period": "REPLACE_WITH_YYYY-MM",
                            "connector_id": "xero.trial_balance",
                            "connector_request": {
                                "mode": "fetch",
                                "as_at": "REPLACE_WITH_YYYY-MM-DD_PERIOD_END",
                                "payments_only": False,
                            },
                        },
                    }
                    required = [
                        "configure OPC_XERO_ACCESS_TOKEN outside this JSON",
                        "configure OPC_XERO_ENTITY_BINDINGS_JSON with this Box entity's tenant_id and organisation_id",
                        "replace period and as_at; the as_at month must equal the Pipeline period",
                        "confirm the bound Xero Organisation id and base currency match this legal entity",
                        "review mapping and closing control totals; opening and period movements are not inferred",
                    ]
                else:
                    request = {
                        "pipeline_id": pipeline_id,
                        "payload": {
                            "entity_id": entity_id,
                            "period": "REPLACE_WITH_YYYY-MM",
                            "connector_id": "file.trial_balance",
                            "connector_request": {
                                "path": "REPLACE_WITH_TRIAL_BALANCE_CSV_OR_XLSX_PATH",
                                "default_currency": "REPLACE_WITH_CURRENCY_WHEN_SOURCE_OMITS_IT",
                            },
                        },
                    }
                    required = [
                        "replace the period and accounting-export CSV/XLSX path",
                        "provide currency when the export omits it",
                        "confirm the export covers one legal entity and one requested period",
                        "review account mapping and control totals before treating it as close evidence",
                    ]
            elif pipeline_id == "finance.accounting_close_review":
                request = {
                    "pipeline_id": pipeline_id,
                    "payload": {
                        "entity_id": entity_id,
                        "period": "REPLACE_WITH_YYYY-MM",
                        "general_ledger_connector_id": "file.general_ledger",
                        "general_ledger_connector_request": {
                            "path": "REPLACE_WITH_GENERAL_LEDGER_CSV_OR_XLSX_PATH",
                            "default_currency": "REPLACE_WITH_CURRENCY_WHEN_SOURCE_OMITS_IT",
                        },
                        "trial_balance_connector_id": "file.trial_balance",
                        "trial_balance_connector_request": {
                            "path": "REPLACE_WITH_TRIAL_BALANCE_CSV_OR_XLSX_PATH",
                            "default_currency": "REPLACE_WITH_CURRENCY_WHEN_SOURCE_OMITS_IT",
                        },
                        "account_mappings": [{
                            "account_code": "REPLACE_WITH_SOURCE_ACCOUNT_CODE",
                            "source_account_name": "REPLACE_WITH_EXACT_SOURCE_ACCOUNT_NAME",
                            "statement_group": "REPLACE_WITH_assets_liabilities_equity_revenue_or_expenses",
                            "statement_line_id": "REPLACE_WITH_STABLE_STATEMENT_LINE_ID",
                            "statement_line_name": "REPLACE_WITH_REVIEWED_STATEMENT_LINE_NAME",
                        }],
                    },
                }
                required = [
                    "replace the period and both accounting-export paths",
                    "provide currency when either export omits it",
                    "map every non-zero trial-balance account explicitly to one reviewed statement line",
                    "confirm journal balance, GL-to-trial movement, mapping completeness and accounting policy",
                ]
            elif pipeline_id == "finance.month_close_control":
                wise_available = "wise.balance_statement" in entity_available_connectors
                request = {
                    "pipeline_id": pipeline_id,
                    "payload": {
                        "entity_id": entity_id,
                        "period": "REPLACE_WITH_YYYY-MM",
                        "bank_connector_id": (
                            "wise.balance_statement" if wise_available else "file.bank_statement"
                        ),
                        "bank_connector_request": (
                            {
                                "mode": "fetch",
                                "currency": entity["functional_currency"],
                                "interval_start": "REPLACE_WITH_PERIOD_START_UTC",
                                "interval_end": "REPLACE_WITH_NEXT_PERIOD_START_UTC",
                            }
                            if wise_available else {
                                "path": "REPLACE_WITH_BANK_CSV_OR_XLSX_PATH",
                                "default_currency": "REPLACE_WITH_CURRENCY_WHEN_SOURCE_OMITS_IT",
                                "account_reference": "REPLACE_WITH_MASKED_ACCOUNT_REFERENCE_WHEN_SOURCE_OMITS_IT",
                            }
                        ),
                        "general_ledger_connector_id": "file.general_ledger",
                        "general_ledger_connector_request": {
                            "path": "REPLACE_WITH_GENERAL_LEDGER_CSV_OR_XLSX_PATH",
                            "default_currency": "REPLACE_WITH_CURRENCY_WHEN_SOURCE_OMITS_IT",
                        },
                        "trial_balance_connector_id": "file.trial_balance",
                        "trial_balance_connector_request": {
                            "path": "REPLACE_WITH_TRIAL_BALANCE_CSV_OR_XLSX_PATH",
                            "default_currency": "REPLACE_WITH_CURRENCY_WHEN_SOURCE_OMITS_IT",
                        },
                        "account_mappings": [{
                            "account_code": "REPLACE_WITH_SOURCE_ACCOUNT_CODE",
                            "source_account_name": "REPLACE_WITH_EXACT_SOURCE_ACCOUNT_NAME",
                            "statement_group": "REPLACE_WITH_assets_liabilities_equity_revenue_or_expenses",
                            "statement_line_id": "REPLACE_WITH_STABLE_STATEMENT_LINE_ID",
                            "statement_line_name": "REPLACE_WITH_REVIEWED_STATEMENT_LINE_NAME",
                        }],
                        "bank_gl_mappings": [{
                            "entity_id": entity_id,
                            "period": "REPLACE_WITH_YYYY-MM",
                            "account_masked": "REPLACE_WITH_MASKED_BANK_ACCOUNT",
                            "currency": "REPLACE_WITH_CURRENCY",
                            "gl_account_code": "REPLACE_WITH_EXPLICIT_CASH_ACCOUNT_CODE",
                            "bank_source_fingerprint": "REPLACE_WITH_CURRENT_BANK_SOURCE_FINGERPRINT",
                            "transaction_review": {
                                "status": "pending",
                                "reviewer_role": "REPLACE_WITH_REVIEWER_ROLE",
                                "rationale": "REPLACE_WITH_REVIEW_RATIONALE",
                                "evidence": ["REPLACE_WITH_TRANSACTION_REVIEW_EVIDENCE"],
                            },
                            "reconciling_items": [],
                        }],
                    },
                }
                required = [
                    (
                        "configure Wise outside this JSON and replace its half-open UTC statement interval plus both accounting source paths"
                        if wise_available else
                        "replace the period and all three entity-scoped source paths"
                    ),
                    "map every non-zero trial-balance account to a reviewed statement line",
                    "map every masked bank account and currency to one explicit GL cash account",
                    "bind transaction review and reconciling-item evidence to the current bank source fingerprint",
                    "obtain month-close control review; this template never posts or closes the period",
                ]
            elif pipeline_id == "finance.first_close_discovery":
                wise_available = "wise.balance_statement" in entity_available_connectors
                request = {
                    "pipeline_id": pipeline_id,
                    "payload": {
                        "entity_id": entity_id,
                        "period": "REPLACE_WITH_YYYY-MM",
                        "bank_connector_id": (
                            "wise.balance_statement" if wise_available else "file.bank_statement"
                        ),
                        "bank_connector_request": (
                            {
                                "mode": "fetch",
                                "currency": entity["functional_currency"],
                                "interval_start": "REPLACE_WITH_PERIOD_START_UTC",
                                "interval_end": "REPLACE_WITH_NEXT_PERIOD_START_UTC",
                            }
                            if wise_available else {
                                "path": "REPLACE_WITH_BANK_CSV_OR_XLSX_PATH",
                                "default_currency": "REPLACE_WITH_CURRENCY_WHEN_SOURCE_OMITS_IT",
                                "account_reference": "REPLACE_WITH_MASKED_ACCOUNT_REFERENCE_WHEN_SOURCE_OMITS_IT",
                            }
                        ),
                        "general_ledger_connector_id": "file.general_ledger",
                        "general_ledger_connector_request": {
                            "path": "REPLACE_WITH_GENERAL_LEDGER_CSV_OR_XLSX_PATH",
                            "default_currency": "REPLACE_WITH_CURRENCY_WHEN_SOURCE_OMITS_IT",
                        },
                        "trial_balance_connector_id": "file.trial_balance",
                        "trial_balance_connector_request": {
                            "path": "REPLACE_WITH_TRIAL_BALANCE_CSV_OR_XLSX_PATH",
                            "default_currency": "REPLACE_WITH_CURRENCY_WHEN_SOURCE_OMITS_IT",
                        },
                    },
                }
                required = [
                    (
                        "configure Wise outside this JSON and replace its half-open UTC statement interval plus both accounting source paths"
                        if wise_available else
                        "replace the period and all three entity-scoped source paths"
                    ),
                    "provide currency or masked account defaults only when the source omits them",
                    "run discovery to obtain exact source fingerprints and account inventories",
                    "review every generated statement and bank-to-GL mapping placeholder before month close",
                ]
            elif pipeline_id == "marketplace.channel_close":
                request = {
                    "pipeline_id": pipeline_id,
                    "payload": {
                        "entity_id": entity_id,
                        "connector_id": "REPLACE_WITH_ENABLED_MARKETPLACE_CONNECTOR",
                        "connector_request": {"path": "REPLACE_WITH_MARKETPLACE_XLSX_PATH"},
                        "platform_inventory": [{
                            "entity_id": entity_id,
                            "sku": "REPLACE_WITH_SKU",
                            "warehouse": "REPLACE_WITH_PLATFORM_WAREHOUSE",
                            "quantity": "REPLACE_WITH_NON_NEGATIVE_QUANTITY",
                            "evidence": ["REPLACE_WITH_PLATFORM_INVENTORY_EVIDENCE"],
                        }],
                        "ledger_inventory": [{
                            "entity_id": entity_id,
                            "sku": "REPLACE_WITH_SKU",
                            "warehouse": "REPLACE_WITH_PLATFORM_WAREHOUSE",
                            "quantity": "REPLACE_WITH_NON_NEGATIVE_QUANTITY",
                            "evidence": ["REPLACE_WITH_LEDGER_INVENTORY_EVIDENCE"],
                        }],
                        "tolerance": 0.01,
                    },
                }
                required = [
                    "select the Marketplace file or editable API Payload Connector",
                    "replace statement source and every platform/ledger inventory placeholder",
                    "scope all order, settlement and inventory evidence to this one entity",
                    "obtain source, contract, inventory, cutoff and valuation reviews before live use",
                ]
            elif pipeline_id == "commerce.channel_close":
                request = {
                    "pipeline_id": pipeline_id,
                    "payload": {
                        "entity_id": entity_id,
                        "connector_id": "REPLACE_WITH_ENABLED_COMMERCE_CONNECTOR",
                        "connector_request": {
                            "path": "REPLACE_WITH_CSV_OR_XLSX_PATH",
                            "default_channel": "REPLACE_WITH_CHANNEL_WHEN_SOURCE_OMITS_IT",
                        },
                        "tolerance": 0.01,
                    },
                }
                required = [
                    "select one enabled Commerce file or API-payload Connector",
                    "replace the source path/payload and optional default channel",
                    "ensure every accepted order and settlement belongs to this one entity",
                    "obtain source-mapping, revenue-cutoff, inventory-policy and sales-tax/nexus reviews before live use",
                ]
            elif pipeline_id == "game.channel_settlement_close":
                request = {
                    "pipeline_id": pipeline_id,
                    "payload": {
                        "entity_id": entity_id,
                        "connector_id": "REPLACE_WITH_ENABLED_GAME_SETTLEMENT_CONNECTOR",
                        "connector_request": {"path": "REPLACE_WITH_XLSX_PATH"},
                        "contract_mappings": [{
                            "settlement_id": "OPTIONAL_WHEN_BUSINESS_KEY_IS_UNIQUE",
                            "entity_id": entity_id,
                            "period": "REPLACE_WITH_YYYY-MM",
                            "game": "REPLACE_WITH_GAME_OR_PROJECT",
                            "channel": "REPLACE_WITH_CHANNEL_FROM_STATEMENT",
                            "currency": "REPLACE_WITH_CURRENCY",
                            "contract_basis": "REPLACE_WITH_CONTRACT_BASIS",
                            "contract_rate": "REPLACE_WITH_RATE_0_TO_1",
                            "contract_adjustments": 0,
                            "evidence": {
                                "source_reference": "REPLACE_WITH_CONTRACT_EVIDENCE_REFERENCE",
                                "captured_at": "REPLACE_WITH_ISO_DATE_OR_TIMESTAMP",
                            },
                        }],
                        "tolerance": 0.01,
                    },
                }
                required = [
                    "select one enabled App Store, Google Play or domestic game XLSX Connector",
                    "replace the statement path and every mapping placeholder",
                    "map every imported settlement exactly once to explicit contract evidence",
                    "obtain contract-mapping and principal-versus-agent review gates before live use",
                ]
            elif pipeline_id == "stripe.daily_close":
                request = {
                    "pipeline_id": pipeline_id,
                    "payload": {
                        "entity_id": entity_id,
                        "arrival_date_tolerance_days": 3,
                        "balance_request": {
                            "mode": "fetch", "created_gte": "REPLACE_WITH_UNIX_TIMESTAMP",
                            "created_lt": "REPLACE_WITH_UNIX_TIMESTAMP", "max_pages": 50,
                        },
                        "payout_request": {
                            "mode": "fetch", "created_gte": "REPLACE_WITH_UNIX_TIMESTAMP",
                            "created_lt": "REPLACE_WITH_UNIX_TIMESTAMP", "max_pages": 50,
                        },
                        "bank_transactions": [],
                    },
                }
                required = [
                    "configure OPC_STRIPE_RESTRICTED_KEY outside this JSON",
                    "replace both Unix timestamp placeholders",
                    "supply entity-scoped bank_transactions in integer minor units with evidence",
                ]
            elif pipeline_id == "dtc.shopify_stripe_daily_close":
                wise_available = "wise.balance_statement" in entity_available_connectors
                request = {
                    "pipeline_id": pipeline_id,
                    "payload": {
                        "entity_id": entity_id,
                        "include_test_orders": False,
                        "arrival_date_tolerance_days": 3,
                        "currency_minor_units": {"REPLACE_CURRENCY": "REPLACE_EXPONENT_0_TO_4"},
                        "shopify_request": {
                            "mode": "fetch", "shop_domain": "REPLACE.myshopify.com",
                            "created_at_gte": "REPLACE_WITH_ISO_TIMESTAMP",
                            "created_at_lt": "REPLACE_WITH_ISO_TIMESTAMP", "max_pages": 50,
                        },
                        "stripe_balance_request": {
                            "mode": "fetch", "created_gte": "REPLACE_WITH_UNIX_TIMESTAMP",
                            "created_lt": "REPLACE_WITH_UNIX_TIMESTAMP", "max_pages": 50,
                        },
                        "stripe_payout_request": {
                            "mode": "fetch", "created_gte": "REPLACE_WITH_UNIX_TIMESTAMP",
                            "created_lt": "REPLACE_WITH_UNIX_TIMESTAMP", "max_pages": 50,
                        },
                        "processor_links": [],
                        **(
                            {
                                "bank_connector_id": "wise.balance_statement",
                                "bank_connector_request": {
                                    "mode": "fetch",
                                    "currency": entity["functional_currency"],
                                    "interval_start": "REPLACE_WITH_PERIOD_START_UTC",
                                    "interval_end": "REPLACE_WITH_NEXT_PERIOD_START_UTC",
                                },
                            }
                            if wise_available else {"bank_transactions": []}
                        ),
                    },
                }
                required = [
                    (
                        "configure OPC_SHOPIFY_ADMIN_TOKEN, OPC_STRIPE_RESTRICTED_KEY, OPC_WISE_ACCESS_TOKEN and OPC_WISE_ENTITY_BINDINGS_JSON outside this JSON"
                        if wise_available else
                        "configure OPC_SHOPIFY_ADMIN_TOKEN and OPC_STRIPE_RESTRICTED_KEY outside this JSON"
                    ),
                    "replace store, time-window and currency exponent placeholders",
                    "supply explicit entity-scoped Shopify transaction to Stripe source evidence links",
                    (
                        "replace the Wise half-open UTC statement interval and review the bound BUSINESS profile/balance"
                        if wise_available else
                        "supply entity-scoped bank transactions in integer minor units with evidence"
                    ),
                    "obtain the configured Shopify and processor-link review gates before live use",
                ]
            elif pipeline_id == "dtc.shopify_stripe_month_close":
                request = {
                    "pipeline_id": pipeline_id,
                    "payload": {
                        "entity_id": entity_id,
                        "include_test_orders": False,
                        "currency_minor_units": {
                            entity["functional_currency"]: "REPLACE_EXPONENT_0_TO_4",
                        },
                        "shopify_monthly_request": {
                            "mode": "fetch",
                            "shop_domain": "REPLACE.myshopify.com",
                            "interval_start": "REPLACE_WITH_MONTH_START_UTC",
                            "interval_end": "REPLACE_WITH_NEXT_MONTH_START_UTC",
                            "max_pages": 50,
                        },
                        "stripe_balance_request": {
                            "mode": "fetch",
                            "created_gte": "REPLACE_WITH_SAME_MONTH_START_UNIX_TIMESTAMP",
                            "created_lt": "REPLACE_WITH_SAME_NEXT_MONTH_START_UNIX_TIMESTAMP",
                            "max_pages": 50,
                        },
                        "processor_links": [],
                    },
                }
                required = [
                    "configure OPC_SHOPIFY_ADMIN_TOKEN and OPC_STRIPE_RESTRICTED_KEY outside this JSON",
                    "run within 72 hours after month end so the Shopify close snapshot is admissible",
                    "replace Shopify UTC month bounds and exactly matching Stripe Unix bounds",
                    "supply explicit entity-scoped Shopify transaction to Stripe source evidence links",
                    "review tax-inclusive allocation and physical return authorization/receipt before approval",
                ]
            elif pipeline_id == "paypal.transaction_close":
                request = {
                    "pipeline_id": pipeline_id,
                    "payload": {
                        "entity_id": entity_id,
                        "paypal_request": {
                            "mode": "fetch",
                            "environment": "production",
                            "interval_start": "REPLACE_WITH_WINDOW_START_UTC",
                            "interval_end": "REPLACE_WITH_NEXT_WINDOW_START_UTC",
                            "page_size": 500,
                            "max_pages": 20,
                        },
                    },
                }
                required = [
                    "configure OPC_PAYPAL_CLIENT_ID and OPC_PAYPAL_CLIENT_SECRET outside this JSON",
                    "enable Transaction Search for the PayPal REST app and confirm reporting/search/read access",
                    "bind this one Box legal entity to the intended PayPal merchant account and credentials",
                    "replace the half-open UTC interval; one request may not exceed 31 days or 10,000 records",
                    "review T-code mapping, fees, refunds and reversals before any accounting use",
                ]
            elif pipeline_id == "amazon_seller.transaction_close":
                request = {
                    "pipeline_id": pipeline_id,
                    "payload": {
                        "entity_id": entity_id,
                        "amazon_seller_request": {
                            "mode": "fetch",
                            "environment": "production",
                            "marketplace_id": "REPLACE_WITH_BOUND_MARKETPLACE_ID",
                            "interval_start": "REPLACE_WITH_WINDOW_START_UTC",
                            "interval_end": "REPLACE_WITH_WINDOW_END_AT_LEAST_TWO_MINUTES_OLD_UTC",
                            "transaction_status": "RELEASED",
                            "max_pages": 20,
                        },
                    },
                }
                required = [
                    "configure OPC_AMAZON_SELLER_ENTITY_BINDINGS_JSON plus three distinct per-entity credential aliases outside this JSON and grant the Finance and Accounting role",
                    "bind this Box legal entity to the exact environment, seller ID, regional endpoint and allowlisted marketplace IDs; legacy root values are single-entity fetch compatibility only",
                    "replace the half-open UTC interval; one Box request may not exceed 31 days and its end must be at least two minutes old",
                    "review transaction/component mapping, deferred activity, Marketplace tax/fees and settlement completeness before accounting use",
                    "do not sum nested financial component levels or treat Finances events as complete order, bank or tax evidence",
                ]
            elif pipeline_id == "amazon_seller.marketplace_close":
                request = {
                    "pipeline_id": pipeline_id,
                    "payload": {
                        "entity_id": entity_id,
                        "amazon_seller_marketplace_request": {
                            "mode": "fetch",
                            "environment": "production",
                            "marketplace_id": "REPLACE_WITH_BOUND_MARKETPLACE_ID",
                            "interval_start": "REPLACE_WITH_WINDOW_START_UTC",
                            "interval_end": "REPLACE_WITH_WINDOW_END_AT_LEAST_TWO_MINUTES_OLD_UTC",
                            "orders_time_basis": "created",
                            "max_order_pages": 20,
                            "max_inventory_pages": 20,
                            "max_transaction_pages": 20,
                        },
                    },
                }
                required = [
                    "configure OPC_AMAZON_SELLER_ENTITY_BINDINGS_JSON plus three distinct per-entity credential aliases outside this JSON and grant only the required Sellers, Orders, FBA Inventory and Finance read roles",
                    "bind this Box legal entity to the exact environment, seller ID, regional endpoint and allowlisted marketplace IDs; legacy root values are single-entity fetch compatibility only",
                    "replace the half-open UTC interval; one Box request may not exceed 31 days and its end must be at least two minutes old",
                    "review order-to-Finances differences and FBA SKU scope candidates without assuming either source is complete",
                    "treat FBA Inventory as a current fetch-time observation, never as historical period-end inventory or inventory valuation",
                    "do not infer revenue, tax, bank settlement, COGS, inventory adjustment or posting from the three-source evidence",
                ]
            elif pipeline_id == "woocommerce.order_refund_close":
                request = {
                    "pipeline_id": pipeline_id,
                    "payload": {
                        "entity_id": entity_id,
                        "woocommerce_request": {
                            "mode": "fetch",
                            "interval_start": "REPLACE_WITH_WINDOW_START_UTC",
                            "interval_end": "REPLACE_WITH_NEXT_WINDOW_START_UTC",
                            "page_size": 100,
                            "max_pages": 100,
                        },
                    },
                }
                required = [
                    "configure OPC_WOOCOMMERCE_SITE_ORIGIN, OPC_WOOCOMMERCE_CONSUMER_KEY and OPC_WOOCOMMERCE_CONSUMER_SECRET outside this JSON",
                    "issue a read-only WooCommerce REST API key bound to the intended store operator",
                    "bind this one Box legal entity to the configured WooCommerce site and key",
                    "replace the half-open UTC interval; one request may not exceed 31 days or 10,000 records per collection",
                    "review order statuses, refund completeness, destination tax evidence and revenue policy before accounting use",
                ]
            elif pipeline_id == "commerce.shipbob_fulfillment_close":
                request = {
                    "pipeline_id": pipeline_id,
                    "payload": {
                        "entity_id": entity_id,
                        "shipbob_request": {
                            "mode": "fetch",
                            "environment": "production",
                            "interval_start": "REPLACE_WITH_WINDOW_START_UTC",
                            "interval_end": "REPLACE_WITH_WINDOW_END_UTC",
                            "page_size": 100,
                            "max_pages": 50,
                        },
                    },
                }
                required = [
                    "configure OPC_SHIPBOB_ENTITY_BINDINGS_JSON plus a distinct per-entity token alias outside this JSON with channels_read, orders_read, fulfillments_read and returns_read only",
                    "bind this Box legal entity to the exact ShipBob environment and channel; legacy OPC_SHIPBOB_ACCESS_TOKEN is single-entity fetch compatibility only",
                    "replace the half-open UTC interval; one incremental request may not exceed 31 days",
                    "review source-order mapping and fulfillment invoices before using them as accounting evidence",
                    "review every restock, quarantine or disposal candidate; this Pipeline never changes inventory or posts",
                ]
            else:
                continue
            templates.append({
                "template_id": f"{pipeline_id}:{entity_id}",
                "pipeline_id": pipeline_id,
                "entity_scope": pipeline["entity_scope"],
                "entity_id": entity_id,
                "runnable_without_configuration": False,
                "required_configuration": required,
                "request": request,
            })
    return {
        "schema_version": 1,
        "secret_values_included": False,
        "templates": templates,
        "control_note": (
            "Templates are intentionally fail-closed. Replace every placeholder, validate source-to-entity "
            "mapping and run fixture/shadow reconciliation before enabling a schedule."
        ),
    }


def _build_pipeline_run_policy(pipelines: list[dict[str, Any]]) -> dict[str, Any]:
    """Compile the durable, secret-free control boundary for Pipeline attempts."""
    return {
        "schema_version": 1,
        "persistence": "explicit_opt_in",
        "default_storage": ".opc-finance-data/pipeline-runs/pipeline_runs.jsonl",
        "runtime_scope": "box_runtime_fingerprint",
        "ledger": {
            "format": "append_only_jsonl",
            "integrity": "sha256_hash_chain",
            "integrity_limit": "tamper_evident_not_immutable",
            "cross_process_locking": True,
            "fsync_each_event": True,
            "directory_mode": "0700",
            "file_mode": "0600",
        },
        "event_types": [
            "PIPELINE_SCHEDULE_CLAIMED", "PIPELINE_RUN_RECORDED", "PIPELINE_RUN_REVIEWED",
        ],
        "read_models": [
            "attempt_history", "schedule_occurrence_status",
            "unresolved_review_queue", "integrity_proof",
        ],
        "backup_and_restore": {
            "backup_scope": "complete_physical_ledger_all_box_fingerprints",
            "backup_overwrite_allowed": False,
            "backup_manifest_includes": [
                "ledger_sha256", "event_count", "chain_head", "created_by",
            ],
            "restore_target_must_be_empty": True,
            "restore_merge_allowed": False,
            "restore_overwrite_allowed": False,
            "restore_receipt_required": True,
            "http_restore_enabled": False,
        },
        "review_decisions": ["approved", "rejected", "needs_more_evidence"],
        "release_candidate_rule": (
            "pipeline_ready AND every required review gate currently approved"
        ),
        "release_candidate_is_external_authorization": False,
        "persisted_data": [
            "runtime/request/result fingerprints",
            "attempt and idempotency lineage",
            "connector and service control summaries",
            "bounded lineage identifiers and counts",
            "human review decisions, rationale and evidence references",
        ],
        "never_automatically_persisted": [
            "raw Pipeline request",
            "raw Connector response",
            "full Pipeline result",
            "credential or environment secret value",
        ],
        "external_actions_performed": False,
        "posting_performed": False,
        "pipelines": [{
            "pipeline_id": item["pipeline_id"],
            "required_review_gates": list(item.get("review_gates") or []),
            "recordable": bool(
                item.get("implementation_status") == "executable"
                and item.get("external_actions") is False
            ),
        } for item in pipelines],
        "operator_warning": (
            "Rationale and evidence references are operator-authored control metadata; "
            "do not paste credentials, raw financial rows or personal data into them."
        ),
    }


def _build_pipeline_schedule_template(
    pipeline_request_templates: dict[str, Any],
) -> dict[str, Any]:
    """Compile an editable, disabled template for the strict runtime scheduler."""
    jobs = []
    excluded_templates = []
    for item in pipeline_request_templates.get("templates") or []:
        entity_id = item.get("entity_id")
        if not isinstance(entity_id, str) or not entity_id:
            excluded_templates.append({
                "template_id": item.get("template_id"),
                "pipeline_id": item.get("pipeline_id"),
                "entity_scope": item.get("entity_scope"),
                "reason": (
                    "schedule jobs require one explicit legal entity; create an entity-scoped "
                    "request before scheduling this management template"
                ),
            })
            continue
        jobs.append({
            "job_id": item["template_id"],
            "enabled": False,
            "pipeline_id": item["pipeline_id"],
            "entity_id": entity_id,
            "request_file": "REQUIRED_RELATIVE_REQUEST_JSON",
            "request_fingerprint": None,
            "cadence": {"kind": "daily", "local_time": "REQUIRED_HH_MM"},
            "execution_window_minutes": 60,
            "max_attempts": 3,
            "retry_delay_minutes": 15,
            "lease_seconds": 900,
            "operator": "REQUIRED_OPERATOR_PRINCIPAL",
            "alert_owner": "REQUIRED_ALERT_OWNER",
            "approved_by": None,
            "approved_at": None,
            "approval_fingerprint": None,
        })
    return {
        "schema_version": 2,
        "template_only": True,
        "timezone": "REQUIRED_IANA_TIMEZONE",
        "jobs": jobs,
        "excluded_templates": excluded_templates,
        "installation_status": "not_installed",
        "execution_contract": {
            "accepted_cadences": ["daily", "weekly", "monthly"],
            "request_files_must_remain_inside_schedule_directory": True,
            "enabled_job_requires_explicit_approval": True,
            "approval_is_bound_to_operational_job_fingerprint": True,
            "approval_is_bound_to_request_content_fingerprint": True,
            "operator_must_match_runtime_actor": True,
            "management_template_without_entity_is_never_auto_scheduled": True,
            "atomic_occurrence_lease": True,
            "lease_event_persisted_in_pipeline_ledger": True,
            "retry_only_when_pipeline_marks_retryable": True,
            "posting_performed": False,
            "external_actions_performed": False,
        },
        "cli": {
            "inspect": "opc-finance-box pipeline-schedule-inspect BOX.json schedule.json",
            "run": "opc-finance-box pipeline-schedule-run BOX.json schedule.json --actor OPERATOR",
            "observability": "opc-finance-box pipeline-observability BOX.json --schedule schedule.json",
        },
        "server_environment_reference": "OPC_FINANCE_PIPELINE_SCHEDULE_FILE",
        "control_note": (
            "Copy and edit this template; it is intentionally invalid until timezone, request files, "
            "owners and approval are supplied. The product never installs cron or approves a job."
        ),
    }


def build_pipeline_runtime_catalog(
    runtime: BoxRuntime,
    registry: PackServiceRegistry | None = None,
) -> dict[str, Any]:
    """Return the current Box's executable Pipeline catalog and secret-free starters."""
    selected_registry = registry or build_default_service_registry()
    context = build_box_context(runtime)
    capabilities = {
        capability
        for values in context["capability_groups"].values()
        for capability in values
    }
    services = selected_registry.catalog(runtime)
    connectors = build_box_connector_registry(runtime).catalog(runtime)
    pipelines = _enabled_pipelines(capabilities, services, connectors, context["entities"])
    templates = _build_pipeline_request_templates(
        pipelines, context["entities"], context["scope"]["reporting_currency"],
    )
    return {
        "schema_version": 1,
        "runtime_fingerprint": context["runtime"]["fingerprint"],
        "pipelines": pipelines,
        "request_templates": templates,
        "control_boundary": {
            "templates_include_secrets": False,
            "preflight_dispatches_sources": False,
            "recorded_runs_are_candidate_only": True,
            "external_authorization_inferred": False,
        },
    }


def preflight_pipeline_request(
    runtime: BoxRuntime,
    request: dict[str, Any],
    registry: PackServiceRegistry | None = None,
) -> dict[str, Any]:
    """Validate a request against the enabled catalog without accessing any source."""
    if not isinstance(request, dict):
        raise ValueError("Pipeline preflight request must be a JSON object")
    catalog = build_pipeline_runtime_catalog(runtime, registry)
    enabled = {item["pipeline_id"]: item for item in catalog["pipelines"]}
    pipeline_id = request.get("pipeline_id")
    blockers: list[str] = []
    if not isinstance(pipeline_id, str) or not pipeline_id:
        blockers.append("pipeline_id is required")
        pipeline = None
    else:
        pipeline = enabled.get(pipeline_id)
        if pipeline is None:
            blockers.append("pipeline_id is not enabled by the current Box")
        elif pipeline.get("implementation_status") == "blocked_connector_binding":
            blockers.append("pipeline has no legal entity with all required Connector bindings")
        elif pipeline.get("implementation_status") != "executable":
            blockers.append("pipeline is missing an enabled provider")
    payload = request.get("payload")
    if not isinstance(payload, dict):
        blockers.append("payload must be a JSON object")
        payload = {}

    placeholder_paths: list[str] = []
    forbidden_secret_paths: list[str] = []
    secret_key_markers = (
        "secret", "token", "password", "apikey", "privatekey",
        "credential", "authorization",
    )

    def inspect(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                child = f"{path}.{key}" if path else str(key)
                normalized_key = "".join(
                    character for character in str(key).lower()
                    if character.isalnum()
                )
                if any(marker in normalized_key for marker in secret_key_markers):
                    forbidden_secret_paths.append(child)
                inspect(nested, child)
        elif isinstance(value, list):
            for index, nested in enumerate(value):
                inspect(nested, f"{path}[{index}]")
        elif isinstance(value, str) and (
            "REPLACE_" in value or value.startswith("OPTIONAL_WHEN_")
        ):
            placeholder_paths.append(path)

    inspect(request, "request")
    if placeholder_paths:
        blockers.append("request still contains fail-closed placeholders")
    if forbidden_secret_paths:
        blockers.append("request JSON must not contain secret or token fields; use server environment configuration")

    entity_ids = {entity.entity_id for entity in runtime.entities.all()}
    entity_id = payload.get("entity_id")
    if entity_id is not None and entity_id not in entity_ids:
        blockers.append("payload.entity_id is not configured in the current Box")
    if pipeline and pipeline.get("entity_scope") == "statutory" and not entity_id:
        blockers.append("statutory Pipeline requires payload.entity_id")
    if (
        pipeline
        and pipeline.get("entity_scope") == "statutory"
        and isinstance(entity_id, str)
        and entity_id in entity_ids
        and entity_id not in set(pipeline.get("eligible_entity_ids") or [])
    ):
        blockers.append("Pipeline Connector requirements are not bound to payload.entity_id")
    requested_entity_ids = payload.get("entity_ids")
    if requested_entity_ids is not None:
        if (
            not isinstance(requested_entity_ids, list)
            or any(not isinstance(item, str) or not item for item in requested_entity_ids)
        ):
            blockers.append("payload.entity_ids must be a list of configured entity ids")
        else:
            if len(requested_entity_ids) != len(set(requested_entity_ids)):
                blockers.append("payload.entity_ids must not contain duplicates")
            if set(requested_entity_ids) - entity_ids:
                blockers.append("payload.entity_ids contains an entity not configured in the current Box")
    if pipeline_id == "finance.multi_entity_month_close_portfolio":
        if not isinstance(requested_entity_ids, list) or len(requested_entity_ids) < 2:
            blockers.append("multi-entity month-close portfolio requires at least two entity_ids")
        if not isinstance(payload.get("entity_close_controls"), list):
            blockers.append("payload.entity_close_controls must be a list")
        if not isinstance(payload.get("fx_rates"), dict):
            blockers.append("payload.fx_rates must be a JSON object")

    connector_fields = (
        "connector_id", "bank_connector_id", "general_ledger_connector_id",
        "trial_balance_connector_id",
    )
    provided_connectors = {
        field: payload.get(field) for field in connector_fields if payload.get(field) is not None
    }
    named_source_contracts = {
        "finance.accounting_close_review": (
            ("general_ledger_connector_id", "general_ledger_connector_request"),
            ("trial_balance_connector_id", "trial_balance_connector_request"),
        ),
        "finance.first_close_discovery": (
            ("bank_connector_id", "bank_connector_request"),
            ("general_ledger_connector_id", "general_ledger_connector_request"),
            ("trial_balance_connector_id", "trial_balance_connector_request"),
        ),
        "finance.month_close_control": (
            ("bank_connector_id", "bank_connector_request"),
            ("general_ledger_connector_id", "general_ledger_connector_request"),
            ("trial_balance_connector_id", "trial_balance_connector_request"),
        ),
    }
    for connector_field, request_field in named_source_contracts.get(str(pipeline_id), ()):
        if not isinstance(payload.get(connector_field), str) or not payload.get(connector_field):
            blockers.append(f"payload.{connector_field} is required")
        if not isinstance(payload.get(request_field), dict):
            blockers.append(f"payload.{request_field} must be a JSON object")
    if (
        pipeline_id == "dtc.shopify_stripe_daily_close"
        and payload.get("bank_connector_id") is not None
        and not isinstance(payload.get("bank_connector_request"), dict)
    ):
        blockers.append("payload.bank_connector_request must be a JSON object")
    if pipeline_id == "dtc.shopify_stripe_month_close":
        if not isinstance(payload.get("shopify_monthly_request"), dict):
            blockers.append("payload.shopify_monthly_request must be a JSON object")
        if not isinstance(payload.get("stripe_balance_request"), dict):
            blockers.append("payload.stripe_balance_request must be a JSON object")
        if not isinstance(payload.get("processor_links"), list):
            blockers.append("payload.processor_links must be a JSON array")
    if (
        pipeline_id == "commerce.shipbob_fulfillment_close"
        and not isinstance(payload.get("shipbob_request"), dict)
    ):
        blockers.append("payload.shipbob_request must be a JSON object")
    if (
        pipeline_id == "paypal.transaction_close"
        and not isinstance(payload.get("paypal_request"), dict)
    ):
        blockers.append("payload.paypal_request must be a JSON object")
    if (
        pipeline_id == "woocommerce.order_refund_close"
        and not isinstance(payload.get("woocommerce_request"), dict)
    ):
        blockers.append("payload.woocommerce_request must be a JSON object")
    if (
        pipeline_id == "amazon_seller.transaction_close"
        and not isinstance(payload.get("amazon_seller_request"), dict)
    ):
        blockers.append("payload.amazon_seller_request must be a JSON object")
    if (
        pipeline_id == "amazon_seller.marketplace_close"
        and not isinstance(payload.get("amazon_seller_marketplace_request"), dict)
    ):
        blockers.append("payload.amazon_seller_marketplace_request must be a JSON object")
    if pipeline and provided_connectors:
        allowed = set(pipeline.get("required_connectors") or []) | set(
            pipeline.get("required_connectors_any") or []
        ) | set(pipeline.get("optional_connectors") or [])
        role_contract = {
            "bank_connector_id": {"file.bank_statement", "wise.balance_statement"},
            "general_ledger_connector_id": {"file.general_ledger"},
            "trial_balance_connector_id": {"file.trial_balance"},
        }
        for field, connector_id in provided_connectors.items():
            if field in role_contract and connector_id not in role_contract[field]:
                blockers.append(f"payload.{field} is not allowed by this Pipeline")
            elif allowed and connector_id not in allowed:
                blockers.append(f"payload.{field} is not allowed by this Pipeline")
            elif (
                isinstance(entity_id, str)
                and entity_id in entity_ids
                and connector_id not in set(
                    (pipeline.get("available_connectors_by_entity") or {}).get(entity_id, [])
                )
            ):
                blockers.append(
                    f"payload.{field} Connector is not bound to payload.entity_id"
                )

    unique_blockers = list(dict.fromkeys(blockers))
    return {
        "schema_version": 1,
        "pipeline_id": pipeline_id if isinstance(pipeline_id, str) else None,
        "enabled": pipeline is not None,
        "ready_to_dispatch": not unique_blockers,
        "blockers": unique_blockers,
        "placeholder_paths": placeholder_paths,
        "forbidden_secret_paths": forbidden_secret_paths,
        "required_review_gates": list((pipeline or {}).get("review_gates") or []),
        "runtime_fingerprint": catalog["runtime_fingerprint"],
        "dispatch_performed": False,
        "source_access_performed": False,
        "state_changed": False,
        "external_actions_performed": False,
    }


def _build_runtime_security_policy(connectors: list[dict[str, Any]]) -> dict[str, Any]:
    airwallex_webhook_enabled = any(
        item.get("connector_id") == "airwallex.approved_expenses" for item in connectors
    )
    return {
        "schema_version": 1,
        "server_binding_default": "127.0.0.1",
        "non_loopback_requires_authentication": True,
        "authentication_modes": [
            "anonymous_loopback", "legacy_admin_token", "role_policy",
        ],
        "role_policy": {
            "raw_tokens_stored": False,
            "token_fingerprint": "sha256",
            "minimum_raw_token_length": 32,
            "policy_file_mode": "0600",
            "roles": ["reader", "operator", "reviewer", "admin"],
            "operator_includes_reviewer": False,
            "reviewer_includes_operator": False,
            "admin_includes_all_roles": True,
        },
        "route_classes": {
            "read_api": "reader",
            "service_and_pipeline_dispatch": "operator",
            "pipeline_run_record": "operator",
            "pipeline_schedule_inspect": "reader",
            "pipeline_observability_export": "reader",
            "pipeline_schedule_run": "operator",
            "pipeline_run_review": "reviewer",
            "other_workbench_mutation": "admin",
            "airwallex_spend_webhook_intake": "hmac_sha256_raw_body",
        },
        "authenticated_actor_source": "principal_id",
        "request_actor_override_allowed_when_authenticated": False,
        "public_api_paths": [
            "/api/health",
            *(["/api/webhooks/airwallex/spend"] if airwallex_webhook_enabled else []),
        ],
        "airwallex_spend_webhook": {
            "enabled_for_box": airwallex_webhook_enabled,
            "path": "/api/webhooks/airwallex/spend",
            "bearer_authentication_used": False,
            "authentication": "hmac_sha256_exact_timestamp_plus_raw_body",
            "signature_headers": ["x-timestamp", "x-signature"],
            "replay_tolerance_seconds": 300,
            "maximum_body_bytes": 1048576,
            "acknowledgement_after_durable_append": True,
            "acknowledgement_waits_for_provider_refetch": False,
            "duplicate_event_ids_are_idempotent": True,
            "event_id_body_conflicts_fail_closed": True,
            "entity_binding": "exact_legal_entity_id_and_account_id",
            "worker_action": "read_only_expense_refetch_then_human_review_candidate",
            "worker_shadow_observation": (
                "optional_private_amount_free_output_with_limit_1;_binds_complete_"
                "pipeline_result_SHA256;_independent_source_evidence_required_separately"
            ),
            "processing_lease_seconds": 300,
            "quarantine_after_failed_attempts": 3,
            "quarantine_resolution": "reviewer_retry_or_dismiss_with_rationale_and_evidence",
            "expense_claims_created": False,
            "posting_performed": False,
            "payment_performed": False,
        },
        "response_cache_policy": "no-store",
        "tls_included": False,
        "control_note": (
            "Built-in authentication is a local/runtime boundary; remote or multi-user deployment "
            "still requires TLS, network controls, identity governance and external audit retention."
        ),
    }


def _build_deployment_environment_contract(
    context: dict[str, Any], connectors: list[dict[str, Any]],
) -> dict[str, Any]:
    connector_env = sorted({
        name
        for connector in connectors
        for name in (connector.get("credential_env") or [])
        if isinstance(name, str) and name
    })
    selected_pack_ids = {
        str(item.get("id") or "") for item in context.get("packs", [])
        if isinstance(item, dict)
    }
    entity_ids_by_pack: dict[str, set[str]] = {}
    for connector in connectors:
        pack_id = str(connector.get("pack_id") or "")
        if not pack_id:
            continue
        entity_ids_by_pack.setdefault(pack_id, set()).update(
            str(entity_id) for entity_id in (connector.get("entity_ids") or [])
            if isinstance(entity_id, str) and entity_id
        )
    entity_binding_catalog = {
        "connector.paypal": {
            "binding_environment_name": PAYPAL_BINDINGS_ENV,
            "selected_entity_fields": [
                "environment", "app_id", "account_id",
                "client_id_env", "client_secret_env",
            ],
            "dynamic_secret_alias_fields": ["client_id_env", "client_secret_env"],
        },
        "connector.woocommerce": {
            "binding_environment_name": WOOCOMMERCE_BINDINGS_ENV,
            "selected_entity_fields": [
                "site_origin", "key_permission", "consumer_key_env",
                "consumer_secret_env",
            ],
            "dynamic_secret_alias_fields": [
                "consumer_key_env", "consumer_secret_env",
            ],
        },
        "connector.shipbob": {
            "binding_environment_name": SHIPBOB_BINDINGS_ENV,
            "selected_entity_fields": ["environment", "channel_id", "token_env"],
            "dynamic_secret_alias_fields": ["token_env"],
        },
        "connector.amazon_seller": {
            "binding_environment_name": AMAZON_SELLER_BINDINGS_ENV,
            "selected_entity_fields": [
                "environment", "region", "seller_id", "marketplace_ids",
                "client_id_env", "client_secret_env", "refresh_token_env",
            ],
            "dynamic_secret_alias_fields": [
                "client_id_env", "client_secret_env", "refresh_token_env",
            ],
        },
    }
    entity_credential_binding_contracts = []
    for pack_id in sorted(selected_pack_ids & set(entity_binding_catalog)):
        template = entity_binding_catalog[pack_id]
        entity_credential_binding_contracts.append({
            "pack_id": pack_id,
            "binding_environment_name": template["binding_environment_name"],
            "entity_ids": sorted(entity_ids_by_pack.get(pack_id, set())),
            "selected_entity_fields": list(template["selected_entity_fields"]),
            "dynamic_secret_alias_fields": list(
                template["dynamic_secret_alias_fields"]
            ),
            "dynamic_secret_alias_count_per_entity": len(
                template["dynamic_secret_alias_fields"]
            ),
            "binding_contains_raw_secret_values": False,
            "dynamic_alias_values_injected_separately": True,
            "selected_entity_slice_only_fingerprinted": True,
            "unbound_or_incomplete_entity_fails_closed": True,
            "legacy_root_environment_supported_for_single_entity_fetch_only": True,
            "legacy_root_environment_unlocks_access_receipt": False,
            "multi_entity_legacy_fallback_allowed": False,
        })
    binding_env = sorted(
        item["binding_environment_name"]
        for item in entity_credential_binding_contracts
    )
    variables = [
        {
            "name": "OPC_FINANCE_BOX_CONFIG", "classification": "read_only_config_path",
            "required_for_production": True, "secret": False, "default": None,
            "constraints": ["absolute_path", "regular_file", "mounted_read_only"],
        },
        {
            "name": "OPC_FINANCE_PACKS_ROOT", "classification": "read_only_pack_catalog_path",
            "required_for_production": False, "secret": False,
            "default": "wheel_bundled_pack_catalog",
            "constraints": ["absolute_path_when_overridden", "directory", "mounted_read_only"],
        },
        {
            "name": "OPC_FINANCE_DATA_DIR", "classification": "persistent_runtime_data_path",
            "required_for_production": True, "secret": False, "default": None,
            "constraints": ["absolute_path", "directory", "writable_by_service_user", "encrypted_volume_recommended"],
        },
        {
            "name": "OPC_FINANCE_HOST", "classification": "network_binding",
            "required_for_production": True, "secret": False, "default": "127.0.0.1",
            "constraints": ["non_loopback_requires_authentication", "public_internet_binding_forbidden_without_reverse_proxy"],
        },
        {
            "name": "OPC_FINANCE_PORT", "classification": "network_port",
            "required_for_production": False, "secret": False, "default": "8765",
            "constraints": ["integer_1_65535"],
        },
        {
            "name": "SETTLEMENT_MVP_PORT", "classification": "deprecated_network_port_alias",
            "required_for_production": False, "secret": False, "default": None,
            "constraints": ["integer_1_65535", "ignored_when_OPC_FINANCE_PORT_is_set"],
        },
        {
            "name": "OPC_FINANCE_API_AUTH_FILE", "classification": "read_only_auth_policy_path",
            "required_for_production": True, "secret": False, "default": None,
            "constraints": ["absolute_path", "regular_file", "mode_0600", "token_hashes_only", "mutually_exclusive_with_legacy_token"],
        },
        {
            "name": "OPC_TAX_APPLICABILITY_REVIEW_DIR",
            "classification": "read_only_private_tax_review_directory",
            "required_for_production": False, "secret": False, "default": None,
            "constraints": [
                "absolute_path", "directory", "mounted_read_only",
                "files_named_exact_entity_id_dot_json", "private_contents_never_returned",
            ],
        },
        {
            "name": "OPC_TAX_APPLICABILITY_REGISTRY_RECEIPT",
            "classification": "read_only_private_tax_registry_receipt_path",
            "required_for_production": False,
            "required_when_tax_review_directory_configured": True,
            "secret": False,
            "default": None,
            "constraints": [
                "absolute_path", "regular_file", "mounted_read_only", "mode_0600",
                "must_match_review_directory_and_runtime_fingerprint",
                "does_not_grant_filing_authorization",
            ],
        },
        {
            "name": "OPC_CONNECTOR_SHADOW_REVIEW_DIR",
            "classification": "read_only_private_connector_shadow_review_directory",
            "required_for_production": False,
            "required_for_network_connector_stable_evidence": True,
            "secret": False,
            "default": None,
            "constraints": [
                "absolute_path", "directory", "mounted_read_only", "mode_0700",
                "schema_v2_real_anonymized_reviews_only", "mode_0600_files",
                "current_independent_passed_review_required",
                "selected_network_connector_pack_coverage_required",
                "duplicate_entity_period_pipeline_scope_fails_closed",
                "paths_file_names_actors_evidence_and_values_never_returned",
                "does_not_grant_stable_promotion_or_external_action_authorization",
            ],
        },
        {
            "name": "OPC_PILOT_READINESS_REVIEW",
            "classification": "read_only_private_pilot_review_path",
            "required_for_production": False,
            "required_before_bounded_shadow": True,
            "secret": False,
            "default": None,
            "constraints": [
                "absolute_path", "regular_file", "mounted_read_only", "mode_0600",
                "must_match_runtime_fingerprint_entities_domains_and_connectors",
                "reviewer_separate_from_preparer_and_operator",
                "does_not_grant_posting_payment_or_filing_authorization",
            ],
        },
        {
            "name": "OPC_PILOT_DATA_HANDOFF_REVIEW",
            "classification": "read_only_private_pilot_data_handoff_review_path",
            "required_for_production": False,
            "required_before_real_data_intake": True,
            "secret": False,
            "default": None,
            "constraints": [
                "absolute_path", "regular_file", "mounted_read_only", "mode_0600",
                "requires_matching_OPC_PILOT_READINESS_REVIEW",
                "must_match_runtime_fingerprint_entities_domains_and_period",
                "private_source_manifest_hashes_never_returned",
                "does_not_copy_or_import_source_files",
                "does_not_grant_posting_payment_close_or_filing_authorization",
            ],
        },
        {
            "name": "OPC_PILOT_SHADOW_RUN_REGISTRATION",
            "classification": "read_only_private_pilot_shadow_run_registration_path",
            "required_for_production": False,
            "required_before_first_shadow_observation": True,
            "secret": False,
            "default": None,
            "constraints": [
                "absolute_path", "regular_file", "mounted_read_only", "mode_0600",
                "requires_matching_OPC_PILOT_DATA_HANDOFF_REVIEW",
                "requires_matching_OPC_PILOT_READINESS_REVIEW",
                "requires_current_pipeline_run_ledger",
                "must_cover_every_entity_for_exact_handoff_period",
                "attempt_ids_and_result_fingerprints_never_returned",
                "does_not_grant_posting_payment_close_or_filing_authorization",
            ],
        },
        {
            "name": "OPC_PILOT_SHADOW_OBSERVATION_REVIEW",
            "classification": "read_only_private_pilot_shadow_observation_review_path",
            "required_for_production": False,
            "required_before_next_shadow_period": True,
            "secret": False,
            "default": None,
            "constraints": [
                "absolute_path", "regular_file", "mounted_read_only", "mode_0600",
                "requires_matching_registration_handoff_readiness_and_pipeline_ledger",
                "requires_exact_entity_report_directory",
                "requires_portfolio_review_for_multi_entity_box",
                "actors_attempt_ids_fingerprints_and_financial_values_never_returned",
                "does_not_grant_pack_promotion_posting_payment_close_or_filing_authorization",
            ],
        },
        {
            "name": "OPC_PILOT_SHADOW_ENTITY_REPORT_DIR",
            "classification": "read_only_private_entity_shadow_report_directory",
            "required_for_production": False,
            "required_with_pilot_shadow_observation_review": True,
            "secret": False,
            "default": None,
            "constraints": [
                "absolute_path", "directory", "mounted_read_only",
                "files_named_exact_entity_id_dot_json", "mode_0600_contents",
                "unexpected_entries_fail_closed", "raw_financial_values_never_returned",
            ],
        },
        {
            "name": "OPC_PILOT_SHADOW_PORTFOLIO_REVIEW",
            "classification": "read_only_private_portfolio_shadow_review_path",
            "required_for_production": False,
            "required_for_multi_entity_pilot_shadow_observation": True,
            "secret": False,
            "default": None,
            "constraints": [
                "absolute_path", "regular_file", "mounted_read_only", "mode_0600",
                "must_bind_exact_registered_entity_attempt_set",
                "not_allowed_for_single_entity_box",
                "financial_values_never_returned",
            ],
        },
        {
            "name": "OPC_PILOT_SHADOW_SERIES_REVIEW",
            "classification": "read_only_private_pilot_shadow_series_review_path",
            "required_for_production": False,
            "required_before_stable_promotion_evidence_preparation": True,
            "secret": False,
            "default": None,
            "constraints": [
                "absolute_path", "regular_file", "mounted_read_only", "mode_0600",
                "requires_matching_consecutive_period_evidence_root",
                "requires_current_pipeline_run_ledger",
                "minimum_two_consecutive_calendar_periods",
                "paths_actors_hashes_attempt_ids_and_values_never_returned",
                "does_not_grant_pack_promotion_posting_payment_close_or_filing_authorization",
            ],
        },
        {
            "name": "OPC_PILOT_SHADOW_SERIES_EVIDENCE_ROOT",
            "classification": "read_only_private_pilot_shadow_series_evidence_directory",
            "required_for_production": False,
            "required_with_pilot_shadow_series_review": True,
            "secret": False,
            "default": None,
            "constraints": [
                "absolute_path", "directory", "mounted_read_only",
                "contains_only_yyyy_mm_directories", "two_to_twenty_four_periods",
                "consecutive_calendar_months", "exact_private_period_layout",
                "unexpected_entries_fail_closed", "raw_financial_values_never_returned",
            ],
        },
        {
            "name": "OPC_ACTIVATION_WORKSPACE_ROOT",
            "classification": "read_only_private_activation_workspace_root",
            "required_for_production": False,
            "required_for_monthly_progress_projection": True,
            "secret": False,
            "default": None,
            "constraints": [
                "absolute_path", "directory", "mounted_read_only", "mode_0700",
                "must_match_current_box_activation_workspace",
                "monthly_get_does_not_create_runbook_files",
                "paths_actors_evidence_hashes_and_values_never_returned",
                "does_not_infer_period_completion_or_unlock_evidence_gates",
            ],
        },
        {
            "name": "OPC_STABLE_PROMOTION_ROOT",
            "classification": "read_only_private_stable_promotion_ledger_directory",
            "required_for_production": False,
            "required_to_project_stable_candidate_approvals": True,
            "secret": False,
            "default": None,
            "constraints": [
                "absolute_path", "directory", "mounted_read_only", "mode_0700",
                "sha256_hash_chain_must_verify", "mode_0600_ledger_and_lock",
                "ledger_and_lock_must_not_be_hard_linked",
                "current_box_pack_and_version_binding_required",
                "latest_assessment_per_pack_controls_projection",
                "paths_assessment_ids_actors_evidence_and_values_never_returned",
                "does_not_modify_pack_manifest_or_authorize_external_actions",
            ],
        },
        {
            "name": "OPC_FINANCE_API_TOKEN", "classification": "legacy_secret",
            "required_for_production": False, "secret": True, "default": None,
            "constraints": ["minimum_32_characters", "legacy_single_admin_only", "prefer_auth_file", "never_commit"],
        },
        {
            "name": "OPC_FINANCE_PIPELINE_SCHEDULE_FILE", "classification": "read_only_schedule_path",
            "required_for_production": False, "secret": False, "default": None,
            "constraints": ["absolute_path", "schema_version_2", "mounted_read_only", "approved_request_fingerprints"],
        },
        {
            "name": "OPC_FINANCE_PIPELINE_RUNS_ROOT", "classification": "persistent_pipeline_ledger_path",
            "required_for_production": False, "required_for_scheduler": True,
            "secret": False, "default": None,
            "constraints": ["absolute_path", "writable_by_service_user", "backup_required"],
        },
        {
            "name": "OPC_FINANCE_SCHEDULER_ACTOR", "classification": "audit_actor_identifier",
            "required_for_production": False, "required_for_scheduler": True,
            "secret": False, "default": None,
            "constraints": ["must_equal_approved_schedule_operator", "not_a_credential"],
        },
    ]
    variables.extend({
        "name": name,
        "classification": "connector_secret_reference",
        "required_for_production": False,
        "required_for_live_connector": True,
        "secret": True,
        "default": None,
        "constraints": ["inject_from_secret_manager", "never_commit", "never_return_from_api"],
    } for name in connector_env)
    variables.extend({
        "name": name,
        "classification": "connector_private_entity_credential_binding",
        "required_for_production": False,
        "required_for_live_connector_access_receipt": True,
        "required_for_multi_entity_connector": True,
        "secret": True,
        "contains_raw_secret_values": False,
        "default": None,
        "constraints": [
            "inject_from_secret_manager_or_protected_environment",
            "strict_entity_keyed_json_object",
            "dynamic_secret_aliases_resolved_from_selected_entity_only",
            "never_commit", "never_return_from_api",
        ],
    } for name in binding_env if name not in connector_env)
    return {
        "schema_version": 2,
        "runtime_fingerprint": context["runtime"]["fingerprint"],
        "process": {
            "command": ["opc-finance-workbench"],
            "scheduler_command": [
                "opc-finance-box", "pipeline-schedule-run", "BOX_CONFIG", "SCHEDULE_FILE",
                "--runs-root", "PIPELINE_RUNS_ROOT", "--actor", "OPERATOR_PRINCIPAL",
            ],
            "scheduler_command_with_pack_override": [
                "opc-finance-box", "--packs", "PACKS_ROOT",
                "pipeline-schedule-run", "BOX_CONFIG", "SCHEDULE_FILE",
                "--runs-root", "PIPELINE_RUNS_ROOT", "--actor", "OPERATOR_PRINCIPAL",
            ],
            "run_as_root": False,
            "working_directory_dependency": False,
            "shutdown_signal": "SIGTERM",
            "graceful_request_drain_implemented": True,
        },
        "environment": variables,
        "connector_secret_environment_names": connector_env,
        "connector_private_binding_environment_names": binding_env,
        "entity_credential_binding_contracts": entity_credential_binding_contracts,
        "secret_values_included": False,
        "filesystem": {
            "read_only_mounts": [
                "box_config", "optional_pack_override", "api_auth_policy",
                "optional_tax_applicability_review_directory",
                "optional_tax_applicability_registry_receipt",
                "optional_connector_shadow_review_directory",
                "optional_pilot_readiness_review",
                "optional_pilot_data_handoff_review",
                "optional_pilot_shadow_run_registration",
                "optional_pilot_shadow_observation_review",
                "optional_pilot_shadow_entity_report_directory",
                "optional_pilot_shadow_portfolio_review",
                "optional_pilot_shadow_series_review",
                "optional_pilot_shadow_series_evidence_directory",
                "optional_read_only_activation_workspace_root",
                "optional_stable_promotion_ledger_directory",
                "optional_pipeline_schedule", "pipeline_request_files",
            ],
            "read_write_mounts": ["runtime_data", "pipeline_run_ledger"],
            "temporary_storage": ["operating_system_temp_directory"],
            "container_root_filesystem_recommended_read_only": True,
            "backup_required": [
                "runtime_data", "pipeline_run_ledger", "connector_sync_control_ledger",
                "airwallex_webhook_durable_inbox",
                "release_promotion_evidence_ledger", "evidence_store_outside_box",
            ],
        },
        "network": {
            "default_binding": "127.0.0.1",
            "container_binding": "0.0.0.0_requires_role_policy",
            "tls_included": False,
            "reverse_proxy_required_for_remote_access": True,
            "connector_egress_allowlist_required": True,
            "airwallex_webhook_ingress_requires_tls_reverse_proxy_and_provider_ip_allowlist": True,
            "metrics_endpoint_requires_reader_role_when_auth_enabled": True,
        },
        "health": {
            "liveness_path": "/api/health",
            "readiness_paths": [
                "/api/box", "/api/box/pipeline-observability", "/api/box/connector-sync",
                "/api/box/connector-shadow",
                "/api/box/pilot-shadow-run",
                "/api/box/pilot-shadow-observation",
                "/api/box/pilot-shadow-periods",
                "/api/box/pilot-shadow-series",
            ],
            "prometheus_path": "/api/box/pipeline-observability?format=prometheus",
            "readiness_requires_expected_runtime_fingerprint": True,
        },
        "smoke_verification": {
            "command": "opc-finance-box --packs PACKS_ROOT deployment-smoke BOX_CONFIG",
            "isolated_temporary_data": True,
            "loopback_only": True,
            "connector_dispatch_performed": False,
            "external_actions_performed": False,
        },
        "included_templates": [
            "deployment/Dockerfile", "deployment/Dockerfile.dockerignore",
            "deployment/compose.example.yaml",
            "deployment/opc-finance-workbench.service",
            "deployment/opc-finance-scheduler.service",
            "deployment/opc-finance-scheduler.timer", "deployment/box.env.example",
        ],
        "production_claim": "single_node_deployment_starter_not_managed_finance_saas",
    }


def _build_runtime_data_contract(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "runtime_fingerprint": context["runtime"]["fingerprint"],
        "layout": {
            "current_version": CURRENT_LAYOUT_VERSION,
            "manifest_file": MANIFEST_NAME,
            "stores": {name: dict(contract) for name, contract in STORE_CONTRACT.items()},
            "legacy_directory_requires_explicit_adoption": True,
            "future_layout_fails_closed": True,
        },
        "upgrade": {
            "preflight_command": "opc-finance-box runtime-data-upgrade-preflight DATA_ROOT",
            "automatic_in_place_migration_available": False,
            "explicit_offline_migration_available": True,
            "migration_command": (
                "opc-finance-box runtime-data-migrate DATA_ROOT VERIFIED_BACKUP "
                "--actor MIGRATION_OPERATOR --service-stopped-confirmed"
            ),
            "backup_required_before_layout_change": True,
            "service_stop_required_before_layout_change": True,
        },
        "backup": {
            "command": (
                "opc-finance-box runtime-data-backup DATA_ROOT BACKUP_DESTINATION "
                "--actor BACKUP_OPERATOR --service-stopped-confirmed"
            ),
            "verify_command": "opc-finance-box runtime-data-backup-verify BACKUP_DIRECTORY",
            "scope": "complete_runtime_data_directory",
            "non_overwriting": True,
            "per_file_sha256": True,
            "service_stopped_confirmation_required": True,
            "contains_sensitive_financial_data": True,
            "encryption_included": False,
        },
        "restore": {
            "command": (
                "opc-finance-box runtime-data-restore BACKUP_DIRECTORY NEW_DATA_ROOT "
                "--actor RESTORE_OPERATOR"
            ),
            "target_must_not_exist": True,
            "merge_allowed": False,
            "overwrite_allowed": False,
            "receipt_required": True,
            "http_restore_enabled": False,
        },
        "external_actions_performed": False,
    }


def _build_connector_sync_policy(
    context: dict[str, Any], connectors: list[dict[str, Any]],
) -> dict[str, Any]:
    incremental = [{
        "connector_id": connector["connector_id"],
        "pack_id": connector["pack_id"],
        "capability": connector["capability"],
        "sync_window": dict(connector["sync_window"]),
        "checkpoint_scope": "connector_id + legal_entity_id + operator_stream_id",
    } for connector in connectors if connector.get("sync_window")]
    return {
        "schema_version": 2,
        "runtime_fingerprint": context["runtime"]["fingerprint"],
        "incremental_connectors": incremental,
        "plan_schema_versions_supported": [1, 2],
        "generated_plan_schema_version": 2,
        "capture_policy": {
            "logical_checkpoint_remains_monotonic_when_overlap_is_applied": True,
            "actual_request_window_is_explicit": True,
            "stable_business_key_upsert_required_for_overlap": True,
            "complete_update_capture_claimed": False,
            "provider_webhook_or_update_cursor_may_still_be_required": True,
        },
        "control_store": ".opc-finance-data/connector_sync/connector_sync_events.jsonl",
        "integrity": "sha256_hash_chain",
        "cross_run_checkpoint": "time_window_high_watermark",
        "opaque_provider_cursor_persisted_across_runs": False,
        "checkpoint_commit": {
            "automatic": False,
            "requires_complete_network_window": True,
            "requires_zero_rejected_and_duplicate_rows": True,
            "requires_named_actor_rationale_and_evidence": True,
            "stale_plan_fails_closed": True,
        },
        "backfill": {
            "bounded": True,
            "automatically_advances_incremental_checkpoint": False,
            "uses_same_quality_and_quarantine_controls": True,
        },
        "retry_and_rate_limit": {
            "bounded_attempts": True,
            "retryable_statuses": [429, "5xx"],
            "retry_after_seconds_honored_up_to": 30,
            "response_bodies_returned_in_error": False,
        },
        "quarantine": {
            "failed_or_incomplete_attempts_isolated": True,
            "resolution_requires_named_actor_and_rationale": True,
            "replacement_must_be_complete_same_stream_attempt": True,
            "raw_request_or_response_stored": False,
        },
        "external_actions_performed": False,
    }


def _setup_tasks(context: dict[str, Any]) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = [
        {
            "task_id": "runtime-security:api-access",
            "category": "runtime_security",
            "severity": "required_before_network_exposure",
            "owner_role": "box_maintainer",
            "summary": (
                "保持工作台绑定 localhost；非回环绑定前配置 OPC_FINANCE_API_AUTH_FILE "
                "多角色 principal（或兼容 token）、TLS 终止和受控网络入口"
            ),
            "secret_values_included": False,
        },
        {
            "task_id": "runtime-security:deployment-verification",
            "category": "runtime_security",
            "severity": "required_before_live_action",
            "owner_role": "box_maintainer",
            "summary": (
                "复核 deployment-environment-contract，验证部署模板，并在最终 wheel "
                "安装环境执行隔离 deployment smoke、备份和空目标恢复演练"
            ),
            "secret_values_included": False,
        },
    ]
    for pack in context["packs"]:
        if pack["status"] != "stable":
            tasks.append({
                "task_id": f"pack-review:{pack['id']}",
                "category": "pack_readiness",
                "severity": "warning",
                "owner_role": "box_maintainer",
                "summary": f"确认 {pack['display_name']} {pack['version']} 的 {pack['status']} 使用边界",
            })
    for entity in context["entities"]:
        if entity.get("tax_readiness") != "filing_assist":
            tasks.append({
                "task_id": f"tax-readiness:{entity['id']}",
                "category": "tax_readiness",
                "severity": "blocking_for_external_filing",
                "owner_role": "tax_reviewer",
                "entity_id": entity["id"],
                "summary": (
                    f"{entity['name']} 的税务包成熟度为 {entity.get('tax_readiness')}；"
                    "不得启用自动申报或声称 filing-ready"
                ),
            })
        uncertain = [
            item for item in entity.get("tax_registrations", [])
            if "confirm" in item.lower() or "review_required" in item.lower()
        ]
        if uncertain:
            tasks.append({
                "task_id": f"tax-registration:{entity['id']}",
                "category": "tax_registration",
                "severity": "blocking_for_calendar",
                "owner_role": "tax_reviewer",
                "entity_id": entity["id"],
                "summary": f"确认税务登记状态：{', '.join(uncertain)}",
            })
    for gate in context["manual_review_gates"]:
        tasks.append({
            "task_id": f"review-owner:{gate}",
            "category": "control_owner",
            "severity": "required_before_live_action",
            "owner_role": "box_owner",
            "summary": f"为人工复核门 {gate} 配置有权人和替补人",
        })
    return tasks


def _capability_enabled(requirement: str | None, capabilities: set[str]) -> bool:
    if requirement is None:
        return True
    if requirement.endswith(".*"):
        prefix = requirement[:-1]
        return any(capability.startswith(prefix) for capability in capabilities)
    return requirement in capabilities


def _build_data_model(capabilities: set[str]) -> dict[str, Any]:
    objects = [
        dict(item) for item in DATA_MODEL_CATALOG
        if _capability_enabled(item["capability"], capabilities)
    ]
    return {
        "schema_version": 1,
        "model_status": "editable_contract",
        "objects": objects,
        "global_invariants": [
            "所有财务记录必须能归属一个法律主体",
            "金额必须带币种；折算必须带期间、汇率和来源",
            "候选、草稿、已批准、已过账、已申报是不同状态",
            "缺失事实保持缺失，不默认填零或由 LLM 猜测",
        ],
    }


def _build_agent_contracts(capabilities: set[str]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "contract_status": "editable_not_runtime_authority",
        "contracts": [
            dict(item) for item in AGENT_CONTRACT_CATALOG
            if _capability_enabled(item["capability"], capabilities)
        ],
    }


def _build_job_plan(
    workflows: list[dict[str, Any]],
    services: list[dict[str, Any]],
    pipelines: list[dict[str, Any]],
) -> dict[str, Any]:
    services_by_capability: dict[str, list[str]] = {}
    for service in services:
        services_by_capability.setdefault(service["capability"], []).append(service["service_id"])
    pipelines_by_capability: dict[str, list[str]] = {}
    for pipeline in pipelines:
        if pipeline["implementation_status"] == "executable":
            pipelines_by_capability.setdefault(pipeline["capability"], []).append(pipeline["pipeline_id"])
    jobs = [{
        "job_id": workflow["workflow_id"],
        "enabled": False,
        "cadence": workflow["cadence"],
        "trigger": {"kind": "operator_configured_schedule", "timezone": "REQUIRED"},
        "candidate_services": sorted(services_by_capability.get(workflow["capability"], [])),
        "candidate_pipelines": sorted(pipelines_by_capability.get(workflow["capability"], [])),
        "human_gate": workflow["human_gate"],
        "outputs": list(workflow["outputs"]),
        "control_note": "启用前配置主体范围、数据就绪检查、幂等键、失败告警和有权人。",
    } for workflow in workflows]
    jobs.append({
        "job_id": "tax.applicability_registry_rotation_alerts",
        "enabled": False,
        "cadence": "daily",
        "trigger": {"kind": "operator_configured_schedule", "timezone": "REQUIRED"},
        "candidate_services": [],
        "candidate_pipelines": [],
        "candidate_command": (
            "opc-finance-box tax-applicability-alerts <box-config.json> "
            "--review-dir <absolute-private-review-dir> "
            "--receipt <private-registry-receipt.json> --as-of <YYYY-MM-DD>"
        ),
        "human_gate": "alert_route_owner_approval",
        "outputs": ["safe_alert_candidates"],
        "notifications_sent": False,
        "control_note": (
            "默认不安装、不发送通知；启用前配置时区、观察日、告警接收人、"
            "去重策略和独立升级责任人。"
        ),
    })
    jobs.append({
        "job_id": "pilot.readiness_review_rotation_alerts",
        "enabled": False,
        "cadence": "daily",
        "trigger": {"kind": "operator_configured_schedule", "timezone": "REQUIRED"},
        "candidate_services": [],
        "candidate_pipelines": [],
        "candidate_command": (
            "opc-finance-box pilot-readiness-alerts <box-config.json> "
            "--review <private-pilot-readiness-review.json> --as-of <YYYY-MM-DD>"
        ),
        "human_gate": "alert_route_owner_approval",
        "outputs": ["safe_alert_candidates"],
        "notifications_sent": False,
        "control_note": (
            "默认不安装、不发送通知；启用前配置时区、观察日、告警接收人、"
            "去重策略和重新签认负责人。"
        ),
    })
    jobs.append({
        "job_id": "connector.access_receipt_rotation_alerts",
        "enabled": False,
        "cadence": "daily",
        "trigger": {"kind": "operator_configured_schedule", "timezone": "REQUIRED"},
        "candidate_services": [],
        "candidate_pipelines": [],
        "candidate_command": (
            "opc-finance-box connector-access-alerts <box-config.json> "
            "<absolute-private-activation-root> --as-of <YYYY-MM-DD>"
        ),
        "human_gate": "alert_route_owner_approval",
        "outputs": ["safe_alert_candidates"],
        "notifications_sent": False,
        "control_note": (
            "默认不安装、不联网、不发送通知；启用前配置时区、接收人、稳定 alert ID "
            "去重、到期前升级责任人和续期失败处置。"
        ),
    })
    return {
        "schema_version": 1,
        "installation_status": "not_installed",
        "jobs": jobs,
    }


def _build_dashboard_layout(capabilities: set[str], entities: list[dict[str, Any]]) -> dict[str, Any]:
    panels = [
        {"panel_id": "box_readiness", "scope": "management", "capability": None},
        {"panel_id": "goal_and_confirmations", "scope": "management", "capability": "agent.plans"},
        {"panel_id": "cash_safety", "scope": "statutory", "capability": "finance.cash_forecast"},
        {"panel_id": "close_readiness", "scope": "statutory", "capability": "finance.record_to_report"},
        {"panel_id": "commerce_margin", "scope": "management", "capability": "commerce.product_margin"},
        {"panel_id": "stripe_cash_reconciliation", "scope": "statutory", "capability": "connector.stripe_payouts"},
        {"panel_id": "paypal_transaction_activity", "scope": "statutory", "capability": "connector.paypal_transaction_activity"},
        {"panel_id": "woocommerce_order_refund_activity", "scope": "statutory", "capability": "connector.woocommerce_order_refund_activity"},
        {"panel_id": "amazon_seller_transaction_activity", "scope": "statutory", "capability": "connector.amazon_seller_transaction_activity"},
        {"panel_id": "amazon_seller_marketplace_completeness", "scope": "statutory", "capability": "connector.amazon_seller_marketplace_evidence"},
        {"panel_id": "shopify_stripe_order_to_cash", "scope": "statutory", "capability": "integration.shopify_stripe_order_to_cash"},
        {"panel_id": "shopify_stripe_monthly_metrics", "scope": "statutory", "capability": "integration.shopify_stripe_monthly_close"},
        {"panel_id": "shipbob_fulfillment_and_returns", "scope": "statutory", "capability": "connector.shipbob_fulfillment_evidence"},
        {"panel_id": "marketplace_inventory", "scope": "statutory", "capability": "channel.marketplace_inventory_reconciliation"},
        {"panel_id": "game_project_finance", "scope": "management", "capability": "game.project_profitability"},
        {"panel_id": "game_ltv_roi", "scope": "management", "capability": "game.ltv_roi_review"},
        {"panel_id": "management_consolidation", "scope": "management", "capability": "entity.management_consolidation"},
        {"panel_id": "tax_readiness_by_entity", "scope": "statutory", "capability": None},
    ]
    return {
        "schema_version": 1,
        "layout_status": "editable_contract",
        "entity_switcher": [entity["id"] for entity in entities],
        "panels": [
            panel for panel in panels
            if panel["capability"] is None or panel["capability"] in capabilities
        ],
        "control_note": "管理面板只聚合进度与管理指标；法定凭证、税表、银行和审批必须切换到单主体。",
    }


def _build_jurisdiction_rules(runtime: BoxRuntime, entities: list[dict[str, Any]]) -> dict[str, Any]:
    output = []
    for entity in entities:
        bundle = runtime.tax_rules(entity["id"])
        output.append({
            "entity_id": entity["id"],
            "pack_id": bundle["pack_id"],
            "pack_version": bundle["pack_version"],
            "pack_status": bundle["pack_status"],
            "tax_readiness": entity.get("tax_readiness"),
            "verified_at": bundle["rules"].get("verified_at"),
            "review_policy": bundle["rules"].get("review_policy"),
            "applicability_review_policy": bundle["rules"].get(
                "applicability_review_policy"
            ),
            "scope_note": bundle["rules"].get("scope_note"),
            "sources": bundle["rules"].get("sources") or [],
            "rules": bundle["rules"].get("rules") or [],
        })
    return {"schema_version": 1, "entities": output}


def _build_release_gates(context: dict[str, Any], declared_only: list[str]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "runtime_fingerprint": context["runtime"]["fingerprint"],
        "release_status": "not_approved",
        "automated_gates": [
            {"gate": "unit_and_contract_tests", "command": "python -m unittest discover -s tests -q"},
            {"gate": "pack_provider_audit", "command": "opc-finance-box pack-audit", "expected": {"declared_only": 0}},
            {
                "gate": "technical_rc_product_matrix",
                "command": (
                    "opc-finance-box release-candidate-audit "
                    "--wheel <built-wheel.whl> --source-kit <source-kit.zip>"
                ),
                "expected": {
                    "passed": True,
                    "source_tree_release_candidate": True,
                    "release_artifacts_verified": True,
                    "starter_matrix": {
                        "unavailable_combination_count": 0,
                    },
                },
            },
            {"gate": "finance_boundary_eval", "command": "opc-finance-box eval evals/core_packs.json", "expected": {"failed": 0}},
            {"gate": "box_doctor", "command": "opc-finance-box doctor <box-config.json>", "expected": {"blocker": 0}},
            {
                "gate": "tax_rule_lifecycle",
                "command": "opc-finance-box tax-rule-status <box-config.json>",
                "expected": {"expired": 0},
            },
            {
                "gate": "tax_applicability_reviews",
                "mode": "one_fingerprint_bound_review_per_entity",
                "command": (
                    "opc-finance-box tax-applicability-portfolio-verify "
                    "<box-config.json> "
                    + " ".join(
                        f"<{entity['id']}-tax-applicability-review.json>"
                        for entity in context["entities"]
                    )
                    + " --as-of <YYYY-MM-DD>"
                ),
                "expected": {
                    "valid": True,
                    "complete": True,
                    "entity_count": len(context["entities"]),
                    "calendar_release_allowed": True,
                },
                "required_reviews": [{
                    "entity_id": entity["id"],
                    "command": (
                        "opc-finance-box tax-applicability-verify "
                        f"<box-config.json> <{entity['id']}-tax-applicability-review.json> "
                        "--as-of <YYYY-MM-DD>"
                    ),
                    "expected": {
                        "valid": True,
                        "entity_id": entity["id"],
                        "decision": "approved-in-scope",
                        "review_current": True,
                        "unanswered_count": 0,
                        "applicability_gate_passed": True,
                    },
                } for entity in context["entities"]],
            },
            {
                "gate": "tax_applicability_registry_activation",
                "mode": "exact_review_set_bound_to_private_receipt",
                "command": (
                    "opc-finance-box tax-applicability-registry-verify "
                    "<box-config.json> <private-registry-receipt.json> "
                    "--review-dir <absolute-private-review-dir> "
                    "--as-of <YYYY-MM-DD>"
                ),
                "expected": {
                    "valid": True,
                    "registry_unchanged": True,
                    "entity_count": len(context["entities"]),
                    "ready_for_calendar_release": True,
                    "digital_signature_verified": False,
                    "filing_authorization_granted": False,
                },
            },
            {
                "gate": "installable_distribution",
                "command": (
                    "python -m pip wheel . --no-deps --wheel-dir <empty-dist-dir> && "
                    "opc-finance-box distribution-verify <built-wheel.whl>"
                ),
                "expected": {"valid": True, "project_name": "opc-finance-box"},
            },
            {"gate": "upgrade_compatibility", "command": "opc-finance-box upgrade-check <box-config.json> <previous-box.lock.json>"},
            {
                "gate": "runtime_data_layout_preflight",
                "command": "opc-finance-box runtime-data-upgrade-preflight <runtime-data-root>",
                "expected": {"compatible": True, "decision": "no_change"},
            },
            {
                "gate": "pipeline_ledger_integrity_and_review_state",
                "command": "python3 -m unittest tests.test_pipeline_run_store -q",
            },
            {
                "gate": "connector_sync_checkpoint_and_quarantine_controls",
                "command": "python3 -m unittest tests.test_connector_sync -q",
                "expected": {"failures": 0},
            },
            {
                "gate": "stable_promotion_evidence_control",
                "command": (
                    "python3 -m unittest tests.test_release_promotion "
                    "tests.test_connector_shadow_release_promotion "
                    "tests.test_connector_shadow_registry -q"
                ),
                "expected": {"failures": 0, "pack_manifest_changed": False},
            },
            {
                "gate": "isolated_workbench_smoke",
                "command": "opc-finance-box --packs <packs-root> deployment-smoke <box-config.json>",
                "expected": {"passed": True, "external_actions_performed": False},
            },
            {
                "gate": "deployment_starter_controls",
                "command": "opc-finance-box deployment-assets-verify <deployment-root>",
                "expected": {"valid": True, "raw_secret_values_included": False},
            },
        ],
        "current_declared_only_capabilities": list(declared_only),
        "manual_gates": [
            "使用代表性真实脱敏数据完成 shadow close，逐项解释差异并由财务专业人士签认",
            "网络 Connector 必须用 schema v2 real_anonymized baseline；schema v1/demo Shadow 不能晋级",
            "用 promotion-record 锁定证据指纹，再由与操作人和 Shadow 复核人分离的发布复核人审批",
            "为 review gate 配置主审、替补、职责分离和审计留痕",
            "每个法律主体分别完成 fingerprint-bound 税务适用性工作底稿；准备人与当地税务复核人必须分离",
            "完成备份、恢复、升级、回滚与故障演练",
            "税务或外部动作按具体地区、登记、表单和外部系统单独批准",
        ],
        "control_note": "自动化通过不等于 stable 或 filing_assist；release_status 只能由明确发布流程更新。",
    }


def compile_box(
    runtime: BoxRuntime,
    registry: PackServiceRegistry | None = None,
) -> dict[str, Any]:
    """Compile a Box into a reproducible product/deployment contract."""
    selected_registry = registry or build_default_service_registry()
    context = build_box_context(runtime)
    capabilities = {
        capability
        for values in context["capability_groups"].values()
        for capability in values
    }
    services = selected_registry.catalog(runtime)
    connectors = build_box_connector_registry(runtime).catalog(runtime)
    pipelines = _enabled_pipelines(capabilities, services, connectors, context["entities"])
    pipeline_request_templates = _build_pipeline_request_templates(
        pipelines, context["entities"], context["scope"]["reporting_currency"],
    )
    pipeline_run_policy = _build_pipeline_run_policy(pipelines)
    pipeline_schedule_template = _build_pipeline_schedule_template(
        pipeline_request_templates,
    )
    runtime_security_policy = _build_runtime_security_policy(connectors)
    deployment_environment_contract = _build_deployment_environment_contract(
        context, connectors,
    )
    runtime_data_contract = _build_runtime_data_contract(context)
    connector_sync_policy = _build_connector_sync_policy(context, connectors)
    promotion_policy = stable_promotion_policy(
        context["runtime"]["fingerprint"], context["packs"],
    )
    promotion_templates = stable_promotion_evidence_template_catalog(runtime)
    pilot_readiness_plan = build_pilot_readiness_plan(runtime)
    production_readiness_plan = build_production_readiness_plan(runtime)
    pilot_readiness_artifact_schema = json.loads(
        (find_resource_root() / "box" / "pilot-readiness-artifact.schema.json").read_text(
            encoding="utf-8"
        )
    )
    pilot_data_handoff_plan = build_pilot_data_handoff_plan(runtime)
    pilot_data_handoff_artifact_schema = json.loads(
        (
            find_resource_root()
            / "box"
            / "pilot-data-handoff-artifact.schema.json"
        ).read_text(encoding="utf-8")
    )
    pilot_shadow_run_registration_schema = json.loads(
        (
            find_resource_root()
            / "box"
            / "pilot-shadow-run-registration.schema.json"
        ).read_text(encoding="utf-8")
    )
    pilot_shadow_observation_artifact_schema = json.loads(
        (
            find_resource_root()
            / "box"
            / "pilot-shadow-observation-artifact.schema.json"
        ).read_text(encoding="utf-8")
    )
    pilot_shadow_series_artifact_schema = json.loads(
        (
            find_resource_root()
            / "box"
            / "pilot-shadow-series-artifact.schema.json"
        ).read_text(encoding="utf-8")
    )
    cfo_metric_evaluation_request_schema = json.loads(
        (find_resource_root() / "box" / "cfo-metric-evaluation-request.schema.json")
        .read_text(encoding="utf-8")
    )
    cfo_metric_operand_assembly_schema = json.loads(
        (find_resource_root() / "box" / "cfo-metric-operand-assembly.schema.json")
        .read_text(encoding="utf-8")
    )
    promotion_evidence_schema = json.loads(
        (find_resource_root() / "box" / "stable-promotion-evidence.schema.json").read_text(
            encoding="utf-8"
        )
    )
    tax_applicability_artifact_schema = json.loads(
        (find_resource_root() / "box" / "tax-applicability-artifact.schema.json").read_text(
            encoding="utf-8"
        )
    )
    tax_applicability_registry_receipt_schema = json.loads(
        (
            find_resource_root()
            / "box"
            / "tax-applicability-registry-receipt.schema.json"
        ).read_text(encoding="utf-8")
    )
    tax_applicability_artifact_security_policy = {
        "schema_version": 2,
        "artifact_types": [
            "tax_applicability_workpaper", "tax_applicability_review",
            "tax_applicability_registry_receipt",
        ],
        "maximum_file_bytes": 2 * 1024 * 1024,
        "write_policy": {
            "exclusive_create": True,
            "overwrite_allowed": False,
            "posix_mode": "0600",
        },
        "read_policy": {
            "symbolic_links_allowed": False,
            "posix_group_or_other_permissions_allowed": False,
            "windows_acl_review_required": True,
        },
        "registry_policy": {
            "directory_must_be_absolute": True,
            "directory_symbolic_link_allowed": False,
            "filename_pattern": "<entity_id>.json",
            "unexpected_entries_close_release_gate": True,
            "http_path_override_allowed": False,
            "controlled_import_command": "tax-applicability-import",
            "overwrite_allowed": False,
            "activation_receipt_required_for_runtime_release": True,
            "receipt_seal_command": "tax-applicability-registry-seal",
            "receipt_verify_command": "tax-applicability-registry-verify",
            "receipt_controller_separation_required": True,
        },
        "safe_output": {
            "paths_returned": False,
            "answers_returned": False,
            "review_rationales_returned": False,
            "evidence_references_returned": False,
        },
    }
    service_capabilities = {item["capability"] for item in services}
    connector_capabilities = {item["capability"] for item in connectors}
    runtime_capabilities = capabilities & RUNTIME_CAPABILITIES
    executable_capabilities = service_capabilities | connector_capabilities | runtime_capabilities
    workflows = _enabled_workflows(capabilities, executable_capabilities)
    workflow_capabilities = {
        item["capability"] for item in workflows if item["implementation_status"] == "executable"
    }
    providers_by_capability: dict[str, list[str]] = {capability: [] for capability in capabilities}
    for capability in service_capabilities:
        providers_by_capability.setdefault(capability, []).append("service")
    for capability in connector_capabilities:
        providers_by_capability.setdefault(capability, []).append("connector")
    for capability in runtime_capabilities:
        providers_by_capability.setdefault(capability, []).append("runtime_guardrail")
    for capability in workflow_capabilities:
        providers_by_capability.setdefault(capability, []).append("workflow")
    coverage = [{
        "capability": capability,
        "providers": sorted(set(providers_by_capability.get(capability, []))),
        "implementation_status": (
            "executable" if providers_by_capability.get(capability) else "declared_only"
        ),
    } for capability in sorted(capabilities)]
    declared_only = [
        item["capability"] for item in coverage if item["implementation_status"] == "declared_only"
    ]
    setup_tasks = _setup_tasks(context)
    tax_applicability_questionnaire = build_tax_applicability_questionnaire(runtime)
    for entity in tax_applicability_questionnaire["entities"]:
        setup_tasks.extend([
            {
                "task_id": f"tax-applicability:{entity['entity_id']}",
                "category": "tax_applicability",
                "severity": "blocking_for_calendar",
                "owner_role": "tax_reviewer",
                "entity_id": entity["entity_id"],
                "summary": (
                    f"完成 {entity['pack_id']} 适用性问卷并由当地税务复核人确认；"
                    "系统不自动判定法律形式、税务居民或特殊制度"
                ),
                "questionnaire_file": "tax-applicability-questionnaire.json",
                "workpaper_command": (
                    "opc-finance-box tax-applicability-init <box-config.json> "
                    f"--entity {entity['entity_id']} --prepared-by <actor> "
                    "--facts-as-of <YYYY-MM-DD> "
                    f"--output <{entity['entity_id']}-tax-applicability-workpaper.json>"
                ),
                "review_command": (
                    "opc-finance-box tax-applicability-review <box-config.json> "
                    f"<{entity['entity_id']}-tax-applicability-workpaper.json> "
                    "--decision approved-in-scope --actor <independent-local-tax-reviewer> "
                    "--rationale <review-rationale> --evidence-reference <evidence://reference> "
                    f"--output <{entity['entity_id']}-tax-applicability-review.json>"
                ),
                "verify_command": (
                    "opc-finance-box tax-applicability-verify <box-config.json> "
                    f"<{entity['entity_id']}-tax-applicability-review.json> "
                    "--as-of <YYYY-MM-DD>"
                ),
                "registry_status_command": (
                    "opc-finance-box tax-applicability-status <box-config.json> "
                    "--review-dir <absolute-private-review-dir> "
                    "--as-of <YYYY-MM-DD>"
                ),
                "registry_import_command": (
                    "opc-finance-box tax-applicability-import <box-config.json> "
                    "<private-review.json> "
                    "--review-dir <absolute-private-review-dir> "
                    "--as-of <YYYY-MM-DD>"
                ),
                "registry_seal_command": (
                    "opc-finance-box tax-applicability-registry-seal "
                    "<box-config.json> --review-dir <absolute-private-review-dir> "
                    "--actor <registry-controller> --as-of <YYYY-MM-DD> "
                    "--output <private-registry-receipt.json>"
                ),
                "registry_verify_command": (
                    "opc-finance-box tax-applicability-registry-verify "
                    "<box-config.json> <private-registry-receipt.json> "
                    "--review-dir <absolute-private-review-dir> "
                    "--as-of <YYYY-MM-DD>"
                ),
                "applicability_review_policy": entity["applicability_review_policy"],
                "fingerprint_bound": True,
                "preparer_reviewer_separation_required": True,
                "raw_tax_identifiers_required": False,
            },
            {
                "task_id": f"tax-rule-review:{entity['entity_id']}",
                "category": "tax_rule_lifecycle",
                "severity": "required_before_calendar_release",
                "owner_role": "tax_reviewer",
                "entity_id": entity["entity_id"],
                "summary": (
                    f"按 {entity['pack_id']} review_policy 监控官方来源时效；"
                    "review_due 需排期复核，expired 禁止日历与外部申报释放"
                ),
                "status_command": "opc-finance-box tax-rule-status <box-config.json>",
            },
        ])
    setup_tasks.append({
        "task_id": "tax-applicability-registry-activation",
        "category": "tax_applicability_registry",
        "severity": "required_before_calendar_release",
        "owner_role": "tax_registry_controller",
        "summary": (
            "完成全主体受控导入后生成私有内容指纹收据；controller 必须与所有"
            "准备人和复核人分离，运行时同时验证目录与收据"
        ),
        "seal_command": (
            "opc-finance-box tax-applicability-registry-seal <box-config.json> "
            "--review-dir <absolute-private-review-dir> "
            "--actor <registry-controller> --as-of <YYYY-MM-DD> "
            "--output <private-registry-receipt.json>"
        ),
        "verify_command": (
            "opc-finance-box tax-applicability-registry-verify <box-config.json> "
            "<private-registry-receipt.json> "
            "--review-dir <absolute-private-review-dir> --as-of <YYYY-MM-DD>"
        ),
        "alert_command": (
            "opc-finance-box tax-applicability-alerts <box-config.json> "
            "--review-dir <absolute-private-review-dir> "
            "--receipt <private-registry-receipt.json> --as-of <YYYY-MM-DD>"
        ),
        "alert_schedule_installed": False,
        "notifications_sent": False,
        "digital_signature_performed": False,
        "filing_authorization_granted": False,
    })
    setup_tasks.append({
        "task_id": "first-company-pilot-readiness",
        "category": "pilot_readiness",
        "severity": "required_before_bounded_shadow",
        "owner_role": "finance_control_reviewer",
        "summary": (
            "逐主体确认只读数据映射、行业资料域、网络 Connector 控制与首期 Shadow Close 计划，"
            "再由独立复核人签认；不得写入凭证、原始账号、税号或财务金额"
        ),
        "plan_file": "pilot-readiness-plan.json",
        "schema_file": "pilot-readiness-artifact.schema.json",
        "init_command": (
            "opc-finance-box pilot-readiness-init <box-config.json> "
            "--period <YYYY-MM> --prepared-by <preparer> "
            "--output <private-pilot-readiness-workpaper.json>"
        ),
        "review_command": (
            "opc-finance-box pilot-readiness-review <box-config.json> "
            "<private-pilot-readiness-workpaper.json> --actor <independent-reviewer> "
            "--rationale <rationale> --evidence-reference <evidence://reference> "
            "--output <private-pilot-readiness-review.json>"
        ),
        "verify_command": (
            "opc-finance-box pilot-readiness-verify <box-config.json> "
            "<private-pilot-readiness-review.json>"
        ),
        "tax_activation_verify_arguments": (
            "--tax-review-dir <absolute-private-review-dir> "
            "--tax-registry-receipt <private-registry-receipt.json> "
            "--as-of <YYYY-MM-DD>"
        ),
        "artifact_file_mode": "0600",
        "overwrite_allowed": False,
        "ready_for_statutory_release": False,
        "ready_for_external_filing": False,
        "external_actions_authorized": False,
    })
    setup_tasks.append({
        "task_id": "first-company-controlled-data-handoff",
        "category": "pilot_data_handoff",
        "severity": "required_before_real_data_intake",
        "owner_role": "data_access_reviewer",
        "summary": (
            "逐主体登记真实资料域的传输方式、文件计数、私有清单指纹、个人数据分类、"
            "访问控制和证据引用；清单不复制原文件、文件名、路径、账号、税号或金额"
        ),
        "plan_file": "pilot-data-handoff-plan.json",
        "schema_file": "pilot-data-handoff-artifact.schema.json",
        "init_command": (
            "opc-finance-box pilot-data-handoff-init <box-config.json> "
            "<private-pilot-readiness-review.json> --prepared-by <preparer> "
            "--custodian-principal <data-custodian> --as-of <YYYY-MM-DD> "
            "--output <private-handoff-workpaper.json>"
        ),
        "review_command": (
            "opc-finance-box pilot-data-handoff-review <box-config.json> "
            "<private-handoff-workpaper.json> <private-pilot-readiness-review.json> "
            "--actor <independent-access-reviewer> --rationale <rationale> "
            "--evidence-reference <evidence://reference> --as-of <YYYY-MM-DD> "
            "--output <private-handoff-review.json>"
        ),
        "verify_command": (
            "opc-finance-box pilot-data-handoff-verify <box-config.json> "
            "<private-handoff-review.json> <private-pilot-readiness-review.json> "
            "--as-of <YYYY-MM-DD>"
        ),
        "artifact_file_mode": "0600",
        "overwrite_allowed": False,
        "raw_files_copied": False,
        "data_import_performed": False,
        "ready_for_statutory_release": False,
        "external_actions_authorized": False,
    })
    setup_tasks.append({
        "task_id": "first-company-shadow-run-registration",
        "category": "pilot_shadow_run",
        "severity": "required_before_first_shadow_observation",
        "owner_role": "shadow_run_registrar",
        "summary": (
            "将资料交接期次与每个法律主体恰好一个已完成全部独立复核 gate 的"
            "月结 Shadow Run 台账记录绑定；登记不保存金额，不授权过账、付款、关账或申报"
        ),
        "schema_file": "pilot-shadow-run-registration.schema.json",
        "register_command": (
            "opc-finance-box pilot-shadow-run-register <box-config.json> "
            "<private-handoff-review.json> <private-pilot-readiness-review.json> "
            "--runs-root <private-pipeline-runs-root> "
            "--entity-attempt <entity_id=attempt_id> "
            "--actor <independent-registrar> --rationale <rationale> "
            "--evidence-reference <evidence://reference> --as-of <YYYY-MM-DD> "
            "--output <private-shadow-run-registration.json>"
        ),
        "verify_command": (
            "opc-finance-box pilot-shadow-run-verify <box-config.json> "
            "<private-shadow-run-registration.json> <private-handoff-review.json> "
            "<private-pilot-readiness-review.json> "
            "--runs-root <private-pipeline-runs-root> --as-of <YYYY-MM-DD>"
        ),
        "exact_entity_coverage_required": True,
        "exact_handoff_period_required": True,
        "all_review_gates_approved_required": True,
        "registrar_role_separation_required": True,
        "artifact_file_mode": "0600",
        "overwrite_allowed": False,
        "financial_values_persisted": False,
        "ready_for_statutory_release": False,
        "ready_for_external_filing": False,
        "external_actions_authorized": False,
    })
    setup_tasks.append({
        "task_id": "first-company-shadow-observation-review",
        "category": "pilot_shadow_observation",
        "severity": "required_after_first_shadow_run",
        "owner_role": "independent_shadow_observation_reviewer",
        "summary": (
            "把当前首次 Shadow Run 登记与逐主体已签认 Shadow Close 报告精确绑定；"
            "多主体 Box 还必须绑定同期间、同 attempt 集合的独立组合签认。最终复核人与登记人、"
            "所有实体复核人及组合复核人分离，system_defect 必须退回修正"
        ),
        "schema_file": "pilot-shadow-observation-artifact.schema.json",
        "assemble_command": (
            "opc-finance-box pilot-shadow-observation-assemble <box-config.json> "
            "<private-shadow-run-registration.json> <private-handoff-review.json> "
            "<private-pilot-readiness-review.json> --runs-root <private-pipeline-runs-root> "
            "--entity-report <private-reviewed-entity-shadow-report.json> "
            "--portfolio-review <private-reviewed-portfolio-shadow-manifest.json> "
            "--as-of <YYYY-MM-DD> --output <private-shadow-observation-receipt.json>"
        ),
        "review_command": (
            "opc-finance-box pilot-shadow-observation-review <box-config.json> "
            "<private-shadow-observation-receipt.json> --decision <decision> "
            "--actor <independent-observation-reviewer> --rationale <rationale> "
            "--evidence-reference <audit://reference> "
            "--output <private-reviewed-shadow-observation.json>"
        ),
        "verify_command": (
            "opc-finance-box pilot-shadow-observation-verify <box-config.json> "
            "<private-reviewed-shadow-observation.json> "
            "<private-shadow-run-registration.json> <private-handoff-review.json> "
            "<private-pilot-readiness-review.json> --runs-root <private-pipeline-runs-root> "
            "--entity-report <private-reviewed-entity-shadow-report.json> "
            "--portfolio-review <private-reviewed-portfolio-shadow-manifest.json> "
            "--as-of <YYYY-MM-DD>"
        ),
        "portfolio_review_required_for_multi_entity": True,
        "registration_entity_period_and_attempt_binding_required": True,
        "fourth_role_separation_required": True,
        "system_defect_blocks_next_shadow_period": True,
        "artifact_file_mode": "0600",
        "overwrite_allowed": False,
        "raw_financial_values_persisted": False,
        "ready_for_stable_promotion": False,
        "ready_for_statutory_release": False,
        "ready_for_external_filing": False,
        "external_actions_authorized": False,
    })
    setup_tasks.append({
        "task_id": "consecutive-shadow-series-review",
        "category": "pilot_shadow_series",
        "severity": "required_before_stable_promotion_evidence",
        "owner_role": "independent_shadow_continuity_reviewer",
        "summary": (
            "重新验证至少两个连续月份的完整 Pilot 登记、台账、逐主体报告和观察复核；"
            "只形成无金额的跨期计数趋势与内容指纹，由不同于所有期间角色的人复核"
        ),
        "schema_file": "pilot-shadow-series-artifact.schema.json",
        "period_evidence_layout": (
            "<root>/<YYYY-MM>/{reviewed-observation.json,shadow-run-registration.json,"
            "data-handoff-review.json,pilot-readiness-review.json,entity-reports/"
            "<entity_id>.json[,portfolio-review.json]}"
        ),
        "assemble_command": (
            "opc-finance-box pilot-shadow-series-assemble <box-config.json> "
            "<private-period-evidence-root> --runs-root <private-pipeline-runs-root> "
            "--as-of <YYYY-MM-DD> --output <private-shadow-series-receipt.json>"
        ),
        "review_command": (
            "opc-finance-box pilot-shadow-series-review <box-config.json> "
            "<private-shadow-series-receipt.json> "
            "--decision <approved-for-promotion-evidence|needs-correction> "
            "--actor <independent-continuity-reviewer> --rationale <rationale> "
            "--evidence-reference <audit://reference> "
            "--output <private-reviewed-shadow-series.json>"
        ),
        "verify_command": (
            "opc-finance-box pilot-shadow-series-verify <box-config.json> "
            "<private-reviewed-shadow-series.json> <private-period-evidence-root> "
            "--runs-root <private-pipeline-runs-root> --as-of <YYYY-MM-DD>"
        ),
        "minimum_consecutive_periods": 2,
        "maximum_periods": 24,
        "every_period_source_reverification_required": True,
        "series_reviewer_separation_required": True,
        "artifact_file_mode": "0600",
        "overwrite_allowed": False,
        "raw_financial_values_persisted": False,
        "eligible_to_prepare_stable_promotion_evidence_only": True,
        "ready_for_stable_promotion": False,
        "ready_for_statutory_release": False,
        "ready_for_external_filing": False,
        "external_actions_authorized": False,
    })
    for connector in connectors:
        if connector.get("network_access"):
            setup_tasks.append({
                "task_id": f"connector-runtime:{connector['connector_id']}",
                "category": "connector_runtime",
                "severity": "required_before_live_action",
                "owner_role": "box_maintainer",
                "summary": (
                    f"为网络 Connector {connector['connector_id']} 配置 secret 引用、受控同步窗口、"
                    "checkpoint 复核人、分页/限流告警和隔离失败负责人"
                ),
                "credential_env": list(connector.get("credential_env") or []),
                "sync_window": connector.get("sync_window"),
                "secret_values_included": False,
            })
    if any(connector.get("network_access") for connector in connectors):
        setup_tasks.append({
            "task_id": "connector-access-alert-routing",
            "category": "connector_access",
            "severity": "required_before_live_connector_schedule",
            "owner_role": "connector_security_reviewer",
            "summary": (
                "按 Connector Pack + 法律主体读取权限回执生命周期，生成稳定、"
                "无路径无凭据的临期、到期和完整性告警候选"
            ),
            "alert_command": (
                "opc-finance-box connector-access-alerts <box-config.json> "
                "<absolute-private-activation-root> --as-of <YYYY-MM-DD>"
            ),
            "alert_schedule_installed": False,
            "notifications_sent": False,
            "network_access_performed": False,
            "paths_or_credentials_returned": False,
            "external_actions_performed": False,
        })
        setup_tasks.append({
            "task_id": "connector-shadow-registry-activation",
            "category": "connector_evidence",
            "severity": "required_before_stable_promotion",
            "owner_role": "connector_shadow_controller",
            "summary": (
                "把当前真实匿名 Connector Shadow 独立复核件装入私有轮换目录，"
                "验证权限、时效、重复 scope 和所选网络 Connector Pack 覆盖"
            ),
            "command": (
                "opc-finance-box connector-shadow-status <box-config.json> "
                "--review-dir <absolute-private-connector-shadow-review-dir> "
                "--as-of <YYYY-MM-DD>"
            ),
            "expected": {
                "activation_status": "current",
                "pack_coverage_complete": True,
                "ready_for_connector_shadow_evidence": True,
            },
            "directory_mode": "0700",
            "artifact_mode": "0600",
            "paths_or_private_evidence_returned": False,
            "stable_promotion_performed": False,
            "external_actions_performed": False,
        })
    setup_tasks.append({
        "task_id": "stable-promotion-evidence",
        "category": "release_control",
        "severity": "required_before_stable_promotion",
        "owner_role": "release_reviewer",
        "summary": (
            "用代表性脱敏数据完成至少两个连续月份的 Shadow Close，将逐主体报告和"
            "多主体组合签认精确绑定到已独立复核的连续期收据，锁定自动门和恢复演练证据，"
            "再由独立发布复核人复核 stable candidate"
        ),
        "policy_file": "stable-promotion-policy.json",
        "template_file": "stable-promotion-evidence-templates.json",
        "schema_file": "stable-promotion-evidence.schema.json",
        "shadow_close_cli": [
            "shadow-close-template", "shadow-close-compare", "shadow-close-review",
            "shadow-close-verify", "shadow-close-portfolio-assemble",
            "shadow-close-portfolio-review", "shadow-close-portfolio-verify",
            "connector-shadow-assess", "connector-shadow-review", "connector-shadow-verify",
            "connector-shadow-status",
        ],
        "shadow_close_artifacts_are_private_and_non_overwriting": True,
        "reviewed_consecutive_shadow_series_required": True,
        "minimum_consecutive_periods": 2,
        "exact_series_report_and_portfolio_binding_required": True,
        "pack_manifest_changed_automatically": False,
    })
    blockers = [task for task in setup_tasks if task["severity"].startswith("blocking")]
    data_model = _build_data_model(capabilities)
    agent_contracts = _build_agent_contracts(capabilities)
    job_plan = _build_job_plan(workflows, services, pipelines)
    dashboard_layout = _build_dashboard_layout(capabilities, context["entities"])
    cfo_control_overlay = build_cfo_control_overlay(
        (pack["id"] for pack in context["packs"]),
        runtime_fingerprint=context["runtime"]["fingerprint"],
    )
    cfo_metric_catalog = build_cfo_metric_catalog(
        (pack["id"] for pack in context["packs"]),
        runtime_fingerprint=context["runtime"]["fingerprint"],
    )
    jurisdiction_rules = _build_jurisdiction_rules(runtime, context["entities"])
    release_gates = _build_release_gates(context, declared_only)
    skill_catalog = {
        "schema_version": 1,
        "installation_status": "not_auto_installed",
        "skills": [dict(item) for item in PRODUCT_SKILLS],
        "control_note": "Skills 提供程序化知识，不授予付款、关账、过账或申报权限。",
    }
    return {
        "schema_version": 1,
        "box": context["product"],
        "lock": {
            "runtime_fingerprint": context["runtime"]["fingerprint"],
            "packs": [
                {key: pack[key] for key in ("id", "version", "status")}
                for pack in context["packs"]
            ],
        },
        "entities": context["entities"],
        "management_scope": context["scope"],
        "capabilities": sorted(capabilities),
        "services": services,
        "connectors": connectors,
        "pipelines": pipelines,
        "pipeline_request_templates": pipeline_request_templates,
        "pipeline_run_policy": pipeline_run_policy,
        "pipeline_schedule_template": pipeline_schedule_template,
        "runtime_security_policy": runtime_security_policy,
        "deployment_environment_contract": deployment_environment_contract,
        "runtime_data_contract": runtime_data_contract,
        "connector_sync_policy": connector_sync_policy,
        "stable_promotion_policy": promotion_policy,
        "stable_promotion_evidence_templates": promotion_templates,
        "stable_promotion_evidence_schema": promotion_evidence_schema,
        "pilot_readiness_plan": pilot_readiness_plan,
        "production_readiness_plan": production_readiness_plan,
        "pilot_readiness_artifact_schema": pilot_readiness_artifact_schema,
        "pilot_data_handoff_plan": pilot_data_handoff_plan,
        "pilot_data_handoff_artifact_schema": pilot_data_handoff_artifact_schema,
        "pilot_shadow_run_registration_schema": (
            pilot_shadow_run_registration_schema
        ),
        "pilot_shadow_observation_artifact_schema": (
            pilot_shadow_observation_artifact_schema
        ),
        "pilot_shadow_series_artifact_schema": (
            pilot_shadow_series_artifact_schema
        ),
        "workflows": workflows,
        "job_plan": job_plan,
        "data_model": data_model,
        "agent_contracts": agent_contracts,
        "dashboard_layout": dashboard_layout,
        "cfo_control_overlay": cfo_control_overlay,
        "cfo_metric_catalog": cfo_metric_catalog,
        "cfo_metric_evaluation_request_schema": cfo_metric_evaluation_request_schema,
        "cfo_metric_operand_assembly_schema": cfo_metric_operand_assembly_schema,
        "jurisdiction_rules": jurisdiction_rules,
        "tax_applicability_questionnaire": tax_applicability_questionnaire,
        "tax_applicability_artifact_schema": tax_applicability_artifact_schema,
        "tax_applicability_registry_receipt_schema": (
            tax_applicability_registry_receipt_schema
        ),
        "tax_applicability_artifact_security_policy": (
            tax_applicability_artifact_security_policy
        ),
        "upgrade_policy": build_upgrade_policy({"lock": {
            "runtime_fingerprint": context["runtime"]["fingerprint"],
        }}),
        "release_gates": release_gates,
        "skill_catalog": skill_catalog,
        "capability_coverage": coverage,
        "declared_only_capabilities": declared_only,
        "setup_tasks": setup_tasks,
        "deployment": {
            "ready_for_internal_demo": bool(services) and any(
                item["implementation_status"] == "executable" for item in workflows
            ),
            "ready_for_external_filing": not blockers and context["product"]["production_ready"],
            "external_actions_default": "disabled",
            "recommended_jobs_are_not_installed": True,
            "pipeline_scheduler_available": True,
            "pipeline_schedule_installed": False,
            "server_binding_default": "127.0.0.1",
            "remote_binding_requires_authentication": True,
            "api_authentication_modes": ["anonymous_loopback", "legacy_admin_token", "role_policy"],
            "role_policy_supports_operator_reviewer_separation": True,
            "deployment_environment_contract_available": True,
            "runtime_data_contract_available": True,
            "connector_sync_control_available": bool(
                connector_sync_policy["incremental_connectors"]
            ),
            "stable_promotion_control_available": True,
            "isolated_smoke_verifier_available": True,
        },
        "warnings": context["warnings"],
        "guardrails": context["guardrails"],
    }


def compile_box_file(
    config_path: str | Path,
    packs_root: str | Path,
    registry: PackServiceRegistry | None = None,
) -> dict[str, Any]:
    return compile_box(BoxRuntime(config_path, packs_root), registry)


def render_box_readme(compiled: dict[str, Any]) -> str:
    lines = [
        f"# {compiled['box']['name']}",
        "",
        "此目录由 OPC Finance Box 编译器生成。它是装配与部署契约，不代表税务申报或外部动作已获批准。",
        "",
        "## 已选择 Pack",
        "",
    ]
    lines.extend(
        f"- `{pack['id']}` {pack['version']}（{pack['status']}）"
        for pack in compiled["lock"]["packs"]
    )
    lines.extend(["", "## 法律主体", ""])
    lines.extend(
        f"- `{entity['id']}` · {entity['name']} · {entity['jurisdiction']} · {entity['functional_currency']}"
        for entity in compiled["entities"]
    )
    lines.extend(["", "## 启用工作流", ""])
    lines.extend(
        f"- {workflow['display_name']}（{workflow['cadence']}）"
        for workflow in compiled["workflows"]
    )
    lines.extend([
        "",
        "## 可编辑构件",
        "",
        "- `data-model.json`：标准财务对象与不可破坏的控制约束。",
        "- `agent-contracts.json`：Agent 输入、允许输出与禁止声明。",
        "- `agent-prompts.md`：由当前能力生成、可 fork 的 Agent 提示模板。",
        "- `service-catalog.json` / `connector-catalog.json`：当前 Box 可调用 provider。",
        "- `pipeline-catalog.json`：Connector、质量门与确定性 Service 的端到端编排契约。",
        "- `pipeline-request-templates.json`：按当前法律主体生成、无 secret 且故意失败关闭的请求起点。",
        "- `pipeline-run-policy.json`：运行台账、幂等历史、复核状态与外部动作禁用边界。",
        "- `runtime-security-policy.json`：本地绑定、API principal、角色分权与 actor 绑定边界。",
        "- `deployment-environment-contract.json`：逐 Box 环境变量、持久卷、网络、健康检查与 secret 引用契约。",
        "- `runtime-data-contract.json`：版本化运行目录、离线备份、升级预检与空目标恢复契约。",
        "- `connector-sync-policy.json`：跨运行水位、受控 backfill、限流重试与失败隔离契约。",
        "- `stable-promotion-policy.json`：Shadow Close 覆盖阈值、运维演练和三方职责分离的 stable candidate 契约。",
        "- `stable-promotion-evidence-templates.json` / `stable-promotion-evidence.schema.json`：按当前 Box/Pack 生成、故意未完成的真实证据填写起点与机器契约。",
        "- `pilot-readiness-plan.json` / `pilot-readiness-artifact.schema.json` 与 `pilot-readiness-init` / `pilot-readiness-review` / `pilot-readiness-verify` / `pilot-readiness-alerts`：首家真实 OPC 的逐主体、行业资料域与网络 Connector 准入契约；运行时只读挂载 `OPC_PILOT_READINESS_REVIEW`，到期即关闭新 Shadow Run，且不批准法定关账或申报。",
        "- `pilot-data-handoff-plan.json` / `pilot-data-handoff-artifact.schema.json` 与 `pilot-data-handoff-init` / `pilot-data-handoff-review` / `pilot-data-handoff-verify`：把首家 OPC 的真实资料交接绑定到当前准入签认、主体、期间和行业资料域；只记录传输控制、计数与 SHA-256 清单指纹，不复制源文件、文件名、路径、账号、税号或金额。",
        "- `pilot-shadow-run-registration.schema.json` 与 `pilot-shadow-run-register` / `pilot-shadow-run-verify`：把资料交接期间与逐主体、全 gate 已独立复核的月结 Pipeline 台账绑定；后续驳回或台账篡改即失效，且不保存金额、不授权过账、付款、关账或申报。",
        "- `pilot-shadow-observation-artifact.schema.json` 与 `pilot-shadow-observation-assemble` / `pilot-shadow-observation-review` / `pilot-shadow-observation-verify`：把首次登记、逐主体 Shadow 报告和多主体组合签认按内容指纹及 attempt 集合闭环；第四角色独立复核，system defect 强制修正，凭证不含金额且不能晋级 stable。",
        "- `pilot-shadow-series-artifact.schema.json` 与 `pilot-shadow-series-assemble` / `pilot-shadow-series-review` / `pilot-shadow-series-verify`：重新验证 2–24 个连续月份的全部 Pilot 私有源证据，生成无金额的跨期计数趋势并由独立连续性复核人签认；通过只允许准备 stable 晋级证据，不等于 Pack 已晋级。",
        "- `shadow-close-template` / `shadow-close-compare` / `shadow-close-review` / `shadow-close-verify`：在 repo 或 CI 中生成并校验私有、不覆盖且精确指纹绑定的逐主体真实样本证据。",
        "- `shadow-close-portfolio-assemble` / `shadow-close-portfolio-review` / `shadow-close-portfolio-verify`：把所有主体签认与台账验证的管理组合绑定成不含金额的多主体验收包，并由不同复核人签认。",
        "- `connector-shadow-assess` / `connector-shadow-review` / `connector-shadow-verify`：把独立来源计数基线与四来源 Pipeline 结果按 SHA-256 绑定，只保存计数/布尔值并强制独立复核。",
        "- `connector-shadow-status`：只读检查 `0700` 私有轮换目录中的 `0600` 真实复核件，验证时效、重复 scope 与所选网络 Connector Pack 覆盖；不返回路径、人员、证据、源控制或金额，也不执行 stable 晋级。",
        "- `workflow-plan.json` / `job-plan.json`：工作流与默认禁用的调度建议。",
        "- `dashboard-layout.json`：按 capability 生成的面板和主体切换边界。",
        "- `cfo-control-overlay.json`：由行业、渠道和 Connector Pack 生成的可 fork CFO 月度控制重点、数据源边界与创始人复盘问题。",
        "- `cfo-metric-catalog.json`：由业务 Pack 生成的可 fork 指标公式、可信来源映射、必需数据域、控制条件、决策用途与聚合边界；不包含实际财务值。",
        "- `cfo-metric-evaluation-request.schema.json` / `cfo-metric-operand-assembly.schema.json`：手工指标请求与可信 Pipeline/Service 自动组装结果的通用结构合同。",
        "- `jurisdiction-rules.json`：逐主体锁定的税务 Pack、来源和结构化规则。",
        "- `tax-applicability-questionnaire.json` / `tax-applicability-artifact.schema.json`：逐主体、不含税号的 Pack 适用性问卷与私有签认结构契约。",
        "- `tax-applicability-init` / `tax-applicability-review` / `tax-applicability-verify` / `tax-applicability-portfolio-verify` / `tax-applicability-import` / `tax-applicability-status` / `tax-applicability-registry-seal` / `tax-applicability-registry-verify` / `tax-applicability-alerts`：逐主体按 facts_as_of 生成私有工作底稿，由不同的当地税务复核人签认，再验证全主体未过期覆盖、受控导入和独立 controller 封存的轮换目录内容；告警命令只生成安全候选且默认不发送，全部命令仅返回不含路径、回答、理由和证据内容的安全摘要，收据不是数字签名或申报授权。",
        "- `upgrade-policy.json` / `release-gates.json`：升级兼容性与发布验收门。",
        "- `skill-catalog.json`：随产品提供但不会自动安装的可 fork Codex Skills。",
        "",
        "## 上线边界",
        "",
        f"- 内部演示：{'可用' if compiled['deployment']['ready_for_internal_demo'] else '仍有阻塞'}",
        f"- 外部申报：{'可用' if compiled['deployment']['ready_for_external_filing'] else '未启用'}",
        "- 定时任务只提供建议，编译不会自行安装 cron job。",
        "- 付款、过账、关账和申报仍由 review gate 控制。",
        "",
    ])
    return "\n".join(lines)


def render_agent_prompts(compiled: dict[str, Any]) -> str:
    lines = [
        f"# {compiled['box']['name']} · Editable Agent Prompts",
        "",
        "这些模板用于组织事实、问题和候选产物，不构成运行时授权。法律主体范围、确定性计算、review gate 与外部动作禁用仍由代码强制执行。",
        "",
        "## 全局系统约束",
        "",
        "你是 OPC Finance Box 中的财务运营 Agent。先确认法律主体、期间、币种、数据来源和证据覆盖，再解释或生成候选产物。",
        "不要把缺失事实填成零，不要混加币种，不要把管理合并写回主体法定账，不要把草稿说成已过账、已关账或已申报。",
        "付款、会计政策、关账、税务判断和外部提交必须停在配置的人工 review gate。",
        "",
    ]
    for contract in compiled["agent_contracts"]["contracts"]:
        lines.extend([
            f"## `{contract['contract_id']}`",
            "",
            f"目标：{contract['purpose']}",
            "",
            "必需输入：",
            "",
        ])
        lines.extend(f"- `{item}`" for item in contract["required_inputs"])
        lines.extend(["", "允许输出：", ""])
        lines.extend(f"- `{item}`" for item in contract["allowed_outputs"])
        lines.extend(["", "禁止声明或动作：", ""])
        lines.extend(f"- {item}" for item in contract["prohibited_claims"])
        lines.extend([
            "",
            "建议输出结构：`scope`、`facts`、`calculations`、`exceptions`、`questions`、`candidate_deliverables`、`review_gate`。",
            "",
        ])
    return "\n".join(lines)


def write_compiled_box(compiled: dict[str, Any], output_dir: str | Path) -> list[Path]:
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    lock_path = destination / "box.lock.json"
    setup_path = destination / "setup-checklist.json"
    service_path = destination / "service-catalog.json"
    connector_path = destination / "connector-catalog.json"
    pipeline_catalog_path = destination / "pipeline-catalog.json"
    pipeline_templates_path = destination / "pipeline-request-templates.json"
    pipeline_run_policy_path = destination / "pipeline-run-policy.json"
    pipeline_schedule_path = destination / "pipeline-schedule-template.json"
    runtime_security_policy_path = destination / "runtime-security-policy.json"
    deployment_environment_path = destination / "deployment-environment-contract.json"
    runtime_data_contract_path = destination / "runtime-data-contract.json"
    connector_sync_policy_path = destination / "connector-sync-policy.json"
    stable_promotion_policy_path = destination / "stable-promotion-policy.json"
    stable_promotion_templates_path = destination / "stable-promotion-evidence-templates.json"
    stable_promotion_schema_path = destination / "stable-promotion-evidence.schema.json"
    pilot_readiness_plan_path = destination / "pilot-readiness-plan.json"
    production_readiness_plan_path = destination / "production-readiness-plan.json"
    pilot_readiness_schema_path = destination / "pilot-readiness-artifact.schema.json"
    pilot_data_handoff_plan_path = destination / "pilot-data-handoff-plan.json"
    pilot_data_handoff_schema_path = destination / "pilot-data-handoff-artifact.schema.json"
    pilot_shadow_run_schema_path = (
        destination / "pilot-shadow-run-registration.schema.json"
    )
    pilot_shadow_observation_schema_path = (
        destination / "pilot-shadow-observation-artifact.schema.json"
    )
    pilot_shadow_series_schema_path = (
        destination / "pilot-shadow-series-artifact.schema.json"
    )
    workflow_path = destination / "workflow-plan.json"
    job_path = destination / "job-plan.json"
    data_model_path = destination / "data-model.json"
    dashboard_path = destination / "dashboard-layout.json"
    cfo_control_overlay_path = destination / "cfo-control-overlay.json"
    cfo_metric_catalog_path = destination / "cfo-metric-catalog.json"
    cfo_metric_evaluation_schema_path = (
        destination / "cfo-metric-evaluation-request.schema.json"
    )
    cfo_metric_assembly_schema_path = (
        destination / "cfo-metric-operand-assembly.schema.json"
    )
    agent_path = destination / "agent-contracts.json"
    prompts_path = destination / "agent-prompts.md"
    rules_path = destination / "jurisdiction-rules.json"
    tax_applicability_path = destination / "tax-applicability-questionnaire.json"
    tax_applicability_schema_path = destination / "tax-applicability-artifact.schema.json"
    tax_applicability_security_path = (
        destination / "tax-applicability-artifact-security-policy.json"
    )
    tax_applicability_receipt_schema_path = (
        destination / "tax-applicability-registry-receipt.schema.json"
    )
    upgrade_path = destination / "upgrade-policy.json"
    release_path = destination / "release-gates.json"
    skill_path = destination / "skill-catalog.json"
    readme_path = destination / "README.md"
    lock_path.write_text(json.dumps(compiled, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    setup_path.write_text(
        json.dumps(compiled["setup_tasks"], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    for path, payload in (
        (service_path, compiled["services"]),
        (connector_path, compiled["connectors"]),
        (pipeline_catalog_path, compiled["pipelines"]),
        (pipeline_templates_path, compiled["pipeline_request_templates"]),
        (pipeline_run_policy_path, compiled["pipeline_run_policy"]),
        (pipeline_schedule_path, compiled["pipeline_schedule_template"]),
        (runtime_security_policy_path, compiled["runtime_security_policy"]),
        (deployment_environment_path, compiled["deployment_environment_contract"]),
        (runtime_data_contract_path, compiled["runtime_data_contract"]),
        (connector_sync_policy_path, compiled["connector_sync_policy"]),
        (stable_promotion_policy_path, compiled["stable_promotion_policy"]),
        (
            stable_promotion_templates_path,
            compiled["stable_promotion_evidence_templates"],
        ),
        (stable_promotion_schema_path, compiled["stable_promotion_evidence_schema"]),
        (pilot_readiness_plan_path, compiled["pilot_readiness_plan"]),
        (production_readiness_plan_path, compiled["production_readiness_plan"]),
        (pilot_readiness_schema_path, compiled["pilot_readiness_artifact_schema"]),
        (pilot_data_handoff_plan_path, compiled["pilot_data_handoff_plan"]),
        (
            pilot_data_handoff_schema_path,
            compiled["pilot_data_handoff_artifact_schema"],
        ),
        (
            pilot_shadow_run_schema_path,
            compiled["pilot_shadow_run_registration_schema"],
        ),
        (
            pilot_shadow_observation_schema_path,
            compiled["pilot_shadow_observation_artifact_schema"],
        ),
        (
            pilot_shadow_series_schema_path,
            compiled["pilot_shadow_series_artifact_schema"],
        ),
        (workflow_path, compiled["workflows"]),
        (job_path, compiled["job_plan"]),
        (data_model_path, compiled["data_model"]),
        (dashboard_path, compiled["dashboard_layout"]),
        (cfo_control_overlay_path, compiled["cfo_control_overlay"]),
        (cfo_metric_catalog_path, compiled["cfo_metric_catalog"]),
        (
            cfo_metric_evaluation_schema_path,
            compiled["cfo_metric_evaluation_request_schema"],
        ),
        (
            cfo_metric_assembly_schema_path,
            compiled["cfo_metric_operand_assembly_schema"],
        ),
        (agent_path, compiled["agent_contracts"]),
        (rules_path, compiled["jurisdiction_rules"]),
        (tax_applicability_path, compiled["tax_applicability_questionnaire"]),
        (
            tax_applicability_schema_path,
            compiled["tax_applicability_artifact_schema"],
        ),
        (
            tax_applicability_security_path,
            compiled["tax_applicability_artifact_security_policy"],
        ),
        (
            tax_applicability_receipt_schema_path,
            compiled["tax_applicability_registry_receipt_schema"],
        ),
        (upgrade_path, compiled["upgrade_policy"]),
        (release_path, compiled["release_gates"]),
        (skill_path, compiled["skill_catalog"]),
    ):
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    readme_path.write_text(render_box_readme(compiled), encoding="utf-8")
    prompts_path.write_text(render_agent_prompts(compiled), encoding="utf-8")
    return [
        lock_path, setup_path, service_path, connector_path, pipeline_catalog_path,
        pipeline_templates_path, pipeline_run_policy_path, pipeline_schedule_path,
        runtime_security_policy_path, deployment_environment_path,
        runtime_data_contract_path,
        connector_sync_policy_path,
        stable_promotion_policy_path,
        stable_promotion_templates_path,
        stable_promotion_schema_path,
        pilot_readiness_plan_path, production_readiness_plan_path,
        pilot_readiness_schema_path,
        pilot_data_handoff_plan_path, pilot_data_handoff_schema_path,
        pilot_shadow_run_schema_path, pilot_shadow_observation_schema_path,
        pilot_shadow_series_schema_path,
        workflow_path, job_path,
        data_model_path, dashboard_path, cfo_control_overlay_path,
        cfo_metric_catalog_path, cfo_metric_evaluation_schema_path,
        cfo_metric_assembly_schema_path,
        agent_path, prompts_path, rules_path,
        tax_applicability_path, tax_applicability_schema_path,
        tax_applicability_security_path, tax_applicability_receipt_schema_path,
        upgrade_path,
        release_path, skill_path, readme_path,
    ]
