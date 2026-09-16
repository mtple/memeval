# Implementation report

Prepared 2026-09-16 by the implementing agent. This report separates completed software,
data obtained, assumptions, failed checks and remaining work. It does not report a
historical evaluator as complete.

## 1. Software status: v1 software-complete for the generated lane

| Requirement (brief §3) | Status | Where |
|---|---|---|
| Language-neutral HTTP market API + MCP wrapper over the same handler | done | `service/app.py`, `service/mcp_server.py`, `engine/session.py` |
| Python and TypeScript client examples | done (3 py + 1 py model client, 3 ts) | `sdk/`, `agents/examples/` |
| Deterministic event-driven engine with virtual time | done | `engine/simulation.py` |
| Blinded aliases and server-side time-limited observations | done | `domain/identity.py`, `observations/`, `engine/session.py` |
| Single-chain weekly packs + shorter dev fixtures | done (4 generated weeks + 2h fixture) | `datasets/generator.py`, `fixtures/generated/gen_dev_short` |
| Exact-input spot swaps in a supported pool model | done (`cpmm_fixed_flow_v1`) | `venues/cpmm/` |
| Portfolio, pending-order, fee, valuation accounting | done | `broker/`, `engine/simulation.py` |
| Dataset import, validation, immutable manifests, coverage reports | done (10 gates) | `datasets/` |
| Base/EVM ingestion path for a bounded, verified pool family | done, tested against a fake RPC and exercised once against the public Base RPC | `collectors/` |
| No-key generated fixtures exercising the whole application | done | `market-replay demo` |
| Browser UI: episodes, agent setup, run, results, compare, data health | done (React/Vite; Playwright smoke test with screenshots) | `apps/web/` |
| Exportable machine-readable traces + human-readable reports | done | `/api/v1/runs/{id}/export`, `/report` |
| Adversarial tests and documented validation limitations | done: 83 tests + 1 browser test; environment-validation report | `tests/`, `data/environment_validation.json` |

Non-goals honored: no wallet, no signing keys, no transactions, no paid infrastructure, no
data purchase, no changes to any existing trading system. The broker has no client for any
real execution provider. Default external spend authorization is zero.

## 2. Verification commands executed (this environment, 2026-09-16)

```
uv venv --python 3.12 .venv && uv lock && uv sync --extra dev        # Python 3.12.11, pinned in uv.lock
.venv/bin/python -m pytest tests -m "not browser" -p no:cacheprovider  # 83 passed, 1 deselected (~15 s)
.venv/bin/ruff check src tests sdk agents                              # All checks passed
pnpm install && pnpm --filter web build                                # tsc --noEmit strict + vite build OK (249 kB JS)
.venv/bin/python -m pytest tests/browser -m browser                    # 1 passed; screenshots in tests/browser/output/
.venv/bin/market-replay demo --quick                                   # 2-hour fixture, 3 participants, ~2 s wall
.venv/bin/market-replay fixtures generate --no-dev                     # 4 full weeks, 19.8 s wall total
.venv/bin/market-replay run-agent --agent cash_only --suite generated-practice-v1   # 4 weeks, 16.5 s wall
.venv/bin/market-replay demo                                           # full suite; see §4
.venv/bin/market-replay verify                                         # environment validation + 6 sensitivity runs, 6.0 s
.venv/bin/market-replay import-report-fixtures                         # diagnostic_only pack from report excerpts
.venv/bin/python scripts/export_schemas.py                             # 12 schema files
BASE_RPC_URL=https://mainnet.base.org .venv/bin/market-replay collect --config collection/base_v2_slice_example.yaml  # §5
```

Tests block every non-loopback socket (`tests/conftest.py`), so no test can call a provider.

### Acceptance-test coverage (brief §19)

