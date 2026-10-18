import argparse
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from batch import collect_batch, batch_inputs
from collector import quality
from evaluation import evaluate
from paper import Experiment
from test_collector import Clock, BOOK
from test_fees import args as strategy_args

ROOT = Path(__file__).resolve().parent


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def options(path, **kw):
    values = dict(series='SERIES', sessions=2, max_minutes=.5, session_max_minutes=.2,
                  retry_seconds=1, interval=1, min_remaining_seconds=0, directory=str(path))
    return argparse.Namespace(**(values | kw))


class Exchange:
    """Two windows: closes at 4/11s, final payouts at 6/13s; a stale duplicate is listed."""
    def __init__(self, clock):
        self.clock = clock
        self.calls = []

    def market(self, ticker, listing=False):
        offset = 0 if ticker == 'M1' else 7
        t = self.clock.now
        status = 'active' if t < offset+4 else 'closed' if t < offset+6 else 'finalized'
        return {'ticker': ticker, 'event_ticker': 'E'+ticker[-1], 'market_type': 'binary',
                'status': 'active' if listing else status,
                'open_time': iso(1000+offset),
                'close_time': iso(2000 if listing and ticker == 'M1' else 1000+offset+4),
                **({'result': 'yes' if ticker == 'M1' else 'no'} if status == 'finalized' else {})}

    def get(self, endpoint, **params):
        self.calls.append((endpoint, self.clock.now))
        self.clock.now += .01
        if endpoint == '/markets':
            return {'markets': [self.market(t, listing=True) for t in ['M1', 'M2']]}
        if endpoint.endswith('/orderbook'):
            return BOOK
        if endpoint.startswith('/markets/'):
            return {'market': self.market(endpoint.split('/')[-1])}
        if endpoint == '/series/SERIES':
            return {'series': {'ticker': 'SERIES', 'fee_type': 'quadratic', 'fee_multiplier': 1}}
        if endpoint == '/series/fee_changes':
            return {'series_fee_change_arr': []}
        if endpoint == '/events/fee_changes':
            return {'event_fee_changes': []}
        if endpoint.startswith('/events/'):
            return {'event': {'event_ticker': endpoint.split('/')[-1], 'series_ticker': 'SERIES'}}
        raise AssertionError(endpoint)


