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
            "candidates": candidates, "pareto": {"status": "computed" if selected else "not_requested", "axis_ids": selected,
             "scope": "complete_value_subset", "excluded_axis_ids": [axis["id"] for axis in axes if axis["id"] not in selected],
             "undetermined_column_ids": [item["column_id"] for item in frontier if item["status"] == "undetermined"],
             "columns": frontier}}
