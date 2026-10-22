#!/usr/bin/env python3
"""Bounded synchronized catalog of genuine phonefarm/GOL records, no device I/O."""
import argparse
from collections import deque
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat

FILE_CAP = 4 * 1024 * 1024
TOTAL_CAP = 128 * 1024 * 1024
MAX_FILES = 6000
MAX_ENTRIES = 512
MAX_DEPTH = 7
MAX_DIRS = 3000
MAX_TOTAL_ENTRIES = 20000
MAX_STEPS = 2000
SKIP = {'private-auth', 'target', 'node_modules', 'venv', 'vendor', '__pycache__',
        'accepted-tool', 'signed-tool', 'tools', 'output', 'releases','build','downloads','cache','source','sources'}
STATE_SCHEMA = 'phonefarm/confirmation_driver_state@1'
MODEL_SCHEMA = 'game_opt_loop/model_pipeline_journal@1'
RESULT_SCHEMAS = {'phonefarm/helper_recovery_diagnostic@1',
                  'phonefarm/confirmation_observation@1', 'phonefarm/confirmation_observation@2',
                  'phonefarm/control_probe@1', 'phonefarm/control_probe_report@1'}
NUMERIC = {'steps', 'calls', 'tokens', 'ms', 'api_ms', 'step_ms', 'n', 'fps', 'cpu',
           'pss', 'battery', 'temp', 'host_wall_ms', 'exit_code', 'frames', 'samples',
           'frame_p95', 'frame_p95_ms', 'fps_mean', 'power_watt', 'duration_s','wall_ms',
           'x','y','x2','y2'}
ACTION={'tap','swipe','back','home','wait','text','type','key','rotate','launch','drag','hold',
        'joystick','stop','done','input','pinch','longpress','screenshot'}
STOP={'done','budget','timeout','max_steps','interrupt','error','capture_error','script_done',
      'blocked','crash','anr','watchdog','manual','failed','exhausted','stopped'}
# Serialized native viz model fields, not arbitrary fields from source documents.
NATIVE_FIELDS=set('''generated_at version paths layers carriers device fleet notices
game_opt_loop phonefarm phonefarm_present lock_root id index name subtitle rounds_total
qualified eliminated invalid evidence_only rounds problems title when layer verdict verdict_text
metric_source headline candidates arms criteria conditions pareto evidence notes label value unit tone hint
from key pass detail origin stage latency_ms frame_p95_ms power_watt psnr_db p_value on_front
power_excluded_reason violations metric a_label b_label a_runs b_runs a_values b_values others diff_pct
significant x_label y_label quality_reference points generation x y on_front path rel kind size_bytes
level body carrier window n_runs p95_dispersion_pct p50_dispersion_pct p95_median_ms fps_median
gate_pct gate_metric status note runs holder queue history recent_tasks last_touch scanned_runs
task run goal serial modified at sha256 snapshot'''.split())


def digest(b): return hashlib.sha256(b).hexdigest()


def encode(v):
    return (json.dumps(v, ensure_ascii=False, sort_keys=True, allow_nan=False, indent=2)+'\n').encode()


