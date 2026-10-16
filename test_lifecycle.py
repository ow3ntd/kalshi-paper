import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from decimal import Decimal as D
from pathlib import Path
from lifecycle import Lifecycle
from portfolio import Portfolio, Fill
import test_paper
from test_paper import row

def market(t, status='active', **extra):
    return {'type': 'market', 'ticker': 'SYNTHETIC', 'received_at': t,
            'market': {'ticker': 'SYNTHETIC', 'status': status,
                       'close_time': '1970-01-01T00:00:03Z', **extra}}

# Reuse the existing engine factory without inheriting its test cases.
class LifecycleTests(unittest.TestCase):
    def engine(self):
        return test_paper.Tests().engine()

    def held(self):
        e = self.engine()
        e.ingest(market(0))
        e.ingest(dict(row(0), type='snapshot'))
        e.ingest(dict(row(1), type='snapshot'))
        return e

    def test_expiration_cancels_pending_entry(self):
        e = self.engine()
        e.ingest(market(0))
        e.ingest(dict(row(2), type='snapshot'))
        e.ingest(dict(row(3), type='snapshot'))
        self.assertIsNone(e.position)
        self.assertIsNone(e.pending)

    def test_closed_inventory_unknown_until_final(self):
        e = self.held()
        e.ingest(market(3, 'closed', result='yes'))
        self.assertEqual(e.portfolio.quantity, D(1))
        self.assertIsNone(e.report()['equity_before_exit_costs'])
        self.assertIsNone(e.portfolio.settlement)

    def test_winning_and_losing_settlements(self):
        for result, cash in [('yes', D('1000.479')), ('no', D('999.479'))]:
            with self.subTest(result=result):
                e = self.held()
                e.ingest(market(4, 'finalized', result=result))
                self.assertEqual(e.cash, cash)
                self.assertIsNone(e.position)
                self.assertEqual(e.portfolio.quantity, D(0))
                self.assertEqual(e.portfolio.realized_pnl, cash-D('1000'))

    def test_provisional_and_determined_not_paid(self):
        for status, provisional in [('determined', False), ('finalized', True)]:
            e = self.held()
            e.ingest(market(4, status, result='yes', is_provisional=provisional))
            self.assertIsNone(e.portfolio.settlement)

    def test_duplicate_final_is_idempotent_and_conflict_rejected(self):
        e = self.held()
        e.ingest(market(4, 'finalized', settlement_value_dollars='0.5'))
        cash = e.cash
        e.ingest(market(5, 'finalized', settlement_value_dollars='0.5'))
        self.assertEqual(e.cash, cash)
        with self.assertRaises(ValueError):
            e.ingest(market(6, 'finalized', settlement_value_dollars='1'))
        self.assertEqual(e.cash, cash)

    def test_final_without_result_preserves_inventory(self):
        e = self.held()
        e.ingest(market(4, 'finalized'))
        self.assertEqual(e.portfolio.quantity, D(1))
        self.assertIsNone(e.portfolio.settlement)

    def test_stale_market_info_blocks_snapshot(self):
        e = self.engine()
        e.ingest(market(0, close_time='1970-01-01T00:01:00Z'))
        e.ingest(dict(row(20), type='snapshot'))
        self.assertIsNone(e.pending)

    def test_fill_after_settlement_rejected(self):
        p = Portfolio(D('100'))
        p.settle(D(0), 1)
        with self.assertRaises(ValueError):
            p.apply(Fill('buy', D('.5'), D(1), D(0), 2))

    def test_invalid_payout_does_not_change_cash(self):
        e = self.held()
        cash = e.cash
        with self.assertRaises(ValueError):
            e.ingest(market(4, 'finalized', settlement_value_dollars='NaN'))
        self.assertEqual(e.cash, cash)

    def test_fixture_replay_is_repeatable(self):
        reports = []
        for _ in range(2):
            e = self.engine()
            with redirect_stdout(StringIO()):
                for line in Path(__file__).with_name('settlement-demo.jsonl').read_text().splitlines():
                    e.ingest(json.loads(line))
            reports.append(e.report())
        self.assertEqual(reports[0], reports[1])
        self.assertEqual(reports[0]['cash'], 1000.479)
        self.assertEqual(reports[0]['open_contracts'], 0)

if __name__ == '__main__':
    unittest.main()
