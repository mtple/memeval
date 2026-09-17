# How a real week is built

This page explains, in order, what happens between someone asking for a past week on the
website and that week appearing on the leaderboard. It also says how the tests stand in for
the blockchain. The runbook covers the knobs an operator turns; this page covers the mechanism.

## The short version

1. A visitor picks a week. The server queues a collection job for it.
2. Every minute a cron tick runs the job for about 200 seconds, then saves its progress. The
   next tick continues from there, on whatever server instance picks it up.
3. The job reads trade data straight from the Base blockchain through the configured RPC
   endpoint. Nothing comes from a price API. It picks a fixed set of pools, fetches every swap
   and liquidity event for them across the week and the hour before it, and reconstructs each
   pool's state at the start.
4. The result is written as a pack, an immutable folder of data files whose id is the hash of
   its contents.
5. A validator replays the whole week with no agent present and checks, after every recorded
   trade, that the model's pool state equals what the chain recorded. A pool that cannot be
   reproduced is removed from the tradable set, with the reason written down.
6. If the remaining pools pass every check, the pack qualifies as research data, is stored in
   the database, and becomes a leaderboard tab named "Base week of YYYY-MM-DD".

## Step 1. Asking for a week

The Weeks page posts a week start date. The server turns it into a job with a fixed
configuration, including which venues to read. The default is every venue, which today means
Uniswap v2 pairs and Uniswap v4 pools. v4 is where Clanker and Bankr launches trade. An
operator can request a single venue instead; when a merged week for the same dates is later
built and qualifies, the single-venue weeks for those dates are retired.

Each job carries the collector version that created it. When the collector or the checker
changes in a way that affects what a finished week contains, that version string is bumped,
and older weeks that failed are looked at again (step 7).

## Step 2. Running in slices

A serverless function cannot run for two hours, so the job runs for a slice of about 200
seconds per invocation. At the end of a slice the collector saves its checkpoints, its
coverage ledger and one raw-log file per pool into the database, uploading only files that
changed and only every 2,000 requests. The next tick, on any instance, downloads those files
if it does not already have them and continues. A tick that finds a slice already running
elsewhere returns at once instead of waiting.

Two spending guards apply. Each week has a request budget, and the whole service has a
rolling cap on RPC requests per 24 hours. When the cap is reached, ticks stop until earlier
requests age out of the window, then continue on their own. An operator can also pause every
tick with one environment variable.

## Step 3. Choosing the pools

The set of pools is frozen before any price inside the week is read, so nothing that happened
later can influence which pools are in the game.

For v2, the collector reads the factory's pair-creation events and keeps pairs with a wrapped
ETH leg that were already trading before the week, earliest created first.

For v3 and v4, half the slots go to pools that were active before the week, chosen the same
way. The other half go to launches from inside the week, ranked by when each pool reached its
twentieth swap. A launched pool becomes visible to agents at that moment, so an agent never
sees a pool before the chain had shown twenty trades in it.

## Step 4. Reading the chain

Everything comes from `eth_getLogs` calls in block ranges, one range at a time, sized to the
provider's limit and halved when the provider complains. Every call and its response is
receipted, and a coverage ledger records which block ranges have been read for which pool, so
a gap is recorded as a gap and never treated as "no trades".

For a v2 pair, the tape holds every Swap, Mint, Burn and Sync event from one hour before the
week to its end. Sync events carry the pair's reserves after each change, so they are exact
checkpoints. The starting reserves come from calling `getReserves` at the block where the
prehistory hour begins.

For a v3 or v4 pool, the state cannot be read as a pair of reserves. The collector fetches
every liquidity event from the pool's creation, folds them into a map of liquidity per tick as
of the start, and takes the price and tick from the last swap before the start. Swap events
from the prehistory onwards carry the price, tick and active liquidity after each swap, so
every swap is a checkpoint. v4 hook addresses and dynamic fees are recorded per pool.

Token names are not part of the data agents see. Inside a session every pool and token has a
generic alias, so an agent cannot look the week up.

## Step 5. Packaging

The collector writes a pack: assets, pools, the ordered tape, a block-to-time table, coverage,
an inventory of pools that were excluded and why, and a manifest. The pack id is the hash of
these files. Loading a pack verifies every hash, so a changed byte makes it unloadable. The
format is documented in [dataset-format.md](dataset-format.md).

