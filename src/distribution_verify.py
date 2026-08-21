from __future__ import annotations

from email.parser import Parser
from pathlib import Path, PurePosixPath
import zipfile


MAX_WHEEL_BYTES = 100 * 1024 * 1024
REQUIRED_MEMBERS = (
    "src/box_pipeline.py",
    "src/pipeline_run_store.py",
    "src/pipeline_scheduler.py",
    "src/pipeline_observability.py",
    "src/deployment_smoke.py",
    "src/deployment_assets.py",
    "src/runtime_storage.py",
    "src/connector_sync.py",
    "src/release_promotion.py",
    "src/shadow_close_artifacts.py",
    "src/connector_shadow_artifacts.py",
    "src/connector_shadow_registry.py",
    "src/connector_access_probe.py",
    "src/connector_access_registry.py",
    "src/activation_orchestrator.py",
    "src/activation_workspace.py",
    "src/activation_runbook.py",
    "src/handoff_verify.py",
    "src/handoff_receipt.py",
    "src/handoff_unpack.py",
    "src/source_kit.py",
    "src/source_kit_unpack.py",
    "src/starter_workspace.py",
    "src/trial_workspace.py",
    "src/multi_entity_shadow_close.py",
    "src/api_auth.py",
    "src/marketplace_services.py",
    "src/shipbob_services.py",
    "src/paypal_services.py",
    "src/woocommerce_services.py",
    "src/amazon_seller_services.py",
    "src/uk_tax_services.py",
    "src/tax_workspace.py",
    "src/au_tax_services.py",
    "src/ca_tax_services.py",
    "src/nz_tax_services.py",
    "src/ie_tax_services.py",
    "src/nl_tax_services.py",
    "src/de_tax_services.py",
    "src/fr_tax_services.py",
    "src/jp_tax_services.py",
    "src/kr_tax_services.py",
    "src/ae_tax_services.py",
    "src/tax_pack_lifecycle.py",
    "src/tax_applicability_artifacts.py",
    "src/month_close_control.py",
    "src/month_close_portfolio_evidence.py",
    "src/first_close_discovery.py",
    "src/pilot_readiness.py",
    "src/pilot_data_handoff.py",
    "src/pilot_shadow_run.py",
    "src/pilot_shadow_observation.py",
    "src/pilot_shadow_series.py",
    "src/pilot_shadow_next_period.py",
    "src/pilot_shadow_period_runbook.py",
    "src/pilot_shadow_period_index.py",
    "src/pilot_shadow_period_tasks.py",
    "src/cfo_control_overlay.py",
    "src/cfo_metric_catalog.py",
    "src/cfo_metric_evaluator.py",
    "src/cfo_metric_assembly.py",
    "src/production_readiness.py",
    "src/release_candidate_audit.py",
    "share/opc-finance-box/box/stable-promotion-evidence.schema.json",
    "share/opc-finance-box/box/box-pipeline-request.schema.json",
    "share/opc-finance-box/box/tax-applicability-artifact.schema.json",
    "share/opc-finance-box/box/tax-applicability-registry-receipt.schema.json",
    "share/opc-finance-box/box/pilot-readiness-artifact.schema.json",
    "share/opc-finance-box/box/pilot-data-handoff-artifact.schema.json",
    "share/opc-finance-box/box/pilot-shadow-run-registration.schema.json",
    "share/opc-finance-box/box/pilot-shadow-observation-artifact.schema.json",
    "share/opc-finance-box/box/pilot-shadow-series-artifact.schema.json",
    "share/opc-finance-box/box/cfo-metric-evaluation-request.schema.json",
    "share/opc-finance-box/box/cfo-metric-operand-assembly.schema.json",
    "share/opc-finance-box/docs/首次Shadow观察复核.md",
    "share/opc-finance-box/docs/连续Shadow期间复核.md",
    "share/opc-finance-box/docs/连续Shadow下一期间工作区.md",
    "share/opc-finance-box/docs/连续Shadow月度Runbook.md",
    "share/opc-finance-box/docs/首客激活Runbook.md",
    "share/opc-finance-box/docs/Handoff接收与安全展开.md",
    "share/opc-finance-box/docs/可Fork源码SourceKit.md",
    "share/opc-finance-box/docs/可Fork源码安全初始化.md",
    "share/opc-finance-box/docs/五分钟本地试用.md",
    "share/opc-finance-box/docs/CFO指标确定性计算.md",
    "share/opc-finance-box/docs/CFO指标操作数自动组装.md",
    "share/opc-finance-box/examples/pipelines/game_channel_settlement_close_template.json",
    "share/opc-finance-box/examples/pipelines/commerce_channel_close_fixture.json",
    "share/opc-finance-box/examples/pipelines/marketplace_channel_close_fixture.json",
    "share/opc-finance-box/examples/commerce/dtc_returns.csv",
    "share/opc-finance-box/examples/commerce/dtc_return_receipts.csv",
    "share/opc-finance-box/examples/commerce/import_costs.csv",
    "share/opc-finance-box/examples/banking/bank_statement.csv",
    "share/opc-finance-box/examples/pipelines/bank_statement_close_fixture.json",
    "share/opc-finance-box/examples/accounting/trial_balance.csv",
    "share/opc-finance-box/examples/pipelines/trial_balance_review_fixture.json",
    "share/opc-finance-box/examples/accounting/general_ledger.csv",
    "share/opc-finance-box/examples/accounting/accounting_close_trial_balance.csv",
    "share/opc-finance-box/examples/accounting/account_mappings.json",
    "share/opc-finance-box/examples/pipelines/accounting_close_review_fixture.json",
    "share/opc-finance-box/examples/accounting/month_close_general_ledger.csv",
    "share/opc-finance-box/examples/accounting/month_close_trial_balance.csv",
    "share/opc-finance-box/examples/accounting/month_close_account_mappings.json",
    "share/opc-finance-box/examples/pipelines/month_close_control_fixture.json",
    "share/opc-finance-box/examples/pipelines/multi_entity_month_close_portfolio_fixture.json",
    "share/opc-finance-box/examples/pipelines/first_close_discovery_fixture.json",
    "share/opc-finance-box/examples/pipelines/shopify_stripe_daily_schedule_demo.json",
    "share/opc-finance-box/examples/pipelines/shopify_stripe_wise_daily_close_fixture.json",
    "share/opc-finance-box/examples/pipelines/airwallex_expense_review_fixture.json",
    "share/opc-finance-box/examples/pipelines/shipbob_fulfillment_close_fixture.json",
    "share/opc-finance-box/examples/pipelines/paypal_transaction_close_fixture.json",
    "share/opc-finance-box/examples/pipelines/woocommerce_order_refund_close_fixture.json",
    "share/opc-finance-box/examples/pipelines/amazon_seller_transaction_close_fixture.json",
    "share/opc-finance-box/examples/pipelines/amazon_seller_marketplace_close_fixture.json",
    "share/opc-finance-box/examples/service_requests/cfo_metric_evaluation_dtc.template.json",
    "share/opc-finance-box/examples/shadow/sg_shopify_stripe_wise_connector_baseline.json",
    "share/opc-finance-box/examples/shadow/sg_airwallex_expense_connector_baseline.json",
    "share/opc-finance-box/packs/industries/commerce/manifest.json",
    "share/opc-finance-box/packs/channels/marketplace_commerce/manifest.json",
    "share/opc-finance-box/packs/connectors/xero/manifest.json",
    "share/opc-finance-box/packs/connectors/xero/provider.py",
    "share/opc-finance-box/packs/connectors/xero/provider-contract.json",
    "share/opc-finance-box/packs/connectors/xero/fixture-trial-balance.json",
    "share/opc-finance-box/packs/connectors/wise/manifest.json",
    "share/opc-finance-box/packs/connectors/wise/provider.py",
    "share/opc-finance-box/packs/connectors/wise/provider-contract.json",
    "share/opc-finance-box/packs/connectors/wise/fixture-balance-statement.json",
    "share/opc-finance-box/packs/connectors/airwallex/manifest.json",
    "share/opc-finance-box/packs/connectors/airwallex/provider.py",
    "share/opc-finance-box/packs/connectors/airwallex/provider-contract.json",
    "share/opc-finance-box/packs/connectors/airwallex/fixture-approved-expenses.json",
    "share/opc-finance-box/packs/connectors/shipbob/manifest.json",
    "share/opc-finance-box/packs/connectors/shipbob/provider.py",
    "share/opc-finance-box/packs/connectors/shipbob/provider-contract.json",
    "share/opc-finance-box/packs/connectors/shipbob/fixture-fulfillment.json",
    "share/opc-finance-box/packs/connectors/paypal/manifest.json",
    "share/opc-finance-box/packs/connectors/paypal/provider.py",
    "share/opc-finance-box/packs/connectors/paypal/provider-contract.json",
    "share/opc-finance-box/packs/connectors/paypal/fixture-transactions.json",
    "share/opc-finance-box/packs/connectors/woocommerce/manifest.json",
    "share/opc-finance-box/packs/connectors/woocommerce/provider.py",
    "share/opc-finance-box/packs/connectors/woocommerce/provider-contract.json",
    "share/opc-finance-box/packs/connectors/woocommerce/fixture-order-refunds.json",
    "share/opc-finance-box/packs/connectors/amazon_seller/manifest.json",
    "share/opc-finance-box/packs/connectors/amazon_seller/provider.py",
    "share/opc-finance-box/packs/connectors/amazon_seller/provider-contract.json",
    "share/opc-finance-box/packs/connectors/amazon_seller/fixture-transactions.json",
    "share/opc-finance-box/packs/connectors/amazon_seller/fixture-marketplace-evidence.json",
    "share/opc-finance-box/packs/jurisdictions/uk_limited_company/manifest.json",
    "share/opc-finance-box/packs/jurisdictions/au_proprietary_company/manifest.json",
    "share/opc-finance-box/packs/jurisdictions/ca_federal_corporation/manifest.json",
    "share/opc-finance-box/packs/jurisdictions/nz_limited_company/manifest.json",
    "share/opc-finance-box/packs/jurisdictions/ie_private_limited_company/manifest.json",
    "share/opc-finance-box/packs/jurisdictions/nl_private_limited_company/manifest.json",
    "share/opc-finance-box/packs/jurisdictions/de_limited_liability_company/manifest.json",
    "share/opc-finance-box/packs/jurisdictions/fr_single_member_simplified_joint_stock_company/manifest.json",
    "share/opc-finance-box/packs/jurisdictions/jp_domestic_corporation/manifest.json",
    "share/opc-finance-box/packs/jurisdictions/kr_domestic_corporation/manifest.json",
    "share/opc-finance-box/packs/jurisdictions/ae_domestic_juridical_person/manifest.json",
    "share/opc-finance-box/packs/jurisdictions/au_proprietary_company/rules.json",
    "share/opc-finance-box/packs/jurisdictions/ca_federal_corporation/rules.json",
    "share/opc-finance-box/packs/jurisdictions/cn_mainland/rules.json",
    "share/opc-finance-box/packs/jurisdictions/de_limited_liability_company/rules.json",
    "share/opc-finance-box/packs/jurisdictions/fr_single_member_simplified_joint_stock_company/rules.json",
    "share/opc-finance-box/packs/jurisdictions/jp_domestic_corporation/rules.json",
    "share/opc-finance-box/packs/jurisdictions/kr_domestic_corporation/rules.json",
    "share/opc-finance-box/packs/jurisdictions/ae_domestic_juridical_person/rules.json",
    "share/opc-finance-box/packs/jurisdictions/hk/rules.json",
    "share/opc-finance-box/packs/jurisdictions/ie_private_limited_company/rules.json",
    "share/opc-finance-box/packs/jurisdictions/nl_private_limited_company/rules.json",
    "share/opc-finance-box/packs/jurisdictions/nz_limited_company/rules.json",
    "share/opc-finance-box/packs/jurisdictions/sg/rules.json",
    "share/opc-finance-box/packs/jurisdictions/uk_limited_company/rules.json",
    "share/opc-finance-box/packs/jurisdictions/us_federal/rules.json",
    "share/opc-finance-box/examples/boxes/uk_dtc_shopify_stripe_ltd.json",
    "share/opc-finance-box/examples/boxes/au_dtc_shopify_stripe_pty_ltd.json",
    "share/opc-finance-box/examples/boxes/ca_dtc_shopify_stripe_federal_corporation.json",
    "share/opc-finance-box/examples/boxes/nz_dtc_shopify_stripe_limited_company.json",
    "share/opc-finance-box/examples/boxes/ie_dtc_shopify_stripe_ltd.json",
    "share/opc-finance-box/examples/boxes/nl_dtc_shopify_stripe_bv.json",
    "share/opc-finance-box/examples/boxes/de_dtc_shopify_stripe_gmbh.json",
    "share/opc-finance-box/examples/boxes/fr_dtc_shopify_stripe_sasu.json",
    "share/opc-finance-box/examples/boxes/jp_dtc_shopify_stripe_kk.json",
    "share/opc-finance-box/examples/boxes/kr_dtc_shopify_stripe_jusik_hoesa.json",
    "share/opc-finance-box/examples/boxes/ae_dtc_shopify_stripe_free_zone_company.json",
    "share/opc-finance-box/examples/boxes/global_game_studio_xero.json",
    "share/opc-finance-box/examples/boxes/sg_dtc_wise_store.json",
    "share/opc-finance-box/examples/boxes/sg_dtc_shopify_stripe_wise_store.json",
    "share/opc-finance-box/examples/boxes/sg_dtc_shopify_stripe_wise_airwallex_store.json",
    "share/opc-finance-box/examples/boxes/us_dtc_shopify_stripe_shipbob_c_corp.json",
    "share/opc-finance-box/examples/boxes/us_dtc_paypal_c_corp.json",
    "share/opc-finance-box/examples/boxes/us_dtc_woocommerce_c_corp.json",
    "share/opc-finance-box/examples/boxes/us_marketplace_amazon_seller_c_corp.json",
    "share/opc-finance-box/docs/Pipeline运行与恢复.md",
    "share/opc-finance-box/docs/Pipeline调度与可观测性.md",
    "share/opc-finance-box/docs/Pipeline部署监控与告警.md",
    "share/opc-finance-box/docs/生产部署与Smoke验证.md",
    "share/opc-finance-box/docs/运行时数据升级与恢复.md",
    "share/opc-finance-box/docs/Connector增量同步控制.md",
    "share/opc-finance-box/docs/Stable晋级证据与签认.md",
    "share/opc-finance-box/docs/Shadow Close试运行.md",
    "share/opc-finance-box/docs/Xero只读会计Connector.md",
    "share/opc-finance-box/docs/Wise只读银行Connector.md",
    "share/opc-finance-box/docs/Connector来源Shadow验收.md",
    "share/opc-finance-box/docs/Airwallex只读费用Connector.md",
    "share/opc-finance-box/docs/ShipBob只读仓配Connector.md",
    "share/opc-finance-box/docs/PayPal只读交易Connector.md",
    "share/opc-finance-box/docs/WooCommerce只读订单退款Connector.md",
    "share/opc-finance-box/docs/AmazonSeller只读财务Connector.md",
    "share/opc-finance-box/docs/AmazonMarketplace订单库存完整性设计.md",
    "share/opc-finance-box/docs/技术RC全矩阵审计.md",
    "share/opc-finance-box/docs/首次月结ShadowRun登记.md",
    "share/opc-finance-box/docs/生产准备度总表.md",
    "share/opc-finance-box/docs/ConnectorShadow证据登记.md",
    "share/opc-finance-box/docs/Connector上线准备.md",
    "share/opc-finance-box/docs/首客激活编排器.md",
    "share/opc-finance-box/docs/首客私有工作区初始化.md",
    "share/opc-finance-box/deployment/Dockerfile",
    "share/opc-finance-box/deployment/Dockerfile.dockerignore",
    "share/opc-finance-box/deployment/compose.example.yaml",
    "share/opc-finance-box/deployment/box.env.example",
    "share/opc-finance-box/deployment/opc-finance-workbench.service",
    "share/opc-finance-box/deployment/opc-finance-scheduler.service",
    "share/opc-finance-box/deployment/opc-finance-scheduler.timer",
    "share/opc-finance-box/docs/英国有限公司税务包.md",
    "share/opc-finance-box/docs/TaxPack工作台.md",
    "share/opc-finance-box/docs/澳大利亚有限公司税务包.md",
    "share/opc-finance-box/docs/加拿大联邦公司税务包.md",
    "share/opc-finance-box/docs/新西兰有限公司税务包.md",
    "share/opc-finance-box/docs/爱尔兰有限公司税务包.md",
    "share/opc-finance-box/docs/荷兰BV税务包.md",
    "share/opc-finance-box/docs/德国GmbH税务包.md",
    "share/opc-finance-box/docs/法国SASU税务包.md",
    "share/opc-finance-box/docs/日本株式会社合同会社税务包.md",
    "share/opc-finance-box/docs/韩国境内营利法人税务包.md",
    "share/opc-finance-box/docs/阿联酋境内法人税务包.md",
    "share/opc-finance-box/docs/纳税地区Pack生命周期与适用性.md",
    "share/opc-finance-box/docs/首家真实OPC资料交接包.md",
)


