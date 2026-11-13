#!/usr/bin/env python3
"""ocr.py — text on a screenshot, with boxes, where ocr.swift (macOS Vision) cannot run: Linux nodes. It prints what
ocr.swift prints, one JSON line per text: {"t": "文字", "b": [x1, y1, x2, y2]} in image pixels from the top left.

    python3 ocr.py <image>      the text on <image>
    python3 ocr.py --check      exit 0 when the engine loads; otherwise 3, with why on stderr
    python3 ocr.py --install    install the engine for this user

The engine is RapidOCR: PaddleOCR's Chinese/English models on onnxruntime, CPU only and offline, its models inside the
package. It is installed with pip --target into this user's own directory — $PF_OCR_DEPS, by default
~/.local/share/phonefarm/ocr-py — so it needs no root and leaves the system Python alone. pip uses its usual index
(PIP_INDEX_URL, pip.conf); when that cannot be reached it tries the Aliyun mirror once."""
import fcntl
import json
import logging
import os
import shutil
import subprocess
import sys

PACKAGES = ["rapidocr==3.10.0", "onnxruntime"]
MIRROR = "https://mirrors.aliyun.com/pypi/simple/"
MIN_SCORE = 0.3   # ocr.swift's confidence floor


def deps():
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.environ.get("PF_OCR_DEPS") or os.path.join(base, "phonefarm", "ocr-py")


def engine():
    sys.path.insert(0, deps())
    from rapidocr import RapidOCR
    logging.getLogger("RapidOCR").setLevel(logging.ERROR)
    return RapidOCR()


def read(engine, path):
    out = engine(path)
    boxes = out.boxes if out.boxes is not None else []
    for box, text, score in zip(boxes, out.txts or (), out.scores or ()):
        text = str(text).strip()
        if not text or float(score) < MIN_SCORE:
            continue
        xs, ys = [float(p[0]) for p in box], [float(p[1]) for p in box]
        yield {"t": text, "b": [round(min(xs)), round(min(ys)), round(max(xs)), round(max(ys))]}


def check():
    try:
        engine()
    except Exception as error:   # missing package, broken wheel, no models: all mean "install it"
        print(f"ocr: 认字引擎 RapidOCR 装不上或读不了 ({type(error).__name__}: {error})", file=sys.stderr)
        return 3
    return 0


def install():
    """pip --target into a side directory, then moved into place: a half-done install is never used."""
    target = deps()
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)   # two onboards starting together install once
        if subprocess.run([sys.executable, os.path.abspath(__file__), "--check"], capture_output=True).returncode == 0:
            return 0
        partial = target + ".partial"
        for index in ([], ["--index-url", MIRROR]):
            shutil.rmtree(partial, ignore_errors=True)
            done = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--no-input",
                                   "--target", partial, *index, *PACKAGES])
            if done.returncode == 0:
                shutil.rmtree(target, ignore_errors=True)
                os.replace(partial, target)
                return 0
        shutil.rmtree(partial, ignore_errors=True)
        return done.returncode or 1


def main(args):
    if args == ["--check"]:
        return check()
    if args == ["--install"]:
        return install()
    if len(args) != 1 or args[0].startswith("--"):
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    if not os.path.isfile(args[0]):
        print("ocr: 读不了图", file=sys.stderr)
        return 1
    try:
        found = list(read(engine(), args[0]))
    except Exception as error:
        print(f"ocr: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    sys.stdout.write("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in found))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
