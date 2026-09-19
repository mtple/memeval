"""Frozen assignments and private holdouts. No endpoint replaces an assigned attempt."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from decimal import Decimal
from statistics import median

from ..domain.profiles import PROFILES, ResourceProfile
from ..evaluation.report import ENGINE_VERSION
from ..evaluation.validity import RULE_VERSION, execution_validity
from ..observations.masking import LeakScanner
from .auth import new_agent_token, token_hash
from .runs import TERMINAL, ApiError, now_iso


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def bundle(manager, bundle_id):
    row = manager.store.one("SELECT * FROM assessment_bundles WHERE bundle_id=?", (bundle_id,))
    if not row:
        raise ApiError(404, "unknown assessment bundle", "NOT_FOUND")
    return json.loads(row["manifest_json"])


def create_bundle(manager, *, label, pack_ids, resource_profile_id="controlled_v1", bankroll_raw=None):
    if not isinstance(label, str) or not 1 <= len(label) <= 80 or not label.isprintable():
        raise ApiError(400, "label must be 1-80 printable characters", "INVALID")
    if not 1 <= len(pack_ids) <= 32 or len(set(pack_ids)) != len(pack_ids):
        raise ApiError(400, "assign 1-32 distinct private episodes", "INVALID")
    if resource_profile_id not in PROFILES or PROFILES[resource_profile_id].timing != "controlled":
        raise ApiError(400, "external assessments require a controlled resource profile", "INVALID")
    packs = [manager.load_pack(ref) for ref in pack_ids]
    if len({p.pack_id for _, p in packs}) != len(packs):
        raise ApiError(400, "duplicate episode aliases are not distinct episodes", "INVALID")
    if any(r["visibility"] != "holdout" for r, _ in packs):
        raise ApiError(
            400, "assessment episodes must have been imported as private holdouts", "EXPOSURE_CONFLICT"
        )
    if (
        len(
            {
                (str(p.manifest.origin), p.manifest.numeraire_alias, p.manifest.numeraire_decimals)
                for _, p in packs
            }
        )
        != 1
    ):
        raise ApiError(400, "a bundle must share data origin and numeraire units", "INCOMPATIBLE")
    if any(LeakScanner.for_pack(p).scan({"label": label}) for _, p in packs):
        raise ApiError(400, "public bundle label must not contain private identifiers, dates or paths", "INVALID")
    for row, _pack in packs:
        if row["use_status"] not in ("demo", "research", "qualified_for_named_suite"):
            raise ApiError(400, "bundle includes an unrunnable episode", "NOT_RUNNABLE")
    bankroll = str(bankroll_raw or 10 ** packs[0][1].manifest.numeraire_decimals)
    if not bankroll.isascii() or not bankroll.isdigit() or not 0 < int(bankroll) < 2**256:
        raise ApiError(400, "bankroll_raw must be a positive integer string below 2^256", "INVALID")
    profile = PROFILES[resource_profile_id]
    bid = "bundle_" + secrets.token_hex(12)
    manifest = {
        "bundle_id": bid,
        "label": label,
        "created_at": now_iso(),
        "resource_profile": profile.model_dump(),
        "bankroll_raw": bankroll,
        "engine_version": ENGINE_VERSION,
        "eligibility_rule": RULE_VERSION,
        "isolation": "trusted_external_client",
        "origin": str(packs[0][1].manifest.origin),
        "episodes": [
            {
                "pack_id": p.pack_id,
                "parameters_hash": p.manifest.execution.parameters_hash,
                "mask_seed": secrets.token_hex(24),
                "engine_seed": secrets.token_hex(24),
            }
            for _, p in packs
        ],
    }
    manifest["commitment"] = digest(manifest)
    manager.store.execute(
        "INSERT INTO assessment_bundles (bundle_id, created_at, manifest_json) VALUES (?, ?, ?)",
        (bid, manifest["created_at"], json.dumps(manifest, sort_keys=True)),
    )
    return public_bundle(manifest)


def public_bundle(manifest):
    return {
        k: manifest[k]
        for k in (
            "bundle_id",
            "label",
            "resource_profile",
            "bankroll_raw",
            "engine_version",
            "eligibility_rule",
            "isolation",
            "origin",
            "commitment",
        )
    } | {
        "episode_count": len(manifest["episodes"]),
        "exposure_basis": "operator-imported private holdouts; outside exposure and external-client memory are unenforced",
        "predictive_validity": "not_established",
    }


def catalog(manager):
    return [
        public_bundle(json.loads(r["manifest_json"]))
        for r in manager.store.query("SELECT * FROM assessment_bundles ORDER BY created_at")
    ]


def enter(manager, *, agent_token, bundle_id, code_sha256, config):
    agent = manager.agent_by_token(agent_token)
    manifest = bundle(manager, bundle_id)
    if not isinstance(code_sha256, str) or not re.fullmatch("[0-9a-f]{64}", code_sha256):
        raise ApiError(400, "code_sha256 must be a lowercase SHA-256 digest", "INVALID")
    if not isinstance(config, dict) or len(json.dumps(config).encode()) > 16_384:
        raise ApiError(400, "config must be an object of at most 16384 bytes", "INVALID")
    profile = PROFILES.get(manifest["resource_profile"]["profile_id"])
    if (
        profile is None
        or profile.model_dump() != manifest["resource_profile"]
        or manifest["engine_version"] != ENGINE_VERSION
        or manifest["eligibility_rule"] != RULE_VERSION
    ):
        raise ApiError(409, "bundle evaluator or profile is no longer available", "PROFILE_CHANGED")
    with (
        manager.store.run_lock("identity:" + agent["agent_id"]),
        manager.store.run_lock("assessment:" + bundle_id + ":" + agent["agent_id"]),
    ):
        agent = manager.agent_by_token(agent_token)
        if manager.store.one(
            "SELECT assessment_id FROM assessments WHERE bundle_id=? AND agent_id=?",
            (bundle_id, agent["agent_id"]),
        ):
            raise ApiError(
                409,
                "this agent version already has an assignment; recover its credentials instead of retrying",
                "ATTEMPT_EXISTS",
            )
        assigned = []
        for i, episode in enumerate(manifest["episodes"]):
            try:
                row, pack = manager.load_pack(episode["pack_id"])
            except (ApiError, ValueError, OSError):
                raise ApiError(
                    409, "a frozen episode is unavailable; no assignment was created", "EPISODE_UNAVAILABLE"
                ) from None
            if (
                pack.manifest.execution.parameters_hash != episode["parameters_hash"]
                or row["visibility"] != "holdout"
            ):
                raise ApiError(409, "frozen episode has changed", "PROFILE_CHANGED")
            prior = manager.store.one(
                "SELECT SUM(attempts.count) AS count FROM attempts JOIN agents ON attempts.agent_id=agents.agent_id WHERE attempts.pack_id=? AND agents.name=?",
                (pack.pack_id, agent["name"]),
            )
            assigned.append(
                {
                    "slot": i + 1,
                    "run_id": "run_" + secrets.token_hex(8),
                    "pack_id": pack.pack_id,
                    "prior_attempts": int(prior["count"] or 0) if prior else 0,
                }
            )
        aid = "assessment_" + secrets.token_hex(12)
        commitment = {
            "code_sha256": code_sha256,
            "config_sha256": digest(config),
            "config": config,
            "agent_fingerprint": agent["fingerprint"],
            "bundle_commitment": manifest["commitment"],
            "resource_profile": manifest["resource_profile"],
            "engine_version": ENGINE_VERSION,
            "rule_version": RULE_VERSION,
            "assigned": assigned,
            "assurance": "trusted_external_client_self_attested_code",
            "created_at": now_iso(),
        }
        commitment["digest"] = digest(commitment)
        manager.store.execute(
            "INSERT INTO assessments (assessment_id, bundle_id, agent_id, created_at, state, commitment_json) VALUES (?, ?, ?, ?, ?, ?)",
            (
                aid,
                bundle_id,
                agent["agent_id"],
                commitment["created_at"],
                "assigned",
                json.dumps(commitment, sort_keys=True),
            ),
        )
        # The full assignment above is durable before any run can produce a result.
        for item in assigned:
            manager.store.execute(
                "INSERT INTO assessment_episodes (assessment_id, slot, pack_id, run_id) VALUES (?, ?, ?, ?)",
                (aid, item["slot"], item["pack_id"], item["run_id"]),
            )
        credentials = []
        try:
            for item, episode in zip(assigned, manifest["episodes"], strict=True):
                run = manager.create_run(
                    agent_id=agent["agent_id"],
                    pack_ref=item["pack_id"],
                    mode="sealed",
                    bankroll_raw=manifest["bankroll_raw"],
                    mask_seed=episode["mask_seed"],
                    engine_seed=episode["engine_seed"],
                    resource_profile_id=profile.profile_id,
                    allow_holdout=True,
                    assigned_run_id=item["run_id"],
                )
                credentials.append(
                    {
                        "slot": item["slot"],
                        "run_id": item["run_id"],
                        "session_credential": run["session_credential"],
                    }
                )
        except Exception:
            manager.store.execute(
                "UPDATE assessments SET state=? WHERE assessment_id=?", ("initialization_failed", aid)
            )
            raise ApiError(
                409,
                "assignment recorded but initialization failed; it remains an incomplete attempt",
                "ASSIGNMENT_INCOMPLETE",
            ) from None
    return {
        "assessment_id": aid,
        "commitment": commitment["digest"],
        "runs": credentials,
        "assurance": commitment["assurance"],
        "note": "All assigned attempts count. Credentials may be recovered; episodes and attempts cannot be replaced.",
    }


def get_entry(manager, assessment_id):
    row = manager.store.one("SELECT * FROM assessments WHERE assessment_id=?", (assessment_id,))
    if not row:
        raise ApiError(404, "unknown assessment", "NOT_FOUND")
    return row, json.loads(row["commitment_json"])


def recover(manager, assessment_id, agent_token):
    row, committed = get_entry(manager, assessment_id)
    if manager.agent_by_token(agent_token)["agent_id"] != row["agent_id"]:
        raise ApiError(404, "unknown assessment", "NOT_FOUND")
    out = []
    for item in committed["assigned"]:
        with manager.store.run_lock(item["run_id"]):
            run = manager.store.run(item["run_id"])
            if not run or run["state"] in TERMINAL or run["report_json"]:
                continue
            token = new_agent_token()
            manager.store.update_run(run["run_id"], token_hash=token_hash(token))
            if run["run_id"] in manager._contexts:
                manager._contexts[run["run_id"]].token_hash = token_hash(token)
            out.append(
                {
                    "slot": item["slot"],
                    "run_id": item["run_id"],
                    "session_credential": {
                        "token": token,
                        "commands_url": manager.gateway_url + "/agent/v1/commands",
                        "mcp_url": manager.gateway_url + "/agent/mcp",
                    },
                }
            )
    return {"assessment_id": assessment_id, "runs": out}


def result(manager, assessment_id):
    row, committed = get_entry(manager, assessment_id)
    manifest = bundle(manager, row["bundle_id"])
    episodes, returns = [], []
    for item, frozen in zip(committed["assigned"], manifest["episodes"], strict=True):
        run = manager.store.run(item["run_id"])
        report = json.loads(run["report_json"]) if run and run["report_json"] else {}
        validity = execution_validity(report)
        expected = frozen["parameters_hash"]
        if manifest["resource_profile"]["profile_id"] != "pack_defaults_v1":
            expected = hashlib.sha256(
                (
                    expected + ResourceProfile.model_validate(manifest["resource_profile"]).fingerprint()
                ).encode()
            ).hexdigest()
        reasons = list(validity["failed_gates"])
        if not run or run["profile_hash"] != expected:
            reasons.append("frozen_execution_profile")
        if item["prior_attempts"]:
            reasons.append("prior_service_exposure")
        if committed["engine_version"] != ENGINE_VERSION or committed["rule_version"] != RULE_VERSION:
            reasons.append("frozen_evaluator_version")
        finished = bool(run and (run["state"] in TERMINAL or run["report_json"]))
        eligible = finished and not reasons
        value = report.get("outcome", {}).get("headline_return")
        episodes.append(
            {
                "slot": item["slot"],
                "state": run["state"] if run else "initialization_failed",
                "finished": finished,
                "eligible": eligible,
                "failed_gates": reasons,
                "headline_return": value,
                "status_dimensions": report.get("status_dimensions", {}),
                "prior_attempts": item["prior_attempts"],
            }
        )
        if eligible and value is not None:
            returns.append(Decimal(value))
    complete = all(e["finished"] for e in episodes)
    eligible = complete and len(returns) == len(episodes) and row["state"] != "initialization_failed"
    if not complete:
        for episode in episodes:
            episode["headline_return"] = None
    agent = manager.store.agent(row["agent_id"])
    return {
        "assessment_id": assessment_id,
        "bundle_id": row["bundle_id"],
        "agent_id": row["agent_id"],
        "agent_name": agent["name"] if agent else row["agent_id"],
        "agent_version": agent["version"] if agent else None,
        "commitment": {
            k: committed[k]
            for k in (
                "digest",
                "code_sha256",
                "config_sha256",
                "engine_version",
                "rule_version",
                "assurance",
            )
        },
        "coverage": {"assigned": len(episodes), "finished": sum(e["finished"] for e in episodes)},
        "complete": complete,
        "eligible": eligible,
        "episodes": episodes,
        "median_return": str(median(returns)) if eligible else None,
        "cash_reference_return": "0",
        "predictive_validity": "not_established",
        "note": "Every assigned episode counts. Results are withheld until every attempt ends. Eligibility applies only to this frozen bundle and declared assurance group; external code and memory controls are not enforced.",
    }


def leaderboard(manager, bundle_id):
    manifest = public_bundle(bundle(manager, bundle_id))
    attempts = [
        result(manager, r["assessment_id"])
        for r in manager.store.query(
            "SELECT assessment_id FROM assessments WHERE bundle_id=? ORDER BY created_at", (bundle_id,)
        )
    ]
    ranked = sorted(
        (a for a in attempts if a["eligible"]),
        key=lambda a: (-Decimal(a["median_return"]), a["assessment_id"]),
    )
    return {
        "bundle": manifest,
        "rows": [{"rank": i + 1, **a} for i, a in enumerate(ranked)],
        "attempts": attempts,
    }


def abort(manager, assessment_id, agent_token):
    row, committed = get_entry(manager, assessment_id)
    if manager.agent_by_token(agent_token)["agent_id"] != row["agent_id"]:
        raise ApiError(404, "unknown assessment", "NOT_FOUND")
    for item in committed["assigned"]:
        run = manager.store.run(item["run_id"])
        if run and run["state"] not in TERMINAL and not run["report_json"]:
            manager.abort(item["run_id"])
    return result(manager, assessment_id)
