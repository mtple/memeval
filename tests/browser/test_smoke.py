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
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        # 1. Anyone: the leaderboard, the sign-up link, no sign-in
        page.goto(f"{srv.url}/")
        page.wait_for_selector("#board-h", timeout=20_000)
        page.wait_for_selector("table.board >> text=cash_only_python", timeout=20_000)
        assert page.inner_text("#join-link") == f"{srv.url}/join"
        assert "Play Market Replay again" in page.inner_text("#play-again")  # the returning case is on the page too
        assert page.locator("#signin-btn").count() == 1
        assert page.inner_text("#board-h") == "Leaderboard"
        assert page.get_by_role("link", name="Assessments", exact=True).count() == 0
        body = page.inner_text("body")
        assert "practice" not in body.lower() and "assessment" not in body.lower()
        assert "not an edge" in body  # the honesty footnote travels with the board
        page.screenshot(path=str(OUT / "home.png"), full_page=True)

        # 2. Anyone: the join link is instructions an agent can act on, nothing to fill in
        page.goto(f"{srv.url}/join")
        skill = page.inner_text("body")
        assert "/api/v1/enroll" in skill and "session.finish" in skill
        assert page.locator("#new-agent-name").count() == 0  # no manual run form anywhere
        page.goto(f"{srv.url}/results")
        page.wait_for_selector("text=cash_only_python", timeout=20_000)
        page.get_by_role("heading", name="Runs", exact=True).wait_for()
        page.goto(f"{srv.url}/episodes")
        page.get_by_text("Generated data", exact=True).wait_for()
        assert "practice" not in page.inner_text("body").lower()
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
        assert page.get_by_label("Visibility", exact=True).count() == 0
        page.screenshot(path=str(OUT / "episodes.png"))

        page.goto(f"{srv.url}/runs/{run_id}")
        page.wait_for_selector("text=completed", timeout=20_000)
        page.screenshot(path=str(OUT / "run.png"))
        page.goto(f"{srv.url}/runs/{run_id}/results")
        page.wait_for_selector("text=How results work", timeout=20_000)
        page.wait_for_selector("text=The trades", timeout=20_000)
        page.wait_for_selector("text=never placed an order", timeout=20_000)
        body = page.inner_text("body")
        assert "not live trading" in body and "predicts live results" in body
        assert "cpmm_fixed_flow_v1" not in body.split("All the details")[0]  # no raw tokens above the fold
        body = page.inner_text("body")
        assert "score" not in body.lower().replace("scored", "") or "0-100" not in body
        page.wait_for_selector("text=Run validity", timeout=20_000)
        page.click("text=Load decision timeline")
        page.wait_for_selector(".decision-timeline > li", timeout=20_000)
        assert "session describe" in page.inner_text(".decision-timeline").lower()
        page.screenshot(path=str(OUT / "results.png"))
        page.goto(f"{srv.url}/runs")
        page.get_by_role("heading", name="All runs", exact=True).wait_for()
        page.get_by_role("cell", name="Revealable trusted_external_client", exact=True).wait_for()
        assert "practice" not in page.inner_text("body").lower()
        page.screenshot(path=str(OUT / "runs.png"))
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
        assert not errors, errors
        browser.close()
