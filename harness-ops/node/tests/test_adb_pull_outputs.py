"""Direct adb downloads belong to the task's downloadable output directory."""
from pathlib import Path
import subprocess
import sys
import unittest

import test_core_flow_web_cli as fixture


class AdbPullOutputTests(unittest.TestCase):
    def test_relative_and_default_pull_destinations_are_job_artifacts(self):
        env = fixture.CoreFlowTests()
        self.addCleanup(env.doCleanups)
        env.setUp()
        configured = env.tmp / "adb-home"
        configured.mkdir()
        adb = configured / "fake-adb"
        adb.write_text(f"#!{sys.executable}\n" + """
import pathlib,sys
args=sys.argv[sys.argv.index('pull')+1:]
source=args[0]
path=pathlib.Path(args[1] if len(args)>1 else pathlib.Path(source).name)
path.write_bytes(b'fixture screenshot')
""")
        adb.chmod(0o755)
        devlock = env.tmp / "devlock"
        devlock.write_text(f"#!{sys.executable}\nimport os,sys\nargs=sys.argv[sys.argv.index('--')+1:]\nos.execv(args[0],args)\n")
        devlock.chmod(0o755)
        env.jobs.bins.update(adb=("./fake-adb", str(configured)), devlock=(str(devlock), str(configured)))
        for i, destination in enumerate(([], ["mc_trial.png"], ["@out/mc_trial.png"])):
            (configured / "mc_trial.png").unlink(missing_ok=True)
            with self.subTest(destination=destination):
                reply = env.jobs.dispatch.submit({"bin": "adb", "argv": ["-s", "S1", "pull", "/sdcard/mc_trial.png", *destination],
                                         "device_access": "none", "resources": []}, b"", f"pull-{i}")
                jid = reply.get("job", reply)["id"]
                prepared = env.jobs._read_root.read_json(jid + "/prepared.json")
                subprocess.run(prepared["command"], cwd=prepared["cwd"], check=True, timeout=5)
                status, result = env.cli("GET", f"/jobs/{jid}/artifacts")
                self.assertEqual(status, 200, result)
                self.assertIn('mc_trial.png', [item['path'] for item in result['items']])
                self.assertTrue(Path(env.jobs.path(jid, "out", "mc_trial.png")).is_file(), result)
                self.assertFalse((configured / "mc_trial.png").exists())
