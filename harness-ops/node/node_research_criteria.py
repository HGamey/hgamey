"""Versioned, authored objectives evaluated on explicitly selected registered points."""
import math

from node_result_compare import validate_spec, MAX_SPEC
from node_results import ResultError, encoded, identifier, object_fields, text


RULES = {"absolute_improvement_min", "relative_improvement_percent_min",
         "candidate_min", "candidate_max", "degradation_max"}
SCOPES = {"module", "game_loop", "display", "gpu_submit_proxy", "power", "image"}


class BoundResults:
    def __init__(self, results, bindings):
        self.results = results
        self.bindings = {item["result_id"]: item for item in bindings}

    def get(self, rid):
        record, binding = self.results.get(rid), self.bindings[rid]
        if binding["status"] == "registration_bound" and (
                record["sequence"] != binding["result_sequence"]
                or record["registered_at"] != binding["result_registered_at"]):
            raise ResultError(409, "research_result_identity_changed", "result registration differs from the frozen criteria binding")
        return record


def finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def validate(criteria):
    object_fields(criteria, {"schema", "comparison", "baseline_role", "axes", "pareto_axes"})
    if criteria.get("schema") != "phonefarm/research-criteria@1" or criteria.get("baseline_role") != "original_game":
        raise ValueError("criteria requires its schema and an explicit original_game baseline role")
    spec = criteria.get("comparison")
    validate_spec(spec, spec.get("node_id") if isinstance(spec, dict) else None)
    if len(encoded(spec).encode("utf-8")) > MAX_SPEC:
        raise ValueError("criteria comparison exceeds 16 KiB")
    axes, pareto = criteria.get("axes"), criteria.get("pareto_axes")
    if not isinstance(axes, list) or not 1 <= len(axes) <= 16:
        raise ValueError("criteria requires 1 to 16 axes")
    ids, rows = set(), set()
    for axis in axes:
        object_fields(axis, {"id", "row_id", "label", "unit", "scope", "role", "prefer", "required", "rule"})
        aid, row = identifier(axis.get("id")), identifier(axis.get("row_id"))
        if aid in ids or row in rows or row not in {item["id"] for item in spec["rows"]}:
            raise ValueError("axes require unique IDs and unique explicitly selected comparison rows")
        ids.add(aid); rows.add(row)
        text(axis.get("label"), 128, True); text(axis.get("unit"), 2048, True)
        if axis.get("scope") not in SCOPES or axis.get("prefer") not in {"minimize", "maximize"}:
            raise ValueError("axis requires a known scope and an explicit preference")
        if axis.get("role") not in {"objective", "constraint", "observe"} or type(axis.get("required")) is not bool:
            raise ValueError("axis requires a role and required boolean")
        if "rule" not in axis:
            raise ValueError("axis requires an explicit rule or null")
        rule = axis["rule"]
        if rule is not None:
            object_fields(rule, {"kind", "threshold"})
            if rule.get("kind") not in RULES or not finite(rule.get("threshold")):
                raise ValueError("rule requires a supported kind and finite threshold")
            if rule["kind"] not in {"candidate_min", "candidate_max"} and rule["threshold"] < 0:
                raise ValueError("improvement and degradation thresholds must be nonnegative")
        if axis["required"] and rule is None or axis["role"] == "observe" and (rule is not None or axis["required"]):
            raise ValueError("required axes need rules; observe axes have no rule and are not required")
    if not any(axis["role"] == "objective" for axis in axes):
        raise ValueError("criteria requires at least one objective axis")
    if not isinstance(pareto, list) or len(pareto) > 16:
        raise ValueError("pareto_axes requires an explicit array of axis IDs")
    for aid in pareto:
        identifier(aid)
    if len(set(pareto)) != len(pareto) or any(aid not in ids for aid in pareto):
        raise ValueError("pareto_axes must select unique defined axes")
    return criteria


def _value(axis, cell):
    if cell["status"] != "available":
        return None, None, cell["reason"]
    metric = cell["metric"]
    value, unit = metric["value"], metric.get("unit")
    reason = ("value_unavailable" if not finite(value) else "axis_unit_mismatch" if unit != axis["unit"]
              else "axis_scope_mismatch" if metric.get("scope") != axis["scope"] else None)
    return value, unit, reason


