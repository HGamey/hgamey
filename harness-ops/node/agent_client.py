"""Agent registry CLI: register, list, show, update — same contract as the API."""
import json
import sys

HELP = """pf-node agents                              # 注册 agent 列表
pf-node agents show NAME                    # 单个详情
pf-node agents register FILE                # 注册（FILE 为 data JSON，至多 4 KiB）
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
