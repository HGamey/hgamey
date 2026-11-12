"""The Agent page says who each agent is, under its name: the same line as its card on the home page (the first clause
of its note, the whole note on hover). An agent registered without a note shows its name alone."""
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path

NOTE = "Claude，HGamey 技术负责人；平台维护和部署"


class AgentIdentityTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli = core.CoreFlowTests.request, core.CoreFlowTests.cli

    def test_each_agent_row_says_who_it_is(self):
        self.assertEqual(self.cli("POST", "/agents", {"name": "lead", "note": NOTE})[0], 201)
        self.assertEqual(self.cli("POST", "/agents", {"name": "plain"})[0], 201)
        with self._browser_page() as page:
            page.goto("http://%s:%d/dash/research#agents-directory" % self.address)
            page.wait_for_selector('#agents-list tr[data-agent-row="plain"]')
            who = page.locator('#agents-list tr[data-agent-row="lead"] .agent-role')
            self.assertEqual((who.inner_text(), who.get_attribute("title")), ("Claude，HGamey 技术负责人", NOTE))
            self.assertTrue(who.is_visible())
            self.assertEqual(page.locator('#agents-list tr[data-agent-row="plain"] .agent-role').count(), 0)
            # The name is still the button that opens its details.
            self.assertEqual(page.locator('#agents-list tr[data-agent-row="lead"] td').first.locator("button.agent-name").inner_text(), "lead")


if __name__ == "__main__":
    unittest.main()
