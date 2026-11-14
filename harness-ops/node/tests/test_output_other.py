"""成果 counts what an agent produced, not a remote control session's own records (each command it ran and its
output, under gray-session-<pid>-<time>/), which stay in the job's files. The files that are no model, library, picture,
video or report are one folded line at the top of the task, by count and size, and opened by kind of file
(.raw 5 个 · 5 KB). When a job has more files than are read, its counts say 至少."""
import unittest
from pathlib import Path

import test_core_flow_web_cli as core   # puts the node directory on sys.path

BRIEF = "#task-summary .summary-outputs"


class OutputOtherTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli, web, pf = core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web, core.CoreFlowTests.pf
    write = staticmethod(core.CoreFlowTests.write)

    def job(self, jid, rid, files):
        out = Path(self.jobs.path(jid, "out"))
        for path, size in files.items():
            (out / path).parent.mkdir(parents=True, exist_ok=True)
            (out / path).write_bytes(b"x" * size)
        self.jobs._write_meta(jid, {"id": jid, "bin": "custom", "argv": ["dump"], "research_id": rid, "started": 1, "exit": 0})

    def deliver(self, rid, jid):
        self.assertEqual(self.web("POST", f"/research-state/research/{rid}", self.write(f"create-{rid}", {"title": rid, "objective": "转储"}))[0], 201)
        code, _, err = self.pf(core.ADMIN, "research", "deliver", rid, "--summary", "## 结论\n转储好了。", "--artifact", f"job:{jid}")
        self.assertEqual(code, 0, err)

    def test_session_records_are_not_outputs_and_the_rest_is_one_line_by_kind(self):
        session = "fd_native/session/gray-session-81771-1793735372454187000/commands"
        files = {f"{session}/{i:04d}.{stream}": 10 for i in range(40) for stream in ("stdout", "stderr")}
        files.update({f"fd_native/frames/f{i:04d}.raw": 1024 for i in range(5)})
        files.update({"fd_native/dump.tar": 3 << 20, "fd_native/meta.json": 100, "fd_native/sample/a.png": 50})
        self.job("b1b1b1b1b1b1", "sr-dump", files)
        self.deliver("sr-dump", "b1b1b1b1b1b1")
        with self._browser_page() as page:
            page.goto("http://%s:%d/dash/research?kind=research&id=sr-dump" % self.address)
            page.wait_for_selector(f"{BRIEF} .output-group.k-other")
            tally = page.eval_on_selector_all(f"{BRIEF} .output-count", "e => e.map(x => x.textContent)")
            self.assertEqual(tally, ["图片 1", "其他文件 7"])
            self.assertEqual(page.text_content(f"{BRIEF} .output-group.k-other > summary"), "其他文件 · 7 个 · 3.0 MB")
            page.click(f"{BRIEF} .output-group.k-other > summary")
            self.assertEqual(page.eval_on_selector_all(f"{BRIEF} .output-group.k-other li", "e => e.map(x => x.textContent)"),
                             [".tar · 1 个 · 3.0 MB", ".raw · 5 个 · 5 KB", ".json · 1 个 · 100 字节"])

    def test_a_control_sessions_mailbox_is_not_an_output_but_what_its_actions_made_is(self):
        control = "fd_native/session/gray-control"
        files = {"fd_native/session/session-control.json": 300, f"{control}/owner.lock": 1,
                 f"{control}/publisher.lock": 1, f"{control}/publisher.json": 50,
                 f"{control}/artifacts/marker-0.json": 60, f"{control}/artifacts/capture-1.png": 50,
                 f"{control}/quality/q1/quality.json": 70, "fd_native/index.csv": 20}
        files.update({f"{control}/{box}/marker-{i}.json": 40 for box in ("requests", "started", "results") for i in range(3)})
        self.job("d3d3d3d3d3d3", "sr-control", files)
        self.deliver("sr-control", "d3d3d3d3d3d3")
        with self._browser_page() as page:
            page.goto("http://%s:%d/dash/research/report?id=sr-control" % self.address)
            page.wait_for_selector("#outputs h3")
            self.assertEqual(page.eval_on_selector_all("#outputs .output-count", "e => e.map(x => x.textContent)"),
                             ["图片 1", "其他文件 3"])
            listed = page.eval_on_selector_all("#outputs .output-files a[href*='/out?']", "e => e.map(x => new URL(x.href).searchParams.get('path'))")
            self.assertEqual(sorted(set(listed)), sorted([f"{control}/artifacts/capture-1.png", f"{control}/artifacts/marker-0.json",
                                                     f"{control}/quality/q1/quality.json", "fd_native/index.csv"]))

    def test_a_job_with_more_files_than_are_read_counts_at_least(self):
        self.job("c2c2c2c2c2c2", "sr-many", {f"frames/f{i:04d}.raw": 10 for i in range(320)})
        self.deliver("sr-many", "c2c2c2c2c2c2")
        with self._browser_page() as page:
            page.goto("http://%s:%d/dash/research?kind=research&id=sr-many" % self.address)
            page.wait_for_selector(f"{BRIEF} .output-group.k-other")
            self.assertEqual(page.eval_on_selector_all(f"{BRIEF} .output-count", "e => e.map(x => x.textContent)"), ["其他文件 至少 300"])
            self.assertEqual(page.text_content(f"{BRIEF} .output-group.k-other > summary"), "其他文件 · 至少 300 个 · 至少 3 KB")


if __name__ == "__main__":
    unittest.main()
