"""One center agent reaches independent PF owners through outbound node calls."""
import http.client
import json
import sys
import unittest
from urllib.parse import quote

import test_multi_node_jobs as fixture
from test_node_remote import FakePF, SERIAL, PNG
from node_remote import Remote
from node_service import make_handler


class MultiNodeRemoteTests(unittest.TestCase):
    def setUp(self):
        self.flow = fixture.MultiNodeJobTests()
        self.flow.setUp()
        self.addCleanup(self.flow.doCleanups)
        self.center, self.worker = self.flow.center, self.flow.worker
        for env in (self.center, self.worker):
            backend = FakePF(env.root / 'remote-records.json')
            remote = Remote(lambda: {'status': 'clear', 'entries': []}, adb='unused',
                            config_dir=str(env.root / 'remote'), backend=backend)
            remote._enumerate = lambda: [{'serial': SERIAL, 'state': 'device', 'transport': 'adb', 'model': 'fixture'}]
            env.remote, env.backend = remote, backend
            self.addCleanup(remote.close)
            env.server.RequestHandlerClass = make_handler(None, env.jobs, env.admin, env.board, remote=remote)
        self.connector = self.flow.start_worker()

    def request(self, method, node, suffix, value=None, token=None):
        path = '/remote/devices/' + quote(node.jobs.node_id + '/' + SERIAL, safe='') + suffix
        connection = http.client.HTTPConnection(*self.center.server.server_address, timeout=15)
        try:
            connection.request(method, path, body=None if value is None else json.dumps(value), headers={
                'Authorization': 'Bearer ' + (token or self.flow.agent), 'Content-Type': 'application/json'})
            response = connection.getresponse()
            body = response.read()
            return response.status, body if response.getheader('Content-Type').startswith('image/') else json.loads(body)
        finally:
            connection.close()

    def test_one_agent_reaches_both_nodes_and_lost_action_reply_is_only_queried(self):
        self.center.jobs.bins['fixture'] = (sys.executable, str(self.center.root))
        # The same identity is accepted by the ordinary job intake.
        self.center.jobs.dispatch.close()
        self.center.jobs.dispatch.start = lambda: None
        code, job = self.flow.submit({'bin': 'fixture', 'argv': ['-c', 'pass'], 'device_access': 'none'}, 'one-token')
        self.assertEqual((code, job['actor_id']), (200, 'researcher'))
        code, inventory = self.center.call('GET', '/remote/devices', token=self.flow.agent)
        self.assertEqual(code, 200, inventory)
        self.assertEqual({device['serial'] for device in inventory['devices']},
                         {node.jobs.node_id + '/' + SERIAL for node in (self.center, self.worker)})
        sessions = {}
        for node in (self.center, self.worker):
            code, value = self.request('POST', node, '/session', {'request_id': 'acquire'})
            self.assertEqual(code, 200, value)
            sessions[node.jobs.node_id] = value['session_id']
            self.assertEqual(node.backend.data['refs'][0]['operator'], {'id': 'agent:researcher', 'name': 'researcher'})
        sid = sessions[self.worker.jobs.node_id]
        code, screenshot = self.request('POST', self.worker, '/screenshot', {'request_id': 'frame', 'session_id': sid})
        self.assertEqual((code, screenshot), (200, PNG))
        lost = False
        original = self.connector._send

        def drop(method, path, *args, **kwargs):
            nonlocal lost
            if path.endswith('/reply') and not lost:
                lost = True
                raise ConnectionError('fixture drops one completed reply')
            return original(method, path, *args, **kwargs)

        self.connector._send = drop
        code, outcome = self.request('POST', self.worker, '/tap', {
            'request_id': 'tap-once', 'session_id': sid, 'frame_id': 'frame', 'x': 0, 'y': 0})
        self.assertEqual(code, 503, outcome)
        self.assertTrue(outcome['query_required'])
        before = self.worker.backend.mutations()
        code, receipt = self.request('GET', self.worker, '/requests/tap-once?session_id=' + sid)
        self.assertEqual((code, receipt['status']), (200, 'completed'))
        self.assertEqual(self.worker.backend.mutations(), before)
        self.assertEqual(sum(call[-1] == 'tap-once' for call in before), 1)
        # A different center identity cannot operate this owner.
        other = self.center.jobs.agents.join({'invite': self.center.jobs.agents.invite()['code'], 'name': 'other'})['token']
        self.assertEqual(self.request('POST', self.worker, '/key', {
            'request_id': 'not-mine', 'session_id': sid, 'key': 'back'}, other)[0], 403)
        self.center.jobs.agents.update('researcher', {
            'revision': self.center.jobs.agents.get('researcher')['revision'], 'enabled': False})
        self.assertEqual(self.request('POST', self.worker, '/key', {
            'request_id': 'revoked', 'session_id': sid, 'key': 'back'})[0], 401)
        self.assertEqual(self.worker.backend.mutations(), before)

    def test_starting_release_uses_original_acquisition_on_its_node(self):
        self.worker.backend.starting.add('waiting')
        code, waiting = self.request('POST', self.worker, '/session', {'request_id': 'waiting'})
        self.assertEqual((code, waiting['phase']), (202, 'starting'))
        code, released = self.request('DELETE', self.worker, '/session', {
            'acquisition_id': 'waiting', 'request_id': 'release-waiter'})
        self.assertEqual((code, released['status']), (202, 'accepted'))
        self.assertEqual([call for call in self.worker.backend.mutations() if call[0] == 'submit'],
                         [('submit', 'ref-1', 'release-waiter')])
        self.assertEqual(self.center.backend.mutations(), [])


if __name__ == '__main__':
    unittest.main()
