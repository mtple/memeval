# Session reliability checks

The September 18 FreeTurtle report identified a Sept 12 run stalled at 22,247,000 ms.
Production logs showed HTTP and MCP requests reaching Vercel's 300-second timeout while
the public metadata endpoint still returned `running`. Those logs did not identify the
last command phase, so they alone cannot establish whether a database lock was held.

A local profile of the same recorded episode found a replay bottleneck in leakage scanning.
The scanner compared every payload key and string with every private term in the pack.
A 467-pool snapshot performed roughly 357 million length checks and spent 38 seconds in
scanning under cProfile. Rebuilding a session repeated that work for prior commands.

The scanner now indexes terms by their first four characters and caches repeated strings
within one scan. It still detects overlapping terms, keys, values, addresses, paths, dates
and absolute timestamps. The same profiled snapshot took 0.2 seconds after the change.
These are local measurements, not production latency guarantees.

Other changes:

- Commands acquire the run lock before rebuilding a cold session. Contending requests return
  `RUN_BUSY` without consuming a request or changing the clock. Lock acquisition is bounded;
  an executing owner's lock is not given an expiry.
- Phase logs separate pack loading, replay, lock acquisition, execution and persistence.
  Logs omit tokens, command arguments and delivered observations.
- `session.snapshot` supports `format: "compact"`. The same 467-pool data page decreased from
  903,417 to 163,228 JSON bytes, an 81.9% reduction. The default nested format is retained.
- The starter requests compact 25-pool pages and saves identity/run credentials atomically
  with owner-only permissions. `--resume` reuses saved runs without enrolling or creating runs.
  Custom strategy memory remains the policy author's responsibility.

A separate local cash-only run used Sept 12 data for all 24 simulated hours, issuing 3,279
commands through the run manager. A fresh manager rebuilt its state near hour 6.18 in 5.5
seconds after 890 commands. The full run completed in 29.2 seconds; a subsequent replay
took 22.6 seconds and matched both state and ledger hashes. Compact pages used at most
9,808 bytes. These checks did not advance, trade, finish or reset FreeTurtle's production run.

After commit `7f58842` reached production, a read-only request to the affected run's
`observed` endpoint returned HTTP 200 in 23.5 seconds. Phase logs recorded a cold rebuild
of all 99 commands in 22.9 seconds, including 1.1 seconds before replay. The response
contained 467 discovered pools and all five confirmed orders at the original 22,247,000 ms
clock. A subsequent metadata read confirmed the run was still running with no final report.
This verifies recovery of the original state; the agent must still resume with its existing
credentials and complete the remaining episode to establish a final result.

The test suite covers scanner equivalence, columnar timestamp scanning, snapshot values and
pagination, lock contention and subsequent recovery, PostgreSQL timeout/cleanup calls, and
starter interruption, token retention, file permissions and resumption. Live PostgreSQL tests
remain opt-in through `TEST_DATABASE_URL`; they never probe a user's local database.

## Terminal report recovery

FreeTurtle subsequently reached the full Sept 12 episode with 22 confirmed orders and
997,603,386,230,650,741 raw NATIVE remaining, with no pending orders or token inventory.
Its successful `session.finish` receipt was persisted, but report generation failed.
The supplied order receipts reproduce `decimal.InvalidOperation` in FIFO attribution at
Decimal precision 28: a raw-unit contribution formatted with 18 decimal places exceeds
that worker context. Import-time precision settings do not configure every worker thread.
Fraction formatting now uses integer division and exact digit placement, preserving
truncation toward zero without ambient precision or intermediate rounding.

An error-only report now has no result summary instead of zero fills and an invented
legacy metric. The report endpoint returns `REPORT_GENERATION_FAILED` with HTTP 503 so
clients display a report error rather than treating the error document as a report.

`POST /api/v1/runs/{run_id}/report/repair` repairs only a report-generation failure after
an otherwise successful `session.finish`. It follows the existing public-run write
policy, privacy checks and rate limits; private deployments require the admin token.
It acquires the run lock, restores the original recorded state if necessary, verifies
that it matches the stored terminal receipt, and builds the report before publishing it.
It issues no new session commands, creates no attempt, and preserves the original clock,
trace and finish timestamp. Repeated repair requests are idempotent. Other failures and
missing or mismatched terminal receipts are refused. Recovery provenance is recorded in
`report_recovery`; execution eligibility still evaluates every existing fidelity flag.

## Zero-output historical swaps

The 59 fidelity flags in FreeTurtle's Sept 12 report all have the code
`EXTERNAL_SWAP_FAILED_ON_PRIVATE_STATE`. A separate no-agent reconstruction reproduces
all 59 at the same timestamps, with the underlying error `INSUFFICIENT_OUTPUT_AMOUNT`.
Each corresponds to a recorded Uniswap v4 event with positive input and zero output.
None of the eight flagged pool aliases overlaps the three pools in FreeTurtle's 22
confirmed order receipts.

