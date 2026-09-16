"""Paired comparison of two fixed agent versions on the same suite of episodes.

Shows per-episode paired outcomes first, then descriptive medians/means/ranges.
Every attempted run is listed, including crashes and incomplete runs. No
significance test, promotion verdict or profit projection is produced.
"""

from __future__ import annotations

from decimal import Decimal
from statistics import mean, median
from typing import Any

COMPARISON_VERSION = "paired_comparison_v1"


def _d(s: str | None) -> Decimal | None:
    return None if s is None else Decimal(s)


def pair_runs(runs_a: list[dict[str, Any]], runs_b: list[dict[str, Any]]) -> dict[str, Any]:
    """Each run dict: {run_id, pack_id, episode_label, state, agent_version, report(optional), profile_hash, mask_seed, capabilities}."""
    by_pack_a: dict[str, list[dict[str, Any]]] = {}
    by_pack_b: dict[str, list[dict[str, Any]]] = {}
    for r in runs_a:
        by_pack_a.setdefault(r["pack_id"], []).append(r)
    for r in runs_b:
        by_pack_b.setdefault(r["pack_id"], []).append(r)
    packs = sorted(set(by_pack_a) | set(by_pack_b))
    warnings: list[str] = []
    per_episode: list[dict[str, Any]] = []
    diffs: list[Decimal] = []
    fee_diffs: list[Decimal] = []
    dd_diffs: list[Decimal] = []
    profiles = {r.get("profile_hash") for r in runs_a + runs_b}
    if len(profiles) > 1:
        warnings.append("MISMATCHED_EXECUTION_PROFILES")
    caps_a = {tuple(sorted(r.get("capabilities") or [])) for r in runs_a}
    caps_b = {tuple(sorted(r.get("capabilities") or [])) for r in runs_b}
    if caps_a and caps_b and caps_a != caps_b:
        warnings.append("MISMATCHED_CAPABILITIES")
    origins = {(r.get("report") or {}).get("status_dimensions", {}).get("data_origin") for r in runs_a + runs_b if r.get("report")}
    if len(origins) > 1:
        warnings.append("MIXED_DATA_ORIGINS_NOT_POOLED")
    for pk in packs:
        ra = by_pack_a.get(pk, [])
        rb = by_pack_b.get(pk, [])
        if not ra or not rb:
            warnings.append(f"UNPAIRED_EPISODE:{pk[:12]}")
        seeds_a = {r.get("mask_seed") for r in ra}
        seeds_b = {r.get("mask_seed") for r in rb}
        if ra and rb and seeds_a != seeds_b:
            warnings.append(f"DIFFERENT_MASK_SEEDS:{pk[:12]}")

        def summarize(rs: list[dict[str, Any]]) -> list[dict[str, Any]]:
            out = []
            for r in rs:
                rep = r.get("report") or {}
                oc = rep.get("outcome", {})
                out.append(
                    {
                        "run_id": r["run_id"],
                        "state": r["state"],
                        "headline_return": oc.get("headline_return"),
                        "valuation_complete": oc.get("valuation_complete"),
                        "max_drawdown": rep.get("risk", {}).get("max_drawdown"),
                        "gas_total_raw": rep.get("costs", {}).get("gas_total_raw"),
                        "confirmed_fills": rep.get("activity", {}).get("confirmed_fills"),
                        "unresolved_orders": len(rep.get("unresolved", {}).get("orders", [])),
                        "unpriced_inventory": len(rep.get("unresolved", {}).get("unpriced_inventory", [])),
                        "error": r.get("error"),
                    }
                )
            return out

        sa, sb = summarize(ra), summarize(rb)
        # Pair the first completed run of each side (all runs remain listed).
        fa = next((s for s in sa if s["state"] == "completed" and s["headline_return"] is not None), None)
        fb = next((s for s in sb if s["state"] == "completed" and s["headline_return"] is not None), None)
        diff = None
        fee_diff = None
        dd_diff = None
        if fa and fb:
            diff = _d(fa["headline_return"]) - _d(fb["headline_return"])  # type: ignore[operator]
            diffs.append(diff)
            if fa["gas_total_raw"] is not None and fb["gas_total_raw"] is not None:
                fee_diff = Decimal(fa["gas_total_raw"]) - Decimal(fb["gas_total_raw"])
                fee_diffs.append(fee_diff)
            if fa["max_drawdown"] is not None and fb["max_drawdown"] is not None:
                dd_diff = _d(fa["max_drawdown"]) - _d(fb["max_drawdown"])  # type: ignore[operator]
                dd_diffs.append(dd_diff)
        per_episode.append(
            {
                "episode_label": (ra or rb)[0].get("episode_label"),
                "runs_a": sa,
                "runs_b": sb,
                "paired": bool(fa and fb),
                "return_diff_a_minus_b": None if diff is None else str(diff),
                "gas_diff_a_minus_b_raw": None if fee_diff is None else str(fee_diff),
                "drawdown_diff_a_minus_b": None if dd_diff is None else str(dd_diff),
            }
        )
    summary: dict[str, Any] = {
        "episodes_total": len(packs),
        "episodes_paired": len(diffs),
        "runs_attempted_a": len(runs_a),
        "runs_attempted_b": len(runs_b),
        "runs_not_completed_a": sum(1 for r in runs_a if r["state"] != "completed"),
        "runs_not_completed_b": sum(1 for r in runs_b if r["state"] != "completed"),
    }
    if diffs:
        summary["return_diff"] = {
            "median": str(median(diffs)),
            "mean": str(mean(diffs)),
            "min": str(min(diffs)),
            "max": str(max(diffs)),
            "a_better_count": sum(1 for d in diffs if d > 0),
            "b_better_count": sum(1 for d in diffs if d < 0),
        }
    if fee_diffs:
        summary["gas_diff_raw"] = {"median": str(median(fee_diffs)), "min": str(min(fee_diffs)), "max": str(max(fee_diffs))}
    if dd_diffs:
        summary["drawdown_diff"] = {"median": str(median(dd_diffs)), "min": str(min(dd_diffs)), "max": str(max(dd_diffs))}
    if len(diffs) < 8:
        warnings.append("FEW_DISTINCT_PERIODS_DESCRIPTIVE_ONLY")
    return {
        "comparison_version": COMPARISON_VERSION,
        "per_episode": per_episode,
        "summary": summary,
        "warnings": sorted(set(warnings)),
        "evidence_counts": {
            "unique_calendar_periods": len(packs),
            "chains": len({r.get("chain") for r in runs_a + runs_b if r.get("chain")}),
            "stochastic_trials_a": len(runs_a) - len(by_pack_a),
            "stochastic_trials_b": len(runs_b) - len(by_pack_b),
        },
        "statement": "One version did better in these episodes or it did not; the sample and execution assumptions do not establish future improvement. No significance test or promotion verdict is computed.",
    }
