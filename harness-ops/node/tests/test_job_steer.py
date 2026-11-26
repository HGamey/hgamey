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


if __name__ == "__main__":
    unittest.main()
