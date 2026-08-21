from __future__ import annotations

from typing import Any

from .agent_services import build_plan_snapshot, create_approval_event_draft, create_goal_draft
from .accounting_import import validate_trial_balance_lines
from .ledger_import import reconcile_ledger_to_trial_balance
from .month_close_control import build_month_close_control
from .first_close_discovery import discover_first_close_configuration
from .commerce import build_commerce_analysis, build_return_inventory_reconciliation
from .commerce_import_costs import build_import_landed_cost_candidates
from .commerce_runner import run_commerce_box
from .commerce_services import (
    summarize_commerce_refunds,
    summarize_destination_evidence,
    summarize_fulfillment_costs,
)
from .core_services import (
    assess_close_readiness,
    build_cash_forecast,
    reconcile_bank_activity,
    summarize_procure_to_pay,
    validate_evidence_lineage,
)
from .cfo_metric_evaluator import evaluate_cfo_metrics
from .document_services import extract_image_ocr, extract_text_pdf
from .expense_evidence_services import review_expense_evidence
from .game_services import analyze_game_kpis, calculate_game_revenue_recognition, draft_game_revenue_policy
from .game_finance_services import (
    calculate_game_project_profitability,
    reconcile_game_channel_settlements,
    review_game_ltv_roi,
)
from .inventory_costing import calculate_inventory_cost
from .multi_entity_services import (
    build_month_close_portfolio,
    consolidate_management_view,
    review_intercompany_adjustments,
    translate_management_balances,
)
from .marketplace_services import (
    reconcile_marketplace_fees,
    reconcile_marketplace_inventory,
    reconcile_marketplace_receivable,
)
from .pack_services import PackServiceRegistry, ServiceContext, ServiceDefinition
from .tax_calendar import build_tax_calendar
from .tax_profile_services import build_sg_evidence_checklist, build_sg_registration_profile
from .tax_return_services import (
    build_cn_cit_prepaid_workpaper,
    build_cn_iit_withholding_workpaper,
    build_cn_stamp_tax_workpaper,
    build_cn_vat_workpaper,
)
from .stripe_services import reconcile_stripe_payouts, summarize_stripe_balance_activity
from .shopify_services import (
    build_shopify_monthly_commerce_scope, summarize_shopify_order_activity,
)
from .dtc_integration_services import reconcile_shopify_stripe_activity
from .shipbob_services import summarize_shipbob_fulfillment_evidence
from .paypal_services import summarize_paypal_transaction_activity
from .woocommerce_services import summarize_woocommerce_order_refund_activity
from .amazon_seller_services import (
    reconcile_amazon_seller_marketplace_evidence,
    summarize_amazon_seller_transaction_activity,
)
from .us_tax_services import (
    build_us_federal_calendar,
    build_us_federal_evidence_checklist,
    build_us_federal_registration_profile,
)
from .hk_tax_services import (
    build_hk_evidence_checklist,
    build_hk_registration_profile,
    build_hk_tax_calendar,
)
from .uk_tax_services import (
    build_uk_evidence_checklist,
    build_uk_registration_profile,
    build_uk_tax_calendar,
)
from .au_tax_services import (
    build_au_evidence_checklist,
    build_au_registration_profile,
    build_au_tax_calendar,
)
from .ca_tax_services import (
    build_ca_evidence_checklist,
    build_ca_registration_profile,
    build_ca_tax_calendar,
)
from .nz_tax_services import (
    build_nz_evidence_checklist,
    build_nz_registration_profile,
    build_nz_tax_calendar,
)
from .ie_tax_services import (
    build_ie_evidence_checklist,
    build_ie_registration_profile,
    build_ie_tax_calendar,
)
from .nl_tax_services import (
    build_nl_evidence_checklist,
    build_nl_registration_profile,
    build_nl_tax_calendar,
)
from .de_tax_services import (
    build_de_evidence_checklist,
    build_de_registration_profile,
    build_de_tax_calendar,
)
from .fr_tax_services import (
    build_fr_evidence_checklist,
    build_fr_registration_profile,
    build_fr_tax_calendar,
)
from .jp_tax_services import (
    build_jp_evidence_checklist,
    build_jp_registration_profile,
    build_jp_tax_calendar,
)
from .kr_tax_services import (
    build_kr_evidence_checklist,
    build_kr_registration_profile,
    build_kr_tax_calendar,
)
from .ae_tax_services import (
    build_ae_evidence_checklist,
    build_ae_registration_profile,
    build_ae_tax_calendar,
)


def _analyze_commerce(payload: dict[str, Any], context: ServiceContext) -> dict[str, Any]:
    return build_commerce_analysis(
        payload.get("orders") or [],
        payload.get("settlements") or [],
        tolerance=payload.get("tolerance", 0.01),
        allowed_entity_ids=set(context.entity_ids),
    )


def _run_commerce_inputs(payload: dict[str, Any], context: ServiceContext) -> dict[str, Any]:
    return run_commerce_box(
        context.runtime,
        payload.get("input_paths") or [],
        default_entity_id=payload.get("default_entity_id"),
        default_channel=payload.get("default_channel"),
    )


def _reconcile_return_inventory(
    payload: dict[str, Any], context: ServiceContext,
) -> dict[str, Any]:
    return build_return_inventory_reconciliation(
        payload.get("returns") or [],
        payload.get("return_receipts") or [],
        order_rows=payload.get("orders") or [],
        allowed_entity_ids=set(context.entity_ids),
    )


def _build_import_landed_cost(
    payload: dict[str, Any], context: ServiceContext,
) -> dict[str, Any]:
    return build_import_landed_cost_candidates(
        payload.get("import_costs") or [], allowed_entity_ids=set(context.entity_ids),
    )


def _build_tax_calendar(payload: dict[str, Any], context: ServiceContext) -> dict[str, Any]:
    if not context.entity_id:
        raise ValueError("Tax calendar service requires entity_id")
    return build_tax_calendar(
        context.runtime,
        context.entity_id,
        period_year=payload.get("period_year"),
        anchors=payload.get("anchors"),
        as_of=payload.get("as_of"),
    )


def _validate_trial_balance(payload: dict[str, Any], context: ServiceContext) -> dict[str, Any]:
    if not context.entity_id:
        raise ValueError("Trial balance validation requires entity_id")
    return validate_trial_balance_lines(
        payload.get("trial_balance_lines") or [], entity_id=context.entity_id,
    )


