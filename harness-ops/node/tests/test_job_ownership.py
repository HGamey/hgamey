import io
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from node_service import Jobs, _job_lock_args
from job_policy import PolicyUnavailable
from job_queue import RequestConflict


class JobOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = self.tmp.name
        self.bins = {name: (str(Path(root) / name), root)
                     for name in ("phonefarm", "game_opt_loop", "adb", "fetch_apk", "devlock")}
        self.jobs = Jobs(root, self.bins)
        # Inspect the process boundary without running binaries or touching devices.
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
            command = self.jobs._read_root.read_json(meta["id"] + "/prepared.json")["command"]
            self.assertEqual(meta["label"], "shared")
            self.assertEqual(meta["argv"], argv)  # Preserve the submitted record.
            self.assertEqual(command, [self.bins[name][0], argv[0], "--lock-label", meta["lock_label"]])
        self.assertNotEqual(metas[0]["lock_label"], metas[1]["lock_label"])
        for sub, option in (("gray-layer", "--actor-id"), ("gray-layer", "--power-policy"), ("gray-ab", "--serial")):
            self.assertEqual(_job_lock_args([sub, option, "--lock-label", "--lock-label", "external"], "job-owner"),
                             [sub, option, "--lock-label", "--lock-label", "job-owner"])

    def test_execution_policy_refuses_before_preparation_and_is_part_of_request_identity(self):
        spec = {"bin": "devlock", "argv": [], "device_access": "none"}
        with patch.object(self.jobs, "_prepare") as prepare:
            with self.assertRaises(PolicyUnavailable):
                self.jobs.start({**spec, "execution_policy": "contained"}, b"not an archive", "strict")
            for invalid in (None, [], "unknown"):
                with self.subTest(policy=invalid), self.assertRaises(ValueError):
                    self.jobs.start({**spec, "execution_policy": invalid}, b"", "invalid")
            prepare.assert_not_called()
            self.assertEqual(self.jobs.dispatch.queue.rows(), [])
        legacy = self.jobs.start(spec, b"", "legacy")
        explicit = self.jobs.start({**spec, "execution_policy": "process_group"}, b"", "explicit")
        self.assertEqual((legacy["execution_policy"], legacy["execution_policy_source"]),
                         ("process_group", "legacy_default"))
        self.assertEqual(explicit["execution_policy_source"], "explicit")
        self.assertNotIn("process_management", explicit["execution"])
        self.assertEqual(self.jobs.start(spec, b"", "legacy")["id"], legacy["id"])
        with self.assertRaises(RequestConflict):
            self.jobs.start({**spec, "execution_policy": "process_group"}, b"", "legacy")

    def test_raw_commands_use_devlock_and_lease_helper_stays_unwrapped(self):
        for name in ("adb", "fetch_apk"):
            with self.subTest(bin=name):
                meta = self.jobs.start({"bin": name, "argv": ["argument with spaces"]}, b"")
                self.assertEqual(self.jobs._read_root.read_json(meta["id"] + "/prepared.json")["command"],
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
        command = self.jobs._read_root.read_json(meta["id"] + "/prepared.json")["command"]
        self.assertEqual(command, [os.path.realpath(self.jobs.path(meta["id"], "in", "helper")),
                                   self.jobs.path(meta["id"], "out")])
        self.assertEqual(meta["lock_mode"], "internal_or_helper")

        meta = self.jobs.start({"bin": "devlock", "argv": ["renew", "external-owner"]}, b"")
        self.assertEqual(self.jobs._read_root.read_json(meta["id"] + "/prepared.json")["command"], [self.bins["devlock"][0], "renew", "external-owner"])
        self.assertEqual(meta["lock_mode"], "management")
        self.assertIsNone(meta["lock_label"])


if __name__ == "__main__":
    unittest.main()
