"""Durable admission and declared-resource scheduling; not a device lease."""
from contextlib import contextmanager
import json
from pathlib import Path
import secrets
import sqlite3
import time


STATES = ("preparing", "queued", "claimed", "blocked", "done")


class RequestConflict(ValueError):
    """An idempotency key was already used for different request content."""


class JobQueue:
    def __init__(self, path):
        self.path = str(Path(path).absolute())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._transaction(write=True) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                sequence INTEGER PRIMARY KEY,
                job_id TEXT NOT NULL UNIQUE,
                request_id TEXT NOT NULL UNIQUE,
                fingerprint TEXT NOT NULL,
                spec TEXT NOT NULL,
                resources TEXT NOT NULL,
                submitted_at REAL NOT NULL,
                state TEXT NOT NULL CHECK (state IN
                    ('preparing', 'queued', 'claimed', 'blocked', 'done'))
            )""")

    @contextmanager
    def _transaction(self, write=False):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _row(row):
        if row is None:
            return None
        value = dict(row)
        value.pop("sequence")
        value["spec"] = json.loads(value["spec"])
        value["resources"] = json.loads(value["resources"])
        return value

    def reserve(self, request_id, fingerprint, spec, resources):
        """The caller supplies a non-secret spec and its content fingerprint."""
        if not isinstance(spec, dict):
            raise ValueError("spec must be an object")
        if not isinstance(resources, list) or any(not isinstance(r, str) for r in resources):
            raise ValueError("resources must be a list of strings")
        with self._transaction(write=True) as db:
            prior = db.execute("SELECT * FROM jobs WHERE request_id=?", (request_id,)).fetchone()
            if prior is not None:
                if prior["fingerprint"] != fingerprint:
                    raise RequestConflict("request_id was already used for different content")
                return self._row(prior), False
            job_id = secrets.token_hex(6)
            db.execute("""INSERT INTO jobs
                (job_id, request_id, fingerprint, spec, resources, submitted_at, state)
                VALUES (?, ?, ?, ?, ?, ?, 'preparing')""",
                (job_id, request_id, fingerprint, json.dumps(spec),
                 json.dumps(resources), time.time()))
            return self._row(db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()), True

    def ready(self, jid):
        """Publish prepared inputs once; later states are never rewound."""
        with self._transaction(write=True) as db:
            db.execute("UPDATE jobs SET state='queued' WHERE job_id=? AND state='preparing'", (jid,))
            return self._row(db.execute("SELECT * FROM jobs WHERE job_id=?", (jid,)).fetchone())

    def claim_next(self, max_running):
        with self._transaction(write=True) as db:
            active = db.execute("SELECT resources FROM jobs WHERE state IN ('claimed', 'blocked')").fetchall()
            if len(active) >= max_running:
                return None
            held = {resource for row in active for resource in json.loads(row["resources"])}
            # Insertion order survives wall-clock changes and equal timestamps.
            for row in db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY sequence"):
                value = self._row(row)
                if held.isdisjoint(value["resources"]):
                    db.execute("UPDATE jobs SET state='claimed' WHERE job_id=?", (value["job_id"],))
                    value["state"] = "claimed"
                    return value
            return None

    def set_state(self, jid, state):
        if state not in STATES:
            raise ValueError("unknown job queue state")
        with self._transaction(write=True) as db:
            db.execute("UPDATE jobs SET state=? WHERE job_id=?", (state, jid))
            return self._row(db.execute("SELECT * FROM jobs WHERE job_id=?", (jid,)).fetchone())

    def get(self, jid):
        with self._transaction() as db:
            return self._row(db.execute("SELECT * FROM jobs WHERE job_id=?", (jid,)).fetchone())

    def by_request(self, request_id):
        with self._transaction() as db:
            return self._row(db.execute("SELECT * FROM jobs WHERE request_id=?", (request_id,)).fetchone())

    def rows(self, states=None):
        with self._transaction() as db:
            if states is None:
                rows = db.execute("SELECT * FROM jobs ORDER BY sequence")
            elif not states:
                return []
            else:
                slots = ",".join("?" for _ in states)
                rows = db.execute(f"SELECT * FROM jobs WHERE state IN ({slots}) ORDER BY sequence", tuple(states))
            return [self._row(row) for row in rows]
