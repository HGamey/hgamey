"""Reading configured PF help needs neither a device nor a task slot."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_node_games as fixture

NODE = Path(__file__).resolve().parents[1]


class PhonefarmHelpTests(unittest.TestCase):
    def setUp(self):
        self.env = fixture.GameFlowTests()
        self.env.setUp()
        self.addCleanup(self.env.tearDown)
        self.env.jobs.dispatch.start = lambda: None
        self.env.jobs.bins['fixture'] = (sys.executable, str(self.env.root))
        self.env.jobs.dispatch.max_running = 1
        job = self.env.jobs.start({'bin': 'fixture', 'argv': ['-c', 'pass'],
                                  'device_access': 'write', 'resources': ['legacy-device']}, b'')
        self.jid = job['id']
        self.assertEqual(self.env.jobs.dispatch.queue.claim_next(1)['job_id'], self.jid)
        self.before = self.env.jobs.dispatch.queue.rows()
        self.program = self.env.root / 'phonefarm'
        self.program.write_text(f'#!{sys.executable}\n' + '''
import json,pathlib,sys
args=sys.argv[1:]
with (pathlib.Path(__file__).parent/'pf-calls.jsonl').open('a') as stream:
    stream.write(json.dumps(args)+'\\n')
assert args[0]=='help', args
print('用法: phonefarm '+(' '.join(args[1:]) or '<命令>'))
''')
        self.program.chmod(0o700)
        self.env.jobs.bins['phonefarm'] = (str(self.program), str(self.env.root))

    def cli(self, *args, token=None, options=()):
        environment = {'PATH': os.defpath, 'NO_PROXY': '*', 'HOME': str(self.env.root),
                       'PF_NODE_URL': self.env.url, 'PF_NODE_ID': self.env.jobs.node_id,
                       'PF_NODE_TOKEN': token or self.env.admin}
        return subprocess.run([sys.executable, str(NODE/'pf-node'), *options, 'phonefarm', *args],
                              env=environment, cwd=self.env.root, capture_output=True, text=True, timeout=5)

    def test_help_returns_while_the_device_and_last_task_slot_are_held(self):
        with patch.object(self.env.jobs, 'start', side_effect=ValueError('help entered the job queue')) as submit, \
                patch.object(self.env.jobs.leases, 'snapshot', side_effect=AssertionError('help probed device locks')):
            for args in (['gray-layer', '--help'], ['gray-layer', '-h'],
                         ['gray-layer', 'session', '--help'], ['devices', '--help'], ['--help']):
                with self.subTest(args=args):
                    result = self.cli(*args)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('用法: phonefarm ', result.stdout)
                    self.assertNotIn('request_id=', result.stderr)
            submit.assert_not_called()
        calls = [json.loads(line) for line in (self.env.root/'pf-calls.jsonl').read_text().splitlines()]
        self.assertEqual(calls, [['help', 'gray-layer'], ['help', 'gray-layer'],
                                 ['help', 'gray-layer', 'session'], ['help', 'devices'], ['help']])
        self.assertEqual(self.env.jobs.dispatch.queue.rows(), self.before)

    def test_help_works_with_a_readonly_token(self):
        from node_service import readonly_token
        result = self.cli('gray-layer', '--help', token=readonly_token(self.env.admin))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('用法: phonefarm gray-layer', result.stdout)
        self.assertEqual(self.env.jobs.dispatch.queue.rows(), self.before)

    def test_an_option_value_named_help_keeps_the_normal_argument_parser(self):
        result = self.cli('gray-layer', '--lock-label', '--help', options=('--detach',))
        self.assertEqual(result.returncode, 0, result.stderr)
        job = self.env.jobs.dispatch.queue.get(result.stdout.strip())
        self.assertEqual(job['spec']['argv'], ['gray-layer', '--lock-label', '--help'])
        self.assertEqual(job['state'], 'queued')
        self.assertFalse((self.env.root/'pf-calls.jsonl').exists())


if __name__ == '__main__':
    unittest.main()