def strict_json(b):
    def pairs(items):
        result = {}
        for k, v in items:
            if k in result: raise ValueError('duplicate JSON key')
            result[k] = v
        return result
    return json.loads(b, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON')))


def opaque(kind, path): return kind+'_'+digest(path.encode())[:24]


def label(v):
    if not isinstance(v, str): return None
    v = re.sub(r'/(?:Users|private|var|home|data)/[^\s,;]+', '[private-path]', v)
    v = re.sub(r'\b[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}\b', '[boot-id]', v)
    v = re.sub(r'\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?', '[network]', v)
    v = re.sub(r'(?<![a-zA-Z0-9])[0-9a-fA-F]{7,64}(?![a-zA-Z0-9])', '[id]', v)
    v = re.sub(r'(?i)\bUID\s*\d{4,}', '[account-id]', v)
    v = re.sub(r'\bsession_[a-zA-Z0-9_]+', '[session]', v)
    v = re.sub(r'(?i)(?:api[_-]?key|password|passwd|credential|token|serial|uid)\s*[:=]\s*\S+', '[private]', v)
    return v[:160]


def number(v):
    return v if type(v) in {int, float} and math.isfinite(v) else None


def stamp(v):
    if type(v) in {float, int} and math.isfinite(v) and 0 <= v <= 32503680000:
        return dt.datetime.fromtimestamp(v, dt.timezone.utc).isoformat()
    if isinstance(v, str) and len(v) <= 64:
        try:
            parsed = dt.datetime.fromisoformat(v.replace('Z', '+00:00'))
            if parsed.tzinfo is not None: return parsed.isoformat()
        except ValueError: pass
    return None


def ui_stamp(v):
    # Native log.jsonl ts is Unix milliseconds (Logger contract), not guessed units.
    return stamp(v/1000) if type(v) in {int,float} and math.isfinite(v) else stamp(v)


def status(v):
    return v if isinstance(v, str) and re.fullmatch(r'[a-zA-Z_]{1,80}', v) else 'UNKNOWN'


# Physical producer reviews are distinct from protocol sessions and code tests.
PHASE_SCHEMAS = {
 'hgamey/live_phase_review@1','hgamey/live_checkpoint_review@1',
 'phonefarm/ui_checkpoint_review@1','phonefarm/frametimeline_capture_review@1',
 'phonefarm/live_helper_readonly_diagnostic_terminal_review@1',
 'phonefarm/live_existing_prepare_terminal_review@1','phonefarm/live_queued_prepare_review@1',
 'phonefarm/readonly_checkpoint_review@1','phonefarm/readonly_inventory_checkpoint@1',
 'phonefarm/live_control_attempt_review@1','phonefarm/carrier_smoke_review@1'}
PHASE_REF_CAP=12*1024*1024
PHASE_TOTAL_CAP=512*1024*1024
PHASE_NUMBERS=set('''exit_code elapsed_s host_elapsed_s elapsed_wall_s duration_s code
helper_actual_installed_files main_bundle_install_calls candidate_control_calls
completed_baseline_raw_count performance_baselines_collected performance_baselines_requested
original_gpu_min_pwrlevel actual_gpu_min_before actual_gpu_min_after actual_ddr_boost_after
actual_llcc_boost_after actual_gpu_max_after actual_tracing_on_after lease_exit lease_recovery_exit
normal_lease_after_recovery_exit_code normal_postfailure_lease_checkpoint_exit
normal_postfailure_lease_exit original_lease_exit lease_wrapper_exit_code capture_cli_exit
witness_exit witness_bytes actual_thread_count trace_bytes target_surface_rows
tracing_sessions_after model_api_calls control_writes formal_samples device_writes
successful_device_calls successful_device_commands successful_device_calls_in_this_phase
actual_device_window_s actual_requested_sleep_count nominal_requested_sample_interval_s
set_frame_rate_vote reported_average_fps total_frames histogram_interval_count slow_bucket_ratio
slow_bucket_threshold_ms fps_mean frame_p95_ms power_watt display_fps target_queued_cadence_mean
 target_queued_interval_p95_ms sample_count span_s max_single_sensor_range_c gap_after_warmup_s
uptime_s warmup_last_uptime_s actual_span_s max_sensor_span_c frozen_max_span_c
maximum_sensor_span_C readings device_warmup_span_s original_game_fps original_fps
current_fixed_load_fps load_configuration_fps original_load_configuration_fps
fps_setting last_known_fps celsius hal_status thermal_status status GPU_min_before
GPU_min_blocked_after gpu_min_before gpu_min_terminal gpu_min_pwrlevel gpuclk_raw
 gpu_clock_hz ddr_boost_freq_raw ddr_cur_freq_raw llcc_boost_freq_raw llcc_cur_freq_raw
skin_C battery_C outer_timeout_s registered_timeout_s transport_timeout_s stdout_bytes
stderr_bytes recorded_reader_exit reader_log_bytes new_baselines sf_stop_clear_exit
self_reported_frames_submitted version_code'''.split())
PHASE_BOOLS=set('''completed terminal eligible performance_eligible promotion_allowed
real_game_optimization_proven real_game_qualification optimization_improvement_proven
optimized_effect_proven performance_gain_proven display_verified display_fps_proven
formal_metric_admitted present_timing_verified game_proof game_optimization_proven
energy_improvement_proven independent_gpu_completion_proven gpu_completion_attested
actual_clock_sustained_proven actual_device_executed actual_control_write_executed
candidate_control_applied candidate_written readback_hold_executed hold_11s_executed
restore_experiment_executed restoration_experiment_executed main_remote_tools_installed
initial_snapshot_restore_graph_and_state_created initial_snapshot_and_restore_graph_frozen
thermal_reference_warmup_passed main_frozen_inputs_exists timedout_remote_idle_verified
helper_closed_proven helper_removed_proven full_control_restore_claimed old_samples_revalidated
full_initial_snapshot_equal owned_groups_absent_verified_by_existing_receiver
remote_session_permanently_revoked remote_idle_verified recovery_completed control_restore_claimed
barrier_before barrier_after retained manually_edited marker_manually_edited
stop_close_delete_performed no_further_normal_lease_or_media_phase no_automatic_retry
real_device_operations_nonzero device_commands_nonzero device_calls_nonzero device_execution
native_pair_capture_executed device_installed native_capture_executed native_capture_installed
frequency_residency_proven package_uid_verified unique_display_join capture_repeated trace_repeated
remote_owned_group_absent owned_directory_removed self_reported_clean_exit
host_process_absence_after_owned_force_stop full_thread_inventory_complete terminal_marker end_marker
raw_readings_saved rules_changed all_gaps_within_frozen_6_s diagnostic_complete
all_registered_file_sections_read process_scan_record_present terminal_marker_present
pending_file_present permit_present master_and_children_rows_have_final_lf
safe_to_declare_old_group_absent safe_to_claim_reconciliation_success continuous_scene_proven
visible_camp_before_prepare settings_changed other_graphics_values_unchanged restored_fps_visually_confirmed
foreground_game_not_proven original_game_fps_restore_pending normal_lease_checkpoint_after_attempt_succeeded
original_result_preserved diagnostic_calculations_not_a_qualification'''.split())
PHASE_TEXT=set('''scope scene claim_scope metric_scope raw_clock_scope observed_image_state
observed_screen observed_screens screenshot_scene graphics render_scale version_name
status reason failed_operation selected_control selected_control_write pre_baseline_probe
capture_ftrace_workload_env temperature_scope raw_scope baseline_repeatability full_restore
control_write_and_11s_hold main_install_batch_wall_time_speedup background_download
background_genshin_download normal_lease_or_prepare_after_diagnosis reconcile_after_diagnosis
control_11s_hold full_restore_snapshot quantile_scope master_and_child_proc_stat_status_cmdline
remote_master_current_liveness ledger_field_format fps histogram frequency gate'''.split())
PHASE_OBJECTS=set('''prepare reconcile recovery recovery_barrier claims scene carrier
warmup blocked_snapshot host remote observed analysis layer metrics metric_semantics
cached_shell_skin actual_sysfs_at_warmup clock_observation thermal_diagnostics actual_thread_checkpoint
candidate_requested'''.split())
PHASE_SERIES={'hal_statuses','thermal_statuses','observed_gap_s','actual_gaps_s','actual_uptime_s',
              'actual_sample_start_intervals_s_range','CPU_range_C','GPU_range_C'}


def phase_project(v,depth=0):
    """Finite producer fields, no argv/path/marker/UI input or unknown strings."""
    if not isinstance(v,dict) or depth>4:return {}
    out={}
    for k,x in v.items():
        if k=='power_watt':out[k]=None  # These diagnostic phases have no power qualification.
        elif k in PHASE_BOOLS and type(x) is bool:out[k]=x
        elif k in PHASE_NUMBERS and (number(x) is not None or x is None):out[k]=number(x)
        elif k in PHASE_NUMBERS and isinstance(x,str) and re.fullmatch(r'[+-]?\d{1,12}(?:\.\d{1,8})?',x):out[k]=number(float(x))
        elif k in PHASE_OBJECTS and isinstance(x,dict):out[k]=phase_project(x,depth+1)
        elif k in PHASE_TEXT and isinstance(x,str):out[k]=label(x)
        elif k in PHASE_SERIES and isinstance(x,list) and len(x)<=256:
            if all(number(t) is not None for t in x):out[k]=x
        elif k in {'sensor_spans_c','raw_span_by_sensor_C','histogram_counts'} and isinstance(x,dict) and len(x)<=256:
            pattern=r'(?:\d{1,4}ms)' if k=='histogram_counts' else r'/sys/class/(?:thermal/thermal_zone\d+/temp|power_supply/battery/temp)'
            out[k]={s:number(t) for s,t in x.items() if re.fullmatch(pattern,s) and number(t) is not None}
    # Fixed receiver error grammar yields the remote code, never publishes stderr.
    error=v.get('error')
    if error is None and isinstance(v.get('status'),dict):error=v['status'].get('error')
    if isinstance(error,str):
        found=re.fullmatch(r'(worker_[a-z_]{1,48}|thermal_snapshot) exited ([0-9]{1,3}): .*',error,re.DOTALL)
        if found:out['receiver_exit_code']=int(found[2])
    # The historical name "threshold" does not establish a precise >=50ms cut.
    if 'slow_bucket_ratio' in out:
        out['histogram_fraction_scope']='50ms_label_bucket_over_retained_pairs_not_jank_or_whole_window'
        out.pop('slow_bucket_threshold_ms',None)
    return out


def phase_schema(v):
    schema=v.get('schema')
    if schema in PHASE_SCHEMAS:
        if schema=='hgamey/live_phase_review@1' and v.get('terminal') is not True:return None
        if schema=='hgamey/live_checkpoint_review@1' and v.get('completed') is not True:return None
        return schema
    # Legacy terminal receipts have no schema; never accept arbitrary schema values.
    if schema is None and v.get('terminal') is True and isinstance(v.get('scope'),str) and (
        type(v.get('device_writes')) is int or type(v.get('control_writes')) is int or
        v.get('performance_eligible') is False):return 'phonefarm_web/legacy_terminal_declaration@1'
    if schema is None and status(v.get('status'))=='LEASE_RECONCILED' and type(v.get('lease_recovery_exit')) is int:
        return 'phonefarm_web/legacy_lease_declaration@1'
    return None


class Reader:
    def __init__(self, root, scope='phonefarm', budget=None):
        self.root = Path(root).absolute()
        if any(part in SKIP or part.startswith('.') for part in self.root.parts[1:]):
            raise ValueError('private_or_cache_source_root')
        if not re.fullmatch('[a-z][a-z0-9_-]{0,39}',scope):raise ValueError('invalid source alias')
        self.scope=scope
        self.budget=budget if budget is not None else {'bytes':0,'files':{},'diagnostics':[],'visited':0,'entries':0}
        self.files=self.budget['files'];self.diagnostics=self.budget['diagnostics']
        self.share=1
        self.usage=self.budget.setdefault('scopes',{}).setdefault(scope,{'bytes':0,'files':0,'visited':0,'entries':0})
        # The caller root itself and every ancestor must be ordinary directories.
        self.fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in self.root.parts[1:]:
                new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.fd)
                os.close(self.fd); self.fd = new
        except Exception:
            os.close(self.fd); raise

    def close(self): os.close(self.fd)

    def identity(self,path):return self.scope+'/'+path

    def diagnostic(self, path, code):
        if len(self.diagnostics) < 200:
            self.diagnostics.append({'source_id': opaque('source', self.identity(path)), 'source_root':self.scope,'code': code})

    def read(self, path):
        parts = path.split('/')
        if not parts or any(not p or p in SKIP or p.startswith('.') or p == '..' for p in parts):
            raise ValueError('private_or_invalid_path')
        if len(self.files) >= MAX_FILES: raise ValueError('source_file_count_cap')
        if self.usage['files']>=MAX_FILES//self.share and self.identity(path) not in self.files:
            raise ValueError('per_root_file_count_cap')
        fd = os.dup(self.fd)
        try:
            for part in parts[:-1]:
                new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd); fd = new
            child = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with os.fdopen(child, 'rb') as f:
                meta = os.fstat(f.fileno())
                if not stat.S_ISREG(meta.st_mode): raise ValueError('nonregular_source')
                if meta.st_size > FILE_CAP: raise ValueError('source_file_size_cap')
                b = f.read(FILE_CAP+1)
                if len(b) > FILE_CAP: raise ValueError('source_file_grew')
                if self.budget['bytes']+len(b) > TOTAL_CAP: raise ValueError('total_source_bytes_cap')
                if self.usage['bytes']+len(b)>TOTAL_CAP//self.share:raise ValueError('per_root_source_bytes_cap')
                self.budget['bytes'] += len(b)
                self.usage['bytes']+=len(b)
                if self.identity(path) not in self.files:self.usage['files']+=1
                self.files[self.identity(path)] = {'id':opaque('source', self.identity(path)),
                    'source_root':self.scope,'bytes':len(b),'sha256':digest(b)}
                return b, meta
        finally: os.close(fd)

    def json(self, path):
        b, meta = self.read(path)
        v = strict_json(b)
        if not isinstance(v, dict): raise ValueError('object_required')
        return v, b, meta

    def phase_reference(self, path, expected):
        if not isinstance(expected,str) or not re.fullmatch('[0-9a-f]{64}',expected):
            raise ValueError('invalid_reference_hash')
        parts=path.split('/')
        if any(not p or p in SKIP or p.startswith('.') or p=='..' for p in parts):
            raise ValueError('private_or_invalid_reference')
        ledger=self.budget.setdefault('phase_refs',{})
        ident=self.identity(path)
        if ident in ledger:
            row=ledger[ident]
            return {**row,'verified':row['sha256']==expected,'expected_sha256':expected}
        if len(ledger)>=3000:raise ValueError('phase_reference_count_cap')
        fd=os.dup(self.fd)
        try:
            for part in parts[:-1]:
                child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
                os.close(fd);fd=child
            child=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
            with os.fdopen(child,'rb') as f:
                meta=os.fstat(f.fileno())
                if not stat.S_ISREG(meta.st_mode):raise ValueError('nonregular_phase_reference')
                if meta.st_size>PHASE_REF_CAP:raise ValueError('phase_reference_size_cap')
                h=hashlib.sha256();size=0
                while True:
                    b=f.read(min(65536,PHASE_REF_CAP+1-size))
                    if not b:break
                    size+=len(b)
                    if size>PHASE_REF_CAP:raise ValueError('phase_reference_grew')
                    total=self.budget.get('phase_ref_bytes',0)+len(b)
                    if total>PHASE_TOTAL_CAP:raise ValueError('total_phase_reference_bytes_cap')
                    self.budget['phase_ref_bytes']=total;h.update(b)
                row={'source_id':opaque('source',ident),'source_root':self.scope,
                     'bytes':size,'sha256':h.hexdigest()}
                ledger[ident]=row
                return {**row,'verified':row['sha256']==expected,'expected_sha256':expected}
        finally:os.close(fd)

    def entries(self, relative=''):
        self.budget['visited'] += 1
        self.usage['visited']+=1
        if self.usage['visited']>MAX_DIRS//self.share:
            self.diagnostic(relative,'per_root_directory_count_cap');return []
        if self.usage['entries']>=MAX_TOTAL_ENTRIES//self.share:
            self.diagnostic(relative,'per_root_directory_entry_cap');return []
        if self.budget['visited'] > MAX_DIRS:
            self.diagnostic(relative, 'directory_count_cap');return []
        fd=os.dup(self.fd)
        rows=[]
        try:
            for part in relative.split('/') if relative else []:
                if part in SKIP or part.startswith('.') or not part:raise ValueError('private_dir')
                new=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
                os.close(fd);fd=new
            class Entry:
                def __init__(self,name,mode,mtime):self.name=name;self.mode=mode;self.mtime=mtime
                def is_dir(self,follow_symlinks=False):return stat.S_ISDIR(self.mode)
                def is_file(self,follow_symlinks=False):return stat.S_ISREG(self.mode)
            with os.scandir(fd) as scan:
                for i,e in enumerate(scan):
                    self.budget['entries']+=1
                    self.usage['entries']+=1
                    if self.usage['entries']>MAX_TOTAL_ENTRIES//self.share:
                        self.diagnostic(relative,'per_root_directory_entry_cap');break
                    if self.budget['entries']>MAX_TOTAL_ENTRIES:
                        self.diagnostic(relative,'total_directory_entry_cap');break
                    if i>=MAX_ENTRIES:
                        self.diagnostic(relative,'directory_entry_cap');break
                    if e.name.startswith('.') or e.name in SKIP:continue
                    meta=os.stat(e.name,dir_fd=fd,follow_symlinks=False)
                    if stat.S_ISLNK(meta.st_mode):continue
                    rows.append(Entry(e.name,meta.st_mode,meta.st_mtime))
        except OSError:
            self.diagnostic(relative, 'directory_read_failed')
        finally:os.close(fd)
        priority={'livephase':0,'runs':1,'sessions':2,'session':3,'pipeline':4}
        return sorted(rows,key=lambda e:(priority.get(e.name,5),-e.mtime,e.name))


