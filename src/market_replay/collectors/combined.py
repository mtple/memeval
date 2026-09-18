"""A week on every venue at once: runs the per-venue collectors one after another (each resumable on
its own), then merges their packs into one. One leaderboard per week, whatever pool type a swap
happened in.

Config: ``protocol: all`` plus ``venues`` (default ``["uniswap_v2", "uniswap_v4"]``), ``venue_pairs``, and
``include_launches`` (every pool launched inside the period, from ``launches.py``, merged in)
(pools per venue; default splits ``max_pairs`` three quarters to v4, the rest to v2). Every other key is
passed to each venue's collector unchanged, except the selection rule: v2 takes ``selection_rule``,
concentrated-liquidity venues take ``selection_rule_cl`` (launches from inside the week by default).

Sub-collections live inside the combined work directory, so the same per-file sync that keeps a
single-venue week resumable across instances keeps this one resumable too.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import yaml

from ..datasets.pack import Pack
from ..domain.status import UseStatus

DEFAULT_VENUES = ["uniswap_v2", "uniswap_v4"]


def venue_pairs(cfg: dict[str, Any], venues: list[str]) -> dict[str, int]:
    given = cfg.get("venue_pairs")
    if isinstance(given, dict) and given:
        return {v: int(given.get(v, 0)) for v in venues}
    total = int(cfg.get("max_pairs", 16))
    if len(venues) == 1:
        return {venues[0]: total}
    cl = [v for v in venues if v != "uniswap_v2"]
    v2 = total - (total * 3 // 4) if "uniswap_v2" in venues else 0
    per_cl = (total - v2) // max(1, len(cl))
    out = {v: per_cl for v in cl}
    if "uniswap_v2" in venues:
        out["uniswap_v2"] = v2
    return out


def run_combined_collection(
    config_path: Path,
    data_dir: Path,
    *,
    transport: httpx.BaseTransport | None = None,
    rpc_url_override: str | None = None,
    sleep=None,
    deadline: float | None = None,
    store_bodies: bool = True,
    budget_used: int = 0,
) -> dict[str, Any]:
    from .historical import run_collection

    cfg = yaml.safe_load(config_path.read_text())
    venues = [str(v) for v in (cfg.get("venues") or DEFAULT_VENUES)]
    pairs = venue_pairs(cfg, venues)
    out_dir = Path(cfg["out_dir"]) if Path(cfg["out_dir"]).is_absolute() else data_dir / cfg["out_dir"]
    work = out_dir.parent / (out_dir.name + "_work")
    work.mkdir(parents=True, exist_ok=True)
    rel_work = str(Path(cfg["out_dir"]).parent / (Path(cfg["out_dir"]).name + "_work"))
    used = int(budget_used)
    logs: list[str] = []
    results: dict[str, dict[str, Any]] = {}
    for venue in venues:
        sub_out = work / venue
        done = work / f"{venue}_result.json"
        if done.exists() and (sub_out / "manifest.yaml").exists():
            res = json.loads(done.read_text())
            results[venue] = res
            logs.extend(f"[{venue}] {line}" for line in res.get("decision_log", []))
            continue
        sub_cfg = {k: v for k, v in cfg.items() if k not in ("venues", "venue_pairs")}
        sub_cfg["protocol"] = venue
        sub_cfg["max_pairs"] = pairs.get(venue, 0)
        sub_cfg["out_dir"] = f"{rel_work}/{venue}" if not Path(cfg["out_dir"]).is_absolute() else str(sub_out)
        # v2 keeps the job's rule (pre-window activity by default); concentrated-liquidity venues add launches
        sub_cfg["selection_rule"] = cfg.get("selection_rule", "active_before_window_earliest_created_v1") if venue == "uniswap_v2" else cfg.get("selection_rule_cl", "active_before_window_plus_window_launches_v1")
        sub_path = work / f"{venue}.yaml"
        sub_path.write_text(yaml.safe_dump(sub_cfg, sort_keys=False))
        res = run_collection(sub_path, data_dir, transport=transport, rpc_url_override=rpc_url_override, sleep=sleep, deadline=deadline, store_bodies=store_bodies, budget_used=used)
        used = int(res.get("budget", {}).get("requests", used))
        if res["status"] != "pack_built":
            res = dict(res)
            res["venue"] = venue
            res["work_dir"] = str(work)
            res["decision_log"] = logs + [f"[{venue}] {line}" for line in res.get("decision_log", [])]
            res["budget"] = {**res.get("budget", {}), "requests": used}
            return res
        done.write_text(json.dumps({k: v for k, v in res.items() if k != "work_dir"}, default=str))
        results[venue] = res
        logs.extend(f"[{venue}] {line}" for line in res.get("decision_log", []))
    parts = list(venues)
    if cfg.get("include_launches"):
        # Every launch of the period, in bulk (see launches.py); the venues above supply the established pools.
        from .launches import run_launch_collection

        done = work / "launches_result.json"
        if done.exists() and (work / "launches" / "manifest.yaml").exists():
            res = json.loads(done.read_text())
        else:
            sub_cfg = {k: v for k, v in cfg.items() if k not in ("venues", "venue_pairs", "include_launches")}
            sub_cfg["venues"] = [str(v) for v in (cfg.get("launch_venues") or venues)]
            sub_cfg["out_dir"] = f"{rel_work}/launches" if not Path(cfg["out_dir"]).is_absolute() else str(work / "launches")
            sub_path = work / "launches.yaml"
            sub_path.write_text(yaml.safe_dump(sub_cfg, sort_keys=False))
            res = run_launch_collection(sub_path, data_dir, transport=transport, rpc_url_override=rpc_url_override, sleep=sleep, deadline=deadline, store_bodies=store_bodies, budget_used=used)
            used = int(res.get("budget", {}).get("requests", used))
            if res["status"] != "pack_built":
                res = dict(res)
                res["venue"] = "launches"
                res["work_dir"] = str(work)
                res["decision_log"] = logs + [f"[launches] {line}" for line in res.get("decision_log", [])]
                res["budget"] = {**res.get("budget", {}), "requests": used}
                return res
            done.write_text(json.dumps({k: v for k, v in res.items() if k != "work_dir"}, default=str))
        results["launches"] = res
        logs.extend(f"[launches] {line}" for line in res.get("decision_log", []))
        parts.append("launches")
    pack = merge_packs(out_dir, [Pack.load(work / v) for v in parts], cfg, logs)
    tape_events = pack.tape_count
    return {
        "status": "pack_built",
        "pack_id": pack.pack_id,
        "pack_dir": str(out_dir),
        "work_dir": str(work),
        "qualification": pack.validation["resulting_qualification"],
        "tape_events": tape_events,
        "pools": len(pack.pools),
        "venues": {v: {"pools": results[v].get("pools"), "tape_events": results[v].get("tape_events"), "qualification": results[v].get("qualification")} for v in parts},
        "budget": {"requests": used},
        "decision_log": logs + [f"merged {len(venues)} venues into one pack: {len(pack.pools)} pools, {tape_events} events"],
    }


def merge_packs(out_dir: Path, packs: list[Pack], cfg: dict[str, Any], decision_log: list[str]) -> Pack:
    """One pack from several venues' packs of the same period: assets and pools are unioned, the tapes are
    interleaved by block and log index, coverage and inventories are concatenated. Everything the
    validator checks is re-checked on the merged whole."""
    from ..datasets.builder import build_pack, make_period
    from ..datasets.execution_params import historical_research_params
    from ..domain.models import Rights, Universe
    from ..domain.status import DataOrigin, TokenBehavior
    from .historical import BLOCK_INTERVAL_MS

    first = packs[0]
    m0 = first.manifest
    assets: dict[str, dict[str, Any]] = {}
    pools: dict[str, dict[str, Any]] = {}
    tape: list[dict[str, Any]] = []
    intervals: list[dict[str, Any]] = []
    ledger_rows: list[Any] = []
    unsupported_events: list[Any] = []
    inventory: dict[str, Any] = {"unsupported": [], "excluded_by_sampling": [], "missing": [], "demoted": [], "candidate_count": 0, "selected_count": 0, "venues": {}}
    counts: dict[str, int] = {}
    factories: list[str] = []
    models: list[Any] = []
    rules: list[str] = []
    ranges: list[list[int]] = []
    candidate_count = selected_count = unsupported_count = missing_count = 0
    pre_start = min(p.manifest.period.prehistory_start_utc_ms or p.manifest.period.start_utc_ms for p in packs)
    requests = 0
    for p in packs:
        m = p.manifest
        for k, a in p.assets.items():
            assets.setdefault(k, a.model_dump(mode="json"))
        for k, pool in p.pools.items():
            pools[k] = pool.model_dump(mode="json")
        tape.extend(dict(r) for r in p.iter_tape())
        cov = p.coverage or {}
        intervals.extend(cov.get("intervals", []))
        ledger_rows.extend(cov.get("ledger", []))
        unsupported_events.extend(cov.get("unsupported_events", []))
        requests += int((cov.get("budget") or {}).get("requests", 0) or 0)
        inv = p.inventory or {}
        for key in ("unsupported", "excluded_by_sampling", "missing", "demoted"):
            inventory[key].extend(inv.get(key, []))
        inventory["candidate_count"] += int(inv.get("candidate_count", 0) or 0)
        inventory["selected_count"] += int(inv.get("selected_count", 0) or 0)
        inventory["unsupported_count"] = int(inventory.get("unsupported_count", 0) or 0) + int(inv.get("unsupported_count", len(inv.get("unsupported", []))) or 0)
        proto = m.universe.selection_rule_version if m.universe.selection_rule_version.startswith("all_launches") else (next(iter(p.pools.values())).protocol if p.pools else m.universe.selection_rule_version)
        inventory["venues"][proto] = {"pools": len(p.pools), "tape_events": p.tape_count, "qualification": p.validation.get("resulting_qualification")}
        u = m.universe
        factories.extend(f for f in u.factories if f not in factories)
        models.extend(x for x in u.pool_models if x not in models)
        rules.append(f"{proto}: {u.selection_rule_version}")
        ranges.extend(u.indexed_block_ranges)
        for k, v in u.excluded_or_unsupported_counts.items():
            counts[k] = counts.get(k, 0) + int(v)
        candidate_count += u.candidate_count
        selected_count += u.selected_count
        unsupported_count += u.unsupported_count
        missing_count += u.missing_count
    tape.sort(key=lambda r: (int(r["block"]), int(r["log_index"]), int(r["seq"])))
    for i, r in enumerate(tape, start=1):
        r["seq"] = i
    delay_ms = int(cfg.get("availability_delay_ms", 4000))
    interval_check = (first.coverage or {}).get("interval_check")
    coverage = {"schema": "coverage_v1", "basis": "evm_rpc_logs", "intervals": intervals, "ledger": ledger_rows, "interval_check": interval_check, "budget": {"requests": requests}, "unsupported_events": unsupported_events}
    return build_pack(
        out_dir,
        origin=DataOrigin.HISTORICAL_RECONSTRUCTION,
        chain=cfg["chain"],
        chain_id=m0.chain_id,
        scope_label="base_all_venues_research_v1",
        title_private=f"Base all venues {cfg['period_start_utc']}..{cfg['period_end_utc']}",
        period=make_period(m0.period.start_utc_ms, m0.period.end_utc_ms, pre_start),
        universe=Universe(
            factories=factories,
            pool_models=models,
            quote_asset=m0.universe.quote_asset,
            selection_rule_version="per_venue_v1: " + "; ".join(rules),
            indexed_block_ranges=ranges,
            excluded_or_unsupported_counts=counts,
            candidate_count=candidate_count,
            selected_count=selected_count,
            unsupported_count=unsupported_count,
            missing_count=missing_count,
            description="One week across venues: " + " | ".join(p.manifest.universe.description for p in packs),
        ),
        assets=list(assets.values()),
        pools=list(pools.values()),
        tape=tape,
        params=historical_research_params(block_interval_ms=BLOCK_INTERVAL_MS, availability_delay_ms=delay_ms),
        coverage=coverage,
        numeraire=m0.numeraire,
        numeraire_alias=m0.numeraire_alias,
        numeraire_decimals=m0.numeraire_decimals,
        token_behavior=TokenBehavior.ASSUMED_STANDARD_TRANSFER,
        availability_model=dict(m0.data.availability_model),
        rights=Rights(**m0.rights.model_dump()),
        qualification=UseStatus.RESEARCH,
        inventory=inventory,
        provenance_notes=sorted({n for p in packs for n in p.manifest.provenance_notes}),
        decision_log=decision_log,
    )
