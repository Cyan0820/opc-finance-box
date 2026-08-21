import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const root = path.resolve(import.meta.dirname, "..");
const outputDir = path.join(root, "outputs", "demo-data");
const dataDir = path.join(root, "data");
await fs.mkdir(outputDir, { recursive: true });

const BLUE = "#2457D6";
const NAVY = "#263A5B";
const LIGHT = "#EDF3FF";
const BORDER = "#D9DEE7";
const TEXT = "#202939";
const MUTED = "#667085";

const scenarios = {
  domestic: {
    label: "国服示例",
    company: "星火互动（上海）有限公司",
    filename: "智能财务工作台-国服示例数据.xlsx",
    profile: {
      company_name: "星火互动（上海）有限公司", credit_code: "91310000MA1DEMO001",
      registered_city: "上海", base_currency: "CNY", accounting_standard: "小企业会计准则",
      fiscal_year_end: "12-31", close_target_days: 7, vat_taxpayer_type: "一般纳税人",
      vat_filing_frequency: "月度", cit_filing_frequency: "季度", cit_collection_method: "查账征收",
      micro_enterprise_candidate: "待核验", payroll_enabled: true,
      asset_policy: { material_assets_present: "否", monthly_attestation: { "2026-01": "无新增及处置", "2026-02": "无新增及处置" } },
      cross_border_business: false, external_accountant: { provider: "示例财税服务中心", contact: "王会计", email: "finance@example.test" },
      cash_planning: { opening_cash_cny: 3180000, minimum_buffer_cny: 800000, forecast_months: 12 },
      fx_policy: { source: "中国人民银行公布汇率中间价（示例）", month_end_rates: {} },
      tax_policy: { cross_border_reviews: {} },
      review_policy: { auto_post_enabled: false, high_confidence_threshold: 0.92, materiality_cny: 1000, company_head_final_close: true },
    },
    settlements: [
      ["2026-01", "长安幻想录", "微信小游戏", "微信支付", "CNY", 4620000, 46000, 4574000, 0.24, 1097760, 0, 1097760, "中国大陆"],
      ["2026-01", "长安幻想录", "iOS", "App Store 中国区", "CNY", 1860000, 31000, 1829000, 0.70, 1280300, 0, 1280300, "中国大陆"],
      ["2026-01", "像素远征", "安卓联运", "硬核联盟", "CNY", 2680000, 54000, 2626000, 0.30, 787800, 0, 787800, "中国大陆"],
      ["2026-02", "长安幻想录", "微信小游戏", "微信支付", "CNY", 5280000, 63000, 5217000, 0.24, 1252080, 0, 1252080, "中国大陆"],
      ["2026-02", "长安幻想录", "iOS", "App Store 中国区", "CNY", 2140000, 38000, 2102000, 0.70, 1471400, 0, 1471400, "中国大陆"],
      ["2026-02", "像素远征", "安卓联运", "硬核联盟", "CNY", 2460000, 48000, 2412000, 0.30, 723600, 0, 723600, "中国大陆"],
    ],
    purchases: [
      ["PO-CN-260201", "2026-02-03", "长安幻想录", "上海虹彩数字科技有限公司", "春节活动美术外包", 1, 168000, 168000, 168000, 168000, 168000, "CNY", 0.06, "已验收已开票已付款"],
      ["PO-CN-260202", "2026-02-06", "长安幻想录", "广州点量广告有限公司", "微信小游戏买量投放", 1, 320000, 320000, 320000, 320000, 280000, "CNY", 0.06, "已验收已开票部分付款"],
      ["PO-CN-260203", "2026-02-10", "像素远征", "深圳云帆科技有限公司", "云服务器及带宽", 1, 86000, 86000, 86000, 86000, 86000, "CNY", 0.06, "已验收已开票已付款"],
      ["PO-CN-260204", "2026-02-18", "像素远征", "成都声画工场", "角色配音与音效外包", 1, 92000, 92000, 92000, 0, 0, "CNY", 0.06, "已验收未开票未付款"],
    ],
    bank: [
      ["2026-02-26", "CN-P-001", "6222****8899", "上海虹彩数字科技有限公司", "3101****0001", "PO-CN-260201美术外包", "支出", "CNY", 168000, 3350000],
      ["2026-02-27", "CN-P-002", "6222****8899", "广州点量广告有限公司", "3102****0002", "PO-CN-260202投放款", "支出", "CNY", 280000, 3070000],
      ["2026-02-27", "CN-P-003", "6222****8899", "深圳云帆科技有限公司", "3103****0003", "PO-CN-260203云服务", "支出", "CNY", 86000, 2984000],
      ["2026-02-28", "CN-R-001", "6222****8899", "微信支付", "3001****1008", "长安幻想录202602结算", "收入", "CNY", 1252080, 4236080],
      ["2026-02-28", "CN-R-003", "6222****8899", "App Store 中国区", "APPLE****CN01", "长安幻想录国区iOS 202602结算", "收入", "CNY", 1471400, 5707480],
      ["2026-02-28", "CN-R-002", "6222****8899", "硬核联盟", "3002****2009", "像素远征202602结算", "收入", "CNY", 723600, 6431080],
    ],
    invoices: [
      ["260231000001", "2026-02-20", "增值税专用发票", "上海虹彩数字科技有限公司", "91310101DEMO00001", "星火互动（上海）有限公司", "春节活动美术外包", 158490.57, 0.06, 9509.43, 168000, "PO-CN-260201", "长安幻想录", "已查验", "已勾选", "未入账", "正常"],
      ["260231000002", "2026-02-24", "增值税专用发票", "广州点量广告有限公司", "91440101DEMO00002", "星火互动（上海）有限公司", "微信小游戏买量投放", 301886.79, 0.06, 18113.21, 320000, "PO-CN-260202", "长安幻想录", "已查验", "已勾选", "未入账", "正常"],
      ["260231000003", "2026-02-25", "增值税专用发票", "深圳云帆科技有限公司", "91440301DEMO00003", "星火互动（上海）有限公司", "云服务器及带宽", 81132.08, 0.06, 4867.92, 86000, "PO-CN-260203", "像素远征", "已查验", "已勾选", "未入账", "正常"],
    ],
    payroll: [
      ["E001", "成员甲", "研发", "长安幻想录", 28000, 2100, 1680, 1500, 0, 52000, 12240, 520, 23660, 0.90],
      ["E002", "成员乙", "研发", "像素远征", 24000, 1800, 1440, 1000, 0, 45000, 10050, 350, 20410, 0.85],
      ["E003", "成员丙", "发行运营", "长安幻想录", 22000, 1700, 1360, 2000, 0, 41000, 8300, 280, 18660, 0.20],
      ["E004", "成员丁", "财务行政", "公司公共", 18000, 1450, 1080, 1000, 0, 34000, 6400, 180, 15290, 0],
    ],
    opening: [
      ["2026-02", "1002", "银行存款", 3180000, 0], ["2026-02", "1122", "应收账款", 920000, 0],
      ["2026-02", "1221", "其他应收款", 80000, 0], ["2026-02", "2202", "应付账款", 0, 580000],
      ["2026-02", "2221", "应交税费", 0, 320000], ["2026-02", "3001", "实收资本", 0, 3280000],
    ],
    plans: [
      ["2026-02", "基准", "长安幻想录", "收入", "收入", 2700000, "CNY", 1, "是", "微信小游戏与国区 iOS 合并收入预算"],
      ["2026-02", "基准", "像素远征", "收入", "收入", 760000, "CNY", 1, "是", "存量版本收入预算"],
      ["2026-02", "基准", "长安幻想录", "投放", "支出", 300000, "CNY", 1, "是", "按周根据ROAS调整"],
      ["2026-02", "基准", "公司公共", "人力", "支出", 92000, "CNY", 1, "是", "固定人员成本"],
      ["2026-03", "基准", "长安幻想录", "收入", "收入", 1320000, "CNY", 0.95, "否", "新资料片上线"],
      ["2026-03", "基准", "像素远征", "收入", "收入", 700000, "CNY", 0.9, "否", "自然衰减"],
      ["2026-03", "基准", "公司公共", "人力", "支出", 94000, "CNY", 1, "是", "固定人员成本"],
      ["2026-03", "基准", "长安幻想录", "投放", "支出", 360000, "CNY", 0.9, "否", "以回收期为门槛"],
    ],
    kpis: [
      ["2026-01", "长安幻想录", "微信小游戏", "中国大陆", 82000, 410000, 118000, 24800, 152000, 4620000, 280000, 0.38, 0.17, 0.07],
      ["2026-01", "长安幻想录", "App Store 中国区", "中国大陆", 31000, 168000, 39000, 10500, 48000, 1860000, 60000, 0.36, 0.16, 0.065],
      ["2026-02", "长安幻想录", "微信小游戏", "中国大陆", 91000, 455000, 132000, 28600, 176000, 5280000, 320000, 0.40, 0.19, 0.08],
      ["2026-02", "长安幻想录", "App Store 中国区", "中国大陆", 35000, 186000, 44000, 12100, 55000, 2140000, 70000, 0.37, 0.17, 0.07],
      ["2026-01", "像素远征", "安卓联运", "中国大陆", 46000, 236000, 52000, 11200, 68000, 2680000, 120000, 0.34, 0.14, 0.05],
      ["2026-02", "像素远征", "安卓联运", "中国大陆", 42000, 218000, 43000, 9800, 57000, 2460000, 98000, 0.32, 0.12, 0.045],
    ],
  },
  overseas: {
    label: "海外示例",
    company: "远帆互动（上海）有限公司",
    filename: "智能财务工作台-海外示例数据.xlsx",
    profile: {
      company_name: "远帆互动（上海）有限公司", credit_code: "91310000MA1DEMO002",
      registered_city: "上海", base_currency: "CNY", accounting_standard: "小企业会计准则",
      fiscal_year_end: "12-31", close_target_days: 7, vat_taxpayer_type: "一般纳税人",
      vat_filing_frequency: "月度", cit_filing_frequency: "季度", cit_collection_method: "查账征收",
      micro_enterprise_candidate: "待核验", payroll_enabled: true,
      asset_policy: { material_assets_present: "否", monthly_attestation: { "2026-01": "无新增及处置", "2026-02": "无新增及处置" } },
      cross_border_business: true, external_accountant: { provider: "示例跨境财税服务中心", contact: "李会计", email: "global@example.test" },
      cash_planning: { opening_cash_cny: 4280000, minimum_buffer_cny: 1000000, forecast_months: 12 },
      fx_policy: { source: "中国人民银行公布汇率中间价（示例）", month_end_rates: { "2026-01": { USD: 7.18, HKD: 0.92 }, "2026-02": { USD: 7.21, HKD: 0.923 } } },
      tax_policy: { cross_border_reviews: {} },
      review_policy: { auto_post_enabled: false, high_confidence_threshold: 0.92, materiality_cny: 1000, company_head_final_close: true },
    },
    settlements: [
      ["2026-01", "星海远征", "iOS", "App Store", "USD", 318000, 5200, 312800, 0.70, 218960, 0, 218960, "美国"],
      ["2026-01", "星海远征", "Android", "Google Play", "USD", 176000, 3100, 172900, 0.70, 121030, 6051.5, 114978.5, "美国"],
      ["2026-01", "岛屿物语", "广告变现", "AdMob", "USD", 42000, 800, 41200, 0.82, 33784, 0, 33784, "全球"],
      ["2026-02", "星海远征", "iOS", "App Store", "USD", 352000, 6300, 345700, 0.70, 241990, 0, 241990, "美国"],
      ["2026-02", "星海远征", "Android", "Google Play", "USD", 198000, 4200, 193800, 0.70, 135660, 6783, 128877, "美国"],
      ["2026-02", "岛屿物语", "广告变现", "AdMob", "USD", 48000, 900, 47100, 0.82, 38622, 0, 38622, "全球"],
    ],
    purchases: [
      ["PO-OS-260201", "2026-02-04", "星海远征", "Northwind UA Ltd.", "海外买量投放", 1, 65000, 65000, 65000, 65000, 65000, "USD", 0, "已验收已开票已付款"],
      ["PO-OS-260202", "2026-02-08", "星海远征", "Tokyo Localize Studio", "日语本地化与LQA", 1, 18000, 18000, 18000, 18000, 0, "USD", 0, "已验收已开票未付款"],
      ["PO-OS-260203", "2026-02-12", "岛屿物语", "Cloud Harbor Inc.", "海外云服务器", 1, 12000, 12000, 12000, 12000, 12000, "USD", 0, "已验收已开票已付款"],
      ["PO-OS-260204", "2026-02-21", "岛屿物语", "Pixel Forge Vietnam", "活动素材外包", 1, 8500, 8500, 8500, 0, 0, "USD", 0, "已验收未开票未付款"],
    ],
    bank: [
      ["2026-02-28", "OS-R-001", "USD****7788", "App Store", "APPLE****001", "星海远征202602结算", "收入", "USD", 241990, 836990],
      ["2026-02-28", "OS-R-002", "USD****7788", "Google Play", "GOOG****002", "星海远征202602结算", "收入", "USD", 128877, 965867],
      ["2026-02-28", "OS-R-003", "USD****7788", "AdMob", "GOOG****003", "岛屿物语广告分成", "收入", "USD", 38622, 1004489],
      ["2026-02-25", "OS-P-001", "USD****7788", "Northwind UA Ltd.", "US****9001", "PO-OS-260201买量款", "支出", "USD", 65000, 605000],
      ["2026-02-26", "OS-P-002", "USD****7788", "Cloud Harbor Inc.", "US****9002", "PO-OS-260203云服务", "支出", "USD", 12000, 593000],
    ],
    invoices: [],
    payroll: [
      ["G001", "成员甲", "研发", "星海远征", 32000, 2300, 1920, 1500, 0, 61000, 14500, 720, 27060, 0.90],
      ["G002", "成员乙", "海外发行", "星海远征", 28000, 2100, 1680, 2000, 0, 54000, 12000, 560, 23660, 0.25],
      ["G003", "成员丙", "研发", "岛屿物语", 23000, 1750, 1380, 1000, 0, 43000, 9700, 320, 19550, 0.85],
      ["G004", "成员丁", "财务行政", "公司公共", 19000, 1500, 1140, 1000, 0, 36000, 6800, 210, 16150, 0],
    ],
    opening: [
      ["2026-02", "1002", "银行存款", 4280000, 0], ["2026-02", "1122", "应收账款", 1860000, 0],
      ["2026-02", "1221", "其他应收款", 160000, 0], ["2026-02", "2202", "应付账款", 0, 980000],
      ["2026-02", "2221", "应交税费", 0, 420000], ["2026-02", "3001", "实收资本", 0, 4900000],
    ],
    plans: [
      ["2026-02", "基准", "星海远征", "收入", "收入", 2700000, "CNY", 1, "是", "按月末汇率折算后的经营预算"],
      ["2026-02", "基准", "岛屿物语", "收入", "收入", 300000, "CNY", 1, "是", "广告变现预算"],
      ["2026-02", "基准", "星海远征", "投放", "支出", 470000, "CNY", 1, "是", "海外买量预算"],
      ["2026-02", "基准", "公司公共", "人力", "支出", 102000, "CNY", 1, "是", "境内团队工资"],
      ["2026-03", "基准", "星海远征", "收入", "收入", 2920000, "CNY", 0.9, "否", "北美活动版本"],
      ["2026-03", "基准", "岛屿物语", "收入", "收入", 335000, "CNY", 0.9, "否", "广告填充率改善"],
      ["2026-03", "基准", "星海远征", "投放", "支出", 520000, "CNY", 0.85, "否", "按D30回收期动态调整"],
      ["2026-03", "基准", "公司公共", "人力", "支出", 104000, "CNY", 1, "是", "境内团队工资"],
    ],
    kpis: [
      ["2026-01", "星海远征", "App Store + Google Play", "北美", 54000, 310000, 98000, 17600, 128000, 494000, 58000, 0.35, 0.15, 0.06],
      ["2026-02", "星海远征", "App Store + Google Play", "北美", 62000, 352000, 116000, 20500, 149000, 550000, 65000, 0.37, 0.16, 0.065],
      ["2026-01", "岛屿物语", "AdMob", "全球", 180000, 780000, 210000, 8200, 245000, 42000, 8000, 0.31, 0.12, 0.04],
      ["2026-02", "岛屿物语", "AdMob", "全球", 195000, 830000, 228000, 9100, 266000, 48000, 9000, 0.32, 0.13, 0.045],
    ],
  },
};

