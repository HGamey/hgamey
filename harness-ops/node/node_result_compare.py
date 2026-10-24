"""Explicit arithmetic over registered metrics, without changing evidence or claims."""
import json
import math
import re
import sqlite3
from urllib.parse import quote, urlencode

from node_results import ResultError, identifier, object_fields, text


MAX_SPEC = 16 << 10
DESCRIPTORS = ("name", "condition", "unit", "scope", "method", "window", "denominator",
               "power_rail", "image_domain", "region")


def validate_spec(spec, node_id):
    """Validate without rewriting the author's selection or adding declarations."""
    try:
        object_fields(spec, {"schema", "node_id", "columns", "rows", "context"})
        if spec.get("schema") != "phonefarm/result-compare@1":
            raise ValueError("unsupported comparison schema")
        if not isinstance(spec.get("node_id"), str) or not re.fullmatch(r"[0-9a-f]{32}", spec["node_id"]):
            raise ValueError("comparison requires an explicit node_id")
        columns, rows = spec.get("columns"), spec.get("rows")
        if not isinstance(columns, list) or not 2 <= len(columns) <= 4:
            raise ValueError("select 2 to 4 columns, with the baseline first")
        if not isinstance(rows, list) or not 1 <= len(rows) <= 16:
            raise ValueError("select 1 to 16 metric rows")
        for group, field in ((columns, "result_id"), (rows, "metric_indexes")):
            seen = set()
            for item in group:
                object_fields(item, {"id", "label", field})
                identity = identifier(item.get("id"))
                text(item.get("label"), 128, True)
                if identity in seen:
                    raise ValueError("column and row IDs must be unique within their group")
                seen.add(identity)
        for column in columns:
            identifier(column.get("result_id"))
        for row in rows:
            indexes = row.get("metric_indexes")
            if (not isinstance(indexes, list) or len(indexes) != len(columns)
                    or any(i is not None and (type(i) is not int or not 0 <= i <= 255) for i in indexes)):
                raise ValueError("each row requires one explicit metric index or null per column")
        if "context" in spec:
            context = object_fields(spec["context"], {"scope", "assumptions", "interventions"})
            if "scope" in context:
                text(context["scope"], 4096, True)
            for key in ("assumptions", "interventions"):
                if key in context:
                    if not isinstance(context[key], list) or len(context[key]) > 16:
                        raise ValueError("comparison context lists allow at most 16 declarations")
                    for item in context[key]:
                        text(item, 1024, True)
    except (ValueError, TypeError) as error:
        raise ResultError(400, "result_compare_invalid", str(error)) from None
    if spec["node_id"] != node_id:
        raise ResultError(409, "result_compare_target_mismatch", "comparison belongs to a different node")
    return spec


def _spec_query(query, node_id):
    if (set(query) - {"spec", "expected_node_id"} or "spec" not in query
            or any(len(values) != 1 for values in query.values())):
        raise ResultError(400, "result_compare_invalid", "comparison requires exactly one spec")
    raw = query["spec"][0]
    if len(raw.encode("utf-8")) > MAX_SPEC:
        raise ResultError(400, "result_compare_invalid", "comparison spec exceeds 16 KiB")
    def invalid_constant(_):
        raise ValueError("comparison JSON must not contain non-finite constants")
    try:
        spec = json.loads(raw, parse_constant=invalid_constant)
    except (ValueError, RecursionError):
        raise ResultError(400, "result_compare_invalid", "comparison spec is not valid JSON") from None
    return validate_spec(spec, node_id)


def _cell(column, index):
    status, reason, metric, evidence = "available", None, None, None
    if index is None:
        status = reason = "not_selected"
    elif column["status"] != "available":
        status = reason = "result_unavailable"
    elif index >= len(column["result"]["manifest"]["metrics"]):
        status = reason = "invalid_index"
    else:
        metric = column["result"]["manifest"]["metrics"][index]
        evidence = next((item for item in column["result"]["metric_evidence"] if item["index"] == index), None)
    return {"column_id": column["id"], "metric_index": index, "status": status,
            "reason": reason, "metric": metric, "metric_evidence": evidence}


