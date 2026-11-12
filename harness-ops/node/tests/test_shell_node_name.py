"""The top bar's node button cuts a long computer name short; hovered, it gives the whole name, then the target state."""
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path

LONG = "huawei-ThinkCentre-M920t-N000"


class ShellNodeNameTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli = core.CoreFlowTests.request, core.CoreFlowTests.cli

    def test_the_whole_name_is_on_hover(self):
        def node(route):
            value = route.fetch().json()
            value["host"]["hostname"] = LONG
            route.fulfill(json=value)
        with self._browser_page() as page:
            page.route("**/dash/api/node", node)
            page.set_viewport_size({"width": 390, "height": 844})
            page.goto("http://%s:%d/dash/fleet" % self.address)
            page.wait_for_function("n => document.querySelector('.shell-node-name').textContent === n", arg=LONG)
            button = page.locator(".shell-node")
            self.assertLess(button.evaluate("b => b.querySelector('.shell-node-name').getBoundingClientRect().width"),
                            button.evaluate("b => b.querySelector('.shell-node-name').scrollWidth"), "the button cuts the name short")
            page.wait_for_function("n => document.querySelector('.shell-node').title.startsWith(n + '\\n')", arg=LONG)
            self.assertRegex(button.get_attribute("title"), r"\n执行目标")


if __name__ == "__main__":
    unittest.main()
