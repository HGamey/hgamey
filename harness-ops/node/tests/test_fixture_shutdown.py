"""Keep fixture writers alive at the exact point temporary storage is removed."""
import os
from pathlib import Path
import subprocess
import threading
import unittest
from unittest.mock import patch

import test_node_games as fixture


class FixtureShutdown(unittest.TestCase):
    def test_teardown_stops_dispatcher_before_removing_jobs(self):
        env = fixture.GameFlowTests()
        env.setUp()
        dispatch = env.jobs.dispatch
        entered, write, written, pending = (threading.Event() for _ in range(4))
        pending.set()

        def tick():
            entered.set()
            write.wait()
            (Path(env.jobs.dir) / "late-writer").write_text("dispatch tick")
            written.set()

        remove = os.rmdir

        def remove_jobs(path, *args, **kwargs):
            if os.fspath(path) == env.jobs.dir or os.fspath(path) == "jobs":
                # Reproduce a write after rmtree scanned jobs but before rmdir.
                if thread.is_alive():
                    write.set()
                    self.assertTrue(written.wait(5))
            return remove(path, *args, **kwargs)

        with patch.object(dispatch, "_pending", side_effect=pending.is_set), \
                patch.object(dispatch, "tick", side_effect=tick):
            dispatch.start()
            thread = dispatch.thread
            self.assertTrue(entered.wait(5))
            join = thread.join

            def join_writer(timeout=None):
                # A real unbounded join releases the in-flight tick and waits.
                # The old timed join expires with the writer still parked.
                if timeout is None:
                    write.set()
                join(timeout)

            try:
                with patch.object(thread, "join", side_effect=join_writer), \
                        patch("os.rmdir", side_effect=remove_jobs):
                    env.tearDown()
                self.assertFalse(thread.is_alive())
                self.assertFalse(env.root.exists())
            finally:
                pending.clear()
                write.set()
                join(5)
                env.temp.cleanup()

    def test_teardown_waits_for_inflight_fleet_sample(self):
        env = fixture.GameFlowTests()
        env.setUp()
        entered, finish = threading.Event(), threading.Event()

        def sample(*args, **kwargs):
            entered.set()
            finish.wait()
            (env.root / "sample-finished").write_text("fleet sample")
            return subprocess.CompletedProcess(args[0], 0, b'{"schema":"phonefarm/fleet@1","devices":[]}', b'')

        cleanup = env.temp.cleanup

        def remove():
            self.assertFalse(thread.is_alive(), "fleet still writes into the temporary directory")
            cleanup()

        with patch("node_fleet.subprocess.run", side_effect=sample):
            env.board.monitor.request()
            thread = env.board.monitor._thread
            self.assertTrue(entered.wait(5))
            join = thread.join

            def join_sample(*args, **kwargs):
                finish.set()
                join(*args, **kwargs)

            try:
                with patch.object(thread, "join", side_effect=join_sample), \
                        patch.object(env.temp, "cleanup", side_effect=remove):
                    env.tearDown()
                self.assertFalse(env.root.exists())
            finally:
                finish.set()
                join(5)
                cleanup()
