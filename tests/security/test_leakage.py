"""Acceptance 31, 32: leakage scanning of agent-visible payloads and restricted-runner controls."""

from __future__ import annotations

import json
import os
from pathlib import Path

from market_replay.datasets.pack import Pack
from market_replay.engine.session import Session
from market_replay.observations.masking import LeakScanner
from market_replay.runners.restricted import scrub_env


def exercise(s: Session) -> list[dict]:
    outs = []
    items = s.handle("r", "markets.list", {}).data["items"]
    pid = items[0]["pool_id"]
    base = items[0]["base_asset"]
    outs.append(s.handle("r", "session.describe", {}))
    outs.append(s.handle("r", "clock.advance", {"to_ms": 1_200_000}))
    outs.append(s.handle("r", "session.snapshot", {"since_ms": 0, "limit": 2}))
    outs.append(s.handle("r", "clock.wait", {"until_ms": 1_201_000, "conditions": [{"kind": "new_pool"}]}))
    for tool, args in (("markets.get", {}), ("market.trades", {"limit": 50}), ("market.candles", {"interval_ms": 60_000}), ("market.liquidity", {}), ("market.restrictions", {})):
        outs.append(s.handle("r", tool, {"pool_id": pid, **args}))
    outs.append(s.handle("r", "broker.quote", {"pool_id": pid, "asset_in": "CASH", "amount_in_raw": "10000"}))
    outs.append(s.handle("r", "broker.submit", {"pool_id": pid, "asset_in": "CASH", "asset_out": base, "amount_in_raw": "10000", "min_amount_out_raw": "0", "deadline_ms": 2_000_000, "idempotency_key": "k"}))
    outs.append(s.handle("r", "clock.advance", {"to_ms": 1_300_000}))
    outs.append(s.handle("r", "broker.order", {}))
    outs.append(s.handle("r", "portfolio.get", {}))
    outs.append(s.handle("r", "portfolio.history", {"limit": 50}))
    outs.append(s.handle("r", "markets.get", {"pool_id": "0:fixture_cpmm:pool_00"}))
    outs.append(s.handle("r", "markets.get", {"pool_id": "0x" + "a" * 40}))
    outs.append(s.handle("r", "broker.quote", {"pool_id": pid, "asset_in": "0:cash", "amount_in_raw": "1"}))
    outs.append(s.handle("r", "clock.advance", {"to_ms": 10**9}))
    outs.append(s.handle("r", "session.finish", {}))
    return [e.model_dump(mode="json") for e in outs]


def test_agent_visible_payloads_contain_no_private_identities_or_dates(dev_pack: Pack):
    s = Session.create(session_id="ses_leak", pack=dev_pack, bankroll_raw=1_000_000, mask_seed="m", engine_seed="e")
    payloads = exercise(s)
    scanner = LeakScanner.for_pack(dev_pack)
    text = json.dumps(payloads)
    for a in dev_pack.assets.values():
        if not a.is_numeraire:
            assert a.key not in text and a.address not in text
    for p in dev_pack.pools.values():
        assert p.key not in text and p.address not in text
    assert dev_pack.manifest.pack_id not in text
    assert str(dev_pack.path) not in text
    assert dev_pack.manifest.period.start_utc[:10] not in text
    assert str(dev_pack.manifest.period.start_utc_ms) not in text
    assert "fixture_tx_" not in text and "fixture_wallet_" not in text
    for p in payloads:
        assert not scanner.scan(p), scanner.scan(p)[:3]


def test_scanner_catches_injected_leaks(dev_pack: Pack):
    scanner = LeakScanner.for_pack(dev_pack)
    pool_key = next(iter(dev_pack.pools))
    assert scanner.scan({"x": pool_key})
    assert scanner.scan({"x": "seen on 2026-01-05"})
    assert scanner.scan({"x": "0x" + "b" * 40})
    assert scanner.scan({"time_utc_ms": dev_pack.manifest.period.start_utc_ms})
    assert scanner.scan({"path": "/home/user/memeval/data/packs/x"})
    assert not scanner.scan({"amount_in_raw": "1700000000000"})  # large quantities are not timestamps
    assert not scanner.scan({"pool_id": "pool_abcdefgh", "time_ms": 123456})


