"""Automatic updates use the real install, outbound connection and job lifecycle."""
import contextlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import test_node_install as fixture

deploy = fixture.deploy


class AutomaticUpdateTests(unittest.TestCase):
    def setUp(self):
        self.flow = fixture.NodeInstallTests()
        self.flow.setUp()
        self.addCleanup(self.flow.doCleanups)
        self.root, self.center = self.flow.root, self.flow.center
        original = self.flow.service_command
        def commands(argv, **kwargs):
            if argv[0] == sys.executable and str(argv[1]).endswith('/deploy_overlay.py'):
                self.assertEqual(argv[2], 'rollback')
                subprocess.run([str(value) for value in argv], check=True, capture_output=True)
                return
            if argv[:3] == ['systemctl', '--user', 'enable'] and argv[-1].endswith('.timer'):
                self.timer = argv[-1]
                return
            return original(argv, **kwargs)
        deploy.run.side_effect = commands
        invitation = self.center.jobs.directory.invite()
        with contextlib.redirect_stdout(self.flow.output):
            self.assertEqual(deploy.join_node(self.center.url, invitation['code'], 'installed',
                self.root, self.flow.port, self.flow.revision, False, home=self.flow.home), 0)
        self.config_file = self.root / 'deploy-node.json'
        self.config = deploy.load_config(None, self.config_file)
        self.node_id = (self.root / 'data/_node/id').read_text().strip()
        self.center.jobs.service_at_launch['service']['revision'] = self.flow.other

    def auto(self):
        with contextlib.redirect_stdout(self.flow.output):
            return deploy.auto_update(self.config_file, fetch=False)

    def state(self):
        return json.loads((self.root / 'data/_node/update.json').read_text())

    def test_idle_executor_updates_and_reports_its_running_revision(self):
        reported = threading.Event()
        report = self.center.jobs.directory.report
        def observed(node_id, snapshot=None):
            entry = report(node_id, snapshot)
            if ((snapshot or {}).get('identity', {}).get('updates', {}).get('status') == 'updated'):
                reported.set()
            return entry
        with patch.object(self.center.jobs.directory, 'report', side_effect=observed):
            self.assertEqual(self.auto(), 0)
            self.assertTrue(reported.wait(45), 'executor did not report its completed automatic update')
        self.assertEqual(deploy.reports(self.config, 0)[0]['service'], self.flow.other)
        state = self.state()
        self.assertEqual((state['status'], state['recommended_revision'], state['behind']),
                         ('updated', self.flow.other, 0))
        entry = self.center.jobs.directory.get('installed')
        self.assertEqual(entry['snapshot']['identity']['updates']['current_revision'], self.flow.other)
        self.assertEqual(entry['snapshot']['identity']['updates']['status'], 'updated', entry)
        self.assertEqual(entry['node_id'], self.node_id)
        self.assertEqual(self.timer, 'hgamey-node-update-' + self.node_id + '.timer')

    def test_owner_pause_and_resume_survive_another_update_invocation(self):
        def owner(action):
            result = subprocess.run([sys.executable, str(self.root / 'deploy.py'), 'auto',
                '--config', str(self.config_file), action], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        self.assertTrue(owner('--pause')['paused'])
        built = deploy.build.call_count
        self.assertEqual(self.auto(), 0)
        self.assertEqual(deploy.build.call_count, built)
        self.assertEqual(self.state()['status'], 'paused')
        self.assertEqual(deploy.reports(self.config, 0)[0]['service'], self.flow.revision)
        self.assertFalse(owner('--resume')['paused'])
        self.assertEqual(self.auto(), 0)
        self.assertEqual(deploy.reports(self.config, 0)[0]['service'], self.flow.other)

    def hold_job(self):
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(('127.0.0.1', 0)); listener.listen(); listener.settimeout(15)
        lock = self.root / 'hold-lock'
        code, job = self.center.call('POST', '/jobs', headers={
            'X-Request-ID': 'busy-update', 'X-Job': json.dumps({'bin': 'phonefarm',
                'argv': ['fixture-hold', str(listener.getsockname()[1]), str(lock)],
                'device_access': 'none', 'execution_node_id': self.node_id})})
        self.assertEqual(code, 200, job)
        child, _ = listener.accept()
        self.addCleanup(child.close)
        def finished():
            child.sendall(b'x')
            self.assertEqual(child.recv(8), b'released')
            deadline = time.monotonic() + 10
            while self.center.call('GET', '/jobs/' + job['id'])[1]['queue']['state'] != 'done':
                self.assertLess(time.monotonic(), deadline, 'job has not finished writing its directory')
                time.sleep(.02)
        self.addCleanup(finished)
        self.assertEqual(child.recv(5), b'ready')
        return child

    def test_running_job_defers_without_building_or_stopping_the_service(self):
        self.hold_job()
        built, pid = deploy.build.call_count, self.flow.process.pid
        self.assertEqual(self.auto(), 0)
        self.assertEqual(deploy.build.call_count, built)
        self.assertEqual(self.flow.process.pid, pid)
        self.assertEqual(self.state()['status'], 'waiting')
        self.assertEqual(self.state()['behind'], 1)

    def test_job_starting_during_build_keeps_the_original_service(self):
        build = deploy.build.side_effect
        def built(*args, **kwargs):
            result = build(*args, **kwargs)
            self.hold_job()
            return result
        deploy.build.side_effect = built
        pid = self.flow.process.pid
        self.assertEqual(self.auto(), 0)
        self.assertEqual(self.flow.process.pid, pid)
        self.assertEqual(deploy.reports(self.config, 0)[0]['service'], self.flow.revision)
        self.assertEqual(self.state()['status'], 'waiting')

    def test_owner_can_pause_during_build_before_the_release_switch(self):
        build = deploy.build.side_effect
        def built(*args, **kwargs):
            result = build(*args, **kwargs)
            deploy.pause_updates(self.config_file, True)
            return result
        deploy.build.side_effect = built
        pid = self.flow.process.pid
        self.assertEqual(self.auto(), 0)
        self.assertEqual(self.flow.process.pid, pid)
        self.assertEqual(self.state()['status'], 'paused')
        self.assertEqual(deploy.reports(self.config, 0)[0]['service'], self.flow.revision)

    def test_build_failure_keeps_old_files_and_running_service(self):
        pid = self.flow.process.pid
        original = (self.root / 'phonefarm/phonefarm').read_bytes()
        deploy.build.side_effect = subprocess.CalledProcessError(1, ['cargo', 'build'])
        self.assertEqual(self.auto(), 2)
        self.assertEqual(self.flow.process.pid, pid)
        self.assertEqual((self.root / 'phonefarm/phonefarm').read_bytes(), original)
        self.assertEqual(deploy.reports(self.config, 0)[0]['service'], self.flow.revision)
        self.assertEqual(self.state()['status'], 'failed')

    def test_failed_version_check_restores_files_and_restarts_old_release(self):
        original = (self.root / 'phonefarm/phonefarm').read_bytes()
        build = deploy.build.side_effect
        def mismatched(*args, **kwargs):
            result = build(*args, **kwargs)
            program = result / 'phonefarm'
            program.write_text(program.read_text().replace(repr(self.flow.other), repr('f' * 40)))
            return result
        deploy.build.side_effect = mismatched
        self.assertEqual(self.auto(), 2)
        self.assertEqual((self.root / 'phonefarm/phonefarm').read_bytes(), original)
        reports = deploy.reports(self.config, 0)[0]
        self.assertEqual([reports[key] for key in ('service', 'phonefarm', 'game_opt_loop')],
                         [self.flow.revision] * 3)
        self.assertEqual(self.state()['status'], 'rolled_back')


class UpdateServicePlanTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / 'node with spaces'
        self.root.mkdir()
        self.home = self.base / 'home'

    def test_macos_updater_is_an_independent_periodic_system_daemon(self):
        plan = deploy.update_service_plan(self.root, 'a' * 32, 'darwin', self.home)
        filename, data = next(iter(plan['files'].items()))
        value = plistlib.loads(data)
        self.assertEqual(value['StartInterval'], 60)
        self.assertTrue(value['RunAtLoad'])
        self.assertNotIn('KeepAlive', value)
        self.assertEqual(value['UserName'], deploy.pwd.getpwuid(os.getuid()).pw_name)
        self.assertEqual(value['ProgramArguments'], [sys.executable, str(self.root / 'deploy.py'),
            'auto', '--config', str(self.root / 'deploy-node.json')])
        self.assertEqual(value['WorkingDirectory'], str(self.root))
        target = '/Library/LaunchDaemons/' + value['Label'] + '.plist'
        self.assertEqual(plan['commands'], [
            ['sudo', '/usr/bin/install', '-o', 'root', '-g', 'wheel', '-m', '644', str(filename), target],
            ['sudo', '/bin/launchctl', 'bootstrap', 'system', target]])

    def test_linux_periodic_units_are_accepted_by_systemd(self):
        plan = deploy.update_service_plan(self.root, 'a' * 32, 'linux', self.home)
        for filename, content in plan['files'].items():
            filename.parent.mkdir(parents=True, exist_ok=True)
            filename.write_bytes(content)
        service = next(p for p in plan['files'] if p.suffix == '.service')
        timer = next(p for p in plan['files'] if p.suffix == '.timer')
        self.assertIn('Type=oneshot', service.read_text())
        self.assertIn('WorkingDirectory=' + str(self.root) + '\n', service.read_text())
        self.assertIn('OnUnitInactiveSec=60', timer.read_text())
        self.assertEqual(plan['commands'], [['systemctl', '--user', 'daemon-reload'],
            ['systemctl', '--user', 'enable', '--now', timer.name]])
        analyzer = shutil.which('systemd-analyze')
        if analyzer:
            runtime = self.base / 'runtime'; runtime.mkdir(mode=0o700)
            result = subprocess.run([analyzer, '--user', 'verify', str(service), str(timer)],
                env={**os.environ, 'XDG_RUNTIME_DIR': str(runtime)}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_bootstrap_installs_only_the_updater_and_preserves_the_old_program(self):
        data = self.root / 'data/_node'; data.mkdir(parents=True)
        (data / 'id').write_text('a' * 32)
        old = self.root / 'phonefarm'; old.write_bytes(b'old program')
        config = {'schema': deploy.CONFIG_SCHEMA, 'root': str(self.root), 'work': str(self.root / 'work'),
            'jobs_roots': [str(self.root / 'data/jobs')], 'stop': [], 'start': [], 'nodes': [], 'gray_layer': False}
        config_file = self.root / 'deploy-node.json'; config_file.write_text(json.dumps(config))
        with patch.object(deploy, 'run') as run, patch.object(deploy, 'deploy') as publish, \
                patch.object(deploy.sys, 'platform', 'linux'):
            deploy.install_updates(config_file, home=self.home)
        publish.assert_not_called()
        self.assertEqual(old.read_bytes(), b'old program')
        self.assertEqual((self.root / 'deploy.py').read_bytes(), Path(deploy.__file__).read_bytes())
        self.assertEqual(json.loads(config_file.read_text())['repository'], str(deploy.REPO))
        self.assertEqual(run.call_count, 2)
        self.assertTrue(json.loads((data / 'update.json').read_text())['enabled'])


if __name__ == '__main__':
    unittest.main()