class BatchTests(unittest.TestCase):
    def run_batch(self, root, clock, getter, **overrides):
        path = collect_batch(options(root, **overrides), getter, clock, lambda *a, **k: None)
        return json.loads((path/'batch.json').read_text())

    def test_rollover_distinct_markets_fee_evidence_and_evaluation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, clock = Path(tmp)/'batch', Clock()
            state = self.run_batch(root, clock, Exchange(clock).get)
            self.assertEqual(state['stop_reason'], 'session_limit')
            self.assertEqual(state['attempted_sessions'], 2)
            self.assertEqual(state['settled_sessions'], 2)
            self.assertEqual([s['ticker'] for s in state['sessions']], ['M1', 'M2'])
            self.assertGreaterEqual(state['selection_waits'], 1)
            self.assertTrue(all((root/s['fee_report']).exists() for s in state['sessions']))
            paths, context = batch_inputs(root)
            r = evaluate(paths, strategy_args(), Experiment, batch_context=context)
            self.assertEqual(r['summary']['included_sessions'], 2)
            self.assertIsNotNone(r['summary']['whole_cohort_net_pnl'])
            self.assertEqual(r['batch']['attempted_sessions'], 2)
            self.assertEqual(len(r['batch']['manifest_sha256']), 64)

    def test_timeout_counts_as_attempt_and_never_reselects_same_market(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, clock = Path(tmp)/'batch', Clock()
            state = self.run_batch(root, clock, Exchange(clock).get, session_max_minutes=.04)
            self.assertEqual(state['attempted_sessions'], 2)
            self.assertEqual(state['settled_sessions'], 0)
            self.assertEqual([s['ticker'] for s in state['sessions']], ['M1', 'M2'])
            self.assertTrue(all(s['status'] == 'settlement_timeout' for s in state['sessions']))
            paths, context = batch_inputs(root)
            r = evaluate(paths, strategy_args(), Experiment, batch_context=context)
            self.assertEqual(r['summary']['submitted_sessions'], 2)
            self.assertEqual(r['summary']['excluded_sessions'], 2)
            self.assertIsNone(r['summary']['whole_cohort_net_pnl'])

    def test_global_limit_clips_recording_and_discovery_sleep(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()
            root = Path(tmp)/'active'
            state = self.run_batch(root, clock, Exchange(clock).get, max_minutes=.05)
            self.assertEqual(state['stop_reason'], 'batch_time_limit')
            self.assertAlmostEqual(clock.now, 3)
            self.assertEqual(state['sessions'][0]['status'], 'batch_time_limit')
            self.assertEqual(quality(root/state['sessions'][0]['recording'])['recording_stop_reason'], 'batch_time_limit')
            clock = Clock()
            state = self.run_batch(Path(tmp)/'empty', clock, lambda *a, **k: {'markets': []},
                                   max_minutes=.05, retry_seconds=15)
            self.assertEqual(state['attempted_sessions'], 0)
            self.assertEqual(state['stop_reason'], 'batch_time_limit')
            self.assertEqual(clock.now, 3)

    def test_deadline_during_fee_check_preserves_missing_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, clock = Path(tmp)/'batch', Clock()
            exchange = Exchange(clock)
            def slow(endpoint, **params):
                result = exchange.get(endpoint, **params)
                if endpoint == '/events/E1':
                    clock.now += 4
                return result
            state = self.run_batch(root, clock, slow, max_minutes=.05)
            self.assertEqual(state['stop_reason'], 'batch_time_limit')
            self.assertEqual(state['attempted_sessions'], 1)
            paths, context = batch_inputs(root)
            r = evaluate(paths, strategy_args(), Experiment, batch_context=context)
            self.assertEqual(r['summary']['submitted_sessions'], 1)
            self.assertIn('invalid_recording', r['sessions'][0]['exclusion_reasons'])
            self.assertIsNone(r['summary']['whole_cohort_net_pnl'])

    def test_interrupt_during_recording_stops_entire_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, clock = Path(tmp)/'batch', Clock()
            exchange = Exchange(clock)
            def get(endpoint, **params):
                if endpoint.endswith('/orderbook') and clock.now > 1:
                    raise KeyboardInterrupt()
                return exchange.get(endpoint, **params)
            state = self.run_batch(root, clock, get)
            self.assertEqual(state['stop_reason'], 'interrupted')
            self.assertEqual(state['attempted_sessions'], 1)
            self.assertEqual(state['sessions'][0]['status'], 'interrupted')
            self.assertTrue((root/state['sessions'][0]['recording']).exists())

    def test_interrupt_while_waiting_preserves_zero_session_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock, root = Clock(), Path(tmp)/'batch'
            def get(*a, **k):
                raise KeyboardInterrupt()
            state = self.run_batch(root, clock, get)
            self.assertEqual(state['stop_reason'], 'interrupted')
            self.assertEqual(state['attempted_sessions'], 0)
            paths, context = batch_inputs(root)
            self.assertEqual(paths, [])

    def test_discovery_error_backoff_then_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, clock = Path(tmp)/'batch', Clock()
            exchange, times = Exchange(clock), []
            def get(endpoint, **params):
                if endpoint == '/markets':
                    times.append(clock.now)
                    if len(times) == 1:
                        raise OSError('temporary network failure')
                return exchange.get(endpoint, **params)
            state = self.run_batch(root, clock, get)
            self.assertEqual(state['selection_errors'], 1)
            self.assertGreaterEqual(times[1]-times[0], 1)
            self.assertEqual(state['settled_sessions'], 2)

    def test_fee_lookup_failure_does_not_discard_recording(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, clock = Path(tmp)/'batch', Clock()
            exchange = Exchange(clock)
            def get(endpoint, **params):
                if endpoint.startswith('/events/'):
                    raise OSError('fee endpoint unavailable')
                return exchange.get(endpoint, **params)
            state = self.run_batch(root, clock, get, sessions=1)
            self.assertEqual(state['settled_sessions'], 1)
            self.assertIn('unavailable', state['sessions'][0]['fee_check_error'])
            paths, context = batch_inputs(root)
            self.assertEqual(context['fee_check_failures'], 1)
            self.assertTrue(paths[0].exists())

    def test_storage_error_stops_and_missing_session_stays_in_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, clock = Path(tmp)/'batch', Clock()
            with patch('batch.collect', side_effect=OSError('disk full')):
                with self.assertRaisesRegex(OSError, 'disk full'):
                    self.run_batch(root, clock, Exchange(clock).get)
            state = json.loads((root/'batch.json').read_text())
            self.assertEqual(state['stop_reason'], 'storage_error')
            paths, _ = batch_inputs(root)
            self.assertEqual(len(paths), 1)
            self.assertFalse(paths[0].exists())

    def test_new_directory_required_and_invalid_args_create_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, clock = Path(tmp)/'batch', Clock()
            root.mkdir()
            (root/'keep').write_text('preserve')
            with self.assertRaises(FileExistsError):
                self.run_batch(root, clock, lambda *a, **k: self.fail('Unexpected HTTP'))
            self.assertEqual((root/'keep').read_text(), 'preserve')
            for kw in [dict(sessions=0), dict(max_minutes=float('nan')), dict(session_max_minutes=-1),
                       dict(retry_seconds=0), dict(interval=.5), dict(min_remaining_seconds=-1)]:
                with self.assertRaises(ValueError):
                    self.run_batch(Path(tmp)/'invalid', Clock(), lambda *a, **k: self.fail(), **kw)
                self.assertFalse((Path(tmp)/'invalid').exists())

    def test_manifest_running_or_escaping_path_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, clock = Path(tmp)/'batch', Clock()
            state = self.run_batch(root, clock, Exchange(clock).get, sessions=1)
            state['status'] = 'running'
            (root/'batch.json').write_text(json.dumps(state))
            with self.assertRaisesRegex(ValueError, 'not stopped'):
                batch_inputs(root)
            state['status'] = 'stopped'
            state['sessions'][0]['recording'] = '../outside.jsonl'
            (root/'batch.json').write_text(json.dumps(state))
            with self.assertRaisesRegex(ValueError, 'escapes'):
                batch_inputs(root)

    def test_cli_evaluates_manifest_and_keeps_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, clock = Path(tmp)/'batch', Clock()
            self.run_batch(root, clock, Exchange(clock).get)
            (root/'sessions/0002.jsonl').unlink()
            report = Path(tmp)/'evaluation.json'
            r = subprocess.run([sys.executable, str(ROOT/'paper.py'), 'evaluate', '--batch', str(root),
                '--fee-coefficient', '.07', '--fee-model', 'direct', '--report', str(report)],
                capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            saved = json.loads(report.read_text())
            self.assertEqual(saved['summary']['submitted_sessions'], 2)
            self.assertEqual(saved['summary']['included_sessions'], 1)
            self.assertIsNone(saved['summary']['whole_cohort_net_pnl'])
            self.assertIn('invalid_recording', saved['sessions'][1]['exclusion_reasons'])


if __name__ == '__main__':
    unittest.main()
