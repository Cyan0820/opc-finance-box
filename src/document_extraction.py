from __future__ import annotations

import csv
import hashlib
import io
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from pypdf import PdfReader


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
DATE_PATTERN = re.compile(r"20\d{2}[-/.\u5e74]\d{1,2}[-/.\u6708]\d{1,2}\u65e5?")


def _run_ocr(image_path: Path) -> tuple[str, float]:
    tesseract = shutil.which("tesseract")
    if not tesseract:
        raise RuntimeError("本机未安装 tesseract，无法识别扫描件或图片")
    result = subprocess.run(
        [tesseract, str(image_path), "stdout", "-l", "chi_sim+eng", "--psm", "6", "tsv"],
        check=True, capture_output=True, text=True, timeout=120,
    )
    words, confidences = [], []
    for row in csv.DictReader(io.StringIO(result.stdout), delimiter="\t"):
        word = str(row.get("text") or "").strip()
        if not word:
            continue
        words.append(word)
        try:
            confidence = float(row.get("conf") or -1)
        except ValueError:
            confidence = -1
        if confidence >= 0:
            confidences.append(confidence)
    text = " ".join(words)
    return text, round(sum(confidences) / len(confidences) / 100, 4) if confidences else 0.35


def extract_document_text(path: str | Path) -> dict:
    path = Path(path)
    extension = path.suffix.lower()
    pages = []
    method = ""
    if extension == ".pdf":
        reader = PdfReader(str(path))
        native_pages = [str(page.extract_text() or "").strip() for page in reader.pages]
        if sum(len(text) for text in native_pages) >= 30:
            pages = [
                {"page": index, "text": text, "confidence": 0.98 if text else 0.0, "method": "pdf_text"}
                for index, text in enumerate(native_pages, 1)
            ]
            method = "pdf_text"
        else:
            pdftoppm = shutil.which("pdftoppm")
            if not pdftoppm:
                raise RuntimeError("本机缺少 pdftoppm，无法把扫描 PDF 转为图片识别")
            with tempfile.TemporaryDirectory(prefix="finance-inbox-pdf-") as temp_dir:
                prefix = Path(temp_dir) / "page"
                subprocess.run(
                    [pdftoppm, "-png", "-r", "200", str(path), str(prefix)],
                    check=True, capture_output=True, timeout=180,
                )
                for index, image in enumerate(sorted(Path(temp_dir).glob("page-*.png")), 1):
                    text, confidence = _run_ocr(image)
                    pages.append({"page": index, "text": text, "confidence": confidence, "method": "ocr"})
            method = "ocr"
    elif extension in IMAGE_EXTENSIONS:
        text, confidence = _run_ocr(path)
        pages = [{"page": 1, "text": text, "confidence": confidence, "method": "ocr"}]
        method = "ocr"
    else:
        raise ValueError("文字提取仅支持 PDF、PNG、JPG、JPEG 和 WEBP")
    full_text = "\n".join(page["text"] for page in pages if page["text"])
    average = (
        round(sum(page["confidence"] for page in pages) / len(pages), 4) if pages else 0.0
    )
    return {
        "method": method, "page_count": len(pages), "pages": pages,
        "text": full_text, "confidence": average,
    }


def _match(text: str, patterns: tuple[str, ...], group: int = 1) -> tuple[str, str]:
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return match.group(group).strip(), match.group(0).strip()
    return "", ""


def _amount(value: str) -> float | None:
    if not value:
        return None
    match = re.search(r"-?\d[\d,]*(?:\.\d{1,2})?", str(value))
    if not match:
        return None
    try:
        return round(float(match.group(0).replace(",", "")), 2)
    except ValueError:
        return None


