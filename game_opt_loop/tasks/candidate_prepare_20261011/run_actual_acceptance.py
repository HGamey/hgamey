"""Explicit local GOL/PF CLI evidence; never a device/GPU/native-SDK acceptance."""
import argparse
import json
import pathlib
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--gol-bin", type=pathlib.Path, required=True)
    parser.add_argument("--phonefarm-bin", type=pathlib.Path, required=True)
    parser.add_argument("--candidate", type=pathlib.Path, required=True)
    parser.add_argument("--archive", type=pathlib.Path, required=True)
    args = parser.parse_args()
    base = pathlib.Path(tempfile.mkdtemp(prefix="gol-prepare-actual-", dir="/private/tmp"))
    cases = []
    for name, extra in [
        ("actual-offline", []),
        ("actual-missing-sdk", ["--build", "--ndk", str(base / "caller-missing-sdk")]),
    ]:
        session = base / name
        argv = [str(args.gol_bin), "candidate-prepare", "run", "--candidate",
                str(args.candidate), "--phonefarm-bin", str(args.phonefarm_bin),
                "--out", str(session)] + extra
        archive = args.archive / name
        archive.mkdir()  # Reject existing evidence destinations.
        (archive / "argv.json").write_text(json.dumps(argv, indent=2) + "\n")
        with (archive / "stdout.json").open("wb") as out, (archive / "stderr.log").open("wb") as err:
            code = subprocess.run(argv, stdout=out, stderr=err).returncode
        (archive / "exit.json").write_text(json.dumps({"exit_code": code, "session": str(session)}, indent=2) + "\n")
        result = json.loads((archive / "stdout.json").read_text())
        assert result["offline_compilation_adapter_complete"] is True
        assert result["preparation_chain_complete"] is (not extra)
        assert result["status"] == ("BLOCKED" if extra else "PREPARED_OFFLINE")
        assert all(v is False for v in result["proof_flags"].values())
        assert code == (1 if extra else 0)
        # The journal records the exact executable hashes; omit the large private copies.
        # These copied artifacts are archival evidence, not a portable session to inspect.
        for path in session.rglob("*"):
            if path.is_file() and path.name not in ("gol.bin", "phonefarm.bin"):
                dest = archive / "session" / path.relative_to(session)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(path.read_bytes())
        inspect_argv = [str(args.gol_bin), "candidate-prepare", "inspect", "--session", str(session)]
        with (archive / "inspect.stdout.json").open("wb") as out, (archive / "inspect.stderr.log").open("wb") as err:
            inspect_code = subprocess.run(inspect_argv, stdout=out, stderr=err).returncode
        (archive / "inspect.argv.json").write_text(json.dumps(inspect_argv, indent=2) + "\n")
        (archive / "inspect.exit.json").write_text(json.dumps({"exit_code": inspect_code}, indent=2) + "\n")
        assert inspect_code == code
        journal = json.loads((archive / "session/journal.json").read_text())
        if extra:
            build = journal["stages"][4]
            assert build["result"]["probe_scope"] == "controlled_empty_sdk_not_caller_installation_or_native_build"
            assert build["result"]["actual_ndk"] != build["result"]["requested_ndk"]
            assert build["argv"][build["argv"].index("--ndk") + 1] == build["result"]["actual_ndk"]
        cases.append({
            "case": name, "session": str(session), "exit_code": code,
            "inspect_exit_code": inspect_code, "status": result["status"],
            "gol_sha256": result["gol_executable"]["sha256"],
            "phonefarm_sha256": result["phonefarm_executable"]["sha256"],
            "candidate_frozen_identity": result["candidate_frozen_identity"],
            "adaptation_id": result["adapter_binding"]["adaptation_id"],
            "offline_compilation_adapter_complete": True,
            "preparation_chain_complete": not bool(extra), "proof_flags": result["proof_flags"],
        })
    summary = {"cases": cases, "device_calls": 0, "model_calls": 0, "sdk_downloads": 0,
               "controlled_fake_ndk_positive": False, "real_ndk_execution": "NOT_RUN"}
    (args.archive / "actual-cli-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
