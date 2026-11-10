"""Browser tests for the web UI (Playwright + Chromium). Skipped when Playwright is not installed."""
import socket
import threading
import time

import pytest
import uvicorn

from mqresolver import server

pw = pytest.importorskip("playwright.sync_api")
pytestmark = pytest.mark.ui


@pytest.fixture(scope="module")
def url():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/"
    srv.should_exit = True
    t.join(5)


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Chromium is not available: {exc}")
        yield b
        b.close()


@pytest.fixture()
def page(browser, url):
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    pg = ctx.new_page()
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    pg.on("console", lambda m: pg.errors.append(m.text) if m.type == "error" else None)
    pg.goto(url)
    pg.wait_for_load_state("networkidle")
    yield pg
    ctx.close()


def run_sample(pg):
    pg.click("#empty-sample")
    pg.wait_for_selector("#results:not([hidden])", timeout=60000)
    pg.wait_for_function("() => window.__mqr && window.__mqr.views.as_is.perf.frames > 0")


def test_sample_run_end_to_end(page):
    run_sample(page)
    head = page.inner_text("#headline")
    assert "queue managers become" in head and "All checks passed" in head
    perf = page.evaluate("window.__mqr.perf()")
    assert perf["as_is"]["max"] < 100 and perf["target"]["max"] < 100
    n = page.evaluate("window.__mqr.views.as_is.nodes.length")
    assert n > 100
    assert page.errors == []


def test_select_queue_manager_shows_where_apps_go(page):
    run_sample(page)
    box = page.locator("#cv-as-is").bounding_box()
    x, y, qm = page.evaluate("() => { const v = window.__mqr.views.as_is; const i = v.order[0]; return [v.sx[i], v.sy[i], v.nodes[i].id]; }")
    page.mouse.click(box["x"] + x, box["y"] + y)
    page.wait_for_timeout(200)
    detail = page.inner_text("#detail")
    assert qm in detail and "Its applications go to" in detail
    lit = page.evaluate("window.__mqr.views.target.hl.nodes.size")
    assert lit >= 1                                       # target QMs receiving those apps light up


def test_find_app_and_applications_view(page):
    run_sample(page)
    app = page.evaluate("window.__mqr.views.target.nodes.length && document.querySelector('#tab-apps td.id').textContent")
    page.fill("#find-q", app)
    page.press("#find-q", "Enter")
    page.wait_for_timeout(200)
    assert app in page.inner_text("#detail")
    page.click("#view-mode button[data-v=app]")
    page.wait_for_timeout(300)
    kinds = page.evaluate("[...new Set(window.__mqr.views.target.nodes.map(n => n.kind))].sort()")
    assert kinds == ["app", "qm"]
    page.select_option("#colour", "app_business_domain")
    assert "Mixes several" not in page.inner_text("#legend")   # rings only in queue manager view
    assert page.errors == []


def test_tabs_render(page):
    run_sample(page)
    for tab, needle in (("score", "Complexity goes from"), ("rules", "before and after"),
                        ("apps", "applications"), ("checks", "Every check passed")):
        page.click(f".tabs [data-tab={tab}]")
        assert needle in page.inner_text(f"#tab-{tab}")
    page.click(".tabs [data-tab=apps]")
    page.locator("#tab-apps tbody tr").first.click()
    assert not page.locator("#tab-topology").is_hidden()


def test_rule_switch_changes_result(page):
    run_sample(page)
    before = page.inner_text("#cap-target")
    page.locator("#rules details.more summary").click()
    rule = page.locator("#rules .rule", has_text="business domain").first
    rule.locator("button", has_text="Hard").click()
    assert "Settings changed" in page.inner_text("#run-note")
    page.click("#run")
    page.wait_for_function("(b) => document.querySelector('#cap-target').textContent !== b", arg=before, timeout=60000)
    page.click(".tabs [data-tab=rules]")
    assert "share the same business domain" in page.inner_text("#tab-rules")


def test_downloads(page):
    run_sample(page)
    page.click("#downloads summary")
    with page.expect_download() as d:
        page.click("#download-menu button:has-text('Target topology')")
    path = d.value.path()
    with open(path, encoding="utf-8") as f:
        assert f.readline().startswith("queue_name,queue_type,queue_manager")


def test_bad_file_is_explained(page, tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("colour,size\nred,1\n")
    page.set_input_files("#file", str(bad))
    page.wait_for_selector("#source-info .error")
    assert "required column" in page.inner_text("#source-info")
    assert page.is_hidden("#run-box")


def test_phone_width_has_no_sideways_scroll(browser, url):
    ctx = browser.new_context(viewport={"width": 390, "height": 844})
    pg = ctx.new_page()
    pg.goto(url)
    run_sample(pg)
    assert pg.evaluate("document.documentElement.scrollWidth - window.innerWidth") <= 0
    ctx.close()


def test_large_estate_stays_responsive(browser, url, tmp_path):
    import pandas as pd
    from mqresolver import synth
    cols, rows = synth.generate(apps=2000, qms=700, flows=3800, seed=11)
    src = tmp_path / "large.csv"
    pd.DataFrame(rows, columns=cols).to_csv(src, index=False)
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    pg = ctx.new_page()
    pg.goto(url)
    pg.set_input_files("#file", str(src))
    pg.wait_for_selector("#run-box:not([hidden])", timeout=60000)
    pg.click("#run")
    pg.wait_for_selector("#results:not([hidden])", timeout=180000)
    pg.click("#view-mode button[data-v=app]")
    pg.wait_for_timeout(300)
    frames = pg.evaluate("""async () => {
      const v = window.__mqr.views.as_is, t = window.__mqr.views.target, out = [];
      for (let i = 0; i < 30; i++) {
        const a = performance.now(); v.zoomAt(v.w / 2, v.h / 2, i % 2 ? 0.9 : 1.1); v.draw(); t.draw(); out.push(performance.now() - a);
        await new Promise(r => requestAnimationFrame(r));
      }
      return out.sort((x, y) => x - y);
    }""")
    assert frames[len(frames) // 2] < 16, frames            # median frame fits a 60 fps budget
    assert pg.evaluate("window.__mqr.views.as_is.nodes.length") > 2000
    ctx.close()
