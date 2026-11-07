"""Phone reads and the post-join report keep the real shared sampler active."""
import json
import sys
import threading
import time
import unittest
from unittest.mock import patch

import test_node_games as fixture
from node_connector import LocalHTTP, NodeConnector


class PhoneSamplingTests(unittest.TestCase):
    def node(self):
        node = fixture.GameFlowTests()
        node.setUp()
        self.addCleanup(node.tearDown)
        program = node.root / 'phonefarm'
        program.write_text('#!' + sys.executable + '\n' + '''import json,sys
from pathlib import Path
root=Path(__file__).parent
with (root/'sample-calls').open('a') as out: out.write(json.dumps(sys.argv[1:])+'\\n')
print(json.dumps({'schema':'phonefarm/fleet@1','devices':[{
    'serial':'fixture-phone','online':True,'battery':{'level':60},
    'thermal':{'battery_c':31}}]}))
''')
        program.chmod(0o755)
        return node

    def assert_metadata_only(self, node):
        node.board.monitor.close()
        if node.board.monitor._thread:
            node.board.monitor._thread.join()
        calls = [json.loads(line) for line in (node.root / 'sample-calls').read_text().splitlines()]
        self.assertTrue(calls)
        for args in calls:
            self.assertEqual(args, ['fleet', '--json', '--lock-dir', str(node.root / 'locks')])

    def test_phones_read_starts_the_local_sampler_without_opening_the_dashboard(self):
        node = self.node()
        sampled = threading.Event()
        record = node.board.monitor._record
        def observed(fleet):
            record(fleet)
            sampled.set()
        with patch.object(node.board.monitor, '_record', side_effect=observed):
            self.assertEqual(node.call('GET', '/phones')[0], 200)
            self.assertTrue(sampled.wait(5), '/phones never started local sampling')
            code, result = node.call('GET', '/phones')
        self.assertEqual(code, 200, result)
        phone, = result['items']
        self.assertEqual(phone['serial'], 'fixture-phone')
        self.assertTrue(phone['online'])
        self.assertFalse(phone['stale'])
        self.assertIsNotNone(phone['sampled_at'])
        self.assert_metadata_only(node)

    def joined_connector(self, center, worker, **backend_args):
        joined = center.jobs.directory.join({'invite': center.jobs.directory.invite()['code'],
            'name': 'worker', 'node_id': worker.jobs.node_id})
        return NodeConnector({'url': center.url, 'center_id': joined['center_id'], 'token': joined['token']},
                             LocalHTTP(worker.url, worker.admin, worker.jobs.node_id, **backend_args))

    def test_the_post_join_report_refreshes_phone_readings_without_local_readers(self):
        center, worker = self.node(), self.node()
        connector = self.joined_connector(center, worker, sample=True)
        sampled = threading.Event()
        seen = []
        report = connector._json
        def observed(path, value):
            result = report(path, value)
            if path == '/node-connect/report' and value['fleet']['sampled_at'] is not None:
                seen.append(value['fleet']['sampled_at'])
                sampled.set()
            return result
        connector._json = observed
        with patch('node_connector.REFRESH_S', .05), patch('node_fleet.FLEET_EVERY_FREE_S', .05):
            try:
                connector.start()
                self.assertTrue(sampled.wait(5), 'the report after joining kept reusing absent phone readings')
                _, entry = center.call('GET', '/nodes/worker')
                self.assertGreaterEqual(entry['snapshot']['fleet']['sampled_at'], seen[0])
                _, result = center.call('GET', '/phones')
                phone, = [item for item in result['items'] if item['node_id'] == worker.jobs.node_id]
                self.assertFalse(phone['stale'])
                self.assertTrue(phone['online'])
                self.assertGreaterEqual(phone['sampled_at'], seen[0])
            finally:
                connector.close()
        self.assert_metadata_only(worker)

    def test_a_plain_periodic_report_refreshes_phone_readings_with_nobody_watching(self):
        # A node that joined long ago (no join-time sample) and that nobody reads locally must still send fresh phone
        # readings with its routine reports.
        center, worker = self.node(), self.node()
        connector = self.joined_connector(center, worker)
        reports = []
        report = connector._json
        def observed(path, value):
            result = report(path, value)
            if path == '/node-connect/report':
                reports.append(value['fleet']['sampled_at'])
            return result
        connector._json = observed
        with patch('node_connector.REFRESH_S', .05), patch('node_fleet.FLEET_EVERY_FREE_S', .05):
            try:
                connector.start()
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if len(reports) >= 2 and reports[1] is not None:
                        break
                    time.sleep(.05)
            finally:
                connector.close()
        self.assertGreaterEqual(len(reports), 2, 'the worker never sent a second periodic report')
        self.assertIsNotNone(reports[1],
            'a routine report from an ordinary joined node kept reusing absent phone readings')


if __name__ == '__main__':
    unittest.main()
