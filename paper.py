#!/usr/bin/env python3
"""Public GET-only recorder and deliberately simple paper-trading experiment."""
import argparse
import json
import ssl
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from portfolio import Fill, Portfolio
from lifecycle import Lifecycle
from research import replay_recording, provenance, digest_file, compare_reports
from collector import collect, quality, selection_details
from clocks import EventClock

BASE = 'https://external-api.kalshi.com/trade-api/v2'
D = Decimal

def get(path, **params):
    url = BASE + path + '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={'User-Agent': 'kalshi-paper-research/0.1'})
    context = ssl.create_default_context()
    # python.org macOS installs may lack their own CA bundle; use system roots.
    if Path('/etc/ssl/cert.pem').exists():
        context.load_verify_locations('/etc/ssl/cert.pem')
    with urllib.request.urlopen(req, timeout=10, context=context) as response:
        return json.load(response)

def book(raw):
    b = raw['orderbook_fp']
    yes = sorted([(D(p), D(q)) for p, q in b['yes_dollars'] if D(q) > 0], reverse=True)
    no = sorted([(D(p), D(q)) for p, q in b['no_dollars'] if D(q) > 0], reverse=True)
    if not yes or not no:
        return None
    bid, bq = yes[0]
    ask, aq = 1-no[0][0], no[0][1]
    if not 0 < bid < ask < 1:
        return None
    y, n = sum(q for _, q in yes[:3]), sum(q for _, q in no[:3])
    return bid, ask, bq, aq, float((y-n)/(y+n))

def fee(price, coefficient, quantum):
    # Configurable research assumption, NOT automatic market fee discovery.
    return (coefficient * price * (1-price) / quantum).to_integral_value(rounding=ROUND_CEILING) * quantum

