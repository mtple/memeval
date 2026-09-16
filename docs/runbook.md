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
- Admin token: `MARKET_REPLAY_ADMIN_TOKEN` or the value printed by `serve`.
- `MARKET_REPLAY_DEV_MODE=0` hides practice-pack dates from the control plane listing.
- Restricted runner: use `compose.yaml` profile `restricted` for network isolation; the
  in-process runner only scrubs environment/filesystem and says so in the report.

## Troubleshooting

- `PACK_INVALID: hash mismatch` — the pack was modified; regenerate or re-collect.
- `NOT_RUNNABLE` — pack use status is `diagnostic_only`/`rejected`; read its validation report.
- Agent `agent_failed` with exit code — see `data/runs/<run_id>/agent.log`.
- `environment_failed` — an engine exception; the traceback is in the run's `error` field.
- Playwright: uses `/opt/pw-browsers` if present; otherwise set `PLAYWRIGHT_BROWSERS_PATH`.
