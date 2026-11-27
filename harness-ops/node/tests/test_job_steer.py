import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import job_steer  # noqa: E402


class Jobs:
    def __init__(self, root):
        self.dir = root

    def path(self, jid, *parts):
        if not job_steer.JOB_ID.fullmatch(jid or ""):
            raise KeyError(jid)
        return os.path.join(self.dir, jid, *parts)


class SteerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.jobs = Jobs(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def job(self, jid, state="running", argv=("run", "--task", "t", "--goal", "g")):
        os.makedirs(self.jobs.path(jid), exist_ok=True)
        with open(self.jobs.path(jid, "meta.json"), "w") as f:
            json.dump({"id": jid, "bin": "phonefarm", "argv": list(argv), "label": "L", "state": state, "started": 1}, f)

    def test_post_list_and_ack(self):
        self.job("abcdef012345")
        code, msg = job_steer.post(self.jobs, "abcdef012345", '{"text":"别用搜索，从分类进"}'.encode(), "dash")
        self.assertEqual(code, 200)
        self.assertTrue(job_steer.ID.fullmatch(msg["id"]))
        items = job_steer.list_messages(self.jobs, "abcdef012345")
        self.assertEqual([(m["text"], m["read_at_step"]) for m in items], [("别用搜索，从分类进", None)])
        with open(self.jobs.path("abcdef012345", "steer", msg["id"] + ".ack.json"), "w") as f:
            json.dump({"step": 7}, f)
        self.assertEqual(job_steer.list_messages(self.jobs, "abcdef012345")[0]["read_at_step"], 7)
        self.assertEqual([j["job_id"] for j in job_steer.running(self.jobs)], ["abcdef012345"])

    def test_refusals(self):
        self.job("abcdef012345", state="finished")
        self.job("0123456789ab", argv=("skill", "run", "x"))
        self.assertEqual(job_steer.post(self.jobs, "abcdef012345", b'{"text":"x"}', "a")[0], 409)
        self.assertEqual(job_steer.post(self.jobs, "0123456789ab", b'{"text":"x"}', "a")[0], 409)
        self.assertEqual(job_steer.post(self.jobs, "fedcba987654", b'{"text":"x"}', "a")[0], 404)
        self.job("111111111111")
        self.assertEqual(job_steer.post(self.jobs, "111111111111", b'{"text":"  "}', "a")[0], 400)
        self.assertEqual(job_steer.post(self.jobs, "111111111111", json.dumps({"text": "x" * 501}).encode(), "a")[0], 400)
        self.assertEqual(job_steer.post(self.jobs, "111111111111", b"not json", "a")[0], 400)
        self.assertEqual([j["job_id"] for j in job_steer.running(self.jobs)], ["111111111111"])
        self.job("222222222222", argv=("skill", "run", "x", "--fallback", "--task", "t"))
        self.assertEqual(job_steer.post(self.jobs, "222222222222", b'{"text":"x"}', "a")[0], 200)


class ContainedSelectionTests(unittest.TestCase):
    def test_only_phonefarm_jobs_on_a_switched_node_with_a_worker(self):
        import job_dispatch

        class N:
            contained_phonefarm, worker_entry = True, "/usr/local/libexec/phonefarm-worker/phonefarm-worker"
        self.assertTrue(job_dispatch.contained_job(N(), {"bin": "phonefarm", "argv": ["run"]}))
        self.assertFalse(job_dispatch.contained_job(N(), {"bin": "game_opt_loop", "argv": []}))
        N.worker_entry = None
        self.assertFalse(job_dispatch.contained_job(N(), {"bin": "phonefarm"}), "没装 worker 不切")
        N.worker_entry, N.contained_phonefarm = "/x/phonefarm-worker", False
        self.assertFalse(job_dispatch.contained_job(N(), {"bin": "phonefarm"}), "节点没开开关不切")

    def test_message_files_are_group_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            jobs = Jobs(tmp)
            os.makedirs(jobs.path("abcdef012345"))
            with open(jobs.path("abcdef012345", "meta.json"), "w") as f:
                json.dump({"bin": "phonefarm", "argv": ["run"], "state": "running"}, f)
            old = os.umask(0o002)
            try:
                code, msg = job_steer.post(jobs, "abcdef012345", b'{"text":"x"}', "dash")
            finally:
                os.umask(old)
            self.assertEqual(code, 200)
            mode = os.stat(jobs.path("abcdef012345", "steer", msg["id"] + ".json")).st_mode & 0o777
            self.assertEqual(mode, 0o660, "独立作业用户靠组读留言")


if __name__ == "__main__":
    unittest.main()
