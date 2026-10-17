import argparse
import json
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

from evaluation import evaluate
from paper import Experiment
from collector import collect, quality
from test_collector import Clock, market, BOOK

ROOT = Path(__file__).resolve().parent


def settings(**overrides):
    return argparse.Namespace(cash=1000, hold=60, latency=1, max_gap=15,
        threshold=.6, max_spread=.02, slippage=.001, fee_coefficient=.07,
        fee_quantum=.01, **overrides)


class EvaluationTests(unittest.TestCase):
    def fixture(self, folder, filename, ticker, transform=None):
        rows = [json.loads(line) for line in (ROOT/filename).read_text().splitlines()]
        for r in rows:
            if 'ticker' in r:
                r['ticker'] = ticker
            if 'market' in r:
                r['market']['ticker'] = ticker
        if transform:
            transform(rows)
        path = Path(folder)/(ticker+'.jsonl')
        path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        return path

    def test_win_loss_and_idle_baseline_reconcile_exactly(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = [self.fixture(tmp, 'settlement-demo.jsonl', 'SYNTHETIC-WIN'),
                     self.fixture(tmp, 'settlement-loss-demo.jsonl', 'SYNTHETIC-LOSS')]
            result = evaluate(paths, settings(), Experiment, synthetic=True)
            summary = result['summary']
            self.assertEqual(summary['included_sessions'], 2)
            self.assertEqual(D(summary['included_strategy_net_pnl']), D('-.042'))
            self.assertEqual(D(summary['whole_cohort_net_pnl']), D('-.042'))
            self.assertEqual(D(summary['included_fees_paid']), D('.04'))
            self.assertEqual(summary['negative_sessions'], 1)
            self.assertEqual(summary['positive_sessions'], 1)
            for s in result['sessions']:
                self.assertEqual(s['no_trade']['cash'], 1000)
                self.assertEqual(s['no_trade']['fills'], [])
                self.assertEqual(s['no_trade']['fees_paid'], 0)
                self.assertEqual(s['no_trade']['open_contracts'], 0)
                self.assertEqual(s['exposure_seconds'], 3)
                self.assertEqual(D(s['max_inventory_cost_dollars']), D('.521'))
                self.assertEqual(s['excess_net_pnl'], s['strategy_net_pnl'])
            by_ticker = {s['ticker']: s for s in result['sessions']}
            self.assertEqual(D(by_ticker['SYNTHETIC-LOSS']['sampled_max_drawdown_before_exit_costs']), D('.521'))
            self.assertEqual(D(by_ticker['SYNTHETIC-WIN']['sampled_max_drawdown_before_exit_costs']), D('.031'))
            self.assertGreater(by_ticker['SYNTHETIC-WIN']['unknown_equity_observations'], 0)
            self.assertEqual(evaluate(paths, settings(), Experiment, synthetic=True)['evaluation_fingerprint'],
                             result['evaluation_fingerprint'])
            changed = settings()
            changed.slippage = .005
            self.assertNotEqual(evaluate(paths, changed, Experiment, synthetic=True)['evaluation_fingerprint'],
                                result['evaluation_fingerprint'])

    def test_pending_inventory_not_counted_as_zero_or_subset_profit(self):
        with tempfile.TemporaryDirectory() as tmp:
            win = self.fixture(tmp, 'settlement-demo.jsonl', 'WIN')
            pending = self.fixture(tmp, 'settlement-pending-demo.jsonl', 'PENDING')
            before = pending.read_bytes()
            result = evaluate([win, pending], settings(), Experiment)
            self.assertEqual(result['summary']['included_sessions'], 1)
            self.assertIsNone(result['summary']['whole_cohort_net_pnl'])
            s = result['sessions'][1]
            self.assertIsNone(s['strategy_net_pnl'])
            self.assertIn('unresolved_inventory', s['exclusion_reasons'])
            self.assertIn('final_payout_not_observed', s['exclusion_reasons'])
            self.assertEqual(pending.read_bytes(), before)

    def test_duplicates_and_conflicting_same_market_all_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = self.fixture(tmp, 'settlement-demo.jsonl', 'TEST')
            copy = Path(tmp)/'copy.jsonl'
            copy.write_bytes(a.read_bytes())
            r = evaluate([a, copy], settings(), Experiment)
            self.assertEqual(r['summary']['included_sessions'], 0)
            self.assertIn('duplicate_recording', r['sessions'][0]['exclusion_reasons'])
            self.assertIn('duplicate_recording', r['sessions'][1]['exclusion_reasons'])
            loss = self.fixture(tmp, 'settlement-loss-demo.jsonl', 'OTHER')
            loss.write_text(loss.read_text().replace('OTHER', 'TEST'))
            r = evaluate([a, loss], settings(), Experiment)
            self.assertEqual(r['summary']['included_sessions'], 0)
            self.assertEqual(r['summary']['exclusion_counts']['repeated_market'], 2)

    def test_missing_final_provisional_and_flat_incomplete_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            def provisional(rows):
                rows[-1]['market']['is_provisional'] = True
            path = self.fixture(tmp, 'settlement-demo.jsonl', 'PROVISIONAL', provisional)
            a = settings()
            a.threshold = 1  # No position: still requires observed final payout.
            r = evaluate([path], a, Experiment)
            self.assertEqual(r['sessions'][0]['strategy']['open_contracts'], 0)
            self.assertIn('final_payout_not_observed', r['sessions'][0]['exclusion_reasons'])
            self.assertIsNone(r['summary']['included_strategy_net_pnl'])

    def test_no_trade_signal_is_valid_zero_result(self):
        a = settings()
        a.threshold = 1
        r = evaluate([ROOT/'settlement-demo.jsonl'], a, Experiment, synthetic=True)
        self.assertEqual(r['summary']['included_sessions'], 1)
        self.assertEqual(D(r['summary']['included_excess_net_pnl']), 0)
        self.assertEqual(r['summary']['flat_sessions'], 1)
        self.assertEqual(r['summary']['included_fills'], 0)

    def test_clock_error_and_malformed_books_visible_as_exclusions(self):
        with tempfile.TemporaryDirectory() as tmp:
            def jump(rows):
                for r in rows:
                    if 'received_at' in r:
                        r['elapsed_s'] = r['received_at']
                rows[-1]['received_at'] += 20
            a = self.fixture(tmp, 'settlement-demo.jsonl', 'JUMP', jump)
            def invalid(rows):
                rows[2]['book']['orderbook_fp']['yes_dollars'] = [['NaN', '1']]
            b = self.fixture(tmp, 'settlement-demo.jsonl', 'INVALID', invalid)
            r = evaluate([a, b], settings(), Experiment)
            self.assertEqual(r['summary']['included_sessions'], 0)
            self.assertTrue(r['sessions'][0]['complete'])
            self.assertIn('clock_discontinuity', r['sessions'][0]['exclusion_reasons'])
            self.assertIn('invalid_recording', r['sessions'][1]['exclusion_reasons'])

    def test_finalization_only_is_not_a_trading_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.fixture(tmp, 'settlement-demo.jsonl', 'FINAL', lambda rows: rows.__delitem__(slice(1, -1)))
            r = evaluate([path], settings(), Experiment)
            self.assertIn('no_snapshots_during_observed_active_market', r['sessions'][0]['exclusion_reasons'])

    def test_errors_crossed_quotes_and_large_gaps_are_retained_but_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            def error(rows):
                rows.insert(3, {'type': 'error', 'at': 1, 'category': 'http_error'})
            def crossed(rows):
                rows[2]['book']['orderbook_fp']['yes_dollars'] = [['.6', '100']]
            def gap(rows):
                rows[3]['received_at'] = 20
                rows[4]['received_at'] = 21
                rows[5]['received_at'] = 22
            for transform, reason in [(error, 'recorded_data_errors'),
                                      (crossed, 'locked_or_crossed_book'),
                                      (gap, 'snapshot_gap_exceeds_max_gap')]:
                path = self.fixture(tmp, 'settlement-demo.jsonl', reason, transform)
                r = evaluate([path], settings(), Experiment)
                self.assertEqual(r['summary']['included_sessions'], 0)
                self.assertIn(reason, r['sessions'][0]['exclusion_reasons'])
                self.assertIn('strategy', r['sessions'][0])

    def test_synthetic_input_requires_label_and_events_after_footer_rejected(self):
        r = evaluate([ROOT/'settlement-demo.jsonl'], settings(), Experiment)
        self.assertIn('synthetic_input_requires_synthetic_flag', r['sessions'][0]['exclusion_reasons'])
        with tempfile.TemporaryDirectory() as tmp:
            def footer(rows):
                rows.insert(-1, {'type': 'recording_end', 'ticker': 'FOOTER', 'received_at': 4})
            path = self.fixture(tmp, 'settlement-demo.jsonl', 'FOOTER', footer)
            r = evaluate([path], settings(), Experiment)
            self.assertIn('invalid_recording', r['sessions'][0]['exclusion_reasons'])
            self.assertIn('Events after recording_end', r['sessions'][0]['error'])

    def test_cli_report_and_overwrite_protection(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)/'evaluation.json'
            cmd = [sys.executable, str(ROOT/'paper.py'), 'evaluate', '--files',
                   str(ROOT/'settlement-demo.jsonl'), '--fee-coefficient', '.07',
                   '--synthetic', '--report', str(target)]
            first = subprocess.run(cmd, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertIn('strategy $0.479', first.stdout)
            before = target.read_bytes()
            second = subprocess.run(cmd, capture_output=True, text=True)
            self.assertEqual(second.returncode, 2)
            self.assertEqual(target.read_bytes(), before)
            report = json.loads(before)
            self.assertEqual(report['dataset_kind'], 'synthetic_mechanics_demo')


class ThroughSettlementTests(unittest.TestCase):
    def args(self, path):
        return argparse.Namespace(ticker='TEST', series=None, file=str(path),
            interval=1, minutes=.01, until_settlement=True, max_wait_minutes=.2,
            min_remaining_seconds=0)

    def test_keeps_polling_closed_and_determined_until_final(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, clock = Path(tmp)/'session.jsonl', Clock()
            requests = []
            def get(endpoint, **params):
                requests.append((endpoint, clock.now))
                clock.now += .01
                if endpoint.endswith('/orderbook'):
                    return BOOK
                status = 'active' if clock.now < 3 else 'closed' if clock.now < 5 else 'determined' if clock.now < 7 else 'finalized'
                return {'market': market(status)}
            collect(self.args(path), get, clock, lambda *a, **k: None)
            q = quality(path)
            self.assertEqual(q['recording_stop_reason'], 'final_settlement')
            self.assertGreater(q['monotonic_recording_duration_seconds'], .6)
            self.assertIn('closed', q['market_status_counts'])
            self.assertIn('determined', q['market_status_counts'])
            self.assertTrue(all(t < 3 for p, t in requests if p.endswith('/orderbook')))
            r = evaluate([path], settings(), Experiment)
            self.assertEqual(r['summary']['included_sessions'], 1)
            self.assertEqual(D(r['summary']['included_strategy_net_pnl']), D('.479'))

    def test_timeout_and_interrupt_preserve_incomplete_recording(self):
        with tempfile.TemporaryDirectory() as tmp:
            for interrupt in (False, True):
                with self.subTest(interrupt=interrupt):
                    path, clock = Path(tmp)/f'{interrupt}.jsonl', Clock()
                    def get(endpoint, **params):
                        if interrupt and clock.now > 1:
                            raise KeyboardInterrupt()
                        return BOOK if endpoint.endswith('/orderbook') else {'market': market()}
                    collect(self.args(path), get, clock, lambda *a, **k: None)
                    self.assertEqual(quality(path)['recording_stop_reason'],
                                     'interrupted' if interrupt else 'settlement_timeout')
                    r = evaluate([path], settings(), Experiment)
                    self.assertIn('final_payout_not_observed', r['sessions'][0]['exclusion_reasons'])
                    self.assertIsNone(r['summary']['whole_cohort_net_pnl'])

    def test_invalid_time_cap_rejected_before_collection(self):
        for value in ('0', '-1', 'nan', 'inf'):
            result = subprocess.run([sys.executable, str(ROOT/'paper.py'), 'collect',
                '--series', 'TEST', '--until-settlement', '--max-wait-minutes', value],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn('max-wait-minutes', result.stderr)


if __name__ == '__main__':
    unittest.main()
