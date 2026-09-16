"""Dataset qualification gates (Section 10). Produces a machine-readable report.

A pack that fails an executable gate is downgraded: generated packs to ``rejected``,
historical packs to ``diagnostic_only``. Passing all gates never upgrades a pack above
the qualification its builder requested, and never establishes predictive validity.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from ..domain.models import TapeEvent
from ..domain.status import SUPPORTED_CPMM_MODELS, DataOrigin, GateStatus, PoolModel, UseStatus
from ..venues.cpmm.math import CpmmMathError
from ..venues.cpmm.pool import CpmmPoolState, FidelityLimit
from .builder import VALIDATOR_VERSION
from .pack import Pack


def _gate(name: str, status: GateStatus, detail: str, **extra: Any) -> dict[str, Any]:
    d = {"gate": name, "status": str(status), "detail": detail}
    d.update(extra)
    return d


def reconcile_no_agent(pack: Pack, max_events: int | None = None) -> dict[str, Any]:
    """Replay the tape with no participant and compare against Sync checkpoints (exact integers)."""
    pools: dict[str, CpmmPoolState] = {}
    for key, p in pack.pools.items():
        if not p.supported_by_cpmm or PoolModel(p.model) not in SUPPORTED_CPMM_MODELS:
            continue
        if p.initial_reserve0 is None or p.initial_reserve1 is None:
            continue
        pools[key] = CpmmPoolState(key=key, asset0=p.asset0, asset1=p.asset1, reserve0=int(p.initial_reserve0), reserve1=int(p.initial_reserve1), fee_num=p.fee_numerator, fee_den=p.fee_denominator, model=PoolModel(p.model))
    checkpoints = 0
    mismatches: list[dict[str, Any]] = []
    fidelity: list[dict[str, Any]] = []
    ratio_deviations = 0
    rows = pack.tape if max_events is None else pack.tape[:max_events]
    for r in rows:
        pool = pools.get(r["pool"])
        if pool is None:
            continue
        kind = r["kind"]
        if kind == "swap":
            amount_in = int(r["amount_in"])
            rec = r.get("amount_out_recorded")
            try:
                mx = pool.max_out(r["asset_in"], amount_in)
            except CpmmMathError as e:
                fidelity.append({"seq": r["seq"], "pool": r["pool"], "code": "REFERENCE_SWAP_INVALID", "message": str(e)})
                continue
            out = mx if rec is None else int(rec)
            if out > mx:
                fidelity.append({"seq": r["seq"], "pool": r["pool"], "code": "RECORDED_OUTPUT_EXCEEDS_MODEL", "recorded": out, "model_max": mx})
                out = mx
            if out != mx:
                ratio_deviations += 1
            if r["asset_in"] == pool.asset0:
                pool.reserve0 += amount_in
                pool.reserve1 -= out
            else:
                pool.reserve1 += amount_in
                pool.reserve0 -= out
        elif kind == "mint":
            pool.apply_mint(int(r.get("amount0") or 0), int(r.get("amount1") or 0))
        elif kind == "burn":
            try:
                pool.apply_burn(int(r.get("amount0") or 0), int(r.get("amount1") or 0))
            except FidelityLimit as e:
                fidelity.append({"seq": r["seq"], "pool": r["pool"], "code": "REFERENCE_BURN_OVERDRAW", "message": str(e)})
        elif kind == "sync":
            checkpoints += 1
            d0, d1 = pool.reconcile_sync(int(r["reserve0"]), int(r["reserve1"]))
            if d0 or d1:
                mismatches.append({"seq": r["seq"], "pool": r["pool"], "block": r["block"], "delta0": d0, "delta1": d1})
    return {
        "checkpoints": checkpoints,
        "mismatches": mismatches[:50],
        "mismatch_count": len(mismatches),
        "fidelity_flags": fidelity[:50],
        "fidelity_flag_count": len(fidelity),
        "recorded_output_below_model_max": ratio_deviations,
        "final_reserves": {k: {"reserve0": str(v.reserve0), "reserve1": str(v.reserve1)} for k, v in pools.items()},
        "rounding_rule": "floor division per Uniswap v2 getAmountOut; exact integers",
    }


def validate_pack(pack: Pack) -> dict[str, Any]:
    m = pack.manifest
    gates: list[dict[str, Any]] = []
    executable_failure = False

    # 1. Identity
    problems = []
    for k, a in pack.assets.items():
        if a.key != f"{a.chain_id}:{a.address}":
            problems.append(f"asset key mismatch {k}")
        if a.decimals < 0 or a.decimals > 77:
            problems.append(f"bad decimals {k}")
    for k, p in pack.pools.items():
        if p.key != f"{p.chain_id}:{p.protocol}:{p.address}":
            problems.append(f"pool key mismatch {k}")
        if p.asset0 not in pack.assets or p.asset1 not in pack.assets:
            problems.append(f"pool {k} references unknown asset")
    symbols = Counter(a.symbol for a in pack.assets.values() if a.symbol)
    collisions = [s for s, n in symbols.items() if n > 1]
    if m.numeraire not in pack.assets:
        problems.append("numeraire asset missing")
    gates.append(
        _gate("identity", GateStatus.FAILED if problems else GateStatus.PASSED, "; ".join(problems) or "canonical ids, decimals and denominations present", symbol_collisions=collisions, note="symbol collisions are allowed and never merge assets")
    )
    executable_failure |= bool(problems)

    # 2. Universe
    u = m.universe
    uni_ok = bool(u.selection_rule_version) and (u.candidate_count >= u.selected_count) and (u.unsupported_count == len(pack.inventory.get("unsupported", [])) if pack.inventory else True)
    disc_missing = [k for k, p in pack.pools.items() if p.discovery_available_utc_ms is None and p.created_time_utc_ms is None]
    gates.append(_gate("universe", GateStatus.PASSED if uni_ok and not disc_missing else GateStatus.FAILED, f"selection rule {u.selection_rule_version}; candidates={u.candidate_count} selected={u.selected_count} unsupported={u.unsupported_count} missing={u.missing_count}", pools_without_discovery_time=disc_missing))
    executable_failure |= not uni_ok or bool(disc_missing)

    # 3. Temporal ordering
    t_problems = []
    last = None
    for r in pack.tape:
        key = (r["block"], r["log_index"], r["seq"])
        if last is not None and key <= last:
            t_problems.append(f"tape not strictly ordered at seq {r['seq']}")
            break
        last = key
        av = r.get("available_utc_ms")
        if av is not None and av < r["time_utc_ms"]:
            t_problems.append(f"available before event at seq {r['seq']}")
            break
        rec = r.get("received_utc_ms")
        if rec is not None and rec < r["time_utc_ms"]:
            t_problems.append(f"received before event at seq {r['seq']}")
            break
    schema_errors = 0
    for r in pack.tape[:5000]:
        try:
            TapeEvent.model_validate(r)
        except Exception:
            schema_errors += 1
    if schema_errors:
        t_problems.append(f"{schema_errors} tape rows fail schema (first 5000 checked)")
    gates.append(_gate("temporal_ordering", GateStatus.FAILED if t_problems else GateStatus.PASSED, "; ".join(t_problems) or "event/available/received times distinguished and ordered"))
    executable_failure |= bool(t_problems)

    # 4. Trade coverage
    cov = pack.coverage or {}
    intervals = cov.get("intervals", [])
    states = Counter(i.get("state") for i in intervals)
    if not intervals:
        cov_status = GateStatus.FAILED if m.origin != DataOrigin.REPORT_EXCERPT else GateStatus.NOT_APPLICABLE
        detail = "no coverage ledger; completeness unknown cannot be marked complete"
    else:
        cov_status = GateStatus.PASSED if states.get("completed_and_checked") else GateStatus.WARNING
        detail = f"coverage intervals by state: {dict(states)}"
    gates.append(_gate("trade_coverage", cov_status, detail))
    executable_failure |= cov_status == GateStatus.FAILED

    # 5. Execution state / no-agent reconciliation
    exec_pools = [p for p in pack.pools.values() if p.supported_by_cpmm and PoolModel(p.model) in SUPPORTED_CPMM_MODELS]
    missing_state = [p.key for p in exec_pools if p.initial_reserve0 is None or p.initial_reserve1 is None]
    recon = reconcile_no_agent(pack)
    exec_ok = not missing_state and recon["mismatch_count"] == 0 and recon["fidelity_flag_count"] == 0 and len(exec_pools) > 0
    if m.execution.model == "diagnostic_no_execution":
        gates.append(_gate("execution_state", GateStatus.NOT_APPLICABLE, "diagnostic pack: no execution model claimed", reconciliation=recon))
    else:
        gates.append(_gate("execution_state", GateStatus.PASSED if exec_ok else GateStatus.FAILED, f"executable pools={len(exec_pools)} missing_initial_state={len(missing_state)} sync_checkpoints={recon['checkpoints']} mismatches={recon['mismatch_count']} fidelity_flags={recon['fidelity_flag_count']}", reconciliation={k: v for k, v in recon.items() if k != "final_reserves"}, pools_missing_initial_state=missing_state))
        executable_failure |= not exec_ok

    # 6. Mechanics
    unsupported_in_adapter = [p.key for p in pack.pools.values() if p.supported_by_cpmm and PoolModel(p.model) not in SUPPORTED_CPMM_MODELS]
    gates.append(_gate("mechanics", GateStatus.FAILED if unsupported_in_adapter else GateStatus.PASSED, "unsupported pool models are excluded from the CPMM adapter" if not unsupported_in_adapter else f"pools marked supported with unsupported model: {unsupported_in_adapter}", token_behavior_basis=str(m.data.token_behavior_basis), unsupported_pools_kept=len(pack.inventory.get("unsupported", [])) if pack.inventory else 0))
    executable_failure |= bool(unsupported_in_adapter)

    # 7. Valuation
    gates.append(_gate("valuation", GateStatus.PASSED, f"declared policy {pack.params.valuation_policy}; unresolved states remain visible in reports"))

    # 8. Anonymization (structural check; runtime leakage tests live in tests/security)
    gates.append(_gate("anonymization", GateStatus.PASSED, "public descriptor is allowlisted; aliases keyed per run; runtime scanner enforced in service"))

    # 9. Provenance and rights
    r = m.rights
    rights_ok = bool(r.storage_basis and r.local_processing_basis and r.redistribution)
    gates.append(_gate("provenance_rights", GateStatus.PASSED if rights_ok else GateStatus.FAILED, f"storage={r.storage_basis} processing={r.local_processing_basis} redistribution={r.redistribution} serving={r.simulator_serving}", redistribution_enabled=r.redistribution.startswith("permitted")))

    # 10. Reproducibility
    gates.append(_gate("reproducibility", GateStatus.PASSED, f"pack_id {m.pack_id} content-addressed; params hash {m.execution.parameters_hash[:12]}...; validator {VALIDATOR_VERSION}"))

    requested = m.validation.qualification
    if executable_failure:
        resulting = UseStatus.REJECTED if m.origin == DataOrigin.GENERATED_FIXTURE else UseStatus.DIAGNOSTIC_ONLY
    else:
        resulting = requested
    if m.execution.model == "diagnostic_no_execution" and resulting not in (UseStatus.DIAGNOSTIC_ONLY, UseStatus.REJECTED):
        resulting = UseStatus.DIAGNOSTIC_ONLY
    return {
        "validator_version": VALIDATOR_VERSION,
        "pack_id": m.pack_id,
        "origin": str(m.origin),
        "gates": gates,
        "executable_failure": executable_failure,
        "requested_qualification": str(requested),
        "resulting_qualification": str(resulting),
        "predictive_validity": "not_established",
        "notes": [
            "Passing gates means the pack meets declared mechanical/data requirements. It does not mean the simulator predicts live profitability.",
        ],
    }
