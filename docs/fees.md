# KXBTC15M fee verification and replay

Checked October 17, 2026 UTC (October 16 Pacific) against Kalshi's public API and official
fee documentation. This is a dated observation, not a guarantee about past or future fills.

## What was verified

- Series `KXBTC15M`: `fee_type=quadratic`, `fee_multiplier=1`.
- Market `KXBTC15M-26OCT162030-30`: parent event `KXBTC15M-26OCT162030`, no returned
  fee override or waiver. Series and event fee-change endpoints returned empty lists.
- The published taker formula is `M * 0.07 * C * P * (1-P)` before rounding: multiplier,
  contracts, and price in dollars. This supports effective coefficient `0.07` for the
  checked metadata. The simulator crosses the spread, so its fills are modeled as taker fills.
- The published schedule specifies no settlement fee. Broker fees may differ.

Sources: [series API](https://docs.kalshi.com/api-reference/market/get-series),
[event overrides](https://docs.kalshi.com/api-reference/events/get-event-fee-changes),
[fee schedule](https://kalshi.com/docs/kalshi-fee-schedule.pdf).

## What changed in the simulator

The detailed [rounding documentation](https://docs.kalshi.com/getting_started/fee_rounding)
describes a trade fee rounded upward to six decimal dollar places, followed by a rounding
adjustment that aligns signed cash movement. Buys have negative revenue; sells positive.
The balance grid is $0.0001 for direct members and $0.01 for non-direct members.
The PDF's formula footnote describes centicent alignment, while its general examples still
show whole-cent fees. The implementation follows the explicit API rounding mechanics;
it has not been reconciled against authenticated account fills.

Choose the model explicitly:

| CLI model | Behavior |
|---|---|
| `legacy` (default) | Old fee-only rounding using `--fee-quantum` (default $0.01) |
| `direct` | Trade fee rounded upward to $0.000001, then cash movement floored to $0.0001 |
| `non-direct` | Same trade fee precision, cash movement floored to $0.01 |

Account category is not inferred from a public API response. The two new models are scenarios
until the applicable category is confirmed. Non-direct simulations exclude any additional
broker charges. `--fee-quantum` is rejected with the new models to avoid contradictory settings.
Starting cash must align to the chosen grid. Fill revenue supports up to six dollar decimals.

For example, one contract bought at $0.055 with coefficient 0.07 incurs $0.003639 trade fee.
The non-direct rounding adjustment is $0.001361, giving a $0.005 total fee and $0.06 cash debit.
Rounding the fee alone would instead charge $0.01. Rounding may also differ between buys and sells.

Each simulated order is exactly one contract filled once. Its rounding accumulator begins at
zero; no rebate is due within that single fill. Independent orders do not share excess rounding.
Multi-fill orders and maker orders require additional execution modeling before use.

## Step-by-step commands

Run these from the repository root.

1. Refresh the public series-level check:

   ```sh
   python3 paper.py fees --series KXBTC15M
   ```

   This does not check a particular event's overrides. It prints the scope and saves timestamped
   responses, response hashes, and source URLs under `reports/`.

2. Check a specific market, including its event and paginated event fee-change records:

   ```sh
   python3 paper.py fees --ticker KXBTC15M-26OCT162030-30
   ```

   Use the actual ticker printed by collection for new sessions. Review overrides, waivers,
   and scheduled change records. Unknown fee types or missing multipliers produce an error,
   rather than an invented zero rate. This inspection does not automatically change parameters.

3. Evaluate fixed parameters using either documented account scenario:

   ```sh
   python3 paper.py evaluate --files recordings/*.jsonl --fee-coefficient 0.07 --fee-model direct
   python3 paper.py evaluate --files recordings/*.jsonl --fee-coefficient 0.07 --fee-model non-direct
   ```

   The coefficient is the effective rate after any multiplier, not the multiplier itself.
   Verify it again for other markets or dates. Replay stays offline; today's metadata is not
   applied silently to old recordings. Omit `--report` to generate a new file each time.

4. Read `fee_assumptions` and `fee_details` in each session's strategy report. Fee details list
   trade fee, rounding adjustment, rebate (zero for this order model), and net fee. The selected
   model enters both replay and evaluation fingerprints. The original inputs and reports remain
   unchanged; fingerprints from the older implementation differ because code/report schema changed.

## Comparison on the existing complete session

All three runs use the same 11 hypothetical fills, coefficient 0.07, and strategy parameters.
They finish with zero open contracts. These are fee-model comparisons, not historical account
reconciliation: the metadata check happened after the recording.

| Model | Net fees | Net P&L | No-trade P&L |
|---|---:|---:|---:|
| Legacy | $0.1700 | -$0.2140 | $0 |
| Direct | $0.1061 | -$0.1501 | $0 |
| Non-direct, excluding broker add-ons | $0.1560 | -$0.2000 | $0 |

Correcting the model reduces the simulated loss in this session. Neither new scenario beats
no-trade. More observations and realistic execution checks are still required to assess an edge.
