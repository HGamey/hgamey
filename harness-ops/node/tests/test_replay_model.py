"""我来当一次 Agent, second part: /dash/api/replay plays a task back from what the platform kept of it. Each write is
a step with who sent it, the request as the agent's token or the page sent it, the platform's receipt and what the
task page showed; a job is a step when it was sent and another when it ended. A blind comparison stays blind, and
reading the replay writes nothing."""
import json
import time
import unittest
from pathlib import Path

import test_core_flow_web_cli as core   # puts the node directory on sys.path

RID = "sr-replay"


class ReplayModelTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    request, cli, web, agent, pf = (core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web,
                                    core.CoreFlowTests.agent, core.CoreFlowTests.pf)
    write = staticmethod(core.CoreFlowTests.write)

    def note(self, token, *args):
        code, out, err = self.pf(token, "research", "note", RID, *args)
        self.assertEqual(code, 0, err)
        return json.loads(out)["object"]["id"]

    def human_note(self, operation, text, **fields):
        code, receipt = self.web("POST", "/research-state/note/" + operation, self.write(operation, {"research_id": RID, "text": text, **fields}))
        self.assertEqual(code, 201, receipt)
        return operation

    def test_a_delivered_task_plays_back_step_by_step(self):
        self.assertEqual(self.web("POST", f"/research-state/research/{RID}", self.write(
            "create-replay", {"title": "红魔上原神超分对比", "objective": "比画质和帧率"}))[0], 201)
        self.assertEqual(self.cli("POST", "/agents", {"name": "agent-a"})[0], 201)
        token = self.cli("POST", "/agents/agent-a/token", {})[1]["token"]
        self.assertEqual(self.pf(token, "research", "claim", RID)[0], 0)
        self.assertEqual(self.agent(token, "POST", "/research-state/direction/d-replay", self.write(
            "dir-replay", {"research_id": RID, "title": "自研超分", "hypothesis": "画质比双线性好"}))[0], 201)
        # A run on a phone that ended, with a picture the agent shows in a note.
        ran, started = "cdcdcdcdcdcd", time.time()
        Path(self.jobs.path(ran, "out")).mkdir(parents=True)
        Path(self.jobs.path(ran, "out", "shot.png")).write_bytes(b"\x89PNG\r\n\x1a\nshot")
        Path(self.jobs.path(ran, "out", "a.png")).write_bytes(b"\x89PNG\r\n\x1a\na")
        Path(self.jobs.path(ran, "out", "b.png")).write_bytes(b"\x89PNG\r\n\x1a\nb")
        self.jobs._write_meta(ran, {"id": ran, "bin": "experiment", "argv": ["selfsr/repro.sh", "RM1"], "label": None,
                                    "actor_id": "agent-a", "research_id": RID, "direction_id": "d-replay", "device_access": "write",
                                    "started": started, "ended": started + 0.001, "outcome": "succeeded", "exit": 0})
        self.note(token, "第一轮跑完，画面见图", "--evidence", f"job:{ran}:shot.png")
        asked = self.note(token, "要不要换一台手机？", "--ask")
        answer = self.human_note("n-answer", "不用换", reply_to=asked)
        guidance = self.human_note("n-guidance", "再加一组低分辨率")
        self.note(token, "好，就用这台", "--reply-to", answer, "--decision", "adopt")
        self.note(token, "采纳，加一组低分辨率", "--reply-to", guidance, "--decision", "adopt")
        queued = self.jobs.dispatch.submit({"bin": "fixture", "argv": ["-c", "pass"], "device_access": "none", "resources": [],
                                            "actor_id": "agent-a", "research_id": RID, "label": "低分辨率一组"}, b"", "replay-queued")
        queued = queued["job"]["id"] if "job" in queued else queued["id"]
        code, out, err = self.pf(token, "research", "compare", RID, "--item", f"job:{ran}:a.png", "--item", f"job:{ran}:b.png",
                                 "--pick", "1", "--title", "哪张更清楚", "--question", "选更清楚的一张")
        self.assertEqual(code, 0, err)
        hidden = json.loads(out)["result_id"]
        self.assertEqual(self.agent(token, "POST", "/research-state/request/rq-replay", self.write("rq-replay", {
            "research_id": RID, "type": "assistance", "title": "把红魔充上电", "need": "插上充电线", "why": "电量低",
            "state": "open", "context": {"research_revision": 2, "directions": []}}))[0], 201)
        self.assertEqual(self.web("POST", "/research-state/feedback/fb-replay", self.write("fb-replay", {
            "request_id": "rq-replay", "request_revision": 1, "text": "插好了"}))[0], 201)
        self.assertEqual(self.pf(token, "research", "conclude", "d-replay", "verified", "--reason", "画质更好")[0], 0)
        self.assertEqual(self.pf(token, "research", "deliver", RID, "--summary", "## 结论\n自研超分画质更好。",
                                 "--artifact", f"job:{ran}")[0], 0)
        late = self.human_note("n-late", "报告里补一句帧率")
        self.note(token, "采纳，已补", "--reply-to", late, "--decision", "adopt")
        self.assertEqual(self.pf(token, "research", "deliver", RID, "--summary", "## 结论\n自研超分画质更好，帧率持平。",
                                 "--artifact", f"job:{ran}")[0], 0)

        head = self.jobs.research.events({"after": ["0"], "limit": ["1"]})["head"]
        code, replay = self.web("GET", "/replay?id=" + RID, steer=False)
        self.assertEqual(code, 200, replay)
        self.assertEqual(self.jobs.research.events({"after": ["0"], "limit": ["1"]})["head"], head, "reading writes nothing")
        steps = replay["steps"]
        self.assertEqual([s["kind"] for s in steps], [
            "task_create", "claim", "direction_new", "job_submit", "job_end", "note_media", "ask", "human_note", "human_note",
            "reply", "reply", "job_submit", "eval_new", "request_new", "feedback", "direction_end", "deliver", "human_note",
            "reply", "deliver"])
        self.assertEqual([s["n"] for s in steps], list(range(len(steps))))
        self.assertEqual([s["who"]["kind"] for s in steps][:5], ["human", "agent", "agent", "agent", "platform"])
        self.assertEqual(replay["agents"], ["agent-a"])
        self.assertEqual((replay["task"]["state"], replay["task"]["report_url"]), ("delivered", "/dash/research/report?id=" + RID))
        by = lambda kind: [s for s in steps if s["kind"] == kind]

        # The page's own write, then the agent's: the request each sent and the receipt the platform kept.
        created, claim = steps[0], steps[1]
        self.assertEqual(created["sent"]["path"], f"/dash/api/research-state/research/{RID}")
        self.assertEqual((created["sent"]["body"]["base_revision"], created["sent"]["body"]["data"]["title"]), (None, "红魔上原神超分对比"))
        self.assertEqual(claim["sent"]["path"], f"/research-state/research/{RID}")
        self.assertEqual((claim["sent"]["body"]["base_revision"], claim["sent"]["body"]["data"]), (1, {"owner": "agent-a"}))
        self.assertEqual(claim["answer"]["raw"], self.jobs.research.operation(claim["sent"]["body"]["operation_id"]))
        self.assertEqual(claim["answer"]["raw"]["changed_fields"], ["owner"])
        self.assertEqual((claim["summary"], claim["seen"]["owner"]), ("认领这个任务", "agent-a"))

        submitted, ended = by("job_submit")[0], by("job_end")[0]
        self.assertEqual(submitted["sent"]["path"], "/jobs")
        self.assertEqual(submitted["sent"]["headers"]["X-Job"]["argv"], ["selfsr/repro.sh", "RM1"])
        self.assertEqual(submitted["answer"]["raw"]["id"], ran)
        self.assertEqual((submitted["seen"]["url"], submitted["seen"]["direction"]["title"]), (f"/dash/jobs#job={ran}", "自研超分"))
        self.assertTrue(submitted["summary"].startswith("在手机上跑："))
        self.assertIsNone(ended["sent"])
        self.assertEqual(ended["who"], {"kind": "platform", "name": "平台", "for": "agent-a"})
        self.assertIn("退出码 0", ended["answer"]["text"])
        self.assertEqual((ended["seen"]["outcome"], ended["key"]), ("succeeded", False))
        self.assertEqual(by("job_submit")[1]["answer"]["raw"]["id"], queued)
        self.assertEqual(len([s for s in steps if s["seen"].get("id") == queued]), 1, "a job still queued has not ended")

        self.assertEqual(by("note_media")[0]["seen"]["evidence"], [{"kind": "job", "job_id": ran, "path": "shot.png"}])
        human = by("human_note")
        self.assertEqual(human[0]["summary"], "回答 agent-a 的提问：不用换")
        self.assertEqual(human[1]["summary"], "在任务页上留言：再加一组低分辨率")
        reply = by("reply")[1]
        self.assertEqual((reply["summary"], reply["seen"]["reply_to"]["text"]), ("答复留言（采纳）：采纳，加一组低分辨率", "再加一组低分辨率"))
        compare = by("eval_new")[0]
        self.assertEqual(compare["summary"], "发出盲评：哪张更清楚")
        self.assertEqual([(s["slot"], s["kind"]) for s in compare["seen"]["evaluation"]["slots"]], [("A", "image"), ("B", "image")])
        self.assertTrue(compare["seen"]["evaluation"]["slots"][0]["url"].startswith(f"/research-state/request/{compare['seen']['id']}/media/A?"))
        self.assertNotIn(hidden, json.dumps(replay), "a comparison nobody has answered stays blind")
        self.assertEqual(by("feedback")[0]["summary"], "答复「把红魔充上电」：插好了")
        self.assertEqual(by("direction_end")[0]["summary"], "给方向「自研超分」下结论：已验证")

        # A note on a delivered task reopened it in the same write: one step, not two.
        self.assertTrue(human[2]["answer"]["reopened"])
        self.assertIn("任务从「已交付」回到「推进中」", human[2]["answer"]["text"])
        self.assertFalse(any(s["sent"] and s["sent"]["body"].get("operation_id", "").startswith("auto-reopen-")
                             for s in steps if s["sent"] and "body" in s["sent"]))
        first, second = by("deliver")
        self.assertEqual((first["summary"], second["summary"]), ("交付报告（第 1 版）", "更新交付报告（第 2 版）"))
        self.assertEqual((second["seen"]["type"], second["seen"]["version"]), ("report", 2))
        self.assertIn("帧率持平", second["seen"]["summary"])
        self.assertEqual([s["kind"] for s in steps if not s["key"]], ["job_end"])

        self.assertEqual(self.web("GET", "/replay?id=sr-nope", steer=False)[0], 404)


if __name__ == "__main__":
    unittest.main()
