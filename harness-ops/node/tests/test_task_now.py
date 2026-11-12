"""The top of a task page says in one line what is happening now: what it waits on a person for (questions to answer,
comparisons to judge, and how long the oldest has waited), else the jobs running or queued for it, else when its owner
last did something there and what. A task nobody owns says so; a closed task has no such line."""
import json
import time
import unittest
from pathlib import Path

import test_core_flow_web_cli as core   # puts the node directory on sys.path
from test_report_pictures import png


class TaskNowTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli, web, pf = core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web, core.CoreFlowTests.pf
    write = staticmethod(core.CoreFlowTests.write)

    def task(self, rid, **data):
        self.assertEqual(self.web("POST", f"/research-state/research/{rid}", self.write(
            "create-" + rid, {"title": rid, "objective": "现在", **data}))[0], 201)

    def line(self, page, rid, jobs=None):
        page.unroute("**/dash/api/jobs?research=*")
        if jobs is not None:
            page.route(f"**/dash/api/jobs?research={rid}*", lambda route: route.fulfill(json={
                "schema_version": 1, "node_id": self.node_id, "items": jobs, "next_cursor": None}))
        page.goto("http://%s:%d/dash/research?kind=research&id=%s" % (*self.address, rid))
        page.wait_for_selector("#task-runs")
        page.wait_for_function("() => document.querySelector('.task-now span')?.textContent")
        return page.locator(".task-now").inner_text()

    def test_one_line_says_what_the_task_waits_on_runs_or_last_did(self):
        self.task("sr-now")
        self.assertEqual(self.cli("POST", "/agents", {"name": "agent-a"})[0], 201)
        token = self.cli("POST", "/agents/agent-a/token", {})[1]["token"]
        self.assertEqual(self.pf(token, "research", "claim", "sr-now")[0], 0)
        self.assertEqual(self.pf(token, "research", "note", "sr-now", "先跑 720p 基线。然后看 1080p。")[0], 0)
        code, now = self.web("GET", "/tasks/now?id=sr-now")
        self.assertEqual(code, 200, now)
        self.assertEqual((now["owner"], now["questions"], now["comparisons"], now["last"]["kind"]), ("agent-a", [], [], "note"))
        self.task("sr-free")
        self.task("sr-shut", state="closed", state_reason="不做了")
        mine = {"id": "a" * 12, "bin": "fixture", "argv": ["run"], "label": "720p 基线", "actor_id": "agent-a",
                "state": "finished", "outcome": "succeeded", "started": time.time() + 5}

        with self._browser_page() as page:
            self.assertEqual(self.line(page, "sr-now"), "现在负责人 agent-a，刚刚最后一次动作：留言「先跑 720p 基线。」")
            self.assertEqual(self.line(page, "sr-now", [mine]), "现在负责人 agent-a，刚刚最后一次动作：启动作业「720p 基线」")
            self.assertEqual(self.line(page, "sr-free"), "现在还没人负责")
            page.goto("http://%s:%d/dash/research?kind=research&id=sr-shut" % self.address)
            page.wait_for_selector("#task-runs")
            self.assertEqual(page.locator(".task-now").count(), 0)

            # A question and a comparison wait on a person; one job runs and one waits in the queue.
            code, out, err = self.pf(token, "research", "note", "sr-now", "先测 1080p 还是 720p？", "--ask")
            self.assertEqual(code, 0, err)
            question = json.loads(out)["object"]["id"]
            output = Path(self.jobs.path(self.jid, "out"))
            output.mkdir(parents=True, exist_ok=True)
            (output / "a.png").write_bytes(png(b"\x10\x20\x30"))
            (output / "b.png").write_bytes(png(b"\x30\x20\x10"))
            code, out, err = self.pf(token, "research", "compare", "sr-now", "--title", "两张截图", "--question", "哪个清楚",
                                     "--item", f"job:{self.jid}:a.png", "--item", f"job:{self.jid}:b.png")
            self.assertEqual(code, 0, err)
            comparison = json.loads(out)["request_id"]
            code, now = self.web("GET", "/tasks/now?id=sr-now")
            self.assertEqual(([q["id"] for q in now["questions"]], [c["id"] for c in now["comparisons"]]), ([question], [comparison]))
            self.assertEqual(self.web("GET", "/tasks/now?id=sr-free")[1]["questions"], [])
            jobs = [{**mine, "id": "b" * 12, "state": "running", "phase": "running", "outcome": None, "started": time.time() - 2400},
                    {**mine, "id": "c" * 12, "state": "queued", "outcome": None, "started": None}]
            self.assertEqual(self.line(page, "sr-now", jobs),
                             "现在在等你：回答 1 个问题、评 1 组对比，最早的已等 1 分钟；在跑 1 个作业（最早的 40 分钟前开始），排队 1 个")
            # Its links go to what waits, on the same page: the question in 留言, the comparison in 待你评.
            page.wait_for_selector(f"#note-{question}")
            page.click(".task-now a:has-text('回答 1 个问题')")
            page.wait_for_function(f"() => {{ const r = document.getElementById('note-{question}').getBoundingClientRect(); return r.top >= 0 && r.bottom <= innerHeight; }}")
            self.assertEqual(page.evaluate("location.hash"), "")
            self.assertEqual(page.get_attribute(".task-now a:has-text('评 1 组对比')", "href"), f"#cmp-{comparison}")


if __name__ == "__main__":
    unittest.main()
