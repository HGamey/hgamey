"""Installation shares the durable queue but retains its own operation identity."""
import io
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from node_deployments import NodeDeployments
import test_node_games as game_fixture


class InstallationFlowTests(unittest.TestCase):
    def test_install_identity_cache_selection_and_recovery_without_metadata_tool(self):
        env = game_fixture.GameFlowTests()
        env.setUp()
        self.addCleanup(env.tearDown)
        helper = env.root / "fixture-phonefarm"
        helper.write_text("#!" + sys.executable + "\n" + '''
import json, pathlib, sys
a=sys.argv[1:]; root=pathlib.Path(__file__).parent
if a[:2]==['game-deploy','capabilities']:
    available=not (root/'tool-missing').exists()
    print(json.dumps({'schema':'phonefarm/game-deploy-capabilities@1','resources_v1':True,
        'fresh_single_apk_v1':True,'metadata_tool':{'available':available,'error':None if available else 'configure AAPT2_BIN'}})); sys.exit()
if a[:2]==['lease','status']:
    print(json.dumps({'schema_version':1,'status':'clear','bridge_held':False,'entries':[],
      'storage':{'schema_version':1,'readiness':'ready','devlock_dir':str(root/'locks')}})); sys.exit()
out=pathlib.Path(a[a.index('--out')+1]); out.mkdir(exist_ok=True)
if 'recover' in a:
    r=json.loads((pathlib.Path(a[a.index('--session')+1])/'fixture.json').read_text())
else: r=json.loads(pathlib.Path(a[a.index('--request')+1]).read_text())
session=out/'game-deploy'; session.mkdir(); (session/'fixture.json').write_text(json.dumps(r))
report={k:r[k] for k in ('node_id','game_id','version','serial','mode')}
report.update(schema='phonefarm/game-installation@1',phase='recovery_required',session_path='game-deploy',
    recovery={'status':'required','scope':'owned_package_install_transaction_settled'},apk={'status':'verified'})
for k in ('installed','resources_deployed','application_access','login','scene_ready'): report[k]={'status':'unknown'}
(out/'deployment.json').write_text(json.dumps(report)); sys.exit(1)
''')
        helper.chmod(0o700)
        env.jobs.bins["phonefarm"] = (str(helper), str(env.root))
        env.jobs.deployments = NodeDeployments(env.jobs, env.jobs.games)
        self.assertIn("games.install-v1", env.call("GET", "/node")[1]["capabilities"])
        manifest = env.manifest()
        asset = manifest["assets"][0]
        asset.update(name="engine", role="apk")
        manifest["assets"].append({**asset, "name": "data", "role": "resource", "sha256": "b" * 64})
        identity = {"package": "test.game", "version_code": 1, "apk_sha256": asset["sha256"]}
        manifest["installation"] = {"schema": "phonefarm/game-install-recipe@1", **identity, "apk_asset_name": "engine"}
        manifest["deployment"] = {"schema": "phonefarm/game-resources-recipe@1", **identity,
                                  "files": [{"asset_name": "data", "path": "base/data.pk4"}]}
        self.assertEqual(env.call("POST", "/games", manifest)[0], 201)
        cache = env.jobs.games.cache
        cache.upload_open(asset, "install-fixture")
        cache.upload_chunk(asset, "install-fixture", 0, 7, io.BytesIO(b"fixture"))
        cache.upload_seal(asset, "install-fixture")
        cache.verify_upload(asset, "install-fixture")
        # Missing game data does not prevent an independently selected engine install.
        body = {"node_id": env.jobs.games.node_id, "serial": "fixture-android", "mode": "apply"}
        route = "/games/demo/versions/v1/install"
        headers = {"X-Request-ID": "install-original"}
        result = env.cli("install", "demo", "v1", "--serial", body["serial"], "--request-id", headers["X-Request-ID"])
        self.assertEqual(result.returncode, 0, result.stderr)
        submitted = json.loads(result.stdout)
        jid = submitted["id"]
        env.finish(jid)
        original = env.call("GET", "/jobs/" + jid + "/deployment")[1]
        self.assertEqual(original["operation"], "install")
        self.assertTrue(original["recovery"]["eligible"], original)
        frozen = json.loads((Path(env.jobs.dir) / jid / "in/manifest.json").read_text())
        self.assertEqual([a["name"] for a in frozen["assets"]], ["engine"])
        self.assertEqual(env.call("POST", route.replace("/install", "/deploy"), body, headers=headers)[0], 409)
        (env.root / "tool-missing").touch()
        env.jobs.deployments = NodeDeployments(env.jobs, env.jobs.games)
        capabilities = env.call("GET", "/node")[1]["capabilities"]
        self.assertNotIn("games.install-v1", capabilities)
        self.assertIn("games.deploy-v1", capabilities)
        self.assertEqual(env.call("POST", route, body, headers={"X-Request-ID": "new-install"})[0], 503)
        Path(cache.object_path(asset["sha256"])).unlink()
        self.assertEqual(env.call("POST", route, body, headers=headers)[1]["id"], jid)
        result = env.cli("deployment-recover", jid, "--request-id", "install-recover")
        self.assertEqual(result.returncode, 0, result.stderr)
        child = json.loads(result.stdout)
        env.finish(child["id"])
        recovered = env.call("GET", "/jobs/" + child["id"] + "/deployment")[1]
        self.assertEqual((recovered["operation"], recovered["mode"]), ("install", "recover"))
        self.assertTrue(recovered["recovery"]["eligible"], recovered)
        record = env.call("GET", "/games/demo/versions/v1")[1]
        self.assertEqual(record["deployments"], [])
        self.assertEqual({j["id"] for j in record["installations"]}, {jid, child["id"]})
        self.assertFalse(record["installation_support"]["available"])
        self.assertTrue(record["installation_support"]["recovery_available"])
        # Wrong-family reports cannot be interpreted as installation facts.
        path = Path(env.jobs.dir) / child["id"] / "out/deployment.json"
        report = json.loads(path.read_text()); report["schema"] = "phonefarm/game-deployment@1"
        path.write_text(json.dumps(report))
        self.assertIsNone(env.call("GET", "/jobs/" + child["id"] + "/deployment")[1]["report"])
