#!/usr/bin/env python3
"""Two private HTTP nodes -> real workers/PF fd locks -> fake adb, run by Cargo."""
import json
import os
from pathlib import Path
import select
import socket
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from test_multi_node_jobs import MultiNodeJobTests

PF = str(Path(sys.argv.pop(1)).resolve())
DEVLOCK = str(Path(__file__).resolve().parents[3] / 'phonefarm' / 'tools' / 'devlock')


class RoutedLocks(unittest.TestCase):
    def setUp(self):
        self.flow = MultiNodeJobTests()
        self.flow.setUp()
        self.addCleanup(self.flow.doCleanups)
        self.listener = socket.socket()
        self.addCleanup(self.listener.close)
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen()
        self.listener.settimeout(15)
        self.environments = {}
        for node, serial in ((self.flow.center, 'USB-A'), (self.flow.worker, 'USB-B')):
            adb = node.root / 'adb'
            adb.write_text('''#!/usr/bin/env python3
import json,os,socket,sys
s=socket.create_connection(('127.0.0.1',int(os.environ['FIXTURE_PORT'])))
s.sendall((json.dumps(sys.argv[1:])+'\\n').encode())
s.recv(1)
print('fake-adb-complete')
''')
            adb.chmod(0o755)
            environment = {'PATH': os.defpath, 'HOME': str(node.root), 'ADB_BIN': str(adb),
                'PHONEFARM_BIN': PF, 'DEVLOCK_DIR': str(node.root / 'locks'),
                'PHONEFARM_LEASE_DIR': str(node.root / 'leases'), 'PHONEFARM_STATE_DIR': str(node.root / 'pf-state'),
                'FIXTURE_PORT': str(self.listener.getsockname()[1])}
            self.environments[node.jobs.node_id] = environment
            node.jobs.bins.update(phonefarm=(PF, str(node.root)), adb=(str(adb), str(node.root)),
                                  devlock=(DEVLOCK, str(node.root)))
            node.jobs.pf_env = node.jobs.leases.env = node.jobs.programs.env = environment
            request = {'expected_revision': 0, 'device': {'device_id': 'phone', 'label': serial,
                       'aliases': [serial, serial + '-wifi'], 'enabled': True}}
            result = subprocess.run([PF, 'lease', 'devices-set', '--request', '-', '--json'],
                input=json.dumps(request), text=True, capture_output=True, env=environment, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            node.jobs.programs.refresh()
            node.board.monitor._fleet = {'devices': [{'serial': serial, 'model': 'NX809J', 'online': True}]}
            node.board.monitor._sampled_at = time.time()
        self.addCleanup(self.finish_jobs)

    def finish_jobs(self):
        for node in (self.flow.worker, self.flow.center):
            for row in node.jobs.dispatch.queue.rows():
                node.jobs.reset_queue(row['job_id'], 'integration fixture cleanup')

    def holder(self, node, serial):
        proc = subprocess.Popen([PF, 'lease', '--serial', serial, '--', sys.executable, '-c',
            "import sys; print('held',flush=True); sys.stdin.read()"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=self.environments[node.jobs.node_id])
        def close():
            if proc.poll() is None:
                proc.stdin.close()
            proc.wait(timeout=10)
            proc.stdout.close()
            proc.stderr.close()
        self.addCleanup(close)
        self.assertTrue(select.select([proc.stdout], [], [], 10)[0], 'PF owner did not acquire')
        self.assertEqual(proc.stdout.readline(), b'held\n')
        return proc

    def probe(self, node, serial=None):
        result = subprocess.run([PF, 'lease', *(['--serial', serial] if serial else []),
            '--timeout-ms', '0', '--', '/usr/bin/true'], capture_output=True,
            env=self.environments[node.jobs.node_id], timeout=5)
        return result.returncode == 0

    def adb_entered(self, serial):
        child, _ = self.listener.accept()
        self.addCleanup(child.close)
        with child.makefile('rb') as stream:
            arguments = json.loads(stream.readline())
        self.assertEqual(arguments, ['-s', serial, 'shell', 'fixture-hold'])
        return child

    def test_model_cli_aliases_two_nodes_and_selection_race_use_real_pf(self):
        center, worker = self.flow.center, self.flow.worker
        occupied = self.holder(center, 'USB-A-wifi')
        connector = self.flow.start_worker()
        result = self.flow.cli('--device-model', 'NX809J', '--device-access', 'write', '--request-id',
            'real-model', '--detach', 'adb', '-s', '@serial', 'shell', 'fixture-hold')
        self.assertEqual(result.returncode, 0, result.stderr)
        first = center.jobs.center.targets.by_request('real-model')
        self.assertEqual(first['execution_node_id'], worker.jobs.node_id)
        self.adb_entered('USB-B')
        self.assertFalse(self.probe(worker, 'USB-B-wifi'))
        self.assertFalse(self.probe(worker))
        self.assertTrue(self.probe(worker, 'different-phone'))

        occupied.stdin.close()
        self.assertEqual(occupied.wait(timeout=10), 0)
        result = self.flow.cli('--device', center.jobs.node_id + '/phone', '--device-access', 'write',
            '--request-id', 'real-local', '--detach', 'adb', '-s', '@serial', 'shell', 'fixture-hold')
        self.assertEqual(result.returncode, 0, result.stderr)
        second = center.jobs.center.targets.by_request('real-local')
        self.adb_entered('USB-A')
        self.assertFalse(self.probe(center, 'USB-A-wifi'))
        self.assertFalse(self.probe(center))
        self.assertEqual(len(worker.jobs.dispatch.queue.rows()), 1)
        self.assertEqual(len(center.jobs.dispatch.queue.rows()), 1)

        worker.jobs.reset_queue(first['id'], 'release worker for selection race')
        self.assertTrue(self.probe(worker, 'USB-B'))
        connector._json('/node-connect/report', connector.backend.snapshot())
        connector.backend.acknowledge()
        transfer = center.jobs.center.transfer
        holders = []

        def raced(record, body):
            self.assertEqual(record['execution_node_id'], worker.jobs.node_id)
            holders.append(self.holder(worker, 'USB-B-wifi'))
            return transfer(record, body)

        spec = {'bin': 'adb', 'argv': ['-s', '@serial', 'shell', 'fixture-hold'],
                'device_access': 'write', 'device_model': 'NX809J'}
        with patch.object(center.jobs.center, 'transfer', side_effect=raced):
            code, waiting = self.flow.submit(spec, 'real-race')
        self.assertEqual((code, waiting['queue']['state']), (200, 'queued'), waiting)
        self.assertFalse(select.select([self.listener], [], [], 0)[0], 'busy phone reached adb')
        self.assertFalse(self.probe(worker, 'USB-B'))
        holders[0].stdin.close()
        self.assertEqual(holders[0].wait(timeout=10), 0)
        self.adb_entered('USB-B')
        code, repeated = self.flow.submit(spec, 'real-race')
        self.assertEqual((code, repeated['id']), (200, waiting['id']))
        self.assertEqual(len(worker.jobs.dispatch.queue.rows()), 2)
        self.assertEqual(center.jobs.status(second['id'])['state'], 'running')


if __name__ == '__main__':
    unittest.main(defaultTest='RoutedLocks')
