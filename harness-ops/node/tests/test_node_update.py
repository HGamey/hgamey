"""Isolated code publication; no production node, device, download or large build."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

NODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NODE))
import node_update


@unittest.skipUnless(Path('/bin/zsh').exists() and shutil.which('git') and shutil.which('rsync'), 'zsh/git/rsync required')
class NodeUpdateTests(unittest.TestCase):
    def env(self, home, tools=None):
        return {'HOME': str(home), 'PATH': str(tools or NODE) + os.pathsep + os.environ['PATH'],
                'HG_NODE_PYTHON': sys.executable, 'LANG': 'C.UTF-8',
                'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_TERMINAL_PROMPT': '0'}

    def cli(self, env, *args):
        return subprocess.run(['/bin/zsh', str(NODE / 'update_node.sh'), *map(str, args)],
                              env=env, text=True, capture_output=True, timeout=20)

    def test_git_mode_is_retired_in_favour_of_deploy_py(self):
        # HGamey is one repository now; per-repository --git staging would pull the frozen old repos.
        with tempfile.TemporaryDirectory(prefix='node-update-git-') as temporary:
            base = Path(temporary)
            home, install = base / 'home', base / 'install'
            home.mkdir()
            (install / 'harness-ops').mkdir(parents=True)
            result = self.cli(self.env(home), '--install-root', install, '--offline', '--git', 'harness-ops@origin/main')
            self.assertEqual(result.returncode, 2)
            self.assertIn('deploy.py', result.stderr)
            self.assertEqual(result.stdout, '')
            self.assertFalse((install / '.updates').exists())

    def test_upload_publication_and_explicit_rollback_preserve_unmanaged_data(self):
        with tempfile.TemporaryDirectory(prefix='node-update-upload-') as temporary:
            base = Path(temporary)
            home, install, upload = base / 'home', base / 'install with spaces', base / 'upload'
            home.mkdir()
            (upload / 'harness-ops').mkdir(parents=True)
            (upload / 'harness-ops' / 'module.py').write_text('VERSION = "new"\n')
            env = self.env(home)
            target = install / 'harness-ops'
            target.mkdir(parents=True)
            old = target / 'module.py'
            old.write_text('VERSION = "old"\n')
            (target / 'runtime-build.json').write_text('{"old":true}\n')
            data = target / 'runs' / 'evidence.bin'
            data.parent.mkdir()
            data.write_bytes(b'original evidence')
            before = (data.read_bytes(), data.stat().st_mtime_ns)
            result = self.cli(env, '--install-root', install, '--offline', upload)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report['status'], 'succeeded')
            self.assertEqual(old.read_text(), 'VERSION = "new"\n', result.stderr)
            # An undeclared upload must not keep the old build identity.
            self.assertEqual((target / 'runtime-build.json').read_text(), 'null\n')
            receipt = json.loads(Path(report['receipt']).read_text())
            self.assertEqual(receipt['guard']['kind'], 'administrator_asserted_offline')
            self.assertFalse(receipt['guard']['all_participants_verified'])
            self.assertEqual((data.read_bytes(), data.stat().st_mtime_ns), before)
            self.assertFalse((home / 'node').exists())
            # A later edit is not silently destroyed by a rollback request.
            old.write_text('later administrator edit\n')
            refused = self.cli(env, '--install-root', install, '--offline', '--rollback', report['run_id'])
            self.assertEqual(refused.returncode, 2)
            self.assertEqual(json.loads(refused.stdout)['status'], 'rollback_required')
            self.assertEqual(old.read_text(), 'later administrator edit\n')
            old.write_text('VERSION = "new"\n')
            restored = self.cli(env, '--install-root', install, '--offline', '--rollback', report['run_id'])
            self.assertEqual(restored.returncode, 0, restored.stderr)
            self.assertEqual(json.loads(restored.stdout)['status'], 'restored')
            self.assertEqual(old.read_text(), 'VERSION = "old"\n')
            self.assertEqual((target / 'runtime-build.json').read_text(), '{"old":true}\n')
            self.assertEqual((data.read_bytes(), data.stat().st_mtime_ns), before)

    def test_signature_guard_and_interrupted_publication_keep_recovery_receipt(self):
        with tempfile.TemporaryDirectory(prefix='node-update-fault-') as temporary:
            base = Path(temporary)
            home, install, upload, tools = [base / p for p in ('home', 'install', 'upload', 'tools')]
            home.mkdir()
            tools.mkdir()
            (upload / 'phonefarm').mkdir(parents=True)
            (install / 'phonefarm').mkdir(parents=True)
            first, second = install / 'phonefarm' / 'a', install / 'phonefarm' / 'b'
            first.write_bytes(b'old-a')
            second.write_bytes(b'old-b')
            (upload / 'phonefarm' / 'a').write_bytes(bytes.fromhex('feedfacf') + b'synthetic Mach-O; never executed')
            (upload / 'phonefarm' / 'b').write_bytes(b'new-b')
            calls = base / 'sign-calls'
            signer = tools / 'codesign'
            signer.write_text('#!' + sys.executable + '\nimport os,sys\n'
                'with open(os.environ["SIGN_CALLS"],"a") as f: f.write(sys.argv[1]+"\\n")\n'
                'sys.exit(7 if "--verify" in sys.argv and os.environ.get("FAIL_VERIFY")=="1" else 0)\n')
            signer.chmod(0o755)
            env = dict(self.env(home, tools), SIGN_CALLS=str(calls), FAIL_VERIFY='1')
            failed = self.cli(env, '--install-root', install, '--offline', upload)
            self.assertEqual(failed.returncode, 2)
            self.assertEqual(json.loads(failed.stdout)['status'], 'failed')
            self.assertEqual(calls.read_text().splitlines(), ['--force', '--verify'])
            self.assertEqual(first.read_bytes(), b'old-a')
            env['FAIL_VERIFY'] = '0'
            jobs = base / 'jobs'
            jobs.mkdir()
            (jobs / '.intake.lock').touch()
            db = sqlite3.connect(jobs / '.queue.sqlite3')
            db.execute('CREATE TABLE jobs(job_id TEXT,state TEXT,spec TEXT)')
            db.execute('INSERT INTO jobs VALUES (?,?,?)', ('a' * 12, 'claimed', '{"bin":"other"}'))
            db.commit()
            busy = self.cli(env, '--install-root', install, '--jobs-root', jobs, upload)
            self.assertEqual(busy.returncode, 2)
            self.assertIn('other active or blocked jobs', busy.stderr)
            self.assertEqual(first.read_bytes(), b'old-a')
            db.execute('UPDATE jobs SET state="queued"')
            db.execute('INSERT INTO jobs VALUES (?,?,?)', ('b' * 12, 'claimed', '{"bin":"update_node"}'))
            db.commit()
            db.close()
            layout = {'phonefarm': str(install / 'phonefarm'), 'game_opt_loop': str(install / 'game_opt_loop'),
                      'harness_ops': str(install / 'harness-ops')}
            layout['state_dir'] = str(node_update.state_directory(layout))
            env.update(HG_UPDATE_LAYOUT=json.dumps(layout), HG_UPDATE_JOBS_ROOT=str(jobs), PF_JOB_ID='b' * 12)
            real_copy = node_update.copy_file
            real_sync = node_update.sync_dir
            synced, at_first_publication = [], []

            def observed_sync(path):
                real_sync(path)
                synced.append(Path(path).resolve())

            def publication_fault(source, destination, **kw):
                if destination.resolve() == first.resolve() and 'backups' not in source.parts:
                    at_first_publication.extend(synced)
                if destination.resolve() == second.resolve() or (destination.resolve() == first.resolve() and 'backups' in source.parts):
                    raise OSError('synthetic publish/rollback I/O failure')
                return real_copy(source, destination, **kw)

            stdout, stderr = io.StringIO(), io.StringIO()
            # The production module and real file replacement are used; only
            # this run's second publish and first rollback copy fail.
            with patch.dict(os.environ, env, clear=True), patch.object(node_update, 'copy_file', side_effect=publication_fault), \
                    patch.object(node_update, 'sync_dir', side_effect=observed_sync), \
                    redirect_stdout(stdout), redirect_stderr(stderr):
                code = node_update.main([str(upload)])
            self.assertEqual(code, 2, stderr.getvalue())
            report = json.loads(stdout.getvalue())
            self.assertEqual(report['status'], 'rollback_required')
            run = Path(report['receipt']).parent
            for ancestor in (run, run.parent, run.parent.parent, run / 'backups'):
                self.assertIn(ancestor.resolve(), at_first_publication)
            ordinary = node_update.maintenance_problem(layout, {'bin': 'phonefarm', 'argv': []})
            self.assertEqual(ordinary['kind'], 'node_update')
            self.assertEqual(ordinary['blocking_runs'], [report['run_id']])
            rollback = {'bin': 'update_node', 'argv': ['--rollback', report['run_id']]}
            self.assertIsNone(node_update.maintenance_problem(layout, rollback))
            self.assertIsNotNone(node_update.maintenance_problem(layout, dict(rollback, exe='/tmp/other')))
            self.assertNotEqual(first.read_bytes(), b'old-a')
            self.assertEqual(second.read_bytes(), b'old-b')
            stopped = self.cli(env, upload)
            self.assertEqual(stopped.returncode, 2)
            self.assertIn('unfinished publication requires explicit rollback', stopped.stderr)
            recovered = self.cli(env, '--rollback', report['run_id'])
            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            self.assertEqual(first.read_bytes(), b'old-a')
            self.assertEqual(second.read_bytes(), b'old-b')
            receipt = json.loads(Path(report['receipt']).read_text())
            self.assertEqual(receipt['status'], 'restored')
            self.assertIsNone(node_update.maintenance_problem(layout, {'bin': 'phonefarm'}))
            self.assertFalse((Path(layout['state_dir']) / 'maintenance.json').exists())
            # A malformed old receipt is not silently treated as completed.
            receipt_path = Path(report['receipt'])
            original = receipt_path.read_bytes()
            receipt_path.write_text('[]')
            with self.assertRaises(node_update.UpdateError):
                node_update.unresolved(Path(layout['state_dir']))
            receipt_path.write_bytes(original)
            self.assertEqual(receipt['rollback_guard']['job_id'], 'b' * 12)
            self.assertTrue(receipt['rollback_guard']['managed_jobs_checked'])
            self.assertFalse(receipt['rollback_guard']['all_participants_verified'])


if __name__ == '__main__':
    unittest.main()
