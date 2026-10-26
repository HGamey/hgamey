"""Overlay deployment: atomic cursor switch, smoke gate, rollback and retention."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

NODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NODE))
import deploy_overlay


def run(args):
    process = subprocess.run([sys.executable, str(NODE / "deploy_overlay.py"), *args],
                             capture_output=True, text=True, timeout=30)
    return process.returncode, (json.loads(process.stdout) if process.returncode == 0
                                else json.loads(process.stderr))


class DeployOverlayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "node"
        # A plausible existing installation plus unrelated sibling data.
        self.install = self.root / "harness-ops"
        (self.install / "node").mkdir(parents=True)
        (self.install / "node" / "node_service.py").write_text("print('service v1')\n")
        (self.install / "node" / "helper.py").write_text("VALUE = 1\n")
        (self.install / "README.md").write_text("readme v1\n")
        (self.root / "phonefarm").mkdir()
        (self.root / "phonefarm" / "phonefarm").write_text("#!/bin/sh\n")

    def stage(self, version):
        staged = Path(self.temp.name) / f"stage-{version}"
        (staged / "node").mkdir(parents=True)
        (staged / "node" / "node_service.py").write_text(f"print('service {version}')\n")
        (staged / "node" / "helper.py").write_text(f"VALUE = {version}\n")
        (staged / "README.md").write_text(f"readme {version}\n")
        return staged

    def test_activate_switches_atomically_rolls_back_and_prunes(self):
        code, out = run(["activate", "--root", str(self.root), "--source", str(self.stage(2))])
        self.assertEqual((code, out["status"]), (0, "active"))
        self.assertTrue(str(out["previous"]).startswith("baseline-"),
                        "first adoption keeps the original directory as the baseline release")
        cursor = self.root / "harness-ops"
        self.assertTrue(cursor.is_symlink())
        self.assertEqual((cursor / "node" / "helper.py").read_text(), "VALUE = 2\n")
        self.assertEqual((self.root / "phonefarm" / "phonefarm").read_text(), "#!/bin/sh\n",
                         "sibling components are never touched")
        second_run = self.active_run()
        code, out = run(["activate", "--root", str(self.root), "--source", str(self.stage(3))])
        self.assertEqual(out["previous"], second_run)
        self.assertEqual((cursor / "node" / "helper.py").read_text(), "VALUE = 3\n")
        code, out = run(["rollback", "--root", str(self.root)])
        self.assertEqual(code, 0)
        self.assertEqual((cursor / "node" / "helper.py").read_text(), "VALUE = 2\n")
        code, out = run(["rollback", "--root", str(self.root)])
        self.assertEqual(code, 0, out)
        self.assertEqual((self.root / "harness-ops" / "node" / "helper.py").read_text(), "VALUE = 1\n",
                         "chain rollback reaches the baseline release")
        code, error = run(["rollback", "--root", str(self.root)])
        self.assertEqual((code, error["error"]["status"]), (1, 404),
                         "baseline has no previous; rollback is refused instead of guessing")

    def active_run(self):
        return os.readlink(self.root / "harness-ops").split("/")[1]

    def test_smoke_failure_and_check_only_never_switch(self):
        plain_before = (self.root / "harness-ops").is_symlink()
        broken = self.stage(4)
        (broken / "node" / "helper.py").write_text("VALUE = (2\n")
        code, error = run(["activate", "--root", str(self.root), "--source", str(broken)])
        self.assertEqual((code, error["error"]["status"]), (1, 400))
        self.assertIn("smoke compile failed", error["error"]["message"])
        self.assertEqual((self.root / "harness-ops").is_symlink(), plain_before,
                         "cursor state is unchanged on failure")
        self.assertEqual((self.root / "harness-ops" / "node" / "helper.py").read_text(), "VALUE = 1\n")
        code, out = run(["activate", "--root", str(self.root), "--source", str(self.stage(5)),
                         "--check-only"])
        self.assertEqual((code, out["status"]), (0, "checked"))
        self.assertEqual((self.root / "harness-ops" / "node" / "helper.py").read_text(), "VALUE = 1\n",
                         "failed smoke and check-only must not change the installation")

    def test_non_harness_source_is_rejected(self):
        empty = Path(self.temp.name) / "not-harness"
        empty.mkdir()
        code, error = run(["activate", "--root", str(self.root), "--source", str(empty)])
        self.assertEqual((code, error["error"]["status"]), (1, 400))


if __name__ == "__main__":
    unittest.main()