class Experiment:
    def __init__(self, args, trade_enabled=True, output=print):
        self.a = args
        self.trade_enabled = trade_enabled
        self.output = output
        self.portfolio = Portfolio(D(str(args.cash)))
        self.position = None
        self.pending = None
        self.completed = []
        self.last = None
        self.mark = None
        self.mark_at = None
        self.lifecycle = Lifecycle()
        self.clock = EventClock()
        self.lifecycle_elapsed = None

    def invalidate(self):
        self.pending = None
        self.mark = None
        self.mark_at = None

    def ingest(self, row):
        """Same event processing for live recording and offline replay."""
        kind = row.get('type')
        if kind not in ('market', 'snapshot', 'error', 'recording_end'):
            return
        wall, t = self.clock.observe(row)
        if self.clock.discontinuities:
            # Calendar deadlines cannot be repaired without an authoritative clock anchor.
            # Preserve/settle inventory, but suspend simulated trading for this session.
            self.invalidate()
        if kind == 'error':
            self.invalidate()
            self.last = t
            return
        if kind not in ('market', 'snapshot'):
            return
        if self.last is not None and t < self.last:
            raise ValueError('Events must be chronological')
        if self.last is not None and t-self.last > self.a.max_gap:
            self.invalidate()
        duration = row.get('request_duration_ms')
        request_seconds = duration/1000 if duration is not None else wall-row.get('requested_at', wall)
        if request_seconds > self.a.max_gap or request_seconds < 0:
            self.invalidate()
            return
        if kind == 'market':
            if row.get('ticker') != row['market']['ticker']:
                raise ValueError('Market event ticker mismatch')
            payout = self.lifecycle.update(row['market'], wall)
            self.lifecycle_elapsed = t
            if payout is not None:
                first_settlement = self.portfolio.settlement is None
                self.portfolio.settle(payout, wall)
                self.position = None
                if first_settlement:
                    self.output(f'{t:.0f} PAPER SETTLEMENT YES payout={payout}', flush=True)
            if not self.lifecycle.can_trade(wall) or self.portfolio.settlement is not None:
                self.invalidate()
            self.last = t
        else:
            self.step(row)

    @property
    def cash(self):
        return self.portfolio.cash

    @property
    def fees(self):
        return self.portfolio.fees

    def step(self, row):
        wall, t = self.clock.observe(row)
        if not self.trade_enabled:
            self.last = t
            return
        # Legacy files without lifecycle events retain snapshot-only behavior.
        if self.clock.discontinuities or self.portfolio.settlement is not None or (self.lifecycle.ticker is not None and
                (not self.lifecycle.can_trade(wall) or t-self.lifecycle_elapsed > self.a.max_gap)):
            self.invalidate()
            self.last = t
            return
        b = book(row['book'])
        # Expire pending instructions across recording/network gaps.
        if self.last is not None and t-self.last > self.a.max_gap:
            self.pending = None
        self.last = t
        if b is None:
            self.pending = None
            self.mark = None
            self.mark_at = None
            return
        bid, ask, bq, aq, imbalance = b
        self.mark = bid if bq >= 1 else None
        self.mark_at = wall if self.mark is not None else None
        slip = D(str(self.a.slippage))
        if self.pending and t >= self.pending[1]:
            action = self.pending[0]
            self.pending = None
            p = ask+slip if action == 'buy' else bid-slip
            f = fee(p, D(str(self.a.fee_coefficient)), D(str(self.a.fee_quantum)))
            if action == 'buy' and aq >= 1 and 0 < p < 1 and self.cash >= p+f:
                self.portfolio.apply(Fill('buy', p, D(1), f, wall))
                self.position = (p+f, t)
                self.output(f'{t:.0f} PAPER BUY 1 YES @ {p} fee={f}', flush=True)
            elif action == 'sell' and self.position and bq >= 1 and 0 < p < 1:
                pnl = p-f-self.position[0]
                self.portfolio.apply(Fill('sell', p, D(1), f, wall))
                self.completed.append(float(pnl))
                self.position = None
                self.output(f'{t:.0f} PAPER SELL 1 YES @ {p} net_pnl={pnl}', flush=True)
        if self.pending is None:
            if self.position and t-self.position[1] >= self.a.hold:
                self.pending = ('sell', t+self.a.latency)
            elif not self.position and imbalance >= self.a.threshold and ask-bid <= D(str(self.a.max_spread)):
                self.pending = ('buy', t+self.a.latency)

    def report(self):
        return {'report_version': 4,
                'strategy_clock': self.clock.mode,
                'clock_discontinuities': self.clock.discontinuities,
                'trading_suspended_for_clock': bool(self.clock.discontinuities),
                'market_status': self.lifecycle.status,
                'lifecycle_observed_at': self.lifecycle.last_observed,
                'lifecycle_tracking': self.lifecycle.ticker is not None,
                'settlement': self.portfolio.settlement,
                'completed_round_trips': len(self.completed),
                **{k: float(v) if v is not None else None for k, v in self.portfolio.valuation(self.mark).items()},
                'bid_mark_received_at': self.mark_at,
                'fills': [{'side': f.side, 'price': str(f.price), 'quantity': str(f.quantity),
                           'fee': str(f.fee), 'timestamp': f.timestamp} for f in self.portfolio.fills],
                'pending_action': self.pending[0] if self.pending else None,
                'warning': 'Snapshot fills are hypothetical. Clock discontinuities suspend trading for the remainder of the session; observed final settlement remains supported. Unsettled inventory may have unknown value. No settlement fee assumed. Legacy wall-only recordings cannot provide monotonic duration guarantees.'}

