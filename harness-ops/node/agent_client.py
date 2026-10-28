"""Agent registry CLI: register, list, show, update, token — same contract as the API — and the check-in view."""
import json
import sys
from cli_time import utc

HELP = """pf-node agents                              # 注册 agent 列表
pf-node agents show NAME                    # 单个详情
pf-node agents register FILE                # 注册（FILE 为 data JSON，至多 4 KiB）
pf-node agents status NAME TEXT             # 上报当前在做什么（至多 1024 字符，保留最近 50 条）
pf-node agents token NAME                   # 用管理 token 为 NAME 签发它自己的 token（只打印一次，旧的作废）
pf-node agents update NAME FILE --revision N  # 并发更新（CAS）"""


def main(args, target=None):
    if args and args[0] in ("--help", "-h"):
        print(HELP)
        return 0
    if target is None:
        sys.exit("pf-node agents: 需显式目标（--url / PF_NODE_URL）")
    node_id = target.expected_node_id
    if not node_id:
        sys.exit("pf-node agents: 需 --node-id / PF_NODE_ID")

    def call(method, path, body=None):
        request = target.request(method, path, body,
            {"Content-Type": "application/json"} if body is not None else None)
        with target.opener.open(request, timeout=30) as response:
            return json.loads(response.read())

    if not args:
        print(json.dumps(call("GET", "/agents"), ensure_ascii=False, indent=1))
        return 0
    if args[0] == "show" and len(args) == 2:
        print(json.dumps(call("GET", f"/agents/{args[1]}"), ensure_ascii=False, indent=1))
        return 0
    if args[0] == "register" and len(args) == 2:
        data = json.loads(open(args[1], "rb").read())
        if not isinstance(data, dict) or "name" not in data:
            sys.exit("pf-node agents register: FILE 须含 name")
        print(json.dumps(call("POST", "/agents", json.dumps(data).encode()), ensure_ascii=False, indent=1))
        return 0
    if args[0] == "token" and len(args) == 2:
        value = call("POST", f"/agents/{args[1]}/token", b"{}")
        print(value["token"])
        print(f"pf-node: 已为 {args[1]} 签发 token（之前的作废，只显示这一次）。存进私有文件，"
              "之后用 PF_NODE_TOKEN_FILE=<文件>，所有写入都记在这个 agent 名下。", file=sys.stderr)
        return 0
    if args[0] == "status" and len(args) == 3:
        # agents status NAME "在做什么"
        print(json.dumps(call("POST", f"/agents/{args[1]}/status",
                              json.dumps({"text": args[2]}).encode()), ensure_ascii=False, indent=1))
        return 0
    if args[0] == "update" and len(args) == 5 and args[3] == "--revision":
        data = json.loads(open(args[2], "rb").read())
        data["revision"] = int(args[4])
        print(json.dumps(call("POST", f"/agents/{args[1]}", json.dumps(data).encode()),
                         ensure_ascii=False, indent=1))
        return 0
    if not args or args == ["list"]:
        print(json.dumps(call("GET", "/agents"), ensure_ascii=False, indent=1))
        return 0
    sys.exit("pf-node agents: 无效命令；用 agents --help 查看用法")


def render_checkin(value):
    """The check-in as an agent reads it at the start of a loop."""
    def when(at):
        return utc(at) if at else "从未"
    lines = [f"{value['agent']} · 节点时区 UTC · 当前时间 {when(value['at'])} · 上次签到 {when(value['previous_checkin_at'])}"]
    duties = value["duties"]
    lines.append(f"待办 {len(duties)} 项：" if duties else "待办：无，按计划推进实验。")
    for index, duty in enumerate(duties, 1):
        lines.append(f" {index}. {'[新] ' if duty.get('unread') else ''}{duty['text']}")
        if duty.get("command"):
            lines.append(f"    → {duty['command']}")
    if value["waiting_on_human"]:
        lines.append("等人回复（不要停下，继续做其他方向）：")
        for item in value["waiting_on_human"]:
            lines.append(f" - {item['research_id']} / {item['request_id']} {item['title']}{' · 已过期' if item['overdue'] else ''}")
    jobs = value["jobs"]
    finished = jobs["finished_since_last_checkin"]
    lines.append(f"任务：进行中 {len(jobs['active'])} · 卡住 {len(jobs['blocked'])} · 上次签到后结束 {len(finished)}"
                 + (" (" + ", ".join(item["job_id"] for item in finished) + ")" if finished else ""))
    unlinked = [item["job_id"] for item in jobs["active"] if not item.get("research_id")]
    if unlinked and value["research"]["mine"]:
        lines.append("未关联研究的任务：" + ", ".join(unlinked) + "（之后提交加 --research <研究>）")
    for item in value["research"]["mine"]:
        lines.append(f"我负责的研究：{item['research_id']} {item['title']}（{item['state']}）")
    for item in value["research"]["unclaimed"]:
        lines.append(f"可认领：{item['research_id']} {item['title']} → pf-node research claim {item['research_id']}")
    lines.append("每轮做法：")
    lines += [f" {index}. {step}" for index, step in enumerate(value["loop"], 1)]
    return "\n".join(lines)
