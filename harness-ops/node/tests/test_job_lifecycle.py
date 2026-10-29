"""One scheduling outcome for unknown completion and failed result storage."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import job_runtime
from node_service import Jobs
from test_job_group_observation import until


class JobLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.jobs = Jobs(temporary.name, {'host': (sys.executable, temporary.name)}, max_running=1)
        self.addCleanup(self.jobs._read_root.close)
        self.jobs.dispatch.start = lambda: None
        self.jobs.leases.snapshot = lambda **kwargs: {'status': 'clear', 'entries': [], 'error': None}
        self.queue = self.jobs.dispatch.queue

    def submit(self, device, program='pass'):
        return self.jobs.start({'bin': 'host', 'argv': ['-c', program, '@out'],
                               'device_access': 'write', 'resources': ['device:' + device]}, b'')['id']

    def ended(self, jid):
        self.jobs.dispatch.tick()
        until(lambda: self.jobs._read_meta(jid).get('phase') == 'finished'
              and not job_runtime.runner_alive(self.jobs._read_root, jid))

    def test_unknown_without_lost_releases_slots_and_explains_its_reservation(self):
        for lost in (False, True):
            with self.subTest(lost=lost):
                jid = self.submit('usb-a')
                self.ended(jid)
                meta = self.jobs._read_meta(jid)
                meta.update(phase='interrupted', outcome='interrupted', lost=lost,
                            recovery={'status': 'required', 'reason': 'device state unknown'})
                meta['execution']['owned_process_group'].update(status='unknown', error='prior query failed')
                self.jobs._write_meta(jid, meta)
                with patch('job_runtime.subprocess.run', side_effect=OSError('fixture: current query unavailable')):
                    self.jobs.dispatch.finalize(self.queue.get(jid))
                view = self.jobs.status(jid)
                self.assertEqual(view['queue']['state'], 'blocked')
                self.assertEqual(view['queue']['host_capacity']['state'], 'released')
                self.assertEqual(self.queue.capacity_snapshot()['reserved']['cpu'], 0)
                same, other = self.submit('usb-a'), self.submit('usb-b')
                self.assertIn(jid, self.jobs.status(same)['queue']['waiting']['blocking_jobs'])
                self.assertEqual(self.queue.claim_next(1)['job_id'], other)
                for ended in (jid, same, other):
                    self.queue.set_state(ended, 'done')

    def test_result_sync_failure_does_not_reserve_a_finished_job(self):
        program = "import pathlib,sys;pathlib.Path(sys.argv[1],'executions').write_text('once')"
        jid = self.submit('usb-a', program)
        self.ended(jid)
        original = self.jobs._read_root.sync

        def failed_sync(rel, directory=None):
            if rel == jid + '/meta.json':
                raise OSError('fixture: result fsync failed')
            return original(rel, directory=directory)

        with patch.object(self.jobs._read_root, 'sync', side_effect=failed_sync):
            self.assertFalse(self.jobs.dispatch.finalize(self.queue.get(jid)))
        view = self.jobs.status(jid)
        self.assertEqual((view['state'], view['queue']['state'], view['exit']), ('finished', 'done', 0))
        self.assertIn('result fsync failed', view['storage_error']['message'])
        self.assertEqual(view['queue']['host_capacity']['state'], 'released')
        following = self.submit('usb-a')
        self.assertEqual(self.queue.claim_next(1)['job_id'], following)
        self.assertEqual(Path(self.jobs.path(jid, 'out', 'executions')).read_text(), 'once')
        self.assertEqual(view['committed_status']['queue_state'], 'done')
        self.assertEqual(view['committed_status']['storage_kind'], 'durability')
        reset = self.jobs.reset_queue(jid, 'fixture: retry saving existing records')
        self.assertTrue(reset['storage_committed'])
        self.assertTrue(reset['released'])
        self.assertNotIn('storage_error', reset['job'])
        self.assertEqual(Path(self.jobs.path(jid, 'out', 'executions')).read_text(), 'once')

    def test_unknown_reset_is_idempotent_and_does_not_claim_observed_completion(self):
        jid = self.submit('usb-a')
        self.ended(jid)
        meta = self.jobs._read_meta(jid)
        meta.update(phase='interrupted', outcome='interrupted', lost=True,
                    recovery={'status': 'required', 'reason': 'original failure'})
        meta['execution']['owned_process_group'].update(status='unknown', pgid=None, error='not recorded')
        self.jobs._write_meta(jid, meta)
        self.jobs.dispatch.finalize(self.queue.get(jid))
        self.assertEqual(self.queue.get(jid)['state'], 'blocked')
        first = self.jobs.reset_queue(jid, 'fixture: explicit owner reset')
        second = self.jobs.reset_queue(jid, 'fixture: repeat after network uncertainty')
        self.assertTrue(first['released'] and second['released'])
        self.assertEqual(first['job']['queue']['host_capacity']['release'],
                         second['job']['queue']['host_capacity']['release'])
        self.assertEqual(second['job']['execution']['owned_process_group']['status'], 'unknown')
        self.assertEqual((second['job']['outcome'], second['job']['exit']), ('interrupted', 0))
        self.assertEqual(second['job']['recovery']['reason'], 'original failure')

    def test_reset_between_claim_and_launch_never_runs_the_command(self):
        jid = self.submit('usb-a', "import pathlib,sys;pathlib.Path(sys.argv[1],'executed').touch()")
        claimed = self.queue.claim_next(1)
        self.assertEqual(claimed['job_id'], jid)
        self.assertTrue(self.jobs.reset_queue(jid, 'fixture: reset claimed before launch')['released'])
        self.jobs.dispatch.launch(claimed)
        self.assertFalse(Path(self.jobs.path(jid, 'launch.json')).exists())
        self.assertFalse(Path(self.jobs.path(jid, 'out', 'executed')).exists())
        self.assertEqual(self.queue.get(jid)['state'], 'done')

    def test_reset_signal_error_does_not_publish_a_release(self):
        jid = self.submit('usb-a')
        self.ended(jid)
        meta = self.jobs._read_meta(jid)
        group = meta['execution']['owned_process_group']
        group.update(status='unknown', error='query unavailable')
        self.jobs._write_meta(jid, meta)
        with patch('job_runtime._group_completion', return_value=group):
            self.jobs.dispatch.finalize(self.queue.get(jid))
            with patch('job_runtime.os.killpg', side_effect=PermissionError('fixture: signal denied')):
                with self.assertRaises(PermissionError):
                    self.jobs.reset_queue(jid, 'fixture: report real signal failure')
        self.assertEqual(self.queue.get(jid)['state'], 'blocked')
        self.assertIsNone(self.queue.get(jid)['host_capacity_release'])


if __name__ == '__main__':
    unittest.main()