def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    listing = sub.add_parser('markets', help='List open markets; optional series filter')
    listing.add_argument('--series')
    listing.add_argument('--details', action='store_true', help='Explain contract selection using raw lifecycle times')
    listing.add_argument('--min-remaining-seconds', type=float, default=300)
    comparison = sub.add_parser('compare', help='Compare two versioned experiment reports')
    comparison.add_argument('left')
    comparison.add_argument('right')
    recording = sub.add_parser('collect', help='Record public data without running a strategy')
    selection = recording.add_mutually_exclusive_group(required=True)
    selection.add_argument('--ticker')
    selection.add_argument('--series', help='Automatically choose earliest-closing active binary market')
    recording.add_argument('--file', help='New recording path; otherwise generated under recordings/')
    recording.add_argument('--minutes', type=float, default=5)
    recording.add_argument('--until-settlement', action='store_true',
                           help='Follow the selected contract until a final payout; replaces --minutes')
    recording.add_argument('--max-wait-minutes', type=float, default=30,
                           help='Total collection cap with --until-settlement, including trading (default: 30)')
    recording.add_argument('--interval', type=float, default=2)
    recording.add_argument('--min-remaining-seconds', type=float, default=300,
                           help='Minimum lifetime for automatic --series selection (default: 300; 0 allows any unexpired market)')
    diagnostic = sub.add_parser('quality', help='Inspect recording quality without a strategy')
    diagnostic.add_argument('--file', required=True)
    for name in ('run', 'replay', 'evaluate'):
        s = sub.add_parser(name)
        if name == 'evaluate':
            s.add_argument('--files', nargs='+', required=True, help='Recordings to evaluate as a fixed cohort')
            s.add_argument('--synthetic', action='store_true', help='Label this entire evaluation as a mechanics demo')
        else:
            s.add_argument('--file', required=True, help='New JSONL recording (run) or existing recording (replay)')
        s.add_argument('--report', help='New report path; existing reports are never overwritten')
        s.add_argument('--fee-coefficient', type=float, required=True, help='Verify for selected market; e.g. 0.07 is an ASSUMPTION')
        s.add_argument('--fee-quantum', type=float, default=.01, help='Conservative per-fill rounding assumption')
        s.add_argument('--cash', type=float, default=1000)
        s.add_argument('--hold', type=float, default=60)
        s.add_argument('--latency', type=float, default=1)
        s.add_argument('--max-gap', type=float, default=15)
        s.add_argument('--threshold', type=float, default=.6)
        s.add_argument('--max-spread', type=float, default=.02)
        s.add_argument('--slippage', type=float, default=.001, help='Extra dollars per contract on EACH leg')
        if name == 'run':
            s.add_argument('--ticker', required=True)
            s.add_argument('--interval', type=float, default=2)
            s.add_argument('--minutes', type=float, default=30)
    a = p.parse_args()
    if a.command == 'quality':
        print(json.dumps(quality(a.file), indent=2))
        return
    if a.command == 'collect':
        if not 1 <= a.interval < float('inf') or not 0 < a.minutes < float('inf'):
            p.error('interval must be finite and >=1 second; minutes finite and positive')
        if not 0 <= a.min_remaining_seconds < float('inf'):
            p.error('min-remaining-seconds must be finite and nonnegative')
        if not 0 < a.max_wait_minutes < float('inf'):
            p.error('max-wait-minutes must be finite and positive')
        try:
            collect(a, get)
        except (OSError, ValueError, KeyError) as error:
            p.exit(2, f'Collection error: {error}\n')
        return
    if a.command == 'compare':
        result = compare_reports(json.loads(Path(a.left).read_text()), json.loads(Path(a.right).read_text()))
        print(json.dumps(result, indent=2))
        return
    if a.command == 'markets':
        if not 0 <= a.min_remaining_seconds < float('inf'):
            p.error('min-remaining-seconds must be finite and nonnegative')
        params = {'status': 'open', 'limit': 100, 'mve_filter': 'exclude'}
        if a.series:
            params['series_ticker'] = a.series
        markets = get('/markets', **params)['markets']
        now = time.time()
        if a.details:
            print(json.dumps({'computer_time_utc': datetime.fromtimestamp(now, timezone.utc).isoformat(),
                              'markets': [selection_details(m, now, a.min_remaining_seconds) for m in markets]}, indent=2))
        else:
            for m in markets:
                print(m['ticker'], '|', m.get('title', ''), '| bid', m.get('yes_bid_dollars'), 'ask', m.get('yes_ask_dollars'))
        print('First page only. Use --series to narrow the selection.')
        return
    for field in ('cash', 'hold', 'latency', 'max_gap', 'fee_quantum', 'max_spread'):
        if not 0 < getattr(a, field) < float('inf'):
            p.error(field+' must be finite and positive')
    if not 0 <= a.threshold <= 1 or not 0 <= a.slippage < 1 or not 0 <= a.fee_coefficient < float('inf'):
        p.error('Invalid threshold, slippage, or fee coefficient')
    if a.command == 'evaluate':
        from evaluation import evaluate, save_report, overview
        try:
            report_path = Path(a.report) if a.report else Path('reports')/f'evaluation-{uuid.uuid4().hex}.json'
            if report_path.exists():
                raise FileExistsError(f'Report already exists: {report_path}')
            result = evaluate(a.files, a, Experiment, synthetic=a.synthetic)
            save_report(result, report_path)
        except (OSError, ValueError, KeyError) as error:
            p.exit(2, f'Evaluation error: {error}\n')
        print(overview(result))
        print('Evaluation report:', report_path)
        return
    e = Experiment(a)
    path = Path(a.file)
    report_path = Path(a.report) if a.report else path.parent/'reports'/f'{path.stem}-{uuid.uuid4().hex}.json'
    if report_path.exists():
        p.error(f'Report already exists: {report_path}. Choose a new --report filename.')
    if a.command == 'replay':
        # Fail without publishing a success report if replay is invalid or interrupted.
        data_hash = replay_recording(path, e)
    try:
        if a.command == 'run':
            if a.interval < 1 or a.minutes <= 0:
                p.error('interval must be >=1 second and minutes positive')
            market = get('/markets/'+urllib.parse.quote(a.ticker, safe=''))['market']
            end = time.monotonic()+a.minutes*60
            # Exclusive creation avoids mixing runs or accidentally overwriting evidence.
            with path.open('x') as f:
                f.write(json.dumps({'type': 'metadata', 'market': market, 'settings': vars(a)})+'\n')
                f.flush()
                failures = 0
                while time.monotonic() < end:
                    start = time.time()
                    try:
                        market = get('/markets/'+urllib.parse.quote(a.ticker, safe=''))['market']
                        row = {'type': 'market', 'ticker': a.ticker, 'requested_at': start,
                               'received_at': time.time(), 'market': market}
                        f.write(json.dumps(row)+'\n')
                        f.flush()
                        e.ingest(row)
                        if e.portfolio.settlement is not None:
                            break
                        if e.lifecycle.can_trade(time.time()):
                            requested = time.time()
                            raw = get('/markets/'+urllib.parse.quote(a.ticker, safe='')+'/orderbook', depth=10)
                            row = {'type': 'snapshot', 'ticker': a.ticker, 'requested_at': requested, 'received_at': time.time(), 'book': raw}
                            f.write(json.dumps(row)+'\n')
                            f.flush()
                            e.ingest(row)
                        else:
                            e.invalidate()
                        failures = 0
                    except (OSError, ValueError, KeyError) as error:
                        failures += 1
                        event = {'type': 'error', 'at': time.time(), 'message': str(error)}
                        e.ingest(event)
                        f.write(json.dumps(event)+'\n')
                        f.flush()
                        print('Data error:', error, flush=True)
                        time.sleep(min(30, 2**min(failures, 5)))
                    time.sleep(max(0, a.interval-(time.time()-start)))
    except KeyboardInterrupt:
        print('\nStopped; recording preserved.')
    if a.command == 'run':
        data_hash = digest_file(path)
    report = {'settings': vars(a), 'created_at_utc': datetime.now(timezone.utc).isoformat(),
              'provenance': provenance(data_hash, vars(a)), **e.report()}
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open('x') as f:
        f.write(json.dumps(report, indent=2, allow_nan=False)+'\n')
    print(json.dumps(report, indent=2))
    print('Report:', report_path)

if __name__ == '__main__':
    main()
