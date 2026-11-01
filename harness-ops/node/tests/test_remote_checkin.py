"""A delayed execution report must not fall behind the agent's check-in cursor."""
import sys
import unittest
from unittest.mock import patch

import test_node_games as fixture
from job_targets import JobTargets


class RemoteCheckinTests(unittest.TestCase):
    def setUp(self):
        self.env = fixture.GameFlowTests()
        self.env.setUp()
        self.addCleanup(self.env.tearDown)
        self.jobs = self.env.jobs
        self.jobs.dispatch.close()
        self.jobs.dispatch.start = lambda: None
        # This fixture reports execution facts; it has no worker artifact server.
        self.jobs.center.artifacts.close()
        directory = self.jobs.directory
        self.worker = 'b' * 32
        joined = directory.join({'invite': directory.invite()['code'], 'name': 'worker', 'node_id': self.worker})
        self.worker_token = joined['token']
        code, invite = self.env.call('POST', '/agent-invites', {'url': self.env.url})
        self.assertEqual(code, 201, invite)
        code, agent = self.env.call('POST', '/join', {'invite': invite['code'], 'name': 'researcher'})
        self.assertEqual(code, 201, agent)
        self.agent_token = agent['token']
        self.jid = 'a' * 12
        self.jobs.center.targets.reserve({'id': self.jid, 'request_id': 'late-completion',
            'fingerprint': 'fixture', 'execution_node_id': self.worker,
            'request': {'spec': {'bin': 'fixture', 'argv': [], 'actor_id': 'researcher'}},
            'submitted_at': 80, 'sent_at': 85, 'stop': None, 'device_selection': None, 'observation': None})
        self.running = {'id': self.jid, 'state': 'running', 'phase': 'running',
                        'queue': {'state': 'claimed'}, 'status_revision': 1, 'status_committed_at': 90,
                        'recovery': {'status': 'unknown'}}
        self.finished = {**self.running, 'state': 'finished', 'phase': 'finished', 'outcome': 'interrupted',
                         'queue': {'state': 'done'}, 'status_revision': 2, 'status_committed_at': 110,
                         'recovery': {'status': 'required'}}

    def report(self, at, observation):
        with patch('time.time', return_value=at):
            code, reply = self.env.call('POST', '/node-connect/report',
                {'at': at, 'executions': [observation]}, token=self.worker_token)
        self.assertEqual(code, 200, reply)

    def checkin(self, at):
        with patch('time.time', return_value=at):
            code, reply = self.env.call('POST', '/agents/researcher/checkin', {}, token=self.agent_token)
        self.assertEqual(code, 200, reply)
        return reply

    def assert_completion(self, checked, expected):
        self.assertEqual([item['job_id'] for item in checked['jobs']['finished_since_last_checkin']], expected)
        self.assertEqual([item['target'] for item in checked['duties'] if item['kind'] == 'restore_device'], expected)

    def test_delayed_completion_is_seen_once_even_after_more_heartbeats_and_reopening(self):
        self.report(100, self.running)
        # Worker committed at 110; the hub still knows only its running report.
        before = self.checkin(120)
        self.assertEqual([item['job_id'] for item in before['jobs']['active']], [self.jid])
        self.assert_completion(before, [])
        self.report(130, self.finished)
        after = self.checkin(140)
        self.assertEqual(after['jobs']['active'], [])
        self.assert_completion(after, [self.jid])

        self.jobs.center.targets = JobTargets(self.jobs.dispatch.queue)
        self.report(150, {**self.finished, 'execution': {'last_observed_at': 150}, 'log_size': 123})
        self.assert_completion(self.checkin(160), [])
        self.report(170, self.finished)
        self.assert_completion(self.checkin(180), [])
        row = self.jobs.center.task_rows()[0]
        self.assertEqual(row['status_committed_at'], 130)
        status = self.jobs.center.status(self.jid)
        self.assertEqual(status['status_committed_at'], 110, 'the original worker timestamp remains public')
        self.assertEqual(status['observed_at'], 170, 'connection freshness still follows heartbeats')

    def test_a_second_observer_with_an_old_record_does_not_refresh_the_state_time(self):
        self.report(100, self.running)
        stale = self.jobs.center.targets.get(self.jid)
        self.report(130, self.finished)
        self.checkin(140)
        with patch('time.time', return_value=150):
            self.jobs.center.observe(stale, self.finished)
        self.assert_completion(self.checkin(160), [])
        self.assertEqual(self.jobs.center.task_rows()[0]['status_committed_at'], 130)

    def test_local_completion_keeps_its_original_commit_time(self):
        self.checkin(100)
        self.jobs.bins['fixture'] = (sys.executable, str(self.env.root))
        with patch('time.time', return_value=110):
            local = self.jobs.start({'bin': 'fixture', 'argv': ['-c', 'pass'],
                                    'device_access': 'none', 'actor_id': 'researcher'}, b'', 'local')
            self.jobs.reset_queue(local['id'], 'cancel before launch')
        checked = self.checkin(120)
        self.assertEqual([item['job_id'] for item in checked['jobs']['finished_since_last_checkin']], [local['id']])
        row = next(row for row in self.jobs.center.task_rows() if row['job_id'] == local['id'])
        self.assertEqual(row['status_committed_at'], 110)
        self.assertEqual(self.jobs.status(local['id'])['status_committed_at'], 110)
        self.assert_completion(self.checkin(130), [])
