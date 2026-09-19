"""Seam: a run must survive the process that created it (serverless instances come and go)."""

from __future__ import annotations

from pathlib import Path

import pytest

from market_replay.service.runs import ApiError, RunManager

SCRIPT = [
    ("session.describe", {}),
    ("markets.list", {"limit": 5}),
    ("clock.advance", {"to_ms": 600_000}),
    ("session.snapshot", {"since_ms": 0, "limit": 2}),
    ("clock.wait", {"until_ms": 1_800_000, "conditions": [{"kind": "new_pool"}]}),
    ("portfolio.get", {}),
    ("clock.advance", {"to_ms": 10**9}),
    ("session.finish", {}),
]


def make_manager(store_url: str, tmp: Path, name: str) -> RunManager:
    return RunManager(data_dir=tmp / name, store_url=store_url)


def register_and_run(mgr: RunManager, dev_pack_dir: Path):
    mgr.import_pack(dev_pack_dir, "gen_dev_short")
    aid = mgr.register_agent(name="ext", version="1", runtime="python", capabilities=[], config={})["agent_id"]
    run = mgr.create_run(agent_id=aid, pack_ref="gen_dev_short", mask_seed="m", engine_seed="e")
    return run["run_id"], run["session_credential"]["token"]


def test_session_continues_in_a_fresh_process(store_url, tmp_path, dev_pack_dir):
    a = make_manager(store_url, tmp_path, "a")
    run_id, tok = register_and_run(a, dev_pack_dir)
    for tool, args in SCRIPT[:3]:
        assert a.handle_command(tok, "r", tool, args).status == "ok"
    a.close()  # the first instance disappears
    b = make_manager(store_url, tmp_path, "b")  # a new instance, empty memory
    for tool, args in SCRIPT[3:]:
        env = b.handle_command(tok, "r", tool, args)
        assert env.status == "ok", env.error
    assert b.run_view(run_id)["state"] == "completed"
    report_b = b.report(run_id)
    # A single-process run of the same script produces the identical ledger and state.
    c = make_manager(store_url, tmp_path, "c")
    aid = c.store.agent_by_name_version("ext", "1")["agent_id"]
    run2 = c.create_run(agent_id=aid, pack_ref="gen_dev_short", mask_seed="m", engine_seed="e")
    for tool, args in SCRIPT:
        c.handle_command(run2["session_credential"]["token"], "r", tool, args)
    report_c = c.report(run2["run_id"])
    assert report_b["reproducibility"]["ledger_hash"] == report_c["reproducibility"]["ledger_hash"]
    assert report_b["reproducibility"]["state_hash"] == report_c["reproducibility"]["state_hash"]
    assert b.replay(run_id)["ledger_matches"]
    b.close()
    c.close()


def test_two_instances_alternating_commands_stay_consistent(store_url, tmp_path, dev_pack_dir):
    a = make_manager(store_url, tmp_path, "a")
    run_id, tok = register_and_run(a, dev_pack_dir)
    b = make_manager(store_url, tmp_path, "b")
    managers = [a, b]
    for i, (tool, args) in enumerate(SCRIPT):
        env = managers[i % 2].handle_command(tok, f"r{i}", tool, args)
        assert env.status == "ok", env.error
    assert a.store.trace_len(run_id) == len(SCRIPT)
    assert a.run_view(run_id)["state"] == "completed"
    assert a.replay(run_id)["ledger_matches"]
    a.close()
    b.close()


def test_pack_regenerated_when_path_is_gone(store_url, tmp_path, dev_pack_dir):
    a = make_manager(store_url, tmp_path, "a")
    row = a.import_pack(dev_pack_dir, "gen_dev_short")
    a.close()
    b = make_manager(store_url, tmp_path, "b")
    b.store.execute("UPDATE packs SET path=? WHERE pack_id=?", ("/nonexistent/path/gen_dev_short", row["pack_id"]))
    _row, pack = b.load_pack(row["pack_id"])
    assert pack.pack_id == row["pack_id"]  # deterministic generator reproduces the identical pack
    b.close()


def test_usage_cap_refuses_new_runs(store_url, tmp_path, dev_pack_dir):
    a = RunManager(data_dir=tmp_path / "a", store_url=store_url, max_runs_per_day=1)
    run_id, tok = register_and_run(a, dev_pack_dir)
    aid = a.store.agent_by_name_version("ext", "1")["agent_id"]
    with pytest.raises(ApiError) as e:
        a.create_run(agent_id=aid, pack_ref="gen_dev_short")
    assert e.value.status == 429 and "cap" in e.value.message
    usage = a.usage_view()
    assert usage["today"]["runs"] == 1 and usage["caps"]["max_runs_per_day"] == 1
    a.close()


def test_reference_agent_runs_in_process(store_url, tmp_path, dev_pack_dir):
    a = make_manager(store_url, tmp_path, "a")
    a.import_pack(dev_pack_dir, "gen_dev_short")
    aid = a.register_agent(name="basket", version="1", runtime="python", capabilities=[], config={})["agent_id"]
    run = a.create_run(agent_id=aid, pack_ref="gen_dev_short", launch_spec={"kind": "example", "name": "scheduled_basket", "runtime": "python"}, execute="inprocess")
    view = a.run_view(run["run_id"])
    assert view["state"] == "completed", view["error"]
    rep = a.report(run["run_id"])
    assert rep["activity"]["confirmed_fills"] > 0
    assert a.usage_today()["cpu_seconds"] > 0
    assert a.agent_log(run["run_id"]).strip().endswith("valuation_complete True")
    # the whole run survives a fresh instance: report, trace and export read from the store
    b = make_manager(store_url, tmp_path, "b")
    assert b.export(run["run_id"], "participant")["report"]["activity"]["confirmed_fills"] == rep["activity"]["confirmed_fills"]
    a.close()
    b.close()
