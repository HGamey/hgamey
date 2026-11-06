"""打开游戏: a result of a task (here an engine library and its configuration) opened in an installed game on a chosen
phone, and the original next to it. The game's registration says where result files go and how the game starts; each
opening is a chain of adb jobs under the task. What a placed file replaces is kept as <file>.pf-orig (a missing one
gets a .pf-absent mark), and opening the original puts it back, however many times it is run."""
import hashlib
import json
import re
import subprocess
import time
import unittest
from pathlib import Path
from unittest import mock

import test_core_flow_web_cli as core   # puts the node directory on sys.path
from game_deploy_recipe import validate_opening  # noqa: E402

PHONE_ROOT = "/storage/emulated/0/Android/data/com.karin.idTech4Amm/files/diii4a"
LAUNCH = ["-W", "-S", "-n", "com.karin.idTech4Amm/com.n0n3m4.q3e.Q3EMain", "--es", "game", "doom3", "--es", "command",
          "game.arm +set fs_basepath /storage/emulated/0/Android/data/com.karin.idTech4Amm/files/diii4a/doom3 +map game/demo_mars_city1"]
OPENING = {"schema": "hgamey/game-opening@1", "package": "com.karin.idTech4Amm", "root": PHONE_ROOT,
           "files": [{"match": "libdoom3.so", "to": ["doom3/libdoom3.so", "doom3/lib/arm64/libdoom3.so"], "result": True},
                     {"match": "sr_*.cfg", "to": ["doom3/base/"], "result": False}],
           "launch": LAUNCH, "wait_seconds": 60, "notes": "替换前存成 .pf-orig，原来没有的留 .pf-absent；打开原版按标记还原。"}
MANIFEST = {"schema_version": 1, "game_id": "doom3-demo", "version": "v73-open1", "title": "Doom 3 Demo", "opening": OPENING,
            "assets": [{"name": "idtech4amm.apk", "role": "engine-apk", "sha256": "a" * 64, "size": 10,
                        "source": {"kind": "unavailable", "reason": "installed on the phones"}}]}
RUN, OLD = "c1c1c1c1c1c1", "c2c2c2c2c2c2"
SHOT = b"\x89PNG\r\n\x1a\nshot"


class GameOpenTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli, web, pf = core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web, core.CoreFlowTests.pf
    write = staticmethod(core.CoreFlowTests.write)
    maxDiff = None

    def test_an_opening_says_where_result_files_go_and_how_the_game_starts(self):
        self.assertEqual(validate_opening(OPENING), OPENING)
        broken = {"a * name can only go to directories": {"files": [{"match": "sr_*.cfg", "to": ["doom3/base/sr.cfg"], "result": True}]},
                  "no result to choose": {"files": [{"match": "sr_*.cfg", "to": ["doom3/base/"], "result": False}]},
                  "a place outside the root": {"files": [{"match": "libdoom3.so", "to": ["../libdoom3.so"], "result": True}]},
                  "a relative root": {"root": "storage/emulated/0"},
                  "a launch argument on two lines": {"launch": ["-n", "pkg/.A\n"]},
                  "no notes": {"notes": " "}}
        for reason, change in broken.items():
            with self.subTest(reason):
                with self.assertRaises(ValueError):
                    validate_opening({**OPENING, **change})
        self.assertEqual(self.cli("POST", "/games", MANIFEST)[0], 201)
        self.assertEqual(self.cli("POST", "/games", {**MANIFEST, "version": "v73-open2", "opening": {**OPENING, "wait_seconds": 0}})[0], 400)

    def fixture(self):
        """The game registered with its opening. A delivered task whose newest run wrote the engine library under
        src/, two configurations beside src/ and weights; an older run wrote only a library. On the center HONOR is
        online and HUAWEI, registered, is not."""
        self.assertEqual(self.cli("POST", "/games", MANIFEST)[0], 201)
        for jid, files in ((RUN, {"d_honor2_x/src/libdoom3.so": b"\x7fELF new", "d_honor2_x/sr_on.cfg": b"set r_smallSR 2\n",
                                  "d_honor2_x/sr_off.cfg": b"set r_smallSR 1\n", "d_honor2_x/src/weights.npz": b"w" * 64}),
                           (OLD, {"d_honor_y/src/libdoom3.so": b"\x7fELF old"})):
            out = Path(self.jobs.path(jid, "out"))
            for path, data in files.items():
                (out / path).parent.mkdir(parents=True, exist_ok=True)
                (out / path).write_bytes(data)
            self.jobs._write_meta(jid, {"id": jid, "bin": "experiment", "argv": ["doom/d_honor_sr2.sh"], "research_id": "sr-open",
                                        "state": "finished", "phase": "finished", "outcome": "succeeded", "started": 1, "exit": 0})
        self.assertEqual(self.web("POST", "/research-state/research/sr-open", self.write(
            "create-sr-open", {"title": "D 轨：装回手机", "objective": "打开游戏"}))[0], 201)
        code, _, err = self.pf(core.ADMIN, "research", "deliver", "sr-open", "--summary", "## 结论\n引擎库在 src/。",
                               "--artifact", f"job:{RUN}", "--artifact", f"job:{OLD}")
        self.assertEqual(code, 0, err)
        honor = {"serial": "HN1", "transport": "adb", "online": True, "brand": "HONOR", "model": "AAP-AN00", "os": "Android 16 (SDK 36)"}
        scopes = {"schema": "phonefarm/device-scopes@1", "registry_revision": 3, "unknown": False, "queue_waiting": False,
                  "global": {"status": "clear", "error": None}, "devices": [{"device_id": d, "aliases": [a], "status": "clear", "error": None}
                                                                       for d, a in (("HONOR", "HN1"), ("HUAWEI", "HD1"))]}
        for patch in (mock.patch.object(self.jobs.fleet, "snapshot", return_value={"stale": False, "sampled_at": time.time(), "devices": [honor]}),
                      mock.patch.object(self.jobs.leases, "snapshot", return_value={"status": "clear", "error": None, "device_scopes": scopes})):
            patch.start()
            self.addCleanup(patch.stop)

    def fake_jobs(self, page, fail=()):
        """Jobs the page submits are recorded, not run: each finishes at once (the labels in `fail` fail) and the last
        one's screen.png is a picture."""
        submitted = []

        def submit(route):
            if route.request.method != "POST":
                return route.continue_()
            spec = route.request.post_data_json
            jid = "f%011x" % len(submitted)
            submitted.append((jid, spec))
            route.fulfill(json={"id": jid})

        def status(route):
            jid = re.search(r"/jobs/(f[0-9a-f]{11})", route.request.url).group(1)
            if "/out?" in route.request.url:
                return route.fulfill(body=SHOT, content_type="image/png")
            spec = dict(submitted)[jid]
            failed = any(spec["label"].endswith(label) for label in fail)
            route.fulfill(json={"id": jid, "state": "finished", "phase": "finished", "outcome": "failed" if failed else "succeeded",
                                "exit": 3 if failed else 0, "bin": "adb", "argv": spec["argv"]})

        page.route(re.compile(r".*/dash/api/jobs(\?.*)?$"), submit)
        page.route(re.compile(r".*/dash/api/jobs/f[0-9a-f]{11}.*"), status)
        return submitted

    def test_a_result_opens_in_the_game_as_a_chain_of_adb_jobs_and_the_original_beside_it(self):
        self.fixture()
        base = "http://%s:%d" % self.address
        with self._browser_page() as page:
            page.context.add_cookies([{"name": self.steer_cookie.split("=", 1)[0], "value": self.steer_cookie.split("=", 1)[1], "url": base}])
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base + "/dash/research?kind=research&id=sr-open")
            page.click('.task-owner a.owner-jump:has-text("打开游戏")')
            page.wait_for_selector("#open-results .open-choice")
            self.assertEqual(page.text_content("#open-title"), "D 轨：装回手机")
            # Each engine library of the delivery is a result; the configurations of its run go with it.
            self.assertEqual(page.eval_on_selector_all("#open-results .open-choice", "e => e.map(x => x.textContent)"),
                             [f"引擎库libdoom3.so8 字节 · 作业 {RUN}一起放入：sr_off.cfg、sr_on.cfg", f"引擎库libdoom3.so8 字节 · 作业 {OLD}"])
            self.assertEqual(page.eval_on_selector_all("#open-phones .open-choice", "e => e.map(x => x.textContent)"), ["AAP-AN00中心 · HN1"], "only a phone that is online")
            submitted = self.fake_jobs(page)
            page.click("#open-result")
            page.wait_for_selector('#open-shots figure[data-mode="result"] img')
            self.assertEqual([spec["label"] for _, spec in submitted],
                             ["打开游戏：推送 libdoom3.so、sr_off.cfg、sr_on.cfg", "打开游戏：放入并启动", "打开游戏：截图"])
            for _, spec in submitted:
                self.assertEqual({key: spec[key] for key in ("bin", "device_access", "device_ref", "research_id")},
                                 {"bin": "adb", "device_access": "write", "device_ref": f"{self.node_id}/HONOR", "research_id": "sr-open"})
                self.assertEqual(spec["argv"][:2], ["-s", "@serial"])
            push, place, shot = (spec for _, spec in submitted)
            self.assertEqual(push["argv"][2:], ["push", "@in/pf-open-libdoom3.so", "@in/pf-open-sr_off.cfg", "@in/pf-open-sr_on.cfg", "/data/local/tmp/"])
            out = Path(self.jobs.path(RUN, "out"))
            self.assertEqual(push["inputs"], {"schema": "phonefarm/job-inputs@1", "node_id": self.node_id, "bindings": [], "cwd": "default", "files": [
                {"name": f"f{i}", "target": f"pf-open-{Path(path).name}", "source": {"job_id": RUN, "path": path, "size": (out / path).stat().st_size,
                 "sha256": hashlib.sha256((out / path).read_bytes()).hexdigest()}}
                for i, path in enumerate(["d_honor2_x/src/libdoom3.so", "d_honor2_x/sr_off.cfg", "d_honor2_x/sr_on.cfg"])]})
            script = place["argv"][3]
            self.assertEqual(place["argv"][2], "shell")
            self.assertTrue(script.startswith("am force-stop 'com.karin.idTech4Amm'; place() {"), script)
            for line in (f"place '/data/local/tmp/pf-open-libdoom3.so' '{PHONE_ROOT}/doom3/libdoom3.so'",
                         f"place '/data/local/tmp/pf-open-libdoom3.so' '{PHONE_ROOT}/doom3/lib/arm64/libdoom3.so'",
                         f"place '/data/local/tmp/pf-open-sr_on.cfg' '{PHONE_ROOT}/doom3/base/sr_on.cfg'"):
                self.assertIn(line, script)
            self.assertTrue(script.endswith(f"; am start {' '.join(repr(arg) for arg in LAUNCH)}; sleep 60; screencap -p '/data/local/tmp/pf-open-screen.png'"), script)
            self.assertEqual(shot["argv"][2:], ["pull", "/data/local/tmp/pf-open-screen.png", "screen.png"])
            self.assertEqual(page.eval_on_selector_all("#open-steps .open-step a", "e => e.map(x => x.textContent)"),
                             [f"作业 {jid}" for jid, _ in submitted])
            self.assertIn(f"/dash/api/jobs/{submitted[2][0]}/out", page.get_attribute('#open-shots figure[data-mode="result"] img', "src"))

            # The original: put back, start, a screenshot; both pictures stay side by side.
            page.click("#open-original")
            page.wait_for_selector('#open-shots figure[data-mode="original"] img')
            restore = submitted[3][1]
            self.assertEqual([spec["label"] for _, spec in submitted[3:]], ["打开原版：还原并启动", "打开原版：截图"])
            self.assertNotIn("inputs", restore)
            self.assertIn(f"back '{PHONE_ROOT}/doom3/libdoom3.so'", restore["argv"][3])
            self.assertIn(f"for m in '{PHONE_ROOT}/doom3/base/'sr_*.cfg.pf-orig", restore["argv"][3])
            self.assertEqual(page.eval_on_selector_all("#open-shots figcaption", "e => e.map(x => x.textContent.split(' · ').slice(0, 2).join(' · '))"),
                             ["用 libdoom3.so 打开 · AAP-AN00", "打开原版 · AAP-AN00"])

            # A step that fails stops the chain.
            page.unroute(re.compile(r".*/dash/api/jobs(\?.*)?$"))
            page.unroute(re.compile(r".*/dash/api/jobs/f[0-9a-f]{11}.*"))
            submitted = self.fake_jobs(page, fail=("放入并启动",))
            page.click("#open-result")
            page.wait_for_selector("#open-message.task-error")
            self.assertEqual(page.text_content("#open-message"), "「放入并启动」没有成功（退出码 3），后面的步骤没有做。")
            self.assertEqual(len(submitted), 2)
            self.assertFalse(page.is_disabled("#open-result"))
            page.set_viewport_size({"width": 390, "height": 844})
            self.assertEqual(page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth"), 0)
            self.assertEqual(errors, [])

    def test_placing_twice_and_opening_the_original_twice_keep_the_original_files(self):
        """The scripts the page sends, run by a shell against a directory standing in for the phone."""
        phone, stage = self.tmp / "phone", self.tmp / "stage" / "pf-open-"
        stage.parent.mkdir()
        doom = phone / "doom3"
        (doom / "base").mkdir(parents=True)
        (doom / "libdoom3.so").write_bytes(b"original engine")
        (doom / "base" / "autoexec.cfg").write_bytes(b"the player's own")
        opening = {**OPENING, "root": str(phone)}
        files = [{"job": RUN, "path": "x/src/libdoom3.so"}, {"job": RUN, "path": "x/sr_on.cfg"}]
        with self._browser_page() as page:
            page.goto("http://%s:%d/dash/research/open" % self.address)
            page.wait_for_function("() => window.HGGameOpen")
            place = page.evaluate("([o, f, s]) => HGGameOpen.placeFiles(o, f, s)", [opening, files, str(stage)])
            restore = page.evaluate("o => HGGameOpen.restoreFiles(o)", opening)
        run = lambda script: subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=10)
        state = lambda: {str(path.relative_to(phone)): path.read_bytes() for path in sorted(phone.rglob("*")) if path.is_file()}
        original = state()

        for engine in (b"new engine", b"newer engine"):
            Path(str(stage) + "libdoom3.so").write_bytes(engine)
            Path(str(stage) + "sr_on.cfg").write_bytes(b"set r_smallSR 2\n")
            done = run(place)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(state(), {**original, "doom3/libdoom3.so": engine, "doom3/libdoom3.so.pf-orig": b"original engine",
                                       "doom3/lib/arm64/libdoom3.so": engine, "doom3/lib/arm64/libdoom3.so.pf-absent": b"",
                                       "doom3/base/sr_on.cfg": b"set r_smallSR 2\n", "doom3/base/sr_on.cfg.pf-absent": b""},
                             "a second placement keeps the first backup, which is the original")
            self.assertFalse(list(stage.parent.iterdir()), "the pushed copies are removed")
        for _ in range(2):
            done = run(restore)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(state(), original)


if __name__ == "__main__":
    unittest.main()
