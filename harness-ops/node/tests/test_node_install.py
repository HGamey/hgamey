"""Enroll a private installation and upgrade while its independent worker holds an fd."""
import contextlib
import fcntl
import io
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
import unittest
from unittest.mock import patch

import test_node_games as fixture

NODE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(NODE.parents[1]))
import deploy

PROGRAM=r'''
import fcntl,json,os,pathlib,socket,sys
args=sys.argv[1:]
if args[:1]==['runtime-info']:
    print(json.dumps({'schema':'phonefarm/runtime-identity@1','program':PROGRAM,'package_version':'fixture',
        'build':{'source_revision':REVISION,'source_dirty':False,'target':'fixture','profile':'test'},'supported_protocols':[]}));sys.exit()
if args[:1]==['lease']:
    print(json.dumps({'schema_version':1,'status':'clear','entries':[]}));sys.exit()
if args[:1]==['fixture-hold']:
    fd=os.open(args[2],os.O_RDWR|os.O_CREAT,0o600);fcntl.flock(fd,fcntl.LOCK_EX)
    sock=socket.create_connection(('127.0.0.1',int(args[1])))
    sock.sendall(b'ready');sock.recv(1);os.close(fd);sock.sendall(b'released');sys.exit()
print('{}');sys.exit(2)
'''


