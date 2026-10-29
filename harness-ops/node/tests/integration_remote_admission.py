#!/usr/bin/env python3
"""Node remote -> real PF owners/fd locks -> fake adb. Invoked by Cargo CI."""
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from node_remote import Remote
from remote_owner import PFRemote

PF = str(Path(sys.argv.pop(1)).resolve())
A, B, ALIAS = "remote-a", "remote-b", "remote-a-wifi"


class Admission(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pf-node-remote-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        adb = self.root / "adb"
        adb.write_text('''#!/usr/bin/env python3
import json,os,re,sys
from pathlib import Path
args=sys.argv[1:]
if args[:1]==['devices']:
 print('List of devices attached\\nremote-a device model:Fixture\\nremote-b device model:Fixture\\nremote-a-wifi device model:Fixture'); sys.exit(0)
assert args[:1]==['-s'],args
with Path(os.environ['REMOTE_CALLS']).open('a') as out: out.write(json.dumps(args)+'\\n')
terminal=re.search(r'PF_GAME_DEPLOY_EXIT:[^:]+:\\d+:', ' '.join(args))
assert terminal,args
print('\\n'+terminal.group(0)+'0')
''')
        adb.chmod(0o755)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("PF_", "PHONEFARM_", "DEVLOCK_"))}
        env.update(ADB_BIN=str(adb), DEVLOCK_DIR=str(self.root / "locks"),
                   PHONEFARM_LEASE_DIR=str(self.root / "leases"),
                   PHONEFARM_STATE_DIR=str(self.root / "state"),
                   REMOTE_CALLS=str(self.root / "adb-calls"))
        patch = mock.patch.dict(os.environ, env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.env = env
        for revision, device, aliases in ((0, "a", [A, ALIAS]), (1, "b", [B])):
            self.pf("lease", "devices-set", "--request", "-", "--json", request={
                "expected_revision": revision, "device": {
                    "device_id": device, "label": device, "aliases": aliases, "enabled": True}})
        self.backend = PFRemote(PF, str(self.root), env, self.root / "remote")
        self.addCleanup(self.cleanup_owners)
        self.remote = Remote(lambda: self.pf("lease", "status", "--json"), adb=str(adb),
                             config_dir=str(self.root / "config"), reaper=False, backend=self.backend)
        token, _ = self.remote.operators.add("fixture")
        self.operator = self.remote.operators.verify(token)
        for serial in (A, B, ALIAS):
            self.remote.policy.enable(serial)
        policy = self.remote.policy.load()
        policy["aliases"] = [[A, ALIAS]]
        self.remote.policy.save(policy)

    def cleanup_owners(self):
        for process in list(self.backend.processes.values()):
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            if process.stdin:
                process.stdin.close()
        self.backend.close()
        self.backend.store.close()

    def pf(self, *args, request=None):
        done = subprocess.run([PF, *args], input=None if request is None else json.dumps(request),
                              text=True, capture_output=True, env=self.env, timeout=10)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return json.loads(done.stdout)

    def probe(self, serial=None):
        args = ["lease", *(["--serial", serial] if serial else []), "--timeout-ms", "0", "--", "/usr/bin/true"]
        return subprocess.run([PF, *args], capture_output=True, env=self.env, timeout=5).returncode == 0

    def acquire(self, serial, request):
        return self.remote.do(self.operator, "fixture", serial, "acquire", {"request_id": request})

    def process(self, request):
        session = next(s for s in self.remote.sessions.values() if s.ref["acquisition_id"] == request)
        return self.backend.processes[session.ref["id"]]

    def key(self, serial, session, request):
        result = self.remote.do(self.operator, "fixture", serial, "key", {
            "session_id": session["session_id"], "request_id": request, "key": "home"})
        deadline = time.monotonic() + 5
        while result["status"] in ("accepted", "started"):
            self.assertLess(time.monotonic(), deadline, result)
            time.sleep(.02)
            result = self.remote.query(self.operator, serial, session_id=session["session_id"], request_id=request)
        self.assertEqual(result["status"], "completed", result)
        return result

    def test_device_and_global_locks_remain_authoritative(self):
        first = self.acquire(A, "a")
        second = self.acquire(B, "b")
        self.assertEqual((first["phase"], second["phase"]), ("ready", "ready"))
        receipt = self.key(A, first, "a-key")
        self.key(B, second, "b-key")
        calls = (self.root / "adb-calls").read_bytes()
        self.assertEqual({json.loads(line)[1] for line in calls.splitlines()}, {A, B})
        self.assertFalse(self.probe(A))
        self.assertFalse(self.probe(ALIAS))
        self.assertFalse(self.probe())
        self.assertTrue(self.probe("unrelated-phone"))

        # The PF owner, not the node's session table, waits for the same device.
        waiting = self.acquire(ALIAS, "alias-wait")
        self.assertEqual(waiting["phase"], "starting", waiting)
        waiter = self.process("alias-wait")
        self.assertIsNone(waiter.poll())
        self.assertTrue(self.acquire(ALIAS, "alias-wait")["reused"])
        devices = {d["serial"]: d for d in self.remote.devices()["devices"]}
        self.assertEqual(devices[A]["holder"]["session_id"], first["session_id"])
        self.assertEqual(devices[B]["holder"]["session_id"], second["session_id"])
        self.assertEqual((self.root / "adb-calls").read_bytes(), calls)
        waiter.terminate()
        waiter.wait(timeout=5)
        self.assertFalse(self.probe(A), "cancelling a waiter must not release the original owner")

        owner = self.process("a")
        owner.terminate()
        owner.wait(timeout=5)
        self.assertTrue(self.probe(A), "owner exit must release its fd")
        self.assertEqual(self.remote.query(self.operator, A, session_id=first["session_id"],
                                          request_id="a-key"), receipt)
        replacement = self.acquire(A, "new-a")
        self.assertEqual(replacement["phase"], "ready", replacement)
        self.key(A, replacement, "new-a-key")
        devices = {d["serial"]: d for d in self.remote.devices()["devices"]}
        self.assertEqual(devices[A]["holder"]["session_id"], replacement["session_id"])

        for request in ("new-a", "b"):
            process = self.process(request)
            process.terminate()
            process.wait(timeout=5)
        self.assertTrue(self.probe())
        # A true global holder still blocks either phone. Synchronize by stdout.
        holder = subprocess.Popen([PF, "lease", "--", sys.executable, "-c",
            "import sys; print('held',flush=True); sys.stdin.read()"], env=self.env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.assertTrue(select.select([holder.stdout], [], [], 5)[0])
            self.assertEqual(holder.stdout.readline(), b"held\n")
            waiting = self.acquire(B, "global-wait")
            self.assertEqual(waiting["phase"], "starting", waiting)
            waiter = self.process("global-wait")
            waiter.send_signal(signal.SIGTERM)
            waiter.wait(timeout=5)
            self.assertFalse(self.probe(B))
        finally:
            holder.communicate(timeout=5)
        self.assertTrue(self.probe(B))


if __name__ == "__main__":
    unittest.main()
