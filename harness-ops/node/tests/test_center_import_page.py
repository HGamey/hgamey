"""The 数据导入 page (更多 → 数据导入): two ways to bring data into this center.

A) 结果 / 数据集 registers a result manifest or imports a small report straight into the running center — the same
   POST /results and /result-imports agents use, no stop needed. B) 整条任务 uploads a 数据导出 zip and previews the
   import plan (dry-run: nothing is written); applying a whole task backs up and replaces the center's databases, so
   it needs the center stopped and the page only prints the exact command. Everything here needs the steer password."""
import collections
import http.client
import json
import os
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path
import test_center_export_import as exports

MAX_SOURCE = 1 << 20
MANIFEST = {"schema": "phonefarm/result@1", "result_id": "live-reg-1", "title": "直接登记的结果", "job_id": None,
            "labels": {"research_id": "sr-live"}, "origin": {"evidence_origin": "synthetic"}, "outcome": "unknown",
            "metrics": [], "sources": [], "claims": ["smoke"], "limitations": ["fixture"]}


def upload(center, path, data, steer=True, hg=True, ctype="application/zip"):
    """A raw body POST as HGTarget.fetch sends it from the page: view cookie, optional steer unlock, X-HG."""
    connection = http.client.HTTPConnection(*center.address, timeout=30)
    headers = {"Cookie": center.view_cookie + (f"; {center.steer_cookie}" if steer else ""),
               "X-Expected-Node-ID": center.node_id, "Content-Type": ctype, "Content-Length": str(len(data))}
    if hg:
        headers["X-HG"] = "1"
    connection.request("POST", path, body=data, headers=headers)
    response = connection.getresponse()
    raw = response.read()
    connection.close()
    return response.status, json.loads(raw) if raw else None


class CenterImportPageTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli, web = core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web
    write = staticmethod(core.CoreFlowTests.write)
    fixture, job, result, export = (exports.CenterExportImportTests.fixture, exports.CenterExportImportTests.job,
                                    exports.CenterExportImportTests.result, exports.CenterExportImportTests.export)

    # ── B) whole task: upload a zip, preview the plan, write nothing ──
    def test_whole_task_preview_plans_new_data_without_writing(self):
        self.fixture()
        path, _ = self.export({"tasks": exports.TASKS, "unassigned": True})
        center = exports.Center(self, self.tmp / "target")   # an empty center, running, served like ours
        before = exports.snapshot(center.root)
        code, report = upload(center, "/dash/api/center-import", path.read_bytes())
        self.assertEqual(code, 200, report)
        self.assertEqual(report["applied"], False)
        self.assertEqual(report["target"]["data_root"], os.path.realpath(center.root))
        self.assertEqual([t["id"] for t in report["tasks"]], exports.TASKS)
        self.assertEqual(report["new"]["作业目录"], 5)
        self.assertEqual(report["conflicts"], [])
        self.assertIn("agent 账号和 token", report["excluded"])   # the data the export leaves out
        self.assertIsNone(report["zip"])   # the server's temporary path is never exposed
        self.assertEqual(exports.snapshot(center.root), before, "a preview writes nothing to the data root")

    def test_whole_task_preview_needs_the_steer_password(self):
        self.fixture()
        path, _ = self.export({"tasks": ["sr-c"]})
        center = exports.Center(self, self.tmp / "guarded")
        before = exports.snapshot(center.root)
        self.assertEqual(upload(center, "/dash/api/center-import", path.read_bytes(), steer=False)[0], 403)
        self.assertEqual(upload(center, "/dash/api/center-import", path.read_bytes(), hg=False)[0], 400)
        self.assertEqual(center.web("GET", "/center-import", steer=False)[0], 403)   # the target info is steered too
        code, info = center.web("GET", "/center-import")
        self.assertEqual((code, info["data_root"]), (200, os.path.realpath(center.root)))
        self.assertEqual(exports.snapshot(center.root), before, "a rejected or read request writes nothing")

    def test_a_file_that_is_not_an_export_is_refused_as_a_preview(self):
        center = exports.Center(self, self.tmp / "nonzip")
        before = exports.snapshot(center.root)
        code, report = upload(center, "/dash/api/center-import", b"this is not a zip")
        self.assertEqual((code, report["code"]), (400, "import_refused"))
        self.assertEqual(exports.snapshot(center.root), before)

    # ── A) results / datasets: live register and import into the running center ──
    def test_a_result_registers_live_and_is_found(self):
        self.assertEqual(self.web("POST", "/results", MANIFEST, steer=False)[0], 403)
        self.assertEqual(self.web("POST", "/results", MANIFEST, hg=False)[0], 400)
        code, value = self.web("POST", "/results", MANIFEST)
        self.assertEqual((code, value["result_id"]), (201, "live-reg-1"))
        listed = [item["result_id"] for item in self.web("GET", "/results")[1]["items"]]
        self.assertIn("live-reg-1", listed)

    def test_register_and_import_over_the_limit_are_refused(self):
        oversize = {**MANIFEST, "claims": ["x" * 300000]}   # the serialized manifest is well over 256 KiB
        self.assertEqual(self.web("POST", "/results", oversize)[0], 413)
        # A small report whose content is over 1 MiB is refused before the adapter runs.
        over = {"schema": "phonefarm/result-import@1", "import_id": "imp-big", "adapter": "gol-optimize-v1",
                "filename": "report.json", "content": "x" * (MAX_SOURCE + 1), "labels": {}}
        self.assertEqual(self.web("POST", "/result-imports", over)[0], 413)

    # ── the page itself: nav entry, unlock, then both ways through the UI ──
    def open(self, page, steer=True):
        base = "http://%s:%d" % self.address
        if steer:
            name, value = self.steer_cookie.split("=", 1)
            page.context.add_cookies([{"name": name, "value": value, "url": base}])
        page.goto(base + "/dash/import")

    def test_the_page_unlocks_registers_and_previews(self):
        self.fixture()
        self.board.fails, self.board._lan_plain = collections.defaultdict(list), True
        path, _ = self.export({"tasks": exports.TASKS, "unassigned": True})
        with self._browser_page() as page:
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            self.open(page, steer=False)
            page.wait_for_selector("#unlock:not([hidden])")
            self.assertIn("数据导入", page.eval_on_selector_all("#shell-more-menu a", "links => links.map(a => a.textContent)"))
            page.fill("#unlock-pw", "wrong")
            page.click("#unlock button[type=submit]")
            page.wait_for_function("() => /解锁失败/.test(document.getElementById('unlock-msg').textContent)")
            page.fill("#unlock-pw", core.STEER)
            page.click("#unlock button[type=submit]")
            page.wait_for_selector("#content:not([hidden])")

            # A) register a result straight into the running center.
            page.fill("#manifest-input", json.dumps(MANIFEST, ensure_ascii=False))
            page.click("#register-submit")
            page.wait_for_selector("#register-links a")
            self.assertIn("已登记", page.text_content("#register-message"))
            self.assertIn("live-reg-1", [item["result_id"] for item in self.web("GET", "/results")[1]["items"]])

            # B) upload an export zip and preview the plan; nothing is applied, the apply command is printed.
            page.click("#tab-package")
            page.set_input_files("#zip-file", str(path))
            page.click("#preview-submit")
            page.wait_for_selector(".im-stop .im-cmd")
            command = page.text_content(".im-stop .im-cmd")
            self.assertIn("center_import.py", command)
            self.assertIn("--apply", command)
            self.assertIn("sr-a", page.text_content("#plan"))
            self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
