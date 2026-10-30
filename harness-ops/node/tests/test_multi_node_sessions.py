"""Session recovery keeps its original computer, source and public child identity."""
import json
import sys
import threading
import unittest
from unittest.mock import patch

import test_multi_node_jobs as fixture
from test_job_session import RECOVERY_HELPER, SESSION_ARGV


class MultiNodeSessionTests(unittest.TestCase):
    def setUp(self):
        self.flow = fixture.MultiNodeJobTests()
        self.flow.setUp()
        self.addCleanup(self.flow.doCleanups)
        self.center, self.worker = self.flow.center, self.flow.worker
        helper = self.worker.root / 'phonefarm-fixture'
        helper.write_text('#!' + sys.executable + '\n' + RECOVERY_HELPER)
        helper.chmod(0o755)
        self.worker.jobs.bins['phonefarm'] = (str(helper), str(self.worker.root))
        self.worker.jobs.programs.refresh()
        # This helper exercises real workers, original output paths and HTTP,
        # while PF fd exclusion has its separate real-PF integration suite.
        self.leases = patch.object(self.worker.jobs.leases, 'snapshot', return_value={'status': 'clear', 'entries': []})
        self.leases.start()
        self.addCleanup(self.leases.stop)
        self.flow.start_worker()

    def completion(self, action):
        done = threading.Event()
        commit = self.worker.jobs.dispatch._commit_completion
        def committed(*args, **kwargs):
            value = commit(*args, **kwargs)
            done.set()
            return value
        with patch.object(self.worker.jobs.dispatch, '_commit_completion', side_effect=committed):
            code, job = action()
            self.assertEqual(code, 200, job)
            self.assertTrue(done.wait(10), job)
        code, status = self.center.call('GET', '/jobs/' + job['id'])
        self.assertEqual((code, status['state'], status['queue']['state']), (200, 'finished', 'done'), status)
        return status

    def test_recovery_runs_once_on_the_original_node_and_returns_a_center_job(self):
        source = self.completion(lambda: self.flow.submit({'bin': 'phonefarm', 'argv': SESSION_ARGV,
            'device_access': 'write', 'execution_node_id': self.worker.jobs.node_id}, 'gray-source'))
        jid = source['id']
        code, session = self.center.call('GET', '/jobs/' + jid + '/session', token=self.flow.agent)
        self.assertEqual((code, session.get('serial')), (200, 'fake-device'), session)
        code, plan = self.center.call('GET', '/jobs/' + jid + '/session/recovery', token=self.flow.agent)
        self.assertEqual((code, plan.get('eligible')), (200, True), plan)
        self.assertEqual(plan['node_id'], self.center.jobs.node_id)
        path = '/jobs/' + jid + '/session/recover'
        child = self.completion(lambda: self.center.call('POST', path, {}, token=self.flow.agent,
                                                       headers={'X-Request-ID': 'gray-recover'}))
        self.assertEqual(child['execution_node_id'], self.worker.jobs.node_id)
        self.assertEqual(child['recovery_for'], jid)
        with self.center.jobs.center.artifacts.open_output(jid, 'session-control.json') as stream:
            recovered = json.load(stream)
        self.assertEqual(len(recovered['recovery']['reports']), 2)
        self.assertEqual(child['exit'], 2, child)  # Failed recovery remains a failed attempt.
        self.assertEqual(self.center.jobs.dispatch.queue.rows(), [])
        code, repeated = self.center.call('POST', path, {}, token=self.flow.agent,
                                        headers={'X-Request-ID': 'gray-recover'})
        self.assertEqual((code, repeated['id']), (200, child['id']))
        self.assertEqual(len(self.worker.jobs.dispatch.queue.rows()), 2)
        observed = json.loads((self.worker.root / 'jobs' / child['id'] / 'out' / 'observed.json').read_text())
        self.assertEqual(observed['source'], self.worker.jobs.path(jid, 'out'))
        self.assertTrue((self.worker.root / 'jobs' / jid / 'out' / 'session-control.json').exists())
        self.assertEqual(self.center.call('GET', '/jobs/' + child['id'] + '/session/recovery')[1]['target_job_id'], child['id'])


if __name__ == '__main__':
    unittest.main()
