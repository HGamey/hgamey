import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.request import Request,urlopen
from urllib.error import HTTPError
import catalog
import server


def put(root,path,value,jsonl=False):
    p=root/path;p.parent.mkdir(parents=True,exist_ok=True)
    raw=b''.join(catalog.encode(v).replace(b'\n',b'')+b'\n' for v in value) if jsonl else catalog.encode(value)
    p.write_bytes(raw);return p


def fixture(root):
    put(root,'arbitrary-task/runs/run-a/log.jsonl',[
        {'r':'start','ts':1790187316518,'pid':123,'serial':'serial-private-sentinel'},
        {'r':'act','n':1,'a':'tap','x':42,'y':87,'text':'PIN_PRIVATE_SENTINEL','what':'provider-secret-sentinel'},
        {'r':'end','achieved':True,'stop':'done','steps':1,'calls':0,'tokens':0,'wall_ms':1500}],True)
    put(root,'other-task/sessions/any-session/state.json',{'schema':catalog.STATE_SCHEMA,
        'serial':'serial-private-sentinel','token':'token-private-sentinel','boot_id':'boot-private-sentinel','transport_origin':'synthetic'})
    put(root,'other-task/sessions/any-session/request.json',{'schema':'phonefarm/confirmation_driver_request@1',
        'transport':{'kind':'fake','program':'/Users/person/secret'},'goal':'render_cadence','capture_seconds':10})
    put(root,'other-task/sessions/any-session/journal.jsonl',[
        {'event':'sample_intent','at':'2026-10-12T00:00:00Z','argv':['PIN_PRIVATE_SENTINEL']},
        {'event':'helper_diagnostic_recorded','at':'2026-10-12T00:01:00Z','diagnostic_complete':False}],True)
    put(root,'other-task/sessions/any-session/diagnostic/result.json',{
        'schema':'phonefarm/helper_recovery_diagnostic@1','status':'UNKNOWN','exit_code':124,
        'diagnostic_complete':False,'remote_idle_verified':False,'recovery_completed':False,
        'eligible':False,'promotion_allowed':False,'error':'token-private-sentinel',
        'metrics':{'power_watt':None,'fps_mean':None}})
    stages=[{'stage':s,'state':state,'host_wall_ms':10,'error':'PIN_PRIVATE_SENTINEL'}
            for s,state in [('design','completed'),('train','completed'),('quantize','completed'),('deploy','failed'),('evaluate','pending')]]
    put(root,'models-task/session/pipeline.json',{'schema':catalog.MODEL_SCHEMA,'entries':stages,
        'promotion_allowed':False,'real_game_optimization_proven':False,'model_semantics_verified':False,
        'frozen':{'drivers':['provider-secret-sentinel']}})


