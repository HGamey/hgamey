"""Retention regressions for live, active and rollback overlay releases."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

NODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NODE))

import deploy_overlay


class PruneSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "node"
        (self.root / "releases").mkdir(parents=True)
        (self.root / "deploy-state").mkdir()

    def release(self, run_id, previous=None):
        component = self.root / "releases" / run_id / deploy_overlay.COMPONENT
        component.mkdir(parents=True)
        (component / "version").write_text(run_id + "\n")
        state = self.root / "deploy-state" / run_id
        state.mkdir()
        receipt = {"schema": deploy_overlay.SCHEMA, "run_id": run_id,
                   "status": "active", "previous": previous}
        (state / "receipt.json").write_text(json.dumps(receipt))
        return component.parent

    def activate(self, run_id):
        os.symlink(f"releases/{run_id}/{deploy_overlay.COMPONENT}", self.root / deploy_overlay.COMPONENT)

    def names(self):
        return {path.name for path in (self.root / "releases").iterdir()}

    def test_prune_keeps_active_cursor_and_default_rollback_target(self):
        rollback = self.release("r000")
        active = self.release("r001", previous=rollback.name)
        self.release("r002")
        self.release("r003")
        self.release("r004")
        self.activate(active.name)

        with mock.patch.object(deploy_overlay, "process_release_users",
                               return_value=set(), create=True):
            deploy_overlay.prune(self.root, keep=1)

        self.assertEqual(self.names(), {"r000", "r001", "r004"})
        self.assertTrue((rollback / deploy_overlay.COMPONENT / "version").is_file())

    def test_prune_keeps_release_reported_in_use(self):
        in_use = self.release("r001")
        active = self.release("r002", previous=None)
        self.activate(active.name)

        with mock.patch.object(deploy_overlay, "process_release_users",
                               return_value={"r001"}, create=True) as users:
            deploy_overlay.prune(self.root, keep=1)

        self.assertEqual(self.names(), {"r001", "r002"})
        users.assert_called_once_with([in_use])

    def test_linux_detector_covers_links_and_maps_with_path_boundaries(self):
        proc = Path(self.temp.name) / "proc"
        release = self.release("r001")
        sibling = self.release("r001-old")
        mapped = release / deploy_overlay.COMPONENT / "mapped.py"
        opened = release / deploy_overlay.COMPONENT / "opened.py"
        mapped.write_text("mapped\n")
        opened.write_text("opened\n")
        process = proc / "123"
        (process / "fd").mkdir(parents=True)
        os.symlink(release / deploy_overlay.COMPONENT, process / "cwd")
        os.symlink(opened, process / "fd" / "3")
        (process / "maps").write_text(
            "7f000-7f001 r--p 00000000 00:00 1 " + str(mapped) + " (deleted)\n")
        outside = process.parent / "124"
        (outside / "fd").mkdir(parents=True)
        os.symlink(sibling / deploy_overlay.COMPONENT, outside / "exe")
        scripted_release = self.release("r002")
        scripted = process.parent / "125"
        (scripted / "fd").mkdir(parents=True)
        (scripted / "cmdline").write_bytes(
            b"/usr/bin/python3\0" + os.fsencode(
                scripted_release / deploy_overlay.COMPONENT / "node_service.py") + b"\0")

        users = deploy_overlay.linux_release_users(
            [release, sibling, scripted_release], proc_root=proc)

        self.assertEqual(users, {"r001", "r001-old", "r002"})

    def test_macos_detector_parses_lsof_and_fails_closed(self):
        release = self.release("r001")
        other = self.release("r002")
        output = "p123\nn" + str(release / deploy_overlay.COMPONENT / "node_service.py") + "\n"

        def successful(command, **kwargs):
            if command[0] == "/usr/sbin/lsof":
                self.assertEqual(command[:2], ["/usr/sbin/lsof", "-nP"])
                stdout = output
            else:
                self.assertEqual(command, ["/bin/ps", "-ax", "-o", "args="])
                stdout = "/usr/bin/python3 " + str(
                    other / deploy_overlay.COMPONENT / "node_service.py") + "\n"
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

        users = deploy_overlay.macos_release_users([release, other], runner=successful)
        self.assertEqual(users, {"r001", "r002"})

        def failed(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="unavailable")

        users = deploy_overlay.macos_release_users([release, other], runner=failed)
        self.assertEqual(users, {"r001", "r002"})


if __name__ == "__main__":
    unittest.main()