class Catalog:
    def __init__(self, reader, now=None):
        self.reader = reader
        self.time = now or dt.datetime.now(dt.timezone.utc).isoformat()
        self.tasks = []; self.runs = []; self.artifacts = {}; self.public_files = {};self.phase_imports=[]

    def artifact(self, relative, value, source_hash, name):
        ident = opaque('artifact', self.reader.identity(relative))
        b = encode(value)
        path = 'artifacts/'+ident+'.json'
        self.public_files[path] = b
        row = {'id':ident,'label':name,'kind':'sanitized_json','bytes':len(b),'sha256':digest(b),
               'source_sha256':source_hash,'source_root':self.reader.scope,'source_relative_path':label(relative),
               'source_path_redacted':label(relative)!=relative,'file':path}
        self.artifacts[ident] = row
        return ident

    def run(self, task, relative, kind, schema, raw, meta, runlabel):
        ident = opaque('run', self.reader.identity(relative))
        r = {'id':ident,'task_id':task['id'],'kind':kind,'label':label(runlabel),'status':'UNKNOWN',
             'started_at':None,'finished_at':None,'updated_at':None,
             'qualification':{'eligible':None,'scope':'source_record_declaration_not_attestation'},
             'metrics':{},'step_count':0,'artifact_count':0,
             'source':{'schema':schema,'sha256':digest(raw),'origin':'synchronized_file',
                       'source_root':self.reader.scope,'source_mtime':stamp(meta.st_mtime)},
             'steps':[],'artifacts':[],'diagnostics':[]}
        self.runs.append(r)
        return r

    def ui(self, task, relative):
        try: raw, meta = self.reader.read(relative)
        except (ValueError, OSError) as e:
            self.reader.diagnostic(relative, type(e).__name__+':source_unreadable'); return
        records = []; broken = False
        for line in raw.splitlines():
            if not line: continue
            if len(line)>65536:
                broken=True; continue
            try:
                v=strict_json(line)
                if isinstance(v,dict):records.append(v)
                else:broken=True
            except (ValueError, UnicodeError):broken=True
        r=self.run(task,relative,'ui_run','phonefarm/log.jsonl',raw,meta,relative.split('/')[-2])
        start=next((v for v in records if v.get('r')=='start'),{})
        end=next((v for v in reversed(records) if v.get('r')=='end'),None)
        r['started_at']=ui_stamp(start.get('ts'));r['finished_at']=ui_stamp(end.get('ts')) if end else None
        times=[ui_stamp(v.get('ts')) for v in records if ui_stamp(v.get('ts'))]
        r['updated_at']=max(times) if times else None
        r['status']='finished' if end and not broken else 'UNKNOWN'
        # Model prompts, actions' input text and screen widget contents are never projected.
        for index,v in enumerate(records[:MAX_STEPS]):
            ty=v.get('r')
            if ty not in {'start','end','act','screen','diff','telemetry','hook','app_event','trace'}:continue
            summary={k:number(v[k]) for k in NUMERIC if k in v and number(v[k]) is not None}
            for key in ['achieved','derived','pass']:
                if type(v.get(key)) is bool:summary[key]=v[key]
            action=v.get('a') if isinstance(v.get('a'),str) else v.get('action')
            if isinstance(action,str) and action in ACTION:summary['action']=action
            if isinstance(v.get('stop'),str) and v['stop'] in STOP:summary['stop']=v['stop']
            step={'index':index,'type':ty,'name':ty,'status':status(v.get('status')),
                  'at':ui_stamp(v.get('ts')),'summary':summary,'artifact_ids':[]}
            r['steps'].append(step)
        if broken:r['diagnostics'].append({'code':'partial_or_malformed_log'})
        if len(records)>MAX_STEPS:r['diagnostics'].append({'code':'step_projection_cap'})
        for k in ['steps','calls','tokens','wall_ms']:
            if end and number(end.get(k)) is not None:r['metrics'][k]=end[k]
        r['summary']={'achieved':end.get('achieved') if end and type(end.get('achieved')) is bool else None,
                      'stop':end.get('stop') if end and isinstance(end.get('stop'),str) and end['stop'] in STOP else None,
                      'scope':'ui_traversal_result_not_optimization_qualification'}
        a=self.artifact(relative,{'schema':'phonefarm_web/sanitized_log@1','status':r['status'],
                       'steps':r['steps'],'raw_text_omitted':True},digest(raw),'log.jsonl / 脱敏记录')
        r['artifacts'].append(a)

    def confirmation(self,task,relative):
        state,raw,meta=self.reader.json(relative+'/state.json')
        if state.get('schema')!=STATE_SCHEMA:return
        request,reqraw,_=self.reader.json(relative+'/request.json')
        if request.get('schema')!='phonefarm/confirmation_driver_request@1':
            raise ValueError('unsupported_request_schema')
        journal,jmeta=self.reader.read(relative+'/journal.jsonl')
        rows=[];broken=False
        for line in journal.splitlines():
            if not line:continue
            try:
                v=strict_json(line)
                if isinstance(v,dict):rows.append(v)
                else:broken=True
            except ValueError:broken=True
        components=relative.split('/')
        name='/'.join(components[-2:]) if components[-1]=='session' else components[-1]
        r=self.run(task,relative+'/state.json','confirmation',STATE_SCHEMA,raw,meta,name)
        if not isinstance(request.get('transport'),dict):raise ValueError('invalid transport object')
        origin=request['transport'].get('kind')
        r['source']['evidence_origin']='synthetic' if origin=='fake' else 'recorded_receiver_declared'
        declared=state.get('transport_origin')
        if declared in {'synthetic','live_device','recorded_device','recorded_receiver','fake','adb','hdc'}:
            r['source']['declared_transport_origin']=declared
        for index,v in enumerate(rows[:MAX_STEPS]):
            event=v.get('event'); op=v.get('operation')
            name=event if isinstance(event,str) and re.fullmatch('[a-z_]{1,80}',event) else 'journal event'
            summary={k:number(v[k]) for k in ['code','exit_code','duration_s'] if k in v and number(v[k]) is not None}
            for k in ['eligible','diagnostic_complete','recovery_completed','remote_idle_verified']:
                if type(v.get(k)) is bool:summary[k]=v[k]
            r['steps'].append({'index':index,'type':'journal','name':name,'status':status(v.get('status')),
                               'at':stamp(v.get('at')),'summary':summary,'artifact_ids':[]})
        if broken:r['diagnostics'].append({'code':'partial_or_malformed_journal'})
        times=[stamp(v.get('at')) for v in rows if stamp(v.get('at'))]
        r['started_at']=min(times) if times else None
        r['updated_at']=max(times) if times else None
        # Intent or file presence alone is never a terminal/idle receipt.
        r['status']='UNKNOWN'
        r['summary']={'goal':request.get('goal') if isinstance(request.get('goal'),str) and request['goal'] in {'render_cadence','render_efficiency'} else None,
                      'capture_seconds':number(request.get('capture_seconds')),'journal_events':len(rows),
                      'recovery_completed':None,'remote_idle_verified':None,'performance_eligible':False}
        a=self.artifact(relative+'/journal.jsonl',{'schema':'phonefarm_web/sanitized_session@1',
                       'summary':r['summary'],'steps':r['steps'],'private_transport_and_input_omitted':True},
                       digest(journal),'journal.jsonl / 脱敏会话')
        r['artifacts'].append(a)
        a=self.artifact(relative+'/state.json',{'schema':STATE_SCHEMA,'goal':r['summary']['goal'],
                      'identity_fields_omitted':True},digest(raw),'state.json / 脱敏状态')
        r['artifacts'].append(a)

    def result(self,task,relative,v,raw,meta):
        schema=v.get('schema')
        if schema not in RESULT_SCHEMAS:return
        r=self.run(task,relative,'confirmation',schema,raw,meta,schema.split('/')[1])
        r['status']=status(v.get('status'))
        q=v.get('qualification',{})
        if not isinstance(q,dict):q={}
        r['qualification']['eligible']=q.get('eligible') if type(q.get('eligible')) is bool else v.get('eligible') if type(v.get('eligible')) is bool else None
        metrics=v.get('metrics',{})
        if isinstance(metrics,dict):
            for k in NUMERIC:
                if k in metrics:r['metrics'][k]=number(metrics[k])
        flags={k:v[k] for k in ['diagnostic_complete','recovery_completed','remote_idle_verified','control_restore_claimed',
                                 'promotion_allowed','real_game_optimization_proven'] if type(v.get(k)) is bool}
        r['summary']=flags
        r['steps']=[{'index':0,'type':'result','name':schema,'status':r['status'],'at':None,
                     'summary':{**flags,'exit_code':number(v.get('exit_code'))},'artifact_ids':[]}]
        if v.get('error'):r['diagnostics'].append({'code':'producer_reported_error_text_omitted'})
        a=self.artifact(relative,{'schema':schema,'status':r['status'],'metrics':r['metrics'],
                                'qualification':r['qualification'],'summary':flags},digest(raw),'result.json / 脱敏结果')
        r['artifacts'].append(a)

    def model(self,task,relative,v,raw,meta):
        if v.get('schema')!=MODEL_SCHEMA:return
        if any(v.get(k) is not False for k in ['promotion_allowed','real_game_optimization_proven','model_semantics_verified']):
            raise ValueError('model_protocol_cannot_claim_game_or_deployment_qualification')
        entries=v.get('entries')
        if not isinstance(entries,list) or len(entries)>5:raise ValueError('invalid_model_entries')
        r=self.run(task,relative,'model_pipeline',MODEL_SCHEMA,raw,meta,'model pipeline')
        states=[]
        for index,item in enumerate(entries):
            if not isinstance(item,dict):raise ValueError('invalid_model_stage')
            stage=item.get('stage');state=item.get('state')
            if not isinstance(stage,str) or not isinstance(state,str) or stage not in {'design','train','quantize','deploy','evaluate'} or state not in {'pending','ready','in_flight','unknown','completed','failed'}:
                raise ValueError('invalid_model_state')
            states.append(state)
            r['steps'].append({'index':index,'type':'model_stage','name':stage,'status':state,
                              'at':stamp(item.get('finished_at')) or stamp(item.get('started_at')),
                              'summary':{'host_wall_ms':number(item.get('host_wall_ms')),'exit_code':number(item.get('exit_code'))},
                              'artifact_ids':[]})
        r['status']='failed' if 'failed' in states else 'completed' if states and all(s=='completed' for s in states) else 'UNKNOWN'
        r['qualification']['eligible']=False
        r['qualification']['scope']='pipeline_protocol_and_artifact_integrity_not_device_or_game'
        r['summary']={k:v.get(k) if type(v.get(k)) is bool else None for k in ['promotion_allowed','real_game_optimization_proven','model_semantics_verified']}
        times=[s['at'] for s in r['steps'] if s['at']]
        r['updated_at']=max(times) if times else None
        a=self.artifact(relative,{'schema':MODEL_SCHEMA,'status':r['status'],'steps':r['steps'],'claims':r['summary']},digest(raw),'pipeline.json / 脱敏阶段')
        r['artifacts'].append(a)

    def phase(self,task,relative,v,raw,meta):
        schema=phase_schema(v)
        if schema is None:return False
        directory=relative.rsplit('/',1)[0]
        if '/livephase/' not in '/'+directory+'/':return False
        declared=v.get('artifact_sha256',v.get('files',{}))
        if not isinstance(declared,dict):raise ValueError('phase_reference_map_required')
        declared=dict(declared)
        if isinstance(v.get('raw_file'),str):declared[v['raw_file']]=v.get('raw_sha256')
        if isinstance(v.get('image_file'),str):declared[v['image_file']]=v.get('image_sha256')
        if len(declared)>128:raise ValueError('phase_reference_map_cap')
        references=[]
        for path,expected in declared.items():
            row={'source_id':opaque('source',self.reader.identity(str(path))),
                 'expected_sha256':expected if isinstance(expected,str) and re.fullmatch('[0-9a-f]{64}',expected) else None,
                 'verified':False}
            try:
                if not isinstance(path,str):raise ValueError('invalid_reference_path')
                if path.startswith('/'):
                    path=str(Path(path).relative_to(self.reader.root))
                elif path.startswith('tasks/'):path=path[len('tasks/'):]
                elif path.startswith('livephase/'):
                    path=directory.split('/livephase/',1)[0]+'/'+path
                else:path=directory+'/'+path
                # No reference to another experiment, accepted binary, or private dir.
                if not path.startswith(directory+'/'):raise ValueError('out_of_phase_reference')
                row=self.reader.phase_reference(path,expected)
                if not row['verified']:row['code']='reference_hash_mismatch'
            except (ValueError,OSError,UnicodeError) as e:
                code=str(e)
                row['code']=code if isinstance(e,ValueError) and re.fullmatch('[a-z_]{1,80}',code) else 'reference_unreadable'
            references.append(row)
        good=sum(t['verified'] for t in references)
        integrity={'status':'verified' if references and good==len(references) else 'partial' if references else 'not_declared',
                   'declared_count':len(references),'verified_count':good,'failed_count':len(references)-good}
        projected=phase_project(v)
        declared_status=status(v.get('status'))
        r=self.run(task,relative,'experiment_phase',schema,raw,meta,directory.split('/')[-1])
        r['status']=declared_status
        r['qualification']={'eligible':False,'scope':'physical_phase_declaration_not_optimization_or_whole_window_qualification'}
        reviewed=next((stamp(v.get(k)) for k in ['written_at_unix_s','completed_at_unix_s','reviewed_at_unix_s','reviewed_unix_s'] if stamp(v.get(k))),None)
        r['updated_at']=reviewed
        r['finished_at']=stamp(v.get('completed_at_unix_s'))
        r['source']['evidence_origin']='recorded_physical_phase_declaration'
        r['summary']={'declared_status':declared_status,'reviewed_at':reviewed,
                      'evidence_integrity':integrity,'observations':projected,
                      'performance_eligible':False,'promotion_allowed':False,'real_game_optimization_proven':False,
                      'metric_scope':'diagnostic_only; submit_proxy_is_not_display_fps; retained_SF_pairs_are_not_whole_window',
                      'power_watt':None}
        r['metrics']={'power_watt':None}
        for name in ['prepare','reconcile','recovery','host','remote','warmup','layer','thermal_diagnostics','clock_observation']:
            if name in projected:
                item=projected[name]
                r['steps'].append({'index':len(r['steps']),'type':'phase_observation','name':name,
                    'status':status(item.get('status')),'at':None,'summary':item,'artifact_ids':[]})
        if not r['steps']:r['steps']=[{'index':0,'type':'phase_observation','name':'checkpoint',
                'status':r['status'],'at':None,'summary':projected,'artifact_ids':[]}]
        if integrity['status']!='verified':r['diagnostics'].append({'code':'phase_reference_integrity_'+integrity['status']})
        a=self.artifact(relative,{'schema':'phonefarm_web/sanitized_phase_review@1','producer_schema':schema,
             'status':r['status'],'summary':r['summary'],'reference_receipts':references,
             'raw_artifacts_and_private_identifiers_omitted':True},digest(raw),'review / 脱敏实验阶段')
        r['artifacts'].append(a)
        self.phase_imports.append({'run_id':r['id'],'source_root':self.reader.scope,
            'source_id':opaque('source',self.reader.identity(relative)),'original_sha256':digest(raw),
            'producer_schema':schema,'evidence_integrity':integrity,'reference_receipts':references})
        return True

    def walk(self,task,relative,depth=0,recursive=True):
        children=[]
        if depth>MAX_DEPTH:
            self.reader.diagnostic(relative,'directory_depth_cap');return children
        entries=self.reader.entries(relative); names={e.name for e in entries}
        # Do not index a prior synchronized export as a new experiment.
        if 'release-manifest.json' in names:return children
        if {'state.json','request.json','journal.jsonl'}<=names:
            try:self.confirmation(task,relative)
            except (ValueError,OSError,UnicodeError):self.reader.diagnostic(relative,'confirmation_parse_failed')
        phase_review=False
        if '/livephase/' in '/'+relative+'/' and {'review.json','telemetry.json','result.json'}&names:
            for name in ['review.json','telemetry.json','result.json']:
                if name not in names:continue
                try:
                    v,b,meta=self.reader.json(relative+'/'+name)
                    if self.phase(task,relative+'/'+name,v,b,meta):phase_review=True;break
                    if name!='result.json':self.reader.diagnostic(relative+'/'+name,'unsupported_phase_review_schema')
                except (ValueError,OSError,UnicodeError):self.reader.diagnostic(relative+'/'+name,'phase_review_parse_failed')
        for e in entries:
            rel=relative+'/'+e.name
            if e.is_dir(follow_symlinks=False):
                if recursive:self.walk(task,rel,depth+1)
                else:children.append((task,rel,depth+1))
            elif e.is_file(follow_symlinks=False):
                if e.name=='log.jsonl' and '/runs/' in rel:self.ui(task,rel)
                elif e.name in {'result.json','pipeline.json'} and not (phase_review and e.name=='result.json'):
                    try:
                        v,b,meta=self.reader.json(rel)
                        if e.name=='pipeline.json':self.model(task,rel,v,b,meta)
                        else:self.result(task,rel,v,b,meta)
                    except (ValueError,OSError,UnicodeError):self.reader.diagnostic(rel,'result_parse_failed')
        return children

    def build(self, readers=None):
        readers=readers or [self.reader]
        queues=[]
        for reader in readers:
            reader.share=len(readers)
            queue=deque();queues.append(queue)
            self.reader=reader
            for entry in self.reader.entries():
                if not entry.is_dir(follow_symlinks=False) or entry.name.startswith('_'):continue
                task={'id':opaque('task',self.reader.identity(entry.name)),
                      'name':label(self.reader.scope+'/'+entry.name),'source_root':self.reader.scope,'run_count':0,
                      'latest_run_id':None,'updated_at':None,'kinds':[]}
                self.tasks.append(task);queue.append((task,entry.name,0))
        # Fair breadth-first discovery: a cache-heavy root cannot starve another
        # registered producer. mtime is only scheduling metadata, not sample time.
        while any(queues):
            for reader,queue in zip(readers,queues):
                if queue:
                    self.reader=reader
                    task,relative,depth=queue.popleft()
                    queue.extend(self.walk(task,relative,depth,recursive=False))
        for r in self.runs:
            r['step_count']=len(r['steps']);r['artifact_count']=len(r['artifacts'])
        self.runs.sort(key=lambda r:(r['updated_at'] or r['source']['source_mtime'] or '',r['id']),reverse=True)
        for task in self.tasks:
            runs=[r for r in self.runs if r['task_id']==task['id']]
            task.update(run_count=len(runs),latest_run_id=runs[0]['id'] if runs else None,
                        updated_at=max([r['updated_at'] for r in runs if r['updated_at']],default=None),
                        kinds=sorted({r['kind'] for r in runs}))
        self.tasks.sort(key=lambda t:(t['updated_at'] or '',t['id']),reverse=True)
        fingerprint=digest(encode({'sources':sorted(self.reader.files.values(),key=lambda f:f['id']),
                                  'synchronized_at':self.time}))
        snapshot={'id':'snapshot_'+fingerprint[:24],'synchronized_at':self.time,'origin':'mac_filesystem_sync','live_device':False}
        return {'schema':'phonefarm_web/catalog@1','snapshot':snapshot,'tasks':self.tasks,'runs':self.runs,
                'artifacts':self.artifacts,'diagnostics':self.reader.diagnostics,'dashboard':None}


