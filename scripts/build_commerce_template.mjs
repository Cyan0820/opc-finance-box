import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";


const outputPath = process.argv[2] || path.resolve(
  "outputs/019ffa00-b25b-7c20-b8ee-ff4ccc25a009/OPC-Finance-Box-Commerce导入模板.xlsx",
);
const previewDir = path.join(path.dirname(outputPath), ".commerce-template-preview");
await fs.mkdir(path.dirname(outputPath), { recursive: true });
await fs.mkdir(previewDir, { recursive: true });

const workbook = Workbook.create();
const guide = workbook.worksheets.add("使用说明");
const orders = workbook.worksheets.add("订单明细");
const settlements = workbook.worksheets.add("渠道结算");
const returns = workbook.worksheets.add("退货授权与退款");
const returnReceipts = workbook.worksheets.add("退货入库");
const importCosts = workbook.worksheets.add("进口成本与关税");
const fields = workbook.worksheets.add("字段说明");
const checks = workbook.worksheets.add("检查");

const navy = "#172033";
const cobalt = "#315EFB";
const paleBlue = "#EAF0FF";
const paleGray = "#F4F5F7";
const line = "#D8DCE5";
const amber = "#FFF2CC";
const green = "#E2F0D9";
const red = "#FCE8E6";
const inputBlue = "#0000FF";
const bodyFont = "#1D2433";

function titleBand(sheet, endColumn, title, subtitle) {
  sheet.mergeCells(`A1:${endColumn}1`);
  sheet.getRange("A1").values = [[title]];
  sheet.getRange(`A1:${endColumn}1`).format = {
    fill: navy,
    font: { bold: true, color: "#FFFFFF" },
    verticalAlignment: "center",
  };
  sheet.getRange("A1").format.rowHeight = 32;
  sheet.mergeCells(`A2:${endColumn}2`);
  sheet.getRange("A2").values = [[subtitle]];
  sheet.getRange(`A2:${endColumn}2`).format = {
    fill: paleBlue,
    font: { color: bodyFont },
    wrapText: true,
    verticalAlignment: "center",
  };
  sheet.getRange("A2").format.rowHeight = 38;
  sheet.showGridLines = false;
}

function styleHeader(range) {
  range.format = {
    fill: cobalt,
    font: { bold: true, color: "#FFFFFF" },
    horizontalAlignment: "center",
    verticalAlignment: "center",
    wrapText: true,
    borders: { preset: "all", style: "thin", color: line },
  };
  range.format.rowHeight = 34;
}

function setWidths(sheet, widths) {
  for (const [column, width] of Object.entries(widths)) {
    sheet.getRange(`${column}:${column}`).format.columnWidth = width;
  }
}

titleBand(guide, "H", "OPC Finance Box · Commerce / DTC 导入模板", "版本 0.3 · 用于订单、渠道结算、退货入库、进口成本证据和贡献利润的标准化导入；蓝色文字区域可填写，公式与检查不要覆盖。 ");
guide.getRange("A4:B10").values = [
  ["步骤", "操作"],
  [1, "在 Box 配置中确认法律主体 ID、渠道和本位币。"],
  [2, "将订单逐行填入“订单明细”；同一订单 ID 在同一主体内必须唯一。"],
  [3, "将渠道结算填入“渠道结算”；如有退货或跨境进口，分别填写对应可选页。"],
  [4, "查看“检查”：必填字段和数据数量满足要求后才显示 PASS。"],
  [5, "上传本工作簿。Agent 会按主体、期间、渠道、币种分组核对，不会跨币种直接相加。"],
  [6, "目的地国家和已收税额只作为税务判断证据，不代表系统已确认登记义务或应纳税额。"],
];
styleHeader(guide.getRange("A4:B4"));
guide.getRange("A5:A10").format = { fill: paleGray, horizontalAlignment: "center" };
guide.getRange("B5:B10").format = { wrapText: true, verticalAlignment: "center" };
guide.getRange("A12:B16").values = [
  ["颜色", "含义"],
  ["蓝色文字", "用户输入"],
  ["黑色文字", "公式、检查和系统计算"],
  ["黄色背景", "仍需补充或人工确认"],
  ["绿色背景", "检查通过"],
];
styleHeader(guide.getRange("A12:B12"));
guide.getRange("A13:A13").format.font = { color: inputBlue };
guide.getRange("A15:B15").format.fill = amber;
guide.getRange("A16:B16").format.fill = green;
setWidths(guide, { A: 16, B: 76, C: 3, D: 12, E: 12, F: 12, G: 12, H: 12 });
guide.getRange("A4:B16").format.borders = { preset: "outside", style: "thin", color: line };