class CatalogContract(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.p=Path(self.tmp.name).resolve()
        self.root=self.p/'tasks';self.root.mkdir();fixture(self.root)
        self.out=self.p/'export'
    def tearDown(self):self.tmp.cleanup()

    def test_actual_files_index_separate_kinds_and_keep_unknown(self):
        v=catalog.export(self.root,self.out)
        self.assertEqual({r['kind'] for r in v['runs']},{'ui_run','confirmation','model_pipeline'})
        self.assertEqual(len(v['runs']),4)
        ui=next(r for r in v['runs'] if r['kind']=='ui_run')
        self.assertEqual(ui['started_at'],'2026-09-23T18:15:16.518000+00:00')
        self.assertIsNone(ui['finished_at'])
        self.assertEqual(ui['status'],'finished');self.assertTrue(ui['summary']['achieved'])
        self.assertIsNone(ui['qualification']['eligible'])
        self.assertEqual(ui['steps'][1]['summary']['action'],'tap')
        self.assertEqual(ui['steps'][1]['summary']['x'],42)
        self.assertEqual(ui['metrics']['wall_ms'],1500)
        result=next(r for r in v['runs'] if r['source']['schema']=='phonefarm/helper_recovery_diagnostic@1')
        self.assertEqual(result['status'],'UNKNOWN');self.assertIsNone(result['metrics']['power_watt'])
        self.assertFalse(result['summary']['remote_idle_verified'])
        session=next(r for r in v['runs'] if r['source']['schema']==catalog.STATE_SCHEMA)
        self.assertEqual(session['source']['evidence_origin'],'synthetic');self.assertEqual(session['status'],'UNKNOWN')
        model=next(r for r in v['runs'] if r['kind']=='model_pipeline')
        self.assertEqual(model['status'],'failed');self.assertFalse(model['qualification']['eligible'])
        self.assertEqual([s['status'] for s in model['steps']],['completed']*3+['failed','pending'])

    def test_public_projection_omits_private_source_and_input(self):
        v=catalog.export(self.root,self.out)
        all_public=(self.out/'catalog.json').read_bytes()+b''.join(p.read_bytes() for p in (self.out/'artifacts').iterdir())
        for value in ['PIN_PRIVATE_SENTINEL','provider-secret-sentinel','serial-private-sentinel','token-private-sentinel','boot-private-sentinel','/Users/person/secret']:
            self.assertNotIn(value.encode(),all_public)
        self.assertFalse(v['snapshot']['live_device'])

    def test_symlink_private_auth_fifo_and_oversize_are_not_read(self):
        outside=self.p/'outside.json';outside.write_text('SECRET')
        (self.root/'external').symlink_to(self.p,target_is_directory=True)
        put(self.root,'private-auth/key.json',{'secret':'SECRET'})
        fifo=self.root/'fifo';os.mkfifo(fifo)
        reader=catalog.Reader(self.root)
        try:
            for path in ['external/outside.json','private-auth/key.json','fifo','../outside.json']:
                with self.assertRaises((OSError,ValueError)):reader.read(path)
            huge=self.root/'huge';huge.write_bytes(b'')
            with huge.open('wb') as f:f.truncate(catalog.FILE_CAP+1)
            with self.assertRaises(ValueError):reader.read('huge')
        finally:reader.close()

    def test_private_auth_direct_root_or_ancestor_is_rejected(self):
        hidden=self.p/'private-auth';hidden.mkdir();(hidden/'tasks').mkdir()
        for root in [hidden,hidden/'tasks']:
            with self.assertRaises(ValueError):catalog.Reader(root)
        with self.assertRaises(ValueError):catalog.export(self.root,self.out,extra_tasks_roots={'other':hidden})

    def test_malformed_log_not_finished_and_mtime_not_sample_time(self):
        p=self.root/'arbitrary-task/runs/run-a/log.jsonl';p.write_bytes(p.read_bytes()+b'{broken\n')
        v=catalog.export(self.root,self.out)
        ui=next(r for r in v['runs'] if r['kind']=='ui_run')
        self.assertEqual(ui['status'],'UNKNOWN')
        result=next(r for r in v['runs'] if r['source']['schema']=='phonefarm/helper_recovery_diagnostic@1')
        self.assertIsNone(result['updated_at']);self.assertIsNotNone(result['source']['source_mtime'])

    def test_namespaces_do_not_merge_same_task_and_run_names(self):
        extra=self.p/'extra';extra.mkdir();fixture(extra)
        v=catalog.export(self.root,self.out,extra_tasks_roots={'models':extra})
        self.assertEqual(len(v['runs']),8);self.assertEqual(len({r['id'] for r in v['runs']}),8)
        self.assertEqual({r['source']['source_root'] for r in v['runs']},{'phonefarm','models'})

    def test_caps_are_explicit_and_prior_exports_not_reindexed(self):
        (self.root/'build').mkdir();put(self.root,'build/something/result.json',{'schema':'wrong'})
        put(self.root,'previous/release-manifest.json',{'complete':True})
        put(self.root,'previous/hidden/pipeline.json',{'schema':catalog.MODEL_SCHEMA})
        old=catalog.MAX_TOTAL_ENTRIES;catalog.MAX_TOTAL_ENTRIES=2
        try:v=catalog.export(self.root,self.out)
        finally:catalog.MAX_TOTAL_ENTRIES=old
        self.assertTrue(any('directory_entry_cap' in d['code'] for d in v['diagnostics']))

    def test_cache_heavy_first_root_does_not_starve_later_producer_root(self):
        for i in range(60):
            for j in range(8):
                p=self.root/'noise-task'/('branch-'+str(i))/('item-'+str(j)+'.txt')
                p.parent.mkdir(parents=True,exist_ok=True);p.write_text('irrelevant build metadata')
        extra=self.p/'extra';extra.mkdir();fixture(extra)
        old=catalog.MAX_TOTAL_ENTRIES;catalog.MAX_TOTAL_ENTRIES=400
        try:v=catalog.export(self.root,self.out,extra_tasks_roots={'models':extra})
        finally:catalog.MAX_TOTAL_ENTRIES=old
        self.assertTrue(any(r['kind']=='model_pipeline' and r['source']['source_root']=='models' for r in v['runs']))
        self.assertTrue(any(r['source']['schema']==catalog.STATE_SCHEMA and r['source']['source_root']=='models' for r in v['runs']))
        self.assertTrue(any('cap' in d['code'] for d in v['diagnostics']))

    def test_physical_reviews_keep_latest_barrier_separate_from_old_restore(self):
        root=self.root/'arbitrary-task/livephase'
        data=b'raw local receipt with private input';p=root/'new-phase/raw.txt';p.parent.mkdir(parents=True);p.write_bytes(data)
        put(self.root,'arbitrary-task/livephase/new-phase/review.json',{
            'schema':'phonefarm/live_queued_prepare_review@1','status':'BLOCKED_HELPER_WITNESS_AND_RECONCILIATION_UNKNOWN',
            'written_at_unix_s':1791763334.137372,'prepare':{'exit_code':1,'host_elapsed_s':121.491,
                'helper_actual_installed_files':5,'main_bundle_install_calls':0,'candidate_control_calls':0,
                'completed_baseline_raw_count':0},
            'reconcile':{'exit_code':1,'host_elapsed_s':15.328,'status':{'error':'worker_reconcile_render exited 85: SECRET_ERROR'}},
            'recovery_barrier':{'retained':True,'markers':[{'token':'MARKER_SECRET'}]},
            'claims':{'full_restore':'UNKNOWN_NOT_CLAIMED','baseline_repeatability':'NOT_RUN_0_OF_3'},
            'metrics':{'power_watt':999,'display_fps':None},'password':'PRIVATE_UNKNOWN_KEY',
            'artifact_sha256':{'raw.txt':catalog.digest(data)}})
        put(self.root,'arbitrary-task/livephase/old-phase/review.json',{
            'schema':'phonefarm/live_existing_prepare_terminal_review@1','status':'BLOCKED',
            'recovery':{'status':'RESTORED_REQUIRES_NEW_SESSION','full_initial_snapshot_equal':True,'exit_code':0}})
        # No second physical record from a sibling result.
        put(self.root,'arbitrary-task/livephase/new-phase/result.json',{'schema':'phonefarm/control_probe@1','status':'PASS'})
        put(self.root,'arbitrary-task/build/review.json',{'schema':'phonefarm/live_queued_prepare_review@1','status':'PASS'})
        v=catalog.export(self.root,self.out)
        phases=[r for r in v['runs'] if r['kind']=='experiment_phase'];self.assertEqual(len(phases),2)
        latest=next(r for r in phases if r['label']=='new-phase');old=next(r for r in phases if r['label']=='old-phase')
        obs=latest['summary']['observations'];self.assertEqual(obs['prepare']['host_elapsed_s'],121.491)
        self.assertEqual(obs['reconcile']['receiver_exit_code'],85);self.assertTrue(obs['recovery_barrier']['retained'])
        self.assertNotIn('recovery',obs);self.assertTrue(old['summary']['observations']['recovery']['full_initial_snapshot_equal'])
        self.assertIsNone(obs['metrics']['power_watt']);self.assertFalse(latest['qualification']['eligible'])
        m=json.loads((self.out/'release-manifest.json').read_bytes());self.assertEqual(len(m['phase_imports']),2)
        self.assertEqual(latest['summary']['evidence_integrity']['status'],'verified')
        public=(self.out/'catalog.json').read_bytes()+b''.join(p.read_bytes() for p in (self.out/'artifacts').iterdir())
        for secret in [b'PRIVATE_UNKNOWN_KEY',b'MARKER_SECRET',b'SECRET_ERROR',data]:self.assertNotIn(secret,public)

    def test_phase_reference_failures_remain_explicit_without_reading_outside(self):
        d=self.root/'t/livephase/any';d.mkdir(parents=True)
        (d/'real.txt').write_text('changed');(d/'linked.txt').symlink_to(self.p/'outside-secret')
        (self.p/'outside-secret').write_text('NEVER_PUBLIC');os.mkfifo(d/'fifo')
        put(self.root,'t/private-auth/secret.json',{'password':'NEVER_READ'})
        put(self.root,'t/livephase/any/review.json',{'schema':'hgamey/live_checkpoint_review@1','completed':True,
            'status':'DIAGNOSTIC_OBSERVED','artifact_sha256':{'real.txt':'a'*64,'linked.txt':'a'*64,
              'fifo':'a'*64,'../other.txt':'a'*64,'../../private-auth/secret.json':'a'*64,
              str(self.p/'outside-secret'):'a'*64}})
        v=catalog.export(self.root,self.out);r=next(r for r in v['runs'] if r['kind']=='experiment_phase')
        self.assertEqual(r['summary']['evidence_integrity'],{'status':'partial','declared_count':6,'verified_count':0,'failed_count':6})
        self.assertFalse(r['qualification']['eligible'])
        m=json.loads((self.out/'release-manifest.json').read_bytes());refs=m['phase_imports'][0]['reference_receipts']
        self.assertTrue(all(t['verified'] is False for t in refs));self.assertNotIn(str(self.p),catalog.encode(refs).decode())
        self.assertNotIn(b'NEVER_PUBLIC',(self.out/'catalog.json').read_bytes())

    def test_histogram_scope_and_schema_terminal_rules_are_not_optimization(self):
        put(self.root,'t/livephase/buckets/review.json',{'schema':'hgamey/live_checkpoint_review@1','completed':True,
            'layer':{'total_frames':900,'histogram_interval_count':900,'histogram_counts':{'16ms':1,'33ms':890,'50ms':9},
                     'slow_bucket_ratio':.01,'slow_bucket_threshold_ms':50,'uid':99999,'layer_name':'PRIVATE_LAYER'}})
        put(self.root,'t/livephase/not-final/review.json',{'schema':'hgamey/live_checkpoint_review@1','completed':False})
        put(self.root,'t/livephase/unknown/review.json',{'schema':'unknown/test@1','terminal':True,'performance_eligible':False,'scope':'fake'})
        v=catalog.export(self.root,self.out);phases=[r for r in v['runs'] if r['kind']=='experiment_phase']
        self.assertEqual(len(phases),1);layer=phases[0]['summary']['observations']['layer']
        self.assertEqual(layer['slow_bucket_ratio'],.01);self.assertEqual(sum(layer['histogram_counts'].values()),900)
        self.assertIn('50ms_label_bucket',layer['histogram_fraction_scope']);self.assertNotIn('slow_bucket_threshold_ms',layer)
        self.assertNotIn('uid',layer);self.assertNotIn('layer_name',layer)
        self.assertFalse(phases[0]['qualification']['eligible'])

    def test_dashboard_types_ids_and_hashes_survive_without_private_live_state(self):
        h='a'*64
        raw={'generated_at':'2026-10-12T00:00:00Z','paths':{'phonefarm':'/Users/person/phonefarm','phonefarm_present':True},
            'layers':[{'id':'operator','rounds':[{'id':'01234567','sha256':h},{'id':'89abcdef','sha256':h}]}],
            'device':{'history':[{'label':'token-private-sentinel'}],'queue':['private'],
                      'holder':{'serial':'serial-private-sentinel'},'recent_tasks':[{'goal':'PIN_PRIVATE_SENTINEL'}]},
            'fleet':{'serial':'serial-private-sentinel'},'notices':[],
            'password':'PlainPrivateSecret','ui_text':'用户界面秘密',
            'nested_unknown':{'password':'NestedSecret'},'credential':123456}
        p=self.p/'dashboard.json';p.write_bytes(catalog.encode(raw))
        v=catalog.export(self.root,self.out,p)['dashboard']
        self.assertEqual(v['device']['history'],[]);self.assertIsNone(v['device']['holder']);self.assertIsNone(v['fleet'])
        self.assertEqual(v['layers'][0]['id'],'operator')
        self.assertNotEqual(v['layers'][0]['rounds'][0]['id'],v['layers'][0]['rounds'][1]['id'])
        self.assertEqual(v['layers'][0]['rounds'][0]['sha256'],h)
        self.assertIsNone(v['notices'][-1]['evidence'])
        self.assertNotIn('PIN_PRIVATE_SENTINEL',catalog.encode(v).decode())
        for sentinel in ['PlainPrivateSecret','用户界面秘密','NestedSecret','123456']:
            self.assertNotIn(sentinel,catalog.encode(v).decode())


class HTTPContract(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.p=Path(self.tmp.name).resolve()
        self.root=self.p/'tasks';self.root.mkdir();fixture(self.root)
        self.snapshot=self.p/'snapshot';self.value=catalog.export(self.root,self.snapshot)
        self.s=server.make_server(self.snapshot,port=0)
        self.thread=threading.Thread(target=self.s.serve_forever);self.thread.start()
        self.url='http://127.0.0.1:'+str(self.s.server_port)
    def tearDown(self):self.s.shutdown();self.s.server_close();self.thread.join();self.tmp.cleanup()
    def get(self,path):
        with urlopen(self.url+path,timeout=5) as r:return json.load(r)
    def fail(self,path,code,method='GET'):
        with self.assertRaises(HTTPError) as e:urlopen(Request(self.url+path,method=method),timeout=5)
        self.assertEqual(e.exception.code,code)
        e.exception.close()

    def test_tasks_runs_details_steps_and_real_artifact_queries(self):
        meta=self.get('/api/meta');self.assertEqual(meta['counts']['ui_runs'],1);self.assertFalse(meta['device_live'])
        tasks=self.get('/api/tasks?limit=200');self.assertEqual(tasks['total'],3)
        ui=next(r for r in self.value['runs'] if r['kind']=='ui_run')
        rows=self.get('/api/tasks/'+ui['task_id']+'/runs?limit=30')
        self.assertEqual(rows['items'][0]['id'],ui['id'])
        self.assertEqual(self.get('/api/runs?kind=model_pipeline')['total'],1)
        detail=self.get('/api/runs/'+ui['id']);self.assertEqual(len(detail['steps']),3)
        self.assertEqual(self.get('/api/runs/'+ui['id']+'/steps/1')['step']['summary']['action'],'tap')
        art=detail['artifacts'][0];self.assertNotIn('file',art)
        public=self.get('/api/artifacts/'+art['id']);self.assertTrue(public['raw_text_omitted'])

    def test_invalid_unknown_and_write_routes_rejected(self):
        for path in ['/api/tasks?limit=201','/api/tasks?offset=-1','/api/tasks?path=/etc/passwd','/api/tasks?limit=1&limit=2']:
            self.fail(path,400)
        for path in ['/api/file?path=/etc/passwd','/api/runs/unknown','/api/artifacts/../../etc/passwd','/api/dashboard']:
            self.fail(path,404)
        self.fail('/api/tasks',405,'POST')

    def test_phase_kind_filter_and_count_use_the_same_snapshot(self):
        put(self.root,'arbitrary-task/livephase/arbitrary-phase/review.json',{
            'schema':'phonefarm/live_queued_prepare_review@1','status':'BLOCKED',
            'prepare':{'completed_baseline_raw_count':0},'claims':{'baseline_repeatability':'NOT_RUN_0_OF_3'}})
        new=self.p/'phases';v=catalog.export(self.root,new);self.s.store=server.Store(new)
        meta=self.get('/api/meta');self.assertEqual(meta['counts']['experiment_phase'],1)
        page=self.get('/api/runs?kind=experiment_phase');self.assertEqual(page['total'],1)
        detail=self.get('/api/runs/'+page['items'][0]['id'])
        self.assertEqual(meta['snapshot'],detail['snapshot']);self.assertFalse(detail['run']['qualification']['eligible'])
        self.assertEqual(detail['steps'][0]['summary']['completed_baseline_raw_count'],0)

    def test_corrupt_artifact_cannot_be_served(self):
        art=next(iter(self.value['artifacts'].values()));(self.snapshot/art['file']).write_bytes(b'{}')
        self.fail('/api/artifacts/'+art['id'],400)

    def test_symlink_current_swap_does_not_change_pinned_artifact(self):
        current=self.p/'current';current.symlink_to(self.snapshot,target_is_directory=True)
        store=server.Store(current);v,lookup,pinned=store.load()
        other=self.p/'other';catalog.export(self.root,other)
        current.unlink();current.symlink_to(other,target_is_directory=True)
        art=next(iter(v['artifacts']))
        raw=store.artifact(v,lookup,pinned,art)
        self.assertEqual(catalog.digest(raw),v['artifacts'][art]['sha256'])


if __name__=='__main__':unittest.main()
