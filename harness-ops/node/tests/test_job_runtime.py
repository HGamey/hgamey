"""Real local child processes only; no node service or device access."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

NODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NODE))
from artifact_io import SafeRoot
import job_runtime as runtime


FAULT_WORKER = r"""
import errno,json,os,signal,sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from artifact_io import SafeRoot
import job_runtime as runtime
directory,lock_fd,scenario=Path(sys.argv[2]),int(sys.argv[3]),sys.argv[4]
evidence={'worker_pid':os.getpid(),'scenario':scenario,'faults':[],'signals':[],'children':[]}
def save():
    temporary=directory/'fault-evidence.tmp'
    temporary.write_text(json.dumps(evidence))
    temporary.replace(directory/'fault-evidence.json')
def fault(operation,path,number):
    evidence['faults'].append({'operation':operation,'path':path,'errno':number,'monotonic':time.monotonic()})
    save()
    raise OSError(number,'fixture active owner record failure')
original_write,original_read,original_publish=SafeRoot.write_json,SafeRoot.read_json,SafeRoot.publish_json_once
failed_signals=set()
failed_read=failed_publish=False
def write(self,rel,value):
    if rel.endswith('/meta.json'):
        if scenario=='permanent' and value.get('state')=='finished':
            fault('final_meta',rel,errno.ENOSPC)
        active=value.get('execution',{}).get('child_pid') is not None and value.get('state')!='finished'
        signals=value.get('cancel',{}).get('signals',[])
        if active and scenario=='manual' and signals and len(signals) not in failed_signals:
            failed_signals.add(len(signals))
            fault('signal_meta',rel,errno.ENOSPC)
    return original_write(self,rel,value)
def read(self,rel,*args,**kwargs):
    global failed_read
    armed=(scenario=='manual' and evidence['signals']) or (scenario in ('owner','permanent') and (directory/'child-ready').exists())
    if rel.endswith('/cancel.json') and armed and not failed_read:
        failed_read=True
        fault('cancel_read',rel,errno.EDQUOT)
    return original_read(self,rel,*args,**kwargs)
def publish(self,rel,value):
    global failed_publish
    if scenario=='graceful' and rel.endswith('/cancel.json') and value.get('source')=='execution_deadline' and not failed_publish:
        failed_publish=True
        fault('deadline_cancel',rel,errno.EDQUOT)
    return original_publish(self,rel,value)
SafeRoot.write_json,SafeRoot.read_json,SafeRoot.publish_json_once=write,read,publish
original_signal,original_popen=runtime.os.killpg,runtime.subprocess.Popen
owned=[]
def send(pgid,sig):
    result=original_signal(pgid,sig)
    if sig:
        evidence['signals'].append({'name':signal.Signals(sig).name,'pgid':pgid,'monotonic':time.monotonic()})
        save()
    return result
def spawn(*args,**kwargs):
    child=original_popen(*args,**kwargs)
    owned.append(child)
    evidence['children'].append({'pid':child.pid})
    save()
    return child
runtime.os.killpg,runtime.subprocess.Popen=send,spawn
try:
    code=runtime._worker(str(directory),lock_fd,(.12,.12))
finally:
    # Reap only this fixture's actual Popen children after a regression; their
    # alarm bounds survival even if the product worker has already lost its lock.
    for child,item in zip(owned,evidence['children']):
        item['alive_when_worker_returned']=child.poll() is None
        try:
            child.wait(timeout=7)
        except runtime.subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=3)
            item['fixture_cleanup_killed']=True
        item['returncode']=child.returncode
    save()
