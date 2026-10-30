"""An enrolled computer executes center jobs without creating a second business store."""
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import test_node_games as fixture
from node_connector import join, LocalHTTP, NodeConnector
from node_service import Jobs, make_handler


class ExecutionNodeRoleTests(unittest.TestCase):
    def setUp(self):
        self.center = fixture.GameFlowTests()
        self.center.setUp()
        self.addCleanup(self.center.tearDown)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.token = 'isolated-worker-administration-token-12345'
        invitation = self.center.jobs.directory.invite()
        join(self.center.url, invitation['code'], 'enrolled', str(self.root))
        self.config = json.loads((self.root / '_node/center.json').read_text())
        self.start_worker()
        self.addCleanup(self.stop_worker)

    def execution_bins(self):
        return {'fixture': (sys.executable, str(self.root))}

    def start_worker(self):
        self.jobs = Jobs(str(self.root), self.execution_bins(), data_dir=str(self.root))
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(None, self.jobs, self.token))
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)
        self.connector = NodeConnector(self.config, LocalHTTP(self.url, self.token, self.jobs.node_id))
        reported = threading.Event()
        post = self.connector._json
        def report(path, value):
            response = post(path, value)
            if path == '/node-connect/report':
                reported.set()
            return response
        self.connector._json = report
        self.connector.start()
        self.assertTrue(reported.wait(5), 'enrolled computer did not report')

    def stop_worker(self):
        self.connector.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.jobs.dispatch.close()
        self.jobs.center.close()
        self.jobs.fleet.close()
        if self.jobs.fleet._thread:
            self.jobs.fleet._thread.join()
        for name in ('directory', 'games', 'history', 'export_cache', '_read_root'):
            value = getattr(self.jobs, name, None)
            if value is not None:
                value.close()

    def call(self, method, path, value=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=10)
        try:
            connection.request(method, path, body=None if value is None else json.dumps(value), headers={
                'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_joined_computer_has_only_execution_and_local_administration(self):
        with self.subTest('no second business stores'):
            self.assertEqual([name for name in ('_research', '_results', '_node/agents', '_games/catalog', '_node/directory')
                              if (self.root / name).exists()], [])
        for path, body in [('/agents', {'name': 'duplicate'}), ('/node-invites', {}), ('/games', self.center.manifest())]:
            with self.subTest(path=path):
                self.assertEqual(self.call('POST', path, body)[0], 404)
        self.assertEqual(self.call('GET', '/node')[0], 200)
        self.assertEqual(self.call('GET', '/queue')[0], 200)
        done = threading.Event()
        commit = self.jobs.dispatch._commit_completion
        def committed(*args, **kwargs):
            result = commit(*args, **kwargs)
            done.set()
            return result
        with patch.object(self.jobs.dispatch, '_commit_completion', side_effect=committed):
            code, job = self.center.call('POST', '/jobs', headers={'X-Request-ID': 'executor-only', 'X-Job': json.dumps({
                'bin': 'fixture', 'argv': ['-c', "print('from-enrolled-computer')"], 'device_access': 'none',
                'execution_node_id': self.jobs.node_id})})
            self.assertEqual(code, 200, job)
            self.assertTrue(done.wait(10), job)
        self.assertEqual(self.center.call('GET', '/jobs/' + job['id'])[1]['queue']['state'], 'done')
        self.assertEqual(self.call('POST', '/jobs/' + job['id'] + '/reset', {'note': 'local administration'})[0], 200)

    def test_service_restart_preserves_identity_connection_and_running_worker(self):
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        listener.settimeout(10)
        program = "import socket; s=socket.create_connection(%r); s.sendall(b'ready'); s.recv(1)" % (listener.getsockname(),)
        spec = {'bin': 'fixture', 'argv': ['-c', program], 'device_access': 'none', 'execution_node_id': self.jobs.node_id}
        code, job = self.center.call('POST', '/jobs', headers={'X-Request-ID': 'survive-restart', 'X-Job': json.dumps(spec)})
        self.assertEqual(code, 200, job)
        child, _ = listener.accept()
        self.addCleanup(child.close)
        self.assertEqual(child.recv(5), b'ready')
        identity = self.jobs.node_id
        self.stop_worker()
        self.start_worker()
        self.assertEqual((self.jobs.node_id, json.loads((self.root / '_node/center.json').read_text())), (identity, self.config))
        self.assertEqual(self.jobs.status(job['id'])['queue']['state'], 'claimed')
        done = threading.Event()
        commit = self.jobs.dispatch._commit_completion
        def committed(*args, **kwargs):
            result = commit(*args, **kwargs)
            done.set()
            return result
        with patch.object(self.jobs.dispatch, '_commit_completion', side_effect=committed):
            child.sendall(b'x')
            self.assertTrue(done.wait(10))
        self.assertEqual(len(self.jobs.dispatch.queue.rows()), 1)
        self.assertEqual(len(self.center.jobs.directory.list()), 1)

    def test_preexisting_business_files_are_left_unchanged_after_enrollment(self):
        self.stop_worker()
        roots = ('_games', '_research', '_results', '_node/agents', '_node/directory')
        for name in roots:
            source = self.center.root / name
            if source.exists():
                shutil.copytree(source, self.root / name)
        def originals():
            return {str(file.relative_to(self.root)): (file.read_bytes(), file.stat().st_mtime_ns)
                    for name in roots for file in (self.root / name).rglob('*') if file.is_file()}
        before = originals()
        self.assertTrue(before)
        self.start_worker()
        self.assertEqual(self.call('GET', '/games')[0], 404)
        self.assertEqual(self.call('POST', '/agents', {'name': 'unused'})[0], 404)
        self.assertTrue(all(getattr(self.jobs, name) is None
                            for name in ('games', 'research', 'results', 'agents', 'directory')))
        self.assertEqual(originals(), before)


if __name__ == '__main__':
    unittest.main()
