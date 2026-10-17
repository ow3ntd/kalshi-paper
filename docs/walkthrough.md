# Kalshi paper experiment

Python 3.10+ (3.12 recommended); standard library only. No account, keys, deposits, or dependencies.
This program only makes public GET requests. It contains no order submission code.

## Start in Terminal

```sh
cd kalshi-paper
python3 -m unittest -v
python3 paper.py markets
```

The list is the first 100 open non-combination markets, not a ranked recommendation.
Use `python3 paper.py markets --series SERIES_TICKER` to narrow it.
Choose a binary market with a two-sided book, read its settlement rules, and verify its fee settings.
Copy its actual ticker in place of YOUR_TICKER below:

```sh
python3 paper.py run --ticker YOUR_TICKER --file session-01.jsonl --minutes 30 --fee-coefficient 0.07
```

**0.07 is an illustrative quadratic taker fee coefficient, not a verified fee for your market.**
Only use this model where quadratic fees apply; set the actual coefficient including applicable multiplier.
Default rounding is conservatively up to one cent per one-contract fill. Fee schedules and sub-cent
accounting can differ; compare multiple cost assumptions before drawing conclusions.
The default adds $0.001 slippage on each leg, beyond the displayed spread.

Keep Terminal open and your Mac awake. Ctrl+C stops and writes a summary.
Each run requires a new filename; positions do not carry between runs.
Output: raw JSONL containing metadata, lifecycle observations, snapshots, and data errors;
plus a uniquely named JSON report under reports/ (or a new path supplied with --report).

## What it tests

At each snapshot, sum quantities at the three best YES bid and NO bid levels.
Imbalance = (YES quantity - NO quantity) / combined quantity.
When imbalance >=0.6 and YES spread <=$0.02, schedule a hypothetical one-contract
YES purchase. Execute at the first subsequent valid snapshot at least one second later,
using its YES ask plus slippage. YES ask is 1 minus the best NO bid.
After 60 seconds, schedule a sale at the next eligible snapshot's YES bid minus slippage.
Fees apply on both legs. One position at a time, no borrowing, no real money.
Only top-level displayed depth of at least one contract permits a hypothetical execution.

There is no trained model and no presumed edge. This is a directional, taker-style baseline,
not a spread-capture simulator. Two-second snapshots cannot measure subsecond opportunities,
queue priority, actual latency, or maker fills. Displayed liquidity may disappear before a real order
arrives; delays and slippage assumptions do not eliminate that uncertainty.
It records local request/receipt times, not exchange creation times. Network gaps cancel pending
instructions; existing positions remain open until a sale or observed final settlement.
Automatic market rollover is not modeled.

## Reproduce or stress-test

```sh
python3 paper.py replay --file session-01.jsonl --fee-coefficient 0.07
python3 paper.py replay --file session-01.jsonl --fee-coefficient 0.07 --slippage 0.005 --latency 3
```

Each replay saves a new report without changing raw data or overwriting earlier reports.
Check completed round trips, realized P&L, fees, and open positions. Reports include inventory
value at the last usable bid, unrealized P&L, and total equity/P&L before future exit costs. Missing
marks produce null equity for open positions. Bid marks can be stale; they are not guaranteed
liquidation proceeds. Realized P&L alone is NOT total portfolio return.

First collect several sessions to check reliability. Then fix parameters and evaluate NEW days and
independent events. Do not repeatedly optimize and report results on the same recording. Track daily
net results, data gaps, and all open exposure. No minimum trade count or paper profit proves an edge.
If a promising signal survives costs and new data, the next upgrade is authenticated WebSocket
recording and a more realistic execution model.

## Official references

- https://docs.kalshi.com/api-reference/market/get-markets
- https://docs.kalshi.com/api-reference/market/get-market-orderbook
- https://docs.kalshi.com/getting_started/fee_rounding
- https://kalshi.com/regulatory/fee-schedule

The public API can change. If schema parsing or network requests fail, preserve the recording and
inspect the error. Do not substitute missing prices with zero.

## Milestone 1: accounting you can explain

Open `portfolio.py`. It has no network or strategy dependencies. Each immutable Fill carries
side, price, quantity, fee, and time. Portfolio applies fills using Decimal arithmetic, rejects
unfunded buys and oversells, and allocates cost basis proportionally on partial exits.
The report preserves exact decimal strings in its fill journal; headline numbers are JSON floats.
This ledger currently supports long YES positions for one market only.

Run this offline example (synthetic data, not a profitable backtest):

```sh
python3 -m unittest -v
python3 paper.py replay --file accounting-demo.jsonl --fee-coefficient 0.07
```

