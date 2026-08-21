import json
import re
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HAN = re.compile(r"[\u3400-\u9fff]")


class _VisibleTextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self._skip_depth = 0
        self.items = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "code", "pre", "textarea"}:
            self._skip_depth += 1
        attributes = dict(attrs)
        names = ["aria-label", "placeholder", "title"]
        if "data-i18n-value" in attributes:
            names.append("value")
        for name in names:
            value = attributes.get(name, "")
            if value and HAN.search(value):
                self.items.append(value)

    def handle_endtag(self, tag):
        if tag in {"script", "style", "code", "pre", "textarea"}:
            self._skip_depth = max(0, self._skip_depth - 1)

    def handle_data(self, data):
        value = " ".join(data.split())
        if not self._skip_depth and value and HAN.search(value):
            self.items.append(value)


def _translate_in_node(values):
    runner = r"""
const fs = require('fs');
const vm = require('vm');
global.window = global;
global.localStorage = {getItem(){return null}, setItem(){}};
global.navigator = {language: 'zh-CN'};
global.document = {readyState: 'loading', addEventListener(){}};
global.Node = {ELEMENT_NODE: 1, TEXT_NODE: 3};
global.NodeFilter = {SHOW_ELEMENT: 1, SHOW_TEXT: 4};
global.MutationObserver = function(){};
global.CustomEvent = function(){};
vm.runInThisContext(fs.readFileSync('public/i18n.js', 'utf8'));
const values = JSON.parse(fs.readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(values.map(value => financeI18n.t(value, 'en'))));
"""
    result = subprocess.run(
        ["node", "-e", runner],
        cwd=ROOT,
        input=json.dumps(values, ensure_ascii=False),
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


class BilingualWorkspaceUiTests(unittest.TestCase):
    def setUp(self):
        self.html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
        self.javascript = (ROOT / "public" / "i18n.js").read_text(encoding="utf-8")
        self.app_javascript = (ROOT / "public" / "app.js").read_text(
            encoding="utf-8"
        )

    def test_language_switch_is_persistent_and_available_before_app_boot(self):
        self.assertIn('id="language-switch"', self.html)
        self.assertIn('data-locale="zh-CN"', self.html)
        self.assertIn('data-locale="en"', self.html)
        i18n_asset = re.search(r'\./i18n\.js\?v=([^"\s]+)', self.html)
        app_asset = re.search(r'\./app\.js\?v=([^"\s]+)', self.html)
        self.assertIsNotNone(i18n_asset)
        self.assertIsNotNone(app_asset)
        self.assertEqual(i18n_asset.group(1), app_asset.group(1))
        self.assertLess(self.html.index(i18n_asset.group(0)), self.html.index(app_asset.group(0)))
        self.assertIn("localStorage.setItem(STORAGE_KEY, locale)", self.javascript)
        self.assertIn("MutationObserver", self.javascript)

    def test_static_interface_has_complete_english_rendering(self):
        parser = _VisibleTextParser()
        parser.feed(self.html)
        translated = _translate_in_node(parser.items)
        residual = sorted({value for value in translated if HAN.search(value) and value != "中文"})
        self.assertEqual(residual, [], f"untranslated static UI text: {residual[:20]}")

    def test_translation_preserves_business_names_and_uses_finance_workflow_term(self):
        source = ["财务任务流运行与复核", "长安幻想录", "星海远征", "供应商收款账户"]
        translated = _translate_in_node(source)
        self.assertEqual(translated[0], "Finance Workflow Runs and Reviews")
        self.assertEqual(translated[1], "长安幻想录")
        self.assertEqual(translated[2], "星海远征")
        self.assertEqual(translated[3], "Vendor Bank Account")

    def test_monthly_runbook_projection_has_complete_dynamic_english_rendering(self):
        source = [
            "月度工作区已验证",
            "尚未挂载月度工作区",
            "只统计操作者报告进度",
            "不等于真实证据或权威完成",
            "2 / 9 步报告完成",
            "1 个报告阻塞",
            "下一步：reconcile",
            "权威完成：否",
            "尚无下一期间工作区；完成首期观察复核和事务性归档后再生成。",
            "2026-09 · 当前安全任务",
            "完成期间准入底稿 · cn_dtc_company",
            "负责角色",
            "试运行财务准备人",
            "完成前所需证据类别",
            "当前期间主体映射",
            "职责分离：不得与 数据访问复核人 重合。",
            "3 项已报告完成",
            "本任务产出",
            "已完成的准入底稿",
            "操作检查清单",
            "核对当前工作台与期间",
            "核对法律主体范围",
            "确认数据连接保持有界只读",
            "出现以下情况必须暂停",
            "工作台或期间不匹配",
            "法律主体范围不匹配",
            "可能暴露凭证或私有路径",
            "网页只提供角色、证据类别和方法指导；不返回命令、路径、人员或证据内容，也不提供执行和签认按钮。操作者报告完成后仍须权威验证器重验。",
        ]
        translated = _translate_in_node(source)
        residual = [value for value in translated if HAN.search(value)]
        self.assertEqual(residual, [], f"untranslated monthly projection text: {residual}")
        self.assertEqual(translated[4], "2 / 9 steps reported complete")
        self.assertEqual(translated[5], "1 reported blockers")
        self.assertEqual(translated[7], "Authoritative Completion: No")

    def test_monthly_method_catalog_has_complete_dynamic_english_rendering(self):
        labels = []
        for name in (
            "PILOT_PERIOD_WORK_PRODUCT_LABELS",
            "PILOT_PERIOD_CHECK_LABELS",
            "PILOT_PERIOD_STOP_LABELS",
        ):
            match = re.search(
                rf"const {name}=\{{(.*?)\}};", self.app_javascript,
                flags=re.DOTALL,
            )
            self.assertIsNotNone(match, name)
            labels.extend(re.findall(r":'([^']+)'", match.group(1)))
        translated = _translate_in_node(labels)
        residual = [value for value in translated if HAN.search(value)]
        self.assertEqual(
            residual, [],
            f"untranslated monthly method labels: {residual}",
        )

    def test_business_control_overlay_has_complete_dynamic_english_rendering(self):
        labels = []
        for name in (
            "CFO_BUSINESS_MODEL_LABELS",
            "CFO_CONTROL_OBJECTIVE_LABELS",
            "CFO_SOURCE_BOUNDARY_LABELS",
            "CFO_FOUNDER_QUESTION_LABELS",
            "CFO_METRIC_SCOPE_LABELS",
            "CFO_METRIC_LABELS",
            "CFO_METRIC_VALUE_LABELS",
            "CFO_METRIC_OPERATOR_LABELS",
            "CFO_METRIC_DOMAIN_LABELS",
        ):
            match = re.search(
                rf"const {name}=\{{(.*?)\}};", self.app_javascript,
                flags=re.DOTALL,
            )
            self.assertIsNotNone(match, name)
            labels.extend(re.findall(r":'([^']+)'", match.group(1)))
        translated = _translate_in_node(labels)
        residual = [value for value in translated if HAN.search(value)]
        self.assertEqual(
            residual, [],
            f"untranslated business control labels: {residual}",
        )

    def test_trial_onboarding_has_complete_dynamic_english_rendering(self):
        source = [
            "本地演示可运行",
            "试用验证未通过",
            "当前阶段",
            "本地演示",
            "上线控制",
            "2 项阻塞 · 40 项必需",
            "优先阻塞",
            "生产就绪",
            "演示可运行不等于真实数据或申报就绪",
            "当前可执行",
            "可以开始",
            "等待真实证据",
            "前置门禁未通过",
            "运行本地演示工作台",
            "建立自己的可编辑工作台",
            "确认主体、税务、数据源和控制人",
            "初始化首客私有激活工作区",
            "真实只读并行验证与连续月结",
            "不可变工作台与可变运行数据已经分别复验，可在本机演示。",
            "沿用已选行业、纳税地区和集成，但新建正式候选，不改写试用快照。",
            "上线清单尚无任何权威完成证据；演示可运行不等于真实业务就绪。",
            "先完成可编辑工作台与初始配置复核，再把真实证据放入独立私有目录。",
            "数据连接器、资料交接、逐主体观察和连续期间证据必须依次通过权威验证。",
        ]
        translated = _translate_in_node(source)
        residual = [value for value in translated if HAN.search(value)]
        self.assertEqual(
            residual, [],
            f"untranslated trial onboarding text: {residual}",
        )

    def test_connector_access_renewal_projection_has_complete_dynamic_english_rendering(self):
        source = [
            "即将到期",
            "需要续期",
            "显式授权新探测并保留旧回执后续期 · 7 天内到期",
            "1 个即将到期 · 当前仍可受限读取",
            "2 个未就绪 · 1 个已需续期 · 1 个即将到期",
            "当前 Pack + 主体权限回执均有效",
            "Connector 权限",
            "1 个严重告警候选 · 2 个提醒候选",
            "稳定 alert ID 可供外部调度去重；工作台默认不发送通知",
            "未挂载 schema v5 私有工作区，或当前 Box 不需要受支持 Connector 权限回执。",
        ]
        translated = _translate_in_node(source)
        residual = [value for value in translated if HAN.search(value)]
        self.assertEqual(
            residual, [],
            f"untranslated Connector renewal projection: {residual}",
        )
        self.assertEqual(translated[0], "Renewal Due")
        self.assertIn("7 days until expiry", translated[2])
        self.assertIn("Bounded Read Access Remains Available", translated[3])

    def test_connector_provider_preflight_has_complete_dynamic_english_rendering(self):
        source = [
            "可准备版本化文件样例",
            "缺少凭证引用",
            "可初始化私有只读并行请求",
            "可初始化只读权限探测",
            "需查看专用运行手册",
            "仍有服务方阻塞",
            "已有可执行下一步",
            "当前无需服务方预检",
            "数据源能力包",
            "CSV / XLSX / PDF 文件导入",
            "Shopify Admin GraphQL 只读订单证据 Connector",
            "Stripe 只读财务证据 Connector",
            "同一服务方的多个数据集只计一次",
            "需要只读网络",
            "不发起外部连接",
            "当前阻塞",
            "优先补凭证引用或主体绑定",
            "可以继续",
            "只开放私有请求初始化或文件样例准备",
            "准备版本固定的脱敏文件样例与主体映射",
            "在服务端密钥管理中配置最小只读凭证引用",
            "初始化主体与期间绑定的私有只读并行请求",
            "初始化主体与服务方账户绑定的私有只读权限探测请求",
            "查看该服务方的专用同步与并行验证手册",
            "文件版本、法律主体、来源期间、业务键和字段映射由有权人确认",
            "只记录环境变量名、权限范围和独立批准，不复制凭证值",
            "私有请求通过专用验证器且尚未访问外部数据源",
            "专用请求边界、主体、期间和只读权限已由有权人确认",
            "2 个数据集适配器 · 1 个法律主体 · 已配置",
            "完成证据",
            "由有权人确认",
            "当前财务任务流没有需要预检的数据源能力包。",
        ]
        translated = _translate_in_node(source)
        residual = [value for value in translated if HAN.search(value)]
        self.assertEqual(
            residual, [],
            f"untranslated Connector provider preflight text: {residual}",
        )

    def test_chinese_interface_localizes_product_and_developer_terms(self):
        forbidden = [
            "Box", "Pack", "Agent", "Pipeline", "Connector", "Shadow",
            "Checkpoint", "Bundle", "provider", "readiness", "fixture",
            "quarantine", "CLI-only", "KPI", "LTV", "Core", "Provider",
            "Backfill", "cron", "stream", "cohort", "executable",
        ]
        parser = _VisibleTextParser()
        parser.feed(self.html)
        static_text = "\n".join(parser.items)
        for term in forbidden:
            self.assertNotIn(term, static_text)

        runner = r"""
const fs = require('fs');
const vm = require('vm');
global.window = global;
global.localStorage = {getItem(){return null}, setItem(){}};
global.navigator = {language: 'zh-CN'};
global.document = {readyState: 'loading', addEventListener(){}};
global.Node = {ELEMENT_NODE: 1, TEXT_NODE: 3};
global.NodeFilter = {SHOW_ELEMENT: 1, SHOW_TEXT: 4};
global.MutationObserver = function(){};
global.CustomEvent = function(){};
vm.runInThisContext(fs.readFileSync('public/i18n.js', 'utf8'));
const source = 'OPC Finance Box · Finance Core · Tax Pack · Agent · Pipeline · Connector · Shadow Close · Checkpoint · Provider readiness · fixture · quarantine · Backfill · cron · stream · cohort · executable · KPI · LTV';
process.stdout.write(financeI18n.t(source, 'zh-CN'));
"""
        rendered = subprocess.run(
            ["node", "-e", runner], cwd=ROOT, text=True,
            capture_output=True, check=True,
        ).stdout
        for term in forbidden:
            self.assertNotIn(term, rendered)
        self.assertIn("智能财务工作台", rendered)
        self.assertIn("税务能力包", rendered)
        self.assertIn("数据连接器", rendered)


if __name__ == "__main__":
    unittest.main()