| # | Test | Location |
|---|---|---|
| 1–6 | as-of cutoffs, future range, open candle, missing minute, late observation, cache keys | `tests/unit/test_observations.py`, `tests/unit/test_session.py` |
| 7 | future pools not enumerable | `test_session.py::test_future_pools_not_enumerable` |
| 8–9 | same-symbol distinct assets; large integers through Python/JSON/TypeScript | `tests/unit/test_quantities_identity.py` |
| 10–13 | HTTP success + app error; empty tax unknown; trade page completeness; snapshot-only not executable | `tests/unit/test_importer_validator.py` |
| 14–17 | unsupported curves; CPMM reference; round trip; fee once | `tests/unit/test_cpmm_math.py`, `tests/property/` |
| 18–29 | reconciliation, private reserves, split orders, quote expiry/min-out, atomic reservation, idempotency, lifecycle gas, no-route inventory, missing-data headline null, non-mutating valuation, end-of-week, burn overdraw | `tests/unit/test_simulation.py` |
| 30 | agent credentials cannot reach control plane / other runs | `tests/integration/test_api.py` |
| 31–32 | leakage scan of all agent payloads; restricted runner controls (honest unenforced) | `tests/security/test_leakage.py` |
| 33 | Python and TypeScript equivalent; MCP reaches same handler | `test_api.py` |
| 34–35 | any in-scope token investigable; two unrelated participants without engine changes | `test_session.py`, `test_api.py` |
| 36 | no live provider / transaction in demo and tests | socket guard + `test_api.py` |
| 37 | pause/resume and action replay reproduce ledger/report hashes | `test_api.py`, `test_session.py` |
| 38 | per-run state isolation | `test_session.py::test_runs_do_not_share_state` |
| Evidence | report excerpts: open candle, missing minute, incomplete page, empty taxes, provider disagreement, quote-only | `test_importer_validator.py` |

## 3. Datasets: provenance and limitations

| Pack | Origin | Use status | Notes |
|---|---|---|---|
| `gen_dev_short` (committed) | generated_fixture | demo | 2 h, 4 pools, sell-block restriction at 70%, dropout window; 1,803 events |
| `gen_week_trending` | generated_fixture | demo | 7 d, 8 pools, 82,238 events |
| `gen_week_reversal` | generated_fixture | demo | 7 d, 8 pools, 138,651 events |
| `gen_week_sparse_missing` | generated_fixture | demo | 7 d, 6 pools, 23,774 events, two publication-dropout windows (coverage `partial`) |
| `gen_week_liquidity_shift` | generated_fixture | demo | 7 d, 8 pools (1 unsupported v3-like kept in inventory), mint/burn, drained+halted pool, sell-block restriction; 85,151 events |
| `report_excerpt_diagnostic` | report_excerpt | diagnostic_only | built from the supplied evidence excerpts; no execution model; hash is of the fixture, not a receipt |
| Base v2 slices (see §5) | historical_reconstruction | research (with warning) | not committed (data/ is gitignored); rights not cleared |

All generated results are labeled generated. None is historical performance.

## 4. Demo results (generated suite, four weeks, three participants)

Filled from `data/demo/summary.json` and `data/demo/comparison.json` after `make demo`; see the
**Demo numbers** section at the end of this file. The comparison of `scheduled_basket_python`
versus `cash_only_python` carries the warning `FEW_DISTINCT_PERIODS_DESCRIPTIVE_ONLY`; four
generated weeks are not independent market regimes, and no significance or promotion
statement is produced.

## 5. Historical evaluation status (reported separately)

**Deliverable includes: no committed historical pack; short research slices produced locally.**

Covered chain / factory / family: Base (chain id 8453), Uniswap v2 factory
`0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6` (from the official deployments page; interface
verified at runtime: `allPairsLength()` and `feeToSetter()` answered), `uniswap_v2_plain`
wrapped-native (WETH `0x4200…0006`) pairs.

Universe rule: `PairCreated` cohort from a 24-hour discovery window (2026-09-14 12:00 →
2026-09-15 12:00 UTC): **537 candidates, 500 with a WETH leg**, sampled by a frozen,
outcome-independent rule before any period flow was read.

Interval: `[2026-09-15T12:00:01Z, 2026-09-15T12:30:01Z)` (30 minutes; period start snaps to the
first block at/after the requested time), prehistory 1 hour. **Not a full week.** Block times use
the 2000 ms Base interval anchored at the period-start header and verified on 3 sampled
headers (0 mismatches); this is an assumption for unseen blocks.

Runs of the collector against the official public endpoint (read-only, budgets 350–500
requests, no credential):

