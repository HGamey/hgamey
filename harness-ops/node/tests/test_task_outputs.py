"""成果: what a task's delivery produced, by kind, at the top of the task page and in full on the report page; the code
commits behind it link to GitHub and open to the files they changed."""
import subprocess
import unittest
from pathlib import Path

import test_core_flow_web_cli as core

JOB = "e1e1e1e1e1e1"


def git(*args, cwd=None):
    return subprocess.run(["git", "-c", "user.email=fixture@example.invalid", "-c", "user.name=fixture", *args],
                          cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class TaskOutputsTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli, web, pf = core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web, core.CoreFlowTests.pf
    write = staticmethod(core.CoreFlowTests.write)

    def fixture(self):
        """The node's experiments mirror holds two commits: the training code an experiment job ran, and the engine
        shipped later, which the report names. The job wrote weights, an engine library, a frame, notes, data and the
        platform's lease receipts. The delivery cites the job and an outside link."""
        source = self.tmp / "experiments"
        (source / "qrisp").mkdir(parents=True)
        (source / "qrisp" / "train.py").write_text("print('train')\n")
        git("init", "-q", "-b", "main", str(source))
        git("add", ".", cwd=source)
        git("commit", "-qm", "Train the small model", cwd=source)
        self.trained = git("rev-parse", "HEAD", cwd=source)
        (source / "doom3-sr" / "realtime").mkdir(parents=True)
        (source / "doom3-sr" / "realtime" / "engine.txt").write_text("engine\n")
        (source / "qrisp" / "train.py").write_text("print('train v2')\n")
        git("add", ".", cwd=source)
        git("commit", "-qm", "Ship the engine to phones", cwd=source)
        self.shipped = git("rev-parse", "HEAD", cwd=source)
        git("clone", "-q", "--bare", str(source), self.jobs.experiment_mirror)

        out = Path(self.jobs.path(JOB, "out"))
        for path, data in (("base.npz", b"w" * 2048), ("data.json", b"{}"), ("notes.md", b"# notes"), ("shots/frame.png", b"\x89PNG"),
                           ("sub/src/libdoom3.so", b"\x7fELF" * 300), ("lease-receipts/r.json", b"{}")):
            (out / path).parent.mkdir(parents=True, exist_ok=True)
            (out / path).write_bytes(data)
        self.jobs._write_meta(JOB, {"id": JOB, "bin": "experiment", "argv": ["qrisp/train.py"], "experiment": {"commit": self.trained},
                                    "research_id": "sr-out", "started": 1, "exit": 0})
        self.assertEqual(self.web("POST", "/research-state/research/sr-out", self.write(
            "create-sr-out", {"title": "小超分", "objective": "成果"}))[0], 201)
        summary = f"## 结论\n权重在 base.npz；引擎已随 {self.shipped[:7]} 装回手机（包 sha 6cc44b53）。作业 {JOB}。"
        code, _, err = self.pf(core.ADMIN, "research", "deliver", "sr-out", "--summary", summary,
                               "--artifact", f"job:{JOB}", "--artifact", "url:https://example.invalid/dataset")
        self.assertEqual(code, 0, err)

    def test_the_delivery_lists_its_files_by_kind_and_the_commits_open_to_what_they_changed(self):
        self.fixture()
        base = "http://%s:%d" % self.address
        with self._browser_page() as page:
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base + "/dash/research?kind=research&id=sr-out")
            page.wait_for_selector("#task-summary .summary-outputs")
            brief = "#task-summary .summary-outputs"
            self.assertEqual(page.eval_on_selector_all(f"{brief} .output-count", "e => e.map(x => x.textContent)"),
                             ["权重 1", "引擎库 1", "代码提交 2", "图片 1", "报告 1", "其他文件 1", "外部链接 1"])
            # Weights and engine libraries by name, each a download of the job's file.
            files = page.eval_on_selector_all(f"{brief} .output-file", "e => e.map(x => [x.querySelector('.output-kind').textContent, x.querySelector('a').textContent, new URL(x.querySelector('a').href).searchParams.get('path')])")
            self.assertEqual(files, [["权重", "base.npz", "base.npz"], ["引擎库", "libdoom3.so", "sub/src/libdoom3.so"]])
            self.assertIn(f"/dash/api/jobs/{JOB}/out", page.get_attribute(f"{brief} .output-file a", "href"))
            # The commit the report names comes first, then the code the job ran; a hash that is no commit is not one.
            commits = page.eval_on_selector_all(f"{brief} .output-commit", "e => e.map(x => [x.querySelector('code').textContent, x.querySelector('.output-subject').textContent, x.querySelector('.tag').textContent, x.querySelector('.output-github').href])")
            self.assertEqual(commits, [[self.shipped[:7], "Ship the engine to phones", "报告里提到", f"https://github.com/HGamey/experiments/commit/{self.shipped}"],
                                       [self.trained[:7], "Train the small model", f"作业 {JOB} 运行的代码", f"https://github.com/HGamey/experiments/commit/{self.trained}"]])
            first = page.locator(f"{brief} .output-commit").first
            self.assertTrue(first.locator(".output-changes").is_hidden())
            self.assertEqual(first.locator(".output-open").text_content(), "改了 2 个文件")
            first.locator("summary").click()
            self.assertEqual(first.locator(".output-changes li").all_text_contents(), ["新增doom3-sr/realtime/engine.txt", "修改qrisp/train.py"])

            # The report page lists every kind in full; the platform's lease receipts are not the agent's work.
            page.goto(base + "/dash/research/report?id=sr-out#outputs")
            page.wait_for_selector("#outputs .output-tally")
            self.assertEqual(page.eval_on_selector_all("#outputs h3", "e => e.map(x => x.textContent)"),
                             ["权重 · 1", "引擎库 · 1", "代码提交 · 2", "图片 · 1", "报告 · 1", "其他文件 · 1", "外部链接 · 1"])
            paths = page.eval_on_selector_all("#outputs .output-file a:not(.output-job)", "e => e.map(x => x.title || x.textContent)")
            self.assertEqual(paths, ["base.npz", "sub/src/libdoom3.so", "shots/frame.png", "notes.md", "data.json"])
            self.assertEqual(page.locator('#outputs a[href="https://example.invalid/dataset"]').count(), 1)
            self.assertEqual(page.get_attribute("#outputs .output-job", "href").split("#")[1], f"job={JOB}")
            self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
