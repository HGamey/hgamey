"""Bounded, explicit reads of registered result indexes; originals stay at source."""
import http.client
import math
import queue
import re
import threading
import time
import json
from urllib.parse import urlencode, quote

from node_directory import DirectoryError, _ProbeError, _name, DEADLINE_S
from node_results import identifier, result_query, OUTCOMES, LABELS


class NodeResultSearch:
    def __init__(self, directory, node_id):
        self.directory, self.node_id = directory, node_id
        # A stalled DNS call retains its slot. There is no unbounded executor queue.
        self._slots = threading.BoundedSemaphore(4)

    @staticmethod
    def _targets(query):
        values = query.get("targets")
        if not values or len(values) != 1 or len(values[0]) > 4096:
            raise ValueError("targets must be one JSON array of 1 to 4 frozen directory selections")
        targets = json.loads(values[0])
        if not isinstance(targets, list) or not 1 <= len(targets) <= 4:
            raise ValueError("select 1 to 4 remote nodes explicitly")
        names, identities = set(), set()
        for target in targets:
            if not isinstance(target, dict) or set(target) != {"name", "node_id", "revision"}:
                raise ValueError("each target requires name, node_id and revision")
            _name(target["name"])
            nid, revision = target["node_id"], target["revision"]
            if not isinstance(nid, str) or not re.fullmatch(r"[0-9a-f]{32}", nid):
                raise ValueError("select an already bound source node identity")
            if type(revision) is not int or revision < 1:
                raise ValueError("source configuration revision must be positive")
            if target["name"] in names or nid in identities:
                raise ValueError("each selected name and source identity must be distinct")
            names.add(target["name"])
            identities.add(nid)
        return targets

    @staticmethod
    def _failed(row, status, message):
        return {**row, "status": status, "read_at": None,
                "error": {"code": status, "message": message}, "items": None, "next_cursor": None}

    @staticmethod
    def _items(page, source, limit):
        if page.get("node_id") != source["node_id"]:
            raise _ProbeError("identity_mismatch", "source index belongs to a different node")
        items, cursor = page.get("items"), page.get("next_cursor")
        if not isinstance(items, list) or len(items) > limit:
            raise ValueError("invalid source result page")
        if cursor is not None and (not isinstance(cursor, str) or not cursor or len(cursor) > 2048):
            raise ValueError("invalid source cursor")
        output = []
        seen = set()
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("invalid source summary")
            rid = identifier(item.get("result_id"))
            if rid in seen:
                raise ValueError("duplicate source result")
            seen.add(rid)
            labels, observed = item.get("labels"), item.get("registered_at")
            if (not isinstance(item.get("title"), str) or not 1 <= len(item["title"]) <= 240
                    or item.get("outcome") not in OUTCOMES or not isinstance(labels, dict)
                    or any(labels.get(key) is not None and not isinstance(labels[key], str) for key in LABELS)
                    or type(observed) not in (int, float) or not math.isfinite(observed)
                    or any(type(item.get(key)) is not int or item[key] < 0 for key in ("source_count", "metric_count"))):
                raise ValueError("invalid source summary fields")
            value = {key: item.get(key) for key in ("result_id", "title", "job_id", "labels", "outcome",
                     "registered_at", "provenance", "metric_count", "source_count")}
            value["detail_url"] = (source["url"] + "/dash/results?" + urlencode({"expected_node_id": source["node_id"]})
                                   + "#result=" + quote(rid, safe=""))
            output.append(value)
        return output, cursor

    def _read(self, entry, row, filters, limit, deadline):
        source = row["source"]
        nid = source["node_id"]
        identity = self.directory._request(entry, "/node", deadline, expected_node_id=nid)
        if identity.get("node_id") != nid:
            raise _ProbeError("identity_mismatch", "source identity differs from the selected directory node")
        caps = identity.get("capabilities")
        if not isinstance(caps, list) or not {"results.index-v1", "node.target-v1"} <= set(caps):
            raise _ProbeError("unsupported", "source lacks explicit result-index or target-binding support")
        params = {**filters, "limit": limit}
        if row["requested_cursor"] is not None:
            params["cursor"] = row["requested_cursor"]
        page = self.directory._request(entry, "/results?" + urlencode(params), deadline, expected_node_id=nid)
        items, cursor = self._items(page, source, limit)
        # Config changes during IO invalidate the whole page. Never switch URLs or retry.
        self.directory.read_target(source["name"], nid, source["revision"])
        return {**row, "status": "ok", "read_at": time.time(), "error": None,
                "items": items, "next_cursor": cursor}

    def _worker(self, entry, row, filters, limit, deadline, output, index):
        try:
            try:
                result = self._read(entry, row, filters, limit, deadline)
            except _ProbeError as error:
                result = self._failed(row, "unsupported" if error.status == "incompatible" else error.status, error.message)
            except DirectoryError:
                result = self._failed(row, "configuration_changed", "source configuration could no longer be confirmed")
            except (OSError, http.client.HTTPException):
                result = self._failed(row, "unavailable", "source could not be read before the deadline")
            except (ValueError, TypeError, KeyError, OverflowError):
                result = self._failed(row, "unsupported", "source returned an incompatible result index")
            if time.monotonic() <= deadline:
                output.put((index, result))
        finally:
            self._slots.release()

    def search(self, query):
        targets = self._targets(query)
        filters, limit, cursor = result_query({key: val for key, val in query.items() if key != "targets"})
        if cursor is not None and len(targets) != 1:
            raise ValueError("cursor continuation requires exactly one frozen source target")
        deadline, attempted = time.monotonic() + DEADLINE_S, time.time()
        output, rows, pending = queue.Queue(), [], set()
        for index, target in enumerate(targets):
            row = {"source": {**target, "url": None}, "attempted_at": attempted, "requested_cursor": cursor}
            rows.append(self._failed(row, "unavailable", "source did not finish before the query deadline"))
            try:
                entry = self.directory.read_target(target["name"], target["node_id"], target["revision"])
            except DirectoryError as error:
                rows[index] = self._failed(row, "unknown_node" if error.status == 404 else "unavailable",
                                           "selected source registration is unavailable")
                continue
            except _ProbeError as error:
                rows[index] = self._failed(row, error.status, error.message)
                continue
            row["source"]["url"] = entry["url"]
            if not self._slots.acquire(blocking=False):
                rows[index] = self._failed(row, "busy", "remote result readers are busy; retry this source explicitly")
                continue
            pending.add(index)
            worker = threading.Thread(target=self._worker, args=(entry, row, filters, limit, deadline, output, index),
                                      name="result-index-read", daemon=True)
            try:
                worker.start()
            except RuntimeError:
                self._slots.release()
                pending.remove(index)
                rows[index] = self._failed(row, "busy", "remote result reader could not be started")
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                index, row = output.get(timeout=remaining)
            except queue.Empty:
                break
            rows[index] = row
            pending.remove(index)
        return {"schema_version": 1, "node_id": self.node_id, "filters": filters, "limit_per_node": limit,
                "partial": any(row["status"] != "ok" for row in rows), "nodes": rows}