The historical adapter incorrectly required both legs to be positive. Core swap math
can consume input while the integer output rounds to zero, and the pool price can still
move. See the [Uniswap v4 core swap implementation](https://github.com/Uniswap/v4-core/blob/main/src/libraries/Pool.sol).
The reference reconstruction already accepted these recorded transitions, but the
private copy rejected them and retained its prior state.

Engine v3 permits zero output only when the historical event itself records zero
output. It commits the calculated pool state without emitting an exchange-price
observation, a zero-price candle, or a price alert. Agent orders retain their positive
output requirement. A positive-output historical event that cannot execute still
raises a fidelity flag. Reports count handled events under
`coverage_and_assumptions.external_zero_output_swaps`.

Verification on the complete Sept 12 pack after this change: 59 zero-output events
handled, zero fidelity flags, zero reconciliation mismatches, and all final private
pool states exactly equal to their reference states without agent intervention.
This check does not create a hosted attempt or overwrite FreeTurtle's original report.
The original report describes engine v2; it is not silently promoted to a result under
engine v3. Its original 22 fills and terminal balance remain preserved.

## Lost responses and premature finish

Billifer's `run_3eeee33398c5bec5` completed with no trades after its client reported
incomplete HTTP bodies, stale absolute clock targets, and an error path that called
finish. Production logs around 04:37 UTC on September 19 contain several `RUN_BUSY`
503 responses after five-second lock waits, followed by successful HTTP responses
at the terminal clock. These logs establish contention, but do not establish why
the client observed truncated bodies. No claim of a proven proxy or framing defect
follows from this evidence.

Commands now store a gzip-compressed, scanned response atomically with each new trace
row. A retry under the same request ID returns that receipt without executing again,
even after worker loss or completion. A different tool or arguments under the same ID
is a conflict. The envelope clock reflects the latest committed state; saved data
keeps its original observation time. Legacy trace rows have no retry receipt.
If a database write fails with an uncertain outcome, the worker discards its in-memory
session and reconstructs from durable state on retry.

HTTP JSON responses use normal buffered framing and negotiate gzip above 1 KB. The
starter reads and validates the complete response, persists pending commands before
sending, and retries transient failures under the original ID. Retry exhaustion stops
without finishing. Relative `advance_ms`, error clocks, and explicit `confirm: true`
for finish prevent the reported stale-clock/early-finish chain. Old successful finish
records still replay; newly refused finish records remain refused during replay.

Regression coverage includes concurrent large market/trade responses with exact
Content-Length and gzip integrity checks, injected response loss after snapshot and
order execution, a confirmed buy and sell after clock recovery, restart recovery,
uncertain database commits, and refusal of unconfirmed zero-trade finish. These local
checks do not prove every production connection will remain intact. Billifer's original
terminal run is preserved; these changes do not undo an already confirmed finish.


## Completed report inspection

On September 19, production logs for FreeTurtle's completed run
`run_4b027b4d34007f84` showed the observed-price endpoint replaying 515 commands in
82.004 seconds. A concurrent chart request and a decision-timeline request returned
503 while waiting for the same run lock. The trade table already used a saved review
and returned successfully without rebuilding the simulation.

The decision timeline now reads original durable trace records and recorded order
deliveries directly. It does not restore a session or acquire the trading lock. Final
order snapshots are saved with completed inspection views; older runs use their latest
recorded order deliveries until that snapshot is available. Missing legacy deliveries
are explicitly unavailable, rather than synthesized by rerunning the current engine.
The full response, including untrusted request arguments, retains pack-specific redaction.

Completed observed-price views are stored in the existing database. Finalization warms
the default run view and the full-episode charts for up to eight traded pools. This is
a cache-warming bound, not a limit on available pools or market data. Other views remain
available on demand. Durable cache variants are limited to the default one-minute
interval and the full-episode interval used by the trade-review UI. Other requested
intervals still return their exact result without creating additional durable variants.

An older completed run needs one reconstruction to populate the cache. It holds a
separate inspection lock with a 120-second acquisition deadline. A waiter rechecks the
stored view after acquiring that lock, so it can reuse the first worker's result.
PostgreSQL's statement timeout accommodates that explicit deadline; trading commands
retain their existing five-second acquisition limit and acquired locks never expire.
Active charts still share the trading lock because their simulation state can change.

Regression checks cover cold-worker reads, concurrent legacy chart reconstruction,
timeline access during contention, original delivery retention and redaction, pagination,
final order states, active-run serialization, and cache failures during finalization.
They also assert unchanged run records, command traces and usage after report inspection.
The change does not migrate the database, advance or rescore existing runs, or alter
collection jobs or execution rules.