def sanitize_dashboard(v,catalog):
    """Preserve native structure; private endpoint data and unregistered URLs cannot escape."""
    def visit(value,key='',pointer=''):
        if key=='paths':return {'game_opt_loop':'[private-path]','phonefarm':'[private-path]',
                                'phonefarm_present':value.get('phonefarm_present') if isinstance(value,dict) else None,
                                'lock_root':'[private-path]'}
        if key in {'history','queue'}:return []
        if key=='last_touch':return {}
        if key in {'serial','token','boot_id','pid','holder','command','screenshot','goal','input','text'}:return None
        if key=='path':return None
        if isinstance(value,dict):
            if isinstance(value.get('name'),str) and re.search(r'(?i)serial|\buid\b|boot|token|\bpid\b|password|account',value['name']):
                value={**value,'value':None}
            return {k:visit(x,k,pointer+'/'+k) for k,x in value.items() if k in NATIVE_FIELDS}
        if isinstance(value,list):return [visit(x,'',pointer+'/'+str(i)) for i,x in enumerate(value[:2000])]
        if isinstance(value,str):
            if key=='id':return value if value in {'operator','graphics','sysparam','device','refbench'} else opaque('native',pointer+value)
            if key.endswith('sha256') and re.fullmatch('[0-9a-f]{64}',value):return value
            if key=='value' and re.fullmatch(r'[+−-]?\d{1,16}(?:\.\d{1,12})?',value):return value
            return label(value)
        if type(value) is float and not math.isfinite(value):return None
        return value
    result=visit(v)
    if not isinstance(result,dict):raise ValueError('dashboard_object_required')
    result['snapshot']=catalog['snapshot']
    result['fleet']=None
    result.setdefault('notices',[]).append({'level':'info','title':'同步历史',
        'body':'Mac synchronized snapshot; device live state not queried. Unregistered private evidence paths are omitted.','evidence':None})
    return result


