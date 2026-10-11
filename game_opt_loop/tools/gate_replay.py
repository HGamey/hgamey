#!/usr/bin/env python3
"""上机前守门 (Jev) 的离线回放: 拿历史上真机判过的候选问 Jev, 看它的「值不值得上机」和真实判定对不对得上。

    python3 tools/gate_replay.py [--out runs/gate_replay] <证据>...

<证据> 可以是:
  - phonefarm loop_v1 的 sysparam 闭环目录 (含 report.json, 里面有 results[])
  - game_opt_loop optimize 的输出目录 (含 report.json, 里面有 archive[])
按给出的顺序串成一条时间线: 每个候选问 Jev 时, 只给它看在它之前上过机的候选 (和守门在线时看到的一样)。

另外做一组「已知答案」探针: 对每个量准了且被 REJECT 的候选, 把它原样再提一次 (历史里已经有它),
理想的守门应该给低分 —— 这正是守门要挡的「已被真机否定的重复」。

state 的形状与 src/optimize/gate.rs 的 state_for 一致 (改那边要同步改这里)。
key: 环境 TYPESAFE_API_KEY, 或 ~/.config/hgamey/typesafe.key。只用标准库。
"""
import argparse
import json
import os
import sys
import time
import urllib.request

URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
HISTORY_TAIL = 12

# 与 gate.rs::question() 同一段话。
QUESTION = {
    "worth_device_time": {
        "type": "noul",
        "instructions": "这是手机游戏性能优化流水线里的一个候选改动, 上真机评测一次要 20 分钟左右。"
        "根据设备、这一层的判定口径、这个候选改了什么、以及这一层已经上过机的候选和真机结论, "
        "判断这个候选值不值得花一次真机评测: 有合理的理由期望它能显著改善帧时/帧率/功耗且不退化, "
        "并且不是已被真机否定过的候选的近似重复。",
        "criteria": {
            "true": "值得上机: 有合理的改善预期, 且不是已被否定方向的重复",
            "false": "不值得上机: 几乎是已被否定候选的重复, 或没有合理的改善预期, 或风险明显大于收益",
        },
    }
}

DEVICE = {"model": "NX809J", "platform": "adb", "root": True, "note": "红魔 NX809J (骁龙 8 Elite, Adreno 840), 已 root"}
TARGET = {"game": "原神", "workload": "开放世界原地触控转视角 36 秒 (workload_spin_touch_v1)"}
JUDGING = {
    "metrics": [{"id": "fps_p95_ms", "better": "lower"}, {"id": "fps_mean", "better": "higher"},
                {"id": "power_watt", "better": "lower"}],
    "rule": "每臂 5 轮交替 A/B, 精确置换检验, 赢面 Bonferroni; 任一指标显著退化即不留",
}


def key():
    k = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if k:
        return k
    return open(os.path.expanduser("~/.config/hgamey/typesafe.key")).read().strip()


def change_of(params):
    return ", ".join(f"{k}={v}" for k, v in sorted(params.items()))


def load(path):
    """一个证据目录 → [(来源, 候选)]。候选: change, why, status, valid, kept, reason。"""
    r = json.load(open(os.path.join(path, "report.json")))
    out = []
    if "results" in r:  # loop_v1 sysparam
        for i, x in enumerate(r["results"]):
            st = x.get("verdict")
            out.append({"src": f"{os.path.basename(path)}/cand{i + 1}", "change": change_of(x["params"]),
                        "why": x.get("why", ""), "status": st, "valid": st in ("PASS", "REJECT", "KEEP"),
                        "kept": st in ("PASS", "KEEP"), "reason": x.get("reason")})
    elif "archive" in r:  # optimize
        for e in r["archive"]:
            ch = e["change"]
            if ch.get("kind") == "sysparam":
                c = change_of(ch["params"])
            elif ch.get("kind") == "gray":
                c = "+".join(ch["rewrites"])
            else:
                continue
            out.append({"src": f"{os.path.basename(path.rstrip('/'))}/{e['id']}", "change": c, "why": e.get("why", ""),
                        "status": e["status"], "valid": e["measurement_valid"], "kept": e["kept"],
                        "reason": (e.get("verdict") or {}).get("reason")})
    else:
        sys.exit(f"{path}/report.json 认不出格式")
    return out


def state(c, history):
    hist = [{"change": h["change"], "status": h["status"], "measurement_valid": h["valid"], "kept": h["kept"],
             "reason": h["reason"], "per_metric": []} for h in history[-HISTORY_TAIL:]]
    return {"task": "手机游戏性能优化: 判断候选值不值得上真机评测", "device": DEVICE, "target": TARGET,
            "layer": "③ 系统参数", "judging": JUDGING, "candidate": {"change": c["change"], "why": c["why"]},
            "history": hist}


def ask(k, st):
    body = json.dumps({"model": MODEL, "state": st, "questions": QUESTION}).encode()
    req = urllib.request.Request(URL, data=body, method="POST",
                                 headers={"authorization": f"Bearer {k}", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        a = json.load(r)
    return a["answers"]["worth_device_time"]["noul"], a.get("usage", {})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="runs/gate_replay")
    ap.add_argument("--repeats", type=int, default=1, help="每个问题问几次 (看稳定性)")
    ap.add_argument("evidence", nargs="+")
    a = ap.parse_args()
    k = key()
    rows, history, tokens = [], [], 0
    timeline = [c for p in a.evidence for c in load(p)]
    for c in timeline:
        ps = []
        for _ in range(a.repeats):
            p, u = ask(k, state(c, history))
            ps.append(p)
            tokens += u.get("input_tokens", 0) + u.get("output_tokens", 0)
        rows.append({"probe": "replay", **c, "p": ps})
        history.append(c)
    # 已知答案: 被量准判 REJECT 的候选原样重提。
    for c in timeline:
        if c["valid"] and not c["kept"]:
            ps = []
            for _ in range(a.repeats):
                p, u = ask(k, state(dict(c, why=c["why"]), history))
                ps.append(p)
                tokens += u.get("input_tokens", 0) + u.get("output_tokens", 0)
            rows.append({"probe": "duplicate_of_rejected", **c, "p": ps})

    os.makedirs(a.out, exist_ok=True)
    json.dump({"generated_at": time.strftime("%FT%T%z"), "model": MODEL, "evidence": a.evidence,
               "tokens": tokens, "rows": rows}, open(os.path.join(a.out, "gate_replay.json"), "w"),
              ensure_ascii=False, indent=1)
    print(f"{'探针':<22} {'来源':<34} {'真机':<8} {'p(值得)':<16} 改动")
    for r in rows:
        ps = " ".join(f"{x:.2f}" for x in r["p"])
        print(f"{r['probe']:<22} {r['src']:<34} {r['status']:<8} {ps:<16} {r['change'][:70]}")
    print(f"\n共 {len(rows)} 问 x {a.repeats} 次, tokens {tokens}; 明细 {a.out}/gate_replay.json")


if __name__ == "__main__":
    main()
