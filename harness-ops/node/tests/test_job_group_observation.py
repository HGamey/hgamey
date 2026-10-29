"""Actual process groups and stale completion observations; no device IO."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import job_runtime
from node_service import Jobs


def until(predicate, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.02)
    raise AssertionError('process fixture did not reach its expected event')


class GroupObservationTests(unittest.TestCase):
    def test_leader_exit_kill_and_kernel_lock_release(self):
        from integration_job_process_groups import run
        self.assertTrue(run()['passed'])

    def test_signal_zero_permission_is_not_a_completion_probe(self):
        child = subprocess.Popen([sys.executable, '-c', 'import sys;sys.stdin.read()'],
                                 stdin=subprocess.PIPE, start_new_session=True)
        try:
            with patch.object(os, 'killpg', side_effect=PermissionError('fixture: macOS signal zero')) as signal:
                self.assertEqual(job_runtime._group_completion(child.pid)['status'], 'present')
                child.communicate(timeout=5)
                self.assertEqual(job_runtime._group_completion(child.pid)['status'], 'absent')
            signal.assert_not_called()
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)

    def test_unreaped_zombie_is_not_live_work(self):
        child = subprocess.Popen([sys.executable, '-c', 'pass'], start_new_session=True)
        try:
            def zombie():
                table = subprocess.check_output(['ps', '-axo', 'pid=,stat='], text=True)
                return any(int(pid) == child.pid and state.startswith('Z')
                           for pid, state in (line.split() for line in table.splitlines()))
            until(zombie)  # Do not poll/wait the child until the zombie is observed.
            self.assertEqual(job_runtime._group_completion(child.pid)['status'], 'absent')
        finally:
            child.wait(timeout=5)

    def test_failed_process_query_stays_unknown_without_signals(self):
        with patch('job_runtime.subprocess.run', side_effect=OSError('fixture: ps unavailable')), \
                patch.object(os, 'killpg') as signal:
            value = job_runtime._group_completion(os.getpgrp())
        self.assertEqual(value['status'], 'unknown')
        self.assertIn('ps unavailable', value['error'])
        signal.assert_not_called()


class GroupRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.jobs = Jobs(self.temporary.name, {'host': (sys.executable, self.temporary.name)}, max_running=1)
        self.addCleanup(self.jobs._read_root.close)
        self.dispatch = self.jobs.dispatch
        self.dispatch.start = lambda: None
        self.queue = self.dispatch.queue
        self.jobs.leases.snapshot = lambda **kwargs: {'status': 'clear', 'entries': [], 'error': None}

    def completed(self, pgid, *, lost=False):
        jid = self.jobs.start({'bin': 'host', 'argv': ['-c', 'pass'], 'device_access': 'none'}, b'')['id']
        meta = self.jobs._read_meta(jid)
        meta.update(state='finished', phase='interrupted', outcome='interrupted', exit=-1 if lost else 143,
                    ended=time.time(), lost=lost, recovery={'status': 'required', 'reason': 'original evidence'})
        meta['execution'].update(worker_alive=False, child_pid=pgid, returncode=None if lost else 143,
            owned_process_group={'scope': 'original_process_group', 'status': 'unknown', 'pgid': pgid,
                                 'checked_at': time.time(), 'error': 'PermissionError: original EPERM'})
        self.jobs._write_meta(jid, meta)
        self.jobs._read_root.write_json(jid + '/exitcode', meta['exit'])
        self.jobs._read_root.write_json(jid + '/launch.json', {})
        self.queue.set_state(jid, 'blocked', observation=meta)
        return jid

    def test_dead_groups_release_old_observations_without_rewriting_results(self):
        child = subprocess.Popen([sys.executable, '-c', 'pass'], start_new_session=True)
        child.wait(timeout=5)
        ids = [self.completed(child.pid, lost=lost) for lost in (False, True)]
        before = {jid: self.jobs._read_meta(jid) for jid in ids}
        with patch.object(os, 'killpg', side_effect=AssertionError('reconciliation must not signal')), \
                patch('job_runtime.subprocess.run', wraps=subprocess.run) as ps:
            self.dispatch._refresh_host_capacity()
        self.assertEqual(ps.call_count, 1, 'one process snapshot serves the dispatch pass')
        for jid in ids:
            status = self.jobs.status(jid)
            self.assertEqual(status['queue']['state'], 'done')
            self.assertEqual(status['execution']['owned_process_group']['status'], 'absent')
            for key in ('outcome', 'exit', 'lost', 'recovery'):
                self.assertEqual(status[key], before[jid][key])
            self.assertEqual(status['execution']['returncode'], before[jid]['execution']['returncode'])
            with self.queue._transaction() as db:
                events = [json.loads(row[0]) for row in db.execute(
                    'SELECT committed_status FROM job_events WHERE job_id=? ORDER BY sequence', (jid,))]
            self.assertTrue(any(event.get('owned_error') == 'PermissionError: original EPERM' for event in events))
        self.assertEqual(self.queue.capacity_snapshot()['reserved']['cpu'], 0)

    def test_blocked_only_dispatch_keeps_observing_until_group_exits(self):
        child = subprocess.Popen([sys.executable, '-c', 'import sys;sys.stdin.read()'],
                                 stdin=subprocess.PIPE, start_new_session=True)
        jid = self.completed(child.pid, lost=True)
        observed_twice = threading.Event()
        original = job_runtime._group_completion
        count = 0

        def observe(*args, **kwargs):
            nonlocal count
            value = original(*args, **kwargs)
            if value['status'] == 'present':
                count += 1
                if count >= 2:
                    observed_twice.set()
            return value

        try:
            with patch.object(job_runtime, '_group_completion', side_effect=observe):
                type(self.dispatch).start(self.dispatch)
                self.assertTrue(observed_twice.wait(4), 'blocked-only dispatcher stopped observing')
                child.communicate(timeout=5)
                until(lambda: self.queue.get(jid)['state'] == 'done')
                self.assertEqual(self.jobs.status(jid)['outcome'], 'interrupted')
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
            self.queue.set_state(jid, 'done')
            thread = self.dispatch.thread
            if thread:
                thread.join(5)
                self.assertFalse(thread.is_alive())

    def test_stale_blocked_snapshot_cannot_reopen_a_released_job(self):
        jid = self.completed(os.getpgrp())
        stale = self.queue.get(jid)
        self.queue.set_state(jid, 'done', host_capacity_release={'at': time.time(), 'basis': 'administrator_reset'})
        before = self.jobs._read_meta(jid)
        with patch.object(job_runtime, '_group_completion', side_effect=AssertionError('done job must not probe a reused PGID')):
            self.assertTrue(self.dispatch.finalize(stale))
        self.assertEqual(self.queue.get(jid)['state'], 'done')
        self.assertEqual(self.jobs._read_meta(jid), before)


if __name__ == '__main__':
    unittest.main()
