"""The password that unlocks writes is called 操作口令 everywhere a page shows it, and every box asking for it says
向管理员要 until something is typed. Only the shown words changed: the API's codes and the cookie keep their names."""
import http.client
import json
import re
import unittest
from pathlib import Path

import test_core_flow_web_cli as core   # puts the node directory on sys.path

STATIC = Path(core.__file__).resolve().parent.parent / "dash_static"
COMMENT = re.compile(r"^\s*(//|/\*|\*)")


class OpPasswordWordingTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli, web, pf = core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web, core.CoreFlowTests.pf
    write = staticmethod(core.CoreFlowTests.write)

    def test_pages_say_操作口令_and_boxes_say_whom_to_ask(self):
        old = [f"{path.name}:{n}" for path in sorted(STATIC.iterdir()) if path.suffix in (".html", ".js")
               for n, line in enumerate(path.read_text().split("\n"), 1)
               if not COMMENT.match(line) and ("掌舵" in line or "操作密码" in line)]
        self.assertEqual(old, [])
        boxes = [(path.name, tag) for path in sorted(STATIC.glob("*.html"))
                 for tag in re.findall(r'<input[^>]*type="password"[^>]*>', path.read_text())]
        self.assertGreaterEqual(len(boxes), 10)
        for name, tag in boxes:
            self.assertRegex(tag, r'placeholder="向管理员要', name)

        with self._browser_page() as page:
            page.goto("http://%s:%d/dash" % self.address)
            page.evaluate("() => document.getElementById('dlg-unlock').showModal()")
            self.assertEqual(page.text_content('#dlg-unlock label[for="steer-pw"]'), "操作口令")
            self.assertEqual(page.get_attribute("#steer-pw", "placeholder"), "向管理员要")

        connection = http.client.HTTPConnection(*self.address, timeout=10)
        connection.request("GET", "/dash/api/center-export", headers={"Cookie": self.view_cookie})
        response = connection.getresponse()
        self.assertEqual((response.status, json.loads(response.read())),
                         (403, {"code": "steer_required", "error": "需要操作口令", "need_steer": True}))
        connection.close()


if __name__ == "__main__":
    unittest.main()
