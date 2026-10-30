"""Typed games carry frozen center assets to the chosen execution computer."""
import hashlib
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlparse, parse_qs

import test_execution_node_role as role

HELPER = r'''
import hashlib,json,pathlib,sys
args=sys.argv[1:]
if args[:2]==['game-deploy','capabilities']:
    print(json.dumps({'schema':'phonefarm/game-deploy-capabilities@1','resources_v1':True,'device_binding_v1':True,'doom_render_scene_v1':True})); sys.exit()
if args[:1]==['lease']:
    print(json.dumps({'schema_version':1,'status':'clear','entries':[]})); sys.exit()
if args[:1]!=['game-deploy']: sys.exit(2)
out=pathlib.Path(args[args.index('--out')+1])
recovering=args[:2]==['game-deploy','recover']
if recovering:
    saved=json.loads((pathlib.Path(args[args.index('--session')+1])/'session.json').read_text())
    request=saved['request']; binding=saved['binding']
else:
    request=json.loads(pathlib.Path(args[args.index('--request')+1]).read_text())
    binding=json.loads(args[args.index('--device-binding')+1]) if '--device-binding' in args else None
    for asset in request['assets']:
        path=pathlib.Path(asset['path'])
        assert path.is_relative_to(pathlib.Path(__file__).parent/'_games/cache'), path
        data=path.read_bytes()
        assert len(data)==asset['size'] and hashlib.sha256(data).hexdigest()==asset['sha256']
    if 'deployment' in request:
        sources={key:pathlib.Path(path).read_text() for key,path in request['deployment'].items()}
        (out/'observed-inputs.json').write_text(json.dumps(sources))
session=out/'game-deploy'; session.mkdir()
(session/'session.json').write_text(json.dumps({'request':request,'binding':binding}))
(out/'observed.json').write_text(json.dumps(request))
report={k:request[k] for k in ('node_id','game_id','version','serial','mode')}
report_schema='phonefarm/game-scene@1' if 'deployment' in request else 'phonefarm/game-deployment@1'
report.update(schema=report_schema,lease_scope='device' if binding else 'global',device_binding=binding,session_path='game-deploy',
              phase='restored' if recovering else 'recovery_required',
              recovery={'status':'reported_complete' if recovering else 'required'})
for key in ('installed','resources_deployed','application_access','login','scene_ready'):
    report[key]={'status':'unknown'}
if 'deployment' in request:
    for key in ('input','launcher_configuration','runtime_seed','launch','scene_observation','stop'):
        report[key]={'status':'unknown'}
(out/'deployment.json').write_text(json.dumps(report))
sys.exit(0 if recovering else 2)
'''