sys.exit(code)
"""


class JobRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = SafeRoot(self.root)
        self.addCleanup(self.store.close)
        self.sequence = 0
        self.fault_jobs, self.fault_assertions = [], []
        self.addCleanup(self.save_fault_evidence)

    def save_fault_evidence(self):
        qa = NODE.parents[1] / "qa"
        if not self.fault_jobs or not qa.is_dir():
            return
        jobs = []
        for jid, worker in self.fault_jobs:
            item = {"job_id": jid, "worker_pid": worker.pid, "worker_returncode": worker.returncode}
            for name in ("meta.json", "cancel.json", "cancel.force.json", "fault-evidence.json"):
                path = self.root / jid / name
                item[name] = json.loads(path.read_text()) if path.exists() else None
            for name in ("log", "runner.log", "exitcode"):
                path = self.root / jid / name
                item[name] = path.read_bytes()[:8192].decode("utf-8", "replace") if path.exists() else None
            jobs.append(item)
        path = qa / f"job-owner-record-failure-{self._testMethodName}-{os.getpid()}-{time.time_ns()}.json"
        path.write_text(json.dumps({"scope": "actual ordinary worker and harmless original process group; injected record I/O only",
                                   "completed_assertions": self.fault_assertions, "jobs": jobs,
                                   "device_transport_calls": 0}, indent=2) + "\n")
        print(f"job owner record failure evidence: {path}", file=sys.stderr)

    def fault_record(self, jid):
        path = self.root / jid / "fault-evidence.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def job(self):
        self.sequence += 1
        jid = f"{self.sequence:012x}"
        (self.root / jid).mkdir()
        self.store.write_json(jid + "/meta.json", {
            "id": jid, "pid": None, "state": "running", "phase": "starting",
            "execution": {"kind": "supervised-v1"}})
        return jid

    def read(self, jid):
        return self.store.read_json(jid + "/meta.json")

    def until(self, predicate, timeout=5):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            value = predicate()
            if value:
                return value
            time.sleep(.02)
        self.fail("local worker did not reach expected state")

    def ready(self, jid):
        return b"ready" in self.store.read_bytes(jid + "/log", 4096)

    def start(self, jid, program, short_delays=False, time_budget=None, record_fault=None):
        command = [sys.executable, "-u", "-c", program]
        args = (self.store, jid, str(self.root / jid), command, str(self.root), dict(os.environ))
        if short_delays or record_fault:
            # Explicit test-only entry into the same worker function. The shipped
            # worker CLI has no delay flag or environment override.
            def test_command(job_dir, lock_fd):
                if record_fault:
                    return [sys.executable, "-c", FAULT_WORKER, str(NODE), job_dir, str(lock_fd), record_fault]
                code = (f"import sys;sys.path.insert(0,{str(NODE)!r});import job_runtime;"
                        f"sys.exit(job_runtime._worker({job_dir!r},{lock_fd},(.12,.12)))")
                return [sys.executable, "-c", code]
            with patch.object(runtime, "_worker_command", test_command):
                worker = runtime.launch(*args, time_budget=time_budget)
            if record_fault:
                self.fault_jobs.append((jid, worker))
                self.addCleanup(lambda: worker.wait(timeout=10))
            return worker
        return runtime.launch(*args, time_budget=time_budget)

    def finished(self, jid, worker):
        meta = self.until(lambda: (m if (m := self.read(jid)).get("state") == "finished" else None))
        worker.wait(5)
        self.assertFalse(runtime.runner_alive(self.store, jid))
        self.assertEqual(meta["phase"], "finished")
        self.assertNotIn("phase", meta["execution"])
        return meta

    def test_new_launcher_instance_can_cancel_worker_after_original_exits(self):
        jid = self.job()
        program = ("import signal,time,sys;signal.alarm(5);"
                   "signal.signal(signal.SIGINT,lambda *a:(print('restoring',flush=True),sys.exit(0)));"
                   "print('ready',flush=True);time.sleep(5)")
        code = (f"import os,sys;sys.path.insert(0,{str(NODE)!r});"
                "from artifact_io import SafeRoot;import job_runtime;"
                f"s=SafeRoot({str(self.root)!r});"
                f"job_runtime.launch(s,{jid!r},{str(self.root / jid)!r},"
                f"{[sys.executable, '-u', '-c', program]!r},{str(self.root)!r},dict(os.environ));"
                "os._exit(0)")
        subprocess.run([sys.executable, "-c", code], check=True, timeout=5)
        self.until(lambda: self.ready(jid))
        self.store.close()
        self.store = SafeRoot(self.root)  # Same files, entirely new caller state.
        self.addCleanup(self.store.close)
        self.assertTrue(runtime.runner_alive(self.store, jid))
        request = runtime.request_cancel(self.store, jid)
        self.assertEqual(request["mode"], "graceful")
        meta = self.until(lambda: (m if (m := self.read(jid)).get("state") == "finished" else None))
        self.until(lambda: not runtime.runner_alive(self.store, jid))
        self.assertEqual((meta["outcome"], meta["recovery"]["status"], meta["exit"]), ("cancelled", "not_reported", 0))
        self.assertEqual(meta["execution"]["owned_process_group"]["status"], "absent")
        self.assertEqual(meta["execution"]["process_management"], {
            "policy": "process_group", "backend": "posix_process_group",
            "completion_scope": "original_process_group", "detached_descendants": "unverified"})
        self.assertEqual([s["name"] for s in meta["cancel"]["signals"]], ["SIGINT"])
        self.assertIn(b"restoring", self.store.read_bytes(jid + "/log", 4096))
        self.assertEqual(self.store.read_json(jid + "/launch.json").keys(), {"command", "cwd"})

    def test_graceful_is_once_and_force_only_upgrades_to_term_then_kill(self):
        jid = self.job()
        program = ("import signal,time;signal.alarm(5);"
                   "signal.signal(signal.SIGINT,lambda *a:print('INT',flush=True));"
                   "signal.signal(signal.SIGTERM,lambda *a:print('TERM',flush=True));"
                   "print('ready',flush=True);time.sleep(5)")
        worker = self.start(jid, program, short_delays=True)
        self.until(lambda: self.ready(jid))
        first = runtime.request_cancel(self.store, jid)
        self.until(lambda: self.read(jid).get("cancel", {}).get("signals"))
        time.sleep(.3)  # Longer than the injected force intervals: graceful never escalates.
        self.assertEqual([s["name"] for s in self.read(jid)["cancel"]["signals"]], ["SIGINT"])
        self.assertEqual(runtime.request_cancel(self.store, jid)["requested_at"], first["requested_at"])
        runtime.request_cancel(self.store, jid, force=True)
        self.assertEqual(runtime.request_cancel(self.store, jid)["mode"], "escalate")
        meta = self.finished(jid, worker)
        self.assertEqual([s["name"] for s in meta["cancel"]["signals"]], ["SIGINT", "SIGTERM", "SIGKILL"])
        self.assertEqual((meta["outcome"], meta["recovery"]["status"], meta["exit"], meta["execution"]["returncode"]),
                         ("cancelled", "required", 137, -9))
        self.assertEqual(self.store.read_bytes(jid + "/exitcode", 128), b"137")

    def test_cancel_before_execution_and_child_start_failure_have_no_fake_exitcode(self):
        jid = self.job()
        runtime.request_cancel(self.store, jid)
        worker = self.start(jid, "raise RuntimeError('must not run')",
                            time_budget={"wall_seconds": 1, "timeout_action": "escalate"})
        meta = self.finished(jid, worker)
        self.assertEqual((meta["outcome"], meta["recovery"]["status"]), ("cancelled", "not_needed"))
        self.assertEqual(meta["cancel"]["status"], "not_applied")
        self.assertIsNone(meta["execution"]["child_pid"])
        self.assertEqual(meta["execution"]["owned_process_group"]["status"], "not_started")
        self.assertEqual(meta["execution"]["time_budget"]["status"], "not_started")
        self.assertIsNone(meta["execution"]["time_budget"]["started_at"])
        self.assertFalse((self.root / jid / "exitcode").exists())
        jid = self.job()
        worker = runtime.launch(self.store, jid, str(self.root / jid), [str(self.root / "missing-command")],
                                str(self.root), dict(os.environ))
        meta = self.finished(jid, worker)
        self.assertEqual(meta["outcome"], "failed")
        self.assertIsNone(meta["exit"])
        self.assertIn("FileNotFoundError", meta["error"])
        self.assertFalse((self.root / jid / "exitcode").exists())
        self.assertEqual(self.store.read_bytes(jid + "/log", 128), b"")
        jid = self.job()
        unexpected = self.root / "must-not-start"
        worker = runtime.launch(self.store, jid, str(self.root / jid),
                                [sys.executable, "-c", f"from pathlib import Path;Path({str(unexpected)!r}).touch()"],
                                str(self.root), dict(os.environ), execution_policy="contained")
        meta = self.finished(jid, worker)
        self.assertEqual((meta["outcome"], meta["recovery"]["status"], meta["exit"]), ("failed", "not_needed", None))
        self.assertIn("execution policy unavailable", meta["error"])
        self.assertNotIn("process_management", meta["execution"])
        self.assertFalse(unexpected.exists())

    def test_duplicate_launch_blocked_and_child_does_not_keep_dead_worker_lock(self):
        jid = self.job()
        done = self.root / "child-done"
        worker = self.start(jid, f"import time,pathlib;print('ready',flush=True);time.sleep(.5);pathlib.Path({str(done)!r}).touch()")
        self.until(lambda: self.ready(jid))
        self.assertTrue(runtime.runner_alive(self.store, jid))
        with self.assertRaisesRegex(ValueError, "live worker"):
            self.start(jid, "raise RuntimeError('duplicate')")
        worker.kill()  # Actual Popen owned by this test, never a PID recovered from disk.
        worker.wait(5)
        self.assertFalse(runtime.runner_alive(self.store, jid))
        self.assertEqual(self.read(jid)["state"], "running")  # Service must reconcile interruption.
        with self.assertRaisesRegex(ValueError, "already launched"):
            self.start(jid, "raise RuntimeError('replay')")
        self.until(done.exists)

    def test_deadline_preserves_natural_exit_and_upgrades_existing_manual_intent(self):
        budget = {"wall_seconds": 1, "timeout_action": "escalate"}
        jid = self.job()
        worker = self.start(jid, "print('done')", time_budget=budget)
        meta = self.finished(jid, worker)
        observed = meta["execution"]["time_budget"]
        self.assertEqual((meta["exit"], observed["status"]), (0, "finished"))
        self.assertIsNone(observed["exceeded_at"])
        self.assertIsNotNone(observed["finished_at"])
        self.assertNotIn("cancel", meta)
        self.assertEqual(self.store.read_json(jid + "/launch.json")["time_budget"], budget)

        jid = self.job()
        program = ("import signal,time;signal.alarm(5);"
                   "signal.signal(signal.SIGINT,lambda *a:print('INT',flush=True));"
                   "signal.signal(signal.SIGTERM,lambda *a:print('TERM',flush=True));"
                   "print('ready',flush=True);time.sleep(5)")
        worker = self.start(jid, program, short_delays=True, time_budget=budget, record_fault="manual")
        self.until(lambda: self.ready(jid))
        manual = runtime.request_cancel(self.store, jid)
        self.until(lambda: self.fault_record(jid).get("faults"))
        self.assertTrue(runtime.runner_alive(self.store, jid))
        time.sleep(.3)  # Known graceful intent must survive I/O failure without early escalation.
        self.assertEqual([s["name"] for s in self.fault_record(jid)["signals"]], ["SIGINT"])
        meta = self.finished(jid, worker)
        observed = meta["execution"]["time_budget"]
        self.assertEqual((observed["status"], meta["exit"]), ("exceeded", 137))
        self.assertGreaterEqual(observed["elapsed_seconds"], 1)
        self.assertEqual(meta["cancel"]["requested_at"], manual["requested_at"])
        self.assertNotIn("source", meta["cancel"])
        self.assertEqual(meta["cancel"]["escalation_source"], "execution_deadline")
        self.assertEqual([s["name"] for s in meta["cancel"]["signals"]], ["SIGINT", "SIGTERM", "SIGKILL"])
        self.assertEqual(meta["recovery"]["status"], "required")
        intent = meta["execution"]["stop_intent"]
        self.assertEqual(intent["schema"], "phonefarm/job-stop-intent@1")
        self.assertEqual(intent["mode"], "escalate")
        self.assertIn("execution_deadline", intent["causes"])
        self.assertGreaterEqual(intent["record_errors"]["count"], 2)
        evidence = self.fault_record(jid)
        self.assertEqual([s["name"] for s in evidence["signals"]], ["SIGINT", "SIGTERM", "SIGKILL"])
        gap = evidence["signals"][2]["monotonic"] - evidence["signals"][1]["monotonic"]
        self.assertGreaterEqual(gap, .1)
        self.assertLess(gap, 1, "record publication retry must not reset the escalation clock")
        self.assertTrue(all(s["pgid"] == meta["execution"]["child_pid"] for s in evidence["signals"]))
        self.assertFalse(evidence["children"][0]["alive_when_worker_returned"])
        self.fault_assertions.append("manual intent/history survive record faults; deadline escalates on original signal clock")

    def test_active_record_failures_keep_owner_and_never_publish_false_success(self):
        for scenario in ("graceful", "owner", "permanent"):
            with self.subTest(scenario=scenario):
                jid = self.job()
                ready = self.root / jid / "child-ready"
                # Graceful deliberately lives past both shortened escalation
                # intervals. Other cases exit zero promptly after the real INT.
                pause = .4 if scenario == "graceful" else .01
                program = ("import signal,time,sys;from pathlib import Path;signal.alarm(6);"
                           f"signal.signal(signal.SIGINT,lambda *a:(print('INT',flush=True),time.sleep({pause}),sys.exit(0)));"
                           f"Path({str(ready)!r}).touch();print('ready',flush=True);time.sleep(6)")
                budget = {"wall_seconds": 1, "timeout_action": "graceful"} if scenario == "graceful" else None
                worker = self.start(jid, program, time_budget=budget, record_fault=scenario)
                if scenario == "graceful":
                    self.until(lambda: self.fault_record(jid).get("signals"))
                    self.assertIsNone(worker.poll())
                    self.assertTrue(runtime.runner_alive(self.store, jid))
                    with runtime.runner_lock(self.store, jid) as acquired:
                        self.assertFalse(acquired)
                worker.wait(timeout=8)
                self.assertFalse(runtime.runner_alive(self.store, jid))
                evidence, meta = self.fault_record(jid), self.read(jid)
                self.assertTrue(evidence["faults"])
                self.assertEqual([s["name"] for s in evidence["signals"]], ["SIGINT"])
                self.assertEqual(evidence["children"][0]["returncode"], 0)
                self.assertFalse(evidence["children"][0]["alive_when_worker_returned"])
                self.assertIn(b"INT", self.store.read_bytes(jid + "/log", 4096))
                if scenario == "permanent":
                    self.assertNotEqual(worker.returncode, 0)
                    self.assertNotEqual(meta.get("state"), "finished")
                    self.assertNotEqual(meta.get("outcome"), "succeeded")
                    self.assertTrue(any(f["operation"] == "final_meta" for f in evidence["faults"]))
                    self.assertEqual(self.store.read_bytes(jid + "/exitcode", 128), b"0")
                else:
                    self.assertEqual(worker.returncode, 0)
                    self.assertEqual(meta["exit"], 0)
                    self.assertEqual(meta["execution"]["owned_process_group"]["status"], "absent")
                    intent = meta["execution"]["stop_intent"]
                    self.assertEqual(intent["schema"], "phonefarm/job-stop-intent@1")
                    self.assertGreaterEqual(intent["record_errors"]["count"], 1)
                    self.assertEqual([s["name"] for s in meta["cancel"]["signals"]], ["SIGINT"])
                    if scenario == "graceful":
                        self.assertEqual(intent["mode"], "graceful")
                        self.assertIn("execution_deadline", intent["causes"])
                        self.assertEqual(meta["execution"]["time_budget"]["status"], "exceeded")
                        self.assertEqual(meta["outcome"], "cancelled")
                        self.assertEqual({f["operation"] for f in evidence["faults"]}, {"deadline_cancel"})
                    else:
                        self.assertEqual(intent["mode"], "escalate")
                        self.assertIn("owner_record_error", intent["causes"])
                        self.assertEqual((meta["phase"], meta["outcome"]), ("interrupted", "interrupted"))
                self.fault_assertions.append(scenario)


if __name__ == "__main__":
    unittest.main()
