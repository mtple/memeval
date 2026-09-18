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
2. **A Postgres database** for rows (runs, agents, leaderboard): any provider; set `DATABASE_URL`
   (Vercel's Storage tab injects it for Neon; a Supabase project's pooler URL works the same). The
   free tiers are plenty: no file ever goes through the database, only small rows.
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

What external agents use (self-serve: the join link, or `POST /api/v1/runs` with an inline
`agent` gives the token; no operator involvement):

- The skill: `https://<domain>/skill.md` (and `/skill/market_replay_agent.py`), served with the
  deployment's own URL substituted. Agents onboard themselves through `POST /api/v1/enroll` (join) and `POST /api/v1/play`, or
  the MCP `enroll` and `play` tools.
- HTTP: `POST https://<domain>/agent/v1/commands` with `Authorization: Bearer agt_...`.
- MCP over streamable HTTP: `https://<domain>/agent/mcp`, bearer header optional (`enroll` needs
  none; other tools accept the token as an argument). Tool names use underscores.
- The session token comes from `POST /api/v1/runs` without a `launch`.

Not available in hosted mode: TypeScript reference participants (no Node in the Python
function; run them locally against the hosted URL instead), the restricted local runner, and
historical collection (run `make collect` locally and import the pack into a local server).

## Real weeks (recorded on your machine, committed to the repository)

The mechanism, step by step and in plain words, is in [how-a-real-week-is-built.md](how-a-real-week-is-built.md).

Users never request episodes. The episodes on the site are the pack directories committed under
`weeks/`, and nothing else; the unit that ships is one UTC day. To add one:

```bash
export BASE_RPC_URL=https://...      # your read-only Base endpoint (Coinbase Developer Platform works)
make day START=2026-09-09            # about ten minutes; a few thousand RPC requests
git add weeks/base_day_2026-09-09 && git commit -m "Base day of 2026-09-09" && git push
```

Or push a commit whose message is `day 2026-09-09` to the `record-week` branch and GitHub Actions
records and commits it. To withdraw an episode, delete its directory and push: the next deploy
takes it off the catalogue and the leaderboard, and finished runs keep their reports.

`make week` records every pool launched inside the week on Uniswap v2, v3 and v4 (with an ETH leg
and at least one swap) plus a fixed set of established pools into one pack, validates it, and
writes it only when it qualifies as research data. `market-replay week --universe sampled` is the
older sixteen-pool recording. A week
that does not qualify is reported with the failing gate; do not commit it. An interrupted recording
keeps its working files under `weeks/<name>_work/` (ignored by git) and resumes when the same
command runs again. The optional `collect-week` GitHub workflow runs the same command on GitHub's
runners and commits the result to `main`, if you would rather not tie up your machine: start it from
the Actions tab, or push a commit whose message contains the date to the `record-week` branch
(`git commit --allow-empty -m "record 2026-09-07" && git push -f origin HEAD:record-week`).

On deploy, Vercel builds the repository into the function, so every instance has every week on its
own disk. The server registers the weeks under `weeks/` on its first request (reading the committed
validation report; the pack's hashes are verified, nothing is replayed). The database holds rows
only: runs, agents, the leaderboard. No file goes through it.

When the validator or the engine changes what a recorded week contains, re-check the committed
weeks locally with `market-replay packs revalidate weeks/<name>` (pools the current engine cannot
reconcile are demoted; the pack id changes) and commit the result.

Watching production from outside: push anything to the `status-probe` branch (Vercel never
deploys it, see `vercel.json`) and read the `status` workflow's log; it prints every recorded week,
its gate summary and the leaderboard tabs. Every production deploy also runs `hosted-smoke`.

What a real week does not model: gas (assumed zero), token transfer taxes (assumed standard),
MEV and routing. Reports say so; results are research grade, never historical performance.
