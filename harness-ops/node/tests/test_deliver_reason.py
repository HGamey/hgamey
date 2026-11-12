"""A delivery's reason is the task's state reason, also after the task was reopened: the reopen reason does not
stay on a delivered task."""
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path


class DeliverReasonTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    request, cli, web, pf = core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web, core.CoreFlowTests.pf
    write = staticmethod(core.CoreFlowTests.write)

    def deliver(self, token, *reason):
        code, _, err = self.pf(token, "research", "deliver", "sr-again", "--summary", "完成", "--artifact", f"job:{self.jid}", *reason)
        self.assertEqual(code, 0, err)
        return self.cli("GET", "/research-state/research/sr-again")[1]["data"]

    def test_a_delivery_after_a_reopen_states_its_own_reason(self):
        self.assertEqual(self.web("POST", "/research-state/research/sr-again", self.write(
            "web-task", {"title": "再交一次", "objective": "补实验"}))[0], 201)
        self.assertEqual(self.cli("POST", "/agents", {"name": "agent-a"})[0], 201)
        token = self.cli("POST", "/agents/agent-a/token", {})[1]["token"]
        self.assertEqual(self.pf(token, "research", "claim", "sr-again")[0], 0)
        self.assertEqual(self.deliver(token)["state_reason"], "交付")
        current = self.cli("GET", "/research-state/research/sr-again")[1]
        self.assertEqual(self.web("POST", "/research-state/research/sr-again", {
            **self.write("web-reopen", {**current["data"], "state": "active", "state_reason": "补一组跨机实验"}),
            "base_revision": current["revision"], "reason": "补一组跨机实验"})[0], 201)
        data = self.deliver(token, "--reason", "跨机实验补齐")
        self.assertEqual((data["state"], data["state_reason"]), ("delivered", "跨机实验补齐"))


if __name__ == "__main__":
    unittest.main()