def _descriptors(baseline, candidate):
    if baseline is None or candidate is None:
        return {"status": "unavailable", "fields": []}
    fields = []
    for field in DESCRIPTORS:
        a, b = baseline.get(field), candidate.get(field)
        relation = ("unrecorded" if a in (None, "unknown") or b in (None, "unknown")
                    else "match" if a == b else "mismatch")
        fields.append({"field": field, "baseline": a, "candidate": b, "relation": relation})
    relations = {item["relation"] for item in fields}
    status = "mismatch" if "mismatch" in relations else "incomplete" if "unrecorded" in relations else "match"
    return {"status": status, "fields": fields}


def _delta(baseline, candidate):
    result = {"value": None, "percent": None, "unit": None, "reason": None, "percent_reason": None,
              "formula": "candidate_minus_baseline", "percent_formula": "(candidate-baseline)/baseline*100",
              "basis": "registered_values"}
    reason = baseline["reason"] or candidate["reason"]
    a, b = baseline["metric"], candidate["metric"]
    if not reason:
        if a["value"] is None or b["value"] is None:
            reason = "value_unavailable"
        elif not a.get("unit") or not b.get("unit"):
            reason = "unit_unavailable"
        elif a["unit"] != b["unit"]:
            reason = "unit_mismatch"
        elif a.get("scope") in (None, "unknown") or b.get("scope") in (None, "unknown"):
            reason = "scope_unavailable"
        elif a["scope"] != b["scope"]:
            reason = "scope_mismatch"
    if reason:
        result.update(reason=reason, percent_reason=reason)
        return result
    result["unit"] = a["unit"]
    try:
        value = b["value"] - a["value"]
        valid = math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid:
        result.update(reason="arithmetic_overflow", percent_reason="arithmetic_overflow")
        return result
    result["value"] = value
    if a["value"] <= 0:
        result["percent_reason"] = "baseline_nonpositive"
    else:
        try:
            percent = value / a["value"] * 100
            valid = math.isfinite(percent)
        except OverflowError:
            valid = False
        if valid:
            result["percent"] = percent
        else:
            result["percent_reason"] = "arithmetic_overflow"
    return result


def compare(results, node_id, spec):
    """At most four distinct result reads; a missing column does not erase its peers."""
    spec = validate_spec(spec, node_id)
    columns, cache = [], {}
    for requested in spec["columns"]:
        rid = requested["result_id"]
        if rid not in cache:
            try:
                cache[rid] = (results.get(rid), None)
            except ResultError as error:
                cache[rid] = (None, {"code": error.code, "message": str(error)})
            except (OSError, sqlite3.Error):
                cache[rid] = (None, {"code": "result_read_unavailable", "message": "registered result could not be read"})
        record, error = cache[rid]
        columns.append({**requested, "node_id": node_id, "status": "available" if record else "unavailable",
                        "error": error, "result": record,
                        "links": {"api": "/results/" + quote(rid, safe=""),
                                  "dashboard": "/dash/results?" + urlencode({"expected_node_id": node_id})
                                               + "#result=" + quote(rid, safe="")}})
    rows = []
    for requested in spec["rows"]:
        cells = [_cell(column, index) for column, index in zip(columns, requested["metric_indexes"])]
        for cell in cells:
            cell["descriptor_relation"] = _descriptors(cells[0]["metric"], cell["metric"])
            cell["delta"] = _delta(cells[0], cell)
        rows.append({**requested, "cells": cells})
    return {"schema_version": 1, "node_id": node_id, "basis": "registered_values",
            "comparability": "not_established", "spec": spec, "baseline_column_id": columns[0]["id"],
            "partial": any(column["status"] == "unavailable" for column in columns), "columns": columns, "rows": rows}


def compare_query(results, node_id, query):
    return compare(results, node_id, _spec_query(query, node_id))
