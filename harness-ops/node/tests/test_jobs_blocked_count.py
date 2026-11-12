"""A computer's queue on the 作业 page: 执行范围待核查 says nothing while no job waits on it, and shows in amber when
one does."""
import re
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path

QUEUE = re.compile(r".*/dash/api/queue(\?.*)?$")


class JobsBlockedCountTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli = core.CoreFlowTests.request, core.CoreFlowTests.cli

    def counts(self, blocked):
        with self._browser_page() as page:
            def queue(route):
                value = route.fetch().json()
                value["blocked_count"] = blocked
                route.fulfill(json=value)
            page.route(QUEUE, queue)
            page.goto("http://%s:%d/dash/jobs" % self.address)
            page.wait_for_selector("#queue-summary:not([hidden])")
            return page.eval_on_selector_all("#queue-summary > span", "e => e.map(s => [s.textContent, s.className, getComputedStyle(s).color])")

    def test_the_blocked_count_shows_only_when_a_job_waits(self):
        quiet = self.counts(0)
        self.assertEqual([text.lstrip("0123456789") for text, _, _ in quiet], ["已分配作业 / 2 个名额", "排队"])
        loud = self.counts(1)
        self.assertEqual(loud[-1][:2], ["1执行范围待核查", "warn"])
        self.assertNotEqual(loud[-1][2], loud[0][2], "amber, not the plain colour")


if __name__ == "__main__":
    unittest.main()