def _reconcile_accounting_close_exports(
    payload: dict[str, Any], context: ServiceContext,
) -> dict[str, Any]:
    if not context.entity_id:
        raise ValueError("Accounting close export reconciliation requires entity_id")
    return reconcile_ledger_to_trial_balance(
        payload.get("general_ledger_lines") or [],
        payload.get("trial_balance_lines") or [],
        payload.get("account_mappings") or [],
        entity_id=context.entity_id,
        period=str(payload.get("period") or ""),
    )


def _build_month_close_control(
    payload: dict[str, Any], context: ServiceContext,
) -> dict[str, Any]:
    if not context.entity_id:
        raise ValueError("Month close control requires entity_id")
    return build_month_close_control(
        payload.get("bank_reconciliation") or {},
        payload.get("accounting_close") or {},
        payload.get("trial_balance_lines") or [],
        payload.get("bank_gl_mappings") or [],
        entity_id=context.entity_id,
        period=str(payload.get("period") or ""),
    )


def _discover_first_close_configuration(
    payload: dict[str, Any], context: ServiceContext,
) -> dict[str, Any]:
    if not context.entity_id:
        raise ValueError("First close discovery requires entity_id")
    return discover_first_close_configuration(
        payload.get("bank_reconciliation") or {},
        payload.get("general_ledger_lines") or [],
        payload.get("trial_balance_lines") or [],
        entity_id=context.entity_id,
        period=str(payload.get("period") or ""),
    )


def _review_airwallex_expense_evidence(
    payload: dict[str, Any], context: ServiceContext,
) -> dict[str, Any]:
    if not context.entity_id:
        raise ValueError("Airwallex expense evidence review requires entity_id")
    return review_expense_evidence(
        payload.get("expense_evidence") or [], entity_id=context.entity_id,
        state_changes=payload.get("expense_evidence_state_changes") or [],
    )


def _evaluate_cfo_metrics(
    payload: dict[str, Any], context: ServiceContext,
) -> dict[str, Any]:
    if not context.entity_id:
        raise ValueError("CFO metric evaluation requires entity_id")
    return evaluate_cfo_metrics(context.runtime, context.entity_id, payload)


