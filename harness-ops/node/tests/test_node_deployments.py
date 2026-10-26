import base64
import copy
import io
import json
from pathlib import Path
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dash
from node_deployments import NodeDeployments
from node_service import readonly_token
from test_device_scope import bound, resource, DOMAIN
import test_node_games as game_fixture


class DeploymentFlowTests(unittest.TestCase):
    def setUp(self):
        self.env = game_fixture.GameFlowTests()
        self.env.setUp()

    def tearDown(self):
        self.env.tearDown()

    def test_bound_game_workers_parallel_alias_and_original_recovery(self):
        env = self.env
        helper = env.root / "game-scope-pf"
        helper.write_text("#!" + sys.executable + "\n" + r'''
import json, os, pathlib, sys, time
root=pathlib.Path(__file__).parent
a=sys.argv[1:]
if a[:2]==['game-deploy','capabilities']:
    print(json.dumps({'schema':'phonefarm/game-deploy-capabilities@1','resources_v1':True,'device_binding_v1':True})); sys.exit()
if a[:2]==['lease','resolve']:
    serial=a[a.index('--serial')+1]; registry=json.loads((root/'registry.json').read_text())
    with (root/'resolves').open('a') as log: log.write(serial+'\n')
    print(json.dumps({'schema':'phonefarm/device-binding@1','domain_id':'a'*32,
      'registry_revision':registry['revision'],'device_id':registry['aliases'][serial],'serial':serial})); sys.exit()
out=pathlib.Path(a[a.index('--out')+1]); recovering=a[:2]==['game-deploy','recover']
if recovering:
    assert '--device-binding' not in a
    source=pathlib.Path(a[a.index('--session')+1])
    saved=json.loads((source/'session.json').read_text()); r=saved['request']; binding=saved['binding']
else:
    r=json.loads(pathlib.Path(a[a.index('--request')+1]).read_text())
    binding=json.loads(a[a.index('--device-binding')+1])
    assert r['serial']==binding['serial']
session=out/'game-deploy'; session.mkdir()
(session/'session.json').write_text(json.dumps({'request':r,'binding':binding}))
(out/'observed.json').write_text(json.dumps({'argv':a,'binding':binding}))
(root/('started-'+os.environ['PF_JOB_ID'])).touch()
if not recovering:
    deadline=time.monotonic()+12
    while not (root/('finish-'+r['serial'])).exists():
        if time.monotonic()>deadline: raise RuntimeError('bounded game fixture expired')
        time.sleep(.02)
done=recovering or binding['device_id']=='B'
report={k:r[k] for k in ('node_id','game_id','version','serial','mode')}
report.update(schema='phonefarm/game-deployment@1',lease_scope='device',device_binding=binding,
 phase='restored' if done else 'recovery_required',observed_at='synthetic',session_path='game-deploy',
 recovery={'status':'reported_complete' if recovering else 'not_needed' if done else 'required'})
for k in ('installed','resources_deployed','application_access','login','scene_ready'): report[k]={'status':'unknown'}
(out/'deployment.json').write_text(json.dumps(report))
sys.exit(0 if done else 2)
''')
        helper.chmod(0o700)
        env.jobs.bins["phonefarm"] = (str(helper), str(env.root))
        env.jobs.deployments = NodeDeployments(env.jobs, env.jobs.games)
        self.assertIn("games.device-lanes-v1", env.call("GET", "/node")[1]["capabilities"])
        registry = env.root / "registry.json"
        registry.write_text(json.dumps({"revision": 1, "aliases": {"usb-a": "A", "wifi-a": "A", "usb-b": "B"}}))
        current = {"schema_version": 1, "status": "clear", "bridge_held": False, "entries": [], "error": None,
            "storage": {"schema_version": 1, "layout_version": 2, "domain_id": DOMAIN,
                        "readiness": "ready", "devlock_dir": str(env.root / "locks")},
            "device_scopes": {"schema": "phonefarm/device-scopes@1", "domain_id": DOMAIN,
                "registry_revision": 1, "global": {"status": "clear", "error": None},
                "queue_waiting": False, "unknown": False,
                "devices": [{"device_id": key, "status": "clear", "error": None} for key in ("A", "B")]}}
        env.jobs.leases.snapshot = lambda **kw: copy.deepcopy(current)
        manifest = env.manifest(deployment={"schema": "phonefarm/game-resources-recipe@1",
            "package": "test.game", "version_code": 1, "apk_sha256": "a" * 64,
            "files": [{"asset_name": "data", "path": "base/data.pk4"}]})
        self.assertEqual(env.call("POST", "/games", manifest)[0], 201)
        asset = manifest["assets"][0]
        cache = env.jobs.games.cache
        cache.upload_open(asset, "scope-fixture")
        cache.upload_chunk(asset, "scope-fixture", 0, 7, io.BytesIO(b"fixture"))
        cache.upload_seal(asset, "scope-fixture")
        cache.verify_upload(asset, "scope-fixture")
        pin = {"X-Expected-Node-ID": env.jobs.node_id}
        route = "/games/demo/versions/v1/deploy"
        submitted = []

        def submit(key, value=None):
            body = {"node_id": env.jobs.node_id, "serial": value["serial"] if value else "usb-b", "mode": "check"}
            if value:
                body["device_binding"] = value
            code, result = env.call("POST", route, body, headers={**pin, "X-Request-ID": key})
            self.assertEqual(code, 200, result)
            submitted.append(result["id"])
            return result["id"]

        def started(jid):
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if (env.root / ("started-" + jid)).exists():
                    return
                time.sleep(.025)
            self.fail("synthetic game job did not start: " + json.dumps(env.jobs.status(jid)))

        try:
            a = submit("game-scope-a", bound("A", "usb-a")); started(a)
            alias = submit("game-scope-alias", bound("A", "wifi-a"))
            b = submit("game-scope-b", bound("B", "usb-b")); started(b)
            self.assertEqual(env.jobs.status(a)["state"], "running")
            self.assertIn(a, env.jobs.status(alias)["queue"]["waiting"]["blocking_jobs"])
            self.assertFalse((env.root / ("started-" + alias)).exists())
            self.assertEqual(env.jobs.dispatch.queue.get(a)["resources"], [resource(bound("A", "usb-a"))])
            legacy = submit("game-scope-global")
            old = env.jobs.dispatch.queue.get(legacy)
            self.assertNotIn("device_binding", old["spec"])
            self.assertEqual(old["resources"], sorted(["device:usb-b", env.jobs.games._resource("demo", "v1")]))
            self.assertIn(a, env.jobs.status(legacy)["queue"]["waiting"]["blocking_jobs"])
            before = (env.root / "resolves").read_bytes()
            registry.write_text(json.dumps({"revision": 2, "aliases": {"usb-a": "B"}}))
            env.jobs.deployments.device_binding_available = False
            self.assertEqual(submit("game-scope-a", bound("A", "usb-a")), a)
            self.assertEqual((env.root / "resolves").read_bytes(), before)
            for jid in (alias, legacy):
                self.assertEqual(env.call("POST", "/jobs/" + jid + "/cancel", {}, headers=pin)[0], 200)
            (env.root / "finish-usb-a").touch()
            self.assertEqual(env.finish(a)["queue"]["state"], "blocked")
            current["status"] = "blocked"
            current["device_scopes"]["registry_revision"] = 2
            current["device_scopes"]["devices"][0]["status"] = "blocked"
            current["device_scopes"]["devices"][1]["status"] = "held"
            code, attempt = env.call("POST", "/jobs/" + a + "/deployment/recover", {},
                                     headers={**pin, "X-Request-ID": "game-scope-recover"})
            self.assertEqual(code, 200, attempt)
            recovered = attempt["id"]; submitted.append(recovered)
            self.assertEqual(env.finish(recovered)["exit"], 0)
            actual = json.loads((Path(env.jobs.path(recovered, "out")) / "observed.json").read_text())
            self.assertEqual(actual["binding"], bound("A", "usb-a"))
            self.assertNotIn("--device-binding", actual["argv"])
            self.assertEqual((env.root / "resolves").read_bytes(), before)
            history = env.call("GET", "/games/demo/versions/v1")[1]["deployments"]
            self.assertTrue({a, b, recovered}.issubset({item["id"] for item in history}))
            code, record = env.call("GET", "/jobs/" + recovered + "/deployment")
            self.assertEqual((code, record["target"]["device_binding"], record["report"]["lease_scope"]),
                             (200, bound("A", "usb-a"), "device"))
            self.assertEqual(env.jobs.status(b)["state"], "running")
        finally:
            for serial in ("usb-a", "wifi-a", "usb-b"):
                (env.root / ("finish-" + serial)).touch()
            for row in env.jobs.dispatch.queue.rows(("preparing", "queued")):
                env.jobs.cancel(row["job_id"])
            for jid in set(submitted):
                env.finish(jid)

    def test_frozen_http_request_and_failed_recovery_follow_the_blocking_attempt(self):
        env = self.env
        helper = env.root / "fixture-phonefarm"
        helper.write_text("#!" + sys.executable + "\n" + '''
import json, pathlib, sys
a=sys.argv[1:]
if a[:2]==['game-deploy','capabilities']:
    print(json.dumps({'schema':'phonefarm/game-deploy-capabilities@1','resources_v1':True})); sys.exit()
if a[:2]==['lease','status']:
    print(json.dumps({'schema_version':1,'status':'clear','bridge_held':False,'entries':[],
      'storage':{'schema_version':1,'readiness':'ready','devlock_dir':str(pathlib.Path(__file__).parent/'locks')}})); sys.exit()
out=pathlib.Path(a[a.index('--out')+1]); out.mkdir(exist_ok=True)
if 'recover' in a:
    old=pathlib.Path(a[a.index('--session')+1]); saved=json.loads((old/'fixture.json').read_text())
    count=pathlib.Path(__file__).parent/'recover-count'
    attempt=int(count.read_text())+1 if count.exists() else 1
    count.write_text(str(attempt)); r=saved['request']
else:
    r=json.loads(pathlib.Path(a[a.index('--request')+1]).read_text()); attempt=0
if attempt!=1:
    session=out/'game-deploy'; session.mkdir()
    (session/'fixture.json').write_text(json.dumps({'request':r,'attempt':attempt}))
report={k:r[k] for k in ('node_id','game_id','version','serial','mode')}
report.update(schema='phonefarm/game-deployment@1',phase='restored' if attempt==2 else 'recovery_required',
 observed_at='fixture',session_path='game-deploy',recovery={'status':'reported_complete' if attempt==2 else 'required'})
for k in ('installed','resources_deployed','application_access','login','scene_ready'): report[k]={'status':'unknown'}
if attempt==1:
    report['session_path']=None
    report['recovery'].update(source_session=str(old.resolve()),context_moved=False)
(out/'deployment.json').write_text(json.dumps(report))
sys.exit(0 if attempt==2 else 1)
''')
        helper.chmod(0o700)
        env.jobs.bins["phonefarm"] = (str(helper), str(env.root))
        env.jobs.deployments = NodeDeployments(env.jobs, env.jobs.games)
        self.assertIn("games.deploy-v1", env.call("GET", "/node")[1]["capabilities"])
        manifest = env.manifest(deployment={"schema": "phonefarm/game-resources-recipe@1",
            "package": "test.game", "version_code": 1, "apk_sha256": "a" * 64,
            "files": [{"asset_name": "data", "path": "base/data.pk4"}]})
        self.assertEqual(env.call("POST", "/games", manifest)[0], 201)
        body = {"node_id": env.jobs.games.node_id, "serial": "fixture-android", "mode": "apply"}
        route = "/games/demo/versions/v1/deploy"
        headers = {"X-Request-ID": "deployment-original"}
        self.assertEqual(env.call("POST", route, body, token=readonly_token(env.admin), headers=headers)[0], 401)
        self.assertEqual(env.call("POST", route, body, headers=headers)[1]["code"], "assets_unavailable")
        view = {"Authorization": "Basic " + base64.b64encode(b"viewer:test-view").decode(), "X-HG": "1", **headers}
        self.assertEqual(env.call("POST", "/dash/api" + route, body, headers=view)[0], 403)
        asset = manifest["assets"][0]
        cache = env.jobs.games.cache
        cache.upload_open(asset, "deploy-fixture")
        import io
        cache.upload_chunk(asset, "deploy-fixture", 0, 7, io.BytesIO(b"fixture"))
        cache.upload_seal(asset, "deploy-fixture")
        cache.verify_upload(asset, "deploy-fixture")
        steer = {**view, "Cookie": dash.STEER_COOKIE + "=" + env.board._cookie_val("test-steer", b'hgamey-steer-v1')}
        code, submitted = env.call("POST", "/dash/api" + route, body, headers=steer)
        self.assertEqual(code, 200, submitted)
        original = submitted["id"]

        def finished(jid):
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                status, record = env.call("GET", "/jobs/" + jid + "/deployment")
                if status == 200 and record["job"].get("state") == "finished":
                    return record
                time.sleep(0.025)
            self.fail("deployment fixture did not finish")

        first = finished(original)
        self.assertTrue(first["recovery"]["eligible"], first)
        self.assertEqual(first["target"], {"node_id": body["node_id"], "serial": body["serial"]})
        Path(cache.object_path(asset["sha256"])).unlink()
        self.assertEqual(env.call("POST", route, body, headers=headers)[1]["id"], original)
        self.assertEqual(env.call("POST", route, {**body, "serial": "different-device"}, headers=headers)[0], 409)
        first_bytes = (Path(env.jobs.dir) / original / "out/deployment.json").read_bytes()
        recovery_path = "/jobs/" + original + "/deployment/recover"
        code, child = env.call("POST", recovery_path, {}, headers={"X-Request-ID": "recover-first"})
        self.assertEqual(code, 200, child)
        failed = finished(child["id"])
        self.assertTrue(failed["recovery"]["eligible"], failed)
        self.assertIsNone(failed["report"]["session_path"])
        self.assertFalse(failed["report"]["recovery"]["context_moved"])
        self.assertEqual(env.call("GET", "/jobs/" + original + "/deployment")[1]["recovery"]["target_job_id"], child["id"])
        self.assertEqual(env.call("POST", recovery_path, {}, headers={"X-Request-ID": "recover-first"})[1]["id"], child["id"])
        self.assertEqual(env.call("POST", recovery_path, {}, headers={"X-Request-ID": "wrong-parent"})[0], 409)
        code, last = env.call("POST", "/jobs/" + child["id"] + "/deployment/recover", {},
                              headers={"X-Request-ID": "recover-leaf"})
        self.assertEqual(code, 200, last)
        done = finished(last["id"])
        self.assertEqual((done["mode"], done["report"]["mode"], done["report"]["phase"]), ("recover", "apply", "restored"))
        self.assertFalse(done["recovery"]["eligible"])
        self.assertEqual((Path(env.jobs.dir) / original / "out/deployment.json").read_bytes(), first_bytes)
        history = env.call("GET", "/games/demo/versions/v1")[1]["deployments"]
        self.assertEqual({j["id"] for j in history}, {original, child["id"], last["id"]})
        env.jobs.release_queue(last["id"], "isolated fixture recovery reviewed")
        settled = env.call("GET", "/jobs/" + original + "/deployment")[1]["recovery"]
        self.assertFalse(settled["eligible"])
        self.assertEqual(settled["target_job_id"], last["id"])
        # A foreign device report cannot turn into facts for the selected target.
        report_path = Path(env.jobs.dir) / last["id"] / "out/deployment.json"
        changed = json.loads(report_path.read_text()); changed["serial"] = "foreign"
        report_path.write_text(json.dumps(changed))
        self.assertIsNone(env.call("GET", "/jobs/" + last["id"] + "/deployment")[1]["report"])
