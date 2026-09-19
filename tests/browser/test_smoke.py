"""Browser smoke test: the built web UI loads for anyone, the join link serves the agent instructions,
the operator can sign in, and the results screens keep their honesty statements.

Requires apps/web/dist (``make build``) and the preinstalled Chromium. Marked ``browser``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from market_replay.service.app import WEB_DIST
from market_replay.service.embedded import EmbeddedServer
from market_replay.service.runs import RunManager

pytestmark = pytest.mark.browser

OUT = Path(__file__).parent / "output"


@pytest.fixture(scope="module")
def ui_server(tmp_path_factory, dev_pack_dir):
    if not (WEB_DIST / "index.html").exists():
        pytest.skip("web UI not built (run `make build`)")
    import market_replay.service.runs as runs_mod

    data = tmp_path_factory.mktemp("uidata")
    saved, runs_mod.WEEKS_DIR = runs_mod.WEEKS_DIR, tmp_path_factory.mktemp("no_weeks")  # the recorded days in the checkout are not fixtures
    mgr = RunManager(data_dir=data)
    srv = EmbeddedServer(mgr, "adm_ui_token").start()
    admin = srv.admin()
    admin.post("/api/v1/packs/import", json={"path": str(dev_pack_dir), "name": "gen_dev_short"}).raise_for_status()
    aid = admin.post("/api/v1/agents", json={"name": "cash_only_python", "version": "1", "runtime": "python"}).json()["agent_id"]
    run = admin.post("/api/v1/runs", json={"agent_id": aid, "pack_id": "gen_dev_short", "launch": {"name": "cash_only", "runtime": "python"}}).json()
    mgr.wait_for_run(run["run_id"], 120)
    yield srv, run["run_id"]
    srv.stop()
    runs_mod.WEEKS_DIR = saved


def _chromium_path() -> str | None:
    if explicit := os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE"):
        return explicit
    root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers"))
    for cand in sorted(root.glob("chromium*/chrome-linux*/chrome")) + sorted(root.glob("chromium*/chrome-linux*/headless_shell")) + sorted(root.glob("chromium_headless_shell*/chrome-linux*/headless_shell")):
        if cand.exists():
            return str(cand)
    return None


def test_ui_smoke(ui_server):
    pw = pytest.importorskip("playwright.sync_api")
    srv, run_id = ui_server
    OUT.mkdir(exist_ok=True)
    exe = _chromium_path()
    with pw.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
        page = browser.new_page(viewport={"width": 1200, "height": 900})

        # 1. Anyone: the leaderboard, the sign-up link, no sign-in
        page.goto(f"{srv.url}/")
        page.wait_for_selector("#board-h", timeout=20_000)
        page.wait_for_selector("table.board >> text=cash_only_python", timeout=20_000)
        assert page.inner_text("#join-link") == f"{srv.url}/join"
        assert "Play Market Replay again" in page.inner_text("#play-again")  # the returning case is on the page too
        assert page.locator("#signin-btn").count() == 1
        body = page.inner_text("body")
        assert "not an edge" in body  # the honesty footnote travels with the board
        page.screenshot(path=str(OUT / "home.png"), full_page=True)

        # 2. Anyone: the join link is instructions an agent can act on, nothing to fill in
        page.goto(f"{srv.url}/join")
        skill = page.inner_text("body")
        assert "/api/v1/enroll" in skill and "session.finish" in skill
        assert page.locator("#new-agent-name").count() == 0  # no manual run form anywhere
        page.goto(f"{srv.url}/results")
        page.wait_for_selector("text=cash_only_python", timeout=20_000)
        # ?agent= from the enroll response marks the agent as mine on the board
        page.goto(f"{srv.url}/?agent=cash_only_python")
        page.wait_for_selector("tr.mine >> text=you", timeout=20_000)

        # 3. Operator: sign in through the dialog (no token in the URL)
        page.click("#signin-btn")
        page.fill("#admin-token", "adm_ui_token")
        page.keyboard.press("Enter")
        page.wait_for_selector("text=Operator", timeout=20_000)
        page.goto(f"{srv.url}/episodes")
        page.wait_for_selector("text=gen_dev_short", timeout=20_000)
        page.wait_for_selector("text=Import pack", timeout=20_000)  # operator-only form is visible now
        page.screenshot(path=str(OUT / "episodes.png"))

        page.goto(f"{srv.url}/runs/{run_id}")
        page.wait_for_selector("text=completed", timeout=20_000)
        page.screenshot(path=str(OUT / "run.png"))
        page.goto(f"{srv.url}/runs/{run_id}/results")
        page.wait_for_selector("text=How much to trust this", timeout=20_000)
        page.wait_for_selector("text=never placed an order", timeout=20_000)
        body = page.inner_text("body")
        assert "does not establish an edge" in body and "predictive validity is not established" in body
        assert "cpmm_fixed_flow_v1" not in body.split("All the details")[0]  # no raw tokens above the fold
        body = page.inner_text("body")
        assert "score" not in body.lower().replace("scored", "") or "0-100" not in body
        page.wait_for_selector("text=Evaluation validity", timeout=20_000)
        page.click("text=Load decision timeline")
        page.wait_for_selector(".decision-timeline > li", timeout=20_000)
        assert "session describe" in page.inner_text(".decision-timeline").lower()
        page.screenshot(path=str(OUT / "results.png"))
        page.goto(f"{srv.url}/assessments")
        page.wait_for_selector("text=No assessment bundles yet", timeout=20_000)
        assert "Every assigned attempt counts" in page.inner_text("body")
        page.screenshot(path=str(OUT / "assessments.png"))
        page.goto(f"{srv.url}/data-health")
        page.wait_for_selector("text=Data health", timeout=20_000)
        page.screenshot(path=str(OUT / "data_health.png"))
        # clicking an agent on the board opens its history in plain words
        page.goto(f"{srv.url}/")
        page.click("table.board >> text=cash_only_python")
        page.wait_for_selector("text=What this agent has done here", timeout=20_000)
        page.wait_for_selector("text=Ranked result", timeout=20_000)
        body = page.inner_text("body")
        assert "counts on the board" in body and "Full result" in body
        page.screenshot(path=str(OUT / "agent_history.png"))

        # 4. Mobile width renders without horizontal overflow of the main content
        page.set_viewport_size({"width": 360, "height": 800})
        page.goto(f"{srv.url}/")
        page.wait_for_selector("table.board >> text=cash_only_python", timeout=20_000)
        width = page.evaluate("document.documentElement.scrollWidth")
        assert width <= 380
        page.screenshot(path=str(OUT / "home_mobile.png"))
        browser.close()


def test_assessment_ui_workflow(ui_server, tmp_path):
    from dataclasses import replace

    from market_replay.datasets.generator import dev_short_config, generate_pack
    from market_replay.service.assessments import catalog, enter

    pw = pytest.importorskip("playwright.sync_api")
    srv, _ = ui_server
    pack_path = tmp_path / "private_ui"
    generate_pack(replace(dev_short_config(), name="private_ui", seed="ui-private-only", duration_ms=60_000, prehistory_ms=20_000, n_pools=2), pack_path)
    private = srv.manager.import_pack(pack_path, "Private UI episode", visibility="holdout")
    exe = _chromium_path()
    with pw.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
        page = browser.new_page(viewport={"width": 1200, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(f"{srv.url}/assessments?token=adm_ui_token")
        page.get_by_text("Create a frozen bundle", exact=True).click()
        page.get_by_label("Public bundle label").fill("UI private bundle")
        page.get_by_label("Private UI episode", exact=True).check()
        page.get_by_role("button", name="Freeze bundle", exact=True).click()
        page.get_by_role("heading", name="UI private bundle", exact=True).wait_for()
        bundle = next(b for b in catalog(srv.manager) if b["label"] == "UI private bundle")
        identity = srv.manager.enroll(agent={"name": "UI candidate", "version": "1", "runtime": "external"})
        assessment = enter(srv.manager, agent_token=identity["agent_token"], bundle_id=bundle["bundle_id"], code_sha256="e" * 64, config={})
        for run in assessment["runs"]:
            assert srv.manager.handle_command(run["session_credential"]["token"], "finish", "session.finish", {}).status == "ok"
        # A fresh public browser sees only bundle/slot data, never the private pack.
        public = browser.new_page(viewport={"width": 1200, "height": 900})
        public.on("pageerror", lambda error: errors.append(str(error)))
        public.goto(f"{srv.url}/assessments")
        public.get_by_role("button", name="View all attempts", exact=True).click()
        public.get_by_role("link", name="UI candidate v1", exact=True).click()
        public.get_by_text("Eligible within this bundle", exact=True).wait_for()
        assert "1/1" in public.inner_text("body")
        assert "assigned episode 1" in public.inner_text("body").lower()
        assert "Private UI episode" not in public.content()
        assert private["pack_id"] not in public.content()
        assert not errors, errors
        public.screenshot(path=str(OUT / "assessment_result.png"), full_page=True)
        public.set_viewport_size({"width": 360, "height": 800})
        assert public.evaluate("document.documentElement.scrollWidth") <= 380
        browser.close()
