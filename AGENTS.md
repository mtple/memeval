# Notes for coding agents working in this repository

## Boundaries that must hold

- `src/market_replay/engine`, `venues`, `broker`, `observations`, `domain`: no network, no
  system clock, no wallet, no model provider, no strategy imports. Tests block non-loopback
  sockets; keep it that way.
- `collectors/` are read-only and budgeted; never imported by the engine; never run inside a
  replay. The RPC URL comes from an environment variable named in the config, never from a file.
- The broker never talks to a real swap endpoint. `orders` actions cannot reach an execution
  provider by construction (there is no client for one).
- One command handler (`engine/session.py`) backs HTTP, the SDKs and MCP. Add tools there.
- Agent tokens (`agt_`) cannot call `/api/v1`; admin tokens cannot call `/agent/v1`.
- Every agent-visible payload passes the leakage scanner; keep private terms (canonical
  keys, addresses, dates, paths, pack ids) out of `data`/`error`.

## Conventions

- Integers for raw quantities, decimal strings on the wire, `Fraction`/`Decimal` for ratios.
  Never floats for balances, AMM math or fees.
- Relative milliseconds for agent-visible time; absolute UTC ms only in private pack files.
- Every status dimension is reported separately (origin, availability basis, execution model,
  token behavior, isolation, use status, predictive validity). Never merge them into a badge.
- Reports contain no 0–100 score, percentile, projection, significance test or verdict.
- New choices narrow unsupported claims; they do not add hidden strategy assumptions.

## Commands

`make test`, `make demo`, `make verify`, `make build`, `make lint`. Python via `.venv`
(uv, 3.12). TypeScript runs with `node --experimental-strip-types` (no build step for SDK
or examples); the web UI is built with pnpm/Vite.

## Adding a venue

Implement a new adapter package under `venues/` with its own state, math and reconciliation.
Do not route unsupported mechanics through `venues/cpmm`. Mark such pools
`supported_by_cpmm: false` with a reason; the validator keeps them in the inventory.
