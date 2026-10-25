"""Small host-only fault chains for the SQL boundary around job completion."""
from contextlib import closing
import errno
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

NODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NODE))
import job_runtime
from job_queue import RequestConflict
from node_service import Jobs


class JobDurabilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.jobs = Jobs(str(self.root), {"host": (sys.executable, str(self.root))}, max_running=1,
                         host_capacity={"cpu": 2, "gpu": 0, "io": 0})
        self.jobs.dispatch.start = lambda: None
        self.queue = self.jobs.dispatch.queue
        self.store = self.jobs._read_root
        self.lease_read = patch.object(self.jobs.leases, "snapshot", return_value={
            "schema_version": 1, "status": "clear", "entries": [], "error": None})
        self.lease_read.start()
        self.known_pids = set()
        self.case_ok = False
        self.observations = {}

    def until(self, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(.02)
        self.fail("bounded durability fixture did not reach expected state")

    @staticmethod
    def gone(pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        return False

    def submit(self, key, resources, program="pass"):
        return self.jobs.start({"bin": "host", "argv": ["-c", program, "@out"],
                               "device_access": "none", "resources": resources}, b"", key)["id"]

    def terminal(self, jid):
        self.jobs.dispatch.tick()
        if self.queue.get(jid)["state"] in {"done", "blocked"}:
            return self.jobs.status(jid)
        return None

    def remember_pids(self):
        for row in self.queue.rows():
            try:
                meta = self.jobs._read_meta(row["job_id"])
            except (OSError, ValueError):
                continue
            self.known_pids.update(pid for pid in (
                meta.get("pid"), (meta.get("execution") or {}).get("child_pid"))
                if isinstance(pid, int) and pid > 1)

    def committed_events(self, jid):
        # One read transaction checks the published row/event pair, not a
        # recomputation of the production projection.
        with closing(sqlite3.connect(Path(self.queue.path).as_uri() + "?mode=ro", uri=True)) as db:
            db.execute("BEGIN")
            row = db.execute("SELECT status_revision,status_projection FROM jobs WHERE job_id=?", (jid,)).fetchone()
            events = db.execute("SELECT revision,committed_status FROM job_events WHERE job_id=? ORDER BY sequence", (jid,)).fetchall()
        projection = json.loads(row[1]) if row[1] is not None else None
        values = [{"revision": revision, "committed_status": json.loads(raw)} for revision, raw in events]
        self.assertTrue(values)
        self.assertEqual(values[-1], {"revision": row[0], "committed_status": projection})
        return values

    def tearDown(self):
        # Release only the test's fsync pause; no historical PID signals or job replay.
        (self.root / "continue-fsync").touch()
        self.remember_pids()
        try:
            self.until(lambda: all(self.gone(pid) for pid in self.known_pids), timeout=5)
        except BaseException:
            self.case_ok = False
            raise
        finally:
            self.lease_read.stop()
            self.store.close()
            qa = NODE.parents[1] / "qa"
            if qa.is_dir():
                name = f"job-durability-host-{self._testMethodName}-{os.getpid()}-{time.time_ns()}"
                summary = {"passed": self.case_ok, "scope": "host fsync fault injection, actual worker and SQL; no power-loss/device claim",
                           "observations": self.observations, "known_pids": sorted(self.known_pids),
                           "remaining_known_pids": [pid for pid in sorted(self.known_pids) if not self.gone(pid)],
                           "runner_logs": {p.parent.name: p.read_text(errors="replace")
                                           for p in Path(self.jobs.dir).glob("*/runner.log") if p.is_file()},
                           "device_transport_calls": 0}
                if not self.case_ok:
                    evidence = qa / (name + "-failure")
                    shutil.copytree(self.root, evidence)
                    summary["failure_evidence"] = str(evidence)
                path = qa / (name + ".json")
                path.write_text(json.dumps(summary, indent=2) + "\n")
                print(f"job durability host result: {path}", file=sys.stderr)
            self.temporary.cleanup()

    def test_visible_terminal_fsync_failure_blocks_until_retry_without_replay(self):
        counter = "import pathlib,sys; p=pathlib.Path(sys.argv[1],'executions'); p.open('a').write('run\\n')"
        jid = self.submit("visible-terminal", ["host:shared"], counter)
        job_dir = self.jobs.path(jid)
        armed = self.root / "fsync-fault-armed"
        armed.touch()
        visible = self.root / "terminal-visible"
        resume = self.root / "continue-fsync"

        def worker_with_fault(directory, lock_fd):
            # Execute the actual worker. Only its first terminal directory fsync
            # is paused and failed after the candidate rename became visible.
            code = f"""
import errno, json, os, pathlib, sys, time
sys.path.insert(0, {str(NODE)!r})
import job_runtime
directory = pathlib.Path({directory!r})
identity = directory.stat()
original_fsync = os.fsync
def fail_terminal_directory(fd):
    current = os.fstat(fd)
    try:
        terminal = json.loads((directory / 'meta.json').read_text()).get('phase') in ('finished', 'interrupted')
    except (OSError, ValueError):
        terminal = False
    if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino) and terminal and pathlib.Path({str(armed)!r}).exists():
        pathlib.Path({str(visible)!r}).touch()
        deadline = time.monotonic() + 15
        while not pathlib.Path({str(resume)!r}).exists() and time.monotonic() < deadline:
            time.sleep(.02)
        raise OSError(errno.EIO, 'fixture: terminal rename visible but directory fsync failed')
    return original_fsync(fd)
os.fsync = fail_terminal_directory
sys.exit(job_runtime._worker({directory!r}, {lock_fd}))
"""
            return [sys.executable, "-c", code]

        with patch.object(job_runtime, "_worker_command", worker_with_fault):
            self.jobs.dispatch.tick()
        self.until(visible.exists)
        candidate = self.jobs._read_meta(jid)
        self.assertEqual((candidate["phase"], candidate["exit"]), ("finished", 0))
        self.assertTrue(job_runtime.runner_alive(self.store, jid))
        public = self.jobs.status(jid)
        self.assertEqual((public["phase"], public["state"], public["queue"]["state"]),
                         ("finalizing", "running", "claimed"))
        self.assertEqual(public["queue"]["host_capacity"]["state"], "reserved")
        self.assertIsNone(self.queue.get(jid)["host_capacity_release"])
        self.jobs.dispatch.tick()
        self.assertEqual(self.queue.get(jid)["state"], "claimed")
        held_events = self.committed_events(jid)
        self.assertFalse(any(item["committed_status"]["outcome"] == "succeeded" for item in held_events))
        self.assertFalse(any(item["committed_status"]["phase"] == "finished" for item in held_events))
        self.observations["visible_while_worker_held"] = {"phase": public["phase"], "state": public["state"]}

        waiting = self.submit("same-resource", ["host:shared"], "raise RuntimeError('must remain queued')")
        other = self.submit("unrelated-resource", ["host:other"])
        sync = self.store.sync

        def fail_job_sync(rel, directory=None):
            if rel == jid + "/meta.json" and armed.exists():
                raise OSError(errno.EIO, "fixture: job storage remains unavailable")
            return sync(rel, directory=directory)

        with patch.object(self.store, "sync", side_effect=fail_job_sync):
            resume.touch()
            self.until(lambda: not job_runtime.runner_alive(self.store, jid))
            blocked = self.until(lambda: self.terminal(jid))
            self.assertEqual((blocked["phase"], blocked["state"], blocked["queue"]["state"]),
                             ("storage_blocked", "running", "blocked"))
            self.assertEqual(blocked["queue"]["host_capacity"]["state"], "reserved")
            self.assertIsNone(self.queue.get(jid)["host_capacity_release"])
            reason = blocked["queue"]["state_reason"]
            self.assertEqual(reason["kind"], "durability")
            self.assertTrue(reason["stage"] and reason["message"])
            self.assertIsInstance(reason["at"], (int, float))
            completed_other = self.until(lambda: self.terminal(other))
            self.assertEqual((completed_other["exit"], completed_other["queue"]["state"]), (0, "done"))
            self.assertEqual(completed_other["queue"]["host_capacity"]["state"], "released")
            self.assertEqual(self.queue.capacity_snapshot()["reserved"], {"cpu": 1, "gpu": 0, "io": 0})
            self.assertEqual(self.queue.get(waiting)["state"], "queued")
            self.assertFalse(Path(self.jobs.path(waiting, "launch.json")).exists())
            with self.assertRaises(RequestConflict):
                self.jobs.release_queue(jid, "cannot replace a failed storage barrier")
            retry = self.jobs.retry_finalize(jid)
            self.assertFalse(retry["storage_committed"])
            self.assertEqual(retry["job"]["phase"], "storage_blocked")
            self.assertIsNone(self.queue.get(jid)["host_capacity_release"])
            blocked_events = self.committed_events(jid)
            self.assertEqual(blocked_events[-1]["committed_status"]["queue_state"], "blocked")
            self.assertEqual(blocked_events[-1]["committed_status"]["storage_kind"], "durability")
            self.assertFalse(any(item["committed_status"]["outcome"] == "succeeded" for item in blocked_events))
            self.observations["blocked"] = reason
            self.observations["blocked_host_capacity"] = blocked["queue"]["host_capacity"]
            self.observations["blocked_events"] = blocked_events

        armed.unlink()
        before = Path(job_dir, "launch.json").read_bytes()
        with patch.object(job_runtime, "launch", side_effect=AssertionError("finalize must not launch")) as launch:
            retry = self.jobs.retry_finalize(jid)
        launch.assert_not_called()
        self.assertTrue(retry["storage_committed"])
        self.assertEqual((retry["job"]["state"], retry["job"]["phase"], retry["job"]["exit"],
                          retry["job"]["queue"]["state"]), ("finished", "finished", 0, "done"))
        self.assertEqual(retry["job"]["queue"]["host_capacity"]["state"], "released")
        self.assertEqual(self.queue.get(jid)["host_capacity_release"]["basis"], "original_process_group_completed")
        self.assertEqual(self.queue.capacity_snapshot()["reserved"], {"cpu": 0, "gpu": 0, "io": 0})
        committed_events = self.committed_events(jid)
        terminal_event = committed_events[-1]
        self.assertGreater(terminal_event["revision"], blocked_events[-1]["revision"])
        self.assertEqual(terminal_event["revision"], retry["job"]["status_revision"])
        self.assertEqual(terminal_event["committed_status"], retry["job"]["committed_status"])
        self.assertEqual({key: terminal_event["committed_status"][key] for key in
                          ("queue_state", "phase", "outcome", "exit", "host_capacity_state", "storage_kind")},
                         {"queue_state": "done", "phase": "finished", "outcome": "succeeded", "exit": 0,
                          "host_capacity_state": "released", "storage_kind": None})
        self.assertEqual(sum(item["committed_status"]["outcome"] == "succeeded" for item in committed_events), 1)
        self.jobs.status(jid)
        self.assertEqual(self.committed_events(jid), committed_events)
        self.observations["committed_terminal_event"] = terminal_event
        self.assertEqual(Path(job_dir, "launch.json").read_bytes(), before)
        self.assertEqual(Path(job_dir, "out", "executions").read_text(), "run\n")
        self.assertTrue(self.jobs.cancel(waiting)["accepted"])
        self.observations["explicit_retry"] = {"storage_committed": True, "command_executions": 1,
                                                "unrelated_job_done": True, "same_resource_waited": True,
                                                "host_capacity": retry["job"]["queue"]["host_capacity"]}
        self.case_ok = True

    def test_unstarted_and_cancel_sql_facts_survive_missing_diagnostic_metadata(self):
        write_json = self.store.write_json

        def missing_meta(rel, value):
            if rel.endswith("/meta.json"):
                raise OSError(errno.EIO, "fixture: diagnostic metadata cannot be saved")
            return write_json(rel, value)

        with patch.object(self.jobs, "_prepare", side_effect=OSError(errno.EIO, "fixture: preparation unavailable")), \
                patch.object(self.store, "write_json", side_effect=missing_meta):
            jid = self.submit("prepare-failed", ["host:prepare"], "raise RuntimeError('must never execute')")
            status = self.jobs.status(jid)
            self.assertEqual((status["phase"], status["state"]), ("storage_blocked", "running"))
            row = self.queue.get(jid)
            self.assertEqual(row["state"], "blocked")
            self.assertEqual(row["state_reason"]["kind"], "durability")
            self.assertEqual(row["state_reason"]["unstarted"]["outcome"], "failed")
            self.assertEqual(row["host_capacity_release"]["basis"], "not_started")
            self.assertEqual(status["queue"]["host_capacity"]["state"], "released")
            self.assertFalse(Path(self.jobs.path(jid, "meta.json")).exists())
        with patch.object(job_runtime, "launch", side_effect=AssertionError("unstarted finalize must not launch")) as launch:
            retry = self.jobs.retry_finalize(jid)
        launch.assert_not_called()
        self.assertTrue(retry["storage_committed"])
        self.assertEqual((retry["job"]["outcome"], retry["job"]["recovery"]["status"],
                          retry["job"]["queue"]["state"]), ("failed", "not_needed", "done"))
        self.assertFalse(Path(self.jobs.path(jid, "launch.json")).exists())
        self.observations["unstarted_retry"] = {"outcome": "failed", "command_started": False}

        cancelled = self.submit("cancel-without-meta", ["host:cancel"], "raise RuntimeError('must never execute')")
        Path(self.jobs.path(cancelled, "meta.json")).unlink()
        with patch.object(self.store, "write_json", side_effect=missing_meta):
            response = self.jobs.cancel(cancelled)
        self.assertTrue(response["accepted"])
        status = self.jobs.status(cancelled)
        self.assertEqual((status["state"], status["outcome"], status["recovery"]["status"]),
                         ("finished", "cancelled", "not_needed"))
        row = self.queue.get(cancelled)
        self.assertEqual((row["state"], row["state_reason"]["kind"]), ("done", "cancelled_before_launch"))
        self.assertEqual(row["host_capacity_release"]["basis"], "not_started")
        self.assertEqual(status["queue"]["host_capacity"]["state"], "released")
        self.assertFalse(Path(self.jobs.path(cancelled, "launch.json")).exists())
        self.observations["cancel_sql_fact"] = row["state_reason"]
        self.case_ok = True


if __name__ == "__main__":
    unittest.main()
