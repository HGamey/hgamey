"""A phone a person should look at stands out on the 设备 page: battery under 10% or a SoC at 45 °C or more gets an
amber tag, on its reading card and in 全部手机. Shown only: jobs are still sent to it. A phone that is not there
shows no warning from an old reading."""
import re
import time
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path


def device(serial, brand, model, level, heat):
    return {"serial": serial, "transport": "adb", "online": True, "state": "device", "brand": brand, "model": model,
            "battery": {"level_pct": level, "on_battery": False}, "thermal": {"soc_max_c": heat}}


class PhoneWarningTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page

    def test_low_battery_and_heat_get_amber_tags(self):
        now = time.time()
        devices = [device("HN1", "HONOR", "AAP-AN00", 2, 48.6), device("OP1", "OnePlus", "PLC110", 94, None),
                   device("RM1", "REDMAGIC", "NX809J", 44, 41.8)]
        phones = [{"ref": f"{self.node_id}/{d['serial']}", "node_id": self.node_id, "node_name": "local", "device_id": d["serial"],
                   "serial": d["serial"], "connected": True, "stale": False, "sampled_at": now, "remote_actions": [],
                   "observation": d} for d in devices]
        phones.append({**phones[0], "ref": f"{self.node_id}/GONE", "serial": "GONE", "connected": False,
                       "observation": device("GONE", "OnePlus", "PHK110", 1, 50)})
        with self._browser_page() as page:
            page.route(re.compile(r".*/dash/api/fleet(\?.*)?$"), lambda route: route.fulfill(json={
                "schema_version": 1, "lock": None, "devices": devices, "sampled_at": now, "age_s": 0, "stale": False}))
            page.route(re.compile(r".*/dash/api/phones(\?.*)?$"), lambda route: route.fulfill(json={"items": phones}))
            page.goto("http://%s:%d/dash/fleet" % self.address)
            page.wait_for_selector("#devices .device-card")
            page.wait_for_selector('#all-phones-list li[data-ref$="/GONE"]')
            cards = page.eval_on_selector_all("#devices .device-card", """cards => cards.map(c => [c.querySelector('h3').textContent,
                [...c.querySelectorAll('.device-head .task-state.warn')].map(t => t.textContent)])""")
            self.assertEqual(cards, [["AAP-AN00", ["电量低 2%", "过热 48.6 °C"]], ["PLC110", []], ["NX809J", []]])
            rows = page.eval_on_selector_all("#all-phones-list li", """rows => rows.map(r => [r.dataset.ref.split('/').pop(),
                [...r.querySelectorAll('.phone-state .task-state.warn')].map(t => t.textContent)])""")
            self.assertEqual(dict(rows), {"HN1": ["电量低 2%", "过热 48.6 °C"], "OP1": [], "RM1": [], "GONE": []})
            color = page.eval_on_selector("#devices .task-state.warn", "t => getComputedStyle(t).color")
            plain = page.eval_on_selector("#devices .task-state:not(.warn)", "t => getComputedStyle(t).color")
            self.assertNotEqual(color, plain)


if __name__ == "__main__":
    unittest.main()
