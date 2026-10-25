from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from unittest import TestCase, mock

import test_node_read as read_fixture
from node_session import SessionError
from job_dispatch import request_fingerprint
from runtime_identity import is_gray_session


SESSION_ARGV = ["gray-layer", "session", "--mode", "probe", "--package", "com.example.fixture",
                "--serial", "fake-device", "--out", "@out", "--max-seconds", "30"]


RECOVERY_HELPER = r'''
import json, os, pathlib, sys, time
a = sys.argv[1:]
root = pathlib.Path(__file__).parent
def save(path, value):
    path.write_text(json.dumps(value))
if a == ['runtime-info', '--json']:
    print(json.dumps({'schema':'phonefarm/runtime-identity@1','program':'phonefarm',
      'package_version':'host-fixture','build':{'source_revision':None,'source_dirty':False,
      'target':'host-fixture','profile':'test'},
      'supported_protocols':['phonefarm/runtime-identity@1','phonefarm/gray-control-gateway@1']}))
    sys.exit(0)
if a[:2] == ['gray-layer', 'session-status']:
    out = pathlib.Path(a[a.index('--out') + 1])
    print(json.dumps(dict(json.loads((out / 'session-control.json').read_text()), live_owner=False)))
    sys.exit(0)
if a[:2] not in (['gray-layer', 'session'], ['gray-layer', 'session-recover']):
    raise RuntimeError('unexpected fixture command: ' + repr(a))
out = pathlib.Path(os.environ['PF_JOB_OUT'])
jid = os.environ['PF_JOB_ID']
initial = a[1] == 'session'
source = out if initial else pathlib.Path(a[a.index('--session') + 1]).parent
session = source / 'gray-session-fixture'
if initial:
    session.mkdir()
    (source / 'gray-control').mkdir()
    state = {'schema':'phonefarm/gray-control@1', 'producer':'phonefarm', 'job_id':jid,
      'session_id':'gray-session-fixture', 'session_path':session.name, 'serial':'fake-device',
      'phase':'recovery_required', 'accepting':False, 'allowed_actions':[], 'latest_image':None,
      'controller':None, 'recovery':{'status':'required','session_path':session.name,'reports':[]}}
    attempt = 0
else:
    state = json.loads((source / 'session-control.json').read_text())
    count = source / 'recovery-count'
    attempt = int(count.read_text()) + 1 if count.exists() else 1
    count.write_text(str(attempt))
    if pathlib.Path(os.environ['PF_RECOVERY_OUT']).resolve() != source.resolve():
        raise RuntimeError('recovery output source drifted to the queue parent')
save(out / 'observed.json', {'job_id':jid, 'source':str(source), 'attempt':attempt,
     'recovery_out':os.environ.get('PF_RECOVERY_OUT'), 'pid':os.getpid()})
print('synthetic worker attempt=' + str(attempt), flush=True)
if attempt == 2:
    (root / 'r2-started').write_text(jid)
    deadline = time.monotonic() + 8
    while not (root / 'release-r2').exists():
        if time.monotonic() > deadline:
            raise RuntimeError('bounded fixture gate expired')
        time.sleep(.02)
done = attempt == 2
report = {'attempt':attempt, 'job_id':jid, 'evidence_origin':'synthetic',
          'status':'verified' if done else 'required', 'scope':'host_fixture_only'}
report_path = session / ('report-' + jid + '.json')
save(report_path, report)
state['recovery']['reports'].append(str(report_path.relative_to(source)))
state['recovery']['status'] = 'reported_complete' if done else 'required'
state['phase'] = 'closed' if done else 'recovery_required'
save(source / 'session-control.json', state)
prior_path = session / 'last-fixture-lease-job'
prior = prior_path.read_text() if prior_path.exists() else None
receipt = {'schema':'phonefarm/job-lease@1','job_id':jid,'lease_id':'fixture-' + jid,
 'serial':'fake-device','label':'fixture','state':'released' if done else 'retained',
 'updated_at':'2026-10-25T00:00:00Z',
 'recovery_of':None if prior is None else {'job_id':prior,'lease_id':'fixture-' + prior},
 'recovery':{'status':'reported_complete' if done else 'required','reason':'synthetic fixture',
             'context':str(session),'scope':'host_fixture_only','evidence_origin':'synthetic',
             'evidence':[str(report_path)]}}
save(pathlib.Path(os.environ['PF_JOB_LEASE_DIR']) / ('fixture-' + jid + '.json'), receipt)
prior_path.write_text(jid)
print(json.dumps(dict(state, live_owner=False)), flush=True)
sys.exit(0 if done else 2)
'''


