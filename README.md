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
The example uses the `legacy` fee model so its expected numbers remain stable. A coefficient
alone does not specify rounding or account category. See [fee verification](docs/fees.md)
for the dated KXBTC15M check and documented cash-alignment models.

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

## Collect through settlement and evaluate a batch

For a session that follows one contract through its final payout:

```sh
python3 paper.py collect --series KXBTC15M --until-settlement --max-wait-minutes 30 --min-remaining-seconds 300
```

This mode replaces the five-minute recording limit. The 30-minute cap starts when recording
begins and includes trading and waiting for settlement. The collector keeps polling market
status after trading closes. Only an observed, non-provisional final payout completes it;
`determined` alone is insufficient. Timeout or Ctrl+C preserves an incomplete session.
Keep the terminal running and the computer awake. No eligible market means the command
exits; rerun it when a contract meets the selection criteria. Each run generates a unique filename.

After collection finishes, evaluate all the recordings in that directory with fixed parameters:

```sh
python3 paper.py evaluate --files recordings/*.jsonl --fee-coefficient 0.07
```

This command retains legacy fee rounding. Select `--fee-model direct` or `--fee-model non-direct`
explicitly to model the corresponding documented balance precision after verifying the
coefficient for the market and date. All strategy flags from `replay`,
including `--slippage`, `--latency`, `--hold`, and `--threshold`, also work with `evaluate`.
It prints a compact comparison and saves the full report under `reports/` using a unique name.
No-trade holds the same starting cash, earns no interest, and places no orders. The same
recorded observations are delivered to both portfolios. Every session starts with fresh
paper cash; summed P&L is not a compounded or concurrently funded portfolio return.

Read the report in this order:

1. `included_sessions`, `excluded_sessions`, and each session's `exclusion_reasons`.
   Eligibility requires snapshots while a market was observed active, a final payout,
   resolved inventory, and the quality checks below. Complete describes the recorded window
   through settlement; collection may begin after market opening.
2. `included_strategy_net_pnl` versus `included_no_trade_net_pnl` (zero).
   `included_excess_net_pnl` is the difference, after simulated fees and slippage.
   These monetary summary fields use exact decimal strings; `null` means unavailable.
3. `whole_cohort_net_pnl`. It is `null` if any submitted session is excluded. The included
   subset can show a gain while the entire batch remains unresolved; report both counts.
4. Fees, fills, positive/negative/flat sessions, and per-session `exposure_seconds` and
   `max_inventory_cost_dollars`. Zero trades are retained as a valid zero result when complete.
5. Per-session `sampled_max_drawdown_before_exit_costs` and `unknown_equity_observations`.
   This indicative bid-mark drawdown omits future exit costs, gaps, and unobserved prices.
   `session_close_pnl_drawdown` uses only included final session P&Ls in end-time order;
   it does not measure intraday or overlapping-portfolio risk.

Fixed exclusions: missing final payout, unresolved inventory, no active snapshots, invalid
recordings, detected clock jumps, recorded data errors, locked/crossed books, snapshot gaps
above `--max-gap`, and duplicate recordings or repeated market tickers. Every member of a
duplicate group is excluded, so file order cannot choose the better result. Exclusions are
not evidence of zero P&L and are never silently removed from the report.

The report records source/data hashes, parameters, runtime, and an evaluation fingerprint.
Keep the initial parameters fixed on new sessions. Choosing settings after inspecting their
profits turns those sessions into development data; evaluate the next version on new sessions.

For offline mechanics checks, run these **separately** (they share a synthetic ticker):

```sh
python3 paper.py evaluate --files settlement-demo.jsonl --fee-coefficient 0.07 --synthetic
python3 paper.py evaluate --files settlement-loss-demo.jsonl --fee-coefficient 0.07 --synthetic
python3 paper.py evaluate --files settlement-pending-demo.jsonl --fee-coefficient 0.07 --synthetic
```

Expected: +$0.479, -$0.521, and an excluded incomplete session, respectively. The no-trade
baseline has zero P&L. These fixtures verify mechanics and provide no evidence of an edge.

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
| `evaluation.py` | Complete-session eligibility, no-trade comparison, risk summaries, and cohort provenance |
| `fees.py` | Public fee metadata inspection and single-fill taker fee rounding |
| `clocks.py` | Separate elapsed strategy time from calendar lifecycle time |
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
- Fee coefficients and account category are explicit research inputs; slippage remains assumed.
  `legacy` rounds the fee alone and exists for reproducibility. `direct`/`non-direct` implement
  documented single-fill cash alignment. Each simulated order has one fill; multi-fill
  accumulators, maker executions, and broker add-ons are not modeled. Settlement fee is zero.
- Replay uses monotonic time for strategy durations when available and calendar time for lifecycle
  checks. A detected clock discontinuity suspends simulated trading for the rest of that session;
  inventory can still settle on an observed final payout. Reports identify affected sessions.
- Legacy recordings without elapsed timestamps use wall time; mixed clock modes are rejected.
- One market and long YES inventory only; no portfolio resume, automatic rollover, or WebSocket feed.
- Open inventory can remain unresolved when a recording ends. Realized P&L alone is not total return.
- No statistical evidence of an edge or live-money execution path is provided.

Live recordings, generated reports, environments, and credentials are excluded by `.gitignore`.
The committed JSONL fixtures are explicitly synthetic. Detailed development exercises and field
explanations are in [the walkthrough](docs/walkthrough.md).

## Research direction

Gather more complete sessions with fixed parameters, refresh market-specific fee checks, and evaluate
cost sensitivity on new sessions. Add authenticated WebSocket recording, sequence-gap recovery,
and uncertainty estimates once data coverage supports them. These are planned capabilities.

## References

- [Kalshi market API](https://docs.kalshi.com/api-reference/market/get-markets)
- [Order-book API](https://docs.kalshi.com/api-reference/market/get-market-orderbook)
- [Fee rounding](https://docs.kalshi.com/getting_started/fee_rounding)
- [Fee schedule](https://kalshi.com/regulatory/fee-schedule)
