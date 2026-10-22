"""Deterministic open-time races against local fixtures; never use devices."""
import hashlib
import http.client
import io
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import artifact_io
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


if __name__ == "__main__":
    unittest.main()
