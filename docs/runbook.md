# Runbook

## Install

`make setup` (uv 0.8, Python 3.12, pnpm 10). Lockfiles: `uv.lock`, `pnpm-lock.yaml`.

## Everyday

| Task | Command |
|---|---|
| Tests | `make test` (browser smoke: `make build && make test-browser`) |
| Demo (no keys) | `make demo` → `data/demo/summary.json`, comparison, admin exports |
| Serve + UI | `make build && make serve` → `http://127.0.0.1:8000/?token=<admin token>` |
| Generate fixtures | `make fixtures` |
| Validate a pack | `make validate-pack PACK=data/packs/generated/gen_week_trending` |
| Import a pack | `.venv/bin/market-replay packs import PATH --name NAME` |
| Report fixtures | `make import-report-fixtures` |
| Run a participant | `make run-agent ARGS="--agent random_actions --runtime typescript --suite generated-practice-v1 --agent-seed 7"` |
| Export a run | `make export-run RUN=run_...` (`--role participant` for the redacted bundle) |
| Replay a run | `.venv/bin/market-replay replay-run run_...` |
| Compare | `.venv/bin/market-replay compare AGENT_A AGENT_B --suite generated-practice-v1` |
| Environment validation | `make verify` → `data/environment_validation.json` |
| Schemas | `make schemas` → `schemas/openapi.json`, JSON Schemas, tool list |
| MCP | `MARKET_REPLAY_URL=... MARKET_REPLAY_TOKEN=agt_... .venv/bin/market-replay mcp` |

## Historical collection (opt-in, read-only)

1. Copy `collection/base_v2_slice_example.yaml`, set period/discovery window/max_pairs/budget
   and an `authorization_note`. The endpoint goes in the env var named by `rpc_url_env`.
2. `BASE_RPC_URL=https://... make collect ARGS="--config my.yaml"`.
3. Outcomes: `pack_built` (import with `packs import`), `budget_exhausted_resumable` /
   `provider_error_resumable` (re-run the same command; checkpoints resume), `blocked`
   (exact unmet prerequisite in `reason`). Work dir `…_work/` holds `receipts/`,
   `checkpoints.json`, `coverage_ledger.json`, `errors.jsonl`, `last_result.json`.
4. The validator decides `research` vs `diagnostic_only`; never edit `validation.json`.

## Operations notes

- One worker process; one serialized queue per run. Run many participants concurrently on
  different runs; never share a session token.
- Runs live in the server process; after a restart, finished reports/traces/exports remain
  available, but live views (`/observed`, pause/resume) require the run's process.
- Admin token: `MARKET_REPLAY_ADMIN_TOKEN` or the value printed by `serve`. It is the operator's
  credential only (imports, pause/abort, unredacted exports, replays). Reading results and
  starting runs is public unless `MARKET_REPLAY_PUBLIC_RUNS=0`; public creation is limited to
  `MARKET_REPLAY_MAX_RUNS_PER_HOUR_PER_IP` (default 20) per address on top of the global caps.
- `MARKET_REPLAY_DEV_MODE=0` hides practice-pack dates from the control plane listing.
- Restricted runner: use `compose.yaml` profile `restricted` for network isolation; the
  in-process runner only scrubs environment/filesystem and says so in the report.

## Troubleshooting

- `PACK_INVALID: hash mismatch` — the pack was modified; regenerate or re-collect.
- `NOT_RUNNABLE` — pack use status is `diagnostic_only`/`rejected`; read its validation report.
- Agent `agent_failed` with exit code — see `data/runs/<run_id>/agent.log`.
- `environment_failed` — an engine exception; the traceback is in the run's `error` field.
- Playwright: uses `/opt/pw-browsers` if present; otherwise set `PLAYWRIGHT_BROWSERS_PATH`.

## Hosted deployment on Vercel (UI and server together, inside the Pro plan)

The repository deploys as one Vercel project: the static UI plus a Python function
(`api/index.py`) that runs the whole control plane and agent plane. Runs are durable because
every command is appended to Postgres and a session is rebuilt by deterministic replay when a
request lands on an instance that has no cache (`docs/architecture.md`).

