"""A dataset's sample pictures for the 数据集 page, taken from files it already has, so agents owe nothing new: a
low-res / high-res pair when there is one, else one picture. Only files on this center's disk are used: nothing is
fetched from another computer, and a dataset with none shows no sample.

Where they come from, in order:
1. the dataset's registered picture sources: a pair named as one (lr/low/低清 and hr/high/gt/native/高清), else the first;
2. the folder of a registered frame manifest (…/manifest.jsonl): its lr/ and hr/ folders when it has both, else its
   first picture. A simulated pairing keeps only the high-res frames, so it shows that one."""
import os
import re
from urllib.parse import quote, urlencode

PICTURE = re.compile(r".+\.(png|jpe?g|webp)\Z", re.I)
LOW = re.compile(r"(?:^|[^a-z0-9])(?:lr|low|lowres|低清)(?:[^a-z0-9]|$)", re.I)
HIGH = re.compile(r"(?:^|[^a-z0-9])(?:hr|high|highres|gt|native|高清|原生)(?:[^a-z0-9]|$)", re.I)
LISTED = 200   # entries looked at in a frame folder


def _output(jobs, jid, rel):
    """A job's output file on this center's disk, by its path in the job's outputs (None when it is not here)."""
    path = jobs.path(jid, "out", *rel.split("/"))
    return path if os.path.isfile(path) and not os.path.islink(path) else None


def _output_url(jid, rel):
    return f"/dash/api/jobs/{quote(jid)}/out?" + urlencode({"path": rel})


def _first_picture(folder):
    try:
        names = sorted(os.listdir(folder))[:LISTED]
    except OSError:
        return None
    return next((name for name in names if PICTURE.match(name) and os.path.isfile(os.path.join(folder, name))), None)


def _registered(jobs, source):
    """(name, url) of a registered picture source this center holds, else None."""
    if source["kind"] == "job_output" and PICTURE.match(source.get("path") or ""):
        return (source["path"], _output_url(source["job_id"], source["path"])) if _output(jobs, source["job_id"], source["path"]) else None
    if source["kind"] == "attachment" and PICTURE.match(source.get("filename") or source["name"]):
        held = os.path.isfile(os.path.join(jobs.data_root, "_research", "attachments", source["attachment_id"]))
        return (source.get("filename") or source["name"], "/dash/api/research-attachments/" + quote(source["attachment_id"])) if held else None
    return None


def samples(jobs, rid):
    """[{label, url}] for result `rid`; url is a dashboard API path (the page adds the target binding)."""
    manifest = jobs.results.manifest(rid)
    sources = manifest["sources"]
    named = [found for found in (_registered(jobs, s) for s in sources) if found]
    if named:
        low = next((url for name, url in named if LOW.search(name)), None)
        high = next((url for name, url in named if HIGH.search(name) and url != low), None)
        if low and high:
            return [{"label": "低清", "url": low}, {"label": "高清", "url": high}]
        return [{"label": "样例", "url": named[0][1]}]
    for source in sources:
        path = source.get("path") or ""
        if source["kind"] != "job_output" or os.path.basename(path) != "manifest.jsonl":
            continue
        directory = os.path.dirname(path)
        folder = os.path.dirname(jobs.path(source["job_id"], "out", *path.split("/")))
        if not os.path.isdir(folder):   # on another computer and never brought home
            continue
        join = lambda *parts: "/".join(p for p in parts if p)   # noqa: E731
        split = {}
        for side, names in (("低清", ("lr", "low")), ("高清", ("hr", "high"))):
            found = next((join(directory, name, picture) for name in names
                          for picture in [_first_picture(os.path.join(folder, name))] if picture), None)
            if found:
                split[side] = found
        if len(split) == 2:
            return [{"label": side, "url": _output_url(source["job_id"], split[side])} for side in ("低清", "高清")]
        found = _first_picture(folder)
        if found:
            simulated = (manifest.get("dataset") or {}).get("pairing") == "simulated"
            return [{"label": "高清原图" if simulated else "样例", "url": _output_url(source["job_id"], join(directory, found))}]
    return []
