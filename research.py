"""Replay validation, experiment provenance, and comparison of saved results."""
import hashlib
import json
import math
import platform
from clocks import EventClock
from decimal import getcontext
from pathlib import Path

PARAMETERS = ('cash', 'hold', 'latency', 'max_gap', 'threshold', 'max_spread',
              'slippage', 'fee_coefficient', 'fee_quantum', 'fee_model')
SOURCES = ('paper.py', 'portfolio.py', 'lifecycle.py', 'research.py', 'collector.py', 'clocks.py', 'evaluation.py', 'fees.py')

def normalized_parameters(settings):
    result = {k: settings[k] for k in PARAMETERS if k != 'fee_model'}
    result['fee_model'] = settings.get('fee_model', 'legacy')
    if result['fee_model'] != 'legacy':
        result['fee_quantum'] = None  # Not used by the cash-alignment models.
    return result

def digest_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()

def replay_recording(path: Path, engine) -> str:
    """Hash the exact bytes consumed. Reject bad event order with a line number."""
    digest = hashlib.sha256()
    previous = -math.inf
    ticker = None
    previous_elapsed = None
    clock = EventClock()
    with path.open('rb') as f:
        for number, raw in enumerate(f, 1):
            digest.update(raw)
            try:
                row = json.loads(raw)
                kind = row.get('type')
                if kind == 'metadata':
                    continue
                if kind not in ('snapshot', 'market', 'error', 'recording_end'):
                    raise ValueError('Unknown event type')
                observed = row.get('received_at', row.get('at'))
                if not isinstance(observed, (int, float)) or not math.isfinite(observed):
                    raise ValueError('Missing or invalid observation time')
                if observed < previous and row.get('elapsed_s') is None:
                    raise ValueError('Events are out of chronological order')
                elapsed = row.get('elapsed_s')
                if elapsed is not None:
                    if not isinstance(elapsed, (int, float)) or not math.isfinite(elapsed) or elapsed < 0:
                        raise ValueError('Invalid monotonic elapsed time')
                    if previous_elapsed is not None:
                        if elapsed < previous_elapsed:
                            raise ValueError('Monotonic elapsed time decreased')
                clock.observe(row)
                requested = row.get('requested_at', observed)
                duration = row.get('request_duration_ms')
                if duration is not None and (not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration < 0):
                    raise ValueError('Invalid request duration')
                if not isinstance(requested, (int, float)) or not math.isfinite(requested) or (requested > observed and duration is None):
                    raise ValueError('Invalid request time')
                if kind != 'error':
                    event_ticker = row.get('ticker')
                    if not isinstance(event_ticker, str) or not event_ticker:
                        raise ValueError('Missing ticker')
                    if ticker not in (None, event_ticker):
                        raise ValueError('Replay cannot mix tickers')
                    ticker = event_ticker
                previous = observed
                previous_elapsed = elapsed
                engine.ingest(row)
            except (ValueError, KeyError, TypeError, AttributeError) as error:
                raise ValueError(f'{path}: line {number}: {error}') from error
    if ticker is None:
        raise ValueError('Recording contains no market or snapshot events')
    return digest.hexdigest()

def provenance(data_sha256: str, settings: dict) -> dict:
    root = Path(__file__).resolve().parent
    identity = {'data_sha256': data_sha256,
                'parameters': normalized_parameters(settings),
                'source_sha256': {name: digest_file(root/name) for name in SOURCES},
                'runtime': {'python': platform.python_version(),
                            'decimal_precision': getcontext().prec,
                            'decimal_rounding': getcontext().rounding}}
    encoded = json.dumps(identity, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    return {**identity, 'experiment_fingerprint': hashlib.sha256(encoded).hexdigest()}

def compare_reports(left: dict, right: dict) -> dict:
    a, b = left['provenance'], right['provenance']
    # Creation time, paths, and command name are intentionally excluded.
    ignored = {'settings', 'provenance', 'created_at_utc'}
    result_a = {k: v for k, v in left.items() if k not in ignored}
    result_b = {k: v for k, v in right.items() if k not in ignored}
    same_experiment = a['experiment_fingerprint'] == b['experiment_fingerprint']
    same_results = result_a == result_b
    return {'same_data': a['data_sha256'] == b['data_sha256'],
            'same_parameters': a['parameters'] == b['parameters'],
            'same_code': a['source_sha256'] == b['source_sha256'],
            'same_runtime': a['runtime'] == b['runtime'],
            'same_experiment': same_experiment, 'same_results': same_results,
            'reproduced': same_experiment and same_results,
            'changed_result_fields': sorted(k for k in result_a.keys() | result_b.keys()
                                           if result_a.get(k) != result_b.get(k))}
