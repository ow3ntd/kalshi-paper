import unittest
from collector import selection_details, choose_market

class SelectionDiagnosticsTests(unittest.TestCase):
    def market(self):
        return {'ticker': 'TEST', 'status': 'active', 'market_type': 'binary',
                'open_time': '1970-01-01T00:00:00Z',
                'close_time': '1970-01-01T00:20:00Z',
                'expiration_time': '1970-01-01T00:15:00Z'}

    def test_explains_earlier_expiration_rejection(self):
        details = selection_details(self.market(), 500, 600)
        self.assertEqual(details['remaining_seconds'], 400)
        self.assertEqual(details['rejection_reasons'], ['insufficient_remaining_time'])

    def test_clock_past_deadline_is_distinct_from_threshold(self):
        details = selection_details(self.market(), 1300, 600)
        self.assertEqual(details['rejection_reasons'], ['close_or_expiration_has_passed'])

    def test_selection_agrees_with_explanation_at_boundary(self):
        m = self.market()
        self.assertTrue(selection_details(m, 300, 600)['eligible'])
        self.assertEqual(choose_market(lambda *a, **k: {'markets': [m]}, 'SERIES', 300, 600), 'TEST')

if __name__ == '__main__':
    unittest.main()
