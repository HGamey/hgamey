"""Moving a center's data by task: exported on the source center's page (whole tasks only), imported on the target
with center_import.py. Checked first, written all or nothing, the same zip twice changes nothing, a damaged zip or a
conflicting ID changes nothing, and what was imported opens on the target as it did on the source."""
import contextlib
import hashlib
import http.client
import io
import json
import os
import sqlite3
import sys
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import test_core_flow_web_cli as core   # puts the node directory on sys.path
from test_plain_http import png
import center_archive
import center_export
import center_import
import dash
from http_server_fixture import start_http_server
from http.server import ThreadingHTTPServer
from node_service import Jobs, make_handler

TASKS = ["sr-a", "sr-b", "sr-c"]


class Center:
    """Another center on its own data directory, served as CoreFlowTests serves its own."""
    request, web, cli = core.CoreFlowTests.request, core.CoreFlowTests.web, core.CoreFlowTests.cli

    def __init__(self, case, root):
        self.root = str(root)
        self.jobs = Jobs(self.root, {"fixture": (sys.executable, self.root)}, data_dir=self.root)
        self.jobs.dispatch.start = lambda: None
        self.jobs.leases.snapshot = lambda **kw: {"status": "clear", "error": None}
        board = dash.Dash.__new__(dash.Dash)
        board._pass, board._steer_pass = (lambda: core.VIEW), (lambda: core.STEER)
        board.job_service = self.jobs
        board.last_view, board.wake, board.monitor = 0, threading.Event(), core.NoDevices()
        board.showcase_dir = os.path.join(self.root, "showcase")
        self.board = board
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(core.NoDevices(), self.jobs, core.ADMIN, board))
        self.thread = start_http_server(self.server)
        case.addCleanup(self.stop)
        self.address, self.node_id = self.server.server_address, self.jobs.games.node_id
        self.view_cookie = f"{dash.COOKIE}={board._cookie_val(core.VIEW)}"
        self.steer_cookie = f"{dash.STEER_COOKIE}={board._cookie_val(core.STEER, b'hgamey-steer-v1')}"

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(5)
            self.jobs._read_root.close()
            self.server = None


def bytes_of(center, path, headers=None):
    connection = http.client.HTTPConnection(*center.address, timeout=10)
    connection.request("GET", path, headers={"Cookie": f"{center.view_cookie}; {center.steer_cookie}",
                                             "X-Expected-Node-ID": center.node_id, **(headers or {})})
    response = connection.getresponse()
    raw = response.read()
    connection.close()
    return response.status, dict(response.getheaders()), raw


def snapshot(root):
    """Every row of the three databases and every other file under the data root, by content."""
    state = {}
    for rel in ("_research/state.sqlite3", "_results/index.sqlite3", "jobs/.queue.sqlite3"):
        db = sqlite3.connect(os.path.join(root, rel))
        for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            state[f"{rel}:{table}"] = sorted(map(repr, db.execute(f"SELECT * FROM {table}")))
        db.close()
    for parent, directories, names in os.walk(root):
        directories[:] = [d for d in directories if d != "_center_import"]
        for name in names:
            if ".sqlite3" not in name:
                path = os.path.join(parent, name)
                state[os.path.relpath(path, root)] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    return state


class CenterExportImportTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli, web = core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web
    write = staticmethod(core.CoreFlowTests.write)

    def job(self, jid, research_id, files):
        out = Path(self.jobs.path(jid, "out"))
        out.mkdir(parents=True)
        self.jobs._write_meta(jid, {"id": jid, "bin": "custom", "argv": ["capture"], "started": 1, "exit": 0,
                                    **({"research_id": research_id} if research_id else {})})
        for name, raw in files.items():
            (out / name).write_bytes(raw)

    def result(self, rid, research_id, jid, names):
        self.assertEqual(self.cli("POST", "/results", {"schema": "phonefarm/result@1", "result_id": rid, "title": rid,
            "origin": {"evidence_origin": "synthetic"}, "limitations": ["fixture"], "labels": {"research_id": research_id},
            "sources": [{"name": n.split(".")[0], "kind": "job_output", "job_id": jid, "path": n} for n in names]})[0], 201)

    def fixture(self):
        """Task sr-a: a job with two screenshots, their registered result, a blind comparison of them with an answer,
        a direction waiting on that comparison and a note with an attached picture. sr-b follows it with a direction
        based on sr-a's and a report of its own job. sr-c has a queued job. Not in any task: setUp's queued job, a
        finished job and its result."""
        self.native, self.upscaled, self.shot = png(b"\x10\x20\x30"), png(b"\x30\x20\x10"), png(b"\x70\x70\x70")
        self.job("aaaaaaaaaaaa", "sr-a", {"native.png": self.native, "upscaled.png": self.upscaled})
        self.job("bbbbbbbbbbbb", "sr-b", {"report.txt": b"sr-b report\n"})
        self.job("dddddddddddd", None, {"loose.txt": b"no task\n"})
        self.assertEqual(self.web("POST", "/research-state/research/sr-a", self.write("create-sr-a", {"title": "画质对比", "objective": "人评"}))[0], 201)
        self.assertEqual(self.web("POST", "/research-state/research/sr-b", self.write("create-sr-b", {"title": "接续", "objective": "再看", "follows": "sr-a"}))[0], 201)
        self.assertEqual(self.web("POST", "/research-state/research/sr-c", self.write("create-sr-c", {"title": "排队中", "objective": "等"}))[0], 201)
        self.result("r-a", "sr-a", "aaaaaaaaaaaa", ["native.png", "upscaled.png"])
        self.result("r-b", "sr-b", "bbbbbbbbbbbb", ["report.txt"])
        self.result("r-u", None, "dddddddddddd", ["loose.txt"])
        ref = lambda name: {"kind": "result_source", "node_id": self.node_id, "result_id": "r-a", "source_name": name}   # noqa: E731
        self.assertEqual(self.cli("POST", "/research-state/request/ev-a", self.write("create-ev-a", {
            "research_id": "sr-a", "type": "evaluation", "title": "两种渲染", "need": "哪张更清楚？", "why": "fixture",
            "context": {"research_revision": 1, "directions": []}, "evidence_refs": [ref("native"), ref("upscaled")],
            "evaluation": {"schema": "phonefarm/ab-evaluation@1", "media_kind": "image", "slots": {"A": 1, "B": 0}}}))[0], 201)
        self.assertEqual(self.web("POST", "/research-state/feedback/fb-a", self.write("answer-a", {
            "request_id": "ev-a", "request_revision": 1, "text": "A 更好"}))[0], 201)
        self.assertEqual(self.web("POST", "/research-state/direction/d-a", self.write("create-d-a", {
            "research_id": "sr-a", "title": "等人评", "hypothesis": "超分更清楚", "state": "waiting_human", "state_reason": "要人看",
            "dependencies": [{"id": "needs-ev-a", "kind": "request", "target_id": "ev-a", "target_revision": 1,
                              "blocking": True, "reason": "要人评", "resolution": None}]}))[0], 201)
        self.assertEqual(self.web("POST", "/research-state/direction/d-b", self.write("create-d-b", {
            "research_id": "sr-b", "title": "接着做", "hypothesis": "沿用", "based_on": {"direction_id": "d-a", "job_id": None}}))[0], 201)
        self.aid = "5" * 32
        self.jobs.research.upload_attachment(self.aid, "shot.png", "image/png", self.shot)
        self.assertEqual(self.web("POST", "/research-state/note/n-a", self.write("create-n-a", {
            "research_id": "sr-a", "text": "看这张", "reply_to": None, "decision": None, "evidence_refs": [
                {"kind": "attachment", "node_id": self.node_id, "attachment_id": self.aid, "filename": "shot.png",
                 "content_type": "image/png", "sha256": hashlib.sha256(self.shot).hexdigest()}]}))[0], 201)
        submitted = self.jobs.dispatch.submit({"bin": "fixture", "argv": ["-c", "pass"], "device_access": "none",
                                               "resources": [], "research_id": "sr-c"}, b"", "sr-c-job")
        self.queued = submitted["job"]["id"] if "job" in submitted else submitted["id"]

    def export(self, body):
        code, status = self.web("POST", "/center-export", body)
        self.assertEqual(code, 202, status)
        deadline = time.time() + 30
        while status["state"] == "running" and time.time() < deadline:
            time.sleep(0.05)
            status = self.web("GET", "/center-export/" + status["id"])[1]
        self.assertEqual(status["state"], "done", status)
        code, headers, raw = bytes_of(self, f"/dash/api/center-export/{status['id']}/download")
        self.assertEqual((code, headers["X-Content-SHA256"]), (200, hashlib.sha256(raw).hexdigest()))
        path = self.tmp / (status["id"] + ".zip")
        path.write_bytes(raw)
        return path, status

    def target(self, name):
        """An empty center that has run once (it has its identity and databases) and is stopped."""
        center = Center(self, self.tmp / name)
        center.stop()
        return center.root

    def run_import(self, path, root, *flags):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = center_import.main([str(path), "--data-root", root, "--json", *flags])
        return code, json.loads(out.getvalue() or err.getvalue())

    def test_everything_exported_imports_into_an_empty_center_and_opens_there(self):
        self.fixture()
        code, listing = self.web("GET", "/center-export/tasks")
        self.assertEqual(code, 200)
        tasks = {t["id"]: t for t in listing["tasks"]}
        self.assertEqual((tasks["sr-a"]["jobs"], tasks["sr-a"]["results"], tasks["sr-a"]["attachments"], tasks["sr-a"]["objects"]), (1, 1, 1, 5))
        self.assertEqual((tasks["sr-b"]["chain"], tasks["sr-c"]["unfinished_jobs"]), ("sr-a", [self.queued]))
        self.assertEqual(listing["chains"], [{"root": "sr-a", "tasks": ["sr-a", "sr-b"]}])
        self.assertEqual((listing["unassigned"]["jobs"], listing["unassigned"]["results"]), (2, 1))
        self.assertGreater(tasks["sr-a"]["bytes"], len(self.native) + len(self.upscaled))
        self.assertIn("agent 账号和 token", listing["excluded"])

        mention = f"在 {self.node_id} 上跑的"   # part of a longer string: left as it is
        self.assertEqual(self.web("POST", "/research-state/note/n-id", self.write("create-n-id", {
            "research_id": "sr-a", "text": mention, "reply_to": None, "decision": None}))[0], 201)
        path, status = self.export({"tasks": TASKS, "unassigned": True})
        self.assertEqual(sorted(status["unfinished_jobs"]), sorted([self.jid, self.queued]))
        # Downloads resume: a range comes back as 206 with the rest of the file.
        code, headers, tail = bytes_of(self, f"/dash/api/center-export/{status['id']}/download", {"Range": "bytes=100-"})
        self.assertEqual((code, tail), (206, path.read_bytes()[100:]))
        with zipfile.ZipFile(path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
        self.assertEqual(manifest["schema"], "hgamey/center-export@1")
        self.assertEqual([t["id"] for t in manifest["tasks"]], TASKS)
        self.assertEqual(len(manifest["result_sources"]["verified"]), 4)
        self.assertEqual(manifest["result_sources"]["unverified"], [])

        root = self.target("empty")
        before = snapshot(root)
        code, plan = self.run_import(path, root)
        self.assertEqual((code, plan["applied"], plan["conflicts"]), (0, False, []))
        self.assertEqual(snapshot(root), before, "checking writes nothing")
        # Every whole string equal to the source node ID, counted by database; the manifest keeps the source's.
        with zipfile.ZipFile(path) as archive:
            expected = {group: sum(value.count(f'"{self.node_id}"') for name in archive.namelist() if name.startswith(group + "/")
                                   for line in archive.read(name).decode().splitlines() if line
                                   for value in center_archive.line_row(line).values() if isinstance(value, str))
                        for group in ("research", "results", "queue")}
        self.assertGreater(expected["research"], 0)
        self.assertEqual(plan["node_id_rewritten"], {"from": self.node_id, "to": plan["target"]["node_id"], "replaced": expected})
        self.assertEqual(manifest["source"]["node_id"], self.node_id)
        self.assertEqual(plan["new"]["作业目录"], 5)
        code, report = self.run_import(path, root, "--apply")
        self.assertEqual((code, report["applied"], report["rebound_sources"]), (0, True, 4))
        self.assertTrue(os.path.isdir(report["backup"]))
        self.assertEqual(report["dangling"], [])
        for rel in ("_research/state.sqlite3", "_results/index.sqlite3", "jobs/.queue.sqlite3"):
            db = sqlite3.connect(os.path.join(root, rel))
            tables = [t for (t,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            self.assertFalse(any(f'"{self.node_id}"' in repr(row) for t in tables for row in db.execute(f"SELECT * FROM {t}")), rel)
            db.close()

        there = Center(self, root)
        for kind, oid in (("research", "sr-a"), ("research", "sr-b"), ("research", "sr-c"), ("request", "ev-a"),
                          ("feedback", "fb-a"), ("direction", "d-a"), ("direction", "d-b"), ("note", "n-a")):
            self.assertEqual(there.web("GET", f"/research-state/{kind}/{oid}")[0], 200, (kind, oid))
        self.assertEqual(there.web("GET", "/research-state/note/n-id")[1]["data"]["text"], mention)
        note = there.web("GET", "/research-state/note/n-a")[1]
        self.assertEqual(note["data"]["evidence_refs"][0]["node_id"], there.node_id, "references follow the center")
        for rid in ("r-a", "r-b", "r-u"):
            self.assertEqual({s["status"] for s in there.cli("GET", "/results/" + rid)[1]["source_observations"]}, {"available"}, rid)
        self.assertEqual(bytes_of(there, "/dash/api/results/r-a/sources/native")[2], self.native)
        self.assertEqual(bytes_of(there, "/dash/api/research-state/request/ev-a/media/A?revision=1")[2], self.upscaled)
        self.assertEqual(bytes_of(there, f"/dash/api/research-attachments/{self.aid}")[2], self.shot)
        self.assertEqual(there.web("GET", "/research-state/direction/d-a")[1]["data"]["dependencies"][0]["target_id"], "ev-a")
        # Jobs that had not finished do not run here: they were queued, so they are cancelled before launch.
        for jid in (self.jid, self.queued):
            job = there.cli("GET", "/jobs/" + jid)[1]
            self.assertEqual((job["state"], job["outcome"]), ("finished", "cancelled"), jid)
        self.assertEqual(there.cli("GET", "/jobs/aaaaaaaaaaaa")[1]["state"], "finished")

        # The task page opens there with its comparison pictures and the note's picture.
        with self._browser_page() as page:
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            base = "http://%s:%d" % there.address
            name, value = there.steer_cookie.split("=", 1)
            page.context.add_cookies([{"name": n, "value": v, "url": base} for n, v in (
                there.view_cookie.split("=", 1), (name, value))])
            page.goto(base + "/dash/research?kind=research&id=sr-a#cmp-ev-a")
            loaded = "s => [...document.querySelectorAll(s)].filter(i => i.complete && i.naturalWidth === 2).length"
            page.wait_for_function(f"() => ({loaded})('#cmp-ev-a .compare-item img') === 2")
            page.wait_for_function(f"() => ({loaded})('img') === 3")   # and the note's attached picture
            self.assertEqual(errors, [])

    def test_a_part_imports_whole_and_its_dangling_references_are_listed(self):
        self.fixture()
        path, _ = self.export({"tasks": ["sr-b"], "unassigned": False})
        root = self.target("part")
        code, report = self.run_import(path, root, "--apply")
        self.assertEqual(code, 0, report)
        self.assertIn("任务 sr-b 接续的任务 sr-a 不在这里，也不在这次导入里", report["dangling"])
        self.assertIn("方向 d-b 基于的方向 d-a 不在这里，也不在这次导入里", report["dangling"])
        self.assertIn(report["pause_agents"], center_import.PAUSE_NOTE)
        there = Center(self, root)
        self.assertEqual(there.web("GET", "/research-state/research/sr-b")[0], 200)
        self.assertEqual(there.web("GET", "/research-state/direction/d-b")[0], 200)
        self.assertEqual(there.web("GET", "/research-state/research/sr-a")[0], 404)
        self.assertEqual(there.web("GET", "/research-state/research/sr-c")[0], 404)
        self.assertEqual(bytes_of(there, "/dash/api/results/r-b/sources/report")[2], b"sr-b report\n")
        self.assertEqual(there.cli("GET", "/results/r-a")[0], 404)
        self.assertEqual(sorted(os.listdir(os.path.join(root, "jobs"))), [".queue.sqlite3", "bbbbbbbbbbbb"])

    def test_jobs_run_elsewhere_are_collected_first_and_what_cannot_be_is_listed(self):
        self.fixture()
        far = "f" * 32
        for jid, observation in (("eeeeeeeeeeee", {"state": "finished", "phase": "finished", "exit": 0}), ("cccccccccccc", None)):
            self.jobs.center.targets.reserve({"id": jid, "request_id": "far-" + jid, "fingerprint": jid, "execution_node_id": far,
                "request": {"spec": {"bin": "fixture", "argv": [], "research_id": "sr-c"}}, "submitted_at": time.time(),
                "device_selection": None, "sent_at": time.time(), "stop": None, "observation": observation})
        task = next(t for t in self.web("GET", "/center-export/tasks")[1]["tasks"] if t["id"] == "sr-c")
        self.assertEqual(sorted(task["originals_elsewhere"]), ["cccccccccccc", "eeeeeeeeeeee"])
        self.assertEqual(sorted(task["unfinished_jobs"]), sorted(["cccccccccccc", self.queued]))
        path, status = self.export({"tasks": ["sr-c"]})
        reasons = {m["job_id"]: m["reason"] for m in status["missing_originals"]}
        self.assertEqual(reasons["cccccccccccc"], "作业还没结束，原件还在从节点上")
        self.assertTrue(reasons["eeeeeeeeeeee"].startswith("从节点上的原件没收回来："), reasons)
        with zipfile.ZipFile(path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
        self.assertEqual({m["job_id"] for m in manifest["tasks"][0]["missing_originals"]}, {"cccccccccccc", "eeeeeeeeeeee"})
        root = self.target("far")
        code, report = self.run_import(path, root, "--apply")
        self.assertEqual(code, 0, report)
        self.assertEqual({m["job_id"] for m in report["missing_originals"]}, {"cccccccccccc", "eeeeeeeeeeee"})
        there = Center(self, root)
        self.assertEqual(there.jobs.center.targets.get("eeeeeeeeeeee")["execution_node_id"], far, "still routed to its computer")

    def test_part_of_a_task_is_refused_and_so_is_anyone_without_the_steer_password(self):
        self.fixture()
        for body in ({"tasks": ["sr-a"], "jobs": ["aaaaaaaaaaaa"]}, {"results": ["r-a"]}, {"tasks": ["sr-a"], "attachments": [self.aid]},
                     {"tasks": "sr-a"}, {"tasks": ["sr-a", "sr-a"]}, {"tasks": [], "unassigned": False}):
            code, value = self.web("POST", "/center-export", body)
            self.assertEqual(code, 400, body)
        self.assertEqual(self.web("POST", "/center-export", {"tasks": ["sr-nope"]})[1]["tasks"], ["sr-nope"])
        self.assertEqual(self.web("POST", "/center-export", {"tasks": ["sr-a"]}, steer=False)[0], 403)
        self.assertEqual(self.web("POST", "/center-export", {"tasks": ["sr-a"]}, hg=False)[0], 400)
        self.assertEqual(self.web("GET", "/center-export/tasks", steer=False)[0], 403)
        self.assertEqual(self.web("GET", "/center-export", steer=False)[0], 403)
        self.assertEqual(self.web("GET", "/center-export")[1]["exports"], [], "nothing was started")
        # Not enough room in the export cache: refused up front, saying how much is needed.
        with mock.patch.object(center_export.shutil, "disk_usage", return_value=mock.Mock(free=1 << 20)):
            code, value = self.web("POST", "/center-export", {"tasks": ["sr-a"]})
        self.assertEqual(code, 507)
        self.assertIn("这次导出约需", value["error"])
        self.assertGreater(value["needed_bytes"], 256 << 20)
        # An export can be deleted; a deleted one is gone.
        path, status = self.export({"tasks": ["sr-c"]})
        self.assertEqual(self.web("DELETE", "/center-export/" + status["id"], {}, steer=False)[0], 403)
        self.assertEqual(self.web("DELETE", "/center-export/" + status["id"], {})[0], 200)
        self.assertEqual(self.web("GET", "/center-export/" + status["id"])[0], 404)
        self.assertEqual(bytes_of(self, f"/dash/api/center-export/{status['id']}/download")[0], 404)

    def test_importing_the_same_zip_twice_changes_nothing(self):
        self.fixture()
        path, _ = self.export({"tasks": TASKS, "unassigned": True})
        root = self.target("twice")
        self.assertEqual(self.run_import(path, root, "--apply")[0], 0)
        once = snapshot(root)
        code, report = self.run_import(path, root, "--apply")
        self.assertEqual((code, report["new"], report["conflicts"]), (0, {}, []))
        self.assertEqual(report["same"]["作业目录"], 5)
        self.assertEqual(snapshot(root), once)

    def test_a_damaged_zip_is_refused_whole(self):
        self.fixture()
        path, _ = self.export({"tasks": TASKS, "unassigned": True})
        with zipfile.ZipFile(path) as archive:   # one byte inside a stored picture
            info = archive.getinfo("jobs/aaaaaaaaaaaa/out/native.png")
        raw = bytearray(path.read_bytes())
        name_length, extra_length = (int.from_bytes(raw[info.header_offset + at:info.header_offset + at + 2], "little") for at in (26, 28))
        raw[info.header_offset + 30 + name_length + extra_length + 20] ^= 0xFF
        path.write_bytes(bytes(raw))
        root = self.target("damaged")
        before = snapshot(root)
        for flags in ((), ("--apply",)):
            code, refused = self.run_import(path, root, *flags)
            self.assertEqual(code, 2)
            self.assertIn("jobs/aaaaaaaaaaaa/out/native.png", "\n".join(refused["details"]))
        self.assertEqual(snapshot(root), before)
        self.assertFalse(os.path.exists(os.path.join(root, "_center_import")))

    def test_the_same_id_with_other_content_aborts_and_lists_the_conflicts(self):
        self.fixture()
        path, _ = self.export({"tasks": TASKS, "unassigned": True})
        center = Center(self, self.tmp / "conflict")
        self.assertEqual(center.web("POST", "/research-state/research/sr-a", self.write("other-sr-a", {"title": "另一个", "objective": "同名"}))[0], 201)
        Path(center.jobs.path("dddddddddddd", "out")).mkdir(parents=True)
        Path(center.jobs.path("dddddddddddd", "out", "loose.txt")).write_bytes(b"something else\n")
        center.stop()
        before = snapshot(center.root)
        code, report = self.run_import(path, center.root, "--apply")
        self.assertEqual(code, 3)
        self.assertIn("研究对象 research/sr-a 第 1 版：这里已有，内容不同", report["details"])
        self.assertTrue(any(c.startswith("作业目录 dddddddddddd：这里已有") for c in report["details"]), report["details"])
        self.assertEqual(snapshot(center.root), before)

    def test_a_running_center_is_not_written_and_a_failure_midway_puts_everything_back(self):
        self.fixture()
        path, _ = self.export({"tasks": TASKS, "unassigned": True})
        root = self.target("guarded")
        before = snapshot(root)
        # The center serving this data root (started with --data-dir or --phonefarm) refuses the import.
        with mock.patch.object(center_import, "node_service_roots", return_value=[(4242, os.path.realpath(root))]):
            code, refused = self.run_import(path, root, "--apply")
        self.assertEqual(code, 4)
        self.assertIn("PID 4242", refused["refused"])
        self.assertIn("systemctl --user start", refused["refused"])
        self.assertEqual(snapshot(root), before)
        # How a command line is read: the blue-zone center runs `node_service.py --phonefarm $PWD/phonefarm ...`
        # from run_center.sh, executor nodes run --data-dir; relative paths start at its working directory, and
        # symbolic links are resolved before comparing.
        link = self.tmp / "via-link"
        link.symlink_to(root)
        center = ["python3", "node_service.py", "--phonefarm", str(link), "--game-opt-loop", "/x", "--host", "127.0.0.1", "--port", "8765"]
        self.assertEqual(center_import.service_root(center, None), os.path.realpath(root))
        self.assertEqual(center_import.service_root(["python3", "/opt/node/node_service.py", "--data-dir=" + os.path.basename(root)],
                                                    os.path.dirname(root)), os.path.realpath(root))
        self.assertEqual(center_import.service_root(["python3", "node_service.py", "--data-dir", "x", "--phonefarm", "/y"], "/w"), "/w/x")
        self.assertIsNone(center_import.service_root(["python3", "other.py", "--data-dir", root], None))
        # The research database fails after jobs and results were written: all of it is put back.
        real = center_import.insert

        def failing(db, table, row):
            if table == "revisions":
                raise sqlite3.OperationalError("disk I/O error")
            return real(db, table, row)
        with mock.patch.object(center_import, "insert", failing):
            code, refused = self.run_import(path, root, "--apply")
        self.assertEqual(code, 1)
        self.assertIn("已全部还原", refused["refused"])
        self.assertEqual(snapshot(root), before)
        self.assertEqual(os.listdir(os.path.join(root, "_center_import")), [])


if __name__ == "__main__":
    unittest.main()
