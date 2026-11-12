"""A card's name on the 结构 page is cut to fit its card (huawei-Think…); the whole name is on hover, for computers,
phones and agents alike."""
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path
import test_structure_page as structure


class StructureTitleTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli = core.CoreFlowTests.request, core.CoreFlowTests.cli
    fixture, open = structure.StructurePageTests.fixture, structure.StructurePageTests.open

    def test_every_card_name_shows_whole_on_hover(self):
        self.fixture()
        self.jobs.directory.join({"invite": self.jobs.directory.invite()["code"], "name": "huawei-thinkcentre-m920t-n000-lab",
                                  "node_id": "d" * 32, "tags": []})
        with self._browser_page() as page:
            self.open(page, 1280)
            page.wait_for_selector('.st-card[data-id="a:agent-b"]')
            names = page.eval_on_selector_all(".st-card .st-title", "items => items.map(i => [i.textContent, i.title])")
            self.assertGreaterEqual(len(names), 7, names)
            self.assertEqual([title for _, title in names], [text for text, _ in names])
            long = page.locator('.st-card[data-id="n:' + "d" * 32 + '"] .st-title')
            self.assertEqual(long.get_attribute("title"), "huawei-thinkcentre-m920t-n000-lab")
            self.assertTrue(long.evaluate("e => e.scrollWidth > e.clientWidth"), "the name is cut to fit")


if __name__ == "__main__":
    unittest.main()
