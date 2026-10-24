"""Initialization does not require cached resources or imply a playable scene."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from node_deployments import NodeDeployments
import test_node_games as game_fixture


class InitializationFlowTests(unittest.TestCase):
    def test_cli_initialization_keeps_missing_directory_separate_from_recovery(self):
        env = game_fixture.GameFlowTests()
        env.setUp()
        self.addCleanup(env.tearDown)
        helper = env.root / "fixture-phonefarm"
        helper.write_text("#!" + sys.executable + "\n" + '''
import json, pathlib, sys
a=sys.argv[1:]
if a[:2]==['game-deploy','capabilities']:
    print(json.dumps({'schema':'phonefarm/game-deploy-capabilities@1','launcher_initialization_v1':True})); sys.exit()
if a[:2]==['lease','status']:
    print(json.dumps({'schema_version':1,'status':'clear','bridge_held':False,'entries':[],
      'storage':{'schema_version':1,'readiness':'ready','devlock_dir':str(pathlib.Path(__file__).parent/'locks')}})); sys.exit()
r=json.loads(pathlib.Path(a[a.index('--request')+1]).read_text())
assert r['schema']=='phonefarm/game-initialize-request@1' and r['assets']==[]
out=pathlib.Path(a[a.index('--out')+1]); out.mkdir(exist_ok=True)
report={k:r[k] for k in ('node_id','game_id','version','serial','mode')}
report.update(schema='phonefarm/game-initialization@1',phase='needs_assistance',session_path=None,
    recovery={'status':'verified','scope':'owned_launcher_attempt_force_stopped'},
    storage_initialization={'status':'needs_assistance'},launch={'status':'confirmed'},
    stop={'status':'confirmed','all_target_processes_gone':'unknown'})
for k in ('installed','resources_deployed','application_access','login','scene_ready'): report[k]={'status':'unknown'}
(out/'deployment.json').write_text(json.dumps(report)); sys.exit(2)
''')
        helper.chmod(0o700)
        env.jobs.bins["phonefarm"] = (str(helper), str(env.root))
        env.jobs.deployments = NodeDeployments(env.jobs, env.jobs.games)
        caps = env.call("GET", "/node")[1]["capabilities"]
        self.assertIn("games.initialize-v1", caps)
        self.assertNotIn("games.install-v1", caps)
        recipe = {"schema": "phonefarm/game-initialize-recipe@1", "package": "test.game",
                  "version_code": 1, "apk_sha256": "a" * 64, "wait_seconds": 1}
        manifest = env.manifest(initialization=recipe)
        self.assertEqual(env.call("POST", "/games", {**manifest, "initialization": {**recipe, "component": "test.game/Main"}})[0], 400)
        self.assertEqual(env.call("POST", "/games", {**manifest, "initialization": {**recipe, "wait_seconds": 0}})[0], 400)
        self.assertEqual(env.call("POST", "/games", manifest)[0], 201)
        result = env.cli("initialize", "demo", "v1", "--serial", "fixture-android", "--request-id", "initialize-original")
        self.assertEqual(result.returncode, 0, result.stderr)
        jid = json.loads(result.stdout)["id"]
        env.finish(jid)
        record = env.call("GET", "/jobs/" + jid + "/deployment")[1]
        self.assertEqual(record["operation"], "initialize")
        self.assertEqual(record["report"]["phase"], "needs_assistance")
        self.assertEqual(record["report"]["stop"]["status"], "confirmed")
        self.assertEqual(record["report"]["scene_ready"]["status"], "unknown")
        self.assertFalse(record["recovery"]["eligible"])
        self.assertEqual(record["job"]["queue"]["state"], "blocked")
        # Local assistance is an experiment decision; it is not automatic queue release.
        version = env.call("GET", "/games/demo/versions/v1")[1]
        self.assertEqual(version["resource_state"], "incomplete")
        self.assertEqual([r["id"] for r in version["initializations"]], [jid])
        self.assertEqual(version["installations"], [])
        self.assertEqual(version["deployments"], [])
        result = env.cli("initialize", "demo", "v1", "--serial", "fixture-android", "--request-id", "initialize-original")
        self.assertEqual(json.loads(result.stdout)["id"], jid)
