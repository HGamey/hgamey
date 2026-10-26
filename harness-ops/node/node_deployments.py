"""Typed resource deployments use the existing queue; only PF touches devices."""
import json
import os
import re
import subprocess

from game_deploy_recipe import validate_recipe, validate_installation, validate_initialization, validate_scene
from job_inputs import validate as validate_inputs
from job_dispatch import request_fingerprint
from job_queue import RequestConflict
from node_games import MAX_MANIFEST, snapshot_body
from device_scope import binding, resource as device_resource

CAPABILITY = "games.deploy-v1"
INSTALL_CAPABILITY = "games.install-v1"
INITIALIZE_CAPABILITY = "games.initialize-v1"
SCENE_CAPABILITY = "games.scene-v1"
OPERATIONS = {
    "deploy": ("deployment", validate_recipe, "phonefarm/game-deployment@1", "resources"),
    "install": ("installation", validate_installation, "phonefarm/game-installation@1", "APK"),
    "initialize": ("initialization", validate_initialization, "phonefarm/game-initialization@1", "initialization"),
    "scene": ("scene_attempt", validate_scene, "phonefarm/game-scene@1", "scene attempt"),
}


def operation(request):
    return {f"phonefarm/game-{kind}-request@1": kind for kind in OPERATIONS}[request["schema"]]


class DeploymentError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.payload = status, {"code": code, "error": message}


