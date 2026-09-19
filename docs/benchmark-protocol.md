# Benchmark protocol

## Trading objective

Maximize final settled ETH (NATIVE), or CASH in generated episodes.
The primary return is (final settled cash - starting cash) / starting cash.
Agents must submit their own sell orders before the episode ends and allow time for confirmation.
Unsold tokens and unconfirmed sale proceeds do not count. Reserved but unspent cash does count.
There is no automatic liquidation. Liquidatable portfolio value and portfolio drawdown remain
secondary diagnostics, including explicit unknown valuations. Old portfolio-scored reports
remain readable but are excluded from rankings and paired scores; agents must run again
under this objective. The default play flow permits that new run.


## Suites

A suite freezes: pack list, bankroll, export policy (revealable/sealed), mask seed schedule, engine
seed, isolation. Shipped: `generated-practice-v1` (four generated weeks), `generated-sealed-v1`
(same packs, sealed), `generated-dev-v1` (two-hour fixture). `suites.yaml` lives in the data
dir and is editable only before sealing. Bankroll `1.0 CASH` is a software default, not a
recommendation.

## Runs

Standalone weekly run: declared cash bankroll, no inherited positions or memory; agent
memory across held-out runs is unenforced for trusted clients. Every attempt is recorded
(`attempts` table). Runs with revealable exports (`mode: "practice"` in the API) may be exposed
(dates/mappings disclosed via admin export);
the run is then labeled `exposed` and is no longer "unseen".

## Comparison

`POST /api/v1/comparisons {suite_id, agent_a, agent_b}` pairs runs per pack: same pack,
same execution profile (hash), same mask seed schedule, same bankroll. Warnings:
`MISMATCHED_EXECUTION_PROFILES`, `MISMATCHED_CAPABILITIES`, `DIFFERENT_MASK_SEEDS:*`,
`UNPAIRED_EPISODE:*`, `MIXED_DATA_ORIGINS_NOT_POOLED`, `FEW_DISTINCT_PERIODS_DESCRIPTIVE_ONLY`.
Per-episode differences come first; then median/mean/min/max and a/b better counts. All
attempted runs are shown including crashes and incomplete runs. No significance test,
promotion verdict, "probability of profitable live trading" or number of additional trades.

## Evidence counting

Unique calendar periods, chains, stochastic trials (extra runs per pack) and mappings are
counted separately. Different chains in the same week are not independent regimes. Generated
and historical results are never pooled; neither are different execution profiles.

## Reproducibility

Every run report carries `ledger_hash`, `state_hash`, `trace_hash`, `result_hash`, pack id,
execution parameter hash, engine/report/validator versions, mask and engine seeds (admin
only). `POST /runs/{id}/replay` re-executes the recorded actions on a fresh session and
compares hashes. Deterministic agents reproduce exactly; stochastic agents vary only through
`agent_seed`.

## Study registry

`POST /api/v1/studies` freezes candidates, evaluator version, pack ids, comparison id,
intended outcome and prediction; later outcomes are attached without changing predictive
validity, which stays `not_established` until a documented analysis exists. Held-out simulated
periods test generalization inside the modeled environment only.

## The honest conclusion

"This version did better in these episodes; the sample and execution assumptions do not
establish future improvement."

