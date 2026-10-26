"""One real-browser pass over the tasks page pause/resume controls (local test node).

Run manually on rhodes1 with the node service on 127.0.0.1:8765:
    python3 tests/browser_pause_check.py
Needs playwright plus the local dash passwords; not part of unittest discovery.
"""
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

HOME = Path.home()
PASS = (HOME / ".config/hgamey/dash.pass").read_text().strip()
STEER = (HOME / ".config/hgamey/steer.pass").read_text().strip()
TOKEN = (HOME / ".config/hgamey/node.token").read_text().strip()
OUT = Path(os.environ.get("PAUSE_CHECK_OUT", str(Path(__file__).resolve().parents[3] / "qa/browser-pause")))
OUT.mkdir(parents=True, exist_ok=True)


NODE_ID = json.loads(urllib.request.urlopen(urllib.request.Request(
    "http://127.0.0.1:8765/node",
    headers={"Authorization": "Bearer " + TOKEN})).read())["node_id"]


def submit(spec):
    request = urllib.request.Request(
        "http://127.0.0.1:8765/jobs", data=b"", method="POST",
        headers={"Authorization": "Bearer " + TOKEN, "X-Expected-Node-ID": NODE_ID,
                 "X-Job": json.dumps(spec), "X-Request-ID": __import__("uuid").uuid4().hex,
                 "Content-Type": "application/gzip"})
    return json.loads(urllib.request.urlopen(request).read())


def api(path, body=None, method=None):
    request = urllib.request.Request(
        "http://127.0.0.1:8765" + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method or ("POST" if body is not None else "GET"),
        headers={"Authorization": "Bearer " + TOKEN, "X-Expected-Node-ID": NODE_ID,
                 "Content-Type": "application/json", **({"X-Request-ID": __import__("uuid").uuid4().hex}
                                                        if method == "POST" and path == "/jobs" else {})})
    return json.loads(urllib.request.urlopen(request).read())

# 准备一个已暂停的排队任务：提交后立刻暂停，被领取则换新 ID 重试。
paused_id = None
for attempt in range(6):
    job = submit({"bin": "phonefarm", "argv": ["last", "--json"], "device_access": "none",
                  "resources": []})
    jid = job["id"]
    for _ in range(20):
        try:
            result = api(f"/jobs/{jid}/pause", {})
        except Exception:
            break
        if result.get("paused") is True:
            paused_id = jid
            break
        if result.get("state") != "queued":
            break
        time.sleep(0.05)
        if api(f"/jobs/{jid}").get("paused"):
            paused_id = jid
            break
    if paused_id:
        break
if not paused_id:
    print(json.dumps({"problems": ["could not prepare a paused queued job"]}))
    sys.exit(1)
print("prepared paused job:", paused_id)

from playwright.sync_api import sync_playwright

problems, notes = [], []
with sync_playwright() as p:
    browser = p.chromium.launch(
        executable_path=str(HOME / ".cache/ms-playwright/chromium-1248/chrome-linux64/chrome"),
        headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto("http://127.0.0.1:8765/dash/login")
    page.fill('input[name="pw"]', PASS)
    page.click('button[type="submit"]')
    page.wait_for_url("**/dash**", timeout=10000)
    page.goto("http://127.0.0.1:8765/dash/jobs")
    page.fill("#query", paused_id)
    page.click("#search-jobs")
    page.wait_for_selector(f'button[data-job="{paused_id}"]', timeout=10000)
    page.click(f'button[data-job="{paused_id}"]')
    page.wait_for_selector("#pause-job", state="visible", timeout=10000)
    page.click("#access-toggle")
    page.wait_for_selector("#steer-password", state="visible")
    page.fill("#steer-password", STEER)
    page.click("#unlock-submit")
    page.wait_for_timeout(1200)
    resume_disabled = page.locator("#resume-job").is_disabled()
    pause_disabled = page.locator("#pause-job").is_disabled()
    (notes if not resume_disabled else problems).append(
        f"paused job {paused_id}: resume enabled={not resume_disabled}, pause disabled={pause_disabled}")
    paused_text = page.locator("#pause-job").inner_text()
    (notes if "已暂停" in paused_text else problems).append(f"pause button label: {paused_text!r}")
    page.screenshot(path=str(OUT / "jobs-paused-1440.png"), full_page=True)
    if not resume_disabled:
        responses = []
        page.on("response", lambda r: responses.append((r.status, r.url)) if "/resume" in r.url or "/pause" in r.url else None)
        page.click("#resume-job")
        page.wait_for_timeout(2000)
        notes.append(f"pause/resume network: {responses}")
        notes.append(f"pause-result text: {page.locator('#pause-result').inner_text()!r}")
        row_now = api(f"/jobs/{paused_id}")
        notes.append(f"api after resume: paused={row_now.get('paused')} phase={row_now.get('phase')} queue={row_now.get('queue', {}).get('state')}")
        status = api(f"/jobs/{paused_id}")
        resumed = status.get("paused") is False or status.get("state") == "finished"
        (notes if resumed else problems).append(
            f"after resume click: paused={status.get('paused')} state={status.get('state')} phase={status.get('phase')}")
        receipt_visible = page.locator("#pause-result").is_visible()
        (notes if receipt_visible else problems).append("pause-result receipt visible after resume")
        page.screenshot(path=str(OUT / "jobs-resumed-1440.png"), full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    page.screenshot(path=str(OUT / "jobs-390.png"), full_page=True)
    notes.append(f"pageerrors: {errors or 'none'}")
    browser.close()

report = {"scope": "one real browser pass over pause/resume on the local test node; light theme, 1440+390",
          "paused_job": paused_id, "problems": problems, "notes": notes,
          "screenshots": sorted(p.name for p in OUT.glob("*.png"))}
(OUT / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
print(json.dumps(report, ensure_ascii=False, indent=2))
sys.exit(1 if problems else 0)
