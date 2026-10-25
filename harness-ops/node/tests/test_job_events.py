"""Committed event pages are readonly; they do not replay worker history."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

NODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NODE))
from job_events import NodeJobEvents
from job_queue import JobQueue
from node_read import decode_cursor, encode_cursor
from node_service import readonly_token
import test_node_games as fixture


class JobEventTests(unittest.TestCase):
    def test_http_cli_committed_pages_restart_and_legacy_reads(self):
        env = fixture.GameFlowTests()
        env.setUp()
        self.addCleanup(env.tearDown)
        env.jobs.dispatch.start = lambda: None
        env.jobs.bins["fixture"] = (sys.executable, str(env.root))
        queue = env.jobs.dispatch.queue
        pin = {"X-Expected-Node-ID": env.jobs.node_id}
        ro = readonly_token(env.admin)

        def get(path, **kwargs):
            return env.call("GET", path, token=ro, headers=pin, **kwargs)

        def page(**query):
            code, value = get("/job-events?" + urlencode(query))
            self.assertEqual(code, 200, value)
            return value

        def sql_facts():
            with queue._transaction() as db:
                return ([dict(row) for row in db.execute("SELECT * FROM job_events ORDER BY sequence")],
                        [dict(row) for row in db.execute("SELECT * FROM jobs ORDER BY sequence")])

        code, identity = get("/node")
        self.assertEqual(code, 200, identity)
        self.assertIn("jobs.state-events-v1", identity["capabilities"])
        self.assertEqual(env.call("GET", "/job-events", token=ro)[0], 400)
        self.assertEqual(env.call("GET", "/job-events", headers=pin, token="invalid")[0], 401)
        self.assertEqual(env.call("GET", "/job-events", token=ro,
                                 headers={"X-Expected-Node-ID": "0" * 32})[0], 409)

        # A directory predating SQL and an old SQL done row have no invented revision.
        legacy, old_sql, lost = "1" * 12, "2" * 12, "3" * 12
        for jid in (legacy, old_sql, lost):
            env.jobs._read_root.mkdir(jid)
            env.jobs._read_root.write_json(jid + "/meta.json", {
                "id": jid, "bin": "fixture", "argv": [], "started": 1, "pid": None,
                **({"exit": 0} if jid == old_sql else {})})
        Path(env.jobs.path(legacy, "exitcode")).write_text("7\n")
        with queue._transaction(write=True) as db:
            db.execute("""INSERT INTO jobs(job_id,request_id,fingerprint,spec,resources,submitted_at,state)
                VALUES(?,?,?,?,?,?,'done')""", (old_sql, "historical", "historical",
                    json.dumps({"bin": "fixture", "argv": [], "resources": [], "device_access": "none"}), "[]", 1))
        originals = {jid: Path(env.jobs.path(jid, "meta.json")).read_bytes() for jid in (legacy, old_sql, lost)}
        before = sql_facts()
        for jid in (legacy, old_sql, lost):
            code, value = get("/jobs/" + jid)
            self.assertEqual(code, 200, value)
            self.assertEqual(value["status_revision"], 0 if jid == old_sql else None)
            self.assertIsNone(value["committed_status"])
        self.assertEqual(get("/jobs/" + legacy)[1]["exit"], 7)
        self.assertTrue(get("/jobs/" + lost)[1]["lost"])
        self.assertEqual(get("/job-events?job_id=" + legacy)[0], 404)
        self.assertEqual(page(job_id=old_sql)["items"], [])
        self.assertEqual(sql_facts(), before)
        for jid, raw in originals.items():
            self.assertEqual(Path(env.jobs.path(jid, "meta.json")).read_bytes(), raw)

        spec = {"bin": "fixture", "argv": ["-c", "pass"], "resources": [], "device_access": "none"}
        created = []
        for key in ("events-first", "events-second"):
            code, job = env.call("POST", "/jobs", headers={**pin, "X-Request-ID": key, "X-Job": json.dumps(spec)})
            self.assertEqual((code, job["phase"]), (200, "queued"), job)
            self.assertGreater(job["status_revision"], 0)
            self.assertEqual(job["status_revision_scope"], "committed_job_state")
            self.assertIsNone(job["committed_status"]["phase"], "claim/queue is not a worker start")
            created.append(job)
        first, second = created
        before = sql_facts()
        repeated = env.call("POST", "/jobs", headers={**pin, "X-Request-ID": "events-first", "X-Job": json.dumps(spec)})[1]
        self.assertEqual((repeated["id"], repeated["status_revision"]), (first["id"], first["status_revision"]))
        self.assertEqual(sql_facts(), before)
        for job in created:
            code, result = env.call("POST", "/jobs/" + job["id"] + "/cancel", {}, headers=pin)
            self.assertEqual((code, result["job"]["outcome"]), (200, "cancelled"), result)
            self.assertEqual(result["job"]["committed_status"]["recovery_status"], "not_needed")
            self.assertFalse(Path(env.jobs.path(job["id"], "launch.json")).exists())

        # Event reads alone never ask for lease/device state or write SQL/meta.
        before = sql_facts()
        with patch.object(env.jobs.leases, "snapshot", side_effect=AssertionError("event GET probed leases")):
            head = page(limit=1)
            self.assertTrue(head["has_more"])
            filtered = page(job_id=first["id"], limit=1)
            self.assertEqual(filtered["items"][0]["kind"], "admitted")
            self.assertEqual(filtered["items"][0]["job_id"], first["id"])
            self.assertEqual(filtered["head"], head["head"])
            # Reopen the same persistent stream, without a dispatcher or worker.
            reopened = NodeJobEvents(JobQueue(queue.path), env.jobs.node_id)
            expected = reopened.events({"cursor": [filtered["next_cursor"]], "limit": ["200"]})
            env.jobs.events = reopened
            rest = page(cursor=filtered["next_cursor"], limit=200)
            self.assertEqual(rest, expected)
            self.assertEqual(rest["stream_id"], filtered["stream_id"])
            self.assertTrue(all(item["job_id"] == first["id"] for item in rest["items"]))
            self.assertEqual(rest["items"][-1]["committed_status"]["outcome"], "cancelled")
            empty = page(cursor=rest["next_cursor"])
            self.assertEqual((empty["items"], empty["next_after"]), ([], rest["next_after"]))
            self.assertEqual(empty["next_cursor"], rest["next_cursor"])

            def rejected(query, code):
                status, value = get("/job-events?" + urlencode(query))
                self.assertEqual((status, value.get("code")), (409, code), value)

            rejected({"job_id": second["id"], "cursor": filtered["next_cursor"]}, "job_cursor_scope_changed")
            changed = decode_cursor(filtered["next_cursor"])
            rejected({"cursor": encode_cursor({**changed, "stream_id": "different-stream"})}, "job_cursor_stream_changed")
            rejected({"cursor": encode_cursor({**changed, "event_id": "different-event"})}, "job_cursor_history_changed")
            rejected({"after": head["head"] + 1}, "job_cursor_ahead")
            other = page(job_id=second["id"], limit=1)["items"][0]["sequence"]
            rejected({"job_id": first["id"], "after": other}, "job_cursor_history_changed")
            view = {**pin, "Authorization": "Basic " + base64.b64encode(b"viewer:test-view").decode()}
            status, dashboard = env.call("GET", "/dash/api/job-events?job_id=" + first["id"], headers=view)
            self.assertEqual(status, 200, dashboard)
            self.assertEqual(dashboard["items"][-1], rest["items"][-1])
        self.assertEqual(sql_facts(), before)

        requests, handler = [], env.server.RequestHandlerClass
        original_get = handler._read_get

        def recorded_get(this, parts, query):
            requests.append((parts, query))
            return original_get(this, parts, query)

        cli_env = {**os.environ, "PF_NODE_URL": env.url, "PF_NODE_ID": env.jobs.node_id,
                   "PF_NODE_TOKEN": ro, "HOME": str(env.root)}
        cli_env.pop("PF_NODE_TOKEN_FILE", None)
        with patch.object(handler, "_read_get", recorded_get):
            result = subprocess.run([sys.executable, str(NODE / "pf-node"), "job-events", "--job", first["id"],
                                     "--cursor", filtered["next_cursor"], "--limit", "1"], env=cli_env,
                                    capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(len(value["items"]), 1)
        self.assertEqual([parts for parts, _ in requests], [["node"], ["job-events"]])
        self.assertEqual(requests[-1][1]["cursor"], [filtered["next_cursor"]])
        self.assertEqual(sql_facts(), before)
        self.assertIsNone(env.jobs.dispatch.thread)


if __name__ == "__main__":
    unittest.main()
