"""接入新 agent is for whoever holds the steer password: a read-only viewer of the home page does not see it, so a
curious click does not end in a password box; once steer is unlocked it is there."""
import time
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path

HOME = {"now": 0, "steer": False, "questions": [], "reviews": [], "tasks": [], "slots": [], "fleet_age_s": 0,
        "elsewhere": [], "agents": [{"name": "a1", "note": None, "checkin_at": None, "status": None, "owes": [], "research": [],
                                     "archived": None, "activity_at": None}]}


class InviteSteerTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page

    def test_invite_shows_only_once_steer_is_unlocked(self):
        steer = {"on": False}
        with self._browser_page() as page:
            page.route("**/dash/api/home", lambda route: route.fulfill(json={**HOME, "now": time.time(), "steer": steer["on"]}))
            page.goto("http://%s:%d/dash" % self.address)
            page.wait_for_selector('#agents li[data-agent="a1"]')
            self.assertTrue(page.is_hidden("#invite-open"))
            self.assertEqual(page.locator("dialog[open]").count(), 0)
            steer["on"] = True
            page.reload()
            page.wait_for_selector("#invite-open:visible")
            self.assertEqual(page.inner_text("#invite-open"), "接入新 agent")


if __name__ == "__main__":
    unittest.main()
