"""An omitted device-access flag must not quietly lose a named phone (F12)."""
import json
import sys
import unittest
from unittest.mock import patch

import test_node_games as fixture
import test_job_experiment as experiment
import test_multi_node_jobs as multi
from test_device_scope import PROGRAM, bound
from node_service import Jobs


class JobDeviceDefaults(unittest.TestCase):
    def setUp(self):
        self.env = fixture.GameFlowTests()
        self.env.setUp()
        self.addCleanup(self.env.tearDown)
        self.jobs = self.env.jobs
        self.jobs.dispatch.start = lambda: None
        source = self.env.root / 'source'
        source.mkdir()
        (source / 'baseline_cmp.sh').write_text('echo "$1"\n')
        experiment.git('init', '-q', '-b', 'main', str(source))
        experiment.git('add', '.', cwd=source)
        experiment.git('commit', '-qm', 'fixture', cwd=source)
        self.commit = experiment.git('rev-parse', 'HEAD', cwd=source)
        self.jobs.experiment_url = str(source)
        self.jobs.bins['experiment'] = (sys.executable, str(self.env.root))
        program = self.env.root / 'phonefarm-fixture'
        program.write_text('#!' + sys.executable + '\n' + PROGRAM)
        program.chmod(0o755)
        self.jobs.bins['phonefarm'] = (str(program), str(self.env.root))
        self.lease = {'status': 'clear', 'device_scopes': {
            'schema': 'phonefarm/device-scopes@1', 'registry_revision': 1,
            'unknown': False, 'queue_waiting': False, 'global': {'status': 'clear', 'error': None},
            'devices': [{'device_id': 'REDMAGIC', 'aliases': ['91253241019A', 'wifi-red'],
                         'status': 'clear', 'error': None},
                        {'device_id': 'HUAWEI', 'aliases': ['hdc:88X9K26901026869'],
                         'status': 'clear', 'error': None}]}}
        self.save_registry()
        patcher = patch.object(self.jobs.leases, 'snapshot', return_value=self.lease)
        patcher.start()
        self.addCleanup(patcher.stop)

    def save_registry(self):
        (self.env.root / 'registry.json').write_text(json.dumps({'revision': 1, 'aliases': {
            alias: phone['device_id'] for phone in self.lease['device_scopes']['devices']
            for alias in phone['aliases']}}))

    def spec(self, *args, **extra):
        return {'bin': 'experiment', 'argv': ['baseline_cmp.sh', *args],
                'experiment': {'commit': self.commit}, **extra}

    def submit(self, spec, key):
        code, job = self.env.call('POST', '/jobs', headers={
            'X-Job': json.dumps(spec), 'X-Request-ID': key})
        self.assertEqual(code, 200, job)
        return job

    def test_a_positional_registered_alias_defaults_to_write_with_binding_and_home_reservation(self):
        job = self.submit(self.spec('91253241019A'), 'default-phone')
        self.assertEqual(job['device_access'], 'write')
        self.assertEqual(job['resources'], ['device:REDMAGIC'])
        self.assertEqual(job['device_binding'], bound('REDMAGIC', '91253241019A'))
        queue = self.jobs.dispatch.queue
        self.assertEqual(queue.claim_next(2, lease_snapshot=self.lease)['job_id'], job['id'])
        owners = self.env.board._reservations()
        self.assertEqual(self.env.board._reserved('91253241019A', owners)['job_id'], job['id'])
        self.assertIsNone(self.env.board._reserved('hdc:88X9K26901026869', owners))

    def test_explicit_access_and_non_matching_arguments_keep_their_defaults(self):
        cases = [self.spec('91253241019A', device_access='none'),
                 self.spec('not-registered'), self.spec('--phone=91253241019A'),
                 self.spec('prefix-91253241019A-suffix')]
        for index, spec in enumerate(cases):
            with self.subTest(spec=spec):
                job = self.submit(spec, 'unchanged-' + str(index))
                self.assertEqual((job['device_access'], job['resources']), ('none', []))
                self.assertNotIn('device_binding', job)
        job = self.submit(self.spec('91253241019A', device_access='write'), 'explicit-write')
        self.assertEqual((job['device_access'], job['resources']), ('write', []))
        self.assertNotIn('device_binding', job)

    def test_two_omitted_flags_for_the_same_phone_queue_while_another_phone_can_claim(self):
        first = self.submit(self.spec('91253241019A'), 'first')
        second = self.submit(self.spec('wifi-red'), 'same-phone')
        other = self.submit(self.spec('hdc:88X9K26901026869'), 'other-phone')
        queue = self.jobs.dispatch.queue
        self.assertEqual(queue.claim_next(2, lease_snapshot=self.lease)['job_id'], first['id'])
        self.assertEqual(queue.claim_next(2, lease_snapshot=self.lease)['job_id'], other['id'])
        self.assertEqual(queue.get(second['id'])['state'], 'queued')
        queue.set_state(first['id'], 'done')
        self.assertEqual(queue.claim_next(2, lease_snapshot=self.lease)['job_id'], second['id'])

    def test_multiple_named_phones_reserve_each_device_once(self):
        job = self.submit(self.spec('91253241019A', 'wifi-red', 'hdc:88X9K26901026869',
                                    resources=['capture:power']), 'two-phones')
        self.assertEqual(job['device_access'], 'write')
        self.assertEqual(job['resources'], ['capture:power', 'device:HUAWEI', 'device:REDMAGIC'])
        explicit = self.submit(self.spec('91253241019A', 'wifi-red', 'hdc:88X9K26901026869',
            device_access='write', resources=job['resources']), 'explicit-two')
        self.assertEqual(job['queue']['device_scope'], explicit['queue']['device_scope'])

    def test_inferred_request_replay_keeps_the_original_choice_without_registry_reads(self):
        spec = self.spec('91253241019A')
        job = self.submit(spec, 'frozen')
        self.assertEqual(job['device_access'], 'write')
        self.lease['device_scopes']['devices'] = []
        self.save_registry()
        with patch.object(self.jobs.device_scopes, 'registry', side_effect=AssertionError('reselected')):
            again = self.submit(spec, 'frozen')
        self.assertEqual(again['id'], job['id'])
        self.assertEqual(again['device_binding'], job['device_binding'])
        code, _ = self.env.call('POST', '/jobs', headers={
            'X-Request-ID': 'frozen', 'X-Job': json.dumps(self.spec('91253241019A', device_access='none'))})
        self.assertEqual(code, 409)

    def test_a_previously_unmatched_request_stays_the_same_after_registering_its_serial(self):
        spec = self.spec('later-registered')
        job = self.submit(spec, 'old-none')
        self.assertEqual(job['device_access'], 'none')
        self.lease['device_scopes']['devices'][0]['aliases'].append('later-registered')
        self.save_registry()
        again = self.submit(spec, 'old-none')
        self.assertEqual((again['id'], again['device_access'], again['resources']), (job['id'], 'none', []))

    def test_standalone_jobs_infer_local_phones_without_an_http_node_directory(self):
        root = str(self.env.root / 'standalone')
        jobs = Jobs(root, {'fixture': (sys.executable, root), 'phonefarm': self.jobs.bins['phonefarm']})
        self.addCleanup(jobs._read_root.close)
        self.addCleanup(jobs.center.close)
        self.addCleanup(jobs.dispatch.close)
        jobs.dispatch.start = lambda: None
        with patch.object(jobs.leases, 'snapshot', return_value=self.lease):
            job = jobs.start({'bin': 'fixture', 'argv': ['-c', 'pass', '91253241019A']}, b'', 'standalone')
        self.assertEqual((job['device_access'], job['resources']), ('write', ['device:REDMAGIC']))
        self.assertEqual(job['device_binding'], bound('REDMAGIC', '91253241019A'))


