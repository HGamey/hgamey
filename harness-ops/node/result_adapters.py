"""Small, explicit report projections; the imported JSON remains the evidence."""

import math
import re


SCHEMAS = {
    "doom-lr4-026-v1": "hgamey/lr4-026-original-native-owner-result@1",
    "gol-optimize-v1": "game_opt_loop/optimize_report@1",
}
LABELS = ("game_id", "game_version", "device", "research_id", "direction_id")
GOL_METRICS = {
    "operator_latency_ms": ("ms", "module"),
    "fps_p95_ms": ("ms", "unknown"),
    "frame_p95_ms": ("ms", "unknown"),
    "fps_mean": ("fps", "unknown"),
    "power_watt": ("W", "power"),
    "psnr_db": ("dB", "image"),
    "hud_ghost_db": ("dB", "image"),
    "stretch_pct": ("%", "image"),
    "soc_temp_max_c": ("C", "unknown"),
}


def _object(value, field):
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _array(value, field):
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    return value


def _text(value, field):
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{field} must be text or null")
    return value


def _strings(value, field):
    values = _array(value, field)
    if any(not isinstance(item, str) for item in values):
        raise ValueError(f"{field} must contain text")
    return values


def _pointer(value):
    return value.replace("~", "~0").replace("/", "~1")


def _metric(name, condition, value, pointer, *, unit=None, scope="unknown",
            window=None, power_rail=None, image_domain=None, region=None,
            unavailable_reason=None):
    try:
        finite = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        finite = False
    if value is not None and not finite:
        raise ValueError(f"{pointer} must be a finite number or null")
    return {"name": name, "condition": condition, "value": value, "unit": unit,
            "scope": scope, "method": "reported", "window": window, "denominator": None,
            "power_rail": power_rail, "image_domain": image_domain, "region": region,
            "source": {"name": "report", "pointer": pointer},
            "unavailable_reason": (unavailable_reason or "Source reports null; no value is inferred")
            if value is None else None}


def _base(import_id, result_id, title, labels, producer, version=None):
    return {"schema": "phonefarm/result@1", "result_id": result_id, "title": title[:240],
            "job_id": None, "labels": dict(labels),
            "origin": {"producer": producer, "producer_version": version,
                       "evidence_origin": "unknown", "observed_at": None},
            "outcome": "unknown", "source_outcome": None,
            "sources": [{"name": "report", "kind": "imported_json", "import_id": import_id}],
            "metrics": [], "claims": [], "limitations": [
                "Imported producer declarations; matching JSON values does not verify measurement qualification.",
            ], "supersedes": None}