const orderHeaders = [
  "订单ID", "法律主体ID", "期间", "渠道", "目的地国家", "币种", "商品原价不含税",
  "折扣不含税", "运费收入不含税", "已收税额", "退款不含税", "退回税额",
  "商品成本", "履约成本", "物流成本",
];
titleBand(orders, "O", "订单明细", "一行代表一个订单在一个主体、期间、渠道和币种下的财务事实。期间使用 YYYY-MM；国家使用 ISO 两位代码；金额使用原币数值。最多预留 200 行，可自行扩展。 ");
orders.getRange("A4:O4").values = [orderHeaders];
styleHeader(orders.getRange("A4:O4"));
orders.getRange("A5:O204").format = {
  font: { color: inputBlue },
  borders: { preset: "inside", style: "thin", color: "#ECEEF2" },
  verticalAlignment: "center",
};
orders.getRange("G5:O204").format.numberFormat = "#,##0.00;[Red](#,##0.00);-";
orders.getRange("C5:C204").format.numberFormat = "@";
orders.getRange("A5:F204").format.numberFormat = "@";
orders.getRange("F5:F204").dataValidation = { rule: { type: "list", values: ["CNY", "USD", "EUR", "GBP", "SGD", "HKD", "JPY", "AUD", "CAD"] } };
orders.freezePanes.freezeRows(4);
setWidths(orders, { A: 18, B: 20, C: 12, D: 18, E: 14, F: 10, G: 18, H: 16, I: 18, J: 14, K: 14, L: 14, M: 14, N: 14, O: 14 });

const settlementHeaders = [
  "结算ID", "法律主体ID", "期间", "渠道", "币种", "渠道报告订单净流入",
  "渠道及支付费用", "渠道代扣代缴税额", "其他调整", "实际打款",
];
titleBand(settlements, "J", "渠道结算", "一行代表一个渠道或支付平台的结算批次。其他调整可正可负；实际打款应与银行到账继续核对。最多预留 200 行。 ");
settlements.getRange("A4:J4").values = [settlementHeaders];
styleHeader(settlements.getRange("A4:J4"));
settlements.getRange("A5:J204").format = {
  font: { color: inputBlue },
  borders: { preset: "inside", style: "thin", color: "#ECEEF2" },
  verticalAlignment: "center",
};
settlements.getRange("F5:J204").format.numberFormat = "#,##0.00;[Red](#,##0.00);-";
settlements.getRange("A5:E204").format.numberFormat = "@";
settlements.getRange("E5:E204").dataValidation = { rule: { type: "list", values: ["CNY", "USD", "EUR", "GBP", "SGD", "HKD", "JPY", "AUD", "CAD"] } };
settlements.freezePanes.freezeRows(4);
setWidths(settlements, { A: 22, B: 20, C: 12, D: 18, E: 10, F: 22, G: 20, H: 22, I: 16, J: 16 });

const returnHeaders = [
  "退货单号", "订单号", "法律主体ID", "期间", "渠道", "商品SKU", "币种",
  "授权退货数量", "已退款数量", "退款金额不含税", "退回税额",
];
titleBand(returns, "K", "退货授权与退款", "一行代表一个退货授权中的一个 SKU。退货单号 + SKU 在主体内唯一；授权、已退款数量和退款金额分别保留。没有退货可留空。 ");
returns.getRange("A4:K4").values = [returnHeaders];
styleHeader(returns.getRange("A4:K4"));
returns.getRange("A5:K204").format = {
  font: { color: inputBlue },
  borders: { preset: "inside", style: "thin", color: "#ECEEF2" },
  verticalAlignment: "center",
};
returns.getRange("H5:K204").format.numberFormat = "#,##0.00;[Red](#,##0.00);-";
returns.getRange("A5:G204").format.numberFormat = "@";
returns.getRange("G5:G204").dataValidation = { rule: { type: "list", values: ["CNY", "USD", "EUR", "GBP", "SGD", "HKD", "JPY", "AUD", "CAD"] } };
returns.freezePanes.freezeRows(4);
setWidths(returns, { A: 20, B: 18, C: 20, D: 12, E: 18, F: 18, G: 10, H: 18, I: 16, J: 18, K: 14 });

