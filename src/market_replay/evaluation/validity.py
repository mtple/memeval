"""Named inclusion rules. These gates are not a score or a claim about trading skill."""

RULE_VERSION = "execution_eligibility_v1"


def execution_validity(report: dict) -> dict:
    outcome = report.get("outcome", {})
    unresolved = report.get("unresolved", {})
    assumptions = report.get("coverage_and_assumptions", {})
    activity = report.get("activity", {})
    gates = [
        ("run_completed", report.get("run", {}).get("state") == "completed"),
        (
            "settled_cash_objective",
            outcome.get("primary_metric") == "final_cash_return_v1"
            and outcome.get("headline_return") is not None,
        ),
        (
            "no_fidelity_failures",
            not unresolved.get("environment_fidelity_flags")
            and not assumptions.get("reconciliation_mismatches_in_run", 0),
        ),
        ("within_request_and_decision_budgets", not activity.get("budget_exhausted", False)),
        (
            "pack_runnable",
            report.get("status_dimensions", {}).get("use_status")
            in ("demo", "research", "qualified_for_named_suite"),
        ),
    ]
    return {
        "rule_version": RULE_VERSION,
        "eligible": all(passed for _, passed in gates),
        "gates": [{"gate": name, "passed": passed} for name, passed in gates],
        "failed_gates": [name for name, passed in gates if not passed],
        "capacity_policy": "Rejected requests and orders stay within capacity and are counted, not penalized. A fidelity failure excludes the result.",
        "capacity_rejections": activity.get("model_capacity_rejected", 0)
        + activity.get("quality_exposure", {}).get("errors_by_code", {}).get("MODEL_CAPACITY_LIMIT", 0),
        "valuation_complete": outcome.get("valuation_complete"),
        "token_behavior": report.get("status_dimensions", {}).get("token_behavior", "unknown"),
        "scope": "Execution eligibility under the declared model; not evidence of predictive trading skill.",
    }


def ranking_eligible(report: dict) -> bool:
    return execution_validity(report)["eligible"]
