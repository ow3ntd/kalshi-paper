# Collect consecutive sessions

The batch command records public data only. It selects the earliest-closing eligible contract,
records it through final payout or a time limit, then selects the next distinct eligible contract.
It does not place simulated or real orders; evaluation is a separate offline command.

## 1. Start a bounded batch

From the repository root:

```sh
python3 paper.py batch --series KXBTC15M --sessions 3 --max-minutes 60 --session-max-minutes 25 --min-remaining-seconds 300 --directory recordings/batch-01
```

On macOS, prevent idle sleep for the lifetime of the command:

```sh
caffeinate -is python3 paper.py batch --series KXBTC15M --sessions 3 --max-minutes 60 --session-max-minutes 25 --min-remaining-seconds 300 --interval 3 --directory recordings/batch-02
```

Keep the lid open, power connected, and the network available. `-i` prevents idle system
sleep; `-s` also requests system sleep prevention on AC power. These assertions end with
the command. They do not repair network outages or guarantee protection from lid closure,
forced sleep, shutdown, or loss of power. The display can turn off.

The live recorder and batch discovery now stop on wall/monotonic divergence greater than
0.1 seconds (`clock_discontinuity`). This catches suspend intervals that the platform's
monotonic clock does not count, as well as calendar clock adjustments. The partial recording
and manifest remain evidence; the batch does not continue to another contract. Existing
replay quality exclusions stay in force. A stopped batch is not necessarily settled.

HTTP 429 responses wait at least 30 seconds before retrying. Valid `Retry-After` seconds or
HTTP dates can extend that delay, bounded by the remaining run budget. Backoff starts after
the failed response. Discovery and fee lookups also honor this delay. A recovered error still
appears in the quality report and does not become eligible merely because a later request worked.

- `--sessions 3` limits attempted contracts, including failures and timeouts.
- `--max-minutes 60` limits the entire run using monotonic time, including discovery and fee
  lookups. An in-flight HTTP request can finish after the deadline (the client has a 10-second
  timeout); the runner does not start another discovery request after that deadline.
- `--session-max-minutes 25` caps recording/waiting per selected contract. The global deadline
  clips this limit when less batch time remains.
- `--min-remaining-seconds 300` requires five minutes remaining at selection. If nothing
  qualifies, the process waits and tries again every 15 seconds. Network/invalid-response errors
  use capped backoff; `--retry-seconds` changes the starting retry interval.
- `--interval 2` controls snapshot polling. Leave the terminal open and computer awake.
- `--directory` must name a new directory. Omit it to generate one under `recordings/`.

Run the command once. Watch `BATCH WAIT`, `BATCH session=...`, and the regular collector
heartbeats. Completion prints `BATCH STOP` and the manifest location. Ctrl+C stops the entire
batch, preserving completed events and marking the active attempt interrupted.

The attempt count deliberately does not mean three successful settlements. A settlement
may be delayed, the API may fail, or the time budget may run out. Those outcomes stay visible.
Already-attempted tickers are excluded within this batch, including timed-out contracts still
listed as active. Separate batch directories do not share a duplicate registry.

## 2. Inspect the saved evidence

The directory contains:

```text
batch.json                 Current state/final manifest, updated by atomic replacement
events.ndjson              Append-only progress and failure journal
sessions/0001.jsonl         First attempted recording, when recording began
sessions/0001.jsonl.quality.json
fees/0001.json              Dated public fee check, when successful
```

`batch.json` includes settings, every attempt and its ticker, relative paths, stop reason,
recording hash, snapshot/error counts, and whether final payout was observed. `settled_sessions`
is an observation count, not a count of profitable or evaluation-eligible sessions.

Fee checks happen before each recording. A failed fee lookup is recorded but does not discard
raw data. Inspect its error before relying on fees; the batch never substitutes a coefficient
into evaluation automatically. Fee-change records/waivers require review, and a lookup at the
start does not guarantee that fees remained constant afterward.

The journal is flushed after each event; the manifest is checkpointed at state transitions.
Disk failures stop the batch. Hard termination, power loss, or disk failure can leave the last
manifest marked `running`; completed bytes and the journal remain evidence, not a completed run.
This version does not resume batches. Use a new directory for a new run.

## 3. Evaluate the stopped batch

```sh
python3 paper.py evaluate --batch recordings/batch-01 --fee-coefficient 0.07 --fee-model direct
```

Use `non-direct` for that account scenario and verify the coefficient for the recorded markets.
Neither flag authenticates an account; see [fee verification](fees.md).

The evaluator reads every attempted path from the manifest, including missing recordings when
collection stopped before the first event. Those are reported as exclusions rather than silently
omitted. It refuses a manifest still marked `running`. The manifest hash enters the evaluation
fingerprint, along with the usual data/code hashes and fixed strategy parameters.

Read these fields in order:

1. `batch.stop_reason`, `batch.attempted_sessions`, `batch.selection_errors`, and
   `batch.fee_check_failures` describe collection coverage and outstanding fee checks.
2. `summary.included_sessions` and `summary.excluded_sessions` describe evaluation eligibility.
   An observed settlement still needs active snapshots and adequate data quality.
3. `summary.included_excess_net_pnl` compares the eligible sessions with no-trade.
4. `summary.whole_cohort_net_pnl` remains `null` when any attempt is excluded. Inspect each
   excluded session's reasons, including unresolved inventory, rather than treating it as $0.

Each session begins with the same paper cash. The sum is independent-session P&L, not a
compounded account return. Selection/fee-lookup failures do not establish a trading edge.

## Coverage limits

Sequential collection may miss a following contract while waiting for the prior settlement,
or skip one with too little lifetime remaining. It does not guarantee every scheduled market,
full market-lifetime coverage, or every order-book event. This version does not schedule future
runs, collect concurrently, or tune strategy parameters. Repeat batches with fixed parameters
and preserve failed attempts when assessing performance.
