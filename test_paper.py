import argparse
import unittest
from decimal import Decimal as D
from paper import Experiment, book, fee

def row(t, bid='.49', no='.50'):
    return {'received_at': t, 'book': {'orderbook_fp': {
        'yes_dollars': [[bid, '100']], 'no_dollars': [[no, '10']]}}}

class Tests(unittest.TestCase):
    def engine(self):
        return Experiment(argparse.Namespace(cash=1000, max_gap=15, slippage=.001,
            fee_coefficient=.07, fee_quantum=.01, hold=2, latency=1,
            threshold=.6, max_spread=.02))

    def test_binary_complement_and_unsorted(self):
        r = row(0)
        r['book']['orderbook_fp']['yes_dollars'].append(['.48', '2'])
        self.assertEqual(book(r['book'])[:2], (D('.49'), D('.50')))

    def test_delay_and_both_fees(self):
        e = self.engine()
        e.step(row(0))
        self.assertIsNone(e.position)
        e.step(row(1))
        e.step(row(3))
        e.step(row(4, '.51', '.48'))
        self.assertEqual(len(e.completed), 1)
        self.assertAlmostEqual(e.completed[0], -.032)
        self.assertEqual(e.fees, D('.04'))

    def test_gap_cancels_pending(self):
        e = self.engine()
        e.step(row(0))
        e.step(row(20))
        self.assertIsNone(e.position)

    def test_empty_book_no_fill(self):
        e = self.engine()
        e.step(row(0))
        r = row(1)
        r['book']['orderbook_fp']['no_dollars'] = []
        e.step(r)
        self.assertIsNone(e.position)
        self.assertIsNone(e.pending)

    def test_rounding(self):
        self.assertEqual(fee(D('.5'), D('.07'), D('.01')), D('.02'))

if __name__ == '__main__':
    unittest.main()
