# Architecture

## Components

```
participant (any language) ──HTTP/MCP──▶ agent plane  ──▶ Session (tool handler) ──▶ Simulation
operator / UI ───────────────HTTP──────▶ control plane ─▶ RunManager ─▶ SQLite + JSONL traces
collectors (opt-in, read-only) ─▶ raw receipts ─▶ normalizer ─▶ pack builder ─▶ validator ─▶ pack
```

- **Pack** (`datasets/`): immutable directory (manifest, execution params, assets, pools,
  tape, coverage, validation, inventory). Content-addressed `pack_id`.
- **Simulation** (`engine/simulation.py`): virtual clock in relative ms; block schedule
  (fixed interval for fixtures, table for historical); ordered external tape applied to a
  *private* pool state and a *reference* (no-agent) state; pending-order schedule; balanced
  ledger; observation store; equity grid; valuation branch.
- **Session** (`engine/session.py`): the only agent path. Aliases, budgets, virtual-time rate
  limit, latency accounting, range clamping, leakage scanning, trace recording. Same handler
  for HTTP (`/agent/v1/commands`), the SDKs and MCP (`service/mcp_server.py`).
- **RunManager** (`service/runs.py`): one `RLock` per run; sessions never share portfolio,
  market state, alias maps or random state. Launches reference participants as subprocesses
  (trusted) or through the restricted runner. Finalizes reports, exports, replays.
- **Control plane** (`service/app.py`): packs, agents, suites, runs, comparisons, studies,
  data health. Admin token only. Serves the built web UI.
- **Evaluation**: run report (`evaluation/report.py`), paired comparison, study registry,
  environment validation.
- **Collectors**: budgeted HTTP with receipts, checkpoints and a coverage ledger
  (`collectors/base.py`); EVM JSON-RPC (`evm_rpc.py`); historical Base/v2 pipeline
  (`historical.py`); optional GeckoTerminal enrichment (`gecko.py`).

## Event ordering

Within a run all commands are serialized. `process_until(t)` processes, in order: tape
events (by block, log index, seq) up to and including time `t`; then, per block whose time is
`t`, inclusions of pending orders (submission order) *after* the block's external events,
then confirmations; then reporting-grid snapshots. A data request advances the clock by the
declared service latency first and answers as of the new time. Ranges are clamped to the
present with an explicit warning; past ranges stay within their bounds.

## Information vs. market state

The engine knows an external swap happened at its block time. The agent sees it only from
`available_ms = event + availability_delay` (fixture) or the reconstructed delay model. Quotes
and `market.liquidity` are declared to reflect the current private model state at response
time (their own information product). Undiscovered pools and unknown identifiers get one
uniform `NOT_YET_DISCOVERED` response; pagination totals count only currently discoverable
pools; `clock.advance next_event` never uses events of undiscovered pools.

## Storage

- SQLite (WAL) for control-plane rows; per-run `trace.jsonl`, `report.json`,
  `run_manifest.json`, `agent.log` under `data/runs/<run_id>/`.
- Packs as JSONL/YAML/JSON. Full-week generated tapes load in ~3–5 s; a performance
  diagnostic is in the implementation report. Parquet/DuckDB are pinned dependencies for
  normalized historical queries but are not required by the v1 engine path.

## Extension points

- New venue: `venues/<name>/` with its own adapter; pools declare `model`, the validator's
  mechanics gate refuses unsupported models in the CPMM adapter.
- New collector: subclass `HttpCollector`, write receipts and coverage, emit tape rows.
- New tool: add to `TOOLS` and `Session._dispatch`; HTTP, SDKs and MCP pick it up.

## Durable runs without a resident process

The store (SQLite locally, Postgres when hosted) holds each run's append-only command trace,
manifest, report and agent log. A live `Session` is a cache: an instance that receives a
command for a run it does not hold rebuilds the session by replaying the stored trace (the
same replay the reproducibility test verifies by hash), then applies the new command. A
store-level lock per run (advisory lock in Postgres, `flock` for SQLite) serializes commands,
and a catch-up step applies any records another instance appended. This is what lets the
whole service run as a Vercel Function while agents connect over HTTP or MCP from anywhere.
