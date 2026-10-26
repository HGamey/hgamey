"""The actual worker supplies installation identity, independent of data root."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

NODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NODE))
from node_service import Jobs
import job_runtime
import node_update


@unittest.skipUnless(Path('/bin/zsh').exists(), 'zsh required')
class UpdateDispatchTests(unittest.TestCase):
    def test_real_worker_updates_only_configured_code_and_keeps_recovery_across_data_roots(self):
        with tempfile.TemporaryDirectory(prefix='managed-code-update-') as tmp:
            root = Path(tmp).resolve()
            pf, gol, harness = (root / name for name in ('pf source', 'gol source', 'node source'))
            for path in (pf, gol, harness):
                path.mkdir()
            for name in ('node_update.py', 'update_node.sh'):
                shutil.copy2(NODE / name, harness / name)
            installed = pf / 'module.py'
            installed.write_text('original\n')
            evidence = pf / 'runs' / 'preserved.json'
            evidence.parent.mkdir()
            evidence.write_text('{"original":true}\n')
            upload = root / 'upload'
            (upload / 'phonefarm').mkdir(parents=True)
            (upload / 'phonefarm' / 'module.py').write_text('updated\n')
            bins = {'phonefarm': (str(pf / 'phonefarm'), str(pf)),
                    'game_opt_loop': (str(gol / 'game_opt_loop'), str(gol)),
                    'host': (sys.executable, str(pf)),
                    'update_node': (str(harness / 'update_node.sh'), str(root))}
            env = {'HG_NODE_PYTHON': sys.executable, 'PATH': os.defpath,
                   'PYTHONDONTWRITEBYTECODE': '1', 'HOME': str(root)}
            workers, launch = [], job_runtime.launch

            def tracked(*args, **kwargs):
                process = launch(*args, **kwargs)
                workers.append(process)
                if len(workers) == 1:
                    # The production dispatcher holds intake across launch and
                    # its first status observation. Keep that real lock until
                    # the child reaches publication, making the former CI race
                    # deterministic without making it publish under the lock.
                    log = Path(args[2]) / 'log'
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        if log.exists() and 'waiting for node intake before checking publication' in log.read_text():
                            break
                        if process.poll() is not None:
                            self.fail('updater exited while its launcher still held intake: ' + log.read_text())
                        time.sleep(.01)
                    else:
                        self.fail('updater did not reach the held publication gate')
                    self.assertEqual(installed.read_text(), 'original\n')
                return process

            with patch.dict(os.environ, env, clear=True), patch('job_dispatch.job_runtime.launch', side_effect=tracked):
                jobs = Jobs(str(pf), bins)
                other = Jobs(str(pf), bins, data_dir=str(root / 'moved data'))
                try:
                    self.assertEqual(jobs.update_layout, other.update_layout)
                    self.assertEqual(jobs.update_layout['harness_ops'], str(harness))
                    jid = jobs.start({'bin': 'update_node', 'argv': [str(upload)],
                                      'device_access': 'none', 'resources': []}, b'', 'managed-update')['id']
                    deadline = time.monotonic() + 15
                    while jobs.dispatch.thread is not None and time.monotonic() < deadline:
                        time.sleep(.02)
                    self.assertIsNone(jobs.dispatch.thread, 'isolated update worker did not finish')
                    status = jobs.status(jid)
                    self.assertEqual(status.get('exit'), 0, Path(jobs.path(jid, 'log')).read_text())
                    self.assertEqual(installed.read_text(), 'updated\n')
                    self.assertEqual(evidence.read_text(), '{"original":true}\n')
                    receipts = list(Path(jobs.update_layout['state_dir']).glob('runs/*/receipt.json'))
                    self.assertEqual(len(receipts), 1)
                    receipt = json.loads(receipts[0].read_text())
                    self.assertEqual(receipt['status'], 'succeeded')
                    self.assertEqual(receipt['guard']['job_id'], jid)
                    self.assertTrue(receipt['guard']['managed_jobs_checked'])
                    self.assertFalse(receipt['guard']['all_participants_verified'])
                    # Model a persisted maintenance obligation after process
                    # death. No filesystem/power-loss claim is made here.
                    node_update.begin_maintenance(jobs.update_layout, receipt['run_id'])
                    stamp = root / 'ordinary-job-started'
                    with patch.object(jobs.dispatch, 'start'):
                        waiting_id = jobs.start({'bin': 'host', 'argv': ['-c',
                            'from pathlib import Path; Path(' + repr(str(stamp)) + ').touch()'],
                            'device_access': 'none', 'resources': []}, b'', 'ordinary-after-update')['id']
                    jobs.dispatch.tick()
                    self.assertEqual(jobs.dispatch.queue.get(waiting_id)['state'], 'queued')
                    self.assertEqual(jobs.status(waiting_id)['queue']['waiting']['kind'], 'node_update')
                    self.assertFalse(stamp.exists())
                    rollback_id = jobs.start({'bin': 'update_node', 'argv': ['--rollback', receipt['run_id']],
                                              'device_access': 'none', 'resources': []}, b'', 'rollback-original')['id']
                    deadline = time.monotonic() + 15
                    while jobs.dispatch.thread is not None and time.monotonic() < deadline:
                        time.sleep(.02)
                    self.assertIsNone(jobs.dispatch.thread, 'rollback or queued host worker did not finish')
                    self.assertEqual(jobs.status(rollback_id).get('exit'), 0, Path(jobs.path(rollback_id, 'log')).read_text())
                    self.assertEqual(jobs.status(waiting_id).get('exit'), 0)
                    self.assertTrue(stamp.exists())
                    self.assertEqual(installed.read_text(), 'original\n')
                    self.assertEqual(json.loads(receipts[0].read_text())['status'], 'restored')
                finally:
                    for worker in workers:
                        try:
                            worker.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            worker.terminate()
                            worker.wait(timeout=5)
                    jobs._read_root.close()
                    other._read_root.close()


if __name__ == '__main__':
    unittest.main()
