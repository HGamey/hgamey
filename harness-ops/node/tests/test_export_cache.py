"""External derived exports retain source identity, original locks and errors."""
from contextlib import contextmanager, ExitStack
import http.client
from http.server import ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

NODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NODE))
import artifact_io
import job_export_cache
import node_service
from node_service import Jobs, make_handler, readonly_token

JID = "0123456789ab"
TOKEN = readonly_token("export-cache-fixture-admin-not-production")


@contextmanager
def endpoint(jobs):
    handler = make_handler(None, jobs, "export-cache-fixture-admin-not-production")
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def get(path, headers=None, *, method="GET", body=None):
        connection = http.client.HTTPConnection(*server.server_address, timeout=3)
        try:
            connection.request(method, path, body=body,
                               headers={"Authorization": "Bearer " + TOKEN, **(headers or {})})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()
    try:
        yield f"http://127.0.0.1:{server.server_port}", get
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        jobs.history.close()
        jobs.directory.close()
        jobs.games.close()
        if thread.is_alive():
            raise RuntimeError("export fixture HTTP thread did not exit")


class ExportCacheTests(unittest.TestCase):
    def test_overview_and_clear_all_are_bounded_and_regenerable_only(self):
        with tempfile.TemporaryDirectory(prefix="export-overview-") as temporary, ExitStack() as cleanup:
            root = Path(temporary).resolve()
            jobs = Jobs(str(root / "node"), {}, export_cache_root=str(root / "cache"))
            cleanup.callback(jobs._read_root.close)
            cleanup.callback(jobs.export_cache.close)
            second = "ba9876543210"
            for jid in (JID, second):
                job = Path(jobs.path(jid))
                (job / "out").mkdir(parents=True)
                (job / "out" / "result.bin").write_bytes(("original " + jid).encode())
                jobs._write_meta(jid, {"id": jid, "bin": "fixture", "started": 1, "exit": 0})
            originals = {jid: (Path(jobs.path(jid)) / "out/result.bin").read_bytes() for jid in (JID, second)}
            for jid in (JID, second):
                opened, _ = jobs.out_blob(jid)
                opened.read()
                opened.close()
            cache = root / "cache" / jobs.export_cache.namespace
            seeded = {jid: len(list((cache / jid).glob("*.tar.gz*"))) for jid in (JID, second)}
            self.assertTrue(all(count >= 1 for count in seeded.values()), "fixture expects cache entries per job")
            (cache / "not-a-job").mkdir()
            (cache / "not-a-job" / ".outcache-0123456789.tar.gz").write_bytes(b"untracked place")

            with endpoint(jobs) as (url, request):
                identity = json.loads(request("/node")[2])
                read_headers = {"X-Expected-Node-ID": identity["node_id"]}
                write_headers = {**read_headers, "Content-Type": "application/json",
                                 "Authorization": "Bearer export-cache-fixture-admin-not-production"}
                code, _, raw = request("/export-caches", read_headers)
                value = json.loads(raw)
                self.assertEqual(code, 200)
                self.assertEqual(value["schema"], "phonefarm/export-cache-overview@1")
                self.assertEqual([item["job_id"] for item in value["jobs"]], [JID, second],
                                 "only real job namespaces are enumerated")
                self.assertEqual(value["totals"]["count"], sum(seeded.values()))
                self.assertTrue(value["complete"])
                # 只读 token 可以查看聚合，但清理需要管理员写授权与显式确认体。
                self.assertEqual(request("/export-caches/clear", read_headers, method="POST",
                                         body=json.dumps({"schema": "phonefarm/export-cache-clear@1",
                                                          "confirm": "regenerable-only"}).encode())[0], 401)
                bad = request("/export-caches/clear", write_headers, method="POST", body=b"{}")
                self.assertEqual(bad[0], 400)
                wrong = request("/export-caches/clear", write_headers, method="POST",
                                body=json.dumps({"schema": "phonefarm/export-cache-clear@1",
                                                 "confirm": "everything"}).encode())
                self.assertEqual(wrong[0], 400)
                code, _, raw = request("/export-caches/clear", write_headers, method="POST",
                                       body=json.dumps({"schema": "phonefarm/export-cache-clear@1",
                                                        "confirm": "regenerable-only"}).encode())
                value = json.loads(raw)
                self.assertEqual(code, 200)
                self.assertEqual(value["deleted"]["count"], sum(seeded.values()))
                self.assertEqual(value["totals"]["count"], 0)
                self.assertTrue(value["complete"])
                for jid in (JID, second):
                    self.assertEqual(list((cache / jid).glob("*.tar.gz*")), [])
                    self.assertEqual((Path(jobs.path(jid)) / "out/result.bin").read_bytes(), originals[jid])
                self.assertTrue((cache / "not-a-job" / ".outcache-0123456789.tar.gz").exists(),
                                "non-job namespaces must not be touched")
                # 清理后聚合回到零，且不创建任何新目录。
                code, _, raw = request("/export-caches", read_headers)
                self.assertEqual(json.loads(raw)["totals"]["count"], 0)

    def test_explicit_cleanup_uses_export_lock_and_preserves_open_download_and_originals(self):
        with tempfile.TemporaryDirectory(prefix="export-cleanup-") as temporary, ExitStack() as cleanup:
            root = Path(temporary).resolve()
            jobs = Jobs(str(root / "node"), {}, export_cache_root=str(root / "cache"))
            cleanup.callback(jobs._read_root.close)
            cleanup.callback(jobs.export_cache.close)
            job = Path(jobs.path(JID))
            (job / "out").mkdir(parents=True)
            (job / "in").mkdir()
            (job / "out" / "result.bin").write_bytes(b"original result")
            (job / "in" / "input.bin").write_bytes(b"original input")
            jobs._write_meta(JID, {"id": JID, "bin": "fixture", "started": 1, "exit": 0})
            originals = {path: path.read_bytes() for path in (job / "meta.json", job / "in/input.bin", job / "out/result.bin")}
            path = f"/jobs/{JID}/export-cache"
            destination = root / "cache" / jobs.export_cache.namespace / JID
            with endpoint(jobs) as (url, request):
                identity = json.loads(request("/node")[2])
                self.assertIn("jobs.export-cache-cleanup-v1", identity["capabilities"])
                read_headers = {"X-Expected-Node-ID": identity["node_id"]}
                write_headers = {**read_headers, "Content-Type": "application/json",
                                 "Authorization": "Bearer export-cache-fixture-admin-not-production"}
                code, _, raw = request(path, read_headers)
                self.assertEqual((code, json.loads(raw)["count"], json.loads(raw)["bytes"]), (200, 0, 0))
                self.assertFalse(destination.parent.exists())
                self.assertFalse((job / ".out-export.lock").exists())
                self.assertEqual(request(path, {**read_headers, "Content-Type": "application/json"},
                                         method="POST", body=b"{}")[0], 401)
                self.assertEqual(request(path, {"Authorization": write_headers["Authorization"], "Content-Type": "application/json"},
                                         method="POST", body=b"{}")[0], 400)
                opened, _ = jobs.out_blob(JID)
                cleanup.callback(opened.close)
                expected = opened.read(); opened.seek(0)
                archives = list(destination.glob("*.tar.gz"))
                metadata = Path(str(archives[0]) + ".json")
                kept = destination / ".outcache-0123456789.tar.gz.tmp"
                kept.write_bytes(b"retained partial")
                link = destination / ".outcache-aaaaaaaaaa.tar.gz"
                link.symlink_to(job / "out/result.bin")
                with artifact_io.export_lock(jobs._read_root, JID):
                    code, _, raw = request(path, write_headers, method="POST", body=b"{}")
                    self.assertEqual((code, json.loads(raw)["code"]), (409, "export_busy"))
                real_unlink = os.unlink
                def deny_metadata(name, *args, **kwargs):
                    if name == metadata.name:
                        raise PermissionError("fixture metadata removal denied")
                    return real_unlink(name, *args, **kwargs)
                with patch.object(job_export_cache.os, "unlink", side_effect=deny_metadata):
                    code, _, raw = request(path, write_headers, method="POST", body=b"{}")
                value = json.loads(raw)
                self.assertEqual((code, value["deleted"], value["count"], value["bytes"], value["complete"]),
                                 (200, {"count": 1, "bytes": len(expected)}, 1, metadata.stat().st_size, False))
                self.assertEqual({error["operation"] for error in value["errors"]}, {"inspect", "unlink"})
                self.assertTrue(link.is_symlink())
                self.assertEqual(opened.read(), expected, "unlink must allow the original download FD to finish")
                link.unlink()  # Remove only the intentionally invalid fixture entry before the successful CLI pass.
                process = subprocess.run([sys.executable, str(NODE / "pf-node"), "--url", url,
                    "--node-id", identity["node_id"], "export-cache", JID, "--clear"],
                    env={"PATH": os.defpath, "HOME": str(root), "PF_NODE_TOKEN": "export-cache-fixture-admin-not-production",
                         "NO_PROXY": "*", "no_proxy": "*", "PYTHONDONTWRITEBYTECODE": "1"},
                    cwd=root, capture_output=True, text=True, timeout=10)
                self.assertEqual(process.returncode, 0, process.stderr + process.stdout)
                cleared = json.loads(process.stdout)
                self.assertEqual((cleared["deleted"]["count"], cleared["count"], cleared["complete"]), (1, 0, True))
                code, _, raw = request(path, write_headers, method="POST", body=b"{}")
                self.assertEqual((code, json.loads(raw)["deleted"]), (200, {"count": 0, "bytes": 0}))
                self.assertEqual(kept.read_bytes(), b"retained partial")
                self.assertEqual({path: path.read_bytes() for path in originals}, originals)
                self.assertTrue((job / ".out-export.lock").is_file())
                self.assertEqual(request(f"/jobs/{JID}/out?meta=1")[0], 200)
                self.assertEqual(json.loads(request(path)[2])["count"], 2)

    def test_external_cache_isolates_sources_reopens_and_never_falls_back(self):
        with tempfile.TemporaryDirectory(prefix="export-cache-") as temporary, ExitStack() as cleanup:
            root = Path(temporary).resolve()
            cache_root = root / "external cache"

            def jobs_at(name, cache=None):
                jobs = Jobs(str(root / name), {}, export_cache_root=str(cache) if cache else None)
                cleanup.callback(jobs._read_root.close)
                cleanup.callback(jobs.export_cache.close)
                return jobs

            def seed(jobs, content):
                job = Path(jobs.path(JID))
                (job / "out" / "nested").mkdir(parents=True)
                (job / "out" / "nested" / "result.bin").write_bytes(content)
                (job / "log").write_bytes(b"synthetic source log\n")
                jobs._write_meta(JID, {"id": JID, "bin": "offline-fixture", "started": 1, "exit": 0})
                return job

            def archive(jobs):
                opened, info = jobs.out_blob(JID)
                with opened:
                    data = opened.read()
                return data, info

            def payload(data):
                with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as source:
                    return source.extractfile("./nested/result.bin").read()

            def files(paths):
                return {str(path): (path.stat().st_ino, path.stat().st_mtime_ns, path.read_bytes()) for path in paths}

            def legacy_files(job):
                return files(job.glob(".outcache-*"))

            def cli(url, node_id, destination, *args):
                return subprocess.run([sys.executable, str(NODE / "pf-node"), "--url", url,
                    "--node-id", node_id, "--out-dir", str(destination), "out", JID, *args],
                    env={"PATH": os.defpath, "HOME": str(root), "PF_NODE_TOKEN": TOKEN,
                         "NO_PROXY": "*", "no_proxy": "*", "PYTHONDONTWRITEBYTECODE": "1"},
                    cwd=root, capture_output=True, text=True, timeout=10)

            legacy = jobs_at("source A")
            job_a = seed(legacy, b"source A original bytes")
            old_bytes, _ = archive(legacy)
            old_cache = legacy_files(job_a)
            self.assertTrue(old_cache)
            lock_inode = (job_a / ".out-export.lock").stat().st_ino
            a = jobs_at("source A", cache_root)
            b = jobs_at("source B", cache_root)
            job_b = seed(b, b"source B different original bytes")
            original_paths = [job / name for job in (job_a, job_b)
                              for name in ("meta.json", "log", "out/nested/result.bin")]
            originals = files(original_paths)
            bytes_a, info_a = archive(a)
            bytes_b, _ = archive(b)
            self.assertEqual(payload(bytes_a), b"source A original bytes")
            self.assertEqual(payload(bytes_b), b"source B different original bytes")
            self.assertEqual(payload(old_bytes), payload(bytes_a))
            store_a, prefix_a, binding_a = a.export_cache.target(JID)
            _, prefix_b, binding_b = b.export_cache.target(JID)
            self.assertNotEqual(prefix_a, prefix_b)
            self.assertNotEqual(binding_a["inode"], binding_b["inode"])
            self.assertEqual(binding_a["job"], JID)
            destination = cache_root / prefix_a
            external_cache = files(destination.glob(".outcache-*"))
            self.assertEqual(len(list(cache_root.rglob("*.tar.gz"))), 2)

            # New service readers use the same pinned source namespace and cache.
            reopened = jobs_at("source A", cache_root)
            with endpoint(reopened) as (url, get):
                status, _, raw = get("/node")
                self.assertEqual(status, 200, raw)
                identity = json.loads(raw)
                self.assertIn("jobs.export-cache-root-v1", identity["capabilities"])
                self.assertEqual(identity["storage"]["export_cache"], reopened.export_cache.public())
                self.assertEqual(identity["storage"]["export_cache"], {
                    "scope": "directory_export", "mode": "external", "configured_root": str(cache_root),
                    "effective_root": str(destination.parent), "availability": "available",
                    "reason": None, "legacy_entries_migrated": False})
                with patch.object(node_service, "archive_directory", side_effect=AssertionError("reopened cache rebuilt")):
                    status, _, raw = get(f"/jobs/{JID}/out?meta=1")
                    self.assertEqual((status, json.loads(raw)), (200, info_a))
                    status, headers, raw = get(f"/jobs/{JID}/out", {"Range": "bytes=1-8"})
                    self.assertEqual((status, raw), (206, bytes_a[1:9]))
                    self.assertEqual(headers["Content-Range"], f"bytes 1-8/{len(bytes_a)}")
                    downloaded = root / "download directory"
                    process = cli(url, identity["node_id"], downloaded)
                    self.assertEqual(process.returncode, 0, process.stderr)
                    self.assertEqual((downloaded / "nested" / "result.bin").read_bytes(), payload(bytes_a))
                # Old-layout and external readers must share the original stable lock.
                with artifact_io.export_lock(legacy._read_root, JID):
                    status, _, raw = get(f"/jobs/{JID}/out?meta=1")
                    self.assertEqual((status, json.loads(raw)), (200, info_a))
                    status, _, raw = get(f"/jobs/{JID}/out?path=nested&meta=1")
                    self.assertEqual((status, json.loads(raw)["code"]), (409, "export_busy"))
                self.assertEqual((job_a / ".out-export.lock").stat().st_ino, lock_inode)

                # Only the destination directory reports low space; a mistaken
                # source-volume check would allow the export and fail this test.
                expected = destination.stat()
                observed = []
                def space(fd):
                    value = os.fstat(fd)
                    key = (value.st_dev, value.st_ino)
                    observed.append((key, stat.S_ISDIR(value.st_mode)))
                    return SimpleNamespace(f_bavail=0 if key == (expected.st_dev, expected.st_ino) else 1 << 30,
                                           f_frsize=4096)
                with patch.object(artifact_io.os, "fstatvfs", space):
                    status, _, raw = get(f"/jobs/{JID}/out?path=nested&meta=1")
                self.assertEqual((status, json.loads(raw)["code"]), (507, "export_space_insufficient"))
                self.assertTrue(observed)
                self.assertTrue(all(key == (expected.st_dev, expected.st_ino) and is_directory
                                    for key, is_directory in observed))
                self.assertEqual(files(destination.glob(".outcache-*")), external_cache)
                self.assertEqual(list(destination.glob("*.tmp")), [])

            # An unavailable configured root is retained as an error, including
            # when this job has a usable old legacy cache. Files remain readable.
            unavailable = root / "not a directory"
            unavailable.write_bytes(b"explicit cache destination is unavailable")
            broken = jobs_at("source A", unavailable)
            with endpoint(broken) as (url, get):
                status, _, raw = get("/node")
                self.assertEqual(status, 200, raw)
                identity = json.loads(raw)
                state = identity["storage"]["export_cache"]
                self.assertEqual((state["mode"], state["availability"], state["configured_root"]),
                                 ("external", "unavailable", str(unavailable)))
                self.assertFalse(state["legacy_entries_migrated"])
                status, _, raw = get(f"/jobs/{JID}/out?meta=1")
                self.assertEqual((status, json.loads(raw)["code"]), (503, "export_storage_unavailable"))
                status, _, raw = get(f"/jobs/{JID}/out?path=nested/result.bin")
                self.assertEqual((status, raw), (200, b"source A original bytes"))
                downloaded = root / "download file"
                process = cli(url, identity["node_id"], downloaded, "nested/result.bin")
                self.assertEqual(process.returncode, 0, process.stderr)
                self.assertEqual((downloaded / "nested" / "result.bin").read_bytes(), payload(bytes_a))
            self.assertEqual(files(original_paths), originals)
            self.assertEqual(legacy_files(job_a), old_cache)
            self.assertEqual(legacy_files(job_b), {})
            self.assertEqual(files(destination.glob(".outcache-*")), external_cache)


if __name__ == "__main__":
    unittest.main()
