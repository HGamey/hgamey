"""The task page's line about the latest failed job is amber, 最近一次失败的作业, not an alarm; and a failure the same
kind of job (same label) got past later is history and is not shown."""
import json
import time
import unittest
from pathlib import Path

import test_core_flow_web_cli as core   # puts the node directory on sys.path


class FailedBarTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli, web = core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web
    write = staticmethod(core.CoreFlowTests.write)

    def run_of(self, rid, key, label, at, outcome):
        submitted = self.jobs.dispatch.submit({"bin": "fixture", "argv": ["-c", "pass"], "device_access": "none", "resources": [],
                                               "actor_id": "agent-a", "research_id": rid, "label": label}, b"", key)
        jid = submitted["job"]["id"] if "job" in submitted else submitted["id"]
        self.jobs.dispatch.queue.set_state(jid, "done")
        (Path(self.jobs.path(jid)) / "meta.json").write_text(json.dumps(
            {"id": jid, "bin": "fixture", "argv": ["-c", "pass"], "label": label, "actor_id": "agent-a", "research_id": rid,
             "started": at, "ended": at + 5, "state": "finished", "phase": "finished", "outcome": outcome,
             "exit": 0 if outcome == "succeeded" else 2, "error": None if outcome == "succeeded" else "超时",
             "execution": {"kind": "supervised-v1"}}))

    def test_a_failure_is_amber_and_one_retried_since_is_not_shown(self):
        base = time.time() - 1000
        for rid, failed_at, succeeded_at in (("sr-retried", base, base + 100), ("sr-still", base + 100, base)):
            self.assertEqual(self.web("POST", f"/research-state/research/{rid}", self.write(
                "create-" + rid, {"title": rid, "objective": "失败"}))[0], 201)
            self.run_of(rid, rid + "-bad", "跨机复现", failed_at, "failed")
            self.run_of(rid, rid + "-good", "跨机复现", succeeded_at, "succeeded")
            self.run_of(rid, rid + "-other", "别的作业", base + 200, "succeeded")
        with self._browser_page() as page:
            page.goto("http://%s:%d/dash/research?kind=research&id=sr-still" % self.address)
            page.wait_for_selector("#task-failed .failed-line")
            tag = page.locator("#task-failed .failed-line .tag")
            self.assertEqual((tag.text_content(), tag.get_attribute("class")), ("最近一次失败的作业", "tag warn"))
            self.assertIn("跨机复现", page.text_content("#task-failed"))
            page.goto("http://%s:%d/dash/research?kind=research&id=sr-retried" % self.address)
            page.wait_for_selector("#task-thread")
            page.wait_for_function("() => !document.querySelector('#task-failed')")


if __name__ == "__main__":
    unittest.main()
