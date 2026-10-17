import argparse
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import patch

from fees import single_fill_fee, effective_taker_terms, inspect_fees
from paper import Experiment, main
from research import compare_reports, provenance, replay_recording
from test_paper import row

ROOT = Path(__file__).resolve().parent


def args(model='direct', cash=1000):
    return argparse.Namespace(cash=cash, max_gap=15, slippage=.001,
        fee_coefficient=.07, fee_quantum=.01, fee_model=model, hold=2,
        latency=1, threshold=.6, max_spread=.02)


class FeeTests(unittest.TestCase):
    def test_official_non_direct_buy_example(self):
        f = single_fill_fee('buy', D('.055'), D('.07'), 'non-direct')
        self.assertEqual(f, {'trade_fee': D('.003639'), 'rounding_fee': D('.001361'),
                             'rebate': D(0), 'net_fee': D('.005')})
        self.assertEqual(-D('.055')-f['net_fee'], D('-.06'))

    def test_direct_precision_and_directional_cash_alignment(self):
        self.assertEqual(single_fill_fee('buy', D('.055'), D('.07'), 'direct')['net_fee'], D('.0037'))
        buy = single_fill_fee('buy', D('.501'), D('.07'), 'non-direct')
        sell = single_fill_fee('sell', D('.501'), D('.07'), 'non-direct')
        self.assertEqual(buy['net_fee'], D('.019'))
        self.assertEqual(sell['net_fee'], D('.021'))
        self.assertEqual(D('.501')-sell['net_fee'], D('.48'))

    def test_grid_alignment_and_nonnegative_fees_across_prices(self):
        for model, grid in [('direct', D('.0001')), ('non-direct', D('.01'))]:
            for i in range(1, 1000):
                p = D(i)/1000
                for side, sign in [('buy', -1), ('sell', 1)]:
                    fee = single_fill_fee(side, p, D('.07'), model)
                    self.assertGreaterEqual(fee['net_fee'], 0)
                    self.assertEqual((sign*p-fee['net_fee']) % grid, 0)
                    self.assertLess(fee['rounding_fee'], grid)
                    self.assertEqual(fee['net_fee'], fee['trade_fee']+fee['rounding_fee'])

    def test_separate_orders_do_not_share_rounding_rebates(self):
        fees = [single_fill_fee('buy', D('.501'), D('.07'), 'non-direct') for _ in range(10)]
        self.assertEqual(sum(f['net_fee'] for f in fees), D('.190'))
        self.assertTrue(all(f['rebate'] == 0 for f in fees))
        self.assertEqual(single_fill_fee('buy', D('.005'), D(0), 'non-direct')['net_fee'], D('.005'))

    def test_invalid_fee_inputs_fail(self):
        for p, c in [('NaN', '.07'), ('1', '.07'), ('.5', '-1')]:
            with self.assertRaises(ValueError):
                single_fill_fee('buy', D(p), D(c), 'direct')
        with self.assertRaisesRegex(ValueError, 'six decimal'):
            single_fill_fee('buy', D('.1234567'), D('.07'), 'direct')
        with self.assertRaisesRegex(ValueError, 'precision'):
            Experiment(args('non-direct', cash=1000.001))

    def test_engine_cash_fee_journal_and_settlement_reconcile(self):
        for model, expected in [('legacy', D('999.968')), ('direct', D('999.9730')),
                                ('non-direct', D('999.970'))]:
            e = Experiment(args(model), output=lambda *a, **k: None)
            for t, bid, no in [(0, '.49', '.5'), (1, '.49', '.5'), (3, '.49', '.5'), (4, '.51', '.48')]:
                e.step(row(t, bid, no))
            self.assertEqual(e.cash, expected)
            r = e.report()
            self.assertEqual(len(r['fee_details']), 2)
            self.assertEqual(sum(D(f['net_fee']) for f in r['fee_details']), e.portfolio.fees)
            self.assertEqual(r['fee_assumptions']['model'], model)
        e = Experiment(args(), output=lambda *a, **k: None)
        replay_recording(ROOT/'settlement-demo.jsonl', e)
        self.assertEqual(e.cash, D('1000.4815'))
        self.assertEqual(e.portfolio.fees, D('.0175'))
        self.assertEqual(e.portfolio.quantity, 0)

    def test_provenance_distinguishes_rounding_models(self):
        a, b = provenance('same-data', vars(args('legacy'))), provenance('same-data', vars(args('direct')))
        self.assertNotEqual(a['experiment_fingerprint'], b['experiment_fingerprint'])
        self.assertIsNone(b['parameters']['fee_quantum'])
        self.assertIn('fees.py', b['source_sha256'])

    def test_cli_rejects_conflicting_rounding_and_preserves_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            common = [sys.executable, str(ROOT/'paper.py'), 'replay', '--file',
                      str(ROOT/'settlement-demo.jsonl'), '--fee-coefficient', '.07']
            target = Path(tmp)/'bad.json'
            r = subprocess.run(common+['--fee-model', 'direct', '--fee-quantum', '.01',
                                       '--report', str(target)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 2)
            self.assertFalse(target.exists())
            a, b = Path(tmp)/'default.json', Path(tmp)/'explicit.json'
            for path, extra in [(a, []), (b, ['--fee-model', 'legacy'])]:
                r = subprocess.run(common+['--report', str(path)]+extra, capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(compare_reports(json.loads(a.read_text()), json.loads(b.read_text()))['reproduced'])
            self.assertEqual(json.loads(a.read_text())['cash'], 1000.479)


class FeeMetadataTests(unittest.TestCase):
    def getter(self, endpoint, **params):
        if endpoint == '/markets/TEST-1':
            return {'market': {'ticker': 'TEST-1', 'event_ticker': 'EVENT'}}
        if endpoint == '/events/EVENT':
            return {'event': {'event_ticker': 'EVENT', 'series_ticker': 'SERIES',
                             'fee_multiplier_override': .5}}
        if endpoint == '/events/fee_changes':
            if 'cursor' not in params:
                return {'event_fee_changes': [{'event_ticker': 'EVENT', 'scheduled_ts': '2030-01-01T00:00:00Z'}], 'cursor': 'next'}
            return {'event_fee_changes': [], 'cursor': ''}
        if endpoint == '/series/SERIES':
            return {'series': {'ticker': 'SERIES', 'fee_type': 'quadratic', 'fee_multiplier': 1}}
        if endpoint == '/series/fee_changes':
            self.assertEqual(params, {'series_ticker': 'SERIES', 'show_historical': 'true'})
            return {'series_fee_change_arr': []}
        self.fail('Unexpected endpoint '+endpoint)

    def test_event_override_and_paginated_changes_with_evidence(self):
        r = inspect_fees(self.getter, ticker='TEST-1', now=datetime(2026, 10, 17, tzinfo=timezone.utc))
        self.assertEqual(r['taker_coefficient_before_waivers'], '0.035')
        self.assertTrue(r['event_override_present'])
        self.assertEqual(len(r['event_fee_changes']), 1)
        self.assertEqual(len(r['evidence']), 6)
        self.assertTrue(all(len(e['response_sha256']) == 64 for e in r['evidence']))
        self.assertEqual(r['scope'], 'market_current_metadata')

    def test_series_only_does_not_claim_event_verification(self):
        r = inspect_fees(self.getter, series_ticker='SERIES')
        self.assertEqual(r['scope'], 'series_only_event_overrides_unchecked')
        self.assertEqual(r['taker_coefficient_before_waivers'], '0.07')

    def test_zero_override_null_fallback_and_unknown_terms(self):
        series = {'fee_type': 'quadratic', 'fee_multiplier': 1}
        self.assertEqual(effective_taker_terms(series, {'fee_multiplier_override': 0})['taker_coefficient_before_waivers'], '0.00')
        self.assertEqual(effective_taker_terms(series, {'fee_multiplier_override': None})['taker_coefficient_before_waivers'], '0.07')
        for bad in [{'fee_type': 'flat', 'fee_multiplier': 1}, {'fee_type': 'quadratic'},
                    {'fee_type': 'quadratic', 'fee_multiplier': 'NaN'},
                    {'fee_type': 'quadratic', 'fee_multiplier': -1}]:
            with self.assertRaises(ValueError):
                effective_taker_terms(bad)

    def test_mismatched_tickers_and_repeated_cursor_rejected(self):
        def wrong(path, **params):
            result = self.getter(path, **params)
            if path == '/events/EVENT':
                result['event']['event_ticker'] = 'OTHER'
            return result
        with self.assertRaisesRegex(ValueError, 'mismatch'):
            inspect_fees(wrong, ticker='TEST-1')
        def repeated(path, **params):
            if path == '/events/fee_changes':
                return {'event_fee_changes': [], 'cursor': 'again'}
            return self.getter(path, **params)
        with self.assertRaisesRegex(ValueError, 'cursor'):
            inspect_fees(repeated, ticker='TEST-1')

    def test_cli_evidence_file_exclusive_and_no_network_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp)/'fee.json'
            with patch('sys.argv', ['paper.py', 'fees', '--series', 'SERIES', '--report', str(report)]), \
                    patch('paper.get', side_effect=self.getter), patch('builtins.print'):
                main()
            self.assertEqual(json.loads(report.read_text())['fee_multiplier'], '1')
            before = report.read_bytes()
            with patch('sys.argv', ['paper.py', 'fees', '--series', 'SERIES', '--report', str(report)]), \
                    patch('paper.get') as getter:
                with self.assertRaises(SystemExit):
                    main()
                getter.assert_not_called()
            self.assertEqual(report.read_bytes(), before)
            with patch('paper.get', side_effect=AssertionError('Replay must stay offline')):
                e = Experiment(args(), output=lambda *a, **k: None)
                replay_recording(ROOT/'settlement-demo.jsonl', e)


if __name__ == '__main__':
    unittest.main()
