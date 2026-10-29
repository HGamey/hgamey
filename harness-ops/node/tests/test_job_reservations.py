import json
import sys
import time
from unittest import TestCase

import test_node_read as read_fixture


class ReservationTests(TestCase):
    setUp = read_fixture.NodeReadTests.setUp
    tearDown = read_fixture.NodeReadTests.tearDown
    fixture = read_fixture.NodeReadTests.fixture
    request = read_fixture.NodeReadTests.request
    get_json = read_fixture.NodeReadTests.get_json

    def submit(self, spec, request_id):
        self.jobs.dispatch.start = lambda: None
        self.jobs.bins["phonefarm"] = (sys.executable, str(self.root))
        spec = dict(spec)
        spec.setdefault("resources", ["legacy-device"] if spec.get("device_access") == "write" else [])
        return self.jobs.dispatch.submit(spec, b"", request_id)["id"]

    def finish(self, jid, *, lost=False, group="absent", revision=None):
        meta = self.jobs._read_meta(jid)
        meta.update(state="finished", phase="interrupted" if lost else "finished",
                    outcome="interrupted" if lost else "failed", exit=-1 if lost else 1,
                    ended=time.time(), lost=lost,
                    recovery={"status": "required", "reason": "fixture recovery evidence is incomplete"})
        execution = meta["execution"]
        execution.update(child_pid=424242, returncode=-1 if lost else 1, worker_alive=False,
                         owned_process_group={"scope": "original_process_group", "status": group,
                                              "pgid": 424242, "checked_at": time.time(), "error": None})
        if revision is not None:
            execution["service_at_launch"] = {"node_id": self.jobs.node_id,
                                               "service": {"revision": revision, "dirty": False}}
        self.jobs._write_meta(jid, meta)
        self.jobs._read_root.write_json(jid + "/exitcode", -1 if lost else 1)
        self.jobs.dispatch.queue.set_state(jid, "claimed")

    def test_terminal_write_releases_reservation_and_done_parent_can_recover(self):
        spec = {"bin": "phonefarm", "argv": ["-c", "pass"], "device_access": "write",
                "actor_id": "glm-researcher", "resources": ["legacy-device"]}
        jid = self.submit(spec, "terminal-write")
        self.finish(jid)
        self.assertTrue(self.jobs.dispatch.finalize(self.jobs.dispatch.queue.get(jid)))
        row = self.jobs.dispatch.queue.get(jid)
        self.assertEqual(row["state"], "done")
        self.assertEqual(self.jobs.status(jid)["recovery"]["status"], "required")

        following = self.submit({**spec, "device_access": "write"}, "following-write")
        self.assertEqual(self.jobs.dispatch.queue.claim_next(2)["job_id"], following)
        recovery = self.submit({**spec, "device_access": "recovery", "recovery_for": jid}, "recover-terminal")
        self.assertEqual(self.jobs.dispatch.queue.get(recovery)["resources"], ["legacy-device"])

    def test_lost_deploy_interruption_records_reason_and_unknown_can_reset_as_admin(self):
        self.jobs.service_at_launch = {"node_id": self.jobs.node_id,
                                       "service": {"revision": "newsha", "dirty": False}}
        jid = self.submit({"bin": "phonefarm", "argv": ["-c", "pass"], "device_access": "write"},
                          "deploy-lost")
        self.finish(jid, lost=True, revision="oldsha")
        self.assertTrue(self.jobs.dispatch.finalize(self.jobs.dispatch.queue.get(jid)))
        row = self.jobs.dispatch.queue.get(jid)
        self.assertEqual(row["state"], "done")
        self.assertEqual(self.jobs.status(jid)["note"], "部署 newsha 中断")

        unknown = self.submit({"bin": "phonefarm", "argv": ["-c", "pass"], "device_access": "write"},
                              "unknown-lost")
        self.finish(unknown, lost=True, group="unknown")
        self.assertTrue(self.jobs.dispatch.finalize(self.jobs.dispatch.queue.get(unknown)))
        self.assertEqual(self.jobs.dispatch.queue.get(unknown)["state"], "blocked")
        self.assertNotEqual(self.jobs.status(unknown).get("note"), "部署 newsha 中断")
        self.assertIsNone(self.jobs.dispatch.queue.get(unknown)["host_capacity_release"])
        self.jobs.agents.create({"name": "glm-researcher"})
        agent = self.jobs.agents.issue_token("glm-researcher")["token"]
        body = json.dumps({"note": "admin accepted the unknown host state"}).encode()
        self.assertEqual(self.request(f"/jobs/{unknown}/reset", token=agent, method="POST", body=body,
                                      headers={"Content-Type": "application/json"})[0], 403)
        code, _, raw = self.request(f"/jobs/{unknown}/reset", token=self.admin, method="POST", body=body,
                                    headers={"Content-Type": "application/json"})
        self.assertEqual(code, 200, raw)
        value = json.loads(raw)
        self.assertTrue(value["released"])
        self.assertEqual(value["job"]["queue"]["release"]["basis"], "administrator_reset")
        row = self.jobs.dispatch.queue.get(unknown)
        self.assertEqual((row["state"], row["host_capacity_release"]["basis"]), ("done", "administrator_reset"))
        self.assertEqual(self.request(f"/jobs/{unknown}/reset", token=self.admin, method="POST", body=body,
                                      headers={"Content-Type": "application/json"})[0], 409)

    def test_unknown_host_recovery_frees_capacity_but_waits_for_owner_reset(self):
        self.jobs.agents.create({"name": "glm-researcher"})
        self.jobs.agents.create({"name": "other-agent"})
        spec = {"bin": "phonefarm", "argv": ["-c", "pass", "usb-a"], "device_access": "write",
                "actor_id": "glm-researcher", "resources": ["device:usb-a"]}
        jid = self.submit(spec, "unknown-recovery")
        self.finish(jid, lost=True, group="unknown")
        self.assertTrue(self.jobs.dispatch.finalize(self.jobs.dispatch.queue.get(jid)))
        row = self.jobs.dispatch.queue.get(jid)
        self.assertEqual(row["state"], "blocked")
        self.assertEqual((row.get("state_reason") or {}).get("kind"), "device_recovery_pending")
        self.assertIsNone(row["host_capacity_release"])
        self.assertFalse(self.jobs.queue_release_eligibility(row, self.jobs.status(jid))["eligible"])

        other = self.submit({**spec, "argv": ["-c", "pass", "usb-b"], "resources": ["device:usb-b"]}, "other-phone")
        self.assertEqual(self.jobs.dispatch.queue.claim_next(2)["job_id"], other)
        same = self.submit(spec, "same-phone")
        self.assertIsNone(self.jobs.dispatch.queue.claim_next(2))
        duties = __import__("agent_duties").duties(self.jobs, "glm-researcher")
        self.assertTrue(any(item["kind"] == "reset_job" and item["target"] == jid for item in duties["duties"]))
        other_duties = __import__("agent_duties").duties(self.jobs, "other-agent")
        self.assertFalse(any(item["kind"] == "reset_job" for item in other_duties["duties"]))
        body = json.dumps({"note": "已按脚本还原设备并核查"}).encode()
        other_token = self.jobs.agents.issue_token("other-agent")["token"]
        self.assertEqual(self.request(f"/jobs/{jid}/reset", token=other_token, method="POST", body=body,
                                      headers={"Content-Type": "application/json"})[0], 403)
        owner_token = self.jobs.agents.issue_token("glm-researcher")["token"]
        code, _, raw = self.request(f"/jobs/{jid}/reset", token=owner_token, method="POST", body=body,
                                    headers={"Content-Type": "application/json"})
        self.assertEqual(code, 200, raw)
        self.assertEqual(self.jobs.dispatch.queue.get(jid)["state"], "done")
        self.assertEqual(self.jobs.dispatch.queue.claim_next(2)["job_id"], same)

    def test_status_duty_requires_research_or_active_job(self):
        import agent_duties
        self.jobs.agents.create({"name": "maintainer"})
        value = agent_duties.duties(self.jobs, "maintainer")
        self.assertFalse(any(item["kind"] == "post_status" for item in value["duties"]))
        self.jobs.agents.create({"name": "researcher"})
        self.submit({"bin": "phonefarm", "argv": ["-c", "pass"], "device_access": "none",
                     "actor_id": "researcher"}, "active-status-duty")
        value = agent_duties.duties(self.jobs, "researcher")
        self.assertTrue(any(item["kind"] == "post_status" for item in value["duties"]))

    def test_legacy_blocked_finalizes_and_checkin_has_no_release_duty(self):
        self.jobs.agents.create({"name": "glm-researcher"})
        jid = self.submit({"bin": "phonefarm", "argv": ["-c", "pass"], "device_access": "write",
                           "actor_id": "glm-researcher"}, "legacy-blocked")
        self.finish(jid)
        self.jobs.dispatch.queue.set_state(jid, "blocked")
        self.jobs.dispatch._refresh_host_capacity()
        self.assertEqual(self.jobs.dispatch.queue.get(jid)["state"], "done")
        duties = self.jobs.agents and __import__("agent_duties").duties(self.jobs, "glm-researcher")
        self.assertFalse(any(item["kind"] == "unblock_job" for item in duties["duties"]))
