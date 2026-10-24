import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from game_assets import AssetCache, AssetUploadError, UPLOAD_READ, run_manifest


def asset(data, name="fixture"):
    return {"name": name, "role": "resource", "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data), "source": {"kind": "url", "url": "https://example.invalid/resource"}}


def manifest(assets):
    return {"schema_version": 1, "game_id": "fixture", "version": "v1", "title": "Fixture", "assets": assets}


class GameUploadCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cache = AssetCache(self.root / "cache")
        (self.root / "jobs").mkdir()
        (self.root / "out").mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_interrupted_prefix_offset_and_cancel_resume(self):
        payload = bytes(range(256)) * 2048
        item = asset(payload)
        self.assertEqual(self.cache.upload_status(item, "first")["state"], "unclaimed")
        self.assertFalse(Path(self.cache.root).exists())
        self.assertEqual(self.cache.upload_open(item, "first")["offset"], 0)
        owner = self

        class Interrupted:
            calls = 0

            def read(self, size):
                owner.assertLessEqual(size, UPLOAD_READ)
                self.calls += 1
                if self.calls == 1:
                    return payload[:100_000]
                status = owner.cache.upload_status(item, "first")
                owner.assertEqual((status["state"], status["offset"]), ("busy", None))
                raise TimeoutError("fixture transfer interrupted")

        with self.assertRaises(AssetUploadError) as failure:
            self.cache.upload_chunk(item, "first", 0, len(payload), Interrupted())
        self.assertEqual(failure.exception.code, "upload_interrupted")
        # A fresh cache object sees the flushed prefix even though the request
        # never acknowledged it. Querying never hashes the prefix.
        self.cache = AssetCache(self.root / "cache")
        with patch.object(self.cache, "_digest", side_effect=AssertionError("query hashed content")):
            self.assertEqual(self.cache.upload_status(item, "first")["offset"], 100_000)
            with self.assertRaises(AssetUploadError) as failure:
                self.cache.upload_chunk(item, "first", 0, 1, io.BytesIO(b"x"))
            self.assertEqual((failure.exception.status, failure.exception.code), (409, "upload_offset_mismatch"))
            self.assertEqual(self.cache.upload_cancel(item, "first")["offset"], 100_000)
            self.assertEqual(self.cache.upload_open(item, "second")["offset"], 100_000)
            self.assertEqual(self.cache.upload_status(item, "first")["state"], "superseded")
            result = self.cache.upload_chunk(item, "second", 100_000, len(payload) - 100_000,
                                             io.BytesIO(payload[100_000:]))
            self.assertEqual((result["state"], result["offset"]), ("open", len(payload)))
        self.assertEqual((Path(self.cache.root) / "partial" / (item["sha256"] + ".part")).read_bytes(), payload)

    def test_seal_excludes_writers_and_scoped_verification_preserves_failures(self):
        payload = b"uploaded bytes\x00" * 1024
        item = asset(payload)
        absent = asset(b"unavailable", "other")
        absent["source"] = {"kind": "unavailable", "reason": "fixture has no source"}
        self.cache.upload_open(item, "valid")
        self.cache.upload_chunk(item, "valid", 0, len(payload), io.BytesIO(payload))
        self.assertEqual(self.cache.upload_seal(item, "valid")["state"], "sealed")
        with self.cache._storage() as store, self.cache._lock(store, item["sha256"]):
            status = self.cache.upload_status(item, "valid")
            self.assertEqual((status["state"], status["offset"], status["busy"]), ("sealed", len(payload), True))
            with self.assertRaises(AssetUploadError) as failure:
                self.cache.upload_cancel(item, "valid")
            self.assertEqual(failure.exception.code, "upload_busy")
        with self.assertRaises(AssetUploadError):
            self.cache.upload_chunk(item, "valid", len(payload), 1, io.BytesIO(b"x"))
        with patch.object(AssetCache, "_download", side_effect=AssertionError("upload fell back to URL")):
            self.assertEqual(run_manifest(manifest([item, absent]), self.cache.root,
                                          self.root / "jobs", self.root / "out"), 1)
            report = json.loads((self.root / "out" / "preparation.json").read_text())
            self.assertEqual([row["state"] for row in report["assets"]], ["failed", "unavailable"])
            self.assertEqual(run_manifest(manifest([absent, item]), self.cache.root,
                                          self.root / "jobs", self.root / "out", "valid", 1), 0)
            report = json.loads((self.root / "out" / "preparation.json").read_text())
            self.assertEqual(report["scope"], {"kind": "uploaded_asset", "asset_index": 1,
                                              "asset_name": "fixture", "upload_id": "valid"})
            self.assertEqual(len(report["assets"]), 1)
            self.assertEqual(Path(self.cache.object_path(item["sha256"])).read_bytes(), payload)
            with patch.object(self.cache, "_digest", side_effect=AssertionError("cache hit rehashed")):
                self.assertTrue(self.cache.verify_upload(item, "valid")["cache_hit"])
                self.assertTrue(self.cache.upload_open(item, "cached")["cache_hit"])

            bad = asset(b"expected bytes", "bad")
            wrong = b"incorrect data"
            self.assertEqual(len(wrong), bad["size"])
            self.cache.upload_open(bad, "old")
            self.cache.upload_chunk(bad, "old", 0, len(wrong), io.BytesIO(wrong))
            self.cache.upload_seal(bad, "old")
            self.cache.upload_cancel(bad, "old")
            self.cache.upload_open(bad, "replacement")
            self.cache.upload_seal(bad, "replacement")
            with self.assertRaises(AssetUploadError) as failure:
                self.cache.verify_upload(bad, "old")
            self.assertEqual(failure.exception.code, "upload_not_owner")
            result = self.cache.verify_upload(bad, "replacement")
            self.assertEqual(result["state"], "failed")
            self.assertEqual(self.cache.upload_status(bad, "replacement")["state"], "failed")
            rejected = Path(self.cache.root) / result["quarantine"][0]["path"]
            self.assertEqual(rejected.read_bytes(), wrong)
            self.assertTrue(Path(str(rejected) + ".json").is_file())
            self.assertFalse(Path(self.cache.object_path(bad["sha256"])).exists())


if __name__ == "__main__":
    unittest.main()