Expected: 0 completed round trips, 1 open contract, $0 realized P&L, cash $999.479,
inventory bid value $0.40, equity $999.879, total P&L -$0.121 before exit costs.
The purchase cost is $0.501 plus a $0.02 entry fee. This demonstrates why a report with
zero realized losses can still contain a losing position. The report includes the fill journal.

Suggested learning sequence:
1. Read the cash movements in `Portfolio.apply` and work through the demo by hand.
2. Read `test_portfolio.py` and explain the reconciliation identity:
   total P&L = realized P&L + unrealized P&L (before future exit costs).
3. Run your own recording through replay and inspect all open exposure.
4. Next milestones: WebSocket ingestion, stronger event validation, CI, and out-of-sample research.

Known limitations remain: no automatic rollover, snapshot executions, assumed fees, no persisted
portfolio resume, and no data-gap-aware uncertainty analysis. No live-money path is implemented.

## Milestone 2: expiration and settlement

`lifecycle.py` separates trading eligibility from payout. Live mode polls market state before
requesting a book, recording each observation at local receipt time. It stops executing at the
earlier of close_time/expiration_time or whenever the observed status is not active. Pending
instructions and bid marks are cleared while closed. An open contract with no usable mark
has unknown equity, not an assumed zero loss or a guessed $1 payout.

Only finalized/settled, non-provisional observations can trigger settlement. An explicit
settlement_value_dollars is preferred; otherwise a final yes/no result maps to $1/$0.
A missing or unsupported payout leaves inventory unresolved. Intermediate determined results
do not release cash. Settlement pays quantity times payout, realizes the remaining cost basis,
and clears inventory. Duplicate identical final observations do not pay twice; conflicting
payouts raise an error for reconciliation. The ledger rejects subsequent trades.
Settlement adds no trading fee in this model; check the applicable product rules before research use.

```sh
python3 -m unittest -v
python3 paper.py replay --file settlement-demo.jsonl --fee-coefficient 0.07
```

The synthetic example buys at t=2, closes at t=3, and observes a final YES payout at t=5.
Expected: report_version 3, market_status finalized, open_contracts 0, cash/equity 1000.479,
realized_net_pnl 0.479, and settlement.cash_credit "1.0000". completed_round_trips remains 0
because settlement is not a sale. This is an accounting example, not profit evidence.

Read `Lifecycle.can_trade`, then `Experiment.ingest`, then `Portfolio.settle` in that order.
In the demo, change only result to no AND settlement_value_dollars to "0.0000" in a copied
fixture: expected cash 999.479 and realized P&L -0.521. Omitting the final event entirely
leaves 1 open contract and null equity. Do not change original recordings to improve results.

Live recording still ends at --minutes (or Ctrl+C). If final settlement has not arrived then,
the report remains unresolved; it does not continue monitoring in the background. There is no
portfolio resume or rollover yet. Starting a new run with an old finalized ticker creates a new,
empty portfolio, not settlement of a previous session. Old recordings without market events
retain legacy snapshot behavior and explicitly report lifecycle_tracking false.

Data errors and slow responses clear pending instructions and marks in live and replay mode.
Lifecycle state must be fresh within --max-gap before snapshots can execute. Polling cannot
detect an early closure before it is observed, so actual fills remain approximate.
The reference schema is https://docs.kalshi.com/api-reference/market/get-market.

## Milestone 3: reproducible experiments

Every new CLI report records a SHA-256 fingerprint of the exact data bytes, the simulation
parameters, source modules, and the Python/Decimal runtime settings. The combined
experiment fingerprint identifies these inputs; it does not prove data authenticity or accuracy.
Creation time and file locations are not part of experiment identity. Avoid editing source
files while an experiment is running. Legacy reports without provenance cannot use compare.

```sh
python3 -m unittest -v
python3 paper.py replay --file settlement-demo.jsonl --fee-coefficient 0.07 --report reports/repeat-a.json
python3 paper.py replay --file settlement-demo.jsonl --fee-coefficient 0.07 --report reports/repeat-b.json
python3 paper.py compare reports/repeat-a.json reports/repeat-b.json
```

Expected: same_data, same_parameters, same_code, same_runtime, same_experiment,
same_results, and reproduced are all true. Changed result fields is empty.
Changing a fee or execution assumption should produce a different experiment even if the
result coincidentally stays the same. For example:

```sh
python3 paper.py replay --file settlement-demo.jsonl --fee-coefficient 0.07 --slippage 0.005 --report reports/stress-a.json
python3 paper.py compare reports/repeat-a.json reports/stress-a.json
```