const settlementHeaders = ["账期月份", "游戏名称", "平台", "渠道", "结算币种", "总流水", "退款流水", "分成基数", "分成比例", "结算金额", "预扣税", "甲方实收金额（结算币种）", "国家/地区"];
const purchaseHeaders = ["PO编号", "下单日期", "项目", "供应商名称", "采购内容", "数量", "含税单价", "订单金额", "验收金额", "开票金额", "付款金额", "币种", "税率", "备注"];
const bankHeaders = ["交易日期", "交易流水号", "本方账号", "对方户名", "对方账号", "摘要", "收支方向", "币种", "交易金额", "余额"];
const invoiceHeaders = ["发票号码", "开票日期", "发票类型", "销售方名称", "销售方纳税人识别号", "购买方名称", "项目名称", "不含税金额", "税率", "税额", "价税合计", "PO编号", "项目", "查验状态", "用途确认", "入账状态", "发票状态"];
const payrollHeaders = ["工号", "姓名", "部门", "项目", "应发工资", "个人社保", "个人公积金", "专项附加扣除", "其他扣除", "累计收入", "累计扣除", "累计已预扣税额", "实发工资", "研发工时占比"];
const openingHeaders = ["期间", "科目编码", "科目名称", "期初借方", "期初贷方"];
const planHeaders = ["月份", "情景", "项目", "类别", "收支方向", "金额", "币种", "概率", "已承诺", "备注"];
const kpiHeaders = ["月份", "游戏项目编码", "渠道", "区域", "DAU", "MAU", "新增用户", "付费用户", "安装数", "游戏流水", "投放金额", "D1留存", "D7留存", "D30留存"];

