"""One center identity and credential for computer management, without a local registry."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

import test_node_games as fixture
from node_connection_fixture import connect

NODE = Path(__file__).resolve().parents[1]


class NodesCLITests(unittest.TestCase):
    def setUp(self):
        self.center = fixture.GameFlowTests()
        self.center.setUp()
        self.addCleanup(self.center.tearDown)
        self.calls = []
        owner = self
        inherited = self.center.server.RequestHandlerClass

        class Observed(inherited):
            def do_GET(self):
                owner.calls.append((self.command, self.path, self.headers.get('Authorization'),
                                    self.headers.get('X-Expected-Node-ID')))
                return super().do_GET()

            def do_POST(self):
                owner.calls.append((self.command, self.path, self.headers.get('Authorization'),
                                    self.headers.get('X-Expected-Node-ID')))
                if self.path == '/nodes/uncertain':
                    self.rfile.read(int(self.headers['Content-Length']))
                    self.close_connection = True
                    return
                return super().do_POST()

        self.center.server.RequestHandlerClass = Observed

    def cli(self, *args):
        env = {'PATH': os.defpath, 'NO_PROXY': '*', 'PF_NODE_URL': self.center.url,
               'PF_NODE_ID': self.center.jobs.node_id, 'PF_NODE_TOKEN': self.center.admin}
        result = subprocess.run([sys.executable, str(NODE / 'pf-node'), 'nodes', *args],
                                env=env, capture_output=True, text=True, timeout=15)
        self.assertNotIn(self.center.admin, result.stdout + result.stderr)
        return result

    def test_list_show_update_use_the_same_center_and_keep_observations(self):
        worker = fixture.GameFlowTests()
        worker.setUp()
        self.addCleanup(worker.tearDown)
        connector = connect(self.center.jobs.directory, self.center.url, 'worker', worker.url,
                            worker.admin, worker.jobs.node_id)
        self.addCleanup(connector.close)
        self.calls.clear()
        listed = self.cli('list')
        self.assertEqual(listed.returncode, 0, listed.stderr)
        entry = json.loads(listed.stdout)['items'][0]
        self.assertEqual(entry['snapshot']['identity']['node_id'], worker.jobs.node_id)
        self.assertNotIn('url', entry)
        shown = self.cli('show', 'worker')
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertEqual(json.loads(shown.stdout)['snapshot'], entry['snapshot'])
        changed = self.cli('update', 'worker', '--revision', str(entry['revision']), '--note', 'new note', '--tag', 'lab')
        self.assertEqual(changed.returncode, 0, changed.stderr)
        value = json.loads(changed.stdout)
        self.assertEqual((value['note'], value['tags']), ('new note', ['lab']))
        self.assertEqual(value['snapshot'], entry['snapshot'])
        user_calls = [call for call in self.calls if not call[1].startswith('/node-connect/')]
        self.assertEqual([(call[0], call[1]) for call in user_calls],
                         [('GET', '/node'), ('GET', '/nodes'), ('GET', '/node'), ('GET', '/nodes/worker'),
                          ('GET', '/node'), ('POST', '/nodes/worker')])
        self.assertTrue(all(call[2:] == ('Bearer ' + self.center.admin, self.center.jobs.node_id) for call in user_calls))
        self.assertEqual(worker.jobs.dispatch.queue.rows(), [])

    def test_invite_list_and_revoke_do_not_import_remote_credentials(self):
        invited = self.cli('invite', '--note', 'new computer')
        self.assertEqual(invited.returncode, 0, invited.stderr)
        invitation = json.loads(invited.stdout)
        listed = self.cli('invites')
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertNotIn(invitation['code'], listed.stdout)
        self.assertEqual(json.loads(listed.stdout)['invites'][0]['id'], invitation['id'])
        revoked = self.cli('invites', 'revoke', invitation['id'])
        self.assertEqual(revoked.returncode, 0, revoked.stderr)
        self.assertEqual(json.loads(self.cli('invites').stdout)['invites'], [])

    def test_unknown_write_is_reported_once_without_replay_or_secret(self):
        result = self.cli('update', 'uncertain', '--revision', '1', '--note', 'no reply')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(json.loads(result.stdout)['error']['code'], 'write_result_unknown')
        self.assertEqual([(call[0], call[1]) for call in self.calls],
                         [('GET', '/node'), ('POST', '/nodes/uncertain')])
        failed = self.cli('show', 'missing')
        self.assertEqual(failed.returncode, 2, failed.stderr)
        self.assertIn('HTTP 404', json.loads(failed.stdout)['error']['message'])


if __name__ == '__main__':
    unittest.main()