## Step 6. Checking

Before the pack is used, the validator replays the tape with no agent and compares the model
against the chain at every checkpoint, in exact integers.

For v2, every Sync row re-anchors the model to the chain. A Sync with no matching swap (a
direct token transfer followed by `sync()`) is an explained adjustment. A swap that paid two
tokens in, which the plain v2 model cannot express, becomes a net reserve adjustment. Any
other difference, even one wei, is a material mismatch.

For v3 and v4, every swap is replayed with the exact Uniswap v3 math and the recorded fee, and
the resulting price, tick and liquidity must match the event exactly. A swap the math cannot
replay at all, which is what a hook that absorbed a whole leg leaves in the log, anchors the
model to the recorded state as an explained adjustment. Any other difference is a material
mismatch, and the model is anchored to the chain so one bad record does not spoil the rest.

A pool with a material mismatch or a fidelity flag is demoted. It stays in the pack as data,
so the prices it saw still count when valuing a holding, but agents cannot trade in it, and
the inventory records the reason. The week then has to pass every gate on the pools that
remain: identity, universe, event ordering, coverage, execution state, mechanics, valuation,
anonymization, provenance and reproducibility. A week that passes is marked research. A week
that fails is marked diagnostic only and stays off the leaderboard.

The live engine applies the same anchoring rules while an agent trades, and reports the count
of explained and material adjustments on every run.

## Step 7. When a week fails the check

A failed week is not deleted. On an idle tick the server looks for weeks that failed under an
older collector version. It first re-runs the current validator over the existing pack, with
no RPC requests, and applies any demotions to the pack in place. That gives the pack a new id;
the week follows it and the old id is dropped. Only if the pack still fails is the week
collected again from the chain. Each version bump does this at most once per week.

## Step 8. On the leaderboard

A research pack is imported into the database, archived as one compressed file so any
instance can serve it, and listed as a leaderboard tab labelled by its dates. Runs on it are
scored on the same pack forever; the id in every run report says which one. Artificial
practice weeks are labelled as such and listed after the real ones.

## What a real week does not model

Gas is assumed zero. Tokens are assumed to transfer without taxes or limits. There is no MEV
and no routing across pools. A hook's own fee on an agent's fill is the last fee the hook
charged in the recording, not the hook's rule. Every report states these limits, and the
[accuracy roadmap](accuracy-roadmap.md) lists them in the order they will be addressed.

## How the tests cover this

No test touches the real chain. The integration tests run the real collectors against fake RPC
servers that answer `eth_getLogs` and `eth_call` from in-memory logs.

- `tests/integration/test_collector.py` has `FakeBase`, a v2 world with one factory and one
  pair. Switches make it fail the first request, rate-limit once, receive a token donation
  (an orphan Sync) or hide a reserve drift that must lead to demotion. The tests cover resuming
  after an interruption, normalization of every event kind, and reconciliation.
- `tests/integration/test_collector_cl.py` has `FakePoolManager`, which emits the same world
  as v4 PoolManager logs or as v3 factory and pool logs. Each fake pool is driven by the real
  `ClPoolState`, so every emitted swap is consistent with the exact math and the collected
  tape must reconcile to zero mismatches.
- `tests/integration/test_weeks.py` runs the week jobs end to end against those fakes: slicing
  across fresh instances, the merged all-venue week retiring single-venue weeks, the daily
  request cap, pausing, throttled uploads, rebuilds and revalidation without extra requests.
- `tests/unit/test_engine_clmm.py` builds a concentrated-liquidity pack in process and checks
  validation, replay, quotes, fills and valuation, including a tampered record (one mismatch
  and one correction), a hook-absorbed swap (explained, still research) and a pool that must
  be demoted while the rest of the pack qualifies.
- `tests/unit/test_clmm_math.py` checks the v3 math against the vectors from the Uniswap
  v3-core repository.

The hosted deployment runs a smoke test on every deploy that requests the latest complete week
and drives its collection for a few minutes, and the `status` workflow prints every week job,
its validation report and the leaderboard tabs on demand.
