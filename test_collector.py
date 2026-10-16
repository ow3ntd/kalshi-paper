import argparse
import json
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stderr
from io import StringIO
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from collector import collect, quality, quotes, choose_market
from research import replay_recording
import test_paper

class Clock:
    def __init__(self):
        self.now = 0
    def monotonic(self):
        return self.now
    def time(self):
        return 1000+self.now
    def sleep(self, seconds):
        self.now += seconds

def market(status='active'):
    return {'ticker': 'TEST', 'market_type': 'binary', 'status': status,
            'open_time': '1970-01-01T00:00:00Z',
            'close_time': '1970-01-01T00:33:20Z',
            **({'result': 'yes'} if status == 'finalized' else {})}

BOOK = {'orderbook_fp': {'yes_dollars': [['0.49', '100']], 'no_dollars': [['0.50', '10']]}}

class CollectorTests(unittest.TestCase):
    def args(self, path):
        return argparse.Namespace(ticker='TEST', series=None, file=str(path), minutes=.1, interval=2, min_remaining_seconds=300)

    def getter(self, clock):
        def get(path, **params):
            clock.now += .05
            return BOOK if path.endswith('/orderbook') else {'market': market()}
        return get

    def test_collection_flushes_replayable_events_and_quality(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, clock = Path(tmp)/'data.jsonl', Clock()
            collect(self.args(path), self.getter(clock), clock, lambda *a, **k: None)
            summary = quality(path)
            self.assertEqual(summary['snapshots'], 3)
            self.assertEqual(summary['errors'], 0)
            self.assertEqual(summary['request_duration_ms_p95'], 50)
            self.assertEqual(summary['usable_price_snapshots'], 3)
            self.assertAlmostEqual(summary['max_snapshot_gap_seconds'], 2)
            engine = test_paper.Tests().engine()
            replay_recording(path, engine)
            self.assertTrue(engine.report()['lifecycle_tracking'])

    def test_rate_limit_backoff_then_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, clock = Path(tmp)/'data.jsonl', Clock()
            normal, calls = self.getter(clock), []
            def get(endpoint, **params):
                calls.append(clock.monotonic())
                if len(calls) == 1:
                    raise HTTPError(endpoint, 429, 'rate limited', {}, None)
                return normal(endpoint, **params)
            collect(self.args(path), get, clock, lambda *a, **k: None)
            self.assertGreaterEqual(calls[1]-calls[0], 4)
            self.assertEqual(quality(path)['errors'], 1)
            self.assertEqual(quality(path)['snapshots'], 1)

    def test_invalid_raw_book_is_preserved_as_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, clock = Path(tmp)/'data.jsonl', Clock()
            normal = self.getter(clock)
            def get(endpoint, **params):
                return {'bad': 'schema'} if endpoint.endswith('/orderbook') else normal(endpoint, **params)
            collect(self.args(path), get, clock, lambda *a, **k: None)
            summary = quality(path)
            self.assertEqual(summary['snapshots'], 0)
            self.assertGreater(summary['error_categories']['invalid_book'], 0)
            self.assertIn('raw_response', path.read_text())

    def test_finalization_stops_collection(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, clock = Path(tmp)/'data.jsonl', Clock()
            def get(endpoint, **params):
                self.assertFalse(endpoint.endswith('/orderbook'))
                return {'market': market('finalized')}
            collect(self.args(path), get, clock, lambda *a, **k: None)
            self.assertEqual(quality(path)['market_status_counts'], {'finalized': 1})
            self.assertEqual(clock.now, 0)

    def test_ctrl_c_preserves_recording(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, clock = Path(tmp)/'data.jsonl', Clock()
            def get(endpoint, **params):
                raise KeyboardInterrupt()
            collect(self.args(path), get, clock, lambda *a, **k: None)
            self.assertTrue(path.exists())
            self.assertEqual(quality(path)['snapshots'], 0)

    def test_existing_recording_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'data.jsonl'
            path.write_text('preserve')
            with self.assertRaises(FileExistsError):
                collect(self.args(path), lambda *a, **k: self.fail('Unexpected HTTP request'))
            self.assertEqual(path.read_text(), 'preserve')

    def test_paginated_series_selection(self):
        def get(endpoint, **params):
            if 'cursor' not in params:
                return {'markets': [], 'cursor': 'page2'}
            return {'markets': [market()], 'cursor': ''}
        self.assertEqual(choose_market(get, 'SERIES', 1000), 'TEST')

    def test_selection_lifetime_boundary_and_pagination(self):
        def candidate(ticker, remaining, **extra):
            return {**market(), 'ticker': ticker,
                    'close_time': datetime.fromtimestamp(1000+remaining, timezone.utc).isoformat(), **extra}
        pages = [
            {'markets': [candidate('TOO-SOON', 299)], 'cursor': 'next'},
            {'markets': [candidate('LATER', 600), candidate('BOUNDARY', 300),
                         candidate('EXPIRES', 400, expiration_time='1970-01-01T00:20:00Z'),
                         candidate('FUTURE', 350, open_time='1970-01-01T00:20:00Z'),
                         candidate('CLOSED', 350, status='closed'),
                         candidate('NONBINARY', 350, market_type='other')]}]
        calls = []
        def get(endpoint, **params):
            calls.append(params)
            return pages[len(calls)-1]
        self.assertEqual(choose_market(get, 'SERIES', 1000), 'BOUNDARY')
        self.assertEqual(calls[1]['cursor'], 'next')
        get = lambda *a, **k: {'markets': [candidate('SHORT', 30), candidate('EXPIRED', 0)]}
        self.assertEqual(choose_market(get, 'SERIES', 1000, 0), 'SHORT')
        self.assertEqual(choose_market(get, 'SERIES', 1000, 30), 'SHORT')
        with self.assertRaisesRegex(ValueError, 'SERIES.*60 seconds remaining'):
            choose_market(get, 'SERIES', 1000, 60)

    def test_no_eligible_market_creates_no_recording(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'data.jsonl'
            args = self.args(path)
            args.ticker, args.series = None, 'SERIES'
            with self.assertRaisesRegex(ValueError, 'No eligible market.*300 seconds'):
                collect(args, lambda *a, **k: {'markets': []}, Clock())
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_cli_minimum_default_override_and_validation(self):
        from paper import main
        for option, expected in [([], 300), (['--min-remaining-seconds', '42'], 42)]:
            with patch('sys.argv', ['paper.py', 'collect', '--series', 'SERIES', *option]), patch('paper.collect') as run:
                main()
                self.assertEqual(run.call_args.args[0].min_remaining_seconds, expected)
        for value in ('-1', 'nan', 'inf'):
            with self.subTest(value=value), patch('sys.argv', ['paper.py', 'collect', '--series', 'SERIES', '--min-remaining-seconds', value]), patch('paper.collect') as run, redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main()
                self.assertEqual(error.exception.code, 2)
                run.assert_not_called()

    def test_quality_book_counts_and_legacy_recording_preserved(self):
        books = [([], []), ([['.5', '0']], []),
                 ([['.5', '1']], []), ([], [['.4', '1']]),
                 ([['.4', '1']], [['.5', '1']]),
                 ([['.6', '1']], [['.5', '1']])]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'legacy.jsonl'
            path.write_text(''.join(json.dumps({'type': 'snapshot', 'ticker': 'TEST', 'at': t,
                'book': {'orderbook_fp': {'yes_dollars': yes, 'no_dollars': no}}})+'\n'
                for t, (yes, no) in enumerate(books)))
            before = path.read_bytes()
            result = quality(path)
            self.assertEqual(result['empty_snapshots'], 2)
            self.assertEqual(result['one_sided_snapshots'], 2)
            self.assertEqual(result['two_sided_snapshots'], 2)
            self.assertEqual(result['locked_or_crossed_snapshots'], 1)
            self.assertEqual(result['usable_price_snapshots'], 1)
            self.assertEqual(result['snapshots'], 6)
            self.assertEqual(path.read_bytes(), before)

    def test_one_sided_book_is_not_fabricated_quote(self):
        self.assertEqual(quotes({'orderbook_fp': {'yes_dollars': [['.5', '1']], 'no_dollars': []}})[1], None)

    def test_nonfinite_price_rejected(self):
        with self.assertRaises(ValueError):
            quotes({'orderbook_fp': {'yes_dollars': [['NaN', '1']], 'no_dollars': []}})

    def test_quality_rejects_out_of_order_recording(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'data.jsonl'
            path.write_text('\n'.join(json.dumps({'type': 'error', 'at': t}) for t in [2, 1]))
            with self.assertRaisesRegex(ValueError, 'line 2:'):
                quality(path)

if __name__ == '__main__':
    unittest.main()
