import json
import tempfile
import unittest
from pathlib import Path
from collector import quality, collect
from research import replay_recording
import test_collector
import test_paper

class TimingTests(unittest.TestCase):
    def summarize(self, rows):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'timing.jsonl'
            original = ''.join(json.dumps(r)+'\n' for r in rows).encode()
            path.write_bytes(original)
            report = quality(path)
            self.assertEqual(path.read_bytes(), original)
            return report

    def test_forward_wall_jump_separated_from_elapsed(self):
        result = self.summarize([
            {'type': 'error', 'at': 1000, 'elapsed_s': 0, 'sequence': 1},
            {'type': 'error', 'at': 1027, 'elapsed_s': 2, 'sequence': 2}])
        self.assertEqual(result['wall_clock_observation_span_seconds'], 27)
        self.assertEqual(result['monotonic_observation_span_seconds'], 2)
        self.assertEqual(result['wall_minus_monotonic_span_seconds'], 25)
        self.assertEqual(result['wall_clock_step_count'], 1)
        self.assertIsNone(result['monotonic_recording_duration_seconds'])

    def test_backward_wall_jump_can_be_diagnosed(self):
        result = self.summarize([
            {'type': 'error', 'at': 1000, 'elapsed_s': 0},
            {'type': 'error', 'at': 990, 'elapsed_s': 2}])
        self.assertEqual(result['wall_clock_steps'][0]['difference_seconds'], -12)

    def test_legacy_recording_does_not_invent_monotonic_duration(self):
        result = self.summarize([{'type': 'error', 'at': 1000}, {'type': 'error', 'at': 1002}])
        self.assertIsNone(result['monotonic_observation_span_seconds'])
        self.assertIsNone(result['monotonic_recording_duration_seconds'])

    def test_decreasing_monotonic_time_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'decreasing monotonic'):
            self.summarize([{'type': 'error', 'at': 1000, 'elapsed_s': 2},
                            {'type': 'error', 'at': 1002, 'elapsed_s': 1}])

    def test_footer_records_full_duration_not_just_observation_span(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, clock = Path(tmp)/'data.jsonl', test_collector.Clock()
            helper = test_collector.CollectorTests()
            collect(helper.args(path), helper.getter(clock), clock, lambda *a, **k: None)
            result = quality(path)
            self.assertEqual(result['monotonic_recording_duration_seconds'], 6)
            self.assertAlmostEqual(result['monotonic_observation_span_seconds'], 4.05)
            self.assertEqual(result['recording_stop_reason'], 'duration_limit')
            self.assertTrue(result['recording_end_observed'])
            replay_recording(path, test_paper.Tests().engine())

    def test_replay_refuses_wall_jump_instead_of_distorting_holding_period(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'jump.jsonl'
            path.write_text(''.join(json.dumps(r)+'\n' for r in [
                {'type': 'error', 'at': 1000, 'elapsed_s': 0},
                {'type': 'error', 'at': 1027, 'elapsed_s': 2}]))
            with self.assertRaisesRegex(ValueError, 'Wall-clock discontinuity'):
                replay_recording(path, test_paper.Tests().engine())

if __name__ == '__main__':
    unittest.main()