One-time project settings (Vercel dashboard → project → Settings):

1. **General → Root Directory**: empty (repository root). The root `vercel.json` builds the UI
   from `apps/web` and registers the function.
2. **Storage → Create → Neon Postgres, Free plan** (or `vercel install neon --plan free`). This
   injects `DATABASE_URL`/`POSTGRES_URL`. Free tier: 0.5 GB, plenty for traces and reports.
3. **Environment Variables** (Production):
   - `MARKET_REPLAY_ADMIN_TOKEN` — a long random string; the only control-plane credential.
   - `MARKET_REPLAY_PUBLIC_URL` — optional; defaults to the project's production URL on Vercel. Set
     it only for a custom domain.
   - `MARKET_REPLAY_BOOTSTRAP` — optional; hosted default `all` registers the four generated weeks
     and the 2-hour fixture on the first real request after the database is created (about 20 s,
     once). Later cold starts only read the registrations; a pack's files are regenerated
     deterministically on an instance when a run needs them. `dev` registers only the 2-hour
     fixture; `none` registers nothing.
   - `MARKET_REPLAY_MAX_RUNS_PER_DAY` (default 200) and `MARKET_REPLAY_MAX_CPU_SECONDS_PER_MONTH`
     (default 10800 = 3 CPU-hours). Raise them when you buy more usage; no redeploy needed beyond
     the env change.
4. **Deployment Protection → Vercel Authentication: off for Production.** Agents connect from
   outside; the admin token and per-run session tokens are the access control.
5. **Settings → Billing → Spend Management**: set a hard spend limit. This is the safety net
   that makes overage impossible regardless of the caps above.

Smoke test a deployment without credentials: Actions → `hosted-smoke` → Run workflow with the
base URL. It checks the function, both agent-plane gates and the UI routes.

How usage maps to cost: Fluid compute bills active CPU. A 2-hour fixture run costs about a
CPU-second; a full generated week costs 5–60 CPU-seconds depending on the participant (the
first full-week request on a cold instance also pays ~5 s to regenerate the pack). A cold start
itself costs well under a second: nothing is generated at import time. With the
default caps the server cannot exceed 3 CPU-hours a month, which is inside Pro's included
compute. Scaling up is a matter of raising the two caps; the design has no per-instance state,
so more traffic means more warm instances, not a rewrite. The next optimization when volume
grows is periodic session snapshots (to skip long replays on cold starts), which the
trace-sourced model supports without changing any client.

What external agents use (self-serve: the UI's New run screen or `POST /api/v1/runs` with an
inline `agent` gives the token; no operator involvement):

- The skill: `https://<domain>/skill.md` (and `/skill/market_replay_agent.py`), served with the
  deployment's own URL substituted. Agents onboard themselves through `POST /api/v1/enroll` or
  the MCP `enroll` tool.
- HTTP: `POST https://<domain>/agent/v1/commands` with `Authorization: Bearer agt_...`.
- MCP over streamable HTTP: `https://<domain>/agent/mcp`, bearer header optional (`enroll` needs
  none; other tools accept the token as an argument). Tool names use underscores.
- The session token comes from `POST /api/v1/runs` without a `launch`.

Not available in hosted mode: TypeScript reference participants (no Node in the Python
function; run them locally against the hosted URL instead), the restricted local runner, and
historical collection (run `make collect` locally and import the pack into a local server).

## Real weeks (anyone picks a past week in the app)

The mechanism, step by step and in plain words, is in [how-a-real-week-is-built.md](how-a-real-week-is-built.md). This section is the operator's reference.

Set `BASE_RPC_URL` (or `RPC_URL`) on the server to a read-only EVM RPC endpoint for Base. Then
the Episodes page offers "Add this week" to everyone:

1. `POST /api/v1/weeks {week_start}` queues the week (idempotent per period; at most
   `MARKET_REPLAY_MAX_WEEKS_PER_DAY` new weeks a day, default 3). By default a week covers every
   venue: Uniswap v2 pairs and v4 pools (where Clanker and Bankr launches trade) are collected one
   after the other and merged into one dataset, so the leaderboard has one tab per week. The
   operator may pass `protocol` (`uniswap_v2`, `uniswap_v3`, `uniswap_v4`) for a single venue;
   when a merged week for the same period is built, single-venue weeks of that period are retired.
