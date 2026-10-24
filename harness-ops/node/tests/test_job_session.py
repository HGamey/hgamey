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


SESSION_ARGV = ["gray-layer", "session", "--mode", "probe", "--package", "com.example.fixture",
                "--serial", "fake-device", "--out", "@out", "--max-seconds", "30"]


RECOVERY_HELPER = r'''
import json, os, pathlib, sys, time
a = sys.argv[1:]
root = pathlib.Path(__file__).parent
def save(path, value):
    path.write_text(json.dumps(value))
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
