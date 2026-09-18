"""market-replay command line interface."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import typer

from ..datasets.generator import GeneratorConfig, dev_short_config, generate_pack, standard_suite_configs
from ..datasets.importer import import_report_excerpts
from ..datasets.pack import Pack
from ..datasets.validator import validate_pack
from ..service.auth import resolve_admin_token
from ..service.embedded import EmbeddedServer
from ..service.runs import RunManager

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Market Replay: strategy-agnostic trading-agent evaluator.")
packs_app = typer.Typer(help="Pack import, validation and listing.")
fixtures_app = typer.Typer(help="Generated fixture packs.")
app.add_typer(packs_app, name="packs")
app.add_typer(fixtures_app, name="fixtures")

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATA = Path(os.environ.get("MARKET_REPLAY_DATA_DIR", str(REPO_ROOT / "data")))
FIXTURE_DIR = REPO_ROOT / "fixtures" / "generated"
EVIDENCE_FIXTURE = REPO_ROOT / "fixtures" / "reported_evidence" / "report_excerpts.json"


def _echo(obj: Any) -> None:
    typer.echo(json.dumps(obj, indent=2, sort_keys=True, default=str))


def _manager(data_dir: Path) -> RunManager:
    return RunManager(data_dir=data_dir, dev_mode=os.environ.get("MARKET_REPLAY_DEV_MODE", "1") != "0")


# ---------------------------------------------------------------------- serve
@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000, data_dir: Path = DEFAULT_DATA, admin_token: str | None = None) -> None:
    """Start the HTTP service (control plane, agent plane and the web UI if built)."""
    import uvicorn

    from ..service.app import cors_origins_from_env, create_app

    token = resolve_admin_token(admin_token)
    mgr = _manager(data_dir)
    mgr.gateway_url = f"http://{host}:{port}"
    for w in mgr.register_shipped_weeks():
        typer.echo(f"recorded week: {w['name']} ({w['use_status']})")
    origins = cors_origins_from_env()
    typer.echo(f"admin token: {token}")
    typer.echo(f"listening on http://{host}:{port}  (UI at / if apps/web is built)")
    if origins:
        typer.echo(f"browser origins allowed (MARKET_REPLAY_CORS_ORIGINS): {', '.join(origins)}")
    uvicorn.run(create_app(mgr, token, cors_origins=origins), host=host, port=port, log_level="info")


# ---------------------------------------------------------------------- fixtures
@fixtures_app.command("generate")
def fixtures_generate(out: Path = DEFAULT_DATA / "packs" / "generated", weeks: bool = True, dev: bool = True, force: bool = False) -> None:
    """Generate the standard generated suite (four full weeks) and the short development fixture."""
    cfgs: list[GeneratorConfig] = []
    if dev:
        cfgs.append(dev_short_config())
    if weeks:
        cfgs.extend(standard_suite_configs())
    for cfg in cfgs:
        target = out / cfg.name
        if (target / "manifest.yaml").exists() and not force:
            typer.echo(f"exists: {target}")
            continue
        t = time.time()
        pack = generate_pack(cfg, target)
        typer.echo(f"generated {cfg.name}: {len(pack.tape)} events, {pack.pack_id}, qualification={pack.validation['resulting_qualification']} ({time.time() - t:.1f}s)")


# ---------------------------------------------------------------------- packs
@packs_app.command("validate")
def packs_validate(path: Path) -> None:
    """Run the qualification gates on a pack and print the machine-readable report."""
    pack = Pack.load(path)
    report = validate_pack(pack)
    (path / "validation.json").write_text(json.dumps(report, indent=2, sort_keys=True))
    _echo(report)
    raise typer.Exit(code=0 if not report["executable_failure"] else 2)


@packs_app.command("revalidate")
def packs_revalidate(path: Path) -> None:
    """Re-run the current validator over a recorded week in place. Pools the current engine cannot
    reconcile are demoted (the week keeps its data; its pack id changes). Commit the result."""
    from ..datasets.builder import rebuild_pack
    from ..datasets.validator import demote_unreconciled_pools

    pack = Pack.load(path)
    pools = [p.model_dump(mode="json") for p in pack.pools.values()]
    demoted = demote_unreconciled_pools(pools, list(pack.iter_tape()))
    if demoted:
        inventory = dict(pack.inventory or {})
        inventory["demoted"] = [*inventory.get("demoted", []), *demoted]
        note = f"revalidated {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}: {len(demoted)} pool(s) demoted from execution under the current validator: " + "; ".join(f"{d['pool']}: {d['reason']}" for d in demoted)
        pack = rebuild_pack(pack, pools=pools, inventory=inventory, decision_note=note)
    else:
        report = validate_pack(pack)
        (Path(path) / "validation.json").write_text(json.dumps(report, indent=2, sort_keys=True))
        pack = Pack.load(path)
    _echo({"pack_id": pack.pack_id, "qualification": pack.validation.get("resulting_qualification"), "demoted": [d["pool"] for d in demoted]})


@packs_app.command("import")
def packs_import(path: Path, name: str | None = None, data_dir: Path = DEFAULT_DATA) -> None:
    """Import a pack into the local control plane."""
    mgr = _manager(data_dir)
    _echo(mgr.import_pack(path, name))


@packs_app.command("list")
def packs_list(data_dir: Path = DEFAULT_DATA) -> None:
    mgr = _manager(data_dir)
    for p in mgr.packs():
        typer.echo(f"{p['name']:28s} {p['origin']:26s} {p['use_status']:16s} runnable={p['runnable']} duration_ms={p['duration_ms']} {p['pack_id']}")


@app.command("import-report-fixtures")
def import_report_fixtures(out: Path = DEFAULT_DATA / "packs" / "report_excerpt_diagnostic", data_dir: Path = DEFAULT_DATA) -> None:
    """Build the diagnostic-only pack from the supplied report excerpts and import it."""
    pack = import_report_excerpts(EVIDENCE_FIXTURE, out)
    mgr = _manager(data_dir)
    row = mgr.import_pack(out, "report_excerpt_diagnostic")
    typer.echo(f"imported {row['name']} as {row['use_status']} (runnable={row['runnable']}); gates: " + ", ".join(f"{g['gate']}={g['status']}" for g in pack.validation["gates"]))


# ---------------------------------------------------------------------- runs
def _ensure_packs(mgr: RunManager, names: list[str], packs_dir: Path) -> None:
    for name in names:
        if mgr.store.pack(name) is None:
            target = packs_dir / name
            if not (target / "manifest.yaml").exists():
                cfgs = {c.name: c for c in [dev_short_config(), *standard_suite_configs()]}
                if name not in cfgs:
                    raise typer.BadParameter(f"pack {name} is not imported and not a known fixture")
                generate_pack(cfgs[name], target)
            mgr.import_pack(target, name)


def _ensure_agent(mgr: RunManager, name: str, version: str, runtime: str) -> str:
    row = mgr.store.agent_by_name_version(name, version)
    if row:
        return row["agent_id"]
    return mgr.register_agent(name=name, version=version, runtime=runtime, capabilities=["markets", "broker", "clock"], config={"example": name, "runtime": runtime})["agent_id"]


@app.command("run-agent")
def run_agent(
    agent: str = typer.Option(..., help="example name: cash_only | scheduled_basket | random_actions | model_client"),
    suite: str = typer.Option("generated-dev-v1"),
    pack: str | None = typer.Option(None, help="run on one pack (imported name/id or a pack directory) instead of a suite"),
    runtime: str = typer.Option("python", help="python | typescript"),
    version: str = typer.Option("1"),
    isolation: str = typer.Option("trusted_external_client"),
    mode: str = typer.Option("practice"),
    bankroll_raw: str = typer.Option("1000000"),
    agent_seed: str | None = typer.Option(None),
    data_dir: Path = DEFAULT_DATA,
) -> None:
    """Run a reference participant on a suite (or one pack) through the real HTTP agent plane and print the reports."""
    mgr = _manager(data_dir)
    agent_id = _ensure_agent(mgr, f"{agent}_{runtime}", version, runtime)
    if pack is not None:
        row = mgr.store.pack(pack)
        if row is None:
            if Path(pack).is_dir():
                row = mgr.import_pack(Path(pack), Path(pack).name)
                row = mgr.store.pack(row["pack_id"])
            else:
                raise typer.BadParameter(f"unknown pack {pack}")
        assert row is not None
        with EmbeddedServer(mgr, resolve_admin_token(None)):
            view = mgr.create_run(agent_id=agent_id, pack_ref=row["pack_id"], mode=mode, bankroll_raw=bankroll_raw, isolation=isolation, launch_spec={"kind": "example", "name": agent, "runtime": runtime}, agent_seed=agent_seed)
            mgr.wait_for_run(view["run_id"], 3600)
        r = mgr.run_view(view["run_id"])
        rep = mgr.report(r["run_id"])
        typer.echo(f"{r['run_id']} {r['pack_name']:26s} state={r['state']:12s} return={rep.get('outcome', {}).get('headline_return')} complete={rep.get('outcome', {}).get('valuation_complete')} fills={rep.get('activity', {}).get('confirmed_fills')} error={r['error']}")
        typer.echo(json.dumps({"run_ids": [r["run_id"]]}))
        return
    suite_def = mgr.suites.get(suite)
    if suite_def is None:
        raise typer.BadParameter(f"unknown suite {suite}; known: {sorted(mgr.suites)}")
    _ensure_packs(mgr, suite_def.packs, data_dir / "packs" / "generated")
    with EmbeddedServer(mgr, resolve_admin_token(None)):
        result = mgr.run_suite(suite, agent_id=agent_id, launch_spec={"kind": "example", "name": agent, "runtime": runtime}, isolation=isolation, wait=True, agent_seed=agent_seed)
    for r in result["runs"]:
        rep = mgr.report(r["run_id"])
        typer.echo(f"{r['run_id']} {r['pack_name']:26s} state={r['state']:12s} return={rep.get('outcome', {}).get('headline_return')} complete={rep.get('outcome', {}).get('valuation_complete')} fills={rep.get('activity', {}).get('confirmed_fills')} error={r['error']}")
    typer.echo(json.dumps({"suite_run_id": result["suite_run_id"], "run_ids": result["run_ids"]}))


@app.command("export-run")
def export_run(run_id: str, role: str = "admin", include_mappings: bool = False, out: Path | None = None, data_dir: Path = DEFAULT_DATA) -> None:
    """Export a run bundle (report, trace, descriptor; admin adds ledger/orders)."""
    mgr = _manager(data_dir)
    bundle = mgr.export(run_id, role, include_mappings)
    text = json.dumps(bundle, indent=2, sort_keys=True, default=str)
    if out:
        out.write_text(text)
        typer.echo(f"wrote {out}")
    else:
        typer.echo(text)


@app.command("replay-run")
def replay_run(run_id: str, data_dir: Path = DEFAULT_DATA) -> None:
    """Re-execute a run's recorded actions on a fresh session and compare ledger/state hashes."""
    mgr = _manager(data_dir)
    _echo(mgr.replay(run_id))