def _doom(import_id, document, labels):
    rows = _array(document.get("primary_display_summary_derived_from_equal_trial_means"),
                  "primary_display_summary_derived_from_equal_trial_means")
    images = _object(document.get("image_summary"), "image_summary")
    result = _base(import_id, import_id, "Doom LR4-026 fixed-scene module comparison", labels,
                   "hgamey/lr4-026")
    status = _text(document.get("status"), "status")
    result["source_outcome"] = status
    if status == "FIXED_SCENE_TWO_INDEPENDENT_PAIRS_MODULE_MS_GAIN_OBSERVED_OWNED016_RESTORED":
        result["outcome"] = "gain"
    limits = _strings(document.get("limits", []), "limits")
    result["limitations"] += limits
    result["limitations"] += [
        "Gain, if reported, is limited to producer-reported module ms in this fixed scene; not display FPS or adoption.",
        "CPU and GPU elapsed values must not be added; GPU elapsed includes command/submission gaps, LR4 passes and native HUD, not total GPU busy or compositor GPU.",
        "ROI geometry and reference-image identity are not bound here; PSNR does not establish cross-scene quality.",
        "External paths, job IDs and hashes are historical declarations, not current-node associations or verified originals.",
        "RESTORED in the source status does not establish current device, lease or whole-research restoration.",
    ]
    for key in ("scope", "at_utc"):
        value = _text(document.get(key), key)
        if value is not None:
            result["claims"].append(f"/{key}: {value}")
    if "adopt" in document:
        if not isinstance(document["adopt"], bool):
            raise ValueError("adopt must be boolean")
        result["claims"].append("/adopt: " + str(document["adopt"]).lower())
    result["claims"].append("Primary and repaired-native pair details remain separate at /primary_two_pairs and /common_repaired_native026_two_pairs; no statistics are recomputed.")
    # This source's first limit describes its measurement window. Never invent a
    # window for a reduced report that omits the declaration.
    window = limits[0] if limits else None
    columns = (("original016_equal_trial_mean_ms", "baseline", "ms"),
               ("trained026_equal_trial_mean_ms", "candidate", "ms"),
               ("change_percent", "reduction", "%"))
    for index, row in enumerate(rows):
        row = _object(row, f"summary/{index}")
        name = _text(row.get("module"), f"summary/{index}/module")
        if not name:
            raise ValueError("summary module is required")
        for field, condition, unit in columns:
            if field in row:
                result["metrics"].append(_metric(name, condition, row[field],
                    f"/primary_display_summary_derived_from_equal_trial_means/{index}/{field}",
                    unit=unit, scope="module", window=window))
    result["claims"].append("baseline=original016; candidate=trained026. reduction preserves the original signed change_percent, without sign reversal or recalculation.")
    domain = _text(images.get("screenshot_domain"), "image_summary/screenshot_domain")
    image_scope = _text(images.get("scope"), "image_summary/scope")
    if image_scope is not None:
        result["limitations"].append(image_scope)
    for condition in ("trained_model", "zero_mechanism"):
        for suffix, region in (("full_PSNR_dB", "full_frame"), ("fixed_ROI_PSNR_dB", "roi")):
            field = f"{condition}_{suffix}"
            if field in images:
                result["metrics"].append(_metric("psnr", condition, images[field],
                    f"/image_summary/{field}", unit="dB", scope="image",
                    image_domain=domain, region=region))
    # The source does not freeze whether each gain is an absolute change or a ratio.
    for field, scope in (("glass_FPS_gain", "display"), ("power_W_gain", "power"),
                         ("energy_J_per_software_present_gain", "power")):
        if field in document:
            result["metrics"].append(_metric(field, "reduction", document[field], f"/{field}",
                scope=scope, unavailable_reason="Unmeasured in the source; no FPS, power or energy gain is inferred"))
    for key in ("primary_statistics", "method", "fixed_seven_inputs", "seven_arm_actual_qualification",
                "exact_trained_build", "single_training_result", "data_source_24",
                "actual_image_comparison", "rollback", "power_reuse"):
        if key not in document:
            continue
        value = _object(document[key], key)
        locator = _text(value.get("path"), key + "/path")
        if locator is not None:
            source = {"name": key, "kind": "external_reference", "locator": locator}
            if "bytes" in value:
                source["declared_size"] = value["bytes"]
            if "sha256" in value:
                source["declared_sha256"] = value["sha256"]
            result["sources"].append(source)
    for key in ("primary_statistics_job", "actual_image_comparison_job"):
        value = _text(document.get(key), key)
        if value is not None:
            result["sources"].append({"name": key, "kind": "external_reference",
                                      "locator": "historical-job:" + value})
    return [result]


def _unavailable(metrics, field):
    reasons = []
    for item in _array(metrics.get("unavailable") or [], "metrics/unavailable"):
        item = _object(item, "metrics/unavailable entry")
        if item.get("field") in (field, "*"):
            reason = _text(item.get("reason"), "metrics/unavailable/reason")
            if reason:
                reasons.append(reason)
    return "; ".join(reasons) or None


