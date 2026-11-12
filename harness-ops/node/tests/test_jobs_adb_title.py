"""An adb job without a label reads on the 作业 page as what it is: 在 <phone> 上执行 adb 命令, the phone by its brand and
model where the platform knows its serial. The raw command is then a technical detail. A job with a label keeps it."""
import re
import unittest
from pathlib import Path

import test_core_flow_web_cli as core   # puts the node directory on sys.path
from test_dataset_plain import PHONES

JOBS = {"a1a1a1a1a1a1": ("adb", ["-s", "R5CT10ABCDE", "shell", "am", "force-stop", "com.quark.browser"], None),
        "b2b2b2b2b2b2": ("adb", ["-s", "UNKNOWN0001X", "shell", "getprop", "ro.product.model"], None),
        "c3c3c3c3c3c3": ("adb", ["devices", "-l"], None),
        "d4d4d4d4d4d4": ("adb", ["-s", "R5CT10ABCDE", "shell", "true"], "打开游戏：截图")}


class JobsAdbTitleTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli = core.CoreFlowTests.request, core.CoreFlowTests.cli

    def test_an_unlabeled_adb_job_names_its_phone(self):
        for started, (jid, (binary, argv, label)) in enumerate(JOBS.items(), 1):
            Path(self.jobs.path(jid, "out")).mkdir(parents=True)
            self.jobs._write_meta(jid, {"id": jid, "bin": binary, "argv": argv, "started": started, "exit": 0, **({"label": label} if label else {})})
        with self._browser_page() as page:
            page.route(re.compile(r".*/dash/api/phones(\?.*)?$"), lambda route: route.fulfill(json=PHONES))
            page.goto("http://%s:%d/dash/jobs" % self.address)
            item = lambda jid: f'#task-list button[data-job="{jid}"]'   # noqa: E731
            page.wait_for_function("s => document.querySelector(s)?.textContent.includes('samsung')", arg=item("a1a1a1a1a1a1") + " .task-item-title")
            titles = {jid: page.text_content(item(jid) + " .task-item-title") for jid in JOBS}
            self.assertEqual(titles, {"a1a1a1a1a1a1": "在 samsung SM-S9180 上执行 adb 命令", "b2b2b2b2b2b2": "在手机上执行 adb 命令",
                                      "c3c3c3c3c3c3": "执行 adb 命令", "d4d4d4d4d4d4": "打开游戏：截图"})
            self.assertTrue(page.is_hidden(item("a1a1a1a1a1a1") + " .task-item-command"))
            self.assertIn("force-stop com.quark.browser", page.text_content(item("a1a1a1a1a1a1") + " .task-item-command"), "folded, not removed")
            self.assertTrue(page.is_visible(item("d4d4d4d4d4d4") + " .task-item-command"), "a labelled job shows its command as before")
            page.click(item("a1a1a1a1a1a1"))
            page.wait_for_function("() => document.getElementById('job-title').textContent === '在 samsung SM-S9180 上执行 adb 命令'")


if __name__ == "__main__":
    unittest.main()
