"""Task provenance and nested CLI calls use the executing node's persistent ID."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import test_multi_node_jobs as fixture
import job_cgroup

NODE = Path(__file__).resolve().parents[1]


class JobNodeIdentityTests(unittest.TestCase):
    def test_dispatched_jobs_get_the_execution_node_id_and_can_call_pf_node(self):
        flow = fixture.MultiNodeJobTests()
        flow.setUp()
        self.addCleanup(flow.doCleanups)
        flow.center.jobs.bins['fixture'] = (sys.executable, str(flow.center.root))
        flow.start_worker()
        script = '''
import json,os,subprocess,sys
from pathlib import Path
facts = {'node_id': os.environ.get('PF_NODE_ID'), 'reads': []}
for options in ([], ['--url', sys.argv[2], '--node-id', sys.argv[3]]):
    result = subprocess.run([sys.executable, sys.argv[1], *options, 'node'],
                            capture_output=True, text=True, timeout=10)
    facts['reads'].append({'exit': result.returncode, 'stderr': result.stderr,
                          'value': json.loads(result.stdout) if result.returncode == 0 else None})
(Path(os.environ['PF_JOB_OUT'])/'node.json').write_text(json.dumps(facts))
'''
        for executing, other in ((flow.center, flow.worker), (flow.worker, flow.center)):
            with self.subTest(node_id=executing.jobs.node_id):
                executing.jobs.pf_env = {'PATH': os.defpath, 'NO_PROXY': '*', 'PF_NODE_URL': executing.url,
                                         'PF_NODE_TOKEN': executing.admin}
                if executing is flow.worker:
                    executing.jobs.pf_env['PF_NODE_ID'] = flow.center.jobs.node_id
                completed = threading.Event()
                commit = executing.jobs.dispatch._commit_completion

                def committed(*args, **kwargs):
                    result = commit(*args, **kwargs)
                    completed.set()
                    return result

                with patch.object(executing.jobs.dispatch, '_commit_completion', side_effect=committed):
                    code, job = flow.submit({'bin': 'fixture', 'argv': ['-c', script, str(NODE / 'pf-node'),
                                            other.url, other.jobs.node_id], 'device_access': 'none',
                                            'execution_node_id': executing.jobs.node_id},
                                           'node-id-' + executing.jobs.node_id)
                    self.assertEqual(code, 200, job)
                    self.assertTrue(completed.wait(15), executing.jobs.status(job['id']))
                status = executing.jobs.status(job['id'])
                self.assertEqual(status['exit'], 0, status)
                facts = json.loads(Path(executing.jobs.path(job['id'], 'out/node.json')).read_text())
                self.assertEqual(facts['node_id'], executing.jobs.node_id)
                for read, expected in zip(facts['reads'], (executing.jobs.node_id, other.jobs.node_id)):
                    self.assertEqual(read['exit'], 0, read['stderr'])
                    self.assertEqual(read['value']['node_id'], expected)

    def test_contained_gate_supplies_the_persisted_node_id_without_inheriting_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / '_node').mkdir()
            node_id = '1' * 32
            (root / '_node/id').write_text(node_id + '\n')
            jid = '0123456789ab'
            job = root / 'jobs' / jid
            (job / 'out').mkdir(parents=True)
            domain = job_cgroup.Domain.__new__(job_cgroup.Domain)
            domain.uid, domain.gid = os.getuid(), os.getgid()
            spawn = subprocess.Popen

            def unprivileged_gate(*args, **kwargs):
                # Only emulate the privilege boundary; execute the real shell
                # gate and child with exactly the environment start_gate built.
                for key in ('user', 'group', 'extra_groups', 'preexec_fn'):
                    kwargs.pop(key)
                return spawn(*args, **kwargs)

            with tempfile.TemporaryFile() as procs, (job / 'log').open('wb') as log:
                domain.procs = procs.fileno()
                command = [sys.executable, '-c',
                           "import json,os;print(json.dumps(dict(os.environ)))"]
                with patch.dict(os.environ, PF_NODE_ID='2' * 32, PF_NODE_TOKEN='must-not-inherit'), \
                        patch.object(job_cgroup.subprocess, 'Popen', side_effect=unprivileged_gate):
                    child = domain.start_gate(command, str(job / 'out'), str(job), jid, log.fileno())
                try:
                    child.stdin.write(b'GO\n')
                    child.stdin.close()
                    self.assertEqual(child.wait(timeout=5), 0)
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.wait(timeout=5)
            environment = json.loads((job / 'log').read_text())
            self.assertEqual(environment.get('PF_NODE_ID'), node_id)
            self.assertNotIn('PF_NODE_TOKEN', environment)


if __name__ == '__main__':
    unittest.main()
