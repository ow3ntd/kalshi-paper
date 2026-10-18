import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError

from collector import collect, quality, retry_delay
from batch import collect_batch
from test_collector import Clock, BOOK, market
import test_collector
from test_batch import options, Exchange


class SuspendClock(Clock):
    def __init__(self, jump=3600):
        super().__init__()
        self.offset = 0
        self.jump = jump

    def time(self):
        return super().time()+self.offset

    def sleep(self, seconds):
        super().sleep(seconds)
        self.offset += self.jump


class ResilienceTests(unittest.TestCase):
    def test_sleep_or_backward_clock_stops_recording_before_another_request(self):
        for jump in (3600, -120):
            with self.subTest(jump=jump), tempfile.TemporaryDirectory() as tmp:
                path, clock, calls = Path(tmp)/'data.jsonl', SuspendClock(jump), []
                def get(endpoint, **params):
                    calls.append(endpoint)
                    return BOOK if endpoint.endswith('/orderbook') else {'market': market()}
                collect(test_collector.CollectorTests().args(path), get, clock, lambda *a, **k: None)
                q = quality(path)
                self.assertEqual(len(calls), 2)
                self.assertEqual(q['recording_stop_reason'], 'clock_discontinuity')
                self.assertEqual(q['error_categories']['clock_discontinuity'], 1)
                self.assertTrue(q['recording_end_observed'])

    def test_sleep_during_request_preserves_response_and_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, clock = Path(tmp)/'data.jsonl', SuspendClock()
            def get(endpoint, **params):
                clock.offset += 3600
                return {'market': market()}
            collect(test_collector.CollectorTests().args(path), get, clock, lambda *a, **k: None)
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(rows[1]['type'], 'market')
            self.assertEqual(rows[-1]['reason'], 'clock_discontinuity')

    def test_batch_stops_on_sleep_during_recording_or_discovery_wait(self):
        for waiting in (False, True):
            with self.subTest(waiting=waiting), tempfile.TemporaryDirectory() as tmp:
                root, clock = Path(tmp)/'batch', SuspendClock()
                exchange = Exchange(clock)
                getter = (lambda *a, **k: {'markets': []}) if waiting else exchange.get
                collect_batch(options(root), getter, clock, lambda *a, **k: None)
                state = json.loads((root/'batch.json').read_text())
                self.assertEqual(state['stop_reason'], 'clock_discontinuity')
                self.assertEqual(state['attempted_sessions'], 0 if waiting else 1)
                self.assertEqual(state['settled_sessions'], 0)

    def test_discovery_response_after_sleep_does_not_select_market(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, clock = Path(tmp)/'batch', SuspendClock()
            exchange = Exchange(clock)
            def get(endpoint, **params):
                result = exchange.get(endpoint, **params)
                clock.offset += 3600
                return result
            collect_batch(options(root), get, clock, lambda *a, **k: None)
            state = json.loads((root/'batch.json').read_text())
            self.assertEqual(state['stop_reason'], 'clock_discontinuity')
            self.assertEqual(state['attempted_sessions'], 0)

    def test_retry_after_formats_and_invalid_values(self):
        for value, expected in [('45', 45), ('Thu, 01 Jan 1970 00:17:25 GMT', 45),
                                ('garbage', 30), ('nan', 30), ('-9', 30)]:
            error = HTTPError('test', 429, 'limited', {'Retry-After': value}, None)
            self.assertEqual(retry_delay(error, 4, 1000), expected)

    def test_retry_after_starts_at_response_and_deadline_clips_wait(self):
        for limit, expected_calls in [(60, 2), (20, 1)]:
            with self.subTest(limit=limit), tempfile.TemporaryDirectory() as tmp:
                path, clock, calls = Path(tmp)/'data.jsonl', Clock(), []
                def get(endpoint, **params):
                    calls.append(clock.now)
                    if len(calls) == 1:
                        clock.now += 10
                        raise HTTPError(endpoint, 429, 'limited', {'Retry-After': '45'}, None)
                    return {'market': market('finalized')}
                args = test_collector.CollectorTests().args(path)
                args.minutes = limit/60
                collect(args, get, clock, lambda *a, **k: None)
                self.assertEqual(len(calls), expected_calls)
                if expected_calls == 2:
                    self.assertEqual(calls[1], 55)
                else:
                    self.assertEqual(clock.now, limit)


if __name__ == '__main__':
    unittest.main()