def test_participant_report_and_export_redacted(dev_pack: Pack, tmp_path: Path):
    from market_replay.evaluation.report import build_report
    from market_replay.observations.masking import redact_for_role

    s = Session.create(session_id="ses_r", pack=dev_pack, bankroll_raw=1_000_000, mask_seed="m", engine_seed="e")
    exercise(s)
    meta = {"run_id": "run_x", "pack_id": dev_pack.manifest.pack_id, "mask_seed": "m", "engine_seed": "e", "pack_path": str(dev_pack.path), "isolation": "trusted_external_client"}
    admin_report = build_report(s, run_meta=meta, role="admin")
    part_report = build_report(s, run_meta=meta, role="participant")
    assert admin_report["versions"]["pack_id"] == dev_pack.manifest.pack_id
    assert part_report["versions"]["pack_id"] == "hidden"
    assert "mask_seed" not in part_report["run"] and "pack_path" not in part_report["run"]
    scanner = LeakScanner.for_pack(dev_pack)
    assert not scanner.scan(redact_for_role(part_report, "participant", scanner))


def test_restricted_runner_env_scrub():
    base = dict(os.environ)
    base.update({"ANTHROPIC_API_KEY": "sk-secret", "AWS_SECRET_ACCESS_KEY": "x", "MARKET_REPLAY_PACK_PATH": "/data/packs/week", "EPISODE_START_DATE": "2026-01-05", "WALLET_PRIVATE_KEY": "0xdead", "PATH": "/usr/bin", "HOME": "/root", "SOME_TOKEN": "t"})
    env = scrub_env(base, gateway_url="http://127.0.0.1:1/", token="agt_x", agent_seed="7")
    assert "ANTHROPIC_API_KEY" not in env and "AWS_SECRET_ACCESS_KEY" not in env and "WALLET_PRIVATE_KEY" not in env
    assert "MARKET_REPLAY_PACK_PATH" not in env and "EPISODE_START_DATE" not in env and "SOME_TOKEN" not in env
    assert env["MARKET_REPLAY_TOKEN"] == "agt_x" and env["MARKET_REPLAY_URL"] == "http://127.0.0.1:1/"
    assert env["HOME"] != "/root"
    assert set(env) <= {"PATH", "LANG", "LC_ALL", "HOME", "TMPDIR", "PYTHONUNBUFFERED", "PYTHONPATH", "NODE_OPTIONS", "PLAYWRIGHT_BROWSERS_PATH", "MARKET_REPLAY_URL", "MARKET_REPLAY_TOKEN", "MARKET_REPLAY_AGENT_SEED"}


def test_restricted_runner_reports_unenforced_controls_honestly(dev_pack: Pack, tmp_path: Path):
    from market_replay.runners.restricted import launch_restricted
    from market_replay.runners.trusted import LaunchSpec

    # Launch a trivial python example against a dead gateway just to inspect controls; kill quickly.
    proc, controls, workdir = launch_restricted(LaunchSpec("cash_only", "python", []), gateway_url="http://127.0.0.1:9/", token="agt_x", agent_seed=None, log_path=tmp_path / "log.txt")
    try:
        d = controls.as_dict()
        assert d["env_scrub"] == "enforced" and d["no_pack_mount"] == "enforced" and d["no_secrets"] == "enforced"
        assert d["network_namespace"] == "unenforced"
        assert any("not restricted" in n for n in d["notes"])
        assert workdir.exists() and not any(workdir.iterdir())
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_participant_redaction_keeps_the_document_valid_for_numeric_findings():
    """A real pack's report carries the recording time as a bare epoch number; redacting it must not
    leave unquoted text in the JSON (the bug FreeTurtle hit: every participant report answered 500)."""
    import json

    from market_replay.observations.masking import LeakScanner, redact_for_role

    scanner = LeakScanner(private_terms=set(), allow_dates=False)
    report = {"coverage_and_assumptions": {"availability_model": {"acquisition_utc_ms": 1789739082056, "note": "x"}}, "list": [{"created_at": "1789739082056"}], "addr": "sent to 0x4200000000000000000000000000000000000006 today", "outcome": {"final_cash_raw": "1006772206301670659"}}
    out = redact_for_role(report, "participant", scanner)
    json.dumps(out)  # serializable, hence valid
    assert out["coverage_and_assumptions"]["availability_model"]["acquisition_utc_ms"] == "[redacted]"
    assert out["list"][0]["created_at"] == "[redacted]"
    assert out["addr"] == "sent to [redacted] today"
    assert out["outcome"]["final_cash_raw"] == "1006772206301670659"  # a raw quantity is not a timestamp
    assert not scanner.scan(out)
    assert redact_for_role(report, "admin", scanner) is report
