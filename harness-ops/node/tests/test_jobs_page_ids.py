"""The jobs page must declare every static element id its script references."""
from pathlib import Path
import re
import unittest

NODE = Path(__file__).resolve().parents[1]


class JobsPageIds(unittest.TestCase):
    def test_script_element_ids_exist_in_the_page(self):
        page = (NODE / "dash_static/jobs.html").read_text()
        declared = set(re.findall(r'id="([^"]+)"', page))
        script = (NODE / "dash_static/jobs.js").read_text()
        referenced = set(re.findall(r"\$\('([a-z0-9-]+)'\)", script))
        # Dynamic template ids (built at runtime) are not in this static list.
        missing = sorted(referenced - declared - {"cancel-signals", "job-events", "directory-export-controls"})
        self.assertEqual(missing, [], "jobs.js references ids missing from jobs.html")
        for required in ("pause-job", "resume-job", "pause-result"):
            self.assertIn(required, declared)


if __name__ == "__main__":
    unittest.main()