def _rule(axis, cell, value, reason):
    if reason:
        return "unknown", None, reason
    rule = axis["rule"]
    if rule is None:
        return "observed", value, None
    kind, threshold = rule["kind"], rule["threshold"]
    if kind in {"candidate_min", "candidate_max"}:
        observed = value
    else:
        delta = cell["delta"]
        reason = delta["percent_reason"] if kind == "relative_improvement_percent_min" else delta["reason"]
        if reason:
            return "unknown", None, reason
        change = delta["percent"] if kind == "relative_improvement_percent_min" else delta["value"]
        observed = -change if axis["prefer"] == "minimize" else change
        if kind == "degradation_max":
            observed = -observed
    if not finite(observed):
        return "unknown", None, "arithmetic_overflow"
    met = observed <= threshold if kind in {"candidate_max", "degradation_max"} else observed >= threshold
    return "satisfied" if met else "not_satisfied", observed, None


def suggestions(criteria, comparison, candidates):
    """Options grounded in recorded gaps, without assigning direction priority."""
    options = {
        "reuse_baseline": ("先复用原游戏基线", "原游戏基线的结果或所选指标尚不可用。",
            "先查原游戏已登记结果与原件，复用同场景、同采样口径的已有基线；仅补缺失的测量。引用原 result/job，不把双线性候选改成原游戏基线。"),
        "verify_measurement": ("核对结果选择与测量口径", "结果、指标选择、单位或计算口径存在明确缺项或冲突。",
            "先核对原 result、metric index、单位、范围与原始数值。已有证据可复用；若需换选择或规则，显式保存新研究版本，保留旧判据。零基线不计算改善百分比，不把未知补零。"),
        "matched_images": ("补同状态画质对照", "画质数值或图像域、区域、采集口径尚未对齐。",
            "在原游戏与候选的同场景、相机和游戏状态下保存真实渲染截图，明确图像域、ROI及指标方法；允许画质按已声明容忍值下降，需要时另收异步主观评价。"),
        "module_timing": ("补明确范围的耗时测量", "耗时数值或测量方法、窗口尚不足以对照。",
            "固定场景、预热与采样窗口，记录原游戏和候选同一模块的 ms 及分布；主循环、显示和 GPU 提交代理各保留原范围，不能相互替代或直接当作 FPS。"),
        "aligned_power": ("补同窗口功耗与帧计数", "功耗值或采样窗口、供电口径、分母尚缺失或不同。",
            "让原游戏与候选使用同场景、热状态、供电口径和时间窗口，同步保存功耗、时长与完成帧数；只有能量与帧数来自同一窗口时才求每帧能耗，缺测继续记未知。"),
        "asynchronous_assistance": ("必要时发起异步协助", "部分证据仍未取得；若需要人参与，可只阻断依赖它的方向。",
            "若缺项涉及登录、授权、素材收集或主观画质判断，创建现有协助请求并关联所需方向和原证据；其他方向继续。收到回复后由外部 Agent 复核并明确解除依赖，不自动解阻。"),
    }
    grouped = {}
    def add(kind, column_id, axis_id, reason):
        if kind not in grouped:
            title, explanation, plan = options[kind]
            grouped[kind] = {"kind": kind, "title": title, "reason": explanation,
                             "next_experiment": plan, "affected": []}
        evidence = {"column_id": column_id, "axis_id": axis_id, "reason": reason}
        if evidence not in grouped[kind]["affected"]:
            grouped[kind]["affected"].append(evidence)
    rows = {row["id"]: row for row in comparison["rows"]}
    baseline = comparison["baseline_column_id"]
    for index, (column, candidate) in enumerate(zip(comparison["columns"], candidates)):
        cid = column["id"]
        if column["status"] != "available":
            reason = column["error"]["code"]
            add("reuse_baseline" if cid == baseline else "verify_measurement", cid, None, reason)
            add("asynchronous_assistance", cid, None, reason)
            continue
        for axis, evaluated in zip(criteria["axes"], candidate["axes"]):
            aid, reason = axis["id"], evaluated["reason"]
            cell = rows[axis["row_id"]]["cells"][index]
            if reason:
                if cid == baseline and reason == "value_unavailable":
                    add("reuse_baseline", cid, aid, reason)
                if reason != "value_unavailable":
                    add("verify_measurement", cid, aid, reason)
                    continue  # Check the selected evidence before proposing another capture.
                add("asynchronous_assistance", cid, aid, reason)
            scope = axis["scope"]
            kind = "matched_images" if scope == "image" else "aligned_power" if scope == "power" else "module_timing"
            fields = {"method", "image_domain", "region"} if scope == "image" else {"method", "window"}
            if scope == "power":
                fields |= {"power_rail", "denominator"}
            gaps = [f'{item["field"]}:{item["relation"]}' for item in cell["descriptor_relation"]["fields"]
                    if item["field"] in fields and item["relation"] != "match"]
            for gap in ([reason] if reason else []) + gaps:
                add(kind, cid, aid, gap)
    return list(grouped.values())


