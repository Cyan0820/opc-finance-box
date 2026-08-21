import tempfile
import unittest
from pathlib import Path

from src.company_profile import DEFAULT_PROFILE, load_profile, profile_gaps, save_profile, validate_profile


class CompanyProfileTests(unittest.TestCase):
    def test_default_profile_exposes_material_setup_gaps(self):
        gaps = profile_gaps(DEFAULT_PROFILE)
        fields = {gap['field'] for gap in gaps}
        self.assertIn('vat_taxpayer_type', fields)
        self.assertIn('vat_filing_frequency', fields)
        self.assertIn('external_accountant.provider', fields)

    def test_profile_round_trip_and_nested_merge(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / 'profile.json'
            saved = save_profile(path, {
                'company_name': '测试游戏公司', 'base_currency': 'CNY',
                'vat_taxpayer_type': '一般纳税人', 'vat_filing_frequency': '月度',
                'review_policy': {'materiality_cny': 5000},
            })
            loaded = load_profile(path)
            self.assertEqual(saved['company_name'], '测试游戏公司')
            self.assertEqual(loaded['review_policy']['materiality_cny'], 5000)
            self.assertEqual(loaded['review_policy']['high_confidence_threshold'], 0.92)

    def test_invalid_base_currency_is_rejected(self):
        invalid = dict(DEFAULT_PROFILE, base_currency='USD')
        self.assertTrue(any('本位币' in error for error in validate_profile(invalid)))

    def test_shanghai_profile_exposes_vat_pilot_identity_gap(self):
        profile = dict(DEFAULT_PROFILE, registered_city='上海市徐汇区')
        self.assertTrue(any(gap['field'] == 'tax_policy.shanghai_vat_pilot_status' for gap in profile_gaps(profile)))


if __name__ == '__main__':
    unittest.main()
