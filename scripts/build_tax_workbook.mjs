import fs from "node:fs/promises";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const [inputPath, outputPath, verifyDir] = process.argv.slice(2);
if (!inputPath || !outputPath) throw new Error("Usage: build_tax_workbook.mjs input.json output.xlsx [verifyDir]");
const data = JSON.parse(await fs.readFile(inputPath, "utf8"));
const workbook = Workbook.create();
const C = {navy:"#17342A",green:"#176B4D",mint:"#E7F2ED",pale:"#F4F7F5",amber:"#FFF3DF",red:"#FDEBEA",line:"#DDE6E1",ink:"#17211C",muted:"#67736D",white:"#FFFFFF",blue:"#0000FF"};

function cleanName(value) { return String(value).replace(/[\\/?*\[\]:]/g,"-").slice(0,31); }
function title(sheet,endCol,name,subtitle) {
  sheet.showGridLines=false; sheet.mergeCells(`A1:${endCol}1`); sheet.getRange("A1").values=[[name]];
  sheet.getRange(`A1:${endCol}1`).format={fill:C.navy,font:{color:C.white,bold:true,size:17},rowHeight:34,verticalAlignment:"center"};
  sheet.mergeCells(`A2:${endCol}2`); sheet.getRange("A2").values=[[subtitle]];
  sheet.getRange(`A2:${endCol}2`).format={fill:C.mint,font:{color:C.green,size:10},rowHeight:30,verticalAlignment:"center",wrapText:true};
}
function header(sheet,range){sheet.getRange(range).format={fill:C.green,font:{color:C.white,bold:true,size:10},rowHeight:28,wrapText:true,verticalAlignment:"center"};}
function body(sheet,range){sheet.getRange(range).format={font:{color:C.ink,size:10},borders:{insideHorizontal:{style:"thin",color:C.line}},verticalAlignment:"center",wrapText:true};}
function widths(sheet,map){for(const [col,width] of Object.entries(map))sheet.getRange(`${col}:${col}`).format.columnWidth=width;}
function lastCol(n){let s="";while(n){n--;s=String.fromCharCode(65+n%26)+s;n=Math.floor(n/26)}return s;}

const cover=workbook.worksheets.add("使用说明");
title(cover,"H","税务申报工作底稿",`由智能财务工作台根据现有业务、账务和税务档案生成。它不是已提交申报表；蓝字或黄色状态代表仍需补充或复核。账面口径：${data.accounting_basis||"未注明"}`);
cover.getRange("A4:H4").values=[["表单","表号/版本","期间","状态","传输状态","Agent判断","复核角色","官方依据"]];header(cover,"A4:H4");
const coverRows=data.returns.map(x=>[x.name,x.form_code+" / "+x.version,x.period,x.status,x.transport,x.agent_position,x.review_role,x.official_source]);
if(coverRows.length)cover.getRange(`A5:H${4+coverRows.length}`).values=coverRows;
body(cover,`A5:H${Math.max(5,4+coverRows.length)}`);cover.getRange(`D5:E${Math.max(5,4+coverRows.length)}`).format.fill=C.amber;
cover.getRange("A12:H12").merge();cover.getRange("A12").values=[["工作流："+(data.workflow||[]).join(" → ")]];cover.getRange("A12:H12").format={fill:C.pale,font:{color:C.green,bold:true},rowHeight:28};
cover.getRange("A14:H14").merge();cover.getRange("A14").values=[[data.guardrail]];cover.getRange("A14:H14").format={fill:C.amber,font:{color:"#725224"},wrapText:true,rowHeight:38};
widths(cover,{A:34,B:26,C:14,D:14,E:36,F:56,G:22,H:58});cover.freezePanes.freezeRows(4);

