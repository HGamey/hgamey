"""Application observations finish before the task starts, and never veto it."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import test_core_flow_web_cli as fixture
import job_runtime
import job_apps


class JobAppTests(unittest.TestCase):
    def test_scope_uses_serials_once_and_includes_all_selected_local_phones(self):
        observation = {'device_scopes': {'registry_revision': 1, 'devices': [
            {'device_id': 'A', 'aliases': ['usb-a', 'wifi-a']},
            {'device_id': 'B', 'aliases': ['hdc:usb-b']}]}}
        self.assertEqual(job_apps.targets({'device_access': 'write', 'resources': ['device:A']}, observation), ['usb-a'])
        self.assertEqual(job_apps.targets({'device_access': 'write', 'argv': ['--serial', 'wifi-a'],
            'resources': ['device:A', 'device:B']}, observation), ['hdc:usb-b', 'wifi-a'])
        self.assertEqual(job_apps.targets({'device_access': 'write', 'resources': ['global']}, observation), ['hdc:usb-b', 'usb-a'])
        self.assertIsNone(job_apps.targets({'device_access': 'none', 'resources': ['device:A']}, observation))

    def test_the_task_page_shows_the_start_observation_and_read_errors(self):
        env = fixture.CoreFlowTests()
        self.addCleanup(env.doCleanups)
        env.setUp()
        meta = env.jobs._read_meta(env.jid)
        meta['app_snapshot'] = {'at': 1700000000, 'errors': [], 'devices': [
            {'serial': 'S1', 'at': 1700000000, 'foreground': 'com.example.game',
             'running_third_party': ['com.example.game', 'com.example.megacity'], 'errors': []},
            {'serial': 'hdc:S2', 'at': 1700000001, 'foreground': None,
             'running_third_party': None, 'errors': ['device disconnected']}]}
        env.jobs._write_meta(env.jid, meta)
        env.jobs.dispatch.queue.set_state(env.jid, 'done')
        self.assertEqual(env.web('GET', f'/jobs/{env.jid}')[1]['app_snapshot'], meta['app_snapshot'])
        with env._browser_page() as page:
            from playwright.sync_api import expect
            page.goto(f'http://%s:%d/dash/jobs#job={env.jid}' % env.address)
            facts = page.locator('#execution-info')
            expect(facts).to_contain_text('开始时应用')
            for value in ('S1', 'hdc:S2', 'com.example.game', 'com.example.megacity', 'device disconnected', '2023'):
                expect(facts).to_contain_text(value)

    def test_write_tasks_record_apps_before_execution_and_sampling_failure_does_not_block(self):
        env = fixture.CoreFlowTests()
        self.addCleanup(env.doCleanups)
        env.setUp()
        env.jobs.cancel(env.jid)
        tool = env.tmp / "fake-pf"
        tool.write_text(f"#!{sys.executable}\n" + """
import json,signal,sys
assert sys.argv[1:3]==['app-snapshot','--serial'],sys.argv
serial=sys.argv[3]
if serial=='unavailable': sys.exit('device disconnected')
if serial=='hung': signal.pause()
print(json.dumps(dict(serial=serial,foreground='com.example.game',running_third_party=['com.example.game','com.example.megacity'],errors=[])))
""")
        tool.chmod(0o755)
        env.jobs.bins['phonefarm'] = (str(tool), str(env.tmp))
        real_launch, workers = job_runtime.launch, []
        def launch(*args, **kwargs):
            worker = real_launch(*args, **kwargs)
            workers.append(worker)
            self.addCleanup(lambda: worker.wait(timeout=30))
            return worker
        for serial in ('android-test', 'hdc:harmony-test', 'unavailable', 'hung'):
            with self.subTest(serial=serial):
                program = "import json,os,pathlib; p=pathlib.Path(os.environ['PF_JOB_OUT']); p.joinpath('at-start.json').write_text(p.parent.joinpath('meta.json').read_text())"
                reply = env.jobs.dispatch.submit({'bin': 'fixture', 'argv': ['-c', program],
                    'device_access': 'write', 'resources': ['device:' + serial]}, b'', 'app-' + serial)
                jid = reply.get('job', reply)['id']
                row = env.jobs.dispatch.queue.claim_next(1)
                self.assertEqual(row['job_id'], jid)
                with patch.object(job_runtime, 'launch', side_effect=launch):
                    env.jobs.dispatch.launch(row)
                workers[-1].wait(timeout=15)
                env.jobs.dispatch.finalize(env.jobs.dispatch.queue.get(jid))
                status = env.jobs.status(jid)
                self.assertEqual(status['outcome'], 'succeeded', status)
                captured = json.loads(Path(env.jobs.path(jid, 'out', 'at-start.json')).read_text())
                self.assertIn('app_snapshot', captured)
                self.assertEqual(status['app_snapshot'], captured['app_snapshot'])
                observation = captured['app_snapshot']['devices'][0]
                self.assertEqual(observation['serial'], serial)
                if serial in ('unavailable', 'hung'):
                    self.assertIn('device disconnected' if serial == 'unavailable' else 'timed out', observation['errors'][0])
                    self.assertIsNone(observation['running_third_party'])
                else:
                    self.assertEqual(observation['foreground'], 'com.example.game')
                    self.assertEqual(observation['running_third_party'], ['com.example.game', 'com.example.megacity'])
                    self.assertEqual(observation['errors'], [])