Expected: same_data true; same_parameters, same_experiment, same_results, and reproduced false.
The stress example has cash 1000.475 instead of 1000.479.
Reusing an explicit report name gives an error to preserve the existing result; choose a new
name or omit --report to generate a unique name. Old FILE.report.json files remain untouched.

Replay validates event types, finite local timestamps, request-before-receipt ordering,
chronological order, and a single ticker, with line numbers on failures. These are basic checks,
not complete schema or exchange-sequence validation. Interrupted or invalid replays do not
publish success reports. Reproduction demonstrates consistency, not trading realism or an edge.

Read research.py: replay_recording validates evidence, provenance identifies inputs, and
compare_reports distinguishes changes in inputs from changes in results. Research tests exercise
the actual command-line program in separate processes, not just calls to the same object.

## Milestone 4: observable public-data collection

Start with a data-only session. No strategy, fees, credentials, or portfolio is needed:

```sh
python3 -m unittest -v
python3 paper.py collect --series KXBTC15M --minutes 5 --file live-01.jsonl
python3 paper.py quality --file live-01.jsonl
```

The collector paginates open markets in the series and selects the earliest-closing active
binary contract whose open time has passed and which has at least 300 seconds (five minutes)
remaining before the earlier of close or expiration. A known close time is required.
Use `--min-remaining-seconds 120` to change this threshold; it must be finite and nonnegative.
The boundary is inclusive; 0 allows any unexpired contract. Selection is a deterministic convenience, not a
recommendation or liquidity filter. It stays with that one ticker; no automatic rollover.
You can instead supply --ticker EXACT_TICKER, which bypasses the automatic selection filter.
If no eligible contract exists, collection explains the threshold and exits without creating a
recording. Try later or lower `--min-remaining-seconds`.
Use a new --file name each session or omit --file to generate one under recordings/.

The terminal prints a heartbeat roughly every 10 seconds (requests may delay it): status,
snapshot count, error count, and elapsed seconds. Keep Terminal open and Mac awake.
Ctrl+C saves all completed events and writes a summary. A hard kill can leave a truncated final
JSON line; quality/replay report its line number instead of silently discarding it.

Each completed event is flushed with a local sequence number, wall-clock receipt time, monotonic
elapsed time, and successful-request duration. Requests are public REST GETs, normally two per
two-second cycle. Network errors, including HTTP 429, cause capped exponential backoff.
Malformed decoded API payloads are retained as error evidence with raw_response; they are not
replaced with fabricated prices. HTTP failures log status/message, not necessarily the response body.
Filesystem write failures stop collection rather than being treated as network failures.

At the end, FILE.quality.json summarizes snapshots, lifecycle updates, errors, empty books (`empty_snapshots`), one-sided books
(`one_sided_snapshots`), two-sided books,
usable price snapshots, locked/crossed books, request duration percentiles, observation gaps, and
median spread. "Usable" means two positive interior prices with bid < ask, not guaranteed execution
or sufficient depth. One-sided books remain valid observations but cannot supply a two-sided spread.
Zero-quantity levels do not count as quotes. These three book counts partition all snapshots.
The quality command also computes them for existing recordings without modifying the recording
or its saved quality report.
Local wall-clock corrections are flagged by quality; replay suspends trading after detected discontinuities.
Preserve and inspect those sessions instead of treating wall timestamps as reliable holding periods.

The diagnostics do not pass or fail a strategy. Check first that snapshots > 0, errors are understood,
and timing gaps fit what happened. A large gap can be a network problem, Mac sleep, or time spent
waiting for settlement with no new books. Request duration measures a data fetch; it is NOT quote age,
exchange processing time, or future order latency. Slow snapshots remain in the evidence; replay
uses --max-gap to exclude slow responses from strategy execution.

Read collector.py in this order: choose_market, collect, quality. Its tests inject a fake HTTP client
and clock, so rate limits, malformed responses, interruption, and recovery are tested without sleeps.

After inspecting the data, replay the saved recording using explicit fee assumptions:

```sh
python3 paper.py replay --file live-01.jsonl --fee-coefficient 0.07
```

The coefficient is still illustrative and must be verified for actual research conclusions.
No model has been trained. Two-second polling still misses intermediate events; authenticated
WebSocket snapshots/deltas and sequence-gap recovery are the next data-feed milestone.
Official schemas: https://docs.kalshi.com/api-reference/market/get-markets and
https://docs.kalshi.com/api-reference/market/get-market-orderbook.

