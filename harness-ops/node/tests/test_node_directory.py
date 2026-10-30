import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from node_directory import DirectoryError, NodeDirectory, _ProbeError

LOCAL, REMOTE = 'a' * 32, 'b' * 32


class NodeDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = NodeDirectory(self.temp.name, LOCAL)

    def tearDown(self):
        self.directory.close()
        self.temp.cleanup()

    def join(self):
        invitation = self.directory.invite()
        return self.directory.join({'invite': invitation['code'], 'name': 'remote', 'node_id': REMOTE})

    def test_configuration_is_durable_private_and_never_probes_on_read(self):
        joined = self.join()
        token = joined['token']
        with patch.object(self.directory.link, 'call') as request:
            entry = self.directory.get('remote')
            self.assertEqual(entry['node_id'], REMOTE)
            self.assertEqual(entry['connection']['status'], 'disconnected')
            self.assertEqual(self.directory.list(), [entry])
            request.assert_not_called()
        self.assertNotIn(token, json.dumps(entry))
        self.assertNotIn('credential', entry)
        self.assertNotIn('url', entry)
        private = Path(self.temp.name) / '_node' / 'directory'
        self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((private / 'registry.json').stat().st_mode), 0o600)
        self.assertNotIn(token, (private / 'registry.json').read_text())
        self.directory.close()
        self.directory = NodeDirectory(self.temp.name, LOCAL)
        self.assertEqual(self.directory.get('remote'), entry)
        self.assertEqual(self.directory.verify(token), REMOTE)
        changed = self.directory.update('remote', {'revision': entry['revision'], 'note': 'new note'})
        self.assertEqual(changed['note'], 'new note')
        with self.assertRaises(DirectoryError) as conflict:
            self.directory.update('remote', {'revision': entry['revision'], 'note': 'old revision'})
        self.assertEqual(conflict.exception.status, 409)

    def test_offline_preserves_the_last_complete_snapshot_and_rejoin_identity(self):
        joined = self.join()
        snapshot = {'at': 123, 'fleet': {'devices': [{'serial': 'fixture-only'}], 'age_s': 12},
                    'queue': {'items': []}}
        with patch('node_directory.time.time', return_value=1000):
            ready = self.directory.report(REMOTE, snapshot)
        with patch('node_directory.time.time', return_value=1100):
            offline = self.directory.get('remote')
        self.assertEqual(offline['snapshot'], ready['snapshot'])
        self.assertEqual(offline['connection']['last_seen_at'], ready['connection']['last_seen_at'])
        self.assertEqual(offline['connection']['status'], 'disconnected')
        self.assertTrue(offline['stale'])
        with patch.object(self.directory, '_request', side_effect=_ProbeError('unavailable', 'disconnected')):
            refreshed = self.directory.refresh('remote', devices=True)
        self.assertEqual(refreshed['snapshot'], snapshot)
        self.assertEqual(refreshed['connection']['error'], 'disconnected')
        # A new invitation rotates the connection token, not the computer identity or its observations.
        rejoined = self.join()
        self.assertEqual(rejoined['node']['node_id'], REMOTE)
        self.assertEqual(rejoined['node']['snapshot'], snapshot)
        self.assertIsNone(self.directory.verify(joined['token']))
        self.assertEqual(self.directory.verify(rejoined['token']), REMOTE)
        self.assertEqual(len(self.directory.list()), 1)

    def test_inflight_note_change_keeps_the_same_identity_and_observation(self):
        original = self.join()['node']
        snapshot = {'at': 123, 'fleet': {'devices': []}, 'queue': {'items': []}}

        def read(entry, path, deadline):
            self.assertEqual(entry['node_id'], REMOTE)
            self.assertEqual(path, '/node/snapshot')
            self.directory.update('remote', {'revision': original['revision'], 'note': 'changed while reading'})
            return snapshot

        with patch.object(self.directory, '_request', side_effect=read) as request:
            result = self.directory.refresh('remote')
        self.assertEqual(result['note'], 'changed while reading')
        self.assertEqual(result['node_id'], REMOTE)
        self.assertEqual(result['snapshot'], snapshot)
        self.assertEqual(result['connection']['status'], 'ready')
        self.assertEqual(request.call_count, 1)

    def test_invitation_expiry_revocation_and_name_conflict_do_not_spend_another_code(self):
        self.join()
        expired = self.directory.invite(hours=1)
        with patch('node_directory.time.time', return_value=expired['expires_at']):
            with self.assertRaises(DirectoryError) as error:
                self.directory.join({'invite': expired['code'], 'name': 'another', 'node_id': 'c' * 32})
        self.assertEqual(error.exception.status, 410)
        self.directory.revoke_invite(expired['id'])
        self.assertNotIn(expired['id'], [item['id'] for item in self.directory.invites()])
        invitation = self.directory.invite()
        with self.assertRaises(DirectoryError) as error:
            self.directory.join({'invite': invitation['code'], 'name': 'remote', 'node_id': 'c' * 32})
        self.assertEqual(error.exception.status, 409)
        added = self.directory.join({'invite': invitation['code'], 'name': 'another', 'node_id': 'c' * 32})
        self.assertEqual(added['node']['name'], 'another')

    def test_old_directory_keeps_names_and_snapshots_while_discarding_dial_in_credentials(self):
        snapshot = {'at': 123, 'fleet': {'devices': []}, 'queue': {'items': []}}
        original = {'name': 'remote', 'node_id': REMOTE, 'tags': ['lab'], 'note': 'existing node',
                    'revision': 7, 'created_at': 1, 'updated_at': 2, 'snapshot': snapshot,
                    'url': 'https://obsolete.invalid', 'token': 'old-remote-token', 'enabled': True,
                    'ssh': 'old-ssh', 'goals_dir': '/old/goals', 'connection': {'status': 'ready'}}
        self.directory._store.write_json('registry.json', {'schema': 'phonefarm/node-directory@1', 'nodes': [original]})
        self.directory.close()
        self.directory = NodeDirectory(self.temp.name, LOCAL)
        current = self.directory.get('remote')
        for key in ('name', 'node_id', 'tags', 'note', 'revision', 'created_at', 'updated_at', 'snapshot'):
            self.assertEqual(current[key], original[key])
        self.assertEqual(current['connection']['status'], 'disconnected')
        self.assertTrue(current['stale'])
        for key in ('url', 'token', 'ssh', 'goals_dir', 'enabled'):
            self.assertNotIn(key, current)
        rejoined = self.join()
        self.assertEqual(rejoined['node']['snapshot'], snapshot)
        self.assertEqual(len(self.directory.list()), 1)


if __name__ == '__main__':
    unittest.main()
