"""我来当一次 Agent: a person walks an agent's five steps on the real platform under a temporary identity, the page
showing for each step what was sent, what came back and what the web shows of it. The identity and its task stay off
the home page, the task list, the command view and the agent list, and a tour ends by itself."""
import base64
import collections
import json
import time
import unittest
from pathlib import Path
from unittest import mock

import test_core_flow_web_cli as core   # puts the node directory on sys.path
import dash_tour

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg==")
SHOOT = f"import base64, os, sys; os.makedirs(sys.argv[1]); open(os.path.join(sys.argv[1], 'screen.png'), 'wb').write(base64.b64decode({base64.b64encode(PNG).decode()!r}))"


def fixture_shot(self, tour):
    """The screenshot step on a computer without phones: a fixture job that writes the picture a phone would."""
    return (["--research", tour["task"], "--label", "体验：截一张图", "--detach", "fixture", "-c", SHOOT, "@out/shot"],
            {"ref": None, "name": "测试手机"})


class AgentTourTests(unittest.TestCase):
    request, web, cli, pf = core.CoreFlowTests.request, core.CoreFlowTests.web, core.CoreFlowTests.cli, core.CoreFlowTests.pf

    def setUp(self):
        core.CoreFlowTests.setUp(self)
        self.board.fails = collections.defaultdict(list)   # the unlock's throttle; this board skips __init__
        del self.jobs.dispatch.start          # the tour's screenshot job really runs
        self.addCleanup(self.jobs.dispatch.close)
        patch = mock.patch.object(dash_tour.Tours, "shot_args", fixture_shot)
        patch.start()
        self.addCleanup(patch.stop)

    def wait_shot(self, tid):
        deadline = time.time() + 30
        while time.time() < deadline:
            code, view = self.web("GET", f"/tour/{tid}")
            self.assertEqual(code, 200, view)
            if view["seen"]["shot"] != "waiting":
                return view
            time.sleep(0.2)
        self.fail("the screenshot job did not finish")

    def hidden(self, tid):
        tasks = self.web("GET", "/tasks?all=1")[1]["items"]
        self.assertNotIn(tid, [t["id"] for t in tasks])
        self.assertIn(tid, [t["id"] for t in self.web("GET", "/tasks?all=1&tour=1")[1]["items"]])
        self.assertNotIn(tid, [t["id"] for t in self.board._tasks()])
        model = self.board._model()
        self.assertNotIn(tid, [t["id"] for t in model["research"]] + model["agents"])
        self.assertFalse([n for n in model["notes"] if n["research_id"] == tid])
        self.assertFalse([j for j in model["jobs"] if j["research_id"] == tid])
        self.assertEqual({a["name"]: a["archived"] for a in self.board._agents()}[tid], "tour")

    def test_five_steps_through_the_page_then_the_tour_is_over(self):
        with core.CoreFlowTests._browser_page(self) as page:
            base = "http://%s:%d" % self.address
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base + "/dash/tour")
            page.wait_for_selector("#tour-start:not([hidden])")
            self.assertEqual(page.eval_on_selector_all(".tour-step h2", "e => e.map(h => h.textContent)"),
                             ["签到", "认领任务", "让空闲手机截图", "写进展留言", "交付"])
            page.click("#tour-start")
            page.wait_for_selector("#tour-unlock:not([hidden])")   # a write: unlocked on the page, then it goes on
            page.fill("#tour-pw", core.STEER)
            page.click("#tour-unlock button")
            page.wait_for_selector('.tour-step[data-step="checkin"][data-state="next"]')
            tid = page.evaluate("new URLSearchParams(location.search).get('id')")
            self.assertRegex(tid, r"^tour-[0-9a-f]{6}$")
            for step in ("checkin", "claim", "shot", "note", "deliver"):
                button = f'.tour-step[data-step="{step}"] .tour-step-head button:not([disabled])'
                page.wait_for_selector(button, timeout=30000)
                page.click(button)
                page.wait_for_selector(f'.tour-step[data-step="{step}"][data-state="done"]', timeout=30000)
                if step == "checkin":
                    self.hidden(tid)

            # Each step: what was sent (the command, folded), what came back and what the web shows.
            cols = page.eval_on_selector_all(".tour-step", """steps => steps.map(li => [li.dataset.step,
                [...li.querySelectorAll('.tour-col')].map(col => col.querySelector('h3').textContent)])""")
            self.assertEqual(cols, [[s, ["发出的", "平台返回的", "网页上看到的"]] for s in ("checkin", "claim", "shot", "note", "deliver")])
            self.assertEqual(page.eval_on_selector_all(".tour-raw[open]", "e => e.length"), 0)
            self.assertEqual(page.text_content('.tour-step[data-step="claim"] .tour-col:nth-child(1) pre'), f"pf-node research claim {tid}")
            claimed = page.text_content('.tour-step[data-step="claim"] .tour-card')
            self.assertIn("负责人 " + tid, claimed)
            page.wait_for_function("""() => [...document.querySelectorAll('.tour-step[data-step="shot"] img.tour-shot, .tour-step[data-step="note"] img.tour-shot')]
                .every(img => img.complete && img.naturalWidth === 1)""")
            self.assertIn("截好了", page.text_content('.tour-step[data-step="note"] .tour-card'))
            self.assertIn("已交付", page.text_content('.tour-step[data-step="deliver"] .tour-card'))
            self.assertIn("已交付", page.text_content("#tour-who"))
            self.assertTrue(page.is_hidden("#tour-end"))
            self.assertEqual(errors, [])

        task = self.cli("GET", f"/research-state/research/{tid}")[1]["data"]
        self.assertEqual((task["state"], task["owner"]), ("delivered", tid))
        self.assertIn(f"![测试手机 现在的画面](job:", task["deliverable"]["summary"])
        self.assertFalse(self.cli("GET", f"/agents/{tid}")[1]["enabled"])
        self.hidden(tid)

    def test_a_tour_ended_or_left_closes_its_task_and_stops_its_identity(self):
        self.assertEqual(self.web("POST", "/tour", {}, steer=False)[0], 403)
        code, tour = self.web("POST", "/tour", {})
        self.assertEqual(code, 200, tour)
        tid = tour["id"]
        self.assertEqual(self.web("POST", f"/tour/{tid}/claim", {})[0], 409)   # steps go in order
        code, tour = self.web("POST", f"/tour/{tid}/checkin", {})
        self.assertEqual((code, tour["next"]), (200, "claim"))
        self.assertEqual(tour["steps"]["checkin"]["code"], 0)
        self.assertIsNotNone(tour["seen"]["agent"]["checkin_at"])
        self.assertIn(f"可认领：{tid}", tour["steps"]["checkin"]["stdout"])
        # Other agents are not offered the tour's task.
        self.assertEqual(self.cli("POST", "/agents", {"name": "agent-x"})[0], 201)
        other = self.cli("POST", "/agents/agent-x/token", {})[1]["token"]
        code, out, err = self.pf(other, "checkin", "--json")
        self.assertEqual(code, 0, err)
        self.assertNotIn(tid, [t["research_id"] for t in json.loads(out)["research"]["unclaimed"]])
        self.assertEqual([t["id"] for t in self.web("GET", "/tour")[1]["open"]], [tid])
        token = Path(self.jobs.data_root, "_tour", tid, "token").read_text().strip()

        code, tour = self.web("POST", f"/tour/{tid}/end", {})
        self.assertEqual((code, tour["end_reason"], tour["seen"]["task"]["state"]), (200, "ended", "closed"))
        self.assertEqual(self.web("POST", f"/tour/{tid}/claim", {})[0], 409)
        self.assertNotEqual(self.pf(token, "checkin")[0], 0)
        self.assertEqual(self.web("GET", "/tour")[1]["open"], [])

        # One left open is ended a day later, when anything reads it.
        left = self.web("POST", "/tour", {})[1]["id"]
        path = Path(self.jobs.data_root, "_tour", left, "tour.json")
        stored = json.loads(path.read_text())
        path.write_text(json.dumps({**stored, "created_at": stored["created_at"] - 2 * 86400}))
        self.assertEqual(self.web("GET", "/tour")[1]["open"], [])
        view = self.web("GET", f"/tour/{left}")[1]
        self.assertEqual((view["end_reason"], view["seen"]["task"]["state"], view["seen"]["agent"]["enabled"]), ("left", "closed", False))


if __name__ == "__main__":
    unittest.main()
