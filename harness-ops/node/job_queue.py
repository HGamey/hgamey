"""Durable admission and declared-resource scheduling; not a device lease."""
from contextlib import contextmanager
import json
from pathlib import Path
import secrets
import sqlite3
import time
from device_scope import conflicts, scope, device_access
import job_capacity
import job_events


STATES = ("preparing", "queued", "claimed", "blocked", "done")


class RequestConflict(ValueError):
    """Request content or resource state conflicts with an existing operation."""


def recovery_reservations(row, owners):
    """Borrow the blocked target's ancestor chain, never a sibling's reservation."""
    blocked = {r["job_id"]: r for r in owners if r["state"] == "blocked"}
    borrowed, target = set(), row["spec"].get("recovery_for")
    while target in blocked and target not in borrowed:
        borrowed.add(target)
        target = blocked[target]["spec"].get("recovery_for")
    return borrowed


def reservation_conflicts(row, owner):
    return (bool(set(row["resources"]) & set(owner["resources"]))
            or conflicts(scope(row["spec"]), scope(owner["spec"])))


def global_waiter(row):
    return device_access(row["spec"]) == "write" and scope(row["spec"])["scope"] == "global"


class JobQueue:
    def __init__(self, path, default_cpu=2, host_capacity=None):
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
            columns = {r["name"] for r in db.execute("PRAGMA table_info(jobs)")}
            if "state_reason" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN state_reason TEXT")
            if "host_capacity_release" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN host_capacity_release TEXT")
            for column, declaration in (("status_revision", "INTEGER NOT NULL DEFAULT 0"),
                                        ("status_projection", "TEXT"), ("status_committed_at", "REAL")):
                if column not in columns:
                    db.execute(f"ALTER TABLE jobs ADD COLUMN {column} {declaration}")
            db.execute("CREATE TABLE IF NOT EXISTS job_event_stream (id INTEGER PRIMARY KEY CHECK(id=1), stream_id TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO job_event_stream(id,stream_id) VALUES (1,?)", (secrets.token_hex(16),))
            db.execute("""CREATE TABLE IF NOT EXISTS job_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
                job_id TEXT NOT NULL, revision INTEGER NOT NULL, kind TEXT NOT NULL,
                at REAL NOT NULL, committed_status TEXT NOT NULL, UNIQUE(job_id,revision))""")
            db.execute("CREATE INDEX IF NOT EXISTS job_events_by_job ON job_events(job_id,sequence)")
            db.execute("CREATE TABLE IF NOT EXISTS host_capacity (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL)")
            self._configure_capacity(db, host_capacity, default_cpu)

    @staticmethod
    def _configure_capacity(db, slots, default_cpu=2):
        previous = db.execute("SELECT value FROM host_capacity WHERE id=1").fetchone()
        if previous and slots is None:
            return
        value = job_capacity.normalize_slots(slots if slots is not None else {"cpu": default_cpu, "gpu": 0, "io": 0})
        if previous and json.loads(previous["value"])["slots"] == value:
            return
        config = {"schema": "phonefarm/host-capacity@1", "slots": value,
                  "source": "file" if slots is not None else "initial_default", "updated_at": time.time(),
                  "basis": "declared_scheduler_slots"}
        db.execute("INSERT INTO host_capacity(id,value) VALUES (1,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value",
                   (json.dumps(config),))

    @staticmethod
    def _capacity(db):
        return json.loads(db.execute("SELECT value FROM host_capacity WHERE id=1").fetchone()["value"])

    def capacity(self):
        with self._transaction() as db:
            return self._capacity(db)

    def capacity_snapshot(self, rows=None):
        with self._transaction() as db:
            if rows is None:
                rows = [self._row(r) for r in db.execute("SELECT * FROM jobs WHERE state IN ('claimed','blocked')")]
            return job_capacity.snapshot(self._capacity(db), rows)

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
        value["state_reason"] = json.loads(value["state_reason"]) if value["state_reason"] else None
        value["host_capacity_release"] = json.loads(value["host_capacity_release"]) if value["host_capacity_release"] else None
        value["status_projection"] = json.loads(value["status_projection"]) if value["status_projection"] else None
        return value

    def _publish(self, db, jid, kind="queue_transition", observation=None):
        """Commit a bounded projection and its event with the owning SQL change."""
        row = self._row(db.execute("SELECT * FROM jobs WHERE job_id=?", (jid,)).fetchone())
        if row is None:
            return None
        projection = job_events.projection(row, observation)
        if projection == row["status_projection"]:
            return row
        revision, at = row["status_revision"] + 1, time.time()
        if row["status_revision"] == 0 and kind != "admitted":
            kind = "baseline"
        encoded = json.dumps(projection)
        db.execute("UPDATE jobs SET status_revision=?,status_projection=?,status_committed_at=? WHERE job_id=?",
                   (revision, encoded, at, jid))
        db.execute("INSERT INTO job_events(event_id,job_id,revision,kind,at,committed_status) VALUES (?,?,?,?,?,?)",
                   (secrets.token_hex(16), jid, revision, kind, at, encoded))
        row.update(status_revision=revision, status_projection=projection, status_committed_at=at)
        return row

    def observe(self, jid, observation):
        # Caller reads the worker receipt and commits it under the intake lock.
        with self._transaction(write=True) as db:
            return self._publish(db, jid, "worker_observation", observation)

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
            # Configuration may change between the HTTP preflight and this
            # transaction. Existing IDs above retain their original admission.
            job_capacity.check_fit(spec, self._capacity(db))
            job_id = secrets.token_hex(6)
            db.execute("""INSERT INTO jobs
                (job_id, request_id, fingerprint, spec, resources, submitted_at, state)
                VALUES (?, ?, ?, ?, ?, ?, 'preparing')""",
                (job_id, request_id, fingerprint, json.dumps(spec),
                 json.dumps(resources), time.time()))
            return self._publish(db, job_id, "admitted"), True

    def ready(self, jid):
        """Publish prepared inputs once; later states are never rewound."""
        with self._transaction(write=True) as db:
            db.execute("UPDATE jobs SET state='queued' WHERE job_id=? AND state='preparing'", (jid,))
            return self._publish(db, jid)

    def cancel_pending(self, jid):
        """Win cancellation only before a dispatcher has claimed the job."""
        with self._transaction(write=True) as db:
            reason = json.dumps({"kind": "cancelled_before_launch", "at": time.time()})
            release = json.dumps({"at": time.time(), "basis": "not_started"})
            result = db.execute("UPDATE jobs SET state='done', state_reason=?, host_capacity_release=COALESCE(host_capacity_release,?) WHERE job_id=? AND state IN ('preparing', 'queued')", (reason, release, jid))
            if result.rowcount == 1:
                self._publish(db, jid)
            return result.rowcount == 1

    def claim_next(self, max_running, admit=None):
        with self._transaction(write=True) as db:
            active = [self._row(row) for row in db.execute("SELECT * FROM jobs WHERE state IN ('claimed', 'blocked')")]
            if sum(row["state"] == "claimed" for row in active) >= max_running:
                return None
            capacity = job_capacity.snapshot(self._capacity(db), active)
            # Insertion order survives wall-clock changes and equal timestamps.
            global_waiting = False
            for row in db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY sequence"):
                value = self._row(row)
                later_device_write = (device_access(value["spec"]) == "write"
                                      and scope(value["spec"])["scope"] == "device")
                if global_waiting and later_device_write:
                    continue
                if global_waiter(value):
                    global_waiting = True
                if admit and not admit(value):
                    continue
                if job_capacity.waiting(value["spec"], capacity, active):
                    continue
                borrowed = recovery_reservations(value, active)
                if not any(reservation_conflicts(value, owner) for owner in active
                           if owner["job_id"] not in borrowed):
                    db.execute("UPDATE jobs SET state='claimed' WHERE job_id=?", (value["job_id"],))
                    return self._publish(db, value["job_id"])
            return None

    def set_state(self, jid, state, reason=None, host_capacity_release=None, observation=None):
        if state not in STATES:
            raise ValueError("unknown job queue state")
        with self._transaction(write=True) as db:
            db.execute("UPDATE jobs SET state=?, state_reason=?, host_capacity_release=COALESCE(host_capacity_release,?) WHERE job_id=?",
                       (state, json.dumps(reason) if reason else None,
                        json.dumps(host_capacity_release) if host_capacity_release else None, jid))
            return self._publish(db, jid, observation=observation)

    def release_capacity(self, jid, fact, observation=None):
        with self._transaction(write=True) as db:
            db.execute("UPDATE jobs SET host_capacity_release=COALESCE(host_capacity_release,?) WHERE job_id=? AND state='blocked'",
                       (json.dumps(fact), jid))
            self._publish(db, jid, observation=observation)

    def record_unstarted(self, jid, outcome, note):
        """Persist caller-confirmed no-launch intent without overriding cancellation."""
        now = time.time()
        reason = {"kind": "durability", "stage": "unstarted", "message": note, "at": now,
                  "unstarted": {"outcome": outcome, "note": note, "at": now}}
        with self._transaction(write=True) as db:
            db.execute("UPDATE jobs SET state='blocked', state_reason=?, host_capacity_release=COALESCE(host_capacity_release,?) WHERE job_id=? AND state!='done'",
                       (json.dumps(reason), json.dumps({"at": now, "basis": "not_started"}), jid))
            return self._publish(db, jid)

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
