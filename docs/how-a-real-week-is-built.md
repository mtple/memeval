# How a real week is built

This page explains, in order, what happens between the operator recording a past week and that
week appearing on the leaderboard. It also says how the tests stand in for the blockchain. The
runbook covers the commands; this page covers the mechanism.

## Days, not weeks

The unit the hosted site ships is one UTC day, named `base_day_YYYY-MM-DD` and labelled "Base day
of YYYY-MM-DD". Everything below that says "week" applies to a day in the same way; `make day
START=...` is `make week` with a 24-hour period and the launches universe.

The reason is size, measured on the week of 2026-09-07. That week saw about 3,500 Uniswap v2,
1,100 v3 and 25,400 v4 pools created on Base. Recording every one of them with at least one swap
came to 1.63 million events after 60 percent of the week, so about 2.7 million for the week: a
gzipped tape near 300 MB, over a gigabyte of engine memory and a cold start measured in minutes.
Vercel functions are limited to a 250 MB bundle. Raising the minimum swap count does not help,
because the events sit in the busy pools: keeping only pools with 100 or more swaps still keeps
75 percent of the events. A day is 135,000 to 400,000 events and 15 to 40 MB gzipped, which fits
with room for several. A week with every launch is still recordable (`make week`) for a
self-hosted server with a persistent process; the GitHub workflow records it in 40-minute slices.

## The short version

1. The operator runs one command on their own machine with a week's start date. Users never
   request weeks; the weeks on the site are the ones that have been recorded and committed.
2. The command reads trade data straight from the Base blockchain through the operator's RPC
   endpoint, in one uninterrupted run of about two hours. Nothing comes from a price API. It
   picks a fixed set of pools, fetches every swap and liquidity event for them across the week
   and the day before it, and reconstructs each pool's state at the start.
3. The result is written as a pack, an immutable folder of data files whose id is the hash of
   its contents.
4. A validator replays the whole week with no agent present and checks, after every recorded
   trade, that the model's pool state equals what the chain recorded. A pool that cannot be
   reproduced is removed from the tradable set, with the reason written down.
5. If the remaining pools pass every check, the pack qualifies as research data and the command
   leaves it under `weeks/` in the repository. Otherwise it says which gate failed and the
   operator does not commit it.
6. The operator commits and pushes. Vercel builds the repository into the site, so every server
   instance has the week on its own disk. On its first request the server registers the week and
   it becomes a leaderboard tab named "Base week of YYYY-MM-DD".

## Step 1. Recording a week

`make week START=2026-09-07` builds a fixed configuration for that week: every venue, which
today means Uniswap v2 pairs and Uniswap v4 pools (v4 is where Clanker and Bankr launches trade),
a request budget, and the pool selection rules below. The venues are recorded one after the other
and merged into one pack, so the leaderboard has one tab per week.

## Step 2. Running on a real disk

The command runs on the operator's machine (or, optionally, on a GitHub Actions runner through the
`collect-week` workflow), so it has hours and a disk. Progress is checkpointed under
`weeks/<name>_work/`, which git ignores: an interrupted run resumes from its last chunk when the
same command runs again, and the working files are removed when the week is finished. The request
budget is the only spending guard needed. Nothing about recording touches the deployed site or its
database.

## Step 3. Choosing the pools

The universe has two parts, and neither part is chosen with any knowledge of what happened
inside the week.

Every launch. Every pool created on Uniswap v2, v3 or v4 during the week with an ETH leg is in
the recording, provided it saw at least one swap. Nothing is sampled. Most of these pools die
within a handful of trades, and that is the point: finding the one launch worth trading among
thousands is the skill a discovery agent is supposed to have, so the noise stays in. A launch
becomes visible to the agent at its creation time plus the availability delay, the moment the
chain showed it, and nothing later than the creation itself decides whether it is in the
universe.

Established pools. A fixed set of pools that were already trading before the week, chosen by
creation order among those active before the window: by default four v2 pairs, four v3 pools and
eight v4 pools. They give the agent a market that exists on day one.

The older sampled universe, sixteen pools with launches chosen by reaching their twentieth swap,
is still available as `--universe sampled` for local experiments. It is not what the site offers:
a hand-picked cast has no noise to discover, so the sampled week that was on the site was
withdrawn once the first all-launches day shipped. Removing a pack directory from the repository
withdraws it on the next deploy; runs already finished on it keep their reports.

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

Launches need no starting state, since they are born inside the recording. Their events are
read in bulk: v4 pools all share one contract, so one scan of it covers every v4 launch, and v2
and v3 launches are read in batches of addresses. A week of launches is far larger than the
established set, so its tape is stored compressed and the server streams it from disk instead
of holding every row in memory.

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

## Step 7. When a week fails the check, or the checker improves

A week that fails stays on the operator's disk with its validation report and is not committed.
When the validator or the engine changes what a recorded week contains, the operator runs
`market-replay packs revalidate weeks/<name>` on the committed weeks: it re-runs the current
validator over the existing files, with no RPC requests, demotes any pool the current engine
cannot reconcile, and rewrites the pack in place (its id changes with its pool records). The
result is committed like any other change. A week is recorded from the chain again only when the
collector itself changed.

## Step 8. On the leaderboard

When the site starts on a new deployment it registers every pack directory under `weeks/` from
its committed validation report, verifying the file hashes and replaying nothing. The database
row for a week holds its name, dates, qualification and summary; the files stay in the checkout.
Runs on a week are scored on the same pack forever; the id in every run report says which one.
Artificial practice weeks are labelled as such and listed after the real ones.

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
- `tests/integration/test_public_access.py` checks that a pack directory under `weeks/` is
  registered on start, served from the checkout by a fresh instance with an empty scratch
  directory, and playable end to end, with no upload path left on the server.
- `tests/unit/test_engine_clmm.py` builds a concentrated-liquidity pack in process and checks
  validation, replay, quotes, fills and valuation, including a tampered record (one mismatch
  and one correction), a hook-absorbed swap (explained, still research) and a pool that must
  be demoted while the rest of the pack qualifies.
- `tests/unit/test_clmm_math.py` checks the v3 math against the vectors from the Uniswap
  v3-core repository.

The hosted deployment runs a smoke test on every deploy, and the `status` workflow prints every
recorded week, its gate summary and the leaderboard tabs on demand.