function applyTable(sheet, headers, rows, formats = {}) {
  const width = headers.length;
  sheet.showGridLines = false;
  sheet.getRangeByIndexes(0, 0, 1, width).values = [[...headers]];
  sheet.getRangeByIndexes(0, 0, 1, width).format = { fill: NAVY, font: { bold: true, color: "#FFFFFF" }, rowHeight: 26, verticalAlignment: "center" };
  if (rows.length) sheet.getRangeByIndexes(1, 0, rows.length, width).values = rows;
  const used = sheet.getRangeByIndexes(0, 0, Math.max(rows.length + 1, 2), width);
  used.format.font = { name: "Arial", size: 10, color: TEXT };
  sheet.getRangeByIndexes(0, 0, 1, width).format.font = { name: "Arial", size: 10, bold: true, color: "#FFFFFF" };
  used.format.borders = { insideHorizontal: { style: "thin", color: BORDER }, bottom: { style: "thin", color: BORDER } };
  used.format.autofitColumns();
  for (let col = 0; col < width; col += 1) {
    const range = sheet.getRangeByIndexes(0, col, Math.max(rows.length + 1, 2), 1);
    if ((range.format.columnWidthPx || 0) > 180) range.format.columnWidthPx = 180;
    if ((range.format.columnWidthPx || 0) < 72) range.format.columnWidthPx = 72;
  }
  for (const [col, numberFormat] of Object.entries(formats)) {
    const column = sheet.getRangeByIndexes(0, Number(col), Math.max(rows.length + 1, 2), 1);
    if (rows.length) sheet.getRangeByIndexes(1, Number(col), rows.length, 1).format.numberFormat = numberFormat;
    column.format.columnWidthPx = headers[Number(col)].length > 10 ? 176 : 112;
  }
  sheet.freezePanes.freezeRows(1);
  sheet.getRangeByIndexes(0, 0, Math.max(rows.length + 1, 2), width).format.wrapText = false;
}

