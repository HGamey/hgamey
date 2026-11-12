"""全部手机 on the 设备 page says what each phone is working on: the task its job works for (a link) and how long that
job has run, read the way the home page reads it (this computer's lock and queue, the center's record of jobs it sent
to other computers); 空闲 when nothing holds it; — for a phone that is not there."""
import re
import time
import unittest
from unittest import mock

import test_core_flow_web_cli as core   # puts the node directory on sys.path

REMOTE = "r" * 32


class PhoneWorkTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli, web, pf = core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web, core.CoreFlowTests.pf
    write = staticmethod(core.CoreFlowTests.write)

    def test_each_phone_says_what_it_is_working_on(self):
        self.assertEqual(self.web("POST", "/research-state/research/sr-genshin", self.write(
            "web-task", {"title": "原神超分：帧率能提高多少", "objective": "画质不降时提高帧率"}))[0], 201)
        code, jid, err = self.pf(core.ADMIN, "--research", "sr-genshin", "--label", "selfsr 1080p", "--device-access", "none",
                                 "--detach", "fixture", "-c", "pass")
        self.assertEqual(code, 0, err)
        jid = jid.strip()
        for _ in range(100):   # the fixture job starts and records when
            if (self.board._job_meta(jid) or {}).get("started"):
                break
            time.sleep(0.05)
        started = self.board._job_meta(jid)["started"]
        lock = {"status": "held", "held_by": None, "lease": {"entries": [{"job_id": jid, "serial": "S1", "state": "active", "label": f"job-{jid}"}]}}
        remote = {f"{REMOTE}/RM1": {"kind": "job", "job_id": "f" * 12, "label": "跨机复现", "actor_id": "agent-b", "research_id": None,
                                    "research_title": None, "state": "running"}}
        patches = (mock.patch.object(self.board.monitor, "snapshot", return_value={"lock": lock, "devices": [{"serial": "S1"}, {"serial": "S2"}]}),
                   mock.patch.object(self.board, "_remote_holders", return_value=remote))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

        code, work = self.web("GET", "/phone-work")
        self.assertEqual(code, 200, work)
        self.assertEqual(sorted(work["local"]), ["S1"])
        self.assertEqual((work["local"]["S1"]["research_title"], work["local"]["S1"]["started"]), ("原神超分：帧率能提高多少", started))
        self.assertEqual(work["remote"][f"{REMOTE}/RM1"]["label"], "跨机复现")

        now = time.time()
        phone = lambda node, serial, connected=True: {   # noqa: E731
            "ref": f"{node}/{serial}", "node_id": node, "node_name": "lab", "device_id": serial, "serial": serial, "connected": connected,
            "stale": False, "sampled_at": now, "remote_actions": [], "observation": {"online": True, "brand": "OnePlus", "model": serial}}
        phones = [phone(self.node_id, "S1"), phone(self.node_id, "S2"), phone(REMOTE, "RM1"), phone(REMOTE, "GONE", False)]
        with self._browser_page() as page:
            page.route(re.compile(r".*/dash/api/phones(\?.*)?$"), lambda route: route.fulfill(json={"items": phones}))
            page.goto("http://%s:%d/dash/fleet" % self.address)
            page.wait_for_selector('#all-phones-list li[data-ref$="/GONE"] .phone-work')
            cells = dict(page.eval_on_selector_all("#all-phones-list li", "rows => rows.map(r => [r.dataset.ref.split('/').pop(), r.querySelector('.phone-work').innerText.trim()])"))
            self.assertRegex(cells["S1"], r"^原神超分：帧率能提高多少\n已跑 \d+ 分钟$")
            self.assertEqual((cells["S2"], cells["RM1"], cells["GONE"]), ("空闲", "跨机复现", "—"))
            link = page.locator('#all-phones-list li[data-ref$="/S1"] .phone-work a')
            self.assertTrue(link.get_attribute("href").startswith("/dash/research?kind=research&id=sr-genshin"))
            self.assertEqual(page.inner_text(".phone-head span:nth-child(4)"), "在做")


if __name__ == "__main__":
    unittest.main()
