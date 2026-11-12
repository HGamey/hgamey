"""The 设备 page's 本节点设备登记 shows each registered phone by its name and whether it is enabled. What a
registration is, how it was read (node, revision, times) and each phone's device ID and aliases are technical
details; an error reading it stays in view."""
import re
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path

REGISTRY = {"schema": "phonefarm/device-registry@1", "revision": 3, "updated_at": "2026-11-10T15:13:23Z",
            "identity_basis": "operator_declared", "physical_identity_verified": False, "execution_scope": "global",
            "devices": [{"device_id": "HONOR", "label": "HONOR AAP-AN00", "aliases": ["AVXB6R5C19000481"], "enabled": True}]}


class FleetRegistryTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page

    def open(self, page, registry):
        def identity(route):   # this node can read its device registry
            response = route.fetch()
            value = response.json()
            value["capabilities"] = sorted(set(value.get("capabilities") or []) | {"devices.registry-v1"})
            route.fulfill(response=response, json=value)
        page.route(re.compile(r".*/dash/api/node(\?.*)?$"), identity)
        page.route(re.compile(r".*/dash/api/node/devices(\?.*)?$"), registry)
        page.goto("http://%s:%d/dash/fleet" % self.address)

    def test_a_registration_reads_as_its_phones(self):
        with self._browser_page() as page:
            self.open(page, lambda route: route.fulfill(json={**REGISTRY, "node_id": self.node_id}))
            page.wait_for_selector("#device-registry-list .device-registry-item")
            card = page.inner_text("#device-registry")
            self.assertIn("HONOR AAP-AN00", card)
            self.assertIn("登记启用", card)
            for hidden in ("execution_scope", "device_id", "AVXB6R5C19000481", "revision", self.node_id):
                self.assertNotIn(hidden, card)
            page.click(".shell-more-btn")
            page.click("#shell-tech")
            card = page.inner_text("#device-registry")
            for shown in ("execution_scope", "device_id · HONOR", "AVXB6R5C19000481", "revision 3"):
                self.assertIn(shown, card)

    def test_an_error_reading_it_stays_in_view(self):
        with self._browser_page() as page:
            self.open(page, lambda route: route.fulfill(status=503, json={"error": "登记暂不可读"}))
            page.wait_for_function("() => document.getElementById('device-registry-message').textContent.includes('登记暂不可读')")
            self.assertTrue(page.is_visible("#device-registry-message"))


if __name__ == "__main__":
    unittest.main()
