"""数据流向: one task, job by job — what the platform received from the agent, what it sent to the phone (as the job's
device record saw it) and what came back to the agent — from the job records themselves."""
import json
import subprocess
import time
import unittest
from pathlib import Path

import test_core_flow_web_cli as core
import test_multi_node_jobs as multi_node
import job_preparation  # noqa: E402 - on the path test_core_flow_web_cli sets

SENT, UNRECORDED, HOSTONLY = "a1a1a1a1a1a1", "b2b2b2b2b2b2", "c3c3c3c3c3c3"


def git(*args, cwd=None):
    return subprocess.run(["git", "-c", "user.email=fixture@example.invalid", "-c", "user.name=fixture", *args],
                          cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class TaskFlowTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli, web, pf = core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web, core.CoreFlowTests.pf
    write = staticmethod(core.CoreFlowTests.write)

    def job(self, jid, started, **meta):
        out = Path(self.jobs.path(jid, "out"))
        out.mkdir(parents=True, exist_ok=True)
        self.jobs._write_meta(jid, {"id": jid, "research_id": "sr-flow", "actor_id": "agent-a", "state": "finished", "phase": "finished",
                                    "outcome": "succeeded", "exit": 0, "started": started, "ended": started + 60, **meta})
        return out

    def fixture(self):
        """Three jobs of one task, oldest first: an experiment on HONOR that was given a weights file and whose device
        record saw an install, two pushes, two shell commands and a pull; an older run on the same phone with no device
        record; a host-only job."""
        source = self.tmp / "experiments"
        (source / "qrisp").mkdir(parents=True)
        (source / "qrisp" / "train.py").write_text("print('train')\n")
        git("init", "-q", "-b", "main", str(source))
        git("add", ".", cwd=source)
        git("commit", "-qm", "Train on the phone", cwd=source)
        commit = git("rev-parse", "HEAD", cwd=source)
        git("clone", "-q", "--bare", str(source), self.jobs.experiment_mirror)
        now = time.time()
        honor = {"device_access": "write", "resources": ["device:HONOR"],
                 "device_binding": {"schema": "phonefarm/device-binding@1", "registry_revision": 1, "device_id": "HONOR", "serial": "HN1"}}
        out = self.job(SENT, now - 600, bin="experiment", argv=["qrisp/train.py", "--steps", "1"], label="装机并测量",
                       experiment={"commit": commit}, **honor)
        Path(self.jobs.path(SENT, job_preparation.RECEIPT)).write_text(json.dumps({"status": "ready", "entries": 2, "declared_bytes": 2048}))
        for path, data in (("base.npz", b"w" * 1024), ("shot_on.png", b"\x89PNG"), ("report.md", b"# r")):
            (out / path).write_bytes(data)
        record = [{"at": now, "serial": "HN1", "command": "install", "files": [{"path": "/w/game.apk", "size": 4096, "sha256": "ab" * 32}], "flags": ["-r"]},
                  {"at": now, "serial": "HN1", "command": "push", "files": [{"path": "/w/libdoom3.so", "size": 2048, "sha256": "cd" * 32}], "to": "/data/local/tmp/libdoom3.so"},
                  {"at": now, "serial": "HN1", "command": "push", "files": [{"path": "/w/sr_on.cfg", "size": 60}], "to": "/data/local/tmp/sr_on.cfg"},
                  {"at": now, "serial": "HN1", "command": "shell", "text": "am force-stop com.karin.idTech4Amm"},
                  {"at": now, "serial": "HN1", "command": "shell", "text": "input tap 100 200"},
                  {"at": now, "serial": "HN1", "command": "pull", "source": "/sdcard/fps.txt", "to": "out/fps.txt"}]
        (out / ".device-io.jsonl").write_text("".join(json.dumps(line) + "\n" for line in record))
        given = Path(self.jobs.path(SENT, "in"))
        (given / "0").mkdir(parents=True)
        (given / "0" / "base.npz").write_bytes(b"w" * 2048)
        (given / "experiment" / "qrisp").mkdir(parents=True)   # the pinned commit's code, shown as the commit
        (given / "experiment" / "qrisp" / "train.py").write_text("print('train')\n")
        self.job(UNRECORDED, now - 1200, bin="experiment", argv=["doom/old.sh", "HN1"], label="旧的装机", experiment={"commit": commit},
                 outcome="failed", exit=3, **honor)
        self.job(HOSTONLY, now - 300, bin="fixture", argv=["-c", "pass"], label="整理结果", device_access="none")
        self.assertEqual(self.web("POST", "/research-state/research/sr-flow", self.write(
            "create-sr-flow", {"title": "装回手机", "objective": "数据流向"}))[0], 201)
        return commit

    def test_each_job_shows_what_came_in_what_went_to_the_phone_and_what_came_back(self):
        commit = self.fixture()
        base = "http://%s:%d" % self.address
        with self._browser_page() as page:
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base + "/dash/research?kind=research&id=sr-flow")
            page.wait_for_selector(".task-owner .owner-jump")
            self.assertIn("/dash/research/flow?id=sr-flow", page.get_attribute('.task-owner a.owner-jump:has-text("数据流向")', "href"))
            page.click('.task-owner a.owner-jump:has-text("数据流向")')
            page.wait_for_function("() => document.querySelectorAll('#flow-jobs > li.flow-job:not(.note)').length === 3")
            self.assertEqual(page.text_content("#flow-title"), "装回手机")
            self.assertEqual(page.eval_on_selector_all("#flow-jobs > li", "e => e.map(x => x.dataset.job)"), [UNRECORDED, SENT, HOSTONLY], "oldest first")
            row = f'#flow-jobs > li[data-job="{SENT}"]'
            text = lambda selector: page.text_content(f"{row} {selector}")
            # Received from the agent: the command, the commit it ran (opening to what it changed), the file it was given.
            self.assertEqual(text(".flow-in .flow-command"), "experiment qrisp/train.py --steps 1")
            self.assertIn("来自 agent-a", text(".flow-in"))
            self.assertEqual(text(".flow-in .output-commit code"), commit[:7])
            self.assertEqual(page.eval_on_selector_all(f"{row} .flow-in .flow-list .flow-file", "e => e.map(x => x.textContent)"), ["0/base.npz"])
            # Sent to the phone: which phone, the package, the pushed files, the commands.
            self.assertEqual(text(".flow-phone-name"), "HONOR · HN1 · 写")
            blocks = page.eval_on_selector_all(f"{row} .flow-phone .flow-block", "e => e.map(x => [x.querySelector('h4').textContent, [...x.querySelectorAll('li')].map(li => li.textContent)])")
            self.assertEqual(blocks, [["装的包 · 1", ["game.apk4 KBsha abababab"]],
                                      ["推的文件 · 2", ["libdoom3.so2 KBsha cdcdcdcd→ /data/local/tmp/libdoom3.so", "sr_on.cfg60 字节→ /data/local/tmp/sr_on.cfg"]],
                                      ["执行的命令 · 2", ["am force-stop com.karin.idTech4Amm", "input tap 100 200"]]])
            # Back to the agent: how it ended, the files it wrote (not the platform's record), the pull, the log.
            self.assertIn("成功", text(".flow-out .flow-outcome"))
            self.assertEqual(page.eval_on_selector_all(f"{row} .flow-out .output-count", "e => e.map(x => x.textContent)"), ["权重 1", "报告 1", "图片 1"])
            self.assertIn("/sdcard/fps.txt → out/fps.txt", text(".flow-out"))
            self.assertIn(f"job={SENT}", page.get_attribute(f"{row} .flow-log", "href"))
            # A run with no device record says so; a host-only job used no phone.
            self.assertIn("没有逐条记录", page.text_content(f'#flow-jobs > li[data-job="{UNRECORDED}"] .flow-phone'))
            self.assertIn("退出码 3", page.text_content(f'#flow-jobs > li[data-job="{UNRECORDED}"] .flow-out'))
            self.assertEqual(page.text_content(f'#flow-jobs > li[data-job="{HOSTONLY}"] .flow-phone').strip(), "没有用手机")
            self.assertEqual([page.text_content(s) for s in ("#total-in", "#total-phone", "#total-out")],
                             ["3 条命令 · 1 个代码提交 · 1 个输入文件", "装包 1 · 推文件 2 · 命令 2 · 1 个作业没有逐条记录", "3 个输出文件 · 截图 1 · 日志 3"])
            self.assertEqual(errors, [])
            page.set_viewport_size({"width": 390, "height": 844})
            page.reload()
            page.wait_for_function("() => document.querySelectorAll('#flow-jobs > li.flow-job:not(.note)').length === 3")
            self.assertEqual(page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth"), 0)
            self.assertEqual(page.eval_on_selector_all(f"{row} .flow-columns > .flow-cell", "e => new Set(e.map(x => x.getBoundingClientRect().left)).size"), 1,
                             "on a phone the three columns stack")

    def test_an_adb_pull_comes_back_and_the_platforms_app_check_is_not_the_jobs(self):
        """A `pf-node adb pull` job: what it pulled is in 返回给 agent. An experiment whose device record starts with the
        platform's look at the phone's apps before the job: that is one line of its own, not the job's commands."""
        now = time.time()
        honor = {"device_access": "write", "resources": ["device:HONOR"],
                 "device_binding": {"schema": "phonefarm/device-binding@1", "registry_revision": 1, "device_id": "HONOR", "serial": "HN1"}}
        self.job(SENT, now - 60, bin="adb", argv=["-s", "HN1", "pull", "/sdcard/fps.txt", "fps.txt"], **honor)
        out = self.job(UNRECORDED, now - 30, bin="experiment", argv=["doom/tap.sh"], **honor)
        check = lambda text: {"at": now, "serial": "HN1", "command": "shell", "text": text + "\nprintf '\\n__PF_APP_STATUS__%s\\n' \"$?\""}
        record = [check("dumpsys window"), check("ps -A -o NAME"), check("pm list packages -3"),
                  {"at": now, "serial": "HN1", "command": "shell", "text": "input tap 1 2"}]
        (out / ".device-io.jsonl").write_text("".join(json.dumps(line) + "\n" for line in record))
        self.assertEqual(self.web("POST", "/research-state/research/sr-flow", self.write(
            "create-sr-flow", {"title": "装回手机", "objective": "数据流向"}))[0], 201)
        with self._browser_page() as page:
            page.goto("http://%s:%d/dash/research/flow?id=sr-flow" % self.address)
            page.wait_for_function("() => document.querySelectorAll('#flow-jobs > li.flow-job:not(.note)').length === 2")
            pull, checked = (f'#flow-jobs > li[data-job="{jid}"]' for jid in (SENT, UNRECORDED))
            self.assertIn("从手机取回 · 1/sdcard/fps.txt → fps.txt", page.text_content(f"{pull} .flow-out"))
            self.assertEqual(page.text_content(f"{pull} .flow-phone .note"), "作业本身没有发给手机的内容")
            self.assertEqual(page.eval_on_selector_all(f"{checked} .flow-phone .flow-block li", "e => e.map(x => x.textContent)"), ["input tap 1 2"])
            self.assertEqual(page.text_content(f"{checked} .flow-check"), "开工前平台查看了手机上的应用（3 条命令）")
            self.assertEqual(page.text_content("#total-phone"), "装包 0 · 推文件 0 · 命令 1")

    def test_the_files_a_job_was_given_are_listed_from_its_input_directory(self):
        self.fixture()
        code, top = self.web("GET", f"/jobs/{SENT}/input-files?path=")
        self.assertEqual((code, [item["name"] for item in top["items"]]), (200, ["0", "experiment"]))
        code, given = self.web("GET", f"/jobs/{SENT}/input-files?path=0")
        self.assertEqual((code, [(item["path"], item["size"]) for item in given["items"]]), (200, [("0/base.npz", 2048)]))
        self.assertEqual(self.web("GET", f"/jobs/{SENT}/input-files?path=../out")[0], 400)
        self.assertEqual(self.web("GET", "/jobs/ffffffffffff/input-files?path=")[0], 404)


class RemoteInputFilesTests(unittest.TestCase):
    def test_a_job_run_on_another_computer_says_where_its_inputs_are(self):
        flow = multi_node.MultiNodeJobTests()
        flow.setUp()
        self.addCleanup(flow.doCleanups)
        flow.start_worker()
        code, job = flow.submit({"bin": "fixture", "argv": ["-c", "pass"], "device_access": "none",
                                 "execution_node_id": flow.worker.jobs.node_id}, "inputs-elsewhere")
        self.assertEqual(code, 200, job)
        code, reply = flow.center.call("GET", f"/jobs/{job['id']}/input-files?path=", token=flow.agent)
        self.assertEqual((code, reply["code"]), (409, "inputs_elsewhere"))
        self.assertEqual(flow.worker.call("GET", f"/jobs/{job['id']}/input-files?path=")[0], 200)


if __name__ == "__main__":
    unittest.main()
