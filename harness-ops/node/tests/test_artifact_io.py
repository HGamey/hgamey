"""Deterministic open-time races against local fixtures; never use devices."""
import errno
from contextlib import ExitStack
import hashlib
import http.client
import io
import json
import os
import stat
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import artifact_io
import node_service
from artifact_io import ArtifactChanged, describe_file, ensure_unchanged
from node_service import Jobs, make_handler, readonly_token


class ArtifactIOTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.jobs = Jobs(str(self.base / "service"), {})
        self.jid = "0123456789ab"
        self.job = Path(self.jobs.path(self.jid))
        self.out = self.job / "out"
        (self.out / "nested").mkdir(parents=True)
        self.file = self.out / "nested" / "result.bin"
        self.file.write_bytes(b"safe fixture bytes")
        (self.job / "log").write_bytes(b"safe log")
        self.jobs._write_meta(self.jid, {"id": self.jid, "started": 1, "exit": 0})
        self.external = self.base / "outside"
        self.external.mkdir()
        (self.external / "result.bin").write_bytes(b"outside fixture must not be exposed")

    def tearDown(self):
        self.jobs._read_root.close()
        self.tmp.cleanup()

    def swap_on_open(self, name, action, occurrence=1):
        real_open = os.open
        calls = 0
        def opening(path, flags, *args, **kwargs):
            nonlocal calls
            if path == name and kwargs.get("dir_fd") is not None:
                calls += 1
                if calls == occurrence:
                    action()
            return real_open(path, flags, *args, **kwargs)
        return patch.object(artifact_io.os, "open", opening)

    def test_file_swap_to_symlink_at_open_is_rejected(self):
        def swap():
            self.file.unlink()
            self.file.symlink_to(self.external / "result.bin")
        with self.swap_on_open("result.bin", swap):
            with self.assertRaises(ValueError):
                self.jobs.out_blob(self.jid, "nested/result.bin")

    def test_parent_swap_before_open_is_rejected(self):
        def swap():
            (self.out / "nested").rename(self.out / "saved")
            (self.out / "nested").symlink_to(self.external, target_is_directory=True)
        with self.swap_on_open("nested", swap):
            with self.assertRaises(ValueError):
                self.jobs.out_blob(self.jid, "nested/result.bin")

    def test_parent_swap_after_open_cannot_redirect_child(self):
        def swap():
            (self.out / "nested").rename(self.out / "saved")
            (self.out / "nested").symlink_to(self.external, target_is_directory=True)
        with self.swap_on_open("result.bin", swap):
            f, info = self.jobs.out_blob(self.jid, "nested/result.bin")
        with f:
            ensure_unchanged(f)
            data = f.read()
        self.assertEqual(data, b"safe fixture bytes")
        self.assertEqual(hashlib.sha256(data).hexdigest(), info["sha256"])

    def test_download_keeps_original_fd_after_path_replacement(self):
        f, info = self.jobs.out_blob(self.jid, "nested/result.bin")
        with f:
            self.file.unlink()
            self.file.symlink_to(self.external / "result.bin")
            # Hash/HTTP validation may reject the changed inode metadata, but
            # streaming never follows the replacement path to the outside file.
            with self.assertRaises(ArtifactChanged):
                ensure_unchanged(f)
            self.assertEqual(f.read(), b"safe fixture bytes")
        self.assertEqual(info["size"], len(b"safe fixture bytes"))

    def test_http_rejects_changed_file_before_sending_headers(self):
        original = self.jobs.out_blob
        def changed(*args):
            opened, info = original(*args)
            with self.file.open("ab") as writer:
                writer.write(b"concurrent writer")
            return opened, info
        admin = "unit-test-artifact-read-token-not-production"
        class HostOnly:
            def handle(self, *args):
                raise AssertionError("no device calls")
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(HostOnly(), self.jobs, admin))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(self.jobs, "out_blob", changed):
                connection = http.client.HTTPConnection(*server.server_address, timeout=3)
                connection.request("GET", f"/jobs/{self.jid}/out?path=nested/result.bin",
                                   headers={"Authorization": "Bearer " + readonly_token(admin)})
                response = connection.getresponse()
                self.assertEqual(response.status, 409)
                self.assertNotIn(b"outside fixture", response.read())
                connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_archive_rejects_symlink_inserted_after_signature_walk(self):
        def swap():
            self.file.unlink()
            self.file.symlink_to(self.external / "result.bin")
        with self.swap_on_open("result.bin", swap, occurrence=2):
            with self.assertRaises(ValueError):
                self.jobs.out_blob(self.jid)
        self.assertEqual(list(self.job.glob(".outcache-*.tmp")), [])
        self.assertEqual(list(self.job.glob(".outcache-*.tar.gz")), [])

    def test_directory_archive_cache_contains_only_opened_regular_files(self):
        first, info1 = self.jobs.out_blob(self.jid)
        with first:
            ensure_unchanged(first)
            content = first.read()
        self.assertEqual(hashlib.sha256(content).hexdigest(), info1["sha256"])
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
            self.assertTrue(all(m.isdir() or m.isfile() for m in archive.getmembers()))
            self.assertEqual(archive.extractfile("./nested/result.bin").read(), b"safe fixture bytes")
        cached, info2 = self.jobs.out_blob(self.jid)
        with cached:
            ensure_unchanged(cached)
            self.assertEqual(cached.read(), content)
        self.assertEqual(info1, info2)

        # Two readers share only the job's filesystem lock, not a Python mutex.
        peer = Jobs(self.jobs.root, {})
        self.addCleanup(peer._read_root.close)
        admin = "unit-test-export-token-not-production"
        requests = []
        handler = make_handler(None, self.jobs, admin)
        class RecordingHandler(handler):
            def do_GET(self):
                requests.append(self.path)
                return super().do_GET()

            def log_message(self, *_):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), RecordingHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def get(path, headers=None):
            connection = http.client.HTTPConnection(*server.server_address, timeout=3)
            try:
                connection.request("GET", path, headers={"Authorization": "Bearer " + readonly_token(admin), **(headers or {})})
                response = connection.getresponse()
                return response.status, dict(response.getheaders()), response.read()
            finally:
                connection.close()

        try:
            identity = json.loads(get("/node")[2])
            self.assertIn("jobs.export-limits-v1", identity["capabilities"])
            self.assertEqual(identity["execution"]["export_limits"], artifact_io.EXPORT_LIMITS)
            with patch.object(node_service, "archive_directory", side_effect=AssertionError("cache hit must not rebuild")):
                status, _, data = get(f"/jobs/{self.jid}/out?meta=1")
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(data), info1)
                status, headers, data = get(f"/jobs/{self.jid}/out", {"Range": "bytes=1-8"})
                self.assertEqual((status, data), (206, content[1:9]))
                self.assertEqual(headers["Content-Range"], f"bytes 1-8/{len(content)}")
            with artifact_io.export_lock(self.jobs._read_root, self.jid) as jobfd:
                self.assertEqual(os.fstat(jobfd).st_ino, self.job.stat().st_ino)
                lock_identity = (self.job / ".out-export.lock").stat().st_ino
                opened, _ = peer.out_blob(self.jid)
                with opened:
                    self.assertEqual(opened.read(), content, "an existing cache is readable during another build")
                # Use a bounded thread so a regression to blocking flock cannot
                # hang the suite; release the owned lock before joining it.
                results = []
                def another_builder():
                    try:
                        opened, _ = peer.out_blob(self.jid, "nested")
                        opened.close()
                        results.append(None)
                    except BaseException as error:
                        results.append(error)
                contender = threading.Thread(target=another_builder, daemon=True)
                contender.start()
                contender.join(1)
                completed_without_release = not contender.is_alive()
                if completed_without_release:
                    status, _, data = get(f"/jobs/{self.jid}/out?path=nested&meta=1")
                    self.assertEqual((status, json.loads(data)["code"]), (409, "export_busy"))
            contender.join(3)
            self.assertTrue(completed_without_release, "second reader blocked on the existing builder")
            self.assertIsInstance(results[0], artifact_io.ExportError)
            self.assertEqual((results[0].status, results[0].code), (409, "export_busy"))
            self.assertEqual((self.job / ".out-export.lock").stat().st_ino, lock_identity)

            # Metadata rejection and a later chunk rejection both preserve the
            # CLI's existing partial; export_busy is never a source-change retry.
            destination = self.base / "download"
            partial = self.base / f".pf-node-{self.jid}-{info1['etag']}.part"
            partial.write_bytes(content[:5])
            original = self.jobs.out_blob
            env = {"PATH": "/usr/bin:/bin", "HOME": str(self.base), "LANG": "C.UTF-8",
                   "PF_NODE_URL": f"http://127.0.0.1:{server.server_port}", "PF_NODE_ID": identity["node_id"],
                   "PF_NODE_TOKEN": readonly_token(admin), "PYTHONDONTWRITEBYTECODE": "1"}
            for reject_call in (1, 2):
                calls = []
                def busy_export(*args):
                    calls.append(args)
                    if len(calls) >= reject_call:
                        raise artifact_io.ExportError(409, "export_busy", "fixture builder is occupied")
                    return original(*args)
                begin = len(requests)
                with patch.object(self.jobs, "out_blob", busy_export):
                    cli = subprocess.run([sys.executable, str(Path(node_service.__file__).with_name("pf-node")),
                                          "--out-dir", str(destination), "out", self.jid],
                                         env=env, cwd=self.base, capture_output=True, text=True, timeout=5)
                self.assertNotEqual(cli.returncode, 0, cli.stdout)
                self.assertIn("export_busy", cli.stderr)
                self.assertEqual(len(calls), reject_call)
                self.assertEqual(sum("meta=1" in path for path in requests[begin:]), 1)
                self.assertEqual(partial.read_bytes(), content[:5])
            self.assertFalse(destination.exists())
            self.assertEqual(self.file.read_bytes(), b"safe fixture bytes")
            self.assertEqual(list(self.job.glob(".outcache-*.tmp")), [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
            self.jobs.directory.close()

    def test_export_limits_short_writes_and_failures_preserve_sources_and_old_cache(self):
        original_write = os.write
        accepted = []
        def short_write(fd, data):
            count = original_write(fd, data[:7])
            accepted.append(count)
            return count
        with patch.object(artifact_io.os, "write", short_write):
            opened, _ = self.jobs.out_blob(self.jid)
        with opened:
            old_bytes = opened.read()
        self.assertEqual(sum(accepted), len(old_bytes))
        self.assertGreater(len(accepted), 3)
        with tarfile.open(fileobj=io.BytesIO(old_bytes), mode="r:gz") as archive:
            self.assertEqual(archive.extractfile("./nested/result.bin").read(), self.file.read_bytes())
        cache = next(self.job.glob(".outcache-*.tar.gz"))
        descriptor = Path(str(cache) + ".json")
        old_descriptor, old_inode = descriptor.read_bytes(), cache.stat().st_ino
        changed_source = b"source changed after the valid old cache"
        self.file.write_bytes(changed_source)
        original_create = self.jobs._read_root.create_file
        for fault, limits, status, code in (
                ("entries", {"entries": 2}, 413, "export_budget_exceeded"),
                ("source_bytes", {"source_bytes": len(changed_source) - 1}, 413, "export_budget_exceeded"),
                ("compressed_bytes", {"compressed_bytes": 16}, 413, "export_budget_exceeded"),
                ("space", {}, 507, "export_space_insufficient"),
                ("enospc", {}, 507, "export_write_space_failed")):
            with self.subTest(fault=fault):
                temporary, writes, failed = [], [], []
                def created(rel):
                    file = original_create(rel)
                    if ".outcache-" in rel and rel.endswith(".tmp"):
                        temporary.append((rel, os.fstat(file.fileno()).st_ino))
                    return file
                def writing(fd, data):
                    if failed:
                        self.fail("gzip attempted another actual write after a latched failure")
                    if fault == "enospc" and writes:
                        failed.append(True)
                        raise OSError(errno.ENOSPC, "fixture cache space exhausted")
                    count = original_write(fd, data[:1] if fault == "enospc" else data)
                    writes.append(count)
                    return count
                with ExitStack() as patches:
                    patches.enter_context(patch.dict(artifact_io.EXPORT_LIMITS, limits))
                    patches.enter_context(patch.object(self.jobs._read_root, "create_file", created))
                    patches.enter_context(patch.object(artifact_io.os, "write", writing))
                    if fault == "space":
                        patches.enter_context(patch.object(artifact_io.os, "fstatvfs",
                                                          return_value=SimpleNamespace(f_bavail=0, f_frsize=4096)))
                    with self.assertRaises(artifact_io.ExportError) as caught:
                        self.jobs.out_blob(self.jid)
                error = caught.exception
                self.assertEqual((error.status, error.code), (status, code))
                self.assertEqual(error.payload["scope"], "directory_export_cache_generation")
                if fault in ("entries", "source_bytes", "compressed_bytes"):
                    self.assertEqual(error.details["dimension"], fault)
                    self.assertGreater(error.details["observed"], error.details["limit"])
                if fault in ("entries", "source_bytes"):
                    self.assertEqual(temporary, [], "static source budget must reject before creating a cache")
                if fault == "compressed_bytes":
                    self.assertLessEqual(sum(writes), 16, "gzip close must not append beyond the limit")
                if fault == "space":
                    self.assertEqual(writes, [])
                    self.assertEqual(error.details["available_bytes"], 0)
                if fault == "enospc":
                    self.assertTrue(failed)
                    self.assertEqual(sum(writes), 1)
                    self.assertEqual(error.details["written_bytes"], 1)
                self.assertEqual(self.file.read_bytes(), changed_source)
                self.assertEqual(cache.read_bytes(), old_bytes)
                self.assertEqual(cache.stat().st_ino, old_inode)
                self.assertEqual(descriptor.read_bytes(), old_descriptor)
                self.assertEqual(list(self.job.glob(".outcache-*.tmp")), [])

    def test_log_and_metadata_swap_to_symlink_are_rejected(self):
        for name, read in (("log", lambda: self.jobs.log_from(self.jid, 0)),
                           ("meta.json", lambda: self.jobs.status(self.jid))):
            with self.subTest(name=name):
                original = self.job / name
                saved = original.read_bytes()
                def swap():
                    original.unlink()
                    original.symlink_to(self.external / "result.bin")
                with self.swap_on_open(name, swap):
                    with self.assertRaises(ValueError):
                        read()
                original.unlink()
                original.write_bytes(saved)

    def test_special_log_and_oversize_metadata_do_not_block_or_read_unbounded(self):
        (self.job / "log").unlink()
        os.mkfifo(self.job / "log")
        with self.assertRaises(ValueError):
            self.jobs.log_from(self.jid, 0)
        (self.job / "meta.json").write_bytes(b" " * ((1 << 20) + 1))
        with self.assertRaisesRegex(ValueError, "read limit"):
            self.jobs.status(self.jid)
        (self.job / "meta.json").unlink()
        os.mkfifo(self.job / "meta.json")
        with self.assertRaises(ValueError):
            self.jobs.status(self.jid)

    def test_mutation_during_hash_is_not_returned_as_a_valid_digest(self):
        path = self.file
        class ChangingReader:
            def __init__(self, source):
                self.source, self.changed = source, False
            def fileno(self):
                return self.source.fileno()
            def seek(self, *args):
                return self.source.seek(*args)
            def read(self, *args):
                value = self.source.read(*args)
                if not self.changed:
                    self.changed = True
                    with path.open("ab") as writer:
                        writer.write(b"new bytes")
                return value
        with self.file.open("rb") as source:
            with self.assertRaises(ArtifactChanged):
                describe_file(ChangingReader(source))


class ArtifactDurabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="artifact-durability-")
        self.base = Path(self.tmp.name)
        self.store = artifact_io.SafeRoot(self.base)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)

    def test_json_failure_before_and_after_rename_preserves_error_boundary(self):
        self.store.write_json("result.json", {"old": True})
        original = (self.base / "result.json").read_bytes()
        real_sync = os.fsync
        events = []

        def fail_file(fd):
            if stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(errno.EIO, "fixture file sync failure")
            real_sync(fd)

        with patch.object(artifact_io.os, "fsync", fail_file):
            with self.assertRaisesRegex(OSError, "file sync failure"):
                self.store.write_json("result.json", {"new": True})
        self.assertEqual((self.base / "result.json").read_bytes(), original)

        def fail_parent(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                events.append("directory")
                # The data is visible, but the operation must still raise.
                self.assertEqual(self.store.read_json("result.json"), {"new": True})
                raise OSError(errno.EIO, "fixture directory sync failure")
            events.append("file")
            real_sync(fd)

        with patch.object(artifact_io.os, "fsync", fail_parent):
            with self.assertRaisesRegex(OSError, "directory sync failure"):
                self.store.write_json("result.json", {"new": True})
        self.assertEqual(events, ["file", "directory"])
        self.assertEqual(list(self.base.glob("*.tmp")), [])

    def test_publish_once_resyncs_existing_target_before_acknowledgement(self):
        self.assertTrue(self.store.publish_json_once("intent.json", {"first": True}))
        target_inode = (self.base / "intent.json").stat().st_ino
        root_inode = self.base.stat().st_ino
        real_sync = os.fsync
        synced = []

        def recording(fd):
            synced.append(os.fstat(fd).st_ino)
            real_sync(fd)

        with patch.object(artifact_io.os, "fsync", recording):
            self.assertFalse(self.store.publish_json_once("intent.json", {"second": True}))
        self.assertEqual(synced[-2:], [target_inode, root_inode])
        self.assertEqual(self.store.read_json("intent.json"), {"first": True})

        def fail_parent(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError(errno.EIO, "existing target directory sync failed")
            real_sync(fd)

        with patch.object(artifact_io.os, "fsync", fail_parent):
            with self.assertRaisesRegex(OSError, "existing target directory sync failed"):
                self.store.publish_json_once("intent.json", {"second": True})
        (self.base / "alias.json").symlink_to(self.base / "intent.json")
        with self.assertRaises(ValueError):
            self.store.publish_json_once("alias.json", {})

    def test_tree_and_mkdir_sync_real_objects_leaf_first(self):
        self.store.mkdir("input")
        self.store.mkdir("input")
        self.store.mkdir("input/nested")
        leaf = self.base / "input/nested/data"
        leaf.write_bytes(b"fixed input")
        real_sync = os.fsync
        synced = []

        def recording(fd):
            synced.append(os.fstat(fd).st_ino)
            real_sync(fd)

        with patch.object(artifact_io.os, "fsync", recording):
            self.store.sync_tree("input")
        self.assertEqual(synced, [p.stat().st_ino for p in
                                 (leaf, leaf.parent, self.base / "input", self.base)])
        synced.clear()
        with patch.object(artifact_io.os, "fsync", recording):
            self.store.sync("")
        self.assertEqual(synced, [self.base.stat().st_ino, self.base.parent.stat().st_ino])
        self.store.sync("input/nested/data", directory=False)
        with self.assertRaises(ValueError):
            self.store.mkdir("input/nested/data")
        link = self.base / "input/link"
        link.symlink_to(leaf.parent, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.store.mkdir("input/link")
        with self.assertRaises(ValueError):
            self.store.sync_tree("input")
        link.unlink()
        os.mkfifo(self.base / "input/fifo")
        with self.assertRaises(ValueError):
            self.store.sync_tree("input")


if __name__ == "__main__":
    unittest.main()
