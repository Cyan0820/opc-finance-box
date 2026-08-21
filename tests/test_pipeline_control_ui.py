import re
import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class _IdCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []

    def handle_starttag(self, _tag, attrs):
        values = dict(attrs)
        if values.get("id"):
            self.ids.append(values["id"])


class PipelineControlUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
        cls.javascript = (ROOT / "public" / "app.js").read_text(encoding="utf-8")
        cls.styles = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")

    def test_control_plane_has_unique_navigation_and_regions(self):
        parser = _IdCollector()
        parser.feed(self.html)
        self.assertEqual(len(parser.ids), len(set(parser.ids)))
        self.assertIn('data-module="pipelines" data-view="pipeline-launch"', self.html)
        self.assertIn('data-module="builder" data-view="box-builder"', self.html)
        self.assertIn("['pipeline-launch','启动与预检'],['connector-onboarding','数据连接器准备'],['pipeline-control','运行复核']", self.javascript)
        for element_id in (
            "view-pipeline-control",
            "pipeline-integrity-banner",
            "pipeline-control-metrics",
            "pipeline-schedule-summary",
            "pipeline-schedule-list",
            "pipeline-review-queue",
            "pipeline-attempt-list",
            "pipeline-attempt-detail",
            "view-pipeline-launch",
            "pipeline-catalog-list",
            "pipeline-request-editor",
            "pipeline-preflight-button",
            "pipeline-record-button",
            "view-connector-onboarding",
            "connector-readiness-refresh",
            "connector-readiness-boundary",
            "connector-readiness-metrics",
            "connector-readiness-list",
            "activation-status",
            "activation-metrics",
            "activation-init-command",
            "activation-wave",
            "connector-shadow-status",
            "connector-shadow-metrics",
            "connector-shadow-packs",
            "pilot-readiness-status",
            "pilot-readiness-metrics",
            "pilot-readiness-alerts",
            "pilot-readiness-entities",
            "pilot-handoff-status",
            "pilot-handoff-metrics",
            "pilot-handoff-entities",
            "pilot-shadow-run-status",
            "pilot-shadow-run-metrics",
            "pilot-shadow-run-entities",
            "pilot-business-control-status",
            "pilot-business-control-overlay",
            "connector-sync-metrics",
            "connector-sync-list",
            "connector-unreferenced-count",
            "connector-unreferenced-list",
            "view-box-builder",
            "box-builder-profile",
            "box-builder-jurisdiction",
            "box-builder-preview",
            "box-builder-config",
            "box-builder-checklist",
            "box-builder-checklist-count",
            "box-builder-add-entity",
            "box-builder-additional-entities",
            "box-builder-binding-draft",
            "box-builder-bindings",
            "box-builder-cli-command",
            "box-builder-copy-cli",
            "box-builder-download-bundle",
        ):
            self.assertIn(f'id="{element_id}"', self.html)

    def test_control_plane_uses_only_safe_summary_endpoints(self):
        for endpoint in (
            "/api/box/pipeline-runs?limit=50",
            "/api/box/pipeline-review-queue?limit=100",
            "/api/box/pipeline-run-integrity",
            "/api/box/pipeline-schedule",
            "/api/auth/whoami",
            "/api/box/pipeline-run-reviews",
        ):
            self.assertIn(endpoint, self.javascript)
        self.assertIn("原始请求、完整运行结果和密钥均不在控制台运行台账中持久化", self.javascript)
        self.assertNotIn("run.full_request", self.javascript)
        self.assertNotIn("run.full_result", self.javascript)
        self.assertIn("本页不会触发运行", self.javascript)
        self.assertIn("request 不在控制面返回", self.javascript)

    def test_connector_readiness_is_secret_free_and_shadow_fail_closed(self):
        self.assertIn("/api/box/connectors/readiness", self.javascript)
        self.assertIn("renderConnectorProviderPreflight", self.javascript)
        self.assertIn('id="connector-provider-status"', self.html)
        self.assertIn('id="connector-provider-metrics"', self.html)
        self.assertIn('id="connector-provider-list"', self.html)
        self.assertIn("ready_to_initialize_private_access_probe_request", self.javascript)
        self.assertIn("可初始化只读权限探测", self.javascript)
        self.assertIn("/api/box/activation", self.javascript)
        self.assertIn("renewal_due:'即将到期'", self.javascript)
        self.assertIn("renewalDueCount", self.javascript)
        self.assertIn("scope.days_until_expiry", self.javascript)
        self.assertIn("当前仍可受限读取", self.javascript)
        self.assertIn("scope.next_cli_command", self.javascript)
        self.assertIn("accessData=data.connector_access", self.javascript)
        self.assertIn("accessAlerts=data.connector_access_alerts", self.javascript)
        self.assertIn("稳定 alert ID 可供外部调度去重", self.javascript)
        self.assertIn("默认提前 7 天提示续期", self.html)
        self.assertIn("可去重的安全告警候选", self.html)
        self.assertIn("20260816-81", self.html)
        self.assertIn("当前 Box 不需要受支持 Connector 权限回执", self.javascript)
        self.assertIn("/api/box/connector-shadow", self.javascript)
        self.assertIn("/api/box/pilot-readiness", self.javascript)
        self.assertIn("/api/box/pilot-data-handoff", self.javascript)
        self.assertIn("/api/box/pilot-shadow-run", self.javascript)
        self.assertIn("/api/box/pilot-shadow-periods", self.javascript)
        self.assertIn('id="pilot-shadow-periods-status"', self.html)
        self.assertIn('id="pilot-shadow-current-task"', self.html)
        self.assertIn('id="pilot-cfo-metric-status"', self.html)
        self.assertIn('id="pilot-cfo-metric-catalog"', self.html)
        self.assertIn("报告完成永远不等于权威证据通过", self.html)
        self.assertIn("PILOT_PERIOD_TASK_LABELS", self.javascript)
        self.assertIn("PILOT_PERIOD_ROLE_LABELS", self.javascript)
        self.assertIn("PILOT_PERIOD_WORK_PRODUCT_LABELS", self.javascript)
        self.assertIn("PILOT_PERIOD_CHECK_LABELS", self.javascript)
        self.assertIn("PILOT_PERIOD_STOP_LABELS", self.javascript)
        self.assertIn("CFO_BUSINESS_MODEL_LABELS", self.javascript)
        self.assertIn("CFO_CONTROL_OBJECTIVE_LABELS", self.javascript)
        self.assertIn("CFO_SOURCE_BOUNDARY_LABELS", self.javascript)
        self.assertIn("CFO_FOUNDER_QUESTION_LABELS", self.javascript)
        self.assertIn("CFO_METRIC_LABELS", self.javascript)
        self.assertIn("CFO_METRIC_DOMAIN_LABELS", self.javascript)
        self.assertIn("business_control_overlay", self.javascript)
        self.assertIn("business_metric_catalog", self.javascript)
        self.assertIn("missing_operand_policy", (ROOT / "src" / "cfo_metric_catalog.py").read_text(encoding="utf-8"))
        self.assertIn("required_evidence_type_ids", self.javascript)
        self.assertIn("operator_checklist_type_ids", self.javascript)
        self.assertIn("stop_condition_type_ids", self.javascript)
        self.assertIn("本任务产出", self.javascript)
        self.assertIn("操作检查清单", self.javascript)
        self.assertIn("出现以下情况必须暂停", self.javascript)
        self.assertIn("操作者报告完成后仍须权威验证器重验", self.javascript)
        self.assertIn("不提供执行和签认按钮", self.javascript)
        self.assertNotIn("current.shell_preview", self.javascript)
        self.assertNotIn("current.argv", self.javascript)
        self.assertIn(".pilot-shadow-current-task", self.styles)
        self.assertIn(".pilot-task-playbook", self.styles)
        self.assertIn(".pilot-task-stop", self.styles)
        self.assertIn(".pilot-business-control-panel", self.styles)
        self.assertIn(".pilot-business-control-grid", self.styles)
        self.assertIn(".pilot-cfo-metric-panel", self.styles)
        self.assertIn(".pilot-cfo-metric-grid", self.styles)
        self.assertIn("/api/box/connector-sync", self.javascript)
        self.assertIn("凭证存在不会改变这两个状态", self.javascript)
        self.assertIn("data-shadow-resolution-row", self.javascript)
        self.assertIn("exception_resolutions", self.javascript)
        self.assertIn("逐项差异处置", self.html)
        self.assertIn("系统缺陷（阻塞 stable）", self.javascript)
        self.assertIn("Shadow 已执行：<strong>否</strong>", self.javascript)
        self.assertIn("只显示环境变量名与是否配置", self.javascript)
        self.assertIn("这些 Connector 虽已随 Pack 安装，但不属于当前 Box 的可执行财务任务流", self.javascript)
        self.assertNotIn("credential.value", self.javascript)
        self.assertNotIn("credential_value", self.javascript)
        self.assertIn(".connector-readiness-list", self.styles)
        self.assertIn(".connector-onboarding-steps", self.styles)
        self.assertIn(".connector-sync-panel", self.styles)
        self.assertIn("执行、提交与解决仍仅限命令行操作", self.html)
        self.assertIn("不含原始请求/响应", self.html)
        for gate in (
            "paypal_entity_account_binding_review",
            "paypal_transaction_event_mapping_review",
            "paypal_fee_treatment_review", "paypal_refund_reversal_review",
            "shipbob_entity_binding_review", "shipbob_order_mapping_review",
            "shipbob_fulfillment_cost_review", "return_disposition_review",
        ):
            self.assertIn(gate, self.javascript)

    def test_integrity_and_permissions_fail_closed(self):
        self.assertIn("const reviewDisabled=!integrityOk||!canReview||state.pipelineControlBusy", self.javascript)
        self.assertIn("完整性验证未通过，不能提交复核", self.javascript)
        self.assertIn("integrity.event_count||0", self.javascript)
        self.assertIn("当前身份没有 reviewer 权限", self.javascript)
        self.assertIn("error.status===403", self.javascript)
        self.assertRegex(
            self.javascript,
            re.compile(r"roles\.includes\('reviewer'\).*roles\.includes\('admin'\)"),
        )

    def test_review_requires_rationale_and_warns_before_terminal_decisions(self):
        self.assertIn("if(!rationale)return toast('请填写复核决定依据')", self.javascript)
        self.assertIn("window.confirm", self.javascript)
        self.assertIn("evidence_references:evidence?[evidence]:[]", self.javascript)
        store = (ROOT / "src" / "pipeline_run_store.py").read_text(encoding="utf-8")
        self.assertIn('"release_candidate_is_external_authorization": False', store)

    def test_pipeline_layout_is_responsive_and_cache_versions_match(self):
        self.assertIn(".pipeline-control-grid", self.styles)
        self.assertIn(".pipeline-integrity-failed", self.styles)
        self.assertIn(".pipeline-launch-grid", self.styles)
        self.assertIn(".pipeline-schedule-list", self.styles)
        self.assertIn("@media(max-width:760px)", self.styles)
        css_version = re.search(r"styles\.css\?v=([^\"]+)", self.html).group(1)
        js_version = re.search(r"app\.js\?v=([^\"]+)", self.html).group(1)
        i18n_version = re.search(r"i18n\.js\?v=([^\"]+)", self.html).group(1)
        self.assertEqual(css_version, js_version)
        self.assertEqual(css_version, i18n_version)

    def test_non_game_workbench_is_pack_driven_and_does_not_boot_game_demo(self):
        for element_id in (
            "view-box-home",
            "box-home-name",
            "box-home-entities",
            "box-home-packs",
            "box-home-warnings",
        ):
            self.assertIn(f'id="{element_id}"', self.html)
        self.assertIn("await loadBoxContext()", self.javascript)
        self.assertIn("workbench.profile==='game_studio'", self.javascript)
        self.assertIn("MODULES.workbench.title=isGame?'财务目标组合':'Box 概览'", self.javascript)
        self.assertIn("isGame?'agent':'box-home'", self.javascript)
        self.assertIn("else{updateCompanyContext(null);if(initial==='documents')await loadDocuments()}", self.javascript)
        self.assertIn(".box-pack-grid", self.styles)

    def test_pipeline_launcher_preflights_before_operator_recording(self):
        self.assertIn("/api/box/pipelines/preflight", self.javascript)
        self.assertIn("/api/box/pipeline-runs", self.javascript)
        self.assertIn("if(!state.pipelinePreflight?.ready_to_dispatch)", self.javascript)
        self.assertIn("pipelineCanOperate()", self.javascript)
        self.assertIn("当前身份没有 operator 权限", self.javascript)
        self.assertIn("预检不会读取数据源", self.html)
        self.assertIn("凭证和密钥不能写进任务请求配置", self.html)

    def test_box_builder_is_pack_driven_and_preview_only(self):
        self.assertIn("/api/box-builder/options", self.javascript)
        self.assertIn("/api/box-builder/preview", self.javascript)
        self.assertIn("候选已生成；当前运行中的 Box 没有改变", self.javascript)
        self.assertIn("tax_registrations:[]", self.javascript)
        self.assertIn("国家选择不是报税结论", self.html)
        self.assertIn("不会替换当前运行中的工作台", self.html)
        self.assertIn(".box-builder-grid", self.styles)
        self.assertIn("copyBoxBuilder('config')", self.javascript)
        self.assertIn("setup_checklist", self.javascript)
        self.assertIn("完成证据：", self.javascript)
        self.assertIn(".box-builder-checklist-group", self.styles)
        self.assertIn("feature.multi_entity", (ROOT / "src" / "box_scaffold.py").read_text(encoding="utf-8"))
        self.assertIn("boxBuilderAdditionalEntities", self.javascript)
        self.assertIn("renderAdditionalBoxBuilderEntities", self.javascript)
        self.assertIn(".box-builder-entity-card", self.styles)
        self.assertIn("/api/box-builder/bundle", self.javascript)
        self.assertIn("URL.createObjectURL", self.javascript)
        self.assertIn("boxBuilderSha256Hex", self.javascript)
        self.assertIn("actual!==expected", self.javascript)
        self.assertLess(
            self.javascript.index("actual!==expected"),
            self.javascript.index("URL.createObjectURL(blob)"),
        )
        self.assertIn("handoff_download_policy", self.javascript)
        self.assertIn("receipt_is_digital_signature:false", self.javascript)
        self.assertIn("handoff-unpack-verify", self.javascript)
        self.assertIn("box-builder-handoff-receipt", self.html)
        self.assertIn("box-builder-copy-receipt", self.html)
        self.assertIn("box-builder-download-receipt", self.html)
        self.assertIn("box-builder-copy-handoff", self.html)
        self.assertIn("downloadBoxBuilderReceipt", self.javascript)
        self.assertIn("handoff-receipt-verify", self.javascript)
        self.assertIn("['chmod','600',archive,receipt]", self.javascript)
        self.assertIn(".box-builder-handoff-details", self.styles)
        self.assertIn("data-builder-entity-integration", self.javascript)
        self.assertIn("connector_bindings:bindingDraft.bindings", self.javascript)
        self.assertIn("single_credential_connector_packs", self.javascript)
        self.assertIn("当前每个运行凭证只能绑定一个主体", self.javascript)
        self.assertIn("--entity-integration", self.javascript)
        self.assertIn("starter-init", self.javascript)
        self.assertIn("starter-compose", self.javascript)
        self.assertIn("copyBoxBuilder('cli')", self.javascript)
        self.assertIn("完整数据连接器主体绑定", self.html)
        self.assertIn(".box-builder-binding-draft", self.styles)
        self.assertIn(".box-builder-binding-result", self.styles)
        self.assertIn(".box-builder-cli", self.styles)


if __name__ == "__main__":
    unittest.main()
