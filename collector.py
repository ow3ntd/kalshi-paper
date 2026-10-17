"""Public-data-only collection and streaming recording diagnostics. No portfolio."""
import hashlib
import json
import math
import time
import uuid
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote
from lifecycle import Lifecycle, timestamp

def quotes(raw):
    """Validate price levels; a one-sided book is valid data, not a zero price."""
    b = raw['orderbook_fp']
    sides = []
    for name in ('yes_dollars', 'no_dollars'):
        levels = b[name]
        if not isinstance(levels, list):
            raise ValueError('Book levels must be lists')
        parsed = []
        for level in levels:
            if len(level) != 2:
                raise ValueError('Expected price and quantity per level')
            p, q = (Decimal(str(x)) for x in level)
            if not p.is_finite() or not q.is_finite() or not 0 <= p <= 1 or q < 0:
                raise ValueError('Invalid price or quantity')
            if q:
                parsed.append((p, q))
        sides.append(sorted(parsed, reverse=True))
    yes, no = sides
    bid = yes[0][0] if yes else None
    ask = 1-no[0][0] if no else None
    return bid, ask

def selection_details(market, now, min_remaining_seconds=300):
    """Explain the same eligibility checks used by automatic selection."""
    opened = timestamp(market.get('open_time'))
    closed = timestamp(market.get('close_time'))
    expiration = timestamp(market.get('expiration_time'))
    deadline = min((t for t in (closed, expiration) if t is not None), default=None)
    remaining = deadline-now if deadline is not None else None
    reasons = []
    if market.get('status') != 'active':
        reasons.append('status_is_not_active')
    if market.get('market_type', 'binary') != 'binary':
        reasons.append('not_binary')
    if opened is not None and opened > now:
        reasons.append('open_time_is_in_future')
    if closed is None:
        reasons.append('missing_close_time')
    if remaining is not None:
        if remaining <= 0:
            reasons.append('close_or_expiration_has_passed')
        elif remaining < min_remaining_seconds:
            reasons.append('insufficient_remaining_time')
    return {'ticker': market.get('ticker'), 'status': market.get('status'),
            'market_type': market.get('market_type', 'binary'),
            'open_time': market.get('open_time'), 'close_time': market.get('close_time'),
            'expiration_time': market.get('expiration_time'),
            'remaining_seconds': round(remaining, 3) if remaining is not None else None,
            'required_remaining_seconds': min_remaining_seconds,
            'eligible': not reasons, 'rejection_reasons': reasons}

class NoEligibleMarket(ValueError):
    """No currently eligible contract; batch discovery may retry later."""

def choose_market(getter, series, now, min_remaining_seconds=300, exclude=()):
    """Choose earliest-closing eligible contract; paginate the whole series."""
    if not 0 <= min_remaining_seconds < math.inf:
        raise ValueError("min_remaining_seconds must be finite and nonnegative")
    candidates = []
    cursor = None
    seen = set()
    while True:
        params = {'status': 'open', 'series_ticker': series, 'limit': 100, 'mve_filter': 'exclude'}
        if cursor:
            params['cursor'] = cursor
        response = getter('/markets', **params)
        for m in response['markets']:
            if m['ticker'] not in exclude and selection_details(m, now, min_remaining_seconds)['eligible']:
                candidates.append((timestamp(m['close_time']), m['ticker']))
        cursor = response.get('cursor')
        if not cursor:
            break
        if cursor in seen:
            raise ValueError('Repeated API pagination cursor')
        seen.add(cursor)
    if not candidates:
        raise NoEligibleMarket(
            f'No eligible market in {series}: automatic selection requires an active binary '
            f'market already open with a known close time and at least '
            f'{min_remaining_seconds:g} seconds remaining before close or expiration. '
            'Try again later or lower --min-remaining-seconds (0 allows any unexpired market).')
    return min(candidates)[1]

