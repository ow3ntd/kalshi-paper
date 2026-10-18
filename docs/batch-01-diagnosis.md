# Batch 01 interruption diagnosis

The original batch manifest and all three recording hashes matched the saved
`reports/batch-01-direct.json` evaluation. No original evidence was changed.

All three contracts eventually reached an observed final payout, but all three were excluded
for clock discontinuities. Session 1 also contained nine HTTP errors: one 429 response,
one network-unreachable error, and seven name-resolution errors. The records do not establish
why the server rate-limited the request or the independent cause of every network error.

macOS `pmset -g log`, inspected on October 18, confirms these October 17 events in Eastern time:

- 11:24:35: entered Idle Sleep, waking at 11:26:33.
- 11:26:48: entered Clamshell Sleep, followed by repeated maintenance wakes and sleeps.

These align with the recording interruptions. The first detected clock divergence was about
116 seconds; session 1 accumulated about 25,553 seconds more wall time than monotonic time.
The old batch reported only 576 seconds of monotonic duration despite spanning hours of
calendar time. Its monotonic-only budget did not include these suspend intervals.

The fix stops live collection and the entire batch after detected clock divergence, preserves
partial evidence, and avoids silently continuing into later contracts. Rate-limit handling
also uses a minimum 30-second delay and respects Retry-After. A fresh macOS run uses caffeinate
with the lid open and AC power. Timing checks and evaluation exclusions remain strict.

The excluded replay P&Ls were -$0.196, $0, and $0 under the explicit direct fee scenario.
They are diagnostics, not an eligible whole-batch performance result.
