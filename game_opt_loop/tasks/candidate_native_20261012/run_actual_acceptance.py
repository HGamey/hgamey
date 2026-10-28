"""Explicit actual GOL/PF CLI evidence with synthetic SDK. Zero real NDK/GPU/device/API."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time


def identity(data):
    return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def call(archive, label, argv, session):
    begin = time.monotonic()
    p = subprocess.Popen(list(map(str, argv)), stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, start_new_session=True)
    try:
        out, err = p.communicate(timeout=240)
    except subprocess.TimeoutExpired:
        journal = session / "journal.json"
        if journal.is_file():
            for stage in json.loads(journal.read_bytes())["stages"]:
                if stage["state"] == "IN_FLIGHT" and stage.get("pid"):
                    try:
                        os.killpg(stage["pid"], 9)
                    except ProcessLookupError:
                        pass
        os.killpg(p.pid, 9)
        p.communicate(timeout=5)
        raise RuntimeError("owned evidence watchdog expired; no acceptance")
    (archive / (label + ".stdout.json")).write_bytes(out)
    (archive / (label + ".stderr.txt")).write_bytes(err)
    (archive / (label + ".receipt.json")).write_text(json.dumps({
        "argv": list(map(str, argv)), "exit_code": p.returncode,
        "wall_seconds": time.monotonic() - begin, "stdout": identity(out), "stderr": identity(err)
    }, indent=2) + "\n")
    return p.returncode, json.loads(out)


def main():
    p = argparse.ArgumentParser()
    for arg in ["gol", "pf", "layer", "legacy-pf", "legacy-layer", "source", "archive"]:
        p.add_argument("--" + arg, type=Path, required=True)
    a = p.parse_args()
    a.archive.mkdir()  # Never overwrite an earlier evidence archive.
    root = Path(tempfile.mkdtemp(prefix="gol-native-actual-", dir="/private/tmp"))
    fixture = a.source / "tasks/candidate_compilation_20261011"
    driver = re.search(r'const DRIVER: &str = r#"(.*?)"#;',
                       (a.source / "tests/candidate_native_cli.rs").read_text(), re.S).group(1)
    cases = []
    for label, pf, layer in [("actual-ownership5", a.pf, a.layer),
                              ("actual-legacy4", a.legacy_pf, a.legacy_layer),
                              ("actual-missing-sdk", a.pf, a.layer)]:
        owned = root / label
        owned.mkdir()
        archive = a.archive / label
        archive.mkdir()
        candidate = owned / "candidate.json"
        shutil.copyfile(fixture / "sr-candidate.json", candidate)
        sdk = owned / "sdk"
        if label != "actual-missing-sdk":
            sdk.mkdir()
            shutil.copyfile(fixture / "adapted/storage-01e67a5e92937da8b0369f19bbc6a38bea0be40e8c7f820af75883e60fc595a1/adapted.spv", sdk / "copy-fixture.spv")
            for relative, kind in [
                ("shader-tools/linux-x86_64/glslc", "shader"),
                ("toolchains/llvm/prebuilt/linux-x86_64/bin/aarch64-linux-android30-clang++", "native"),
                ("toolchains/llvm/prebuilt/linux-x86_64/bin/llvm-nm", "nm"),
                ("toolchains/llvm/prebuilt/linux-x86_64/bin/llvm-readelf", "readelf")]:
                tool = sdk / relative
                tool.parent.mkdir(parents=True, exist_ok=True)
                tool.write_text("#!/usr/bin/python3\nROOT=" + repr(str(owned)) +
                                "\nBEHAVIOR='ok'\nKIND=" + repr(kind) + "\n" + driver)
                tool.chmod(0o700)
        session = owned / "session"
        argv = [a.gol, "candidate-prepare", "run", "--candidate", candidate,
                "--phonefarm-bin", pf, "--out", session, "--synthetic", "--build",
                "--ndk", sdk, "--native-layer-input-root", layer]
        code, result = call(archive, "run", argv, session)
        negative = label == "actual-missing-sdk"
        assert code == (1 if negative else 0), result
        assert result["status"] == ("BLOCKED" if negative else "PREPARED_OFFLINE_NATIVE")
        assert result["offline_compilation_adapter_complete"] is True
        assert result["preparation_chain_complete"] is (not negative)
        assert all(v is False for v in result["proof_flags"].values())
        assert result["provenance"] == "synthetic_mechanism_fixture"
        inspected, _ = call(archive, "inspect", [a.gol, "candidate-prepare", "inspect", "--session", session], session)
        assert inspected == code
        journal = json.loads((session / "journal.json").read_bytes())
        if not negative:
            assert result["native_plan"]["layer_contract"] == ("ownership5" if label.endswith("5") else "legacy4")
            assert result["native_binding"]["android_codegen_validated"] is False
            assert result["native_binding"]["deployable"] is False
            assert all(s["state"] == "COMPLETED" and s["group_absent"] is True for s in journal["stages"])
            records = [json.loads(s) for s in (owned / "tool-groups.jsonl").read_text().splitlines()]
            assert len(records) == 8 and all(s["pgid"] == journal["stages"][4]["pid"] for s in records)
            shutil.copyfile(owned / "tool-groups.jsonl", archive / "tool-groups.jsonl")
        else:
            assert journal["stages"][4]["state"] == "BLOCKED"
            assert journal["stages"][5]["state"] == "NOT_RUN"
            probe = journal["stages"][4]["result"]
            assert probe["actual_ndk"] != probe["requested_ndk"]
            assert journal["stages"][4]["argv"][journal["stages"][4]["argv"].index("--ndk") + 1] == probe["actual_ndk"]
        # Omit large private executable copies. This is an archive, not a portable inspectable session.
        for path in session.rglob("*"):
            if path.is_file() and path.name not in ["gol.bin", "phonefarm.bin"]:
                dest = archive / "session" / path.relative_to(session)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(path.read_bytes())
        cases.append({"case": label, "live_session": str(session), "exit_code": code,
                      "inspect_exit_code": inspected, "status": result["status"],
                      "gol": journal["inputs"]["gol"], "phonefarm": journal["inputs"]["phonefarm"],
                      "layer_contract": result["native_plan"]["layer_contract"],
                      "proof_flags": result["proof_flags"]})
    summary = {"cases": cases, "scope": "actual local GOL/PF commands; controlled synthetic SDK only",
               "real_ndk_execution": "NOT_RUN", "GPU_device_API_calls": 0, "SDK_downloads": 0}
    (a.archive / "actual-cli-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