function buildReadme(workbook, scenario) {
  const sheet = workbook.worksheets.add("使用说明");
  sheet.showGridLines = false;
  sheet.getRange("A1:H1").merge();
  sheet.getRange("A1").values = [[`智能财务工作台｜${scenario.label}`]];
  sheet.getRange("A1:H1").format = { fill: NAVY, font: { bold: true, color: "#FFFFFF", size: 16 }, rowHeight: 36, verticalAlignment: "center" };
  sheet.getRange("A3:B10").values = [
    ["公司", scenario.company], ["场景", scenario.label], ["数据期间", "2026-01 至 2026-02"],
    ["用途", "产品演示、导入测试和财务流程体验"], ["真实性", "全部为虚构示例，不对应真实公司或个人"],
    ["导入顺序", "结算对账 → 采购 → 银行 → 发票 → 工资 → 期初 → 预算/KPI"],
    ["币种提示", scenario.label === "海外示例" ? "USD 原币核对，经营分析按公司档案中的月末汇率折算 CNY" : "全部以 CNY 核对"],
    ["使用方式", "网页右上角选择对应示例；也可分别导入本工作簿中的各台账"],
  ];
  sheet.getRange("A3:A10").format = { fill: LIGHT, font: { bold: true, color: BLUE } };
  sheet.getRange("A3:B10").format.borders = { insideHorizontal: { style: "thin", color: BORDER }, outside: { style: "thin", color: BORDER } };
  sheet.getRange("A3:B10").format.rowHeight = 28;
  sheet.getRange("A3:B10").format.verticalAlignment = "center";
  sheet.getRange("B3:B10").format.wrapText = true;
  sheet.getRange("A:A").format.columnWidthPx = 110;
  sheet.getRange("B:B").format.columnWidthPx = 560;
  sheet.getRange("A12:H12").merge();
  sheet.getRange("A12").values = [["提示：示例用于展示系统能力，税务结论、申报数据与凭证仍应由有权限人员根据真实资料复核。"]];
  sheet.getRange("A12:H12").format = { fill: "#FFF4DF", font: { color: "#8A4B08" }, rowHeight: 32, verticalAlignment: "center", wrapText: true };
}

