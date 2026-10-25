"""Explicit source identities and baseline pairs over bounded result reads."""
import json
import sqlite3
import time
from urllib.parse import quote, urlencode

from node_results import ResultError, object_fields
from node_result_compare import MAX_SPEC, validate_spec, build_rows
from node_result_search import NodeResultSearch

SCHEMA = "phonefarm/result-cross-compare@1"
MAX_RESPONSE = 6 << 20


def validate_cross_spec(spec, node_id):
    try:
        object_fields(spec, {"schema", "node_id", "targets", "columns", "rows", "context"})
        if spec.get("schema") != SCHEMA:
            raise ValueError("unsupported cross-node comparison schema")
        targets = NodeResultSearch._targets({"targets": [json.dumps(spec.get("targets"))]})
        if any(target["node_id"] == node_id for target in targets):
            raise ValueError("remote targets must not use the hub identity")
        columns = spec.get("columns")
        if not isinstance(columns, list):
            raise ValueError("comparison columns must be an array")
        local_columns = []
        for column in columns:
            object_fields(column, {"id", "label", "node_id", "result_id", "baseline_column_id"})
            if "baseline_column_id" not in column:
                raise ValueError("each column requires an explicit baseline_column_id or null")
            local_columns.append({key: column.get(key) for key in ("id", "label", "result_id")})
        local = {"schema": "phonefarm/result-compare@1", "node_id": spec.get("node_id"),
                 "columns": local_columns, "rows": spec.get("rows")}
        if "context" in spec:
            local["context"] = spec["context"]
        validate_spec(local, node_id)
        ids = {column["id"] for column in columns}
        target_ids = {target["node_id"] for target in targets}
        used = set()
        for column in columns:
            source_id, baseline = column.get("node_id"), column["baseline_column_id"]
            if not isinstance(source_id, str) or source_id not in target_ids | {node_id}:
                raise ValueError("each column must name the hub or one frozen remote node")
            if baseline is not None and (not isinstance(baseline, str) or baseline not in ids):
                raise ValueError("baseline_column_id must name a selected column or be null")
            if source_id != node_id:
                used.add(source_id)
        if used != target_ids or not used:
            raise ValueError("select at least one remote column and use every frozen target")
    except ResultError as error:
        if error.status == 409:
            raise ResultError(409, "result_cross_compare_target_mismatch",
                              "comparison belongs to a different hub") from None
        raise ResultError(400, "result_cross_compare_invalid", str(error)) from None
    except (ValueError, TypeError) as error:
        raise ResultError(400, "result_cross_compare_invalid", str(error)) from None
    return spec


def _spec_query(query, node_id):
    if (set(query) - {"spec", "expected_node_id"} or "spec" not in query
            or any(len(values) != 1 for values in query.values())):
        raise ResultError(400, "result_cross_compare_invalid", "comparison requires exactly one spec")
    raw = query["spec"][0]
    if len(raw.encode("utf-8")) > MAX_SPEC:
        raise ResultError(400, "result_cross_compare_invalid", "comparison spec exceeds 16 KiB")
    def invalid_constant(_):
        raise ValueError("non-finite JSON constant")
    try:
        value = json.loads(raw, parse_constant=invalid_constant)
    except (ValueError, RecursionError):
        raise ResultError(400, "result_cross_compare_invalid", "comparison spec is not valid JSON") from None
    return validate_cross_spec(value, node_id)


def cross_compare(results, search, node_id, spec):
    spec = validate_cross_spec(spec, node_id)
    selections = list(dict.fromkeys((column["node_id"], column["result_id"]) for column in spec["columns"]))
    remote = [(nid, rid) for nid, rid in selections if nid != node_id]
    records = search.details(spec["targets"], remote)
    for nid, rid in selections:
        if nid != node_id:
            continue
        attempted = time.time()
        try:
            record, error, status = results.get(rid), None, "ok"
        except ResultError as failure:
            record, error = None, {"code": failure.code, "message": str(failure)}
            status = "not_found" if failure.status == 404 else "unavailable"
        except (OSError, sqlite3.Error):
            record, status = None, "unavailable"
            error = {"code": "result_read_unavailable", "message": "local registered result could not be read"}
        records[(nid, rid)] = {"source": {"name": None, "node_id": node_id, "url": None, "revision": None},
                              "status": status, "error": error, "result": record,
                              "attempted_at": attempted, "read_at": time.time() if record else None}
    targets = {target["node_id"]: target for target in spec["targets"]}
    columns = []
    for requested in spec["columns"]:
        nid, rid = requested["node_id"], requested["result_id"]
        envelope = records[(nid, rid)]
        source = envelope["source"]
        if nid == node_id:
            api = "/results/" + quote(rid, safe="")
            dashboard = "/dash/results?" + urlencode({"expected_node_id": nid}) + "#result=" + quote(rid, safe="")
        else:
            api = "/remote-result?" + urlencode({"target": json.dumps(targets[nid], separators=(",", ":")), "result_id": rid})
            dashboard = ((source["url"] + "/dash/results?" + urlencode({"expected_node_id": nid})
                          + "#result=" + quote(rid, safe="")) if source.get("url") else None)
        columns.append({**requested, "source": source, "source_status": envelope["status"],
                        "attempted_at": envelope["attempted_at"], "read_at": envelope["read_at"],
                        "status": "available" if envelope["status"] == "ok" else "unavailable",
                        "error": envelope["error"], "result": envelope["result"],
                        "links": {"api": api, "dashboard": dashboard}})
    rows = build_rows(columns, spec["rows"], {column["id"]: column["baseline_column_id"] for column in columns})
    response = {"schema_version": 1, "node_id": node_id, "basis": "registered_values",
                "comparability": "not_established", "spec": spec, "columns": columns, "rows": rows,
                "partial": any(column["status"] != "available" for column in columns)}
    if len(json.dumps(response, ensure_ascii=False, allow_nan=False).encode("utf-8")) > MAX_RESPONSE:
        raise ResultError(413, "result_cross_compare_too_large",
                          "comparison exceeds 6 MiB; select fewer columns or metric rows")
    return response


def cross_compare_query(results, search, node_id, query):
    return cross_compare(results, search, node_id, _spec_query(query, node_id))

