#!/usr/bin/env python3
"""Isolated hub/node-b, outbound connector, real PF and fake adb. No phones/credentials."""
import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from urllib.parse import quote, urlencode
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_multi_node_jobs as fixture
from node_remote import Remote
from remote_owner import PFRemote
from node_service import make_handler

PF = str(Path(sys.argv.pop(1)).resolve())
SERIAL = 'usb-k1'
PNG = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNocFAAAAIkAOGrWWInAAAAAElFTkSuQmCC'


class Control(unittest.TestCase):
    def setUp(self):
        self.flow = fixture.MultiNodeJobTests()
        self.flow.setUp()
        self.addCleanup(self.flow.doCleanups)
        self.center, self.worker = self.flow.center, self.flow.worker
        for node in (self.center, self.worker):
            adb = node.root / 'adb'
            adb.write_text('''#!''' + sys.executable + '''
import base64,hashlib,json,re,subprocess,sys
from pathlib import Path
root=Path(__file__).parent
args=sys.argv[1:]
if args[:1]==['devices']:
 print('List of devices attached\\nusb-k1 device model:Fixture'); sys.exit()
assert args[:2]==['-s','usb-k1'],args
entry={'argv':args}
if args[2] in ('push','install'):
 data=Path(args[3] if args[2]=='push' else args[-1]).read_bytes()
 entry.update(sha256=hashlib.sha256(data).hexdigest(),bytes=len(data))
with (root/'adb-calls').open('a') as f: f.write(json.dumps(entry)+'\\n')
if args[2]=='exec-out':
 sys.stdout.buffer.write(base64.b64decode(''' + repr(PNG) + ''')); sys.exit()
if 'fixture-terminal-error' in ' '.join(args):
 sys.exit(subprocess.run(args[3],shell=True).returncode)
terminal=re.search(r'PF_GAME_DEPLOY_EXIT:[^:]+:\\d+:',' '.join(args))
print('fixture command on '+root.name)
if terminal:
 print('SurfaceOrientation: 0\\n'+terminal.group(0)+'0')
''')
            adb.chmod(0o700)
            env = {k: v for k, v in os.environ.items() if not k.startswith(('PF_', 'PHONEFARM_', 'DEVLOCK_'))}
            env.update(ADB_BIN=str(adb), PHONEFARM_STATE_DIR=str(node.root / 'state'),
                       DEVLOCK_DIR=str(node.root / 'locks'), PHONEFARM_LEASE_DIR=str(node.root / 'leases'))
            node.backend = PFRemote(PF, str(node.root), env, node.root / 'remote')
            self.addCleanup(self.close_owner, node.backend)
            node.remote = Remote(lambda: {'status': 'clear', 'entries': []}, adb=str(adb),
                                 config_dir=str(node.root / 'remote-config'), backend=node.backend)
            node.server.RequestHandlerClass = make_handler(None, node.jobs, node.admin, node.board, remote=node.remote)
            node.jobs.fleet._fleet = {'devices': [{'serial': SERIAL, 'model': 'Fixture', 'online': True,
                'root': False, 'os': 'Android fixture', 'platform': 'android', 'transport': 'adb',
                'layers': [{'layer': 'gray', 'blocked_by': 'root required'}]}]}
            node.jobs.fleet._sampled_at = time.time()
        self.connector = self.flow.start_worker()

    @staticmethod
    def close_owner(backend):
        processes = list(backend.processes.values())
        backend.close()
        for process in processes:
            process.wait(timeout=10)
        backend.store.close()

    def request(self, node, method, action, params, body=None):
        reference = node.jobs.node_id + '/' + SERIAL
        path = '/remote/devices/' + quote(reference, safe='') + '/' + action
        headers = {'Authorization': 'Bearer ' + self.flow.agent}
        if method in ('PUT', 'GET'):
            path += '?' + urlencode(params)
            headers['Content-Type'] = 'application/octet-stream'
        else:
            body = json.dumps(params).encode()
            headers['Content-Type'] = 'application/json'
        connection = http.client.HTTPConnection(*self.center.server.server_address, timeout=20)
        try:
            connection.request(method, path, body, headers)
            response = connection.getresponse()
            data = response.read()
            return response.status, data if response.getheader('Content-Type').startswith('image/') else json.loads(data)
        finally:
            connection.close()

    def settle(self, node, session, request, result, expected='completed'):
        deadline = time.monotonic() + 10
        while result['status'] in ('accepted', 'started'):
            self.assertLess(time.monotonic(), deadline, result)
            time.sleep(.02)
            code, result = self.request(node, 'GET', 'requests/' + request, {'session_id': session})
            self.assertIn(code, (200, 202), result)
        self.assertEqual(result['status'], expected, result)
        return result

    def test_center_controls_each_nodes_phone_with_the_same_actions(self):
        for node in (self.worker, self.center):
            with self.subTest(node=node.jobs.node_id):
                code, owner = self.request(node, 'POST', 'session', {'request_id': 'acquire'})
                self.assertEqual((code, owner['phase']), (200, 'ready'), owner)
                sid = owner['session_id']
                for action, params, data in [('shell', {'cmd': 'id'}, None),
                        ('install', {}, b'fixture apk bytes'),
                        # Exceed the connection's default 2 MiB response limit:
                        # file bytes use the command body, not a JSON envelope.
                        ('push', {'path': '/sdcard/fixture space.bin'}, b'\x00file payload' * (1 << 18))]:
                    code, result = self.request(node, 'POST' if data is None else 'PUT', action,
                        {'session_id': sid, 'request_id': action, **params}, data)
                    self.assertIn(code, (200, 202), result)
                    result = self.settle(node, sid, action, result)
                    self.assertEqual(result['result']['exit'], 0, result)
                    if data is not None:
                        self.assertEqual((result['result']['sha256'], result['result']['bytes']),
                                         (hashlib.sha256(data).hexdigest(), len(data)))
                    before = (node.root / 'adb-calls').read_bytes()
                    self.assertEqual(self.request(node, 'GET', 'requests/' + action, {'session_id': sid})[1], result)
                    self.assertEqual(self.request(node, 'POST' if data is None else 'PUT', action,
                        {'session_id': sid, 'request_id': action, **params}, data)[1], result)
                    self.assertEqual((node.root / 'adb-calls').read_bytes(), before)
                code, result = self.request(node, 'POST', 'screenshot', {'session_id': sid, 'request_id': 'image'})
                self.assertIn(code, (200, 202), result)
                if not isinstance(result, bytes):
                    self.settle(node, sid, 'image', result)
                    code, result = self.request(node, 'GET', 'requests/image', {'session_id': sid, 'image': 1})
                self.assertEqual(code, 200)
                self.assertTrue(result.startswith(b'\x89PNG\r\n\x1a\n'))
                code, result = self.request(node, 'DELETE', 'session', {'session_id': sid, 'request_id': 'release'})
                self.settle(node, sid, 'release', result)
                calls = [json.loads(line) for line in (node.root / 'adb-calls').read_text().splitlines()]
                self.assertTrue(all(call['argv'][:2] == ['-s', SERIAL] for call in calls))
                uploads = [call for call in calls if 'sha256' in call]
                self.assertEqual(len(uploads), 2)
                self.assertEqual(uploads[1]['argv'][-1], '/sdcard/fixture space.bin')

    def test_catalog_exposes_child_capabilities_and_job_targets_the_child(self):
        code, catalog = self.center.call('GET', '/phones')
        self.assertEqual(code, 200)
        phone = next(p for p in catalog['items'] if p['node_id'] == self.worker.jobs.node_id)
        self.assertEqual((phone['node_name'], phone['connected'], phone['online'], phone['root'], phone['os']),
                         ('worker', True, True, False, 'Android fixture'))
        self.assertTrue({'shell', 'install', 'push', 'screenshot'} <= set(phone['remote_actions']))
        completed = threading.Event()
        original = self.worker.jobs.dispatch._commit_completion
        def commit(*args, **kwargs):
            value = original(*args, **kwargs)
            completed.set()
            return value
        with patch.object(self.worker.jobs.dispatch, '_commit_completion', side_effect=commit):
            code, job = self.flow.submit({'bin': 'fixture', 'argv': [str(self.worker.root / 'adb'), '-s', '@serial', 'shell', 'id'],
                'device_access': 'none', 'device_ref': phone['ref']}, 'child-job')
            self.assertEqual(code, 200, job)
            self.assertTrue(completed.wait(10))
        code, job = self.center.call('GET', '/jobs/' + job['id'])
        self.assertEqual((code, job['execution_node_id'], job['state'], job['exit']),
                         (200, self.worker.jobs.node_id, 'finished', 0), job)
        self.assertEqual(job['argv'][2], SERIAL)
        self.assertFalse((self.center.root / 'adb-calls').exists())

    def test_cli_uploads_to_the_child_and_preserves_request_identity(self):
        code, owner = self.request(self.worker, 'POST', 'session', {'request_id': 'cli-acquire'})
        self.assertEqual(code, 200, owner)
        sid = owner['session_id']
        source = self.center.root / 'upload with spaces.apk'
        source.write_bytes(b'APK for CLI')
        token = self.center.root / 'test-token'
        token.write_text(self.flow.agent)
        command = [sys.executable, str(Path(__file__).resolve().parents[1] / 'pf-remote'),
            '--url', self.center.url, '--node-id', self.center.jobs.node_id, '--token-file', str(token),
            'action', self.worker.jobs.node_id + '/' + SERIAL, 'install', '--session-id', sid, '--request-id', 'cli-install']
        missing = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn('require --file', missing.stderr)
        result = subprocess.run([*command, '--file', str(source)], capture_output=True, text=True, timeout=10)
        self.assertIn(result.returncode, (0, 3), result.stdout + result.stderr)
        receipt = self.settle(self.worker, sid, 'cli-install', json.loads(result.stdout))
        self.assertEqual(receipt['result']['sha256'], hashlib.sha256(source.read_bytes()).hexdigest())
        calls = (self.worker.root / 'adb-calls').read_bytes()
        changed = self.request(self.worker, 'PUT', 'install', {'session_id': sid, 'request_id': 'cli-install'}, b'different APK')
        self.assertEqual(changed[0], 409, changed)
        self.assertEqual((self.worker.root / 'adb-calls').read_bytes(), calls)

    def test_shell_failure_returns_the_device_diagnostic(self):
        code, owner = self.request(self.worker, 'POST', 'session', {'request_id': 'shell-errors'})
        self.assertEqual(code, 200, owner)
        sid = owner['session_id']
        code, result = self.request(self.worker, 'POST', 'shell', {'session_id': sid,
            'request_id': 'failure', 'cmd': 'printf fixture-terminal-error >&2; exit 7'})
        self.assertIn(code, (200, 202), result)
        result = self.settle(self.worker, sid, 'failure', result, expected='failed')
        self.assertEqual(result['result']['exit'], 7)
        self.assertIn('fixture-terminal-error', result['result']['stdout'])

    def test_lost_upload_reply_is_queried_without_reinstalling(self):
        code, owner = self.request(self.worker, 'POST', 'session', {'request_id': 'lost-acquire'})
        self.assertEqual(code, 200, owner)
        sid = owner['session_id']
        send = self.connector._send
        dropped = threading.Event()
        def lose_reply(method, path, *args, **kwargs):
            if path.endswith('/reply') and not dropped.is_set():
                dropped.set()
                raise ConnectionError('fixture loses only the installation reply')
            return send(method, path, *args, **kwargs)
        with patch.object(self.connector, '_send', side_effect=lose_reply):
            code, result = self.request(self.worker, 'PUT', 'install',
                {'session_id': sid, 'request_id': 'install-once'}, b'install once')
        self.assertTrue(dropped.is_set())
        self.assertEqual(code, 503, result)
        self.assertTrue(result['query_required'])
        code, result = self.request(self.worker, 'GET', 'requests/install-once', {'session_id': sid})
        self.assertEqual(code, 200, result)
        self.assertEqual((result['status'], result['result']['exit']), ('completed', 0))
        calls = [json.loads(line) for line in (self.worker.root / 'adb-calls').read_text().splitlines()]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['argv'][2:4], ['install', '-r'])


if __name__ == '__main__':
    unittest.main()
