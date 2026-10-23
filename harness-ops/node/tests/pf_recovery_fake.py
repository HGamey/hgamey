"""Battery/idle additions reused from the frozen control power host fixture.

Only Android responses are synthetic. The shared base checks the actual PF
lease-command PID/PGID and durable pending records. No shell/ADB fallback.
"""

SAMPLING_EXTENSION = r'''
import re
parent_path = Path(context) / 'eval-session.json' if context else None
parent = json.loads(parent_path.read_text()) if parent_path and parent_path.is_file() else None
entry['parent_pending'] = parent.get('pending') if parent else None
entry.update(uid=os.geteuid(), gid=os.getegid(), job_id=os.environ.get('PF_JOB_ID'),
             recovery_out=os.environ.get('PF_RECOVERY_OUT'), shared_gid=os.environ.get('PHONEFARM_SHARED_GID'))
BOOT = '11111111-2222-3333-4444-555555555555'
CHARGE_NODE = '/sys/class/power_supply/battery/input_suspend'
entry['charging'] = parent.get('charging') if parent else None


def on_battery():
    return state.get('initially_discharging', False) or state['files'][CHARGE_NODE] == '1\n'



def parent_pending():
    pending()
    entry['parent_pending_ok'] = (parent is not None and parent.get('pending') is not None
                                  and parent.get('schema') == 'phonefarm/gray-control-resources@3'
                                  and session.get('control_resources') == {
                                      'path': 'eval-session.json', 'schema': 'phonefarm/gray-control-resources@3'})
    if not entry['parent_pending_ok']:
        finish(99, error='parent resource mutation missing durable bound parent pending\n')


def reply(kind, output='', mutates=False):
    entry['kind'] = kind
    if mutates:
        parent_pending()
    save()
    finish(0, output)


def trace_text(seconds, start):
    rows = [f'#loop_v1_meta capture_start_uptime={start} capture_end_uptime={start+seconds} duration_req={seconds}',
            '#loop_v1_meta gpu_model=SyntheticAdreno ddr_cur_khz=1000000 ddr_max_khz=1000000',
            '# phonefarm trace_mode=instance buffer_size_kb=16384 trace_clock=boot',
            '# phonefarm trace_event=adreno_cmdbatch_submitted status=enabled',
            '# phonefarm trace_event=adreno_cmdbatch_retired status=enabled',
            '# phonefarm cpu0 overrun: 0']
    t = start + .05
    for i in range(max(12, int((seconds-.2)/.016)*2)):
        t += .005 if i % 2 == 0 else .011
        rows.append(f'  UnityGfxDeviceW-123 [001] ..... {t:.6f}: adreno_cmdbatch_submitted: ctx=45 ts={i} inflight=1')
        rows.append(f'  kgsl-124 [001] ..... {t+.001:.6f}: adreno_cmdbatch_retired: ctx=45 ts={i} active=19200')
    return '\n'.join(rows) + '\n'


def env_text(seconds, start):
    duration = seconds + 10
    status = 'Discharging' if on_battery() else 'Charging'
    current = -1000000 if on_battery() else 1000000
    rows = [f'#sample_env v1 dur={duration} interval=2', '#battery_status='+status,
            '#battery_capacity=80', '#charge_suspended=no', '#charge_mode=', '#fan_state=fan_enable=1']
    for i in range(0, duration, 2):
        rows.extend([f'ENV {start+i} 4000000 {current} NA 0 0 cpu-0 30000', f'GPU {start+i} 0 600000000'])
    rows.extend(['#battery_status_after='+status, '#sample_env done'])
    return '\n'.join(rows) + '\n'


if len(args) == 3 and args[0] == 'push' and not args[2].endswith('/libVkLayer_refknobs.so'):
    parent_pending()
    path = args[2]
    directory = str(PurePosixPath(path).parent)
    assert parent and directory == parent['remote']
    assert state.get('dir_owners', {}).get(directory) == 'shell'
    assert parent['pending'] in ('eval_push_tool', 'control_push_workload'), parent['pending']
    if PurePosixPath(path).name.startswith('workload-'):
        round = parent['rounds'][-1]
        assert round['deployment'] == 'recorded' and round['workload'] == PurePosixPath(path).name
        assert path == directory + '/workload-' + round['label'] + '.sh'
        assert all(step['action'] == 'sleep' for step in round['input_plan']['steps'])
    state['files'][path] = Path(args[1]).read_bytes().decode('latin1')
    reply('sampling-tool-push', 'synthetic owned tool pushed\n', True)

if len(args) == 2 and args[0] == 'shell':
    shell = args[1]
    words = shlex.split(shell)
    as_root = words[:2] == ['su', '-c'] and len(words) == 3
    if as_root:
        shell = words[2]
        words = shlex.split(shell)
    if shell.startswith('test "$(cat /proc/sys/kernel/random/boot_id)" = '):
        guard, command = shell.split(' && ', 1)
        assert shlex.split(guard) == ['test', '$(cat /proc/sys/kernel/random/boot_id)', '=', session['boot_id']]
        words = shlex.split(command)
        assert len(words) == 3 and words[:2] == ['su', '-c']
        shell = words[2]
        words = shlex.split(shell)
        as_root = True
        entry['charge_boot_guard_verified'] = True
    entry.update(shell=shell, words=words, as_root=as_root)
    if shell.startswith('s=$(cat /sys/class/power_supply/battery/status) && c=$(cat '):
        assert as_root
        reply('charge-battery-read', 'Discharging\n-1000000\n80\n' if on_battery() else 'Charging\n1000000\n80\n')
    if words == ['cat', CHARGE_NODE]:
        assert entry.get('charge_boot_guard_verified') is True
        if (device / 'restore-mismatch').exists() and state.get('charge_restore_written'):
            entry['kind'] = 'known-charge-readback-mismatch'
            finish(0, '1\n')
        reply('charge-control-read', state['files'][CHARGE_NODE])
    if len(words) == 4 and words[:1] == ['echo'] and words[2:] == ['>', CHARGE_NODE]:
        parent_pending()
        assert entry.get('charge_boot_guard_verified') is True and parent['pending'] == 'charge_control'
        value = words[1]
        assert value in ('0', '1') and not state.get('initially_discharging', False)
        charge, binding = parent['charging'], parent['charging']['binding']
        assert charge['stage'] == ('recorded' if value == '1' else 'restoring')
        assert binding['node'] == CHARGE_NODE and binding['original'] == '0' and binding['experiment'] == '1'
        assert binding['serial'] == serial and binding['token'] == session['token'] and binding['session'] == context
        assert binding['record_path'] == charge['record_path']
        raw = Path(binding['record_path']).read_bytes()
        header, body = raw.split(b'\n', 1)
        assert header == b'phonefarm-recovery@1 ' + str(len(body)).encode()
        assert body == (CHARGE_NODE+'\n0\n').encode() and Path(binding['record_evidence']).read_bytes() == raw
        entry.update(charge_binding_verified=True, value=value,
                     charge_record_uid=Path(binding['record_path']).stat().st_uid,
                     charge_record_gid=Path(binding['record_path']).stat().st_gid,
                     charge_record_mode=Path(binding['record_path']).stat().st_mode & 0o777)
        if value == '0':
            assert state.get('groups_verified') is True, 'restoring charge before current owned groups proof'
            state['charge_restore_written'] = True
        state['files'][CHARGE_NODE] = value+'\n'
        state.setdefault('charge_writes', []).append(value)
        reply('charge-control-write', mutates=True)

    if words == ['id', '-u']:
        reply('sampling-root-capability', '0\n')
    if shell.startswith('if [ -r /sys/kernel/tracing/events/kgsl/'):
        reply('sampling-trace-capability', 'AVAILABLE\n')
    if shell.startswith('set -eu; command -v setsid;'):
        parent_pending()
        remote = re.search(r"mkdir '([^']+)'", shell).group(1)
        owner = re.search(r"printf '%s\\n' '([^']+)'", shell).group(1)
        assert remote == parent['remote'] and owner == session['token'] + ' ' + BOOT
        assert remote not in state['dirs']
        state['dirs'].append(remote)
        state.setdefault('dir_owners', {})[remote] = 'shell'
        state['files'][remote + '/owner'] = owner + '\n'
        state.update(sampling_remote=remote, sampling_owner=owner, sampling_closed=False)
        reply('sampling-tools-create', '/system/bin/setsid\n', True)
    if words[:2] == ['setsid', 'sh'] and words[2].endswith('/worker.sh'):
        _, _, script, remote, token, boot, mode, run, *extra = words
        assert remote == state['sampling_remote'] and token + ' ' + boot == state['sampling_owner'] and boot == BOOT
        assert script == remote + '/worker.sh'
        if mode == 'gray_control_sample':
            parent_pending()
            assert len(extra) == 3 and extra[1] == parent['trace'] and extra[2] == '0'
            seconds = int(extra[0])
            round = parent['rounds'][-1]
            plan = round['input_plan']
            assert round['label'] == run and round['seconds'] == seconds and round['qualify'] is False
            assert round['deployment'] == 'ready' and round['workload'] == 'workload-' + run + '.sh'
            assert plan['seconds'] == seconds and plan['total_ms'] == (seconds+10)*1000
            assert all(step['action'] == 'sleep' for step in plan['steps'])
            assert remote + '/' + round['workload'] in state['files']
            assert is_running() and not state['sampling_closed']
            directory = remote + '/' + run
            assert directory not in state['dirs']
            state['dirs'].append(directory)
            start = 1000 + 100 * len(state.get('sampling_rounds', []))
            end = start + seconds + 10
            def window(child, start, end, child_start=None, child_end=None):
                return json.dumps({'schema': 'phonefarm/gray-child-time@1', 'child': child,
                    'start': start, 'end': end, 'child_start': start if child_start is None else child_start,
                    'child_end': end if child_end is None else child_end, 'exit_code': 0}) + '\n'
            files = {'workload_exit.txt': '0\n', 'trace_exit.txt': '0\n', 'sampler_exit.txt': '0\n',
                'quiescence.txt': 'OWNED_GROUPS_ABSENT\n', 'workload.log': 'synthetic sleep-only workload complete\n',
                'trace.log': 'synthetic trace child complete\n', 'sampler.log': env_text(seconds, start),
                'trace.txt': trace_text(seconds, start+6),
                'workload-time.json': window('workload', start, end),
                'trace-time.json': window('trace', start+6, start+6+seconds, start-1, start+6+seconds+.1),
                'sampler-time.json': window('sampler', start-.1, end-.1),
                'round-kind.txt': 'gray_control_v1\n', 'permit-workload': 'synthetic durable workload permit\n',
                'permit-trace': 'synthetic durable trace permit\n', 'permit-sampler': 'synthetic durable sampler permit\n',
                'input-complete.json': json.dumps({'schema': 'phonefarm/gray-input-complete@1',
                    'token': token, 'boot_id': boot, 'run': run, 'steps': len(plan['steps']),
                    'declared_ms': plan['total_ms'], 'start': start, 'end': end}) + '\n'}
            for name, value in files.items():
                state['files'][directory + '/' + name] = value
            state['files'][parent['trace'] + '/tracing_on'] = '0\n'
            state.setdefault('sampling_rounds', []).append({'label': run, 'seconds': seconds, 'plan': plan,
                                                           'natural_complete': True, 'groups_absent': True})
            entry.update(run=run, seconds=seconds, simulated_child_groups=3, natural_complete=True)
            reply('control-sample', 'GRAY_SCRIPT_SAMPLE_COMPLETE\n', True)
        if mode == 'verify_gray_idle':
            assert not state['sampling_closed'] and state.get('sampling_rounds')
            assert parent['pending'] is None
            reply('sampling-idle-verify', 'GRAY_IDLE_QUIESCENT\n')
        if mode == 'verify_gray_groups':
            assert parent['pending'] is None
            assert all(item['groups_absent'] for item in state.get('sampling_rounds', []))
            state['groups_verified'] = True
            reply('sampling-groups-verify', 'GRAY_GROUPS_QUIESCENT\n')
        if mode == 'reconcile_bench':
            parent_pending()
            assert all(round['natural_complete'] for round in state.get('sampling_rounds', []))
            state['sampling_closed'] = True
            state['dirs'].append(remote + '/session.closed')
            state['files'][remote + '/session.closed/owner'] = token + ' ' + boot + ' ' + run + '\n'
            reply('sampling-close', 'BENCH_RECONCILE_COMPLETE\n', True)
        if mode == 'verify_bench':
            assert state['sampling_closed']
            reply('sampling-closed-verify', 'BENCH_CLOSED_QUIESCENT\n')
        finish(99, error='unexpected sampling worker mode ' + mode + '\n')
    if shell.startswith('test "$(cat ') and parent:
        assert state['sampling_owner'] in shell and BOOT in shell
        if ' && test ! -e ' in shell and ' && mkdir ' in shell:
            parent_pending()
            trace = shlex.split(shell.rsplit(' && mkdir ', 1)[1])[0]
            assert trace == parent['trace'] and trace not in state['dirs']
            state['dirs'].append(trace)
            state['files'][trace + '/tracing_on'] = '0\n'
            reply('sampling-trace-create', mutates=True)
        if ' && cat ' in shell:
            path = shlex.split(shell.rsplit(' && cat ', 1)[1])[0]
            assert path in state['files'], path
            reply('sampling-evidence-read', state['files'][path])
        if '; then echo CLOSED; else echo OPEN; fi' in shell:
            reply('sampling-closure-read', ('CLOSED' if state['sampling_closed'] else 'OPEN') + '\n')
        if " && printf '0\\n' > " in shell:
            parent_pending()
            path = shlex.split(shell.rsplit(' > ', 1)[1])[0]
            assert path == parent['trace'] + '/tracing_on'
            state['files'][path] = '0\n'
            reply('sampling-trace-stop', mutates=True)
        if ' && rmdir ' in shell:
            parent_pending()
            path = shlex.split(shell.rsplit(' && rmdir ', 1)[1])[0]
            assert path == parent['trace'] and state['files'][path + '/tracing_on'] == '0\n'
            state['dirs'].remove(path)
            for name in list(state['files']):
                if name.startswith(path + '/'):
                    del state['files'][name]
            reply('sampling-trace-remove', mutates=True)
    if parent and words[:1] == ['cat'] and words[-1] == parent['trace'] + '/tracing_on':
        reply('sampling-trace-stopped-read', state['files'][words[-1]])

'''


def compose(base):
    marker = "if args==['exec-out','screencap','-p']:\n"
    prefix, found, suffix = base.partition(marker)
    if not found or marker in suffix:
        raise ValueError("shared fake dispatch anchor changed")
    return prefix + SAMPLING_EXTENSION + marker + suffix
