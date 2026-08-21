import unittest

from src.invoices import (
    InvoiceRecord, invoice_payload, match_invoices_to_purchases,
    roll_invoice_totals_to_purchases,
)


class InvoiceTests(unittest.TestCase):
    def invoice(self, invoice_id='I1', amount=5000, po_number='PO1'):
        return InvoiceRecord(
            id=invoice_id, source_file='x.xlsx', source_sheet='票', source_row=2,
            invoice_number=invoice_id, invoice_code='', invoice_date='2026-01-20', invoice_type='数电专票',
            seller_name='素材供应商', seller_tax_id_masked='1234****5678', buyer_name='游戏公司',
            item='视频制作', amount_ex_tax=amount, tax_rate=None, tax_amount=0,
            total_amount=amount, po_number=po_number, project='游戏甲', verification_status='已查验',
            deduction_status='待确认用途', booking_status='未入账', duplicate_key=invoice_id,
            status='待匹配采购', anomalies=[],
        )

    def test_invoice_matches_purchase_by_po_vendor_and_amount(self):
        invoice = InvoiceRecord(
            id='I1', source_file='x.xlsx', source_sheet='票', source_row=2,
            invoice_number='123', invoice_code='', invoice_date='2026-01-20', invoice_type='数电专票',
            seller_name='素材供应商', seller_tax_id_masked='1234****5678', buyer_name='游戏公司',
            item='视频制作', amount_ex_tax=4716.98, tax_rate=0.06, tax_amount=283.02,
            total_amount=5000, po_number='PO1', project='游戏甲', verification_status='已查验',
            deduction_status='待确认用途', booking_status='未入账', duplicate_key='123',
            status='待匹配采购', anomalies=[],
        )
        purchases = [{
            'id': 'P1', 'po_number': 'PO1', 'vendor': '素材供应商', 'item': '视频制作',
            'ordered_amount': 5000, 'accepted_amount': 5000,
        }]
        matched = match_invoices_to_purchases([invoice], purchases)
        self.assertEqual(matched[0]['status'], '已匹配待入账')
        self.assertEqual(matched[0]['purchase_match']['score'], 1)

    def test_invoice_summary_surfaces_unverified_documents(self):
        payload = invoice_payload([{
            'amount_ex_tax': 100, 'tax_amount': 6, 'total_amount': 106,
            'purchase_match': None, 'anomalies': ['尚未完成发票查验'], 'verification_status': '待查验',
        }])
        self.assertEqual(payload['summary']['unverified_count'], 1)
        self.assertEqual(payload['summary']['exception_count'], 1)

    def test_workflow_order_without_acceptance_cannot_enter_payment_chain(self):
        purchase = {
            'id': 'P1', 'procurement_request_id': 'REQ1', 'po_number': 'PO1',
            'vendor': '素材供应商', 'item': '视频制作', 'ordered_amount': 5000,
            'accepted_amount': 0, 'milestones': [{'id': 'M1', 'amount': 5000}],
        }
        matched = match_invoices_to_purchases([self.invoice()], [purchase])[0]
        self.assertEqual(matched['purchase_match']['control_status'], '待验收')
        self.assertFalse(matched['purchase_match']['eligible_for_payment'])
        self.assertTrue(any('尚无已验收交付' in item for item in matched['anomalies']))

    def test_partial_invoices_consume_acceptance_capacity_in_order(self):
        purchase = {
            'id': 'P1', 'procurement_request_id': 'REQ1', 'po_number': 'PO1',
            'vendor': '素材供应商', 'item': '视频制作', 'ordered_amount': 100,
            'accepted_amount': 100, 'milestones': [{'id': 'M1', 'amount': 100}],
            'acceptance_history': [{'delivery_id': 'DEL1'}],
        }
        rows = match_invoices_to_purchases([
            self.invoice('I1', 40), self.invoice('I2', 60), self.invoice('I3', 10),
        ], [purchase])
        self.assertTrue(rows[0]['purchase_match']['eligible_for_payment'])
        self.assertEqual(rows[1]['purchase_match']['previously_invoiced_amount'], 40)
        self.assertTrue(rows[1]['purchase_match']['eligible_for_payment'])
        self.assertEqual(rows[1]['purchase_match']['accepted_delivery_ids'], ['DEL1'])
        self.assertFalse(rows[2]['purchase_match']['eligible_for_payment'])
        self.assertTrue(any('超过剩余已验收额度' in item for item in rows[2]['anomalies']))

    def test_existing_invoice_reduces_new_batch_capacity(self):
        purchase = {
            'id': 'P1', 'procurement_request_id': 'REQ1', 'po_number': 'PO1',
            'vendor': '素材供应商', 'item': '视频制作', 'ordered_amount': 100,
            'accepted_amount': 100, 'milestones': [{'id': 'M1', 'amount': 100}],
        }
        existing = [{
            'id': 'OLD', 'total_amount': 70, 'verification_status': '已查验', 'anomalies': [],
            'purchase_match': {'purchase_id': 'P1', 'eligible_for_payment': True},
        }]
        row = match_invoices_to_purchases([self.invoice('NEW', 40)], [purchase], existing)[0]
        self.assertEqual(row['purchase_match']['remaining_accepted_capacity_before'], 30)
        self.assertFalse(row['purchase_match']['eligible_for_payment'])

    def test_roll_invoice_totals_updates_order_without_overwriting_acceptance(self):
        purchase = {
            'id': 'P1', 'procurement_request_id': 'REQ1', 'ordered_amount': 100,
            'accepted_amount': 100, 'invoice_amount': 0, 'anomalies': [],
            'acceptance_history': [{'delivery_id': 'DEL1'}],
        }
        invoices = [{
            'id': 'I1', 'total_amount': 60, 'verification_status': '已查验', 'anomalies': [],
            'purchase_match': {'purchase_id': 'P1', 'eligible_for_payment': True},
        }]
        updated = roll_invoice_totals_to_purchases([purchase], invoices)[0]
        self.assertEqual(updated['accepted_amount'], 100)
        self.assertEqual(updated['invoice_amount'], 60)
        self.assertEqual(updated['invoice_status'], '部分开票')
        self.assertEqual(updated['payment_eligible_amount'], 60)
        self.assertEqual(updated['invoice_match_summary']['accepted_delivery_ids'], ['DEL1'])


if __name__ == '__main__':
    unittest.main()