@app.command()
def compare(agent_a: str, agent_b: str, suite: str = "generated-practice-v1", data_dir: Path = DEFAULT_DATA) -> None:
    """Paired comparison of two registered agent ids on a suite."""
    mgr = _manager(data_dir)
    _echo(mgr.compare(suite_id=suite, agent_a=agent_a, agent_b=agent_b))


# ---------------------------------------------------------------------- demo
@app.command()
def demo(data_dir: Path = DEFAULT_DATA, suite: str = "generated-practice-v1", quick: bool = False) -> None:
    """No-key demo: generate the suite, run three reference participants (Python and TypeScript), compare, export."""
    if quick:
        suite = "generated-dev-v1"
    mgr = _manager(data_dir)
    suite_def = mgr.suites[suite]
    typer.echo(f"[1/5] generating and importing packs for {suite} ...")
    _ensure_packs(mgr, suite_def.packs, data_dir / "packs" / "generated")
    try:
        import_report_excerpts(EVIDENCE_FIXTURE, data_dir / "packs" / "report_excerpt_diagnostic")
        mgr.import_pack(data_dir / "packs" / "report_excerpt_diagnostic", "report_excerpt_diagnostic")
    except Exception as e:  # pragma: no cover
        typer.echo(f"report excerpt import skipped: {e}")
    participants = [("cash_only", "python"), ("scheduled_basket", "python"), ("random_actions", "typescript")]
    ids = {}
    t0 = time.time()
    with EmbeddedServer(mgr, resolve_admin_token(None)):
        for i, (name, runtime) in enumerate(participants, start=2):
            typer.echo(f"[{i}/5] running {name} ({runtime}) on {suite} ...")
            aid = _ensure_agent(mgr, f"{name}_{runtime}", "1", runtime)
            ids[name] = aid
            t = time.time()
            res = mgr.run_suite(suite, agent_id=aid, launch_spec={"kind": "example", "name": name, "runtime": runtime}, wait=True, agent_seed="demo-7")
            for r in res["runs"]:
                rep = mgr.report(r["run_id"])
                typer.echo(f"   {r['pack_name']:26s} {r['state']:10s} return={rep.get('outcome', {}).get('headline_return')} complete={rep.get('outcome', {}).get('valuation_complete')} fills={rep.get('activity', {}).get('confirmed_fills')} dd={rep.get('risk', {}).get('max_drawdown')}")
            typer.echo(f"   ({time.time() - t:.1f}s wall)")
    typer.echo("[5/5] comparing scheduled_basket vs cash_only ...")
    cmp = mgr.compare(suite_id=suite, agent_a=ids["scheduled_basket"], agent_b=ids["cash_only"])
    typer.echo(json.dumps(cmp["summary"], indent=2))
    typer.echo("warnings: " + ", ".join(cmp["warnings"]))
    out = data_dir / "demo"
    out.mkdir(parents=True, exist_ok=True)
    (out / "comparison.json").write_text(json.dumps(cmp, indent=2, sort_keys=True))
    runs = mgr.runs(suite_id=suite)
    for r in runs[-len(participants) * len(suite_def.packs) :]:
        (out / f"{r['run_id']}_export_admin.json").write_text(json.dumps(mgr.export(r["run_id"], "admin"), indent=2, sort_keys=True, default=str))
    summary = {"suite": suite, "agents": ids, "wall_seconds": round(time.time() - t0, 1), "comparison_id": cmp["comparison_id"], "runs": [{"run_id": r["run_id"], "pack": r["pack_name"], "state": r["state"], "agent_id": r["agent_id"]} for r in runs], "statement": "GENERATED FIXTURES ONLY. Results describe behavior in artificial episodes; they are not historical performance."}
    (out / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True))
    typer.echo(f"demo artifacts written to {out}")
    typer.echo(summary["statement"])


