#!/usr/bin/env python3
"""最小的外部出方案 (game_opt_loop/proposer@1): 每代只给一个候选, 不看历史。

③ 系统参数: 在白名单里挑一项 (缺省 gpu.min_pwrlevel, 没有就取第一项),
   取一个和当前值不同的合法值。用环境变量 FIXED_PARAM / FIXED_VALUE 可以钉死。
② 渲染流程改写: 不出候选 (回空列表, harness 会如实记「这一层没提出候选」)。

只用标准库。stdin 收一份输入, stdout 回一份输出, 日志写 stderr。
用法: game_opt_loop optimize --game 原神 --proposer cmd:examples/proposers/fixed_one.py
"""
import json
import os
import sys

PROTOCOL = "game_opt_loop/proposer@1"


def main() -> int:
    inp = json.load(sys.stdin)
    if inp.get("protocol") != PROTOCOL:
        print(f"不认识的协议 {inp.get('protocol')!r}", file=sys.stderr)
        return 2

    out = {"protocol": PROTOCOL, "version": 1, "candidates": [], "proposer": "fixed_one.py"}
    if inp["kind"] == "sysparam" and inp["generation"] == 1:
        wl = inp["constraints"].get("whitelist") or {}
        want = os.environ.get("FIXED_PARAM", "gpu.min_pwrlevel")
        key = want if want in wl else next(iter(sorted(wl)), None)
        if key is not None:
            spec = wl[key]
            value = os.environ.get("FIXED_VALUE")
            if value is None:
                others = [v for v in spec["values"] if v != spec["current"]]
                value = others[len(others) // 2] if others else spec["current"]
            out["candidates"].append({
                "kind": "sysparam",
                "why": f"固定候选: 只改 {key} ({spec['current']} -> {value}), 用来验证外部出方案链路",
                "payload": {"params": {key: value}},
            })
    print(f"[fixed_one] kind={inp['kind']} gen={inp['generation']} -> {len(out['candidates'])} 个候选",
          file=sys.stderr)
    json.dump(out, sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
