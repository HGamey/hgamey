"""A dataset's version shows as its name on the datasets page, so the results help and the research skill ask for the
capture date as the version name, not a sequence number like 0."""
import os
import subprocess
import sys
import unittest
from pathlib import Path

CLI = Path(__file__).resolve().parents[1] / "pf-node"
ASK = "dataset.version 用采集日期作版本名"


class DatasetVersionGuideTests(unittest.TestCase):
    def test_help_and_skill_ask_for_a_dated_version(self):
        env = {key: value for key, value in os.environ.items() if not key.startswith("PF_")}
        done = subprocess.run([sys.executable, str(CLI), "results", "--help"], env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(ASK, done.stdout)
        self.assertIn("不要写 0、1 这样的序号", done.stdout)
        skill = (CLI.parent.parent / "skills/hgamey-research/SKILL.md").read_text()
        line = next(line for line in skill.splitlines() if line.startswith("采到数据集时"))
        self.assertIn("`dataset.version` 用采集日期作版本名", line)


if __name__ == "__main__":
    unittest.main()
