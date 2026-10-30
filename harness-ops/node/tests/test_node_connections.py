"""Nodes enroll and report over their outbound connection, with private fixtures."""
import http.client
import json
import multiprocessing
from pathlib import Path
import socket
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_node_games as fixture
from node_connector import NodeConnector, LocalHTTP, exchange, join
from node_connection_fixture import connect
from node_directory import NodeDirectory
from node_service import make_handler


def _center_without_outbound(pipe):
    env = fixture.GameFlowTests()
    env.setUp()
    try:
        with patch.object(socket.socket, 'connect', side_effect=AssertionError('center must not dial any worker')):
            pipe.send((env.url, env.admin, env.jobs.node_id))
            assert pipe.recv() == 'stop'
    finally:
        env.tearDown()
        pipe.close()


class NodeConnectionsTests(unittest.TestCase):
    def setUp(self):
        self.env = fixture.GameFlowTests()
        self.env.setUp()
        self.addCleanup(self.env.tearDown)

    def request(self, method, path, body=None, token=None):
        connection = http.client.HTTPConnection(*self.env.server.server_address, timeout=5)
        try:
            connection.request(method, path, body=json.dumps(body or {}), headers={
                'Content-Type': 'application/json', 'Authorization': 'Bearer ' + (token or self.env.admin)})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_exchange_rejects_a_response_cut_off_before_its_content_length(self):
        inherited = self.env.server.RequestHandlerClass

        class Interrupted(inherited):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Length', '100')
                self.end_headers()
                self.wfile.write(b'x' * 11)
                self.wfile.flush()
                self.close_connection = True

        self.env.server.RequestHandlerClass = Interrupted
        with self.assertRaises(http.client.IncompleteRead):
            exchange(self.env.url, 'GET', '/fixture-short-response', max_bytes=100)

    def test_reply_requires_both_outer_and_inner_content_lengths_before_delivery(self):
        directory = self.env.jobs.directory
        joined = directory.join({'invite': directory.invite()['code'], 'name': 'worker', 'node_id': 'b' * 32})
        link = directory.link
        for framing in ('outer', 'inner'):
            with self.subTest(framing=framing), ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(link.call, 'b' * 32,
                                      {'method': 'GET', 'path': '/log', 'headers': {}, 'max_bytes': 100})
                try:
                    command, = link.poll('b' * 32, wait=5)
                    path = '/node-connect/calls/' + command['id'] + '/reply'
                    headers = {'Authorization': 'Bearer ' + joined['token'],
                        'X-Node-Response': json.dumps({'status': 200, 'headers': {'Content-Length': '100'}})}
                    # Half-close a real HTTP request after only 11 of the declared bytes.
                    with socket.create_connection(self.env.server.server_address, timeout=5) as sock:
                        raw_headers = {**headers, 'Host': 'fixture', 'Connection': 'close',
                                       'Content-Length': '100' if framing == 'outer' else '11'}
                        sock.sendall(('POST ' + path + ' HTTP/1.1\r\n' + ''.join(
                            key + ': ' + value + '\r\n' for key, value in raw_headers.items()) + '\r\n').encode() + b'x' * 11)
                        sock.shutdown(socket.SHUT_WR)
                        response = http.client.HTTPResponse(sock)
                        response.begin()
                        self.assertEqual(response.status, 400, response.read())
                    self.assertFalse(pending.done(), 'a partial reply satisfied the waiting call')
                    response = exchange(self.env.url, 'POST', path, b'x' * 100, headers)
                    self.assertEqual(response['status'], 200, response)
                    self.assertEqual(pending.result(timeout=5)['body'], b'x' * 100)
                finally:
                    if not pending.done():
                        link.reply('b' * 32, command['id'], 500, {}, b'fixture cleanup')

    def test_invite_joins_once_reports_only_its_node_and_keeps_credentials_private(self):
        code, invite = self.request('POST', '/node-invites', {'note': 'isolated worker'})
        self.assertEqual(code, 201, invite)
        joining = {'invite': invite['code'], 'name': 'node-b', 'node_id': 'b' * 32}
        code, joined = self.request('POST', '/node-connect/join', joining, token='no-existing-credential')
        self.assertEqual(code, 201, joined)
        self.assertEqual(joined['node']['node_id'], 'b' * 32)
        self.assertEqual(self.request('POST', '/node-connect/join', joining)[0], 409)
        snapshot = {'at': 1234, 'identity': {'node_id': 'b' * 32},
                    'fleet': {'devices': [], 'sampled_at': 1200}, 'queue': {'jobs': []}}
        self.assertEqual(self.request('POST', '/node-connect/report', snapshot, token=joined['token'])[0], 200)
        code, entry = self.request('GET', '/nodes/node-b')
        self.assertEqual(code, 200, entry)
        self.assertEqual(entry['snapshot'], snapshot)
        self.assertEqual(entry['connection']['status'], 'ready')
        self.assertNotIn('url', entry)
        self.assertNotIn(joined['token'], json.dumps(entry))
        self.assertEqual(self.request('POST', '/node-connect/report', snapshot)[0], 401)
        self.assertEqual(self.request('POST', '/agents', {'name': 'not-an-agent'}, token=joined['token'])[0], 401)

    def test_outbound_only_center_can_query_a_worker_while_another_call_is_slow(self):
        context = multiprocessing.get_context('spawn')
        parent, child = context.Pipe()
        process = context.Process(target=_center_without_outbound, args=(child,))
        process.start()
        child.close()
        connector = None
        release = threading.Event()
        try:
            self.assertTrue(parent.poll(10), 'isolated center did not start')
            url, token, center_id = parent.recv()

            def call(method, path, data=None):
                response = exchange(url, method, path, b'' if data is None else json.dumps(data).encode(),
                    {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}, timeout=12)
                self.assertEqual(response['status'], 201 if path == '/node-invites' else 200, response['body'])
                return json.loads(response['body'])

            invite = call('POST', '/node-invites', {})
            join(url, invite['code'], 'node-b', self.env.jobs.data_root)
            config_path = Path(self.env.jobs.data_root, '_node', 'center.json')
            self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)
            config = json.loads(config_path.read_text())
            self.assertEqual(config['center_id'], center_id)
            backend = LocalHTTP(self.env.url, self.env.admin, self.env.jobs.node_id)
            request = backend.request
            entered = threading.Event()

            def slow(method, path, body, headers, **options):
                if path == '/node':
                    entered.set()
                    if not release.wait(10):
                        raise AssertionError('test did not release slow read')
                return request(method, path, body, headers, **options)

            backend.request = slow
            connector = NodeConnector(config, backend)
            reported = threading.Event()
            send = connector._json

            def observed_report(path, value):
                result = send(path, value)
                if path == '/node-connect/report':
                    reported.set()
                return result

            connector._json = observed_report
            connector.start()
            self.assertTrue(reported.wait(10), 'worker did not upload its snapshot')
            entry = call('GET', '/nodes/node-b')
            self.assertEqual(entry['snapshot']['identity']['node_id'], self.env.jobs.node_id)
            with ThreadPoolExecutor(max_workers=2) as pool:
                pending = pool.submit(call, 'GET', '/nodes/node-b/read/node')
                try:
                    self.assertTrue(entered.wait(5), 'outbound poll did not deliver the first read')
                    queue = call('GET', '/nodes/node-b/read/queue')
                    self.assertEqual(queue['node_id'], self.env.jobs.node_id)
                finally:
                    release.set()
                self.assertEqual(pending.result(timeout=5)['node_id'], self.env.jobs.node_id)
        finally:
            release.set()
            if connector is not None:
                connector.close()
            if process.is_alive():
                parent.send('stop')
            process.join(10)
            if process.is_alive():
                process.terminate()
                process.join()
            parent.close()
        self.assertEqual(process.exitcode, 0)

    def test_center_restart_reconnects_the_original_machine_without_a_new_invitation(self):
        worker = fixture.GameFlowTests()
        worker.setUp()
        self.addCleanup(worker.tearDown)
        connector = connect(self.env.jobs.directory, self.env.url, 'worker', worker.url,
                            worker.admin, worker.jobs.node_id)
        self.addCleanup(connector.close)
        token = connector.config['token']
        address = self.env.server.server_address
        interrupted = threading.Event()
        original = connector._json

        def observe(path, value):
            try:
                return original(path, value)
            except (OSError, ValueError, http.client.HTTPException):
                interrupted.set()
                raise

        connector._json = observe
        self.env.jobs.directory.close()
        self.env.server.shutdown()
        self.env.server.server_close()
        self.env.thread.join()
        self.assertTrue(interrupted.wait(5), 'connector never observed the center stopping')
        self.env.jobs.directory = NodeDirectory(self.env.jobs.data_root, self.env.jobs.node_id)
        self.env.server = ThreadingHTTPServer(address, make_handler(None, self.env.jobs, self.env.admin, self.env.board))
        self.env.server.daemon_threads = False
        self.env.thread = threading.Thread(target=self.env.server.serve_forever, daemon=True)
        self.env.thread.start()
        self.assertEqual(self.env.jobs.directory.verify(token), worker.jobs.node_id)
        connector._json('/node-connect/report', connector.backend.snapshot())
        reply = exchange(self.env.url, 'GET', '/nodes/worker/read/node', headers={
            'Authorization': 'Bearer ' + self.env.admin}, timeout=10)
        self.assertEqual(reply['status'], 200, reply['body'])
        self.assertEqual(json.loads(reply['body'])['node_id'], worker.jobs.node_id)
        self.assertEqual(len(self.env.jobs.directory.list()), 1)
        self.assertEqual(self.env.jobs.directory.invites(), [])

    def test_connector_shutdown_closes_a_pending_local_http_read_before_joining(self):
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()
        inherited = self.env.server.RequestHandlerClass

        class Waiting(inherited):
            def do_GET(self):
                if self.path == '/node':
                    entered.set()
                    release.wait(10)
                    self.close_connection = True
                    return
                super().do_GET()

        self.env.server.RequestHandlerClass = Waiting
        connector = NodeConnector({'url': self.env.url, 'token': 'unused', 'center_id': self.env.jobs.node_id},
                                  LocalHTTP(self.env.url, self.env.admin, self.env.jobs.node_id))
        connector._spawn(connector._execute, {'id': 'fixture', 'body_size': 0, 'method': 'GET',
                         'path': '/node', 'headers': {}, 'max_bytes': 1024})
        self.assertTrue(entered.wait(5))

        def close():
            connector.close()
            closed.set()

        closer = threading.Thread(target=close)
        closer.start()
        try:
            self.assertTrue(closed.wait(3), 'shutdown waited for a local HTTP reply instead of closing the socket')
        finally:
            release.set()
            closer.join(10)
        self.assertFalse(closer.is_alive())


if __name__ == '__main__':
    unittest.main()
