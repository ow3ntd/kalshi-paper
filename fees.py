"""Single-fill taker fees and dated, public fee-metadata inspection.

Every simulated order fills exactly one contract once. No accumulator is shared
between orders, and multi-fill orders / maker executions are not modeled.
"""
import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal as D, ROUND_CEILING, ROUND_FLOOR
from urllib.parse import quote

BALANCE_PRECISION = {'direct': D('.0001'), 'non-direct': D('.01')}
ROUNDING_URL = 'https://docs.kalshi.com/getting_started/fee_rounding'
SCHEDULE_URL = 'https://kalshi.com/docs/kalshi-fee-schedule.pdf'


def ceil_to(value, quantum):
    return (value/quantum).to_integral_value(rounding=ROUND_CEILING)*quantum


def legacy_fee(price, coefficient, quantum):
    return ceil_to(coefficient*price*(1-price), quantum)


def single_fill_fee(side, price, coefficient, model='legacy', quantum=D('.01')):
    """Exact fee components for a new order with one one-contract fill."""
    if side not in ('buy', 'sell'):
        raise ValueError('Unknown fill side')
    if not price.is_finite() or not 0 < price < 1 or not coefficient.is_finite() or coefficient < 0:
        raise ValueError('Invalid fee price or coefficient')
    if model == 'legacy':
        if not quantum.is_finite() or quantum <= 0:
            raise ValueError('Invalid legacy fee quantum')
        return {'trade_fee': None, 'rounding_fee': None, 'rebate': D(0),
                'net_fee': legacy_fee(price, coefficient, quantum)}
    if model not in BALANCE_PRECISION:
        raise ValueError('Unknown fee model')
    if price != price.quantize(D('.000001')):
        raise ValueError('Single-fill revenue must have at most six decimal dollar places')
    precision = BALANCE_PRECISION[model]
    revenue = -price if side == 'buy' else price
    trade_fee = ceil_to(coefficient*price*(1-price), D('.000001'))
    change = revenue-trade_fee
    aligned = (change/precision).to_integral_value(rounding=ROUND_FLOOR)*precision
    rounding_fee = change-aligned
    # New order, one fill: accumulated excess is < precision, so no rebate.
    return {'trade_fee': trade_fee, 'rounding_fee': rounding_fee, 'rebate': D(0),
            'net_fee': trade_fee+rounding_fee}


def fee_parameters(settings):
    model = settings.get('fee_model', 'legacy')
    return {'model': model, 'liquidity_role': 'taker',
            'effective_coefficient': str(settings['fee_coefficient']),
            'coefficient_source': 'explicit_research_parameter',
            'balance_precision_dollars': str(BALANCE_PRECISION[model]) if model in BALANCE_PRECISION else None,
            'legacy_fee_quantum': str(settings['fee_quantum']) if model == 'legacy' else None,
            'order_model': 'one_contract_one_fill_per_order',
            'rounding_reference': ROUNDING_URL if model != 'legacy' else None,
            'settlement_fee_dollars': '0',
            'note': 'Account category is explicitly selected, not authenticated. Broker add-ons and multi-fill rebates are not modeled. A current metadata check does not prove historical fees.'}


def _ticker(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', value):
        raise ValueError('Invalid ticker')
    return value


def effective_taker_terms(series, event=None):
    """Resolve fields independently; a zero event multiplier is a real override."""
    event = event or {}
    kind = event.get('fee_type_override')
    if kind is None:
        kind = series.get('fee_type')
    multiplier = event.get('fee_multiplier_override')
    if multiplier is None:
        multiplier = series.get('fee_multiplier')
    if kind not in ('quadratic', 'quadratic_with_maker_fees', 'quadratic_with_combo_maker_fees'):
        raise ValueError(f'Unsupported or missing fee type: {kind!r}; no coefficient inferred')
    if isinstance(multiplier, bool) or multiplier is None:
        raise ValueError('Missing or invalid fee multiplier')
    multiplier = D(str(multiplier))
    if not multiplier.is_finite() or multiplier < 0:
        raise ValueError('Invalid fee multiplier')
    return {'fee_type': kind, 'fee_multiplier': str(multiplier),
            'taker_coefficient_before_waivers': str(D('.07')*multiplier),
            'event_override_present': any(event.get(k) is not None for k in ('fee_type_override', 'fee_multiplier_override'))}


def inspect_fees(getter, series_ticker=None, ticker=None, now=None):
    """Read current terms and change records, keeping response evidence. Never set replay parameters."""
    if (series_ticker is None) == (ticker is None):
        raise ValueError('Choose either series or market ticker')
    observed = now or datetime.now(timezone.utc)
    evidence = []

    def fetch(path, **params):
        response = getter(path, **params)
        encoded = json.dumps(response, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
        evidence.append({'endpoint': path, 'parameters': params, 'response': response,
                         'response_sha256': hashlib.sha256(encoded).hexdigest()})
        return response

    event = None
    waiver = None
    event_changes = []
    if ticker is not None:
        _ticker(ticker)
        market = fetch('/markets/'+quote(ticker, safe=''))['market']
        if market['ticker'] != ticker:
            raise ValueError('Market response ticker mismatch')
        event_ticker = _ticker(market['event_ticker'])
        event = fetch('/events/'+quote(event_ticker, safe=''))['event']
        if event['event_ticker'] != event_ticker:
            raise ValueError('Event response ticker mismatch')
        series_ticker = _ticker(event['series_ticker'])
        waiver = market.get('fee_waiver_expiration_time')
        cursor, seen = None, set()
        while True:
            params = {'event_ticker': event_ticker, 'limit': 100}
            if cursor:
                params['cursor'] = cursor
            result = fetch('/events/fee_changes', **params)
            changes = result['event_fee_changes']
            if not isinstance(changes, list) or any(c['event_ticker'] != event_ticker for c in changes):
                raise ValueError('Invalid event fee change response')
            event_changes.extend(changes)
            cursor = result.get('cursor')
            if not cursor:
                break
            if not isinstance(cursor, str) or cursor in seen:
                raise ValueError('Invalid or repeated fee pagination cursor')
            seen.add(cursor)
    _ticker(series_ticker)
    series = fetch('/series/'+quote(series_ticker, safe=''))['series']
    if series['ticker'] != series_ticker:
        raise ValueError('Series response ticker mismatch')
    changes = fetch('/series/fee_changes', series_ticker=series_ticker, show_historical='true')['series_fee_change_arr']
    if not isinstance(changes, list) or any(c['series_ticker'] != series_ticker for c in changes):
        raise ValueError('Invalid series fee change response')
    terms = effective_taker_terms(series, event)
    return {'fee_check_version': 1, 'observed_at_utc': observed.isoformat(),
            'series_ticker': series_ticker, 'ticker': ticker,
            'scope': 'market_current_metadata' if ticker else 'series_only_event_overrides_unchecked',
            **terms, 'fee_waiver_expiration_time': waiver,
            'series_fee_changes': changes, 'event_fee_changes': event_changes,
            'references': [SCHEDULE_URL, ROUNDING_URL,
                'https://docs.kalshi.com/api-reference/market/get-series',
                'https://docs.kalshi.com/api-reference/events/get-event-fee-changes'],
            'evidence': evidence,
            'warning': 'Current metadata, not a historical fee reconstruction or account-specific quote. Review any change records and market waiver before selecting a replay coefficient; broker charges may differ.'}
