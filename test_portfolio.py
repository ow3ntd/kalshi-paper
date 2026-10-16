import unittest
from decimal import Decimal as D
from portfolio import Fill, Portfolio

class AccountingTests(unittest.TestCase):
    def test_open_loss_visible_even_with_no_closed_trades(self):
        p = Portfolio(D('100'))
        p.apply(Fill('buy', D('.50'), D('1'), D('.02'), 1))
        v = p.valuation(D('.40'))
        self.assertEqual(v['realized_net_pnl'], D('0'))
        self.assertEqual(v['total_pnl_before_exit_costs'], D('-.12'))
        self.assertEqual(v['equity_before_exit_costs'], D('99.88'))

    def test_partial_exit_reconciles(self):
        p = Portfolio(D('100'))
        p.apply(Fill('buy', D('.50'), D('2'), D('.04'), 1))
        p.apply(Fill('sell', D('.60'), D('1'), D('.02'), 2))
        v = p.valuation(D('.55'))
        self.assertEqual(p.realized_pnl, D('.06'))
        self.assertEqual(p.cost_basis, D('.52'))
        self.assertEqual(v['total_pnl_before_exit_costs'], D('.09'))
        self.assertEqual(v['total_pnl_before_exit_costs'], p.realized_pnl+v['unrealized_pnl_before_exit_costs'])

    def test_no_mark_is_unknown_not_zero(self):
        p = Portfolio(D('100'))
        self.assertEqual(p.valuation(None)['equity_before_exit_costs'], D('100'))
        p.apply(Fill('buy', D('.50'), D('1'), D('0'), 1))
        self.assertIsNone(p.valuation(None)['equity_before_exit_costs'])

    def test_rejected_fill_does_not_mutate(self):
        p = Portfolio(D('1'))
        for fill in [Fill('buy', D('.5'), D('3'), D('0'), 1),
                     Fill('sell', D('.5'), D('1'), D('0'), 1),
                     Fill('buy', D('NaN'), D('1'), D('0'), 1)]:
            with self.assertRaises(ValueError):
                p.apply(fill)
            self.assertEqual(p.cash, D('1'))
            self.assertEqual(p.fills, [])

    def test_round_trip_cash_equals_realized(self):
        p = Portfolio(D('100'))
        p.apply(Fill('buy', D('.501'), D('1'), D('.02'), 1))
        p.apply(Fill('sell', D('.509'), D('1'), D('.02'), 2))
        self.assertEqual(p.quantity, D('0'))
        self.assertEqual(p.cost_basis, D('0'))
        self.assertEqual(p.realized_pnl, D('-.032'))
        self.assertEqual(p.cash-p.initial_cash, p.realized_pnl)

if __name__ == '__main__':
    unittest.main()