def assess(criteria, comparison):
    """No statistical claim, state mutation, inferred axes or missing-value imputation."""
    axes, selected = criteria["axes"], criteria["pareto_axes"]
    rows = {row["id"]: row for row in comparison["rows"]}
    candidates, points = [], {}
    for index, column in enumerate(comparison["columns"]):
        evaluated, values = [], {}
        for axis in axes:
            cell = rows[axis["row_id"]]["cells"][index]
            value, unit, reason = _value(axis, cell)
            values[axis["id"]] = (value, reason)
            status, observed, rule_reason = _rule(axis, cell, value, reason)
            evaluated.append({"axis_id": axis["id"], "row_id": axis["row_id"], "value": value, "unit": unit,
                              "rule": axis["rule"], "status": status, "reason": rule_reason, "observed": observed})
        required = [axis["id"] for axis in axes if axis["required"]]
        failed = [item["axis_id"] for item in evaluated if item["axis_id"] in required and item["status"] == "not_satisfied"]
        unknown = [item["axis_id"] for item in evaluated if item["axis_id"] in required and item["status"] == "unknown"]
        status = "not_configured" if not required else "not_satisfied" if failed else "undetermined" if unknown else "satisfied"
        candidates.append({"column_id": column["id"], "axes": evaluated,
                           "requirements": {"status": status, "required_axes": required, "unknown_axes": unknown, "failed_axes": failed}})
        points[column["id"]] = values
    preferences = {axis["id"]: axis["prefer"] for axis in axes}
    complete = [cid for cid, values in points.items() if selected and all(values[aid][1] is None for aid in selected)]
    def dominates(a, b):
        relations = [(points[a][aid][0] <= points[b][aid][0], points[a][aid][0] < points[b][aid][0])
                     if preferences[aid] == "minimize" else
                     (points[a][aid][0] >= points[b][aid][0], points[a][aid][0] > points[b][aid][0]) for aid in selected]
        return all(item[0] for item in relations) and any(item[1] for item in relations)
    frontier = []
    for cid, values in points.items():
        reasons = [{"axis_id": aid, "reason": values[aid][1]} for aid in selected if values[aid][1] is not None]
        by = [other for other in complete if other != cid and dominates(other, cid)] if cid in complete else []
        status = "not_requested" if not selected else "undetermined" if reasons else "dominated" if by else "non_dominated"
        frontier.append({"column_id": cid, "status": status, "dominated_by": by, "reasons": reasons})
    return {"basis": "registered_point_values", "comparability": "not_established", "comparison": comparison,
            "candidates": candidates, "suggestions": suggestions(criteria, comparison, candidates),
            "pareto": {"status": "computed" if selected else "not_requested", "axis_ids": selected,
             "scope": "complete_value_subset", "excluded_axis_ids": [axis["id"] for axis in axes if axis["id"] not in selected],
             "undetermined_column_ids": [item["column_id"] for item in frontier if item["status"] == "undetermined"],
             "columns": frontier}}