class MultiNodeGameTests(unittest.TestCase):
    def setUp(self):
        class Executor(role.ExecutionNodeRoleTests):
            def execution_bins(self):
                helper = self.root / 'fake-pf'
                helper.write_text('#!' + sys.executable + '\n' + HELPER)
                helper.chmod(0o700)
                return {**super().execution_bins(), 'phonefarm': (str(helper), str(self.root))}
        self.flow = Executor()
        self.flow.setUp()
        self.addCleanup(self.flow.doCleanups)
        self.center, self.worker = self.flow.center, self.flow.jobs
        self.addCleanup(self.center.jobs.center.close)
        leases = patch.object(self.worker.leases, 'snapshot', return_value={'status':'clear','entries':[]})
        leases.start()
        self.addCleanup(leases.stop)
        self.manifest = self.center.manifest(deployment={
            'schema':'phonefarm/game-resources-recipe@1','package':'test.game','version_code':1,
            'apk_sha256':'a'*64,'files':[{'asset_name':'data','path':'base/data.pk4'}]})
        self.data = b'fixture' * (1 << 18)
        asset = self.manifest['assets'][0]
        asset.update(size=len(self.data), sha256=hashlib.sha256(self.data).hexdigest())
        self.assertEqual(self.center.call('POST','/games',self.manifest)[0],201)
        self.center.jobs.games.cache.import_file(asset, self.source())
        self.ref = self.worker.node_id + '/usb-game'
        self.body = {'node_id':self.center.jobs.node_id,'serial':self.ref,'mode':'apply'}
        self.path = '/games/demo/versions/v1/deploy'

    def source(self):
        source = self.flow.root / 'fixture-asset'
        source.write_bytes(self.data)
        stream = source.open('rb')
        self.addCleanup(stream.close)
        return stream

    def completed(self, action, executor=None):
        done = threading.Event()
        executor = executor or self.worker
        commit = executor.dispatch._commit_completion
        def committed(*args, **kwargs):
            value = commit(*args, **kwargs); done.set(); return value
        with patch.object(executor.dispatch, '_commit_completion', side_effect=committed):
            code, job = action()
            self.assertEqual(code, 200, job)
            self.assertTrue(done.wait(10), job)
        job = self.center.call('GET', '/jobs/' + job['id'])[1]
        self.assertEqual((job['state'],job['queue']['state']),('finished','done'),job)
        return job

    def test_frozen_assets_execute_and_recover_without_a_worker_game_catalog(self):
        job = self.completed(lambda:self.center.call('POST',self.path,self.body,headers={'X-Request-ID':'game-on-worker'}))
        jid = job['id']
        self.assertEqual((job['execution_node_id'],job['exit']),(self.worker.node_id,2))
        original_bytes = Path(self.worker.path(jid,'in','manifest.json')).read_bytes()
        frozen = json.loads(original_bytes)
        actual = json.loads(Path(self.worker.path(jid,'out','observed.json')).read_text())
        self.assertEqual(frozen['assets'][0]['path'],self.center.jobs.games.cache.object_path(self.manifest['assets'][0]['sha256']))
        self.assertNotEqual(frozen['assets'][0]['path'],actual['assets'][0]['path'])
        self.assertEqual(actual['node_id'],self.center.jobs.node_id)
        code, detail = self.center.call('GET','/jobs/'+jid+'/deployment')
        self.assertEqual((code,detail['target']['execution_node_id'],detail['target']['serial']),
                         (200,self.worker.node_id,'usb-game'),detail)
        self.assertTrue(detail['recovery']['eligible'],detail)
        self.assertIn(jid,[r['id'] for r in self.center.call('GET','/games/demo/versions/v1')[1]['deployments']])
        # Retrying this request neither reselects the phone nor prepares missing assets again.
        Path(self.center.jobs.games.cache.object_path(self.manifest['assets'][0]['sha256'])).unlink()
        repeated = self.center.call('POST',self.path,self.body,headers={'X-Request-ID':'game-on-worker'})
        self.assertEqual((repeated[0],repeated[1]['id']),(200,jid))
        recovered = self.completed(lambda:self.center.call('POST','/jobs/'+jid+'/deployment/recover',{},
                                                          headers={'X-Request-ID':'recover-worker-game'}))
        self.assertEqual((recovered['execution_node_id'],recovered['exit']),(self.worker.node_id,0))
        self.assertEqual(Path(self.worker.path(jid,'in','manifest.json')).read_bytes(),original_bytes)
        self.assertEqual(len(self.worker.dispatch.queue.rows()),2)
        self.assertFalse((self.flow.root/'_games/catalog').exists())

    def test_cli_routes_to_worker_capabilities_when_the_center_has_no_phonefarm(self):
        code, record = self.center.call('GET', '/games/demo/versions/v1')
        self.assertEqual(code, 200, record)
        self.assertTrue(record['deployment_support']['available'], record['deployment_support'])
        def submit():
            result = self.center.cli('deploy','demo','v1','--serial',self.ref,'--request-id','cli-worker-game')
            self.assertEqual(result.returncode,0,result.stderr)
            return 200,json.loads(result.stdout)
        job = self.completed(submit)
        self.assertEqual(job['execution_node_id'],self.worker.node_id)

    def test_plain_capability_job_does_not_require_a_game_manifest(self):
        job = self.completed(lambda: self.center.call('POST', '/jobs', headers={
            'X-Request-ID': 'plain-game-capabilities', 'X-Job': json.dumps({
                'bin': 'phonefarm', 'argv': ['game-deploy', 'capabilities', '--json'],
                'device_access': 'none', 'execution_node_id': self.worker.node_id})}))
        self.assertEqual(job['exit'], 0, job)

    def test_missing_worker_game_capability_finishes_preparation_and_releases_resources(self):
        self.worker.deployments.support.update(available=False, error='fixture PF capability unavailable')
        code, job = self.center.call('POST', self.path, self.body, headers={'X-Request-ID': 'unsupported-game'})
        self.assertEqual(code, 200, job)
        self.assertEqual((job['state'], job['queue']['state'], job['exit']), ('finished', 'done', None), job)
        self.assertIn('fixture PF capability unavailable', job['note'])

    def test_platform_reference_for_a_local_unregistered_phone_keeps_game_history(self):
        from node_deployments import NodeDeployments
        helper = self.center.root / 'fake-pf'
        helper.write_text('#!' + sys.executable + '\n' + HELPER)
        helper.chmod(0o700)
        self.center.jobs.bins['phonefarm'] = (str(helper), str(self.center.root))
        self.center.jobs.deployments = NodeDeployments(self.center.jobs, self.center.jobs.games)
        done = threading.Event()
        commit = self.center.jobs.dispatch._commit_completion
        def committed(*args, **kwargs):
            value = commit(*args, **kwargs)
            done.set()
            return value
        with patch.object(self.center.jobs.dispatch, '_commit_completion', side_effect=committed), \
             patch.object(self.center.jobs.leases, 'snapshot', return_value={'status': 'clear', 'entries': []}):
            code, job = self.center.call('POST', self.path,
                {**self.body, 'serial': self.center.jobs.node_id + '/local-game'},
                headers={'X-Request-ID': 'local-platform-game'})
            self.assertEqual(code, 200, job)
            self.assertTrue(done.wait(10), job)
        history = self.center.call('GET', '/games/demo/versions/v1')[1]['deployments']
        self.assertIn(job['id'], [item['id'] for item in history])

    def test_registered_platform_phone_keeps_its_single_device_binding(self):
        scopes={'schema':'phonefarm/device-scopes@1','registry_revision':1,'unknown':False,'queue_waiting':False,
                'global':{'status':'clear','error':None},
                'devices':[{'device_id':'phone-a','aliases':['usb-game'],'status':'clear','error':None}]}
        with patch.object(self.worker.leases,'snapshot',return_value={'status':'clear','entries':[],'device_scopes':scopes}):
            self.center.jobs.directory.report(self.worker.node_id,self.flow.connector.backend.snapshot())
            body={**self.body,'serial':self.worker.node_id+'/phone-a'}
            job=self.completed(lambda:self.center.call('POST',self.path,body,headers={'X-Request-ID':'scoped-game'}))
        expected={'schema':'phonefarm/device-binding@1','registry_revision':1,'device_id':'phone-a','serial':'usb-game'}
        self.assertEqual(job.get('device_binding'),expected)
        report=self.center.call('GET','/jobs/'+job['id']+'/deployment')[1]['report']
        self.assertEqual((report['device_binding'],report['lease_scope']),(expected,'device'))
        with patch.object(self.worker.leases,'snapshot',return_value={'status':'clear','entries':[],'device_scopes':scopes}):
            recovered = self.completed(lambda: self.center.call('POST', '/jobs/' + job['id'] + '/deployment/recover', {},
                                                                headers={'X-Request-ID': 'recover-scoped-game'}))
        self.assertEqual((recovered['device_binding'], recovered['exit']), (expected, 0))

    def test_machine_asset_reads_are_assigned_and_missing_bytes_return_an_error(self):
        import node_connector
        job = self.completed(lambda: self.center.call('POST', self.path, self.body,
                                                      headers={'X-Request-ID': 'asset-access'}))
        sha = self.manifest['assets'][0]['sha256']
        path = '/node-connect/assets/' + job['id'] + '/' + sha + '?offset=0&length=7'
        headers = {'Authorization': 'Bearer ' + self.flow.config['token']}
        response = node_connector.exchange(self.center.url, 'GET', path, headers=headers)
        self.assertEqual((response['status'], response['body']), (200, self.data[:7]))
        invite = self.center.jobs.directory.invite()
        stranger = self.center.jobs.directory.join({'invite': invite['code'], 'name': 'another', 'node_id': 'f' * 32})
        response = node_connector.exchange(self.center.url, 'GET', path,
                                           headers={'Authorization': 'Bearer ' + stranger['token']})
        self.assertEqual(response['status'], 404)
        Path(self.center.jobs.games.cache.object_path(sha)).unlink()
        response = node_connector.exchange(self.center.url, 'GET', path, headers=headers)
        self.assertEqual(response['status'], 404)

    def test_partial_asset_stays_in_the_same_cache_and_a_new_request_resumes_it(self):
        from game_assets import CHUNK
        import node_connector
        original = node_connector.exchange
        offsets = []
        def exchange(url, method, path, *args, **kwargs):
            if path.startswith('/node-connect/assets/'):
                offset = int(parse_qs(urlparse(path).query)['offset'][0]); offsets.append(offset)
                if offset == CHUNK:
                    return {'status':503,'headers':{},'body':b'{"error":"fixture interruption"}'}
            return original(url,method,path,*args,**kwargs)
        with patch.object(node_connector,'exchange',side_effect=exchange):
            code, job = self.center.call('POST',self.path,self.body,headers={'X-Request-ID':'partial-game'})
            self.assertEqual(code,200,job)
            self.assertEqual(job['queue']['state'],'done',job)
            jid = job['id']
            self.assertFalse(Path(self.worker.path(jid,'launch.json')).exists())
            self.assertEqual(offsets,[0,CHUNK])
            again = self.center.call('POST',self.path,self.body,headers={'X-Request-ID':'partial-game'})
            self.assertEqual(again[1]['id'],jid)
            self.assertEqual(offsets,[0,CHUNK])
        offsets.clear()
        def resumed(url, method, path, *args, **kwargs):
            if path.startswith('/node-connect/assets/'):
                offsets.append(int(parse_qs(urlparse(path).query)['offset'][0]))
            return original(url,method,path,*args,**kwargs)
        with patch.object(node_connector,'exchange',side_effect=resumed):
            self.completed(lambda:self.center.call('POST',self.path,self.body,headers={'X-Request-ID':'resume-game'}))
        self.assertEqual(offsets,[CHUNK])

    def test_early_transfer_eof_keeps_partial_bytes_without_repeating_the_read(self):
        from game_assets import AssetCache, AssetError
        cache = AssetCache(str(self.flow.root / 'interrupted-cache'))
        asset = self.manifest['assets'][0]
        offsets = []
        def read_at(offset, length):
            offsets.append(offset)
            if len(offsets) > 2:
                raise AssertionError('an empty transfer was read again')
            return self.data[:3] if offset == 0 else b''
        with self.assertRaisesRegex(AssetError, 'ended before the declared size'):
            cache.receive(asset, read_at)
        self.assertEqual(offsets, [0, 3])
        partial = Path(cache.root) / 'partial' / (asset['sha256'] + '.part')
        self.assertEqual(partial.read_bytes(), self.data[:3])
        self.assertFalse(Path(cache.object_path(asset['sha256'])).exists())

    def test_scene_keeps_center_input_references_and_materializes_them_on_the_worker(self):
        self.center.jobs.bins['fixture'] = (sys.executable, str(self.center.root))
        originals = {'deployment.json': '{"source":"center-report"}',
                     'game-deploy/session.json': '{"source":"center-session"}'}
        program = ("import os,pathlib; out=pathlib.Path(os.environ['PF_JOB_OUT']); (out/'game-deploy').mkdir(); "
                   + '; '.join(f'(out/{path!r}).write_text({value!r})' for path, value in originals.items()))
        source = self.completed(lambda: self.center.call('POST', '/jobs', headers={
            'X-Request-ID': 'scene-input-source', 'X-Job': json.dumps({
                'bin': 'fixture', 'argv': ['-c', program], 'device_access': 'none'})}), executor=self.center.jobs)
        sources = {name: self.center.call('GET', '/jobs/' + source['id'] + '/input-reference?path=' + path)[1]['source']
                   for name, path in [('report', 'deployment.json'), ('state', 'game-deploy/session.json')]}
        assets = [{'name': name, 'role': 'resource', 'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data),
                   'source': {'kind': 'unavailable', 'reason': 'fixture seed'}}
                  for name, data in [('demo', b'demo'), ('compat', b'compat')]]
        recipe = {'schema': 'phonefarm/game-scene-recipe@1', 'adapter': 'doom3-v73-render-demo-v1',
                  'package': 'com.karin.idTech4Amm', 'version_code': 11073, 'apk_sha256': 'a' * 64,
                  'resource_prefix': 'doom3', 'demo_name': 'fixture', 'demo_asset_name': 'demo',
                  'compat_asset_name': 'compat', 'renderer': {'opengl': 'GLES3.0', 'multithread': True}, 'wait_seconds': 1}
        self.assertEqual(self.center.call('POST', '/games', self.center.manifest(
            version='v2', assets=assets, scene_attempt=recipe))[0], 201)
        for asset in assets:
            source_file = self.flow.root / asset['name']
            source_file.write_bytes(asset['name'].encode())
            with source_file.open('rb') as stream:
                self.center.jobs.games.cache.import_file(asset, stream)
        job = self.completed(lambda: self.center.call('POST', '/games/demo/versions/v2/scene',
            {**self.body, 'deployment': sources}, headers={'X-Request-ID': 'scene-on-worker'}))
        self.assertEqual(job['exit'], 2, job)
        declared = self.worker.dispatch.queue.get(job['id'])['spec']['inputs']
        self.assertEqual(declared['node_id'], self.center.jobs.node_id)
        self.assertEqual({item['name']: item['source'] for item in declared['files']}, sources)
        observed = json.loads(Path(self.worker.path(job['id'], 'out', 'observed-inputs.json')).read_text())
        self.assertEqual(observed, dict(zip(('report_path', 'state_path'), originals.values())))
        self.assertEqual(self.center.call('GET', '/jobs/' + job['id'] + '/deployment')[0], 200)

    def test_remote_game_history_and_assistance_use_collected_originals_offline(self):
        job = self.completed(lambda:self.center.call('POST',self.path,self.body,headers={'X-Request-ID':'history-game'}))
        for kind, oid, data in [('research','study',{'title':'Study','objective':'Inspect remote game'}),
                               ('direction','approach',{'research_id':'study','title':'Approach','hypothesis':'Check prerequisites'})]:
            code,value=self.center.call('POST','/research-state/'+kind+'/'+oid,{
                'schema':'phonefarm/research-write@1','operation_id':'create-'+oid,'base_revision':None,
                'data':data,'actor_label':None,'reason':None},headers={'X-Expected-Node-ID':self.center.jobs.node_id})
            self.assertEqual(code,201,value)
        with self.center.jobs.center.artifacts.open_output(job['id'],'deployment.json') as stream:
            saved = json.load(stream)
        self.center.jobs.center.artifacts.collect(job['id'])
        self.assertEqual(self.center.jobs.center.artifacts.sync_status(job['id'])['status'],'complete')
        self.center.jobs.center.artifacts.close()
        self.flow.connector.close()
        with patch.object(self.center.jobs.center,'rpc',side_effect=AssertionError('history must use collected originals')):
            code, history = self.center.call('GET','/games/demo/versions/v1')
            self.assertEqual(code,200,history)
            self.assertEqual(history['deployments'][0]['report'],saved)
            code, draft = self.center.call('GET','/jobs/'+job['id']+'/deployment/assistance-draft?research_id=study&direction_id=approach',
                                          headers={'X-Expected-Node-ID':self.center.jobs.node_id})
            self.assertEqual(code,200,draft)
            self.assertEqual(draft['node_id'],self.center.jobs.node_id)
            self.assertEqual(draft['data']['evidence_refs'],[{'kind':'job','node_id':self.center.jobs.node_id,'job_id':job['id']}])


if __name__=='__main__': unittest.main()
