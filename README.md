# Market Replay

A local-first, strategy-agnostic evaluator for trading agents. It replays bounded onchain
market episodes (generated fixtures today; Base / Uniswap v2 research slices through the
included collector) with virtual time, blinded aliases, exact integer accounting and an
explicit constant-product execution model. The platform supplies the market, the
information-access rules and the execution mechanics. **The agent supplies every trading
decision.**

What it is not: a profitability oracle, a live-trading system, a wallet, a strategy tester
for any particular bot, or a claim that simulated results predict live results.
Predictive validity is `not_established` everywhere in the product until an independently
documented study exists.

## Status in one paragraph

Software-complete v1 for the **generated demonstration** lane: a no-key, full-week
simulation with four artificial weekly episodes, three reference participants in Python and
TypeScript, HTTP + MCP interfaces with schemas and client SDKs, point-in-time observation
rules, aliases, deterministic ordering, exact ledger accounting, reports, paired
comparisons, exports and adversarial tests. The **historical** lane has a working, tested,
resumable Base/Uniswap-v2 collector and reconciler; the historical evaluation status is
reported separately in [docs/implementation-report.md](docs/implementation-report.md).

## Quick start (no keys, no network)

```bash
make setup            # uv venv + pinned deps; pnpm install for the web UI
make test             # 83 unit/property/integration/security tests (network blocked in tests)
make demo             # generate 4 full weeks, run 3 participants (py+ts), compare, export
make build && make serve   # web UI at http://127.0.0.1:8000 (operator token printed; not needed to start runs)
```

Without Make:

```bash
uv venv --python 3.12 .venv && uv sync --extra dev
.venv/bin/python -m pytest tests -m "not browser"
.venv/bin/market-replay demo
.venv/bin/market-replay serve            # then open http://127.0.0.1:8000 (Sign in with the printed token for operator actions)
.venv/bin/market-replay run-agent --agent scheduled_basket --suite generated-practice-v1 --runtime typescript
.venv/bin/market-replay import-report-fixtures
.venv/bin/market-replay verify           # environment-validation report with sensitivity runs
```

Container: `docker compose up service` (published on 127.0.0.1:8000 only). See `compose.yaml`
for the internal-network restricted participant runner.

## Hosted on Vercel

The same repository deploys as one Vercel project: static UI plus a Python function running
the server, with a free Neon Postgres for durable runs. Agents anywhere connect over HTTP or
MCP. Setup steps and cost caps: [docs/runbook.md](docs/runbook.md#hosted-deployment-on-vercel-ui-and-server-together-inside-the-pro-plan).

## Connect an agent (bring your own)

Nobody needs an account and nothing is registered by hand. Give your agent the skill and ask it
to run the tests:

> Install the Market Replay skill from https://memeval-web.vercel.app/skill.md and run the tests.

The skill (`skills/market-replay/`, Bankr catalog layout: `SKILL.md`, `catalog.json`, a
standard-library Python participant in `scripts/`) tells the agent to enroll by name
(`POST /api/v1/enroll`), receive one session token per episode, trade through the tools over
HTTP or MCP (`enroll` is also an MCP tool, so an MCP-only agent needs no headers), finish, and
read the report. A person can do the same from the web UI with **New run**, or over HTTP:

1. Create a run with your agent named inline: `POST /api/v1/runs {"agent": {"name": "my-bot",
   "version": "1"}, "pack_id": "gen_week_trending"}`. The response contains a one-time
   `session_credential` with the HTTP and MCP URLs. (With `launch`, the service runs one of the
   included reference participants instead and never returns a credential.)
2. Point the agent at it. MCP-speaking agents (OpenClaw, Hermes, Claude, and similar) use
   `/agent/mcp` with `Authorization: Bearer <token>`; anything else calls
   `POST /agent/v1/commands` with the same header:

```json
{"request_id": "client_unique_001", "tool": "broker.submit",
 "arguments": {"pool_id": "pool_n7q2abcd", "asset_in": "CASH", "asset_out": "asset_p2dxyz12",
               "amount_in_raw": "1000000", "min_amount_out_raw": "950000000",
               "deadline_ms": 180000, "idempotency_key": "agent_intent_42"}}
```

Results, episodes and agents are public reads. The admin token (`MARKET_REPLAY_ADMIN_TOKEN`)
is only for operator actions: importing packs, pausing or aborting runs, unredacted exports and
replays. Public run creation is bounded by the daily and monthly caps plus a per-address rate
limit, and can be switched off with `MARKET_REPLAY_PUBLIC_RUNS=0`.

Every tool returns the same envelope (`request_id, session_id, clock_ms, status, data,
quality, error`). Quantities are decimal strings in raw units; time is integer milliseconds
relative to the episode start. Fourteen tools: `session.describe, markets.list, markets.get,
market.trades, market.candles, market.liquidity, market.restrictions, broker.quote,
broker.submit, broker.order, portfolio.get, portfolio.history, clock.advance,
session.finish`. Everything else returns `UNSUPPORTED_CAPABILITY`.

SDKs: `sdk/python/market_replay_client` (httpx) and `sdk/typescript/src/index.ts` (fetch,
BigInt). Reference participants: `agents/examples/{python,typescript}`. MCP facade over the
same handler: `market-replay mcp`. Conformance checks any client can run:
`python -m market_replay_client.conformance`. Walkthrough: [docs/agent-integration.md](docs/agent-integration.md).

## Repository layout

```
apps/web/                 React + TypeScript + Vite UI (Results, New run, Episodes, Agents, Compare, Data health)
src/market_replay/
  domain/                 identity, exact quantities, statuses, canonical records, envelope
  engine/                 block schedule, tape, deterministic simulation, session (tool handler)
  observations/           as-of trade store, candles with completeness, leakage scanner
  venues/cpmm/            constant-product math and pool state (cpmm_fixed_flow_v1)
  broker/                 balanced ledger, orders/quotes lifecycle
  datasets/               pack format, manifests, generator, validator (10 gates), importers
  collectors/             read-only EVM RPC + GeckoTerminal collectors, historical pipeline
  service/                FastAPI control/agent planes, SQLite store, run manager, MCP facade
  evaluation/             run report, paired comparison, study registry, environment validation
  runners/                trusted launcher, restricted runner, inference gateway
  cli/                    market-replay CLI
sdk/python, sdk/typescript, agents/examples, schemas/, fixtures/, tests/, docs/, data/ (gitignored)
```

## Documentation

- [docs/architecture.md](docs/architecture.md) — components, boundaries, data flow
- [docs/agent-integration.md](docs/agent-integration.md) — connecting any agent; tool reference
- [docs/dataset-format.md](docs/dataset-format.md) — packs, manifests, coverage, qualification gates
- [docs/execution-assumptions.md](docs/execution-assumptions.md) — `cpmm_fixed_flow_v1`, capacity guardrails, gas, valuation
- [docs/benchmark-protocol.md](docs/benchmark-protocol.md) — suites, comparisons, what is and is not claimed
- [docs/data-rights.md](docs/data-rights.md) — storage/processing/redistribution/serving status
- [docs/runbook.md](docs/runbook.md) — operations, collection, troubleshooting
- [docs/implementation-report.md](docs/implementation-report.md) — what works, verification commands executed, blockers

## Claims this software does not make

A profitable simulation is not an edge. Aliases are a blinded interface, not contamination
proofing. Weeks, chains and reruns are not independent observations. Provider snapshots are
not a complete market. A quote is not a fill. Missing data is not zero activity. A
market-data-only test does not evaluate social research.

License: MIT.
