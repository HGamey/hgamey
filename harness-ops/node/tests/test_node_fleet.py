import os
from pathlib import Path
import tempfile
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from node_fleet import FleetState, read_lock


class FleetStateTests(unittest.TestCase):
    def test_incomplete_or_stale_lock_is_never_free_and_queue_is_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "devlock.queue").write_text("waiting-agent\n")
            self.assertEqual(read_lock(directory)["queue"], ["waiting-agent"])
            self.assertEqual(read_lock(directory)["status"], "free")
            (root / "devlock.d").mkdir()
            self.assertEqual(read_lock(directory)["status"], "unknown")
            owner = root / "devlock.d" / "owner"
            owner.write_text("measuring since 2026-10-22 10:00:00\n")
            os.utime(owner, (time.time() - 700, time.time() - 700))
            lock = read_lock(directory)
            self.assertEqual((lock["status"], lock["held_by"], lock["stale"]), ("held", "measuring", True))

    def test_unknown_lock_defers_without_touching_phone_and_unknown_is_not_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "devlock.d").mkdir()
            state = FleetState(directory, directory, str(Path(directory) / "shots"))
            try:
                self.assertIsNone(state.snapshot()["devices"])
                self.assertIsNone(state._thread)
                with patch("node_fleet.subprocess.run") as run:
                    result = state.request(wait=1)
                    run.assert_not_called()
                    self.assertIsNone(result["devices"])
                    self.assertTrue(result["deferred"])
            finally:
                state.close()


if __name__ == "__main__":
    unittest.main()