async function buildWorkbook(key, scenario) {
  const wb = Workbook.create();
  buildReadme(wb, scenario);
  const config = wb.worksheets.add("公司档案");
  const profileRows = [
    ["公司名称", scenario.profile.company_name], ["统一社会信用代码", scenario.profile.credit_code],
    ["注册地", scenario.profile.registered_city], ["会计准则", scenario.profile.accounting_standard],
    ["增值税纳税人类型", scenario.profile.vat_taxpayer_type], ["增值税申报周期", scenario.profile.vat_filing_frequency],
    ["期初现金", scenario.profile.cash_planning.opening_cash_cny], ["最低现金缓冲", scenario.profile.cash_planning.minimum_buffer_cny],
    ["外部会计机构", scenario.profile.external_accountant.provider], ["月末USD/CNY", scenario.profile.fx_policy.month_end_rates["2026-02"]?.USD || "不适用"],
  ];
  applyTable(config, ["配置项", "示例值"], profileRows, { 1: "#,##0.00" });
  const sheets = [
    [key === "domestic" ? "对外账单-国服" : "商店金流账单-海外", settlementHeaders, scenario.settlements, { 5: "#,##0.00", 6: "#,##0.00", 7: "#,##0.00", 8: "0.00%", 9: "#,##0.00", 10: "#,##0.00", 11: "#,##0.00" }],
    ["采购台账", purchaseHeaders, scenario.purchases, { 5: "#,##0.00", 6: "#,##0.00", 7: "#,##0.00", 8: "#,##0.00", 9: "#,##0.00", 10: "#,##0.00", 12: "0.00%" }],
    ["银行流水", bankHeaders, scenario.bank, { 8: "#,##0.00", 9: "#,##0.00" }],
    ["发票台账", invoiceHeaders, scenario.invoices, { 7: "#,##0.00", 8: "0.00%", 9: "#,##0.00", 10: "#,##0.00" }],
    ["工资表", payrollHeaders, scenario.payroll, { 4: "#,##0.00", 5: "#,##0.00", 6: "#,##0.00", 7: "#,##0.00", 8: "#,##0.00", 9: "#,##0.00", 10: "#,##0.00", 11: "#,##0.00", 12: "#,##0.00", 13: "0.00%" }],
    ["期初余额", openingHeaders, scenario.opening, { 3: "#,##0.00", 4: "#,##0.00" }],
    ["预算预测", planHeaders, scenario.plans, { 5: "#,##0.00", 7: "0.00%" }],
    ["经营KPI", kpiHeaders, scenario.kpis, { 4: "#,##0", 5: "#,##0", 6: "#,##0", 7: "#,##0", 8: "#,##0", 9: "#,##0.00", 10: "#,##0.00", 11: "0.0%", 12: "0.0%", 13: "0.0%" }],
  ];
  for (const [name, headers, rows, formats] of sheets) applyTable(wb.worksheets.add(name), headers, rows, formats);
  const out = await SpreadsheetFile.exportXlsx(wb);
  const target = path.join(outputDir, scenario.filename);
  await out.save(target);
  const preview = await wb.render({ sheetName: "使用说明", range: "A1:H12", scale: 1.5, format: "png" });
  await fs.writeFile(path.join(outputDir, `${key}-preview.png`), new Uint8Array(await preview.arrayBuffer()));
  const settlementPreview = await wb.render({ sheetName: sheets[0][0], range: `A1:M${scenario.settlements.length + 1}`, scale: 1.2, format: "png" });
  await fs.writeFile(path.join(outputDir, `${key}-settlements-preview.png`), new Uint8Array(await settlementPreview.arrayBuffer()));
  const inspect = await wb.inspect({ kind: "table", range: `${sheets[0][0]}!A1:M8`, include: "values,formulas", tableMaxRows: 8, tableMaxCols: 13 });
  const errors = await wb.inspect({ kind: "match", searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A", options: { useRegex: true, maxResults: 100 }, summary: "final formula error scan" });
  return { target, inspect: inspect.ndjson, errors: errors.ndjson };
}

const result = {};
for (const [key, scenario] of Object.entries(scenarios)) result[key] = await buildWorkbook(key, scenario);
await fs.writeFile(path.join(dataDir, "demo_scenarios.json"), JSON.stringify(scenarios, null, 2), "utf8");
console.log(JSON.stringify(result, null, 2));
