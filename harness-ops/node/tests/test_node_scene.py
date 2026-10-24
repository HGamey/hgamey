"""Scene input/recovery wiring through HTTP and harmless supervised workers."""
import copy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import job_runtime
from node_deployments import NodeDeployments
import test_node_games as game_fixture


class SceneFlowTests(unittest.TestCase):
    def test_frozen_old_version_inputs_and_recovery_without_external_sources(self):
        env = game_fixture.GameFlowTests()
        env.setUp()
        self.addCleanup(env.tearDown)
        workers = []
        launch = job_runtime.launch

        def tracked_launch(*args, **kwargs):
            worker = launch(*args, **kwargs)
            workers.append(worker)
            return worker

        def finish_workers():
            for worker in workers:
                try:
                    worker.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    worker.terminate()  # Only a Popen handle created by this fixture.
                    worker.wait(timeout=5)

        self.addCleanup(finish_workers)
        launch_patch = patch.object(job_runtime, "launch", side_effect=tracked_launch)
        launch_patch.start()
        self.addCleanup(launch_patch.stop)
        helper = env.root / "fixture-phonefarm"
        helper.write_text("#!" + sys.executable + "\n" + '''
import json, os, pathlib, sys
a=sys.argv[1:]; root=pathlib.Path(__file__).parent
if a[:2]==['game-deploy','capabilities']:
    print(json.dumps({'schema':'phonefarm/game-deploy-capabilities@1','doom_render_scene_v1':True})); sys.exit()
if a[:2]==['lease','status']:
    print(json.dumps({'schema_version':1,'status':'clear','bridge_held':False,'entries':[],
      'storage':{'schema_version':1,'readiness':'ready','devlock_dir':str(root/'isolated-locks')}})); sys.exit()
out=pathlib.Path(a[a.index('--out')+1]); out.mkdir(exist_ok=True)
recover='recover' in a
if recover:
    old=pathlib.Path(a[a.index('--session')+1])
    r=json.loads((old/'fixture.json').read_text())['request']
    assert not (pathlib.Path(os.environ['PF_JOB_IN'])/'source-deployment.json').exists()
    assert not (pathlib.Path(os.environ['PF_JOB_IN'])/'source-session.json').exists()
    observation={'recovery_used_saved_session':True}
else:
    request_path=pathlib.Path(a[a.index('--request')+1]); r=json.loads(request_path.read_text())
    assert pathlib.Path.cwd()==request_path.parent.resolve()
    assert pathlib.Path(os.environ['PF_JOB_IN']).resolve()==pathlib.Path.cwd()
    report=json.loads((request_path.parent/r['deployment']['report_path']).read_text())
    state=json.loads((request_path.parent/r['deployment']['state_path']).read_text())
    assert r['version']=='v2' and report['version']==state['version']=='v1'
    assert r['game_id']==report['game_id']==state['game_id']=='demo'
    assert report['phase']==state['phase']=='committed'
    assert all(pathlib.Path(asset['path']).is_file() for asset in r['assets'])
    observation={'source_version':report['version'],'input_cwd':str(pathlib.Path.cwd())}
session=out/'game-deploy'; session.mkdir()
(session/'fixture.json').write_text(json.dumps({'request':r}))
(out/'observation.json').write_text(json.dumps(observation))
with (root/'fixture-executions.jsonl').open('a') as f:
    f.write(json.dumps({'operation':'recover' if recover else 'scene','job_id':os.environ['PF_JOB_ID']})+'\\n')
report={k:r[k] for k in ('node_id','game_id','version','serial','mode')}
report.update(schema='phonefarm/game-scene@1',phase='observed' if recover else 'recovery_required',
    session_path='game-deploy',recovery={'status':'verified' if recover else 'required'})
for k in ('installed','resources_deployed','application_access','login','scene_ready','input',
          'launcher_configuration','runtime_seed','launch','scene_observation','stop'):
    report[k]={'status':'unknown'}
(out/'deployment.json').write_text(json.dumps(report))
sys.exit(0 if recover else 2)
''')
        helper.chmod(0o700)
        env.jobs.bins.update(phonefarm=(str(helper), str(env.root)), fixture=(sys.executable, str(env.root)))
        env.jobs.deployments = NodeDeployments(env.jobs, env.jobs.games)
        self.assertIn("games.scene-v1", env.call("GET", "/node")[1]["capabilities"])

        identity = {"node_id": env.jobs.games.node_id, "game_id": "demo", "version": "v1",
                    "serial": "fixture-android", "package": "com.karin.idTech4Amm",
                    "version_code": 11073, "apk_sha256": "a" * 64, "phase": "committed"}
        originals = {
            "deployment.json": json.dumps({**identity, "schema": "phonefarm/game-deployment@1",
                                            "recovery": {"status": "verified"}}).encode(),
            "game-deploy/session.json": json.dumps({**identity, "schema": "phonefarm/game-deploy-session@1"}).encode(),
        }
        source_code = ("import os; from pathlib import Path; out=Path(os.environ['PF_JOB_OUT']); "
                       "(out/'game-deploy').mkdir(); " + "; ".join(
                           f"(out/{name!r}).write_bytes({value!r})" for name, value in originals.items()))
        spec = {"bin": "fixture", "argv": ["-c", source_code], "device_access": "none", "resources": []}
        status, source = env.call("POST", "/jobs", headers={"X-Job": json.dumps(spec),
                                                          "X-Request-ID": "scene-source"})
        self.assertEqual(status, 200, source)
        source_meta = env.finish(source["id"])
        self.assertEqual(source_meta["exit"], 0, source_meta)
        self.assertEqual(source_meta["execution"]["owned_process_group"]["status"], "absent")
        sources = {}
        for name, path in (("report", "deployment.json"), ("state", "game-deploy/session.json")):
            status, reference = env.call("GET", f"/jobs/{source['id']}/input-reference?" + urlencode({"path": path}))
            self.assertEqual(status, 200, reference)
            sources[name] = reference["source"]

        old = env.manifest(deployment={"schema": "phonefarm/game-resources-recipe@1",
            "package": identity["package"], "version_code": 11073, "apk_sha256": "a" * 64,
            "files": [{"asset_name": "data", "path": "doom3/base/demo00.pk4"}]})
        self.assertEqual(env.call("POST", "/games", old)[0], 201)
        seed_data = {"fixture.demo": b"small synthetic render demo", "weapon_base.script": b"synthetic script"}
        assets = [{"name": name, "role": "resource", "sha256": hashlib.sha256(data).hexdigest(),
                   "size": len(data), "source": {"kind": "unavailable", "reason": "synthetic test only"}}
                  for name, data in seed_data.items()]
        recipe = {"schema": "phonefarm/game-scene-recipe@1", "adapter": "doom3-v73-render-demo-v1",
                  "package": identity["package"], "version_code": 11073, "apk_sha256": "a" * 64,
                  "resource_prefix": "doom3", "demo_name": "fixture", "demo_asset_name": "fixture.demo",
                  "compat_asset_name": "weapon_base.script", "renderer": {"opengl": "GLES3.0", "multithread": True},
                  "wait_seconds": 1}
        self.assertEqual(env.call("POST", "/games", env.manifest(version="v2", assets=assets, scene_attempt=recipe))[0], 201)
        cache = env.jobs.games.cache
        for index, asset in enumerate(assets):
            upload_id = "scene-seed-" + str(index)
            cache.upload_open(asset, upload_id)
            cache.upload_chunk(asset, upload_id, 0, asset["size"], io.BytesIO(seed_data[asset["name"]]))
            cache.upload_seal(asset, upload_id)
            cache.verify_upload(asset, upload_id)
        body = {"node_id": identity["node_id"], "serial": identity["serial"], "mode": "apply", "deployment": sources}
        route, headers = "/games/demo/versions/v2/scene", {"X-Request-ID": "scene-original"}
        before = len(env.jobs.dispatch.queue.rows())
        bad = copy.deepcopy(body); bad["deployment"]["state"]["path"] = "different-session.json"
        self.assertEqual(env.call("POST", route, bad, headers={"X-Request-ID": "scene-wrong-path"})[0], 400)
        bad = copy.deepcopy(body)
        bad["deployment"]["state"]["job_id"] = "ffffffffffff" if source["id"] != "ffffffffffff" else "eeeeeeeeeeee"
        self.assertEqual(env.call("POST", route, bad, headers={"X-Request-ID": "scene-cross-job"})[0], 400)
        self.assertEqual(len(env.jobs.dispatch.queue.rows()), before)
        status, first = env.call("POST", route, body, headers=headers)
        self.assertEqual(status, 200, first)
        jid = first["id"]
        self.assertEqual(env.finish(jid)["exit"], 2)
        status, scene = env.call("GET", f"/jobs/{jid}/deployment")
        self.assertEqual(status, 200, scene)
        self.assertTrue(scene["recovery"]["eligible"], scene)
        declared = env.jobs.dispatch.queue.get(jid)["spec"]["inputs"]
        self.assertEqual(declared["cwd"], "input")
        self.assertEqual({f["name"]: f["source"] for f in declared["files"]}, sources)
        preparation = env.call("GET", f"/jobs/{jid}/inputs")[1]["preparation"]
        self.assertEqual(preparation["status"], "ready")
        for name, path in (("source-deployment.json", "deployment.json"), ("source-session.json", "game-deploy/session.json")):
            self.assertEqual(Path(env.jobs.path(jid, "in", name)).read_bytes(), originals[path])
            self.assertEqual(Path(env.jobs.path(source["id"], "out", path)).read_bytes(), originals[path])

        # Preserve the original bytes away from their public source/cache paths.
        # Recovery must use the scene's saved context, not prepare these inputs again.
        preserved = env.root / "preserved-external-sources"
        preserved.mkdir()
        for index, path in enumerate(originals):
            Path(env.jobs.path(source["id"], "out", path)).rename(preserved / f"source-{index}.json")
        for index, asset in enumerate(assets):
            Path(cache.object_path(asset["sha256"])).rename(preserved / f"seed-{index}")
        self.assertEqual(env.call("GET", f"/jobs/{source['id']}/input-reference?path=deployment.json")[0], 404)
        self.assertEqual(env.call("GET", "/requests/scene-original")[1]["id"], jid)
        references = env.root / "references.json"
        references.write_text(json.dumps(sources))
        repeated = env.cli("scene", "demo", "v2", "--serial", identity["serial"],
                           "--deployment-inputs", str(references), "--request-id", "scene-original")
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual(json.loads(repeated.stdout)["id"], jid)
        first_report = Path(env.jobs.path(jid, "out", "deployment.json")).read_bytes()
        recovery_route = f"/jobs/{jid}/deployment/recover"
        status, child = env.call("POST", recovery_route, {}, headers={"X-Request-ID": "scene-recover"})
        self.assertEqual(status, 200, child)
        self.assertNotIn("inputs", env.jobs.dispatch.queue.get(child["id"])["spec"])
        self.assertEqual(env.finish(child["id"])["exit"], 0)
        status, recovered = env.call("GET", f"/jobs/{child['id']}/deployment")
        self.assertEqual(status, 200, recovered)
        self.assertEqual((recovered["operation"], recovered["mode"], recovered["report"]["recovery"]["status"]),
                         ("scene", "recover", "verified"))
        self.assertFalse(recovered["recovery"]["eligible"])
        self.assertEqual(env.call("POST", recovery_route, {}, headers={"X-Request-ID": "scene-recover"})[1]["id"], child["id"])
        self.assertEqual(Path(env.jobs.path(jid, "out", "deployment.json")).read_bytes(), first_report)
        executions = [json.loads(line) for line in (env.root / "fixture-executions.jsonl").read_text().splitlines()]
        self.assertEqual(executions, [{"operation": "scene", "job_id": jid}, {"operation": "recover", "job_id": child["id"]}])
        self.assertEqual(len(env.jobs.dispatch.queue.rows()), 3)


if __name__ == "__main__":
    unittest.main()
