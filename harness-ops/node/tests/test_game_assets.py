import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import game_assets
from game_assets import AssetCache, AssetError, run_manifest, validate_manifest
import fetch_apk


PAYLOAD = bytes(range(256)) * 1024


def asset(url, data=PAYLOAD, name="fixture"):
    return {"name": name, "role": "resource", "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data), "source": {"kind": "url", "url": url}}


def manifest(assets):
    return {"schema_version": 1, "game_id": "fixture", "version": "v1", "title": "Fixture", "assets": assets}


class GameAssetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cache = AssetCache(self.root / "cache")
        self.calls = []
        self.mode = "truncate"
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                offset = int(self.headers.get("Range", "bytes=0-").split("=")[1].rstrip("-"))
                owner.calls.append((self.path, offset))
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/payload?signature=fixture-private-signature")
                    self.end_headers()
                    return
                if owner.mode == "bad":
                    data = b"x" * len(PAYLOAD)
                else:
                    data = PAYLOAD
                if owner.mode in {"oversize-header", "oversize-stream"}:
                    data += b"!"
                ranged = offset > 0 and owner.mode not in {"ignore", "oversize-header"}
                if not ranged:
                    offset = 0
                self.send_response(206 if ranged else 200)
                if owner.mode != "oversize-stream":
                    self.send_header("Content-Length", str(len(data) - offset))
                if ranged:
                    total = len(PAYLOAD) if owner.mode == "oversize-stream" else len(data)
                    self.send_header("Content-Range", f"bytes {offset}-{total - 1}/{total}")
                self.end_headers()
                if owner.mode == "truncate":
                    self.wfile.write(data[offset:offset + 8192])
                    self.wfile.flush()
                    self.connection.shutdown(socket.SHUT_RDWR)
                else:
                    try:
                        self.wfile.write(data[offset:])
                    except (BrokenPipeError, ConnectionResetError):
                        pass  # A header rejection can close before this bounded fixture body.

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)
        (self.root / "jobs").mkdir()
        (self.root / "out").mkdir()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def test_interrupted_download_resumes_and_cached_reads_never_rehash(self):
        item = asset(self.url + "/redirect")
        self.assertEqual(self.cache.observe(item)["state"], "missing")
        self.assertFalse(Path(self.cache.root).exists())
        self.assertEqual(run_manifest(manifest([item]), self.cache.root, self.root / "jobs", self.root / "out"), 1)
        partial = Path(self.cache.root) / "partial" / (item["sha256"] + ".part")
        self.assertEqual(partial.stat().st_size, 8192)
        self.assertNotIn("fixture-private-signature", (self.root / "out" / "preparation.json").read_text())
        self.mode = "normal"
        result = self.cache.prepare(item, self.root / "jobs")
        self.assertEqual((result["state"], result["resumed_bytes"]), ("verified", 8192))
        self.assertEqual(Path(self.cache.object_path(item["sha256"])).read_bytes(), PAYLOAD)
        calls = list(self.calls)
        with patch.object(self.cache, "_digest", side_effect=AssertionError("cache hit rehashed")):
            self.assertEqual(self.cache.observe(item)["state"], "verified")
            self.assertTrue(self.cache.prepare(item, self.root / "jobs")["cache_hit"])
        self.assertEqual(self.calls, calls)

    def test_ignored_range_restarts_and_bad_content_is_quarantined(self):
        item = asset(self.url + "/payload")
        with self.assertRaises(AssetError):
            self.cache.prepare(item, self.root / "jobs")
        self.mode = "ignore"
        result = self.cache.prepare(item, self.root / "jobs")
        self.assertEqual((result["state"], result["resumed_bytes"]), ("verified", 0))
        blob = Path(self.cache.object_path(item["sha256"]))
        os.chmod(blob, 0o600)
        blob.write_bytes(b"x" * len(PAYLOAD))
        self.assertEqual(self.cache.observe(item)["state"], "unknown")
        self.mode = "bad"
        result = self.cache.prepare(item, self.root / "jobs")
        self.assertEqual(result["state"], "failed")
        self.assertEqual(len(result["quarantine"]), 2)
        self.assertFalse(blob.exists())
        for rejected in result["quarantine"]:
            path = Path(self.cache.root) / rejected["path"]
            self.assertEqual(path.read_bytes(), b"x" * len(PAYLOAD))
            self.assertTrue(Path(str(path) + ".json").is_file())

    def test_write_limits_reject_http_and_import_growth_without_damaging_cache(self):
        # A legal short-write import establishes a pre-existing verified object.
        stable_bytes = b"small stable imported resource"
        stable_source = self.root / "stable.bin"
        stable_source.write_bytes(stable_bytes)
        stable = asset(self.url + "/unused", stable_bytes, "stable")
        real_write, accepted = os.write, []
        def short_write(fd, block):
            count = real_write(fd, block[:3])
            accepted.append(count)
            return count
        with stable_source.open("rb") as source, patch.object(game_assets.os, "write", short_write):
            result = self.cache.import_file(stable, source)
        self.assertEqual(result["state"], "verified")
        self.assertEqual(sum(accepted), len(stable_bytes))
        self.assertGreater(len(accepted), 1)
        stable_blob = Path(self.cache.object_path(stable["sha256"]))
        stable_identity = (stable_blob.stat().st_ino, stable_blob.read_bytes())

        item = asset(self.url + "/payload")
        with self.assertRaises(AssetError):
            self.cache.prepare(item, self.root / "jobs")
        partial = Path(self.cache.root) / "partial" / (item["sha256"] + ".part")
        prefix = partial.read_bytes()
        self.assertEqual(prefix, PAYLOAD[:8192])
        for mode in ("oversize-header", "oversize-stream"):
            with self.subTest(mode=mode):
                self.mode = mode
                self.assertEqual(run_manifest(manifest([item]), self.cache.root, self.root / "jobs", self.root / "out"), 1)
                report = json.loads((self.root / "out" / "preparation.json").read_text())
                failure = report["assets"][0]
                self.assertEqual(failure["state"], "failed")
                self.assertEqual(failure["write_error"]["code"], "asset_size_exceeded")
                self.assertEqual(report["write_limits"], game_assets.ASSET_WRITE_LIMITS)
                self.assertEqual(failure["write_error"]["details"]["prefix_bytes"], partial.stat().st_size)
                if mode == "oversize-header":
                    self.assertEqual(partial.read_bytes(), prefix, "200 length rejection must precede Range fallback truncate")
                    self.assertEqual(failure["write_error"]["details"]["written_bytes"], 0)
                else:
                    self.assertLessEqual(partial.stat().st_size, item["size"])
                    self.assertEqual(partial.read_bytes(), PAYLOAD[:partial.stat().st_size])
                self.assertFalse(Path(self.cache.object_path(item["sha256"])).exists())
                self.assertNotEqual(self.cache.observe(item)["state"], "verified")

        jid = "c" * 12
        job = self.root / "jobs" / jid
        (job / "out").mkdir(parents=True)
        (job / "meta.json").write_text(json.dumps({"id": jid, "state": "finished"}))
        growing = job / "out" / "growing.bin"
        original = b"growth fixture"
        growing.write_bytes(original)
        imported = asset(self.url + "/unused", original, "growing")
        imported["source"] = {"kind": "artifact", "job_id": jid, "path": "growing.bin"}
        owner, read_sizes = self, []
        class GrowingReader:
            def __init__(self, stream):
                self.stream, self.grown = stream, False
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.stream.close()
            def fileno(self):
                return self.stream.fileno()
            def seek(self, *args):
                return self.stream.seek(*args)
            def read(self, size):
                read_sizes.append(size)
                owner.assertLessEqual(size, 4)
                data = self.stream.read(size)
                if data and not self.grown:
                    self.grown = True
                    with growing.open("ab") as writer:
                        writer.write(b"!")
                return data
        open_file = game_assets.SafeRoot.open_file
        def opened(store, rel):
            stream = open_file(store, rel)
            return GrowingReader(stream) if rel == jid + "/out/growing.bin" else stream
        with patch.object(game_assets, "CHUNK", 4), patch.object(game_assets.SafeRoot, "open_file", opened):
            self.assertEqual(run_manifest(manifest([imported]), self.cache.root, self.root / "jobs", self.root / "out"), 1)
        report = json.loads((self.root / "out" / "preparation.json").read_text())
        failure = report["assets"][0]
        self.assertEqual(failure["write_error"]["code"], "asset_size_exceeded")
        imported_partial = Path(self.cache.root) / "partial" / (imported["sha256"] + ".part")
        self.assertEqual(imported_partial.read_bytes(), original[:12])
        self.assertEqual(failure["write_error"]["details"]["written_bytes"], 12)
        self.assertEqual(read_sizes[-1], len(original) - 12 + 1)
        self.assertEqual(growing.read_bytes(), original + b"!")
        self.assertFalse(Path(self.cache.object_path(imported["sha256"])).exists())
        self.assertEqual(stable_source.read_bytes(), stable_bytes)
        self.assertEqual((stable_blob.stat().st_ino, stable_blob.read_bytes()), stable_identity)
        with patch.object(self.cache, "_digest", side_effect=AssertionError("unchanged cache rehashed")):
            self.assertTrue(self.cache.prepare(stable, self.root / "jobs")["cache_hit"])

    def test_finished_artifact_import_continues_other_assets_and_legacy_is_explicit(self):
        jid = "b" * 12
        source = self.root / "jobs" / jid
        (source / "out").mkdir(parents=True)
        (source / "meta.json").write_text(json.dumps({"id": jid, "state": "running"}))
        original = source / "out" / "fixture.apk"
        original.write_bytes(PAYLOAD)
        imported = asset(self.url + "/unused")
        imported["source"] = {"kind": "artifact", "job_id": jid, "path": "fixture.apk"}
        with self.assertRaises(AssetError):
            self.cache.prepare(imported, self.root / "jobs")
        (source / "meta.json").write_text(json.dumps({"id": jid, "state": "finished"}))
        missing = asset(self.url + "/unused", b"missing", "missing")
        missing["source"] = {"kind": "unavailable", "reason": "historical source is unavailable"}
        self.assertEqual(run_manifest(manifest([missing, imported]), self.cache.root,
                                      self.root / "jobs", self.root / "out"), 1)
        report = json.loads((self.root / "out" / "preparation.json").read_text())
        self.assertEqual([entry["state"] for entry in report["assets"]], ["unavailable", "verified"])
        self.assertEqual(self.cache.observe(missing)["state"], "unavailable")
        self.assertEqual(self.calls, [])
        self.assertEqual(original.read_bytes(), PAYLOAD)
        with self.assertRaises(AssetError):
            validate_manifest(manifest([{**imported, "target": "../escape"}]))
        with patch.dict(os.environ, {"PF_APK_ADB_BIN": "fixture-adb"}), patch(
                "fetch_apk.subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as adb:
            args = ["--cache-root", self.cache.root, "fixture.apk", str(len(PAYLOAD)), "", imported["sha256"]]
            self.assertEqual(fetch_apk.main(args), 2)
            adb.assert_not_called()
            args[-2] = "explicit-fixture-serial"
            self.assertEqual(fetch_apk.main(args), 0)
            self.assertEqual(adb.call_args.args[0], ["fixture-adb", "-s", "explicit-fixture-serial", "install", "-r",
                                                   self.cache.object_path(imported["sha256"])])


if __name__ == "__main__":
    unittest.main()