# ---------------------------------------------------------------------- collect
@app.command()
def collect(config: Path = typer.Option(..., help="authorized collection config YAML"), data_dir: Path = DEFAULT_DATA) -> None:
    """Opt-in, read-only historical collection with an explicit request budget. Never runs inside a replay."""
    from ..collectors.historical import run_collection

    result = run_collection(config, data_dir)
    _echo(result)


@app.command()
def week(
    start: str = typer.Option(..., "--start", help="week start, UTC date (YYYY-MM-DD)"),
    end: str | None = typer.Option(None, "--end", help="period end (default: start + 7 days)"),
    out: Path = typer.Option(REPO_ROOT / "weeks", "--out", help="directory the finished week is written into (committed to the repository)"),
    max_pairs: int = typer.Option(16, help="established pools (trading before the week): 4 v2 pairs, 4 v3 pools, 8 v4 pools by default"),
    universe: str = typer.Option("launches", help="'launches': every pool launched inside the week plus the established pools; 'sampled': established pools plus a sample of launches"),
    min_swaps: int = typer.Option(1, help="launches with fewer swaps inside the week are left out of the tape (counted in the inventory)"),
    max_requests: int = typer.Option(40000, help="hard RPC request budget"),
    log_chunk_blocks: int = typer.Option(10000, help="eth_getLogs block range per request (halved on provider errors; capped to the provider's limit)"),
    rpc_url_env: str = typer.Option("BASE_RPC_URL", help="name of the environment variable holding the read-only RPC endpoint"),
) -> None:
    """Record one real week of Base trading into weeks/<name>, ready to commit: every pool launched
    inside the week on Uniswap v2, v3 and v4 (the noise an agent has to pick through) plus a fixed
    set of established pools.

    Reads the chain through your own RPC endpoint (about two hours and 12,000 to 16,000 requests for a
    week), validates the result, and writes the pack only when it qualifies as research data. Commit
    and push the directory; the deployed site registers it on its next start."""
    from datetime import UTC, datetime, timedelta

    from ..collectors.historical import run_collection

    if not os.environ.get(rpc_url_env):
        typer.echo(f"{rpc_url_env} is not set; export your read-only Base RPC endpoint first", err=True)
        raise typer.Exit(2)
    t0 = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=UTC)
    t1 = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=UTC) if end else t0 + timedelta(days=7)
    if t1 <= t0 or (t1 - t0) > timedelta(days=7, hours=1):
        typer.echo("the period must be at most one week and end after it starts", err=True)
        raise typer.Exit(2)
    if t1 > datetime.now(UTC) - timedelta(hours=12):
        typer.echo("the period must have ended at least 12 hours ago", err=True)
        raise typer.Exit(2)
    is_week = (t1 - t0) >= timedelta(days=7)
    hours = int((t1 - t0).total_seconds() // 3600)
    name = f"base_week_{t0:%Y-%m-%d}" if is_week else f"base_period_{t0:%Y-%m-%d}_{hours}h"
    iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    cfg = {
        "rpc_url_env": rpc_url_env,
        "chain": "base",
        "protocol": "all",
        "period_start_utc": iso(t0),
        "period_end_utc": iso(t1),
        "discovery_window_start_utc": iso(t0 - timedelta(days=7 if is_week else 1)),
        "prehistory_hours": 24 if is_week else 1,
        "selection_rule": "active_before_window_earliest_created_v1",
        "selection_rule_cl": "active_before_window_earliest_created_v1" if universe == "launches" else "active_before_window_plus_window_launches_v1",
        "venues": ["uniswap_v2", "uniswap_v3", "uniswap_v4"] if universe == "launches" else ["uniswap_v2", "uniswap_v4"],
        "venue_pairs": {"uniswap_v2": max_pairs // 4, "uniswap_v3": max_pairs // 4, "uniswap_v4": max_pairs - 2 * (max_pairs // 4)} if universe == "launches" else None,
        "include_launches": universe == "launches",
        "min_swaps": min_swaps,
        "max_launches": max_pairs // 2,
        "launch_min_swaps": 20,
        "activity_lookback_blocks": 43200 if is_week else 5400,
        "max_pairs": max_pairs,
        "max_requests": max_requests,
        "max_response_bytes": 4 * 1024 * 1024 * 1024,
        "log_chunk_blocks": log_chunk_blocks,
        "initial_state_lookback_blocks": 20000,
        "availability_delay_ms": 4000,
        "out_dir": str((out / name).resolve()),
        "authorization_note": "Operator-run read-only collection from the operator's own RPC endpoint with a fixed request budget; public chain data; redistribution not cleared.",
    }
    out.mkdir(parents=True, exist_ok=True)
    work = out / (name + "_work")
    work.mkdir(exist_ok=True)
    cfg_path = work / "config.yaml"
    import yaml

    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    typer.echo(f"recording {name}: {cfg['period_start_utc']} to {cfg['period_end_utc']} into {out / name} (working files in {work})", err=True)
    result = run_collection(cfg_path, out)
    for line in (result.get("decision_log") or [])[-60:]:
        typer.echo(f"  | {line}", err=True)
    _echo({k: v for k, v in result.items() if k != "decision_log"})
    if result.get("status") != "pack_built":
        typer.echo(f"no pack: {result.get('status')}: {result.get('reason') or result.get('error')}; the working files are kept, run the same command again to resume", err=True)
        raise typer.Exit(1)
    if result.get("qualification") != "research":
        typer.echo(f"the week was recorded but does not qualify ({result.get('qualification')}); see {out / name}/validation.json. Not committing it is the right call.", err=True)
        raise typer.Exit(1)
    import shutil

    shutil.rmtree(work, ignore_errors=True)
    typer.echo(f"done: {out / name} qualifies as research. Commit it: git add {out / name} && git commit -m 'Base week of {t0:%Y-%m-%d}' && git push", err=True)


@app.command()
def survey(
    start: str = typer.Option(..., "--start", help="week start, UTC date (YYYY-MM-DD)"),
    end: str | None = typer.Option(None, "--end", help="period end (default: start + 7 days)"),
    out: Path = typer.Option(REPO_ROOT / "data" / "survey", "--out"),
    rpc_url_env: str = typer.Option("BASE_RPC_URL"),
    max_requests: int = typer.Option(60000),
) -> None:
    """Count every pool launch and every swap on Base in a week, per venue. Records nothing;
    sizes an all-launches recording."""
    from datetime import UTC, datetime, timedelta

    from ..collectors.survey import survey_week

    url = os.environ.get(rpc_url_env)
    if not url:
        typer.echo(f"{rpc_url_env} is not set", err=True)
        raise typer.Exit(2)
    t0 = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=UTC)
    t1 = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=UTC) if end else t0 + timedelta(days=7)
    res = survey_week(rpc_url=url, chain="base", period_start_utc=t0.strftime("%Y-%m-%dT%H:%M:%SZ"), period_end_utc=t1.strftime("%Y-%m-%dT%H:%M:%SZ"), work=out / f"base_{start}", max_requests=max_requests, echo=lambda s: typer.echo(s, err=True))
    _echo(res)


@app.command()
def mcp() -> None:
    """Run the MCP facade on stdio (needs MARKET_REPLAY_URL and MARKET_REPLAY_TOKEN)."""
    from ..service.mcp_server import main as mcp_main

    mcp_main()


@app.command()
def verify(data_dir: Path = DEFAULT_DATA) -> None:
    """Run the environment-validation report generator (tested / not_tested / failed / assumed) and print it."""
    from ..evaluation.environment_validation import build_environment_validation

    mgr = _manager(data_dir)
    report = build_environment_validation(mgr, data_dir)
    out = data_dir / "environment_validation.json"
    out.write_text(json.dumps(report, indent=2, sort_keys=True, default=str))
    _echo(report["summary"])
    typer.echo(f"full report: {out}")


if __name__ == "__main__":
    sys.exit(app())