class MultiNodeDeviceDefaults(unittest.TestCase):
    def test_cli_positional_alias_routes_to_its_computer_and_replay_does_not_select_again(self):
        env = multi.MultiNodeJobTests()
        env.setUp()
        self.addCleanup(env.doCleanups)
        env.phone(env.center, 'USB-A', 'clear')
        env.phone(env.worker, 'USB-B', 'clear')
        env.start_worker()
        result = env.cli('--request-id', 'inferred-worker', '--detach', 'fixture', 'probe.py', 'USB-B')
        self.assertEqual(result.returncode, 0, result.stderr)
        record = env.center.jobs.center.targets.by_request('inferred-worker')
        self.assertIsNotNone(record, 'the named worker phone must route the request off the center')
        job = env.worker.jobs.status(record['id'])
        self.assertEqual(job['device_access'], 'write')
        self.assertEqual(job['resources'], ['device:phone'])
        self.assertEqual(job['device_binding']['device_id'], 'phone')
        self.assertEqual(record['execution_node_id'], env.worker.jobs.node_id)
        self.assertEqual(env.center.jobs.dispatch.queue.rows(), [])
        with patch.object(env.center.jobs.center, 'select_request', side_effect=AssertionError('reselected')):
            again = env.cli('--request-id', 'inferred-worker', '--detach', 'fixture', 'probe.py', 'USB-B')
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(again.stdout, result.stdout)