const returnReceiptHeaders = [
  "退货入库单号", "退货单号", "法律主体ID", "期间", "商品SKU", "仓库",
  "实收数量", "处置状态",
];
titleBand(returnReceipts, "H", "退货入库", "一行代表一次仓库实收事件。处置状态只允许 restockable、damaged、inspection_pending；系统只生成补库存候选，不自动改库存。 ");
returnReceipts.getRange("A4:H4").values = [returnReceiptHeaders];
styleHeader(returnReceipts.getRange("A4:H4"));
returnReceipts.getRange("A5:H204").format = {
  font: { color: inputBlue },
  borders: { preset: "inside", style: "thin", color: "#ECEEF2" },
  verticalAlignment: "center",
};
returnReceipts.getRange("A5:F204").format.numberFormat = "@";
returnReceipts.getRange("G5:G204").format.numberFormat = "#,##0.00;[Red](#,##0.00);-";
returnReceipts.getRange("H5:H204").dataValidation = { rule: { type: "list", values: ["restockable", "damaged", "inspection_pending"] } };
returnReceipts.freezePanes.freezeRows(4);
setWidths(returnReceipts, { A: 22, B: 20, C: 20, D: 12, E: 18, F: 20, G: 14, H: 22 });

const importCostHeaders = [
  "进口明细行ID", "进口批次号", "法律主体ID", "期间", "商品SKU", "仓库",
  "原产国", "目的地国家", "币种", "进口数量", "申报货值", "进口运费",
  "保险费", "关税金额", "进口税", "报关服务费",
];
titleBand(importCosts, "P", "进口成本与关税", "一行代表一个进口批次中的一个 SKU/仓库成本事实。系统只生成 landed-cost 候选；进口税默认排除，且不判断商品归类、税率、可抵扣性或会计入账。没有跨境进口可留空。 ");
importCosts.getRange("A4:P4").values = [importCostHeaders];
styleHeader(importCosts.getRange("A4:P4"));
importCosts.getRange("A5:P204").format = {
  font: { color: inputBlue },
  borders: { preset: "inside", style: "thin", color: "#ECEEF2" },
  verticalAlignment: "center",
};
importCosts.getRange("A5:I204").format.numberFormat = "@";
importCosts.getRange("J5:P204").format.numberFormat = "#,##0.00;[Red](#,##0.00);-";
importCosts.getRange("I5:I204").dataValidation = { rule: { type: "list", values: ["CNY", "USD", "EUR", "GBP", "SGD", "HKD", "JPY", "AUD", "CAD"] } };
importCosts.freezePanes.freezeRows(4);
setWidths(importCosts, { A: 22, B: 20, C: 20, D: 12, E: 18, F: 18, G: 12, H: 14, I: 10, J: 14, K: 14, L: 14, M: 12, N: 14, O: 12, P: 16 });

