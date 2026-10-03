"""Browser smoke test of the agent console (Playwright). Asks real questions through the UI and screenshots.

  <venv-with-playwright>/bin/python tests/ui_smoke.py http://127.0.0.1:8090 <password> <outdir> ["question" ...]
"""
import os
import sys

from playwright.sync_api import sync_playwright

base, pw, out = sys.argv[1], sys.argv[2], sys.argv[3]
questions = sys.argv[4:] or ["How many active users do we have in each EMEA country over the last 30 days? Plot it."]
os.makedirs(out, exist_ok=True)
errors = []
with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(http_credentials={"username": "admin", "password": pw}, viewport={"width": 1500, "height": 950})
    pg = ctx.new_page()
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    pg.goto(base + "/?s=ui-smoke-" + str(os.getpid()))
    pg.wait_for_selector(".msg.assistant")
    for i, q in enumerate(questions, 1):
        n_before = pg.locator(".msg.assistant").count()
        pg.fill("#q", q)
        pg.click("#send")
        pg.wait_for_function("n => document.querySelectorAll('.msg.assistant').length > n", arg=n_before)
        pg.wait_for_timeout(2500)
        pg.screenshot(path=f"{out}/q{i}-working.png")
        pg.wait_for_function("() => !document.querySelector('#send').disabled", timeout=300000)
        pg.wait_for_timeout(1200)
        last = pg.locator(".msg.assistant").last
        last.scroll_into_view_if_needed()
        pg.screenshot(path=f"{out}/q{i}-done.png")
        print(f"Q{i}: {q}\n  artifacts={last.locator('.artifact').count()} charts={last.locator('canvas').count()} "
              f"trace_steps={pg.locator('#trace details.step').count()}\n  answer: {last.locator('.body').inner_text()[:300]!r}")
    for path in ("/escalations", "/overview", "/traces"):
        pg.goto(base + path)
        pg.screenshot(path=f"{out}/{path.strip('/')}.png", full_page=False)
    first = pg.locator("table a[href^='/traces/']").first
    if first.count():
        pg.goto(base + first.get_attribute("href"))
        pg.screenshot(path=f"{out}/trace-detail.png", full_page=False)
    b.close()
print("JS errors:", errors or "none")