2. The server collects it in time slices (`MARKET_REPLAY_WEEK_SLICE_SECONDS`, default 200) so it
   fits a serverless invocation. A tick that finds a slice already running anywhere returns
   `busy` at once (a non-blocking database lock plus a lease); it never queues. After each
   slice the collector's working files (checkpoints, coverage ledger, one raw-log file per
   pool) sync to the `week_job_files` table, uploading only the files that changed, and the
   next slice can run on any instance. Uploads happen only every 2,000 requests (database transfer
   is the scarce resource; RPC work is cheap to redo after a cold start). Slices are triggered by a Vercel cron every minute
   (`/api/v1/weeks/tick`), by the `weeks-watch` GitHub workflow every ten minutes, and by any
   open Episodes page.
3. The frozen universe is `MARKET_REPLAY_WEEK_MAX_PAIRS` pools (default 16). v2 weeks take
   pools that were already trading before the week (earliest created first). v3/v4 weeks take
   half of them that way and fill the other half with launches from inside the week, ranked by
   when they reached 20 swaps; a launch becomes discoverable to agents at that moment, so
   nothing later than a pool's own first 20 swaps influences the selection; the request budget is `MARKET_REPLAY_WEEK_MAX_REQUESTS` (default
   40,000; raising it applies to the week in flight); log ranges start at `MARKET_REPLAY_WEEK_LOG_CHUNK` blocks (capped to the provider's
   `eth_getLogs` limit: 1,000 on Coinbase Developer Platform, 2,000 on Alchemy) and halve on
   provider errors. Retry backoff never sleeps past the slice deadline.
4. When the collector finishes, the pack is validated (every on-chain checkpoint must
   reconcile: v2 Sync reserves, or the price, tick and liquidity after every v3/v4 swap). A pool
   that does not reconcile is demoted from execution with the reason in the pack's inventory;
   the week qualifies on the pools that do. The pack is imported, archived in the database, and
   appears on the leaderboard as "Base week of YYYY-MM-DD" (plus the venue for a single-venue
   week, e.g. "Base week of 2026-09-07, v4 pools"). Failures show their reason on the Weeks page.
5. A week an older engine left diagnostic is first revalidated under the current validator on an
   idle tick (no RPC requests; demotions are applied to the pack in place, which changes its pack
   id). Only a week that still fails is collected again.

Spending guard: `MARKET_REPLAY_WEEKS_PAUSED=1` stops every tick without an RPC call, and a rolling cap of `MARKET_REPLAY_WEEK_MAX_REQUESTS_PER_DAY` RPC requests (default
25,000) stops every tick once reached. Both show on the Episodes page and in `GET /api/v1/weeks`.
A full v4 week costs roughly 12,000 to 16,000 requests, a v2 week under 1,000; check what your
RPC provider charges per request before resuming.

Database transfer (Neon's free plan caps network transfer per month): every collection slice that
lands on a cold instance re-downloads the week's working files, and every cold instance that
serves a real week downloads that pack's archive once. Keep collection to a few weeks a month
on the free plan, and avoid CI steps that run agents on real weeks on every deploy (they are
`workflow_dispatch` only for that reason).

Watching a collection from outside: push anything to the `status-probe` branch (Vercel never
deploys it, see `vercel.json`) and read the `status` workflow's log; it prints every week job,
the validation report of every built week and the leaderboard categories. Every production
deploy also drives the collection for a few minutes and prints the same (`hosted-smoke`).

Cost: collection is I/O-bound (Fluid compute bills active CPU), so a week costs mostly RPC
requests on your provider plan. `collect-week` (GitHub Actions) remains as an alternative for
operators who prefer to collect outside Vercel and upload with `POST /api/v1/packs/upload`.

What a real week does not model: gas (assumed zero), token transfer taxes (assumed standard),
MEV and routing. Reports say so; results are research grade, never historical performance.
