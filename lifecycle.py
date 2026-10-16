"""Conservative single-market lifecycle. Observation time controls knowledge."""
from datetime import datetime
from decimal import Decimal

def timestamp(value: str | None) -> float | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Lifecycle timestamps must include a timezone')
    return parsed.timestamp()

class Lifecycle:
    def __init__(self):
        self.status = 'unknown'
        self.deadline = None
        self.ticker = None
        self.last_observed = None

    def update(self, market: dict, observed_at: float) -> Decimal | None:
        ticker = market['ticker']
        if self.ticker not in (None, ticker):
            raise ValueError('Cannot mix market tickers')
        deadlines = [timestamp(market.get(k)) for k in ('close_time', 'expiration_time')]
        deadline = min((t for t in deadlines if t is not None), default=None)
        payout = None
        # Determined/closed does not imply final. Never infer payout from quotes.
        if market.get('status') in ('finalized', 'settled') and not market.get('is_provisional', False):
            value = market.get('settlement_value_dollars')
            if value not in (None, ''):
                payout = Decimal(str(value))
            elif market.get('result') in ('yes', 'no'):
                payout = Decimal(1 if market['result'] == 'yes' else 0)
            if payout is not None and (not payout.is_finite() or not 0 <= payout <= 1):
                raise ValueError('Invalid final payout')
        self.ticker = ticker
        self.status = market.get('status', 'unknown')
        self.deadline = deadline
        self.last_observed = observed_at
        return payout

    def can_trade(self, now: float) -> bool:
        return self.status == 'active' and (self.deadline is None or now < self.deadline)
