import io
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from node_service import Jobs


class JobOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = self.tmp.name
        self.bins = {name: (str(Path(root) / name), root)
                     for name in ("phonefarm", "game_opt_loop", "adb", "fetch_apk", "devlock")}
        self.jobs = Jobs(root, self.bins)
        # Inspect the process boundary without running binaries or touching devices.
        self.spawn_patch = patch("node_service.job_runtime.launch")
        self.spawn = self.spawn_patch.start()
        self.spawn.return_value.pid = 123
        self.addCleanup(self.spawn_patch.stop)
        self.thread_patch = patch("node_service.threading.Thread")
        self.thread_patch.start()
        self.addCleanup(self.thread_patch.stop)

    def test_same_display_label_has_independent_owner_even_with_cli_override(self):
        metas = []
        for name, argv in (("phonefarm", ["eval", "--lock-label", "shared"]),
                           ("game_opt_loop", ["optimize", "--lock-label=shared"])):
            with patch.dict(os.environ, {"DEVLOCK_LABEL": "inherited-parent"}):
                meta = self.jobs.start({"bin": name, "argv": argv, "label": "shared"}, b"")
            metas.append(meta)
            call = self.spawn.call_args
            self.assertEqual(meta["label"], "shared")
            self.assertEqual(meta["argv"], argv)  # Preserve the submitted record.
            self.assertEqual(call.kwargs["env"]["DEVLOCK_LABEL"], meta["lock_label"])
            self.assertEqual(call.args[3], [self.bins[name][0], argv[0], "--lock-label", meta["lock_label"]])
        self.assertNotEqual(metas[0]["lock_label"], metas[1]["lock_label"])

    def test_raw_commands_use_devlock_and_lease_helper_stays_unwrapped(self):
        for name in ("adb", "fetch_apk"):
            with self.subTest(bin=name):
                meta = self.jobs.start({"bin": name, "argv": ["argument with spaces"]}, b"")
                self.assertEqual(self.spawn.call_args.args[3],
                                 [self.bins["devlock"][0], "run", meta["lock_label"], "--",
                                  self.bins[name][0], "argument with spaces"])
                self.assertEqual(meta["lock_mode"], "devlock_run")
                with self.assertRaisesRegex(ValueError, "lease helper"):
                    self.jobs.start({"bin": name, "exe": "@in/helper"}, b"")

        upload = io.BytesIO()
        with tarfile.open(fileobj=upload, mode="w:gz") as archive:
            info = tarfile.TarInfo("helper")
            content = b"#!/bin/sh\nexit 0\n"
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        meta = self.jobs.start({"bin": "phonefarm", "exe": "@in/helper", "argv": ["@out"]}, upload.getvalue())
        command = self.spawn.call_args.args[3]
        self.assertEqual(command, [os.path.realpath(self.jobs.path(meta["id"], "in", "helper")),
                                   self.jobs.path(meta["id"], "out")])
        self.assertEqual(meta["lock_mode"], "internal_or_helper")

        meta = self.jobs.start({"bin": "devlock", "argv": ["renew", "external-owner"]}, b"")
        self.assertEqual(self.spawn.call_args.args[3], [self.bins["devlock"][0], "renew", "external-owner"])
        self.assertEqual(meta["lock_mode"], "management")
        self.assertIsNone(meta["lock_label"])


if __name__ == "__main__":
    unittest.main()
