"""One center index and cursor, independent of execution-node connectivity."""
import base64
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from urllib.parse import parse_qs, urlencode, urlparse

NODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NODE))
from node_service import readonly_token
import test_node_games as game_fixture
from node_connection_fixture import connect


class NodeResultSearchTests(unittest.TestCase):
    def setUp(self):
        self.environments = []
        self.addCleanup(self.cleanup)
        self.hub = self.node('hub')
        self.alpha, self.beta = self.node('alpha'), self.node('beta')
        self.targets = []
        for env in (self.alpha, self.beta):
            env.connector = connect(self.hub.jobs.directory, self.hub.url, env.label,
                                    env.url, env.admin, env.jobs.node_id)
            self.targets.append({'name': env.label, 'node_id': env.jobs.node_id})
            env.source_job = ('a' if env.label == 'alpha' else 'b') * 12
            self.hub.jobs.center.targets.reserve({'id': env.source_job, 'request_id': 'source-' + env.label,
                'execution_node_id': env.jobs.node_id, 'fingerprint': env.label,
                'request': {'spec': {}}, 'device_selection': None, 'stop': None,
                'observation': {'state': 'finished', 'queue': {'state': 'done'}}})
            self.publish(env, 'older-' + env.label)
            self.publish(env, 'result-' + env.label)
        for env in self.environments:
            env.calls.clear()

    def node(self, label):
        env = game_fixture.GameFlowTests()
        env.setUp()
        self.environments.append(env)
        env.label, env.calls = label, []
        inherited = env.server.RequestHandlerClass

        class Observed(inherited):
            def do_GET(self):
                parsed = urlparse(self.path)
                env.calls.append({'method': 'GET', 'path': parsed.path, 'query': parse_qs(parsed.query)})
                return super().do_GET()

        env.server.RequestHandlerClass = Observed
        return env

    def cleanup(self):
        for env in self.environments:
            if hasattr(env, 'connector'):
                env.connector.close()
        for env in reversed(self.environments):
            env.tearDown()
            self.assertFalse(env.thread.is_alive())

    def publish(self, env, rid):
        return self.hub.jobs.results.register({'schema': 'phonefarm/result@1', 'result_id': rid,
            'title': env.label + ' ' + rid, 'job_id': env.source_job,
            'labels': {'game_id': 'fixture-game'}, 'outcome': 'unknown'})

    def request(self, *, cursor=None, dash=False, authenticated=True):
        query = {'game_id': 'fixture-game', 'limit': 1}
        if cursor is not None:
            query['cursor'] = cursor
        headers = {'X-Expected-Node-ID': self.hub.jobs.node_id}
        if authenticated:
            headers['Authorization'] = ('Basic ' + base64.b64encode(b'viewer:test-view').decode()
                if dash else 'Bearer ' + readonly_token(self.hub.admin))
        connection = http.client.HTTPConnection(*self.hub.server.server_address, timeout=12)
        try:
            connection.request('GET', ('/dash/api/results' if dash else '/results') + '?' + urlencode(query), headers=headers)
            reply = connection.getresponse()
            return reply.status, json.loads(reply.read())
        finally:
            connection.close()

    def cli(self, *args):
        env = {'PATH': os.defpath, 'PF_NODE_URL': self.hub.url, 'PF_NODE_ID': self.hub.jobs.node_id,
               'PF_NODE_TOKEN': readonly_token(self.hub.admin)}
        return subprocess.run([sys.executable, str(NODE / 'pf-node'), 'results', 'list', *args],
                              env=env, capture_output=True, text=True, timeout=15)

    def assert_read_boundaries(self):
        for env in (self.alpha, self.beta):
            self.assertFalse(any(call['path'].startswith('/results') for call in env.calls), env.calls)
        for env in self.environments:
            self.assertEqual(env.jobs.dispatch.queue.rows(), [])

    def test_center_cursor_and_collected_results_survive_offline_sources(self):
        first = self.cli('--game-id', 'fixture-game', '--limit', '1')
        self.assertEqual(first.returncode, 0, first.stderr)
        page = json.loads(first.stdout)
        self.assertEqual(page['items'][0]['result_id'], 'result-beta')
        self.assertEqual(page['items'][0]['provenance']['execution_node_id'], self.beta.jobs.node_id)
        self.assertEqual([call['path'] for call in self.hub.calls], ['/node', '/results'])
        self.publish(self.alpha, 'later-alpha')
        self.hub.calls.clear()
        continued = self.cli('--game-id', 'fixture-game', '--limit', '1', '--cursor', page['next_cursor'])
        self.assertEqual(continued.returncode, 0, continued.stderr)
        self.assertEqual(json.loads(continued.stdout)['items'][0]['result_id'], 'older-beta')
        self.beta.connector.close()
        listing = self.cli('--game-id', 'fixture-game')
        self.assertEqual(listing.returncode, 0, listing.stderr)
        self.assertEqual({item['result_id'] for item in json.loads(listing.stdout)['items']},
                         {'later-alpha', 'result-alpha', 'result-beta', 'older-alpha', 'older-beta'})
        self.assertNotIn(self.hub.admin, listing.stdout)
        self.assertNotIn(readonly_token(self.hub.admin), listing.stdout)
        self.assert_read_boundaries()

    def test_directory_note_does_not_change_result_identity_or_read_permissions(self):
        self.assertEqual(self.request(authenticated=False)[0], 401)
        code, before = self.request(dash=True)
        self.assertEqual(code, 200)
        self.hub.jobs.directory.update('beta', {'revision': 1, 'note': 'renamed description'})
        code, after = self.request(dash=True)
        self.assertEqual((code, after), (200, before))
        self.assertEqual(after['items'][0]['provenance']['execution_node_id'], self.beta.jobs.node_id)
        self.assert_read_boundaries()


if __name__ == '__main__':
    unittest.main()
