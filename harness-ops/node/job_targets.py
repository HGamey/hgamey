"""Durable routing facts only; execution queues remain on their owning computers."""
import json
import time

from job_queue import RequestConflict


class JobTargets:
    def __init__(self, queue):
        self.queue = queue
        with queue._transaction(write=True) as db:
            db.execute('CREATE TABLE IF NOT EXISTS job_targets (job_id TEXT PRIMARY KEY, request_id TEXT UNIQUE, value TEXT NOT NULL)')

    @staticmethod
    def _value(row):
        return json.loads(row['value']) if row else None

    def get(self, jid):
        with self.queue._transaction() as db:
            return self._value(db.execute('SELECT value FROM job_targets WHERE job_id=?', (jid,)).fetchone())

    def by_request(self, key):
        with self.queue._transaction() as db:
            return self._value(db.execute('SELECT value FROM job_targets WHERE request_id=?', (key,)).fetchone())

    def rows(self):
        with self.queue._transaction() as db:
            return [self._value(row) for row in db.execute('SELECT value FROM job_targets')]

    def reserve(self, value):
        with self.queue._transaction(write=True) as db:
            previous = self._value(db.execute('SELECT value FROM job_targets WHERE request_id=?', (value['request_id'],)).fetchone())
            if previous:
                if previous['fingerprint'] != value['fingerprint']:
                    raise RequestConflict('request ID was already used with different content')
                return previous
            db.execute('INSERT INTO job_targets VALUES (?,?,?)', (value['id'], value['request_id'], json.dumps(value)))
            return value

    def update(self, jid, **fields):
        with self.queue._transaction(write=True) as db:
            value = self._value(db.execute('SELECT value FROM job_targets WHERE job_id=?', (jid,)).fetchone())
            value.update(fields)
            db.execute('UPDATE job_targets SET value=? WHERE job_id=?', (json.dumps(value), jid))
            return value

    def mark_sent(self, jid):
        with self.queue._transaction(write=True) as db:
            value = self._value(db.execute('SELECT value FROM job_targets WHERE job_id=?', (jid,)).fetchone())
            if value.get('stop'):
                return False
            value['sent_at'] = time.time()
            db.execute('UPDATE job_targets SET value=? WHERE job_id=?', (json.dumps(value), jid))
            return True

    def import_events(self, node_id, events):
        with self.queue._transaction(write=True) as db:
            for event in events:
                record = self._value(db.execute('SELECT value FROM job_targets WHERE job_id=?',
                                               (event['job_id'],)).fetchone())
                if record is None or record['execution_node_id'] != node_id:
                    continue
                db.execute('''INSERT OR IGNORE INTO job_events
                    (event_id,job_id,revision,kind,at,committed_status) VALUES (?,?,?,?,?,?)''',
                    (*(event[key] for key in ('event_id', 'job_id', 'revision', 'kind', 'at')),
                     json.dumps(event['committed_status'])))
