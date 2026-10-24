import base64
import json
from pathlib import Path
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dash
from node_deployments import NodeDeployments
from node_service import readonly_token
import test_node_games as game_fixture


class DeploymentFlowTests(unittest.TestCase):
    def setUp(self):
        self.env = game_fixture.GameFlowTests()
        self.env.setUp()

    def tearDown(self):
        self.env.tearDown()

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
