"""Bounded, explicit reads of registered result indexes; originals stay at source."""
import copy
import http.client
import math
import queue
import re
import threading
import time
import json
from urllib.parse import urlencode, quote

from node_directory import DirectoryError, _ProbeError, _name, DEADLINE_S
from node_results import identifier, manifest, result_query, OUTCOMES, LABELS


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
        return NodeResultSearch._validate_targets(targets)

    @staticmethod
    def _validate_targets(targets, allow_empty=False):
        if not isinstance(targets, list) or not (0 if allow_empty else 1) <= len(targets) <= 4:
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

    @staticmethod
    def _detail_failed(row, status, message):
        return {**row, "status": status, "read_at": None,
                "error": {"code": status, "message": message}, "result": None}

    @staticmethod
    def _detail_error(error):
        if isinstance(error, _ProbeError):
            return ("unsupported" if error.status == "incompatible" else error.status, error.message)
        if isinstance(error, DirectoryError):
            return "configuration_changed", "source configuration could no longer be confirmed"
        if isinstance(error, (OSError, http.client.HTTPException)):
            return "unavailable", "source could not be read before the deadline"
        return "unsupported", "source returned an incompatible result detail"

    @staticmethod
    def _detail_record(value, source, rid):
        """Validate reported metadata without observing or qualifying source bytes."""
        if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
                or value["schema_version"] != 1):
            raise ValueError("invalid result detail protocol")
        if value.get("node_id") != source["node_id"]:
            raise _ProbeError("identity_mismatch", "source result belongs to a different node")
        if value.get("result_id") != rid:
            raise ValueError("source result does not match the requested result")
        original = value.get("manifest")
        checked = manifest(original, internal=True)
        if checked["result_id"] != rid:
            raise ValueError("source manifest belongs to a different result")
        # Registered manifests are already canonical. Do not silently complete
        # missing fields or rewrite the original record while validating it.
        if checked != original:
            raise ValueError("source manifest is not a registered canonical record")
        if (type(value.get("sequence")) is not int or value["sequence"] < 1
                or type(value.get("registered_at")) not in (int, float)
                or not math.isfinite(value["registered_at"]) or value["registered_at"] < 0
                or not isinstance(value.get("provenance"), dict)):
            raise ValueError("invalid result registration identity")
        observations, evidence, current = (value.get(key) for key in
                                            ("source_observations", "metric_evidence", "current"))
        sources, metrics = original["sources"], original["metrics"]
        if (not isinstance(observations, list) or len(observations) != len(sources)
                or not isinstance(evidence, list) or len(evidence) != len(metrics)
                or not isinstance(current, dict) or set(current) != {"job", "sources"}
                or not isinstance(current["sources"], list) or len(current["sources"]) != len(sources)):
            raise ValueError("invalid result evidence or current observation shape")
        nullable_text = lambda item: item is None or isinstance(item, str)
        for declared, observed, now in zip(sources, observations, current["sources"]):
            if (not isinstance(observed, dict) or set(observed) != {"name", "kind", "status", "size", "sha256", "reason"}
                    or observed["name"] != declared["name"] or observed["kind"] != declared["kind"]
                    or observed["status"] not in {"available", "unavailable", "external"}
                    or not nullable_text(observed["reason"])):
                raise ValueError("invalid registered source observation")
            size, digest = observed["size"], observed["sha256"]
            if ((size is not None and (type(size) is not int or size < 0))
                    or (digest is not None and (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)))):
                raise ValueError("invalid registered source size or digest")
            if (not isinstance(now, dict) or set(now) != {"name", "status", "reason", "url"}
                    or now["name"] != declared["name"]
                    or now["status"] not in {"available", "unavailable", "changed", "external"}
                    or not nullable_text(now["reason"])):
                raise ValueError("invalid current source observation")
        for index, (metric, item) in enumerate(zip(metrics, evidence)):
            reference = metric["source"]
            if (not isinstance(item, dict) or set(item) != {"index", "status", "source_name", "pointer", "reason"}
                    or type(item["index"]) is not int or item["index"] != index
                    or item["status"] not in {"matched_source", "unverified"}
                    or item["source_name"] != (reference["name"] if reference else None)
                    or item["pointer"] != (reference["pointer"] if reference else None)
                    or (item["status"] == "matched_source" and reference is None)
                    or not nullable_text(item["reason"])):
                raise ValueError("invalid metric source evidence")
        job = current["job"]
        if original["job_id"] is None:
            if job is not None:
                raise ValueError("unexpected current job binding")
        elif (not isinstance(job, dict) or set(job) != {"job_id", "status", "record", "reason"}
                or job["job_id"] != original["job_id"] or job["status"] not in {"available", "unavailable"}
                or (job["record"] is not None and (not isinstance(job["record"], dict)
                    or job["record"].get("id") != original["job_id"]))
                or not nullable_text(job["reason"])):
            raise ValueError("invalid current job observation")
        # Retain the source's facts, but never use navigation supplied by it.
        result = copy.deepcopy({key: value[key] for key in ("schema_version", "node_id", "result_id", "sequence",
            "registered_at", "manifest", "provenance", "source_observations", "metric_evidence", "current")})
        pin = "?" + urlencode({"expected_node_id": source["node_id"]})
        for item in result["current"]["sources"]:
            item["url"] = (source["url"] + "/results/" + quote(rid, safe="") + "/sources/"
                           + quote(item["name"], safe="") + pin) if item["status"] == "available" else None
        json.dumps(result, allow_nan=False)  # Reject non-finite values outside metrics too.
        return result

    def _detail_worker(self, entry, rows, deadline, output, nid):
        failures = (_ProbeError, DirectoryError, OSError, http.client.HTTPException,
                    ValueError, TypeError, KeyError, OverflowError, RecursionError)
        try:
            try:
                source = rows[0]["source"]
                self.directory.read_target(source["name"], nid, source["revision"])
                identity = self.directory._request(entry, "/node", deadline, expected_node_id=nid)
                if identity.get("node_id") != nid:
                    raise _ProbeError("identity_mismatch", "source identity differs from the selected directory node")
                caps = identity.get("capabilities")
                if (not isinstance(caps, list) or not all(isinstance(cap, str) for cap in caps)
                        or not {"results.index-v1", "node.target-v1"} <= set(caps)):
                    raise _ProbeError("unsupported", "source lacks explicit result-index or target-binding support")
                batch = {}
                for row in rows:
                    # Never continue a source batch against a newly selected URL
                    # or credential, even when the preceding result was missing.
                    self.directory.read_target(source["name"], nid, source["revision"])
                    rid = row["result_id"]
                    try:
                        raw = self.directory._request(entry, "/results/" + quote(rid, safe=""), deadline,
                                                      expected_node_id=nid, result_not_found=True)
                        record = self._detail_record(raw, source, rid)
                        batch[(nid, rid)] = {**row, "status": "ok", "read_at": time.time(), "error": None, "result": record}
                    except failures as error:
                        batch[(nid, rid)] = self._detail_failed(row, *self._detail_error(error))
                    self.directory.read_target(source["name"], nid, source["revision"])
                    if time.monotonic() <= deadline:
                        # A later slow result must not erase an earlier completed
                        # detail. Publish a copy; late workers never mutate it.
                        output.put((nid, dict(batch), False))
                self.directory.read_target(source["name"], nid, source["revision"])
            except failures as error:
                batch = {(nid, row["result_id"]): self._detail_failed(row, *self._detail_error(error)) for row in rows}
            if time.monotonic() <= deadline:
                output.put((nid, batch, True))
        finally:
            self._slots.release()

    def details(self, targets, requests):
        """One slot per selected source; at most four distinct metadata reads."""
        targets = self._validate_targets(targets, allow_empty=True)
        if any(target["node_id"] == self.node_id for target in targets):
            raise ValueError("local results must use the local result API")
        if not isinstance(requests, (list, tuple)) or len(requests) > 4:
            raise ValueError("select at most four distinct remote results")
        selected = {target["node_id"]: target for target in targets}
        grouped = {nid: [] for nid in selected}
        seen = set()
        for request in requests:
            if not isinstance(request, (list, tuple)) or len(request) != 2:
                raise ValueError("each result read requires a node and result identity")
            nid, rid = request
            if not isinstance(nid, str) or nid not in selected:
                raise ValueError("result source is not an explicitly selected target")
            identifier(rid)
            if (nid, rid) not in seen:
                seen.add((nid, rid))
                grouped[nid].append(rid)
        if any(not rids for rids in grouped.values()):
            raise ValueError("each selected target must have an explicit result read")
        deadline, attempted = time.monotonic() + DEADLINE_S, time.time()
        output, results, pending = queue.Queue(), {}, set()
        for nid, rids in grouped.items():
            target = selected[nid]
            source = {**target, "url": None}
            rows = [{"schema_version": 1, "node_id": self.node_id, "source": source,
                     "result_id": rid, "attempted_at": attempted} for rid in rids]
            try:
                entry = self.directory.read_target(target["name"], nid, target["revision"])
            except DirectoryError as error:
                status = "unknown_node" if error.status == 404 else "unavailable"
                results.update({(nid, row["result_id"]): self._detail_failed(row, status,
                    "selected source registration is unavailable") for row in rows})
                continue
            except _ProbeError as error:
                results.update({(nid, row["result_id"]): self._detail_failed(row, error.status, error.message) for row in rows})
                continue
            source["url"] = entry["url"]
            results.update({(nid, row["result_id"]): self._detail_failed(row, "unavailable",
                            "source did not finish before the query deadline") for row in rows})
            if not self._slots.acquire(blocking=False):
                results.update({(nid, row["result_id"]): self._detail_failed(row, "busy",
                    "remote result readers are busy; retry this source explicitly") for row in rows})
                continue
            pending.add(nid)
            worker = threading.Thread(target=self._detail_worker, args=(entry, rows, deadline, output, nid),
                                      name="result-detail-read", daemon=True)
            try:
                worker.start()
            except RuntimeError:
                self._slots.release()
                pending.remove(nid)
                results.update({(nid, row["result_id"]): self._detail_failed(row, "busy",
                    "remote result reader could not be started") for row in rows})
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                nid, batch, done = output.get(timeout=remaining)
            except queue.Empty:
                break
            results.update(batch)
            if done:
                pending.remove(nid)
        # A fast source may have finished before another slow source. Confirm its
        # frozen directory version again before assembling the returned envelope.
        for nid, target in selected.items():
            if not any(results[(nid, rid)]["source"]["url"] is not None for rid in grouped[nid]):
                continue
            try:
                self.directory.read_target(target["name"], nid, target["revision"])
            except (DirectoryError, _ProbeError) as error:
                for rid in grouped[nid]:
                    results[(nid, rid)] = self._detail_failed(results[(nid, rid)], *self._detail_error(error))
        return results

    def remote_result(self, query):
        if (set(query) - {"target", "result_id", "expected_node_id"}
                or any(len(values) != 1 for values in query.values())
                or "target" not in query or "result_id" not in query):
            raise ValueError("remote detail requires one frozen target and result_id")
        raw = query["target"][0]
        if len(raw.encode("utf-8")) > 4096:
            raise ValueError("remote result target exceeds 4 KiB")
        try:
            target = json.loads(raw)
        except (ValueError, RecursionError):
            raise ValueError("remote result target must be a JSON object") from None
        self._validate_targets([target])
        rid = identifier(query["result_id"][0])
        return self.details([target], [(target["node_id"], rid)])[(target["node_id"], rid)]

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
