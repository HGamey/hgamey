#!/usr/bin/env python3
"""出方案一方的离线评测: 不上真机, 只看它出的候选「能不能上机、是不是在炒冷饭、守门怎么看」。

    python3 tools/proposer_eval.py --proposer tools/proposers/pi_kimi.py --runs 3 \
        --input a.input.json [--input b.input.json ...] [--jev] [--out runs/proposer_eval]

对每份 proposer@1 输入, 把出方案进程跑 --runs 次, 统计:
  - 用时、退出码;
  - 候选个数、超出 n 的个数;
  - 过不过白名单 (参数在白名单里、取值原样来自 values、同组 min<=max) —— 这是 harness 校验的
    **近似** (真正的校验在 src/optimize/sysparam.rs::validate), 只用来离线比较;
  - 是否原样重复了历史里被量准判 REJECT 的候选;
  - (--jev) 守门给的 p(值得上机), state 与 gate.rs 同形。
只用标准库。--jev 要环境变量 TYPESAFE_API_KEY (key) 或 TYPESAFE_KEY_FILE (文件路径)。
"""
import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gate_replay  # noqa: E402  (同目录: 复用守门的问法)


def norm(params):
    return tuple(sorted(params.items()))


def check(c, inp):
    """→ None (合法) 或 一句原因。"""
    if c.get("kind") != inp["kind"]:
        return f"kind={c.get('kind')}"
    if set(c) - {"kind", "why", "payload"}:
        return f"多余字段 {sorted(set(c) - {'kind', 'why', 'payload'})}"
    if inp["kind"] != "sysparam":
        return None
    wl = inp["constraints"].get("whitelist") or {}
    params = (c.get("payload") or {}).get("params") or {}
    if not params:
        return "空 params"
    for k, v in params.items():
        if k not in wl:
            return f"{k} 不在白名单"
        if v not in wl[k]["values"]:
            return f"{k}={v} 不在取值表"
    # 同组 min/max: 合上当前值后 min <= max
    for k, v in params.items():
        if k.endswith("scaling_min_freq"):
            mx = k.replace("min", "max")
            hi = params.get(mx, (wl.get(mx) or {}).get("current"))
            if hi and v.isdigit() and str(hi).isdigit() and int(v) > int(hi):
                return f"{k}={v} > {mx}={hi}"
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--proposer", required=True, help="可执行文件 (与 --proposer cmd: 后面同)")
    ap.add_argument("--input", action="append", required=True)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--jev", action="store_true")
    ap.add_argument("--out", default="runs/proposer_eval")
    a = ap.parse_args()
    key = gate_replay.key() if a.jev else None
    rows = []
    for path in a.input:
        inp = json.load(open(path))
        rejected = {norm(h["candidate"]["payload"]["params"]) for h in inp.get("history", [])
                    if h.get("measurement_valid") and not h.get("kept") and h["candidate"]["kind"] == "sysparam"}
        hist = [{"change": h["candidate"]["payload"].get("params") and gate_replay.change_of(h["candidate"]["payload"]["params"]),
                 "status": h["status"], "valid": h["measurement_valid"], "kept": h["kept"], "reason": h.get("reason")}
                for h in inp.get("history", [])]
        for i in range(a.runs):
            t0 = time.time()
            r = subprocess.run([a.proposer], input=json.dumps(inp), capture_output=True, text=True, timeout=900)
            dt = time.time() - t0
            row = {"input": os.path.basename(path), "run": i + 1, "rc": r.returncode, "seconds": round(dt, 1),
                   "n": inp["n"], "candidates": []}
            try:
                out = json.loads(r.stdout) if r.returncode == 0 else {"candidates": []}
            except Exception as e:
                out, row["parse_error"] = {"candidates": []}, str(e)
            for j, c in enumerate(out.get("candidates", [])):
                params = (c.get("payload") or {}).get("params") or {}
                cr = {"change": gate_replay.change_of(params) if params else json.dumps(c.get("payload"), ensure_ascii=False),
                      "over_n": j >= inp["n"], "invalid": check(c, inp),
                      "dup_of_rejected": bool(params) and norm(params) in rejected}
                if key and cr["invalid"] is None:
                    st = gate_replay.state({"change": cr["change"], "why": c.get("why", "")}, hist)
                    cr["jev_p"], _ = gate_replay.ask(key, st)
                row["candidates"].append(cr)
            rows.append(row)
            print(f"{row['input']} #{row['run']} rc={row['rc']} {row['seconds']}s "
                  + "; ".join(f"{c['change']}{' [不合法:' + c['invalid'] + ']' if c['invalid'] else ''}"
                              f"{' [重复REJECT]' if c['dup_of_rejected'] else ''}"
                              f"{' p=%.2f' % c['jev_p'] if 'jev_p' in c else ''}" for c in row["candidates"]))
    cands = [c for r in rows for c in r["candidates"]]
    summary = {
        "proposer": a.proposer, "calls": len(rows), "failed_calls": sum(r["rc"] != 0 for r in rows),
        "median_seconds": sorted(r["seconds"] for r in rows)[len(rows) // 2],
        "candidates": len(cands), "invalid": sum(c["invalid"] is not None for c in cands),
        "over_n": sum(c["over_n"] for c in cands), "dup_of_rejected": sum(c["dup_of_rejected"] for c in cands),
        "distinct": len({c["change"] for c in cands}),
    }
    if key:
        ps = [c["jev_p"] for c in cands if "jev_p" in c]
        summary["jev_p_min_median_max"] = [min(ps), sorted(ps)[len(ps) // 2], max(ps)] if ps else None
    os.makedirs(a.out, exist_ok=True)
    name = os.path.basename(a.proposer).replace(".py", "")
    json.dump({"summary": summary, "rows": rows}, open(os.path.join(a.out, f"{name}.json"), "w"),
              ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