def extract_invoice_fields(extraction: dict, source_file: str = "") -> dict:
    text = str(extraction.get("text") or "")
    compact = re.sub(r"[ \t]+", " ", text)
    number, number_evidence = _match(compact, (
        r"(?:数电票号码|发票号码|发票号)\s*[:：]?\s*([0-9A-Z]{6,30})",
        r"Invoice\s*(?:No\.?|Number)\s*[:：#]?\s*([0-9A-Z-]{5,30})",
    ))
    code, code_evidence = _match(compact, (r"发票代码\s*[:：]?\s*([0-9]{8,20})",))
    invoice_date, date_evidence = _match(compact, (
        r"(?:开票日期|发票日期)\s*[:：]?\s*(20\d{2}[-/.年]\s*\d{1,2}[-/.月]\s*\d{1,2}日?)",
        r"(?:Date)\s*[:：]?\s*(20\d{2}[-/.]\d{1,2}[-/.]\d{1,2})",
    ))
    invoice_date = re.sub(r"[年/.]", "-", invoice_date).replace("月", "-").replace("日", "")
    invoice_date = re.sub(r"\s+", "", invoice_date)
    buyer, buyer_evidence = _match(compact, (
        r"(?:购买方名称|购方名称)\s*[:：]?\s*([^\n]{2,80}?)(?=\s*(?:纳税人识别号|统一社会信用代码|地址|电话|销售方|$))",
        r"Buyer\s*[:：]?\s*(.{2,100}?)(?=\s*(?:Seller|Amount|Subtotal|Tax|Total|$))",
    ))
    seller, seller_evidence = _match(compact, (
        r"(?:销售方名称|销方名称)\s*[:：]?\s*([^\n]{2,80}?)(?=\s*(?:纳税人识别号|统一社会信用代码|地址|电话|备注|$))",
        r"Seller\s*[:：]?\s*(.{2,100}?)(?=\s*(?:Buyer|Amount|Subtotal|Tax|Total|$))",
    ))
    seller_tax_id, tax_id_evidence = _match(compact, (
        r"(?:销售方.{0,20})?(?:纳税人识别号|统一社会信用代码)\s*[:：]?\s*([0-9A-Z]{15,20})",
    ))
    total, total_evidence = _match(compact, (
        r"(?:价税合计(?:\s*\(小写\))?|小写)\s*[:：]?\s*[¥￥]?\s*([0-9,]+(?:\.\d{1,2})?)",
        r"(?:Total|Amount Due)\s*[:：]?\s*[¥￥$]?\s*([0-9,]+(?:\.\d{1,2})?)",
    ))
    tax, tax_evidence = _match(compact, (
        r"(?:合计税额|税额)\s*[:：]?\s*[¥￥]?\s*([0-9,]+(?:\.\d{1,2})?)",
        r"Tax\s*[:：]?\s*[¥￥$]?\s*([0-9,]+(?:\.\d{1,2})?)",
    ))
    amount_ex_tax, amount_evidence = _match(compact, (
        r"(?:合计金额|不含税金额)\s*[:：]?\s*[¥￥]?\s*([0-9,]+(?:\.\d{1,2})?)",
        r"(?:Subtotal|Amount)\s*[:：]?\s*[¥￥$]?\s*([0-9,]+(?:\.\d{1,2})?)",
    ))
    total_value, tax_value, amount_value = _amount(total), _amount(tax), _amount(amount_ex_tax)
    if amount_value is None and total_value is not None and tax_value is not None:
        amount_value = round(total_value - tax_value, 2)
    if total_value is None and amount_value is not None and tax_value is not None:
        total_value = round(amount_value + tax_value, 2)

    values = {
        "invoice_number": number, "invoice_code": code, "invoice_date": invoice_date,
        "buyer_name": buyer, "seller_name": seller, "seller_tax_id": seller_tax_id,
        "amount_ex_tax": amount_value, "tax_amount": tax_value, "total_amount": total_value,
    }
    missing = [key for key in ("invoice_number", "invoice_date", "seller_name", "total_amount") if not values.get(key)]
    extraction_confidence = float(extraction.get("confidence") or 0)
    found_ratio = (4 - len(missing)) / 4
    confidence = round(min(0.99, extraction_confidence * 0.55 + found_ratio * 0.45), 4)
    evidence = {
        "invoice_number": number_evidence, "invoice_code": code_evidence,
        "invoice_date": date_evidence, "buyer_name": buyer_evidence,
        "seller_name": seller_evidence, "seller_tax_id": tax_id_evidence,
        "amount_ex_tax": amount_evidence, "tax_amount": tax_evidence, "total_amount": total_evidence,
    }
    return {
        "document_kind": "invoice",
        "fields": values,
        "field_evidence": {key: value for key, value in evidence.items() if value},
        "confidence": confidence,
        "missing_fields": missing,
        "requires_human_confirmation": True,
        "recommendation": (
            "关键字段基本齐全；请对照原票确认号码、购销方和价税合计，再查验发票状态。"
            if not missing else f"先补核对：{'、'.join(missing)}；识别结果不能直接入账。"
        ),
        "source_file": source_file,
    }