def percentile(values, fraction, digits=3):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(len(ordered)*fraction)-1)], digits)

def quality(path):
    """Diagnostics from recorded evidence; gaps are observation gaps, not lost-event counts."""
    path = Path(path)
    counts, errors, statuses = Counter(), Counter(), Counter()
    durations, gaps, spreads = [], [], []
    empty = one_sided = two_sided = usable = crossed = 0
    last_snapshot = first = last = None
    first_elapsed = last_elapsed = previous_elapsed = None
    previous_wall = previous_sequence = None
    missing_elapsed = 0
    clock_steps = []
    end_event = None
    last_snapshot_elapsed = None
    previous = -math.inf
    ticker = None
    h = hashlib.sha256()
    with path.open('rb') as f:
        for number, raw in enumerate(f, 1):
            h.update(raw)
            try:
                row = json.loads(raw)
                kind = row['type']
                elapsed = row.get('elapsed_s')
                if elapsed is not None:
                    if (not isinstance(elapsed, (int, float)) or not math.isfinite(elapsed)
                            or elapsed < 0 or (previous_elapsed is not None and elapsed < previous_elapsed)):
                        raise ValueError('Invalid or decreasing monotonic elapsed time')
                    previous_elapsed = elapsed
                if kind == 'metadata':
                    continue
                if kind not in ('market', 'snapshot', 'error', 'recording_end'):
                    raise ValueError('Unknown event type')
                t = row.get('received_at', row.get('at'))
                if not isinstance(t, (int, float)) or not math.isfinite(t):
                    raise ValueError('Invalid or out-of-order observation time')
                if kind == 'recording_end':
                    end_event = row
                    continue
                if t < previous and (elapsed is None or last_elapsed is None):
                    raise ValueError('Invalid or out-of-order observation time')
                if previous_wall is not None and elapsed is not None and last_elapsed is not None:
                    discrepancy = (t-previous_wall)-(elapsed-last_elapsed)
                    if abs(discrepancy) > .1:
                        clock_steps.append({'from_sequence': previous_sequence,
                            'to_sequence': row.get('sequence'),
                            'wall_delta_seconds': round(t-previous_wall, 6),
                            'monotonic_delta_seconds': round(elapsed-last_elapsed, 6),
                            'difference_seconds': round(discrepancy, 6)})
                previous_wall, previous_sequence = t, row.get('sequence')
                if first is None:
                    first_elapsed = elapsed
                if elapsed is None:
                    missing_elapsed += 1
                last_elapsed = elapsed
                previous = t
                if first is None:
                    first = t
                last = t
                if kind != 'error':
                    event_ticker = row['ticker']
                    if not event_ticker or ticker not in (None, event_ticker):
                        raise ValueError('Missing or mixed tickers')
                    ticker = event_ticker
                counts[kind] += 1
                duration = row.get('request_duration_ms')
                if duration is not None:
                    if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration < 0:
                        raise ValueError('Invalid request duration')
                    durations.append(duration)
                if kind == 'error':
                    errors[row.get('category', 'unspecified')] += 1
                elif kind == 'market':
                    statuses[row['market']['status']] += 1
                elif kind == 'snapshot':
                    bid, ask = quotes(row['book'])
                    # New recordings measure elapsed time monotonically, immune to wall clock corrections.
                    if last_snapshot is not None:
                        if elapsed is not None and last_snapshot_elapsed is not None:
                            gaps.append(elapsed-last_snapshot_elapsed)
                        elif elapsed is None and last_snapshot_elapsed is None:
                            gaps.append(t-last_snapshot)
                    last_snapshot, last_snapshot_elapsed = t, elapsed
                    if bid is None and ask is None:
                        empty += 1
                    elif bid is None or ask is None:
                        one_sided += 1
                    else:
                        two_sided += 1
                        if bid >= ask:
                            crossed += 1
                        else:
                            spreads.append(float(ask-bid))
                            if 0 < bid < ask < 1:
                                usable += 1
            except (ValueError, TypeError, KeyError, InvalidOperation) as error:
                raise ValueError(f'{path}: line {number}: {error}') from error
    mono_span = (last_elapsed-first_elapsed if first_elapsed is not None and
                 last_elapsed is not None and not missing_elapsed else None)
    wall_span = last-first if first is not None else 0
    return {'recording': str(path), 'data_sha256': h.hexdigest(), 'ticker': ticker,
            'observation_span_seconds': round(last-first, 3) if first is not None else 0,
            'wall_clock_observation_span_seconds': round(wall_span, 3),
            'monotonic_observation_span_seconds': round(mono_span, 3) if mono_span is not None else None,
            'monotonic_recording_duration_seconds': (round(end_event['elapsed_s'], 3)
                if end_event is not None and end_event.get('elapsed_s') is not None else None),
            'last_observation_elapsed_seconds': last_elapsed,
            'recording_end_observed': end_event is not None,
            'recording_stop_reason': end_event.get('reason') if end_event else None,
            'wall_minus_monotonic_span_seconds': round(wall_span-mono_span, 3) if mono_span is not None else None,
            'wall_clock_step_count': len(clock_steps),
            'wall_clock_steps': clock_steps,
            'timing_warning': ('Wall and monotonic timestamps diverge; do not interpret wall-time holding periods as elapsed durations.'
                               if clock_steps else None),
            'snapshots': counts['snapshot'], 'market_updates': counts['market'],
            'errors': counts['error'], 'error_categories': dict(errors),
            'market_status_counts': dict(statuses), 'two_sided_snapshots': two_sided,
            'empty_snapshots': empty, 'one_sided_snapshots': one_sided,
            'usable_price_snapshots': usable, 'locked_or_crossed_snapshots': crossed,
            'request_duration_ms_p50': percentile(durations, .5),
            'request_duration_ms_p95': percentile(durations, .95),
            'max_snapshot_gap_seconds': round(max(gaps), 3) if gaps else None,
            'median_spread_dollars': percentile(spreads, .5, 6),
            'warning': 'Polling misses events between snapshots. Request duration is not quote age or order latency. Gaps can reflect closed markets. Usable prices do not establish tradability or an edge.'}