class JobSessionTests(TestCase):
    setUp = read_fixture.NodeReadTests.setUp
    tearDown = read_fixture.NodeReadTests.tearDown
    fixture = read_fixture.NodeReadTests.fixture
    request = read_fixture.NodeReadTests.request
    get_json = read_fixture.NodeReadTests.get_json

    def prepare(self, number=1):
        jid = self.fixture(number, bin="phonefarm", argv=SESSION_ARGV)
        out = Path(self.jobs.path(jid, "out"))
        (out / "gray-control").mkdir()
        (out / "gray-session-fixture").mkdir()
        self.value = {"schema": "phonefarm/gray-control@1", "producer": "phonefarm", "job_id": jid,
                      "session_id": "fixture", "serial": "fake-device", "session_path": "gray-session-fixture",
                      "phase": "ready", "accepting": True, "controller": {"actor_id": "agent", "epoch": 1},
                      "allowed_actions": ["capture", "stop"], "latest_image": None,
                      "recovery": {"status": "not_reported", "session_path": "gray-session-fixture", "reports": []}}
        (out / "session-control.json").write_text(json.dumps(self.value))
        helper = self.root / "phonefarm-fake"
        helper.write_text("#!" + sys.executable + "\n" + '''import json, pathlib, sys
args = sys.argv[1:]
if args == ['runtime-info', '--json']:
    print(pathlib.Path(__file__).with_suffix('.identity.json').read_text())
    sys.exit(0)
out = pathlib.Path(args[args.index('--out') + 1])
with (out / 'helper-calls').open('a') as f: f.write(json.dumps(args) + '\\n')
d = json.loads((out / 'session-control.json').read_text())
if args[1] == 'session-action':
    request = json.load(sys.stdin)
    (out / 'helper-input.json').write_text(json.dumps(request))
    if request['action'] == 'closed':
        print(json.dumps({'schema':'phonefarm/gray-control-error@1','code':'owner_unavailable','error':'fixture owner gone'}))
        sys.exit(2)
    rid = request['request_id']
elif '--request-id' in args: rid = args[args.index('--request-id') + 1]
else:
    print(json.dumps(dict(d, live_owner=d.get('live_owner', True))))
    sys.exit(0)
print(json.dumps({'schema':'phonefarm/gray-control-action-result@1','session_id':d['session_id'],'request_id':rid,'status':'accepted','sequence':1}))
''')
        helper.chmod(0o755)
        self.jobs.bins["phonefarm"] = (str(helper), str(self.root))
        action = {"schema": "phonefarm/gray-control-action@1", "request_id": "capture-1", "session_id": "fixture",
                  "actor_id": "agent", "control_epoch": 1, "action": "capture", "params": {}}
        return jid, out, action

    def test_configured_gateway_admission_refresh_and_old_session_continuity(self):
        old, out, action = self.prepare()
        helper = Path(self.jobs.bins["phonefarm"][0])
        identity = {"schema": "phonefarm/runtime-identity@1", "program": "phonefarm",
                    "package_version": "host-fixture", "build": {"source_revision": None,
                    "source_dirty": False, "target": "host-fixture", "profile": "test"},
                    "supported_protocols": ["phonefarm/runtime-identity@1"]}
        helper.with_suffix(".identity.json").write_text(json.dumps(identity))
        self.jobs.programs.refresh()
        # Only existing host helpers execute. New producer jobs remain queued
        # until an explicit tick exercises the real unstarted finalization path.
        self.jobs.dispatch.start = lambda: None
        spec = {"bin": "phonefarm", "argv": SESSION_ARGV}
        pin = {"X-Expected-Node-ID": self.node.node_id}
        lease = {"schema_version": 1, "status": "clear", "bridge_held": False, "entries": [], "error": None}

        def cli(*args, ok=True):
            env = {**os.environ, "PF_NODE_TOKEN": self.admin, "HOME": str(self.root)}
            for name in ("PF_NODE_TOKEN_FILE", "PF_NODE_URL", "PF_NODE_ID"):
                env.pop(name, None)
            result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "pf-node"),
                "--url", "http://%s:%s" % self.server.server_address, "--node-id", self.node.node_id, *args],
                env=env, capture_output=True, text=True, timeout=10)
            if ok:
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            else:
                self.assertNotEqual(result.returncode, 0, result.stderr + result.stdout)
            return result

        def post(key, value=spec, body=b""):
            code, _, raw = self.request("/jobs", method="POST", token=self.admin, body=body,
                headers={**pin, "X-Request-ID": key, "X-Job": json.dumps(value),
                         "Content-Type": "application/octet-stream"})
            return code, json.loads(raw)

        def rejected(key, code, error, value=spec):
            with mock.patch.object(self.server.RequestHandlerClass, "_body",
                                   side_effect=AssertionError("admission must precede reading the upload")):
                status, receipt = post(key, value, b"unread upload")
            self.assertEqual((status, receipt["code"]), (code, error), receipt)
            self.assertEqual(receipt["program"], "phonefarm")
            self.assertFalse(receipt["command_started"])
            self.assertIsNone(self.jobs.dispatch.queue.by_request(key))
            return receipt

        def unstarted(jid, code):
            self.jobs.dispatch.tick()
            status = self.get_json("/jobs/" + jid)
            self.assertEqual((status["state"], status["outcome"], status["queue"]["state"]),
                             ("finished", "failed", "done"))
            self.assertIn(code, status["note"])
            self.assertIsNone(status["exit"])
            self.assertEqual(status["execution"]["owned_process_group"]["status"], "not_started")
            self.assertFalse(Path(self.jobs.path(jid, "launch.json")).exists())
            self.assertEqual(self.jobs.dispatch.queue.get(jid)["host_capacity_release"]["basis"], "not_started")

        with mock.patch.object(self.jobs, "_prepare", wraps=self.jobs._prepare) as prepare, \
                mock.patch.object(self.jobs.leases, "snapshot", return_value=lease), \
                mock.patch("job_runtime.launch", side_effect=AssertionError("new producer must never launch")) as launch:
            node = self.get_json("/node")
            self.assertIn("node.protocol-compat-v1", node["capabilities"])
            missing = rejected("gateway-missing", 409, "program_protocol_unsupported")
            self.assertEqual(missing["compatibility"]["missing"], ["phonefarm/gray-control-gateway@1"])
            rejected("gateway-prefixed-exe", 400, "program_override_unsupported", {
                **spec, "argv": ["gray-layer", "--lock-wait", "0", *SESSION_ARGV[1:]],
                "exe": "@in/replacement"})
            self.assertFalse(is_gray_session({"bin": "phonefarm",
                "argv": ["gray-layer", "--lock-label", "session", "eval"]}),
                "a queue flag value must not be mistaken for the PF mode")
            refused = cli("--detach", "phonefarm", *SESSION_ARGV, ok=False)
            self.assertIn("program_protocol_unsupported", refused.stderr)
            prepare.assert_not_called()
            self.assertEqual(self.jobs.dispatch.queue.rows(), [])

            identity["supported_protocols"].append("phonefarm/gray-control-gateway@1")
            helper.with_suffix(".identity.json").write_text(json.dumps(identity))
            # Changing the declared value alone does not silently refresh the cache.
            self.assertEqual(self.get_json("/node")["execution"]["programs"]["phonefarm"]
                             ["compatibility"]["gray_control"]["status"], "incompatible")
            refreshed = json.loads(cli("node", "--refresh-runtime").stdout)
            self.assertEqual(refreshed["execution"]["programs"]["phonefarm"]
                             ["compatibility"]["gray_control"]["status"], "compatible")
            submitted = cli("--request-id", "gateway-original", "--detach", "phonefarm", *SESSION_ARGV)
            jid = submitted.stdout.strip()
            original = self.jobs.dispatch.queue.get(jid)
            self.assertEqual(original["state"], "queued")
            prepared = Path(self.jobs.path(jid, "prepared.json")).read_bytes()
            self.assertEqual(prepare.call_count, 1)

            replacement = helper.with_suffix(".replacement")
            replacement.write_bytes(helper.read_bytes() + b"\n# replaced at the same configured path\n")
            replacement.chmod(0o755)
            os.replace(replacement, helper)
            stale = self.get_json("/node")["execution"]["programs"]["phonefarm"]
            self.assertEqual((stale["state"], stale["compatibility"]["gray_control"]["status"]),
                             ("stale", "unknown"))
            rejected("gateway-stale", 503, "program_identity_unavailable")
            same = cli("--request-id", "gateway-original", "--detach", "phonefarm", *SESSION_ARGV)
            self.assertEqual(same.stdout.strip(), jid)
            changed = cli("--request-id", "gateway-original", "--detach", "phonefarm",
                          *SESSION_ARGV[:-1], "31", ok=False)
            self.assertIn("409", changed.stderr)
            self.assertEqual(self.jobs.dispatch.queue.get(jid)["fingerprint"], original["fingerprint"])
            self.assertEqual(Path(self.jobs.path(jid, "prepared.json")).read_bytes(), prepared)
            self.assertEqual(prepare.call_count, 1, "old IDs must not reprepare, even when admission is stale")
            unstarted(jid, "program_identity_unavailable")

            cli("node", "--refresh-runtime")
            code, queued = post("gateway-config-change")
            self.assertEqual(code, 200, queued)
            other = self.root / "other-phonefarm"
            other.write_bytes(helper.read_bytes())
            other.chmod(0o755)
            other.with_suffix(".identity.json").write_text(json.dumps(identity))
            self.jobs.bins["phonefarm"] = (str(other), str(self.root))
            cli("node", "--refresh-runtime")
            unstarted(queued["id"], "program_configuration_changed")

            identity["supported_protocols"].remove("phonefarm/gray-control-gateway@1")
            other.with_suffix(".identity.json").write_text(json.dumps(identity))
            cli("node", "--refresh-runtime")
            rejected("gateway-exe", 400, "program_override_unsupported", {**spec, "exe": "@in/replacement"})
            self.assertTrue(self.get_json(f"/jobs/{old}/session")["live_owner"])
            receipt = json.loads(cli("session-status", old, "--request-id", action["request_id"]).stdout)
            self.assertEqual(receipt["request_id"], action["request_id"])
            stop = {**action, "request_id": "stop-old-owner", "action": "stop"}
            stop_file = self.root / "stop.json"
            stop_file.write_text(json.dumps(stop))
            stopped = json.loads(cli("session-action", old, "--request", str(stop_file)).stdout)
            self.assertEqual((stopped["request_id"], stopped["status"]), (stop["request_id"], "accepted"))
            self.assertEqual(json.loads((out / "helper-input.json").read_text()), stop)
            self.assertEqual(prepare.call_count, 2)
            self.assertEqual(len(self.jobs.dispatch.queue.rows()), 2)
            launch.assert_not_called()

    def test_session_recovery_derives_source_and_reuses_request_after_close(self):
        self.jobs.dispatch.start = lambda: None
        # Preserve the normalized old source shape; the recovery resolver must
        # not accept a bin-only placeholder or require new default spec fields.
        source_spec = {"bin": "phonefarm", "argv": SESSION_ARGV, "label": None,
                       "resources": ["phone:fake-device"]}
        row, _ = self.jobs.dispatch.queue.reserve("source", request_fingerprint(source_spec, b""),
                                                 source_spec, source_spec["resources"])
        jid, out, _ = self.prepare(int(row["job_id"], 16))
        self.jobs.dispatch.queue.set_state(jid, "blocked")
        meta = self.jobs._read_meta(jid)
        meta.update(state="finished", phase="finished", outcome="failed", exit=2,
                    execution={"kind": "supervised-v1", "process_management": {"policy": "contained"},
                               "owned_cgroup": {"status": "absent"}})
        self.jobs._write_meta(jid, meta)
        out.chmod(0o700)
        self.value.update(phase="recovery_required", live_owner=False)
        (out / "session-control.json").write_text(json.dumps(self.value))
        path = f"/jobs/{jid}/session/recover"
        post = dict(method="POST", body=b"{}", headers={"Content-Type": "application/json", "X-Request-ID": "recover-1"})
        self.assertIn("jobs.session-recovery-v1", self.get_json("/node")["capabilities"])
        self.assertEqual(self.request(path, **post)[0], 401)
        auth = {"Cookie": f"{read_fixture.dash.COOKIE}={self.board._cookie_val(self.pw)}", "X-HG": "1", **post["headers"]}
        self.assertEqual(self.request("/dash/api" + path, **{**post, "headers": auth})[0], 403)
        self.assertEqual(self.request(path, token=self.admin, **{**post, "headers": {"Content-Type": "application/json"}})[0], 400)
        self.assertEqual(self.request(path, token=self.admin, **{**post, "body": b'{"serial":"other"}'})[0], 400)
        for change in ("domain", "handback", "owner", "storage", "lost"):
            with self.subTest(change=change):
                if change == "domain":
                    meta["execution"]["owned_cgroup"]["status"] = "unknown"
                elif change == "handback":
                    out.chmod(0o2770)
                elif change == "owner":
                    self.value["live_owner"] = True
                elif change == "storage":
                    self.jobs.dispatch.queue.set_state(jid, "blocked", {"kind": "durability"})
                else:
                    meta["phase"] = "starting"
                    meta["execution"]["launch_deadline"] = 0
                self.jobs._write_meta(jid, meta)
                (out / "session-control.json").write_text(json.dumps(self.value))
                self.assertEqual(self.request(path, token=self.admin, **post)[0], 409)
                meta["execution"]["owned_cgroup"]["status"] = "absent"
                meta["phase"] = "finished"
                out.chmod(0o700)
                self.value["live_owner"] = False
                self.jobs.dispatch.queue.set_state(jid, "blocked")
        self.jobs._write_meta(jid, meta)
        (out / "session-control.json").write_text(json.dumps(self.value))
        status, _, raw = self.request(path, token=self.admin, **post)
        self.assertEqual(status, 200, raw)
        recovery = json.loads(raw)
        self.assertEqual(recovery["recovery_for"], jid)
        self.assertEqual(recovery["phase"], "queued")
        spec = self.jobs.dispatch.queue.get(recovery["id"])["spec"]
        self.assertEqual(spec["resources"], ["phone:fake-device"])
        self.assertEqual(spec["execution_policy"], "process_group")
        self.assertEqual(spec["argv"], ["gray-layer", "session-recover", "--session", str(out / "gray-session-fixture"),
                                        "--serial", "fake-device", "--json"])
        self.value["phase"] = "closed"
        (out / "session-control.json").write_text(json.dumps(self.value))
        auth["Cookie"] += "; " + read_fixture.dash.STEER_COOKIE + "=" + self.board._cookie_val("test-steer-password", b"hgamey-steer-v1")
        status, _, raw = self.request("/dash/api" + path, **{**post, "headers": auth})
        self.assertEqual(status, 200, raw)
        self.assertEqual(json.loads(raw)["id"], recovery["id"])
        self.assertEqual(self.get_json("/requests/recover-1")["id"], recovery["id"])
        self.assertEqual(self.jobs.dispatch.queue.get(jid)["state"], "blocked")

    def test_failed_recovery_worker_continues_from_leaf_with_original_session_source(self):
        helper = self.root / "recovery-phonefarm"
        helper.write_text("#!" + sys.executable + "\n" + RECOVERY_HELPER)
        helper.chmod(0o700)
        self.jobs.bins.update(phonefarm=(str(helper), str(self.root)), host=(sys.executable, str(self.root)))
        self.jobs.programs.refresh()
        pin = {"X-Expected-Node-ID": self.node.node_id}
        lease = {"schema_version": 1, "status": "clear", "bridge_held": False, "entries": [], "error": None}

        def post(path, key, value=None, spec=None):
            headers = {**pin, "X-Request-ID": key, "Content-Type": "application/json"}
            if spec is not None:
                headers.update({"X-Job": json.dumps(spec), "Content-Type": "application/octet-stream"})
            code, _, body = self.request(path, method="POST", token=self.admin, headers=headers,
                                        body=b"" if spec is not None else json.dumps(value or {}).encode())
            return code, json.loads(body)

        def submit(key, spec):
            code, value = post("/jobs", key, spec=spec)
            self.assertEqual(code, 200, value)
            return value["id"]

        def finished(jid, state):
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                value = self.get_json("/jobs/" + jid)
                if value.get("state") == "finished" and value.get("queue", {}).get("state") == state:
                    self.assertFalse(value["execution"]["worker_alive"])
                    self.assertEqual(value["execution"]["owned_process_group"]["status"], "absent")
                    return value
                time.sleep(.025)
            self.fail(f"job {jid} did not reach committed {state}: {value}")

        def cli(*args):
            env = {**os.environ, "PF_NODE_TOKEN": self.admin, "HOME": str(self.root)}
            for name in ("PF_NODE_TOKEN_FILE", "PF_NODE_URL", "PF_NODE_ID"):
                env.pop(name, None)
            address = "http://%s:%s" % self.server.server_address
            result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "pf-node"),
                                     "--url", address, "--node-id", self.node.node_id, *args],
                                    env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            return json.loads(result.stdout)

        with mock.patch.object(self.jobs.leases, "snapshot", side_effect=lambda *a, **kw: dict(lease)):
            try:
                self.assertIn("jobs.session-recovery-chain-v1", self.get_json("/node")["capabilities"])
                source = submit("chain-source", {"bin": "phonefarm", "argv": SESSION_ARGV,
                                                 "resources": ["phone:fake-device"], "device_access": "write"})
                original = finished(source, "blocked")
                self.assertEqual((original["outcome"], original["exit"]), ("failed", 2))
                source_out = Path(self.jobs.path(source, "out"))
                source_report = source_out / "gray-session-fixture" / f"report-{source}.json"
                source_bytes = source_report.read_bytes()
                original_log = Path(self.jobs.path(source, "log")).read_bytes()
                lease["status"] = "blocked"
                first = cli("session-recover", source, "--request-id", "chain-first")
                r1 = first["id"]
                self.assertEqual(first["recovery_for"], source)
                failed = finished(r1, "blocked")
                self.assertEqual((failed["outcome"], failed["exit"]), ("failed", 2))
                # These terminal states came from actual workers and finalize(),
                # not fixture writes to meta/queue or a mock recovery verdict.
                self.assertEqual(failed["recovery"]["status"], "not_reported")
                r1_receipt = Path(self.jobs.path(r1, "out", "lease-receipts", f"fixture-{r1}.json"))
                first_receipt = r1_receipt.read_bytes()
                first_log = Path(self.jobs.path(r1, "log")).read_bytes()
                plan = cli("session-recovery", source)
                self.assertEqual((plan["schema_version"], plan["node_id"], plan["job_id"]),
                                 (1, self.node.node_id, source))
                self.assertEqual((plan["source_job_id"], plan["session_id"], plan["target_job_id"], plan["eligible"]),
                                 (source, "gray-session-fixture", r1, True))
                self.assertEqual([(item["job_id"], item["recovery_for"]) for item in plan["attempts"]], [(r1, source)])
                self.assertEqual(self.get_json(f"/jobs/{r1}/session/recovery")["source_job_id"], source)
                code, stale = post(f"/jobs/{source}/session/recover", "chain-stale")
                self.assertEqual((code, stale["code"], stale["target_job_id"]), (409, "session_recovery_leaf", r1))
                self.assertIsNone(self.jobs.dispatch.queue.by_request("chain-stale"))
                same = submit("same-resource", {"bin": "host", "argv": ["-c", "raise RuntimeError('must stay queued')"],
                                               "resources": ["phone:fake-device"], "device_access": "none"})
                free = submit("other-host", {"bin": "host", "argv": ["-c", "print('independent host work')"],
                                            "resources": [], "device_access": "none"})
                self.assertEqual(finished(free, "done")["exit"], 0)
                self.assertEqual(self.get_json("/jobs/" + same)["queue"]["state"], "queued")
                self.assertFalse(Path(self.jobs.path(same, "launch.json")).exists())

                barrier = threading.Barrier(2)
                def race(key):
                    barrier.wait(timeout=5)
                    return post(f"/jobs/{r1}/session/recover", key)
                before = len(self.jobs.dispatch.queue.rows())
                with ThreadPoolExecutor(max_workers=2) as threads:
                    responses = list(threads.map(race, ("chain-second-a", "chain-second-b")))
                self.assertEqual(sorted(code for code, _ in responses), [200, 409], responses)
                second = next(value for code, value in responses if code == 200)
                r2 = second["id"]
                self.assertEqual(second["recovery_for"], r1)
                self.assertEqual(second["queue"]["resources"], ["phone:fake-device"])
                self.assertEqual(len(self.jobs.dispatch.queue.rows()), before + 1)
                denial = next(value for code, value in responses if code == 409)
                self.assertIn(denial["code"], ("session_recovery_leaf", "session_recovery_blocked"))
                self.assertEqual(post(f"/jobs/{r1}/queue-release", "early-release", {"note": "must not bypass child"})[0], 409)
                (self.root / "release-r2").touch()
                recovered = finished(r2, "blocked")
                self.assertEqual((recovered["outcome"], recovered["exit"]), ("succeeded", 0))
                for jid in (r1, r2):
                    observed = json.loads(Path(self.jobs.path(jid, "out", "observed.json")).read_text())
                    self.assertEqual(Path(observed["source"]).resolve(), source_out.resolve())
                    self.assertEqual(Path(observed["recovery_out"]).resolve(), source_out.resolve())
                    self.assertFalse(Path(self.jobs.path(jid, "out", "session-control.json")).exists())
                self.assertEqual((source_out / "recovery-count").read_text(), "2")
                self.assertEqual(source_report.read_bytes(), source_bytes)
                self.assertEqual(Path(self.jobs.path(source, "log")).read_bytes(), original_log)
                self.assertEqual(r1_receipt.read_bytes(), first_receipt)
                self.assertEqual(Path(self.jobs.path(r1, "log")).read_bytes(), first_log)
                receipt = self.get_json(f"/jobs/{r2}/leases")["entries"][0]
                self.assertEqual(receipt["recovery"]["status"], "reported_complete")
                self.assertEqual(receipt["recovery"]["evidence_origin"], "synthetic")
                self.assertEqual(receipt["recovery_of"], {"job_id": r1, "lease_id": "fixture-" + r1})
                self.assertEqual(cli("session-recover", source, "--request-id", "chain-first")["id"], r1)
                settled = self.get_json(f"/jobs/{source}/session/recovery")
                self.assertFalse(settled["eligible"])
                self.assertEqual(settled["target_job_id"], r2)
                self.assertEqual([item["job_id"] for item in settled["attempts"]], [r1, r2])
                self.assertTrue(all(self.jobs.dispatch.queue.get(jid)["state"] == "blocked" for jid in (source, r1, r2)))
                self.assertEqual(post(f"/jobs/{r2}/queue-release", "lease-not-clear", {"note": "still blocked"})[0], 409)
                self.assertTrue(post(f"/jobs/{same}/cancel", "cancel-waiter")[1]["accepted"])
                lease["status"] = "clear"
                for jid in (r2, r1, source):
                    code, released = post(f"/jobs/{jid}/queue-release", "release-" + jid,
                                          {"note": "synthetic host chain reviewed; no device recovery claim"})
                    self.assertEqual((code, released["released"]), (200, True), released)
                self.assertEqual(self.get_json("/jobs/" + r1)["outcome"], "failed")
            except BaseException:
                # The enclosing QA log retains raw diagnostics if this fixture fails.
                for row in self.jobs.dispatch.queue.rows():
                    jid = row["job_id"]
                    log = Path(self.jobs.path(jid, "log"))
                    print("recovery-chain fixture", jid, self.jobs.status(jid),
                          log.read_text(errors="replace")[-16384:] if log.exists() else "no log", flush=True)
                raise
            finally:
                (self.root / "release-r2").touch()
                for row in self.jobs.dispatch.queue.rows(("preparing", "queued")):
                    self.jobs.cancel(row["job_id"])
                dispatcher = self.jobs.dispatch.thread
                if dispatcher:
                    dispatcher.join(timeout=15)
                    self.assertFalse(dispatcher.is_alive(), "bounded fixture workers must finish before teardown")

    def test_http_auth_fixed_helper_and_readonly_receipts(self):
        jid, out, action = self.prepare()
        path = f"/jobs/{jid}/session"
        self.assertIn("jobs.session-control-v1", self.get_json("/node")["capabilities"])
        self.assertTrue(self.get_json(path)["live_owner"])
        auth = {"Cookie": f"{read_fixture.dash.COOKIE}={self.board._cookie_val(self.pw)}"}
        self.assertEqual(self.get_json(path), self.get_json("/dash/api" + path, headers=auth))
        post = dict(method="POST", body=json.dumps(action).encode(), headers={"Content-Type": "application/json"})
        self.assertEqual(self.request(path + "/actions", **post)[0], 401)
        self.assertEqual(self.request(path + "/actions", token=self.admin, **{**post, "body": b"null"})[0], 400)
        self.assertEqual(self.request("/dash/api" + path + "/actions", **{**post,
                         "headers": {**auth, "X-HG": "1", "Content-Type": "application/json"}})[0], 403)
        status, _, raw = self.request(path + "/actions", token=self.admin, **post)
        self.assertEqual(status, 200, raw)
        self.assertEqual(json.loads(raw)["status"], "accepted")
        self.assertEqual(json.loads((out / "helper-input.json").read_text()), action)
        receipt = self.get_json(path + "/actions/capture-1")
        self.assertEqual(receipt["request_id"], "capture-1")
        auth["Cookie"] += "; " + read_fixture.dash.STEER_COOKIE + "=" + self.board._cookie_val("test-steer-password", b"hgamey-steer-v1")
        status, _, raw = self.request("/dash/api" + path + "/actions", method="POST", body=json.dumps(action).encode(),
                                      headers={**auth, "X-HG": "1", "Content-Type": "application/json"})
        self.assertEqual(status, 200, raw)
        action["action"] = "closed"
        status, _, raw = self.request(path + "/actions", token=self.admin, **{**post, "body": json.dumps(action).encode()})
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(raw)["code"], "owner_unavailable")
        calls = [json.loads(line) for line in (out / "helper-calls").read_text().splitlines()]
        self.assertTrue(all(c[:2] in (["gray-layer", "session-status"], ["gray-layer", "session-action"]) for c in calls))
        self.assertTrue(all(c[c.index("--out") + 1] == str(out) for c in calls))
        self.assertFalse(self.board.wake.is_set(), "session reads must not wake the independent sampler")

    def test_descriptor_paths_identity_and_timeout_never_publish_twice(self):
        jid, out, action = self.prepare()
        for change in ({"producer": "other"}, {"job_id": "f" * 12}, {"serial": ""},
                       {"session_path": "../outside"}, {"latest_image": {"path": "/outside.png"}}):
            (out / "session-control.json").write_text(json.dumps({**self.value, **change}))
            with mock.patch("node_session.subprocess.run") as run, self.assertRaises(ValueError):
                self.jobs.session(jid, action=action)
            run.assert_not_called()
        (out / "session-control.json").write_text(json.dumps(self.value))
        (out / "gray-control").rmdir()
        (out / "gray-control").symlink_to(out / "gray-session-fixture")
        with mock.patch("node_session.subprocess.run") as run, self.assertRaises(ValueError):
            self.jobs.session(jid, action=action)
        run.assert_not_called()
        (out / "gray-control").unlink()
        (out / "gray-control").mkdir()
        with mock.patch("node_session.subprocess.run", side_effect=subprocess.TimeoutExpired("helper", 5)) as run:
            with self.assertRaises(SessionError) as failed:
                self.jobs.session(jid, action=action)
            self.assertEqual(failed.exception.status, 504)
            self.assertEqual(failed.exception.payload["request_id"], action["request_id"])
            self.assertEqual(run.call_count, 1)
            self.assertEqual(json.loads(run.call_args.kwargs["input"]), action)
            self.assertLessEqual(run.call_args.kwargs["timeout"], 5)
