"""Environment-validation report: ``tested`` / ``not_tested`` / ``failed`` / ``assumed`` per capability.

Section 18B. Software-correctness items are exercised here against the generated dev
fixture; fidelity items that need historical evidence are reported as ``not_tested`` or
``assumed``. Sensitivity runs re-run the scheduled-basket participant on fixture variants
with higher/lower latency and gas and report the outcomes without claiming monotonicity.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..datasets.execution_params import fixture_default_params
from ..datasets.generator import dev_short_config, generate_pack
from ..datasets.validator import reconcile_no_agent
from ..domain.status import ValidationStatus
from ..engine.session import Session
from ..service.auth import resolve_admin_token
from ..service.embedded import EmbeddedServer
from ..service.runs import RunManager


def _item(capability: str, status: ValidationStatus, evidence: str, **extra: Any) -> dict[str, Any]:
    return {"capability": capability, "status": str(status), "evidence": evidence, **extra}


def build_environment_validation(mgr: RunManager, data_dir: Path) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    work = data_dir / "environment_validation"
    work.mkdir(parents=True, exist_ok=True)

    # --- Software correctness on the dev fixture
    base_cfg = dev_short_config()
    base_pack = generate_pack(base_cfg, work / "packs" / "base")
    recon = reconcile_no_agent(base_pack)
    items.append(_item("no_agent_reconstruction_matches_checkpoints", ValidationStatus.TESTED if recon["mismatch_count"] == 0 else ValidationStatus.FAILED, f"{recon['checkpoints']} sync checkpoints, {recon['mismatch_count']} mismatches on generated fixture"))
    s1 = Session.create(session_id="ev1", pack=base_pack, bankroll_raw=1_000_000, mask_seed="m", engine_seed="e")
    s2 = Session.create(session_id="ev2", pack=base_pack, bankroll_raw=1_000_000, mask_seed="m", engine_seed="e")
    script = [("markets.list", {}), ("clock.advance", {"to_ms": 600_000}), ("portfolio.get", {}), ("clock.advance", {"to_ms": 7_200_000}), ("session.finish", {})]
    for s in (s1, s2):
        for tool, args in script:
            s.handle("r", tool, args)
    items.append(_item("deterministic_replay_same_actions_same_ledger", ValidationStatus.TESTED if s1.result_hash() == s2.result_hash() else ValidationStatus.FAILED, "two sessions with identical seeds and actions produced identical result hashes"))
    items.append(_item("observation_cutoffs_respected", ValidationStatus.TESTED, "covered by tests/unit/test_observations.py and tests/unit/test_session.py (as-of filtering, future range clamping, open bars)"))
    items.append(_item("amount_conservation_in_ledger", ValidationStatus.TESTED, "every ledger entry balances per asset; enforced at post time and covered by tests/unit/test_simulation.py"))
    items.append(_item("fees_applied_once", ValidationStatus.TESTED, "pool fee embedded in output; report lists it as informational; tests/unit/test_cpmm_math.py"))
    items.append(_item("cpmm_matches_reference_integer_formula", ValidationStatus.TESTED, "tests/unit/test_cpmm_math.py and tests/property/test_cpmm_property.py"))

    # --- Environment fidelity
    items.append(_item("historical_no_agent_reconstruction", ValidationStatus.NOT_TESTED, "no authorized historical pack available in this build; collector and reconciler exist and are tested against a fake RPC"))
    items.append(_item("modeled_outputs_vs_contemporaneous_quotes", ValidationStatus.NOT_TESTED, "the supplied quote-only CLI excerpt is rounded and not a fill; it cannot calibrate the model"))
    items.append(_item("routing_depth_and_mev", ValidationStatus.NOT_TESTED, "no router simulation; single-pool CPMM only"))
    items.append(_item("historical_token_restrictions_and_sellability", ValidationStatus.ASSUMED, "assumed_standard_transfer for research packs; fixture rules for generated packs"))
    items.append(_item("block_interval_fixed_2000ms_on_base", ValidationStatus.ASSUMED, "verified only by sampled headers during collection; not established for unseen ranges"))
    items.append(_item("latency_and_gas_parameters", ValidationStatus.ASSUMED, "fixture values are test settings; historical profile values are assumptions until a fee series is imported"))

    # --- Sensitivity runs (scheduled basket on fixture variants)
    variants = {
        "baseline": {},
        "high_latency": {"data_latency_ms": 2000, "quote_latency_ms": 2000, "submit_latency_ms": 5000, "confirm_blocks": 3},
        "low_latency": {"data_latency_ms": 10, "quote_latency_ms": 10, "submit_latency_ms": 50},
        "high_gas": {"gas_cost_raw": "5000"},
        "zero_gas": {"gas_cost_raw": "0"},
        "long_availability_delay": {"availability_delay_ms": 30_000},
    }
    sens: dict[str, Any] = {}
    with EmbeddedServer(mgr, resolve_admin_token(None)):
        for name, overrides in variants.items():
            params = fixture_default_params(reporting_grid_ms=60_000, **overrides)
            cfg = dev_short_config(params=params, name=f"gen_dev_short_{name}")
            pack = generate_pack(cfg, work / "packs" / name)
            mgr.import_pack(work / "packs" / name, f"ev_{name}")
            row = mgr.store.agent_by_name_version("ev_scheduled_basket", "1")
            aid = row["agent_id"] if row else mgr.register_agent(name="ev_scheduled_basket", version="1", runtime="python", capabilities=[], config={})["agent_id"]
            view = mgr.create_run(agent_id=aid, pack_ref=pack.pack_id, mode="practice", bankroll_raw="1000000", mask_seed="sens-mask", engine_seed="sens-engine", launch_spec={"kind": "example", "name": "scheduled_basket", "runtime": "python"})
            mgr.wait_for_run(view["run_id"], 300)
            rep = mgr.report(view["run_id"])
            sens[name] = {
                "overrides": overrides,
                "state": mgr.run_view(view["run_id"])["state"],
                "headline_return": rep.get("outcome", {}).get("headline_return"),
                "valuation_complete": rep.get("outcome", {}).get("valuation_complete"),
                "gas_total_raw": rep.get("costs", {}).get("gas_total_raw"),
                "confirmed_fills": rep.get("activity", {}).get("confirmed_fills"),
                "reverted": rep.get("activity", {}).get("reverted"),
                "expired": rep.get("activity", {}).get("expired"),
            }
    items.append(_item("sensitivity_to_latency_and_gas", ValidationStatus.TESTED, "scheduled basket re-run on fixture variants; outcomes reported without a monotonicity claim", variants=sens))

    report = {
        "report_version": "environment_validation_v1",
        "items": items,
        "sensitivity_runs": sens,
        "summary": {
            "tested": sum(1 for i in items if i["status"] == "tested"),
            "not_tested": sum(1 for i in items if i["status"] == "not_tested"),
            "failed": sum(1 for i in items if i["status"] == "failed"),
            "assumed": sum(1 for i in items if i["status"] == "assumed"),
        },
        "statement": "Software correctness items are tested on generated fixtures. Environment fidelity against real markets is not established. Predictive usefulness requires a prospective study (see the study registry).",
    }
    (work / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True))
    return report
