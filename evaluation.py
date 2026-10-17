"""Fixed-parameter, independent-session evaluation against an idle cash portfolio."""
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path

from collector import quality, quotes
from research import PARAMETERS, provenance, replay_recording


class SessionReplay:
    """Feed identical observations to both portfolios and measure sampled risk."""
    def __init__(self, strategy, baseline):
        self.strategy, self.baseline = strategy, baseline
        self.previous_time = None
        self.exposure_seconds = 0.0
        self.peak = strategy.cash
        self.drawdown = D(0)
        self.max_cost = D(0)
        self.unknown_equity_observations = 0
        self.active_snapshots = 0
        self.first_observed = None
        self.last_observed = None
        self.end_seen = False

    def ingest(self, row):
        if self.end_seen:
            raise ValueError('Events after recording_end are not allowed')
        kind = row['type']
        if kind == 'recording_end':
            self.end_seen = True
        wall = row.get('received_at', row.get('at'))
        t = row.get('elapsed_s', wall)
        if self.first_observed is None:
            self.first_observed = wall
        self.last_observed = wall
        if self.previous_time is not None and self.strategy.portfolio.quantity:
            self.exposure_seconds += t-self.previous_time
        self.previous_time = t
        if kind == 'snapshot':
            quotes(row['book'])  # Reject malformed depth, including non-finite numbers.
            if self.strategy.lifecycle.can_trade(wall):
                self.active_snapshots += 1
        self.strategy.ingest(row)
        self.baseline.ingest(row)
        portfolio = self.strategy.portfolio
        self.max_cost = max(self.max_cost, portfolio.cost_basis)
        equity = portfolio.valuation(self.strategy.mark)['equity_before_exit_costs']
        if equity is None:
            self.unknown_equity_observations += 1
        else:
            self.peak = max(self.peak, equity)
            self.drawdown = max(self.drawdown, self.peak-equity)


