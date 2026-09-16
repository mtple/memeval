# Market Replay — web UI

Browser interface for the local-first trading-agent evaluator. React 18 + TypeScript + Vite, plain CSS (CSS variables, light/dark via `prefers-color-scheme`), inline-SVG charts, no UI or chart libraries.

## Develop

```sh
pnpm install                 # from the repo root (workspace: apps/web)
pnpm --filter web dev        # http://localhost:5173 ; proxies /api and /agent to http://127.0.0.1:8000
```

Start the FastAPI server separately (it is not started by this package). Paste the admin token into the settings bar at the top of the page, or open `http://localhost:5173/?token=<admin token>`. The token is stored in `localStorage` under `mr_admin_token`.

## Build

```sh
pnpm --filter web build      # tsc --noEmit (strict) + vite build -> apps/web/dist
```

The server serves `apps/web/dist/index.html` at `/` (with SPA fallback for unknown paths) and `apps/web/dist/assets/*` at `/assets`. `base` is `/`.

## Screens

| Route | Purpose |
| --- | --- |
| `/episodes` | Packs table with filters, detail panel, import form |
| `/agents` | Registered agents, register form, reference agents, connection instructions |
| `/runs`, `/runs/:id` | Create run / run suite, live run detail with observed-only chart |
| `/runs/:id/results` | Terminal report: outcome, risk, costs, unresolved, assumptions, reproducibility, export |
| `/compare` | Agent A vs B comparison with per-episode table first |
| `/data-health/:packId?` | Universe, ingestion, coverage, rights, validation, decision log |

## Conventions

- Raw quantities are decimal strings in atomic units; they are never parsed to `Number`. `fmtRaw(raw, decimals)` formats with `BigInt`. Prices are parsed to `Number` only to compute pixel positions in charts.
- Charts render nothing at or beyond the run's virtual clock; the observed endpoint already enforces this.
- No scores, percentiles, projections or recommendations are shown. Null valuations, gaps and unresolved inventory are shown explicitly.