const fieldRows = [
  ["对象", "标准字段", "模板列", "必填", "定义", "财务用途"],
  ["订单", "order_id", "订单ID", "是", "销售渠道内稳定订单标识；主体内不可重复", "幂等导入与退款追踪"],
  ["订单", "entity_id", "法律主体ID", "是", "Box 配置中的法律主体 ID", "法定账、税务与银行范围"],
  ["订单", "period", "期间", "是", "YYYY-MM，不使用支付到账月替代订单归属月", "收入截止与月度分析"],
  ["订单", "destination_country", "目的地国家", "是", "ISO 两位国家代码；应来自收货/消费事实", "间接税登记和申报判断证据"],
  ["订单", "merchandise_gross_ex_tax", "商品原价不含税", "是", "折扣与退款前、不含税商品销售额", "订单到收入桥接"],
  ["订单", "tax_collected", "已收税额", "否", "订单向客户收取的税额；未知应留空或填0并记录缺口", "税务证据，不直接等于应纳税额"],
  ["订单", "cogs", "商品成本", "否", "已售商品对应成本，口径需与库存政策一致", "商品毛利"],
  ["订单", "fulfillment_cost", "履约成本", "否", "仓储、拣货、包装等订单履约成本", "贡献利润"],
  ["结算", "reported_order_inflow", "渠道报告订单净流入", "是", "渠道报告的客户收款减退款，费用扣除前", "订单与渠道结算核对"],
  ["结算", "channel_and_payment_fees", "渠道及支付费用", "否", "支付手续费、平台费等结算扣费", "费用核对与贡献利润"],
  ["结算", "tax_withheld_or_remitted", "渠道代扣代缴税额", "否", "渠道从结算中扣除或代缴的税额", "结算桥与税务证据"],
  ["结算", "other_adjustments", "其他调整", "否", "返利、准备金释放等有证据的正负调整", "结算差异解释"],
  ["结算", "payout", "实际打款", "是", "渠道结算文件中的净打款额", "后续与银行到账核对"],
  ["退货", "return_id", "退货单号", "是", "渠道或退货系统中的稳定退货授权标识", "与主体和 SKU 共同形成唯一业务键"],
  ["退货", "sku", "商品SKU", "是", "退回商品的稳定库存编码", "与退货单号共同连接授权、退款与仓库实收"],
  ["退货", "authorized_quantity", "授权退货数量", "是", "客户获准退回的正数数量", "识别未完成授权与超收"],
  ["退货", "refunded_quantity", "已退款数量", "否", "已经实际退款的数量，不由金额反推", "识别退款未入库或入库未退款"],
  ["退货入库", "warehouse + disposition", "仓库 + 处置状态", "是", "实际收货仓库及 restockable/damaged/inspection_pending", "多仓处置与补库存候选"],
  ["退货入库", "received_quantity", "实收数量", "是", "一次真实收货事件的正数数量", "物理实收核对；不自动调库存"],
  ["进口成本", "entry_line_id + import_entry_id", "进口明细行ID + 进口批次号", "是", "报关或物流证据中的稳定明细行与进口批次标识", "幂等导入与批次追踪"],
  ["进口成本", "sku + warehouse", "商品SKU + 仓库", "是", "成本归集对应的商品和实际入库仓库", "按主体、期间、币种、SKU、仓库生成候选"],
  ["进口成本", "origin_country + destination_country", "原产国 + 目的地国家", "是", "ISO 两位国家代码，仅作为申报事实", "保留跨境路径证据；不决定商品归类或税率"],
  ["进口成本", "quantity", "进口数量", "是", "本明细行对应的正数进口数量", "计算单位 landed-cost 候选"],
  ["进口成本", "declared_value", "申报货值", "是", "报关证据中的原币申报价值", "库存成本候选基数"],
  ["进口成本", "inbound_freight + insurance", "进口运费 + 保险费", "否", "可追溯到进口批次的入境前运保费用", "库存 landed-cost 候选组成"],
  ["进口成本", "customs_duty + brokerage", "关税金额 + 报关服务费", "否", "已发生并有证据的关税与报关服务费", "库存 landed-cost 候选组成；仍需政策复核"],
  ["进口成本", "import_tax", "进口税", "否", "进口环节税额，默认不并入库存成本候选", "单独披露；系统不判断可抵扣性或入账"],
];
titleBand(fields, "F", "字段说明", "字段定义是导入契约的一部分；换 Shopify、Amazon 或自建站连接器时，应映射到这些标准字段，而不是重写财务计算。 ");
fields.getRange(`A4:F${3 + fieldRows.length}`).values = fieldRows;
styleHeader(fields.getRange("A4:F4"));
fields.getRange(`A5:F${3 + fieldRows.length}`).format = { wrapText: true, verticalAlignment: "top" };
fields.getRange(`A4:F${3 + fieldRows.length}`).format.borders = { preset: "inside", style: "thin", color: line };
fields.freezePanes.freezeRows(4);
setWidths(fields, { A: 10, B: 28, C: 24, D: 10, E: 54, F: 42 });