class NodeInstallTests(unittest.TestCase):
    program = PROGRAM

    def check_phone_lock(self, held):
        """The Cargo integration also probes a real PF device lock here."""

    def setUp(self):
        self.center=fixture.GameFlowTests();self.center.setUp()
        self.addCleanup(self.center.tearDown)
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.base=Path(self.temporary.name)
        self.root=self.base/'installed node'
        self.home=self.base/'service files only'
        self.process=None
        platform = patch.object(deploy.sys, 'platform', 'linux')
        platform.start()
        self.addCleanup(platform.stop)
        self.addCleanup(self.stop)
        self.addCleanup(self.center.jobs.center.close)
        self.output=io.StringIO()
        self.launch_log=(self.base/'launcher.log').open('wb')
        self.addCleanup(self.launch_log.close)
        # CI checks out only HEAD. Make the second release in a private git store
        # instead of depending on repository history or fetching over the network.
        checkout = self.base / 'release-source'
        subprocess.run(['git', 'clone', '--quiet', '--shared', '--no-checkout', str(deploy.REPO), str(checkout)],
                       check=True, capture_output=True)
        repository = patch.object(deploy, 'REPO', checkout)
        repository.start()
        self.addCleanup(repository.stop)
        self.revision = deploy.git('rev-parse', 'HEAD')
        self.other = deploy.git('-c', 'user.name=Installation fixture', '-c', 'user.email=fixture@example.invalid',
                                'commit-tree', 'HEAD^{tree}', '-p', 'HEAD', '-m', 'Second private installation release')
        with socket.socket() as listener:
            listener.bind(('127.0.0.1',0));self.port=listener.getsockname()[1]
        extract=deploy.extract
        def extracted(sha,destination):
            extract(sha,destination)
            # Exercise this checkout's Python implementation before its commit exists.
            for source in NODE.glob('*.py'):
                shutil.copy2(source,destination/'harness-ops/node'/source.name)
        def built(sha,source,work,config):
            target=work/'builds'/sha;target.mkdir(parents=True,exist_ok=True)
            for _,_,name in deploy.PROGRAMS:
                program=target/name
                program.write_text('#!'+sys.executable+'\nPROGRAM='+repr(name)+'\nREVISION='+repr(sha)+'\n'+self.program)
                program.chmod(0o700)
            return target
        for name,value in [('extract',extracted),('build',built),('run',self.service_command)]:
            p=patch.object(deploy,name,side_effect=value);p.start();self.addCleanup(p.stop)

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate();self.process.wait(timeout=10)

    def service_command(self,argv,**kwargs):
        self.assertEqual(argv[0],'systemctl',argv)
        if 'stop' in argv:
            self.stop()
        elif 'enable' in argv:
            self.process=subprocess.Popen([sys.executable,str(self.root/'harness-ops/node/node_launch.py'),
                '--config',str(self.root/'node-launch.json'),'--workdir',str(self.root),'--no-tunnel'],
                env={**os.environ,'DEVLOCK_DIR':str(self.root/'locks'),'HG_CONFIG_DIR':str(self.root/'config')},
                stdout=self.launch_log,stderr=subprocess.STDOUT)
        else:
            self.assertIn('daemon-reload',argv)

    def test_join_restart_and_upgrade_preserve_identity_and_a_live_lock_fd(self):
        invitation=self.center.jobs.directory.invite()
        with contextlib.redirect_stdout(self.output):
            result=deploy.join_node(self.center.url,invitation['code'],'installed',self.root,self.port,
                                    self.revision,False,home=self.home)
        self.assertEqual(result,0,self.output.getvalue())
        config=deploy.load_config(None,self.root/'deploy-node.json')
        connection=(self.root/'data/_node/center.json').read_bytes()
        node_id=(self.root/'data/_node/id').read_text().strip()
        self.assertEqual((self.root/'data/_node/center.json').stat().st_mode&0o777,0o600)
        self.assertEqual((self.root/'data/_node/admin.token').stat().st_mode&0o777,0o600)
        self.assertFalse((self.root/'data/_research').exists())
        listener=socket.socket();self.addCleanup(listener.close)
        listener.bind(('127.0.0.1',0));listener.listen();listener.settimeout(15)
        lock=self.root/'locks'/'held-by-worker';lock.parent.mkdir(exist_ok=True)
        spec={'bin':'phonefarm','argv':['fixture-hold',str(listener.getsockname()[1]),str(lock)],
              'device_access':'none','execution_node_id':node_id}
        done=threading.Event();update=self.center.jobs.center.targets.update
        def updated(jid,**fields):
            value=update(jid,**fields)
            if (fields.get('observation') or {}).get('queue',{}).get('state')=='done':done.set()
            return value
        with patch.object(self.center.jobs.center.targets,'update',side_effect=updated):
            code,job=self.center.call('POST','/jobs',headers={'X-Job':json.dumps(spec),'X-Request-ID':'hold-across-install'})
            self.assertEqual(code,200,job)
            child,_=listener.accept();self.addCleanup(child.close)
            self.assertEqual(child.recv(5),b'ready')
            self.check_phone_lock(True)
            with lock.open('rb') as probe:
                with self.assertRaises(BlockingIOError):fcntl.flock(probe,fcntl.LOCK_EX|fcntl.LOCK_NB)
                before=lock.stat().st_ino
                with contextlib.redirect_stdout(self.output):
                    self.assertEqual(deploy.deploy('installed',config,self.other,False,False),0)
                self.assertEqual(lock.stat().st_ino,before)
                with self.assertRaises(BlockingIOError):fcntl.flock(probe,fcntl.LOCK_EX|fcntl.LOCK_NB)
                self.check_phone_lock(True)
                self.assertEqual((self.root/'data/_node/center.json').read_bytes(),connection)
                self.assertEqual((self.root/'data/_node/id').read_text().strip(),node_id)
                child.sendall(b'x');self.assertEqual(child.recv(8),b'released')
                fcntl.flock(probe,fcntl.LOCK_EX|fcntl.LOCK_NB)
            # This is a separate installed process, using the real 30-second report interval.
            self.assertTrue(done.wait(45),'reconnected executor did not report completion')
        self.check_phone_lock(False)
        self.assertEqual(len(self.center.jobs.directory.list()),1)
        self.assertEqual(self.center.call('GET','/jobs/'+job['id'])[1]['queue']['state'],'done')
        self.assertEqual(deploy.reports(config,0)[0]['service'],self.other)
        self.assertNotIn(json.loads(connection)['token'],self.output.getvalue())
        self.assertIn('KillMode=process',next((self.home/'.config/systemd/user').glob('*.service')).read_text())

    def test_macos_service_has_one_outbound_executor_and_the_same_fixed_paths(self):
        env={'DEVLOCK_DIR':str(self.root/'locks'),'HG_CONFIG_DIR':str(self.root/'config'),'PATH':'/usr/bin:/bin'}
        plan=deploy.service_plan(self.root,'a'*32,'darwin',self.home,env)
        value=plistlib.loads(plan['content'])
        self.assertEqual(value['EnvironmentVariables'],env)
        self.assertEqual(value['WorkingDirectory'],str(self.root))
        self.assertIn('--no-tunnel',value['ProgramArguments'])
        self.assertIn(str(self.root/'node-launch.json'),value['ProgramArguments'])
        self.assertNotIn('token',str(value).lower())
        self.assertEqual(plan['start'][0][:2],['launchctl','bootstrap'])
        self.assertEqual(plan['stop'][0][:2],['launchctl','bootout'])


if __name__=='__main__':unittest.main()
