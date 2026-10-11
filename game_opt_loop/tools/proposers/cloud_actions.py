#!/usr/bin/env python3
"""云端出方案适配器 (game_opt_loop/proposer@1): 把输入交给 GitHub Actions 里的 pi + Kimi, 等结果回来。

    game_opt_loop optimize ... --proposer cmd:tools/proposers/cloud_actions.py --proposer-timeout-s 900

需要: 跑 optimize 的那台机器上有已登录、对仓库有 actions 写权限的 gh;
仓库里配好 .github/workflows/proposer.yml 要的 secrets。环境变量 GOL_PROPOSER_REPO 可换仓库
(缺省 HGamey/game_opt_loop), GOL_PROPOSER_REF 可换分支 (缺省 main)。

流程: gh workflow run (输入 gzip+base64, 带 nonce) → 按 run-name 找到这次 run → 等它结束 →
gh run download 取 artifact → 原样把 output.json 写到 stdout。runner 上出方案失败则退出码非 0。
"""
import base64
import gzip
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid

REPO = os.environ.get("GOL_PROPOSER_REPO", "HGamey/game_opt_loop")
REF = os.environ.get("GOL_PROPOSER_REF", "main")
WORKFLOW = "proposer.yml"


def log(*a):
    print("[cloud_actions]", *a, file=sys.stderr, flush=True)


def gh(*args, check=True):
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:3])} 失败: {r.stderr.strip()[-400:]}")
    return r.stdout


def main() -> int:
    raw = sys.stdin.buffer.read()
    b64 = base64.b64encode(gzip.compress(raw)).decode()
    if len(b64) > 60000:
        log(f"输入压缩后仍有 {len(b64)} 字符, 超过 workflow_dispatch 输入上限")
        return 2
    nonce = uuid.uuid4().hex[:12]
    gh("workflow", "run", WORKFLOW, "-R", REPO, "--ref", REF, "-f", f"nonce={nonce}", "-f", f"input_b64={b64}")
    log(f"已触发 {REPO} {WORKFLOW} nonce={nonce}")

    run_id = None
    for _ in range(60):
        time.sleep(5)
        runs = json.loads(gh("run", "list", "-R", REPO, "--workflow", WORKFLOW, "--limit", "20",
                             "--json", "databaseId,displayTitle,status,conclusion"))
        hit = [r for r in runs if r["displayTitle"] == f"proposer {nonce}"]
        if hit:
            run_id = hit[0]["databaseId"]
            break
    if run_id is None:
        log("5 分钟内没找到这次 run")
        return 3
    log(f"run {run_id}")
    while True:
        r = json.loads(gh("run", "view", str(run_id), "-R", REPO, "--json", "status,conclusion"))
        if r["status"] == "completed":
            break
        time.sleep(10)
    with tempfile.TemporaryDirectory() as d:
        try:
            gh("run", "download", str(run_id), "-R", REPO, "-n", f"proposer-{nonce}", "-D", d)
        except RuntimeError as e:
            log(f"run {run_id} 结束 ({r['conclusion']}), 取不回结果: {e}")
            return 4
        rc = open(os.path.join(d, "rc.txt")).read().strip() if os.path.exists(os.path.join(d, "rc.txt")) else "?"
        err = os.path.join(d, "stderr.txt")
        if os.path.exists(err):
            sys.stderr.write(open(err).read())
        if r["conclusion"] != "success" or rc != "0":
            log(f"云端出方案失败: conclusion={r['conclusion']} rc={rc}")
            return 4
        sys.stdout.write(open(os.path.join(d, "output.json")).read())
    return 0


if __name__ == "__main__":
    sys.exit(main())
