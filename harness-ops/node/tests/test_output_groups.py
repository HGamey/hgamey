"""A delivery's models (and engine libraries) are one folded line at the top of its task page, 训练出的模型 · 9 个
文件: a person sees what was produced, not a row of file names and sizes; the names are a click away."""
import unittest
from pathlib import Path

import test_core_flow_web_cli as core   # puts the node directory on sys.path

JOB = "a7a7a7a7a7a7"
MODELS = ["base.npz", "half_channels.npz", "no_residual.npz", "wide.npz", "deep.npz", "q8.onnx", "q16.onnx", "best.pt", "last.pt"]


class OutputGroupTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli, web, pf = core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web, core.CoreFlowTests.pf
    write = staticmethod(core.CoreFlowTests.write)

    def test_models_are_one_folded_line_with_their_files_inside(self):
        out = Path(self.jobs.path(JOB, "out"))
        for name in MODELS:
            (out / "models" / name).parent.mkdir(parents=True, exist_ok=True)
            (out / "models" / name).write_bytes(b"w" * 1024)
        self.jobs._write_meta(JOB, {"id": JOB, "bin": "custom", "argv": ["train"], "research_id": "sr-models", "started": 1, "exit": 0})
        self.assertEqual(self.web("POST", "/research-state/research/sr-models", self.write(
            "create-sr-models", {"title": "小模型", "objective": "训练"}))[0], 201)
        code, _, err = self.pf(core.ADMIN, "research", "deliver", "sr-models", "--summary", "## 结论\n训练好了。", "--artifact", f"job:{JOB}")
        self.assertEqual(code, 0, err)
        brief = "#task-summary .summary-outputs"
        with self._browser_page() as page:
            page.goto("http://%s:%d/dash/research?kind=research&id=sr-models" % self.address)
            page.wait_for_selector(f"{brief} .output-group")
            self.assertEqual(page.text_content(f"{brief} .output-count"), "模型 9")
            self.assertEqual(page.eval_on_selector_all(f"{brief} .output-group > summary", "e => e.map(x => x.textContent)"), ["训练出的模型 · 9 个文件"])
            self.assertEqual(page.locator(f"{brief} .output-file:visible").count(), 0)
            page.click(f"{brief} .output-group > summary")
            self.assertEqual(sorted(page.eval_on_selector_all(f"{brief} .output-file:visible > a", "e => e.map(x => x.textContent)")), sorted(MODELS))


if __name__ == "__main__":
    unittest.main()
