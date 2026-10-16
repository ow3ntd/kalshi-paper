import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from research import compare_reports, digest_file, replay_recording
import test_paper

ROOT = Path(__file__).resolve().parent

class ResearchTests(unittest.TestCase):
    def replay(self, report, *extra):
        return subprocess.run([sys.executable, str(ROOT/'paper.py'), 'replay',
            '--file', str(ROOT/'settlement-demo.jsonl'), '--fee-coefficient', '.07',
            '--report', str(report), *extra], capture_output=True, text=True)

    def test_two_cli_runs_reproduce_without_modifying_data(self):
        before = digest_file(ROOT/'settlement-demo.jsonl')
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp)/'a.json', Path(tmp)/'b.json'
            self.assertEqual(self.replay(a).returncode, 0)
            self.assertEqual(self.replay(b).returncode, 0)
            comparison = compare_reports(json.loads(a.read_text()), json.loads(b.read_text()))
            self.assertTrue(comparison['reproduced'])
            self.assertEqual(comparison['changed_result_fields'], [])
        self.assertEqual(before, digest_file(ROOT/'settlement-demo.jsonl'))

    def test_changed_assumption_is_different_experiment(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp)/'a.json', Path(tmp)/'b.json'
            self.assertEqual(self.replay(a).returncode, 0)
            self.assertEqual(self.replay(b, '--slippage', '.005').returncode, 0)
            result = compare_reports(json.loads(a.read_text()), json.loads(b.read_text()))
            self.assertTrue(result['same_data'])
            self.assertFalse(result['same_parameters'])
            self.assertFalse(result['same_results'])
            self.assertFalse(result['reproduced'])

    def test_existing_report_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)/'existing.json'
            target.write_text('preserve me')
            result = self.replay(target)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Report already exists', result.stderr)
            self.assertEqual(target.read_text(), 'preserve me')

    def test_bad_order_reports_line_number(self):
        rows = [json.loads(line) for line in (ROOT/'settlement-demo.jsonl').read_text().splitlines()]
        rows[-1]['received_at'] = 1
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'bad.jsonl'
            path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
            with self.assertRaisesRegex(ValueError, 'line 6: Events are out of chronological order'):
                replay_recording(path, test_paper.Tests().engine())

    def test_mixed_tickers_rejected(self):
        rows = [json.loads(line) for line in (ROOT/'settlement-demo.jsonl').read_text().splitlines()]
        rows[2]['ticker'] = 'OTHER'
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'mixed.jsonl'
            path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
            with self.assertRaisesRegex(ValueError, 'line 3: Replay cannot mix tickers'):
                replay_recording(path, test_paper.Tests().engine())

    def test_corrupt_recording_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'corrupt.jsonl'
            path.write_text('{not json}\n')
            with self.assertRaisesRegex(ValueError, 'line 1:'):
                replay_recording(path, test_paper.Tests().engine())

if __name__ == '__main__':
    unittest.main()
