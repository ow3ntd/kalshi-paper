# Kalshi Paper Research

A Python research system for recording public Kalshi market data, replaying it through a
paper strategy, and examining execution assumptions, portfolio accounting, and timing quality.

**Paper only.** There is no real-order submission code, trained model, or established profitable
strategy. Synthetic examples verify mechanics; they are not evidence of a trading edge.

## Quick start

Requires Python 3.10 or newer; developed and tested locally with Python 3.12.
Uses the standard library only. Offline examples need no API keys or account.
Run these commands from the repository root:

```sh
python3 -m unittest discover -v
python3 paper.py replay --file settlement-demo.jsonl --fee-coefficient 0.07
```

The synthetic winning settlement ends with cash $1,000.479, zero open contracts, and
realized P&L $0.479. Losing and pending-settlement fixtures are also included.
**The fee coefficient is an illustrative assumption**, not verified for every Kalshi market.

## Record public data

```sh
python3 paper.py collect --series KXBTC15M --minutes 5 --min-remaining-seconds 600 --file live-01.jsonl
python3 paper.py quality --file live-01.jsonl
```

Selection requires an active binary contract with at least ten minutes remaining before the
earlier of close or expiration. If no contract qualifies, collection exits without creating a
recording. Use a new filename per session, or omit `--file` to generate one under `recordings/`.
Collection follows one contract, prints progress, and stops at the time limit or observed final
settlement. Ctrl+C preserves completed events. There is no background monitoring or rollover.

To diagnose selection:

```sh
python3 paper.py markets --series KXBTC15M --details --min-remaining-seconds 600
```

Public requests require internet access. The collector backs off after data-fetch errors and
retains invalid decoded responses as error evidence. Quality reports describe coverage,
one-sided books, spreads, request durations, gaps, and wall/monotonic clock disagreement.

## Reproduce an experiment

```sh
python3 paper.py replay --file settlement-demo.jsonl --fee-coefficient 0.07 --report reports/a.json
python3 paper.py replay --file settlement-demo.jsonl --fee-coefficient 0.07 --report reports/b.json
python3 paper.py compare reports/a.json reports/b.json
```

Identical data, parameters, source, and runtime should produce `reproduced: true`.
Explicit report names cannot overwrite existing files; omit `--report` for a unique name.
Reports include input hashes and an exact-decimal fill journal. Headline metrics are JSON floats.
Legacy reports without provenance cannot use the comparison command.

## Architecture

| Module | Responsibility |
|---|---|
| `collector.py` | Market selection, public snapshot recording, recovery, and quality diagnostics |
| `lifecycle.py` | Trading eligibility and final-payout interpretation |
| `portfolio.py` | Decimal cash, cost basis, inventory, realized/unrealized P&L, and settlement |
| `research.py` | Replay validation, experiment fingerprints, and result comparison |
| `paper.py` | CLI and baseline strategy orchestration |

The baseline buys one YES contract when top-three-level book imbalance exceeds a threshold
and the spread is sufficiently narrow. Hypothetical execution occurs at a later snapshot,
with spread, configurable slippage, and fees. It attempts to exit after a holding period, or
credits an observed final settlement if the position remains open.

## Verification

The offline test suite covers accounting reconciliation, partial exits, expiration, provisional
and duplicate settlements, replay consistency, report preservation, selection boundaries,
malformed responses, rate-limit recovery, interruption, and clock discontinuities.
Tests inject a clock and HTTP client; no live API or credentials are required.
GitHub Actions is configured to run the suite on Python 3.10 and 3.12. Local verification
alone does not establish that those hosted jobs have passed; check the repository Actions tab.

## Current limitations

- Two-second snapshots miss intermediate events and cannot establish queue priority or maker fills.
- Displayed prices/depth do not guarantee executable liquidity. Hypothetical fills remain approximate.
- Fees and slippage are configurable assumptions; settlement currently assumes no added fee.
- Replay uses wall time for strategy holding periods and rejects detected wall/monotonic discontinuities.
- One market and long YES inventory only; no portfolio resume, automatic rollover, or WebSocket feed.
- Open inventory can remain unresolved when a recording ends. Realized P&L alone is not total return.
- No statistical evidence of an edge or live-money execution path is provided.

Live recordings, generated reports, environments, and credentials are excluded by `.gitignore`.
The committed JSONL fixtures are explicitly synthetic. Detailed development exercises and field
explanations are in [the walkthrough](docs/walkthrough.md).

## Research direction

Separate elapsed-time strategy execution from lifecycle timestamps; add authenticated WebSocket
recording and sequence-gap recovery; then evaluate fixed baselines on unseen sessions with realistic
cost sensitivity and uncertainty estimates. These are planned work, not implemented capabilities.

## References

- [Kalshi market API](https://docs.kalshi.com/api-reference/market/get-markets)
- [Order-book API](https://docs.kalshi.com/api-reference/market/get-market-orderbook)
- [Fee rounding](https://docs.kalshi.com/getting_started/fee_rounding)
- [Fee schedule](https://kalshi.com/regulatory/fee-schedule)
