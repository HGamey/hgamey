"""The 设备 page names the app in front of each phone: a game registered under 游戏与资源 by its title, a common system
app (the desktop, a browser) from a small table, anything else by its package name as before. A named app keeps its
package name as a technical detail."""
import re
import time
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path

GAME = {"game_id": "doom3-demo", "version": "v73", "title": "Doom 3 Demo / idTech4A++ v73 · 打开结果与原版",
        "manifest": {"title": "Doom 3 Demo / idTech4A++ v73 · 打开结果与原版", "application": {"package": "com.karin.idTech4Amm"}}}


def device(serial, model, foreground):
    return {"serial": serial, "transport": "adb", "online": True, "state": "device", "brand": "OnePlus", "model": model,
            "foreground": foreground, "battery": {"level_pct": 80}, "thermal": {"soc_max_c": 38}}


class FleetAppNameTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page

    def test_the_app_in_front_reads_by_name(self):
        devices = [device("A", "PLC110", "com.karin.idTech4Amm"), device("B", "PHK110", "com.heytap.browser"),
                   device("C", "PHP110", "com.example.lab"), device("D", "PKG110", "net.oneplus.launcher/.Launcher")]
        with self._browser_page() as page:
            page.route(re.compile(r".*/dash/api/fleet(\?.*)?$"), lambda route: route.fulfill(json={
                "schema_version": 1, "lock": None, "devices": devices, "sampled_at": time.time(), "age_s": 0, "stale": False}))
            page.route(re.compile(r".*/dash/api/games(\?.*)?$"), lambda route: route.fulfill(json={"schema_version": 1, "items": [GAME]}))
            page.goto("http://%s:%d/dash/fleet" % self.address)
            page.wait_for_function("() => document.querySelector('#devices .device-card')?.innerText.includes('Doom 3 Demo')")
            read = """cards => cards.map(c => { const dt = [...c.querySelectorAll('dt')].find(d => d.textContent === '前台应用');
                return dt.nextElementSibling.innerText.trim(); })"""
            self.assertEqual(page.eval_on_selector_all("#devices .device-card", read),
                             ["Doom 3 Demo", "OPPO 浏览器", "com.example.lab", "桌面"])
            page.click(".shell-more-btn")
            page.click("#shell-tech")
            self.assertEqual(page.eval_on_selector_all("#devices .device-card", read),
                             ["Doom 3 Demo · com.karin.idTech4Amm", "OPPO 浏览器 · com.heytap.browser", "com.example.lab",
                              "桌面 · net.oneplus.launcher/.Launcher"])


if __name__ == "__main__":
    unittest.main()
