"""The Agent page names an agent's zone in plain words (公网, not zone:公网); its other tags (glm, dsh: the model's maker,
how it runs) are technical details, shown with 显示技术细节."""
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path


class AgentTagTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli = core.CoreFlowTests.request, core.CoreFlowTests.cli

    def test_zones_read_as_words_and_other_tags_are_technical(self):
        for name, tags in (("glm", ["glm", "dsh", "zone:公网"]), ("blue", ["zone:蓝区"]), ("bare", []), ("odd", ["codex"])):
            self.assertEqual(self.cli("POST", "/agents", {"name": name, "tags": tags})[0], 201)
        with self._browser_page() as page:
            page.goto("http://%s:%d/dash/research#agents-directory" % self.address)
            page.wait_for_selector('#agents-list tr[data-agent-row="odd"]')
            cell = lambda name: page.locator(f'#agents-list tr[data-agent-row="{name}"] td').nth(1)   # noqa: E731
            shown = lambda name: [t for t in cell(name).locator(".tag").all() if t.is_visible()]   # noqa: E731
            self.assertEqual([t.inner_text() for t in shown("glm")], ["公网"])
            self.assertEqual([t.inner_text() for t in shown("blue")], ["蓝区"])
            self.assertEqual((cell("bare").inner_text().strip(), shown("bare")), ("–", []))
            self.assertEqual((cell("odd").inner_text().strip(), shown("odd")), ("–", []))
            self.assertNotIn("zone:", page.inner_text("#agents-list"))
            page.click(".shell-more-btn")
            page.click("#shell-tech")
            self.assertEqual([t.inner_text() for t in shown("glm")], ["公网", "glm", "dsh"])
            self.assertEqual([t.inner_text() for t in shown("odd")], ["codex"])


if __name__ == "__main__":
    unittest.main()
