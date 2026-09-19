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
