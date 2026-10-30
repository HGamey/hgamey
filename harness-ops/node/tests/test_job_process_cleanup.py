"""Real orphaned task groups: reset/cancel must release their kernel locks."""
import fcntl
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

NODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NODE))
import job_runtime
from node_service import Jobs


class TaskProcessCleanupTests(unittest.TestCase):
    def test_reset_and_cancel_terminate_then_kill_remaining_group(self):
        for action in ("normal", "reset", "reset-running", "cancel", "cancel-running"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as directory:
                jobs = Jobs(directory, {"host": (sys.executable, directory)}, max_running=1)
                jobs.dispatch.start = lambda: None
                descendant = r"""
import fcntl,json,os,signal,sys,time
from pathlib import Path
out=Path(sys.argv[1])
lock=open(out/'device.lock','w')
fcntl.flock(lock,fcntl.LOCK_EX)
signal.signal(signal.SIGINT,signal.SIG_IGN)
signal.signal(signal.SIGTERM,lambda *_: (out/'term-seen').touch())
signal.alarm(20)
(out/'ready.tmp').write_text(json.dumps({'pgid':os.getpgrp(),'pid':os.getpid()}))
(out/'ready.tmp').replace(out/'ready.json')
while True: time.sleep(.01)
"""
                leader = ("import pathlib,signal,subprocess,sys,time;out=pathlib.Path(sys.argv[1]);"
                          f"subprocess.Popen([sys.executable,'-c',{descendant!r},str(out)]);"
                          "signal.signal(signal.SIGINT,lambda *_:sys.exit(0))\n"
                          "while not (out/'ready.json').exists(): time.sleep(.01)\n")
                if action.startswith("cancel") or action == "reset-running":
                    leader += "while True: time.sleep(.01)\n"
                if action == "cancel-running":
                    leader = leader.replace("lambda *_:sys.exit(0)", "signal.SIG_IGN")
                jid = jobs.start({"bin": "host", "argv": ["-c", leader, "@out"],
                                  "device_access": "none"}, b"")["id"]
                out = Path(jobs.path(jid, "out"))
                pgid = None
                # Run the real worker with short escalation intervals, as the
                # existing runtime tests do; no mocked process or signal call.
                bootstrap = (f"import sys;sys.path.insert(0,{str(NODE)!r});import job_runtime;"
                             "sys.exit(job_runtime._worker(sys.argv[1],int(sys.argv[2]),(.1,.1)))")
                worker = patch("job_runtime._worker_command", lambda job_dir, lock_fd:
                               [sys.executable, "-c", bootstrap, job_dir, str(lock_fd)])
                worker.start()
                processes = []
                launch = job_runtime.launch

                def launch_worker(*args, **kwargs):
                    proc = launch(*args, **kwargs)
                    processes.append(proc)
                    return proc

                launcher = patch("job_runtime.launch", side_effect=launch_worker)
                launcher.start()

                def until(predicate):
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        jobs.dispatch.tick()
                        if predicate():
                            return
                        time.sleep(.02)
                    self.fail(f"{action} fixture did not reach expected state")

                def lock_free():
                    with (out / "device.lock").open("r+") as lock:
                        try:
                            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            return True
                        except BlockingIOError:
                            return False

                try:
                    until(lambda: (out / "ready.json").exists())
                    pgid = json.loads((out / "ready.json").read_text())["pgid"]
                    self.assertFalse(lock_free())
                    if action == "normal":
                        until(lambda: jobs.status(jid)["queue"]["state"] == "done")
                        self.assertEqual(jobs.status(jid)["exit"], 0)
                        self.assertEqual(jobs.status(jid)["recovery"]["status"], "required")
                    elif action == "reset":
                        until(lambda: jobs.status(jid)["state"] == "finished")
                        self.assertTrue(jobs.reset_queue(jid, "fixture: stop leftover task processes")["released"])
                    elif action == "reset-running":
                        self.assertTrue(jobs.reset_queue(jid, "fixture: stop active task and its children")["released"])
                    else:
                        self.assertTrue(jobs.cancel(jid)["accepted"])
                    until(lock_free)
                    self.assertTrue((out / "term-seen").exists(), "send TERM before KILL")
                    # The child ignores TERM; the lock becoming free proves the
                    # forced phase ran, without counting an orphan zombie as live work.
                finally:
                    worker.stop()
                    launcher.stop()
                    if pgid is not None:
                        try:
                            os.killpg(pgid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    # The device lock is released before the worker finishes
                    # writing its terminal records. Reap it before rmtree.
                    for proc in processes:
                        proc.wait()
                    jobs._read_root.close()


if __name__ == "__main__":
    unittest.main()