def evaluate(paths, args, engine_factory, synthetic=False):
    """No tuning or winner selection; retain all exclusions in the submitted cohort."""
    sessions = []
    for name in paths:
        path = Path(name)
        strategy = engine_factory(args, output=lambda *a, **k: None)
        baseline = engine_factory(args, trade_enabled=False, output=lambda *a, **k: None)
        replay = SessionReplay(strategy, baseline)
        session = {'recording': str(path), 'included': False, 'exclusion_reasons': []}
        try:
            data_hash = replay_recording(path, replay)
            diagnostic = quality(path)
            if diagnostic['data_sha256'] != data_hash:
                raise ValueError('Recording changed during evaluation; finish collecting first')
            report, idle = strategy.report(), baseline.report()
            complete = (strategy.portfolio.settlement is not None and
                        strategy.portfolio.quantity == 0 and replay.active_snapshots > 0)
            reasons = session['exclusion_reasons']
            if strategy.portfolio.settlement is None:
                reasons.append('final_payout_not_observed')
            if replay.active_snapshots == 0:
                reasons.append('no_snapshots_during_observed_active_market')
            if strategy.portfolio.quantity:
                reasons.append('unresolved_inventory')
            if report['trading_suspended_for_clock']:
                reasons.append('clock_discontinuity')
            if diagnostic['errors']:
                reasons.append('recorded_data_errors')
            if diagnostic['locked_or_crossed_snapshots']:
                reasons.append('locked_or_crossed_book')
            if (diagnostic['max_snapshot_gap_seconds'] is not None and
                    diagnostic['max_snapshot_gap_seconds'] > args.max_gap):
                reasons.append('snapshot_gap_exceeds_max_gap')
            # Complete means the recorded trading window reached settlement, not that
            # collection covered the full life of a contract or every exchange event.
            session.update(data_sha256=data_hash, ticker=strategy.lifecycle.ticker,
                complete=complete, active_snapshots=replay.active_snapshots,
                first_observed_at=replay.first_observed, last_observed_at=replay.last_observed,
                quality=diagnostic, strategy=report, no_trade=idle,
                strategy_net_pnl=str(strategy.cash-D(str(args.cash))) if complete else None,
                no_trade_net_pnl='0' if complete else None,
                excess_net_pnl=str(strategy.cash-baseline.cash) if complete else None,
                exposure_seconds=round(replay.exposure_seconds, 6),
                max_inventory_cost_dollars=str(replay.max_cost),
                sampled_max_drawdown_before_exit_costs=str(replay.drawdown),
                unknown_equity_observations=replay.unknown_equity_observations,
                provenance=provenance(data_hash, vars(args)))
            if not synthetic and (session['ticker'] or '').upper().startswith('SYNTHETIC'):
                reasons.append('synthetic_input_requires_synthetic_flag')
        except (ValueError, KeyError, TypeError, OSError, ArithmeticError) as error:
            session['exclusion_reasons'].append('invalid_recording')
            session['error'] = str(error)
            session['complete'] = False
        sessions.append(session)
    # Reject every member of a duplicate group, rather than selecting a favorable copy.
    for key, reason in [('data_sha256', 'duplicate_recording'), ('ticker', 'repeated_market')]:
        counts = Counter(s[key] for s in sessions if s.get(key))
        for s in sessions:
            if s.get(key) and counts[s[key]] > 1:
                s['exclusion_reasons'].append(reason)
    for s in sessions:
        s['included'] = bool(s.get('complete')) and not s['exclusion_reasons']
    included = [s for s in sessions if s['included']]
    pnls = [D(s['strategy_net_pnl']) for s in included]
    total = sum(pnls, D(0)) if included else None
    cash_curve = peak = drawdown = D(0)
    for s in sorted(included, key=lambda s: (s['last_observed_at'], s['ticker'])):
        cash_curve += D(s['strategy_net_pnl'])
        peak = max(peak, cash_curve)
        drawdown = max(drawdown, peak-cash_curve)
    reasons = Counter(r for s in sessions for r in s['exclusion_reasons'])
    identity = {'policy_version': 1, 'synthetic': synthetic,
                'inputs': [{'data_sha256': s.get('data_sha256'), 'included': s['included'],
                            'exclusion_reasons': s['exclusion_reasons'], 'error': s.get('error')}
                           for s in sessions],
                'implementation': provenance('cohort', vars(args))}
    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True, allow_nan=False).encode()).hexdigest()
    return {'evaluation_version': 1, 'created_at_utc': datetime.now(timezone.utc).isoformat(),
        'evaluation_fingerprint': fingerprint, 'identity': identity,
        'dataset_kind': 'synthetic_mechanics_demo' if synthetic else 'research_recordings',
        'parameters': {k: getattr(args, k) for k in PARAMETERS},
        'summary': {'submitted_sessions': len(sessions), 'included_sessions': len(included),
            'excluded_sessions': len(sessions)-len(included), 'exclusion_counts': dict(reasons),
            'included_strategy_net_pnl': str(total) if total is not None else None,
            'included_no_trade_net_pnl': '0' if included else None,
            'included_excess_net_pnl': str(total) if total is not None else None,
            'whole_cohort_net_pnl': str(total) if included and len(included) == len(sessions) else None,
            'mean_included_net_pnl': str(total/len(included)) if included else None,
            'positive_sessions': sum(p > 0 for p in pnls),
            'negative_sessions': sum(p < 0 for p in pnls),
            'flat_sessions': sum(p == 0 for p in pnls),
            'included_fills': sum(len(s['strategy']['fills']) for s in included),
            'included_fees_paid': str(sum((sum((D(f['fee']) for f in s['strategy']['fills']), D(0)) for s in included), D(0))),
            'included_exposure_seconds': round(sum(s['exposure_seconds'] for s in included), 6),
            'session_close_pnl_drawdown': str(drawdown) if included else None},
        'sessions': sessions,
        'limitations': [
            'Each session starts with the same cash; sums are independent-session P&L, not a compounded portfolio return.',
            'The no-trade baseline holds cash with zero interest, fees, fills, and exposure.',
            'Complete means observed active snapshots through final payout, not full coverage from market opening.',
            'Clock anomalies, recorded errors, crossed books, snapshot gaps above max_gap, and duplicate markets are excluded by a fixed policy; excluded sessions remain visible.',
            'Sampled drawdown excludes future exit costs and can understate losses between observations or when inventory has no mark.',
            'Exposure uses monotonic time where present; legacy recordings use wall time.',
            'Snapshot fills and configured fees/slippage are assumptions. These results do not establish a durable or statistically significant edge.',
            'Use new sessions with frozen parameters for evaluation; synthetic scenarios are mechanics tests only.']}


def save_report(report, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x') as f:
        f.write(json.dumps(report, indent=2, allow_nan=False)+'\n')


def overview(report):
    summary = report['summary']
    lines = [f"Evaluation: {report['dataset_kind']}",
             f"Sessions: {summary['included_sessions']} included / {summary['submitted_sessions']} submitted"]
    for s in report['sessions']:
        if s['included']:
            lines.append(f"  {s['ticker']}: strategy ${s['strategy_net_pnl']}; no-trade $0; excess ${s['excess_net_pnl']}")
        else:
            lines.append(f"  {s.get('ticker', s['recording'])}: EXCLUDED — {', '.join(s['exclusion_reasons'])}")
    value = summary['included_excess_net_pnl']
    lines.append(f"Included sessions' excess P&L: {'unavailable' if value is None else '$'+value}")
    if summary['whole_cohort_net_pnl'] is None:
        lines.append('Whole-cohort P&L unavailable: the submitted batch contains exclusions or no eligible sessions.')
    lines.append('Independent paper sessions; hypothetical fills and assumed costs. No established edge.')
    return '\n'.join(lines)
