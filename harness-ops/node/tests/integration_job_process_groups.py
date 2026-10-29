#!/usr/bin/env python3
"""macOS/Linux host acceptance: live group, departed leader, KILL, kernel fd release.

Run with python3 from any directory. Uses only a new temporary directory and
processes it creates. No node service, credentials, adb/hdc or phone access.
"""
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import job_runtime


def until(predicate):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.02)
    raise AssertionError('process fixture event was not observed')


def run():
    observations = []
    with tempfile.TemporaryDirectory(prefix='hgamey-group-observation-') as directory:
        root = Path(directory)
        child = '''
import fcntl, json, os, pathlib, signal, sys, time
root = pathlib.Path(sys.argv[1])
signal.signal(signal.SIGTERM, signal.SIG_IGN)
signal.alarm(30)
lock = open(root / 'kernel.lock', 'w')
fcntl.flock(lock, fcntl.LOCK_EX)
(root / 'ready.tmp').write_text(json.dumps({'pid': os.getpid(), 'pgid': os.getpgrp()}))
(root / 'ready.tmp').replace(root / 'ready.json')
while True: time.sleep(.05)
'''
        leader_code = ("import pathlib,subprocess,sys,time;root=pathlib.Path(sys.argv[1]);"
                       f"subprocess.Popen([sys.executable,'-c',{child!r},str(root)],close_fds=True);"
                       "deadline=time.monotonic()+20\n"
                       "while not (root/'exit-leader').exists() and time.monotonic()<deadline:time.sleep(.02)\n")
        leader = subprocess.Popen([sys.executable, '-c', leader_code, directory], start_new_session=True)
        try:
            until(lambda: (root / 'ready.json').exists())
            ready = json.loads((root / 'ready.json').read_text())
            assert ready['pgid'] == leader.pid
            with (root / 'kernel.lock').open('r') as contender:
                try:
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass
                else:
                    raise AssertionError('live child did not hold its kernel fd lock')
                for stage in ('leader_alive', 'leader_exited_child_alive'):
                    if stage.startswith('leader_exited'):
                        (root / 'exit-leader').touch()
                        leader.wait(timeout=5)
                    value = job_runtime._group_completion(leader.pid)
                    assert value['status'] == 'present', (stage, value)
                    observations.append({'stage': stage, 'group': value})
                sent = job_runtime.terminate_group(leader.pid, grace=.1)
                assert [item['name'] for item in sent] == ['SIGTERM', 'SIGKILL'], sent
                until(lambda: job_runtime._group_completion(leader.pid)['status'] == 'absent')
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                observations.append({'stage': 'after_kill', 'group': job_runtime._group_completion(leader.pid),
                                     'signals': sent, 'kernel_lock_acquired': True})
        finally:
            if job_runtime._group_completion(leader.pid)['status'] != 'absent':
                try:
                    os.killpg(leader.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            leader.wait(timeout=5)
    return {'passed': True, 'platform': sys.platform,
            'scope': 'isolated host process groups and kernel fd; no device IO', 'observations': observations}


if __name__ == '__main__':
    print(json.dumps(run(), indent=2))
