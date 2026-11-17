"""A task an agent creates with its own token is its own unless the data names the owner ("owner": null leaves it for
anyone to claim), so another agent's wake-up cannot take it as unowned. A task a person creates on the web, or one
written with the admin token, stays unowned until someone claims it. The check-in and the research help say so."""
import json
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path
import agent_duties

OWNER_RULE = "data 里不写 owner，任务就由你负责；要留给别人认领，写 \"owner\": null。"


class TaskOwnerDefaultTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    request, cli, web, pf, duties = (core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web,
                                     core.CoreFlowTests.pf, core.CoreFlowTests.duties)
    write = staticmethod(core.CoreFlowTests.write)

    def agent_token(self, name):
        self.assertEqual(self.cli("POST", "/agents", {"name": name})[0], 201)
        return self.cli("POST", f"/agents/{name}/token", {})[1]["token"]

    def create(self, token, rid, data):
        source = self.tmp / f"{rid}.json"
        source.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        code, _, err = self.pf(token, "research", "create", "research", rid, str(source))
        self.assertEqual(code, 0, err)

    def owner(self, rid):
        return self.cli("GET", f"/research-state/research/{rid}")[1]["data"]["owner"]

    def test_an_agent_owns_the_task_it_creates_unless_it_leaves_the_owner_open(self):
        mine, other = self.agent_token("agent-a"), self.agent_token("agent-b")
        self.create(mine, "gi-study", {"title": "原神私服调研", "objective": "看看能不能绕开登录"})
        self.assertEqual(self.owner("gi-study"), "agent-a")
        code, _, err = self.pf(other, "research", "claim", "gi-study")
        self.assertNotEqual(code, 0)
        self.assertIn("agent-a", err)
        self.create(mine, "gi-open", {"title": "留给别人做的任务", "objective": "谁有空谁做", "owner": None})
        self.assertIsNone(self.owner("gi-open"))
        self.assertEqual(self.pf(other, "research", "claim", "gi-open")[0], 0)
        self.assertEqual(self.owner("gi-open"), "agent-b")

    def test_a_task_created_on_the_web_or_with_the_admin_token_waits_for_a_claim(self):
        self.assertEqual(self.web("POST", "/research-state/research/web-task",
                                  self.write("create-web-task", {"title": "网页上建的任务", "objective": "等人认领"}))[0], 201)
        self.assertIsNone(self.owner("web-task"))
        self.assertEqual(self.cli("POST", "/research-state/research/admin-task",
                                  self.write("create-admin-task", {"title": "管理员建的任务", "objective": "等人认领"}))[0], 201)
        self.assertIsNone(self.owner("admin-task"))

    def test_the_check_in_and_the_research_help_explain_the_default(self):
        self.assertIn(OWNER_RULE, agent_duties.TASK_TITLE_HELP)
        token = self.agent_token("agent-a")
        self.assertIn(agent_duties.TASK_TITLE_HELP, self.duties(token, rules=True)["loop"])
        self.assertIn(OWNER_RULE, self.pf(token, "research", "--help")[1])


if __name__ == "__main__":
    unittest.main()
