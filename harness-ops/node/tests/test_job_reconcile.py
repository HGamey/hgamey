import json
import threading
from unittest import TestCase, mock

import test_node_read as read_fixture
import job_cgroup
from job_queue import RequestConflict


class JobReconcileTests(TestCase):
    setUp = read_fixture.NodeReadTests.setUp
    tearDown = read_fixture.NodeReadTests.tearDown
    fixture = read_fixture.NodeReadTests.fixture
    request = read_fixture.NodeReadTests.request
    get_json = read_fixture.NodeReadTests.get_json

    def test_reconciliation_auth_async_receipt_and_release_barrier(self):
        self.jobs.dispatch.start = lambda: None
        row, _ = self.jobs.dispatch.queue.reserve("source", "fixture", {
            "bin": "custom", "execution_policy": "contained", "device_access": "none"}, [])
        jid = self.fixture(int(row["job_id"], 16))
        meta = self.jobs._read_meta(jid)
        meta.update(state="finished", phase="interrupted", outcome="interrupted", lost=True, exit=-1,
                    recovery={"status": "required"}, execution={"kind": "supervised-v1",
                    "process_management": {"policy": "contained"}, "owned_cgroup": {"status": "present"}})
        self.jobs._write_meta(jid, meta)
        self.jobs.dispatch.queue.set_state(jid, "blocked")
        path = f"/jobs/{jid}/execution/reconcile"
        post = dict(method="POST", body=b"{}", headers={"Content-Type": "application/json"})
        self.assertNotIn("jobs.execution-reconcile-v1", self.get_json("/node")["capabilities"])
        self.assertEqual(self.request(path, token=self.admin, **post)[0], 409)
        self.jobs.worker_entry = "/fixed-test-worker/phonefarm-worker"
        self.assertIn("jobs.execution-reconcile-v1", self.get_json("/node")["capabilities"])
        self.assertEqual(self.request(path, **post)[0], 401)
        auth = {"Cookie": f"{read_fixture.dash.COOKIE}={self.board._cookie_val(self.pw)}",
                "X-HG": "1", **post["headers"]}
        self.assertEqual(self.request("/dash/api" + path, **{**post, "headers": auth})[0], 403)
        for body in (b'{"force":1}', b'{"pid":1}', b'[]'):
            self.assertEqual(self.request(path, token=self.admin, **{**post, "body": body})[0], 400)
        with self.assertRaises(RequestConflict):
            self.jobs.release_queue(jid, "review alone cannot clear a populated domain")
        # Hold the fake helper response to verify HTTP returns before it finishes.
        done = threading.Event()
        def launch(store, job_id, entry, force):
            meta["execution"]["reconciliation"] = {"state": "requested", "force": force, "requested_at": 1}
            self.jobs._write_meta(jid, meta)
            return mock.Mock(wait=lambda: done.wait(3))
        try:
            with mock.patch("node_service.job_runtime.launch_reconcile", side_effect=launch) as called:
                status, _, raw = self.request(path, token=self.admin, **{**post, "body": b'{"force":true}'})
                self.assertEqual(status, 200, raw)
                reply = json.loads(raw)
                self.assertTrue(reply["accepted"])
                self.assertEqual(reply["job"]["id"], jid)
                self.assertTrue(reply["job"]["lost"])
                self.assertEqual(called.call_count, 1)
                self.assertIs(called.call_args.args[-1], True)
                # No actual worker holds a lock in this protocol fixture.
                self.assertEqual(self.get_json(f"/jobs/{jid}")["execution"]["reconciliation"]["state"], "unknown")
                self.assertEqual(len(self.jobs.dispatch.queue.rows()), 1)
        finally:
            done.set()

    def test_missing_domain_requires_new_boot_and_output_still_needs_handback(self):
        # Model only the boot decision on an ordinary directory. Actual cgroup
        # membership, kill and UID isolation are verified by the Linux driver.
        root = self.root.resolve() / "boot-decision"
        root.mkdir()
        (root / "cgroup.type").write_text("domain\n")
        (root / "cgroup.procs").write_text("")
        jid = "123456789abc"
        old_boot, new_boot = "00000000-0000-0000-0000-000000000001", "00000000-0000-0000-0000-000000000002"
        config = {"uid": 12345, "gid": 12345, "node_uid": 12346, "node_gid": 12346, "cgroup_root": str(root)}
        bound = {"schema": job_cgroup.CONTAINMENT_SCHEMA, "host_boot_id": old_boot,
                 **config, "name": "job-" + jid, "inode": 1, "dev": 0, "handoff": {"status": "pending", "at": 1}}
        with mock.patch.object(job_cgroup.sys, "platform", "linux"), mock.patch.object(job_cgroup.os, "geteuid", return_value=0), \
                mock.patch.object(job_cgroup, "_libc"), mock.patch.object(job_cgroup, "_cgroup_filesystem"), \
                mock.patch.object(job_cgroup, "_root_owned"), mock.patch.object(job_cgroup, "_boot_id", return_value=old_boot) as boot:
            with self.assertRaisesRegex(ValueError, "same boot"):
                job_cgroup.Domain(config, jid, existing=bound)
            boot.return_value = new_boot
            domain = job_cgroup.Domain(config, jid, existing=bound)
            try:
                observed = domain.observe()
                self.assertEqual((observed["status"], observed["basis"]), ("absent", "previous_boot"))
                meta = {"lost": True, "phase": "interrupted", "execution": {
                    "containment": bound, "owned_cgroup": observed, "reconciliation": {"state": "complete"}}}
                self.assertIn("handoff", job_cgroup.completion_problem(meta))
                bound["handoff"]["status"] = "returned"
                self.assertIsNone(job_cgroup.completion_problem(meta))
                self.assertTrue(meta["lost"])
                del meta["execution"]["reconciliation"]
                self.assertIn("requires execution reconciliation", job_cgroup.completion_problem(meta))
            finally:
                domain.close()
