"""A/B metadata binds evidence; synthetic media headers do not prove human viewing."""
import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import unittest
from unittest.mock import patch
import zlib

NODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NODE))
from node_service import readonly_token
from node_media import MediaError
import test_node_games as game_fixture


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def png():
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00\x00\xff\x00" * 2)) + chunk(b"IEND", b""))


def media_headers():
    # JPEG/video fixtures deliberately only exercise bounded server identification,
    # not a browser decoder, playable tracks, or scientific image quality.
    jpeg = b"\xff\xd8\xff\xc0" + struct.pack(">HBHHB", 17, 8, 2, 3, 3) + b"\x01\x11\x00\x02\x11\x00\x03\x11\x00\xff\xd9"
    mp4 = struct.pack(">I4s4sI4s4s", 24, b"ftyp", b"isom", 0, b"isom", b"mp42")
    webm = b"\x1a\x45\xdf\xa3\x87\x42\x82\x84webm"
    return {"png": png(), "jpeg": jpeg, "mp4": mp4, "webm": webm}


class NodeEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.env = game_fixture.GameFlowTests()
        self.env.setUp()
        self.addCleanup(self.env.tearDown)
        self.pin = self.env.jobs.games.node_id
        self.ro = readonly_token(self.env.admin)
        self.jid = "eeeeeeeeeeee"
        self.out = Path(self.env.jobs.path(self.jid, "out"))
        self.out.mkdir(parents=True)
        self.env.jobs._write_meta(self.jid, {"id": self.jid, "bin": "custom", "argv": ["legacy-fixture"],
            "started": 1, "exit": 7, "lost": True})

    def request(self, method, path, value=None, *, token=None, headers=None, pin=True):
        connection = http.client.HTTPConnection(*self.env.server.server_address, timeout=5)
        values = {"Authorization": "Bearer " + (self.env.admin if token is None else token),
                  "Content-Type": "application/json"}
        if pin:
            values["X-Expected-Node-ID"] = self.pin
        values.update(headers or {})
        try:
            connection.request(method, path, body=None if value is None else json.dumps(value), headers=values)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def api(self, method, path, value=None, **kwargs):
        code, _, raw = self.request(method, path, value, **kwargs)
        return code, json.loads(raw)

    @staticmethod
    def envelope(operation, data, revision=None, reason=None):
        return {"schema": "phonefarm/research-write@1", "operation_id": operation,
                "base_revision": revision, "data": data, "actor_label": "fixture-agent", "reason": reason}

    def write(self, kind, oid, data, *, revision=None, operation=None):
        return self.api("POST", f"/research-state/{kind}/{oid}", self.envelope(
            operation or "create-" + oid, data, revision, "Explicit fixture update" if revision else None))

    def create(self, kind, oid, data):
        code, result = self.write(kind, oid, data)
        self.assertEqual(code, 201, result)
        return result

    def read(self, kind, oid, revision=None):
        suffix = "" if revision is None else "?revision=" + str(revision)
        code, value = self.api("GET", f"/research-state/{kind}/{oid}" + suffix, token=self.ro)
        self.assertEqual(code, 200, value)
        return value

    def register(self, rid, sources):
        code, value = self.api("POST", "/results", {"schema": "phonefarm/result@1", "result_id": rid,
            "title": "Synthetic media; no viewing or quality claim", "origin": {"evidence_origin": "synthetic"},
            "sources": sources, "limitations": ["Container/header fixtures only; not browser playback evidence"]})
        self.assertEqual(code, 201, value)
        return value

    def files(self):
        blobs = media_headers()
        for name, raw in blobs.items():
            (self.out / name).write_bytes(raw)
        result = self.register("media", [{"name": name, "kind": "job_output", "job_id": self.jid, "path": name}
                                          for name in blobs])
        self.assertTrue(all(source["status"] == "available" for source in result["source_observations"]))
        return blobs

    def ref(self, name, rid="media"):
        return {"kind": "result_source", "node_id": self.pin, "result_id": rid, "source_name": name}

    def question(self, refs, *, kind="image", slots=None):
        return {"research_id": "study", "type": "evaluation", "title": "Compare A and B",
                "need": "State any visible difference or inability to judge", "why": "Synthetic protocol check",
                "context": {"research_revision": 1, "directions": []}, "evidence_refs": refs,
                "evaluation": {"schema": "phonefarm/ab-evaluation@1", "media_kind": kind,
                               "slots": {"A": 1, "B": 0} if slots is None else slots}}

    def seed_legacy(self, kind, oid, data, bindings, observations):
        """Install a frozen pre-extension record, without calling current normalize."""
        operation = "legacy-" + oid
        envelope = self.envelope(operation, data)
        request = canonical({"kind": kind, "id": oid, "base_revision": None, "data": data,
                             "actor_label": "fixture-agent", "reason": None})
        with self.env.jobs.research._db(True) as db:
            sequence = db.execute("INSERT INTO events(research_id,kind,object_id,revision,operation_id,at) VALUES (?,?,?,?,?,?)",
                                  ("study", kind, oid, 1, operation, 1.0)).lastrowid
            db.execute("INSERT INTO revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (sequence, kind, oid, 1, "study",
                data.get("title"), data.get("state"), data.get("request_id"), canonical(data),
                canonical({"actor_label": "fixture-agent", "auth_channel": "admin_token", "reason": None, "at": 1.0}),
                canonical(bindings), canonical(observations), 1.0))
            db.execute("INSERT INTO objects VALUES (?,?,?,?,?,?)", (kind, oid, "study", 1, 1.0, 1.0))
            receipt = {"schema_version": 1, "node_id": self.pin, "operation_id": operation,
                       "object": {"kind": kind, "id": oid, "revision": 1}, "event_seq": sequence, "committed_at": 1.0}
            db.execute("INSERT INTO operations VALUES (?,?,?)", (operation, request, canonical(receipt)))
        return envelope, receipt, request

    def cli(self, *args):
        env = {**os.environ, "PF_NODE_URL": self.env.url, "PF_NODE_ID": self.pin,
               "PF_NODE_TOKEN": self.ro, "HOME": str(self.env.root)}
        env.pop("PF_NODE_TOKEN_FILE", None)
        return subprocess.run([sys.executable, str(NODE / "pf-node"), "research", *args], env=env,
                              capture_output=True, text=True, timeout=10)

    def test_legacy_operations_fixed_slots_and_late_feedback_preserve_dependency(self):
        self.create("research", "study", {"title": "Synthetic study", "objective": "Keep claims scoped"})
        external = self.register("legacy-source", [{"name": "unknown", "kind": "external_reference",
                                                    "locator": "fixture://not-inspected"}])
        reference = self.ref("unknown", "legacy-source")
        old_data = {"research_id": "study", "type": "evaluation", "title": "Old text question",
                    "need": "Text only", "why": "No media viewed", "context": {"research_revision": 1, "directions": []},
                    "evidence_refs": [reference], "state": "open", "resume_hint": None, "close_reason": None,
                    "due_at": None, "supersedes": None}
        binding = {"reference": reference, "identity_kind": "unverified", "result_sequence": external["sequence"],
                   "result_registered_at": external["registered_at"], "source_observation": external["source_observations"][0]}
        old_request = self.seed_legacy("request", "old", old_data, [binding], {})
        old_feedback_data = {"request_id": "old", "request_revision": 1, "text": "Legacy text only",
                             "evidence_refs": [], "supersedes": None}
        old_feedback = self.seed_legacy("feedback", "old-reply", old_feedback_data, [], {
            "request_state_at_submit": "open", "request_revision_at_submit": 1,
            "context_changed_at_submit": {"research": False, "directions": []}})
        for kind, oid, (envelope, receipt, request) in (("request", "old", old_request),
                                                       ("feedback", "old-reply", old_feedback)):
            self.assertEqual(self.api("POST", f"/research-state/{kind}/{oid}", envelope), (201, receipt))
            self.assertNotIn("evaluation", self.read(kind, oid)["data"])
            with self.env.jobs.research._db() as db:
                self.assertEqual(db.execute("SELECT request FROM operations WHERE id=?",
                                           (envelope["operation_id"],)).fetchone()[0], request)
        code, value = self.write("request", "old", {**old_data, "state": "closed", "close_reason": "Text handled"},
                                 revision=1, operation="old-close")
        self.assertEqual(code, 201, value)
        self.assertNotIn("evaluation", self.read("request", "old")["data"])
        self.assertEqual(self.api("POST", "/research-state/request/old", old_request[0]), (201, old_request[1]))

        self.files()
        refs = [self.ref("png"), self.ref("jpeg")]
        ab = self.question(refs)
        with patch.object(self.env.jobs.results, "source", side_effect=AssertionError("metadata must not load media")):
            created = self.create("request", "ab", ab)
            record = self.read("request", "ab")
        derived = record["derived"]["evaluation"]
        self.assertEqual(derived["slots"], {"A": 1, "B": 0})
        self.assertEqual(derived["blinding"], "presentation_only")
        self.assertEqual(derived["limits"], {"max_bytes": 16 << 20, "max_dimension": 8192, "max_pixels": 16777216})
        self.assertEqual(derived["media"]["A"], f"/research-state/request/ab/media/A?revision=1&expected_node_id={self.pin}")
        code, immutable = self.write("request", "ab", {**record["data"], "evaluation": {**ab["evaluation"],
                                    "slots": {"A": 0, "B": 1}}}, revision=1, operation="swap-denied")
        self.assertEqual((code, immutable["code"]), (409, "research_immutable"))
        self.assertEqual(self.api("POST", "/research-state/request/ab", self.envelope("create-ab", ab)), (201, created))
        self.create("direction", "a", {"research_id": "study", "title": "A", "hypothesis": "Needs evaluation",
            "state": "waiting_human", "state_reason": "Explicit dependency", "dependencies": [{
                "id": "needs-ab", "kind": "request", "target_id": "ab", "target_revision": 1,
                "blocking": True, "reason": "Inspect these exact sources", "resolution": None}]})
        self.create("direction", "b", {"research_id": "study", "title": "B", "hypothesis": "Independent", "state": "ready"})
        code, closed = self.write("request", "ab", {**record["data"], "state": "closed", "close_reason": "Window closed"},
                                  revision=1, operation="close-ab")
        self.assertEqual(code, 201, closed)
        study = self.read("research", "study")
        self.assertEqual(self.write("research", "study", {**study["data"], "objective": "Revised context"},
                                    revision=1, operation="change-context")[0], 201)
        self.create("request", "new-ab", {**ab, "supersedes": "ab", "context": {"research_revision": 2, "directions": []},
                    "evaluation": {**ab["evaluation"], "slots": {"A": 0, "B": 1}}})
        assessment = {"schema": "phonefarm/ab-feedback@1", "preference": None, "difference": None,
                      "reviewed": {"A": False, "B": None}, "client_display": {"A": "failed", "B": "not_loaded"},
                      "identity_revealed": None}
        feedback = {"request_id": "ab", "request_revision": 1, "text": "Cannot judge these synthetic headers",
                    "evaluation": assessment}
        invalid = {**feedback, "evaluation": {**assessment, "preference": "A", "difference": "small"}}
        self.assertEqual(self.write("feedback", "not-reviewed", invalid)[0], 400)
        code, not_ab = self.write("feedback", "not-ab", {**feedback, "request_id": "old"})
        self.assertEqual((code, not_ab["code"]), (409, "research_evaluation_required"))
        self.create("feedback", "late", feedback)
        late = self.read("feedback", "late")
        self.assertEqual(late["data"]["evaluation"], assessment, "unknown must not become watched, equal, or zero")
        self.assertEqual((late["derived"]["request_state_at_submit"], late["derived"]["request_revision_at_submit"]), ("closed", 2))
        self.assertTrue(late["derived"]["context_changed_at_submit"]["research"])
        expected = {"schema": "phonefarm/ab-evaluation@1", "media_kind": "image", "slots": {"A": 1, "B": 0},
                    "references": {"A": refs[1], "B": refs[0]},
                    "evidence_bindings": {"A": record["evidence_bindings"][1], "B": record["evidence_bindings"][0]}}
        self.assertEqual(late["derived"]["evaluation_binding"], expected)
        self.assertEqual(self.read("request", "ab", 1)["data"]["evaluation"], ab["evaluation"])
        self.assertEqual(self.read("request", "new-ab")["derived"]["feedback_count"], 0)
        self.assertTrue(self.read("direction", "a")["derived"]["metadata_blocked"])
        self.assertFalse(self.read("direction", "b")["derived"]["metadata_blocked"])
        self.assertEqual(self.env.jobs.dispatch.queue.rows(), [])

    def test_explicit_bound_media_range_limits_and_cli_never_overwrites(self):
        self.create("research", "study", {"title": "Media transport", "objective": "Check bytes, not viewing"})
        blobs = self.files()
        self.create("request", "images", self.question([self.ref("png"), self.ref("jpeg")], slots={"A": 0, "B": 1}))
        self.create("request", "videos", self.question([self.ref("mp4"), self.ref("webm")], kind="video", slots={"A": 0, "B": 1}))
        self.assertEqual(self.read("request", "videos")["derived"]["evaluation"]["limits"],
                         {"max_bytes": 128 << 20, "max_dimension": None, "max_pixels": None})
        media = "/research-state/request/images/media/A?revision=1"
        code, _, body = self.request("GET", media, token=self.ro, pin=False)
        self.assertEqual((code, json.loads(body)["code"]), (400, "target_required"))
        self.assertEqual(self.request("GET", media, token=self.ro, headers={"X-Expected-Node-ID": "f" * 32})[0], 409)
        self.assertEqual(self.request("GET", "/research-state/request/images/media/A", token=self.ro)[0], 400)
        for request_id, slot, name, mime in (("images", "A", "png", "image/png"), ("images", "B", "jpeg", "image/jpeg"),
                                            ("videos", "A", "mp4", "video/mp4"), ("videos", "B", "webm", "video/webm")):
            code, headers, body = self.request("GET", f"/research-state/request/{request_id}/media/{slot}?revision=1",
                                               token=self.ro, headers={"Range": "bytes=0-7"})
            self.assertEqual((code, body), (206, blobs[name][:8]))
            self.assertEqual(headers["Content-Type"], mime)
            self.assertTrue(headers["Content-Disposition"].startswith("inline"))
            self.assertEqual(headers["Content-Range"], f"bytes 0-7/{len(blobs[name])}")
        view = {"Authorization": "Basic " + base64.b64encode(b"viewer:test-view").decode()}
        code, headers, body = self.request("GET", "/dash/api" + media, headers=view)
        self.assertEqual((code, body, headers["Content-Type"]), (200, blobs["png"], "image/png"))
        code, headers, _ = self.request("GET", "/results/media/sources/png", token=self.ro)
        self.assertEqual(code, 200)
        self.assertTrue(headers["Content-Disposition"].startswith("attachment"), "ordinary source download retains its contract")

        target = self.env.root / "downloaded.png"
        downloaded = self.cli("media", "images", "A", "--revision", "1", "--output", str(target))
        self.assertEqual(downloaded.returncode, 0, downloaded.stderr)
        facts = json.loads(downloaded.stdout)
        self.assertEqual((facts["slot"], facts["mime"], facts["size"], facts["sha256"]),
                         ("A", "image/png", len(blobs["png"]), hashlib.sha256(blobs["png"]).hexdigest()))
        self.assertEqual(target.read_bytes(), blobs["png"])
        target.write_bytes(b"caller-owned-do-not-overwrite")
        refused = self.cli("media", "images", "A", "--revision", "1", "--output", str(target))
        self.assertEqual(refused.returncode, 2, refused.stderr)
        self.assertEqual(target.read_bytes(), b"caller-owned-do-not-overwrite")

        # Current source changes do not replace the bytes bound to an old question.
        (self.out / "png").write_bytes(b"replacement bytes")
        code, _, body = self.request("GET", media, token=self.ro)
        self.assertEqual((code, json.loads(body)["code"]), (409, "result_source_changed"))
        self.assertNotIn(b"replacement bytes", body)
        self.register("external", [{"name": "external", "kind": "external_reference", "locator": "fixture://not-bound"}])
        refs = [self.ref("external", "external"), self.ref("jpeg")]
        code, unbound = self.write("request", "unbound", self.question(refs))
        self.assertEqual((code, unbound["code"]), (409, "research_media_unbound"))
        legacy = self.question(refs)
        del legacy["evaluation"]
        self.create("request", "text-only", legacy)
        code, _, body = self.request("GET", "/research-state/request/text-only/media/A?revision=1", token=self.ro)
        self.assertEqual((code, json.loads(body)["code"]), (409, "research_evaluation_required"))

        bad = self.out / "not-media.png"
        bad.write_bytes(b"<html>This suffix must not grant an image MIME.</html>")
        self.register("unsupported", [{"name": "bad", "kind": "job_output", "job_id": self.jid, "path": bad.name}])
        self.create("request", "unsupported", self.question([self.ref("bad", "unsupported"), self.ref("jpeg")]))
        with patch("node_results.describe_file", side_effect=AssertionError("rejected media must not be hashed")) as describe:
            code, _, body = self.request("GET", "/research-state/request/unsupported/media/B?revision=1", token=self.ro)
            self.assertEqual((code, json.loads(body)["code"]), (415, "media_unsupported"))
            large = self.out / "large.png"
            with large.open("wb") as stream:
                stream.write(blobs["png"])
                stream.truncate((16 << 20) + 1)
            self.register("oversized", [{"name": "large", "kind": "job_output", "job_id": self.jid, "path": large.name}])
            code, rejected = self.write("request", "oversized", self.question([self.ref("large", "oversized"), self.ref("jpeg")]))
            self.assertEqual((code, rejected["code"]), (413, "media_too_large"))
            # The registered stamp is genuine. Directly exercise the same read
            # helper to prove its fstat limit precedes describe_file/hash too.
            with self.assertRaises(MediaError) as too_large:
                self.env.jobs.results.source("oversized", "large", media_kind="image")
            self.assertEqual((too_large.exception.status, too_large.exception.payload["code"]),
                             (413, "media_too_large"))
            describe.assert_not_called()
        self.assertEqual(self.env.jobs.dispatch.queue.rows(), [])


if __name__ == "__main__":
    unittest.main()
