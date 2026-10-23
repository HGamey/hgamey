"""An explicit PF profile must also work for ordinary process-group jobs."""
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from node_service import Jobs


class SharedProfileTests(unittest.TestCase):
    def test_process_group_output_and_old_private_queue_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            jobs = Jobs(tmp, {"fixture": (sys.executable, tmp)})
            try:
                # Installation/identity validation is covered by the real Linux
                # chain. Here use the current group to exercise normal dispatch.
                jobs.pf_env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
                               "PHONEFARM_SHARED_GID": str(os.getegid()),
                               "PHONEFARM_STATE_DIR": str(Path(tmp) / "state")}
                script = """import json,os,pathlib,stat
out=pathlib.Path(os.environ['PF_JOB_OUT'])
receipt=pathlib.Path(os.environ['PF_JOB_LEASE_DIR'])
value={'directories':[[p.stat().st_gid,stat.S_IMODE(p.stat().st_mode)] for p in (out,receipt)],
       'state_root':os.environ['PHONEFARM_STATE_DIR']}
(out/'observed.json').write_text(json.dumps(value))
"""
                spec = {"bin": "fixture", "argv": ["-c", script], "resources": [],
                        "device_access": "none", "execution_policy": "process_group"}
                value = jobs.start(spec, b"", "shared-profile-current")
                jid = value["id"]
                deadline = time.monotonic() + 8
                while jobs.dispatch.thread is not None and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertIsNone(jobs.dispatch.thread, "dispatch did not finish")
                self.assertEqual(jobs.status(jid)["exit"], 0)
                observed = json.loads(Path(jobs.path(jid, "out", "observed.json")).read_text())
                self.assertEqual(observed["directories"], [[os.getegid(), 0o2770]] * 2)
                self.assertEqual(observed["state_root"], jobs.pf_env["PHONEFARM_STATE_DIR"])

                row, _ = jobs.dispatch.queue.reserve("shared-profile-old", "old-fixture", spec, [])
                jobs._prepare(row, b"")
                old_out = Path(jobs.path(row["job_id"], "out"))
                old_out.chmod(0o700)
                jobs.dispatch.queue.ready(row["job_id"])
                claimed = jobs.dispatch.queue.claim_next(1)
                with patch("job_dispatch.job_runtime.launch") as launch:
                    jobs.dispatch.launch(claimed)
                    launch.assert_not_called()
                failed = jobs.status(row["job_id"])
                self.assertEqual(failed["outcome"], "failed")
                self.assertEqual(failed["execution"]["owned_process_group"]["status"], "not_started")
                self.assertEqual(stat.S_IMODE(old_out.stat().st_mode), 0o700)
                self.assertFalse((old_out / "lease-receipts").exists())
            finally:
                jobs._read_root.close()
