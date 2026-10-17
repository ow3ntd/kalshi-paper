import unittest
from decimal import Decimal as D
from clocks import EventClock
import test_paper

def snapshot(elapsed, wall=None, **extra):
    return dict(test_paper.row(wall if wall is not None else 1000+elapsed),
                type='snapshot', ticker='TEST', elapsed_s=elapsed, **extra)

class ExecutionClockTests(unittest.TestCase):
    def test_holding_period_uses_elapsed_seconds(self):
        e = test_paper.Tests().engine()
        # Calendar time runs slightly slower; elapsed duration still controls the exit.
        for t in (0, 1, 2, 3, 4):
            e.ingest(snapshot(t, 1000+.99*t))
        self.assertEqual(len(e.completed), 1)
        self.assertEqual(e.report()['strategy_clock'], 'monotonic')
        self.assertFalse(e.report()['trading_suspended_for_clock'])
        self.assertEqual(e.portfolio.fills[0].timestamp, 1000.99)

    def test_backward_jump_suspends_but_final_settlement_still_pays(self):
        e = test_paper.Tests().engine()
        e.ingest(snapshot(0))
        e.ingest(snapshot(1))
        e.ingest(snapshot(2, 990))
        self.assertTrue(e.report()['trading_suspended_for_clock'])
        self.assertEqual(e.portfolio.quantity, D(1))
        e.ingest({'type': 'market', 'ticker': 'TEST', 'received_at': 991, 'elapsed_s': 3,
                  'market': {'ticker': 'TEST', 'status': 'finalized', 'result': 'yes'}})
        self.assertEqual(e.portfolio.quantity, D(0))
        self.assertEqual(e.cash, D('1000.479'))

    def test_slow_request_uses_measured_duration(self):
        e = test_paper.Tests().engine()
        e.ingest(snapshot(0))
        e.ingest(snapshot(1, requested_at=1000.9, request_duration_ms=16000))
        self.assertIsNone(e.position)
        self.assertIsNone(e.pending)

    def test_mixed_clock_modes_rejected(self):
        clock = EventClock()
        clock.observe(snapshot(0))
        with self.assertRaisesRegex(ValueError, 'Cannot mix'):
            clock.observe({'received_at': 1001})

    def test_monotonic_regression_rejected(self):
        clock = EventClock()
        clock.observe(snapshot(2))
        with self.assertRaisesRegex(ValueError, 'chronological'):
            clock.observe(snapshot(1))

if __name__ == '__main__':
    unittest.main()