def collect(args, getter, clock=time, output=print, *, stop_at=None, show_summary=True):
    """Injected clock/HTTP client allow failure recovery tests without network or sleeping."""
    ticker = args.ticker or choose_market(getter, args.series, clock.time(), args.min_remaining_seconds)
    if not ticker or '/' in ticker:
        raise ValueError('Invalid ticker')
    path = Path(args.file) if args.file else Path('recordings')/f'{ticker}-{uuid.uuid4().hex}.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f'Recording already exists: {path}')
    start = clock.monotonic()
    until_settlement = getattr(args, 'until_settlement', False)
    limit_seconds = (getattr(args, 'max_wait_minutes', 30) if until_settlement else args.minutes)*60
    deadline, heartbeat = start+limit_seconds, start
    seq = snapshots = failures = error_count = 0
    life = Lifecycle()
    stop_reason = 'settlement_timeout' if until_settlement else 'duration_limit'
    if stop_at is not None and stop_at <= deadline:
        deadline = stop_at
        stop_reason = 'batch_time_limit'
    endpoint = '/markets/'+quote(ticker, safe='')
    output(f'Collecting {ticker}; no simulated or real orders.\nRecording: {path}', flush=True)
    if until_settlement:
        output(f'Following this contract through final payout; total time limit {limit_seconds/60:g} minutes.', flush=True)
    with path.open('x') as f:
        def write(event):
            nonlocal seq
            seq += 1
            event.update(sequence=seq, elapsed_s=round(clock.monotonic()-start, 6))
            f.write(json.dumps(event, allow_nan=False)+'\n')
            f.flush()

        def request(suffix, kind):
            requested, begin = clock.time(), clock.monotonic()
            try:
                payload = getter(endpoint+suffix, **({'depth': 10} if suffix else {}))
            except OSError as error:
                raise DataRequestError(error) from error
            duration = (clock.monotonic()-begin)*1000
            received = clock.time()
            event = {'type': kind, 'ticker': ticker, 'requested_at': requested,
                     'received_at': received, 'request_duration_ms': round(duration, 3)}
            try:
                if suffix:
                    event['book'] = payload
                    quotes(payload)
                else:
                    event['market'] = payload['market']
                    if payload['market']['ticker'] != ticker:
                        raise ValueError('API returned a different ticker')
                    if not isinstance(payload['market']['status'], str):
                        raise ValueError('Missing market status')
                    Lifecycle().update(payload['market'], received)
            except (ValueError, TypeError, KeyError, InvalidOperation) as error:
                write({'type': 'error', 'at': received, 'ticker': ticker,
                       'endpoint': endpoint+suffix, 'category': 'invalid_book' if suffix else 'invalid_market',
                       'message': str(error), 'raw_response': payload})
                raise RecordedError('Invalid response saved as error evidence') from error
            write(event)
            return event

        write({'type': 'metadata', 'collector_version': 2, 'ticker': ticker,
               'settings': vars(args), 'created_at': clock.time()})
        try:
            while clock.monotonic() < deadline:
                cycle = clock.monotonic()
                delay = args.interval
                try:
                    event = request('', 'market')
                    payout = life.update(event['market'], event['received_at'])
                    if payout is not None:
                        stop_reason = 'final_settlement'
                        output('Final payout observed; recording complete.', flush=True)
                        break
                    if life.can_trade(clock.time()) and clock.monotonic() < deadline:
                        request('/orderbook', 'snapshot')
                        snapshots += 1
                    failures = 0
                except (ValueError, TypeError, KeyError, InvalidOperation) as error:
                    failures += 1
                    error_count += 1
                    if not isinstance(error, RecordedError):
                        write({'type': 'error', 'at': clock.time(), 'ticker': ticker,
                               'category': 'http_error' if isinstance(error, DataRequestError) else 'invalid_response',
                               'http_status': getattr(error, 'code', None), 'message': str(error)})
                    delay = min(30, args.interval*2**min(failures, 4))
                    output(f'Data error ({error}); retry after backoff.', flush=True)
                if clock.monotonic() >= heartbeat:
                    output(f'HEARTBEAT status={life.status} snapshots={snapshots} errors={error_count} elapsed={clock.monotonic()-start:.0f}s', flush=True)
                    heartbeat = clock.monotonic()+10
                clock.sleep(min(max(0, deadline-clock.monotonic()),
                                max(0, delay-(clock.monotonic()-cycle))))
        except KeyboardInterrupt:
            stop_reason = 'interrupted'
            output('Stopped; completed events preserved.', flush=True)
        write({'type': 'recording_end', 'ticker': ticker, 'received_at': clock.time(),
               'reason': stop_reason, 'requested_duration_seconds': limit_seconds})
    if until_settlement and stop_reason != 'final_settlement':
        output('Final payout was not observed. This session is incomplete; its data has been preserved.', flush=True)
    summary = quality(path)
    summary_path = Path(str(path)+'.quality.json')
    with summary_path.open('x') as f:
        f.write(json.dumps(summary, indent=2)+'\n')
    if show_summary:
        output(json.dumps(summary, indent=2), flush=True)
    output(f'Quality report: {summary_path}', flush=True)
    return path

class RecordedError(ValueError):
    """An invalid response already persisted as an error event."""

class DataRequestError(ValueError):
    """Separate retryable data-fetch errors from fatal filesystem errors."""
    def __init__(self, error):
        super().__init__(str(error))
        self.code = getattr(error, 'code', None)
