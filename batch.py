"""Bounded sequential public-data collection with an auditable session manifest."""
import argparse
import hashlib
import json
import math
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from collector import collect, choose_market, quality, NoEligibleMarket
from fees import inspect_fees


class BatchDeadline(Exception):
    pass


def validate(args):
    if isinstance(args.sessions, bool) or not isinstance(args.sessions, int) or args.sessions < 1:
        raise ValueError('sessions must be a positive integer')
    for key in ('max_minutes', 'session_max_minutes', 'retry_seconds', 'interval'):
        value = getattr(args, key)
        if not math.isfinite(value) or value <= 0:
            raise ValueError(key+' must be finite and positive')
    if args.interval < 1 or args.retry_seconds < 1:
        raise ValueError('interval and retry-seconds must be at least one second')
    if not math.isfinite(args.min_remaining_seconds) or args.min_remaining_seconds < 0:
        raise ValueError('min-remaining-seconds must be finite and nonnegative')
    if not args.series:
        raise ValueError('series is required')


def collect_batch(args, getter, clock=time, output=print):
    validate(args)
    root = Path(args.directory) if args.directory else Path('recordings')/f'batch-{uuid.uuid4().hex}'
    # A new directory is required: never merge runs, overwrite evidence, or accidentally resume.
    root.mkdir(parents=True, exist_ok=False)
    (root/'sessions').mkdir()
    (root/'fees').mkdir()
    start = clock.monotonic()
    deadline = start+args.max_minutes*60
    state = {'batch_version': 1, 'status': 'running', 'stop_reason': None,
             'started_at_utc': datetime.fromtimestamp(clock.time(), timezone.utc).isoformat(),
             'settings': vars(args), 'sessions': [], 'selection_errors': 0, 'selection_waits': 0}
    attempted = set()
    active = None
    reason = 'session_limit'
    output(f'BATCH directory={root} attempts_limit={args.sessions} time_limit={args.max_minutes:g}m', flush=True)

    with (root/'events.ndjson').open('x') as journal:
        def checkpoint(kind, **details):
            state['elapsed_seconds'] = round(clock.monotonic()-start, 6)
            state['attempted_sessions'] = len(state['sessions'])
            state['settled_sessions'] = sum(s.get('final_payout_observed', False) for s in state['sessions'])
            journal.write(json.dumps({'type': kind, 'elapsed_s': state['elapsed_seconds'],
                                      'observed_at': clock.time(), **details}, allow_nan=False)+'\n')
            journal.flush()
            temporary = root/'batch.json.tmp'
            temporary.write_text(json.dumps(state, indent=2, allow_nan=False)+'\n')
            temporary.replace(root/'batch.json')

        def bounded_get(path, **params):
            # Check every discovery/fee page, not just the start of an API operation.
            if clock.monotonic() >= deadline:
                raise BatchDeadline()
            return getter(path, **params)

        def pause(seconds):
            clock.sleep(min(seconds, max(0, deadline-clock.monotonic())))

        checkpoint('batch_started')
        failures = 0
        try:
            while len(state['sessions']) < args.sessions:
                if clock.monotonic() >= deadline:
                    reason = 'batch_time_limit'
                    break
                try:
                    ticker = choose_market(bounded_get, args.series, clock.time(),
                                           args.min_remaining_seconds, exclude=attempted)
                except NoEligibleMarket:
                    state['selection_waits'] += 1
                    checkpoint('waiting_for_market')
                    output(f'BATCH WAIT no new eligible market; retry in {args.retry_seconds:g}s', flush=True)
                    pause(args.retry_seconds)
                    continue
                except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as error:
                    failures += 1
                    state['selection_errors'] += 1
                    delay = min(60, args.retry_seconds*2**min(failures-1, 4))
                    checkpoint('selection_error', message=str(error), retry_seconds=delay)
                    output(f'BATCH discovery error: {error}; retry in {delay:g}s', flush=True)
                    pause(delay)
                    continue
                failures = 0
                if clock.monotonic() >= deadline:
                    reason = 'batch_time_limit'
                    break
                attempted.add(ticker)
                number = len(state['sessions'])+1
                active = {'number': number, 'ticker': ticker, 'status': 'starting',
                          'recording': f'sessions/{number:04d}.jsonl',
                          'fee_report': None, 'fee_check_error': None, 'final_payout_observed': False}
                state['sessions'].append(active)
                checkpoint('session_started', number=number, ticker=ticker)
                output(f'BATCH session={number}/{args.sessions} ticker={ticker}', flush=True)
                # Fee lookup failure is visible but does not discard potentially useful raw data.
                try:
                    evidence = inspect_fees(bounded_get, ticker=ticker,
                        now=datetime.fromtimestamp(clock.time(), timezone.utc))
                except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as error:
                    active['fee_check_error'] = str(error)
                    checkpoint('fee_check_error', number=number, message=str(error))
                    output(f'BATCH fee check unavailable: {error}; continuing data collection.', flush=True)
                else:
                    fee_path = f'fees/{number:04d}.json'
                    with (root/fee_path).open('x') as f:
                        f.write(json.dumps(evidence, indent=2, allow_nan=False)+'\n')
                    active['fee_report'] = fee_path
                if clock.monotonic() >= deadline:
                    raise BatchDeadline()
                active['status'] = 'collecting'
                checkpoint('recording_started', number=number)
                single = argparse.Namespace(ticker=ticker, series=None, file=str(root/active['recording']),
                    interval=args.interval, minutes=5, until_settlement=True,
                    max_wait_minutes=args.session_max_minutes, min_remaining_seconds=args.min_remaining_seconds)
                try:
                    path = collect(single, getter, clock, output, stop_at=deadline, show_summary=False)
                    q = quality(path)
                except (ValueError, KeyError, TypeError, ArithmeticError) as error:
                    active.update(status='failed', error=str(error))
                    checkpoint('session_failed', number=number, message=str(error))
                    output(f'BATCH session failed; evidence retained: {error}', flush=True)
                    active = None
                    continue
                active.update(status=q['recording_stop_reason'],
                    final_payout_observed=q['recording_stop_reason'] == 'final_settlement',
                    data_sha256=q['data_sha256'], snapshots=q['snapshots'], errors=q['errors'],
                    quality_report=active['recording']+'.quality.json')
                checkpoint('session_finished', number=number, outcome=active['status'])
                output(f"BATCH session={number} outcome={active['status']} snapshots={q['snapshots']} errors={q['errors']}", flush=True)
                outcome = active['status']
                active = None
                if outcome == 'interrupted':
                    reason = 'interrupted'
                    break
                if outcome == 'batch_time_limit':
                    reason = 'batch_time_limit'
                    break
        except BatchDeadline:
            reason = 'batch_time_limit'
        except KeyboardInterrupt:
            reason = 'interrupted'
        except OSError as error:
            # Disk failures must not masquerade as successful sessions or HTTP retries.
            state.update(status='stopped', stop_reason='storage_error')
            if active is not None:
                active.update(status='failed', error=str(error))
            try:
                checkpoint('storage_error', message=str(error))
            except OSError:
                pass  # The most recent checkpoint/journal still provides recovery evidence.
            raise
        if active is not None:
            active.update(status=reason, error='Stopped before recording completed; no success inferred.')
        state.update(status='stopped', stop_reason=reason)
        checkpoint('batch_finished', reason=reason)
    output(f"BATCH STOP reason={reason} attempts={len(state['sessions'])} final_payouts={state['settled_sessions']} manifest={root/'batch.json'}", flush=True)
    return root


def batch_inputs(directory):
    """Keep every attempted recording in evaluation, including missing/failed files."""
    root = Path(directory).resolve()
    raw = (root/'batch.json').read_bytes()
    state = json.loads(raw)
    if state.get('batch_version') != 1 or state.get('status') != 'stopped':
        raise ValueError('Batch is not stopped or has an unsupported manifest; stop collection before evaluation')
    entries = state['sessions']
    if not isinstance(entries, list) or state['attempted_sessions'] != len(entries):
        raise ValueError('Invalid batch session count')
    paths = []
    for entry in entries:
        relative = Path(entry['recording'])
        path = (root/relative).resolve()
        if relative.is_absolute() or not path.is_relative_to(root/'sessions'):
            raise ValueError('Recording path escapes batch sessions directory')
        paths.append(path)
    context = {'manifest_sha256': hashlib.sha256(raw).hexdigest(), 'stop_reason': state['stop_reason'],
               'attempted_sessions': len(entries), 'selection_errors': state['selection_errors'],
               'fee_check_failures': sum(bool(s.get('fee_check_error')) for s in entries)}
    return paths, context
