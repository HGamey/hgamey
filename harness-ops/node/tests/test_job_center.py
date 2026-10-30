"""One business intake, a local execution source, and unchanged public job IDs."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
import hashlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from node_service import Jobs
from node_read import NodeRead
import test_node_games as fixture
from test_node_results import registration
import job_inputs


class JobCenterTests(unittest.TestCase):
    def fixture(self):
        env = fixture.GameFlowTests()
        env.setUp()
        self.addCleanup(env.tearDown)
        env.jobs.dispatch.close()
        env.jobs.dispatch.start = lambda: None
        env.jobs.bins['host'] = (sys.executable, str(env.root))
        return env

    def test_frozen_business_context_is_accepted_without_a_worker_research_database(self):
        env = self.fixture()
        env.jobs.research.write('research', 'central', {
            'schema': 'phonefarm/research-write@1', 'operation_id': 'create-central',
            'base_revision': None, 'actor_label': None, 'reason': None,
            'data': {'title': 'Central research', 'objective': 'one business store',
                     'device_requirements': ['usb-a']}}, 'fixture')
        spec = {'bin': 'host', 'argv': ['-c', 'pass'], 'device_access': 'none',
                'actor_id': 'researcher', 'research_id': 'central'}
        request = env.jobs.center.prepare_request(spec)
        with tempfile.TemporaryDirectory() as root:
            worker = Jobs(root, {'host': (sys.executable, root)})
            worker.dispatch.start = lambda: None
            try:
                self.assertFalse(hasattr(worker, 'research'))
                job = worker.accept(request, b'', 'center-request', job_id='abcdef123456')
                self.assertEqual(job['id'], 'abcdef123456')
                self.assertEqual(job['research_id'], 'central')
                self.assertEqual(job['actor_id'], 'researcher')
                self.assertEqual(job['queue']['state'], 'queued')
                self.assertEqual(request['research_devices'], ['usb-a'])
                row = worker.dispatch.queue.get(job['id'])
                self.assertEqual(row['spec']['research_id'], 'central')
                self.assertEqual(worker.accept(request, b'', 'center-request',
                                              job_id='111111111111')['id'], job['id'])
                self.assertTrue(worker.reset_queue(job['id'], 'test cleanup')['released'])
            finally:
                worker.dispatch.close()
                worker.export_cache.close()
                worker._read_root.close()

    def test_public_job_identity_and_request_survive_restart_with_one_local_queue(self):
        env = self.fixture()
        headers = {'X-Job': json.dumps({'bin': 'host', 'argv': ['-c', 'pass']}),
                   'X-Request-ID': 'public-id'}
        code, job = env.call('POST', '/jobs', headers=headers)
        self.assertEqual(code, 200, job)
        self.assertEqual(job['execution_node_id'], env.jobs.node_id)
        self.assertEqual(env.call('GET', '/jobs/' + job['id'])[1]['execution_node_id'], env.jobs.node_id)
        self.assertIn(job['id'], env.jobs.center.job_ids())
        restarted = Jobs(str(env.root), env.jobs.bins, data_dir=str(env.root))
        restarted.dispatch.start = lambda: None
        try:
            NodeRead(restarted, restarted.data_root, str(env.root))
            replay = restarted.start({'bin': 'host', 'argv': ['-c', 'pass']}, b'', 'public-id')
            self.assertEqual(replay['id'], job['id'])
            self.assertEqual(replay['execution_node_id'], job['execution_node_id'])
            self.assertEqual(len(restarted.dispatch.queue.rows()), 1)
            self.assertEqual(restarted.center.job_ids(), {job['id']})
            self.assertTrue(restarted.reset_queue(job['id'], 'test cleanup')['released'])
        finally:
            restarted.dispatch.close()
            restarted.export_cache.close()
            restarted._read_root.close()

    def test_result_and_input_read_the_same_artifact_source_outside_the_center_root(self):
        env = self.fixture()
        with tempfile.TemporaryDirectory() as root:
            worker = Jobs(root, {'host': (sys.executable, root)})
            worker.dispatch.start = lambda: None
            try:
                request = env.jobs.center.prepare_request({'bin': 'host', 'argv': ['-c', 'pass']})
                job = worker.accept(request, b'', 'artifact-source', job_id='fedcba123456')
                jid = job['id']
                # Synthetic archived output: source completion is still read from its own queue.
                meta = worker._read_meta(jid)
                meta.update(state='finished', phase='finished', outcome='succeeded', exit=0)
                meta['execution'].update(worker_alive=False)
                worker._write_meta(jid, meta)
                worker.dispatch.queue.set_state(jid, 'done')
                data = b'{"timing":{"gpu_ms":2.413}}'
                Path(worker.path(jid, 'out', 'report.json')).write_bytes(data)
                env.jobs.center.artifacts = worker.center.artifacts
                self.assertFalse(Path(env.jobs.path(jid)).exists())
                code, result = env.call('POST', '/results', registration('source-adapter', jid),
                                        headers={'X-Expected-Node-ID': env.jobs.node_id})
                self.assertEqual(code, 201, result)
                ref = job_inputs.reference(env.jobs, jid, 'report.json')
                self.assertEqual(ref['source']['sha256'], hashlib.sha256(data).hexdigest())
                self.assertEqual(ref['source']['size'], len(data))
                stream, info = env.jobs.results.source('source-adapter', 'report')
                with stream:
                    self.assertEqual(stream.read(), data)
                self.assertEqual(info['sha256'], ref['source']['sha256'])
                self.assertEqual(env.jobs.results.get('source-adapter')['current']['sources'][0]['status'], 'available')
                meta.update(state='unknown', phase='interrupted')
                worker._write_meta(jid, meta)
                worker.dispatch.queue.set_state(jid, 'done', {'kind': 'manual_unknown'})
                with self.assertRaisesRegex(job_inputs.InputError, 'has not finished'):
                    job_inputs.reference(env.jobs, jid, 'report.json')
            finally:
                worker.dispatch.close()
                worker.export_cache.close()
                worker._read_root.close()


if __name__ == '__main__':
    unittest.main()
