#!/usr/bin/env python3
"""Finite, offline export of completed phonefarm evidence into the existing UI."""
import argparse
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat

JSON_CAP = 2 * 1024 * 1024
TOTAL_CAP = 32 * 1024 * 1024
FIXED = {
    "sf30": "livephase/endfield-surfaceflinger-timestats-30s/review.json",
    "sf30_raw": "livephase/endfield-surfaceflinger-timestats-30s/collector.stdout",
    "sf60": "livephase/endfield-60fps-sf-frequency-30s/review.json",
    "sf60_summary": "livephase/endfield-60fps-sf-frequency-30s/diagnostic-summary.json",
    "floor5": "livephase/endfield-gpu-floor5-capability-absolute/review.json",
    "thermal3": "livephase/gpu-control-probe-v4-deadline/review.json",
    "ft": "reporting/frametimeline-actual5s-analysis/result.json",
}


def digest(b):
    return hashlib.sha256(b).hexdigest()


def encode(v):
    return (json.dumps(v, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()


def safe_relative(s):
    if not isinstance(s, str):
        raise ValueError("input path must be text")
    p = PurePosixPath(s)
    if p.is_absolute() or not p.parts or any(
        c in {".", "..", "private-auth"} or c.startswith(".") for c in p.parts
    ) or str(p) != s:
        raise ValueError("forbidden/noncanonical input path")
    return p.parts


class Source:
    def __init__(self, root):
        root = Path(root)
        if "private-auth" in root.parts:
            raise ValueError("private-auth root is forbidden before filesystem access")
        if not root.is_absolute() or any(c in {".", ".."} for c in root.parts):
            raise ValueError("task root must be an absolute canonical path")
        # Anchor each ancestor without following aliases; all later reads use this fd.
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for c in root.parts[1:]:
                nxt = os.open(c, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = nxt
        except Exception:
            os.close(fd)
            raise
        self.root = root
        self.root_fd = fd
        self.total = 0
        self.inputs = []

    def __del__(self):
        if hasattr(self, "root_fd"):
            os.close(self.root_fd)

    def read(self, relative, expected=None):
        parts = safe_relative(relative)
        fd = os.dup(self.root_fd)
        try:
            for c in parts[:-1]:
                nxt = os.open(c, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = nxt
            f = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            try:
                m = os.fstat(f)
                if not stat.S_ISREG(m.st_mode) or m.st_size > JSON_CAP:
                    raise ValueError("input is not a bounded regular file")
                chunks = []
                count = 0
                while True:
                    chunk = os.read(f, min(65536, JSON_CAP + 1 - count))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    count += len(chunk)
                    if count > JSON_CAP:
                        raise ValueError("input byte cap exceeded")
                b = b"".join(chunks)
                if len(b) != m.st_size or os.fstat(f).st_size != m.st_size:
                    raise ValueError("input changed size while reading")
            finally:
                os.close(f)
        finally:
            os.close(fd)
        self.total += len(b)
        if self.total > TOTAL_CAP:
            raise ValueError("export input budget exceeded")
        h = digest(b)
        if expected is not None and h != expected:
            raise ValueError("review input hash mismatch: " + relative)
        self.inputs.append({"path": relative, "bytes": len(b), "sha256": h})
        return b

    def json(self, relative, expected=None):
        return json.loads(self.read(relative, expected))


def terminal(j):
    if j.get("completed") is not True and j.get("terminal") is not True:
        raise ValueError("receipt is not an explicit terminal checkpoint")


def metric(label, value, unit="", note=""):
    if value is not None and isinstance(value, float) and not math.isfinite(value):
        raise ValueError("nonfinite metric")
    return {"label": label, "value": value, "unit": unit, "note": note}


def public_reason(value):
    if not isinstance(value, str):
        return None
    value = value[:2000]
    value = re.sub(r"(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?", "[network]", value)
    value = re.sub(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}\b", "[boot-id]", value)
    value = re.sub(r"\b[0-9a-fA-F]{7,32}\b", "[device-id]", value)
    value = re.sub(r"/(?:Users|private|var|home|tmp)/[^\s,;\"']+", "[local-path]", value)
    return value


def record(i, title, kind, status, settings, metrics, limits, restoration, sources):
    return {"id": i, "title": title, "kind": kind, "status": status,
            "settings": settings, "metrics": metrics, "limitations": limits,
            "restoration": restoration, "sources": sources,
            "optimized_effect_proven": False, "promotion_allowed": False}


def initial_records(s):
    j = s.json(FIXED["sf30"])
    terminal(j)
    start = len(s.inputs) - 1
    raw = s.read(FIXED["sf30_raw"], j["artifact_sha256"]["collector.stdout"]).decode()
    marks = re.search(r"START_UPTIME=(\d+(?:\.\d+)?)", raw), re.search(r"END_UPTIME=(\d+(?:\.\d+)?)", raw)
    wall = round(float(marks[1].group(1)) - float(marks[0].group(1)), 3) if all(marks) else None
    layer = j["layer"]
    hist = {int(k.removesuffix("ms")): v for k, v in layer["histogram_counts"].items()}
    a = record("sf30", "原30设置 · SurfaceFlinger辅助诊断", "diagnostic", j["status"],
               "同营地端点；30设置；未施加系统候选", [
                   metric("SF桶内平均", layer["reported_average_fps"], "FPS", "仅被保留的目标层间隔，不是全窗FPS"),
                   metric("目标层间隔数", layer["total_frames"], "个"),
                   metric("实际启停窗", wall, "s"),
                   metric("桶标签加权跨度", sum(k*v for k, v in hist.items())/1000, "s"),
                   metric("≥50ms桶占比", layer["slow_bucket_ratio"]*100, "%", "有损量化；条件于保留的间隔")],
               ["不是正式A/B基线；未证明完整墙窗覆盖", "HAL 1→2；仅端点营地截图，不证明全窗场景不变"],
               "SF停止/清空 exit=" + str(j["sf_stop_clear_exit"]), s.inputs[start:])
    a["histogram"] = hist
    j = s.json(FIXED["sf60"])
    terminal(j)
    start = len(s.inputs)-1
    d = s.json(FIXED["sf60_summary"], j["artifact_sha256"]["diagnostic-summary.json"])
    hist = {int(k): v for k, v in d["nonzero_interval_histogram"].items()}
    weighted = sum(k*v for k, v in hist.items())/1000
    b = record("sf60", "60设置探索 · 覆盖存在缺口", "diagnostic", j["status"],
               "60仅作为探索负载；并非优化候选；原30随后已恢复", [
                   metric("SF桶内平均", float(d["reported_average_fps"]), "FPS", "不能解释成连续42.89s的FPS"),
                   metric("实际启停窗", j["actual_device_window_s"], "s"),
                   metric("桶标签加权跨度", weighted, "s"),
                   metric("未解释跨度", round(j["actual_device_window_s"]-weighted, 3), "s"),
                   metric("GPU离散点", len(d["frequency_samples"]), "个", "30个读点全部578MHz，不是驻留时间")],
               ["目标间隔1336；全局SF计数不代替游戏", "频率为离散采样；未知功耗；HAL 2→2"],
               "SF停止/清空 exit=" + str(j["sf_stop_clear_exit"]), s.inputs[start:])
    b["histogram"] = hist
    j = s.json(FIXED["thermal3"])
    terminal(j)
    c = record("thermal3", "配置能力探测 · 稳态门禁拒绝", "control_capability", j["status"],
               "旧boot的一次能力探测；不属于当前游戏收益试验", [
                   metric("7点热窗", j["clock_observation"]["actual_span_s"], "s"),
                   metric("全传感器最大跨度", j["thermal_diagnostics"]["max_sensor_span_c"], "°C", "冻结限3°C；CPU26"),
                   metric("实际控制写入", j["control_writes"], "次"),
                   metric("GPU最小档", str(j["gpu_min_before"])+"→"+str(j["gpu_min_terminal"]))],
               ["温度稳定性未通过；没有initial snapshot/restore graph", "11s持有与完整恢复实验均NOT_RUN；黑图不证明游戏"],
               "独立普通租约 exit=0；不能当作候选完整恢复验收", s.inputs[-1:])
    j = s.json(FIXED["floor5"])
    terminal(j)
    d = record("floor5", "GPU下限5 · 绝对热门禁写前拒绝", "control_capability", j["status"],
               "明确请求 gpu.min_pwrlevel=5；实际仍11", [
                   metric("热状态", "2→3", "HAL", "SEVERE在任何控制写入前出现"),
                   metric("实际控制写入", 0 if j["candidate_written"] is False else None, "次"),
                   metric("GPU最小档", str(j["actual_gpu_min_before"])+"→"+str(j["actual_gpu_min_after"]))],
               ["未安装worker/未创建还原图/未写候选", "11s持有与完整恢复实验NOT_RUN；无GPU/DDR增益数据"],
               "独立普通租约 exit=" + str(j["postfailure_normal_lease_exit"]), s.inputs[-1:])
    j = s.json(FIXED["ft"])
    if j["schema"] != "hgamey/actual_frametimeline_feasibility_result@1" or j["verdict"] != "UNSUPPORTED_FOR_TARGET_IN_THIS_CAPTURE":
        raise ValueError("unknown FrameTimeline result contract")
    e = record("ft5", "FrameTimeline 5s · 无目标SurfaceFrame", "diagnostic", j["verdict"],
               "原30营地；唯一5s采集；未重复", [
                   metric("目标SurfaceFrame", j["target"]["target_surface_frame_rows"], "条"),
                   metric("目标唯一显示token关联", j["target"]["unique_target_display_joins"], "条"),
                   metric("全局SF帧", j["inventory"]["all_actual_sf_display_rows"], "条", "不计作终末地帧"),
                   metric("实际CLI窗", j["clocks_windows"]["capture_cli_span_s"], "s")],
               ["目标PID/UID元数据正确，但不能建立目标显示指标", "注册loss计数为0不能补成游戏流完整", "原后截图超4MiB被截断；晚补图不是原窗端点"],
               "原lease UNKNOWN/2；独立reconcile完成；不改原始失败", s.inputs[-1:])
    return [a, b, c, d, e]


def eval_records(s, registry):
    if not registry:
        return []
    p = Path(registry)
    relative = p.relative_to(s.root).as_posix() if p.is_absolute() else p.as_posix()
    r = s.json(relative)
    if r.get("schema") != "hgamey/live_eval_input_registry@1" or not isinstance(r.get("entries"), list) or len(r["entries"]) > 32:
        raise ValueError("unknown/oversized eval registry")
    out = []
    ids = set()
    for item in r["entries"]:
        if item.get("terminal") is not True:
            raise ValueError("inflight eval not exportable")
        identifier = item.get("id")
        kind = item.get("kind", "existing_eval")
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-z0-9_-]{1,80}", identifier) or identifier in ids:
            raise ValueError("invalid/duplicate registry record identity")
        if kind not in {"existing_eval", "existing_prepare"}:
            raise ValueError("unknown existing workflow stage")
        ids.add(identifier)
        start = len(s.inputs)
        command = s.json(item["command"], item["command_sha256"])
        report = s.json(item["report"], item["report_sha256"])
        finished = command.get("finished_unix_s")
        exit_code = command.get("exit_code")
        if isinstance(finished, bool) or not isinstance(finished, (int, float)) or not math.isfinite(finished) or finished <= 0 or type(exit_code) is not int:
            raise ValueError("eval command is not completed")
        v = report.get("verdict", {})
        q = report.get("qualification", {})
        state = v.get("status", report.get("status", "UNKNOWN")) if isinstance(v, dict) else str(v)
        if not isinstance(state, str) or len(state) > 120 or not isinstance(q, dict) or not isinstance(report.get("metrics", {}), dict):
            raise ValueError("invalid receiver summary")
        x = record(identifier, "phonefarm现有流程 · " + item.get("label", "测量")[:160], kind, state,
                   "终末地；准备阶段不等于基线已采样" if kind == "existing_prepare" else "终末地；KGSL提交代理；按接收端原有口径", [],
                   ["提交代理FPS不等于显示FPS；面板不授予增益/质量/晋级", "功耗未知时保留null，不冒充瓦特"],
                   report.get("reverted", report.get("restored")) if isinstance(report.get("reverted", report.get("restored")), bool) else "UNKNOWN", s.inputs[start:])
        # No recursive raw receiver export: device IDs, absolute paths and argv stay local.
        projected_q = {}
        for k in ("stage", "status", "eligible", "probe_allowed", "experiment_allowed", "conclusion_allowed"):
            value = q.get(k)
            if k in {"stage", "status"}:
                if value is not None and not isinstance(value, str):
                    raise ValueError("invalid qualification text")
                projected_q[k] = public_reason(value)
            elif value is None or type(value) is bool:
                projected_q[k] = value
            else:
                raise ValueError("invalid qualification predicate")
        projected_metrics = {}
        for k in ("frame_p95", "frame_p95_ms", "fps_mean", "power_w_mean", "gpu_ms", "power_w"):
            value = report.get("metrics", {}).get(k)
            if value is None or (type(value) in {int, float} and math.isfinite(value)):
                projected_metrics[k] = value
        x["receiver_summary"] = {"verdict_status": state, "qualification": projected_q,
                                 "metrics": projected_metrics, "command_exit": exit_code,
                                 "reason": public_reason((v.get("reason") if isinstance(v, dict) else None) or report.get("error")),
                                 "finished_at": dt.datetime.fromtimestamp(finished, dt.timezone.utc).isoformat(),
                                 "metric_scope": "NOT_RUN: prepare only" if kind == "existing_prepare" else "gpu_submit_proxy; not display fps"}
        x["metrics"] = [metric(k, value, "W" if "power" in k else "FPS代理" if k == "fps_mean" else "ms",
                               "接收端保存值；缺失不填0") for k, value in projected_metrics.items()]
        notes = item.get("public_notes", [])
        if not isinstance(notes, list) or len(notes) > 8 or any(not isinstance(n, str) or len(n) > 300 for n in notes):
            raise ValueError("invalid checkpoint annotations")
        x["limitations"] += [public_reason(n) for n in notes]
        recovery_keys = {"restoration_command", "restoration_command_sha256", "restoration_report", "restoration_report_sha256"}
        if recovery_keys.intersection(item):
            if not recovery_keys.issubset(item):
                raise ValueError("incomplete restoration binding")
            rc = s.json(item["restoration_command"], item["restoration_command_sha256"])
            rr = s.json(item["restoration_report"], item["restoration_report_sha256"])
            rf = rc.get("finished_unix_s")
            if type(rf) not in {int, float} or not math.isfinite(rf) or rf < finished or type(rc.get("exit_code")) is not int:
                raise ValueError("restoration command not completed after original phase")
            if rc["exit_code"] == 0 and rr.get("status") == "RESTORED_REQUIRES_NEW_SESSION" and rr.get("old_samples_revalidated") is False:
                x["restoration"] = "reconcile返回 RESTORED_REQUIRES_NEW_SESSION；旧样本不重验；需新会话"
            else:
                x["restoration"] = "UNKNOWN：恢复回执没有满足本发布的完成合同"
            x["sources"] = s.inputs[start:]
        checkpoint_keys = {"checkpoint_review", "checkpoint_review_sha256"}
        if checkpoint_keys.intersection(item):
            if not checkpoint_keys.issubset(item) or not recovery_keys.issubset(item) or kind != "existing_prepare":
                raise ValueError("incomplete/unsupported checkpoint binding")
            review = s.json(item["checkpoint_review"], item["checkpoint_review_sha256"])
            at = review.get("completed_at_unix_s")
            if review.get("schema") != "phonefarm/live_existing_prepare_terminal_review@1" or review.get("status") != "BLOCKED_BEFORE_SELECTED_CONTROL_AND_BASELINES" or type(at) not in {int, float} or not math.isfinite(at) or at < finished:
                raise ValueError("unrecognized terminal prepare checkpoint")
            linked = review.get("artifact_sha256", {})
            for path_key, hash_key in [("command", "command_sha256"), ("report", "report_sha256"), ("restoration_command", "restoration_command_sha256"), ("restoration_report", "restoration_report_sha256")]:
                if path_key in item and linked.get(str(s.root/item[path_key])) != item[hash_key]:
                    raise ValueError("checkpoint contradicts consumed phase hashes")
            prepared = review.get("prepare", {})
            if prepared.get("status") != state or prepared.get("exit_code") != exit_code or prepared.get("performance_baselines_collected") != 0 or prepared.get("performance_baselines_requested") != 3 or prepared.get("selected_control_write") != "NOT_RUN" or prepared.get("main_remote_tools_installed") is not True or prepared.get("initial_snapshot_restore_graph_and_state_created") is not True or report.get("error") != "thermal_snapshot exited 82: ":
                raise ValueError("checkpoint prepare scope mismatch")
            warmup = review.get("warmup", {})
            if type(warmup.get("sample_count")) is not int or not 1 <= warmup["sample_count"] <= 256 or any(type(warmup.get(k)) not in {int, float} or not math.isfinite(warmup[k]) for k in ("span_s", "max_single_sensor_range_c")) or warmup.get("hal_statuses") != [0]*warmup["sample_count"]:
                raise ValueError("unsupported warmup diagnostic")
            x["metrics"] = [metric("基线采样", "0 / 3", "轮"), metric("候选施加", "NOT_RUN"), metric("帧 / 功耗", None)]
            x["limitations"] += ["写前温度资格检查失败：exit82共用数值/计数/参考范围，缺少失败温度值，原因未细分",
                "main工具已安装；initial snapshot、还原图和state已创建；恢复原值可能发生写入",
                f"{warmup['sample_count']}点暖机诊断{warmup['span_s']:.2f}s，最大单传感器跨度{warmup['max_single_sensor_range_c']:.1f}°C、HAL全0；这不是性能基线"]
            recovery = review.get("recovery", {})
            if recovery.get("exit_code") != rc["exit_code"] or recovery.get("status") != rr.get("status"):
                raise ValueError("checkpoint recovery contradicts consumed restoration")
            if recovery.get("exit_code") == 0 and recovery.get("full_initial_snapshot_equal") is True and recovery.get("owned_groups_absent_verified_by_existing_receiver") is True and recovery.get("remote_session_permanently_revoked") is True and recovery.get("normal_lease_after_recovery_exit_code") == 0:
                x["restoration"] += "；接收端核还原快照等于initial、远端会话撤销、归属组消失；独立普通租约exit0"
            x["sources"] = s.inputs[start:]
        out.append(x)
    return sorted(out, key=lambda x: x["receiver_summary"]["finished_at"])


def export(task_root, out, registry=None):
    source = Source(task_root)
    existing = eval_records(source, registry)
    records = list(reversed(existing)) + initial_records(source)
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    if existing and existing[-1]["kind"] == "existing_prepare":
        latest = existing[-1]
        count = next((m["value"] for m in latest["metrics"] if m["label"] == "基线采样"), "未知")
        workflow = "phonefarm confirmation-driver prepare：基线准备；辅助SF/FT不替换主指标"
        baseline_status = "最新prepare终态：" + latest["status"] + "；基线 " + str(count) + "；没有候选收益数据"
    elif existing:
        workflow = "phonefarm既有eval基线验证；辅助SF/FT不替换主指标"
        baseline_status = "最新现有eval终态：" + existing[-1]["status"] + "；按下方接收端资格与缺失指标核对"
    else:
        workflow = "phonefarm既有测量流程；辅助SF/FT不替换主指标"
        baseline_status = "等待现有测量流程终态回执；诊断记录不算正式基线PASS"
    data = {"schema": "hgamey/live_experiments_public@1", "generated_at": now,
            "measurement_date": "2026-10-12 JST", "device": "一加 Ace 5 · Android16 · root",
            "game": "明日方舟：终末地", "records": records,
            "main_workflow": workflow, "baseline_status": baseline_status,
            "optimized_effect_proven": False, "promotion_allowed": False,
            "power_watt": None, "refresh_seconds": 30,
            "cost": {"export_model_api_calls": 0, "export_device_calls": 0},
            "source_count": len(source.inputs)}
    empty = {"generated_at": now, "version": "static-live-export", "paths": {
        "game_opt_loop": "既有GOL面板资源", "phonefarm": "有限脱敏终态数据", "phonefarm_present": True, "lock_root": "不连接设备"},
        "layers": [], "carriers": [], "device": {}, "notices": [], "cases": {"cases": [], "problems": []}}
    out = Path(out)
    out.mkdir(mode=0o755, parents=False, exist_ok=False)
    try:
        for name in ("index.html", "app.js", "app.css"):
            b = (Path(__file__).resolve().parents[2]/"src/viz/assets"/name).read_bytes()
            (out/name).write_bytes(b)
        (out/"api").mkdir()
        (out/"api/dashboard").write_bytes(encode(empty))
        db = encode(data)
        (out/"api/live-experiments.json").write_bytes(db)
        (out/"latest.json").write_bytes(encode({"schema": "hgamey/live_dashboard_latest@1", "generated_at": now,
            "data_sha256": digest(db), "data_bytes": len(db), "source_count": len(source.inputs), "is_measurement_timestamp": False}))
        files = []
        for path in [out/"index.html", out/"app.js", out/"app.css", out/"api/dashboard", out/"api/live-experiments.json", out/"latest.json"]:
            b = path.read_bytes()
            files.append({"path": path.relative_to(out).as_posix(), "bytes": len(b), "sha256": digest(b)})
        manifest = {"schema": "hgamey/live_dashboard_release@1", "complete": True, "generated_at": now,
                    "files": files, "inputs": source.inputs, "public_raw_command_or_auth": False,
                    "optimized_effect_proven": False, "promotion_allowed": False}
        (out/"release-manifest.json").write_bytes(encode(manifest))
    except Exception as e:
        (out/"INCOMPLETE.json").write_bytes(encode({"complete": False, "reason": str(e)}))
        raise
    return manifest


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--task-root", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--eval-registry")
    args = p.parse_args()
    result = export(args.task_root, args.out, args.eval_registry)
    print(json.dumps({"complete": True, "files": len(result["files"]), "inputs": len(result["inputs"])}, ensure_ascii=False))
