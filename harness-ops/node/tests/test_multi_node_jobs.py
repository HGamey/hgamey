"""Center identity and fixed public jobs carried to isolated execution nodes."""
import http.client
import json
import multiprocessing
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

import test_node_games as fixture
from node_connector import LocalHTTP, NodeConnector


class MultiNodeJobTests(unittest.TestCase):
    def setUp(self):
        self.center, self.worker = fixture.GameFlowTests(), fixture.GameFlowTests()
        self.center.setUp()
        self.addCleanup(self.center.tearDown)
        self.worker.setUp()
        self.addCleanup(self.worker.tearDown)
        self.worker.jobs.bins['fixture'] = (sys.executable, str(self.worker.root))
        invitation = self.center.jobs.directory.invite()
        joined = self.center.jobs.directory.join({'invite': invitation['code'], 'name': 'worker',
                                                  'node_id': self.worker.jobs.node_id})
        self.config = {'url': self.center.url, 'center_id': joined['center_id'], 'token': joined['token']}
        _, invitation = self.center.call('POST', '/agent-invites', {'url': self.center.url})
        _, joined = self.center.call('POST', '/join', {'invite': invitation['code'], 'name': 'researcher'})
        self.agent = joined['token']

    def start_worker(self):
        connector = NodeConnector(self.config, LocalHTTP(self.worker.url, self.worker.admin, self.worker.jobs.node_id))
        reported = threading.Event()
        original = connector._json

        def observed(path, value):
            reply = original(path, value)
            if path == '/node-connect/report':
                reported.set()
            return reply

        connector._json = observed
        self.addCleanup(connector.close)
        self.addCleanup(self.center.jobs.center.artifacts.close)
        connector.start()
        self.assertTrue(reported.wait(5))
        return connector

    def submit(self, spec, key):
        return self.center.call('POST', '/jobs', token=self.agent,
                                headers={'X-Job': json.dumps(spec), 'X-Request-ID': key})

    def cli(self, *args):
        env = {'PATH': os.defpath, 'NO_PROXY': '*', 'PF_NODE_URL': self.center.url,
               'PF_NODE_TOKEN': self.agent, 'PF_NODE_ID': self.center.jobs.node_id}
        return subprocess.run([sys.executable, str(Path(fixture.__file__).resolve().parents[1] / 'pf-node'),
                               *args], env=env, capture_output=True, text=True, timeout=15)

    def test_cloned_identity_cannot_take_the_original_workers_tasks(self):
        directory = self.center.jobs.directory
        original = directory.get('worker')
        invitation = directory.invite()
        code, rejected = self.center.call('POST', '/node-connect/join', {
            'invite': invitation['code'], 'node_id': self.worker.jobs.node_id, 'name': 'cloned-worker'},
            token='not-a-machine-credential')
        self.assertEqual(code, 409, rejected)
        self.assertEqual(directory.get('worker'), original)
        self.assertEqual(directory.verify(self.config['token']), self.worker.jobs.node_id)
        self.start_worker()
        finished = threading.Event()
        commit = self.worker.jobs.dispatch._commit_completion

        def committed(*args, **kwargs):
            value = commit(*args, **kwargs)
            finished.set()
            return value

        with patch.object(self.worker.jobs.dispatch, '_commit_completion', side_effect=committed):
            code, job = self.submit({'bin': 'fixture', 'argv': ['-c', "print('original-worker')"],
                                    'device_access': 'none', 'execution_node_id': self.worker.jobs.node_id},
                                   'after-cloned-join')
            self.assertEqual(code, 200, job)
            self.assertTrue(finished.wait(10))
        self.assertEqual(self.worker.jobs.status(job['id'])['outcome'], 'succeeded')
        self.assertIn(b'original-worker', self.worker.jobs.log_from(job['id'], 0, 1024))
        self.assertEqual(self.center.jobs.center.targets.get(job['id'])['execution_node_id'],
                         self.worker.jobs.node_id)

    def test_only_an_admin_can_rename_a_node_through_the_cli(self):
        directory = self.center.jobs.directory
        before = directory.get('worker')
        args = ('nodes', 'update', 'worker', '--revision', str(before['revision']), '--rename', 'renamed-worker')
        denied = self.cli(*args)
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn('HTTP 403', denied.stdout)
        self.assertEqual(directory.get('worker'), before)
        with patch.object(self, 'agent', self.center.admin):
            result = self.cli(*args)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(directory.get('renamed-worker')['node_id'], self.worker.jobs.node_id)
        self.assertEqual(directory.verify(self.config['token']), self.worker.jobs.node_id)

    def test_reported_disconnect_returns_cached_status_and_pending_stop_without_transport(self):
        self.worker.jobs.dispatch.close()
        self.worker.jobs.dispatch.start = lambda: None
        connector = self.start_worker()
        spec = {'bin': 'fixture', 'argv': ['-c', 'pass'], 'device_access': 'none',
                'execution_node_id': self.worker.jobs.node_id}
        code, job = self.submit(spec, 'known-offline')
        self.assertEqual(code, 200, job)
        connector.close()
        directory = self.center.jobs.directory
        with directory._locked() as state:
            state['nodes'][0]['last_seen_at'] = 0
            directory._store.write_json('registry.json', state)
        with patch.object(directory.link, 'call', side_effect=AssertionError('offline read waited on transport')):
            self.assertEqual(self.center.call('GET', '/jobs/' + job['id'])[1]['id'], job['id'])
            for action, body in [('cancel', {}), ('reset', {'note': 'offline stop'})]:
                code, reply = self.center.call('POST', '/jobs/' + job['id'] + '/' + action, body)
                self.assertEqual((code, reply['pending']), (202, True), reply)
        self.assertIsNotNone(self.center.jobs.center.remote(job['id'])['stop'])

    def test_task_rows_use_recorded_terminal_facts_without_reading_each_node(self):
        center = self.center.jobs.center
        for index in range(3):
            center.targets.reserve({'id': '%012x' % index, 'request_id': 'cached-' + str(index),
                'fingerprint': str(index), 'execution_node_id': self.worker.jobs.node_id,
                'request': {'spec': {'actor_id': 'researcher'}}, 'device_selection': None,
                'stop': None, 'observation': {'state': 'finished', 'queue': {'state': 'done'}}})
        with patch.object(self.center.jobs.directory, 'list', wraps=self.center.jobs.directory.list) as listed, \
                patch.object(center, 'status', side_effect=AssertionError('task_rows refreshed a routed job')):
            rows = center.task_rows()
        self.assertEqual([row['state'] for row in rows], ['done'] * 3)
        self.assertLessEqual(listed.call_count, 1)

    def test_model_cli_reaches_the_selected_serial_and_old_centers_cannot_ignore_the_selector(self):
        self.phone(self.center, 'USB-A', 'held')
        self.phone(self.worker, 'USB-B', 'clear')
        self.start_worker()
        result = self.cli('--device-model', 'NX809J', '--request-id', 'model-cli', '--detach',
                          '--device-access', 'write', 'fixture', 'probe.py', '--serial', '@serial')
        self.assertEqual(result.returncode, 0, result.stderr)
        record = self.center.jobs.center.targets.by_request('model-cli')
        self.assertEqual(record['device_selection']['serial'], 'USB-B')
        self.assertEqual(self.worker.jobs.status(record['id'])['argv'][-1], 'USB-B')
        from node_read import NodeRead
        original = NodeRead.identity

        def old_identity(node):
            value = original(node)
            if node.node_id == self.center.jobs.node_id:
                value['capabilities'].remove('jobs.routing-v1')
            return value

        with patch.object(NodeRead, 'identity', old_identity):
            result = self.cli('--device-model', 'NX809J', '--request-id', 'old-center', '--detach',
                              'fixture', 'probe.py', '--serial', '@serial')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('不支持设备选择', result.stderr)
        self.assertIsNone(self.center.jobs.center.targets.by_request('old-center'))
        self.assertEqual(self.center.jobs.dispatch.queue.rows(), [])

    def test_lost_accept_reply_and_center_recreation_keep_one_public_job(self):
        from job_center import JobCenter
        self.worker.jobs.dispatch.close()
        self.worker.jobs.dispatch.start = lambda: None
        connector = self.start_worker()
        send = connector._send
        dropped = threading.Event()

        def drop_reply(method, path, *args, **kwargs):
            if path.endswith('/reply') and not dropped.is_set():
                dropped.set()
                raise OSError('fixture lost the accepted reply')
            return send(method, path, *args, **kwargs)

        spec = {'bin': 'fixture', 'argv': ['-c', 'pass'], 'device_access': 'none',
                'execution_node_id': self.worker.jobs.node_id}
        with patch.object(connector, '_send', side_effect=drop_reply):
            code, first = self.submit(spec, 'lost-reply')
        self.assertEqual((code, first['phase']), (200, 'submission_unknown'), first)
        self.assertTrue(dropped.is_set())
        self.center.jobs.center.close()
        self.center.jobs.center = JobCenter(self.center.jobs)
        code, repeated = self.submit(spec, 'lost-reply')
        self.assertEqual((code, repeated['id'], repeated['queue']['state']), (200, first['id'], 'queued'))
        self.assertEqual(len(self.worker.jobs.dispatch.queue.rows()), 1)

    def test_center_cannot_dial_worker_and_a_slow_reset_does_not_block_status_or_log(self):
        from test_node_connections import _center_without_outbound
        from node_connector import exchange
        context = multiprocessing.get_context('spawn')
        parent, child = context.Pipe()
        process = context.Process(target=_center_without_outbound, args=(child,))
        process.start()
        child.close()
        connector = None
        release, entered = threading.Event(), threading.Event()
        self.worker.jobs.dispatch.close()
        self.worker.jobs.dispatch.start = lambda: None
        try:
            self.assertTrue(parent.poll(10))
            url, token, center_id = parent.recv()

            def call(method, path, data=None, headers=None):
                response = exchange(url, method, path, b'' if data is None else json.dumps(data).encode(),
                    {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json', **(headers or {})})
                self.assertIn(response['status'], (200, 201), response['body'])
                return response['body']

            invitation = json.loads(call('POST', '/node-invites', {}))
            joined = json.loads(call('POST', '/node-connect/join', {'invite': invitation['code'],
                'name': 'worker', 'node_id': self.worker.jobs.node_id}))
            connector = NodeConnector({'url': url, 'center_id': center_id, 'token': joined['token']},
                LocalHTTP(self.worker.url, self.worker.admin, self.worker.jobs.node_id))
            connector.start()
            spec = {'bin': 'fixture', 'argv': ['-c', 'pass'], 'device_access': 'none',
                    'execution_node_id': self.worker.jobs.node_id}
            job = json.loads(call('POST', '/jobs', headers={'X-Job': json.dumps(spec), 'X-Request-ID': 'nat-job'}))
            self.assertEqual(job['queue']['state'], 'queued')
            reset = self.worker.jobs.reset_queue

            def slow(*args, **kwargs):
                entered.set()
                self.assertTrue(release.wait(15))
                return reset(*args, **kwargs)

            with patch.object(self.worker.jobs, 'reset_queue', side_effect=slow), ThreadPoolExecutor(1) as pool:
                pending = pool.submit(call, 'POST', '/jobs/' + job['id'] + '/reset', {'note': 'NAT fixture'})
                try:
                    self.assertTrue(entered.wait(5))
                    self.assertEqual(json.loads(call('GET', '/jobs/' + job['id']))['id'], job['id'])
                    self.assertEqual(call('GET', '/jobs/' + job['id'] + '/log'), b'')
                finally:
                    release.set()
                self.assertTrue(json.loads(pending.result(timeout=10))['released'])
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

    def test_disconnection_keeps_execution_running_and_web_reset_waits_for_reconnection(self):
        connector = self.start_worker()
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        listener.settimeout(10)
        program = ("import socket; s=socket.create_connection(%r); s.sendall(b'ready'); s.recv(1)"
                   % (listener.getsockname(),))
        spec = {'bin': 'fixture', 'argv': ['-c', program], 'device_access': 'none',
                'execution_node_id': self.worker.jobs.node_id}
        code, job = self.submit(spec, 'offline-running')
        self.assertEqual(code, 200, job)
        self.addCleanup(self.worker.jobs.reset_queue, job['id'], 'fixture cleanup')
        child, _ = listener.accept()
        self.addCleanup(child.close)
        self.assertEqual(child.recv(5), b'ready')
        self.assertEqual(self.center.call('GET', '/jobs/' + job['id'])[1]['state'], 'running')
        connector.close()
        board = self.center.board
        headers = {'X-HG': '1', 'Cookie': 'hg_dash=' + board._cookie_val('test-view')}
        path = '/dash/api/jobs/' + job['id'] + '/reset'
        self.assertEqual(self.center.call('POST', path, {'note': 'stop offline'}, headers=headers)[0], 403)
        headers['Cookie'] += '; hg_steer=' + board._cookie_val('test-steer', b'hgamey-steer-v1')
        code, result = self.center.call('POST', path, {'note': 'stop offline'}, headers=headers)
        self.assertEqual((code, result.get('pending'), result.get('released')), (202, True, False), result)
        self.assertEqual(self.worker.jobs.status(job['id'])['state'], 'running')
        finished = threading.Event()
        commit = self.worker.jobs.dispatch._commit_completion

        def committed(*args, **kwargs):
            value = commit(*args, **kwargs)
            finished.set()
            return value

        with patch.object(self.worker.jobs.dispatch, '_commit_completion', side_effect=committed):
            self.start_worker()
            self.assertTrue(finished.wait(15))
        self.assertEqual(child.recv(1), b'')
        status = self.center.call('GET', '/jobs/' + job['id'])[1]
        self.assertEqual((status['state'], status['queue']['state']), ('finished', 'done'), status)
        self.assertEqual(self.center.jobs.dispatch.queue.rows(), [])

    def phone(self, env, serial, state):
        env.jobs.bins['fixture'] = (sys.executable, str(env.root))
        env.jobs.dispatch.close()
        env.jobs.dispatch.start = lambda: None
        device_id = 'phone'
        registry = {'revision': 1, 'devices': [{'device_id': device_id, 'aliases': [serial, serial + '-wifi'], 'enabled': True}]}
        lease = {'status': 'clear', 'device_scopes': {'schema': 'phonefarm/device-scopes@1',
            'registry_revision': 1, 'queue_waiting': False, 'unknown': False,
            'global': {'status': 'clear', 'error': None},
            'devices': [{'device_id': device_id, 'aliases': [serial, serial + '-wifi'], 'status': state, 'error': None}]}}
        for target, name, options in (
                (env.jobs.leases, 'snapshot', {'return_value': lease}),
                (env.jobs.device_scopes, 'registry', {'return_value': registry}),
                (env.jobs.device_scopes, 'registered', {'side_effect': lambda chosen, _: {
                    'schema': 'phonefarm/device-binding@1', 'registry_revision': 1, 'device_id': device_id, 'serial': chosen}})):
            patcher = patch.object(target, name, **options)
            patcher.start()
            self.addCleanup(patcher.stop)
        env.jobs.fleet._fleet = {'devices': [{'serial': serial, 'model': 'NX809J', 'online': True}]}
        env.jobs.fleet._sampled_at = time.time()
        return lease

    def test_model_prefers_idle_phone_and_replay_keeps_that_phone_after_observations_change(self):
        local = self.phone(self.center, 'USB-A', 'held')
        remote = self.phone(self.worker, 'USB-B', 'clear')
        connector = self.start_worker()
        spec = {'bin': 'fixture', 'argv': ['probe.py', '--serial', '@serial'],
                'device_access': 'write', 'device_model': 'NX809J'}
        code, job = self.submit(spec, 'idle-model')
        self.assertEqual(code, 200, job)
        self.assertEqual((job['execution_node_id'], job['argv'][-1]), (self.worker.jobs.node_id, 'USB-B'))
        self.assertEqual(job['queue']['resources'], ['device:phone'])
        self.assertEqual(self.center.jobs.dispatch.queue.rows(), [])
        local['device_scopes']['devices'][0]['status'] = 'clear'
        remote['device_scopes']['devices'][0]['status'] = 'held'
        connector._json('/node-connect/report', connector.backend.snapshot())
        connector.backend.acknowledge()
        code, repeated = self.submit(spec, 'idle-model')
        self.assertEqual((code, repeated['id'], repeated['execution_node_id'], repeated['argv'][-1]),
                         (200, job['id'], self.worker.jobs.node_id, 'USB-B'))
        changed = {**spec, 'argv': ['different.py', '--serial', '@serial']}
        self.assertEqual(self.submit(changed, 'idle-model')[0], 409)
        mismatch = {**spec, 'device_ref': self.worker.jobs.node_id + '/phone',
                    'argv': ['probe.py', '--serial', 'USB-A']}
        code, error = self.submit(mismatch, 'wrong-serial')
        self.assertEqual(code, 400, error)
        self.assertIn('command serial differs', error['error'])
        self.assertEqual(len(self.worker.jobs.dispatch.queue.rows()), 1)

    def test_all_matching_phones_busy_still_selects_one_queue_and_ambiguous_alias_lists_candidates(self):
        self.phone(self.center, 'USB-A', 'held')
        self.phone(self.worker, 'USB-B', 'held')
        self.start_worker()
        spec = {'bin': 'fixture', 'argv': ['probe.py', '--serial', '@serial'],
                'device_access': 'write', 'device_model': 'NX809J'}
        code, job = self.submit(spec, 'busy-model')
        self.assertEqual((code, job['queue']['state']), (200, 'queued'), job)
        expected = min(self.center.jobs.node_id, self.worker.jobs.node_id)
        self.assertEqual(job['execution_node_id'], expected)
        self.assertEqual(sum(len(env.jobs.dispatch.queue.rows()) for env in (self.center, self.worker)), 1)
        explicit = {key: value for key, value in spec.items() if key != 'device_model'}
        explicit['device_ref'] = 'phone'
        code, error = self.submit(explicit, 'ambiguous-phone')
        self.assertEqual(code, 400, error)
        for env in (self.center, self.worker):
            self.assertIn(env.jobs.node_id + '/phone', error['error'])

    def test_explicit_raw_phone_needs_no_registration_or_fleet_sample(self):
        self.worker.jobs.dispatch.close()
        self.worker.jobs.dispatch.start = lambda: None
        self.start_worker()
        spec = {'bin': 'fixture', 'argv': ['probe.py', '--serial', '@serial'], 'device_access': 'write',
                'device_ref': self.worker.jobs.node_id + '/raw-usb'}
        code, job = self.submit(spec, 'raw-phone')
        self.assertEqual(code, 200, job)
        self.assertEqual((job['execution_node_id'], job['argv'][-1]), (self.worker.jobs.node_id, 'raw-usb'))
        self.assertEqual(job['queue']['device_scope']['binding']['serial'], 'raw-usb')

    def test_research_phone_selects_its_worker_without_using_center_program_admission(self):
        self.phone(self.worker, 'USB-B', 'clear')
        self.start_worker()
        self.center.jobs.research.write('research', 'remote-phone', {
            'schema': 'phonefarm/research-write@1', 'operation_id': 'research-phone',
            'base_revision': None, 'actor_label': None, 'reason': None,
            'data': {'title': 'One remote phone', 'objective': 'use the research phone',
                     'device_requirements': [self.worker.jobs.node_id + '/phone']}}, 'researcher')
        spec = {'bin': 'fixture', 'argv': ['probe.py', '--serial', '@serial'],
                'device_access': 'write', 'research_id': 'remote-phone'}
        with patch.object(self.center.jobs.programs, 'require_session', side_effect=ValueError('center binary differs')):
            code, job = self.submit(spec, 'research-phone')
        self.assertEqual(code, 200, job)
        self.assertEqual((job['execution_node_id'], job['argv'][-1]), (self.worker.jobs.node_id, 'USB-B'))

    def test_one_center_agent_runs_its_research_on_a_worker_and_reads_the_original_log(self):
        self.start_worker()
        self.center.jobs.research.write('research', 'central', {
            'schema': 'phonefarm/research-write@1', 'operation_id': 'central-research',
            'base_revision': None, 'actor_label': None, 'reason': None,
            'data': {'title': 'Central research', 'objective': 'execute on another computer'}}, 'researcher')
        with self.assertRaises(ValueError):
            self.worker.jobs.research.get('research', 'central')
        finished = threading.Event()
        commit = self.worker.jobs.dispatch._commit_completion

        def observed(*args, **kwargs):
            value = commit(*args, **kwargs)
            finished.set()
            return value

        spec = {'bin': 'fixture', 'argv': ['-c', "print('executed-on-worker')"],
                'device_access': 'none', 'research_id': 'central',
                'execution_node_id': self.worker.jobs.node_id}
        with patch.object(self.worker.jobs.dispatch, '_commit_completion', side_effect=observed):
            code, job = self.submit(spec, 'central-to-worker')
            self.assertEqual(code, 200, job)
            self.assertEqual(job['execution_node_id'], self.worker.jobs.node_id)
            self.assertTrue(finished.wait(15), 'worker did not finish the transferred command')
        code, status = self.center.call('GET', '/jobs/' + job['id'], token=self.agent)
        self.assertEqual((code, status['state'], status['exit']), (200, 'finished', 0), status)
        self.assertEqual((status['research_id'], status['actor_id']), ('central', 'researcher'))
        self.assertEqual(self.worker.jobs.status(job['id'])['id'], job['id'])
        self.assertEqual(self.center.jobs.dispatch.queue.rows(), [], 'the center must not reserve resources twice')
        self.assertEqual(self.submit(spec, 'central-to-worker')[1]['id'], job['id'])
        self.assertEqual(len(self.worker.jobs.dispatch.queue.rows()), 1)
        connection = http.client.HTTPConnection(*self.center.server.server_address, timeout=10)
        try:
            connection.request('GET', '/jobs/' + job['id'] + '/log', headers={'Authorization': 'Bearer ' + self.agent})
            reply = connection.getresponse()
            self.assertEqual(reply.status, 200)
            self.assertIn(b'executed-on-worker', reply.read())
        finally:
            connection.close()

    def test_reset_before_delivery_does_not_send_the_job_after_reconnection(self):
        spec = {'bin': 'fixture', 'argv': ['-c', "print('must-not-run')"], 'device_access': 'none',
                'execution_node_id': self.worker.jobs.node_id}
        code, job = self.submit(spec, 'never-delivered')
        self.assertEqual(code, 200, job)
        self.assertEqual(job['phase'], 'submission_unknown')
        self.assertIsNone(self.center.jobs.center.targets.get(job['id'])['sent_at'])
        code, reset = self.center.call('POST', '/jobs/' + job['id'] + '/reset',
                                       {'note': 'stop before delivery'}, token=self.agent)
        self.assertEqual((code, reset['released']), (200, True), reset)
        self.assertEqual(reset['job']['state'], 'finished')
        self.start_worker()
        # This read crosses the reconnected poll channel after the abandoned submission.
        reply = self.center.jobs.center.rpc(self.center.jobs.center.targets.get(job['id']), 'GET', '/queue')
        self.assertEqual(reply['status'], 200)
        self.assertEqual(self.worker.jobs.dispatch.queue.rows(), [])
        self.assertEqual(self.submit(spec, 'never-delivered')[1]['id'], job['id'])
        self.assertEqual(self.worker.jobs.dispatch.queue.rows(), [])

    def test_remote_queue_events_checkin_and_cancel_use_the_execution_node(self):
        self.worker.jobs.dispatch.close()
        self.worker.jobs.dispatch.start = lambda: None
        connector = self.start_worker()
        spec = {'bin': 'fixture', 'argv': ['-c', 'pass'], 'device_access': 'none',
                'execution_node_id': self.worker.jobs.node_id}
        code, job = self.submit(spec, 'remote-observers')
        self.assertEqual(code, 200, job)
        page = connector.backend.snapshot()
        connector._json('/node-connect/report', page)
        connector.backend.acknowledge()
        connector._json('/node-connect/report', page)
        code, queues = self.center.call('GET', '/queue', token=self.agent)
        self.assertEqual(code, 200, queues)
        remote_queue = next(item for item in queues['execution_queues'] if item['node_id'] == self.worker.jobs.node_id)
        self.assertEqual([item['job_id'] for item in remote_queue['queue']['items']], [job['id']])
        self.assertEqual(queues['items'], [], 'the center local queue stays independent')
        code, events = self.center.call('GET', '/job-events?job_id=' + job['id'], token=self.agent,
                                       headers={'X-Expected-Node-ID': self.center.jobs.node_id})
        self.assertEqual(code, 200, events)
        originals = self.worker.jobs.events.events({'job_id': [job['id']]})['items']
        self.assertEqual([item['event_id'] for item in events['items']], [item['event_id'] for item in originals])
        self.assertEqual([item['committed_status'] for item in events['items']],
                         [item['committed_status'] for item in originals])
        code, duties = self.center.call('POST', '/agents/researcher/checkin', {}, token=self.agent)
        self.assertEqual(code, 200, duties)
        self.assertEqual([item['job_id'] for item in duties['jobs']['active']], [job['id']])
        code, cancelled = self.center.call('POST', '/jobs/' + job['id'] + '/cancel', {}, token=self.agent)
        self.assertEqual((code, cancelled.get('accepted')), (200, True), cancelled)
        self.assertEqual(cancelled['job']['queue']['state'], 'done')
        self.assertEqual(self.worker.jobs.status(job['id'])['outcome'], 'cancelled')
        self.assertEqual(self.center.jobs.dispatch.queue.rows(), [])

    def test_reset_retries_remote_storage_even_when_resources_are_already_released(self):
        self.start_worker()
        spec = {'bin': 'fixture', 'argv': ['-c', "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('once')", '@out/count'], 'device_access': 'none',
                'execution_node_id': self.worker.jobs.node_id}
        failed = threading.Event()
        sync = self.worker.jobs._read_root.sync
        commit = self.worker.jobs.dispatch._commit_completion

        def fail_meta(path, directory=None):
            if path.endswith('/meta.json'):
                raise OSError('fixture result fsync failure')
            return sync(path, directory=directory)

        def committed(row, observation, error=None):
            value = commit(row, observation, error)
            if error is not None:
                failed.set()
            return value

        with patch.object(self.worker.jobs._read_root, 'sync', side_effect=fail_meta), \
                patch.object(self.worker.jobs.dispatch, '_commit_completion', side_effect=committed):
            code, job = self.submit(spec, 'remote-storage')
            self.assertEqual(code, 200, job)
            self.assertTrue(failed.wait(10))
            self.worker.jobs.dispatch.close()
        self.worker.jobs.dispatch.start = lambda: None
        status = self.center.call('GET', '/jobs/' + job['id'])[1]
        self.assertEqual(status['queue']['state'], 'done')
        self.assertIn('fixture result fsync failure', status['storage_error']['message'])
        code, reset = self.center.call('POST', '/jobs/' + job['id'] + '/reset', {'note': 'disk repaired'}, token=self.agent)
        self.assertEqual(code, 200, reset)
        self.assertTrue(reset['released'])
        self.assertTrue(reset['storage_committed'])
        self.assertEqual(Path(self.worker.jobs.path(job['id'], 'out/count')).read_text(), 'once')

    def test_reset_of_unknown_admission_waits_for_the_report_then_stops_the_same_job(self):
        connector = self.start_worker()
        self.worker.jobs.dispatch.close()
        self.worker.jobs.dispatch.start = lambda: None
        entered, release, reset_done = threading.Event(), threading.Event(), threading.Event()
        request, reset = connector.backend.request, self.worker.jobs.reset_queue
        calls = []

        def delayed(method, path, body, headers, **options):
            calls.append((method, path))
            if method == 'POST' and path.startswith('/node/executions/'):
                entered.set()
                self.assertTrue(release.wait(15))
            return request(method, path, body, headers, **options)

        def observed_reset(*args, **kwargs):
            result = reset(*args, **kwargs)
            reset_done.set()
            return result

        connector.backend.request = delayed
        spec = {'bin': 'fixture', 'argv': ['-c', 'pass'], 'device_access': 'none',
                'execution_node_id': self.worker.jobs.node_id}
        with patch.object(self.worker.jobs, 'reset_queue', side_effect=observed_reset), ThreadPoolExecutor(1) as pool:
            pending = pool.submit(self.submit, spec, 'delivered-but-unknown')
            try:
                self.assertTrue(entered.wait(5))
                record = self.center.jobs.center.targets.by_request('delivered-but-unknown')
                self.assertIsNotNone(record['sent_at'])
                code, receipt = self.center.call('POST', '/jobs/' + record['id'] + '/reset',
                    {'note': 'stop the unknown submission'}, token=self.agent)
                self.assertEqual((code, receipt['released'], receipt['pending']), (202, False, True), receipt)
                self.assertEqual(self.worker.jobs.dispatch.queue.rows(), [])
                self.assertFalse(any(path.endswith('/reset') for _, path in calls))
            finally:
                release.set()
            code, job = pending.result(timeout=10)
            self.assertEqual(code, 200, job)
            connector._json('/node-connect/report', connector.backend.snapshot())
            connector.backend.acknowledge()
            self.assertTrue(reset_done.wait(5))
        code, status = self.center.call('GET', '/jobs/' + record['id'], token=self.agent)
        self.assertEqual((code, status['queue']['state'], status['outcome']), (200, 'done', 'cancelled'))
        self.assertEqual(len(self.worker.jobs.dispatch.queue.rows()), 1)


if __name__ == '__main__':
    unittest.main()
