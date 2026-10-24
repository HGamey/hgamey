import json
from pathlib import Path
import stat
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from node_directory import DirectoryError, NodeDirectory


LOCAL = "a" * 32
REMOTE = "b" * 32


def response(path, node_id=REMOTE):
    base = {"schema_version": 1, "node_id": node_id}
    if path == "/node":
        return {**base, "capabilities": ["fleet.cached-v1"], "host": {"hostname": "fixture"}}
    if path.startswith("/fleet?"):
        return {**base, "devices": [{"transport": "adb", "serial": "fixture-only"}],
                "lock": {"status": "unknown"}, "age_s": 12, "sampled_at": 123}
    if path == "/queue":
        return {**base, "items": []}
    raise AssertionError(path)


class NodeDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = NodeDirectory(self.temp.name, LOCAL)

    def tearDown(self):
        self.directory.close()
        with self.directory._guard:
            self.assertTrue(self.directory._guard.wait_for(lambda: not self.directory._workers, timeout=2))
        self.temp.cleanup()

    def drain(self):
        with self.directory._guard:
            self.assertTrue(self.directory._guard.wait_for(lambda: not self.directory._inflight, timeout=2))

    def create(self, name="remote", **fields):
        return self.directory.create({"name": name, "url": "http://fixture.invalid/prefix/",
                                      "token": "fixture-credential-never-production", **fields})

    def test_configuration_is_durable_private_and_never_probes_on_read(self):
        with patch.object(self.directory, "_request") as request:
            entry = self.create(tags=["android"], ssh="fixture-host", goals_dir="/fixture/goals")
            self.assertEqual(entry["url"], "http://fixture.invalid/prefix")
            self.assertEqual(entry["connection"]["status"], "unverified")
            self.assertEqual(self.directory.list(), [entry])
            self.assertEqual(self.directory.get("remote"), entry)
            request.assert_not_called()
        self.assertNotIn("fixture-credential-never-production", json.dumps(entry))
        self.assertNotIn("token", entry)
        private = Path(self.temp.name) / "_node" / "directory"
        self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((private / "registry.json").stat().st_mode), 0o600)
        self.directory.close()
        self.directory = NodeDirectory(self.temp.name, LOCAL)
        self.assertEqual(self.directory.get("remote"), entry)
        changed = self.directory.update("remote", {"revision": entry["revision"], "token": None})
        self.assertEqual(changed["connection"]["status"], "no_credentials")
        self.assertFalse(changed["credential_present"])
        with self.assertRaises(DirectoryError) as conflict:
            self.directory.update("remote", {"revision": entry["revision"], "enabled": False})
        self.assertEqual(conflict.exception.status, 409)

    def test_offline_and_changed_identity_preserve_the_last_complete_snapshot(self):
        self.create()
        calls = []

        def read(entry, path, deadline):
            calls.append(path)
            return response(path)

        with patch.object(self.directory, "_request", side_effect=read):
            self.directory.refresh()
            self.drain()
            ready = self.directory.get("remote")
            self.directory.refresh()
            self.assertEqual(calls, ["/node", "/fleet?cached=1", "/queue"])
        self.assertEqual(ready["node_id"], REMOTE)
        self.assertEqual(ready["connection"]["status"], "ready")
        self.assertEqual(ready["snapshot"]["fleet"]["age_s"], 12)
        with patch.object(self.directory, "_request", side_effect=OSError("fixture-credential-never-production")):
            self.directory.refresh("remote", devices=True)
            self.drain()
        offline = self.directory.get("remote")
        self.assertEqual(offline["snapshot"], ready["snapshot"])
        self.assertEqual(offline["connection"]["last_seen_at"], ready["connection"]["last_seen_at"])
        self.assertEqual(offline["connection"]["status"], "unavailable")
        self.assertTrue(offline["stale"])
        self.assertNotIn("fixture-credential-never-production", json.dumps(offline))
        with patch.object(self.directory, "_request", side_effect=lambda entry, path, deadline: response(path, "c" * 32)) as changed:
            self.directory.refresh("remote", devices=True)
            self.drain()
            self.assertEqual(changed.call_count, 1)  # No fleet probe after a different /node identity.
        mismatch = self.directory.get("remote")
        self.assertEqual(mismatch["snapshot"], ready["snapshot"])
        self.assertEqual(mismatch["connection"]["status"], "identity_mismatch")
        self.create("duplicate")
        with patch.object(self.directory, "_request", side_effect=read):
            self.directory.refresh("duplicate")
            self.drain()
        duplicate = self.directory.get("duplicate")
        self.assertIsNone(duplicate["node_id"])
        self.assertEqual(duplicate["connection"]["status"], "identity_mismatch")

    def test_inflight_configuration_change_discards_old_identity_and_result(self):
        original = self.create()
        entered, release = threading.Event(), threading.Event()
        calls = []

        def read(entry, path, deadline):
            calls.append((entry["url"], path))
            entered.set()
            if not release.wait(2):
                raise AssertionError("fixture release was not signaled")
            return response(path)

        with patch.object(self.directory, "_request", side_effect=read):
            self.directory.refresh("remote")
            self.assertTrue(entered.wait(2))
            try:
                with self.assertRaises(DirectoryError) as busy:
                    self.directory.refresh("remote", devices=True)
                self.assertEqual(busy.exception.status, 409)
                changed = self.directory.update("remote", {"revision": original["revision"],
                                                            "url": "http://new-fixture.invalid", "enabled": False})
                self.assertFalse(changed["refreshing"])
            finally:
                release.set()
            self.drain()
        result = self.directory.get("remote")
        self.assertEqual(result["url"], "http://new-fixture.invalid")
        self.assertEqual(result["connection"]["status"], "disabled")
        self.assertIsNone(result["node_id"])
        self.assertIsNone(result["snapshot"])
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