def export(tasks_root,out,dashboard=None,extra_tasks_roots=None):
    out=Path(out)
    if out.exists():raise ValueError('output_must_be_new')
    reader=Reader(tasks_root);readers=[reader]
    try:
        extras=extra_tasks_roots or {}
        if not isinstance(extras,dict) or len(extras)>3 or 'phonefarm' in extras:raise ValueError('source root count/alias invalid')
        for alias,path in extras.items():readers.append(Reader(path,alias,reader.budget))
        build=Catalog(reader);catalog=build.build(readers)
        if dashboard:
            p=Path(dashboard)
            fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
            with os.fdopen(fd,'rb') as f:
                if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):raise ValueError('dashboard_not_regular')
                raw=f.read(FILE_CAP+1)
                if len(raw)>FILE_CAP:raise ValueError('dashboard_size_cap')
            catalog['dashboard']=sanitize_dashboard(strict_json(raw),catalog)
            catalog['dashboard_source_sha256']=digest(raw)
            catalog['snapshot']['id']='snapshot_'+digest(encode({
                'source_snapshot':catalog['snapshot']['id'],'dashboard_sha256':digest(raw)}))[:24]
        out.mkdir(parents=True)
        (out/'artifacts').mkdir()
        manifest=[]
        for relative,raw in {**build.public_files,'catalog.json':encode(catalog)}.items():
            (out/relative).write_bytes(raw)
            manifest.append({'path':relative,'bytes':len(raw),'sha256':digest(raw)})
        result={'schema':'phonefarm_web/release_manifest@1','complete':True,'snapshot':catalog['snapshot'],
                'files':manifest,'source_files':list(reader.files.values()),'phase_imports':build.phase_imports,
                'device_calls':0,'model_api_calls':0}
        (out/'release-manifest.json').write_bytes(encode(result))
        return catalog
    finally:
        for r in readers:r.close()


def main():
    p=argparse.ArgumentParser();g=p.add_mutually_exclusive_group(required=True)
    g.add_argument('--tasks-root');g.add_argument('--phonefarm-root');p.add_argument('--out',required=True)
    p.add_argument('--dashboard','--gol-dashboard',dest='dashboard')
    p.add_argument('--extra-tasks-root',action='append',default=[],metavar='ALIAS=ABSOLUTE_TASKS_ROOT');a=p.parse_args()
    extras={}
    for value in a.extra_tasks_root:
        alias,separator,path=value.partition('=')
        if not separator or alias in extras:raise ValueError('duplicate/missing source alias')
        extras[alias]=path
    result=export(a.tasks_root or str(Path(a.phonefarm_root)/'tasks'),a.out,a.dashboard,extras)
    print(json.dumps({'snapshot':result['snapshot'],'tasks':len(result['tasks']),'runs':len(result['runs']),
                      'artifacts':len(result['artifacts']),'diagnostics':len(result['diagnostics'])}))


if __name__=='__main__':main()
