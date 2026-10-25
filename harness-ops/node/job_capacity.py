"""Declared host scheduling slots; no hardware discovery or OS enforcement."""
DEFAULT_SLOTS = {"cpu": 1, "gpu": 0, "io": 0}


def normalize_slots(value):
    if not isinstance(value, dict) or set(value) != set(DEFAULT_SLOTS):
        raise ValueError("host_slots requires exactly cpu, gpu and io")
    for name, amount in value.items():
        if type(amount) is not int or not (1 if name == "cpu" else 0) <= amount <= 65535:
            raise ValueError("host_slots requires cpu=1..65535 and gpu/io=0..65535 integers")
    return dict(value)


def requirement(spec):
    return {"slots": dict(spec.get("host_slots", DEFAULT_SLOTS)),
            "source": "explicit" if "host_slots" in spec else "legacy_default"}


def shortages(requested, total, reserved):
    return [{"resource": name, "requested": requested[name], "total": total[name],
             "reserved": reserved[name], "available": max(0, total[name] - reserved[name])}
            for name in DEFAULT_SLOTS if requested[name] > max(0, total[name] - reserved[name])]


class CapacityExceeded(ValueError):
    status = 422

    def __init__(self, requested, total):
        super().__init__("job host slots exceed the configured node capacity")
        self.payload = {"code": "host_capacity_exceeded", "error": str(self),
                        "requested": requested, "total": total,
                        "shortages": shortages(requested, total, dict.fromkeys(DEFAULT_SLOTS, 0))}


def check_fit(spec, config):
    requested, total = requirement(spec)["slots"], config["slots"]
    if any(requested[name] > total[name] for name in DEFAULT_SLOTS):
        raise CapacityExceeded(requested, total)


def held(row):
    return row["state"] in {"claimed", "blocked"} and row.get("host_capacity_release") is None


def snapshot(config, rows):
    reserved = dict.fromkeys(DEFAULT_SLOTS, 0)
    for row in rows:
        if held(row):
            for name, amount in requirement(row["spec"])["slots"].items():
                reserved[name] += amount
    return {**config, "reserved": reserved,
            "available": {n: max(0, config["slots"][n] - reserved[n]) for n in DEFAULT_SLOTS},
            "overcommitted": {n: max(0, reserved[n] - config["slots"][n]) for n in DEFAULT_SLOTS}}


def waiting(spec, capacity, rows):
    requested = requirement(spec)["slots"]
    missing = shortages(requested, capacity["slots"], capacity["reserved"])
    if not missing:
        return None
    names = {item["resource"] for item in missing}
    return {"kind": "capacity", "reason": "request_exceeds_current_total" if any(
                requested[n] > capacity["slots"][n] for n in DEFAULT_SLOTS) else "slots_unavailable",
            "shortages": missing, "blocking_jobs": [row["job_id"] for row in rows if held(row)
                and any(requirement(row["spec"])["slots"][n] for n in names)]}


def job_facts(row):
    release = row.get("host_capacity_release")
    state = ("released" if release else "reserved" if held(row) else
             "historical_done" if row["state"] == "done" else "not_claimed")
    return {**requirement(row["spec"]), "state": state, "release": release}
