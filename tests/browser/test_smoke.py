"""Browser smoke test: the built web UI loads for anyone, a stranger can start a run and get a token,
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
    data = tmp_path_factory.mktemp("uidata")
    mgr = RunManager(data_dir=data)
    srv = EmbeddedServer(mgr, "adm_ui_token").start()
    admin = srv.admin()
    admin.post("/api/v1/packs/import", json={"path": str(dev_pack_dir), "name": "gen_dev_short"}).raise_for_status()
    aid = admin.post("/api/v1/agents", json={"name": "cash_only_python", "version": "1", "runtime": "python"}).json()["agent_id"]
    run = admin.post("/api/v1/runs", json={"agent_id": aid, "pack_id": "gen_dev_short", "launch": {"name": "cash_only", "runtime": "python"}}).json()
    mgr.wait_for_run(run["run_id"], 120)
    yield srv, run["run_id"]
    srv.stop()


def _chromium_path() -> str | None:
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
        page.wait_for_selector("text=Leaderboard", timeout=20_000)
        page.wait_for_selector("table.board >> text=cash_only_python", timeout=20_000)
        assert page.inner_text("#join-link") == f"{srv.url}/join"
        assert page.locator("#signin-btn").count() == 1
        body = page.inner_text("body")
        assert "not an edge" in body  # the honesty footnote travels with the board
        page.screenshot(path=str(OUT / "home.png"), full_page=True)

        # 2. Anyone: start a run for their own agent and receive a one-time token
        page.goto(f"{srv.url}/new")
        page.wait_for_selector("#new-agent-name", timeout=20_000)
        page.fill("#new-agent-name", "smoke-bot")
        page.click("#new-submit")
        page.wait_for_selector("#session-token", timeout=20_000)
        token = page.inner_text("#session-token")
        assert token.startswith("agt_")
        body = page.inner_text("body")
        assert "/agent/mcp" in body and "/agent/v1/commands" in body
        page.screenshot(path=str(OUT / "new_run_token.png"))
        page.goto(f"{srv.url}/results")
        page.wait_for_selector("text=smoke-bot", timeout=20_000)
        assert "waiting for the agent to connect" in page.inner_text("body")
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
        page.screenshot(path=str(OUT / "results.png"))
        page.goto(f"{srv.url}/data-health")
        page.wait_for_selector("text=Data health", timeout=20_000)
        page.screenshot(path=str(OUT / "data_health.png"))

        # 4. Mobile width renders without horizontal overflow of the main content
        page.set_viewport_size({"width": 360, "height": 800})
        page.goto(f"{srv.url}/")
        page.wait_for_selector("table.board >> text=cash_only_python", timeout=20_000)
        width = page.evaluate("document.documentElement.scrollWidth")
        assert width <= 380
        page.screenshot(path=str(OUT / "home_mobile.png"))
        browser.close()
