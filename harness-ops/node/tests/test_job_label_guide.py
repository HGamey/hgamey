"""A job's label is what the 作业 and 任务 pages show for it, so check-in, pf-node --help and the research skill all ask
for one sentence a person can read on every submission, not just parameters or a command."""
import os
import subprocess
import sys
import unittest
from pathlib import Path

CLI = Path(__file__).resolve().parents[1] / "pf-node"
PLAIN, NOT = "用一句人能看懂的话", "不要只写参数或命令"


class JobLabelGuideTests(unittest.TestCase):
    def test_checkin_help_and_skill_ask_for_a_plain_label(self):
        sys.path.insert(0, str(CLI.parent))
        self.addCleanup(sys.path.remove, str(CLI.parent))
        from agent_duties import LOOP
        step = next(step for step in LOOP if "--label" in step)
        env = {key: value for key, value in os.environ.items() if not key.startswith("PF_")}
        done = subprocess.run([sys.executable, str(CLI), "--help"], env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(done.returncode, 0, done.stderr)
        line = next(line for line in done.stdout.splitlines() if "--label" in line)
        skill = (CLI.parent.parent / "skills/hgamey-research/SKILL.md").read_text()
        submit = next(line for line in skill.splitlines() if line.startswith("4. **提交并继续其他工作。**"))
        for name, text in (("checkin", step), ("help", line), ("skill", submit)):
            self.assertIn(PLAIN, text, name)
            self.assertIn(NOT, text, name)
        self.assertNotIn("SETS=srip sfwait=off", step + submit, "the example is a sentence, not parameters")


if __name__ == "__main__":
    unittest.main()