for(const item of data.returns){
  const sheet=workbook.worksheets.add(cleanName(item.form_code));title(sheet,"G",item.name,`${data.company_name} | ${data.credit_code||"税号待补"} | 税款所属期 ${data.period_start} 至 ${data.period_end} | ${item.version}`);
  sheet.getRange("A4:G4").values=[["栏次/字段","项目","候选填报值","数据来源","状态","Agent说明","复核结果"]];header(sheet,"A4:G4");
  const fields=(item.fields||[]).map(x=>[x.code,x.name,x.value,x.source,x.status,"候选值不等于已申报值；按阻塞项补证据后复核",""]);
  if(fields.length)sheet.getRange(`A5:G${4+fields.length}`).values=fields;
  body(sheet,`A5:G${Math.max(5,4+fields.length)}`);sheet.getRange(`C5:C${Math.max(5,4+fields.length)}`).setNumberFormat("#,##0.00;[Red](#,##0.00);-");sheet.getRange(`C5:C${Math.max(5,4+fields.length)}`).format.font={color:C.blue};
  const blockerRow=Math.max(7,6+fields.length);sheet.mergeCells(`A${blockerRow}:G${blockerRow}`);sheet.getRange(`A${blockerRow}`).values=[["当前阻塞："+((item.blockers||[]).join("；")||"无硬阻塞，待复核")]];sheet.getRange(`A${blockerRow}:G${blockerRow}`).format={fill:(item.blockers||[]).length?C.amber:C.mint,font:{color:(item.blockers||[]).length?"#725224":C.green,bold:true},wrapText:true,rowHeight:38};
  const checkHeader=blockerRow+2;sheet.getRange(`A${checkHeader}:D${checkHeader}`).values=[["勾稽检查","结果","说明","修复位置"]];header(sheet,`A${checkHeader}:D${checkHeader}`);
  const checks=(item.checks||[]).map(x=>[x.name,x.passed===true?"OK":x.passed===false?"FAIL":"WARN",x.note,"数据与配置 / 对应业务台账"]);
  if(checks.length)sheet.getRange(`A${checkHeader+1}:D${checkHeader+checks.length}`).values=checks;
  body(sheet,`A${checkHeader+1}:D${Math.max(checkHeader+1,checkHeader+checks.length)}`);sheet.getRange(`B${checkHeader+1}:B${Math.max(checkHeader+1,checkHeader+checks.length)}`).conditionalFormats.add("containsText",{text:"OK",format:{fill:"#DFF1E8",font:{color:C.green,bold:true}}});sheet.getRange(`B${checkHeader+1}:B${Math.max(checkHeader+1,checkHeader+checks.length)}`).conditionalFormats.add("containsText",{text:"FAIL",format:{fill:C.red,font:{color:"#B3453F",bold:true}}});
  widths(sheet,{A:22,B:36,C:20,D:42,E:14,F:52,G:24});sheet.freezePanes.freezeRows(4);

  const rawSchedules=item.schedules||[];
  const scheduleGroups=rawSchedules.length&&rawSchedules.every(x=>x&&Array.isArray(x.rows))
    ?rawSchedules.map((x,i)=>({name:x.name||`明细${i+1}`,rows:x.rows||[]}))
    :rawSchedules.length?[{name:"明细",rows:rawSchedules}]:[];
  for(const group of scheduleGroups){
    if(!group.rows.length)continue;
    const schedule=workbook.worksheets.add(cleanName(item.form_code+"-"+group.name));
    const keys=[...new Set(group.rows.flatMap(row=>Object.keys(row)))];
    title(schedule,lastCol(keys.length),item.name+" · "+group.name,"由现有台账生成的候选明细；隐私/合同/税目字段不足时不得直接提交。");
    schedule.getRange(`A4:${lastCol(keys.length)}4`).values=[keys];header(schedule,`A4:${lastCol(keys.length)}4`);
    schedule.getRange(`A5:${lastCol(keys.length)}${4+group.rows.length}`).values=group.rows.map(row=>keys.map(k=>row[k]??null));
    body(schedule,`A5:${lastCol(keys.length)}${4+group.rows.length}`);schedule.getRange(`A5:${lastCol(keys.length)}${4+group.rows.length}`).format.font={color:C.blue,size:10};
    for(let i=1;i<=keys.length;i++)schedule.getRange(`${lastCol(i)}:${lastCol(i)}`).format.columnWidth=keys[i-1].includes("status")?30:20;
    schedule.freezePanes.freezeRows(4);
  }
}

const checks=workbook.worksheets.add("Checks");title(checks,"G","Checks","工作簿级质量检查。FAIL/WARN 表示不能把本底稿当作可直接上传的申报文件。");checks.getRange("A4:G4").values=[["检查","实际","期望","差异/问题","容差","状态","修复位置"]];header(checks,"A4:G4");
checks.getRange("A5:G9").values=[
  ["主体税号",data.credit_code||"", "非空",data.credit_code?"":"税号未配置",0,data.credit_code?"OK":"FAIL","数据与配置"],
  ["申报表数量",data.summary.form_count,5,data.summary.form_count-5,0,data.summary.form_count===5?"OK":"FAIL","税务工作台"],
  ["可直接上传表",data.summary.direct_upload_ready,0,0,0,"OK","取得属地模板并完成适配后才改变"],
  ["待补资料表",data.summary.blocked,0,data.summary.blocked,0,data.summary.blocked===0?"OK":"WARN","各表阻塞事项"],
  ["MODEL STATUS",data.summary.blocked,0,data.summary.blocked,0,data.summary.blocked===0?"PASS":"REVIEW","先处理FAIL/WARN并由有权人复核"],
];body(checks,"A5:G9");checks.getRange("F5:F9").conditionalFormats.add("containsText",{text:"OK",format:{fill:"#DFF1E8",font:{color:C.green,bold:true}}});checks.getRange("F5:F9").conditionalFormats.add("containsText",{text:"FAIL",format:{fill:C.red,font:{color:"#B3453F",bold:true}}});checks.getRange("F5:F9").conditionalFormats.add("containsText",{text:"WARN",format:{fill:C.amber,font:{color:"#725224",bold:true}}});widths(checks,{A:28,B:22,C:18,D:28,E:12,F:14,G:44});checks.freezePanes.freezeRows(4);

if(verifyDir){await fs.mkdir(verifyDir,{recursive:true});const inspect=await workbook.inspect({kind:"table",range:"Checks!A1:G9",include:"values,formulas",tableMaxRows:12,tableMaxCols:8,maxChars:8000});await fs.writeFile(`${verifyDir}/checks.ndjson`,inspect.ndjson,"utf8");const errors=await workbook.inspect({kind:"match",searchTerm:"#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",options:{useRegex:true,maxResults:200},summary:"formula errors"});await fs.writeFile(`${verifyDir}/formula-errors.ndjson`,errors.ndjson,"utf8");for(const sheet of workbook.worksheets.items){const preview=await workbook.render({sheetName:sheet.name,autoCrop:"all",scale:1,format:"png"});await fs.writeFile(`${verifyDir}/${sheet.name}.png`,new Uint8Array(await preview.arrayBuffer()));}}
await fs.mkdir(outputPath.slice(0,outputPath.lastIndexOf("/")),{recursive:true});const output=await SpreadsheetFile.exportXlsx(workbook);await output.save(outputPath);
console.log(JSON.stringify({outputPath,sheets:workbook.worksheets.items.map(s=>s.name),summary:data.summary}));
