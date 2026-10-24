"""Immutable game manifests and host preparation through the existing job queue."""
import gzip
import io
import json
import os
import re
import sys
import tarfile
import time

from artifact_io import SafeRoot
from game_assets import AssetCache, validate_manifest


MAX_MANIFEST = 64 << 10


class GameError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", value):
        raise GameError(400, "game_id and version require 1-64 lowercase identifier characters")
    return value


def snapshot_body(manifest):
    """Stable bytes make a repeated request address the same queue reservation."""
    raw = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w") as archive:
            member = tarfile.TarInfo("manifest.json")
            member.size, member.mode = len(raw), 0o600
            archive.addfile(member, io.BytesIO(raw))
    return out.getvalue()


class NodeGames:
    def __init__(self, jobs, node_id):
        self.jobs, self.node_id = jobs, node_id
        self.store = SafeRoot(jobs.data_root)
        self.store.mkdir("_games")
        self.store.mkdir("_games/catalog")
        self.cache_root = os.path.join(jobs.data_root, "_games", "cache")
        self.cache = AssetCache(self.cache_root)
        module = os.path.dirname(os.path.abspath(__file__))
        self.producer = os.path.join(module, "game_assets.py")
        jobs.bins["bundle_prepare"] = (sys.executable, module)

    def close(self):
        self.store.close()

    @staticmethod
    def _path(game_id, version):
        return f"_games/catalog/{identifier(game_id)}/{identifier(version)}.json"

    @staticmethod
    def _resource(game_id, version):
        return f"game:{game_id}:{version}"

    def register(self, manifest):
        if len(json.dumps(manifest, ensure_ascii=False).encode()) > MAX_MANIFEST:
            raise GameError(413, "game manifest exceeds 64 KiB")
        manifest = validate_manifest(manifest)
        game_id, version = identifier(manifest["game_id"]), identifier(manifest["version"])
        self.store.mkdir("_games/catalog/" + game_id)
        path = self._path(game_id, version)
        entry = {"schema_version": 1, "game_id": game_id, "version": version,
                 "title": manifest["title"], "created_at": time.time(), "manifest": manifest}
        self.store.publish_json_once(path, entry)
        saved = self.store.read_json(path)
        if saved["manifest"] != manifest:
            raise GameError(409, "this game version is immutable; register a new version for changed contents")
        return self._record(saved)

    def list(self):
        entries, warnings = [], []
        fd = self.store.open_fd("_games/catalog", directory=True)
        try:
            names = sorted(os.listdir(fd))
        finally:
            os.close(fd)
        rows = self.jobs.dispatch.queue.rows()
        for game_id in names:
            try:
                identifier(game_id)
                fd = self.store.open_fd("_games/catalog/" + game_id, directory=True)
                try:
                    versions = sorted(os.listdir(fd))
                finally:
                    os.close(fd)
                for name in versions:
                    if not name.endswith(".json"):
                        continue
                    try:
                        saved = self.store.read_json(self._path(game_id, name[:-5]))
                        entries.append(self._record(saved, rows=rows, history=False))
                    except (OSError, ValueError, KeyError):
                        warnings.append({"game_id": game_id, "version": name[:-5], "error": "manifest unavailable"})
            except (OSError, ValueError):
                warnings.append({"game_id": game_id, "error": "catalog entry unavailable"})
        return {"schema_version": 1, "node_id": self.node_id, "items": entries, "warnings": warnings}

    def get(self, game_id, version):
        return self._record(self.store.read_json(self._path(game_id, version)))

    def _preparations(self, entry, rows):
        resource = self._resource(entry["game_id"], entry["version"])
        result = []
        for row in reversed(rows):
            if row["spec"].get("bin") != "bundle_prepare" or row["resources"] != [resource]:
                continue
            jid = row["job_id"]
            try:
                job = self.jobs.status(jid)
            except (OSError, ValueError, KeyError):
                job = {"id": jid, "request_id": row["request_id"], "state": "unknown",
                       "error": "job metadata unavailable"}
            report, error = None, None
            try:
                report = self.jobs._read_root.read_json(jid + "/out/preparation.json")
                if not isinstance(report, dict) or (report.get("game_id"), report.get("version")) != (
                        entry["game_id"], entry["version"]):
                    raise ValueError("report identity mismatch")
            except FileNotFoundError:
                pass
            except (OSError, ValueError):
                report, error = None, "preparation report unavailable"
            result.append({**job, "report": report, "report_error": error,
                           "job_url": "/dash/jobs#job=" + jid,
                           "report_url": "/dash/api/jobs/" + jid + "/out?path=preparation.json"})
        return result

    def _record(self, entry, rows=None, history=True):
        rows = self.jobs.dispatch.queue.rows() if rows is None else rows
        preparations = self._preparations(entry, rows)
        latest = preparations[0] if preparations else None
        reported = {a.get("name"): a for a in ((latest or {}).get("report") or {}).get("assets", [])}
        assets = []
        for asset in entry["manifest"]["assets"]:
            observation = self.cache.observe(asset)
            previous = reported.get(asset["name"], {})
            # A past task cannot overwrite the current cache's file identity or error.
            progress = previous if previous.get("sha256") == asset["sha256"] else None
            assets.append({**asset, "observation": observation, "progress": progress})
        states = {a["observation"]["state"] for a in assets}
        ready = "verified" if states == {"verified"} else ("unknown" if "unknown" in states else "incomplete")
        deployments = getattr(self.jobs, "deployments", None)
        transactions = deployments.history(entry, rows) if deployments and history else []
        return {**entry, "node_id": self.node_id, "assets": assets, "resource_state": ready,
                "deployment_support": deployments.support if deployments else {"available": False},
                "deployments": [r for r in transactions if r["operation"] == "deploy"],
                "installation_support": deployments.installation_support if deployments else {"available": False},
                "installations": [r for r in transactions if r["operation"] == "install"],
                "deployment_history_errors": [r for r in transactions if r["operation"] == "unknown"],
                "preparations": preparations if history else [], "preparation_count": len(preparations),
                "device_state": dict.fromkeys(("installed", "resources_deployed", "login", "scene_ready"), "unknown")}

    def prepare(self, game_id, version, body, request_id):
        if body != {}:
            raise GameError(400, "prepare accepts an empty JSON object; inputs come from the registered version")
        if not request_id:
            raise GameError(400, "X-Request-ID is required; query the same request after an uncertain response")
        entry = self.store.read_json(self._path(game_id, version))
        spec = {"bin": "bundle_prepare", "argv": [self.producer, "--manifest", "@in/manifest.json",
                "--cache-root", self.cache_root, "--jobs-root", self.jobs.dir, "--out", "@out"],
                "device_access": "none", "resources": [self._resource(game_id, version)],
                "label": f"Prepare {game_id} {version}"}
        return self.jobs.start(spec, snapshot_body(entry["manifest"]), request_id=request_id)

    def write(self, parts, body, request_id):
        if parts == ["games"]:
            return 201, self.register(body)
        if len(parts) == 5 and parts[0] == "games" and parts[2] == "versions" and parts[4] == "prepare":
            return 200, self.prepare(parts[1], parts[3], body, request_id)
        if len(parts) == 5 and parts[0] == "games" and parts[2] == "versions" and parts[4] == "deploy":
            return 200, self.jobs.deployments.start(parts[1], parts[3], body, request_id)
        if len(parts) == 5 and parts[0] == "games" and parts[2] == "versions" and parts[4] == "install":
            return 200, self.jobs.deployments.start(parts[1], parts[3], body, request_id, "install")
        raise GameError(404, "no such game operation")
