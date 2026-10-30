"""Central artifact bytes remain usable after their execution computer disconnects."""
import http.client
import errno
import json
import threading
import sys
import unittest
from unittest.mock import patch

import test_multi_node_jobs as job_fixture
from test_node_results import registration
import test_node_evaluation as evaluation_fixture


class MultiNodeArtifactsTests(unittest.TestCase):
    def setUp(self):
        self.flow = job_fixture.MultiNodeJobTests()
        self.flow.setUp()
        self.addCleanup(self.flow.doCleanups)
        self.center, self.worker = self.flow.center, self.flow.worker
        from artifact_io import SafeRoot
        store = SafeRoot(self.worker.jobs.data_root)
        try:
            store.write_json('_node/center.json', self.flow.config)
        finally:
            store.close()

    def get(self, path, headers=None):
        connection = http.client.HTTPConnection(*self.center.server.server_address, timeout=15)
        try:
            connection.request('GET', path, headers={'Authorization': 'Bearer ' + self.flow.agent, **(headers or {})})
            reply = connection.getresponse()
            return reply.status, dict(reply.getheaders()), reply.read()
        finally:
            connection.close()

    def test_finished_outputs_are_collected_in_chunks_and_results_survive_a_disconnected_worker(self):
        connector = self.flow.start_worker()
        finished, collected = threading.Event(), threading.Event()
        commit, update = self.worker.jobs.dispatch._commit_completion, self.center.jobs.center.targets.update
        ranges = []
        request = connector.backend.request

        def called(method, path, body, headers, **options):
            if '/out?' in path and 'Range' in headers:
                ranges.append(headers['Range'])
            return request(method, path, body, headers, **options)

        connector.backend.request = called

        def committed(*args, **kwargs):
            value = commit(*args, **kwargs)
            finished.set()
            return value

        def stored(jid, **fields):
            value = update(jid, **fields)
            if fields.get('artifacts_sync', {}).get('status') == 'complete':
                collected.set()
            return value

        program = ("from pathlib import Path; import sys; out=Path(sys.argv[1]); "
                   "(out/'report.json').write_text('{\"timing\":{\"gpu_ms\":2.413}}'); "
                   "(out/'large.bin').write_bytes(b'abcdefgh' * 400001); "
                   "(out/'image.png').write_bytes(%r); print('archived-original-log')" % evaluation_fixture.png())
        spec = {'bin': 'fixture', 'argv': ['-c', program, '@out'], 'device_access': 'none',
                'execution_node_id': self.worker.jobs.node_id}
        with patch.object(self.worker.jobs.dispatch, '_commit_completion', side_effect=committed), \
                patch.object(self.center.jobs.center.targets, 'update', side_effect=stored):
            code, job = self.flow.submit(spec, 'collect-originals')
            self.assertEqual(code, 200, job)
            self.assertTrue(finished.wait(10))
            self.center.call('GET', '/jobs/' + job['id'])
            code, listing = self.center.call('GET', '/jobs/' + job['id'] + '/artifacts')
            self.assertEqual(code, 200, listing)
            self.assertTrue(collected.wait(15), 'finished originals were not collected in the background')
        self.assertGreaterEqual(len(ranges), 2)
        connector.close()
        code, _, data = self.get('/jobs/' + job['id'] + '/out?path=large.bin')
        self.assertEqual(code, 200)
        self.assertEqual(data, b'abcdefgh' * 400001)
        self.assertIn(b'archived-original-log', self.get('/jobs/' + job['id'] + '/log')[2])
        code, reference = self.center.call('GET', '/jobs/' + job['id'] + '/input-reference?path=report.json')
        self.assertEqual(code, 200, reference)
        manifest = registration('remote-result', job['id'])
        manifest['sources'] += [{'name': name, 'kind': 'job_output', 'job_id': job['id'], 'path': 'image.png'}
                                for name in ('image-a', 'image-b')]
        code, result = self.center.call('POST', '/results', manifest,
            token=self.flow.agent, headers={'X-Expected-Node-ID': self.center.jobs.node_id})
        self.assertEqual(code, 201, result)
        self.assertEqual(result['metric_evidence'][0]['status'], 'matched_source')
        self.assertEqual(result['provenance']['execution_node_id'], self.worker.jobs.node_id)
        url = next(item['url'] for item in result['current']['sources'] if item['name'] == 'report')
        self.assertEqual(self.get(url)[0], 200)
        envelope = evaluation_fixture.NodeEvaluationTests.envelope
        code, study = self.center.call('POST', '/research-state/research/study', envelope('create-study',
            {'title': 'Offline media fixture', 'objective': 'Read collected originals'}),
            headers={'X-Expected-Node-ID': self.center.jobs.node_id})
        self.assertEqual(code, 201, study)
        refs = [{'kind': 'result_source', 'node_id': self.center.jobs.node_id,
                 'result_id': 'remote-result', 'source_name': name} for name in ('image-a', 'image-b')]
        question = evaluation_fixture.NodeEvaluationTests.question(None, refs)
        code, evaluation = self.center.call('POST', '/research-state/request/offline-images',
            envelope('create-question', question), headers={'X-Expected-Node-ID': self.center.jobs.node_id})
        self.assertEqual(code, 201, evaluation)
        code, headers, image = self.get('/research-state/request/offline-images/media/A?revision=1',
                                       {'X-Expected-Node-ID': self.center.jobs.node_id})
        self.assertEqual((code, headers.get('Content-Type'), image), (200, 'image/png', evaluation_fixture.png()))
        self.assertEqual(self.center.jobs.dispatch.queue.rows(), [])

    def test_interrupted_copy_keeps_its_prefix_and_disk_failure_never_reserves_execution(self):
        connector = self.flow.start_worker()
        finished, failed = threading.Event(), threading.Event()
        commit, update = self.worker.jobs.dispatch._commit_completion, self.center.jobs.center.targets.update
        request = connector.backend.request
        ranges = []

        def interrupted(method, path, body, headers, **options):
            result = request(method, path, body, headers, **options)
            if 'path=large.bin' in path and 'Range' in headers:
                ranges.append(headers['Range'])
                if headers['Range'].startswith('bytes=1048576-'):
                    result['body'] = result['body'][:11]
            return result

        connector.backend.request = interrupted

        def committed(*args, **kwargs):
            value = commit(*args, **kwargs)
            finished.set()
            return value

        def stored(jid, **fields):
            value = update(jid, **fields)
            if fields.get('artifacts_sync', {}).get('status') == 'error':
                failed.set()
            return value

        program = ("from pathlib import Path; import sys; p=Path(sys.argv[1]); "
                   "(p/'large.bin').write_bytes(b'x'*2500000); (p/'small.json').write_bytes(b'{}')")
        spec = {'bin': 'fixture', 'argv': ['-c', program, '@out'], 'device_access': 'none',
                'execution_node_id': self.worker.jobs.node_id}
        with patch.object(self.worker.jobs.dispatch, '_commit_completion', side_effect=committed), \
                patch.object(self.center.jobs.center.targets, 'update', side_effect=stored):
            code, job = self.flow.submit(spec, 'partial-originals')
            self.assertEqual(code, 200, job)
            self.assertTrue(finished.wait(10))
            self.center.call('GET', '/jobs/' + job['id'])
            self.assertTrue(failed.wait(10))
        status = self.center.jobs.status(job['id'])
        self.assertEqual((status['state'], status['queue']['state']), ('finished', 'done'))
        self.assertEqual(status['artifacts_sync']['status'], 'error')
        code, progress = self.center.call('GET', '/jobs/' + job['id'] + '/artifacts-sync')
        self.assertEqual(code, 200, progress)
        large = next(item for item in progress['files'] if item['path'] == 'large.bin')
        self.assertEqual((large['received_bytes'], large['complete']), (1048576, False))
        with self.assertRaises(FileNotFoundError):
            self.center.jobs._read_root.open_file(job['id'] + '/out/large.bin')
        # A different complete file can be used while the large transfer is broken.
        code, reference = self.center.call('GET', '/jobs/' + job['id'] + '/input-reference?path=small.json')
        self.assertEqual(code, 200, reference)
        with self.subTest('complete file remains readable while its sibling is incomplete and the node is offline'), \
                patch.object(self.center.jobs.center, 'rpc', side_effect=ConnectionError('fixture offline')):
            code, _, original = self.get('/jobs/' + job['id'] + '/out?path=small.json')
            self.assertEqual((code, original), (200, b'{}'))
        connector.backend.request = request
        with patch.object(self.center.jobs._read_root, 'append_file', side_effect=OSError(errno.ENOSPC, 'fixture disk full')):
            self.center.jobs.center.artifacts.collect(job['id'])
            with self.subTest('HTTP reports storage failure without publishing the partial file'):
                code, _, error = self.get('/jobs/' + job['id'] + '/out?path=large.bin')
                self.assertEqual(code, 507, error)
        self.assertIn('fixture disk full', self.center.jobs.status(job['id'])['artifacts_sync']['error'])
        self.assertEqual(self.worker.jobs.status(job['id'])['queue']['host_capacity']['state'], 'released')
        resumed = []

        def resumed_read(method, path, body, headers, **options):
            if 'path=large.bin' in path and 'Range' in headers:
                resumed.append(headers['Range'])
            return request(method, path, body, headers, **options)

        connector.backend.request = resumed_read
        self.center.jobs.center.artifacts.collect(job['id'])
        self.assertTrue(resumed[0].startswith('bytes=1048576-'), resumed)
        self.assertEqual(self.center.jobs.status(job['id'])['artifacts_sync']['status'], 'complete')
        self.assertEqual(self.get('/jobs/' + job['id'] + '/out?path=large.bin')[2], b'x' * 2500000)
        self.assertEqual(len(self.worker.jobs.dispatch.queue.rows()), 1)

    def test_corrupt_completed_transfer_can_be_fetched_again_without_reexecuting(self):
        connector = self.flow.start_worker()
        completed, failed = threading.Event(), threading.Event()
        commit, update = self.worker.jobs.dispatch._commit_completion, self.center.jobs.center.targets.update
        request = connector.backend.request
        corrupt = True
        def received(method, path, body, headers, **options):
            reply = request(method, path, body, headers, **options)
            if corrupt and 'path=original' in path and 'Range' in headers:
                reply['body'] = b'X' + reply['body'][1:]
            return reply
        connector.backend.request = received
        def committed(*args, **kwargs):
            value = commit(*args, **kwargs)
            completed.set()
            return value
        def stored(jid, **fields):
            value = update(jid, **fields)
            if fields.get('artifacts_sync', {}).get('status') == 'error':
                failed.set()
            return value
        spec = {'bin': 'fixture', 'argv': ['-c',
            "from pathlib import Path; import sys; Path(sys.argv[1]).write_bytes(b'original bytes')", '@out/original'],
            'device_access': 'none', 'execution_node_id': self.worker.jobs.node_id}
        with patch.object(self.worker.jobs.dispatch, '_commit_completion', side_effect=committed), \
                patch.object(self.center.jobs.center.targets, 'update', side_effect=stored):
            code, job = self.flow.submit(spec, 'corrupt-transfer')
            self.assertEqual(code, 200, job)
            self.assertTrue(completed.wait(10))
            self.center.call('GET', '/jobs/' + job['id'])
            self.assertTrue(failed.wait(10))
        self.assertFalse((self.center.root / 'jobs' / job['id'] / 'out' / 'original').exists())
        corrupt = False
        self.center.jobs.center.artifacts.collect(job['id'])
        self.assertEqual(self.center.jobs.status(job['id'])['artifacts_sync']['status'], 'complete')
        self.assertEqual(self.get('/jobs/' + job['id'] + '/out?path=original')[2], b'original bytes')
        self.assertEqual(len(self.worker.jobs.dispatch.queue.rows()), 1)

    def test_live_prefix_and_finished_directory_use_center_urls(self):
        import socket
        import io
        import tarfile
        self.flow.start_worker()
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        listener.settimeout(10)
        program = ("from pathlib import Path; import sys,socket; p=Path(sys.argv[1]); "
                   "p.mkdir(); f=p/'sample'; f.write_bytes(b'prefix'); "
                   "s=socket.create_connection(%r); s.sendall(b'ready'); s.recv(1); "
                   "f.write_bytes(b'prefix-complete')" % (listener.getsockname(),))
        code, job = self.flow.submit({'bin': 'fixture', 'argv': ['-c', program, '@out/nested'],
            'device_access': 'none', 'execution_node_id': self.worker.jobs.node_id}, 'live-file')
        self.assertEqual(code, 200, job)
        self.addCleanup(self.worker.jobs.reset_queue, job['id'], 'fixture cleanup')
        child, _ = listener.accept()
        self.addCleanup(child.close)
        self.assertEqual(child.recv(5), b'ready')
        self.assertEqual(self.get('/jobs/' + job['id'] + '/out?path=nested/sample')[2], b'prefix')
        self.assertEqual(self.center.call('GET', '/jobs/' + job['id'] + '/input-reference?path=nested/sample')[0], 409)
        finished = threading.Event()
        commit = self.worker.jobs.dispatch._commit_completion
        def committed(*args, **kwargs):
            value = commit(*args, **kwargs)
            finished.set()
            return value
        with patch.object(self.worker.jobs.dispatch, '_commit_completion', side_effect=committed):
            child.sendall(b'x')
            self.assertTrue(finished.wait(10))
        self.center.call('GET', '/jobs/' + job['id'])
        code, _, archive = self.get('/jobs/' + job['id'] + '/out?path=nested')
        self.assertEqual(code, 200, archive)
        with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as bundle:
            member = next(item for item in bundle.getmembers() if item.isfile())
            self.assertEqual(bundle.extractfile(member).read(), b'prefix-complete')

    def test_worker_consumes_a_finished_center_input_without_another_agent_token(self):
        connector = self.flow.start_worker()
        self.center.jobs.bins['fixture'] = (sys.executable, str(self.center.root))
        finished = threading.Event()
        commit = self.center.jobs.dispatch._commit_completion

        def committed(*args, **kwargs):
            value = commit(*args, **kwargs)
            finished.set()
            return value

        source = {'bin': 'fixture', 'argv': ['-c', "from pathlib import Path; import sys; Path(sys.argv[1]).write_bytes(b'original input')", '@out/source'],
                  'device_access': 'none'}
        with patch.object(self.center.jobs.dispatch, '_commit_completion', side_effect=committed):
            code, source_job = self.flow.submit(source, 'central-input')
            self.assertEqual(code, 200, source_job)
            self.assertTrue(finished.wait(10))
        code, reference = self.center.call('GET', '/jobs/' + source_job['id'] + '/input-reference?path=source')
        self.assertEqual(code, 200, reference)
        inputs = {'schema': 'phonefarm/job-inputs@1', 'node_id': self.center.jobs.node_id,
                  'cwd': 'default', 'files': [{'name': 'original', 'target': 'original', 'source': reference['source']}],
                  'bindings': []}
        consumer = {'bin': 'fixture', 'argv': ['-c',
            "from pathlib import Path; import sys; Path(sys.argv[2]).write_bytes(Path(sys.argv[1]).read_bytes())", '@in/original', '@out/copied'],
            'device_access': 'none', 'inputs': inputs, 'execution_node_id': self.worker.jobs.node_id}
        copied = threading.Event()
        worker_commit = self.worker.jobs.dispatch._commit_completion

        def worker_committed(*args, **kwargs):
            value = worker_commit(*args, **kwargs)
            copied.set()
            return value

        with patch.object(self.worker.jobs.dispatch, '_commit_completion', side_effect=worker_committed):
            code, job = self.flow.submit(consumer, 'consume-central')
            self.assertEqual(code, 200, job)
            self.assertTrue(copied.wait(10))
        code, status = self.center.call('GET', '/jobs/' + job['id'])
        self.assertEqual((code, status['exit']), (200, 0), status)
        self.assertEqual(self.get('/jobs/' + job['id'] + '/out?path=copied')[2], b'original input')
        self.assertEqual(self.flow.submit(consumer, 'consume-central')[1]['id'], job['id'])
        self.assertEqual(len(self.worker.jobs.dispatch.queue.rows()), 1)
        self.center.jobs.center.artifacts.collect(job['id'])
        connector.close()
        code, input_status = self.center.call('GET', '/jobs/' + job['id'] + '/inputs')
        self.assertEqual((code, input_status.get('declared')), (200, inputs), input_status)
        self.assertEqual(input_status['preparation']['status'], 'ready')
        self.assertEqual(input_status['node_id'], self.center.jobs.node_id)



if __name__ == '__main__':
    unittest.main()
