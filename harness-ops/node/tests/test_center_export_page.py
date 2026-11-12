"""The 数据导出 page (更多 → 数据导出): unlock first; tasks are picked whole, a follow-on chain at once, and the ones
still running jobs say so when picked; the export is written on the center with its progress shown, then downloaded
or deleted."""
import collections
import io
import json
import unittest
import zipfile

import test_core_flow_web_cli as core   # puts the node directory on sys.path
import test_center_export_import as exports


class CenterExportPageTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli, web = core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web
    write = staticmethod(core.CoreFlowTests.write)
    fixture, job, result = (exports.CenterExportImportTests.fixture, exports.CenterExportImportTests.job,
                            exports.CenterExportImportTests.result)

    def open(self, page, steer=True):
        base = "http://%s:%d" % self.address
        if steer:
            name, value = self.steer_cookie.split("=", 1)
            page.context.add_cookies([{"name": name, "value": value, "url": base}])
        page.goto(base + "/dash/export")

    def picked(self, page):
        return sorted(page.eval_on_selector_all("#tasks input[data-pick]:checked", "boxes => boxes.map(b => b.dataset.pick)"))

    def test_without_the_steer_password_it_asks_to_unlock_first(self):
        self.fixture()
        self.board.fails, self.board._lan_plain = collections.defaultdict(list), True
        with self._browser_page() as page:
            self.open(page, steer=False)
            page.wait_for_selector("#unlock:not([hidden])")
            self.assertTrue(page.is_hidden("#content"))
            self.assertIn("先解锁掌舵权限", page.text_content("#message"))
            page.fill("#unlock-pw", "wrong")
            page.click("#unlock button[type=submit]")
            page.wait_for_function("() => /解锁失败/.test(document.getElementById('unlock-msg').textContent)")
            page.fill("#unlock-pw", core.STEER)
            page.click("#unlock button[type=submit]")
            page.wait_for_selector('#tasks input[data-pick="sr-a"]')
            self.assertTrue(page.is_hidden("#unlock"))

    def test_tasks_are_picked_whole_a_chain_at_once_and_running_ones_warn_before_the_export(self):
        self.fixture()
        with self._browser_page() as page:
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            self.open(page)
            page.wait_for_selector('#tasks input[data-pick="sr-a"]')
            self.assertIn("数据导出", page.eval_on_selector_all("#shell-more-menu a", "links => links.map(a => a.textContent)"))
            self.assertTrue(page.is_disabled("#start"))
            chain = page.locator("#tasks fieldset.ex-chain")
            self.assertEqual(chain.count(), 1)
            self.assertIn("任务链 · 画质对比 · 2 个任务", chain.locator("legend").text_content())
            self.assertIn("作业 1 · 结果 1 · 附件 1", page.text_content('label:has(input[data-pick="sr-a"])'))
            self.assertIn("1 个作业未结束", page.text_content('label:has(input[data-pick="sr-c"])'))

            page.check('#tasks input[data-chain="sr-a"]')
            self.assertEqual(self.picked(page), ["sr-a", "sr-b"])
            self.assertIn("已选 2 个任务", page.text_content("#summary-text"))
            self.assertEqual(page.text_content("#summary-warn"), "", "nothing running in this chain")
            page.uncheck('#tasks input[data-pick="sr-b"]')
            self.assertTrue(page.eval_on_selector('#tasks input[data-chain="sr-a"]', "box => box.indeterminate"))
            page.check('#tasks input[data-pick="sr-b"]')

            # Filtering narrows what is shown; what was picked stays picked.
            page.fill("#f-text", "排队")
            self.assertEqual(page.eval_on_selector_all("#tasks input[data-pick]", "boxes => boxes.map(b => b.dataset.pick)"), ["sr-c", "*unassigned"])
            page.click("#pick-shown")
            page.fill("#f-text", "")
            self.assertEqual(self.picked(page), ["sr-a", "sr-b", "sr-c"])
            warning = page.text_content("#summary-warn")
            self.assertIn("排队中：1 个作业还没结束", warning)
            self.assertIn("导入后它们不会再运行", warning)
            page.check('#tasks input[data-pick="*unassigned"]')
            self.assertIn("已选 3 个任务 和未归属任务的数据", page.text_content("#summary-text"))
            self.assertIn("未归属任务的数据：1 个作业还没结束", page.text_content("#summary-warn"))

            page.click("#start")
            page.wait_for_selector("#exports .ex-file.is-done a[download]")
            self.assertIn("3 个任务 和未归属任务的数据", page.text_content("#exports .ex-file"))
            self.assertIn("导出时还没结束的作业 2 个", page.text_content("#exports .ex-file"))
            href = page.eval_on_selector("#exports .ex-file.is-done a[download]", "a => a.href")
            response = page.request.get(href)
            self.assertEqual(response.status, 200)
            with zipfile.ZipFile(io.BytesIO(response.body())) as archive:
                manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual([t["id"] for t in manifest["tasks"]], ["sr-a", "sr-b", "sr-c"])
            self.assertIsNotNone(manifest["unassigned"])

            # Deleting takes a second click.
            page.click("#exports [data-delete]")
            self.assertEqual(page.text_content("#exports [data-delete]"), "确认删除")
            self.assertEqual(len(self.web("GET", "/center-export")[1]["exports"]), 1)
            page.click("#exports [data-delete]")
            page.wait_for_function("() => /还没有导出文件/.test(document.getElementById('exports').textContent)")
            self.assertEqual(self.web("GET", "/center-export")[1]["exports"], [])
            self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