class DistributionVerifyError(ValueError):
    """Raised when a wheel cannot be trusted as an installable OPC Finance Box artifact."""


def verify_wheel(path: str | Path) -> dict[str, object]:
    wheel = Path(path)
    if wheel.suffix != ".whl" or not wheel.is_file():
        raise DistributionVerifyError("distribution must be an existing .whl file")
    size = wheel.stat().st_size
    if size <= 0 or size > MAX_WHEEL_BYTES:
        raise DistributionVerifyError("wheel size is empty or exceeds 100 MiB")
    try:
        with zipfile.ZipFile(wheel) as archive:
            bad_member = archive.testzip()
            if bad_member:
                raise DistributionVerifyError(f"wheel member CRC failed: {bad_member}")
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise DistributionVerifyError("wheel contains duplicate member names")
            for name in names:
                member = PurePosixPath(name)
                if member.is_absolute() or ".." in member.parts or "\\" in name:
                    raise DistributionVerifyError(f"wheel contains unsafe member path: {name}")
            metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
            entry_names = [name for name in names if name.endswith(".dist-info/entry_points.txt")]
            if len(metadata_names) != 1 or len(entry_names) != 1:
                raise DistributionVerifyError("wheel requires exactly one METADATA and entry_points.txt")
            metadata = Parser().parsestr(archive.read(metadata_names[0]).decode("utf-8"))
            project_name = str(metadata.get("Name") or "").strip()
            version = str(metadata.get("Version") or "").strip()
            if project_name != "opc-finance-box":
                raise DistributionVerifyError(f"unexpected project name: {project_name or 'missing'}")
            if not version or version in {"0.0.0", "UNKNOWN"}:
                raise DistributionVerifyError("wheel has missing or placeholder project version")
            entries = archive.read(entry_names[0]).decode("utf-8")
            for command, target in (
                ("opc-finance-box", "src.cli:main"),
                ("opc-finance-workbench", "src.server:main"),
            ):
                if f"{command} = {target}" not in entries:
                    raise DistributionVerifyError(f"wheel is missing console entry point: {command}")
            missing = [
                required for required in REQUIRED_MEMBERS
                if not any(name.endswith(required) for name in names)
            ]
            if missing:
                raise DistributionVerifyError(
                    "wheel is missing required product members: " + ", ".join(missing)
                )
    except zipfile.BadZipFile as exc:
        raise DistributionVerifyError("distribution is not a valid wheel ZIP archive") from exc
    return {
        "valid": True,
        "path": str(wheel.resolve()),
        "project_name": project_name,
        "version": version,
        "size_bytes": size,
        "member_count": len(names),
        "required_member_count": len(REQUIRED_MEMBERS),
        "console_entry_points": ["opc-finance-box", "opc-finance-workbench"],
    }
