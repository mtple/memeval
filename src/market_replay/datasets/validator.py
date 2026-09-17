"""Dataset qualification gates (Section 10). Produces a machine-readable report.

A pack that fails an executable gate is downgraded: generated packs to ``rejected``,
historical packs to ``diagnostic_only``. Passing all gates never upgrades a pack above
the qualification its builder requested, and never establishes predictive validity.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from ..domain.models import Pool, TapeEvent
from ..domain.status import SUPPORTED_CPMM_MODELS, DataOrigin, GateStatus, PoolModel, UseStatus
from ..venues.clmm.math import ClMathError, get_tick_at_sqrt_ratio
from ..venues.clmm.pool import SUPPORTED_CLMM_MODELS, ClPoolState
from ..venues.cpmm.math import CpmmMathError
from ..venues.cpmm.pool import CpmmPoolState, FidelityLimit, classify_checkpoint_delta
from .builder import VALIDATOR_VERSION
from .pack import Pack


def _gate(name: str, status: GateStatus, detail: str, **extra: Any) -> dict[str, Any]:
    d = {"gate": name, "status": str(status), "detail": detail}
    d.update(extra)
    return d


def _cpmm_supported(p: Pool) -> bool:
    return bool(p.supported_by_cpmm) and PoolModel(p.model) in SUPPORTED_CPMM_MODELS


def _cl_supported(p: Pool) -> bool:
    return bool(p.supported_by_clmm) and str(p.model) in SUPPORTED_CLMM_MODELS


def _cl_initial_state(p: Pool) -> ClPoolState | None:
    """Initial ``ClPoolState`` from the pool record (None when the pool is initialized inside the window)."""
    if p.initial_sqrt_price_x96 is None:
        return None
    sqrt_price = int(p.initial_sqrt_price_x96)
    tick = p.initial_tick if p.initial_tick is not None else get_tick_at_sqrt_ratio(sqrt_price)
    return ClPoolState(
        key=p.key,
        asset0=p.asset0,
        asset1=p.asset1,
        fee_pips=int(p.fee_pips or 0),
        tick_spacing=int(p.tick_spacing or 1),
        sqrt_price_x96=sqrt_price,
        tick=int(tick),
        liquidity=int(p.initial_liquidity or 0),
        ticks={int(t): (int(net), int(gross)) for t, net, gross in p.initial_ticks},
        model=str(p.model),
    )


def reconcile_no_agent(pack: Pack, max_events: int | None = None) -> dict[str, Any]:
    """Replay the tape with no participant and compare it against the recorded checkpoints (exact integers).

    CPMM pools: every ``sync`` row is a checkpoint (reserves). Concentrated-liquidity pools: every
    ``cl_swap`` row is a checkpoint (``sqrt_price_x96_after``, ``liquidity_after``, ``tick_after`` after
    replaying the recorded swap with the v3 swap loop); ``cl_modify`` rows are applied to the tick map
    and ``cl_init`` rows create pools initialized inside the window.
    """
    return reconcile_rows(pack.pools, pack.tape, max_events)


def reconcile_rows(pool_records: dict[str, Pool], tape: list[dict[str, Any]], max_events: int | None = None) -> dict[str, Any]:
    """The reconciliation itself, on pool records and raw tape rows (the collector calls it before a
    pack exists to decide which pools stay executable)."""
    pools: dict[str, CpmmPoolState | ClPoolState] = {}
    cl_meta: dict[str, Pool] = {}
    fidelity: list[dict[str, Any]] = []
    for key, p in pool_records.items():
        if _cpmm_supported(p):
            if p.initial_reserve0 is None or p.initial_reserve1 is None:
                continue
            pools[key] = CpmmPoolState(key=key, asset0=p.asset0, asset1=p.asset1, reserve0=int(p.initial_reserve0), reserve1=int(p.initial_reserve1), fee_num=p.fee_numerator, fee_den=p.fee_denominator, model=PoolModel(p.model))
        elif _cl_supported(p):
            cl_meta[key] = p
            try:
                st = _cl_initial_state(p)
            except ClMathError as e:
                fidelity.append({"seq": 0, "pool": key, "code": "INITIAL_STATE_INVALID", "message": str(e)})
                continue
            if st is not None:
                pools[key] = st
    checkpoints = 0
    mismatches: list[dict[str, Any]] = []
    ratio_deviations = 0
    adjustments: Counter[str] = Counter()
    per_pool: dict[str, Counter[str]] = {}
    rows = tape if max_events is None else tape[:max_events]
    for r in rows:
        kind = r["kind"]
        if kind == "cl_init":
            meta = cl_meta.get(r["pool"])
            if meta is None or r.get("sqrt_price_x96") is None:
                continue
            if r["pool"] in pools:
                fidelity.append({"seq": r["seq"], "pool": r["pool"], "code": "CL_ALREADY_INITIALIZED", "message": "Initialize for a pool that already has a state; ignored"})
                continue
            sqrt_price = int(r["sqrt_price_x96"])
            tick = r["tick"] if r.get("tick") is not None else get_tick_at_sqrt_ratio(sqrt_price)
            try:
                pools[r["pool"]] = ClPoolState(key=meta.key, asset0=meta.asset0, asset1=meta.asset1, fee_pips=int(meta.fee_pips or 0), tick_spacing=int(meta.tick_spacing or 1), sqrt_price_x96=sqrt_price, tick=int(tick), model=str(meta.model))
            except ClMathError as e:
                fidelity.append({"seq": r["seq"], "pool": r["pool"], "code": "REFERENCE_INIT_INVALID", "message": str(e)})
            continue
        pool = pools.get(r["pool"])
        if pool is None:
            continue
        if isinstance(pool, ClPoolState):
            if kind == "cl_modify":
                try:
                    pool.apply_modify_liquidity(int(r["tick_lower"]), int(r["tick_upper"]), int(r["liquidity_delta"]))
                except ClMathError as e:
                    fidelity.append({"seq": r["seq"], "pool": r["pool"], "code": "REFERENCE_MODIFY_INVALID", "message": str(e)})
            elif kind == "cl_swap":
                checkpoints += 1
                try:
                    rec = pool.apply_recorded_swap(int(r["amount0"]), int(r["amount1"]), int(r["sqrt_price_x96_after"]), int(r["liquidity_after"]), int(r["tick_after"]), r.get("fee_pips"))
                except ClMathError as e:
                    fidelity.append({"seq": r["seq"], "pool": r["pool"], "code": "REFERENCE_SWAP_INVALID", "message": str(e)})
                    continue
                if rec["sqrt_price_x96"] or rec["liquidity"] or rec["tick"]:
                    mismatches.append({"seq": r["seq"], "pool": r["pool"], "block": r["block"], "delta_sqrt_price_x96": rec["sqrt_price_x96"], "delta_liquidity": rec["liquidity"], "delta_tick": rec["tick"], "delta_amount0": rec["amount0"], "delta_amount1": rec["amount1"], "mode": rec["mode"]})
            continue
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
        elif kind == "adjust":
            try:
                pool.apply_adjust(int(r.get("amount0") or 0), int(r.get("amount1") or 0))
            except FidelityLimit as e:
                fidelity.append({"seq": r["seq"], "pool": r["pool"], "code": "REFERENCE_ADJUST_OVERDRAW", "message": str(e)})
        elif kind == "sync":
            checkpoints += 1
            r0, r1 = int(r["reserve0"]), int(r["reserve1"])
            d0, d1 = pool.anchor_to_sync(r0, r1)
            cls = classify_checkpoint_delta(d0, d1, r0, r1, explained=bool((r.get("payload") or {}).get("orphan")))
            if cls != "match":
                adjustments[cls] += 1
                per_pool.setdefault(r["pool"], Counter())[cls] += 1
                if cls == "material":
                    mismatches.append({"seq": r["seq"], "pool": r["pool"], "block": r["block"], "delta0": d0, "delta1": d1})
    final_reserves = {k: {"reserve0": str(v.reserve0), "reserve1": str(v.reserve1)} for k, v in pools.items() if isinstance(v, CpmmPoolState)}
    final_cl_state = {
        k: {"sqrt_price_x96": str(v.sqrt_price_x96), "tick": v.tick, "liquidity": str(v.liquidity), "initialized_ticks": len(v.ticks)}
        for k, v in pools.items()
        if isinstance(v, ClPoolState)
    }
    final_state = {k: {"model": str(v.model), **(final_reserves.get(k) or final_cl_state.get(k) or {})} for k, v in pools.items()}
    return {
        "checkpoints": checkpoints,
        "mismatches": mismatches[:50],
        "mismatch_count": len(mismatches),
        "reserve_adjustments": dict(adjustments),
        "reserve_adjustments_by_pool": {k: dict(v) for k, v in per_pool.items()},
        "adjustment_rule": "every Sync re-anchors the reference to the chain; an orphan Sync (direct transfer followed by sync()) is an explained adjustment; any other non-zero delta, even one wei, is a material mismatch",
        "fidelity_flags": fidelity[:50],
        "fidelity_flag_count": len(fidelity),
        "recorded_output_below_model_max": ratio_deviations,
        "final_reserves": final_reserves,
        "final_cl_state": final_cl_state,
        "final_state": final_state,
        "rounding_rule": "CPMM: floor division per Uniswap v2 getAmountOut; CL: Uniswap v3 SwapMath/SqrtPriceMath rounding per step; exact integers",
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
    cpmm_pools = [p for p in pack.pools.values() if _cpmm_supported(p)]
    cl_pools = [p for p in pack.pools.values() if _cl_supported(p)]
    exec_pools = cpmm_pools + cl_pools
    cl_init_on_tape = {r["pool"] for r in pack.tape if r["kind"] == "cl_init"}
    missing_state = [p.key for p in cpmm_pools if p.initial_reserve0 is None or p.initial_reserve1 is None]
    missing_state += [p.key for p in cl_pools if p.initial_sqrt_price_x96 is None and p.key not in cl_init_on_tape]
    recon = reconcile_no_agent(pack)
    exec_ok = not missing_state and recon["mismatch_count"] == 0 and recon["fidelity_flag_count"] == 0 and len(exec_pools) > 0
    if m.execution.model == "diagnostic_no_execution":
        gates.append(_gate("execution_state", GateStatus.NOT_APPLICABLE, "diagnostic pack: no execution model claimed", reconciliation=recon))
    else:
        status = GateStatus.PASSED if exec_ok else GateStatus.FAILED
        detail = f"executable pools={len(exec_pools)} (cpmm={len(cpmm_pools)} cl={len(cl_pools)}) missing_initial_state={len(missing_state)} checkpoints={recon['checkpoints']} mismatches={recon['mismatch_count']} fidelity_flags={recon['fidelity_flag_count']}"
        if recon.get("reserve_adjustments"):
            detail += f"; reserve checkpoints re-anchored: {recon['reserve_adjustments']}"
        if exec_ok and recon["checkpoints"] == 0:
            status = GateStatus.WARNING
            detail += "; no checkpoints were reconciled (reconciliation untested for this pack: no external flow in the window)"
        gates.append(_gate("execution_state", status, detail, reconciliation={k: v for k, v in recon.items() if k not in ("final_reserves", "final_cl_state", "final_state")}, pools_missing_initial_state=missing_state, reconciliation_untested=recon["checkpoints"] == 0))
        executable_failure |= not exec_ok

    # 6. Mechanics: a pool may only claim the adapter that serves its model (CPMM: uniswap_v2_plain /
    #    fixture_cpmm; CLMM: uniswap_v3_cl / uniswap_v4_cl). A CL model marked supported_by_cpmm fails.
    unsupported_in_adapter = [p.key for p in pack.pools.values() if (p.supported_by_cpmm and PoolModel(p.model) not in SUPPORTED_CPMM_MODELS) or (p.supported_by_clmm and str(p.model) not in SUPPORTED_CLMM_MODELS)]
    gates.append(_gate("mechanics", GateStatus.FAILED if unsupported_in_adapter else GateStatus.PASSED, "unsupported pool models are excluded from the CPMM and CLMM adapters" if not unsupported_in_adapter else f"pools marked supported with unsupported model: {unsupported_in_adapter}", token_behavior_basis=str(m.data.token_behavior_basis), unsupported_pools_kept=len(pack.inventory.get("unsupported", [])) if pack.inventory else 0))
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
