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

## Deploying the UI on a static host (Vercel, Netlify, S3)

The UI is static; **the Market Replay server is not**. A static deployment shows every screen
only after you connect it to a server you run:

1. `MARKET_REPLAY_CORS_ORIGINS=https://<your-ui-host> make serve` on a machine you control
   (or in `compose.yaml`). Without the allowlist the browser blocks cross-origin calls.
2. Open the UI and enter the server URL and admin token in the settings bar (or use
   `?server=https://host:8000&token=adm_...`). Both are stored in `localStorage`.
3. `apps/web/vercel.json` rewrites client routes to `index.html` so deep links like `/agents`
   load; `/api/*`, `/agent/*` and `/assets/*` are never rewritten. Set `VITE_API_BASE` at build
   time to pre-fill the server URL.

Never expose the server publicly without putting it behind your own authentication; the admin
token alone is a single shared secret.
