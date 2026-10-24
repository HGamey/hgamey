"""Durable upload bindings; bytes and verification remain in the shared asset cache."""
from contextlib import contextmanager
import os
import time

from game_assets import AssetUploadError, UPLOAD_MAX, _upload_id
from job_dispatch import file_lock, request_fingerprint
from node_games import snapshot_body


class NodeUploads:
    def __init__(self, jobs, games):
        self.jobs, self.games = jobs, games
        self.store = games.store
        self.store.mkdir("_games/uploads")

    @staticmethod
    def _path(upload_id):
        return "_games/uploads/" + _upload_id(upload_id) + ".json"

    @contextmanager
    def _mutation(self, upload_id):
        path = os.path.join(self.jobs.data_root, "_games", "uploads", _upload_id(upload_id) + ".lock")
        try:
            with file_lock(path, blocking=False) as acquired:
                if not acquired:
                    raise AssetUploadError(409, "upload_busy", "upload is busy; query the same ID before retrying")
                yield
        except AssetUploadError:
            raise
        except OSError:
            raise AssetUploadError(503, "upload_storage_error", "upload storage could not be read or saved") from None

    def _entry(self, upload_id):
        try:
            return self.store.read_json(self._path(upload_id))
        except FileNotFoundError:
            raise AssetUploadError(404, "upload_not_found", "no such upload") from None

    def _verification(self, upload_id, entry):
        manifest = self.store.read_json(self.games._path(entry["game_id"], entry["version"]))["manifest"]
        spec = {"bin": "bundle_prepare", "argv": [self.games.producer, "--manifest", "@in/manifest.json",
                "--cache-root", self.games.cache_root, "--jobs-root", self.jobs.dir, "--out", "@out",
                "--verify-upload", upload_id, "--asset-index", str(entry["asset_index"])],
                "device_access": "none", "resources": [self.games._resource(entry["game_id"], entry["version"])],
                "label": "Verify upload " + upload_id}
        return spec, snapshot_body(manifest)

    def _job(self, upload_id, entry):
        row = self.jobs.dispatch.queue.by_request("upload-verify:" + upload_id)
        if row is None:
            return None
        spec, body = self._verification(upload_id, entry)
        if row["fingerprint"] != request_fingerprint(spec, body):
            raise AssetUploadError(409, "upload_verification_conflict", "verification request ID belongs to a different job; no job is adopted")
        jid = row["job_id"]
        return {**self.jobs.status(jid), "job_url": "/dash/jobs#job=" + jid,
                "report_url": "/dash/api/jobs/" + jid + "/out?path=preparation.json"}

    def get(self, upload_id):
        entry = self._entry(upload_id)
        asset = entry["asset"]
        state = self.games.cache.upload_status(asset, upload_id)
        observation = self.games.cache.observe(asset)
        job = self._job(upload_id, entry)
        phase, offset, error = state["state"], state["offset"], state.get("error")
        # A cache hit does not acquire someone else's historical reservation.
        # It is valid only while the current receipt still matches the content.
        if entry.get("cancelled"):
            phase, offset, error = "cancelled", None, None
        elif not entry["registered"]:
            phase, offset = "unclaimed", None
            error = "upload registration is incomplete; explicitly retry the same upload ID"
        elif entry.get("cache_hit"):
            phase = "verified" if observation["state"] == "verified" else "unknown"
            offset = asset["size"] if phase == "verified" else None
            error = observation.get("error")
        elif phase == "open":
            phase = "received" if offset == asset["size"] else "receiving"
        elif phase == "sealed" and job:
            if job.get("lost"):
                phase, error = "failed", "verification worker is lost; inspect the original job"
            elif job.get("state") in {"queued", "preparing", "running"}:
                phase = "verifying"
            elif job.get("state") in {"finished", "interrupted", "cancelled", "lost"}:
                phase, error = "failed", "verification stopped; inspect the original job before starting a new attempt"
        if phase == "verified" and observation["state"] != "verified":
            phase, offset, error = "unknown", None, "current cache verification is unavailable"
        return {"schema_version": 1, "upload_id": upload_id, "node_id": self.games.node_id,
                "game_id": entry["game_id"], "version": entry["version"], "asset_name": asset["name"],
                "sha256": asset["sha256"], "size": asset["size"], "offset": offset,
                "phase": phase, "error": error, "chunk_max_bytes": UPLOAD_MAX,
                "created_at": entry["created_at"], "observation": observation, "job": job}

    def open(self, body):
        if not isinstance(body, dict) or set(body) != {"upload_id", "game_id", "version", "asset_name"}:
            raise AssetUploadError(400, "invalid_upload", "upload requires upload_id, game_id, version and asset_name")
        upload_id = _upload_id(body["upload_id"])
        try:
            manifest = self.store.read_json(self.games._path(body["game_id"], body["version"]))["manifest"]
        except FileNotFoundError:
            raise AssetUploadError(404, "game_not_found", "register the game version before uploading") from None
        selected = [(index, asset) for index, asset in enumerate(manifest["assets"])
                    if asset["name"] == body["asset_name"]]
        if not selected:
            raise AssetUploadError(404, "asset_not_found", "no such asset in the registered version")
        index, asset = selected[0]
        binding = {"game_id": body["game_id"], "version": body["version"], "asset": asset, "asset_index": index}
        with self._mutation(upload_id):
            path = self._path(upload_id)
            # Publish the binding before acquiring content, so a crash never
            # leaves a reserved SHA without its recoverable client upload ID.
            self.store.publish_json_once(path, {**binding, "created_at": time.time(), "registered": False})
            entry = self._entry(upload_id)
            if any(entry[key] != value for key, value in binding.items()):
                raise AssetUploadError(409, "upload_conflict", "upload ID is already bound to another asset or version")
            if not entry["registered"]:
                state = self.games.cache.upload_open(asset, upload_id)
                self.store.write_json(path, {**entry, "registered": True, "cache_hit": bool(state.get("cache_hit"))})
        return self.get(upload_id)

    def chunk(self, upload_id, offset, length, stream):
        with self._mutation(upload_id):
            entry = self._entry(upload_id)
            self.games.cache.upload_chunk(entry["asset"], upload_id, offset, length, stream)
        return self.get(upload_id)

    def finalize(self, upload_id):
        with self._mutation(upload_id):
            entry = self._entry(upload_id)
            if entry.get("cancelled"):
                raise AssetUploadError(409, "upload_not_writable", "upload was cancelled before registration completed")
            if entry.get("cache_hit"):
                return self.get(upload_id)
            # An existing request is an observation, even when its verifier has
            # the content lock. Re-submit the identical spec only for queue dedup.
            if self._job(upload_id, entry) is None:
                sealed = self.games.cache.upload_seal(entry["asset"], upload_id)
                if sealed["state"] == "verified":
                    return self.get(upload_id)
                if sealed["state"] != "sealed":
                    raise AssetUploadError(409, "upload_not_sealed", "upload cannot be verified in its current state")
            spec, body = self._verification(upload_id, entry)
            self.jobs.start(spec, body, request_id="upload-verify:" + upload_id)
        return self.get(upload_id)

    def cancel(self, upload_id):
        with self._mutation(upload_id):
            entry = self._entry(upload_id)
            if not entry.get("cache_hit"):
                self.games.cache.upload_cancel(entry["asset"], upload_id)
                if not entry["registered"]:
                    # Cancellation of a saved but unclaimed binding is still
                    # terminal; a later create retry must not acquire content.
                    self.store.write_json(self._path(upload_id), {**entry, "registered": True, "cancelled": True})
        return self.get(upload_id)

    def write(self, parts, body):
        if parts == ["uploads"]:
            return 201, self.open(body)
        if len(parts) == 3 and parts[0] == "uploads" and parts[2] in {"finalize", "cancel"}:
            if body != {}:
                raise AssetUploadError(400, "invalid_upload", "upload action accepts an empty JSON object")
            return 200, getattr(self, parts[2])(parts[1])
        raise AssetUploadError(404, "upload_operation_not_found", "no such upload operation")
