#!/usr/bin/env python3
"""用 pi + Kimi 出方案的外部出方案 (game_opt_loop/proposer@1)。

本机直接跑 (pi 已配好 Kimi):
    game_opt_loop optimize ... --proposer cmd:tools/proposers/pi_kimi.py
在 GitHub Actions 里跑: 见 .github/workflows/proposer.yml 与 tools/proposers/cloud_actions.py。

环境变量:
    PI_MODEL      pi 的模型 (缺省 kimi-1/kimi-for-coding)
    PI_MODELS     逗号分隔的多个模型 (通常是同一模型挂在不同账号的 provider 下), 按顺序轮换:
                  某个账号回 403 额度用完 / 429 限流就换下一个, 全部失败才报错。给了它就不看 PI_MODEL。
                  日志里只记用的是第几个, 不打印 key。
    PI_BIN        pi 可执行 (缺省 pi)
    PI_MCP_TOOLS  逗号分隔的 MCP 工具白名单 (例如 mcp__pfnode__phonefarm_fleet)。
                  不给 = 不开任何工具, 只凭输入出方案。只应放只读工具。
                  对应的 MCP 服务器要在 pi 的 mcp.json 里设 "exposure": "direct", 否则模型看不到工具。
    PI_TIMEOUT_S  pi 单次超时 (缺省 420 秒)
    PI_EXTRA_ARGS 追加给 pi 的参数 (空白分隔), 例如 --approve 以读项目本地的 .pi/mcp.json

stdin 一份输入, stdout 一份输出, 日志写 stderr。只用标准库。
"""
import json
import os
import re
import subprocess
import sys

PROTOCOL = "game_opt_loop/proposer@1"

RULES = """你在为一个手机游戏性能优化流水线出候选改动。你只决定「这一代改什么」, 判定由真机 A/B 冻结规则做, 你影响不了。

硬规矩:
1. 只出 kind 等于输入里 kind 的候选, 最多 n 个。
2. ③ sysparam: payload.params 是 {参数id: 取值字符串}, 参数 id 必须在 constraints.whitelist 里,
   取值必须原样来自该参数的 values 列表。不在白名单里的、自己编的取值, 一律会被打回。
   同组 (group 相同) 的 min/max 要一起合法 (min <= max)。不要碰含 deny_keywords 的东西。
3. ② gray: payload.rewrites 是 [{"knob": 开关id}], 只能用 constraints.knobs 里 status = landed 的开关。
4. 不要重复 history 里已经上过机且被判 REJECT 的候选或其近似; ABORT (measurement_valid=false) 不代表方向不行。
5. 每个候选只放 kind / why / payload 三个字段。why 用一句中文说清想验证什么。
6. 每次真机评测约 20 分钟, 宁缺毋滥: 没有把握就少出。

只输出一个 JSON 对象, 不要任何别的文字:
{"protocol": "game_opt_loop/proposer@1", "version": 1, "proposer": "pi_kimi", "candidates": [ ... ]}
"""


def log(*a):
    print("[pi_kimi]", *a, file=sys.stderr, flush=True)


def slim(inp: dict) -> dict:
    """给模型看的输入: 去掉对出方案没用、又很占篇幅的部分。"""
    keep = {k: inp.get(k) for k in ("kind", "generation", "n", "device", "target", "plan", "judging", "constraints")}
    hist = []
    for h in inp.get("history") or []:
        hist.append({
            "candidate": h.get("candidate"),
            "status": h.get("status"),
            "measurement_valid": h.get("measurement_valid"),
            "kept": h.get("kept"),
            "reason": h.get("reason"),
            "per_metric": h.get("summary"),
        })
    keep["history"] = hist
    return keep


def quota_or_rate_limited(text: str) -> bool:
    """pi 的报错里看得出是 403 额度用完或 429 限流 (换账号能解决的那一类)。"""
    t = text.lower()
    return ("429" in t or "rate limit" in t or "usage limit" in t or "quota" in t
            or ("403" in t and ("limit" in t or "permission_error" in t)))


def extract_json(text: str):
    """从模型回复里抠出最后一个能解析、带 candidates 的 JSON 对象。"""
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S)
    tries = fenced[::-1] + [text[text.find("{"): text.rfind("}") + 1]] if "{" in text else fenced[::-1]
    for t in tries:
        try:
            v = json.loads(t)
        except Exception:
            continue
        if isinstance(v, dict) and isinstance(v.get("candidates"), list):
            return v
    return None


def main() -> int:
    inp = json.load(sys.stdin)
    if inp.get("protocol") != PROTOCOL:
        log("不认识的协议", inp.get("protocol"))
        return 2
    models = [m.strip() for m in os.environ.get("PI_MODELS", "").split(",") if m.strip()]
    if not models:
        models = [os.environ.get("PI_MODEL", "kimi-1/kimi-for-coding")]
    pi = os.environ.get("PI_BIN", "pi")
    tools = os.environ.get("PI_MCP_TOOLS", "").strip()
    timeout = int(os.environ.get("PI_TIMEOUT_S", "420"))

    prompt = RULES + "\n\n输入:\n" + json.dumps(slim(inp), ensure_ascii=False)
    cmd = [pi, "-p", "--no-session", "-nc", "-ns", "-np"]
    if tools:
        # MCP 由 pi 的内置扩展提供, 不能加 -ne; 服务器要在 mcp.json 里设 "exposure": "direct"。
        cmd += ["--tools", tools]
        prompt += "\n\n你可以用这些只读工具查手机现状: " + tools + "。不要尝试任何会改动设备的操作。"
    else:
        cmd += ["-ne", "-nt", "--no-mcp"]
    cmd += os.environ.get("PI_EXTRA_ARGS", "").split()
    log(f"kind={inp['kind']} gen={inp['generation']} n={inp['n']} tools={tools or '无'} 账号 {len(models)} 个")
    r, model = None, None
    for i, m in enumerate(models, 1):
        try:
            r = subprocess.run(cmd + ["--model", m], input=prompt, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            log(f"第 {i} 个账号: pi 超过 {timeout} 秒")
            return 3
        if r.returncode == 0:
            model = m
            log(f"用的是第 {i} 个账号")
            break
        if quota_or_rate_limited(r.stdout + r.stderr) and i < len(models):
            log(f"第 {i} 个账号额度用完或被限流, 换下一个")
            continue
        log(f"第 {i} 个账号失败, pi 退出码 {r.returncode}:", (r.stderr or r.stdout)[-800:])
        return 3
    if model is None:
        return 3
    out = extract_json(r.stdout)
    if out is None:
        log("回复里找不到合法 JSON:", r.stdout[-800:])
        return 4
    out["protocol"], out["version"] = PROTOCOL, 1
    out.setdefault("proposer", f"pi_kimi ({model})")
    # 只留契约里的三个字段, 其余的 (模型手滑带上的) 去掉; 真正的校验在 harness。
    cands = []
    for c in out.get("candidates") or []:
        if isinstance(c, dict):
            cands.append({k: c[k] for k in ("kind", "why", "payload") if k in c})
    out["candidates"] = cands[: max(int(inp.get("n") or 0), 0)]
    log(f"{len(out['candidates'])} 个候选")
    json.dump(out, sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