def invoice_record_from_extraction(extracted: dict, source_file: str) -> dict:
    fields = extracted["fields"]
    raw_tax_id = str(fields.get("seller_tax_id") or "")
    masked_tax_id = (
        f"{raw_tax_id[:4]}****{raw_tax_id[-4:]}" if len(raw_tax_id) > 8 else raw_tax_id
    )
    identity = f"{source_file}|{fields.get('invoice_code')}|{fields.get('invoice_number')}|{fields.get('total_amount')}"
    anomalies = ["OCR/文字层识别结果尚未由人工对照原票确认", "尚未完成发票查验"]
    if extracted.get("missing_fields"):
        anomalies.append(f"缺少关键字段：{'、'.join(extracted['missing_fields'])}")
    return {
        "id": hashlib.sha1(identity.encode("utf-8")).hexdigest()[:12],
        "source_file": source_file, "source_sheet": "PDF/图片", "source_row": 1,
        "invoice_number": fields.get("invoice_number") or "",
        "invoice_code": fields.get("invoice_code") or "",
        "invoice_date": fields.get("invoice_date") or "",
        "invoice_type": "待确认票种",
        "seller_name": fields.get("seller_name") or "待识别销售方",
        "seller_tax_id_masked": masked_tax_id,
        "buyer_name": fields.get("buyer_name") or "",
        "item": "待识别项目", "amount_ex_tax": fields.get("amount_ex_tax") or 0,
        "tax_rate": None, "tax_amount": fields.get("tax_amount") or 0,
        "total_amount": fields.get("total_amount") or 0,
        "po_number": "", "project": "待分配项目",
        "verification_status": "待查验", "deduction_status": "待确认用途",
        "booking_status": "未入账", "duplicate_key": fields.get("invoice_number") or identity,
        "status": "待人工确认", "confidence": extracted.get("confidence"),
        "anomalies": anomalies,
        "extraction_evidence": extracted.get("field_evidence") or {},
    }


def _normalized_date(value: str) -> str:
    value = re.sub(r"[\u5e74/.]", "-", str(value or "")).replace("月", "-").replace("日", "")
    parts = [item for item in re.sub(r"\s+", "", value).split("-") if item]
    if len(parts) != 3:
        return ""
    try:
        return f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
    except ValueError:
        return ""


def _label_value(text: str, labels: tuple[str, ...], *, stop: tuple[str, ...] = ()) -> tuple[str, str]:
    label_pattern = "|".join(re.escape(item) for item in labels)
    stop_pattern = "|".join(re.escape(item) for item in stop)
    tail = rf"(?=\s*(?:{stop_pattern})\s*[:：]?|$)" if stop_pattern else r"(?=$)"
    match = re.search(rf"(?:{label_pattern})\s*[:：]?\s*(.+?){tail}", text, re.I)
    return (match.group(1).strip(), match.group(0).strip()) if match else ("", "")


