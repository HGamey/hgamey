"""History listing keeps the same live records without per-job database reads."""
import sqlite3
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

import test_node_read as fixture


class JobsListCostTests(unittest.TestCase):
    def setUp(self):
        self.env = fixture.NodeReadTests()
        self.env.setUp()
        self.addCleanup(self.env.tearDown)
        self.jobs = self.env.jobs
        self.addCleanup(self.jobs.center.close)
        self.addCleanup(self.jobs.dispatch.close)

    def finished(self, number):
        jid = f'{number:012x}'
        spec = {'bin': 'fixture', 'argv': ['measure', 'fake-phone'], 'label': f'Run {number}',
                'research_id': 'selected' if number % 5 == 0 else 'other', 'device_access': 'none'}
        row, _ = self.jobs.dispatch.queue.reserve(jid, jid, spec, [], job_id=jid)
        meta = self.jobs.dispatch.base_meta(row)
        meta.update(started=number, phase='finished', state='finished', exit=0, outcome='succeeded')
        meta['execution'].update(worker_alive=False, owned_process_group={'status': 'absent'})
        self.jobs._read_root.mkdir(jid)
        self.jobs._write_meta(jid, meta)
        self.jobs.dispatch.queue.set_state(jid, 'done', observation=meta)
        self.jobs.center.targets.reserve({'id': jid, 'request_id': jid, 'fingerprint': jid,
            'execution_node_id': self.jobs.node_id, 'request': {'spec': spec}, 'submitted_at': number,
            'device_selection': None, 'sent_at': number, 'stop': None, 'observation': meta})
        return jid

    def test_history_growth_does_not_repeat_sql_reads_for_every_job(self):
        measured = {}
        for lower, upper in ((1, 26), (26, 201)):
            for number in range(lower, upper):
                self.finished(number)
            for research in ('', 'selected'):
                query = {'limit': 20, **({'research': research} if research else {})}
                with patch('sqlite3.connect', wraps=sqlite3.connect) as connections:
                    started = time.perf_counter()
                    page = self.env.get_json('/jobs?' + urlencode(query))
                    elapsed = time.perf_counter() - started
                wanted = [f'{n:012x}' for n in range(upper - 1, 0, -1) if not research or n % 5 == 0][:20]
                self.assertEqual(page['items'], [self.jobs.status(jid) for jid in wanted])
                self.assertEqual(page['warnings'], [])
                measured[upper - 1, research] = (connections.call_count, elapsed)
        for research in ('', 'selected'):
            with self.subTest(research=research):
                small, large = measured[25, research], measured[200, research]
                self.assertLessEqual(large[0], small[0] + 4,
                    f'25 -> 200 completed jobs caused per-job SQL I/O: {small} -> {large} (connections, seconds)')

    def test_pages_preserve_detail_fields_filters_and_warnings_outside_the_page(self):
        ids = [self.finished(n) for n in range(1, 13)]
        store, queue = self.jobs._read_root, self.jobs.dispatch.queue
        store.write_json(ids[0] + '/queue-release.json', {'note': 'historical release receipt'})
        store.write_json(ids[0] + '/input-write-limits.json', {'status': 'ready', 'written_bytes': 7})
        self.jobs.center.targets.update(ids[0], device_selection={'ref': self.jobs.node_id + '/fake-phone'})
        queue.set_state(ids[1], 'queued')
        queue.set_state(ids[2], 'preparing')
        queue.set_state(ids[3], 'blocked', reason={'kind': 'durability', 'stage': 'fixture', 'message': 'disk'})
        queue.set_state(ids[4], 'done', reason={'kind': 'manual_unknown'})
        queue.set_state(ids[5], 'queued')
        queue.cancel_pending(ids[5])
        ids.append(self.env.fixture(13, research_id='selected'))  # Legacy file without an admission row.
        broken = 'ffffffffffff'
        store.mkdir(broken)
        with store.create_file(broken + '/meta.json') as stream:
            stream.write(b'{')
        queries = (({}, ids), ({'research': 'selected'}, [ids[4], ids[9], ids[12]]),
                   ({'query': 'RUN'}, ids[:12]), ({'state': 'running'}, ids[1:3]),
                   ({'state': 'finished'}, [ids[0], ids[3], *ids[5:]]),
                   ({'state': 'lost'}, [ids[4]]), ({'state': 'blocked'}, [ids[3]]),
                   ({'state': 'queued'}, [ids[1]]), ({'state': 'preparing'}, [ids[2]]))
        for query, selected in queries:
            with self.subTest(query=query):
                expected = [self.jobs.status(jid) for jid in selected]
                expected.sort(key=lambda m: (m.get('started', 0), m['id']), reverse=True)
                actual, cursor = [], None
                while True:
                    page = self.env.get_json('/jobs?' + urlencode({**query, 'limit': 3,
                        **({'cursor': cursor} if cursor else {})}))
                    actual.extend(page['items'])
                    self.assertEqual(page['warning_count'], 1)
                    self.assertEqual(page['warnings'], [{'job': broken, 'error': 'metadata_unavailable'}])
                    cursor = page['next_cursor']
                    if cursor is None:
                        break
                self.assertEqual(actual, expected)
        # No cross-request cache: the next list must read changed receipts.
        meta = self.jobs._read_meta(ids[0])
        meta['label'] = 'Changed label'
        self.jobs._write_meta(ids[0], meta)
        page = self.env.get_json('/jobs?query=Changed')
        self.assertEqual(page['items'], [self.jobs.status(ids[0])])

    def test_remote_history_uses_one_directory_read_and_keeps_saved_observations(self):
        directory = self.jobs.directory
        remote = directory.join({'invite': directory.invite()['code'], 'name': 'worker', 'node_id': 'b' * 32})['node']
        for n in range(1, 31):
            jid = f'{n:012x}'
            self.jobs.center.targets.reserve({'id': jid, 'request_id': jid, 'fingerprint': jid,
                'execution_node_id': remote['node_id'], 'request': {'spec': {'bin': 'fixture'}},
                'submitted_at': n, 'sent_at': n, 'stop': None, 'device_selection': {'serial': 'remote-phone'},
                'observation': {'id': jid, 'started': n, 'state': 'finished', 'queue': {'state': 'done'},
                                'label': 'remote original', 'custom_fact': n},
                'artifacts_sync': {'status': 'complete'}})
        expected = [self.jobs.status(f'{n:012x}') for n in range(30, 10, -1)]
        with patch.object(directory, 'list', wraps=directory.list) as reads, \
                patch.object(self.jobs.center, 'rpc', side_effect=AssertionError('list must not dial a worker')):
            page = self.env.get_json('/jobs?limit=20')
        self.assertEqual(page['items'], expected)
        self.assertLessEqual(reads.call_count, 1, 'the same node directory was reread for every remote job')
        local = self.finished(31)
        with patch.object(directory, 'list', side_effect=ValueError('fixture registry is unreadable')):
            page = self.env.get_json('/jobs?limit=20')
        self.assertEqual(page['items'], [self.jobs.status(local)])
        self.assertEqual(page['warning_count'], 30)
        self.assertEqual({w['job'] for w in page['warnings']}, {f'{n:012x}' for n in range(1, 31)})


if __name__ == '__main__':
    unittest.main()