def build_default_service_registry() -> PackServiceRegistry:
    registry = PackServiceRegistry()
    registry.register(ServiceDefinition(
        service_id="woocommerce.summarize_order_refund_activity",
        pack_id="connector.woocommerce",
        capability="connector.woocommerce_order_refund_activity",
        display_name="汇总 WooCommerce 订单状态、金额、税额与退款候选",
        handler=summarize_woocommerce_order_refund_activity,
        deterministic=True,
        action_class="read",
        entity_scope="statutory",
        review_gate="woocommerce_order_status_mapping_review",
    ))
    registry.register(ServiceDefinition(
        service_id="paypal.summarize_transaction_activity",
        pack_id="connector.paypal",
        capability="connector.paypal_transaction_activity",
        display_name="汇总 PayPal 交易、费用、退款与余额转出候选",
        handler=summarize_paypal_transaction_activity,
        deterministic=True,
        action_class="read",
        entity_scope="statutory",
        review_gate="paypal_transaction_event_mapping_review",
    ))
    registry.register(ServiceDefinition(
        service_id="shipbob.summarize_fulfillment_evidence",
        pack_id="connector.shipbob",
        capability="connector.shipbob_fulfillment_evidence",
        display_name="汇总 ShipBob 履约成本、交付状态与退货处置候选",
        handler=summarize_shipbob_fulfillment_evidence,
        deterministic=True,
        action_class="read",
        entity_scope="statutory",
        review_gate="shipbob_fulfillment_cost_review",
    ))
    registry.register(ServiceDefinition(
        service_id="airwallex.review_expense_evidence",
        pack_id="connector.airwallex",
        capability="connector.airwallex_approved_expenses",
        display_name="复核 Airwallex 已批准费用、收据与会计映射缺口",
        handler=_review_airwallex_expense_evidence,
        deterministic=True,
        action_class="read",
        entity_scope="statutory",
        review_gate="expense_accounting_mapping_review",
    ))
    registry.register(ServiceDefinition(
        service_id="agent.create_goal_draft",
        pack_id="core.finance",
        capability="agent.goals",
        display_name="生成主体范围明确的财务目标草稿",
        handler=create_goal_draft,
        deterministic=True,
        action_class="draft",
        entity_scope="management",
    ))
    registry.register(ServiceDefinition(
        service_id="agent.build_plan_snapshot",
        pack_id="core.finance",
        capability="agent.plans",
        display_name="根据当前财务事实重算目标计划快照",
        handler=build_plan_snapshot,
        deterministic=False,
        action_class="draft",
        entity_scope="management",
    ))
    registry.register(ServiceDefinition(
        service_id="agent.create_approval_event_draft",
        pack_id="core.finance",
        capability="agent.approvals",
        display_name="生成不改变状态的审批事件草稿",
        handler=create_approval_event_draft,
        deterministic=True,
        action_class="draft",
        entity_scope="management",
    ))
    registry.register(ServiceDefinition(
        service_id="connector.extract_text_pdf",
        pack_id="connector.file_import",
        capability="connector.text_pdf",
        display_name="安全提取文本 PDF（扫描件回退到本地 OCR）",
        handler=extract_text_pdf,
        deterministic=False,
        action_class="read",
        entity_scope="statutory",
        review_gate="low_confidence_document_extraction",
    ))
    registry.register(ServiceDefinition(
        service_id="connector.extract_image_ocr",
        pack_id="connector.file_import",
        capability="connector.local_ocr_optional",
        display_name="使用可选本地 OCR 提取图片文字",
        handler=extract_image_ocr,
        deterministic=False,
        action_class="read",
        entity_scope="statutory",
        review_gate="low_confidence_document_extraction",
    ))
    registry.register(ServiceDefinition(
        service_id="core.reconcile_bank_activity",
        pack_id="core.finance",
        capability="finance.bank_reconciliation",
        display_name="按主体和币种生成银行流水候选对账",
        handler=reconcile_bank_activity,
        deterministic=True,
        action_class="read",
        entity_scope="statutory",
    ))
    registry.register(ServiceDefinition(
        service_id="core.cash_forecast",
        pack_id="core.finance",
        capability="finance.cash_forecast",
        display_name="按主体本位币计算滚动现金预测",
        handler=build_cash_forecast,
        deterministic=True,
        action_class="read",
        entity_scope="statutory",
    ))
    registry.register(ServiceDefinition(
        service_id="core.evaluate_cfo_metrics",
        pack_id="core.finance",
        capability="finance.cfo_metrics",
        display_name="按主体、期间和本位币确定性计算 Pack 驱动的 CFO 指标",
        handler=_evaluate_cfo_metrics,
        deterministic=True,
        action_class="read",
        entity_scope="statutory",
    ))
    registry.register(ServiceDefinition(
        service_id="core.close_readiness",
        pack_id="core.finance",
        capability="finance.record_to_report",
        display_name="评估单一主体月结与关账准备度",
        handler=assess_close_readiness,
        deterministic=True,
        action_class="read",
        entity_scope="statutory",
        review_gate="period_close",
    ))
    registry.register(ServiceDefinition(
        service_id="core.validate_trial_balance_import",
        pack_id="core.finance",
        capability="finance.record_to_report",
        display_name="按主体、期间和币种确定性校验试算平衡",
        handler=_validate_trial_balance,
        deterministic=True,
        action_class="read",
        entity_scope="statutory",
        review_gate="trial_balance_control_total_review",
    ))
    registry.register(ServiceDefinition(
        service_id="core.reconcile_accounting_close_exports",
        pack_id="core.finance",
        capability="finance.record_to_report",
        display_name="勾稽总账明细、试算平衡与显式报表科目映射",
        handler=_reconcile_accounting_close_exports,
        deterministic=True,
        action_class="read",
        entity_scope="statutory",
        review_gate="financial_statement_mapping_review",
    ))
    registry.register(ServiceDefinition(
        service_id="core.build_month_close_control",
        pack_id="core.finance",
        capability="finance.record_to_report",
        display_name="把银行期末余额与显式 GL 现金科目勾稽为三方月结控制候选",
        handler=_build_month_close_control,
        deterministic=True,
        action_class="read",
        entity_scope="statutory",
        review_gate="month_close_control_review",
    ))
    registry.register(ServiceDefinition(
        service_id="core.discover_first_close_configuration",
        pack_id="core.finance",
        capability="finance.record_to_report",
        display_name="盘点首月结来源并生成不推断的 fail-closed 映射起点",
        handler=_discover_first_close_configuration,
        deterministic=True,
        action_class="read",
        entity_scope="statutory",
        review_gate="first_close_configuration_review",
    ))
    registry.register(ServiceDefinition(
        service_id="core.procure_to_pay_summary",
        pack_id="core.finance",
        capability="finance.procure_to_pay",
        display_name="按主体和币种汇总采购到付款状态",
        handler=summarize_procure_to_pay,
        deterministic=True,
        action_class="read",
        entity_scope="statutory",
    ))
    registry.register(ServiceDefinition(
        service_id="core.validate_evidence_lineage",
        pack_id="core.finance",
        capability="audit.evidence_lineage",
        display_name="检查标准数据集的主体与证据链",
        handler=validate_evidence_lineage,
        deterministic=True,
        action_class="read",
        entity_scope="management",
    ))
    registry.register(ServiceDefinition(
        service_id="entity.build_month_close_portfolio",
        pack_id="feature.multi_entity",
        capability="entity.management_consolidation",
        display_name="组合已生成的单主体月结候选与显式汇率为创始人组合视图",
        handler=build_month_close_portfolio,
        deterministic=True,
        action_class="read",
        entity_scope="management",
        review_gate="month_close_portfolio_review",
    ))
    registry.register(ServiceDefinition(
        service_id="entity.translate_management_balances",
        pack_id="feature.multi_entity",
        capability="entity.reporting_currency_translation",
        display_name="按显式汇率换算多主体管理余额",
        handler=translate_management_balances,
        deterministic=True,
        action_class="read",
        entity_scope="management",
    ))
    registry.register(ServiceDefinition(
        service_id="entity.review_intercompany_adjustments",
        pack_id="feature.multi_entity",
        capability="entity.intercompany_elimination",
        display_name="校验内部往来管理抵销调整",
        handler=review_intercompany_adjustments,
        deterministic=True,
        action_class="read",
        entity_scope="management",
        review_gate="consolidation_adjustment",
    ))
    registry.register(ServiceDefinition(
        service_id="entity.consolidate_management_view",
        pack_id="feature.multi_entity",
        capability="entity.management_consolidation",
        display_name="生成保留法定主体来源的管理合并视图",
        handler=consolidate_management_view,
        deterministic=True,
        action_class="read",
        entity_scope="management",
        review_gate="consolidation_adjustment",
    ))
    registry.register(ServiceDefinition(
        service_id="commerce.analyze",
        pack_id="industry.commerce",
        capability="commerce.product_margin",
        display_name="计算订单、渠道结算和贡献利润",
        handler=_analyze_commerce,
        deterministic=True,
        action_class="read",
        entity_scope="management",
    ))
    registry.register(ServiceDefinition(
        service_id="commerce.order_to_cash",
        pack_id="industry.commerce",
        capability="commerce.order_to_cash",
        display_name="核对 Commerce 订单、渠道结算与到账",
        handler=_analyze_commerce,
        deterministic=True,
        action_class="read",
        entity_scope="management",
    ))
    registry.register(ServiceDefinition(
        service_id="commerce.refund_summary",
        pack_id="industry.commerce",
        capability="commerce.refunds",
        display_name="按主体、渠道和币种汇总退款",
        handler=summarize_commerce_refunds,
        deterministic=True,
        action_class="read",
        entity_scope="management",
    ))
    registry.register(ServiceDefinition(
        service_id="commerce.reconcile_return_inventory",
        pack_id="industry.commerce",
        capability="commerce.return_inventory_reconciliation",
        display_name="核对退货授权、退款数量、仓库实收与处置候选",
        handler=_reconcile_return_inventory,
        deterministic=True,
        action_class="draft",
        entity_scope="management",
        review_gate="return_disposition_review",
    ))
    registry.register(ServiceDefinition(
        service_id="commerce.build_import_landed_cost_candidates",
        pack_id="industry.commerce",
        capability="commerce.import_landed_cost",
        display_name="按主体、币种、SKU 与仓库生成进口 landed-cost 候选",
        handler=_build_import_landed_cost,
        deterministic=True,
        action_class="draft",
        entity_scope="management",
        review_gate="import_landed_cost_policy",
    ))
    registry.register(ServiceDefinition(
        service_id="commerce.fulfillment_cost_summary",
        pack_id="industry.commerce",
        capability="commerce.fulfillment_cost",
        display_name="汇总履约、物流与订单贡献",
        handler=summarize_fulfillment_costs,
        deterministic=True,
        action_class="read",
        entity_scope="management",
    ))
    registry.register(ServiceDefinition(
        service_id="commerce.calculate_inventory_cost",
        pack_id="industry.commerce",
        capability="commerce.inventory_cost",
        display_name="按批准方法计算 FIFO 或移动加权库存成本底稿",
        handler=calculate_inventory_cost,
        deterministic=True,
        action_class="draft",
        entity_scope="statutory",
        review_gate="inventory_valuation_policy",
    ))
    registry.register(ServiceDefinition(
        service_id="commerce.import_and_analyze",
        pack_id="channel.dtc_storefront",
        capability="channel.dtc_order_import",
        display_name="导入并核对 DTC 订单和渠道结算",
        handler=_run_commerce_inputs,
        deterministic=True,
        action_class="draft",
        entity_scope="management",
    ))
    registry.register(ServiceDefinition(
        service_id="dtc.reconcile_payments",
        pack_id="channel.dtc_storefront",
        capability="channel.dtc_payment_reconciliation",
        display_name="核对 DTC 订单资金、费用、代缴税与打款",
        handler=_analyze_commerce,
        deterministic=True,
        action_class="read",
        entity_scope="management",
    ))
    registry.register(ServiceDefinition(
        service_id="dtc.reconcile_refunds",
        pack_id="channel.dtc_storefront",
        capability="channel.dtc_refund_reconciliation",
        display_name="生成 DTC 退款核对摘要",
        handler=summarize_commerce_refunds,
        deterministic=True,
        action_class="read",
        entity_scope="management",
    ))
    registry.register(ServiceDefinition(
        service_id="dtc.destination_evidence",
        pack_id="channel.dtc_storefront",
        capability="channel.dtc_destination_summary",
        display_name="生成 DTC 目的地与已收税额证据摘要",
        handler=summarize_destination_evidence,
        deterministic=True,
        action_class="read",
        entity_scope="management",
        review_gate="sales_tax_nexus_review",
    ))
    registry.register(ServiceDefinition(
        service_id="stripe.summarize_balance_activity",
        pack_id="connector.stripe",
        capability="connector.stripe_balance_transactions",
        display_name="按主体、币种和 Stripe reporting category 汇总余额活动",
        handler=summarize_stripe_balance_activity,
        deterministic=True,
        action_class="read",
        entity_scope="statutory",
        review_gate="stripe_mapping_approval",
    ))
    registry.register(ServiceDefinition(
        service_id="shopify.summarize_order_activity",
        pack_id="connector.shopify",
        capability="connector.shopify_orders",
        display_name="核对 Shopify 订单、成功收款、退款与多币种事实",
        handler=summarize_shopify_order_activity,
        deterministic=True,
        action_class="read",
        entity_scope="statutory",
        review_gate="shopify_mapping_approval",
    ))
    registry.register(ServiceDefinition(
        service_id="shopify.build_monthly_commerce_scope",
        pack_id="feature.shopify_stripe_order_to_cash",
        capability="integration.shopify_stripe_monthly_close",
        display_name="由 Shopify 月末双窗口证据生成税外 DTC 月度指标范围",
        handler=build_shopify_monthly_commerce_scope,
        deterministic=True,
        action_class="read",
        entity_scope="statutory",
        review_gate="shopify_mapping_approval",
    ))
    registry.register(ServiceDefinition(
        service_id="dtc.reconcile_shopify_stripe_activity",
        pack_id="feature.shopify_stripe_order_to_cash",
        capability="integration.shopify_stripe_order_to_cash",
        display_name="以显式处理商映射核对 Shopify 收退款与 Stripe 余额活动",
        handler=reconcile_shopify_stripe_activity,
        deterministic=True,
        action_class="read",
        entity_scope="statutory",
        review_gate="processor_link_mapping_approval",
    ))
    registry.register(ServiceDefinition(
        service_id="stripe.reconcile_payouts",
        pack_id="connector.stripe",
        capability="connector.stripe_payouts",
        display_name="核对 Stripe Payout、余额交易与银行到账候选",
        handler=reconcile_stripe_payouts,
        deterministic=True,
        action_class="read",
        entity_scope="statutory",
        review_gate="stripe_mapping_approval",
    ))
    registry.register(ServiceDefinition(
        service_id="amazon_seller.summarize_transaction_activity",
        pack_id="connector.amazon_seller",
        capability="connector.amazon_seller_transaction_activity",
        display_name="汇总 Amazon Seller Finances 交易、费用、退款与结算引用证据",
        handler=summarize_amazon_seller_transaction_activity,
        deterministic=True,
        action_class="read",
        entity_scope="statutory",
        review_gate="amazon_seller_transaction_mapping_review",
    ))
    registry.register(ServiceDefinition(
        service_id="amazon_seller.reconcile_marketplace_evidence",
        pack_id="connector.amazon_seller",
        capability="connector.amazon_seller_marketplace_evidence",
        display_name="交叉核对 Amazon Orders、FBA Inventory 与 Finances 最小化证据",
        handler=reconcile_amazon_seller_marketplace_evidence,
        deterministic=True,
        action_class="read",
        entity_scope="statutory",
        review_gate="amazon_seller_order_finance_completeness_review",
    ))
    registry.register(ServiceDefinition(
        service_id="marketplace.reconcile_fees",
        pack_id="channel.marketplace_commerce",
        capability="channel.marketplace_fee_reconciliation",
        display_name="核对第三方电商平台费用与打款",
        handler=reconcile_marketplace_fees,
        deterministic=True,
        action_class="read",
        entity_scope="management",
        review_gate="marketplace_contract_mapping",
    ))
    registry.register(ServiceDefinition(
        service_id="marketplace.reconcile_inventory",
        pack_id="channel.marketplace_commerce",
        capability="channel.marketplace_inventory_reconciliation",
        display_name="核对平台库存与账面库存",
        handler=reconcile_marketplace_inventory,
        deterministic=True,
        action_class="read",
        entity_scope="management",
        review_gate="marketplace_inventory_mapping",
    ))
    registry.register(ServiceDefinition(
        service_id="marketplace.reconcile_receivable",
        pack_id="channel.marketplace_commerce",
        capability="channel.marketplace_receivable",
        display_name="核对第三方平台订单资金与应收",
        handler=reconcile_marketplace_receivable,
        deterministic=True,
        action_class="read",
        entity_scope="management",
        review_gate="marketplace_contract_mapping",
    ))
    registry.register(ServiceDefinition(
        service_id="game.analyze_kpis",
        pack_id="industry.game_studio",
        capability="game.user_acquisition_finance",
        display_name="计算游戏经营与投放财务指标",
        handler=analyze_game_kpis,
        deterministic=True,
        action_class="read",
        entity_scope="management",
    ))
    registry.register(ServiceDefinition(
        service_id="game.reconcile_channel_settlements",
        pack_id="industry.game_studio",
        capability="game.channel_settlement",
        display_name="按合同公式核对游戏渠道结算与应收",
        handler=reconcile_game_channel_settlements,
        deterministic=True,
        action_class="read",
        entity_scope="management",
        review_gate="game_principal_agent_assessment",
    ))
    registry.register(ServiceDefinition(
        service_id="game.project_profitability",
        pack_id="industry.game_studio",
        capability="game.project_profitability",
        display_name="按主体、项目与币种计算游戏项目直接贡献",
        handler=calculate_game_project_profitability,
        deterministic=True,
        action_class="read",
        entity_scope="management",
    ))
    registry.register(ServiceDefinition(
        service_id="game.ltv_roi_review",
        pack_id="industry.game_studio",
        capability="game.ltv_roi_review",
        display_name="生成 cohort LTV / ROI 财务预算门控建议",
        handler=review_game_ltv_roi,
        deterministic=True,
        action_class="read",
        entity_scope="management",
        review_gate="user_acquisition_budget_change",
    ))
    for service_id, pack_id, capability, display_name in (
        ("app_store.reconcile_fees", "channel.app_store", "channel.app_store_fee_reconciliation", "核对 App Store 结算费率与金额"),
        ("app_store.receivable", "channel.app_store", "channel.app_store_receivable", "核对 App Store 应收净额"),
        ("google_play.reconcile_fees", "channel.google_play", "channel.google_play_fee_reconciliation", "核对 Google Play 结算费率与金额"),
        ("google_play.receivable", "channel.google_play", "channel.google_play_receivable", "核对 Google Play 应收净额"),
        ("domestic_game.reconcile_share", "channel.domestic_game_platforms", "channel.domestic_game_share_reconciliation", "核对国内游戏渠道分成"),
        ("domestic_game.receivable", "channel.domestic_game_platforms", "channel.domestic_game_receivable", "核对国内游戏渠道应收"),
    ):
        registry.register(ServiceDefinition(
            service_id=service_id,
            pack_id=pack_id,
            capability=capability,
            display_name=display_name,
            handler=reconcile_game_channel_settlements,
            deterministic=True,
            action_class="read",
            entity_scope="management",
            review_gate="channel_contract_mapping",
        ))
    registry.register(ServiceDefinition(
        service_id="game.draft_revenue_policy",
        pack_id="industry.game_studio",
        capability="game.revenue_recognition",
        display_name="草拟游戏收入确认政策",
        handler=draft_game_revenue_policy,
        deterministic=True,
        action_class="draft",
        entity_scope="statutory",
        review_gate="game_principal_agent_assessment",
    ))
    registry.register(ServiceDefinition(
        service_id="game.calculate_revenue_recognition",
        pack_id="industry.game_studio",
        capability="game.revenue_recognition",
        display_name="按已批准政策计算游戏收入确认",
        handler=calculate_game_revenue_recognition,
        deterministic=True,
        action_class="draft",
        entity_scope="statutory",
        review_gate="accounting_policy_decision",
    ))
    registry.register(ServiceDefinition(
        service_id="tax.cn.build_calendar",
        pack_id="jurisdiction.cn_mainland",
        capability="tax.cn.filing_calendar",
        display_name="生成中国大陆主体属地税务配置任务",
        handler=_build_tax_calendar,
        deterministic=True,
        action_class="draft",
        entity_scope="statutory",
        review_gate="tax_workpaper_approval",
    ))
    registry.register(ServiceDefinition(
        service_id="tax.cn.vat_workpaper",
        pack_id="jurisdiction.cn_mainland",
        capability="tax.cn.vat_workpaper",
        display_name="生成中国大陆增值税及附加候选工作底稿",
        handler=build_cn_vat_workpaper,
        deterministic=True,
        action_class="draft",
        entity_scope="statutory",
        review_gate="tax_workpaper_approval",
    ))
    registry.register(ServiceDefinition(
        service_id="tax.cn.cit_prepaid_workpaper",
        pack_id="jurisdiction.cn_mainland",
        capability="tax.cn.cit_prepaid_workpaper",
        display_name="生成中国大陆企业所得税预缴候选工作底稿",
        handler=build_cn_cit_prepaid_workpaper,
        deterministic=True,
        action_class="draft",
        entity_scope="statutory",
        review_gate="tax_workpaper_approval",
    ))
    registry.register(ServiceDefinition(
        service_id="tax.cn.stamp_tax_workpaper",
        pack_id="jurisdiction.cn_mainland",
        capability="tax.cn.stamp_tax_source_workpaper",
        display_name="生成中国大陆印花税税源候选底稿",
        handler=build_cn_stamp_tax_workpaper,
        deterministic=True,
        action_class="draft",
        entity_scope="statutory",
        review_gate="tax_workpaper_approval",
    ))
    registry.register(ServiceDefinition(
        service_id="tax.cn.iit_withholding_workpaper",
        pack_id="jurisdiction.cn_mainland",
        capability="tax.cn.iit_withholding_workpaper",
        display_name="生成中国大陆个税扣缴候选底稿",
        handler=build_cn_iit_withholding_workpaper,
        deterministic=True,
        action_class="draft",
        entity_scope="statutory",
        review_gate="tax_workpaper_approval",
    ))
    registry.register(ServiceDefinition(
        service_id="tax.sg.build_calendar",
        pack_id="jurisdiction.sg",
        capability="tax.sg.review_calendar_skeleton",
        display_name="生成新加坡主体税务候选日历",
        handler=_build_tax_calendar,
        deterministic=True,
        action_class="draft",
        entity_scope="statutory",
        review_gate="tax_advisor_review",
    ))
    registry.register(ServiceDefinition(
        service_id="tax.sg.registration_profile",
        pack_id="jurisdiction.sg",
        capability="tax.sg.registration_profile",
        display_name="整理新加坡主体 CIT / GST 登记事实",
        handler=build_sg_registration_profile,
        deterministic=True,
        action_class="read",
        entity_scope="statutory",
        review_gate="tax_registration_confirmation",
    ))
    registry.register(ServiceDefinition(
        service_id="tax.sg.evidence_checklist",
        pack_id="jurisdiction.sg",
        capability="tax.sg.evidence_checklist",
        display_name="生成新加坡税务规则证据清单",
        handler=build_sg_evidence_checklist,
        deterministic=True,
        action_class="read",
        entity_scope="statutory",
        review_gate="tax_advisor_review",
    ))
    registry.register(ServiceDefinition(
        service_id="tax.us_federal.build_calendar",
        pack_id="jurisdiction.us_federal",
        capability="tax.us_federal.review_calendar_skeleton",
        display_name="生成美国联邦 C corporation 候选日历配置任务",
        handler=build_us_federal_calendar,
        deterministic=True,
        action_class="draft",
        entity_scope="statutory",
        review_gate="tax_advisor_review",
    ))
    registry.register(ServiceDefinition(
        service_id="tax.us_federal.registration_profile",
        pack_id="jurisdiction.us_federal",
        capability="tax.us_federal.registration_profile",
        display_name="整理美国联邦 C corporation 分类与 EIN 证据",
        handler=build_us_federal_registration_profile,
        deterministic=True,
        action_class="read",
        entity_scope="statutory",
        review_gate="tax_registration_confirmation",
    ))
    registry.register(ServiceDefinition(
        service_id="tax.us_federal.evidence_checklist",
        pack_id="jurisdiction.us_federal",
        capability="tax.us_federal.evidence_checklist",
        display_name="生成美国联邦 C corporation 证据清单",
        handler=build_us_federal_evidence_checklist,
        deterministic=True,
        action_class="read",
        entity_scope="statutory",
        review_gate="tax_advisor_review",
    ))
    registry.register(ServiceDefinition(
        service_id="tax.hk.build_calendar",
        pack_id="jurisdiction.hk",
        capability="tax.hk.review_calendar_skeleton",
        display_name="生成香港法团 BIR51 与预缴利得税候选配置任务",
        handler=build_hk_tax_calendar,
        deterministic=True,
        action_class="draft",
        entity_scope="statutory",
        review_gate="tax_advisor_review",
    ))
    registry.register(ServiceDefinition(
        service_id="tax.hk.registration_profile",
        pack_id="jurisdiction.hk",
        capability="tax.hk.registration_profile",
        display_name="整理香港法团 BRN / UBI 与利得税登记证据",
        handler=build_hk_registration_profile,
        deterministic=True,
        action_class="read",
        entity_scope="statutory",
        review_gate="tax_registration_confirmation",
    ))
    registry.register(ServiceDefinition(
        service_id="tax.hk.evidence_checklist",
        pack_id="jurisdiction.hk",
        capability="tax.hk.evidence_checklist",
        display_name="生成香港法团利得税证据清单",
        handler=build_hk_evidence_checklist,
        deterministic=True,
        action_class="read",
        entity_scope="statutory",
        review_gate="tax_advisor_review",
    ))
    registry.register(ServiceDefinition(
        service_id="tax.uk_limited_company.build_calendar",
        pack_id="jurisdiction.uk_limited_company",
        capability="tax.uk_limited_company.review_calendar_skeleton",
        display_name="生成英国 Ltd CT600、VAT 与 Companies House 候选日历",
        handler=build_uk_tax_calendar,
        deterministic=True,
        action_class="draft",
        entity_scope="statutory",
        review_gate="tax_advisor_review",
    ))
    registry.register(ServiceDefinition(
        service_id="tax.uk_limited_company.registration_profile",
        pack_id="jurisdiction.uk_limited_company",
        capability="tax.uk_limited_company.registration_profile",
        display_name="整理英国 Ltd 主体、Corporation Tax 与 VAT 登记证据",
        handler=build_uk_registration_profile,
        deterministic=True,
        action_class="read",
        entity_scope="statutory",
        review_gate="tax_registration_confirmation",
    ))
    registry.register(ServiceDefinition(
        service_id="tax.uk_limited_company.evidence_checklist",
        pack_id="jurisdiction.uk_limited_company",
        capability="tax.uk_limited_company.evidence_checklist",
        display_name="生成英国 Ltd Corporation Tax、VAT 与年度账目证据清单",
        handler=build_uk_evidence_checklist,
        deterministic=True,
        action_class="read",
        entity_scope="statutory",
        review_gate="tax_advisor_review",
    ))
    registry.register(ServiceDefinition(
        service_id="tax.au_proprietary_company.build_calendar",
        pack_id="jurisdiction.au_proprietary_company",
        capability="tax.au_proprietary_company.review_calendar_skeleton",
        display_name="生成澳大利亚公司税、BAS 与 ASIC annual review 候选日历",
        handler=build_au_tax_calendar,
        deterministic=True,
        action_class="draft",
        entity_scope="statutory",
        review_gate="tax_advisor_review",
    ))
    registry.register(ServiceDefinition(
        service_id="tax.au_proprietary_company.registration_profile",
        pack_id="jurisdiction.au_proprietary_company",
        capability="tax.au_proprietary_company.registration_profile",
        display_name="整理澳大利亚 Pty Ltd、ABN、TFN、GST 与 PAYG 登记证据",
        handler=build_au_registration_profile,
        deterministic=True,
        action_class="read",
        entity_scope="statutory",
        review_gate="tax_registration_confirmation",
    ))
    registry.register(ServiceDefinition(
        service_id="tax.au_proprietary_company.evidence_checklist",
        pack_id="jurisdiction.au_proprietary_company",
        capability="tax.au_proprietary_company.evidence_checklist",
        display_name="生成澳大利亚 Pty Ltd 税务与 ASIC 年审证据清单",
        handler=build_au_evidence_checklist,
        deterministic=True,
        action_class="read",
        entity_scope="statutory",
        review_gate="tax_advisor_review",
    ))
    registry.register(ServiceDefinition(
        service_id="tax.ca_federal_corporation.build_calendar",
        pack_id="jurisdiction.ca_federal_corporation",
        capability="tax.ca_federal_corporation.review_calendar_skeleton",
        display_name="生成加拿大联邦公司 T2、GST/HST 与 annual return 候选日历",
        handler=build_ca_tax_calendar,
        deterministic=True,
        action_class="draft",
        entity_scope="statutory",
        review_gate="tax_advisor_review",
    ))
    registry.register(ServiceDefinition(
        service_id="tax.ca_federal_corporation.registration_profile",
        pack_id="jurisdiction.ca_federal_corporation",
        capability="tax.ca_federal_corporation.registration_profile",
        display_name="整理加拿大联邦公司、BN、T2 与 GST/HST 登记证据",
        handler=build_ca_registration_profile,
        deterministic=True,
        action_class="read",
        entity_scope="statutory",
        review_gate="tax_registration_confirmation",
    ))
    registry.register(ServiceDefinition(
        service_id="tax.ca_federal_corporation.evidence_checklist",
        pack_id="jurisdiction.ca_federal_corporation",
        capability="tax.ca_federal_corporation.evidence_checklist",
        display_name="生成加拿大联邦公司 T2、GST/HST 与 annual return 证据清单",
        handler=build_ca_evidence_checklist,
        deterministic=True,
        action_class="read",
        entity_scope="statutory",
        review_gate="tax_advisor_review",
    ))
    registry.register(ServiceDefinition(
        service_id="tax.nz_limited_company.build_calendar",
        pack_id="jurisdiction.nz_limited_company",
        capability="tax.nz_limited_company.review_calendar_skeleton",
        display_name="生成新西兰有限公司 IR4、GST 与 annual return 候选日历",
        handler=build_nz_tax_calendar,
        deterministic=True,
        action_class="draft",
        entity_scope="statutory",
        review_gate="tax_advisor_review",
    ))
    registry.register(ServiceDefinition(
        service_id="tax.nz_limited_company.registration_profile",
        pack_id="jurisdiction.nz_limited_company",
        capability="tax.nz_limited_company.registration_profile",
        display_name="整理新西兰有限公司、IRD、IR4 与 GST 登记证据",
        handler=build_nz_registration_profile,
        deterministic=True,
        action_class="read",
        entity_scope="statutory",
        review_gate="tax_registration_confirmation",
    ))
    registry.register(ServiceDefinition(
        service_id="tax.nz_limited_company.evidence_checklist",
        pack_id="jurisdiction.nz_limited_company",
        capability="tax.nz_limited_company.evidence_checklist",
        display_name="生成新西兰有限公司 IR4、GST 与 annual return 证据清单",
        handler=build_nz_evidence_checklist,
        deterministic=True,
        action_class="read",
        entity_scope="statutory",
        review_gate="tax_advisor_review",
    ))
    registry.register(ServiceDefinition(
        service_id="tax.ie_private_limited_company.build_calendar",
        pack_id="jurisdiction.ie_private_limited_company",
        capability="tax.ie_private_limited_company.review_calendar_skeleton",
        display_name="生成爱尔兰 LTD CT1、VAT 与 CRO annual return 候选日历",
        handler=build_ie_tax_calendar,
        deterministic=True,
        action_class="draft",
        entity_scope="statutory",
        review_gate="tax_advisor_review",
    ))
    registry.register(ServiceDefinition(
        service_id="tax.ie_private_limited_company.registration_profile",
        pack_id="jurisdiction.ie_private_limited_company",
        capability="tax.ie_private_limited_company.registration_profile",
        display_name="整理爱尔兰 LTD、Corporation Tax 与 VAT 登记证据",
        handler=build_ie_registration_profile,
        deterministic=True,
        action_class="read",
        entity_scope="statutory",
        review_gate="tax_registration_confirmation",
    ))
    registry.register(ServiceDefinition(
        service_id="tax.ie_private_limited_company.evidence_checklist",
        pack_id="jurisdiction.ie_private_limited_company",
        capability="tax.ie_private_limited_company.evidence_checklist",
        display_name="生成爱尔兰 LTD CT1、VAT 与 CRO 年报证据清单",
        handler=build_ie_evidence_checklist,
        deterministic=True,
        action_class="read",
        entity_scope="statutory",
        review_gate="tax_advisor_review",
    ))
    registry.register(ServiceDefinition(
        service_id="tax.nl_private_limited_company.build_calendar",
        pack_id="jurisdiction.nl_private_limited_company",
        capability="tax.nl_private_limited_company.review_calendar_skeleton",
        display_name="生成荷兰 BV VPB、VAT 与 KVK 财务报表人工日历",
        handler=build_nl_tax_calendar,
        deterministic=True,
        action_class="draft",
        entity_scope="statutory",
        review_gate="tax_advisor_review",
    ))
    registry.register(ServiceDefinition(
        service_id="tax.nl_private_limited_company.registration_profile",
        pack_id="jurisdiction.nl_private_limited_company",
        capability="tax.nl_private_limited_company.registration_profile",
        display_name="整理荷兰 BV、KVK、VPB 与 VAT 登记证据",
        handler=build_nl_registration_profile,
        deterministic=True,
        action_class="read",
        entity_scope="statutory",
        review_gate="tax_registration_confirmation",
    ))
    registry.register(ServiceDefinition(
        service_id="tax.nl_private_limited_company.evidence_checklist",
        pack_id="jurisdiction.nl_private_limited_company",
        capability="tax.nl_private_limited_company.evidence_checklist",
        display_name="生成荷兰 BV VPB、VAT 与 KVK 财务报表证据清单",
        handler=build_nl_evidence_checklist,
        deterministic=True,
        action_class="read",
        entity_scope="statutory",
        review_gate="tax_advisor_review",
    ))
    registry.register(ServiceDefinition(
        service_id="tax.de_limited_liability_company.build_calendar",
        pack_id="jurisdiction.de_limited_liability_company",
        capability="tax.de_limited_liability_company.review_calendar_skeleton",
        display_name="生成德国 GmbH 企业所得税、营业税、VAT 与财务报表人工日历",
        handler=build_de_tax_calendar,
        deterministic=True,
        action_class="draft",
        entity_scope="statutory",
        review_gate="tax_advisor_review",
    ))
    registry.register(ServiceDefinition(
        service_id="tax.de_limited_liability_company.registration_profile",
        pack_id="jurisdiction.de_limited_liability_company",
        capability="tax.de_limited_liability_company.registration_profile",
        display_name="整理德国 GmbH、商业登记、企业所得税、营业税与 VAT 登记证据",
        handler=build_de_registration_profile,
        deterministic=True,
        action_class="read",
        entity_scope="statutory",
        review_gate="tax_registration_confirmation",
    ))
    registry.register(ServiceDefinition(
        service_id="tax.de_limited_liability_company.evidence_checklist",
        pack_id="jurisdiction.de_limited_liability_company",
        capability="tax.de_limited_liability_company.evidence_checklist",
        display_name="生成德国 GmbH 税务与年度财务报表证据清单",
        handler=build_de_evidence_checklist,
        deterministic=True,
        action_class="read",
        entity_scope="statutory",
        review_gate="tax_advisor_review",
    ))
    registry.register(ServiceDefinition(
        service_id="tax.fr_single_member_simplified_joint_stock_company.build_calendar",
        pack_id="jurisdiction.fr_single_member_simplified_joint_stock_company",
        capability="tax.fr_single_member_simplified_joint_stock_company.review_calendar_skeleton",
        display_name="生成法国 SASU 利润税、IS 付款、VAT 与年度账目人工日历",
        handler=build_fr_tax_calendar,
        deterministic=True,
        action_class="draft",
        entity_scope="statutory",
        review_gate="tax_advisor_review",
    ))
    registry.register(ServiceDefinition(
        service_id="tax.fr_single_member_simplified_joint_stock_company.registration_profile",
        pack_id="jurisdiction.fr_single_member_simplified_joint_stock_company",
        capability="tax.fr_single_member_simplified_joint_stock_company.registration_profile",
        display_name="整理法国 SASU、RNE、利润税制与 VAT 登记证据",
        handler=build_fr_registration_profile,
        deterministic=True,
        action_class="read",
        entity_scope="statutory",
        review_gate="tax_registration_confirmation",
    ))
    registry.register(ServiceDefinition(
        service_id="tax.fr_single_member_simplified_joint_stock_company.evidence_checklist",
        pack_id="jurisdiction.fr_single_member_simplified_joint_stock_company",
        capability="tax.fr_single_member_simplified_joint_stock_company.evidence_checklist",
        display_name="生成法国 SASU 税务与年度账目证据清单",
        handler=build_fr_evidence_checklist,
        deterministic=True,
        action_class="read",
        entity_scope="statutory",
        review_gate="tax_advisor_review",
    ))
    registry.register(ServiceDefinition(
        service_id="tax.jp_domestic_corporation.build_calendar",
        pack_id="jurisdiction.jp_domestic_corporation",
        capability="tax.jp_domestic_corporation.review_calendar_skeleton",
        display_name="生成日本株式会社 / 合同会社法人税、地方税、消费税与源泉税人工日历",
        handler=build_jp_tax_calendar,
        deterministic=True,
        action_class="draft",
        entity_scope="statutory",
        review_gate="tax_advisor_review",
    ))
    registry.register(ServiceDefinition(
        service_id="tax.jp_domestic_corporation.registration_profile",
        pack_id="jurisdiction.jp_domestic_corporation",
        capability="tax.jp_domestic_corporation.registration_profile",
        display_name="整理日本株式会社 / 合同会社、法人税、地方税与消费税登记证据",
        handler=build_jp_registration_profile,
        deterministic=True,
        action_class="read",
        entity_scope="statutory",
        review_gate="tax_registration_confirmation",
    ))
    registry.register(ServiceDefinition(
        service_id="tax.jp_domestic_corporation.evidence_checklist",
        pack_id="jurisdiction.jp_domestic_corporation",
        capability="tax.jp_domestic_corporation.evidence_checklist",
        display_name="生成日本株式会社 / 合同会社税务与申报证据清单",
        handler=build_jp_evidence_checklist,
        deterministic=True,
        action_class="read",
        entity_scope="statutory",
        review_gate="tax_advisor_review",
    ))
    registry.register(ServiceDefinition(
        service_id="tax.kr_domestic_corporation.build_calendar",
        pack_id="jurisdiction.kr_domestic_corporation",
        capability="tax.kr_domestic_corporation.review_calendar_skeleton",
        display_name="生成韩国境内营利法人税、地方所得税、VAT、电子税票与源泉税人工日历",
        handler=build_kr_tax_calendar,
        deterministic=True,
        action_class="draft",
        entity_scope="statutory",
        review_gate="tax_advisor_review",
    ))
    registry.register(ServiceDefinition(
        service_id="tax.kr_domestic_corporation.registration_profile",
        pack_id="jurisdiction.kr_domestic_corporation",
        capability="tax.kr_domestic_corporation.registration_profile",
        display_name="整理韩国境内营利法人、营业登记、法人税、地方所得税与 VAT 登记证据",
        handler=build_kr_registration_profile,
        deterministic=True,
        action_class="read",
        entity_scope="statutory",
        review_gate="tax_registration_confirmation",
    ))
    registry.register(ServiceDefinition(
        service_id="tax.kr_domestic_corporation.evidence_checklist",
        pack_id="jurisdiction.kr_domestic_corporation",
        capability="tax.kr_domestic_corporation.evidence_checklist",
        display_name="生成韩国境内营利法人税务与申报证据清单",
        handler=build_kr_evidence_checklist,
        deterministic=True,
        action_class="read",
        entity_scope="statutory",
        review_gate="tax_advisor_review",
    ))
    registry.register(ServiceDefinition(
        service_id="tax.ae_domestic_juridical_person.build_calendar",
        pack_id="jurisdiction.ae_domestic_juridical_person",
        capability="tax.ae_domestic_juridical_person.review_calendar_skeleton",
        display_name="生成阿联酋境内法人 Corporate Tax 与 VAT 人工日历",
        handler=build_ae_tax_calendar,
        deterministic=True,
        action_class="draft",
        entity_scope="statutory",
        review_gate="tax_advisor_review",
    ))
    registry.register(ServiceDefinition(
        service_id="tax.ae_domestic_juridical_person.registration_profile",
        pack_id="jurisdiction.ae_domestic_juridical_person",
        capability="tax.ae_domestic_juridical_person.registration_profile",
        display_name="整理阿联酋 mainland/free-zone 法人、Corporate Tax 与 VAT 登记证据",
        handler=build_ae_registration_profile,
        deterministic=True,
        action_class="read",
        entity_scope="statutory",
        review_gate="tax_registration_confirmation",
    ))
    registry.register(ServiceDefinition(
        service_id="tax.ae_domestic_juridical_person.evidence_checklist",
        pack_id="jurisdiction.ae_domestic_juridical_person",
        capability="tax.ae_domestic_juridical_person.evidence_checklist",
        display_name="生成阿联酋境内法人税务、自由区与会计记录证据清单",
        handler=build_ae_evidence_checklist,
        deterministic=True,
        action_class="read",
        entity_scope="statutory",
        review_gate="tax_advisor_review",
    ))
    return registry
