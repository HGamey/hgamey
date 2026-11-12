"""Clicking an agent's name on the Agent page brings its details into view: who it is, the tasks it is working on and
what it delivered lately (each a link), then its running jobs and status history."""
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path


class AgentDetailTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli, web, pf = core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web, core.CoreFlowTests.pf
    write = staticmethod(core.CoreFlowTests.write)

    def test_a_click_shows_who_it_is_and_its_work(self):
        tokens = {}
        for name, note in (("worker", "GLM 5.3，外部研究员；在 mbp1 上跑"), ("other", "")):
            self.assertEqual(self.cli("POST", "/agents", {"name": name, "note": note})[0], 201)
            tokens[name] = self.cli("POST", f"/agents/{name}/token", {})[1]["token"]
        for rid, title, owner in (("sr-open", "原神超分：帧率能提高多少", "worker"), ("sr-done", "Doom 超分交付", "worker"),
                                  ("sr-theirs", "别人的任务", "other")):
            self.assertEqual(self.web("POST", f"/research-state/research/{rid}", self.write(
                "create-" + rid, {"title": title, "objective": "详情"}))[0], 201)
            self.assertEqual(self.pf(tokens[owner], "research", "claim", rid)[0], 0)
        code, _, err = self.pf(tokens["worker"], "research", "deliver", "sr-done", "--summary", "## 结论\n做成了。", "--artifact", f"job:{self.jid}")
        self.assertEqual(code, 0, err)
        with self._browser_page() as page:
            page.set_viewport_size({"width": 1280, "height": 720})
            page.goto("http://%s:%d/dash/research#agents-directory" % self.address)
            page.wait_for_selector('#agents-list tr[data-agent-row="other"]')
            page.click('#agents-list .agent-name[data-agent="worker"]')
            page.wait_for_selector("#agent-detail-delivered a")
            top = page.evaluate("document.getElementById('agent-detail').getBoundingClientRect().top")
            self.assertTrue(0 <= top < 200, top)   # in view, not below the list
            self.assertEqual(page.inner_text("#agent-detail-name"), "worker")
            self.assertEqual(page.inner_text("#agent-detail-who"), "GLM 5.3，外部研究员；在 mbp1 上跑")
            owned = page.locator("#agent-detail-owned a")
            self.assertEqual(owned.all_inner_texts(), ["原神超分：帧率能提高多少"])
            self.assertTrue(owned.first.get_attribute("href").startswith("/dash/research?kind=research&id=sr-open"))
            delivered = page.locator("#agent-detail-delivered a")
            self.assertEqual(delivered.all_inner_texts(), ["Doom 超分交付"])
            self.assertTrue(delivered.first.get_attribute("href").startswith("/dash/research/report?id=sr-done"))
            # Another agent's details replace these; one without a note shows no identity line.
            page.evaluate("scrollTo(0, 0)")
            page.click('#agents-list .agent-name[data-agent="other"]')
            page.wait_for_function("() => document.querySelector('#agent-detail-owned a')?.textContent === '别人的任务'")
            self.assertTrue(page.is_hidden("#agent-detail-who"))
            self.assertEqual(page.inner_text("#agent-detail-delivered"), "还没有交付")


if __name__ == "__main__":
    unittest.main()