class NodeDeployments:
    def __init__(self, jobs, games):
        self.jobs, self.games = jobs, games
        self.device_binding_available = False
        self.support = {"available": False, "capability": CAPABILITY,
                        "error": "configured phonefarm does not support resource deployment"}
        self.installation_support = {"available": False, "recovery_available": False, "capability": INSTALL_CAPABILITY,
                                     "error": "configured phonefarm or its fixed APK metadata tool is unavailable"}
        self.initialization_support = {"available": False, "capability": INITIALIZE_CAPABILITY,
                                       "error": "configured phonefarm does not support launcher initialization"}
        self.scene_support = {"available": False, "capability": SCENE_CAPABILITY,
                              "error": "configured phonefarm does not support bounded Doom render-demo attempts"}
        if "phonefarm" not in jobs.bins:
            return
        exe, cwd = jobs.bins["phonefarm"]
        env = jobs.pf_env
        if env is None:
            env = {k: v for k, v in os.environ.items() if not k.startswith("PF_JOB_") and k != "DEVLOCK_LABEL"}
        try:
            result = subprocess.run([exe, "game-deploy", "capabilities", "--json"], cwd=cwd, env=env,
                                    capture_output=True, timeout=5, check=False)
            value = json.loads(result.stdout) if len(result.stdout) <= MAX_MANIFEST else None
            if (result.returncode == 0 and isinstance(value, dict)
                    and value.get("schema") == "phonefarm/game-deploy-capabilities@1"):
                self.device_binding_available = value.get("device_binding_v1") is True
                if value.get("resources_v1") is True:
                    self.support.update(available=True, error=None)
                if value.get("launcher_initialization_v1") is True:
                    self.initialization_support.update(available=True, error=None)
                if value.get("doom_render_scene_v1") is True:
                    self.scene_support.update(available=True, error=None)
                if value.get("fresh_single_apk_v1") is True:
                    self.installation_support["recovery_available"] = True
                    metadata = value.get("metadata_tool") or {}
                    if isinstance(metadata, dict) and metadata.get("available") is True:
                        self.installation_support.update(available=True, error=None)
                    elif isinstance(metadata, dict):
                        reason = metadata.get("error")
                        if isinstance(reason, str) and len(reason) <= 512:
                            self.installation_support["error"] = reason
        except (OSError, ValueError, subprocess.SubprocessError):
            pass

    def _support(self, kind):
        return {"deploy": self.support, "install": self.installation_support,
                "initialize": self.initialization_support, "scene": self.scene_support}[kind]

    def _scene_inputs(self, sources):
        if not isinstance(sources, dict) or set(sources) != {"report", "state"}:
            raise ValueError("scene deployment requires report and state input sources")
        files = []
        for name, path, target in (("report", "deployment.json", "source-deployment.json"),
                                   ("state", "game-deploy/session.json", "source-session.json")):
            source = sources[name]
            if (not isinstance(source, dict) or source.get("path") != path
                    or type(source.get("size")) is not int or not 0 < source["size"] <= 128 << 10):
                raise ValueError("scene deployment sources require the original small report and session files")
            files.append({"name": name, "target": target, "source": source})
        inputs = validate_inputs({"schema": "phonefarm/job-inputs@1", "node_id": self.games.node_id,
                                  "files": files, "bindings": [], "cwd": "input"}, self.games.node_id)
        if inputs["files"][0]["source"]["job_id"] != inputs["files"][1]["source"]["job_id"]:
            raise ValueError("scene report and session must come from the same deployment job")
        return inputs

    def _scene_sources(self, row):
        # A recovery needs its saved session, not a fresh copy of external inputs.
        # Retain provenance in the original queue declaration for readonly binding.
        seen = set()
        while row["spec"].get("recovery_for"):
            parent = row["spec"]["recovery_for"]
            if parent in seen:
                raise ValueError("scene recovery ancestry contains a cycle")
            seen.add(parent)
            row = self.jobs.dispatch.queue.get(parent)
            if row is None:
                raise ValueError("scene input declaration is unavailable")
        inputs = row["spec"].get("inputs")
        if not isinstance(inputs, dict):
            raise ValueError("scene input declaration is unavailable")
        sources = {item["name"]: item["source"] for item in inputs.get("files", [])}
        if self._scene_inputs(sources) != inputs:
            raise ValueError("scene input declaration changed")
        return sources

    def _require_support(self, kind="deploy", recovery=False):
        support = self._support(kind)
        if not support.get("recovery_available" if recovery and kind == "install" else "available"):
            raise DeploymentError(503, "deployment_unavailable", support["error"])

    def _request(self, game_id, version, body, kind="deploy"):
        fields = {"node_id", "serial", "mode"} | ({"deployment"} if kind == "scene" else set())
        if not isinstance(body, dict) or set(body) not in (fields, fields | {"device_binding"}):
            raise ValueError("deployment requires node_id, serial and mode; scene also requires its deployment input sources")
        if body["node_id"] != self.games.node_id:
            raise DeploymentError(409, "node_mismatch", "select the current node before deploying")
        serial = body["serial"]
        if (not isinstance(serial, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", serial)
                or serial.lower().startswith(("hdc:", "ios:"))):
            raise ValueError("deployment requires an explicit Android serial")
        if body["mode"] not in {"check", "apply"}:
            raise ValueError("deployment mode must be check or apply")
        if "device_binding" in body and binding(body["device_binding"])["serial"] != serial:
            raise ValueError("deployment serial must match device_binding")
        if kind == "scene":
            self._scene_inputs(body["deployment"])
        entry = self.games.store.read_json(self.games._path(game_id, version))
        manifest = entry["manifest"]
        field, validator, _, _ = OPERATIONS[kind]
        if field not in manifest:
            raise DeploymentError(409, "recipe_missing", "this version has no supported " + field + " recipe")
        recipe = validator(manifest[field], manifest["assets"])
        selected = ({recipe["apk_asset_name"]} if kind == "install" else
                    {item["asset_name"] for item in recipe["files"]} if kind == "deploy" else
                    {recipe["demo_asset_name"], recipe["compat_asset_name"]} if kind == "scene" else set())
        assets = [asset for asset in manifest["assets"] if asset["name"] in selected]
        request = {"schema": f"phonefarm/game-{kind}-request@1",
                   "node_id": self.games.node_id,
                   "game_id": game_id, "version": version, "serial": serial, "mode": body["mode"],
                   "recipe": recipe, "assets": [{"name": a["name"], "path": str(self.games.cache.object_path(a["sha256"])),
                                                "sha256": a["sha256"], "size": a["size"]} for a in assets]}
        if kind == "scene":
            request["deployment"] = {"report_path": "source-deployment.json", "state_path": "source-session.json"}
        if len(json.dumps(request).encode()) > MAX_MANIFEST:
            raise ValueError("frozen deployment request exceeds 64 KiB")
        return request, assets

    def _spec(self, request, recovery_for=None, inputs=None, device_binding=None):
        serial = request["serial"]
        spec = {"bin": "phonefarm", "argv": ["game-deploy", "--request", "@in/manifest.json",
                "--serial", serial, "--out", "@out", "--json"],
                "label": f"{request['mode'].title()} {OPERATIONS[operation(request)][3]} {request['game_id']} {request['version']}",
                "device_access": "write", "resources": sorted(["device:" + serial,
                    self.games._resource(request["game_id"], request["version"])])}
        if device_binding is not None:
            spec.update(device_binding=binding(device_binding), resources=[device_resource(device_binding)])
        if recovery_for:
            spec.update(argv=["game-deploy", "recover", "--session",
                              self._source_session(recovery_for),
                              "--serial", serial, "--out", "@out", "--json"],
                        label="Recover deployment " + recovery_for, device_access="recovery", recovery_for=recovery_for)
        elif operation(request) == "scene":
            if inputs is None:
                raise ValueError("scene requires frozen deployment input references")
            spec["inputs"] = inputs
        return spec

    def _source_session(self, jid):
        report = self.jobs._read_root.read_json(jid + "/out/deployment.json", maximum=1 << 20)
        if report.get("session_path") == "game-deploy":
            return self.jobs.path(jid, "out", "game-deploy")
        # Acquisition can fail before an attempt has its own session. Its
        # frozen argv, not a caller-supplied path, identifies the still-held source.
        row = self.jobs.dispatch.queue.get(jid)
        argv = row["spec"].get("argv", [])
        if (row["spec"].get("recovery_for") and argv[:3] == ["game-deploy", "recover", "--session"]
                and (report.get("recovery") or {}).get("context_moved") is False
                and (report.get("recovery") or {}).get("source_session") in (argv[3], os.path.realpath(argv[3]))):
            relative = os.path.relpath(argv[3], self.jobs.dir).split(os.sep)
            if len(relative) == 3 and relative[1:] == ["out", "game-deploy"]:
                return self.jobs.path(relative[0], "out", "game-deploy")
        raise DeploymentError(409, "recovery_context_unavailable", "no bound deployment recovery session is available")

    def start(self, game_id, version, body, request_id, kind="deploy"):
        key = self.jobs.dispatch.request_key(request_id)
        if not request_id:
            raise ValueError("X-Request-ID is required; query the same request after an uncertain response")
        request, assets = self._request(game_id, version, body, kind)
        inputs = self._scene_inputs(body["deployment"]) if kind == "scene" else None
        spec, payload = self._spec(request, inputs=inputs, device_binding=body.get("device_binding")), snapshot_body(request)
        # A retry still returns the original job after its cache has disappeared.
        if not self.jobs.dispatch.queue.by_request(key):
            self._require_support(kind)
            missing = [a["name"] for a in assets if self.games.cache.observe(a)["state"] != "verified"]
            if missing:
                raise DeploymentError(409, "assets_unavailable", "verify selected resources before deployment: " + ", ".join(missing))
        return self.jobs.start(spec, payload, request_id=key)

    def require_device_binding(self):
        if not self.device_binding_available:
            raise DeploymentError(503, "deployment_binding_unavailable",
                                  "configured phonefarm does not support game device binding; no global fallback")

    def validate_recovery_spec(self, spec):
        parent, request = self._bound(spec["recovery_for"])
        original = parent["spec"].get("device_binding")
        expected = self._spec(request, parent["job_id"], device_binding=original)
        if "priority" in spec:
            expected["priority"] = spec["priority"]
        report, _ = self._report(parent["job_id"], request, original)
        if spec != expected or report is None:
            raise DeploymentError(409, "deployment_binding_changed",
                                  "bound recovery must inherit the original game transaction, report and device binding")

    def _bound(self, jid):
        self.jobs.path(jid)
        row = self.jobs.dispatch.queue.get(jid)
        if not row or row["spec"].get("bin") != "phonefarm" or row["spec"].get("argv", [])[:1] != ["game-deploy"]:
            raise DeploymentError(404, "deployment_not_found", "this job is not a typed resource deployment")
        request = self.jobs._read_root.read_json(jid + "/in/manifest.json", maximum=MAX_MANIFEST)
        body = {k: request[k] for k in ("node_id", "serial", "mode")}
        original = row["spec"].get("device_binding")
        if original is not None:
            body["device_binding"] = original
        inputs = None
        if operation(request) == "scene":
            body["deployment"] = self._scene_sources(row)
            inputs = self._scene_inputs(body["deployment"])
        expected, _ = self._request(request["game_id"], request["version"],
                                     body, operation(request))
        parent = row["spec"].get("recovery_for")
        if parent:
            source = self.jobs.dispatch.queue.get(parent)
            if (self.jobs._read_root.read_json(parent + "/in/manifest.json", maximum=MAX_MANIFEST) != request
                    or not source or source["spec"].get("device_binding") != original):
                raise DeploymentError(409, "deployment_binding_changed", "recovery inputs differ from its source transaction")
        expected_spec = self._spec(request, parent, inputs, original)
        if "priority" in row["spec"]:
            expected_spec["priority"] = row["spec"]["priority"]
        if request != expected or row["fingerprint"] != request_fingerprint(expected_spec, snapshot_body(request)):
            raise DeploymentError(409, "deployment_binding_changed", "deployment input or queue binding does not match the registered version")
        return row, request

    def _report(self, jid, request, device_binding=None):
        try:
            value = self.jobs._read_root.read_json(jid + "/out/deployment.json", maximum=1 << 20)
            schema = OPERATIONS[operation(request)][2]
            facts = ("installed", "resources_deployed", "application_access", "login", "scene_ready", "recovery")
            if operation(request) == "initialize":
                facts += ("storage_initialization", "launch", "stop")
            elif operation(request) == "scene":
                facts += ("input", "launcher_configuration", "runtime_seed", "launch", "scene_observation", "stop")
            if (not isinstance(value, dict) or value.get("schema") != schema
                    or any(value.get(k) != request[k] for k in ("node_id", "game_id", "version", "serial", "mode"))
                    or value.get("session_path") not in (None, "game-deploy")
                    or any(not isinstance(value.get(k), dict) for k in facts)):
                raise ValueError("deployment report identity mismatch")
            if (value.get("device_binding") != device_binding
                    or (value.get("lease_scope") == "device") != (device_binding is not None)):
                raise ValueError("deployment report device binding mismatch")
            return value, None
        except FileNotFoundError:
            return None, None
        except (OSError, ValueError):
            return None, "deployment report is unavailable or its identity does not match this job"

    def _qualification(self, row, status, report):
        if status.get("phase") in {"finalizing", "storage_blocked"}:
            return "commit the source result first; use retry-finalize if storage is blocked"
        if row["state"] != "blocked" or status.get("state") != "finished":
            return "recovery requires a finished job with a blocked reservation"
        execution = status.get("execution") or {}
        if execution.get("kind") != "supervised-v1" or execution.get("worker_alive") is not False:
            return "source worker completion is unconfirmed; reconcile its execution domain first"
        contained = (row["spec"].get("execution_policy") == "contained"
                     or (execution.get("process_management") or {}).get("policy") == "contained")
        if contained:
            problem = self.jobs.host_completion_problem(row["job_id"], status)
            if problem:
                return problem
        elif (status.get("phase") != "finished" or status.get("lost")
              or (execution.get("owned_process_group") or {}).get("status") != "absent"):
            return "source original process group completion is unconfirmed"
        if (not report or report.get("phase") != "recovery_required"
                or report["recovery"].get("status") != "required"):
            return "no recoverable deployment context is reported; inspect the original job and lease"
        try:
            self._source_session(row["job_id"])
        except (OSError, ValueError, KeyError, DeploymentError):
            return "no bound deployment recovery session is available"
        return None

    def _recovery(self, row, status, report, rows, kind):
        support = self._support(kind)
        supported = support.get("recovery_available", support["available"])
        target = row["job_id"]
        while True:
            children = [r for r in rows if r["spec"].get("recovery_for") == target
                        and not (r.get("state_reason") or {}).get("kind") == "cancelled_before_launch"]
            if not children:
                break
            if len(children) == 1 and children[0]["state"] == "done":
                return {"supported": supported, "eligible": False,
                        "target_job_id": children[0]["job_id"],
                        "reason": "the recovery attempt is settled; inspect its receipt and explicitly release the source reservation"}
            if len(children) != 1 or children[0]["state"] != "blocked":
                return {"supported": supported, "eligible": False, "target_job_id": target,
                        "reason": "an existing recovery attempt is active; inspect its original job"}
            row = children[0]
            target = row["job_id"]
            try:
                status = self.jobs.status(target)
                row, request = self._bound(target)
                report, _ = self._report(target, request, row["spec"].get("device_binding"))
            except (OSError, ValueError, KeyError, DeploymentError):
                return {"supported": supported, "eligible": False, "target_job_id": target,
                        "reason": "blocked recovery context is unavailable; inspect that attempt"}
        reason = self._qualification(row, status, report) if supported else support["error"]
        return {"supported": supported, "eligible": reason is None,
                "target_job_id": target, "reason": reason}

    def get(self, jid, rows=None):
        job = self.jobs.status(jid)
        # Finalization may commit while status is read. Do not qualify that
        # finished status against a queue row captured before the read.
        row, request = self._bound(jid)
        report, error = self._report(jid, request, row["spec"].get("device_binding"))
        kind = operation(request)
        return {"schema_version": 1, "job": job,
                "target": {"node_id": request["node_id"], "serial": request["serial"],
                           **({"device_binding": row["spec"]["device_binding"]} if "device_binding" in row["spec"] else {})},
                "game_id": request["game_id"], "version": request["version"], "operation": kind,
                "mode": "recover" if row["spec"].get("recovery_for") else request["mode"],
                "report": report, "report_error": error, "job_url": "/dash/jobs#job=" + jid,
                "report_url": "/dash/api/jobs/" + jid + "/out?path=deployment.json",
                "recovery": self._recovery(row, job, report, self.jobs.dispatch.queue.rows() if rows is None else rows, kind)}

    def assistance_draft(self, jid, research_id, direction_id):
        """Draft existing research metadata from saved facts; never refresh execution."""
        from node_research import normalize

        row, request = self._bound(jid)
        report, error = self._report(jid, request, row["spec"].get("device_binding"))
        if report is None:
            raise DeploymentError(409, "deployment_report_unavailable",
                                  error or "a saved deployment report is required to draft assistance")
        study = self.jobs.research.get("research", research_id)
        direction = self.jobs.research.get("direction", direction_id)
        if direction["research_id"] != research_id:
            raise DeploymentError(409, "assistance_context_mismatch", "direction belongs to another research")
        declared_game = study["data"].get("game") or {}
        if declared_game.get("game_id") not in (None, request["game_id"]):
            raise DeploymentError(409, "assistance_context_mismatch", "research declares a different game")
        kind = operation(request)
        facts = {}
        for field in ("installed", "resources_deployed", "application_access", "login", "scene_ready",
                      "storage_initialization", "runtime_seed", "scene_observation", "launch", "stop", "recovery"):
            if field in report:
                value = report[field].get("status") if isinstance(report[field], dict) else None
                facts[field] = value if isinstance(value, str) else "unknown"
        missing = {key: value for key, value in facts.items()
                   if value not in ("verified", "ready", "prepared", "confirmed", "matched",
                                    "not_needed", "not_required", "not_applicable")}
        target = f"node={request['node_id']}，game={request['game_id']}，version={request['version']}，serial={request['serial']}"
        data = normalize("request", {
            "research_id": research_id, "type": "assistance", "state": "open",
            "title": f"协助核对 {request['game_id']} {request['version']} 的准备前提",
            "need": "请确认所需现场操作或补充材料；不要在回复中填写账号密码、令牌。原报告未验证/未完成事项："
                    + (json.dumps(missing, ensure_ascii=False) if missing else "原报告未指明；请明确实际需要协助的条件。"),
            "why": f"原任务 {jid}；{target}；operation={kind}，phase={report.get('phase', 'unknown')}，"
                   f"observed_at={report.get('observed_at', 'unknown')}。这些是历史观察，不保证当前仍缺失。",
            "resume_hint": f"收到反馈后，先读取原任务 {jid} 的 deployment 报告并核对 {target} 和未完成恢复义务。"
                           f"若需要设备复核，沿原 {kind} 配方/源输入显式提交新的 games {kind} --check 任务并使用新的 request ID；"
                           "不重放旧请求。保存新任务证据，只认可该检查覆盖的安装/资源/目录前提；"
                           "登录、授权、场景及同状态没有专门验证时仍为 unknown。"
                           "反馈和关闭请求不自动解除依赖或恢复屏障；外部 Agent 再显式决定方向的 resolution。",
            "context": {"research_revision": study["revision"],
                        "directions": [{"id": direction_id, "revision": direction["revision"]}]},
            "evidence_refs": [{"kind": "job", "node_id": request["node_id"], "job_id": jid}],
        })
        return {"schema": "hgamey/game-assistance-draft@1", "node_id": request["node_id"], "job_id": jid,
                "source": {"game_id": request["game_id"], "version": request["version"],
                           "serial": request["serial"], "operation": kind, "phase": report.get("phase")},
                "data": data,
                "limitations": ["只生成草稿，未创建请求、方向依赖或设备任务。",
                                "原任务引用不是报告内容哈希；缺失事实来自保存的历史报告。",
                                "人工反馈不证明登录、场景就绪或恢复完成。"]}

    def history(self, entry, rows):
        resource = self.games._resource(entry["game_id"], entry["version"])
        result = []
        for row in reversed(rows):
            if row["spec"].get("argv", [])[:1] != ["game-deploy"]:
                continue
            belongs = resource in row["resources"]
            try:
                if not belongs:
                    # Scoped jobs reserve the device only. The frozen input
                    # retains game identity without serializing other phones.
                    if "device_binding" not in row["spec"]:
                        continue
                    request = self.jobs._read_root.read_json(row["job_id"] + "/in/manifest.json", maximum=MAX_MANIFEST)
                    if (request.get("game_id"), request.get("version")) != (entry["game_id"], entry["version"]):
                        continue
                    belongs = True
                record = self.get(row["job_id"], rows)
                result.append({**record["job"], **record})
            except (OSError, ValueError, KeyError, DeploymentError):
                if not belongs:
                    continue  # Missing input cannot establish which game owns this job.
                result.append({"id": row["job_id"], "operation": "unknown", "state": "unknown", "report": None,
                               "report_error": "deployment input is unavailable; inspect the original job",
                               "job_url": "/dash/jobs#job=" + row["job_id"]})
        return result

    def recover(self, jid, body, request_id):
        if body != {} or not request_id:
            raise ValueError("recovery requires an empty JSON object and X-Request-ID; the node derives paths and serial")
        key = self.jobs.dispatch.request_key(request_id)
        row, request = self._bound(jid)
        spec, payload = self._spec(request, jid, device_binding=row["spec"].get("device_binding")), snapshot_body(request)
        def admit():
            self._require_support(operation(request), recovery=True)
            current = self.get(jid)
            recovery = current["recovery"]
            if recovery["target_job_id"] != jid:
                raise RequestConflict("recover the latest blocked deployment attempt: " + recovery["target_job_id"])
            if not recovery["eligible"]:
                raise RequestConflict(recovery["reason"])
            os.close(self.jobs._read_root.open_fd(os.path.relpath(self._source_session(jid), self.jobs.dir), directory=True))
        return self.jobs.start(spec, payload, request_id=key, admit=admit)