| Run | Rule | Requests | Result |
|---|---|---|---|
| 1 | earliest-created, 2 pairs | 112 | pack built; universe gate failed on an inventory-count bookkeeping bug (fixed) → `diagnostic_only` |
| 2 | earliest-created, 8 pairs | 145 (8 retries) | `research`; **0 tape events** in the window; 8 executable initial states from `getReserves` |
| 3 | active-before-window (whole discovery range), 8 pairs | 166 (9 retries) | `research` with `execution_state: warning` (no checkpoint reconciled); still **0 events** |
| 4 | active within 5,400 blocks (3 h) before the window, 8 pairs of 88 in scope | 500 budget | **`research`**: 12 real swaps + 12 `Sync` checkpoints in 1 of 8 pools, **all 12 reconciled exactly** (0 mismatches, 0 fidelity flags); coverage `completed_and_checked` for 8 pools |

What was independently checked: chain id, factory interface, block interval on sampled
headers, `getReserves` at the block before the window, and, where events exist, the exact
integer no-agent replay against every `Sync` checkpoint (the validator downgrades on any
mismatch). What was not checked: provider indexing completeness (coverage evidence is "range
returned without truncation error"), token transfer behavior (assumed standard), gas
(assumed zero), MEV/routing (not modeled), rights of the endpoint terms (not reviewed). No
historical pack is qualified for a named suite. Predictive validity: not established.

Blockers for a qualified historical suite: authorized archive-capable endpoint and rights
review; a full-week interval with a larger frozen universe (the public endpoint's log range
limits made `eth_getLogs` chunks halve repeatedly; a week of 500 pairs needs an indexer or a
paid RPC budget, which was not authorized); an independent second read for reconciliation;
recorded restriction observations with as-of availability; a time-qualified gas series.

## 6. Assumptions and their labels

Fixture timings (2000 ms blocks, 100/100/500 ms latencies, 1 confirm block, 5000 ms quote TTL,
1500 ms availability delay, 50 raw gas) are test settings. Research profile timings (250/250/
1000 ms, 4000 ms availability, gas 0) are assumptions. Capacity guardrails (fixtures 100/500
bps, research 10/50 bps) are unvalidated modeling limits. The fixed-flow counterfactual does
not model other participants' reactions. Aliases are a blinded interface, not contamination
proofing; a local operator can read pack files.

## 7. Environment validation summary (`market-replay verify`)

7 tested, 3 not tested, 3 assumed, 0 failed. Sensitivity runs of the scheduled basket on
fixture variants (baseline, high/low latency, high/zero gas, long availability delay) are
recorded with outcomes and no monotonicity claim; see `data/environment_validation.json`.

## 8. Failed checks and fixes during the build

- Universe gate failed on the first real slice because sampled-out pairs were counted as
  unsupported; the inventory now separates `unsupported`, `excluded_by_sampling`, `missing`.
- The "last Sync before the window" fallback for initial state could never trigger; replaced by
  `getReserves` first, then a bounded backward `Sync` scan that also replays the intervening
  events.
- The reconciliation gate passed vacuously with zero checkpoints; it now reports a warning and
  `reconciliation_untested`.
- Browser test initially looked for the raw enum text; the UI humanizes labels (fixed the test).

## 9. Remaining work (deliberately deferred, brief §23)

v3/v4/launchpad/Solana adapters; larger universes and contiguous multiweek episodes;
recorded holder/wallet histories; historical restriction execution; social data as a separate
task; native conditional orders; deployment-latency-sensitive profiles and gas/MEV models; a
formal prospective study; hardened remote execution and public hosting.

## Demo numbers

### Unchanged reference agents on the historical research slice (`base_v2_recent_active_slice_2026-09-15T12`)

| Participant | State | Headline return (NATIVE) | Confirmed fills | Notes |
|---|---|---|---|---|
| `cash_only_python` | completed | 0.00000000 | 0 | abstention is legitimate; valuation complete |
| `scheduled_basket_python` | completed | −0.00118000 | 48 | 5 compressed buy slots × 8 pools + 8 sells; implicit pool fee 1,200 raw NATIVE; gas assumed 0 |
| `random_actions_typescript` (seed 11) | completed | 0.00000000 | 0 | drew only waits/looks in 30 minutes |

Same engine, same agents, no code changes between generated and historical packs. These are
mechanics checks on a 30-minute research slice, not performance evidence.

### Generated suite demo (`make demo`, four full weeks)

(filled below)

