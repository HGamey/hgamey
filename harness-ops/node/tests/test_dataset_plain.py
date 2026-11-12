"""The datasets page in a person's words: each dataset says what it is for (dataset.purpose), and the device column
reads as a device, phone serials and commits folded into the technical details; a serial a phone on this center has
becomes its model. Registering tells the agent when the device label carries such codes."""
import json
import unittest

import test_core_flow_web_cli as core

PHONES = {"items": [{"ref": "x/phone", "serial": "R5CT10ABCDE", "device_id": "phone", "aliases": ["R5CT10ABCDE"], "model": "SM-S9180",
                     "observation": {"serial": "R5CT10ABCDE", "brand": "samsung", "model": "SM-S9180"}}]}


class DatasetPlainTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli, web, pf = core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web, core.CoreFlowTests.pf

    def body(self, rid, name, device, purpose=None):
        dataset = {"name": name, "version": "v1", "pairing": "simulated", "capture_method": "screencap", "frames": 10,
                   "scenes": 1, "splits": None, "size_bytes": 1000, "location": None}
        if purpose:
            dataset["purpose"] = purpose
        return {"schema": "phonefarm/result@1", "result_id": rid, "title": rid, "labels": {"device": device},
                "origin": {"evidence_origin": "synthetic"}, "limitations": ["fixture"], "sources": [], "dataset": dataset}

    def test_datasets_say_what_they_are_for_and_name_devices_plainly(self):
        path = self.tmp / "ds.json"
        path.write_text(json.dumps(self.body("ds-gi", "gi-sim", "REDMAGIC NX809J / 91253241019A / Android 16 / root",
                                             "红魔上录的原神画面，用来训练超分")))
        code, _, err = self.pf(core.ADMIN, "results", "register", str(path))
        self.assertEqual(code, 0, err)
        self.assertIn("labels.device 里有 91253241019A，像串号或提交号；设备只写型号", err)
        self.assertNotIn("purpose", err)
        path.write_text(json.dumps(self.body("ds-s9", "s9-crops", "R5CT10ABCDE")))
        code, _, err = self.pf(core.ADMIN, "results", "register", str(path))
        self.assertEqual(code, 0, err)
        self.assertIn("dataset.purpose 没写；写一句人能看懂的用途", err)
        self.assertEqual(self.cli("POST", "/results", self.body("ds-doom", "doom-history", "Historical OnePlus PKG110 c531e0a; import on kamico Mac"))[0], 201)

        with self._browser_page() as page:
            page.route("**/dash/api/phones", lambda route: route.fulfill(json=PHONES))
            page.goto("http://%s:%d/dash/datasets" % self.address)
            page.wait_for_selector('tr[data-result="ds-s9"]')
            devices = {rid: page.inner_text(f'tr[data-result="{rid}"] td:nth-child(4)') for rid in ("ds-gi", "ds-doom", "ds-s9")}
            self.assertEqual(devices, {"ds-gi": "REDMAGIC NX809J / Android 16 / root",
                                       "ds-doom": "Historical OnePlus PKG110; import on kamico Mac", "ds-s9": "samsung SM-S9180"})
            self.assertEqual(page.text_content('tr[data-result="ds-gi"] td:nth-child(4)'),
                             "REDMAGIC NX809J / 91253241019A / Android 16 / root", "folded, not removed")
            name = page.locator(".ds-name", has_text="gi-sim")
            self.assertEqual(name.locator(".ds-purpose").text_content(), "红魔上录的原神画面，用来训练超分")
            self.assertEqual(page.locator(".ds-name", has_text="s9-crops").locator(".ds-purpose").text_content(), "模拟低清配对，10 帧",
                             "one that does not say is described from its fields")


if __name__ == "__main__":
    unittest.main()
