"""One synthetic host worker chain and one SQL scope chain; no device IO."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_node_games as fixture
from device_scope import conflicts, resource, scope, lease_problem, DeviceScope
from device_registry import DeviceRegistry, RegistryError, ERROR_SCHEMA
from job_queue import JobQueue
from node_service import readonly_token


DOMAIN = "a" * 32


def bound(device, serial):
    return {"schema": "phonefarm/device-binding@1", "domain_id": DOMAIN,
            "registry_revision": 1, "device_id": device, "serial": serial}


PROGRAM = r'''
import json, os, pathlib, sys, time
root = pathlib.Path(__file__).parent
args = sys.argv[1:]
if args == ['runtime-info', '--json']:
    print(json.dumps({'schema':'phonefarm/runtime-identity@1','program':'phonefarm',
        'package_version':'host-fixture','build':{'source_revision':None,'source_dirty':False,
        'target':'host-fixture','profile':'test'},
        'supported_protocols':['phonefarm/runtime-identity@1','phonefarm/gray-control-gateway@1']}))
    sys.exit(0)
with (root/'calls.jsonl').open('a') as log:
    log.write(json.dumps({'argv':args,'pid':os.getpid()})+'\n')
if args[:2] == ['lease','resolve']:
    serial=args[args.index('--serial')+1]
    data=json.loads((root/'registry.json').read_text())
    print(json.dumps({'schema':'phonefarm/device-binding@1','domain_id':'a'*32,
        'registry_revision':data['revision'],'device_id':data['aliases'][serial],'serial':serial}))
    sys.exit(0)
if args[:2] == ['lease','devices']:
    data=json.loads((root/'registry.json').read_text())
    print(json.dumps({'schema':'phonefarm/device-registry@1','revision':data['revision'],
        'identity_basis':'operator_declared','physical_identity_verified':False,'execution_scope':'global',
        'updated_at':None,'devices':[{'device_id':device,'label':device,'aliases':[serial],'enabled':True}
                                      for serial,device in data['aliases'].items()]}))
    sys.exit(0)
if args[:2] == ['gray-layer','session-status']:
    out=pathlib.Path(args[args.index('--out')+1])
    print(json.dumps(dict(json.loads((out/'session-control.json').read_text()),live_owner=False)))
    sys.exit(0)
assert args[:2] in (['gray-layer','session'],['gray-layer','session-recover']),args
out=pathlib.Path(os.environ['PF_JOB_OUT'])
jid=os.environ['PF_JOB_ID']
initial=args[1]=='session'
if initial:
    binding=json.loads(args[args.index('--device-binding')+1])
    serial=args[args.index('--serial')+1]
    assert binding['serial']==serial
    source=out
    session=out/'gray-session-fixture'
    session.mkdir()
    (out/'gray-control').mkdir()
    state={'schema':'phonefarm/gray-control@1','producer':'phonefarm','job_id':jid,
        'session_id':session.name,'session_path':session.name,'serial':serial,'lease_scope':'device',
        'device_binding':binding,'phase':'recovery_required','accepting':False,'allowed_actions':[],
        'controller':None,'latest_image':None,
        'recovery':{'status':'required','session_path':session.name,'reports':[]}}
    attempt=0
else:
    assert '--device-binding' not in args,'recovery must use the original session, not a new scope flag'
    session=pathlib.Path(args[args.index('--session')+1])
    source=session.parent
    assert pathlib.Path(os.environ['PF_RECOVERY_OUT'])==source
    state=json.loads((source/'session-control.json').read_text())
    count=source/'attempt-count'
    attempt=int(count.read_text())+1 if count.exists() else 1
    count.write_text(str(attempt))
    binding=state['device_binding']; serial=state['serial']
observed={'job_id':jid,'attempt':attempt,'source':str(source),'binding':binding,
          'argv':args,'inherited_instance':os.environ.get('DEVLOCK_INSTANCE_ID')}
(out/'observed.json').write_text(json.dumps(observed))
(root/('started-'+jid)).write_text(str(os.getpid()))
if initial:
    deadline=time.monotonic()+12
    while not (root/('finish-'+serial)).exists():
        if time.monotonic()>deadline:
            raise RuntimeError('bounded synthetic worker expired')
        time.sleep(.02)
done=(not initial and attempt==2) or (initial and binding['device_id']=='B')
report=session/('report-'+jid+'.json')
report.write_text(json.dumps({'evidence_origin':'synthetic','attempt':attempt,
    'status':'verified' if done else 'required','scope':'host_fixture_only'}))
state['recovery']['reports'].append(str(report.relative_to(source)))
state['recovery']['status']='reported_complete' if done else 'required'
state['phase']='closed' if done else 'recovery_required'
(source/'session-control.json').write_text(json.dumps(state))
print(json.dumps(state),flush=True)
sys.exit(0 if done else 2)
'''


class DeviceScopeTests(unittest.TestCase):
    def test_raw_serial_uses_its_lane_in_a_busy_snapshot(self):
        snapshot = {"status": "held", "storage": {"readiness": "ready", "layout_version": 2,
                    "domain_id": DOMAIN}, "device_scopes": {"schema": "phonefarm/device-scopes@1",
                    "domain_id": DOMAIN, "registry_revision": 1, "unknown": False, "queue_waiting": False,
                    "global": {"status": "clear", "error": None}, "devices": [
                        {"device_id": "A", "aliases": ["usb-a", "wifi-a"], "status": "clear", "error": None},
                        {"device_id": "B", "aliases": ["usb-b"], "status": "held", "error": None}]}}
        original = {"bin": "adb", "argv": ["-s", "usb-a", "shell", "getprop"]}
        self.assertIsNone(lease_problem(original, snapshot))
        self.assertNotIn("device_binding", original, "historical job input stays frozen")
        with tempfile.TemporaryDirectory() as directory:
            queue = JobQueue(Path(directory) / "queue.sqlite3", default_cpu=3)
            for key, spec in (("b", {"bin": "adb", "device_binding": bound("B", "usb-b")}),
                              ("a", original), ("alias", {**original, "argv": ["-s", "wifi-a"]})):
                row, _ = queue.reserve(key, key, spec, [])
                queue.ready(row["job_id"])
            self.assertEqual(queue.claim_next(3, lease_snapshot=snapshot)["request_id"], "b")
            self.assertEqual(queue.claim_next(3, lease_snapshot=snapshot)["request_id"], "a")
            self.assertIsNone(queue.claim_next(3, lease_snapshot=snapshot), "aliases still serialize")
        self.assertIsNotNone(lease_problem({**original, "argv": ["-s", "usb-b"]}, snapshot))
        snapshot["device_scopes"]["global"]["status"] = "held"
        self.assertIsNotNone(lease_problem(original, snapshot))

    def test_auto_bound_perf_prepares_without_a_session_only_flag(self):
        env = fixture.GameFlowTests()
        env.setUp()
        self.addCleanup(env.tearDown)
        env.jobs.bins["phonefarm"] = ("/usr/bin/false", str(env.root))
        with mock.patch.object(env.jobs.device_scopes, "registered", return_value=bound("A", "usb-a")), \
                mock.patch.object(env.jobs.device_scopes, "admit"), \
                mock.patch.object(env.jobs.dispatch, "start"):
            job = env.jobs.start({"bin": "phonefarm", "argv": ["perf", "--serial", "usb-a", "--rounds", "1"]}, b"")
            explicit = env.jobs.start({"bin": "phonefarm", "argv": ["perf", "--serial", "usb-a", "--rounds", "1"],
                                       "device_binding": bound("A", "usb-a")}, b"")
        path = Path(env.jobs.path(job["id"], "prepared.json"))
        self.assertTrue(path.exists(), job)
        prepared = json.loads(path.read_text())
        self.assertNotIn("--device-binding", prepared["command"])
        self.assertEqual(job["device_binding"], bound("A", "usb-a"))
        self.assertEqual(explicit["device_binding"], bound("A", "usb-a"))

    def test_serial_scopes_write_jobs_to_one_device(self):
        first = scope({"bin": "adb", "device_access": "write", "argv": ["-s", "redmagic", "shell", "getprop"]})
        second = scope({"bin": "adb", "device_access": "write", "argv": ["--serial=huawei", "shell", "getprop"]})
        self.assertEqual((first["scope"], first["binding"]["device_id"]), ("device", "redmagic"))
        self.assertEqual((second["scope"], second["binding"]["device_id"]), ("device", "huawei"))
        self.assertFalse(conflicts(first, second))
        self.assertTrue(conflicts(first, first))
        self.assertTrue(conflicts(first, scope({"bin": "adb", "device_access": "write", "argv": ["shell", "getprop"]})))
        self.assertEqual(scope({"bin": "adb", "device_access": "none", "argv": ["-s", "redmagic"]})["scope"], "none")
        self.assertEqual(scope({"bin": "adb", "device_access": "write", "argv": ["-s", "a", "-s", "b"]})["scope"], "global")
        typed = bound("A", "redmagic")
        self.assertEqual(scope({"bin": "phonefarm", "device_access": "write", "device_binding": typed,
                                "argv": ["--serial", "huawei"]})["binding"], typed)

        with tempfile.TemporaryDirectory() as temporary:
            queue = JobQueue(Path(temporary) / "queue.sqlite3", default_cpu=3)
            ids = []
            for serial in ("redmagic", "huawei", "redmagic"):
                spec = {"bin": "adb", "device_access": "write", "argv": ["-s", serial, "shell", "getprop"]}
                row, _ = queue.reserve(serial, serial, spec, ["legacy-device"])
                queue.ready(row["job_id"])
                ids.append(row["job_id"])
            self.assertEqual(queue.claim_next(3)["job_id"], ids[0])
            self.assertEqual(queue.claim_next(3)["job_id"], ids[1])
            self.assertIsNone(queue.claim_next(3), "only the same serial remains reserved")

    def test_empty_registry_resolve_explains_raw_serial_alternative(self):
        env = fixture.GameFlowTests()
        env.setUp()
        self.addCleanup(env.tearDown)
        unavailable = {"schema": ERROR_SCHEMA, "code": "unavailable", "error": "registry unavailable"}
        with mock.patch.object(DeviceRegistry, "_run", return_value=(2, unavailable)), \
                mock.patch.object(DeviceRegistry, "read", return_value={"devices": []}):
            with self.assertRaises(RegistryError) as rejected:
                DeviceScope(env.jobs).resolve("91253241019A")
        self.assertEqual(rejected.exception.status, 503)
        self.assertIn("device registry is empty", str(rejected.exception))
        self.assertIn("device_requirements", str(rejected.exception))
        self.assertIn("--resource device:<serial>", str(rejected.exception))

    def test_research_single_device_requirement_scopes_write_job(self):
        env = fixture.GameFlowTests()
        env.setUp()
        self.addCleanup(env.tearDown)
        pin = {"X-Expected-Node-ID": env.jobs.node_id}

        def research(rid, devices):
            write = {"schema": "phonefarm/research-write@1", "operation_id": "create-" + rid,
                     "base_revision": None, "actor_label": None, "reason": None,
                     "data": {"title": rid, "objective": "只占用指定手机", "device_requirements": devices}}
            env.jobs.research.write("research", rid, write, "fixture")

        def submit(request, spec):
            status, response = env.call("POST", "/jobs", headers={**pin, "X-Request-ID": request,
                                                                  "X-Job": json.dumps(spec)})
            self.assertEqual(status, 200, response)
            return response

        research("sr-redmagic", ["91253241019A"])
        scoped = submit("research-scoped", {"bin": "bundle_prepare", "argv": ["selfsr/repro.sh", "91253241019A"],
                                            "device_access": "write", "research_id": "sr-redmagic"})
        self.assertEqual(scoped["resources"], ["device:91253241019A"])
        self.assertEqual((scope(scoped)["scope"], scope(scoped)["binding"]["device_id"]), ("device", "91253241019A"))
        self.assertFalse(conflicts(scope(scoped), scope({"bin": "adb", "device_access": "write", "argv": ["-s", "HONOR"]})))

        explicit = submit("explicit-resource", {"bin": "bundle_prepare", "argv": ["selfsr/repro.sh", "91253241019A"],
                                                "device_access": "write", "research_id": "sr-redmagic",
                                                "resources": ["device:custom-serial"]})
        self.assertEqual((explicit["resources"], scope(explicit)["binding"]["device_id"]),
                         (["device:custom-serial"], "custom-serial"))
        argv_scoped = submit("explicit-argv", {"bin": "bundle_prepare", "argv": ["-s", "HONOR", "shell", "getprop"],
                                               "device_access": "write", "research_id": "sr-redmagic"})
        self.assertEqual(scope(argv_scoped)["binding"]["device_id"], "HONOR")
        research("sr-multiple", ["91253241019A", "HONOR"])
        global_job = submit("multiple-devices", {"bin": "bundle_prepare", "argv": ["selfsr/repro.sh", "91253241019A"],
                                                 "device_access": "write", "research_id": "sr-multiple"})
        self.assertEqual(scope(global_job)["scope"], "global")

    def test_registered_serial_scope_uses_typed_binding(self):
        env = fixture.GameFlowTests()
        env.setUp()
        self.addCleanup(env.tearDown)
        program = env.root / "scope-pf"
        program.write_text("#!" + sys.executable + "\n" + PROGRAM)
        program.chmod(0o755)
        env.jobs.bins["phonefarm"] = (str(program), str(env.root))
        env.jobs.programs.refresh()
        (env.root / "registry.json").write_text(json.dumps({"revision": 1, "aliases": {"usb-a": "A", "usb-b": "B"}}))
        pin = {"X-Expected-Node-ID": env.jobs.node_id}

        def submit(request, spec):
            status, response = env.call("POST", "/jobs", headers={**pin, "X-Request-ID": request,
                                                                  "X-Job": json.dumps(spec)})
            self.assertEqual(status, 200, response)
            return response

        write = {"schema": "phonefarm/research-write@1", "operation_id": "create-typed", "base_revision": None,
                 "actor_label": None, "reason": None,
                 "data": {"title": "typed", "objective": "已登记设备走 lane", "device_requirements": ["usb-a"]}}
        env.jobs.research.write("research", "sr-typed", write, "fixture")
        research = submit("typed-research", {"bin": "bundle_prepare", "argv": ["script"], "device_access": "write",
                                             "research_id": "sr-typed"})
        self.assertEqual(research["device_binding"], bound("A", "usb-a"))
        self.assertEqual(scope(research)["scope"], "device")
        explicit = submit("typed-resource", {"bin": "bundle_prepare", "argv": ["script"], "device_access": "write",
                                             "resources": ["device:usb-b"]})
        self.assertEqual(explicit["device_binding"], bound("B", "usb-b"))
        argv = submit("typed-argv", {"bin": "bundle_prepare", "argv": ["-s", "usb-a"], "device_access": "write"})
        self.assertEqual(argv["device_binding"], bound("A", "usb-a"))
        self.assertFalse(conflicts(scope(research), scope(explicit)))
        self.assertTrue(conflicts(scope(research), scope(argv)))
        legacy = submit("legacy-resource", {"bin": "bundle_prepare", "argv": ["script"], "device_access": "write",
                                            "resources": ["device:unknown-serial"]})
        self.assertNotIn("device_binding", legacy)
        self.assertEqual(scope(legacy)["binding"]["device_id"], "unknown-serial")

    def test_bound_http_workers_recovery_and_original_request(self):
        env = fixture.GameFlowTests()
        env.setUp()
        self.addCleanup(env.tearDown)
        program = env.root / "scope-pf"
        program.write_text("#!" + sys.executable + "\n" + PROGRAM)
        program.chmod(0o755)
        env.jobs.bins["phonefarm"] = (str(program), str(env.root))
        env.jobs.programs.refresh()
        registry = env.root / "registry.json"
        registry.write_text(json.dumps({"revision": 1, "aliases": {"usb-a": "A", "wifi-a": "A", "usb-b": "B"}}))
        current = {"schema_version": 1, "status": "clear", "bridge_held": False, "entries": [], "error": None,
            "storage": {"schema_version": 1, "layout_version": 2, "domain_id": DOMAIN,
                        "readiness": "ready", "devlock_dir": str(env.root / "locks"),
                        "queue_dir": str(env.root / "locks/v2/global")},
            "device_scopes": {"schema": "phonefarm/device-scopes@1", "domain_id": DOMAIN,
                "registry_revision": 1, "global": {"status": "clear", "error": None},
                "queue_waiting": False, "unknown": False,
                "devices": [{"device_id": key, "status": "clear", "error": None} for key in ("A", "B")]}}
        env.jobs.leases.snapshot = lambda **kw: copy.deepcopy(current)
        pin = {"X-Expected-Node-ID": env.jobs.node_id}
        a, a2, b = bound("A", "usb-a"), bound("A", "wifi-a"), bound("B", "usb-b")
        submitted = []

        def wait(check):
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                value = check()
                if value:
                    return value
                time.sleep(.025)
            self.fail("bounded synthetic scope chain did not settle")

        def spec(value):
            return {"bin": "phonefarm", "argv": ["gray-layer", "session", "--serial", value["serial"],
                "--mode", "probe", "--package", "com.example.fixture", "--out", "@out", "--max-seconds", "30"],
                "device_binding": value}

        def submit(key, value):
            status, response = env.call("POST", "/jobs", headers={**pin, "X-Request-ID": key,
                                                                   "X-Job": json.dumps(value)})
            if status == 200:
                submitted.append(response["id"])
            return status, response

        def terminal(jid):
            def read():
                value = env.jobs.status(jid)
                return value if value["state"] == "finished" else None
            return wait(read)

        def resolve_calls():
            path = env.root / "calls.jsonl"
            return sum(json.loads(line)["argv"][:2] == ["lease", "resolve"]
                       for line in path.read_text().splitlines()) if path.exists() else 0

        try:
            status, resolved = env.call("GET", "/node/devices/resolve?serial=usb-a",
                                       token=readonly_token(env.admin), headers=pin)
            self.assertEqual((status, resolved), (200, {"node_id": env.jobs.node_id, "binding": a}))
            before = resolve_calls()
            self.assertEqual(env.call("GET", "/node/devices/resolve?serial=usb-a")[0], 400)
            for invalid in ({**spec(a), "exe": "@in/other"}, {**spec(a), "device_access": "none"},
                            {**spec(a), "argv": spec(a)["argv"] + ["--device-binding", json.dumps(a)]}):
                self.assertEqual(submit("refuse-" + str(len(json.dumps(invalid))), invalid)[0], 400)
            self.assertEqual(resolve_calls(), before)
            code, first = submit("scope-a", spec(a)); self.assertEqual(code, 200, first)
            wait(lambda: (env.root / ("started-" + first["id"])).exists())
            code, alias = submit("scope-a-alias", spec(a2)); self.assertEqual(code, 200, alias)
            code, other = submit("scope-b", spec(b)); self.assertEqual(code, 200, other)
            wait(lambda: (env.root / ("started-" + other["id"])).exists())
            self.assertFalse((env.root / ("started-" + alias["id"])).exists())
            blocked = env.jobs.status(alias["id"])["queue"]["waiting"]
            self.assertIn(first["id"], blocked["blocking_jobs"])
            self.assertEqual(env.jobs.dispatch.queue.get(first["id"])["resources"], [resource(a)])
            self.assertEqual(env.jobs.status(first["id"])["state"], "running")
            before = resolve_calls()
            registry.write_text(json.dumps({"revision": 2, "aliases": {"usb-a": "B"}}))
            code, repeat = submit("scope-a", spec(a))
            self.assertEqual((code, repeat["id"]), (200, first["id"]))
            self.assertEqual(resolve_calls(), before, "original ID must not resolve a changed registry")
            self.assertEqual(env.call("POST", "/jobs/" + alias["id"] + "/cancel", {}, headers=pin)[0], 200)
            (env.root / "finish-usb-a").touch()
            ended = terminal(first["id"])
            self.assertEqual((ended["exit"], ended["queue"]["state"]), (2, "done"))
            self.assertEqual(submit("refuse-unbound-recovery", {
                "bin": "phonefarm", "argv": ["gray-layer", "session-recover"],
                "device_access": "recovery", "recovery_for": first["id"]})[0], 400)
            current["status"] = "blocked"
            current["device_scopes"]["devices"][0]["status"] = "blocked"
            current["device_scopes"]["devices"][1]["status"] = "held"
            current["device_scopes"]["queue_waiting"] = True
            code, plan = env.call("GET", "/jobs/" + first["id"] + "/session/recovery", headers=pin)
            self.assertEqual((code, plan["eligible"], plan["device_binding"]), (200, True, a))
            attempts = []
            for number, parent in ((1, first["id"]), (2, None)):
                parent = parent or attempts[-1]["id"]
                code, attempt = env.call("POST", "/jobs/" + parent + "/session/recover", {},
                    headers={**pin, "X-Request-ID": "scope-recover-" + str(number)})
                self.assertEqual(code, 200, attempt)
                submitted.append(attempt["id"])
                done = terminal(attempt["id"])
                self.assertEqual((done["exit"], done["queue"]["state"]), (2 if number == 1 else 0, "done"))
                actual = json.loads((Path(env.jobs.path(attempt["id"], "out")) / "observed.json").read_text())
                self.assertEqual(actual["binding"], a)
                self.assertEqual(actual["source"], env.jobs.path(first["id"], "out"))
                self.assertNotIn("--device-binding", actual["argv"])
                self.assertIsNone(actual["inherited_instance"])
                attempts.append(attempt)
            self.assertEqual(env.call("POST", "/jobs/" + first["id"] + "/queue-release",
                                     {"note": "premature ancestor"}, headers=pin)[0], 409)
            current["device_scopes"]["devices"][0]["status"] = "clear"
            for item in [*reversed(attempts), first]:
                view = env.jobs.status(item["id"])
                self.assertFalse(view["queue"]["release_eligibility"]["eligible"], view)
                self.assertEqual(env.jobs.dispatch.queue.get(item["id"])["state"], "done")
            self.assertEqual(env.jobs.status(other["id"])["state"], "running")
            (env.root / "finish-usb-b").touch(); terminal(other["id"])
        finally:
            for serial in ("usb-a", "wifi-a", "usb-b"):
                (env.root / ("finish-" + serial)).touch()
            for row in env.jobs.dispatch.queue.rows(("preparing", "queued")):
                env.jobs.cancel(row["job_id"])
            for jid in set(submitted):
                terminal(jid)

    def test_claim_live_global_and_ancestor_scope_conflicts(self):
        with tempfile.TemporaryDirectory() as temporary:
            queue = JobQueue(Path(temporary) / "queue.sqlite3", default_cpu=3)
            def settled(jid, state):
                # This SQL-only fixture never starts a command. Device/resource
                # barriers remain independent of the confirmed unused host slot.
                queue.record_unstarted(jid, "failed", "scope fixture never executed")
                queue.set_state(jid, state)
            def add(key, value=None, *, access="write", parent=None):
                spec = {"bin": "phonefarm", "device_access": access}
                if value is not None:
                    spec["device_binding"] = value
                if parent:
                    spec["recovery_for"] = parent
                row, _ = queue.reserve(key, key, spec, [])
                queue.ready(row["job_id"])
                return row["job_id"]
            a = add("a", bound("A", "usb-a"))
            self.assertEqual(queue.claim_next(3)["job_id"], a)
            global_job = add("global")
            b = add("b", bound("B", "usb-b"))
            host = add("host", access="none")
            self.assertEqual(queue.claim_next(3)["job_id"], b, "a waiting global job must not pre-drain another device")
            self.assertEqual(queue.claim_next(3)["job_id"], host)
            self.assertIsNone(queue.claim_next(3))
            settled(b, "done")
            settled(a, "blocked")
            recovery = add("recover-a", bound("A", "usb-a"), access="recovery", parent=a)
            self.assertEqual(queue.claim_next(3)["job_id"], recovery, "recovery borrows the original reservation")
            settled(recovery, "blocked")
            sibling = add("sibling", bound("A", "wifi-a"), access="recovery", parent=a)
            leaf = add("leaf", bound("A", "usb-a"), access="recovery", parent=recovery)
            self.assertEqual(queue.claim_next(3)["job_id"], leaf)
            self.assertIsNone(queue.claim_next(4), "a sibling may not borrow the failed recovery reservation")
            for jid in (leaf, recovery, a):
                settled(jid, "done")
            self.assertEqual(queue.claim_next(3)["job_id"], global_job)
            self.assertIsNone(queue.claim_next(4), "claimed global covers every device before PF starts")
            settled(global_job, "done")
            self.assertEqual(queue.claim_next(3)["job_id"], sibling)


if __name__ == "__main__":
    unittest.main()
