"""Exact-decimal, long-only YES accounting, independent of strategy and network."""
from dataclasses import dataclass, field
from decimal import Decimal

ZERO = Decimal('0')

@dataclass(frozen=True)
class Fill:
    side: str
    price: Decimal
    quantity: Decimal
    fee: Decimal
    timestamp: float

@dataclass
class Portfolio:
    initial_cash: Decimal
    cash: Decimal = field(init=False)
    quantity: Decimal = ZERO
    cost_basis: Decimal = ZERO
    realized_pnl: Decimal = ZERO
    fees: Decimal = ZERO
    fills: list[Fill] = field(default_factory=list)
    settlement: dict | None = None

    def __post_init__(self):
        if not self.initial_cash.is_finite() or self.initial_cash < ZERO:
            raise ValueError('Initial cash must be finite and nonnegative')
        self.cash = self.initial_cash

    def apply(self, fill: Fill) -> None:
        if self.settlement is not None:
            raise ValueError('Cannot trade a settled portfolio')
        p, q, f = fill.price, fill.quantity, fill.fee
        if not all(x.is_finite() for x in (p, q, f)) or not ZERO < p < 1 or q <= ZERO or f < ZERO:
            raise ValueError('Invalid fill price, quantity, or fee')
        if fill.side == 'buy':
            cost = p*q+f
            if cost > self.cash:
                raise ValueError('Insufficient cash')
            self.cash -= cost
            self.quantity += q
            self.cost_basis += cost
        elif fill.side == 'sell':
            if q > self.quantity:
                raise ValueError('Cannot sell more than held')
            allocated = self.cost_basis*q/self.quantity
            proceeds = p*q-f
            self.cash += proceeds
            self.realized_pnl += proceeds-allocated
            self.quantity -= q
            self.cost_basis -= allocated
        else:
            raise ValueError('Unknown fill side')
        self.fees += f
        self.fills.append(fill)

    def settle(self, payout: Decimal, timestamp: float) -> None:
        """Credit final YES payout once. Settlement is not an exchange trade."""
        if not payout.is_finite() or not ZERO <= payout <= 1:
            raise ValueError('Settlement payout must be between 0 and 1')
        if self.settlement is not None:
            if Decimal(self.settlement['payout_per_contract']) != payout:
                raise ValueError('Conflicting settlement; manual reconciliation required')
            return
        proceeds = payout*self.quantity
        pnl = proceeds-self.cost_basis
        self.settlement = {'payout_per_contract': str(payout), 'quantity': str(self.quantity),
                           'cash_credit': str(proceeds), 'net_pnl': str(pnl),
                           'observed_at': timestamp}
        self.cash += proceeds
        self.realized_pnl += pnl
        self.quantity = ZERO
        self.cost_basis = ZERO

    def valuation(self, mark: Decimal | None) -> dict:
        """Indicative bid mark, not guaranteed liquidation or settlement proceeds."""
        if mark is not None and (not mark.is_finite() or not ZERO <= mark <= 1):
            raise ValueError('Invalid mark')
        value = self.quantity*mark if mark is not None else (ZERO if not self.quantity else None)
        unrealized = value-self.cost_basis if value is not None else None
        equity = self.cash+value if value is not None else None
        return {'cash': self.cash, 'open_contracts': self.quantity,
                'open_position_cost_including_entry_fee': self.cost_basis,
                'realized_net_pnl': self.realized_pnl, 'fees_paid': self.fees,
                'inventory_bid_value': value, 'unrealized_pnl_before_exit_costs': unrealized,
                'equity_before_exit_costs': equity,
                'total_pnl_before_exit_costs': equity-self.initial_cash if equity is not None else None}
