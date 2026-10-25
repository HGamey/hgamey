import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dash
from node_service import Jobs, make_handler, readonly_token
from node_read import NodeRead
from node_fleet import FleetState


class NoMcp:
    def handle(self, msg, allow):
        raise AssertionError("host-only reads must not invoke phonefarm")


class NodeReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.jobs = Jobs(str(self.root), {})
        self.node = NodeRead(self.jobs, self.jobs.data_root, str(self.root))
        self.admin = "unit-test-admin-credential-not-production"
        self.ro = readonly_token(self.admin)
        self.pw = "unit-test-dash-password"
        # Use actual dashboard authentication and routing, without starting samplers.
        self.board = dash.Dash.__new__(dash.Dash)
        self.board._pass = lambda: self.pw
        self.board._steer_pass = lambda: "test-steer-password"
        self.board.job_service = self.jobs
        self.board.last_view = 0
        self.board.wake = threading.Event()
        self.board.monitor = FleetState(str(self.root), str(self.root / "locks"), str(self.root / "shots"))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(
            NoMcp(), self.jobs, self.admin, self.board, self.node))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.jobs.directory.close()
        self.board.monitor.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def fixture(self, n, **extra):
        jid = f"{n:012x}"
        p = Path(self.jobs.path(jid))
        (p / "out" / "nested").mkdir(parents=True)
        (p / "log").write_bytes(b"example log\n" * 10000)
        (p / "out" / "nested" / "result.json").write_text('{"metric": 2.413144}\n')
        (p / "out" / "image.bin").write_bytes(bytes(range(256)))
        self.jobs._write_meta(jid, {"id": jid, "bin": "custom", "argv": ["custom-analysis"],
                                   "label": "Doom study", "started": n, "exit": 0, **extra})
        return jid

    def request(self, path, *, method="GET", token=None, headers=None, body=None):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        h = {"Authorization": "Bearer " + (token or self.ro), **(headers or {})}
        conn.request(method, path, body=body, headers=h)
        res = conn.getresponse()
        status, response_headers, content = res.status, dict(res.getheaders()), res.read()
        conn.close()
        return status, response_headers, content

    def get_json(self, path, **kw):
        status, _, body = self.request(path, **kw)
        self.assertEqual(status, 200, body)
        return json.loads(body)

    def test_identity_survives_restart_and_data_root_is_explicit(self):
        again = NodeRead(self.jobs, self.jobs.data_root, str(self.root))
        self.assertEqual(again.node_id, self.node.node_id)
        identity = self.get_json("/node")
        self.assertEqual(identity["node_id"], self.node.node_id)
        self.assertEqual(identity["storage"]["jobs_root"], self.jobs.dir)
        self.assertEqual(identity["devices"]["status"], "not_probed")
        self.assertIsNone(identity["service"]["revision"])
        separate = Jobs(str(self.root), {}, str(self.root / "runtime"))
        self.assertEqual(separate.dir, str(self.root / "runtime" / "jobs"))
        self.assertTrue((self.root / "_jobs").is_dir())

    def test_registered_node_web_and_token_clients_share_cached_directory(self):
        remote_root = self.root / "peer"
        remote_root.mkdir()
        remote_jobs = Jobs(str(remote_root), {})
        remote_node = NodeRead(remote_jobs, remote_jobs.data_root, str(remote_root))
        remote_fleet = FleetState(str(remote_root), str(remote_root / "locks"), str(remote_root / "shots"))
        remote_token = "isolated-directory-peer-admin"
        remote_server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(
            NoMcp(), remote_jobs, remote_token, node=remote_node, fleet=remote_fleet))
        remote_thread = threading.Thread(target=remote_server.serve_forever, daemon=True)
        remote_thread.start()
        view = {"Cookie": f"{dash.COOKIE}={self.board._cookie_val(self.pw)}",
                "X-HG": "1", "Content-Type": "application/json"}
        steer = {**view, "Cookie": view["Cookie"] + "; " + dash.STEER_COOKIE + "=" +
                 self.board._cookie_val(self.board._steer_pass(), b"hgamey-steer-v1")}
        credential = readonly_token(remote_token)
        body = json.dumps({"name": "peer", "url": "http://127.0.0.1:" + str(remote_server.server_port),
                           "token": credential, "tags": ["isolated"]})

        def settled():
            until = time.monotonic() + 4
            while time.monotonic() < until:
                entry = self.get_json("/nodes/peer")
                if not entry["refreshing"]:
                    return entry
                time.sleep(.02)
            self.fail("isolated node directory refresh did not settle")

        try:
            self.assertEqual(self.request("/nodes", method="POST", body=body,
                                          headers={"Content-Type": "application/json"})[0], 401)
            self.assertEqual(self.request("/dash/api/nodes", method="POST", body=body, headers=view)[0], 403)
            status, _, created = self.request("/dash/api/nodes", method="POST", body=body, headers=steer)
            self.assertEqual(status, 201, created)
            self.assertNotIn(credential, created.decode())
            self.assertIsNone(json.loads(created)["snapshot"])
            self.assertIsNone(self.get_json("/fleet?cached=1")["devices"])
            self.assertIsNone(self.board.monitor._thread)
            self.get_json("/nodes?refresh=1")
            ready = settled()
            self.assertEqual(ready["connection"]["status"], "ready", ready)
            self.assertEqual(ready["node_id"], remote_node.node_id)
            self.assertIsNone(ready["snapshot"]["fleet"]["devices"])
            self.assertIsNone(remote_fleet._thread, "directory polling must not activate device sampler")
            self.assertEqual(ready, self.get_json("/dash/api/nodes/peer", headers=view))
            self.assertEqual(self.get_json("/nodes")["local"]["node_id"], self.node.node_id)
            self.assertEqual(ready["snapshot"]["identity"]["storage"]["data_root"], remote_jobs.data_root)

            # Explicit device refresh uses the existing remote sampler route, not a job or MCP call.
            sampled = {**remote_fleet.snapshot(), "devices": [], "sampled_at": time.time(), "stale": False}
            with patch.object(remote_fleet, "request", return_value=sampled) as request:
                self.get_json("/nodes/peer?devices=1")
                self.assertEqual(settled()["snapshot"]["fleet"]["devices"], [])
                request.assert_called_once_with(5.0)

            update = json.dumps({"revision": ready["revision"], "enabled": False})
            code, _, data = self.request("/nodes/peer", method="POST", token=self.admin,
                                         body=update, headers={"Content-Type": "application/json"})
            self.assertEqual(code, 200, data)
            disabled = json.loads(data)
            self.assertEqual(disabled["connection"]["status"], "disabled")
            self.assertTrue(disabled["stale"])
            self.assertEqual(disabled["snapshot"]["identity"]["node_id"], remote_node.node_id)
            self.assertEqual(self.request("/nodes/peer", method="POST", token=self.admin,
                                          body=update, headers={"Content-Type": "application/json"})[0], 409)
            self.assertEqual(self.get_json("/nodes/peer?devices=1")["connection"]["status"], "disabled")
        finally:
            remote_jobs.directory.close()
            remote_fleet.close()
            remote_server.shutdown()
            remote_server.server_close()
            remote_thread.join()

    def test_arbitrary_old_failed_jobs_are_paginated_and_searchable(self):
        for i in range(1, 206):
            self.fixture(i, exit=7 if i == 1 else 0)
        seen, cursor = [], None
        while True:
            query = {"limit": 50, "query": "custom-analysis"}
            if cursor:
                query["cursor"] = cursor
            page = self.get_json("/jobs?" + urlencode(query))
            seen.extend(item["id"] for item in page["items"])
            cursor = page["next_cursor"]
            if not cursor:
                break
        self.assertEqual(len(seen), 205)
        self.assertEqual(len(set(seen)), 205)
        oldest = self.get_json("/jobs?query=000000000001")["items"]
        self.assertEqual(oldest[0]["exit"], 7)
        self.assertEqual(oldest[0]["state"], "finished")

    def test_cursor_stays_on_query_and_missing_metadata_is_visible(self):
        for i in range(1, 4):
            self.fixture(i)
        (Path(self.jobs.dir) / "eeeeeeeeeeee").mkdir()
        (Path(self.jobs.dir) / "eeeeeeeeeeee" / "meta.json").write_text("{")
        page = self.get_json("/jobs?limit=1")
        self.assertEqual(page["warning_count"], 1)
        self.assertEqual(page["warnings"][0]["job"], "eeeeeeeeeeee")
        cursor = page["next_cursor"]
        self.fixture(4)  # Newer insertion must not duplicate the previous page.
        following = self.get_json("/jobs?" + urlencode({"cursor": cursor, "limit": 50}))
        self.assertEqual([m["started"] for m in following["items"]], [2, 1])
        for q in ({"cursor": cursor, "query": "changed"}, {"limit": 201}, {"cursor": "!bad"}, {"state": "success"}):
            self.assertEqual(self.request("/jobs?" + urlencode(q))[0], 400)

    def test_readonly_can_read_bounded_log_artifacts_and_ranged_output(self):
        jid = self.fixture(1)
        self.assertEqual(self.get_json(f"/jobs/{jid}")["id"], jid)
        entries = self.get_json(f"/jobs/{jid}/artifacts")["items"]
        self.assertEqual([e["name"] for e in entries], ["image.bin", "nested"])
        meta = self.get_json(f"/jobs/{jid}/out?path=image.bin&meta=1")
        self.assertEqual(meta["sha256"], hashlib.sha256(bytes(range(256))).hexdigest())
        status, h, body = self.request(f"/jobs/{jid}/out?path=image.bin", headers={"Range": "bytes=10-19"})
        self.assertEqual((status, body), (206, bytes(range(10, 20))))
        self.assertEqual(h["Content-Disposition"], "attachment")
        self.assertEqual(h["X-Content-Type-Options"], "nosniff")
        self.assertEqual(len(self.request(f"/jobs/{jid}/log?offset=10&limit=65536")[2]), 65536)
        self.assertEqual(self.request(f"/jobs/{jid}/log?offset=-1")[0], 400)
        self.assertEqual(self.request(f"/jobs/{jid}/out?path=image.bin&length=-1&offset=0")[0], 416)
        self.assertEqual(self.request(f"/jobs/{jid}/out?path=image.bin", headers={"Range": "bytes=20-10"})[0], 416)
        self.assertEqual(self.request(f"/jobs/{jid}/out?path=image.bin&etag=old")[0], 409)

    def test_job_lease_receipts_keep_scoped_evidence_separate_from_recovery(self):
        jid = self.fixture(1, recovery={"status": "required", "reason": "fixture needs review"})
        self.assertIn("jobs.leases-v1", self.get_json("/node")["capabilities"])
        self.assertEqual(self.get_json(f"/jobs/{jid}/leases")["status"], "not_reported")
        directory = Path(self.jobs.path(jid, "out", "lease-receipts"))
        directory.mkdir()
        receipt = {"schema": "phonefarm/job-lease@1", "job_id": jid, "lease_id": "fixture-1",
                   "serial": "fake-device", "label": "helper-only", "state": "released",
                   "updated_at": "2026-10-22T13:00:00Z", "recovery_of": None,
                   "recovery": {"status": "reported_complete", "scope": "helper_retirement",
                                "evidence_origin": "synthetic", "evidence": ["/not-a-readable-node-path/evidence.json"]}}
        (directory / "fixture-1.json").write_text(json.dumps({**receipt, "token": "must-not-be-indexed"}))
        (directory / "wrong-job.json").write_text(json.dumps({**receipt, "job_id": "a" * 12, "lease_id": "wrong-job"}))
        (directory / "broken.json").write_text("{")
        (directory / "link.json").symlink_to(directory / "fixture-1.json")
        auth = {"Cookie": f"{dash.COOKIE}={self.board._cookie_val(self.pw)}"}
        response = self.get_json(f"/jobs/{jid}/leases")
        self.assertEqual(response, self.get_json(f"/dash/api/jobs/{jid}/leases", headers=auth))
        self.assertEqual(response["status"], "partial")
        self.assertEqual(len(response["errors"]), 3)
        self.assertEqual(len(response["entries"]), 1)
        entry = response["entries"][0]
        self.assertNotIn("token", entry)
        self.assertEqual(entry["recovery"], {**receipt["recovery"], "reason": None, "context": None})
        self.assertEqual(entry["artifact_path"], "lease-receipts/fixture-1.json")
        raw = self.request(f"/jobs/{jid}/out?" + urlencode({"path": entry["artifact_path"]}))[2]
        self.assertEqual(json.loads(raw)["lease_id"], "fixture-1")
        self.assertEqual(self.get_json(f"/jobs/{jid}")["recovery"]["status"], "required")
        self.assertEqual(self.request(f"/dash/api/jobs/{jid}/leases")[0], 401)
        self.assertEqual(self.request(f"/jobs/{jid}/leases", token="wrong")[0], 401)
        self.assertEqual(self.request("/jobs/ffffffffffff/leases")[0], 404)
        self.assertFalse(self.board.wake.is_set())

    def test_readonly_and_dash_password_cannot_mutate(self):
        jid = self.fixture(1)
        for path in ("/jobs", f"/jobs/{jid}/kill", f"/jobs/{jid}/cancel", f"/jobs/{jid}/queue-release", "/admin/restart"):
            self.assertEqual(self.request(path, method="POST", body=b"")[0], 401)
        self.assertEqual(self.request("/node", token="wrong")[0], 401)
        self.assertEqual(self.request("/jobs", method="DELETE")[0], 401)
        self.assertEqual(self.request(f"/jobs/{jid}", headers={"Authorization": "Basic " +
            base64.b64encode(("user:" + self.pw).encode()).decode()})[0], 401)

    def test_dashboard_uses_same_catalog_without_waking_phone_sampler(self):
        jid = self.fixture(1)
        auth = {"Cookie": f"{dash.COOKIE}={self.board._cookie_val(self.pw)}"}
        self.assertEqual(self.get_json("/dash/api/node", headers=auth)["node_id"], self.node.node_id)
        for path in ("/jobs", f"/jobs/{jid}", f"/jobs/{jid}/artifacts"):
            self.assertEqual(self.get_json(path), self.get_json("/dash/api" + path, headers=auth))
        self.assertEqual(self.request(f"/dash/api/jobs/{jid}/out?path=image.bin", headers=auth)[2], bytes(range(256)))
        self.assertEqual(self.request("/dash/api/jobs")[0], 401)
        access = self.get_json("/dash/api/access", headers=auth)
        self.assertEqual(access, {"steer": False, "steer_configured": True})
        write_headers = {**auth, "X-HG": "1", "Content-Type": "application/json"}
        self.assertEqual(self.request(f"/dash/api/jobs/{jid}/cancel", method="POST",
                                      headers=write_headers, body=b'{}')[0], 403)
        write_headers["Cookie"] += "; " + dash.STEER_COOKIE + "=" + self.board._cookie_val(
            self.board._steer_pass(), b"hgamey-steer-v1")
        code, _, content = self.request(f"/dash/api/jobs/{jid}/cancel", method="POST",
                                         headers=write_headers, body=b'{}')
        self.assertEqual(code, 200)
        self.assertFalse(json.loads(content)["accepted"])  # Terminal legacy job, never signal a saved PID.
        self.assertEqual(self.request(f"/jobs/{jid}/cancel", token=self.admin, method="POST",
                                      body=b'{"force":"yes"}')[0], 400)
        # Static files may be absent during development, but route must never wake samplers.
        self.request("/dash/jobs", headers=auth)
        self.request("/dash/static/jobs.js", headers=auth)
        self.assertEqual(self.board.last_view, 0)
        self.assertFalse(self.board.wake.is_set())

    def test_queue_submission_lookup_and_recovery_release_share_permissions(self):
        self.jobs.bins["phonefarm"] = (sys.executable, str(self.root))
        auth = {"Cookie": f"{dash.COOKIE}={self.board._cookie_val(self.pw)}"}
        write = {**auth, "X-HG": "1", "Content-Type": "application/json", "X-Request-ID": "web:fixture"}
        spec = b'{"bin":"phonefarm","argv":["-c","pass"]}'
        self.assertEqual(self.request("/dash/api/jobs", method="POST", headers=write, body=spec)[0], 403)
        write["Cookie"] += "; " + dash.STEER_COOKIE + "=" + self.board._cookie_val(
            self.board._steer_pass(), b"hgamey-steer-v1")
        # Exercise actual admission/auth without launching a fixture command.
        with patch.object(self.jobs.dispatch, "start"):
            self.assertEqual(self.request("/dash/api/jobs", method="POST", headers=write, body=b'{}')[0], 400)
            job = self.get_json("/dash/api/jobs", method="POST", headers=write, body=spec)
            jid = job["id"]
            self.assertEqual(job["phase"], "queued")
            self.assertEqual(self.get_json("/dash/api/jobs", method="POST", headers=write, body=spec)["id"], jid)
            self.assertEqual(self.request("/dash/api/jobs", method="POST", headers=write,
                body=b'{"bin":"phonefarm","argv":["-c","print(1)"]}')[0], 409)
            for prefix, headers in (("", {}), ("/dash/api", auth)):
                self.assertEqual(self.get_json(prefix + "/requests/web%3Afixture", headers=headers)["id"], jid)
                self.assertEqual(self.get_json(prefix + "/queue", headers=headers)["queued_count"], 1)
                self.assertEqual(self.request(prefix + "/requests/absent", headers=headers)[0], 404)
            self.assertEqual(self.request("/queue", token="wrong")[0], 401)
            self.assertEqual(self.request("/dash/api/queue")[0], 401)
            meta = self.jobs._read_meta(jid)
            meta.update(state="finished", phase="interrupted", outcome="interrupted", exit=-1,
                        recovery={"status": "required", "reason": "fixture worker lost"})
            self.jobs._write_meta(jid, meta)
            self.jobs.dispatch.queue.set_state(jid, "blocked")
            self.assertEqual(self.get_json("/queue")["blocked_count"], 1)
            # A missing/incompatible host lease reader must not become clear.
            for prefix, headers in (("", {}), ("/dash/api", auth)):
                self.assertEqual(self.get_json(prefix + "/leases", headers=headers)["status"], "unknown")
            self.assertEqual(self.request("/dash/api/leases")[0], 401)
            self.assertEqual(self.request("/leases", token="wrong")[0], 401)
            for path, headers in ((f"/jobs/{jid}/queue-release", {}),
                                  (f"/dash/api/jobs/{jid}/queue-release", {**auth, "X-HG": "1", "Content-Type": "application/json"})):
                self.assertIn(self.request(path, method="POST", headers=headers, body=b'{"note":"checked"}')[0], (401, 403))
            self.assertEqual(self.request(f"/dash/api/jobs/{jid}/queue-release", method="POST", headers=write, body=b'{}')[0], 400)
            self.assertEqual(self.request(f"/dash/api/jobs/{jid}/queue-release", method="POST", headers=write,
                body=b'{"note":"note alone is insufficient"}')[0], 409)
            recovery = {"bin": "phonefarm", "argv": ["-c", "pass"], "device_access": "recovery", "recovery_for": jid}
            child = self.get_json("/jobs", method="POST", token=self.admin,
                headers={"X-Job": json.dumps(recovery), "X-Request-ID": "recovery:fixture"})
            self.assertEqual(child["queue"]["resources"], job["queue"]["resources"])
            self.assertEqual(child["recovery_for"], jid)
            with patch.object(self.jobs.leases, "snapshot", return_value={"schema_version": 1, "status": "clear", "entries": []}):
                self.assertEqual(self.request(f"/dash/api/jobs/{jid}/queue-release", method="POST", headers=write,
                    body=b'{"note":"child still pending"}')[0], 409)
                self.get_json(f"/jobs/{child['id']}/cancel", method="POST", token=self.admin, body=b'{}')
                self.assertEqual(self.request(f"/dash/api/jobs/{jid}/queue-release", method="POST", headers=write,
                    body=b'{"note":"lease clear cannot prove lost host finished"}')[0], 409)
                unknown_id = jid
                self.assertIsNone(self.jobs.dispatch.queue.get(jid)["host_capacity_release"])
                # A separate synthetic completed-worker receipt exercises the
                # positive review path; the original unknown job stays blocked.
                known = self.get_json("/dash/api/jobs", method="POST",
                    headers={**write, "X-Request-ID": "web:completed-fixture"}, body=spec)
                jid = known["id"]
                meta = self.jobs._read_meta(jid)
                meta.update(state="finished", phase="finished", outcome="failed", exit=1, ended=time.time(),
                            recovery={"status": "required", "reason": "fixture device still needs review"})
                meta["execution"].update(child_pid=424242, returncode=1,
                    owned_process_group={"scope": "original_process_group", "status": "absent",
                                         "pgid": 424242, "checked_at": time.time(), "error": None})
                self.jobs._write_meta(jid, meta)
                self.jobs._read_root.write_json(jid + "/exitcode", 1)
                self.jobs.dispatch.queue.set_state(jid, "claimed")
                self.assertTrue(self.jobs.dispatch.finalize(self.jobs.dispatch.queue.get(jid)))
                receipt = self.get_json(f"/dash/api/jobs/{jid}/queue-release", method="POST", headers=write, body=b'{"note":"checked fixture resource"}')
            self.assertEqual(receipt["job"]["queue"]["state"], "done")
            self.assertEqual(receipt["job"]["queue"]["release"]["note"], "checked fixture resource")
            self.assertEqual(receipt["job"]["queue"]["release"]["lease_observation"]["status"], "clear")
            self.assertEqual(receipt["job"]["recovery"]["status"], "required")
            self.assertEqual(self.get_json("/queue")["blocked_count"], 1)
            self.assertEqual(self.jobs.dispatch.queue.get(unknown_id)["state"], "blocked")
            self.assertEqual(self.request("/jobs", method="POST", token=self.admin,
                headers={"X-Job": json.dumps({**recovery, "recovery_for": jid}), "X-Request-ID": "new-recovery"})[0], 409)
            self.assertEqual(self.get_json("/jobs", method="POST", token=self.admin,
                headers={"X-Job": json.dumps(recovery), "X-Request-ID": "recovery:fixture"})["id"], child["id"])
            self.assertEqual(self.request(f"/jobs/{jid}/queue-release", method="POST", token=self.admin,
                body=b'{"note":"already released"}')[0], 409)
            # SQLite-only failed preparation remains visible and can only be finalized by writers.
            self.board.jobs_dir = self.jobs.dir
            self.assertIn("jobs.durability-v1", self.get_json("/node")["capabilities"])
            host = {"bin": "host", "argv": [], "device_access": "none"}
            for n, prefix in enumerate(("", "/dash/api")):
                row, _ = self.jobs.dispatch.queue.reserve(f"durability:{n}", f"fixture:{n}", host, [f"host:{n}"])
                fid = row["job_id"]
                self.jobs.dispatch.queue.record_unstarted(fid, "failed", "fixture preparation failed")
                self.assertFalse(Path(self.jobs.path(fid)).exists())
                listed = next(m for m in self.get_json("/jobs")["items"] if m["id"] == fid)
                mirrored = next(m for m in self.get_json("/dash/api/jobs", headers=auth)["items"] if m["id"] == fid)
                maintained = next(m for m in self.board.jobs() if m["id"] == fid)
                self.assertEqual(listed["queue"]["state_reason"]["kind"], "durability")
                for view in (listed, mirrored, maintained):
                    self.assertEqual((view["phase"], view["state"]), ("storage_blocked", "running"))
                    self.assertEqual(view["recovery"], maintained["recovery"])
                suffix = f"/jobs/{fid}/retry-finalize"
                self.assertEqual(self.request(suffix, method="POST", body=b'{}')[0], 401)
                self.assertEqual(self.request("/dash/api" + suffix, method="POST",
                    headers={**auth, "X-HG": "1", "Content-Type": "application/json"}, body=b'{}')[0], 403)
                credentials = {"token": self.admin} if not prefix else {"headers": write}
                finalized = self.get_json(prefix + suffix, method="POST", body=b'{}', **credentials)
                self.assertTrue(finalized["storage_committed"])
                self.assertEqual(finalized["job"]["outcome"], "failed")
                self.assertEqual(finalized["job"]["recovery"]["status"], "not_needed")
                self.assertEqual(finalized["job"]["queue"]["state"], "done")
                self.assertFalse((Path(self.jobs.path(fid)) / "launch.json").exists())
        self.assertEqual(self.board.last_view, 0)
        self.assertFalse(self.board.wake.is_set())

    def test_fleet_readers_share_one_sampler_without_jobs_or_screenshots(self):
        monitor = self.board.monitor
        self.get_json("/node")
        self.get_json("/jobs")
        self.assertIsNone(monitor._thread)
        self.assertEqual(self.request("/fleet", token="wrong")[0], 401)
        self.assertIsNone(monitor._thread)
        auth = {"Cookie": f"{dash.COOKIE}={self.board._cookie_val(self.pw)}"}
        sample = {"schema": "phonefarm/fleet@1", "devices": [{"serial": "fixture-only", "online": True,
                               "thermal": {"soc_max_c": 42}}], "notes": []}
        with patch("node_fleet.subprocess.run", return_value=subprocess.CompletedProcess(
                [], 0, json.dumps(sample).encode(), b"")) as run:
            first = self.get_json("/fleet?wait=2")
            second = self.get_json("/dash/api/fleet?wait=2", headers=auth)
            self.assertEqual(first["devices"], sample["devices"])
            self.assertEqual(first["sampled_at"], second["sampled_at"])
            self.assertEqual(first["node_id"], self.node.node_id)
            self.assertFalse(first["stale"])
            self.assertEqual(run.call_count, 1)
            self.assertNotIn("--screenshot-dir", run.call_args.args[0])
            self.assertNotIn("DEVLOCK_LABEL", run.call_args.kwargs["env"])
            self.assertEqual([p for p in Path(self.jobs.dir).iterdir() if p.is_dir()], [])
            # A failed refresh preserves last-good data and its original timestamp.
            with monitor._condition:
                monitor._sampled_at -= 100
                previous_at = monitor._sampled_at
                monitor._next_at = 0
            run.side_effect = subprocess.CalledProcessError(1, "fixture-only")
            failed = self.get_json("/fleet?wait=2")
            self.assertEqual(failed["sampled_at"], previous_at)
            self.assertEqual(failed["devices"], sample["devices"])
            self.assertTrue(failed["stale"])
            self.assertIn("CalledProcessError", failed["error"])
            # An incompatible sample is a failed attempt, not a dead sampler thread.
            with monitor._condition:
                monitor._next_at = 0
            run.side_effect = None
            run.return_value.stdout = b'{"schema":"phonefarm/fleet@1","devices":[{"online":true,"battery":"unavailable"}]}'
            incompatible = self.get_json("/fleet?wait=2")
            self.assertIn("battery must be an object", incompatible["error"])
            self.assertFalse(incompatible["refreshing"])
            self.assertEqual(incompatible["devices"], sample["devices"])
            with monitor._condition:
                monitor._next_at = 0
            run.return_value.stdout = json.dumps(sample).encode()
            recovered = self.get_json("/fleet?wait=2")
            self.assertIsNone(recovered["error"])
            self.assertFalse(recovered["stale"])
        for wait in ("nan", "6", "-1"):
            self.assertEqual(self.request("/fleet?wait=" + wait)[0], 400)

    def test_artifact_pagination_and_cross_directory_cursor_rejected(self):
        jid = self.fixture(1)
        page = self.get_json(f"/jobs/{jid}/artifacts?limit=1")
        next_page = self.get_json(f"/jobs/{jid}/artifacts?" + urlencode({"cursor": page["next_cursor"]}))
        self.assertEqual(next_page["items"][0]["name"], "nested")
        self.assertEqual(self.request(f"/jobs/{jid}/artifacts?" + urlencode(
            {"cursor": page["next_cursor"], "path": "nested"}))[0], 400)

    def test_paths_and_symlinks_cannot_expose_host_files(self):
        jid = self.fixture(1)
        secret = self.root / "private.config"
        secret.write_text("must-not-read")
        out = Path(self.jobs.path(jid, "out"))
        (out / "external").symlink_to(secret)
        (out / "linked-directory").symlink_to(self.root, target_is_directory=True)
        entries = self.get_json(f"/jobs/{jid}/artifacts")["items"]
        self.assertEqual(next(e for e in entries if e["name"] == "external")["kind"], "unavailable")
        for rel in ("../meta.json", str(secret), "external", "linked-directory/private.config"):
            for endpoint in ("out", "artifacts"):
                status, _, body = self.request(f"/jobs/{jid}/{endpoint}?" + urlencode({"path": rel}))
                self.assertEqual(status, 400)
                self.assertNotIn(b"must-not-read", body)
        self.assertEqual(self.request(f"/jobs/{jid}/out")[0], 400)  # Whole archive cannot carry links either.
        (Path(self.jobs.dir) / "ffffffffffff").symlink_to(self.root, target_is_directory=True)
        self.assertEqual(self.request("/jobs/ffffffffffff")[0], 400)
        self.assertEqual(self.request("/jobs/../../private.config")[0], 404)

    def test_cli_queries_use_the_same_read_contract(self):
        jid = self.fixture(1)
        cli = Path(__file__).resolve().parents[1] / "pf-node"
        env = dict(os.environ, PF_NODE_URL="http://%s:%s" % self.server.server_address,
                   PF_NODE_TOKEN=self.ro, PF_NODE_ID=self.node.node_id)
        env.pop("PF_NODE_TOKEN_FILE", None)
        for args in (["node"], ["jobs", "--query", "Doom"], ["artifacts", jid, "--path", "nested"]):
            result = subprocess.run([sys.executable, str(cli), *args], env=env, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["schema_version"], 1)


if __name__ == "__main__":
    unittest.main()
