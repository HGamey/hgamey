"""Every state a direction can be in reads in words on the 指挥中心 map, in the task page's words for the ones the map
had no word of its own for: a direction that ended without a gain reads 无收益, not no_gain, and looks ended like one
that was given up; one waiting on a phone reads 等待设备."""
import re
import time
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path

STATES = {"no_gain": "无收益", "waiting_device": "等待设备", "waiting_human": "等待协助", "waiting_material": "等待素材",
          "awaiting_evaluation": "等待评价", "preparing": "准备中", "ready": "可开展", "rejected": "试过放弃"}


def model(now):
    dirs, jobs = [], []
    for i, state in enumerate(STATES):
        dirs.append({"id": f"d-{state}", "research_id": "sr-1", "title": f"方向 {i}", "state": state, "based_on": None,
                     "created_at": now - 7000 + i, "updated_at": now - 600 + i})
        jobs.append({"id": "%012x" % (0xa0 + i), "kind": "实验", "agent": "kimi-researcher", "research_id": "sr-1", "device": None,
                     "started": now - 3600 + i * 60, "ended": now - 3500 + i * 60, "outcome": "succeeded", "label": None, "phone": False,
                     "direction_id": f"d-{state}", "state": "finished", "phase": "finished", "queued": now - 3600 + i * 60,
                     "claimed": now - 3600 + i * 60, "waits": []})
    return {"at": now, "live": True, "source": "test", "phones": [], "phones_sampled_at": None, "notes": [], "agents": ["kimi-researcher"],
            "research": [{"id": "sr-1", "title": "原神超分", "state": "active", "owner": "kimi-researcher", "follows": None,
                          "created_at": now - 7200, "updated_at": now - 60}], "directions": dirs, "jobs": jobs}


class CommandDirectionStateTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page

    def test_each_direction_state_reads_in_words(self):
        with self._browser_page() as page:
            page.route(re.compile(r".*/dash/api/command(\?.*)?$"), lambda route: route.fulfill(json=model(time.time())))
            page.set_viewport_size({"width": 1440, "height": 900})
            page.goto("http://%s:%d/dash/command" % self.address)
            page.wait_for_function("() => window.__ready === true", timeout=20000)
            page.wait_for_function("() => document.querySelectorAll('.chip.rejected small').length === 1")
            chips = page.evaluate("""() => Object.fromEntries([...document.querySelectorAll('.chip')].filter(c => c.querySelector('.t span'))
                .map(c => [c.className.split(' ')[1], c.querySelector('small').textContent]))""")
            self.assertEqual(chips, {state: f"{word} · 1 次实验" for state, word in STATES.items()})
            body = page.evaluate("() => document.body.textContent")
            for code in ("no_gain", "waiting_", "awaiting_"):
                self.assertNotIn(code, body, code)
            look = page.evaluate("""() => ['no_gain', 'rejected'].map(s => { const c = document.querySelector('.chip.' + s);
                return [getComputedStyle(c).opacity, getComputedStyle(c).borderStyle, c.querySelector('.mark').innerHTML.includes('stroke-dasharray')]; })""")
            self.assertEqual(look[0][:2], look[1][:2], "ended without a gain looks ended, as given up does")
            self.assertFalse(look[0][2], "not the mark of a direction still to do")


if __name__ == "__main__":
    unittest.main()
