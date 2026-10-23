import json
import os
from pathlib import Path
import subprocess
import sys
from unittest import TestCase, mock

import test_node_read as read_fixture
from node_session import SessionError


class JobSessionTests(TestCase):
    setUp = read_fixture.NodeReadTests.setUp
    tearDown = read_fixture.NodeReadTests.tearDown
    fixture = read_fixture.NodeReadTests.fixture
    request = read_fixture.NodeReadTests.request
    get_json = read_fixture.NodeReadTests.get_json

    def prepare(self, number=1):
        jid = self.fixture(number, bin="phonefarm", argv=["gray-layer", "session", "--serial", "fake-device"])
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
        row, _ = self.jobs.dispatch.queue.reserve("source", "fixture", {"bin": "phonefarm"}, ["phone:fake-device"])
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