titleBand(checks, "F", "导入检查", "检查公式只验证模板完整性；上传后系统还会执行主体存在性、唯一键、金额、订单—结算、退货—实收、进口成本证据和打款方程检查。空模板显示 FAIL 是正常状态。 ");
checks.getRange("A4:F15").values = [
  ["检查", "实际", "期望", "差异", "状态", "修复位置"],
  ["订单行数", null, "> 0", null, null, "订单明细!A5:O204"],
  ["结算行数", null, "> 0", null, null, "渠道结算!A5:J204"],
  ["订单缺少主体", null, 0, null, null, "订单明细!B5:B204"],
  ["订单缺少期间", null, 0, null, null, "订单明细!C5:C204"],
  ["订单缺少币种", null, 0, null, null, "订单明细!F5:F204"],
  ["结算缺少主体", null, 0, null, null, "渠道结算!B5:B204"],
  ["结算缺少实际打款", null, 0, null, null, "渠道结算!J5:J204"],
  ["退货缺少主体", null, 0, null, null, "退货授权与退款!C5:C204"],
  ["退货入库缺少退货单号", null, 0, null, null, "退货入库!B5:B204"],
  ["进口成本缺少主体", null, 0, null, null, "进口成本与关税!C5:C204"],
  ["进口成本缺少批次号", null, 0, null, null, "进口成本与关税!B5:B204"],
];
styleHeader(checks.getRange("A4:F4"));
checks.getRange("B5").formulas = [["=COUNTA('订单明细'!$A$5:$A$204)"]];
checks.getRange("B6").formulas = [["=COUNTA('渠道结算'!$A$5:$A$204)"]];
checks.getRange("B7").formulas = [["=COUNTIFS('订单明细'!$A$5:$A$204,\"<>\",'订单明细'!$B$5:$B$204,\"\")"]];
checks.getRange("B8").formulas = [["=COUNTIFS('订单明细'!$A$5:$A$204,\"<>\",'订单明细'!$C$5:$C$204,\"\")"]];
checks.getRange("B9").formulas = [["=COUNTIFS('订单明细'!$A$5:$A$204,\"<>\",'订单明细'!$F$5:$F$204,\"\")"]];
checks.getRange("B10").formulas = [["=COUNTIFS('渠道结算'!$A$5:$A$204,\"<>\",'渠道结算'!$B$5:$B$204,\"\")"]];
checks.getRange("B11").formulas = [["=COUNTIFS('渠道结算'!$A$5:$A$204,\"<>\",'渠道结算'!$J$5:$J$204,\"\")"]];
checks.getRange("B12").formulas = [["=COUNTIFS('退货授权与退款'!$A$5:$A$204,\"<>\",'退货授权与退款'!$C$5:$C$204,\"\")"]];
checks.getRange("B13").formulas = [["=COUNTIFS('退货入库'!$A$5:$A$204,\"<>\",'退货入库'!$B$5:$B$204,\"\")"]];
checks.getRange("B14").formulas = [["=COUNTIFS('进口成本与关税'!$A$5:$A$204,\"<>\",'进口成本与关税'!$C$5:$C$204,\"\")"]];
checks.getRange("B15").formulas = [["=COUNTIFS('进口成本与关税'!$A$5:$A$204,\"<>\",'进口成本与关税'!$B$5:$B$204,\"\")"]];
checks.getRange("D5").formulas = [["=IF(B5>0,0,1)"]];
checks.getRange("D6").formulas = [["=IF(B6>0,0,1)"]];
checks.getRange("D7").formulas = [["=B7-C7"]];
checks.getRange("D7:D15").fillDown();
checks.getRange("E5").formulas = [["=IF(D5=0,\"OK\",\"FAIL\")"]];
checks.getRange("E5:E15").fillDown();
checks.mergeCells("A17:B17");
checks.getRange("A17").values = [["MODEL STATUS"]];
checks.mergeCells("C17:E17");
checks.getRange("C17").formulas = [["=IF(COUNTIF(E5:E15,\"FAIL\")=0,\"PASS\",\"FAIL\")"]];
checks.getRange("A17:E17").format = { fill: paleGray, font: { bold: true }, verticalAlignment: "center" };
checks.getRange("C17:E17").format.horizontalAlignment = "center";
checks.getRange("E5:E15").conditionalFormats.add("containsText", { text: "OK", format: { fill: green, font: { color: "#166534", bold: true } } });
checks.getRange("E5:E15").conditionalFormats.add("containsText", { text: "FAIL", format: { fill: red, font: { color: "#B42318", bold: true } } });
checks.getRange("C17:E17").conditionalFormats.add("containsText", { text: "PASS", format: { fill: green, font: { color: "#166534", bold: true } } });
checks.getRange("C17:E17").conditionalFormats.add("containsText", { text: "FAIL", format: { fill: red, font: { color: "#B42318", bold: true } } });
checks.getRange("A4:F15").format.borders = { preset: "inside", style: "thin", color: line };
checks.freezePanes.freezeRows(4);
setWidths(checks, { A: 24, B: 14, C: 14, D: 14, E: 14, F: 34 });

const keyInspect = await workbook.inspect({
  kind: "table",
  range: "检查!A4:F17",
  include: "values,formulas",
  tableMaxRows: 17,
  tableMaxCols: 8,
  maxChars: 5000,
});
const formulaErrors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 100 },
  summary: "commerce template formula error scan",
  maxChars: 3000,
});

for (const [sheetName, range] of [
  ["使用说明", "A1:H16"],
  ["订单明细", "A1:O14"],
  ["渠道结算", "A1:J14"],
  ["退货授权与退款", "A1:K14"],
  ["退货入库", "A1:H14"],
  ["进口成本与关税", "A1:P14"],
  ["字段说明", "A1:F33"],
  ["检查", "A1:F17"],
]) {
  const rendered = await workbook.render({ sheetName, range, scale: 1, format: "png" });
  await fs.writeFile(path.join(previewDir, `${sheetName}.png`), new Uint8Array(await rendered.arrayBuffer()));
}

const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);
process.stdout.write(JSON.stringify({
  outputPath,
  keyInspect: keyInspect.ndjson,
  formulaErrors: formulaErrors.ndjson,
  previewDir,
}, null, 2));