def extract_bank_statement_rows(extraction: dict, source_file: str = "") -> dict:
    """Extract conservative transaction candidates from labeled PDF/OCR text.

    This intentionally requires a date and an amount on every candidate. It does
    not infer a legal entity, and ambiguous direction/currency remain blockers.
    """
    text = str(extraction.get("text") or "")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines() if line.strip()]
    if not lines and text.strip():
        lines = [re.sub(r"[ \t]+", " ", text).strip()]
    # OCR sometimes places one transaction over adjacent lines. A new date starts
    # a new candidate block; non-date lines are appended to the current block.
    blocks: list[str] = []
    current = ""
    for line in lines:
        if DATE_PATTERN.search(line):
            if current:
                blocks.append(current)
            current = line
        elif current:
            current = f"{current} {line}"
    if current:
        blocks.append(current)

    label_stops = (
        "交易流水号", "银行流水号", "交易编号", "Reference", "Transaction ID",
        "对方户名", "对方名称", "Counterparty", "Beneficiary", "摘要", "附言", "Description", "Narrative",
        "收支方向", "借贷标志", "Direction", "币种", "Currency", "交易金额", "发生额", "金额", "Amount",
        "收入金额", "贷方发生额", "Credit", "支出金额", "借方发生额", "Debit", "余额", "账户余额", "Balance",
    )
    candidates = []
    for index, block in enumerate(blocks, 1):
        date_match = DATE_PATTERN.search(block)
        transaction_date = _normalized_date(date_match.group(0)) if date_match else ""
        transaction_id, id_evidence = _label_value(block, (
            "交易流水号", "银行流水号", "交易编号", "Reference", "Transaction ID",
        ), stop=label_stops)
        counterparty, counterparty_evidence = _label_value(block, (
            "对方户名", "对方名称", "Counterparty", "Beneficiary",
        ), stop=label_stops)
        summary, summary_evidence = _label_value(block, (
            "摘要", "附言", "Description", "Narrative",
        ), stop=label_stops)
        currency, currency_evidence = _label_value(block, ("币种", "Currency"), stop=label_stops)
        currency_match = re.search(r"\b(CNY|RMB|USD|HKD|SGD|EUR|GBP|JPY|AUD|CAD)\b", currency or block, re.I)
        currency = (currency_match.group(1).upper().replace("RMB", "CNY") if currency_match else "")
        direction_text, direction_evidence = _label_value(block, ("收支方向", "借贷标志", "Direction"), stop=label_stops)
        credit, credit_evidence = _label_value(block, ("收入金额", "贷方发生额", "Credit"), stop=label_stops)
        debit, debit_evidence = _label_value(block, ("支出金额", "借方发生额", "Debit"), stop=label_stops)
        amount, amount_evidence = _label_value(block, ("交易金额", "发生额", "金额", "Amount"), stop=label_stops)
        balance, balance_evidence = _label_value(block, ("账户余额", "余额", "Balance"), stop=label_stops)
        credit_value, debit_value, amount_value = _amount(credit), _amount(debit), _amount(amount)
        if credit_value not in {None, 0}:
            direction, amount_value = "收入", abs(float(credit_value))
        elif debit_value not in {None, 0}:
            direction, amount_value = "支出", abs(float(debit_value))
        elif amount_value is not None:
            raw = str(direction_text or "").lower()
            if any(token in raw for token in ("收", "贷", "credit", "in")):
                direction = "收入"
            elif any(token in raw for token in ("支", "借", "debit", "out")) or amount_value < 0:
                direction = "支出"
            else:
                direction = "待确认"
            amount_value = abs(float(amount_value))
        else:
            continue
        if not transaction_date:
            continue
        missing = []
        if not transaction_id:
            missing.append("transaction_id")
        if direction == "待确认":
            missing.append("direction")
        if not currency:
            missing.append("currency")
        evidence = {
            "transaction_date": date_match.group(0) if date_match else "",
            "transaction_id": id_evidence, "counterparty": counterparty_evidence,
            "summary": summary_evidence, "direction": direction_evidence or credit_evidence or debit_evidence,
            "currency": currency_evidence or (currency_match.group(0) if currency_match else ""),
            "amount": credit_evidence or debit_evidence or amount_evidence,
            "balance": balance_evidence,
        }
        candidates.append({
            "source_index": index, "transaction_date": transaction_date,
            "transaction_id": transaction_id, "counterparty": counterparty,
            "summary": summary, "direction": direction, "currency": currency,
            "amount": round(amount_value, 2), "balance": _amount(balance),
            "missing_fields": missing,
            "field_evidence": {key: value for key, value in evidence.items() if value},
        })
    extraction_confidence = float(extraction.get("confidence") or 0)
    completeness = (
        sum(1 - len(item["missing_fields"]) / 3 for item in candidates) / len(candidates)
        if candidates else 0
    )
    confidence = round(min(0.99, extraction_confidence * 0.55 + completeness * 0.45), 4)
    return {
        "document_kind": "bank_statement", "rows": candidates,
        "confidence": confidence, "row_count": len(candidates),
        "requires_human_confirmation": True,
        "recommendation": (
            "已形成银行流水候选；请逐页核对账号归属、收支方向、币种、金额和余额后再入台账。"
            if candidates else "文字已提取，但没有找到同时包含交易日期和金额的可靠流水行。"
        ),
        "source_file": source_file,
    }


def bank_records_from_extraction(extracted: dict, source_file: str) -> list[dict]:
    records = []
    for row in extracted.get("rows") or []:
        identity = f"{source_file}|{row.get('source_index')}|{row.get('transaction_id')}|{row.get('transaction_date')}|{row.get('amount')}"
        anomalies = ["OCR/文字层识别结果尚未由人工对照原件确认"]
        if row.get("missing_fields"):
            anomalies.append(f"缺少关键字段：{'、'.join(row['missing_fields'])}")
        records.append({
            "id": hashlib.sha1(identity.encode("utf-8")).hexdigest()[:12],
            "source_file": source_file, "source_sheet": "PDF/图片", "source_row": row.get("source_index") or 1,
            "transaction_date": row.get("transaction_date") or "",
            "transaction_id": row.get("transaction_id") or "",
            "account_masked": "", "counterparty": row.get("counterparty") or "",
            "counterparty_account_masked": "", "summary": row.get("summary") or "",
            "direction": row.get("direction") or "待确认", "currency": row.get("currency") or "待确认",
            "amount": row.get("amount") or 0, "balance": row.get("balance"),
            "status": "待人工确认", "suggested_match": None,
            "confidence": extracted.get("confidence"), "anomalies": anomalies,
            "requires_human_confirmation": True,
            "extraction_evidence": row.get("field_evidence") or {},
        })
    return records