If automatic selection keeps failing, inspect its exact checks instead of guessing from ticker text:

```sh
python3 paper.py markets --series KXBTC15M --details --min-remaining-seconds 600
```

This prints the computer's UTC time, raw open/close/expiration times, remaining seconds, and
rejection reasons. It uses the same eligibility function as the selector and creates no recording.
The listing covers the first API page; collection paginates the series. The diagnostic is a fresh
observation, so availability can change between listing and collection. Don't infer close time
or timezone solely from a ticker name, or bypass the filter before understanding the rejection.

## Milestone 5: distinguish wall time from elapsed time

```sh
python3 -m unittest -v
python3 paper.py quality --file live-03.jsonl
```

Wall time is a calendar timestamp and can change. Monotonic elapsed time is the collector's
elapsed clock. Its behavior during machine sleep depends on the platform; it is not proof of
continuous network availability. Request durations and new snapshot gaps use this elapsed clock.

Quality now reports:
- wall_clock_observation_span_seconds: last minus first event wall timestamp. The old
  observation_span_seconds field remains as an alias for compatibility.
- monotonic_observation_span_seconds: last minus first event elapsed_s, only when every
  observation supplies this field. It excludes time before the first and after the last observation.
- last_observation_elapsed_seconds: elapsed time from collector start to the last observation.
- monotonic_recording_duration_seconds: collector start to recording_end, including final wait.
  Older recordings have no end marker, so this is null rather than an invented duration.
- wall_minus_monotonic_span_seconds and wall_clock_steps: disagreement between clocks.
  Adjacent observation deltas differing by more than 0.1 seconds are listed with sequence numbers.
  This diagnoses timestamp disagreement; it cannot establish the OS-level cause.
- recording_end_observed and recording_stop_reason: new recordings distinguish duration limit,
  final settlement, and Ctrl+C. A hard kill may leave no end marker.

The original live-03 file has a 323.558-second wall span but a 298.874-second monotonic observation
span. Events 167→168 have wall delta 26.694460s and monotonic delta 2.009647s, a 24.684813s
disagreement. The last observation occurred at elapsed 299.129299s. The original recording
is preserved; quality does not overwrite its saved .quality.json sidecar.

New collector_version 2 recordings append recording_end and measure the full recording duration.
Polling stops when the monotonic deadline is reached, but an in-flight request or scheduling delay
can overshoot it. Reading a recording and writing its quality report happen after the end marker.

Replay uses wall timestamps for lifecycle and monotonic time for strategy durations when available.
It suspends trading for the session after detected wall/monotonic discontinuities. Quality
can diagnose backward wall jumps if ordered monotonic timestamps are present; legacy wall-only
files still require chronological timestamps. This does not reconstruct an authoritative calendar
clock or claim to correct historical P&L after a clock anomaly.

For another live check:

```sh
python3 paper.py collect --series KXBTC15M --minutes 5 --min-remaining-seconds 600 --file live-04.jsonl
python3 paper.py quality --file live-04.jsonl
```

Look for full duration near 300s, an end marker, and clock-step count 0. New collection still
needs an eligible market; no qualifying market means no recording is created.

## Milestone 6: strategy elapsed time and conservative anomaly handling

`clocks.py` selects monotonic elapsed_s when present. Pending-order latency, holding periods,
observation gaps, and lifecycle-information age use that clock. Calendar close/expiration checks,
settlement observations, and fill journal timestamps keep their wall-clock meaning. Missing
elapsed_s in every event selects legacy wall time; switching clock modes mid-recording fails.
Measured request_duration_ms controls slow-response exclusion when recorded, instead of a wall
subtraction that can be distorted by clock changes.

On a detected clock discontinuity (>0.1s disagreement between adjacent clock deltas), pending
instructions are canceled and trading remains suspended for that recording. No forced sale or
invented liquidation value is used. Existing inventory may settle when a final market update
arrives; otherwise total P&L can remain unknown. New reports expose strategy_clock,
clock_discontinuities, trading_suspended_for_clock, and report_version 4.

```sh
python3 -m unittest discover -v
python3 paper.py replay --file live-02.jsonl --fee-coefficient 0.07
python3 paper.py replay --file live-03.jsonl --fee-coefficient 0.07
```

The original local sessions are not in Git. With the current baseline and assumed fees, live-01
had no trades; live-02 had one purchased contract settle for a loss of $0.211. Live-03 suspends
at its clock anomaly with one unsold contract, so its partial realized P&L is not total performance.
These few sessions do not establish profitability. Preserve the fixed baseline and collect new,
unseen sessions before evaluating any strategy changes.