def _gol(import_id, document, labels):
    archive = _array(document.get("archive"), "archive")
    layers = _array(document.get("layers"), "layers")
    producer = _text(document.get("tool"), "tool") or "game_opt_loop"
    version = _text(document.get("tool_version"), "tool_version")
    title = _text(document.get("game"), "game") or "GOL optimize report"
    parent = _base(import_id, import_id, title, labels, producer, version)
    if document.get("plan_only") is True:
        parent["outcome"] = "not_run"
    for key in ("headline", "disclaimer", "generated_at", "game", "package", "serial", "device_model"):
        value = _text(document.get(key), key)
        if value is not None:
            parent["claims"].append(f"/{key}: {value}")
    parent["claims"].append("All archive entries are indexed in original order, including unkept and unknown entries. /plan, /campaign and /proposer remain in the imported source.")
    for index, layer in enumerate(layers):
        layer = _object(layer, f"layers/{index}")
        for field in ("eval_errors", "rejected_locally", "blockers"):
            for message in _strings(layer.get(field, []), f"layers/{index}/{field}"):
                parent["limitations"].append(f"/layers/{index}/{field}: {message}")
        for field in ("reason", "llm_error"):
            value = _text(layer.get(field), f"layers/{index}/{field}")
            if value is not None:
                parent["limitations"].append(f"/layers/{index}/{field}: {value}")
    results = [parent]
    for index, entry in enumerate(archive):
        prefix = f"/archive/{index}"
        entry = _object(entry, prefix)
        candidate = _text(entry.get("id"), prefix + "/id")
        status = _text(entry.get("status"), prefix + "/status")
        if not candidate or not status:
            raise ValueError("each archive entry requires id and status")
        metrics = _object(entry.get("metrics"), prefix + "/metrics")
        verdict = _object(entry.get("verdict"), prefix + "/verdict")
        result = _base(import_id, f"{import_id}.{index}", f"{title}: {candidate}", labels,
                       producer, version)
        result["source_outcome"] = status
        if status in ("CRASH", "FAILED"):
            result["outcome"] = "failed"
        elif (status == "PASS" and entry.get("measurement_valid") is True
              and verdict.get("is_pareto_improvement") is True
              and entry.get("qualification_state") != "ineligible"):
            result["outcome"] = "gain"
        result["claims"] += [
            f"{prefix}/id: {candidate}",
            f"Original candidate, verdict, qualification and all evidence extensions are retained at {prefix}; missing qualification is unknown.",
        ]
        for field in ("qualification_state", "device", "why"):
            value = _text(entry.get(field), prefix + "/" + field)
            if value is not None:
                result["claims"].append(f"{prefix}/{field}: {value}")
        for field in ("measurement_valid", "kept"):
            if field in entry:
                if not isinstance(entry[field], bool):
                    raise ValueError(f"{prefix}/{field} must be boolean")
                result["claims"].append(f"{prefix}/{field}: " + str(entry[field]).lower())
        result["limitations"] += [
            "Entry does not retain receiver source, phonefarm_version or obtained_via; device origin is unknown despite the top-level disclaimer.",
            "Gain is a producer verdict, not inferred from kept. REJECT/ABORT and missing qualification do not establish no gain.",
            "Summary metrics are not assumed to be candidate means. Frame metric names do not establish display FPS; missing windows, rails, image domains and references remain unknown.",
        ]
        evidence_dir = _text(entry.get("evidence_dir"), prefix + "/evidence_dir")
        if evidence_dir:
            result["sources"].append({"name": "evidence_dir", "kind": "external_reference",
                                      "locator": evidence_dir})
        power = entry.get("power_source")
        rail = power.get("rail") if isinstance(power, dict) else None
        rail = _text(rail, prefix + "/power_source/rail")
        for field, (unit, scope) in GOL_METRICS.items():
            if field in metrics:
                result["metrics"].append(_metric(field, "reported_summary", metrics[field],
                    prefix + "/metrics/" + field, unit=unit, scope=scope,
                    power_rail=rail if scope == "power" else None,
                    region="roi" if field == "hud_ghost_db" else "unknown" if scope == "image" else None,
                    unavailable_reason=_unavailable(metrics, field)))
        details = verdict.get("per_metric")
        if details is not None:
            for name, detail in _object(details, prefix + "/verdict/per_metric").items():
                detail = _object(detail, prefix + "/verdict/per_metric/" + name)
                unit, scope = GOL_METRICS.get(name, (None, "unknown"))
                for field, condition in (("a_mean", "baseline"), ("b_mean", "candidate"),
                                         ("diff_mean", "difference"), ("diff_pct", "reduction")):
                    if field in detail:
                        result["metrics"].append(_metric(name, condition, detail[field],
                            prefix + "/verdict/per_metric/" + _pointer(name) + "/" + field,
                            unit="%" if field == "diff_pct" else unit, scope=scope,
                            power_rail=rail if scope == "power" else None,
                            region="roi" if name == "hud_ghost_db" else "unknown" if scope == "image" else None))
        if (metrics.get("fps_p95_ms") is not None and metrics.get("frame_p95_ms") is not None
                and metrics["fps_p95_ms"] != metrics["frame_p95_ms"]):
            result["limitations"].append("fps_p95_ms and frame_p95_ms aliases disagree; both source values are retained without combining them.")
        if isinstance(power, dict) and power.get("on_battery") is False and metrics.get("power_watt") is not None:
            result["limitations"].append("Source reports on_battery=false with a power value; whole-device battery power is not established.")
        results.append(result)
    return results


def convert(adapter, import_id, document, labels):
    """Project a recognized snapshot, without reading paths or recomputing statistics."""
    if adapter not in SCHEMAS:
        raise ValueError("unsupported result adapter")
    if not isinstance(import_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,48}", import_id):
        raise ValueError("import_id must contain 1–48 letters, digits, dots, underscores or hyphens")
    document = _object(document, "report")
    if document.get("schema") != SCHEMAS[adapter]:
        raise ValueError("report schema does not match the selected adapter")
    labels = _object(labels, "labels")
    if set(labels) - set(LABELS):
        raise ValueError("unsupported result label")
    normalized = {key: _text(labels.get(key), "labels/" + key) for key in LABELS}
    results = (_doom if adapter == "doom-lr4-026-v1" else _gol)(import_id, document, normalized)
    if any(len(item["metrics"]) > 256 or len(item["sources"]) > 64 for item in results):
        raise ValueError("report projection exceeds result metric/source limits; no entries were omitted")
    return results
