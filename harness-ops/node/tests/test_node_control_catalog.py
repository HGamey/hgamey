"""One phone reference supplies jobs and remote control without hiding capabilities."""
import time
import unittest

import test_multi_node_remote as fixture


class Catalog(unittest.TestCase):
    def setUp(self):
        self.flow = fixture.MultiNodeRemoteTests()
        self.flow.setUp()
        self.addCleanup(self.flow.doCleanups)
        self.center, self.worker = self.flow.center, self.flow.worker
        self.worker.jobs.fleet._fleet = {'devices': [{'serial': fixture.SERIAL, 'model': 'fixture',
            'online': True, 'root': False, 'os': 'Android fixture', 'platform': 'android', 'transport': 'adb',
            'layers': [{'layer': 'gray', 'blocked_by': 'root required'}]}]}
        self.worker.jobs.fleet._sampled_at = time.time()
        self.worker.backend.actions = [*self.worker.backend.actions, 'shell', 'install', 'push']
        observation = self.flow.connector.backend.snapshot()
        observation['queue']['lease']['device_scopes'] = {'registry_revision': 1,
            'devices': [{'device_id': 'stable-phone', 'aliases': [fixture.SERIAL], 'status': 'clear'}]}
        self.center.jobs.directory.report(self.worker.jobs.node_id, observation)

    def test_catalog_keeps_root_and_layer_facts_separate_from_remote_actions(self):
        code, response = self.center.call('GET', '/phones')
        self.assertEqual(code, 200)
        phone = next(item for item in response['items'] if item['node_id'] == self.worker.jobs.node_id)
        self.assertEqual((phone['node_name'], phone['online'], phone['root'], phone['os']),
                         ('worker', True, False, 'Android fixture'))
        self.assertEqual(phone['layers'], [{'layer': 'gray', 'blocked_by': 'root required'}])
        self.assertTrue({'shell', 'install', 'push'} <= set(phone['remote_actions']))
        code, response = self.center.call('GET', '/remote/devices')
        self.assertEqual(code, 200)
        remote = next(item for item in response['devices'] if item['execution_node_id'] == phone['node_id'])
        self.assertEqual((remote['ref'], remote['raw_serial'], remote['root'], remote['os']),
                         (phone['ref'], phone['serial'], False, phone['os']))
        self.assertEqual(self.center.jobs.remote_center.target(phone['ref']), (phone['node_id'], phone['serial']))
        self.flow.connector.close()
        with self.center.jobs.directory._locked() as state:
            state['nodes'][0]['last_seen_at'] = 0
            self.center.jobs.directory._store.write_json('registry.json', state)
        code, response = self.center.call('GET', '/phones')
        offline = next(item for item in response['items'] if item['ref'] == phone['ref'])
        self.assertEqual((code, offline['connected'], offline['online']), (200, False, False))
        self.assertEqual((offline['root'], offline['os'], offline['remote_actions'], offline['sampled_at']),
                         (phone['root'], phone['os'], phone['remote_actions'], phone['sampled_at']))


if __name__ == '__main__':
    unittest.main()
