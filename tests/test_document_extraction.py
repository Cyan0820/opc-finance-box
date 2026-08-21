import unittest

from src.document_extraction import (
    bank_records_from_extraction, extract_bank_statement_rows,
    extract_invoice_fields, invoice_record_from_extraction,
)


class DocumentExtractionTests(unittest.TestCase):
    def test_invoice_fields_keep_evidence_and_require_confirmation(self):
        extraction = {
            "text": (
                "增值税电子普通发票\n发票号码：12345678901234567890\n"
                "开票日期：2026年01月05日\n购买方名称：星火游戏有限公司 纳税人识别号：913100001234567890\n"
                "销售方名称：云服务有限公司 纳税人识别号：914400001234567890\n"
                "合计金额：1000.00 合计税额：60.00 价税合计(小写)：1060.00"
            ),
            "confidence": 0.96,
        }
        result = extract_invoice_fields(extraction, "发票.pdf")
        self.assertEqual(result["fields"]["invoice_number"], "12345678901234567890")
        self.assertEqual(result["fields"]["total_amount"], 1060)
        self.assertTrue(result["requires_human_confirmation"])
        self.assertIn("发票号码", result["field_evidence"]["invoice_number"])
        record = invoice_record_from_extraction(result, "发票.pdf")
        self.assertEqual(record["status"], "待人工确认")
        self.assertEqual(record["verification_status"], "待查验")

    def test_missing_fields_reduce_confidence_and_block_silent_booking(self):
        result = extract_invoice_fields({"text": "价税合计(小写)：88.00", "confidence": 0.9})
        self.assertIn("invoice_number", result["missing_fields"])
        record = invoice_record_from_extraction(result, "模糊.jpg")
        self.assertTrue(any("缺少关键字段" in item for item in record["anomalies"]))

    def test_overseas_english_invoice_is_supported(self):
        result = extract_invoice_fields({
            "text": (
                "ELECTRONIC INVOICE Invoice No: INV2026010001 Date: 2026-01-05 "
                "Seller: Cloud Service Limited Buyer: Spark Game Limited "
                "Amount: 1000.00 Tax: 60.00 Total: 1060.00"
            ),
            "confidence": 0.96,
        })
        self.assertEqual(result["fields"]["seller_name"], "Cloud Service Limited")
        self.assertEqual(result["fields"]["buyer_name"], "Spark Game Limited")
        self.assertEqual(result["fields"]["amount_ex_tax"], 1000)
        self.assertEqual(result["fields"]["tax_amount"], 60)

    def test_bank_pdf_text_becomes_conservative_candidates_with_evidence(self):
        result = extract_bank_statement_rows({
            "text": (
                "2026-08-01 交易流水号: TX001 对方户名: Apple App Store "
                "摘要: 七月结算 收支方向: 收入 币种: USD 交易金额: $1,250.50 账户余额: 5,000.50\n"
                "2026-08-02 Reference: TX002 Counterparty: Cloud Vendor "
                "Description: hosting Currency: USD Debit: 220.00 Balance: 4,780.50"
            ),
            "confidence": 0.95,
        }, "海外银行对账单.pdf")
        self.assertEqual(result["row_count"], 2)
        self.assertEqual(result["rows"][0]["amount"], 1250.5)
        self.assertEqual(result["rows"][1]["direction"], "支出")
        self.assertIn("Debit", result["rows"][1]["field_evidence"]["amount"])
        records = bank_records_from_extraction(result, "海外银行对账单.pdf")
        self.assertTrue(all(row["status"] == "待人工确认" for row in records))
        self.assertTrue(all(row["requires_human_confirmation"] for row in records))

    def test_bank_candidate_keeps_ambiguous_fields_as_blockers(self):
        result = extract_bank_statement_rows({
            "text": "2026-08-03 交易金额: 88.00 对方户名: 待确认商户",
            "confidence": 0.8,
        })
        self.assertEqual(result["row_count"], 1)
        self.assertEqual(
            result["rows"][0]["missing_fields"],
            ["transaction_id", "direction", "currency"],
        )


if __name__ == "__main__":
    unittest.main()
