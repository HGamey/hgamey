"""The comparison at the top of a task is a judgement a person gave on the page, told in names a person reads: the
names the agent gave its sources (research compare --label), else short file names; the files and runs behind them
are technical details. An answer sent with a token names nobody: it stays with its comparison, marked as such, and is
not the task's key comparison."""
import json
import unittest
from pathlib import Path

import test_core_flow_web_cli as core

LABELS = {"native": "原生", "ours": "自研超分", "gsr": "GSR", "bilinear": "双线性"}


class KeyComparisonTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli, web, pf = core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web, core.CoreFlowTests.pf
    write = staticmethod(core.CoreFlowTests.write)

    def compare(self, token, title, labelled):
        output = Path(self.jobs.path(self.jid, "out"))
        output.mkdir(parents=True, exist_ok=True)
        args = ["research", "compare", "sr-key", "--title", title, "--question", "从好到差排列"]
        for name in LABELS:
            (output / f"{name}.png").write_bytes(b"\x89PNG\r\n\x1a\n" + name.encode())
            args += ["--item", f"job:{self.jid}:{name}.png"] + (["--label", LABELS[name]] if labelled else [])
        code, out, err = self.pf(token, *args)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def answer(self, send, comparison, fid):
        assessment = {"schema": "phonefarm/compare-feedback@1", "picked": ["C", "A", "D", "B"], "unable": False,
                      "gaps": ["small", "moderate", "large"], "difference": None,
                      "client_display": {slot: "decoded" for slot in "ABCD"}, "identity_revealed": False}
        self.assertEqual(send("POST", f"/research-state/feedback/{fid}", self.write("answer-" + fid, {
            "request_id": comparison["request_id"], "request_revision": 1, "text": "C、A、D、B", "evaluation": assessment}))[0], 201)

    def test_the_key_comparison_is_a_persons_and_reads_in_names(self):
        self.assertEqual(self.web("POST", "/research-state/research/sr-key", self.write(
            "create-sr-key", {"title": "关键对比", "objective": "人评"}))[0], 201)
        self.assertEqual(self.cli("POST", "/agents", {"name": "agent-a"})[0], 201)
        token = self.cli("POST", "/agents/agent-a/token", {})[1]["token"]
        self.assertEqual(self.pf(token, "research", "claim", "sr-key")[0], 0)
        code, _, err = self.pf(token, "research", "compare", "sr-key", "--item", f"job:{self.jid}:a.png", "--item", f"job:{self.jid}:b.png",
                               "--label", "只有一个", "--title", "t", "--question", "q")
        self.assertNotEqual(code, 0)
        self.assertIn("--label 要和 --item 一样多", err)

        person = self.compare(token, "人评的排序", labelled=True)
        self.answer(self.web, person, "f-person")
        token_sent = self.compare(token, "用口令交的排序", labelled=False)   # newer, but nobody is behind it
        self.answer(self.cli, token_sent, "f-token")
        names = {slot: LABELS[Path(source.split(":")[-1]).stem] for slot, source in person["slots"].items()}

        with self._browser_page() as page:
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto("http://%s:%d/dash/research?kind=research&id=sr-key" % self.address)
            page.wait_for_function("() => [...document.querySelectorAll('.summary-chip')].length === 4 "
                                   "&& [...document.querySelectorAll('.summary-chip')].every(c => c.querySelector('span'))")
            self.assertEqual(page.text_content(".summary-compare .summary-title"), "人评的排序")
            chips = page.eval_on_selector_all(".summary-chip", "cs => cs.map(c => [c.querySelector('b').textContent, c.querySelector('span').textContent])")
            self.assertEqual(chips, [[s, names[s]] for s in "CADB"])

            # Each card of the person's comparison says its name; the file and run are folded away.
            block = f"#cmp-{person['request_id']}"
            page.wait_for_selector(block + " .compare-source-name")
            shown = page.eval_on_selector_all(block + " .compare-item", "cs => cs.map(c => [c.dataset.slot, c.querySelector('.compare-source-name').textContent])")
            self.assertEqual(sorted(shown), sorted([s, names[s]] for s in "ABCD"))
            self.assertTrue(page.locator(block + " .compare-source a.tech").first.is_hidden())
            self.assertEqual(page.locator(block + " .compare-verdict .tag").count(), 0)

            # The token's answer is marked where it is, with short file names for lack of labels.
            other = f"#cmp-{token_sent['request_id']}"
            page.wait_for_selector(other + " .compare-source-name")
            self.assertEqual(page.text_content(other + " .compare-verdict .tag"), "评判人不明")
            self.assertEqual(sorted(page.eval_on_selector_all(other + " .compare-source-name", "e => e.map(x => x.textContent)")),
                             sorted(LABELS))
            self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
